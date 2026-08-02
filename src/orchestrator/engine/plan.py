"""Plan graph data model.

The plan is authored data (D16); this module is its schema. Everything here
mirrors the node contract in docs/architecture.md §4.1 and the six node kinds
in §4.7 — if the two disagree, the doc is wrong and should be corrected.

`extra="forbid"` throughout is deliberate: a typo in a plan file should fail at
load time with a precise message, not silently disable a gate.
"""

from __future__ import annotations

from enum import StrEnum
from typing import Any, Literal

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


class Stage(StrEnum):
    """The SDLC phase a node belongs to.

    Ours, not the brief's — with a documented crosswalk in §11. Two of the
    brief's six do not survive contact with a real graph: it has no slot for
    security work, and it implies an ordering that the test-first inversion (D5)
    deliberately breaks.

    **A stage is a label, not an ordering constraint.** `tests-acceptance` is
    VERIFICATION work that runs before IMPLEMENTATION. Execution order comes from
    `needs` edges; the stage says what kind of work it is.
    """

    REQUIREMENTS = "requirements"
    DESIGN = "design"
    IMPLEMENTATION = "implementation"
    VERIFICATION = "verification"    # testing and security — the brief has no security slot
    DOCUMENTATION = "documentation"
    RELEASE = "release"


class RunScheme(StrEnum):
    """How a `run:` target is executed.

    `run` covers two different execution models, and guessing between them at
    dispatch time is how a shell command that happens to look like a module path
    produces a baffling failure. The scheme is therefore explicit.
    """

    PY = "py"  # an importable callable, e.g. py:orchestrator.evidence.assemble
    SH = "sh"  # a shell command, e.g. sh:{target.commands.test_cov}


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


TerminalAction = Literal["escalate", "safe_stop", "rollback"]


class RepairPolicy(BaseModel):
    """What happens when a gate **fails** — the check ran and did not hold.

    A failing gate means the work is wrong, so a fix node is a reasonable
    response. Bounded, then a terminal control (§6).

    This applies to FAIL only. See `ErrorPolicy` for the other case.
    """

    model_config = _STRICT
    insert: str
    scoped_to: str | None = None
    max_attempts: int = Field(default=2, ge=1)
    then: TerminalAction = "escalate"


class ErrorPolicy(BaseModel):
    """What happens when a gate **errors** — the check could not be performed.

    Deliberately cannot insert a fix node. A fix node is a code change, and an
    ERROR is not a code problem: an unimplemented predicate or a missing fact is
    exactly as missing on the second attempt. Running the repair loop against it
    burns the retry budget, spends model calls, and delays the real signal —
    which is that the *harness* needs attention, not the work.

    `retries` exists for genuinely transient harness failures (a test command
    that crashed before recording an exit code). It defaults to 0, because the
    common causes of ERROR are deterministic.
    """

    model_config = _STRICT
    retries: int = Field(default=0, ge=0)
    then: TerminalAction = "escalate"


class NodeTemplate(BaseModel):
    """The per-item shape a fanout instantiates. A node, minus its identity."""

    model_config = _STRICT
    kind: NodeKind
    role: str | None = None
    inputs: list[str] = Field(default_factory=list)   # a child has no `needs` to inherit from
    params: dict[str, Any] = Field(default_factory=dict)  # turn and time limits per child
    verify: list[str] = Field(default_factory=list)
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
    stage: Stage
    needs: list[str] = Field(default_factory=list)

    # What the node consumes and produces
    inputs: list[str] = Field(default_factory=list)
    outputs: list[str] = Field(default_factory=list)
    output_files: dict[str, str] = Field(default_factory=dict)  # output -> path on disk
    output_schema: str | None = None
    emits: str | None = None

    # Kind-specific configuration
    role: str | None = None                            # agent, codeagent
    run: str | None = None                             # tool
    params: dict[str, Any] = Field(default_factory=dict)   # arguments for a py: task
    from_: str | None = Field(default=None, alias="from")  # derive, fanout
    template: NodeTemplate | None = None               # fanout
    presents: list[str] = Field(default_factory=list)  # human

    # Gates
    verify: list[str] = Field(default_factory=list)     # checks the engine runs, then gates on
    entry_gate: Gate | None = None
    gate: Gate | None = None                           # the exit gate
    on_fail: RepairPolicy | None = None                # gate said no
    on_error: ErrorPolicy | None = None                # gate could not be evaluated

    # Governance
    autonomy: Autonomy | None = None
    escalate_when: GateCheck | None = None  # same vocabulary as a gate check
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

    @field_validator("escalate_when", mode="before")
    @classmethod
    def _normalise_escalation(cls, value: object) -> object:
        """Accept a bare string as an expression, exactly as a gate check does.

        Escalation is a condition over facts, so it uses the gate vocabulary
        rather than a second expression language of its own.
        """
        return {"expression": value} if isinstance(value, str) else value

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

        if self.kind is NodeKind.DERIVE and not (self.from_ or self.run or self.emits):
            raise ValueError(
                f"node '{self.id}' of kind 'derive' requires 'from', 'run', or 'emits' — "
                f"a derivation must say where its output comes from"
            )

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

        schemes = tuple(f"{scheme}:" for scheme in RunScheme)
        if self.run is not None and not self.run.startswith(schemes):
            raise ValueError(
                f"node '{self.id}': run must name its scheme — "
                f"{' or '.join(schemes)} — got {self.run!r}"
            )
        for check in self.verify:
            if not check.startswith(schemes):
                raise ValueError(
                    f"node '{self.id}': verify entry must name its scheme — "
                    f"{' or '.join(schemes)} — got {check!r}"
                )
        return self

    @property
    def is_model_backed(self) -> bool:
        """Only these kinds consume model tokens — see §4.7."""
        return self.kind in (NodeKind.AGENT, NodeKind.CODEAGENT)

    @property
    def run_scheme(self) -> RunScheme | None:
        return RunScheme(self.run.split(":", 1)[0]) if self.run else None

    @property
    def run_target(self) -> str | None:
        """The command or dotted path, with the scheme stripped."""
        return self.run.split(":", 1)[1] if self.run else None


