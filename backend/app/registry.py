"""Client for a deployed DppAnchorRegistry contract."""

import asyncio
import json
import logging
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Literal

from eth_account.signers.local import LocalAccount
from eth_utils import abi_to_signature
from hexbytes import HexBytes
from pydantic import BaseModel
from starlette.exceptions import HTTPException
from web3 import AsyncWeb3
from web3.contract.async_contract import AsyncContractFunction
from web3.exceptions import ContractCustomError, ContractLogicError
from web3.logs import DISCARD
from web3.types import TxReceipt

from .schemas import (
    BatteryDppAnchor,
    BatteryDppIn,
    CellDppAnchor,
    CellDppIn,
    GarmentDppAnchor,
    GarmentDppIn,
    Health,
    MintResult,
)

log = logging.getLogger(__name__)

_ARTIFACT = json.loads((Path(__file__).parent / "contracts" / "DppAnchorRegistry.json").read_text())

REGISTRY_ABI: list[dict[str, Any]] = _ARTIFACT["abi"]
"""ABI of the compiled registry, exported by `npm run build`."""

REGISTRY_BYTECODE: str = _ARTIFACT["bytecode"]

NetworkName = Literal["arbitrumSepolia", "arbitrumOne"]


@dataclass(frozen=True)
class Network:
    chain_id: int
    host: str


NETWORKS: dict[NetworkName, Network] = {
    "arbitrumSepolia": Network(421614, "api-arbitrum-sepolia.n.dwellir.com"),
    "arbitrumOne": Network(42161, "api-arbitrum-mainnet-archive.n.dwellir.com"),
}
"""
Dwellir endpoints for the networks the registry is deployed to. The API key
goes in the URL path.
"""


def dwellir_rpc_url(network: NetworkName, api_key: str) -> str:
    return f"https://{NETWORKS[network].host}/{api_key}"


class ApiError(HTTPException):
    """An error with the HTTP status and code the API should answer with."""

    def __init__(self, status: int, code: str, message: str):
        super().__init__(status, message)
        self.code = code


@dataclass(frozen=True)
class Kind:
    mint: str
    get: str
    event: str
    input: type[BaseModel]
    anchor: type[BaseModel]


DppKind = Literal["battery", "garment", "cell"]

KINDS: dict[DppKind, Kind] = {
    "battery": Kind("mintDpp", "getDppAnchor", "DppAnchored", BatteryDppIn, BatteryDppAnchor),
    "garment": Kind("mintGarmentDpp", "getGarmentDppAnchor", "GarmentDppAnchored", GarmentDppIn, GarmentDppAnchor),
    "cell": Kind("mintCellDpp", "getCellDppAnchor", "CellDppAnchored", CellDppIn, CellDppAnchor),
}
"""
The three passport kinds, mirroring the Rell `mint_dpp`, `mint_garment_dpp`
and `mint_cell_dpp` operations.
"""

_FUNCTIONS = {e["name"]: e for e in REGISTRY_ABI if e["type"] == "function"}
_ERRORS = {
    "0x" + AsyncWeb3.keccak(text=abi_to_signature(e))[:4].hex(): e for e in REGISTRY_ABI if e["type"] == "error"
}


def _to_struct(function: str, body: BaseModel) -> dict[str, Any]:
    """Converts a request body into the input struct of a mint function."""
    data = body.model_dump(by_alias=True)
    (param,) = _FUNCTIONS[function]["inputs"]
    return {
        c["name"]: bytes.fromhex(data[c["name"]]) if c["type"] == "bytes32" else data[c["name"]]
        for c in param["components"]
    }


