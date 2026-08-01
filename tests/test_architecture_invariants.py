"""Structural invariants that the architecture depends on.

These assert properties a reviewer would otherwise have to take on trust.
They are cheap, they run in CI, and they fail loudly the moment the design
is violated.
"""

import re
from pathlib import Path

import yaml

from orchestrator.engine.loader import load_plan
from orchestrator.engine.profile import TargetProfile

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
        # A plan either authors nodes or changes another plan's. A delta that
        # only overrides is legitimate: `ambiguous.yaml` adds no nodes at all.
        content = plan.get("nodes") or any(
            plan.get(directive) for directive in ("insert_after", "override", "remove")
        )
        assert content, f"{path.name}: authors no nodes and changes none"


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


def test_appendix_a_matches_the_real_plan_file():
    """The doc quotes the plan in full; a quote that drifts is worse than no quote.

    Compared semantically rather than textually, so comment and whitespace edits
    in either copy are fine — only a difference in what the plan *says* fails.
    """
    doc = (REPO / "docs" / "architecture.md").read_text()
    blocks = re.findall(r"```yaml\n(plan: greenfield\n.*?)\n```", doc, re.S)
    assert len(blocks) == 1, "expected exactly one greenfield plan block in the doc"

    documented = yaml.safe_load(blocks[0])
    actual = yaml.safe_load((REPO / "plans" / "greenfield.yaml").read_text())
    assert documented == actual, (
        "docs/architecture.md Appendix A has drifted from plans/greenfield.yaml"
    )


def test_generated_schemas_match_their_models():
    """D8: the contract an agent is held to is derived, not hand-written.

    If `schemas/*.json` were maintained by hand they would drift from the models
    the predicates read, and an agent would be graded against a contract nobody
    was checking.
    """
    import json

    from orchestrator.artifacts import SCHEMAS

    for name, model in SCHEMAS.items():
        path = REPO / "schemas" / f"{name}.json"
        assert path.exists(), f"schemas/{name}.json has not been generated"
        on_disk = json.loads(path.read_text())
        assert on_disk == model.model_json_schema(), (
            f"schemas/{name}.json is stale — regenerate it from {model.__name__}"
        )


def test_run_targets_declare_their_execution_scheme():
    """`run:` covers both Python callables and shell commands; guessing is a bug."""
    for path in sorted((REPO / "plans").glob("*.yaml")):
        plan = yaml.safe_load(path.read_text())
        for node in plan.get("nodes") or []:
            run = node.get("run")
            if run is not None:
                assert run.startswith(("py:", "sh:")), (
                    f"{path.name}: {node['id']} has unprefixed run: {run!r}"
                )


def test_write_scopes_stay_inside_the_target():
    """No node may write outside the target tree.

    The orchestrator must not be modifiable by the agents it governs. Loaded
    through the loader rather than read as text, so scenario plans — which are
    deltas, and whose nodes are not all in their own file — are covered too.
    """
    profile = TargetProfile.load(REPO / "config" / "target.shortener.yaml")

    for path in sorted((REPO / "plans").glob("*.yaml")):
        plan = load_plan(path, profile=profile)
        for node in plan.nodes:
            scopes = list(node.write_scope)
            if node.template:
                scopes += list(node.template.write_scope)
            for scope in scopes:
                assert scope.startswith("target/"), (
                    f"{path.name}: {node.id} writes outside target/: {scope}"
                )
