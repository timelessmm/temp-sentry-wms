"""Admin CRUD + pre-approval lifecycle for transfer orders (v1.8.0 #290).

Cookie-auth + ADMIN role. Picking is on the picker-facing
/api/picking/transfer-orders/<to_id>/* surface (Pass 4.3); admin
approval is the dedicated /approvals surface (Pass 4.4).

State-machine + audit constants live in
services.transfer_order_service + constants.py respectively. This
module orchestrates the admin lifecycle without re-deriving state.
"""

import math
import uuid
from typing import Optional

from flask import g, jsonify, request
from sqlalchemy import text

from constants import (
    ACTION_TO_CANCELLED,
    ACTION_TO_DELETED,
    ACTION_TO_LINE_SHORT_CLOSED,
)
from middleware.auth_middleware import require_auth, require_role
from middleware.db import with_db
from routes.admin import admin_bp
from services.audit_service import write_audit_log
from services.transfer_order_service import (
    TO_LINE_PENDING,
    TO_LINE_PARTIALLY_PICKED,
    TO_LINE_PICKED,
    TO_LINE_SHORT_CLOSED,
    TO_STATUS_CANCELLED,
    TO_STATUS_CLOSED,
    TO_STATUS_OPEN,
    TO_STATUS_PARTIALLY_PICKED,
    validate_header_transition,
    validate_line_transition,
)


# ----------------------------------------------------------------------
# Serialisation helpers
# ----------------------------------------------------------------------


def _serialise_to_header(row) -> dict:
    return {
        "to_id": row.to_id,
        "to_number": row.to_number,
        "source_warehouse_id": row.source_warehouse_id,
        "destination_warehouse_id": row.destination_warehouse_id,
        "status": row.status,
        "created_by": row.created_by,
        "notes": row.notes,
        "external_id": str(row.external_id),
        "created_at": row.created_at.isoformat() if row.created_at else None,
        "updated_at": row.updated_at.isoformat() if row.updated_at else None,
    }


def _serialise_to_line(row) -> dict:
    return {
        "to_line_id": row.to_line_id,
        "to_id": row.to_id,
        "item_id": row.item_id,
        "sku": getattr(row, "sku", None),
        "item_name": getattr(row, "item_name", None),
        "line_number": row.line_number,
        "requested_qty": row.requested_qty,
        "committed_qty": row.committed_qty,
        "picked_qty": row.picked_qty,
        "approved_qty": row.approved_qty,
        "status": row.status,
    }


def _serialise_to_approval(row) -> dict:
    return {
        "to_approval_id": row.to_approval_id,
        "to_id": row.to_id,
        "submitted_by": row.submitted_by,
        "submitted_at": row.submitted_at.isoformat() if row.submitted_at else None,
        "approved_by": row.approved_by,
        "approved_at": row.approved_at.isoformat() if row.approved_at else None,
        "rejected_at": row.rejected_at.isoformat() if row.rejected_at else None,
        "rejection_reason": row.rejection_reason,
        "status": row.status,
        "external_id": str(row.external_id),
    }


# ----------------------------------------------------------------------
# List + detail
# ----------------------------------------------------------------------


@admin_bp.route("/transfer-orders", methods=["GET"])
@require_auth
@require_role("ADMIN")
@with_db
def list_transfer_orders():
    page = request.args.get("page", 1, type=int)
    per_page = min(request.args.get("per_page", 50, type=int), 1000)

    status_filter = request.args.get("status")
    source_warehouse = request.args.get("source_warehouse_id", type=int)
    destination_warehouse = request.args.get("destination_warehouse_id", type=int)

    where_clauses, params = [], {}
    if status_filter:
        where_clauses.append("status = :status")
        params["status"] = status_filter
    if source_warehouse:
        where_clauses.append("source_warehouse_id = :swid")
        params["swid"] = source_warehouse
    if destination_warehouse:
        where_clauses.append("destination_warehouse_id = :dwid")
        params["dwid"] = destination_warehouse

    where_sql = ("WHERE " + " AND ".join(where_clauses)) if where_clauses else ""

    total = g.db.execute(
        text(f"SELECT COUNT(*) FROM transfer_orders {where_sql}"),
        params,
    ).scalar()
    pages = max(1, math.ceil(total / per_page)) if total else 1

    params["limit"] = per_page
    params["offset"] = (page - 1) * per_page
    rows = g.db.execute(
        text(
            f"""
            SELECT to_id, to_number, source_warehouse_id, destination_warehouse_id,
                   status, created_by, notes, external_id, created_at, updated_at
              FROM transfer_orders {where_sql}
             ORDER BY to_id DESC
             LIMIT :limit OFFSET :offset
            """
        ),
        params,
    ).fetchall()

    return jsonify({
        "transfer_orders": [_serialise_to_header(r) for r in rows],
        "total": total,
        "page": page,
        "per_page": per_page,
        "pages": pages,
    })


