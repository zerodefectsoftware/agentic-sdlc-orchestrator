"""Autonomy classes, escalation, and what to do about a gate verdict.

The gate reaches a verdict; policy decides the consequence. That separation is
what lets a HIGH security finding force an approval on a node whose default is
REVIEW — and it keeps the evaluator ignorant of retry budgets.
"""

from orchestrator.policy.failure import Action, Response, respond_to

__all__ = ["Action", "Response", "respond_to"]
