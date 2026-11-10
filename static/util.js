// 通用小工具：DOM、图标、接口、弹层、格式化。

export const $ = (sel, el = document) => el.querySelector(sel);
export const $$ = (sel, el = document) => [...el.querySelectorAll(sel)];

export function h(tag, attrs, ...kids) {
  const el = document.createElement(tag);
  if (attrs) {
    for (const [k, v] of Object.entries(attrs)) {
      if (v == null || v === false) continue;
      if (k === 'class') el.className = v;
      else if (k === 'html') el.innerHTML = v;
      else if (k === 'text') el.textContent = v;
      else if (k === 'data') Object.assign(el.dataset, v);
      else if (k.startsWith('on')) el.addEventListener(k.slice(2), v);
      else if (k === 'style' && typeof v === 'object') Object.assign(el.style, v);
      else el.setAttribute(k, v === true ? '' : v);
    }
  }
  for (const kid of kids.flat(Infinity)) {
    if (kid == null || kid === false) continue;
    el.append(kid.nodeType ? kid : document.createTextNode(String(kid)));
  }
  return el;
}

const P = d => `<svg viewBox="0 0 24 24" width="18" height="18" fill="none" stroke="currentColor" stroke-width="1.8" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true">${d}</svg>`;
export const ICON = {
  plus: P('<path d="M12 5v14M5 12h14"/>'),
  search: P('<circle cx="11" cy="11" r="7"/><path d="m20 20-3.5-3.5"/>'),
  send: P('<path d="M12 19V5M5.5 11.5 12 5l6.5 6.5"/>'),
  stop: P('<rect x="7" y="7" width="10" height="10" rx="1.5" fill="currentColor" stroke="none"/>'),
  clip: P('<path d="m20.5 11.5-8.6 8.6a5.5 5.5 0 0 1-7.8-7.8l8.9-8.9a3.7 3.7 0 0 1 5.2 5.2l-8.9 8.9a1.8 1.8 0 0 1-2.6-2.6l8.2-8.2"/>'),
  globe: P('<circle cx="12" cy="12" r="9"/><path d="M3 12h18M12 3c2.5 2.6 3.8 5.7 3.8 9s-1.3 6.4-3.8 9c-2.5-2.6-3.8-5.7-3.8-9S9.500 5.600 12 3z"/>'),
  copy: P('<rect x="9" y="9" width="11" height="11" rx="2"/><path d="M5 15V6a2 2 0 0 1 2-2h9"/>'),
  check: P('<path d="m5 12.5 4.5 4.5L19 7.5"/>'),
  refresh: P('<path d="M20 11a8 8 0 0 0-14.3-4.500L4 8.500M4 4v4.500h4.500M4 13a8 8 0 0 0 14.300 4.500L20 15.500M20 20v-4.500h-4.500"/>'),
  edit: P('<path d="M4 20h4L19.500 8.500a2.100 2.100 0 0 0-3-3L5 17v3zM14.500 7.500l3 3"/>'),
  trash: P('<path d="M4 7h16M9 7V4.500h6V7M6.500 7l.8 12.500h9.400L17.500 7M10 11v5M14 11v5"/>'),
  pin: P('<path d="M12 17v4M8 3.500h8l-1 6 3 3.500H6l3-3.500-1-6z"/>'),
  download: P('<path d="M12 4v11M7 10.500l5 5 5-5M5 20h14"/>'),
  x: P('<path d="M6 6l12 12M18 6 6 18"/>'),
  chev: P('<path d="m9 6 6 6-6 6"/>'),
  down: P('<path d="m6 9 6 6 6-6"/>'),
  menu: P('<path d="M4 7h16M4 12h16M4 17h16"/>'),
  gear: P('<circle cx="12" cy="12" r="3"/><path d="M19 12a7 7 0 0 0-.1-1.300l2-1.500-2-3.400-2.300.9a7 7 0 0 0-2.300-1.300L14 3h-4l-.4 2.400a7 7 0 0 0-2.300 1.300l-2.300-.9-2 3.400 2 1.500a7 7 0 0 0 0 2.600l-2 1.500 2 3.400 2.300-.9a7 7 0 0 0 2.300 1.300L10 21h4l.4-2.400a7 7 0 0 0 2.300-1.300l2.300.9 2-3.400-2-1.500c.1-.4.1-.9.1-1.300z"/>'),
  star: P('<path d="m12 3.500 2.600 5.400 5.900.8-4.300 4.100 1 5.900L12 16.900l-5.200 2.800 1-5.900L3.500 9.700l5.900-.8L12 3.500z"/>'),
  sidebar: P('<rect x="3.500" y="4.500" width="17" height="15" rx="2.500"/><path d="M9.500 4.500v15"/>'),
  columns: P('<rect x="3.500" y="4.500" width="7" height="15" rx="1.500"/><rect x="13.500" y="4.500" width="7" height="15" rx="1.500"/>'),
  tabs: P('<path d="M3.500 9.500h17v8a2 2 0 0 1-2 2h-13a2 2 0 0 1-2-2v-8zM3.500 9.500v-3a2 2 0 0 1 2-2h4l1.500 5M13 4.500h4"/>'),
  more: P('<circle cx="5.500" cy="12" r="1.300" fill="currentColor"/><circle cx="12" cy="12" r="1.300" fill="currentColor"/><circle cx="18.500" cy="12" r="1.300" fill="currentColor"/>'),
  swap: P('<path d="M4 8h14l-3.500-3.500M20 16H6l3.500 3.500"/>'),
  merge: P('<path d="M6 4v5a6 6 0 0 0 6 6v5M18 4v5a6 6 0 0 1-6 6M9 17.500l3 3 3-3"/>'),
  file: P('<path d="M7 3.500h7l4.500 4.500V20a.5.5 0 0 1-.5.5H7a.5.5 0 0 1-.5-.5V4a.5.5 0 0 1 .5-.5zM14 3.500V8h4.500"/>'),
  brain: P('<path d="M9.500 4a3 3 0 0 0-3 3 3 3 0 0 0-2 5 3.200 3.200 0 0 0 2 5.500A3 3 0 0 0 12 18V6.500A2.500 2.500 0 0 0 9.500 4zM14.500 4a3 3 0 0 1 3 3 3 3 0 0 1 2 5 3.200 3.200 0 0 1-2 5.500A3 3 0 0 1 12 18"/>'),
  logout: P('<path d="M14 4.500h4a1.500 1.500 0 0 1 1.500 1.500v12a1.500 1.500 0 0 1-1.500 1.500h-4M10 8l-4 4 4 4M6 12h9"/>'),
  sun: P('<circle cx="12" cy="12" r="4"/><path d="M12 2.500v2M12 19.500v2M2.500 12h2M19.500 12h2M5.300 5.300l1.400 1.400M17.300 17.300l1.400 1.400M5.300 18.700l1.400-1.400M17.300 6.700l1.400-1.400"/>'),
  image: P('<rect x="3.500" y="4.500" width="17" height="15" rx="2.500"/><circle cx="9" cy="10" r="1.700"/><path d="m4 17 4.500-4.500 3.500 3.500 3-3 5 5"/>'),
};
export const icon = (name, cls) => h('span', { class: 'ic' + (cls ? ' ' + cls : ''), html: ICON[name] || '' });

