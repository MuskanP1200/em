"""
Estimate Matching Pipeline
─────────────────────────
Runs parts validation (LLM-based) and labour validation (rule-based)
in a single pass over all estimates, producing one unified line-level audit output.
"""

import json
import os
import sys
import time
import logging
from pathlib import Path

import numpy as np
import pandas as pd
from dotenv import load_dotenv
from openai import AzureOpenAI

os.environ["PYDEVD_WARN_EVALUATION_TIMEOUT"] = "30"
os.environ["PYDEVD_UNBLOCK_THREADS_TIMEOUT"] = "30"

# ── Project paths ────────────────────────────────────────────────────────────
PROJECT_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT_ROOT))

from sql_connection import read_table  # noqa: E402
from estimate_matching.llm_prompt import SYSTEM_PROMPT  # noqa: E402
from estimate_matching.db_writer import save_results  # noqa: E402
from estimate_matching.config import (  # noqa: E402
    DOMESTIC_MAKES,
    FOREIGN_MAKES,
    BAD_PART_NUMBERS,
    BASE_COLS,
    PARTS_INPUT_COLS,
    LBR_INPUT_COLS,
    OTHER_CHRG_COLS,
    RATE_COLS,
    PARTS_AUDIT_COLS,
    LBR_AUDIT_COLS,
    OTHER_CHRG_AUDIT_COLS,
    PARTS_SUBTOT_AUDIT_COLS,
    ROUND_DECIMALS,
    LABOR_TYPE_RATE_MAP,
    EST_LINE_NUMERIC_COLS,
    SUBTOT_NUMERIC_COLS,
    EST_GROUPBY_COLS,
    EST_AGG_MAP,
    SUBTOT_MERGE_COLS,
    SUBTOT_RENAME_MAP,
    TABLE_EST_LINE,
    TABLE_SUBTOT,
    # OUTPUT_SCHEMA,
    # TABLE_EST_SUMMARY,
    # TABLE_SUBTOT_DETAIL,
    # TABLE_LINE_DETAIL,
    DATA_SOURCE_MODE,
    API_CONFIG,
    LLM_DEPLOYMENT,
    LLM_API_VERSION,
    LLM_ENDPOINT,
    LLM_MAX_TOKENS,
)
from settings import get_settings  # noqa: E402

# ── Configuration ────────────────────────────────────────────────────────────
LOG_EVERY_N = 500

# Output column order

OUTPUT_COLS = (
    BASE_COLS
    + PARTS_INPUT_COLS
    + LBR_INPUT_COLS
    + OTHER_CHRG_COLS
    + RATE_COLS
    + PARTS_AUDIT_COLS
    + PARTS_SUBTOT_AUDIT_COLS
    + LBR_AUDIT_COLS
    + OTHER_CHRG_AUDIT_COLS
)


# ── Logging ──────────────────────────────────────────────────────────────────
logging.getLogger("openai").setLevel(logging.WARNING)
logging.getLogger("httpx").setLevel(logging.WARNING)
logging.getLogger("httpcore").setLevel(logging.WARNING)
logging.getLogger("charset_normalizer").setLevel(logging.WARNING)

s = get_settings()
logger = logging.getLogger(__name__)
# ── LLM client ───────────────────────────────────────────────────────────────


def create_llm_client() -> AzureOpenAI:
    api_key = None

    try:
        # api_key = get_secret("AZURE-OPENAI-API-KEY")
        api_key = s.AZURE_OPENAI_API_KEY
    except Exception as e:
        logger.warning(f"key vault not available, falling back to env: {e}")

    if not api_key:
        load_dotenv(PROJECT_ROOT / "../../../../config/.env")
        api_key = os.getenv("AZURE_OPENAI_API_KEY")
        logger.info("loading azure_api_key from env")

        if not api_key:
            raise ValueError(
                "Key Vault secret 'AZURE-OPENAI-API-KEY' is empty or missing."
            )

    return AzureOpenAI(
        api_version=LLM_API_VERSION,
        azure_endpoint=LLM_ENDPOINT,
        api_key=api_key,
    )


