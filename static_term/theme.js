// 在样式表之前执行，避免深色模式下先闪一下白底。
(function () {
  var t = 'system';
  try { t = localStorage.getItem('theme') || 'system'; } catch (e) {}
  var dark = t === 'dark' || (t === 'system' && window.matchMedia('(prefers-color-scheme: dark)').matches);
  document.documentElement.dataset.theme = dark ? 'dark' : 'light';
})();
