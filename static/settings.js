// 设置与用量。
import { S, saveSettings, localGet, localSet, applyTheme, shortName, prov, PROV } from './state.js';
import { h, icon, api, dialog, toast, fmtTok, fmtCost, fmtTime } from './util.js';

function seg(options, cur, onPick) {
  const el = h('div', { class: 'seg', role: 'group' });
  const draw = v => el.replaceChildren(...options.map(([val, text]) => h('button', {
    class: 'seg-b' + (val === v ? ' on' : ''), type: 'button', text, onclick: () => { draw(val); onPick(val); },
  })));
  draw(cur);
  return el;
}

function toggle(on, onChange) {
  const b = h('button', { class: 'switch' + (on ? ' on' : ''), type: 'button', role: 'switch', 'aria-checked': String(on) }, h('i'));
  b.addEventListener('click', () => {
    on = !on;
    b.classList.toggle('on', on);
    b.setAttribute('aria-checked', String(on));
    onChange(on);
  });
  return b;
}

const row = (title, note, ctl) => h('div', { class: 'set-row' },
  h('div', { class: 'set-t' }, h('div', { class: 'set-title', text: title }), note ? h('div', { class: 'set-note', text: note }) : null), ctl);

const WINDOW = { five_hour: '5 小时窗口', seven_day: '7 天窗口', seven_day_opus: '7 天窗口（Opus）', seven_day_sonnet: '7 天窗口（Sonnet）', overage: '超额用量' };
const RL_STATUS = { allowed: '正常', allowed_warning: '接近上限', rejected: '已到上限' };

function usageBox(u) {
  const kids = [];
  const line = (name, val, note) => h('div', { class: 'use-line' }, h('span', { class: 'use-name', text: name }), h('span', { class: 'use-val', text: val }),
    note ? h('span', { class: 'use-note', text: note }) : null);
  const or = u.openrouter, bk = u.backends || {};
  if (or) {
    const pct = or.limit ? Math.min(100, or.usage / or.limit * 100) : 0;
    kids.push(line('OpenRouter', or.limit ? `已用 $${or.usage.toFixed(2)} / $${or.limit}` : `已用 $${(or.usage || 0).toFixed(2)}`,
      or.limit ? `还剩 $${(or.remaining ?? or.limit - or.usage).toFixed(2)}` : ''));
    if (or.limit) kids.push(h('div', { class: 'meter' }, h('i', { style: `width:${pct.toFixed(1)}%` })));
  } else kids.push(line('OpenRouter', bk.openrouter && bk.openrouter.ok ? '查不到' : '未启用', bk.openrouter && bk.openrouter.ok ? '接口暂时不通' : '没有配置 OPENROUTER_API_KEY'));
  const rl = u.claude_rate_limit;
  if (rl) {
    const wins = Object.entries(rl.unifiedWindows || {});
    const txt = wins.length ? wins.map(([k, w]) => `${WINDOW[k] || k} 已用 ${Math.round((w.utilization || 0) * 100)}%`).join('，')
      : `${WINDOW[rl.rateLimitType] || rl.rateLimitType || ''} ${RL_STATUS[rl.status] || rl.status || ''}`;
    kids.push(line('Claude 额度', RL_STATUS[rl.status] || rl.status || '—',
      `${txt}${rl.resetsAt ? `，${fmtTime(rl.resetsAt)} 重置` : ''}（${fmtTime(rl.seen_at)} 的读数）`));
  } else if (bk.claude && bk.claude.ok) kids.push(line('Claude 额度', '—', '用订阅登录的 Claude 发过一条之后这里才有读数'));
  const st = (name, b, extra) => kids.push(line(name, !b ? '—' : b.ok ? `可用${b.version ? ' · ' + b.version : ''}` : '未启用', !b ? '' : b.ok ? extra : b.why));
  st('Claude Code', bk.claude, '');
  st('Codex', bk.codex, bk.codex && bk.codex.ok ? [bk.codex.logged_in ? '' : '看起来还没登录（在服务器上运行 codex login）',
    bk.codex.sandbox === 'bwrap' ? '在 bubblewrap 沙箱里运行' : '没有沙箱', bk.codex.note].filter(Boolean).join('；') : '');

  if (u.by_model.length) {
    kids.push(h('div', { class: 'set-sub', text: '近 30 天各模型' }),
      h('div', { class: 'table-wrap' }, h('table', { class: 'use-table' },
        h('thead', null, h('tr', null, ['模型', '次数', '输入', '输出', '花费'].map(t => h('th', { text: t })))),
        h('tbody', null, u.by_model.map(m => h('tr', null,
          h('td', null, h('span', { class: 'dot', data: { prov: prov(m.model) } }), ' ', shortName(m.model, m.label)),
          h('td', { text: String(m.n) }), h('td', { text: fmtTok(m.in) }), h('td', { text: fmtTok(m.out) }),
          h('td', { text: m.cost ? `$${fmtCost(m.cost)}` : prov(m.model) === 'or' ? '$0' : '—',
            title: prov(m.model) === 'or' ? null : '算在命令行工具登录的账号上，这里查不到实际花费' + (m.cost_est ? `；按 API 价折算约 $${fmtCost(m.cost_est)}` : '') })))))));
  }
  return kids;
}

