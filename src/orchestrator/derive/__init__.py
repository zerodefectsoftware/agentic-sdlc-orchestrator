"""Deterministic generation from a contract (D8).

Anything derivable is derived. A generated artifact cannot hallucinate, needs no
gate to catch it inventing an endpoint, and costs no tokens — which is why
`scaffold` is a `derive` node rather than another agent.
"""

from orchestrator.derive.codemap import map_codebase
from orchestrator.derive.scaffold import scaffold_from_design

__all__ = ["map_codebase", "scaffold_from_design"]
