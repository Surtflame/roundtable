// 输入区：选模型、互评设置、附件、发送；以及模型选择器。
import { S, on, emit, info, prov, shortName, EFFORT, PROV, cfgFromServer, saveSettings, localGet, localSet, convBusy } from './state.js';
import { h, icon, api, toast, menu, popover, closePopover, promptDialog, fmtSize, debounce } from './util.js';

let box, ta, banner, chipsEl, attsEl, hintEl, barEl, fileInput, sendBtn, addBtn, presetBtn;
let sending = false, tmpSeq = 0, curKey = 'new';
const pendingBy = new Map();
const coarse = () => window.matchMedia('(pointer: coarse)').matches;
const maxModels = () => (S.me && S.me.limits.max_models) || 6;

export function initComposer(el) {
  box = el;
  banner = h('div', { class: 'edit-banner', hidden: true });
  chipsEl = h('div', { class: 'chips' });
  attsEl = h('div', { class: 'atts', hidden: true });
  hintEl = h('div', { class: 'composer-hint', hidden: true });
  ta = h('textarea', { class: 'ta', rows: '1', placeholder: '问点什么…', 'aria-label': '消息' });
  barEl = h('div', { class: 'bar' });
  fileInput = h('input', { type: 'file', multiple: true, hidden: true });
  sendBtn = h('button', { class: 'send', type: 'button' });
  addBtn = h('button', { class: 'chip add', type: 'button', onclick: () => openPicker(addBtn, {
    multi: true, isSel: key => S.cfg.models.some(m => m.key === key), onPick: toggleModel, place: 'up',
  }) }, icon('plus'), h('span', { text: '模型' }));
  presetBtn = h('button', { class: 'chip ghost', type: 'button', onclick: presetMenu }, h('span', { text: '预设' }), icon('down'));
  box.append(banner, chipsEl, attsEl, ta, hintEl, barEl, fileInput);

  const saveDraft = debounce(() => { if (!S.editing) localSet('draft:' + curKey, ta.value || null); }, 400);
  ta.addEventListener('input', () => { grow(); refreshSend(); saveDraft(); });
  ta.addEventListener('keydown', e => {
    if (e.key === 'Escape' && S.editing && !e.isComposing) { e.preventDefault(); return cancelEdit(); }
    if (e.key !== 'Enter'|| e.shiftKey || e.isComposing || e.keyCode === 229) return;
    const mod = e.ctrlKey || e.metaKey;
    if (mod || (S.settings.send_key !== 'ctrl' && !coarse())) { e.preventDefault(); send(); }
  });
  ta.addEventListener('paste', e => {
    const fs = [...(e.clipboardData?.files || [])];
    if (fs.length) { e.preventDefault(); addFiles(fs); }
  });
  fileInput.addEventListener('change', () => { addFiles([...fileInput.files]); fileInput.value = ''; });
  sendBtn.addEventListener('click', () => (convBusy() ? stopAll() : send()));
  on('busy', refreshSend);
  on('settings', renderComposer);
  renderComposer();
}

function grow() {
  ta.style.height = 'auto';
  ta.style.height = Math.min(ta.scrollHeight, Math.round(window.innerHeight * 0.4)) + 'px';
}
export function focusInput() { if (!coarse()) ta.focus(); }

// 切换对话：草稿和待发附件各归各的对话
export function switchConv() {
  pendingBy.set(curKey, S.pending);
  curKey = S.curId || 'new';
  S.pending = pendingBy.get(curKey) || [];
  S.editing = null;
  ta.value = localGet('draft:' + curKey) || '';
  renderComposer();
  grow();
}

// ---------- 配置 ----------

function normalize(c) {
  const models = c.models.slice(0, maxModels());
  let synth = c.synth && models.some(m => m.key === c.synth) ? c.synth : null;
  if (c.synth && !synth && models.length) synth = models[0].key;
  return { models, mode: c.mode === 'review' ? 'review' : 'parallel', rounds: Math.max(1, Math.min(c.rounds || 1, 3)), synth, web: c.web !== false };
}
export function setCfg(c) {
  if (!c) return;
  S.cfg = normalize(c);
  renderComposer();
}

function toggleModel(key) {
  const c = S.cfg, i = c.models.findIndex(m => m.key === key);
  if (i >= 0) c.models.splice(i, 1);
  else {
    if (c.models.length >= maxModels()) return toast(`一次最多 ${maxModels()} 个模型`);
    c.models.push({ key, effort: '' });
    if (c.models.length === 2) {
      c.mode = localGet('multiMode') === 'parallel' ? 'parallel' : 'review';
      if (c.mode === 'review' && !c.synth) c.synth = c.models[0].key;
    }
  }
  setCfg(c);
}

