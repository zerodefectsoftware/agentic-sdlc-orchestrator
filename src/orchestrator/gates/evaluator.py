"""Gate evaluation.

A gate is a predicate over recorded facts, evaluated by something other than the
producer (D4). This module is that evaluator.

Three verdicts, not two. **ERROR is not FAIL**, and neither is PASS:

    PASS   the check was performed and held
    FAIL   the check was performed and did not hold
    ERROR  the check could not be performed

Collapsing ERROR into either of the others is the failure mode this design
exists to prevent. Fold it into PASS and a missing fact or an unimplemented
predicate reports green — governance that is decorative. Fold it into FAIL and a
broken evaluator is indistinguishable from broken code, so the repair loop
retries an implementation problem forever. Both block the run; only ERROR says
*investigate the harness, not the work*.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import UTC, datetime
from enum import StrEnum

from orchestrator.engine.plan import ExpressionCheck, Gate, GateCheck, PredicateCheck
from orchestrator.gates import expressions
from orchestrator.gates.facts import FactSet
from orchestrator.gates.registry import PredicateRegistry, UnknownPredicate
from orchestrator.gates.registry import registry as default_registry


class Verdict(StrEnum):
    PASS = "pass"
    FAIL = "fail"
    ERROR = "error"

    @property
    def blocks(self) -> bool:
        """Anything that is not a pass stops the run."""
        return self is not Verdict.PASS


@dataclass(frozen=True, slots=True)
class CheckResult:
    """One check's outcome, with enough context to be read in an evidence bundle."""

    check: str
    verdict: Verdict
    detail: str
    observed: str | None = None

    def __str__(self) -> str:
        observed = f" (observed {self.observed})" if self.observed is not None else ""
        return f"{self.check}: {self.verdict.upper()}{observed} — {self.detail}"


@dataclass(frozen=True, slots=True)
class GateResult:
    """The recorded outcome of a gate.

    Carries the evaluator's identity and the time, because §5.4 requires the
    evidence bundle to show *which evaluator* reached *what verdict* and *when*.
    """

    verdict: Verdict
    checks: list[CheckResult] = field(default_factory=list)
    evaluator: str = "orchestrator.gates"
    evaluated_at: datetime = field(default_factory=lambda: datetime.now(UTC))

    @property
    def passed(self) -> bool:
        return self.verdict is Verdict.PASS

    @property
    def failures(self) -> list[CheckResult]:
        return [check for check in self.checks if check.verdict.blocks]

    def summary(self) -> str:
        if self.passed:
            return f"PASS ({len(self.checks)} checks)"
        reasons = "; ".join(str(check) for check in self.failures)
        return f"{self.verdict.upper()}: {reasons}"


def evaluate_gate(
    gate: Gate,
    facts: FactSet,
    *,
    registry: PredicateRegistry | None = None,
    evaluator: str = "orchestrator.gates",
) -> GateResult:
    """Evaluate every check in `gate` against `facts`.

    `all` checks must each pass. `any` checks need one passing member. When both
    are present the gate requires both conditions.
    """
    registry = registry if registry is not None else default_registry

    all_results = [_evaluate_check(check, facts, registry) for check in gate.all_checks]
    any_results = [_evaluate_check(check, facts, registry) for check in gate.any_checks]

    verdict = _combine(all_results, any_results)
    return GateResult(verdict=verdict, checks=[*all_results, *any_results], evaluator=evaluator)


def required_predicates(gate: Gate) -> list[str]:
    """Predicate names a gate depends on — for preflight checks before a run starts."""
    return [check.predicate for check in gate.checks if isinstance(check, PredicateCheck)]


# --------------------------------------------------------------------------- #
# internals
# --------------------------------------------------------------------------- #


def _combine(all_results: list[CheckResult], any_results: list[CheckResult]) -> Verdict:
    """ERROR outranks FAIL when reporting: an unknown needs a different response."""
    verdicts: list[Verdict] = []

    if all_results:
        verdicts.append(_worst(result.verdict for result in all_results))

    if any_results:
        if any(result.verdict is Verdict.PASS for result in any_results):
            verdicts.append(Verdict.PASS)
        elif any(result.verdict is Verdict.ERROR for result in any_results):
            verdicts.append(Verdict.ERROR)
        else:
            verdicts.append(Verdict.FAIL)

    return _worst(verdicts) if verdicts else Verdict.PASS


def _worst(verdicts) -> Verdict:
    seen = list(verdicts)
    if Verdict.ERROR in seen:
        return Verdict.ERROR
    if Verdict.FAIL in seen:
        return Verdict.FAIL
    return Verdict.PASS


def _evaluate_check(check: GateCheck, facts: FactSet, registry: PredicateRegistry) -> CheckResult:
    if isinstance(check, ExpressionCheck):
        return _evaluate_expression(check, facts)
    return _evaluate_predicate(check, facts, registry)


def _evaluate_expression(check: ExpressionCheck, facts: FactSet) -> CheckResult:
    rendered = check.expression

    try:
        path = expressions.fact_path(rendered)
    except expressions.ExpressionError as exc:
        return CheckResult(rendered, Verdict.ERROR, str(exc))

    fact = facts.get(path)
    if fact is None:
        return CheckResult(
            rendered,
            Verdict.ERROR,
            f"no fact recorded for '{path}' — the check could not be performed",
        )

    # D4: a producer's own claim is not evidence about itself.
    if not fact.source.is_admissible:
        return CheckResult(
            rendered,
            Verdict.ERROR,
            f"'{path}' is an agent self-report and is inadmissible as gate evidence; "
            f"gate on the result of a tool or validator run over the artifact instead",
            observed=str(fact),
        )

    try:
        held = expressions.evaluate(rendered, fact.value)
    except expressions.ExpressionError as exc:
        return CheckResult(rendered, Verdict.ERROR, str(exc), observed=str(fact))

    return CheckResult(
        rendered,
        Verdict.PASS if held else Verdict.FAIL,
        "holds" if held else "does not hold",
        observed=str(fact),
    )


def _evaluate_predicate(
    check: PredicateCheck, facts: FactSet, registry: PredicateRegistry
) -> CheckResult:
    rendered = f"{check.predicate}()"

    try:
        predicate = registry.get(check.predicate)
    except UnknownPredicate as exc:
        # Never a pass. An unimplemented check reporting green is the most
        # dangerous state this system can reach.
        return CheckResult(rendered, Verdict.ERROR, str(exc))

    try:
        held, detail = predicate.fn(facts)
    except Exception as exc:  # noqa: BLE001 — a broken predicate is an ERROR, not a FAIL
        return CheckResult(
            rendered,
            Verdict.ERROR,
            f"predicate raised {type(exc).__name__}: {exc}",
        )

    return CheckResult(rendered, Verdict.PASS if held else Verdict.FAIL, detail)
