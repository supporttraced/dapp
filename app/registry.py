"""Client for a deployed DppRegistry."""

import asyncio
import logging
import time
from typing import Any

from eth_account.signers.local import LocalAccount
from hexbytes import HexBytes
from starlette.exceptions import HTTPException
from web3 import AsyncWeb3
from web3.contract.async_contract import AsyncContractFunction
from web3.exceptions import ContractCustomError, ContractLogicError
from web3.logs import DISCARD
from web3.types import TxReceipt

from . import contract
from .hashing import data_hash
from .networks import fees
from .schemas import DocumentIn, Health, Passport, PassportCreate, PassportUpdate, Verification, Version, WriteResult

# uvicorn only configures its own loggers; share them so INFO lines are kept.
log = logging.getLogger("uvicorn.error")

MAX_PAGE = 100


class ApiError(HTTPException):
    """An error with the HTTP status and code the API should answer with."""

    def __init__(self, status: int, code: str, message: str):
        super().__init__(status, message)
        self.code = code


def _document_hash(body: DocumentIn | PassportCreate | PassportUpdate) -> str | None:
    if body.data is None:
        return body.data_hash
    try:
        return data_hash(body.data)
    except Exception as err:  # rfc8785 rejects e.g. integers beyond ±2^53
        raise ApiError(400, "INVALID_INPUT", f"data: cannot be canonicalized ({err})") from err


def _now_millis() -> int:
    return time.time_ns() // 1_000_000


def _plain(record: Any) -> dict[str, Any]:
    """An ABI-decoded struct as a dict, bytes as bare hex."""
    return {k: v.hex() if isinstance(v, bytes) else v for k, v in record._asdict().items()}


