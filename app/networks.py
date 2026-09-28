"""The chains the registry runs on, and how to reach them."""

import asyncio
from dataclasses import dataclass

from web3 import AsyncHTTPProvider, AsyncWeb3

from .config import NetworkName, Settings


@dataclass(frozen=True)
class Network:
    chain_id: int
    rpc_url: str
    """Default RPC endpoint; `{key}` is replaced with the Dwellir API key."""
    testnet: bool
    explorer: str | None = None
    """Block explorer URL for a transaction; `{tx}` is replaced with its hash."""


NETWORKS: dict[NetworkName, Network] = {
    "arbitrumSepolia": Network(
        421614, "https://api-arbitrum-sepolia.n.dwellir.com/{key}", True, "https://sepolia.arbiscan.io/tx/{tx}"
    ),
    "arbitrumOne": Network(
        42161, "https://api-arbitrum-mainnet-archive.n.dwellir.com/{key}", False, "https://arbiscan.io/tx/{tx}"
    ),
    # Hashio: Hedera's free public relay. Testnet HBAR comes from
    # https://portal.hedera.com/faucet without an account.
    "hederaTestnet": Network(296, "https://testnet.hashio.io/api", True, "https://hashscan.io/testnet/transaction/{tx}"),
    # Hashio is rate-limited and meant for development; in production set
    # RPC_URL to a commercial Hedera JSON-RPC relay.
    "hederaMainnet": Network(295, "https://mainnet.hashio.io/api", False, "https://hashscan.io/mainnet/transaction/{tx}"),
    # A local dev chain, e.g. anvil. From docker compose use
    # RPC_URL=http://host.docker.internal:8545.
    "localhost": Network(31337, "http://127.0.0.1:8545", True),
}


def rpc_url(settings: Settings) -> str:
    if settings.rpc_url:
        return settings.rpc_url
    url = NETWORKS[settings.network].rpc_url
    if "{key}" in url:
        if not settings.dwellir_api_key:
            raise RuntimeError(f"DWELLIR_API_KEY is not set (needed for {settings.network}, see .env.example)")
        url = url.replace("{key}", settings.dwellir_api_key)
    return url


async def connect(settings: Settings) -> AsyncWeb3:
    """
    Connects to the configured network. The chain ID is checked even for a
    custom RPC_URL, so a mainnet NETWORK is never served from a testnet
    endpoint or vice versa.
    """
    w3 = AsyncWeb3(AsyncHTTPProvider(rpc_url(settings)))
    chain_id = await w3.eth.chain_id
    expected = NETWORKS[settings.network].chain_id
    if chain_id != expected:
        await w3.provider.disconnect()
        raise RuntimeError(f"RPC returned chain {chain_id}, expected {expected} for NETWORK={settings.network}")
    return w3


async def fees(w3: AsyncWeb3) -> dict[str, int]:
    """
    Fee fields for the next transaction. web3's default reads the base fee of
    the *pending* block, which Hedera's relay reports in tinybars rather than
    wei, producing a fee far below the minimum; so the latest block's base fee
    is used, floored at the node's own eth_gasPrice.
    """
    block, gas_price = await asyncio.gather(w3.eth.get_block("latest"), w3.eth.gas_price)
    base_fee = block.get("baseFeePerGas")
    if base_fee is None:
        return {"gasPrice": gas_price}
    priority = await w3.eth.max_priority_fee
    return {"maxFeePerGas": max(2 * base_fee + priority, gas_price), "maxPriorityFeePerGas": priority}
