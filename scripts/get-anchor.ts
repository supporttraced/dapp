// Look up an anchor and print it — the verifier's view of the chain.
//
// USAGE
//   REGISTRY_ADDRESS=0x... DPP_ID=... [DPP_KIND=battery|garment|cell] \
//     npx hardhat run scripts/get-anchor.ts --network baseSepolia
import type { Result } from "ethers";
import { network } from "hardhat";

const address = process.env.REGISTRY_ADDRESS;
const dppId = process.env.DPP_ID;
const kind = process.env.DPP_KIND ?? "battery";
if (!address || !dppId) {
  throw new Error("Set REGISTRY_ADDRESS and DPP_ID");
}

const { ethers } = await network.create();
const registry = await ethers.getContractAt("DppAnchorRegistry", address);

const anchor =
  kind === "garment"
    ? await registry.getGarmentDppAnchor(dppId)
    : kind === "cell"
      ? await registry.getCellDppAnchor(dppId)
      : await registry.getDppAnchor(dppId);

if (anchor.dppId === "") {
  console.log(`No ${kind} DPP anchored with id ${dppId}`);
} else {
  console.log((anchor as unknown as Result).toObject());
}
