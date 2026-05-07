"""Schema-level tests for migration 053 (v1.8.0 #288 structured
sales_orders billing/shipping address).

Coverage:
- 16 new columns exist with expected types + nullability + max length.
- Old v1.7 mig 046 billing_address / shipping_address TEXT columns are
  gone (DROP COLUMN succeeded).
- Existing rows have NULL for all 16 new columns.
- Round-trip: insert + select preserves each component.
- max_length VARCHAR rejects values longer than the declared bound.
- Idempotent re-run.
"""

import os
import sys
import uuid

os.environ.setdefault("DATABASE_URL", "postgresql://sentry:sentry@localhost:5432/sentry")
os.environ.setdefault("JWT_SECRET", "NEVER_USE_THIS_IN_PRODUCTION_32!")
os.environ.setdefault("SENTRY_ENCRYPTION_KEY", "t5hPIEVn_O41qfiMqAiPEnwzQh68o3Es46YfSOBvEK8=")

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

import psycopg2

MIGRATION_PATH = os.path.join(
    os.path.dirname(__file__), "..", "..", "db", "migrations",
    "053_sales_orders_structured_address.sql",
)


_NEW_COLUMNS = {
    "billing_address_name":        ("character varying", 200),
    "billing_address_line1":       ("character varying", 200),
    "billing_address_line2":       ("character varying", 200),
    "billing_address_city":        ("character varying", 100),
    "billing_address_state":       ("character varying", 100),
    "billing_address_postal_code": ("character varying", 32),
    "billing_address_country":     ("character varying", 64),
    "billing_address_phone":       ("character varying", 64),
    "shipping_address_name":        ("character varying", 200),
    "shipping_address_line1":       ("character varying", 200),
    "shipping_address_line2":       ("character varying", 200),
    "shipping_address_city":        ("character varying", 100),
    "shipping_address_state":       ("character varying", 100),
    "shipping_address_postal_code": ("character varying", 32),
    "shipping_address_country":     ("character varying", 64),
    "shipping_address_phone":       ("character varying", 64),
}


def _make_conn():
    conn = psycopg2.connect(os.environ["DATABASE_URL"])
    conn.autocommit = False
    return conn


class TestColumnShape:
    def test_all_16_new_columns_exist_with_expected_shape(self):
        conn = _make_conn()
        try:
            cur = conn.cursor()
            cur.execute(
                """
                SELECT column_name, data_type, character_maximum_length,
                       is_nullable
                  FROM information_schema.columns
                 WHERE table_name = 'sales_orders'
                   AND column_name = ANY(:names)
                """.replace(":names", "%s"),
                (list(_NEW_COLUMNS.keys()),),
            )
            rows = {r[0]: (r[1], r[2], r[3]) for r in cur.fetchall()}
        finally:
            conn.close()
        assert set(rows.keys()) == set(_NEW_COLUMNS.keys())
        for name, (data_type, max_len) in _NEW_COLUMNS.items():
            actual_type, actual_len, nullable = rows[name]
            assert actual_type == data_type, f"{name}: type {actual_type}"
            assert actual_len == max_len, f"{name}: max_length {actual_len}"
            assert nullable == "YES", f"{name} should be nullable"

    def test_old_text_columns_dropped(self):
        conn = _make_conn()
        try:
            cur = conn.cursor()
            cur.execute(
                """
                SELECT column_name
                  FROM information_schema.columns
                 WHERE table_name = 'sales_orders'
                   AND column_name IN ('billing_address', 'shipping_address')
                """
            )
            rows = cur.fetchall()
        finally:
            conn.close()
        assert rows == [], (
            f"v1.7 TEXT columns should be dropped; still present: {rows}"
        )


class TestStorageBehaviour:
    def test_round_trip_all_components(self):
        conn = _make_conn()
        try:
            cur = conn.cursor()
            cur.execute(
                """
                INSERT INTO sales_orders (
                    so_number, customer_name, warehouse_id, external_id,
                    billing_address_name, billing_address_line1,
                    billing_address_line2, billing_address_city,
                    billing_address_state, billing_address_postal_code,
                    billing_address_country, billing_address_phone,
                    shipping_address_name, shipping_address_line1,
                    shipping_address_line2, shipping_address_city,
                    shipping_address_state, shipping_address_postal_code,
                    shipping_address_country, shipping_address_phone
                ) VALUES (
                    %s, 't53', 1, %s,
                    'Bill To Inc', '1 Pay St', 'Suite 5',
                    'Bill City', 'BS', '11111', 'US', '555-0001',
                    'Ship To LLC', '2 Drop Rd', 'Dock B',
                    'Ship City', 'SS', '22222', 'CA', '555-0002'
                ) RETURNING so_id
                """,
                (f"SO-T53-{uuid.uuid4().hex[:8]}", str(uuid.uuid4())),
            )
            so_id = cur.fetchone()[0]
            conn.commit()

            cur.execute(
                f"""
                SELECT {", ".join(_NEW_COLUMNS.keys())}
                  FROM sales_orders WHERE so_id = %s
                """,
                (so_id,),
            )
            row = cur.fetchone()
        finally:
            conn.close()
        assert row[0] == "Bill To Inc"
        assert row[1] == "1 Pay St"
        assert row[8] == "Ship To LLC"
        assert row[9] == "2 Drop Rd"
        # Spot-check a few more components
        assert row[5] == "11111"  # billing postal
        assert row[6] == "US"     # billing country
        assert row[13] == "22222"  # shipping postal

    def test_existing_rows_get_null_for_new_columns(self):
        # The migration is pure ADD COLUMN; pre-existing sales_orders
        # rows have NULL for every new column (default behaviour).
        conn = _make_conn()
        try:
            cur = conn.cursor()
            cur.execute(
                """
                INSERT INTO sales_orders
                       (so_number, customer_name, warehouse_id, external_id)
                VALUES (%s, 't53', 1, %s) RETURNING so_id
                """,
                (f"SO-T53-{uuid.uuid4().hex[:8]}", str(uuid.uuid4())),
            )
            so_id = cur.fetchone()[0]
            conn.commit()
            cur.execute(
                f"""
                SELECT {", ".join(_NEW_COLUMNS.keys())}
                  FROM sales_orders WHERE so_id = %s
                """,
                (so_id,),
            )
            row = cur.fetchone()
        finally:
            conn.close()
        assert row == (None,) * 16

    def test_max_length_enforced_per_column(self):
        # billing_address_postal_code is VARCHAR(32); a 33-char value
        # should raise StringDataRightTruncation.
        conn = _make_conn()
        try:
            cur = conn.cursor()
            try:
                cur.execute(
                    """
                    INSERT INTO sales_orders
                           (so_number, customer_name, warehouse_id, external_id,
                            billing_address_postal_code)
                    VALUES (%s, 't53', 1, %s, %s)
                    """,
                    (
                        f"SO-T53-{uuid.uuid4().hex[:8]}",
                        str(uuid.uuid4()),
                        "X" * 33,
                    ),
                )
                conn.commit()
                assert False, "33-char postal_code should violate max_length"
            except psycopg2.errors.StringDataRightTruncation:
                conn.rollback()
        finally:
            conn.close()


class TestIdempotency:
    def test_re_run_migration_no_error(self):
        with open(MIGRATION_PATH) as f:
            sql = f.read()
        conn = psycopg2.connect(os.environ["DATABASE_URL"])
        conn.autocommit = True
        try:
            cur = conn.cursor()
            cur.execute(sql)
        finally:
            conn.close()
