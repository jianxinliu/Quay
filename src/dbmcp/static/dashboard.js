/* 看板前端（/admin/dashboard）。原生 JS，无框架——这页只是「拉一份 JSON 再画出来」，
   没有本地编辑状态，用不上 Vue（查询台/Redis 才需要）。

   两个刷新节奏：
   - 每 5s 重新拉一次 /admin/dashboard/data（可关）；
   - 每 1s 只更新在途操作的「已执行 Ns」——用客户端秒表锚定服务端 elapsed_ms
     （anchor = now - elapsed_ms），与查询台同一套做法：纯靠轮询值会一跳一跳的。 */
(function () {
  const root = document.getElementById("dash");
  if (!root) return;

  const REFRESH_MS = 5000;
  const WINDOWS = [["1h", "1 小时"], ["6h", "6 小时"], ["24h", "24 小时"],
                   ["7d", "7 天"], ["30d", "30 天"]];

  let win = localStorage.getItem("dbm.dash.window") || "24h";
  if (!WINDOWS.some((w) => w[0] === win)) win = "24h";
  let auto = localStorage.getItem("dbm.dash.auto") !== "0";
  let data = null;
  let loading = false;
  let pollTimer = null;
  // op_id -> 客户端秒表起点；ops 每 5s 换一份对象，锚点要按 op_id 记住才不会被重置
  const anchors = new Map();

  const esc = (s) => String(s == null ? "" : s).replace(/[&<>"']/g,
    (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));
  const num = (n) => Number(n || 0).toLocaleString("zh-CN");

  function bytes(n) {
    n = Number(n || 0);
    if (!n) return "0 B";
    const units = ["B", "KB", "MB", "GB", "TB"];
    let i = 0;
    while (n >= 1024 && i < units.length - 1) { n /= 1024; i++; }
    return (i === 0 ? n.toFixed(0) : n.toFixed(1)) + " " + units[i];
  }

  function dur(ms) {
    ms = Math.max(Number(ms || 0), 0);
    if (ms < 1000) return ms + " ms";
    const s = ms / 1000;
    if (s < 60) return s.toFixed(1) + " s";
    const m = Math.floor(s / 60);
    if (m < 60) return m + " 分 " + Math.floor(s % 60) + " 秒";
    const h = Math.floor(m / 60);
    if (h < 24) return h + " 小时 " + (m % 60) + " 分";
    return Math.floor(h / 24) + " 天 " + (h % 24) + " 小时";
  }

  const localTime = (iso) => {
    if (!iso) return "—";
    const d = new Date(iso);
    return isNaN(d) ? String(iso) : d.toLocaleString("zh-CN", { hour12: false });
  };

  /* 桶标签是 UTC ISO 前缀：天桶 'YYYY-MM-DD'、小时桶 '…THH'、分钟桶 '…THH:MM'。
     宽度决定粒度，也决定怎么补成完整时间戳。 */
  const BUCKET_WIDTH = { day: 10, hour: 13, minute: 16 };
  const BUCKET_STEP_MS = { day: 86400000, hour: 3600000, minute: 60000 };

  function bucketDate(b) {
    const n = String(b).length;
    return new Date(n <= 10 ? b + "T00:00:00Z" : n <= 13 ? b + ":00:00Z" : b + ":00Z");
  }
  function bucketLabel(b, bucket) {
    const d = bucketDate(b);
    if (isNaN(d)) return String(b);
    if (bucket === "minute") {
      return d.toLocaleTimeString("zh-CN", { hour: "2-digit", minute: "2-digit", hour12: false });
    }
    if (bucket === "day") {
      return d.toLocaleDateString("zh-CN", { month: "2-digit", day: "2-digit" });
    }
    return d.toLocaleString("zh-CN",
      { month: "2-digit", day: "2-digit", hour: "2-digit", hour12: false });
  }

  /* 把稀疏的服务端序列补成覆盖整个时间窗的连续桶。
     不补的话「这一小时跑了 31 次」会画成一根占满整幅的实心块——看着像色块而不是图表，
     更看不出这段时间里其实大部分时候是空闲的。 */
  function fillSeries(series, bucket, hours, generatedAt) {
    const step = BUCKET_STEP_MS[bucket] || BUCKET_STEP_MS.hour;
    const width = BUCKET_WIDTH[bucket] || BUCKET_WIDTH.hour;
    const end = new Date(generatedAt);
    if (isNaN(end)) return series;
    end.setUTCMilliseconds(0);
    end.setUTCSeconds(0);
    if (bucket !== "minute") end.setUTCMinutes(0);
    if (bucket === "day") end.setUTCHours(0);

    const count = Math.max(Math.round((hours * 3600000) / step), 1);
    const byKey = new Map(series.map((s) => [s.bucket, s]));
    const out = [];
    for (let i = count - 1; i >= 0; i--) {
      const key = new Date(end.getTime() - i * step).toISOString().slice(0, width);
      out.push(byKey.get(key)
        || { bucket: key, ops: 0, failed: 0, writes: 0, bytes_read: 0, rows_total: 0 });
    }
    return out;
  }

  // ---------------------------------------------------------------- 取数
  async function load() {
    if (loading) return;
    loading = true;
    try {
      const resp = await fetch("/admin/dashboard/data?window=" + encodeURIComponent(win),
                               { headers: { Accept: "application/json" } });
      if (resp.status === 401) { location.href = "/admin/login"; return; }
      const body = await resp.json();
      if (body.ok === false) throw new Error(body.error || "读取看板数据失败");
      data = body;
      render();
    } catch (e) {
      const bar = document.getElementById("dash-err");
      if (bar) {
        bar.textContent = "看板数据获取失败：" + (e && e.message ? e.message : e);
        bar.style.display = "block";
      }
    } finally {
      loading = false;
    }
  }

  function schedule() {
    clearInterval(pollTimer);
    if (auto) pollTimer = setInterval(load, REFRESH_MS);
  }

  // ---------------------------------------------------------------- 画图
  /* 柱图：只用 <rect> 填充，配 preserveAspectRatio="none" 做横向拉伸——
     没有描边就不会被非等比缩放拉变形，也就不需要读容器宽度再算坐标。 */
  function barChart(series, pick, color, fmt) {
    if (!series.length) return '<div class="chart-empty">这个时间窗内没有操作</div>';
    const W = 600, H = 100, gap = series.length > 60 ? 0.5 : 2;
    const bw = W / series.length;
    const max = Math.max(...series.map((s) => pick(s).total), 1);
    const bars = series.map((s, i) => {
      const v = pick(s);
      const x = (i * bw + gap / 2).toFixed(2);
      const w = Math.max(bw - gap, 0.6).toFixed(2);
      const h = (v.total / max) * H;
      const hb = v.bad ? (v.bad / max) * H : 0;
      const tip = esc(bucketLabel(s.bucket, data.bucket) + " · " + fmt(v));
      return (
        `<rect x="${x}" y="${(H - h).toFixed(2)}" width="${w}" height="${Math.max(h, v.total ? 1 : 0).toFixed(2)}"`
        + ` fill="${color}"><title>${tip}</title></rect>`
        + (hb ? `<rect x="${x}" y="${(H - hb).toFixed(2)}" width="${w}" height="${Math.max(hb, 1).toFixed(2)}"`
                + ` fill="#c0392b"><title>${tip}</title></rect>` : "")
      );
    }).join("");
    const first = bucketLabel(series[0].bucket, data.bucket);
    const last = bucketLabel(series[series.length - 1].bucket, data.bucket);
    return `<div class="chart"><svg viewBox="0 0 ${W} ${H}" preserveAspectRatio="none">${bars}</svg>`
      + `<div class="xaxis"><span>${esc(first)}</span><span>峰值 ${esc(fmt({ total: max }))}</span>`
      + `<span>${esc(last)}</span></div></div>`;
  }

  function rankList(items, valueOf, fmt) {
    if (!items.length) return '<div class="dash-empty">暂无数据</div>';
    const max = Math.max(...items.map(valueOf), 1);
    return items.map((it) =>
      `<div class="rank"><span class="nm" title="${esc(it.name)}">${esc(it.name)}</span>`
      + `<span class="n">${esc(fmt(it))}</span>`
      + `<span class="bar"><i style="width:${((valueOf(it) / max) * 100).toFixed(1)}%"></i></span></div>`
    ).join("");
  }

  // ---------------------------------------------------------------- 渲染
  function render() {
    const d = data;
    if (!d) return;
    const t = d.traffic, c = d.connections;
    const failed = (t.rejected || 0) + (t.error || 0);

    document.getElementById("dash-win").innerHTML = WINDOWS.map(([k, label]) =>
      `<button data-win="${k}" class="${k === win ? "on" : ""}">${esc(label)}</button>`).join("");
    document.getElementById("dash-updated").textContent =
      "更新于 " + new Date(d.generated_at).toLocaleTimeString("zh-CN", { hour12: false })
      + " · 已运行 " + dur(d.uptime_s * 1000);

    document.getElementById("dash-tiles").innerHTML = [
      tile("已配置连接", num(c.configured),
           c.unhealthy ? `<b>${c.unhealthy}</b> 条连接异常` : "全部正常",
           c.unhealthy ? "bad" : ""),
      tile("此刻占用连接", num(c.checked_out),
           `池内 ${num(c.pooled_engines)} 个引擎`),
      tile("正在执行", num(d.live.count),
           d.live.count ? "见下方「此刻正在执行」" : "空闲"),
      tile("操作数", num(t.ops),
           `写 ${num(t.writes)} · 失败 ${num(failed)}`, failed ? "warn" : ""),
      tile("读出数据量", bytes(t.bytes_read), `${num(t.rows_read)} 行`),
      tile("写入影响行", num(t.rows_written), `平均耗时 ${num(t.avg_ms)} ms`),
      tile("待审批", num(d.approvals.pending),
           d.approvals.pending ? '<a href="/admin/approvals">去处理 →</a>' : "无",
           d.approvals.pending ? "warn" : ""),
    ].join("");

    const series = fillSeries(d.series, d.bucket, d.window_hours, d.generated_at);
    document.getElementById("dash-chart-ops").innerHTML =
      barChart(series, (s) => ({ total: s.ops, bad: s.failed }), "#0d9488",
               (v) => num(v.total) + " 次" + (v.bad ? "（失败 " + num(v.bad) + "）" : ""));
    document.getElementById("dash-chart-bytes").innerHTML =
      barChart(series, (s) => ({ total: s.bytes_read, bad: 0 }), "#5eead4",
               (v) => bytes(v.total));
    document.getElementById("dash-ops-total").textContent =
      num(t.ops) + " 次 · " + num(t.sessions) + " 个会话";
    document.getElementById("dash-bytes-total").textContent = bytes(t.bytes_read);

    renderLive();
    renderConnections(c.items);
    renderSessions(d.sessions);
    renderBudgets(d.budgets || []);

    document.getElementById("dash-top-conn").innerHTML =
      rankList(d.top.connections, (i) => i.ops, (i) => num(i.ops) + " 次 / " + bytes(i.bytes_read));
    document.getElementById("dash-top-tool").innerHTML =
      rankList(d.top.tools, (i) => i.ops, (i) => num(i.ops) + " 次");
    document.getElementById("dash-top-agent").innerHTML =
      rankList(d.top.agents, (i) => i.ops, (i) => num(i.ops) + " 次");
  }

  function tile(label, value, sub, cls) {
    return `<div class="tile ${cls || ""}"><div class="lab">${esc(label)}</div>`
      + `<div class="val">${esc(value)}</div><div class="sub">${sub || ""}</div></div>`;
  }

  function renderLive() {
    const ops = data.live.ops || [];
    const box = document.getElementById("dash-live");
    const now = Date.now();
    const alive = new Set();
    ops.forEach((o) => {
      alive.add(o.op_id);
      if (!anchors.has(o.op_id)) anchors.set(o.op_id, now - o.elapsed_ms);
    });
    anchors.forEach((_, id) => { if (!alive.has(id)) anchors.delete(id); });

    if (!ops.length) {
      box.innerHTML = '<div class="dash-empty">当前没有正在执行的查询。</div>';
      return;
    }
    box.innerHTML = '<div class="tablewrap"><table><thead><tr>'
      + "<th>已执行</th><th>连接</th><th>工具</th><th>会话 / agent</th><th>SQL</th>"
      + "</tr></thead><tbody>"
      + ops.map((o) => {
        const sid = o.session_id
          ? `<a href="/admin/audit?session_id=${encodeURIComponent(o.session_id)}">`
            + esc(o.session_id.slice(0, 8)) + "</a>"
          : "—";
        return "<tr>"
          + `<td class="num" data-elapsed="${o.op_id}">${esc(dur(o.elapsed_ms))}</td>`
          + `<td><code>${esc(o.project)}/${esc(o.connection)}</code></td>`
          + `<td>${esc(o.tool)}</td>`
          + `<td>${sid}<br><span class="muted">${esc(o.agent || "—")}</span></td>`
          + `<td><div class="dash-sql" title="${esc(o.sql)}">${esc(o.sql || "—")}</div></td>`
          + "</tr>";
      }).join("") + "</tbody></table></div>";
  }

  // 秒表只重写「已执行」那一格，不重排表格——每秒重画整张表会让选中/滚动位置乱跳
  function tickElapsed() {
    const now = Date.now();
    document.querySelectorAll("[data-elapsed]").forEach((cell) => {
      const anchor = anchors.get(Number(cell.dataset.elapsed));
      if (anchor != null) cell.textContent = dur(now - anchor);
    });
  }

  function renderConnections(items) {
    const box = document.getElementById("dash-conns");
    if (!items.length) {
      box.innerHTML = '<div class="dash-empty">还没有配置任何连接。'
        + '<a href="/admin/settings?tab=connections">去添加 →</a></div>';
      return;
    }
    const stateText = { ok: "正常", unavailable: "不可用", exhausted: "需人介入" };
    box.innerHTML = '<div class="tablewrap"><table><thead><tr>'
      + "<th>连接</th><th>引擎</th><th>环境</th><th>状态</th>"
      + '<th class="num">引擎数</th><th class="num">占用连接</th><th>最近错误</th>'
      + "</tr></thead><tbody>"
      + items.map((i) => {
        const state = i.state || "ok";
        let note = "—";
        if (state !== "ok") {
          note = `<div class="muted">${esc(i.last_error || "")}</div>`
            + (i.retry_in_s ? `<div class="muted">约 ${i.retry_in_s}s 后自动重试</div>` : "");
        }
        // sqlite 这类连接没有 host，拼成「— · /path/to.db」只会多出一个占位横杠
        const meta = [i.host, i.database, i.tunnel ? "SSH 隧道" : ""].filter(Boolean);
        return "<tr>"
          + `<td><b>${esc(i.project)}/${esc(i.connection)}</b>`
          + `<br><span class="muted mono">${esc(meta.join(" · ") || "—")}</span></td>`
          + `<td>${esc(i.engine)}</td>`
          + `<td>${esc(i.environment || "—")}</td>`
          + `<td><span class="dot dot-${esc(state)}"></span>${esc(stateText[state] || state)}</td>`
          + `<td class="num">${num(i.engines)}</td>`
          + `<td class="num">${num(i.checked_out)}</td>`
          + `<td>${note}</td></tr>`;
      }).join("") + "</tbody></table></div>";
  }

  function renderSessions(sessions) {
    const box = document.getElementById("dash-sessions");
    if (!sessions.length) {
      box.innerHTML = '<div class="dash-empty">这个时间窗内没有会话活动。</div>';
      return;
    }
    box.innerHTML = '<div class="tablewrap"><table><thead><tr>'
      + '<th>会话</th><th>agent</th><th class="num">操作</th><th class="num">写</th><th>最近活动</th>'
      + "</tr></thead><tbody>"
      + sessions.map((s) => {
        const name = s.title || "（未声明名字）";
        return "<tr>"
          + `<td><a href="/admin/audit?session_id=${encodeURIComponent(s.session_id)}">${esc(name)}</a>`
          + `<br><span class="muted mono">${esc((s.session_id || "").slice(0, 12))}</span></td>`
          + `<td>${esc(s.agent || "—")}</td>`
          + `<td class="num">${num(s.ops)}</td>`
          + `<td class="num">${num(s.writes)}</td>`
          + `<td class="muted">${esc(localTime(s.last_ts))}</td></tr>`;
      }).join("") + "</tbody></table></div>";
  }

  function renderBudgets(items) {
    const box = document.getElementById("dash-budgets");
    if (!items.length) {
      box.innerHTML = '<div class="dash-empty">本次服务启动以来还没有 agent 取过数。</div>';
      return;
    }
    box.innerHTML = '<div class="tablewrap"><table><thead><tr>'
      + '<th>会话</th><th class="num">已用</th><th class="num">占配额</th>'
      + '<th class="num">取数次数</th><th class="num">已放行</th><th>最近放行理由</th>'
      + "</tr></thead><tbody>"
      + items.map((b) => {
        // 只在接近/超出配额时上色——平时全绿一片反而看不出哪个该管
        const pct = b.percent;
        const color = pct >= 100 ? "#c0392b" : pct >= 75 ? "#b45309" : "";
        return "<tr>"
          + `<td><span class="mono">${esc((b.session_id || "-").slice(0, 12))}</span></td>`
          + `<td class="num">${num(b.used_chars)} 字符<br>`
          + `<span class="muted">≈${num(b.used_tokens)} token</span></td>`
          + `<td class="num" style="${color ? "color:" + color + ";font-weight:600" : ""}">`
          + `${b.enabled ? pct + "%" : "不限"}</td>`
          + `<td class="num">${num(b.calls)}</td>`
          + `<td class="num">${num(b.grants)}</td>`
          + `<td class="muted">${esc(b.last_reason || "—")}</td></tr>`;
      }).join("") + "</tbody></table></div>";
  }

  // ---------------------------------------------------------------- 交互
  document.getElementById("dash-win").addEventListener("click", (e) => {
    const btn = e.target.closest("button[data-win]");
    if (!btn) return;
    win = btn.dataset.win;
    localStorage.setItem("dbm.dash.window", win);
    load();
  });

  const autoBox = document.getElementById("dash-auto");
  autoBox.checked = auto;
  autoBox.addEventListener("change", () => {
    auto = autoBox.checked;
    localStorage.setItem("dbm.dash.auto", auto ? "1" : "0");
    document.getElementById("dash-dot").classList.toggle("beat", auto);
    schedule();
    if (auto) load();
  });
  document.getElementById("dash-dot").classList.toggle("beat", auto);

  // 页面被切到后台时停轮询（省得离开一晚上回来堆一屏请求），回到前台立刻补一次
  document.addEventListener("visibilitychange", () => {
    if (document.hidden) clearInterval(pollTimer);
    else if (auto) { load(); schedule(); }
  });

  setInterval(tickElapsed, 1000);
  load();
  schedule();
})();
