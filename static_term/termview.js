// 终端本体：一个 xterm 实例，接到当前窗口背后的 tmux 上。切换窗口 = 断开旧连接、接上新的，tmux 会把整屏重画一遍。
import { S, emit } from './state.js';
import { h, api, toast } from './util.js';
import { showLock } from './auth.js';

const enc = new TextEncoder();
let term, fit, host, overlay, ws = null, cur = null, gen = 0, retry = 0, retryT = null, ctrlSticky = false, keybar = null;

const THEME = {
  background: '#171716', foreground: '#e6e4de', cursor: '#e6e4de', cursorAccent: '#171716', selectionBackground: '#4a4a6a',
  black: '#262624', red: '#f0776e', green: '#7cc79a', yellow: '#e2c074', blue: '#7aa7f0', magenta: '#c79af0', cyan: '#6fc7c7', white: '#d8d6d0',
  brightBlack: '#6f6c66', brightRed: '#ff9d94', brightGreen: '#9fe0b7', brightYellow: '#f2d898', brightBlue: '#a3c4ff', brightMagenta: '#dcbcff', brightCyan: '#9fe3e3', brightWhite: '#ffffff',
};

export function initTerm(el) {
  // 外面一层只管留边，xterm 开在里面那层：它算行数时看的是“直接包着它的那一层”的高度，
  // 留边要是写在那一层上会被算进去，行数多算，最底下一行就被切掉
  host = h('div', { class: 'term-inner' });
  overlay = h('div', { class: 'term-overlay', hidden: true });
  el.append(h('div', { class: 'term-host' }, host), overlay);
  term = new Terminal({
    fontFamily: 'ui-monospace, SFMono-Regular, "SF Mono", Menlo, Consolas, "DejaVu Sans Mono", "Noto Sans Mono CJK SC", "PingFang SC", "Microsoft YaHei", monospace',
    fontSize: Number(localStorage.getItem('fontSize')) || 14, lineHeight: 1.15, cursorBlink: true, scrollback: 1000, allowProposedApi: true,
    macOptionIsMeta: true, theme: THEME,
  });
  fit = new FitAddon.FitAddon();
  term.loadAddon(fit);
  term.loadAddon(new Unicode11Addon.Unicode11Addon());
  term.unicode.activeVersion = '11';                                   // 中文、表情按两格宽算
  term.loadAddon(new WebLinksAddon.WebLinksAddon((e, uri) => window.open(uri, '_blank', 'noopener,noreferrer')));
  // tmux 里拖选复制 → 浏览器剪贴板（OSC 52）。自带的实现只认“c”这一种写法，tmux 发的是空的，所以自己写一个：
  // 往剪贴板写，什么写法都收；读剪贴板一律不给——不然终端里随便一个程序（或者一段被打印出来的内容）就能把你剪贴板里的东西读走
  term.loadAddon(new ClipboardAddon.ClipboardAddon(undefined, {
    readText: () => Promise.resolve(''),
    writeText: (_sel, text) => (navigator.clipboard ? navigator.clipboard.writeText(text).catch(() => {}) : Promise.resolve()),
  }));
  term.open(host);
  term.onData(d => send(enc.encode(applySticky(d))));
  term.onBinary(d => send(Uint8Array.from(d, c => c.charCodeAt(0) & 255)));
  term.onResize(({ cols, rows }) => { if (ws && ws.readyState === 1) ws.send(JSON.stringify({ resize: [cols, rows] })); });
  term.attachCustomKeyEventHandler(e => {
    if (e.type !== 'keydown') return true;
    const k = e.key.toLowerCase();
    if (e.ctrlKey && e.shiftKey && k === 'c') { const s = term.getSelection(); if (s) navigator.clipboard?.writeText(s).catch(() => {}); e.preventDefault(); return false; }
    if (e.ctrlKey && e.shiftKey && k === 'v') return false;             // 交给浏览器的粘贴事件
    if (e.altKey && !e.ctrlKey && !e.metaKey && /^Digit[1-9]$/.test(e.code)) return false;   // Alt+数字是切窗口，不送进终端
    return true;
  });
  let t;
  new ResizeObserver(() => { clearTimeout(t); t = setTimeout(refit, 60); }).observe(host);
  window.addEventListener('focus', () => { if (ws && ws.readyState > 1 && cur) connect(cur); });
  document.addEventListener('visibilitychange', () => { if (!document.hidden && cur && (!ws || ws.readyState > 1)) connect(cur); });
  if (window.matchMedia('(pointer: coarse)').matches) buildKeybar(el);
}

function refit() {
  if (!host.offsetWidth || !host.offsetHeight) return;
  try { fit.fit(); } catch { /* 还没显示出来 */ }
}

function send(bytes) {
  if (ws && ws.readyState === 1) ws.send(bytes);
}

