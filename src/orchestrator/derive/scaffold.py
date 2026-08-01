"""Scaffolding a target from its design.

Turns the design's module list into importable stubs: one package per module,
each with a docstring naming the responsibility the design assigned it and the
requirements it exists to satisfy.

Deliberately stubs and nothing more. The scaffold's gate checks that imports
resolve and the tree lints — anything cleverer here would be implementation, and
implementation is judged by an acceptance suite this step must not pre-empt.
"""

from __future__ import annotations

import textwrap

from orchestrator.artifacts import Design
from orchestrator.workers.pytask import Task, TaskOutput

HEADER = (
    '"""{title}\n\n{body}\n\n'
    "Generated from the design. Implementation belongs to the module's own node.\n"
    '"""\n'
)

# Derived code is judged by the same lint gate as written code, so it has to
# satisfy it. A module responsibility is prose an architect wrote to a length
# nobody constrained — pasted onto one line it blew the line-length rule, and
# the scaffold gate then failed deterministically, forever, on the generator's
# own output rather than on anything an agent did.
LINE_LENGTH = 96


def scaffold_from_design(task: Task) -> TaskOutput:
    design = Design.model_validate_json(task.require("design.spec"))
    root = _package_root(task)

    files: dict[str, str] = {
        f"{root}/__init__.py": HEADER.format(
            title="The target package.",
            body=_wrap(f"Modules: {', '.join(m.name for m in design.modules) or '(none)'}"),
        )
    }

    for module in design.modules:
        satisfied = sorted(
            {
                requirement
                for element in design.elements
                if element.kind == "module" and element.summary == module.name
                for requirement in element.satisfies
            }
        )
        files[f"{root}/{module.path}/__init__.py"] = HEADER.format(
            title=_wrap(
                f"{module.name} — {module.responsibility or 'no responsibility recorded'}"
            ),
            body=_wrap(f"Satisfies: {', '.join(satisfied) or 'see the design spec'}"),
        )

    return TaskOutput(
        facts={
            "scaffold.modules": len(design.modules),
            "scaffold.endpoints": len(design.endpoints),
        },
        artifacts={"scaffold.manifest": "\n".join(sorted(files)) + "\n"},
        files=files,
    )


def _wrap(text: str) -> str:
    """Fold prose to the line length the lint gate enforces."""
    return "\n".join(textwrap.wrap(text, LINE_LENGTH)) or text


def _package_root(task: Task) -> str:
    """Derive the package root from the node's write scope.

    `target/shortener/**` -> `target/shortener`. Taking it from the scope rather
    than a constant keeps this generic: nothing here knows the target is a URL
    shortener (D3).
    """
    for pattern in task.scope.allowed:
        trimmed = pattern.rstrip("*").rstrip("/")
        if trimmed:
            return trimmed
    raise ValueError("scaffold needs a write scope to derive its package root from")
