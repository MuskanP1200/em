import os
import base64
import logging
from typing import List, Sequence

import asyncpg
from azure.storage.blob.aio import BlobServiceClient, BlobClient

from ..models.schemas import ImageDetailOut
from ..settings import get_settings

settings = get_settings()

logger = logging.getLogger(__name__)


async def fetch_folders(
    pool: asyncpg.Pool, limit: int, offset: int
) -> List[asyncpg.Record]:
    query = """
            select distinct on (f.folder_name) f.folder_name, f.vin_status,f.plate_status, dm.discount_match_status,
            CASE WHEN f.vin_status = true AND f.plate_status = true AND dm.discount_match_status='1' then True else False END status_flag
            from public.folders_901_prompt_eng f
            left join public.discount_matching_trial dm
            on dm.est_id = substring(f.folder_name from 4)
            LIMIT $1 OFFSET $2
        """
    async with pool.acquire() as conn:
        return await conn.fetch(query, limit, offset)


async def fetch_est_info(pool: asyncpg.Pool, folder_name: str) -> asyncpg.Record | None:
    query = """
            SELECT 
                f.folder_name folder_name,
                est.est_id est_id,
                est.repr_ncdnt_id repr_ncdnt_id,
                repr.lgcy_clm_nbr lgcy_clm_nbr,
                repr.clm_nbr clm_nbr,
                repr.clm_gpbr_id clm_gbpr_id,
                repr.acdnt_rpt_gpbr acdnt_rpt_gbpr,
                repr.odmtr_nbr odmtr_nbr,
                repr.dmg_dsc dmg_dsc,
                veh.vin vin,
                veh.licplte_nbr licplte_nbr,
                veh.xtr_colr_dsc xtr_colr_dsc,
                veh.licplte_st licplte_st,
                ymms.veh_make veh_make,
                ymms.veh_modl veh_mod,
                ymms.veh_yr veh_yr
            FROM public.folders_901_prompt_eng f
            INNER JOIN ice.est est
                ON SUBSTR(f.folder_name,4,13) = CAST(est.est_id AS VARCHAR)
            INNER JOIN ice.repr_ncdnt repr
                ON est.repr_ncdnt_id = repr.repr_ncdnt_id
            INNER JOIN ice.veh veh
                ON repr.veh_id = veh.veh_id
            INNER JOIN ice.veh_ymms ymms
                ON veh.veh_ymms_id = ymms.veh_ymms_id
            WHERE f.folder_name = $1;
            """
    async with pool.acquire() as conn:
        return await conn.fetchrow(query, folder_name)


async def fetch_vlm_stats(
    pool: asyncpg.Pool, folder_name: str
) -> asyncpg.Record | None:
    query = """
                SELECT 
                COUNT(*) FILTER (WHERE classified_label IS NOT NULL) as images_classified,
                COUNT(*) FILTER (WHERE text_detected = true) as images_with_text,
                COUNT(*) FILTER (WHERE classified_label = 'VIN') as vin_count,
                COUNT(*) FILTER (WHERE classified_label = 'License Plate') as license_plate_count,
                COUNT(*) FILTER (WHERE classified_label = 'Odometer') as odometer_count,
                COUNT(*) FILTER (WHERE classified_label = 'Other') as other_count
                FROM image_details_901_prompt_eng
                WHERE folder_name = $1
            """
    async with pool.acquire() as conn:
        return await conn.fetchrow(query, folder_name)


async def fetch_images(pool: asyncpg.Pool, folder_name: str) -> List[asyncpg.Record]:
    query = """
        SELECT image_path, classified_label, text_detected, ocr_success,
               vin_ocr_match, best_match_vin_ocr,vin_vlm_match, best_match_vin_vlm, ocr_vin_mismatch_count,vlm_vin_mismatch_count,
               plate_ocr_match, best_match_plate_ocr ,plate_vlm_match, best_match_plate_vlm,plate_ocr_mismatch_count,plate_vlm_mismatch_count
        FROM image_details_901_prompt_eng
        WHERE folder_name = $1
        ORDER BY image_path
        """
    async with pool.acquire() as conn:
        return await conn.fetch(query, folder_name)


