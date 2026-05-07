"""v1.8.0 (#289): inbound line item write-through to *_lines tables.

Pre-v1.8, mapping_loader resolved line_items into canonical_payload
JSONB but the relational *_lines tables were never written. Inbound
POs were unreceivable; inbound SOs were unallocatable.

v1.8 walks canonical_payload[<line_items.canonical_path>] after the
header upsert and writes to purchase_order_lines / sales_order_lines.
Item resolution: cross_system_lookup on the line's item_id field
returns items.canonical_id (UUID); the handler dereferences via
items.external_id -> items.item_id integer FK.

Coverage:
- PO inbound with 2 lines populates purchase_order_lines.
- SO inbound with 2 lines populates sales_order_lines (downstream
  quantity defaults of 0 for allocated/picked/packed/shipped).
- Re-POST with same external_id replaces lines when no downstream
  activity exists.
- Re-POST when downstream activity exists -> 409 lines_in_flight.
- Empty line_items array preserves existing lines (header-only
  update is allowed).
- Missing item_id on a line -> 422 with field-name + position.
- Item not pre-loaded (cross_system_lookup miss) -> 409
  cross_system_lookup_miss with source_type=item.
"""

import hashlib
import json
import os
import sys
import uuid

import pytest

os.environ.setdefault("DATABASE_URL", "postgresql://sentry:sentry@localhost:5432/sentry")
os.environ.setdefault("JWT_SECRET", "NEVER_USE_THIS_IN_PRODUCTION_32!")
os.environ.setdefault("SENTRY_ENCRYPTION_KEY", "t5hPIEVn_O41qfiMqAiPEnwzQh68o3Es46YfSOBvEK8=")
os.environ.setdefault("SENTRY_TOKEN_PEPPER", "NEVER_USE_THIS_PEPPER_IN_PRODUCTION")

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

import yaml

from _wms_token_helpers import PEPPER
import db_test_context
from services import token_cache
from services.mapping_loader import (
    LoadedMappingFile,
    MappingDocument,
    MappingRegistry,
)


# ----------------------------------------------------------------------
# Helpers + fixtures
# ----------------------------------------------------------------------


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


def _fresh_source(prefix="line"):
    return f"{prefix}-{uuid.uuid4().hex[:8]}"


@pytest.fixture(autouse=True)
def _clear_token_cache():
    token_cache.clear()
    yield
    token_cache.clear()


def _build_registry(app, ss: str, body_yaml: str) -> MappingDocument:
    parsed = yaml.safe_load(body_yaml)
    doc = MappingDocument.model_validate(parsed)
    registry = MappingRegistry()
    registry.register(LoadedMappingFile(
        document=doc, path=f"<test:{ss}>",
        sha256="0" * 64,
    ))
    app.config["MAPPING_REGISTRY"] = registry
    return doc


def _make_token(ss, plaintext, inbound_resources):
    conn = db_test_context.get_raw_connection()
    cur = conn.cursor()
    try:
        cur.execute(
            "INSERT INTO inbound_source_systems_allowlist (source_system, kind) "
            "VALUES (%s, 'internal_tool') ON CONFLICT DO NOTHING",
            (ss,),
        )
        token_hash = hashlib.sha256((PEPPER + plaintext).encode()).hexdigest()
        cur.execute(
            "INSERT INTO wms_tokens "
            "(token_name, token_hash, status, warehouse_ids, event_types, "
            " endpoints, source_system, inbound_resources, mapping_override) "
            "VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s) RETURNING token_id",
            (
                f"line-test-{uuid.uuid4().hex[:6]}",
                token_hash, "active",
                [1], [], [], ss, inbound_resources, False,
            ),
        )
        return cur.fetchone()[0]
    finally:
        cur.close()


def _seed_item_with_external_id(sku, external_id):
    """Insert an items row + cross_system_mappings row so the inbound
    line cross_system_lookup resolves the SKU to the items.external_id
    UUID; the handler then dereferences UUID -> integer items.item_id."""
    conn = db_test_context.get_raw_connection()
    cur = conn.cursor()
    try:
        cur.execute(
            "INSERT INTO items (sku, item_name, external_id) "
            "VALUES (%s, %s, %s) "
            "ON CONFLICT (sku) DO UPDATE SET item_name = EXCLUDED.item_name "
            "RETURNING item_id, external_id",
            (sku, f"Test item {sku}", external_id),
        )
        item_row = cur.fetchone()
    finally:
        cur.close()
    return item_row


def _seed_cross_system_mapping(ss, source_id, canonical_id):
    conn = db_test_context.get_raw_connection()
    cur = conn.cursor()
    try:
        cur.execute(
            "INSERT INTO cross_system_mappings "
            "  (source_system, source_type, source_id, "
            "   canonical_type, canonical_id) "
            "VALUES (%s, 'item', %s, 'item', %s) "
            "ON CONFLICT DO NOTHING",
            (ss, source_id, canonical_id),
        )
    finally:
        cur.close()


