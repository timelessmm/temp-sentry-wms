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
from pydantic import ValidationError
from sqlalchemy import text

from middleware.auth_middleware import require_wms_token
from middleware.db import with_db
from schemas.pos import ValidateCartBody
from services.pos_service import get_max_body_kb
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

    # In-scope produced no available stock. Distinguish "genuinely out
    # of stock everywhere" (return 200 [] so the POS Service can show
    # 'out of stock') from "stock only in warehouses outside the token
    # scope" (return 404 to prevent the token from inferring sister-
    # warehouse membership). A single LIMIT 1 probe is enough; we just
    # need to know whether ANY out-of-scope row holds available qty.
    if not rows:
        leak = g.db.execute(
            text(
                """
                SELECT 1
                  FROM inventory inv
                 WHERE inv.item_id      = :item_id
                   AND inv.warehouse_id != ALL(:wh_ids)
                   AND (inv.quantity_on_hand - inv.quantity_allocated) > 0
                 LIMIT 1
                """
            ),
            {"item_id": item.item_id, "wh_ids": token_warehouse_ids},
        ).fetchone()
        if leak is not None:
            return _err("item_not_found", "no item matches the given identifier", 404)

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


# ----------------------------------------------------------------------
# POST /api/v1/pos/validate-cart
# ----------------------------------------------------------------------


def _classify_line(row, token_warehouse_ids):
    """Map one bulk-query row to a conflict reason or None.

    Reason precedence (most informative first):
      sku_not_found ->
      item_inactive ->
      warehouse_not_found ->
      warehouse_not_in_scope ->
      bin_not_found ->
      insufficient_stock

    A line that has multiple problems surfaces under the first
    precedence-order reason that applies; the cashier sees the most
    actionable cause without enumerating sister failures.
    """
    if row.item_id is None:
        return "sku_not_found", None
    if not row.is_active:
        return "item_inactive", None
    if row.warehouse_id is None:
        return "warehouse_not_found", None
    if row.warehouse_id not in token_warehouse_ids:
        return "warehouse_not_in_scope", None
    if row.bin_id is None:
        return "bin_not_found", None
    available = int(row.available)
    if available < int(row.requested_qty):
        return "insufficient_stock", available
    return None, None


@pos_bp.route("/validate-cart", methods=["POST"])
@require_wms_token
@limiter.limit("60 per minute")
@with_db
def validate_cart():
    """Pre-flight cart validation called by the POS Service just before
    initiating a Windcave charge. Read-only. Returns 200 valid:true
    when every line passes; 409 valid:false with all conflicts in one
    response when any line fails.
    """
    cap_bytes = get_max_body_kb() * 1024
    if request.content_length is not None and request.content_length > cap_bytes:
        return _err(
            "body_too_large",
            "request body exceeds SENTRY_POS_MAX_BODY_KB",
            413,
            {"max_body_kb": get_max_body_kb()},
        )

    try:
        body = ValidateCartBody.model_validate(request.get_json(silent=False))
    except ValidationError as exc:
        first = exc.errors()[0]
        field = ".".join(str(p) for p in first.get("loc", ()))
        return _err(
            "invalid_body",
            "body failed schema validation",
            422,
            {"field": field, "reason": first.get("type", "value_error")},
        )
    except Exception:
        return _err("invalid_body", "body is not valid JSON", 422)

    token_warehouse_ids = list(g.current_token.get("warehouse_ids") or [])

    # Bulk classification query. unnest() turns the five parallel arrays
    # into one row per line, then LEFT JOINs resolve each lookup
    # independently so a missing item / warehouse / bin produces a NULL
    # column instead of dropping the row. The aggregate keeps only
    # in-scope inventory contributing to available qty (the warehouse-
    # scope conflation lives in the Python classifier, not the SQL).
    rows = g.db.execute(
        text(
            """
            SELECT i.idx,
                   i.sku, i.warehouse_code, i.bin_code, i.requested_qty,
                   itm.item_id, itm.is_active,
                   w.warehouse_id,
                   b.bin_id,
                   COALESCE(SUM(inv.quantity_on_hand - inv.quantity_allocated), 0) AS available
              FROM unnest(
                       CAST(:idxs       AS int[]),
                       CAST(:skus       AS text[]),
                       CAST(:wh_codes   AS text[]),
                       CAST(:bin_codes  AS text[]),
                       CAST(:qtys       AS int[])
                   ) AS i(idx, sku, warehouse_code, bin_code, requested_qty)
              LEFT JOIN items      itm ON itm.sku            = i.sku
              LEFT JOIN warehouses w   ON w.warehouse_code   = i.warehouse_code
              LEFT JOIN bins       b   ON b.bin_code         = i.bin_code
                                       AND b.warehouse_id    = w.warehouse_id
              LEFT JOIN inventory  inv ON inv.item_id        = itm.item_id
                                       AND inv.bin_id        = b.bin_id
                                       AND inv.warehouse_id  = w.warehouse_id
             GROUP BY i.idx, i.sku, i.warehouse_code, i.bin_code, i.requested_qty,
                      itm.item_id, itm.is_active, w.warehouse_id, b.bin_id
             ORDER BY i.idx
            """
        ),
        {
            "idxs":      list(range(len(body.lines))),
            "skus":      [ln.sku for ln in body.lines],
            "wh_codes":  [ln.warehouse_id for ln in body.lines],
            "bin_codes": [ln.bin_id for ln in body.lines],
            "qtys":      [ln.quantity for ln in body.lines],
        },
    ).fetchall()

    conflicts = []
    for row in rows:
        reason, available_qty = _classify_line(row, token_warehouse_ids)
        if reason is None:
            continue
        entry = {
            "line_index":    row.idx,
            "sku":           row.sku,
            "warehouse_id":  row.warehouse_code,
            "bin_id":        row.bin_code,
            "requested_qty": int(row.requested_qty),
            "reason":        reason,
        }
        if available_qty is not None:
            entry["available_qty"] = available_qty
        conflicts.append(entry)

    if not conflicts:
        return _draft_response({"valid": True}, 200)

    return _draft_response({"valid": False, "conflicts": conflicts}, 409)
