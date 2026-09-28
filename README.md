# dapp-dw

EVM port of `battery-service-dapp` (Chromia/Rell) on **Base**, reached through
**Dwellir** RPC, plus a Python **FastAPI** backend for creating DPP anchors.

| Path          | What                                                          |
| ------------- | ------------------------------------------------------------- |
| `contracts/`  | `DppAnchorRegistry` Solidity contract (Hardhat build/test/deploy) |
| `backend/`    | FastAPI + web3.py API (`Dockerfile`, tests)                   |
| `docker-compose.yml` | Runs the API                                          |

Chromia is not a Dwellir-supported network, so the Rell dapp itself can't move
to Dwellir. This project re-implements the same `battery` module as the
`DppAnchorRegistry` Solidity contract and runs it on Base Sepolia / Base mainnet.

| Rell (`battery-service-dapp`)   | This project                                  |
| ------------------------------- | --------------------------------------------- |
| `mint_dpp`                      | `POST /dpps/battery` → `mintDpp`              |
| `mint_garment_dpp`              | `POST /dpps/garment` → `mintGarmentDpp`       |
| `mint_cell_dpp`                 | `POST /dpps/cell` → `mintCellDpp`             |
| `get_dpp_anchor` (and garment/cell) | `GET /dpps/{kind}/{dppId}`                |
| `admin_addresses` module arg    | `MINTER_ROLE` (OpenZeppelin AccessControl)    |
| `dapp-status.sh`                | `npm run status`                              |

## Setup

Prerequisites: Node 24+ (contract), Docker or Python 3.12+ (API).

```shell
npm ci
npm run build          # compiles and copies the ABI to backend/app/contracts/
cp .env.example .env   # fill in DWELLIR_API_KEY, DEPLOYER_PRIVATE_KEY, API_TOKEN
npm run status         # checks the Dwellir endpoint answers as Base Sepolia (84532)
```

`npm run build` exports the compiled ABI into `backend/app/contracts/DppAnchorRegistry.json`,
so the API needs neither Node nor Hardhat. Commit that file whenever the contract changes;
CI fails if it is stale.

## Deploy

```shell
npm run deploy:base-sepolia
```

The deployer becomes the role admin and the only minter. To use separate
admin / backend worker addresses, copy `ignition/parameters/baseSepolia.example.json`
and pass it with `--parameters`.

Put the printed registry address in `.env` as `REGISTRY_ADDRESS`.

## Run the API

```shell
docker compose up --build -d
curl localhost:8080/health
```

The API is configured from `.env` (see `.env.example`): it needs `REGISTRY_ADDRESS`,
`API_TOKEN`, `DWELLIR_API_KEY` (or `RPC_URL`), and signs with `MINTER_PRIVATE_KEY`
(default `DEPLOYER_PRIVATE_KEY`), which must hold `MINTER_ROLE` and some Base ETH for gas.
Interactive docs are served at `/docs`.

Run a single API instance per minter key: the signer's nonce is tracked in-process.

Without Docker:

```shell
cd backend
python -m venv .venv && .venv/bin/pip install -r requirements-dev.txt
.venv/bin/uvicorn app.main:app --port 8080   # reads ../.env
```

### Endpoints

| Method | Path                   | Auth         | Response                                   |
| ------ | ---------------------- | ------------ | ------------------------------------------ |
| GET    | `/health`              | —            | chain id, block height, registry, signer   |
| POST   | `/dpps/battery`        | Bearer token | `201 { dppId, txHash, blockNumber, anchoredAt }` |
| POST   | `/dpps/garment`        | Bearer token | same                                       |
| POST   | `/dpps/cell`           | Bearer token | same                                       |
| GET    | `/dpps/{kind}/{dppId}` | —            | the anchor, or `404`                       |

Errors are `{ "error": CODE, "message": ... }`: `400 INVALID_INPUT` / `INVALID_JSON`,
`401 UNAUTHORIZED`, `413 BODY_TOO_LARGE`, `409 ALREADY_ANCHORED` / `SERIAL_ALREADY_ANCHORED`,
`502 CHAIN_ERROR` (RPC unreachable, invalid Dwellir key, out of gas funds).

Hashes are 64 hex chars (the Rell format), with or without `0x`; `mintedAt` is
epoch millis.

```shell
curl -X POST localhost:8080/dpps/battery \
  -H "authorization: Bearer $API_TOKEN" -H 'content-type: application/json' \
  -d '{
    "dppId": "DPP-1",
    "tenantId": "11111111-1111-1111-1111-111111111111",
    "createdByUserId": "33333333-3333-3333-3333-333333333333",
    "moduleRef": "<64 hex>",
    "serialNumber": "BAT-001",
    "dataRootHash": "<64 hex>",
    "schemaVersion": "1.0.0",
    "mintedAt": 1700000000000,
    "status": "active"
  }'
```

Garment bodies use `productRef` instead of `moduleRef`. Cell bodies use
`templateRef`, `templateKey`, `manufacturingDate` (`yyyy-MM-dd`) and `dppHash`
instead of `moduleRef` / `dataRootHash`.

Each mint is simulated before it is sent, so rejected requests cost no gas.
Sends are serialized with a locally tracked nonce, so concurrent requests are safe.

## Local development

```shell
npm test                              # contract tests on Hardhat's in-process chain
npm run typecheck
(cd backend && .venv/bin/python -m pytest)   # API tests on an in-process eth-tester chain

npm run node -- --hostname 0.0.0.0    # terminal 1: local chain
npm run deploy:local                  # terminal 2
```

then set in `.env` and run `docker compose up --build`:

```shell
RPC_URL=http://host.docker.internal:8545
REGISTRY_ADDRESS=<address printed by deploy:local>
MINTER_PRIVATE_KEY=<hardhat account #0 key>
```