@admin_bp.route("/transfer-orders/<int:to_id>", methods=["GET"])
@require_auth
@require_role("ADMIN")
@with_db
def get_transfer_order(to_id):
    header = g.db.execute(
        text(
            """
            SELECT to_id, to_number, source_warehouse_id, destination_warehouse_id,
                   status, created_by, notes, external_id, created_at, updated_at
              FROM transfer_orders WHERE to_id = :tid
            """
        ),
        {"tid": to_id},
    ).fetchone()
    if not header:
        return jsonify({"error": "Transfer order not found"}), 404

    lines = g.db.execute(
        text(
            """
            SELECT tol.to_line_id, tol.to_id, tol.item_id,
                   i.sku, i.item_name,
                   tol.line_number, tol.requested_qty, tol.committed_qty,
                   tol.picked_qty, tol.approved_qty, tol.status
              FROM transfer_order_lines tol
              JOIN items i ON i.item_id = tol.item_id
             WHERE tol.to_id = :tid
             ORDER BY tol.line_number
            """
        ),
        {"tid": to_id},
    ).fetchall()

    approvals = g.db.execute(
        text(
            """
            SELECT to_approval_id, to_id, submitted_by, submitted_at,
                   approved_by, approved_at, rejected_at, rejection_reason,
                   status, external_id
              FROM transfer_order_approvals
             WHERE to_id = :tid
             ORDER BY submitted_at DESC
            """
        ),
        {"tid": to_id},
    ).fetchall()

    return jsonify({
        "transfer_order": _serialise_to_header(header),
        "lines": [_serialise_to_line(r) for r in lines],
        "approvals": [_serialise_to_approval(r) for r in approvals],
    })


# ----------------------------------------------------------------------
# Pre-approval lifecycle: cancel + delete
# ----------------------------------------------------------------------


def _release_reservations(db, to_id: int, source_warehouse_id: int) -> None:
    """For every line on the TO with committed_qty > 0, decrement
    inventory.quantity_allocated at the source warehouse so the
    reservation does not survive the cancellation. Items are processed
    in deterministic order by item_id to match the importer + picker
    locking pattern (plan section 4.4)."""
    lines = db.execute(
        text(
            """
            SELECT to_line_id, item_id, committed_qty - approved_qty AS to_release
              FROM transfer_order_lines
             WHERE to_id = :tid AND committed_qty > approved_qty
             ORDER BY item_id ASC
            """
        ),
        {"tid": to_id},
    ).fetchall()
    for row in lines:
        if row.to_release <= 0:
            continue
        db.execute(
            text(
                """
                UPDATE inventory
                   SET quantity_allocated = quantity_allocated - :delta
                 WHERE item_id = :iid AND warehouse_id = :wid
                """
            ),
            {
                "delta": row.to_release,
                "iid": row.item_id,
                "wid": source_warehouse_id,
            },
        )


@admin_bp.route("/transfer-orders/<int:to_id>/cancel", methods=["POST"])
@require_auth
@require_role("ADMIN")
@with_db
def cancel_transfer_order(to_id):
    header = g.db.execute(
        text(
            "SELECT to_id, to_number, status, source_warehouse_id "
            "  FROM transfer_orders WHERE to_id = :tid FOR UPDATE"
        ),
        {"tid": to_id},
    ).fetchone()
    if not header:
        return jsonify({"error": "Transfer order not found"}), 404

    try:
        validate_header_transition(header.status, TO_STATUS_CANCELLED)
    except ValueError as exc:
        return jsonify({
            "error": "invalid_status_for_cancel",
            "current_status": header.status,
            "detail": str(exc),
        }), 409

    # Block cancel once any approval has flipped non-PENDING (an
    # APPROVED or REJECTED approval row already moved inventory or
    # produced an audit trail; cancelling beneath it loses state).
    non_pending_approvals = g.db.execute(
        text(
            "SELECT COUNT(*) FROM transfer_order_approvals "
            " WHERE to_id = :tid AND status <> 'PENDING'"
        ),
        {"tid": to_id},
    ).scalar()
    if non_pending_approvals > 0:
        return jsonify({
            "error": "to_already_partially_approved",
            "non_pending_approvals": non_pending_approvals,
        }), 409

    _release_reservations(g.db, to_id, header.source_warehouse_id)

    g.db.execute(
        text(
            "UPDATE transfer_orders SET status = :st, updated_at = NOW() "
            " WHERE to_id = :tid"
        ),
        {"st": TO_STATUS_CANCELLED, "tid": to_id},
    )

    write_audit_log(
        g.db,
        action_type=ACTION_TO_CANCELLED,
        entity_type="TO",
        entity_id=to_id,
        user_id=g.current_user["username"],
        warehouse_id=header.source_warehouse_id,
        details={
            "to_number": header.to_number,
            "previous_status": header.status,
        },
    )
    g.db.commit()
    return jsonify({"to_id": to_id, "status": TO_STATUS_CANCELLED})


