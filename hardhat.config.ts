import { existsSync } from "node:fs";
import hardhatToolboxMochaEthersPlugin from "@nomicfoundation/hardhat-toolbox-mocha-ethers";
import { configVariable, defineConfig } from "hardhat/config";

// Pick up DWELLIR_API_KEY / DEPLOYER_PRIVATE_KEY from a local .env (gitignored).
if (existsSync(".env")) process.loadEnvFile(".env");

// Dwellir endpoints put the API key in the URL path. Set DWELLIR_API_KEY and
// DEPLOYER_PRIVATE_KEY as env vars, or store them encrypted with
// `npx hardhat keystore set <NAME>`.
export default defineConfig({
  plugins: [hardhatToolboxMochaEthersPlugin],
  solidity: {
    profiles: {
      default: {
        version: "0.8.34",
        settings: {
          // Pin below the compiler default (osaka) so bytecode runs on any
          // current EVM chain, Arbitrum included.
          evmVersion: "prague",
          optimizer: {
            enabled: true,
            runs: 200,
          },
        },
      },
    },
  },
  networks: {
    arbitrumSepolia: {
      type: "http",
      chainId: 421614,
      url: configVariable("DWELLIR_API_KEY", {
        format: "https://api-arbitrum-sepolia.n.dwellir.com/{variable}",
      }),
      accounts: [configVariable("DEPLOYER_PRIVATE_KEY")],
    },
    arbitrumOne: {
      type: "http",
      chainId: 42161,
      url: configVariable("DWELLIR_API_KEY", {
        format: "https://api-arbitrum-mainnet-archive.n.dwellir.com/{variable}",
      }),
      accounts: [configVariable("DEPLOYER_PRIVATE_KEY")],
    },
  },
});
