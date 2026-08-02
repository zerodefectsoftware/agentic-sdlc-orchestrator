# Final engineering summary

**Greenfield ran end to end and was accepted on 2026-08-02.** Figures below are from that
run, `610c782beb9a4ea6bc7c8d06444eb432`, and from the 492-test suite as committed.

---

## 1. What was built

An **agentic SDLC orchestrator**: a requirement goes in, a reviewable engineering outcome
comes out, and every step is gated, attributed and replayable. A URL shortener is the
workload it drives — the demo, not the deliverable.

| Layer | Size | Role |
| --- | --- | --- |
| Orchestration layer | ~7,900 lines | gates, policy, lineage, evidence, metrics, workers — the graded differentiator |
| Graph runtime | ~1,700 lines | plan model, loader, wave scheduler |
| Target (shortener) | 8 modules, 1,557 lines | the SDLC's workload — 86 tests, 93.64% coverage |
| Tests | 492 | all deterministic; no test calls a model |

Three plan files describe three scenarios. The engine is fixed; **a scenario is data**
(D16), and brownfield/ambiguous are deltas over greenfield rather than copies (D19).

---

## 2. Plan and rationale

The brief's §4.4 is a list of control-plane properties, so the design starts from them and
every decision traces back to one:

| Requirement | How it is met | Decision |
| --- | --- | --- |
| Explicit dependency graph, entry/exit gates | YAML plan, six node kinds, gate per node | D16 |
| Non-linear, parallel with synchronization | Wave scheduler; **the join is the wave boundary**, not a node | D1 |
| Dynamic re-planning on upstream change | Re-derived artifact ⇒ descendants STALE, bound approvals revert | D10 |
| Cross-stage context and decision lineage | Artifacts versioned per run, `artifact_inputs` edges, `orchestrator why` | D8 |
| Human approval checkpoints | A human is a **node**, not a status; decision text becomes material | D13 |
| Failure controls | Bounded retries → fix node → escalate / safe-stop / rollback | D14 |
| Policy guardrails | Write ceiling, per-node scope, frozen paths, no agent waiver | D6, D7, D15 |
| Audit-grade observability, reliability metrics | Attempts, gate records, evidence bundle, `orchestrator metrics` | D9 |

**The load-bearing idea is that a producer never judges its own work** (D4). A worker returns
*facts and artifacts, never a verdict*; the engine runs the checks the plan names; a gate
reads those facts. An agent's self-report is inadmissible as evidence, by type.

**The second is three verdicts, not two.** PASS / FAIL / **ERROR** — "the check could not be
performed" is neither of the others. Folding ERROR into PASS makes governance decorative;
folding it into FAIL makes the repair loop retry a harness fault forever. This distinction
earned itself repeatedly in live running (§5).

---

## 3. Artifacts produced

| Artifact | What it is |
| --- | --- |
| `intake.register` v1→v3 | 5 requirements, 20 acceptance criteria, 13 ambiguities — 9 self-disposed as assumptions, 4 escalated to a human, all 13 disposed by v3 |
| `design.spec` v1→v4 | v1 **rejected** at the checkpoint; v4 is 8 modules, 33 elements, 6 endpoints, 48 exports, acyclic |
| Stub packages | 24 functions, every body `raise NotImplementedError`, verified by AST |
| `tests-acceptance.suite` | 86 tests over 20 criteria, authored **before** implementation, RED gate held |
| `*.changeset` | per node: files written, writes denied, scope declared |
| Evidence bundle | assembled and **RELEASABLE** — every gate held, every approval current |

Every artifact is on disk under `runs/<id>/artifacts/<name>/vN`, readable with `cat`, with
its producing attempt recorded in SQLite.

---

## 4. Risks, trade-offs and validation

### Accepted trade-offs

