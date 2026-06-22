import time
from enum import Enum
from typing import Any, Tuple, Type

from pydantic_settings import BaseSettings, PydanticBaseSettingsSource
from azure.identity import DefaultAzureCredential
from azure.keyvault.secrets import SecretClient

VAULT_FIELDS = {
    "POSTGRESQL_PASSWORD",
    "AZURE_VISION_KEY",
    "AZURE_OPENAI_API_KEY",
    "SVC_AI_VEH_REPAIR_PASSWORD",
    "AZURE_BLOB_CONNECTION_STRING",
}


class AppEnvironment(str, Enum):
    SANDBOX = "sandbox"
    NONPROD = "nonprod"
    PRODUCTION = "production"


class AzureKeyVaultSettingsSource(PydanticBaseSettingsSource):
    """Pull settings from AKV at load time"""

    def __init__(self, settings_cls: Type[BaseSettings], vault_url: str):
        super().__init__(settings_cls)
        credential = DefaultAzureCredential()
        self.client = SecretClient(vault_url=vault_url, credential=credential)

    def get_field_value(self, field: Any, field_name: str) -> Tuple[Any, str, bool]:
        # key vault doesn't like underscores
        secret_name = field_name.replace("_", "-").lower()
        secret = self.client.get_secret(secret_name)
        return secret.value, field_name, False

    def __call__(self) -> dict[str, Any]:
        d: dict[str, Any] = {}
        for field_name, field_info in self.settings_cls.model_fields.items():
            if field_name not in VAULT_FIELDS:
                continue
            value, _, _ = self.get_field_value(field_info, field_name)
            if value is not None:
                d[field_name] = value
        return d


class Settings(BaseSettings):
    # POSTGRES_HOST: str = "vodev-db.postgres.database.azure.com"
    POSTGRES_HOST: str 
    POSTGRES_DB: str 
    POSTGRES_USER: str 
    POSTGRES_PORT: str 
    POSTGRESQL_PASSWORD: str
    AZURE_BLOB_CONNECTION_STRING: str
    AZURE_STORAGE_ACCOUNT: str 
    AZURE_CONTAINER_NAME: str 

    AZURE_VISION_ENDPOINT: str 
    AZURE_VISION_KEY: str 
    AZURE_VISION_COST_PER_1K: float 

    AZURE_OPENAI_ENDPOINT: str 
    AZURE_OPENAI_API_KEY: str

    VAULT_URL: str 

    APP_NAME: str 
    APP_VERSION: str 
    APP_ENVIRONMENT: AppEnvironment 
    log_level: str = "INFO"

    ICE_API_USER_NAME: str 
    API_CALLER_ID: str 
    API_CALLING_APP: str 
    SVC_AI_VEH_REPAIR_PASSWORD: str

    # ── API Ingest (environment-specific) ─────────────────────────────────
    API_AUTH_URL: str 
    API_BASE_URL: str 

    # ── CVD API (environment-specific) ─────────────────────────────────
    CVD_AUTH_URL: str 
    CVD_API_URL: str 

    # ── CSS API (environment-specific) ─────────────────────────────────
    CSS_API_URL: str 

    # ── CVD API (environment-specific) ─────────────────────────────────
    CVD_AUTH_URL: str = "https://auth-ipd.bpas.ehiaws-nonprod.com/auth/logon/jwt"
    CVD_API_URL: str = "https://xqa.api.ehi.dev/vehicle/fleetVehicle/search"

    # ── CSS API (environment-specific) ─────────────────────────────────
    CSS_API_URL: str = "http://devaply4:30040/claimsrv/services"

    # ── Vehicle Verification: VLM Configuration (environment-specific) ─────
    VLM_DEPLOYMENT: str 
    VLM_API_VERSION: str 
    VLM_PROMPT_COST_PER_1K: float 
    VLM_COMPLETION_COST_PER_1K: float 
    VLM_CURRENCY: str 

    # ── Estimate Matching: LLM Configuration (environment-specific) ────────
    LLM_DEPLOYMENT: str 
    LLM_API_VERSION: str 
    LLM_ENDPOINT: str 
    LLM_MAX_TOKENS: int 

    @classmethod
    def settings_customise_sources(
        cls,
        settings_cls: Type[BaseSettings],
        init_settings: PydanticBaseSettingsSource,
        env_settings: PydanticBaseSettingsSource,
        dotenv_settings: PydanticBaseSettingsSource,
        file_secret_settings: PydanticBaseSettingsSource,
    ):
        resolved = {**dotenv_settings(), **env_settings()}
        vault_url = resolved.get("VAULT_URL", cls.model_fields["VAULT_URL"].default)
        return (
            init_settings,
            AzureKeyVaultSettingsSource(settings_cls, vault_url=vault_url),
            env_settings,  # fallback in case secret is not in AKV
        )


# Cached singleton
_settings = None
_settings_ts = 0
TTL = 300


def get_settings() -> Settings:
    global _settings, _settings_ts
    if _settings is None or (time.time() - _settings_ts) > TTL:
        _settings = Settings()
        _settings_ts = time.time()
    return _settings