"""Readers that turn Hermes state into inbox items.

Every reader opens Hermes databases read-only and never writes to ~/.hermes.
"""
from __future__ import annotations

import difflib
import hashlib
import json
import mimetypes
import os
import re
import sqlite3
import time
from datetime import datetime
from pathlib import Path

BODY_CAP = 300_000
SCRIPT_CAP = 64_000
SETTLE_SECONDS = 5  # don't read a cron output file that is still being written

ATTENTION_RE = re.compile(
    r"\b(DEGRADED|FAILED|FAILURE|OUTAGE|URGENT)\b"
    r"|(?i:(?<!no )(?<!no operator )\b(?:action (?:needed|required)"
    r"|needs? your (?:decision|attention|input|approval|review)))"
)
ATTENTION_WINDOW = 1000  # only the top of a report counts; deep sections quote old incidents
# Alert emojis only count as a headline marker — in the first lines, near the start of a
# line. Reports ABOUT other reports quote ⚠/🔴 deep in their bodies; those are not alerts.
EMOJI_RE = re.compile(r"[\u26a0\U0001f6a8\u274c\U0001f534]")
EMOJI_LINES = 6
EMOJI_COL = 12
SILENT_RE = re.compile(r"^\s*(\[SILENT\]|status: silent\b)", re.I)
MEDIA_RE = re.compile(r"MEDIA:(\S+)")
PREVIEW_RE = re.compile(r"::preview\{file=\"?([^\"}]+)\"?\}")
TRAILING = "`'\")]>,.;:*"

ATTENTION_OUTCOMES = {"blocked", "gave_up", "spawn_failed"}
NOISE_OUTCOMES = {"reclaimed", "released"}


# ── helpers ──────────────────────────────────────────────────────────────

def ro_connect(path: Path) -> sqlite3.Connection:
    conn = sqlite3.connect(f"file:{path}?mode=ro", uri=True, timeout=10)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA busy_timeout=10000")
    return conn


def is_missing_table(exc: BaseException) -> bool:
    """A store that opened fine but has no table yet is empty, not unreadable."""
    return "no such table" in str(exc).lower()


def hermes_homes(root: Path):
    """Yield (profile_name, home_path) for the default home and every profile."""
    yield "default", root
    prof = root / "profiles"
    if prof.is_dir():
        for p in sorted(prof.iterdir()):
            # "default" duplicates the root home; ".deleted" holds retired profiles.
            if p.is_dir() and p.name not in ("default", ".deleted"):
                yield p.name, p


def strip_md(line: str) -> str:
    line = re.sub(r"!\[[^\]]*\]\([^)]*\)", "", line)
    line = re.sub(r"\[([^\]]+)\]\([^)]*\)", r"\1", line)
    line = re.sub(r"^[#>\-*+\s]+|^\d+\.\s+", "", line)
    line = re.sub(r"[*`~]+", "", line)
    line = re.sub(r"(?<!\w)_+|_+(?!\w)", "", line)
    return re.sub(r"\s+", " ", line).strip()


def summarize(text: str, limit: int = 240) -> tuple[str, str]:
    """Return (subject, preview) from markdown text."""
    lines = []
    for raw in text.splitlines():
        s = strip_md(raw)
        if not s or set(s) <= set("-=|:"):
            continue
        lines.append(s)
        if sum(len(x) for x in lines) > limit * 2:
            break
    if not lines:
        return "", ""
    subject = lines[0][:140]
    preview = " ".join(lines[1:])[:limit] if len(lines) > 1 else ""
    return subject, preview


def norm_hash(text: str) -> str:
    # Timestamps, counters and ids change every run; strip digits so a
    # watchdog saying the same thing with a new clock reads as unchanged.
    t = re.sub(r"\d+", "#", text.strip().lower())
    t = re.sub(r"\s+", " ", t)
    return hashlib.sha1(t.encode()).hexdigest()[:16]


def alert_emoji(text: str) -> bool:
    """True when an alert emoji leads one of the first few lines."""
    lines = [ln for ln in text.splitlines() if ln.strip()][:EMOJI_LINES]
    return any(EMOJI_RE.search(ln.lstrip("#>*-`~ \t")[:EMOJI_COL]) for ln in lines)


