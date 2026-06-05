from __future__ import annotations

from pathlib import Path
from typing import Any

import yaml

from settings import get_settings

_full_cfg: dict[str, Any] = yaml.safe_load(
    (Path(__file__).parent.parent / "config.yaml").read_text()
)
_cfg = _full_cfg["estimate_matching"]
_settings = get_settings()


# ── ICE table map (alias → fully-qualified postgres name) ─────────────────────
_ice_schema: str = _cfg["sources"]["postgres"]["schema"]
ICE_TABLES: dict[str, str] = {
    alias: f"{_ice_schema}.{name}" for alias, name in _cfg["ice_tables"].items()
}


# ── Shared staging table names (written by api_ingest) ───────────────────────

_staging = _full_cfg["tables"]
API_INGEST_SCHEMA: str = _staging["schema"]
API_INGEST_EST_RAW: str = _staging["staging"]["est_raw"]
API_INGEST_EST_LINE: str = _staging["staging"]["est_line"]
API_INGEST_EST_SUBTOT: str = _staging["staging"]["est_subtot"]

# ── API ingest settings ───────────────────────────────────────────────────────

_api = _full_cfg["api_ingest"]
AUTH_URL: str = _settings.API_AUTH_URL
API_MAX_RECORDS: int | None = _api.get("max_records")
API_MAX_WORKERS: int = _api.get("max_workers", 4)
API_IMAGE_WORKERS: int = _api.get("image_max_workers", 4)
API_CONFIG: dict = _api

# ── EM output table names ─────────────────────────────────────────────────────

TABLE_EST_LINE: str = _cfg["tables_public"]["est_line"]
TABLE_SUBTOT: str = _cfg["tables_public"]["subtot"]

_em_out = _full_cfg["tables"]["em_output"]
_vi_out = _full_cfg["tables"]["vi_output"]
OUTPUT_SCHEMA: str = _full_cfg["tables"]["schema"]
TABLE_EST_SUMMARY: str = _em_out["est_summary"]
TABLE_SUBTOT_DETAIL: str = _em_out["subtot_detail"]
TABLE_LINE_DETAIL: str = _em_out["line_detail"]
TABLE_OVERALL_SUMMARY: str = _em_out["overall_summary"]

TABLE_VI_EST: str = _vi_out["folders_table"]
TABLE_VI_IMG: str = _vi_out["images_table"]

# ── Column groups ─────────────────────────────────────────────────────────────

CIECA_DTL_HDR: list[str] = _cfg["columns"]["cieca_dtl_hdr"]
CIECA_PART_DTL: list[str] = _cfg["columns"]["cieca_part_dtl"]
CIECA_LBR_DTL: list[str] = _cfg["columns"]["cieca_lbr_dtl"]
CIECA_OTHR_CHRGES_DTL: list[str] = _cfg["columns"]["cieca_othr_chrg_dtl"]
CIECA_LINE_ADJ: list[str] = _cfg["columns"]["cieca_line_adj"]

BASE_COLS: list[str] = _cfg["output_columns"]["base"]
PARTS_INPUT_COLS: list[str] = _cfg["output_columns"]["parts_input"]
LBR_INPUT_COLS: list[str] = _cfg["output_columns"]["labor_input"]
OTHER_CHRG_COLS: list[str] = _cfg["output_columns"]["other_charges"]
RATE_COLS: list[str] = _cfg["output_columns"]["rates"]
PARTS_AUDIT_COLS: list[str] = _cfg["output_columns"]["parts_audit"]
LBR_AUDIT_COLS: list[str] = _cfg["output_columns"]["labor_audit"]
OTHER_CHRG_AUDIT_COLS: list[str] = _cfg["output_columns"]["other_chrg_audit"]
PAINT_AUDIT_COLS: list[str] = _cfg["output_columns"]["paint_audit"]
PARTS_SUBTOT_AUDIT_COLS: list[str] = _cfg["output_columns"]["parts_subtot_audit"]

# ── Numerics / groupby / agg ──────────────────────────────────────────────────

EST_LINE_NUMERIC_COLS: list[str] = _cfg["numeric_cols"]["est_line"]
SUBTOT_NUMERIC_COLS: list[str] = _cfg["numeric_cols"]["subtot"]
EST_GROUPBY_COLS: list[str] = _cfg["groupby_cols"]
EST_AGG_MAP: dict[str, tuple[str, str]] = {
    k: (v["col"], v["func"]) for k, v in _cfg["aggregations"].items()
}
SUBTOT_MERGE_COLS: list[str] = _cfg["subtot_merge_cols"]
SUBTOT_RENAME_MAP: dict[str, str] = _cfg["subtot_rename_map"]

# ── Data source mode ──────────────────────────────────────────────────────────

DATA_SOURCE_MODE: str = _cfg["data_source_mode"]

# ── LLM settings ──────────────────────────────────────────────────────────────

LLM_DEPLOYMENT: str = _settings.LLM_DEPLOYMENT
LLM_API_VERSION: str = _settings.LLM_API_VERSION
LLM_ENDPOINT: str = _settings.LLM_ENDPOINT
LLM_MAX_TOKENS: int = _settings.LLM_MAX_TOKENS

# ── Query filters ─────────────────────────────────────────────────────────────

FILTERS: dict = _cfg["filters"]

# ── Misc ──────────────────────────────────────────────────────────────────────

ROUND_DECIMALS: int = _cfg["rounding"]["decimals"]
LABOR_TYPE_RATE_MAP: dict[str, str] = _cfg["labor_type_rate_map"]
DOMESTIC_MAKES: list[str] = _cfg["vehicle_makes"]["domestic"]
FOREIGN_MAKES: list[str] = _cfg["vehicle_makes"]["foreign"]
BAD_PART_NUMBERS: list[str] = _cfg["bad_part_numbers"]
