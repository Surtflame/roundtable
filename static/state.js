// 全局状态 + 模型目录的小工具。各模块之间用 on/emit 解耦，避免循环依赖。
import { api } from './util.js';

export const S = {
  me: null,
  settings: {},
  cat: { claude: [], codex: [], codebuddy: [], openrouter: [], openrouter_ready: true, backends: {} },
  map: new Map(),            // key → 模型信息
  convs: [],                 // 对话摘要
  curId: null,
  cur: null,                 // 当前对话全文
  loading: false,
  cfg: { models: [], mode: 'parallel', rounds: 1, synth: null, web: true, rules: [] },
  pending: [],               // 输入框里待发的附件
  editing: null,             // 正在编辑的 turn id
  unread: new Set(),
  live: new Set(),           // 本页面亲眼看着生成的 turn（阶段默认展开）
  stageOpen: new Map(),
  tabSel: new Map(),
  ui: new Map(),             // 回答卡片的折叠状态
  narrow: false,
  searchQ: '',
  hits: null,
  mcp: null,                 // 现在接着哪些 MCP（后端给的摘要）；没有就是 null
  rules: [],                 // 存好的规则模板 [{id, name, body}]；这次勾了哪几份记在 cfg.rules（id）
};

const subs = {};
export const on = (name, fn) => { (subs[name] ||= []).push(fn); };
export const emit = (name, ...args) => { (subs[name] || []).forEach(fn => fn(...args)); };

export const PROV = {
  claude: { name: 'Claude', sub: 'Claude Code', note: '经本机的 Claude Code，算在它登录的账号上' },
  codex: { name: 'ChatGPT', sub: 'Codex', note: '经本机的 Codex，算在它登录的账号上' },
  codebuddy: { name: 'CodeBuddy', sub: 'CodeBuddy', note: '经本机的 CodeBuddy，算在它登录的账号上' },
  or: { name: 'OpenRouter', sub: 'OpenRouter', note: '按量计费' },
};
export const EFFORT = { none: '不思考', minimal: '极低', low: '低', medium: '中', high: '高', xhigh: '很高', max: '最高' };

export function setCatalog(cat) {
  S.cat = { ...cat, codebuddy: cat.codebuddy || [] };
  S.map = new Map();
  for (const m of [...cat.claude, ...cat.codex, ...S.cat.codebuddy, ...cat.openrouter]) S.map.set(m.key, m);
}
export const prov = key => (key || '').split(':')[0];
export function info(key) {
  return S.map.get(key) || { key, provider: prov(key), id: key.slice(key.indexOf(':') + 1), name: key.slice(key.indexOf(':') + 1), efforts: [], missing: true };
}
// 去掉厂商前缀的短名字："Google: Gemini 4" → "Gemini 4"，"Claude Opus 5.5" → "Opus 5.5"
export function shortName(key, label) {
  let n = label || info(key).name || key;
  if (prov(key) === 'or') n = n.replace(/^[^:]{1,24}:\s+/, '');
  if (prov(key) === 'claude') n = n.replace(/^Claude\s+/, '');
  return n;
}

export const layoutTabs = () => S.narrow || S.settings.layout === 'tabs';

export function cfgFromServer(c) {
  if (!c || !Array.isArray(c.models)) return null;
  const models = c.models.filter(m => m && S.map.has(m.key)).map(m => ({ key: m.key, effort: m.effort || '' }));
  if (!models.length) return null;
  let synth = c.synth ? (c.synth.key || c.synth) : null;
  if (synth && !models.some(m => m.key === synth)) synth = models[0].key;
  return {
    models, mode: c.mode === 'review' ? 'review' : c.mode === 'parallel' ? 'parallel' : (localGet('multiMode') || 'parallel'),
    rounds: Math.max(1, Math.min(c.rounds || 1, 3)), synth, web: c.web !== false,
    rules: c.rules === undefined ? S.cfg.rules || [] : ruleIds(c.rules),   // 有这个功能之前的对话没有这一项：保持现在勾的不动
  };
}
// 配置里的规则模板可能是 id，也可能是 {id, name}（一轮记下的快照）；只留现在还在的
export function ruleIds(list) {
  const ids = (Array.isArray(list) ? list : []).map(r => (r && typeof r === 'object' ? r.id : r));
  return [...new Set(ids)].filter(id => S.rules.some(r => r.id === id));
}
export function defaultCfg() {
  const c = cfgFromServer(S.settings.last_config);
  if (c) return c;
  const first = S.cat.claude.find(m => m.id === 'opus') || S.cat.claude[0] || S.cat.codex[0] || S.cat.codebuddy[0]
    || (S.settings.or_favorites || []).map(k => S.map.get(k)).find(m => m && !m.image_out);
  return { models: first ? [{ key: first.key, effort: '' }] : [], mode: 'parallel', rounds: 1, synth: null, web: true, rules: [] };
}

export async function saveSettings(patch) {
  Object.assign(S.settings, patch);
  try {
    const last = S.settings.last_config;
    S.settings = await api('PUT', '/api/settings', patch);
    if (!S.settings.last_config) S.settings.last_config = last;
  } catch (e) { emit('toast', e.message, 'err'); }
  emit('settings');
}

// MCP 摘要变了（设置里改过，或者重新读了一次）
export function setMcp(sum) {
  S.mcp = sum || null;
  emit('mcp');
}
export async function refreshMcp() {
  try { setMcp((await api('GET', '/api/me')).mcp); } catch { /* 下次再说 */ }
}

// 规则模板的清单变了（自己改的，或者别的页面改的）
export function setRules(list) {
  S.rules = list || [];
  S.cfg.rules = ruleIds(S.cfg.rules);
  emit('rules');
}

export function localGet(k) { try { return localStorage.getItem(k); } catch { return null; } }
export function localSet(k, v) { try { v == null ? localStorage.removeItem(k) : localStorage.setItem(k, v); } catch { /* 隐私模式 */ } }

export function applyTheme() {
  const t = localGet('theme') || 'system';
  const dark = t === 'dark' || (t === 'system' && window.matchMedia('(prefers-color-scheme: dark)').matches);
  document.documentElement.dataset.theme = dark ? 'dark' : 'light';
  const l = document.getElementById('hljs-light'), d = document.getElementById('hljs-dark');
  if (l) l.disabled = dark;
  if (d) d.disabled = !dark;
  const meta = document.querySelector('meta[name="theme-color"]');
  if (meta) meta.content = dark ? '#1c1c1a' : '#f6f5f1';
}

export const convBusy = () => !!(S.cur && S.cur.turns.some(t => t.busy));
export const findTurn = id => S.cur && S.cur.turns.find(t => t.id === id);
