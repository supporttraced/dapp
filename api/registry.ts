import { readFileSync } from "node:fs";
import {
  Contract,
  type BaseContractMethod,
  type ContractTransactionReceipt,
  type ContractTransactionResponse,
  type Signer,
} from "ethers";

const artifact = JSON.parse(
  readFileSync(
    new URL("../artifacts/contracts/DppAnchorRegistry.sol/DppAnchorRegistry.json", import.meta.url),
    "utf8",
  ),
);

/** ABI of the compiled registry. Run `npm run build` first. */
export const REGISTRY_ABI = artifact.abi;

/**
 * Dwellir endpoints for the networks the registry is deployed to. Dwellir
 * only publishes archive endpoints for Base; the API key goes in the URL path.
 */
export const NETWORKS = {
  baseSepolia: { chainId: 84532n, host: "api-base-sepolia-archive.n.dwellir.com" },
  baseMainnet: { chainId: 8453n, host: "api-base-mainnet-archive.n.dwellir.com" },
} as const;

export type NetworkName = keyof typeof NETWORKS;

export function dwellirRpcUrl(network: NetworkName, apiKey: string): string {
  return `https://${NETWORKS[network].host}/${apiKey}`;
}

/** An error with the HTTP status the API should answer with. */
export class ApiError extends Error {
  readonly status: number;
  readonly code: string;

  constructor(status: number, code: string, message: string) {
    super(message);
    this.status = status;
    this.code = code;
  }
}

type FieldType = "text" | "hash" | "millis";

/**
 * The three passport kinds, mirroring the Rell `mint_dpp`,
 * `mint_garment_dpp` and `mint_cell_dpp` operations. Field names match the
 * contract's input structs.
 */
const KINDS = {
  battery: {
    mint: "mintDpp",
    get: "getDppAnchor",
    event: "DppAnchored",
    fields: {
      dppId: "text",
      tenantId: "text",
      createdByUserId: "text",
      moduleRef: "hash",
      serialNumber: "text",
      dataRootHash: "hash",
      schemaVersion: "text",
      mintedAt: "millis",
      status: "text",
    },
  },
  garment: {
    mint: "mintGarmentDpp",
    get: "getGarmentDppAnchor",
    event: "GarmentDppAnchored",
    fields: {
      dppId: "text",
      tenantId: "text",
      createdByUserId: "text",
      productRef: "hash",
      serialNumber: "text",
      dataRootHash: "hash",
      schemaVersion: "text",
      mintedAt: "millis",
      status: "text",
    },
  },
  cell: {
    mint: "mintCellDpp",
    get: "getCellDppAnchor",
    event: "CellDppAnchored",
    fields: {
      dppId: "text",
      tenantId: "text",
      createdByUserId: "text",
      templateRef: "hash",
      templateKey: "text",
      serialNumber: "text",
      manufacturingDate: "text",
      dppHash: "hash",
      schemaVersion: "text",
      mintedAt: "millis",
      status: "text",
    },
  },
} satisfies Record<string, { mint: string; get: string; event: string; fields: Record<string, FieldType> }>;

export type DppKind = keyof typeof KINDS;

export function isDppKind(value: string): value is DppKind {
  return Object.hasOwn(KINDS, value);
}

export interface MintResult {
  dppId: string;
  txHash: string;
  blockNumber: number;
  /** Chain's view of when the anchor was committed (epoch millis). */
  anchoredAt: number;
}

/**
 * Converts a JSON request body into the contract's input struct.
 * <p>
 * Hashes are accepted as 64 hex chars — the format the Rell dapp takes —
 * with or without a `0x` prefix.
 */
export function parseInput(kind: DppKind, body: unknown): Record<string, string | bigint> {
  if (typeof body !== "object" || body === null || Array.isArray(body)) {
    throw new ApiError(400, "INVALID_INPUT", "request body must be a JSON object");
  }
  const raw = body as Record<string, unknown>;
  const input: Record<string, string | bigint> = {};

  for (const [name, type] of Object.entries(KINDS[kind].fields) as [string, FieldType][]) {
    const value = raw[name];
    if (type === "millis") {
      if (!Number.isSafeInteger(value) || (value as number) < 0) {
        throw new ApiError(400, "INVALID_INPUT", `${name} must be epoch millis (non-negative integer)`);
      }
      input[name] = BigInt(value as number);
    } else if (typeof value !== "string") {
      throw new ApiError(400, "INVALID_INPUT", `${name} must be a string`);
    } else if (type === "hash") {
      const hex = value.startsWith("0x") ? value.slice(2) : value;
      if (!/^[0-9a-fA-F]{64}$/.test(hex)) {
        throw new ApiError(400, "INVALID_INPUT", `${name} must be 64 hex chars`);
      }
      input[name] = "0x" + hex.toLowerCase();
    } else {
      input[name] = value;
    }
  }
  return input;
}