class Defaults(BaseModel):
    """Plan-wide defaults, applied to any node that does not override them."""

    model_config = _STRICT
    model: str | None = None
    effort: Effort = Effort.HIGH
    retry_budget: int = Field(default=2, ge=0)
    autonomy: Autonomy = Autonomy.AUTO


class RollbackPolicy(BaseModel):
    """How a run restores the target after an unrecoverable failure (§6).

    Only meaningful where there is prior state to return to, which is why
    greenfield does not declare one — there is nothing to restore.
    """

    model_config = _STRICT
    restore_from: str          # the node whose snapshot is the baseline
    verify_with: str           # a command that must pass before rollback is believed


class Plan(BaseModel):
    """A plan graph: intent, versioned and authored (§3)."""

    model_config = _STRICT

    name: str = Field(alias="plan")
    version: int
    description: str | None = None
    defaults: Defaults = Field(default_factory=Defaults)
    rollback: RollbackPolicy | None = None
    nodes: list[Node]

    @property
    def node_ids(self) -> list[str]:
        return [node.id for node in self.nodes]

    def node(self, node_id: str) -> Node:
        for node in self.nodes:
            if node.id == node_id:
                return node
        raise KeyError(f"no node '{node_id}' in plan '{self.name}'")

    @property
    def stages_covered(self) -> list[Stage]:
        """Which lifecycle stages this plan actually touches."""
        present = {node.stage for node in self.nodes}
        return [stage for stage in Stage if stage in present]

    @property
    def missing_stages(self) -> list[Stage]:
        """Stages with no node.

        A plan claiming to drive an SDLC that never verifies or never documents
        has a hole; this makes it visible rather than leaving §11 to assert
        lifecycle coverage on the plan's behalf.
        """
        covered = set(self.stages_covered)
        return [stage for stage in Stage if stage not in covered]

    def nodes_in(self, stage: Stage) -> list[Node]:
        return [node for node in self.nodes if node.stage is stage]

    def unsatisfied_params(self) -> list[str]:
        """Every `py:` target a node names whose required params it does not declare.

        A `verify:` probe inherits the node's params, so a node can declare the
        ones its *worker* needs and none of the ones its *checks* need — which
        reports as three ERRORed gate checks after the work has already been
        done and paid for. Checkable before the run starts, so it is.
        """
        problems: list[str] = []
        for node in self.nodes:
            # A fan-out child inherits its checks and params from the template,
            # never from the node — so checking only the node passes a plan whose
            # every child will ERROR after its work is done and paid for.
            checked = [(node.id, node.run, node.verify, node.params)]
            if node.template:
                checked.append(
                    (f"{node.id}.template", None, node.template.verify, node.template.params)
                )

            for label, run, verify, params in checked:
                problems.extend(_missing_params(label, [run, *verify], params))
        return problems

    @property
    def required_predicates(self) -> list[str]:
        """Every predicate this plan names, from gates, escalations, and templates.

        A run should refuse to start when the engine cannot supply one of these,
        rather than discovering it at the gate and having to decide what an
        unrunnable check means.
        """
        names: set[str] = set()

        def collect(check: GateCheck | None) -> None:
            if isinstance(check, PredicateCheck):
                names.add(check.predicate)

        for node in self.nodes:
            collect(node.escalate_when)
            gates = [node.gate, node.entry_gate]
            if node.template:
                gates.append(node.template.gate)
            for gate in gates:
                if gate is not None:
                    for check in gate.checks:
                        collect(check)

        return sorted(names)


def _missing_params(label: str, targets: list[str | None], params: dict) -> list[str]:
    """Which `py:` targets among `targets` need params `params` does not have."""
    from orchestrator.workers.pytask import resolve

    problems: list[str] = []
    for target in targets:
        if not target or not target.startswith("py:"):
            continue
        try:
            required = getattr(resolve(target.split(":", 1)[1]), "required_params", ())
        except Exception:  # noqa: BLE001 — unresolvable is reported elsewhere
            continue
        missing = [name for name in required if name not in params]
        if missing:
            problems.append(
                f"{label}: {target} needs {', '.join(missing)} — "
                f"declared {sorted(params) or '(none)'}"
            )
    return problems
