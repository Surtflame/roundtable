// 「对话 | 终端」切换不重新加载。
//
// 圆桌和终端是两个站、各自是完整的页面。谁先被打开谁在最外层；第一次切到另一边时，把另一边放进一个铺满窗口的 iframe，
// 以后切换只是把这个 iframe 显示出来或藏起来，两边都一直开着（终端不断线，对话不用重新载入）。
//
// iframe 里的页面和外面的页面不同源，互相碰不到对方的脚本和内容。两边只用 postMessage 说三句话：
//   里面 → 外面：ready（我加载好了）、back（切回你那边）；外面 → 里面：shown（现在显示的是你）。
// 只认对方那一个来源发来的消息，消息里也没有任何能触发操作的内容。
const framed = window.parent !== window;
let frame = null, peerUrl = '', peerOrigin = '', readyT = null;
let hooks = { onShown() {}, beforeLeave() {}, firstUrl: null, title: '' };

export const isFramed = () => framed;

// opt.firstUrl：第一次把对方装进来、以及退回整页跳转时用的地址（异步函数）。从对话去终端要先换一张登录用的一次性票据，
// 票据就拼在这个地址里；不给就直接用 url。
export function initPeer(url, opt = {}) {
  peerUrl = url;
  peerOrigin = new URL(url).origin;
  hooks = { ...hooks, ...opt };
  window.addEventListener('message', e => {
    if (e.origin !== peerOrigin || !e.data || typeof e.data !== 'object') return;
    if (framed) {
      if (e.source === window.parent && e.data.rt === 'shown') hooks.onShown();
    } else if (frame && e.source === frame.contentWindow) {
      if (e.data.rt === 'ready') { clearTimeout(readyT); readyT = null; }
      if (e.data.rt === 'back') hidePeer();
    }
  });
  if (framed) {
    document.documentElement.classList.add('framed');
    window.parent.postMessage({ rt: 'ready' }, peerOrigin);
  }
}

async function entryUrl() {
  if (!hooks.firstUrl) return peerUrl;
  try { return (await hooks.firstUrl()) || peerUrl; } catch { return peerUrl; }
}

// 整页跳到对方那边（不用 iframe）。
export async function goToPeer() {
  hooks.beforeLeave();
  const u = await entryUrl();
  if (framed) window.open(u, '_top'); else location.href = u;
}

// 点「对话 / 终端」时调用。按住 Ctrl/⌘ 或中键点的话不拦，照常在新标签页打开。
export function switchToPeer(ev) {
  if (ev) {
    if (ev.metaKey || ev.ctrlKey || ev.shiftKey || ev.button) return;
    ev.preventDefault();
  }
  if (framed) return window.parent.postMessage({ rt: 'back' }, peerOrigin);
  if (!frame) {
    frame = document.createElement('iframe');
    frame.className = 'peer-frame';
    frame.title = hooks.title || '';
    frame.allow = 'clipboard-read; clipboard-write';
    document.body.append(frame);
    entryUrl().then(u => { frame.src = u; });
    // 八秒还没收到“加载好了”：多半是嵌不进来（浏览器不让，或者那边要重新登录）。退回老办法，直接跳过去
    readyT = setTimeout(goToPeer, 8000);
  }
  frame.hidden = false;
  document.documentElement.classList.add('peer-shown');
  if (hooks.title) { hooks.ownTitle = document.title; document.title = hooks.title; }
  frame.focus();
  try { frame.contentWindow.postMessage({ rt: 'shown' }, peerOrigin); } catch { /* 还没加载出来，它自己会抢焦点 */ }
}

function hidePeer() {
  if (!frame) return;
  frame.hidden = true;
  document.documentElement.classList.remove('peer-shown');
  if (hooks.ownTitle) document.title = hooks.ownTitle;
  window.focus();
  hooks.onShown();
}

// 被嵌在里面时要整页跳转（比如重新登录）：得让最外层的窗口去跳
export function topNavigate(url) {
  if (framed) window.open(url, '_top'); else location.href = url;
}
