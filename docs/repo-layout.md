# Repository Layout

Where every class of thing lives, and the rule that governs it.

The organising principle: **authored behaviour is data at the repo root; the
engine that executes it is code under `src/orchestrator/`; everything the
orchestrator produces is under `target/` or `runs/`.**

```
src/orchestrator/     the deliverable — control plane (ours, human-written)
  engine/             scheduler, plan loading, node-kind dispatch, fan-out/join
  gates/              expression evaluation + registered predicates
  policy/             autonomy classes, escalation, write-scope resolution
  workers/            Worker interface (D18): live / replay / stub
  lineage/            causal record of artifacts and decisions
  state/              durable run state (SQLAlchemy)
  metrics/            success rate, retry/rollback frequency, MTTR, latency
  cli/                run, status, approve, metrics, replay

plans/                plan graphs (YAML) — the SDLC, as data
prompts/              agent role prompts, one per role
schemas/              JSON Schemas constraining agent artifacts
config/               target profiles and engine settings
requirements/         prose scenario inputs — the orchestrator's input
fixtures/             recorded worker outputs, for replay runs and engine tests

target/               the target codebase — WRITTEN BY RUNS, not by hand
  shortener/
  tests/

runs/<run_id>/        per-run artifacts, lineage, evidence bundles (gitignored)
tests/                orchestrator's own tests (ours; never agent-written)
docs/                 architecture, this file, the assignment brief (unpublished)
```

## The rules

**`src/orchestrator/` never imports from `target/` (D3).** The orchestrator treats
the target as an arbitrary codebase. An import edge would make the generality
claim false, so it is checkable rather than asserted.

**`target/` is written by orchestrator runs.** Hand-editing it undermines the
whole demonstration: if a reviewer asks "show me the run that produced this
file," the answer has to exist. The target starts empty for a greenfield run.

**`tests/` and `target/tests/` are different populations.** Ours are
human-written and no agent may touch them. The target's are agent-written, and
frozen during repair loops (D6). Keeping them in separate trees means the
distinction is enforced by path, not by care.

**Nothing may be written outside `target/`.** `config/target.*.yaml` sets a
`write_ceiling` that bounds every node's `write_scope`, so a misbehaving agent
cannot modify the orchestrator that governs it.

## Why behaviour lives at the root, not in the package

`plans/`, `prompts/`, `schemas/`, and `config/` are all **authored artifacts that
change system behaviour without an engine change** — the same principle as D16.
Adding an SDLC stage, tuning a role prompt, tightening an artifact schema, or
retargeting to a different codebase are all edits to data.

That also makes them reviewable in isolation: a reader can inspect the entire
SDLC, every prompt, and every artifact contract without reading Python.

## Naming

| Kind | Pattern | Example |
| --- | --- | --- |
| Plan graph | `plans/<scenario>.yaml` | `plans/greenfield.yaml` |
| Role prompt | `prompts/<role>.md` | `prompts/architect.md` |
| Artifact schema | `schemas/<artifact>.json` | `schemas/requirement_register.json` |
| Target profile | `config/target.<name>.yaml` | `config/target.shortener.yaml` |
| Requirement | `requirements/<scenario>.md` | `requirements/brownfield.md` |
| Replay fixture | `fixtures/<node_id>/<input_hash>.json` | `fixtures/intake/a3f1.json` |
