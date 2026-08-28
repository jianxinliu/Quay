/* 流程独立页：Vue 3 无构建 SPA。URL hash 路由：
 *   /admin/workflows            → 列表页
 *   /admin/workflows#name=xxx   → 详情页（蓝图视图 + 模块视图 tab 切换）
 *   /admin/workflows#new        → 新建对话框
 *
 * 蓝图视图 = 从查询台搬迁的 SVG 画布（拖拽节点、拉线端口、侧栏 cfg 面板）。
 * JOIN 节点端口从固定 left/right 改为动态 in_1..in_N（≤8 路 UI 上限）；
 * 老图 left/right 端口渲染时兼容为 in_1/in_2（后端 compile_graph 也有兼容层）。
 *
 * DgSelect / ENV_COLORS 从 window 引用（dg-select.js 先加载）。
 */
(function () {
  "use strict";
  var API = "/admin/workflows";
  var JOIN_MAX_PORTS = 8;
  var NODE_W = 150, NODE_H = 40;

  function apiGet(url) {
    return fetch(url, { headers: { Accept: "application/json" } })
      .then(function (r) { return r.json().catch(function () { return { ok: false, error: r.statusText }; }); });
  }
  function apiPost(url, data) {
    var body = new URLSearchParams();
    Object.keys(data || {}).forEach(function (k) {
      var v = data[k];
      body.set(k, typeof v === "object" ? JSON.stringify(v) : String(v == null ? "" : v));
    });
    return fetch(url, { method: "POST", body: body,
      headers: { Accept: "application/json", "Content-Type": "application/x-www-form-urlencoded" } })
      .then(function (r) { return r.json().catch(function () { return { ok: false, error: r.statusText }; }); });
  }

  function parseHash() {
    // 详情页 SPA hash 路由。运行详情页 (/admin/workflows/runs/{id}) 不走 hash，
    // 而是让服务端渲染同一个 shell + 由 pathname 触发不同视图。
    var h = (location.hash || "").replace(/^#/, "");
    // 服务端渲染的运行详情页：pathname 形如 /admin/workflows/runs/42
    var m = /^\/admin\/workflows\/runs\/(\d+)/.exec(location.pathname || "");
    if (m) return { view: "run", runId: parseInt(m[1], 10) };
    if (!h) return { view: "list" };
    if (h === "new") return { view: "new" };
    if (h === "schedules") return { view: "schedules" };
    var m2 = /^name=(.+)$/.exec(h);
    if (m2) return { view: "detail", name: decodeURIComponent(m2[1]) };
    return { view: "list" };
  }

  function fmtTime(iso) {
    if (!iso) return "—";
    try {
      var d = new Date(iso);
      if (isNaN(d.getTime())) return iso;
      var pad = function (n) { return n < 10 ? "0" + n : "" + n; };
      return d.getFullYear() + "-" + pad(d.getMonth() + 1) + "-" + pad(d.getDate())
        + " " + pad(d.getHours()) + ":" + pad(d.getMinutes());
    } catch (e) { return iso; }
  }

  // ---------- JOIN 端口辅助（N 路） ----------
  // 一个 join 节点用 cfg.ports_n（默认 2）声明输入端口数。老图带 left/right 也认。
  function joinPortsN(node) {
    if (!node || node.type !== "join") return 0;
    var n = parseInt((node.cfg || {}).ports_n, 10);
    return isFinite(n) && n >= 2 ? Math.min(JOIN_MAX_PORTS, n) : 2;
  }
  function nodeInPorts(node) {
    if (node.type === "source" || node.type === "file") return [];
    if (node.type === "join") {
      var arr = [];
      for (var i = 1; i <= joinPortsN(node); i++) arr.push("in_" + i);
      return arr;
    }
    return ["in"];
  }
  function normalizePort(node, port) {
    // 老图 join 端口 left/right → in_1/in_2，用于连线渲染
    if (node.type !== "join") return port || "in";
    if (port === "left") return "in_1";
    if (port === "right") return "in_2";
    return port || "in_1";
  }
  function portY(node, port) {
    // 输入口按序号均匀分布节点左缘（节点高 40px）
    if (port === "out") return node.y + NODE_H / 2;
    if (node.type === "join") {
      var p = normalizePort(node, port);
      var idx = parseInt(p.split("_")[1], 10) || 1;
      var n = joinPortsN(node);
      var step = NODE_H / (n + 1);
      return node.y + step * idx;
    }
    return node.y + NODE_H / 2;
  }

  // ---------- 蓝图视图：SVG 画布组件 ----------
  var Blueprint = {
    name: "wf-blueprint",
    // 节点状态由父组件传下来（运行时染色），选中态双向绑定
    props: ["graph", "conn", "nodeStatus", "selectedNodeId"],
    emits: ["update:graph", "open-node", "select-node", "run"],
    data: function () {
      return {
        linkDraft: null       // 拉线中 {from, x, y}
      };
    },
    computed: {
      sel: {
        get: function () { return this.selectedNodeId; },
        set: function (v) { this.$emit("select-node", v); }
      }
    },
    methods: {
      persist: function () { this.$emit("update:graph", this.graph); },
      typeLabel: function (t) {
        return { source: "取数", file: "文件", filter: "过滤", join: "JOIN",
                 aggregate: "聚合", sql: "SQL", output: "输出",
                 describe: "描述", distinct: "去重", percentile: "分位",
                 correlate: "相关", pivot: "透视" }[t] || t;
      },
      nodeDesc: function (n) {
        var c = n.cfg || {};
        if (n.type === "source") return c.conn ? c.conn + (c.sql ? " · " + c.sql : "") : "（选连接）";
        if (n.type === "file") return c.path || "（选文件）";
        if (n.type === "filter") return c.where ? "WHERE " + c.where : "（填条件）";
        if (n.type === "join") {
          var kind = c.kind || "INNER";
          var pn = joinPortsN(n);
          return kind + " · " + pn + " 路" + (c.on ? " ON " + c.on : "（填 ON）");
        }
        if (n.type === "aggregate") return (c.group ? "BY " + c.group + " · " : "") + (c.aggs || "（填聚合）");
        if (n.type === "sql") return c.sql || "（写 SQL）";
        if (n.type === "output") return (c.order_by ? "ORDER BY " + c.order_by + " " : "")
                                       + "LIMIT " + (c.limit || 1000);
        if (n.type === "describe") return "描述 " + (c.cols || "全部数值列");
        if (n.type === "distinct") return "DISTINCT " + (c.cols || "*");
        if (n.type === "percentile")
          return "分位 " + (c.col || "?") + " · " + (c.quantiles || "0.25,0.5,0.75,0.95");
        if (n.type === "correlate") return "相关 " + (c.cols || "（选列）");
        if (n.type === "pivot")
          return "PIVOT " + (c.on || "?") + " USING " + (c.using || "?");
        return "";
      },
      addNode: function (type) {
        var prefix = { source: "src", file: "file", filter: "flt", join: "join",
                       aggregate: "agg", sql: "sql", output: "out",
                       describe: "desc", distinct: "dist", percentile: "pct",
                       correlate: "corr", pivot: "pv" }[type] || "n";
        var i = 1;
        var names = {};
        (this.graph.nodes || []).forEach(function (n) { names[n.name] = 1; });
        while (names[prefix + i]) i++;
        var id = "n" + Date.now().toString(36) + Math.floor(Math.random() * 1e4).toString(36);
        var cfg = type === "join" ? { kind: "INNER", on: "", select: "", ports_n: 2 }
                : type === "aggregate" ? { group: "", aggs: "" }
                : type === "source" ? { conn: "", sql: "", limit: null }
                : type === "describe" ? { cols: "" }
                : type === "distinct" ? { cols: "" }
                : type === "percentile" ? { col: "", quantiles: "0.25,0.5,0.75,0.95", group: "" }
                : type === "correlate" ? { cols: "" }
                : type === "pivot" ? { on: "", using: "sum(amount)", group: "" }
                : {};
        var count = (this.graph.nodes || []).length;
        this.graph.nodes = (this.graph.nodes || []).concat([{
          id: id, type: type, name: prefix + i,
          x: 30 + (count % 5) * 190, y: 30 + Math.floor(count / 5) * 90, cfg: cfg
        }]);
        this.sel = id;
        this.persist();
      },
      delNode: function (id) {
        this.graph.nodes = (this.graph.nodes || []).filter(function (n) { return n.id !== id; });
        this.graph.edges = (this.graph.edges || []).filter(function (e) {
          return e.from !== id && e.to !== id;
        });
        if (this.sel === id) this.sel = null;
        // nodeStatus 是父组件管理的运行时状态，节点删除后父组件那边下次运行会自动清；
        // 这里不 mutate prop（Vue 会警告）。
        this.persist();
      },
      // ---------- 拖拽 / 连线 ----------
      nodeById: function (id) {
        return (this.graph.nodes || []).find(function (n) { return n.id === id; }) || null;
      },
      portX: function (node, port) {
        if (port === "out") return node.x + NODE_W;
        return node.x;  // pin 靠左缘
      },
      canvasXY: function (ev) {
        var r = this.$refs.canvas.getBoundingClientRect();
        var el = this.$refs.canvas;
        return { x: ev.clientX - r.left + el.scrollLeft, y: ev.clientY - r.top + el.scrollTop };
      },
      edgePath: function (e) {
        var f = this.nodeById(e.from), t = this.nodeById(e.to);
        if (!f || !t) return "";
        var port = normalizePort(t, e.port);
        var ax = this.portX(f, "out"), ay = portY(f, "out");
        var bx = this.portX(t, port), by = portY(t, port);
        var dx = Math.max(40, Math.abs(bx - ax) / 2);
        return "M" + ax + "," + ay + " C" + (ax + dx) + "," + ay + " "
             + (bx - dx) + "," + by + " " + bx + "," + by;
      },
      draftPath: function () {
        var d = this.linkDraft;
        if (!d) return "";
        var f = this.nodeById(d.from);
        if (!f) return "";
        var ax = this.portX(f, "out"), ay = portY(f, "out");
        var dx = Math.max(40, Math.abs(d.x - ax) / 2);
        return "M" + ax + "," + ay + " C" + (ax + dx) + "," + ay + " "
             + (d.x - dx) + "," + d.y + " " + d.x + "," + d.y;
      },
      edgeMid: function (e) {
        var f = this.nodeById(e.from), t = this.nodeById(e.to);
        if (!f || !t) return { x: -99, y: -99 };
        var port = normalizePort(t, e.port);
        var ax = this.portX(f, "out"), ay = portY(f, "out");
        var bx = this.portX(t, port), by = portY(t, port);
        return { x: (ax + bx) / 2, y: (ay + by) / 2 };
      },
      nodeDown: function (ev, node) {
        var self = this;
        this.sel = node.id;
        var start = this.canvasXY(ev), ox = node.x, oy = node.y, moved = false;
        function move(e2) {
          var p = self.canvasXY(e2);
          node.x = Math.max(0, ox + p.x - start.x);
          node.y = Math.max(0, oy + p.y - start.y);
          moved = true;
        }
        function up() {
          window.removeEventListener("mousemove", move);
          window.removeEventListener("mouseup", up);
          if (moved) self.persist();
        }
        window.addEventListener("mousemove", move);
        window.addEventListener("mouseup", up);
        ev.preventDefault();
      },
      portDown: function (ev, node) {
        var self = this;
        var p = this.canvasXY(ev);
        this.linkDraft = { from: node.id, x: p.x, y: p.y };
        function move(e2) {
          if (self.linkDraft) { var q = self.canvasXY(e2); self.linkDraft.x = q.x; self.linkDraft.y = q.y; }
        }
        function up() {
          window.removeEventListener("mousemove", move);
          window.removeEventListener("mouseup", up);
          setTimeout(function () { self.linkDraft = null; }, 0);
        }
        window.addEventListener("mousemove", move);
        window.addEventListener("mouseup", up);
        ev.preventDefault(); ev.stopPropagation();
      },
      portUp: function (ev, node, port) {
        var d = this.linkDraft;
        if (!d || d.from === node.id) return;
        var normalized = normalizePort(node, port);
        // 每个输入口只接一条
        this.graph.edges = (this.graph.edges || []).filter(function (e) {
          if (e.to !== node.id) return true;
          return normalizePort(node, e.port) !== normalized;
        }).concat([{ from: d.from, to: node.id, port: normalized }]);
        this.linkDraft = null;
        this.persist();
      },
      delEdge: function (i) {
        this.graph.edges.splice(i, 1);
        this.persist();
      },
      nodeInPorts: nodeInPorts,
      portTitle: function (node, port) {
        if (node.type !== "join") return "输入";
        var normalized = normalizePort(node, port);
        var idx = parseInt(normalized.split("_")[1], 10) || 1;
        return "输入 " + idx + "（SQL 别名 " + "abcdefghijklmnop".charAt(idx - 1) + "）";
      },
      hasOut: function (n) { return n.type !== "output"; },
      openNode: function (node) { this.$emit("open-node", node.id); }
    },
    template:
      '<div class="wf-blueprint">'
      // 顶栏
      + '<div class="wf-bp-bar">'
      +   '<button class="dg-btn" @click="addNode(\'source\')" title="从任意连接取数为数据集">＋取数</button>'
      +   '<button class="dg-btn" @click="addNode(\'file\')" title="导入本地 CSV/Parquet/JSON">＋文件</button>'
      +   '<button class="dg-btn" @click="addNode(\'filter\')">＋过滤</button>'
      +   '<button class="dg-btn" @click="addNode(\'join\')">＋JOIN</button>'
      +   '<button class="dg-btn" @click="addNode(\'aggregate\')">＋聚合</button>'
      +   '<button class="dg-btn" @click="addNode(\'distinct\')" title="去重">＋去重</button>'
      +   '<button class="dg-btn" @click="addNode(\'describe\')" title="描述统计（count/mean/std/min/25%/50%/75%/max）">＋描述</button>'
      +   '<button class="dg-btn" @click="addNode(\'percentile\')" title="分位数（quantile_cont）">＋分位</button>'
      +   '<button class="dg-btn" @click="addNode(\'correlate\')" title="Pearson 相关系数（两两）">＋相关</button>'
      +   '<button class="dg-btn" @click="addNode(\'pivot\')" title="透视表（DuckDB PIVOT）">＋透视</button>'
      +   '<button class="dg-btn" @click="addNode(\'sql\')" title="自由 SQL（直接引用上游节点名）">＋SQL</button>'
      +   '<button class="dg-btn" @click="addNode(\'output\')">＋输出</button>'
      +   '<span class="wf-bp-hint">拖节点右缘圆点 → 下一节点左缘连线 · 点 ✕ 删连线 · 工作区 {{ (conn||"").split("/")[1] }}</span>'
      + '</div>'
      // 主体：画布 + 侧栏
      + '<div class="wf-bp-body">'
      +   '<div class="wf-bp-canvas" ref="canvas">'
      +     '<svg class="wf-bp-svg">'
      +       '<path v-for="(e,i) in graph.edges" :key="i" class="fe" :d="edgePath(e)"/>'
      +       '<path v-if="linkDraft" class="fe draft" :d="draftPath()"/>'
      +     '</svg>'
      +     '<div v-for="(e,i) in graph.edges" :key="\'x\'+i" class="wf-edge-x"'
      +       ' :style="{left: edgeMid(e).x + \'px\', top: edgeMid(e).y + \'px\'}"'
      +       ' title="删除连线" @click="delEdge(i)">✕</div>'
      +     '<div v-for="n in graph.nodes" :key="n.id" class="wf-fnode"'
      +       ' :class="[n.type, {sel: sel===n.id}, nodeStatus && nodeStatus[n.id] || \'\']"'
      +       ' :style="{left: n.x + \'px\', top: n.y + \'px\'}"'
      +       ' :title="\'双击打开编辑面板\'"'
      +       ' @mousedown="nodeDown($event, n)"'
      +       ' @dblclick="openNode(n)">'
      +       '<div class="hd"><span class="ty">{{ typeLabel(n.type) }}</span>'
      +         '<span class="nm">{{ n.name }}</span>'
      +         '<span class="st">{{ nodeStatus && nodeStatus[n.id]===\'ok\' ? \'✓\' : nodeStatus && nodeStatus[n.id]===\'err\' ? \'✗\' : nodeStatus && nodeStatus[n.id]===\'running\' ? \'⟳\' : \'\' }}</span>'
      +         '<span class="x" @mousedown.stop @click.stop="delNode(n.id)">✕</span></div>'
      +       '<div class="bd">{{ nodeDesc(n) }}</div>'
      +       '<span v-for="p in nodeInPorts(n)" :key="p" class="port pin"'
      +         ' :style="{top: (portY(n, p) - n.y - 5) + \'px\'}"'
      +         ' :title="portTitle(n, p)"'
      +         ' @mousedown.stop @mouseup="portUp($event, n, p)"></span>'
      +       '<span v-if="hasOut(n)" class="port pout" title="拖到下一节点的输入口"'
      +         ' @mousedown="portDown($event, n)"></span>'
      +     '</div>'
      +     '<div v-if="!graph.nodes.length" class="wf-bp-empty">'
      +       '用上方按钮添加节点：取数 → 过滤 / JOIN / 聚合 → 输出，拖节点右缘圆点到下一节点左缘完成连线。'
      +       '<br><br>提示：<b>双击任意节点</b>打开编辑面板（含 SQL 拼装 UI 与预览）。</div>'
      +   '</div>'
      + '</div>'
      + '</div>'
  };
  Blueprint.methods.portY = portY;

  // ---------- 拓扑排序（Kahn；无环即返节点数组）----------
  function topoOrder(graph) {
    var nodes = graph.nodes || [], edges = graph.edges || [];
    var byId = {};
    nodes.forEach(function (n) { byId[n.id] = n; });
    var indeg = {}, downstream = {};
    nodes.forEach(function (n) { indeg[n.id] = 0; downstream[n.id] = []; });
    edges.forEach(function (e) {
      if (!byId[e.from] || !byId[e.to]) return;
      indeg[e.to] = (indeg[e.to] || 0) + 1;
      downstream[e.from].push(e.to);
    });
    var queue = nodes.filter(function (n) { return !indeg[n.id]; }).map(function (n) { return n.id; });
    var order = [];
    while (queue.length) {
      var id = queue.shift();
      order.push(byId[id]);
      downstream[id].forEach(function (t) {
        indeg[t]--;
        if (indeg[t] === 0) queue.push(t);
      });
    }
    // 有环时补上剩余节点（模块视图仍显示，让用户能编辑修复）
    if (order.length < nodes.length) {
      var seen = {};
      order.forEach(function (n) { seen[n.id] = 1; });
      nodes.forEach(function (n) { if (!seen[n.id]) order.push(n); });
    }
    return order;
  }

  // ---------- 上游节点名（供 sql 节点提示） ----------
  function upstreamNamesOf(graph, nodeId) {
    var order = topoOrder(graph);
    var idx = -1;
    for (var i = 0; i < order.length; i++) if (order[i].id === nodeId) { idx = i; break; }
    if (idx < 0) return [];
    return order.slice(0, idx).map(function (n) { return n.name; });
  }

  // ---------- 图表：结果集 → ECharts option（借鉴查询台 renderChart） ----------
  var CHART_PALETTE = ["#5b8dd6", "#57965c", "#d9a343", "#c084fc", "#f472b6", "#60a5fa", "#7ee2a8", "#e8c07a"];
  function chartRowsFromResult(result, cfg) {
    var xi = result.columns.indexOf(cfg.x), yi = result.columns.indexOf(cfg.y);
    if (xi < 0 || yi < 0) return [];
    function num(v) { return typeof v === "string" && v !== "" && !isNaN(+v) ? +v : v; }
    if (!cfg.agg) return result.rows.map(function (r) { return [r[xi], num(r[yi])]; });
    var groups = {}, order = [];
    result.rows.forEach(function (r) {
      var k = r[xi] === null ? "NULL" : String(r[xi]);
      if (!(k in groups)) { groups[k] = []; order.push(k); }
      groups[k].push(num(r[yi]));
    });
    return order.map(function (k) {
      var vals = groups[k].filter(function (v) { return typeof v === "number"; });
      var v;
      if (cfg.agg === "count") v = groups[k].length;
      else if (!vals.length) v = 0;
      else if (cfg.agg === "sum") v = vals.reduce(function (a, b) { return a + b; }, 0);
      else if (cfg.agg === "avg") v = vals.reduce(function (a, b) { return a + b; }, 0) / vals.length;
      else if (cfg.agg === "min") v = Math.min.apply(null, vals);
      else v = Math.max.apply(null, vals);
      return [k, Math.round(v * 1000) / 1000];
    });
  }
  // ---- 多 Y（stacked/grouped/area）：cfg.ys = "col1, col2, col3" ----
  function multiRowsFromResult(result, cfg) {
    var xi = result.columns.indexOf(cfg.x);
    var ys = (cfg.ys || "").split(/[,\s]+/).filter(Boolean);
    if (xi < 0 || !ys.length) return null;
    var yis = ys.map(function (y) { return result.columns.indexOf(y); });
    if (yis.some(function (i) { return i < 0; })) return null;
    function num(v) { return typeof v === "string" && v !== "" && !isNaN(+v) ? +v : v; }
    return {
      cats: result.rows.map(function (r) { return String(r[xi]); }),
      seriesData: ys.map(function (y, si) {
        return { name: y, data: result.rows.map(function (r) { return num(r[yis[si]]); }) };
      })
    };
  }
  // ---- histogram：单列数值分箱 ----
  function histBins(vals, nb) {
    vals = vals.filter(function (v) { return typeof v === "number" && !isNaN(v); });
    if (!vals.length) return { cats: [], counts: [] };
    var mn = Math.min.apply(null, vals), mx = Math.max.apply(null, vals);
    if (mn === mx) return { cats: [String(mn)], counts: [vals.length] };
    var n = nb || Math.min(30, Math.max(5, Math.ceil(Math.sqrt(vals.length))));
    var w = (mx - mn) / n;
    var counts = new Array(n).fill(0);
    vals.forEach(function (v) {
      var i = Math.min(n - 1, Math.floor((v - mn) / w));
      counts[i]++;
    });
    var cats = counts.map(function (_, i) {
      var lo = mn + i * w; return lo.toFixed(2);
    });
    return { cats: cats, counts: counts };
  }
  function chartOptionFor(result, cfg) {
    var axis = { axisLabel: { color: "#9aa0a8" }, axisLine: { lineStyle: { color: "#45484e" } },
                 splitLine: { lineStyle: { color: "#2e3033" } } };
    var opt = { backgroundColor: "transparent", color: CHART_PALETTE, textStyle: { color: "#bcbec4" },
                tooltip: { trigger: /^(pie|scatter|heatmap|funnel)$/.test(cfg.type) ? "item" : "axis",
                           backgroundColor: "#2b2d30", borderColor: "#393b40",
                           textStyle: { color: "#bcbec4" } },
                grid: { left: 16, right: 24, top: 40, bottom: 12, containLabel: true } };
    var t = cfg.type;

    // ---- 多 Y 系列（stacked/grouped/area/multi-line）----
    if (t === "stacked" || t === "grouped" || t === "multi_line" || t === "multi_area") {
      var m = multiRowsFromResult(result, cfg);
      if (!m) return _emptyOpt(opt, "多列 chart 需要在 cfg.ys 里指定 2+ 列");
      opt.legend = { textStyle: { color: "#bcbec4" }, top: 6 };
      opt.grid.top = 34;
      opt.xAxis = Object.assign({ type: "category", data: m.cats }, axis);
      opt.yAxis = Object.assign({ type: "value" }, axis);
      opt.series = m.seriesData.map(function (s) {
        var base = { name: s.name, data: s.data };
        if (t === "stacked") return Object.assign({ type: "bar", stack: "total" }, base);
        if (t === "grouped") return Object.assign({ type: "bar", barMaxWidth: 28 }, base);
        if (t === "multi_line") return Object.assign({ type: "line", smooth: true }, base);
        return Object.assign({ type: "line", smooth: true, areaStyle: {}, stack: "total" }, base);
      });
      return opt;
    }

    // ---- heatmap（相关系数矩阵最合适）----
    if (t === "heatmap") {
      // 期待 result 是 correlate 输出格式：只有 a__b 这样的两列名列
      var cells = [];
      var vars = {};
      result.columns.forEach(function (c, ci) {
        var m2 = /^(.+?)__(.+)$/.exec(c);
        if (!m2 || !result.rows.length) return;
        var val = result.rows[0][ci];
        var v = typeof val === "number" ? val : +val;
        if (isNaN(v)) return;
        vars[m2[1]] = 1; vars[m2[2]] = 1;
        cells.push({ a: m2[1], b: m2[2], v: v });
      });
      var names = Object.keys(vars);
      names.forEach(function (n) { cells.push({ a: n, b: n, v: 1 }); });          // 对角线
      cells.slice(0, cells.length).forEach(function (c) {
        cells.push({ a: c.b, b: c.a, v: c.v });                                    // 对称
      });
      var data = cells.map(function (c) {
        return [names.indexOf(c.a), names.indexOf(c.b), Math.round(c.v * 1000) / 1000];
      });
      opt.xAxis = Object.assign({ type: "category", data: names }, axis);
      opt.yAxis = Object.assign({ type: "category", data: names }, axis);
      opt.visualMap = { min: -1, max: 1, calculable: true, orient: "horizontal",
                        left: "center", bottom: "0",
                        inRange: { color: ["#3b7cff", "#f5f5f5", "#ef4444"] },
                        textStyle: { color: "#bcbec4" } };
      opt.grid.bottom = 50;
      opt.series = [{
        type: "heatmap", data: data,
        label: { show: true, color: "#111", fontSize: 11,
                 formatter: function (p) { return p.data[2]; } }
      }];
      return opt;
    }

    // ---- histogram（单数值列的分布）----
    if (t === "histogram") {
      var yi = result.columns.indexOf(cfg.y);
      if (yi < 0) return _emptyOpt(opt, "histogram 需要选一个数值列 (cfg.y)");
      var vals = result.rows.map(function (r) { return typeof r[yi] === "string" ? +r[yi] : r[yi]; });
      var h = histBins(vals, cfg.bins || 0);
      opt.xAxis = Object.assign({ type: "category", data: h.cats, name: cfg.y }, axis);
      opt.yAxis = Object.assign({ type: "value", name: "count" }, axis);
      opt.series = [{ type: "bar", data: h.counts, barMaxWidth: 42 }];
      return opt;
    }

    // ---- funnel（漏斗）----
    if (t === "funnel") {
      var data0 = chartRowsFromResult(result, cfg);
      opt.series = [{ type: "funnel", left: "10%", top: 30, bottom: 10, width: "80%",
                      label: { color: "#bcbec4" },
                      data: data0.map(function (d) { return { name: String(d[0]), value: d[1] }; }) }];
      return opt;
    }

    // ---- boxplot：期待 percentile 节点输出（含 p25/p50/p75，可选 p05/p95）----
    if (t === "boxplot") {
      var cols = result.columns || [];
      // 找 p05/p25/p50/p75/p95 索引；缺 p05/p95 用 p25/p75 代替
      var idx = {};
      ["p05", "p25", "p50", "p75", "p95"].forEach(function (k) { idx[k] = cols.indexOf(k); });
      if (idx.p25 < 0 || idx.p50 < 0 || idx.p75 < 0)
        return _emptyOpt(opt, "boxplot 需要上游有 p25 / p50 / p75 列（用「分位数」节点）");
      var catIdx = cols.indexOf(cfg.x);
      var rows = result.rows.map(function (r, i) {
        var cat = catIdx >= 0 ? String(r[catIdx]) : "组 " + (i + 1);
        var p05 = idx.p05 >= 0 ? +r[idx.p05] : +r[idx.p25];
        var p95 = idx.p95 >= 0 ? +r[idx.p95] : +r[idx.p75];
        return { cat: cat, box: [p05, +r[idx.p25], +r[idx.p50], +r[idx.p75], p95] };
      });
      opt.xAxis = Object.assign({ type: "category", data: rows.map(function (r) { return r.cat; }) }, axis);
      opt.yAxis = Object.assign({ type: "value" }, axis);
      opt.series = [{ type: "boxplot", data: rows.map(function (r) { return r.box; }) }];
      return opt;
    }

    // ---- 老的 bar/line/pie/scatter/area ----
    var data = chartRowsFromResult(result, cfg);
    if (t === "pie") {
      opt.series = [{ type: "pie", radius: ["28%", "66%"], label: { color: "#9aa0a8" },
                      data: data.map(function (d) { return { name: String(d[0]), value: d[1] }; }) }];
    } else if (t === "scatter") {
      opt.xAxis = Object.assign({ type: "value", name: cfg.x }, axis);
      opt.yAxis = Object.assign({ type: "value", name: cfg.y }, axis);
      opt.series = [{ type: "scatter", symbolSize: 9,
                      data: data.map(function (d) {
                        var x = typeof d[0] === "number" ? d[0] : +d[0];
                        return [isNaN(x) ? 0 : x, d[1]];
                      }) }];
    } else {
      opt.xAxis = Object.assign({ type: "category",
                                  data: data.map(function (d) { return String(d[0]); }) }, axis);
      opt.yAxis = Object.assign({ type: "value" }, axis);
      var s = { data: data.map(function (d) { return d[1]; }), barMaxWidth: 42 };
      if (t === "area") { s.type = "line"; s.smooth = true; s.areaStyle = {}; }
      else if (t === "line") { s.type = "line"; s.smooth = true; }
      else { s.type = "bar"; }
      opt.series = [s];
    }
    return opt;
  }
  function _emptyOpt(baseOpt, msg) {
    baseOpt.series = [];
    baseOpt.graphic = { type: "text", left: "center", top: "middle",
                        style: { text: msg, fill: "#8a8f96", font: "12px sans-serif" } };
    return baseOpt;
  }
  function defaultChartCfg(result) {
    var cols = (result && result.columns) || [];
    var y = "";
    if (result && result.rows && result.rows.length) {
      for (var i = 0; i < cols.length; i++) {
        if (typeof result.rows[0][i] === "number" && i !== 0) { y = cols[i]; break; }
      }
    }
    return { type: "bar", x: cols[0] || "", y: y || cols[1] || cols[0] || "", agg: "" };
  }

  // 内嵌图表组件：受控接收 result + cfg，渲染 echarts 到自己的 div
  var WfChart = {
    name: "wf-chart",
    props: ["result", "cfg"],
    data: function () { return { _inst: null }; },
    watch: {
      result: function () { this.rerender(); },
      cfg: { deep: true, handler: function () { this.rerender(); } }
    },
    methods: {
      rerender: function () {
        var self = this;
        this.$nextTick(function () {
          if (!self.result || !self.result.columns || !window.echarts) return;
          var el = self.$refs.chartEl;
          if (!el) return;
          if (!self._inst) self._inst = window.echarts.init(el);
          self._inst.setOption(chartOptionFor(self.result, self.cfg), true);
          self._inst.resize();
        });
      }
    },
    mounted: function () {
      this.rerender();
      var self = this;
      this._resize = function () { if (self._inst) self._inst.resize(); };
      window.addEventListener("resize", this._resize);
    },
    unmounted: function () {
      if (this._resize) window.removeEventListener("resize", this._resize);
      if (this._inst) { this._inst.dispose(); this._inst = null; }
    },
    template: '<div class="wf-chart"><div class="wf-chart-el" ref="chartEl"></div></div>'
  };

  // ---------- 节点编辑抽屉组件 ----------
  // 双击画布节点打开：右滑 480-560px 面板；含节点表单 + 上游列（非源节点）+ 预览标签。
  // source 节点：默认 UI 拼装（选表 + 列多选 + WHERE builder + LIMIT），可切自由 SQL。
  var NodeEditor = {
    name: "wf-node-editor",
    components: { "wf-chart": WfChart },
    props: ["graph", "workspace", "nodeId"],
    emits: ["update:graph", "close", "delete-node"],
    data: function () {
      return {
        tab: "form",                // form | preview
        realConnOptions: [],
        realConnMeta: {},           // conn value -> {engine, database, environment}
        upstream: { loading: false, error: "", columns: [] },
        preview: { loading: false, error: "", columns: [], rows: [] },
        // source SQL builder 状态
        sourceMode: "builder",       // builder | sql
        srcDbs: [],                  // 未绑库连接需选库
        srcDbsLoading: false,
        srcDbsErr: "",
        srcTables: [],
        srcTablesLoading: false,
        srcTablesErr: "",
        srcSchemaCols: [],
        srcSchemaLoading: false,
        srcTable: "",
        srcCols: [],                 // 选中的列（空=全部）
        srcWhere: []                 // [{col, op, val}]
      };
    },
    computed: {
      node: function () {
        var self = this;
        return (this.graph.nodes || []).find(function (n) { return n.id === self.nodeId; }) || null;
      },
      joinKindOptions: function () {
        return [{ value: "INNER", label: "INNER" }, { value: "LEFT", label: "LEFT" },
                { value: "RIGHT", label: "RIGHT" }, { value: "FULL", label: "FULL" }];
      },
      maxPorts: function () { return JOIN_MAX_PORTS; },
      // 以下几个 *Options 都是喂给 dg-select 的。节点配置面板里原来用的是原生 <select>，
      // 深色 IDE 里弹出列表是系统外观、和其它页对不上（查询台/Redis 早就统一用 dg-select）。
      whereColOptions: function () {
        return [{ value: "", label: "列…" }].concat(
          this.srcSchemaCols.map(function (c) { return { value: c.name, label: c.name }; }));
      },
      whereOpOptions: function () {
        return ["=", "!=", ">", ">=", "<", "<=", "LIKE", "NOT LIKE",
                "IN", "NOT IN", "IS NULL", "IS NOT NULL"].map(function (o) {
          return { value: o, label: o };
        });
      },
      chartTypeOptions: function () {
        return [
          { value: "bar", label: "柱状图", group: "基础" },
          { value: "line", label: "折线图", group: "基础" },
          { value: "area", label: "面积图", group: "基础" },
          { value: "pie", label: "饼图", group: "基础" },
          { value: "scatter", label: "散点图", group: "基础" },
          { value: "stacked", label: "堆叠柱状", group: "多列" },
          { value: "grouped", label: "分组柱状", group: "多列" },
          { value: "multi_line", label: "多折线", group: "多列" },
          { value: "multi_area", label: "堆叠面积", group: "多列" },
          { value: "histogram", label: "直方图（单列分布）", group: "统计" },
          { value: "heatmap", label: "热力图（相关矩阵）", group: "统计" },
          { value: "boxplot", label: "箱线图（分位数）", group: "统计" },
          { value: "funnel", label: "漏斗图", group: "业务" }
        ];
      },
      chartXOptions: function () {
        var cols = this.previewCols.map(function (c) { return { value: c, label: c }; });
        if (!cols.length) return [{ value: "", label: "（先「预览」运行拿到列）" }];
        return [{ value: "", label: "（不选）" }].concat(cols);
      },
      chartYOptions: function () {
        var cols = this.previewCols.map(function (c) { return { value: c, label: c }; });
        return cols.length ? cols : [{ value: "", label: "（先「预览」运行拿到列）" }];
      },
      chartAggOptions: function () {
        return [{ value: "", label: "不聚合" }, { value: "sum", label: "SUM" },
                { value: "avg", label: "AVG" }, { value: "count", label: "COUNT" },
                { value: "min", label: "MIN" }, { value: "max", label: "MAX" }];
      },
      upstreamNames: function () {
        return upstreamNamesOf(this.graph, this.nodeId);
      },
      // output 节点图表 X/Y 下拉的列源：优先取 preview 已跑过的列；退回上游列（若已加载）
      previewCols: function () {
        if (this.preview.columns && this.preview.columns.length) return this.preview.columns;
        return (this.upstream.columns || []).map(function (c) { return c.name; });
      },
      // 当前 source 连接是否需要先选库（MySQL/PG/CH 未绑默认库时反射会崩，须显式选库）
      srcNeedsDb: function () {
        var n = this.node; if (!n || n.type !== "source" || !n.cfg.conn) return false;
        var m = this.realConnMeta[n.cfg.conn]; if (!m) return false;
        if (!(m.engine === "mysql" || m.engine === "postgres" || m.engine === "clickhouse")) return false;
        return !m.database;
      }
    },
    watch: {
      nodeId: function (newId, oldId) {
        if (newId && newId !== oldId) {
          this.tab = "form";
          this.upstream = { loading: false, error: "", columns: [] };
          this.preview = { loading: false, error: "", columns: [], rows: [] };
          this.$nextTick(this.initFromNode);
        }
      }
    },
    methods: {
      typeLabel: function (t) {
        return { source: "取数", file: "文件", filter: "过滤", join: "JOIN",
                 aggregate: "聚合", sql: "SQL", output: "输出",
                 describe: "描述", distinct: "去重", percentile: "分位",
                 correlate: "相关", pivot: "透视" }[t] || t;
      },
      persist: function () { this.$emit("update:graph", this.graph); },
      joinPortsCount: function (n) { return joinPortsN(n); },
      addJoinPort: function () {
        var n = this.node; if (!n || n.type !== "join") return;
        var cur = joinPortsN(n); if (cur >= JOIN_MAX_PORTS) return;
        n.cfg.ports_n = cur + 1; this.persist();
      },
      delJoinPort: function () {
        var n = this.node; if (!n || n.type !== "join") return;
        var cur = joinPortsN(n); if (cur <= 2) return;
        n.cfg.ports_n = cur - 1;
        var removed = "in_" + cur;
        this.graph.edges = (this.graph.edges || []).filter(function (e) {
          return !(e.to === n.id && normalizePort(n, e.port) === removed);
        });
        this.persist();
      },
      loadUpstream: function (refresh) {
        var self = this;
        var n = this.node;
        if (!n || n.type === "source" || n.type === "file") return;
        self.upstream = { loading: true, error: "", columns: [] };
        apiPost(API + "/preview_columns", {
          workspace: self.workspace, graph: JSON.stringify(self.graph),
          node: self.nodeId, refresh: refresh ? "1" : "0"
        }).then(function (d) {
          if (!d || !d.ok) {
            self.upstream = { loading: false, error: (d && d.error) || "取列失败", columns: [] };
            return;
          }
          self.upstream = { loading: false, error: d.error || "", columns: d.columns || [] };
        });
      },
      runPreview: function () {
        var self = this;
        self.tab = "preview";
        self.preview = { loading: true, error: "", columns: [], rows: [] };
        apiPost(API + "/preview_node", {
          workspace: self.workspace, graph: JSON.stringify(self.graph),
          node: self.nodeId, limit: 100
        }).then(function (d) {
          if (!d || !d.ok) {
            self.preview = { loading: false, error: (d && d.error) || "预览失败",
                             columns: [], rows: [] };
            return;
          }
          self.preview = { loading: false, error: d.error || "",
                           columns: d.columns || [], rows: d.rows || [] };
        });
      },
      // ---------- source SQL builder ----------
      initFromNode: function () {
        var n = this.node;
        if (!n) return;
        if (n.type === "source") {
          this.parseSourceSql();
          this.loadRealConns();
          // loadRealConns 拿到 meta 后会按 srcNeedsDb 自动 loadSrcDbs（若需要且未选库）。
          // 已有 schema 或不需要库时先尝试列表（meta 未到 srcNeedsDb=false 也 OK：有 schema 就带上）
          if ((n.cfg.conn || "").indexOf("/") >= 0) {
            this._tryLoadTablesWhenReady();
            // 已保存的 source 节点：parseSourceSql 抽出了 srcTable，主动拉表结构填「列」区
            if (this.srcTable) this.loadSrcTableCols(this.srcTable);
          }
        } else {
          this.loadUpstream(false);
        }
      },
      _tryLoadTablesWhenReady: function () {
        // meta 就绪前谨慎：若 meta 未就绪且节点有保存的 schema，直接带 schema 拉；
        // meta 未就绪且无 schema，等 loadRealConns 回调再决定
        var n = this.node; if (!n) return;
        if (this.realConnOptions.length === 0) {
          // meta 未就绪；loadRealConns 完成后自会补拉
          if (n.cfg.schema) this.loadSrcTables();
          return;
        }
        if (this.srcNeedsDb) {
          this.loadSrcDbs();
          if (n.cfg.schema) this.loadSrcTables();
        } else {
          this.loadSrcTables();
        }
      },
      loadRealConns: function () {
        var self = this;
        apiGet("/admin/sql/connections").then(function (d) {
          if (!d || !d.ok) return;
          var meta = {};
          self.realConnOptions = (d.connections || []).filter(function (c) {
            return c.project !== "analysis";
          }).map(function (c) {
            var v = c.project + "/" + c.connection;
            meta[v] = { engine: c.engine, database: c.database, environment: c.environment };
            return { value: v, label: v, env: c.environment };
          });
          self.realConnMeta = meta;
          // meta 就绪：按当前节点状态决定后续动作
          if (self.node && self.node.type === "source" && self.node.cfg.conn) {
            if (self.srcNeedsDb) {
              self.loadSrcDbs();
              if (self.node.cfg.schema) self.loadSrcTables();
            } else {
              self.loadSrcTables();
            }
          }
        });
      },
      onSourceConnChange: function (v) {
        this.node.cfg.conn = v;
        this.node.cfg.schema = "";                  // 换连接就清 schema
        this.srcTable = ""; this.srcCols = []; this.srcSchemaCols = [];
        this.srcDbs = []; this.srcDbsErr = ""; this.srcTables = []; this.srcTablesErr = "";
        this.persist();
        if (this.srcNeedsDb) this.loadSrcDbs();
        else this.loadSrcTables();
      },
      loadSrcDbs: function () {
        var self = this;
        var conn = self.node && self.node.cfg.conn;
        if (!conn) return;
        self.srcDbsLoading = true; self.srcDbsErr = "";
        apiGet("/admin/sql/databases?conn=" + encodeURIComponent(conn)).then(function (d) {
          self.srcDbsLoading = false;
          if (d && d.ok) self.srcDbs = d.databases || [];
          else self.srcDbsErr = (d && d.error) || "无法列出库";
        }).catch(function (e) { self.srcDbsLoading = false; self.srcDbsErr = String(e); });
      },
      onSrcDbChange: function (v) {
        if (!this.node) return;
        this.node.cfg.schema = v || "";
        this.srcTable = ""; this.srcCols = []; this.srcSchemaCols = [];
        this.persist();
        this.loadSrcTables();
      },
      loadSrcTables: function () {
        var self = this;
        var n = self.node; if (!n) return;
        var conn = n.cfg.conn;
        if (!conn) return;
        // 未绑库连接必须带 schema
        if (self.srcNeedsDb && !n.cfg.schema) { self.srcTables = []; return; }
        self.srcTablesLoading = true; self.srcTablesErr = "";
        var url = "/admin/sql/tables?conn=" + encodeURIComponent(conn);
        if (n.cfg.schema) url += "&schema=" + encodeURIComponent(n.cfg.schema);
        apiGet(url).then(function (d) {
          self.srcTablesLoading = false;
          if (d && d.ok) { self.srcTables = d.tables || []; }
          else { self.srcTables = []; self.srcTablesErr = (d && d.error) || "无法列出表"; }
        }).catch(function (e) {
          self.srcTablesLoading = false; self.srcTables = []; self.srcTablesErr = String(e);
        });
      },
      onSrcTableChange: function (v) {
        this.srcTable = v;
        this.srcCols = [];
        this.loadSrcTableCols(v);
        this.rebuildSourceSql();
      },
      loadSrcTableCols: function (tbl) {
        if (!tbl || !this.node || !this.node.cfg.conn) return;
        var self = this;
        self.srcSchemaLoading = true;
        var url = "/admin/sql/table?conn=" + encodeURIComponent(this.node.cfg.conn)
                + "&table=" + encodeURIComponent(tbl);
        if (this.node.cfg.schema) url += "&schema=" + encodeURIComponent(this.node.cfg.schema);
        apiGet(url).then(function (d) {
          self.srcSchemaLoading = false;
          if (!d || !d.ok) { self.srcSchemaCols = []; return; }
          self.srcSchemaCols = (d.columns || []).map(function (c) {
            return { name: c.name, type: c.type || "" };
          });
        }).catch(function () { self.srcSchemaLoading = false; });
      },
      toggleSrcCol: function (col) {
        var i = this.srcCols.indexOf(col);
        if (i >= 0) this.srcCols.splice(i, 1);
        else this.srcCols.push(col);
        this.rebuildSourceSql();
      },
      srcAllCols: function () {
        this.srcCols = [];
        this.rebuildSourceSql();
      },
      addWhereRow: function () {
        this.srcWhere.push({ col: "", op: "=", val: "" });
      },
      delWhereRow: function (i) {
        this.srcWhere.splice(i, 1);
        this.rebuildSourceSql();
      },
      onWhereChange: function () { this.rebuildSourceSql(); },
      rebuildSourceSql: function () {
        var n = this.node;
        if (!n || n.type !== "source" || !this.srcTable) return;
        var q = this._quoteIdent(n.cfg.conn);
        var cols = this.srcCols.length ? this.srcCols.map(q).join(", ") : "*";
        var sql = "SELECT " + cols + " FROM " + q(this.srcTable);
        var wheres = this.srcWhere.filter(function (w) { return w.col && w.op; })
          .map(function (w) {
            var lhs = q(w.col);
            var op = w.op.toUpperCase();
            if (op === "IS NULL" || op === "IS NOT NULL") return lhs + " " + op;
            var v = String(w.val || "").trim();
            if (v === "") return "";
            var isNum = /^-?\d+(\.\d+)?$/.test(v);
            if (op === "IN" || op === "NOT IN") return lhs + " " + op + " (" + v + ")";
            var rhs = isNum ? v : "'" + v.replace(/'/g, "''") + "'";
            return lhs + " " + op + " " + rhs;
          }).filter(function (s) { return s; });
        if (wheres.length) sql += " WHERE " + wheres.join(" AND ");
        n.cfg.sql = sql;
        this.persist();
      },
      _quoteIdent: function (connKey) {
        return function (name) {
          if (!name) return name;
          if (connKey && connKey.indexOf("mysql") >= 0) return "`" + name.replace(/`/g, "``") + "`";
          return '"' + name.replace(/"/g, '""') + '"';
        };
      },
      parseSourceSql: function () {
        var sql = ((this.node && this.node.cfg.sql) || "").trim();
        this.srcWhere = [];
        this.srcCols = [];
        this.srcTable = "";
        if (!sql) return;
        var m = /^SELECT\s+([\s\S]+?)\s+FROM\s+([`"]?)([\w$.]+)\2(?:\s+WHERE\s+([\s\S]+?))?(?:\s+LIMIT\s+\d+)?\s*;?$/i.exec(sql);
        if (!m) { this.sourceMode = "sql"; return; }
        var colsRaw = m[1].trim(), table = m[3].trim(), whereRaw = (m[4] || "").trim();
        this.srcTable = table;
        if (colsRaw !== "*") {
          this.srcCols = colsRaw.split(",").map(function (s) {
            return s.trim().replace(/^["`]|["`]$/g, "");
          }).filter(Boolean);
        }
        if (whereRaw) {
          if (/\bOR\b/i.test(whereRaw) || whereRaw.indexOf("(") >= 0) {
            this.sourceMode = "sql";
            return;
          }
          this.srcWhere = whereRaw.split(/\s+AND\s+/i).map(function (part) {
            var mm = /^([`"]?)([\w$.]+)\1\s*(=|!=|<>|>=|<=|>|<|LIKE|NOT LIKE|IN|NOT IN|IS NULL|IS NOT NULL)\s*(.*)$/i.exec(part.trim());
            if (!mm) return null;
            var val = (mm[4] || "").trim();
            if (val.startsWith("'") && val.endsWith("'")) val = val.slice(1, -1);
            if (val.startsWith("(") && val.endsWith(")")) val = val.slice(1, -1);
            return { col: mm[2], op: mm[3].toUpperCase(), val: val };
          }).filter(Boolean);
        }
      },
      setOutputView: function (v) {
        var n = this.node;
        if (!n || n.type !== "output") return;
        n.cfg.view = v;
        // 切到 chart 时懒初始化 cfg.chart（若无）
        if (v === "chart" && !n.cfg.chart) {
          n.cfg.chart = { type: "bar", x: this.previewCols[0] || "",
                          y: this.previewCols[1] || this.previewCols[0] || "", agg: "" };
        }
        this.persist();
      }
    },
    mounted: function () {
      this.initFromNode();
      var self = this;
      this._escHandler = function (e) { if (e.key === "Escape") self.$emit("close"); };
      document.addEventListener("keydown", this._escHandler);
    },
    unmounted: function () {
      if (this._escHandler) document.removeEventListener("keydown", this._escHandler);
    },
    template:
      // 半透明遮罩 + 抽屉本体：点遮罩空白 = 关闭；点抽屉里不冒泡上来
      '<div class="wf-drawer-overlay" v-if="node" @click.self="$emit(\'close\')">'
      + '<div class="wf-drawer" @click.stop>'
      + '<div class="wf-drawer-bd">'
      +   '<div class="wf-drawer-hd">'
      +     '<span class="wf-drawer-ty">{{ typeLabel(node.type) }}</span>'
      +     '<input class="wf-drawer-name" v-model="node.name" @change="persist" spellcheck="false">'
      +     '<div class="wf-drawer-tabs">'
      +       '<button :class="{active: tab===\'form\'}" @click="tab=\'form\'">配置</button>'
      +       '<button :class="{active: tab===\'preview\'}" @click="runPreview">预览</button>'
      +     '</div>'
      +     '<button class="wf-drawer-icon wf-drawer-del" @click="$emit(\'delete-node\', node.id)" title="删除节点" aria-label="删除节点">'
      +       '<svg viewBox="0 0 16 16" width="14" height="14" fill="none" stroke="currentColor" stroke-width="1.5" stroke-linecap="round">'
      +         '<path d="M3 4h10M6.5 4V2.5h3V4M5 4l.5 9a1 1 0 0 0 1 1h3a1 1 0 0 0 1-1L11 4M7 7v4M9 7v4"/>'
      +       '</svg>'
      +     '</button>'
      +     '<button class="wf-drawer-icon wf-drawer-close" @click="$emit(\'close\')" title="关闭（ESC）" aria-label="关闭">'
      +       '<svg viewBox="0 0 16 16" width="14" height="14" fill="none" stroke="currentColor" stroke-width="1.6" stroke-linecap="round">'
      +         '<path d="M4 4l8 8M12 4l-8 8"/>'
      +       '</svg>'
      +     '</button>'
      +   '</div>'
      +   '<div class="wf-drawer-body">'
      // ============ 配置 tab ============
      +     '<div v-if="tab===\'form\'" class="wf-drawer-form">'
      // 上游列（非源节点）
      +       '<div v-if="node.type !== \'source\' && node.type !== \'file\'" class="wf-mod-schema">'
      +         '<div class="wf-mod-schema-hd">'
      +           '<span>上游列</span>'
      +           '<button class="dg-btn sm" @click="loadUpstream(true)" title="强制重跑上游依赖，重新读列">↻ 刷新</button>'
      +         '</div>'
      +         '<div v-if="upstream.loading" class="wf-mod-schema-msg">读取中…</div>'
      +         '<div v-else-if="upstream.error" class="wf-mod-schema-err">{{ upstream.error }}</div>'
      +         '<div v-else-if="upstream.columns.length" class="wf-mod-cols">'
      +           '<span v-for="c in upstream.columns" :key="c.name" class="wf-mod-col"'
      +             ' :title="c.type"><b>{{ c.name }}</b><i>{{ c.type }}</i></span>'
      +         '</div>'
      +         '<div v-else class="wf-mod-schema-msg">（暂无上游列。点「↻ 刷新」触发编译）</div>'
      +       '</div>'
      // ---- source 节点 ----
      +       '<template v-if="node.type===\'source\'">'
      +         '<div class="row"><label>连接</label>'
      +           '<dg-select :model-value="node.cfg.conn" :options="realConnOptions" placeholder="选择连接…"'
      +             ' @update:model-value="onSourceConnChange"/></div>'
      +         '<div class="wf-mode-tabs">'
      +           '<button :class="{active: sourceMode===\'builder\'}" @click="sourceMode=\'builder\'">UI 拼装</button>'
      +           '<button :class="{active: sourceMode===\'sql\'}" @click="sourceMode=\'sql\'">SQL 高级</button>'
      +         '</div>'
      +         '<template v-if="sourceMode===\'builder\'">'
      +           '<div v-if="srcNeedsDb" class="row">'
      +             '<label>库 <span v-if="srcDbsLoading" class="wf-hint">加载中…</span></label>'
      +             '<dg-select :model-value="node.cfg.schema || \'\'" '
      +                ':options="srcDbs.map(x => ({value: x, label: x}))"'
      +                ' :placeholder="srcDbsLoading ? \'加载库…\' : \'选择库…（此连接未绑定默认库）\'"'
      +                ' @update:model-value="onSrcDbChange"/>'
      +             '<div v-if="srcDbsErr" class="wf-mod-schema-err">{{ srcDbsErr }}</div>'
      +           '</div>'
      +           '<div class="row"><label>表</label>'
      +             '<dg-select :model-value="srcTable" '
      +                ':options="srcTables.map(t => ({value: t, label: t}))"'
      +                ' :placeholder="srcTablesLoading ? \'加载表…\' : (srcNeedsDb && !node.cfg.schema ? \'先选库\' : (node.cfg.conn ? \'选择表…\' : \'先选连接\'))"'
      +                ' @update:model-value="onSrcTableChange"/>'
      +             '<div v-if="srcTablesErr" class="wf-mod-schema-err">{{ srcTablesErr }}</div>'
      +           '</div>'
      +           '<div v-if="srcTable" class="row">'
      +             '<label>列 <a href="#" @click.prevent="srcAllCols" class="wf-hint-link">选全部</a></label>'
      +             '<div v-if="srcSchemaLoading" class="wf-mod-schema-msg">读列…</div>'
      +             '<div v-else class="wf-col-picker">'
      +               '<label v-for="c in srcSchemaCols" :key="c.name" class="wf-col-check">'
      +                 '<input type="checkbox" :checked="srcCols.indexOf(c.name)>=0"'
      +                    ' @change="toggleSrcCol(c.name)">'
      +                 '<span>{{ c.name }}</span><i>{{ c.type }}</i>'
      +               '</label>'
      +               '<span v-if="!srcSchemaCols.length" class="wf-mod-schema-msg">（无列信息或加载中）</span>'
      +             '</div>'
      +           '</div>'
      +           '<div v-if="srcTable" class="row">'
      +             '<label>WHERE 条件 <a href="#" @click.prevent="addWhereRow" class="wf-hint-link">＋ 加条件</a></label>'
      +             '<div v-for="(w, i) in srcWhere" :key="i" class="wf-where-row">'
      +               '<dg-select class="wf-where-col" :model-value="w.col" :options="whereColOptions"'
      +                 ' placeholder="列…" @update:model-value="v => { w.col = v; onWhereChange() }"/>'
      +               '<dg-select class="wf-where-op" :model-value="w.op" :options="whereOpOptions"'
      +                 ' @update:model-value="v => { w.op = v; onWhereChange() }"/>'
      +               '<input v-model="w.val" @change="onWhereChange" class="wf-where-val" placeholder="值">'
      +               '<button class="dg-btn sm" @click="delWhereRow(i)">−</button>'
      +             '</div>'
      +             '<div v-if="!srcWhere.length" class="wf-mod-schema-msg">（无过滤条件）</div>'
      +           '</div>'
      +           '<div class="row"><label>行数上限</label>'
      +             '<input type="number" v-model.number="node.cfg.limit" @change="persist" placeholder="默认 20 万"></div>'
      +           '<div v-if="!srcNeedsDb" class="row"><label>schema（未绑库连接需填）</label>'
      +             '<input v-model="node.cfg.schema" @change="persist" placeholder="留空使用默认库"></div>'
      +           '<div class="wf-sql-preview">'
      +             '<div class="wf-sql-preview-hd">生成的 SQL <span class="wf-hint">（切「SQL 高级」可直接修改）</span></div>'
      +             '<pre>{{ node.cfg.sql || "(尚未选表)" }}</pre>'
      +           '</div>'
      +         '</template>'
      +         '<template v-else>'
      +           '<div class="row"><label>取数 SQL（自由手写）</label>'
      +             '<textarea v-model="node.cfg.sql" @change="persist" rows="8" spellcheck="false"'
      +                ' placeholder="SELECT * FROM t WHERE ..."></textarea></div>'
      +           '<div class="row"><label>行数上限</label>'
      +             '<input type="number" v-model.number="node.cfg.limit" @change="persist" placeholder="默认 20 万"></div>'
      +           '<div class="row"><label>schema</label>'
      +             '<input v-model="node.cfg.schema" @change="persist" placeholder="未绑库连接需指定"></div>'
      +         '</template>'
      +       '</template>'
      // ---- file 节点 ----
      +       '<template v-else-if="node.type===\'file\'">'
      +         '<div class="row"><label>文件路径</label>'
      +           '<input v-model="node.cfg.path" @change="persist" '
      +             'placeholder="/path/data.csv（csv/parquet/json）"></div>'
      +       '</template>'
      // ---- filter 节点 ----
      +       '<template v-else-if="node.type===\'filter\'">'
      +         '<div class="row"><label>WHERE 表达式</label>'
      +           '<textarea v-model="node.cfg.where" @change="persist" rows="4" spellcheck="false"'
      +             ' placeholder="status = \'paid\' AND amount > 100"></textarea></div>'
      +       '</template>'
      // ---- join 节点 ----
      +       '<template v-else-if="node.type===\'join\'">'
      +         '<div class="row"><label>类型</label>'
      +           '<dg-select :model-value="node.cfg.kind || \'INNER\'" :options="joinKindOptions"'
      +             ' @update:model-value="v => { node.cfg.kind = v; persist(); }"/></div>'
      +         '<div class="row"><label>输入端口数 <span class="wf-hint">{{ joinPortsCount(node) }}/{{ maxPorts }}</span></label>'
      +           '<div class="wf-btn-grp">'
      +             '<button class="dg-btn sm" @click="delJoinPort" :disabled="joinPortsCount(node)<=2">−</button>'
      +             '<span class="wf-port-cnt">{{ joinPortsCount(node) }}</span>'
      +             '<button class="dg-btn sm" @click="addJoinPort" :disabled="joinPortsCount(node)>=maxPorts">＋</button>'
      +           '</div>'
      +         '</div>'
      +         '<div class="row"><label>ON</label>'
      +           '<input v-model="node.cfg.on" @change="persist" spellcheck="false"'
      +             ' :placeholder="joinPortsCount(node)===2 ? \'a.uid = b.id\' : \'a.x=b.x AND b.y=c.y ...\'"></div>'
      +         '<div class="row"><label>SELECT</label>'
      +           '<input v-model="node.cfg.select" @change="persist" spellcheck="false"'
      +             ' placeholder="留空即 a.*, b.*, c.*..."></div>'
      +       '</template>'
      // ---- aggregate 节点 ----
      +       '<template v-else-if="node.type===\'aggregate\'">'
      +         '<div class="row"><label>GROUP BY</label>'
      +           '<input v-model="node.cfg.group" @change="persist" spellcheck="false"'
      +             ' placeholder="留空 = 全局聚合"></div>'
      +         '<div class="row"><label>聚合表达式</label>'
      +           '<textarea v-model="node.cfg.aggs" @change="persist" rows="3" spellcheck="false"'
      +             ' placeholder="count(*) AS n, sum(amount) AS total"></textarea></div>'
      +       '</template>'
      // ---- describe 节点 ----
      +       '<template v-else-if="node.type===\'describe\'">'
      +         '<div class="row"><label>数值列（必填，逗号分隔）</label>'
      +           '<input v-model="node.cfg.cols" @change="persist" spellcheck="false"'
      +             ' placeholder="amount, revenue"></div>'
      +         '<div class="wf-hint" style="margin-top:6px">'
      +           '输出每列一行：<code>column_name / n / nulls / min / max / avg / std / q25 / q50 / q75</code>'
      +         '</div>'
      +       '</template>'
      // ---- distinct 节点 ----
      +       '<template v-else-if="node.type===\'distinct\'">'
      +         '<div class="row"><label>去重列（可选，留空 = SELECT DISTINCT *）</label>'
      +           '<input v-model="node.cfg.cols" @change="persist" spellcheck="false"'
      +             ' placeholder="user_id, day"></div>'
      +       '</template>'
      // ---- percentile 节点 ----
      +       '<template v-else-if="node.type===\'percentile\'">'
      +         '<div class="row"><label>目标数值列</label>'
      +           '<input v-model="node.cfg.col" @change="persist" spellcheck="false"'
      +             ' placeholder="amount"></div>'
      +         '<div class="row"><label>分位数（0-1 间，逗号分隔）</label>'
      +           '<input v-model="node.cfg.quantiles" @change="persist" spellcheck="false"'
      +             ' placeholder="0.25, 0.5, 0.75, 0.95"></div>'
      +         '<div class="row"><label>分组列（可选）</label>'
      +           '<input v-model="node.cfg.group" @change="persist" spellcheck="false"'
      +             ' placeholder="channel"></div>'
      +       '</template>'
      // ---- correlate 节点 ----
      +       '<template v-else-if="node.type===\'correlate\'">'
      +         '<div class="row"><label>相关列（至少 2 列，逗号分隔）</label>'
      +           '<input v-model="node.cfg.cols" @change="persist" spellcheck="false"'
      +             ' placeholder="amount, revenue, cost"></div>'
      +         '<div class="wf-hint" style="margin-top:6px">'
      +           'Pearson 相关系数，两两组合输出为 <code>a__b</code>、<code>a__c</code>、<code>b__c</code> 等列（值域 -1 ~ 1）'
      +         '</div>'
      +       '</template>'
      // ---- pivot 节点 ----
      +       '<template v-else-if="node.type===\'pivot\'">'
      +         '<div class="row"><label>透视列（ON）</label>'
      +           '<input v-model="node.cfg.on" @change="persist" spellcheck="false"'
      +             ' placeholder="channel"></div>'
      +         '<div class="row"><label>聚合表达式（USING）</label>'
      +           '<input v-model="node.cfg.using" @change="persist" spellcheck="false"'
      +             ' placeholder="sum(amount)"></div>'
      +         '<div class="row"><label>行分组（GROUP BY，可选）</label>'
      +           '<input v-model="node.cfg.group" @change="persist" spellcheck="false"'
      +             ' placeholder="day"></div>'
      +         '<div class="wf-hint" style="margin-top:6px">'
      +           '编译为 DuckDB 原生 <code>PIVOT</code>；透视列的每个不同值成为一列'
      +         '</div>'
      +       '</template>'
      // ---- sql 节点 ----
      +       '<template v-else-if="node.type===\'sql\'">'
      +         '<div class="row">'
      +           '<label>SQL <span v-if="upstreamNames.length" class="wf-hint">上游节点：{{ upstreamNames.join(", ") }}</span></label>'
      +           '<textarea v-model="node.cfg.sql" @change="persist" rows="8" spellcheck="false"'
      +             ' placeholder="SELECT ...（直接用上游节点名作表名）"></textarea>'
      +         '</div>'
      +       '</template>'
      // ---- output 节点 ----
      +       '<template v-else-if="node.type===\'output\'">'
      +         '<div class="row"><label>ORDER BY</label>'
      +           '<input v-model="node.cfg.order_by" @change="persist" spellcheck="false"'
      +             ' placeholder="total DESC"></div>'
      +         '<div class="row"><label>LIMIT</label>'
      +           '<input type="number" v-model.number="node.cfg.limit" @change="persist" placeholder="1000"></div>'
      // 可视化：默认 table，可切 chart（切到 chart 时 preview 标签渲染图表；预览时也能改）
      +         '<div class="row"><label>展示形式</label>'
      +           '<div class="wf-mode-tabs">'
      +             '<button :class="{active: (node.cfg.view||\'table\')===\'table\'}" @click="setOutputView(\'table\')">表格</button>'
      +             '<button :class="{active: node.cfg.view===\'chart\'}" @click="setOutputView(\'chart\')">图表</button>'
      +           '</div>'
      +         '</div>'
      +         '<template v-if="node.cfg.view===\'chart\'">'
      +           '<div class="row"><label>图表类型</label>'
      +             '<dg-select class="wf-chart-cfg-in" :model-value="node.cfg.chart.type"'
      +               ' :options="chartTypeOptions" @update:model-value="v => { node.cfg.chart.type = v; persist() }"/></div>'
      +           '<div class="row"><label>X 轴列<span class="wf-hint">（boxplot 用作分组、heatmap 忽略）</span></label>'
      +             '<dg-select class="wf-chart-cfg-in" :model-value="node.cfg.chart.x"'
      +               ' :options="chartXOptions" @update:model-value="v => { node.cfg.chart.x = v; persist() }"/></div>'
      +           '<div class="row" v-if="!/^(stacked|grouped|multi_)/.test(node.cfg.chart.type||\'\') && node.cfg.chart.type!==\'heatmap\' && node.cfg.chart.type!==\'boxplot\'"><label>Y 轴列</label>'
      +             '<dg-select class="wf-chart-cfg-in" :model-value="node.cfg.chart.y"'
      +               ' :options="chartYOptions" @update:model-value="v => { node.cfg.chart.y = v; persist() }"/></div>'
      +           '<div class="row" v-if="/^(stacked|grouped|multi_)/.test(node.cfg.chart.type||\'\')"><label>Y 列（逗号分隔多列）</label>'
      +             '<input v-model="node.cfg.chart.ys" @change="persist" spellcheck="false" placeholder="revenue, cost, roi"></div>'
      +           '<div class="row" v-if="node.cfg.chart.type===\'histogram\'"><label>分箱数</label>'
      +             '<input type="number" v-model.number="node.cfg.chart.bins" @change="persist" placeholder="留空自动（√N）"></div>'
      +           '<div class="row" v-if="!/^(heatmap|boxplot|histogram|multi_|stacked|grouped)$/.test(node.cfg.chart.type||\'\')"><label>聚合</label>'
      +             '<dg-select class="wf-chart-cfg-in" :model-value="node.cfg.chart.agg"'
      +               ' :options="chartAggOptions" @update:model-value="v => { node.cfg.chart.agg = v; persist() }"/></div>'
      +         '</template>'
      +       '</template>'
      +     '</div>'
      // ============ 预览 tab ============
      +     '<div v-else-if="tab===\'preview\'" class="wf-drawer-preview">'
      +       '<div class="wf-drawer-preview-hd">'
      +         '<span v-if="preview.loading">运行中…</span>'
      +         '<span v-else-if="preview.error" class="wf-err">{{ preview.error }}</span>'
      +         '<span v-else>{{ preview.rows.length }} 行 · {{ preview.columns.length }} 列</span>'
      +         '<button class="dg-btn sm" @click="runPreview">↻ 重新预览</button>'
      +       '</div>'
      // output 节点 view=chart 时预览标签渲染图表；其它情况仍是表格
      +       '<wf-chart v-if="!preview.loading && !preview.error && preview.columns.length'
      +         ' && node.type===\'output\' && node.cfg.view===\'chart\' && node.cfg.chart"'
      +         ' :result="preview" :cfg="node.cfg.chart"/>'
      +       '<div v-else-if="!preview.loading && !preview.error && preview.columns.length" class="wf-mod-preview-tbl">'
      +         '<table><thead><tr><th v-for="c in preview.columns" :key="c">{{ c }}</th></tr></thead>'
      +         '<tbody><tr v-for="(r, ri) in preview.rows.slice(0, 100)" :key="ri">'
      +           '<td v-for="(v, ci) in r" :key="ci">{{ v == null ? "" : String(v).slice(0, 200) }}</td>'
      +         '</tr></tbody></table>'
      +       '</div>'
      +     '</div>'
      +   '</div>'
      + '</div>'  // wf-drawer-bd 结束
      + '</div>'  // wf-drawer 结束
      + '</div>'  // wf-drawer-overlay 结束
  };

  // ---------- 主 App ----------
  var App = {
    components: { "wf-blueprint": Blueprint, "wf-node-editor": NodeEditor, "wf-chart": WfChart },
    data: function () {
      return {
        route: parseHash(),
        wfs: [],
        wsList: [],
        loading: false,
        err: "",
        newForm: { name: "", workspace: "", newWorkspace: "" },
        current: null,        // {name, workspace, graph, ...}
        viewMode: "blueprint",  // blueprint | history （模块视图已下线）
        // 蓝图侧交互
        selectedNodeId: null,   // 单击选中
        drawerNodeId: null,     // 双击打开抽屉的节点 id（null=关）
        nodeStatus: {},         // {nodeId: 'running'|'ok'|'err'} 运行时染色
        // 运行结果面板（底部）
        runOut: null,
        runBusy: false,
        runErr: "",
        runSteps: [],           // 运行步骤时间轴（从 kind=workflow 的 job 结果里来）
        runOutputTable: null,   // 输出预览表 {columns, rows}
        saveTimer: null,
        // 调度浮层
        schedOpen: false,
        schedForm: { cron_type: "interval", cron_value: "5", enabled: true,
                     notify_on: "failure", attach_kinds: ["summary"] },
        schedLoading: false,
        schedError: "",
        schedEditName: "",  // 编辑哪个 workflow 的调度（详情页=当前 workflow，列表页=从表行传入）
        // 定时任务管理页
        schedulesList: [],
        schedulesLoading: false,
        schedulesErr: "",
        triggerBusy: {},  // {name: true} 立即触发按钮 loading
        // 运行历史
        runsList: [],
        runsLoading: false,
        // 运行详情（view=run）
        currentRun: null,
        currentRunErr: "",
        // 运行中面板（列表页顶部，5s 轮询）
        runningList: [],
        runningTimer: null,
        // AI 生成流程面板
        aiEnabled: false,
        wfAi: null   // {question, conn, connOptions, tables, tablesLoading, picked, filter, running, error}
      };
    },
    computed: {
      // 调度对话框的两个下拉也走 dg-select（本页最后两处原生 <select>）
      cronTypeOptions: function () {
        return [{ value: "interval", label: "每 N 分钟" }, { value: "daily", label: "每天" },
                { value: "weekly", label: "每周" }, { value: "monthly", label: "每月" },
                { value: "cron", label: "Cron 表达式（高级）" }];
      },
      notifyOnOptions: function () {
        return [{ value: "failure", label: "仅失败时（推荐）" }, { value: "success", label: "仅成功时" },
                { value: "always", label: "每次都通知" }, { value: "none", label: "不通知" }];
      },
      workspaceOptions: function () {
        var opts = (this.wsList || []).map(function (w) {
          return { value: w.workspace, label: w.workspace
            + (w.datasets && w.datasets.length ? " (" + w.datasets.length + ")" : "") };
        });
        opts.push({ value: "__new__", label: "＋ 新建工作区…" });
        return opts;
      }
    },
    methods: {
      fmtTime: fmtTime,
      refresh: function () {
        var self = this;
        self.loading = true;
        apiGet(API + "/list").then(function (d) {
          self.wfs = d && d.ok ? (d.workflows || []) : [];
          self.err = d && d.ok ? "" : (d && d.error) || "加载失败";
        }).finally(function () { self.loading = false; });
      },
      refreshWorkspaces: function () {
        var self = this;
        apiGet(API + "/workspaces").then(function (d) {
          self.wsList = d && d.ok ? (d.workspaces || []) : [];
        });
      },
      refreshRunning: function () {
        var self = this;
        apiGet(API + "/running").then(function (d) {
          self.runningList = d && d.ok ? (d.runs || []) : [];
        }).catch(function () { /* 静默失败，下个 tick 再试 */ });
      },
      fmtElapsed: function (s) {
        if (s == null || s < 0) return "-";
        if (s < 60) return s + "s";
        var m = Math.floor(s / 60), r = s % 60;
        if (m < 60) return m + "m " + r + "s";
        var h = Math.floor(m / 60); m = m % 60;
        return h + "h " + m + "m";
      },
      openRunDetail: function (runId) {
        window.location.href = "/admin/workflows/runs/" + runId;
      },
      onHashChange: function () {
        this.route = parseHash();
        this.runOut = null; this.runErr = "";
        this.schedOpen = false;
        this.currentRun = null; this.currentRunErr = "";
        if (this.route.view === "detail") this.loadOne(this.route.name);
        if (this.route.view === "new") this.refreshWorkspaces();
        if (this.route.view === "run") this.loadRun(this.route.runId);
        if (this.route.view === "schedules") { this.refreshSchedules(); this.refreshRunning(); }
      },
      // ---------- 调度浮层 ----------
      openSchedule: function (nameOverride) {
        // 详情页调用不传参（Vue 会传 MouseEvent，忽略掉）→ 用 current；
        // schedules 页调用传字符串 name → 用它
        var name = (typeof nameOverride === "string" && nameOverride)
          || (this.current && this.current.name);
        if (!name) return;
        this.schedEditName = name;
        this.schedOpen = true;
        this.schedError = "";
        this.schedLoading = true;
        var self = this;
        apiGet(API + "/schedule?name=" + encodeURIComponent(name))
          .then(function (d) {
            self.schedLoading = false;
            if (d && d.ok && d.schedule) {
              self.schedForm = {
                cron_type: d.schedule.cron_type,
                cron_value: d.schedule.cron_value,
                enabled: d.schedule.enabled,
                notify_on: d.schedule.notify_on,
                attach_kinds: d.schedule.attach_kinds || ["summary"]
              };
            } else {
              // 默认：每 5 分钟，只失败通知，summary
              self.schedForm = { cron_type: "interval", cron_value: "5",
                                 enabled: true, notify_on: "failure",
                                 attach_kinds: ["summary"] };
            }
          });
      },
      saveSchedule: function () {
        var name = this.schedEditName || (this.current && this.current.name);
        if (!name) return;
        var self = this;
        self.schedError = "";
        apiPost(API + "/schedule", {
          name: name,
          cron_type: self.schedForm.cron_type,
          cron_value: self.schedForm.cron_value,
          enabled: self.schedForm.enabled ? "1" : "0",
          notify_on: self.schedForm.notify_on,
          attach_kinds: JSON.stringify(self.schedForm.attach_kinds || [])
        }).then(function (d) {
          if (!d.ok) { self.schedError = d.error || "保存失败"; return; }
          self.schedOpen = false;
          // 从 schedules 页发起的编辑，保存后刷新列表
          if (self.route.view === "schedules") self.refreshSchedules();
        });
      },
      deleteSchedule: function () {
        var name = this.schedEditName || (this.current && this.current.name);
        if (!name) return;
        if (!window.confirm("确认删除「" + name + "」的调度？")) return;
        var self = this;
        apiPost(API + "/schedule/delete", { name: name }).then(function () {
          self.schedOpen = false;
          if (self.route.view === "schedules") self.refreshSchedules();
        });
      },
      // ---------- 定时任务管理页（schedules） ----------
      refreshSchedules: function () {
        var self = this;
        self.schedulesLoading = true;
        apiGet(API + "/schedules").then(function (d) {
          self.schedulesLoading = false;
          if (d && d.ok) {
            self.schedulesList = d.schedules || [];
            self.schedulesErr = "";
          } else {
            self.schedulesList = [];
            self.schedulesErr = (d && d.error) || "加载失败";
          }
        });
      },
      toggleScheduleEnabled: function (s) {
        // 直接改 enabled 字段（保留 cron/notify_on/attach_kinds 原值）
        var self = this;
        apiPost(API + "/schedule", {
          name: s.name,
          cron_type: s.cron_type,
          cron_value: s.cron_value,
          enabled: s.enabled ? "0" : "1",  // 翻转
          notify_on: s.notify_on,
          attach_kinds: JSON.stringify(s.attach_kinds || [])
        }).then(function (d) {
          if (!d.ok) { alert(d.error || "切换失败"); return; }
          self.refreshSchedules();
        });
      },
      deleteScheduleRow: function (s) {
        if (!window.confirm("确认删除「" + s.name + "」的调度？（不删 workflow 本身）")) return;
        var self = this;
        apiPost(API + "/schedule/delete", { name: s.name }).then(function () {
          self.refreshSchedules();
        });
      },
      triggerScheduleNow: function (s) {
        if (s.running) { alert("上一次运行还没完成，跳过本次触发"); return; }
        if (!window.confirm("立即触发「" + s.name + "」一次？（会入运行历史 + 按 notify_on 发通知）")) return;
        var self = this;
        var busy = Object.assign({}, self.triggerBusy);
        busy[s.name] = true;
        self.triggerBusy = busy;
        apiPost(API + "/schedule/trigger", { name: s.name }).then(function (d) {
          var b = Object.assign({}, self.triggerBusy);
          delete b[s.name];
          self.triggerBusy = b;
          if (!d.ok) { alert(d.error || "触发失败"); return; }
          setTimeout(function () { self.refreshSchedules(); }, 800);
        });
      },
      fmtCron: function (s) {
        // cron_type / cron_value → 人类可读
        var t = s.cron_type, v = s.cron_value;
        if (t === "interval") return "每 " + v + " 分钟";
        if (t === "daily") return "每天 " + v;
        if (t === "weekly") {
          var parts = (v || "").split(" ");
          var wd = ["周日", "周一", "周二", "周三", "周四", "周五", "周六"];
          return "每" + (wd[parseInt(parts[0], 10)] || "?") + " " + (parts[1] || "");
        }
        if (t === "monthly") {
          var pp = (v || "").split(" ");
          return "每月 " + pp[0] + " 日 " + (pp[1] || "");
        }
        return "cron " + v;
      },
      goToSchedulesTab: function () { location.hash = "schedules"; },
      goToListTab: function () { location.hash = ""; },
      toggleAttach: function (kind) {
        var kinds = this.schedForm.attach_kinds || [];
        var idx = kinds.indexOf(kind);
        if (idx >= 0) kinds.splice(idx, 1);
        else kinds.push(kind);
        this.schedForm.attach_kinds = kinds.slice();  // 触发 reactivity
      },
      // ---------- 运行历史 ----------
      loadRuns: function () {
        if (!this.current) return;
        var self = this;
        self.runsLoading = true;
        apiGet(API + "/runs?name=" + encodeURIComponent(this.current.name) + "&limit=50")
          .then(function (d) {
            self.runsLoading = false;
            self.runsList = d && d.ok ? (d.runs || []) : [];
          });
      },
      openRun: function (runId) {
        // 跳到服务端渲染的详情页（新页 shell 会读 pathname）
        location.href = "/admin/workflows/runs/" + runId;
      },
      loadRun: function (runId) {
        var self = this;
        apiGet(API + "/runs/" + runId + "/detail").then(function (d) {
          if (!d || !d.ok) { self.currentRunErr = (d && d.error) || "加载失败"; return; }
          self.currentRun = d.run;
        });
      },
      loadOne: function (name) {
        var self = this;
        self.current = null;
        apiGet(API + "/list").then(function (d) {
          if (!d || !d.ok) { self.err = (d && d.error) || "加载失败"; return; }
          var hit = (d.workflows || []).filter(function (w) { return w.name === name; })[0];
          if (!hit) { self.err = "workflow 不存在：" + name; return; }
          // 确保 graph 有 nodes/edges 数组
          hit.graph = hit.graph || { nodes: [], edges: [] };
          hit.graph.nodes = hit.graph.nodes || [];
          hit.graph.edges = hit.graph.edges || [];
          self.current = hit;
        });
      },
      openDetail: function (name) { location.hash = "#name=" + encodeURIComponent(name); },
      openNew: function () { location.hash = "#new"; },
      backToList: function () { location.hash = ""; },
      del: function (name) {
        if (!window.confirm('确认删除 workflow "' + name + '"？')) return;
        var self = this;
        apiPost(API + "/delete", { name: name }).then(function (d) {
          if (!d.ok) { alert("删除失败：" + (d.error || "")); return; }
          self.refresh();
        });
      },
      createWorkspaceInline: function () {
        var name = (this.newForm.newWorkspace || "").trim();
        if (!name) return;
        var self = this;
        apiPost(API + "/workspace_create", { name: name }).then(function (d) {
          if (!d.ok) { alert("建工作区失败：" + (d.error || "")); return; }
          self.newForm.workspace = name;
          self.newForm.newWorkspace = "";
          self.refreshWorkspaces();
        });
      },
      submitNew: function () {
        var name = (this.newForm.name || "").trim();
        var ws = this.newForm.workspace;
        if (!name) { alert("workflow 名称不能为空"); return; }
        if (!ws || ws === "__new__") { alert("请选择或新建一个工作区"); return; }
        var self = this;
        // 空图会被后端 compile_graph 拒为「流程为空」——传 script="-- 空流程，请添加节点"
        // 走脚本分支落库，进入详情页后由用户搭画布，onGraphUpdate 会覆盖存图
        apiPost(API + "/save", {
          name: name, workspace: ws, script: "-- 空流程，请添加节点"
        }).then(function (d) {
          if (!d.ok) { alert("创建失败：" + (d.error || "")); return; }
          location.hash = "#name=" + encodeURIComponent(name);
          self.refresh();
        });
      },
      // 蓝图子组件改动 → 防抖保存
      onGraphUpdate: function (newGraph) {
        if (!this.current) return;
        this.current.graph = newGraph;
        var self = this;
        if (self.saveTimer) clearTimeout(self.saveTimer);
        self.saveTimer = setTimeout(function () { self.save(); }, 600);
      },
      save: function () {
        if (!this.current) return;
        var c = this.current;
        return apiPost(API + "/save", {
          name: c.name, workspace: c.workspace, script: c.script || "",
          graph: JSON.stringify(c.graph || {}), chart: JSON.stringify(c.chart || null)
        }).then(function (d) {
          if (!d.ok) console.warn("save failed:", d.error);
        });
      },
      runCurrent: function () {
        if (!this.current) return;
        var self = this;
        self.runBusy = true; self.runOut = null; self.runErr = "";
        self.runSteps = []; self.runOutputTable = null;
        // 每个节点先标 running（除 output 之外均对应一个物化视图）
        var ns = {};
        (self.current.graph.nodes || []).forEach(function (n) { ns[n.id] = "running"; });
        self.nodeStatus = ns;
        this.save().then(function () {
          return apiPost(API + "/run", { name: self.current.name });
        }).then(function (d) {
          if (!d.ok) {
            self.runBusy = false; self.runErr = d.error || "启动失败";
            self.nodeStatus = {}; return;
          }
          self.pollJob(d.job_id);
        });
      },
      pollJob: function (jobId) {
        var self = this;
        var t = setInterval(function () {
          apiGet("/admin/sql/job?id=" + encodeURIComponent(jobId)).then(function (d) {
            if (!d || !d.ok) return;
            if (d.status === "running" || d.status === "queued") return;
            clearInterval(t);
            self.runBusy = false;
            if (d.status !== "done") {
              self.runErr = d.error || "运行失败";
              // 全部节点标失败
              var errNs = {};
              (self.current.graph.nodes || []).forEach(function (n) { errNs[n.id] = "err"; });
              self.nodeStatus = errNs;
              return;
            }
            var r = d.result || {};
            self.runOut = r;
            self.runSteps = r.steps || [];
            // 按 step.node 更新 nodeStatus
            var ns = {};
            (r.steps || []).forEach(function (s) {
              if (s.node) ns[s.node] = s.ok ? "ok" : "err";
            });
            self.nodeStatus = ns;
            // 输出预览表
            if (r.output && r.output.columns) {
              self.runOutputTable = {
                columns: r.output.columns,
                rows: (r.output.rows || []).slice(0, 100),
                total: r.output.row_count || (r.output.rows || []).length
              };
            }
            if (!r.ok) {
              // 找到第一步失败的错误
              var bad = (r.steps || []).filter(function (s) { return !s.ok; })[0];
              self.runErr = (bad && bad.error) || r.error || "运行失败";
            }
          });
        }, 500);
      },
      // ---------- 节点抽屉 ----------
      openNodeDrawer: function (nodeId) {
        this.drawerNodeId = nodeId;
        this.selectedNodeId = nodeId;
      },
      closeNodeDrawer: function () { this.drawerNodeId = null; },
      onDrawerDeleteNode: function (nodeId) {
        // 抽屉里点删除：从 graph 里删除节点 + 关抽屉
        if (!this.current || !this.current.graph) return;
        this.current.graph.nodes = (this.current.graph.nodes || []).filter(function (n) { return n.id !== nodeId; });
        this.current.graph.edges = (this.current.graph.edges || []).filter(function (e) { return e.from !== nodeId && e.to !== nodeId; });
        if (this.selectedNodeId === nodeId) this.selectedNodeId = null;
        this.drawerNodeId = null;
        this.onGraphUpdate(this.current.graph);
      },
      // ---------- AI 生成流程（详情页「✨ AI 生成」）----------
      openWfAi: function () {
        var self = this;
        // 当前 workflow 已有节点 → 默认「修改」模式；否则「新建」
        var hasGraph = !!(this.current && this.current.graph
                          && (this.current.graph.nodes || []).length);
        this.wfAi = { mode: hasGraph ? "modify" : "create",
                      question: "", conn: "", schema: "",
                      connOptions: [], connMeta: {},
                      dbs: [], dbsLoading: false, dbsErr: "",
                      tables: [], tablesLoading: false, picked: {},
                      filter: "", running: false, error: "" };
        // 拉真实连接：过滤 analysis 工作区、redis（不支持 AI）
        apiGet("/admin/sql/connections").then(function (d) {
          if (!self.wfAi) return;
          var meta = {};
          var opts = ((d && d.connections) || []).filter(function (c) {
            return c.project !== "analysis" && c.engine !== "redis";
          }).map(function (c) {
            var v = c.project + "/" + c.connection;
            meta[v] = { engine: c.engine, database: c.database, environment: c.environment };
            return { value: v, label: v, env: c.environment };
          });
          // 排序：绑了默认库的先（省一步选 schema）→ 环境 local→dev→staging→prod
          var envRank = { local: 0, dev: 1, staging: 2, prod: 3 };
          opts.sort(function (a, b) {
            var da = meta[a.value].database ? 0 : 1;
            var db = meta[b.value].database ? 0 : 1;
            if (da !== db) return da - db;
            var pa = a.env in envRank ? envRank[a.env] : 4;
            var pb = b.env in envRank ? envRank[b.env] : 4;
            return pa - pb;
          });
          self.wfAi.connOptions = opts;
          self.wfAi.connMeta = meta;
          // 修改模式：默认用当前 workflow 里已有 source 节点的连接（延用现有）
          if (opts.length && !self.wfAi.conn) {
            var reuse = "";
            if (self.wfAi.mode === "modify" && self.current && self.current.graph) {
              var src = (self.current.graph.nodes || []).find(function (n) {
                return n.type === "source" && n.cfg && n.cfg.conn && meta[n.cfg.conn];
              });
              if (src) reuse = src.cfg.conn;
            }
            self.wfAiSetConn(reuse || opts[0].value);
          }
        });
      },
      closeWfAi: function () { this.wfAi = null; },
      wfAiNeedsDb: function () {
        var w = this.wfAi; if (!w || !w.conn) return false;
        var m = w.connMeta[w.conn]; if (!m) return false;
        if (!(m.engine === "mysql" || m.engine === "postgres" || m.engine === "clickhouse")) return false;
        return !m.database;
      },
      wfAiSetConn: function (v) {
        if (!this.wfAi) return;
        this.wfAi.conn = v; this.wfAi.schema = "";
        this.wfAi.picked = {}; this.wfAi.tables = []; this.wfAi.filter = "";
        this.wfAi.dbs = []; this.wfAi.dbsErr = ""; this.wfAi.error = "";
        if (this.wfAiNeedsDb()) this.wfAiLoadDbs();
        else this.wfAiLoadTables();
      },
      wfAiLoadDbs: function () {
        var self = this, w = this.wfAi; if (!w || !w.conn) return;
        w.dbsLoading = true; w.dbsErr = "";
        apiGet("/admin/sql/databases?conn=" + encodeURIComponent(w.conn)).then(function (d) {
          if (!self.wfAi) return;
          self.wfAi.dbsLoading = false;
          if (d && d.ok) self.wfAi.dbs = d.databases || [];
          else self.wfAi.dbsErr = (d && d.error) || "无法列出库";
        }).catch(function (e) {
          if (self.wfAi) { self.wfAi.dbsLoading = false; self.wfAi.dbsErr = String(e); }
        });
      },
      wfAiSetDb: function (v) {
        if (!this.wfAi) return;
        this.wfAi.schema = v || "";
        this.wfAi.picked = {}; this.wfAi.tables = []; this.wfAi.filter = "";
        this.wfAiLoadTables();
      },
      wfAiLoadTables: function () {
        var self = this, w = this.wfAi; if (!w || !w.conn) return;
        if (this.wfAiNeedsDb() && !w.schema) { w.tables = []; return; }
        w.tablesLoading = true; w.error = "";
        var url = "/admin/sql/tables?conn=" + encodeURIComponent(w.conn);
        if (w.schema) url += "&schema=" + encodeURIComponent(w.schema);
        apiGet(url).then(function (d) {
          if (!self.wfAi) return;
          self.wfAi.tablesLoading = false;
          if (d && d.ok) self.wfAi.tables = d.tables || [];
          else self.wfAi.error = (d && d.error) || "无法加载表列表";
        }).catch(function (e) {
          if (self.wfAi) { self.wfAi.tablesLoading = false; self.wfAi.error = String(e); }
        });
      },
      wfAiTogglePick: function (name) {
        var w = this.wfAi; if (!w) return;
        if (w.picked[name]) delete w.picked[name]; else w.picked[name] = true;
      },
      wfAiVisibleTables: function () {
        var w = this.wfAi; if (!w) return [];
        var f = (w.filter || "").toLowerCase().trim();
        return f ? w.tables.filter(function (t) { return t.toLowerCase().indexOf(f) >= 0; }) : w.tables;
      },
      wfAiGenerate: function () {
        var self = this, w = this.wfAi;
        if (!w || w.running) return;
        var promptForMode = w.mode === "modify" ? "请描述要对流程做的修改" : "请描述你想做的流程";
        if (!(w.question || "").trim()) { w.error = promptForMode; return; }
        if (!w.conn) { w.error = "请选择取数连接"; return; }
        if (this.wfAiNeedsDb() && !w.schema) { w.error = "请先选择一个库（schema）"; return; }
        if (!self.current) { w.error = "请先打开一个流程"; return; }
        w.running = true; w.error = "";
        var payload = {
          conn: w.conn, question: w.question,
          schema: w.schema || null,
          tables: JSON.stringify(Object.keys(w.picked))
        };
        // 修改模式：把当前 graph 塞进 payload，后端拿去做 modify prompt
        if (w.mode === "modify" && self.current.graph
            && (self.current.graph.nodes || []).length) {
          payload.current_graph = JSON.stringify(self.current.graph);
        }
        apiPost("/admin/workflows/ai", payload).then(function (d) {
          if (!self.wfAi) return;
          w.running = false;
          if (!d || !d.ok) { w.error = (d && d.error) || "生成失败"; return; }
          var g = d.graph || {};
          self.current.graph = { nodes: g.nodes || [], edges: g.edges || [] };
          self.selectedNodeId = null; self.nodeStatus = {};
          self.onGraphUpdate(self.current.graph);   // 触发防抖保存
          self.wfAi = null;
        }).catch(function (e) {
          if (self.wfAi) { w.running = false; w.error = String(e); }
        });
      }
    },
    mounted: function () {
      window.addEventListener("hashchange", this.onHashChange);
      // 列表页 / 新建页需要 list & workspaces；详情页/运行详情页也顺便刷一下
      this.refresh();
      this.refreshWorkspaces();
      // 拉一次全局 AI 开关（决定详情页「✨ AI 生成」按钮是否显示）
      var self = this;
      apiGet("/admin/sql/connections").then(function (d) {
        self.aiEnabled = !!(d && d.ai_enabled);
      });
      // 列表页启动运行中轮询（5s）；进入其他视图时停
      this.refreshRunning();
      var self = this;
      this.runningTimer = setInterval(function () {
        // 定时任务 tab 显示运行中面板；流程 tab 不显示但仍需要角标数据可选保留
        if (self.route.view === "schedules") self.refreshRunning();
      }, 5000);
      if (this.route.view === "detail") this.loadOne(this.route.name);
      if (this.route.view === "run") this.loadRun(this.route.runId);
      if (this.route.view === "schedules") { this.refreshSchedules(); this.refreshRunning(); }
    },
    unmounted: function () {
      window.removeEventListener("hashchange", this.onHashChange);
      if (this.runningTimer) { clearInterval(this.runningTimer); this.runningTimer = null; }
    },
    template:
      '<div class="wf-root">'
      // ============ 列表页（流程 / 定时任务 二级 tab）============
      + '<div v-if="route.view===\'list\'||route.view===\'schedules\'" class="wf-list">'
      +   '<div class="wf-list-hd">'
      +     '<div class="wf-tabs2">'
      +       '<a href="#" :class="{active: route.view===\'list\'}" @click.prevent="goToListTab">流程</a>'
      +       '<a href="#schedules" :class="{active: route.view===\'schedules\'}" @click.prevent="goToSchedulesTab">'
      +         '定时任务 <span v-if="schedulesList.length" class="wf-tabs2-badge">{{ schedulesList.length }}</span>'
      +       '</a>'
      +     '</div>'
      +     '<button v-if="route.view===\'list\'" class="dg-btn run" @click="openNew">＋ 新建流程</button>'
      +   '</div>'
      // ---- 流程 tab ----
      + '<div v-if="route.view===\'list\'">'
      +   '<div v-if="err" class="wf-err">{{ err }}</div>'
      +   '<div v-if="!wfs.length && !loading" class="wf-empty">还没有流程。点右上「＋ 新建流程」创建。</div>'
      +   '<div class="wf-cards">'
      +     '<div v-for="w in wfs" :key="w.name" class="wf-card" @click="openDetail(w.name)">'
      +       '<div class="wf-card-hd"><b>{{ w.name }}</b><span class="wf-ws">{{ w.workspace }}</span></div>'
      +       '<div class="wf-card-meta">'
      +         '<span>{{ (w.graph && w.graph.nodes || []).length }} 个节点</span>'
      +         '<span>更新 {{ fmtTime(w.updated_at) }}</span>'
      +       '</div>'
      +       '<div class="wf-card-acts" @click.stop>'
      +         '<button class="dg-btn sm" @click="openDetail(w.name)">编辑</button>'
      +         '<button class="dg-btn sm danger" @click="del(w.name)">删除</button>'
      +       '</div>'
      +     '</div>'
      +   '</div>'
      + '</div>'
      // ---- 定时任务 tab ----
      + '<div v-else class="wf-schedules">'
      +   '<div v-if="runningList.length" class="wf-running-panel">'
      +     '<div class="wf-running-hd">'
      +       '<span class="wf-running-title"><span class="wf-running-dot"></span> 正在运行 ({{ runningList.length }})</span>'
      +       '<span class="wf-running-sub">每 5 秒自动刷新</span>'
      +     '</div>'
      +     '<div class="wf-running-list">'
      +       '<div v-for="r in runningList" :key="r.id" class="wf-running-row" @click="openRunDetail(r.id)">'
      +         '<b>{{ r.name }}</b>'
      +         '<span class="wf-running-tag">{{ r.triggered_by }}</span>'
      +         '<span class="wf-running-elap">已运行 {{ fmtElapsed(r.elapsed_s) }}</span>'
      +         '<a class="wf-running-link" @click.stop="openRunDetail(r.id)">查看 →</a>'
      +       '</div>'
      +     '</div>'
      +   '</div>'
      +   '<div v-if="schedulesErr" class="wf-err">{{ schedulesErr }}</div>'
      +   '<div v-if="!schedulesList.length && !schedulesLoading" class="wf-empty">还没有定时任务。在流程详情页点「⚙ 调度」建。</div>'
      +   '<table v-if="schedulesList.length" class="wf-sched-tbl">'
      +     '<thead><tr>'
      +       '<th style="width:36px"></th>'
      +       '<th>流程</th><th>触发规则</th><th>通知</th><th>产物</th>'
      +       '<th>上次运行</th><th style="text-align:right">操作</th>'
      +     '</tr></thead>'
      +     '<tbody>'
      +       '<tr v-for="s in schedulesList" :key="s.name" :class="{disabled: !s.enabled, missing: !s.workflow_exists}">'
      +         '<td>'
      +           '<label class="wf-sw" :title="s.enabled?\'点击停用\':\'点击启用\'">'
      +             '<input type="checkbox" :checked="s.enabled" @change.stop="toggleScheduleEnabled(s)">'
      +             '<span></span>'
      +           '</label>'
      +         '</td>'
      +         '<td>'
      +           '<a v-if="s.workflow_exists" :href="\'#name=\'+encodeURIComponent(s.name)"><b>{{ s.name }}</b></a>'
      +           '<span v-else class="wf-sched-missing" title="workflow 已删除，请删除此调度">'
      +             '<b>{{ s.name }}</b> <em>（流程已删）</em>'
      +           '</span>'
      +           '<span v-if="s.running" class="wf-sched-running">运行中</span>'
      +         '</td>'
      +         '<td class="mono">{{ fmtCron(s) }}</td>'
      +         '<td class="mono">{{ s.notify_on }}</td>'
      +         '<td class="mono">{{ (s.attach_kinds||[]).join(", ") || "—" }}</td>'
      +         '<td class="mono">'
      +           '<span v-if="s.last_run_at">{{ fmtTime(s.last_run_at) }}'
      +             '<span v-if="s.last_status===\'ok\'" class="wf-sched-ok">✓</span>'
      +             '<span v-else-if="s.last_status===\'failed\'" class="wf-sched-err">✗</span>'
      +           '</span>'
      +           '<span v-else class="wf-muted">—</span>'
      +         '</td>'
      +         '<td style="text-align:right; white-space:nowrap">'
      +           '<button class="dg-btn sm" :disabled="triggerBusy[s.name]||!s.workflow_exists" '
      +             '@click.stop="triggerScheduleNow(s)" title="立即触发一次（走跟 cron 到点一样的链路）">'
      +             '{{ triggerBusy[s.name] ? "触发中…" : "▶ 立即" }}'
      +           '</button>'
      +           '<button class="dg-btn sm" @click.stop="openSchedule(s.name)" title="编辑">编辑</button>'
      +           '<button class="dg-btn sm danger" @click.stop="deleteScheduleRow(s)">删除</button>'
      +         '</td>'
      +       '</tr>'
      +     '</tbody>'
      +   '</table>'
      + '</div>'
      + '</div>'
      // ============ 新建页 ============
      + '<div v-else-if="route.view===\'new\'" class="wf-new">'
      +   '<div class="wf-new-hd"><h2>新建流程</h2><a href="#" @click.prevent="backToList">← 返回列表</a></div>'
      +   '<div class="wf-form">'
      +     '<label>名称</label>'
      +     '<input v-model="newForm.name" placeholder="示例：订单渠道 ROI">'
      +     '<label>分析工作区</label>'
      +     '<dg-select v-if="wsList.length" :model-value="newForm.workspace" :options="workspaceOptions" '
      +       'placeholder="选择工作区…" @update:model-value="v => newForm.workspace = v"/>'
      +     '<div v-if="newForm.workspace === \'__new__\'" class="wf-new-ws">'
      +       '<input v-model="newForm.newWorkspace" placeholder="新工作区名（字母/数字/下划线）">'
      +       '<button class="dg-btn sm" @click="createWorkspaceInline">建工作区</button>'
      +     '</div>'
      +     '<div class="wf-form-acts">'
      +       '<button class="dg-btn run" @click="submitNew">创建并编辑</button>'
      +       '<button class="dg-btn" @click="backToList">取消</button>'
      +     '</div>'
      +   '</div>'
      + '</div>'
      // ============ 详情页 ============
      + '<div v-else-if="route.view===\'detail\'" class="wf-detail">'
      +   '<div class="wf-detail-hd">'
      +     '<a href="#" @click.prevent="backToList">← 返回列表</a>'
      +     '<b v-if="current">{{ current.name }}</b>'
      +     '<span v-if="current" class="wf-ws">{{ current.workspace }}</span>'
      +     '<div class="wf-tabs">'
      +       '<button :class="{active: viewMode===\'blueprint\'}" @click="viewMode=\'blueprint\'">蓝图</button>'
      +       '<button :class="{active: viewMode===\'history\'}" @click="viewMode=\'history\'; loadRuns()">历史</button>'
      +     '</div>'
      +     '<button v-if="aiEnabled" class="dg-btn" @click="openWfAi" title="AI 生成整张流程（覆盖当前画布，仅生成不执行）">✨ AI 生成</button>'
      +     '<button class="dg-btn" @click="openSchedule" title="定时执行 + 通知设置">⚙ 调度</button>'
      +     '<button class="dg-btn run" @click="runCurrent" :disabled="runBusy">'
      +       '{{ runBusy ? "运行中…" : "▶ 运行" }}</button>'
      +   '</div>'
      +   '<div v-if="!current" class="wf-empty">加载中…</div>'
      +   '<div v-else class="wf-detail-body">'
      +     '<wf-blueprint v-if="viewMode===\'blueprint\'" '
      +       ':graph="current.graph" :conn="\'analysis/\' + current.workspace" '
      +       ':node-status="nodeStatus" :selected-node-id="selectedNodeId" '
      +       '@update:graph="onGraphUpdate" '
      +       '@open-node="openNodeDrawer" '
      +       '@select-node="v => selectedNodeId = v" '
      +       '@run="runCurrent"/>'
      // 历史 tab
      +     '<div v-else-if="viewMode===\'history\'" class="wf-history">'
      +       '<div v-if="runsLoading" class="wf-empty">加载中…</div>'
      +       '<div v-else-if="!runsList.length" class="wf-empty">还没有运行记录。点上方「▶ 运行」执行一次，或建调度让它定时跑。</div>'
      +       '<div v-else class="wf-runs">'
      +         '<div v-for="r in runsList" :key="r.id" class="wf-run-row"'
      +            ' :class="[\'st-\'+r.status]" @click="openRun(r.id)">'
      +           '<span class="wf-run-status">{{ r.status===\'ok\' ? \'✓\' : r.status===\'failed\' ? \'✗\' : \'⟳\' }}</span>'
      +           '<span class="wf-run-time">{{ fmtTime(r.started_at) }}</span>'
      +           '<span class="wf-run-trig">{{ r.triggered_by }}</span>'
      +           '<span class="wf-run-err" v-if="r.error">{{ r.error.slice(0, 80) }}</span>'
      +           '<span class="wf-run-arrow">›</span>'
      +         '</div>'
      +       '</div>'
      +     '</div>'
      // ============ 底部固定运行结果面板（蓝图视图下） ============
      +     '<div v-if="viewMode===\'blueprint\' && (runBusy || runSteps.length || runErr || runOutputTable)" '
      +          'class="wf-run-panel">'
      +       '<div class="wf-run-panel-hd">'
      +         '<span v-if="runBusy" class="wf-run-panel-status running">⟳ 运行中…</span>'
      +         '<span v-else-if="runErr" class="wf-run-panel-status err">✗ 失败</span>'
      +         '<span v-else class="wf-run-panel-status ok">✓ 完成</span>'
      +         '<span v-if="runSteps.length" class="wf-hint">'
      +           '{{ runSteps.filter(s=>s.ok).length }}/{{ runSteps.length }} 步成功</span>'
      +         '<button class="wf-run-panel-close" @click="runSteps=[]; runErr=\'\'; runOutputTable=null; runOut=null"'
      +           ' title="关闭">✕</button>'
      +       '</div>'
      +       '<div v-if="runSteps.length" class="wf-run-panel-steps">'
      +         '<span v-for="(s, i) in runSteps" :key="i" class="wf-run-panel-step"'
      +           ' :class="s.ok ? \'ok\' : \'err\'" :title="(s.step || s.name || \'\') + (s.error ? \'\\n\' + s.error : \'\')">'
      +           '<span class="wf-run-panel-step-icon">{{ s.ok ? \'✓\' : \'✗\' }}</span>'
      +           '<span class="wf-run-panel-step-name">{{ s.step || s.name || (\'步骤 \' + (i+1)) }}</span>'
      +           '<span v-if="s.rows != null" class="wf-run-panel-step-rows">{{ s.rows }} 行</span>'
      +         '</span>'
      +       '</div>'
      +       '<div v-if="runErr" class="wf-run-panel-err">{{ runErr }}</div>'
      +       '<div v-if="runOutputTable && runOutputTable.columns.length" class="wf-run-panel-tbl">'
      +         '<div class="wf-run-panel-tbl-hd">输出预览 · {{ runOutputTable.total }} 行</div>'
      +         '<div class="wf-mod-preview-tbl">'
      +           '<table>'
      +             '<thead><tr><th v-for="c in runOutputTable.columns" :key="c">{{ c }}</th></tr></thead>'
      +             '<tbody><tr v-for="(r, ri) in runOutputTable.rows" :key="ri">'
      +               '<td v-for="(v, ci) in r" :key="ci">{{ v == null ? "" : String(v).slice(0, 200) }}</td>'
      +             '</tr></tbody>'
      +           '</table>'
      +         '</div>'
      +       '</div>'
      +     '</div>'
      // ============ 节点抽屉 ============
      +     '<wf-node-editor v-if="drawerNodeId" '
      +       ':graph="current.graph" :workspace="current.workspace" :node-id="drawerNodeId" '
      +       '@update:graph="onGraphUpdate" '
      +       '@close="closeNodeDrawer" '
      +       '@delete-node="onDrawerDeleteNode"/>'
      +   '</div>'
      + '</div>'
      // ============ 运行详情页 ============
      + '<div v-else-if="route.view===\'run\'" class="wf-run-page">'
      +   '<div class="wf-detail-hd">'
      +     '<a href="/admin/workflows">← 返回流程列表</a>'
      +     '<b v-if="currentRun">运行 #{{ currentRun.id }}</b>'
      +     '<a v-if="currentRun" :href="\'/admin/workflows#name=\' + encodeURIComponent(currentRun.name)"'
      +       ' class="wf-ws">{{ currentRun.name }}</a>'
      +     '<span v-if="currentRun" class="wf-run-status" :class="\'st-\'+currentRun.status">'
      +       '{{ currentRun.status===\'ok\' ? \'✓ 成功\' : currentRun.status===\'failed\' ? \'✗ 失败\' : \'⟳ 运行中\' }}</span>'
      +   '</div>'
      +   '<div v-if="currentRunErr" class="wf-err">{{ currentRunErr }}</div>'
      +   '<div v-else-if="!currentRun" class="wf-empty">加载中…</div>'
      +   '<div v-else class="wf-detail-body wf-run-detail">'
      +     '<div class="wf-run-meta">'
      +       '<div><b>触发方式</b> {{ currentRun.triggered_by }}</div>'
      +       '<div><b>开始</b> {{ fmtTime(currentRun.started_at) }}</div>'
      +       '<div v-if="currentRun.finished_at"><b>结束</b> {{ fmtTime(currentRun.finished_at) }}</div>'
      +       '<div v-if="currentRun.xlsx_path">'
      +         '<a :href="\'/admin/workflows/runs/\'+currentRun.id+\'/download/output.xlsx\'" class="dg-btn sm">⬇ 下载 xlsx</a>'
      +       '</div>'
      +     '</div>'
      +     '<div v-if="currentRun.error" class="wf-run-out"><h3>错误信息</h3><pre>{{ currentRun.error }}</pre></div>'
      +     '<div v-if="currentRun.steps && currentRun.steps.length" class="wf-run-steps">'
      +       '<h3>步骤（{{ currentRun.steps.length }}）</h3>'
      +       '<div v-for="(s, i) in currentRun.steps" :key="i" class="wf-run-step"'
      +          ' :class="s.ok ? \'ok\' : \'err\'">'
      +         '<span class="wf-run-step-idx">{{ i+1 }}</span>'
      +         '<span class="wf-run-step-name">{{ s.step || s.name }}</span>'
      +         '<span v-if="s.rows != null" class="wf-run-step-rows">{{ s.rows }} 行</span>'
      +         '<span v-if="s.error" class="wf-run-err">{{ s.error }}</span>'
      +       '</div>'
      +     '</div>'
      +     '<div v-if="currentRun.output_preview && currentRun.output_preview.columns" class="wf-run-preview">'
      +       '<h3>输出预览（{{ currentRun.output_preview.row_count }} 行）</h3>'
      +       '<div class="wf-mod-preview-tbl"><table>'
      +         '<thead><tr><th v-for="c in currentRun.output_preview.columns" :key="c">{{ c }}</th></tr></thead>'
      +         '<tbody><tr v-for="(r, ri) in currentRun.output_preview.rows.slice(0, 50)" :key="ri">'
      +           '<td v-for="(v, ci) in r" :key="ci">{{ v == null ? "" : String(v).slice(0, 80) }}</td>'
      +         '</tr></tbody></table></div>'
      +     '</div>'
      +   '</div>'
      + '</div>'
      // ============ AI 生成流程浮层（顶层）============
      + '<div v-if="wfAi" class="wf-ai-overlay" @click.self="closeWfAi">'
      +   '<div class="wf-ai-card">'
      +     '<div class="wf-ai-hd">'
      +       '<span class="wf-ai-title">✨ AI {{ wfAi.mode===\'modify\' ? \'修改\' : \'生成\' }}流程</span>'
      +       '<span class="wf-ai-sub">{{ wfAi.mode===\'modify\' ? \'在当前流程上按需求增/删/改节点与连线\' : \'描述你要做的分析，AI 会生成一张 DAG 载入画布\' }}（不执行，可继续编辑）</span>'
      +       '<button class="wf-drawer-icon wf-drawer-close" @click="closeWfAi" title="关闭" aria-label="关闭">'
      +         '<svg viewBox="0 0 16 16" width="14" height="14" fill="none" stroke="currentColor" stroke-width="1.6" stroke-linecap="round">'
      +           '<path d="M4 4l8 8M12 4l-8 8"/></svg>'
      +       '</button>'
      +     '</div>'
      +     '<div class="wf-ai-body">'
      // 模式切换（仅当当前 workflow 已有节点时才显示）
      +       '<div v-if="current && current.graph && (current.graph.nodes||[]).length" class="row">'
      +         '<div class="wf-ai-mode-tabs">'
      +           '<button :class="{active: wfAi.mode===\'modify\'}" @click="wfAi.mode=\'modify\'" title="在当前流程上做增/删/改">修改当前流程</button>'
      +           '<button :class="{active: wfAi.mode===\'create\'}" @click="wfAi.mode=\'create\'" title="覆盖生成一张全新流程">新建覆盖</button>'
      +         '</div>'
      +         '<div v-if="wfAi.mode===\'create\'" class="wf-ai-hint" style="margin-top:6px; color: var(--dg-red)">⚠ 新建会覆盖当前画布（{{ (current.graph.nodes||[]).length }} 个节点）</div>'
      +       '</div>'
      +       '<div class="row">'
      +         '<label>{{ wfAi.mode===\'modify\' ? \'修改需求（比如：把 by_channel 改成按日聚合、加 ROI 节点）\' : \'需求描述\' }}</label>'
      +         '<textarea v-model="wfAi.question" rows="3" spellcheck="false"'
      +           ' :placeholder="wfAi.mode===\'modify\' ? \'示例：给 orders 加 WHERE status=paid 的过滤；把 by_channel 改成按天分组\' : \'示例：按渠道聚合最近 7 天订单 ROI，输出前 10 名\'"></textarea>'
      +       '</div>'
      +       '<div class="row">'
      +         '<label>取数连接</label>'
      +         '<dg-select :model-value="wfAi.conn" :options="wfAi.connOptions"'
      +           ' placeholder="选择连接…" @update:model-value="wfAiSetConn"/>'
      +       '</div>'
      +       '<div v-if="wfAi.conn && wfAiNeedsDb()" class="row">'
      +         '<label>库 <span v-if="wfAi.dbsLoading" class="wf-hint">加载中…</span></label>'
      +         '<dg-select :model-value="wfAi.schema" :options="wfAi.dbs.map(x => ({value: x, label: x}))"'
      +           ' :placeholder="wfAi.dbsLoading ? \'加载库…\' : \'选择库…（此连接未绑定默认库）\'"'
      +           ' @update:model-value="wfAiSetDb"/>'
      +         '<div v-if="wfAi.dbsErr" class="wf-mod-schema-err">{{ wfAi.dbsErr }}</div>'
      +       '</div>'
      +       '<div v-if="wfAi.conn && (!wfAiNeedsDb() || wfAi.schema)" class="row">'
      +         '<label>相关表（可多选，帮 AI 定位；不选则给 AI 全部表）'
      +           '<span v-if="wfAi.tablesLoading" class="wf-hint">加载中…</span>'
      +         '</label>'
      +         '<input v-model="wfAi.filter" placeholder="筛选表名…" class="wf-ai-filter">'
      +         '<div v-if="!wfAi.tablesLoading" class="wf-ai-tables">'
      +           '<label v-for="t in wfAiVisibleTables()" :key="t" class="wf-ai-tbl">'
      +             '<input type="checkbox" :checked="!!wfAi.picked[t]" @change="wfAiTogglePick(t)">'
      +             '<span>{{ t }}</span>'
      +           '</label>'
      +           '<span v-if="!wfAiVisibleTables().length" class="wf-ai-hint">（无匹配表）</span>'
      +         '</div>'
      +       '</div>'
      +       '<div v-if="wfAi.error" class="wf-err">{{ wfAi.error }}</div>'
      +       '<div class="wf-ai-foot">'
      +         '<button class="dg-btn" @click="closeWfAi">取消</button>'
      +         '<button class="dg-btn run" @click="wfAiGenerate" :disabled="wfAi.running">'
      +           '{{ wfAi.running ? (wfAi.mode===\'modify\' ? "修改中…（可能 10~30 秒）" : "生成中…（可能 10~30 秒）") : (wfAi.mode===\'modify\' ? "✨ 应用修改" : "✨ 生成流程") }}</button>'
      +       '</div>'
      +     '</div>'
      +   '</div>'
      + '</div>'
      // ============ 调度浮层（顶层，任何视图下都可弹出）============
      + '<div v-if="schedOpen" class="wf-sched-overlay" @click.self="schedOpen=false">'
      +   '<div class="wf-sched-card">'
      +     '<div class="wf-sched-hd">'
      +       '<b>⚙ 调度设置</b>'
      +       '<span class="x" @click="schedOpen=false">✕</span>'
      +     '</div>'
      +     '<div v-if="schedLoading" class="wf-empty">加载中…</div>'
      +     '<div v-else class="wf-sched-body">'
      +       '<div class="row">'
      +         '<label>频率</label>'
      +         '<div class="wf-sched-cron">'
      +           '<dg-select :model-value="schedForm.cron_type" :options="cronTypeOptions"'
      +             ' @update:model-value="v => schedForm.cron_type = v"/>'
      +           '<input v-model="schedForm.cron_value" '
      +              ':placeholder="schedForm.cron_type===\'interval\'?\'5\':'
      +              'schedForm.cron_type===\'daily\'?\'09:30\':'
      +              'schedForm.cron_type===\'weekly\'?\'1 09:30（1=周一，0=周日）\':'
      +              'schedForm.cron_type===\'monthly\'?\'15 09:00（每月15日）\':\'*/5 * * * *\'">'
      +         '</div>'
      +       '</div>'
      +       '<div class="row"><label><input type="checkbox" v-model="schedForm.enabled"> 启用</label></div>'
      +       '<div class="row">'
      +         '<label>通知策略</label>'
      +         '<dg-select :model-value="schedForm.notify_on" :options="notifyOnOptions"'
      +           ' @update:model-value="v => schedForm.notify_on = v"/>'
      +       '</div>'
      +       '<div class="row">'
      +         '<label>通知产物</label>'
      +         '<div class="wf-attach">'
      +           '<label><input type="checkbox" :checked="schedForm.attach_kinds.indexOf(\'summary\')>=0"'
      +             ' @change="toggleAttach(\'summary\')"> 摘要 + 链接</label>'
      +           '<label><input type="checkbox" :checked="schedForm.attach_kinds.indexOf(\'markdown_table\')>=0"'
      +             ' @change="toggleAttach(\'markdown_table\')"> Markdown 表格</label>'
      +           '<label><input type="checkbox" :checked="schedForm.attach_kinds.indexOf(\'xlsx_link\')>=0"'
      +             ' @change="toggleAttach(\'xlsx_link\')"> xlsx 下载链接</label>'
      +         '</div>'
      +       '</div>'
      +       '<div v-if="schedError" class="wf-err">{{ schedError }}</div>'
      +       '<div class="wf-sched-foot">'
      +         '<button class="dg-btn danger" @click="deleteSchedule">删除调度</button>'
      +         '<button class="dg-btn" @click="schedOpen=false">取消</button>'
      +         '<button class="dg-btn run" @click="saveSchedule">保存</button>'
      +       '</div>'
      +     '</div>'
      +   '</div>'
      + '</div>'
      + '</div>'
  };

  window.addEventListener("DOMContentLoaded", function () {
    var app = window.Vue.createApp(App);
    if (window.DgSelect) app.component("dg-select", window.DgSelect);
    app.mount("#wf-app");
  });
})();
