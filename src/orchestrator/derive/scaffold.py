"""Scaffolding a target from its design.

Turns the architect's interface contract into importable, typed stubs: one
package per module, every promised name present, every body unimplemented.

**Why this is a derivation and not an agent (D8, D24).** The names modules call
each other by have to be decided once, by someone with authority over all of
them, *before* anyone writes a body — a live fan-out died on three exception
names invented by a module that did not own them. The architect decides them;
this generates them. Generating rather than writing them is what makes the stub
and the contract impossible to disagree: there is no second author to disagree
with, and a generator cannot accidentally implement the product.

A module with no interface still gets a package, so the graph shape never
depends on how complete the contract is.
"""

from __future__ import annotations

import keyword
import re
import textwrap

from orchestrator.artifacts import Design, Export, Interface
from orchestrator.workers.pytask import Task, TaskOutput

HEADER = (
    '"""{title}\n\n{body}\n\n'
    "Generated from the design contract. Implementation belongs to the module's own node.\n"
    '"""\n'
)

# Derived code is judged by the same lint gate as written code, so it has to
# satisfy it. A module responsibility is prose an architect wrote to a length
# nobody constrained — pasted onto one line it blew the line-length rule, and
# the scaffold gate then failed deterministically, forever, on the generator's
# own output rather than on anything an agent did.
LINE_LENGTH = 96

# A signature is authored text that becomes code. Anything that is not a
# parameter list is refused rather than pasted: a stub that does not parse fails
# the whole scaffold, and the useful failure names the export, not the file.
SIGNATURE = re.compile(r"^\(.*\)(\s*->\s*.+)?$", re.S)

BODY = "    raise NotImplementedError"


class ContractError(ValueError):
    """The contract cannot be turned into code, and says which name broke it."""


def scaffold_from_design(task: Task) -> TaskOutput:
    design = Design.model_validate_json(task.require("design.spec"))
    root = _package_root(task)
    interfaces = design.interface_for

    cycles = design.dependency_cycles()
    if cycles:
        raise ContractError(
            "the module dependency graph is not acyclic: "
            + "; ".join(" -> ".join([*cycle, cycle[0]]) for cycle in cycles)
        )

    files: dict[str, str] = {
        f"{root}/__init__.py": HEADER.format(
            title="The target package.",
            body=_wrap(f"Modules: {', '.join(m.name for m in design.modules) or '(none)'}"),
        )
    }

    exported = 0
    for module in design.modules:
        interface = interfaces.get(module.name)
        files[f"{root}/{module.path}/__init__.py"] = _module_source(design, module, interface)
        exported += len(interface.exports) if interface else 0

    return TaskOutput(
        facts={
            "scaffold.modules": len(design.modules),
            "scaffold.endpoints": len(design.endpoints),
            "scaffold.interfaces": len(design.interfaces),
            "scaffold.exports": exported,
        },
        artifacts={"scaffold.manifest": "\n".join(sorted(files)) + "\n"},
        files=files,
    )


def _module_source(design: Design, module, interface: Interface | None) -> str:
    satisfied = sorted(
        {
            requirement
            for element in design.elements
            if element.kind == "module" and element.summary == module.name
            for requirement in element.satisfies
        }
    )
    header = HEADER.format(
        title=_wrap(f"{module.name} — {module.responsibility or 'no responsibility recorded'}"),
        body=_wrap(f"Satisfies: {', '.join(satisfied) or 'see the design spec'}"),
    )
    if interface is None or not interface.exports:
        return header

    parts = [header]
    if interface.depends_on:
        parts.append(_wrap(f"# Depends on: {', '.join(sorted(interface.depends_on))}") + "\n")

    for export in interface.exports:
        parts.append(_stub(module.name, export))

    return "\n".join(parts)


def _stub(module: str, export: Export) -> str:
    """One promised name, as code that imports and raises."""
    name = _identifier(module, export.name)
    doc = _wrap(export.summary or f"{export.kind} exported by {module}.")
    raises = (
        _wrap(f"Raises: {', '.join(export.raises)}") if export.raises else ""
    )
    docstring = '    """' + "\n    ".join(filter(None, [doc, "", raises])).rstrip() + '\n    """'

    match export.kind:
        case "exception":
            return f'class {name}(Exception):\n    """{doc}"""\n'
        case "class":
            return f'class {name}:\n{docstring}\n\n{BODY}\n'
        case "type":
            return f'{name} = object  # {export.summary or "contract type"}\n'
        case _:
            return f"def {name}{_signature(module, export)}:\n{docstring}\n{BODY}\n"


def _signature(module: str, export: Export) -> str:
    signature = (export.signature or "()").strip()
    if not SIGNATURE.match(signature):
        raise ContractError(
            f"{module}.{export.name} declares signature {signature!r}, which is not a "
            f"parameter list — expected something like '(code: str) -> Link'"
        )
    return signature


def _identifier(module: str, name: str) -> str:
    if not name.isidentifier() or keyword.iskeyword(name):
        raise ContractError(f"{module} exports {name!r}, which is not a Python name")
    return name


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
