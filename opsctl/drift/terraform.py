"""Drift from Terraform.

Run a refresh-only plan, which compares state with reality without proposing
changes, and export it as JSON:

    terraform plan -refresh-only -out=drift.tfplan
    terraform show -json drift.tfplan > drift.json

The JSON's `resource_drift` lists every resource whose real configuration no
longer matches state, with `before` (what Terraform last applied) and `after`
(what exists now).
"""

from __future__ import annotations

import json
from pathlib import Path

from opsctl.drift.model import Drift, diff

# Computed or volatile attributes that change without anyone touching anything.
IGNORED = {"id", "arn", "owner_id", "timeouts", "last_modified", "etag", "version"}


def parse_plan(plan: dict) -> list[Drift]:
    drifts = []
    for rd in plan.get("resource_drift", []):
        change = rd.get("change", {})
        actions = change.get("actions", [])
        before = change.get("before") or {}
        after = change.get("after")
        if "delete" in actions or after is None:
            drifts.append(Drift("terraform", rd["address"], rd["type"], "deleted",
                                physical_id=str(before.get("id", "")),
                                tags=dict(before.get("tags") or {})))
            continue
        changes = [c for c in diff(before, after)
                   if c.path.split(".")[0].split("[")[0] not in IGNORED]
        if changes:
            drifts.append(Drift("terraform", rd["address"], rd["type"], "modified", changes,
                                physical_id=str(before.get("id", "")),
                                tags=dict(after.get("tags") or before.get("tags") or {})))
    return drifts


def load(path: str | Path) -> list[Drift]:
    return parse_plan(json.loads(Path(path).read_text()))
