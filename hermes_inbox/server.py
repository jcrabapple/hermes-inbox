"""HTTP server: JSON API + static single-page UI + artifact file serving."""
from __future__ import annotations

import hashlib
import hmac
import io
import json
import logging
import mimetypes
import os
import re
import secrets
import tempfile
import threading
import time
import urllib.parse
from http import HTTPStatus
from http.cookies import SimpleCookie
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

from . import export, retention, sources, store
from . import indexer as indexer_mod

log = logging.getLogger("hermes_inbox.server")

STATIC = Path(__file__).resolve().parent / "static"
COOKIE = "hermes_inbox"
INDEX_INTERVAL = int(os.environ.get("HERMES_INBOX_INTERVAL", "60"))
LIST_FIELDS = ("id", "source", "title", "summary", "status", "attention", "noise", "group_key",
               "group_label", "profile", "ts", "read", "starred", "archived", "model", "cost_usd")
PRUNE_INTERVAL = 3600
USAGE_RUNS = "source='cron' AND json_extract(extra, '$.kind') IN ('agent', 'error')"
PREVIEW_TEXT_LIMIT = 2_000_000
SANDBOXED_TYPES = ("text/html", "image/svg+xml", "application/xhtml+xml", "application/xml", "text/xml")
APP_CSP = ("default-src 'self'; img-src 'self' data: blob: https:; media-src 'self' blob:; "
           "style-src 'self' 'unsafe-inline'; script-src 'self'; frame-src 'self'; "
           "connect-src 'self'; base-uri 'none'; form-action 'self'; frame-ancestors 'self'")


# ── config / auth ────────────────────────────────────────────────────────

def config_dir() -> Path:
    if os.environ.get("HERMES_INBOX_CONFIG"):
        return Path(os.environ["HERMES_INBOX_CONFIG"])
    return Path(os.environ.get("XDG_CONFIG_HOME") or os.path.expanduser("~/.config")) / "hermes-inbox"


def load_token() -> str:
    env = os.environ.get("HERMES_INBOX_TOKEN")
    if env:
        return env.strip()
    path = config_dir() / "token"
    if not path.exists():
        path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        with os.fdopen(fd, "w") as fh:
            fh.write(secrets.token_urlsafe(32) + "\n")
    return path.read_text().strip()


def cookie_value(token: str) -> str:
    return hmac.new(token.encode(), b"hermes-inbox-session-v1", hashlib.sha256).hexdigest()


# ── query helpers ────────────────────────────────────────────────────────

FTYPE_SQL = {
    "image": "json_extract(extra,'$.mime') LIKE 'image/%'",
    "html": "json_extract(extra,'$.mime') IN ('text/html','application/xhtml+xml')",
    "doc": "(json_extract(extra,'$.mime') IN ('text/markdown','application/pdf','text/plain','text/csv',"
           "'application/json') OR json_extract(extra,'$.mime') LIKE 'application/vnd.openxmlformats%')",
    "media": "(json_extract(extra,'$.mime') LIKE 'audio/%' OR json_extract(extra,'$.mime') LIKE 'video/%')",
}

VIEW_SQL = {
    "inbox": "archived=0 AND noise=0",
    "unread": "archived=0 AND noise=0 AND read=0",
    # Unread alerts plus anything flagged in the last week; older read alerts drop off.
    "attention": "archived=0 AND attention=1 AND (read=0 OR ts > strftime('%s','now') - 604800)",
    "starred": "starred=1",
    "archive": "archived=1",
    "all": "1=1",
}


def fts_query(q: str) -> str:
    toks = re.findall(r"[\w][\w.\-@]*", q, re.UNICODE)
    if not toks:
        return ""
    parts = ['"' + t.replace('"', "") + '"' for t in toks]
    parts[-1] += "*"
    return " AND ".join(parts)


