"""Productivity dashboard API (v1.8.0 #297).

Three endpoints:

- GET  /api/v1/dashboard/productivity   per-user metrics for a date range
- GET  /api/v1/dashboard/preferences    user's chart_order + defaults
- PUT  /api/v1/dashboard/preferences    upsert preferences row

Auth: cookie + ADMIN role for productivity (operators with admin
visibility); preferences are per-user with the user_id derived
from g.current_user (never from the request body).
"""

from datetime import date, timedelta
from typing import List, Optional

from flask import Blueprint, g, jsonify, request
from pydantic import BaseModel, ConfigDict, Field, ValidationError, field_validator
from sqlalchemy import text

from middleware.auth_middleware import require_auth, require_role
from middleware.db import with_db
from services.productivity_service import (
    DASHBOARD_EVENTS,
    get_productivity,
)


dashboard_bp = Blueprint("dashboard", __name__)


_VALID_CHART_ORDER_KEYS = {slug for (slug, _, _) in DASHBOARD_EVENTS}
_VALID_DEFAULT_RANGES = {
    "today", "yesterday", "last_7d", "last_30d", "custom",
}
_VALID_DEFAULT_VIEWS = {"charts", "table"}

_MAX_RANGE_DAYS = 90


# ============================================================
# Productivity
# ============================================================


class _ProductivityQuery(BaseModel):
    model_config = ConfigDict(extra="forbid")
    start: date
    end: date
    warehouse_id: int = Field(..., gt=0)

    @field_validator("end")
    @classmethod
    def _end_not_before_start(cls, v, info):
        start = info.data.get("start")
        if start is not None and v < start:
            raise ValueError("end must be on or after start")
        return v


@dashboard_bp.route("/productivity", methods=["GET"])
@require_auth
@require_role("ADMIN")
@with_db
def productivity():
    try:
        params = _ProductivityQuery.model_validate({
            "start": request.args.get("start"),
            "end": request.args.get("end"),
            "warehouse_id": request.args.get("warehouse_id", type=int),
        })
    except ValidationError as exc:
        return jsonify({
            "error": "validation_error",
            "details": exc.errors(include_url=False, include_context=False),
        }), 422

    span_days = (params.end - params.start).days + 1
    if span_days > _MAX_RANGE_DAYS:
        return jsonify({
            "error": "range_too_large",
            "max_range_days": _MAX_RANGE_DAYS,
            "requested_days": span_days,
        }), 422

    # The endpoint accepts inclusive [start, end] dates; the SQL uses
    # half-open [start, end+1day) so the index range scan is clean.
    start_dt = params.start
    end_dt_exclusive = params.end + timedelta(days=1)
    payload = get_productivity(
        g.db, params.warehouse_id, start_dt, end_dt_exclusive,
    )
    # Fix up the response range to mirror what the operator asked for
    # (the cache key uses the SQL-level half-open form).
    payload["range"] = {
        "start": params.start.isoformat(),
        "end": params.end.isoformat(),
    }
    return jsonify(payload)


# ============================================================
# Preferences
# ============================================================


class _PreferencesBody(BaseModel):
    model_config = ConfigDict(extra="forbid")
    chart_order: Optional[List[str]] = None
    default_range: Optional[str] = None
    default_view: Optional[str] = None

    @field_validator("chart_order")
    @classmethod
    def _check_chart_order(cls, v):
        if v is None:
            return v
        unknown = [k for k in v if k not in _VALID_CHART_ORDER_KEYS]
        if unknown:
            raise ValueError(
                f"chart_order has unknown keys: {unknown!r}. "
                f"Valid: {sorted(_VALID_CHART_ORDER_KEYS)}"
            )
        if len(set(v)) != len(v):
            raise ValueError("chart_order must not contain duplicates")
        return v

    @field_validator("default_range")
    @classmethod
    def _check_default_range(cls, v):
        if v is not None and v not in _VALID_DEFAULT_RANGES:
            raise ValueError(
                f"default_range must be one of {sorted(_VALID_DEFAULT_RANGES)}"
            )
        return v

    @field_validator("default_view")
    @classmethod
    def _check_default_view(cls, v):
        if v is not None and v not in _VALID_DEFAULT_VIEWS:
            raise ValueError(
                f"default_view must be one of {sorted(_VALID_DEFAULT_VIEWS)}"
            )
        return v


