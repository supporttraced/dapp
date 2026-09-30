"""
Digital Product Passport API.

    GET  /health                                  network, chain, registry, signer and its gas funds
    POST /hash                                    hash a passport document (no chain access)
    POST /passports                               create a passport (bearer token)
    POST /passports/batch                         create up to 100, idempotently (bearer token)
    GET  /passports/by-serial                     find one by tenant, product type and serial
    GET  /passports/{dppId}                       read a passport (latest version)
    POST /passports/{dppId}/versions              update it, appending a version (bearer token)
    GET  /passports/{dppId}/versions              its version history
    POST /passports/{dppId}/verify                check a document against it
    GET  /tenants/{tenantId}/passports            a tenant's passports, newest first

Run a single worker: the signer's nonce is tracked in-process, so several
workers sharing one key would send colliding transactions.
"""

import hmac
import logging
from contextlib import asynccontextmanager
from typing import Annotated, Any

from eth_account import Account
from fastapi import Body, Depends, FastAPI, Path, Query, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from starlette.exceptions import HTTPException
from starlette.types import ASGIApp, Message, Receive, Scope, Send

from . import networks
from .config import Settings, load_settings
from .hashing import data_hash
from .registry import MAX_PAGE, ApiError, RegistryClient
from .schemas import (
    BatchWriteResult,
    DocumentIn,
    ErrorBody,
    HashResult,
    Health,
    Passport,
    PassportBatchCreate,
    PassportCreate,
    PassportPage,
    PassportUpdate,
    Verification,
    VersionPage,
    WriteResult,
)

MAX_BODY_BYTES = 1024 * 1024

log = logging.getLogger("uvicorn.error")


async def connect(settings: Settings) -> RegistryClient:
    settings.require("registry_address", "api_token")
    key = settings.minter_private_key or settings.deployer_private_key
    if not key:
        raise RuntimeError("MINTER_PRIVATE_KEY (or DEPLOYER_PRIVATE_KEY) must be set (see .env.example)")

    w3 = await networks.connect(settings)
    registry = RegistryClient(
        w3,
        settings.registry_address,
        Account.from_key(key),
        network=settings.network,
        explorer=networks.NETWORKS[settings.network].explorer,
        confirmations=settings.confirmations,
        gas_budget=settings.gas_budget,
    )
    await registry.preflight()
    log.info(
        "DPP API — %s (chain %s), registry %s, signer %s",
        settings.network,
        await w3.eth.chain_id,
        registry.contract.address,
        registry.signer.address,
    )
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
        summary="Issues and verifies Digital Product Passports anchored on an EVM chain.",
        lifespan=lifespan,
        responses={"4XX": {"model": ErrorBody}, "5XX": {"model": ErrorBody}},
    )
    app.state.registry = registry
    app.state.api_token = api_token
    app.add_middleware(BodySizeLimit, max_bytes=MAX_BODY_BYTES)
    app.add_exception_handler(HTTPException, _http_error)
    app.add_exception_handler(RequestValidationError, _validation_error)
    app.add_exception_handler(Exception, _internal_error)
    _routes(app)
    return app


def _registry(request: Request) -> RegistryClient:
    return request.app.state.registry


RegistryDep = Annotated[RegistryClient, Depends(_registry)]

_bearer = HTTPBearer(auto_error=False)


def require_token(
    request: Request,
    credentials: Annotated[HTTPAuthorizationCredentials | None, Depends(_bearer)],
) -> None:
    """Writes spend the signer's gas, so they require the API token."""
    expected = request.app.state.api_token.encode()
    if credentials is None or not hmac.compare_digest(credentials.credentials.encode(), expected):
        raise ApiError(401, "UNAUTHORIZED", "missing or invalid bearer token")


# Identifiers in paths must not contain "/".
DppId = Annotated[str, Path(alias="dppId", min_length=1, max_length=128, pattern=r"^[^/]+$")]
TenantId = Annotated[str, Path(alias="tenantId", min_length=1, max_length=128, pattern=r"^[^/]+$")]
Offset = Annotated[int, Query(ge=0)]
Limit = Annotated[int, Query(ge=1, le=MAX_PAGE)]


