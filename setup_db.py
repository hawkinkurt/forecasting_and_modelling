"""Create (or update) the database tables and views. Safe to re-run.

run.py does this automatically, so this is only needed on its own after
editing the SQL files.
"""
from pipeline.db import connect, ensure_schema


def main():
    con = connect()
    ensure_schema(con)
    print(con.execute("SELECT table_name, table_type FROM information_schema.tables ORDER BY 1").fetchall())
    con.close()


if __name__ == "__main__":
    main()
