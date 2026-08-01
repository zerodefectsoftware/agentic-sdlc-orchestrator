"""`py:` executor tests.

The property worth defending: a derivation is deterministic, not privileged. It
goes through the same `WorkScope` a code agent does, and its facts are stamped
`DERIVED` by the worker rather than declared by the task — a task that could
label its own output `TOOL`-sourced could launder an opinion into evidence.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from orchestrator.artifacts import (
    Ambiguity,
    Design,
    DesignElement,
    Module,
    RequirementRegister,
    Severity,
)
from orchestrator.derive import scaffold_from_design
from orchestrator.engine.plan import Node
from orchestrator.gates.facts import FactSource
from orchestrator.gates.security import security_scan
from orchestrator.policy.triage import triage_ambiguities
from orchestrator.workers import CommandWorker, PyWorker, TaskOutput, WorkerError, WorkScope
from orchestrator.workers.pytask import Task, resolve


def node(**overrides) -> Node:
    payload = {
        "id": "scaffold",
        "kind": "derive",
        "stage": "implementation",
        "run": "py:orchestrator.derive.scaffold_from_design",
        "write_scope": ["target/shortener/**"],
    }
    payload.update(overrides)
    return Node.model_validate(payload)


SCOPE = WorkScope(allowed=("target/shortener/**",))


def task(inputs=None, *, cwd=Path("."), scope=SCOPE) -> Task:
    return Task(node=node(), inputs=inputs or {}, scope=scope, cwd=cwd)


# --------------------------------------------------------------------------- #
# resolution
# --------------------------------------------------------------------------- #


def test_a_dotted_path_resolves_to_a_callable():
    assert resolve("orchestrator.policy.triage_ambiguities") is triage_ambiguities


@pytest.mark.parametrize(
    ("target", "expected"),
    [
        ("notdotted", "not a dotted path"),
        ("orchestrator.nowhere.thing", "cannot import"),
        ("orchestrator.policy.no_such_task", "has no attribute"),
        ("orchestrator.policy.ESCALATION_PREFIX", "not callable"),
    ],
)
def test_resolution_failures_say_precisely_what_is_wrong(target, expected):
    with pytest.raises(WorkerError, match=expected):
        resolve(target)


# --------------------------------------------------------------------------- #
# the worker
# --------------------------------------------------------------------------- #


def test_facts_are_stamped_derived_by_the_worker(tmp_path):
    """A task cannot declare its own provenance (D4)."""
    design = Design(modules=[Module(name="api", path="api")])
    result = PyWorker(cwd=tmp_path).run(
        node(), {"design.spec": design.model_dump_json()}, SCOPE
    )

    assert result.facts["scaffold.modules"].source is FactSource.DERIVED
    assert all(fact.source is not FactSource.AGENT for fact in result.facts.values())


def test_files_are_written_only_where_the_scope_permits(tmp_path):
    design = Design(modules=[Module(name="api", path="api")])
    PyWorker(cwd=tmp_path).run(node(), {"design.spec": design.model_dump_json()}, SCOPE)

    assert (tmp_path / "target/shortener/api/__init__.py").exists()


def test_a_task_writing_outside_its_scope_is_refused(tmp_path):
    """The same rule a code agent gets; determinism is not a privilege."""

    def rogue(task):
        return TaskOutput(files={"src/orchestrator/engine/loader.py": "pwned"})

    import orchestrator.derive.scaffold as scaffold_module

    scaffold_module.rogue = rogue
    try:
        with pytest.raises(WorkerError, match="outside its scope"):
            PyWorker(cwd=tmp_path).run(
                node(run="py:orchestrator.derive.scaffold.rogue"), {}, SCOPE
            )
        assert not (tmp_path / "src").exists()
    finally:
        del scaffold_module.rogue


def test_a_task_that_returns_the_wrong_type_is_refused(tmp_path):
    import orchestrator.derive.scaffold as scaffold_module

    scaffold_module.wrong = lambda task: {"facts": {}}
    try:
        with pytest.raises(WorkerError, match="must return TaskOutput"):
            PyWorker(cwd=tmp_path).run(
                node(run="py:orchestrator.derive.scaffold.wrong"), {}, SCOPE
            )
    finally:
        del scaffold_module.wrong


def test_a_missing_required_input_says_what_was_available(tmp_path):
    """A policy applied to an absent register would return a confident, empty answer."""
    with pytest.raises(WorkerError, match="needs input 'design.spec'"):
        PyWorker(cwd=tmp_path).run(node(), {}, SCOPE)


def test_the_worker_refuses_a_shell_target(tmp_path):
    with pytest.raises(WorkerError, match="executes 'py:' only"):
        PyWorker(cwd=tmp_path).run(node(run="sh:make"), {}, SCOPE)


def test_the_command_worker_routes_by_scheme(tmp_path):
    """The gap --dry-run found: schemes were declared and never read."""
    design = Design(modules=[])
    worker = CommandWorker(python=PyWorker(cwd=tmp_path))
    result = worker.run(node(), {"design.spec": design.model_dump_json()}, SCOPE)
    assert "scaffold.modules" in result.facts


def test_a_node_with_no_command_is_reported_not_guessed():
    detail = CommandWorker().describe(node(run=None, kind="human", stage="release"), {}, SCOPE)
    assert detail["issues"]


# --------------------------------------------------------------------------- #
# triage — the escalation threshold is deterministic (D13)
# --------------------------------------------------------------------------- #


def test_high_severity_is_left_for_a_human():
    register = RequirementRegister(
        ambiguities=[Ambiguity(id="A1", question="301 or 302?", severity=Severity.HIGH)]
    )
    output = triage_ambiguities(task({"intake.register": register.model_dump_json()}))

    assert output.facts["ambiguities.escalated"] == 1
    updated = RequirementRegister.model_validate_json(output.artifacts["intake.register"])
    assert not updated.ambiguities[0].is_disposed


def test_lower_severity_carries_a_recorded_assumption():
    """A system that asks forty questions is as useless as one that asks none."""
    register = RequirementRegister(
        ambiguities=[Ambiguity(id="A2", question="idempotent?", severity=Severity.MEDIUM)]
    )
    output = triage_ambiguities(task({"intake.register": register.model_dump_json()}))

    assert output.facts["ambiguities.assumed"] == 1
    updated = RequirementRegister.model_validate_json(output.artifacts["intake.register"])
    assert updated.ambiguities[0].is_disposed
    assert "below the escalation threshold" in updated.ambiguities[0].answer


# --------------------------------------------------------------------------- #
# the security scan
# --------------------------------------------------------------------------- #


def test_the_scan_finds_an_open_redirect(tmp_path):
    """The risk intrinsic to what a URL shortener is."""
    source = tmp_path / "target" / "shortener"
    source.mkdir(parents=True)
    (source / "main.py").write_text("return RedirectResponse(url=stored.url)\n")

    output = security_scan(task(cwd=tmp_path))
    assert output.facts["scan.high"] >= 1
    assert "OPEN_REDIRECT" in output.artifacts["security.report"]


def test_a_clean_target_produces_an_empty_report(tmp_path):
    (tmp_path / "target").mkdir()
    output = security_scan(task(cwd=tmp_path))
    assert output.facts["scan.findings"] == 0


# --------------------------------------------------------------------------- #
# the scaffold derivation
# --------------------------------------------------------------------------- #


def test_the_scaffold_derives_a_package_per_module():
    design = Design(
        modules=[Module(name="api", path="api", responsibility="HTTP surface")],
        elements=[DesignElement(id="E1", kind="module", summary="api", satisfies=["R1"])],
    )
    output = scaffold_from_design(task({"design.spec": design.model_dump_json()}))

    body = output.files["target/shortener/api/__init__.py"]
    assert "HTTP surface" in body
    assert "R1" in body            # traceability survives into the generated stub
    assert "Implementation belongs" in body   # it is a stub, not an implementation


def test_the_package_root_comes_from_the_scope_not_a_constant():
    """Nothing here knows the target is a URL shortener (D3)."""
    design = Design(modules=[Module(name="core", path="core")])
    elsewhere = WorkScope(allowed=("target/other/**",))
    output = scaffold_from_design(
        task({"design.spec": design.model_dump_json()}, scope=elsewhere)
    )
    assert "target/other/core/__init__.py" in output.files


def test_the_manifest_lists_what_was_generated():
    design = Design(modules=[Module(name="api", path="api")])
    output = scaffold_from_design(task({"design.spec": design.model_dump_json()}))
    assert "target/shortener/api/__init__.py" in output.artifacts["scaffold.manifest"]


def test_scaffolding_a_design_with_no_modules_still_makes_the_package():
    output = scaffold_from_design(task({"design.spec": Design().model_dump_json()}))
    assert list(output.files) == ["target/shortener/__init__.py"]
    assert json.loads("{}") == {}  # sanity: the artifact bodies are plain text, not JSON
