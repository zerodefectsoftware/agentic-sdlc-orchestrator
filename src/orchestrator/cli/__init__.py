"""Command surface: run, status, approve, resume, metrics, evidence, why.

The composition root — the only place that reads settings, builds a store,
chooses a worker, and wires a scheduler. Everything below takes its
collaborators as arguments, which is why the rest of the system is testable
without an environment.
"""

from orchestrator.cli.main import app

__all__ = ["app"]
