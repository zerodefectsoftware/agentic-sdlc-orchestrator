"""Agent worker tests.

No network. A fake client stands in for the SDK, because the point of these is
the *contract* around the call — what the worker sends, what it records, and
what it refuses — not that the API works.

The load-bearing assertion is the provenance one: everything this worker records
is VALIDATOR- or TOOL-sourced, never AGENT. A worker that recorded its own model
saying "looks good" would let an agent satisfy its own gate.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from typing import Any

import pytest

from orchestrator.artifacts import (
    AcceptanceCriterion,
    Design,
    DesignElement,
    Module,
    Requirement,
    RequirementRegister,
)
from orchestrator.engine.plan import Node
from orchestrator.gates.facts import FactSource
from orchestrator.workers import LiveWorker, WorkerError, WorkScope
from orchestrator.workers.agent import AgentWorker, project, render_inputs, resolve_schema

SCOPE = WorkScope()


@dataclass
class FakeUsage:
    input_tokens: int = 1200
    output_tokens: int = 800


@dataclass
class FakeResponse:
    parsed_output: Any
    usage: Any = None
    stop_reason: str = "end_turn"


class FakeClient:
    """Records what it was asked, returns what it was told to."""

    def __init__(self, response: Any) -> None:
        self._response = response
        self.requests: list[dict] = []
        self.messages = self

    def parse(self, **kwargs):
        self.requests.append(kwargs)
        if isinstance(self._response, Exception):
            raise self._response
        return self._response


REGISTER = RequirementRegister(
    requirements=[
        Requirement(
            id="R1",
            statement="Submit a long URL, receive a short code",
            acceptance=[AcceptanceCriterion(id="AC1.1", then="201 with a 7-character code")],
        )
    ]
)

DESIGN = Design(
    elements=[DesignElement(id="E1", kind="endpoint", satisfies=["R1"])],
    modules=[Module(name="api", path="api"), Module(name="storage", path="storage")],
    endpoints=["/shorten"],
)


@pytest.fixture
def prompts(tmp_path):
    (tmp_path / "analyst.md").write_text("You turn prose into a register.")
    (tmp_path / "architect.md").write_text("You turn a register into a design.")
    return tmp_path


def intake(**overrides) -> Node:
    payload = {
        "id": "intake",
        "kind": "agent",
        "stage": "requirements",
        "role": "analyst",
        "output_schema": "schemas/requirement_register.json",
        "outputs": ["register"],
        "model": "claude-opus-5",
        "effort": "medium",
    }
    payload.update(overrides)
    return Node.model_validate(payload)


def worker(response, prompts, **kwargs) -> AgentWorker:
    return AgentWorker(FakeClient(response), prompts_dir=prompts, **kwargs)


# --------------------------------------------------------------------------- #
# provenance — the reason a gate can trust this worker at all
# --------------------------------------------------------------------------- #


def test_nothing_this_worker_records_is_an_agent_self_report(prompts):
    """D4: a model vouching for its own output is inadmissible."""
    result = worker(FakeResponse(REGISTER, FakeUsage()), prompts).run(intake(), {}, SCOPE)

    assert result.facts
    assert all(fact.source is not FactSource.AGENT for fact in result.facts.values())


def test_the_parsed_fact_is_attributed_to_the_parser(prompts):
    """A parser confirmed the shape — that is a validator's observation."""
    result = worker(FakeResponse(REGISTER), prompts).run(intake(), {}, SCOPE)

    fact = result.facts["intake.parsed"]
    assert fact.source is FactSource.VALIDATOR
    assert fact.produced_by == "pydantic"


def test_token_usage_is_recorded_as_measurement(prompts):
    result = worker(FakeResponse(REGISTER, FakeUsage()), prompts).run(intake(), {}, SCOPE)

    assert result.facts["intake.input_tokens"].value == 1200
    assert result.facts["intake.input_tokens"].source is FactSource.TOOL


# --------------------------------------------------------------------------- #
# what the worker sends
# --------------------------------------------------------------------------- #


def test_the_request_carries_the_plan_s_model_and_effort(prompts):
    """D16: cost is a property of the plan, not of the code."""
    client = FakeClient(FakeResponse(REGISTER))
    AgentWorker(client, prompts_dir=prompts).run(intake(), {}, SCOPE)

    request = client.requests[0]
    assert request["model"] == "claude-opus-5"
    assert request["output_config"]["effort"] == "medium"
    assert request["thinking"] == {"type": "adaptive"}


def test_the_response_is_constrained_to_the_declared_schema(prompts):
    """A malformed register should never reach a gate."""
    client = FakeClient(FakeResponse(REGISTER))
    AgentWorker(client, prompts_dir=prompts).run(intake(), {}, SCOPE)

    assert client.requests[0]["output_format"] is RequirementRegister


def test_the_role_prompt_becomes_the_system_prompt(prompts):
    client = FakeClient(FakeResponse(REGISTER))
    AgentWorker(client, prompts_dir=prompts).run(intake(), {}, SCOPE)

    assert client.requests[0]["system"] == "You turn prose into a register."


