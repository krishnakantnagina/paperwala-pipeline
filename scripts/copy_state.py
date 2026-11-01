"""Copy a pipeline state DB (Turso) into a local SQLite file with small paged reads.

    PIPELINE_DB_URL=... PIPELINE_DB_TOKEN=... PIPELINE_READ_BUDGET=3000000 \
        python scripts/copy_state.py ../local/pipeline-state.db

Reads each table by rowid in pages of 2000 (a read costs one row per row returned, never a scan),
creates the destination with the current schema first (`state.migrate`), and copies the columns both
sides have, by name, so an older source schema (5) fills a newer one (6). Prints the receipt line.
Re-running continues from what is already in the destination (INSERT OR REPLACE by rowid).
"""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from pwpipe import state                      # noqa: E402
from pwpipe.db import METER, SqliteDb, TursoDb  # noqa: E402

PAGE = 2000
TABLES = ("meta", "papers", "routing", "events")


def columns(db, table: str) -> list[str]:
    return [r["name"] for r in db.execute(f"PRAGMA table_info({table})")]


def copy_table(src, dst, table: str) -> int:
    have = columns(src, table)
    want = set(columns(dst, table))
    cols = [c for c in have if c in want]
    if not cols:
        print(f"{table}: no common columns, skipped")
        return 0
    names = ", ".join(f'"{c}"' for c in cols)
    marks = ", ".join(f":c{i}" for i in range(len(cols)))
    last = dst.execute(f"SELECT COALESCE(MAX(rowid), 0) AS m FROM {table}")[0]["m"]
    copied = 0
    while True:
        rows = src.execute(
            f"SELECT rowid AS _rowid, {names} FROM {table} WHERE rowid > :last "
            f"ORDER BY rowid LIMIT {PAGE}", {"last": last})
        if not rows:
            break
        stmts = []
        for r in rows:
            params = {"rid": r["_rowid"], **{f"c{i}": r[c] for i, c in enumerate(cols)}}
            stmts.append((f"INSERT OR REPLACE INTO {table} (rowid, {names}) VALUES (:rid, {marks})", params))
        dst.batch(stmts)
        last = rows[-1]["_rowid"]
        copied += len(rows)
        if copied % 10000 < PAGE:
            print(f"{table}: {copied} rows", flush=True)
    print(f"{table}: done, {copied} rows copied this run", flush=True)
    return copied


def main() -> None:
    out = sys.argv[1] if len(sys.argv) > 1 else "pipeline-state.db"
    src = TursoDb.from_env()
    dst = SqliteDb(out)
    state.migrate(dst)
    for t in TABLES:
        try:
            copy_table(src, dst, t)
        except Exception as exc:                  # a table the source does not have
            print(f"{t}: {exc}")
    for t in TABLES:
        try:
            print(f"{t}: {dst.execute(f'SELECT count(*) AS n FROM {t}')[0]['n']} rows in {out}")
        except Exception:
            pass
    print(METER.summary())


if __name__ == "__main__":
    main()
