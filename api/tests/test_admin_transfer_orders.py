"""Integration tests for admin transfer order CRUD routes
(v1.8.0 #290).

Covers:
- GET /api/admin/transfer-orders (list + filters + pagination)
- GET /api/admin/transfer-orders/<to_id> (detail w/ lines + approvals)
- DELETE /api/admin/transfer-orders/<to_id>
  (success path; 409 on picks/approvals)
- POST /api/admin/transfer-orders/<to_id>/cancel
  (releases reservations; 409 on already-approved)
- POST /api/admin/transfer-orders/<to_id>/lines/<line_id>/short-close
  (releases remaining; transitions line to SHORT_CLOSED)
- audit_log row written per state-changing action.

Picking, submission, and admin approval live in Pass 4.3 / 4.4 tests.
"""

import json
import os
import sys
import uuid

import pytest

os.environ.setdefault("DATABASE_URL", "postgresql://sentry:sentry@localhost:5432/sentry")
os.environ.setdefault("JWT_SECRET", "NEVER_USE_THIS_IN_PRODUCTION_32!")
os.environ.setdefault("SENTRY_ENCRYPTION_KEY", "t5hPIEVn_O41qfiMqAiPEnwzQh68o3Es46YfSOBvEK8=")

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

import db_test_context


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


def _seed_to(source_wh=1, dest_wh=2, status="OPEN", created_by="t290"):
    conn = db_test_context.get_raw_connection()
    cur = conn.cursor()
    try:
        cur.execute(
            "INSERT INTO transfer_orders "
            "(to_number, source_warehouse_id, destination_warehouse_id, "
            " status, created_by, external_id) "
            "VALUES (%s, %s, %s, %s, %s, %s) RETURNING to_id",
            (
                f"TO-T290-{uuid.uuid4().hex[:8]}",
                source_wh, dest_wh, status, created_by, str(uuid.uuid4()),
            ),
        )
        return cur.fetchone()[0]
    finally:
        cur.close()


def _seed_to_line(to_id, item_id=1, line_number=1, requested=10,
                  committed=10, picked=0, approved=0, status="PENDING"):
    conn = db_test_context.get_raw_connection()
    cur = conn.cursor()
    try:
        cur.execute(
            "INSERT INTO transfer_order_lines "
            "(to_id, item_id, line_number, requested_qty, committed_qty, "
            " picked_qty, approved_qty, status) "
            "VALUES (%s, %s, %s, %s, %s, %s, %s, %s) RETURNING to_line_id",
            (
                to_id, item_id, line_number, requested, committed,
                picked, approved, status,
            ),
        )
        return cur.fetchone()[0]
    finally:
        cur.close()


def _seed_to_approval(to_id, status="PENDING", submitted_by="picker"):
    conn = db_test_context.get_raw_connection()
    cur = conn.cursor()
    try:
        cur.execute(
            "INSERT INTO transfer_order_approvals "
            "(to_id, submitted_by, lines_snapshot, status, external_id"
            + (", approved_by, approved_at" if status == "APPROVED" else "")
            + ") VALUES (%s, %s, '{}'::jsonb, %s, %s"
            + (", 'admin1', NOW()" if status == "APPROVED" else "")
            + ") RETURNING to_approval_id",
            (to_id, submitted_by, status, str(uuid.uuid4())),
        )
        return cur.fetchone()[0]
    finally:
        cur.close()


def _bump_inventory_allocated(item_id, warehouse_id, delta):
    """Adjust inventory.quantity_allocated to simulate a TO reservation
    so the cancel/delete release-reservation logic has something to
    decrement."""
    conn = db_test_context.get_raw_connection()
    cur = conn.cursor()
    try:
        cur.execute(
            "UPDATE inventory SET quantity_allocated = quantity_allocated + %s "
            " WHERE item_id = %s AND warehouse_id = %s",
            (delta, item_id, warehouse_id),
        )
    finally:
        cur.close()


def _allocated(item_id, warehouse_id):
    rows = _query(
        "SELECT quantity_allocated FROM inventory "
        " WHERE item_id = %s AND warehouse_id = %s",
        (item_id, warehouse_id),
    )
    return rows[0][0] if rows else None


