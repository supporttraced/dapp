// Copy the compiled registry's ABI and bytecode into the Python backend, so it
// runs (and its Docker image builds) without Node or Hardhat. Run by
// `npm run build`; commit the result.
import { readFileSync, writeFileSync } from "node:fs";

const artifact = JSON.parse(
  readFileSync("artifacts/contracts/DppAnchorRegistry.sol/DppAnchorRegistry.json", "utf8"),
);
const { contractName, abi, bytecode } = artifact;

writeFileSync(
  "backend/app/contracts/DppAnchorRegistry.json",
  JSON.stringify({ contractName, abi, bytecode }, null, 2) + "\n",
);
