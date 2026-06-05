import time
from enum import Enum
from typing import Any, Tuple, Type

from pydantic_settings import BaseSettings, PydanticBaseSettingsSource
from azure.identity import DefaultAzureCredential
from azure.keyvault.secrets import SecretClient

VAULT_FIELDS = {"SESSION_SECRET", "VO_AZURE_CLIENT_SECRET"}


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
        # try:
        print(f"Looking up secret: {secret_name}")
        secret = self.client.get_secret(secret_name)
        return secret.value, field_name, False
        # except Exception:
        #     return None, field_name, False

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
    BACKEND_URL: str = "http://localhost:8018"

    VAULT_URL: str = "https://kv-centralus-vodev.vault.azure.net"

    # Entra ID
    AUTH_ENABLED: bool = False
    VO_AZURE_CLIENT_ID: str = ""
    VO_AZURE_CLIENT_SECRET: str = ""
    VO_AZURE_TENANT_ID: str = ""
    REDIRECT_PATH: str = "/oauth2callback"
    REDIRECT_URI: str = ""
    SCOPES: list[str] = [""]
    SESSION_SECRET: str = ""

    APP_NAME: str = "vo-frontend"
    APP_VERSION: str = "1.0.0"
    APP_ENVIRONMENT: AppEnvironment = AppEnvironment.SANDBOX
    log_level: str = "DEBUG"

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
