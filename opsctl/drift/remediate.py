"""Turn classified drift into a remediation plan, then carry it out.

Guardrails, in order:
  1. Protected resources (tag ops:drift=ignore) are reported, never touched.
  2. CloudFormation drift can't be reverted by redeploying the same template
     (CloudFormation sees no change), so it becomes a manual action.
  3. Only rules marked `auto` revert without a person, and at most N per run:
     a burst of drift is more likely a broken plan than many independent edits.
  4. Everything else needs an explicit approval (by address or rule id).
  5. Dry run is the default; nothing changes without --apply.
"""

from __future__ import annotations

import subprocess
from collections.abc import Callable
from dataclasses import dataclass, field
from enum import StrEnum

from opsctl.drift.policy import Classified, Policy


class Mode(StrEnum):
    AUTO = "auto"                  # will run on --apply
    APPROVED = "approved"          # will run on --apply, a person approved it
    NEEDS_APPROVAL = "needs_approval"
    MANUAL = "manual"              # can't be automated; instructions given
    REPORT = "report"              # nothing to do but tell people
    SKIPPED = "skipped"            # protected


@dataclass
class Item:
    classified: Classified
    action: str                    # revert | adopt | notify
    mode: Mode
    reason: str

    @property
    def address(self) -> str:
        return self.classified.drift.address


@dataclass
class Plan:
    items: list[Item] = field(default_factory=list)

    def runnable(self) -> list[Item]:
        return [i for i in self.items if i.mode in (Mode.AUTO, Mode.APPROVED)]


def plan(classified: list[Classified], policy: Policy, approvals: set[str] | None = None) -> Plan:
    approvals = approvals or set()
    out = Plan()
    autos = 0
    by_severity = sorted(classified, key=lambda c: ["critical", "high", "medium", "low"]
                         .index(c.rule.severity))
    for c in by_severity:
        d, rule = c.drift, c.rule
        if d.tags.get(policy.protected_tag) == "ignore":
            out.items.append(Item(c, "notify", Mode.SKIPPED,
                                  f"protected by tag {policy.protected_tag}=ignore"))
            continue
        if rule.remediation == "notify":
            out.items.append(Item(c, "notify", Mode.REPORT, rule.description))
            continue
        if rule.remediation == "adopt":
            out.items.append(Item(c, "adopt", Mode.REPORT,
                                  "may be intentional: codify it in the IaC or revert it"))
            continue
        # revert
        if d.source == "cloudformation":
            out.items.append(Item(c, "revert", Mode.MANUAL,
                                  "CloudFormation won't revert drift on an unchanged template: "
                                  "fix the resource, or update the stack so the property changes"))
        elif d.address in approvals or rule.id in approvals:
            out.items.append(Item(c, "revert", Mode.APPROVED, "approved"))
        elif rule.auto and autos < policy.max_auto_reverts_per_run:
            autos += 1
            out.items.append(Item(c, "revert", Mode.AUTO, f"{rule.severity}: {rule.description}"))
        elif rule.auto:
            out.items.append(Item(c, "revert", Mode.NEEDS_APPROVAL,
                                  f"over the limit of {policy.max_auto_reverts_per_run} automatic "
                                  "reverts per run"))
        else:
            out.items.append(Item(c, "revert", Mode.NEEDS_APPROVAL,
                                  f"{rule.severity}: needs approval ({rule.id})"))
    return out


Runner = Callable[[list[str], str], subprocess.CompletedProcess]


def _run(cmd: list[str], cwd: str) -> subprocess.CompletedProcess:
    return subprocess.run(cmd, cwd=cwd, capture_output=True, text=True)


@dataclass
class Result:
    command: list[str]
    applied: bool
    output: str = ""
    ok: bool = True


def execute(p: Plan, workdir: str, apply: bool = False, runner: Runner = _run) -> Result | None:
    """Revert all runnable Terraform items in one targeted apply."""
    targets = [i.address for i in p.runnable() if i.classified.drift.source == "terraform"]
    if not targets:
        return None
    cmd = ["terraform", "apply", "-input=false", "-auto-approve",
           *[f"-target={t}" for t in targets]]
    if not apply:
        return Result(cmd, applied=False, output="dry run: nothing changed")
    proc = runner(cmd, workdir)
    return Result(cmd, applied=True, output=(proc.stdout + proc.stderr)[-4000:],
                  ok=proc.returncode == 0)