def diff_bodies(old: str, new: str, context: int = 2) -> dict | None:
    """Line diff of a run against the previous run of the same job.

    Returns hunks (changed lines with a little context) plus totals, or None
    when the bodies are identical.
    """
    a = (old or "").splitlines()[:2000]
    b = (new or "").splitlines()[:2000]
    sm = difflib.SequenceMatcher(None, a, b, autojunk=True)
    added_n = removed_n = 0
    hunks, cur = [], None
    for tag, i1, i2, j1, j2 in sm.get_opcodes():
        if tag == "equal":
            if cur:
                cur["after"] = a[i1:min(i2, i1 + context)]
                if i2 - i1 > context * 2:
                    hunks.append(cur)
                    cur = None
            continue
        if cur is None:
            cur = {"before": a[max(0, i1 - context):i1], "removed": [], "added": [], "after": []}
        if tag in ("delete", "replace"):
            cur["removed"] += a[i1:i2]
        if tag in ("insert", "replace"):
            cur["added"] += b[j1:j2]
    if cur:
        hunks.append(cur)
    if not hunks:
        return None
    for h in hunks:
        removed_n += len(h["removed"])
        added_n += len(h["added"])
        h["removed"] = h["removed"][:40]
        h["added"] = h["added"][:40]
    return {"added_n": added_n, "removed_n": removed_n, "hunks": hunks[:6],
            "more_hunks": max(0, len(hunks) - 6)}


def local_ts(s: str | None) -> float | None:
    if not s:
        return None
    for fmt in ("%Y-%m-%d %H:%M:%S", "%Y-%m-%d_%H-%M-%S", "%Y%m%d_%H%M%S"):
        try:
            return datetime.strptime(s.strip(), fmt).timestamp()
        except ValueError:
            continue
    return None


# ── cron ─────────────────────────────────────────────────────────────────

HEADER_RE = re.compile(r"^\*\*(Job ID|Run Time|Schedule|Mode):\*\*\s*(.+?)\s*$", re.M)


def _section_after(text: str, marker: str) -> str | None:
    idx = text.rfind(f"\n{marker}\n")
    if idx < 0:
        return None
    return text[idx + len(marker) + 2:]


def parse_cron_output(text: str) -> dict:
    """Parse a cron/output/<job>/<stamp>.md file into its parts."""
    first = text.split("\n", 1)[0]
    name = first[len("# Cron Job:"):].strip() if first.startswith("# Cron Job:") else ""
    failed_title = name.endswith("(FAILED)")
    if failed_title:
        name = name[: -len("(FAILED)")].strip()
    head = text[:2000]
    headers = {k: v for k, v in HEADER_RE.findall(head)}
    out = {
        "name": name,
        "job_id": headers.get("Job ID", ""),
        "run_time": headers.get("Run Time", ""),
        "schedule": headers.get("Schedule", ""),
        "mode": headers.get("Mode", ""),
        "response": None,
        "error": None,
        "script_output": None,
        "kind": "agent",
    }
    resp = _section_after(text, "## Response")
    err = _section_after(text, "## Error")
    if resp is not None and (err is None or text.rfind("\n## Response\n") > text.rfind("\n## Error\n")):
        out["response"] = resp.strip()
    elif err is not None:
        out["error"] = err.strip()
        out["kind"] = "error"

    so_idx = text.rfind("\n## Script Output\n")
    if so_idx >= 0:
        rest = text[so_idx + len("\n## Script Output\n"):]
        ends = [i for i in (rest.find("\n**Response Characters:**"), rest.find("\n## Response\n"),
                            rest.find("\n## Error\n")) if i >= 0]
        out["script_output"] = rest[: min(ends)] if ends else rest
        out["script_output"] = out["script_output"].strip()

    if out["response"] is None and out["error"] is None:
        # No agent turn: either a no_agent script job or a script gate skip.
        body_start = 0
        for m in HEADER_RE.finditer(head):
            body_start = max(body_start, m.end())
        body = text[body_start:].lstrip()
        if body.startswith("---"):
            body = body[3:].lstrip()
        if "wakeAgent=false" in body[:400]:
            out["kind"] = "skipped"
        else:
            out["kind"] = "script"
        out["response"] = body.strip()
    if failed_title and out["kind"] != "error":
        out["error"] = out["response"] or "Run marked FAILED by the scheduler."
        out["response"] = None
        out["kind"] = "error"
    return out


def load_jobs(home: Path) -> dict[str, dict]:
    return read_jobs(home)[0]


def read_jobs(home: Path) -> tuple[dict[str, dict], str]:
    """Job records plus how the file read went: read / absent / unreadable (why)."""
    path = home / "cron" / "jobs.json"
    if not path.exists():
        return {}, "absent"
    try:
        data = json.loads(path.read_text(encoding="utf-8-sig"))
    except (OSError, ValueError) as exc:
        return {}, f"unreadable ({type(exc).__name__})"
    jobs = data.get("jobs", data) if isinstance(data, dict) else data
    if not isinstance(jobs, list):
        return {}, "unreadable (not a list)"
    return {j.get("id"): j for j in jobs if isinstance(j, dict) and j.get("id")}, "read"


BOILERPLATE_RE = re.compile(r"^(generated|report generated|run time|date|timestamp)\b", re.I)


