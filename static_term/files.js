// 侧边栏的“文件”：看目录、上传下载、看和改文本文件、新建、改名、删除。
import { S, localGet, localSet } from './state.js';
import { h, icon, api, toast, menu, dialog, confirmDialog, promptDialog, fmtSize, fmtTime, shortPath, baseName } from './util.js';
import { newMenu } from './tabs.js';

let root, pathEl, listEl, upInput, cur = null, data = null, hidden = false, loaded = false;
const join = (dir, name) => (dir === '/' ? '/' + name : dir + '/' + name);

export function initFiles(el) {
  root = el;
  pathEl = h('input', { class: 'input side-q mono', type: 'text', spellcheck: 'false', 'aria-label': '路径' });
  pathEl.addEventListener('keydown', e => { if (e.key === 'Enter') go(pathEl.value.trim()); });
  listEl = h('div', { class: 'side-list' });
  upInput = h('input', { type: 'file', multiple: true, hidden: true });
  upInput.addEventListener('change', () => { upload([...upInput.files]); upInput.value = ''; });
  const more = h('button', { class: 'icon-btn', type: 'button', title: '更多', onclick: () => menu(more, [
    { text: '新建文件…', icon: 'file', fn: newFile },
    { text: '新建文件夹…', icon: 'folder', fn: newDir },
    { text: '在这个目录开终端…', icon: 'term', fn: () => newMenu(more, cur) },
    '-',
    { text: '显示隐藏文件', checked: hidden, fn: () => { hidden = !hidden; go(cur); } },
    { text: '刷新', icon: 'refresh', fn: () => go(cur) },
    { label: '常用位置' },
    ...S.me.roots.map(r => ({ text: r.name, hint: r.path, fn: () => go(r.path) })),
  ], { align: 'right' }) }, icon('more'));
  root.append(h('div', { class: 'side-tools' },
    h('button', { class: 'icon-btn', type: 'button', title: '上一级', onclick: () => data?.parent && go(data.parent) }, icon('up')),
    pathEl,
    h('button', { class: 'icon-btn', type: 'button', title: '上传文件到这个目录（也可以把文件拖进来）', onclick: () => upInput.click() }, icon('upload')),
    more), listEl, upInput);
  root.addEventListener('dragover', e => { if (e.dataTransfer?.types?.includes('Files')) { e.preventDefault(); root.classList.add('drop'); } });
  root.addEventListener('dragleave', e => { if (!root.contains(e.relatedTarget)) root.classList.remove('drop'); });
  root.addEventListener('drop', e => { root.classList.remove('drop'); if (e.dataTransfer?.files?.length) { e.preventDefault(); upload([...e.dataTransfer.files]); } });
}

export function showFiles(path) {
  if (path) return go(path);
  if (!loaded) go(localGet('fsdir') || S.me.default_cwd);
}

async function go(path) {
  loaded = true;
  let d;
  try { d = await api('GET', `/api/fs/list?path=${encodeURIComponent(path)}&hidden=${hidden ? 1 : 0}`); } catch (e) {
    toast(e.message, 'err');
    if (!data) go(S.me.home);
    else pathEl.value = cur;
    return;
  }
  data = d;
  cur = d.path;
  localSet('fsdir', cur);
  pathEl.value = cur;
  pathEl.scrollLeft = pathEl.scrollWidth;          // 路径长的时候看得到的是末尾那一段
  draw();
}

function draw() {
  const rows = data.items.map(it => {
    const isDir = it.type === 'dir' || it.to_dir;
    const full = join(cur, it.name);
    const row = h('div', { class: 'file' + (isDir ? ' dir' : ''), tabindex: '0', title: `${full}\n${it.mode || ''}${it.target ? ' → ' + it.target : ''}` },
      icon(isDir ? 'folder' : it.type === 'link' ? 'link' : 'file'),
      h('span', { class: 'file-name', text: it.name }),
      h('span', { class: 'file-meta', text: isDir ? '' : fmtSize(it.size) }),
      h('button', { class: 'file-more', type: 'button', title: '操作', 'aria-label': '操作', onclick: e => { e.stopPropagation(); itemMenu(e.currentTarget, it, full, isDir); } }, icon('more')));
    const open = () => (isDir ? go(full) : openFile(full));
    row.addEventListener('click', open);
    row.addEventListener('keydown', e => { if (e.key === 'Enter') open(); });
    row.addEventListener('contextmenu', e => { e.preventDefault(); itemMenu(row.querySelector('.file-more'), it, full, isDir); });
    return row;
  });
  const notes = [];
  if (data.protected) notes.push(h('div', { class: 'pick-empty', text: '这个目录受保护：只能看，不能在这里改、删、上传。' }));
  if (data.total > data.items.length) notes.push(h('div', { class: 'pick-empty', text: `这个目录有 ${data.total} 项，只列了前 ${data.items.length} 项。` }));
  if (!rows.length) notes.push(h('div', { class: 'pick-empty', text: hidden ? '空目录' : '空目录（或者只有隐藏文件）' }));
  listEl.replaceChildren(...notes, ...rows);
  listEl.scrollTop = 0;
}

function itemMenu(anchor, it, full, isDir) {
  menu(anchor, [
    isDir ? { text: '在这里开终端…', icon: 'term', fn: () => newMenu(anchor, full) } : { text: '下载', icon: 'download', fn: () => download(full) },
    { text: '复制路径', icon: 'copy', fn: () => navigator.clipboard?.writeText(full).then(() => toast('已复制')) },
    { text: '改名…', icon: 'edit', fn: async () => {
      const n = await promptDialog('新名字', it.name, '改名');
      if (!n || n === it.name) return;
      if (n.includes('/')) return toast('名字里不能有 /', 'err');
      try { await api('POST', '/api/fs/rename', { src: full, dst: join(cur, n) }); go(cur); } catch (e) { toast(e.message, 'err'); }
    } },
    '-',
    { text: '删除…', icon: 'trash', danger: true, fn: () => remove(it, full, isDir && it.type === 'dir') },
  ], { align: 'right' });
}

