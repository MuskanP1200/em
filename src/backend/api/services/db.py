import logging
from contextlib import asynccontextmanager
from typing import Annotated

import asyncpg
from azure.storage.blob.aio import BlobServiceClient
from azure.identity.aio import DefaultAzureCredential
from fastapi import Depends, FastAPI, Request

from ..settings import get_settings

settings = get_settings()

logger = logging.getLogger(__name__)


def _build_connection_string(password: str) -> str:
    cs = (
        f"postgresql://{settings.POSTGRES_USER}:{password}"
        f"@{settings.POSTGRES_HOST}:{settings.POSTGRES_PORT}/{settings.POSTGRES_DB}"
        "?sslmode=require"
    )
    return cs.replace("+psycopg2", "").replace("+asyncpg", "")


@asynccontextmanager
async def lifespan(app: FastAPI):
    settings = get_settings()

    app.state.db_pool = await asyncpg.create_pool(
        _build_connection_string(settings.POSTGRESQL_PASSWORD),
        min_size=2,
        max_size=10,
    )
    logger.info("Database connection pool created")

    credential = DefaultAzureCredential()
    app.state.blob_service = BlobServiceClient(
        account_url=f"https://{settings.AZURE_STORAGE_ACCOUNT}.blob.core.windows.net",
        credential=credential,
    )
    logger.info("Blob service client initialized")

    yield

    await app.state.db_pool.close()
    await app.state.blob_service.close()
    await credential.close()
    logger.info("Database connection pool closed")


async def get_db_pool(request: Request) -> asyncpg.Pool:
    return request.app.state.db_pool


async def get_blob_service(request: Request) -> BlobServiceClient:
    return request.app.state.blob_service


DBPool = Annotated[asyncpg.Pool, Depends(get_db_pool)]
BlobService = Annotated[BlobServiceClient, Depends(get_blob_service)]
