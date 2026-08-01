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
from dataclasses import dataclass

from orchestrator.gates.facts import FactSet

# (facts) -> (passed, detail)
PredicateFn = Callable[[FactSet], tuple[bool, str]]


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
