# Transfer Orders

Operator playbook for warehouse-to-warehouse transfers (v1.8.0). A transfer order (TO) moves a set of items from one warehouse to another with admin-approval gating between the picker and the inventory move.

## Lifecycle at a glance

```
                    CSV import
                        |
                        v
                     OPEN ---------> CANCELLED  (admin pre-pick)
                        |                     |
                        | first pick          |
                        v                     |
              PARTIALLY_PICKED ---------> CANCELLED  (admin pre-approval)
                        |
                        | all lines fully picked or short-closed
                        v
              AWAITING_APPROVAL ---+
                        |         |
                        | admin   |
                        | approves|
                        | each    |
                        | batch   |
                        v         |
                    APPROVED -----+ (back to PARTIALLY_PICKED while
                        |           multi-batch picks continue)
                        | all approvals processed
                        v
                     CLOSED
```

Inventory moves only at admin **approve**. A **reject** leaves source stock at the source warehouse so the picker can re-pick or the operator can short-close the line.

## Roles

- **Admin** -- creates TOs (CSV import), starts picking (creates the pick batch + tasks), approves / rejects picker submissions, cancels / deletes TOs, short-closes lines.
- **Picker** -- works the pick tasks the admin starts; submits batches of picks for admin approval. Picker uses the existing mobile picking flow; the picker UI shows "TO {to_number}" instead of "X orders" when the active batch is a TO pick.

## Creating a TO via CSV import

`Admin Panel -> Warehouse -> Transfer Orders -> Import CSV`.

CSV shape (two columns; header row required):

```
sku,quantity
SKU-A,5
SKU-B,12
```

The Import modal asks for **source warehouse**, **destination warehouse**, and **optional notes** at the request level (not per row, by design -- one TO ships from one source to one destination). Source must differ from destination (422 enforced).

The handler:

1. Validates each row via `TransferOrderImportRow` (formula-prefix protection on every text field).
2. Resolves SKUs to `items.item_id`. Unknown SKU returns 422 with the row index.
3. Walks inventory rows for `(item, source_warehouse)` in `inventory_id ASC` (matches the picking + cancel lock ordering).
4. Computes `committed_qty = min(requested, available)` per row and distributes the commit across bins.
5. Creates the `transfer_orders` header + `transfer_order_lines` rows. Lines with `committed_qty = 0` land `SHORT_CLOSED` so they don't block closure.
6. Returns a **shortages payload** for any line where `committed_qty < requested_qty`.

The Shortage Modal that opens on response gives three actions:

- **Download Shortage CSV** -- export the (sku, requested, available, committed, shortfall) table for ops follow-up.
- **Cancel TO** -- DELETE the just-created TO (releases the partial reservation).
- **Create with Available** -- dismiss the modal; the TO is already created with the partial commitments.

## Picking a TO

Admin opens the TO detail and clicks **Start Picking**. The handler:

- Walks lines with `picked_qty < committed_qty`.
- For each line, finds inventory rows at the source warehouse and creates one `pick_tasks` row per `(line, bin)` with `to_id` + `to_line_id` set (no `so_id`).
- Anchors the tasks under a fresh `pick_batch` row.

The picker then opens the mobile app, sees the active batch (with the "ACTIVE TRANSFER" label and the TO number on the v1.8 APK; "ACTIVE BATCH" + "0 orders" on the v1.5.1 APK), and works the tasks via the existing PickWalk + scan flow. Each confirmed pick:

- Updates `transfer_order_lines.picked_qty` via a WHERE-clause guard (`picked_qty + delta <= committed_qty AND status IN (PENDING, PARTIALLY_PICKED)`); over-pick attempts return 409 from the server.
- Does **NOT** decrement source `quantity_on_hand` (TO inventory moves only at approval; the `quantity_allocated` reservation persists).
- Writes an `ACTION_TO_LINE_PICKED` audit row.

When all lines reach PICKED or SHORT_CLOSED, the header advances to AWAITING_APPROVAL.

## Submitting picks

Picker opens the TO and clicks **Submit** (mobile or admin UI). The handler:

- Finds lines where `picked_qty > approved_qty` (new picks since the last submit).
- Snapshots `(to_line_id, item_id, picked_in_snapshot)` into a fresh `transfer_order_approvals` row with status `PENDING`.
- Returns 422 `nothing_picked` when no lines qualify.

A TO can have multiple approval rows when picking spans several batches; each batch is approved or rejected independently.

## Admin approval

Admin opens the TO detail and clicks **Approve** on a pending submission row.

