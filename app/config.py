from typing import Literal

from pydantic import Field, ValidationError
from pydantic_settings import BaseSettings, SettingsConfigDict

NetworkName = Literal["arbitrumSepolia", "arbitrumOne", "hederaTestnet", "hederaMainnet", "localhost"]


class Settings(BaseSettings):
    """Configuration, read from the environment or `.env`. See `.env.example`."""

    # hide_input_in_errors: a validation error must not echo a private key.
    model_config = SettingsConfigDict(env_file=".env", env_ignore_empty=True, extra="ignore", hide_input_in_errors=True)

    network: NetworkName = "hederaTestnet"
    dwellir_api_key: str | None = None
    rpc_url: str | None = Field(None, description="Overrides the network's RPC endpoint. Its chain must still match NETWORK.")

    registry_address: str | None = Field(None, description="The deployed DppRegistry. Required by the API.")
    api_token: str | None = Field(None, min_length=32, description="Bearer token for writes. Required by the API.")
    minter_private_key: str | None = Field(None, description="The API's signer; must be a minter. Defaults to DEPLOYER_PRIVATE_KEY.")
    deployer_private_key: str | None = Field(None, description="Deploys the registry (CLI only).")
    admin_private_key: str | None = Field(None, description="Manages minters (CLI only). Defaults to DEPLOYER_PRIVATE_KEY.")
    confirmations: int = Field(1, ge=1, description="Blocks to wait for before a write returns.")

    def require(self, *names: str) -> None:
        missing = [n.upper() for n in names if not getattr(self, n)]
        if missing:
            raise RuntimeError(f"{', '.join(missing)} must be set (see .env.example)")


def load_settings() -> Settings:
    """Reads the settings, naming the offending variables if any are invalid."""
    try:
        return Settings()
    except ValidationError as e:
        problems = "; ".join(f"{str(err['loc'][0]).upper()}: {err['msg']}" for err in e.errors())
        raise RuntimeError(f"invalid configuration (see .env.example) — {problems}") from None
