# Changelog

All notable changes to Hermes Inbox. Versions follow [Semantic Versioning](https://semver.org).

## 2.2.0 (2026-11-02)

### Added
- **Profile filter.** Once more than one profile has cron activity, a chip row at the top
  of the list scopes any view (and its exports and bulk actions) to one profile. The row
  stays hidden while only the default profile has cron jobs. Demo data now includes a
  second profile so the feature is visible in screenshots.

## 2.1.0 (2026-11-02)

### Added
- **What changed since the last run.** The reader can show a line diff of a run against
  the previous run of the same job (context lines, added/removed highlighted), computed
  server-side by `GET /api/items/<id>`. Runs with no changes show nothing new.
- **Live refresh.** `GET /api/pulse` fingerprints every watched source (mtime/size of
  cron output, `jobs.json`, `state.db`, kanban databases) and the web UI polls it every
  5 seconds while visible; a full index pass runs only when something changed, so new
  items appear within seconds. The 60-second background pass remains as a backstop.
- **Profiles.** Cron jobs, artifacts and kanban data from every Hermes profile
  (`~/.hermes/profiles/*`) are indexed alongside the default home, tagged with the
  profile name in the UI and filterable with the existing `profile` parameter.
  Retired (`profiles/.deleted`) and duplicate (`profiles/default`) directories are skipped.
- **`hermes-inbox doctor`** checks the Hermes home, every profile's cron and state
  databases, the inbox database (with an integrity check), token file permissions and
  environment variables, and exits 1 on failure.

### Fixed
- Alert emoji (⚠ 🚨 ❌ 🔴) now count as an attention signal only when one leads one of the
  first six lines of a report. Reports *about* other jobs that quote ⚠/🔴 deep in their
  bodies (log reviews, health digests) no longer get flagged.
- The reader no longer prints the subject twice when the agent wrapped the title into a
  longer first body line ("Title — more text").

## 2.0.0 (2026-11-02)

### Added
- **Model and cost per run.** Scheduled runs are matched to their Hermes cron session (by
  job title and finish time, following compression continuations). Each run gets its model,
  provider, tokens (input, cached, output, reasoning), API calls, duration and cost,
  including auxiliary models such as compression. Costs are marked as estimated or
  billed, depending on what Hermes recorded.
- **Usage & storage page** (`#/usage`, sidebar and the phone's More menu): total spend,
  cost per day, per job and per model for 7, 30, 90 days or all time, plus database size,
  item counts and the retention policy.
- **Cost line on job views:** what a job has cost across its priced runs, and the models
  it used.
- **Retention.** An hourly cleanup deletes quiet runs older than
  `HERMES_INBOX_KEEP_QUIET_DAYS` (default 30) and, optionally, every unstarred item older
  than `HERMES_INBOX_KEEP_DAYS`. Starred items are never deleted. Pruned runs are
  tombstoned so the indexer doesn't bring them back from cron output still on disk.
- **Export** of any view as JSON (full items) or CSV (one row per item, with model and
  cost columns), from a menu in the list header, the Usage page, `hermes-inbox export`
  or `GET /api/export`.
- CLI: `hermes-inbox usage`, `hermes-inbox prune`, `hermes-inbox export`.
- API: `GET /api/usage`, `GET /api/export`, `POST /api/prune`. List items carry `model`
  and `cost_usd`, and job lists return `group_usage`.
- Demo data includes cron session costs.

### Changed
- **Quiet runs older than 30 days are now deleted by default.** Set
  `HERMES_INBOX_KEEP_QUIET_DAYS=0` to keep the 1.x behaviour of keeping everything.
- The database gains `model`, `cost_usd`, `tokens_in` and `tokens_out` columns and a
  `tombstones` table. The migration runs automatically on first start and is additive.

## 1.0.0 (2026-11-02)

First stable release.

### Sources
- **Scheduled runs** from `cron/output/<job>/*.md` in the Hermes home and every profile.
  Recognises agent runs, `no_agent` script jobs, script-gate skips and failures (both the
  `## Error` section and the `(FAILED)` title).
- **Artifacts** from `MEDIA:` and `::preview{file=…}` lines in assistant messages across
  every profile's `state.db`, plus kanban run artifacts and task attachments. One item per
  file; re-shares merge, and a real edit brings it back as unread. Scratch and temp paths
  never resurface just because something overwrote them.
- **Kanban** worker runs from `kanban.db` and every board under `kanban/boards/`, labelled
  with the board's display name.
- **Notes** posted with `hermes-inbox post` or `POST /api/notes`.

### Triage
- Inbox, Needs attention, Starred, Everything and Archive views. The sidebar lists jobs
  with unread or flagged items first, then kanban boards.
- Skipped, `[SILENT]` and unchanged runs stay out of the inbox. Runs count as unchanged
  when the output matches the previous run with digits ignored.
- Flags failures, blocked or abandoned kanban tasks, and reports that open with
  DEGRADED / FAILED / "action needed" / "needs your decision" or warning emoji. Negations
  don't count, and repeats of the same alert go quiet.
- Full-text search (SQLite FTS5), bulk mark-read, star, archive with Undo.

### Interface
- Three-pane desktop layout with keyboard shortcuts.
- Phone layout with a bottom tab bar, a bottom action bar in the reader, swipe to
  read/archive, 44px touch targets, safe-area insets and scroll restore. Installable as
  a home-screen app.
- Dark theme by default, light theme on demand, both checked to WCAG AA (body text AAA)
  by `tools/contrast.py`.
- Inline previews for Markdown, images, PDFs, sandboxed HTML, audio, video, text and code.

### Operations
- Python standard library only. Installs with pipx or runs from a clone.
- Token login with an HMAC session cookie. File serving is allowlisted by the database,
  HTML/SVG are served sandboxed, and the database and token are owner-only.
- XDG data and config paths, environment overrides, a systemd user unit, and a demo data
  generator (`tools/demo_home.py`).
