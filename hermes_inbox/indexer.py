"""Incremental indexer: pulls new items from every Hermes source into the inbox DB."""
from __future__ import annotations

import json
import logging
import os
import sqlite3
import time
from pathlib import Path

from . import sources, store

log = logging.getLogger("hermes_inbox.indexer")

# On the very first index, everything older than this is filed as already read
# so the inbox opens on recent activity instead of months of backlog.
BACKFILL_UNREAD_WINDOW = 48 * 3600


def hermes_root() -> Path:
    return Path(os.environ.get("HERMES_HOME") or os.path.expanduser("~/.hermes"))


class Indexer:
    def __init__(self, conn: sqlite3.Connection, root: Path | None = None):
        self.conn = conn
        self.root = root or hermes_root()

    def _initial_read(self, ts: float) -> bool:
        cutoff = store.get_meta(self.conn, "backfill_cutoff")
        return cutoff is not None and ts < cutoff

    def run(self) -> dict:
        started = time.time()
        if store.get_meta(self.conn, "backfill_cutoff") is None:
            store.set_meta(self.conn, "backfill_cutoff", started - BACKFILL_UNREAD_WINDOW)
            self.conn.commit()
        counts = {"cron": 0, "artifact": 0, "kanban": 0}
        for profile, home in sources.hermes_homes(self.root):
            counts["cron"] += self._index_cron(profile, home)
            counts["artifact"] += self._index_artifacts(profile, home)
            counts["kanban"] += self._index_kanban(profile, home)
        store.set_meta(self.conn, "last_index", {"at": time.time(), "took": round(time.time() - started, 2),
                                                 "new": counts})
        self.conn.commit()
        return counts

    # cron ────────────────────────────────────────────────────────────
    def _last_hash(self, profile: str, job_id: str) -> str | None:
        row = self.conn.execute(
            "SELECT extra FROM items WHERE source='cron' AND profile=? AND group_key=? "
            "AND status IN ('ok','unchanged') ORDER BY ts DESC LIMIT 1",
            (profile, job_id),
        ).fetchone()
        return json.loads(row["extra"]).get("hash") if row else None

    def _index_cron(self, profile: str, home: Path) -> int:
        jobs = sources.load_jobs(home)
        known = store.known_refs(self.conn, "cron")
        added = 0
        for job_dir, files in sources.iter_cron_files(home):
            new = [f for f in files if f"{profile}:{job_dir}/{f.name}" not in known]
            if not new:
                continue
            prev = None
            for f in new:
                try:
                    item = sources.cron_item(f, profile, jobs, prev or self._last_hash(profile, job_dir))
                except OSError as exc:
                    log.warning("cron file unreadable %s: %s", f, exc)
                    continue
                if item["status"] in ("ok", "unchanged"):
                    prev = item["extra"]["hash"]
                if store.insert_item(self.conn, item, read=self._initial_read(item["ts"])):
                    added += 1
            self.conn.commit()
        return added

    def retitle_cron(self) -> int:
        """Recompute cron subjects/previews from stored bodies after the subject rules change."""
        changed = 0
        for r in self.conn.execute("SELECT id, title, summary, body, group_label FROM items "
                                   "WHERE source='cron' AND status IN ('ok','unchanged')").fetchall():
            subject, preview = sources.cron_subject(r["body"], r["group_label"])
            if (subject, preview) != (r["title"], r["summary"]):
                self.conn.execute("UPDATE items SET title=?, summary=? WHERE id=?", (subject, preview, r["id"]))
                changed += 1
        self.conn.commit()
        return changed

    # artifacts ───────────────────────────────────────────────────────
    def _existing_extra(self, source: str, ref: str) -> dict | None:
        row = self.conn.execute(
            "SELECT extra FROM items WHERE source=? AND source_ref=?", (source, ref)).fetchone()
        return json.loads(row["extra"]) if row else None

    def _index_artifacts(self, profile: str, home: Path) -> int:
        key = f"cursor:messages:{profile}"
        cursor = store.get_meta(self.conn, key, 0)
        # state.db ids can go backwards only if the DB was rebuilt; restart then.
        if cursor > sources.max_message_id(home):
            cursor = 0
        added, last = 0, cursor
        try:
            mentions = list(sources.iter_artifact_mentions(home, cursor))
        except sqlite3.Error as exc:
            log.warning("state.db read failed for %s: %s", profile, exc)
            return 0
        for msg_id, mention in mentions:
            existing = self._existing_extra("artifact", mention["path"])
            item, reopen = sources.artifact_item(mention, profile, existing)
            if existing is None:
                added += 1
            store.upsert_item(self.conn, item, read=self._initial_read(item["ts"]), reopen=reopen)
            last = max(last, msg_id)
        if mentions:
            store.set_meta(self.conn, key, last)
        self.conn.commit()
        return added

    def refresh_artifact_facts(self) -> None:
        """Re-stat artifact files so missing/changed files show correctly."""
        rows = self.conn.execute("SELECT id, extra, status FROM items WHERE source='artifact'").fetchall()
        for r in rows:
            extra = json.loads(r["extra"])
            facts = sources.file_facts(extra.get("path", ""))
            status = "file" if facts["exists"] else "missing"
            if facts["mtime"] != extra.get("mtime") or status != r["status"]:
                changed = bool(extra.get("mtime") and facts["mtime"] and facts["mtime"] > extra["mtime"] + 1
                               and not sources.is_volatile(extra.get("path", "")))
                extra.update(facts)
                self.conn.execute(
                    "UPDATE items SET extra=?, status=?" + (", read=0, ts=?" if changed else "") + " WHERE id=?",
                    (json.dumps(extra), status, *( [facts["mtime"]] if changed else []), r["id"]))
        self.conn.commit()

    # kanban ──────────────────────────────────────────────────────────
    def _index_kanban(self, profile: str, home: Path) -> int:
        added = 0
        for board, db, label in sources.kanban_dbs(home):
            key = f"cursor:kanban:{profile}:{board}"
            cursor = store.get_meta(self.conn, key, 0)
            last = cursor
            try:
                runs = list(sources.iter_kanban_runs(board, db, cursor, label))
            except sqlite3.Error as exc:
                log.warning("kanban read failed %s/%s: %s", profile, board, exc)
                continue
            for run_id, item, artifacts, attachments in runs:
                item["profile"] = profile
                if store.insert_item(self.conn, item, read=self._initial_read(item["ts"])):
                    added += 1
                for path in artifacts:
                    self._kanban_artifact(profile, board, item, os.path.realpath(os.path.expanduser(path)))
                for att in attachments:
                    self._kanban_artifact(profile, board, item, att["stored_path"], att["filename"])
                last = max(last, run_id)
            if runs:
                store.set_meta(self.conn, key, last)
            self.conn.commit()
        return added

    def _kanban_artifact(self, profile, board, run_item, path, name=None):
        existing = self._existing_extra("artifact", path)
        if existing is not None:
            return
        facts = sources.file_facts(path)
        store.insert_item(self.conn, {
            "source": "artifact",
            "source_ref": path,
            "title": name or os.path.basename(path),
            "summary": f"From kanban task: {run_item['title']}",
            "body": run_item["body"],
            "status": "file" if facts["exists"] else "missing",
            "group_key": f"kanban:{board}",
            "group_label": run_item["group_label"],
            "profile": profile,
            "ts": run_item["ts"],
            "extra": {"path": path, **facts, "origin": "kanban", "board": board,
                      "task_id": run_item["extra"]["task_id"], "sessions": []},
        }, read=self._initial_read(run_item["ts"]))
