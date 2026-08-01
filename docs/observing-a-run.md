# Watching a run

Where every intermediate result lands, node by node. Nothing here is a summary
the system generates for you — these are the actual files it writes, so a
reviewer can check the claims rather than take them.

## The three places anything appears

| Location | What lives there | How to read it |
| --- | --- | --- |
| `runs/<run_id>/artifacts/<name>/v<N>` | Every artifact version, as the producing node emitted it | `cat` — they are JSON or text |
| `runs/orchestrator.db` | Run state: nodes, attempts, gate verdicts, approvals, lineage | `orchestrator status` / `why` / `metrics` |
| `target/` | What the run actually changed in the codebase | `git status target/` |

The split matters. `target/` is the *product*; `runs/` is the *evidence*. A
reviewer who only reads `target/` sees code with no account of how it got there;
one who only reads `runs/` sees an account with nothing to check it against.

Artifacts are versioned, never overwritten. `intake.register/v1` is what the
analyst produced; `v2` is what triage did to it; `v3` is what a human's answer
did. The chain is the decision lineage, and it is why an approval can go stale.

## Node by node

Each entry: what it produces, where that lands, and the gate that has to hold.

### `intake` — agent, requirements

| | |
| --- | --- |
| Produces | `intake.register` — requirements, acceptance criteria, ambiguities |
| Lands in | `runs/<run>/artifacts/intake.register/v1` |
| Gate | `schema_valid`, `every_requirement_has_testable_ac` |

```bash
jq '.requirements[] | {id, statement}' runs/<run>/artifacts/intake.register/v1
jq '.ambiguities[] | {id, severity, question}' runs/<run>/artifacts/intake.register/v1
```

The ambiguity list is the interesting part: it is what the analyst refused to
decide on its own.

### `ambiguity-triage` — tool, requirements

| | |
| --- | --- |
| Produces | `intake.register` **v2** — same document, dispositions filled in |
| Lands in | `runs/<run>/artifacts/intake.register/v2` |
| Gate | `every_ambiguity_is_disposed_or_escalated` |

```bash
diff <(jq -S . runs/<run>/artifacts/intake.register/v1) \
     <(jq -S . runs/<run>/artifacts/intake.register/v2)
```

Everything below the severity threshold now carries `disposition: assumption`
and a recorded reason. Everything at or above it is left open, deliberately —
that is the node asking for a person, not failing.

### `clarify-with-human` — human, requirements

Runs only if triage escalated. The run **stops** here and the process exits.

```bash
orchestrator status <run>            # what is being asked, and of whom
orchestrator approve <run> clarify-with-human --by you --note "A1: 302
A2: per API key, 100/minute"
```

Answers are matched by ambiguity id, one per line. Nothing is written to
`target/`; the decision goes to the `approvals` table with your name on it.

### `normalize-clarification` — tool, requirements

| | |
| --- | --- |
| Produces | `intake.register` **v3** — your answers folded in per question |
| Lands in | `runs/<run>/artifacts/intake.register/v3` |
| Gate | `clarification.resolved > 0`, `no_ambiguity_without_disposition` |

```bash
jq '.ambiguities[] | select(.severity=="high") | {id, disposition, answer}' \
   runs/<run>/artifacts/intake.register/v3
```

Each answer carries who gave it. Re-emitting the register is what makes this
stateful: anything downstream that consumed v2 is invalidated.

### `design` — agent, design

| | |
| --- | --- |
| Produces | `design.spec` (whole design) and `design.modules` (the list `impl` fans out over) |
| Lands in | `runs/<run>/artifacts/design.spec/v1`, `.../design.modules/v1` |
| Gate | `contract_is_valid`, and traceability in **both** directions |

```bash
jq '{endpoints, modules: [.modules[].name]}' runs/<run>/artifacts/design.spec/v1
jq '[.elements[] | select(.satisfies == [])]' runs/<run>/artifacts/design.spec/v1
```

The second query should return `[]`. A design element satisfying no requirement
is gold-plating, and `no_unmapped_design_elements` fails the node for it.

### `design-approval` — human, design

Stops the run. The approval is **bound to `design.spec@v1`** — if the design is
ever re-derived, this approval reverts to pending (D10).

```bash
orchestrator status <run>     # shows which artifact versions the decision covers
```

### `scaffold` — derive, implementation

| | |
| --- | --- |
| Produces | `scaffold.manifest`, plus real files |
| Lands in | `runs/<run>/artifacts/scaffold.manifest/v1` and **`target/shortener/*/__init__.py`** |
| Gate | `imports.resolve == true`, `ruff.exit_code == 0` |
| Checks | `sh:ruff check target/`, `py:orchestrator.gates.imports_resolve` |

