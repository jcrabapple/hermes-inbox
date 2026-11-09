"""Build a throwaway Hermes home full of fictional activity, for demos and screenshots.

    python3 tools/demo_home.py /tmp/hermes-demo
    HERMES_INBOX_DATA=/tmp/hermes-demo/inbox HERMES_INBOX_TOKEN=demo \\
        python3 -m hermes_inbox --hermes-home /tmp/hermes-demo serve --port 8950

Everything here is made up. Nothing reads your real ~/.hermes.
"""
from __future__ import annotations

import json
import os
import sqlite3
import sys
import time
from datetime import datetime, timedelta
from pathlib import Path

NOW = time.time()
H = 3600

JOBS = [
    ("a1b2c3d4e5f6", "Server Health Check", "every day at 8am"),
    ("b2c3d4e5f6a1", "Weekly Tech News Digest", "0 19 * * 5"),
    ("c3d4e5f6a1b2", "Nightly Backup", "every day at 4am"),
    ("d4e5f6a1b2c3", "Release Watch: home-assistant", "every 6h"),
    ("e5f6a1b2c3d4", "Disk Space Watchdog", "every 30m"),
    ("f6a1b2c3d4e5", "Garden Weather Alerts", "every day at 6am"),
    ("a7b8c9d0e1f2", "Inbox Triage", "every 15m"),
]

RUNS = [
    # (job index, hours ago, kind, response)
    (0, 3.2, "agent", "HEALTH=DEGRADED (1 issue: certificate expires in 6 days)\n\n"
     "The TLS certificate for `status.example.org` renews through certbot, but the last renewal "
     "**failed** with a DNS-01 timeout.\n\n- web: 200, 142 ms\n- api: 200, 88 ms\n- database: healthy, 61% of connections free\n\n"
     "Action needed: re-run `certbot renew --dry-run` and check the DNS provider token."),
    (0, 27.2, "agent", "HEALTH=OK\n\nAll 6 checks passed. p95 latency 131 ms, error rate 0.02%."),
    (1, 5.5, "agent", "Tech digest: 9 stories this week\n\n"
     "## Highlights\n\n1. **SQLite 3.53** ships faster FTS5 prefix queries.\n2. A practical guide to passkeys on self-hosted apps.\n"
     "3. Why your cron jobs should be idempotent.\n\n| Topic | Stories |\n|---|---|\n| Databases | 3 |\n| Security | 4 |\n| Tooling | 2 |\n\n"
     "Email sent to the digest list."),
    (2, 8.1, "agent", "Backup finished: 2.4 GB snapshot, verified restore of 3 sample files.\n\n"
     "Retention pruned 2 snapshots older than 14 days. Offsite copy uploaded."),
    (2, 32.1, "error", "RuntimeError: HTTP 503 from the storage endpoint after 3 retries"),
    (3, 1.4, "agent", "New release: home-assistant 2026.11.1\n\n"
     "Patch release with 14 fixes. Nothing in the breaking-changes list touches your integrations "
     "(zha, mqtt, met). Safe to update this weekend."),
    (4, 0.4, "agent", "Disk OK: / at 52%, /data at 71%."),
    (4, 0.9, "agent", "Disk OK: / at 52%, /data at 70%."),
    (4, 1.4, "agent", "Disk OK: / at 52%, /data at 70%."),
    (5, 6.3, "agent", "Frost tonight: low of -1°C around 5am\n\nCover the tomatoes and bring the basil inside. "
     "Rain returns Thursday afternoon (8 mm)."),
    (6, 0.2, "skipped", ""),
    (6, 0.5, "skipped", ""),
    (6, 2.0, "agent", "2 messages need your decision\n\n- **Invoice from Acme Hosting** ($24.00), due Friday.\n"
     "- **Dentist** asked to move Tuesday's appointment to 3pm.\n\n11 newsletters archived, 3 receipts filed."),
]

