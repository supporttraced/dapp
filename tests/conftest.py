from dataclasses import dataclass

import pytest
from eth_account import Account
from eth_account.signers.local import LocalAccount
from web3 import AsyncWeb3
from web3.contract import AsyncContract

from app import contract
from tests.tester_provider import NodeLikeTesterProvider


@dataclass
class Chain:
    """A fresh in-process chain with a deployed registry."""

    w3: AsyncWeb3
    admin: str
    """Unlocked eth-tester account; `transact({"from": admin})` works."""
    minter: LocalAccount
    """Funded local key, like the API's signer."""
    outsider: str
    registry: AsyncContract


async def deploy(minter_allowed: bool = True) -> Chain:
    w3 = AsyncWeb3(NodeLikeTesterProvider())
    admin, outsider = (await w3.eth.accounts)[:2]
    minter = Account.create()
    await w3.eth.send_transaction({"from": admin, "to": minter.address, "value": 10**18})

    factory = w3.eth.contract(abi=contract.ABI, bytecode=contract.BYTECODE)
    tx = await factory.constructor(admin, [minter.address] if minter_allowed else []).transact({"from": admin})
    address = (await w3.eth.wait_for_transaction_receipt(tx))["contractAddress"]
    registry = w3.eth.contract(address=address, abi=contract.ABI, decode_tuples=True)
    return Chain(w3, admin, minter, outsider, registry)


@pytest.fixture
async def chain() -> Chain:
    return await deploy()
