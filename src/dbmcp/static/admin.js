// 后台外壳脚本（全站加载）。原先内联在 admin.py 的 <script>。
(function () {
  // 侧栏高亮当前页
  var p = location.pathname;
  document.querySelectorAll('.side nav a').forEach(function (a) {
    if (p.indexOf(a.getAttribute('href')) === 0) a.classList.add('active');
  });
})();

// 危险操作二次确认（自绘，替代原生 confirm）：首次点击按钮变「确认删除？」，3 秒内再点才提交
function dbmConfirm(form) {
  var btn = form.querySelector('button');
  if (form.dataset.armed === '1') return true;
  form.dataset.armed = '1';
  btn.dataset.old = btn.textContent;
  btn.textContent = '确认删除？';
  btn.style.filter = 'brightness(1.25)';
  setTimeout(function () {
    form.dataset.armed = '';
    btn.textContent = btn.dataset.old;
    btn.style.filter = '';
  }, 3000);
  return false;
}