ARTIFACTS = [
    ("trip-plan.md", 2.6, "Plan a weekend in the mountains",
     "# Weekend trip plan\n\n**Friday**: leave at 4pm, 3h drive, check in by 7:30.\n\n"
     "**Saturday**: ridge trail loop (11 km, 650 m gain), picnic at the lookout.\n\n"
     "**Sunday**: farmers market, home by 3pm.\n\n| Item | Cost |\n|---|---|\n| Cabin | $310 |\n| Fuel | $60 |\n| Food | $90 |\n",
     "Here's the plan with the drive times and a budget table."),
    ("cost-report.html", 26, "Monthly cloud cost report",
     "<!doctype html><meta charset=utf-8><title>Costs</title><body style='font:15px system-ui;padding:24px'>"
     "<h2>October costs</h2><p>Total <b>$41.18</b>, down 12% from September.</p>"
     "<ul><li>Compute $22.40</li><li>Storage $11.03</li><li>Egress $7.75</li></ul></body>",
     "I built the report as a small HTML page you can open on any device."),
    ("chart.svg", 50, "Chart my running distances",
     "<svg xmlns='http://www.w3.org/2000/svg' viewBox='0 0 320 160'><rect width='320' height='160' fill='#f4f6fb'/>"
     + "".join(f"<rect x='{20 + i * 40}' y='{140 - h}' width='26' height='{h}' rx='4' fill='#4a6fd1'/>"
               for i, h in enumerate([40, 70, 55, 95, 80, 120, 105]))
     + "</svg>",
     "Seven weeks of distance, steadily climbing."),
]

# Two weeks of routine history so the usage chart has something to show.
for _day in range(2, 15):
    RUNS.append((0, _day * 24 + 3.2, "agent", "HEALTH=OK\n\nAll 6 checks passed."))
    RUNS.append((2, _day * 24 + 8.1, "agent", f"Backup finished: {2.2 + _day / 50:.1f} GB snapshot, verified restore."))
    if _day % 7 == 5:
        RUNS.append((1, _day * 24 + 5.5, "agent", "Tech digest: 8 stories this week\n\nEmail sent to the digest list."))

# (model, cost per run) used for each job's fake session records
JOB_MODELS = {
    "a1b2c3d4e5f6": ("z-ai/glm-5.3-flash", 0.0042),
    "b2c3d4e5f6a1": ("deepseek/deepseek-v4.1-flash", 0.061),
    "c3d4e5f6a1b2": ("deepseek/deepseek-v4.1-flash", 0.0093),
    "d4e5f6a1b2c3": ("z-ai/glm-5.3-flash", 0.0031),
    "e5f6a1b2c3d4": ("z-ai/glm-5.3-flash", 0.0007),
    "f6a1b2c3d4e5": ("z-ai/glm-5.3-flash", 0.0026),
    "a7b8c9d0e1f2": ("anthropic/claude-haiku-5.5", 0.0188),
    "c9d0e1f2a3b4": ("deepseek/deepseek-v4.1-flash", 0.0062),
}

KANBAN = [
    ("t_1", "Add dark mode to the settings page", "coder", "completed", 4.0,
     "Dark mode added behind the existing theme toggle; 12 tests added, all green."),
    ("t_2", "Review: dark mode settings page", "reviewer", "completed", 3.1,
     "Approved. One nit: the focus ring on the toggle needs 3:1 contrast. Fixed in follow-up."),
    ("t_3", "Migrate image uploads to object storage", "coder", "blocked", 1.2,
     "Blocked: needs the storage bucket credentials before the migration can run."),
]

# A second profile with its own cron job, so profile tags and the profile filter
# have something to show in screenshots.
PROFILE = ("writer", "c9d0e1f2a3b4", "Reading Queue Digest", "0 7 * * 1")
PROFILE_RUNS = [
    (5.5, "Reading queue: 14 items, 3 worth your time\n\n"
     "1. **Postgres query plans in plain English** — directly useful for the FeedEcho slow page.\n"
     "2. A field guide to self-hosting DNS.\n3. Why small banks write better software.\n\n"
     "The other 11 were skimmed and filed. Digest emailed."),
    (29.5, "Reading queue: 12 items, 2 worth your time\n\n"
     "1. **CSVs considered harmful, again** — links to the RFC quote we argued about.\n"
     "2. Letter writing as an engineering habit.\n\nDigest emailed."),
]


