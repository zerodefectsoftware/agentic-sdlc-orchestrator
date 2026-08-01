"""Run state and lineage tests.

The centrepiece is stale-approval detection. "Approval of a superseded artifact
is not approval" is the strongest governance control in the design and the one
most systems get wrong, because they store approval as a boolean and so cannot
answer the question at all.
"""

from __future__ import annotations

import pytest

from orchestrator.lineage import query, recorder
from orchestrator.state import store
from orchestrator.state.models import Decision, NodeStatus, RunStatus, content_hash

PLAN_NODES = [
    ("intake", "agent"),
    ("design", "agent"),
    ("design-approval", "human"),
    ("impl", "fanout"),
    ("tests", "tool"),
]


@pytest.fixture
def db():
    return store.Store.in_memory()


@pytest.fixture
def session(db):
    with db.session() as session:
        yield session


@pytest.fixture
def run(session):
    return store.start_run(
        session,
        plan_name="greenfield",
        plan_version=1,
        requirement_path="requirements/greenfield.md",
        target_profile="config/target.shortener.yaml",
        node_ids=PLAN_NODES,
    )


# --------------------------------------------------------------------------- #
# run lifecycle
# --------------------------------------------------------------------------- #


def test_starting_a_run_materialises_the_plan_shape(session, run):
    assert run.status is RunStatus.RUNNING
    assert {node.node_id for node in run.nodes} == {n for n, _ in PLAN_NODES}
    assert all(node.status is NodeStatus.PENDING for node in run.nodes)


def test_attempts_accumulate_rather_than_overwrite(session, run):
    """Retry frequency and MTTR are computed from the sequence (§8)."""
    node = store.get_node(session, run, "tests")

    first = store.begin_attempt(session, node, worker="stub")
    store.finish_attempt(session, first, status=NodeStatus.FAILED, error="2 tests failed")
    second = store.begin_attempt(session, node, worker="stub")
    store.finish_attempt(session, second, status=NodeStatus.PASSED)

    assert [a.number for a in node.attempts] == [1, 2]
    assert node.attempts[0].error == "2 tests failed"
    assert node.status is NodeStatus.PASSED


def test_inserted_nodes_are_distinguishable_from_planned_ones(session, run):
    """The bundle should show which parts of a run were planned and which were a response."""
    fix = store.insert_node(session, run, "fix:tests", "codeagent")
    assert fix.inserted
    assert not store.get_node(session, run, "tests").inserted


def test_gate_verdicts_are_recorded_as_reached(session, run):
    node = store.get_node(session, run, "tests")
    attempt = store.begin_attempt(session, node, worker="stub")

    record = store.record_gate(
        session,
        attempt,
        verdict="fail",
        evaluator="orchestrator.gates",
        checks=[{"check": "pytest.exit_code == 0", "verdict": "fail", "observed": "1"}],
    )

    assert record.checks[0]["observed"] == "1"
    assert record.evaluated_at is not None


def test_nonterminal_nodes_block_release_readiness(session, run):
    """Feeds G10 — a bundle assembled mid-flight documents an incomplete run."""
    for node in run.nodes:
        node.status = NodeStatus.PASSED
    session.flush()
    assert store.nodes_in_nonterminal_state(session, run) == []

    store.get_node(session, run, "tests").status = NodeStatus.BLOCKED
    session.flush()
    assert [n.node_id for n in store.nodes_in_nonterminal_state(session, run)] == ["tests"]


def test_safe_stop_preserves_state(session, run):
    """A run you cannot resume is not safely stoppable."""
    store.finish_run(session, run, status=RunStatus.STOPPED, stop_reason="red baseline")
    assert run.status is RunStatus.STOPPED
    assert run.stop_reason == "red baseline"
    assert run.finished_at is not None
    assert len(run.nodes) == len(PLAN_NODES)  # nothing discarded


# --------------------------------------------------------------------------- #
# artifacts are versioned, never updated
# --------------------------------------------------------------------------- #


