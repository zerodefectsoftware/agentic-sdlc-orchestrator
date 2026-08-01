"""The contracts agents produce and gates read.

These are the schemas in `output_schema:` — a requirement register, a design, a
set of findings. Writing them as Pydantic models rather than hand-written JSON
Schema follows D8: the schema is derived from the model, so the contract an
agent is held to and the contract a predicate reads can never drift apart.

Traceability is what shapes them. Every acceptance criterion carries an id, and
every design element declares which requirements it satisfies, because the two
matrices in §4.2 are the strongest evidence this system produces and neither is
computable from prose.
"""

from __future__ import annotations

from enum import StrEnum

from pydantic import BaseModel, ConfigDict, Field

_STRICT = ConfigDict(extra="forbid")


class Severity(StrEnum):
    LOW = "low"
    MEDIUM = "medium"
    HIGH = "high"

    @property
    def is_high(self) -> bool:
        return self is Severity.HIGH


class Disposition(StrEnum):
    """What was decided about an ambiguity."""

    RESOLVED = "resolved"      # a human answered it
    ASSUMPTION = "assumption"  # recorded and carried forward (D13)


# --------------------------------------------------------------------------- #
# requirements
# --------------------------------------------------------------------------- #


class AcceptanceCriterion(BaseModel):
    model_config = _STRICT

    id: str
    given: str | None = None
    when: str | None = None
    then: str

    @property
    def is_testable(self) -> bool:
        """A criterion with no observable outcome cannot gate anything."""
        return bool(self.then.strip())


class Requirement(BaseModel):
    model_config = _STRICT

    id: str
    statement: str
    acceptance: list[AcceptanceCriterion] = Field(default_factory=list)


class Ambiguity(BaseModel):
    model_config = _STRICT

    id: str
    question: str
    severity: Severity
    rationale: str | None = None
    impact: list[str] = Field(default_factory=list)
    disposition: Disposition | None = None
    answer: str | None = None

    @property
    def is_disposed(self) -> bool:
        return self.disposition is not None


class RequirementRegister(BaseModel):
    """Prose in, structured engineering problem out — the `intake` artifact."""

    model_config = _STRICT

    requirements: list[Requirement] = Field(default_factory=list)
    ambiguities: list[Ambiguity] = Field(default_factory=list)

    @property
    def acceptance_criteria(self) -> list[AcceptanceCriterion]:
        return [ac for requirement in self.requirements for ac in requirement.acceptance]


# --------------------------------------------------------------------------- #
# design
# --------------------------------------------------------------------------- #


class Module(BaseModel):
    """One unit the implementation fans out over (§5.1)."""

    model_config = _STRICT

    name: str
    path: str
    responsibility: str | None = None


class DesignElement(BaseModel):
    model_config = _STRICT

    id: str
    kind: str  # endpoint | model | module | decision
    summary: str | None = None
    satisfies: list[str] = Field(default_factory=list)  # requirement ids


class Design(BaseModel):
    """The `design` artifact: what will be built, and what each part is for."""

    model_config = _STRICT

    elements: list[DesignElement] = Field(default_factory=list)
    modules: list[Module] = Field(default_factory=list)
    endpoints: list[str] = Field(default_factory=list)


# --------------------------------------------------------------------------- #
# tests and findings
# --------------------------------------------------------------------------- #


class AcceptanceTest(BaseModel):
    model_config = _STRICT

    id: str
    covers: list[str] = Field(default_factory=list)  # acceptance criterion ids


class AcceptanceSuite(BaseModel):
    model_config = _STRICT

    tests: list[AcceptanceTest] = Field(default_factory=list)


class Finding(BaseModel):
    model_config = _STRICT

    id: str
    title: str
    severity: Severity
    waived: bool = False
    waived_by: str | None = None  # D15: never an agent
    rationale: str | None = None

    @property
    def is_open_high(self) -> bool:
        return self.severity.is_high and not self.waived


class SecurityReport(BaseModel):
    model_config = _STRICT

    findings: list[Finding] = Field(default_factory=list)


# --------------------------------------------------------------------------- #
# brownfield
# --------------------------------------------------------------------------- #


class ContractDiff(BaseModel):
    """What a change does to the target's published interface.

    `breaking` is separate from the change list because change control asks a
    different question than review does: not "what moved" but "does anyone
    downstream have to care".
    """

    model_config = _STRICT

    breaking: bool = False
    added: list[str] = Field(default_factory=list)
    removed: list[str] = Field(default_factory=list)
    changed: list[str] = Field(default_factory=list)


class ImpactAnalysis(BaseModel):
    """Codebase reasoning (§4.3 of the brief) — what a change actually touches.

    `referenced_symbols` exists to be checked. Hallucinated impact analysis is
    the characteristic brownfield failure: a confident account of modules and
    functions that do not exist, which reads as thorough and is worthless. Naming
    them explicitly makes the claim falsifiable against the repository.
    """

    model_config = _STRICT

    summary: str
    affected_modules: list[Module] = Field(default_factory=list)
    referenced_symbols: list[str] = Field(default_factory=list)  # paths, checked to exist
    contract_diff: ContractDiff = Field(default_factory=ContractDiff)
    invalidated_decisions: list[str] = Field(default_factory=list)  # prior decision ids
    regression_surface: list[str] = Field(default_factory=list)     # test ids at risk
    risk: Severity = Severity.MEDIUM


class Baseline(BaseModel):
    """The state a rollback returns to.

    Recorded before anything is touched. A run that starts from a red baseline
    cannot attribute later failures to its own change, which is why the gate
    refuses rather than proceeding with a caveat.

    `files` holds the bodies, not a reference to them: a rollback that can only
    name the state it wanted is a rollback in the documentation only.
    """

    model_config = _STRICT

    green: bool
    snapshot_ref: str                                    # content hash of `files`
    failing: list[str] = Field(default_factory=list)     # test ids red before the change
    files: dict[str, str] = Field(default_factory=dict)  # path -> body


class Symbol(BaseModel):
    model_config = _STRICT

    name: str
    kind: str          # "def" | "class"
    line: int


class CodeMap(BaseModel):
    """What exists in the target today.

    Derived, not described. An analyst agent reasons over this rather than over
    the filesystem, which is what lets `every_referenced_symbol_exists` check the
    analysis against something that was not written by the thing being checked
    (D4).
    """

    model_config = _STRICT

    root: str
    files: dict[str, list[Symbol]] = Field(default_factory=dict)

    @property
    def symbol_refs(self) -> set[str]:
        """Every addressable name, as `path` and `path::symbol`."""
        refs: set[str] = set()
        for path, symbols in self.files.items():
            refs.add(path)
            refs.update(f"{path}::{symbol.name}" for symbol in symbols)
        return refs


SCHEMAS: dict[str, type[BaseModel]] = {
    "impact_analysis": ImpactAnalysis,
    "requirement_register": RequirementRegister,
    "design": Design,
    "acceptance_suite": AcceptanceSuite,
    "security_report": SecurityReport,
}
