"""Request and response bodies. Field names match the contract's structs."""

import re
from typing import Annotated

from pydantic import AfterValidator, BaseModel, ConfigDict, Field
from pydantic.alias_generators import to_camel
from pydantic_core import PydanticCustomError

_HEX64 = re.compile(r"[0-9a-fA-F]{64}")


def _hash(value: str) -> str:
    hex_ = value.removeprefix("0x")
    if not _HEX64.fullmatch(hex_):
        raise PydanticCustomError("hash", "must be 64 hex chars")
    return hex_.lower()


Hash = Annotated[str, AfterValidator(_hash)]
"""
A 32-byte hash as 64 hex chars — the format the Rell dapp takes. Accepted with
or without a `0x` prefix; always returned lowercase without one.
"""

Millis = Annotated[int, Field(ge=0, le=2**64 - 1)]
"""Epoch millis, stored on-chain as uint64."""


class _Body(BaseModel):
    # strict: no silent coercion ("123" is not a number, 1 is not a string).
    model_config = ConfigDict(strict=True, alias_generator=to_camel, validate_by_name=True)


class BatteryDppIn(_Body):
    dpp_id: str
    tenant_id: str
    created_by_user_id: str
    module_ref: Hash
    serial_number: str
    data_root_hash: Hash
    schema_version: str
    minted_at: Millis
    status: str


class GarmentDppIn(_Body):
    dpp_id: str
    tenant_id: str
    created_by_user_id: str
    product_ref: Hash
    serial_number: str
    data_root_hash: Hash
    schema_version: str
    minted_at: Millis
    status: str


class CellDppIn(_Body):
    dpp_id: str
    tenant_id: str
    created_by_user_id: str
    template_ref: Hash
    template_key: str
    serial_number: str
    manufacturing_date: str
    dpp_hash: Hash
    schema_version: str
    minted_at: Millis
    status: str


class BatteryDppAnchor(BatteryDppIn):
    anchored_at: Millis


class GarmentDppAnchor(GarmentDppIn):
    anchored_at: Millis


class CellDppAnchor(CellDppIn):
    anchored_at: Millis


class MintResult(_Body):
    dpp_id: str
    tx_hash: str
    block_number: int
    anchored_at: Millis = Field(description="Chain's view of when the anchor was committed (epoch millis).")


class Health(_Body):
    chain_id: int
    block_number: int
    registry: str
    signer: str


class ErrorBody(BaseModel):
    error: str
    message: str