# ── Data helpers ─────────────────────────────────────────────────────────────
def cast_numeric(df: pd.DataFrame, cols: list[str]) -> pd.DataFrame:
    """Coerce specified columns to numeric, setting invalid values to NaN."""
    for col in cols:
        df[col] = pd.to_numeric(df[col], errors="coerce")
    return df


def get_unit_cost_by_labor_type(df: pd.DataFrame) -> pd.Series:
    """Map each labor type to its expected rate column."""
    result = pd.Series(np.nan, index=df.index, dtype=float)
    for labor_type, rate_col in LABOR_TYPE_RATE_MAP.items():
        mask = df["cieca_lbr_typ_dsc"] == labor_type
        result[mask] = df.loc[mask, rate_col]
    return result


# ── Parts: filtering ─────────────────────────────────────────────────────────
def filter_parts_lines(est_df: pd.DataFrame) -> pd.DataFrame:
    """
    Keep only genuine, discount-eligible parts lines.
    Drops: no parts detail, existing parts, misclassified part numbers.
    """
    df = est_df.copy()
    df = df[df["cieca_part_dtl_line_id"].notna()]
    df = df[
        ~df["cieca_part_typ_dsc"]
        .str.strip()
        .str.lower()
        .str.contains("existing", na=False)
    ]
    df = df[~df["dtl_part_nbr"].str.strip().str.lower().isin(BAD_PART_NUMBERS)]
    return df


# ── Parts: discount type derivation ──────────────────────────────────────────
def get_discount_type(parts_type: str, veh_make: str) -> str | None:
    """Derive discount type from parts classification and vehicle make."""
    if pd.isna(parts_type):
        return None
    pt = parts_type.lower()
    if "aftermarket" in pt:
        return "keyless"
    if "new" in pt:
        make = str(veh_make).upper().strip() if pd.notna(veh_make) else ""
        if make in FOREIGN_MAKES:
            return "foreign"
        if make in DOMESTIC_MAKES:
            return "domestic"
        return "unknown_make"
    return None


# ── Parts: JSON builder ──────────────────────────────────────────────────────
def build_estimate_json(est_df: pd.DataFrame) -> dict:
    """Convert filtered parts lines for one estimate into LLM input JSON."""
    first = est_df.iloc[0]

    parts_lines = []
    for _, row in est_df.iterrows():
        parts_lines.append(
            {
                "cieca_dtl_hdr_id": row.get("cieca_dtl_hdr_id"),
                "line_desc": row.get("line_dsc"),
                "parts_type": str(row.get("cieca_part_typ_dsc", "")).strip(),
                "parts_num": str(row.get("dtl_part_nbr", "")).strip(),
                "prts_amt": float(row.get("dtl_tot_part_price_amt", 0) or 0),
                "rule_derived_discount_type": get_discount_type(
                    str(row.get("cieca_part_typ_dsc", "")),
                    str(row.get("veh_make", "")),
                ),
                "cieca_line_adj_actual": float(row.get("cieca_line_adj_amt", 0) or 0),
            }
        )

    return {
        "est_id": int(first["est_id"]),
        "veh_make": str(first.get("veh_make", "")),
        # "damage_description": first["dmg_dsc"],
        "discount_rates": {
            "foreign": float(first.get("frn_part_disc_amt", 0) or 0),
            "domestic": float(first.get("dmstc_part_disc_amt", 0) or 0),
            "aftermarket": float(first.get("kyls_disc_amt", 0) or 0),
        },
        "special_notes": str(first.get("specl_instruct_txt", "") or ""),
        "sublet_markup": str(first.get("sublet_mrkup", "")),
        "postscn_flag": str(first.get("postscn", "")),
        "eligible_parts_lines": parts_lines,
    }


