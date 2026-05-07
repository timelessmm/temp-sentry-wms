"""v1.8.0 (#296): pending_to_approvals on /admin/dashboard.

Covers:
- pending_to_approvals counts only PENDING transfer_order_approvals.
- APPROVED + REJECTED rows do not contribute.
- warehouse_id query param scopes to TOs touching that warehouse
  (source OR destination match).
- Without warehouse_id, the count is global.
"""

import os
import sys
import uuid

import pytest

os.environ.setdefault("DATABASE_URL", "postgresql://sentry:sentry@localhost:5432/sentry")
os.environ.setdefault("JWT_SECRET", "NEVER_USE_THIS_IN_PRODUCTION_32!")
os.environ.setdefault("SENTRY_ENCRYPTION_KEY", "t5hPIEVn_O41qfiMqAiPEnwzQh68o3Es46YfSOBvEK8=")

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

import db_test_context


def _seed_to(source_wh, dest_wh):
    conn = db_test_context.get_raw_connection()
    cur = conn.cursor()
    try:
        cur.execute(
            "INSERT INTO transfer_orders "
            "(to_number, source_warehouse_id, destination_warehouse_id, "
            " status, created_by, external_id) "
            "VALUES (%s, %s, %s, 'PARTIALLY_PICKED', 't296', %s) "
            "RETURNING to_id",
            (
                f"TO-T296-{uuid.uuid4().hex[:8]}",
                source_wh, dest_wh, str(uuid.uuid4()),
            ),
        )
        return cur.fetchone()[0]
    finally:
        cur.close()


def _seed_approval(to_id, status="PENDING"):
    conn = db_test_context.get_raw_connection()
    cur = conn.cursor()
    try:
        if status == "APPROVED":
            cur.execute(
                "INSERT INTO transfer_order_approvals "
                "(to_id, submitted_by, lines_snapshot, external_id, "
                " status, approved_by, approved_at) "
                "VALUES (%s, 'p1', '{}'::jsonb, %s, 'APPROVED', "
                "        'admin', NOW()) RETURNING to_approval_id",
                (to_id, str(uuid.uuid4())),
            )
        elif status == "REJECTED":
            cur.execute(
                "INSERT INTO transfer_order_approvals "
                "(to_id, submitted_by, lines_snapshot, external_id, "
                " status, rejected_at) "
                "VALUES (%s, 'p1', '{}'::jsonb, %s, 'REJECTED', NOW()) "
                "RETURNING to_approval_id",
                (to_id, str(uuid.uuid4())),
            )
        else:
            cur.execute(
                "INSERT INTO transfer_order_approvals "
                "(to_id, submitted_by, lines_snapshot, external_id) "
                "VALUES (%s, 'p1', '{}'::jsonb, %s) "
                "RETURNING to_approval_id",
                (to_id, str(uuid.uuid4())),
            )
        return cur.fetchone()[0]
    finally:
        cur.close()


class TestPendingToApprovals:
    def test_counts_only_pending(self, client, auth_headers):
        to_id = _seed_to(1, 2)
        _seed_approval(to_id, status="PENDING")
        _seed_approval(to_id, status="APPROVED")
        _seed_approval(to_id, status="REJECTED")
        resp = client.get("/api/admin/dashboard?warehouse_id=1", headers=auth_headers)
        assert resp.status_code == 200
        body = resp.get_json()
        assert body["pending_to_approvals"] == 1

    def test_warehouse_scope_matches_source_or_destination(
        self, client, auth_headers,
    ):
        # TO from 1 -> 2 has 1 pending; TO from 2 -> 1 has 1 pending;
        # both touch warehouse 1 (one as source, one as destination)
        # so the warehouse_id=1 count is 2.
        to_a = _seed_to(1, 2)
        to_b = _seed_to(2, 1)
        _seed_approval(to_a, status="PENDING")
        _seed_approval(to_b, status="PENDING")
        resp = client.get("/api/admin/dashboard?warehouse_id=1", headers=auth_headers)
        assert resp.status_code == 200
        assert resp.get_json()["pending_to_approvals"] == 2

    def test_warehouse_scope_excludes_unrelated(
        self, client, auth_headers,
    ):
        # Mig 049 only seeds warehouse_ids 1 + 2 in the schema by
        # default. Insert a third warehouse to test scope exclusion.
        conn = db_test_context.get_raw_connection()
        cur = conn.cursor()
        try:
            cur.execute(
                "INSERT INTO warehouses (warehouse_code, warehouse_name) "
                "VALUES (%s, 't296-third') RETURNING warehouse_id",
                (f"T296-{uuid.uuid4().hex[:6]}",),
            )
            wh3 = cur.fetchone()[0]
        finally:
            cur.close()
        # TO between 1 and 2 should NOT count toward warehouse 3.
        to_a = _seed_to(1, 2)
        _seed_approval(to_a, status="PENDING")
        resp = client.get(
            f"/api/admin/dashboard?warehouse_id={wh3}",
            headers=auth_headers,
        )
        assert resp.status_code == 200
        assert resp.get_json()["pending_to_approvals"] == 0

    def test_global_count_without_warehouse_filter(
        self, client, auth_headers,
    ):
        # No warehouse_id query param -> global PENDING count
        to_a = _seed_to(1, 2)
        to_b = _seed_to(2, 1)
        _seed_approval(to_a, status="PENDING")
        _seed_approval(to_b, status="PENDING")
        _seed_approval(to_a, status="APPROVED")
        resp = client.get("/api/admin/dashboard", headers=auth_headers)
        assert resp.status_code == 200
        assert resp.get_json()["pending_to_approvals"] == 2
