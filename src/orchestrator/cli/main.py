"""The command surface.

This is the composition root: the only place that reads settings, builds a
store, chooses a worker, and wires a scheduler. Everything below it takes its
collaborators as arguments, which is why the rest of the system is testable
without an environment.

The interaction model is **stop, don't wait** (§6). A run that reaches a human
checkpoint persists and this process exits; `approve` starts a new one that
reloads state and continues. A blocked run holding a terminal open for three
hours is not a checkpoint.
"""

from __future__ import annotations

import json
import shlex
import subprocess
import time
from pathlib import Path
from typing import Annotated

import networkx as nx
import typer
from rich.console import Console
from rich.table import Table
from sqlalchemy import select

from orchestrator.artifacts import Baseline
from orchestrator.config import MissingCredential, WorkerMode, get_settings
from orchestrator.engine.loader import (
    PlanError,
    dependency_graph,
    execution_order,
    load_plan,
)
from orchestrator.engine.profile import ProfileError, TargetProfile
from orchestrator.engine.scheduler import Scheduler, SchedulerError, verify_probes
from orchestrator.evidence import assemble, render
from orchestrator.gates import Verdict
from orchestrator.gates.predicates import register_all
from orchestrator.gates.registry import PredicateRegistry
from orchestrator.lineage import query, recorder
from orchestrator.metrics import fleet_metrics, run_metrics
from orchestrator.state import store
from orchestrator.state.artifacts import ArtifactStore
from orchestrator.state.models import Decision, NodeStatus, Run, RunStatus
from orchestrator.workers import LiveWorker, ReplayWorker, StubWorker, WorkScope
from orchestrator.workers import stub as scripts

app = typer.Typer(
    add_completion=False,
    help="A governed SDLC orchestrator. Requirement in, reviewable change set out.",
)
console = Console()

STATUS_STYLE = {
    "passed": "green",
    "failed": "red",
    "errored": "yellow",
    "blocked": "cyan",
    "stale": "magenta",
    "pending": "dim",
    "running": "blue",
    "skipped": "dim",
}


# --------------------------------------------------------------------------- #
# composition
# --------------------------------------------------------------------------- #


def _worker():
    """Pick the worker the environment asked for.

    `live` is refused rather than silently downgraded — a run that quietly used
    stubs when the operator asked for real models would produce evidence that
    describes work nobody did.
    """
    settings = get_settings()
    match settings.worker:
        case WorkerMode.REPLAY:
            return ReplayWorker()
        case WorkerMode.LIVE:
            try:
                settings.require_api_key()
            except MissingCredential as exc:
                # The message already names the way out; a traceback would bury it.
                console.print(f"[red]{exc}[/red]")
                raise typer.Exit(1) from exc
            return LiveWorker()
        case _:
            return StubWorker(default=scripts.declared_outputs())


def _registry() -> PredicateRegistry:
    return register_all(PredicateRegistry())


def _decide_here(session, run: Run, pending: list, by: str | None) -> None:
    """Answer a checkpoint from the terminal that is already watching the run.

    `watch` can only tell you a decision is needed; answering it meant switching
    terminals, finding the run id, and typing a command — which makes a human
    checkpoint feel like the run died rather than like it asked you something.

    Deliberately additive: the run is still stopped and persisted before this
    prompt appears (§stop, don't wait), and every decision goes through the same
    recorded path as `approve`. Nothing here is a shortcut around governance —
    it is the same question, asked where you are standing.
    """
    if not by:
        console.print("[red]--decide needs --by: a decision with no decider is not a record[/red]")
        raise typer.Exit(1)

    for approval in pending:
        console.print(f"\n[bold cyan]decision needed[/bold cyan] on {approval.node_id}")
        for binding in approval.bindings:
            console.print(f"  covers [bold]{binding.artifact.ref}[/bold]")

        answer = typer.prompt("  [a]pprove / [r]eject / [s]kip", default="s").strip().lower()
        if answer.startswith("s"):
            console.print("[dim]  left open — the run stays blocked[/dim]")
            continue

        note = typer.prompt("  note", default="")
        recorder.decide(
            session,
            approval,
            decision=Decision.APPROVED if answer.startswith("a") else Decision.REJECTED,
            decided_by=by,
            note=note or None,
        )
        console.print(f"[green]  recorded[/green] by {by}")

    session.commit()
    console.print("\n[dim]resume with: orchestrator resume " + run.id + "[/dim]")


def _doing(session, run: Run) -> str:
    """What a run is actually doing, for the heartbeat.

    "idle" has to mean *nothing is happening*, and it did not: a run waiting on
    a person reported idle, as did a fan-out reshaping the graph, because
    neither `human` nor `fanout` is ever marked RUNNING — correctly, since
    neither does work. But a watcher cannot tell a stalled run from one waiting
    on a decision only it can make, and that is the difference that matters when
    somebody is looking at the screen.
    """
    waiting = sorted(
        approval.node_id
        for approval in run.approvals
        if approval.decision is Decision.PENDING
    )
    if waiting:
        return f"blocked — awaiting your decision on {', '.join(waiting)}"

    busy = sorted(
        node.node_id
        for node in store.all_nodes(session, run)
        if node.status is NodeStatus.RUNNING
    )
    if busy:
        return f"{len(busy)} running: {', '.join(busy)}"

    if run.status is not RunStatus.RUNNING:
        return f"{run.status} — nothing further will happen without you"
    return "between waves"


