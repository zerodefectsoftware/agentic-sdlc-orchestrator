"""Artifact bodies on disk.

The database records an artifact's identity — name, version, content hash,
producer — and this stores what it actually said. Keeping bodies out of the
database keeps run state small and greppable, and it means a reviewer can read
an artifact with `cat` rather than a SQL client.

Laid out by name and version rather than by hash:

    runs/<run_id>/artifacts/design.openapi/v1
    runs/<run_id>/artifacts/design.openapi/v2

Content addressing would deduplicate, but browsability matters more here. The
whole point of the evidence bundle is that a person can look at it, and
`v1` next to `v2` shows a re-derivation at a glance where two hashes would not.
"""

from __future__ import annotations

from pathlib import Path

from orchestrator.state.models import Artifact


class ArtifactStore:
    def __init__(self, root: Path | str = "runs") -> None:
        self.root = Path(root)

    def path_for(self, run_id: str, name: str, version: int) -> Path:
        return self.root / run_id / "artifacts" / name / f"v{version}"

    def write(self, run_id: str, name: str, version: int, content: str) -> Path:
        path = self.path_for(run_id, name, version)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(content)
        return path

    def read(self, artifact: Artifact) -> str:
        """Read a recorded artifact's body.

        Raises rather than returning empty: an artifact whose body is missing is
        a broken run, and a caller that silently proceeded on `""` would produce
        a fanout with zero children or an evidence bundle with a blank section.
        """
        path = Path(artifact.path) if artifact.path else self.path_for(
            artifact.run_id, artifact.name, artifact.version
        )
        if not path.exists():
            raise FileNotFoundError(
                f"artifact {artifact.ref} is recorded but its body is missing at {path}"
            )
        return path.read_text()
