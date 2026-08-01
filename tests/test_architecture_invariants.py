"""Structural invariants that the architecture depends on.

These assert properties a reviewer would otherwise have to take on trust.
They are cheap, they run in CI, and they fail loudly the moment the design
is violated.
"""

import re
from pathlib import Path

import yaml

REPO = Path(__file__).resolve().parents[1]
ORCHESTRATOR = REPO / "src" / "orchestrator"


def _python_sources(root: Path) -> list[Path]:
    return [p for p in root.rglob("*.py") if "__pycache__" not in p.parts]


def test_orchestrator_never_imports_the_target():
    """D3: generality is checkable, not merely claimed.

    An import edge from the control plane to the target would make
    "the orchestrator knows nothing about URL shortening" false.
    """
    offenders = []
    pattern = re.compile(r"^\s*(?:from|import)\s+(shortener|target)\b", re.MULTILINE)
    for path in _python_sources(ORCHESTRATOR):
        if pattern.search(path.read_text()):
            offenders.append(str(path.relative_to(REPO)))
    assert not offenders, f"orchestrator imports the target: {offenders}"


def test_every_plan_parses_and_declares_required_fields():
    plans = sorted((REPO / "plans").glob("*.yaml"))
    assert plans, "no plan graphs found"
    for path in plans:
        plan = yaml.safe_load(path.read_text())
        assert plan.get("plan"), f"{path.name}: missing 'plan'"
        assert plan.get("version"), f"{path.name}: missing 'version'"
        nodes = plan.get("nodes") or plan.get("insert_after")
        assert nodes, f"{path.name}: declares neither 'nodes' nor 'insert_after'"


def test_plan_node_ids_are_unique_and_dependencies_resolve():
    for path in sorted((REPO / "plans").glob("*.yaml")):
        plan = yaml.safe_load(path.read_text())
        nodes = plan.get("nodes")
        if not nodes:
            continue  # scenario deltas are validated once merged onto a base plan
        ids = [n["id"] for n in nodes]
        assert len(ids) == len(set(ids)), f"{path.name}: duplicate node ids"
        for node in nodes:
            for dep in node.get("needs", []):
                assert dep in ids, f"{path.name}: {node['id']} needs unknown '{dep}'"


def test_write_scopes_stay_inside_the_target():
    """No node may write outside the target tree.

    The orchestrator must not be modifiable by the agents it governs.
    """
    for path in sorted((REPO / "plans").glob("*.yaml")):
        plan = yaml.safe_load(path.read_text())
        for node in plan.get("nodes") or []:
            scopes = list(node.get("write_scope", []))
            template = node.get("template") or {}
            scopes += list(template.get("write_scope", []))
            for scope in scopes:
                assert scope.startswith("target/"), (
                    f"{path.name}: {node['id']} writes outside target/: {scope}"
                )
