// 顶上那一排窗口标签：切换、新开、改名、关闭。每个标签背后是 tmux 里的一个会话。
import { S, on, emit, localGet, localSet, winTitle, AGENT } from './state.js';
import { h, icon, api, toast, menu, dialog, promptDialog, confirmDialog, shortPath } from './util.js';
import { connect, detach, currentWindow, focusTerm } from './termview.js';

let strip, addBtn, emptyEl, pollT = null;

export function initTabs(stripEl, empty) {
  strip = stripEl;
  emptyEl = empty;
  addBtn = h('button', { class: 'tab-add', type: 'button', title: '开一个新窗口', 'aria-label': '新窗口', onclick: () => newMenu(addBtn) }, icon('plus'));
  on('gone', () => refresh());
  on('unlocked', () => { refresh().then(() => { if (S.cur) connect(S.cur); }); });
  on('locked', () => detach());
  document.addEventListener('visibilitychange', () => { if (!document.hidden) refresh(); });
  pollT = setInterval(() => { if (!document.hidden) refresh(true); }, 3000);
}

export function cwdDefault() { return localGet('cwd') || S.me.default_cwd; }

export function newMenu(anchor, cwd) {
  const dir = cwd || cwdDefault();
  const items = [
    { label: `这台机器 · ${shortPath(dir, S.me.home)}` },
    { text: 'Shell', icon: 'term', fn: () => openWindow({ cwd: dir }) },
    { text: 'Claude Code', icon: 'star', fn: () => openWindow({ cwd: dir, agent: 'claude' }) },
    { text: 'Claude Code + Remote Control', icon: 'link', hint: '手机上也能看', fn: () => openWindow({ cwd: dir, agent: 'claude', rc: true }) },
    { text: 'Codex', icon: 'star', fn: () => openWindow({ cwd: dir, agent: 'codex' }) },
    { text: '换个目录…', icon: 'folder', fn: async () => {
      const p = await promptDialog('新窗口默认开在哪个目录', dir, '确定');
      if (!p) return;
      localSet('cwd', p);
      newMenu(anchor, p);
    } },
  ];
  // 别的机器上的环境（和 Claude 应用里选环境是同一批机器）：窗口开在那台机器上
  for (const e of S.me.envs || []) {
    items.push('-', { label: `${e.label} · ${e.cwd}` },
      { text: 'Shell', icon: 'term', fn: () => openWindow({ env: e.key }) },
      { text: 'Claude Code', icon: 'star', fn: () => openWindow({ env: e.key, agent: 'claude' }) },
      { text: 'Claude Code + Remote Control', icon: 'link', hint: '手机上也能看', fn: () => openWindow({ env: e.key, agent: 'claude', rc: true }) });
  }
  items.push('-', { text: '添加远程环境…', icon: 'plus', hint: 'AI 带着配', fn: addEnv });
  if ((S.me.envs || []).length) items.push({ text: '移除远程环境…', icon: 'x', fn: removeEnvDialog });
  menu(anchor, items, { place: 'auto' });
}

// 添加远程环境：开一个 Claude Code 对话，它问地址和登录方式，然后自己把钥匙、登记都配好
async function addEnv() {
  if (!await confirmDialog('添加远程环境', '会在这台机器上开一个 Claude Code 对话，它来问你那台机器的地址、用户名、怎么登录，然后替你配好。配完之后，「新窗口」菜单里会多出那台机器。\n\n需要这台机器上的 Claude Code 已经登录。你告诉它的密码会留在那次对话的记录里（存在这台机器上）；介意的话，可以让它生成公钥、你自己装到那台机器上。', '开始')) return;
  try {
    const r = await api('POST', '/api/envs/assistant', {});
    S.windows = r.windows;
    select(r.name);
  } catch (e) { toast(e.message, 'err'); }
}

