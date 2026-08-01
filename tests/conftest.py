"""Suite-wide hermeticity.

`Settings` reads `.env`, which is exactly right for an operator and exactly
wrong for a test: whoever has a real key in theirs would see a different suite
than whoever does not. That is not hypothetical — a `.env` with
`ORCHESTRATOR_WORKER=live` turns a passing suite red, on a machine where nothing
is broken.

So the suite reads no `.env`, carries no credential, and writes its runs to a
temporary directory. Anything a test needs about the environment, it sets
itself.
"""

from __future__ import annotations

import pytest

from orchestrator.config import Settings, reset_settings


@pytest.fixture(autouse=True)
def hermetic_settings(monkeypatch, tmp_path_factory):
    monkeypatch.setitem(Settings.model_config, "env_file", None)
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)

    # And nothing writes to the real runs directory. Anything that defaults to
    # `settings.runs_dir` — an ArtifactStore, a Store — was quietly depositing
    # run folders next to real ones, which makes "what did this run produce"
    # unanswerable on a machine where the suite has been run.
    monkeypatch.setenv("ORCHESTRATOR_RUNS_DIR", str(tmp_path_factory.mktemp("runs")))

    reset_settings()
    yield
    reset_settings()
