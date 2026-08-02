"""Plan composition and target-profile resolution.

D16 claims a scenario is added by editing data. These tests are what make that
checkable rather than rhetorical: the three shipped plans are one spine and two
deltas, and a delta is validated exactly like an authored plan.

The failure this guards against is subtle. Three near-identical 180-line plan
files would satisfy "the engine did not change" while making the claim useless —
the second one to drift would prove it, and nothing would have caught the drift.
"""

from __future__ import annotations

from pathlib import Path

import pytest
import yaml

from orchestrator.engine.compose import CompositionError, compose
from orchestrator.engine.loader import PlanError, dependency_graph, load_plan
from orchestrator.engine.profile import ProfileError, TargetProfile
from orchestrator.gates.predicates import register_all
from orchestrator.gates.registry import PredicateRegistry

REPO = Path(__file__).resolve().parents[1]
PLANS = REPO / "plans"
PROFILE = TargetProfile.load(REPO / "config" / "target.shortener.yaml")

BASE = {
    "plan": "base",
    "version": 1,
    "nodes": [
        {"id": "a", "kind": "tool", "stage": "requirements", "run": "sh:true"},
        {"id": "b", "kind": "tool", "stage": "design", "run": "sh:true", "needs": ["a"]},
        {"id": "c", "kind": "tool", "stage": "release", "run": "sh:true", "needs": ["b"]},
    ],
}


def ids(composed: dict) -> list[str]:
    return [node["id"] for node in composed["nodes"]]


def node(composed: dict, node_id: str) -> dict:
    return next(n for n in composed["nodes"] if n["id"] == node_id)


# --------------------------------------------------------------------------- #
# splicing
# --------------------------------------------------------------------------- #


def test_an_inserted_node_goes_between_the_anchor_and_its_dependents():
    """The rewiring is the whole point.

    Without it, `baseline-capture` would run *alongside* the work it is supposed
    to precede — which is the one thing a baseline must not do.
    """
    composed = compose(
        BASE,
        {
            "insert_after": {
                "a": [{"id": "x", "kind": "tool", "stage": "requirements", "run": "sh:true"}]
            }
        },
    )

    assert ids(composed) == ["a", "x", "b", "c"]
    assert node(composed, "x")["needs"] == ["a"]
    assert node(composed, "b")["needs"] == ["x"]  # rewired
    assert node(composed, "c")["needs"] == ["b"]  # untouched


def test_several_inserted_nodes_form_a_chain():
    composed = compose(
        BASE,
        {
            "insert_after": {
                "a": [
                    {"id": "x", "kind": "tool", "stage": "requirements", "run": "sh:true"},
                    {"id": "y", "kind": "tool", "stage": "requirements", "run": "sh:true"},
                ]
            }
        },
    )

    assert node(composed, "y")["needs"] == ["x"]
    assert node(composed, "b")["needs"] == ["y"]  # the last of the chain


def test_a_node_that_declares_its_own_needs_is_placed_not_spliced():
    """An optional escalation target must hang off the graph, not sit in it.

    Spliced in, every run would wait for a question nobody raised.
    """
    composed = compose(
        BASE,
        {
            "insert_after": {
                "a": [
                    {
                        "id": "ask",
                        "kind": "human",
                        "stage": "design",
                        "needs": [],
                        "optional": True,
                    },
                ]
            }
        },
    )

    assert node(composed, "b")["needs"] == ["a"]  # not rewired to 'ask'
    assert node(composed, "ask")["needs"] == []


# --------------------------------------------------------------------------- #
# overriding and removing
# --------------------------------------------------------------------------- #


def test_an_override_replaces_a_field_wholesale():
    """Shallow by design: a deep merge would keep base checks a scenario meant
    to replace, and a scenario that cannot restate a gate cannot express one."""
    composed = compose(BASE, {"override": {"b": {"gate": {"all": [{"predicate": "p"}]}}}})
    assert node(composed, "b")["gate"] == {"all": [{"predicate": "p"}]}
    assert node(composed, "b")["run"] == "sh:true"  # untouched fields survive


def test_a_node_can_be_removed_once_its_dependents_are_rewired():
    """Ordering: insert, then rewire, then drop.

    Removing first would reject the ordinary shape of "this scenario does not
    scaffold" — which is exactly what brownfield says.
    """
    composed = compose(BASE, {"override": {"c": {"needs": ["a"]}}, "remove": ["b"]})
    assert ids(composed) == ["a", "c"]


def test_removing_a_node_something_still_needs_is_refused():
    with pytest.raises(CompositionError, match="would leave 'c' needing"):
        compose(BASE, {"remove": ["b"]})