- **Self-approval gate**: by default `app_settings.transfer_order_block_self_approval = true`, so the picker who submitted cannot approve their own batch. Admin gets 403 `self_approval_blocked`. Set the setting to `false` if a single-admin warehouse needs the same person to submit + approve.
- For each line in the snapshot:
  - `transfer_order_lines.approved_qty += picked_in_snapshot`
  - Source: `inventory.quantity_allocated -= snapshot_qty`, `quantity_on_hand -= snapshot_qty` distributed across bins in `inventory_id ASC`.
  - Destination: `inventory.quantity_on_hand += snapshot_qty` at the destination warehouse's first **Staging** bin (INSERTs an inventory row when missing).
- The approval row flips to APPROVED with `approved_at` + `approved_by`.
- Closure check: when all lines are APPROVED with `approved_qty == picked_qty` (or SHORT_CLOSED) and no PENDING approvals remain, the header flips to CLOSED.
- A `transfer.completed/1` event lands in the `integration_events` outbox so external consumers can react.

**Required setup at the destination warehouse**: at least one bin with `bin_type='Staging'`. Approve returns 409 `no_destination_staging_bin` otherwise.

## Admin rejection

Admin clicks **Reject** with an optional reason (max 1000 chars). The approval row flips to REJECTED with `rejected_at` + `rejection_reason`. **No inventory movement**, **no event emission**. The picker can re-pick the affected lines (the source stock is still there) or the operator can short-close the line if the picks should not return.

## Cancel + delete

- **Cancel** -- valid pre-approval (statuses OPEN, PARTIALLY_PICKED). Releases the remaining `committed - approved` reservation on each line. Returns 409 `to_already_partially_approved` when a non-PENDING approval row exists (the multi-batch audit trail must not be nuked).
- **Delete** -- only valid for OPEN TOs with no picks and no approvals. Use Cancel for everything else.

## Short-close a line

`Admin Panel -> TO detail -> Short-Close` on the line row. Transitions the line to SHORT_CLOSED and releases `committed_qty - approved_qty` back to source `inventory.quantity_allocated`. Used when the picker can't fulfil the remaining commitment and the operator wants to lock the line out without cancelling the whole TO.

## Failure modes -- operator action

| Scenario | Response | What to do |
|---|---|---|
| CSV row references unknown SKU | 422 `unknown_sku` with row index | Add the SKU to items via inbound POST or admin UI, retry import |
| CSV row quantity <= 0 | 422 `validation_error` with row index | Fix the quantity, retry |
| Source warehouse equals destination | 422 `source_and_destination_must_differ` | Pick distinct warehouses |
| Inventory has 0 available for an SKU | TO created, line lands SHORT_CLOSED in shortage modal | Wait for replenishment or short-close at import |
| Two pickers pick the same line totalling > committed_qty | Second pick 409 over-pick | Second picker re-syncs and picks remaining |
| Admin tries to approve own submission with gate ON | 403 `self_approval_blocked` | Different admin approves, or operator flips `app_settings.transfer_order_block_self_approval=false` |
| Destination warehouse has no Staging bin | Approve 409 `no_destination_staging_bin` | Add a Staging bin at the destination, retry |
| Admin tries to delete a TO with picks or approvals | 409 `to_not_deletable` | Use Cancel instead |
| Admin tries to cancel a TO with non-pending approvals | 409 `to_already_partially_approved` | Process the remaining approvals or short-close the lines |
| Picker submits with no new picks | 422 `nothing_picked` | Pick more lines before submitting |

## Audit trail

Every state-changing action writes one `audit_log` row through the V-025 hash chain:

- `TO_CREATED` (entity_type=`TO`) on import.
- `TO_LINE_PICKED` (entity_type=`TO_LINE`) on each picker confirm.
- `TO_SUBMITTED` (entity_type=`TO`) on each picker submission, with `to_approval_id` in details.
- `TO_APPROVED` / `TO_REJECTED` (entity_type=`TO_APPROVAL`) on each admin action.
- `TO_LINE_SHORT_CLOSED` (entity_type=`TO_LINE`) on short-close.
- `TO_CANCELLED` (entity_type=`TO`) on cancel.
- `TO_DELETED` (entity_type=`TO`) on delete.
- `TO_CLOSED` (entity_type=`TO`) when the header reaches CLOSED.

Investigators can reconstruct the full TO lifecycle from the audit chain without scanning the row diff.

## See also

- [ERP Integration](erp-integration.md) -- how source ERPs push canonical-shaped resource updates to Sentry (the inbound side of transfers).
- [API Reference](api-reference.md) -- the REST surface for TO + approval routes.
- [Audit log](audit-log.md) -- chain integrity + tamper-evidence guarantees.
