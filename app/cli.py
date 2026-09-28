"""
Registry administration. Reads `.env` like the API.

    python -m app.cli compile [--check]      compile contracts/DppRegistry.sol
    python -m app.cli deploy [--admin ADDR] [--minter ADDR ...] [--yes]
    python -m app.cli minter add|remove ADDR
    python -m app.cli status
"""

import argparse
import asyncio
import sys

from eth_account import Account
from eth_account.signers.local import LocalAccount
from web3 import AsyncWeb3

from . import contract, networks
from .compiler import ARTIFACT, compile_registry, serialize
from .config import Settings, load_settings


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="python -m app.cli", description="DPP registry administration")
    commands = parser.add_subparsers(dest="command", required=True)

    compile_ = commands.add_parser("compile", help="compile contracts/DppRegistry.sol into app/contracts/")
    compile_.add_argument("--check", action="store_true", help="fail if the committed artifact is out of date")

    deploy = commands.add_parser("deploy", help="deploy a new registry with DEPLOYER_PRIVATE_KEY")
    deploy.add_argument("--admin", help="manages minters (default: the deployer)")
    deploy.add_argument(
        "--minter", action="append", default=[], help="may write passports; repeatable (default: the API signer)"
    )
    deploy.add_argument("--yes", action="store_true", help="don't ask for confirmation on a mainnet")

    minter = commands.add_parser("minter", help="grant or revoke write access (signed by ADMIN_PRIVATE_KEY)")
    minter.add_argument("action", choices=["add", "remove"])
    minter.add_argument("address")

    commands.add_parser("status", help="check the RPC, the registry and the API signer")

    args = parser.parse_args(argv)
    try:
        if args.command == "compile":
            return _compile(args.check)
        settings = load_settings()
        return asyncio.run({"deploy": _deploy, "minter": _minter, "status": _status}[args.command](settings, args))
    except RuntimeError as err:
        print(f"error: {err}", file=sys.stderr)
        return 1


def _compile(check: bool) -> int:
    fresh = serialize(compile_registry())
    if check:
        if not ARTIFACT.exists() or ARTIFACT.read_text() != fresh:
            print(f"error: {ARTIFACT.name} is out of date; run `python -m app.cli compile` and commit it", file=sys.stderr)
            return 1
        print(f"{ARTIFACT.name} is up to date")
        return 0
    ARTIFACT.write_text(fresh)
    print(f"wrote {ARTIFACT}")
    return 0


def _account(key: str | None, name: str) -> LocalAccount:
    if not key:
        raise RuntimeError(f"{name} must be set (see .env.example)")
    return Account.from_key(key)


async def _transact(w3: AsyncWeb3, account: LocalAccount, tx: dict) -> dict:
    """Signs, sends and waits for a transaction built without fees or nonce."""
    tx |= {"nonce": await w3.eth.get_transaction_count(account.address, "pending"), **await networks.fees(w3)}
    tx.setdefault("chainId", await w3.eth.chain_id)
    if "gas" not in tx:
        tx["gas"] = await w3.eth.estimate_gas(tx)
    tx_hash = await w3.eth.send_raw_transaction(account.sign_transaction(tx).raw_transaction)
    print(f"sent {tx_hash.to_0x_hex()}, waiting for confirmation...")
    receipt = await w3.eth.wait_for_transaction_receipt(tx_hash, timeout=300, poll_latency=2)
    if receipt["status"] != 1:
        raise RuntimeError(f"transaction {tx_hash.to_0x_hex()} reverted")
    return receipt


def _explorer(settings: Settings, tx_hash: str) -> str:
    template = networks.NETWORKS[settings.network].explorer
    return template.replace("{tx}", tx_hash) if template else tx_hash


async def _deploy(settings: Settings, args: argparse.Namespace) -> int:
    deployer = _account(settings.deployer_private_key, "DEPLOYER_PRIVATE_KEY")
    admin = args.admin or deployer.address
    if args.minter:
        minters = args.minter
    else:
        minters = [_account(settings.minter_private_key or settings.deployer_private_key, "MINTER_PRIVATE_KEY").address]

    spec = networks.NETWORKS[settings.network]
    print(f"network   {settings.network} (chain {spec.chain_id}{'' if spec.testnet else ', MAINNET'})")
    print(f"deployer  {deployer.address}\nadmin     {admin}\nminters   {', '.join(minters)}")
    if not spec.testnet and not args.yes:
        if input("This spends real funds. Deploy? [y/N] ").strip().lower() != "y":
            print("aborted")
            return 1

    w3 = await networks.connect(settings)
    try:
        factory = w3.eth.contract(abi=contract.ABI, bytecode=contract.BYTECODE)
        tx = await factory.constructor(w3.to_checksum_address(admin), [w3.to_checksum_address(m) for m in minters]).build_transaction(
            {"from": deployer.address, **await networks.fees(w3)}
        )
        receipt = await _transact(w3, deployer, tx)
    finally:
        await w3.provider.disconnect()

    address = receipt["contractAddress"]
    print(f"\nDppRegistry deployed at {address}")
    print(f"transaction {_explorer(settings, receipt['transactionHash'].to_0x_hex())}")
    print(f"\nSet in .env:  REGISTRY_ADDRESS={address}")
    return 0


async def _minter(settings: Settings, args: argparse.Namespace) -> int:
    settings.require("registry_address")
    admin = _account(settings.admin_private_key or settings.deployer_private_key, "ADMIN_PRIVATE_KEY")
    w3 = await networks.connect(settings)
    try:
        registry = w3.eth.contract(address=w3.to_checksum_address(settings.registry_address), abi=contract.ABI)
        if await registry.functions.admin().call() != admin.address:
            raise RuntimeError(f"{admin.address} is not the registry admin")
        allow = args.action == "add"
        tx = await registry.functions.setMinter(w3.to_checksum_address(args.address), allow).build_transaction(
            {"from": admin.address, **await networks.fees(w3)}
        )
        receipt = await _transact(w3, admin, tx)
    finally:
        await w3.provider.disconnect()
    print(f"{args.address} {'can now' if allow else 'can no longer'} write passports")
    print(f"transaction {_explorer(settings, receipt['transactionHash'].to_0x_hex())}")
    return 0


async def _status(settings: Settings, args: argparse.Namespace) -> int:
    ok = True
    w3 = await networks.connect(settings)
    try:
        print(f"✅ {settings.network} reachable, block {await w3.eth.block_number}")
        if not settings.registry_address:
            print("⚠️  REGISTRY_ADDRESS is not set")
            return 1
        address = w3.to_checksum_address(settings.registry_address)
        if len(await w3.eth.get_code(address)) == 0:
            print(f"❌ no contract at {address}")
            return 1
        registry = w3.eth.contract(address=address, abi=contract.ABI)
        print(f"✅ DppRegistry at {address}, admin {await registry.functions.admin().call()}")

        key = settings.minter_private_key or settings.deployer_private_key
        if key:
            signer = Account.from_key(key).address
            is_minter = await registry.functions.isMinter(signer).call()
            balance = await w3.eth.get_balance(signer)
            ok = is_minter and balance > 0
            print(f"{'✅' if is_minter else '❌'} API signer {signer} {'is' if is_minter else 'is NOT'} a minter")
            print(f"{'✅' if balance else '❌'} signer balance {w3.from_wei(balance, 'ether')} (native token)")
        else:
            print("⚠️  no MINTER_PRIVATE_KEY / DEPLOYER_PRIVATE_KEY set")
            ok = False
    finally:
        await w3.provider.disconnect()
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
