"""Gate evaluation: the expression language and the registered predicates.

A gate is a predicate over recorded facts, evaluated by something other than the
producer (D4). Two forms, one purpose:

- **expressions** — `pytest.exit_code == 0`, for facts a tool observed
- **predicates**  — `no_stale_approvals`, where an expression would be a lie
  about the complexity

Facts carry provenance, and an agent's self-report is inadmissible: gates check
the *result of running something over* an artifact, never the artifact author's
opinion of it.
"""

from orchestrator.gates.checks import imports_resolve, report_coverage
from orchestrator.gates.evaluator import (
    CheckResult,
    GateResult,
    Verdict,
    evaluate_gate,
    required_predicates,
)
from orchestrator.gates.facts import Fact, FactSet, FactSource, tool_facts
from orchestrator.gates.registry import PredicateRegistry, UnknownPredicate, registry
from orchestrator.gates.security import security_scan

__all__ = [
    "CheckResult",
    "Fact",
    "FactSet",
    "FactSource",
    "GateResult",
    "PredicateRegistry",
    "UnknownPredicate",
    "Verdict",
    "evaluate_gate",
    "imports_resolve",
    "report_coverage",
    "registry",
    "security_scan",
    "required_predicates",
    "tool_facts",
]
