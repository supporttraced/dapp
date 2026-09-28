import { expect } from "chai";
import { network } from "hardhat";

const { ethers } = await network.create();

const HASH_A = "0x" + "a".repeat(64);
const HASH_B = "0x" + "b".repeat(64);
const ZERO_HASH = ethers.ZeroHash;
const TENANT = "11111111-1111-1111-1111-111111111111";
const OTHER_TENANT = "22222222-2222-2222-2222-222222222222";
const USER = "33333333-3333-3333-3333-333333333333";

function dppInput(overrides: Record<string, unknown> = {}) {
  return {
    dppId: "DPP-1",
    tenantId: TENANT,
    createdByUserId: USER,
    moduleRef: HASH_A,
    serialNumber: "SN-1",
    dataRootHash: HASH_B,
    schemaVersion: "1.0.0",
    mintedAt: 1_700_000_000_000n,
    status: "active",
    ...overrides,
  };
}

function garmentInput(overrides: Record<string, unknown> = {}) {
  return {
    dppId: "G-1",
    tenantId: TENANT,
    createdByUserId: USER,
    productRef: HASH_A,
    serialNumber: "GSN-1",
    dataRootHash: HASH_B,
    schemaVersion: "1.0.0",
    mintedAt: 1_700_000_000_000n,
    status: "active",
    ...overrides,
  };
}

function cellInput(overrides: Record<string, unknown> = {}) {
  return {
    dppId: "CELL-DPP-1",
    tenantId: TENANT,
    createdByUserId: USER,
    templateRef: HASH_A,
    templateKey: "tpl-1",
    serialNumber: "CELL-001",
    manufacturingDate: "2026-09-28",
    dppHash: HASH_B,
    schemaVersion: "1.0.0",
    mintedAt: 1_700_000_000_000n,
    status: "active",
    ...overrides,
  };
}

async function deploy() {
  const [admin, minter, stranger] = await ethers.getSigners();
  const registry = await ethers.deployContract("DppAnchorRegistry", [admin.address, [minter.address]]);
  return { registry, admin, minter, stranger, asMinter: registry.connect(minter) };
}

