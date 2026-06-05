import logging
import threading
from concurrent.futures import ThreadPoolExecutor, as_completed

from storage import upload_blob_bytes
from api_ingest.estimate_client import get_image_list, get_image_bytes

logger = logging.getLogger(__name__)


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