def test_re_deriving_an_artifact_creates_a_new_version(session, run):
    v1 = recorder.record_artifact(session, run, name="design.openapi", content="paths: {}")
    v2 = recorder.record_artifact(session, run, name="design.openapi", content="paths: {/x}")

    assert (v1.version, v2.version) == (1, 2)
    assert recorder.latest(session, run, "design.openapi").id == v2.id
    assert v1.content_hash != v2.content_hash


def test_identical_content_still_creates_a_version(session, run):
    """Re-running is an event worth recording even when the bytes are unchanged."""
    same = "paths: {}"
    v1 = recorder.record_artifact(session, run, name="a", content=same)
    v2 = recorder.record_artifact(session, run, name="a", content=same)

    assert v2.version == 2
    assert v1.content_hash == v2.content_hash == content_hash(same)


def test_artifact_history_is_ordered(session, run):
    for body in ("one", "two", "three"):
        recorder.record_artifact(session, run, name="doc", content=body)
    history = query.artifact_history(session, run, "doc")
    assert [a.version for a in history] == [1, 2, 3]


# --------------------------------------------------------------------------- #
# D10: approval of a superseded artifact is not approval
# --------------------------------------------------------------------------- #


def test_approval_is_not_stale_while_its_artifact_is_current(session, run):
    openapi = recorder.record_artifact(session, run, name="design.openapi", content="v1")
    approval = recorder.request_approval(
        session, run, node_id="design-approval", artifacts=[openapi]
    )
    recorder.decide(session, approval, decision=Decision.APPROVED, decided_by="alice")

    assert query.stale_approvals(session, run) == []


def test_approval_goes_stale_when_its_artifact_is_re_derived(session, run):
    """The security→design re-plan (§6) lands here: the human approved a document
    that no longer exists."""
    v1 = recorder.record_artifact(session, run, name="design.openapi", content="v1")
    approval = recorder.request_approval(
        session, run, node_id="design-approval", artifacts=[v1]
    )
    recorder.decide(session, approval, decision=Decision.APPROVED, decided_by="alice")

    recorder.record_artifact(session, run, name="design.openapi", content="v2")

    stale = query.stale_approvals(session, run)
    assert len(stale) == 1
    assert (stale[0].approved_version, stale[0].current_version) == (1, 2)
    assert "v2 now exists" in str(stale[0])


def test_pending_approvals_are_not_reported_as_stale(session, run):
    v1 = recorder.record_artifact(session, run, name="design.openapi", content="v1")
    recorder.request_approval(session, run, node_id="design-approval", artifacts=[v1])
    recorder.record_artifact(session, run, name="design.openapi", content="v2")

    assert query.stale_approvals(session, run) == []  # never approved in the first place


def test_reverting_keeps_the_original_decision_in_the_audit_trail(session, run):
    """Not a deletion: 'approved, then the thing it approved was replaced' is the
    sequence a reviewer needs to see."""
    v1 = recorder.record_artifact(session, run, name="design.openapi", content="v1")
    approval = recorder.request_approval(
        session, run, node_id="design-approval", artifacts=[v1]
    )
    recorder.decide(session, approval, decision=Decision.APPROVED, decided_by="alice")
    recorder.record_artifact(session, run, name="design.openapi", content="v2")

    recorder.revert_to_pending(session, approval, "design re-derived after security finding")

    assert approval.decision is Decision.PENDING
    assert approval.decided_by == "alice"          # who decided is retained
    assert "security finding" in approval.note
    assert query.stale_approvals(session, run) == []  # no longer an approval to be stale


def test_an_approval_bound_to_several_artifacts_goes_stale_if_any_moves(session, run):
    openapi = recorder.record_artifact(session, run, name="design.openapi", content="a")
    decisions = recorder.record_artifact(session, run, name="design.decisions", content="b")
    approval = recorder.request_approval(
        session, run, node_id="design-approval", artifacts=[openapi, decisions]
    )
    recorder.decide(session, approval, decision=Decision.APPROVED, decided_by="alice")

    recorder.record_artifact(session, run, name="design.decisions", content="b-revised")

    stale = query.stale_approvals(session, run)
    assert [s.artifact_name for s in stale] == ["design.decisions"]


# --------------------------------------------------------------------------- #
# why does this exist?
# --------------------------------------------------------------------------- #


