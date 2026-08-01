"""Reliability metrics: success rate, retry/rollback frequency, MTTR, latency (§8).

Every definition is written down in `collect.py`, because a metric whose
definition is vague invites a number to be quoted without anyone knowing what it
measures.

Three choices resist flattering the system: unrecovered incidents are excluded
from MTTR and reported separately, rates return None over an empty set rather
than 0.0, and human wait time is separated from end-to-end latency.
"""

from orchestrator.metrics.collect import (
    FleetMetrics,
    Incident,
    RunMetrics,
    StageMetrics,
    fleet_metrics,
    run_metrics,
    unfinished_nodes,
)

__all__ = [
    "FleetMetrics",
    "Incident",
    "RunMetrics",
    "StageMetrics",
    "fleet_metrics",
    "run_metrics",
    "unfinished_nodes",
]