# ----------------------------------------------------------------------
# List + detail
# ----------------------------------------------------------------------


class TestListAndDetail:
    def test_list_returns_paginated_payload(self, client, auth_headers):
        _seed_to()
        _seed_to()
        resp = client.get(
            "/api/admin/transfer-orders?page=1&per_page=10",
            headers=auth_headers,
        )
        assert resp.status_code == 200
        body = resp.get_json()
        assert "transfer_orders" in body
        assert body["page"] == 1
        assert body["per_page"] == 10
        assert body["total"] >= 2

    def test_list_filters_by_status(self, client, auth_headers):
        seed_open = _seed_to(status="OPEN")
        seed_cancelled = _seed_to(status="CANCELLED")
        resp = client.get(
            "/api/admin/transfer-orders?status=OPEN",
            headers=auth_headers,
        )
        body = resp.get_json()
        ids = {r["to_id"] for r in body["transfer_orders"]}
        assert seed_open in ids
        assert seed_cancelled not in ids

    def test_detail_returns_header_lines_and_approvals(
        self, client, auth_headers,
    ):
        to_id = _seed_to()
        line_id = _seed_to_line(to_id, item_id=1)
        approval_id = _seed_to_approval(to_id)
        resp = client.get(
            f"/api/admin/transfer-orders/{to_id}",
            headers=auth_headers,
        )
        assert resp.status_code == 200
        body = resp.get_json()
        assert body["transfer_order"]["to_id"] == to_id
        assert any(line["to_line_id"] == line_id for line in body["lines"])
        assert any(
            a["to_approval_id"] == approval_id for a in body["approvals"]
        )
        # Item join surfaces sku + item_name on each line.
        line = body["lines"][0]
        assert line["sku"] is not None
        assert line["item_name"] is not None

    def test_detail_404_on_unknown_id(self, client, auth_headers):
        resp = client.get(
            "/api/admin/transfer-orders/999999",
            headers=auth_headers,
        )
        assert resp.status_code == 404


# ----------------------------------------------------------------------
# Cancel
# ----------------------------------------------------------------------


class TestCancel:
    def test_cancel_releases_reservation_and_writes_audit(
        self, client, auth_headers,
    ):
        to_id = _seed_to(source_wh=1, dest_wh=2)
        _seed_to_line(to_id, item_id=1, committed=10)
        _bump_inventory_allocated(1, 1, 10)
        before_alloc = _allocated(1, 1)

        resp = client.post(
            f"/api/admin/transfer-orders/{to_id}/cancel",
            headers=auth_headers,
        )
        assert resp.status_code == 200, resp.get_json()
        assert resp.get_json()["status"] == "CANCELLED"

        # Reservation released back to inventory
        assert _allocated(1, 1) == before_alloc - 10

        # Header status flipped
        rows = _query(
            "SELECT status FROM transfer_orders WHERE to_id = %s",
            (to_id,),
        )
        assert rows[0][0] == "CANCELLED"

        # Audit row written
        audit = _query(
            "SELECT details FROM audit_log "
            " WHERE entity_type = 'TO' AND entity_id = %s "
            "   AND action_type = 'TO_CANCELLED'",
            (to_id,),
        )
        assert audit
        assert audit[0][0]["previous_status"] == "OPEN"

    def test_cancel_blocked_when_non_pending_approval_exists(
        self, client, auth_headers,
    ):
        # Multi-batch flow where the picker submitted a partial batch
        # and admin already approved it; the TO is back at
        # PARTIALLY_PICKED waiting for the next batch. Cancel should
        # surface to_already_partially_approved so the operator
        # processes the remaining picks rather than nuking the
        # in-flight approval audit trail.
        to_id = _seed_to(status="PARTIALLY_PICKED")
        _seed_to_line(to_id, item_id=1)
        _seed_to_approval(to_id, status="APPROVED")
        resp = client.post(
            f"/api/admin/transfer-orders/{to_id}/cancel",
            headers=auth_headers,
        )
        assert resp.status_code == 409
        assert resp.get_json()["error"] == "to_already_partially_approved"

    def test_cancel_404_on_unknown_id(self, client, auth_headers):
        resp = client.post(
            "/api/admin/transfer-orders/999999/cancel",
            headers=auth_headers,
        )
        assert resp.status_code == 404