def test_provenance_records_producer_inputs_and_model(session, run):
    intake = store.get_node(session, run, "intake")
    intake_attempt = store.begin_attempt(session, intake, worker="live", model="claude-opus-5")
    requirements = recorder.record_artifact(
        session, run, name="requirements", content="R1", produced_by=intake_attempt
    )

    design = store.get_node(session, run, "design")
    design_attempt = store.begin_attempt(session, design, worker="live", model="claude-opus-5")
    recorder.record_inputs(session, design_attempt, [requirements])
    openapi = recorder.record_artifact(
        session, run, name="design.openapi", content="paths", produced_by=design_attempt
    )

    step = query.provenance(session, openapi)
    assert step.produced_by_node == "design"
    assert step.model == "claude-opus-5"
    assert [a.name for a in step.inputs] == ["requirements"]


def test_why_walks_back_to_the_origin(session, run):
    """The brownfield question — 'why is this a 301?' — is this traversal."""
    intake = store.get_node(session, run, "intake")
    a1 = store.begin_attempt(session, intake, worker="stub")
    requirements = recorder.record_artifact(
        session, run, name="requirements", content="R1", produced_by=a1
    )

    design = store.get_node(session, run, "design")
    a2 = store.begin_attempt(session, design, worker="stub")
    recorder.record_inputs(session, a2, [requirements])
    decisions = recorder.record_artifact(
        session, run, name="design.decisions", content="D1: use 301", produced_by=a2
    )

    chain = query.why(session, decisions)
    assert [step.artifact.name for step in chain] == ["design.decisions", "requirements"]


def test_why_terminates_on_a_cycle(session, run):
    """Lineage should be acyclic; a traversal that hangs on bad data is worse than
    one that stops."""
    node = store.get_node(session, run, "design")
    attempt = store.begin_attempt(session, node, worker="stub")
    artifact = recorder.record_artifact(
        session, run, name="self", content="x", produced_by=attempt
    )
    recorder.record_inputs(session, attempt, [artifact])  # consumes what it produced

    assert len(query.why(session, artifact)) == 1


def test_unproduced_artifacts_are_reportable(session, run):
    """Feeds G10's lineage_complete: an artifact nobody can account for."""
    node = store.get_node(session, run, "intake")
    attempt = store.begin_attempt(session, node, worker="stub")
    recorder.record_artifact(session, run, name="tracked", content="a", produced_by=attempt)
    recorder.record_artifact(session, run, name="orphan", content="b")

    assert [a.name for a in query.unproduced_artifacts(session, run)] == ["orphan"]


def test_attempts_for_a_run_are_ordered(session, run):
    for node_id in ("intake", "design", "tests"):
        store.begin_attempt(session, store.get_node(session, run, node_id), worker="stub")
    attempts = query.attempts_for(session, run)
    assert len(attempts) == 3


# --------------------------------------------------------------------------- #
# persistence
# --------------------------------------------------------------------------- #


def test_state_survives_the_process(tmp_path):
    """Safe-stop and resume depend on durability, so this uses a real file.

    An in-memory store would pass by accident — SQLAlchemy reuses the connection —
    and prove nothing about surviving a stopped process.
    """
    db_path = tmp_path / "runs.db"
    db = store.Store(f"sqlite:///{db_path}")

    with db.session() as session:
        run = store.start_run(
            session,
            plan_name="greenfield",
            plan_version=1,
            requirement_path="requirements/greenfield.md",
            target_profile="config/target.shortener.yaml",
            node_ids=PLAN_NODES,
        )
        run_id = run.id
        store.finish_run(session, run, status=RunStatus.STOPPED, stop_reason="safe stop")

    # A fresh Store against the same file — as a resumed process would open it.
    reopened = store.Store(f"sqlite:///{db_path}")
    with reopened.session() as session:
        from orchestrator.state.models import Run

        reloaded = session.get(Run, run_id)
        assert reloaded.status is RunStatus.STOPPED
        assert reloaded.stop_reason == "safe stop"
        assert len(reloaded.nodes) == len(PLAN_NODES)
