import asyncio

import pytest
from httpx import ASGITransport, AsyncClient

from app.hashing import data_hash
from app.main import create_app
from app.registry import RegistryClient
from tests.conftest import Chain, deploy

TOKEN = "test-token-that-is-at-least-32-chars"

DOCUMENT = {
    "product": {"name": "Cotton T-shirt", "gtin": "04012345678901"},
    "materials": [{"name": "organic cotton", "share": 95}, {"name": "elastane", "share": 5}],
    "manufacturer": "Acme Textiles",
}

PASSPORT = {
    "dppId": "DPP-1",
    "tenantId": "acme",
    "productType": "textile",
    "serialNumber": "TS-0001",
    "data": DOCUMENT,
    "uri": "https://example.com/passports/DPP-1",
}


def registry_for(chain: Chain) -> RegistryClient:
    return RegistryClient(chain.w3, chain.registry.address, chain.minter, network="test", poll_interval=0)


def client_for(registry: RegistryClient) -> AsyncClient:
    return AsyncClient(transport=ASGITransport(app=create_app(registry, TOKEN)), base_url="http://test")


@pytest.fixture
async def client(chain):
    async with client_for(registry_for(chain)) as c:
        yield c


def auth(token: str = TOKEN) -> dict[str, str]:
    return {"authorization": f"Bearer {token}"}


async def create(client: AsyncClient, body: dict | None = None, **changes):
    return await client.post("/passports", json=(body or PASSPORT) | changes, headers=auth())


async def test_creates_a_passport_from_its_document_and_reads_it_back(client):
    res = await create(client)
    assert res.status_code == 201
    written = res.json()
    assert written["dppId"] == "DPP-1" and written["version"] == 1
    assert written["dataHash"] == data_hash(DOCUMENT)
    assert written["txHash"].startswith("0x") and len(written["txHash"]) == 66

    passport = (await client.get("/passports/DPP-1")).json()
    assert passport == {
        "dppId": "DPP-1",
        "tenantId": "acme",
        "productType": "textile",
        "serialNumber": "TS-0001",
        "version": 1,
        "dataHash": data_hash(DOCUMENT),
        "schemaVersion": "1.0",
        "uri": "https://example.com/passports/DPP-1",
        "status": "active",
        "issuedAt": passport["issuedAt"],
        "anchoredAt": written["anchoredAt"],
        "createdAt": written["anchoredAt"],
    }


async def test_accepts_a_precomputed_hash_for_any_product_type(client):
    body = {k: v for k, v in PASSPORT.items() if k != "data"} | {
        "dppId": "BAT-7",
        "productType": "battery",
        "dataHash": "0x" + "AB" * 32,
        "issuedAt": 1_700_000_000_000,
    }
    assert (await create(client, body)).status_code == 201
    passport = (await client.get("/passports/BAT-7")).json()
    assert (passport["productType"], passport["dataHash"], passport["issuedAt"]) == ("battery", "ab" * 32, 1_700_000_000_000)


async def test_hash_is_canonical_json(client):
    reordered = {"manufacturer": "Acme Textiles", "materials": DOCUMENT["materials"], "product": DOCUMENT["product"]}
    res = await client.post("/hash", json=reordered)
    assert res.json() == {"dataHash": data_hash(DOCUMENT)}


async def test_updates_append_versions_and_keep_history(client):
    await create(client)
    res = await client.post("/passports/DPP-1/versions", json={"status": "recalled"}, headers=auth())
    assert res.status_code == 201 and res.json()["version"] == 2

    new_document = DOCUMENT | {"repairs": ["seam 2026-09-01"]}
    res = await client.post("/passports/DPP-1/versions", json={"data": new_document}, headers=auth())
    assert res.json()["version"] == 3

    passport = (await client.get("/passports/DPP-1")).json()
    # Fields left out of an update keep their value.
    assert (passport["version"], passport["status"], passport["uri"]) == (3, "recalled", PASSPORT["uri"])
    assert passport["dataHash"] == data_hash(new_document)

    history = (await client.get("/passports/DPP-1/versions")).json()
    assert history["total"] == 3
    assert [(v["version"], v["status"]) for v in history["items"]] == [(1, "active"), (2, "recalled"), (3, "recalled")]
    page = (await client.get("/passports/DPP-1/versions?offset=2&limit=1")).json()
    assert [v["version"] for v in page["items"]] == [3]


async def test_verifies_documents_against_the_chain(client):
    await create(client)
    ok = (await client.post("/passports/DPP-1/verify", json={"data": DOCUMENT})).json()
    assert (ok["valid"], ok["matchedVersion"], ok["latestVersion"], ok["status"]) == (True, 1, 1, "active")

    tampered = DOCUMENT | {"manufacturer": "Someone Else"}
    bad = (await client.post("/passports/DPP-1/verify", json={"data": tampered})).json()
    assert (bad["valid"], bad["matchedVersion"]) == (False, None)

    await client.post("/passports/DPP-1/versions", json={"data": tampered}, headers=auth())
    outdated = (await client.post("/passports/DPP-1/verify", json={"data": DOCUMENT})).json()
    assert (outdated["valid"], outdated["matchedVersion"], outdated["latestVersion"]) == (False, 1, 2)


