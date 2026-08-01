"""CLI tests.

The one that matters is `test_a_run_survives_being_stopped_and_resumed`. Every
other command is a view; that one exercises the claim the whole design rests on
— a run stops at a checkpoint, the process exits, and a *different* process
picks it up and finishes.
"""

from __future__ import annotations

import textwrap

import pytest
from typer.testing import CliRunner

from orchestrator.cli.main import app
from orchestrator.config import get_settings, reset_settings

PLAN = """
plan: demo
version: 1
nodes:
  - id: design
    kind: agent
    stage: design
    role: architect
    outputs: [openapi]
  - id: design-approval
    kind: human
    stage: design
    needs: [design]
    autonomy: APPROVE
    binds_to: [design.artifacts.openapi]
  - id: build
    kind: tool
    stage: implementation
    needs: [design-approval]
    run: sh:make
  - id: verify
    kind: tool
    stage: verification
    needs: [build]
    run: sh:pytest
"""


@pytest.fixture
def workspace(tmp_path, monkeypatch):
    """An isolated environment, configured the way an operator would."""
    plans = tmp_path / "plans"
    plans.mkdir()
    (plans / "demo.yaml").write_text(textwrap.dedent(PLAN))
    (tmp_path / "requirement.md").write_text("Build something.")
    (tmp_path / "target.yaml").write_text("target: {}\n")

    monkeypatch.setenv("ORCHESTRATOR_RUNS_DIR", str(tmp_path / "runs"))
    monkeypatch.setenv("ORCHESTRATOR_PLANS_DIR", str(plans))
    monkeypatch.setenv("ORCHESTRATOR_WORKER", "stub")
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    reset_settings()
    yield tmp_path
    reset_settings()


@pytest.fixture
def cli():
    return CliRunner()


def invoke(cli, workspace, *args):
    result = cli.invoke(app, list(args))
    assert result.exit_code == 0, result.output
    return result.output


def start(cli, workspace):
    return invoke(
        cli,
        workspace,
        "run",
        "--plan", str(workspace / "plans" / "demo.yaml"),
        "--requirement", str(workspace / "requirement.md"),
        "--target", str(workspace / "target.yaml"),
    )


# --------------------------------------------------------------------------- #
# the cycle the design rests on
# --------------------------------------------------------------------------- #


def test_a_run_stops_at_a_checkpoint_rather_than_waiting(cli, workspace):
    """A blocked run holding a terminal open for three hours is not a checkpoint."""
    output = start(cli, workspace)

    assert "blocked" in output
    assert "awaiting decision" in output
    assert "design-approval" in output
    assert "orchestrator approve" in output  # tells you the next move


def test_a_run_survives_being_stopped_and_resumed(cli, workspace):
    """Separate invocations: state comes back from disk, not from memory."""
    start(cli, workspace)
    run_id = _latest_run_id(cli, workspace)

    output = invoke(cli, workspace, "approve", run_id, "design-approval", "--by", "alice")

    assert "approved" in output
    assert "completed" in output  # ran through build and verify after the decision


def test_the_decision_records_who_made_it(cli, workspace):
    start(cli, workspace)
    run_id = _latest_run_id(cli, workspace)
    invoke(cli, workspace, "approve", run_id, "design-approval", "--by", "alice")

    output = invoke(cli, workspace, "evidence", run_id)
    assert "alice" in output
    assert "design.openapi@v1" in output  # the version the approval covered


def test_rejecting_stops_the_run(cli, workspace):
    start(cli, workspace)
    run_id = _latest_run_id(cli, workspace)

    output = invoke(
        cli, workspace, "reject", run_id, "design-approval",
        "--by", "alice", "--note", "contract is wrong",
    )
    assert "rejected" in output

    assert "failed" in invoke(cli, workspace, "status", run_id)