function accountRow() {
  const keep = '对话保存在服务器上，不会自动删除。';
  const out = h('button', { class: 'btn', type: 'button', onclick: async () => {
    try { const r = await api('POST', '/auth/logout'); location.href = r.next || '/login'; } catch (e) { toast(e.message, 'err'); }
  } }, '退出登录');
  if (S.me.auth_mode === 'google') return row(S.me.user, `用 Google 账号登录；谁能进来由服务器上的 ROUNDTABLE_ALLOWED_EMAILS 决定。登录 ${S.me.session_days} 天后要重新登一次。${keep}`, out);
  if (S.me.auth_mode === 'password') return row('密码登录', `改密码：在服务器上运行 python -m roundtable set-password（会让所有浏览器下线）。登录 ${S.me.session_days} 天后要重新登一次。${keep}`, out);
  return row('本机模式', `没有开登录，只有这台机器自己（或 SSH 隧道）能访问。${keep}`, null);
}

export function openSettings() {
  const s = S.settings;
  const ci = h('textarea', { class: 'input set-ci', rows: '4', maxlength: '8000', placeholder: '例如：用中文回答；先给结论再给理由；涉及数字要给出处。' });
  ci.value = s.custom_instructions || '';
  const ciState = h('span', { class: 'set-note' });
  ci.addEventListener('change', async () => {
    await saveSettings({ custom_instructions: ci.value });
    ciState.textContent = '已保存';
    setTimeout(() => { ciState.textContent = ''; }, 1500);
  });

  const presets = h('div', { class: 'set-presets' });
  const drawPresets = () => {
    const ps = S.settings.presets || [];
    presets.replaceChildren(...(ps.length ? ps.map(p => h('div', { class: 'set-preset' },
      h('span', { class: 'preset-dots' }, p.models.map(m => h('span', { class: 'dot', data: { prov: prov(m.key) } }))),
      h('span', { class: 'grow' }, h('span', { class: 'set-title', text: p.name }),
        h('span', { class: 'set-note', text: p.models.map(m => shortName(m.key)).join(' + ') + (p.models.length > 1 ? (p.mode === 'review' ? ' · 互评' : ' · 各答各的') : '') })),
      h('button', { class: 'icon-btn', type: 'button', title: '删除这个预设', 'aria-label': '删除预设',
        onclick: async () => { await saveSettings({ presets: (S.settings.presets || []).filter(x => x !== p) }); drawPresets(); } }, icon('trash'))))
      : [h('div', { class: 'set-note', text: '还没有预设。在输入框上方选好模型后，点「预设 → 把当前组合存为预设」。' })]));
  };
  drawPresets();

  const usage = h('div', { class: 'usage' }, h('div', { class: 'set-note' }, h('span', { class: 'spin' }), ' 读取中…'));
  api('GET', '/api/usage').then(u => usage.replaceChildren(...usageBox(u)))
    .catch(e => usage.replaceChildren(h('div', { class: 'set-note', text: '读取失败：' + e.message })));

  const body = h('div', { class: 'settings' },
    h('section', null, h('h3', { text: '自定义指示' }),
      h('div', { class: 'set-note', text: '每个模型每次回答前都会看到这段话。' }), ci, ciState),
    h('section', null, h('h3', { text: '界面' }),
      row('主题', null, seg([['system', '跟随系统'], ['light', '浅色'], ['dark', '深色']], localGet('theme') || 'system', v => { localSet('theme', v === 'system' ? null : v); applyTheme(); })),
      row('多个模型的排版', '手机上总是用标签页。', seg([['columns', '并排'], ['tabs', '标签页']], s.layout === 'tabs' ? 'tabs' : 'columns', v => saveSettings({ layout: v }))),
      row('发送键', '手机上回车总是换行。', seg([['enter', 'Enter 发送'], ['ctrl', 'Ctrl+Enter 发送']], s.send_key === 'ctrl' ? 'ctrl' : 'enter', v => saveSettings({ send_key: v })))),
    h('section', null, h('h3', { text: '联网' }),
      row('OpenRouter 模型也联网搜索', `OpenRouter 的搜索按次另收费（大约每次几美分），默认关闭。${PROV.claude.name} 和 ${PROV.codex.name} 的联网搜索用的是它们自带的搜索，由输入框旁的「联网」开关控制。`,
        toggle(!!s.or_web, v => saveSettings({ or_web: v })))),
    h('section', null, h('h3', { text: '预设' }), presets),
    h('section', null, h('h3', { text: '用量' }), usage),
    h('section', null, h('h3', { text: '登录' }), accountRow()));
  dialog('设置', body, { cls: 'wide' });
}