async def test_finds_passports_by_serial_and_by_tenant(client):
    for i in range(3):
        await create(client, dppId=f"DPP-{i}", serialNumber=f"TS-{i}")

    res = await client.get("/passports/by-serial", params={"tenantId": "acme", "productType": "textile", "serialNumber": "TS-1"})
    assert res.json()["dppId"] == "DPP-1"
    missing = await client.get("/passports/by-serial", params={"tenantId": "acme", "productType": "shoe", "serialNumber": "TS-1"})
    assert missing.status_code == 404

    page = (await client.get("/tenants/acme/passports?limit=2")).json()
    assert page["total"] == 3 and [p["dppId"] for p in page["items"]] == ["DPP-2", "DPP-1"]
    assert (await client.get("/tenants/nobody/passports")).json() == {"tenantId": "nobody", "total": 0, "items": []}


async def test_answers_409_for_duplicates(client):
    await create(client)
    res = await create(client)
    assert (res.status_code, res.json()["error"]) == (409, "ALREADY_EXISTS")
    res = await create(client, dppId="DPP-2")
    assert (res.status_code, res.json()["error"]) == (409, "SERIAL_TAKEN")


async def test_answers_400_for_invalid_input(client):
    both = await create(client, dataHash="ab" * 32)
    assert both.json() == {"error": "INVALID_INPUT", "message": "give exactly one of data or dataHash"}

    res = await create(client, data=None, dataHash="abc")
    assert res.json() == {"error": "INVALID_INPUT", "message": "dataHash: must be 64 hex chars"}

    res = await create(client, issuedAt="1700000000000")
    assert res.status_code == 400 and res.json()["message"].startswith("issuedAt:")

    res = await create(client, status="")
    assert res.status_code == 400 and res.json()["message"].startswith("status:")

    empty = await client.post("/passports/DPP-1/versions", json={}, headers=auth())
    assert empty.json() == {"error": "INVALID_INPUT", "message": "nothing to update"}

    bad_json = await client.post("/passports", content=b"{nope", headers=auth() | {"content-type": "application/json"})
    assert bad_json.json()["error"] == "INVALID_JSON"


async def test_answers_404_for_unknown_passports(client):
    assert (await client.get("/passports/nope")).json() == {"error": "NOT_FOUND", "message": "no passport nope"}
    assert (await client.post("/passports/nope/versions", json={"status": "x"}, headers=auth())).status_code == 404
    assert (await client.get("/passports/nope/versions")).status_code == 404
    assert (await client.post("/passports/nope/verify", json={"data": {}})).status_code == 404


async def test_requires_the_bearer_token_to_write(client):
    assert (await client.post("/passports", json=PASSPORT, headers=auth("wrong"))).status_code == 401
    assert (await client.post("/passports", json=PASSPORT)).status_code == 401
    assert (await client.post("/passports/DPP-1/versions", json={"status": "x"})).status_code == 401


async def test_answers_413_for_an_oversized_body(client):
    res = await create(client, data={"blob": "x" * (1024 * 1024 + 1)})
    assert (res.status_code, res.json()["error"]) == (413, "BODY_TOO_LARGE")


async def test_keeps_writing_after_a_rejected_write_and_under_concurrency(client):
    await create(client)
    assert (await create(client)).status_code == 409
    responses = await asyncio.gather(*(create(client, dppId=f"C-{i}", serialNumber=f"C-{i}") for i in range(6)))
    assert [r.status_code for r in responses] == [201] * 6


async def test_answers_500_when_the_signer_is_not_a_minter():
    async with client_for(registry_for(await deploy(minter_allowed=False))) as client:
        res = await create(client)
    assert (res.status_code, res.json()["error"]) == (500, "SIGNER_NOT_MINTER")


async def test_answers_504_with_the_tx_hash_when_a_sent_write_is_not_confirmed(chain):
    registry = registry_for(chain)

    async def timeout(tx_hash):
        raise TimeoutError

    registry._wait = timeout
    async with client_for(registry) as client:
        res = await create(client)
        assert (res.status_code, res.json()["error"]) == (504, "TX_PENDING")
        assert "0x" in res.json()["message"]
        # The transaction did land, so the passport exists.
        assert (await client.get("/passports/DPP-1")).status_code == 200


async def test_reports_health(client):
    health = (await client.get("/health")).json()
    assert health["chainId"] == 131277322940537  # eth-tester's chain id
    assert health["signerIsMinter"] is True
    assert 0 < int(health["signerBalanceWei"]) <= 10**18


async def test_preflight(chain):
    await registry_for(chain).preflight()
    with pytest.raises(RuntimeError, match="not a minter"):
        await registry_for(await deploy(minter_allowed=False)).preflight()
    wrong = RegistryClient(chain.w3, "0x" + "11" * 20, chain.minter)
    with pytest.raises(RuntimeError, match="no contract"):
        await wrong.preflight()
