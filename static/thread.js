// 对话正文：用户消息、各阶段、回答卡片，以及流式更新。
import { S, emit, info, prov, shortName, layoutTabs, EFFORT, findTurn } from './state.js';
import { h, icon, api, toast, menu, copyText, fmtTok, fmtDur, fmtCost, fmtSize, fmtTime } from './util.js';
import { renderMarkdown } from './md.js';
import { synthInstrItem } from './settings.js';

let root, scroller, jump, hooks = {};
const refs = new Map();      // 回答 id → 卡片里各部分的节点
const timers = new Map();    // 回答 id → 待执行的重绘
let stick = true;

const MARK = '<svg viewBox="0 0 64 64" width="56" height="56" aria-hidden="true"><circle cx="32" cy="32" r="19" fill="none" stroke="currentColor" stroke-width="2.5" opacity=".35"/><circle cx="32" cy="13" r="6.500" fill="#d97757"/><circle cx="48.500" cy="41.500" r="6.500" fill="#10a37f"/><circle cx="15.500" cy="41.500" r="6.500" fill="#6d7cff"/></svg>';

export function initThread(opt) {
  ({ root, scroller, jump, hooks } = opt);
  scroller.addEventListener('scroll', () => { stick = nearBottom(); jump.hidden = stick; }, { passive: true });
  jump.addEventListener('click', () => toBottom());
  root.addEventListener('load', keep, true);
  root.addEventListener('click', e => {
    const b = e.target.closest('.copy-code');
    if (!b) return;
    const code = b.closest('.code').querySelector('code');
    copyText(code.textContent).then(ok => {
      b.textContent = ok ? '已复制' : '复制失败';
      setTimeout(() => { b.textContent = '复制'; }, 1200);
    });
  });
  setInterval(() => {
    const now = Date.now() / 1000;
    for (const el of root.querySelectorAll('.elapsed')) el.textContent = fmtElapsed(now - Number(el.dataset.t0));
  }, 1000);
}

const nearBottom = () => scroller.scrollHeight - scroller.scrollTop - scroller.clientHeight < 70;
function keep() { if (stick) scroller.scrollTop = scroller.scrollHeight; }
export { keep as stickBottom };
export function toBottom() { stick = true; scroller.scrollTop = scroller.scrollHeight; jump.hidden = true; }
const fmtElapsed = s => { s = Math.max(0, Math.round(s)); return s < 60 ? `${s} 秒` : `${Math.floor(s / 60)}:${String(s % 60).padStart(2, '0')}`; };
const uiOf = id => { if (!S.ui.has(id)) S.ui.set(id, {}); return S.ui.get(id); };
function clearTimer(id) { if (timers.has(id)) { clearTimeout(timers.get(id)); timers.delete(id); } }

// ---------- 整体 ----------

export function renderThread() {
  refs.clear();
  for (const t of timers.values()) clearTimeout(t);
  timers.clear();
  if (!S.curId) { root.replaceChildren(hero()); return; }
  if (!S.cur) { root.replaceChildren(h('div', { class: 'loading' }, h('span', { class: 'spin' }), '载入中…')); return; }
  if (!S.cur.turns.length) { root.replaceChildren(h('div', { class: 'loading', text: '这个对话还没有内容' })); return; }
  root.replaceChildren(...S.cur.turns.map(turnEl));
}

function hero() {
  const ps = (S.settings.presets || []).filter(p => p && Array.isArray(p.models) && p.models.length);
  return h('div', { class: 'hero' },
    h('div', { class: 'hero-mark', html: MARK }),
    h('h1', { text: '今天想让谁来回答？' }),
    h('p', { text: '在下面选一个或几个模型。多个模型可以各答各的，也可以互相评审，最后合成一份结论。' }),
    S.map.size ? null : h('p', { class: 'hero-warn', text: '还没有可用的模型：Claude Code、Codex、OpenRouter 至少要配好一个。原因见左下角设置里的「用量」，配法见 docs/backends.md。' }),
    ps.length ? h('div', { class: 'hero-presets' }, ps.map(p => h('button', { class: 'preset', type: 'button', onclick: () => hooks.applyPreset(p) },
      h('span', { class: 'preset-dots' }, p.models.map(m => h('span', { class: 'dot', data: { prov: prov(m.key) } }))),
      h('span', { class: 'preset-name', text: p.name }),
      h('span', { class: 'preset-sub', text: p.models.map(m => shortName(m.key)).join(' + ') + (p.models.length > 1 ? (p.mode === 'review' ? ' · 互评' : ' · 各答各的') : '') })))) : null);
}

