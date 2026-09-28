import ast
from typing import Any

from eth_tester.exceptions import TransactionFailed
from web3.providers.eth_tester import AsyncEthereumTesterProvider
from web3.providers.eth_tester.defaults import API_ENDPOINTS


class _Reverted(Exception):
    def __init__(self, data: bytes):
        self.data = data


def _raw_revert(delegator):
    def call(tester, params):
        try:
            return delegator(tester, params)
        except TransactionFailed as e:
            data = e.args[0].args[0] if isinstance(e.args[0], Exception) else e.args[0]
            # py-evm passes the revert data as the repr of its bytes.
            if isinstance(data, str) and data.startswith(("b'", 'b"')):
                data = ast.literal_eval(data)
            if not isinstance(data, bytes):
                raise
            raise _Reverted(data) from e

    return call


class NodeLikeTesterProvider(AsyncEthereumTesterProvider):
    """
    eth-tester, but reverts come back the way a JSON-RPC node (geth, reth, the
    Dwellir endpoints) reports them — `{code: 3, data: "0x<revert data>"}` —
    so tests go through web3's real revert parsing.
    """

    def __init__(self):
        super().__init__()
        eth = dict(API_ENDPOINTS["eth"])
        for endpoint in ("call", "estimateGas"):
            eth[endpoint] = _raw_revert(eth[endpoint])
        self.api_endpoints = {**API_ENDPOINTS, "eth": eth}

    async def make_request(self, method: str, params: Any) -> dict[str, Any]:
        try:
            return await super().make_request(method, params)
        except _Reverted as r:
            error = {"code": 3, "message": "execution reverted", "data": "0x" + r.data.hex()}
            return {"jsonrpc": "2.0", "id": self._current_request_id, "error": error}
