"""v5: delegation results that never reached their conversation.

Run: python3 -m unittest discover -s tests
"""
import json
import sqlite3
import sys
import tempfile
import time
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
sys.path.insert(0, str(Path(__file__).resolve().parent))

from hermes_inbox import indexer, sources, store  # noqa: E402


def make_state_db(home: Path, rows: list[tuple], sessions: dict | None = None) -> Path:
    """A state.db with the delegation table Hermes writes.

    rows: (id, state, dispatched_at, completed_at, delivery_state, attempts, goal, summary)
    """
    home.mkdir(parents=True, exist_ok=True)
    db = home / "state.db"
    if db.exists():
        db.unlink()
    conn = sqlite3.connect(db)
    conn.executescript("""
        CREATE TABLE sessions (id TEXT PRIMARY KEY, title TEXT, source TEXT, cwd TEXT, display_name TEXT);
        CREATE TABLE messages (id INTEGER PRIMARY KEY AUTOINCREMENT, session_id TEXT, role TEXT,
                               content TEXT, timestamp REAL);
        CREATE TABLE async_delegations (
            delegation_id TEXT PRIMARY KEY, state TEXT, dispatched_at REAL, completed_at REAL,
            updated_at REAL, delivery_state TEXT, delivery_attempts INTEGER, delivered_at REAL,
            task_json TEXT, result_json TEXT, origin_session TEXT);
    """)
    for sid, title in (sessions or {}).items():
        conn.execute("INSERT INTO sessions (id, title) VALUES (?, ?)", (sid, title))
    now = time.time()
    for did, state, dispatched, completed, delivery, attempts, goal, summary in rows:
        conn.execute(
            "INSERT INTO async_delegations VALUES (?,?,?,?,?,?,?,?,?,?,?)",
            (did, state, dispatched if dispatched is not None else now - 3600,
             completed, completed, delivery, attempts, None,
             json.dumps({"goal": goal, "goals": [goal]}),
             json.dumps({"results": [{"status": "completed", "summary": summary}],
                         "live_transcripts": [f"/tmp/deleg/{did}/task-0.log"]}),
             "sess-1"))
    conn.commit()
    conn.close()
    return db


