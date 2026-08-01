"""The controls that only exist because prior state does.

Greenfield cannot regress, has nothing to roll back to, and reasons about code
it wrote itself. Every test here is about one of those three assumptions failing.

The sharpest of them is the regression comparison. `pytest.exit_code == 0` looks
like a regression gate and is not one: against a suite that arrived red it blocks
every change forever, and against one that arrived green it accepts a change that
broke something the run then "fixed" by deleting the test.
"""

from __future__ import annotations

import json

import pytest

from orchestrator.artifacts import (
    Ambiguity,
    Baseline,
    CodeMap,
    ContractDiff,
    ImpactAnalysis,
    Module,
    RequirementRegister,
    Severity,
    Symbol,
)
from orchestrator.derive.codemap import map_codebase, symbols_in
from orchestrator.engine.plan import Node
from orchestrator.gates.facts import Fact, FactSource
from orchestrator.gates.predicates import register_all
from orchestrator.gates.registry import PredicateContext, PredicateRegistry
from orchestrator.lineage import recorder
from orchestrator.policy.baseline import capture_baseline, verify_no_regression
from orchestrator.policy.clarify import normalize_clarification, parse_answers
from orchestrator.policy.triage import triage_ambiguities
from orchestrator.state import store
from orchestrator.state.artifacts import ArtifactStore
from orchestrator.workers import WorkerError, WorkScope
from orchestrator.workers.pytask import Task

SCOPE = WorkScope(allowed=("target/**",))


def task(run: str, *, params=None, inputs=None, cwd=None) -> Task:
    node = Node.model_validate(
        {
            "id": "n",
            "kind": "tool",
            "stage": "verification",
            "run": f"py:{run}",
            "params": params or {},
        }
    )
    return Task(node=node, inputs=inputs or {}, scope=SCOPE, cwd=cwd or ".")


# A stand-in for the target's test command: prints pytest-shaped failure lines
# and exits accordingly, so the parsing is exercised without a real suite.
def fake_suite(tmp_path, *failing: str) -> str:
    script = tmp_path / "suite.py"
    lines = "\n".join(f"FAILED {name} - AssertionError" for name in failing)
    script.write_text(f"import sys\nprint({lines!r})\nsys.exit({1 if failing else 0})\n")
    return f"python3 {script}"


# --------------------------------------------------------------------------- #
# the baseline
# --------------------------------------------------------------------------- #


def test_the_baseline_records_the_bodies_it_would_restore(tmp_path):
    """Not a reference to them: a rollback that can only name the state it
    wanted is a rollback in the documentation only."""
    (tmp_path / "target").mkdir()
    (tmp_path / "target" / "main.py").write_text("x = 1\n")

    output = capture_baseline(
        task("...", params={"command": fake_suite(tmp_path), "root": "target"}, cwd=tmp_path)
    )
    baseline = Baseline.model_validate_json(output.artifacts["baseline.snapshot"])

    assert baseline.green
    assert baseline.files == {"target/main.py": "x = 1\n"}
    assert baseline.snapshot_ref


def test_a_red_baseline_is_recorded_as_red(tmp_path):
    (tmp_path / "target").mkdir()
    output = capture_baseline(
        task(
            "...",
            params={"command": fake_suite(tmp_path, "t/test_a.py::test_x"), "root": "target"},
            cwd=tmp_path,
        )
    )
    baseline = Baseline.model_validate_json(output.artifacts["baseline.snapshot"])

    assert not baseline.green
    assert baseline.failing == ["t/test_a.py::test_x"]


def test_identical_trees_produce_the_same_snapshot_ref(tmp_path):
    def capture(body: str) -> str:
        root = tmp_path / body[:1]
        (root / "target").mkdir(parents=True)
        (root / "target" / "main.py").write_text(body)
        output = capture_baseline(
            task("...", params={"command": fake_suite(tmp_path), "root": "target"}, cwd=root)
        )
        return Baseline.model_validate_json(output.artifacts["baseline.snapshot"]).snapshot_ref

    assert capture("a = 1\n") != capture("b = 1\n")


def test_a_test_command_that_cannot_run_is_an_error_not_a_pass(tmp_path):
    """A suite that never ran says nothing about whether anything regressed."""
    with pytest.raises(WorkerError, match="could not run"):
        capture_baseline(
            task(
                "...",
                params={"command": "definitely-not-a-command", "root": "target"},
                cwd=tmp_path,
            )
        )


def test_a_missing_param_names_the_node_that_should_declare_it(tmp_path):
    with pytest.raises(WorkerError, match="needs param 'command'"):
        capture_baseline(task("...", params={"root": "target"}, cwd=tmp_path))


# --------------------------------------------------------------------------- #
# the regression comparison
# --------------------------------------------------------------------------- #