def _retire_escalations(session, run: Run, node_id: str, by: str, why: str) -> None:
    """Close the handoffs a withdrawn node's failures opened.

    An escalation asks a person to decide about a verdict. Withdraw the verdict
    and the question is moot — but it is an *inserted* node, so it is not in the
    plan graph the cascade walks, and nothing was closing it. The run then
    blocked on a question about an attempt that no longer existed, before it
    could reach the node it had just been told to re-run.

    SKIPPED rather than STALE: STALE means "do this again", and re-asking a
    question whose subject was withdrawn is the one thing that must not happen.
    """
    for execution in store.all_nodes(session, run):
        if not execution.node_id.startswith(f"escalate:{node_id}#"):
            continue
        if execution.status is NodeStatus.SKIPPED:
            continue
        execution.status = NodeStatus.SKIPPED
        console.print(f"[dim]  retired[/dim] {execution.node_id} — its verdict was withdrawn")
        for approval in run.approvals:
            if approval.node_id == execution.node_id and approval.decision is Decision.PENDING:
                recorder.decide(
                    session,
                    approval,
                    decision=Decision.REJECTED,
                    decided_by=by,
                    note=f"moot: {node_id} withdrawn. {why}",
                )


def _latest_run(session) -> Run:
    run = session.scalar(select(Run).order_by(Run.started_at.desc()).limit(1))
    if run is None:
        raise typer.BadParameter("no runs recorded yet — start one with `orchestrator run`")
    return run


def _resolve(session, run_id: str | None) -> Run:
    if run_id is None:
        return _latest_run(session)
    run = session.get(Run, run_id)
    if run is None:
        raise typer.BadParameter(f"no run '{run_id}'")
    return run


def _show(run_id: str) -> None:
    """Report on a run in its own read-only session.

    Deliberately outside the transaction that did the work. Rendering can fail —
    a closed pipe raises BrokenPipeError mid-print — and holding the write
    transaction open across that would roll back an entire run because output
    failed. Work is committed first; display is a separate, disposable act.
    """
    with store.Store().session() as session:
        _report(session, session.get(Run, run_id))


def _report(session, run: Run) -> None:
    """Print where a run stands, and what to do next."""
    colour = {"completed": "green", "blocked": "cyan"}.get(str(run.status), "yellow")
    console.print(f"\n[bold]{run.plan_name}[/bold] · run [dim]{run.id}[/dim]")
    console.print(f"status: [{colour}]{run.status}[/{colour}]")
    if run.stop_reason:
        console.print(f"[dim]{run.stop_reason}[/dim]")

    waiting = [
        approval for approval in run.approvals if approval.decision is Decision.PENDING
    ]
    for approval in waiting:
        console.print(f"\n[cyan]awaiting decision[/cyan] on [bold]{approval.node_id}[/bold]")
        for binding in approval.bindings:
            console.print(f"  covers [bold]{binding.artifact.ref}[/bold]")
        console.print(
            f"\n  [dim]orchestrator approve {run.id} {approval.node_id} --by <you>[/dim]"
        )


# --------------------------------------------------------------------------- #
# commands
# --------------------------------------------------------------------------- #


@app.command()
def preflight(
    plan: Annotated[Path, typer.Option(help="Plan graph to validate")] = Path(
        "plans/greenfield.yaml"
    ),
    target: Annotated[Path, typer.Option(help="Target profile")] = Path(
        "config/target.shortener.yaml"
    ),
) -> None:
    """Validate a plan and confirm every check it names can be performed.

    A run should refuse to start when its plan names checks the engine cannot
    perform, rather than discovering that at the gate and having to decide what
    an unrunnable check means.
    """
    try:
        loaded = load_plan(plan, profile=_profile(target))
    except PlanError as exc:
        console.print(f"[red]invalid plan[/red] {exc}")
        raise typer.Exit(1) from exc

    missing = _registry().missing(loaded.required_predicates)
    console.print(f"[green]plan valid[/green] — {len(loaded.nodes)} nodes")
    console.print(f"stages: {', '.join(str(s) for s in loaded.stages_covered)}")
    if loaded.missing_stages:
        console.print(f"[yellow]stages with no node:[/yellow] {loaded.missing_stages}")
    console.print(f"predicates required: {len(loaded.required_predicates)}")

    unsatisfied = loaded.unsatisfied_params()
    if missing or unsatisfied:
        if missing:
            console.print(f"[red]not registered:[/red] {', '.join(missing)}")
        for problem in unsatisfied:
            console.print(f"[red]missing param:[/red] {problem}")
        raise typer.Exit(1)
    console.print("[green]every predicate is registered[/green]")
    console.print("[green]every check has the params it needs[/green]")


