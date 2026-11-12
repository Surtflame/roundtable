// 第二道门：动态验证码。没绑定过就先扫码绑定，绑定过就输 6 位码。
import { S, emit } from './state.js';
import { h, icon, api, toast } from './util.js';
import { switchToPeer } from './peer.js';

let shown = null;

export function lockScreenShown() { return !!shown; }

export async function showLock() {
  if (shown) return;
  shown = h('div', { class: 'lock' });
  document.body.append(shown);
  let stt;
  try { stt = await api('GET', '/api/auth/state'); } catch (e) { shown.replaceChildren(h('div', { class: 'lock-box' }, `载入失败：${e.message}`)); return; }
  if (stt.unlocked) return hideLock();
  if (!stt.enrolled) return enroll();
  codeForm('输入动态验证码', '打开手机上的验证器，输入“Roundtable 终端”那一行的 6 位数字。', '/api/auth/unlock', stt.locked_for);
}

function hideLock() {
  if (shown) shown.remove();
  shown = null;
  emit('unlocked');
}

function codeForm(title, note, url, lockedFor, extra) {
  const input = h('input', { class: 'input lock-code', type: 'text', inputmode: 'numeric', autocomplete: 'one-time-code', maxlength: '7', placeholder: '6 位数字', 'aria-label': '动态验证码' });
  const msg = h('div', { class: 'lock-msg', text: lockedFor ? `错太多次了，${Math.ceil(lockedFor / 60)} 分钟后再试` : '' });
  const btn = h('button', { class: 'btn primary', type: 'button', text: '确定' });
  let busy = false;
  const submit = async () => {
    const code = input.value.replace(/\D/g, '');
    if (busy || code.length !== 6) return;
    busy = true;
    btn.disabled = true;
    try {
      await api('POST', url, { code });
      hideLock();
    } catch (e) {
      msg.textContent = e.message;
      input.value = '';
      input.focus();
    } finally { busy = false; btn.disabled = false; }
  };
  input.addEventListener('input', () => { if (input.value.replace(/\D/g, '').length === 6) submit(); });
  input.addEventListener('keydown', e => { if (e.key === 'Enter') submit(); });
  btn.addEventListener('click', submit);
  shown.replaceChildren(h('div', { class: 'lock-box' },
    h('div', { class: 'lock-ic' }, icon('lock')), h('h1', { text: title }), h('p', { class: 'lock-note', text: note }),
    extra || null, h('div', { class: 'lock-row' }, input, btn), msg,
    h('p', { class: 'lock-foot', text: `输一次最多管 ${Math.round((S.me?.limits?.ttl || 43200) / 3600)} 小时，${Math.round((S.me?.limits?.idle || 7200) / 3600)} 小时没有操作会自动锁上。终端里的程序不受影响，照常在跑。` }),
    S.me?.chat_url ? h('a', { class: 'link-btn lock-back', href: S.me.chat_url, text: '← 回到对话', onclick: switchToPeer }) : null));
  setTimeout(() => input.focus(), 50);
}

async function enroll() {
  let d;
  try { d = await api('POST', '/api/auth/enroll/start', {}); } catch (e) {
    shown.replaceChildren(h('div', { class: 'lock-box' }, h('h1', { text: '绑定验证器' }), h('p', { class: 'lock-note', text: e.message })));
    return;
  }
  const qr = h('div', { class: 'lock-qr', html: d.qr });       // 二维码是服务器生成的 SVG
  codeForm('绑定验证器', '第一次用，先绑定。用谷歌验证器（或 1Password 之类）扫下面的二维码，然后输入它显示的 6 位数字。', '/api/auth/enroll/confirm', 0,
    h('div', { class: 'lock-enroll' }, qr, h('div', { class: 'lock-secret' }, h('span', { text: '扫不了就手工输入这串密钥：' }), h('code', { text: d.secret })),
      h('p', { class: 'lock-note warn', text: '这个二维码只出现这一次。绑定之后要换手机或重新绑定，只能在服务器上重置（python -m roundtable term totp-reset）。' })));
}

export async function lockNow() {
  try { await api('POST', '/api/auth/lock', {}); } catch (e) { toast(e.message, 'err'); }
  emit('locked');
  showLock();
}
