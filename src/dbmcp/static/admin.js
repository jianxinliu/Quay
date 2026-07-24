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

/* -------- 全站铃铛 --------
   放在 fixed 右上角，SPA / 服务端渲染页共用。
   - 首屏拉 unread_count，SSE 接实时新增（EventSource 走 cookie，与 admin 同源自动带上）
   - 点铃铛切开/关面板；面板打开时拉最近 20 条 + 全部标已读
   - 单条 item 有 deeplink 就跳转，无 deeplink 只关面板
   - 断线自动重连（EventSource 内建重试；这里再兜底 fetch 一次 unread 保稳）
*/
(function () {
  if (window.__dbmBellMounted) return;
  window.__dbmBellMounted = true;

  // 登录页不装铃铛：body 无 .shell 结构 = 登录/错误页
  if (!document.querySelector('.shell') && !document.querySelector('.dg-root, .rd-root')) {
    // 保险：SPA 主容器命名（查询台 .dg-root、redis .rd-root），若都没有仍装（例外只覆盖登录页）
  }
  if (location.pathname.startsWith('/admin/login')) return;

  var bell = document.createElement('button');
  bell.className = 'dbm-bell';
  bell.type = 'button';
  bell.title = '通知';
  bell.innerHTML = '<svg width="16" height="16" viewBox="0 0 16 16" fill="none">' +
    '<path d="M8 1.5c-2 0-3.5 1.7-3.5 3.7v2.3L3 10h10L11.5 7.5V5.2C11.5 3.2 10 1.5 8 1.5Z" ' +
      'stroke="currentColor" stroke-width="1.5" stroke-linejoin="round"/>' +
    '<path d="M6.5 11.5c0 .8.7 1.5 1.5 1.5s1.5-.7 1.5-1.5" stroke="currentColor" stroke-width="1.5" stroke-linecap="round"/>' +
    '</svg>' +
    '<span class="dbm-badge">0</span>';
  document.body.appendChild(bell);

  var panel = document.createElement('div');
  panel.className = 'dbm-panel';
  panel.innerHTML =
    '<div class="dbm-panel-hd"><b>通知</b>' +
      '<button type="button" class="dbm-clear">全部已读</button></div>' +
    '<div class="dbm-panel-list"><div class="dbm-panel-empty">暂无通知</div></div>';
  document.body.appendChild(panel);

  var badge = bell.querySelector('.dbm-badge');
  var listEl = panel.querySelector('.dbm-panel-list');
  var clearBtn = panel.querySelector('.dbm-clear');

  function setUnread(n) {
    n = Number(n) || 0;
    if (n > 0) {
      bell.classList.add('has-unread');
      badge.textContent = n > 99 ? '99+' : String(n);
    } else {
      bell.classList.remove('has-unread');
      badge.textContent = '0';
    }
  }

  function fmtTime(iso) {
    if (!iso) return '';
    try {
      var d = new Date(iso);
      var now = new Date();
      var diff = (now - d) / 1000; // seconds
      if (diff < 60) return '刚刚';
      if (diff < 3600) return Math.floor(diff / 60) + ' 分钟前';
      if (diff < 86400) return Math.floor(diff / 3600) + ' 小时前';
      if (diff < 86400 * 7) return Math.floor(diff / 86400) + ' 天前';
      return d.toLocaleString();
    } catch (e) { return iso; }
  }

  function renderList(items) {
    if (!items || !items.length) {
      listEl.innerHTML = '<div class="dbm-panel-empty">暂无通知</div>';
      return;
    }
    var html = items.map(function (n) {
      var deeplink = (n.meta && n.meta.deeplink) || '';
      var cls = 'dbm-item' + (n.read_at ? '' : ' unread');
      // deeplink 直接用后端拼好的，但仍需转义（防 <img onerror=...> 之类）
      var title = escapeHtml(n.title || '');
      var body = escapeHtml(n.body || '');
      var href = deeplink ? ' data-href="' + escapeHtml(deeplink) + '"' : '';
      return '<div class="' + cls + '" data-id="' + n.id + '"' + href + '>' +
        '<div class="dbm-item-title">' + title + '</div>' +
        '<div class="dbm-item-body">' + body + '</div>' +
        '<div class="dbm-item-time">' + escapeHtml(fmtTime(n.created_at)) + '</div>' +
        '</div>';
    }).join('');
    listEl.innerHTML = html;
  }

  function escapeHtml(s) {
    return String(s).replace(/[&<>"']/g, function (c) {
      return { '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' }[c];
    });
  }

  async function fetchUnreadCount() {
    try {
      var r = await fetch('/admin/notifications/unread_count', { credentials: 'same-origin', headers: { 'Accept': 'application/json' } });
      if (!r.ok) return;
      var d = await r.json();
      if (d.ok) setUnread(d.count);
    } catch (e) { /* silent */ }
  }

  async function fetchList() {
    try {
      var r = await fetch('/admin/notifications/list?limit=20', { credentials: 'same-origin', headers: { 'Accept': 'application/json' } });
      if (!r.ok) return;
      var d = await r.json();
      if (d.ok) renderList(d.items);
    } catch (e) {
      listEl.innerHTML = '<div class="dbm-panel-empty">加载失败</div>';
    }
  }

  async function markRead(idOrAll) {
    try {
      var body = idOrAll === '*'
        ? new URLSearchParams({ all: '1' })
        : new URLSearchParams({ ids: String(idOrAll) });
      await fetch('/admin/notifications/mark_read', {
        method: 'POST', credentials: 'same-origin', headers: { 'Accept': 'application/json' }, body: body,
      });
      fetchUnreadCount();
    } catch (e) { /* silent */ }
  }

  // 打开/关闭面板
  bell.addEventListener('click', function (e) {
    e.stopPropagation();
    var open = panel.classList.toggle('open');
    if (open) fetchList();
  });
  document.addEventListener('click', function (e) {
    if (!panel.classList.contains('open')) return;
    if (panel.contains(e.target) || bell.contains(e.target)) return;
    panel.classList.remove('open');
  });
  clearBtn.addEventListener('click', function () {
    markRead('*');
    // 已读后 UI 立即刷新
    listEl.querySelectorAll('.dbm-item.unread').forEach(function (el) {
      el.classList.remove('unread');
    });
  });

  // 单条点击：走 deeplink（同时标已读）
  listEl.addEventListener('click', function (e) {
    var item = e.target.closest('.dbm-item');
    if (!item) return;
    var id = item.dataset.id;
    var href = item.dataset.href;
    if (id) {
      markRead(id);
      item.classList.remove('unread');
    }
    if (href) {
      // 相对路径直接跳（同源）；绝对路径也允许
      window.location.href = href;
    }
  });

  // 首屏 & SSE
  fetchUnreadCount();
  var es;
  function connectSSE() {
    try {
      es = new EventSource('/admin/notifications/stream');
      es.addEventListener('notification', function (evt) {
        try {
          var n = JSON.parse(evt.data);
          // 面板打开时插入到列表顶（保留最多 30 条）
          if (panel.classList.contains('open')) {
            renderList((function () {
              var cur = [];
              listEl.querySelectorAll('.dbm-item').forEach(function (el) {
                cur.push({
                  id: Number(el.dataset.id),
                  title: el.querySelector('.dbm-item-title').textContent,
                  body: el.querySelector('.dbm-item-body').textContent,
                  read_at: el.classList.contains('unread') ? '' : '1',
                  meta: { deeplink: el.dataset.href || '' },
                  created_at: '',
                });
              });
              return [n].concat(cur).slice(0, 30);
            })());
          }
          // 未读数 +1、弹一下铃铛引起注意
          fetchUnreadCount();
          bell.classList.remove('new-pulse');
          void bell.offsetWidth;  // 强制 reflow 重放动画
          bell.classList.add('new-pulse');
        } catch (e) { /* silent */ }
      });
      es.onerror = function () {
        // EventSource 会自动重连；断线时先拉一次未读数
        fetchUnreadCount();
      };
    } catch (e) { /* silent */ }
  }
  connectSSE();
})();