def write_profile(home: Path):
    """A writer profile with its own cron job, sessions and state.db."""
    prof, jid, job, sched = PROFILE
    pdir = home / "profiles" / prof
    out = pdir / "cron" / "output" / jid
    out.mkdir(parents=True, exist_ok=True)
    (pdir / "cron" / "jobs.json").write_text(json.dumps(
        {"jobs": [{"id": jid, "name": job, "enabled": True, "deliver": "local",
                   "schedule": {"kind": "cron", "display": sched}}]}))
    conn = sqlite3.connect(pdir / "state.db")
    conn.executescript("""
        CREATE TABLE IF NOT EXISTS sessions (id TEXT PRIMARY KEY, source TEXT, title TEXT, cwd TEXT, display_name TEXT,
            model TEXT, billing_provider TEXT, input_tokens INTEGER, output_tokens INTEGER, cache_read_tokens INTEGER,
            cache_write_tokens INTEGER, reasoning_tokens INTEGER, estimated_cost_usd REAL, actual_cost_usd REAL,
            api_call_count INTEGER, started_at REAL, ended_at REAL, parent_session_id TEXT);
    """)
    model, cost = JOB_MODELS[jid]
    for n, (hours, resp) in enumerate(PROFILE_RUNS):
        ts = NOW - hours * H
        head = f"# Cron Job: {job}\n\n**Job ID:** {jid}\n**Run Time:** {stamp(ts, '%Y-%m-%d %H:%M:%S')}\n"
        text = head + f"**Schedule:** {sched}\n\n## Prompt\n\nRun the job.\n\n" \
                      f"**Response Characters:** {len(resp)}\n## Response\n\n{resp}\n"
        f = out / f"{stamp(ts, '%Y-%m-%d_%H-%M-%S')}.md"
        f.write_text(text)
        os.utime(f, (ts, ts))
        wobble = 0.8 + (n * 41 % 50) / 100
        conn.execute(
            "INSERT OR REPLACE INTO sessions (id, source, title, cwd, display_name, model, billing_provider, "
            "input_tokens, output_tokens, cache_read_tokens, cache_write_tokens, reasoning_tokens, "
            "estimated_cost_usd, actual_cost_usd, api_call_count, started_at, ended_at) "
            "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (f"demo-{prof}-{n}", "cron", f"{job} · {stamp(ts, '%b %d %H:%M')}", None, None, model, "openrouter",
             int(8000 * wobble), int(600 * wobble), int(26000 * wobble), 0, 0, round(cost * wobble, 6), None,
             3, ts - 40, ts))
    conn.commit()
    conn.close()


def stamp(ts: float, fmt: str) -> str:
    return datetime.fromtimestamp(ts).strftime(fmt)


