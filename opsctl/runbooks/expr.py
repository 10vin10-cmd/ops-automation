"""A tiny, safe expression language for runbook `when:` conditions.

    service.running < service.desired and not rollback.ran
    service.last_deploy_age_min < 60 and service.previous_task_definition

Only literals, names, attribute access, comparisons, `and`/`or`/`not` and
basic arithmetic are allowed. No calls, subscripts or anything else, so a
runbook file can't execute code.
"""

from __future__ import annotations

import ast
import operator
from typing import Any

_CMP = {ast.Lt: operator.lt, ast.LtE: operator.le, ast.Gt: operator.gt, ast.GtE: operator.ge,
        ast.Eq: operator.eq, ast.NotEq: operator.ne, ast.In: lambda a, b: a in b,
        ast.NotIn: lambda a, b: a not in b}
_BIN = {ast.Add: operator.add, ast.Sub: operator.sub, ast.Mult: operator.mul,
        ast.Div: operator.truediv}


class ExpressionError(ValueError):
    pass


class _Missing:
    """An unknown name or attribute: falsy, and not comparable to anything."""

    def __bool__(self):
        return False

    def __getattr__(self, _):
        return self


MISSING = _Missing()


def evaluate(text: str, names: dict[str, Any]) -> Any:
    try:
        tree = ast.parse(text, mode="eval")
    except SyntaxError as err:
        raise ExpressionError(f"invalid expression {text!r}: {err.msg}") from None
    return _eval(tree.body, names)


def _eval(node, names):
    if isinstance(node, ast.Constant):
        return node.value
    if isinstance(node, ast.Name):
        return names.get(node.id, MISSING)
    if isinstance(node, ast.Attribute):
        base = _eval(node.value, names)
        if isinstance(base, dict):
            return base.get(node.attr, MISSING)
        return MISSING
    if isinstance(node, ast.BoolOp):
        values = (_eval(v, names) for v in node.values)
        return all(values) if isinstance(node.op, ast.And) else any(values)
    if isinstance(node, ast.UnaryOp) and isinstance(node.op, ast.Not):
        return not _eval(node.operand, names)
    if isinstance(node, ast.UnaryOp) and isinstance(node.op, ast.USub):
        return -_eval(node.operand, names)
    if isinstance(node, ast.Compare):
        left = _eval(node.left, names)
        for op, right_node in zip(node.ops, node.comparators, strict=True):
            right = _eval(right_node, names)
            if left is MISSING or right is MISSING:
                return False
            if not _CMP[type(op)](left, right):
                return False
            left = right
        return True
    if isinstance(node, ast.BinOp) and type(node.op) in _BIN:
        return _BIN[type(node.op)](_eval(node.left, names), _eval(node.right, names))
    raise ExpressionError(f"{type(node).__name__} is not allowed in a runbook condition")
