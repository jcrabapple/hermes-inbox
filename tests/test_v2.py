"""Usage (model and cost per run), retention and export."""
import csv
import io
import json
import os
import sqlite3
import sys
import tempfile
import threading
import time
import unittest
import unittest.mock
import urllib.error
import urllib.request
from datetime import datetime
from http.server import ThreadingHTTPServer
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
sys.path.insert(0, str(Path(__file__).resolve().parent))

from hermes_inbox import export, indexer, retention, server, store, usage  # noqa: E402
from test_inbox import make_home  # noqa: E402

RUN_TS = datetime.strptime("2026-11-02 08:00:53", "%Y-%m-%d %H:%M:%S").timestamp()

SESSION_COLS = """id TEXT PRIMARY KEY, source TEXT, title TEXT, cwd TEXT, display_name TEXT,
    model TEXT, billing_provider TEXT, input_tokens INTEGER, output_tokens INTEGER,
    cache_read_tokens INTEGER, cache_write_tokens INTEGER, reasoning_tokens INTEGER,
    estimated_cost_usd REAL, actual_cost_usd REAL, api_call_count INTEGER,
    started_at REAL, ended_at REAL, parent_session_id TEXT"""


def add_cron_sessions(home: Path):
    """Rebuild the fixture's sessions table with Hermes' cost columns and two cron sessions."""
    conn = sqlite3.connect(home / "state.db")
    rows = conn.execute("SELECT id, source, title, cwd, display_name FROM sessions").fetchall()
    conn.executescript(f"DROP TABLE sessions; CREATE TABLE sessions ({SESSION_COLS});")
    conn.executemany("INSERT INTO sessions (id, source, title, cwd, display_name) VALUES (?,?,?,?,?)", rows)
    ins = ("INSERT INTO sessions (id, source, title, model, billing_provider, input_tokens, output_tokens, "
           "cache_read_tokens, cache_write_tokens, reasoning_tokens, estimated_cost_usd, actual_cost_usd, "
           "api_call_count, started_at, ended_at, parent_session_id) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)")
    # The real run: compressed once, so the titled tip has a parent holding earlier usage.
    conn.execute(ins, ("cron-parent", "cron", None, "z-ai/glm-5.3-flash", "openrouter", 1000, 100, 5000, 0, 0,
                       0.002, None, 3, RUN_TS - 120, RUN_TS - 60, None))
    conn.execute(ins, ("cron-tip", "cron", "Health Report · Nov 02 08:00", "z-ai/glm-5.3-flash", "openrouter",
                       2000, 300, 9000, 0, 50, 0.004, None, 4, RUN_TS - 60, RUN_TS, "cron-parent"))
    # A decoy from the same job a day earlier must not match.
    conn.execute(ins, ("cron-old", "cron", "Health Report · Nov 01 08:00", "z-ai/glm-5.3-flash", "openrouter",
                       10, 1, 0, 0, 0, 0.5, None, 1, RUN_TS - 86400 - 60, RUN_TS - 86400, None))
    conn.executescript("""
        CREATE TABLE session_model_usage (session_id TEXT, model TEXT, billing_provider TEXT, task TEXT,
            api_call_count INTEGER, input_tokens INTEGER, output_tokens INTEGER, cache_read_tokens INTEGER,
            cache_write_tokens INTEGER, reasoning_tokens INTEGER, estimated_cost_usd REAL, actual_cost_usd REAL);
    """)
    conn.executemany("INSERT INTO session_model_usage VALUES (?,?,?,?,?,?,?,?,?,?,?,?)", [
        ("cron-parent", "z-ai/glm-5.3-flash", "openrouter", "", 3, 1000, 100, 5000, 0, 0, 0.002, 0),
        ("cron-tip", "z-ai/glm-5.3-flash", "openrouter", "", 3, 1800, 280, 9000, 0, 50, 0.0035, 0),
        ("cron-tip", "deepseek/deepseek-v4.1-flash", "openrouter", "compression", 1, 200, 20, 0, 0, 0, 0.0005, 0),
    ])
    conn.commit()
    conn.close()


