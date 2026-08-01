"""Load a plan graph from YAML, validate it, and resolve defaults.

Validation is deliberately strict and happens before anything executes. A plan
that reaches the scheduler is guaranteed to be a well-formed DAG whose nodes are
individually executable — so the scheduler never has to defend against a typo.

Graph algorithms come from networkx (§2.1): cycle detection and topological
ordering are solved problems, and the governed behaviour built on top of them is
what we write.
"""

from __future__ import annotations

from collections import Counter
from pathlib import Path

import networkx as nx
import yaml
from pydantic import ValidationError

from orchestrator.engine.plan import Node, Plan


class PlanError(Exception):
    """A plan file is malformed, inconsistent, or unsafe.

    Carries the file path so the message is actionable without a traceback.
    """

    def __init__(self, path: Path | str, message: str) -> None:
        self.path = Path(path)
        super().__init__(f"{self.path}: {message}")


def load_plan(path: Path | str, *, write_ceiling: list[str] | None = None) -> Plan:
    """Parse, validate, and return the plan at `path`.

    `write_ceiling` comes from the target profile. When supplied, every
    `write_scope` in the plan must fall inside it — the orchestrator must not be
    modifiable by the agents it governs.
    """
    path = Path(path)
    raw = _read_yaml(path)

    try:
        plan = Plan.model_validate(raw)
    except ValidationError as exc:
        raise PlanError(path, _format_validation_error(exc)) from exc

    _check_unique_ids(path, plan)
    _check_references_resolve(path, plan)
    _check_acyclic(path, plan)
    if write_ceiling is not None:
        _check_write_scopes(path, plan, write_ceiling)

    return _apply_defaults(plan)


def dependency_graph(plan: Plan) -> nx.DiGraph:
    """Edges point from dependency to dependent, so topological order is execution order."""
    graph = nx.DiGraph()
    graph.add_nodes_from(plan.node_ids)
    for node in plan.nodes:
        for dependency in node.needs:
            graph.add_edge(dependency, node.id)
    return graph


def execution_order(plan: Plan) -> list[str]:
    """One valid sequential order. Parallelism comes from the scheduler, not from this."""
    return list(nx.topological_sort(dependency_graph(plan)))


# --------------------------------------------------------------------------- #
# internals
# --------------------------------------------------------------------------- #


def _read_yaml(path: Path) -> dict:
    if not path.exists():
        raise PlanError(path, "plan file not found")
    try:
        raw = yaml.safe_load(path.read_text())
    except yaml.YAMLError as exc:
        raise PlanError(path, f"invalid YAML: {exc}") from exc
    if not isinstance(raw, dict):
        raise PlanError(path, "plan must be a YAML mapping")
    return raw


def _format_validation_error(exc: ValidationError) -> str:
    """Pydantic's default rendering is noisy; keep the location and the reason."""
    lines = []
    for error in exc.errors():
        location = ".".join(str(part) for part in error["loc"]) or "<root>"
        lines.append(f"{location}: {error['msg']}")
    return "; ".join(lines)


def _check_unique_ids(path: Path, plan: Plan) -> None:
    counts = Counter(plan.node_ids)
    duplicates = sorted(node_id for node_id, count in counts.items() if count > 1)
    if duplicates:
        raise PlanError(path, f"duplicate node ids: {', '.join(duplicates)}")


def _check_references_resolve(path: Path, plan: Plan) -> None:
    """Every `needs` and `on_escalate` must name a node that exists."""
    known = set(plan.node_ids)
    for node in plan.nodes:
        for dependency in node.needs:
            if dependency not in known:
                raise PlanError(path, f"node '{node.id}' needs unknown node '{dependency}'")
        if node.on_escalate and node.on_escalate not in known:
            raise PlanError(
                path, f"node '{node.id}' escalates to unknown node '{node.on_escalate}'"
            )


def _check_acyclic(path: Path, plan: Plan) -> None:
    graph = dependency_graph(plan)
    if not nx.is_directed_acyclic_graph(graph):
        cycle = nx.find_cycle(graph)
        trail = " -> ".join(edge[0] for edge in cycle) + f" -> {cycle[0][0]}"
        raise PlanError(path, f"plan graph contains a cycle: {trail}")


def _check_write_scopes(path: Path, plan: Plan, ceiling: list[str]) -> None:
    """No node may write outside the target tree, whatever its plan entry asks for."""
    prefixes = tuple(pattern.rstrip("*").rstrip("/") for pattern in ceiling)
    for node in plan.nodes:
        scopes = list(node.write_scope)
        if node.template:
            scopes += node.template.write_scope
        for scope in scopes:
            if not scope.startswith(prefixes):
                raise PlanError(
                    path,
                    f"node '{node.id}' declares write_scope '{scope}' outside the "
                    f"write ceiling {list(ceiling)}",
                )


def _apply_defaults(plan: Plan) -> Plan:
    """Resolve plan-wide defaults so every node is complete before execution.

    Only model-backed nodes get a model: a default silently attached to a
    subprocess would be a lie in the lineage record.
    """
    resolved: list[Node] = []
    for node in plan.nodes:
        updates: dict[str, object] = {}
        if node.autonomy is None:
            updates["autonomy"] = plan.defaults.autonomy
        if node.retry_budget is None:
            updates["retry_budget"] = plan.defaults.retry_budget
        if node.is_model_backed:
            if node.effort is None:
                updates["effort"] = plan.defaults.effort
            if node.model is None and plan.defaults.model:
                updates["model"] = plan.defaults.model
        resolved.append(node.model_copy(update=updates) if updates else node)

    return plan.model_copy(update={"nodes": resolved})
