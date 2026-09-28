"""
Compiles contracts/DppRegistry.sol into app/contracts/DppRegistry.json.

The pinned solc release is downloaded from binaries.soliditylang.org, checked
against its published SHA-256 and cached, so no Node.js or system solc is
needed. Only development uses this; the API reads the committed JSON.
"""

import hashlib
import json
import os
import ssl
import subprocess
import sys
import urllib.request
from pathlib import Path

import certifi

SOLC_VERSION = "0.8.37"
# Cancun is supported by every target chain (Arbitrum, Hedera) and eth-tester.
EVM_VERSION = "cancun"

ROOT = Path(__file__).resolve().parent.parent
SOURCE = "contracts/DppRegistry.sol"
ARTIFACT = Path(__file__).resolve().parent / "contracts" / "DppRegistry.json"

_PLATFORMS = {"darwin": "macosx-amd64", "linux": "linux-amd64"}


def solc_binary() -> Path:
    """The pinned solc, downloaded and verified on first use."""
    cache = Path(os.environ.get("XDG_CACHE_HOME", Path.home() / ".cache")) / "dpp-registry" / f"solc-{SOLC_VERSION}"
    if cache.exists():
        return cache
    platform = _PLATFORMS.get(sys.platform)
    if platform is None:
        raise RuntimeError(f"no solc binary published for {sys.platform}; compile on macOS or Linux (x86-64)")

    base = f"https://binaries.soliditylang.org/{platform}"
    builds = json.loads(_download(f"{base}/list.json"))["builds"]
    build = next((b for b in builds if b["version"] == SOLC_VERSION), None)
    if build is None:
        raise RuntimeError(f"solc {SOLC_VERSION} is not published for {platform}")
    binary = _download(f"{base}/{build['path']}")
    if "0x" + hashlib.sha256(binary).hexdigest() != build["sha256"]:
        raise RuntimeError("downloaded solc does not match its published SHA-256")

    cache.parent.mkdir(parents=True, exist_ok=True)
    tmp = cache.with_suffix(".tmp")
    tmp.write_bytes(binary)
    tmp.chmod(0o755)
    tmp.replace(cache)
    return cache


def _download(url: str) -> bytes:
    # The host's CDN rejects Python's default User-Agent.
    request = urllib.request.Request(url, headers={"User-Agent": "dpp-registry-compiler"})
    with urllib.request.urlopen(request, context=ssl.create_default_context(cafile=certifi.where())) as r:
        return r.read()


def compile_registry() -> dict:
    """Compiles the registry and returns the artifact written by `compile`."""
    standard_input = {
        "language": "Solidity",
        "sources": {SOURCE: {"content": (ROOT / SOURCE).read_text()}},
        "settings": {
            "optimizer": {"enabled": True, "runs": 200},
            "viaIR": True,
            "evmVersion": EVM_VERSION,
            "outputSelection": {"*": {"DppRegistry": ["abi", "evm.bytecode.object"]}},
        },
    }
    result = subprocess.run(
        [str(solc_binary()), "--standard-json"],
        input=json.dumps(standard_input),
        capture_output=True,
        text=True,
        check=True,
    )
    output = json.loads(result.stdout)
    errors = [e for e in output.get("errors", []) if e["severity"] == "error"]
    if errors:
        raise RuntimeError("\n".join(e["formattedMessage"] for e in errors))

    contract = output["contracts"][SOURCE]["DppRegistry"]
    return {
        "contractName": "DppRegistry",
        "compiler": f"solc {SOLC_VERSION}, evm {EVM_VERSION}, optimizer 200 runs, via-IR",
        "abi": contract["abi"],
        "bytecode": "0x" + contract["evm"]["bytecode"]["object"],
    }


def serialize(artifact: dict) -> str:
    return json.dumps(artifact, indent=2) + "\n"
