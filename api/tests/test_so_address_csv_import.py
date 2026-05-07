"""v1.8.0 (#288): CSV import surfaces the 16 structured billing /
shipping address fields on sales_orders.

Coverage:
- All 16 fields populate on the canonical row when present in CSV.
- Subset works: any combination of present + absent fields lands.
- Formula-prefix protection extends to every new field.
- max_length VARCHAR enforcement catches oversize values.
"""

import os
import sys

os.environ.setdefault("DATABASE_URL", "postgresql://sentry:sentry@localhost:5432/sentry")
os.environ.setdefault("JWT_SECRET", "NEVER_USE_THIS_IN_PRODUCTION_32!")
os.environ.setdefault("SENTRY_ENCRYPTION_KEY", "t5hPIEVn_O41qfiMqAiPEnwzQh68o3Es46YfSOBvEK8=")

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

import pytest

import db_test_context


_ALL_ADDRESS_KEYS = (
    "billing_address_name", "billing_address_line1", "billing_address_line2",
    "billing_address_city", "billing_address_state",
    "billing_address_postal_code", "billing_address_country",
    "billing_address_phone",
    "shipping_address_name", "shipping_address_line1", "shipping_address_line2",
    "shipping_address_city", "shipping_address_state",
    "shipping_address_postal_code", "shipping_address_country",
    "shipping_address_phone",
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


@pytest.fixture
def _seed_item():
    """Insert a single SKU so the SO import can resolve it."""
    conn = db_test_context.get_raw_connection()
    cur = conn.cursor()
    try:
        cur.execute(
            "INSERT INTO items (sku, item_name, external_id) "
            "VALUES (%s, %s, gen_random_uuid()) "
            "ON CONFLICT (sku) DO UPDATE SET item_name = EXCLUDED.item_name "
            "RETURNING item_id",
            ("ADDR-SKU", "Address-test SKU"),
        )
        cur.fetchone()
    finally:
        cur.close()
    return "ADDR-SKU"


class TestSoAddressCsvImport:
    def test_all_16_fields_populate(self, client, auth_headers, _seed_item):
        record = {
            "so_number": "SO-ADDR-FULL",
            "sku": _seed_item,
            "quantity": 1,
            "warehouse_id": 1,
            "billing_address_name": "Bill Recipient",
            "billing_address_line1": "1 Pay St",
            "billing_address_line2": "Suite 5",
            "billing_address_city": "Bill City",
            "billing_address_state": "BS",
            "billing_address_postal_code": "11111",
            "billing_address_country": "US",
            "billing_address_phone": "555-0001",
            "shipping_address_name": "Ship Recipient",
            "shipping_address_line1": "2 Drop Rd",
            "shipping_address_line2": "Dock B",
            "shipping_address_city": "Ship City",
            "shipping_address_state": "SS",
            "shipping_address_postal_code": "22222",
            "shipping_address_country": "CA",
            "shipping_address_phone": "555-0002",
        }
        resp = client.post(
            "/api/admin/import/sales-orders",
            json={"records": [record]},
            headers=auth_headers,
        )
        assert resp.status_code == 200, resp.get_json()
        body = resp.get_json()
        assert body["imported"] == 1
        assert body["skipped"] == 0

        cols = ", ".join(_ALL_ADDRESS_KEYS)
        rows = _query(
            f"SELECT {cols} FROM sales_orders WHERE so_number = %s",
            ("SO-ADDR-FULL",),
        )
        assert rows
        stored = dict(zip(_ALL_ADDRESS_KEYS, rows[0]))
        for key in _ALL_ADDRESS_KEYS:
            assert stored[key] == record[key], f"{key}: {stored[key]!r}"

    def test_subset_fields_partial_populate(
        self, client, auth_headers, _seed_item,
    ):
        record = {
            "so_number": "SO-ADDR-PART",
            "sku": _seed_item,
            "quantity": 1,
            "warehouse_id": 1,
            "shipping_address_name": "Ship Only",
            "shipping_address_postal_code": "33333",
        }
        resp = client.post(
            "/api/admin/import/sales-orders",
            json={"records": [record]},
            headers=auth_headers,
        )
        assert resp.status_code == 200
        assert resp.get_json()["imported"] == 1

        cols = ", ".join(_ALL_ADDRESS_KEYS)
        rows = _query(
            f"SELECT {cols} FROM sales_orders WHERE so_number = %s",
            ("SO-ADDR-PART",),
        )
        stored = dict(zip(_ALL_ADDRESS_KEYS, rows[0]))
        assert stored["shipping_address_name"] == "Ship Only"
        assert stored["shipping_address_postal_code"] == "33333"
        # Every unpopulated field is NULL
        unpopulated = set(_ALL_ADDRESS_KEYS) - {
            "shipping_address_name", "shipping_address_postal_code",
        }
        for key in unpopulated:
            assert stored[key] is None, f"{key} should be NULL: {stored[key]!r}"

    def test_formula_prefix_rejected_on_address_field(
        self, client, auth_headers, _seed_item,
    ):
        # Each new address field is wired into the _no_formula validator
        # via the field_validator extension; a formula-prefixed value
        # rejects the row.
        record = {
            "so_number": "SO-ADDR-FORMULA",
            "sku": _seed_item,
            "quantity": 1,
            "warehouse_id": 1,
            "billing_address_line1": "=cmd|/c calc",
        }
        resp = client.post(
            "/api/admin/import/sales-orders",
            json={"records": [record]},
            headers=auth_headers,
        )
        body = resp.get_json()
        assert body["imported"] == 0
        assert body["skipped"] == 1

    def test_oversize_value_rejected(
        self, client, auth_headers, _seed_item,
    ):
        # billing_address_postal_code is VARCHAR(32); the Pydantic
        # max_length=32 catches a 33-char value at parse time.
        record = {
            "so_number": "SO-ADDR-OVERSIZE",
            "sku": _seed_item,
            "quantity": 1,
            "warehouse_id": 1,
            "billing_address_postal_code": "X" * 33,
        }
        resp = client.post(
            "/api/admin/import/sales-orders",
            json={"records": [record]},
            headers=auth_headers,
        )
        body = resp.get_json()
        assert body["imported"] == 0
        assert body["skipped"] == 1