/** Converts an on-chain anchor struct into the JSON shape returned by the API. */
function toJson(kind: DppKind, anchor: Record<string, unknown>): Record<string, unknown> {
  const fields: Record<string, FieldType> = { ...KINDS[kind].fields, anchoredAt: "millis" };
  const out: Record<string, unknown> = {};
  for (const [name, type] of Object.entries(fields)) {
    const value = anchor[name];
    if (type === "millis") out[name] = Number(value);
    else if (type === "hash") out[name] = (value as string).slice(2);
    else out[name] = value;
  }
  return out;
}

/**
 * Maps a failed mint to an ApiError. Contract reverts become 4xx responses;
 * anything else (RPC down, bad API key, out of gas money) is a 502.
 */
function toApiError(contract: Contract, err: unknown): ApiError {
  const e = err as { revert?: { name: string; args: unknown[] }; data?: string; shortMessage?: string; message?: string };
  let revert = e.revert;
  if (!revert && typeof e.data === "string" && e.data.length > 2) {
    const parsed = contract.interface.parseError(e.data);
    if (parsed) revert = { name: parsed.name, args: [...parsed.args] };
  }

  switch (revert?.name) {
    case "AlreadyAnchored":
      return new ApiError(409, "ALREADY_ANCHORED", `DPP already anchored: ${revert.args[0]}`);
    case "SerialAlreadyAnchored":
      return new ApiError(409, "SERIAL_ALREADY_ANCHORED", `Serial already anchored: ${revert.args[0]}`);
    case "InvalidInput":
      return new ApiError(400, "INVALID_INPUT", String(revert.args[0]));
    case "AccessControlUnauthorizedAccount":
      return new ApiError(500, "MINTER_NOT_AUTHORIZED", "API signer does not hold MINTER_ROLE on the registry");
    default:
      return new ApiError(502, "CHAIN_ERROR", e.shortMessage ?? e.message ?? String(err));
  }
}

/** Thin client over a deployed DppAnchorRegistry. */
export class RegistryClient {
  readonly contract: Contract;
  private readonly signer: Signer;
  private readonly confirmations: number;

  /** Tail of the send queue; sends run one at a time so nonces stay in order. */
  private sendQueue: Promise<unknown> = Promise.resolve();
  /** Next nonce to use, or null to re-read it from the chain. */
  private nextNonce: number | null = null;

  constructor(address: string, signer: Signer, confirmations = 1) {
    this.contract = new Contract(address, REGISTRY_ABI, signer);
    this.signer = signer;
    this.confirmations = confirmations;
  }

  /** Validates, submits and waits for a mint transaction. */
  async mint(kind: DppKind, body: unknown): Promise<MintResult> {
    const input = parseInput(kind, body);
    const { mint, event } = KINDS[kind];
    const method = this.contract.getFunction(mint);

    let receipt: ContractTransactionReceipt | null;
    try {
      // Simulate first: a revert (duplicate, bad input) is decoded here and
      // rejected before a nonce is used or any gas is spent.
      await method.staticCall(input);
      const tx = await this.enqueueSend(method, input);
      receipt = await tx.wait(this.confirmations);
    } catch (err) {
      throw toApiError(this.contract, err);
    }

    const log = receipt!.logs
      .map((l) => this.contract.interface.parseLog(l))
      .find((l) => l?.name === event)!;

    return {
      dppId: input.dppId as string,
      txHash: receipt!.hash,
      blockNumber: receipt!.blockNumber,
      anchoredAt: Number(log.args.anchoredAt),
    };
  }

  /**
   * Sends one transaction at a time with a locally tracked nonce, so
   * concurrent requests don't collide. Only the send is serialized; waiting
   * for confirmation happens in parallel. After a failed send the nonce is
   * re-read from the chain, so a rejected transaction never leaves a gap
   * that would stall every later mint.
   */
  private enqueueSend(method: BaseContractMethod, input: Record<string, unknown>) {
    const send = async (): Promise<ContractTransactionResponse> => {
      this.nextNonce ??= await this.signer.getNonce("pending");
      try {
        const tx = await method.send(input, { nonce: this.nextNonce });
        this.nextNonce++;
        return tx;
      } catch (err) {
        this.nextNonce = null;
        throw err;
      }
    };
    const result = this.sendQueue.then(send, send);
    this.sendQueue = result.catch(() => {});
    return result;
  }

  /** Reads an anchor back from the chain. Returns null if it does not exist. */
  async get(kind: DppKind, dppId: string): Promise<Record<string, unknown> | null> {
    const anchor = await this.contract.getFunction(KINDS[kind].get)(dppId);
    if (anchor.dppId === "") return null;
    return toJson(kind, anchor.toObject());
  }
}
