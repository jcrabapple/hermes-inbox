"""Behaviour tests against a throwaway Hermes home. Run: python3 -m unittest discover -s tests"""
import json
import os
import sqlite3
import sys
import tempfile
import threading
import time
import unittest
import urllib.error
import urllib.request
from http.server import ThreadingHTTPServer
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from hermes_inbox import indexer, server, sources, store  # noqa: E402

AGENT_RUN = """# Cron Job: Health Report

**Job ID:** job1
**Run Time:** 2026-11-02 08:00:53
**Schedule:** every day at 8am

**Prompt Characters:** 40
## Prompt

Do the thing.
## Response

this is a skill section that must be ignored

## Script Output

HEALTH=OK
uptime=5

**Response Characters:** 20
## Response

All good. Disk at 52%.
"""

FAILED_RUN = """# Cron Job: Health Report (FAILED)

**Job ID:** job1
**Run Time:** 2026-11-02 09:00:00

## Prompt

Do the thing.

## Error

```
RuntimeError: HTTP 402: Insufficient credits
```
"""

SKIPPED_RUN = """# Cron Job: Media Poll

**Job ID:** job2
**Run Time:** 2026-11-01 11:08:04

Script gate returned `wakeAgent=false` — agent skipped.
"""

NO_AGENT_RUN = """# Cron Job: Cleanup

**Job ID:** job3
**Run Time:** 2026-11-01 10:02:48
**Mode:** no_agent (script)

---

Pruned 93 sessions.
"""


def make_home(tmp: Path) -> Path:
    home = tmp / "hermes"
    out = home / "cron" / "output"
    for job, name, text in [("job1", "2026-11-02_08-00-54.md", AGENT_RUN),
                            ("job1", "2026-11-02_09-00-01.md", FAILED_RUN),
                            ("job2", "2026-11-01_11-08-04.md", SKIPPED_RUN),
                            ("job3", "2026-11-01_10-02-48.md", NO_AGENT_RUN)]:
        (out / job).mkdir(parents=True, exist_ok=True)
        p = out / job / name
        p.write_text(text)
        os.utime(p, (time.time() - 60, time.time() - 60))
    (home / "cron" / "jobs.json").write_text(json.dumps({"jobs": [
        {"id": "job1", "name": "Health Report", "deliver": "discord", "schedule": {"display": "0 8 * * *"}}]}))
    art = tmp / "report.md"
    art.write_text("# Report\n\nBody")
    secret = tmp / "secret.txt"
    secret.write_text("not published")
    conn = sqlite3.connect(home / "state.db")
    conn.executescript("""
        CREATE TABLE sessions (id TEXT PRIMARY KEY, source TEXT, title TEXT, cwd TEXT, display_name TEXT);
        CREATE TABLE messages (id INTEGER PRIMARY KEY, session_id TEXT, role TEXT, content TEXT, timestamp REAL);
    """)
    conn.execute("INSERT INTO sessions VALUES ('s1','desktop','Write a report',?,NULL)", (str(tmp),))
    conn.execute("INSERT INTO messages VALUES (1,'s1','assistant',?,?)",
                 (f"Here it is.\n\nMEDIA:{art}\n", time.time() - 30))
    conn.execute("INSERT INTO messages VALUES (2,'s1','user',?,?)", (f"MEDIA:{secret}", time.time() - 20))
    conn.execute("INSERT INTO messages VALUES (3,'s1','assistant',?,?)",
                 ('Preview:\n::preview{file="report.md"}', time.time() - 10))
    conn.commit()
    conn.close()
    return home


class ParseTests(unittest.TestCase):
    def test_agent_run_uses_last_response_and_script_output(self):
        p = sources.parse_cron_output(AGENT_RUN)
        self.assertEqual(p["response"], "All good. Disk at 52%.")
        self.assertIn("HEALTH=OK", p["script_output"])
        self.assertEqual((p["job_id"], p["name"]), ("job1", "Health Report"))

    def test_failed_title_and_error_section(self):
        p = sources.parse_cron_output(FAILED_RUN)
        self.assertEqual(p["kind"], "error")
        self.assertEqual(p["name"], "Health Report")
        self.assertIn("402", p["error"])

    def test_skip_and_no_agent(self):
        self.assertEqual(sources.parse_cron_output(SKIPPED_RUN)["kind"], "skipped")
        p = sources.parse_cron_output(NO_AGENT_RUN)
        self.assertEqual((p["kind"], p["response"]), ("script", "Pruned 93 sessions."))

    def test_attention_ignores_negations(self):
        self.assertFalse(sources.ATTENTION_RE.search("No errors, no action needed."))
        self.assertTrue(sources.ATTENTION_RE.search("HEALTH=DEGRADED"))
        self.assertTrue(sources.ATTENTION_RE.search("1 signup needs your decision"))
        self.assertTrue(sources.ATTENTION_RE.search("2 messages need your decision"))

    def test_subject_skips_name_heading_and_boilerplate(self):
        body = "# Cron Fleet Audit\n\n_Generated 2026-11-02T11:43 by ops-pack._\n\nFleet: 62 jobs, 3 paused."
        self.assertEqual(sources.cron_subject(body, "Cron Fleet Audit")[0], "Fleet: 62 jobs, 3 paused.")

    def test_scratch_paths_are_volatile(self):
        self.assertTrue(sources.is_volatile("/home/user/.config/browser-harness/tmp/p-1/shot.png"))
        self.assertTrue(sources.is_volatile("/var/home/user/.hermes/cache/scratch/a.pdf"))
        self.assertFalse(sources.is_volatile("/home/user/Documents/notes/plan.md"))

    def test_strip_md_keeps_snake_case(self):
        self.assertEqual(sources.strip_md("**backup_age_hours**=52"), "backup_age_hours=52")


