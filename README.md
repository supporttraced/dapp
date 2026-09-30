# DPP API

A FastAPI service that issues and verifies **Digital Product Passports** for any kind of
product (batteries, textiles, electronics, …) by anchoring them on an EVM blockchain.

The passport document (JSON) stays with you. The API stores its SHA-256 hash, the product's
identifiers and its lifecycle status in the `DppRegistry` smart contract, so anyone can later
check that a passport is genuine and unaltered. Updates (new data, a recall, end of life)
append versions; the full history stays on-chain.

Pure Python: FastAPI + web3.py. No Node.js — the contract is compiled, deployed and tested from
Python too.

| Path | What |
| --- | --- |
| `app/` | FastAPI app (`main.py`), chain client (`registry.py`), CLI (`cli.py`) |
| `app/contracts/DppRegistry.json` | Compiled contract (ABI + bytecode), committed |
| `contracts/DppRegistry.sol` | Contract source |
| `tests/` | Contract and API tests on an in-process chain |

## Quick start (free, Hedera testnet)

1. Get a testnet account with free HBAR at https://portal.hedera.com/faucet (ECDSA key).
2. `cp .env.example .env`, then set `DEPLOYER_PRIVATE_KEY` (the HEX encoded private key) and
   `API_TOKEN` (`openssl rand -hex 32`). `NETWORK=hederaTestnet` needs no API key.
3. Deploy the registry and start the API:

   ```shell
   docker compose build
   docker compose run --rm api python -m app.cli deploy   # prints REGISTRY_ADDRESS=...
   # put REGISTRY_ADDRESS in .env
   docker compose up -d
   docker compose run --rm api python -m app.cli status    # all ✅
   ```

4. Issue a passport:

   ```shell
   curl -X POST localhost:8080/passports \
     -H "authorization: Bearer $API_TOKEN" -H 'content-type: application/json' \
     -d '{
       "dppId": "BAT-2026-0001",
       "tenantId": "acme",
       "productType": "battery",
       "serialNumber": "EB500-0001",
       "uri": "https://acme.example/dpp/BAT-2026-0001",
       "data": { "model": "EB-500", "chemistry": "NMC", "capacityWh": 500 }
     }'
   ```

   The response has the `txHash` and an `explorerUrl` to see it on HashScan. A deploy costs
   about 5 test HBAR, a write about 0.5; a write takes ~10 s.

Interactive API docs: http://localhost:8080/docs.

## API

| Method | Path | Auth | What |
| --- | --- | --- | --- |
| POST | `/passports` | token | Create a passport → `201 WriteResult` |
| POST | `/passports/batch` | token | Create up to 100, idempotently → `200 BatchWriteResult` |
| GET | `/passports/{dppId}` | — | The passport, latest version |
| POST | `/passports/{dppId}/versions` | token | Update → new version (`201 WriteResult`) |
| GET | `/passports/{dppId}/versions?offset&limit` | — | Version history, oldest first |
| POST | `/passports/{dppId}/verify` | — | Is this document the anchored one? |
| GET | `/passports/by-serial?tenantId&productType&serialNumber` | — | Find by serial number |
| GET | `/tenants/{tenantId}/passports?offset&limit` | — | A tenant's passports, newest first |
| POST | `/hash` | — | The hash that would be anchored for a document |
| GET | `/health` | — | Network, chain, registry, signer, gas balance |

**Create** takes `dppId`, `tenantId`, `productType` (any string), `serialNumber`, and the
document as `data` — or its hash as `dataHash` if you'd rather not send it. Optional:
`schemaVersion` (default `1.0`), `uri`, `status` (default `active`), `issuedAt` (epoch millis,
default now). A serial number can be used once per tenant and product type.

**Batch create** takes `{"passports": [...]}` — up to 100 of the same objects **Create**
takes — and anchors them in as few transactions as the gas budget allows. It is built for an
issuing backend with an outbox:

- **Idempotent.** A passport already on-chain *as the same passport* (same identity and
  version-1 hash) is reported, never written again, so a caller that did not hear back can
  resend the whole batch. The contract enforces this too (`createPassports`), which covers a
  transaction that lands after its response timed out.
- **Per-passport outcomes.** One conflict never blocks the rest. Every passport comes back in
  exactly one list:

  | List | Meaning | Caller should |
  | --- | --- | --- |
  | `anchored` | created by this request; has `txHash`, `blockNumber` | mark anchored |
  | `alreadyAnchored` | on-chain already as this passport | mark anchored |
  | `rejected` | `ALREADY_EXISTS` (a *different* passport has the id) or `SERIAL_TAKEN` | fail it; do not resend as-is |
  | `retryable` | `TX_PENDING` (sent, not confirmed — carries its `txHash`) or `CHAIN_ERROR` | resend later |