def _routes(app: FastAPI) -> None:
    @app.get("/health", response_model=Health, tags=["ops"])
    async def health(registry: RegistryDep):
        return await registry.health()

    @app.post("/hash", response_model=HashResult, tags=["passports"])
    async def hash_document(document: Annotated[dict[str, Any], Body()]):
        """The hash that would be anchored for this passport document (RFC 8785 canonical JSON, SHA-256)."""
        try:
            return HashResult(data_hash=data_hash(document))
        except Exception as err:
            raise ApiError(400, "INVALID_INPUT", f"cannot be canonicalized ({err})") from err

    @app.post(
        "/passports",
        status_code=201,
        response_model=WriteResult,
        dependencies=[Depends(require_token)],
        tags=["passports"],
    )
    async def create_passport(body: PassportCreate, registry: RegistryDep):
        """Creates a passport for any product type. Send the document as `data`, or its hash as `dataHash`."""
        return await registry.create(body)

    @app.post(
        "/passports/batch",
        response_model=BatchWriteResult,
        dependencies=[Depends(require_token)],
        tags=["passports"],
    )
    async def create_passports(body: PassportBatchCreate, registry: RegistryDep):
        """
        Creates up to 100 passports, in as many transactions as the gas budget
        needs. Idempotent: resending a batch never writes a passport twice, so a
        caller that did not hear back can retry it. Each passport is reported in
        exactly one of `anchored`, `alreadyAnchored`, `rejected`, `retryable`.
        """
        return await registry.create_batch(body)

    @app.get("/passports/by-serial", response_model=Passport, tags=["passports"])
    async def passport_by_serial(
        registry: RegistryDep,
        tenant_id: Annotated[str, Query(alias="tenantId", min_length=1)],
        product_type: Annotated[str, Query(alias="productType", min_length=1)],
        serial_number: Annotated[str, Query(alias="serialNumber", min_length=1)],
    ):
        passport = await registry.get_by_serial(tenant_id, product_type, serial_number)
        if passport is None:
            raise ApiError(404, "NOT_FOUND", f"no {product_type} passport for serial {serial_number}")
        return passport

    @app.get("/passports/{dppId}", response_model=Passport, tags=["passports"])
    async def get_passport(dpp_id: DppId, registry: RegistryDep):
        passport = await registry.get(dpp_id)
        if passport is None:
            raise ApiError(404, "NOT_FOUND", f"no passport {dpp_id}")
        return passport

    @app.post(
        "/passports/{dppId}/versions",
        status_code=201,
        response_model=WriteResult,
        dependencies=[Depends(require_token)],
        tags=["passports"],
    )
    async def update_passport(dpp_id: DppId, body: PassportUpdate, registry: RegistryDep):
        """Records a new version: new data, a status change (e.g. recalled), a new URI or schema."""
        return await registry.update(dpp_id, body)

    @app.get("/passports/{dppId}/versions", response_model=VersionPage, tags=["passports"])
    async def passport_versions(dpp_id: DppId, registry: RegistryDep, offset: Offset = 0, limit: Limit = 20):
        """Version history, oldest first."""
        result = await registry.versions(dpp_id, offset, limit)
        if result is None:
            raise ApiError(404, "NOT_FOUND", f"no passport {dpp_id}")
        total, items = result
        return VersionPage(dpp_id=dpp_id, total=total, items=items)

    @app.post("/passports/{dppId}/verify", response_model=Verification, tags=["passports"])
    async def verify_passport(dpp_id: DppId, body: DocumentIn, registry: RegistryDep):
        """Checks whether a passport document is the one anchored on-chain, and which version it is."""
        result = await registry.verify(dpp_id, body)
        if result is None:
            raise ApiError(404, "NOT_FOUND", f"no passport {dpp_id}")
        return result

    @app.get("/tenants/{tenantId}/passports", response_model=PassportPage, tags=["passports"])
    async def tenant_passports(tenant_id: TenantId, registry: RegistryDep, offset: Offset = 0, limit: Limit = 20):
        """A tenant's passports, newest first."""
        total, items = await registry.by_tenant(tenant_id, offset, limit)
        return PassportPage(tenant_id=tenant_id, total=total, items=items)


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
