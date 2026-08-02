"""The predicate registry.

Where an expression would be a lie about the complexity — traceability matrices,
stale-approval detection, lineage completeness — the plan names a predicate and
the engine supplies it. Predicates are ordinary Python, so they are tested like
ordinary code rather than debugged through a config file.

A predicate returns `(passed, detail)`: the verdict plus a human-readable reason.
The detail is what a reviewer reads in the evidence bundle, so "3 acceptance
criteria have no test: AC1.2, AC3.1, AC4.4" is worth far more than `False`.
"""

from __future__ import annotations

from collections.abc import Callable, Iterable
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any

from orchestrator.gates.facts import FactSet

if TYPE_CHECKING:  # imports for types only — gates has no runtime dependency on state
    from sqlalchemy.orm import Session

    from orchestrator.engine.plan import Node
    from orchestrator.state.artifacts import ArtifactStore
    from orchestrator.state.models import Run


@dataclass(slots=True)
class PredicateContext:
    """What a predicate is allowed to look at.

    Facts alone are not enough. `no_stale_approvals` has to compare approvals
    against artifact versions, and `ac_test_matrix_complete` has to read the
    requirement register — neither is expressible as a fact a tool emitted.

    Everything beyond `facts` is optional, so a predicate that only needs facts
    stays testable with one line of setup, and a predicate that needs the run
    says so by failing clearly when it is absent.
    """

    facts: FactSet = field(default_factory=dict)
    session: Session | None = None
    run: Run | None = None
    artifacts: ArtifactStore | None = None
    node: Node | None = None
    plan: Any | None = None       # the graph, for predicates that ask about shape

    def require(self, *names: str) -> Any:
        """Fetch context a predicate depends on, or explain what is missing.

        A predicate given no run should report that as an ERROR about the
        harness, not quietly evaluate to False and look like a finding.
        """
        missing = [name for name in names if getattr(self, name, None) is None]
        if missing:
            raise LookupError(
                f"predicate needs {', '.join(missing)} but the gate was evaluated "
                f"without it — this is a harness problem, not a failed check"
            )
        values = tuple(getattr(self, name) for name in names)
        return values[0] if len(values) == 1 else values


# (context) -> (passed, detail)
PredicateFn = Callable[[PredicateContext], tuple[bool, str]]


class UnknownPredicate(KeyError):
    """The plan names a predicate the engine does not supply.

    Never silently treated as a pass: an unimplemented check that reports green
    is the most dangerous state a governance system can be in.
    """


@dataclass(frozen=True, slots=True)
class Predicate:
    name: str
    fn: PredicateFn
    description: str


class PredicateRegistry:
    def __init__(self) -> None:
        self._predicates: dict[str, Predicate] = {}

    def register(self, name: str, description: str) -> Callable[[PredicateFn], PredicateFn]:
        """Decorator form: ``@registry.register("schema_valid", "...")``."""

        def decorate(fn: PredicateFn) -> PredicateFn:
            if name in self._predicates:
                raise ValueError(f"predicate '{name}' is already registered")
            self._predicates[name] = Predicate(name=name, fn=fn, description=description)
            return fn

        return decorate

    def get(self, name: str) -> Predicate:
        try:
            return self._predicates[name]
        except KeyError as exc:
            raise UnknownPredicate(
                f"no predicate registered as '{name}'; known: "
                f"{', '.join(sorted(self._predicates)) or '(none)'}"
            ) from exc

    def __contains__(self, name: object) -> bool:
        return name in self._predicates

    @property
    def names(self) -> list[str]:
        return sorted(self._predicates)

    def missing(self, required: Iterable[str]) -> list[str]:
        """Which of `required` are not registered.

        Intended as a preflight check: a run should refuse to start when its plan
        names checks the engine cannot perform, rather than discovering that at
        the gate and having to decide what an unrunnable check means.
        """
        return sorted({name for name in required if name not in self._predicates})


registry = PredicateRegistry()