@app.command()
def run(
    plan: Annotated[Path, typer.Option(help="Plan graph")] = Path("plans/greenfield.yaml"),
    requirement: Annotated[Path, typer.Option(help="Prose requirement")] = Path(
        "requirements/greenfield.md"
    ),
    target: Annotated[Path, typer.Option(help="Target profile")] = Path(
        "config/target.shortener.yaml"
    ),
    dry_run: Annotated[
        bool, typer.Option("--dry-run", help="Show what each node would dispatch, and stop")
    ] = False,
) -> None:
    """Start a run and execute until it blocks, finishes, or stops."""
    try:
        loaded = load_plan(plan, profile=_profile(target))
    except PlanError as exc:
        console.print(f"[red]invalid plan[/red] {exc}")
        raise typer.Exit(1) from exc
    if dry_run:
        _dry_run(loaded, requirement)
        return

    missing = _registry().missing(loaded.required_predicates)
    if missing:
        console.print(f"[red]refusing to start[/red] — unregistered predicates: {missing}")
        raise typer.Exit(1)

    with store.Store().session() as session:
        scheduler = Scheduler(loaded, _worker(), registry=_registry(), artifacts=ArtifactStore())
        started = scheduler.start(
            session, requirement_path=str(requirement), target_profile=str(target)
        )
        run_id = scheduler.advance(session, started).id

    _show(run_id)


def _release_escalation(session, run, execution) -> None:
    """Put the escalated node back in play, according to why it escalated.

    Approving the checkpoint has to do something to the node it was raised for,
    or the run deadlocks the moment a human attends to it: the escalated node
    sits in its terminal failure and nothing downstream is ever satisfiable.

    After an ERROR the check could not be performed — the operator fixed the
    harness, so the node is re-entered and judged properly this time. After a
    FAIL the work was judged; approval is the human waiving past it (D15), so it
    is marked skipped and the graph moves on without pretending it passed.
    """
    params = (execution.config or {}).get("params") or {}
    escalated = params.get("escalated_node")
    if not escalated:
        return

    source = store.get_node(session, run, escalated)
    if source is None:
        return

    if source.status not in (NodeStatus.FAILED, NodeStatus.ERRORED):
        # The checkpoint outlived the problem: the node was retried and has
        # since passed. Releasing it now would demote work that is already
        # green — a stale approval must not be able to undo a real result.
        console.print(
            f"  [dim]nothing to release[/dim] — {escalated} is {source.status}"
        )
        return

    if params.get("escalated_for") == "error":
        source.status = NodeStatus.PENDING
        console.print(f"  [dim]re-entering[/dim] {escalated} — the check can now be performed")
    else:
        source.status = NodeStatus.SKIPPED
        console.print(f"  [dim]waived past[/dim] {escalated} by {run.approvals[-1].decided_by}")
    session.flush()


def _profile(path: Path) -> TargetProfile:
    """Load the target profile, or stop with a message rather than a traceback."""
    try:
        return TargetProfile.load(path)
    except ProfileError as exc:
        console.print(f"[red]invalid target profile[/red] {exc}")
        raise typer.Exit(1) from exc


def _dry_run(loaded, requirement: Path) -> None:
    """Show the configuration each node would dispatch with, and execute nothing.

    Every value comes from the same functions the live path uses, so this cannot
    describe a call the worker would not make. It needs no credential and no
    optional package, which is the point: the configuration is inspectable on a
    machine that cannot run it.
    """
    worker = LiveWorker()
    material = {"requirement": requirement.read_text() if requirement.exists() else ""}
    problems: list[str] = []

    console.print(f"\n[bold]{loaded.name}[/bold] · dry run · nothing will execute\n")

    for node_id in execution_order(loaded):
        node = loaded.node(node_id)
        detail = worker.describe(node, material, WorkScope.for_node(node))
        problems.extend(f"{node.id}: {issue}" for issue in detail.pop("issues", []))

        # The gate reads facts these produce, so an unroutable check is as fatal
        # as an unroutable node — and just as worth knowing before a live run.
        checks = []
        for probe in verify_probes(node):
            probe_detail = worker.describe(probe, material, WorkScope())
            problems.extend(f"{probe.id}: {issue}" for issue in probe_detail.get("issues", []))
            checks.append(probe.run)
        detail["verify"] = checks

        header = f"[bold]{node.id}[/bold]  [dim]{node.stage} · {node.kind}[/dim]"
        body = "\n".join(
            f"  {key.replace('_', ' '):<18} {_render(value)}"
            for key, value in detail.items()
            if value not in (None, [], "")
        )
        console.print(f"{header}\n{body}\n")

    if problems:
        console.print("[red]problems that would fail a live run:[/red]")
        for problem in problems:
            console.print(f"  - {problem}")
        raise typer.Exit(1)
    console.print("[green]every node has what it needs to dispatch[/green]")


def _render(value) -> str:
    if isinstance(value, list):
        return ", ".join(str(item) for item in value)
    return str(value)


@app.command()
def status(run_id: Annotated[str | None, typer.Argument()] = None) -> None:
    """Show where a run stands, node by node."""
    with store.Store().session() as session:
        target = _resolve(session, run_id)

        table = Table(title=f"{target.plan_name} · {target.id}")
        for column in ("stage", "node", "kind", "status", "attempts"):
            table.add_column(column)

        for node in store.all_nodes(session, target):
            marker = " [dim](inserted)[/dim]" if node.inserted else ""
            style = STATUS_STYLE.get(str(node.status), "white")
            table.add_row(
                node.stage,
                f"{node.node_id}{marker}",
                node.kind,
                f"[{style}]{node.status}[/{style}]",
                str(len(node.attempts)),
            )
        console.print(table)
        run_id = target.id

    _show(run_id)


