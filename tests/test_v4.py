"""v4: scheduled-job health incidents and snoozing.

Run: python3 -m unittest discover -s tests
"""
import json
import os
import sys
import tempfile
import threading
import time
import unittest
import urllib.error
import urllib.request
from datetime import datetime, timedelta, timezone
from http.server import ThreadingHTTPServer
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
sys.path.insert(0, str(Path(__file__).resolve().parent))

from hermes_inbox import indexer, server, sources, store  # noqa: E402
from test_inbox import make_home  # noqa: E402


def iso(ts: float) -> str:
    return datetime.fromtimestamp(ts, timezone.utc).isoformat()


def job(**kw) -> dict:
    base = {"id": "j", "name": "Job", "enabled": True, "schedule": {"kind": "cron", "display": "0 8 * * *"},
            "failure_streak": 0, "next_run_at": iso(time.time() + 3600)}
    base.update(kw)
    return base


class SchedulerIncidentTests(unittest.TestCase):
    def test_cron_job_within_the_hour_is_not_late(self):
        now = time.time()
        for late in (60, 1800, 3500):
            jobs = {"j": job(next_run_at=iso(now - late), last_run_at=iso(now - late - 86400))}
            self.assertEqual(sources.scheduler_incidents("default", jobs, now), [], f"{late}s late")

    def test_cron_job_over_an_hour_late_is_an_incident(self):
        now = time.time()
        jobs = {"j": job(id="j", name="Nightly report", next_run_at=iso(now - 3 * 3600),
                         last_run_at=iso(now - 27 * 3600), last_status="ok")}
        items = sources.scheduler_incidents("default", jobs, now)
        self.assertEqual(len(items), 1)
        it = items[0]
        self.assertEqual(it["source"], "scheduler")
        self.assertEqual(it["source_ref"], "default:j:overdue")
        self.assertEqual(it["status"], "overdue")
        self.assertTrue(it["attention"])
        self.assertEqual(it["group_label"], "Nightly report")
        self.assertAlmostEqual(it["ts"], now - 3 * 3600, delta=1)
        self.assertIn("Overdue", it["title"])

    def test_interval_job_gets_three_periods_of_grace(self):
        now = time.time()
        five = {"kind": "interval", "minutes": 5, "display": "every 5m"}
        # 10 minutes late is inside 3x5m + floor; 25 minutes is not.
        jobs = {"j": job(schedule=five, next_run_at=iso(now - 10 * 60))}
        self.assertEqual(sources.scheduler_incidents("default", jobs, now), [])
        jobs = {"j": job(schedule=five, next_run_at=iso(now - 25 * 60))}
        self.assertEqual(len(sources.scheduler_incidents("default", jobs, now)), 1)
        self.assertEqual(sources.overdue_grace({"schedule": five}), 900.0)
        self.assertEqual(sources.overdue_grace({"schedule": {"kind": "interval", "minutes": 600}}), 3600.0)

    def test_failure_streak_two_is_an_incident(self):
        now = time.time()
        jobs = {"j": job(failure_streak=1)}
        self.assertEqual(sources.scheduler_incidents("default", jobs, now), [])
        jobs = {"j": job(name="Mail poll", failure_streak=4, last_error="RuntimeError: HTTP 402",
                         last_failure={"at": iso(now - 7200), "detail": "worker exited -15"},
                         last_run_at=iso(now - 60))}
        items = sources.scheduler_incidents("default", jobs, now)
        self.assertEqual(len(items), 1)
        it = items[0]
        self.assertEqual(it["source_ref"], "default:j:failing")
        self.assertEqual(it["status"], "failing")
        self.assertIn("4 runs in a row", it["title"])
        self.assertEqual(it["summary"], "RuntimeError: HTTP 402")
        self.assertIn("HTTP 402", it["body"])
        self.assertAlmostEqual(it["ts"], now - 7200, delta=1)

    def test_disabled_and_paused_jobs_are_ignored(self):
        now = time.time()
        jobs = {"a": job(enabled=False, failure_streak=9, next_run_at=iso(now - 5 * 3600)),
                "b": job(paused_at=iso(now - 86400), failure_streak=9,
                         next_run_at=iso(now - 5 * 3600))}
        self.assertEqual(sources.scheduler_incidents("default", jobs, now), [])

    def test_bad_timestamps_do_not_raise(self):
        self.assertIsNone(sources.parse_iso("not a date"))
        self.assertIsNone(sources.parse_iso(None))
        now = time.time()
        self.assertEqual(sources.scheduler_incidents("default", {"j": job(next_run_at="garbage")}, now), [])


