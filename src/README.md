# EH Vehicle Repair — CDR Approval Validator

An enterprise claims management system that automates vehicle repair estimate validation using AI-powered image classification, OCR, LLM-based parts discount auditing, and rule-based labour/materials matching.

---

## Overview

The CDR Approval Validator processes vehicle insurance repair estimates by running three coordinated pipelines:

1. **API Ingestion** — Fetches new estimates from the VR Services SOAP API, parses electronic estimate XML, and stages data in PostgreSQL
2. **Vehicle Verification (VI)** — Classifies vehicle images using OCR + VLM to verify VIN, license plate, and odometer against claim records
3. **Estimate Matching (EM)** — Audits repair estimates against CDR contracted rates for parts discounts, labour rates, and materials costs

Results are surfaced via a FastAPI backend and Streamlit frontend for claims adjusters to review and approve.

---

## Architecture

```
┌──────────────────────┐        ┌──────────────────┐        ┌──────────────────────┐
│  FastAPI + Jinja2 UI │ ──────▶│  FastAPI Backend  │ ──────▶│  Azure PostgreSQL DB │
│  (HTML templates)    │        │  (Port 8018)      │        │  (asyncpg pool)      │
└──────────────────────┘        └──────────────────┘        └──────────────────────┘
                                    │
                                    ▼
                           ┌──────────────────┐
                           │  Azure Blob       │
                           │  Storage          │
                           │  (claim images)   │
                           └──────────────────┘

                    ┌─────────────────────────────┐
                    │    vo_pipeline (batch)       │
                    │  api_ingest → VI → EM        │
                    └─────────────────────────────┘
```

**Tech Stack:**
- **Frontend:** FastAPI + Jinja2 (server-side rendered HTML templates)
- **Backend:** FastAPI + Uvicorn (async)
- **Database:** Azure PostgreSQL via asyncpg (backend) and SQLAlchemy/psycopg2 (pipeline)
- **Storage:** Azure Blob Storage
- **AI/ML:** Azure OpenAI (GPT-4 for parts audit), Azure Vision AI (OCR), VLM classifier
- **Infrastructure:** Azure Key Vault, Azure Container Registry, Docker, Azure Pipelines

---

## Project Structure

```
src/
├── backend/                        # FastAPI REST API
│   ├── api/
│   │   ├── main.py                 # App entry point & routes
│   │   ├── settings.py             # Config via Azure Key Vault
│   │   ├── models/schemas.py       # Pydantic response models
│   │   └── services/
│   │       ├── db.py               # asyncpg connection pool
│   │       ├── backend_queries.py  # SQL queries
│   │       └── incident_orchestrator.py  # Business logic / response shaping
│   ├── requirements/
│   └── Dockerfile
│
├── frontend/                       # Streamlit UI
│   ├── ui/
│   │   ├── main.py
│   │   ├── settings.py
│   │   └── styles.css
│   ├── requirements/
│   └── Dockerfile
│
└── vo_pipeline/                    # Batch data pipeline
    └── services/
        ├── config.yaml             # Unified config for all three pipelines
        ├── sql_connection.py       # Shared SQLAlchemy engine
        ├── settings.py             # Azure Key Vault secrets
        ├── pipeline_orchestrator.py  # Orchestrates all three pipeline steps
        │
        ├── api_ingest/             # Step 1 — Fetch estimates from SOAP API
        │   ├── estimate_client.py          # SOAP API client (7 endpoints)
        │   ├── estimate_loader.py          # Search + fetch + save estimates
        │   ├── electronic_estimate_parser.py  # XML → DataFrame
        │   ├── api_ingestion_pipeline.py   # Entry point
        │   ├── db_staging.py               # Staging table DDL
        │   └── api_auth.py                 # Token auth
        │
        ├── vehicle_verification/   # Step 2 — Verify vehicle images
        │   ├── vi_pipeline.py      # Per-estimate entry point
        │   ├── processing.py       # Image processing orchestration
        │   ├── ocr.py              # Azure Vision OCR
        │   ├── vlm_classifier.py   # VLM image classification
        │   └── db_writer.py        # VI output table DDL + upsert logic
        │
        └── estimate_matching/      # Step 3 — Audit estimate vs CDR rates
            ├── em_pipeline.py      # Orchestrator: run_em_pipeline()
            ├── parts_audit.py      # LLM discount audit + parts subtotal matching
            ├── labor_audit.py      # Labour hours/rate/amount matching
            ├── material_audit.py   # Paint-and-materials rate matching
            ├── llm.py              # LLM client, prompt, JSON encoder
            ├── helpers.py          # cast_numeric()
            ├── config.py           # Config constants (rates, filters, etc.)
            ├── db_writer.py        # EM output table DDL + save_results()
            └── query_table.py      # Live DB data loader (prefiltered mode)
```