# ── Parts: derived columns (computed from LLM output) ───────────────────────
def _compute_parts_derived_cols(line: dict, parts_df: pd.DataFrame) -> None:
    """
    Mutates the LLM result dict in-place, adding fields that can be calculated
    from applicable_pct and the actual adjustment amount.
    """
    pct = line.get("applicable_discount_pct")
    hdr_id = line.get("cieca_dtl_hdr_id")

    actual_adj = 0.0
    if hdr_id is not None:
        row = parts_df[parts_df["cieca_dtl_hdr_id"] == hdr_id]
        if not row.empty:
            actual_adj = float(row.iloc[0].get("cieca_line_adj_amt") or 0)
            prts_amt = float(row.iloc[0].get("dtl_tot_part_price_amt") or 0)
        else:
            prts_amt = 0.0
    else:
        prts_amt = 0.0

    try:
        pct = float(pct) if pct is not None else None
    except (TypeError, ValueError):
        pct = None

    discount_expected = pct is not None and pct > 0
    expected_discount_amt = (
        round(prts_amt * (-pct / 100), ROUND_DECIMALS) if discount_expected else None
    )

    if expected_discount_amt is not None:
        discount_match = abs(actual_adj - expected_discount_amt) <= 1.0
        discount_variance = round(actual_adj - expected_discount_amt, ROUND_DECIMALS)
    else:
        discount_match = None
        discount_variance = None

    if discount_variance is not None and discount_match is False:
        if discount_variance < 0:
            discount_direction = "Over Discount"
        elif discount_variance > 0:
            discount_direction = "Under Discount"
        else:
            discount_direction = None
    else:
        discount_direction = None

    line["discount_expected"] = discount_expected
    line["expected_discount_amt"] = expected_discount_amt
    line["discount_match"] = discount_match
    line["discount_variance"] = discount_variance
    line["discount_direction"] = discount_direction


# ── Parts: LLM audit ────────────────────────────────────────────────────────
def audit_estimate_with_llm(client: AzureOpenAI, estimate_json: dict) -> list[dict]:
    """Send one estimate to the LLM, return parsed audit results."""
    response = client.chat.completions.create(
        model=LLM_DEPLOYMENT,
        max_tokens=LLM_MAX_TOKENS,
        messages=[
            {"role": "system", "content": SYSTEM_PROMPT},
            {"role": "user", "content": json.dumps(estimate_json, indent=2)},
        ],
    )
    return json.loads(response.choices[0].message.content)


