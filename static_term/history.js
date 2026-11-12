// 侧边栏的“历史对话”：Claude Code 和 Codex 存在这台机器上的会话，点一下就在新窗口里接着聊。
// Claude 应用（Remote Control）里的标题和置顶是导进来的一份清单，见 term/history.py 开头的说明。
import { S, on, AGENT } from './state.js';
import { h, icon, api, toast, menu, confirmDialog, fmtAgo, shortPath, debounce } from './util.js';
import { select, refresh as refreshTabs } from './tabs.js';

let root, listEl, qEl, footBtn, hiddenView = false, hiddenCount = 0, loaded = false, items = [];

export function initHistory(el) {
  root = el;
  qEl = h('input', { class: 'input side-q', type: 'search', placeholder: '搜标题、目录', 'aria-label': '搜索历史对话' });
  listEl = h('div', { class: 'side-list' });
  // 底下那一行：平时是“已隐藏 N”，点进去看隐藏的；在隐藏列表里是“← 返回”
  footBtn = h('button', { class: 'hist-foot', type: 'button', onclick: () => { hiddenView = !hiddenView; items = []; load(); } });
  root.append(h('div', { class: 'side-tools' }, qEl, h('button', { class: 'icon-btn', type: 'button', title: '刷新', onclick: () => load() }, icon('refresh'))),
    listEl, footBtn);
  qEl.addEventListener('input', debounce(load, 250));
  on('window', () => { if (loaded && !root.hidden) draw(); });
  on('winclosed', () => { if (loaded) load(true); });           // 关掉一个窗口：哪条对话“已打开”跟着变
  // 页面一直开着的话，隔一阵自己刷新一下（哪个在跑、哪个有新内容会变）
  setInterval(() => { if (loaded && !root.hidden && !document.hidden) load(true); }, 20000);
}

export function showHistory() { if (!loaded) load(); }

async function load(quiet) {
  loaded = true;
  if (!items.length && !quiet) listEl.replaceChildren(h('div', { class: 'pick-empty', text: '载入中…' }));
  let r;
  try { r = await api('GET', `/api/history?q=${encodeURIComponent(qEl.value.trim())}&hidden=${hiddenView ? 1 : 0}`); } catch (e) {
    if (!quiet) listEl.replaceChildren(h('div', { class: 'pick-empty', text: e.message }));
    return;
  }
  items = r.sessions;
  hiddenCount = r.hidden_count || 0;
  draw();
}

const envOf = s => (s.env ? (S.me.envs || []).find(e => e.key === s.env) : null);

// 这条会话现在在哪（服务器算好的，见 app.py 的 _scene）：
//   win 我们自己的某个窗口里（copy＝那只是另开的副本，正主在别处） / attach 这台机器别的 tmux 里，可以直接接进去 /
//   server Remote Control 后台带着（应用里开的，没有终端界面） / other 别的终端里 / 都没有＝没在跑
const where = s => s.at || {};

function badge(s, at) {
  if (at.win) return at.copy ? h('span', { class: 'badge warn', text: '开了份副本' }) : h('span', { class: 'badge', text: '已打开' });
  if (at.attach) return h('span', { class: 'badge', text: s.working ? '正忙 · 可接入' : '在跑 · 可接入' });
  if (at.server) return h('span', { class: 'badge warn', text: s.working ? '应用里正忙' : '应用里开着' });
  if (at.other) return h('span', { class: 'badge warn', text: '别处在跑' });
  return s.busy ? h('span', { class: 'badge warn', text: '刚有动静' }) : null;
}

