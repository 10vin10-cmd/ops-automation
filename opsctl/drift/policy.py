"""Classify drift against the policy file (policies/drift.yaml)."""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from fnmatch import fnmatchcase
from pathlib import Path

import yaml

from opsctl.drift.model import Change, Drift

SEVERITY_ORDER = {"critical": 0, "high": 1, "medium": 2, "low": 3}
DEFAULT_POLICY = Path(__file__).resolve().parents[2] / "policies" / "drift.yaml"


@dataclass
class Rule:
    id: str
    description: str
    severity: str
    remediation: str              # revert | adopt | notify
    auto: bool = False
    resource_types: list[str] = field(default_factory=list)
    attributes: list[str] = field(default_factory=list)
    status: str | None = None
    actual_contains: list[str] = field(default_factory=list)
    actual_equals: object = "__unset__"
    tags_only: bool | None = None

    def matches(self, drift: Drift) -> list[Change] | None:
        """The changes that triggered the rule ([] if it matched the whole
        resource), or None if it doesn't apply."""
        if self.resource_types and not any(fnmatchcase(drift.resource_type, p)
                                           for p in self.resource_types):
            return None
        if self.status and drift.status != self.status:
            return None
        if self.tags_only is not None and drift.only_tags_changed != self.tags_only:
            return None
        changes = drift.changes
        if self.attributes:
            changes = [c for c in changes
                       if any(fnmatchcase(c.path, p) or fnmatchcase(_leaf(c.path), p)
                              for p in self.attributes)]
            if not changes:
                return None
        if self.actual_contains:
            changes = [c for c in changes
                       if any(v in json.dumps(c.actual) for v in self.actual_contains)]
            if not changes:
                return None
        if self.actual_equals != "__unset__":
            changes = [c for c in changes if c.actual == self.actual_equals]
            if not changes:
                return None
        return changes if (self.attributes or self.actual_contains
                           or self.actual_equals != "__unset__") else []


def _leaf(path: str) -> str:
    return path.rsplit(".", 1)[-1].split("[")[0]


@dataclass
class Classified:
    drift: Drift
    rule: Rule
    triggering: list[Change]


@dataclass
class Policy:
    rules: list[Rule]
    default: Rule
    max_auto_reverts_per_run: int = 5
    protected_tag: str = "ops:drift"

    @classmethod
    def load(cls, path: str | Path = DEFAULT_POLICY) -> Policy:
        raw = yaml.safe_load(Path(path).read_text())
        guard = raw.get("guardrails", {})
        return cls(rules=[Rule(**r) for r in raw["rules"]], default=Rule(**raw["default"]),
                   max_auto_reverts_per_run=guard.get("max_auto_reverts_per_run", 5),
                   protected_tag=guard.get("protected_tag", "ops:drift"))

    def classify(self, drift: Drift) -> Classified:
        matched = [(r, ch) for r in self.rules if (ch := r.matches(drift)) is not None]
        if not matched:
            return Classified(drift, self.default, [])
        rule, changes = min(enumerate(matched),
                            key=lambda im: (SEVERITY_ORDER[im[1][0].severity], im[0]))[1]
        return Classified(drift, rule, changes)
