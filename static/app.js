// 入口：搭页面骨架、路由、服务器事件流。
import { S, on, setCatalog, cfgFromServer, defaultCfg, applyTheme, localGet, localSet, saveSettings, findTurn, refreshMcp } from './state.js';
import { h, icon, api, toast, setAuthLostHandler, dialog, debounce, closePopover } from './util.js';
import { initThread, renderThread, toBottom, stickBottom, applyTurn, applyResp, applyDelta, applyAct, applyImg, applyTurnState } from './thread.js';
import { initComposer, renderComposer, setCfg, startEdit, applyPreset, openPicker, focusInput, switchConv, addFiles, refreshSend } from './composer.js';
import { initSide, renderSide, convMenu, renameConv } from './side.js';
import { openSettings } from './settings.js';

const app = document.getElementById('app');
let topEl, connEl, loadSeq = 0, lastLayout = null;

// ---------- 登录失效 ----------

let authShown = false;
function authLost() {
  if (authShown) return;
  authShown = true;
  if (es) es.close();
  dialog('登录已过期', h('p', { class: 'dlg-msg', text: '需要重新登录。输入框里没发出去的文字会保留。' }), {
    esc: false, buttons: [{ text: '重新登录', kind: 'primary', fn: () => {
      try { sessionStorage.setItem('back', location.hash); } catch { /* 记不住就回首页 */ }
      location.href = '/login';
      return false;
    } }],
  });
}

// ---------- 顶栏 ----------

function curSummary() { return S.convs.find(c => c.id === S.curId) || (S.cur && { id: S.cur.id, title: S.cur.title, pinned: S.cur.pinned }); }

function renderTop() {
  const c = S.curId ? curSummary() : null;
  const title = c ? c.title : '新对话';
  document.title = c ? `${title} · 圆桌` : '圆桌';
  document.body.classList.toggle('is-new', !S.curId);
  const more = c ? h('button', { class: 'icon-btn', type: 'button', title: '更多', 'aria-label': '对话操作' }, icon('more')) : null;
  if (more) more.addEventListener('click', () => convMenu(more, c, { align: 'right' }));
  const tabs = S.settings.layout === 'tabs';
  topEl.replaceChildren(...[
    h('button', { class: 'icon-btn side-toggle', type: 'button', title: '对话列表', 'aria-label': '对话列表', onclick: toggleSide }, icon('sidebar')),
    h('a', { class: 'icon-btn top-new', href: '#/', title: '新对话', 'aria-label': '新对话' }, icon('edit')),
    c ? h('button', { class: 'top-title', type: 'button', title: '点击重命名', text: title, onclick: () => renameConv(c) }) : h('span', { class: 'top-title static', text: title }),
    h('span', { class: 'grow' }), connEl,
    S.narrow ? null : h('button', { class: 'icon-btn', type: 'button', title: tabs ? '多个模型：现在是标签页，点击改为并排' : '多个模型：现在是并排，点击改为标签页',
      'aria-label': '切换排版', onclick: () => saveSettings({ layout: tabs ? 'columns' : 'tabs' }) }, icon(tabs ? 'tabs' : 'columns')),
    more].filter(Boolean));
}

function toggleSide() {
  if (S.narrow) document.body.classList.toggle('side-open');
  else localSet('sideCollapsed', document.body.classList.toggle('side-collapsed') ? '1' : null);
}
const closeDrawer = () => document.body.classList.remove('side-open');

// ---------- 对话载入 ----------

function saveUnread() { localSet('unread', S.unread.size ? JSON.stringify([...S.unread].slice(-200)) : null); }

async function openConv(id) {
  closePopover();
  S.curId = id; S.cur = null; S.loading = true;
  switchConv();
  if (S.unread.delete(id)) saveUnread();
  renderSide(); renderTop(); renderThread();
  const seq = ++loadSeq;
  let conv;
  try { conv = await api('GET', `/api/conversations/${id}`); } catch (e) {
    if (seq !== loadSeq) return;
    S.loading = false;
    toast(e.message, 'err');
    if (location.hash !== '#/') location.hash = '#/'; else newChat();
    return;
  }
  if (seq !== loadSeq) return;
  S.cur = conv; S.loading = false;
  for (const t of conv.turns) if (t.busy) S.live.add(t.id);
  const c = cfgFromServer(conv.config);
  if (c) setCfg(c); else renderComposer();
  renderTop(); renderThread(); toBottom(); refreshSend();
  focusInput();
}

