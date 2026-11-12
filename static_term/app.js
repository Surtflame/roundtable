// 入口：页面骨架、侧边栏、启动。
import { S, on, emit, localGet, localSet } from './state.js';
import { h, icon, api, toast, menu, setAuthLostHandler, setLockedHandler, closePopover } from './util.js';
import { showLock, lockNow, lockScreenShown } from './auth.js';
import { initTerm, focusTerm, setFontSize } from './termview.js';
import { initTabs, refresh, newMenu, openWindow, cwdDefault } from './tabs.js';
import { initHistory, showHistory } from './history.js';
import { initFiles, showFiles } from './files.js';
import { initPeer, switchToPeer, topNavigate } from './peer.js';

const app = document.getElementById('app');
let authShown = false, leaving = false, panelBtns = {}, panels = {};

function authLost() {
  if (authShown) return;
  authShown = true;
  app.append(h('div', { class: 'lock' }, h('div', { class: 'lock-box' }, h('h1', { text: '登录过期了' }),
    h('p', { class: 'lock-note', text: '重新登录之后回到这里。终端里的程序不受影响，照常在跑。' }),
    h('button', { class: 'btn primary', type: 'button', text: '重新登录', onclick: () => { leaving = true; topNavigate((S.me?.chat_url || '/') + '?term=1'); } }))));
}

const whoami = () => (S.me.auth_mode === 'google' ? S.me.email : S.me.auth_mode === 'password' ? '已登录' : '本机模式');

// 退出终端这边的登录（对话那边不受影响）。终端里的程序照常跑
async function logout() {
  try { await api('POST', '/api/auth/logout', {}); } catch (e) { return toast(e.message, 'err'); }
  leaving = true;
  topNavigate(S.me.chat_url || '/');
}

function setPanel(name, opt) {
  S.panel = name;
  localSet('panel', name);
  for (const k of Object.keys(panels)) { panels[k].hidden = k !== name; panelBtns[k].classList.toggle('on', k === name); }
  if (name === 'history') showHistory();
  if (name === 'files') showFiles(opt);
}

function toggleSide(force) {
  const open = force ?? (S.narrow ? !document.body.classList.contains('side-open') : document.body.classList.contains('side-collapsed'));
  if (S.narrow) document.body.classList.toggle('side-open', open);
  else { document.body.classList.toggle('side-collapsed', !open); localSet('sideCollapsed', open ? null : '1'); }
}

function build() {
  const strip = h('div', { class: 'tabs', role: 'tablist' });
  const termBox = h('div', { class: 'term-box' });
  const empty = h('div', { class: 'empty', hidden: true },
    h('div', { class: 'empty-mark' }, icon('term')),
    h('h1', { text: '还没有打开的窗口' }),
    h('p', { text: '每个窗口背后是服务器上一个一直在跑的终端。关掉浏览器、断网都不影响，回来接着用。' }),
    h('div', { class: 'empty-btns' },
      h('button', { class: 'btn primary', type: 'button', onclick: () => openWindow({ cwd: cwdDefault(), agent: 'claude' }) }, 'Claude Code'),
      h('button', { class: 'btn', type: 'button', onclick: () => openWindow({ cwd: cwdDefault(), agent: 'codex' }) }, 'Codex'),
      h('button', { class: 'btn', type: 'button', onclick: () => openWindow({ cwd: cwdDefault() }) }, 'Shell')),
    h('p', { class: 'empty-sub', text: '想接着以前的对话聊，在左边“历史对话”里点它。' }));
  const pbtn = (name, ic, text) => (panelBtns[name] = h('button', { class: 'side-tab', type: 'button', onclick: () => setPanel(name) }, icon(ic), h('span', { text })));
  panels.history = h('div', { class: 'side-panel' });
  panels.files = h('div', { class: 'side-panel', hidden: true });
  const moreBtn = h('button', { class: 'icon-btn', type: 'button', title: '更多', onclick: () => menu(moreBtn, [
    { text: '字号大一点', fn: () => setFontSize(1) },
    { text: '字号小一点', fn: () => setFontSize(-1) },
    '-',
    S.me.totp ? { text: '现在就锁上', icon: 'lock', fn: lockNow } : null,
    S.me.auth_mode !== 'none' ? { text: '退出终端的登录', icon: 'x', fn: logout } : null,
    { label: whoami() },
  ].filter(Boolean), { align: 'right' }) }, icon('more'));
  const newBtn = h('button', { class: 'new-chat', type: 'button', onclick: () => newMenu(newBtn) }, icon('plus'), h('span', { class: 'grow', text: '新窗口' }), icon('down'));
  app.replaceChildren(
    h('aside', { class: 'side' },
      h('div', { class: 'side-top' },
        h('a', { class: 'brand', href: S.me.chat_url, title: '圆桌', onclick: switchToPeer }, h('img', { src: '/static/brand.svg', alt: '', width: '22', height: '22' }), h('span', { text: '圆桌' })),
        h('button', { class: 'icon-btn', type: 'button', title: '收起侧栏', 'aria-label': '收起侧栏', onclick: () => toggleSide(false) }, icon('sidebar'))),
      h('nav', { class: 'app-switch', 'aria-label': '切换' },
        h('a', { href: S.me.chat_url, onclick: switchToPeer, text: '对话' }), h('span', { class: 'on', text: '终端' })),
      newBtn,
      h('div', { class: 'side-head' }, pbtn('history', 'history', '历史对话'), pbtn('files', 'folder', '文件')),
      panels.history, panels.files,
      h('div', { class: 'side-foot' },
        h('span', { class: 'me', text: whoami(), title: whoami() }),
        S.me.totp ? h('button', { class: 'icon-btn', type: 'button', title: '现在就锁上（终端里的程序照常跑）', 'aria-label': '锁上', onclick: lockNow }, icon('lock')) : null)),
    h('div', { class: 'side-shade', onclick: () => toggleSide(false) }),
    h('main', { class: 'main' },
      h('div', { class: 'top' },
        h('button', { class: 'icon-btn side-toggle', type: 'button', title: '历史对话和文件', onclick: () => toggleSide() }, icon('sidebar')),
        strip, moreBtn),
      termBox, empty));
  initTerm(termBox);
  initTabs(strip, empty);
  initHistory(panels.history);
  initFiles(panels.files);
  setPanel(localGet('panel') === 'files' ? 'files' : 'history');
}