# ── Parts: subtotal matching ─────────────────────────────────────────────────
def match_parts_subtotals(
    est_df: pd.DataFrame, est_subtot_df: pd.DataFrame
) -> list[dict]:
    """
    Validate parts lines for one estimate against the subtotal table.
    Groups line-level data by [est_id, cieca_part_typ_dsc], merges against
    subtotal rows (cieca_tot_typ_dsc containing "part"), then checks:
      - parts_gross_match: sum of line part amounts    == subtotal gross_amt
      - adj_match:         sum of line adjustments     == subtotal adj_tot_amt
      - parts_net_match:   sum of (part + adjustment)  == subtotal tot_amt (already net)
    Returns list of dicts (one per part type).
    """
    parts_df = est_df[est_df["cieca_part_dtl_line_id"].notna()].copy()
    subtot_df = est_subtot_df[
        est_subtot_df["cieca_tot_typ_dsc"].str.contains("part", case=False, na=False)
    ].copy()

    if parts_df.empty:
        return []

    subtot_df = subtot_df.rename(columns={"cieca_tot_typ_dsc": "cieca_part_typ_dsc"})
    subtot_df = cast_numeric(subtot_df, ["gross_amt", "adj_tot_amt", "tot_amt"])
    subtot_df[["gross_amt", "adj_tot_amt", "tot_amt"]] = subtot_df[
        ["gross_amt", "adj_tot_amt", "tot_amt"]
    ].fillna(0)

    # Aggregate line-level parts by estimate + part type
    grouped = (
        parts_df.groupby(["est_id", "cieca_part_typ_dsc"])
        .agg(
            line_tot_part_amt=("dtl_tot_part_price_amt", "sum"),
            line_adj_amt=("cieca_line_adj_amt", "sum"),
        )
        .reset_index()
        .round(ROUND_DECIMALS)
    )
    grouped["line_net_amt"] = (
        grouped["line_tot_part_amt"] + grouped["line_adj_amt"]
    ).round(ROUND_DECIMALS)

    # Merge subtotal
    merged = grouped.merge(
        subtot_df[
            ["est_id", "cieca_part_typ_dsc", "gross_amt", "adj_tot_amt", "tot_amt"]
        ],
        on=["est_id", "cieca_part_typ_dsc"],
        how="left",
    )

    no_subtot = merged["gross_amt"].isna()

    merged = cast_numeric(
        merged,
        [
            "line_tot_part_amt",
            "line_adj_amt",
            "line_net_amt",
            "gross_amt",
            "adj_tot_amt",
            "tot_amt",
        ],
    ).fillna(0)

    gross_match = merged["line_tot_part_amt"].round(ROUND_DECIMALS) == merged[
        "gross_amt"
    ].round(ROUND_DECIMALS)
    merged["parts_gross_match"] = np.where(
        no_subtot,
        "No subtotal found",
        np.where(gross_match, "Match", "No Match"),
    )

    adj_match = merged["line_adj_amt"] == merged["adj_tot_amt"].round(ROUND_DECIMALS)
    merged["adj_match"] = np.where(
        no_subtot,
        "No subtotal found",
        np.where(adj_match, "Match", "No Match"),
    )

    net_match = merged["line_net_amt"] == merged["tot_amt"].round(ROUND_DECIMALS)
    merged["parts_net_match"] = np.where(
        no_subtot,
        "No subtotal found",
        np.where(net_match, "Match", "No Match"),
    )

    merged["overall_parts_subtot_match"] = np.where(
        no_subtot,
        "No subtotal found",
        np.where(
            (merged["parts_gross_match"] == "Match")
            & (merged["parts_net_match"] == "Match"),
            "Match",
            "No Match",
        ),
    )

    return merged.to_dict(orient="records")


# ── Labour: matching ─────────────────────────────────────────────────────────
def match_labor_subtotals(
    est_df: pd.DataFrame, est_subtot_df: pd.DataFrame
) -> list[dict]:
    """
    Validate labour lines for one estimate against the subtotal table.
    Returns list of dicts (one per labour type).
    """
    est_df = est_df[
        est_df["cieca_lbr_typ_dsc"].str.contains("labor", case=False, na=False)
    ].copy()
    lbr_subtot_df = est_subtot_df[
        est_subtot_df["cieca_tot_typ_dsc"].str.contains("labor", case=False, na=False)
    ].copy()
    lbr_subtot_df = lbr_subtot_df.rename(columns=SUBTOT_RENAME_MAP)

    if est_df.empty:
        return []

    # Group line-level data
    grouped = (
        est_df.groupby(EST_GROUPBY_COLS)
        .agg(**EST_AGG_MAP)
        .reset_index()
        .round(ROUND_DECIMALS)
    )

    # Merge subtotal rates
    grouped = grouped.merge(lbr_subtot_df, on=SUBTOT_MERGE_COLS, how="left")
    grouped["tot_hr"] = grouped["tot_hr"].fillna(0)

    # Hours match
    no_hr_data = (grouped["tot_hr"].isna() | (grouped["tot_hr"] == 0)) & (
        grouped["dtl_lbr_hr_qty"].isna() | (grouped["dtl_lbr_hr_qty"] == 0)
    )
    hrs_match = grouped["dtl_lbr_hr_qty"].round(ROUND_DECIMALS) == grouped[
        "tot_hr"
    ].round(ROUND_DECIMALS)
    grouped["lbr_typ_hrs_match"] = np.where(
        no_hr_data, "Match", np.where(hrs_match, "Match", "No Match")
    )

    # Unit cost match
    grouped["unit_cost_based_lbr_dsc"] = get_unit_cost_by_labor_type(grouped)
    grouped["calc_unit_cost"] = (grouped["tot_amt"] / grouped["tot_hr"]).replace(
        [np.inf, -np.inf], np.nan
    )

    both_null = (
        grouped["unit_cost_based_lbr_dsc"].isna() & grouped["calc_unit_cost"].isna()
    )
    cost_match = grouped["unit_cost_based_lbr_dsc"].round(ROUND_DECIMALS) == grouped[
        "calc_unit_cost"
    ].round(ROUND_DECIMALS)
    grouped["lbr_typ_unit_cost_match"] = np.where(
        no_hr_data,
        "Match",
        np.where(both_null, "Match", np.where(cost_match, "Match", "No Match")),
    )

    # Overall match
    grouped["overall_lbr_match"] = np.where(
        (grouped["lbr_typ_hrs_match"] == "Match")
        & (grouped["lbr_typ_unit_cost_match"] == "Match"),
        "Match",
        "No Match",
    )

    # Directional flags — only meaningful when unit cost mismatches
    mismatch = grouped["lbr_typ_unit_cost_match"] == "No Match"
    grouped["overcharged"] = np.where(
        mismatch, grouped["calc_unit_cost"] > grouped["unit_cost_based_lbr_dsc"], None
    )
    grouped["undercharged"] = np.where(
        mismatch, grouped["calc_unit_cost"] < grouped["unit_cost_based_lbr_dsc"], None
    )

    return grouped.to_dict(orient="records")


