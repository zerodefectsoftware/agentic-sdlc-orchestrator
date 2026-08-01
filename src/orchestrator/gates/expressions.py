"""The gate expression language.

Deliberately tiny: one fact path, one comparison operator, one literal. That is
enough for every expression the plans use, and stopping here is a decision
rather than an accident (§4.7) — anything more expressive belongs in a
registered predicate, where it can be tested like ordinary code.

It is emphatically **not** `eval()`. A governance layer that executes arbitrary
strings from a config file has no business calling itself a control.

    pytest.exit_code == 0
    pytest.exit_code != 0
    coverage.percent >= 80
    openapi.valid == true
"""

from __future__ import annotations

import operator
import re
from collections.abc import Callable
from typing import Any

_GRAMMAR = re.compile(
    r"""
    ^\s*
    (?P<path>[A-Za-z_][\w.]*)          # fact path, e.g. pytest.exit_code
    \s*
    (?P<op><=|>=|==|!=|<|>)            # comparison
    \s*
    (?P<literal>.+?)
    \s*$
    """,
    re.VERBOSE,
)

_OPERATORS: dict[str, Callable[[Any, Any], bool]] = {
    "==": operator.eq,
    "!=": operator.ne,
    ">=": operator.ge,
    "<=": operator.le,
    ">": operator.gt,
    "<": operator.lt,
}

_ORDERING = {">=", "<=", ">", "<"}


class ExpressionError(Exception):
    """The expression is not valid, or cannot be applied to the value observed."""


def parse(expression: str) -> tuple[str, str, Any]:
    """Return (fact_path, operator, literal). Raises ExpressionError if malformed."""
    match = _GRAMMAR.match(expression)
    if not match:
        raise ExpressionError(
            f"cannot parse {expression!r} — expected '<fact.path> <op> <literal>' "
            f"with op one of {', '.join(sorted(_OPERATORS))}"
        )
    return match["path"], match["op"], _literal(match["literal"], expression)


def evaluate(expression: str, value: Any) -> bool:
    """Apply the expression's comparison to an observed value."""
    _, op, literal = parse(expression)

    if op in _ORDERING and not _is_ordered(value, literal):
        raise ExpressionError(
            f"cannot compare {value!r} to {literal!r} with '{op}' — "
            f"ordering requires numbers, got {type(value).__name__}"
        )
    return bool(_OPERATORS[op](value, literal))


def fact_path(expression: str) -> str:
    """The fact an expression depends on — used to report what was missing."""
    return parse(expression)[0]


def _literal(raw: str, expression: str) -> Any:
    lowered = raw.lower()
    if lowered == "true":
        return True
    if lowered == "false":
        return False
    if lowered in ("null", "none"):
        return None
    if len(raw) >= 2 and raw[0] == raw[-1] and raw[0] in "\"'":
        return raw[1:-1]
    try:
        return int(raw)
    except ValueError:
        pass
    try:
        return float(raw)
    except ValueError as exc:
        raise ExpressionError(
            f"cannot read {raw!r} in {expression!r} as a literal — "
            f"expected a number, a quoted string, true, false, or null"
        ) from exc


def _is_ordered(value: Any, literal: Any) -> bool:
    """Ordering comparisons only make sense between numbers.

    bool is excluded on purpose: `flag > 0` reads as a mistake, not intent.
    """
    return all(
        isinstance(item, (int, float)) and not isinstance(item, bool) for item in (value, literal)
    )
