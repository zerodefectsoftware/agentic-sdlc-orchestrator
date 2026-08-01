"""Plan graph data model.

The plan is authored data (D16); this module is its schema. Everything here
mirrors the node contract in docs/architecture.md §4.1 and the six node kinds
in §4.7 — if the two disagree, the doc is wrong and should be corrected.

`extra="forbid"` throughout is deliberate: a typo in a plan file should fail at
load time with a precise message, not silently disable a gate.
"""

from __future__ import annotations

from enum import StrEnum
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

_STRICT = ConfigDict(extra="forbid", populate_by_name=True, protected_namespaces=())


class NodeKind(StrEnum):
    """The fixed vocabulary the engine implements. Plans compose these."""

    AGENT = "agent"          # model call, schema-constrained artifact, no filesystem
    CODEAGENT = "codeagent"  # coding-agent session, write-scoped to declared paths
    TOOL = "tool"            # deterministic command; exit code and output are the result
    DERIVE = "derive"        # deterministic generation from a contract
    HUMAN = "human"          # approval or clarification checkpoint
    FANOUT = "fanout"        # materialises N children from an upstream artifact


class Autonomy(StrEnum):
    AUTO = "AUTO"            # proceeds; result gated and logged
    REVIEW = "REVIEW"        # proceeds; flagged for attention, non-blocking
    APPROVE = "APPROVE"      # blocks until a human authorises
    FORBIDDEN = "FORBIDDEN"  # denied; escalates


class Effort(StrEnum):
    LOW = "low"
    MEDIUM = "medium"
    HIGH = "high"


class ExpressionCheck(BaseModel):
    """A gate check written as an expression over tool results.

    Example: ``pytest.exit_code == 0``
    """

    model_config = _STRICT
    expression: str

    def __str__(self) -> str:
        return self.expression


class PredicateCheck(BaseModel):
    """A gate check delegated to a predicate the engine registers.

    Used where an expression would be a lie about the complexity — traceability
    matrices, stale-approval detection, lineage completeness (§4.7).
    """

    model_config = _STRICT
    predicate: str

    def __str__(self) -> str:
        return f"{self.predicate}()"


GateCheck = ExpressionCheck | PredicateCheck


class Gate(BaseModel):
    """A predicate over artifacts, evaluated by something other than the producer (D4)."""

    model_config = _STRICT
    all_checks: list[GateCheck] = Field(default_factory=list, alias="all")
    any_checks: list[GateCheck] = Field(default_factory=list, alias="any")

    @field_validator("all_checks", "any_checks", mode="before")
    @classmethod
    def _normalise_checks(cls, value: object) -> object:
        """Accept a bare string as shorthand for an expression check."""
        if not isinstance(value, list):
            return value
        return [{"expression": item} if isinstance(item, str) else item for item in value]

    @model_validator(mode="after")
    def _must_check_something(self) -> Gate:
        if not self.all_checks and not self.any_checks:
            raise ValueError("gate declares neither 'all' nor 'any' checks")
        return self

    @property
    def checks(self) -> list[GateCheck]:
        return [*self.all_checks, *self.any_checks]


class RepairPolicy(BaseModel):
    """What happens when a gate fails: bounded retry, then a terminal control (§6)."""

    model_config = _STRICT
    insert: str
    scoped_to: str | None = None
    max_attempts: int = Field(default=2, ge=1)
    then: Literal["escalate", "safe_stop", "rollback"] = "escalate"


class NodeTemplate(BaseModel):
    """The per-item shape a fanout instantiates. A node, minus its identity."""

    model_config = _STRICT
    kind: NodeKind
    role: str | None = None
    write_scope: list[str] = Field(default_factory=list)
    freeze_paths: list[str] = Field(default_factory=list)
    gate: Gate | None = None
    model: str | None = None
    effort: Effort | None = None
    retry_budget: int | None = None
    autonomy: Autonomy | None = None


class Node(BaseModel):
    """One unit of work in the plan graph (§4.1)."""

    model_config = _STRICT

    id: str
    kind: NodeKind
    needs: list[str] = Field(default_factory=list)

    # What the node consumes and produces
    inputs: list[str] = Field(default_factory=list)
    outputs: list[str] = Field(default_factory=list)
    output_schema: str | None = None
    emits: str | None = None

    # Kind-specific configuration
    role: str | None = None                            # agent, codeagent
    run: str | None = None                             # tool
    from_: str | None = Field(default=None, alias="from")  # derive, fanout
    template: NodeTemplate | None = None               # fanout
    presents: list[str] = Field(default_factory=list)  # human

    # Gates
    entry_gate: Gate | None = None
    gate: Gate | None = None                           # the exit gate
    on_fail: RepairPolicy | None = None

    # Governance
    autonomy: Autonomy | None = None
    escalate_when: str | None = None
    on_escalate: str | None = None
    binds_to: list[str] = Field(default_factory=list)  # D10: version-bound approval
    may_waive: bool = True                             # D15
    optional: bool = False                             # instantiated only when triggered

    # Blast radius
    write_scope: list[str] = Field(default_factory=list)   # D7
    freeze_paths: list[str] = Field(default_factory=list)  # D6

    # Cost and depth
    model: str | None = None
    effort: Effort | None = None
    retry_budget: int | None = None

    @model_validator(mode="after")
    def _check_kind_requirements(self) -> Node:
        """Each kind needs different configuration to be executable."""
        requirements: dict[NodeKind, list[tuple[str, object]]] = {
            NodeKind.AGENT: [("role", self.role)],
            NodeKind.CODEAGENT: [("role", self.role), ("write_scope", self.write_scope)],
            NodeKind.TOOL: [("run", self.run)],
            NodeKind.FANOUT: [("from", self.from_), ("template", self.template)],
        }
        for field, value in requirements.get(self.kind, []):
            if not value:
                raise ValueError(f"node '{self.id}' of kind '{self.kind}' requires '{field}'")

        if self.kind is NodeKind.DERIVE and not (self.from_ or self.run):
            raise ValueError(f"node '{self.id}' of kind 'derive' requires 'from' or 'run'")

        if self.kind is NodeKind.HUMAN and self.autonomy not in (None, Autonomy.APPROVE):
            raise ValueError(
                f"node '{self.id}' is a human checkpoint but declares autonomy "
                f"'{self.autonomy}'; only APPROVE is meaningful"
            )

        # An escalation target is only reachable if something escalates to it.
        if self.on_escalate and not self.escalate_when:
            raise ValueError(
                f"node '{self.id}' declares 'on_escalate' without 'escalate_when'"
            )
        return self

    @property
    def is_model_backed(self) -> bool:
        """Only these kinds consume model tokens — see §4.7."""
        return self.kind in (NodeKind.AGENT, NodeKind.CODEAGENT)


class Defaults(BaseModel):
    """Plan-wide defaults, applied to any node that does not override them."""

    model_config = _STRICT
    model: str | None = None
    effort: Effort = Effort.HIGH
    retry_budget: int = Field(default=2, ge=0)
    autonomy: Autonomy = Autonomy.AUTO


class Plan(BaseModel):
    """A plan graph: intent, versioned and authored (§3)."""

    model_config = _STRICT

    name: str = Field(alias="plan")
    version: int
    description: str | None = None
    defaults: Defaults = Field(default_factory=Defaults)
    nodes: list[Node]

    @property
    def node_ids(self) -> list[str]:
        return [node.id for node in self.nodes]

    def node(self, node_id: str) -> Node:
        for node in self.nodes:
            if node.id == node_id:
                return node
        raise KeyError(f"no node '{node_id}' in plan '{self.name}'")
