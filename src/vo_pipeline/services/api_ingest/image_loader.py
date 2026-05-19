import logging
from concurrent.futures import ThreadPoolExecutor, as_completed

from storage import upload_blob_bytes
from api_ingest.estimate_client import get_image_list, get_image_bytes

logger = logging.getLogger(__name__)


def upload_estimate_images(
    token: str, est_id: str, container_client, max_workers: int = 10
) -> list:
    """
    Fetches all images for an estimate and uploads them to Azure Blob Storage
    under the folder EST{est_id}/. Returns list of blob URLs for successfully
    uploaded images. Images are fetched and uploaded concurrently.
    """
    attachments = get_image_list(token, est_id)
    if not attachments:
        logger.warning("No attachments found for est_id=%s.", est_id)
        return []

    blob_folder = f"EST{est_id}"
    uploaded_urls = []

    def _fetch_and_upload(attachment: dict) -> str:
        file_name, image_bytes = get_image_bytes(token, attachment)
        return upload_blob_bytes(
            container_client, f"{blob_folder}/{file_name}", image_bytes
        )

    with ThreadPoolExecutor(max_workers=max_workers) as pool:
        futures = {pool.submit(_fetch_and_upload, att): att for att in attachments}
        for fut in as_completed(futures):
            att = futures[fut]
            try:
                url = fut.result()
                uploaded_urls.append(url)
            except Exception:
                logger.error("Failed to upload attachment %s", att.get("att:Id"), exc_info=True)

    return uploaded_urls