async function boot() {
  setAuthLostHandler(authLost);
  setLockedHandler(() => { emit('locked'); showLock(); });
  on('authlost', authLost);
  on('toast', toast);
  try { S.me = await api('GET', '/api/auth/state'); } catch (e) {
    if (!authShown) app.replaceChildren(h('div', { class: 'boot' }, `载入失败：${e.message}`, h('br'), h('button', { class: 'btn', type: 'button', onclick: () => location.reload() }, '重试')));
    return;
  }
  // 本机模式下地址栏里可能是 localhost 也可能是 127.0.0.1：让对话的地址跟着当前用的那个主机名走，两边才对得上
  if (S.me.auth_mode === 'none') { const u = new URL(S.me.chat_url); u.hostname = location.hostname; S.me.chat_url = u.href; }
  // 切到“对话”不重新加载：见 peer.js。真要整页跳走的时候（嵌不进去的退路）不用弹“确定要走吗”
  initPeer(S.me.chat_url, { onShown: () => { try { focusTerm(); } catch { /* 还没建好 */ } }, beforeLeave: () => { leaving = true; }, title: '圆桌' });
  if (!S.me.unlocked) {
    // 还没解锁：先只画锁屏，解锁后整页重来一遍（这时才取得到别的东西）
    app.replaceChildren();
    on('unlocked', () => location.reload());
    return showLock();
  }
  const chatUrl = S.me.chat_url;
  S.me = await api('GET', '/api/me');
  S.me.chat_url = chatUrl;
  const mq = window.matchMedia('(max-width: 760px)');
  S.narrow = mq.matches;
  mq.addEventListener('change', () => { S.narrow = mq.matches; document.body.classList.remove('side-open'); });
  if (localGet('sideCollapsed')) document.body.classList.add('side-collapsed');
  build();
  await refresh();
  focusTerm();
  // Ctrl+W 在 shell 里是“删掉前一个词”，在浏览器里是“关掉这个标签页”。拦不住它，只能在真要关的时候问一句。
  window.addEventListener('beforeunload', e => { if (S.windows.length && !lockScreenShown() && !leaving) { e.preventDefault(); e.returnValue = ''; } });
  document.addEventListener('keydown', e => {
    if (e.altKey && !e.ctrlKey && !e.metaKey && /^Digit[1-9]$/.test(e.code)) {    // Alt+数字：切到第几个窗口（Mac 上是 Option）
      const w = S.windows[Number(e.code.slice(5)) - 1];
      if (w) { e.preventDefault(); closePopover(); import('./tabs.js').then(m => m.select(w.name)); }
    }
  }, true);
  // 解锁状态快到点了也没人动：到时候服务器会断开终端，页面这边跟着出锁屏（见 termview 里的 4423）
}

boot();
