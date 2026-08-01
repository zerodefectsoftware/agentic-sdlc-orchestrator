"""Worker tests.

The seam that makes a non-deterministic system testable (D18). Three properties
matter more than the rest: a worker never returns a verdict, provenance survives
a replay round trip, and a missing recording is an error rather than a pass.
"""

from __future__ import annotations

import json

import pytest

from orchestrator.engine.plan import Gate, Node
from orchestrator.gates import Verdict, evaluate_gate
from orchestrator.gates.facts import Fact, FactSource
from orchestrator.workers import (
    ProducedArtifact,
    RecordingWorker,
    ReplayWorker,
    StubWorker,
    ToolWorker,
    WorkerError,
    WorkerResult,
    WorkScope,
)
from orchestrator.workers import stub as scripts
from orchestrator.workers.replay import decode, encode, fixture_key
from orchestrator.workers.tool import fact_namespace


def node(**overrides) -> Node:
    payload = {
        "id": "tests",
        "kind": "tool",
        "stage": "verification",
        "run": "sh:echo hello",
    }
    payload.update(overrides)
    return Node.model_validate(payload)


EMPTY_SCOPE = WorkScope()


# --------------------------------------------------------------------------- #
# write scope
# --------------------------------------------------------------------------- #


def test_scope_permits_only_declared_paths():
    """D7: an agent that decides the cleanest fix is a neighbour's module is denied."""
    scope = WorkScope(allowed=("target/shortener/api/**",))
    assert scope.permits("target/shortener/api/routes.py")
    assert not scope.permits("target/shortener/storage/db.py")
    assert not scope.permits("src/orchestrator/engine/loader.py")


def test_frozen_paths_win_over_allowed_ones():
    """D6: the cheapest route to a green suite is a weakened test."""
    scope = WorkScope(allowed=("target/**",), frozen=("target/tests/**",))
    assert scope.permits("target/shortener/main.py")
    assert not scope.permits("target/tests/test_api.py")


def test_scope_reports_every_violation():
    scope = WorkScope(allowed=("target/**",), frozen=("target/tests/**",))
    changed = ["target/shortener/a.py", "target/tests/b.py", "pyproject.toml"]
    assert scope.violations(changed) == ["target/tests/b.py", "pyproject.toml"]


def test_scope_is_derived_from_the_node():
    n = node(write_scope=["target/x/**"], freeze_paths=["target/tests/**"])
    scope = WorkScope.for_node(n)
    assert scope.allowed == ("target/x/**",)
    assert scope.frozen == ("target/tests/**",)


# --------------------------------------------------------------------------- #
# the tool worker
# --------------------------------------------------------------------------- #


def test_tool_worker_records_the_exit_code_as_a_tool_fact():
    result = ToolWorker().run(node(run="sh:python3 -c 'exit(0)'"), {}, EMPTY_SCOPE)
    fact = result.facts["python3.exit_code"]
    assert fact.value == 0
    assert fact.source is FactSource.TOOL


def test_tool_worker_reports_a_nonzero_exit_without_raising():
    """A command that ran and failed is a gate FAIL, not a worker error."""
    result = ToolWorker().run(node(run="sh:python3 -c 'exit(3)'"), {}, EMPTY_SCOPE)
    assert result.facts["python3.exit_code"].value == 3


def test_tool_worker_raises_when_the_command_does_not_exist():
    """No exit code was produced, so there is nothing to gate on — the ERROR path."""
    with pytest.raises(WorkerError, match="command not found"):
        ToolWorker().run(node(run="sh:definitely-not-a-real-binary"), {}, EMPTY_SCOPE)


def test_tool_worker_rejects_a_python_target():
    with pytest.raises(WorkerError, match="handles 'sh:' only"):
        ToolWorker().run(node(run="py:orchestrator.gates.security_scan"), {}, EMPTY_SCOPE)


@pytest.mark.parametrize(
    ("command", "expected"),
    [
        (".venv/bin/pytest target/tests", "pytest"),
        ("ruff check target/", "ruff"),
        ("/usr/bin/env python3 -m pytest", "env"),
        ("", "command"),
    ],
)
def test_facts_are_namespaced_by_the_tool_not_the_node(command, expected):
    """Plans gate on `pytest.exit_code`, not `tests.exit_code`."""
    assert fact_namespace(command) == expected


def test_tool_facts_reach_a_real_gate():
    """End to end: a command runs, and its exit code satisfies the plan's expression."""
    result = ToolWorker().run(node(run="sh:python3 -c 'exit(0)'"), {}, EMPTY_SCOPE)
    gate = Gate.model_validate({"all": ["python3.exit_code == 0"]})
    assert evaluate_gate(gate, result.facts).passed


# --------------------------------------------------------------------------- #
# the stub worker — arranging failures on demand
# --------------------------------------------------------------------------- #


def test_stub_returns_the_scripted_result():
    worker = StubWorker({"tests": scripts.passing()})
    assert worker.run(node(), {}, EMPTY_SCOPE).facts["pytest.exit_code"].value == 0


def test_stub_can_script_a_sequence_so_repair_loops_are_testable():
    """'fails, then passes after the fix node' is the repair loop's whole reason
    for existing, and this is the one line of setup that arranges it."""
    worker = StubWorker({"tests": [scripts.failing(), scripts.passing()]})
    first = worker.run(node(), {}, EMPTY_SCOPE)
    second = worker.run(node(), {}, EMPTY_SCOPE)
    assert first.facts["pytest.exit_code"].value == 1
    assert second.facts["pytest.exit_code"].value == 0