@admin_bp.route("/transfer-orders/<int:to_id>", methods=["DELETE"])
@require_auth
@require_role("ADMIN")
@with_db
def delete_transfer_order(to_id):
    header = g.db.execute(
        text(
            "SELECT to_id, to_number, status, source_warehouse_id "
            "  FROM transfer_orders WHERE to_id = :tid FOR UPDATE"
        ),
        {"tid": to_id},
    ).fetchone()
    if not header:
        return jsonify({"error": "Transfer order not found"}), 404

    # Hard delete only allowed pre-approval and pre-pick. Once any line
    # has picked_qty > 0 OR any approval row exists, the audit trail
    # would lose state; require cancel instead.
    any_picks = g.db.execute(
        text(
            "SELECT COUNT(*) FROM transfer_order_lines "
            " WHERE to_id = :tid AND picked_qty > 0"
        ),
        {"tid": to_id},
    ).scalar()
    any_approvals = g.db.execute(
        text(
            "SELECT COUNT(*) FROM transfer_order_approvals "
            " WHERE to_id = :tid"
        ),
        {"tid": to_id},
    ).scalar()
    if any_picks > 0 or any_approvals > 0:
        return jsonify({
            "error": "to_not_deletable",
            "any_picks": any_picks,
            "any_approvals": any_approvals,
            "detail": (
                "Use cancel for TOs with picks or approvals; delete "
                "only applies to OPEN TOs with no downstream activity."
            ),
        }), 409

    _release_reservations(g.db, to_id, header.source_warehouse_id)

    write_audit_log(
        g.db,
        action_type=ACTION_TO_DELETED,
        entity_type="TO",
        entity_id=to_id,
        user_id=g.current_user["username"],
        warehouse_id=header.source_warehouse_id,
        details={
            "to_number": header.to_number,
            "previous_status": header.status,
        },
    )
    g.db.execute(
        text("DELETE FROM transfer_orders WHERE to_id = :tid"),
        {"tid": to_id},
    )
    g.db.commit()
    return ("", 204)


# ----------------------------------------------------------------------
# Line short-close
# ----------------------------------------------------------------------


@admin_bp.route(
    "/transfer-orders/<int:to_id>/lines/<int:line_id>/short-close",
    methods=["POST"],
)
@require_auth
@require_role("ADMIN")
@with_db
def short_close_to_line(to_id, line_id):
    line = g.db.execute(
        text(
            """
            SELECT tol.to_line_id, tol.to_id, tol.item_id, tol.line_number,
                   tol.requested_qty, tol.committed_qty, tol.picked_qty,
                   tol.approved_qty, tol.status,
                   o.source_warehouse_id, o.to_number
              FROM transfer_order_lines tol
              JOIN transfer_orders o ON o.to_id = tol.to_id
             WHERE tol.to_line_id = :lid AND tol.to_id = :tid
             FOR UPDATE OF tol
            """
        ),
        {"lid": line_id, "tid": to_id},
    ).fetchone()
    if not line:
        return jsonify({"error": "Transfer order line not found"}), 404

    try:
        validate_line_transition(line.status, TO_LINE_SHORT_CLOSED)
    except ValueError as exc:
        return jsonify({
            "error": "invalid_line_status_for_short_close",
            "current_status": line.status,
            "detail": str(exc),
        }), 409

    # Short-close only applies to the remaining (committed - approved)
    # quantity. The release re-credits inventory.quantity_allocated for
    # the still-reserved committed_qty - approved_qty, and the line
    # transitions to SHORT_CLOSED to lock the remaining out.
    remaining = line.committed_qty - line.approved_qty
    if remaining > 0:
        g.db.execute(
            text(
                """
                UPDATE inventory
                   SET quantity_allocated = quantity_allocated - :delta
                 WHERE item_id = :iid AND warehouse_id = :wid
                """
            ),
            {
                "delta": remaining,
                "iid": line.item_id,
                "wid": line.source_warehouse_id,
            },
        )

    g.db.execute(
        text(
            "UPDATE transfer_order_lines "
            "   SET status = :st "
            " WHERE to_line_id = :lid"
        ),
        {"st": TO_LINE_SHORT_CLOSED, "lid": line_id},
    )

    write_audit_log(
        g.db,
        action_type=ACTION_TO_LINE_SHORT_CLOSED,
        entity_type="TO_LINE",
        entity_id=line_id,
        user_id=g.current_user["username"],
        warehouse_id=line.source_warehouse_id,
        details={
            "to_id": to_id,
            "to_number": line.to_number,
            "line_number": line.line_number,
            "previous_status": line.status,
            "released_qty": remaining,
        },
    )
    g.db.commit()
    return jsonify({
        "to_line_id": line_id,
        "status": TO_LINE_SHORT_CLOSED,
        "released_qty": remaining,
    })
