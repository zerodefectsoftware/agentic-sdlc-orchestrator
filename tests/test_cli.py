"""CLI tests.

The one that matters is `test_a_run_survives_being_stopped_and_resumed`. Every
other command is a view; that one exercises the claim the whole design rests on
— a run stops at a checkpoint, the process exits, and a *different* process
picks it up and finishes.
"""

from __future__ import annotations

import textwrap

import pytest
from sqlalchemy import select
from typer.testing import CliRunner

from orchestrator.artifacts import Baseline
from orchestrator.cli.main import app
from orchestrator.config import get_settings, reset_settings
from orchestrator.lineage import recorder
from orchestrator.state import store
from orchestrator.state.artifacts import ArtifactStore
from orchestrator.state.models import NodeStatus, Run, RunStatus

PLAN = """
plan: demo
version: 1
nodes:
  - id: design
    kind: agent
    stage: design
    role: architect
    output_schema: schemas/design.json
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
    prompts = tmp_path / "prompts"
    prompts.mkdir()
    (prompts / "architect.md").write_text("You design.")
    (tmp_path / "requirement.md").write_text("Build something.")
    (tmp_path / "target.yaml").write_text(
        "target:\n"
        "  name: demo\n"
        "  root: target/demo\n"
        "  tests_root: target/tests\n"
        "  write_ceiling: ['target/**']\n"
    )

    monkeypatch.setenv("ORCHESTRATOR_RUNS_DIR", str(tmp_path / "runs"))
    monkeypatch.setenv("ORCHESTRATOR_PLANS_DIR", str(plans))
    monkeypatch.setenv("ORCHESTRATOR_PROMPTS_DIR", str(prompts))
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
        "--plan",
        str(workspace / "plans" / "demo.yaml"),
        "--requirement",
        str(workspace / "requirement.md"),
        "--target",
        str(workspace / "target.yaml"),
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
        cli,
        workspace,
        "reject",
        run_id,
        "design-approval",
        "--by",
        "alice",
        "--note",
        "contract is wrong",
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
# dry run
# --------------------------------------------------------------------------- #


def test_a_dry_run_executes_nothing(cli, workspace):
    """It should be safe to point at a live configuration you cannot yet run."""
    invoke(
        cli,
        workspace,
        "run",
        "--dry-run",
        "--plan",
        str(workspace / "plans" / "demo.yaml"),
        "--requirement",
        str(workspace / "requirement.md"),
    )
    result = cli.invoke(app, ["runs"])
    assert "demo" not in result.output  # no run was recorded


def test_a_dry_run_shows_what_each_node_would_dispatch(cli, workspace):
    output = invoke(
        cli,
        workspace,
        "run",
        "--dry-run",
        "--plan",
        str(workspace / "plans" / "demo.yaml"),
        "--requirement",
        str(workspace / "requirement.md"),
    )

    assert "nothing will execute" in output
    assert "design" in output
    assert "architect.md" in output  # the prompt it would send
    assert "RequirementRegister" in output or "Design" in output


def test_a_dry_run_needs_no_credential(cli, workspace, monkeypatch):
    """The point is inspecting a configuration on a machine that cannot run it."""
    monkeypatch.setenv("ORCHESTRATOR_WORKER", "live")
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    reset_settings()

    result = cli.invoke(app, ["run", "--dry-run", "--plan", str(workspace / "plans" / "demo.yaml")])
    assert "ANTHROPIC_API_KEY" not in result.output


def test_a_dry_run_fails_when_a_node_could_not_dispatch(cli, workspace, monkeypatch):
    """Surfacing this before a live run is the whole point of the flag."""
    monkeypatch.setenv("ORCHESTRATOR_PROMPTS_DIR", str(workspace / "absent"))
    reset_settings()

    result = cli.invoke(app, ["run", "--dry-run", "--plan", str(workspace / "plans" / "demo.yaml")])
    assert result.exit_code == 1
    assert "problems that would fail a live run" in result.output


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


# --------------------------------------------------------------------------- #
# rollback — the failure control that is easiest to claim and hardest to mean
# --------------------------------------------------------------------------- #


RESTORE_PLAN = """
plan: restore
version: 1
rollback:
  restore_from: baseline
  verify_with: "{verify}"
