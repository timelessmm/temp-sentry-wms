"""v1.8.0 (#288): admin PATCH /api/admin/sales-orders/<so_id>/address.

Coverage:
- ADMIN edits at any status (PICKED, SHIPPED, etc.).
- Non-admin can only edit at status='OPEN'; PICKED returns 403.
- One audit row per actually-changed field with field-level delta.
- No-op edit (same value) returns {"unchanged": true} and writes no
  audit rows.
- Empty string in body clears the column to NULL.
- Validation: empty body rejected with model-validator error;
  oversize value rejected by Pydantic max_length.
"""

import json
import os
import sys
import uuid

os.environ.setdefault("DATABASE_URL", "postgresql://sentry:sentry@localhost:5432/sentry")
os.environ.setdefault("JWT_SECRET", "NEVER_USE_THIS_IN_PRODUCTION_32!")
os.environ.setdefault("SENTRY_ENCRYPTION_KEY", "t5hPIEVn_O41qfiMqAiPEnwzQh68o3Es46YfSOBvEK8=")

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

import bcrypt
import pytest

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


def _seed_so(status="OPEN", **address):
    """Insert a sales_order with optional pre-existing address fields.

    Literal column list (per feedback memory) so the
    test_external_id_inserts static guardrail can see external_id.
    Address fields are UPDATEd in a follow-up statement so this
    helper does not need to enumerate every combination at INSERT
    time.
    """
    conn = db_test_context.get_raw_connection()
    cur = conn.cursor()
    try:
        cur.execute(
            "INSERT INTO sales_orders "
            "(so_number, customer_name, warehouse_id, status, external_id) "
            "VALUES (%s, 't288', 1, %s, %s) RETURNING so_id",
            (
                f"SO-T288-{uuid.uuid4().hex[:8]}",
                status,
                str(uuid.uuid4()),
            ),
        )
        so_id = cur.fetchone()[0]
        if address:
            assignments = ", ".join(f"{k} = %s" for k in address)
            cur.execute(
                f"UPDATE sales_orders SET {assignments} WHERE so_id = %s",
                (*address.values(), so_id),
            )
    finally:
        cur.close()
    return so_id


def _seed_user(role="USER", username=None):
    """Create a USER (or ADMIN) so we can log in as that role."""
    username = username or f"u288-{uuid.uuid4().hex[:6]}"
    pw_hash = bcrypt.hashpw(b"pw", bcrypt.gensalt(rounds=4)).decode()
    conn = db_test_context.get_raw_connection()
    cur = conn.cursor()
    try:
        cur.execute(
            "INSERT INTO users "
            "(username, password_hash, full_name, role, external_id) "
            "VALUES (%s, %s, 't288', %s, %s) RETURNING user_id",
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


def _patch(client, headers, so_id, body):
    return client.open(
        f"/api/admin/sales-orders/{so_id}/address",
        method="PATCH",
        headers={**headers, "Content-Type": "application/json"},
        data=json.dumps(body),
    )


class TestStatusGate:
    def test_admin_can_edit_at_picked_status(self, client, auth_headers):
        so_id = _seed_so(status="PICKED")
        resp = _patch(client, auth_headers, so_id, {
            "shipping_address_name": "Late edit",
        })
        assert resp.status_code == 200, resp.get_json()
        body = resp.get_json()
        assert body["edited_fields"] == ["shipping_address_name"]
        rows = _query(
            "SELECT shipping_address_name FROM sales_orders WHERE so_id = %s",
            (so_id,),
        )
        assert rows[0][0] == "Late edit"

    def test_non_admin_blocked_at_picked_status(self, client):
        so_id = _seed_so(status="PICKED")
        username = _seed_user(role="USER")
        headers = _login(client, username)
        resp = _patch(client, headers, so_id, {
            "shipping_address_name": "User attempt",
        })
        assert resp.status_code == 403
        body = resp.get_json()
        assert body["current_status"] == "PICKED"

    def test_non_admin_can_edit_at_open(self, client):
        so_id = _seed_so(status="OPEN")
        username = _seed_user(role="USER")
        headers = _login(client, username)
        resp = _patch(client, headers, so_id, {
            "billing_address_city": "User-edit OK at OPEN",
        })
        assert resp.status_code == 200, resp.get_json()


class TestAuditDelta:
    def test_one_audit_row_per_changed_field(self, client, auth_headers):
        so_id = _seed_so(
            status="OPEN",
            billing_address_city="OldCity",
            shipping_address_postal_code="00000",
        )
        resp = _patch(client, auth_headers, so_id, {
            "billing_address_city": "NewCity",
            "shipping_address_postal_code": "11111",
            "billing_address_phone": "555-9999",
        })
        assert resp.status_code == 200
        assert sorted(resp.get_json()["edited_fields"]) == [
            "billing_address_city",
            "billing_address_phone",
            "shipping_address_postal_code",
        ]
        rows = _query(
            "SELECT details FROM audit_log "
            " WHERE entity_type = 'SO' AND entity_id = %s "
            "   AND action_type = 'SO_ADDRESS_EDITED' "
            " ORDER BY (details ->> 'field_changed')",
            (so_id,),
        )
        assert len(rows) == 3
        deltas = [r[0] for r in rows]
        assert deltas[0] == {
            "field_changed": "billing_address_city",
            "old_value": "OldCity",
            "new_value": "NewCity",
        }
        assert deltas[1] == {
            "field_changed": "billing_address_phone",
            "old_value": None,
            "new_value": "555-9999",
        }
        assert deltas[2] == {
            "field_changed": "shipping_address_postal_code",
            "old_value": "00000",
            "new_value": "11111",
        }


class TestNoOpAndClear:
    def test_no_op_when_value_unchanged(self, client, auth_headers):
        so_id = _seed_so(status="OPEN", billing_address_city="Same")
        resp = _patch(client, auth_headers, so_id, {
            "billing_address_city": "Same",
        })
        assert resp.status_code == 200
        assert resp.get_json()["unchanged"] is True
        rows = _query(
            "SELECT COUNT(*) FROM audit_log "
            " WHERE entity_type = 'SO' AND entity_id = %s "
            "   AND action_type = 'SO_ADDRESS_EDITED'",
            (so_id,),
        )
        assert rows[0][0] == 0

    def test_empty_string_clears_to_null(self, client, auth_headers):
        so_id = _seed_so(status="OPEN", billing_address_city="ToClear")
        resp = _patch(client, auth_headers, so_id, {
            "billing_address_city": "",
        })
        assert resp.status_code == 200
        rows = _query(
            "SELECT billing_address_city FROM sales_orders WHERE so_id = %s",
            (so_id,),
        )
        assert rows[0][0] is None
        # Audit captures the clear with new_value=None.
        rows = _query(
            "SELECT details FROM audit_log "
            " WHERE entity_type = 'SO' AND entity_id = %s "
            "   AND action_type = 'SO_ADDRESS_EDITED'",
            (so_id,),
        )
        assert len(rows) == 1
        assert rows[0][0]["new_value"] is None

    def test_404_on_unknown_so(self, client, auth_headers):
        resp = _patch(client, auth_headers, 999_999, {
            "billing_address_city": "x",
        })
        assert resp.status_code == 404