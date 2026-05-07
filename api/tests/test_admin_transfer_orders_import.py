"""Integration tests for POST /api/admin/transfer-orders/import
(v1.8.0 #291).

Covers:
- Happy path: create TO with two lines, full availability -> 201,
  no shortage payload, lines PENDING.
- Shortage path: requested > available -> committed = available,
  shortage payload populated, line lands PENDING when committed > 0
  or SHORT_CLOSED when committed == 0.
- Duplicate-SKU rows: each gets its own line; later row's available
  reflects earlier row's commit.
- Source = destination warehouse code -> 422.
- Unknown source / destination warehouse code -> 404.
- Per-row errors aggregate (validation + unknown_sku) -> 422 with
  rows array.
- Inventory.quantity_allocated bumped per item after import.
- Audit row TO_CREATED written with shortage count in details.
- Same-millisecond TO number collision retries once.
"""

import os
import sys
import uuid
from unittest import mock

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


def _seed_item(sku):
    conn = db_test_context.get_raw_connection()
    cur = conn.cursor()
    try:
        cur.execute(
            "INSERT INTO items (sku, item_name, external_id) "
            "VALUES (%s, %s, %s) ON CONFLICT (sku) DO NOTHING "
            "RETURNING item_id",
            (sku, f"Test {sku}", str(uuid.uuid4())),
        )
        row = cur.fetchone()
        if row is None:
            cur.execute(
                "SELECT item_id FROM items WHERE sku = %s", (sku,),
            )
            row = cur.fetchone()
    finally:
        cur.close()
    return row[0]


def _set_inventory(item_id, warehouse_id, on_hand, allocated=0,
                   bin_id=1, lot_number=None):
    """Inventory uses (item_id, bin_id, lot_number) as its unique
    constraint, so the test creates one row at the seed bin_id and
    bumps it on conflict."""
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
            "VALUES (%s, %s, %s, %s, %s, %s)",
            (item_id, bin_id, warehouse_id, on_hand, allocated, lot_number),
        )
    finally:
        cur.close()


def _allocated(item_id, warehouse_id):
    rows = _query(
        "SELECT COALESCE(SUM(quantity_allocated), 0) FROM inventory "
        " WHERE item_id = %s AND warehouse_id = %s",
        (item_id, warehouse_id),
    )
    return rows[0][0] if rows else 0


def _warehouse_code(warehouse_id):
    rows = _query(
        "SELECT warehouse_code FROM warehouses WHERE warehouse_id = %s",
        (warehouse_id,),
    )
    return rows[0][0] if rows else None


@pytest.fixture
def two_skus():
    a = _seed_item(f"TOIMP-A-{uuid.uuid4().hex[:6]}")
    b = _seed_item(f"TOIMP-B-{uuid.uuid4().hex[:6]}")
    _set_inventory(a, 1, 100, 0)
    _set_inventory(b, 1, 50, 0)
    return a, b


# ----------------------------------------------------------------------
# Happy + shortage paths
# ----------------------------------------------------------------------