class DelegationReaderTests(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        self.home = self.tmp / "hermes"

    def test_only_undelivered_results_are_reported(self):
        now = time.time()
        make_state_db(self.home, [
            # delivered: nothing to say
            ("d_delivered", "completed", now - 7200, now - 7000, "delivered", 1, "done", "ok"),
            # terminal, pending delivery, older than the grace: lost
            ("d_pending", "completed", now - 7200, now - 3600, "pending", 2, "Research venue pricing", "42 seats"),
            # pending but only just finished: the delivery path still has time
            ("d_fresh", "completed", now - 120, now - 60, "pending", 0, "fresh", "x"),
            # dropped: lost for good
            ("d_dropped", "completed", now - 90000, now - 86400, "dropped", 8, "Icon candidates", "three svgs"),
            # still running: not finished, not undelivered
            ("d_running", "running", now - 600, None, "pending", 0, "working", ""),
        ])
        items = sources.delegation_incidents("default", self.home, now)
        by_id = {i["source_ref"]: i for i in items}
        self.assertEqual(set(by_id), {"default:d_pending", "default:d_dropped"})

        pending = by_id["default:d_pending"]
        self.assertEqual(pending["status"], "undelivered")
        self.assertTrue(pending["attention"])
        self.assertEqual(pending["title"], "Result not delivered")
        self.assertEqual(pending["summary"], "Research venue pricing")
        self.assertIn("42 seats", pending["body"])
        self.assertEqual(pending["extra"]["kind"], "pending")
        self.assertEqual(pending["extra"]["attempts"], 2)
        self.assertAlmostEqual(pending["ts"], now - 3600, delta=2)

        dropped = by_id["default:d_dropped"]
        self.assertEqual(dropped["title"], "Result dropped after 8 delivery attempts")
        self.assertEqual(dropped["extra"]["kind"], "dropped")
        self.assertIn("three svgs", dropped["body"])

    def test_a_null_delivery_state_counts_as_undelivered_once_past_the_grace(self):
        now = time.time()
        make_state_db(self.home, [
            ("d_null", "completed", now - 3600, now - 3000, None, 0, "legacy row", "body")])
        items = sources.delegation_incidents("default", self.home, now)
        self.assertEqual([i["source_ref"] for i in items], ["default:d_null"])

    def test_session_title_labels_the_item(self):
        now = time.time()
        make_state_db(self.home, [
            ("d1", "completed", now - 3600, now - 3000, "dropped", 5, "goal", "sum")],
            sessions={"sess-1": "Plan the offsite"})
        item = sources.delegation_incidents("default", self.home, now)[0]
        self.assertEqual(item["group_label"], "Plan the offsite")

    def test_missing_or_tableless_state_db_returns_none(self):
        self.assertIsNone(sources.delegation_incidents("default", self.home))
        self.home.mkdir(parents=True, exist_ok=True)
        sqlite3.connect(self.home / "state.db").close()
        self.assertIsNone(sources.delegation_incidents("default", self.home))

    def test_unreadable_state_db_raises(self):
        self.home.mkdir(parents=True, exist_ok=True)
        (self.home / "state.db").write_text("not a database")
        with self.assertRaises(sqlite3.Error):
            sources.delegation_incidents("default", self.home)


class DelegationIndexTests(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        self.home = self.tmp / "hermes"
        self.conn = store.connect(self.tmp / "inbox.db")
        self.idx = indexer.Indexer(self.conn, self.home)

    def add_pending(self, did="d1", delivery="pending", completed=None, state="completed"):
        make_state_db(self.home, [
            (did, state, time.time() - 7200, completed if completed is not None else time.time() - 3600,
             delivery, 3, "Research venue pricing", "42 seats")])

    def row(self):
        r = self.conn.execute("SELECT * FROM items WHERE source='delegation'").fetchone()
        return dict(r) if r else None

    def test_new_result_is_indexed_once(self):
        self.add_pending()
        self.assertEqual(self.idx.run()["delegation"], 1)
        self.assertEqual(self.idx.run()["delegation"], 0)
        row = self.row()
        self.assertEqual(row["status"], "undelivered")
        self.assertEqual(row["source_ref"], "default:d1")
        self.assertEqual(row["read"], 0, "recent on a fresh install, so it starts unread")

    def test_an_old_lost_result_still_starts_unread(self):
        # The 48-hour backfill rule absorbs routine volume; a lost result is not that.
        self.add_pending(completed=time.time() - 30 * 86400)
        self.idx.run()
        self.assertEqual(self.row()["read"], 0)

    def test_delivery_resolves_and_archives(self):
        self.add_pending()
        self.idx.run()
        make_state_db(self.home, [
            ("d1", "completed", time.time() - 7200, time.time() - 3600, "delivered", 4,
             "Research venue pricing", "42 seats")])
        self.idx.run()
        row = self.row()
        self.assertEqual(row["status"], "resolved")
        self.assertEqual(row["archived"], 1)
        self.assertEqual(row["attention"], 0)
        self.assertIn("Delivered", row["body"])

    def test_missing_state_db_never_resolves_a_real_loss(self):
        self.add_pending()
        self.idx.run()
        (self.home / "state.db").unlink()
        self.idx.run()
        self.assertEqual(self.row()["status"], "undelivered")

    def test_unreadable_state_db_never_resolves_a_real_loss(self):
        self.add_pending()
        self.idx.run()
        # A corrupt file must not look like "the record went away". Drive the source
        # directly: a corrupt state.db breaks artifact indexing too, which is older
        # behaviour and not what this test is about.
        (self.home / "state.db").write_text("not a database")
        self.assertEqual(self.idx._index_delegations("default", self.home), 0)
        self.assertEqual(self.row()["status"], "undelivered")

    def test_a_result_that_misses_delivery_again_reopens(self):
        self.add_pending()
        self.idx.run()
        self.conn.execute("UPDATE items SET read=1 WHERE source='delegation'")
        self.conn.commit()
        make_state_db(self.home, [
            ("d1", "completed", time.time() - 7200, time.time() - 3600, "delivered", 1, "g", "s")])
        self.idx.run()
        self.assertEqual(self.row()["status"], "resolved")
        self.add_pending()
        self.idx.run()
        row = self.row()
        self.assertEqual(row["status"], "undelivered")
        self.assertEqual(row["archived"], 0)
        self.assertEqual(row["read"], 0)


if __name__ == "__main__":
    unittest.main()
