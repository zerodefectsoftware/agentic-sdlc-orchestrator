"""Facts — the evidence a gate is evaluated against.

A fact is a recorded observation with a **provenance**, and provenance is what
makes D4 enforceable rather than aspirational. "The producer never evaluates its
own exit gate" is not really about who calls the evaluator — the engine always
does. It is about whether the evidence is the producer's own claim.

So an agent's self-report is inadmissible as gate evidence. What *is* admissible
is the result of running something over that output: `schema_valid` is a
VALIDATOR fact derived from the agent's artifact, not the agent's opinion of it.
The artifact is the subject of the check, never its author.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum
from typing import Any


class FactSource(StrEnum):
    """Where an observation came from, and therefore whether it can be trusted."""

    TOOL = "tool"            # a command's exit code or parsed output
    VALIDATOR = "validator"  # a check run over an artifact (schema, matrix, scan)
    DERIVED = "derived"      # computed deterministically by the engine
    HUMAN = "human"          # a recorded human decision
    AGENT = "agent"          # a model's own assertion — INADMISSIBLE as gate evidence

    @property
    def is_admissible(self) -> bool:
        return self is not FactSource.AGENT


@dataclass(frozen=True, slots=True)
class Fact:
    """One observation, with where it came from and what produced it."""

    value: Any
    source: FactSource
    produced_by: str | None = None  # node id, command, or predicate name

    def __str__(self) -> str:
        origin = f" from {self.produced_by}" if self.produced_by else ""
        return f"{self.value!r} ({self.source}{origin})"


# Facts are keyed by the dotted path an expression names: "pytest.exit_code".
FactSet = dict[str, Fact]


def tool_facts(command: str, **values: Any) -> FactSet:
    """Convenience for recording a command's observations.

    ``tool_facts("pytest", **{"pytest.exit_code": 0})``
    """
    return {
        key: Fact(value, FactSource.TOOL, produced_by=command) for key, value in values.items()
    }