class RegistryClient:
    """Thin client over a deployed DppAnchorRegistry."""

    def __init__(
        self,
        w3: AsyncWeb3,
        address: str,
        signer: LocalAccount,
        confirmations: int = 1,
        poll_interval: float = 1.0,
        receipt_timeout: float = 120.0,
    ):
        self.w3 = w3
        self.contract = w3.eth.contract(address=w3.to_checksum_address(address), abi=REGISTRY_ABI, decode_tuples=True)
        self.signer = signer
        self.confirmations = confirmations
        self.poll_interval = poll_interval
        self.receipt_timeout = receipt_timeout

        self._send_lock = asyncio.Lock()
        # Next nonce to use, or None to re-read it from the chain.
        self._next_nonce: int | None = None

    async def health(self) -> Health:
        try:
            chain_id, block_number = await asyncio.gather(self.w3.eth.chain_id, self.w3.eth.block_number)
        except Exception as err:
            raise self._to_api_error(err) from err
        return Health(
            chain_id=chain_id,
            block_number=block_number,
            registry=self.contract.address,
            signer=self.signer.address,
        )

    async def mint(self, kind: DppKind, body: BaseModel) -> MintResult:
        """Submits a mint transaction and waits for it to be confirmed."""
        spec = KINDS[kind]
        function = self.contract.functions[spec.mint](_to_struct(spec.mint, body))

        try:
            # Simulate first: a revert (duplicate, bad input) is decoded here and
            # rejected before a nonce is used or any gas is spent.
            await function.call({"from": self.signer.address})
            tx_hash = await self._send(function)
            receipt = await self._wait(tx_hash)
        except Exception as err:
            raise self._to_api_error(err) from err

        if receipt["status"] != 1:
            raise ApiError(502, "CHAIN_ERROR", f"mint transaction reverted: {tx_hash.to_0x_hex()}")
        (event,) = self.contract.events[spec.event]().process_receipt(receipt, errors=DISCARD)
        return MintResult(
            dpp_id=body.dpp_id,
            tx_hash=tx_hash.to_0x_hex(),
            block_number=receipt["blockNumber"],
            anchored_at=event["args"]["anchoredAt"],
        )

    async def get(self, kind: DppKind, dpp_id: str) -> dict[str, Any] | None:
        """Reads an anchor back from the chain. Returns None if it does not exist."""
        try:
            anchor = await self.contract.functions[KINDS[kind].get](dpp_id).call()
        except Exception as err:
            raise self._to_api_error(err) from err
        if anchor.dppId == "":
            return None
        return {k: v.hex() if isinstance(v, bytes) else v for k, v in anchor._asdict().items()}

    async def _send(self, function: AsyncContractFunction) -> HexBytes:
        """
        Sends one transaction at a time with a locally tracked nonce, so
        concurrent requests don't collide. Only the send is serialized; waiting
        for confirmation happens in parallel. After a failed send the nonce is
        re-read from the chain, so a rejected transaction never leaves a gap
        that would stall every later mint.
        """
        async with self._send_lock:
            try:
                if self._next_nonce is None:
                    self._next_nonce = await self.w3.eth.get_transaction_count(self.signer.address, "pending")
                tx = await function.build_transaction({"from": self.signer.address, "nonce": self._next_nonce})
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

        name, args = None, ()
        if isinstance(err, ContractCustomError) and isinstance(err.data, str) and err.data[:10] in _ERRORS:
            abi = _ERRORS[err.data[:10]]
            name = abi["name"]
            args = self.w3.codec.decode([i["type"] for i in abi["inputs"]], bytes.fromhex(err.data[10:]))

        match name:
            case "AlreadyAnchored":
                return ApiError(409, "ALREADY_ANCHORED", f"DPP already anchored: {args[0]}")
            case "SerialAlreadyAnchored":
                return ApiError(409, "SERIAL_ALREADY_ANCHORED", f"Serial already anchored: {args[0]}")
            case "InvalidInput":
                return ApiError(400, "INVALID_INPUT", args[0])
            case "AccessControlUnauthorizedAccount":
                return ApiError(500, "MINTER_NOT_AUTHORIZED", "API signer does not hold MINTER_ROLE on the registry")

        if isinstance(err, ContractLogicError):
            return ApiError(502, "CHAIN_ERROR", str(err))
        # Transport errors can echo the RPC URL, which holds the Dwellir API
        # key, so only the error type goes to the client.
        log.error("chain request failed", exc_info=err)
        return ApiError(502, "CHAIN_ERROR", f"chain request failed ({type(err).__name__})")
