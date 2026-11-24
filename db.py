"""Shared DuckDB connection utility."""

import logging
import duckdb
from datetime import date
from pathlib import Path
from typing import Optional

DB_PATH = Path(__file__).parent / "data" / "rwa.duckdb"

log = logging.getLogger(__name__)


def get_connection() -> duckdb.DuckDBPyConnection:
    DB_PATH.parent.mkdir(parents=True, exist_ok=True)
    return duckdb.connect(str(DB_PATH))


def append_snapshot(
    con: duckdb.DuckDBPyConnection,
    table_name: str,
    df,  # pd.DataFrame
    snapshot_date: Optional[str] = None,
) -> int:
    """
    Append df to table_name, tagged with snapshot_date (YYYY-MM-DD).

    Behaviour:
    - Adds a 'snapshot_date' column to df before writing.
    - If the table does not exist, creates it (first run).
    - If a snapshot for this date already exists, skips silently (idempotent).
    - On API failure the caller should pass an empty df — the existing history
      is preserved because we never DROP the table.

    Returns the number of rows inserted (0 if skipped or df is empty).
    """
    if snapshot_date is None:
        snapshot_date = date.today().isoformat()

    df = df.copy()
    df["snapshot_date"] = snapshot_date

    # ── Check whether the table exists ───────────────────────────────────────
    table_exists = False
    try:
        con.execute(f"SELECT 1 FROM {table_name} LIMIT 1")
        table_exists = True
    except Exception:
        pass

    if not table_exists:
        if df.empty:
            con.execute(f"CREATE TABLE {table_name} AS SELECT * FROM df")
            log.debug(f"{table_name}: created (empty schema) for {snapshot_date}")
            return 0
        con.execute(f"CREATE TABLE {table_name} AS SELECT * FROM df")
        log.debug(f"{table_name}: created with {len(df)} rows for {snapshot_date}")
        return len(df)

    # ── Table exists — migrate if snapshot_date column is missing ─────────────
    existing_cols = [r[1] for r in con.execute(f"PRAGMA table_info({table_name})").fetchall()]
    if "snapshot_date" not in existing_cols:
        con.execute(f"ALTER TABLE {table_name} ADD COLUMN snapshot_date VARCHAR")
        # Back-fill existing rows with a sentinel so we know they pre-date append mode
        con.execute(f"UPDATE {table_name} SET snapshot_date = 'pre-append' WHERE snapshot_date IS NULL")
        log.info(f"{table_name}: migrated — added snapshot_date column (existing rows tagged 'pre-append')")

    if df.empty:
        return 0

    # ── Idempotency check ─────────────────────────────────────────────────────
    existing_count = con.execute(
        f"SELECT COUNT(*) FROM {table_name} WHERE snapshot_date = ?",
        [snapshot_date],
    ).fetchone()[0]
    if existing_count > 0:
        log.debug(f"{table_name}: snapshot {snapshot_date} already loaded ({existing_count} rows) — skipping")
        return 0

    con.execute(f"INSERT INTO {table_name} SELECT * FROM df")
    return len(df)
