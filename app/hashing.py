"""The passport document hash anchored on-chain."""

import hashlib
from typing import Any

import rfc8785


def data_hash(document: Any) -> str:
    """
    SHA-256 of the document in RFC 8785 canonical JSON (sorted keys, no
    whitespace, normalized numbers), as 64 lowercase hex chars. Any party can
    recompute it with an RFC 8785 library to verify a passport.
    """
    return hashlib.sha256(rfc8785.dumps(document)).hexdigest()
