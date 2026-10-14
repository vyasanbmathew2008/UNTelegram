'use strict';

const $ = (s, r = document) => r.querySelector(s);
const $$ = (s, r = document) => [...r.querySelectorAll(s)];
const COLORS = ['#e17076', '#faa774', '#a695e7', '#7bc862', '#6ec9cb', '#65aadd', '#ee7aae'];
const ICON = {
  lock: '<svg class="lock" viewBox="0 0 24 24" aria-hidden="true"><path d="M18 8h-1V6a5 5 0 0 0-10 0v2H6a2 2 0 0 0-2 2v10a2 2 0 0 0 2 2h12a2 2 0 0 0 2-2V10a2 2 0 0 0-2-2ZM9 6a3 3 0 0 1 6 0v2H9V6Z"/></svg>',
  dl: '<svg viewBox="0 0 24 24" aria-hidden="true"><path d="M5 20h14v-2H5v2ZM19 9h-4V3H9v6H5l7 7 7-7Z"/></svg>',
  info: '<svg viewBox="0 0 24 24" aria-hidden="true"><path d="M11 7h2v2h-2V7Zm0 4h2v6h-2v-6Zm1-9a10 10 0 1 0 0 20 10 10 0 0 0 0-20Zm0 18a8 8 0 1 1 0-16 8 8 0 0 1 0 16Z"/></svg>',
};

const state = {
  dialogs: [], chat: null, oldest: null, end: true, loading: false,
  selectMode: false, sel: new Set(), jobs: [], inChatQuery: '', pollTimer: null,
};