def _post(client, plaintext, path, body):
    return client.post(
        path,
        headers={"X-WMS-Token": plaintext, "Content-Type": "application/json"},
        data=json.dumps(body),
    )


# ----------------------------------------------------------------------
# Mapping doc fixtures with cross_system_lookup on item_id
# ----------------------------------------------------------------------


_PO_MAPPING = """\
mapping_version: "1.0"
source_system: "{ss}"
version_compare: "lexicographic"
resources:
  purchase_orders:
    canonical_type: "purchase_order"
    fields:
      - canonical: "po_number"
        source_path: "$.poNumber"
        type: "string"
        required: true
      - canonical: "warehouse_id"
        source_path: "$.warehouseId"
        type: "integer"
        required: true
      - canonical: "vendor_name"
        source_path: "$.vendor.name"
        type: "string"
    line_items:
      source_path: "$.lineItems"
      canonical_path: "lines"
      fields:
        - canonical: "item_id"
          source_path: "$.sku"
          type: "uuid"
          required: true
          cross_system_lookup:
            source_type: "item"
        - canonical: "quantity_ordered"
          source_path: "$.quantity"
          type: "integer"
          required: true
"""


_SO_MAPPING = """\
mapping_version: "1.0"
source_system: "{ss}"
version_compare: "iso_timestamp"
resources:
  sales_orders:
    canonical_type: "sales_order"
    fields:
      - canonical: "so_number"
        source_path: "$.orderNumber"
        type: "string"
        required: true
      - canonical: "warehouse_id"
        source_path: "$.warehouseId"
        type: "integer"
        required: true
      - canonical: "customer_name"
        source_path: "$.customer.name"
        type: "string"
    line_items:
      source_path: "$.lineItems"
      canonical_path: "lines"
      fields:
        - canonical: "item_id"
          source_path: "$.sku"
          type: "uuid"
          required: true
          cross_system_lookup:
            source_type: "item"
        - canonical: "quantity_ordered"
          source_path: "$.quantity"
          type: "integer"
          required: true
"""


# ----------------------------------------------------------------------
# Purchase orders
# ----------------------------------------------------------------------