export function applyPreset(p) {
  const ms = (p.models || []).filter(m => m && S.map.has(m.key)).map(m => ({ key: m.key, effort: m.effort || '' }));
  const miss = (p.models || []).length - ms.length;
  if (!ms.length) return toast('这个预设里的模型现在都不可用', 'err');
  if (miss) toast(`预设里有 ${miss} 个模型现在不可用，已跳过`);
  setCfg({ models: ms, mode: p.mode === 'review' ? 'review' : 'parallel', rounds: p.rounds || 1,
    synth: p.mode === 'review' ? (p.synth || null) : null, web: S.cfg.web });
  focusInput();
}

function presetMenu() {
  const ps = S.settings.presets || [];
  menu(presetBtn, [
    ...ps.map(p => ({ text: p.name, hint: p.models.length > 1 ? `${p.models.length} 个${p.mode === 'review' ? ' · 互评' : ''}` : '', fn: () => applyPreset(p) })),
    ps.length ? '-' : null,
    { text: '把当前组合存为预设…', icon: 'star', fn: async () => {
      const c = S.cfg;
      if (!c.models.length) return toast('先选模型');
      const def = c.models.map(m => shortName(m.key)).join(' + ') + (c.models.length > 1 && c.mode === 'review' ? ' 互评' : '');
      const name = await promptDialog('预设名称', def.slice(0, 60));
      if (!name) return;
      const multi = c.models.length > 1;
      const p = { name, models: c.models.map(m => ({ key: m.key, ...(m.effort ? { effort: m.effort } : {}) })), mode: multi ? c.mode : 'single' };
      if (multi && c.mode === 'review') { p.rounds = c.rounds; if (c.synth) p.synth = c.synth; }
      await saveSettings({ presets: [...ps.filter(x => x.name !== name), p] });
      toast('已保存');
    } },
  ], { place: 'up' });
}

// ---------- 渲染 ----------