def cron_subject(body: str, name: str) -> tuple[str, str]:
    """Subject + preview for a run, skipping a heading that repeats the job name and
    "Generated <timestamp> by ..." boilerplate so the list shows what the run found."""
    lines = body.splitlines()
    while lines:
        first = strip_md(lines[0])
        if not first or first.lower().rstrip(":") == name.lower() or BOILERPLATE_RE.match(first):
            lines = lines[1:]
            continue
        break
    subject, preview = summarize("\n".join(lines))
    return (subject or name), preview


def cron_item(path: Path, profile: str, jobs: dict, prev_hash: str | None) -> dict:
    text = path.read_text(errors="replace")
    p = parse_cron_output(text)
    job_id = p["job_id"] or path.parent.name
    job = jobs.get(job_id, {})
    name = p["name"] or job.get("name") or job_id
    ts = local_ts(p["run_time"]) or local_ts(path.stem) or path.stat().st_mtime

    status, noise, attention = "ok", False, False
    body = p["response"] or ""
    if p["kind"] == "error":
        status, attention = "failed", True
        body = "**Run failed.**\n\n```\n" + (p["error"] or "").strip() + "\n```"
    elif p["kind"] == "skipped":
        status, noise = "skipped", True
    elif SILENT_RE.match(strip_md(body.lstrip().split("\n", 1)[0])):
        status, noise = "silent", True
    elif not body.strip():
        status, noise = "empty", True

    rhash = norm_hash(body)
    if status == "ok":
        if prev_hash and rhash == prev_hash:
            status, noise = "unchanged", True
        elif ATTENTION_RE.search(body[:ATTENTION_WINDOW]) or alert_emoji(body[:ATTENTION_WINDOW]):
            attention = True

    if status == "failed":
        last = [ln for ln in (p["error"] or "").splitlines() if ln.strip()]
        subject, preview = "Run failed", (last[-1] if last else "")[:240]
    elif status == "skipped":
        subject, preview = "Skipped: script gate found nothing to do", ""
    elif status == "silent":
        subject, preview = "Ran silently (nothing to report)", ""
    else:
        subject, preview = cron_subject(body, name)

    return {
        "source": "cron",
        "source_ref": f"{profile}:{path.parent.name}/{path.name}",
        "title": subject,
        "summary": preview,
        "body": body[:BODY_CAP],
        "status": status,
        "attention": attention,
        "noise": noise,
        "group_key": job_id,
        "group_label": name,
        "profile": profile,
        "ts": ts,
        "extra": {
            "job_id": job_id,
            "run_time": p["run_time"],
            "schedule": p["schedule"] or (job.get("schedule") or {}).get("display", ""),
            "mode": p["mode"],
            "kind": p["kind"],
            "deliver": job.get("deliver"),
            "script_output": (p["script_output"] or "")[:SCRIPT_CAP] or None,
            "source_path": str(path),
            "hash": rhash,
        },
    }


def iter_cron_files(home: Path):
    outdir = home / "cron" / "output"
    if not outdir.is_dir():
        return
    now = time.time()
    for jobdir in sorted(outdir.iterdir()):
        if not jobdir.is_dir():
            continue
        files = []
        for f in jobdir.glob("*.md"):
            try:
                if now - f.stat().st_mtime < SETTLE_SECONDS:
                    continue
            except OSError:
                continue
            files.append(f)
        yield jobdir.name, sorted(files)


# ── scheduled-job health ────────────────────────────────────────────────
# A cron run reaches the inbox only by writing an output file, so a job that stopped
# firing (dispatcher down, gateway wedged, job deleted from the queue) is invisible.
# jobs.json knows: next_run_at, last_run_at, failure_streak, last_error. These readers
# turn that state into one durable incident per job and problem.

STREAK_ALERT = 2              # two failures in a row is a pattern, not a blip
OVERDUE_GRACE = 3600          # a job an hour past its slot is not just slow
OVERDUE_GRACE_MIN = 15 * 60   # floor, so a 5-minute poller does not flap


def parse_iso(value) -> float | None:
    """Epoch seconds from a Hermes timestamp (ISO string, or {'at': ISO, ...})."""
    if isinstance(value, dict):
        value = value.get("at")
    if not isinstance(value, str) or not value:
        return None
    try:
        return datetime.fromisoformat(value.replace("Z", "+00:00")).timestamp()
    except ValueError:
        return None


def overdue_grace(job: dict) -> float:
    """How late a job may be before it counts as not running.

    Interval jobs get 3x their own period (never under 15 minutes), capped at an hour;
    cron and one-shot jobs get the full hour because their period is unknown here.
    """
    sched = job.get("schedule") or {}
    minutes = sched.get("minutes")
    if sched.get("kind") == "interval" and isinstance(minutes, (int, float)) and minutes > 0:
        return float(max(OVERDUE_GRACE_MIN, min(OVERDUE_GRACE, 3 * minutes * 60)))
    return float(OVERDUE_GRACE)