def write_cron(home: Path):
    out = home / "cron" / "output"
    jobs = []
    for jid, name, sched in JOBS:
        jobs.append({"id": jid, "name": name, "enabled": True, "deliver": "local",
                     "schedule": {"kind": "cron", "display": sched}})
    (home / "cron").mkdir(parents=True, exist_ok=True)
    # One job the scheduler itself calls broken, so Job health has something to show.
    now = datetime.now()
    jobs.append({"id": "d4a1b7c9e0f2", "name": "Nightly Backup Verify", "enabled": True,
                 "deliver": "local", "schedule": {"kind": "cron", "display": "0 3 * * *"},
                 "next_run_at": (now + timedelta(hours=6)).isoformat(),
                 "last_run_at": (now - timedelta(minutes=95)).isoformat(),
                 "last_status": "error", "failure_streak": 3,
                 "last_error": "RuntimeError: restic snapshot not found for the last 24h",
                 "last_failure": {"at": (now - timedelta(hours=1, minutes=35)).isoformat()}})
    jobs.append({"id": "e5f6a1b2c3d4", "name": "Feed watcher", "enabled": False, "state": "paused",
                 "paused_at": (now - timedelta(hours=30)).isoformat(),
                 "paused_reason": "waiting on the FeedEcho API key",
                 "schedule": {"kind": "cron", "display": "0 8 * * *"},
                 "next_run_at": (now - timedelta(hours=26)).isoformat(),
                 "last_run_at": (now - timedelta(days=2)).isoformat(),
                 "last_status": "error", "failure_streak": 2, "deliver": "local"})
    (home / "cron" / "jobs.json").write_text(json.dumps({"jobs": jobs}, indent=2))
    for idx, hours, kind, resp in RUNS:
        jid, name, sched = JOBS[idx]
        ts = NOW - hours * H
        d = out / jid
        d.mkdir(parents=True, exist_ok=True)
        head = f"# Cron Job: {name}{' (FAILED)' if kind == 'error' else ''}\n\n**Job ID:** {jid}\n" \
               f"**Run Time:** {stamp(ts, '%Y-%m-%d %H:%M:%S')}\n"
        if kind == "skipped":
            text = head + "\nScript gate returned `wakeAgent=false` — agent skipped.\n"
        elif kind == "error":
            text = head + f"**Schedule:** {sched}\n\n## Prompt\n\nRun the job.\n\n## Error\n\n```\n{resp}\n```\n"
        else:
            text = head + f"**Schedule:** {sched}\n\n## Prompt\n\nRun the job.\n\n" \
                          f"**Response Characters:** {len(resp)}\n## Response\n\n{resp}\n"
        f = d / f"{stamp(ts, '%Y-%m-%d_%H-%M-%S')}.md"
        f.write_text(text)
        os.utime(f, (ts, ts))
        if kind != "skipped":
            SESSIONS.append((jid, name, ts))


SESSIONS: list = []


def write_sessions(home: Path):
    """Cron session records with token counts and cost, the way Hermes stores them."""
    conn = sqlite3.connect(home / "state.db")
    for n, (jid, name, ts) in enumerate(SESSIONS):
        model, cost = JOB_MODELS[jid]
        wobble = 0.8 + (n * 37 % 50) / 100  # deterministic spread so the chart isn't flat
        dur = 30 + (n * 53 % 200)
        conn.execute(
            "INSERT OR REPLACE INTO sessions (id, source, title, cwd, display_name, model, billing_provider, "
            "input_tokens, output_tokens, cache_read_tokens, cache_write_tokens, reasoning_tokens, "
            "estimated_cost_usd, actual_cost_usd, api_call_count, started_at, ended_at) "
            "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (f"demo-cron-{n}", "cron", f"{name} · {stamp(ts, '%b %d %H:%M')}", None, None, model, "openrouter",
             int(9000 * wobble), int(700 * wobble), int(30000 * wobble), 0, 0, round(cost * wobble, 6), None,
             3 + n % 6, ts - dur, ts))
    conn.commit()
    conn.close()


def write_chats(home: Path):
    files = home / "demo-files"
    files.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(home / "state.db")
    conn.executescript("""
        CREATE TABLE IF NOT EXISTS sessions (id TEXT PRIMARY KEY, source TEXT, title TEXT, cwd TEXT, display_name TEXT,
            model TEXT, billing_provider TEXT, input_tokens INTEGER, output_tokens INTEGER, cache_read_tokens INTEGER,
            cache_write_tokens INTEGER, reasoning_tokens INTEGER, estimated_cost_usd REAL, actual_cost_usd REAL,
            api_call_count INTEGER, started_at REAL, ended_at REAL, parent_session_id TEXT);
        CREATE TABLE IF NOT EXISTS messages (id INTEGER PRIMARY KEY, session_id TEXT, role TEXT, content TEXT, timestamp REAL);
    """)
    for n, (name, hours, title, content, said) in enumerate(ARTIFACTS, 1):
        p = files / name
        p.write_text(content)
        ts = NOW - hours * H
        os.utime(p, (ts, ts))
        sid = f"demo-session-{n}"
        conn.execute("INSERT OR REPLACE INTO sessions (id, source, title, cwd, started_at) VALUES (?,?,?,?,?)",
                     (sid, ["desktop", "telegram", "cli"][n % 3], title, str(files), ts))
        conn.execute("INSERT OR REPLACE INTO messages VALUES (?,?,?,?,?)", (n, sid, "assistant", f"{said}\n\nMEDIA:{p}\n", ts))
    conn.commit()
    conn.close()


