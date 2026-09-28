import type { AddressInfo } from "node:net";
import type { Server } from "node:http";
import { expect } from "chai";
import { network } from "hardhat";
import { RegistryClient } from "../api/registry.ts";
import { createServer } from "../api/server.ts";

const { ethers } = await network.create();

const TOKEN = "test-token";
const HASH_A = "a".repeat(64);
const HASH_B = "0x" + "B".repeat(64);

const battery = {
  dppId: "DPP-1",
  tenantId: "tenant-1",
  createdByUserId: "user-1",
  moduleRef: HASH_A,
  serialNumber: "BAT-001",
  dataRootHash: HASH_B,
  schemaVersion: "1.0.0",
  mintedAt: 1_700_000_000_000,
  status: "active",
};

describe("DPP API", function () {
  let server: Server;
  let baseUrl: string;

  beforeEach(async function () {
    const [admin, minter] = await ethers.getSigners();
    const contract = await ethers.deployContract("DppAnchorRegistry", [admin.address, [minter.address]]);
    const registry = new RegistryClient(await contract.getAddress(), minter);
    server = createServer(registry, TOKEN).listen(0);
    await new Promise((resolve) => server.once("listening", resolve));
    baseUrl = `http://127.0.0.1:${(server.address() as AddressInfo).port}`;
  });

  afterEach(function () {
    server.close();
  });

  function post(path: string, body: unknown, token = TOKEN) {
    return fetch(baseUrl + path, {
      method: "POST",
      headers: { "content-type": "application/json", authorization: `Bearer ${token}` },
      body: JSON.stringify(body),
    });
  }

  it("mints a battery DPP and reads it back", async function () {
    const res = await post("/dpps/battery", battery);
    expect(res.status).to.equal(201);
    const minted = (await res.json()) as any;
    expect(minted.dppId).to.equal("DPP-1");
    expect(minted.txHash).to.match(/^0x[0-9a-f]{64}$/);

    const read = await fetch(`${baseUrl}/dpps/battery/DPP-1`);
    expect(read.status).to.equal(200);
    expect((await read.json()) as any).to.deep.equal({
      ...battery,
      dataRootHash: "b".repeat(64),
      anchoredAt: minted.anchoredAt,
    });
  });

  it("mints garment and cell DPPs", async function () {
    const { moduleRef, ...rest } = battery;
    expect((await post("/dpps/garment", { ...rest, productRef: moduleRef })).status).to.equal(201);

    const cell = {
      ...rest,
      dppId: "CELL-DPP-1",
      templateRef: HASH_A,
      templateKey: "template-1",
      manufacturingDate: "2026-09-01",
      dppHash: HASH_A,
    };
    delete (cell as Record<string, unknown>).dataRootHash;
    expect((await post("/dpps/cell", cell)).status).to.equal(201);
    expect(((await (await fetch(`${baseUrl}/dpps/cell/CELL-DPP-1`)).json()) as any).templateKey).to.equal("template-1");
  });

  it("answers 409 for a duplicate DPP", async function () {
    await post("/dpps/battery", battery);
    const res = await post("/dpps/battery", battery);
    expect(res.status).to.equal(409);
    expect(((await res.json()) as any).error).to.equal("ALREADY_ANCHORED");
  });

  it("answers 400 for invalid input before touching the chain", async function () {
    const res = await post("/dpps/battery", { ...battery, dataRootHash: "abc" });
    expect(res.status).to.equal(400);
    expect(((await res.json()) as any).message).to.equal("dataRootHash must be 64 hex chars");
  });

  it("answers 400 for a contract validation failure", async function () {
    const res = await post("/dpps/battery", { ...battery, status: "burned" });
    expect(res.status).to.equal(400);
    expect(((await res.json()) as any).error).to.equal("INVALID_INPUT");
  });

  it("keeps minting after a rejected mint and under concurrency", async function () {
    await post("/dpps/battery", battery);
    expect((await post("/dpps/battery", battery)).status).to.equal(409);

    const statuses = await Promise.all(
      [2, 3, 4, 5, 6].map(async (i) =>
        (await post("/dpps/battery", { ...battery, dppId: `DPP-${i}`, serialNumber: `BAT-00${i}` })).status,
      ),
    );
    expect(statuses).to.deep.equal([201, 201, 201, 201, 201]);
  });

  it("requires the bearer token to mint", async function () {
    expect((await post("/dpps/battery", battery, "wrong")).status).to.equal(401);
  });

  it("answers 404 for unknown DPPs and kinds", async function () {
    expect((await fetch(`${baseUrl}/dpps/battery/nope`)).status).to.equal(404);
    expect((await post("/dpps/shoe", battery)).status).to.equal(404);
  });

  it("reports chain health", async function () {
    const health = (await (await fetch(`${baseUrl}/health`)).json()) as any;
    expect(health.chainId).to.equal(31337);
    expect(health.registry).to.match(/^0x[0-9a-fA-F]{40}$/);
  });
});
