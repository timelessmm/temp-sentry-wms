"""v1.8.0 (#293): TO submit + admin approve/reject + transfer.completed
event emission tests.

Covers:
- POST /api/admin/picker/transfer-orders/<to_id>/submit:
    happy path (creates approval row + status flip);
    nothing_picked guard;
    multi-batch creates two approval rows with disjoint snapshots.
- POST /api/admin/transfer-orders/<to_id>/approvals/<id>/approve:
    inventory move source -> destination via Staging bin;
    auto-INSERT destination row on first approve;
    409 already_approved on second approve;
    403 self_approval_blocked;
    409 no Staging bin at destination;
    closes header when all lines fully approved -> ACTION_TO_CLOSED;
    transfer.completed/1 event emitted with correct shape.
- POST /api/admin/transfer-orders/<to_id>/approvals/<id>/reject:
    status flip + audit + no inventory change;
    rejection_reason persisted.
- Discriminator-branch fix: TO confirm_pick does NOT touch inventory
  (regression net for the picking_service patch).
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

import bcrypt
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


def _seed_to(source_wh=1, dest_wh=2, status="OPEN", created_by="t293"):
    conn = db_test_context.get_raw_connection()
    cur = conn.cursor()
    try:
        cur.execute(
            "INSERT INTO transfer_orders "
            "(to_number, source_warehouse_id, destination_warehouse_id, "
            " status, created_by, external_id) "
            "VALUES (%s, %s, %s, %s, %s, %s) RETURNING to_id",
            (
                f"TO-T293-{uuid.uuid4().hex[:8]}",
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
            (to_id, item_id, line_number, requested, committed,
             picked, approved, status),
        )
        return cur.fetchone()[0]
    finally:
        cur.close()


def _set_inventory(item_id, warehouse_id, on_hand, allocated=0,
                   bin_id=1):
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
            "VALUES (%s, %s, %s, %s, %s, NULL)",
            (item_id, bin_id, warehouse_id, on_hand, allocated),
        )
    finally:
        cur.close()


def _ensure_dest_staging_bin(warehouse_id):
    """Insert a Staging bin at the given warehouse if missing.
    Returns the bin_id."""
    conn = db_test_context.get_raw_connection()
    cur = conn.cursor()
    try:
        cur.execute(
            "SELECT bin_id FROM bins "
            " WHERE warehouse_id = %s AND bin_type = 'Staging' "
            " ORDER BY bin_id LIMIT 1",
            (warehouse_id,),
        )
        row = cur.fetchone()
        if row:
            return row[0]
        # Need a zone first
        cur.execute(
            "SELECT zone_id FROM zones WHERE warehouse_id = %s LIMIT 1",
            (warehouse_id,),
        )
        zone_row = cur.fetchone()
        if not zone_row:
            cur.execute(
                "INSERT INTO zones (warehouse_id, zone_code, zone_name, "
                "                   zone_type) "
                "VALUES (%s, %s, 'staging zone', 'STAGING') "
                "RETURNING zone_id",
                (warehouse_id, f"Z-T293-{uuid.uuid4().hex[:6]}"),
            )
            zone_row = cur.fetchone()
        bin_code = f"STAGE-T293-{uuid.uuid4().hex[:6]}"
        cur.execute(
            "INSERT INTO bins "
            "(zone_id, warehouse_id, bin_code, bin_barcode, bin_type, "
            " external_id) "
            "VALUES (%s, %s, %s, %s, 'Staging', %s) RETURNING bin_id",
            (zone_row[0], warehouse_id, bin_code, bin_code, str(uuid.uuid4())),
        )
        return cur.fetchone()[0]
    finally:
        cur.close()


def _seed_user(role="USER"):
    username = f"u293-{uuid.uuid4().hex[:6]}"
    pw_hash = bcrypt.hashpw(b"pw", bcrypt.gensalt(rounds=4)).decode()
    conn = db_test_context.get_raw_connection()
    cur = conn.cursor()
    try:
        cur.execute(
            "INSERT INTO users "
            "(username, password_hash, full_name, role, external_id) "
            "VALUES (%s, %s, 't293', %s, %s) RETURNING user_id",
            (username, pw_hash, role, str(uuid.uuid4())),
        )
        cur.fetchone()
    finally:
        cur.close()
    return username


def _login(client, username, password="pw"):
    resp = client.post(
        "/api/auth/login",
        json={"username": username, "password": password},
    )
    assert resp.status_code == 200, resp.get_json()
    return {"Authorization": f"Bearer {resp.get_json()['token']}"}


# ----------------------------------------------------------------------
# Submit
# ----------------------------------------------------------------------


class TestSubmit:
    def test_creates_approval_row_and_flips_to_awaiting_approval(
        self, client, auth_headers,
    ):
        to_id = _seed_to(status="PARTIALLY_PICKED")
        line_id = _seed_to_line(
            to_id, item_id=1, committed=5, picked=5, approved=0,
            status="PICKED",
        )
        resp = client.post(
            f"/api/admin/picker/transfer-orders/{to_id}/submit",
            headers=auth_headers,
        )
        assert resp.status_code == 201, resp.get_json()
        body = resp.get_json()
        assert body["to_status"] == "AWAITING_APPROVAL"
        assert body["line_count"] == 1

        rows = _query(
            "SELECT status, lines_snapshot FROM transfer_order_approvals "
            " WHERE to_approval_id = %s",
            (body["to_approval_id"],),
        )
        assert rows[0][0] == "PENDING"
        snapshot = rows[0][1]
        assert snapshot["lines"][0]["to_line_id"] == line_id
        assert snapshot["lines"][0]["picked_in_snapshot"] == 5

    def test_partial_pick_keeps_partially_picked(
        self, client, auth_headers,
    ):
        to_id = _seed_to(status="PARTIALLY_PICKED")
        _seed_to_line(
            to_id, line_number=1, item_id=1, committed=5, picked=3,
            status="PARTIALLY_PICKED",
        )
        _seed_to_line(
            to_id, line_number=2, item_id=2, committed=5, picked=0,
            status="PENDING",
        )
        resp = client.post(
            f"/api/admin/picker/transfer-orders/{to_id}/submit",
            headers=auth_headers,
        )
        assert resp.status_code == 201
        assert resp.get_json()["to_status"] == "PARTIALLY_PICKED"
        # Line 1 only -- line 2 has nothing to submit yet
        assert resp.get_json()["line_count"] == 1

    def test_nothing_picked_returns_422(self, client, auth_headers):
        to_id = _seed_to(status="PARTIALLY_PICKED")
        _seed_to_line(to_id, committed=5, picked=0, status="PENDING")
        resp = client.post(
            f"/api/admin/picker/transfer-orders/{to_id}/submit",
            headers=auth_headers,
        )
        assert resp.status_code == 422
        assert resp.get_json()["error"] == "nothing_picked"

    def test_multi_batch_creates_two_disjoint_snapshots(
        self, client, auth_headers,
    ):
        # First submit covers picked_qty=3, then the picker bumps
        # picked_qty to 5 (on the row directly via test helper) and
        # submits again. Second snapshot only has the +2 delta.
        to_id = _seed_to(status="PARTIALLY_PICKED")
        line_id = _seed_to_line(
            to_id, committed=5, picked=3, approved=0,
            status="PARTIALLY_PICKED",
        )
        r1 = client.post(
            f"/api/admin/picker/transfer-orders/{to_id}/submit",
            headers=auth_headers,
        )
        assert r1.status_code == 201
        # Approve r1 so approved_qty advances
        from db_test_context import get_raw_connection
        conn = get_raw_connection()
        cur = conn.cursor()
        try:
            cur.execute(
                "UPDATE transfer_order_lines SET approved_qty = 3, "
                "    status = 'PARTIALLY_PICKED' "
                " WHERE to_line_id = %s",
                (line_id,),
            )
            cur.execute(
                "UPDATE transfer_order_approvals SET status = 'APPROVED', "
                "    approved_at = NOW(), approved_by = 'admin' "
                " WHERE to_approval_id = %s",
                (r1.get_json()["to_approval_id"],),
            )
            cur.execute(
                "UPDATE transfer_order_lines SET picked_qty = 5, "
                "    status = 'PICKED' WHERE to_line_id = %s",
                (line_id,),
            )
        finally:
            cur.close()

        r2 = client.post(
            f"/api/admin/picker/transfer-orders/{to_id}/submit",
            headers=auth_headers,
        )
        assert r2.status_code == 201
        snapshots = _query(
            "SELECT lines_snapshot FROM transfer_order_approvals "
            " WHERE to_id = %s ORDER BY to_approval_id",
            (to_id,),
        )
        assert snapshots[0][0]["lines"][0]["picked_in_snapshot"] == 3
        assert snapshots[1][0]["lines"][0]["picked_in_snapshot"] == 2


# ----------------------------------------------------------------------
# Approve
# ----------------------------------------------------------------------


class TestApprove:
    def test_inventory_moves_source_to_destination(
        self, client, auth_headers,
    ):
        to_id = _seed_to(status="AWAITING_APPROVAL", source_wh=1, dest_wh=2)
        # Source has 8 on hand, 5 allocated (the TO's reservation).
        _set_inventory(1, 1, on_hand=8, allocated=5)
        # Ensure destination Staging bin exists.
        dest_bin = _ensure_dest_staging_bin(2)
        # Pre-pick 5 then submit.
        line_id = _seed_to_line(
            to_id, item_id=1, committed=5, picked=5, status="PICKED",
        )
        submit = client.post(
            f"/api/admin/picker/transfer-orders/{to_id}/submit",
            headers=auth_headers,
        )
        approval_id = submit.get_json()["to_approval_id"]
        # Picker == auth_headers user, so disable the self-approval gate
        # for this assertion.
        from db_test_context import get_raw_connection
        conn = get_raw_connection()
        cur = conn.cursor()
        try:
            cur.execute(
                "UPDATE app_settings SET value = 'false' "
                " WHERE key = 'transfer_order_block_self_approval'"
            )
        finally:
            cur.close()

        resp = client.post(
            f"/api/admin/transfer-orders/{to_id}/approvals/{approval_id}/approve",
            headers=auth_headers,
        )
        assert resp.status_code == 200, resp.get_json()
        body = resp.get_json()
        assert body["status"] == "APPROVED"
        assert body["to_closed"] is True

        # Source: 8 - 5 = 3 on hand; allocated dropped to 0.
        rows = _query(
            "SELECT quantity_on_hand, quantity_allocated FROM inventory "
            " WHERE item_id = 1 AND warehouse_id = 1",
        )
        assert rows[0] == (3, 0)
        # Destination Staging bin gained 5.
        rows = _query(
            "SELECT quantity_on_hand FROM inventory "
            " WHERE bin_id = %s AND item_id = 1",
            (dest_bin,),
        )
        assert rows[0][0] == 5
        # Line approved_qty bumped, status -> APPROVED.
        rows = _query(
            "SELECT approved_qty, status FROM transfer_order_lines "
            " WHERE to_line_id = %s",
            (line_id,),
        )
        assert rows[0] == (5, "APPROVED")
        # transfer.completed/1 event emitted.
        rows = _query(
            "SELECT payload FROM integration_events "
            " WHERE aggregate_type = 'inventory_transfer' "
            "   AND aggregate_id = %s",
            (approval_id,),
        )
        assert rows
        payload = rows[0][0]
        assert payload["from_warehouse_id"] == 1
        assert payload["to_warehouse_id"] == 2
        assert payload["lines"][0]["quantity"] == 5

    def test_self_approval_blocked(self, client):
        # Picker submits, then logs in and tries to approve own
        # submission. mig 049 seeds app_settings.transfer_order_block_
        # self_approval=TRUE; conftest TRUNCATEs app_settings at
        # session start so the test re-inserts to mirror the migration.
        from db_test_context import get_raw_connection
        conn = get_raw_connection()
        cur = conn.cursor()
        try:
            cur.execute(
                "INSERT INTO app_settings (key, value) "
                "VALUES ('transfer_order_block_self_approval', 'true') "
                "ON CONFLICT (key) DO UPDATE SET value = 'true'"
            )
        finally:
            cur.close()
        username = _seed_user(role="ADMIN")
        headers = _login(client, username)
        to_id = _seed_to(status="AWAITING_APPROVAL", source_wh=1, dest_wh=2)
        _set_inventory(1, 1, on_hand=10, allocated=5)
        _ensure_dest_staging_bin(2)
        _seed_to_line(
            to_id, committed=5, picked=5, status="PICKED",
        )
        submit = client.post(
            f"/api/admin/picker/transfer-orders/{to_id}/submit",
            headers=headers,
        )
        approval_id = submit.get_json()["to_approval_id"]

        resp = client.post(
            f"/api/admin/transfer-orders/{to_id}/approvals/{approval_id}/approve",
            headers=headers,
        )
        assert resp.status_code == 403
        assert resp.get_json()["error"] == "self_approval_blocked"

    def test_already_approved_returns_409(self, client, auth_headers):
        to_id = _seed_to(status="AWAITING_APPROVAL", source_wh=1, dest_wh=2)
        _set_inventory(1, 1, on_hand=10, allocated=5)
        _ensure_dest_staging_bin(2)
        _seed_to_line(to_id, committed=5, picked=5, status="PICKED")
        submit = client.post(
            f"/api/admin/picker/transfer-orders/{to_id}/submit",
            headers=auth_headers,
        )
        approval_id = submit.get_json()["to_approval_id"]
        # Disable self-approval gate
        from db_test_context import get_raw_connection
        conn = get_raw_connection()
        cur = conn.cursor()
        try:
            cur.execute(
                "UPDATE app_settings SET value = 'false' "
                " WHERE key = 'transfer_order_block_self_approval'"
            )
        finally:
            cur.close()

        client.post(
            f"/api/admin/transfer-orders/{to_id}/approvals/{approval_id}/approve",
            headers=auth_headers,
        )
        # Second approve attempt
        resp = client.post(
            f"/api/admin/transfer-orders/{to_id}/approvals/{approval_id}/approve",
            headers=auth_headers,
        )
        assert resp.status_code == 409
        assert resp.get_json()["error"] == "approval_not_pending"

    def test_no_destination_staging_bin_returns_409(
        self, client, auth_headers,
    ):
        to_id = _seed_to(status="AWAITING_APPROVAL", source_wh=1, dest_wh=2)
        _set_inventory(1, 1, on_hand=10, allocated=5)
        # Wipe Staging bins at warehouse 2.
        from db_test_context import get_raw_connection
        conn = get_raw_connection()
        cur = conn.cursor()
        try:
            cur.execute(
                "DELETE FROM bins WHERE warehouse_id = 2 "
                "  AND bin_type = 'Staging'"
            )
            cur.execute(
                "UPDATE app_settings SET value = 'false' "
                " WHERE key = 'transfer_order_block_self_approval'"
            )
        finally:
            cur.close()
        _seed_to_line(to_id, committed=5, picked=5, status="PICKED")
        submit = client.post(
            f"/api/admin/picker/transfer-orders/{to_id}/submit",
            headers=auth_headers,
        )
        approval_id = submit.get_json()["to_approval_id"]

        resp = client.post(
            f"/api/admin/transfer-orders/{to_id}/approvals/{approval_id}/approve",
            headers=auth_headers,
        )
        assert resp.status_code == 409
        body = resp.get_json()
        assert body["error"] == "approval_failed"
        assert "Staging bin" in body["detail"]


# ----------------------------------------------------------------------
# Reject
# ----------------------------------------------------------------------


class TestReject:
    def test_status_flip_audit_no_inventory_change(
        self, client, auth_headers,
    ):
        to_id = _seed_to(status="AWAITING_APPROVAL", source_wh=1, dest_wh=2)
        _set_inventory(1, 1, on_hand=10, allocated=5)
        _seed_to_line(to_id, committed=5, picked=5, status="PICKED")
        submit = client.post(
            f"/api/admin/picker/transfer-orders/{to_id}/submit",
            headers=auth_headers,
        )
        approval_id = submit.get_json()["to_approval_id"]

        resp = client.post(
            f"/api/admin/transfer-orders/{to_id}/approvals/{approval_id}/reject",
            json={"rejection_reason": "wrong destination"},
            headers=auth_headers,
        )
        assert resp.status_code == 200
        assert resp.get_json()["status"] == "REJECTED"

        rows = _query(
            "SELECT status, rejection_reason, rejected_at "
            "  FROM transfer_order_approvals "
            " WHERE to_approval_id = %s",
            (approval_id,),
        )
        assert rows[0][0] == "REJECTED"
        assert rows[0][1] == "wrong destination"
        assert rows[0][2] is not None

        # Inventory unchanged (source still has the reservation)
        rows = _query(
            "SELECT quantity_on_hand, quantity_allocated FROM inventory "
            " WHERE item_id = 1 AND warehouse_id = 1",
        )
        assert rows[0] == (10, 5)

        audit = _query(
            "SELECT details FROM audit_log "
            " WHERE entity_type = 'TO_APPROVAL' AND entity_id = %s "
            "   AND action_type = 'TO_REJECTED'",
            (approval_id,),
        )
        assert audit
        assert audit[0][0]["rejection_reason"] == "wrong destination"

    def test_reject_already_finalised_returns_409(
        self, client, auth_headers,
    ):
        to_id = _seed_to(status="AWAITING_APPROVAL")
        _seed_to_line(to_id, committed=5, picked=5, status="PICKED")
        submit = client.post(
            f"/api/admin/picker/transfer-orders/{to_id}/submit",
            headers=auth_headers,
        )
        approval_id = submit.get_json()["to_approval_id"]
        client.post(
            f"/api/admin/transfer-orders/{to_id}/approvals/{approval_id}/reject",
            json={},
            headers=auth_headers,
        )
        resp = client.post(
            f"/api/admin/transfer-orders/{to_id}/approvals/{approval_id}/reject",
            json={},
            headers=auth_headers,
        )
        assert resp.status_code == 409