# ── Data loading (branches on data_source_mode from config.yaml) ─────────────


def load_estimate_data(
    data_source_mode: str = DATA_SOURCE_MODE,
) -> tuple[pd.DataFrame, pd.DataFrame]:
    """
    Load est_line_df and subtot_df from the source configured in config.yaml.

    data_source_mode:
        prefiltered — read from pre-built DB snapshot (fast, for testing)
        live        — run load_estimates() against the live DB (query_table.py)
        api         — fetch from VR Services SOAP APIs (estimate_loader.py)

    Returns (est_line_df, subtot_df) ready for the main validation loop.
    """
    if data_source_mode == "api":
        from api_ingest.estimate_loader import (
            search_and_save_new_estimates,
            fetch_estimate_details,
        )  # noqa: E402
        from api_ingest.api_auth import get_token  # noqa: E402
        from settings import get_settings  # noqa: E402

        logger.info("data_source_mode=api: loading from VR Services APIs")
        creds = get_settings().model_dump()
        token = get_token(
            username=creds["ICE_API_USER_NAME"],
            password=creds["SVC_AI_VEH_REPAIR_PASSWORD"],
            auth_url="http://appsecwse-iprod/appsec/enhanced/webservice/rsi",
        )
        est_ids = search_and_save_new_estimates(
            token,
            status_code=API_CONFIG.get("status_code", "WAITONAUTH"),
            group=API_CONFIG.get("group", "DR"),
            max_records=API_CONFIG.get("max_records"),
        )
        est_line_df, subtot_df = fetch_estimate_details(
            token,
            est_ids,
            max_workers=API_CONFIG.get("max_workers", 4),
        )
        est_line_df = cast_numeric(est_line_df, EST_LINE_NUMERIC_COLS)
        subtot_df = cast_numeric(subtot_df, SUBTOT_NUMERIC_COLS)
        return est_line_df, subtot_df

    # prefiltered and live share the same subtot loading path; only est_line_df differs
    if data_source_mode == "live":
        from query_table import load_estimates  # noqa: E402

        logger.info("data_source_mode=live: loading est_line_df from live DB")
        est_line_df = cast_numeric(load_estimates(), EST_LINE_NUMERIC_COLS)
    else:  # prefiltered (default)
        logger.info(
            "data_source_mode=prefiltered: loading est_line_df from DB snapshot"
        )
        est_line_df = cast_numeric(read_table(TABLE_EST_LINE), EST_LINE_NUMERIC_COLS)

    subtot_df = cast_numeric(read_table(TABLE_SUBTOT), SUBTOT_NUMERIC_COLS)

    return est_line_df, subtot_df


# ── Single-estimate callable (used by pipeline_main.py orchestrator) ─────────