def _dur(seconds: float) -> str:
    if seconds < 5400:
        return f"{max(1, round(seconds / 60))} min"
    if seconds < 172800:
        return f"{round(seconds / 3600)} h"
    return f"{round(seconds / 86400)} d"


def _when(ts: float) -> str:
    return time.strftime("%a %d %b %H:%M", time.localtime(ts))


def scheduler_incidents(profile: str, jobs: dict, now: float | None = None) -> list[dict]:
    """One item per (job, problem). Re-indexing the same problem reuses its ref."""
    now = time.time() if now is None else now
    out: list[dict] = []
    for job_id, job in jobs.items():
        name = job.get("name") or job_id
        sched = job.get("schedule") or {}
        schedule = sched.get("display") or job.get("schedule_display") or ""
        last_run = parse_iso(job.get("last_run_at"))
        last_status = job.get("last_status") or ""
        streak = int(job.get("failure_streak") or 0)
        extra = {
            "job_id": job_id,
            "schedule": schedule,
            "deliver": job.get("deliver"),
            "last_run_at": job.get("last_run_at"),
            "last_status": last_status or None,
            "next_run_at": job.get("next_run_at"),
            "failure_streak": streak,
            "checked_at": now,
        }

        # Pausing sets enabled=False, so it must be recognised before the disabled check.
        if job.get("paused_at") or job.get("state") == "paused":
            reason = str(job.get("paused_reason") or "").strip()
            started = parse_iso(job.get("paused_at")) or now
            out.append({
                "source": "scheduler",
                "source_ref": f"{profile}:{job_id}:paused",
                "title": f"Paused: {reason[:140]}" if reason else "Paused with no reason",
                "summary": f"Since {_when(started)}"
                           + (f" · last run {_when(last_run)} ({last_status or 'unknown'})" if last_run
                              else " · never run"),
                "body": ("**This job is paused and will not run.**\n\n"
                         + (f"Reason: {reason}\n\n" if reason
                            else "No reason was recorded when it was paused.\n\n")
                         + f"Paused {_when(started)}.\n\nSchedule: `{schedule or 'unknown'}`.\n\n"
                         + (f"Last run: {_when(last_run)} ({last_status or 'unknown'}).\n\n" if last_run
                            else "This job has never run.\n\n")
                         + (f"It failed {streak} times before it was paused.\n\n" if streak else "")
                         + "Resume it with `hermes cron resume <job>` once the reason no longer "
                           "applies; this item clears itself when it runs again."),
                "status": "paused",
                "attention": bool(reason),
                "noise": False,
                "group_key": job_id,
                "group_label": name,
                "profile": profile,
                "ts": started,
                "extra": {**extra, "kind": "paused", "paused_at": job.get("paused_at"),
                          "paused_reason": reason or None},
            })
            continue  # a paused job cannot be late or failing

        if not job.get("enabled", True):
            continue  # a job nobody expects to run cannot be late or failing

        nxt = parse_iso(job.get("next_run_at"))
        if nxt and now - nxt > overdue_grace(job):
            late = now - nxt
            out.append({
                "source": "scheduler",
                "source_ref": f"{profile}:{job_id}:overdue",
                "title": f"Overdue by {_dur(late)}",
                "summary": f"Was due {_when(nxt)}"
                           + (f" · last run {_when(last_run)} ({last_status or 'unknown'})" if last_run else " · no run recorded"),
                "body": (f"**This job was due {_when(nxt)} and has not run for {_dur(late)}.**\n\n"
                         f"The scheduler still lists it as enabled, so either the dispatcher is not "
                         f"firing, the job is stuck behind another long run, or its delivery is "
                         f"failing before the run starts.\n\n"
                         + (f"Last run: {_when(last_run)} ({last_status or 'unknown'}).\n\n" if last_run
                            else "No run has ever been recorded for this job.\n\n")
                         + f"Schedule: `{schedule or 'unknown'}`."),
                "status": "overdue",
                "attention": True,
                "noise": False,
                "group_key": job_id,
                "group_label": name,
                "profile": profile,
                "ts": nxt,
                "extra": {**extra, "kind": "overdue", "late_by": round(late)},
            })

        if streak >= STREAK_ALERT:
            started = parse_iso(job.get("last_failure")) or last_run or now
            err = job.get("last_error") or ""
            if isinstance(job.get("last_failure"), dict) and not err:
                err = job["last_failure"].get("detail") or ""
            out.append({
                "source": "scheduler",
                "source_ref": f"{profile}:{job_id}:failing",
                "title": f"Failed {streak} runs in a row",
                "summary": (err.strip().splitlines() or [""])[0][:240],
                "body": (f"**This job has failed {streak} consecutive runs.**\n\n"
                         f"Failing since {_when(started)}"
                         + (f"; last run {_when(last_run)}." if last_run else ".")
                         + f"\n\nSchedule: `{schedule or 'unknown'}`.\n\n"
                         + (f"Last error:\n\n```\n{err.strip()[:4000]}\n```" if err else "No error message was recorded.")
                         ),
                "status": "failing",
                "attention": True,
                "noise": False,
                "group_key": job_id,
                "group_label": name,
                "profile": profile,
                "ts": started,
                "extra": {**extra, "kind": "failing", "last_error": err[:4000] or None},
            })
    return out


