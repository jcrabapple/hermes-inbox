"""SQLite store for the inbox.

The inbox keeps its own copy of every item because the sources rotate:
cron output dirs are capped per job and cron sessions are pruned weekly.
Read/star/archive state lives only here.
"""
from __future__ import annotations

import json
import os
import sqlite3
import time
from pathlib import Path

SCHEMA = """
CREATE TABLE IF NOT EXISTS items (
    id          INTEGER PRIMARY KEY,
    source      TEXT NOT NULL,      -- cron | artifact | kanban | note
    source_ref  TEXT NOT NULL,      -- unique key within the source
    title       TEXT NOT NULL,
    summary     TEXT NOT NULL DEFAULT '',
    body        TEXT NOT NULL DEFAULT '',
    extra       TEXT NOT NULL DEFAULT '{}',
    status      TEXT NOT NULL DEFAULT 'info',
    attention   INTEGER NOT NULL DEFAULT 0,
    noise       INTEGER NOT NULL DEFAULT 0,   -- skipped/silent/unchanged runs
    group_key   TEXT NOT NULL DEFAULT '',
    group_label TEXT NOT NULL DEFAULT '',
    profile     TEXT NOT NULL DEFAULT 'default',
    first_seen  REAL NOT NULL,
    ts          REAL NOT NULL,                -- sort time (latest event)
    read        INTEGER NOT NULL DEFAULT 0,
    starred     INTEGER NOT NULL DEFAULT 0,
    archived    INTEGER NOT NULL DEFAULT 0,
    UNIQUE (source, source_ref)
);
CREATE INDEX IF NOT EXISTS idx_items_ts ON items(ts DESC);
CREATE INDEX IF NOT EXISTS idx_items_group ON items(source, group_key, ts DESC);
CREATE INDEX IF NOT EXISTS idx_items_flags ON items(archived, noise, read);

CREATE VIRTUAL TABLE IF NOT EXISTS items_fts USING fts5(
    title, summary, body, group_label,
    content='items', content_rowid='id', tokenize='porter unicode61'
);
CREATE TRIGGER IF NOT EXISTS items_ai AFTER INSERT ON items BEGIN
    INSERT INTO items_fts(rowid, title, summary, body, group_label)
    VALUES (new.id, new.title, new.summary, new.body, new.group_label);
END;
CREATE TRIGGER IF NOT EXISTS items_ad AFTER DELETE ON items BEGIN
    INSERT INTO items_fts(items_fts, rowid, title, summary, body, group_label)
    VALUES ('delete', old.id, old.title, old.summary, old.body, old.group_label);
END;
CREATE TRIGGER IF NOT EXISTS items_au AFTER UPDATE OF title, summary, body, group_label ON items BEGIN
    INSERT INTO items_fts(items_fts, rowid, title, summary, body, group_label)
    VALUES ('delete', old.id, old.title, old.summary, old.body, old.group_label);
    INSERT INTO items_fts(rowid, title, summary, body, group_label)
    VALUES (new.id, new.title, new.summary, new.body, new.group_label);
END;

CREATE TABLE IF NOT EXISTS meta (k TEXT PRIMARY KEY, v TEXT NOT NULL);

-- Refs removed by retention. Cron output files can outlive the item, and without a
-- tombstone the next index pass would bring a pruned run straight back.
CREATE TABLE IF NOT EXISTS tombstones (
    source     TEXT NOT NULL,
    source_ref TEXT NOT NULL,
    pruned_at  REAL NOT NULL,
    PRIMARY KEY (source, source_ref)
);
"""

# Columns added after 1.0. Each is (name, DDL type); connect() adds any that are missing.
MIGRATIONS = (
    ("model", "TEXT"),          # primary model of the run (cron sessions)
    ("cost_usd", "REAL"),       # total cost of the run, all models and auxiliary tasks
    ("tokens_in", "INTEGER"),   # input + cache read + cache write
    ("tokens_out", "INTEGER"),  # output (reasoning included)
)
POST_MIGRATION = """
CREATE INDEX IF NOT EXISTS idx_items_cost ON items(source, ts) WHERE cost_usd IS NOT NULL;
"""

ITEM_FIELDS = (
    "source", "source_ref", "title", "summary", "body", "extra", "status",
    "attention", "noise", "group_key", "group_label", "profile", "ts",
)