```bash
find target/shortener -name '__init__.py' | xargs head -3
```

The first node that touches the target. No model call — the stubs are derived
from the design, which is why they cannot hallucinate a module.

### `tests-acceptance` — codeagent, verification

| | |
| --- | --- |
| Produces | `tests-acceptance.suite`, `tests-acceptance.changeset`, plus test files |
| Lands in | `runs/<run>/artifacts/tests-acceptance.*/v1` and **`target/tests/`** |
| Gate | `session.files_written > 0`, `pytest.exit_code != 0`, `every_ac_has_a_test` |

```bash
cat runs/<run>/artifacts/tests-acceptance.changeset/v1     # what the guard allowed and refused
jq '.tests | length' runs/<run>/artifacts/tests-acceptance.suite/v1
.venv/bin/pytest target/tests -q                           # must FAIL here
```

**The gate requires the suite to fail.** Tests written against an implementation
that does not exist must be red; a green suite here would mean they assert
nothing (D5).

The changeset is where the write scope becomes visible:

```json
{"written": ["target/tests/test_r1_create.py"], "denied": [],
 "write_scope": ["target/tests/**"], "report": "<the agent's own account>"}
```

`denied` is the number that proves D7 is enforced rather than declared.

### `impl` — fanout, implementation

One child per module from `design.modules`, four at a time.

| | |
| --- | --- |
| Produces | `impl:<module>.changeset` per child, plus module code |
| Lands in | `runs/<run>/artifacts/impl:*/v1` and **`target/shortener/<module>/`** |
| Gate | `session.files_written > 0`, `ruff.exit_code == 0` — per module |

```bash
for f in runs/<run>/artifacts/impl:*/v1; do echo "== $f"; jq '{written, denied}' "$f"; done
```

Each child may write **only its own directory**, and `target/tests/**` is frozen
(D6) — the cheapest route to a green suite is weakening a test, and the runtime
refuses it rather than asking the agent not to.

### `tests` — tool, verification

| | |
| --- | --- |
| Produces | facts only: `pytest.exit_code`, `coverage.percent` |
| Lands in | the gate record — `orchestrator status <run>` |
| Gate | `pytest.exit_code == 0`, coverage threshold, `ac_test_matrix_complete` |

This is where the modules have to add up. On failure a `fix:` node is inserted,
scoped to the failing module, and the node re-enters behind it.

### `docs` — codeagent, documentation

| | |
| --- | --- |
| Produces | `docs.readme` + **`target/README.md`** |
| Gate | `setup_steps_execute_in_clean_venv`, `documented_endpoints_match_openapi` |

The documentation gate compares the README's endpoints against the design's
contract. A doc gate that checks for headings is vacuous.

### `security` — tool, verification

| | |
| --- | --- |
| Produces | `security.report` |
| Lands in | `runs/<run>/artifacts/security.report/v1` |
| Gate | `no_unapproved_high_findings` — and an agent may never waive one (D15) |

```bash
jq '.findings[] | {id, severity, title}' runs/<run>/artifacts/security.report/v1
```

### `release-readiness` — derive, release

| | |
| --- | --- |
| Produces | the evidence bundle |
| Gate | every upstream gate green, lineage complete, no stale approvals, nothing unfinished |

```bash
orchestrator evidence <run> --write     # assembles and writes the bundle
orchestrator metrics <run>              # success rate, retries, MTTR, latency
orchestrator why design.spec            # trace any artifact to what produced it
```

Deterministic by design (D9). A model judging "ready to ship" would put
probabilistic governance at the last step.

### `accept` — human, release

The final checkpoint, presented with the bundle. Approving ends the run.

## While it is running

Two honest limitations:

- **A code-agent session is opaque until it ends.** Facts, the changeset and
  every guard decision are recorded on completion, so a 15-minute session gives
  no interim signal. Watch the filesystem instead: `find target -newermt '-2 minutes'`.
- **Mid-wave state is not visible.** The scheduler holds one transaction per
  wave, so `orchestrator status` from another process shows the state *before*
  the wave. Node rows appear when the wave commits.

## When something goes wrong

| Verb | Means | Use when |
| --- | --- | --- |
| `approve` | accept the state and proceed | a checkpoint, or waiving a judged failure |
| `reject` | stop the run | the work is wrong and should not continue |
| `retry` | re-enter a failed node | you fixed the harness, not the work |
| `invalidate` | withdraw a *passing* result | the gate was wrong and passed something it should not have |
| `rollback` | restore the baseline, then verify | brownfield only — greenfield has nothing to restore |

`retry` refuses a node that passed, on purpose: re-running work until it agrees
with you is how a green run gets manufactured. `invalidate` is the safe
direction — it can only make a run less green.
