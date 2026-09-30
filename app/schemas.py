"""Request and response bodies. JSON field names are camelCase."""

import re
from typing import Annotated, Any, Self

from pydantic import AfterValidator, BaseModel, ConfigDict, Field, StringConstraints, model_validator
from pydantic.alias_generators import to_camel
from pydantic_core import PydanticCustomError

_HEX64 = re.compile(r"[0-9a-fA-F]{64}")


def _hash(value: str) -> str:
    hex_ = value.removeprefix("0x")
    if not _HEX64.fullmatch(hex_):
        raise PydanticCustomError("hash", "must be 64 hex chars")
    # The registry rejects a zero hash; catching it here names the field, where a
    # revert inside a batch could not say which entry was at fault.
    if hex_.strip("0") == "":
        raise PydanticCustomError("hash", "must not be zero")
    return hex_.lower()


Hash = Annotated[str, AfterValidator(_hash)]
"""A SHA-256 hash as 64 hex chars, `0x` optional on input; returned lowercase without it."""

Millis = Annotated[int, Field(ge=0, le=2**64 - 1)]
"""Epoch millis, stored on-chain as uint64."""

Id = Annotated[str, StringConstraints(min_length=1, max_length=128)]
Status = Annotated[str, StringConstraints(min_length=1, max_length=32)]
SchemaVersion = Annotated[str, StringConstraints(max_length=32)]
Uri = Annotated[str, StringConstraints(max_length=2048)]


class _Body(BaseModel):
    # strict: no silent coercion ("123" is not a number, 1 is not a string).
    model_config = ConfigDict(strict=True, alias_generator=to_camel, validate_by_name=True)


class _Document(_Body):
    data: dict[str, Any] | None = Field(
        None, description="The passport document. The API anchors its hash; the document itself is not stored."
    )
    data_hash: Hash | None = Field(
        None, description="SHA-256 of the document in RFC 8785 canonical JSON, if you hash it yourself."
    )


class PassportCreate(_Document):
    """Exactly one of `data` or `dataHash`."""

    dpp_id: Id
    tenant_id: Id = Field(description="The economic operator issuing the passport.")
    product_type: Id = Field(description='Any product category, e.g. "battery", "textile", "electronics".')
    serial_number: Id = Field(description="Unique per tenant and product type.")
    schema_version: SchemaVersion = "1.0"
    uri: Uri = Field("", description="Where the full passport can be read, e.g. the QR code target.")
    status: Status = "active"
    issued_at: Millis | None = Field(None, description="Defaults to now.")

    @model_validator(mode="after")
    def _one_document(self) -> Self:
        if (self.data is None) == (self.data_hash is None):
            raise PydanticCustomError("document", "give exactly one of data or dataHash")
        return self


class PassportUpdate(_Document):
    """
    Appends a version. Fields left out keep their current value, so
    `{"status": "recalled"}` alone records a recall.
    """

    schema_version: SchemaVersion | None = None
    uri: Uri | None = None
    status: Status | None = None
    issued_at: Millis | None = Field(None, description="Defaults to now.")

    @model_validator(mode="after")
    def _something_changes(self) -> Self:
        if self.data is not None and self.data_hash is not None:
            raise PydanticCustomError("document", "give at most one of data or dataHash")
        if not self.model_fields_set - {"issued_at"}:
            raise PydanticCustomError("empty", "nothing to update")
        return self


MAX_BATCH = 100
"""Most passports per batch request. Equals DppRegistry.MAX_BATCH; gas decides how many go per transaction."""


class PassportBatchCreate(_Body):
    """
    Up to MAX_BATCH passports. Each is anchored exactly once however often the
    batch is resent, so a caller that did not hear back can simply retry it.
    """

    passports: list[PassportCreate] = Field(min_length=1, max_length=MAX_BATCH)

    @model_validator(mode="after")
    def _distinct(self) -> Self:
        ids = [p.dpp_id for p in self.passports]
        if len(set(ids)) != len(ids):
            raise PydanticCustomError("duplicate", "each dppId may appear once per batch")
        serials = [(p.tenant_id, p.product_type, p.serial_number) for p in self.passports]
        if len(set(serials)) != len(serials):
            raise PydanticCustomError("duplicate", "each serial number may appear once per tenant and product type")
        return self


class DocumentIn(_Document):
    """Exactly one of `data` or `dataHash`."""

    @model_validator(mode="after")
    def _one_document(self) -> Self:
        if (self.data is None) == (self.data_hash is None):
            raise PydanticCustomError("document", "give exactly one of data or dataHash")
        return self


class Passport(_Body):
    """A passport with its latest version."""

    dpp_id: str
    tenant_id: str
    product_type: str
    serial_number: str
    version: int = Field(description="Number of versions; the fields below are the latest one's.")
    data_hash: str
    schema_version: str
    uri: str
    status: str
    issued_at: Millis
    anchored_at: Millis = Field(description="When the chain recorded the latest version (epoch millis).")
    created_at: Millis = Field(description="When the chain recorded version 1 (epoch millis).")


class Version(_Body):
    version: int
    data_hash: str
    schema_version: str
    uri: str
    status: str
    issued_at: Millis
    anchored_at: Millis


class VersionPage(_Body):
    dpp_id: str
    total: int
    items: list[Version]


class PassportPage(_Body):
    tenant_id: str
    total: int
    items: list[Passport]


class WriteResult(_Body):
    dpp_id: str
    version: int
    data_hash: str
    tx_hash: str
    block_number: int
    anchored_at: Millis
    explorer_url: str | None = Field(None, description="The transaction on a public block explorer.")


class BatchItem(_Body):
    """What happened to one passport of a batch."""

    dpp_id: str
    version: int | None = Field(None, description="1 for a passport created now; its current version if it already existed.")
    data_hash: str | None = None
    anchored_at: Millis | None = Field(None, description="When the chain recorded version 1 (epoch millis).")
    tx_hash: str | None = Field(None, description="The transaction that wrote it, or that is still pending.")
    block_number: int | None = None
    explorer_url: str | None = None
    error: str | None = Field(None, description="For rejected and retryable items: the error code.")
    message: str | None = None


class BatchWriteResult(_Body):
    """
    Every passport of the batch lands in exactly one list. `anchored` and
    `alreadyAnchored` are both on-chain now; `rejected` must not be resent as-is;
    `retryable` is safe to resend, since creation is idempotent.
    """

    anchored: list[BatchItem] = Field(description="Created by this request.")
    already_anchored: list[BatchItem] = Field(description="Already on-chain as this same passport; nothing written.")
    rejected: list[BatchItem] = Field(description="Conflicts with a different passport on-chain (ALREADY_EXISTS, SERIAL_TAKEN).")
    retryable: list[BatchItem] = Field(description="Not confirmed (TX_PENDING, with its tx) or not sent (CHAIN_ERROR). Resend later.")
    transactions: list[str] = Field(description="Hashes of the transactions this request sent, one per gas-sized chunk.")


class Verification(_Body):
    dpp_id: str
    data_hash: str = Field(description="Hash of the document you sent.")
    valid: bool = Field(description="True if it matches the passport's latest version.")
    matched_version: int | None = Field(description="The version it matches, if any (an outdated one if not valid).")
    latest_version: int
    status: str = Field(description="Status of the latest version.")


class HashResult(_Body):
    data_hash: str


class Health(_Body):
    network: str
    chain_id: int
    block_number: int
    registry: str
    signer: str
    signer_is_minter: bool
    signer_balance_wei: str = Field(description="Gas funds left on the signer (decimal string, wei).")


class ErrorBody(BaseModel):
    error: str
    message: str
