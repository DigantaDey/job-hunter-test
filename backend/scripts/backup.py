#!/usr/bin/env python
"""
Database backup.

    python scripts/backup.py                     # uses DATABASE_URL
    python scripts/backup.py --out /backups --keep 14

SQLite uses the online backup API (safe while the app is running) and verifies
the copy with ``PRAGMA integrity_check`` before reporting success. PostgreSQL
shells out to ``pg_dump`` and gzips the result. Exit codes: 0 ok, 1 failure —
so cron/CI alerts on non-zero.
"""
from __future__ import annotations

import argparse
import gzip
import os
import shutil
import sqlite3
import subprocess
import sys
import time
from pathlib import Path
from typing import Optional

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

from app.core.config import settings  # noqa: E402


def _stamp() -> str:
    """Second precision plus microseconds: two backups in the same second must
    not overwrite each other (cron retries, manual runs)."""
    return time.strftime("%Y%m%d-%H%M%S") + f"-{int(time.time_ns() % 1_000_000):06d}"


def backup_sqlite(database_url: str, out_dir: Path) -> Path:
    source_path = database_url.replace("sqlite:///", "", 1)
    if not os.path.exists(source_path):
        raise FileNotFoundError(f"SQLite database not found: {source_path}")
    target = out_dir / f"jobhunter-{_stamp()}.db"
    source = sqlite3.connect(source_path)
    destination = sqlite3.connect(str(target))
    try:
        source.backup(destination)
        result = destination.execute("PRAGMA integrity_check").fetchone()
        if not result or result[0] != "ok":
            raise RuntimeError(f"integrity check failed: {result}")
    finally:
        destination.close()
        source.close()
    return target


def backup_postgres(database_url: str, out_dir: Path) -> Path:
    if not shutil.which("pg_dump"):
        raise RuntimeError("pg_dump not found — install postgresql-client in the image/host")
    target = out_dir / f"jobhunter-{_stamp()}.sql.gz"
    with gzip.open(target, "wb") as compressed:
        completed = subprocess.run(
            ["pg_dump", "--no-owner", "--no-privileges", database_url],
            stdout=subprocess.PIPE, stderr=subprocess.PIPE, check=False,
        )
        if completed.returncode != 0:
            target.unlink(missing_ok=True)
            raise RuntimeError(f"pg_dump failed: {completed.stderr.decode()[:400]}")
        compressed.write(completed.stdout)
    return target


def prune(out_dir: Path, keep: int) -> int:
    backups = sorted(out_dir.glob("jobhunter-*"), key=lambda path: path.stat().st_mtime, reverse=True)
    removed = 0
    for stale in backups[keep:]:
        stale.unlink(missing_ok=True)
        removed += 1
    return removed


def main(argv: Optional[list] = None) -> int:
    parser = argparse.ArgumentParser(description="Back up the JobHunter database")
    parser.add_argument("--out", default=os.environ.get("BACKUP_DIR", "./backups"), help="destination directory")
    parser.add_argument("--keep", type=int, default=int(os.environ.get("BACKUP_KEEP", "14")),
                        help="how many backups to retain")
    args = parser.parse_args(argv)

    out_dir = Path(args.out).expanduser().resolve()
    out_dir.mkdir(parents=True, exist_ok=True)
    database_url = settings.database_url

    try:
        if database_url.startswith("sqlite"):
            path = backup_sqlite(database_url, out_dir)
        elif database_url.startswith(("postgres", "postgresql")):
            path = backup_postgres(database_url, out_dir)
        else:
            raise RuntimeError(f"unsupported database scheme: {database_url.split(':', 1)[0]}")
    except Exception as exc:  # noqa: BLE001 - CLI boundary
        print(f"BACKUP FAILED: {type(exc).__name__}: {exc}", file=sys.stderr)
        return 1

    removed = prune(out_dir, max(1, args.keep))
    print(f"backup ok: {path} ({path.stat().st_size / 1024:.1f} KiB), pruned {removed} old backup(s)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