class TestImport:
    def test_happy_path_full_availability(self, client, auth_headers, two_skus):
        a, b = two_skus
        sku_a = _query("SELECT sku FROM items WHERE item_id = %s", (a,))[0][0]
        sku_b = _query("SELECT sku FROM items WHERE item_id = %s", (b,))[0][0]

        resp = client.post(
            "/api/admin/transfer-orders/import",
            json={
                "source_warehouse_code": _warehouse_code(1),
                "destination_warehouse_code": _warehouse_code(2),
                "records": [
                    {"sku": sku_a, "quantity": 5},
                    {"sku": sku_b, "quantity": 12},
                ],
            },
            headers=auth_headers,
        )
        assert resp.status_code == 201, resp.get_json()
        body = resp.get_json()
        assert body["to_number"].startswith("TO-")
        assert body["line_count"] == 2
        assert body["shortages"] == []

        # Reservations land in inventory.quantity_allocated at source
        assert _allocated(a, 1) == 5
        assert _allocated(b, 1) == 12

        # Lines persist with committed_qty == requested_qty
        rows = _query(
            "SELECT requested_qty, committed_qty, status "
            "  FROM transfer_order_lines WHERE to_id = %s "
            " ORDER BY line_number",
            (body["to_id"],),
        )
        assert rows[0] == (5, 5, "PENDING")
        assert rows[1] == (12, 12, "PENDING")

        # Audit row written
        audit = _query(
            "SELECT details FROM audit_log "
            " WHERE entity_type = 'TO' AND entity_id = %s "
            "   AND action_type = 'TO_CREATED'",
            (body["to_id"],),
        )
        assert audit
        assert audit[0][0]["line_count"] == 2
        assert audit[0][0]["shortage_count"] == 0

    def test_partial_shortage(self, client, auth_headers, two_skus):
        a, _ = two_skus
        _set_inventory(a, 1, 30, 0)
        sku_a = _query("SELECT sku FROM items WHERE item_id = %s", (a,))[0][0]

        resp = client.post(
            "/api/admin/transfer-orders/import",
            json={
                "source_warehouse_code": _warehouse_code(1),
                "destination_warehouse_code": _warehouse_code(2),
                "records": [{"sku": sku_a, "quantity": 100}],
            },
            headers=auth_headers,
        )
        assert resp.status_code == 201, resp.get_json()
        body = resp.get_json()
        assert len(body["shortages"]) == 1
        s = body["shortages"][0]
        assert s["sku"] == sku_a
        assert s["requested_qty"] == 100
        assert s["committed_qty"] == 30
        assert s["shortfall"] == 70
        # Inventory carries the partial reservation
        assert _allocated(a, 1) == 30

    def test_zero_availability_lands_short_closed(
        self, client, auth_headers, two_skus,
    ):
        a, _ = two_skus
        _set_inventory(a, 1, 0, 0)  # nothing available
        sku_a = _query("SELECT sku FROM items WHERE item_id = %s", (a,))[0][0]
        resp = client.post(
            "/api/admin/transfer-orders/import",
            json={
                "source_warehouse_code": _warehouse_code(1),
                "destination_warehouse_code": _warehouse_code(2),
                "records": [{"sku": sku_a, "quantity": 5}],
            },
            headers=auth_headers,
        )
        body = resp.get_json()
        assert resp.status_code == 201, body
        rows = _query(
            "SELECT committed_qty, status FROM transfer_order_lines "
            " WHERE to_id = %s",
            (body["to_id"],),
        )
        assert rows[0] == (0, "SHORT_CLOSED")

    def test_duplicate_sku_rows_each_get_their_own_line(
        self, client, auth_headers, two_skus,
    ):
        a, _ = two_skus
        _set_inventory(a, 1, 8, 0)
        sku_a = _query("SELECT sku FROM items WHERE item_id = %s", (a,))[0][0]
        resp = client.post(
            "/api/admin/transfer-orders/import",
            json={
                "source_warehouse_code": _warehouse_code(1),
                "destination_warehouse_code": _warehouse_code(2),
                "records": [
                    {"sku": sku_a, "quantity": 5},
                    {"sku": sku_a, "quantity": 5},
                ],
            },
            headers=auth_headers,
        )
        body = resp.get_json()
        assert resp.status_code == 201
        assert body["line_count"] == 2
        # Total reservation == on_hand (8); first line gets 5, second
        # gets 3.
        assert _allocated(a, 1) == 8
        rows = _query(
            "SELECT line_number, committed_qty FROM transfer_order_lines "
            " WHERE to_id = %s ORDER BY line_number",
            (body["to_id"],),
        )
        assert rows == [(1, 5), (2, 3)]


# ----------------------------------------------------------------------
# Validation failures
# ----------------------------------------------------------------------


