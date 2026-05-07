"""Unit tests for services.transfer_order_service (v1.8.0 #290).

Pure-Python: no DB. Covers the TO number generator (millisecond
zero-padding + monotonic-per-ms property) and the three state
machines (header / line / approval) plus the closure-derivation
helper.
"""

import os
import sys
from datetime import datetime

import pytest

os.environ.setdefault("DATABASE_URL", "postgresql://sentry:sentry@localhost:5432/sentry")
os.environ.setdefault("JWT_SECRET", "NEVER_USE_THIS_IN_PRODUCTION_32!")
os.environ.setdefault("SENTRY_ENCRYPTION_KEY", "t5hPIEVn_O41qfiMqAiPEnwzQh68o3Es46YfSOBvEK8=")

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from services.transfer_order_service import (  # noqa: E402
    TO_APPROVAL_APPROVED,
    TO_APPROVAL_PENDING,
    TO_APPROVAL_REJECTED,
    TO_LINE_APPROVED,
    TO_LINE_PARTIALLY_PICKED,
    TO_LINE_PENDING,
    TO_LINE_PICKED,
    TO_LINE_SHORT_CLOSED,
    TO_STATUS_APPROVED,
    TO_STATUS_AWAITING_APPROVAL,
    TO_STATUS_CANCELLED,
    TO_STATUS_CLOSED,
    TO_STATUS_OPEN,
    TO_STATUS_PARTIALLY_PICKED,
    generate_to_number,
    is_header_closeable,
    validate_approval_transition,
    validate_header_transition,
    validate_line_transition,
)


class TestTONumberGenerator:
    def test_format_matches_spec(self):
        n = generate_to_number(datetime(2026, 5, 7, 14, 38, 29, 905_252))
        # microseconds 905_252 // 1000 == 905
        assert n == "TO-20260507143829905"

    def test_millisecond_zero_padding(self):
        n = generate_to_number(datetime(2026, 1, 1, 0, 0, 0, 1_000))
        # 1000 microseconds == 1 millisecond -> zero-padded "001"
        assert n == "TO-20260101000000001"
        assert len(n) == len("TO-") + 17

    def test_millisecond_floor_truncation(self):
        # 999_999 microseconds -> 999 ms (truncates, not rounds)
        n = generate_to_number(datetime(2026, 1, 1, 0, 0, 0, 999_999))
        assert n == "TO-20260101000000999"

    def test_no_default_now_collision_risk(self):
        # Two calls with the same explicit timestamp produce identical
        # numbers; the route's UNIQUE retry handles this race.
        ts = datetime(2026, 5, 7, 12, 0, 0, 500_000)
        assert generate_to_number(ts) == generate_to_number(ts)

    def test_default_now_returns_well_formed_number(self):
        n = generate_to_number()
        assert n.startswith("TO-")
        assert len(n) == 20
        # All-digit suffix
        assert n[3:].isdigit()


class TestHeaderTransitions:
    def test_open_to_partially_picked(self):
        validate_header_transition(TO_STATUS_OPEN, TO_STATUS_PARTIALLY_PICKED)

    def test_open_to_cancelled(self):
        validate_header_transition(TO_STATUS_OPEN, TO_STATUS_CANCELLED)

    def test_partially_picked_to_awaiting_approval(self):
        validate_header_transition(
            TO_STATUS_PARTIALLY_PICKED, TO_STATUS_AWAITING_APPROVAL,
        )

    def test_awaiting_approval_to_approved(self):
        validate_header_transition(
            TO_STATUS_AWAITING_APPROVAL, TO_STATUS_APPROVED,
        )

    def test_approved_to_closed(self):
        validate_header_transition(TO_STATUS_APPROVED, TO_STATUS_CLOSED)

    def test_closed_is_terminal(self):
        with pytest.raises(ValueError, match="invalid transition"):
            validate_header_transition(TO_STATUS_CLOSED, TO_STATUS_OPEN)
        with pytest.raises(ValueError, match="invalid transition"):
            validate_header_transition(TO_STATUS_CLOSED, TO_STATUS_CANCELLED)

    def test_cancelled_is_terminal(self):
        with pytest.raises(ValueError, match="invalid transition"):
            validate_header_transition(TO_STATUS_CANCELLED, TO_STATUS_OPEN)

    def test_invalid_skip_open_to_approved(self):
        with pytest.raises(ValueError):
            validate_header_transition(TO_STATUS_OPEN, TO_STATUS_APPROVED)

    def test_unknown_state_raises(self):
        with pytest.raises(ValueError, match="unknown current state"):
            validate_header_transition("BOGUS", TO_STATUS_OPEN)


class TestLineTransitions:
    def test_pending_to_partial(self):
        validate_line_transition(TO_LINE_PENDING, TO_LINE_PARTIALLY_PICKED)

    def test_pending_to_short_closed(self):
        validate_line_transition(TO_LINE_PENDING, TO_LINE_SHORT_CLOSED)

    def test_partial_to_picked(self):
        validate_line_transition(TO_LINE_PARTIALLY_PICKED, TO_LINE_PICKED)

    def test_picked_to_approved(self):
        validate_line_transition(TO_LINE_PICKED, TO_LINE_APPROVED)

    def test_picked_to_short_closed(self):
        validate_line_transition(TO_LINE_PICKED, TO_LINE_SHORT_CLOSED)

    def test_approved_is_terminal(self):
        with pytest.raises(ValueError):
            validate_line_transition(TO_LINE_APPROVED, TO_LINE_PENDING)

    def test_short_closed_is_terminal(self):
        with pytest.raises(ValueError):
            validate_line_transition(TO_LINE_SHORT_CLOSED, TO_LINE_PICKED)


class TestApprovalTransitions:
    def test_pending_to_approved(self):
        validate_approval_transition(TO_APPROVAL_PENDING, TO_APPROVAL_APPROVED)

    def test_pending_to_rejected(self):
        validate_approval_transition(TO_APPROVAL_PENDING, TO_APPROVAL_REJECTED)

    def test_approved_is_terminal(self):
        with pytest.raises(ValueError):
            validate_approval_transition(
                TO_APPROVAL_APPROVED, TO_APPROVAL_PENDING,
            )

    def test_rejected_is_terminal(self):
        with pytest.raises(ValueError):
            validate_approval_transition(
                TO_APPROVAL_REJECTED, TO_APPROVAL_APPROVED,
            )


class TestClosureDerivation:
    def test_all_lines_approved_with_matching_qty_closes(self):
        line_states = [
            (TO_LINE_APPROVED, 5, 5),
            (TO_LINE_APPROVED, 10, 10),
        ]
        assert is_header_closeable(line_states) is True

    def test_short_closed_lines_count_as_closed(self):
        line_states = [
            (TO_LINE_APPROVED, 5, 5),
            (TO_LINE_SHORT_CLOSED, 0, 3),
        ]
        assert is_header_closeable(line_states) is True

    def test_pending_line_blocks_closure(self):
        line_states = [
            (TO_LINE_APPROVED, 5, 5),
            (TO_LINE_PENDING, 0, 0),
        ]
        assert is_header_closeable(line_states) is False

    def test_picked_but_unapproved_blocks_closure(self):
        line_states = [
            (TO_LINE_APPROVED, 5, 5),
            (TO_LINE_PICKED, 0, 7),
        ]
        assert is_header_closeable(line_states) is False

    def test_partial_approval_blocks_closure(self):
        # APPROVED state but approved_qty < picked_qty (multi-batch
        # approval still in flight).
        line_states = [
            (TO_LINE_APPROVED, 3, 5),
        ]
        assert is_header_closeable(line_states) is False
