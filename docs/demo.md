# Demonstrating this system

What to show, in what order, and what will bite you. Read this before running anything
live.

---

## The constraint that shapes everything

**A full greenfield run takes 60–90 minutes.** Eight parallel code agents, an architect
session, a documentation session that builds a venv and installs FastAPI. It cannot be
demonstrated live end to end.

So the demo is two halves: **a completed run you read**, and **a short fresh run you
watch**.

---

## Part 1 — the completed run (instant, real, safe)

Greenfield `610c782beb9a4ea6bc7c8d06444eb432` ran to acceptance. Everything below reads
recorded state; nothing executes.

```bash
orchestrator status  610c782beb9a4ea6bc7c8d06444eb432     # 20 nodes, all green
orchestrator metrics 610c782beb9a4ea6bc7c8d06444eb432     # 11 incidents, 11 recovered
orchestrator evidence 610c782beb9a4ea6bc7c8d06444eb432    # RELEASABLE, with the argument
orchestrator why design.spec 610c782beb9a4ea6bc7c8d06444eb432
```

**What to point at, in order:**

| Show | Say |
| --- | --- |
| `status` | Twenty nodes, six stages, parallel verification branches joined at release readiness. The join is the wave boundary, not a node |
| The **human decisions** table in `evidence` | `design.spec@v1` rejected, v2 approved, v2 and v3 withdrawn, v4 approved. Approval binds to a *version* — that is D10, and it blocked release until the signature matched what was built |
| `why design.spec` | Every artifact traces to the attempt that produced it |
| `runs/<id>/artifacts/intake.register/v3` | 5 requirements, 20 acceptance criteria, 13 ambiguities — 4 escalated to a human, 9 disposed as *recorded assumptions* |
| `target/README.md` | Written by an agent, and its setup steps were **executed** by the gate that accepted it |

Then run the product:

```bash
.venv/bin/pytest target/tests            # 86 passed
cd target && ../.venv/bin/uvicorn shortener.main:app --reload
curl -i http://127.0.0.1:8000/health
```

---

## Part 2 — a fresh run you can watch (3–5 minutes)

**Use the ambiguous scenario.** It stops at a human checkpoint by design, so it is short,
and the stopping *is* the demonstration.

Two terminals. Second one first:

```bash
# terminal 2 — the live view
orchestrator watch <run-id> -v
```

```bash
# terminal 1 — start it
orchestrator run --plan plans/ambiguous.yaml \
                 --requirement requirements/ambiguous.md \
                 --target config/target.ratelimit.yaml
```

The requirement is one sentence: *"Add rate limiting to protect the service."*

**What you will see, and what to say about it:**

| In the stream | Say |
| --- | --- |
| `running intake` then `passed` | Status is committed per wave, so a second terminal sees a run as it happens |
| `artifact intake.register@v1` | ~15 ambiguities surfaced from one sentence |
| `gate pass ambiguity-triage` | Policy split them: below the threshold gets a recorded assumption, at or above it goes to a person. The threshold is **one line of data** in the plan, not code |
| `awaiting clarify-with-human` + the questions | The run stops. It will not guess |
| `· blocked — awaiting your decision` | A run waiting on a person is not a failed run |

Answer them live:

```bash
orchestrator watch <run-id> --decide --by "Komali Avadhani"
```

It asks each question in turn. Then `normalize-clarification` folds the answers back into
the register as attributable dispositions — **that is the step that makes clarification
more than theatre**, and it is worth saying so: without it the run stops, a person
answers, and every downstream node keeps reading the same unresolved register.

**Then stop it deliberately:**

```bash
orchestrator stop <run-id> --by "Komali Avadhani"
```

Say why: the graded part of this scenario is *ambiguity identified and normalized*, and
that is now done. Continuing would build a rate limiter, which is a different demo.

---

## Part 3 — the failure controls (optional, 1 minute, no cost)

These are the most interesting part and they cost nothing to show, because they are all
recorded state.

```bash
orchestrator evidence 610c782beb9a4ea6bc7c8d06444eb432 | grep -A5 "denied"
```

- **The write ceiling refused an agent reaching for the rules.** The documentation agent,
  failing a gate, tried to edit `src/orchestrator/gates/predicates.py` — the file
  implementing the check that was failing it. Refused. It tried `/tmp` on the previous
  attempt.
- **Four implementers tried to edit `main` and were refused.** Each then said so, which is
  the escalation path working rather than four silent cross-edits.
- **The stub-only gate held**: the architect wrote 24 functions and implemented none,
  verified by parsing the AST rather than asking it.

---

## What will bite you

| Hazard | Avoid it by |
| --- | --- |
| **A run overwrites `target/shortener`** | Ambiguous now has its own profile (`config/target.ratelimit.yaml`). Never run it against the shortener profile. `.backup/` holds a restore |
| **A blocked checkpoint does not stop its wave-mates** | Do not resume a run expecting a checkpoint to hold back work already dispatched beside it |
| **Waiving an escalation makes the node SKIPPED, which satisfies dependents** | Do not approve `escalate:normalize-clarification` — it lets `design` proceed on unanswered questions |
| **A killed session's writes stay written** | `orchestrator stop --force` ends a run and reopens its nodes; it cannot undo files. Greenfield has no baseline (that is brownfield's) |
| Demoing a full greenfield run | 60–90 minutes. Use the completed one |

---

## Questions you should expect

**"Why not LangGraph or Temporal?"** — D1, with the alternatives table. And be honest: two
of the ~30 defects found in live running were durable-resume bugs that a checkpointer
would have supplied. The control plane was worth building; the durable-execution layer
underneath it was not.

**"No authentication?"** — A2 in the register, surfaced as a HIGH ambiguity and recorded
as an assumption rather than silently resolved. `DELETE` is open. It is the largest open
question and it is written down.

**"How do you know the tests are real?"** — They were authored before implementation, by a
different agent, and the gate required them to **fail** against the empty scaffold (D5).
An acceptance suite that passes against nothing is testing nothing.

**"What happened when it went wrong?"** — §5 of the engineering summary: ~30 defects, every
one in re-entry, governance, or a gate nothing had reached. The unit tests covered the
graph running forwards.
