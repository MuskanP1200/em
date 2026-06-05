
# EH Vehicle Repair — CDR Approval Validator

An enterprise claims management system that automates vehicle repair estimate validation using AI-powered image classification, OCR, and discount matching.

---

## Overview

The CDR Approval Validator processes vehicle insurance claims by:

- Classifying uploaded vehicle images (VIN plates, license plates, odometers, etc.) using Vision Language Models (VLM)
- Extracting text via OCR and cross-referencing against claim records
- Detecting VIN mismatches with character-level comparison
- Matching parts estimates against discount catalogs

---

## Architecture

```
┌─────────────────┐        ┌──────────────────┐        ┌──────────────────────┐
│  Streamlit UI   │ ─────▶│  FastAPI Backend  │ ────▶│  Azure PostgreSQL DB │
│  (Port 4200)    │        │  (Port 8018)     │       │  (asyncpg pool)      │
└─────────────────┘        └──────────────────┘        └──────────────────────┘
                                    │
                                    ▼
                           ┌──────────────────┐
                           │  Azure Blob       │
                           │  Storage          │
                           │  (claims-images)  │
                           └──────────────────┘
```

**Tech Stack:**
- **Frontend:** Streamlit 1.54
- **Backend:** FastAPI 0.128 + Uvicorn (async)
- **Database:** Azure PostgreSQL via asyncpg
- **Storage:** Azure Blob Storage
- **AI/ML:** OpenAI GPT (VLM), Azure Vision AI, Azure Postgres
- **Infrastructure:** Azure (Key Vault, Container Registry), Docker, Azure Pipelines

---

## Project Structure

```
.
├── src/
│   ├── backend/                    # FastAPI REST API
│   │   ├── api/
│   │   │   ├── main.py             # App entry point & routes
│   │   │   ├── settings.py         # Config via Azure Key Vault
│   │   │   ├── models/schemas.py   # Pydantic response models
│   │   │   └── services/
│   │   │       ├── db.py                          # DB connection pool
│   │   │       ├── vehicle_verification/          # VIN, license, odometer checks
│   │   │       └── estimate_matching/             # Discount matching service
│   │   ├── requirements/
│   │   └── Dockerfile
│   │
│   ├── frontend/                   # Streamlit UI
│   │   ├── ui/
│   │   │   ├── main.py             # App entry point
│   │   │   ├── settings.py         # Frontend config
│   │   │   └── styles.css
│   │   ├── requirements/
│   │   └── Dockerfile
│   │
│   └── vo_pipeline/             # Data pipeline components
│       ├── discount_matching/      # Discount consolidation & profiling
│       └── veh_ind_pipeline/       # VLM image classification pipeline
│
├── deployment/
│   ├── docker-compose/base.yaml    # Docker Compose config
│   └── pipelines/                  # Azure CI/CD pipeline definitions
│
├── analysis_scripts/               # Team analysis scripts
│   ├── muskan/
│   ├── piyush/
│   └── samvit/
│
└── Makefile                        # Root build orchestration
```

---

## API Endpoints

| Method | Endpoint | Description |
|--------|----------|-------------|
| `GET` | `/` | Health check |
| `GET` | `/folders` | List claim folders with processing status |
| `GET` | `/folders/{folder}/est_info` | Estimate metadata (VIN, license, YMMS) |
| `GET` | `/folders/{folder}/vlm-stats` | VLM classification statistics |
| `GET` | `/folders/{folder}/images` | Paginated images with base64 encoding |
| `GET` | `/folders/{folder}/parts` | Parts list for estimate |
| `GET` | `/folders/{folder}/discount` | Discount matching results |

---

## Getting Started

### Prerequisites

- Docker & Docker Compose
- Access to Azure resources (Key Vault, PostgreSQL, Blob Storage, ACR)
- Python 3.12 (for local development)

### Local Development

**Backend:**
```bash
cd src/backend
make install       # Install dependencies
make lint          # Run ruff linter
make format-check  # Check formatting with black
make sast          # Run bandit security scan
```

**Frontend:**
```bash
cd src/frontend
make install
make lint
```

### Building & Pushing to ACR

Coming soon

---

## Configuration

Configuration is managed via Pydantic Settings with Azure Key Vault as the secrets backend. Settings are cached with a 5-minute TTL.

Key environment variables:

| Variable | Description |
|----------|-------------|
| `AZURE_KEY_VAULT_URL` | Azure Key Vault endpoint |
| `ENVIRONMENT` | `development` / `staging` / `production` |
| `BACKEND_URL` | Backend API URL (used by frontend) |

---

## Data Models

**Image Classification Labels (`ClassifiedLabel`):**
- `VIN` — Vehicle Identification Number plate
- `License Plate` — License plate image
- `Odometer` — Odometer reading
- `Other` — Uncategorized image

**Key Schemas:**
- `FolderOut` — Claim folder with processing status flags
- `EstInfoOut` — Estimate metadata (VIN, license plate, claim numbers, year/make/model/style)
- `VLMStatsOut` — Breakdown of classified image counts
- `ImageDetailOut` — Image with base64 data, classification label, OCR status

---
## Run Locally


 
**Terminal 1 — Backend:**
```bash
cd src/backend
make install
uvicorn api.main:app --host 0.0.0.0 --port 8018 --reload
```
 
**Terminal 2 — Frontend:**
```bash
cd src/frontend
make install
```
 
Before starting the frontend, open `src/frontend/ui/settings.py` and change the `BACKEND_URL` default from `http://backend:8018` to `http://localhost:8018`:
 
```python
# src/frontend/ui/settings.py
BACKEND_URL: str = http://localhost:8018 # change from http://backend:8018
```
 
Then run:
```bash
streamlit run ui/main.py
```
 
Services will be available at:
- Frontend: `http://localhost:8501`
- Backend: `http://localhost:8018`
 
### Running with Docker Compose
 
Coming soon

---

## CI/CD

Coming soon

---

## Build and Test

Coming soon

---

## Security

- Secrets managed exclusively via Azure Key Vault (no hardcoded credentials)



----

#adrien's reference


# Introduction 
TODO: Give a short introduction of your project. Let this section explain the objectives or the motivation behind this project. 

# Getting Started
TODO: Guide users through getting your code up and running on their own system. In this section you can talk about:
1.	Installation process
2.	Software dependencies
3.	Latest releases
4.	API references

# Build and Test
TODO: Describe and show how to build your code and run the tests. 

# Contribute
TODO: Explain how other users and developers can contribute to make your code better. 

If you want to learn more about creating good readme files then refer the following [guidelines](https://docs.microsoft.com/en-us/azure/devops/repos/git/create-a-readme?view=azure-devops). You can also seek inspiration from the below readme files:
- [ASP.NET Core](https://github.com/aspnet/Home)
- [Visual Studio Code](https://github.com/Microsoft/vscode)
- [Chakra Core](https://github.com/Microsoft/ChakraCore)

NOTE- Run before pushing code or creating PR

```bash
make format && make format-check && make lint && make python-sast
```
