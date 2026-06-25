from datetime import timedelta
from typing import List, Optional
from pydantic import Field
from pydantic_settings import BaseSettings


class IapJwtSettings(BaseSettings):
    jwt_expected_audience: str = Field(alias="VO_ENTRA_CLIENT_ID")
    tenant_id: str = Field(alias="VO_ENTRA_TENANT_ID")
    # These should rarely change
    AZURE_JWK_URL: Optional[str] = None
    AZURE_JWT_LEEWAY: timedelta = timedelta(seconds=60)
    AZURE_JWT_EXPECTED_ISS: Optional[str] = None
    AZURE_JWT_EXPECTED_ALGORITHMS: List[str] = ["RS256"]

    def __init__(self, **data):
        super().__init__(**data)
        if not self.AZURE_JWK_URL:
            self.AZURE_JWK_URL = f"https://login.microsoftonline.com/{self.tenant_id}/discovery/v2.0/keys"
        if not self.AZURE_JWT_EXPECTED_ISS:
            self.AZURE_JWT_EXPECTED_ISS = (
                f"https://login.microsoftonline.com/{self.tenant_id}/v2.0"
            )
