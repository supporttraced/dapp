"""
DPP API.

    GET  /health                    chain id, block height, registry, signer
    POST /dpps/{kind}               mint an anchor (bearer token required)
    GET  /dpps/{kind}/{dppId}       read an anchor back (public)

`kind` is `battery`, `garment` or `cell`.

Run a single worker: the signer's nonce is tracked in-process, so several
workers sharing one minter key would send colliding transactions.
"""

import hmac
import logging
from collections.abc import Awaitable, Callable
from contextlib import asynccontextmanager
from typing import Annotated, Any

from eth_account import Account
from fastapi import Depends, FastAPI, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from pydantic import BaseModel
from starlette.exceptions import HTTPException
from starlette.types import ASGIApp, Message, Receive, Scope, Send
from web3 import AsyncHTTPProvider, AsyncWeb3

from .config import Settings, load_settings
from .registry import KINDS, NETWORKS, ApiError, DppKind, RegistryClient, dwellir_rpc_url
from .schemas import ErrorBody, Health, MintResult

MAX_BODY_BYTES = 64 * 1024

log = logging.getLogger("uvicorn.error")


async def connect(settings: Settings) -> RegistryClient:
    rpc_url = settings.rpc_url
    if rpc_url is None:
        if settings.dwellir_api_key is None:
            raise RuntimeError("DWELLIR_API_KEY is not set (see .env.example)")
        rpc_url = dwellir_rpc_url(settings.network, settings.dwellir_api_key)

    w3 = AsyncWeb3(AsyncHTTPProvider(rpc_url))
    chain_id = await w3.eth.chain_id
    expected = NETWORKS[settings.network].chain_id
    if settings.rpc_url is None and chain_id != expected:
        raise RuntimeError(f"RPC returned chain {chain_id}, expected {expected} for {settings.network}")

    key = settings.minter_private_key or settings.deployer_private_key
    if key is None:
        raise RuntimeError("MINTER_PRIVATE_KEY or DEPLOYER_PRIVATE_KEY must be set (see .env.example)")
    registry = RegistryClient(w3, settings.registry_address, Account.from_key(key), settings.confirmations)
    log.info("DPP API — chain %s, registry %s, signer %s", chain_id, registry.contract.address, registry.signer.address)
    return registry


def create_app(registry: RegistryClient | None = None, api_token: str | None = None) -> FastAPI:
    """
    Builds the app. Without arguments the registry is connected at startup
    from the environment; tests pass their own.
    """

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        if registry is None:
            settings = load_settings()
            app.state.registry = await connect(settings)
            app.state.api_token = settings.api_token
        yield
        await app.state.registry.w3.provider.disconnect()

    app = FastAPI(
        title="DPP API",
        summary="Anchors Digital Product Passports in the DppAnchorRegistry on Base.",
        lifespan=lifespan,
        responses={"4XX": {"model": ErrorBody}, "5XX": {"model": ErrorBody}},
    )
    app.state.registry = registry
    app.state.api_token = api_token
    app.add_middleware(BodySizeLimit, max_bytes=MAX_BODY_BYTES)
    app.add_exception_handler(HTTPException, _http_error)
    app.add_exception_handler(RequestValidationError, _validation_error)
    app.add_exception_handler(Exception, _internal_error)

    @app.get("/health", response_model=Health)
    async def health(registry: RegistryDep):
        return await registry.health()

    for kind, spec in KINDS.items():
        app.post(
            f"/dpps/{kind}",
            status_code=201,
            response_model=MintResult,
            dependencies=[Depends(require_token)],
            summary=f"Mint a {kind} DPP anchor",
        )(_mint_route(kind, spec.input))
        app.get(
            f"/dpps/{kind}/{{dpp_id}}",
            response_model=spec.anchor,
            summary=f"Read a {kind} DPP anchor",
        )(_get_route(kind))

    return app


def _registry(request: Request) -> RegistryClient:
    return request.app.state.registry


RegistryDep = Annotated[RegistryClient, Depends(_registry)]

_bearer = HTTPBearer(auto_error=False)


def require_token(
    request: Request,
    credentials: Annotated[HTTPAuthorizationCredentials | None, Depends(_bearer)],
) -> None:
    """Mint endpoints spend the minter wallet's gas, so they require the API token."""
    expected = request.app.state.api_token.encode()
    if credentials is None or not hmac.compare_digest(credentials.credentials.encode(), expected):
        raise ApiError(401, "UNAUTHORIZED", "missing or invalid bearer token")


def _mint_route(kind: DppKind, model: type[BaseModel]) -> Callable[..., Awaitable[MintResult]]:
    async def mint(body: model, registry: RegistryDep) -> MintResult:  # type: ignore[valid-type]
        return await registry.mint(kind, body)

    return mint


def _get_route(kind: DppKind) -> Callable[..., Awaitable[dict[str, Any]]]:
    async def get(dpp_id: str, registry: RegistryDep) -> dict[str, Any]:
        anchor = await registry.get(kind, dpp_id)
        if anchor is None:
            raise ApiError(404, "NOT_FOUND", f"DPP not anchored: {dpp_id}")
        return anchor

    return get


# Errors are always `{ "error": CODE, "message": ... }`.

_STATUS_CODES = {404: "NOT_FOUND", 405: "METHOD_NOT_ALLOWED"}


def _error(status: int, code: str, message: str) -> JSONResponse:
    return JSONResponse({"error": code, "message": message}, status_code=status)


async def _http_error(request: Request, exc: HTTPException) -> JSONResponse:
    if isinstance(exc, ApiError):
        return _error(exc.status_code, exc.code, exc.detail)
    if exc.status_code == 404:
        return _error(404, "NOT_FOUND", "no such route")
    return _error(exc.status_code, _STATUS_CODES.get(exc.status_code, "HTTP_ERROR"), str(exc.detail))


async def _validation_error(request: Request, exc: RequestValidationError) -> JSONResponse:
    first = exc.errors()[0]
    if first["type"] == "json_invalid":
        return _error(400, "INVALID_JSON", "request body is not valid JSON")
    field = ".".join(str(part) for part in first["loc"][1:])
    return _error(400, "INVALID_INPUT", f"{field}: {first['msg']}" if field else first["msg"])


async def _internal_error(request: Request, exc: Exception) -> JSONResponse:
    log.error("unhandled error", exc_info=exc)
    return _error(500, "INTERNAL_ERROR", "internal error")


class BodySizeLimit:
    """Rejects request bodies over `max_bytes` with 413, however they are sent."""

    def __init__(self, app: ASGIApp, max_bytes: int):
        self.app = app
        self.max_bytes = max_bytes

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            return await self.app(scope, receive, send)
        received = 0

        async def limited_receive() -> Message:
            nonlocal received
            message = await receive()
            if message["type"] == "http.request":
                received += len(message.get("body", b""))
                if received > self.max_bytes:
                    # FastAPI re-raises HTTPExceptions from body parsing, so this
                    # reaches _http_error.
                    raise ApiError(413, "BODY_TOO_LARGE", "request body too large")
            return message

        await self.app(scope, limited_receive, send)


app = create_app()