function newChat() {
  closePopover();
  ++loadSeq;
  S.curId = null; S.cur = null; S.loading = false;
  switchConv();
  renderSide(); renderTop(); renderThread(); refreshSend();
  focusInput();
}

async function reloadNow() {
  if (!S.curId || S.loading) return;
  const id = S.curId, seq = ++loadSeq;
  try {
    const conv = await api('GET', `/api/conversations/${id}`);
    if (seq !== loadSeq || id !== S.curId) return;
    S.cur = conv;
    for (const t of conv.turns) if (t.busy) S.live.add(t.id);
    renderTop(); renderThread(); stickBottom(); refreshSend();
  } catch { /* 下次事件再试 */ }
}
const reload = debounce(reloadNow, 400);

async function refreshList() {
  try { S.convs = (await api('GET', '/api/conversations')).conversations; renderSide(); renderTop(); } catch { /* ignore */ }
}

function route() {
  if (location.hash.startsWith('#/settings')) {
    // 从 MCP 授权页回来：回到出发前的那个对话，再把设置打开在 MCP 那一节
    let back = null;
    try { back = sessionStorage.getItem('mcpBack'); sessionStorage.removeItem('mcpBack'); } catch { /* ignore */ }
    history.replaceState(null, '', back && /^#\/c\/[\w-]+$/.test(back) ? back : '#/');
    openSettings('mcp');
  }
  const m = location.hash.match(/^#\/c\/([\w-]+)$/);
  if (m) { if (m[1] !== S.curId) openConv(m[1]); } else newChat();
}

// ---------- 服务器事件 ----------

let es = null, hellos = 0, downT = null;
function setConn(ok) {
  clearTimeout(downT);
  if (ok) connEl.hidden = true;
  else downT = setTimeout(() => { connEl.hidden = false; }, 4000);
}

const HANDLERS = {
  hello() {
    if (hellos++) { refreshList(); reloadNow(); }
  },
  auth() { authLost(); },
  conv(ev) {
    const c = ev.conv, i = S.convs.findIndex(x => x.id === c.id);
    const was = i >= 0 ? S.convs[i] : null;
    if (i >= 0) S.convs[i] = c; else S.convs.unshift(c);
    if (was && was.busy && !c.busy) finished(c);
    if (c.id === S.curId) {
      if (S.cur) { S.cur.title = c.title; S.cur.pinned = c.pinned; }
      if (!document.hidden) renderTop();
    }
    renderSide();
  },
  'conv.delete'(ev) {
    S.convs = S.convs.filter(c => c.id !== ev.id);
    if (S.unread.delete(ev.id)) saveUnread();
    if (ev.id === S.curId) { if (location.hash !== '#/') location.hash = '#/'; else newChat(); } else renderSide();
  },
  'turn.state'(ev) {
    const c = S.convs.find(x => x.id === ev.conv_id);
    if (c && c.busy !== ev.conv_busy) {
      c.busy = ev.conv_busy;
      if (!c.busy) finished(c);
      renderSide();
    }
    applyTurnState(ev);
  },
  // 模型的显示名变了（刚知道 Claude 的别名指向哪个版本）：重新取一次清单
  async models() {
    try { setCatalog(await api('GET', '/api/models')); renderComposer(); } catch { /* 刷新页面时自然会取到 */ }
  },
  turn: applyTurn, resp: applyResp, delta: applyDelta, act: applyAct, img: applyImg,
};

// 一个对话的回答全部出完了：不在眼前的话，留个记号。
function finished(c) {
  if (c.id !== S.curId) { S.unread.add(c.id); saveUnread(); }
  if (document.hidden) document.title = `● ${c.title} · 圆桌`;
}

function connect() {
  if (authShown) return;
  if (es) es.close();
  es = new EventSource('/api/events');
  es.onopen = () => setConn(true);
  es.onmessage = e => {
    let ev;
    try { ev = JSON.parse(e.data); } catch { return; }
    const fn = HANDLERS[ev.type];
    if (fn) { try { fn(ev); } catch (err) { console.error(err); reload(); } }
  };
  es.onerror = async () => {
    setConn(false);
    if (es.readyState !== EventSource.CLOSED) return;   // 浏览器会自己重连
    try { await api('GET', '/api/me'); } catch { /* 登录失效时 api() 已经弹窗 */ }
    setTimeout(connect, 3000);
  };
}

// ---------- 启动 ----------

function build() {
  const side = h('aside', { class: 'side' });
  topEl = h('header', { class: 'top' });
  connEl = h('span', { class: 'conn', hidden: true }, h('span', { class: 'spin' }), '连接断开，正在重连');
  const thread = h('div', { id: 'thread', class: 'thread' });
  const scroller = h('div', { id: 'scroll', class: 'scroll' }, thread);
  const jump = h('button', { class: 'jump', type: 'button', hidden: true, title: '回到底部', 'aria-label': '回到底部' }, icon('down'));
  const composer = h('div', { id: 'composer', class: 'composer' });
  const veil = h('div', { class: 'drop-veil', hidden: true }, h('div', null, icon('clip'), '松手即可添加为附件'));
  app.replaceChildren(side, h('div', { class: 'backdrop', onclick: closeDrawer }),
    h('main', { class: 'main' }, topEl, scroller,
      h('div', { class: 'composer-wrap' }, jump, composer,
        h('div', { class: 'foot-note', text: '模型会出错，重要的事请核对。多找几个模型互评能减少错误，但不能保证没有。' }))),
    veil);
  initSide(side, { toggleSide, closeDrawer, openSettings: () => openSettings() });
  initThread({ root: thread, scroller, jump, hooks: { applyPreset, startEdit, openPicker } });
  initComposer(composer);

  let depth = 0;
  const hasFiles = e => [...(e.dataTransfer?.types || [])].includes('Files');
  window.addEventListener('dragenter', e => { if (hasFiles(e)) { depth++; veil.hidden = false; } });
  window.addEventListener('dragleave', e => { if (hasFiles(e) && --depth <= 0) { depth = 0; veil.hidden = true; } });
  window.addEventListener('dragover', e => { if (hasFiles(e)) e.preventDefault(); });
  window.addEventListener('drop', e => {
    if (!hasFiles(e)) return;
    e.preventDefault();
    depth = 0; veil.hidden = true;
    addFiles([...e.dataTransfer.files]);
  });
}

async function boot() {
  setAuthLostHandler(authLost);
  try {                       // 重新登录回来：回到刚才那个对话
    const back = sessionStorage.getItem('back');
    sessionStorage.removeItem('back');
    if (back && /^#\/c\/[0-9a-f]+$/.test(back) && !location.hash) history.replaceState(null, '', back);
  } catch { /* 忽略 */ }
  on('toast', toast);
  applyTheme();
  window.matchMedia('(prefers-color-scheme: dark)').addEventListener('change', applyTheme);
  let cat, list;
  try {
    [S.me, cat, list] = await Promise.all([api('GET', '/api/me'), api('GET', '/api/models'), api('GET', '/api/conversations')]);
  } catch (e) {
    if (!authShown) app.replaceChildren(h('div', { class: 'boot' }, `载入失败：${e.message}`, h('br'), h('button', { class: 'btn', type: 'button', onclick: () => location.reload() }, '重试')));
    return;
  }
  S.settings = S.me.settings;
  S.mcp = S.me.mcp || null;
  lastLayout = S.settings.layout;
  setCatalog(cat);
  S.convs = list.conversations;
  S.cfg = defaultCfg();
  try { S.unread = new Set(JSON.parse(localGet('unread') || '[]')); } catch { S.unread = new Set(); }
  const mq = window.matchMedia('(max-width: 720px)');
  S.narrow = mq.matches;
  if (localGet('sideCollapsed')) document.body.classList.add('side-collapsed');
  build();
  mq.addEventListener('change', () => { S.narrow = mq.matches; closeDrawer(); renderTop(); renderThread(); stickBottom(); });

  on('settings', () => {
    renderTop();
    if (S.settings.layout !== lastLayout) { lastLayout = S.settings.layout; renderThread(); stickBottom(); }
  });
  on('resync', reload);
  on('sent', (r, replaced) => {
    if (r.conv_id !== S.curId) {
      history.replaceState(null, '', `#/c/${r.conv_id}`);
      openConv(r.conv_id);
    } else if (replaced || !findTurn(r.turn_id)) reloadNow().then(toBottom);
    else toBottom();
  });
  window.addEventListener('hashchange', route);
  document.addEventListener('visibilitychange', () => {
    if (document.hidden) return;
    renderTop();
    if (S.mcp) refreshMcp();
    if (es && es.readyState === EventSource.CLOSED) connect();
  });
  document.addEventListener('keydown', e => {
    if ((e.ctrlKey || e.metaKey) && e.shiftKey && e.key.toLowerCase() === 'o') {
      e.preventDefault();
      if (location.hash && location.hash !== '#/') location.hash = '#/'; else newChat();
    }
  });
  route();
  connect();
}

boot();
