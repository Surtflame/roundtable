// 左侧栏：新对话、搜索、对话列表。
import { S, on } from './state.js';
import { h, icon, api, toast, menu, confirmDialog, promptDialog, debounce } from './util.js';
import { switchToPeer } from './peer.js';

let listEl, searchEl, hooks = {};
const whoami = () => ['google', 'external'].includes(S.me.auth_mode) ? S.me.user : S.me.auth_mode === 'password' ? '已登录' : '本机模式';

export function initSide(el, opt) {
  hooks = opt;
  searchEl = h('input', { class: 'input side-q', type: 'search', placeholder: '搜索对话', 'aria-label': '搜索对话', autocomplete: 'off' });
  listEl = h('nav', { class: 'conv-list', 'aria-label': '对话列表' });
  el.append(
    h('div', { class: 'side-top' },
      h('a', { class: 'brand', href: '#/', title: '圆桌' }, h('img', { src: '/static/icon.svg', alt: '', width: '22', height: '22' }), h('span', { text: '圆桌' })),
      h('button', { class: 'icon-btn', type: 'button', title: '收起侧栏', 'aria-label': '收起侧栏', onclick: () => hooks.toggleSide() }, icon('sidebar'))),
    // “对话 | 终端”：终端是另一个进程、另一个来源（见 docs/terminal.md），左边栏做成一样的，点过去像是同一个东西的两页；
    // 切换不重新加载（peer.js）。没配终端就没有这一排。
    S.me.term_url ? h('nav', { class: 'app-switch', 'aria-label': '切换' }, h('span', { class: 'on', text: '对话' }),
      h('a', { href: S.me.term_url, text: '终端', onclick: switchToPeer })) : null,
    h('a', { class: 'new-chat', href: '#/', onclick: () => hooks.closeDrawer() }, icon('plus'), h('span', { class: 'grow', text: '新对话' }), h('kbd', { text: 'Ctrl ⇧ O' })),
    h('div', { class: 'side-search' }, icon('search'), searchEl),
    listEl,
    h('div', { class: 'side-foot' },
      h('span', { class: 'me', text: whoami(), title: whoami() }),
      h('button', { class: 'icon-btn', type: 'button', title: '设置与用量', 'aria-label': '设置', onclick: () => hooks.openSettings() }, icon('gear'))));

  const remote = debounce(async () => {
    const q = S.searchQ;
    if (q.length < 2) { S.hits = null; return renderSide(); }
    try {
      const r = await api('GET', '/api/conversations?q=' + encodeURIComponent(q));
      if (q === S.searchQ) { S.hits = r.conversations; renderSide(); }
    } catch { /* 搜索失败就只按标题筛 */ }
  }, 300);
  searchEl.addEventListener('input', () => { S.searchQ = searchEl.value.trim(); S.hits = null; renderSide(); remote(); });
  on('settings', renderSide);
}

function groupOf(c, now) {
  if (c.pinned) return '置顶';
  const d0 = new Date(now); d0.setHours(0, 0, 0, 0);
  const days = Math.floor((d0.getTime() / 1000 - c.updated_at) / 86400) + 1;
  return days <= 0 ? '今天' : days === 1 ? '昨天' : days <= 7 ? '7 天内' : days <= 30 ? '30 天内' : '更早';
}

export function renderSide() {
  if (!listEl) return;
  const q = S.searchQ.toLowerCase();
  let convs = S.convs;
  if (q) {
    const byTitle = S.convs.filter(c => c.title.toLowerCase().includes(q));
    const seen = new Set(byTitle.map(c => c.id));
    convs = [...byTitle, ...(S.hits || []).filter(c => !seen.has(c.id))];
  }
  const kids = [];
  if (!convs.length) kids.push(h('div', { class: 'conv-empty', text: q ? '没有找到' : '还没有对话' }));
  const now = Date.now();
  const sorted = q ? convs : [...convs].sort((a, b) => (b.pinned - a.pinned) || (b.updated_at - a.updated_at));
  let last = null;
  for (const c of sorted) {
    const g = q ? '搜索结果' : groupOf(c, now);
    if (g !== last) { kids.push(h('div', { class: 'conv-group', text: g })); last = g; }
    const more = h('button', { class: 'conv-more', type: 'button', title: '更多', 'aria-label': '更多操作' }, icon('more'));
    more.addEventListener('click', e => { e.preventDefault(); e.stopPropagation(); convMenu(more, c); });
    kids.push(h('a', { class: 'conv' + (c.id === S.curId ? ' cur' : ''), href: `#/c/${c.id}`, title: c.title, onclick: () => hooks.closeDrawer() },
      c.busy ? h('span', { class: 'spin' }) : S.unread.has(c.id) ? h('span', { class: 'unread', title: '有新回答' }) : null,
      h('span', { class: 'conv-title', text: c.title }), more));
  }
  listEl.replaceChildren(...kids);
}

export function convMenu(anchor, c, opt = { align: 'left' }) {
  menu(anchor, [
    { text: '重命名', icon: 'edit', fn: () => renameConv(c) },
    { text: c.pinned ? '取消置顶' : '置顶', icon: 'pin', fn: () => api('PATCH', `/api/conversations/${c.id}`, { pinned: !c.pinned }).catch(e => toast(e.message, 'err')) },
    { text: '导出 Markdown', icon: 'download', fn: () => {
      const a = h('a', { href: `/api/conversations/${c.id}/export`, download: '' });
      document.body.append(a); a.click(); a.remove();
    } },
    '-',
    { text: '删除', icon: 'trash', danger: true, fn: async () => {
      if (!await confirmDialog('删除对话', `「${c.title}」连同里面的附件和生成的图片都会删掉，无法恢复。`, '删除', true)) return;
      try { await api('DELETE', `/api/conversations/${c.id}`); } catch (e) { toast(e.message, 'err'); }
    } },
  ], opt);
}

export async function renameConv(c) {
  const t = await promptDialog('重命名对话', c.title);
  if (!t || t === c.title) return;
  api('PATCH', `/api/conversations/${c.id}`, { title: t }).catch(e => toast(e.message, 'err'));
}
