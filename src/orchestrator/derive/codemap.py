"""Deriving a map of what the target already contains.

Brownfield work begins with a question greenfield never asks: *what is there?*
The characteristic failure is a confident account of modules and functions that
do not exist — analysis that reads as thorough and is worthless.

So the map is parsed, not described. `ast` gives the real symbols, and the
analyst agent reasons over the result rather than over the filesystem. That
ordering is what makes the analysis checkable afterwards: the claims and the
ground truth come from different producers (D4).

Nothing here knows what the target is. The root comes from the plan, which gets
it from the target profile (D3).
"""

from __future__ import annotations

import ast
from pathlib import Path

from orchestrator.artifacts import CodeMap, Symbol
from orchestrator.workers.pytask import Task, TaskOutput


def map_codebase(task: Task) -> TaskOutput:
    root = Path(task.param("root"))
    absolute = task.cwd / root

    files: dict[str, list[Symbol]] = {}
    unparsable: list[str] = []

    for path in sorted(absolute.rglob("*.py")) if absolute.exists() else []:
        if "__pycache__" in path.parts:
            continue
        relative = str(path.relative_to(task.cwd))
        try:
            files[relative] = symbols_in(path.read_text())
        except SyntaxError:
            # Recorded rather than raised: a target that does not parse is a
            # finding about the target, and the analyst should still see the
            # rest of the tree.
            files[relative] = []
            unparsable.append(relative)

    code_map = CodeMap(root=str(root), files=files)
    return TaskOutput(
        facts={
            "codemap.files": len(files),
            "codemap.symbols": sum(len(s) for s in files.values()),
            "codemap.unparsable": len(unparsable),
        },
        artifacts={"codebase.map": code_map.model_dump_json(indent=2)},
    )


def symbols_in(source: str) -> list[Symbol]:
    """Top-level and class-level definitions, in source order.

    Deliberately shallow: a symbol table exists so an analysis can be checked
    against it, and nesting past a method body adds noise without adding a name
    anyone would reference in an impact analysis.
    """
    tree = ast.parse(source)
    found: list[Symbol] = []

    for node in tree.body:
        if isinstance(node, ast.ClassDef):
            found.append(Symbol(name=node.name, kind="class", line=node.lineno))
            found.extend(
                Symbol(name=f"{node.name}.{child.name}", kind="def", line=child.lineno)
                for child in node.body
                if isinstance(child, ast.FunctionDef | ast.AsyncFunctionDef)
            )
        elif isinstance(node, ast.FunctionDef | ast.AsyncFunctionDef):
            found.append(Symbol(name=node.name, kind="def", line=node.lineno))

    return found
