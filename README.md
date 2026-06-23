# CDR Validator

CDR Validator is an AI-powered tool that automates the pre-review of vehicle damage estimates for Corporate Desk Reviewers (CDRs). When a rented vehicle is damaged, a repair workshop submits an estimate through the Vehicle Repair (VR) system. Before approving the repair, a CDR reviewer must validate that the estimate is accurate, the vehicle identity checks out, and the claimed costs are reasonable. This tool automates that validation pipeline so that by the time a reviewer opens a claim, the AI has already verified the VIN, odometer, licence plate, and cost breakdown.

> **Note:** The UI included in this repository is a proof-of-concept illustration dashboard. The intended end-state is for CDR Validator's output to be integrated directly into the existing CDR reviewer dashboard.

---

## Table of Contents

1. [Architecture Overview](#architecture-overview)
2. [System Components](#system-components)
3. [External API Integrations](#external-api-integrations)
4. [Data Flow](#data-flow)
5. [Prerequisites](#prerequisites)
6. [Environment Variables & Configuration](#environment-variables--configuration)
7. [Local Development Setup](#local-development-setup)
8. [Running with Docker](#running-with-docker)
9. [CI/CD Pipelines](#cicd-pipelines)
10. [Troubleshooting](#troubleshooting)

---

## Architecture Overview

CDR Validator is composed of three independent services that share a PostgreSQL database and Azure Blob Storage.

```
┌──────────────────────────────────────────────────────────────────────┐
│                        External Systems                              │
│  ┌─────────────────┐  ┌────────────────┐  ┌──────────────────────┐  │
│  │   VR Services   │  │  CVD (Vehicle  │  │  CSS (Claims Service │  │
│  │  (SOAP / XML)   │  │  Reg. Data)    │  │       System)        │  │
│  │  7 endpoints    │  │  REST / JSON   │  │  SOAP / XML          │  │
│  └────────┬────────┘  └───────┬────────┘  └──────────┬───────────┘  │
└───────────┼───────────────────┼──────────────────────┼──────────────┘
            │                   │                       │
┌───────────▼───────────────────▼───────────────────────▼──────────────┐
│                        vo_pipeline (port: internal)                  │
│                                                                       │
│  Polls VR Services every 10 min for new WAITONAUTH estimates         │
│  → Fetches estimate details, images, CDR rates                       │
│  → Resolves VINs to licence plates via CVD                           │
│  → Downloads claim images from CSS                                   │
│  → Classifies images (VIN / odometer / plate) via Azure AI Vision   │
│  → Extracts values via OCR + LLM                                     │
│  → Verifies extracted values against claim data                      │
│  → Runs estimate matching (parts / labour / paint & materials)       │
└────────────────────────┬──────────────────────────────────────────────┘
                         │ writes results
              ┌──────────▼──────────┐    ┌─────────────────────┐
              │     PostgreSQL      │    │  Azure Blob Storage  │
              │  (processed claims, │    │  (raw images per     │
              │   AI findings)      │    │   estimate)          │
              └──────────┬──────────┘    └─────────────────────┘
                         │ reads
┌────────────────────────▼──────────────────────────────────────────────┐
│                        backend API (port 8018)                        │
│            FastAPI REST API — serves processed estimate data          │
└────────────────────────┬──────────────────────────────────────────────┘
                         │
┌────────────────────────▼──────────────────────────────────────────────┐
│                        frontend UI (port 4200)                        │
│  FastAPI + Jinja2 — illustration dashboard for CDR reviewers         │
│  Auth: Microsoft Entra ID (Azure AD) + Redis session store           │
└───────────────────────────────────────────────────────────────────────┘
```

---

## System Components

### 1. `vo_pipeline` — Data & AI Pipeline

The core processing engine. Runs on a scheduled loop (every 10 minutes) and is responsible for ingesting new estimates, downloading all associated data and images, and running all AI validation steps.

**Location:** `src/vo_pipeline/`  
**Entry point:** `src/vo_pipeline/run.py`

Key sub-services:
- `services/api_ingest/` — API clients for VR Services, CVD, and CSS
- `services/image_processing/` — Azure AI Vision image classification
- `services/ocr/` — OCR extraction of VIN, odometer, and plate values
- `services/estimate_matching/` — Cost validation logic (parts, labour, paint & materials)

### 2. `backend` — REST API

A FastAPI application that exposes processed estimate data stored in PostgreSQL to the frontend and any future integrations.

**Location:** `src/backend/`  
**Entry point:** `src/backend/api/main.py`  
**Port:** `8018`

### 3. `frontend` — Illustration Dashboard

A FastAPI + Jinja2 web application that allows CDR reviewers to browse processed estimates and AI validation results. Authentication is handled via Microsoft Entra ID with Redis-backed sessions.

**Location:** `src/frontend/`  
**Entry point:** `src/frontend/ui/main.py`  
**Port:** `4200`

---

## External API Integrations

The pipeline integrates with four external systems. All calls are rate-limited to 30 requests/second globally and use a 3-attempt exponential backoff retry strategy.

### VR Services (Vehicle Repair) — SOAP/XML

The primary data source. All calls share a single base URL authenticated via an AppSec XML token.

| Endpoint | Purpose |
|---|---|
| `AuthenticateUserRQ` | Obtain session token for all subsequent VR calls |
| `SearchEstimateRQ` | Poll for new estimates in `WAITONAUTH` status for the `DR` group |
| `GetEstimateDetailForSubtotalsRQ` | Fetch estimate header: vendor, group, dates, manual estimate flag |
| `GetElectronicEstimateRQ` | Fetch full estimate XML: line items, vehicle info, all damage details |
| `GetCDRGroupVendorRQ` | Fetch contracted repair rates for a vendor/group (labour rates, parts discounts, CDR thresholds, fees) — LRU-cached |
| `SearchRepairIncidentRQ` | Fetch damage description text for an estimate |
| `GetAttachmentsForEstimateRQ` | List all image attachments for an estimate |
| `GetAttachmentBytesRQ` | Download binary image data for a single attachment |

**Caller identity:** `SVC_AI_VEH_REPAIR` (AppStaticId: `759935158`)

### CVD (Connected Vehicle Data) — REST/JSON

Used to resolve VINs to current vehicle registration plate numbers. Calls are batched in groups of 50 VINs (API hard cap: 100).

| Endpoint | Purpose |
|---|---|
| `POST /auth` | Obtain JWT token (password is MD5-hashed per API requirement) |
| `POST /fleetVehicle/search` | Look up registration plates for a batch of VINs |

Audience: `com.ehi.vehicle`  
Accept header: `application/prs.com-ehi.vehicle.fleetVehicle.salesVehicleDetails+json; version=2.12.0`

### CSS (Claims Service System) — SOAP/XML

Provides claim-related documents and images. Uses a separate AppSec token from VR Services.

| Endpoint | Purpose |
|---|---|
| `AuthenticateUserRQ` | Obtain CSS session token (AppStaticId: `388582721`) |
| `DocumentSearchRQ` | List all documents for a claim (filters TagId 1 and 12; excludes 53 and 57) |
| `GetDocumentRQ` | Download binary document/image data |

**Caller identity:** `E192G5`, calling process `VEHREPR_DESKTOP`  
**Timeout:** 90 seconds (documents can reach ~6 MB)

### Azure Services

| Service | Purpose |
|---|---|
| Azure Blob Storage | Stores raw estimate and claim images organised by estimate ID |
| Azure AI Vision | Classifies images to identify which contain VIN, odometer, or licence plate |
| Azure Key Vault | Stores all credentials and sensitive configuration values |

---

## Data Flow

The pipeline runs every 10 minutes and processes each new estimate through the following stages:

```
Stage 1 — Authentication
  Obtain tokens: VR Services, CSS (optional), CVD (optional)
  └─ If VR auth fails → pipeline stops
  └─ If CSS or CVD auth fails → those enrichment steps are skipped gracefully

Stage 2 — Estimate Discovery
  SearchEstimates(status=WAITONAUTH, group=DR)
  └─ Filters to only estimates not yet seen in this run
  └─ Saves initial estimate records to PostgreSQL

Stage 3 — Detail Fetch (parallelised, up to 4 workers)
  Per estimate:
  ├─ GetElectronicEstimateRQ        → full damage line items, vehicle info
  ├─ GetEstimateDetailForSubtotals  → vendor, group, dates
  ├─ SearchRepairIncidentRQ         → damage description text
  └─ GetCDRGroupVendorRQ            → contracted rates (LRU-cached)

Stage 4 — CVD Enrichment
  Batch all new VINs → CVD Fleet Vehicle Search
  └─ Maps each VIN to its current registration plate
  └─ Falls back to plate from estimate data if CVD is unavailable

Stage 5 — Image Ingestion (parallelised, up to 6 image workers)
  Per estimate:
  ├─ GetAttachmentsForEstimate + GetAttachmentBytes → estimate images
  ├─ DocumentSearch + GetDocument (CSS)             → claim images
  └─ All images uploaded to Azure Blob Storage: EST{est_id}/{filename}
     (claim images suffixed with _cl to distinguish source)

Stage 6 — Image Classification
  Azure AI Vision classifies each image:
  └─ Identifies which images contain: VIN plate, odometer, licence plate

Stage 7 — OCR + LLM Extraction & Verification
  For each classified image:
  ├─ OCR extracts raw text (VIN, odometer reading, plate number)
  └─ LLM verifies extracted values against claim data

Stage 8 — Estimate Matching
  Validates costs from the electronic estimate XML:
  ├─ Parts cost (domestic, foreign, KYLS discounts)
  ├─ Labour cost (body, mechanical, frame, aluminium rates)
  └─ Paint & materials charges
  Compared against contracted CDR rates from GetCDRGroupVendor
```

---

## Prerequisites

Before running CDR Validator locally, you need:

- **Docker** (v24+) and **Docker Compose** (v2.20+)
- **Python 3.12.3** (if running outside Docker)
- **Access to the internal Artifactory registry** — Docker base images and Python packages are pulled from private registries; you need a valid `pip.conf` with credentials
- **Azure access** with the following:
  - Permission to read from the project's **Azure Key Vault** — your identity needs the `Key Vault Secrets User` role on the vault
  - Access to the **Azure Container Registry** — `acrcentralusvodev.azurecr.io` (dev)
  - An Azure identity (user or managed identity) authenticated via `az login`
- **Microsoft Entra ID app registration** — required for frontend authentication (client ID, tenant ID, client secret)
- Network access to:
  - VR Services SOAP endpoint
  - CVD REST endpoint
  - CSS SOAP endpoint
  - PostgreSQL instance
  - Redis instance (frontend only)

---

## Environment Variables & Configuration

All three services use Pydantic Settings with the following resolution order (highest to lowest priority):

1. **Azure Key Vault** — fields listed in `VAULT_FIELDS` are fetched from the vault at startup
2. **Environment variables**
3. **`.env` file** in the service root

Set `VAULT_URL` to point to the correct Key Vault. All sensitive values (passwords, connection strings, client secrets) are expected to exist as secrets in that vault and do **not** need to be set in `.env`.

---

### `vo_pipeline` configuration

| Variable | Source | Description |
|---|---|---|
| `VAULT_URL` | Env / `.env` | Azure Key Vault URL, e.g. `https://<vault-name>.vault.azure.net/` |
| `APP_ENV` | Env / `.env` | Deployment environment: `SANDBOX`, `NONPROD`, or `PRODUCTION` |
| `AUTH_URL` | Env / `.env` | VR Services authentication endpoint URL |
| `API_BASE_URL` | Env / `.env` | VR Services SOAP base URL |
| `CVD_AUTH_URL` | Env / `.env` | CVD authentication endpoint URL |
| `CVD_API_URL` | Env / `.env` | CVD vehicle search endpoint URL |
| `CSS_API_URL` | Env / `.env` | CSS SOAP endpoint URL |
| `POSTGRES_HOST` | Env / `.env` | PostgreSQL hostname or IP |
| `POSTGRES_PORT` | Env / `.env` | PostgreSQL port (default: `5432`) |
| `POSTGRES_DB` | Env / `.env` | PostgreSQL database name |
| `POSTGRES_USER` | Env / `.env` | PostgreSQL username |
| `AZURE_VISION_ENDPOINT` | Env / `.env` | Azure AI Vision endpoint URL |
| `OPENAI_API_BASE` | Env / `.env` | Azure OpenAI endpoint URL |
| `DATABRICKS_HOST` | Env / `.env` | Databricks workspace URL |
| `DATABRICKS_CLIENT_ID` | Env / `.env` | Databricks OAuth client ID |
| `ICE_API_USER_NAME` | Key Vault | Username for VR Services and CSS authentication |
| `SVC_AI_VEH_REPAIR_PASSWORD` | Key Vault | Password for VR Services authentication |
| `CSS_PASSWORD` | Key Vault | Password for CSS authentication |
| `CVD_LOGON_ID` | Key Vault | Login ID for CVD authentication |
| `CVD_PASSWORD` | Key Vault | Password for CVD (MD5-hashed before use per API requirement) |
| `POSTGRESQL_DEV_PASSWORD` | Key Vault | PostgreSQL password |
| `AZURE_STORAGE_CONNECTION_STRING` | Key Vault | Azure Blob Storage connection string |
| `AZURE_VISION_KEY` | Key Vault | Azure AI Vision API key |
| `OPENAI_API_KEY` | Key Vault | Azure OpenAI API key |
| `DATABRICKS_CLIENT_SECRET` | Key Vault | Databricks OAuth client secret |

---

### `backend` configuration

| Variable | Source | Description |
|---|---|---|
| `VAULT_URL` | Env / `.env` | Azure Key Vault URL |
| `APP_ENV` | Env / `.env` | `SANDBOX`, `NONPROD`, or `PRODUCTION` |
| `POSTGRES_HOST` | Env / `.env` | PostgreSQL hostname or IP |
| `POSTGRES_PORT` | Env / `.env` | PostgreSQL port |
| `POSTGRES_DB` | Env / `.env` | PostgreSQL database name |
| `POSTGRES_USER` | Env / `.env` | PostgreSQL username |
| `ENTRA_TENANT_ID` | Env / `.env` | Azure AD tenant ID for JWT validation |
| `ENTRA_CLIENT_ID` | Env / `.env` | Azure AD app client ID for JWT validation |
| `AZURE_STORAGE_ACCOUNT_NAME` | Env / `.env` | Azure Blob Storage account name |
| `AZURE_STORAGE_CONTAINER_NAME` | Env / `.env` | Blob container name for images |
| `POSTGRESQL_DEV_PASSWORD` | Key Vault | PostgreSQL password |

---

### `frontend` configuration

| Variable | Source | Description |
|---|---|---|
| `VAULT_URL` | Env / `.env` | Azure Key Vault URL |
| `APP_ENV` | Env / `.env` | `SANDBOX`, `NONPROD`, or `PRODUCTION` |
| `ENTRA_TENANT_ID` | Env / `.env` | Azure AD tenant ID |
| `ENTRA_CLIENT_ID` | Env / `.env` | Azure AD app registration client ID |
| `BACKEND_URL` | Env / `.env` | URL of the backend API, e.g. `http://localhost:8018` |
| `REDIS_HOST` | Env / `.env` | Redis hostname for session storage |
| `REDIS_PORT` | Env / `.env` | Redis port (default: `6379`) |
| `ENTRA_CLIENT_SECRET` | Key Vault | Azure AD app registration client secret |
| `SESSION_SECRET_KEY` | Key Vault | Secret key for signing session cookies |

---

## Local Development Setup

### 1. Clone the repository

```bash
git clone <repo-url>
cd em-main
```

### 2. Configure pip for the private registry

All Python packages are served from an internal Artifactory instance. Create or update `~/.pip/pip.conf` with the credentials provided by your team:

```ini
[global]
index-url = https://<artifactory-host>/artifactory/api/pypi/<repo>/simple
trusted-host = <artifactory-host>
```

### 3. Authenticate with Azure

Log in with an identity that has `Key Vault Secrets User` on the project vault:

```bash
az login
az account set --subscription <subscription-id>
```

The application uses `DefaultAzureCredential`, so a successful `az login` is sufficient for local development — no further credential configuration is needed.

### 4. Create `.env` files

Create a `.env` file in each service directory and populate the non-vault variables. Vault-sourced variables are fetched automatically at startup and do not need to appear in `.env`.

**`src/backend/.env`**
```env
VAULT_URL=https://<vault-name>.vault.azure.net/
APP_ENV=SANDBOX
POSTGRES_HOST=<host>
POSTGRES_PORT=5432
POSTGRES_DB=<db-name>
POSTGRES_USER=<username>
ENTRA_TENANT_ID=<tenant-id>
ENTRA_CLIENT_ID=<client-id>
AZURE_STORAGE_ACCOUNT_NAME=<account-name>
AZURE_STORAGE_CONTAINER_NAME=<container-name>
```

**`src/frontend/.env`**
```env
VAULT_URL=https://<vault-name>.vault.azure.net/
APP_ENV=SANDBOX
ENTRA_TENANT_ID=<tenant-id>
ENTRA_CLIENT_ID=<client-id>
BACKEND_URL=http://localhost:8018
REDIS_HOST=localhost
REDIS_PORT=6379
```

**`src/vo_pipeline/.env`**
```env
VAULT_URL=https://<vault-name>.vault.azure.net/
APP_ENV=SANDBOX
AUTH_URL=<vr-services-auth-url>
API_BASE_URL=<vr-services-soap-url>
CVD_AUTH_URL=<cvd-auth-url>
CVD_API_URL=<cvd-api-url>
CSS_API_URL=<css-soap-url>
POSTGRES_HOST=<host>
POSTGRES_PORT=5432
POSTGRES_DB=<db-name>
POSTGRES_USER=<username>
AZURE_VISION_ENDPOINT=<vision-endpoint>
OPENAI_API_BASE=<openai-endpoint>
DATABRICKS_HOST=<databricks-host>
DATABRICKS_CLIENT_ID=<client-id>
```

### 5. Run each service

Each service has its own virtual environment. Run these in separate terminals.

**Backend:**
```bash
cd src/backend
python -m venv .venv
source .venv/bin/activate
pip install -r requirements/requirements.txt
uvicorn api.main:app --host 0.0.0.0 --port 8018 --reload
```

**Frontend:**
```bash
cd src/frontend
python -m venv .venv
source .venv/bin/activate
pip install -r requirements/requirements.txt
# Redis must be running before starting the frontend
uvicorn ui.main:app --host 0.0.0.0 --port 4200 --reload
```

**Pipeline:**
```bash
cd src/vo_pipeline
python -m venv .venv
source .venv/bin/activate
pip install -r requirements/requirements.txt
python run.py
```

> **Redis for frontend sessions:** If you don't have Redis running locally, spin one up with `docker run -d -p 6379:6379 redis:7`.

---

## Running with Docker

### Log in to the container registry

```bash
az acr login --name acrcentralusvodev
```

### Build and run all services

```bash
docker compose -f deployment/docker-compose/base.yaml up --build
```

This starts the backend (port 8018) and frontend (port 4200). The pipeline is run separately as it is a scheduled worker rather than a persistent server.

### Build individual images

```bash
# Backend
docker build --secret id=pip_conf,src=/etc/pip.conf \
  -f src/backend/Dockerfile -t cdr-validator-backend:local src/backend

# Frontend
docker build --secret id=pip_conf,src=/etc/pip.conf \
  -f src/frontend/Dockerfile -t cdr-validator-frontend:local src/frontend

# Pipeline
docker build --secret id=pip_conf,src=/etc/pip.conf \
  -f src/vo_pipeline/Dockerfile -t cdr-validator-pipeline:local src/vo_pipeline
```

The Dockerfiles pass `pip.conf` as a build secret so Artifactory credentials are not baked into the image layer. Ensure the file exists at `/etc/pip.conf` on the build host (or adjust the `src=` path accordingly).

---

## CI/CD Pipelines

Pipelines are defined in `deployment/pipelines/` and run on Azure DevOps.

### Pull request validation

Runs on every PR targeting `main`:

| Pipeline | File | Checks |
|---|---|---|
| Backend PR | `backend-pr.yml` | Lint (ruff), format check (black), SAST (bandit) |
| Frontend PR | `frontend-pr.yaml` | Lint (ruff), format check (black), SAST (bandit) |

### Continuous integration

Runs on merge to `main`:

| Pipeline | File | Steps |
|---|---|---|
| Backend CI | `backend-ci.yml` | Build Docker image → push to dev ACR → push to prod ACR |
| Frontend CI | `frontend-ci.yml` | Build Docker image → push to dev ACR → push to prod ACR |

### Continuous deployment

CD pipelines (`backend-cd.yml`, `frontend-cd.yml`) deploy to the target environment after the CI image push completes.

### Running checks locally

```bash
# From any service directory
cd src/backend

black .               # auto-format
ruff check .          # lint
bandit -r api/        # security static analysis
```

---

## Troubleshooting

### Key Vault access denied at startup

```
azure.core.exceptions.ClientAuthenticationError
```

Your identity is not authenticated with Azure or does not have the `Key Vault Secrets User` role on the vault. Run `az login` and confirm the active subscription with `az account show`.

---

### Pipeline finds no new estimates

The pipeline filters for estimates in `WAITONAUTH` status assigned to group `DR`. If none appear:
- Confirm VR Services authentication succeeded (check logs for the `AuthenticateUserRQ` call)
- Estimates already saved to PostgreSQL are deduplicated — check whether they were processed in a prior run
- Confirm there are genuinely new estimates in the environment for that status and group

---

### CVD token failure — plates fall back to estimate data

```
WARNING - CVD authentication failed, falling back to estimate plate data
```

This is a graceful degradation. The pipeline continues using the plate number recorded in the VR estimate. If this persists, check the `CVD_LOGON_ID` and `CVD_PASSWORD` secrets in Key Vault.

---

### CSS image fetch skipped

```
WARNING - CSS token not available, skipping claim image upload for est_id=...
```

CSS authentication failed at startup. Claim images will not be downloaded for this pipeline run; VR estimate images are unaffected. Check the `CSS_PASSWORD` secret in Key Vault.

---

### Frontend login loop (Entra ID redirect never completes)

- Confirm `ENTRA_CLIENT_ID`, `ENTRA_TENANT_ID`, and `ENTRA_CLIENT_SECRET` are set correctly
- Confirm the redirect URI registered in the Azure AD app registration matches the URL you are accessing (e.g. `http://localhost:4200/auth/callback`)
- Confirm Redis is running — without it, sessions cannot be stored and every request re-triggers the login flow

---

### PostgreSQL connection refused

Check that `POSTGRES_HOST`, `POSTGRES_PORT`, `POSTGRES_DB`, and `POSTGRES_USER` are set in `.env` and that `POSTGRESQL_DEV_PASSWORD` is accessible from Key Vault. The backend maintains a connection pool (`min_size=2, max_size=10`) — if all connections are held, check for long-running or stuck queries.

---

### Rate limit errors against VR Services / CVD / CSS

All external API calls share a global rate limiter of 30 calls/second. If you are seeing `429` responses or timeout spikes during a large batch run, reduce `API_MAX_WORKERS` (estimate-level parallelism, default 4) and `API_IMAGE_WORKERS` (image-level parallelism, default 6) in the pipeline settings.
