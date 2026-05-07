"""Service-level helpers for warehouse-to-warehouse transfer orders
(v1.8.0 #290).

Lives alongside the picking + cycle-count services since the TO
lifecycle reuses both: pick_tasks dispatch via the to_id discriminator
(mig 049), and the admin approval pattern modelled on cycle count
adjustments. Routes orchestrate the lifecycle; this module owns the
TO number generator, the state-machine validation helpers, and the
inventory-locking pattern shared by import + approval.

State machines (plan section 4.1):

  HEADER  OPEN -> PARTIALLY_PICKED -> AWAITING_APPROVAL -> APPROVED ->
                  CLOSED  ;  OPEN / PARTIALLY_PICKED -> CANCELLED.
  LINE    PENDING -> PARTIALLY_PICKED -> PICKED -> APPROVED  ;
          PENDING / PARTIALLY_PICKED -> SHORT_CLOSED.
  APPRVL  PENDING -> APPROVED  ;  PENDING -> REJECTED.

Every state-changing route writes one ACTION_TO_* audit_log row via
api.services.audit_service.write_audit_log so the V-025 chain
extends through the lifecycle.
"""

from __future__ import annotations

from datetime import datetime
from typing import Iterable, Optional


# ============================================================
# TO number generator
# ============================================================
#
# Mirrors picking_service.py:49 BATCH-{YYYYMMDD-HHMMSS} but at
# millisecond precision so a same-second burst from the CSV importer
# does not collide on the UNIQUE (transfer_orders.to_number)
# constraint. The route retries once with a fresh timestamp before
# surfacing 500; the second collision in 1 ms is rare enough that one
# retry is enough.

TO_NUMBER_PREFIX = "TO-"


def generate_to_number(now: Optional[datetime] = None) -> str:
    """Format: TO-{YYYYMMDDHHMMSSmmm}. Millisecond zero-padded."""
    now = now or datetime.now()
    return (
        f"{TO_NUMBER_PREFIX}"
        f"{now.strftime('%Y%m%d%H%M%S')}"
        f"{now.microsecond // 1000:03d}"
    )


# ============================================================
# State machine validation
# ============================================================

# Header
TO_STATUS_OPEN              = "OPEN"
TO_STATUS_PARTIALLY_PICKED  = "PARTIALLY_PICKED"
TO_STATUS_AWAITING_APPROVAL = "AWAITING_APPROVAL"
TO_STATUS_APPROVED          = "APPROVED"
TO_STATUS_CLOSED            = "CLOSED"
TO_STATUS_CANCELLED         = "CANCELLED"

_HEADER_TRANSITIONS = {
    TO_STATUS_OPEN: {TO_STATUS_PARTIALLY_PICKED, TO_STATUS_CANCELLED},
    TO_STATUS_PARTIALLY_PICKED: {
        TO_STATUS_PARTIALLY_PICKED,  # picker keeps picking
        TO_STATUS_AWAITING_APPROVAL,
        TO_STATUS_CANCELLED,
    },
    TO_STATUS_AWAITING_APPROVAL: {TO_STATUS_APPROVED, TO_STATUS_PARTIALLY_PICKED},
    TO_STATUS_APPROVED: {TO_STATUS_CLOSED, TO_STATUS_PARTIALLY_PICKED},
    TO_STATUS_CLOSED: set(),
    TO_STATUS_CANCELLED: set(),
}

# Line
TO_LINE_PENDING           = "PENDING"
TO_LINE_PARTIALLY_PICKED  = "PARTIALLY_PICKED"
TO_LINE_PICKED            = "PICKED"
TO_LINE_APPROVED          = "APPROVED"
TO_LINE_SHORT_CLOSED      = "SHORT_CLOSED"

_LINE_TRANSITIONS = {
    TO_LINE_PENDING: {TO_LINE_PARTIALLY_PICKED, TO_LINE_SHORT_CLOSED},
    TO_LINE_PARTIALLY_PICKED: {
        TO_LINE_PARTIALLY_PICKED,
        TO_LINE_PICKED,
        TO_LINE_SHORT_CLOSED,
    },
    TO_LINE_PICKED: {TO_LINE_APPROVED, TO_LINE_SHORT_CLOSED},
    TO_LINE_APPROVED: set(),
    TO_LINE_SHORT_CLOSED: set(),
}

# Approval
TO_APPROVAL_PENDING  = "PENDING"
TO_APPROVAL_APPROVED = "APPROVED"
TO_APPROVAL_REJECTED = "REJECTED"

_APPROVAL_TRANSITIONS = {
    TO_APPROVAL_PENDING: {TO_APPROVAL_APPROVED, TO_APPROVAL_REJECTED},
    TO_APPROVAL_APPROVED: set(),
    TO_APPROVAL_REJECTED: set(),
}


def _validate_transition(machine: dict, current: str, target: str) -> None:
    if current not in machine:
        raise ValueError(f"unknown current state: {current!r}")
    allowed = machine[current]
    if target not in allowed:
        raise ValueError(
            f"invalid transition {current!r} -> {target!r}; allowed: "
            f"{sorted(allowed)}"
        )


def validate_header_transition(current: str, target: str) -> None:
    _validate_transition(_HEADER_TRANSITIONS, current, target)


def validate_line_transition(current: str, target: str) -> None:
    _validate_transition(_LINE_TRANSITIONS, current, target)


def validate_approval_transition(current: str, target: str) -> None:
    _validate_transition(_APPROVAL_TRANSITIONS, current, target)


# ============================================================
# Closure derivation
# ============================================================


def is_header_closeable(line_states: Iterable[tuple]) -> bool:
    """Header transitions to CLOSED when every line has either
    approved_qty == picked_qty (line state = APPROVED) or status
    SHORT_CLOSED, AND no PENDING approval rows remain. The caller
    passes (state, approved_qty, picked_qty) tuples; the boolean
    answer is the closure decision.
    """
    for state, approved_qty, picked_qty in line_states:
        if state == TO_LINE_SHORT_CLOSED:
            continue
        if state == TO_LINE_APPROVED and approved_qty == picked_qty:
            continue
        return False
    return True