# ----------------------------------------------------------------------
# Delete
# ----------------------------------------------------------------------


class TestDelete:
    def test_delete_open_to_with_no_picks_succeeds(
        self, client, auth_headers,
    ):
        to_id = _seed_to()
        _seed_to_line(to_id, item_id=1, committed=5)
        _bump_inventory_allocated(1, 1, 5)
        before = _allocated(1, 1)

        resp = client.delete(
            f"/api/admin/transfer-orders/{to_id}",
            headers=auth_headers,
        )
        assert resp.status_code == 204
        assert _allocated(1, 1) == before - 5
        rows = _query(
            "SELECT COUNT(*) FROM transfer_orders WHERE to_id = %s",
            (to_id,),
        )
        assert rows[0][0] == 0

    def test_delete_blocked_when_picks_present(
        self, client, auth_headers,
    ):
        to_id = _seed_to()
        _seed_to_line(to_id, item_id=1, committed=5, picked=2,
                      status="PARTIALLY_PICKED")
        resp = client.delete(
            f"/api/admin/transfer-orders/{to_id}",
            headers=auth_headers,
        )
        assert resp.status_code == 409
        body = resp.get_json()
        assert body["error"] == "to_not_deletable"
        assert body["any_picks"] >= 1

    def test_delete_blocked_when_approval_exists(
        self, client, auth_headers,
    ):
        to_id = _seed_to()
        _seed_to_line(to_id, item_id=1)
        _seed_to_approval(to_id, status="PENDING")
        resp = client.delete(
            f"/api/admin/transfer-orders/{to_id}",
            headers=auth_headers,
        )
        assert resp.status_code == 409
        assert resp.get_json()["any_approvals"] >= 1


# ----------------------------------------------------------------------
# Short-close
# ----------------------------------------------------------------------


class TestShortClose:
    def test_short_close_releases_remaining_and_writes_audit(
        self, client, auth_headers,
    ):
        to_id = _seed_to(source_wh=1, dest_wh=2)
        line_id = _seed_to_line(
            to_id, item_id=1, committed=10, picked=4, approved=4,
            status="PARTIALLY_PICKED",
        )
        _bump_inventory_allocated(1, 1, 6)  # remaining = committed - approved
        before = _allocated(1, 1)

        resp = client.post(
            f"/api/admin/transfer-orders/{to_id}/lines/{line_id}/short-close",
            headers=auth_headers,
        )
        assert resp.status_code == 200, resp.get_json()
        body = resp.get_json()
        assert body["status"] == "SHORT_CLOSED"
        assert body["released_qty"] == 6

        assert _allocated(1, 1) == before - 6
        rows = _query(
            "SELECT status FROM transfer_order_lines WHERE to_line_id = %s",
            (line_id,),
        )
        assert rows[0][0] == "SHORT_CLOSED"

        audit = _query(
            "SELECT details FROM audit_log "
            " WHERE entity_type = 'TO_LINE' AND entity_id = %s "
            "   AND action_type = 'TO_LINE_SHORT_CLOSED'",
            (line_id,),
        )
        assert audit
        assert audit[0][0]["released_qty"] == 6

    def test_short_close_blocked_on_already_approved_line(
        self, client, auth_headers,
    ):
        to_id = _seed_to()
        line_id = _seed_to_line(
            to_id, item_id=1, committed=10, picked=10, approved=10,
            status="APPROVED",
        )
        resp = client.post(
            f"/api/admin/transfer-orders/{to_id}/lines/{line_id}/short-close",
            headers=auth_headers,
        )
        assert resp.status_code == 409
        assert resp.get_json()["error"] == "invalid_line_status_for_short_close"

    def test_short_close_404_on_unknown_line(self, client, auth_headers):
        to_id = _seed_to()
        resp = client.post(
            f"/api/admin/transfer-orders/{to_id}/lines/999999/short-close",
            headers=auth_headers,
        )
        assert resp.status_code == 404
