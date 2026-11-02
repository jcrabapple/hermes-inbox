"""hermes-inbox: one inbox for everything your Hermes agent does.

  hermes-inbox [--hermes-home ~/.hermes] serve [--host 127.0.0.1] [--port 8941]
  hermes-inbox index                       one-shot index pass, prints new counts
  hermes-inbox post TITLE [-b BODY | -]    drop a note into the inbox (stdin with -)
        [--level info|attention] [--group NAME] [--link URL]
  hermes-inbox stats
  hermes-inbox retitle                     recompute cron subjects after changing the subject rules
  hermes-inbox token [--show]              where the login token lives (creates it if missing)
  hermes-inbox usage [--days 30]           model and cost per scheduled job
  hermes-inbox prune [--dry-run] [--quiet-days N] [--keep-days N]
                                           apply the retention policy now
  hermes-inbox export [--format json|csv] [--view all] [--source S] [--group JOB] [--since 30d] [-o FILE]
  hermes-inbox doctor                     check sources, database and config; exit 1 on failure

Environment: HERMES_HOME (default ~/.hermes), HERMES_INBOX_DATA (default
$XDG_DATA_HOME/hermes-inbox), HERMES_INBOX_CONFIG (default $XDG_CONFIG_HOME/hermes-inbox),
HERMES_INBOX_TOKEN (overrides the token file), HERMES_INBOX_HOST, HERMES_INBOX_PORT,
HERMES_INBOX_INTERVAL (seconds between index passes, default 60), HERMES_INBOX_KEEP_QUIET_DAYS
(delete quiet runs older than this, default 30, 0 = never), HERMES_INBOX_KEEP_DAYS (delete
every unstarred item older than this, default 0 = never).
"""
from __future__ import annotations

import argparse
import json
import logging
import os
import re
import secrets
import sys
import time

from . import __version__, export, indexer, retention, sources, store


