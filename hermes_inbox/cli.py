"""hermes-inbox: one inbox for everything your Hermes agent does.

  hermes-inbox [--hermes-home ~/.hermes] serve [--host 127.0.0.1] [--port 8941]
  hermes-inbox index                       one-shot index pass, prints new counts
  hermes-inbox post TITLE [-b BODY | -]    drop a note into the inbox (stdin with -)
        [--level info|attention] [--group NAME] [--link URL]
  hermes-inbox stats
  hermes-inbox retitle                     recompute cron subjects after changing the subject rules
  hermes-inbox token [--show]              where the login token lives (creates it if missing)

Environment: HERMES_HOME (default ~/.hermes), HERMES_INBOX_DATA (default
$XDG_DATA_HOME/hermes-inbox), HERMES_INBOX_CONFIG (default $XDG_CONFIG_HOME/hermes-inbox),
HERMES_INBOX_TOKEN (overrides the token file), HERMES_INBOX_HOST, HERMES_INBOX_PORT,
HERMES_INBOX_INTERVAL (seconds between index passes, default 60).
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

from . import __version__, indexer, sources, store


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
    elif args.cmd == "token":
        from . import server
        tok = server.load_token()
        print(tok if args.show else (os.environ.get("HERMES_INBOX_TOKEN") and "set via HERMES_INBOX_TOKEN"
                                     or str(server.config_dir() / "token")))


if __name__ == "__main__":
    main()
