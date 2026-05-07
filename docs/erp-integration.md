# ERP Integration

Operator-facing reference for connecting an external ERP or commerce
platform to Sentry WMS via the v1.7.0 inbound API. The full wire
contract lives in the [API Reference](api-reference.md) and the
[inbound OpenAPI](api/inbound-openapi.yaml); this page covers the
integration mental model + the v1.8.0 mapping_overrides feature.

## How an inbound integration is wired

Three pieces of operator-managed configuration:

1. **`inbound_source_systems_allowlist` row** -- one per source
   ERP. The handler refuses any request whose token's `source_system`
   isn't on the allowlist. Insert via SQL:

       INSERT INTO inbound_source_systems_allowlist (source_system, kind)
            VALUES ('acme-erp', 'connector');

2. **Mapping document** -- one YAML file per source ERP under
   `db/mappings/<source_system>.yaml`. Translates the ERP's payload
   shape into Sentry's canonical model. The annotated template at
   `db/mappings/example-template.yaml.template` is the starting
   point. Boot loads each file once; the canonical-column validator
   (#267) refuses startup if any `canonical:` field name doesn't
   match a real column on the canonical table.

3. **WMS token** issued via the admin panel with:
   - `source_system` matching the allowlist row,
   - `inbound_resources` listing which canonical resources the token
     can write to (subset of `sales_orders`, `items`, `customers`,
     `vendors`, `purchase_orders`).

The connector author then POSTs canonical-shaped resource updates to
`/api/v1/inbound/<resource>` with `X-WMS-Token: <plaintext>`.

## v1.8.0: per-token `mapping_overrides`

The `mapping_override` boolean capability flag (v1.7.0) now pairs
with a `mapping_overrides` JSONB column on `wms_tokens` (mig 052).
When both are set, the inbound handler applies the JSONB to the
canonical record after `mapping_loader.apply()` runs, replacing any
source-derived value for the listed canonical fields.

### When to use

When a source ERP can't (or won't) emit a particular canonical field
correctly and the value is the same for every order from that source.
Examples:

- Source ERP doesn't emit `currency`; the operator knows every order
  from this ERP is in USD.
- Source ERP emits a numeric `marketplace_id` that needs to map to a
  string label like `"AMAZON"` for downstream consumers.
- A connector only sends to one warehouse; the operator forces
  `warehouse_id` rather than relying on a mapping doc derived
  expression.

For one-off fixes (a single bad order), use direct SQL on the
canonical row instead. Per-token overrides apply to **every** request
from the token.

### How to set

At token issuance time via `POST /api/admin/tokens`:

```json
{
  "token_name": "acme-erp-prod",
  "source_system": "acme-erp",
  "inbound_resources": ["sales_orders"],
  "mapping_override": true,
  "mapping_overrides": {
    "currency": "USD",
    "warehouse_id": 1
  }
}
```

The admin endpoint validates every override key against the columns
of the canonical tables for the token's `inbound_resources`. An
unknown key returns `422 unknown_mapping_overrides_keys` with the
offending keys listed; operators see a clear error rather than a
silent no-op at first inbound POST.

To change overrides on an existing token, re-issue a fresh token
(rotation does not change scope by design) or update the JSONB
column directly:

    UPDATE wms_tokens
       SET mapping_overrides = '{"currency":"USD","warehouse_id":2}'::jsonb
     WHERE token_id = <id>;

The token cache invalidates within 60 seconds across all workers.

### How it interacts with the mapping doc

`mapping_overrides` runs **after** `mapping_loader.apply()`. The
flow per inbound POST:

1. Source payload arrives.
2. Mapping doc translates source -> canonical record.
3. Per-token overrides replace any listed canonical field's value.
4. Canonical record is INSERTed/UPDATEd.

The override wins. If the mapping doc declares
`currency: "$.order.currency"` and the token overrides `currency`
to `"USD"`, the stored value is `"USD"` regardless of what the
source payload contained.

### What lands in `audit_log`

Token issue / rotate / delete audit rows record only the override
**keys** (sorted), never the values:

```json
{
  "token_name": "acme-erp-prod",
  "mapping_overrides_keys": ["currency", "warehouse_id"],
  ...
}
```

Values may include credential-shaped strings or fragments that
matter to log scraping; the field-name footprint is sufficient for
investigators to reconstruct who configured what when they pair it
with the live `wms_tokens.mapping_overrides` JSONB.

### Per-request body overrides are not supported

Per-request `mapping_overrides` in the inbound POST body is rejected
with `403 mapping_overrides_not_supported_in_body`. The static
per-token shape is the v1.8 surface. Per-request overrides may land
in v1.x if real demand surfaces; for v1.8 the surface is locked.

### Security note

`mapping_override` is a **canonical-write capability**. The token
holder can write any value to any canonical field listed in
`mapping_overrides`. Grant only to tokens whose plaintext is held by
a single connector, not shared with humans or pasted into runbooks.
Rotate the token if the override list needs to change and you want
the previous shape invalidated atomically.

## v1.8.0: line items write through to relational tables

For `purchase_orders` and `sales_orders`, the inbound handler now
writes resolved line items to the relational `purchase_order_lines`
/ `sales_order_lines` tables (v1.8.0 #289). v1.7 stored lines only
in `inbound_<resource>.canonical_payload` JSONB; receiving had
nothing to scan against and picking had no allocation target.

### Required line shape

The mapping doc's `line_items` block must declare these canonical
fields per line:

- `item_id` -- the canonical item UUID. Use `cross_system_lookup`
  with `source_type: item` so the source-system SKU translates to
  the canonical UUID. Items must already exist in Sentry +
  `cross_system_mappings` (via prior `/api/v1/inbound/items` POST
  or admin UI item create) for the lookup to resolve.
- `quantity_ordered` -- positive integer.

`line_number` auto-assigns 1..N when omitted; declare it explicitly
to honor the source's ordering. Other line columns
(`quantity_received` for PO; `quantity_allocated` /
`quantity_picked` / `quantity_packed` / `quantity_shipped` for SO)
default to 0; downstream Sentry workflows update them.

### Idempotency on re-POST

A re-POST with the same `external_id` (newer `external_version`)
replaces existing lines via DELETE + INSERT, but only when no
downstream activity exists. The handler returns `409
lines_in_flight` if:

- PO: any line has `quantity_received > 0`, OR
- SO: any line has `quantity_allocated`, `quantity_picked`,
  `quantity_packed`, or `quantity_shipped > 0`.

Operators cancel or complete the in-flight work (or reverse the
receiving step) before re-POSTing. The canonical header upsert
rolls back via the outer transaction, so the v1 line state is
preserved.

### Empty `line_items` array on re-POST

A re-POST with an empty `lineItems` source array preserves
existing lines. This allows header-only updates (e.g., status
change, carrier update on SO) without nuking the line list.

### Item resolution misses

If `cross_system_lookup` on a line's `item_id` does not resolve
(no `cross_system_mappings` row for the source SKU), the handler
returns `409 cross_system_lookup_miss` with `source_type: item`
and the unresolved SKU. Operators ingest the missing item first,
then retry the PO / SO.

## See also

- [Annotated mapping template](https://github.com/hightower-systems/sentry-wms/blob/main/db/mappings/example-template.yaml.template)
- [Inbound OpenAPI](api/inbound-openapi.yaml)
- [Mapping document JSON schema](api/mapping-document-schema.json)
- [Connectors](connectors.md) -- when to write a pull-mode connector
  instead of using the push-mode inbound API.