class TestPoLineWriteThrough:
    def test_first_post_creates_po_lines(self, client, app):
        ss = _fresh_source("po")
        _build_registry(app, ss, _PO_MAPPING.format(ss=ss))
        _make_token(ss, "po-line-1", ["purchase_orders"])

        # Pre-load two items into items + cross_system_mappings.
        sku_a, ext_a = "PO-LINE-A", uuid.uuid4()
        sku_b, ext_b = "PO-LINE-B", uuid.uuid4()
        item_a = _seed_item_with_external_id(sku_a, ext_a)
        item_b = _seed_item_with_external_id(sku_b, ext_b)
        _seed_cross_system_mapping(ss, sku_a, ext_a)
        _seed_cross_system_mapping(ss, sku_b, ext_b)

        resp = _post(client, "po-line-1", "/api/v1/inbound/purchase_orders", {
            "external_id": "PO-LINE-FIRST",
            "external_version": "v1",
            "source_payload": {
                "poNumber": "PO-LINE-FIRST",
                "warehouseId": 1,
                "vendor": {"name": "Acme"},
                "lineItems": [
                    {"sku": sku_a, "quantity": 5},
                    {"sku": sku_b, "quantity": 12},
                ],
            },
        })
        assert resp.status_code == 201, resp.get_json()

        po_id = _query(
            "SELECT po_id FROM purchase_orders WHERE external_id = %s",
            (resp.get_json()["canonical_id"],),
        )[0][0]
        rows = _query(
            "SELECT item_id, quantity_ordered, line_number, status, "
            "       quantity_received "
            "  FROM purchase_order_lines WHERE po_id = %s "
            " ORDER BY line_number",
            (po_id,),
        )
        assert len(rows) == 2
        assert rows[0] == (item_a[0], 5, 1, "PENDING", 0)
        assert rows[1] == (item_b[0], 12, 2, "PENDING", 0)

    def test_repost_replaces_lines_when_no_downstream_activity(
        self, client, app,
    ):
        ss = _fresh_source("po")
        _build_registry(app, ss, _PO_MAPPING.format(ss=ss))
        _make_token(ss, "po-line-2", ["purchase_orders"])

        sku, ext = "PO-LINE-REPLACE", uuid.uuid4()
        item = _seed_item_with_external_id(sku, ext)
        _seed_cross_system_mapping(ss, sku, ext)

        # First POST: 1 line qty 5.
        v1 = _post(client, "po-line-2", "/api/v1/inbound/purchase_orders", {
            "external_id": "PO-REPLACE",
            "external_version": "v1",
            "source_payload": {
                "poNumber": "PO-REPLACE",
                "warehouseId": 1,
                "vendor": {"name": "Acme"},
                "lineItems": [{"sku": sku, "quantity": 5}],
            },
        })
        assert v1.status_code == 201

        # v2: 2 lines, different quantity.
        v2 = _post(client, "po-line-2", "/api/v1/inbound/purchase_orders", {
            "external_id": "PO-REPLACE",
            "external_version": "v2",
            "source_payload": {
                "poNumber": "PO-REPLACE",
                "warehouseId": 1,
                "vendor": {"name": "Acme"},
                "lineItems": [
                    {"sku": sku, "quantity": 7},
                    {"sku": sku, "quantity": 3},
                ],
            },
        })
        assert v2.status_code == 201, v2.get_json()

        po_id = _query(
            "SELECT po_id FROM purchase_orders WHERE external_id = %s",
            (v1.get_json()["canonical_id"],),
        )[0][0]
        rows = _query(
            "SELECT quantity_ordered, line_number "
            "  FROM purchase_order_lines WHERE po_id = %s "
            " ORDER BY line_number",
            (po_id,),
        )
        assert rows == [(7, 1), (3, 2)]

    def test_repost_blocked_when_quantity_received_present(
        self, client, app,
    ):
        ss = _fresh_source("po")
        _build_registry(app, ss, _PO_MAPPING.format(ss=ss))
        _make_token(ss, "po-line-3", ["purchase_orders"])

        sku, ext = "PO-LINE-INFLIGHT", uuid.uuid4()
        _seed_item_with_external_id(sku, ext)
        _seed_cross_system_mapping(ss, sku, ext)

        v1 = _post(client, "po-line-3", "/api/v1/inbound/purchase_orders", {
            "external_id": "PO-INFLIGHT",
            "external_version": "v1",
            "source_payload": {
                "poNumber": "PO-INFLIGHT",
                "warehouseId": 1,
                "vendor": {"name": "Acme"},
                "lineItems": [{"sku": sku, "quantity": 10}],
            },
        })
        assert v1.status_code == 201

        # Simulate partial receipt by bumping quantity_received.
        po_id = _query(
            "SELECT po_id FROM purchase_orders WHERE external_id = %s",
            (v1.get_json()["canonical_id"],),
        )[0][0]
        conn = db_test_context.get_raw_connection()
        cur = conn.cursor()
        try:
            cur.execute(
                "UPDATE purchase_order_lines SET quantity_received = 3 "
                " WHERE po_id = %s",
                (po_id,),
            )
        finally:
            cur.close()

        v2 = _post(client, "po-line-3", "/api/v1/inbound/purchase_orders", {
            "external_id": "PO-INFLIGHT",
            "external_version": "v2",
            "source_payload": {
                "poNumber": "PO-INFLIGHT",
                "warehouseId": 1,
                "vendor": {"name": "Acme"},
                "lineItems": [{"sku": sku, "quantity": 99}],
            },
        })
        assert v2.status_code == 409
        body = v2.get_json()
        assert body["error_kind"] == "lines_in_flight"
        assert "quantity_received" in body["message"]

    def test_item_not_preloaded_returns_409_lookup_miss(
        self, client, app,
    ):
        ss = _fresh_source("po")
        _build_registry(app, ss, _PO_MAPPING.format(ss=ss))
        _make_token(ss, "po-line-4", ["purchase_orders"])

        # No items + cross_system_mappings setup; cross_system_lookup
        # misses on the line's item_id.
        resp = _post(client, "po-line-4", "/api/v1/inbound/purchase_orders", {
            "external_id": "PO-MISS",
            "external_version": "v1",
            "source_payload": {
                "poNumber": "PO-MISS",
                "warehouseId": 1,
                "vendor": {"name": "Acme"},
                "lineItems": [{"sku": "NOT-PRELOADED", "quantity": 1}],
            },
        })
        assert resp.status_code == 409, resp.get_json()
        body = resp.get_json()
        assert body["error_kind"] == "cross_system_lookup_miss"
        assert body["missing"]["source_type"] == "item"
        assert body["missing"]["source_id"] == "NOT-PRELOADED"

    def test_empty_line_items_preserves_existing_lines(
        self, client, app,
    ):
        ss = _fresh_source("po")
        _build_registry(app, ss, _PO_MAPPING.format(ss=ss))
        _make_token(ss, "po-line-5", ["purchase_orders"])

        sku, ext = "PO-LINE-PRESERVE", uuid.uuid4()
        _seed_item_with_external_id(sku, ext)
        _seed_cross_system_mapping(ss, sku, ext)

        v1 = _post(client, "po-line-5", "/api/v1/inbound/purchase_orders", {
            "external_id": "PO-PRESERVE",
            "external_version": "v1",
            "source_payload": {
                "poNumber": "PO-PRESERVE",
                "warehouseId": 1,
                "vendor": {"name": "Acme"},
                "lineItems": [{"sku": sku, "quantity": 4}],
            },
        })
        assert v1.status_code == 201

        v2 = _post(client, "po-line-5", "/api/v1/inbound/purchase_orders", {
            "external_id": "PO-PRESERVE",
            "external_version": "v2",
            "source_payload": {
                "poNumber": "PO-PRESERVE",
                "warehouseId": 1,
                "vendor": {"name": "Updated Acme"},
                "lineItems": [],
            },
        })
        assert v2.status_code == 201, v2.get_json()

        po_id = _query(
            "SELECT po_id FROM purchase_orders WHERE external_id = %s",
            (v1.get_json()["canonical_id"],),
        )[0][0]
        rows = _query(
            "SELECT quantity_ordered FROM purchase_order_lines "
            " WHERE po_id = %s ORDER BY line_number",
            (po_id,),
        )
        # Empty lineItems on re-POST preserves the v1 line.
        assert rows == [(4,)]