@pytest.mark.parametrize(
    "delta",
    [
        {"override": {"nope": {"role": "x"}}},
        {"remove": ["nope"]},
        {"insert_after": {"nope": [{"id": "x", "kind": "tool", "stage": "design"}]}},
    ],
)
def test_a_delta_naming_an_unknown_node_says_what_the_base_contains(delta):
    with pytest.raises(CompositionError, match="a, b, c"):
        compose(BASE, delta)


# --------------------------------------------------------------------------- #
# through the loader
# --------------------------------------------------------------------------- #


def write(tmp_path: Path, name: str, body: dict) -> Path:
    path = tmp_path / f"{name}.yaml"
    path.write_text(yaml.safe_dump(body))
    return path


def test_a_composed_plan_is_validated_exactly_like_an_authored_one(tmp_path):
    """There must be no second, weaker path into the scheduler."""
    write(tmp_path, "base", BASE)
    delta = write(
        tmp_path,
        "delta",
        {
            "plan": "delta",
            "extends": "base",
            "override": {"b": {"kind": "agent", "role": None}},  # agent without a role
        },
    )

    with pytest.raises(PlanError, match="requires 'role'"):
        load_plan(delta)


def test_extending_a_plan_that_does_not_exist_says_so(tmp_path):
    delta = write(tmp_path, "delta", {"plan": "delta", "extends": "missing"})
    with pytest.raises(PlanError, match="does not exist"):
        load_plan(delta)


def test_a_circular_extends_chain_is_refused(tmp_path):
    write(tmp_path, "one", {"plan": "one", "extends": "two", "nodes": []})
    write(tmp_path, "two", {"plan": "two", "extends": "one", "nodes": []})
    with pytest.raises(PlanError, match="circular extends"):
        load_plan(tmp_path / "one.yaml")


# --------------------------------------------------------------------------- #
# the target profile
# --------------------------------------------------------------------------- #


def test_placeholders_resolve_from_the_profile(tmp_path):
    path = write(
        tmp_path,
        "p",
        {
            "plan": "p",
            "version": 1,
            "nodes": [
                {
                    "id": "t",
                    "kind": "tool",
                    "stage": "verification",
                    "run": "sh:{target.commands.test}",
                    "gate": {"all": ["coverage.percent >= {target.thresholds.coverage_min}"]},
                }
            ],
        },
    )
    plan = load_plan(path, profile=PROFILE)

    assert plan.node("t").run_target == PROFILE.commands["test"]
    assert str(plan.node("t").gate.all_checks[0]) == "coverage.percent >= 80"


def test_a_fanout_item_placeholder_is_left_for_runtime(tmp_path):
    """`{item.path}` belongs to materialisation, not to load time."""
    path = write(
        tmp_path,
        "p",
        {
            "plan": "p",
            "version": 1,
            "nodes": [
                {
                    "id": "f",
                    "kind": "fanout",
                    "stage": "implementation",
                    "from": "design.artifacts.modules",
                    "template": {
                        "kind": "codeagent",
                        "role": "implementer",
                        "write_scope": ["{target.root}/{item.path}/**"],
                    },
                }
            ],
        },
    )
    plan = load_plan(path, profile=PROFILE)
    assert plan.node("f").template.write_scope == ["target/shortener/{item.path}/**"]


def test_a_placeholder_the_profile_does_not_define_is_an_error(tmp_path):
    path = write(
        tmp_path,
        "p",
        {
            "plan": "p",
            "version": 1,
            "nodes": [
                {
                    "id": "t",
                    "kind": "tool",
                    "stage": "release",
                    "run": "sh:{target.commands.deploy}",
                }
            ],
        },
    )
    with pytest.raises(PlanError, match="does not define"):
        load_plan(path, profile=PROFILE)


def test_an_incomplete_profile_reads_as_a_profile_problem(tmp_path):
    path = tmp_path / "profile.yaml"
    path.write_text("target: {}\n")
    with pytest.raises(ProfileError, match="incomplete"):
        TargetProfile.load(path)


# --------------------------------------------------------------------------- #
# the plans that ship
# --------------------------------------------------------------------------- #


SCENARIOS = ["greenfield", "brownfield", "ambiguous"]


@pytest.fixture(params=SCENARIOS)
def scenario(request):
    return load_plan(PLANS / f"{request.param}.yaml", profile=PROFILE)


def test_every_shipped_plan_loads_and_is_acyclic(scenario):
    assert scenario.nodes
    assert dependency_graph(scenario).number_of_nodes() == len(scenario.nodes)