# ── delegations ─────────────────────────────────────────────────────────
# A delegation that finished but whose result never reached its conversation is lost
# work: the child ran, its answer is sitting in state.db, and nobody saw it.

# Still might land on its own, so they are not "finished" yet.
LIVE_DELEGATION_STATES = ("running", "finalizing", "stalling")
# Give the normal delivery path a few minutes before calling a pending result lost.
DELIVERY_GRACE = 600


def _json_list(raw, key: str) -> list:
    try:
        data = json.loads(raw or "{}")
    except ValueError:
        return []
    value = data.get(key) if isinstance(data, dict) else None
    return value if isinstance(value, list) else []


def _goals(task_json) -> str:
    try:
        task = json.loads(task_json or "{}")
    except ValueError:
        return ""
    if not isinstance(task, dict):
        return ""
    goals = task.get("goals")
    if isinstance(goals, list) and goals:
        return "\n".join(f"- {g}" for g in goals if g)[:2000]
    return str(task.get("goal") or "")[:2000]


def _summaries(result_json, limit: int = 3) -> list[str]:
    out = []
    for r in _json_list(result_json, "results")[:limit]:
        if isinstance(r, dict) and r.get("summary"):
            out.append(str(r["summary"]))
    return out


def _transcripts(result_json) -> list[str]:
    try:
        data = json.loads(result_json or "{}")
    except ValueError:
        return []
    paths = data.get("live_transcripts") if isinstance(data, dict) else None
    return [str(p) for p in paths][:5] if isinstance(paths, list) else []


def delegation_incidents(profile: str, home: Path, now: float | None = None) -> list[dict] | None:
    """Undelivered delegation results. None when state.db is absent.

    Raises sqlite3.Error when it exists but cannot be read, so the caller can
    distinguish "nothing to report" from "could not look".
    """
    db = home / "state.db"
    if not db.exists():
        return None
    now = time.time() if now is None else now
    live = ",".join("?" for _ in LIVE_DELEGATION_STATES)
    conn = ro_connect(db)
    try:
        try:
            rows = conn.execute(
                f"""SELECT delegation_id, state, dispatched_at, completed_at, updated_at,
                           delivery_state, delivery_attempts, task_json, result_json, origin_session
                    FROM async_delegations
                    WHERE state NOT IN ({live})
                      AND delivered_at IS NULL
                      AND COALESCE(delivery_state, 'pending') != 'delivered'
                    ORDER BY COALESCE(completed_at, dispatched_at) DESC""",
                LIVE_DELEGATION_STATES,
            ).fetchall()
        except sqlite3.OperationalError as exc:
            if "no such table" in str(exc).lower():
                return None  # delegation was never used on this profile
            raise
        session_ids = sorted({r["origin_session"] for r in rows if r["origin_session"]})
        titles: dict[str, str] = {}
        if session_ids:
            try:
                found = conn.execute(
                    f"SELECT id, title FROM sessions WHERE id IN ({','.join('?' * len(session_ids))})",
                    session_ids).fetchall()
                titles = {r["id"]: r["title"] for r in found if r["title"]}
            except sqlite3.Error:
                pass  # titles are decoration; the item stands without one
    finally:
        conn.close()

    out: list[dict] = []
    for r in rows:
        dropped = r["delivery_state"] == "dropped"
        finished = r["completed_at"] or r["updated_at"] or r["dispatched_at"]
        if not dropped and finished and now - finished < DELIVERY_GRACE:
            continue  # the normal delivery path is probably still working
        attempts = int(r["delivery_attempts"] or 0)
        goal = _goals(r["task_json"])
        summaries = _summaries(r["result_json"])
        session_title = titles.get(r["origin_session"] or "")
        extra = {
            "delegation_id": r["delegation_id"],
            "state": r["state"],
            "delivery_state": r["delivery_state"],
            "attempts": attempts,
            "dispatched_at": r["dispatched_at"],
            "completed_at": r["completed_at"],
            "origin_session": r["origin_session"],
            "goal": goal[:2000] or None,
            "transcripts": _transcripts(r["result_json"]),
        }
        body = ""
        if goal:
            body += f"**Goal**\n\n{goal}\n\n"
        if summaries:
            body += "**What came back**\n\n" + "\n\n".join(
                f"> {s.strip()[:3000]}" for s in summaries) + "\n\n"
        body += ("The child finished and its output is still in `state.db`, but the result never "
                 "reached the conversation it was requested from. Re-run the work only if you still "
                 "need it; the summary above is usually enough.")
        out.append({
            "source": "delegation",
            "source_ref": f"{profile}:{r['delegation_id']}",
            "title": (f"Result dropped after {attempts} delivery attempt{'s' if attempts != 1 else ''}"
                      if dropped else "Result not delivered"),
            "summary": (goal.splitlines()[0].lstrip("- ")[:240] if goal else f"Delegation {r['delegation_id']}"),
            "body": body[:BODY_CAP],
            "status": "undelivered",
            "attention": True,
            "noise": False,
            "group_key": r["delegation_id"],
            "group_label": session_title or "Delegations",
            "profile": profile,
            "ts": finished or now,
            "extra": {**extra, "kind": "dropped" if dropped else "pending"},
        })
    return out


