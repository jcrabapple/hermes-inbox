"""Model and cost for cron runs, read from Hermes' session records.

Hermes titles each finished cron session "<job name> · <Mon DD HH:MM>" and records token
counts and estimated (sometimes actual) cost on the session, with a per-model breakdown
in session_model_usage that includes auxiliary calls (compression, vision, approvals).
Cron sessions are pruned after a few days, so usage has to be captured while it exists.
"""
from __future__ import annotations

import sqlite3

TITLE_SEP = " · "
MATCH_WINDOW = 15 * 60       # a run's output is written within seconds of its session closing
LONGEST_RUN = 6 * 3600       # how far before the output a session may have started
LINEAGE_LIMIT = 20           # compression continuations to walk back through


def _columns(conn: sqlite3.Connection, table: str) -> set[str]:
    return {r[1] for r in conn.execute(f"PRAGMA table_info({table})")}


def _like_escape(s: str) -> str:
    return s.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")


def cron_title_base(job_name: str, job_id: str) -> str:
    """Mirror of Hermes' own cron session title prefix (whitespace collapsed, 60 chars)."""
    return " ".join((job_name or "").split())[:60].strip() or f"cron {job_id}"


def find_cron_session(conn: sqlite3.Connection, job_name: str, job_id: str, run_ts: float,
                      taken: set[str]) -> str | None:
    """The session id of the cron run that wrote output at run_ts, or None."""
    cols = _columns(conn, "sessions")
    if not {"title", "started_at"} <= cols:
        return None  # an older or foreign schema without the fields needed to match runs
    end = f"COALESCE({', '.join(c for c in ('ended_at', 'last_activity_at', 'started_at') if c in cols)})"
    base = cron_title_base(job_name, job_id)
    rows = conn.execute(
        f"""SELECT id, {end} AS end_ts FROM sessions
            WHERE source = 'cron'
              AND (title LIKE ? ESCAPE '\\' OR title = ? OR title LIKE ? ESCAPE '\\')
              AND started_at BETWEEN ? AND ?""",
        (_like_escape(base + TITLE_SEP) + "%", f"cron {job_id}", _like_escape(f"cron {job_id} ") + "%",
         run_ts - LONGEST_RUN, run_ts + 120),
    ).fetchall()
    best, best_gap = None, MATCH_WINDOW + 1
    for r in rows:
        if r[0] in taken or r[1] is None:
            continue
        gap = abs(float(r[1]) - run_ts)
        if gap < best_gap:
            best, best_gap = r[0], gap
    return best


def _lineage(conn: sqlite3.Connection, session_id: str) -> list[str]:
    """The session plus the cron sessions it continued from after compression."""
    ids, cur = [session_id], session_id
    if "parent_session_id" not in _columns(conn, "sessions"):
        return ids
    for _ in range(LINEAGE_LIMIT):
        row = conn.execute("SELECT p.id FROM sessions s JOIN sessions p ON p.id = s.parent_session_id "
                           "WHERE s.id = ? AND p.source = 'cron'", (cur,)).fetchone()
        if not row or row[0] in ids:
            break
        ids.append(row[0])
        cur = row[0]
    return ids


def _pick_cost(actual, estimated):
    if actual:
        return float(actual), "actual"
    if estimated is not None:
        return float(estimated), "estimated"
    return None, "unknown"


def session_usage(conn: sqlite3.Connection, session_id: str) -> dict | None:
    """Model, tokens, cost and duration for a cron run, summed over its lineage."""
    ids = _lineage(conn, session_id)
    marks = ",".join("?" * len(ids))
    scols = _columns(conn, "sessions")
    want = [c for c in ("model", "billing_provider", "input_tokens", "output_tokens", "cache_read_tokens",
                        "cache_write_tokens", "reasoning_tokens", "estimated_cost_usd", "actual_cost_usd",
                        "api_call_count", "started_at", "ended_at") if c in scols]
    sess = [dict(zip(want, r)) for r in conn.execute(
        f"SELECT {', '.join(want)} FROM sessions WHERE id IN ({marks})", ids)]
    if not sess:
        return None

    models: list[dict] = []
    tables = {r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")}
    if "session_model_usage" in tables:
        for r in conn.execute(
                f"""SELECT model, billing_provider, task, SUM(api_call_count), SUM(input_tokens),
                           SUM(output_tokens), SUM(cache_read_tokens), SUM(cache_write_tokens),
                           SUM(reasoning_tokens), SUM(estimated_cost_usd), SUM(actual_cost_usd)
                    FROM session_model_usage WHERE session_id IN ({marks})
                    GROUP BY model, billing_provider, task""", ids):
            cost, status = _pick_cost(r[10], r[9])
            models.append({"model": r[0], "provider": r[1] or None, "task": r[2] or "main", "calls": r[3] or 0,
                           "input": r[4] or 0, "output": r[5] or 0, "cache_read": r[6] or 0,
                           "cache_write": r[7] or 0, "reasoning": r[8] or 0, "cost": cost, "cost_status": status})

    def total(key):
        return sum(int(s.get(key) or 0) for s in sess)

    if models:
        inp = sum(m["input"] for m in models)
        out = sum(m["output"] for m in models)
        cache_read = sum(m["cache_read"] for m in models)
        cache_write = sum(m["cache_write"] for m in models)
        reasoning = sum(m["reasoning"] for m in models)
        calls = sum(m["calls"] for m in models)
        priced = [m for m in models if m["cost"] is not None]
        cost = sum(m["cost"] for m in priced) if priced else None
        status = "actual" if priced and all(m["cost_status"] == "actual" for m in priced) else \
            ("estimated" if priced else "unknown")
        main = [m for m in models if m["task"] == "main"] or models
        model = max(main, key=lambda m: m["input"] + m["cache_read"] + m["output"])["model"]
        provider = max(main, key=lambda m: m["input"] + m["cache_read"] + m["output"])["provider"]
    else:
        inp, out = total("input_tokens"), total("output_tokens")
        cache_read, cache_write = total("cache_read_tokens"), total("cache_write_tokens")
        reasoning, calls = total("reasoning_tokens"), total("api_call_count")
        picked = [_pick_cost(s.get("actual_cost_usd"), s.get("estimated_cost_usd")) for s in sess]
        priced = [c for c in picked if c[0] is not None]
        cost = sum(c[0] for c in priced) if priced else None
        status = "actual" if priced and all(c[1] == "actual" for c in priced) else \
            ("estimated" if priced else "unknown")
        model = next((s.get("model") for s in sess if s.get("model")), None)
        provider = next((s.get("billing_provider") for s in sess if s.get("billing_provider")), None)

    starts = [s["started_at"] for s in sess if s.get("started_at")]
    ends = [s["ended_at"] for s in sess if s.get("ended_at")]
    duration = round(max(ends) - min(starts), 1) if starts and ends else None
    if not (inp or out or cache_read or calls) and cost is None:
        return None  # a session that never reached the model (script-gated or crashed early)
    return {
        "session_id": session_id, "sessions": len(ids), "model": model, "provider": provider,
        "cost_usd": round(cost, 6) if cost is not None else None, "cost_status": status,
        "input": inp, "cache_read": cache_read, "cache_write": cache_write, "output": out,
        "reasoning": reasoning, "calls": calls, "duration_s": duration,
        "models": [m for m in models if m["task"] != "main" or len(models) > 1],
    }
