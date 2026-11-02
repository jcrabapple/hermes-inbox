"""Retention: keep the inbox from growing forever.

Defaults delete only quiet runs (skipped, silent, empty, unchanged) older than 30 days.
Starred items are never deleted. Everything else is kept unless HERMES_INBOX_KEEP_DAYS
is set. Pruned refs get a tombstone so the indexer does not re-add them from cron
output files that are still on disk.
"""
from __future__ import annotations

import os
import sqlite3
import time

from . import store

DAY = 86400
VACUUM_THRESHOLD = 500


def policy() -> dict:
    def env_int(name, default):
        try:
            return max(0, int(os.environ.get(name, default)))
        except ValueError:
            return default
    return {"quiet_days": env_int("HERMES_INBOX_KEEP_QUIET_DAYS", 30),
            "keep_days": env_int("HERMES_INBOX_KEEP_DAYS", 0)}


def _where(quiet_days: int, keep_days: int, now: float) -> tuple[str, list]:
    clauses, args = [], []
    if quiet_days:
        clauses.append("(noise = 1 AND ts < ?)")
        args.append(now - quiet_days * DAY)
    if keep_days:
        clauses.append("(ts < ?)")
        args.append(now - keep_days * DAY)
    if not clauses:
        return "", []
    return f"starred = 0 AND ({' OR '.join(clauses)})", args


def prune(conn: sqlite3.Connection, quiet_days: int | None = None, keep_days: int | None = None,
          dry_run: bool = False, now: float | None = None) -> dict:
    pol = policy()
    quiet_days = pol["quiet_days"] if quiet_days is None else quiet_days
    keep_days = pol["keep_days"] if keep_days is None else keep_days
    now = now or time.time()
    where, args = _where(quiet_days, keep_days, now)
    result = {"quiet_days": quiet_days, "keep_days": keep_days, "dry_run": dry_run, "deleted": 0,
              "by_source": {}, "at": now}
    if not where:
        return result
    for r in conn.execute(f"SELECT source, COUNT(*) FROM items WHERE {where} GROUP BY source", args):
        result["by_source"][r[0]] = r[1]
    result["deleted"] = sum(result["by_source"].values())
    if dry_run or not result["deleted"]:
        return result
    with conn:
        conn.execute(f"INSERT OR IGNORE INTO tombstones (source, source_ref, pruned_at) "
                     f"SELECT source, source_ref, ? FROM items WHERE {where}", (now, *args))
        conn.execute(f"DELETE FROM items WHERE {where}", args)
        store.set_meta(conn, "last_prune", result)
    if result["deleted"] >= VACUUM_THRESHOLD:
        conn.execute("INSERT INTO items_fts(items_fts) VALUES ('optimize')")
        conn.commit()
        conn.execute("VACUUM")
    return result


def storage(conn: sqlite3.Connection, db_path) -> dict:
    size = 0
    for suffix in ("", "-wal"):
        try:
            size += os.path.getsize(f"{db_path}{suffix}")
        except OSError:
            pass
    by_source = {r[0]: r[1] for r in conn.execute("SELECT source, COUNT(*) FROM items GROUP BY source")}
    return {
        "db_bytes": size,
        "items": sum(by_source.values()),
        "by_source": by_source,
        "quiet_items": conn.execute("SELECT COUNT(*) FROM items WHERE noise = 1").fetchone()[0],
        "starred": conn.execute("SELECT COUNT(*) FROM items WHERE starred = 1").fetchone()[0],
        "oldest_ts": conn.execute("SELECT MIN(ts) FROM items").fetchone()[0],
        "policy": policy(),
        "last_prune": store.get_meta(conn, "last_prune"),
    }