---

## Database Schema

### Staging Tables (`container` schema — written by api_ingest)

| Table | Description |
|-------|-------------|
| `backup_api_est_imgs` | One row per estimate — search result metadata |
| `backup_api_est_line` | One row per estimate line item — parsed from electronic estimate XML + CDR rates |
| `backup_api_est_subtot` | One row per subtotal category per estimate |

### EM Output Tables (`container` schema — written by estimate_matching)

| Table | Description |
|-------|-------------|
| `backup_api_em_est_summary` | One row per estimate — pass/fail verdicts + issue counts |
| `backup_api_em_est_line_dtl` | One row per line item — LLM parts discount audit results |
| `backup_api_em_est_subtot_dtl` | One row per subtotal type — parts/labour/materials matching results |
| `backup_api_est_overall_results` | One row per estimate — combined EM + VI verdict |

### VI Output Tables (`container` schema — written by vehicle_verification)

| Table | Description |
|-------|-------------|
| `backup_api_vi_estimate_results` | One row per estimate folder — VIN/plate/odometer match results |
| `backup_api_vi_image_results` | One row per image — OCR text, VLM classification, match details |

---

## Estimate Matching — Audit Domains

### Parts Audit (`parts_audit.py`)
- Filters eligible parts lines (excludes existing parts, bad part numbers)
- Sends lines to GPT-4 LLM which determines the applicable discount % from CDR special instructions or structured rates
- Computes `expected_discount_amt`, `actual_discount_amt`, `discount_match`, `discount_pct_match`, `discount_pass`
- Validates parts subtotals: gross/adj/net amounts + `adj_pct` vs expected CDR rate

### Labour Audit (`labor_audit.py`)
- Validates Body / Mechanical / Frame / Glass labour via groupby aggregation
- Validates Refinish labour separately (sourced from `paint_hrs` where `paint_type_code='R'`)
- Per labour type: `lbr_typ_hrs_match`, `lbr_typ_rate_match`, `lbr_amt_match`, `overall_lbr_match`, `lbr_mismatch_reason`
- Line-level: `actual_line_lbr_rate`, `expected_line_lbr_rate`, `line_lbr_rate_match`

### Materials Audit (`material_audit.py`)
- Aggregates all "paint + material" subtotal rows (covers 2-stage, 3-stage, etc.)
- Computes `actual_paint_rate = paint_tot_amt / refinish_hrs` vs CDR `pnt_mtrl_rate`
- Reports `paint_rate_match`, `paint_rate_direction`, `expected_paint_amt`, `paint_amt_match`

---

## Configuration

All three pipelines share a single `config.yaml` at `services/config.yaml`. Secrets are loaded from Azure Key Vault at startup via `settings.py`.

Key config sections:

| Section | Description |
|---------|-------------|
| `run` | Feature flags: `ingestion`, `vehicle_verification`, `estimate_matching`, `reset_staging_tables`, `reset_output_tables` |
| `tables` | Schema and table names for all staging/output tables |
| `api_ingest` | Status code filter, group, worker counts |
| `vehicle_verification` | Input columns, processing config, parallelism |
| `estimate_matching` | Data source mode, ICE table names, filter conditions, CDR rate maps |

---

## Running the Pipeline

```bash
cd src/vo_pipeline
python run.py
```

The orchestrator (`pipeline_orchestrator.py`) runs the three steps sequentially per estimate, with VI and EM running in parallel across estimates via `ThreadPoolExecutor`.

**Reset tables (dev/test):** Set `reset_staging_tables: true` and `reset_output_tables: true` in `config.yaml`.

**Skip ingestion (test against saved data):** Set `ingestion: false` in the `run` section — the pipeline reads est_ids directly from the staging table.

---

## Local Development

**Backend:**
```bash
cd src/backend
make install
make lint
make format-check
make sast
uvicorn api.main:app --host 0.0.0.0 --port 8018 --reload
```

**Frontend:**
```bash
cd src/frontend
make install
make lint
# Change BACKEND_URL in ui/settings.py from http://backend:8018 to http://localhost:8018
uvicorn ui.main:app --host 0.0.0.0 --port 4200 --reload
```

Services will be available at:
- Frontend: `http://localhost:4200`
- Backend: `http://localhost:8018`

---

## Security

- All secrets managed exclusively via Azure Key Vault — no hardcoded credentials
- SQL injection prevention via SQLAlchemy parameterised queries (`text()` with named params)
- Bandit SAST scan on every build (`make sast`)

---

## Before Pushing

```bash
make format && make format-check && make lint && make python-sast
```