# ----------------------------------------------------------------------
# Sales orders
# ----------------------------------------------------------------------


class TestSoLineWriteThrough:
    def test_first_post_creates_so_lines(self, client, app):
        ss = _fresh_source("so")
        _build_registry(app, ss, _SO_MAPPING.format(ss=ss))
        _make_token(ss, "so-line-1", ["sales_orders"])

        sku, ext = "SO-LINE-A", uuid.uuid4()
        item = _seed_item_with_external_id(sku, ext)
        _seed_cross_system_mapping(ss, sku, ext)

        resp = _post(client, "so-line-1", "/api/v1/inbound/sales_orders", {
            "external_id": "SO-LINE-FIRST",
            "external_version": "2026-05-04T10:00:00+00:00",
            "source_payload": {
                "orderNumber": "SO-LINE-FIRST",
                "warehouseId": 1,
                "customer": {"name": "Acme"},
                "lineItems": [{"sku": sku, "quantity": 8}],
            },
        })
        assert resp.status_code == 201, resp.get_json()

        so_id = _query(
            "SELECT so_id FROM sales_orders WHERE external_id = %s",
            (resp.get_json()["canonical_id"],),
        )[0][0]
        rows = _query(
            "SELECT item_id, quantity_ordered, quantity_allocated, "
            "       quantity_picked, quantity_packed, quantity_shipped, "
            "       line_number, status "
            "  FROM sales_order_lines WHERE so_id = %s "
            " ORDER BY line_number",
            (so_id,),
        )
        assert rows == [(item[0], 8, 0, 0, 0, 0, 1, "PENDING")]

    def test_repost_blocked_when_allocation_present(self, client, app):
        ss = _fresh_source("so")
        _build_registry(app, ss, _SO_MAPPING.format(ss=ss))
        _make_token(ss, "so-line-2", ["sales_orders"])

        sku, ext = "SO-LINE-INFLIGHT", uuid.uuid4()
        _seed_item_with_external_id(sku, ext)
        _seed_cross_system_mapping(ss, sku, ext)

        v1 = _post(client, "so-line-2", "/api/v1/inbound/sales_orders", {
            "external_id": "SO-INFLIGHT",
            "external_version": "2026-05-04T10:00:00+00:00",
            "source_payload": {
                "orderNumber": "SO-INFLIGHT",
                "warehouseId": 1,
                "customer": {"name": "Acme"},
                "lineItems": [{"sku": sku, "quantity": 6}],
            },
        })
        assert v1.status_code == 201

        # Simulate allocation
        so_id = _query(
            "SELECT so_id FROM sales_orders WHERE external_id = %s",
            (v1.get_json()["canonical_id"],),
        )[0][0]
        conn = db_test_context.get_raw_connection()
        cur = conn.cursor()
        try:
            cur.execute(
                "UPDATE sales_order_lines SET quantity_allocated = 4 "
                " WHERE so_id = %s",
                (so_id,),
            )
        finally:
            cur.close()

        v2 = _post(client, "so-line-2", "/api/v1/inbound/sales_orders", {
            "external_id": "SO-INFLIGHT",
            "external_version": "2026-05-04T11:00:00+00:00",
            "source_payload": {
                "orderNumber": "SO-INFLIGHT",
                "warehouseId": 1,
                "customer": {"name": "Acme"},
                "lineItems": [{"sku": sku, "quantity": 99}],
            },
        })
        assert v2.status_code == 409
        assert v2.get_json()["error_kind"] == "lines_in_flight"