def _resolve_user_id():
    """user_id derived from g.current_user (CSRF / IDOR protection per
    plan section 5.2). Returns None when unauthenticated."""
    user = getattr(g, "current_user", None) or {}
    return user.get("user_id")


def _row_to_dict(row) -> dict:
    return {
        "chart_order": (
            list(row.chart_order) if row.chart_order is not None
            else [slug for (slug, _, _) in DASHBOARD_EVENTS]
        ),
        "default_range": row.default_range,
        "default_view": row.default_view,
        "updated_at": row.updated_at.isoformat() if row.updated_at else None,
    }


@dashboard_bp.route("/preferences", methods=["GET"])
@require_auth
@with_db
def get_preferences():
    user_id = _resolve_user_id()
    if user_id is None:
        return jsonify({"error": "unauthenticated"}), 401
    row = g.db.execute(
        text(
            "SELECT chart_order, default_range, default_view, updated_at "
            "  FROM user_dashboard_preferences WHERE user_id = :uid"
        ),
        {"uid": user_id},
    ).fetchone()
    if row is None:
        return jsonify({
            "chart_order": [slug for (slug, _, _) in DASHBOARD_EVENTS],
            "default_range": "today",
            "default_view": "charts",
            "updated_at": None,
        })
    return jsonify(_row_to_dict(row))


@dashboard_bp.route("/preferences", methods=["PUT"])
@require_auth
@with_db
def put_preferences():
    user_id = _resolve_user_id()
    if user_id is None:
        return jsonify({"error": "unauthenticated"}), 401
    try:
        body = _PreferencesBody.model_validate(request.get_json() or {})
    except ValidationError as exc:
        return jsonify({
            "error": "validation_error",
            "details": exc.errors(include_url=False, include_context=False),
        }), 422

    # Build the UPSERT dynamically so unset fields keep their existing
    # value (rather than reverting to defaults).
    fields = {}
    if body.chart_order is not None:
        fields["chart_order"] = body.chart_order
    if body.default_range is not None:
        fields["default_range"] = body.default_range
    if body.default_view is not None:
        fields["default_view"] = body.default_view
    if not fields:
        return jsonify({"error": "no_fields_to_update"}), 422

    # Build the INSERT column list (always includes user_id) + the
    # ON CONFLICT updates for the fields the body actually set. Empty
    # column case (only user_id) is handled by the no_fields_to_update
    # guard above.
    insert_cols = ["user_id"] + list(fields.keys())
    insert_placeholders = ["(:uid)"] + [f"(:{c})" for c in fields.keys()]

    # Use psycopg2.extras.Json wrapping for chart_order JSONB binding.
    from psycopg2.extras import Json
    params = {"uid": user_id}
    for col, val in fields.items():
        params[col] = Json(val) if col == "chart_order" else val

    # Construct the SQL: INSERT ... ON CONFLICT (user_id) DO UPDATE SET ...
    set_clause = ", ".join(
        f"{c} = EXCLUDED.{c}" for c in fields.keys()
    ) + ", updated_at = NOW()"
    cols_sql = ", ".join(insert_cols)
    vals_sql = ", ".join(f":{c}" if c != "user_id" else ":uid"
                         for c in insert_cols)
    g.db.execute(
        text(
            f"INSERT INTO user_dashboard_preferences ({cols_sql}) "
            f"VALUES ({vals_sql}) "
            f"ON CONFLICT (user_id) DO UPDATE SET {set_clause}"
        ),
        params,
    )
    g.db.commit()

    row = g.db.execute(
        text(
            "SELECT chart_order, default_range, default_view, updated_at "
            "  FROM user_dashboard_preferences WHERE user_id = :uid"
        ),
        {"uid": user_id},
    ).fetchone()
    return jsonify(_row_to_dict(row))