export function renderComposer() {
  if (!box) return;
  const c = S.cfg, n = c.models.length;

  banner.hidden = !S.editing;
  if (S.editing) {
    banner.replaceChildren(icon('edit'), h('span', { class: 'grow', text: '正在修改这条消息。发送后，它和它之后的内容会被新的回答替换。' }),
      h('button', { class: 'link-btn', type: 'button', text: '取消', onclick: cancelEdit }));
  }

  chipsEl.replaceChildren(...c.models.map(m => {
    const mi = info(m.key);
    const chip = h('button', { class: 'chip model', type: 'button', data: { prov: prov(m.key) }, title: mi.name + (mi.missing ? '（已下架）' : '') },
      h('span', { class: 'dot' }), h('span', { class: 'chip-name', text: shortName(m.key) }),
      m.effort ? h('span', { class: 'chip-effort', text: EFFORT[m.effort] || m.effort }) : null, icon('down'));
    chip.addEventListener('click', () => {
      const efforts = mi.efforts || [];
      menu(chip, [
        { label: `${PROV[prov(m.key)]?.sub || ''} · ${PROV[prov(m.key)]?.note || ''}` },
        ...(efforts.length ? [{ label: '思考强度' },
          { text: '默认' + (mi.default_effort ? `（${EFFORT[mi.default_effort] || mi.default_effort}）` : ''), checked: !m.effort, fn: () => { m.effort = ''; setCfg(c); } },
          ...efforts.map(e => ({ text: EFFORT[e] || e, checked: m.effort === e, fn: () => { m.effort = e; setCfg(c); } })), '-'] : []),
        { text: '移除', icon: 'x', fn: () => toggleModel(m.key) },
      ], { place: 'up' });
    });
    return chip;
  }), addBtn, presetBtn);
  addBtn.querySelector('span:last-child').textContent = n ? '模型' : '选择模型';

  renderAtts();

  const kids = [];
  kids.push(h('button', { class: 'tool', type: 'button', title: '添加图片或文件（也可以直接粘贴、拖进来）', 'aria-label': '添加附件', onclick: () => fileInput.click() }, icon('clip')));
  kids.push(h('button', { class: 'tool txt' + (c.web ? ' on' : ''), type: 'button', 'aria-pressed': String(c.web),
    title: c.web ? '已允许联网搜索，点击关闭' : '已关闭联网搜索，点击开启',
    onclick: () => { c.web = !c.web; renderComposer(); } }, icon('globe'), h('span', { text: '联网' })));
  if (n > 1) {
    const seg = (mode, text, title) => h('button', { class: 'seg-b' + (c.mode === mode ? ' on' : ''), type: 'button', title,
      onclick: () => {
        c.mode = mode;
        localSet('multiMode', mode);
        if (mode === 'review' && !c.synth) c.synth = c.models[0].key;
        renderComposer();
      } }, text);
    kids.push(h('div', { class: 'seg', role: 'group' },
      seg('parallel', '各答各的', '每个模型独立回答，并排对比'),
      seg('review', '互评', '先各自回答，再互相审阅对方的答案并修订，最后合成一份结论')));
    if (c.mode === 'review') {
      const b = h('button', { class: 'tool txt opt', type: 'button', title: '互评轮数与综合人' },
        h('span', { text: `${c.rounds} 轮 · ${c.synth ? shortName(c.synth) + ' 综合' : '不综合'}` }), icon('down'));
      b.addEventListener('click', () => menu(b, [
        { label: '互评几轮' },
        ...[1, 2].map(r => ({ text: `${r} 轮`, hint: r === 2 ? '更慢、更费额度' : '', checked: c.rounds === r, fn: () => { c.rounds = r; renderComposer(); } })),
        { label: '最后由谁综合成一份结论' },
        ...c.models.map(m => ({ text: shortName(m.key), checked: c.synth === m.key, fn: () => { c.synth = m.key; renderComposer(); } })),
        { text: '不综合', checked: !c.synth, fn: () => { c.synth = null; renderComposer(); } },
      ], { place: 'up' }));
      kids.push(b);
    }
  }
  kids.push(h('span', { class: 'grow' }), sendBtn);
  barEl.replaceChildren(...kids);

  const blind = S.pending.some(p => (p.file ? p.file.kind === 'image' : p.url)) ? c.models.filter(m => info(m.key).vision === false) : [];
  hintEl.hidden = !blind.length;
  hintEl.textContent = blind.length ? `${blind.map(m => shortName(m.key)).join('、')} 不能看图，它${blind.length > 1 ? '们' : ''}只会收到文字部分。` : '';
  ta.placeholder = S.cur && S.cur.turns.length ? '接着问…' : (n > 1 ? `同时问 ${n} 个模型…` : '问点什么…');
  refreshSend();
}

export function refreshSend() {
  if (!sendBtn) return;
  const busy = convBusy();
  const up = S.pending.some(p => p.uploading);
  const empty = !ta.value.trim() && !S.pending.some(p => p.id);
  sendBtn.replaceChildren(icon(busy ? 'stop' : 'send'));
  sendBtn.classList.toggle('stop', busy);
  sendBtn.disabled = !busy && (sending || up || empty || !S.cfg.models.length);
  sendBtn.title = busy ? '停止生成' : up ? '附件还在上传' : !S.cfg.models.length ? '先选一个模型' : '发送';
  sendBtn.setAttribute('aria-label', sendBtn.title);
}

// ---------- 附件 ----------

function renderAtts() {
  attsEl.hidden = !S.pending.length;
  attsEl.replaceChildren(...S.pending.map(p => {
    const f = p.file;
    const src = p.url || (f && f.kind === 'image' ? `/api/files/${f.id}` : null);
    const sub = p.uploading ? '上传中…' : f ? (f.kind === 'pdf' ? `PDF${f.pages ? ` · ${f.pages} 页` : ''}` : f.kind === 'image' ? fmtSize(f.size) : `${fmtSize(f.size)}${f.truncated ? ' · 已截断' : ''}`) : '';
    return h('div', { class: 'att' + (p.uploading ? ' up' : ''), title: p.name },
      src ? h('img', { src, alt: '' }) : h('span', { class: 'att-ic' }, icon('file')),
      h('span', { class: 'att-t' }, h('span', { class: 'att-name', text: p.name }), h('span', { class: 'att-sub' }, p.uploading ? h('span', { class: 'spin' }) : null, sub)),
      h('button', { class: 'att-x', type: 'button', title: '移除', 'aria-label': '移除附件', onclick: () => removeAtt(p) }, icon('x')));
  }));
}

function removeAtt(p) {
  S.pending.splice(S.pending.indexOf(p), 1);
  p.gone = true;
  if (p.url) URL.revokeObjectURL(p.url);
  if (p.id && !p.keep) api('DELETE', `/api/files/${p.id}`).catch(() => {});
  renderComposer();
}