// ---------- 用户消息 ----------

function fileChip(f) {
  if (f.kind === 'image') {
    return h('button', { class: 'thumb', type: 'button', title: f.name, onclick: () => lightbox(f) },
      h('img', { src: `/api/files/${f.id}`, alt: f.name, loading: 'lazy' }));
  }
  const sub = f.kind === 'pdf' ? `PDF${f.pages ? ` · ${f.pages} 页` : ''}${f.scanned ? ' · 扫描件' : ''}`
    : `${f.chars != null ? fmtTok(f.chars) + ' 字' : fmtSize(f.size)}${f.truncated ? ' · 已截断' : ''}`;
  return h('a', { class: 'file-chip', href: `/api/files/${f.id}${f.kind === 'pdf' ? '' : '?dl=1'}`, target: '_blank', rel: 'noopener', title: f.name },
    icon('file'), h('span', { class: 'file-t' }, h('span', { class: 'file-name', text: f.name }), h('span', { class: 'file-sub', text: sub })));
}

function opBtn(ic, title, fn) {
  const b = h('button', { class: 'op', type: 'button', title, 'aria-label': title }, icon(ic));
  b.addEventListener('click', e => fn(e, b));
  return b;
}
function flashCopied(b) {
  const old = b.firstChild;
  b.replaceChildren(icon('check'));
  setTimeout(() => b.replaceChildren(old), 1200);
}

function userEl(t) {
  const text = t.user_text || '';
  const long = text.length > 1800 || text.split('\n').length > 28;
  const bubble = text ? h('div', { class: 'bubble' + (long ? ' clamp' : ''), text }) : null;
  const more = long ? h('button', { class: 'link-btn', type: 'button', text: '展开全文', onclick: () => {
    const c = bubble.classList.toggle('clamp');
    more.textContent = c ? '展开全文' : '收起';
  } }) : null;
  return h('div', { class: 'user' },
    t.attachments.length ? h('div', { class: 'user-atts' }, t.attachments.map(fileChip)) : null,
    bubble, more,
    h('div', { class: 'user-ops' },
      h('span', { class: 'time', text: fmtTime(t.created_at) }),
      text ? opBtn('copy', '复制', (e, b) => copyText(text).then(ok => ok && flashCopied(b))) : null,
      opBtn('edit', '编辑后重新发送', () => hooks.startEdit(t))));
}

// ---------- 阶段 ----------

function stageGroups(t) {
  const m = new Map();
  for (const r of t.responses) {
    const k = `${r.stage}:${r.round}`;
    if (!m.has(k)) m.set(k, { key: k, stage: r.stage, round: r.round, rs: [] });
    m.get(k).rs.push(r);
  }
  const gs = [...m.values()].sort((a, b) => a.round - b.round);
  const reviews = gs.filter(g => g.stage === 'review');
  const synths = gs.filter(g => g.stage === 'synth');
  for (const g of gs) {
    g.rs.sort((a, b) => a.pos - b.pos);
    if (g.stage === 'answer') g.title = g.rs.length > 1 ? '各自回答' : '回答';
    else if (g.stage === 'review') g.title = reviews.length > 1 ? `第 ${reviews.indexOf(g) + 1} 轮互评与修订` : '互评与修订';
    else g.title = synths.length > 1 ? `综合（第 ${synths.indexOf(g) + 1} 版）` : '综合结论';
  }
  return gs;
}

function stageIsOpen(t, g, groups) {
  const k = `${t.id}|${g.key}`;
  if (S.stageOpen.has(k)) return S.stageOpen.get(k);
  if (groups.length === 1) return true;
  if (g.rs.every(r => r.status === 'pending')) return false;
  if (S.live.has(t.id)) return true;
  const last = [...groups].reverse().find(x => x.rs.some(r => r.status !== 'pending' && r.status !== 'skipped'));
  return g === last;
}

function selTab(t, g) {
  const id = S.tabSel.get(`${t.id}|${g.key}`);
  return g.rs.some(r => r.id === id) ? id : g.rs[0].id;
}

