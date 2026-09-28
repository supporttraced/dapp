"""The DppRegistry contract itself, called directly."""

import pytest
from web3.exceptions import ContractCustomError

from app.contract import decode_error

H1 = bytes.fromhex("11" * 32)
H2 = bytes.fromhex("22" * 32)


def new(dpp_id="DPP-1", tenant="acme", product_type="battery", serial="SN-1", data_hash=H1, status="active"):
    return {
        "dppId": dpp_id,
        "tenantId": tenant,
        "productType": product_type,
        "serialNumber": serial,
        "dataHash": data_hash,
        "schemaVersion": "1.0",
        "uri": "https://example.com/p/" + dpp_id,
        "status": status,
        "issuedAt": 1_700_000_000_000,
    }


def update(dpp_id="DPP-1", data_hash=H2, status="recalled"):
    return {"dppId": dpp_id, "dataHash": data_hash, "schemaVersion": "1.1", "uri": "", "status": status, "issuedAt": 1}


async def send(chain, function, sender=None):
    tx = await function.transact({"from": sender or chain.admin})
    return await chain.w3.eth.wait_for_transaction_receipt(tx)


async def as_minter(chain, function):
    """Sends a write signed by the minter's local key."""
    nonce = await chain.w3.eth.get_transaction_count(chain.minter.address)
    tx = await function.build_transaction({"from": chain.minter.address, "nonce": nonce})
    signed = chain.minter.sign_transaction(tx)
    return await chain.w3.eth.wait_for_transaction_receipt(await chain.w3.eth.send_raw_transaction(signed.raw_transaction))


async def reverts_with(chain, function, sender, name):
    with pytest.raises(ContractCustomError) as e:
        await function.call({"from": sender})
    assert decode_error(e.value.data)[0] == name


async def test_creates_a_passport_for_any_product_type(chain):
    r = chain.registry.functions
    await as_minter(chain, r.createPassport(new()))
    await as_minter(chain, r.createPassport(new("DPP-2", product_type="textile", serial="T-9")))

    p = await r.getPassport("DPP-2").call()
    assert (p.productType, p.serialNumber, p.version, p.status) == ("textile", "T-9", 1, "active")
    assert p.createdAt == p.anchoredAt > 0
    assert await r.passportExists("DPP-1").call()
    assert (await r.getPassport("nope").call()).dppId == ""


async def test_only_minters_write(chain):
    r = chain.registry.functions
    await reverts_with(chain, r.createPassport(new()), chain.outsider, "NotMinter")
    await as_minter(chain, r.createPassport(new()))
    await reverts_with(chain, r.updatePassport(update()), chain.outsider, "NotMinter")


async def test_rejects_duplicates(chain):
    r = chain.registry.functions
    m = chain.minter.address
    await as_minter(chain, r.createPassport(new()))
    await reverts_with(chain, r.createPassport(new()), m, "AlreadyExists")
    await reverts_with(chain, r.createPassport(new("DPP-2")), m, "SerialTaken")
    # The same serial is fine for another tenant or product type.
    await as_minter(chain, r.createPassport(new("DPP-3", tenant="globex")))
    await as_minter(chain, r.createPassport(new("DPP-4", product_type="charger")))


async def test_validates_input(chain):
    r = chain.registry.functions
    m = chain.minter.address
    await reverts_with(chain, r.createPassport(new(dpp_id="")), m, "InvalidInput")
    await reverts_with(chain, r.createPassport(new(serial="")), m, "InvalidInput")
    await reverts_with(chain, r.createPassport(new(data_hash=bytes(32))), m, "InvalidInput")
    await reverts_with(chain, r.createPassport(new(status="")), m, "InvalidInput")


async def test_updates_append_versions(chain):
    r = chain.registry.functions
    await as_minter(chain, r.createPassport(new()))
    await as_minter(chain, r.updatePassport(update()))

    p = await r.getPassport("DPP-1").call()
    assert (p.version, p.status, p.schemaVersion, p.dataHash) == (2, "recalled", "1.1", H2)
    versions = await r.getVersions("DPP-1", 0, 10).call()
    assert [(v.dataHash, v.status) for v in versions] == [(H1, "active"), (H2, "recalled")]
    assert len(await r.getVersions("DPP-1", 1, 10).call()) == 1
    await reverts_with(chain, r.updatePassport(update("nope")), chain.minter.address, "NotFound")


async def test_finds_passports_by_serial_and_tenant(chain):
    r = chain.registry.functions
    for i in range(3):
        await as_minter(chain, r.createPassport(new(f"DPP-{i}", serial=f"SN-{i}")))

    assert (await r.getPassportBySerial("acme", "battery", "SN-1").call()).dppId == "DPP-1"
    assert (await r.getPassportBySerial("acme", "textile", "SN-1").call()).dppId == ""
    assert await r.getPassportCountByTenant("acme").call() == 3
    page = await r.getPassportsByTenant("acme", 0, 2).call()
    assert [p.dppId for p in page] == ["DPP-2", "DPP-1"]
    assert [p.dppId for p in await r.getPassportsByTenant("acme", 2, 2).call()] == ["DPP-0"]
    assert await r.getPassportsByTenant("acme", 5, 2).call() == []


async def test_admin_manages_minters_and_hands_over_in_two_steps(chain):
    r = chain.registry.functions
    await reverts_with(chain, r.setMinter(chain.outsider, True), chain.outsider, "NotAdmin")
    await send(chain, r.setMinter(chain.outsider, True))
    assert await r.isMinter(chain.outsider).call()
    await send(chain, r.setMinter(chain.minter.address, False))
    await reverts_with(chain, r.createPassport(new()), chain.minter.address, "NotMinter")

    await send(chain, r.transferAdmin(chain.outsider))
    assert await r.admin().call() == chain.admin
    await reverts_with(chain, r.acceptAdmin(), chain.admin, "NotAdmin")
    await send(chain, r.acceptAdmin(), chain.outsider)
    assert await r.admin().call() == chain.outsider