// ---------- 接口 ----------

let onAuthLost = () => {};
export const setAuthLostHandler = fn => { onAuthLost = fn; };

export async function api(method, url, body) {
  const opt = { method, headers: { 'X-Requested-With': 'roundtable' }, redirect: 'manual', credentials: 'same-origin' };
  if (body instanceof FormData) opt.body = body;
  else if (body !== undefined) {
    opt.headers['Content-Type'] = 'application/json';
    opt.body = JSON.stringify(body);
  }
  let r;
  try { r = await fetch(url, opt); } catch { throw new Error('网络连接失败'); }
  if (r.type === 'opaqueredirect' || r.status === 401) {
    onAuthLost();
    throw new Error('登录已过期');
  }
  let data = null;
  try { data = await r.json(); } catch { /* 非 JSON */ }
  if (!r.ok) throw new Error((data && data.error) || `请求失败（${r.status}）`);
  return data;
}

// ---------- 提示条 ----------

export function toast(msg, kind = '') {
  let box = $('#toasts');
  if (!box) { box = h('div', { id: 'toasts' }); document.body.append(box); }
  const el = h('div', { class: 'toast ' + kind, text: msg });
  box.append(el);
  setTimeout(() => { el.classList.add('out'); setTimeout(() => el.remove(), 300); }, kind === 'err' ? 5000 : 2200);
}