def build_filter(params: dict) -> tuple[str, list]:
    view = params.get("view", "inbox")
    where = [VIEW_SQL.get(view, VIEW_SQL["inbox"])]
    args: list = []
    if params.get("source"):
        where.append("source=?")
        args.append(params["source"])
    if params.get("group"):
        where.append("group_key=?")
        args.append(params["group"])
        # Inside one job, show every run including skipped/unchanged ones.
        where[0] = where[0].replace(" AND noise=0", "").replace("noise=0", "1=1")
    if params.get("profile"):
        where.append("profile=?")
        args.append(params["profile"])
    if params.get("ftype") in FTYPE_SQL:
        where.append(FTYPE_SQL[params["ftype"]])
    if params.get("noise") == "1":
        where[0] = where[0].replace(" AND noise=0", "").replace("noise=0", "1=1")
    if params.get("status"):
        where.append("status=?")
        args.append(params["status"])
    q = fts_query(params.get("q", ""))
    if q:
        where.append("id IN (SELECT rowid FROM items_fts WHERE items_fts MATCH ?)")
        args.append(q)
    return " AND ".join(where), args


def row_to_list(row) -> dict:
    d = {k: row[k] for k in LIST_FIELDS}
    extra = json.loads(row["extra"] or "{}")
    d["extra"] = {k: extra.get(k) for k in ("mime", "exists", "kind", "job_id", "origin", "size", "platform",
                                            "board", "level", "tags") if extra.get(k) is not None}
    return d


# ── request handler ──────────────────────────────────────────────────────

class App:
    def __init__(self, db_path: Path | None, root: Path | None):
        self.db_path = db_path
        self.root = root or indexer_mod.hermes_root()
        self.token = load_token()
        self.cookie = cookie_value(self.token)
        self._local = threading.local()
        self.index_lock = threading.Lock()
        store.connect(db_path).close()

    def db(self):
        conn = getattr(self._local, "conn", None)
        if conn is None:
            conn = store.connect(self.db_path)
            self._local.conn = conn
        return conn

    def reindex(self, refresh_files: bool = False) -> dict:
        with self.index_lock:
            conn = store.connect(self.db_path)
            try:
                idx = indexer_mod.Indexer(conn, self.root)
                counts = idx.run()
                if refresh_files:
                    idx.refresh_artifact_facts()
                return counts
            finally:
                conn.close()

    def prune(self, **kw) -> dict:
        with self.index_lock:
            conn = store.connect(self.db_path)
            try:
                return retention.prune(conn, **kw)
            finally:
                conn.close()

    def index_loop(self, stop: threading.Event):
        n, last_prune = 0, 0.0
        while not stop.is_set():
            try:
                self.reindex(refresh_files=(n % 10 == 0))
                if time.time() - last_prune >= PRUNE_INTERVAL:
                    res = self.prune()
                    last_prune = time.time()
                    if res["deleted"]:
                        log.info("retention removed %d items %s", res["deleted"], res["by_source"])
            except Exception:  # keep the loop alive; the next tick retries
                log.exception("index run failed")
            n += 1
            stop.wait(INDEX_INTERVAL)