@app.command()
def runs() -> None:
    """List recorded runs, newest first."""
    with store.Store().session() as session:
        table = Table(title="runs")
        for column in ("run", "plan", "status", "started"):
            table.add_column(column)
        for record in session.scalars(select(Run).order_by(Run.started_at.desc())):
            table.add_row(
                record.id[:12],
                record.plan_name,
                str(record.status),
                record.started_at.isoformat(timespec="seconds"),
            )
        console.print(table)


@app.command()
def approve(
    run_id: Annotated[str, typer.Argument()],
    node_id: Annotated[str, typer.Argument()],
    by: Annotated[str, typer.Option(help="Who is deciding — recorded in the audit trail")],
    note: Annotated[str | None, typer.Option()] = None,
    resume: Annotated[bool, typer.Option(help="Continue the run afterwards")] = True,
) -> None:
    """Approve a checkpoint, then continue.

    The decision is bound to the artifact versions it covered, so a later
    re-derivation makes it stale rather than silently keeping it valid (D10).
    """
    _decide(run_id, node_id, Decision.APPROVED, by, note, resume)


@app.command()
def reject(
    run_id: Annotated[str, typer.Argument()],
    node_id: Annotated[str, typer.Argument()],
    by: Annotated[str, typer.Option(help="Who is deciding")],
    note: Annotated[str, typer.Option(help="Why — recorded for the reviewer")],
) -> None:
    """Reject a checkpoint and stop the run."""
    _decide(run_id, node_id, Decision.REJECTED, by, note, resume=False)


def _decide(
    run_id: str,
    node_id: str,
    decision: Decision,
    by: str,
    note: str | None,
    resume: bool,
) -> None:
    with store.Store().session() as session:
        target = _resolve(session, run_id)
        approval = next(
            (
                a
                for a in target.approvals
                if a.node_id == node_id and a.decision is Decision.PENDING
            ),
            None,
        )
        if approval is None:
            raise typer.BadParameter(f"no decision pending on '{node_id}'")

        recorder.decide(session, approval, decision=decision, decided_by=by, note=note)
        execution = store.get_node(session, target, node_id)

        if decision is Decision.APPROVED:
            execution.status = NodeStatus.PASSED
            target.status = RunStatus.RUNNING
            target.stop_reason = None
            console.print(f"[green]approved[/green] {node_id} by {by}")
            _release_escalation(session, target, execution)
        else:
            execution.status = NodeStatus.FAILED
            store.finish_run(
                session, target, status=RunStatus.FAILED, stop_reason=f"{node_id} rejected: {note}"
            )
            console.print(f"[red]rejected[/red] {node_id} by {by}")
        session.flush()

    if decision is Decision.APPROVED and resume:
        _advance(run_id)


@app.command()
def rollback(
    run_id: Annotated[str | None, typer.Argument()] = None,
    by: Annotated[str, typer.Option(help="Who is deciding — recorded in the audit trail")] = "",
    dry_run: Annotated[
        bool, typer.Option("--dry-run", help="List what would be restored, and stop")
    ] = False,
) -> None:
    """Restore the target to the run's baseline, then verify the restore.

    Only meaningful for a plan that declares `rollback:` — greenfield has
    nothing to return to. The bodies come from the baseline artifact rather than
    from git, so this works on a dirty tree and needs no VCS.

    The restore is verified, not assumed: an unverified restore is a second
    unreviewed change to the target, made at the worst possible moment.
    """
    with store.Store().session() as session:
        target = _resolve(session, run_id)
        profile = _profile(Path(target.target_profile))
        loaded = load_plan(
            get_settings().plans_dir / f"{target.plan_name}.yaml", profile=profile
        )
        if loaded.rollback is None:
            console.print(
                f"[red]plan '{loaded.name}' declares no rollback[/red] — there is no "
                f"recorded state to return to"
            )
            raise typer.Exit(1)

        source = loaded.node(loaded.rollback.restore_from)
        name = f"{source.id}.{source.outputs[0]}" if source and source.outputs else None
        artifact = recorder.latest(session, target, name) if name else None
        if artifact is None:
            console.print(f"[red]no baseline recorded[/red] — '{name}' was never produced")
            raise typer.Exit(1)

        baseline = Baseline.model_validate_json(ArtifactStore().read(artifact))
        ceiling = tuple(p.rstrip("*").rstrip("/") for p in profile.write_ceiling)
        outside = sorted(path for path in baseline.files if not path.startswith(ceiling))
        if outside:
            # The snapshot is data like any other. Restoring it is still a write,
            # and the write ceiling does not stop applying because it is a repair.
            console.print(f"[red]refusing to restore outside {list(profile.write_ceiling)}[/red]")
            console.print(f"  {', '.join(outside[:5])}")
            raise typer.Exit(1)

        console.print(
            f"\n[bold]rollback[/bold] {target.id[:8]} → baseline {baseline.snapshot_ref} "
            f"({len(baseline.files)} files)"
        )
        if dry_run:
            for path in sorted(baseline.files):
                console.print(f"  [dim]would restore[/dim] {path}")
            return

        for path, body in baseline.files.items():
            destination = Path(path)
            destination.parent.mkdir(parents=True, exist_ok=True)
            destination.write_text(body)

        completed = subprocess.run(  # noqa: S603 — the command comes from the target profile
            shlex.split(loaded.rollback.verify_with),
            capture_output=True,
            text=True,
            check=False,
        )
        verified = completed.returncode == 0
        reason = (
            f"rolled back to {baseline.snapshot_ref}"
            f"{'' if verified else ' — VERIFICATION FAILED'}"
            f"{f' by {by}' if by else ''}"
        )
        store.finish_run(session, target, status=RunStatus.ROLLED_BACK, stop_reason=reason)
        run_identifier = target.id

    if verified:
        console.print(f"[green]restored and verified[/green] — {loaded.rollback.verify_with}")
    else:
        console.print(
            f"[red]restored, but verification failed[/red] "
            f"(exit {completed.returncode}) — the target is not in a known-good state"
        )
    _show(run_identifier)
    raise typer.Exit(0 if verified else 1)


