"""Database location and connection helpers."""
from pathlib import Path

import duckdb

ROOT = Path(__file__).resolve().parent.parent
DB_PATH = ROOT / "data" / "uk_monitor.duckdb"
SQL_DIR = ROOT / "sql"


def connect(read_only=False):
    """Open the DuckDB database, creating the data folder if needed."""
    DB_PATH.parent.mkdir(exist_ok=True)
    return duckdb.connect(str(DB_PATH), read_only=read_only)


def ensure_schema(con):
    """Create or update tables and views. Safe to run every time."""
    con.execute((SQL_DIR / "schema.sql").read_text(encoding="utf-8"))
    con.execute((SQL_DIR / "views.sql").read_text(encoding="utf-8"))
