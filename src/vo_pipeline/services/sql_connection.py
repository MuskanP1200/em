import sys
from pathlib import Path

import pandas as pd
from sqlalchemy import create_engine, Engine, text

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from settings import get_settings  # noqa: E402

_engine: Engine | None = None


def get_engine() -> Engine:
    global _engine
    if _engine is None:
        s = get_settings()
        url = (
            f"postgresql+psycopg2://{s.POSTGRES_USER}:{s.POSTGRESQL_PASSWORD}@"
            f"{s.POSTGRES_HOST}:{s.POSTGRES_PORT}/{s.POSTGRES_DB}"
        )
        _engine = create_engine(url)
    return _engine


# ── DB HELPERS ───────────────────────────────────────────────────────────────────


def read_table(table_name: str) -> pd.DataFrame:
    df = pd.read_sql(text(f"SELECT * FROM {table_name}"), get_engine())  # nosec B608
    df.columns = df.columns.str.lower()
    return df


def read_query(query: str) -> pd.DataFrame:
    df = pd.read_sql(query, get_engine())
    df.columns = df.columns.str.lower()
    return df


def build_in_clause(values: list) -> str:
    return ", ".join(f"'{v}'" for v in values)


def update_rows(
    schema: str,
    table: str,
    rows: list[dict],
    match_col: str = "est_id",
) -> None:
    """Bulk-update rows by match_col. Each dict in rows must contain match_col
    plus whatever columns to update."""
    if not rows:
        return
    set_cols = [k for k in rows[0] if k != match_col]
    set_clause = ", ".join(f"{c} = :{c}" for c in set_cols)
    sql = text(  # nosec B608
        f"UPDATE {schema}.{table} SET {set_clause} WHERE {match_col} = :{match_col}"  # nosec B608
    )  # nosec B608
    with get_engine().begin() as conn:
        conn.execute(sql, rows)


def write_table(
    df: pd.DataFrame,
    table_name: str,
    schema: str = "public",
    if_exists: str = "replace",
) -> None:
    with get_engine().begin() as conn:
        df.to_sql(
            table_name,
            con=conn,
            schema=schema,
            if_exists=if_exists,
            index=False,
            chunksize=5000,
        )
