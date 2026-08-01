"""Evidence bundle tests.

The property that matters: the bundle can never look healthier than the run it
describes. Everything else is presentation.
"""

from __future__ import annotations

import json
import textwrap
from pathlib import Path

import pytest

from orchestrator.engine.loader import load_plan
from orchestrator.engine.scheduler import Scheduler
from orchestrator.evidence import assemble, render
from orchestrator.lineage import recorder
from orchestrator.state import store
from orchestrator.state.artifacts import ArtifactStore
from orchestrator.state.models import Decision, NodeStatus, RunStatus
from orchestrator.workers import StubWorker
from orchestrator.workers import stub as scripts

PLAN = """
plan: t
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
  - id: verify
    kind: tool
    stage: verification
    needs: [design-approval]
    run: sh:pytest
    retry_budget: 1
    gate: {all: ["pytest.exit_code == 0"]}
"""


@pytest.fixture
def session():
    with store.Store.in_memory().session() as session:
        yield session


@pytest.fixture
def plan(tmp_path):
    path = tmp_path / "plan.yaml"
    path.write_text(textwrap.dedent(PLAN))
    return load_plan(path)


def run_to_approval(session, plan, tmp_path, worker=None):
    worker = worker or StubWorker(
        default=scripts.passing("pytest", **{"design.openapi": "paths: {}"})
    )
    scheduler = Scheduler(plan, worker, artifacts=ArtifactStore(tmp_path / "runs"))
    run = scheduler.start(session, requirement_path="requirements/x.md", target_profile="t.yaml")
    return scheduler, scheduler.advance(session, run)


# --------------------------------------------------------------------------- #
# collection
# --------------------------------------------------------------------------- #


def test_the_bundle_describes_the_run(session, plan, tmp_path):
    _, run = run_to_approval(session, plan, tmp_path)
    bundle = assemble(session, run)

    assert bundle.run_id == run.id
    assert bundle.plan == "t"
    assert bundle.requirement_path == "requirements/x.md"
    assert {node.node_id for node in bundle.nodes} == {"design", "design-approval", "verify"}


def test_gate_verdicts_are_read_back_not_recomputed(session, plan, tmp_path):
    """A matrix recomputed at assembly time could differ from the one that gated."""
    worker = StubWorker(
        {
            "design": scripts.passing("agent", **{"design.openapi": "paths: {}"}),
            "verify": scripts.failing(),
        }
    )
    scheduler, run = run_to_approval(session, plan, tmp_path, worker)

    approval = run.approvals[0]
    recorder.decide(session, approval, decision=Decision.APPROVED, decided_by="alice")
    store.get_node(session, run, "design-approval").status = NodeStatus.PASSED
    run.status = RunStatus.RUNNING
    scheduler.advance(session, run)

    bundle = assemble(session, run)
    blocking = bundle.blocking_gates
    assert blocking
    assert blocking[0].node_id == "verify"
    assert blocking[0].evaluator == "orchestrator.gates"
    assert "pytest.exit_code == 0" in blocking[0].failures[0].check
    assert "1" in blocking[0].failures[0].observed


def test_approvals_carry_the_versions_they_covered(session, plan, tmp_path):
    _, run = run_to_approval(session, plan, tmp_path)
    recorder.decide(
        session, run.approvals[0], decision=Decision.APPROVED, decided_by="alice"
    )

    bundle = assemble(session, run)
    approval = bundle.approvals[0]
    assert approval.decided_by == "alice"
    assert approval.covers == ["design.openapi@v1"]
    assert not approval.stale


def test_a_stale_approval_is_flagged(session, plan, tmp_path):
    """D10 reaching the bundle: the human approved something that no longer exists."""
    _, run = run_to_approval(session, plan, tmp_path)
    recorder.decide(
        session, run.approvals[0], decision=Decision.APPROVED, decided_by="alice"
    )
    recorder.record_artifact(session, run, name="design.openapi", content="paths: {/x}")

    bundle = assemble(session, run)
    assert bundle.stale_approvals
    assert not bundle.is_releasable


def test_artifacts_record_what_produced_them(session, plan, tmp_path):
    _, run = run_to_approval(session, plan, tmp_path)
    bundle = assemble(session, run)

    openapi = next(a for a in bundle.artifacts if a.name == "design.openapi")
    assert openapi.produced_by_node == "design"
    assert not openapi.orphaned


