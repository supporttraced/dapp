import { timingSafeEqual } from "node:crypto";
import { existsSync, readFileSync } from "node:fs";
import { createServer as createHttpServer, type IncomingMessage, type Server, type ServerResponse } from "node:http";
import { JsonRpcProvider, Wallet } from "ethers";
import {
  ApiError,
  NETWORKS,
  RegistryClient,
  dwellirRpcUrl,
  isDppKind,
  type NetworkName,
} from "./registry.ts";

const MAX_BODY_BYTES = 64 * 1024;

/**
 * DPP API.
 * <p>
 *   GET  /health                    chain id, block height, registry, signer
 *   POST /dpps/{kind}               mint an anchor (bearer token required)
 *   GET  /dpps/{kind}/{dppId}       read an anchor back (public)
 * <p>
 * `kind` is `battery`, `garment` or `cell`.
 */
export function createServer(registry: RegistryClient, apiToken: string): Server {
  return createHttpServer(async (req, res) => {
    try {
      await route(registry, apiToken, req, res);
    } catch (err) {
      const e = err instanceof ApiError ? err : new ApiError(500, "INTERNAL_ERROR", "internal error");
      if (!(err instanceof ApiError)) console.error(err);
      send(res, e.status, { error: e.code, message: e.message });
    }
  });
}

async function route(registry: RegistryClient, apiToken: string, req: IncomingMessage, res: ServerResponse) {
  const path = new URL(req.url ?? "/", "http://localhost").pathname;
  const parts = path.split("/").filter(Boolean).map(decodeURIComponent);

  if (req.method === "GET" && path === "/health") {
    const provider = registry.contract.runner!.provider!;
    const [network, blockNumber] = await Promise.all([provider.getNetwork(), provider.getBlockNumber()]);
    return send(res, 200, {
      chainId: Number(network.chainId),
      blockNumber,
      registry: await registry.contract.getAddress(),
      signer: await (registry.contract.runner as Wallet).getAddress(),
    });
  }

  if (parts[0] !== "dpps" || !parts[1]) throw new ApiError(404, "NOT_FOUND", "no such route");
  if (!isDppKind(parts[1])) throw new ApiError(404, "NOT_FOUND", `unknown DPP kind: ${parts[1]}`);
  const kind = parts[1];

  if (req.method === "POST" && parts.length === 2) {
    requireToken(req, apiToken);
    const result = await registry.mint(kind, await readJson(req));
    return send(res, 201, result);
  }

  if (req.method === "GET" && parts.length === 3) {
    const anchor = await registry.get(kind, parts[2]);
    if (!anchor) throw new ApiError(404, "NOT_FOUND", `DPP not anchored: ${parts[2]}`);
    return send(res, 200, anchor);
  }

  throw new ApiError(404, "NOT_FOUND", "no such route");
}

/** Mint endpoints spend the minter wallet's gas, so they require the API token. */
function requireToken(req: IncomingMessage, apiToken: string) {
  const given = Buffer.from(req.headers.authorization ?? "");
  const expected = Buffer.from(`Bearer ${apiToken}`);
  if (given.length !== expected.length || !timingSafeEqual(given, expected)) {
    throw new ApiError(401, "UNAUTHORIZED", "missing or invalid bearer token");
  }
}

async function readJson(req: IncomingMessage): Promise<unknown> {
  let size = 0;
  const chunks: Buffer[] = [];
  for await (const chunk of req) {
    size += chunk.length;
    if (size > MAX_BODY_BYTES) throw new ApiError(413, "BODY_TOO_LARGE", "request body too large");
    chunks.push(chunk);
  }
  try {
    return JSON.parse(Buffer.concat(chunks).toString("utf8"));
  } catch {
    throw new ApiError(400, "INVALID_JSON", "request body is not valid JSON");
  }
}

function send(res: ServerResponse, status: number, body: unknown) {
  res.writeHead(status, { "content-type": "application/json" });
  res.end(JSON.stringify(body));
}

function requireEnv(name: string): string {
  const value = process.env[name];
  if (!value) throw new Error(`${name} is not set (see .env.example)`);
  return value;
}

/** Falls back to the address Ignition recorded when REGISTRY_ADDRESS is unset. */
function registryAddress(chainId: bigint): string {
  if (process.env.REGISTRY_ADDRESS) return process.env.REGISTRY_ADDRESS;
  const file = new URL(`../ignition/deployments/chain-${chainId}/deployed_addresses.json`, import.meta.url);
  if (existsSync(file)) {
    const address = JSON.parse(readFileSync(file, "utf8"))["DppAnchorRegistryModule#DppAnchorRegistry"];
    if (address) return address;
  }
  throw new Error("REGISTRY_ADDRESS is not set and no Ignition deployment was found");
}

async function main() {
  const network = (process.env.NETWORK ?? "baseSepolia") as NetworkName;
  if (!Object.hasOwn(NETWORKS, network)) throw new Error(`unknown NETWORK: ${network}`);

  // RPC_URL overrides Dwellir, e.g. http://127.0.0.1:8545 for `npm run node`.
  const rpcUrl = process.env.RPC_URL ?? dwellirRpcUrl(network, requireEnv("DWELLIR_API_KEY"));
  const provider = new JsonRpcProvider(rpcUrl, undefined, { staticNetwork: true });
  const { chainId } = await provider.getNetwork();
  if (!process.env.RPC_URL && chainId !== NETWORKS[network].chainId) {
    throw new Error(`RPC returned chain ${chainId}, expected ${NETWORKS[network].chainId} for ${network}`);
  }

  const signer = new Wallet(process.env.MINTER_PRIVATE_KEY ?? requireEnv("DEPLOYER_PRIVATE_KEY"), provider);
  const registry = new RegistryClient(
    registryAddress(chainId),
    signer,
    Number(process.env.CONFIRMATIONS ?? 1),
  );

  const port = Number(process.env.PORT ?? 8080);
  createServer(registry, requireEnv("API_TOKEN")).listen(port, () => {
    console.log(`DPP API on :${port} — chain ${chainId}, registry ${registry.contract.target}`);
  });
}

if (import.meta.main) {
  main().catch((err) => {
    console.error(err.message ?? err);
    process.exit(1);
  });
}