class IndexTests(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        self.home = make_home(self.tmp)
        self.conn = store.connect(self.tmp / "inbox.db")

    def test_index_is_incremental_and_classifies(self):
        counts = indexer.Indexer(self.conn, self.home).run()
        self.assertEqual(counts["cron"], 4)
        self.assertEqual(counts["artifact"], 1, "same file via MEDIA and preview dedupes to one item")
        self.assertEqual(indexer.Indexer(self.conn, self.home).run(), {"cron": 0, "artifact": 0, "kanban": 0})
        st = {r["source_ref"].split("/")[-1]: (r["status"], r["noise"], r["attention"])
              for r in self.conn.execute("SELECT * FROM items WHERE source='cron'")}
        self.assertEqual(st["2026-11-02_09-00-01.md"], ("failed", 0, 1))
        self.assertEqual(st["2026-11-01_11-08-04.md"][:2], ("skipped", 1))
        art = self.conn.execute("SELECT * FROM items WHERE source='artifact'").fetchone()
        self.assertEqual(art["group_label"], "Write a report")
        self.assertNotIn("secret.txt", art["source_ref"], "user-pasted MEDIA lines are not artifacts")

    def test_unchanged_runs_are_quiet(self):
        out = self.home / "cron" / "output" / "job1"
        for i, stamp in enumerate(["2026-11-03_08-00-00.md", "2026-11-04_08-00-00.md"]):
            p = out / stamp
            p.write_text(AGENT_RUN.replace("Disk at 52%", f"Disk at {60 + i}%")
                         .replace("2026-11-02 08:00:53", f"2026-11-0{3 + i} 08:00:00"))
            os.utime(p, (time.time() - 60, time.time() - 60))
        indexer.Indexer(self.conn, self.home).run()
        rows = self.conn.execute("SELECT status FROM items WHERE group_key='job1' ORDER BY ts").fetchall()
        self.assertEqual([r[0] for r in rows], ["ok", "failed", "unchanged", "unchanged"])


class ServerTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.tmp = Path(tempfile.mkdtemp())
        home = make_home(cls.tmp)
        os.environ["HERMES_INBOX_TOKEN"] = "test-token"
        cls.app = server.App(cls.tmp / "inbox.db", home)
        cls.app.reindex()
        cls.httpd = ThreadingHTTPServer(("127.0.0.1", 0), server.make_handler(cls.app))
        threading.Thread(target=cls.httpd.serve_forever, daemon=True).start()
        cls.base = f"http://127.0.0.1:{cls.httpd.server_address[1]}"

    @classmethod
    def tearDownClass(cls):
        cls.httpd.shutdown()
        os.environ.pop("HERMES_INBOX_TOKEN", None)

    def req(self, path, token="test-token", data=None, ctype="application/json"):
        r = urllib.request.Request(self.base + path, data=data)
        if token:
            r.add_header("Authorization", f"Bearer {token}")
        if data is not None:
            r.add_header("Content-Type", ctype)
        try:
            with urllib.request.urlopen(r) as resp:
                return resp.status, resp.read()
        except urllib.error.HTTPError as e:
            return e.code, e.read()

    def test_auth_required(self):
        self.assertEqual(self.req("/api/summary", token=None)[0], 401)
        self.assertEqual(self.req("/api/summary", token="wrong")[0], 401)
        self.assertEqual(self.req("/api/summary")[0], 200)

    def test_only_published_artifacts_are_served(self):
        items = json.loads(self.req("/api/items?view=all&source=artifact")[1])["items"]
        status, body = self.req(f"/api/items/{items[0]['id']}/file")
        self.assertEqual((status, body), (200, b"# Report\n\nBody"))
        cron = json.loads(self.req("/api/items?view=all&source=cron")[1])["items"][0]
        self.assertEqual(self.req(f"/api/items/{cron['id']}/file")[0], 404)

    def test_writes_need_json_and_flags_persist(self):
        iid = json.loads(self.req("/api/items?view=all")[1])["items"][0]["id"]
        self.assertEqual(self.req(f"/api/items/{iid}", data=b"starred=1", ctype="application/x-www-form-urlencoded")[0], 415)
        self.assertEqual(self.req(f"/api/items/{iid}", data=json.dumps({"starred": True}).encode())[0], 200)
        starred = json.loads(self.req("/api/items?view=starred")[1])["items"]
        self.assertEqual([i["id"] for i in starred], [iid])

    def test_notes_and_search(self):
        s, b = self.req("/api/notes", data=json.dumps({"title": "Disk almost full", "body": "zebrafish volume at 91%",
                                                         "level": "attention"}).encode())
        self.assertEqual(s, 201)
        hits = json.loads(self.req("/api/items?view=all&q=zebrafish")[1])["items"]
        self.assertEqual([h["title"] for h in hits], ["Disk almost full"])
        self.assertTrue(hits[0]["attention"])


if __name__ == "__main__":
    unittest.main()