/* ---------- helpers ---------- */
const esc = s => String(s).replace(/[&<>"']/g, c => ({ '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' }[c]));
const linkify = s => esc(s).replace(/(https?:\/\/[^\s<]+)/g, '<a href="$1" target="_blank" rel="noopener noreferrer">$1</a>');
const initials = n => (n.trim().split(/\s+/).slice(0, 2).map(w => w[0]).join('') || '?').toUpperCase();
const hm = d => new Date(d).toLocaleTimeString([], { hour: '2-digit', minute: '2-digit' });
const color = id => COLORS[Math.abs(id) % COLORS.length];

function fmtSize(n) {
  if (!n && n !== 0) return '';
  const u = ['B', 'KB', 'MB', 'GB'];
  let i = 0;
  while (n >= 1024 && i < 3) { n /= 1024; i++; }
  return n.toFixed(i ? 1 : 0) + ' ' + u[i];
}
function listTime(d) {
  if (!d) return '';
  const t = new Date(d), now = new Date();
  if (t.toDateString() === now.toDateString()) return hm(d);
  if (now - t < 6 * 864e5) return t.toLocaleDateString([], { weekday: 'short' });
  return t.toLocaleDateString([], { day: 'numeric', month: 'short' });
}
function dayLabel(d) {
  const t = new Date(d), now = new Date();
  if (t.toDateString() === now.toDateString()) return 'Today';
  return t.toLocaleDateString([], { day: 'numeric', month: 'long', year: t.getFullYear() === now.getFullYear() ? undefined : 'numeric' });
}
function avatarHTML(id, name) {
  return `<span>${esc(initials(name))}</span><img src="/api/avatar/${id}" alt="" loading="lazy" data-hide-on-error>`;
}
// CSP blocks inline handlers, so broken images are handled by one capturing listener.
document.addEventListener('error', e => {
  const t = e.target;
  if (t.tagName === 'IMG') {
    if (t.hasAttribute('data-hide-on-error')) t.remove(); else t.style.display = 'none';
  }
}, true);

let toastTimer;
function toast(msg) {
  const t = $('#toast');
  t.textContent = msg; t.classList.remove('hidden');
  clearTimeout(toastTimer);
  toastTimer = setTimeout(() => t.classList.add('hidden'), 4500);
}

async function api(path, { method = 'GET', body } = {}) {
  const opts = { method, headers: {}, credentials: 'same-origin' };
  if (method !== 'GET') opts.headers['X-TG-Web'] = '1';
  if (body !== undefined) { opts.headers['Content-Type'] = 'application/json'; opts.body = JSON.stringify(body); }
  const r = await fetch(path, opts);
  let j = {};
  try { j = await r.json(); } catch { /* non-JSON body */ }
  if (r.status === 401 && !path.startsWith('/api/auth/')) { showLogin(); throw new Error('auth'); }
  if (!r.ok) {
    const d = j.detail;
    const msg = typeof d === 'string' ? d : Array.isArray(d) ? d.map(x => x.msg).join('; ') : r.statusText;
    throw new Error(msg.replace(/^Value error, /, ''));
  }
  return j;
}

/* ---------- login ---------- */
function step(which) {
  ['phone', 'code', 'pass'].forEach(k => $('#f-' + k).classList.toggle('hidden', k !== which));
  $('#l-title').textContent = { phone: 'Sign in to Telegram', code: 'Enter code', pass: 'Enter your password' }[which];
  $('#l-sub').textContent = {
    phone: 'Enter your phone number with country code.',
    code: 'We sent a code to your Telegram app on another device (or by SMS).',
    pass: 'Your account has two-step verification turned on.',
  }[which];
  const input = $('#f-' + which + ' input'); if (input) input.focus();
}
function showLogin() {
  stopPolling();
  $('#app').classList.add('hidden'); $('#login').classList.remove('hidden'); step('phone');
}
function bindForm(id, errId, fn) {
  $(id).addEventListener('submit', async e => {
    e.preventDefault();
    const btn = $('button[type=submit]', e.target), err = $(errId);
    err.textContent = ''; btn.disabled = true;
    try { await fn(); } catch (x) { if (x.message !== 'auth') err.textContent = x.message; }
    btn.disabled = false;
  });
}
bindForm('#f-phone', '#e-phone', async () => { await api('/api/auth/send_code', { method: 'POST', body: { phone: $('#phone').value } }); step('code'); });
bindForm('#f-code', '#e-code', async () => {
  const r = await api('/api/auth/verify_code', { method: 'POST', body: { code: $('#code').value } });
  $('#code').value = '';
  r.need_password ? step('pass') : boot();
});
bindForm('#f-pass', '#e-pass', async () => {
  await api('/api/auth/verify_password', { method: 'POST', body: { password: $('#pass').value } });
  $('#pass').value = ''; boot();
});
$('#wrong').addEventListener('click', () => step('phone'));
$('#btn-logout').addEventListener('click', async () => {
  if (!confirm('Log out? This ends the Telegram session stored on the server.')) return;
  await api('/api/auth/logout', { method: 'POST', body: {} }).catch(() => {});
  location.reload();
});

/* ---------- chat list ---------- */
function renderChats() {
  const q = $('#q').value.trim().toLowerCase();
  const list = state.dialogs.filter(d => d.name.toLowerCase().includes(q));
  $('#chats').innerHTML = list.map(d => `
    <div class="chat ${state.chat && state.chat.id === d.id ? 'active' : ''}" data-id="${d.id}" tabindex="0" role="button">
      <div class="avatar" style="background:${color(d.id)}">${avatarHTML(d.id, d.name)}</div>
      <div class="chat-main">
        <div class="row"><div class="name">${d.protected ? ICON.lock : ''}${esc(d.name)}</div><div class="time">${listTime(d.date)}</div></div>
        <div class="row"><div class="sub">${esc(d.last || '')}</div>${d.unread ? `<div class="badge">${d.unread}</div>` : ''}</div>
      </div>
    </div>`).join('') || '<p class="sub" style="padding:24px;text-align:center">No chats found</p>';
}
$('#q').addEventListener('input', renderChats);
$('#chats').addEventListener('click', e => { const c = e.target.closest('.chat'); if (c) openChat(+c.dataset.id); });
$('#chats').addEventListener('keydown', e => { if (e.key === 'Enter') { const c = e.target.closest('.chat'); if (c) openChat(+c.dataset.id); } });
$('#back').addEventListener('click', () => document.body.classList.remove('chat-open'));

/* ---------- message rendering ---------- */
function mediaHTML(m) {
  const md = m.media; if (!md) return '';
  const locked = m.protected;
  const id = `${state.chat.id}/${m.id}`;
  let h = '';
  if (['photo', 'sticker', 'gif', 'video'].includes(md.kind)) {
    const th = `<img class="photo" src="/api/thumb/${id}" alt="" loading="lazy">`;
    if (md.kind === 'video' && !locked) h += `<video class="photo" controls preload="none" poster="/api/thumb/${id}" src="/api/media/${id}"></video>`;
    else if (md.kind === 'photo' && !locked) h += `<a href="/api/media/${id}" target="_blank" rel="noopener">${th}</a>`;
    else h += th;
  }
  if ((md.kind === 'audio' || md.kind === 'voice') && !locked) h += `<audio controls preload="none" src="/api/media/${id}"></audio>`;
  if (md.kind === 'photo') {
    h += locked ? `<div class="file-size">${ICON.lock}Saving is turned off</div>`
      : `<a class="dl-link" href="/api/media/${id}?download=1" download>Save photo</a>`;
  } else {
    h += `<div class="file">
      ${locked ? `<span class="file-ic off" title="Saving is turned off">${ICON.lock}</span>`
        : `<a class="file-ic" href="/api/media/${id}?download=1" download title="Download">${ICON.dl}</a>`}
      <div><div class="file-name">${esc(md.name)}</div>
      <div class="file-size">${fmtSize(md.size)}${locked ? ' · saving restricted' : ''}</div></div></div>`;
  }
  return h;
}
function msgHTML(m) {
  if (m.service) return `<div class="svc">${esc(m.service.replace(/([a-z])([A-Z])/g, '$1 $2'))}</div>`;
  const hasMedia = !!m.media;
  const showWho = !m.out && m.sender && state.chat.type !== 'user';
  const sel = state.sel.has(m.id) ? ' selected' : '';
  return `<div class="msg ${m.out ? 'out' : ''}${m.protected ? ' locked' : ''}${sel}" data-mid="${m.id}" data-media="${hasMedia ? 1 : 0}" data-locked="${m.protected ? 1 : 0}">
    ${showWho ? `<div class="who">${esc(m.sender)}</div>` : ''}
    ${mediaHTML(m)}${m.text ? `<div class="text">${linkify(m.text)}</div>` : ''}
    <span class="meta">${hasMedia ? `<button class="mi" data-act="inspect" title="Details" aria-label="Message details">${ICON.info}</button>` : ''}${hm(m.date)}</span>
  </div>`;
}
function renderBatch(msgs) {
  let html = '', last = null;
  for (const m of msgs) {
    const day = new Date(m.date).toDateString();
    if (day !== last) { html += `<div class="day">${dayLabel(m.date)}</div>`; last = day; }
    html += msgHTML(m);
  }
  return html;
}
const moreBtn = '<button class="more" id="more">Load older messages</button>';

/* ---------- open a chat ---------- */
async function openChat(id, { focus = 0, query = '' } = {}) {
  const chat = state.dialogs.find(d => d.id === id);
  if (!chat) return;
  state.chat = chat; state.inChatQuery = query;
  exitSelectMode();
  document.body.classList.add('chat-open'); renderChats();
  $('#empty').classList.add('hidden'); $('#top').classList.remove('hidden'); $('#msgs').classList.remove('hidden');
  $('#top-name').innerHTML = (chat.protected ? ICON.lock : '') + esc(chat.name);
  $('#top-sub').textContent = { user: 'private chat', bot: 'bot', group: 'group', channel: 'channel' }[chat.type] + (chat.protected ? ' · saving restricted' : '');
  const av = $('#top-av'); av.style.background = color(chat.id); av.innerHTML = avatarHTML(chat.id, chat.name);
  $('#prot-note').classList.toggle('hidden', !chat.protected);
  $('#btn-all').classList.toggle('hidden', chat.protected);
  $('#btn-select').classList.toggle('hidden', chat.protected);
  $('#inner').innerHTML = '<div class="svc">Loading…</div>';
  try {
    const qs = new URLSearchParams();
    if (focus) qs.set('around', focus);
    if (query) qs.set('q', query);
    const r = await api(`/api/messages/${id}?${qs}`);
    if (!state.chat || state.chat.id !== id) return;
    chat.protected = chat.protected || r.protected;
    state.oldest = r.messages.length ? r.messages[0].id : null; state.end = r.end;
    $('#inner').innerHTML = r.messages.length
      ? (r.end ? '' : moreBtn) + renderBatch(r.messages)
      : `<div class="svc">${query ? 'No results' : 'No messages'}</div>`;
    const target = focus && $(`.msg[data-mid="${focus}"]`);
    if (target) { target.scrollIntoView({ block: 'center' }); target.classList.add('flash'); }
    else $('#msgs').scrollTop = $('#msgs').scrollHeight;
  } catch (x) { if (x.message !== 'auth') $('#inner').innerHTML = `<div class="svc">${esc(x.message)}</div>`; }
}
$('#inner').addEventListener('click', async e => {
  if (e.target.closest('#more')) return loadOlder();
  const inspectBtn = e.target.closest('[data-act="inspect"]');
  const msgEl = e.target.closest('.msg');
  if (inspectBtn && msgEl) { e.stopPropagation(); return inspect(+msgEl.dataset.mid); }
  if (state.selectMode && msgEl && msgEl.dataset.media === '1') {
    e.preventDefault();
    if (msgEl.dataset.locked === '1') return toast('Saving is turned off for this message.');
    const mid = +msgEl.dataset.mid;
    state.sel.has(mid) ? state.sel.delete(mid) : state.sel.add(mid);
    msgEl.classList.toggle('selected', state.sel.has(mid));
    updateSelBar();
  }
});
async function loadOlder() {
  if (state.loading || !state.oldest || !state.chat) return;
  state.loading = true;
  const btn = $('#more'); btn.textContent = 'Loading…';
  const box = $('#msgs'), prev = box.scrollHeight;
  try {
    const qs = new URLSearchParams({ before: state.oldest });
    if (state.inChatQuery) qs.set('q', state.inChatQuery);
    const r = await api(`/api/messages/${state.chat.id}?${qs}`);
    if (r.messages.length) state.oldest = r.messages[0].id;
    btn.remove();
    $('#inner').insertAdjacentHTML('afterbegin', (r.end ? '' : moreBtn) + renderBatch(r.messages));
    box.scrollTop = box.scrollHeight - prev;
  } catch { btn.textContent = 'Try again'; }
  state.loading = false;
}

/* ---------- search inside chat ---------- */
$('#btn-csearch').addEventListener('click', () => {
  const box = $('#cq'); box.classList.toggle('hidden');
  if (!box.classList.contains('hidden')) box.focus();
  else if (state.inChatQuery) { box.value = ''; openChat(state.chat.id); }
});
$('#cq').addEventListener('keydown', e => {
  if (e.key === 'Enter' && state.chat) openChat(state.chat.id, { query: e.target.value.trim() });
});

/* ---------- message details (protected-content inspection) ---------- */
async function inspect(mid) {
  try {
    const r = await api(`/api/inspect/${state.chat.id}/${mid}`);
    const rows = [
      ['Chat', esc(r.chat.name)], ['Type', r.chat.type],
      ['Chat restricts saving', r.chat.saving_restricted ? 'Yes' : 'No'],
      ['Message restricts saving', r.message.saving_restricted ? 'Yes' : 'No'],
      ['Media', r.message.media_kind || 'none'],
      ['File', r.message.name ? esc(r.message.name) : '–'],
      ['Size', r.message.size ? fmtSize(r.message.size) : '–'],
      ['Type (MIME)', r.message.mime ? esc(r.message.mime) : '–'],
    ];
    if (r.chat.restrictions.length) rows.push(['Telegram notes', esc(r.chat.restrictions.join('; '))]);
    $('#info-body').innerHTML = `<dl>${rows.map(([k, v]) => `<dt>${k}</dt><dd>${v}</dd>`).join('')}</dl>
      <div class="verdict ${r.can_download ? 'ok' : 'no'}">${esc(r.reason)}</div>`;
    $('#dlg-info').showModal();
  } catch (x) { toast(x.message); }
}
$('#info-close').addEventListener('click', () => $('#dlg-info').close());

/* ---------- selection & batch download ---------- */
function updateSelBar() { $('#sel-count').textContent = `${state.sel.size} selected`; }
function enterSelectMode() {
  if (state.chat && state.chat.protected) return toast('Saving is turned off in this chat.');
  state.selectMode = true; state.sel.clear();
  $('#msgs').classList.add('sel-mode'); $('#selbar').classList.remove('hidden');
  $('#btn-select').classList.add('on'); updateSelBar(); refreshFolders();
}
function exitSelectMode() {
  state.selectMode = false; state.sel.clear();
  $('#msgs').classList.remove('sel-mode'); $('#selbar').classList.add('hidden');
  $('#btn-select').classList.remove('on');
  $$('.msg.selected').forEach(el => el.classList.remove('selected'));
}
$('#btn-select').addEventListener('click', () => state.selectMode ? exitSelectMode() : enterSelectMode());
$('#sel-clear').addEventListener('click', exitSelectMode);
$('#sel-go').addEventListener('click', async () => {
  if (!state.sel.size) return toast('Tap media messages to select them first.');
  try {
    const r = await api('/api/jobs', { method: 'POST', body: { chat_id: state.chat.id, message_ids: [...state.sel], folder: folderValue() } });
    toast(`Queued ${r.job.files_total} file(s)` + (r.skipped.length ? `, skipped ${r.skipped.length}` : ''));
    exitSelectMode(); openPanel(); pollJobs();
  } catch (x) { toast(x.message); }
});

// folder inputs share one value
let folderText = localStorage.getItem('tgweb_folder') || '';
function folderValue() { return folderText.trim(); }
document.addEventListener('input', e => {
  if (e.target.classList && e.target.classList.contains('folder-input')) {
    folderText = e.target.value; localStorage.setItem('tgweb_folder', folderText);
    $$('.folder-input').forEach(i => { if (i !== e.target) i.value = folderText; });
  }
});
async function refreshFolders() {
  $$('.folder-input').forEach(i => { i.value = folderText; });
  try {
    const r = await api('/api/folders');
    $('#folders').innerHTML = r.folders.map(f => `<option value="${esc(f)}"></option>`).join('');
    $('#dl-root').textContent = 'Saved on the server in: ' + r.root;
    $('#all-root').textContent = r.root;
  } catch { /* ignore */ }
}

/* download everything dialog */
$('#btn-all').addEventListener('click', () => { $('#all-err').textContent = ''; refreshFolders(); $('#dlg-all').showModal(); });
$('#all-form').addEventListener('submit', async e => {
  if (e.submitter && e.submitter.value !== 'go') return;
  e.preventDefault();
  const kinds = $$('input[name=k]:checked', e.target).map(i => i.value);
  if (!kinds.length) { $('#all-err').textContent = 'Pick at least one type.'; return; }
  try {
    const r = await api('/api/jobs', { method: 'POST', body: { chat_id: state.chat.id, all_media: true, kinds, limit: +$('#all-limit').value || 50, folder: folderValue() } });
    $('#dlg-all').close(); toast(`Queued ${r.job.files_total} file(s)`); openPanel(); pollJobs();
  } catch (x) { $('#all-err').textContent = x.message; }
});

/* ---------- downloads panel ---------- */
function openPanel() { $('#dl-panel').classList.remove('hidden'); refreshFolders(); pollJobs(); }
$('#btn-downloads').addEventListener('click', () => $('#dl-panel').classList.contains('hidden') ? openPanel() : $('#dl-panel').classList.add('hidden'));
$('#dl-close').addEventListener('click', () => $('#dl-panel').classList.add('hidden'));
$('#dl-clear').addEventListener('click', async () => { await api('/api/jobs/clear', { method: 'POST', body: {} }); pollJobs(); });
$('#jobs').addEventListener('click', async e => {
  const b = e.target.closest('[data-cancel]');
  if (b) { await api(`/api/jobs/${b.dataset.cancel}/cancel`, { method: 'POST', body: {} }); pollJobs(); }
});
function jobHTML(j) {
  const pct = j.bytes_total ? Math.min(100, Math.round(j.bytes_done * 100 / j.bytes_total)) : (j.status === 'done' ? 100 : 0);
  const items = j.items.map(i => {
    const p = i.size ? Math.min(100, Math.round(i.done * 100 / i.size)) : (i.status === 'done' ? 100 : 0);
    return `<div class="item"><div class="item-name">${esc(i.name)}</div>
      <div class="row"><span class="file-size">${fmtSize(i.done)}${i.size ? ' / ' + fmtSize(i.size) : ''}</span><span class="pill ${i.status}">${i.status}</span></div>
      ${i.status === 'downloading' ? `<div class="bar"><i style="width:${p}%"></i></div>` : ''}
      ${i.error ? `<div class="err">${esc(i.error)}</div>` : ''}</div>`;
  }).join('');
  return `<div class="job"><div class="job-head"><strong>${j.files_done}/${j.files_total} files</strong>
    <span><span class="pill ${j.status}">${j.status}</span>${j.status === 'running' ? ` <button class="link" data-cancel="${j.id}">Cancel</button>` : ''}</span></div>
    <div class="file-size">Folder: ${esc(j.folder)} · ${fmtSize(j.bytes_done)}${j.bytes_total ? ' / ' + fmtSize(j.bytes_total) : ''}</div>
    <div class="bar"><i style="width:${pct}%"></i></div>
    ${j.error ? `<div class="err">${esc(j.error)}</div>` : ''}<div class="items">${items}</div></div>`;
}
async function pollJobs() {
  try {
    state.jobs = await api('/api/jobs');
    const running = state.jobs.filter(j => j.status === 'running').length;
    const dot = $('#dl-dot'); dot.textContent = running; dot.classList.toggle('hidden', !running);
    if (!$('#dl-panel').classList.contains('hidden'))
      $('#jobs').innerHTML = state.jobs.map(jobHTML).join('') || '<p class="sub" style="padding:16px">No downloads yet. Select media in a chat and press Download.</p>';
  } catch { /* ignore transient errors */ }
}
function startPolling() { stopPolling(); state.pollTimer = setInterval(pollJobs, 1500); }
function stopPolling() { clearInterval(state.pollTimer); }

/* ---------- t.me links (private channels need existing membership) ---------- */
$('#link-form').addEventListener('submit', async e => {
  e.preventDefault();
  const url = $('#link').value.trim(); if (!url) return;
  try {
    const r = await api('/api/resolve_link', { method: 'POST', body: { url } });
    if (r.status === 'not_member') return toast(`"${r.title}": ${r.detail}`);
    if (!state.dialogs.some(d => d.id === r.chat.id)) state.dialogs.unshift({ ...r.chat, unread: 0, last: '', date: null, pinned: false });
    $('#link').value = '';
    await openChat(r.chat.id, { focus: r.message_id || 0 });
  } catch (x) { if (x.message !== 'auth') toast(x.message); }
});

/* ---------- boot ---------- */
async function boot() {
  try {
    const cfg = await api('/api/config');
    if (!cfg.configured) { showLogin(); $('#e-phone').textContent = 'Server is missing API_ID / API_HASH in its .env file.'; return; }
    await api('/api/auth/me');
    $('#login').classList.add('hidden'); $('#app').classList.remove('hidden');
    state.dialogs = await api('/api/dialogs'); renderChats();
    startPolling();
  } catch (x) { if (x.message !== 'auth') showLogin(); }
}
boot();
