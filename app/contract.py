"""The compiled DppRegistry, as committed in app/contracts/DppRegistry.json."""

import json
from pathlib import Path
from typing import Any

from eth_abi import decode
from eth_utils import abi_to_signature, keccak

_ARTIFACT = json.loads((Path(__file__).parent / "contracts" / "DppRegistry.json").read_text())

ABI: list[dict[str, Any]] = _ARTIFACT["abi"]
BYTECODE: str = _ARTIFACT["bytecode"]

_ERRORS = {"0x" + keccak(text=abi_to_signature(e))[:4].hex(): e for e in ABI if e["type"] == "error"}


def decode_error(data: str) -> tuple[str, tuple[Any, ...]] | None:
    """Decodes revert data into (error name, arguments), if it is one of the registry's errors."""
    abi = _ERRORS.get(data[:10])
    if abi is None:
        return None
    return abi["name"], tuple(decode([i["type"] for i in abi["inputs"]], bytes.fromhex(data[10:])))
