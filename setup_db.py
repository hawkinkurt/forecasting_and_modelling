"""Create (or update) the database tables and views. Safe to re-run."""
from pathlib import Path

import duckdb

ROOT = Path(__file__).parent
DB_PATH = ROOT / "data" / "uk_monitor.duckdb"


def main():
    DB_PATH.parent.mkdir(exist_ok=True)
    con = duckdb.connect(str(DB_PATH))
    con.execute((ROOT / "sql" / "schema.sql").read_text())
    con.execute((ROOT / "sql" / "views.sql").read_text())
    print(con.execute("SELECT table_name, table_type FROM information_schema.tables ORDER BY 1").fetchall())
    con.close()


if __name__ == "__main__":
    main()
