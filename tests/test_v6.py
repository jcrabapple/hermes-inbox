"""v6: paused cron jobs and their reasons.

Pausing a job sets enabled=False + state='paused' + paused_at, so a paused job used to
look exactly like a deliberately disabled one. These cover the third job-health incident.

Run: python3 -m unittest discover -s tests
"""
import json
import sys
import tempfile
import time
import unittest
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
sys.path.insert(0, str(Path(__file__).resolve().parent))

from hermes_inbox import indexer, sources, store  # noqa: E402


def iso(ts: float) -> str:
    return datetime.fromtimestamp(ts, timezone.utc).isoformat()


def paused_job(reason="waiting on the FeedEcho API key", hours=5, **kw) -> dict:
    now = time.time()
    job = {"id": "j", "name": "Feed watcher", "enabled": False, "state": "paused",
           "paused_at": iso(now - hours * 3600), "paused_reason": reason,
           "schedule": {"kind": "cron", "display": "0 8 * * *"},
           "next_run_at": iso(now - 4 * 3600), "last_run_at": iso(now - 26 * 3600),
           "last_status": "ok", "failure_streak": 0}
    job.update(kw)
    return job


class PausedJobTests(unittest.TestCase):
    def test_paused_job_with_a_reason_is_an_attention_item(self):
        now = time.time()
        jobs = {"j": paused_job()}
        items = sources.scheduler_incidents("default", jobs, now)
        self.assertEqual(len(items), 1)
        it = items[0]
        self.assertEqual(it["source_ref"], "default:j:paused")
        self.assertEqual(it["status"], "paused")
        self.assertTrue(it["attention"])
        self.assertEqual(it["title"], "Paused: waiting on the FeedEcho API key")
        self.assertEqual(it["extra"]["kind"], "paused")
        self.assertEqual(it["extra"]["paused_reason"], "waiting on the FeedEcho API key")
        self.assertAlmostEqual(it["ts"], now - 5 * 3600, delta=2)
        self.assertIn("waiting on the FeedEcho API key", it["body"])
        self.assertIn("hermes cron resume", it["body"])

    def test_paused_without_a_reason_is_quiet(self):
        jobs = {"j": paused_job(reason=None)}
        it = sources.scheduler_incidents("default", jobs)[0]
        self.assertFalse(it["attention"])
        self.assertEqual(it["title"], "Paused with no reason")
        self.assertIn("No reason was recorded", it["body"])

    def test_paused_job_reports_only_the_pause(self):
        # Overdue and failing conditions would both fire on this record; pause wins.
        now = time.time()
        jobs = {"j": paused_job(failure_streak=7, last_error="boom",
                                next_run_at=iso(now - 9 * 3600))}
        items = sources.scheduler_incidents("default", jobs, now)
        self.assertEqual([i["status"] for i in items], ["paused"])
        self.assertIn("failed 7 times before it was paused", items[0]["body"])

    def test_state_paused_without_paused_at_still_counts(self):
        jobs = {"j": paused_job(paused_at=None)}
        it = sources.scheduler_incidents("default", jobs)[0]
        self.assertEqual(it["status"], "paused")
        self.assertAlmostEqual(it["ts"], time.time(), delta=5)

    def test_a_disabled_job_is_not_paused(self):
        jobs = {"j": {"id": "j", "name": "Old job", "enabled": False, "state": "disabled"}}
        self.assertEqual(sources.scheduler_incidents("default", jobs), [])


class PausedIndexTests(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        self.home = self.tmp / "hermes"
        (self.home / "cron").mkdir(parents=True)
        self.conn = store.connect(self.tmp / "inbox.db")
        self.idx = indexer.Indexer(self.conn, self.home)

    def write(self, *jobs):
        (self.home / "cron" / "jobs.json").write_text(json.dumps({"jobs": list(jobs)}))

    def row(self):
        r = self.conn.execute("SELECT * FROM items WHERE source_ref='default:j:paused'").fetchone()
        return dict(r) if r else None

    def test_resume_resolves_and_archives(self):
        self.write(paused_job())
        self.assertEqual(self.idx.run()["scheduler"], 1)
        self.assertEqual(self.row()["status"], "paused")
        resumed = paused_job(paused_at=None, paused_reason=None, enabled=True, state="scheduled",
                             next_run_at=iso(time.time() + 3600))
        self.write(resumed)
        self.idx.run()
        row = self.row()
        self.assertEqual(row["status"], "resolved")
        self.assertEqual(row["archived"], 1)
        self.assertEqual(row["attention"], 0)
        self.assertIn("running again", row["summary"])

    def test_pausing_again_reopens_unread(self):
        self.write(paused_job())
        self.idx.run()
        self.conn.execute("UPDATE items SET read=1 WHERE source='scheduler'")
        self.conn.commit()
        self.write(paused_job(paused_at=None, paused_reason=None, enabled=True, state="scheduled"))
        self.idx.run()
        self.assertEqual(self.row()["status"], "resolved")
        self.write(paused_job(reason="disk full again"))
        self.idx.run()
        row = self.row()
        self.assertEqual(row["status"], "paused")
        self.assertEqual(row["archived"], 0)
        self.assertEqual(row["read"], 0)
        self.assertIn("disk full again", row["title"])

    def test_a_deleted_paused_job_is_resolved(self):
        self.write(paused_job())
        self.idx.run()
        self.write()
        self.idx.run()
        self.assertEqual(self.row()["status"], "resolved")


if __name__ == "__main__":
    unittest.main()
