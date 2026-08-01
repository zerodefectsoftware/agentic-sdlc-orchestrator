"""The code agent worker: `codeagent` nodes, backed by the Claude Agent SDK.

The kind that touches a filesystem — `scaffold`, `impl:*`, `tests-acceptance`,
`docs`, and inserted `fix` nodes.

This is where D6 and D7 stop being declarations. `ClaudeAgentOptions.can_use_tool`
is a permission callback the SDK consults before every tool call, so the
`WorkScope` the plan declared becomes the thing that actually decides whether a
write happens. `impl:api` cannot write `impl:storage`'s directory because the
runtime refuses, not because the prompt asked nicely.

The callback both enforces and observes: denials and writes flow through the same
place, so a run records *"the agent attempted 3 writes outside its scope"* as
evidence rather than losing it.

**Bash is disallowed here, and that is the interesting constraint.** A scope
guard can inspect a `Write` tool's `file_path`; it cannot parse an arbitrary
shell command and know what it will touch. Allowing Bash would leave a hole
through which every scope is escapable, and a permission boundary with a known
hole is worse than none — it reads as enforced. The cost is real: the agent
cannot run its own tests. That is already the design, since verification is a
separate `tool` node whose facts a gate can trust (D4).
"""

from __future__ import annotations

import asyncio
import json
import warnings
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from orchestrator.config import get_settings
from orchestrator.engine.plan import Node, NodeKind
from orchestrator.gates.facts import Fact, FactSource
from orchestrator.workers.agent import render_inputs
from orchestrator.workers.base import (
    ProducedArtifact,
    WorkerError,
    WorkerResult,
    WorkInputs,
    WorkScope,
)

# Tools whose target a scope guard can actually inspect. Anything that can write
# by an unlisted route — Bash above all — is refused for codeagent nodes.
WRITE_TOOLS = ("Write", "Edit", "MultiEdit", "NotebookEdit")
READ_TOOLS = ("Read", "Glob", "Grep")
FORBIDDEN_TOOLS = ("Bash", "WebFetch", "WebSearch", "Task")

PATH_KEYS = ("file_path", "path", "notebook_path")

DEFAULT_MAX_TURNS = 40
DEFAULT_TIMEOUT_S = 900


@dataclass
class ScopeGuard:
    """Decides, and remembers, every write a node attempts.

    Enforcement and observation in one place: a denial is both refused and
    recorded, so the evidence bundle can show that an agent tried to leave its
    blast radius (D7) or edit the tests judging it (D6).
    """

    scope: WorkScope
    cwd: Path
    written: list[str] = field(default_factory=list)
    denied: list[str] = field(default_factory=list)

    def relativise(self, raw: str) -> str:
        """Scopes are repo-relative globs; the SDK reports absolute paths."""
        path = Path(raw)
        if not path.is_absolute():
            return str(path)
        try:
            return str(path.resolve().relative_to(self.cwd.resolve()))
        except ValueError:
            return str(path)  # outside the tree entirely — no scope will permit it

    def decide(self, tool_name: str, payload: dict[str, Any]) -> tuple[bool, str]:
        if tool_name in FORBIDDEN_TOOLS:
            return False, (
                f"{tool_name} is not available to a scoped code agent: its effects "
                f"cannot be checked against a write scope"
            )

        if tool_name not in WRITE_TOOLS:
            return True, ""

        target = next((payload[key] for key in PATH_KEYS if payload.get(key)), None)
        if target is None:
            return False, f"{tool_name} was called without a path to check"

        relative = self.relativise(str(target))
        if not self.scope.permits(relative):
            self.denied.append(relative)
            return False, (
                f"'{relative}' is outside this node's write scope "
                f"{list(self.scope.allowed)}"
                + (f" or is frozen {list(self.scope.frozen)}" if self.scope.frozen else "")
            )

        self.written.append(relative)
        return True, ""


def _refuse_if_guard_was_shadowed(node: Node, raised: list) -> None:
    """Stop the run if the SDK reports that the permission callback was skipped.

    The SDK says so out loud — `CanUseToolShadowedWarning` — and a warning on
    stderr is not a control. A scoped agent whose scope silently did not apply
    is worse than an unscoped one, because the evidence bundle would record a
    blast radius that was never enforced. So this is an ERROR: the work may have
    happened, but nothing about it can be trusted, and D7 is a claim the run can
    no longer make.
    """
    shadowed = [
        str(warning.message)
        for warning in raised
        if "CanUseToolShadowed" in type(warning.message).__name__
        # Read tools are auto-approved deliberately: the guard exists to decide
        # writes, and refusing a session because `Grep` skipped the callback
        # would throw away work the guard actually governed. Only a shadowed
        # *write* tool means the scope did not apply.
        and any(tool in str(warning.message) for tool in WRITE_TOOLS)
    ]
    if shadowed:
        raise WorkerError(
            f"the scope guard for '{node.id}' was bypassed by the tool "
            f"configuration — refusing to trust this session. {shadowed[0]}"
        )


