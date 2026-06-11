"""
helpers.py
──────────
Shared utility functions used across the estimate matching pipeline.
"""

from __future__ import annotations

import pandas as pd


def cast_numeric(df: pd.DataFrame, cols: list[str]) -> pd.DataFrame:
    """Coerce specified columns to numeric, setting invalid values to NaN."""
    for col in cols:
        df[col] = pd.to_numeric(df[col], errors="coerce")
    return df