describe("DppAnchorRegistry", function () {
  describe("access control", function () {
    it("rejects mints from accounts without MINTER_ROLE", async function () {
      const { registry, stranger } = await deploy();
      await expect(registry.connect(stranger).mintDpp(dppInput()))
        .to.be.revertedWithCustomError(registry, "AccessControlUnauthorizedAccount");
    });

    it("lets the admin grant MINTER_ROLE to a new worker", async function () {
      const { registry, admin, stranger } = await deploy();
      await registry.connect(admin).grantRole(await registry.MINTER_ROLE(), stranger.address);
      await expect(registry.connect(stranger).mintDpp(dppInput())).to.emit(registry, "DppAnchored");
    });
  });

  describe("battery DPP", function () {
    it("stores the anchor with the chain's anchoredAt in millis", async function () {
      const { registry, asMinter } = await deploy();
      const tx = await asMinter.mintDpp(dppInput());
      const block = await ethers.provider.getBlock((await tx.wait())!.blockNumber);

      const a = await registry.getDppAnchor("DPP-1");
      expect(a.dppId).to.equal("DPP-1");
      expect(a.tenantId).to.equal(TENANT);
      expect(a.moduleRef).to.equal(HASH_A);
      expect(a.dataRootHash).to.equal(HASH_B);
      expect(a.mintedAt).to.equal(1_700_000_000_000n);
      expect(a.anchoredAt).to.equal(BigInt(block!.timestamp) * 1000n);
      expect(a.status).to.equal("active");
      expect(await registry.dppExists("DPP-1")).to.equal(true);
    });

    it("returns an empty anchor for unknown ids", async function () {
      const { registry } = await deploy();
      expect((await registry.getDppAnchor("nope")).dppId).to.equal("");
      expect(await registry.dppExists("nope")).to.equal(false);
    });

    it("rejects duplicate dppId and duplicate serial", async function () {
      const { registry, asMinter } = await deploy();
      await asMinter.mintDpp(dppInput());
      await expect(asMinter.mintDpp(dppInput({ serialNumber: "SN-2" })))
        .to.be.revertedWithCustomError(registry, "AlreadyAnchored").withArgs("DPP-1");
      await expect(asMinter.mintDpp(dppInput({ dppId: "DPP-2", tenantId: OTHER_TENANT })))
        .to.be.revertedWithCustomError(registry, "SerialAlreadyAnchored").withArgs("SN-1");
    });

    it("validates input", async function () {
      const { registry, asMinter } = await deploy();
      const cases: [Record<string, unknown>, string][] = [
        [{ dppId: "" }, "dppId must not be empty"],
        [{ dataRootHash: ZERO_HASH }, "dataRootHash must not be zero"],
        [{ moduleRef: ZERO_HASH }, "moduleRef must not be zero"],
        [{ serialNumber: "" }, "serialNumber must not be empty"],
        [{ status: "broken" }, "status must be one of: active, recalled, recycled"],
      ];
      for (const [overrides, reason] of cases) {
        await expect(asMinter.mintDpp(dppInput(overrides)))
          .to.be.revertedWithCustomError(registry, "InvalidInput").withArgs(reason);
      }
    });

    it("pages a tenant's anchors newest first", async function () {
      const { registry, asMinter } = await deploy();
      for (let i = 1; i <= 3; i++) {
        await asMinter.mintDpp(dppInput({ dppId: `DPP-${i}`, serialNumber: `SN-${i}` }));
      }
      await asMinter.mintDpp(dppInput({ dppId: "X", serialNumber: "X", tenantId: OTHER_TENANT }));

      expect(await registry.getDppAnchorCountByTenant(TENANT)).to.equal(3n);
      const all = await registry.getDppAnchorsByTenant(TENANT, 0, 10);
      expect(all.map((a) => a.dppId)).to.deep.equal(["DPP-3", "DPP-2", "DPP-1"]);
      const page2 = await registry.getDppAnchorsByTenant(TENANT, 2, 2);
      expect(page2.map((a) => a.dppId)).to.deep.equal(["DPP-1"]);
      expect(await registry.getDppAnchorsByTenant(TENANT, 5, 2)).to.have.length(0);
    });
  });

  describe("garment DPP", function () {
    it("mints, looks up, and rejects duplicates", async function () {
      const { registry, asMinter } = await deploy();
      await expect(asMinter.mintGarmentDpp(garmentInput())).to.emit(registry, "GarmentDppAnchored");

      const a = await registry.getGarmentDppAnchor("G-1");
      expect(a.productRef).to.equal(HASH_A);
      expect(await registry.garmentDppExists("G-1")).to.equal(true);
      expect(await registry.getGarmentDppAnchorsByTenant(TENANT, 0, 10)).to.have.length(1);

      await expect(asMinter.mintGarmentDpp(garmentInput()))
        .to.be.revertedWithCustomError(registry, "AlreadyAnchored");
      await expect(asMinter.mintGarmentDpp(garmentInput({ dppId: "G-2" })))
        .to.be.revertedWithCustomError(registry, "SerialAlreadyAnchored");
    });

    it("keeps battery and garment namespaces separate", async function () {
      const { registry, asMinter } = await deploy();
      await asMinter.mintDpp(dppInput({ dppId: "SAME", serialNumber: "SAME" }));
      await asMinter.mintGarmentDpp(garmentInput({ dppId: "SAME", serialNumber: "SAME" }));
      expect(await registry.garmentDppExists("SAME")).to.equal(true);
    });
  });

  describe("cell DPP", function () {
    it("scopes serial uniqueness to the tenant", async function () {
      const { registry, asMinter } = await deploy();
      await asMinter.mintCellDpp(cellInput());
      // Another supplier may reuse the same serial.
      await asMinter.mintCellDpp(cellInput({ dppId: "CELL-DPP-2", tenantId: OTHER_TENANT }));
      // The same supplier may not.
      await expect(asMinter.mintCellDpp(cellInput({ dppId: "CELL-DPP-3" })))
        .to.be.revertedWithCustomError(registry, "SerialAlreadyAnchored").withArgs("CELL-001");
    });

    it("finds a cell by tenant + serial (damaged QR recovery)", async function () {
      const { registry, asMinter } = await deploy();
      await asMinter.mintCellDpp(cellInput());
      const a = await registry.getCellDppAnchorBySerial(TENANT, "CELL-001");
      expect(a.dppId).to.equal("CELL-DPP-1");
      expect(a.manufacturingDate).to.equal("2026-09-28");
      expect((await registry.getCellDppAnchorBySerial(OTHER_TENANT, "CELL-001")).dppId).to.equal("");
    });

    it("lists cells by tenant and by template key", async function () {
      const { registry, asMinter } = await deploy();
      await asMinter.mintCellDpp(cellInput());
      await asMinter.mintCellDpp(cellInput({ dppId: "C2", serialNumber: "CELL-002" }));
      await asMinter.mintCellDpp(cellInput({ dppId: "C3", serialNumber: "CELL-003", templateKey: "tpl-2" }));

      const byTenant = await registry.getCellDppAnchorsByTenant(TENANT, 0, 10);
      expect(byTenant.map((a) => a.dppId)).to.deep.equal(["C3", "C2", "CELL-DPP-1"]);
      const byTemplate = await registry.getCellDppAnchorsByTemplateKey("tpl-1", 0, 10);
      expect(byTemplate.map((a) => a.dppId)).to.deep.equal(["C2", "CELL-DPP-1"]);
      expect(await registry.getCellDppAnchorCountByTemplateKey("tpl-2")).to.equal(1n);
    });

    it("validates input", async function () {
      const { registry, asMinter } = await deploy();
      await expect(asMinter.mintCellDpp(cellInput({ manufacturingDate: "2026-9-28" })))
        .to.be.revertedWithCustomError(registry, "InvalidInput")
        .withArgs("manufacturingDate must be ISO-8601 yyyy-MM-dd");
      await expect(asMinter.mintCellDpp(cellInput({ dppHash: ZERO_HASH })))
        .to.be.revertedWithCustomError(registry, "InvalidInput").withArgs("dppHash must not be zero");
    });
  });
});
