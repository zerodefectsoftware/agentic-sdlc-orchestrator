"""Composing a plan from a base and a delta.

D16 claims a scenario is added by editing data. This is what makes that true: a
scenario plan says what it changes about another plan, rather than restating it.
Three near-identical 180-line files would have made the claim technically true
and practically false — the second one to drift would prove it.

Merging happens on raw mappings, before validation, so a composed plan is
validated exactly like an authored one. There is no second, weaker path into the
scheduler.

    plan: brownfield
    extends: greenfield

    insert_after:
      intake:
        - id: impact-analysis     # spliced between intake and whatever followed it
    override:
      impl:
        from: impact-analysis.artifacts.affected_modules
"""

from __future__ import annotations

from copy import deepcopy
from typing import Any

DIRECTIVES = ("extends", "insert_after", "override", "remove")


class CompositionError(Exception):
    """A delta refers to something its base does not contain."""


def compose(base: dict[str, Any], delta: dict[str, Any]) -> dict[str, Any]:
    """Apply `delta` to `base` and return a plain, complete plan mapping."""
    composed = deepcopy(base)

    composed["plan"] = delta.get("plan", composed["plan"])
    composed["version"] = delta.get("version", composed.get("version"))
    if "description" in delta:
        composed["description"] = delta["description"]
    if "defaults" in delta:
        composed["defaults"] = {**composed.get("defaults", {}), **delta["defaults"]}
    if "rollback" in delta:
        composed["rollback"] = delta["rollback"]

    # Order matters: insert, then rewire, then drop. Removing first would reject
    # a delta that removes a node *and* re-points its dependents elsewhere —
    # which is the ordinary shape of "this scenario does not scaffold".
    nodes: list[dict] = composed.get("nodes", [])
    nodes = _splice(nodes, delta.get("insert_after", {}))
    nodes = _override(nodes, delta.get("override", {}))
    nodes = _remove(nodes, delta.get("remove", []))

    composed["nodes"] = nodes
    for directive in DIRECTIVES:
        composed.pop(directive, None)
    return composed


def _splice(nodes: list[dict], insertions: dict[str, list[dict]]) -> list[dict]:
    """Insert nodes after an anchor, rewiring what depended on it.

    The splice is linear: `anchor → new₁ → … → newₙ`, and everything that
    previously needed the anchor now needs `newₙ`. Without the rewiring the
    inserted nodes would sit beside the anchor rather than between it and its
    dependents — `baseline-capture` would run *alongside* the work it is supposed
    to precede, which is the one thing a baseline must not do.
    """
    for anchor, added in insertions.items():
        index = _index_of(nodes, anchor, context="insert_after")
        if not added:
            continue

        inserted = [deepcopy(node) for node in added]

        # A node that declares its own `needs` is *placed*, not spliced: an
        # optional escalation target has to hang off the graph rather than sit
        # in the middle of it, or every run would wait for a question nobody
        # raised.
        chain = [node for node in inserted if "needs" not in node]
        previous = anchor
        for node in chain:
            node["needs"] = [previous]
            previous = node["id"]

        if chain:
            last = chain[-1]["id"]
            for node in nodes:
                if node["id"] == anchor:
                    continue
                node["needs"] = [
                    last if dependency == anchor else dependency
                    for dependency in node.get("needs", [])
                ]

        nodes = [*nodes[: index + 1], *inserted, *nodes[index + 1 :]]
    return nodes


def _override(nodes: list[dict], overrides: dict[str, dict]) -> list[dict]:
    """Replace named fields on existing nodes.

    Shallow by design. A deep merge of a `gate` would silently keep base checks a
    scenario meant to replace, and a scenario that cannot fully restate a gate
    cannot express a regression gate at all.
    """
    for node_id, changes in overrides.items():
        index = _index_of(nodes, node_id, context="override")
        nodes[index] = {**nodes[index], **deepcopy(changes)}
    return nodes


def _remove(nodes: list[dict], removals: list[str]) -> list[dict]:
    for node_id in removals:
        _index_of(nodes, node_id, context="remove")
    remaining = [node for node in nodes if node["id"] not in set(removals)]

    known = {node["id"] for node in remaining}
    for node in remaining:
        orphaned = [dep for dep in node.get("needs", []) if dep not in known]
        if orphaned:
            raise CompositionError(
                f"removing {removals} would leave '{node['id']}' needing {orphaned}"
            )
    return remaining


def _index_of(nodes: list[dict], node_id: str, *, context: str) -> int:
    for index, node in enumerate(nodes):
        if node["id"] == node_id:
            return index
    raise CompositionError(
        f"{context} names '{node_id}', which the base plan does not contain "
        f"(it has: {', '.join(node['id'] for node in nodes)})"
    )
