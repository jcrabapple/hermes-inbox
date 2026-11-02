# Notices

## Not affiliated with Nous Research

Hermes Inbox is an independent companion tool for
[Hermes Agent](https://github.com/NousResearch/hermes-agent). It reads the files and
SQLite databases Hermes Agent writes to its home directory. It is not made, endorsed
or supported by Nous Research, and "Hermes" in the name only describes what it works
with.

## Vendored third-party code

Shipped unmodified under `hermes_inbox/static/vendor/`:

| File | Project | Version | License |
|---|---|---|---|
| `marked.min.js` | [marked](https://github.com/markedjs/marked) | 15.0.12 | MIT, Copyright (c) 2018+ MarkedJS, (c) 2011-2018 Christopher Jeffrey |
| `purify.min.js` | [DOMPurify](https://github.com/cure53/DOMPurify) | 3.4.17 | Apache-2.0 or MPL-2.0 (dual), Copyright (c) Cure53 and other contributors |

Each file keeps its upstream license header. Full license texts are in
`hermes_inbox/static/vendor/LICENSE-marked.md` and
`hermes_inbox/static/vendor/LICENSE-dompurify.txt`.

The interface icons are SVG paths from [Lucide](https://lucide.dev) (ISC license,
Copyright (c) Lucide Icons and Contributors), inlined in `hermes_inbox/static/index.html`.
License text: `hermes_inbox/static/vendor/LICENSE-lucide.txt`.