def main(argv=None):
    ap = argparse.ArgumentParser(prog="hermes-inbox", description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--version", action="version", version=f"hermes-inbox {__version__}")
    ap.add_argument("--hermes-home", help="Hermes home to index (default: $HERMES_HOME or ~/.hermes)")
    sub = ap.add_subparsers(dest="cmd", required=True)
    s = sub.add_parser("serve")
    s.add_argument("--host", default=os.environ.get("HERMES_INBOX_HOST", "127.0.0.1"))
    s.add_argument("--port", type=int, default=int(os.environ.get("HERMES_INBOX_PORT", "8941")))
    sub.add_parser("index")
    sub.add_parser("stats")
    sub.add_parser("retitle", help="recompute cron subjects from stored bodies")
    p = sub.add_parser("post")
    p.add_argument("title")
    p.add_argument("body", nargs="?", help="markdown body, or - to read stdin")
    p.add_argument("-b", "--body-text", dest="body_text")
    p.add_argument("--level", default="info", choices=["info", "attention", "warning", "error"])
    p.add_argument("--group", default="Notes")
    p.add_argument("--link")
    u = sub.add_parser("usage")
    u.add_argument("--days", type=int, default=30, help="window in days (0 = all time)")
    pr = sub.add_parser("prune")
    pr.add_argument("--dry-run", action="store_true")
    pr.add_argument("--quiet-days", type=int)
    pr.add_argument("--keep-days", type=int)
    ex = sub.add_parser("export")
    ex.add_argument("--format", choices=["json", "csv"], default="json")
    ex.add_argument("--view", default="all", choices=["inbox", "unread", "attention", "starred", "all", "archive"])
    ex.add_argument("--source", choices=["cron", "artifact", "kanban", "note"])
    ex.add_argument("--group", help="job id or board slug")
    ex.add_argument("--since", help="only items newer than this, e.g. 7d, 12h")
    ex.add_argument("--body", action="store_true", help="include full bodies in CSV")
    ex.add_argument("-o", "--output", help="file to write (default stdout)")
    sub.add_parser("doctor", help="check sources, database and config")
    t = sub.add_parser("token")
    t.add_argument("--show", action="store_true", help="print the token itself")
    args = ap.parse_args(argv)
    if args.hermes_home:
        os.environ["HERMES_HOME"] = os.path.expanduser(args.hermes_home)
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")

    if args.cmd == "serve":
        from . import server
        server.serve(args.host, args.port)
    elif args.cmd == "index":
        conn = store.connect()
        idx = indexer.Indexer(conn)
        print(json.dumps(idx.run()))
        idx.refresh_artifact_facts()
    elif args.cmd == "retitle":
        print(json.dumps({"retitled": indexer.Indexer(store.connect()).retitle_cron()}))
    elif args.cmd == "stats":
        conn = store.connect()
        for r in conn.execute("SELECT source, COUNT(*) n, SUM(read=0 AND noise=0 AND archived=0) unread, "
                              "SUM(attention) attention FROM items GROUP BY source"):
            print(f"{r['source']:9} {r['n']:6} items  {r['unread'] or 0:5} unread  {r['attention'] or 0:4} attention")
        print("last index:", store.get_meta(conn, "last_index"))
    elif args.cmd == "post":
        body = args.body_text or args.body or ""
        if body == "-":
            body = sys.stdin.read()
        conn = store.connect()
        now = time.time()
        iid = store.insert_item(conn, {
            "source": "note", "source_ref": f"{now:.6f}-{secrets.token_hex(4)}",
            "title": args.title[:300], "summary": sources.summarize(body)[0][:240], "body": body,
            "status": args.level, "attention": args.level != "info",
            "group_key": re.sub(r"[^a-z0-9]+", "-", args.group.lower()).strip("-") or "notes",
            "group_label": args.group, "ts": now,
            "extra": {"level": args.level, "link": args.link},
        })
        conn.commit()
        print(json.dumps({"ok": True, "id": iid}))
    elif args.cmd == "usage":
        print_usage(store.connect(), args.days)
    elif args.cmd == "prune":
        res = retention.prune(store.connect(), quiet_days=args.quiet_days, keep_days=args.keep_days,
                              dry_run=args.dry_run)
        print(json.dumps(res))
    elif args.cmd == "export":
        from .server import build_filter
        params = {"view": args.view}
        if args.source:
            params["source"] = args.source
        if args.group:
            params["group"] = args.group
        where, qargs = build_filter(params)
        if args.since:
            where += " AND ts >= ?"
            qargs.append(time.time() - parse_age(args.since))
        out = open(args.output, "w", encoding="utf-8", newline="") if args.output else sys.stdout
        try:
            rows = export.rows(store.connect(), where, qargs)
            n = export.write_json(rows, out) if args.format == "json" else \
                export.write_csv(rows, out, include_body=args.body)
        finally:
            if args.output:
                out.close()
        print(f"exported {n} items", file=sys.stderr)
    elif args.cmd == "token":
        from . import server
        tok = server.load_token()
        print(tok if args.show else (os.environ.get("HERMES_INBOX_TOKEN") and "set via HERMES_INBOX_TOKEN"
                                     or str(server.config_dir() / "token")))
    elif args.cmd == "doctor":
        sys.exit(doctor())


def doctor() -> int:
    """Check every source, the inbox database and the configuration; print a report."""
    import sqlite3 as sq

    def out(level: str, name: str, detail: str = "") -> None:
        print(f"{level:5} {name}{(' — ' + detail) if detail else ''}")

    fails = 0
    root = indexer.hermes_root()
    if not root.is_dir():
        out("fail", "hermes home", f"{root} does not exist")
        return 1
    out("ok", "hermes home", str(root))

    profiles = list(sources.hermes_homes(root))
    total_jobs = 0
    for profile, home in profiles:
        jobs = sources.load_jobs(home)
        if not jobs and not (home / "cron" / "jobs.json").exists():
            out("warn", f"[{profile}] cron", "no jobs.json — nothing scheduled here")
        else:
            total_jobs += len(jobs)
            out("ok", f"[{profile}] cron", f"{len(jobs)} jobs")
        outdir = home / "cron" / "output"
        runs = sum(1 for _, files in sources.iter_cron_files(home) for _ in files)
        out("ok" if outdir.is_dir() else "warn", f"[{profile}] cron output",
            f"{runs} run files indexed-ready" if outdir.is_dir() else "no output dir")
        db = home / "state.db"
        if db.exists():
            try:
                conn = sources.ro_connect(db)
                n = conn.execute("SELECT COUNT(*) FROM sessions").fetchone()[0]
                conn.close()
                out("ok", f"[{profile}] state.db", f"{n} sessions (read-only)")
            except sq.Error as exc:
                out("fail", f"[{profile}] state.db", f"cannot read: {exc}")
                fails += 1
        else:
            out("warn", f"[{profile}] state.db", "missing — no chat artifacts or costs from here")
    out("info", "profiles", ", ".join(p for p, _ in profiles))

    db_path = store.default_db_path()
    if not db_path.exists():
        out("fail", "inbox database", f"{db_path} missing — run: hermes-inbox index")
        return 1
    try:
        conn = sq.connect(f"file:{db_path}?mode=ro", uri=True)
        conn.row_factory = sq.Row
        n = conn.execute("SELECT COUNT(*) FROM items").fetchone()[0]
        per = ", ".join(f"{r['source']} {r['c']}" for r in
                        conn.execute("SELECT source, COUNT(*) c FROM items GROUP BY source"))
        integrity = conn.execute("PRAGMA integrity_check").fetchone()[0]
        conn.close()
        out("ok" if integrity == "ok" else "fail", "inbox database",
            f"{db_path}, {n} items ({per}), integrity {integrity}")
        if integrity != "ok":
            fails += 1
    except sq.Error as exc:
        out("fail", "inbox database", f"cannot open: {exc}")
        return 1

    from .server import config_dir
    tok = config_dir() / "token"
    if tok.exists():
        mode = oct(tok.stat().st_mode & 0o777)
        out("ok" if mode == "0o600" else "warn", "token file",
            f"{tok} (mode {mode})" + ("" if mode == "0o600" else " — should be 0600"))
    elif os.environ.get("HERMES_INBOX_TOKEN"):
        out("ok", "token file", "set via HERMES_INBOX_TOKEN")
    else:
        out("warn", "token file", "will be created on first serve")

    for name in ("HERMES_INBOX_KEEP_QUIET_DAYS", "HERMES_INBOX_KEEP_DAYS", "HERMES_INBOX_INTERVAL"):
        v = os.environ.get(name)
        if v is not None and not v.lstrip("-").isdigit():
            out("fail", name, f"{v!r} is not an integer")
            fails += 1
    conn = sq.connect(f"file:{db_path}?mode=ro", uri=True)
    conn.row_factory = sq.Row
    last = store.get_meta(conn, "last_index")
    conn.close()
    if last:
        age = time.time() - last["at"]
        state = "ok" if age < 3 * 60 else "warn"
        out(state, "last index pass", f"{age:.0f}s ago, took {last.get('took')}s, "
                                     f"new: {last.get('new')}, priced: {last.get('priced')}")
    else:
        out("warn", "last index pass", "never ran — run: hermes-inbox index")
    out("info", "summary", f"{total_jobs} cron jobs across {len(profiles)} homes; "
                           f"{fails} failure{'' if fails == 1 else 's'}")
    return 1 if fails else 0


def parse_age(text: str) -> float:
    m = re.fullmatch(r"\s*(\d+(?:\.\d+)?)\s*([smhdw]?)\s*", text)
    if not m:
        raise SystemExit(f"bad --since value {text!r}; use e.g. 30d, 12h, 2w")
    return float(m.group(1)) * {"s": 1, "m": 60, "h": 3600, "d": 86400, "w": 604800, "": 86400}[m.group(2)]


def print_usage(conn, days: int):
    since = time.time() - days * 86400 if days else 0
    rows = conn.execute(
        """SELECT group_label, profile, COUNT(*) runs, COUNT(cost_usd) priced, COALESCE(SUM(cost_usd), 0) cost,
                  GROUP_CONCAT(DISTINCT model) models
           FROM items WHERE source='cron' AND json_extract(extra, '$.kind') IN ('agent', 'error') AND ts >= ?
           GROUP BY group_key, profile ORDER BY cost DESC""", (since,)).fetchall()
    total = sum(r["cost"] for r in rows)
    print(f"{'job':40} {'runs':>5} {'priced':>6} {'cost':>10}  models")
    for r in rows:
        name = r["group_label"] + (f" ({r['profile']})" if r["profile"] != "default" else "")
        models = ", ".join(m for m in (r["models"] or "").split(",") if m)
        print(f"{name[:40]:40} {r['runs']:5} {r['priced']:6} {r['cost']:10.4f}  {models}")
    window = f"last {days} days" if days else "all time"
    print(f"{'total (' + window + ')':40} {sum(r['runs'] for r in rows):5} {sum(r['priced'] for r in rows):6} "
          f"{total:10.4f}")


if __name__ == "__main__":
    main()