// ---------- 弹出层 ----------

let openPop = null;
export function closePopover() {
  if (openPop) { const p = openPop; openPop = null; p.el.remove(); p.onClose && p.onClose(); }
}
export function popover(anchor, content, { align = 'left', place = 'auto', cls = '', onClose } = {}) {
  if (openPop && openPop.anchor === anchor) { closePopover(); return null; }
  closePopover();
  const el = h('div', { class: 'pop ' + cls, role: 'menu' }, content);
  document.body.append(el);
  const sheet = cls.includes('sheet') && window.matchMedia('(max-width: 720px)').matches;
  if (sheet) el.classList.add('as-sheet');
  else positionPop(el, anchor, align, place);
  openPop = { el, anchor, onClose };
  return el;
}
export function positionPop(el, anchor, align = 'left', place = 'auto') {
  const a = anchor.getBoundingClientRect();
  const vw = window.innerWidth, vh = window.innerHeight;
  el.style.maxHeight = '';
  const w = el.offsetWidth, hh = el.offsetHeight;
  let left = align === 'right' ? a.right - w : a.left;
  left = Math.max(8, Math.min(left, vw - w - 8));
  const below = vh - a.bottom - 12, above = a.top - 12;
  const up = place === 'up' || (place === 'auto' && below < Math.min(hh, 260) && above > below);
  if (up) {
    el.style.maxHeight = Math.max(160, above) + 'px';
    el.style.bottom = (vh - a.top + 6) + 'px';
    el.style.top = 'auto';
  } else {
    el.style.maxHeight = Math.max(160, below) + 'px';
    el.style.top = (a.bottom + 6) + 'px';
    el.style.bottom = 'auto';
  }
  el.style.left = left + 'px';
}
document.addEventListener('pointerdown', e => {
  if (openPop && !openPop.el.contains(e.target) && !openPop.anchor.contains(e.target)) closePopover();
}, true);
document.addEventListener('keydown', e => {
  if (e.key === 'Escape') {
    if (openPop) { closePopover(); e.stopPropagation(); return; }
    const d = $$('.dlg-back').pop();
    if (d && d.dataset.esc !== 'no') d.remove();
  }
});
window.addEventListener('resize', closePopover);

export function menu(anchor, items, opt = {}) {
  const list = h('div', { class: 'menu' }, items.filter(Boolean).map(it => {
    if (it === '-') return h('div', { class: 'menu-sep' });
    if (it.label && !it.fn) return h('div', { class: 'menu-label', text: it.label });
    return h('button', {
      class: 'menu-item' + (it.danger ? ' danger' : '') + (it.checked ? ' checked' : ''), type: 'button',
      onclick: () => { closePopover(); it.fn(); },
    }, it.icon ? icon(it.icon) : null, h('span', { class: 'grow', text: it.text }),
    it.hint ? h('span', { class: 'hint', text: it.hint }) : null, it.checked ? icon('check') : null);
  }));
  return popover(anchor, list, opt);
}

export function dialog(title, body, { buttons = [], cls = '', esc = true } = {}) {
  const back = h('div', { class: 'dlg-back', data: { esc: esc ? 'yes' : 'no' } });
  const close = () => back.remove();
  const box = h('div', { class: 'dlg ' + cls, role: 'dialog', 'aria-modal': 'true' },
    h('div', { class: 'dlg-h' }, h('h2', { text: title }),
      esc ? h('button', { class: 'icon-btn', type: 'button', title: '关闭', onclick: close }, icon('x')) : null),
    h('div', { class: 'dlg-b' }, body),
    buttons.length ? h('div', { class: 'dlg-f' }, buttons.map(b => h('button', {
      class: 'btn ' + (b.kind || ''), type: 'button', text: b.text,
      onclick: async () => { if ((await b.fn?.()) !== false) close(); },
    }))) : null);
  back.append(box);
  if (esc) back.addEventListener('pointerdown', e => { if (e.target === back) close(); });
  document.body.append(back);
  return { el: box, close };
}

