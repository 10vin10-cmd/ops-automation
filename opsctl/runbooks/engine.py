"""Run a runbook against an alarm, with guardrails.

  cooldown        one execution per runbook and target at a time; an alarm
                  that flaps doesn't restart a service five times
  daily limit     a runbook that keeps firing has stopped helping: stop and
                  page a person instead
  conditions      each step runs only if its `when:` holds, evaluated on the
                  outputs of earlier checks
  approval gates  risky steps pause the execution. On approval the checks
                  run again and the step only proceeds if its condition still
                  holds: the world may have changed while waiting
  audit trail     every check, decision, action, approval and notification is
                  recorded with who and when

Runbook files are YAML (or JSON) in runbooks/; see runbooks/ecs-5xx.yaml.
"""

from __future__ import annotations

import fnmatch
import json
import re
import uuid
from dataclasses import asdict, dataclass, field
from datetime import UTC, datetime, timedelta
from enum import StrEnum
from pathlib import Path
from typing import Any

from opsctl.runbooks import actions as act
from opsctl.runbooks.expr import MISSING, evaluate
from opsctl.runbooks.store import Store


class Status(StrEnum):
    RUNNING = "running"
    WAITING_APPROVAL = "waiting_approval"
    SUCCEEDED = "succeeded"
    FAILED = "failed"
    SKIPPED = "skipped"
    REJECTED = "rejected"


@dataclass
class Step:
    id: str
    check: str | None = None
    action: str | None = None
    params: dict = field(default_factory=dict)
    when: str | None = None
    requires_approval: bool = False

    def __post_init__(self):
        if bool(self.check) == bool(self.action):
            raise ValueError(f"step {self.id}: give exactly one of check or action")
        if self.check and self.check not in act.CHECKS:
            raise ValueError(f"step {self.id}: unknown check {self.check}")
        if self.action and self.action not in act.ACTIONS:
            raise ValueError(f"step {self.id}: unknown action {self.action}")


@dataclass
class Runbook:
    id: str
    description: str
    trigger: dict
    target: dict
    steps: list[Step]
    guards: dict = field(default_factory=dict)

    @classmethod
    def from_dict(cls, raw: dict) -> Runbook:
        steps = [Step(**s) for s in raw["steps"]]
        ids = [s.id for s in steps]
        if len(ids) != len(set(ids)):
            raise ValueError(f"runbook {raw['id']}: duplicate step ids")
        return cls(id=raw["id"], description=raw.get("description", ""), trigger=raw["trigger"],
                   target=raw.get("target", {}), steps=steps, guards=raw.get("guards", {}))

    def matches(self, alarm: str, state: str) -> bool:
        return (fnmatch.fnmatchcase(alarm, self.trigger.get("alarm", "*"))
                and state == self.trigger.get("state", "ALARM"))


def load_runbooks(directory: str | Path) -> list[Runbook]:
    out = []
    for path in sorted(Path(directory).iterdir()):
        if path.suffix in (".yaml", ".yml"):
            import yaml  # not needed in Lambda, where runbooks are shipped as JSON
            out.append(Runbook.from_dict(yaml.safe_load(path.read_text())))
        elif path.suffix == ".json":
            out.append(Runbook.from_dict(json.loads(path.read_text())))
    return out


_FULL = re.compile(r"^\$\{(.+)\}$")
_PART = re.compile(r"\$\{([^}]+)\}")


def render(value: Any, names: dict) -> Any:
    """`${expr}` alone keeps the result's type; inside a string it's formatted."""
    if isinstance(value, dict):
        return {k: render(v, names) for k, v in value.items()}
    if isinstance(value, list):
        return [render(v, names) for v in value]
    if not isinstance(value, str):
        return value
    if m := _FULL.match(value):
        out = evaluate(m.group(1), names)
        return None if out is MISSING else out
    return _PART.sub(lambda m: "" if (v := evaluate(m.group(1), names)) is MISSING else str(v), value)


@dataclass
class Execution:
    id: str
    runbook: str
    target: dict
    status: Status
    started_at: str
    lock_key: str = ""
    outputs: dict = field(default_factory=dict)       # step id -> output (+ "ran")
    done_actions: list[str] = field(default_factory=list)
    approvals: dict = field(default_factory=dict)     # step id -> approver
    waiting_step: str | None = None
    audit: list[dict] = field(default_factory=list)
    alarm: dict = field(default_factory=dict)

    def log(self, actor: str, event: str, step: str | None = None, detail: Any = None,
            now: datetime | None = None) -> None:
        self.audit.append({"at": (now or datetime.now(UTC)).isoformat(timespec="seconds"),
                           "actor": actor, "event": event, "step": step, "detail": detail})

    def to_dict(self) -> dict:
        d = asdict(self)
        d["status"] = self.status.value
        return d

    @classmethod
    def from_dict(cls, d: dict) -> Execution:
        return cls(**{**d, "status": Status(d["status"])})


