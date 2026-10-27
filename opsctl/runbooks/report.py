"""Markdown for a runbook execution: outcome, then the full audit trail."""

from __future__ import annotations

import json

from opsctl.runbooks.engine import Execution, Status

ICON = {Status.SUCCEEDED: "✅", Status.WAITING_APPROVAL: "⏸️", Status.SKIPPED: "⏭️",
        Status.FAILED: "❌", Status.REJECTED: "🚫", Status.RUNNING: "▶️"}


def _detail(d) -> str:
    if d is None:
        return ""
    text = d if isinstance(d, str) else json.dumps(d, default=str)
    text = text.replace("|", "\\|").replace("\n", " ")
    return text if len(text) <= 140 else text[:139] + "…"


def markdown(ex: Execution) -> str:
    target = " / ".join(str(v) for v in ex.target.values())
    out = [f"### {ICON[ex.status]} `{ex.runbook}` on {target}: {ex.status.value}",
           "", f"Execution `{ex.id}` · alarm `{ex.alarm.get('name')}` · started {ex.started_at}", ""]
    if ex.status == Status.WAITING_APPROVAL:
        out += [f"Waiting for approval of **{ex.waiting_step}**: "
                f"`opsctl runbook approve {ex.id}` or `opsctl runbook reject {ex.id}`", ""]
    out += ["| When | Who | What | Step | Detail |", "|---|---|---|---|---|"]
    for a in ex.audit:
        out.append(f"| {a['at'][11:19]} | {a['actor']} | {a['event']} | {a['step'] or ''} | "
                   f"{_detail(a['detail'])} |")
    return "\n".join(out)
