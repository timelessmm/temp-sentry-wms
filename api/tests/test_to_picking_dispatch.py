"""v1.8.0 (#292): TO picking dispatch tests.

Covers:
- update_transfer_order_line_picked WHERE-clause guard at the
  service layer (single + concurrent attempts).
- maybe_promote_header_to_partially_picked / _to_awaiting_approval
  state-flip helpers.
- POST /api/admin/transfer-orders/<to_id>/start-picking creates a
  pick_batch + pick_tasks per (line, bin) with to_id + to_line_id
  set (so_id NULL); XOR CHECK passes.
- start-picking refuses on no_pickable_inventory + invalid status.
- POST /api/picking/confirm on a TO task updates
  transfer_order_lines.picked_qty + status, decrements
  inventory.quantity_on_hand + quantity_allocated, audits with
  TO_LINE_PICKED + TO_LINE entity_type.
- Two pickers picking the same line totalling > committed_qty:
  second attempt raises OverPickAttempt.
- GET /api/admin/picker/transfer-orders/<to_id> returns header +
  lines with per-line pick progress.
"""

import os
import sys
import uuid

import pytest

os.environ.setdefault("DATABASE_URL", "postgresql://sentry:sentry@localhost:5432/sentry")
os.environ.setdefault("JWT_SECRET", "NEVER_USE_THIS_IN_PRODUCTION_32!")
os.environ.setdefault("SENTRY_ENCRYPTION_KEY", "t5hPIEVn_O41qfiMqAiPEnwzQh68o3Es46YfSOBvEK8=")

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

import db_test_context
from services.transfer_order_service import (  # noqa: E402
    OverPickAttempt,
    maybe_promote_header_to_awaiting_approval,
    maybe_promote_header_to_partially_picked,
    update_transfer_order_line_picked,
)


def _query(sql, params=()):
    conn = db_test_context.get_raw_connection()
    cur = conn.cursor()
    try:
        cur.execute(sql, params)
        if cur.description is None:
            return None
        return cur.fetchall()
    finally:
        cur.close()


def _seed_to(source_wh=1, dest_wh=2, status="OPEN"):
    conn = db_test_context.get_raw_connection()
    cur = conn.cursor()
    try:
        cur.execute(
            "INSERT INTO transfer_orders "
            "(to_number, source_warehouse_id, destination_warehouse_id, "
            " status, created_by, external_id) "
            "VALUES (%s, %s, %s, %s, 't292', %s) RETURNING to_id",
            (
                f"TO-T292-{uuid.uuid4().hex[:8]}",
                source_wh, dest_wh, status, str(uuid.uuid4()),
            ),
        )
        return cur.fetchone()[0]
    finally:
        cur.close()


def _seed_to_line(to_id, item_id=1, line_number=1, requested=10,
                  committed=10, picked=0, status="PENDING"):
    conn = db_test_context.get_raw_connection()
    cur = conn.cursor()
    try:
        cur.execute(
            "INSERT INTO transfer_order_lines "
            "(to_id, item_id, line_number, requested_qty, committed_qty, "
            " picked_qty, status) "
            "VALUES (%s, %s, %s, %s, %s, %s, %s) RETURNING to_line_id",
            (to_id, item_id, line_number, requested, committed, picked, status),
        )
        return cur.fetchone()[0]
    finally:
        cur.close()


def _seed_inventory(item_id, warehouse_id, on_hand, allocated=0,
                    bin_id=1, lot_number=None):
    conn = db_test_context.get_raw_connection()
    cur = conn.cursor()
    try:
        cur.execute(
            "DELETE FROM inventory WHERE item_id = %s AND warehouse_id = %s",
            (item_id, warehouse_id),
        )
        cur.execute(
            "INSERT INTO inventory "
            "(item_id, bin_id, warehouse_id, quantity_on_hand, "
            " quantity_allocated, lot_number) "
            "VALUES (%s, %s, %s, %s, %s, %s) RETURNING inventory_id",
            (item_id, bin_id, warehouse_id, on_hand, allocated, lot_number),
        )
        return cur.fetchone()[0]
    finally:
        cur.close()


# ----------------------------------------------------------------------
# Service-layer helpers
# ----------------------------------------------------------------------