@app.command()
def retry(
    run_id: Annotated[str, typer.Argument()],
    node_id: Annotated[str, typer.Argument()],
    by: Annotated[str, typer.Option(help="Who is asking — recorded in the audit trail")],
    why: Annotated[str, typer.Option(help="What changed since it failed")],
    resume: Annotated[bool, typer.Option(help="Continue the run afterwards")] = True,
) -> None:
    """Re-enter a node that failed or errored, after fixing what broke it.

    The verb the escalation checkpoint was missing. Approving an escalation
    means *accept the state and move on*; rejecting means *stop*. Neither says
    "the generator was wrong, I fixed it, run that node again" — which is what
    an operator actually does most of the time, and doing it by hand in the
    database is not an audit trail.

    Only a node in a terminal failure can be re-entered. A passing node is not
    retryable: re-running work that already satisfied its gate, to see whether
    it satisfies it again, is how a green run gets manufactured.
    """
    with store.Store().session() as target_run:
        run = _resolve(target_run, run_id)
        execution = store.get_node(target_run, run, node_id)
        if execution is None:
            raise typer.BadParameter(f"run has no node '{node_id}'")

        if execution.status not in (NodeStatus.FAILED, NodeStatus.ERRORED):
            console.print(
                f"[red]{node_id} is {execution.status}[/red] — only a failed or errored "
                f"node can be re-entered"
            )
            raise typer.Exit(1)

        # A retry is a human authorising work to be re-run, so it is recorded
        # where every other human decision is — the approval trail the evidence
        # bundle reads. Doing it by hand in the database would leave the run
        # green with no record of who reopened it.
        recorder.decide(
            target_run,
            recorder.request_approval(target_run, run, node_id=node_id, artifacts=[]),
            decision=Decision.APPROVED,
            decided_by=by,
            note=f"re-entered after failure: {why}",
        )
        execution.status = NodeStatus.PENDING
        run.status = RunStatus.RUNNING
        run.stop_reason = None
        target_run.flush()
        console.print(f"[green]re-entering[/green] {node_id} — {why} [dim](by {by})[/dim]")

    if resume:
        _advance(run_id)


