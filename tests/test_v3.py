"""v3: run diffs, live pulse, profile indexing and doctor."""
import json
import os
import sqlite3
import sys
import tempfile
import threading
import time
import unittest
import urllib.request
from http.server import ThreadingHTTPServer
from io import StringIO
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
sys.path.insert(0, str(Path(__file__).resolve().parent))

from hermes_inbox import cli, indexer, server, sources, store  # noqa: E402
from test_inbox import AGENT_RUN, make_home  # noqa: E402

AGENT_RUN_V2 = (AGENT_RUN.replace("All good. Disk at 52%.", "Disk at 71% — clean up old logs.")
                         .replace("2026-11-02 08:00:53", "2026-11-02 10:30:00"))


class DiffTests(unittest.TestCase):
    def test_identical_bodies_give_no_diff(self):
        self.assertIsNone(sources.diff_bodies("a\nb\nc", "a\nb\nc"))

    def test_replace_produces_a_hunk_with_context(self):
        d = sources.diff_bodies("line1\nold value\nline3\nline4\nline5", "line1\nnew value\nline3\nline4\nline5")
        self.assertEqual((d["added_n"], d["removed_n"]), (1, 1))
        self.assertEqual(len(d["hunks"]), 1)
        h = d["hunks"][0]
        self.assertEqual(h["removed"], ["old value"])
        self.assertEqual(h["added"], ["new value"])
        self.assertEqual(h["before"], ["line1"])
        self.assertEqual(h["after"], ["line3", "line4"])

    def test_two_nearby_changes_merge_into_one_hunk(self):
        old = "a\nx1\nb\nc\nd\ne\nf\ng\nx2\nh"
        new = "a\nX1\nb\nc\nd\ne\nf\ng\nX2\nh"
        d = sources.diff_bodies(old, new, context=3)
        self.assertEqual(len(d["hunks"]), 1)
        self.assertEqual(d["added_n"], 2)

    def test_far_apart_changes_are_separate_hunks(self):
        old = "\n".join(f"s{i}" for i in range(40)) + "\nBAD"
        new = "NEW\n" + "\n".join(f"s{i}" for i in range(40))
        d = sources.diff_bodies(old, new)
        self.assertEqual(d["added_n"] + d["removed_n"], 2)
        self.assertEqual(len(d["hunks"]), 2)

    def test_huge_changes_are_capped(self):
        d = sources.diff_bodies("", "\n".join(f"line {i}" for i in range(500)))
        self.assertLessEqual(len(d["hunks"][0]["added"]), 40)
        self.assertEqual(d["added_n"], 500)


class AttentionEmojiTests(unittest.TestCase):
    def flagged(self, body: str) -> bool:
        return bool(sources.ATTENTION_RE.search(body[:sources.ATTENTION_WINDOW])
                    or sources.alert_emoji(body[:sources.ATTENTION_WINDOW]))

    def test_headline_emoji_flags(self):
        self.assertTrue(self.flagged("⚠️ MASTODON SIGNUP NEEDS REVIEW\n\n31 auto-rejected.\n"))
        self.assertTrue(self.flagged("All quiet\n\n## Notes\n- 🔴 disk full on plex\n"))

    def test_quoted_or_inline_emoji_does_not_flag(self):
        # a log-review quoting other jobs' alerts deep in the body
        self.assertFalse(self.flagged(
            "Analysis complete. Here's the report:\n\n28 jobs ran clean. Details:\n"
            + "\n".join(f"- job {i}: ok, no issues found in the log tail" for i in range(10))
            + "\n- Nightly Backup: 🔴 failed checksum again\n"))
        # an energy report mentioning an alert mid-line
        self.assertFalse(self.flagged(
            "Done. Weather fetched from KOKV (59°F, cloudy), Enphase reported 12.83 kWh produced. "
            "Inverter status table below shows one ⚠ entry for panel 7, cosmetic only.\n"))

    def test_emoji_only_counts_in_the_first_lines(self):
        late = "intro line\n" * 10 + "⚠️ something is wrong\n"
        self.assertFalse(self.flagged(late))


def write_run(home: Path, job: str, name: str, text: str, age_s: int = 120) -> None:
    d = home / "cron" / "output" / job
    d.mkdir(parents=True, exist_ok=True)
    p = d / name
    p.write_text(text)
    t = time.time() - age_s
    os.utime(p, (t, t))


class PulseApiTests(unittest.TestCase):
    """/api/pulse re-indexes only when a watched source changed."""

    @classmethod
    def setUpClass(cls):
        cls.tmp = Path(tempfile.mkdtemp())
        home = make_home(cls.tmp)
        os.environ["HERMES_INBOX_TOKEN"] = "t"
        cls.app = server.App(cls.tmp / "inbox.db", home)
        cls.app.reindex()
        cls.httpd = ThreadingHTTPServer(("127.0.0.1", 0), server.make_handler(cls.app))
        threading.Thread(target=cls.httpd.serve_forever, daemon=True).start()
        cls.base = f"http://127.0.0.1:{cls.httpd.server_address[1]}"
        cls.home = home

    @classmethod
    def tearDownClass(cls):
        cls.httpd.shutdown()
        os.environ.pop("HERMES_INBOX_TOKEN", None)

    def get(self, path):
        req = urllib.request.Request(self.base + path)
        req.add_header("Authorization", "Bearer t")
        with urllib.request.urlopen(req) as r:
            return json.loads(r.read())

    def test_pulse_quiet_then_reindexes_on_change(self):
        res = self.get("/api/pulse")
        self.assertEqual(res, {"changed": False})
        write_run(self.home, "job1", "2026-11-02_10-00-00.md", AGENT_RUN_V2)
        res = self.get("/api/pulse")
        self.assertTrue(res["changed"])
        self.assertIn("views", res["summary"])
        n = self.app.db().execute(
            "SELECT COUNT(*) FROM items WHERE source='cron' AND ts >= ?",
            (sources.local_ts("2026-11-02_10-30-00"),)).fetchone()[0]
        self.assertEqual(n, 1)
        self.assertEqual(self.get("/api/pulse"), {"changed": False})