# ── background processes ────────────────────────────────────────────────
# processes.json is a checkpoint of processes that had not exited when it was written.
# A crashed owner stops rewriting it, which strands entries whose PID is long gone.

ORPHAN_GRACE = 120  # a process that just exited is usually rewritten out of the checkpoint


def _proc_start_ticks(pid: int) -> int | None:
    """Kernel start time for a PID (clock ticks since boot), Linux only."""
    try:
        fields = Path(f"/proc/{pid}/stat").read_text().rsplit(")", 1)[1].split()
        return int(fields[19])  # starttime is field 22 of proc(5), state is field 3
    except (OSError, IndexError, ValueError):
        return None


def host_pid_state(pid, expected_start=None) -> str:
    """'running' | 'gone' | 'reused' | 'unknown' for a host PID.

    Mirrors Hermes' own recovery rule: a start-time probe that cannot be read is not
    proof of reuse, so it counts as running. Never invent an exit.
    """
    if os.name == "nt":
        return "unknown"  # os.kill(pid, 0) is not a liveness probe on Windows
    try:
        pid = int(pid)
    except (TypeError, ValueError):
        return "unknown"
    if pid <= 0:
        return "gone"
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return "gone"
    except PermissionError:
        pass  # alive, owned by another user
    except OSError:
        return "unknown"
    if not isinstance(expected_start, int) or isinstance(expected_start, bool):
        return "running"
    actual = _proc_start_ticks(pid)
    if actual is None:
        return "running"
    return "running" if actual == expected_start else "reused"


def read_processes(home: Path) -> tuple[list[dict], str]:
    """Registry entries plus how the file read went: read / absent / unreadable (why)."""
    path = home / "processes.json"
    if not path.exists():
        return [], "absent"
    try:
        data = json.loads(path.read_text(encoding="utf-8-sig"))
    except (OSError, ValueError) as exc:
        return [], f"unreadable ({type(exc).__name__})"
    if not isinstance(data, list):
        return [], "unreadable (not a list)"
    return [e for e in data if isinstance(e, dict)], "read"


def process_incidents(profile: str, entries: list[dict], now: float | None = None) -> list[dict]:
    """Checkpoint entries whose process is gone. One item per stranded entry."""
    now = time.time() if now is None else now
    out: list[dict] = []
    for e in entries:
        if e.get("pid_scope", "host") != "host":
            continue  # an in-sandbox PID means nothing out here
        pid = e.get("pid")
        if not pid:
            continue
        started = e.get("started_at")
        if isinstance(started, (int, float)) and now - started < ORPHAN_GRACE:
            continue
        state = host_pid_state(pid, e.get("host_start_time"))
        if state not in ("gone", "reused"):
            continue
        output_log = e.get("output_log")
        if isinstance(output_log, str) and output_log:
            try:
                if Path(output_log).with_suffix(".exit").exists():
                    continue  # the child recorded its exit; the entry is a handoff, not an orphan
            except OSError:
                pass
        command = " ".join(str(e.get("command") or "").split())
        key = e.get("session_id") or f"pid-{pid}"
        extra = {
            "process_id": key,
            "pid": pid,
            "pid_state": state,
            "started_at": started,
            "cwd": e.get("cwd"),
            "output_log": output_log,
            "notify_on_complete": bool(e.get("notify_on_complete")),
            "checked_at": now,
        }
        when = (f"It was started {_when(started)} (PID {pid})" if isinstance(started, (int, float))
                else f"It was recorded with PID {pid}")
        ending = ("; that number now belongs to a different process." if state == "reused"
                  else " and it no longer exists.")
        body = ("**This background process is listed in the registry, but its PID is gone.**\n\n"
                + (f"```\n{command[:2000]}\n```\n\n" if command else "")
                + when + ending
                + "\n\nThe checkpoint listing it was written before it died and has not been "
                  "rewritten since, so nothing is going to report on it. If it was doing work "
                  "you still need, that work did not finish.\n\n"
                + (f"Output log: `{output_log}`" if isinstance(output_log, str) and output_log else ""))
        out.append({
            "source": "process",
            "source_ref": f"{profile}:{key}",
            "title": (f"Process gone: {command[:80]}" if command else f"Process gone (pid {pid})"),
            "summary": (f"PID {pid}"
                        + (f" · started {_when(started)} ({_dur(now - started)} ago)" if isinstance(started, (int, float)) else "")),
            "body": body,
            "status": "orphaned",
            "attention": True,
            "noise": False,
            "group_key": key,
            "group_label": "Background processes",
            "profile": profile,
            "ts": started if isinstance(started, (int, float)) else now,
            "extra": extra,
        })
    return out


