# dapp-dw

EVM port of `battery-service-dapp` (Chromia/Rell) on **Base**, reached through
**Dwellir** RPC, plus an HTTP API for creating DPP anchors.

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

Prerequisites: Node 24+.

```shell
npm ci
npm run build
cp .env.example .env   # fill in DWELLIR_API_KEY, DEPLOYER_PRIVATE_KEY, API_TOKEN
npm run status         # checks the Dwellir endpoint answers as Base Sepolia (84532)
```

## Deploy

```shell
npm run deploy:base-sepolia
```

The deployer becomes the role admin and the only minter. To use separate
admin / backend worker addresses, copy `ignition/parameters/baseSepolia.example.json`
and pass it with `--parameters`.

## Run the API

```shell
npm run api
```

The API reads `REGISTRY_ADDRESS`, or falls back to the Ignition deployment for the
connected chain. It signs with `MINTER_PRIVATE_KEY` (default `DEPLOYER_PRIVATE_KEY`),
which must hold `MINTER_ROLE` and some Base ETH for gas.

### Endpoints

| Method | Path                   | Auth         | Response                                   |
| ------ | ---------------------- | ------------ | ------------------------------------------ |
| GET    | `/health`              | —            | chain id, block height, registry, signer   |
| POST   | `/dpps/battery`        | Bearer token | `201 { dppId, txHash, blockNumber, anchoredAt }` |
| POST   | `/dpps/garment`        | Bearer token | same                                       |
| POST   | `/dpps/cell`           | Bearer token | same                                       |
| GET    | `/dpps/{kind}/{dppId}` | —            | the anchor, or `404`                       |

Errors are `{ "error": CODE, "message": ... }`: `400 INVALID_INPUT`,
`401 UNAUTHORIZED`, `409 ALREADY_ANCHORED` / `SERIAL_ALREADY_ANCHORED`,
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
npm test                 # contract + API tests on Hardhat's in-process chain
npm run typecheck

npm run node             # terminal 1: local chain
npm run deploy:local     # terminal 2
RPC_URL=http://127.0.0.1:8545 MINTER_PRIVATE_KEY=<hardhat account #0 key> npm run api
```
