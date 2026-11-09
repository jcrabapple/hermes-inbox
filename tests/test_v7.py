"""v7: orphaned background processes, and source health for the footer.

Run: python3 -m unittest discover -s tests
"""
import json
import os
import subprocess
import sys
import tempfile
import time
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
sys.path.insert(0, str(Path(__file__).resolve().parent))

from hermes_inbox import indexer, sources, store  # noqa: E402


def dead_pid() -> int:
    p = subprocess.Popen([sys.executable, "-c", "pass"])
    p.wait()
    return p.pid


def entry(**kw) -> dict:
    base = {"session_id": "proc_1", "command": "npm run dev -- --port 5173",
            "pid": dead_pid(), "pid_scope": "host", "started_at": time.time() - 7200,
            "cwd": "/home/user/project", "notify_on_complete": True}
    base.update(kw)
    return base


class ProcessProbeTests(unittest.TestCase):
    def test_gone_pid(self):
        self.assertEqual(sources.host_pid_state(dead_pid()), "gone")
        self.assertEqual(sources.host_pid_state(999999), "gone")
        self.assertEqual(sources.host_pid_state(0), "gone")
        self.assertEqual(sources.host_pid_state("not a pid"), "unknown")

    @unittest.skipUnless(sys.platform.startswith("linux"), "reads /proc")
    def test_start_time_distinguishes_a_recycled_pid(self):
        pid = os.getpid()
        actual = sources._proc_start_ticks(pid)
        self.assertIsNotNone(actual)
        self.assertEqual(sources.host_pid_state(pid, actual), "running")
        self.assertEqual(sources.host_pid_state(pid, actual + 1), "reused")
        # No recorded start means we cannot prove reuse, so it counts as running.
        self.assertEqual(sources.host_pid_state(pid), "running")
        self.assertEqual(sources.host_pid_state(pid, "nope"), "running")


class ProcessIncidentTests(unittest.TestCase):
    def test_dead_host_process_is_an_item(self):
        now = time.time()
        items = sources.process_incidents("default", [entry()], now)
        self.assertEqual(len(items), 1)
        it = items[0]
        self.assertEqual(it["source"], "process")
        self.assertEqual(it["source_ref"], "default:proc_1")
        self.assertEqual(it["status"], "orphaned")
        self.assertTrue(it["attention"])
        self.assertIn("npm run dev", it["title"])
        self.assertIn("PID", it["summary"])
        self.assertEqual(it["extra"]["pid_state"], "gone")
        self.assertIn("npm run dev", it["body"])
        self.assertIn("/home/user/project", json.dumps(it["extra"]))

    def test_recycled_pid_says_so(self):
        pid = os.getpid()
        actual = sources._proc_start_ticks(pid)
        it = sources.process_incidents("default", [entry(pid=pid, host_start_time=actual + 1)])[0]
        self.assertEqual(it["extra"]["pid_state"], "reused")
        self.assertIn("belongs to a different process", it["body"])

    def test_live_process_is_not_an_item(self):
        self.assertEqual(sources.process_incidents("default", [entry(pid=os.getpid())]), [])

    def test_recent_exit_is_left_to_the_registry(self):
        fresh = entry(started_at=time.time() - 5)
        self.assertEqual(sources.process_incidents("default", [fresh]), [])

    def test_sandboxed_pid_is_not_probed(self):
        self.assertEqual(sources.process_incidents("default", [entry(pid_scope="container")]), [])

    def test_recorded_exit_marker_is_a_handoff_not_an_orphan(self):
        tmp = Path(tempfile.mkdtemp())
        log = tmp / "task.log"
        log.write_text("done\n")
        log.with_suffix(".exit").write_text("0\n")
        self.assertEqual(sources.process_incidents("default", [entry(output_log=str(log))]), [])

    def test_missing_and_unreadable_registry(self):
        home = Path(tempfile.mkdtemp())
        self.assertEqual(sources.read_processes(home), ([], "absent"))
        (home / "processes.json").write_text("{not json")
        self.assertEqual(sources.read_processes(home)[1][:10], "unreadable")
        (home / "processes.json").write_text('{"not": "a list"}')
        self.assertEqual(sources.read_processes(home), ([], "unreadable (not a list)"))


