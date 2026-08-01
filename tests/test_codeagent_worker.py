"""Code agent worker tests.

No SDK, no agent, no network — the session is injected. What these check is the
part that matters: **the scope guard is the thing that decides whether a write
happens.** D6 and D7 are only real if the runtime refuses, and this is the
runtime.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from orchestrator.engine.plan import Node
from orchestrator.gates.facts import FactSource
from orchestrator.workers import WorkerError, WorkScope
from orchestrator.workers.codeagent import (
    FORBIDDEN_TOOLS,
    CodeAgentWorker,
    ScopeGuard,
)


@pytest.fixture
def prompts(tmp_path):
    for role in ("implementer", "fixer"):
        (tmp_path / f"{role}.md").write_text(f"You are the {role}.")
    return tmp_path


def node(**overrides) -> Node:
    payload = {
        "id": "impl:api",
        "kind": "codeagent",
        "stage": "implementation",
        "role": "implementer",
        "write_scope": ["target/shortener/api/**"],
    }
    payload.update(overrides)
    return Node.model_validate(payload)


def scope(**overrides) -> WorkScope:
    payload = {"allowed": ("target/shortener/api/**",)}
    payload.update(overrides)
    return WorkScope(**payload)


def guard(**overrides) -> ScopeGuard:
    return ScopeGuard(scope=overrides.pop("scope", scope()), cwd=Path("/repo"), **overrides)


# --------------------------------------------------------------------------- #
# the guard is the enforcement point
# --------------------------------------------------------------------------- #


def test_a_write_inside_scope_is_allowed():
    allowed, _ = guard().decide("Write", {"file_path": "target/shortener/api/routes.py"})
    assert allowed


def test_a_write_into_a_neighbours_module_is_refused():
    """D7: blast radius. `impl:api` cannot reach `impl:storage`."""
    g = guard()
    allowed, reason = g.decide("Write", {"file_path": "target/shortener/storage/db.py"})

    assert not allowed
    assert "outside this node's write scope" in reason
    assert g.denied == ["target/shortener/storage/db.py"]


def test_a_write_to_a_frozen_path_is_refused():
    """D6: during repair the cheapest green suite is a weakened test."""
    g = guard(scope=WorkScope(allowed=("target/**",), frozen=("target/tests/**",)))
    allowed, reason = g.decide("Edit", {"file_path": "target/tests/test_api.py"})

    assert not allowed
    assert "frozen" in reason


def test_absolute_paths_are_checked_against_the_repo(tmp_path):
    """The SDK reports absolute paths; scopes are repo-relative globs."""
    g = ScopeGuard(scope=scope(), cwd=Path("/repo"))
    inside, _ = g.decide("Write", {"file_path": "/repo/target/shortener/api/routes.py"})
    outside, _ = g.decide("Write", {"file_path": "/etc/passwd"})

    assert inside
    assert not outside


@pytest.mark.parametrize("tool", FORBIDDEN_TOOLS)
def test_tools_whose_effects_cannot_be_checked_are_refused(tool):
    """A scope guard can read a Write's file_path. It cannot parse a shell
    command — and a boundary with a known hole reads as enforced when it is not."""
    allowed, reason = guard().decide(tool, {"command": "rm -rf /"})
    assert not allowed
    assert "cannot be checked" in reason


def test_reads_are_not_scoped():
    """A node has to understand the design it is implementing."""
    allowed, _ = guard().decide("Read", {"file_path": "target/shortener/storage/db.py"})
    assert allowed


def test_a_write_with_no_path_is_refused():
    """Unable to check is not the same as permitted."""
    allowed, reason = guard().decide("Write", {"content": "x"})
    assert not allowed
    assert "without a path" in reason


def test_the_guard_records_what_was_written_as_well_as_denied():
    g = guard()
    g.decide("Write", {"file_path": "target/shortener/api/a.py"})
    g.decide("Write", {"file_path": "target/shortener/api/b.py"})
    g.decide("Write", {"file_path": "src/orchestrator/engine/loader.py"})

    assert g.written == ["target/shortener/api/a.py", "target/shortener/api/b.py"]
    assert g.denied == ["src/orchestrator/engine/loader.py"]


# --------------------------------------------------------------------------- #
# the session
# --------------------------------------------------------------------------- #


class ResultMessage:
    """Stands in for the SDK's ResultMessage — matched by shape."""

    def __init__(self, terminal_reason="stop", subtype="success"):
        self.terminal_reason = terminal_reason
        self.subtype = subtype


