"""`py:` executor tests.

The property worth defending: a derivation is deterministic, not privileged. It
goes through the same `WorkScope` a code agent does, and its facts are stamped
`DERIVED` by the worker rather than declared by the task — a task that could
label its own output `TOOL`-sourced could launder an opinion into evidence.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

from orchestrator.artifacts import (
    Ambiguity,
    Design,
    DesignElement,
    Export,
    Interface,
    Module,
    RequirementRegister,
    Severity,
)
from orchestrator.derive import verify_target_matches_contract
from orchestrator.derive.scaffold import ContractError
from orchestrator.engine.plan import Node
from orchestrator.gates.checks import (
    imports_resolve,
    report_coverage,
    stubs_are_unimplemented,
)
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
        "run": "py:orchestrator.derive.verify_target_matches_contract",
        "write_scope": ["target/shortener/**"],
        "params": {"root": "target/shortener"},
    }
    payload.update(overrides)
    return Node.model_validate(payload)


SCOPE = WorkScope(allowed=("target/shortener/**",))


def task(inputs=None, *, cwd=Path("."), scope=SCOPE, params=None) -> Task:
    built = node(params=params) if params else node()
    return Task(node=built, inputs=inputs or {}, scope=scope, cwd=cwd)


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

    assert result.facts["contract.modules"].source is FactSource.DERIVED
    assert all(fact.source is not FactSource.AGENT for fact in result.facts.values())


def test_files_are_written_only_where_the_scope_permits(tmp_path):
    import orchestrator.derive.scaffold as scaffold_module

    scaffold_module.inside = lambda task: TaskOutput(
        files={"target/shortener/api/__init__.py": ""}
    )
    try:
        PyWorker(cwd=tmp_path).run(
            node(run="py:orchestrator.derive.scaffold.inside"), {}, SCOPE
        )
        assert (tmp_path / "target/shortener/api/__init__.py").exists()
    finally:
        del scaffold_module.inside


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
    assert "contract.modules" in result.facts


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
# the architect may declare, never implement (D24)
# --------------------------------------------------------------------------- #


def stub_check(tmp_path, **modules: str) -> Task:
    root = tmp_path / "target" / "shortener"
    for name, body in modules.items():
        package = root / name
        package.mkdir(parents=True, exist_ok=True)
        (package / "__init__.py").write_text(body)
    return Task(
        node=node(
            run="py:orchestrator.gates.stubs_are_unimplemented",
            params={"root": "target/shortener"},
        ),
        inputs={},
        scope=WorkScope(),
        cwd=tmp_path,
    )


def test_declared_stubs_pass(tmp_path):
    body = (
        "def resolve(code: str) -> str:\n"
        '    """Resolve a code."""\n'
        "    raise NotImplementedError\n"
    )
    output = stubs_are_unimplemented(stub_check(tmp_path, links=body))
    assert output.facts["stubs.implemented"] == 0
    assert output.facts["stubs.total"] == 1


@pytest.mark.parametrize(
    "body",
    [
        "def resolve(code: str) -> str:\n    return code\n",
        "def resolve(code: str) -> str:\n    x = 1\n    raise NotImplementedError\n",
        "def resolve(code: str) -> str:\n    raise ValueError('nope')\n",
        "def resolve(code: str) -> str:\n    pass\n",
    ],
)
def test_an_implemented_body_is_caught(tmp_path, body):
    """A model told to write stubs will eventually write one that works.

    Every one of these lints clean and imports clean — only parsing tells them
    apart from a declaration.
    """
    output = stubs_are_unimplemented(stub_check(tmp_path, links=body))
    assert output.facts["stubs.implemented"] == 1
    assert "links" in output.facts["stubs.offenders"][0]


def test_one_implemented_function_among_many_stubs_is_still_caught(tmp_path):
    """Not 'the file mentions NotImplementedError' — that passes for 9 of 10."""
    stubbed = "def a() -> None:\n    raise NotImplementedError\n"
    body = stubbed + "\n\ndef b() -> int:\n    return 2\n" + "\n" + stubbed.replace("a(", "c(")
    output = stubs_are_unimplemented(stub_check(tmp_path, links=body))
    assert output.facts["stubs.implemented"] == 1
    assert output.facts["stubs.total"] == 2


def test_methods_are_checked_too(tmp_path):
    body = (
        "class Repo:\n"
        "    def get(self, code: str) -> str:\n"
        "        return code\n"
    )
    output = stubs_are_unimplemented(stub_check(tmp_path, storage=body))
    assert output.facts["stubs.implemented"] == 1


def test_an_ellipsis_before_the_raise_is_still_a_stub(tmp_path):
    body = "def resolve() -> None:\n    ...\n    raise NotImplementedError\n"
    assert stubs_are_unimplemented(stub_check(tmp_path, links=body)).facts["stubs.implemented"] == 0


def test_a_file_that_does_not_parse_is_an_error_not_a_pass(tmp_path):
    with pytest.raises(WorkerError, match="does not parse"):
        stubs_are_unimplemented(stub_check(tmp_path, links="def broken(\n"))


# --------------------------------------------------------------------------- #
# auditing a target against its contract (D24)
# --------------------------------------------------------------------------- #


def contracted() -> Design:
    """Two modules where one calls the other — the shape that used to break."""
    return Design(
        modules=[Module(name="errors", path="errors"), Module(name="links", path="links")],
        elements=[DesignElement(id="E1", kind="module", summary="errors", satisfies=["R1"])],
        interfaces=[
            Interface(
                module="errors",
                exports=[
                    Export(name="LinkNotFound", kind="exception", summary="no such code"),
                    Export(name="LinkExpired", kind="exception", summary="past expiry"),
                ],
            ),
            Interface(
                module="links",
                depends_on=["errors"],
                exports=[
                    Export(
                        name="resolve",
                        kind="function",
                        signature="(code: str) -> str",
                        raises=["LinkNotFound", "LinkExpired"],
                    )
                ],
            ),
        ],
    )


def wrote(tmp_path, **modules: str) -> Task:
    """Lay out a target the way an architect would have written it."""
    root = tmp_path / "target" / "shortener"
    for name, body in modules.items():
        package = root / name
        package.mkdir(parents=True, exist_ok=True)
        (package / "__init__.py").write_text(body)
    return task(
        {"design.spec": contracted().model_dump_json()},
        cwd=tmp_path,
        params={"root": "target/shortener"},
    )


KEPT = {
    "errors": (
        "class LinkNotFound(Exception):\n    pass\n\n\n"
        "class LinkExpired(Exception):\n    pass\n"
    ),
    "links": "def resolve(code: str) -> str:\n    raise NotImplementedError\n",
}


def test_a_kept_contract_passes(tmp_path):
    output = verify_target_matches_contract(wrote(tmp_path, **KEPT))
    assert output.facts["contract.broken"] == 0
    assert output.facts["contract.kept"] == 3
    assert output.facts["contract.exports"] == 3


def test_a_promised_name_the_code_never_defines_is_caught(tmp_path):
    """The cost of letting the architect write Python: its two artifacts can disagree.

    Exactly the original failure, one stage earlier — `links` calls a name
    `errors` does not define. Caught before seven implementers write against it.
    """
    thinner = dict(KEPT, errors="class LinkNotFound(Exception):\n    pass\n")
    output = verify_target_matches_contract(wrote(tmp_path, **thinner))

    assert output.facts["contract.broken"] == 1
    assert "errors.LinkExpired (exception)" in output.facts["contract.missing"]


def test_a_re_exported_name_counts_as_defined(tmp_path):
    """A package whose __init__ imports a name does define it for its callers."""
    split = dict(KEPT, errors="from target.shortener.errors.kinds import (\n"
                              "    LinkExpired,\n    LinkNotFound,\n)\n")
    output = verify_target_matches_contract(wrote(tmp_path, **split))
    assert output.facts["contract.broken"] == 0


def test_an_empty_package_breaks_every_promise_it_made(tmp_path):
    output = verify_target_matches_contract(wrote(tmp_path, errors="", links=KEPT["links"]))
    assert output.facts["contract.broken"] == 2


def test_a_circular_contract_is_refused_before_implementation(tmp_path):
    """Parallel implementation is only safe over a DAG."""
    design = Design(
        modules=[Module(name="a", path="a"), Module(name="b", path="b")],
        interfaces=[
            Interface(module="a", depends_on=["b"], exports=[Export(name="f", kind="function")]),
            Interface(module="b", depends_on=["a"], exports=[Export(name="g", kind="function")]),
        ],
    )
    probe = task(
        {"design.spec": design.model_dump_json()},
        cwd=tmp_path,
        params={"root": "target/shortener"},
    )
    with pytest.raises(ContractError, match="acyclic"):
        verify_target_matches_contract(probe)


def test_the_manifest_records_what_was_audited(tmp_path):
    output = verify_target_matches_contract(wrote(tmp_path, **KEPT))
    assert "target/shortener/errors" in output.artifacts["scaffold.manifest"]


def test_the_audit_never_imports_the_target(tmp_path):
    """Parsed, not imported: a module with an import-time side effect would run
    it inside the orchestrator. Whether it *can* be imported is a separate check,
    in a subprocess, which is the right place for that question."""
    exploding = dict(KEPT, links=KEPT["links"] + "\nraise SystemExit('imported')\n")
    output = verify_target_matches_contract(wrote(tmp_path, **exploding))
    assert output.facts["contract.broken"] == 0


# --------------------------------------------------------------------------- #
# the checks that produce a gate's facts
# --------------------------------------------------------------------------- #


def check_task(tmp_path, **params) -> Task:
    return Task(
        node=node(id="scaffold", run="py:orchestrator.gates.imports_resolve", params=params),
        inputs={},
        scope=WorkScope(),          # a check verifies; it does not write
        cwd=tmp_path,
    )


def package(tmp_path, **modules: str) -> None:
    root = tmp_path / "target" / "shortener"
    root.mkdir(parents=True)
    (root / "__init__.py").write_text("")
    for name, body in modules.items():
        (root / f"{name}.py").write_text(body)


def test_a_package_whose_modules_import_reports_true(tmp_path):
    package(tmp_path, storage="VALUE = 1\n", api="from shortener import storage\n")

    output = imports_resolve(check_task(tmp_path, root="target/shortener"))

    assert output.facts["imports.resolve"] is True
    assert output.facts["imports.modules"] == 3      # package, storage, api


def test_a_module_that_does_not_import_is_named(tmp_path):
    """The failure the scaffold gate exists to catch: generated code that
    references something that was never generated."""
    package(tmp_path, api="from shortener import nowhere\n")

    output = imports_resolve(check_task(tmp_path, root="target/shortener"))

    assert output.facts["imports.resolve"] is False
    assert any("shortener.api" in item for item in output.facts["imports.unresolved"])


def test_an_empty_tree_is_not_a_pass(tmp_path):
    """A scaffold that produced nothing and a scaffold whose every module
    imports are different outcomes."""
    (tmp_path / "target").mkdir()

    output = imports_resolve(check_task(tmp_path, root="target/shortener"))
    assert output.facts["imports.resolve"] is False


def test_target_code_is_imported_in_a_separate_interpreter(tmp_path):
    """A module with a side effect at import time must not run in this process."""
    package(tmp_path, boom="import sys; sys.modules['ORCHESTRATOR_WAS_HERE'] = True\n")

    imports_resolve(check_task(tmp_path, root="target/shortener"))
    assert "ORCHESTRATOR_WAS_HERE" not in sys.modules


def test_coverage_is_read_from_the_report_the_tool_wrote(tmp_path):
    (tmp_path / "coverage.json").write_text(
        json.dumps({"totals": {"percent_covered": 91.666, "num_statements": 120}})
    )

    output = report_coverage(check_task(tmp_path))

    assert output.facts["coverage.percent"] == 91.67
    assert output.facts["coverage.statements"] == 120


def test_a_missing_coverage_report_errors_rather_than_reporting_zero(tmp_path):
    """0% is a finding about the target; a missing report is a finding about
    the run, and a gate must be able to tell them apart."""
    with pytest.raises(WorkerError, match="no coverage report"):
        report_coverage(check_task(tmp_path))


def test_an_unreadable_coverage_report_errors(tmp_path):
    (tmp_path / "coverage.json").write_text("not json")

    with pytest.raises(WorkerError, match="not readable"):
        report_coverage(check_task(tmp_path))
