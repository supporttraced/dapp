#!/usr/bin/env sh
#
# NAME
#   rpc-status.sh - check the Dwellir Base Sepolia endpoint and the registry
#
# USAGE
#   DWELLIR_API_KEY=... [REGISTRY_ADDRESS=0x...] ./scripts/rpc-status.sh
#
# The EVM counterpart of the Chromia dapp-status.sh: exits non-zero if the
# endpoint is unreachable, on the wrong chain, or the registry has no code.
#
[ -f .env ] && { set -a; . ./.env; set +a; }
: "${DWELLIR_API_KEY:?DWELLIR_API_KEY is not set}"
RPC_URL="https://api-base-sepolia-archive.n.dwellir.com/${DWELLIR_API_KEY}"

rpc() {
  curl -s -m 10 -X POST -H 'Content-Type: application/json' \
    --data "{\"jsonrpc\":\"2.0\",\"id\":1,\"method\":\"$1\",\"params\":$2}" "$RPC_URL" \
    | sed -n 's/.*"result":"\([^"]*\)".*/\1/p'
}

chain_id=$(rpc eth_chainId '[]')
if [ "$chain_id" != "0x14a34" ]; then
  echo "❌ Expected Base Sepolia (0x14a34 / 84532), got: ${chain_id:-no response}"
  exit 1
fi
height=$(rpc eth_blockNumber '[]')
echo "✅ Dwellir Base Sepolia reachable, block height $(printf '%d' "$height")"

if [ -n "$REGISTRY_ADDRESS" ]; then
  code=$(rpc eth_getCode "[\"$REGISTRY_ADDRESS\",\"latest\"]")
  if [ -z "$code" ] || [ "$code" = "0x" ]; then
    echo "❌ No contract code at $REGISTRY_ADDRESS"
    exit 1
  fi
  echo "✅ DppAnchorRegistry deployed at $REGISTRY_ADDRESS"
fi
