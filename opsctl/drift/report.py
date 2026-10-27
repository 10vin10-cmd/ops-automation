"""Markdown for a drift run: what drifted, what it means, what happens next."""

from __future__ import annotations

import json

from opsctl.drift.remediate import Mode, Plan, Result

ICON = {"critical": "🔴", "high": "🟠", "medium": "🟡", "low": "⚪"}
MODE_TEXT = {
    Mode.AUTO: "revert automatically",
    Mode.APPROVED: "revert (approved)",
    Mode.NEEDS_APPROVAL: "waiting for approval",
    Mode.MANUAL: "manual fix",
    Mode.REPORT: "report",
    Mode.SKIPPED: "skipped (protected)",
}


def _short(value, limit=60) -> str:
    text = json.dumps(value) if not isinstance(value, str) else value
    return text if len(text) <= limit else text[: limit - 1] + "…"


def markdown(p: Plan, result: Result | None = None) -> str:
    if not p.items:
        return "## Drift\n\nNo drift: deployed resources match the code. ✅\n"
    counts = {s: sum(1 for i in p.items if i.classified.rule.severity == s)
              for s in ("critical", "high", "medium", "low")}
    head = " · ".join(f"{ICON[s]} {n} {s}" for s, n in counts.items() if n)
    out = [f"## Drift: {len(p.items)} resource(s)", "", head, "",
           "| | Resource | Rule | What changed | Next step |", "|---|---|---|---|---|"]
    for i in p.items:
        c = i.classified
        changes = c.triggering or c.drift.changes
        what = "deleted" if c.drift.status == "deleted" else "<br>".join(
            f"`{ch.path}` added: **{_short(ch.actual, 70)}**" if ch.path.endswith("[+]") else
            f"`{ch.path}` removed: {_short(ch.expected, 70)}" if ch.path.endswith("[-]") else
            f"`{ch.path}`: {_short(ch.expected, 30)} → **{_short(ch.actual, 40)}**"
            for ch in changes[:3])
        if len(changes) > 3:
            what += f"<br>…and {len(changes) - 3} more"
        out.append(f"| {ICON[c.rule.severity]} | `{c.drift.address}` | {c.rule.id} | {what} | "
                   f"**{MODE_TEXT[i.mode]}**: {i.reason} |")
    out.append("")

    waiting = [i for i in p.items if i.mode == Mode.NEEDS_APPROVAL]
    if waiting:
        out += ["### Needs approval", "",
                "Re-run with `--approve <address-or-rule>` to include these:", ""]
        out += [f"- `{i.address}` ({i.classified.rule.id})" for i in waiting]
        out.append("")
    adopt = [i for i in p.items if i.action == "adopt"]
    if adopt:
        out += ["### Codify or undo", "",
                "These look deliberate. Update the code to match, or revert them:", ""]
        out += [f"- `{i.address}`: " + ", ".join(f"`{ch.path}` = {_short(ch.actual, 40)}"
                                                  for ch in i.classified.drift.changes[:4])
                for i in adopt]
        out.append("")
    if result:
        state = ("applied" if result.ok else "FAILED") if result.applied else "dry run"
        out += [f"### Remediation ({state})", "", f"```\n{' '.join(result.command)}\n```"]
        if result.applied:
            out += ["", f"```\n{result.output[-1500:]}\n```"]
    return "\n".join(out)
