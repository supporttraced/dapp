import asyncio

import pytest
from eth_account import Account
from httpx import ASGITransport, AsyncClient
from web3 import AsyncWeb3

from app.main import create_app
from app.registry import REGISTRY_ABI, REGISTRY_BYTECODE, RegistryClient
from tests.tester_provider import NodeLikeTesterProvider

TOKEN = "test-token"
HASH_A = "a" * 64
HASH_B = "0x" + "B" * 64

BATTERY = {
    "dppId": "DPP-1",
    "tenantId": "tenant-1",
    "createdByUserId": "user-1",
    "moduleRef": HASH_A,
    "serialNumber": "BAT-001",
    "dataRootHash": HASH_B,
    "schemaVersion": "1.0.0",
    "mintedAt": 1_700_000_000_000,
    "status": "active",
}


async def serve(authorized: bool = True) -> AsyncClient:
    """The API over a freshly deployed registry on an in-process chain."""
    w3 = AsyncWeb3(NodeLikeTesterProvider())
    admin = (await w3.eth.accounts)[0]
    signer = Account.create()
    await w3.eth.send_transaction({"from": admin, "to": signer.address, "value": 10**18})

    factory = w3.eth.contract(abi=REGISTRY_ABI, bytecode=REGISTRY_BYTECODE)
    minters = [signer.address] if authorized else []
    tx = await factory.constructor(admin, minters).transact({"from": admin})
    address = (await w3.eth.wait_for_transaction_receipt(tx))["contractAddress"]

    registry = RegistryClient(w3, address, signer, poll_interval=0)
    app = create_app(registry, TOKEN)
    return AsyncClient(transport=ASGITransport(app=app), base_url="http://test")


@pytest.fixture
async def client():
    async with await serve() as c:
        yield c


def post(client: AsyncClient, path: str, body: object, token: str = TOKEN):
    return client.post(path, json=body, headers={"authorization": f"Bearer {token}"})


async def test_mints_a_battery_dpp_and_reads_it_back(client):
    res = await post(client, "/dpps/battery", BATTERY)
    assert res.status_code == 201
    minted = res.json()
    assert minted["dppId"] == "DPP-1"
    assert len(minted["txHash"]) == 66

    read = await client.get("/dpps/battery/DPP-1")
    assert read.status_code == 200
    assert read.json() == {**BATTERY, "dataRootHash": "b" * 64, "anchoredAt": minted["anchoredAt"]}


async def test_mints_garment_and_cell_dpps(client):
    rest = {k: v for k, v in BATTERY.items() if k != "moduleRef"}
    assert (await post(client, "/dpps/garment", {**rest, "productRef": HASH_A})).status_code == 201

    cell = {k: v for k, v in rest.items() if k != "dataRootHash"} | {
        "dppId": "CELL-DPP-1",
        "templateRef": HASH_A,
        "templateKey": "template-1",
        "manufacturingDate": "2026-09-01",
        "dppHash": HASH_A,
    }
    assert (await post(client, "/dpps/cell", cell)).status_code == 201
    assert (await client.get("/dpps/cell/CELL-DPP-1")).json()["templateKey"] == "template-1"


async def test_answers_409_for_a_duplicate_dpp(client):
    await post(client, "/dpps/battery", BATTERY)
    res = await post(client, "/dpps/battery", BATTERY)
    assert res.status_code == 409
    assert res.json()["error"] == "ALREADY_ANCHORED"


async def test_answers_400_for_invalid_input_before_touching_the_chain(client):
    res = await post(client, "/dpps/battery", {**BATTERY, "dataRootHash": "abc"})
    assert res.status_code == 400
    assert res.json() == {"error": "INVALID_INPUT", "message": "dataRootHash: must be 64 hex chars"}

    res = await post(client, "/dpps/battery", {**BATTERY, "mintedAt": "1700000000000"})
    assert res.status_code == 400
    assert res.json()["message"].startswith("mintedAt:")


async def test_answers_400_for_a_contract_validation_failure(client):
    res = await post(client, "/dpps/battery", {**BATTERY, "status": "burned"})
    assert res.status_code == 400
    assert res.json()["error"] == "INVALID_INPUT"


async def test_answers_400_for_malformed_json(client):
    res = await client.post(
        "/dpps/battery",
        content=b"{not json",
        headers={"authorization": f"Bearer {TOKEN}", "content-type": "application/json"},
    )
    assert res.status_code == 400
    assert res.json()["error"] == "INVALID_JSON"


async def test_answers_413_for_an_oversized_body(client):
    res = await post(client, "/dpps/battery", {**BATTERY, "status": "x" * 70_000})
    assert res.status_code == 413
    assert res.json()["error"] == "BODY_TOO_LARGE"


async def test_keeps_minting_after_a_rejected_mint_and_under_concurrency(client):
    await post(client, "/dpps/battery", BATTERY)
    assert (await post(client, "/dpps/battery", BATTERY)).status_code == 409

    responses = await asyncio.gather(
        *(
            post(client, "/dpps/battery", {**BATTERY, "dppId": f"DPP-{i}", "serialNumber": f"BAT-00{i}"})
            for i in range(2, 7)
        )
    )
    assert [r.status_code for r in responses] == [201] * 5


async def test_answers_500_when_the_signer_is_not_a_minter():
    async with await serve(authorized=False) as client:
        res = await post(client, "/dpps/battery", BATTERY)
    assert res.status_code == 500
    assert res.json()["error"] == "MINTER_NOT_AUTHORIZED"


async def test_requires_the_bearer_token_to_mint(client):
    assert (await post(client, "/dpps/battery", BATTERY, "wrong")).status_code == 401
    assert (await client.post("/dpps/battery", json=BATTERY)).status_code == 401


async def test_answers_404_for_unknown_dpps_and_kinds(client):
    res = await client.get("/dpps/battery/nope")
    assert res.status_code == 404
    assert res.json() == {"error": "NOT_FOUND", "message": "DPP not anchored: nope"}
    assert (await post(client, "/dpps/shoe", BATTERY)).status_code == 404


async def test_reports_chain_health(client):
    health = (await client.get("/health")).json()
    assert health["chainId"] == 131277322940537  # eth-tester's chain id
    assert health["registry"].startswith("0x") and len(health["registry"]) == 42