def make_handler(app: App):
    class Handler(BaseHTTPRequestHandler):
        server_version = "HermesInbox/1.0"
        protocol_version = "HTTP/1.1"

        def log_message(self, fmt, *args):
            log.info("%s %s", self.headers.get("Cf-Connecting-Ip") or self.client_address[0], fmt % args)

        # ── plumbing ──
        def _send(self, status, body: bytes, ctype: str, headers: dict | None = None):
            self.send_response(status)
            self.send_header("Content-Type", ctype)
            self.send_header("Content-Length", str(len(body)))
            self.send_header("X-Content-Type-Options", "nosniff")
            self.send_header("Referrer-Policy", "no-referrer")
            self.send_header("Cache-Control", "no-store")
            for k, v in (headers or {}).items():
                self.send_header(k, v)
            self.end_headers()
            if self.command != "HEAD":
                self.wfile.write(body)

        def _json(self, obj, status=200):
            self._send(status, json.dumps(obj).encode(), "application/json; charset=utf-8")

        def _redirect(self, location, headers=None):
            self._send(303, b"", "text/plain", {"Location": location, **(headers or {})})

        def _body(self) -> bytes:
            n = int(self.headers.get("Content-Length") or 0)
            if n > 5_000_000:
                raise ValueError("body too large")
            return self.rfile.read(n) if n else b""

        def _secure(self) -> bool:
            return self.headers.get("X-Forwarded-Proto") == "https" or bool(self.headers.get("Cf-Visitor"))

        def _authed(self) -> bool:
            auth = self.headers.get("Authorization", "")
            if auth.startswith("Bearer ") and hmac.compare_digest(auth[7:].strip(), app.token):
                return True
            c = SimpleCookie(self.headers.get("Cookie", ""))
            val = c.get(COOKIE)
            return bool(val and hmac.compare_digest(val.value, app.cookie))

        def _parts(self):
            u = urllib.parse.urlsplit(self.path)
            params = {k: v[-1] for k, v in urllib.parse.parse_qs(u.query).items()}
            return u.path, params

        # ── routing ──
        def do_HEAD(self):
            self.do_GET()

        def do_GET(self):
            path, params = self._parts()
            if path == "/login":
                return self._static("login.html")
            if path.startswith("/static/"):
                return self._static(path[len("/static/"):])
            if path == "/healthz":
                return self._json({"ok": True})
            if not self._authed():
                if path.startswith("/api/"):
                    return self._json({"error": "unauthorized"}, 401)
                return self._redirect("/login")
            if path in ("/", "/index.html") or path.startswith("/item/") or path.startswith("/v/"):
                return self._static("index.html", csp=True)
            if path == "/api/summary":
                return self._json(self.summary())
            if path == "/api/items":
                return self._json(self.list_items(params))
            m = re.fullmatch(r"/api/items/(\d+)", path)
            if m:
                return self.get_item(int(m.group(1)))
            m = re.fullmatch(r"/api/items/(\d+)/file", path)
            if m:
                return self.serve_file(int(m.group(1)), params)
            if path == "/api/jobs":
                return self._json(self.jobs())
            if path == "/api/usage":
                return self._json(self.usage_report(params))
            if path == "/api/export":
                return self.export(params)
            return self._json({"error": "not found"}, 404)

        def do_POST(self):
            path, params = self._parts()
            if path == "/login":
                return self.login()
            if path == "/logout":
                return self._redirect("/login", {"Set-Cookie": f"{COOKIE}=; Path=/; Max-Age=0; HttpOnly; SameSite=Lax"})
            if not self._authed():
                return self._json({"error": "unauthorized"}, 401)
            # Cookie-authed writes must be JSON: a cross-site form can't send that without a preflight.
            if not self.headers.get("Content-Type", "").startswith("application/json"):
                return self._json({"error": "json body required"}, 415)
            try:
                data = json.loads(self._body() or b"{}")
            except ValueError:
                return self._json({"error": "bad json"}, 400)
            m = re.fullmatch(r"/api/items/(\d+)", path)
            if m:
                return self.update_item(int(m.group(1)), data)
            if path == "/api/bulk":
                return self.bulk(data)
            if path == "/api/notes":
                return self.add_note(data)
            if path == "/api/reindex":
                return self._json({"new": app.reindex(refresh_files=True)})
            if path == "/api/prune":
                return self._json(app.prune(dry_run=bool(data.get("dry_run"))))
            return self._json({"error": "not found"}, 404)

        # ── pages ──
        def _static(self, rel: str, csp: bool = False):
            target = (STATIC / rel).resolve()
            if STATIC not in target.parents or not target.is_file():
                return self._send(404, b"not found", "text/plain")
            ctype = mimetypes.guess_type(str(target))[0] or "application/octet-stream"
            if ctype.startswith("text/") or ctype.endswith("javascript"):
                ctype += "; charset=utf-8"
            headers = {"Content-Security-Policy": APP_CSP}
            self._send(200, target.read_bytes(), ctype, headers)

        def login(self):
            try:
                form = urllib.parse.parse_qs(self._body().decode())
            except ValueError:
                form = {}
            tok = (form.get("token") or [""])[0].strip()
            if tok and hmac.compare_digest(tok, app.token):
                secure = "; Secure" if self._secure() else ""
                return self._redirect("/", {"Set-Cookie": f"{COOKIE}={app.cookie}; Path=/; Max-Age=7776000; "
                                                          f"HttpOnly; SameSite=Lax{secure}"})
            time.sleep(1.0)
            return self._redirect("/login?error=1")

        # ── api ──
        def summary(self):
            db = app.db()
            one = lambda sql, *a: db.execute(sql, a).fetchone()[0]
            views = {
                "inbox": one("SELECT COUNT(*) FROM items WHERE archived=0 AND noise=0 AND read=0"),
                "attention": one("SELECT COUNT(*) FROM items WHERE archived=0 AND attention=1 AND read=0"),
                "attention_total": one(f"SELECT COUNT(*) FROM items WHERE {VIEW_SQL['attention']}"),
                "starred": one("SELECT COUNT(*) FROM items WHERE starred=1"),
                "archive": one("SELECT COUNT(*) FROM items WHERE archived=1"),
            }
            sources_ = {r["source"]: {"unread": r["unread"], "total": r["total"]} for r in db.execute(
                "SELECT source, SUM(read=0 AND noise=0 AND archived=0) unread, COUNT(*) total FROM items GROUP BY source")}
            groups = [dict(r) for r in db.execute(
                """SELECT source, group_key, group_label, profile, COUNT(*) total,
                          SUM(read=0 AND noise=0 AND archived=0) unread,
                          SUM(attention=1 AND archived=0 AND read=0) attention, MAX(ts) last_ts
                   FROM items WHERE source IN ('cron','kanban','note')
                   GROUP BY source, group_key, profile ORDER BY MAX(ts) DESC""")]
            # Use the most recent label for each group (job names get renamed).
            for g in groups:
                lab = db.execute("SELECT group_label, status FROM items WHERE source=? AND group_key=? AND profile=? "
                                 "ORDER BY ts DESC LIMIT 1", (g["source"], g["group_key"], g["profile"])).fetchone()
                g["group_label"], g["last_status"] = lab["group_label"], lab["status"]
            return {"views": views, "sources": sources_, "groups": groups,
                    "last_index": store.get_meta(db, "last_index"), "now": time.time()}

        def list_items(self, params):
            base, base_args = build_filter(params)
            limit = max(1, min(int(params.get("limit", 60)), 200))
            where, args = base, list(base_args)
            if params.get("before"):
                ts, _, iid = params["before"].partition(":")
                where += " AND (ts < ? OR (ts = ? AND id < ?))"
                args += [float(ts), float(ts), int(iid or 0)]
            db = app.db()
            rows = db.execute(
                f"SELECT * FROM items WHERE {where} ORDER BY ts DESC, id DESC LIMIT ?", (*args, limit + 1)).fetchall()
            items = [row_to_list(r) for r in rows[:limit]]
            nxt = f"{rows[limit - 1]['ts']}:{rows[limit - 1]['id']}" if len(rows) > limit else None
            counts = db.execute(f"SELECT COUNT(*), COALESCE(SUM(read=0),0) FROM items WHERE {base}", base_args).fetchone()
            out = {"items": items, "next": nxt, "total": counts[0], "unread": counts[1]}
            if params.get("group") and params.get("source") == "cron":
                u = db.execute(f"SELECT COUNT(cost_usd), COALESCE(SUM(cost_usd), 0), MIN(ts) FROM items "
                               f"WHERE {base} AND cost_usd IS NOT NULL", base_args).fetchone()
                models = [r[0] for r in db.execute(
                    f"SELECT model FROM items WHERE {base} AND model IS NOT NULL AND model != '' "
                    f"GROUP BY model ORDER BY MAX(ts) DESC", base_args)]
                out["group_usage"] = {"priced_runs": u[0], "cost_usd": round(u[1], 6), "since": u[2],
                                      "models": models}
            return out

        def get_item(self, iid):
            row = app.db().execute("SELECT * FROM items WHERE id=?", (iid,)).fetchone()
            if not row:
                return self._json({"error": "not found"}, 404)
            d = dict(row)
            d["extra"] = json.loads(d["extra"] or "{}")
            if d["source"] == "artifact":
                d["extra"].update(sources.file_facts(d["extra"].get("path", "")))
            # neighbours in the same group (previous/next run of the same job)
            if d["source"] in ("cron", "kanban"):
                prev_ = app.db().execute("SELECT id, ts, status FROM items WHERE source=? AND group_key=? AND ts<? "
                                         "ORDER BY ts DESC LIMIT 1", (d["source"], d["group_key"], d["ts"])).fetchone()
                d["prev_in_group"] = dict(prev_) if prev_ else None
            return self._json(d)

        def update_item(self, iid, data):
            sets, args = [], []
            for k in ("read", "starred", "archived"):
                if k in data:
                    sets.append(f"{k}=?")
                    args.append(1 if data[k] else 0)
            if not sets:
                return self._json({"error": "nothing to update"}, 400)
            db = app.db()
            db.execute(f"UPDATE items SET {', '.join(sets)} WHERE id=?", (*args, iid))
            db.commit()
            return self._json({"ok": True})

        def bulk(self, data):
            action = data.get("action")
            col = {"read": ("read", 1), "unread": ("read", 0), "archive": ("archived", 1),
                   "unarchive": ("archived", 0)}.get(action)
            if not col:
                return self._json({"error": "bad action"}, 400)
            db = app.db()
            if isinstance(data.get("ids"), list):
                ids = [int(i) for i in data["ids"]][:5000]
                q = f"UPDATE items SET {col[0]}=? WHERE id IN ({','.join('?' * len(ids))})"
                cur = db.execute(q, (col[1], *ids)) if ids else None
            else:
                where, args = build_filter({k: str(v) for k, v in (data.get("filter") or {}).items()})
                cur = db.execute(f"UPDATE items SET {col[0]}=? WHERE {where}", (col[1], *args))
            db.commit()
            return self._json({"ok": True, "changed": cur.rowcount if cur else 0})

        def add_note(self, data):
            title = str(data.get("title") or "").strip()
            if not title:
                return self._json({"error": "title required"}, 400)
            level = str(data.get("level") or "info")
            group = str(data.get("group") or "Notes")
            now = time.time()
            iid = store.insert_item(app.db(), {
                "source": "note", "source_ref": f"{now:.6f}-{secrets.token_hex(4)}",
                "title": title[:300], "summary": sources.summarize(str(data.get("body") or ""))[0][:240],
                "body": str(data.get("body") or "")[:sources.BODY_CAP], "status": level,
                "attention": level in ("attention", "warning", "error"),
                "group_key": re.sub(r"[^a-z0-9]+", "-", group.lower()).strip("-") or "notes",
                "group_label": group, "ts": now,
                "extra": {"level": level, "tags": data.get("tags") or [], "link": data.get("link")},
            })
            app.db().commit()
            return self._json({"ok": True, "id": iid}, 201)

        def usage_report(self, params):
            db = app.db()
            try:
                days = max(0, min(int(params.get("days", 30)), 3650))
            except ValueError:
                days = 30
            since = time.time() - days * 86400 if days else 0
            win = f"{USAGE_RUNS} AND ts >= ?"
            tot = db.execute(f"SELECT COUNT(*), COUNT(cost_usd), COALESCE(SUM(cost_usd), 0), "
                             f"COALESCE(SUM(tokens_in), 0), COALESCE(SUM(tokens_out), 0) FROM items WHERE {win}",
                             (since,)).fetchone()
            by_job = []
            for r in db.execute(
                    f"""SELECT group_key, profile, COUNT(*) runs, COUNT(cost_usd) priced,
                               COALESCE(SUM(cost_usd), 0) cost, COALESCE(SUM(tokens_in), 0) tin,
                               COALESCE(SUM(tokens_out), 0) tout, MAX(ts) last_ts
                        FROM items WHERE {win} GROUP BY group_key, profile
                        ORDER BY cost DESC, runs DESC""", (since,)):
                label = db.execute("SELECT group_label FROM items WHERE source='cron' AND group_key=? AND profile=? "
                                   "ORDER BY ts DESC LIMIT 1", (r["group_key"], r["profile"])).fetchone()[0]
                models = [m[0] for m in db.execute(
                    f"SELECT model FROM items WHERE {win} AND group_key=? AND profile=? AND model != '' "
                    f"AND model IS NOT NULL GROUP BY model ORDER BY COUNT(*) DESC", (since, r["group_key"], r["profile"]))]
                by_job.append({**dict(r), "label": label, "cost": round(r["cost"], 6),
                               "avg": round(r["cost"] / r["priced"], 6) if r["priced"] else None, "models": models})
            by_model = [dict(r) | {"cost": round(r["cost"], 6)} for r in db.execute(
                f"""SELECT model, COUNT(*) runs, COALESCE(SUM(cost_usd), 0) cost, COALESCE(SUM(tokens_in), 0) tin,
                           COALESCE(SUM(tokens_out), 0) tout FROM items
                    WHERE {win} AND model IS NOT NULL AND model != '' GROUP BY model ORDER BY cost DESC""", (since,))]
            by_day = [{"day": r[0], "cost": round(r[1], 6), "runs": r[2]} for r in db.execute(
                f"""SELECT date(ts, 'unixepoch', 'localtime') d, COALESCE(SUM(cost_usd), 0), COUNT(*)
                    FROM items WHERE {win} GROUP BY d ORDER BY d""", (since,))]
            first_priced = db.execute(f"SELECT MIN(ts) FROM items WHERE {USAGE_RUNS} AND cost_usd IS NOT NULL"
                                      ).fetchone()[0]
            return {"days": days, "since": since, "runs": tot[0], "priced_runs": tot[1],
                    "cost_usd": round(tot[2], 6), "tokens_in": tot[3], "tokens_out": tot[4],
                    "first_priced_ts": first_priced, "by_job": by_job, "by_model": by_model, "by_day": by_day,
                    "storage": retention.storage(db, app.db_path or store.default_db_path())}

        def export(self, params):
            fmt = params.get("format", "json")
            if fmt not in ("json", "csv"):
                return self._json({"error": "format must be json or csv"}, 400)
            where, args = build_filter(params)
            # A real temp file (not Spooled: on 3.10 it cannot back a TextIOWrapper) gives us Content-Length.
            with tempfile.TemporaryFile(mode="w+b") as raw:
                fh = io.TextIOWrapper(raw, encoding="utf-8", newline="")
                rows = export.rows(app.db(), where, args)
                if fmt == "json":
                    export.write_json(rows, fh)
                else:
                    export.write_csv(rows, fh, include_body=params.get("body") == "1")
                fh.flush()
                size = raw.tell()
                raw.seek(0)
                stamp = time.strftime("%Y%m%d-%H%M")
                name = f"hermes-inbox-{params.get('view', 'inbox')}-{stamp}.{fmt}"
                self.send_response(200)
                self.send_header("Content-Type", "application/json; charset=utf-8" if fmt == "json"
                                 else "text/csv; charset=utf-8")
                self.send_header("Content-Length", str(size))
                self.send_header("Content-Disposition", f'attachment; filename="{name}"')
                self.send_header("Cache-Control", "no-store")
                self.send_header("X-Content-Type-Options", "nosniff")
                self.end_headers()
                while True:
                    chunk = raw.read(65536)
                    if not chunk:
                        break
                    self.wfile.write(chunk)
                fh.detach()

        def jobs(self):
            out = []
            db = app.db()
            for profile, home in sources.hermes_homes(app.root):
                for jid, j in sources.load_jobs(home).items():
                    out.append({
                        "id": jid, "profile": profile, "name": j.get("name") or jid,
                        "enabled": j.get("enabled", True), "state": j.get("state"),
                        "schedule": (j.get("schedule") or {}).get("display") or j.get("schedule_display"),
                        "next_run_at": j.get("next_run_at"), "last_run_at": j.get("last_run_at"),
                        "last_status": j.get("last_status"), "last_error": j.get("last_error"),
                        "deliver": j.get("deliver"), "model": j.get("model"),
                        "runs": db.execute("SELECT COUNT(*) FROM items WHERE source='cron' AND group_key=? AND profile=?",
                                           (jid, profile)).fetchone()[0],
                    })
            return {"jobs": out}

        def serve_file(self, iid, params):
            row = app.db().execute("SELECT source, extra FROM items WHERE id=?", (iid,)).fetchone()
            if not row or row["source"] != "artifact":
                return self._json({"error": "not found"}, 404)
            path = json.loads(row["extra"]).get("path", "")
            # Only paths the agent itself published are servable; the DB row is the allowlist.
            try:
                st = os.stat(path)
            except OSError:
                return self._json({"error": "file no longer exists", "path": path}, 410)
            if not os.path.isfile(path):
                return self._json({"error": "not a file"}, 404)
            ctype = sources.file_facts(path)["mime"]
            if params.get("as") == "text" or ctype in ("text/markdown",) or (
                    ctype.startswith("text/") and ctype not in SANDBOXED_TYPES):
                ctype = "text/plain"
            headers = {"Content-Security-Policy": "sandbox allow-scripts allow-popups allow-forms"}
            if ctype not in SANDBOXED_TYPES and ctype != "text/plain":
                headers = {}  # PDF viewers and media players refuse sandboxed frames
            name = urllib.parse.quote(os.path.basename(path))
            disp = "attachment" if params.get("download") else "inline"
            headers["Content-Disposition"] = f"{disp}; filename*=UTF-8''{name}"
            headers["Accept-Ranges"] = "bytes"
            if ctype.startswith("text/"):
                ctype += "; charset=utf-8"
            size = st.st_size
            start, end = 0, size - 1
            rng = self.headers.get("Range", "")
            m = re.fullmatch(r"bytes=(\d*)-(\d*)", rng.strip())
            status = 200
            if m and size:
                if m.group(1):
                    start = int(m.group(1))
                    end = int(m.group(2)) if m.group(2) else size - 1
                elif m.group(2):
                    start = max(0, size - int(m.group(2)))
                end = min(end, size - 1)
                if start > end:
                    return self._send(416, b"", "text/plain", {"Content-Range": f"bytes */{size}"})
                status = 206
                headers["Content-Range"] = f"bytes {start}-{end}/{size}"
            length = end - start + 1 if size else 0
            self.send_response(status)
            self.send_header("Content-Type", ctype)
            self.send_header("Content-Length", str(length))
            self.send_header("X-Content-Type-Options", "nosniff")
            self.send_header("Cache-Control", "private, max-age=60")
            for k, v in headers.items():
                self.send_header(k, v)
            self.end_headers()
            if self.command == "HEAD" or not length:
                return
            with open(path, "rb") as fh:
                fh.seek(start)
                remaining = length
                while remaining > 0:
                    chunk = fh.read(min(65536, remaining))
                    if not chunk:
                        break
                    self.wfile.write(chunk)
                    remaining -= len(chunk)

    return Handler


def serve(host: str, port: int, db_path: Path | None = None, root: Path | None = None):
    app = App(db_path, root)
    stop = threading.Event()
    t = threading.Thread(target=app.index_loop, args=(stop,), daemon=True, name="indexer")
    t.start()
    httpd = ThreadingHTTPServer((host, port), make_handler(app))
    httpd.daemon_threads = True
    log.info("Hermes Inbox on http://%s:%d (hermes home %s)", host, port, app.root)
    try:
        httpd.serve_forever()
    finally:
        stop.set()
        httpd.server_close()
