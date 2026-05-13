import yaml
from pathlib import Path

import sys
import base64
import logging

import requests
import xmltodict

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from storage import upload_blob_bytes
from api_ingest.api_request_builder import build_image_list_body, build_image_bytes_body
from settings import get_settings  # noqa: E402

logger = logging.getLogger(__name__)

BASE_URL = get_settings().API_BASE_URL

_COMMON_HEADERS = {
    "Content-Type": "application/xml",
    "ehi-locale": "en_US",
}


def _post_xml(url: str, payload: str) -> dict:
    """POST XML payload and return parsed response as a dict (via xmltodict)."""
    resp = requests.post(url, headers=_COMMON_HEADERS, data=payload, timeout=30)
    if resp.status_code != 200:
        raise RuntimeError(f"Request failed [{resp.status_code}]: {resp.text[:500]}")
    return xmltodict.parse(resp.text)


def fetch_image_list(token: str, est_id: str) -> list:
    """Returns list of attachment metadata dicts for a given estimate."""
    response = _post_xml(BASE_URL, build_image_list_body(token, est_id))

    attachments = response.get("att:GetAttachmentsForEstimateRS", {}).get(
        "att:Attachment", []
    )
    if isinstance(attachments, dict):
        attachments = [attachments]

    return attachments


def fetch_image_bytes(token: str, attachment: dict) -> tuple[str, bytes]:
    """Downloads a single image and returns (filename, raw_bytes)."""
    attachment_id = attachment.get("att:Id")
    attachment_name = attachment.get("att:Name", f"{attachment_id}.jpg")

    response = _post_xml(BASE_URL, build_image_bytes_body(token, attachment_id))

    image_b64 = response.get("att:GetAttachmentBytesRS", {}).get("att:AttachmentBytes")
    if not image_b64:
        raise RuntimeError(
            f"No image bytes returned for attachment ID {attachment_id}."
        )

    return attachment_name, base64.b64decode(image_b64)


def upload_estimate_images(token: str, est_id: str, container_client) -> list:
    """
    Fetches all images for an estimate and uploads them to Azure Blob Storage
    under the folder EST{est_id}/. Returns list of blob URLs for successfully
    uploaded images.
    """
    attachments = fetch_image_list(token, est_id)
    if not attachments:
        logger.warning("No attachments found for est_id=%s.", est_id)
        return []

    blob_folder = f"EST{est_id}"
    uploaded_urls = []

    for attachment in attachments:
        try:
            file_name, image_bytes = fetch_image_bytes(token, attachment)
            url = upload_blob_bytes(
                container_client, f"{blob_folder}/{file_name}", image_bytes
            )
            uploaded_urls.append(url)
        except Exception as e:
            logger.error("Failed to upload %s: %s", attachment.get("att:Id"), e)

    return uploaded_urls