function note(text, action) {
  overlay.hidden = !text;
  overlay.replaceChildren(...(text ? [h('span', { text }), action ? h('button', { class: 'btn sm', type: 'button', text: action.text, onclick: action.fn }) : null].filter(Boolean) : []));
}

export function currentWindow() { return cur; }
export function focusTerm() { term.focus(); }

export function detach() {
  gen++;
  clearTimeout(retryT);
  if (ws) { try { ws.close(); } catch { /* 已经关了 */ } }
  ws = null;
  cur = null;
  note('');
}

// 接到一个窗口上。name 为空 = 什么都不接（没有窗口时）。
export async function connect(name) {
  const my = ++gen;
  clearTimeout(retryT);
  if (ws) { try { ws.close(); } catch { /* 已经关了 */ } ws = null; }
  cur = name || null;
  if (!name) { term.reset(); note(''); return; }
  refit();
  let ticket;
  try { ticket = (await api('POST', '/api/ws-ticket', {})).ticket; } catch (e) {
    if (my !== gen || /验证码|登录/.test(e.message)) return;      // 锁了或登录没了：锁屏会出来，不用在这儿重试
    // 多半是网络断了或服务在重启：隔一会儿自己再试
    note('连不上，正在重试…', { text: '现在重试', fn: () => connect(name) });
    retryT = setTimeout(() => { if (my === gen) connect(name); }, Math.min(8000, 800 * 2 ** retry++));
    return;
  }
  if (my !== gen) return;
  const url = `${location.protocol === 'https:' ? 'wss' : 'ws'}://${location.host}/ws/term/${name}?t=${encodeURIComponent(ticket)}&cols=${term.cols}&rows=${term.rows}`;
  const sock = new WebSocket(url);
  sock.binaryType = 'arraybuffer';
  ws = sock;
  let opened = false;
  sock.onopen = () => {
    if (my !== gen) return sock.close();
    opened = true;
    retry = 0;
    term.reset();                       // tmux 接上来会把整屏重画一遍
    note('');
    sock.send(JSON.stringify({ resize: [term.cols, term.rows] }));
    term.focus();
  };
  sock.onmessage = e => { if (my === gen) term.write(typeof e.data === 'string' ? e.data : new Uint8Array(e.data)); };
  sock.onclose = e => {
    if (my !== gen) return;
    ws = null;
    if (e.code === 4423) { note('已锁定'); return showLock(); }
    if (e.code === 4401) { note('登录过期了'); return emit('authlost'); }
    if (e.code === 4404) { note(''); return emit('gone', name); }
    if (document.hidden) { note('连接断开了', { text: '重连', fn: () => connect(name) }); return; }
    // 别的原因（网络断了、服务重启）：自己重连。握手就被拒的话多半是锁了或登录没了，问一下状态
    const wait = Math.min(8000, 500 * 2 ** retry++);
    note(opened ? '连接断开了，正在重连…' : '正在连接…');
    retryT = setTimeout(async () => {
      if (my !== gen) return;
      if (!opened) {
        try { const s = await api('GET', '/api/auth/state'); if (!s.unlocked) return showLock(); } catch { /* 网络还没好，接着试 */ }
      }
      connect(name);
    }, wait);
  };
}

// ---------- 手机上的按键条：手机键盘没有 Esc、Tab、Ctrl、方向键 ----------

function applySticky(d) {
  if (!ctrlSticky || d.length !== 1) return d;
  ctrlSticky = false;
  keybar?.querySelector('.kb-ctrl')?.classList.remove('on');
  const c = d.toUpperCase().charCodeAt(0);
  return c >= 64 && c <= 95 ? String.fromCharCode(c - 64) : d;
}

function buildKeybar(el) {
  const key = (label, seq, cls) => h('button', { class: 'kb' + (cls ? ' ' + cls : ''), type: 'button', text: label, onpointerdown: e => {
    e.preventDefault();                 // 不抢走终端的焦点，软键盘不会缩回去
    if (seq === 'ctrl') { ctrlSticky = !ctrlSticky; e.currentTarget.classList.toggle('on', ctrlSticky); return; }
    send(enc.encode(seq));
  } });
  keybar = h('div', { class: 'keybar' }, key('Esc', '\x1b'), key('Tab', '\t'), key('Ctrl', 'ctrl', 'kb-ctrl'), key('↑', '\x1b[A'), key('↓', '\x1b[B'),
    key('←', '\x1b[D'), key('→', '\x1b[C'), key('^C', '\x03'), key('^D', '\x04'), key('/', '/'), key('-', '-'), key('|', '|'), key('~', '~'));
  el.append(keybar);
}

export function setFontSize(delta) {
  const n = Math.max(10, Math.min(24, (term.options.fontSize || 14) + delta));
  term.options.fontSize = n;
  localStorage.setItem('fontSize', String(n));
  refit();
  toast(`字号 ${n}`);
}
