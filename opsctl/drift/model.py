"""Drift in one shape, whichever tool detected it."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any


@dataclass(frozen=True)
class Change:
    path: str          # attribute path, e.g. ingress[0].cidr_blocks[0] or Tags
    expected: Any      # what the code says
    actual: Any        # what is deployed


@dataclass
class Drift:
    source: str                       # terraform | cloudformation
    address: str                      # aws_security_group.web / Stack/LogicalId
    resource_type: str                # aws_security_group / AWS::EC2::SecurityGroup
    status: str                       # modified | deleted
    changes: list[Change] = field(default_factory=list)
    physical_id: str = ""
    tags: dict[str, str] = field(default_factory=dict)

    @property
    def only_tags_changed(self) -> bool:
        return bool(self.changes) and all(
            c.path.split(".")[0].split("[")[0] in ("tags", "tags_all", "Tags") for c in self.changes)


def diff(expected: Any, actual: Any, path: str = "") -> list[Change]:
    """Leaf-level differences between two JSON-like values."""
    if isinstance(expected, dict) and isinstance(actual, dict):
        out: list[Change] = []
        for key in sorted(set(expected) | set(actual)):
            child = f"{path}.{key}" if path else str(key)
            out += diff(expected.get(key), actual.get(key), child)
        return out
    if isinstance(expected, list) and isinstance(actual, list):
        if len(expected) == len(actual):
            out = []
            for i, (e, a) in enumerate(zip(expected, actual, strict=True)):
                out += diff(e, a, f"{path}[{i}]")
            return out
        # Items added or removed (e.g. a new ingress rule): report each one,
        # not the whole before/after list.
        added = [a for a in actual if a not in expected]
        removed = [e for e in expected if e not in actual]
        return ([Change(f"{path}[+]", None, a) for a in added]
                + [Change(f"{path}[-]", e, None) for e in removed])
    return [] if expected == actual else [Change(path, expected, actual)]