def test_every_check_a_shipped_plan_names_can_be_performed(scenario):
    """An unimplemented check that reports green is the most dangerous state a
    governance system can be in — so a plan may not name one."""
    assert register_all(PredicateRegistry()).missing(scenario.required_predicates) == []


def test_the_scenarios_are_deltas_not_copies():
    """The claim in D16, as a diff rather than an assertion."""
    for name in ("brownfield", "ambiguous"):
        body = yaml.safe_load((PLANS / f"{name}.yaml").read_text())
        assert body["extends"] == "greenfield"
        assert "nodes" not in body


def test_greenfield_normalizes_a_clarification_too():
    """The clarification path belongs to the spine, not to the ambiguous plan —
    a run that stops for an answer must fold it back in wherever that happens."""
    plan = load_plan(PLANS / "greenfield.yaml", profile=PROFILE)

    normalize = plan.node("normalize-clarification")
    assert normalize.optional is True          # skipped unless triage escalates
    assert normalize.needs == ["clarify-with-human"]


def test_brownfield_orders_the_baseline_before_anything_reads_the_code():
    plan = load_plan(PLANS / "brownfield.yaml", profile=PROFILE)
    graph = dependency_graph(plan)

    import networkx as nx

    assert nx.has_path(graph, "baseline-capture", "codebase-map")
    assert nx.has_path(graph, "codebase-map", "impact-analysis")
    assert nx.has_path(graph, "impact-analysis", "impl")


def test_brownfield_does_not_scaffold_over_an_existing_codebase():
    plan = load_plan(PLANS / "brownfield.yaml", profile=PROFILE)
    assert "scaffold" not in plan.node_ids
    assert plan.node("tests-acceptance").needs == ["design-approval"]


def test_brownfield_fans_out_over_what_the_analysis_found():
    """Both plans fan out; they differ in where the contract comes from.

    Brownfield changes a codebase whose interfaces already exist, so it fans out
    over what the impact analysis found. Greenfield fans out over the design's
    modules, against a contract the architect settled first (D24). The failure
    D23 responded to was a greenfield fan-out with no contract at all — not
    parallelism itself.
    """
    plan = load_plan(PLANS / "brownfield.yaml", profile=PROFILE)
    assert plan.node("impl").kind == "fanout"
    assert plan.node("impl").from_ == "impact-analysis.artifacts.affected_modules"

    greenfield = load_plan(PLANS / "greenfield.yaml", profile=PROFILE)
    assert greenfield.node("impl").kind == "fanout"
    assert greenfield.node("impl").from_ == "design.artifacts.modules"


def test_brownfield_declares_a_rollback_the_engine_can_perform():
    plan = load_plan(PLANS / "brownfield.yaml", profile=PROFILE)
    assert plan.rollback is not None

    source = plan.node(plan.rollback.restore_from)
    assert source.outputs == ["snapshot"]  # the artifact rollback reads
    assert plan.rollback.verify_with == PROFILE.commands["test"]


def test_a_rollback_naming_an_unknown_node_is_refused(tmp_path):
    """The restore point has to exist, or the control is a comment."""
    write(tmp_path, "base", BASE)
    delta = write(
        tmp_path,
        "delta",
        {
            "plan": "delta",
            "extends": "base",
            "rollback": {"restore_from": "nowhere", "verify_with": "true"},
        },
    )
    with pytest.raises(PlanError, match="restores from unknown node"):
        load_plan(delta)


def test_greenfield_has_nothing_to_roll_back_to():
    assert load_plan(PLANS / "greenfield.yaml", profile=PROFILE).rollback is None


def test_ambiguous_always_stops_to_ask():
    """Greenfield asks only when triage escalates; here the requirement is too
    vague to have a defensible reading to proceed from."""
    plan = load_plan(PLANS / "ambiguous.yaml", profile=PROFILE)

    clarify = plan.node("clarify-with-human")
    assert clarify.optional is False
    assert clarify.needs == ["ambiguity-triage"]


def test_ambiguous_normalizes_the_answer_before_designing():
    """Otherwise clarification is theatre: the run stops, a person answers, and
    design works from the same unresolved register."""
    plan = load_plan(PLANS / "ambiguous.yaml", profile=PROFILE)

    assert plan.node("normalize-clarification").needs == ["clarify-with-human"]
    assert plan.node("normalize-clarification").optional is False   # mandatory here
    assert "normalize-clarification" in plan.node("design").needs


def test_ambiguous_lowers_the_escalation_threshold_in_data():
    plan = load_plan(PLANS / "ambiguous.yaml", profile=PROFILE)
    assert plan.node("ambiguity-triage").params == {"threshold": "medium"}