def compare(tmp_path, *, before: list[str], after: list[str]):
    baseline = Baseline(green=not before, snapshot_ref="ref", failing=before)
    return verify_no_regression(
        task(
            "...",
            params={"command": fake_suite(tmp_path, *after)},
            inputs={"baseline.snapshot": baseline.model_dump_json()},
            cwd=tmp_path,
        )
    )


def test_a_failure_that_was_already_there_is_not_a_regression(tmp_path):
    output = compare(tmp_path, before=["t::test_old"], after=["t::test_old"])

    assert output.facts["regression.new_failures"] == 0
    assert output.facts["regression.inherited"] == 1


def test_a_newly_red_test_is_a_regression(tmp_path):
    output = compare(tmp_path, before=["t::test_old"], after=["t::test_old", "t::test_new"])

    assert output.facts["regression.new_failures"] == 1
    assert output.facts["regression.ids"] == ["t::test_new"]


def test_fixing_an_inherited_failure_is_recorded_not_penalised(tmp_path):
    output = compare(tmp_path, before=["t::test_old"], after=[])

    assert output.facts["regression.new_failures"] == 0
    assert output.facts["regression.fixed"] == 1
    assert output.facts["tests.exit_code"] == 0


# --------------------------------------------------------------------------- #
# the code map
# --------------------------------------------------------------------------- #


def test_symbols_are_parsed_not_guessed():
    found = symbols_in(
        "def record(x):\n    pass\n\nclass Store:\n    def put(self):\n        pass\n"
    )
    assert [(s.name, s.kind) for s in found] == [
        ("record", "def"),
        ("Store", "class"),
        ("Store.put", "def"),
    ]


def test_a_file_that_does_not_parse_is_recorded_not_raised(tmp_path):
    """A target that does not parse is a finding about the target."""
    (tmp_path / "target").mkdir()
    (tmp_path / "target" / "broken.py").write_text("def (:\n")

    output = map_codebase(task("...", params={"root": "target"}, cwd=tmp_path))

    assert output.facts["codemap.unparsable"] == 1
    assert output.facts["codemap.files"] == 1


def test_the_map_is_addressable_the_way_an_analysis_refers_to_code():
    code_map = CodeMap(
        root="target/shortener",
        files={"target/shortener/a.py": [Symbol(name="f", kind="def", line=1)]},
    )
    assert code_map.symbol_refs == {"target/shortener/a.py", "target/shortener/a.py::f"}


# --------------------------------------------------------------------------- #
# the predicates
# --------------------------------------------------------------------------- #


@pytest.fixture
def registry() -> PredicateRegistry:
    return register_all(PredicateRegistry())


@pytest.fixture
def session():
    with store.Store.in_memory().session() as session:
        yield session


@pytest.fixture
def run(session):
    return store.start_run(
        session,
        plan_name="brownfield",
        plan_version=1,
        requirement_path="requirements/brownfield.md",
        target_profile="config/target.shortener.yaml",
        nodes=[("impact-analysis", "agent", "design"), ("tests", "tool", "verification")],
    )


@pytest.fixture
def artifacts(tmp_path) -> ArtifactStore:
    return ArtifactStore(tmp_path)


def record(session, run, artifacts, name: str, model) -> None:
    body = model.model_dump_json() if hasattr(model, "model_dump_json") else json.dumps(model)
    artifact = recorder.record_artifact(session, run, name=name, content=body)
    artifact.path = str(artifacts.write(run.id, name, artifact.version, body))
    session.flush()


def check(registry, name, ctx):
    return registry.get(name).fn(ctx)


ANALYSIS = ImpactAnalysis(
    summary="301 responses are browser-cached, so repeat visits never reach the service",
    affected_modules=[Module(name="analytics", path="analytics")],
    referenced_symbols=["target/shortener/analytics.py::record_click"],
)

MAP = CodeMap(
    root="target/shortener",
    files={"target/shortener/analytics.py": [Symbol(name="record_click", kind="def", line=4)]},
)


def test_a_red_baseline_stops_the_run(registry, session, run, artifacts):
    record(
        session,
        run,
        artifacts,
        "baseline.snapshot",
        Baseline(green=False, snapshot_ref="r", failing=["t::a"]),
    )

    passed, detail = check(
        registry,
        "baseline_is_green",
        PredicateContext(session=session, run=run, artifacts=artifacts),
    )
    assert not passed
    assert "already failing" in detail


def test_an_analysis_referencing_code_that_exists_passes(registry, session, run, artifacts):
    record(session, run, artifacts, "impact-analysis.report", ANALYSIS)
    record(session, run, artifacts, "codebase.map", MAP)

    passed, _ = check(
        registry,
        "every_referenced_symbol_exists",
        PredicateContext(session=session, run=run, artifacts=artifacts),
    )
    assert passed