function stageSummary(g) {
  const n = g.rs.length, c = st => g.rs.filter(r => r.status === st).length;
  const run = c('running'), pend = c('pending'), err = c('error');
  if (run) return n > 1 ? `生成中 · 已完成 ${n - run - pend}/${n}` : '生成中';
  if (pend === n) return '等上一步完成';
  if (pend) return `排队中 ${pend}`;
  const bits = [];
  if (n > 1) bits.push(`${n} 个模型`);
  if (err) bits.push(`${err} 个出错`);
  if (c('stopped')) bits.push('已停止');
  if (c('skipped') === n) bits.push('已跳过');
  return bits.join(' · ');
}

function decorateStage(t, g, groups, sEl) {
  const tabs = layoutTabs() && g.rs.length > 1;
  const open = stageIsOpen(t, g, groups);
  const k = `${t.id}|${g.key}`;
  sEl.className = `stage ${open ? 'open' : 'closed'} ${tabs ? 'tabs' : 'cols'}${groups.length > 1 ? ' titled' : ''}`;
  const parts = [];
  if (groups.length > 1) {
    const running = g.rs.some(r => r.status === 'running');
    parts.push(h('button', { class: 'stage-h', type: 'button', 'aria-expanded': String(open), onclick: () => { S.stageOpen.set(k, !open); refreshChrome(t); } },
      icon('chev', 'chev'), h('span', { class: 'stage-title', text: g.title }),
      h('span', { class: 'stage-dots' }, g.rs.map(r => h('span', { class: 'dot', data: { prov: prov(r.model) }, title: r.label }))),
      running ? h('span', { class: 'spin' }) : null,
      h('span', { class: 'stage-sum', text: stageSummary(g) })));
  }
  const sel = tabs ? selTab(t, g) : null;
  if (tabs && open) {
    parts.push(h('div', { class: 'tabbar', role: 'tablist' }, g.rs.map(r => h('button', {
      class: 'tab' + (r.id === sel ? ' sel' : ''), type: 'button', role: 'tab', data: { prov: prov(r.model) },
      onclick: () => { S.tabSel.set(k, r.id); refreshChrome(t); },
    }, h('span', { class: 'dot' }), h('span', { text: shortName(r.model, r.label) }),
    r.status === 'running' ? h('span', { class: 'spin' }) : r.status === 'error' ? h('span', { class: 'tab-err', text: '!' }) : null))));
  }
  sEl.querySelector(':scope > .stage-top').replaceChildren(...parts);
  for (const c of sEl.querySelectorAll(':scope > .stage-b > .resp')) c.classList.toggle('sel', !tabs || c.dataset.rid === sel);
}

function turnEl(t) {
  const groups = stageGroups(t);
  const wide = !layoutTabs() && groups.some(g => g.rs.length > 1);
  const solo = t.responses.length === 1;
  // --n = 这一轮里最多并排几列，app.css 用它决定这一轮能占多宽
  return h('section', { class: 'turn' + (wide ? ' wide' : ''), data: { tid: t.id }, style: `--n:${Math.max(...groups.map(g => g.rs.length))}` },
    userEl(t),
    h('div', { class: 'stages' }, groups.map(g => {
      const sEl = h('div', { class: 'stage', data: { sk: g.key } },
        h('div', { class: 'stage-top' }),
        h('div', { class: 'stage-b' + (g.rs.length === 1 ? ' n1' : ''), style: `--n:${g.rs.length}` }, g.rs.map(r => respEl(t, r, solo))));
      decorateStage(t, g, groups, sEl);
      return sEl;
    })),
    turnActions(t));
}

function refreshTurn(t) {
  const old = root.querySelector(`.turn[data-tid="${t.id}"]`);
  if (!old) return renderThread();
  for (const r of t.responses) clearTimer(r.id);
  old.replaceWith(turnEl(t));
}

function refreshChrome(t) {
  const el = root.querySelector(`.turn[data-tid="${t.id}"]`);
  if (!el) return;
  const groups = stageGroups(t);
  const els = [...el.querySelectorAll(':scope > .stages > .stage')];
  if (els.length !== groups.length || groups.some((g, i) => els[i].dataset.sk !== g.key)) return refreshTurn(t);
  groups.forEach((g, i) => decorateStage(t, g, groups, els[i]));
  el.querySelector(':scope > .turn-actions').replaceWith(turnActions(t));
}

