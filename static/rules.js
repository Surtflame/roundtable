// 规则模板：存好的几份提示词。输入区的「规则模板」按钮点开这里的菜单，勾上哪份，提问时就带上哪份。
import { S, emit, setRules } from './state.js';
import { h, icon, api, toast, popover, closePopover, dialog, confirmDialog } from './util.js';

const MAX_NAME = 60, MAX_BODY = 10000, MAX_PICK = 10;

// 模板正文的第一行有字的内容，去掉 Markdown 的标题、列表记号，当成列表里的一句简介
function preview(body) {
  const line = (body || '').split('\n').map(s => s.replace(/^[#>\-*\s]+/, '').trim()).find(Boolean) || '';
  return line.length > 80 ? line.slice(0, 80) + '…' : line;
}

export function openRulesMenu(anchor) {
  const list = h('div', { class: 'rules-list' });
  const pop = popover(anchor, h('div', { class: 'rules-menu' },
    h('div', { class: 'rules-head' }, h('b', { text: '规则模板' }), h('span', { text: '勾上的会跟着提问一起发给每个模型' })),
    list,
    h('button', { class: 'menu-item rules-new', type: 'button', onclick: () => editRule(null) }, icon('plus'), h('span', { class: 'grow', text: '新建规则模板' }))),
  { cls: 'rules-pop sheet', place: 'up' });
  if (!pop) return;

  const toggle = r => {
    const cur = S.cfg.rules || [];
    if (cur.includes(r.id)) S.cfg.rules = cur.filter(id => id !== r.id);
    else {
      if (cur.length >= MAX_PICK) return toast(`一次最多带 ${MAX_PICK} 份`);
      S.cfg.rules = [...cur, r.id];
    }
    draw();
    emit('rules');
  };

  function draw() {
    if (!S.rules.length) {
      list.replaceChildren(h('div', { class: 'pick-empty', text: '还没有模板。把你希望模型每次都遵守的要求写成一份（比如回答某类问题时的口径、格式、不要做什么），提问时勾上就行。' }));
      return;
    }
    list.replaceChildren(...S.rules.map(r => {
      const sel = (S.cfg.rules || []).includes(r.id);
      const row = h('div', { class: 'pick-row rules-row' + (sel ? ' sel' : ''), role: 'menuitemcheckbox', 'aria-checked': String(sel), tabindex: '0' },
        h('span', { class: 'rules-box' + (sel ? ' on' : '') }, sel ? icon('check') : null),
        h('span', { class: 'pick-t' }, h('span', { class: 'pick-name', text: r.name }), h('span', { class: 'pick-sub', text: preview(r.body) })),
        h('button', { class: 'icon-btn rules-edit', type: 'button', title: '查看、修改或删除', 'aria-label': `修改「${r.name}」`,
          onclick: e => { e.stopPropagation(); editRule(r); } }, icon('edit')));
      row.addEventListener('click', () => toggle(r));
      row.addEventListener('keydown', e => { if ((e.key === 'Enter' || e.key === ' ') && e.target === row) { e.preventDefault(); toggle(r); } });
      return row;
    }));
  }
  draw();
}

// 新建（r 为空）或修改一份模板
export function editRule(r) {
  closePopover();
  const name = h('input', { class: 'input', type: 'text', maxlength: MAX_NAME, placeholder: '名字，例如：技术问题的回答规则', 'aria-label': '模板名字', value: r ? r.name : null });
  const body = h('textarea', { class: 'input rule-body', rows: '14', 'aria-label': '模板内容', spellcheck: 'false',
    placeholder: '写给模型看的要求，想到什么写什么，可以用 Markdown。\n\n例如：\n- 具体数字要标出处，找不到原始出处就直说\n- 不解释基础概念，直接给事实\n- 我说错了直接指出' });
  body.value = r ? r.body : '';
  const count = h('span', { class: 'rule-count' });
  const recount = () => {
    const n = body.value.trim().length;
    count.textContent = `${n} / ${MAX_BODY} 字`;
    count.classList.toggle('over', n > MAX_BODY);
  };
  body.addEventListener('input', recount);
  recount();

  const save = async () => {
    const d = { name: name.value.trim(), body: body.value.trim() };
    if (!d.name) { toast('给模板起个名字', 'err'); name.focus(); return false; }
    if (!d.body) { toast('模板内容是空的', 'err'); body.focus(); return false; }
    if (d.body.length > MAX_BODY) { toast(`内容最多 ${MAX_BODY} 个字`, 'err'); return false; }
    try {
      const res = await api(r ? 'PATCH' : 'POST', r ? `/api/rules/${r.id}` : '/api/rules', d);
      if (!r && res.id && (S.cfg.rules || []).length < MAX_PICK) S.cfg.rules = [...(S.cfg.rules || []), res.id];
      setRules(res.rules);
      toast(r ? '已保存' : '已保存，这次提问会带上它');
    } catch (e) { toast(e.message, 'err'); return false; }
  };
  const remove = async () => {
    if (!await confirmDialog('删除这份模板？', `「${r.name}」删掉后找不回来。已经问过的那些轮次不受影响。`, '删除', true)) return false;
    try { setRules((await api('DELETE', `/api/rules/${r.id}`)).rules); toast('已删除'); } catch (e) { toast(e.message, 'err'); return false; }
  };

  dialog(r ? '修改规则模板' : '新建规则模板', h('div', { class: 'rule-form' },
    name, body,
    h('div', { class: 'rule-foot' },
      h('span', { class: 'set-note', text: r ? '改动只影响以后的提问；已经问过的轮次用的还是当时的内容。' : '保存后在「规则模板」里勾上，才会跟着提问发给模型。' }),
      count)),
  { cls: 'wide', esc: false, buttons: [
    r ? { text: '删除', kind: 'danger-ghost left', fn: remove } : null,
    { text: '取消' },
    { text: '保存', kind: 'primary', fn: save },
  ].filter(Boolean) });
  setTimeout(() => (r ? body : name).focus(), 30);
}

// 某一轮发出去时带了哪些模板、当时的原文
export async function showTurnRules(t) {
  let rs;
  try { rs = (await api('GET', `/api/turns/${t.id}/rules`)).rules; } catch (e) { return toast(e.message, 'err'); }
  dialog('这一轮带的规则模板', h('div', { class: 'rule-view' },
    h('p', { class: 'set-note', text: '下面是发出这条消息时的内容。模板后来改过或删掉，都不影响这一轮；重新生成、追加互评用的也是这一份。' }),
    rs.length ? rs.map(r => h('section', null, h('h3', { text: r.name }), h('pre', { text: r.body }))) : h('p', { class: 'dlg-msg', text: '这一轮没有带规则模板。' })),
  { cls: 'wide', buttons: [{ text: '关闭' }] });
}