def run_em_pipeline(
    est_id: str,
    est_rows: pd.DataFrame,
    subtot_rows: pd.DataFrame,
    client: AzureOpenAI,
    save: bool = True,
) -> tuple[pd.DataFrame, pd.DataFrame, pd.DataFrame]:
    """
    Validate a single estimate.
    If save=True (default), appends results to Postgres immediately.
    Pass save=False to accumulate results in the caller and write in bulk.
    Returns (est_summary, subtot_detail, line_detail).
    """
    t0 = time.perf_counter()

    # Ensure discount % is present — safe to (re)compute; caller may omit it
    est_rows = est_rows.copy()
    est_rows["cieca_discount_pct"] = (
        est_rows["cieca_line_adj_amt"]
        / est_rows["dtl_tot_part_price_amt"].replace(0, pd.NA)
    ) * 100

    parts_results = []
    parts_subtot_results = []
    lbr_results = []

    # ── Parts matching (LLM) ────────────────────────────────────────────────
    parts_lines = filter_parts_lines(est_rows)
    if parts_lines.empty:
        logger.info("est_id %s: no parts lines — skipping LLM.", est_id)
    else:
        try:
            estimate_json = build_estimate_json(parts_lines)
            audit_lines = audit_estimate_with_llm(client, estimate_json)
            for line in audit_lines:
                line["est_id"] = est_id
                _compute_parts_derived_cols(line, parts_lines)
            parts_results.extend(audit_lines)
        except json.JSONDecodeError as e:
            logger.error("est_id %s: JSON PARSE ERROR — %s", est_id, e)
        except Exception as e:
            logger.error("est_id %s: PARTS LLM ERROR — %s", est_id, e)

    # ── Parts subtotal matching (rule-based) ────────────────────────────────
    try:
        result = match_parts_subtotals(est_rows, subtot_rows)
        if result:
            parts_subtot_results.extend(result)
    except Exception as e:
        logger.error("est_id %s: PARTS SUBTOTAL ERROR — %s", est_id, e)

    # ── Labour matching (rule-based) ────────────────────────────────────────
    try:
        result = match_labor_subtotals(est_rows, subtot_rows)
        if result:
            lbr_results.extend(result)
    except Exception as e:
        logger.error("est_id %s: LABOUR ERROR — %s", est_id, e)

    # ── Assemble + persist ───────────────────────────────────────────────────
    df_parts_audit = pd.DataFrame(parts_results)
    df_parts_subtot_audit = pd.DataFrame(parts_subtot_results)
    df_lbr_audit = pd.DataFrame(lbr_results)

    est_summary, subtot_detail, line_detail = _build_output_tables(
        est_rows, df_parts_audit, df_parts_subtot_audit, df_lbr_audit
    )
    if save:
        save_results(est_summary, subtot_detail, line_detail, if_exists="append")

    elapsed = time.perf_counter() - t0
    logger.info("est_id %s: EM completed in %.2fs", est_id, elapsed)

    return est_summary, subtot_detail, line_detail


