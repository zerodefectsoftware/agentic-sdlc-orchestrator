"""The target profile: everything the engine knows about a specific codebase.

D3 says `orchestrator` never imports the target. This is what makes that
survivable rather than merely true — the plan says *run the target's test
command*, and this file says what that command is. Retargeting is then a config
change, and nothing in `src/orchestrator/` has to learn a second codebase.

Plans reference profile values as placeholders:

    run: "sh:{target.commands.test_cov}"
    gate:
      all:
        - "coverage.percent >= {target.thresholds.coverage_min}"

Resolution happens at load time, before validation, so `--dry-run` prints the
command that would actually run rather than the placeholder. An unresolvable
placeholder is an error: a plan that silently executed the literal string
`{target.commands.test}` would fail as "command not found", which reads like a
broken environment rather than a broken plan.
"""

from __future__ import annotations

import re
from pathlib import Path
from typing import Any

import yaml
from pydantic import BaseModel, ConfigDict, Field, ValidationError

PLACEHOLDER = re.compile(r"\{(target\.[a-zA-Z0-9_.]+)\}")


class ProfileError(Exception):
    """The profile is missing, malformed, or does not define what a plan asks for."""


class TargetProfile(BaseModel):
    """The contract between a plan and a codebase."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    name: str
    root: str
    tests_root: str
    language: str = "python"
    commands: dict[str, str] = Field(default_factory=dict)
    write_ceiling: list[str] = Field(default_factory=list)
    thresholds: dict[str, Any] = Field(default_factory=dict)

    @classmethod
    def load(cls, path: Path | str) -> TargetProfile:
        path = Path(path)
        if not path.exists():
            raise ProfileError(f"{path}: target profile not found")
        try:
            raw = yaml.safe_load(path.read_text())
        except yaml.YAMLError as exc:
            raise ProfileError(f"{path}: invalid YAML: {exc}") from exc
        if not isinstance(raw, dict) or "target" not in raw:
            raise ProfileError(f"{path}: profile must be a mapping with a 'target' key")
        try:
            return cls.model_validate(raw["target"])
        except ValidationError as exc:
            fields = ", ".join(".".join(str(x) for x in e["loc"]) for e in exc.errors())
            raise ProfileError(f"{path}: profile is incomplete or unknown ({fields})") from exc

    def lookup(self, dotted: str) -> Any:
        """Resolve `target.commands.test` against this profile."""
        cursor: Any = {"target": self.model_dump()}
        for part in dotted.split("."):
            if not isinstance(cursor, dict) or part not in cursor:
                raise ProfileError(
                    f"the plan references '{{{dotted}}}', which profile "
                    f"'{self.name}' does not define"
                )
            cursor = cursor[part]
        return cursor

    def resolve(self, value: Any) -> Any:
        """Substitute `{target.*}` placeholders anywhere in a loaded plan.

        Only `target.` placeholders are touched. `{item.path}` belongs to fanout
        materialisation and is resolved per item at runtime, so leaving it alone
        here is deliberate rather than an oversight.
        """
        if isinstance(value, dict):
            return {key: self.resolve(item) for key, item in value.items()}
        if isinstance(value, list):
            return [self.resolve(item) for item in value]
        if isinstance(value, str):
            return self._resolve_string(value)
        return value

    def _resolve_string(self, value: str) -> Any:
        match = PLACEHOLDER.fullmatch(value)
        if match:
            # A whole-string placeholder keeps its type: a coverage threshold
            # should reach a gate expression as 80, not "80".
            return self.lookup(match.group(1))
        return PLACEHOLDER.sub(lambda m: str(self.lookup(m.group(1))), value)
