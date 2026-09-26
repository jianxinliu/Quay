/* 看板前端（/admin/dashboard）。原生 JS + echarts，无框架——这页只是「拉一份 JSON
   再画出来」，没有本地编辑状态，用不上 Vue（查询台/Redis 才需要）。

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

  /* 连接表排序。默认按状态倒序——**异常的排最前**，看板上先该看见的就是它们；
     同状态内按名字排，保证顺序稳定（每 5s 重画一次，顺序抖动会很刺眼）。 */
  const STATE_RANK = { unprobed: 0, ok: 1, unavailable: 2, exhausted: 3 };
  const CONN_COLS = [
    { key: "name", label: "连接", get: (i) => `${i.project}/${i.connection}` },
    { key: "engine", label: "引擎", get: (i) => i.engine },
    { key: "environment", label: "环境", get: (i) => i.environment || "" },
    { key: "state", label: "状态", get: (i) => STATE_RANK[i.state] ?? 0 },
    { key: "engines", label: "引擎数", get: (i) => i.engines, num: true },
    { key: "checked_out", label: "占用连接", get: (i) => i.checked_out, num: true },
  ];
  let connSort = { key: "state", dir: "desc" };
  try {
    const saved = JSON.parse(localStorage.getItem("dbm.dash.connSort") || "null");
    if (saved && CONN_COLS.some((c) => c.key === saved.key)) connSort = saved;
  } catch (e) { /* 存坏了就用默认，不值得为它报错 */ }

  /* 引擎用品牌图标而不是文字：一列文字全是同一个形状，扫视时分不出哪条是 MySQL；
     图标一眼就分得开。文件与后台连接列表共用同一套（static/db-icons/）。 */
  const ENGINE_ICON = {
    mysql: "mysql", postgres: "postgresql", sqlite: "sqlite",
    clickhouse: "clickhouse", redis: "redis", duckdb: "duckdb",
  };
  function engineCell(engine) {
    const file = ENGINE_ICON[engine];
    const label = esc(engine || "—");
    if (!file) return `<span class="mono muted">${label}</span>`;
    return `<img class="eng-ico" src="/admin/static/db-icons/${file}.svg" alt="${label}"`
      + ` title="${label}">`;
  }

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
        || { bucket: key, ops: 0, rejected: 0, errors: 0, writes: 0, bytes_read: 0, rows_total: 0 });
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

  // ---------------------------------------------------------------- 图表
  /* 用 echarts（已 vendored 在 static/echarts.min.js，查询台的图表也用它）而不是手绘 SVG：
     手绘的版本给不了 hover tooltip、坐标轴与图例，而看板上「那根柱子到底是几点、多少次、
     其中失败几次」正是要看的东西。

     实例只建一次、之后 setOption 更新——每 5s 重建会闪，也会漏掉 dispose 导致内存泄漏。 */
  const charts = {};

  function chartOf(id) {
    if (typeof echarts === "undefined") return null;
    if (!charts[id]) {
      const el = document.getElementById(id);
      if (!el) return null;
      charts[id] = echarts.init(el, null, { renderer: "canvas" });
    }
    return charts[id];
  }

  // 图表颜色从 CSS 变量取（admin-chrome.css 的 --chart-*），深浅主题各一套；
  // 样式表在 <head>、本脚本在 body 末尾，初始化时变量已可读。
  const cv = (name) => getComputedStyle(document.documentElement).getPropertyValue(name).trim();
  const AXIS_STYLE = {
    axisLine: { lineStyle: { color: cv("--chart-axis") } },
    axisTick: { show: false },
    axisLabel: { color: cv("--chart-label"), fontSize: 11 },
  };
  const GRID = { left: 8, right: 12, top: 28, bottom: 4, containLabel: true };
  const TOOLTIP_BASE = {
    trigger: "axis",
    axisPointer: { type: "shadow" },
    backgroundColor: cv("--chart-tip-bg"),
    borderWidth: 0,
    padding: [8, 11],
    textStyle: { color: cv("--chart-tip-ink"), fontSize: 12 },
    extraCssText: "border-radius:8px;box-shadow:0 6px 20px rgba(15,20,27,.22)",
  };

  // 空窗口不画空坐标轴，直接说「没有操作」——一条平线比一句话更让人犯嘀咕
  function toggleEmpty(id, isEmpty) {
    const el = document.getElementById(id);
    el.classList.toggle("is-empty", isEmpty);
    el.dataset.emptyText = "这个时间窗内没有操作";
    return isEmpty;
  }

  /* 三段而不是两段：**「被挡下」不是失败**。agent 每提交一次写操作都会先落一条
     rejected（生成审批单、没落库），那是审批流的正常一步；把它算进失败，看板会在
     一切正常的时候显示一片红。三段相加仍等于总操作数，堆叠才有意义。 */
  function renderOpsChart(series, bucket) {
    const labels = series.map((s) => bucketLabel(s.bucket, bucket));
    const rejected = series.map((s) => s.rejected || 0);
    const errors = series.map((s) => s.errors || 0);
    const ok = series.map((s, i) => Math.max(s.ops - rejected[i] - errors[i], 0));
    const writes = series.map((s) => s.writes);
    if (toggleEmpty("dash-chart-ops", series.every((s) => !s.ops))) return;
    const chart = chartOf("dash-chart-ops");
    if (!chart) return;
    chart.setOption({
      grid: GRID,
      legend: {
        top: 0, right: 0, itemWidth: 9, itemHeight: 9, itemGap: 14,
        textStyle: { color: cv("--chart-legend"), fontSize: 11 },
        data: ["成功", "被挡下", "出错"],
      },
      tooltip: {
        ...TOOLTIP_BASE,
        formatter: (ps) => {
          const i = ps[0].dataIndex;
          const total = ok[i] + rejected[i] + errors[i];
          // 「其中写」不进堆叠（写与失败会重叠、堆起来不等于总数），只在 tooltip 里说明
          return `<b>${labels[i]}</b><br>共 ${num(total)} 次`
            + `<br>成功 ${num(ok[i])} · 被挡下 ${num(rejected[i])} · 出错 ${num(errors[i])}`
            + `<br>其中写操作 ${num(writes[i])} 次`;
        },
      },
      xAxis: { type: "category", data: labels, ...AXIS_STYLE },
      yAxis: {
        type: "value", minInterval: 1, ...AXIS_STYLE,
        splitLine: { lineStyle: { color: cv("--chart-grid") } },
      },
      series: [
        { name: "成功", type: "bar", stack: "ops", data: ok,
          itemStyle: { color: cv("--chart-ok"), borderRadius: [2, 2, 0, 0] },
          emphasis: { itemStyle: { color: cv("--chart-ok-2") } } },
        { name: "被挡下", type: "bar", stack: "ops", data: rejected,
          itemStyle: { color: cv("--chart-blocked"), borderRadius: [2, 2, 0, 0] },
          emphasis: { itemStyle: { color: cv("--chart-blocked-2") } } },
        { name: "出错", type: "bar", stack: "ops", data: errors,
          itemStyle: { color: cv("--chart-err"), borderRadius: [2, 2, 0, 0] },
          emphasis: { itemStyle: { color: cv("--chart-err-2") } } },
      ],
    }, { notMerge: true });
    chart.resize();
  }

  function renderBytesChart(series, bucket) {
    const labels = series.map((s) => bucketLabel(s.bucket, bucket));
    const vals = series.map((s) => s.bytes_read);
    const rows = series.map((s) => s.rows_total);
    if (toggleEmpty("dash-chart-bytes", vals.every((v) => !v))) return;
    const chart = chartOf("dash-chart-bytes");
    if (!chart) return;
    chart.setOption({
      grid: GRID,
      tooltip: {
        ...TOOLTIP_BASE,
        axisPointer: { type: "line" },
        formatter: (ps) => {
          const i = ps[0].dataIndex;
          return `<b>${labels[i]}</b><br>读出 ${bytes(vals[i])}<br>${num(rows[i])} 行`;
        },
      },
      xAxis: { type: "category", boundaryGap: false, data: labels, ...AXIS_STYLE },
      yAxis: {
        type: "value", ...AXIS_STYLE,
        axisLabel: { ...AXIS_STYLE.axisLabel, formatter: (v) => bytes(v) },
        splitLine: { lineStyle: { color: cv("--chart-grid") } },
      },
      series: [{
        name: "读出数据量", type: "line", data: vals, smooth: true,
        showSymbol: false, symbolSize: 6,
        lineStyle: { width: 2, color: cv("--chart-ok") },
        itemStyle: { color: cv("--chart-ok") },
        areaStyle: {
          color: new echarts.graphic.LinearGradient(0, 0, 0, 1, [
            { offset: 0, color: cv("--chart-area-0") },
            { offset: 1, color: cv("--chart-area-1") },
          ]),
        },
      }],
    }, { notMerge: true });
    chart.resize();
  }

  // 容器宽度随侧栏/窗口变化，echarts 不会自己跟
  window.addEventListener("resize", () => {
    Object.values(charts).forEach((c) => c.resize());
  });

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
    // 「被挡下」是审批流的正常一步（首提生成审批单），不是失败——只有 error 才该告警
    const blocked = t.rejected || 0;
    const errors = t.error || 0;

    document.getElementById("dash-win").innerHTML = WINDOWS.map(([k, label]) =>
      `<button data-win="${k}" class="${k === win ? "on" : ""}">${esc(label)}</button>`).join("");
    document.getElementById("dash-updated").textContent =
      "更新于 " + new Date(d.generated_at).toLocaleTimeString("zh-CN", { hour12: false })
      + " · 已运行 " + dur(d.uptime_s * 1000);

    document.getElementById("dash-tiles").innerHTML = [
      tile("已配置连接", num(c.configured), connTileSub(c), c.unhealthy ? "bad" : ""),
      tile("此刻占用连接", num(c.checked_out),
           `池内 ${num(c.pooled_engines)} 个引擎`),
      tile("正在执行", num(d.live.count),
           d.live.count ? "见下方「此刻正在执行」" : "空闲"),
      tile("操作数", num(t.ops),
           `写 ${num(t.writes)} · 被挡下 ${num(blocked)} · 出错 ${num(errors)}`,
           errors ? "warn" : ""),
      tile("读出数据量", bytes(t.bytes_read), `${num(t.rows_read)} 行`),
      tile("写入影响行", num(t.rows_written), `平均耗时 ${num(t.avg_ms)} ms`),
      tile("待审批", num(d.approvals.pending),
           d.approvals.pending ? '<a href="/admin/approvals">去处理 →</a>' : "无",
           d.approvals.pending ? "warn" : ""),
    ].join("");

    const series = fillSeries(d.series, d.bucket, d.window_hours, d.generated_at);
    renderOpsChart(series, d.bucket);
    renderBytesChart(series, d.bucket);
    document.getElementById("dash-ops-total").textContent =
      num(t.ops) + " 次 · " + num(t.sessions) + " 个会话";
    document.getElementById("dash-bytes-total").textContent = bytes(t.bytes_read);

    renderLive();
    renderOnboard(c);
    renderConnections(c.items);
    renderSessions(d.sessions, d.session_days);
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
    box.innerHTML = '<div class="tablewrap dash-scroll"><table><thead><tr>'
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

  function sortConnections(items) {
    const col = CONN_COLS.find((c) => c.key === connSort.key) || CONN_COLS[3];
    const sign = connSort.dir === "desc" ? -1 : 1;
    const byName = (a, b) => `${a.project}/${a.connection}`.localeCompare(
      `${b.project}/${b.connection}`, "zh-CN");
    return items.slice().sort((a, b) => {
      const x = col.get(a), y = col.get(b);
      const cmp = col.num || typeof x === "number"
        ? (x || 0) - (y || 0)
        : String(x).localeCompare(String(y), "zh-CN");
      return cmp ? cmp * sign : byName(a, b);   // 同值时用名字兜底，顺序才稳定
    });
  }

  /* 「全部正常」只有在每条连接都真的连过时才成立；从未触达过的连接是「未探测」，
     新装实例上把示例配置里那几台不存在的库显示成正常，是首屏最大的误导。 */
  function connTileSub(c) {
    if (c.unhealthy) return `<b>${c.unhealthy}</b> 条连接异常`;
    if (!c.configured) return "尚未配置";
    if (c.unprobed === c.configured) return "均未探测";
    if (c.unprobed) return `${c.configured - c.unprobed} 条正常 · ${c.unprobed} 条未探测`;
    return "全部正常";
  }

  function renderOnboard(c) {
    const box = document.getElementById("dash-onboard");
    if (c.configured) { box.style.display = "none"; return; }
    box.style.display = "";
    box.innerHTML = "<h3>开始使用</h3>"
      + "<ol>"
      + '<li><a href="/admin/settings?tab=connections">新建连接</a>——账号密码进系统钥匙串，配置文件只存引用。</li>'
      + '<li>在<a href="/admin/sql">查询台</a>跑第一条 SQL。</li>'
      + '<li>把 MCP 端点 <code>/mcp</code> 接给 agent（README「接入 Agent」）；它的写操作会出现在'
      + '<a href="/admin/approvals">审批中心</a>。</li>'
      + "</ol>";
  }

  function renderConnections(items) {
    const box = document.getElementById("dash-conns");
    if (!items.length) {
      box.innerHTML = '<div class="dash-empty">还没有配置任何连接。'
        + '<a href="/admin/settings?tab=connections">去添加 →</a></div>';
      return;
    }
    const stateText = { ok: "正常", unprobed: "未探测", unavailable: "不可用", exhausted: "需人介入" };
    const arrow = (k) => (connSort.key === k
      ? `<i class="sarrow ${connSort.dir}"></i>` : '<i class="sarrow"></i>');
    const head = CONN_COLS.map((c) =>
      `<th class="sortable${c.num ? " num" : ""}" data-sort="${c.key}"`
      + `${connSort.key === c.key ? ' aria-sort="' + connSort.dir + '"' : ""}>`
      + `${esc(c.label)}${arrow(c.key)}</th>`).join("");

    box.innerHTML = '<div class="tablewrap dash-scroll"><table><thead><tr>'
      + head + "<th>最近错误</th></tr></thead><tbody>"
      + sortConnections(items).map((i) => {
        const state = i.state || "ok";
        let note = "—";
        if (state !== "ok" && state !== "unprobed") {
          note = `<div class="muted">${esc(i.last_error || "")}</div>`
            + (i.retry_in_s ? `<div class="muted">约 ${i.retry_in_s}s 后自动重试</div>` : "");
        }
        // sqlite 这类连接没有 host，拼成「— · /path/to.db」只会多出一个占位横杠
        const meta = [i.host, i.database, i.tunnel ? "SSH 隧道" : ""].filter(Boolean);
        return "<tr>"
          + `<td><b>${esc(i.project)}/${esc(i.connection)}</b>`
          + `<span class="connmeta muted mono" title="${esc(meta.join(" · "))}">`
          + `${esc(meta.join(" · ") || "—")}</span></td>`
          + `<td class="eng">${engineCell(i.engine)}</td>`
          + `<td>${esc(i.environment || "—")}</td>`
          + `<td><span class="dot dot-${esc(state)}"></span>${esc(stateText[state] || state)}</td>`
          + `<td class="num">${num(i.engines)}</td>`
          + `<td class="num">${i.checked_out == null
              ? '<span class="muted" title="这类连接池不报占用数">—</span>'
              : num(i.checked_out)}</td>`
          + `<td>${note}</td></tr>`;
      }).join("") + "</tbody></table></div>";
  }

  function renderSessions(sessions, days) {
    const box = document.getElementById("dash-sessions");
    document.getElementById("dash-sessions-range").textContent =
      `最近 ${days} 天 · ${sessions.length} 个`;
    if (!sessions.length) {
      box.innerHTML = `<div class="dash-empty">最近 ${days} 天没有会话活动。</div>`;
      return;
    }
    box.innerHTML = '<div class="tablewrap dash-scroll"><table><thead><tr>'
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
    box.innerHTML = '<div class="tablewrap dash-scroll"><table><thead><tr>'
      + '<th>会话</th><th class="num">已用</th><th class="num">占配额</th>'
      + '<th class="num">取数次数</th><th class="num">已放行</th><th>最近放行理由</th>'
      + "</tr></thead><tbody>"
      + items.map((b) => {
        // 只在接近/超出配额时上色——平时全绿一片反而看不出哪个该管
        const pct = b.percent;
        const color = pct >= 100 ? cv("--danger-ink") : pct >= 75 ? cv("--warn-ink") : "";
        return "<tr>"
          + `<td><span class="mono">${esc((b.session_id || "-").slice(0, 12))}</span></td>`
          + `<td class="num">${num(b.used_chars)} 字符<br>`
          // 装了 tiktoken 就是真实分词的结果，不该再挂个「≈」说成估算
          + `<span class="muted" title="${b.tokens_exact
              ? "tiktoken 真实分词（o200k）" : "按字符类别粗估——装上 tokenizer 附加依赖可精确计数"}">`
          + `${b.tokens_exact ? "" : "≈"}${num(b.used_tokens)} token</span></td>`
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

  document.getElementById("dash-conns").addEventListener("click", (e) => {
    const th = e.target.closest("th[data-sort]");
    if (!th) return;
    const key = th.dataset.sort;
    // 点同一列切换升降序，点新列从升序开始（状态列例外：先看异常，默认降序）
    connSort = connSort.key === key
      ? { key, dir: connSort.dir === "asc" ? "desc" : "asc" }
      : { key, dir: key === "state" ? "desc" : "asc" };
    localStorage.setItem("dbm.dash.connSort", JSON.stringify(connSort));
    if (data) renderConnections(data.connections.items);
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
