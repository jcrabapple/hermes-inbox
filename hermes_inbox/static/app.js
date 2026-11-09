'use strict';

const $ = (s, el = document) => el.querySelector(s);
const $$ = (s, el = document) => [...el.querySelectorAll(s)];
const esc = (s) => String(s ?? '').replace(/[&<>"']/g, (c) => ({ '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' }[c]));
const normText = (s) => String(s || '').replace(/[*_`~#>]+/g, '').replace(/\s+/g, ' ').trim().toLowerCase();
const icon = (name) => `<svg aria-hidden="true"><use href="#i-${name}"/></svg>`;
const narrowMQ = window.matchMedia('(max-width: 860px)');
const isNarrow = () => narrowMQ.matches;

const SOURCE_META = {
  cron: { label: 'Scheduled runs', icon: 'clock' },
  scheduler: { label: 'Job health', icon: 'alert' },
  artifact: { label: 'Artifacts', icon: 'file' },
  kanban: { label: 'Kanban', icon: 'board' },
  note: { label: 'Notes', icon: 'note' },
};
const VIEWS = [
  { id: 'inbox', label: 'Inbox', icon: 'inbox', badge: 'inbox' },
  { id: 'attention', label: 'Needs attention', icon: 'alert', badge: 'attention', hot: true },
  { id: 'starred', label: 'Starred', icon: 'star' },
  { id: 'snoozed', label: 'Snoozed', icon: 'clock', badge: 'snoozed' },
  { id: 'all', label: 'Everything', icon: 'layers' },
  { id: 'archive', label: 'Archive', icon: 'archive' },
];
const FTYPES = [['', 'All files'], ['doc', 'Documents'], ['image', 'Images'], ['html', 'Web pages'], ['media', 'Audio & video']];
const STATUS_TAG = {
  failed: ['warn', 'Failed'], blocked: ['warn', 'Blocked'], gave_up: ['warn', 'Gave up'], spawn_failed: ['warn', 'Spawn failed'],
  crashed: ['', 'Crashed'], timed_out: ['', 'Timed out'], review_requested: ['accent', 'Review requested'],
  skipped: ['', 'Skipped'], silent: ['', 'Silent'], unchanged: ['', 'Same as last run'], empty: ['', 'No output'],
  missing: ['', 'File gone'], completed: ['ok', 'Completed'], attention: ['warn', 'Attention'], warning: ['warn', 'Warning'], error: ['warn', 'Error'],
  overdue: ['', 'Overdue'], failing: ['', 'Failing'], resolved: ['ok', 'Resolved'],
};

const state = {
  route: { view: '__init__' }, q: '', ftype: '', noise: false, profile: '',
  items: [], next: null, total: 0, unread: 0, selected: null, summary: null,
  showAllJobs: false, listSeq: 0, listScroll: 0,
};

/* ── api ─────────────────────────────────────────── */
async function api(path, opts = {}) {
  const res = await fetch(path, {
    ...opts,
    headers: { ...(opts.body ? { 'Content-Type': 'application/json' } : {}), ...(opts.headers || {}) },
    credentials: 'same-origin',
  });
  if (res.status === 401) { location.href = '/login'; throw new Error('Signed out'); }
  if (!res.ok) throw new Error((await res.json().catch(() => ({}))).error || res.statusText);
  return res.json();
}
const post = (path, body) => api(path, { method: 'POST', body: JSON.stringify(body) });

/* ── time ────────────────────────────────────────── */
const DAY = 86400000;
function dayKey(ts) { const d = new Date(ts * 1000); return new Date(d.getFullYear(), d.getMonth(), d.getDate()).getTime(); }
function dayLabel(key) {
  const today = dayKey(Date.now() / 1000);
  if (key === today) return 'Today';
  if (key === today - DAY) return 'Yesterday';
  const d = new Date(key);
  const sameYear = d.getFullYear() === new Date().getFullYear();
  return d.toLocaleDateString(undefined, { weekday: 'short', month: 'short', day: 'numeric', ...(sameYear ? {} : { year: 'numeric' }) });
}
function shortTime(ts) {
  const d = new Date(ts * 1000);
  if (dayKey(ts) === dayKey(Date.now() / 1000)) return d.toLocaleTimeString(undefined, { hour: 'numeric', minute: '2-digit' });
  return d.toLocaleDateString(undefined, { month: 'short', day: 'numeric' });
}
function fullTime(ts) { return new Date(ts * 1000).toLocaleString(undefined, { dateStyle: 'medium', timeStyle: 'short' }); }
function ago(ts) {
  const s = Math.max(0, Date.now() / 1000 - ts);
  if (s < 60) return 'just now';
  if (s < 3600) return `${Math.round(s / 60)}m ago`;
  if (s < 86400) return `${Math.round(s / 3600)}h ago`;
  return `${Math.round(s / 86400)}d ago`;
}
function bytes(n) {
  if (n == null) return '';
  const u = ['B', 'KB', 'MB', 'GB']; let i = 0;
  while (n >= 1024 && i < u.length - 1) { n /= 1024; i++; }
  return `${n.toFixed(i ? 1 : 0)} ${u[i]}`;
}

/* ── markdown ────────────────────────────────────── */
marked.setOptions({ gfm: true, breaks: false });
DOMPurify.addHook('afterSanitizeAttributes', (node) => {
  if (node.tagName === 'A' && node.getAttribute('href')) { node.setAttribute('target', '_blank'); node.setAttribute('rel', 'noopener noreferrer'); }
  if (node.tagName === 'IMG') node.setAttribute('loading', 'lazy');
});
function md(text) { return DOMPurify.sanitize(marked.parse(String(text || ''))); }

/* ── routing ─────────────────────────────────────── */
function parseHash() {
  const parts = location.hash.replace(/^#\/?/, '').split('/').map(decodeURIComponent);
  if (parts[0] === 'usage') return { page: 'usage', days: /^\d+$/.test(parts[1] || '') ? Number(parts[1]) : 30 };
  const r = { view: 'inbox' };
  if (parts[0] === 'view' && parts[1]) r.view = parts[1];
  else if (parts[0] === 'source' && parts[1]) { r.view = 'all'; r.source = parts[1]; }
  else if (parts[0] === 'group' && parts[1] && parts[2]) {
    r.view = 'all'; r.source = parts[1]; r.group = parts[2];
    r.profile = parts[3] && parts[3] !== 'item' ? parts[3] : '';
  }
  const itemIdx = parts.indexOf('item');
  r.item = itemIdx >= 0 ? Number(parts[itemIdx + 1]) : null;
  return r;
}
function routeHash(r, item) {
  let h = '#/view/' + r.view;
  if (r.group) h = `#/group/${encodeURIComponent(r.source)}/${encodeURIComponent(r.group)}${r.profile ? '/' + encodeURIComponent(r.profile) : ''}`;
  else if (r.source) h = `#/source/${encodeURIComponent(r.source)}`;
  if (item) h += `/item/${item}`;
  return h;
}
function sameList(a, b) { return a.view === b.view && a.source === b.source && a.group === b.group && (a.profile || '') === (b.profile || ''); }

async function onRoute() {
  const r = parseHash();
  if (r.page === 'usage') {
    // Usage is a page over the current list: keep the list route, show the report in the reader.
    closeDrawer();
    state.page = 'usage';
    if (state.route.view === '__init__') { state.route = { view: 'inbox' }; await loadList(true); }
    if ($('#app').dataset.pane !== 'item') state.listScroll = $('#list').scrollTop;
    $('#app').dataset.pane = 'item';
    renderNav();
    showUsage(r.days);
    return;
  }
  const leavingPage = state.page === 'usage';
  state.page = null;
  if (leavingPage) renderNav();
  const listChanged = !sameList(r, state.route);
  const wasItem = $('#app').dataset.pane === 'item' && !leavingPage;
  state.route = r;
  closeDrawer();
  if (listChanged) {
    if (r.source !== 'artifact') state.ftype = '';
    state.listScroll = 0;
    renderNav();
    await loadList(true);
  }
  if (r.item && !wasItem) state.listScroll = $('#list').scrollTop; // read before the list gets hidden
  $('#app').dataset.pane = r.item ? 'item' : 'list';
  $('#app').classList.remove('page-usage');
  if (r.item) {
    openItem(r.item, false);
  } else {
    showEmpty();
    // Phones hide the list while reading; put the reader back where they left it.
    if ((wasItem || leavingPage) && !listChanged) requestAnimationFrame(() => { $('#list').scrollTop = state.listScroll; });
  }
}

/* ── sidebar + tab bar ───────────────────────────── */
function navLink(href, label, iconName, badge, { current = false, hot = false, dot = null, title = '' } = {}) {
  return `<li><a href="${href}" ${current ? 'aria-current="true"' : ''} ${title ? `title="${esc(title)}"` : ''}>`
    + (dot !== null ? `<span class="dot ${dot}"></span>` : icon(iconName))
    + `<span class="label">${esc(label)}</span>`
    + (badge ? `<span class="badge ${hot ? 'hot' : ''}">${badge}</span>` : '') + '</a></li>';
}
function renderNav() {
  const s = state.summary; const r = state.route;
  if (!s) return;
  const onPage = state.page === 'usage';
  $('#nav-views').innerHTML = VIEWS.map((v) => navLink(`#/view/${v.id}`, v.label, v.icon,
    v.badge ? s.views[v.badge] || '' : '', { current: !onPage && !r.source && r.view === v.id, hot: v.hot })).join('')
    + navLink('#/usage', 'Usage & storage', 'chart', '', { current: onPage });
  $('#nav-sources').innerHTML = Object.entries(SOURCE_META)
    .filter(([k]) => s.sources[k])
    .map(([k, m]) => navLink(`#/source/${k}`, m.label, m.icon, s.sources[k].unread || '', { current: r.source === k && !r.group })).join('');

  const groupDot = (g) => (g.attention ? 'attention' : ['failed', 'blocked'].includes(g.last_status) ? 'failed'
    : ['ok', 'completed'].includes(g.last_status) ? 'ok' : 'quiet');
  const link = (g) => navLink(`#/group/${encodeURIComponent(g.source)}/${encodeURIComponent(g.group_key)}${g.profile !== 'default' ? '/' + encodeURIComponent(g.profile) : ''}`,
    g.group_label + (g.profile !== 'default' ? ` (${g.profile})` : ''), '', g.unread || '',
    { current: r.group === g.group_key && r.source === g.source, dot: groupDot(g), hot: !!g.attention, title: `${g.group_label} · last activity ${ago(g.last_ts)}` });

  const jobs = s.groups.filter((g) => g.source === 'cron' || g.source === 'note');
  // Default: anything unread or flagged, topped up with the most recently active, max 10.
  const hot = jobs.filter((g) => g.unread || g.attention);
  const rest = jobs.filter((g) => !hot.includes(g));
  const shown = state.showAllJobs ? jobs : hot.concat(rest.slice(0, Math.max(0, 10 - hot.length)));
  $('#nav-jobs').innerHTML = shown.map(link).join('');
  $('#jobs-toggle').textContent = state.showAllJobs ? 'Show recent' : `Show all ${jobs.length}`;
  $('#jobs-toggle').hidden = jobs.length <= shown.length && !state.showAllJobs;

  const boards = s.groups.filter((g) => g.source === 'kanban');
  $('#boards-heading').hidden = !boards.length;
  $('#nav-boards').innerHTML = boards.map(link).join('');

  const li = s.last_index;
  $('#index-status').textContent = li ? `Checked ${ago(li.at)}` : 'Indexing…';
  document.title = s.views.inbox ? `(${s.views.inbox}) Hermes Inbox` : 'Hermes Inbox';

  const tab = onPage || r.view === 'snoozed' ? 'more' : r.group ? '' : r.source === 'artifact' ? 'artifact' : r.source ? '' : r.view;
  $$('.tab[data-tab]').forEach((t) => t.setAttribute('aria-current', String(t.dataset.tab === tab)));
  $('[data-badge="inbox"]').textContent = s.views.inbox > 99 ? '99+' : s.views.inbox || '';
  $('[data-badge="attention"]').textContent = s.views.attention || '';
}
let snoozeTimer;
function scheduleSnoozeRefresh() {
  // Snoozed items come back on their own; refresh when the earliest one is due.
  clearTimeout(snoozeTimer);
  const due = state.summary?.next_snooze;
  if (!due) return;
  const ms = Math.max(1000, due * 1000 - Date.now() + 1000);
  snoozeTimer = setTimeout(async () => {
    await loadSummary();
    if (!state.q) { await loadList(true); markSelected(); }
  }, Math.min(ms, 2147483000));
}
async function loadSummary() {
  try { state.summary = await api('/api/summary'); renderNav(); scheduleSnoozeRefresh(); } catch (e) { console.warn(e); }
}
function openDrawer() { $('#app').classList.add('nav-open'); $('#btn-drawer-close').focus({ preventScroll: true }); }
function closeDrawer() { $('#app').classList.remove('nav-open'); }

/* ── list ────────────────────────────────────────── */
function listTitle() {
  const r = state.route;
  if (r.group) {
    const g = state.summary?.groups.find((x) => x.group_key === r.group && x.source === r.source);
    return g ? g.group_label : r.group;
  }
  if (r.source) return SOURCE_META[r.source]?.label || r.source;
  return VIEWS.find((v) => v.id === r.view)?.label || 'Inbox';
}
function listParams(before) {
  const r = state.route; const p = new URLSearchParams({ view: r.view, limit: '60' });
  if (r.source) p.set('source', r.source);
  if (r.group) p.set('group', r.group);
  if (r.profile) p.set('profile', r.profile);
  else if (r.group) p.set('profile', 'default');
  else if (state.profile) p.set('profile', state.profile);
  if (state.q) p.set('q', state.q);
  if (state.ftype) p.set('ftype', state.ftype);
  if (state.noise) p.set('noise', '1');
  if (before) p.set('before', before);
  return p;
}
async function loadList(reset) {
  const seq = ++state.listSeq;
  if (reset) { state.items = []; state.next = null; }
  $('#list-title').textContent = listTitle();
  renderFilters();
  if (reset) $('#list').innerHTML = '<div class="list-msg">Loading…</div>';
  try {
    const data = await api('/api/items?' + listParams(reset ? null : state.next));
    if (seq !== state.listSeq) return;
    state.items = reset ? data.items : state.items.concat(data.items);
    state.next = data.next; state.total = data.total; state.unread = data.unread;
    if (reset) renderGroupUsage(data.group_usage);
    renderList();
  } catch (e) {
    if (seq === state.listSeq) $('#list').innerHTML = `<div class="list-msg"><strong>Couldn't load items</strong>${esc(e.message)}</div>`;
  }
}
function knownProfiles() {
  // Profiles with cron activity (kanban worker profiles are not clutter for this filter).
  const set = new Set(['default']);
  for (const g of state.summary?.groups || []) if (g.source === 'cron') set.add(g.profile);
  return [...set];
}

function renderFilters() {
  const r = state.route; let html = '';
  const profiles = knownProfiles();
  if (!r.group && profiles.length > 1) {
    // Profile chips: the chips row only appears once a second profile exists.
    html += `<button class="chip" type="button" data-profile="" aria-pressed="${!state.profile}" title="All profiles">All</button>`
      + profiles.filter((p) => p !== 'default').sort().map((p) =>
        `<button class="chip" type="button" data-profile="${esc(p)}" aria-pressed="${state.profile === p}">${esc(p)}</button>`).join('');
  }
  if (r.source === 'artifact') {
    html += FTYPES.map(([k, l]) => `<button class="chip" type="button" data-ftype="${k}" aria-pressed="${state.ftype === k}">${l}</button>`).join('');
  } else if (!r.group && ['inbox', 'all'].includes(r.view) && r.source !== 'note') {
    html += `<button class="chip" type="button" data-noise aria-pressed="${state.noise}" title="Skipped, silent and same-as-last-time runs">Show quiet runs</button>`;
  }
  $('#filters').innerHTML = html;
}
function typeLabel(mime, name) {
  const ext = (name.split('.').pop() || '').toUpperCase();
  if (mime === 'text/markdown') return 'Markdown';
  if (mime === 'application/pdf') return 'PDF';
  if (mime === 'text/html') return 'HTML';
  if (mime.startsWith('image/')) return 'Image';
  if (mime.startsWith('audio/')) return 'Audio';
  if (mime.startsWith('video/')) return 'Video';
  return ext.length <= 5 ? ext : 'File';
}
function snoozeText(ts) {
  const d = new Date(ts * 1000);
  const sameDay = dayKey(ts) === dayKey(Date.now() / 1000);
  return d.toLocaleString(undefined, sameDay ? { hour: 'numeric', minute: '2-digit' }
    : { weekday: 'short', hour: 'numeric', minute: '2-digit' });
}
function isSnoozed(it) { return !!it.snoozed_until && it.snoozed_until * 1000 > Date.now(); }
function isoTs(s) { const t = Date.parse(s); return Number.isFinite(t) ? t / 1000 : null; }
function rowHtml(it) {
  const src = SOURCE_META[it.source] || { icon: 'note' };
  const tags = [];
  if (it.attention) tags.push(`<span class="tag warn">${icon('alert')}Needs attention</span>`);
  const st = STATUS_TAG[it.status];
  if (st && !(it.attention && st[0] === 'warn')) tags.push(`<span class="tag ${st[0]}">${st[1]}</span>`);
  if (isSnoozed(it)) tags.push(`<span class="tag">${icon('clock')}Snoozed until ${esc(snoozeText(it.snoozed_until))}</span>`);
  if (it.source === 'artifact' && it.extra.mime) tags.push(`<span class="tag">${esc(typeLabel(it.extra.mime, it.title))}</span>`);
  if (it.profile && it.profile !== 'default') tags.push(`<span class="tag accent">${esc(it.profile)}</span>`);
  const cls = ['row', it.read ? '' : 'unread', it.noise ? 'noise' : ''].join(' ');
  return `<div class="swipe" data-id="${it.id}">
    <div class="swipe-bg left" aria-hidden="true"><span>${icon('mail')}${it.read ? 'Unread' : 'Read'}</span></div>
    <div class="swipe-bg right" aria-hidden="true"><span>${it.archived ? 'Inbox' : 'Archive'}${icon(it.archived ? 'inbox' : 'archive')}</span></div>
    <div class="${cls}" role="option" data-id="${it.id}" aria-selected="${state.selected === it.id}" tabindex="-1">
      <span class="unread-dot"></span>
      <div class="from">${icon(src.icon)}<span>${esc(it.group_label || src.label)}</span></div>
      <div class="time">${it.starred ? `<span class="starred" aria-label="Starred">${icon('star')}</span>` : ''}${shortTime(it.ts)}</div>
      <div class="subject">${esc(it.title)}</div>
      ${it.summary ? `<div class="preview">${esc(it.summary)}</div>` : ''}
      <div class="tags">${tags.join('')}</div>
    </div>
  </div>`;
}
function renderCount() {
  const total = state.total.toLocaleString();
  $('#list-count').textContent = !state.total ? '' : state.unread > 0
    ? (isNarrow() ? `${state.unread.toLocaleString()} unread` : `${state.unread.toLocaleString()} unread of ${total}`)
    : `${total} item${state.total === 1 ? '' : 's'}`;
}
function renderList() {
  const el = $('#list');
  renderCount();
  if (!state.items.length) {
    const r = state.route;
    const msg = state.q ? ['Nothing matches that search', 'Try fewer words, or search from Everything.']
      : r.view === 'inbox' ? ['Inbox zero', 'New cron results, chat artifacts and kanban updates land here.']
      : r.view === 'attention' ? ['Nothing needs you', 'Failed runs, blocked tasks and alerts show up here.']
      : r.view === 'starred' ? ['No starred items', 'Star anything you want to keep handy.']
      : r.view === 'snoozed' ? ['Nothing snoozed', 'Snooze an item to keep it out of the inbox until you want it.']
      : ['Nothing here yet', ''];
    el.innerHTML = `<div class="list-msg"><strong>${msg[0]}</strong>${msg[1]}</div>`;
    return;
  }
  let html = ''; let lastDay = null;
  for (const it of state.items) {
    const k = dayKey(it.ts);
    if (k !== lastDay) { html += `<div class="day">${dayLabel(k)}</div>`; lastDay = k; }
    html += rowHtml(it);
  }
  if (state.next) html += '<button class="btn more" type="button" id="btn-more">Load older</button>';
  el.innerHTML = html;
}
function updateRow(it) {
  const old = $(`.swipe[data-id="${it.id}"]`);
  if (old) old.outerHTML = rowHtml(it);
}
function markSelected() {
  $$('.row').forEach((r) => r.setAttribute('aria-selected', String(Number(r.dataset.id) === state.selected)));
}
function removeFromList(id) {
  const idx = state.items.findIndex((x) => x.id === id);
  if (idx < 0) return null;
  const [it] = state.items.splice(idx, 1);
  state.total = Math.max(0, state.total - 1);
  if (!it.read) state.unread = Math.max(0, state.unread - 1);
  renderList();
  return { it, idx };
}
function restoreToList(entry) {
  state.items.splice(entry.idx, 0, entry.it);
  state.total += 1;
  if (!entry.it.read) state.unread += 1;
  renderList();
}

/* ── reader ──────────────────────────────────────── */
function showEmpty() {
  state.selected = null;
  $('#item').hidden = true; $('#item')._item = null; $('#reader-empty').hidden = false;
  markSelected();
}
let openSeq = 0;
async function openItem(id, push = true) {
  if (push) { location.hash = routeHash(state.route, id); return; }
  const seq = ++openSeq;
  state.selected = id;
  markSelected();
  if (!isNarrow()) $(`.row[data-id="${id}"]`)?.scrollIntoView({ block: 'nearest' });
  let it;
  try { it = await api(`/api/items/${id}`); } catch (e) { toast(e.message); return; }
  if (seq !== openSeq) return;
  if (!it.read) setFlag(it, 'read', true, { quiet: true }); // flips it.read synchronously, saves in the background
  renderItem(it);
}
function factRows(rows) {
  return '<dl class="panel facts">' + rows.filter(([, v]) => v).map(([k, v]) => `<dt>${k}</dt><dd>${v}</dd>`).join('') + '</dl>';
}
function barButton(act, iconName, label, extra = '', cls = '') {
  return `<button class="icon-btn ${cls}" type="button" data-act="${act}" aria-label="${esc(label)}" title="${esc(label)}" ${extra}>${icon(iconName)}<span class="lbl">${esc(label.split(' (')[0])}</span></button>`;
}
function itemBar(it) {
  const idx = state.items.findIndex((x) => x.id === it.id);
  const hasNewer = idx > 0; const hasOlder = idx >= 0 && (idx < state.items.length - 1 || !!state.next);
  const snz = isSnoozed(it);
  return `<div class="item-bar" role="toolbar" aria-label="Item actions">
    ${barButton('back', 'back', 'Back', '', 'only-narrow')}
    ${barButton('star', 'star', it.starred ? 'Starred (s)' : 'Star (s)', `aria-pressed="${!!it.starred}"`)}
    <span class="snooze-wrap">
      ${barButton('snooze', 'clock', snz ? `Snoozed until ${snoozeText(it.snoozed_until)}` : 'Snooze (b)',
        `aria-pressed="${snz}" aria-haspopup="true" aria-expanded="false"`)}
      <div class="menu snooze-menu" role="menu" hidden>
        <button type="button" role="menuitem" data-snooze="1h">In 1 hour</button>
        <button type="button" role="menuitem" data-snooze="tomorrow">Tomorrow 9am</button>
        <button type="button" role="menuitem" data-snooze="week">In a week</button>
        ${snz ? '<button type="button" role="menuitem" data-snooze="clear">Bring back now</button>' : ''}
      </div>
    </span>
    ${barButton('unread', 'mail', it.read ? 'Unread (u)' : 'Read (u)')}
    ${barButton('archive', it.archived ? 'inbox' : 'archive', it.archived ? 'To inbox (e)' : 'Archive (e)')}
    <span class="bar-sep"></span>
    ${barButton('newer', 'up', 'Newer (k)', hasNewer ? '' : 'disabled', 'only-wide')}
    ${barButton('older', 'down', isNarrow() ? 'Next' : 'Older (j)', hasOlder ? '' : 'disabled')}
  </div>`;
}
function renderItem(it) {
  const el = $('#item'); const x = it.extra || {};
  const src = SOURCE_META[it.source] || { label: it.source, icon: 'note' };
  const tags = [];
  if (it.attention) tags.push(`<span class="tag warn">${icon('alert')}Needs attention</span>`);
  const st = STATUS_TAG[it.status]; if (st && !(it.attention && st[0] === 'warn')) tags.push(`<span class="tag ${st[0]}">${st[1]}</span>`);
  if (it.profile && it.profile !== 'default') tags.push(`<span class="tag accent">${esc(it.profile)}</span>`);
  let body = '';

  if (it.source === 'cron') {
    body += it.body ? `<div class="md">${md(it.body)}</div>` : '<p class="muted">The run produced no output.</p>';
    if (it.diff && (it.diff.added_n || it.diff.removed_n)) {
      const dl = (cls, l) => `<div class="dl ${cls}">${esc(l)}</div>`;
      const hunk = (h) => (h.before || []).map((l) => dl('ctx', l)).join('')
        + (h.removed || []).map((l) => dl('del', l)).join('')
        + (h.added || []).map((l) => dl('add', l)).join('')
        + (h.after || []).map((l) => dl('ctx', l)).join('');
      body += `<div class="section-title">What changed since the previous run</div>`
        + `<div class="panel diff">${it.diff.hunks.map(hunk).join('')}`
        + (it.diff.more_hunks ? `<p class="muted note">… ${it.diff.more_hunks} more changed section${it.diff.more_hunks === 1 ? '' : 's'} not shown</p>` : '')
        + `</div>`;
    }
    if (x.script_output) body += `<details class="fold"><summary>Script output</summary><pre>${esc(x.script_output)}</pre></details>`;
    body += '<div class="section-title">Run details</div>' + factRows([
      ['Job', `<a href="${routeHash({ view: 'all', source: 'cron', group: it.group_key, profile: it.profile !== 'default' ? it.profile : '' })}">${esc(it.group_label)}</a> <code class="muted">${esc(x.job_id)}</code>`],
      ['Ran', esc(fullTime(it.ts))], ['Schedule', esc(x.schedule)], ['Mode', esc(x.mode)],
      ['Delivery', esc(x.deliver)], ['Profile', it.profile !== 'default' ? esc(it.profile) : ''],
      ['Output file', x.source_path ? `<code>${esc(x.source_path)}</code>` : ''],
      ['Previous run', it.prev_in_group ? `<a href="${routeHash(state.route, it.prev_in_group.id)}">${esc(fullTime(it.prev_in_group.ts))}</a> <span class="tag">${esc(STATUS_TAG[it.prev_in_group.status]?.[1] || 'OK')}</span>` : ''],
    ]);
    body += runUsage(x.usage, x.kind);
  } else if (it.source === 'artifact') {
    body += artifactPreview(it);
    const sess = (x.sessions || []).slice().reverse();
    body += '<div class="section-title">File</div>' + factRows([
      ['Path', `<code>${esc(x.path)}</code>`], ['Type', esc(x.mime)], ['Size', esc(bytes(x.size))],
      ['Modified', x.mtime ? esc(fullTime(x.mtime)) : ''],
      ['From', x.origin === 'kanban' ? `Kanban board <b>${esc(x.board)}</b>, task <code>${esc(x.task_id)}</code>` : ''],
    ]);
    if (sess.length) {
      body += `<div class="section-title">Shared in ${sess.length === 1 ? 'chat' : sess.length + ' chats'}</div><ul class="sessions">`
        + sess.map((s) => `<li><b>${esc(s.session_title)}</b><span class="muted">${esc(s.platform)} · ${esc(fullTime(s.ts))}</span></li>`).join('') + '</ul>';
    }
    if (it.body) body += `<div class="section-title">What the agent said</div><div class="md">${md(it.body)}</div>`;
  } else if (it.source === 'kanban') {
    body += `<div class="md">${md(it.body)}</div>` + '<div class="section-title">Task</div>' + factRows([
      ['Board', esc(it.group_label)], ['Task', `<code>${esc(x.task_id)}</code>`], ['Worker', esc(x.profile)],
      ['Task status now', esc(x.task_status)], ['Run', `#${esc(x.run_id)}`], ['Ended', esc(fullTime(it.ts))],
    ]);
  } else if (it.source === 'scheduler') {
    const jobLink = routeHash({ view: 'all', source: 'cron', group: it.group_key, profile: it.profile !== 'default' ? it.profile : '' });
    body += `<div class="md">${md(it.body)}</div>` + '<div class="section-title">Job</div>' + factRows([
      ['Job', `<a href="${jobLink}">${esc(it.group_label)}</a> <code class="muted">${esc(x.job_id)}</code>`],
      ['Since', esc(fullTime(it.ts))],
      ['Schedule', x.schedule ? `<code>${esc(x.schedule)}</code>` : ''],
      ['Last run', isoTs(x.last_run_at) ? `${esc(fullTime(isoTs(x.last_run_at)))} <span class="tag">${esc(x.last_status || 'unknown')}</span>` : ''],
      ['Failures in a row', x.failure_streak ? String(x.failure_streak) : ''],
      ['Delivery', typeof x.deliver === 'string' ? esc(x.deliver) : ''],
      ['Checked', x.checked_at ? esc(fullTime(x.checked_at)) : ''],
      ['Runs', `<a href="${jobLink}">Open this job's runs</a>`],
    ]);
  } else {
    body += `<div class="md">${md(it.body)}</div>`;
    if (x.link && /^https?:\/\//.test(x.link)) body += `<p><a class="btn" href="${esc(x.link)}" target="_blank" rel="noopener noreferrer">${icon('external')}Open link</a></p>`;
  }

  if (isSnoozed(it)) {
    body = `<div class="notice soft">Snoozed until ${esc(fullTime(it.snoozed_until))}. ` +
      `Use Snooze → Bring back now to return it to the inbox early.</div>` + body;
  }
  el.innerHTML = `${itemBar(it)}
    <h1 class="${it.source === 'artifact' ? 'filename' : ''}">${esc(it.title || it.group_label)}</h1>
    <div class="meta">
      <span class="from">${icon(src.icon)}${esc(it.group_label || src.label)}</span>
      <span>${esc(fullTime(it.ts))}</span>
      ${tags.join('')}
    </div>
    ${body}`;
  el.dataset.id = it.id;
  el._item = it;
  if (it.source === 'cron') {
    // The subject is the report's first line; don't print it twice. Agents sometimes
    // wrap the title into a longer first body line ("Title — more"), so also strip that.
    const first = el.querySelector('.md > :first-child');
    if (first && first.nextElementSibling) {
      const t = normText(it.title);
      const f = normText(first.textContent);
      if (t && f === t) first.remove();
      else if (t && f.startsWith(t) && it.title.length < 140 && !first.querySelector('*')) {
        const rest = first.textContent.trim().slice(String(it.title).trim().length);
        if (/^\s*[\u2014\u2013:;-]/.test(rest)) {
          const tail = rest.replace(/^[\s\u2014\u2013:;-]+/, '');
          if (tail) first.textContent = tail; else first.remove();
        }
      }
    }
  }
  el.hidden = false; $('#reader-empty').hidden = true;
  $('#reader').scrollTop = 0;
  if (it.source === 'artifact') hydrateTextPreview(it);
}
function artifactPreview(it) {
  const x = it.extra; const url = `/api/items/${it.id}/file`;
  if (!x.exists) return `<div class="notice">This file no longer exists at <code>${esc(x.path)}</code>. It was probably a scratch file that got cleaned up. The agent's message about it is below.</div>`;
  const actions = `<div class="preview-actions"><a class="btn" href="${url}" target="_blank" rel="noopener">${icon('external')}Open</a><a class="btn" href="${url}?download=1">${icon('download')}Download</a></div>`;
  const m = x.mime || '';
  let inner = '';
  if (m.startsWith('image/')) inner = `<a href="${url}" target="_blank" rel="noopener"><img src="${url}" alt="${esc(it.title)}"></a>`;
  else if (m === 'application/pdf') {
    // Mobile browsers don't render PDFs inside iframes; hand off to the native viewer instead.
    if (isNarrow()) return actions + '<div class="notice soft">PDFs open in your phone\'s viewer. Tap Open.</div>';
    inner = `<iframe src="${url}" title="${esc(it.title)}"></iframe>`;
  } else if (m === 'text/html' || m === 'application/xhtml+xml') inner = `<iframe src="${url}" sandbox="allow-scripts allow-popups allow-forms" title="${esc(it.title)}"></iframe>`;
  else if (m.startsWith('video/')) inner = `<video src="${url}" controls playsinline preload="metadata"></video>`;
  else if (m.startsWith('audio/')) inner = `<audio src="${url}" controls preload="metadata"></audio>`;
  else if (isTextual(m) && (x.size || 0) <= 2_000_000) inner = '<div class="md" data-text-preview><p class="muted">Loading preview…</p></div>';
  else return actions + `<div class="notice soft">No inline preview for ${esc(m || 'this file type')}. Use Open or Download.</div>`;
  return actions + `<div class="panel preview-frame">${inner}</div>`;
}
function isTextual(m) {
  return m.startsWith('text/') || ['application/json', 'application/xml', 'application/javascript', 'application/x-sh', 'application/toml', 'application/x-yaml'].includes(m);
}
async function hydrateTextPreview(it) {
  const slot = $('[data-text-preview]'); if (!slot) return;
  try {
    const res = await fetch(`/api/items/${it.id}/file?as=text`, { credentials: 'same-origin' });
    if (!res.ok) throw new Error(res.statusText);
    const text = await res.text();
    if ($('#item').dataset.id !== String(it.id)) return;
    if (it.extra.mime === 'text/markdown') slot.innerHTML = md(text);
    else slot.outerHTML = `<pre>${esc(text)}</pre>`;
  } catch (e) { slot.innerHTML = `<p class="muted">Preview failed: ${esc(e.message)}</p>`; }
}

/* ── usage, cost and storage ─────────────────────── */
const compact = new Intl.NumberFormat(undefined, { notation: 'compact', maximumFractionDigits: 1 });
function money(v) {
  if (v == null) return '—';
  if (v > 0 && v < 0.01) return '$' + v.toFixed(4);
  return '$' + v.toLocaleString(undefined, { minimumFractionDigits: 2, maximumFractionDigits: 2 });
}
function shortModel(m) { return (m || '').split('/').pop(); }
function duration(s) {
  if (s == null) return '';
  if (s < 90) return `${Math.round(s)}s`;
  if (s < 5400) return `${Math.round(s / 60)} min`;
  return `${(s / 3600).toFixed(1)} h`;
}
function renderGroupUsage(gu) {
  const el = $('#group-usage');
  if (!gu || !gu.priced_runs) { el.hidden = true; el.textContent = ''; return; }
  el.hidden = false;
  el.textContent = `${money(gu.cost_usd)} over ${gu.priced_runs} priced run${gu.priced_runs === 1 ? '' : 's'} (${money(gu.cost_usd / gu.priced_runs)} each)`
    + (gu.models.length ? ` · ${gu.models.map(shortModel).join(', ')}` : '');
}
function runUsage(u, kind) {
  if (!u) {
    if (kind !== 'agent' && kind !== 'error') return '';
    return '<div class="section-title">Model &amp; cost</div><p class="muted note">No usage recorded for this run. '
      + 'Hermes keeps cron sessions for about a week, so runs indexed after their session was pruned have no cost data.</p>';
  }
  const status = u.cost_status === 'actual' ? '<span class="tag ok">billed</span>' : u.cost_status === 'estimated' ? '<span class="tag">estimated</span>' : '';
  const tokens = `${compact.format(u.input + u.cache_read + u.cache_write)} in`
    + (u.cache_read ? ` <span class="muted">(${compact.format(u.cache_read)} cached)</span>` : '')
    + ` · ${compact.format(u.output)} out` + (u.reasoning ? ` <span class="muted">(${compact.format(u.reasoning)} reasoning)</span>` : '');
  let html = '<div class="section-title">Model &amp; cost</div>' + factRows([
    ['Model', `<code>${esc(u.model || 'unknown')}</code>${u.provider ? ` <span class="muted">via ${esc(u.provider)}</span>` : ''}`],
    ['Cost', `<b>${money(u.cost_usd)}</b> ${status}`],
    ['Tokens', tokens],
    ['Duration', esc(duration(u.duration_s))],
    ['API calls', u.calls ? String(u.calls) : ''],
    ['Session', `<code>${esc(u.session_id)}</code>${u.sessions > 1 ? ` <span class="muted">+${u.sessions - 1} earlier after compression</span>` : ''}`],
  ]);
  if (u.models && u.models.length) {
    html += '<ul class="breakdown">' + u.models.map((m) => `<li><span><code>${esc(shortModel(m.model))}</code> <span class="muted">${esc(m.task)}</span></span>`
      + `<span class="muted">${m.calls} call${m.calls === 1 ? '' : 's'}</span><b>${money(m.cost)}</b></li>`).join('') + '</ul>';
  }
  return html;
}
const RANGES = [[7, '7 days'], [30, '30 days'], [90, '90 days'], [0, 'All time']];
let usageSeq = 0;
async function showUsage(days) {
  const seq = ++usageSeq;
  const el = $('#item');
  $('#app').classList.add('page-usage');
  state.selected = null; markSelected();
  el._item = null; el.dataset.id = '';
  el.hidden = false; $('#reader-empty').hidden = true;
  el.innerHTML = '<p class="muted">Loading usage…</p>';
  let u;
  try { u = await api(`/api/usage?days=${days}`); } catch (e) { el.innerHTML = `<p class="notice">Couldn't load usage: ${esc(e.message)}</p>`; return; }
  if (seq !== usageSeq) return;
  const span = days ? `in the last ${days} days` : "in total";
  const unpriced = u.runs - u.priced_runs;
  const maxDay = Math.max(0.000001, ...u.by_day.map((d) => d.cost));
  const st = u.storage;
  const pol = st.policy;
  const lp = st.last_prune;
  const srcNames = { cron: ['run', 'runs'], artifact: ['artifact', 'artifacts'], kanban: ['kanban run', 'kanban runs'], note: ['note', 'notes'] };
  const srcLabel = (k, v) => `${v.toLocaleString()} ${(srcNames[k] || [k, k])[v === 1 ? 0 : 1]}`;
  const jobLink = (j) => `#/group/cron/${encodeURIComponent(j.group_key)}${j.profile !== 'default' ? '/' + encodeURIComponent(j.profile) : ''}`;
  el.innerHTML = `
    <header class="page-head">
      <button class="icon-btn only-narrow" type="button" data-page-back aria-label="Back">${icon('back')}</button>
      <h1>Usage &amp; storage</h1>
    </header>
    <div class="filters page-filters">${RANGES.map(([d, l]) => `<a class="chip" href="#/usage/${d}" aria-pressed="${d === days}">${l}</a>`).join('')}</div>
    <section class="usage-hero">
      <div class="big">${money(u.cost_usd)}</div>
      <p>spent on <b>${u.runs.toLocaleString()}</b> scheduled run${u.runs === 1 ? "" : "s"} ${span}
        · ${compact.format(u.tokens_in)} tokens in, ${compact.format(u.tokens_out)} out</p>
      ${unpriced > 0 ? `<p class="muted note">${unpriced.toLocaleString()} of these runs have no cost data. Hermes keeps cron sessions for about a week, so costs exist only for runs indexed while their session did${u.first_priced_ts ? ` (from ${esc(fullTime(u.first_priced_ts))} on)` : ''}.</p>` : ''}
    </section>
    ${u.by_day.length > 1 ? `<div class="section-title">Cost per day</div>
    <div class="chart">
      <span class="chart-max muted">${money(maxDay)}</span><span class="chart-mid muted">${money(maxDay / 2)}</span>
      <div class="bars" role="img" aria-label="Daily cost, highest ${money(maxDay)} per day">${u.by_day.map((d) => `<span class="bar" style="--h:${Math.max(2, Math.round(d.cost / maxDay * 100))}%" title="${esc(d.day)}: ${money(d.cost)} over ${d.runs} runs"></span>`).join('')}</div>
    </div>
    <div class="bars-axis muted"><span>${esc(u.by_day[0].day)}</span><span>${esc(u.by_day[u.by_day.length - 1].day)}</span></div>` : ''}
    <div class="section-title">By job</div>
    ${u.by_job.length ? `<ul class="usage-list">${u.by_job.map((j) => `<li><a href="${jobLink(j)}">
        <span class="name">${esc(j.label)}${j.profile !== 'default' ? ` <span class="tag accent">${esc(j.profile)}</span>` : ''}</span>
        <span class="amount">${j.priced ? money(j.cost) : '—'}</span>
        <span class="sub muted">${j.runs} run${j.runs === 1 ? '' : 's'}${j.priced && j.priced < j.runs ? ` (${j.priced} priced)` : ''}${j.avg != null ? ` · ${money(j.avg)} per run` : ''}${j.models.length ? ` · ${esc(j.models.map(shortModel).join(', '))}` : ''}</span>
      </a></li>`).join('')}</ul>` : '<p class="muted">No scheduled runs in this window.</p>'}
    ${u.by_model.length ? `<div class="section-title">By model</div>
    <ul class="usage-list">${u.by_model.map((m) => `<li><div>
        <span class="name"><code>${esc(m.model)}</code></span>
        <span class="amount">${money(m.cost)}</span>
        <span class="sub muted">${m.runs} run${m.runs === 1 ? '' : 's'} · ${compact.format(m.tin)} in · ${compact.format(m.tout)} out</span>
      </div></li>`).join('')}</ul>` : ''}
    <div class="section-title">Storage</div>
    <div class="panel storage">
      <p><b>${bytes(st.db_bytes)}</b> · ${st.items.toLocaleString()} items
        (${Object.entries(st.by_source).map(([k, v]) => srcLabel(k, v)).join(', ')})
        · ${st.quiet_items.toLocaleString()} quiet · ${st.starred.toLocaleString()} starred</p>
      <p class="muted">${pol.quiet_days ? `Quiet runs (skipped, silent, unchanged) are deleted after ${pol.quiet_days} days.` : 'Quiet runs are kept forever.'}
        ${pol.keep_days ? `Everything else is deleted after ${pol.keep_days} days.` : 'Everything else is kept.'}
        Starred items are never deleted.</p>
      <p class="muted">${lp ? (lp.deleted ? `Last cleanup removed ${lp.deleted.toLocaleString()} item${lp.deleted === 1 ? '' : 's'} on ${esc(fullTime(lp.at))}.` : `Last cleanup on ${esc(fullTime(lp.at))} had nothing to remove.`) : 'No cleanup has removed anything yet. It runs hourly.'}</p>
      <div class="preview-actions">
        <a class="btn" href="/api/export?view=all&format=json">${icon('download')}Export all (JSON)</a>
        <a class="btn" href="/api/export?view=all&format=csv">${icon('download')}Export all (CSV)</a>
      </div>
    </div>`;
  $('#reader').scrollTop = 0;
}

/* ── export menu ─────────────────────────────────── */
function exportHref(fmt) {
  const p = listParams(null); p.delete('limit'); p.set('format', fmt);
  return '/api/export?' + p;
}
function toggleExportMenu(open) {
  const menu = $('#export-menu'); const btn = $('#btn-export');
  const show = open ?? menu.hidden;
  if (show) $$('#export-menu a').forEach((a) => { a.href = exportHref(a.dataset.fmt); });
  menu.hidden = !show; btn.setAttribute('aria-expanded', String(show));
  if (show) menu.querySelector('a').focus({ preventScroll: true });
}

/* ── actions ─────────────────────────────────────── */
async function setFlag(it, flag, value, { quiet = false } = {}) {
  const before = it[flag];
  it[flag] = value ? 1 : 0;
  const listItem = state.items.find((x) => x.id === it.id);
  if (listItem && listItem !== it) listItem[flag] = it[flag];
  if (listItem) {
    if (flag === 'read' && before !== it[flag]) { state.unread += it[flag] ? -1 : 1; renderCount(); }
    updateRow(listItem); markSelected();
  }
  try { await post(`/api/items/${it.id}`, { [flag]: !!value }); } catch (e) { toast(`Couldn't save: ${e.message}`); return false; }
  if (!quiet) toast({ starred: value ? 'Starred' : 'Unstarred', read: value ? 'Marked read' : 'Marked unread' }[flag]);
  loadSummary();
  return true;
}
function current() { const el = $('#item'); return !el.hidden ? el._item : null; }
function neighbour(id, delta) {
  const idx = state.items.findIndex((x) => x.id === id);
  return idx < 0 ? null : state.items[idx + delta] || null;
}
function archiveLeavesView(archiving) {
  const r = state.route;
  return (archiving && !['all', 'starred'].includes(r.view)) || (!archiving && r.view === 'archive');
}
async function archive(it, { fromReader = false } = {}) {
  const archiving = !it.archived;
  const next = neighbour(it.id, 1) || neighbour(it.id, -1);
  it.archived = archiving ? 1 : 0;
  let removed = null;
  if (archiveLeavesView(archiving)) removed = removeFromList(it.id);
  else { const li = state.items.find((x) => x.id === it.id); if (li) { li.archived = it.archived; updateRow(li); } }
  try { await post(`/api/items/${it.id}`, { archived: archiving }); } catch (e) {
    it.archived = archiving ? 0 : 1; if (removed) restoreToList(removed);
    toast(`Couldn't save: ${e.message}`); return;
  }
  loadSummary();
  toast(archiving ? 'Archived' : 'Moved to inbox', {
    label: 'Undo',
    run: async () => {
      it.archived = archiving ? 0 : 1;
      await post(`/api/items/${it.id}`, { archived: !archiving });
      if (removed) restoreToList(removed); else { const li = state.items.find((x) => x.id === it.id); if (li) { li.archived = it.archived; updateRow(li); } }
      markSelected(); loadSummary();
    },
  });
  if (fromReader) {
    if (!removed) { renderItem(it); return; }
    // Phones go back to the list (like a mail app); desktop moves on to the next item.
    if (isNarrow() || !next) location.hash = routeHash(state.route);
    else openItem(next.id);
  }
}
function snoozeTarget(kind) {
  const now = Date.now();
  if (kind === '1h') return now + 3600e3;
  if (kind === 'week') return now + 7 * 86400e3;
  const d = new Date(now); d.setDate(d.getDate() + 1); d.setHours(9, 0, 0, 0);
  return d.getTime();
}
function toggleSnoozeMenu(open) {
  const menu = $('.snooze-menu'); if (!menu) return;
  const show = open ?? menu.hidden;
  menu.hidden = !show;
  const btn = $('.item-bar [data-act="snooze"]');
  if (btn) btn.setAttribute('aria-expanded', String(show));
  if (show) menu.querySelector('button')?.focus({ preventScroll: true });
}
async function setSnooze(it, untilMs, { fromReader = false } = {}) {
  const until = untilMs ? untilMs / 1000 : null;
  const prev = isSnoozed(it) ? it.snoozed_until : null;
  // Snoozing hides an item from the inbox; unsnoozing hides it from the Snoozed view.
  const leaving = until ? !['all', 'snoozed'].includes(state.route.view) : state.route.view === 'snoozed';
  const next = neighbour(it.id, 1) || neighbour(it.id, -1);
  const put = (val) => {
    it.snoozed_until = val;
    const li = state.items.find((x) => x.id === it.id);
    if (li) { li.snoozed_until = val; updateRow(li); }
  };
  let removed = null;
  if (leaving) removed = removeFromList(it.id); else put(until);
  try { await post(`/api/items/${it.id}`, { snoozed_until: until }); } catch (e) {
    if (removed) restoreToList(removed); else put(prev);
    toast(`Couldn't save: ${e.message}`); return;
  }
  loadSummary();
  toast(until ? `Snoozed until ${snoozeText(until)}` : 'Back in the inbox', {
    label: 'Undo',
    run: async () => {
      await post(`/api/items/${it.id}`, { snoozed_until: prev });
      if (removed) { restoreToList(removed); put(prev); } else put(prev);
      markSelected(); loadSummary();
    },
  });
  if (fromReader) {
    if (!removed) { renderItem(it); return; }
    if (isNarrow() || !next) location.hash = routeHash(state.route);
    else openItem(next.id);
  }
}
async function act(name) {
  if (name === 'back') { history.length > 1 && location.hash.includes('/item/') ? history.back() : (location.hash = routeHash(state.route)); return; }
  const it = current(); if (!it) return;
  if (name === 'star') { await setFlag(it, 'starred', !it.starred); renderItem(it); }
  else if (name === 'snooze') toggleSnoozeMenu();
  else if (name === 'unread') {
    await setFlag(it, 'read', !it.read);
    if (!it.read && isNarrow()) location.hash = routeHash(state.route); else renderItem(it);
  } else if (name === 'archive') archive(it, { fromReader: true });
  else if (name === 'older' || name === 'newer') step(name === 'older' ? 1 : -1);
}
async function step(delta) {
  const n = state.selected ? neighbour(state.selected, delta) : state.items[0];
  if (n) { openItem(n.id); return; }
  if (delta > 0 && state.next) {
    await loadList(false);
    const m = neighbour(state.selected, 1); if (m) openItem(m.id);
  }
}
async function markAll() {
  if (!state.unread) { toast('Already all read'); return; }
  if (isNarrow() && !confirm(`Mark ${state.unread} item${state.unread === 1 ? '' : 's'} as read?`)) return;
  const r = state.route;
  const filter = { view: r.view, ...(r.source ? { source: r.source } : {}), ...(r.group ? { group: r.group, profile: r.profile || 'default' } : {}),
    ...(!r.group && state.profile ? { profile: state.profile } : {}),
    ...(state.q ? { q: state.q } : {}), ...(state.ftype ? { ftype: state.ftype } : {}), ...(state.noise ? { noise: '1' } : {}) };
  const res = await post('/api/bulk', { action: 'read', filter });
  state.items.forEach((x) => { x.read = 1; });
  state.unread = 0;
  renderList(); markSelected(); loadSummary();
  toast(res.changed ? `Marked ${res.changed} read` : 'Already all read');
}
let toastTimer;
function toast(msg, action = null) {
  const t = $('#toast');
  t.innerHTML = `<span>${esc(msg)}</span>` + (action ? `<button type="button">${esc(action.label)}</button>` : '');
  t.classList.toggle('has-action', !!action);
  if (action) t.querySelector('button').onclick = () => { t.classList.remove('show'); action.run(); };
  t.classList.add('show');
  clearTimeout(toastTimer); toastTimer = setTimeout(() => t.classList.remove('show'), action ? 4500 : 1800);
}

/* ── swipe gestures (touch only) ─────────────────── */
// Swipe right toggles read, swipe left archives. Vertical scrolling is untouched (touch-action: pan-y).
const SWIPE_ARM = 88;
let swipe = null;
let suppressClick = false;
$('#list').addEventListener('pointerdown', (e) => {
  if (e.pointerType !== 'touch') return;
  const wrap = e.target.closest('.swipe'); if (!wrap) return;
  swipe = { wrap, row: wrap.querySelector('.row'), x0: e.clientX, y0: e.clientY, dx: 0, active: false, id: e.pointerId };
});
$('#list').addEventListener('pointermove', (e) => {
  if (!swipe || e.pointerId !== swipe.id) return;
  const dx = e.clientX - swipe.x0; const dy = e.clientY - swipe.y0;
  if (!swipe.active) {
    if (Math.abs(dy) > 10 && Math.abs(dy) > Math.abs(dx)) { swipe = null; return; }
    if (Math.abs(dx) < 12) return;
    swipe.active = true;
    swipe.wrap.classList.remove('settling');
  }
  swipe.dx = dx;
  swipe.row.style.transform = `translateX(${dx}px)`;
  swipe.wrap.classList.toggle('drag-right', dx > 0);
  swipe.wrap.classList.toggle('drag-left', dx < 0);
  swipe.wrap.classList.toggle('armed', Math.abs(dx) >= SWIPE_ARM);
});
function endSwipe(cancelled) {
  if (!swipe) return;
  const s = swipe; swipe = null;
  if (!s.active) return;
  suppressClick = true; setTimeout(() => { suppressClick = false; }, 350);
  const id = Number(s.wrap.dataset.id);
  const it = state.items.find((x) => x.id === id);
  const fire = !cancelled && it && Math.abs(s.dx) >= SWIPE_ARM;
  s.wrap.classList.add('settling');
  if (fire && s.dx < 0 && archiveLeavesView(!it.archived)) {
    s.row.style.transform = 'translateX(-110%)';
    setTimeout(() => archive(it), 180);
    return;
  }
  s.row.style.transform = '';
  setTimeout(() => s.wrap.classList.remove('drag-left', 'drag-right', 'armed', 'settling'), 230);
  if (!fire) return;
  if (s.dx > 0) setFlag(it, 'read', !it.read);
  else archive(it);
}
$('#list').addEventListener('pointerup', () => endSwipe(false));
$('#list').addEventListener('pointercancel', () => endSwipe(true));

/* ── events ──────────────────────────────────────── */
$('#list').addEventListener('click', (e) => {
  if (suppressClick) { e.preventDefault(); return; }
  if (e.target.closest('#btn-more')) { loadList(false); return; }
  const row = e.target.closest('.row'); if (row) openItem(Number(row.dataset.id));
});
$('#item').addEventListener('click', (e) => {
  const s = e.target.closest('[data-snooze]');
  if (s) {
    const it = current();
    if (it) setSnooze(it, s.dataset.snooze === 'clear' ? null : snoozeTarget(s.dataset.snooze), { fromReader: true });
    return;
  }
  const b = e.target.closest('[data-act]'); if (b && !b.disabled) act(b.dataset.act);
});
$('#filters').addEventListener('click', (e) => {
  const c = e.target.closest('.chip'); if (!c) return;
  if (c.hasAttribute('data-noise')) state.noise = !state.noise;
  else if (c.hasAttribute('data-profile')) state.profile = c.dataset.profile;
  else state.ftype = c.dataset.ftype;
  loadList(true);
});
$('#btn-markall').addEventListener('click', markAll);
$('#btn-export').addEventListener('click', (e) => { e.stopPropagation(); toggleExportMenu(); });
$('#export-menu').addEventListener('click', () => toggleExportMenu(false));
document.addEventListener('click', (e) => {
  if (!e.target.closest('.menu-wrap')) toggleExportMenu(false);
  if (!e.target.closest('.snooze-wrap')) toggleSnoozeMenu(false);
});
$('#item').addEventListener('click', (e) => { if (e.target.closest('[data-page-back]')) location.hash = routeHash(state.route); });
$('#jobs-toggle').addEventListener('click', () => { state.showAllJobs = !state.showAllJobs; renderNav(); });
$('#btn-menu').addEventListener('click', openDrawer);
$('#btn-drawer-close').addEventListener('click', closeDrawer);
$('#scrim').addEventListener('click', closeDrawer);
$('#sidebar').addEventListener('click', (e) => { if (e.target.closest('a[href^="#"]')) closeDrawer(); });
function syncThemeButton() {
  const next = window.hermesTheme.get() === 'dark' ? 'light' : 'dark';
  $('#btn-theme').title = `Switch to ${next} theme`;
  $('#btn-theme').setAttribute('aria-label', `Switch to ${next} theme`);
}
$('#btn-theme').addEventListener('click', () => { window.hermesTheme.toggle(); syncThemeButton(); });
syncThemeButton();
$('#btn-reindex').addEventListener('click', async (e) => {
  const b = e.currentTarget; if (b.classList.contains('spin')) return;
  b.classList.add('spin');
  try {
    const res = await post('/api/reindex', {});
    const n = Object.values(res.new).reduce((a, c) => a + c, 0);
    await loadSummary(); await loadList(true); markSelected();
    toast(n ? `${n} new item${n === 1 ? '' : 's'}` : 'Nothing new');
  } catch (err) { toast(err.message); } finally { b.classList.remove('spin'); }
});
let searchTimer;
function setSearch(v) { $('#search-box').classList.toggle('has-text', !!v); }
$('#search').addEventListener('input', (e) => {
  setSearch(e.target.value);
  clearTimeout(searchTimer);
  searchTimer = setTimeout(() => { state.q = e.target.value.trim(); loadList(true); }, 250);
});
$('#search').addEventListener('keydown', (e) => {
  if (e.key === 'Enter') e.target.blur(); // closes the phone keyboard so results are visible
  if (e.key === 'Escape') { clearSearch(); e.target.blur(); }
});
function clearSearch() { $('#search').value = ''; setSearch(''); if (state.q) { state.q = ''; loadList(true); } }
$('#search-clear').addEventListener('click', (e) => { e.preventDefault(); clearSearch(); $('#search').focus(); });

document.addEventListener('keydown', (e) => {
  if (e.target.matches('input, textarea') || e.metaKey || e.ctrlKey || e.altKey) return;
  const k = e.key;
  if (k === 'Escape' && $('#app').classList.contains('nav-open')) { closeDrawer(); return; }
  if (k === 'Escape' && !$('#export-menu').hidden) { toggleExportMenu(false); $('#btn-export').focus(); return; }
  if (k === 'Escape' && $('.snooze-menu') && !$('.snooze-menu').hidden) { toggleSnoozeMenu(false); return; }
  if (state.page === 'usage') { if (k === 'Escape') location.hash = routeHash(state.route); return; }
  if (k === '/') { e.preventDefault(); $('#search').focus(); return; }
  if (k === 'j' || k === 'k') { e.preventDefault(); step(k === 'j' ? 1 : -1); return; }
  if (k === 'e' || k === '#') act('archive');
  else if (k === 's') act('star');
  else if (k === 'u') act('unread');
  else if (k === 'b') act('snooze');
  else if (k === 'I' && e.shiftKey) markAll();
  else if (k === 'Escape') act('back');
  else if (k === 'g') { location.hash = '#/view/inbox'; }
});
window.addEventListener('hashchange', onRoute);
narrowMQ.addEventListener('change', () => { const it = current(); if (it) renderItem(it); });

/* ── live refresh ────────────────────────────────── */
// /api/pulse fingerprints the sources and re-indexes only when one changed, so new
// items appear within seconds. The 60s background pass stays as a backstop.
let pulseBusy = false;
async function pulse() {
  if (pulseBusy) return;
  pulseBusy = true;
  try {
    const res = await api('/api/pulse');
    if (!res.changed) return;
    state.summary = res.summary;
    renderNav();
    scheduleSnoozeRefresh();
    if (state.page === 'usage' || state.q) return;
    // Only refresh the list when it is visible and scrolled to the top, so nothing jumps under a finger.
    const listVisible = !isNarrow() || $('#app').dataset.pane === 'list';
    if (listVisible && $('#list').scrollTop < 40 && !swipe) { await loadList(true); markSelected(); }
  } catch (e) { /* server unreachable; the next tick retries */ }
  finally { pulseBusy = false; }
}
document.addEventListener('visibilitychange', () => { if (!document.hidden) pulse(); });
setInterval(() => { if (!document.hidden) pulse(); }, 5000);

(async function init() {
  await loadSummary();
  await onRoute();
})();