function removeEnvDialog() {
  const list = h('div', { class: 'env-list' });
  const draw = () => list.replaceChildren(...((S.me.envs || []).length ? S.me.envs.map(e => h('div', { class: 'env-row' },
    h('span', { class: 'env-name' }, h('b', { text: e.label }), h('span', { text: e.cwd })),
    h('button', { class: 'btn sm danger', type: 'button', text: '移除', onclick: async () => {
      try { S.me.envs = (await api('DELETE', `/api/envs/${e.key}`)).envs; draw(); toast('已移除'); } catch (err) { toast(err.message, 'err'); }
    } }))) : [h('p', { class: 'set-note', text: '没有远程环境了。' })]));
  draw();
  dialog('移除远程环境', h('div', null, list, h('p', { class: 'set-note', text: '移除只是这里不再列出那台机器。登录用的钥匙还留在这台机器上，那台机器上装的公钥也还在——要彻底断开，去那台机器的 ~/.ssh/authorized_keys 里删掉备注以 roundtable-> 开头的那一行。' })), { buttons: [{ text: '关闭' }] });
}

export async function openWindow(opt) {
  if (opt.env) toast('正在连过去开窗口…');
  try {
    const r = await api('POST', '/api/windows', opt);
    S.windows = r.windows;
    select(r.name);
  } catch (e) { toast(e.message, 'err'); }
}

export function select(name) {
  S.cur = name;
  localSet('cur', name);
  const w = S.windows.find(x => x.name === name);
  if (w) S.seen[name] = w.activity;
  render();
  if (currentWindow() !== name) connect(name);
  else focusTerm();
  emit('window');
}

export async function refresh(quiet) {
  let r;
  try { r = await api('GET', '/api/windows'); } catch (e) { if (!quiet) toast(e.message, 'err'); return; }
  S.windows = r.windows;
  if (r.envs) S.me.envs = r.envs;                                             // 远程环境可能刚被加上或拿掉
  if (S.cur && !S.windows.some(w => w.name === S.cur)) S.cur = null;          // 现在看的窗口没了（里面的程序退出，或者被关掉）
  if (!S.cur && S.windows.length) {
    const last = localGet('cur');
    S.cur = (S.windows.find(w => w.name === last) || S.windows[S.windows.length - 1]).name;
  }
  render();
  if (S.cur !== currentWindow()) { if (S.cur) connect(S.cur); else detach(); }
}

function render() {
  const now = S.cur;
  strip.replaceChildren(...S.windows.map(w => {
    if (w.name === now) S.seen[w.name] = w.activity;
    const fresh = w.name !== now && S.seen[w.name] !== undefined && w.activity > S.seen[w.name];
    if (S.seen[w.name] === undefined) S.seen[w.name] = w.activity;
    const env = w.env ? (S.me.envs || []).find(e => e.key === w.env) : null;
    const tip = w.ext ? `${winTitle(w)}\n接在这台机器的 tmux 会话 ${w.ext} 上：和 Claude 应用里是同一个进程，两边同步\n从 SSH 接进来：${sshCmd(w)}`
      : env ? `${winTitle(w)}\n开在 ${env.label} 那台机器上（${env.cwd}）${w.rc ? '\n连着 Claude 应用（Remote Control）' : ''}\n在那台机器上接进来：${sshCmd(w)}`
      : `${winTitle(w)}\n${w.cwd}${w.dup ? '\n这是另开的一份副本：这个对话的正主在别处跑着，这里说的话那边看不到' : w.rc ? '\n连着 Claude 应用（Remote Control）' : ''}\n从 SSH 接进来：${sshCmd(w)}`;
    const tab = h('div', { class: 'tab' + (w.name === now ? ' on' : '') + (fresh ? ' fresh' : ''), role: 'tab', tabindex: '0', title: tip,
      data: { kind: w.agent || 'shell' } },
      h('span', { class: 'tab-dot' }), h('span', { class: 'tab-name', text: winTitle(w) }),
      env ? h('span', { class: 'badge tab-tag', text: env.short }) : null, w.dup ? h('span', { class: 'badge warn tab-tag', text: '副本' }) : null,
      h('button', { class: 'tab-x', type: 'button', title: w.ext ? '不在这里看了（那边照常跑）' : '关闭这个窗口（里面的程序会被结束）', 'aria-label': '关闭', onclick: e => { e.stopPropagation(); closeWindow(w); } }, icon('x')));
    tab.addEventListener('click', () => select(w.name));
    tab.addEventListener('dblclick', () => renameWindow(w));
    tab.addEventListener('contextmenu', e => { e.preventDefault(); tabMenu(tab, w); });
    tab.addEventListener('keydown', e => { if (e.key === 'Enter') select(w.name); });
    return tab;
  }), addBtn);
  emptyEl.hidden = S.windows.length > 0;
  document.body.classList.toggle('no-windows', !S.windows.length);
  document.title = now ? `${winTitle(S.windows.find(w => w.name === now) || {})} · 终端` : '终端';
  strip.querySelector('.tab.on')?.scrollIntoView({ block: 'nearest', inline: 'nearest' });
}