function draw() {
  footBtn.replaceChildren(...(hiddenView ? [icon('back'), h('span', { text: '返回' })] : [icon('eyeoff'), h('span', { text: `已隐藏${hiddenCount ? ` ${hiddenCount}` : ''}` })]));
  footBtn.hidden = !hiddenView && !hiddenCount;
  const kids = [];
  if (hiddenView) kids.push(h('div', { class: 'hist-note', text: '隐藏的对话只在这里看得到。程序自己开的（脚本、定时任务调的 Claude）默认就在这里。' }));
  if (!items.length) {
    kids.push(h('div', { class: 'pick-empty', text: qEl.value.trim() ? '没有匹配的对话' : hiddenView ? '没有隐藏的对话' : '还没有历史对话。在终端里用 Claude Code 或 Codex 聊过之后，这里会列出来。' }));
    listEl.replaceChildren(...kids);
    return;
  }
  let last = null;
  for (const s of items) {
    const g = s.pinned ? '置顶' : '最近';
    if (!hiddenView && g !== last && (s.pinned || last === '置顶')) kids.push(h('div', { class: 'hist-group', text: g }));
    last = g;
    const at = where(s), env = envOf(s);
    const more = h('button', { class: 'file-more', type: 'button', title: '更多', 'aria-label': '更多' }, icon('more'));
    more.addEventListener('click', e => { e.stopPropagation(); rowMenu(more, s, at); });
    const place = `${env ? env.short + ' · ' : ''}${shortPath(s.cwd, env ? env.home : S.me.home) || '?'}`;
    const row = h('div', { class: 'hist' + (at.win && !at.copy ? ' open' : ''), role: 'button', tabindex: '0', data: { kind: s.agent },
      title: `${s.title || '（没有标题）'}\n${env ? env.label + '：' : ''}${s.cwd}\n${AGENT[s.agent]}${s.turns ? ` · 问过 ${s.turns} 次` : ''}${s.rc ? ' · Claude 应用里也有' : ''}${s.auto ? ' · 程序开的' : ''}\n${s.id}` },
      h('span', { class: 'tab-dot' }),
      h('span', { class: 'hist-t' },
        h('span', { class: 'hist-name', text: s.title || '（没有标题）' }),
        h('span', { class: 'hist-sub' }, h('span', { text: `${fmtAgo(s.mtime)} · ${place}` }),
          hiddenView && s.auto ? h('span', { class: 'badge', text: '程序开的' }) : null, badge(s, at))),
      more);
    row.addEventListener('click', () => resume(s, at));
    row.addEventListener('keydown', e => { if (e.key === 'Enter') resume(s, at); });
    row.addEventListener('contextmenu', e => { e.preventDefault(); rowMenu(more, s, at); });
    kids.push(row);
  }
  listEl.replaceChildren(...kids);
}

function rowMenu(anchor, s, at) {
  const idle = !at.win && !at.attach && !at.server && !at.other;
  menu(anchor, [
    at.win ? { text: at.copy ? '切到这里开的那份副本' : '切到它所在的窗口', icon: 'term', fn: () => select(at.win) }
      : at.attach ? { text: '接进去', icon: 'link', hint: '和应用同步', fn: () => resume(s, at) }
      : { text: '接着聊', icon: 'term', hint: idle && s.synced ? '和应用同步' : '', fn: () => resume(s, at) },
    idle && s.agent === 'claude' && !s.synced ? { text: '接着聊，并开 Remote Control', icon: 'link', hint: '手机上也能看', fn: () => resume(s, at, true) } : null,
    '-',
    { text: s.pinned ? '取消置顶' : '置顶', icon: 'pin', fn: async () => {
      try { await api('POST', '/api/history/pin', { agent: s.agent, id: s.id, pinned: !s.pinned }); load(true); } catch (e) { toast(e.message, 'err'); }
    } },
    { text: s.hidden ? '取消隐藏' : '隐藏', icon: s.hidden ? 'eye' : 'eyeoff', fn: async () => {
      try { await api('POST', '/api/history/hide', { agent: s.agent, id: s.id, hidden: !s.hidden }); load(true); } catch (e) { toast(e.message, 'err'); }
    } },
    { text: '复制对话 id', icon: 'copy', fn: () => navigator.clipboard?.writeText(s.id).then(() => toast('已复制')) },
  ], { align: 'right' });
}

async function resume(s, at, rc) {
  if (at.win) return select(at.win);
  let path = '/api/history/resume', fork = false;
  if (at.attach) path = '/api/history/attach';            // 它跑在这台机器的另一个 tmux 里：直接接上去，同一个进程，不会分叉
  else if (at.server) {
    if (!await confirmDialog('这个对话在 Claude 应用那边开着', `它是在 Claude 应用里新开的，由 Remote Control 的后台带着${s.working ? '，而且正在干活' : ''}。那种进程没有终端界面，这里接不进去。\n\n要接着聊：去 Claude 应用里。\n\n也可以在这里另开一份，但那是分叉：两边各聊各的，应用里看不到这边说的话，两边还往同一份存档里写。\n\n以后想两边都能用，就从这里用“Claude Code + Remote Control”开对话。`, '另开一份（不同步）', true)) return;
    fork = true;
  } else if (at.other) {
    if (!await confirmDialog('这个对话正在别的终端里跑着', '在这里再打开一份，两边会各聊各的、往同一份存档里写，容易乱。\n\n稳妥的做法：去它所在的那个终端里接着聊，或者先把那边退出。', '还是在这里打开', true)) return;
    fork = true;
  } else if (s.busy && !await confirmDialog('这个对话可能正开着', '它的存档两分钟内还有新内容，可能正在别处开着。两边同时接着聊，存档会写乱。还要在这里打开吗？', '还是打开')) return;
  try {
    const r = await api('POST', path, { agent: s.agent, id: s.id, rc: !!rc, fork });
    if (r.note) toast(r.note);
    await refreshTabs(true);
    select(r.name);
    load(true);
  } catch (e) { toast(e.message, 'err'); }
}