class TestValidation:
    def test_source_equals_destination_rejected(self, client, auth_headers):
        resp = client.post(
            "/api/admin/transfer-orders/import",
            json={
                "source_warehouse_code": _warehouse_code(1),
                "destination_warehouse_code": _warehouse_code(1),
                "records": [{"sku": "ANY", "quantity": 1}],
            },
            headers=auth_headers,
        )
        assert resp.status_code == 422
        assert resp.get_json()["error"] == "source_and_destination_must_differ"

    def test_unknown_source_warehouse_404(self, client, auth_headers):
        resp = client.post(
            "/api/admin/transfer-orders/import",
            json={
                "source_warehouse_code": "DOES-NOT-EXIST",
                "destination_warehouse_code": _warehouse_code(2),
                "records": [{"sku": "ANY", "quantity": 1}],
            },
            headers=auth_headers,
        )
        assert resp.status_code == 404
        body = resp.get_json()
        assert body["error"] == "unknown_warehouse"
        assert body["field"] == "source_warehouse_code"

    def test_unknown_destination_warehouse_404(self, client, auth_headers):
        resp = client.post(
            "/api/admin/transfer-orders/import",
            json={
                "source_warehouse_code": _warehouse_code(1),
                "destination_warehouse_code": "DOES-NOT-EXIST",
                "records": [{"sku": "ANY", "quantity": 1}],
            },
            headers=auth_headers,
        )
        assert resp.status_code == 404
        assert resp.get_json()["field"] == "destination_warehouse_code"

    def test_unknown_sku_aggregates_row_error(self, client, auth_headers):
        resp = client.post(
            "/api/admin/transfer-orders/import",
            json={
                "source_warehouse_code": _warehouse_code(1),
                "destination_warehouse_code": _warehouse_code(2),
                "records": [
                    {"sku": "TOIMP-NOT-A-REAL-SKU", "quantity": 1},
                ],
            },
            headers=auth_headers,
        )
        assert resp.status_code == 422
        body = resp.get_json()
        assert body["error"] == "row_errors"
        assert body["rows"][0]["error_kind"] == "unknown_sku"
        assert body["rows"][0]["sku"] == "TOIMP-NOT-A-REAL-SKU"
        assert body["rows"][0]["row_index"] == 0

    def test_formula_prefix_in_sku_rejected(
        self, client, auth_headers, two_skus,
    ):
        resp = client.post(
            "/api/admin/transfer-orders/import",
            json={
                "source_warehouse_code": _warehouse_code(1),
                "destination_warehouse_code": _warehouse_code(2),
                "records": [{"sku": "=cmd|/c calc", "quantity": 1}],
            },
            headers=auth_headers,
        )
        assert resp.status_code == 422
        body = resp.get_json()
        assert body["error"] == "row_errors"
        assert body["rows"][0]["error_kind"] == "validation_error"

    def test_quantity_zero_rejected(self, client, auth_headers, two_skus):
        a, _ = two_skus
        sku_a = _query("SELECT sku FROM items WHERE item_id = %s", (a,))[0][0]
        resp = client.post(
            "/api/admin/transfer-orders/import",
            json={
                "source_warehouse_code": _warehouse_code(1),
                "destination_warehouse_code": _warehouse_code(2),
                "records": [{"sku": sku_a, "quantity": 0}],
            },
            headers=auth_headers,
        )
        assert resp.status_code == 422
        assert resp.get_json()["error"] == "row_errors"

    def test_empty_records_rejected(self, client, auth_headers):
        resp = client.post(
            "/api/admin/transfer-orders/import",
            json={
                "source_warehouse_code": _warehouse_code(1),
                "destination_warehouse_code": _warehouse_code(2),
                "records": [],
            },
            headers=auth_headers,
        )
        assert resp.status_code == 422


# ----------------------------------------------------------------------
# TO number collision retry
# ----------------------------------------------------------------------


class TestCollisionRetry:
    def test_unique_violation_retried_once(
        self, client, auth_headers, two_skus,
    ):
        # Force generate_to_number to return the same value the first
        # time it's called inside the route, then a different value on
        # retry. UNIQUE on transfer_orders.to_number triggers the
        # IntegrityError path; the route catches and tries again.
        a, _ = two_skus
        sku_a = _query("SELECT sku FROM items WHERE item_id = %s", (a,))[0][0]

        # Pre-insert a TO with a known number to collide against.
        existing_number = "TO-" + "9" * 17
        from db_test_context import get_raw_connection
        conn = get_raw_connection()
        cur = conn.cursor()
        try:
            cur.execute(
                "INSERT INTO transfer_orders "
                "(to_number, source_warehouse_id, destination_warehouse_id, "
                " created_by, external_id) "
                "VALUES (%s, 1, 2, 'precollision', %s) RETURNING to_id",
                (existing_number, str(uuid.uuid4())),
            )
        finally:
            cur.close()

        # First call returns the colliding number, second call returns
        # a fresh one. The route must retry and succeed.
        with mock.patch(
            "routes.admin.admin_transfer_orders.generate_to_number",
            side_effect=[existing_number, "TO-" + "8" * 17],
        ):
            resp = client.post(
                "/api/admin/transfer-orders/import",
                json={
                    "source_warehouse_code": _warehouse_code(1),
                    "destination_warehouse_code": _warehouse_code(2),
                    "records": [{"sku": sku_a, "quantity": 1}],
                },
                headers=auth_headers,
            )
        assert resp.status_code == 201, resp.get_json()
        assert resp.get_json()["to_number"] == "TO-" + "8" * 17