nodes:
  - id: baseline
    kind: tool
    stage: requirements
    run: py:orchestrator.policy.capture_baseline
    outputs: [snapshot]
"""


def arrange_rollback(workspace, monkeypatch, *, verify: str, body: str = "original\n") -> str:
    """A run with a recorded baseline, and a target that has since been damaged."""
    (workspace / "plans" / "restore.yaml").write_text(RESTORE_PLAN.format(verify=verify))

    monkeypatch.chdir(workspace)
    damaged = workspace / "target" / "demo" / "main.py"
    damaged.parent.mkdir(parents=True)
    damaged.write_text("broken\n")

    baseline = Baseline(green=True, snapshot_ref="abc123", files={"target/demo/main.py": body})
    with store.Store().session() as session:
        run = store.start_run(
            session,
            plan_name="restore",
            plan_version=1,
            requirement_path=str(workspace / "requirement.md"),
            target_profile=str(workspace / "target.yaml"),
            nodes=[("baseline", "tool", "requirements")],
        )
        content = baseline.model_dump_json()
        artifact = recorder.record_artifact(session, run, name="baseline.snapshot", content=content)
        artifact.path = str(ArtifactStore().write(run.id, "baseline.snapshot", 1, content))
        session.flush()
        return run.id


def test_rollback_restores_the_baseline_and_verifies_it(cli, workspace, monkeypatch):
    """An unverified restore is a second unreviewed change to the target."""
    run_id = arrange_rollback(workspace, monkeypatch, verify="python3 -c pass")

    output = invoke(cli, workspace, "rollback", run_id)

    assert (workspace / "target" / "demo" / "main.py").read_text() == "original\n"
    assert "restored and verified" in output

    with store.Store().session() as session:
        restored = session.get(Run, run_id)
        assert restored.status is RunStatus.ROLLED_BACK
        assert "abc123" in restored.stop_reason


def test_a_restore_that_fails_verification_says_so(cli, workspace, monkeypatch):
    """Restoring is not the same as being back in a known-good state."""
    run_id = arrange_rollback(workspace, monkeypatch, verify="python3 -c exit(1)")

    result = cli.invoke(app, ["rollback", run_id])

    assert result.exit_code == 1
    assert "verification failed" in result.output


def test_a_plan_with_no_restore_point_refuses_to_roll_back(cli, workspace):
    """Greenfield has nothing to return to, and should say that rather than
    inventing a definition of 'original'."""
    start(cli, workspace)
    result = cli.invoke(app, ["rollback"])

    assert result.exit_code == 1
    assert "declares no rollback" in result.output


# --------------------------------------------------------------------------- #
# escalation — approving one has to do something to the node it was raised for
# --------------------------------------------------------------------------- #

BROKEN = """
plan: demo
version: 1
nodes:
  - id: build
    kind: tool
    stage: implementation
    run: sh:make
    gate: {all: ["coverage.percent >= 80"]}
  - id: ship
    kind: tool
    stage: release
    needs: [build]
    run: sh:true
"""


def escalated(cli, workspace) -> str:
    """A run stopped at an escalation raised by an unperformable check."""
    (workspace / "plans" / "demo.yaml").write_text(textwrap.dedent(BROKEN))
    invoke(
        cli, workspace,
        "run",
        "--plan", str(workspace / "plans" / "demo.yaml"),
        "--requirement", str(workspace / "requirement.md"),
        "--target", str(workspace / "target.yaml"),
    )
    with store.Store().session() as session:
        run = session.scalars(select(Run)).all()[-1]
        return run.id


def test_approving_an_error_escalation_re_enters_the_node(cli, workspace):
    """The live run that found this: `scaffold` ERRORed on a missing input, and
    approving the escalation left it errored forever with the run deadlocked."""
    run_id = escalated(cli, workspace)

    with store.Store().session() as session:
        run = session.get(Run, run_id)
        node = next(n for n in store.all_nodes(session, run) if n.node_id.startswith("escalate:"))
        assert store.get_node(session, run, "build").status is NodeStatus.ERRORED

    output = invoke(cli, workspace, "approve", run_id, node.node_id, "--by", "ops", "--no-resume")

    assert "re-entering" in output           # rich colours the node id mid-word
    with store.Store().session() as session:
        run = session.get(Run, run_id)
        assert store.get_node(session, run, "build").status is NodeStatus.PENDING
