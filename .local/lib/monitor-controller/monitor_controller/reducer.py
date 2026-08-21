"""Fail-closed pure reducer boundary.

Transition policy is intentionally deferred to the next implementation bead.  Until
then, every well-typed event is a safe no-op and can never dispatch a side effect.
"""

from __future__ import annotations

from .invariants import assert_controller_invariants
from .model import EVENT_TYPES, Decision, Event, State


class UnknownEventError(TypeError):
    """Raised when a caller bypasses the closed Event type."""


def reduce(state: State, event: Event) -> Decision:
    """Validate state and return a no-effect decision for a closed-union event."""
    assert_controller_invariants(state)
    if not isinstance(event, EVENT_TYPES):
        msg = f"event is outside the closed Event union: {type(event).__name__}"
        raise UnknownEventError(msg)
    decision = Decision(state=state)
    assert_controller_invariants(decision.state)
    return decision