class UsageTests(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        self.home = make_home(self.tmp)
        add_cron_sessions(self.home)
        self.conn = store.connect(self.tmp / "inbox.db")
        indexer._usage_tries.clear()

    def test_match_picks_the_session_that_closed_with_the_run(self):
        h = sqlite3.connect(self.home / "state.db")
        self.assertEqual(usage.find_cron_session(h, "Health Report", "job1", RUN_TS + 1, set()), "cron-tip")
        self.assertIsNone(usage.find_cron_session(h, "Health Report", "job1", RUN_TS + 1, {"cron-tip"}))
        self.assertIsNone(usage.find_cron_session(h, "Other Job", "job9", RUN_TS, set()))

    def test_usage_sums_lineage_and_auxiliary_models(self):
        h = sqlite3.connect(self.home / "state.db")
        u = usage.session_usage(h, "cron-tip")
        self.assertEqual(u["sessions"], 2)
        self.assertAlmostEqual(u["cost_usd"], 0.006)
        self.assertEqual(u["model"], "z-ai/glm-5.3-flash")
        self.assertEqual(u["input"], 3000)
        self.assertEqual(u["cost_status"], "estimated")
        self.assertIn("compression", {m["task"] for m in u["models"]})

    def test_indexer_prices_agent_runs_only(self):
        # The fixture's runs are dated 2026-11-02; widen the lookback so the test never ages out.
        with unittest.mock.patch.object(indexer, "USAGE_LOOKBACK", 10 * 365 * 86400):
            indexer.Indexer(self.conn, self.home).run()
        row = self.conn.execute("SELECT model, cost_usd, tokens_in, tokens_out, extra FROM items "
                                "WHERE source='cron' AND status='ok'").fetchone()
        self.assertEqual(row["model"], "z-ai/glm-5.3-flash")
        self.assertAlmostEqual(row["cost_usd"], 0.006)
        self.assertEqual(row["tokens_in"], 3000 + 14000)
        self.assertEqual(json.loads(row["extra"])["usage"]["session_id"], "cron-tip")
        skipped = self.conn.execute("SELECT model FROM items WHERE status='skipped'").fetchone()
        self.assertIsNone(skipped["model"])


class RetentionTests(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        self.home = make_home(self.tmp)
        self.conn = store.connect(self.tmp / "inbox.db")
        indexer.Indexer(self.conn, self.home).run()

    def test_quiet_runs_pruned_starred_kept_and_not_reindexed(self):
        now = time.time() + 400 * 86400
        quiet = self.conn.execute("SELECT id FROM items WHERE noise=1").fetchall()
        self.assertTrue(quiet)
        self.conn.execute("UPDATE items SET starred=1 WHERE id=?", (quiet[0]["id"],))
        self.conn.commit()
        dry = retention.prune(self.conn, quiet_days=30, keep_days=0, dry_run=True, now=now)
        self.assertEqual(dry["deleted"], len(quiet) - 1)
        self.assertEqual(self.conn.execute("SELECT COUNT(*) FROM items WHERE noise=1").fetchone()[0], len(quiet))
        res = retention.prune(self.conn, quiet_days=30, keep_days=0, now=now)
        self.assertEqual(res["deleted"], len(quiet) - 1)
        self.assertEqual(self.conn.execute("SELECT COUNT(*) FROM items WHERE noise=1").fetchone()[0], 1)
        loud = self.conn.execute("SELECT COUNT(*) FROM items WHERE noise=0").fetchone()[0]
        self.assertEqual(indexer.Indexer(self.conn, self.home).run()["cron"], 0, "tombstones stop re-adds")
        self.assertEqual(self.conn.execute("SELECT COUNT(*) FROM items WHERE noise=0").fetchone()[0], loud)

    def test_keep_days_zero_means_forever(self):
        before = self.conn.execute("SELECT COUNT(*) FROM items WHERE noise=0").fetchone()[0]
        retention.prune(self.conn, quiet_days=0, keep_days=0, now=time.time() + 4000 * 86400)
        self.assertEqual(self.conn.execute("SELECT COUNT(*) FROM items WHERE noise=0").fetchone()[0], before)


class ExportTests(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        self.conn = store.connect(self.tmp / "inbox.db")
        store.insert_item(self.conn, {"source": "note", "source_ref": "n1", "title": "=HYPERLINK(\"x\")",
                                      "body": "body", "ts": 1000.0, "group_label": "Notes"})
        self.conn.commit()

    def test_csv_neutralises_formulas_and_json_round_trips(self):
        buf = io.StringIO()
        n = export.write_csv(export.rows(self.conn, "1=1", []), buf)
        self.assertEqual(n, 1)
        rec = next(csv.DictReader(io.StringIO(buf.getvalue())))
        self.assertTrue(rec["title"].startswith("'="))
        jbuf = io.StringIO()
        export.write_json(export.rows(self.conn, "1=1", []), jbuf)
        data = json.loads(jbuf.getvalue())
        self.assertEqual(data[0]["title"], "=HYPERLINK(\"x\")")
        self.assertEqual(data[0]["time"], "1970-01-01T00:16:40+00:00")


class ApiTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.tmp = Path(tempfile.mkdtemp())
        home = make_home(cls.tmp)
        add_cron_sessions(home)
        os.environ["HERMES_INBOX_TOKEN"] = "t"
        cls.app = server.App(cls.tmp / "inbox.db", home)
        with unittest.mock.patch.object(indexer, "USAGE_LOOKBACK", 10 * 365 * 86400):
            cls.app.reindex()
        cls.httpd = ThreadingHTTPServer(("127.0.0.1", 0), server.make_handler(cls.app))
        threading.Thread(target=cls.httpd.serve_forever, daemon=True).start()
        cls.base = f"http://127.0.0.1:{cls.httpd.server_address[1]}"

    @classmethod
    def tearDownClass(cls):
        cls.httpd.shutdown()
        os.environ.pop("HERMES_INBOX_TOKEN", None)

    def get(self, path, token="t"):
        req = urllib.request.Request(self.base + path)
        if token:
            req.add_header("Authorization", f"Bearer {token}")
        try:
            with urllib.request.urlopen(req) as r:
                return r.status, r.headers, r.read()
        except urllib.error.HTTPError as e:
            return e.code, e.headers, e.read()

    def test_usage_report(self):
        s, _, body = self.get("/api/usage?days=0")
        d = json.loads(body)
        self.assertEqual(s, 200)
        self.assertAlmostEqual(d["cost_usd"], 0.006)
        self.assertEqual(d["by_job"][0]["label"], "Health Report")
        self.assertEqual(d["by_model"][0]["model"], "z-ai/glm-5.3-flash")
        self.assertIn("db_bytes", d["storage"])

    def test_group_usage_on_job_list(self):
        d = json.loads(self.get("/api/items?view=all&source=cron&group=job1&profile=default")[2])
        self.assertAlmostEqual(d["group_usage"]["cost_usd"], 0.006)

    def test_export_requires_auth_and_downloads(self):
        self.assertEqual(self.get("/api/export?format=csv", token=None)[0], 401)
        s, headers, body = self.get("/api/export?view=all&source=cron&format=csv")
        self.assertEqual(s, 200)
        self.assertIn("attachment", headers["Content-Disposition"])
        self.assertEqual(int(headers["Content-Length"]), len(body))
        rows = list(csv.DictReader(io.StringIO(body.decode())))
        self.assertTrue(rows and all(r["source"] == "cron" for r in rows))
        self.assertEqual(self.get("/api/export?format=xml")[0], 400)


if __name__ == "__main__":
    unittest.main()