class ProfileTests(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        self.home = make_home(self.tmp)
        # A second profile with its own cron job that shares the default job id.
        alt = self.home / "profiles" / "voice"
        (alt / "cron" / "output" / "job1").mkdir(parents=True)
        p = alt / "cron" / "output" / "job1" / "2026-11-02_07-00-00.md"
        p.write_text(AGENT_RUN)
        t = time.time() - 120
        os.utime(p, (t, t))  # past the settle window, or the indexer skips it as still being written
        (alt / "cron" / "jobs.json").write_text(json.dumps({"jobs": [
            {"id": "job1", "name": "Health Report", "deliver": "telegram"}]}))
        self.conn = store.connect(self.tmp / "inbox.db")
        indexer.Indexer(self.conn, self.home).run()

    def test_items_carry_their_profile(self):
        rows = self.conn.execute(
            "SELECT profile, COUNT(*) c FROM items WHERE source='cron' GROUP BY profile").fetchall()
        by = {r["profile"]: r["c"] for r in rows}
        self.assertGreater(by.get("default", 0), 0)
        self.assertEqual(by.get("voice"), 1)

    def test_group_filter_can_scope_to_a_profile(self):
        where, args = server.build_filter({"view": "all", "source": "cron", "group": "job1", "profile": "voice"})
        n = self.conn.execute(f"SELECT COUNT(*) FROM items WHERE {where}", args).fetchone()[0]
        self.assertEqual(n, 1)

    def test_profile_filter_scopes_whole_views(self):
        where, args = server.build_filter({"view": "all", "profile": "voice"})
        rows = self.conn.execute(f"SELECT DISTINCT profile FROM items WHERE {where}", args).fetchall()
        self.assertEqual([r[0] for r in rows], ["voice"])


class DoctorTests(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        self.home = make_home(self.tmp)
        self.db = self.tmp / "inbox.db"
        store.connect(self.db).close()

    def run_doctor(self, home: Path, db: Path) -> tuple[int, str]:
        buf = StringIO()
        with mock.patch.dict(os.environ, {"HERMES_HOME": str(home), "HERMES_INBOX_DATA": str(db.parent)}), \
             mock.patch.object(store, "default_db_path", return_value=db), \
             mock.patch.object(server, "config_dir", return_value=self.tmp / "config"), \
             mock.patch("sys.stdout", buf):
            code = cli.doctor()
        return code, buf.getvalue()

    def test_healthy_home_passes(self):
        code, text = self.run_doctor(self.home, self.db)
        self.assertEqual(code, 0)
        self.assertIn("hermes home", text)
        self.assertIn("[default] cron", text)
        self.assertIn("integrity ok", text)
        self.assertIn("profiles", text)

    def test_missing_home_fails(self):
        code, text = self.run_doctor(self.tmp / "nope", self.db)
        self.assertEqual(code, 1)
        self.assertIn("does not exist", text)

    def test_missing_database_fails(self):
        code, _ = self.run_doctor(self.home, self.tmp / "missing" / "inbox.db")
        self.assertEqual(code, 1)

    def test_corrupt_state_db_fails(self):
        (self.home / "state.db").write_bytes(b"not a database")
        code, text = self.run_doctor(self.home, self.db)
        self.assertEqual(code, 1)
        self.assertIn("cannot read", text)


class DiffEndpointTests(unittest.TestCase):
    """get_item returns a diff against the previous run of the same job."""

    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        self.home = make_home(self.tmp)
        write_run(self.home, "job1", "2026-11-02_10-30-00.md", AGENT_RUN_V2)
        self.conn = store.connect(self.tmp / "inbox.db")
        indexer.Indexer(self.conn, self.home).run()

    def test_reader_diff_arrives_with_the_item(self):
        row = self.conn.execute(
            "SELECT id FROM items WHERE source='cron' AND status='ok' ORDER BY ts DESC").fetchone()
        d = dict(self.conn.execute("SELECT * FROM items WHERE id=?", (row["id"],)).fetchone())
        prev = self.conn.execute(
            "SELECT id, body FROM items WHERE source='cron' AND group_key=? AND ts<?",
            (d["group_key"], d["ts"])).fetchone()
        self.assertIsNotNone(prev)
        diff = sources.diff_bodies(prev["body"], d["body"])
        self.assertIsNotNone(diff)
        self.assertIn("71%", "\n".join(diff["hunks"][0]["added"]))


if __name__ == "__main__":
    unittest.main()