| Choice | Cost accepted |
| --- | --- |
| Hand-rolled graph runtime (D1) | We own the scheduler **and its bugs**. Two of the seven defects found in live running were durable-resume bugs — precisely what LangGraph's checkpointer or Temporal would have supplied. Documented rather than glossed |
| Architect writes stubs, not a generator (D25) | Two artifacts from one author can disagree, so an audit checks every promised name; a code agent could implement instead of declare, so every body is parsed. Rules kept, not states made impossible |
| Declarative plan DSL (D16) | Expressiveness ceiling; dynamic fan-out needed an explicit construct |
| Bash disallowed for code agents | A scope guard cannot parse a shell command, so agents cannot run their own tests. Verification is a separate node — which D4 wanted anyway |
| Scope guard detects, does not prevent | Prevention belongs to the runtime; this catches what the runtime cannot |

### Validation

- **465 deterministic tests.** The `Worker` seam (D18) makes a non-deterministic system
  testable: gate evaluation, invalidation cascades, retry budgets and stale-approval
  detection are all covered without a model call.
- **Architecture invariants are tested**, not asserted: `orchestrator` never imports the
  target; generated schemas match their models; the appendix cannot drift from the plan.
- **Live running is the real validation**, and it is where everything below came from.

---

## 5. What live running actually found

**Roughly thirty defects, none visible to a 496-test suite, all from executing the
system rather than testing it.** This is the most useful evidence in the submission and
is not presented as embarrassment — it is what validation looks like when the thing
being validated is a control plane.

They cluster, and the clustering is the finding.

### Re-entry: resume, invalidate, re-check — 8 defects

| Defect | Consequence if unfound |
| --- | --- |
| A fan-out reported PASS having done nothing on resume | **A node that did no work recording success**, with every gate downstream reasoning from it |
| STALE nodes never re-entered the graph | Re-planning was a one-way door onto a wedged run |
| Fan-out children identified by history, not by their source artifact | An obsolete decomposition satisfied a current one |
| A re-materialised child kept the SKIPPED status of the run that abandoned it | Six of eight modules silently never ran |
| A re-entered child ran the definition persisted when it was created | Two implementers judged by a gate two plan revisions old |
| Reclaimed children ran in the same wave as the parent redefining them | The redefinition never applied |
| A node left RUNNING by a killed process was uncollectable | A killed run could never advance again |
| A retry could not see its own last gate after a wave committed | Retry feedback silently returned nothing |

### Governance: the mechanisms that make a decision mean something — 9 defects

| Defect | Consequence if unfound |
| --- | --- |
| `invalidate` did not cascade to consumers | A run completing around a change nobody applied |
| The cascade missed blocked, failed and errored nodes | Wedged runs; checkpoints waiting on superseded questions |
| `invalidate` never retired the escalations it made moot | Dead checkpoints blocked four consecutive attempts |
| **`revert_to_pending` was dead code** | D10's "a re-derived artifact reverts its approval" was documentation, not behaviour |
| `stale_approvals` counted every approval ever recorded | Approving v2 made a run permanently stale — even after approving v4 |
| A run waiting on a person recorded as FAILED | Tooling keys on BLOCKED; nothing offered to ask |
| An interactive decision recorded without passing the checkpoint | A question answerable by neither route |
| No way to stop a run | Ending one meant finding the process and killing it |

### Gates that could not hold — 7 defects

| Defect | Consequence if unfound |
| --- | --- |
| `documented_endpoints_match_openapi` compared `/api/links` with `POST /api/links` | **No README could ever have passed.** Five attempts on correct documentation |
| `setup_steps_execute_in_clean_venv` demanded a fact nothing produced | Same: unsatisfiable, and reported as FAIL rather than ERROR |
| `no_node_in_nonterminal_state` counted the node asking and the checkpoint after it | The final gate waited for itself. Being last, nothing had reached it to find out |
| `ambiguities.total > 0` named a fact only the agent could produce (D4 forbids it) | ERROR on the ambiguous plan's first run |
| `session.files_written > 0` measured activity, not outcome | Failed an implementer for correctly writing nothing to a finished module |
| Preflight ignored required params, and never looked inside fan-out templates | Every child ERRORed after its work was done and paid for |
| The `design` gate linted the whole target | The architect failed on a test suite it is frozen out of by D6 |

### Policy and plans — 4 defects

