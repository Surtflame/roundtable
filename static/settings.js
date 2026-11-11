// 设置与用量。
import { S, saveSettings, setMcp, localGet, localSet, applyTheme, shortName, prov, PROV } from './state.js';
import { h, icon, api, dialog, confirmDialog, toast, fmtTok, fmtCost, fmtTime } from './util.js';

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
  if (bk.codebuddy && bk.codebuddy.found) {
    st('CodeBuddy', bk.codebuddy, [`能选 ${bk.codebuddy.models} 个模型`,
      bk.codebuddy.logged_in === false ? '看起来还没登录（在服务器上运行 codebuddy，按提示登录）' : ''].filter(Boolean).join('；'));
  }

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

// ---------- 综合要求 ----------

const SYNTH_SAMPLES = [
  ['只看分歧', '不用写共识。只找出各模型观点不同、互相对立的地方，逐条写清楚各方分别怎么说、理由是什么，最后说你更支持谁。'],
  ['合成一份统一答案', '不要分“共识 / 分歧”小节。把各方内容合成一份独立成文、直接回答问题的答案；有分歧的地方由你判断谁对并按对的写，只在最后简单列出你拿不准的分歧。'],
];

const clip = (s, n) => { s = (s || '').replace(/\s+/g, ' ').trim(); return s.length > n ? s.slice(0, n) + '…' : s; };

// 放进「由谁来综合」那两个菜单里的一项
export const synthInstrItem = () => ({ text: '综合要求…', hint: clip(S.settings.synth_instructions, 14) || '默认写法', fn: editSynthInstr });

export function editSynthInstr() {
  const ta = h('textarea', { class: 'input set-ci', rows: '5', maxlength: '4000', placeholder: '例如：不要罗列共识，只找出大家观点不同、互相对立的地方。' });
  ta.value = S.settings.synth_instructions || '';
  const fill = (text, v) => h('button', { class: 'link-btn', type: 'button', text, onclick: () => { ta.value = v; ta.focus(); } });
  const body = h('div', { class: 'synth-edit' },
    h('div', { class: 'set-note', text: '综合稿默认按「共识 / 分歧 / 综合者自己的判断」来写，不会把分歧抹平。这里可以再加你自己的要求，和默认写法冲突时以你的为准；留空就只用默认写法。保存后对之后的每一次综合都生效，直到你改掉。' }),
    ta,
    h('div', { class: 'synth-samples' }, h('span', { class: 'set-note', text: '填一个现成的：' }),
      SYNTH_SAMPLES.map(([name, v]) => fill(name, v)), fill('清空', '')),
    h('details', { class: 'synth-default' }, h('summary', { text: '看默认写法的提示词' }), h('pre', { text: S.settings.synth_default || '' })));
  dialog('综合要求', body, { buttons: [{ text: '取消' }, { text: '保存', kind: 'primary', fn: async () => {
    await saveSettings({ synth_instructions: ta.value.trim() });
    toast(ta.value.trim() ? '已保存，下一次综合起生效' : '已清空，之后按默认写法综合');
  } }] });
  setTimeout(() => ta.focus(), 30);
}

// ---------- MCP ----------

const MCP_STATUS = { ok: ['已连接', 'ok'], needs_auth: ['需要授权', 'warn'], error: ['出错', 'err'] };