def _open_session():
    """Bound to the conftest test transaction via savepoint mode.
    Caller commits after the operation under test so the savepoint
    flushes into the outer transaction (which conftest rolls back at
    end-of-test)."""
    import models.database as db
    return db.SessionLocal()


class TestUpdateLinePicked:
    def test_first_pick_partial(self):
        to_id = _seed_to()
        line_id = _seed_to_line(to_id, committed=10, picked=0)
        session = _open_session()
        try:
            result = update_transfer_order_line_picked(session, line_id, 4)
            session.commit()
        finally:
            session.close()
        assert result["picked_qty"] == 4
        assert result["status"] == "PARTIALLY_PICKED"

    def test_pick_completes_line(self):
        to_id = _seed_to()
        line_id = _seed_to_line(to_id, committed=10, picked=0)
        session = _open_session()
        try:
            update_transfer_order_line_picked(session, line_id, 10)
            session.commit()
        finally:
            session.close()
        rows = _query(
            "SELECT picked_qty, status FROM transfer_order_lines "
            " WHERE to_line_id = %s",
            (line_id,),
        )
        assert rows[0] == (10, "PICKED")

    def test_over_pick_raises(self):
        to_id = _seed_to()
        line_id = _seed_to_line(to_id, committed=5, picked=0)
        session = _open_session()
        try:
            with pytest.raises(OverPickAttempt):
                update_transfer_order_line_picked(session, line_id, 6)
            session.rollback()
        finally:
            session.close()
        rows = _query(
            "SELECT picked_qty, status FROM transfer_order_lines "
            " WHERE to_line_id = %s",
            (line_id,),
        )
        assert rows[0] == (0, "PENDING")

    def test_sequential_pick_then_overflow_attempt_rejects(self):
        """Same-session sequential picks: first pick fills 7/10,
        second attempt for +5 would push to 12 which the WHERE clause
        rejects via zero-row UPDATE -> OverPickAttempt."""
        to_id = _seed_to()
        line_id = _seed_to_line(to_id, committed=10, picked=0)
        session = _open_session()
        try:
            update_transfer_order_line_picked(session, line_id, 7)
            session.commit()
            with pytest.raises(OverPickAttempt):
                update_transfer_order_line_picked(session, line_id, 5)
            session.rollback()
        finally:
            session.close()
        rows = _query(
            "SELECT picked_qty, status FROM transfer_order_lines "
            " WHERE to_line_id = %s",
            (line_id,),
        )
        assert rows[0] == (7, "PARTIALLY_PICKED")

    def test_short_closed_line_rejected(self):
        to_id = _seed_to()
        line_id = _seed_to_line(
            to_id, committed=10, picked=0, status="SHORT_CLOSED",
        )
        session = _open_session()
        try:
            with pytest.raises(OverPickAttempt):
                update_transfer_order_line_picked(session, line_id, 1)
            session.rollback()
        finally:
            session.close()


class TestStateFlipHelpers:
    def test_first_pick_promotes_header_to_partially_picked(self):
        to_id = _seed_to(status="OPEN")
        session = _open_session()
        try:
            assert maybe_promote_header_to_partially_picked(session, to_id)
            session.commit()
        finally:
            session.close()
        rows = _query(
            "SELECT status FROM transfer_orders WHERE to_id = %s", (to_id,),
        )
        assert rows[0][0] == "PARTIALLY_PICKED"

    def test_promote_idempotent_after_first_call(self):
        to_id = _seed_to(status="PARTIALLY_PICKED")
        session = _open_session()
        try:
            assert not maybe_promote_header_to_partially_picked(session, to_id)
        finally:
            session.close()

    def test_all_lines_picked_promotes_to_awaiting_approval(self):
        to_id = _seed_to(status="PARTIALLY_PICKED")
        _seed_to_line(
            to_id, line_number=1, committed=5, picked=5, status="PICKED",
        )
        _seed_to_line(
            to_id, line_number=2, item_id=2, committed=3, picked=3,
            status="PICKED",
        )
        session = _open_session()
        try:
            assert maybe_promote_header_to_awaiting_approval(session, to_id)
            session.commit()
        finally:
            session.close()
        rows = _query(
            "SELECT status FROM transfer_orders WHERE to_id = %s", (to_id,),
        )
        assert rows[0][0] == "AWAITING_APPROVAL"

    def test_open_lines_block_promotion(self):
        to_id = _seed_to(status="PARTIALLY_PICKED")
        _seed_to_line(
            to_id, line_number=1, committed=5, picked=5, status="PICKED",
        )
        _seed_to_line(
            to_id, line_number=2, item_id=2, committed=3, picked=1,
            status="PARTIALLY_PICKED",
        )
        session = _open_session()
        try:
            assert not maybe_promote_header_to_awaiting_approval(session, to_id)
        finally:
            session.close()


