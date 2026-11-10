// Markdown → 安全的 HTML。原始 HTML 一律不解析；外链图片不加载（改成链接）。
const esc = s => s.replace(/[&<>"']/g, c => ({ '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' }[c]));

const md = window.markdownit({ html: false, linkify: true, breaks: false });
md.linkify.set({ fuzzyLink: false, fuzzyEmail: false });
md.use(window.texmath, {
  engine: window.katex, delimiters: ['dollars', 'brackets'],
  katexOptions: { throwOnError: false, strict: false, trust: false, maxExpand: 2000 },
});

// CommonMark 的强调规则不认中文：「**标题（说明）**后面」这种写法里，右边的 ** 夹在全角标点和汉字之间，
// 按原规则不算收尾，结果星号原样露出来。这里把紧挨着的中日韩字符也算作可以贴边。
const CJK = /[⺀-鿿가-힯豈-﫿︰-﹏＀-￯]/;
{
  const { isWhiteSpace, isPunctChar, isMdAsciiPunct } = md.utils;
  const State = md.inline.State;
  State.prototype.scanDelims = function (start, canSplitWord) {
    const max = this.posMax, marker = this.src.charCodeAt(start);
    const lastChar = start > 0 ? this.src.charCodeAt(start - 1) : 0x20;
    let pos = start;
    while (pos < max && this.src.charCodeAt(pos) === marker) pos++;
    const count = pos - start;
    const nextChar = pos < max ? this.src.charCodeAt(pos) : 0x20;
    const lastS = String.fromCharCode(lastChar), nextS = String.fromCharCode(nextChar);
    const lastPunct = isMdAsciiPunct(lastChar) || isPunctChar(lastS), nextPunct = isMdAsciiPunct(nextChar) || isPunctChar(nextS);
    const lastWs = isWhiteSpace(lastChar), nextWs = isWhiteSpace(nextChar);
    const lastCjk = CJK.test(lastS), nextCjk = CJK.test(nextS);
    const left = !nextWs && (!nextPunct || lastWs || lastPunct || lastCjk);
    const right = !lastWs && (!lastPunct || nextWs || nextPunct || nextCjk);
    const can_open = left && (canSplitWord || !right || lastPunct || lastCjk || nextCjk);
    const can_close = right && (canSplitWord || !left || nextPunct || lastCjk || nextCjk);
    return { can_open, can_close, length: count };
  };
}

md.renderer.rules.fence = (tokens, idx) => {
  const t = tokens[idx];
  const lang = (t.info || '').trim().split(/\s+/)[0] || '';
  let body;
  try {
    body = lang && window.hljs.getLanguage(lang)
      ? window.hljs.highlight(t.content, { language: lang, ignoreIllegals: true }).value
      : esc(t.content);
  } catch { body = esc(t.content); }
  return `<div class="code"><div class="code-h"><span>${esc(lang || 'text')}</span>` +
    `<button type="button" class="copy-code">复制</button></div><pre><code class="hljs">${body}</code></pre></div>`;
};
md.renderer.rules.code_block = (tokens, idx) =>
  `<div class="code"><pre><code class="hljs">${esc(tokens[idx].content)}</code></pre></div>`;

const linkOpen = md.renderer.rules.link_open || ((t, i, o, e, self) => self.renderToken(t, i, o));
md.renderer.rules.link_open = (tokens, idx, opts, env, self) => {
  tokens[idx].attrSet('target', '_blank');
  tokens[idx].attrSet('rel', 'noopener noreferrer nofollow');
  return linkOpen(tokens, idx, opts, env, self);
};
md.renderer.rules.image = (tokens, idx) => {
  const src = tokens[idx].attrGet('src') || '';
  const alt = tokens[idx].content || '图片';
  if (!/^https?:\/\//i.test(src)) return `<span class="img-stub">[${esc(alt)}]</span>`;
  return `<a class="img-link" href="${esc(src)}" target="_blank" rel="noopener noreferrer nofollow">🖼 ${esc(alt)}</a>`;
};
md.renderer.rules.table_open = () => '<div class="table-wrap"><table>';
md.renderer.rules.table_close = () => '</table></div>';

const PURIFY = {
  ADD_TAGS: ['eq', 'eqn'], ADD_ATTR: ['target'],
  FORBID_TAGS: ['style', 'form', 'input', 'textarea', 'select', 'iframe', 'object', 'embed', 'img', 'video', 'audio'],
  FORBID_ATTR: ['id', 'name'],
};

export function renderMarkdown(src) {
  let html;
  try { html = md.render(src || ''); } catch (e) { html = `<pre>${esc(src || '')}</pre>`; }
  return window.DOMPurify.sanitize(html, PURIFY);
}

export { esc };