class Engine:
    def __init__(self, store: Store, ctx: act.Context):
        self.store = store
        self.ctx = ctx

    # -- entry points ---------------------------------------------------------------

    def handle_alarm(self, runbooks: list[Runbook], alarm: str, state: str,
                     dimensions: dict[str, str]) -> list[Execution]:
        return [self.start(rb, alarm, dimensions) for rb in runbooks if rb.matches(alarm, state)]

    def start(self, rb: Runbook, alarm: str, dimensions: dict[str, str]) -> Execution:
        names = {"alarm": alarm, "dimensions": dimensions}
        target = render(rb.target, names)
        ex = Execution(id=uuid.uuid4().hex[:12], runbook=rb.id, target=target,
                       status=Status.RUNNING, started_at=self.ctx.now.isoformat(timespec="seconds"),
                       alarm={"name": alarm, "dimensions": dimensions})
        ex.log("system", "triggered", detail=f"alarm {alarm}", now=self.ctx.now)

        key = f"{rb.id}#" + "/".join(str(v) for v in target.values())
        cooldown = timedelta(minutes=rb.guards.get("cooldown_minutes", 30))
        if not self.store.acquire(key, self.ctx.now, cooldown):
            ex.status = Status.SKIPPED
            ex.log("system", "skipped", detail=f"in cooldown ({cooldown}) for {key}", now=self.ctx.now)
            return self._finish(ex)
        ex.lock_key = key
        limit = rb.guards.get("max_runs_per_day")
        if limit and not self.store.count_run(key, self.ctx.now.date().isoformat(), limit):
            ex.status = Status.SKIPPED
            ex.log("system", "skipped", detail=f"daily limit of {limit} runs reached; a person "
                   "should look", now=self.ctx.now)
            self._notify(rb, ex, f"Runbook {rb.id} hit its daily limit on {key}. Not acting.")
            return self._finish(ex)
        return self._run(rb, ex, actor="system")

    def decide(self, rb: Runbook, execution_id: str, approver: str, approve: bool) -> Execution:
        ex = self.store.load(execution_id)
        if ex.status != Status.WAITING_APPROVAL:
            raise ValueError(f"execution {execution_id} is {ex.status}, not waiting for approval")
        if not approve:
            ex.status = Status.REJECTED
            ex.log(approver, "rejected", ex.waiting_step, now=self.ctx.now)
            self._notify(rb, ex, f"{approver} rejected step {ex.waiting_step} of {rb.id}.")
            return self._finish(ex)
        ex.approvals[ex.waiting_step] = approver
        ex.log(approver, "approved", ex.waiting_step, now=self.ctx.now)
        ex.status, ex.waiting_step = Status.RUNNING, None
        return self._run(rb, ex, actor=approver)

    # -- the run loop ----------------------------------------------------------------

    def _run(self, rb: Runbook, ex: Execution, actor: str) -> Execution:
        names: dict[str, Any] = {"target": ex.target, "alarm": ex.alarm.get("name"),
                                 "dimensions": ex.alarm.get("dimensions", {}), "runbook": rb.id}
        for step in rb.steps:
            if step.action and step.id in ex.done_actions:
                names[step.id] = ex.outputs[step.id]  # never repeat an action
                continue
            if step.when and not evaluate(step.when, names):
                names[step.id] = ex.outputs[step.id] = {"ran": False}
                if step.id in ex.approvals:
                    ex.log("system", "skipped", step.id,
                           "approved, but its condition no longer holds", now=self.ctx.now)
                continue
            params = render(step.params, names)
            if step.action and step.requires_approval and step.id not in ex.approvals \
                    and not self.ctx.dry_run:
                ex.status, ex.waiting_step = Status.WAITING_APPROVAL, step.id
                ex.log("system", "approval requested", step.id, params, now=self.ctx.now)
                target = " / ".join(str(v) for v in ex.target.values())
                self._notify(rb, ex, f"Runbook {rb.id} wants to run {step.action} on {target} "
                             f"with {params}. Approve: `opsctl runbook approve {ex.id}`. "
                             f"Reject: `opsctl runbook reject {ex.id}`.")
                return self._finish(ex)
            try:
                fn = act.CHECKS[step.check] if step.check else act.ACTIONS[step.action]
                out = fn(self.ctx, **params)
            except Exception as err:  # record, tell people, stop
                ex.outputs[step.id] = {"ran": False, "error": f"{type(err).__name__}: {err}"}
                ex.status = Status.FAILED
                ex.log("system", "failed", step.id, str(err), now=self.ctx.now)
                self._notify(rb, ex, f"Runbook {rb.id} failed at {step.id}: {err}")
                return self._finish(ex)
            names[step.id] = ex.outputs[step.id] = {"ran": True, **out}
            if step.action:
                ex.done_actions.append(step.id)
                ex.log(actor if step.id in ex.approvals else "system",
                       "dry run" if self.ctx.dry_run else "action", step.id, out, now=self.ctx.now)
            else:
                ex.log("system", "check", step.id, out, now=self.ctx.now)
        ex.status = Status.SUCCEEDED
        return self._finish(ex)

    def _notify(self, rb: Runbook, ex: Execution, message: str) -> None:
        act.notify(self.ctx, message, subject=f"[{rb.id}] {ex.status}")
        ex.log("system", "notified", detail=message, now=self.ctx.now)

    def _finish(self, ex: Execution) -> Execution:
        self.store.save(ex.to_dict())
        return ex