def _build_output_tables(
    est_line_df: pd.DataFrame,
    df_parts_audit: pd.DataFrame,
    df_parts_subtot_audit: pd.DataFrame,
    df_lbr_audit: pd.DataFrame,
) -> tuple[pd.DataFrame, pd.DataFrame, pd.DataFrame]:
    """
    Assemble the three output DataFrames from raw audit results.

    Returns
    -------
    est_summary   : one row per est_id  (estimate-level pass/fail + issue counts)
    subtot_detail : one row per (est_id, type)  (parts + labour subtotals combined)
    line_detail   : one row per cieca_dtl_hdr_id  (LLM parts audit at line level)
    """
    # ── Table 1: line_detail ─────────────────────────────────────────────────
    # One row per cieca_dtl_hdr_id. Parts LLM audit joined at line level.
    # No subtotal columns — those live in their own tables.
    line_detail = est_line_df.copy()
    line_detail["other_charges_match"] = np.where(
        line_detail["cieca_othr_chrg_dtl_line_id"].notna(), "Not validated", None
    )
    if not df_parts_audit.empty:
        line_detail = line_detail.merge(
            df_parts_audit, on="cieca_dtl_hdr_id", how="left", suffixes=("", "_audit")
        )
        line_detail.drop(columns=["est_id_audit"], errors="ignore", inplace=True)

    line_cols = (
        BASE_COLS
        + RATE_COLS
        + PARTS_INPUT_COLS
        + LBR_INPUT_COLS
        + OTHER_CHRG_COLS
        + PARTS_AUDIT_COLS
        + OTHER_CHRG_AUDIT_COLS
    )
    line_detail = line_detail.reindex(columns=line_cols)
    for col in ("discount_expected", "discount_match"):
        if col in line_detail.columns:
            line_detail[col] = line_detail[col].astype("boolean")
    logger.info("line_detail shape: %s", line_detail.shape)

    # ── Table 2: subtot_detail ───────────────────────────────────────────────
    # One row per (est_id, type). Parts and labor combined with a subtot_type
    # discriminator; non-applicable columns are NaN.
    parts_subtot_cols = ["est_id", "cieca_part_typ_dsc"] + PARTS_SUBTOT_AUDIT_COLS
    df_parts_sub = (
        df_parts_subtot_audit[
            [c for c in parts_subtot_cols if c in df_parts_subtot_audit.columns]
        ].copy()
        if not df_parts_subtot_audit.empty
        else pd.DataFrame()
    )
    df_parts_sub.rename(
        columns={"cieca_part_typ_dsc": "cieca_tot_typ_dsc"}, inplace=True
    )
    df_parts_sub["subtot_type"] = "parts"

    lbr_subtot_cols = ["est_id", "cieca_lbr_typ_dsc"] + LBR_AUDIT_COLS + RATE_COLS
    df_lbr_sub = (
        df_lbr_audit[[c for c in lbr_subtot_cols if c in df_lbr_audit.columns]].copy()
        if not df_lbr_audit.empty
        else pd.DataFrame()
    )
    df_lbr_sub.rename(columns={"cieca_lbr_typ_dsc": "cieca_tot_typ_dsc"}, inplace=True)
    df_lbr_sub["subtot_type"] = "labor"

    subtot_cols = (
        ["est_id", "cieca_tot_typ_dsc", "subtot_type"]
        + PARTS_SUBTOT_AUDIT_COLS
        + LBR_AUDIT_COLS
        + RATE_COLS
    )
    subtot_detail = pd.concat(
        [df_parts_sub, df_lbr_sub], ignore_index=True, sort=False
    ).reindex(columns=subtot_cols)
    # subtot_detail = pd.concat([df_parts_sub, df_lbr_sub], ignore_index=True, sort=False)
    logger.info("subtot_detail shape: %s", subtot_detail.shape)

    # ── Table 3: est_summary ─────────────────────────────────────────────────
    # One row per est_id. Estimate-level pass/fail + key metadata + issue counts.
    est_meta_cols = [
        "est_id",
        "est_tot_amt",
        "lbr_hr_qty",
        "grp_nbr",
        "veh_make",
        # "dmg_dsc",
    ]
    est_summary = (
        est_line_df[[c for c in est_meta_cols if c in est_line_df.columns]]
        .drop_duplicates("est_id")
        .copy()
    )

    lbr_pass = (
        (
            df_lbr_audit.groupby("est_id")["overall_lbr_match"]
            .apply(lambda x: (x == "Match").all())
            .rename("lbr_est_pass")
            .reset_index()
        )
        if not df_lbr_audit.empty
        else pd.DataFrame(columns=["est_id", "lbr_est_pass"])
    )

    parts_pass = (
        (
            df_parts_subtot_audit.groupby("est_id")["overall_parts_subtot_match"]
            .apply(lambda x: (x == "Match").all())
            .rename("parts_est_pass")
            .reset_index()
        )
        if not df_parts_subtot_audit.empty
        else pd.DataFrame(columns=["est_id", "parts_est_pass"])
    )

    parts_issues = (
        (
            df_parts_audit[df_parts_audit["discount_match"].eq(False)]
            .groupby("est_id")
            .size()
            .rename("parts_line_issues")
            .reset_index()
        )
        if not df_parts_audit.empty
        else pd.DataFrame(columns=["est_id", "parts_line_issues"])
    )

    under_discount_issues = (
        (
            df_parts_audit[df_parts_audit["discount_direction"] == "Under Discount"]
            .groupby("est_id")
            .size()
            .rename("under_discount_lines")
            .reset_index()
        )
        if not df_parts_audit.empty
        else pd.DataFrame(columns=["est_id", "under_discount_lines"])
    )

    lbr_issues = (
        (
            df_lbr_audit[df_lbr_audit["overall_lbr_match"] == "No Match"]
            .groupby("est_id")
            .size()
            .rename("lbr_issues")
            .reset_index()
        )
        if not df_lbr_audit.empty
        else pd.DataFrame(columns=["est_id", "lbr_issues"])
    )

    for df in [lbr_pass, parts_pass, parts_issues, under_discount_issues, lbr_issues]:
        est_summary["est_id"] = est_summary["est_id"].astype(int)
        df["est_id"] = df["est_id"].astype(int)
        est_summary = est_summary.merge(df, on="est_id", how="left")

    lbr_ok = est_summary["lbr_est_pass"].isna() | est_summary["lbr_est_pass"].eq(True)
    parts_ok = est_summary["parts_est_pass"].isna() | est_summary["parts_est_pass"].eq(
        True
    )
    est_summary["estimate_match"] = np.where(lbr_ok & parts_ok, "Match", "No Match")
    # est_summary.drop(columns=["lbr_est_pass", "parts_est_pass"], inplace=True)
    issue_cols = ["parts_line_issues", "under_discount_lines", "lbr_issues"]
    est_summary[issue_cols] = est_summary[issue_cols].apply(
        lambda s: pd.to_numeric(s, errors="coerce").fillna(0).astype(int)
    )

    logger.info("est_summary shape: %s", est_summary.shape)

    return est_summary, subtot_detail, line_detail