// ---------- 回合下方的追加操作 ----------

async function extend(t, action, models) {
  S.live.add(t.id);
  stick = true;
  try { await api('POST', `/api/turns/${t.id}/extend`, { action, models }); } catch (e) { toast(e.message, 'err'); }
}

function turnActions(t) {
  const el = h('div', { class: 'turn-actions' });
  if (!S.cur || S.cur.turns[S.cur.turns.length - 1] !== t || t.busy) return el;
  const usable = t.responses.filter(r => r.stage !== 'synth' && (r.status === 'done' || r.status === 'stopped') && (r.text || (r.images || []).length))
    .sort((a, b) => a.round - b.round);
  const latest = new Map();
  for (const r of usable) latest.set(r.model, r);
  const ms = [...latest.values()].sort((a, b) => a.pos - b.pos).map(r => ({ key: r.model, effort: r.effort || '' }));
  const answered = new Set(t.responses.filter(r => r.stage === 'answer').map(r => r.model));
  const abtn = (ic, text, fn) => h('button', { class: 'act-btn', type: 'button', onclick: fn }, icon(ic), text);
  el.append(abtn('plus', '让其他模型也回答', e => hooks.openPicker(e.currentTarget, {
    multi: false, exclude: answered, place: 'auto', onPick: key => extend(t, 'answer', [{ key, effort: '' }]),
  })));
  if (ms.length >= 2) {
    const cands = [...ms];
    for (const m of S.cfg.models) if (!cands.some(c => c.key === m.key)) cands.push(m);
    el.append(abtn('swap', '再互评一轮', () => extend(t, 'review', ms)));
    el.append(abtn('merge', '综合成一份', e => menu(e.currentTarget, [{ label: '由谁来综合' },
      ...cands.map(m => ({ text: info(m.key).name, fn: () => extend(t, 'synth', [m]) })), '-', synthInstrItem()])));
  }
  return el;
}

// ---------- 回答卡片 ----------

function respEl(t, r, plain) {
  const el = h('article', { class: `resp ${plain ? 'plain' : 'card'}`, data: { rid: r.id, prov: prov(r.model) } });
  const ref = {
    el, status: h('span', { class: 'resp-st' }), think: h('div', { class: 'fold think' }), acts: h('div', { class: 'fold acts' }),
    md: h('div', { class: 'md' }), imgs: h('div', { class: 'imgs' }), err: h('div', { class: 'resp-err' }),
    foot: h('footer', { class: 'resp-f' }),
  };
  el.append(...[h('header', { class: 'resp-h' }, h('span', { class: 'dot' }),
    h('span', { class: 'resp-name', text: r.label || info(r.model).name }),
    r.effort ? h('span', { class: 'badge', text: '思考 ' + (EFFORT[r.effort] || r.effort) }) : null, ref.status),
  r.stage === 'synth' && r.instr ? h('button', { class: 'resp-instr', type: 'button', title: '这份综合稿是按这条要求写的（点一下展开 / 收起）',
    onclick: e => e.currentTarget.classList.toggle('open') }, h('span', { class: 'k', text: '要求' }), h('span', { class: 'v', text: r.instr })) : null,
  ref.think, ref.acts, ref.md, ref.imgs, ref.err, ref.foot].filter(Boolean));   // append(null) 会写出 “null” 四个字
  refs.set(r.id, ref);
  paintStatus(r); paintThink(r); paintActs(r); paintBody(r); paintImgs(r); paintFoot(t, r);
  return el;
}

function paintStatus(r) {
  const ref = refs.get(r.id);
  if (!ref) return;
  ref.el.dataset.st = r.status;
  const s = ref.status;
  if (r.status === 'running') {
    s.replaceChildren(h('span', { class: 'spin' }), h('span', { class: 'elapsed', data: { t0: r.started_at || Date.now() / 1000 },
      text: fmtElapsed(Date.now() / 1000 - (r.started_at || Date.now() / 1000)) }));
  } else s.textContent = { pending: '排队中', stopped: '已停止', error: '出错', skipped: '已跳过' }[r.status] || '';
  ref.err.hidden = !(r.status === 'error' && r.error);
  ref.err.textContent = r.error || '';
}