async def build_image_details(
    rows: Sequence[asyncpg.Record],
    blob_service: BlobServiceClient,
) -> List[ImageDetailOut]:

    container_name = settings.AZURE_CONTAINER_NAME
    images: List[ImageDetailOut] = []

    async with blob_service.get_container_client(container_name) as container:
        for row in rows:

            # ---------------------------------------------------------
            # 1. Extract mismatch counts
            # ---------------------------------------------------------
            ocr_vin_mismatch = row.get("ocr_vin_mismatch_count")
            vlm_vin_mismatch = row.get("vlm_vin_mismatch_count")
            ocr_plate_mismatch = row.get("plate_ocr_mismatch_count")
            vlm_plate_mismatch = row.get("plate_vlm_mismatch_count")

            # ---------------------------------------------------------
            # 2. Determine match flags
            # ---------------------------------------------------------
            vin_ocr_match = row.get("vin_ocr_match")
            vin_vlm_match = row.get("vin_vlm_match")
            plate_ocr_match = row.get("plate_ocr_match")
            plate_vlm_match = row.get("plate_vlm_match")

            is_perfect_match_vin = vin_ocr_match or vin_vlm_match
            is_perfect_match_plate = plate_ocr_match or plate_vlm_match

            # ---------------------------------------------------------
            # 3. Determine detected VIN (even for mismatches)
            # ---------------------------------------------------------
            # Case A: Perfect OCR match
            if vin_ocr_match:
                detected_vin = row.get("best_match_vin_ocr")

            # Case B: Perfect VLM match
            elif vin_vlm_match:
                detected_vin = row.get("best_match_vin_vlm")

            # Case C: No matches — pick the “less wrong” source
            else:
                if ocr_vin_mismatch is not None and vlm_vin_mismatch is not None:
                    if ocr_vin_mismatch <= vlm_vin_mismatch:
                        detected_vin = row.get("best_match_vin_ocr")
                    else:
                        detected_vin = row.get("best_match_vin_vlm")
                else:
                    # fallback (should not happen but safe)
                    detected_vin = row.get("best_match_vin_ocr") or row.get(
                        "best_match_vin_vlm"
                    )

            # ---------------------------------------------------------
            # 3. Determine detected License Plate (even for mismatches)
            # ---------------------------------------------------------
            # Case A: Perfect OCR match
            if plate_ocr_match:
                detected_plate = row.get("best_match_plate_ocr")

            # Case B: Perfect VLM match
            elif plate_vlm_match:
                detected_plate = row.get("best_match_plate_vlm")

            # Case C: No matches — pick the “less wrong” source
            else:
                if ocr_plate_mismatch is not None and vlm_plate_mismatch is not None:
                    if ocr_plate_mismatch <= vlm_plate_mismatch:
                        detected_plate = row.get("best_match_plate_ocr")
                    else:
                        detected_plate = row.get("best_match_plate_vlm")
                else:
                    # fallback (should not happen but safe)
                    detected_plate = row.get("best_match_plate_ocr") or row.get(
                        "best_match_plate_vlm"
                    )

            # ---------------------------------------------------------
            # 4. Load image as base64
            # ---------------------------------------------------------
            try:
                async with BlobClient.from_blob_url(
                    blob_url=row["image_path"],
                    credential=container.credential,
                ) as blob:
                    downloader = await blob.download_blob()
                    data = await downloader.readall()
                    image_b64 = base64.b64encode(data).decode()
            except Exception as e:
                logger.warning("Failed to download blob: %s", row["image_path"])
                logger.error(f"Error fetching blob {e}")
                image_b64 = None

            # ---------------------------------------------------------
            # 5. Return without modifying the output model definition
            # ---------------------------------------------------------
            images.append(
                ImageDetailOut(
                    image_path=row["image_path"],
                    image_name=os.path.basename(row["image_path"]),
                    image_data=image_b64,
                    classified_label=row["classified_label"],
                    text_detected=row["text_detected"],
                    ocr_success=row["ocr_success"],
                    # final computed backend values
                    is_perfect_match_vin=is_perfect_match_vin,
                    overall_best_match_vin=detected_vin,
                    is_perfect_match_plate=is_perfect_match_plate,
                    overall_best_match_plate=detected_plate,
                )
            )

    return images


async def fetch_parts(pool: asyncpg.Pool, est_id: str) -> List[asyncpg.Record]:
    query = """
        WITH est_subset AS (
            SELECT * FROM ice.est WHERE est_id = $1
        )
        SELECT 
            hdr.line_dsc,
            part.dtl_tot_part_price_amt
        FROM est_subset e
        LEFT JOIN ice.est_repr er
            ON e.est_id = er.est_id
        LEFT JOIN ice.elctrnc_est_dtl ed
            ON er.est_repr_id = ed.est_repr_id
        LEFT JOIN ice.cieca_dtl_hdr hdr
            ON ed.elctrnc_est_dtl_id = hdr.elctrnc_est_dtl_id
        LEFT JOIN ice.cieca_part_dtl_line part
            ON hdr.cieca_dtl_hdr_id = part.cieca_dtl_hdr_id
        LEFT JOIN ice.cieca_line_adj adj
            ON part.cieca_line_adj_id = adj.cieca_line_adj_id
        WHERE part.cieca_part_dtl_line_id IS NOT NULL
        """
    async with pool.acquire() as conn:
        return await conn.fetch(query, est_id)


async def fetch_discount(pool: asyncpg.Pool, est_id: str) -> List[asyncpg.Record]:
    query = """
        SELECT *
        FROM public.discount_matching_trial
        WHERE est_id = $1
        """
    async with pool.acquire() as conn:
        return await conn.fetch(query, est_id)