- **Gas-sized transactions.** A batch is split to fit `GAS_BUDGET` (default 12M). Measured on
  backend-shaped passports (UUID ids, a full public URI) a passport costs **~490k gas**, so 30
  already reach 98.8% of Hedera's 15M per-transaction cap and 40 would fail — which is why the
  split is by measured gas, not a fixed count. Each chunk is simulated first; an entry whose
  revert can be pinned on it is moved to `rejected` and the chunk re-simulated without it.
  Chunks are sent back to back and confirmed concurrently.

The whole request fails (`400`) only for a malformed batch: over 100 entries, a repeated
`dppId`, a repeated serial per tenant and product type, or an invalid field.

**Update** takes any of `data` / `dataHash`, `status`, `uri`, `schemaVersion`; fields left out
keep their value, so `{"status": "recalled"}` records a recall.

**Verify** takes `data` (or `dataHash`) and answers `valid` (matches the latest version),
`matchedVersion` (the version it matches, if an older one) and the current `status`.

The hash is SHA-256 over [RFC 8785](https://www.rfc-editor.org/rfc/rfc8785) canonical JSON
(sorted keys, no whitespace), so any party can recompute it with an RFC 8785 library.
The API does not store documents; keep them in your own system.

Errors are `{ "error": CODE, "message": ... }`: `400 INVALID_INPUT` / `INVALID_JSON`,
`401 UNAUTHORIZED`, `404 NOT_FOUND`, `409 ALREADY_EXISTS` / `SERIAL_TAKEN`,
`413 BODY_TOO_LARGE` (1 MB), `500 SIGNER_NOT_MINTER`, `502 CHAIN_ERROR`, and
`504 TX_PENDING` — the transaction was sent (hash in the message) but not confirmed in time;
it may still land, so read the passport back before retrying.

Writes are simulated before they are sent, so rejected requests cost no gas, and concurrent
writes are safe (nonces are tracked in-process).

## Networks

Set `NETWORK` in `.env`:

| `NETWORK` | Chain | RPC | Cost |
| --- | --- | --- | --- |
| `hederaTestnet` | 296 | Hashio (public, free) | free test HBAR |
| `arbitrumSepolia` | 421614 | Dwellir (`DWELLIR_API_KEY`) | free test ETH from a faucet |
| `localhost` | 31337 | `http://127.0.0.1:8545` (e.g. anvil) | — |
| `hederaMainnet` | 295 | Hashio — use a commercial relay via `RPC_URL` | real HBAR |
| `arbitrumOne` | 42161 | Dwellir | real ETH |

`RPC_URL` overrides the endpoint; the API and CLI always check that the chain ID matches
`NETWORK`.

## CLI

```shell
python -m app.cli deploy [--admin 0x...] [--minter 0x...]   # new registry (DEPLOYER_PRIVATE_KEY)
python -m app.cli minter add|remove 0x...                    # admin: grant/revoke write access
python -m app.cli status                                     # RPC, registry, signer checks
python -m app.cli compile [--check]                          # after editing the contract
```

Run it with `docker compose run --rm api python -m app.cli ...`, or locally (below).
`compile` downloads the pinned solc from binaries.soliditylang.org and verifies its checksum;
commit the regenerated `app/contracts/DppRegistry.json` (CI fails if it is stale).

## Development

```shell
python3 -m venv .venv && .venv/bin/pip install -r requirements-dev.txt
.venv/bin/python -m pytest                       # contract + API tests, in-process chain
.venv/bin/uvicorn app.main:app --reload --port 8080
```

## Production

1. **Keys.** Use a cold **admin** wallet (manages minters) and a hot **minter** wallet the API
   signs with: `deploy --admin <cold address> --minter <api address>`. Only
   `MINTER_PRIVATE_KEY` goes on the server — not the deployer or admin key.
2. **RPC.** Hashio is rate-limited and not meant for production: set `RPC_URL` to a
   commercial Hedera relay, or use a paid Dwellir plan for Arbitrum.
3. **Secrets.** Inject `.env` values from your secret manager; rotate `API_TOKEN` if leaked.
4. **Network.** Compose publishes the API on `127.0.0.1` only. Put a TLS reverse proxy in
   front and rate-limit the public `GET` routes (each is an RPC call).
5. **One instance per minter key.** For more throughput, add minters and run one instance
   each.
6. **Monitor** `/health` (fails when the RPC is down; `signerBalanceWei` shows when to top up).
   Each write logs `passport <dppId> v<n> anchored in tx ...`; logs rotate at 5 × 10 MB.

**Upgrading an existing registry.** `createPassports` and `passportsExist` are new contract
functions, and a deployed contract cannot gain functions. Deploy a new registry
(`python -m app.cli deploy`) and point `REGISTRY_ADDRESS` at it; the old one stays readable on
its old address but is not migrated.

At startup the API refuses to run if the chain ID doesn't match `NETWORK`, there is no
registry at `REGISTRY_ADDRESS`, the signer isn't a minter, or `API_TOKEN` is shorter than
32 characters. The container runs as a non-root user on a read-only filesystem with all
capabilities dropped, and gives in-flight writes 60 s to confirm on shutdown.
