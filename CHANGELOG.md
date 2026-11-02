# Changelog

All notable changes to Hermes Inbox. Versions follow [Semantic Versioning](https://semver.org).

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