async def _streamed(text: str):
    """The one-message streaming form the permission callback requires."""
    yield {
        "type": "user",
        "message": {"role": "user", "content": text},
        "parent_tool_use_id": None,
        "session_id": "default",
    }


class CodeAgentWorker:
    """Executes `codeagent` nodes as a scoped Claude Agent SDK session."""

    name = "codeagent"

    def __init__(
        self,
        *,
        cwd: Path | str = ".",
        prompts_dir: Path | str | None = None,
        max_turns: int = DEFAULT_MAX_TURNS,
        timeout_s: int = DEFAULT_TIMEOUT_S,
        session: Any | None = None,
    ) -> None:
        self.cwd = Path(cwd)
        self.prompts_dir = (
            Path(prompts_dir) if prompts_dir is not None else get_settings().prompts_dir
        )
        self.max_turns = max_turns
        self.timeout_s = timeout_s
        self._session = session  # injectable, so tests never spawn an agent

    def run(self, node: Node, inputs: WorkInputs, scope: WorkScope) -> WorkerResult:
        if node.kind is not NodeKind.CODEAGENT:
            raise WorkerError(
                f"node '{node.id}' is kind '{node.kind}'; CodeAgentWorker handles "
                f"'codeagent' only"
            )
        if not scope.allowed:
            # An unscoped code agent may write anywhere, which is the one thing
            # D7 exists to prevent. Refuse rather than default to something wide.
            raise WorkerError(
                f"node '{node.id}' declares no write_scope; a code agent with no "
                f"blast radius is not something this system will run"
            )

        guard = ScopeGuard(scope=scope, cwd=self.cwd)
        prompt = self._prompt_for(node)

        try:
            with warnings.catch_warnings(record=True) as raised:
                warnings.simplefilter("always")
                outcome = asyncio.run(
                    asyncio.wait_for(
                        self._converse(node, prompt, inputs, guard), timeout=self.timeout_s
                    )
                )
            _refuse_if_guard_was_shadowed(node, raised)
        except TimeoutError as exc:
            raise WorkerError(
                f"'{node.id}' exceeded {self.timeout_s}s. An unbounded agent loop has "
                f"no MTTR; the turn budget was {self.max_turns}."
            ) from exc
        except WorkerError:
            raise
        except Exception as exc:  # noqa: BLE001 — an SDK failure is the ERROR path
            raise WorkerError(f"agent session failed for '{node.id}': {exc}") from exc

        return WorkerResult(
            facts=self._facts(node, guard, outcome),
            artifacts=(self._changeset(node, guard), *self._declared(node)),
            consumed=tuple(sorted(inputs)),
            model=node.model,
            prompt_ref=str(self._prompt_path(node)),
        )

    # ------------------------------------------------------------------ #
    # the session
    # ------------------------------------------------------------------ #

    async def _converse(
        self, node: Node, prompt: str, inputs: WorkInputs, guard: ScopeGuard
    ) -> dict[str, Any]:
        """Drive one scoped session to completion.

        The input is streamed rather than passed as a string, because that is
        what `can_use_tool` requires — the SDK refuses the callback outright in
        single-shot mode. Since the callback *is* how D6 and D7 are enforced,
        the streaming form is not a preference here: a string prompt would mean
        an unscoped agent, which is the one thing this worker will not run.
        """
        query, options = self._sdk(node, prompt, guard)

        terminal_reason = None
        subtype = None
        turns = 0

        async for message in query(prompt=_streamed(render_inputs(inputs)), options=options):
            turns += 1
            # Identified by shape rather than by imported type: this module must
            # not require the SDK to be installed in order to be tested.
            if hasattr(message, "terminal_reason"):
                terminal_reason = message.terminal_reason
                subtype = getattr(message, "subtype", None)

        return {"terminal_reason": terminal_reason, "subtype": subtype, "turns": turns}

    def _sdk(self, node: Node, prompt: str, guard: ScopeGuard):
        """Build the SDK call, or use the injected one.

        Imported lazily so the package is only required for live runs.
        """
        if self._session is not None:
            return self._session(node, prompt, guard)

        try:
            from claude_agent_sdk import ClaudeAgentOptions, query
            from claude_agent_sdk.types import PermissionResultAllow, PermissionResultDeny
        except ImportError as exc:  # pragma: no cover — declared dependency
            raise WorkerError(
                "claude-agent-sdk is required for live codeagent nodes; "
                "install it or run with ORCHESTRATOR_WORKER=replay"
            ) from exc

        async def can_use_tool(tool_name: str, payload: dict, context: Any):
            allowed, reason = guard.decide(tool_name, payload)
            if allowed:
                return PermissionResultAllow(updated_input=payload)
            # `interrupt=False`: one refused write should not end the session.
            # The agent is told why and can choose a path inside its scope.
            return PermissionResultDeny(message=reason, interrupt=False)

        options = ClaudeAgentOptions(
            **self._option_values(node, prompt), can_use_tool=can_use_tool
        )
        return query, options

    def _option_values(self, node: Node, prompt: str) -> dict[str, Any]:
        """Every SDK option except the callback, which cannot be previewed.

        Shared with `describe` so a dry run reports the configuration a live run
        would actually use — a preview built from a second, parallel definition
        would drift the first time either changed.
        """
        values: dict[str, Any] = {
            "cwd": str(self.cwd),
            "model": node.model,
            "system_prompt": prompt,
            # Reads only. A tool named here is auto-approved *before* the
            # permission callback is consulted, so listing the write tools would
            # hand the agent a scope guard that never runs — the enforcement in
            # this file would read as enforced and do nothing. Writes are left
            # unlisted so every one of them falls through to `can_use_tool`.
            "allowed_tools": list(READ_TOOLS),
            "disallowed_tools": list(FORBIDDEN_TOOLS),
            "max_turns": self.max_turns,
        }
        if node.effort:
            values["effort"] = str(node.effort)
        return values

    def describe(self, node: Node, inputs: WorkInputs, scope: WorkScope) -> dict[str, Any]:
        """What this node would run, without running it.

        Needs neither the SDK nor a credential, which matters: the configuration
        is inspectable on a machine where the package is not installed at all.

        Problems are reported rather than raised, so one dry run surfaces every
        issue rather than stopping at the first.
        """
        issues: list[str] = []
        prompt = ""

        try:
            prompt = self._prompt_for(node)
        except WorkerError as exc:
            issues.append(str(exc))
        if not scope.allowed:
            issues.append(f"node '{node.id}' declares no write_scope")

        values = self._option_values(node, prompt)
        return {
            "worker": self.name,
            "issues": issues,
            "prompt": str(self._prompt_path(node)),
            "model": values["model"] or "(inherited)",
            "effort": values.get("effort", "(default)"),
            "cwd": values["cwd"],
            "max_turns": values["max_turns"],
            "timeout_s": self.timeout_s,
            "allowed_tools": values["allowed_tools"],
            "disallowed_tools": values["disallowed_tools"],
            "may_write": list(scope.allowed),
            "frozen": list(scope.frozen),
            "inputs": sorted(inputs),
        }

    # ------------------------------------------------------------------ #
    # what the run records
    # ------------------------------------------------------------------ #

    def _facts(self, node: Node, guard: ScopeGuard, outcome: dict[str, Any]):
        """Observations about the session — never a claim about the work.

        Whether the code is any good is decided by `ruff` and `pytest` in a
        separate node, whose facts a gate can trust (D4). What belongs here is
        what the harness watched happen.
        """
        return {
            f"{node.id}.session_ended": Fact(
                outcome.get("terminal_reason") or "unknown", FactSource.TOOL, "claude-agent-sdk"
            ),
            f"{node.id}.turns": Fact(outcome.get("turns", 0), FactSource.TOOL, "claude-agent-sdk"),
            f"{node.id}.files_written": Fact(
                len(set(guard.written)), FactSource.VALIDATOR, "scope-guard"
            ),
            f"{node.id}.scope_denials": Fact(
                len(guard.denied), FactSource.VALIDATOR, "scope-guard"
            ),
        }

    def _declared(self, node: Node) -> tuple[ProducedArtifact, ...]:
        """Read the files the plan says this node produces.

        A code agent writes files; gates read artifacts. Without this the two
        never meet: `tests-acceptance` declares `outputs: [suite]`, the agent
        writes the suite to disk, and `every_ac_has_a_test` then ERRORs because
        no artifact by that name was ever recorded — which is what happened on
        the first session that ran to completion.

        A declared file that is missing is a worker error, not an empty
        artifact. The plan asked for it; a silent absence would reach the gate
        as "the check could not be performed" and hide whose fault that was.
        """
        produced: list[ProducedArtifact] = []
        for output, relative in node.output_files.items():
            path = self.cwd / relative
            if not path.exists():
                raise WorkerError(
                    f"'{node.id}' declares output '{output}' at {relative}, which the "
                    f"session did not write. The role prompt has to say where it goes."
                )
            produced.append(ProducedArtifact(f"{node.id}.{output}", path.read_text()))
        return tuple(produced)

    def _changeset(self, node: Node, guard: ScopeGuard) -> ProducedArtifact:
        """A manifest of what this node changed, and what it was stopped from changing."""
        body = {
            "written": sorted(set(guard.written)),
            "denied": sorted(set(guard.denied)),
            "write_scope": list(guard.scope.allowed),
            "frozen": list(guard.scope.frozen),
        }
        return ProducedArtifact(
            f"{node.id}.changeset", json.dumps(body, indent=2, sort_keys=True)
        )

    def _prompt_path(self, node: Node) -> Path:
        return self.prompts_dir / f"{node.role}.md"

    def _prompt_for(self, node: Node) -> str:
        path = self._prompt_path(node)
        if not path.exists():
            raise WorkerError(
                f"node '{node.id}' has role '{node.role}' but {path} does not exist"
            )
        return path.read_text()