class SchedulerIndexTests(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        self.home = self.tmp / "hermes"
        (self.home / "cron").mkdir(parents=True)
        self.conn = store.connect(self.tmp / "inbox.db")
        self.idx = indexer.Indexer(self.conn, self.home)

    def write_jobs(self, jobs: dict):
        (self.home / "cron" / "jobs.json").write_text(json.dumps({"jobs": list(jobs.values())}))

    def items(self):
        return [dict(r) for r in self.conn.execute("SELECT * FROM items WHERE source='scheduler' ORDER BY id")]

    def test_incident_is_one_item_that_keeps_its_start_time(self):
        now = time.time()
        self.write_jobs({"j": job(name="Nightly", next_run_at=iso(now - 2 * 3600))})
        self.assertEqual(self.idx.run()["scheduler"], 1)
        first = self.items()[0]
        self.assertEqual(first["status"], "overdue")
        self.assertEqual(first["read"], 0)
        # A later pass with a later due time must not add or move the item.
        self.write_jobs({"j": job(name="Nightly", next_run_at=iso(now - 3600))})
        self.assertEqual(self.idx.run()["scheduler"], 0)
        again = self.items()[0]
        self.assertEqual(again["id"], first["id"])
        self.assertEqual(again["ts"], first["ts"])

    def test_recovery_resolves_and_archives(self):
        self.write_jobs({"j": job(failure_streak=3, last_error="boom")})
        self.idx.run()
        self.assertEqual(self.items()[0]["status"], "failing")
        self.write_jobs({"j": job(failure_streak=0)})
        self.idx.run()
        row = self.items()[0]
        self.assertEqual(row["status"], "resolved")
        self.assertEqual(row["archived"], 1)
        self.assertEqual(row["attention"], 0)
        self.assertIn("Resolved", row["body"])

    def test_a_new_outage_after_recovery_reopens_the_item(self):
        now = time.time()
        self.write_jobs({"j": job(failure_streak=2)})
        self.idx.run()
        self.idx.conn.execute("UPDATE items SET read=1 WHERE source='scheduler'")
        self.idx.conn.commit()
        self.write_jobs({"j": job(failure_streak=0)})
        self.idx.run()
        self.write_jobs({"j": job(failure_streak=2, last_failure={"at": iso(now)})})
        self.idx.run()
        row = self.items()[0]
        self.assertEqual(row["status"], "failing")
        self.assertEqual(row["archived"], 0)
        self.assertEqual(row["read"], 0)


class SnoozeApiTests(unittest.TestCase):
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

    @classmethod
    def tearDownClass(cls):
        cls.httpd.shutdown()
        os.environ.pop("HERMES_INBOX_TOKEN", None)

    def req(self, path, body=None):
        data = json.dumps(body).encode() if body is not None else None
        r = urllib.request.Request(self.base + path, data=data)
        r.add_header("Authorization", "Bearer t")
        if data:
            r.add_header("Content-Type", "application/json")
        try:
            with urllib.request.urlopen(r) as res:
                return json.loads(res.read())
        except urllib.error.HTTPError as e:
            return {"code": e.code, **json.loads(e.read())}

    def note(self, title="Disk warning"):
        return self.req("/api/notes", {"title": title, "body": "body", "level": "error",
                                       "group": "Disk watch"})["id"]

    def views(self):
        return self.req("/api/summary")["views"]

    def ids(self, view):
        return {i["id"] for i in self.req(f"/api/items?view={view}&limit=200")["items"]}

    def test_snoozed_item_leaves_the_inbox_and_comes_back(self):
        iid = self.note()
        self.assertIn(iid, self.ids("inbox"))
        before = self.views()
        self.assertEqual(self.req(f"/api/items/{iid}", {"snoozed_until": time.time() + 3600}), {"ok": True})

        self.assertNotIn(iid, self.ids("inbox"))
        self.assertNotIn(iid, self.ids("attention"))
        self.assertIn(iid, self.ids("snoozed"))
        self.assertIn(iid, self.ids("all"))
        after = self.views()
        self.assertEqual(after["inbox"], before["inbox"] - 1)
        self.assertEqual(after["attention"], before["attention"] - 1)
        self.assertEqual(after["snoozed"], 1)
        self.assertGreater(self.req("/api/summary")["next_snooze"], time.time())

        self.assertEqual(self.req(f"/api/items/{iid}", {"snoozed_until": None}), {"ok": True})
        self.assertIn(iid, self.ids("inbox"))
        self.assertNotIn(iid, self.ids("snoozed"))
        self.assertEqual(self.views()["snoozed"], 0)

    def test_snooze_time_must_be_in_the_future(self):
        iid = self.note()
        res = self.req(f"/api/items/{iid}", {"snoozed_until": time.time() - 60})
        self.assertEqual(res["code"], 400)
        self.assertIn("future", res["error"])
        res = self.req(f"/api/items/{iid}", {"snoozed_until": "soon"})
        self.assertEqual(res["code"], 400)

    def test_expired_snooze_is_active_again(self):
        iid = self.note("Old snooze")
        # A snooze that has already expired behaves like no snooze at all.
        self.app.db().execute("UPDATE items SET snoozed_until=? WHERE id=?", (time.time() - 5, iid))
        self.app.db().commit()
        self.assertIn(iid, self.ids("inbox"))
        self.assertNotIn(iid, self.ids("snoozed"))

    def test_filters_carry_the_snooze_clause(self):
        where, _ = server.build_filter({"view": "inbox"})
        self.assertIn("snoozed_until IS NULL", where)
        where, _ = server.build_filter({"view": "snoozed"})
        self.assertIn("snoozed_until > strftime", where)
        # The group view drops noise filtering but must keep snooze filtering.
        where, _ = server.build_filter({"view": "inbox", "group": "job1", "source": "cron"})
        self.assertNotIn("noise=0", where)
        self.assertIn("snoozed_until IS NULL", where)


if __name__ == "__main__":
    unittest.main()