def test_the_last_scripted_result_repeats():
    worker = StubWorker({"tests": [scripts.failing(), scripts.passing()]})
    for _ in range(3):
        worker.run(node(), {}, EMPTY_SCOPE)
    assert worker.run(node(), {}, EMPTY_SCOPE).facts["pytest.exit_code"].value == 0


def test_stub_can_raise_to_simulate_a_worker_crash():
    worker = StubWorker({"tests": WorkerError("sandbox died")})
    with pytest.raises(WorkerError, match="sandbox died"):
        worker.run(node(), {}, EMPTY_SCOPE)


def test_stub_refuses_to_invent_a_result():
    worker = StubWorker({"design": scripts.passing()})
    with pytest.raises(WorkerError, match="no scripted result for 'tests'"):
        worker.run(node(), {}, EMPTY_SCOPE)


def test_stub_records_what_was_called():
    worker = StubWorker(default=scripts.passing())
    worker.run(node(), {}, EMPTY_SCOPE)
    worker.run(node(id="docs"), {}, EMPTY_SCOPE)
    assert worker.calls == ["tests", "docs"]


def test_unevaluable_result_makes_a_gate_error_not_fail():
    """The distinction the whole failure policy rests on."""
    result = scripts.unevaluable()
    gate = Gate.model_validate({"all": ["pytest.exit_code == 0"]})
    assert evaluate_gate(gate, result.facts).verdict is Verdict.ERROR


def test_a_self_reported_result_is_inadmissible():
    """D4, end to end through a worker."""
    result = scripts.self_reported()
    gate = Gate.model_validate({"all": ["impl.complete == true"]})
    assert evaluate_gate(gate, result.facts).verdict is Verdict.ERROR


# --------------------------------------------------------------------------- #
# record and replay
# --------------------------------------------------------------------------- #


def test_recording_then_replaying_reproduces_the_result(tmp_path):
    live = StubWorker({"tests": scripts.passing("pytest", report="coverage: 91%")})
    recorder = RecordingWorker(live, fixtures_dir=tmp_path)

    recorded = recorder.run(node(), {}, EMPTY_SCOPE)
    replayed = ReplayWorker(tmp_path).run(node(), {}, EMPTY_SCOPE)

    assert replayed.facts["pytest.exit_code"].value == recorded.facts["pytest.exit_code"].value
    assert replayed.artifact("report").content == "coverage: 91%"


def test_replay_preserves_provenance(tmp_path):
    """A replayed agent self-report must stay inadmissible — otherwise replay
    launders claims into evidence."""
    live = StubWorker({"tests": scripts.self_reported()})
    RecordingWorker(live, fixtures_dir=tmp_path).run(node(), {}, EMPTY_SCOPE)

    replayed = ReplayWorker(tmp_path).run(node(), {}, EMPTY_SCOPE)
    assert replayed.facts["impl.complete"].source is FactSource.AGENT

    gate = Gate.model_validate({"all": ["impl.complete == true"]})
    assert evaluate_gate(gate, replayed.facts).verdict is Verdict.ERROR


def test_a_missing_recording_is_an_error_not_a_pass(tmp_path):
    """Silently succeeding on unrecorded input would look like a green run."""
    with pytest.raises(WorkerError, match="no recording for 'tests'"):
        ReplayWorker(tmp_path).run(node(), {}, EMPTY_SCOPE)


def test_different_inputs_get_different_recordings(tmp_path):
    """The same node given different upstream artifacts is different work."""
    n = node()
    first = {"design.spec": '{"elements": []}'}
    second = {"design.spec": '{"elements": [{"id": "E1", "kind": "endpoint"}]}'}
    assert fixture_key(n, first) != fixture_key(n, second)

    ReplayWorker(tmp_path).path_for(n, first).parent.mkdir(parents=True, exist_ok=True)
    assert ReplayWorker(tmp_path).path_for(n, first) != ReplayWorker(tmp_path).path_for(n, second)


def test_fixture_key_is_stable_across_input_ordering():
    n = node()
    a = {"x": "one", "y": "two"}
    b = {"y": "two", "x": "one"}
    assert fixture_key(n, a) == fixture_key(n, b)


def test_encoding_round_trips_every_field():
    original = WorkerResult(
        facts={"pytest.exit_code": Fact(0, FactSource.TOOL, "pytest")},
        artifacts=(ProducedArtifact("report", "body", path="runs/report.md"),),
        consumed=("requirements@v1",),
        model="claude-opus-5",
        prompt_ref="prompts/analyst.md",
        duration_ms=1234,
    )
    restored = decode(json.loads(json.dumps(encode(original))))

    assert restored.facts["pytest.exit_code"].produced_by == "pytest"
    assert restored.artifacts[0].path == "runs/report.md"
    assert restored.consumed == ("requirements@v1",)
    assert restored.model == "claude-opus-5"
    assert restored.duration_ms == 1234


def test_recording_worker_names_itself_after_what_it_wraps(tmp_path):
    """An evidence bundle that cannot tell live from replayed is not evidence."""
    assert RecordingWorker(StubWorker(), fixtures_dir=tmp_path).name == "recording:stub"
    assert ReplayWorker(tmp_path).name == "replay"
    assert ToolWorker().name == "tool"
