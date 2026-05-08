"""v1.9.0 dockd shipping surface.

Endpoints (this commit):

    GET /api/v1/dockd/orders/<so_number>   -- load-on-scan order detail

The two POST routes (ship + void-ship) land in subsequent commits.

Per-request shape:
- @require_wms_token: validates X-WMS-Token, refuses cross-direction
  bridging, refuses tokens without dockd.dispatch in endpoints. The
  decorator's V190 dispatcher branch is what gates this surface.
- @limiter.limit("60 per minute") for GET; the cap exists to refuse a
  station that's misconfigured into a tight scan loop, not to
  constrain legitimate scan-driven UX.
- @with_db opens the request-scoped SQLAlchemy session.
- Every response (success and failure) carries
  X-Sentry-Canonical-Model: DRAFT-v1 so consumers can detect schema-
  stability stage on each response, including 4xx.
- Standard error body shape:
      {"error_kind": str, "message": str, "details": {}}

Path parameter validation:
- so_number is matched against ^[A-Za-z0-9_\\-#.]+$ length 1..128 before
  any DB query so a malformed value never reaches the parameterized
  binding. The regex is defense-in-depth on top of SQLAlchemy's bind
  parameter handling.
"""

import re

from flask import Blueprint, g, jsonify, make_response
from sqlalchemy import text

from constants import SO_PICKED, SO_PACKED, SO_SHIPPED
from middleware.auth_middleware import require_wms_token
from middleware.db import with_db
from services.rate_limit import limiter
from services.shipping_service import require_packing_before_shipping


dockd_bp = Blueprint("dockd", __name__)


# Path-parameter regex. Mirrors the existing inbound so_number Pydantic
# regex; the dockd surface accepts the same shape so a Fabric-pushed
# order number always loads on a dockd scan.
_SO_NUMBER_RE = re.compile(r"^[A-Za-z0-9_\-#.]+$")
_SO_NUMBER_MAX_LEN = 128


def _draft_response(body, status_code=200, extra_headers=None):
    """Build a Flask response carrying the dockd canonical-model header
    on every response (success or failure)."""
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


def _validate_so_number(so_number):
    """Returns None if valid, otherwise an error response."""
    if not so_number or len(so_number) > _SO_NUMBER_MAX_LEN:
        return _err("invalid_so_number", "so_number length out of range", 422)
    if not _SO_NUMBER_RE.match(so_number):
        return _err("invalid_so_number", "so_number contains disallowed characters", 422)
    return None


@dockd_bp.route("/orders/<so_number>", methods=["GET"])
@require_wms_token
@limiter.limit("60 per minute")
@with_db
def get_order(so_number):
    """Load-on-scan dockd read.

    Returns the SO + structured shipping address + items + ship-state
    fields so dockd's UI can render either the "ready to ship" or the
    "already shipped, want to void?" branch off a single call.

    404 not_found is returned for both genuinely-unknown so_numbers AND
    for orders outside the token's warehouse scope. Conflating the two
    prevents an enumeration oracle (the token cannot tell a missing
    order from one in a sibling warehouse).
    """
    err = _validate_so_number(so_number)
    if err is not None:
        return err

    token_warehouse_ids = list(g.current_token.get("warehouse_ids") or [])
    if not token_warehouse_ids:
        return _err("not_found", "order not found", 404)

    so = g.db.execute(
        text(
            """
            SELECT so_id, so_number, external_id, status, warehouse_id,
                   customer_name, customer_phone, ship_method,
                   shipping_address_name, shipping_address_line1,
                   shipping_address_line2, shipping_address_city,
                   shipping_address_state, shipping_address_postal_code,
                   shipping_address_country, shipping_address_phone,
                   order_total, customer_shipping_paid, created_by,
                   order_date, created_at, shipped_at, carrier,
                   tracking_number
              FROM sales_orders
             WHERE so_number = :so_number
               AND warehouse_id = ANY(:wh_ids)
             LIMIT 1
            """
        ),
        {"so_number": so_number, "wh_ids": token_warehouse_ids},
    ).fetchone()

    if not so:
        return _err("not_found", "order not found", 404)

    items = g.db.execute(
        text(
            """
            SELECT i.external_id AS item_external_id, i.sku, i.item_name,
                   i.upc, sol.quantity_picked AS qty
              FROM sales_order_lines sol
              JOIN items i ON i.item_id = sol.item_id
             WHERE sol.so_id = :so_id
             ORDER BY sol.line_number
            """
        ),
        {"so_id": so.so_id},
    ).fetchall()

    packing_required = require_packing_before_shipping(g.db)
    shippable_from_statuses = (
        [SO_PACKED] if packing_required else [SO_PICKED, SO_PACKED]
    )
    shippable = so.status in shippable_from_statuses

    shipped_by = None
    if so.status == SO_SHIPPED:
        ff = g.db.execute(
            text(
                """
                SELECT shipped_by
                  FROM item_fulfillments
                 WHERE so_id = :so_id AND status = 'SHIPPED'
                 ORDER BY shipped_at DESC NULLS LAST
                 LIMIT 1
                """
            ),
            {"so_id": so.so_id},
        ).fetchone()
        if ff:
            shipped_by = ff.shipped_by

    body = {
        "so_number": so.so_number,
        "external_id": str(so.external_id),
        "status": so.status,
        "warehouse_id": so.warehouse_id,
        "customer_name": so.customer_name,
        "customer_phone": so.customer_phone,
        "shipping_address": {
            "name": so.shipping_address_name,
            "line1": so.shipping_address_line1,
            "line2": so.shipping_address_line2,
            "city": so.shipping_address_city,
            "state": so.shipping_address_state,
            "postal_code": so.shipping_address_postal_code,
            "country": so.shipping_address_country,
            "phone": so.shipping_address_phone,
        },
        "ship_method": so.ship_method,
        "items": [
            {
                "external_id": str(it.item_external_id),
                "sku": it.sku,
                "display_name": it.item_name,
                "upc": it.upc,
                "qty": it.qty,
            }
            for it in items
        ],
        "order_total": float(so.order_total) if so.order_total is not None else None,
        "customer_shipping_paid": (
            float(so.customer_shipping_paid)
            if so.customer_shipping_paid is not None
            else None
        ),
        "marketplace": so.created_by,
        "order_date": so.order_date.isoformat() if so.order_date else None,
        "ff_created_at": so.created_at.isoformat() if so.created_at else None,
        "shippable": shippable,
        "shippable_from_statuses": shippable_from_statuses,
        "shipped_by": shipped_by,
        "tracking_number": so.tracking_number,
        "carrier": so.carrier,
        "shipped_at": so.shipped_at.isoformat() if so.shipped_at else None,
        # station_label is a UX aspiration documented in the dockd plan;
        # the queryable path (token-id JOIN on item_fulfillments) is not
        # yet wired. v1.9 returns null; the dockd UI degrades the
        # "Shipped by Mike at Pack Station 3" line to "Shipped by Mike"
        # until a follow-up commit lands the column.
        "station_label": None,
    }
    return _draft_response(body, 200)
