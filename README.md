<div align="center">

<img src="hermes_inbox/static/icon.svg" alt="" width="72">

# Hermes Inbox

**One inbox for everything your [Hermes Agent](https://github.com/NousResearch/hermes-agent) does.**

Cron results, files the agent hands you in chat, kanban hand-offs and your own notes, in a fast mail-style web app that works just as well on your phone.

[![Tests](https://github.com/jcrabapple/hermes-inbox/actions/workflows/tests.yml/badge.svg)](https://github.com/jcrabapple/hermes-inbox/actions/workflows/tests.yml)
![Python 3.10+](https://img.shields.io/badge/python-3.10%2B-3776AB?logo=python&logoColor=white)
![No dependencies](https://img.shields.io/badge/dependencies-none-2f4a8a)
![License: MIT](https://img.shields.io/badge/license-MIT-lightgrey)

</div>

![Desktop, dark theme](docs/screenshots/desktop-dark.png)

<p align="center">
  <img src="docs/screenshots/phone-list-dark.png" alt="Phone inbox, dark theme" width="260">
  <img src="docs/screenshots/phone-item-dark.png" alt="Phone reading view with bottom actions" width="260">
  <img src="docs/screenshots/phone-list-light.png" alt="Phone, light theme" width="260">
</p>

<sub>Screenshots use the bundled demo data (`tools/demo_home.py`); nothing in them is real.</sub>

## Why

A busy Hermes setup produces a lot: dozens of scheduled jobs, reports, alerts, files
shared across Telegram, Discord, the desktop app and the CLI, plus kanban workers handing
tasks back and forth. That output ends up scattered across chat channels and
`~/.hermes/cron/output`, and Hermes rotates both. Hermes Inbox gathers all of it in one
place, keeps its own copy, and lets you triage it like email: read, star, archive, search.

## What lands in it

| Source | Read from | One item per |
|---|---|---|
| **Scheduled runs** | `cron/output/<job>/*.md` in your Hermes home and every profile | run |
| **Artifacts** | `MEDIA:<path>` and `::preview{file=…}` in assistant messages (`state.db`, every profile), kanban artifacts and task attachments | file (re-shares merge; an edited file comes back unread) |
| **Kanban** | finished `task_runs` across `kanban.db` and `kanban/boards/*/kanban.db` | worker run |
| **Notes** | `hermes-inbox post …` or `POST /api/notes` from any script | note |

Hermes databases are opened **read-only**. Nothing under your Hermes home is ever written.

## Features

- **Noise control.** Runs skipped by a script gate, `[SILENT]` runs, and runs whose output
  matches the previous run (ignoring timestamps and counters) stay out of the inbox. They're
  still visible inside each job.
- **Needs attention.** Failed runs, blocked or abandoned kanban tasks, and reports that open
  with DEGRADED, FAILED, "action needed", "needs your decision" or a warning emoji are
  flagged. Negations like "no action needed" aren't. In a streak of identical alerts only
  the first one is flagged.
- **Rich previews.** Markdown rendered, images, PDFs, HTML (sandboxed), audio/video,
  text and code, with Open and Download.
- **Full-text search** (SQLite FTS5) over everything, including runs whose output files
  Hermes has since deleted.
- **Phone-first UI.** Bottom tab bar, bottom action bar in the reader, swipe right to toggle
  read, swipe left to archive (with Undo), 44px touch targets, safe-area aware. Installable
  to the home screen.
- **Dark by default, light on demand.** Every text/surface pair in both themes passes WCAG
  AA (body text passes AAA), checked by `tools/contrast.py` in CI.
- **Keyboard on desktop.** `j`/`k` move, `e` archive, `s` star, `u` unread, `/` search,
  `Shift+I` mark the view read.
- **Zero dependencies.** Python standard library on the server, vanilla JS in the browser
  (marked and DOMPurify vendored).

## Quick start

Requires Python 3.10+ with SQLite FTS5 (standard in python.org builds and every major
Linux distro) on the machine where Hermes runs.

```bash
pipx install git+https://github.com/jcrabapple/hermes-inbox.git
hermes-inbox serve            # http://127.0.0.1:8941
hermes-inbox token --show     # the login token (generated on first run)
```

Or straight from a clone, no install:

```bash
git clone https://github.com/jcrabapple/hermes-inbox.git && cd hermes-inbox
python3 -m hermes_inbox serve
```

The first index pass takes a few seconds. Anything older than 48 hours is filed as read
so you start with recent activity, not months of backlog. After that it checks for new
items every 60 seconds.

### Try it with demo data

```bash
python3 tools/demo_home.py /tmp/hermes-demo
HERMES_INBOX_DATA=/tmp/hermes-demo/inbox HERMES_INBOX_TOKEN=demo \
  python3 -m hermes_inbox --hermes-home /tmp/hermes-demo serve --port 8950
# open http://127.0.0.1:8950 and sign in with "demo"
```

## Configuration

Everything is optional.

| Variable / flag | Default | Purpose |
|---|---|---|
| `--hermes-home`, `HERMES_HOME` | `~/.hermes` | Hermes home to index (profiles under `profiles/` are picked up automatically) |
| `--host`, `HERMES_INBOX_HOST` | `127.0.0.1` | Bind address |
| `--port`, `HERMES_INBOX_PORT` | `8941` | Port |
| `HERMES_INBOX_DATA` | `$XDG_DATA_HOME/hermes-inbox` | Where `inbox.db` lives (mode 600) |
| `HERMES_INBOX_CONFIG` | `$XDG_CONFIG_HOME/hermes-inbox` | Where the `token` file lives (mode 600) |
| `HERMES_INBOX_TOKEN` | *(token file)* | Use this token instead of the file |
| `HERMES_INBOX_INTERVAL` | `60` | Seconds between index passes |

## Running it as a service

`deploy/hermes-inbox.service` is a systemd **user** unit:

```bash
cp deploy/hermes-inbox.service ~/.config/systemd/user/
# edit WorkingDirectory/ExecStart to match how you installed it
systemctl --user daemon-reload && systemctl --user enable --now hermes-inbox
```

### Reaching it from your phone

The inbox holds copies of your agent's reports and the files it shared with you, so keep
it private. In order of preference:

1. **A private network** (Tailscale, WireGuard): leave it bound to `127.0.0.1` and use
   `tailscale serve`, or bind to the tailnet address.
2. **A tunnel with an access layer** (Cloudflare Tunnel plus Cloudflare Access, or any
   reverse proxy with SSO in front).
3. **A plain HTTPS reverse proxy or tunnel.** The app's own token login is the only
   lock then. The token is 256 bits of randomness, so guessing it isn't practical, but
   don't skip TLS.

Example Cloudflare Tunnel ingress:

```yaml
ingress:
  - hostname: inbox.example.com
    service: http://127.0.0.1:8941
  - service: http_status:404
```

## Posting your own notes

Anything without a better home (script alerts, deploy notices, reminders) can go into the
inbox:

```bash
hermes-inbox post "Disk almost full" -b "/var at 91%" --level attention --group "Disk watch"
echo "**v1.4** is live" | hermes-inbox post "Deploy finished" - --link https://example.com/releases
```

Or over HTTP with the token:

```bash
curl -H "Authorization: Bearer $(hermes-inbox token --show)" \
     -H 'Content-Type: application/json' \
     -d '{"title":"Deploy finished","body":"**v1.4** is live","level":"info","group":"Deploys"}' \
     http://127.0.0.1:8941/api/notes
```

`level` is `info`, `attention`, `warning` or `error`; anything other than `info` shows
under Needs attention.

## HTTP API

Authenticate with `Authorization: Bearer <token>` or the session cookie. Writes need a
JSON body.

| Method | Path | |
|---|---|---|
| `GET` | `/api/summary` | Counts per view, source and job; last index pass |
| `GET` | `/api/items?view=&source=&group=&profile=&q=&ftype=&noise=1&before=` | Paged list. `view`: `inbox`, `attention`, `starred`, `all`, `archive` |
| `GET` | `/api/items/<id>` | One item with its full body |
| `GET` | `/api/items/<id>/file[?download=1]` | An artifact's file (only paths the agent published) |
| `POST` | `/api/items/<id>` | `{"read": bool, "starred": bool, "archived": bool}` |
| `POST` | `/api/bulk` | `{"action": "read"\|"unread"\|"archive"\|"unarchive", "ids": [...] \| "filter": {...}}` |
| `POST` | `/api/notes` | Create a note |
| `POST` | `/api/reindex` | Index now |
| `GET` | `/api/jobs` | Cron jobs from every profile's `jobs.json` |

## CLI

```
hermes-inbox [--hermes-home DIR] serve [--host H] [--port P]
hermes-inbox index          one index pass, prints how many new items it found
hermes-inbox post TITLE [-b BODY | -] [--level L] [--group NAME] [--link URL]
hermes-inbox stats          item counts per source
hermes-inbox retitle        recompute run subjects after changing the subject rules
hermes-inbox token [--show] where the login token lives, or the token itself
```

## Security

- **Login.** One random token per install. The session cookie is an HMAC of it (HttpOnly,
  SameSite=Lax, Secure behind HTTPS). To rotate, delete the token file and restart; every
  session is signed out.
- **File access.** Files are allowlisted by the database. Only paths the agent itself
  published (assistant `MEDIA:` lines, kanban artifacts) can be served. Paths users paste
  into chat are not.
- **Untrusted content.** HTML and SVG artifacts are served with
  `Content-Security-Policy: sandbox`, so they run in an opaque origin and can't call the
  API with your cookie. Markdown is sanitized with DOMPurify.
- **CSRF.** Cookie-authenticated writes must be `application/json`, which a cross-site form
  can't send without a preflight.
- **Storage.** The inbox database and token file are created owner-only (600, in 700
  directories).

## How it works

```
~/.hermes ──read-only──▶ indexer (every 60s, per-source cursors)
  cron/output/*/*.md            │
  state.db  (MEDIA: lines)      ▼
  kanban/**/kanban.db     inbox.db (SQLite + FTS5, read/star/archive state)
                                │
                                ▼
                     stdlib HTTP server ──▶ single-page app (vanilla JS)
```

```
hermes_inbox/
  cli.py        CLI entry point
  server.py     HTTP server, auth, JSON API, file serving
  indexer.py    incremental indexing with per-source cursors
  sources.py    readers for cron output, state.db and kanban
  store.py      schema, FTS5, upserts
  static/       the web app (HTML, CSS, JS, icons, vendored libraries)
tools/
  contrast.py   WCAG contrast check for both themes
  demo_home.py  fake Hermes home for demos and screenshots
tests/          python3 -m unittest discover -s tests
```

## Development

```bash
python3 -m unittest discover -s tests   # behaviour tests against a temp Hermes home
python3 tools/contrast.py               # fails if a colour change breaks contrast
```

To add a source, write a reader in `sources.py` that yields item dicts, add a cursor-based
`_index_<source>` to `indexer.py`, register it in `SOURCE_META` in `static/app.js`, and
cover it in `tests/test_inbox.py`.

Hermes Inbox reads Hermes Agent's on-disk formats, which are not a stable public API. If
an update to Hermes changes them, please open an issue with a sample.

## License

MIT. See [LICENSE](LICENSE) and [NOTICE.md](NOTICE.md) for vendored code and icons.
Hermes Inbox is an independent project and is not affiliated with Nous Research.
