import time
from enum import Enum
from typing import Any, Tuple, Type

from pydantic_settings import BaseSettings, PydanticBaseSettingsSource
from azure.identity import DefaultAzureCredential
from azure.keyvault.secrets import SecretClient

VAULT_FIELDS = {
    "POSTGRESQL_PASSWORD",
    "DATABRICKS_CLIENT_SECRET",
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
    POSTGRES_HOST: str = "10.117.111.132"
    POSTGRES_DB: str = "postgres"
    POSTGRES_USER: str = "voadmin"
    POSTGRES_PORT: str = "5432"
    POSTGRESQL_PASSWORD: str
    AZURE_BLOB_CONNECTION_STRING: str
    AZURE_STORAGE_ACCOUNT: str = "storagecentralusvodev"
    AZURE_CONTAINER_NAME: str = "images"
    AZURE_API_IMAGES_CONTAINER_NAME: str = "api-images"

    AZURE_VISION_ENDPOINT: str = (
        "https://comp-vision-centralus.cognitiveservices.azure.com/"
    )
    AZURE_VISION_KEY: str = ""
    AZURE_VISION_COST_PER_1K: float = 1.5

    AZURE_OPENAI_ENDPOINT: str = "https://musk-vo-1.openai.azure.com"
    AZURE_OPENAI_API_KEY: str

    VAULT_URL: str = "https://kv-centralus-vodev.vault.azure.net"

    DATABRICKS_CLIENT_SECRET: str = ""
    DATABRICKS_HOST: str = "https://adb-1919663167401821.1.azuredatabricks.net"
    DATABRICKS_HTTP_PATH: str = "/sql/1.0/warehouses/56c50994f4bfff03"
    DATABRICKS_CLIENT_ID: str = "1e188b65-3866-4ccd-82b7-09017670f13d"
    DATABRICKS_SCOPE: str = "2ff814a6-3304-4ab8-85cb-cd0e6f879c1d/.default"
    AZURE_TENANT_ID: str = "5a9bb941-ba53-48d3-b086-2927fea7bf01"
    DATABRICKS_WAREHOUSE_ID: str = "56c50994f4bfff03"

    APP_NAME: str = "vo-backend"
    APP_VERSION: str = "1.0.0"
    APP_ENVIRONMENT: AppEnvironment = AppEnvironment.SANDBOX
    log_level: str = "DEBUG"

    ICE_API_USER_NAME: str = "SVC_AI_VEH_REPAIR"
    SVC_AI_VEH_REPAIR_PASSWORD: str

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