def test_an_unattributed_artifact_is_visible(session, plan, tmp_path):
    """`lineage_complete` blocks on this; the bundle should show it, not hide it."""
    _, run = run_to_approval(session, plan, tmp_path)
    recorder.record_artifact(session, run, name="mystery", content="{}")

    bundle = assemble(session, run)
    assert any(a.orphaned for a in bundle.artifacts)
    assert not bundle.is_releasable


def test_inserted_nodes_are_distinguishable_from_planned_work(session, plan, tmp_path):
    worker = StubWorker(
        {"design": scripts.passing("agent", **{"design.openapi": "x"}), "verify": scripts.failing()}
    )
    scheduler, run = run_to_approval(session, plan, tmp_path, worker)
    recorder.decide(session, run.approvals[0], decision=Decision.APPROVED, decided_by="a")
    store.get_node(session, run, "design-approval").status = NodeStatus.PASSED
    run.status = RunStatus.RUNNING
    scheduler.advance(session, run)

    bundle = assemble(session, run)
    assert any(node.node_id.startswith("escalate:") for node in bundle.inserted_nodes)


def test_counts_summarise_without_inventing(session, plan, tmp_path):
    _, run = run_to_approval(session, plan, tmp_path)
    counts = assemble(session, run).counts
    assert counts["nodes"] == 3
    assert counts["attempts"] >= 1
    assert counts["approvals"] == 1


# --------------------------------------------------------------------------- #
# the bundle cannot look healthier than the run
# --------------------------------------------------------------------------- #


def test_a_blocked_run_is_not_releasable(session, plan, tmp_path):
    _, run = run_to_approval(session, plan, tmp_path)
    assert run.status is RunStatus.BLOCKED
    assert not assemble(session, run).is_releasable


def test_releasable_requires_a_completed_run_with_nothing_outstanding(session, plan, tmp_path):
    _, run = run_to_approval(session, plan, tmp_path)
    recorder.decide(session, run.approvals[0], decision=Decision.APPROVED, decided_by="alice")
    run.status = RunStatus.COMPLETED
    session.flush()

    bundle = assemble(session, run)
    assert bundle.is_releasable


# --------------------------------------------------------------------------- #
# rendering
# --------------------------------------------------------------------------- #


def test_markdown_leads_with_the_verdict(session, plan, tmp_path):
    """A bundle that has to be read chronologically to find the problem is a log."""
    _, run = run_to_approval(session, plan, tmp_path)
    text = render.render_markdown(assemble(session, run))

    assert text.index("## Verdict") < text.index("## Lifecycle")
    assert "NOT RELEASABLE" in text


def test_markdown_names_what_blocked(session, plan, tmp_path):
    worker = StubWorker(
        {"design": scripts.passing("agent", **{"design.openapi": "x"}), "verify": scripts.failing()}
    )
    scheduler, run = run_to_approval(session, plan, tmp_path, worker)
    recorder.decide(session, run.approvals[0], decision=Decision.APPROVED, decided_by="a")
    store.get_node(session, run, "design-approval").status = NodeStatus.PASSED
    run.status = RunStatus.RUNNING
    scheduler.advance(session, run)

    text = render.render_markdown(assemble(session, run))
    assert "## What blocked" in text
    assert "pytest.exit_code == 0" in text


def test_markdown_flags_a_stale_approval(session, plan, tmp_path):
    _, run = run_to_approval(session, plan, tmp_path)
    recorder.decide(session, run.approvals[0], decision=Decision.APPROVED, decided_by="alice")
    recorder.record_artifact(session, run, name="design.openapi", content="changed")

    text = render.render_markdown(assemble(session, run))
    assert "stale" in text
    assert "D10" in text


def test_json_round_trips(session, plan, tmp_path):
    _, run = run_to_approval(session, plan, tmp_path)
    payload = json.loads(render.render_json(assemble(session, run)))

    assert payload["run_id"] == run.id
    assert payload["releasable"] is False
    assert payload["counts"]["nodes"] == 3


def test_write_produces_both_projections(session, plan, tmp_path):
    _, run = run_to_approval(session, plan, tmp_path)
    paths = render.write(assemble(session, run), root=tmp_path / "runs")

    assert paths["markdown"].exists()
    assert paths["json"].exists()
    assert paths["markdown"].parent.name == "evidence"
    assert Path(paths["markdown"]).read_text().startswith("# Evidence bundle")
