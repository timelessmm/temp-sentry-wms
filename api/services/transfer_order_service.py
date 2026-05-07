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


# ============================================================
# Pick-side helpers
# ============================================================


class OverPickAttempt(Exception):
    """picked_qty + delta would exceed committed_qty. The WHERE clause
    guard on update_transfer_order_line_picked is the atomic safety net
    so two concurrent pickers can't both push past the cap; the route
    surfaces 409 to the second picker."""


def update_transfer_order_line_picked(db, to_line_id: int, delta: int) -> dict:
    """Atomically bump transfer_order_lines.picked_qty by ``delta`` and
    flip status accordingly.

    No FOR UPDATE: the WHERE clause encodes the cap (picked_qty + delta
    <= committed_qty) so two concurrent pickers serialize at the row
    lock taken by the UPDATE itself. The second picker's UPDATE
    returns zero rows if the first one already filled the line; the
    helper raises OverPickAttempt and the route surfaces 409.

    Returns {"picked_qty", "committed_qty", "status"} on success.
    """
    if delta <= 0:
        raise ValueError(f"delta must be > 0, got {delta!r}")
    from sqlalchemy import text  # local import to keep top of file lib-free

    row = db.execute(
        text(
            """
            UPDATE transfer_order_lines
               SET picked_qty = picked_qty + :delta,
                   status = CASE
                       WHEN picked_qty + :delta = committed_qty
                            THEN :picked_state
                       ELSE :partial_state
                   END
             WHERE to_line_id = :lid
               AND picked_qty + :delta <= committed_qty
               AND status IN (:pending, :partial_state)
             RETURNING picked_qty, committed_qty, status
            """
        ),
        {
            "delta": delta,
            "lid": to_line_id,
            "picked_state": TO_LINE_PICKED,
            "partial_state": TO_LINE_PARTIALLY_PICKED,
            "pending": TO_LINE_PENDING,
        },
    ).fetchone()
    if row is None:
        raise OverPickAttempt(
            f"transfer_order_lines.to_line_id={to_line_id}: pick of "
            f"+{delta} would exceed committed_qty or line is in a "
            f"non-pickable state"
        )
    return {
        "picked_qty": row.picked_qty,
        "committed_qty": row.committed_qty,
        "status": row.status,
    }


def maybe_promote_header_to_awaiting_approval(db, to_id: int) -> bool:
    """When every TO line has reached PICKED or SHORT_CLOSED, the
    header advances to AWAITING_APPROVAL. Called after confirm_pick
    + after picker submission. Returns True when the header was
    actually advanced (so the caller can write the status-flip
    audit row), False when the TO is still mid-pick.
    """
    from sqlalchemy import text

    counts = db.execute(
        text(
            """
            SELECT
              COUNT(*) FILTER (WHERE status IN (:pending, :partial)) AS open,
              COUNT(*) FILTER (WHERE status = :picked) AS picked_full,
              COUNT(*) AS total
              FROM transfer_order_lines
             WHERE to_id = :tid
            """
        ),
        {
            "tid": to_id,
            "pending": TO_LINE_PENDING,
            "partial": TO_LINE_PARTIALLY_PICKED,
            "picked": TO_LINE_PICKED,
        },
    ).fetchone()
    if counts.total == 0 or counts.open > 0 or counts.picked_full == 0:
        return False
    # Every line is either PICKED, APPROVED, or SHORT_CLOSED. The
    # header should be AWAITING_APPROVAL unless it is already there or
    # already past it (PARTIALLY_PICKED -> AWAITING_APPROVAL is the
    # only forward transition the picker triggers).
    header_row = db.execute(
        text(
            "SELECT status FROM transfer_orders WHERE to_id = :tid FOR UPDATE"
        ),
        {"tid": to_id},
    ).fetchone()
    if header_row is None or header_row.status == TO_STATUS_AWAITING_APPROVAL:
        return False
    if header_row.status not in (TO_STATUS_OPEN, TO_STATUS_PARTIALLY_PICKED):
        return False
    db.execute(
        text(
            "UPDATE transfer_orders "
            "   SET status = :st, updated_at = NOW() "
            " WHERE to_id = :tid"
        ),
        {"st": TO_STATUS_AWAITING_APPROVAL, "tid": to_id},
    )
    return True


def maybe_promote_header_to_partially_picked(db, to_id: int) -> bool:
    """First pick on an OPEN TO advances the header to
    PARTIALLY_PICKED. Returns True when the row actually flipped."""
    from sqlalchemy import text

    row = db.execute(
        text(
            "UPDATE transfer_orders "
            "   SET status = :new_st, updated_at = NOW() "
            " WHERE to_id = :tid AND status = :open "
            " RETURNING status"
        ),
        {
            "tid": to_id,
            "new_st": TO_STATUS_PARTIALLY_PICKED,
            "open": TO_STATUS_OPEN,
        },
    ).fetchone()
    return row is not None


# ============================================================
# Closure derivation against the live row state
# ============================================================


def evaluate_to_closure(db, to_id: int) -> bool:
    """Return True when the TO meets every closure condition: every
    line is APPROVED with approved_qty == picked_qty (or
    SHORT_CLOSED) AND no PENDING approvals remain. Caller flips the
    header to CLOSED and writes the audit row when this returns True."""
    from sqlalchemy import text

    line_states = db.execute(
        text(
            "SELECT status, approved_qty, picked_qty "
            "  FROM transfer_order_lines WHERE to_id = :tid"
        ),
        {"tid": to_id},
    ).fetchall()
    if not line_states:
        return False
    if not is_header_closeable(
        [(r.status, r.approved_qty, r.picked_qty) for r in line_states],
    ):
        return False
    pending = db.execute(
        text(
            "SELECT COUNT(*) FROM transfer_order_approvals "
            " WHERE to_id = :tid AND status = 'PENDING'"
        ),
        {"tid": to_id},
    ).scalar()
    return pending == 0
