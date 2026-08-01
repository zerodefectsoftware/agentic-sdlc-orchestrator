"""Evidence bundle assembly — the reviewable output of a run (§5.4).

Reads from state, lineage, metrics, and the artifact store; writes a bundle to
`runs/<run_id>/evidence/` as Markdown for a reviewer and JSON for machine checks.

**This package computes nothing.** It collects what was recorded at the moment
each decision was made. A traceability matrix recomputed at assembly time can
differ from the one that actually gated, and the bundle would then document a
decision that was never taken — so gates record their results as artifacts, and
assembly reads those artifacts back.

Separate from `lineage/` because the direction differs: lineage is the write
path during execution and stays close to a leaf, while assembly reads across
four stores. Folding it into lineage would invert that dependency.

Modules: `bundle` (the model), `assemble` (collection), `render` (output).
"""
