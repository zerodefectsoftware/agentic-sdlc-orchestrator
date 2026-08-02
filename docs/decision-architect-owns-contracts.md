# The architect owns the contracts

**Status:** accepted and implemented, 2026-08-02. Registered as **D24**, superseding D23.
This document is the reasoning; `docs/architecture.md` §9 holds the decision.

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
| **Architect** | Module boundaries **and every name crossing them** | Nothing — it has no filesystem |
| **`scaffold`** | Nothing | Every stub, generated from the contract |
| **Implementer** | Nothing that crosses a boundary | One module's bodies |

---

## Why the architect does not write the stubs

The alternative — making `design` a code agent with write access to every package — was
considered and rejected.

| | Architect writes files | **Engine generates from contract** |
| --- | --- | --- |
| Stub can disagree with the contract | possible | impossible — one source |
| Can accidentally implement the product | yes, needs a gate to prevent it | **no such option** |
| Cost | a code-agent session | free |

The third row is the argument. A code agent told to write stubs will eventually write one
that works, and the gate to stop it ("every body is unimplemented") is a check you have to
remember to keep. A generator has no body to write. The role boundary stops being policed
and becomes structural.

The architect still decides every name. It just does not type them.

---

## What was built

| Piece | Change |
| --- | --- |
| Schema | `Design` gains `interfaces` — per module: `depends_on`, and `exports` of `name`, `kind`, `signature` (annotated), `raises` |
| `design` node | Emits `interfaces` alongside `spec` and `modules`. Still an `agent`, still no filesystem |
| `design` gate | Adds `every_module_has_an_interface` and `module_dependencies_are_acyclic` |
| `scaffold` | Generates typed stubs from the contract instead of empty packages. Refuses a cycle or an unparseable signature, naming the export that broke it |
| `scaffold` gate | Adds `scaffold.exports > 0` — empty packages are the state that made the fan-out unsafe, and it used to pass |
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

Types are in the contract and in the generated stubs, but **there is no type-checking gate**
— that would need a checker this project does not depend on. The annotations are contract
for a human and an agent to read, not a machine-enforced constraint.

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
