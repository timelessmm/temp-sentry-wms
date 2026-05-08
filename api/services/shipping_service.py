"""
Shipping service: records a ship event against an already-locked sales order.

Extracted from api/routes/shipping.py so the cookie-auth /api/shipping/fulfill
route and the bearer-token /api/v1/dockd/* surface share one transaction body
(fulfillment insert + line writes + SO update + audit + outbox emit).
"""

import uuid
from datetime import timezone

from sqlalchemy import text

from services.audit_service import write_audit_log
from services.events_service import emit_event, get_user_external_id

from constants import (
    SO_SHIPPED,
    ACTION_SHIP,
    TASK_PICKED,
    TASK_SHORT,
)


def require_packing_before_shipping(db) -> bool:
    """True when app_settings.require_packing_before_shipping is set to
    something OTHER than the literal string 'false'. The setting defaults
    on (returns True when the row is absent) so a fresh install gates
    shipping behind packing rather than letting a misconfigured deploy
    skip the verify step. Both the cookie-auth /api/shipping/fulfill
    and the dockd /api/v1/dockd/orders/.../ship surfaces consult this
    helper, so the gate is consistent across surfaces."""
    row = db.execute(
        text("SELECT value FROM app_settings WHERE key = 'require_packing_before_shipping'")
    ).fetchone()
    return not row or row.value != "false"


