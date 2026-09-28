from pydantic import Field, ValidationError
from pydantic_settings import BaseSettings, SettingsConfigDict

from .registry import NetworkName


class Settings(BaseSettings):
    """API configuration, read from the environment or a `.env` file. See `.env.example`."""

    # The repo-root .env is shared with Hardhat; a backend/.env overrides it.
    # hide_input_in_errors: a validation error must not echo the private key.
    model_config = SettingsConfigDict(
        env_file=("../.env", ".env"), env_ignore_empty=True, extra="ignore", hide_input_in_errors=True
    )

    network: NetworkName = "arbitrumSepolia"
    dwellir_api_key: str | None = None
    rpc_url: str | None = Field(None, description="Overrides Dwellir, e.g. http://127.0.0.1:8545 for `npm run node`.")

    registry_address: str
    api_token: str
    minter_private_key: str | None = Field(None, description="Must hold MINTER_ROLE. Defaults to DEPLOYER_PRIVATE_KEY.")
    deployer_private_key: str | None = None
    confirmations: int = Field(1, ge=1, description="Blocks to wait for before a mint returns.")


def load_settings() -> Settings:
    """Reads the settings, naming the offending variables if any are missing or invalid."""
    try:
        return Settings()
    except ValidationError as e:
        problems = "; ".join(f"{str(err['loc'][0]).upper()}: {err['msg']}" for err in e.errors())
        raise RuntimeError(f"invalid configuration (see .env.example) — {problems}") from None