# ── chat artifacts ───────────────────────────────────────────────────────

def extract_paths(content: str, cwd: str | None) -> list[str]:
    found = []
    for m in MEDIA_RE.finditer(content):
        found.append(m.group(1))
    for m in PREVIEW_RE.finditer(content):
        found.append(m.group(1))
    out = []
    for raw in found:
        p = raw.strip().rstrip(TRAILING)
        if not p or p.startswith(("http://", "https://", "data:")):
            continue
        p = os.path.expanduser(p)
        if not os.path.isabs(p):
            if not cwd:
                continue
            p = os.path.join(cwd, p)
        p = os.path.realpath(p)
        if p not in out:
            out.append(p)
    return out


def message_context(content: str) -> str:
    """The assistant's message with the MEDIA directives removed."""
    text = MEDIA_RE.sub("", content)
    text = PREVIEW_RE.sub("", text)
    return re.sub(r"\n{3,}", "\n\n", text).strip()[:20_000]


VOLATILE_MARKERS = ("/cache/scratch/", "/tmp/", "/browser-harness/", "/.cache/")


def is_volatile(path: str) -> bool:
    """Scratch/tmp files get overwritten by unrelated work (e.g. every browser screenshot
    reuses shot.png), so an mtime change there is not a real edit worth resurfacing."""
    return path.startswith("/tmp/") or any(m in path for m in VOLATILE_MARKERS)


def file_facts(path: str) -> dict:
    mime = mimetypes.guess_type(path)[0] or ""
    if not mime:
        ext = Path(path).suffix.lower()
        mime = {".md": "text/markdown", ".log": "text/plain", ".jsonl": "text/plain",
                ".yaml": "text/plain", ".yml": "text/plain", ".toml": "text/plain",
                ".kt": "text/plain", ".ts": "text/plain", ".tsx": "text/plain",
                ".rs": "text/plain", ".go": "text/plain"}.get(ext, "application/octet-stream")
    try:
        st = os.stat(path)
        return {"mime": mime, "size": st.st_size, "mtime": st.st_mtime, "exists": True}
    except OSError:
        return {"mime": mime, "size": None, "mtime": None, "exists": False}


def iter_artifact_mentions(home: Path, after_id: int):
    """Yield (message_id, artifact dict) for MEDIA/preview mentions after a message id."""
    db = home / "state.db"
    if not db.exists():
        return
    conn = ro_connect(db)
    try:
        rows = conn.execute(
            """SELECT m.id, m.session_id, m.content, m.timestamp,
                      s.title, s.source, s.cwd, s.display_name
               FROM messages m JOIN sessions s ON s.id = m.session_id
               WHERE m.id > ? AND m.role = 'assistant'
                 AND (m.content LIKE '%MEDIA:%' OR m.content LIKE '%::preview{%')
               ORDER BY m.id""",
            (after_id,),
        ).fetchall()
    finally:
        conn.close()
    for r in rows:
        content = r["content"] or ""
        for path in extract_paths(content, r["cwd"]):
            yield r["id"], {
                "path": path,
                "session_id": r["session_id"],
                "session_title": r["title"] or r["display_name"] or "Untitled chat",
                "platform": r["source"],
                "ts": r["timestamp"],
                "context": message_context(content),
            }


def max_message_id(home: Path) -> int:
    db = home / "state.db"
    if not db.exists():
        return 0
    conn = ro_connect(db)
    try:
        return conn.execute("SELECT COALESCE(MAX(id), 0) FROM messages").fetchone()[0]
    finally:
        conn.close()