export function confirmDialog(title, msg, okText = '确定', danger = false) {
  return new Promise(resolve => {
    let done = false;
    const d = dialog(title, h('p', { class: 'dlg-msg', text: msg }), {
      buttons: [
        { text: '取消', fn: () => { done = true; resolve(false); } },
        { text: okText, kind: danger ? 'danger' : 'primary', fn: () => { done = true; resolve(true); } },
      ],
    });
    const obs = new MutationObserver(() => {
      if (!d.el.isConnected) { obs.disconnect(); if (!done) resolve(false); }
    });
    obs.observe(document.body, { childList: true });
    $('.btn.primary, .btn.danger', d.el)?.focus();
  });
}

export function promptDialog(title, value = '', okText = '保存') {
  return new Promise(resolve => {
    let done = false;
    const input = h('input', { class: 'input', type: 'text', value, maxlength: 120 });
    const finish = v => { done = true; resolve(v); };
    const d = dialog(title, input, {
      buttons: [{ text: '取消', fn: () => finish(null) },
        { text: okText, kind: 'primary', fn: () => finish(input.value.trim() || null) }],
    });
    input.addEventListener('keydown', e => {
      if (e.key === 'Enter' && !e.isComposing) { finish(input.value.trim() || null); d.close(); }
    });
    const obs = new MutationObserver(() => {
      if (!d.el.isConnected) { obs.disconnect(); if (!done) resolve(null); }
    });
    obs.observe(document.body, { childList: true });
    setTimeout(() => { input.focus(); input.select(); }, 30);
  });
}

// ---------- 格式化 ----------

export const fmtTok = n => n == null ? '0' : n >= 1e6 ? (n / 1e6).toFixed(2) + 'M' : n >= 1e4 ? Math.round(n / 1e3) + 'k'
  : n >= 1e3 ? (n / 1e3).toFixed(1) + 'k' : String(n);
export const fmtDur = ms => ms < 1000 ? '<1 秒' : ms < 60000 ? (ms / 1000).toFixed(ms < 10000 ? 1 : 0) + ' 秒'
  : `${Math.floor(ms / 60000)} 分 ${Math.round(ms % 60000 / 1000)} 秒`;
export const fmtCost = c => c >= 1 ? c.toFixed(2) : c >= 0.01 ? c.toFixed(3) : c >= 0.0001 ? c.toFixed(4) : c > 0 ? '<0.0001' : '0';
export const fmtSize = b => b >= 1048576 ? (b / 1048576).toFixed(1) + ' MB' : b >= 1024 ? Math.round(b / 1024) + ' KB' : b + ' B';
export function fmtTime(ts) {
  const d = new Date(ts * 1000), now = new Date();
  const p = n => String(n).padStart(2, '0');
  const hm = `${p(d.getHours())}:${p(d.getMinutes())}`;
  if (d.toDateString() === now.toDateString()) return hm;
  if (d.getFullYear() === now.getFullYear()) return `${d.getMonth() + 1}月${d.getDate()}日 ${hm}`;
  return `${d.getFullYear()}-${p(d.getMonth() + 1)}-${p(d.getDate())} ${hm}`;
}

export async function copyText(text) {
  try { await navigator.clipboard.writeText(text); return true; } catch {
    const ta = h('textarea', { style: { position: 'fixed', opacity: '0' } });
    ta.value = text;
    document.body.append(ta);
    ta.select();
    let ok = false;
    try { ok = document.execCommand('copy'); } catch { /* ignore */ }
    ta.remove();
    return ok;
  }
}

export const debounce = (fn, ms) => { let t; return (...a) => { clearTimeout(t); t = setTimeout(() => fn(...a), ms); }; };