def test_inputs_are_delimited_so_sources_cannot_blur(prompts):
    client = FakeClient(FakeResponse(REGISTER))
    inputs = {"requirement": "Build a shortener.", "intake.register": "{}"}
    AgentWorker(client, prompts_dir=prompts).run(intake(), inputs, SCOPE)

    content = client.requests[0]["messages"][0]["content"]
    assert "<requirement>" in content
    assert "Build a shortener." in content
    assert "<intake.register>" in content


def test_what_was_consumed_is_recorded_for_lineage(prompts):
    result = worker(FakeResponse(REGISTER), prompts).run(
        intake(), {"requirement": "x", "design.spec": "{}"}, SCOPE
    )
    assert result.consumed == ("design.spec", "requirement")


# --------------------------------------------------------------------------- #
# projection
# --------------------------------------------------------------------------- #


def test_one_response_becomes_the_artifacts_the_plan_declares(prompts):
    """`design` yields the whole spec and the module list the fan-out reads."""
    node = Node.model_validate(
        {
            "id": "design",
            "kind": "agent",
            "stage": "design",
            "role": "architect",
            "output_schema": "schemas/design.json",
            "outputs": ["spec", "modules"],
        }
    )
    result = worker(FakeResponse(DESIGN), prompts).run(node, {}, SCOPE)

    names = [artifact.name for artifact in result.artifacts]
    assert names == ["design.spec", "design.modules"]

    modules = json.loads(result.artifact("design.modules").content)
    assert [m["name"] for m in modules] == ["api", "storage"]  # a list the fanout can read

    spec = json.loads(result.artifact("design.spec").content)
    assert "elements" in spec


def test_an_output_that_is_not_a_field_gets_the_whole_model():
    artifacts = project(intake(), REGISTER)
    assert artifacts[0].name == "intake.register"
    assert "requirements" in json.loads(artifacts[0].content)


# --------------------------------------------------------------------------- #
# refusals
# --------------------------------------------------------------------------- #


def test_a_node_with_no_schema_is_refused():
    """A model call with no contract cannot produce a gateable artifact."""
    with pytest.raises(WorkerError, match="no output_schema"):
        resolve_schema(intake(output_schema=None))


def test_an_unknown_schema_names_the_ones_that_exist():
    with pytest.raises(WorkerError, match="not registered"):
        resolve_schema(intake(output_schema="schemas/invented.json"))


def test_a_missing_prompt_is_an_error_not_a_default(prompts, tmp_path):
    """An agent run without its instructions produces something plausible and
    wrong — the most expensive failure available here."""
    with pytest.raises(WorkerError, match="does not exist"):
        worker(FakeResponse(REGISTER), tmp_path / "empty").run(intake(), {}, SCOPE)


def test_an_api_failure_is_a_worker_error(prompts):
    """No facts, so the gate ERRORs — the harness needs attention, not the work."""
    with pytest.raises(WorkerError, match="model call failed"):
        worker(RuntimeError("overloaded"), prompts).run(intake(), {}, SCOPE)


def test_a_refusal_leaves_nothing_to_gate_on(prompts):
    response = FakeResponse(parsed_output=None, stop_reason="refusal")
    with pytest.raises(WorkerError, match="no parsed output"):
        worker(response, prompts).run(intake(), {}, SCOPE)


def test_the_agent_worker_refuses_a_codeagent_node(prompts):
    node = Node.model_validate(
        {
            "id": "impl",
            "kind": "codeagent",
            "stage": "implementation",
            "role": "implementer",
            "write_scope": ["target/**"],
        }
    )
    with pytest.raises(WorkerError, match="Claude Agent SDK"):
        worker(FakeResponse(REGISTER), prompts).run(node, {}, SCOPE)


def test_codeagent_is_unimplemented_rather_than_faked():
    """A permission boundary that looks enforced and is not would be worse than
    an honest gap (D6, D7, D17)."""
    node = Node.model_validate(
        {
            "id": "impl",
            "kind": "codeagent",
            "stage": "implementation",
            "role": "implementer",
            "write_scope": ["target/**"],
        }
    )
    with pytest.raises(WorkerError, match="ORCHESTRATOR_WORKER=replay"):
        LiveWorker().run(node, {}, SCOPE)


# --------------------------------------------------------------------------- #
# dispatch
# --------------------------------------------------------------------------- #


def test_the_live_worker_routes_by_node_kind(prompts):
    live = LiveWorker(agent=AgentWorker(FakeClient(FakeResponse(REGISTER)), prompts_dir=prompts))
    result = live.run(intake(), {}, SCOPE)
    assert result.artifact("intake.register")


def test_human_and_fanout_never_reach_a_worker():
    node = Node.model_validate(
        {"id": "accept", "kind": "human", "stage": "release", "autonomy": "APPROVE"}
    )
    with pytest.raises(WorkerError, match="no live runtime"):
        LiveWorker().run(node, {}, SCOPE)


def test_rendering_no_inputs_is_explicit():
    assert "No inputs" in render_inputs({})