function mcpSection() {
  const msgs = h('div', { class: 'mcp-msgs' });
  const list = h('div', { class: 'mcp-list' }, h('div', { class: 'set-note' }, h('span', { class: 'spin' }), ' 读取中…'));
  const addBox = h('div', { class: 'mcp-add' });
  const sec = h('section', { class: 'mcp-sec' }, h('h3', { text: 'MCP' }),
    h('div', { class: 'set-note', text: '把远程的 MCP 服务器接进来，接上之后对所有模型（Claude、ChatGPT、CodeBuddy、OpenRouter）都生效。默认一个都不接；接上以后，每个工具还要你勾了，模型才看得到。' }),
    msgs, list, addBox);
  let servers = [], ready = false, adding = false;
  const open = new Set(), busy = new Set();

  const apply = d => { servers = d.servers || []; setMcp(d.summary); draw(); };
  const say = (text, ok) => msgs.append(h('div', { class: 'mcp-msg' + (ok ? ' ok' : ''), text }));
  const run = async (sid, fn) => {
    if (busy.has(sid)) return;
    busy.add(sid);
    draw();
    try { await fn(); } catch (e) { toast(e.message, 'err'); }
    busy.delete(sid);
    draw();
  };
  const link = (text, fn) => h('button', { class: 'link-btn', type: 'button', text, onclick: fn });

  function card(s) {
    const [stText, stCls] = MCP_STATUS[s.status] || [s.status, 'err'];
    const nOn = s.tools.filter(t => t.enabled).length, nChanged = s.tools.filter(t => t.changed).length;
    const isOpen = open.has(s.id), isBusy = busy.has(s.id);
    const url = `/api/mcp/servers/${s.id}`;
    const patch = body => run(s.id, async () => apply(await api('PATCH', url, body)));
    const setTools = pick => patch({ tools: Object.fromEntries(s.tools.map(t => [t.name, !!pick(t)])) });
    const note = s.status !== 'ok' ? (s.error || '') : !s.enabled ? '已停用，模型看不到它。'
      : !s.tools.length ? '这个服务器没有提供工具。'
      : nOn ? `模型能用其中 ${nOn} 个工具（共 ${s.tools.length} 个）。` : `共 ${s.tools.length} 个工具，一个都没勾，模型现在看不到它。`;

    const authorize = async () => {
      if (!await confirmDialog('去授权', `接下来会跳到「${s.name}」的授权页（由它的服务器指定），登录并同意之后会自动回到这里。`, '继续')) return;
      run(s.id, async () => {
        const r = await api('POST', url + '/oauth/start');
        try { sessionStorage.setItem('mcpBack', location.hash); } catch { /* ignore */ }
        location.href = r.url;
      });
    };
    const refresh = () => run(s.id, async () => {
      const d = await api('POST', url + '/refresh');
      apply(d);
      const now = (d.servers || []).find(x => x.id === s.id);
      const ch = now ? now.tools.filter(t => t.changed).length : 0;
      if (d.error) toast(d.error, 'err');
      else if (!now) toast('这个服务器已被移除', 'err');
      else toast(ch ? `工具表已更新。有 ${ch} 个工具的说明或参数变了，已取消勾选，看过再勾。` : `工具表已更新，共 ${now.tools.length} 个工具`);
      if (ch) open.add(s.id);
    });
    const remove = async () => {
      if (!await confirmDialog('删除 MCP 服务器', `删除「${s.name}」？保存在这里的令牌会一起删掉，以后要用得重新添加。`, '删除', true)) return;
      run(s.id, async () => { apply(await api('DELETE', url)); toast('已删除'); });
    };

    const tools = !isOpen || !s.tools.length ? null : h('div', { class: 'mcp-tools' },
      h('div', { class: 'mcp-bulk' }, h('span', { class: 'set-note', text: '给模型用哪些：' }), link('全选', () => setTools(() => true)),
        s.tools.some(t => t.ro) ? link('只选只读的', () => setTools(t => t.ro)) : null, link('全不选', () => setTools(() => false))),
      s.tools.map(t => {
        const box = h('input', { type: 'checkbox', checked: t.enabled, disabled: isBusy,
          onchange: () => patch({ tools: { [t.name]: box.checked } }) });
        const desc = h('span', { class: 'set-note mcp-tool-d', text: t.description || '（没有说明）' });
        desc.addEventListener('click', e => { e.preventDefault(); desc.classList.toggle('full'); });
        return h('label', { class: 'mcp-tool' + (t.changed ? ' changed' : '') }, box,
          h('span', { class: 'mcp-tool-t' },
            h('span', { class: 'mcp-tool-n' }, h('code', { text: t.name }), t.ro ? h('span', { class: 'badge dim', text: '只读' }) : null,
              t.changed ? h('span', { class: 'badge warn', text: '说明变过' }) : null),
            desc, t.params.length ? h('span', { class: 'mcp-tool-p', text: '参数：' + t.params.join('、') }) : null));
      }),
      h('div', { class: 'set-note', text: '「只读」是服务器自己标的，仅供参考。能写东西的工具（发消息、改数据），模型可能在你没明说的时候就去调，勾之前想一下。' }));

    return h('div', { class: 'mcp-card' + (s.enabled ? '' : ' off'), 'data-sid': s.id },
      h('div', { class: 'mcp-head' },
        h('div', { class: 'set-t' },
          h('div', { class: 'set-title' }, s.name, ' ', h('span', { class: 'badge ' + stCls, text: stText }),
            nChanged ? [' ', h('span', { class: 'badge warn', text: `${nChanged} 个工具有变动` })] : null),
          h('div', { class: 'set-note mcp-url', text: s.url }),
          note ? h('div', { class: 'set-note' + (s.status !== 'ok' ? ' mcp-err' : ''), text: note }) : null),
        toggle(s.enabled, v => patch({ enabled: v }))),
      h('div', { class: 'mcp-ops' },
        s.status === 'needs_auth' ? h('button', { class: 'btn sm primary', type: 'button', onclick: authorize }, '去授权') : null,
        s.tools.length ? h('button', { class: 'btn sm', type: 'button', 'aria-expanded': String(isOpen),
          onclick: () => { if (isOpen) open.delete(s.id); else open.add(s.id); draw(); } }, isOpen ? '收起工具' : '选择工具') : null,
        h('button', { class: 'btn sm', type: 'button', disabled: isBusy, onclick: refresh }, isBusy ? h('span', { class: 'spin' }) : null, s.status === 'error' ? '重试' : '刷新工具'),
        s.auth === 'oauth' && s.status !== 'needs_auth' ? link('重新授权', authorize) : null,
        h('span', { class: 'grow' }),
        h('button', { class: 'icon-btn', type: 'button', title: '删除这个服务器', 'aria-label': '删除', onclick: remove }, icon('trash'))),
      tools);
  }

  function addForm() {
    const inp = (ph, extra) => h('input', { class: 'input', type: 'text', placeholder: ph, autocomplete: 'off', spellcheck: 'false', ...extra });
    const name = inp('名字（可不填）', { maxlength: '40' });
    const url = inp('地址，例如 https://example.com/mcp', { inputmode: 'url', maxlength: '2000' });
    const token = inp('令牌（API key / Bearer token）', { type: 'password', autocomplete: 'new-password' });
    const header = inp('请求头名字，默认 Authorization');
    const cid = inp('OAuth client id（服务器不支持自动注册时才要）');
    const csec = inp('OAuth client secret（没有就空着）', { type: 'password', autocomplete: 'new-password' });
    const err = h('div', { class: 'set-note mcp-err', hidden: true });
    const ok = h('button', { class: 'btn primary', type: 'button', text: '添加' });
    const submit = async () => {
      if (ok.disabled) return;
      if (!url.value.trim()) { url.focus(); return; }
      ok.disabled = true;
      ok.textContent = '正在连接…';
      err.hidden = true;
      try {
        const d = await api('POST', '/api/mcp/servers', { name: name.value, url: url.value, token: token.value, header: header.value,
          client_id: cid.value, client_secret: csec.value });
        adding = false;
        open.add(d.id);
        apply(d);
        const s = servers.find(x => x.id === d.id);
        toast(!s ? '已添加' : s.status === 'needs_auth' ? '已添加，还要点「去授权」' : `已连接，共 ${s.tools.length} 个工具。勾上要给模型用的。`);
        list.querySelector(`[data-sid="${d.id}"]`)?.scrollIntoView({ block: 'start' });
      } catch (e) {
        err.textContent = e.message;
        err.hidden = false;
        ok.disabled = false;
        ok.textContent = '添加';
      }
    };
    ok.addEventListener('click', submit);
    for (const el of [name, url]) el.addEventListener('keydown', e => { if (e.key === 'Enter' && !e.isComposing) submit(); });
    setTimeout(() => url.focus(), 30);
    return h('div', { class: 'mcp-form' },
      h('div', { class: 'set-sub', text: '添加 MCP 服务器' }), url, name,
      h('details', { class: 'mcp-adv' }, h('summary', { text: '高级：固定令牌、OAuth 客户端' }),
        h('div', { class: 'set-note', text: '用固定令牌的服务器，把令牌填在这里。走 OAuth 的不用填，加完点「去授权」就行。' }), token, header, cid, csec),
      h('div', { class: 'set-note', text: '只支持远程服务器（Streamable HTTP）。令牌保存在服务器上，不会交给模型。' }),
      err,
      h('div', { class: 'mcp-form-f' }, h('button', { class: 'btn', type: 'button', text: '取消', onclick: () => { adding = false; draw(); } }), ok));
  }

  function draw() {
    if (!ready) return;
    list.replaceChildren(...(servers.length ? servers.map(card) : [h('div', { class: 'set-note mcp-empty', text: '现在一个 MCP 都没接，模型用不到任何外部工具。' })]));
    if (adding) { if (!addBox.firstChild || !addBox.firstChild.classList.contains('mcp-form')) addBox.replaceChildren(addForm()); }
    else addBox.replaceChildren(h('button', { class: 'btn', type: 'button', onclick: () => { adding = true; draw(); } }, icon('plus'), '添加 MCP 服务器'));
  }

  api('GET', '/api/mcp/servers').then(d => {
    ready = true;
    for (const f of d.flash || []) { say(f.text, f.ok); if (f.sid) open.add(f.sid); }
    for (const s of d.servers || []) if (s.tools.some(t => t.changed)) open.add(s.id);
    apply(d);
    const sid = (d.flash || []).map(f => f.sid).filter(Boolean).pop();
    if (sid) setTimeout(() => list.querySelector(`[data-sid="${sid}"]`)?.scrollIntoView({ block: 'start' }), 80);
  }).catch(e => list.replaceChildren(h('div', { class: 'set-note', text: /404/.test(e.message) ? '这个功能要等服务重启之后才能用。' : '读取失败：' + e.message })));
  return sec;
}