class RegistryClient:
    """Thin client over a deployed DppRegistry."""

    def __init__(
        self,
        w3: AsyncWeb3,
        address: str,
        signer: LocalAccount,
        *,
        network: str = "",
        explorer: str | None = None,
        confirmations: int = 1,
        poll_interval: float = 1.0,
        receipt_timeout: float = 120.0,
    ):
        self.w3 = w3
        self.contract = w3.eth.contract(address=w3.to_checksum_address(address), abi=contract.ABI, decode_tuples=True)
        self.signer = signer
        self.network = network
        self.explorer = explorer
        self.confirmations = confirmations
        self.poll_interval = poll_interval
        self.receipt_timeout = receipt_timeout

        self._send_lock = asyncio.Lock()
        # Next nonce to use, or None to re-read it from the chain.
        self._next_nonce: int | None = None

    # ------------------------------------------------------------------
    # Startup and health
    # ------------------------------------------------------------------

    async def preflight(self) -> None:
        """
        Fails fast on a misconfigured deployment, before the API takes traffic:
        no registry at the configured address, or a signer that can't write.
        """
        address = self.contract.address
        if len(await self.w3.eth.get_code(address)) == 0:
            raise RuntimeError(f"no contract at REGISTRY_ADDRESS {address} on this chain")
        try:
            is_minter = await self.contract.functions.isMinter(self.signer.address).call()
        except Exception as err:
            raise RuntimeError(f"the contract at {address} is not a DppRegistry") from err
        if not is_minter:
            raise RuntimeError(f"signer {self.signer.address} is not a minter on {address} (python -m app.cli minter add)")
        if await self.w3.eth.get_balance(self.signer.address) == 0:
            log.warning("signer %s has no funds for gas; writes will fail", self.signer.address)

    async def health(self) -> Health:
        try:
            chain_id, block_number, balance, is_minter = await asyncio.gather(
                self.w3.eth.chain_id,
                self.w3.eth.block_number,
                self.w3.eth.get_balance(self.signer.address),
                self.contract.functions.isMinter(self.signer.address).call(),
            )
        except Exception as err:
            raise self._to_api_error(err) from err
        return Health(
            network=self.network,
            chain_id=chain_id,
            block_number=block_number,
            registry=self.contract.address,
            signer=self.signer.address,
            signer_is_minter=is_minter,
            signer_balance_wei=str(balance),
        )

    # ------------------------------------------------------------------
    # Writes
    # ------------------------------------------------------------------

    async def create(self, body: PassportCreate) -> WriteResult:
        struct = {
            "dppId": body.dpp_id,
            "tenantId": body.tenant_id,
            "productType": body.product_type,
            "serialNumber": body.serial_number,
            "dataHash": bytes.fromhex(_document_hash(body)),
            "schemaVersion": body.schema_version,
            "uri": body.uri,
            "status": body.status,
            "issuedAt": body.issued_at if body.issued_at is not None else _now_millis(),
        }
        return await self._write(self.contract.functions.createPassport(struct), body.dpp_id)

    async def update(self, dpp_id: str, body: PassportUpdate) -> WriteResult:
        current = await self.get(dpp_id)
        if current is None:
            raise ApiError(404, "NOT_FOUND", f"no passport {dpp_id}")
        new_hash = _document_hash(body)
        struct = {
            "dppId": dpp_id,
            "dataHash": bytes.fromhex(new_hash if new_hash is not None else current.data_hash),
            "schemaVersion": body.schema_version if body.schema_version is not None else current.schema_version,
            "uri": body.uri if body.uri is not None else current.uri,
            "status": body.status if body.status is not None else current.status,
            "issuedAt": body.issued_at if body.issued_at is not None else _now_millis(),
        }
        return await self._write(self.contract.functions.updatePassport(struct), dpp_id)

    async def _write(self, function: AsyncContractFunction, dpp_id: str) -> WriteResult:
        """Simulates, sends and waits for a write; returns the version it recorded."""
        try:
            # Simulate first: a revert (duplicate, bad input) is decoded here and
            # rejected before a nonce is used or any gas is spent.
            await function.call({"from": self.signer.address})
            tx_hash = await self._send(function)
        except Exception as err:
            raise self._to_api_error(err) from err

        tx = tx_hash.to_0x_hex()
        try:
            receipt = await self._wait(tx_hash)
        except Exception as err:
            # The transaction is out and may still be mined, so this is not a
            # plain failure: once it lands, the passport is readable.
            log.warning("write for %s sent as %s but not confirmed: %r", dpp_id, tx, err)
            raise ApiError(
                504, "TX_PENDING", f"transaction {tx} was sent but is not confirmed yet; read the passport back later"
            ) from err

        if receipt["status"] != 1:
            raise ApiError(502, "CHAIN_ERROR", f"transaction {tx} reverted")
        (event,) = self.contract.events.PassportUpdated().process_receipt(receipt, errors=DISCARD)
        args = event["args"]
        log.info("passport %s v%s anchored in tx %s (block %s)", dpp_id, args["version"], tx, receipt["blockNumber"])
        return WriteResult(
            dpp_id=dpp_id,
            version=args["version"],
            data_hash=args["dataHash"].hex(),
            tx_hash=tx,
            block_number=receipt["blockNumber"],
            anchored_at=args["anchoredAt"],
            explorer_url=self.explorer.replace("{tx}", tx) if self.explorer else None,
        )

    # ------------------------------------------------------------------
    # Reads
    # ------------------------------------------------------------------

    async def get(self, dpp_id: str) -> Passport | None:
        return self._passport(await self._call(self.contract.functions.getPassport(dpp_id)))

    async def get_by_serial(self, tenant_id: str, product_type: str, serial_number: str) -> Passport | None:
        function = self.contract.functions.getPassportBySerial(tenant_id, product_type, serial_number)
        return self._passport(await self._call(function))

    async def versions(self, dpp_id: str, offset: int, limit: int) -> tuple[int, list[Version]] | None:
        """(total, page) of a passport's versions, oldest first; None if it does not exist."""
        passport = await self.get(dpp_id)
        if passport is None:
            return None
        page = await self._call(self.contract.functions.getVersions(dpp_id, offset, min(limit, MAX_PAGE)))
        return passport.version, [Version(version=offset + i + 1, **_plain(v)) for i, v in enumerate(page)]

    async def by_tenant(self, tenant_id: str, offset: int, limit: int) -> tuple[int, list[Passport]]:
        """(total, page) of a tenant's passports, newest first."""
        total, page = await asyncio.gather(
            self._call(self.contract.functions.getPassportCountByTenant(tenant_id)),
            self._call(self.contract.functions.getPassportsByTenant(tenant_id, offset, min(limit, MAX_PAGE))),
        )
        return total, [p for p in map(self._passport, page) if p is not None]

    async def verify(self, dpp_id: str, body: DocumentIn) -> Verification | None:
        """Checks a document against a passport's versions; None if the passport does not exist."""
        result = await self.versions(dpp_id, 0, MAX_PAGE)
        if result is None:
            return None
        total, versions = result
        # Beyond the first page, only the latest version is compared.
        if total > len(versions):
            latest = await self._call(self.contract.functions.getVersions(dpp_id, total - 1, 1))
            versions.append(Version(version=total, **_plain(latest[0])))
        hash_ = _document_hash(body)
        matched = max((v.version for v in versions if v.data_hash == hash_), default=None)
        return Verification(
            dpp_id=dpp_id,
            data_hash=hash_,
            valid=matched == total,
            matched_version=matched,
            latest_version=total,
            status=versions[-1].status,
        )

    async def _call(self, function: AsyncContractFunction) -> Any:
        try:
            return await function.call()
        except Exception as err:
            raise self._to_api_error(err) from err

    @staticmethod
    def _passport(record: Any) -> Passport | None:
        if record.dppId == "":
            return None
        return Passport(**_plain(record))

    # ------------------------------------------------------------------
    # Sending
    # ------------------------------------------------------------------

    async def _send(self, function: AsyncContractFunction) -> HexBytes:
        """
        Sends one transaction at a time with a locally tracked nonce, so
        concurrent requests don't collide. Only the send is serialized; waiting
        for confirmation happens in parallel. After a failed send the nonce is
        re-read from the chain, so a rejected transaction never leaves a gap
        that would stall every later write.
        """
        async with self._send_lock:
            try:
                if self._next_nonce is None:
                    self._next_nonce = await self.w3.eth.get_transaction_count(self.signer.address, "pending")
                params = {"from": self.signer.address, "nonce": self._next_nonce, **await fees(self.w3)}
                tx = await function.build_transaction(params)
                signed = self.signer.sign_transaction(tx)
                tx_hash = await self.w3.eth.send_raw_transaction(signed.raw_transaction)
            except Exception:
                self._next_nonce = None
                raise
            self._next_nonce += 1
            return tx_hash

    async def _wait(self, tx_hash: HexBytes) -> TxReceipt:
        receipt = await self.w3.eth.wait_for_transaction_receipt(
            tx_hash, timeout=self.receipt_timeout, poll_latency=self.poll_interval
        )
        target = receipt["blockNumber"] + self.confirmations - 1
        while await self.w3.eth.block_number < target:
            await asyncio.sleep(self.poll_interval)
        return receipt

    def _to_api_error(self, err: Exception) -> ApiError:
        """
        Maps a failed chain call to an ApiError. Contract reverts become 4xx
        responses; anything else (RPC down, bad API key, out of gas money) is a 502.
        """
        if isinstance(err, ApiError):
            return err

        decoded = None
        if isinstance(err, ContractCustomError) and isinstance(err.data, str):
            decoded = contract.decode_error(err.data)
        name, args = decoded or (None, ())

        match name:
            case "AlreadyExists":
                return ApiError(409, "ALREADY_EXISTS", f"passport already exists: {args[0]}")
            case "SerialTaken":
                return ApiError(409, "SERIAL_TAKEN", f"serial number already has a passport: {args[0]}")
            case "NotFound":
                return ApiError(404, "NOT_FOUND", f"no passport {args[0]}")
            case "InvalidInput":
                return ApiError(400, "INVALID_INPUT", args[0])
            case "NotMinter":
                return ApiError(500, "SIGNER_NOT_MINTER", "the API signer is not a minter on the registry")

        if isinstance(err, ContractLogicError):
            return ApiError(502, "CHAIN_ERROR", str(err))
        # Transport errors can echo the RPC URL, which may hold an API key, so
        # only the error type goes to the client.
        log.error("chain request failed", exc_info=err)
        return ApiError(502, "CHAIN_ERROR", f"chain request failed ({type(err).__name__})")
