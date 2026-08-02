"""Checking a target against the contract its architect declared.

The architect writes both: the stub packages, and the structured contract that
describes them (D24). Two artifacts from one author can disagree — that is the
cost of letting it write Python instead of generating Python from data, and this
is what makes the cost bounded.

**Why the architect writes code at all.** Generating stubs from a JSON contract
means encoding Python in the schema, and the first real contract needed three
constructs it did not have: imports for the types its signatures named, base
classes for an exception hierarchy, and module-level constants that `kind: type`
turned into `= object`. Each is a schema field and a re-run to discover the next
one. An architect writing Python needs none of them; it needs this check instead.

So the derivation moved from *generating* the tree to *auditing* it: every name
the contract promises must exist in the module that promised it. A promise the
code does not keep is caught here, before seven implementers write against it.
"""

from __future__ import annotations

import ast
from pathlib import Path

from orchestrator.artifacts import Design
from orchestrator.workers.pytask import Task, TaskOutput


class ContractError(ValueError):
    """The target does not match the contract, and says exactly where."""


@Task.needs_params("root")
def verify_target_matches_contract(task: Task) -> TaskOutput:
    design = Design.model_validate_json(task.require("design.spec"))
    root = _package_root(task)

    cycles = design.dependency_cycles()
    if cycles:
        raise ContractError(
            "the module dependency graph is not acyclic: "
            + "; ".join(" -> ".join([*cycle, cycle[0]]) for cycle in cycles)
        )

    interfaces = design.interface_for
    missing: list[str] = []
    kept = 0
    manifest: list[str] = []

    for module in design.modules:
        package = task.cwd / root / module.path
        defined = _names_defined_in(package)
        manifest.append(f"{root}/{module.path}  {len(defined)} names")

        for export in interfaces.get(module.name, _empty()).exports:
            if export.name in defined:
                kept += 1
            else:
                missing.append(f"{module.name}.{export.name} ({export.kind})")

    return TaskOutput(
        facts={
            "contract.modules": len(design.modules),
            "contract.exports": sum(len(i.exports) for i in design.interfaces),
            "contract.kept": kept,
            "contract.broken": len(missing),
            "contract.missing": sorted(missing)[:20],
        },
        artifacts={"scaffold.manifest": "\n".join(sorted(manifest)) + "\n"},
    )


def _names_defined_in(package: Path) -> set[str]:
    """Every top-level name a package binds, without importing it.

    Parsed rather than imported: the target is the thing under scrutiny, and a
    module with a side effect at import time would run it inside the
    orchestrator. `imports_resolve` already covers whether it *can* be imported,
    in a subprocess, which is the right place for that question.
    """
    names: set[str] = set()
    for path in sorted(package.rglob("*.py")) if package.exists() else []:
        try:
            tree = ast.parse(path.read_text())
        except SyntaxError:
            continue  # `imports_resolve` and the lint gate both report this properly
        for node in tree.body:
            names |= _bound_by(node)
    return names


def _bound_by(node: ast.stmt) -> set[str]:
    match node:
        case ast.FunctionDef() | ast.AsyncFunctionDef() | ast.ClassDef():
            return {node.name}
        case ast.Assign():
            return {t.id for t in node.targets if isinstance(t, ast.Name)}
        case ast.AnnAssign() if isinstance(node.target, ast.Name):
            return {node.target.id}
        case ast.ImportFrom() | ast.Import():
            # A re-export is a kept promise: a package whose `__init__` imports
            # a name from a submodule does define it for its callers.
            return {alias.asname or alias.name.split(".")[0] for alias in node.names}
        case _:
            return set()


def _empty() -> object:
    """An interface with no exports, for a module the contract never described.

    The gate rejects an uncontracted module; this keeps the audit itself from
    raising before that gate can say so.
    """

    class _NoExports:
        exports: list = []

    return _NoExports()


def _package_root(task: Task) -> str:
    """Where the target's packages live, from the node's params.

    A param rather than the write scope, because this node no longer writes —
    it reads a tree somebody else wrote. Nothing here knows the target is a URL
    shortener (D3); the plan supplies the path from the target profile.
    """
    return str(task.param("root"))
