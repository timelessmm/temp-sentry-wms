"""v1.10.0 POS request body schemas.

Strict-typed Pydantic models with extra='forbid'. The POS surface
contract is DRAFT-v1; additive changes only without a header bump.

Field widths match the corresponding DB column widths so a Pydantic-
validated body cannot fail at INSERT on column length:

  sku           VARCHAR(50)   matching items.sku
  warehouse_id  VARCHAR(20)   matching warehouses.warehouse_code
  bin_id        VARCHAR(50)   matching bins.bin_code

The wire-level warehouse_id and bin_id are warehouse_code and bin_code
respectively (string), not the integer surrogate keys; the conversion
to integer IDs happens inside the route's bulk classification query.
"""

from typing import List

from pydantic import BaseModel, ConfigDict, Field


# Column-width caps. Matching the DB columns means a Pydantic-validated
# body cannot fail at the SQL parameter binding on length.
_SKU_MAX = 50
_WAREHOUSE_CODE_MAX = 20
_BIN_CODE_MAX = 50

# Per-line quantity bound. ge=1 because a zero-qty line carries no
# meaning at this surface; le=10000 catches a runaway client integer
# overflow attempt without constraining legitimate single-cart
# bulk orders (a 200-line cart at 10000 qty/line is 2 million units,
# well above any realistic counter sale).
_QTY_MIN = 1
_QTY_MAX = 10000

# Per-cart line bound. min=1 because a zero-line cart is not a cart;
# max=200 is the same ceiling the v1.7.0 inbound batch surface uses,
# so a POS Service that can build inbound batches can also build
# validate-cart bodies with the same memory ceiling.
_LINES_MIN = 1
_LINES_MAX = 200


class ValidateCartLine(BaseModel):
    """One line in the POS cart pre-flight check.

    sku, warehouse_id, and bin_id are wire-level identifiers (strings).
    The route resolves them to integer surrogate keys via the bulk
    classification query.
    """

    model_config = ConfigDict(extra="forbid")

    sku:          str = Field(..., min_length=1, max_length=_SKU_MAX)
    warehouse_id: str = Field(..., min_length=1, max_length=_WAREHOUSE_CODE_MAX)
    bin_id:       str = Field(..., min_length=1, max_length=_BIN_CODE_MAX)
    quantity:     int = Field(..., ge=_QTY_MIN, le=_QTY_MAX)


class ValidateCartBody(BaseModel):
    """POST /api/v1/pos/validate-cart body."""

    model_config = ConfigDict(extra="forbid")

    lines: List[ValidateCartLine] = Field(
        ...,
        min_length=_LINES_MIN,
        max_length=_LINES_MAX,
    )
