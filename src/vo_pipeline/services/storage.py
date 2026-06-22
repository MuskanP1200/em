from __future__ import annotations
import logging
import time
from typing import List, Optional
from urllib.parse import quote as url_quote
import requests
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry as Urllib3Retry
from azure.core.pipeline.transport import RequestsTransport
from azure.storage.blob import BlobServiceClient, ContainerClient, BlobClient

logger = logging.getLogger(__name__)


def _build_transport(pool_size: int) -> RequestsTransport:
    session = requests.Session()
    adapter = HTTPAdapter(
        pool_connections=pool_size, pool_maxsize=pool_size, max_retries=Urllib3Retry(0)
    )
    session.mount("https://", adapter)
    session.mount("http://", adapter)
    return RequestsTransport(session=session, session_owner=False)


def get_container_client(
    connection_string: Optional[str], container_name: Optional[str], pool_size: int = 20
) -> ContainerClient:
    if not connection_string:
        raise RuntimeError("Missing CONNECTION_STRING (env or --connection-string).")
    if not container_name:
        raise RuntimeError("Missing CONTAINER_NAME (env or --container).")
    transport = _build_transport(pool_size)
    bsc = BlobServiceClient.from_connection_string(
        connection_string, transport=transport
    )
    return bsc.get_container_client(container_name)


def list_blobs_under_prefix(
    container_client: ContainerClient, prefix: str, recursive: bool
) -> List[str]:
    names: List[str] = []
    if recursive:
        for b in container_client.list_blobs(name_starts_with=prefix):
            names.append(b.name)
    else:
        plen = len(prefix)
        for b in container_client.list_blobs(name_starts_with=prefix):
            rel = b.name[plen:]
            if "/" not in rel.strip("/"):
                names.append(b.name)
    return names


def azure_blob_url(container_client: ContainerClient, name: str) -> str:
    return f"{container_client.url}/{url_quote(name)}"


def azure_folder_url(container_client: ContainerClient, prefix: str) -> str:
    return f"{container_client.url}/{prefix}"


def _get_retry_after(exc: Exception) -> Optional[float]:
    """Extract Retry-After seconds from an Azure SDK or HTTP error, if present."""
    resp = getattr(exc, "response", None)
    if resp is not None:
        headers = getattr(resp, "headers", None) or {}
        ra = headers.get("Retry-After") or headers.get("retry-after")
        if ra:
            try:
                return float(ra)
            except (ValueError, TypeError):
                pass
    return None


def download_blob_bytes(
    container_client: ContainerClient,
    blob_name: str,
    retries: int = 3,
    backoff: float = 0.5,
) -> bytes:
    last_err = None
    for i in range(retries):
        try:
            bc: BlobClient = container_client.get_blob_client(blob_name)
            return bc.download_blob().readall()
        except Exception as e:
            last_err = e
            retry_after = _get_retry_after(e)
            wait = retry_after if retry_after else backoff * (2**i)
            status = getattr(getattr(e, "response", None), "status_code", None)
            if status == 429:
                logger.warning(
                    "Blob download throttled (429) for %s, retry %d/%d in %.1fs",
                    blob_name,
                    i + 1,
                    retries,
                    wait,
                )
            time.sleep(wait)
    raise last_err


def upload_blob_bytes(
    container_client: ContainerClient,
    blob_name: str,
    data: bytes,
    retries: int = 3,
    backoff: float = 0.5,
) -> str:
    """
    Upload raw bytes to a blob path inside the container.
    Returns the full blob URL on success. Overwrites existing blobs.
    Expected blob_name format: EST_{est_id}/{filename}
    """
    last_err = None
    for i in range(retries):
        try:
            bc: BlobClient = container_client.get_blob_client(blob_name)
            bc.upload_blob(data, overwrite=True)
            url = azure_blob_url(container_client, blob_name)
            logger.debug("Uploaded blob: %s", url)
            return url
        except Exception as e:
            last_err = e
            retry_after = _get_retry_after(e)
            wait = retry_after if retry_after else backoff * (2**i)
            status = getattr(getattr(e, "response", None), "status_code", None)
            if status == 429:
                logger.warning(
                    "Blob upload throttled (429) for %s, retry %d/%d in %.1fs",
                    blob_name,
                    i + 1,
                    retries,
                    wait,
                )
            elif i < retries - 1:
                logger.warning(
                    "Blob upload failed (attempt %d/%d) for %s: %s — retrying in %.1fs",
                    i + 1,
                    retries,
                    blob_name,
                    e,
                    wait,
                )
            time.sleep(wait)
    raise RuntimeError(
        f"Blob upload failed after {retries} retries for {blob_name}: {last_err}"
    )
