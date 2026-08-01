"""The security scan.

Deliberately a small set of high-signal patterns rather than a general analyser:
the point is to produce findings a gate can act on and a human can waive, not to
be a linter. Anything it reports is a `SecurityReport` artifact, which
`no_unapproved_high_findings` then reads (D15 — an agent may never waive one).

The patterns are the ones intrinsic to what a URL shortener *is*. That is not a
coincidence: a scan tuned to a target's actual shape finds real problems, and a
generic one finds style.
"""

from __future__ import annotations

import re

from orchestrator.artifacts import Finding, SecurityReport, Severity
from orchestrator.workers.pytask import Task, TaskOutput

PATTERNS: tuple[tuple[str, str, Severity, str], ...] = (
    (
        "OPEN_REDIRECT",
        r"RedirectResponse\((?![^)]*allow)",
        Severity.HIGH,
        "redirect target is not checked against an allowlist — the service becomes "
        "a phishing laundromat",
    ),
    (
        "SSRF",
        r"(?:requests|httpx|urllib)\.\w*\(\s*(?:url|target|destination)",
        Severity.HIGH,
        "fetches a user-supplied URL — an attacker can reach cloud metadata or "
        "internal hosts",
    ),
    (
        "SEQUENTIAL_CODES",
        r"(?:count\(\)\s*\+\s*1|id\s*\+\s*1|autoincrement)",
        Severity.MEDIUM,
        "sequential short codes let anyone enumerate every stored URL",
    ),
    (
        "RAW_CLIENT_IP",
        r"(?:client\.host|X-Forwarded-For)(?![^\n]*(?:hash|truncat|anonym))",
        Severity.MEDIUM,
        "storing a full client IP is a data-protection problem; hash or truncate it",
    ),
    (
        "HARDCODED_SECRET",
        r"(?:secret|token|api_key)\s*=\s*['\"][A-Za-z0-9_\-]{16,}",
        Severity.HIGH,
        "a credential is embedded in source",
    ),
)


def security_scan(task: Task) -> TaskOutput:
    root = task.cwd / "target"
    findings: list[Finding] = []

    for path in sorted(root.rglob("*.py")) if root.exists() else []:
        if "__pycache__" in path.parts:
            continue
        source = path.read_text()
        relative = path.relative_to(task.cwd)

        for code, pattern, severity, why in PATTERNS:
            for match in re.finditer(pattern, source):
                line = source[: match.start()].count("\n") + 1
                findings.append(
                    Finding(
                        id=f"{code}-{relative}:{line}",
                        title=f"{code} in {relative} line {line}",
                        severity=severity,
                        rationale=why,
                    )
                )

    report = SecurityReport(findings=findings)
    return TaskOutput(
        facts={
            "scan.findings": len(findings),
            "scan.high": sum(1 for f in findings if f.is_open_high),
        },
        artifacts={"security.report": report.model_dump_json(indent=2)},
    )