export function openSettings(focus) {
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

  const mcp = mcpSection();
  const body = h('div', { class: 'settings' },
    h('section', null, h('h3', { text: '自定义指示' }),
      h('div', { class: 'set-note', text: '每个模型每次回答前都会看到这段话。' }), ci, ciState),
    h('section', null, h('h3', { text: '综合' }),
      row('综合要求', '「综合成一份」默认按“共识 / 分歧 / 综合者自己的判断”来写。可以另加自己的要求，比如只看分歧。',
        h('button', { class: 'btn', type: 'button', onclick: editSynthInstr }, '编辑…'))),
    h('section', null, h('h3', { text: '界面' }),
      row('主题', null, seg([['system', '跟随系统'], ['light', '浅色'], ['dark', '深色']], localGet('theme') || 'system', v => { localSet('theme', v === 'system' ? null : v); applyTheme(); })),
      row('多个模型的排版', '手机上总是用标签页。', seg([['columns', '并排'], ['tabs', '标签页']], s.layout === 'tabs' ? 'tabs' : 'columns', v => saveSettings({ layout: v }))),
      row('发送键', '手机上回车总是换行。', seg([['enter', 'Enter 发送'], ['ctrl', 'Ctrl+Enter 发送']], s.send_key === 'ctrl' ? 'ctrl' : 'enter', v => saveSettings({ send_key: v })))),
    h('section', null, h('h3', { text: '联网' }),
      row('OpenRouter 模型也联网搜索', `OpenRouter 的搜索按次另收费（大约每次几美分），默认关闭。${PROV.claude.name} 和 ${PROV.codex.name} 的联网搜索用的是它们自带的搜索，由输入框旁的「联网」开关控制。`,
        toggle(!!s.or_web, v => saveSettings({ or_web: v })))),
    mcp,
    h('section', null, h('h3', { text: '预设' }), presets),
    h('section', null, h('h3', { text: '用量' }), usage),
    h('section', null, h('h3', { text: '登录' }), accountRow()));
  dialog('设置', body, { cls: 'wide' });
  if (focus === 'mcp') requestAnimationFrame(() => mcp.scrollIntoView({ block: 'start' }));
}