def test_deciding_a_checkpoint_that_is_not_pending_is_refused(cli, workspace):
    start(cli, workspace)
    run_id = _latest_run_id(cli, workspace)

    result = cli.invoke(app, ["approve", run_id, "build", "--by", "alice"])
    assert result.exit_code != 0
    assert "no decision pending" in result.output


# --------------------------------------------------------------------------- #
# preflight
# --------------------------------------------------------------------------- #


def test_preflight_accepts_a_plan_whose_checks_can_all_be_performed(cli, workspace):
    output = invoke(cli, workspace, "preflight", "--plan", str(workspace / "plans" / "demo.yaml"))
    assert "plan valid" in output
    assert "every predicate is registered" in output


def test_preflight_rejects_an_invalid_plan(cli, workspace, tmp_path):
    broken = tmp_path / "broken.yaml"
    broken.write_text("plan: x\nversion: 1\nnodes:\n  - id: a\n    kind: tool\n")

    result = cli.invoke(app, ["preflight", "--plan", str(broken)])
    assert result.exit_code == 1
    assert "invalid plan" in result.output


def test_the_real_greenfield_plan_passes_preflight(cli):
    """Every predicate the shipped plan names is registered."""
    result = CliRunner().invoke(app, ["preflight", "--plan", "plans/greenfield.yaml"])
    assert result.exit_code == 0, result.output
    assert "every predicate is registered" in result.output


# --------------------------------------------------------------------------- #
# views
# --------------------------------------------------------------------------- #


def test_status_shows_every_node_with_its_stage(cli, workspace):
    start(cli, workspace)
    output = invoke(cli, workspace, "status")

    for node in ("design", "design-approval", "build", "verify"):
        assert node in output
    assert "implementation" in output


def test_metrics_report_the_caveat_with_the_numbers(cli, workspace):
    """Three runs are instrumentation, not statistics."""
    start(cli, workspace)
    output = invoke(cli, workspace, "metrics")

    assert "success rate" in output
    assert "no significance is claimed" in output


def test_evidence_can_be_written_to_disk(cli, workspace):
    start(cli, workspace)
    output = invoke(cli, workspace, "evidence", "--write")

    assert "wrote" in output
    written = list((workspace / "runs").rglob("evidence.md"))
    assert written and written[0].read_text().startswith("# Evidence bundle")


def test_why_traces_an_artifact_to_what_produced_it(cli, workspace):
    start(cli, workspace)
    output = invoke(cli, workspace, "why", "design.openapi")
    assert "design.openapi@v1" in output


def test_config_shows_what_is_resolved_and_what_is_not_a_setting(cli, workspace):
    output = invoke(cli, workspace, "config")

    assert "worker" in output
    assert "unset" in output  # no API key, and none needed
    assert "not settings" in output  # model and effort live in the plan


def test_runs_lists_what_has_happened(cli, workspace):
    start(cli, workspace)
    assert "demo" in invoke(cli, workspace, "runs")


# --------------------------------------------------------------------------- #
# refusals
# --------------------------------------------------------------------------- #


def test_a_live_run_without_the_agent_worker_is_refused_not_downgraded(cli, workspace, monkeypatch):
    """A run that quietly used stubs when asked for real models would produce
    evidence describing work nobody did."""
    monkeypatch.setenv("ORCHESTRATOR_WORKER", "live")
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-ant-test")
    reset_settings()

    result = cli.invoke(app, ["resume"])
    assert result.exit_code != 0


def test_commands_report_clearly_when_there_are_no_runs(cli, workspace):
    result = cli.invoke(app, ["status"])
    assert result.exit_code != 0
    assert "no runs recorded yet" in result.output


def _latest_run_id(cli, workspace) -> str:
    from sqlalchemy import select

    from orchestrator.state import store
    from orchestrator.state.models import Run

    with store.Store(get_settings().database_url).session() as session:
        return session.scalar(select(Run).order_by(Run.started_at.desc()).limit(1)).id
