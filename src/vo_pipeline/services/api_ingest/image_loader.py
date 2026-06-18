import logging
import threading
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path

from storage import upload_blob_bytes
from api_ingest.estimate_client import get_image_list, get_image_bytes
from api_ingest.claims_client import get_css_image_list, get_css_image_bytes

logger = logging.getLogger(__name__)


def _cl_filename(filename: str) -> str:
    """photo.jpg  →  photo_cl.jpg"""
    p = Path(filename)
    return f"{p.stem}_cl{p.suffix}"


def upload_estimate_images(
    token: str,
    est_id: str,
    container_client,
    max_workers: int = 6,
) -> list[str]:
    """
    Fetch and upload all estimate images to Blob Storage.

    Returns:
        List of successfully uploaded blob URLs.
    """
    attachments = get_image_list(token, est_id)
    if not attachments:
        logger.warning("est_id=%s: no attachments found", est_id)
        return []

    blob_folder = f"EST{est_id}"
    uploaded_urls: list[str] = []

    def _process_attachment(att: dict) -> str | None:
        try:
            file_name, image_bytes = get_image_bytes(token, att)

            if not image_bytes:
                logger.warning(
                    "est_id=%s: attachment=%s has empty payload",
                    est_id,
                    att.get("att:Id"),
                )
                return None

            return upload_blob_bytes(
                container_client,
                f"{blob_folder}/{file_name}",
                image_bytes,
            )

        except Exception:
            logger.error(
                "est_id=%s: attachment failed id=%s",
                est_id,
                att.get("att:Id"),
                exc_info=True,
            )
            return None

    logger.debug(
        "est_id=%s: starting image upload pool (workers=%d, attachments=%d) — active threads: %d",
        est_id,
        max_workers,
        len(attachments),
        threading.active_count(),
    )
    with ThreadPoolExecutor(max_workers=max_workers) as pool:
        futures = [pool.submit(_process_attachment, att) for att in attachments]
        for fut in as_completed(futures):
            result = fut.result()
            if result:
                uploaded_urls.append(result)

    logger.info(
        "est_id=%s: uploaded %d/%d images",
        est_id,
        len(uploaded_urls),
        len(attachments),
    )
    logger.debug(
        "est_id=%s: image upload pool done — active threads: %d",
        est_id,
        threading.active_count(),
    )

    return uploaded_urls


def upload_claims_images(
    css_token: str,
    claim_id: str,
    est_id: str,
    container_client,
    css_api_url: str,
    max_workers: int = 6,
) -> list[str]:
    """
    Fetch and upload all CSS Claims images for one estimate to Blob Storage.
    Images are stored in EST{est_id}/ alongside VR images, with _cl suffix
    to distinguish them:  photo.jpg  →  EST123/photo_cl.jpg

    Returns list of successfully uploaded blob URLs.
    """
    documents = get_css_image_list(css_token, claim_id, css_api_url)
    if not documents:
        logger.warning("est_id=%s claim_id=%s: no CSS documents found", est_id, claim_id)
        return []

    blob_folder = f"EST{est_id}"
    uploaded_urls: list[str] = []

    def _process_document(doc: dict) -> str | None:
        try:
            file_name, image_bytes = get_css_image_bytes(css_token, doc["doc_id"], css_api_url)

            if not image_bytes:
                logger.warning(
                    "est_id=%s: CSS doc_id=%s has empty payload",
                    est_id, doc["doc_id"],
                )
                return None

            blob_name = _cl_filename(file_name)
            return upload_blob_bytes(
                container_client,
                f"{blob_folder}/{blob_name}",
                image_bytes,
            )

        except Exception:
            logger.error(
                "est_id=%s: CSS doc_id=%s upload failed",
                est_id, doc["doc_id"], exc_info=True,
            )
            return None

    with ThreadPoolExecutor(max_workers=max_workers) as pool:
        futures = [pool.submit(_process_document, doc) for doc in documents]
        for fut in as_completed(futures):
            result = fut.result()
            if result:
                uploaded_urls.append(result)

    logger.info(
        "est_id=%s claim_id=%s: CSS uploaded %d/%d images",
        est_id, claim_id, len(uploaded_urls), len(documents),
    )

    return uploaded_urls