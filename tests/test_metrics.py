"""Metrics tests.

Three properties do the work here, and each exists because the obvious
implementation would have flattered the system:

- MTTR excludes incidents that never recovered, and reports them separately
- rates return None over an empty set rather than 0.0
- human wait time is separated from end-to-end latency
"""

from __future__ import annotations

from datetime import timedelta

import pytest

from orchestrator.lineage import recorder
from orchestrator.metrics import fleet_metrics, run_metrics
from orchestrator.state import store
from orchestrator.state.models import Decision, NodeStatus, RunStatus, utcnow

NODES = [
    ("design", "agent", "design"),
    ("impl", "codeagent", "implementation"),
    ("tests", "tool", "verification"),
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
        requirement_path="r.md",
        target_profile="t.yaml",
        nodes=NODES,
    )


def attempt(session, run, node_id, *, verdict="pass", elapsed_s=1, error=None):
    """Record one attempt with a gate verdict, as the scheduler would."""
    node = store.get_node(session, run, node_id)
    started = utcnow()
    a = store.begin_attempt(session, node, worker="stub")
    store.record_gate(session, a, verdict=verdict, evaluator="orchestrator.gates", checks=[])
    store.finish_attempt(
        session,
        a,
        status=NodeStatus.PASSED if verdict == "pass" else NodeStatus.FAILED,
        error=error,
    )
    a.started_at = started
    a.finished_at = started + timedelta(seconds=elapsed_s)
    session.flush()
    return a


# --------------------------------------------------------------------------- #
# rates
# --------------------------------------------------------------------------- #


def test_success_rate_counts_first_attempt_passes(session, run):
    attempt(session, run, "design")
    attempt(session, run, "impl")
    attempt(session, run, "tests", verdict="fail")

    metrics = run_metrics(session, run)
    assert metrics.nodes_executed == 3
    assert metrics.first_attempt_passes == 2
    assert metrics.success_rate == pytest.approx(2 / 3)


def test_a_node_that_passes_on_retry_does_not_count_as_a_first_attempt_pass(session, run):
    attempt(session, run, "tests", verdict="fail")
    attempt(session, run, "tests", verdict="pass")

    metrics = run_metrics(session, run)
    assert metrics.first_attempt_passes == 0
    assert metrics.retries == 1
    assert metrics.retry_rate == 1.0


def test_rates_are_undefined_rather_than_zero_when_nothing_ran(session, run):
    """0.0 would read as total failure; nothing ran is not the same as nothing worked."""
    metrics = run_metrics(session, run)
    assert metrics.nodes_executed == 0
    assert metrics.success_rate is None
    assert metrics.retry_rate is None


def test_the_basis_travels_with_the_rate(session, run):
    """A rate quoted without its denominator is how '100% success' means 'one node'."""
    attempt(session, run, "design")
    assert run_metrics(session, run).summary()["success_rate_basis"] == "1/1"


# --------------------------------------------------------------------------- #
# MTTR
# --------------------------------------------------------------------------- #


def test_an_incident_spans_the_failure_to_the_recovery(session, run):
    attempt(session, run, "tests", verdict="fail", elapsed_s=1)
    attempt(session, run, "tests", verdict="pass", elapsed_s=1)

    metrics = run_metrics(session, run)
    assert len(metrics.incidents) == 1
    incident = metrics.incidents[0]
    assert incident.failed_at_attempt == 1
    assert incident.recovered_at_attempt == 2
    assert incident.recovery_ms is not None and incident.recovery_ms > 0


def test_mttr_excludes_incidents_that_never_recovered(session, run):
    """Otherwise MTTR would improve as reliability got worse — the worst
    incidents would simply stop counting."""
    attempt(session, run, "tests", verdict="fail")
    attempt(session, run, "tests", verdict="pass")   # recovered
    attempt(session, run, "impl", verdict="fail")    # never recovered

    metrics = run_metrics(session, run)
    assert len(metrics.incidents) == 2
    assert len(metrics.recovered_incidents) == 1
    assert len(metrics.unrecovered_incidents) == 1
    assert metrics.mttr_ms is not None


def test_mttr_is_undefined_when_nothing_recovered(session, run):
    attempt(session, run, "tests", verdict="fail")
    metrics = run_metrics(session, run)

    assert metrics.mttr_ms is None
    assert len(metrics.unrecovered_incidents) == 1


def test_an_errored_gate_counts_as_an_incident(session, run):
    """ERROR blocks the run just as FAIL does; leaving it out would undercount."""
    attempt(session, run, "tests", verdict="error")
    assert len(run_metrics(session, run).incidents) == 1


def test_a_clean_run_has_no_incidents(session, run):
    attempt(session, run, "design")
    attempt(session, run, "tests")
    assert run_metrics(session, run).incidents == []


# --------------------------------------------------------------------------- #
# latency, and what is not the system's fault
# --------------------------------------------------------------------------- #


def test_human_wait_time_is_separated_from_elapsed(session, run):
    """A run blocked three hours on an approval is not a slow system."""
    approval = recorder.request_approval(session, run, node_id="design-approval", artifacts=[])
    approval.requested_at = utcnow() - timedelta(seconds=120)
    recorder.decide(session, approval, decision=Decision.APPROVED, decided_by="alice")

    run.started_at = utcnow() - timedelta(seconds=180)
    store.finish_run(session, run, status=RunStatus.COMPLETED)

    metrics = run_metrics(session, run)
    assert metrics.human_wait_ms >= 119_000
    assert metrics.elapsed_ms >= metrics.human_wait_ms
    assert metrics.system_ms < metrics.elapsed_ms


def test_a_pending_approval_contributes_no_wait_time(session, run):
    """Guessing a duration would make a blocked run look slow rather than blocked."""
    recorder.request_approval(session, run, node_id="design-approval", artifacts=[])
    metrics = run_metrics(session, run)

    assert metrics.human_wait_ms == 0
    assert metrics.pending_approvals == 1


def test_an_unfinished_run_has_no_elapsed_time(session, run):
    assert run_metrics(session, run).elapsed_ms is None


# --------------------------------------------------------------------------- #
# by stage
# --------------------------------------------------------------------------- #


def test_metrics_group_by_lifecycle_stage(session, run):
    """Where retries concentrate is more useful than a run-wide rate."""
    attempt(session, run, "design")
    attempt(session, run, "tests", verdict="fail")
    attempt(session, run, "tests", verdict="pass")

    by_stage = run_metrics(session, run).by_stage
    assert by_stage["design"].retries == 0
    assert by_stage["verification"].retries == 1
    assert by_stage["verification"].incidents == 1
    assert by_stage["design"].success_rate == 1.0


# --------------------------------------------------------------------------- #
# across runs
# --------------------------------------------------------------------------- #


def test_rollback_frequency_is_a_fleet_measure(session, run):
    """It means nothing within a single run."""
    store.finish_run(session, run, status=RunStatus.ROLLED_BACK, stop_reason="regression")
    other = store.start_run(
        session,
        plan_name="greenfield",
        plan_version=1,
        requirement_path="r.md",
        target_profile="t.yaml",
        nodes=NODES,
    )
    store.finish_run(session, other, status=RunStatus.COMPLETED)

    fleet = fleet_metrics(session)
    assert fleet.runs == 2
    assert fleet.rollback_rate == 0.5
    assert fleet.completion_rate == 0.5


def test_three_runs_are_not_a_statistic(session, run):
    """Making the sample size a property means a caller has to actively ignore it."""
    store.finish_run(session, run, status=RunStatus.COMPLETED)
    assert not fleet_metrics(session).is_statistically_meaningful