async function remove(it, full, isDir) {
  if (!await confirmDialog(isDir ? '删除这个文件夹？' : '删除这个文件？', `${full}\n\n${isDir ? '连同里面的所有东西一起删，' : ''}删了找不回来（没有回收站）。`, '删除', true)) return;
  try { await api('POST', '/api/fs/delete', { path: full, recursive: isDir }); toast('已删除'); go(cur); } catch (e) { toast(e.message, 'err'); }
}

function download(full) {
  const a = h('a', { href: `/api/fs/download?path=${encodeURIComponent(full)}`, download: baseName(full) });
  document.body.append(a);
  a.click();
  a.remove();
}

async function upload(files) {
  if (!cur) return;
  let ok = 0;
  for (const [i, f] of files.entries()) {
    toast(`上传中 ${i + 1}/${files.length}：${f.name}`);
    const post = async over => {
      const fd = new FormData();
      fd.append('file', f, f.name);
      return api('POST', `/api/fs/upload?dir=${encodeURIComponent(cur)}&overwrite=${over ? 1 : 0}`, fd);
    };
    try { await post(false); ok++; } catch (e) {
      if (/已经有一个/.test(e.message) && await confirmDialog('覆盖同名文件？', `${cur} 里已经有 ${f.name} 了，要用新上传的盖掉它吗？`, '覆盖', true)) {
        try { await post(true); ok++; } catch (e2) { toast(`${f.name}：${e2.message}`, 'err'); }
      } else if (!/已经有一个/.test(e.message)) toast(`${f.name}：${e.message}`, 'err');
    }
  }
  if (ok) toast(`传好了 ${ok} 个文件`);
  go(cur);
}

async function newDir() {
  const n = await promptDialog('新文件夹的名字', '', '新建');
  if (!n) return;
  try { await api('POST', '/api/fs/mkdir', { path: join(cur, n) }); go(cur); } catch (e) { toast(e.message, 'err'); }
}

async function newFile() {
  const n = await promptDialog('新文件的名字', '', '新建');
  if (!n) return;
  try { await api('PUT', '/api/fs/write', { path: join(cur, n), text: '', create: true }); await go(cur); openFile(join(cur, n)); } catch (e) { toast(e.message, 'err'); }
}

// 打开一个文件：文本可以改，图片直接看，别的只给下载
async function openFile(full) {
  let f;
  try { f = await api('GET', `/api/fs/read?path=${encodeURIComponent(full)}`); } catch (e) { return toast(e.message, 'err'); }
  const head = h('div', { class: 'fv-head mono', text: `${shortPath(full, S.me.home)}　${fmtSize(f.size)} · ${fmtTime(f.mtime)}` });
  if (f.kind === 'image') {
    return dialog(baseName(full), h('div', { class: 'fv' }, head, h('img', { class: 'fv-img', src: `/api/fs/download?path=${encodeURIComponent(full)}&inline=1`, alt: '' })),
      { cls: 'wide', buttons: [{ text: '下载', fn: () => { download(full); return false; } }, { text: '关闭' }] });
  }
  if (f.kind !== 'text') {
    return dialog(baseName(full), h('div', { class: 'fv' }, head, h('p', { class: 'dlg-msg', text: f.kind === 'big' ? '这个文件太大，不在网页里打开。可以下载，或者去终端里看。' : '这不是文本文件（或者不是 UTF-8 编码），不在网页里显示。' })),
      { buttons: [{ text: '关闭' }, { text: '下载', kind: 'primary', fn: () => download(full) }] });
  }
  const ta = h('textarea', { class: 'input fv-text mono', spellcheck: 'false', wrap: 'off', readonly: f.protected || null });
  ta.value = f.text;
  let mtime = f.mtime, dirty = false;
  const state = h('span', { class: 'fv-state' });
  ta.addEventListener('input', () => { dirty = true; state.textContent = '改过，还没保存'; });
  const save = async () => {
    try {
      const r = await api('PUT', '/api/fs/write', { path: full, text: ta.value, mtime });
      mtime = r.mtime; dirty = false; state.textContent = '已保存'; toast('已保存');
    } catch (e) { toast(e.message, 'err'); }
    return false;
  };
  ta.addEventListener('keydown', e => {
    if ((e.ctrlKey || e.metaKey) && e.key.toLowerCase() === 's') { e.preventDefault(); save(); }
    if (e.key === 'Tab' && !e.ctrlKey && !e.metaKey && !e.altKey) { e.preventDefault(); ta.setRangeText('\t', ta.selectionStart, ta.selectionEnd, 'end'); dirty = true; }
  });
  const d = dialog(baseName(full), h('div', { class: 'fv' }, h('div', { class: 'fv-bar' }, head, state), ta), { cls: 'wide tall', esc: false, buttons: [
    { text: '下载', fn: () => { download(full); return false; } },
    { text: '关闭', fn: async () => !dirty || await confirmDialog('不保存就关掉？', '改动还没保存。', '不保存', true) },
    f.protected ? null : { text: '保存', kind: 'primary', fn: save },
  ].filter(Boolean) });
  setTimeout(() => ta.focus(), 30);
  return d;
}