export function addFiles(list) {
  const lim = (S.me && S.me.limits.max_upload) || 50 * 1048576;
  for (const f of list) {
    if (S.pending.length >= 20) { toast('一条消息最多 20 个附件'); break; }
    if (f.size > lim) { toast(`${f.name} 超过 ${fmtSize(lim)}，没有添加`, 'err'); continue; }
    if (!f.size) { toast(`${f.name || '文件'} 是空的`, 'err'); continue; }
    const p = { tmp: ++tmpSeq, name: f.name || '粘贴的图片.png', size: f.size, uploading: true,
      url: f.type.startsWith('image/') && !/heic|heif|tiff/.test(f.type) ? URL.createObjectURL(f) : null };
    S.pending.push(p);
    upload(p, f, S.pending);
  }
  renderComposer();
}

async function upload(p, f, arr) {
  const fd = new FormData();
  fd.append('file', f, p.name);
  try {
    const r = await api('POST', '/api/files', fd);
    Object.assign(p, { id: r.id, file: r, uploading: false });
    if (p.gone) api('DELETE', `/api/files/${r.id}`).catch(() => {});
  } catch (e) {
    toast(`${p.name}：${e.message}`, 'err');
    const i = arr.indexOf(p);
    if (i >= 0) arr.splice(i, 1);
  }
  renderComposer();
}

// ---------- 发送 / 停止 / 编辑 ----------

async function send() {
  if (sending || convBusy()) return;
  const c = S.cfg, n = c.models.length;
  if (!n) return toast('先选一个模型');
  if (S.pending.some(p => p.uploading)) return toast('附件还在上传，稍等');
  const text = ta.value.trim();
  const atts = S.pending.filter(p => p.id).map(p => p.id);
  if (!text && !atts.length) return;
  const mode = n > 1 ? c.mode : 'single';
  const sm = mode === 'review' && c.synth ? c.models.find(m => m.key === c.synth) : null;
  const replaced = S.editing;
  const body = { conv_id: S.curId, text, attachments: atts, models: c.models, mode, rounds: c.rounds,
    synth: sm ? { key: sm.key, effort: sm.effort } : null, web: c.web, replace_from: replaced };
  sending = true;
  refreshSend();
  let r;
  try { r = await api('POST', '/api/turns', body); } catch (e) { toast(e.message, 'err'); return; } finally { sending = false; refreshSend(); }
  localSet('draft:' + curKey, null);
  for (const p of S.pending) if (p.url) URL.revokeObjectURL(p.url);
  S.pending = [];
  pendingBy.delete(curKey);
  S.editing = null;
  ta.value = '';
  grow();
  S.live.add(r.turn_id);
  renderComposer();
  emit('sent', r, !!replaced);
}

function stopAll() {
  if (!S.cur) return;
  for (const t of S.cur.turns) if (t.busy) api('POST', `/api/turns/${t.id}/stop`).catch(e => toast(e.message, 'err'));
}

export function startEdit(t) {
  if (convBusy()) return toast('先等这一轮结束，或者点停止');
  S.editing = t.id;
  S.pending = t.attachments.map(f => ({ id: f.id, file: f, name: f.name, size: f.size, keep: true }));
  ta.value = t.user_text || '';
  const c = cfgFromServer(t.config);
  if (c) S.cfg = normalize(c);
  renderComposer();
  grow();
  ta.focus();
  ta.setSelectionRange(ta.value.length, ta.value.length);
}

function cancelEdit() {
  S.editing = null;
  S.pending = [];
  ta.value = localGet('draft:' + curKey) || '';
  renderComposer();
  grow();
}

// ---------- 模型选择器 ----------

const fmtPrice = p => (p >= 100 ? p.toFixed(0) : p >= 1 ? String(+p.toFixed(2)) : String(+p.toFixed(3)));
function modelSub(m) {
  if (m.provider !== 'or') return m.desc || '';
  const bits = [];
  if (m.price_in != null && m.price_out != null) bits.push(m.price_in === 0 && m.price_out === 0 ? '免费' : `$${fmtPrice(m.price_in)} / $${fmtPrice(m.price_out)} 每百万 token`);
  if (m.ctx) bits.push(`上下文 ${m.ctx >= 1e6 ? +(m.ctx / 1e6).toFixed(1) + 'M' : Math.round(m.ctx / 1000) + 'k'}`);
  return bits.join(' · ');
}

