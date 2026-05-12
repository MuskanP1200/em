#!/usr/bin/env python3
"""
Entry point for the VO Pipeline worker.

This script runs the pipeline orchestrator, which executes:
1. API ingestion (fetch estimates from ICE API)
2. Vehicle verification (verify VIN/plates with images)
3. Estimate matching (validate parts/labour with CDR rates)

Run with:
    python run.py
"""

import sys
from pathlib import Path

# Add services directory to path so imports work
sys.path.insert(0, str(Path(__file__).resolve().parent / "services"))

from pipeline_orchestrator import run_pipeline


if __name__ == "__main__":
    run_pipeline()