class ProcessIndexTests(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        self.home = self.tmp / "hermes"
        self.home.mkdir(parents=True)
        self.conn = store.connect(self.tmp / "inbox.db")
        self.idx = indexer.Indexer(self.conn, self.home)

    def write(self, *entries):
        (self.home / "processes.json").write_text(json.dumps(list(entries)))

    def row(self):
        r = self.conn.execute("SELECT * FROM items WHERE source='process'").fetchone()
        return dict(r) if r else None

    def test_orphan_is_indexed_unread_then_resolved(self):
        self.write(entry())
        self.assertEqual(self.idx.run()["process"], 1)
        row = self.row()
        self.assertEqual(row["status"], "orphaned")
        self.assertEqual(row["read"], 0, "a stranded process is not routine volume")
        self.write()
        self.idx.run()
        self.assertEqual(self.row()["status"], "resolved")
        self.assertEqual(self.row()["archived"], 1)

    def test_unreadable_registry_never_resolves_an_item(self):
        self.write(entry())
        self.idx.run()
        (self.home / "processes.json").write_text("garbage")
        self.assertEqual(self.idx.run()["process"], 0)
        self.assertEqual(self.row()["status"], "orphaned")
        self.assertTrue(self.idx.health["default"]["processes.json"].startswith("unreadable"))


class SourceHealthTests(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        self.home = self.tmp / "hermes"
        self.home.mkdir(parents=True)
        self.conn = store.connect(self.tmp / "inbox.db")
        self.idx = indexer.Indexer(self.conn, self.home)

    def health(self):
        return store.get_meta(self.conn, "last_index")["sources"]

    def test_absent_stores_are_named_not_errors(self):
        self.idx.run()
        h = self.health()["default"]
        self.assertEqual(h["jobs.json"], "absent")
        self.assertEqual(h["state.db"], "absent")
        self.assertEqual(h["processes.json"], "absent")
        self.assertEqual(h["kanban"], "absent")

    def test_healthy_stores_read(self):
        (self.home / "cron").mkdir()
        (self.home / "cron" / "jobs.json").write_text('{"jobs": []}')
        (self.home / "processes.json").write_text("[]")
        (self.home / "state.db").touch()
        self.idx.run()
        h = self.health()["default"]
        self.assertEqual(h["jobs.json"], "read")
        self.assertEqual(h["processes.json"], "read")

    def test_corrupt_state_db_is_reported_and_does_not_break_the_pass(self):
        (self.home / "state.db").write_text("not a database")
        counts = self.idx.run()  # must not raise
        self.assertEqual(counts["artifact"], 0)
        self.assertTrue(self.health()["default"]["state.db"].startswith("unreadable"))

    def test_corrupt_jobs_file_is_reported(self):
        (self.home / "cron").mkdir()
        (self.home / "cron" / "jobs.json").write_text("{oops")
        self.idx.run()
        self.assertEqual(self.health()["default"]["jobs.json"], "unreadable (JSONDecodeError)")

    def test_tableless_state_db_is_empty_not_unreadable(self):
        (self.home / "state.db").write_text("")  # a profile that has never written anything
        self.idx.run()
        self.assertEqual(self.health()["default"]["state.db"], "read")

    def test_a_later_reader_cannot_hide_an_unreadable_store(self):
        (self.home / "state.db").write_text("not a database")
        self.idx.run()
        # delegation runs after artifacts and would mark state.db read; it must not win.
        self.assertTrue(self.health()["default"]["state.db"].startswith("unreadable"))


if __name__ == "__main__":
    unittest.main()