def write_kanban(home: Path):
    board = home / "kanban" / "boards" / "website"
    board.mkdir(parents=True, exist_ok=True)
    (board / "board.json").write_text(json.dumps({"slug": "website", "name": "Website refresh"}))
    conn = sqlite3.connect(board / "kanban.db")
    conn.executescript("""
        CREATE TABLE IF NOT EXISTS tasks (id TEXT PRIMARY KEY, title TEXT, body TEXT, assignee TEXT, status TEXT);
        CREATE TABLE IF NOT EXISTS task_runs (id INTEGER PRIMARY KEY, task_id TEXT, profile TEXT, status TEXT, outcome TEXT,
            summary TEXT, metadata TEXT, error TEXT, started_at INTEGER, ended_at INTEGER);
        CREATE TABLE IF NOT EXISTS task_attachments (id INTEGER PRIMARY KEY, task_id TEXT, filename TEXT, stored_path TEXT,
            content_type TEXT, created_at INTEGER);
    """)
    for n, (tid, title, prof, outcome, hours, summary) in enumerate(KANBAN, 1):
        ended = int(NOW - hours * H)
        conn.execute("INSERT OR REPLACE INTO tasks VALUES (?,?,?,?,?)", (tid, title, "", prof, "done" if outcome == "completed" else outcome))
        conn.execute("INSERT OR REPLACE INTO task_runs VALUES (?,?,?,?,?,?,?,?,?,?)",
                     (n, tid, prof, "done", outcome, summary, "{}", None, ended - 900, ended))
    conn.commit()
    conn.close()


def write_delegations(home: Path):
    """One delegation whose result never made it back to its conversation."""
    conn = sqlite3.connect(home / "state.db")
    conn.executescript("""
        CREATE TABLE IF NOT EXISTS async_delegations (
            delegation_id TEXT PRIMARY KEY, state TEXT, dispatched_at REAL, completed_at REAL,
            updated_at REAL, delivery_state TEXT, delivery_attempts INTEGER, delivered_at REAL,
            task_json TEXT, result_json TEXT, origin_session TEXT);
    """)
    done = NOW - 26 * H
    goal = "Audit the FeedEcho retry path for duplicate deliveries and list every caller of deliver()."
    conn.execute(
        "INSERT OR REPLACE INTO async_delegations VALUES (?,?,?,?,?,?,?,?,?,?,?)",
        ("deleg_demo_retry", "completed", done - 1800, done, done, "dropped", 8, None,
         json.dumps({"goal": goal, "goals": [goal]}),
         json.dumps({"results": [{"status": "completed", "summary":
                                  "Three duplicate-delivery paths found. deliver() is called from "
                                  "queue_worker.py:88 (no dedupe key), retry.py:41 (re-queues on a "
                                  "409 instead of acking) and api.py:203 (manual resend). Adding the "
                                  "item id as an idempotency key to all three closes it; retry.py:41 "
                                  "also needs to treat 409 as success."}],
                     "live_transcripts": ["/tmp/hermes-demo/deleg/deleg_demo_retry/task-0.log"]}),
         "demo-session-1"))
    conn.commit()
    conn.close()


def write_processes(home: Path):
    """One checkpointed background process whose PID is long gone."""
    (home / "processes.json").write_text(json.dumps([{
        "session_id": "proc_9f2c44", "command": "pytest -x tests/ --watch",
        "pid": 999999, "pid_scope": "host", "started_at": NOW - 3 * H,
        "cwd": "/home/user/feedecho", "notify_on_complete": True,
        "output_log": "/tmp/hermes-demo/processes/proc_9f2c44.log"}], indent=2))


def main():
    home = Path(sys.argv[1] if len(sys.argv) > 1 else "/tmp/hermes-demo").expanduser()
    home.mkdir(parents=True, exist_ok=True)
    write_cron(home)
    write_chats(home)
    write_sessions(home)
    write_delegations(home)
    write_processes(home)
    write_profile(home)
    write_kanban(home)
    print(f"Demo Hermes home written to {home}")


if __name__ == "__main__":
    main()