def session_writing(paths: list[str], *, terminal_reason: str = "stop"):
    """An injected session that performs writes through the guard, as the SDK would."""

    def build(node, prompt, guard):
        async def query(*, prompt, options):
            for path in paths:
                guard.decide("Write", {"file_path": path})
            yield ResultMessage(terminal_reason=terminal_reason)

        return query, {"prompt": prompt}

    return build


def worker(session, prompts, **kwargs) -> CodeAgentWorker:
    return CodeAgentWorker(cwd=Path("/repo"), prompts_dir=prompts, session=session, **kwargs)


def test_a_session_records_what_it_changed(prompts):
    session = session_writing(["target/shortener/api/routes.py"])
    result = worker(session, prompts).run(node(), {}, scope())

    changeset = json.loads(result.artifact("impl:api.changeset").content)
    assert changeset["written"] == ["target/shortener/api/routes.py"]
    assert changeset["denied"] == []
    assert changeset["write_scope"] == ["target/shortener/api/**"]


def test_attempts_to_escape_the_scope_survive_into_the_evidence(prompts):
    """Losing this would discard the most interesting thing that happened."""
    session = session_writing(
        ["target/shortener/api/routes.py", "target/shortener/storage/db.py"]
    )
    result = worker(session, prompts).run(node(), {}, scope())

    changeset = json.loads(result.artifact("impl:api.changeset").content)
    assert changeset["denied"] == ["target/shortener/storage/db.py"]
    assert result.facts["impl:api.scope_denials"].value == 1


def test_the_facts_describe_the_session_not_the_quality_of_the_work(prompts):
    """Whether the code is good is decided by ruff and pytest in another node (D4)."""
    result = worker(session_writing(["target/shortener/api/a.py"]), prompts).run(
        node(), {}, scope()
    )

    assert result.facts["impl:api.session_ended"].value == "stop"
    assert result.facts["impl:api.files_written"].value == 1
    assert all(fact.source is not FactSource.AGENT for fact in result.facts.values())
    assert result.facts["impl:api.scope_denials"].source is FactSource.VALIDATOR


def test_the_prompt_reference_is_recorded_for_lineage(prompts):
    result = worker(session_writing([]), prompts).run(node(), {}, scope())
    assert result.prompt_ref.endswith("implementer.md")


# --------------------------------------------------------------------------- #
# refusals
# --------------------------------------------------------------------------- #


def test_a_node_with_no_write_scope_is_refused(prompts):
    """A code agent with no blast radius is the one thing D7 exists to prevent."""
    unscoped = node(write_scope=["target/**"])  # plan requires one; scope arrives empty
    with pytest.raises(WorkerError, match="no write_scope"):
        worker(session_writing([]), prompts).run(unscoped, {}, WorkScope())


def test_a_missing_role_prompt_is_an_error(prompts, tmp_path):
    with pytest.raises(WorkerError, match="does not exist"):
        worker(session_writing([]), tmp_path / "empty").run(node(), {}, scope())


def test_a_non_codeagent_node_is_refused(prompts):
    agent = Node.model_validate(
        {"id": "intake", "kind": "agent", "stage": "requirements", "role": "analyst"}
    )
    with pytest.raises(WorkerError, match="handles 'codeagent' only"):
        worker(session_writing([]), prompts).run(agent, {}, scope())


def test_an_sdk_failure_is_a_worker_error(prompts):
    def exploding(node, prompt, guard):
        async def query(*, prompt, options):
            raise RuntimeError("transport died")
            yield  # pragma: no cover

        return query, {}

    with pytest.raises(WorkerError, match="agent session failed"):
        worker(exploding, prompts).run(node(), {}, scope())


def test_a_session_that_never_ends_is_bounded(prompts):
    """An unbounded agent loop has no MTTR and no safe-stop."""
    import asyncio

    def hanging(node, prompt, guard):
        async def query(*, prompt, options):
            await asyncio.sleep(5)
            yield ResultMessage()

        return query, {}

    with pytest.raises(WorkerError, match="exceeded"):
        worker(hanging, prompts, timeout_s=1).run(node(), {}, scope())
