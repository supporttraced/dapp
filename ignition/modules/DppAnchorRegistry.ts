import { buildModule } from "@nomicfoundation/hardhat-ignition/modules";

// Deploys the registry. By default the deployer is both the role admin and the
// minter; override with --parameters ignition/parameters/<network>.json.
// Grant MINTER_ROLE to further backend workers with grantRole after deploy.
export default buildModule("DppAnchorRegistryModule", (m) => {
  const deployer = m.getAccount(0);
  const admin = m.getParameter("admin", deployer);
  const minter = m.getParameter("minter", deployer);

  const registry = m.contract("DppAnchorRegistry", [admin, [minter]]);

  return { registry };
});