def test_a_hallucinated_symbol_fails_the_gate(registry, session, run, artifacts):
    """The characteristic brownfield failure: fluent analysis of code that is
    not there. It reads as thorough right up until someone checks."""
    invented = ANALYSIS.model_copy(
        update={"referenced_symbols": ["target/shortener/analytics.py::flush_counters"]}
    )
    record(session, run, artifacts, "impact-analysis.report", invented)
    record(session, run, artifacts, "codebase.map", MAP)

    passed, detail = check(
        registry,
        "every_referenced_symbol_exists",
        PredicateContext(session=session, run=run, artifacts=artifacts),
    )
    assert not passed
    assert "flush_counters" in detail


def test_a_breaking_contract_change_escalates(registry, session, run, artifacts):
    breaking = ANALYSIS.model_copy(
        update={"contract_diff": ContractDiff(breaking=True, removed=["GET /links/{code}"])}
    )
    record(session, run, artifacts, "impact-analysis.report", breaking)

    escalates, detail = check(
        registry,
        "has_breaking_contract_change",
        PredicateContext(session=session, run=run, artifacts=artifacts),
    )
    assert escalates
    assert "GET /links/{code}" in detail


def test_no_recorded_comparison_errors_rather_than_reporting_green(registry):
    """An unperformable check must never report green."""
    with pytest.raises(LookupError, match="no regression comparison"):
        check(registry, "no_pre_existing_test_regressed", PredicateContext())


def test_a_recorded_regression_fails_with_the_test_ids(registry):
    ctx = PredicateContext(
        facts={
            "regression.new_failures": Fact(2, FactSource.DERIVED, "compare"),
            "regression.ids": Fact(["t::a", "t::b"], FactSource.DERIVED, "compare"),
        }
    )
    passed, detail = check(registry, "no_pre_existing_test_regressed", ctx)

    assert not passed
    assert "t::a, t::b" in detail


# --------------------------------------------------------------------------- #
# normalization — the ambiguous scenario
# --------------------------------------------------------------------------- #


def decision(note: str, by: str = "kavadhani") -> str:
    return f"decision: approved\nby: {by}\nnote:\n{note}"


def register(*ambiguities: Ambiguity) -> RequirementRegister:
    return RequirementRegister(ambiguities=list(ambiguities))


def normalize(note: str, *ambiguities: Ambiguity) -> RequirementRegister:
    output = normalize_clarification(
        task(
            "...",
            params={"checkpoint": "clarify-with-human"},
            inputs={
                "intake.register": register(*ambiguities).model_dump_json(),
                "clarify-with-human.decision": decision(note),
            },
        )
    )
    return RequirementRegister.model_validate_json(output.artifacts["intake.register"])


def test_an_answer_is_matched_to_the_question_it_answers():
    updated = normalize(
        "A1: per API key, 100/minute\nA2: 429 with Retry-After",
        Ambiguity(id="A1", question="scope?", severity=Severity.HIGH),
        Ambiguity(id="A2", question="response?", severity=Severity.HIGH),
    )

    assert updated.ambiguities[0].answer.startswith("per API key, 100/minute")
    assert "kavadhani" in updated.ambiguities[0].answer  # attributable
    assert updated.ambiguities[1].answer.startswith("429 with Retry-After")


def test_an_unstructured_note_is_recorded_rather_than_discarded():
    """Losing what a person said is worse than attributing it a little widely."""
    updated = normalize(
        "limit per key, and 429 at the boundary",
        Ambiguity(id="A1", question="scope?", severity=Severity.HIGH),
    )
    assert "limit per key" in updated.ambiguities[0].answer


def test_a_question_nobody_answered_stays_open():
    """Which is exactly what the gate is looking for."""
    updated = normalize(
        "A1: per API key",
        Ambiguity(id="A1", question="scope?", severity=Severity.HIGH),
        Ambiguity(id="A2", question="window?", severity=Severity.HIGH),
    )
    assert not updated.ambiguities[1].is_disposed


def test_parsing_separates_answers_from_everything_else():
    answers, loose = parse_answers(decision("A1: 302\nthanks for asking"))
    assert answers == {"A1": "302"}
    assert loose == "thanks for asking"


# --------------------------------------------------------------------------- #
# the escalation threshold, as plan data
# --------------------------------------------------------------------------- #


def test_a_lower_threshold_escalates_a_medium_ambiguity():
    """The knob that decides how much autonomy the system takes, in data."""
    inputs = {
        "intake.register": register(
            Ambiguity(id="A1", question="scope?", severity=Severity.MEDIUM)
        ).model_dump_json()
    }

    lenient = triage_ambiguities(task("...", inputs=inputs))
    strict = triage_ambiguities(task("...", params={"threshold": "medium"}, inputs=inputs))

    assert lenient.facts["ambiguities.assumed"] == 1
    assert strict.facts["ambiguities.escalated"] == 1