def record_ship(
    db,
    *,
    so_id,
    so_number,
    so_external_id,
    warehouse_id,
    tracking_number,
    carrier,
    ship_method,
    username,
    source_txn_id,
):
    """Record a ship event on an already-locked sales order.

    Caller MUST have:
      - SELECTed the sales_orders row FOR UPDATE
      - Validated warehouse scope
      - Validated status is shippable (PICKED or PACKED, depending on the
        require_packing_before_shipping app setting)

    Caller is responsible for the transaction commit. This function does not
    commit; it does emit one ship.confirmed/1 event onto the outbox.

    Returns dict with fulfillment_id, shipped_at, lines_shipped, total_quantity.
    """
    # 1. Create item_fulfillments record
    result = db.execute(
        text(
            """
            INSERT INTO item_fulfillments (so_id, warehouse_id, tracking_number, carrier, ship_method, shipped_by, status, external_id)
            VALUES (:so_id, :wh, :tracking, :carrier, :ship_method, :shipped_by, :shipped_status, :ext_id)
            RETURNING fulfillment_id, shipped_at
            """
        ),
        {
            "so_id": so_id,
            "wh": warehouse_id,
            "tracking": tracking_number,
            "carrier": carrier,
            "ship_method": ship_method,
            "shipped_by": username,
            "shipped_status": SO_SHIPPED,
            "ext_id": str(uuid.uuid4()),
        },
    )
    fulfillment_row = result.fetchone()
    fulfillment_id = fulfillment_row.fulfillment_id
    shipped_at = fulfillment_row.shipped_at

    # 2. Create fulfillment lines for each SO line with quantity_picked > 0
    so_lines = db.execute(
        text(
            """
            SELECT sol.so_line_id, sol.item_id, sol.quantity_picked
            FROM sales_order_lines sol
            WHERE sol.so_id = :so_id AND sol.quantity_picked > 0
            """
        ),
        {"so_id": so_id},
    ).fetchall()

    lines_shipped = 0
    total_quantity = 0

    for line in so_lines:
        # Find bin_id from pick_tasks
        pick_task = db.execute(
            text(
                """
                SELECT bin_id FROM pick_tasks
                WHERE so_id = :so_id AND item_id = :item_id AND status IN (:task_picked, :task_short)
                ORDER BY pick_task_id ASC
                LIMIT 1
                """
            ),
            {"so_id": so_id, "item_id": line.item_id, "task_picked": TASK_PICKED, "task_short": TASK_SHORT},
        ).fetchone()

        bin_id = pick_task.bin_id if pick_task else 1  # fallback shouldn't happen

        db.execute(
            text(
                """
                INSERT INTO item_fulfillment_lines (fulfillment_id, so_line_id, item_id, quantity_shipped, bin_id)
                VALUES (:fid, :sol_id, :item_id, :qty, :bin_id)
                """
            ),
            {
                "fid": fulfillment_id,
                "sol_id": line.so_line_id,
                "item_id": line.item_id,
                "qty": line.quantity_picked,
                "bin_id": bin_id,
            },
        )

        # 3. Update SO line
        db.execute(
            text(
                "UPDATE sales_order_lines SET quantity_shipped = quantity_picked, status = :status WHERE so_line_id = :sol_id"
            ),
            {"sol_id": line.so_line_id, "status": SO_SHIPPED},
        )

        lines_shipped += 1
        total_quantity += line.quantity_picked

    # 4. Update SO status with carrier and tracking
    db.execute(
        text(
            """
            UPDATE sales_orders
            SET status = :shipped_status, shipped_at = NOW(), carrier = :carrier, tracking_number = :tracking
            WHERE so_id = :so_id
            """
        ),
        {"so_id": so_id, "carrier": carrier, "tracking": tracking_number, "shipped_status": SO_SHIPPED},
    )

    # 5. Audit log
    write_audit_log(
        db,
        action_type=ACTION_SHIP,
        entity_type="SO",
        entity_id=so_id,
        user_id=username,
        warehouse_id=warehouse_id,
        details={
            "so_number": so_number,
            "tracking_number": tracking_number,
            "carrier": carrier,
            "fulfillment_id": fulfillment_id,
        },
    )

    # 6. v1.5.0 #118: emit ship.confirmed on the integration_events
    # outbox. tracking_numbers[] is array-shaped (Sentry creates one
    # fulfillment per SO today, so exactly one entry). Sentry's internal
    # column is ship_method; the wire contract renames it to
    # service_level per plan 1.7.1. packages[] mirrors the single
    # synthesised package from pack.confirmed.
    pack_lines = db.execute(
        text(
            """
            SELECT i.external_id AS item_external_id, sol.quantity_packed
              FROM sales_order_lines sol
              JOIN items i ON i.item_id = sol.item_id
             WHERE sol.so_id = :sid
             ORDER BY sol.line_number
            """
        ),
        {"sid": so_id},
    ).fetchall()
    stats = db.execute(
        text(
            """
            SELECT COALESCE(SUM(i.weight_lbs * sol.quantity_picked), 0) AS total_weight
              FROM sales_order_lines sol
              JOIN items i ON i.item_id = sol.item_id
             WHERE sol.so_id = :sid
            """
        ),
        {"sid": so_id},
    ).fetchone()
    so_external_id_str = str(so_external_id)
    emit_event(
        db,
        event_type="ship.confirmed",
        event_version=1,
        aggregate_type="sales_order",
        aggregate_id=so_id,
        aggregate_external_id=so_external_id,
        warehouse_id=warehouse_id,
        source_txn_id=source_txn_id,
        payload={
            "sales_order_external_id": so_external_id_str,
            "tracking_numbers": [tracking_number],
            "carrier": carrier,
            "service_level": ship_method,
            "packages": [
                {
                    "package_external_id": f"{so_external_id_str}-pkg-1",
                    "weight_lb": float(stats.total_weight) if stats.total_weight is not None else None,
                    "dimensions_in": None,
                    "lines": [
                        {
                            "item_external_id": str(line.item_external_id),
                            "quantity_packed": line.quantity_packed,
                        }
                        for line in pack_lines
                    ],
                },
            ],
            "completed_by_user_external_id": get_user_external_id(db, username),
            "completed_at": shipped_at.astimezone(timezone.utc).isoformat().replace("+00:00", "Z"),
        },
    )

    return {
        "fulfillment_id": fulfillment_id,
        "shipped_at": shipped_at,
        "lines_shipped": lines_shipped,
        "total_quantity": total_quantity,
    }
