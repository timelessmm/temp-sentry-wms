"""v1.10.0 POS endpoint surface.

Endpoints (this commit):

    GET /api/v1/pos/availability      -- per-warehouse, per-bin stock

The remaining three POST routes (validate-cart, checkout, refund)
arrive in subsequent commits and reuse this blueprint.

Per-request shape:
- @require_wms_token: validates X-WMS-Token, refuses cross-direction
  bridging, refuses tokens without pos.dispatch in endpoints. The
  decorator's V1100 dispatcher branch is what gates this surface.
- @limiter.limit per route, keyed on the token. Availability is the
  high-frequency path (one call per barcode scan); the 120/min budget
  reflects that.
- @with_db opens the request-scoped SQLAlchemy session.
- Every response (success and failure) carries
  X-Sentry-Canonical-Model: DRAFT-v1 so consumers can detect schema-
  stability stage on each response, including 4xx.
- Standard error body shape:
      {"error_kind": str, "message": str, "details": {}}

Path / query parameter validation:
- barcode and sku are matched against ^[A-Za-z0-9_\\-#.]+$ length 1..64
  before any DB query so a malformed value never reaches the
  parameterized binding. The regex is defense-in-depth on top of
  SQLAlchemy's bind parameter handling, mirroring the dockd
  _validate_so_number posture.
"""

import re

from flask import Blueprint, g, jsonify, make_response, request
from sqlalchemy import text

from middleware.auth_middleware import require_wms_token
from middleware.db import with_db
from services.rate_limit import limiter


pos_bp = Blueprint("pos", __name__)


# Lookup-key regex. Matches the dockd so_number shape with a tighter
# 64-char cap (UPCs and SKUs are short; barcodes scanned by the
# Honeywell readers cap at ~50 chars).
_LOOKUP_RE = re.compile(r"^[A-Za-z0-9_\-#.]+$")
_LOOKUP_MAX_LEN = 64


def _draft_response(body, status_code=200, extra_headers=None):
    """Build a Flask response carrying the POS canonical-model header
    on every response (success or failure). Mirrors the dockd helper."""
    response = make_response(jsonify(body), status_code)
    response.headers["X-Sentry-Canonical-Model"] = "DRAFT-v1"
    if extra_headers:
        for k, v in extra_headers.items():
            response.headers[k] = v
    return response


def _err(error_kind, message, status_code, details=None):
    return _draft_response(
        {
            "error_kind": error_kind,
            "message": message,
            "details": details or {},
        },
        status_code,
    )


def _validate_lookup_value(value, field_name):
    """Returns None if valid, otherwise an error response."""
    if not value or len(value) > _LOOKUP_MAX_LEN:
        return _err(
            "invalid_query_param",
            f"{field_name} length out of range",
            422,
            {"field": field_name},
        )
    if not _LOOKUP_RE.match(value):
        return _err(
            "invalid_query_param",
            f"{field_name} contains disallowed characters",
            422,
            {"field": field_name},
        )
    return None


# ----------------------------------------------------------------------
# GET /api/v1/pos/availability
# ----------------------------------------------------------------------


@pos_bp.route("/availability", methods=["GET"])
@require_wms_token
@limiter.limit("120 per minute")
@with_db
def availability():
    """Per-warehouse, per-bin availability for one item.

    One of barcode or sku is required (XOR). The response groups
    inventory by warehouse, then by bin. Empty bins (qty <= 0) and
    empty warehouses (sum(qty) <= 0) are omitted entirely; the POS
    Service surfaces the empty case as "out of stock" via the
    `availability: []` shape rather than a 404.

    SKU truly missing OR only present in warehouses outside the token
    scope -> 404 item_not_found (conflated to prevent enumeration).
    """
    barcode = request.args.get("barcode")
    sku = request.args.get("sku")

    if (barcode and sku) or (not barcode and not sku):
        return _err(
            "invalid_query_param",
            "exactly one of barcode or sku is required",
            422,
            {"field": "barcode|sku"},
        )

    if barcode is not None:
        err = _validate_lookup_value(barcode, "barcode")
        if err is not None:
            return err
    if sku is not None:
        err = _validate_lookup_value(sku, "sku")
        if err is not None:
            return err

    token_warehouse_ids = list(g.current_token.get("warehouse_ids") or [])

    # Look up the item. Both branches return the same canonical row so
    # the rest of the function does not branch on which key was used.
    if barcode is not None:
        item = g.db.execute(
            text(
                """
                SELECT item_id, sku, item_name, upc, is_active
                  FROM items
                 WHERE upc = :barcode
                 LIMIT 1
                """
            ),
            {"barcode": barcode},
        ).fetchone()
    else:
        item = g.db.execute(
            text(
                """
                SELECT item_id, sku, item_name, upc, is_active
                  FROM items
                 WHERE sku = :sku
                 LIMIT 1
                """
            ),
            {"sku": sku},
        ).fetchone()

    if item is None or not item.is_active:
        return _err("item_not_found", "no item matches the given identifier", 404)

    # No warehouses in token scope: same 404 conflation as dockd.
    if not token_warehouse_ids:
        return _err("item_not_found", "no item matches the given identifier", 404)

    # Pull every inventory row for this item that the token can see.
    # Collapse lots within a (warehouse, bin) so the response shows one
    # qty per bin, not per lot. The lot dimension is internal; the POS
    # surface presents bins.
    rows = g.db.execute(
        text(
            """
            SELECT w.warehouse_code,
                   w.warehouse_name,
                   b.bin_code,
                   SUM(inv.quantity_on_hand - inv.quantity_allocated) AS qty
              FROM inventory inv
              JOIN warehouses w ON w.warehouse_id = inv.warehouse_id
              JOIN bins b       ON b.bin_id       = inv.bin_id
             WHERE inv.item_id      = :item_id
               AND inv.warehouse_id = ANY(:wh_ids)
             GROUP BY w.warehouse_code, w.warehouse_name, b.bin_code
             HAVING SUM(inv.quantity_on_hand - inv.quantity_allocated) > 0
             ORDER BY w.warehouse_code, b.bin_code
            """
        ),
        {"item_id": item.item_id, "wh_ids": token_warehouse_ids},
    ).fetchall()

    # Group by warehouse. Rows are pre-ordered by warehouse_code so a
    # single pass is enough.
    availability_by_warehouse = []
    current = None
    for r in rows:
        if current is None or current["warehouse_id"] != r.warehouse_code:
            if current is not None:
                availability_by_warehouse.append(current)
            current = {
                "warehouse_id":   r.warehouse_code,
                "warehouse_name": r.warehouse_name,
                "qty_available":  0,
                "bins":           [],
            }
        current["bins"].append({
            "bin_id":   r.bin_code,
            "bin_name": r.bin_code,
            "qty":      int(r.qty),
        })
        current["qty_available"] += int(r.qty)
    if current is not None:
        availability_by_warehouse.append(current)

    body = {
        "sku":          item.sku,
        "name":         item.item_name,
        "barcode":      item.upc,
        # is_taxable is hardcoded true. items has no is_taxable column
        # in v1.10; a follow-up adds the column when AvidMax has tax-
        # exempt SKUs. The POS Service treats every item as taxable
        # under the universal tax rate from its .env.
        "is_taxable":   True,
        "availability": availability_by_warehouse,
    }
    return _draft_response(body, 200)