def default_db_path() -> Path:
    if os.environ.get("HERMES_INBOX_DATA"):
        return Path(os.environ["HERMES_INBOX_DATA"]) / "inbox.db"
    base = os.environ.get("XDG_DATA_HOME") or os.path.expanduser("~/.local/share")
    return Path(base) / "hermes-inbox" / "inbox.db"


def connect(path: Path | str | None = None) -> sqlite3.Connection:
    path = Path(path) if path else default_db_path()
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    conn = sqlite3.connect(str(path), timeout=15, check_same_thread=False)
    # The DB holds copies of private reports and chat excerpts; keep it owner-only.
    for p in (path, path.parent):
        try:
            if p.stat().st_mode & 0o077:
                p.chmod(0o600 if p.is_file() else 0o700)
        except OSError:
            pass
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA synchronous=NORMAL")
    conn.execute("PRAGMA busy_timeout=15000")
    conn.executescript(SCHEMA)
    have = {r["name"] for r in conn.execute("PRAGMA table_info(items)")}
    for name, ddl in MIGRATIONS:
        if name not in have:
            conn.execute(f"ALTER TABLE items ADD COLUMN {name} {ddl}")
    conn.executescript(POST_MIGRATION)
    conn.commit()
    return conn


def get_meta(conn: sqlite3.Connection, key: str, default=None):
    row = conn.execute("SELECT v FROM meta WHERE k=?", (key,)).fetchone()
    return json.loads(row["v"]) if row else default


def set_meta(conn: sqlite3.Connection, key: str, value) -> None:
    conn.execute(
        "INSERT INTO meta(k, v) VALUES(?, ?) ON CONFLICT(k) DO UPDATE SET v=excluded.v",
        (key, json.dumps(value)),
    )


def known_refs(conn: sqlite3.Connection, source: str) -> set[str]:
    """Refs already indexed, plus refs retention removed (so they are not re-added)."""
    refs = {r[0] for r in conn.execute("SELECT source_ref FROM items WHERE source=?", (source,))}
    refs.update(r[0] for r in conn.execute("SELECT source_ref FROM tombstones WHERE source=?", (source,)))
    return refs


def _normalize(item: dict) -> dict:
    out = {k: item.get(k) for k in ITEM_FIELDS}
    out["summary"] = out["summary"] or ""
    out["body"] = out["body"] or ""
    out["extra"] = json.dumps(out["extra"] or {}) if not isinstance(out["extra"], str) else out["extra"]
    out["status"] = out["status"] or "info"
    out["attention"] = int(bool(out["attention"]))
    out["noise"] = int(bool(out["noise"]))
    out["group_key"] = out["group_key"] or ""
    out["group_label"] = out["group_label"] or ""
    out["profile"] = out["profile"] or "default"
    out["ts"] = float(out["ts"] or time.time())
    return out


def insert_item(conn: sqlite3.Connection, item: dict, *, read: bool = False) -> int | None:
    """Insert if new. Returns the new id, or None when it already existed."""
    it = _normalize(item)
    cur = conn.execute(
        f"INSERT OR IGNORE INTO items ({', '.join(ITEM_FIELDS)}, first_seen, read) "
        f"VALUES ({', '.join('?' for _ in ITEM_FIELDS)}, ?, ?)",
        (*(it[k] for k in ITEM_FIELDS), it["ts"], int(read or it["noise"])),
    )
    return cur.lastrowid if cur.rowcount else None


def upsert_item(conn: sqlite3.Connection, item: dict, *, read: bool = False,
                reopen: bool = False) -> int:
    """Insert or refresh an item that can recur (artifacts, kanban tasks).

    reopen=True marks an existing item unread and un-archives it, used when the
    underlying thing really changed (artifact file modified, task re-blocked).
    """
    it = _normalize(item)
    row = conn.execute(
        "SELECT id FROM items WHERE source=? AND source_ref=?", (it["source"], it["source_ref"])
    ).fetchone()
    if row is None:
        return insert_item(conn, item, read=read)  # type: ignore[return-value]
    sets = ", ".join(f"{k}=?" for k in ITEM_FIELDS if k not in ("source", "source_ref"))
    vals = [it[k] for k in ITEM_FIELDS if k not in ("source", "source_ref")]
    if reopen:
        sets += ", read=0, archived=0"
    conn.execute(f"UPDATE items SET {sets} WHERE id=?", (*vals, row["id"]))
    return row["id"]
