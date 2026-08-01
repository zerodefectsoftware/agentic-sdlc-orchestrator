"""Environment configuration.

The line this file draws: **the environment says who you are and where things
live; the plan says what the system does.**

So the model and effort are *not* here — they are per-node fields in the plan
graph (D16), because a run's cost profile belongs in the artifact that describes
the run, and per-node effort would have nowhere to go in a flat env file. What
lives here is credentials, paths, and the worker switch.

Nothing imports these settings at module scope. Constructors take explicit
arguments and fall back to settings only when given none, so tests stay
hermetic and the CLI stays the composition root.
"""

from __future__ import annotations

from enum import StrEnum
from functools import lru_cache
from pathlib import Path

from pydantic import AliasChoices, Field
from pydantic_settings import BaseSettings, SettingsConfigDict


class WorkerMode(StrEnum):
    """Which worker executes nodes.

    `replay` is the one worth knowing about: it serves recorded fixtures, so a
    demo runs identically every time with no API calls and no latency (D18).
    """

    STUB = "stub"      # scripted results; tests
    REPLAY = "replay"  # recorded fixtures; deterministic demos
    LIVE = "live"      # real models and subprocesses


class MissingCredential(RuntimeError):
    """A live run was requested without the credential it needs."""


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_prefix="ORCHESTRATOR_",
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
        protected_namespaces=(),
    )

    # Credentials — unprefixed, because the SDK and every other tool expect
    # this exact name and a second spelling would be a trap.
    anthropic_api_key: str | None = Field(
        default=None, validation_alias=AliasChoices("ANTHROPIC_API_KEY")
    )

    worker: WorkerMode = WorkerMode.STUB

    runs_dir: Path = Path("runs")
    fixtures_dir: Path = Path("fixtures")
    plans_dir: Path = Path("plans")
    prompts_dir: Path = Path("prompts")

    db_url: str | None = None
    tool_timeout: int = 600
    max_workers: int = 4

    @property
    def database_url(self) -> str:
        """Defaults inside the runs directory, so pointing elsewhere moves both."""
        return self.db_url or f"sqlite:///{self.runs_dir}/orchestrator.db"

    @property
    def is_live(self) -> bool:
        return self.worker is WorkerMode.LIVE

    def require_api_key(self) -> str:
        """Fail early and legibly rather than deep inside an SDK call."""
        if not self.anthropic_api_key:
            raise MissingCredential(
                "ANTHROPIC_API_KEY is not set, but ORCHESTRATOR_WORKER=live. "
                "Set it in .env (see .env.example), or run with "
                "ORCHESTRATOR_WORKER=replay to use recorded fixtures instead."
            )
        return self.anthropic_api_key


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    return Settings()


def reset_settings() -> None:
    """Drop the cache — for tests that change the environment."""
    get_settings.cache_clear()