# ── Main ─────────────────────────────────────────────────────────────────────
def main():
    client = create_llm_client()

    est_line_df, subtot_df = load_estimate_data(DATA_SOURCE_MODE)

    # est_line_df = est_line_df.sort_values(by = 'est_id').head(98)

    logger.info("est_line_df shape: %s", est_line_df.shape)
    logger.info("subtot_df shape:   %s", subtot_df.shape)

    est_ids = est_line_df["est_id"].unique()
    logger.info("Processing %d unique est_ids.", len(est_ids))

    t_total = time.perf_counter()
    succeeded = 0
    all_summary, all_subtot, all_line = [], [], []

    for i, est_id in enumerate(est_ids):
        if (i + 1) % LOG_EVERY_N == 0:
            logger.info("Progress: %d/%d estimates processed.", i + 1, len(est_ids))

        est_rows = est_line_df[est_line_df["est_id"] == est_id]
        subtot_rows = subtot_df[subtot_df["est_id"] == est_id]

        try:
            summary, subtot, line = run_em_pipeline(
                str(est_id), est_rows, subtot_rows, client, save=False
            )
            all_summary.append(summary)
            all_subtot.append(subtot)
            all_line.append(line)
            succeeded += 1
        except Exception as e:
            logger.error("est_id %s: FAILED — %s", est_id, e)

    save_results(
        pd.concat(all_summary, ignore_index=True),
        pd.concat(all_subtot, ignore_index=True),
        pd.concat(all_line, ignore_index=True),
        if_exists="replace",
    )

    elapsed = time.perf_counter() - t_total
    logger.info(
        "Finished: %d/%d estimates in %.1fs (avg %.2fs/estimate)",
        succeeded,
        len(est_ids),
        elapsed,
        elapsed / len(est_ids) if len(est_ids) else 0,
    )


if __name__ == "__main__":
    sys.path.insert(0, str(Path(__file__).resolve().parents[2]))  # api/
    from api_logging.config_logging import configure_logging
    from settings import get_settings

    configure_logging(get_settings())
    main()