| Defect | Consequence if unfound |
| --- | --- |
| **The escalation threshold was inert** | `ambiguous.yaml` lowers it to MEDIUM to make a person decide more; the analyst had already disposed of every medium before the policy ran. Ten questions decided by the agent that raised them |
| brownfield's `impl` template dropped `freeze_paths` | **D6 silently void**: implementers could edit the suite judging them, and weakening a test makes it greener |
| brownfield's `design` inherited a write scope and stub checks from a node it no longer resembled | A blast radius advertised that the node could not use |
| ambiguous's `design` override went stale exactly as D19 predicts a copy will | Would have failed the moment it ran |

### The pattern

**The unit tests covered the graph running forwards.** Every defect above is in re-entry,
governance, or a check that had never been reached — the parts of an orchestrator that
only execute when something has already gone wrong, which is exactly when they matter.

Two conclusions follow, and both are now evidence-backed rather than asserted:

- **The control plane was worth building; the durable-execution layer underneath it was
  not.** Eight of these vanish under Temporal or an equivalent, and several more never
  happen with per-wave commits — which took twenty minutes once observability was finally
  treated as a feature rather than a nicety.
- **A gate nobody has reached is a gate nobody has tested.** Four checks in this system
  were structurally unsatisfiable and passed review, because reviewing a predicate is not
  the same as running one.

### What the guardrails did when it mattered

Not everything found was a defect. Three of the design's central claims were observed
holding, under conditions nobody arranged:

- **The write ceiling refused an agent reaching for the rules.** The documentation agent,
  failing a gate, tried to edit `src/orchestrator/gates/predicates.py` — the file
  implementing the check that was failing it — and was refused. It tried `/tmp` on the
  previous attempt. D7 said "a misbehaving agent cannot reach the orchestrator that
  governs it"; that is now a recorded denial rather than a claim.
- **Blast radius held between peers.** Four implementers independently tried to edit
  `main`, and all four were refused. Each then reported it, which is the escalation path
  working.
- **The stub-only gate held.** The architect had every opportunity to implement rather
  than declare: 24 functions, 0 implemented, verified by parsing rather than by asking.

## 6. Assumptions

Recorded rather than resolved silently — the register carries all 13 with dispositions:

| # | Assumption | Consequence accepted |
| --- | --- | --- |
| A1 | **301 permanent redirects** (human decision) | Cached redirects never reach the service, so click counts under-report. Chosen knowingly, recorded in the design |
| A2 | **No authentication or multi-tenancy** | `DELETE` is open; anyone can read anyone's analytics. The single largest open question |
| A5 | No deduplication by long URL | Two campaigns never share a click count |
| A7 | Soft delete; codes never reissued | Analytics survive deletion |
| A8 | No IP storage, no geolocation | Analytics are referrer + user-agent only |
| A12 | No malware/phishing screening | Creation rejects only self-referential hosts |

---

## 7. Limitations

**Observability.** Fixed during this work, and the fix is instructive: the whole run used to
be one transaction, so nothing was visible until the process exited. A wave is now the commit
boundary, nodes report RUNNING before dispatch, and `orchestrator watch` follows a live run
from another terminal. Most of the day's debugging cost was paid to *not* having this.

What remains: a code-agent session still records nothing until it ends, so a wave in flight
shows which node is running but not what it is doing.

**Coverage of the graph.** Greenfield is proven end to end: 20 nodes, 63 attempts, 11
incidents all recovered, 0 unrecovered. The ambiguous scenario is proven through
requirements — 15 ambiguities surfaced from one sentence, 5 escalated, a human checkpoint
taken. **Brownfield has never been executed**: its plan is written and was audited against
the current engine (two real defects found and fixed — see §5), but no run exists, and a
plan that has not run is a design, not a result.

**Scale.** SQLite, single node, whole-tree rollback. Fine for a prototype, wrong for a fleet.

**Fixtures drift.** The replay worker records real sessions; recordings age as models change.

---

## 8. If this continued

1. **Per-wave commits and turn-level streaming** — make a run watchable. Every problem above
   was harder to diagnose because a run is a black box while it executes.
2. **Durable execution underneath** (Temporal or equivalent), keeping the governance layer.
   Two of seven defects disappear by construction.
3. **Type-checking gate.** Types are in the contract and the stubs; nothing enforces them.
4. **Content-addressed artifacts**, enabling per-node rollback instead of whole-tree.
