"""The live agent worker: `agent` nodes, backed by the Anthropic API.

Handles the kind that produces a **schema-constrained artifact and touches no
filesystem** — `intake` and `design`. Structured output does real work here: the
response is validated against a Pydantic model before it becomes an artifact, so
a malformed register cannot reach a gate at all.

That validation is also the reason a gate can trust anything this worker emits.
The facts it records are `VALIDATOR`-sourced, because a parser confirmed the
shape — not `AGENT`-sourced, which would be the model vouching for itself and
inadmissible under D4. The distinction is narrow and load-bearing: *the artifact
is the subject of the check, never its author.*

`codeagent` nodes are **not** handled here. They need file tools, a permission
layer, and an agent loop — the Claude Agent SDK — and stubbing that with a few
hand-rolled tool calls would contradict D17 and produce a worse version of
something already built. See `CodeAgentWorker` below for the boundary.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from pydantic import BaseModel

from orchestrator.artifacts import SCHEMAS
from orchestrator.config import get_settings
from orchestrator.engine.plan import Node, NodeKind
from orchestrator.gates.facts import Fact, FactSource
from orchestrator.workers.base import (
    ProducedArtifact,
    WorkerError,
    WorkerResult,
    WorkInputs,
    WorkScope,
)

DEFAULT_MAX_TOKENS = 16_000


class AgentWorker:
    """Executes `agent` nodes as a single schema-constrained model call."""

    name = "agent"

    def __init__(
        self,
        client: Any | None = None,
        *,
        prompts_dir: Path | str | None = None,
        max_tokens: int = DEFAULT_MAX_TOKENS,
    ) -> None:
        self._client = client
        self.prompts_dir = (
            Path(prompts_dir) if prompts_dir is not None else get_settings().prompts_dir
        )
        self.max_tokens = max_tokens

    @property
    def client(self) -> Any:
        """Built lazily so importing this module never requires a credential."""
        if self._client is None:
            try:
                import anthropic
            except ImportError as exc:  # pragma: no cover - dependency is declared
                raise WorkerError(
                    "the anthropic package is required for live runs; "
                    "install it or run with ORCHESTRATOR_WORKER=replay"
                ) from exc
            self._client = anthropic.Anthropic(api_key=get_settings().require_api_key())
        return self._client

    def run(self, node: Node, inputs: WorkInputs, scope: WorkScope) -> WorkerResult:
        if node.kind is not NodeKind.AGENT:
            raise WorkerError(
                f"node '{node.id}' is kind '{node.kind}'; AgentWorker handles 'agent' only "
                f"(codeagent needs the Claude Agent SDK)"
            )

        schema = resolve_schema(node)
        prompt = self.prompt_for(node)
        request = self._request(node, schema, prompt, inputs)

        try:
            response = self.client.messages.parse(**request)
        except Exception as exc:  # noqa: BLE001 — any API failure is the ERROR path
            raise WorkerError(f"model call failed for '{node.id}': {exc}") from exc

        parsed = getattr(response, "parsed_output", None)
        if parsed is None:
            # A refusal or a truncated response leaves nothing to gate on.
            raise WorkerError(
                f"'{node.id}' returned no parsed output "
                f"(stop_reason={getattr(response, 'stop_reason', 'unknown')})"
            )

        return WorkerResult(
            facts=self._facts(node, response),
            artifacts=project(node, parsed),
            consumed=tuple(sorted(inputs)),
            model=node.model,
            prompt_ref=str(self._prompt_path(node)),
        )

    # ------------------------------------------------------------------ #
    # request construction
    # ------------------------------------------------------------------ #

    def _request(
        self, node: Node, schema: type[BaseModel], prompt: str, inputs: WorkInputs
    ) -> dict[str, Any]:
        request: dict[str, Any] = {
            "model": node.model or "claude-opus-5",
            "max_tokens": self.max_tokens,
            "system": prompt,
            "messages": [{"role": "user", "content": render_inputs(inputs)}],
            "output_format": schema,
            "thinking": {"type": "adaptive"},
        }
        if node.effort:
            request["output_config"] = {"effort": str(node.effort)}
        return request

    def _prompt_path(self, node: Node) -> Path:
        return self.prompts_dir / f"{node.role}.md"

    def prompt_for(self, node: Node) -> str:
        """Load the role prompt.

        A missing prompt is a worker error rather than a silent default: an agent
        run without its instructions would produce something plausible and wrong,
        which is the most expensive kind of failure here.
        """
        path = self._prompt_path(node)
        if not path.exists():
            raise WorkerError(
                f"node '{node.id}' has role '{node.role}' but {path} does not exist"
            )
        return path.read_text()

    def _facts(self, node: Node, response: Any) -> dict[str, Fact]:
        """Evidence about the call, none of it the model's own claim.

        `parsed` is VALIDATOR-sourced because a parser confirmed the shape.
        Token counts are TOOL-sourced measurements. Neither is the model
        asserting its work was good (D4).
        """
        facts = {
            f"{node.id}.parsed": Fact(True, FactSource.VALIDATOR, "pydantic"),
        }
        usage = getattr(response, "usage", None)
        if usage is not None:
            for field in ("input_tokens", "output_tokens"):
                value = getattr(usage, field, None)
                if value is not None:
                    facts[f"{node.id}.{field}"] = Fact(value, FactSource.TOOL, "anthropic")
        return facts


class CodeAgentWorker:
    """Not implemented — the boundary is deliberate.

    A `codeagent` node needs an agent loop with file tools and a permission layer
    that can enforce `write_scope` and `freeze_paths`. That is the Claude Agent
    SDK's job (D17: build what's graded, buy what isn't), and D6/D7 are only real
    if the runtime *enforces* them rather than the plan merely declaring them.

    Hand-rolling a few tool calls here would produce a worse version of something
    that already exists, and — worse — a permission boundary that looks enforced
    and is not.
    """

    name = "codeagent"

    def run(self, node: Node, inputs: WorkInputs, scope: WorkScope) -> WorkerResult:
        raise WorkerError(
            f"'{node.id}' is a codeagent node, which needs the Claude Agent SDK for its "
            f"file tools and permission layer. Not yet integrated — run with "
            f"ORCHESTRATOR_WORKER=replay, or use stub to walk the graph's shape."
        )


# --------------------------------------------------------------------------- #
# helpers
# --------------------------------------------------------------------------- #


def resolve_schema(node: Node) -> type[BaseModel]:
    """Map a node's declared `output_schema` to the model that defines it.

    Schemas are derived from these models (D8), so the contract an agent is held
    to and the contract a predicate reads are the same object.
    """
    if not node.output_schema:
        raise WorkerError(
            f"node '{node.id}' is an agent node with no output_schema; a model call "
            f"with no contract cannot produce a gateable artifact"
        )

    name = Path(node.output_schema).stem
    schema = SCHEMAS.get(name)
    if schema is None:
        raise WorkerError(
            f"node '{node.id}' declares schema '{name}', which is not registered. "
            f"Known: {', '.join(sorted(SCHEMAS))}"
        )
    return schema


def project(node: Node, parsed: BaseModel) -> tuple[ProducedArtifact, ...]:
    """Split one structured response into the artifacts the plan declares.

    An output whose name matches a field is that field; anything else is the
    whole model. So `design` yields `design.spec` (everything) and
    `design.modules` (the list the fan-out reads) from a single call.
    """
    outputs = node.outputs or ["artifact"]
    whole = parsed.model_dump(mode="json")
    produced: list[ProducedArtifact] = []

    for output in outputs:
        body = whole[output] if output in type(parsed).model_fields else whole
        produced.append(
            ProducedArtifact(f"{node.id}.{output}", json.dumps(body, indent=2, sort_keys=True))
        )

    return tuple(produced)


def render_inputs(inputs: WorkInputs) -> str:
    """Lay the material out so the boundaries between sources are unambiguous."""
    if not inputs:
        return "No inputs were provided."

    sections = []
    for name in sorted(inputs):
        sections.append(f"<{name}>\n{inputs[name]}\n</{name}>")
    return "\n\n".join(sections)