const sshCmd = w => (w.ext ? `tmux attach -t ${w.ext}` : `tmux -L ${S.me.tmux_socket} attach -t ${w.name}`);     // 别的机器上的窗口：同一条命令，在那台机器上跑

function tabMenu(anchor, w) {
  menu(anchor, [
    { text: '改名…', icon: 'edit', fn: () => renameWindow(w) },
    { text: '复制 SSH 里接进来的命令', icon: 'copy', fn: () => navigator.clipboard?.writeText(sshCmd(w)).then(() => toast('已复制')) },
    '-',
    w.ext ? { text: '不在这里看了', icon: 'x', hint: '那边照常跑', fn: () => closeWindow(w) } : { text: '关闭窗口', icon: 'x', danger: true, fn: () => closeWindow(w) },
  ]);
}

function renameWindow(w) {
  const input = h('input', { class: 'input', type: 'text', maxlength: '60', value: w.title || '', placeholder: winTitle({ ...w, title: '' }) });
  const save = async title => {
    try { S.windows = (await api('PATCH', `/api/windows/${w.name}`, { title })).windows; render(); } catch (e) { toast(e.message, 'err'); }
  };
  const d = dialog('窗口名字', h('div', null, input, h('p', { class: 'set-note', text: '不起名字的话，标签上显示的是“正在跑的程序 · 所在目录”，会自己跟着变。' })), { buttons: [
    { text: '取消' }, { text: '用自动的', fn: () => save('') }, { text: '确定', kind: 'primary', fn: () => save(input.value.trim()) }] });
  input.addEventListener('keydown', e => { if (e.key === 'Enter' && !e.isComposing) { save(input.value.trim()); d.close(); } });
  setTimeout(() => { input.focus(); input.select(); }, 30);
}

async function closeWindow(w) {
  // 借来的窗口关掉只是不看了，不用问；别的窗口里有程序在跑才问
  // 别的机器上的普通窗口这边不知道里面在跑什么，一律问一句
  const agent = w.agent && w.kind !== 'ext';
  const idle = w.ext || (!agent && !w.env && ['bash', 'zsh', 'sh', 'fish'].includes(w.command));
  if (!idle && !await confirmDialog('关闭这个窗口？', `「${winTitle(w)}」${agent ? `里正在跑 ${AGENT[w.agent] || w.agent}` : w.env ? '开在别的机器上，里面可能有程序在跑' : `里正在跑 ${w.command}`}，关掉窗口它就被结束了。${agent ? '对话本身有存档，以后可以从“历史对话”里接着聊。' : ''}${w.rc && !w.dup ? '\n\n它连着 Claude 应用：关掉之后在应用里会显示离线，要接着用得回这里重新打开。' : ''}`, '关闭', true)) return;
  try {
    S.windows = (await api('DELETE', `/api/windows/${w.name}`)).windows;
    if (S.cur === w.name) S.cur = null;
    await refresh(true);
    emit('winclosed');
  } catch (e) { toast(e.message, 'err'); refresh(true); }
}
