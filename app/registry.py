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
from .schemas import (
    BatchItem,
    BatchWriteResult,
    DocumentIn,
    Health,
    Passport,
    PassportBatchCreate,
    PassportCreate,
    PassportUpdate,
    Verification,
    Version,
    WriteResult,
)

# uvicorn only configures its own loggers; share them so INFO lines are kept.
log = logging.getLogger("uvicorn.error")

MAX_PAGE = 100

FALLBACK_GAS_PER_PASSPORT = 600_000
"""Used to size chunks when no single passport can be simulated; measured cost is ~500k."""

GAS_HEADROOM = 1.1
"""Gas limit sent = estimate x this. Kept low because Hedera bills at least 80% of the limit."""


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


def _new_passport(body: PassportCreate) -> dict[str, Any]:
    """A PassportCreate as the contract's NewPassport struct."""
    return {
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
        gas_budget: int = 12_000_000,
    ):
        self.w3 = w3
        self.contract = w3.eth.contract(address=w3.to_checksum_address(address), abi=contract.ABI, decode_tuples=True)
        self.signer = signer
        self.network = network
        self.explorer = explorer
        self.confirmations = confirmations
        self.poll_interval = poll_interval
        self.receipt_timeout = receipt_timeout
        self.gas_budget = gas_budget

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
        return await self._write(self.contract.functions.createPassport(_new_passport(body)), body.dpp_id)

    async def create_batch(self, body: PassportBatchCreate) -> BatchWriteResult:
        """
        Anchors a batch of passports, split into as many transactions as the gas
        budget requires. Idempotent: a passport already on-chain as the same
        passport is reported, never written twice, so a caller that timed out
        can resend the whole batch. Every passport lands in exactly one list of
        the result; one bad entry never blocks the rest.
        """
        structs = [_new_passport(p) for p in body.passports]
        order = {s["dppId"]: i for i, s in enumerate(structs)}
        out: dict[str, list[BatchItem]] = {"anchored": [], "already_anchored": [], "rejected": [], "retryable": []}

        # 1. What is on-chain already, in one call for the whole batch.
        exists = await self._call(self.contract.functions.passportsExist([s["dppId"] for s in structs]))
        fresh = [s for s, e in zip(structs, exists) if not e]
        for bucket, item in await asyncio.gather(*(self._classify_existing(s) for s, e in zip(structs, exists) if e)):
            out[bucket].append(item)

        # 2. Chunks that each simulate cleanly and fit the gas budget.
        chunks = await self._chunk_by_gas(fresh, out["rejected"])

        # 3. Send in nonce order, then wait for every receipt at once.
        sent: list[tuple[list[dict], HexBytes]] = []
        for chunk, gas in chunks:
            try:
                sent.append((chunk, await self._send(self.contract.functions.createPassports(chunk), gas)))
            except Exception as err:
                api = self._to_api_error(err)
                out["retryable"] += [BatchItem(dpp_id=s["dppId"], error=api.code, message=api.detail) for s in chunk]

        receipts = await asyncio.gather(*(self._wait(tx_hash) for _, tx_hash in sent), return_exceptions=True)
        for (chunk, tx_hash), receipt in zip(sent, receipts):
            self._settle(chunk, tx_hash.to_0x_hex(), receipt, out)

        for items in out.values():
            items.sort(key=lambda item: order[item.dpp_id])
        return BatchWriteResult(**out, transactions=[tx_hash.to_0x_hex() for _, tx_hash in sent])

    async def _classify_existing(self, struct: dict) -> tuple[str, BatchItem]:
        """An already-existing passport is either this same passport (skip) or a conflict."""
        dpp_id = struct["dppId"]
        passport, first = await asyncio.gather(
            self.get(dpp_id), self._call(self.contract.functions.getVersions(dpp_id, 0, 1))
        )
        v1_hash = first[0].dataHash.hex() if first else None
        same = (
            passport is not None
            and v1_hash == struct["dataHash"].hex()
            and (passport.tenant_id, passport.product_type, passport.serial_number)
            == (struct["tenantId"], struct["productType"], struct["serialNumber"])
        )
        if same:
            item = BatchItem(dpp_id=dpp_id, version=passport.version, data_hash=v1_hash, anchored_at=passport.created_at)
            return "already_anchored", item
        return "rejected", BatchItem(
            dpp_id=dpp_id, error="ALREADY_EXISTS", message=f"a different passport already has dppId {dpp_id}"
        )

    async def _chunk_by_gas(self, structs: list[dict], rejected: list[BatchItem]) -> list[tuple[list[dict], int]]:
        """
        Splits passports into chunks that each fit the gas budget, simulating
        every chunk before anything is sent. An entry whose revert can be pinned
        on it (its serial is taken, its id raced into existence) is moved to
        `rejected` and the chunk is simulated again without it.
        """
        if not structs:
            return []
        # Pre-size from one passport, so a large batch is never estimated whole —
        # a node refuses to estimate beyond its gas cap rather than reverting.
        # This probe only sizes chunks: it must not reject anything itself, or a
        # bad first entry would be reported twice.
        per_passport = await self._probe_gas(structs)
        size = max(1, self.gas_budget // per_passport)
        chunks: list[tuple[list[dict], int]] = []
        for start in range(0, len(structs), size):
            chunks += await self._fit(list(structs[start : start + size]), rejected)
        return chunks

    async def _probe_gas(self, structs: list[dict]) -> int:
        """Gas for one passport of this batch, from the first that simulates; a safe default if none does."""
        for struct in structs[:3]:
            try:
                return await self.contract.functions.createPassports([struct]).estimate_gas({"from": self.signer.address})
            except Exception:
                continue
        return FALLBACK_GAS_PER_PASSPORT

    async def _fit(self, chunk: list[dict], rejected: list[BatchItem]) -> list[tuple[list[dict], int]]:
        while chunk:
            gas = await self._estimate(chunk, rejected)
            if gas is None:
                continue  # an entry was rejected and removed; simulate what is left
            if gas <= self.gas_budget or len(chunk) == 1:
                return [(chunk, gas)]
            mid = len(chunk) // 2
            return await self._fit(chunk[:mid], rejected) + await self._fit(chunk[mid:], rejected)
        return []

    async def _estimate(self, chunk: list[dict], rejected: list[BatchItem]) -> int | None:
        """
        Gas for the chunk, or None after removing the entries that made it
        revert (appended to `rejected`). A revert that cannot be pinned on an
        entry is a problem with the whole request and is raised.
        """
        try:
            return await self.contract.functions.createPassports(chunk).estimate_gas({"from": self.signer.address})
        except Exception as err:
            api = self._to_api_error(err)
            culprits = await self._culprits(err, chunk)
            if not culprits:
                raise api from err
            for struct in culprits:
                chunk.remove(struct)
                rejected.append(BatchItem(dpp_id=struct["dppId"], error=api.code, message=api.detail))
            return None

    async def _culprits(self, err: Exception, chunk: list[dict]) -> list[dict]:
        decoded = None
        if isinstance(err, ContractCustomError) and isinstance(err.data, str):
            decoded = contract.decode_error(err.data)
        name, args = decoded or (None, ())
        if name == "AlreadyExists":
            return [s for s in chunk if s["dppId"] == args[0]]
        if name == "SerialTaken":
            # The revert names only the serial; the same string may belong to
            # another tenant or product type in this chunk, so ask which is taken.
            same_serial = [s for s in chunk if s["serialNumber"] == args[0]]
            owners = await asyncio.gather(*(
                self._call(self.contract.functions.getPassportBySerial(s["tenantId"], s["productType"], s["serialNumber"]))
                for s in same_serial
            ))
            return [s for s, owner in zip(same_serial, owners) if owner.dppId not in ("", s["dppId"])]
        return []

    def _settle(self, chunk: list[dict], tx: str, receipt: Any, out: dict[str, list[BatchItem]]) -> None:
        """Sorts one sent chunk's passports into the result lists from its receipt."""
        explorer = self.explorer.replace("{tx}", tx) if self.explorer else None
        if isinstance(receipt, BaseException):
            log.warning("batch of %d sent as %s but not confirmed: %r", len(chunk), tx, receipt)
            message = f"transaction {tx} was sent but is not confirmed yet; resend the batch later"
            out["retryable"] += [
                BatchItem(dpp_id=s["dppId"], tx_hash=tx, explorer_url=explorer, error="TX_PENDING", message=message)
                for s in chunk
            ]
            return
        if receipt["status"] != 1:
            # Simulated fine, then reverted when mined: the chain changed in between.
            message = f"transaction {tx} reverted; resend the batch to re-check it"
            out["retryable"] += [
                BatchItem(dpp_id=s["dppId"], tx_hash=tx, explorer_url=explorer, error="CHAIN_ERROR", message=message)
                for s in chunk
            ]
            return
        created = {
            e["args"]["dppId"]: e["args"]
            for e in self.contract.events.PassportCreated().process_receipt(receipt, errors=DISCARD)
        }
        log.info("batch of %d anchored in tx %s (block %s): %d new", len(chunk), tx, receipt["blockNumber"], len(created))
        for s in chunk:
            args = created.get(s["dppId"])
            if args is None:
                # Landed by an earlier or concurrent write; the contract skipped it.
                out["already_anchored"].append(BatchItem(dpp_id=s["dppId"], data_hash=s["dataHash"].hex()))
                continue
            out["anchored"].append(BatchItem(
                dpp_id=s["dppId"],
                version=1,
                data_hash=args["dataHash"].hex(),
                anchored_at=args["anchoredAt"],
                tx_hash=tx,
                block_number=receipt["blockNumber"],
                explorer_url=explorer,
            ))

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

    async def _send(self, function: AsyncContractFunction, gas: int | None = None) -> HexBytes:
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
                if gas is not None:
                    # Already estimated while chunking; skip web3's second estimate.
                    params["gas"] = int(gas * GAS_HEADROOM)
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