function fold(box, open, ic, label, side, onToggle, body) {
  box.replaceChildren(...[
    h('button', { class: 'fold-h' + (open ? ' open' : ''), type: 'button', 'aria-expanded': String(open), onclick: onToggle },
      icon('chev', 'chev'), icon(ic), h('span', { class: 'fold-label', text: label }), side),
    open ? body() : null].filter(Boolean));
}

function paintThink(r) {
  const ref = refs.get(r.id);
  if (!ref) return;
  const txt = r.thinking || '';
  ref.think.hidden = !txt;
  if (!txt) return;
  const ui = uiOf(r.id);
  const live = r.status === 'running' && !r.text;
  fold(ref.think, ui.think, 'brain', live ? '正在思考' : '思考过程',
    h('span', { class: 'fold-prev', text: live && !ui.think ? txt.slice(-140).replace(/\s+/g, ' ') : `${fmtTok(txt.length)} 字` }),
    () => { ui.think = !ui.think; paintThink(r); },
    () => {
      const b = h('div', { class: 'think-b', text: txt });
      if (r.status === 'running') requestAnimationFrame(() => { b.scrollTop = b.scrollHeight; });
      return b;
    });
}

const httpUrl = s => (typeof s === 'string' && /^https?:\/\//i.test(s) ? s : null);
function hostOf(u) { try { return new URL(u).host.replace(/^www\./, ''); } catch { return u; } }

const pretty = s => { try { return JSON.stringify(JSON.parse(s), null, 2); } catch { return s || ''; } };

// 一次 MCP 工具调用：点开看参数和结果
function mcpItem(r, ui, a) {
  const seen = (ui.mcp ||= new Set());
  const open = seen.has(a.id);
  const head = h('button', { class: 'act-item act-mcp' + (a.error ? ' err' : ''), type: 'button', 'aria-expanded': String(open),
    onclick: () => { if (open) seen.delete(a.id); else seen.add(a.id); paintActs(r); } },
  a.pending ? h('span', { class: 'spin' }) : icon('plug'), h('span', { text: a.text }),
  h('span', { class: 'act-host', text: a.pending ? '调用中' : a.error ? '失败' : a.ms != null ? fmtDur(a.ms) : '' }));
  if (!open) return head;
  return [head, h('div', { class: 'act-mcp-b' },
    h('div', { class: 'act-mcp-l', text: '参数' }), h('pre', { text: pretty(a.args) || '（无）' }),
    a.pending ? null : [h('div', { class: 'act-mcp-l', text: a.error ? '出错' : '结果' }), h('pre', { text: a.error || a.result || '（空）' })])];
}

function paintActs(r) {
  const ref = refs.get(r.id);
  if (!ref) return;
  const items = r.activity || [];
  ref.acts.hidden = !items.length;
  if (!items.length) return;
  const ui = uiOf(r.id);
  const n = k => items.filter(a => a.kind === k).length;
  const cites = [...new Map(items.filter(a => a.kind === 'cite' && a.url).map(a => [a.url, a])).values()];
  const bits = [];
  if (n('search')) bits.push(`搜索 ${n('search')} 次`);
  if (n('fetch')) bits.push(`读取 ${n('fetch')} 个网页`);
  if (cites.length) bits.push(`${cites.length} 个来源`);
  if (n('tool')) bits.push(`工具 ${n('tool')} 次`);
  if (n('mcp')) bits.push(`MCP 工具 ${n('mcp')} 次`);
  const last = items[items.length - 1];
  const calling = items.filter(a => a.kind === 'mcp' && a.pending).pop();
  const prev = r.status !== 'running' ? '' : calling ? `正在调用：${calling.text}` : last.kind === 'search' ? `正在搜索：${last.text}`
    : last.kind === 'fetch' ? `正在读取：${hostOf(last.text)}` : '';
  fold(ref.acts, ui.acts, items.every(a => a.kind === 'mcp') ? 'plug' : 'globe', bits.join(' · ') || '联网', prev ? h('span', { class: 'fold-prev', text: prev }) : null,
    () => { ui.acts = !ui.acts; paintActs(r); },
    () => h('div', { class: 'acts-b' }, [...items.filter(a => a.kind !== 'cite'), ...cites].map(a => {
      if (a.kind === 'mcp') return mcpItem(r, ui, a);
      const url = httpUrl(a.url) || (a.kind === 'fetch' ? httpUrl(a.text) : null);
      const label = a.kind === 'cite' ? (a.text || hostOf(a.url)) : a.kind === 'fetch' ? (url ? hostOf(url) + new URL(url).pathname.slice(0, 60) : a.text) : a.text;
      return h('div', { class: 'act-item' }, icon(a.kind === 'search' ? 'search' : a.kind === 'tool' ? 'gear' : 'globe'),
        url ? h('a', { href: url, target: '_blank', rel: 'noopener noreferrer nofollow', text: label || url }) : h('span', { text: label || '' }),
        a.kind === 'cite' && url ? h('span', { class: 'act-host', text: hostOf(url) }) : null);
    })));
}

function paintBody(r) {
  const ref = refs.get(r.id);
  if (!ref) return;
  const txt = r.text || '';
  const running = r.status === 'running';
  ref.md.classList.toggle('streaming', running && !!txt);
  if (txt) ref.md.innerHTML = renderMarkdown(txt);
  else if (running) ref.md.replaceChildren(h('div', { class: 'wait' }, h('i'), h('i'), h('i')));
  else if (r.status === 'skipped') ref.md.replaceChildren(h('p', { class: 'muted', text: '上一步没有可用的内容，这一步跳过了。' }));
  else if ((r.status === 'done' || r.status === 'stopped') && !(r.images || []).length) ref.md.replaceChildren(h('p', { class: 'muted', text: r.status === 'stopped' ? '（还没输出就停止了）' : '（没有输出）' }));
  else ref.md.replaceChildren();
}

function paintImgs(r) {
  const ref = refs.get(r.id);
  if (!ref) return;
  const imgs = r.images || [];
  ref.imgs.hidden = !imgs.length;
  ref.imgs.replaceChildren(...imgs.map(f => h('button', { class: 'gen-img', type: 'button', title: '点击放大', onclick: () => lightbox(f) },
    h('img', { src: `/api/files/${f.id}`, alt: f.name, loading: 'lazy', width: f.w || null, height: f.h || null }))));
}

function paintFoot(t, r) {
  const ref = refs.get(r.id);
  if (!ref) return;
  const kids = [];
  const post = url => api('POST', url).catch(e => toast(e.message, 'err'));
  if (r.status === 'running') {
    kids.push(h('button', { class: 'op txt', type: 'button', onclick: () => post(`/api/responses/${r.id}/stop`) }, icon('stop'), '停止'));
  } else if (r.status === 'done' || r.status === 'stopped' || r.status === 'error') {
    if (r.text) kids.push(opBtn('copy', '复制（Markdown 原文）', (e, b) => copyText(r.text).then(ok => ok && flashCopied(b))));
    if (!t.busy) {
      kids.push(opBtn('refresh', '重新生成', (e, b) => menu(b, [{ text: '重新生成（覆盖这条回答）', icon: 'refresh', fn: () => {
        S.live.add(t.id);
        post(`/api/responses/${r.id}/regenerate`);
      } }])));
    }
    const u = r.usage || {}, bits = [], tip = [];
    if (u.ms) bits.push(fmtDur(u.ms));
    if (u.in || u.out) bits.push(`${fmtTok(u.in || 0)} → ${fmtTok(u.out || 0)} tok`);
    if (u.cost) bits.push(`$${fmtCost(u.cost)}`);
    if (u.searches) bits.push(`搜索 ${u.searches} 次`);
    if (u.cached) tip.push(`其中缓存命中 ${fmtTok(u.cached)} tok`);
    if (u.reasoning) tip.push(`思考 ${fmtTok(u.reasoning)} tok`);
    if (u.cost_est) tip.push(`按 API 价折算约 $${fmtCost(u.cost_est)}（参考值；订阅登录不按这个收费）`);
    if (r.finished_at) tip.push(`完成于 ${fmtTime(r.finished_at)}`);
    if (bits.length) kids.push(h('span', { class: 'meta', text: bits.join(' · '), title: tip.join('\n') || null }));
  }
  ref.foot.hidden = !kids.length;
  ref.foot.replaceChildren(...kids);
}

function queuePaint(r) {
  if (timers.has(r.id)) return;
  const n = (r.text || '').length;
  timers.set(r.id, setTimeout(() => {
    timers.delete(r.id);
    const ref = refs.get(r.id);
    if (!ref) return;
    if (ref.len !== n || ref.len == null) paintBody(r);
    ref.len = (r.text || '').length;
    if (ref.tlen !== (r.thinking || '').length) { paintThink(r); ref.tlen = (r.thinking || '').length; }
    if (ref.len !== n) queuePaint(r);
    keep();
  }, n > 40000 ? 450 : n > 10000 ? 200 : 80));
}

// ---------- 服务器事件 ----------

const mine = ev => ev.conv_id === S.curId && S.cur;
function findResp(tid, rid) {
  const t = findTurn(tid);
  return t && t.responses.find(r => r.id === rid);
}

export function applyTurn(ev) {
  if (!mine(ev)) return;
  const t = ev.turn, ts = S.cur.turns;
  const i = ts.findIndex(x => x.id === t.id);
  if (i >= 0) {
    ts[i] = t;
    if (t.busy) S.live.add(t.id);
    refreshTurn(t);
  } else {
    if (ts.some(x => x.seq >= t.seq)) return emit('resync');
    ts.push(t);
    S.live.add(t.id);
    if (root.querySelector('.turn')) {
      root.append(turnEl(t));
      if (ts.length > 1) refreshChrome(ts[ts.length - 2]);
    } else renderThread();
  }
  keep();
  emit('busy');
}

export function applyResp(ev) {
  if (!mine(ev)) return;
  const t = findTurn(ev.turn_id);
  if (!t) return emit('resync');
  const r = ev.resp;
  const i = t.responses.findIndex(x => x.id === r.id);
  if (i < 0) { t.responses.push(r); refreshTurn(t); return keep(); }
  t.responses[i] = r;
  const ref = refs.get(r.id);
  if (ref && ref.el.isConnected) {
    clearTimer(r.id);
    ref.el.replaceWith(respEl(t, r, ref.el.classList.contains('plain')));
    refreshChrome(t);
  } else refreshTurn(t);
  keep();
}

export function applyDelta(ev) {
  if (!mine(ev)) return;
  const r = findResp(ev.turn_id, ev.id);
  if (!r) return emit('resync');
  const cur = r[ev.k] || '';
  if (ev.off === cur.length) r[ev.k] = cur + ev.s;
  else if (ev.off < cur.length) {
    if (ev.off + ev.s.length <= cur.length) return;
    r[ev.k] = cur + ev.s.slice(cur.length - ev.off);
  } else return emit('resync');
  queuePaint(r);
}

export function applyAct(ev) {
  if (!mine(ev)) return;
  const r = findResp(ev.turn_id, ev.id);
  if (!r) return;
  r.activity ||= [];
  const a = ev.item;
  const i = a.id ? r.activity.findIndex(x => x.id === a.id) : -1;      // MCP 调用：开始和结束是同一条
  if (i >= 0) r.activity[i] = a;
  else if (r.activity.some(x => x.kind === a.kind && x.text === a.text && x.at === a.at)) return;
  else r.activity.push(a);
  paintActs(r);
  keep();
}

export function applyImg(ev) {
  if (!mine(ev)) return;
  const r = findResp(ev.turn_id, ev.id);
  if (!r) return;
  r.images ||= [];
  if (r.images.some(f => f.id === ev.file.id)) return;
  r.images.push(ev.file);
  paintImgs(r);
  paintBody(r);
  keep();
}

export function applyTurnState(ev) {
  if (!mine(ev)) return;
  const t = findTurn(ev.turn_id);
  if (!t) return;
  t.busy = ev.busy;
  refreshChrome(t);
  for (const r of t.responses) paintFoot(t, r);
  keep();
  emit('busy');
}

// ---------- 看大图 ----------

export function lightbox(f) {
  const back = h('div', { class: 'dlg-back lightbox' });
  const close = () => back.remove();
  back.append(h('img', { src: `/api/files/${f.id}`, alt: f.name, onclick: close }),
    h('div', { class: 'lb-bar' }, h('span', { class: 'lb-name', text: f.name + (f.w ? ` · ${f.w}×${f.h}` : '') }),
      h('a', { class: 'btn', href: `/api/files/${f.id}?dl=1`, download: f.name }, '下载'),
      h('button', { class: 'btn', type: 'button', onclick: close }, '关闭')));
  back.addEventListener('click', e => { if (e.target === back) close(); });
  document.body.append(back);
}