@app.command()
def invalidate(
    run_id: Annotated[str, typer.Argument()],
    nodes: Annotated[list[str], typer.Argument(help="Nodes whose result is not to be trusted")],
    by: Annotated[str, typer.Option(help="Who is deciding — recorded in the audit trail")],
    why: Annotated[str, typer.Option(help="Why the recorded result is wrong")],
) -> None:
    """Withdraw a passing result and re-enter the work.

    The counterpart to `retry`, and the one that has to exist for a gate to be
    correctable. `retry` deliberately refuses a passed node: re-running work
    until it agrees with you is how a green run gets manufactured. This is the
    safe direction — it makes a run *less* green, never more.

    It is what you reach for when the gate itself was wrong. Seven implementers
    passed a lint check on modules they had never written, because a harness
    fault stopped every write and no gate asked whether anything had been
    written. Fixing the gate does not un-record the green those nodes already
    have; this does.

    Everything downstream of an invalidated node goes STALE, for the same reason
    a re-derived artifact invalidates its consumers (§6): a result computed from
    something withdrawn is not evidence.
    """
    with store.Store().session() as session:
        run = _resolve(session, run_id)
        graph = dependency_graph(
            load_plan(
                get_settings().plans_dir / f"{run.plan_name}.yaml",
                profile=_profile(Path(run.target_profile)),
            )
        )

        for node_id in nodes:
            execution = store.get_node(session, run, node_id)
            if execution is None:
                raise typer.BadParameter(f"run has no node '{node_id}'")

            recorder.decide(
                session,
                recorder.request_approval(session, run, node_id=node_id, artifacts=[]),
                decision=Decision.REJECTED,
                decided_by=by,
                note=f"result withdrawn: {why}",
            )
            execution.status = NodeStatus.PENDING
            console.print(f"[yellow]withdrawn[/yellow] {node_id} — re-entering")

            # Everything this node acquired at runtime is its own work, and goes
            # with it. `extra_needs` is exactly that list: a fan-out's children,
            # a failed node's repair. They are *upstream* of it in dependency
            # terms — the parent waits for them — so no descendant cascade will
            # ever reach them, and leaving them behind is what made withdrawing
            # a fan-out a no-op that then reported success.
            for owned in list(execution.extra_needs or ()):
                row = store.get_node(session, run, owned)
                if row is None or row.status is NodeStatus.PENDING:
                    continue
                # SKIPPED, not PENDING. Reclaimed work must not be collectable
                # until its owner has re-derived it: a fan-out child has no
                # dependencies of its own, so leaving it PENDING put it in the
                # *same wave* as the parent that was about to redefine it, and it
                # ran from the definition it was loaded with. Two implementers
                # were judged by a gate two plan revisions old. `_expand` resets
                # these to PENDING once it has refreshed them.
                row.status = NodeStatus.SKIPPED
                console.print(f"[dim]  reclaimed[/dim] {owned} — {node_id} will re-derive it")
                _retire_escalations(session, run, owned, by, why)

            # Drop the acquired edges as well, so the owner is collected *before*
            # the work it owns and can re-derive it. Left in place, the children
            # have no unmet dependency and dispatch in the same wave their parent
            # is still waiting in — from the definition persisted when they were
            # first created. Two implementers were judged by a gate the plan had
            # already replaced, because nothing had re-derived them yet.
            execution.extra_needs = []

            # A result computed from something withdrawn is not evidence (§6).
            # Without this the withdrawn node re-runs while everything built on
            # its old output keeps its green — and the downstream node that
            # would consume the *new* output is still PASSED, so it is never
            # collected and never sees it.
            #
            # Every recorded verdict, not just the green ones. A FAIL computed
            # from a withdrawn input is as much not-evidence as a PASS, and
            # leaving it FAILED is worse than leaving it green: FAILED is
            # neither PENDING nor STALE, so nothing collects it and the run
            # wedges. BLOCKED counts for a sharper reason still — a checkpoint
            # waiting on a person is waiting to approve versions that are being
            # replaced, so its open request is withdrawn with it. A decision on
            # a superseded version is not one anybody should be held to.
            #
            # SKIPPED is left alone: an optional node whose trigger never fired
            # has no verdict to withdraw.
            for descendant in sorted(nx.descendants(graph, node_id)):
                # Retirement is unconditional: a descendant already STALE from an
                # earlier withdrawal still has open questions about verdicts that
                # are being withdrawn again, and skipping it left two of them
                # blocking every wave.
                _retire_escalations(session, run, descendant, by, why)

                downstream = store.get_node(session, run, descendant)
                if downstream is None or downstream.status not in (
                    NodeStatus.PASSED,
                    NodeStatus.BLOCKED,
                    NodeStatus.FAILED,
                    NodeStatus.ERRORED,
                ):
                    continue

                for approval in run.approvals:
                    if approval.node_id == descendant and approval.decision is Decision.PENDING:
                        recorder.decide(
                            session,
                            approval,
                            decision=Decision.REJECTED,
                            decided_by=by,
                            note=f"request withdrawn with {node_id}: {why}",
                        )

                downstream.status = NodeStatus.STALE
                console.print(f"[dim]  stale[/dim] {descendant} — built on it")

            _retire_escalations(session, run, node_id, by, why)

        run.status = RunStatus.RUNNING
        run.stop_reason = None
        session.flush()

    console.print(f"[dim]{len(nodes)} results withdrawn by {by}: {why}[/dim]")
    console.print("[dim]run `orchestrator resume` when the harness is ready[/dim]")


@app.command()
def resume(run_id: Annotated[str | None, typer.Argument()] = None) -> None:
    """Continue a run that stopped at a checkpoint."""
    _advance(run_id)


