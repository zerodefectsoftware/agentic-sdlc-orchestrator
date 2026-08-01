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

import shlex
import subprocess
from pathlib import Path
from typing import Annotated

import typer
from rich.console import Console
from rich.table import Table
from sqlalchemy import select

from orchestrator.artifacts import Baseline
from orchestrator.config import MissingCredential, WorkerMode, get_settings
from orchestrator.engine.loader import PlanError, execution_order, load_plan
from orchestrator.engine.profile import ProfileError, TargetProfile
from orchestrator.engine.scheduler import Scheduler, verify_probes
from orchestrator.evidence import assemble, render
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

    if missing:
        console.print(f"[red]not registered:[/red] {', '.join(missing)}")
        raise typer.Exit(1)
    console.print("[green]every predicate is registered[/green]")


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
def resume(run_id: Annotated[str | None, typer.Argument()] = None) -> None:
    """Continue a run that stopped at a checkpoint."""
    _advance(run_id)


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
