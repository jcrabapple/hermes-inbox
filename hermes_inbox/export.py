"""Export items as JSON or CSV."""
from __future__ import annotations

import csv
import json
import sqlite3
from datetime import datetime, timezone
from typing import IO, Iterator

CSV_COLUMNS = ("id", "time", "source", "status", "attention", "read", "starred", "archived", "group",
               "profile", "title", "summary", "model", "cost_usd", "tokens_in", "tokens_out",
               "job_id", "path", "body")


def iso(ts: float | None) -> str | None:
    return datetime.fromtimestamp(ts, timezone.utc).isoformat(timespec="seconds") if ts else None


def rows(conn: sqlite3.Connection, where: str, args: list) -> Iterator[sqlite3.Row]:
    yield from conn.execute(f"SELECT * FROM items WHERE {where} ORDER BY ts DESC, id DESC", args)


def item_dict(r: sqlite3.Row) -> dict:
    d = dict(r)
    d["extra"] = json.loads(d.get("extra") or "{}")
    d["time"] = iso(d["ts"])
    d["first_seen_time"] = iso(d.get("first_seen"))
    for k in ("attention", "noise", "read", "starred", "archived"):
        d[k] = bool(d[k])
    return d


def write_json(it: Iterator[sqlite3.Row], fh: IO[str]) -> int:
    n = 0
    fh.write("[")
    for r in it:
        fh.write(("," if n else "") + "\n" + json.dumps(item_dict(r), ensure_ascii=False))
        n += 1
    fh.write("\n]\n")
    return n


def _cell(v):
    # Spreadsheet apps execute cells that start with these characters as formulas.
    if isinstance(v, str) and v[:1] in ("=", "+", "-", "@", "\t", "\r"):
        return "'" + v
    return "" if v is None else v


def write_csv(it: Iterator[sqlite3.Row], fh: IO[str], include_body: bool = False) -> int:
    cols = CSV_COLUMNS if include_body else CSV_COLUMNS[:-1]
    w = csv.writer(fh)
    w.writerow(cols)
    n = 0
    for r in it:
        extra = json.loads(r["extra"] or "{}")
        rec = {
            "id": r["id"], "time": iso(r["ts"]), "source": r["source"], "status": r["status"],
            "attention": int(r["attention"]), "read": int(r["read"]), "starred": int(r["starred"]),
            "archived": int(r["archived"]), "group": r["group_label"], "profile": r["profile"],
            "title": r["title"], "summary": r["summary"], "model": r["model"],
            "cost_usd": "" if r["cost_usd"] is None else f"{r['cost_usd']:.6f}",
            "tokens_in": r["tokens_in"], "tokens_out": r["tokens_out"],
            "job_id": extra.get("job_id"), "path": extra.get("path"), "body": r["body"],
        }
        w.writerow([_cell(rec[c]) for c in cols])
        n += 1
    return n