@app.command()
def watch(
    run_id: Annotated[str | None, typer.Argument()] = None,
    interval: Annotated[float, typer.Option(help="Seconds between polls")] = 1.0,
    until_done: Annotated[
        bool, typer.Option("--until-done", help="Exit when the run stops, instead of following")
    ] = False,
    verbose: Annotated[
        bool, typer.Option("--verbose", "-v", help="Every check, not just the ones that blocked")
    ] = False,
    decide: Annotated[
        bool,
        typer.Option("--decide", help="Answer checkpoints here, and continue the run"),
    ] = False,
    by: Annotated[str | None, typer.Option(help="Who is deciding — required with --decide")] = None,
) -> None:
    """Follow a run as it executes. Open this in a second terminal.

    Reads the run's recorded state and prints each change as it lands: a node
    starting, a gate's verdict and the checks behind it, an artifact version, a
    checkpoint opening.

    **It follows through a stop.** Blocking on a checkpoint is a normal state to
    watch, not the end of one — the run sits there until somebody approves, then
    keeps going in a different process, and a watcher that exited at the first
    block would miss everything after the first human decision. Run status
    changes are printed as they happen; `--until-done` restores the exiting
    behaviour for scripting.

    It is a reader, not a participant — it holds no lock and cannot affect the
    run it is watching. Every line it prints is state already committed, so what
    you see is what a reviewer would find in the evidence bundle afterwards.
    """
    seen_status: dict[str, str] = {}
    seen_gates: set[str] = set()
    seen_artifacts: set[str] = set()
    started = time.monotonic()
    last_event = started

    console.print("[dim]watching — Ctrl-C to stop[/dim]\n")
    first = True
    while True:
        with store.Store().session() as session:
            run = _resolve(session, run_id)
            run_id = run.id

            for execution in store.all_nodes(session, run):
                status = str(execution.status)
                if seen_status.get(execution.node_id) == status:
                    continue
                style = STATUS_STYLE.get(status, "white")
                if execution.node_id in seen_status:
                    console.print(f"  [{style}]{status:<8}[/{style}] {execution.node_id}")
                    last_event = time.monotonic()
                elif first and execution.status is not NodeStatus.PENDING:
                    # Where things stand when you attach. Without it a watcher
                    # started mid-run seeds silently and then says nothing for as
                    # long as the current wave takes — which reads as broken.
                    console.print(
                        f"  [dim]{status:<8} {execution.node_id} (already)[/dim]"
                    )
                seen_status[execution.node_id] = status

                for attempt in execution.attempts:
                    key = f"attempt:{attempt.id}"
                    if key not in seen_gates:
                        seen_gates.add(key)
                        if not first:
                            spent = attempt.model or attempt.worker
                            effort = f"/{attempt.effort}" if attempt.effort else ""
                            console.print(
                                f"    [dim]attempt {attempt.number} · {spent}{effort}[/dim]"
                            )

                    for record in attempt.gate_records:
                        if record.id in seen_gates:
                            continue
                        seen_gates.add(record.id)
                        colour = "green" if record.verdict == "pass" else "red"
                        console.print(
                            f"    [{colour}]gate {record.verdict}[/{colour}] "
                            f"{execution.node_id} [dim]({record.evaluator})[/dim]"
                        )
                        for check in record.checks:
                            passed = check["verdict"] == "pass"
                            if passed and not verbose:
                                continue
                            colour = "green" if passed else "red"
                            observed = check.get("observed")
                            console.print(
                                f"      [{colour}]{check['verdict']}[/{colour}] {check['check']}"
                                + (f" [dim]= {observed}[/dim]" if observed else "")
                                + (
                                    ""
                                    if passed
                                    else f" [dim]{str(check.get('detail') or '')[:80]}[/dim]"
                                )
                            )

            for artifact in sorted(run.artifacts, key=lambda a: a.created_at):
                if artifact.id in seen_artifacts:
                    continue
                seen_artifacts.add(artifact.id)
                if not first:
                    console.print(
                        f"    [cyan]artifact[/cyan] {artifact.name}@v{artifact.version}"
                    )
                # A changeset carries what the scope guard allowed and refused.
                # A denied write is D7 actually happening, and it is the most
                # interesting line in the whole stream — worth surfacing rather
                # than leaving in an artifact somebody reads afterwards.
                if artifact.name.endswith(".changeset") and artifact.path:
                    try:
                        body = json.loads(Path(artifact.path).read_text())
                    except (OSError, json.JSONDecodeError):
                        continue
                    for path in body.get("written", [])[:6]:
                        console.print(f"      [dim]wrote[/dim] {path}")
                    for path in body.get("denied", []):
                        console.print(f"      [red]refused[/red] {path} [dim]out of scope[/dim]")

            for approval in run.approvals:
                key = f"approval:{approval.id}:{approval.decision}"
                if key in seen_artifacts or approval.decision is not Decision.PENDING:
                    continue
                seen_artifacts.add(key)
                console.print(f"  [cyan]awaiting[/cyan] {approval.node_id}")
                for binding in approval.bindings:
                    console.print(f"    [dim]covers {binding.artifact.ref}[/dim]")

            if decide and run.status is RunStatus.BLOCKED:
                pending = [a for a in run.approvals if a.decision is Decision.PENDING]
                if pending:
                    return _decide_here(session, run, pending, by)

            status = str(run.status)
            if seen_status.get("<run>") != status:
                seen_status["<run>"] = status
                colour = {"completed": "green", "blocked": "cyan"}.get(status, "yellow")
                console.print(
                    f"\n[bold {colour}]run {status}[/bold {colour}]"
                    + (f" — {run.stop_reason}" if run.stop_reason else "")
                    + "\n"
                )
            if until_done and run.status is not RunStatus.RUNNING:
                return

            # A long wave is silence, and silence is indistinguishable from a
            # hung watcher. Say what is still out, and for how long.
            if time.monotonic() - last_event > 30:
                elapsed = int(time.monotonic() - started)
                stamp = f"{elapsed // 60}m{elapsed % 60:02d}s"
                console.print(f"  [dim]· {stamp} — {_doing(session, run)}[/dim]")
                last_event = time.monotonic()

        first = False
        time.sleep(interval)


