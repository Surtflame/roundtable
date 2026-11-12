// 全局状态。各模块之间用 on/emit 解耦。
export const S = {
  me: null,
  windows: [],              // tmux 里的窗口（服务器给的）
  cur: null,                // 现在看的是哪个窗口（名字）
  seen: {},                 // 窗口名 → 上次看它时的 activity，用来点亮“有新输出”
  panel: 'history',         // 侧边栏现在是哪一页：history / files
  narrow: false,
};
const subs = {};
export const on = (name, fn) => { (subs[name] ||= []).push(fn); };
export const emit = (name, ...args) => { (subs[name] || []).forEach(fn => fn(...args)); };

export function localGet(k) { try { return localStorage.getItem(k); } catch { return null; } }
export function localSet(k, v) { try { v == null ? localStorage.removeItem(k) : localStorage.setItem(k, v); } catch { /* 隐私模式 */ } }

export const AGENT = { claude: 'Claude Code', codex: 'Codex' };
// 窗口显示的名字：自己起过名就用它；没起过的，对话窗口用工具名，普通窗口用“正在跑的程序 + 目录”
export function winTitle(w) {
  if (w.title) return w.title;
  if (w.session_title) return w.session_title;          // 窗口里那个对话自己的标题（有的话）
  if (w.ext) return `Claude Code · ${w.ext}`;           // 借来的窗口：接在这台机器别的 tmux 会话上
  const env = w.env ? (S.me?.envs || []).find(e => e.key === w.env) : null;
  const dir = env ? (w.cwd || '').split('/').pop() : w.cwd === S.me?.home ? '~' : (w.cwd || '').replace(/\/+$/, '').split('/').pop() || '/';
  if (w.agent) return `${AGENT[w.agent] || w.agent} · ${dir}`;
  return `${env ? 'shell' : w.command || 'shell'} · ${dir}`;
}
