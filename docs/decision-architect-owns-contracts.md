# The architect owns the contracts

**Status:** accepted and implemented, 2026-08-02. Registered as **D24** and **D25**,
superseding D23. This document is the reasoning; `docs/architecture.md` §9 holds the
decisions.

---

## The problem, in one line

The architect described modules in English; the implementers invented the interfaces
between them — seven times, simultaneously, each blind to the others.

That is what killed the live fan-out: `links` imported three exception names from `errors`
while the `errors` author had not run. Neither agent was wrong. **Nobody had the authority
to decide the name.**

D23 answered by deleting the parallelism — one implementer for the whole target. D24
answers by giving the contract an owner, which removes the premise instead of accepting the
cost.

---

## The change

```
   BEFORE                                    AFTER
   ──────                                    ─────
   architect ──▶ prose                       architect ──▶ prose + the contract
                 "storage owns the link                    every export, signature,
                  repository"                              exception, dependency
        │                                          │
        │                                          ▼
        │                                    scaffold (no model call)
        │                                    generates typed stubs
        │                                          │
        ▼                                          ▼
   7 implementers, in parallel,              7 implementers, in parallel,
   each inventing the names it               each filling in bodies behind
   needs from its siblings                   names already importable
        │                                          │
        ▼                                          ▼
   imports that do not resolve                imports that resolved before
                                              anyone wrote a line
```

| Role | Decides | Writes |
| --- | --- | --- |
| **Architect** | Module boundaries **and every name crossing them** | The stub packages, and the contract describing them |
| **`scaffold`** | Nothing | Nothing — it audits the code against the contract |
| **Implementer** | Nothing that crosses a boundary | One module's bodies |

---

## Why the architect writes the stubs

The alternative — the architect emits a JSON contract and the engine generates stubs from
it — was **built first, and rejected on evidence**. It is the better design in principle:
one source of truth, and a generator has no ability to write a working body, so "the
architect must not implement the product" becomes structurally impossible rather than a rule
you enforce.

It failed on contact with the first real contract. Generating Python from data means the
data must encode Python:

| What the stubs needed | What the schema had | Result |
| --- | --- | --- |
| `from datetime import datetime`, `from fastapi import FastAPI` | no imports field | 13 undefined names |
| `NotFoundError(AppError)` — a 7-member hierarchy with constructors | no base class, and exception constructors dropped | every exception became bare `class X(Exception)` |
| `ALPHABET = "0-9A-Za-z"`, `app = create_app()` | only `kind: type` | both became `= object`; the ASGI entry point *was* the literal `object` |

23 lint errors, so the scaffold gate would have blocked the run. Each is a one-field fix —
and that is the argument against it. Three appeared in the first contract, and the fourth
(decorators? dataclass fields? generics?) costs another schema change and another architect
run to discover. An architect writing Python needs none of them.

**What that costs, and how it is bounded.** Both of the generator's advantages are
recoverable by checking rather than by construction:

| Risk | Check | Where |
| --- | --- | --- |
| The architect implements the product | every function body must parse as `raise NotImplementedError` | `design` gate, AST |
| The stubs and the contract disagree | every promised name must exist in the module that promised it | `scaffold` gate |

Both are deterministic, run by a non-producer, and cheaper than the schema chase they
replace. The honest difference: these are rules that must be kept, where the generator had
states that could not occur.

## What was built

| Piece | Change |
| --- | --- |
| Schema | `Design` gains `interfaces` — per module: `depends_on`, and `exports` of `name`, `kind`, `signature` (annotated), `raises` |
| `design` node | Now a `codeagent`: writes the stub packages **and** `target/design.json`. The engine validates that file against the schema and projects `spec`/`modules`/`interfaces` from it, so one file stays one source of truth |
| `design` gate | Adds `every_module_has_an_interface`, `module_dependencies_are_acyclic`, lint, imports resolve, and `stubs.implemented == 0` |
| `scaffold` | No longer generates. Audits the tree against the contract: every promised name must be defined by the module that promised it. Parses rather than imports — the target is the thing under scrutiny |
| `scaffold` gate | `contract.broken == 0`, and `contract.exports > 0` — a contract promising nothing is what the first fan-out had to agree on |
| `impl` | Back to a fan-out, one code agent per module, `max_turns: 60` each |
| Per-module gate | Adds `imports.resolve` — newly meaningful, because stubs mean a module's imports resolve at its own gate rather than two nodes later |
| Prompts | The architect authors the contract; the implementer honours it and **escalates** rather than editing a sibling |

`raises` is the field that carries the original failure. Exceptions cross module boundaries
more often than functions do, and they are the names implementers are likeliest to invent.

---

## The rules that keep it simple

1. **The dependency graph must be a DAG** — checked at the design gate, so the architect is
   told before a human approves a decomposition that cannot be built in parallel. Two
   modules that must change together are one module.
2. **An implementer reads every sibling stub and writes none of them.** A contract quietly
   edited by one of its consumers is not a contract, and the next module to be written would
   not know it changed.
3. **A wrong contract is an escalation, not a local fix.** Say so and stop — do not invent a
   name, duplicate a sibling's function, or weaken your own interface to avoid needing one.

---

## What it does not solve

Behaviour. `links` cannot pass an *integration* test until `storage` really persists,
however clean the contract is. So the per-module gate is scoped to what is checkable in
isolation, and the whole suite runs once at the join:

| Check | Per module, in parallel | At the join |
| --- | --- | --- |
| lint · imports resolve | yes | — |
| the 38-test acceptance suite | no | yes — the existing `tests` node |

Types are in the contract and in the stubs, but **there is no type-checking gate** — that
would need a checker this project does not depend on. The annotations are contract for a
human and an agent to read, not a machine-enforced constraint.

---

## Cost of adopting it mid-run

| Cost | Detail |
| --- | --- |
| One architect session | `design` re-runs, now emitting the contract |
| **One approval** | `design.spec` goes to v3; the approval is version-bound and reverts to pending |
| Acceptance suite re-authored | Happens on resume regardless: `scaffold` re-runs, mints `scaffold.manifest@v2`, and invalidation cascades |

Untouched: `intake.register@v3`, including the four clarification answers, and the rejection
of `design.spec@v1` in the approval trail.

Brownfield is unchanged. Its interfaces already exist in the code being changed, which is
why it kept the fan-out throughout.