@app.command()
def recheck(
    run_id: Annotated[str, typer.Argument()],
    node: Annotated[str, typer.Argument(help="The errored or failed node to re-check")],
    by: Annotated[
        str | None, typer.Option(help="Who is deciding — required to re-check a FAIL")
    ] = None,
    why: Annotated[
        str | None,
        typer.Option(help="What changed about the question — required to re-check a FAIL"),
    ] = None,
) -> None:
    """Re-run the checks that could not be performed, without re-doing the work.

    The recovery an ERROR needs. `retry` re-enters the node and does the work
    again, which is right for a FAIL and wrong here: an ERROR says the harness
    needs attention and the work does not. Repeating a twelve-minute code agent
    session because a plan omitted a param is exactly the waste the ERROR
    verdict exists to prevent.

    After an ERROR only the unperformed checks are re-evaluated; the rest keep
    the verdicts they were given, so nothing that answered "no" is re-asked.

    A FAILED node can be re-checked too, but only with `--by` and `--why` on the
    record. A check that answered "no" is worth asking again when the *question*
    changed — a verify command scoped to the wrong tree, a threshold read from
    the wrong profile — and the alternative is worse: waiving past a gate that
    was simply wrong records someone accepting a defect that never existed. The
    safeguard is the record, not refusal: the superseded verdict keeps its own
    attempt, and the reason lands in the new gate record's evaluator.
    """
    with store.Store().session() as session:
        target = _resolve(session, run_id)
        loaded = load_plan(
            get_settings().plans_dir / f"{target.plan_name}.yaml",
            profile=_profile(Path(target.target_profile)),
        )
        scheduler = Scheduler(loaded, _worker(), registry=_registry(), artifacts=ArtifactStore())
        scheduler.rehydrate(session, target)

        try:
            result = scheduler.revalidate(session, target, node, reason=why, by=by)
        except SchedulerError as exc:
            console.print(f"[red]{exc}[/red]")
            raise typer.Exit(1) from exc

        colour = "green" if result.passed else "red"
        console.print(f"[{colour}]{result.verdict}[/{colour}] {node} — re-checked")
        for check in result.checks:
            mark = "green" if check.verdict is Verdict.PASS else "red"
            console.print(f"  [{mark}]{check.verdict}[/{mark}] {check.check}")

        if target.status is not RunStatus.RUNNING and result.passed:
            target.status = RunStatus.RUNNING
            target.stop_reason = None
            session.flush()
        resolved = target.id

    _show(resolved)


def _advance(run_id: str | None) -> None:
    with store.Store().session() as session:
        target = _resolve(session, run_id)
        # The profile is read back from the run, so a resumed run resolves the
        # same commands and thresholds the first process did.
        loaded = load_plan(
            get_settings().plans_dir / f"{target.plan_name}.yaml",
            profile=_profile(Path(target.target_profile)),
        )
        scheduler = Scheduler(loaded, _worker(), registry=_registry(), artifacts=ArtifactStore())
        scheduler.rehydrate(session, target)  # inserted nodes came from a previous process

        if target.status is not RunStatus.RUNNING:
            target.status = RunStatus.RUNNING
            session.flush()
        run_id = scheduler.advance(session, target).id

    _show(run_id)


@app.command()
def metrics(run_id: Annotated[str | None, typer.Argument()] = None) -> None:
    """Reliability metrics for a run, and across all runs."""
    with store.Store().session() as session:
        target = _resolve(session, run_id)
        summary = run_metrics(session, target).summary()

        table = Table(title=f"reliability · {target.id[:12]}")
        table.add_column("metric")
        table.add_column("value", justify="right")
        for label, value in summary.items():
            rendered = "—" if value is None else (
                f"{value:.2f}" if isinstance(value, float) else str(value)
            )
            table.add_row(label.replace("_", " "), rendered)
        console.print(table)

        fleet = fleet_metrics(session)
        console.print(f"\nacross {fleet.runs} runs · rollback rate: {fleet.rollback_rate}")
        if not fleet.is_statistically_meaningful:
            console.print(
                "[dim]instrumentation, not statistics — no significance is claimed[/dim]"
            )


@app.command()
def evidence(
    run_id: Annotated[str | None, typer.Argument()] = None,
    write: Annotated[bool, typer.Option(help="Write the bundle to disk")] = False,
) -> None:
    """Assemble the reviewable bundle for a run."""
    with store.Store().session() as session:
        target = _resolve(session, run_id)
        bundle = assemble(session, target)

        if write:
            paths = render.write(bundle, root=get_settings().runs_dir)
            for label, path in paths.items():
                console.print(f"[green]wrote[/green] {label}: {path}")
        else:
            console.print(render.render_markdown(bundle))


@app.command()
def why(
    artifact: Annotated[str, typer.Argument(help="Artifact name, e.g. design.openapi")],
    run_id: Annotated[str | None, typer.Argument()] = None,
) -> None:
    """Trace an artifact back through what produced it.

    The brownfield question — *why is this redirect a 301?* — is this traversal.
    """
    with store.Store().session() as session:
        target = _resolve(session, run_id)
        latest = recorder.latest(session, target, artifact)
        if latest is None:
            raise typer.BadParameter(f"no artifact '{artifact}' in run {target.id}")

        for step in query.why(session, latest):
            console.print(f"  {step}")


@app.command()
def config() -> None:
    """Show the resolved configuration and where a run would write."""
    settings = get_settings()
    table = Table(title="configuration")
    table.add_column("setting")
    table.add_column("value")
    table.add_row("worker", str(settings.worker))
    table.add_row("database", settings.database_url)
    table.add_row("runs", str(settings.runs_dir))
    table.add_row("fixtures", str(settings.fixtures_dir))
    table.add_row("api key", "set" if settings.anthropic_api_key else "[dim]unset[/dim]")
    console.print(table)
    console.print("\n[dim]model and effort are per-node fields in the plan, not settings[/dim]")


if __name__ == "__main__":  # pragma: no cover
    app()
