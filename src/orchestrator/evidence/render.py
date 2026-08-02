"""Rendering a bundle.

Two projections of the same record: Markdown for a person, JSON for a machine.
Neither adds anything the bundle does not already contain.

The Markdown is ordered by what a reviewer needs first — the verdict, then what
blocked, then what a human decided — rather than by the order the run happened
in. A bundle that has to be read chronologically to find the problem is a log,
not evidence.
"""

from __future__ import annotations

import json
from dataclasses import asdict
from datetime import datetime
from pathlib import Path

from orchestrator.evidence.bundle import EvidenceBundle


def render_markdown(bundle: EvidenceBundle) -> str:
    sections = [
        _header(bundle),
        _verdict(bundle),
        _blocking(bundle),
        _approvals(bundle),
        _stages(bundle),
        _artifacts(bundle),
        _counts(bundle),
        _metrics(bundle),
    ]
    return "\n\n".join(section for section in sections if section).rstrip() + "\n"


def render_json(bundle: EvidenceBundle) -> str:
    payload = asdict(bundle)
    payload["counts"] = bundle.counts
    payload["releasable"] = bundle.is_releasable
    payload["superseded_gates"] = len(bundle.superseded_gates)
    return json.dumps(payload, indent=2, sort_keys=True, default=_encode) + "\n"


def write(bundle: EvidenceBundle, root: Path | str = "runs") -> dict[str, Path]:
    """Write both projections under `runs/<run_id>/evidence/`."""
    directory = Path(root) / bundle.run_id / "evidence"
    directory.mkdir(parents=True, exist_ok=True)

    paths = {
        "markdown": directory / "evidence.md",
        "json": directory / "evidence.json",
    }
    paths["markdown"].write_text(render_markdown(bundle))
    paths["json"].write_text(render_json(bundle))
    return paths


# --------------------------------------------------------------------------- #
# sections
# --------------------------------------------------------------------------- #


def _header(bundle: EvidenceBundle) -> str:
    return "\n".join(
        [
            f"# Evidence bundle — {bundle.plan} v{bundle.plan_version}",
            "",
            f"- **Run** `{bundle.run_id}`",
            f"- **Requirement** `{bundle.requirement_path}`",
            f"- **Target** `{bundle.target_profile}`",
            f"- **Started** {_when(bundle.started_at)}",
            f"- **Finished** {_when(bundle.finished_at)}",
        ]
    )


def _verdict(bundle: EvidenceBundle) -> str:
    if bundle.is_releasable:
        headline = "**RELEASABLE** — every gate held, every approval current."
    else:
        headline = f"**NOT RELEASABLE** — run status `{bundle.status}`."

    lines = ["## Verdict", "", headline]
    if bundle.stop_reason:
        lines += ["", f"> {bundle.stop_reason}"]
    return "\n".join(lines)


def _blocking(bundle: EvidenceBundle) -> str:
    """What stopped the run — the first thing a reviewer looks for."""
    blocking = bundle.blocking_gates
    if not blocking:
        return ""

    lines = ["## What blocked", ""]
    for gate in blocking:
        lines.append(
            f"### `{gate.node_id}` — {gate.verdict.upper()} "
            f"(attempt {gate.attempt}, {gate.evaluator})"
        )
        lines.append("")
        for check in gate.failures:
            observed = f" — observed {check.observed}" if check.observed else ""
            lines.append(f"- `{check.check}`{observed}")
            if check.detail:
                lines.append(f"  - {check.detail}")
        lines.append("")
    return "\n".join(lines).rstrip()


def _approvals(bundle: EvidenceBundle) -> str:
    if not bundle.approvals:
        return ""

    lines = [
        "## Human decisions",
        "",
        "| Node | Decision | By | When | Covers |",
        "|---|---|---|---|---|",
    ]
    for approval in bundle.approvals:
        covers = ", ".join(f"`{ref}`" for ref in approval.covers) or "—"
        flag = " ⚠️ **stale**" if approval.stale else ""
        lines.append(
            f"| `{approval.node_id}` | {approval.decision}{flag} | "
            f"{approval.decided_by or '—'} | {_when(approval.decided_at)} | {covers} |"
        )

    if bundle.stale_approvals:
        lines += [
            "",
            "> A stale approval covered an artifact version that has since been "
            "re-derived. It no longer authorises what exists (D10).",
        ]
    return "\n".join(lines)


def _stages(bundle: EvidenceBundle) -> str:
    lines = ["## Lifecycle", ""]
    for stage, nodes in bundle.stages.items():
        lines.append(f"### {stage}")
        lines.append("")
        for node in nodes:
            marks = []
            if node.inserted:
                marks.append("inserted")
            if node.retried:
                marks.append(f"{len(node.attempts)} attempts")
            suffix = f" _({', '.join(marks)})_" if marks else ""
            lines.append(f"- `{node.node_id}` — {node.status}{suffix}")
        lines.append("")
    return "\n".join(lines).rstrip()


def _artifacts(bundle: EvidenceBundle) -> str:
    if not bundle.artifacts:
        return ""

    lines = ["## Artifacts", "", "| Artifact | Produced by | Hash |", "|---|---|---|"]
    for artifact in bundle.artifacts:
        producer = artifact.produced_by_node or "**unattributed**"
        lines.append(f"| `{artifact.ref}` | {producer} | `{artifact.content_hash[:12]}` |")
    return "\n".join(lines)


def _counts(bundle: EvidenceBundle) -> str:
    counts = bundle.counts
    lines = ["## Counts", ""]
    lines += [f"- {label.replace('_', ' ')}: {value}" for label, value in counts.items()]
    return "\n".join(lines)


def _metrics(bundle: EvidenceBundle) -> str:
    """Reliability metrics, with the caveat attached rather than left implicit."""
    if not bundle.metrics:
        return ""

    lines = ["## Reliability", ""]
    for label, value in bundle.metrics.items():
        rendered = "—" if value is None else value
        if isinstance(value, float):
            rendered = f"{value:.2f}"
        lines.append(f"- {label.replace('_', ' ')}: {rendered}")

    lines += [
        "",
        "> These describe one run. Across three scenarios they are instrumentation, "
        "not statistics — no significance is claimed.",
    ]
    return "\n".join(lines)


def _when(moment: datetime | None) -> str:
    return moment.isoformat(timespec="seconds") if moment else "—"


def _encode(value: object) -> str:
    if isinstance(value, datetime):
        return value.isoformat()
    return str(value)