export function openPicker(anchor, { multi = false, isSel = () => false, onPick, exclude = new Set(), place = 'up' } = {}) {
  const q = h('input', { class: 'input pick-q', type: 'search', placeholder: '搜索模型名，例如 gemini、grok、deepseek', 'aria-label': '搜索模型', autocomplete: 'off' });
  const list = h('div', { class: 'pick-list' });
  const pop = popover(anchor, h('div', { class: 'picker' }, q, list), { cls: 'picker-pop sheet', place });
  if (!pop) return;

  const row = m => {
    const ex = exclude.has(m.key), sel = isSel(m.key);
    const fav = (S.settings.or_favorites || []).includes(m.key);
    const el = h('div', { class: 'pick-row' + (sel ? ' sel' : '') + (ex ? ' ex' : ''), role: 'option', 'aria-selected': String(sel), tabindex: ex ? null : '0', data: { prov: m.provider } },
      h('span', { class: 'dot' }),
      h('span', { class: 'pick-t' },
        h('span', { class: 'pick-name' }, h('span', { text: m.name }),
          m.image_out ? h('span', { class: 'badge', text: '能出图' }) : null,
          m.vision === false ? h('span', { class: 'badge dim', text: '不看图' }) : null,
          ex ? h('span', { class: 'badge dim', text: '已在这一轮' }) : null),
        h('span', { class: 'pick-sub', text: modelSub(m) })),
      m.provider === 'or' ? h('button', { class: 'star' + (fav ? ' on' : ''), type: 'button', title: fav ? '取消收藏' : '收藏（常驻在列表里）',
        onclick: e => {
          e.stopPropagation();
          const cur = S.settings.or_favorites || [];
          S.settings.or_favorites = fav ? cur.filter(k => k !== m.key) : [...cur, m.key];
          draw();
          saveSettings({ or_favorites: S.settings.or_favorites });
        } }, icon('star')) : null,
      h('span', { class: 'pick-check' }, sel ? icon('check') : null));
    if (!ex) {
      const pick = () => {
        onPick(m.key);
        if (multi) draw(); else closePopover();
      };
      el.addEventListener('click', pick);
      el.addEventListener('keydown', e => { if (e.key === 'Enter' && e.target === el) { e.preventDefault(); pick(); } });
    }
    return el;
  };

  function draw() {
    const term = q.value.trim().toLowerCase();
    const words = term.split(/\s+/).filter(Boolean);
    const hit = m => words.every(w => (m.name + ' ' + m.id).toLowerCase().includes(w));
    const kids = [];
    const sect = (title, note, ms) => { if (ms.length) kids.push(h('div', { class: 'pick-sect' }, h('span', { text: title }), note ? h('span', { class: 'pick-note', text: note }) : null), ...ms.map(row)); };
    const bk = S.cat.backends || {};
    const off = (name, b) => { if (b && !b.ok && !words.length) kids.push(h('div', { class: 'pick-empty', text: `${name} 没有启用：${b.why}` })); };
    sect('Claude', PROV.claude.note, S.cat.claude.filter(hit));
    off('Claude', bk.claude);
    sect('ChatGPT', PROV.codex.note, S.cat.codex.filter(hit));
    off('ChatGPT', bk.codex);
    const favKeys = S.settings.or_favorites || [];
    const favs = favKeys.map(k => S.map.get(k)).filter(m => m && hit(m));
    if (!S.cat.openrouter_ready) { off('OpenRouter', bk.openrouter || { ok: false, why: '没有配置 OPENROUTER_API_KEY' }); }
    else if (words.length) {
      const rest = S.cat.openrouter.filter(m => hit(m) && !favKeys.includes(m.key));
      sect('OpenRouter', PROV.or.note, [...favs, ...rest.slice(0, 80)]);
      if (rest.length > 80) kids.push(h('div', { class: 'pick-empty', text: `还有 ${rest.length - 80} 个匹配，再输入几个字缩小范围。` }));
    } else {
      sect('OpenRouter 收藏', PROV.or.note, favs);
      kids.push(h('div', { class: 'pick-empty', text: `OpenRouter 共 ${S.cat.openrouter.length} 个模型，在上面输入名字查找，点星标收藏。` }));
    }
    if (words.length && !kids.some(k => k.classList.contains('pick-row'))) kids.unshift(h('div', { class: 'pick-empty', text: '没有匹配的模型' }));
    list.replaceChildren(...kids);
  }
  q.addEventListener('input', draw);
  draw();
  if (!coarse()) q.focus();
}