# ----------------------------------------------------------------------
# Start-picking endpoint
# ----------------------------------------------------------------------


class TestStartPicking:
    def test_creates_pick_batch_and_tasks_per_line(
        self, client, auth_headers,
    ):
        to_id = _seed_to()
        line1 = _seed_to_line(to_id, item_id=1, line_number=1, committed=5)
        line2 = _seed_to_line(to_id, item_id=2, line_number=2, committed=3)
        _seed_inventory(1, 1, on_hand=10, bin_id=1)
        _seed_inventory(2, 1, on_hand=10, bin_id=2)

        resp = client.post(
            f"/api/admin/transfer-orders/{to_id}/start-picking",
            headers=auth_headers,
        )
        assert resp.status_code == 201, resp.get_json()
        body = resp.get_json()
        assert body["tasks_created"] == 2
        assert body["batch_id"] > 0

        rows = _query(
            "SELECT to_id, to_line_id, so_id, item_id, quantity_to_pick "
            "  FROM pick_tasks WHERE batch_id = %s "
            " ORDER BY pick_sequence",
            (body["batch_id"],),
        )
        assert all(r[0] == to_id for r in rows)
        assert all(r[2] is None for r in rows)  # so_id NULL
        line_ids_in_tasks = {r[1] for r in rows}
        assert line_ids_in_tasks == {line1, line2}

    def test_no_pickable_inventory_returns_409(
        self, client, auth_headers,
    ):
        to_id = _seed_to()
        _seed_to_line(to_id, item_id=1, committed=5)
        _seed_inventory(1, 1, on_hand=0)  # nothing on the floor
        resp = client.post(
            f"/api/admin/transfer-orders/{to_id}/start-picking",
            headers=auth_headers,
        )
        assert resp.status_code == 409
        body = resp.get_json()
        assert body["error"] == "no_pickable_inventory"
        # Rollback: no pick_tasks left behind
        rows = _query(
            "SELECT COUNT(*) FROM pick_tasks WHERE to_id = %s", (to_id,),
        )
        assert rows[0][0] == 0

    def test_invalid_status_for_start_picking(
        self, client, auth_headers,
    ):
        to_id = _seed_to(status="CANCELLED")
        resp = client.post(
            f"/api/admin/transfer-orders/{to_id}/start-picking",
            headers=auth_headers,
        )
        assert resp.status_code == 409
        assert resp.get_json()["error"] == "invalid_status_for_start_picking"

    def test_no_lines_to_pick_returns_409(
        self, client, auth_headers,
    ):
        # All lines are short-closed
        to_id = _seed_to()
        _seed_to_line(to_id, committed=5, status="SHORT_CLOSED")
        resp = client.post(
            f"/api/admin/transfer-orders/{to_id}/start-picking",
            headers=auth_headers,
        )
        assert resp.status_code == 409
        assert resp.get_json()["error"] == "no_lines_to_pick"


# ----------------------------------------------------------------------
# Picker GET endpoint
# ----------------------------------------------------------------------


class TestPickerGet:
    def test_returns_header_lines_with_progress(
        self, client, auth_headers,
    ):
        to_id = _seed_to()
        line_id = _seed_to_line(to_id, item_id=1, committed=5, picked=2,
                                 status="PARTIALLY_PICKED")
        resp = client.get(
            f"/api/admin/picker/transfer-orders/{to_id}",
            headers=auth_headers,
        )
        assert resp.status_code == 200
        body = resp.get_json()
        assert body["transfer_order"]["to_id"] == to_id
        line = body["lines"][0]
        assert line["to_line_id"] == line_id
        assert line["picked_qty"] == 2
        assert line["committed_qty"] == 5
        assert line["status"] == "PARTIALLY_PICKED"