def artifact_item(mention: dict, profile: str, existing_extra: dict | None) -> tuple[dict, bool]:
    """Build an artifact item; returns (item, reopen)."""
    path = mention["path"]
    facts = file_facts(path)
    sessions = list((existing_extra or {}).get("sessions", []))
    ref = {k: mention[k] for k in ("session_id", "session_title", "platform", "ts")}
    sessions = [s for s in sessions if s.get("session_id") != ref["session_id"]] + [ref]
    sessions.sort(key=lambda s: s.get("ts") or 0)
    prev_mtime = (existing_extra or {}).get("mtime")
    reopen = (bool(existing_extra) and facts["mtime"] and prev_mtime and facts["mtime"] > prev_mtime + 1
              and not is_volatile(path))
    latest = sessions[-1]
    name = os.path.basename(path)
    subject, _ = summarize(mention["context"])
    return {
        "source": "artifact",
        "source_ref": path,
        "title": name,
        "summary": subject[:240],
        "body": mention["context"],
        "status": "missing" if not facts["exists"] else "file",
        "attention": False,
        "noise": False,
        "group_key": latest["session_id"],
        "group_label": latest["session_title"],
        "profile": profile,
        "ts": max(mention["ts"], *(s.get("ts") or 0 for s in sessions)),
        "extra": {"path": path, **facts, "sessions": sessions[-25:], "origin": "chat"},
    }, bool(reopen)


# ── kanban ───────────────────────────────────────────────────────────────

def board_name(board_dir: Path) -> str:
    try:
        return json.loads((board_dir / "board.json").read_text()).get("name") or board_dir.name
    except (OSError, ValueError):
        return board_dir.name


def kanban_dbs(home: Path):
    root = home / "kanban.db"
    if root.exists():
        yield "default", root, "default"
    boards = home / "kanban" / "boards"
    if boards.is_dir():
        for b in sorted(boards.iterdir()):
            db = b / "kanban.db"
            if db.exists():
                yield b.name, db, board_name(b)


def iter_kanban_runs(board: str, db: Path, after_id: int, label: str | None = None):
    conn = ro_connect(db)
    try:
        rows = conn.execute(
            """SELECT r.id, r.task_id, r.profile, r.status, r.outcome, r.summary,
                      r.metadata, r.error, r.started_at, r.ended_at,
                      t.title, t.status AS task_status, t.body, t.assignee
               FROM task_runs r JOIN tasks t ON t.id = r.task_id
               WHERE r.id > ? AND r.ended_at IS NOT NULL ORDER BY r.id""",
            (after_id,),
        ).fetchall()
        atts = {}
        for a in conn.execute("SELECT task_id, filename, stored_path, content_type, created_at FROM task_attachments"):
            atts.setdefault(a["task_id"], []).append(dict(a))
    finally:
        conn.close()
    for r in rows:
        outcome = r["outcome"] or r["status"] or "ended"
        try:
            meta = json.loads(r["metadata"] or "{}")
        except ValueError:
            meta = {}
        if not isinstance(meta, dict):
            meta = {}
        parts = []
        if r["summary"]:
            parts.append(r["summary"])
        if r["error"]:
            parts.append("**Error**\n\n```\n" + r["error"][:8000] + "\n```")
        facts = {k: v for k, v in meta.items() if k not in ("worker_output",)}
        if facts:
            parts.append("**Run metadata**\n\n```json\n" + json.dumps(facts, indent=2)[:8000] + "\n```")
        verb = {"completed": "completed", "blocked": "blocked", "review_requested": "ready for review",
                "crashed": "crashed", "timed_out": "timed out", "gave_up": "gave up"}.get(outcome, outcome)
        artifacts = [a for a in meta.get("artifacts", []) if isinstance(a, str)] if isinstance(meta.get("artifacts"), list) else []
        yield r["id"], {
            "source": "kanban",
            "source_ref": f"{board}:{r['id']}",
            "title": f"{r['title']}",
            "summary": f"{r['profile'] or r['assignee'] or 'worker'} {verb}" + (
                f": {strip_md(r['summary'])[:200]}" if r["summary"] else ""),
            "body": "\n\n".join(parts) or f"Run {verb}.",
            "status": outcome,
            "attention": outcome in ATTENTION_OUTCOMES,
            "noise": outcome in NOISE_OUTCOMES,
            "group_key": board,
            "group_label": label or board,
            "ts": float(r["ended_at"]),
            "extra": {"board": board, "task_id": r["task_id"], "run_id": r["id"],
                      "profile": r["profile"], "task_status": r["task_status"],
                      "artifacts": artifacts},
        }, artifacts, atts.get(r["task_id"], [])
