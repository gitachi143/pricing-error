/* Shared helpers for every page. */
const $  = (s, r = document) => r.querySelector(s);
const $$ = (s, r = document) => [...r.querySelectorAll(s)];

async function api(path, opts = {}) {
  const r = await fetch('/api' + path, {
    headers: { 'Content-Type': 'application/json' }, credentials: 'same-origin', ...opts,
    body: opts.body && typeof opts.body !== 'string' ? JSON.stringify(opts.body) : opts.body,
  });
  if (r.status === 401) { location.href = '/login'; throw new Error('unauthorised'); }
  const txt = await r.text();
  let data; try { data = txt ? JSON.parse(txt) : null; } catch { data = txt; }
  if (!r.ok) throw new Error((data && (data.detail?.errors?.join('; ') || data.detail)) || r.statusText);
  return data;
}

const money = n => (n === null || n === undefined || n === '') ? '—'
  : '$' + Number(n).toLocaleString(undefined, { minimumFractionDigits: 2, maximumFractionDigits: 2 });
const money0 = n => (n == null) ? '—'
  : '$' + Number(n).toLocaleString(undefined, { maximumFractionDigits: 0 });
const pct = n => (n === null || n === undefined) ? '—' : Number(n).toFixed(0) + '%';
const num = n => (n == null) ? '—' : Number(n).toLocaleString();

function ago(ts) {
  if (!ts) return '—';
  const s = Date.now() / 1000 - ts;
  if (s < 60) return Math.max(0, Math.round(s)) + 's ago';
  if (s < 3600) return Math.round(s / 60) + 'm ago';
  if (s < 86400) return Math.round(s / 3600) + 'h ago';
  return Math.round(s / 86400) + 'd ago';
}
const clock = ts => ts ? new Date(ts * 1000).toLocaleTimeString([], { hour12: false }) : '';
const dt = ts => ts ? new Date(ts * 1000).toLocaleString([], { month: 'short', day: 'numeric', hour: '2-digit', minute: '2-digit' }) : '—';
const esc = s => String(s ?? '').replace(/[&<>"']/g, c => ({ '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' }[c]));
const chip = (v, cls) => `<span class="chip ${cls ?? v}"><span class="g"></span>${esc(String(v ?? '').replace(/_/g, ' '))}</span>`;

/* A spoofed domain renders identically to the real one — that's the attack.
   Flag any host containing non-ASCII so the table doesn't lie to you. */
function host(m) {
  if (!m) return '';
  if (!/[^\x00-\x7F]/.test(m)) return esc(m);
  const marked = [...m].map(c => c.charCodeAt(0) > 127
    ? `<u style="color:var(--critical);text-decoration:underline wavy" title="U+${c.charCodeAt(0).toString(16).toUpperCase().padStart(4,'0')} — not a Latin letter">${esc(c)}</u>`
    : esc(c)).join('');
  return `${marked} <span class="chip err" style="margin-left:4px"><span class="g"></span>lookalike</span>`;
}

function toast(msg, bad = false) {
  const t = document.createElement('div');
  t.className = 'toast' + (bad ? ' bad' : '');
  t.textContent = msg;
  document.body.appendChild(t);
  setTimeout(() => t.remove(), 3200);
}

const NAV = [
  ['/', '◈', 'Overview'],
  ['/testbench', '⚑', 'Test bench'],
  ['/console', '▤', 'Console'],
  ['/inventory', '▣', 'Inventory'],
  ['/tasks', '◷', 'Tasks & worker'],
  ['/merchants', '⌂', 'Merchants'],
  ['/cards', '▦', 'Cards & limits'],
  ['/filters', '⚗', 'Buying filters'],
  ['/playbooks', '⟳', 'Checkout playbooks'],
  ['/trust', '⛊', 'Link trust'],
  ['/settings', '⚙', 'Settings'],
];

function shell(active) {
  const here = location.pathname.replace(/\/$/, '') || '/';
  document.body.insertAdjacentHTML('afterbegin', `
    <nav class="side">
      <div class="brand"><span class="dot" id="liveDot"></span><b>Deal Desk</b></div>
      ${NAV.map(([h, i, t]) => `<a href="${h}" class="${(active ?? here) === h ? 'on' : ''}">
         <span class="ic">${i}</span>${t}</a>`).join('')}
      <div class="sep"></div>
      <a href="#" id="themeBtn"><span class="ic">◑</span>Theme</a>
      <a href="#" id="logoutBtn"><span class="ic">⏻</span>Sign out</a>
      <div class="foot" id="navFoot"></div>
    </nav>`);
  const main = $('main'); if (main) main.parentNode.classList?.add?.('shell');
  $('#themeBtn').onclick = e => {
    e.preventDefault();
    const cur = document.documentElement.dataset.theme;
    const next = cur === 'dark' ? 'light' : cur === 'light' ? '' : 'dark';
    if (next) { document.documentElement.dataset.theme = next; localStorage.theme = next; }
    else { delete document.documentElement.dataset.theme; delete localStorage.theme; }
  };
  $('#logoutBtn').onclick = async e => { e.preventDefault(); await api('/logout', { method: 'POST' }); location.href = '/login'; };
  api('/worker/status').then(s => {
    $('#navFoot').innerHTML = `worker ${s.online ? 'online' : 'offline'}`;
    $('#liveDot').style.background = s.online ? 'var(--good)' : 'var(--muted)';
  }).catch(() => {});
}

if (localStorage.theme) document.documentElement.dataset.theme = localStorage.theme;

/* poll helper: fn() every ms, pause when tab is hidden */
function poll(fn, ms) {
  let t;
  const run = async () => { if (!document.hidden) { try { await fn(); } catch (e) { } } t = setTimeout(run, ms); };
  run();
  return () => clearTimeout(t);
}
