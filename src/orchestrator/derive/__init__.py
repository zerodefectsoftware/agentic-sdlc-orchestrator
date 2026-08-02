"""Deterministic work over a contract (D8).

Anything derivable is derived: no hallucination, no gate needed to catch an
invented endpoint, no tokens spent. `map_codebase` derives a map from source;
`verify_target_matches_contract` audits a tree against the contract its author
also declared, which is what makes an architect writing Python safe (D24).
"""

from orchestrator.derive.codemap import map_codebase
from orchestrator.derive.scaffold import verify_target_matches_contract

__all__ = ["map_codebase", "verify_target_matches_contract"]
