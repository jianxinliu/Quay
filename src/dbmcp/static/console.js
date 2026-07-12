/* 查询台前端：Vue 3 + Monaco。DataGrip 风多 tab IDE，少弹框、一屏操作、状态保活。
 *
 * - tab 三类：query（编辑器+结果）/ data（整个主区是数据网格，双击表打开）/
 *   ddl（编辑区只读展示建表语句）。
 * - 树：库 → tables(N) → 表 → columns/keys/indexes(N)，箭头展开、点名选中、
 *   cmd/ctrl 多选、右键菜单（含批量 DROP，红色确认条二次确认）。
 * - 保活：tab（含结果）、每连接的树展开与元数据、分隔条尺寸全部持久化到
 *   localStorage，切页/刷新回来原样恢复，不重查。
 *
 * Monaco 实例与各 tab 的 model 放模块级变量（不进 Vue 响应式，避免被 Proxy 包坏）。
 */
(function () {
  "use strict";
  var Vue = window.Vue;

  var editor = null;          // 单个 Monaco 编辑器，切 tab 换 model
  var models = new Map();     // tabId -> ITextModel
  var monacoReady = false;
  var currentTables = [];     // 补全用：当前连接可见表（未绑库为 库.表）
  var currentConn = "";
  var tableSchema = {};       // 表名 -> schema（补全按 schema 取列）
  var colCache = {};          // "conn|schema|table" -> columns
  var seq = 1;
  var STORE_KEY = "dbm-console-v2";
  // CSV/TSV 解析（导入用）：自动检测分隔符（Excel 复制区域即 TSV），支持引号包裹与转义
  function parseDelimited(text) {
    text = text.replace(/\r\n?/g, "\n").replace(/\n+$/, "");
    if (!text.trim()) return null;
    var nl = text.indexOf("\n");
    var first = nl < 0 ? text : text.slice(0, nl);
    var delim = first.indexOf("\t") >= 0 ? "\t" : ",";
    var rows = [], row = [], cur = "", inQ = false, started = false;
    for (var i = 0; i < text.length; i++) {
      var ch = text[i];
      if (inQ) {
        if (ch === '"') {
          if (text[i + 1] === '"') { cur += '"'; i++; } else inQ = false;
        } else cur += ch;
      } else if (ch === '"' && !started) { inQ = true; started = true; }
      else if (ch === delim) { row.push(cur); cur = ""; started = false; }
      else if (ch === "\n") { row.push(cur); rows.push(row); row = []; cur = ""; started = false; }
      else { cur += ch; started = true; }
    }
    row.push(cur);
    rows.push(row);
    return rows;
  }

  var ENV_COLORS = { local: "#64748b", dev: "#2563eb", staging: "#d97706", prod: "#dc2626" };
  var chartInst = null;       // ECharts 实例（同一时刻只有活动 tab 的图表可见，共用一个）
  var CHART_PALETTE = ["#3574f0", "#d9a343", "#57965c", "#bc8cff", "#d9534f", "#39c5cf"];

  var RESERVED = {};
  ("where on group order by having limit join inner left right full outer cross " +
   "union select from set values as using natural").split(" ")
    .forEach(function (w) { RESERVED[w] = 1; });

  // 从 SQL 解析 别名/表名 → 真实表名（FROM/JOIN [库.]表 [AS] 别名）
  function resolveTable(sql, ident) {
    var lo = ident.toLowerCase(), map = {};
    var re = /\b(?:from|join)\s+`?(?:([a-zA-Z_][\w$]*)`?\.`?)?([a-zA-Z_][\w$]*)`?(?:\s+(?:as\s+)?`?([a-zA-Z_][\w$]*)`?)?/gi, m;
    while ((m = re.exec(sql))) {
      var schema = m[1], table = m[2], alias = m[3];
      if (schema) tableSchema[table] = schema;
      if (alias && !RESERVED[alias.toLowerCase()]) map[alias.toLowerCase()] = table;
    }
    if (map[lo]) return map[lo];
    for (var i = 0; i < currentTables.length; i++) {
      var ct = currentTables[i], bare = ct.indexOf(".") >= 0 ? ct.split(".").pop() : ct;
      if (ct.toLowerCase() === lo || bare.toLowerCase() === lo) return bare;
    }
    return ident;
  }

  // 按分号切分多条语句（跳过引号/反引号/行注释/块注释），返回 [{s,e}] 偏移区间。
  // 供「光标处执行」：编辑器放多条 SQL，只跑光标所在那条（DataGrip 行为）。
  function stmtRanges(text) {
    var ranges = [], start = 0, i = 0, n = text.length;
    while (i < n) {
      var c = text[i], c2 = text.substr(i, 2);
      if (c === "'" || c === '"' || c === "`") {
        var q = c; i++;
        while (i < n) {
          if (text[i] === "\\") { i += 2; continue; }
          if (text[i] === q) { if (q === "'" && text[i + 1] === "'") { i += 2; continue; } i++; break; }
          i++;
        }
      } else if (c2 === "--" || (c === "#")) {
        while (i < n && text[i] !== "\n") i++;
      } else if (c2 === "/*") {
        i += 2; while (i < n && text.substr(i, 2) !== "*/") i++; i += 2;
      } else if (c === ";") {
        ranges.push({ s: start, e: i + 1 }); i++; start = i;
      } else i++;
    }
    if (start < n && text.slice(start).trim()) ranges.push({ s: start, e: n });
    return ranges;
  }

  // 提取一条语句里 FROM/JOIN/UPDATE/INTO 的表（含 schema 前缀与别名），供上下文补全
  function stmtTables(sql) {
    var out = [], seen = {};
    var re = /\b(?:from|join|update|into)\s+`?(?:([a-zA-Z_][\w$]*)`?\.`?)?([a-zA-Z_][\w$]*)`?(?:\s+(?:as\s+)?`?([a-zA-Z_][\w$]*)`?)?/gi, m;
    while ((m = re.exec(sql))) {
      var schema = m[1] || tableSchema[m[2]] || "";
      var alias = (m[3] && !RESERVED[m[3].toLowerCase()]) ? m[3] : null;
      if (m[1]) tableSchema[m[2]] = m[1];
      var k = schema + "|" + m[2];
      if (!seen[k]) { seen[k] = 1; out.push({ schema: schema, table: m[2], alias: alias }); }
    }
    return out;
  }

  // 上下文补全用的关键字与常用函数（MySQL 向，PG 通用子集）
  var SQL_KEYWORDS = ["SELECT", "FROM", "WHERE", "GROUP BY", "ORDER BY", "HAVING", "LIMIT",
    "OFFSET", "JOIN", "LEFT JOIN", "RIGHT JOIN", "INNER JOIN", "ON", "AS", "AND", "OR", "NOT",
    "IN", "IS NULL", "IS NOT NULL", "LIKE", "BETWEEN", "CASE", "WHEN", "THEN", "ELSE", "END",
    "DISTINCT", "UNION", "UNION ALL", "INSERT INTO", "VALUES", "UPDATE", "SET", "DELETE FROM",
    "SHOW", "EXPLAIN", "WITH", "EXISTS", "ASC", "DESC"];
  var SQL_FUNCS = ["COUNT", "SUM", "AVG", "MIN", "MAX", "NOW", "CONCAT", "COALESCE", "IFNULL",
    "GROUP_CONCAT", "SUBSTRING", "LENGTH", "ROUND", "CAST", "DATE_FORMAT", "FROM_UNIXTIME",
    "UNIX_TIMESTAMP", "DATE", "IF", "DISTINCT"];
  // 光标前最近的主要关键字 → 决定此处该补什么
  var KW_TABLE_CTX = { from: 1, join: 1, into: 1, update: 1 };
  function lastKeyword(before) {
    var m = before.toLowerCase().match(/\b(select|from|join|where|on|having|set|by|and|or|not|when|then|else|in|like|between|distinct|values|into|update|limit|offset|union)\b/g);
    return m ? m[m.length - 1] : null;
  }

  function fetchCols(table) {
    var schema = tableSchema[table] || "";
    var key = currentConn + "|" + schema + "|" + table;
    if (colCache[key]) return Promise.resolve(colCache[key]);
    if (!currentConn) return Promise.resolve([]);
    var url = "/admin/sql/table?conn=" + encodeURIComponent(currentConn) +
              "&table=" + encodeURIComponent(table) + (schema ? "&schema=" + encodeURIComponent(schema) : "");
    return fetch(url).then(function (r) { return r.json(); })
      .then(function (d) { var cols = (d && d.ok && d.columns) ? d.columns : []; colCache[key] = cols; return cols; })
      .catch(function () { return []; });
  }

  // ---------- Monaco 加载（AMD loader + data-URI worker） ----------
  window.MonacoEnvironment = {
    getWorkerUrl: function () {
      var base = location.origin + "/admin/static/monaco/";
      var code = "self.MonacoEnvironment={baseUrl:'" + base + "'};" +
                 "importScripts('" + base + "vs/base/worker/workerMain.js');";
      return "data:text/javascript;charset=utf-8," + encodeURIComponent(code);
    }
  };
  function loadMonaco(cb) {
    window.require.config({ paths: { vs: "/admin/static/monaco/vs" } });
    window.require(["vs/editor/editor.main"], function () { monacoReady = true; cb(); });
  }

  function apiGet(url) {
    return fetch(url, { headers: { Accept: "application/json" } }).then(function (r) { return r.json(); });
  }
  function apiPost(url, obj) {
    var fd = new FormData();
    for (var k in obj) if (obj[k] != null) fd.append(k, obj[k]);
    return fetch(url, { method: "POST", headers: { Accept: "application/json" }, body: fd })
      .then(function (r) { return r.json(); });
  }
  function download(blob, name) {
    var u = URL.createObjectURL(blob); var a = document.createElement("a");
    a.href = u; a.download = name; document.body.appendChild(a); a.click(); a.remove();
    setTimeout(function () { URL.revokeObjectURL(u); }, 1500);
  }
  var LVL = { CRITICAL: "#b0413e", HIGH: "#c56a1c", MEDIUM: "#b58a24", LOW: "#3f7a43" };

  // ---------- 表节点组件（表行 + columns/keys/indexes 子层） ----------
  var TblNode = {
    props: ["tname", "meta", "open", "sub", "selected", "pad", "size"],
    emits: ["toggle", "togglesub", "rowclick", "opendata", "ctxmenu"],
    computed: {
      pkset: function () {
        var s = {};
        ((this.meta && this.meta.primary_key) || []).forEach(function (c) { s[c] = 1; });
        return s;
      }
    },
    template: `
<div>
  <div class="dg-item tbl" :class="{sel: selected}" :style="{paddingLeft: pad+'px'}"
       @click="$emit('rowclick', $event)" @dblclick="$emit('opendata')"
       @contextmenu.prevent="$emit('ctxmenu', $event)" title="双击打开数据 · 右键菜单 · ⌘点多选">
    <span class="tw" @click.stop="$emit('toggle')">{{ open ? "▾" : "▸" }}</span>
    <span class="ic ic-table"></span><span class="nm">{{ tname }}</span>
    <span v-if="size" class="sz">{{ size }}</span>
  </div>
  <template v-if="open">
    <div v-if="!meta" class="dg-empty" :style="{paddingLeft:(pad+22)+'px'}">加载中…</div>
    <template v-else>
      <div class="dg-item sub" :style="{paddingLeft:(pad+16)+'px'}" @click="$emit('togglesub','columns')">
        <span class="tw">{{ sub.columns ? "▾" : "▸" }}</span><span class="ic ic-folder"></span>
        <span class="nm">columns</span><span class="cnt">{{ meta.columns.length }}</span></div>
      <template v-if="sub.columns">
        <div v-for="c in meta.columns" :key="c.name" class="dg-col" :style="{paddingLeft:(pad+38)+'px'}">
          <span class="cn" :class="{pk: pkset[c.name]}">{{ c.name }}</span><span class="ct">{{ c.type }}</span></div>
      </template>
      <div class="dg-item sub" :style="{paddingLeft:(pad+16)+'px'}" @click="$emit('togglesub','keys')">
        <span class="tw">{{ sub.keys ? "▾" : "▸" }}</span><span class="ic ic-key"></span>
        <span class="nm">keys</span><span class="cnt">{{ meta.primary_key.length ? 1 : 0 }}</span></div>
      <template v-if="sub.keys">
        <div v-if="!meta.primary_key.length" class="dg-empty" :style="{paddingLeft:(pad+38)+'px'}">（无主键）</div>
        <div v-else class="dg-col" :style="{paddingLeft:(pad+38)+'px'}">
          <span class="cn pk">PRIMARY</span><span class="ct">{{ meta.primary_key.join(", ") }}</span></div>
      </template>
      <div class="dg-item sub" :style="{paddingLeft:(pad+16)+'px'}" @click="$emit('togglesub','indexes')">
        <span class="tw">{{ sub.indexes ? "▾" : "▸" }}</span><span class="ic ic-idx"></span>
        <span class="nm">indexes</span><span class="cnt">{{ meta.indexes.length }}</span></div>
      <template v-if="sub.indexes">
        <div v-if="!meta.indexes.length" class="dg-empty" :style="{paddingLeft:(pad+38)+'px'}">（无索引）</div>
        <div v-else v-for="i in meta.indexes" :key="i.name" class="dg-col" :style="{paddingLeft:(pad+38)+'px'}">
          <span class="cn">{{ i.name }}</span><span class="ct">{{ (i.columns||[]).join(", ") }}{{ i.unique ? " · UNIQUE" : "" }}</span></div>
      </template>
    </template>
  </template>
</div>`
  };

  // 自绘下拉（原生 <select> 的弹出列表无法样式化，深色 UI 里很扎眼）。
  // 支持筛选（选项 > 8 时显示搜索框）。
  var DgSelect = {
    name: "dg-select",
    props: ["modelValue", "options", "placeholder"],  // options: [{value, label}]
    emits: ["update:modelValue"],
    data: function () { return { open: false, q: "" }; },
    computed: {
      label: function () {
        var v = this.modelValue;
        for (var i = 0; i < this.options.length; i++)
          if (this.options[i].value === v) return this.options[i].label;
        return this.placeholder || "选择…";
      },
      selEnv: function () {
        var v = this.modelValue;
        for (var i = 0; i < this.options.length; i++)
          if (this.options[i].value === v) return this.options[i].env || "";
        return "";
      },
      envColor: function () { return function (e) { return ENV_COLORS[e] || "#64748b"; }; },
      filtered: function () {
        var q = this.q.trim().toLowerCase();
        return q ? this.options.filter(function (o) { return o.label.toLowerCase().indexOf(q) >= 0; })
                 : this.options;
      }
    },
    methods: {
      toggle: function () {
        this.open = !this.open; this.q = "";
        if (this.open) {
          var self = this;
          this.$nextTick(function () { var el = self.$refs.qEl; if (el) el.focus(); });
        }
      },
      pick: function (v) { this.$emit("update:modelValue", v); this.open = false; },
      onDocClick: function (e) { if (!this.$el.contains(e.target)) this.open = false; },
    },
    mounted: function () { document.addEventListener("click", this.onDocClick); },
    unmounted: function () { document.removeEventListener("click", this.onDocClick); },
    template: `
<div class="dg-sel">
  <button type="button" class="dg-sel-btn" @click.stop="toggle" :title="label">
    <span v-if="selEnv" class="dg-env" :style="{background: envColor(selEnv)}">{{ selEnv }}</span>
    <span class="lb">{{ label }}</span><span class="ar">▾</span></button>
  <div v-if="open" class="dg-sel-pop" @click.stop>
    <input v-if="options.length > 8" ref="qEl" v-model="q" class="dg-sel-q" placeholder="筛选…">
    <div class="dg-sel-list">
      <div v-for="o in filtered" :key="o.value" class="dg-sel-item"
           :class="{cur: o.value === modelValue}" @click="pick(o.value)">
        <span v-if="o.env" class="dg-env" :style="{background: envColor(o.env)}">{{ o.env }}</span>{{ o.label }}</div>
      <div v-if="!filtered.length" class="dg-sel-none">（无匹配）</div>
    </div>
  </div>
</div>`
  };

  // EXPLAIN JSON 计划 → 可折叠树。table/access/rows/cost 等关键字段徽章高亮，其余通用渲染。
  var PlanNode = {
    name: "plan-node",
    props: ["label", "node", "depth"],
    computed: {
      isObj: function () { return this.node !== null && typeof this.node === "object"; },
      entries: function () {
        if (!this.isObj) return [];
        if (Array.isArray(this.node)) return this.node.map(function (v, i) { return ["#" + i, v]; });
        return Object.entries(this.node);
      },
      scalars: function () {
        return this.entries.filter(function (e) { return e[1] === null || typeof e[1] !== "object"; });
      },
      children: function () {
        return this.entries.filter(function (e) { return e[1] !== null && typeof e[1] === "object"; });
      },
      headline: function () {
        var n = this.node || {};
        return n.table_name || n["Relation Name"] || n["Node Type"] || n.access_type || "";
      },
      badges: function () {
        var n = this.node || {}, out = [];
        var keys = ["access_type", "Node Type", "key", "Index Name", "rows_examined_per_scan",
                    "Plan Rows", "filtered", "Total Cost", "query_cost"];
        keys.forEach(function (k) { if (n[k] != null) out.push(k + ": " + n[k]); });
        var ci = n.cost_info || {};
        if (ci.query_cost) out.push("cost: " + ci.query_cost);
        return out;
      }
    },
    template: `
<details class="dg-plan" :open="depth < 3" :style="{marginLeft: (depth ? 12 : 0) + 'px'}">
  <summary><span class="pl-label">{{ label }}</span>
    <span v-if="headline" class="pl-head">{{ headline }}</span>
    <span v-for="b in badges" :key="b" class="pl-badge">{{ b }}</span></summary>
  <div v-if="scalars.length" class="pl-scalars">
    <span v-for="e in scalars" :key="e[0]" class="pl-kv">{{ e[0] }}: <b>{{ e[1] === null ? "null" : e[1] }}</b></span>
  </div>
  <plan-node v-for="e in children" :key="e[0]" :label="e[0]" :node="e[1]" :depth="depth+1"/>
</details>`
  };

  var app = Vue.createApp({
    data: function () {
      return {
        connections: [], workspaces: [], wfs: [], tabs: [], activeId: null,
        importPlan: null,  // {t, db, workspace, dataset, limit} 导入到分析工作区的内联表单
        importRows: null,  // {t, db, text, header, parsed} 数据导入（CSV/粘贴 → INSERT）
        tblSearch: null,   // {q, results, sel, loading} 全局表名搜索浮层（⌘P）
        // 树状态（当前连接）
        databases: [], tablesByDb: {}, tableMeta: {}, tableSizes: {},
        openDb: {}, openTf: {}, openTbl: {}, openSub: {},
        tablesLoading: false, lastLoadedConn: null, schemaFilter: "",
        treeCache: {},          // conn -> 树快照（切连接/切页保活）
        selected: {},           // metaKey -> {t, db}（多选）
        dragId: null,
        ctx: { show: false, x: 0, y: 0, table: "", schema: "", multi: false },
        dropPlan: null,         // {items:[{t,db}], running, results}
        delSnip: null, delWf: null, wfAsk: null,
        history: [], showHistory: false,
        snippets: [], showSnipForm: false, snipDraft: { title: "", note: "" },
        exportOpen: false, copyOpen: false, editorReady: false, toast: "",
        schemaShow: {}, schemaDefault: {}, schemaPickOpen: false,
        vpOpen: false, vpTab: "value", vpVal: "", vpNull: false,
        leftW: 264, editorH: 300,
        linkDraft: null,        // 画布拉线中 {from, x, y}（画布内坐标）
      };
    },
    computed: {
      activeTab: function () {
        var id = this.activeId, ts = this.tabs;
        for (var i = 0; i < ts.length; i++) if (ts[i].id === id) return ts[i];
        return null;
      },
      connMeta: function () {
        var t = this.activeTab; if (!t || !t.conn) return null;
        for (var i = 0; i < this.connections.length; i++)
          if (this.connections[i].value === t.conn) return this.connections[i];
        return null;
      },
      needsDb: function () {
        var m = this.connMeta;
        return !!m && (m.engine === "mysql" || m.engine === "postgres") && !m.database;
      },
      filteredDatabases: function () {
        var conn = this.activeTab ? this.activeTab.conn : "";
        var show = this.schemaShow[conn];
        var list = (show && show.length)
          ? this.databases.filter(function (d) { return show.indexOf(d) >= 0; })
          : this.databases;
        var q = this.schemaFilter.trim().toLowerCase();
        return q ? list.filter(function (d) { return d.toLowerCase().indexOf(q) >= 0; }) : list;
      },
      selCount: function () { return Object.keys(this.selected).length; },
      execState: function () {
        var t = this.activeTab;
        if (!t) return { cls: "idle", icon: "▷", tip: "未执行" };
        if (t.running) return { cls: "running", icon: "⟳", tip: "执行中…" };
        if (t.err) return { cls: "err", icon: "✗", tip: t.err };
        if (t.ok) return { cls: "ok", icon: "✓", tip: "成功 · 影响 " + t.ok.affected_rows + " 行 · " + t.ok.duration_ms + " ms" };
        if (t.result) return { cls: "ok", icon: "✓",
          tip: "成功 · " + t.result.rows.length + " 行 · " + (t.result.duration_ms || 0) + " ms" };
        return { cls: "idle", icon: "▷", tip: "未执行" };
      },
      editorRowStyle: function () {
        var t = this.activeTab;
        if (t && t.type === "ddl") return { flex: "1", height: "auto" };
        return { height: this.editorH + "px" };
      },
      connOptions: function () {
        var opts = [{ value: "", label: "选择连接…", env: "" }].concat(this.connections.map(function (c) {
          return { value: c.value, label: c.connection + " · " + c.engine, env: c.environment || "" };
        }));
        return opts.concat(this.workspaces.map(function (w) {
          return { value: "analysis/" + w, label: "⚗ " + w + " · 分析工作区", env: "" };
        }));
      },
      isAnalysis: function () {
        var t = this.activeTab; return !!t && (t.conn || "").indexOf("analysis/") === 0;
      },
      schemaOptions: function () {
        var m = this.connMeta;
        var head = { value: "", label: m && m.database ? "默认（" + m.database + "）" : "未指定" };
        return [head].concat(this.databases.map(function (d) { return { value: d, label: d }; }));
      },
      isProd: function () { var m = this.connMeta; return !!m && m.environment === "prod"; },
      isStaging: function () { var m = this.connMeta; return !!m && m.environment === "staging"; },
      envInfo: function () {
        var m = this.connMeta;
        if (!m || !m.environment) return null;
        return { env: m.environment, color: ENV_COLORS[m.environment] || "#64748b" };
      },
      chartTypeOptions: function () {
        return [{ value: "bar", label: "柱状" }, { value: "line", label: "折线" },
                { value: "pie", label: "饼图" }, { value: "scatter", label: "散点" }];
      },
      chartAggOptions: function () {
        return [{ value: "", label: "不聚合" }, { value: "sum", label: "SUM" },
                { value: "count", label: "COUNT" }, { value: "avg", label: "AVG" },
                { value: "min", label: "MIN" }, { value: "max", label: "MAX" }];
      },
      chartColOptions: function () {
        var t = this.activeTab;
        if (!t || !t.result) return [];
        return t.result.columns.map(function (c) { return { value: c, label: c }; });
      },
      selNode: function () {
        var t = this.activeTab;
        if (!t || t.type !== "flow" || !t.sel || !t.graph) return null;
        return t.graph.nodes.find(function (n) { return n.id === t.sel; }) || null;
      },
      realConnOptions: function () {  // 取数节点可选的真实连接（不含分析工作区）
        return this.connections.map(function (c) {
          return { value: c.value,
                   label: c.connection + " · " + c.engine + (c.environment ? " (" + c.environment + ")" : "") };
        });
      },
      joinKindOptions: function () {
        return ["INNER", "LEFT", "RIGHT", "FULL"].map(function (k) { return { value: k, label: k }; });
      }
    },
    methods: {
      flash: function (m) { var self = this; this.toast = m; clearTimeout(this._tt);
        this._tt = setTimeout(function () { self.toast = ""; }, 2600); },
      lvColor: function (l) { return LVL[l] || "#666"; },
      numOr: function (v) { return v == null ? "未知" : "约 " + v; },
      boolText: function (v) { return v === true ? "是" : v === false ? "否" : "未知"; },
      fmtTs: function (iso) {
        if (!iso) return "";
        var d = new Date(iso); if (isNaN(d)) return iso;
        function p(n) { return n < 10 ? "0" + n : "" + n; }
        return d.getFullYear() + "-" + p(d.getMonth() + 1) + "-" + p(d.getDate()) + " "
          + p(d.getHours()) + ":" + p(d.getMinutes());
      },
      // 表容量分级：M / G / T（小于 0.05M 显示 <0.1 M）
      fmtSize: function (b) {
        if (b == null) return "";
        var m = b / 1048576;
        if (m >= 1048576) return (m / 1048576).toFixed(1) + " T";
        if (m >= 1024) return (m / 1024).toFixed(1) + " G";
        return (m < 0.05 ? "<0.1" : m.toFixed(1)) + " M";
      },
      cellText: function (v) {
        if (v == null) return "";
        if (typeof v === "object") return "__bytes_base64__" in v ? "base64:" + v.__bytes_base64__ : JSON.stringify(v);
        return String(v);
      },
      cellTitle: function (v) { return v == null ? "NULL" : this.cellText(v); },
      currentSql: function () {
        var m = models.get(this.activeId);
        if (m) return m.getValue();
        var t = this.activeTab; return t ? (t.sql || "") : "";
      },
      sqlOf: function (t) { var m = models.get(t.id); return m ? m.getValue() : (t.sql || ""); },
      mk: function (t, db) { return (db || "") + "|" + t; },
      qn: function (item) { return item.db ? item.db + "." + item.t : item.t; },

      // ---------- 标签页 ----------
      newTab: function (opts) {
        opts = opts || {};
        var def = this.activeTab ? this.activeTab.conn : (this.connections[0] ? this.connections[0].value : "");
        var _conn = opts.conn || def;
        var defSchema = opts.schema != null ? opts.schema
          : (this.schemaDefault[_conn] || (this.activeTab && this.activeTab.conn === _conn ? this.activeTab.schema : ""));
        var id = seq++;
        var tab = { id: id, title: opts.title || ("查询 " + id), conn: opts.conn || def,
                    schema: defSchema || "", type: opts.type || "query", table: opts.table || "",
                    sql: opts.sql || "", result: null, confirm: null, ok: null, err: null, running: false,
                    pinned: false,
                    // data tab：WHERE 条 / ORDER BY 表达式（走 SQL 重查，跨页正确）
                    where: opts.where || "", orderBy: "", lastPage: 0,
                    pendingSql: null, readSql: null, explain: null, edit: null,
                    wfName: opts.wfName || "", wfSteps: null, vsel: null,
                    view: "table", chart: null,
                    graph: opts.graph || null, sel: null, nodeStatus: {},
                    rowSel: {}, lastSelRi: -1, newRow: null, resQ: null };
        this.tabs.push(tab);
        if (monacoReady) models.set(id, window.monaco.editor.createModel(tab.sql, "sql"));
        this.switchTab(id);
        this.persist();
        return tab;
      },
      togglePin: function (id) {
        var t = this.tabs.find(function (x) { return x.id === id; });
        if (t) { t.pinned = !t.pinned; this.persist(); }
      },
      closeTab: function (id) {
        var i = this.tabs.findIndex(function (t) { return t.id === id; });
        if (i < 0) return;
        if (this.tabs[i].pinned) { this.flash("已固定的 tab，先取消固定再关闭"); return; }
        this.tabs.splice(i, 1);
        var m = models.get(id); if (m) { m.dispose(); models.delete(id); }
        if (!this.tabs.length) { this.newTab({}); return; }
        if (this.activeId === id) this.switchTab(this.tabs[Math.max(0, i - 1)].id);
        this.persist();
      },
      switchTab: function (id) {
        this.activeId = id;
        var t = this.activeTab;
        var m = models.get(id);
        if (editor && m) { editor.setModel(m); editor.updateOptions({ readOnly: !!t && t.type === "ddl" }); }
        if (t && t.conn !== this.lastLoadedConn) this.loadTree();
        var self = this;
        this.$nextTick(function () { if (editor && t && t.type === "query") editor.focus(); });
        if (t && t.view === "chart" && t.result) this.renderChart(); else this.disposeChart();
        this.scheduleLint();
      },
      onTabDragStart: function (id, e) { this.dragId = id; if (e.dataTransfer) e.dataTransfer.effectAllowed = "move"; },
      onTabDrop: function (targetId) {
        var from = this.tabs.findIndex(function (t) { return t.id === this.dragId; }, this);
        var to = this.tabs.findIndex(function (t) { return t.id === targetId; });
        if (from < 0 || to < 0 || from === to) { this.dragId = null; return; }
        var moved = this.tabs.splice(from, 1)[0];
        this.tabs.splice(to, 0, moved);
        this.dragId = null; this.persist();
      },
      // 双击表 / 右键「打开表数据」：data 型 tab，主区直接是数据网格
      openTableTab: function (t, db) {
        var q = db ? db + "." + t : t;
        this.newTab({ type: "data", title: t, table: t, schema: db || "", sql: "SELECT * FROM " + q });
        var self = this; this.$nextTick(function () { self.run(false); });
      },
      // 右键「查看 DDL」：ddl 型 tab，编辑区只读展示建表语句
      openDdlTab: function (t, db) {
        var self = this;
        var tab = this.newTab({ type: "ddl", title: "DDL · " + t, table: t, schema: db || "" });
        apiGet("/admin/sql/ddl?conn=" + encodeURIComponent(tab.conn) + "&table=" + encodeURIComponent(t)
               + (db ? "&schema=" + encodeURIComponent(db) : "")).then(function (d) {
          var m = models.get(tab.id);
          if (m) m.setValue(d.ok ? (d.ddl || "-- （空）") : "-- 获取 DDL 失败：" + d.error);
          self.persist();
        });
      },

      // ---------- 连接 / 树（带每连接快照缓存，保活） ----------
      loadConnections: function () {
        var self = this;
        return apiGet("/admin/sql/connections").then(function (d) {
          self.connections = (d && d.connections) || [];
          self.workspaces = (d && d.workspaces) || [];
          self.loadWorkflows();
          if (!self.tabs.length) self.newTab({});
          else if (self.activeTab) self.loadTree();
        });
      },
      setConn: function (val) { if (this.activeTab) { this.activeTab.conn = val; this.persist(); this.loadTree(); } },
      setSchema: function (val) {
        var t = this.activeTab; if (!t) return;
        t.schema = val;
        if (t.conn) this.schemaDefault[t.conn] = val;  // 该连接后续新 tab 的默认执行 schema
        this.persist();
      },
      schemaChecked: function (db) {
        var conn = this.activeTab ? this.activeTab.conn : "";
        var s = this.schemaShow[conn];
        return !s || !s.length || s.indexOf(db) >= 0;  // 空集视为全部显示
      },
      toggleSchemaShow: function (db) {
        var conn = this.activeTab ? this.activeTab.conn : "";
        if (!conn) return;
        var s = (this.schemaShow[conn] && this.schemaShow[conn].length)
          ? this.schemaShow[conn].slice()
          : this.databases.slice();  // 从"全部"开始显式化，再摘掉这个
        var i = s.indexOf(db);
        if (i >= 0) s.splice(i, 1); else s.push(db);
        this.schemaShow[conn] = s;
        this.persist();
      },
      showAllSchemas: function () {
        var conn = this.activeTab ? this.activeTab.conn : "";
        if (conn) { this.schemaShow[conn] = []; this.persist(); }
      },
      stashTree: function () {
        if (!this.lastLoadedConn) return;
        this.treeCache[this.lastLoadedConn] = {
          databases: this.databases, tablesByDb: this.tablesByDb, tableMeta: this.tableMeta,
          tableSizes: this.tableSizes,
          openDb: this.openDb, openTf: this.openTf, openTbl: this.openTbl, openSub: this.openSub,
          schemaFilter: this.schemaFilter,
        };
      },
      rebuildCompletion: function () {
        currentTables = []; tableSchema = {};
        for (var db in this.tablesByDb) {
          (this.tablesByDb[db] || []).forEach(function (t) {
            if (db) { tableSchema[t] = db; currentTables.push(db + "." + t); }
            else currentTables.push(t);
          });
        }
      },
      loadTree: function (force) {
        var self = this; var t = this.activeTab;
        this.stashTree();
        this.databases = []; this.tablesByDb = {}; this.tableMeta = {}; this.tableSizes = {};
        this.openDb = {}; this.openTf = {}; this.openTbl = {}; this.openSub = {};
        this.schemaFilter = ""; this.selected = {};
        if (!t || !t.conn) { this.lastLoadedConn = null; currentTables = []; currentConn = ""; return; }
        this.lastLoadedConn = t.conn; currentConn = t.conn;
        this.loadHistory();
        var cached = !force && this.treeCache[t.conn];
        if (cached) {  // 快照恢复：不发任何请求
          this.databases = cached.databases; this.tablesByDb = cached.tablesByDb;
          this.tableMeta = cached.tableMeta; this.tableSizes = cached.tableSizes || {};
          this.openDb = cached.openDb;
          this.openTf = cached.openTf; this.openTbl = cached.openTbl; this.openSub = cached.openSub;
          this.schemaFilter = cached.schemaFilter || "";
          this.rebuildCompletion();
          return;
        }
        var m = this.connMeta;
        if (m && (m.engine === "mysql" || m.engine === "postgres")) {
          apiGet("/admin/sql/databases?conn=" + encodeURIComponent(t.conn)).then(function (d) {
            self.databases = d && d.ok ? d.databases : [];
            if (d && !d.ok) self.flash(d.error);
            self.persist();
          });
        }
        if (this.needsDb) return;
        // 绑库连接：顶层就是 tables 文件夹（db key = ""）
        this.fetchTables("");
      },
      refreshTree: function () {
        // 刷新 = 重拉数据但保留展开路径与选择之外的状态；已展开层自动重新加载
        var conn = this.lastLoadedConn;
        if (!conn) return;
        delete this.treeCache[conn];
        var keep = { openDb: this.openDb, openTf: this.openTf, openTbl: this.openTbl,
                     openSub: this.openSub, schemaFilter: this.schemaFilter };
        this.lastLoadedConn = null;
        this.loadTree(true);
        this.openDb = keep.openDb; this.openTf = keep.openTf;
        this.openTbl = keep.openTbl; this.openSub = keep.openSub;
        this.schemaFilter = keep.schemaFilter;
        var self = this;
        Object.keys(this.openTf).forEach(function (db) {
          if (self.openTf[db] && !self.tablesByDb[db]) self.fetchTables(db);
        });
      },
      fetchTables: function (db) {
        var self = this, t = this.activeTab;
        this.tablesLoading = true;
        apiGet("/admin/sql/tables?conn=" + encodeURIComponent(t.conn)
               + (db ? "&schema=" + encodeURIComponent(db) : "")).then(function (d) {
          self.tablesLoading = false;
          if (!d.ok) { self.flash(d.error); self.tablesByDb[db] = []; return; }
          self.tablesByDb[db] = d.tables || [];
          var sz = d.sizes || {};
          (d.tables || []).forEach(function (tb) {
            if (sz[tb] != null) self.tableSizes[self.mk(tb, db)] = sz[tb];
          });
          // 已展开但缺元数据的表（刷新后）自动补拉，避免停在「加载中…」
          (d.tables || []).forEach(function (tb) {
            var k = self.mk(tb, db);
            if (self.openTbl[k] && !self.tableMeta[k]) self.fetchMeta(tb, db);
          });
          self.rebuildCompletion(); self.persist();
        }).catch(function () { self.tablesLoading = false; self.tablesByDb[db] = []; });
      },
      toggleDb: function (db) {
        this.openDb[db] = !this.openDb[db];
        if (this.openDb[db] && this.openTf[db] === undefined) this.toggleTf(db);  // 首次展开库时自动展开 tables
        this.persist();
      },
      toggleTf: function (db) {
        this.openTf[db] = !this.openTf[db];
        if (this.openTf[db] && !this.tablesByDb[db]) this.fetchTables(db);
        this.persist();
      },
      toggleTbl: function (t, db) {
        var k = this.mk(t, db);
        this.openTbl[k] = !this.openTbl[k];
        if (!this.openSub[k]) this.openSub[k] = { columns: true, keys: false, indexes: false };
        if (this.openTbl[k] && !this.tableMeta[k]) this.fetchMeta(t, db);
        this.persist();
      },
      toggleSub: function (t, db, section) {
        var k = this.mk(t, db);
        if (!this.openSub[k]) this.openSub[k] = {};
        this.openSub[k][section] = !this.openSub[k][section];
        this.persist();
      },
      fetchMeta: function (t, db) {
        var self = this, tab = this.activeTab, k = this.mk(t, db);
        apiGet("/admin/sql/table?conn=" + encodeURIComponent(tab.conn) + "&table=" + encodeURIComponent(t)
               + (db ? "&schema=" + encodeURIComponent(db) : "")).then(function (d) {
          if (!d.ok) { self.flash(d.error); return; }
          self.tableMeta[k] = { columns: d.columns || [], indexes: d.indexes || [],
                                primary_key: d.primary_key || [] };
          self.persist();
        });
      },
      // 选中：点名单选，⌘/Ctrl 点多选
      clickTable: function (e, t, db) {
        var k = this.mk(t, db);
        if (e.metaKey || e.ctrlKey) {
          if (this.selected[k]) delete this.selected[k];
          else this.selected[k] = { t: t, db: db || "" };
        } else {
          this.selected = {}; this.selected[k] = { t: t, db: db || "" };
        }
      },
      clearSel: function () { this.selected = {}; },
      insertText: function (text) {
        if (!editor) return;
        var sel = editor.getSelection();
        editor.executeEdits("insert", [{ range: sel, text: text, forceMoveMarkers: true }]);
        editor.focus();
      },
      insertSelect: function (t, db) {
        var q = db ? db + "." + t : t;
        this.insertText("SELECT * FROM " + q + " LIMIT 100;");
      },

      // ---------- 表右键菜单 / 批量 DROP ----------
      openCtx: function (e, t, db) {
        var k = this.mk(t, db);
        var multi = !!this.selected[k] && this.selCount > 1;
        if (!this.selected[k]) { this.selected = {}; this.selected[k] = { t: t, db: db || "" }; }
        this.ctx = { show: true, x: Math.min(e.clientX, window.innerWidth - 210),
                     y: Math.min(e.clientY, window.innerHeight - 300),
                     table: t, schema: db || "", multi: multi };
      },
      closeCtx: function () { this.ctx.show = false; },
      qualified: function () { return this.ctx.schema ? this.ctx.schema + "." + this.ctx.table : this.ctx.table; },
      ctxAction: function (act) {
        var t = this.ctx.table, s = this.ctx.schema, q = this.qualified();
        var multi = this.ctx.multi;
        this.closeCtx();
        var self = this;
        if (act === "open") this.openTableTab(t, s);
        else if (act === "ddl") this.openDdlTab(t, s);
        else if (act === "select") this.insertSelect(t, s);
        else if (act === "count") {
          // 轻量即时统计：不开 tab，toast 显示（之前开 data tab 会被 buildDataSql 覆盖成 SELECT *）
          var tab = this.activeTab;
          this.flash("统计中…");
          apiPost("/admin/sql/run", { conn: tab.conn, sql: "SELECT count(*) AS total FROM " + q })
            .then(function (d) {
              if (d.ok && d.kind === "read" && d.rows.length) self.flash(q + " 共 " + d.rows[0][0] + " 行");
              else self.flash(d.error || "统计失败");
            }).catch(function (e) { self.flash("" + e); });
        }
        else if (act === "copy") {
          var text = multi ? Object.values(this.selected).map(this.qn).join(", ") : q;
          (navigator.clipboard ? navigator.clipboard.writeText(text) : Promise.reject())
            .then(function () { self.flash("已复制 " + text.slice(0, 60)); })
            .catch(function () { self.flash(text); });
        }
        else if (act === "toanalysis") {
          this.importPlan = { t: t, db: s, workspace: this.workspaces[0] || "ws1",
                              dataset: t, limit: 200000, running: false };
        }
        else if (act === "import") this.openImport(t, s);
        else if (act === "drop") {
          var items = multi ? Object.values(this.selected) : [{ t: t, db: s }];
          this.dropPlan = { items: items, running: false, results: null };
        }
      },
      confirmDrop: function () {
        var self = this, plan = this.dropPlan, tab = this.activeTab;
        if (!plan || plan.running || !tab) return;
        plan.running = true; plan.results = [];
        // 逐条执行（每条经 confirm=1，由 writer 执行并落审计 admin_execute）
        var chain = Promise.resolve();
        plan.items.forEach(function (item) {
          chain = chain.then(function () {
            var q = self.qn(item);
            return apiPost("/admin/sql/run", { conn: tab.conn, sql: "DROP TABLE " + q, confirm: "1" })
              .then(function (d) {
                plan.results.push({ q: q, ok: !!(d.ok && d.kind === "write"), error: d.error || "" });
              })
              .catch(function (e) { plan.results.push({ q: q, ok: false, error: "" + e }); });
          });
        });
        chain.then(function () {
          plan.running = false;
          var okN = plan.results.filter(function (r) { return r.ok; }).length;
          self.flash("DROP 完成：成功 " + okN + " / " + plan.results.length);
          self.clearSel();
          self.refreshTree();
        });
      },

      loadWorkflows: function () {
        var self = this;
        apiGet("/admin/workflows").then(function (d) { self.wfs = d && d.ok ? d.workflows : []; });
      },
      saveWorkflow: function () {
        var self = this, t = this.activeTab;
        if (!t || (!this.isAnalysis && t.type !== "flow")) { this.flash("请先切到分析工作区连接"); return; }
        var name = t.wfName || "";
        this.wfAsk = { name: name, ws: (t.conn || "").split("/")[1] || "flow" };
      },
      confirmSaveWorkflow: function () {
        var self = this, t = this.activeTab, a = this.wfAsk;
        if (!a || !a.name.trim()) { this.flash("请填写 workflow 名称"); return; }
        // 图表配置随 workflow 保存（含当前视图，载入/重跑时原样恢复）
        var chart = t.chart ? JSON.stringify(Object.assign({ view: t.view || "table" }, t.chart)) : "";
        var isFlow = t.type === "flow";
        apiPost("/admin/workflows/save", { name: a.name.trim(), workspace: a.ws,
                                           script: isFlow ? "" : this.currentSql(),
                                           graph: isFlow ? JSON.stringify(t.graph) : "",
                                           chart: chart })
          .then(function (d) {
            if (!d.ok) { self.flash(d.error); return; }
            t.wfName = a.name.trim(); self.wfAsk = null;
            self.flash("已保存 workflow「" + d.workflow.name + "」（含 "
                       + d.workflow.sources.length + " 个数据源配方"
                       + (d.workflow.chart ? "，含图表" : "") + "）");
            self.loadWorkflows();
          });
      },
      runWorkflow: function (wf) {
        // 在（或新开）该工作区的 tab 里跑：结果走异步 job，kind=workflow
        var self = this;
        var conn = "analysis/" + wf.workspace;
        var t = this.activeTab;
        if (wf.graph) {  // DAG workflow：开画布 tab，逐节点标注状态
          if (!t || t.type !== "flow" || t.wfName !== wf.name)
            t = this.newFlowTab({ title: "▶ " + wf.name, conn: conn,
                                  graph: JSON.parse(JSON.stringify(wf.graph)), wfName: wf.name });
          t.graph.nodes.forEach(function (n) { t.nodeStatus[n.id] = "running"; });
        } else if (!t || t.conn !== conn) {
          t = this.newTab({ title: "▶ " + wf.name, conn: conn, sql: wf.script });
        }
        t.running = true; t.err = null; t.ok = null; t.result = null; t.confirm = null; t.wfName = wf.name;
        this.applyWfChart(t, wf);
        apiPost("/admin/workflows/run", { name: wf.name }).then(function (d) {
          if (!d.ok) { t.running = false; t.err = d.error; return; }
          t.jobId = d.job_id; t.jobPage = 0; self.persist();
          self.pollJob(t.id, d.job_id, 0);
        });
      },
      loadWorkflow: function (wf) {
        var t;
        if (wf.graph) {  // DAG workflow → 画布 tab
          t = this.newFlowTab({ title: wf.name, conn: "analysis/" + wf.workspace,
                                graph: JSON.parse(JSON.stringify(wf.graph)), wfName: wf.name });
        } else {
          t = this.newTab({ title: wf.name, conn: "analysis/" + wf.workspace, sql: wf.script });
          t.wfName = wf.name;
        }
        this.applyWfChart(t, wf);
      },
      applyWfChart: function (t, wf) {
        if (!wf.chart) return;
        t.chart = { type: wf.chart.type || "bar", x: wf.chart.x || "",
                    y: wf.chart.y || "", agg: wf.chart.agg || "" };
        t.view = wf.chart.view || "chart";
      },
      askDeleteWf: function (name) {
        if (this.delWf !== name) {
          this.delWf = name; this.flash("再点一次确认删除 workflow");
          var self = this; setTimeout(function () { if (self.delWf === name) self.delWf = null; }, 3000);
          return;
        }
        this.delWf = null;
        var self2 = this;
        apiPost("/admin/workflows/delete", { name: name }).then(function (d) {
          if (!d.ok) { self2.flash(d.error); return; } self2.flash("已删除"); self2.loadWorkflows();
        });
      },
      confirmImport: function () {
        var self = this, p = this.importPlan, tab = this.activeTab;
        if (!p || p.running || !tab) return;
        p.running = true;
        var q = p.db ? p.db + "." + p.t : p.t;
        apiPost("/admin/analysis/import", {
          conn: tab.conn, workspace: p.workspace, dataset: p.dataset,
          sql: "SELECT * FROM " + q, limit: p.limit,
        }).then(function (d) {
          p.running = false;
          if (!d.ok) { self.flash(d.error); return; }
          self.importPlan = null;
          if (self.workspaces.indexOf(p.workspace) < 0) self.workspaces.push(p.workspace);
          self.flash("已导入 " + d.rows + " 行 → ⚗" + p.workspace + "." + p.dataset
                     + (d.truncated_to_limit ? "（达行数上限）" : ""));
        }).catch(function (e) { p.running = false; self.flash("" + e); });
      },

      // ---------- 结果可视化（表格/图表切换，ECharts）----------
      setView: function (v) {
        var t = this.activeTab;
        if (!t) return;
        t.view = v;
        if (v === "chart" && !t.chart) t.chart = this.defaultChart(t.result);
        this.persist();
        if (v === "chart") this.renderChart(); else this.disposeChart();
      },
      // 默认配置猜测：X = 第一列，Y = 第一个数值列
      defaultChart: function (res) {
        var cols = (res && res.columns) || [];
        var y = "";
        if (res && res.rows.length) {
          for (var i = 0; i < cols.length; i++) {
            if (typeof res.rows[0][i] === "number" && i !== 0) { y = cols[i]; break; }
          }
        }
        return { type: "bar", x: cols[0] || "", y: y || cols[1] || cols[0] || "", agg: "" };
      },
      setChartOpt: function (k, v) {
        var t = this.activeTab;
        if (!t || !t.chart) return;
        t.chart[k] = v;
        this.persist();
        this.renderChart();
      },
      // 结果集 → [[x, y]]；agg 非空时按 X 分组聚合（COUNT 计所有行，其余只计数值）
      chartRows: function (t) {
        var res = t.result, c = t.chart;
        var xi = res.columns.indexOf(c.x), yi = res.columns.indexOf(c.y);
        if (xi < 0 || yi < 0) return [];
        function num(v) { return typeof v === "string" && v !== "" && !isNaN(+v) ? +v : v; }
        if (!c.agg) return res.rows.map(function (r) { return [r[xi], num(r[yi])]; });
        var groups = {}, order = [];
        res.rows.forEach(function (r) {
          var k = r[xi] === null ? "NULL" : String(r[xi]);
          if (!(k in groups)) { groups[k] = []; order.push(k); }
          groups[k].push(num(r[yi]));
        });
        return order.map(function (k) {
          var vals = groups[k].filter(function (v) { return typeof v === "number"; });
          var v;
          if (c.agg === "count") v = groups[k].length;
          else if (!vals.length) v = 0;
          else if (c.agg === "sum") v = vals.reduce(function (a, b) { return a + b; }, 0);
          else if (c.agg === "avg") v = vals.reduce(function (a, b) { return a + b; }, 0) / vals.length;
          else if (c.agg === "min") v = Math.min.apply(null, vals);
          else v = Math.max.apply(null, vals);
          return [k, Math.round(v * 1000) / 1000];
        });
      },
      renderChart: function () {
        var self = this;
        this.$nextTick(function () {
          var t = self.activeTab;
          if (!t || t.view !== "chart" || !t.result || !t.chart || !window.echarts) return;
          var el = self.$refs.chartEl;
          if (!el) return;
          if (chartInst && chartInst.getDom() !== el) { chartInst.dispose(); chartInst = null; }
          if (!chartInst) chartInst = window.echarts.init(el);
          var data = self.chartRows(t), c = t.chart;
          var axis = { axisLabel: { color: "#9aa0a8" }, axisLine: { lineStyle: { color: "#45484e" } },
                       splitLine: { lineStyle: { color: "#2e3033" } } };
          var opt = { backgroundColor: "transparent", color: CHART_PALETTE,
                      textStyle: { color: "#bcbec4" },
                      tooltip: { trigger: c.type === "bar" || c.type === "line" ? "axis" : "item",
                                 backgroundColor: "#2b2d30", borderColor: "#393b40",
                                 textStyle: { color: "#bcbec4" } },
                      grid: { left: 16, right: 24, top: 28, bottom: 12, containLabel: true } };
          if (c.type === "pie") {
            opt.series = [{ type: "pie", radius: ["28%", "66%"],
                            label: { color: "#9aa0a8" },
                            data: data.map(function (d) { return { name: String(d[0]), value: d[1] }; }) }];
          } else if (c.type === "scatter") {
            opt.xAxis = Object.assign({ type: "value", name: c.x }, axis);
            opt.yAxis = Object.assign({ type: "value", name: c.y }, axis);
            opt.series = [{ type: "scatter", symbolSize: 9,
                            data: data.map(function (d) {
                              var x = typeof d[0] === "number" ? d[0] : +d[0];
                              return [isNaN(x) ? 0 : x, d[1]];
                            }) }];
          } else {
            opt.xAxis = Object.assign({ type: "category",
                                        data: data.map(function (d) { return String(d[0]); }) }, axis);
            opt.yAxis = Object.assign({ type: "value" }, axis);
            opt.series = [{ type: c.type, data: data.map(function (d) { return d[1]; }),
                            smooth: c.type === "line",
                            barMaxWidth: 42 }];
          }
          chartInst.setOption(opt, true);
          chartInst.resize();
        });
      },
      disposeChart: function () { if (chartInst) { chartInst.dispose(); chartInst = null; } },

      // ---------- DAG 画布（flow tab）----------
      // 节点固定尺寸；输入口在左缘（join 两个：left/right），输出口在右缘中点
      newFlowTab: function (opts) {
        opts = opts || {};
        var conn = this.isAnalysis ? this.activeTab.conn
                                   : "analysis/" + (this.workspaces[0] || "flow");
        var t = this.newTab({ type: "flow", title: opts.title || "流程 " + seq,
                              conn: opts.conn || conn,
                              graph: opts.graph || { nodes: [], edges: [] } });
        t.wfName = opts.wfName || "";
        return t;
      },
      flowAddNode: function (type) {
        var t = this.activeTab;
        if (!t || t.type !== "flow") return;
        var prefix = { source: "src", file: "file", filter: "flt", join: "join",
                       aggregate: "agg", sql: "sql", output: "out" }[type] || "n";
        var i = 1;
        var names = {};
        t.graph.nodes.forEach(function (n) { names[n.name] = 1; });
        while (names[prefix + i]) i++;
        var id = "n" + Date.now().toString(36) + Math.floor(Math.random() * 1e4).toString(36);
        var cfg = type === "join" ? { kind: "INNER", on: "", select: "l.*, r.*" }
                : type === "aggregate" ? { group: "", aggs: "" }
                : type === "source" ? { conn: "", sql: "", limit: null }
                : {};
        t.graph.nodes.push({ id: id, type: type, name: prefix + i,
                             x: 30 + (t.graph.nodes.length % 5) * 190,
                             y: 30 + Math.floor(t.graph.nodes.length / 5) * 90, cfg: cfg });
        t.sel = id;
        this.persist();
      },
      flowDelNode: function (id) {
        var t = this.activeTab, g = t.graph;
        g.nodes = g.nodes.filter(function (n) { return n.id !== id; });
        g.edges = g.edges.filter(function (e) { return e.from !== id && e.to !== id; });
        if (t.sel === id) t.sel = null;
        delete t.nodeStatus[id];
        this.persist();
      },
      flowNodeById: function (id) {
        var t = this.activeTab;
        if (!t || !t.graph) return null;
        return t.graph.nodes.find(function (n) { return n.id === id; }) || null;
      },
      flowInPorts: function (n) {
        if (n.type === "join") return ["left", "right"];
        if (n.type === "source" || n.type === "file") return [];
        if (n.type === "sql") return ["in"];  // sql 节点连线仅表意（SQL 里直接引用上游名）
        return ["in"];
      },
      flowHasOut: function (n) { return n.type !== "output"; },
      portPos: function (node, port) {
        var W = 150, H = 40;
        if (port === "out") return { x: node.x + W, y: node.y + H / 2 };
        if (node.type === "join") {
          return { x: node.x, y: node.y + (port === "left" ? 13 : 27) };
        }
        return { x: node.x, y: node.y + H / 2 };
      },
      edgePath: function (e) {
        var f = this.flowNodeById(e.from), t = this.flowNodeById(e.to);
        if (!f || !t) return "";
        var a = this.portPos(f, "out"), b = this.portPos(t, e.port || "in");
        var dx = Math.max(40, Math.abs(b.x - a.x) / 2);
        return "M" + a.x + "," + a.y + " C" + (a.x + dx) + "," + a.y + " "
             + (b.x - dx) + "," + b.y + " " + b.x + "," + b.y;
      },
      draftPath: function () {
        var d = this.linkDraft;
        if (!d) return "";
        var f = this.flowNodeById(d.from);
        if (!f) return "";
        var a = this.portPos(f, "out");
        var dx = Math.max(40, Math.abs(d.x - a.x) / 2);
        return "M" + a.x + "," + a.y + " C" + (a.x + dx) + "," + a.y + " "
             + (d.x - dx) + "," + d.y + " " + d.x + "," + d.y;
      },
      _canvasXY: function (ev) {
        var r = this.$refs.flowCanvas.getBoundingClientRect();
        var el = this.$refs.flowCanvas;
        return { x: ev.clientX - r.left + el.scrollLeft, y: ev.clientY - r.top + el.scrollTop };
      },
      flowNodeDown: function (ev, node) {
        var self = this;
        this.activeTab.sel = node.id;
        var start = this._canvasXY(ev), ox = node.x, oy = node.y, moved = false;
        function move(e2) {
          var p = self._canvasXY(e2);
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
        var p = this._canvasXY(ev);
        this.linkDraft = { from: node.id, x: p.x, y: p.y };
        function move(e2) {
          if (self.linkDraft) { var q = self._canvasXY(e2); self.linkDraft.x = q.x; self.linkDraft.y = q.y; }
        }
        function up() {
          window.removeEventListener("mousemove", move);
          window.removeEventListener("mouseup", up);
          // 若落在输入口上，portUp 已建边并清掉 linkDraft；这里兜底取消
          setTimeout(function () { self.linkDraft = null; }, 0);
        }
        window.addEventListener("mousemove", move);
        window.addEventListener("mouseup", up);
        ev.preventDefault();
        ev.stopPropagation();
      },
      portUp: function (ev, node, port) {
        var d = this.linkDraft, t = this.activeTab;
        if (!d || d.from === node.id) return;
        t.graph.edges = t.graph.edges.filter(function (e) {
          return !(e.to === node.id && (e.port || "in") === port);  // 每个输入口只接一条
        });
        t.graph.edges.push({ from: d.from, to: node.id, port: port });
        this.linkDraft = null;
        this.persist();
      },
      flowDelEdge: function (i) {
        this.activeTab.graph.edges.splice(i, 1);
        this.persist();
      },
      edgeMid: function (e) {
        var f = this.flowNodeById(e.from), t = this.flowNodeById(e.to);
        if (!f || !t) return { x: -99, y: -99 };
        var a = this.portPos(f, "out"), b = this.portPos(t, e.port || "in");
        return { x: (a.x + b.x) / 2, y: (a.y + b.y) / 2 };
      },
      runFlow: function () {
        var self = this, t = this.activeTab;
        if (!t || t.type !== "flow") return;
        var ws = (t.conn || "").split("/")[1] || "flow";
        t.running = true; t.err = null; t.ok = null; t.result = null; t.wfSteps = null;
        t.nodeStatus = {};
        t.graph.nodes.forEach(function (n) { t.nodeStatus[n.id] = "running"; });
        apiPost("/admin/workflows/run_graph", { workspace: ws, graph: JSON.stringify(t.graph) })
          .then(function (d) {
            if (!d.ok) { t.running = false; t.err = d.error; t.nodeStatus = {}; return; }
            t.jobId = d.job_id; t.jobPage = 0; self.persist();
            self.pollJob(t.id, d.job_id, 0);
          }).catch(function (e) { t.running = false; t.err = "" + e; t.nodeStatus = {}; });
      },
      flowPreview: function (node) {
        // 运行过一次后，各节点都是工作区里的视图/表，直接查前 100 行
        this.run(false, 0, 'SELECT * FROM "' + node.name + '" LIMIT 100');
      },
      flowTypeLabel: function (t) {
        return { source: "取数", file: "文件", filter: "过滤", join: "JOIN",
                 aggregate: "聚合", sql: "SQL", output: "输出" }[t] || t;
      },
      flowNodeDesc: function (n) {
        var c = n.cfg || {};
        if (n.type === "source") return c.conn ? c.conn + (c.sql ? " · " + c.sql : "") : "（选连接）";
        if (n.type === "file") return c.path || "（选文件）";
        if (n.type === "filter") return c.where ? "WHERE " + c.where : "（填条件）";
        if (n.type === "join") return (c.kind || "INNER") + (c.on ? " ON " + c.on : "（填 ON）");
        if (n.type === "aggregate") return (c.group ? "BY " + c.group + " · " : "") + (c.aggs || "（填聚合）");
        if (n.type === "sql") return c.sql || "（写 SQL）";
        if (n.type === "output") return (c.order_by ? "ORDER BY " + c.order_by + " " : "")
                                       + "LIMIT " + (c.limit || 1000);
        return "";
      },

      // ---------- 运行 / 分页 / 导出 ----------
      // data tab 的查询由 表 + WHERE 条 + 列头排序 构建（DataGrip Table Data 行为，走 SQL 跨页正确）
      buildDataSql: function (t) {
        var q = t.schema ? t.schema + "." + t.table : t.table;
        var sql = "SELECT * FROM " + q;
        if (t.where && t.where.trim()) sql += " WHERE " + t.where.trim();
        if (t.orderBy && t.orderBy.trim()) sql += " ORDER BY " + t.orderBy.trim();
        return sql;
      },
      // 光标处执行：编辑器多条语句时只跑光标所在那条；有选区则跑选区（DataGrip 行为）
      stmtAtCursor: function () {
        var text = this.currentSql();
        if (!editor) return text;
        var sel = editor.getSelection();
        if (sel && !sel.isEmpty()) return editor.getModel().getValueInRange(sel);
        var ranges = stmtRanges(text);
        if (ranges.length <= 1) return text;
        var off = editor.getModel().getOffsetAt(editor.getPosition());
        for (var i = 0; i < ranges.length; i++) {
          if (off <= ranges[i].e) return text.slice(ranges[i].s, ranges[i].e);
        }
        return text.slice(ranges[ranges.length - 1].s);
      },
      run: function (confirm, page, sqlOverride) {
        var self = this, t = this.activeTab;
        if (!t) return;
        if (t.type === "ddl") { this.flash("DDL 为只读视图"); return; }
        if (t.type === "flow" && sqlOverride == null) { this.runFlow(); return; }
        if (!t.conn) { this.flash("请先选择连接"); return; }
        var sql;
        if (sqlOverride != null) sql = sqlOverride;
        else if (confirm && t.pendingSql) sql = t.pendingSql;   // 确认执行的是刚才那条
        else if (t.type === "data" && t.table) sql = this.buildDataSql(t);
        else sql = this.stmtAtCursor();
        if (!sql.trim()) { this.flash("请输入 SQL"); return; }
        page = page || 0;
        t.pendingSql = sql;
        t.running = true; t.err = null; t.ok = null; t.confirm = null; t.explain = null; t.edit = null; t.wfSteps = null;
        t.rowSel = {}; t.lastSelRi = -1; t.resQ = null;  // 重查后行号会变，行选择/搜索作废
        if (page === 0) t.result = null;
        // 异步任务：查询在服务端线程池执行，切页/刷新不中断；job_id 持久化，回来续接轮询
        apiPost("/admin/sql/run_async", { conn: t.conn, sql: sql, confirm: confirm ? "1" : null,
                                          page: page, schema: t.schema || null })
          .then(function (d) {
            if (!d.ok) { t.running = false; t.err = d.error; self.persist(); return; }
            t.jobId = d.job_id; t.jobPage = page;
            self.persist();
            self.pollJob(t.id, d.job_id, page);
          }).catch(function (e) { t.running = false; t.err = "" + e; });
      },
      pollJob: function (tabId, jobId, page) {
        var self = this;
        var t = this.tabs.find(function (x) { return x.id === tabId; });
        if (!t || t.jobId !== jobId) return;  // tab 已关/已发起新查询
        apiGet("/admin/sql/job?id=" + jobId).then(function (d) {
          var t2 = self.tabs.find(function (x) { return x.id === tabId; });
          if (!t2 || t2.jobId !== jobId) return;
          if (!d.ok) { t2.running = false; t2.jobId = null; t2.err = d.error || "任务丢失"; self.persist(); return; }
          if (d.status === "running") {
            t2.running = true;
            setTimeout(function () { self.pollJob(tabId, jobId, page); }, 450);
            return;
          }
          t2.running = false; t2.jobId = null;
          if (d.status === "error") { t2.err = d.error; self.persist(); return; }
          var r = d.result || {};
          if (r.kind === "workflow") {
            // workflow 运行结果：步骤清单 + 输出预览（输出复用结果表格）
            t2.wfSteps = r.steps || [];
            if (r.output) t2.result = Object.assign({ paginated: false }, r.output);
            else if (!r.ok) t2.err = (r.steps.filter(function (x) { return !x.ok; })[0] || {}).error || "运行失败";
            if (t2.type === "flow") {  // 画布：按 steps 里的 node id 给节点标 ✓/✗
              t2.nodeStatus = {};
              (r.steps || []).forEach(function (s) {
                if (s.node) t2.nodeStatus[s.node] = s.ok ? "ok" : "err";
              });
            }
            self.flash(r.ok ? "workflow 运行完成" : "workflow 运行失败（见步骤）");
          }
          else if (r.kind === "read") { t2.result = r; t2.readSql = t2.pendingSql; t2.lastPage = page; }
          else if (r.kind === "confirm") t2.confirm = { risk: r.risk || {}, statement_kind: r.statement_kind };
          else if (r.kind === "write") {
            t2.ok = r;
            if (t2.type === "data") setTimeout(function () { self.run(false, t2.lastPage); }, 60);
            else self.refreshTree();
          }
          self.persist();
          if (t2.id === self.activeId && t2.view === "chart" && t2.result) self.renderChart();
        }).catch(function () {  // 网络抖动：稍后重试
          setTimeout(function () { self.pollJob(tabId, jobId, page); }, 1200);
        });
      },
      goPage: function (p) {
        var t = this.activeTab;
        if (p < 0 || !t) return;
        // 翻页沿用上次执行的读语句（光标可能已移动到别的语句上）
        this.run(false, p, t.type === "data" ? null : t.readSql);
      },
      confirmRun: function () { if (this.activeTab) this.activeTab.confirm = null; this.run(true); },
      // data tab：WHERE 条应用 / 列头点击循环排序（走 SQL 重查第 0 页）
      applyWhere: function () { this.run(false, 0); },
      // 列头点击与 ORDER BY 输入框联动：点头写入表达式，手写表达式亦可（支持多列/函数）
      cycleOrder: function (col) {
        var t = this.activeTab;
        if (!t || t.type !== "data") return;
        if (t.orderBy === col + " ASC") t.orderBy = col + " DESC";
        else if (t.orderBy === col + " DESC") t.orderBy = "";
        else t.orderBy = col + " ASC";
        this.run(false, 0);
      },
      orderMark: function (col) {
        var t = this.activeTab;
        if (!t || !t.orderBy) return "";
        if (t.orderBy === col + " ASC") return " ↑";
        if (t.orderBy === col + " DESC") return " ↓";
        return "";
      },
      funnel: function (col) {
        var t = this.activeTab; if (!t) return;
        t.where = (t.where && t.where.trim()) ? (t.where.trim() + " AND " + col + " = ") : (col + " = ");
        var self = this;
        this.$nextTick(function () {
          var el = document.getElementById("dg-where-input");
          if (el) { el.focus(); el.setSelectionRange(el.value.length, el.value.length); }
        });
      },

      // ---------- 单元格就地编辑（data tab，经写确认流生成 UPDATE） ----------
      sqlLit: function (v, original) {
        if (v === "NULL") return "NULL";
        if (typeof original === "number" && v !== "" && !isNaN(+v)) return "" + (+v);
        return "'" + String(v).replace(/'/g, "''") + "'";
      },
      startEdit: function (ri, ci) {
        var t = this.activeTab;
        if (!t || t.type !== "data" || !t.result) return;
        var k = this.mk(t.table, t.schema);
        if (!this.tableMeta[k]) this.fetchMeta(t.table, t.schema);  // 预取主键
        var v = t.result.rows[ri][ci];
        t.edit = { ri: ri, ci: ci, val: v == null ? "NULL" : this.cellText(v) };
        this.$nextTick(function () {
          var el = document.getElementById("dg-cell-input");
          if (el) { el.focus(); el.select(); }
        });
      },
      cancelEdit: function () { if (this.activeTab) this.activeTab.edit = null; },
      // 生成按主键定位的单单元格 UPDATE；失败返回 null（原因已 flash）
      makeUpdateSql: function (t, ri, ci, newRaw) {
        var k = this.mk(t.table, t.schema);
        var meta = this.tableMeta[k];
        if (!meta) { this.fetchMeta(t.table, t.schema); this.flash("表结构加载中，请稍后再试"); return null; }
        var pk = meta.primary_key || [];
        if (!pk.length) { this.flash("该表无主键，无法定位行进行编辑"); return null; }
        var cols = t.result.columns, row = t.result.rows[ri];
        var self = this;
        var conds = pk.map(function (p) {
          var idx = cols.indexOf(p);
          if (idx < 0) return null;
          var pv = row[idx];
          return p + (pv == null ? " IS NULL" : " = " + self.sqlLit(self.cellText(pv), pv));
        });
        if (conds.some(function (c) { return c == null; })) {
          this.flash("结果集缺少主键列，无法定位行"); return null;
        }
        var q = t.schema ? t.schema + "." + t.table : t.table;
        return "UPDATE " + q + " SET " + cols[ci] + " = " + this.sqlLit(newRaw, row[ci]) +
               " WHERE " + conds.join(" AND ");
      },
      commitEdit: function () {
        var t = this.activeTab;
        if (!t || !t.edit || !t.result) return;
        var oldV = t.result.rows[t.edit.ri][t.edit.ci];
        var newRaw = t.edit.val;
        if (newRaw === (oldV == null ? "NULL" : this.cellText(oldV))) { t.edit = null; return; }
        var sql = this.makeUpdateSql(t, t.edit.ri, t.edit.ci, newRaw);
        t.edit = null;
        if (sql) this.run(false, 0, sql);  // 写确认流：风险报告 → 确认 → writer 执行 → 自动刷新
      },

      // ---------- SQL 实时语法检查（sqlglot 按方言，防抖 600ms → Monaco 红波浪） ----------
      scheduleLint: function () {
        var self = this;
        clearTimeout(this._lt);
        this._lt = setTimeout(function () { self.runLint(); }, 600);
      },
      runLint: function () {
        if (!editor || !window.monaco) return;
        var model = editor.getModel();
        if (!model) return;
        var t = this.activeTab;
        var clear = function () { window.monaco.editor.setModelMarkers(model, "dbm", []); };
        if (!t || t.type !== "query" && t.type !== "flow") { clear(); return; }
        var dialect = this.isAnalysis ? "duckdb" : (this.connMeta ? this.connMeta.engine : "");
        if (["mysql", "postgres", "sqlite", "duckdb"].indexOf(dialect) < 0) { clear(); return; }
        var sql = model.getValue();
        if (!sql.trim()) { clear(); return; }
        apiPost("/admin/sql/lint", { sql: sql, dialect: dialect }).then(function (d) {
          if (!d.ok || editor.getModel() !== model) return;
          var markers = (d.errors || []).map(function (e) {
            var line = Math.min(e.line || 1, model.getLineCount());
            var col = Math.max(1, e.col || 1);
            return { startLineNumber: line, startColumn: col, endLineNumber: line,
                     endColumn: Math.max(col + 1, model.getLineMaxColumn(line)),
                     message: e.message, severity: window.monaco.MarkerSeverity.Error };
          });
          window.monaco.editor.setModelMarkers(model, "dbm", markers);
        }).catch(function () { /* lint 失败静默，不影响编辑 */ });
      },

      // ---------- 全局表名搜索（⌘P，跨库 LIKE，回车打开表数据） ----------
      openTblSearch: function () {
        var t = this.activeTab;
        if (!t || !t.conn || this.isAnalysis) { this.flash("先选择一个数据库连接"); return; }
        this.tblSearch = { q: "", results: [], sel: 0, loading: false };
        this.$nextTick(function () {
          var el = document.getElementById("dg-tblsearch-input");
          if (el) el.focus();
        });
      },
      tblSearchInput: function () {
        var self = this, s = this.tblSearch;
        if (!s) return;
        clearTimeout(this._tst);
        if (!s.q.trim()) { s.results = []; return; }
        this._tst = setTimeout(function () {
          s.loading = true;
          apiGet("/admin/sql/search_tables?conn=" + encodeURIComponent(self.activeTab.conn)
                 + "&q=" + encodeURIComponent(s.q))
            .then(function (d) {
              if (self.tblSearch !== s) return;  // 已关闭/重开
              s.loading = false;
              s.results = d.ok ? d.results : [];
              s.sel = 0;
            }).catch(function () { s.loading = false; });
        }, 300);
      },
      tblSearchKey: function (e) {
        var s = this.tblSearch;
        if (!s) return;
        if (e.key === "ArrowDown") { e.preventDefault(); s.sel = Math.min(s.sel + 1, s.results.length - 1); }
        else if (e.key === "ArrowUp") { e.preventDefault(); s.sel = Math.max(s.sel - 1, 0); }
        else if (e.key === "Enter" && s.results[s.sel]) this.tblSearchGo(s.results[s.sel]);
        else if (e.key === "Escape") this.tblSearch = null;
      },
      tblSearchGo: function (item) {
        this.tblSearch = null;
        this.openTableTab(item.table, item.db || "");
      },

      // ---------- 数据导入（CSV/粘贴 → 参数化 INSERT，writer 单事务） ----------
      openImport: function (t, db) {
        this.importRows = { t: t, db: db || "", text: "", header: true, parsed: null, running: false };
        var k = this.mk(t, db);
        if (!this.tableMeta[k]) this.fetchMeta(t, db);  // 列映射需要表结构
      },
      importFile: function (ev) {
        var self = this, f = ev.target.files && ev.target.files[0];
        if (!f) return;
        var r = new FileReader();
        r.onload = function () { self.importRows.text = r.result; self.importParse(); };
        r.readAsText(f);
        ev.target.value = "";
      },
      importParse: function () {
        var p = this.importRows;
        if (!p) return;
        var all = parseDelimited(p.text);
        if (!all || !all.length) { this.flash("没有可解析的数据"); return; }
        var meta = this.tableMeta[this.mk(p.t, p.db)];
        if (!meta) { this.fetchMeta(p.t, p.db); this.flash("表结构加载中，请稍后再点解析"); return; }
        var tableCols = meta.columns.map(function (c) { return c.name; });
        var header = p.header ? all[0] : null;
        var dataRows = p.header ? all.slice(1) : all;
        var mapping;
        if (header) {  // 按名匹配（大小写不敏感），未匹配列忽略
          var lower = {};
          tableCols.forEach(function (c) { lower[c.toLowerCase()] = c; });
          mapping = header.map(function (h) { return lower[(h || "").trim().toLowerCase()] || null; });
        } else {       // 无表头：按表列顺序对位
          mapping = (all[0] || []).map(function (_v, i) { return tableCols[i] || null; });
        }
        var cols = [], idxs = [];
        mapping.forEach(function (m, i) { if (m) { cols.push(m); idxs.push(i); } });
        if (!cols.length) { this.flash("没有任何列能对应到表 " + p.t + " 的字段"); return; }
        var rows = dataRows
          .filter(function (r) { return r.some(function (v) { return v !== ""; }); })
          .map(function (r) {
            return idxs.map(function (i) { var v = r[i]; return v === "" || v === undefined ? null : v; });
          });
        if (!rows.length) { this.flash("解析后没有数据行"); return; }
        p.parsed = { columns: cols, rows: rows, skipped: mapping.filter(function (m) { return !m; }).length };
      },
      confirmImportRows: function () {
        var self = this, p = this.importRows;
        if (!p || !p.parsed || p.running) return;
        p.running = true;
        var tab = this.activeTab;
        apiPost("/admin/sql/import", {
          conn: tab.conn, table: p.t, schema: p.db || null,
          columns: JSON.stringify(p.parsed.columns), rows: JSON.stringify(p.parsed.rows),
        }).then(function (d) {
          p.running = false;
          if (!d.ok) { self.flash(d.error); return; }
          self.importRows = null;
          self.flash("已导入 " + d.inserted + " 行 → " + p.t + "（" + d.duration_ms + " ms）");
          var t = self.activeTab;
          if (t && t.type === "data" && t.table === p.t) self.run(false, t.lastPage);
        }).catch(function (e) { p.running = false; self.flash("" + e); });
      },

      // ---------- 行选择 / 行级 CRUD / 复制 / 网格内搜索（DataGrip data editor 行为） ----------
      selRowCount: function () {
        var t = this.activeTab;
        return t && t.rowSel ? Object.keys(t.rowSel).length : 0;
      },
      rowClick: function (ri, ev) {
        var t = this.activeTab;
        if (!t || !t.result) return;
        if (!t.rowSel) t.rowSel = {};
        if (ev.shiftKey && t.lastSelRi >= 0) {
          var a = Math.min(t.lastSelRi, ri), b = Math.max(t.lastSelRi, ri);
          for (var i = a; i <= b; i++) t.rowSel[i] = true;
        } else if (ev.metaKey || ev.ctrlKey) {
          if (t.rowSel[ri]) delete t.rowSel[ri]; else t.rowSel[ri] = true;
          t.lastSelRi = ri;
        } else {
          var only = t.rowSel[ri] && Object.keys(t.rowSel).length === 1;
          t.rowSel = {};
          if (!only) t.rowSel[ri] = true;
          t.lastSelRi = ri;
        }
      },
      clearRowSel: function () {
        var t = this.activeTab;
        if (t) { t.rowSel = {}; t.lastSelRi = -1; }
      },
      selRis: function () {
        var t = this.activeTab;
        return Object.keys(t.rowSel || {}).map(Number).sort(function (a, b) { return a - b; });
      },
      // 新增行：网格顶部出现编辑行；留空 = 用列默认值，填 NULL = 显式 NULL
      startNewRow: function (cloneRi) {
        var t = this.activeTab;
        if (!t || t.type !== "data" || !t.result) return;
        var k = this.mk(t.table, t.schema);
        if (!this.tableMeta[k]) this.fetchMeta(t.table, t.schema);
        var self = this;
        var vals = t.result.columns.map(function (c, ci) {
          if (cloneRi == null) return "";
          var meta = self.tableMeta[k];
          if (meta && (meta.primary_key || []).indexOf(c) >= 0) return "";  // 主键留给自增/默认
          var v = t.result.rows[cloneRi][ci];
          return v == null ? "NULL" : self.cellText(v);
        });
        t.newRow = { values: vals };
        this.$nextTick(function () {
          var el = document.querySelector(".dg-newrow input");
          if (el) el.focus();
        });
      },
      commitNewRow: function () {
        var t = this.activeTab;
        if (!t || !t.newRow) return;
        var cols = [], vals = [];
        for (var i = 0; i < t.result.columns.length; i++) {
          var raw = t.newRow.values[i];
          if (raw === "") continue;               // 留空 → 列默认值
          cols.push(t.result.columns[i]);
          vals.push(this.sqlLit(raw, null));      // "NULL" → NULL，其余按字面量
        }
        if (!cols.length) { this.flash("至少填写一列（留空 = 用默认值）"); return; }
        var q = t.schema ? t.schema + "." + t.table : t.table;
        var sql = "INSERT INTO " + q + " (" + cols.join(", ") + ") VALUES (" + vals.join(", ") + ")";
        t.newRow = null;
        this.run(false, 0, sql);   // 写确认流 → writer 执行 → 自动刷新
      },
      deleteSelRows: function () {
        var t = this.activeTab;
        if (!t || t.type !== "data" || !t.result) return;
        var ris = this.selRis();
        if (!ris.length) return;
        var k = this.mk(t.table, t.schema);
        var meta = this.tableMeta[k];
        if (!meta) { this.fetchMeta(t.table, t.schema); this.flash("表结构加载中，请稍后再试"); return; }
        var pk = meta.primary_key || [];
        if (!pk.length) { this.flash("该表无主键，无法定位行删除"); return; }
        var cols = t.result.columns, self = this;
        var pkIdx = pk.map(function (p) { return cols.indexOf(p); });
        if (pkIdx.some(function (i) { return i < 0; })) { this.flash("结果集缺少主键列"); return; }
        var q = t.schema ? t.schema + "." + t.table : t.table;
        var where;
        if (pk.length === 1) {
          var vs = ris.map(function (ri) {
            var v = t.result.rows[ri][pkIdx[0]];
            return self.sqlLit(self.cellText(v), v);
          });
          where = pk[0] + " IN (" + vs.join(", ") + ")";
        } else {
          where = ris.map(function (ri) {
            return "(" + pk.map(function (p, j) {
              var v = t.result.rows[ri][pkIdx[j]];
              return p + (v == null ? " IS NULL" : " = " + self.sqlLit(self.cellText(v), v));
            }).join(" AND ") + ")";
          }).join(" OR ");
        }
        this.clearRowSel();
        this.run(false, 0, "DELETE FROM " + q + " WHERE " + where);  // 走写确认流
      },
      // 选中行复制为各种格式（无选中 = 当前页全部）
      copyRows: function (fmt) {
        var t = this.activeTab;
        if (!t || !t.result) return;
        var ris = this.selRis();
        if (!ris.length) ris = t.result.rows.map(function (_r, i) { return i; });
        var cols = t.result.columns, self = this;
        var rows = ris.map(function (ri) { return t.result.rows[ri]; });
        var text;
        if (fmt === "tsv") {
          text = [cols.join("\t")].concat(rows.map(function (r) {
            return r.map(function (v) { return v == null ? "" : self.cellText(v).replace(/[\t\n]/g, " "); }).join("\t");
          })).join("\n");
        } else if (fmt === "markdown") {
          text = ["| " + cols.join(" | ") + " |", "|" + cols.map(function () { return " --- "; }).join("|") + "|"]
            .concat(rows.map(function (r) {
              return "| " + r.map(function (v) { return v == null ? "" : self.cellText(v).replace(/\|/g, "\\|"); }).join(" | ") + " |";
            })).join("\n");
        } else if (fmt === "json") {
          text = JSON.stringify(rows.map(function (r) {
            var o = {};
            cols.forEach(function (c, i) { o[c] = r[i]; });
            return o;
          }), null, 2);
        } else {  // insert
          var q = t.type === "data" ? (t.schema ? t.schema + "." + t.table : t.table) : "your_table";
          text = rows.map(function (r) {
            var vals = r.map(function (v) {
              if (v == null) return "NULL";
              if (typeof v === "number") return "" + v;
              return "'" + self.cellText(v).replace(/'/g, "''") + "'";
            });
            return "INSERT INTO " + q + " (" + cols.join(", ") + ") VALUES (" + vals.join(", ") + ");";
          }).join("\n");
        }
        var n = ris.length;
        navigator.clipboard.writeText(text).then(function () {
          self.flash("已复制 " + n + " 行（" + fmt.toUpperCase() + "）");
        }, function () { self.flash("复制失败（剪贴板权限）"); });
        this.copyOpen = false;
      },
      // 网格内文本搜索（⌘F）：高亮命中单元格，Enter 下一个
      openResQ: function () {
        var t = this.activeTab;
        if (!t || !t.result || !t.result.columns.length) return;
        t.resQ = { q: "", hits: [], cur: -1 };
        this.$nextTick(function () {
          var el = document.getElementById("dg-resq-input");
          if (el) el.focus();
        });
      },
      closeResQ: function () { if (this.activeTab) this.activeTab.resQ = null; },
      resQInput: function () {
        var t = this.activeTab;
        if (!t || !t.resQ) return;
        var q = t.resQ.q.toLowerCase(), hits = [], self = this;
        if (q) {
          t.result.rows.forEach(function (r, ri) {
            r.forEach(function (v, ci) {
              if (v != null && self.cellText(v).toLowerCase().indexOf(q) >= 0) hits.push(ri + ":" + ci);
            });
          });
        }
        t.resQ.hits = hits;
        t.resQ.cur = hits.length ? 0 : -1;
        this._hitSet = {};
        hits.forEach(function (h) { self._hitSet[h] = 1; });
        if (hits.length) this.scrollToHit();
      },
      isHit: function (ri, ci) {
        var t = this.activeTab;
        return !!(t && t.resQ && this._hitSet && this._hitSet[ri + ":" + ci]);
      },
      isCurHit: function (ri, ci) {
        var t = this.activeTab;
        return !!(t && t.resQ && t.resQ.cur >= 0 && t.resQ.hits[t.resQ.cur] === ri + ":" + ci);
      },
      resQNext: function (dir) {
        var t = this.activeTab;
        if (!t || !t.resQ || !t.resQ.hits.length) return;
        var n = t.resQ.hits.length;
        t.resQ.cur = ((t.resQ.cur + dir) % n + n) % n;
        this.scrollToHit();
      },
      scrollToHit: function () {
        var t = this.activeTab;
        var h = t.resQ.hits[t.resQ.cur];
        if (!h) return;
        this.$nextTick(function () {
          var el = document.querySelector('td[data-cell="' + h + '"]');
          if (el) el.scrollIntoView({ block: "center", inline: "nearest" });
        });
      },

      // ---------- Value Editor（DataGrip 式侧栏编辑面板，选中单元格展开） ----------
      cellClick: function (ri, ci) {
        var t = this.activeTab;
        if (!t || t.type !== "data" || !t.result) return;
        t.vsel = { ri: ri, ci: ci };
        var v = t.result.rows[ri][ci];
        this.vpVal = v == null ? "" : this.cellText(v);
        this.vpNull = v == null;
        this.vpOpen = true; this.vpTab = this.vpTab || "value";
        var k = this.mk(t.table, t.schema);
        if (!this.tableMeta[k]) this.fetchMeta(t.table, t.schema);  // 预取主键
      },
      vpDirty: function () {
        var t = this.activeTab;
        if (!t || !t.vsel || !t.result) return false;
        var v = t.result.rows[t.vsel.ri][t.vsel.ci];
        if (this.vpNull) return v != null;
        return this.vpVal !== (v == null ? "" : this.cellText(v));
      },
      vpSave: function () {
        var t = this.activeTab;
        if (!t || !t.vsel) return;
        if (!this.vpDirty()) { this.flash("值未变化"); return; }
        var newRaw = this.vpNull ? "NULL" : this.vpVal;
        var sql = this.makeUpdateSql(t, t.vsel.ri, t.vsel.ci, newRaw);
        if (sql) this.run(false, 0, sql);  // 走写确认流（二次确认）
      },
      // Value 面板格式化：JSON 美化 / 压缩（非 JSON 给提示不破坏原值）
      vpFormat: function (pretty) {
        try {
          var obj = JSON.parse(this.vpVal);
          this.vpVal = pretty ? JSON.stringify(obj, null, 2) : JSON.stringify(obj);
        } catch (e) { this.flash("当前值不是合法 JSON"); }
      },
      vpRecord: function () {
        var t = this.activeTab;
        if (!t || !t.vsel || !t.result) return [];
        var row = t.result.rows[t.vsel.ri], self = this;
        return t.result.columns.map(function (c, i) {
          return { col: c, val: row[i] == null ? null : self.cellText(row[i]), cur: i === t.vsel.ci };
        });
      },

      // ---------- EXPLAIN 可视化 ----------
      explainStmt: function () {
        var self = this, t = this.activeTab;
        if (!t || !t.conn) { this.flash("请先选择连接"); return; }
        var sql = t.type === "data" ? this.buildDataSql(t) : this.stmtAtCursor();
        if (!sql.trim()) { this.flash("请输入 SQL"); return; }
        t.explain = { loading: true };
        apiPost("/admin/sql/explain", { conn: t.conn, sql: sql, schema: t.schema || null })
          .then(function (d) {
            if (!d.ok) { t.explain = null; self.flash(d.error); return; }
            if (d.format === "json") {
              try { t.explain = { tree: JSON.parse(d.rows[0][0]) }; }
              catch (e) { t.explain = { rows: d.rows, columns: d.columns }; }
            } else t.explain = { rows: d.rows, columns: d.columns };
            self.persist();
          }).catch(function (e) { t.explain = null; self.flash("" + e); });
      },
      closeExplain: function () { if (this.activeTab) { this.activeTab.explain = null; this.persist(); } },

      // ---------- 查询历史（来自审计，按连接去重） ----------
      loadHistory: function () {
        var self = this, t = this.activeTab;
        if (!t || !t.conn) { this.history = []; return; }
        apiGet("/admin/sql/history?conn=" + encodeURIComponent(t.conn)).then(function (d) {
          self.history = d && d.ok ? d.items : [];
        });
      },
      openHistory: function (item) {
        this.newTab({ title: item.sql.slice(0, 18), sql: item.sql });
      },
      cancelConfirm: function () { if (this.activeTab) this.activeTab.confirm = null; },
      formatSql: function () {
        var t = this.activeTab; if (!t || t.type === "ddl") return;
        apiPost("/admin/sql/format", { conn: t.conn, sql: this.currentSql() }).then(function (d) {
          if (d.ok && d.sql != null) { var m = models.get(t.id); if (m) m.setValue(d.sql); }
        });
      },
      exportAs: function (fmt) {
        this.exportOpen = false;
        var self = this, t = this.activeTab; if (!t) return;
        var fd = new FormData(); fd.append("conn", t.conn); fd.append("sql", this.currentSql()); fd.append("format", fmt);
        if (t.schema) fd.append("schema", t.schema);
        fetch("/admin/sql/export", { method: "POST", body: fd }).then(function (r) {
          if (!r.ok) return r.json().then(function (d) { throw new Error(d.error || "导出失败"); });
          var dispo = r.headers.get("Content-Disposition") || "";
          var mm = /filename="?([^"]+)"?/.exec(dispo);
          var name = mm ? mm[1] : "export." + fmt;
          return r.blob().then(function (b) { download(b, name); self.flash("已导出 " + name); });
        }).catch(function (e) { self.flash("" + (e.message || e)); });
      },
      saveSqlFile: function () {
        var t = this.activeTab; if (!t) return;
        var name = (t.title || "query").replace(/[^\w一-龥.-]+/g, "_");
        if (!/\.sql$/i.test(name)) name += ".sql";
        download(new Blob([this.currentSql()], { type: "application/sql" }), name);
      },

      // ---------- 片段 ----------
      loadSnippets: function () {
        var self = this;
        return apiGet("/admin/sql/snippets").then(function (d) { self.snippets = (d && d.snippets) || []; });
      },
      toggleSnipForm: function () {
        this.showSnipForm = !this.showSnipForm;
        if (this.showSnipForm && this.activeTab) this.snipDraft = { title: this.activeTab.title, note: "" };
      },
      saveSnippet: function () {
        var self = this, t = this.activeTab; if (!t) return;
        if (!this.snipDraft.title.trim()) { this.flash("请填写标题"); return; }
        apiPost("/admin/sql/snippets/save", {
          title: this.snipDraft.title, note: this.snipDraft.note, sql: this.currentSql(), connection: t.conn
        }).then(function (d) {
          if (!d.ok) { self.flash(d.error); return; }
          self.showSnipForm = false; self.flash("已保存片段"); self.loadSnippets();
        });
      },
      openSnippet: function (s) {
        var conn = this.connections.some(function (c) { return c.value === s.connection; })
          ? s.connection : (this.activeTab ? this.activeTab.conn : "");
        this.newTab({ title: s.title, sql: s.sql, conn: conn });
      },
      askDeleteSnippet: function (id) {
        if (this.delSnip !== id) {
          this.delSnip = id; this.flash("再点一次确认删除");
          var self = this; setTimeout(function () { if (self.delSnip === id) self.delSnip = null; }, 3000);
          return;
        }
        this.delSnip = null;
        var self2 = this;
        apiPost("/admin/sql/snippets/delete", { id: id }).then(function (d) {
          if (!d.ok) { self2.flash(d.error); return; } self2.flash("已删除"); self2.loadSnippets();
        });
      },

      // ---------- 持久化（全状态保活，localStorage：关页面/关浏览器均保留） ----------
      persist: function () {
        try {
          this.stashTree();
          var tabs = this.tabs.map(function (t) {
            return { id: t.id, title: t.title, conn: t.conn, schema: t.schema || "",
                     type: t.type || "query", table: t.table || "", sql: this.sqlOf(t),
                     result: t.result, ok: t.ok, err: t.err, pinned: !!t.pinned,
                     where: t.where || "", orderBy: t.orderBy || "",
                     lastPage: t.lastPage || 0, readSql: t.readSql, explain: t.explain,
                     jobId: t.jobId || null, jobPage: t.jobPage || 0, pendingSql: t.pendingSql,
                     wfName: t.wfName || "", wfSteps: t.wfSteps || null,
                     view: t.view || "table", chart: t.chart || null,
                     graph: t.graph || null };
          }, this);
          var data = { v: 2, tabs: tabs, activeId: this.activeId, treeCache: this.treeCache,
                       leftW: this.leftW, editorH: this.editorH,
                       schemaShow: this.schemaShow, schemaDefault: this.schemaDefault };
          var s = JSON.stringify(data);
          if (s.length > 3800000) {  // localStorage 上限兜底：丢结果、保 SQL 与树
            tabs.forEach(function (t) { t.result = null; });
            s = JSON.stringify(data);
          }
          localStorage.setItem(STORE_KEY, s);
        } catch (e) { /* 存储失败不影响使用 */ }
      },
      restore: function () {
        try {
          var raw = localStorage.getItem(STORE_KEY);
          if (!raw) return;
          var d = JSON.parse(raw);
          if (!d.tabs || !d.tabs.length) return;
          this.tabs = d.tabs.map(function (t) {
            return { id: t.id, title: t.title, conn: t.conn, schema: t.schema || "",
                     type: t.type || "query", table: t.table || "", sql: t.sql || "",
                     result: t.result || null, ok: t.ok || null, err: t.err || null,
                     pinned: !!t.pinned,
                     where: t.where || "",
                     orderBy: t.orderBy || (t.orderCol ? t.orderCol + " " + (t.orderDir || "ASC") : ""),
                     lastPage: t.lastPage || 0, readSql: t.readSql || null, explain: t.explain || null,
                     jobId: t.jobId || null, jobPage: t.jobPage || 0,
                     pendingSql: t.pendingSql || null, edit: null, confirm: null,
                     wfName: t.wfName || "", wfSteps: t.wfSteps || null, vsel: null,
                     view: t.view || "table", chart: t.chart || null,
                     graph: t.graph || null, sel: null, nodeStatus: {},
                     rowSel: {}, lastSelRi: -1, newRow: null, resQ: null,
                     running: !!t.jobId };  // 有未完成任务 → 恢复后续接轮询
          });
          this.activeId = d.activeId || this.tabs[0].id;
          this.treeCache = d.treeCache || {};
          if (d.leftW) this.leftW = d.leftW;
          if (d.editorH) this.editorH = d.editorH;
          this.schemaShow = d.schemaShow || {};
          this.schemaDefault = d.schemaDefault || {};
          seq = Math.max.apply(null, this.tabs.map(function (t) { return t.id; })) + 1;
        } catch (e) { /* 损坏则从空开始 */ }
      },

      // ---------- 拖动分隔条 ----------
      beginDrag: function (e, axis) {
        var self = this, start = axis === "x" ? e.clientX : e.clientY;
        var base = axis === "x" ? this.leftW : this.editorH;
        e.preventDefault();
        function move(ev) {
          var delta = (axis === "x" ? ev.clientX : ev.clientY) - start;
          if (axis === "x") self.leftW = Math.max(180, Math.min(560, base + delta));
          else self.editorH = Math.max(100, Math.min(window.innerHeight - 160, base + delta));
        }
        function up() {
          window.removeEventListener("mousemove", move);
          window.removeEventListener("mouseup", up);
          document.body.style.userSelect = "";
          self.persist();
          if (chartInst) chartInst.resize();
        }
        window.addEventListener("mousemove", move);
        window.addEventListener("mouseup", up);
        document.body.style.userSelect = "none";
      },

      // ---------- Monaco ----------
      initEditor: function () {
        var self = this, monaco = window.monaco;
        this.tabs.forEach(function (t) {
          if (!models.has(t.id)) models.set(t.id, monaco.editor.createModel(t.sql || "", "sql"));
        });
        var active = models.get(this.activeId) || (this.tabs[0] && models.get(this.tabs[0].id)) || null;
        editor = monaco.editor.create(this.$refs.editorEl, {
          model: active, language: "sql", theme: "vs-dark", automaticLayout: true,
          fontSize: 13, minimap: { enabled: true }, scrollBeyondLastLine: false, tabSize: 2,
          fontFamily: "'JetBrains Mono', ui-monospace, Menlo, Consolas, monospace",
          renderWhitespace: "selection",
          readOnly: !!this.activeTab && this.activeTab.type === "ddl",
        });
        editor.addCommand(monaco.KeyMod.CtrlCmd | monaco.KeyCode.Enter, function () { self.run(false); });
        editor.onDidBlurEditorText(function () { self.persist(); });
        editor.onDidChangeModelContent(function () { self.scheduleLint(); });  // 实时语法检查
        this.scheduleLint();
        // 上下文感知补全（DataGrip 式）：
        // - `库.` → 该库的表；`表./别名.` → 列
        // - FROM/JOIN/UPDATE/INTO 后 → 表；SELECT/WHERE/ON/BY/SET 等后 → 当前语句各表的列
        //   （向后看整条语句找 FROM 表，SELECT 位置也能补列）+ 函数
        // - 空格触发自动弹（仅在有明确上下文时），语句开头给关键字
        monaco.languages.registerCompletionItemProvider("sql", {
          triggerCharacters: [".", " "],
          provideCompletionItems: function (model, position, ctx) {
            var Kind = monaco.languages.CompletionItemKind;
            var w = model.getWordUntilPosition(position);
            var range = { startLineNumber: position.lineNumber, endLineNumber: position.lineNumber,
                          startColumn: w.startColumn, endColumn: w.endColumn };
            var line = model.getValueInRange({ startLineNumber: position.lineNumber, startColumn: 1,
                                               endLineNumber: position.lineNumber, endColumn: position.column });
            function kw(items, sort) {
              return items.map(function (k) {
                return { label: k, kind: Kind.Keyword, insertText: k, range: range,
                         sortText: (sort || "3") + k };
              });
            }
            function fns(sort) {
              return SQL_FUNCS.map(function (f) {
                return { label: f + "(…)", kind: Kind.Function, insertText: f + "($0)",
                         insertTextRules: monaco.languages.CompletionItemInsertTextRule.InsertAsSnippet,
                         filterText: f, range: range, sortText: (sort || "1") + f };
              });
            }
            function tbls(sort) {
              return currentTables.map(function (t) {
                return { label: t, kind: Kind.Struct, insertText: t, range: range,
                         detail: "表", sortText: (sort || "2") + t };
              });
            }

            // 1) `ident.`：库 → 该库的表；表/别名 → 列
            var dot = /([A-Za-z_][\w$]*)\.\s*[\w$]*$/.exec(line);
            if (dot) {
              var ident = dot[1];
              var dbHit = self.databases.filter(function (d) {
                return d.toLowerCase() === ident.toLowerCase(); })[0];
              if (dbHit) {
                var cachedT = self.tablesByDb[dbHit];
                var p = cachedT ? Promise.resolve(cachedT)
                  : fetch("/admin/sql/tables?conn=" + encodeURIComponent(currentConn)
                          + "&schema=" + encodeURIComponent(dbHit))
                      .then(function (r) { return r.json(); })
                      .then(function (d) {
                        if (d.ok) self.tablesByDb[dbHit] = d.tables || [];
                        return d.ok ? d.tables : [];
                      }).catch(function () { return []; });
                return p.then(function (names) {
                  return { suggestions: (names || []).map(function (t) {
                    return { label: t, kind: Kind.Struct, insertText: t, range: range,
                             detail: dbHit + " 的表", sortText: "0" + t };
                  }) };
                });
              }
              var table = resolveTable(model.getValue(), ident);
              return fetchCols(table).then(function (cols) {
                return { suggestions: cols.map(function (c) {
                  return { label: c.name, kind: Kind.Field, insertText: c.name, range: range,
                           detail: (c.type || "列"), sortText: "0" + c.name };
                }) };
              });
            }

            // 2) 位置感知：光标所在语句 + 光标前最近关键字
            var text = model.getValue();
            var off = model.getOffsetAt(position);
            var ranges = stmtRanges(text), stmt = text, stmtStart = 0;
            for (var i = 0; i < ranges.length; i++) {
              if (off <= ranges[i].e) { stmt = text.slice(ranges[i].s, ranges[i].e); stmtStart = ranges[i].s; break; }
            }
            var before = text.slice(stmtStart, off);
            var last = lastKeyword(before);
            var spaceTrig = ctx && ctx.triggerCharacter === " ";

            if (last && KW_TABLE_CTX[last]) {           // FROM/JOIN/UPDATE/INTO → 表优先
              return { suggestions: tbls("0").concat(kw(["SELECT"], "9")) };
            }
            if (last) {                                  // SELECT/WHERE/ON/BY/SET… → 语句内各表的列
              var tabs = stmtTables(stmt);
              if (tabs.length) {
                return Promise.all(tabs.map(function (t) { return fetchCols(t.table); }))
                  .then(function (colsets) {
                    var sug = [];
                    colsets.forEach(function (cols, ti) {
                      cols.forEach(function (c) {
                        sug.push({ label: c.name, kind: Kind.Field, insertText: c.name, range: range,
                                   detail: tabs[ti].table + " · " + (c.type || ""),
                                   sortText: "0" + c.name });
                      });
                    });
                    return { suggestions: sug.concat(fns("1")).concat(tbls("2"))
                               .concat(kw(SQL_KEYWORDS, "3")) };
                  });
              }
              return { suggestions: fns("1").concat(tbls("2")).concat(kw(SQL_KEYWORDS, "3")) };
            }
            // 语句开头/无上下文：仅打字时给关键字，空格触发不骚扰
            if (spaceTrig) return { suggestions: [] };
            return { suggestions: kw(["SELECT", "INSERT INTO", "UPDATE", "DELETE FROM", "SHOW",
                                      "EXPLAIN", "WITH", "CREATE TABLE", "ALTER TABLE"], "0")
                       .concat(tbls("1")) };
          }
        });
        this.editorReady = true;
      },
    },
    mounted: function () {
      var self = this;
      this.restore();
      // 切页/刷新前发起的查询在服务端继续跑：凭持久化的 job_id 续接轮询
      this.tabs.forEach(function (t) {
        if (t.jobId) self.pollJob(t.id, t.jobId, t.jobPage || 0);
      });
      this.loadConnections().then(function () { self.loadSnippets(); });
      loadMonaco(function () { self.initEditor(); });
      window.addEventListener("beforeunload", function () { self.persist(); });
      document.addEventListener("click", function () { self.closeCtx(); self.exportOpen = false; self.copyOpen = false; self.schemaPickOpen = false; });
      // ⌘/Ctrl+F：焦点不在 Monaco 时打开网格内搜索（Monaco 自己的查找不受影响）
      // ⌘/Ctrl+P：全局表名搜索（跨库，回车直达表数据）
      document.addEventListener("keydown", function (e) {
        if ((e.metaKey || e.ctrlKey) && e.key === "f" && editor && !editor.hasTextFocus()) {
          var t = self.activeTab;
          if (t && t.result && t.result.columns.length) { e.preventDefault(); self.openResQ(); }
        }
        if ((e.metaKey || e.ctrlKey) && e.key === "p") { e.preventDefault(); self.openTblSearch(); }
      });
      window.addEventListener("resize", function () { if (chartInst) chartInst.resize(); });
      // 恢复的活动 tab 若在图表视图，重画
      var at = this.activeTab;
      if (at && at.view === "chart" && at.result) this.renderChart();
    },
    template: `
<div class="dg-root" :class="{'env-prod': isProd, 'env-staging': isStaging}">
  <aside class="dg-left" :style="{width: leftW + 'px'}">
    <div class="dg-conn" :class="{'env-prod': isProd, 'env-staging': isStaging}">
      <dg-select :model-value="activeTab ? activeTab.conn : ''" :options="connOptions"
                 placeholder="选择连接…" @update:model-value="setConn"/>
    </div>
    <div class="dg-tree">
      <div class="dg-sec-hd"><span>{{ needsDb ? "库 / 表" : "表" }}</span>
        <span v-if="selCount" class="selinfo">已选 {{ selCount }} <a @click="clearSel">清除</a></span>
        <span class="act" @click="openTblSearch" title="全局搜表跳转（⌘/Ctrl+P）">⌕</span>
        <span class="act" @click="refreshTree" title="刷新（重新拉取）">↻</span></div>
      <div v-if="!activeTab || !activeTab.conn" class="dg-empty">先选择连接</div>
      <template v-else-if="needsDb">
        <div class="dg-schema-tools" v-if="databases.length">
          <button class="dg-btn" @click.stop="schemaPickOpen = !schemaPickOpen"
                  title="选择要在树里显示的 schema">Schemas ▾</button>
          <input v-if="databases.length > 6" v-model="schemaFilter" placeholder="筛选库…">
          <div v-if="schemaPickOpen" class="dg-schema-pop" @click.stop>
            <div class="hd">显示哪些 schema<a @click="showAllSchemas">全部显示</a></div>
            <label v-for="db in databases" :key="db" class="row">
              <input type="checkbox" :checked="schemaChecked(db)" @change="toggleSchemaShow(db)"> {{ db }}
            </label>
          </div>
        </div>
        <div v-if="!databases.length" class="dg-empty">（加载中或无可用库）</div>
        <div v-else-if="!filteredDatabases.length" class="dg-empty">（无匹配库）</div>
        <template v-for="db in filteredDatabases" :key="db">
          <div class="dg-item" @click="toggleDb(db)">
            <span class="tw">{{ openDb[db] ? "▾" : "▸" }}</span><span class="ic ic-db"></span><span class="nm">{{ db }}</span>
          </div>
          <template v-if="openDb[db]">
            <div class="dg-item sub" style="padding-left:22px" @click="toggleTf(db)">
              <span class="tw">{{ openTf[db] ? "▾" : "▸" }}</span><span class="ic ic-folder"></span>
              <span class="nm">tables</span><span class="cnt">{{ tablesByDb[db] ? tablesByDb[db].length : "…" }}</span></div>
            <template v-if="openTf[db]">
              <div v-if="!tablesByDb[db]" class="dg-empty" style="padding-left:40px">加载中…</div>
              <div v-else-if="!tablesByDb[db].length" class="dg-empty" style="padding-left:40px">（无表）</div>
              <tbl-node v-else v-for="t in tablesByDb[db]" :key="db+'.'+t"
                :tname="t" :meta="tableMeta[mk(t,db)]" :open="!!openTbl[mk(t,db)]"
                :sub="openSub[mk(t,db)]||{}" :selected="!!selected[mk(t,db)]" :pad="38"
                :size="fmtSize(tableSizes[mk(t,db)])"
                @toggle="toggleTbl(t,db)" @togglesub="s=>toggleSub(t,db,s)"
                @rowclick="e=>clickTable(e,t,db)" @opendata="openTableTab(t,db)"
                @ctxmenu="e=>openCtx(e,t,db)"/>
            </template>
          </template>
        </template>
      </template>
      <template v-else>
        <div class="dg-item sub" @click="toggleTf('')">
          <span class="tw">{{ openTf[''] ? "▾" : "▸" }}</span><span class="ic ic-folder"></span>
          <span class="nm">tables</span><span class="cnt">{{ tablesByDb[''] ? tablesByDb[''].length : "…" }}</span></div>
        <template v-if="openTf['']">
          <div v-if="tablesLoading" class="dg-empty" style="padding-left:24px">加载中…</div>
          <div v-else-if="!tablesByDb[''] || !tablesByDb[''].length" class="dg-empty" style="padding-left:24px">（无表）</div>
          <tbl-node v-else v-for="t in tablesByDb['']" :key="t"
            :tname="t" :meta="tableMeta[mk(t,'')]" :open="!!openTbl[mk(t,'')]"
            :sub="openSub[mk(t,'')]||{}" :selected="!!selected[mk(t,'')]" :pad="22"
            :size="fmtSize(tableSizes[mk(t,'')])"
            @toggle="toggleTbl(t,'')" @togglesub="s=>toggleSub(t,'',s)"
            @rowclick="e=>clickTable(e,t,'')" @opendata="openTableTab(t,'')"
            @ctxmenu="e=>openCtx(e,t,'')"/>
        </template>
      </template>
      <div class="dg-sec-hd" style="margin-top:8px"><span>工作流</span>
        <span class="act" @click="newFlowTab()" title="新建可视化流程（DAG 画布）">＋流程</span>
        <span class="act" @click="loadWorkflows" title="刷新">↻</span></div>
      <div v-if="!wfs.length" class="dg-empty">（暂无：点「＋流程」画一个，或在工作区写脚本点「存工作流」）</div>
      <div v-for="w in wfs" :key="w.name" class="dg-snip" @click="loadWorkflow(w)" :title="'工作区 '+w.workspace+(w.graph?' · 点击打开画布':' · 点击载入脚本')">
        <div class="t"><span>{{ w.graph ? "⧉" : "⚙" }} {{ w.name }}</span>
          <span class="x" style="opacity:1;color:var(--dg-green)" @click.stop="runWorkflow(w)" title="重跑（重拉数据+执行脚本）">▶</span>
          <span class="x" :class="{arm: delWf===w.name}" @click.stop="askDeleteWf(w.name)">{{ delWf===w.name ? "确认?" : "✕" }}</span></div>
        <div class="c">⚗ {{ w.workspace }} · {{ w.sources.length }} 源 · {{ fmtTs(w.updated_at) }}</div>
      </div>
      <div class="dg-sec-hd" style="margin-top:8px"><span>历史</span>
        <span class="act" @click="showHistory=!showHistory">{{ showHistory ? "收起" : "展开" }}</span>
        <span class="act" @click="loadHistory" title="刷新">↻</span></div>
      <template v-if="showHistory">
        <div v-if="!history.length" class="dg-empty">（暂无历史）</div>
        <div v-for="(h,hi) in history" :key="hi" class="dg-hist" @click="openHistory(h)" :title="h.sql">
          <span class="st" :class="h.status==='ok'?'ok':'bad'">●</span>
          <span class="sq">{{ h.sql }}</span>
          <span class="tm">{{ fmtTs(h.ts).slice(5) }}</span>
        </div>
      </template>
      <div class="dg-sec-hd" style="margin-top:8px"><span>片段</span><span class="act" @click="toggleSnipForm" title="保存当前 SQL 为片段">＋</span></div>
      <div v-if="showSnipForm" class="dg-snipform">
        <input v-model="snipDraft.title" placeholder="标题">
        <textarea v-model="snipDraft.note" rows="2" placeholder="备注（可选）"></textarea>
        <div style="display:flex;gap:6px"><button class="dg-btn run" style="flex:1" @click="saveSnippet">保存</button><button class="dg-btn" @click="showSnipForm=false">取消</button></div>
      </div>
      <div v-if="!snippets.length" class="dg-empty">（暂无片段）</div>
      <div v-for="s in snippets" :key="s.id" class="dg-snip" @click="openSnippet(s)">
        <div class="t"><span>{{ s.title }}</span><span class="x" :class="{arm: delSnip===s.id}" @click.stop="askDeleteSnippet(s.id)">{{ delSnip===s.id ? "确认?" : "✕" }}</span></div>
        <div v-if="s.note" class="n">{{ s.note }}</div>
        <div class="c">{{ s.connection || "—" }} · {{ fmtTs(s.updated_at) }}</div>
      </div>
    </div>
  </aside>
  <div class="dg-vsplit" @mousedown="beginDrag($event, 'x')"></div>
  <section class="dg-main">
    <div v-if="isProd" class="dg-prod-ribbon">⚠ 生产环境 · PROD · 写操作将影响线上数据，请谨慎</div>
    <div v-else-if="isStaging" class="dg-prod-ribbon staging">预发布 · STAGING 环境</div>
    <div class="dg-top">
      <button class="dg-btn run" :disabled="!activeTab || activeTab.running" @click="run(false)">▶ {{ activeTab && activeTab.running ? "执行中…" : (activeTab && activeTab.type==='data' ? "刷新" : (activeTab && activeTab.type==='flow' ? "运行流程" : "运行")) }}</button>
      <button class="dg-btn" @click="formatSql">格式化</button>
      <button class="dg-btn" @click="explainStmt" title="取执行计划（不执行语句）">解释</button>
      <div class="dg-menu">
        <button class="dg-btn" @click.stop="exportOpen=!exportOpen">导出 ▾</button>
        <div v-if="exportOpen" class="dg-menu-pop">
          <button @click="exportAs('csv')">CSV</button><button @click="exportAs('json')">JSON</button>
          <button @click="exportAs('markdown')">Markdown</button><button @click="exportAs('xlsx')">Excel (.xlsx)</button>
        </div>
      </div>
      <button class="dg-btn" @click="saveSqlFile">保存 .sql</button>
      <button v-if="isAnalysis || (activeTab && activeTab.type==='flow')" class="dg-btn" @click="saveWorkflow" title="把当前脚本/流程图存为可重跑的 workflow">存工作流</button>
      <span class="sp"></span>
      <label v-if="connMeta && (connMeta.engine==='mysql'||connMeta.engine==='postgres')" class="dg-schema-pick">执行 schema
        <dg-select :model-value="activeTab?activeTab.schema:''" :options="schemaOptions"
                   placeholder="未指定" @update:model-value="setSchema"/></label>
      <span class="hint">⌘/Ctrl+Enter 运行 · ⌃Space 补全</span>
    </div>
    <div class="dg-tabs">
      <div v-for="t in tabs" :key="t.id" class="dg-tab" :class="{active: t.id===activeId, drag: t.id===dragId}" @click="switchTab(t.id)"
           draggable="true" @dragstart="onTabDragStart(t.id, $event)" @dragover.prevent @drop="onTabDrop(t.id)" @dragend="dragId=null">
        <span class="ticon" v-if="t.type==='data'">▦</span><span class="ticon" v-else-if="t.type==='ddl'">≔</span>
        <span class="nm">{{ t.title }}</span>
        <span class="pin" :class="{on: t.pinned}" @click.stop="togglePin(t.id)"
              :title="t.pinned ? '取消固定' : '固定（防误关）'">⚲</span>
        <span v-if="!t.pinned" class="x" @click.stop="closeTab(t.id)">✕</span>
      </div>
      <button class="dg-tab-add" @click="newTab({})" title="新建查询">＋</button>
    </div>
    <div v-if="dropPlan" class="dg-drop">
      <div class="hd">⚠ 高危操作：DROP {{ dropPlan.items.length }} 张表（<b>不可逆</b>，writer 账号直接执行并审计）</div>
      <div class="list"><code v-for="i in dropPlan.items" :key="qn(i)">{{ qn(i) }}</code></div>
      <div v-if="dropPlan.results" class="res">
        <div v-for="r in dropPlan.results" :key="r.q" :class="r.ok?'okline':'errline'">{{ r.ok?'✓':'✗' }} {{ r.q }} <span v-if="r.error">— {{ r.error }}</span></div>
      </div>
      <div class="acts">
        <template v-if="!dropPlan.results">
          <button class="dg-btn danger" :disabled="dropPlan.running" @click="confirmDrop">{{ dropPlan.running ? "执行中…" : "确认 DROP" }}</button>
          <button class="dg-btn" :disabled="dropPlan.running" @click="dropPlan=null">取消</button>
        </template>
        <button v-else class="dg-btn" @click="dropPlan=null">关闭</button>
      </div>
    </div>
    <div v-if="importPlan" class="dg-drop" style="background:#1f2d3a;border-color:#2f4a63">
      <div class="hd" style="color:#9fc6ee">导入 <code>{{ importPlan.db ? importPlan.db + "." + importPlan.t : importPlan.t }}</code> 快照到分析工作区（reader 拉取，受审计与行数上限）</div>
      <div class="acts" style="flex-wrap:wrap;gap:10px;align-items:center">
        <label style="font-size:12px;color:var(--dg-muted)">工作区 <input v-model="importPlan.workspace" list="dg-ws-list" style="width:130px" class="dg-imp-in"></label>
        <datalist id="dg-ws-list"><option v-for="w in workspaces" :key="w" :value="w"></option></datalist>
        <label style="font-size:12px;color:var(--dg-muted)">数据集名 <input v-model="importPlan.dataset" style="width:150px" class="dg-imp-in"></label>
        <label style="font-size:12px;color:var(--dg-muted)">行数上限 <input v-model.number="importPlan.limit" type="number" style="width:100px" class="dg-imp-in"></label>
        <button class="dg-btn run" :disabled="importPlan.running" @click="confirmImport">{{ importPlan.running ? "导入中…" : "导入" }}</button>
        <button class="dg-btn" :disabled="importPlan.running" @click="importPlan=null">取消</button>
      </div>
    </div>
    <div v-if="importRows" class="dg-drop" style="background:#1f2d3a;border-color:#2f4a63">
      <div class="hd" style="color:#9fc6ee">导入数据到 <code>{{ importRows.db ? importRows.db + "." + importRows.t : importRows.t }}</code>
        —— 粘贴 CSV / TSV（Excel 里复制区域直接粘）或选文件；writer 单事务执行并审计</div>
      <textarea class="dg-imp-ta" v-model="importRows.text" rows="5" spellcheck="false"
                placeholder="name,email,active&#10;frank,f@x,1&#10;grace,g@x,0"
                @input="importRows.parsed = null"></textarea>
      <div class="acts" style="align-items:center;gap:12px;flex-wrap:wrap">
        <input type="file" accept=".csv,.tsv,.txt" @change="importFile" style="font-size:12px;color:var(--dg-muted);max-width:220px">
        <label style="font-size:12px;color:var(--dg-muted)">
          <input type="checkbox" v-model="importRows.header" @change="importRows.parsed = null"> 首行是表头（按列名匹配）</label>
        <button class="dg-btn" @click="importParse">解析预览</button>
        <template v-if="importRows.parsed">
          <span style="font-size:12px;color:#7ee2a8">✓ {{ importRows.parsed.rows.length }} 行 →
            {{ importRows.parsed.columns.join(", ") }}<template v-if="importRows.parsed.skipped">
            （忽略 {{ importRows.parsed.skipped }} 个未匹配列）</template></span>
          <button class="dg-btn run" :disabled="importRows.running" @click="confirmImportRows">
            {{ importRows.running ? "导入中…" : "确认导入 " + importRows.parsed.rows.length + " 行" }}</button>
        </template>
        <button class="dg-btn" :disabled="importRows.running" @click="importRows=null">取消</button>
      </div>
    </div>
    <div v-if="wfAsk" class="dg-drop" style="background:#22301f;border-color:#3d5a35">
      <div class="hd" style="color:#a8d99b">保存 workflow：当前脚本 + 工作区 <code>{{ wfAsk.ws }}</code> 的取数配方（重跑时自动重拉数据）</div>
      <div class="acts" style="align-items:center">
        <label style="font-size:12px;color:var(--dg-muted)">名称 <input v-model="wfAsk.name" class="dg-imp-in" style="width:200px" @keydown.enter="confirmSaveWorkflow"></label>
        <button class="dg-btn run" @click="confirmSaveWorkflow">保存</button>
        <button class="dg-btn" @click="wfAsk=null">取消</button>
      </div>
    </div>
    <div class="dg-editor-row" v-show="activeTab && activeTab.type!=='data' && activeTab.type!=='flow'" :style="editorRowStyle">
      <div class="dg-estatus" :class="execState.cls" :title="execState.tip"><span>{{ execState.icon }}</span></div>
      <div class="dg-editor"><div ref="editorEl" style="position:absolute;inset:0"></div>
        <div v-if="!editorReady" class="dg-editor-loading">编辑器加载中…</div>
      </div>
    </div>
    <div class="dg-flow" v-if="activeTab && activeTab.type==='flow' && activeTab.graph" :style="{height: editorH + 'px'}">
      <div class="dg-flow-bar">
        <button class="dg-btn" @click="flowAddNode('source')" title="从任意连接取数为数据集">＋取数</button>
        <button class="dg-btn" @click="flowAddNode('file')" title="导入本地 CSV/Parquet/JSON">＋文件</button>
        <button class="dg-btn" @click="flowAddNode('filter')">＋过滤</button>
        <button class="dg-btn" @click="flowAddNode('join')">＋JOIN</button>
        <button class="dg-btn" @click="flowAddNode('aggregate')">＋聚合</button>
        <button class="dg-btn" @click="flowAddNode('sql')" title="自由 SQL（直接引用上游节点名）">＋SQL</button>
        <button class="dg-btn" @click="flowAddNode('output')">＋输出</button>
        <span class="hint" style="margin-left:auto">拖节点右缘圆点 → 下一节点左缘连线 · 点 ✕ 删连线 · 工作区 {{ (activeTab.conn||'').split('/')[1] }}</span>
      </div>
      <div class="dg-flow-body">
        <div class="dg-flow-canvas" ref="flowCanvas">
          <svg class="dg-flow-svg">
            <path v-for="(e,i) in activeTab.graph.edges" :key="i" class="fe" :d="edgePath(e)"/>
            <path v-if="linkDraft" class="fe draft" :d="draftPath()"/>
          </svg>
          <div v-for="(e,i) in activeTab.graph.edges" :key="'x'+i" class="dg-edge-x"
               :style="{left: edgeMid(e).x + 'px', top: edgeMid(e).y + 'px'}"
               title="删除连线" @click="flowDelEdge(i)">✕</div>
          <div v-for="n in activeTab.graph.nodes" :key="n.id" class="dg-fnode"
               :class="[n.type, {sel: activeTab.sel===n.id}, activeTab.nodeStatus[n.id] || '']"
               :style="{left: n.x + 'px', top: n.y + 'px'}"
               @mousedown="flowNodeDown($event, n)">
            <div class="hd"><span class="ty">{{ flowTypeLabel(n.type) }}</span>
              <span class="nm">{{ n.name }}</span>
              <span class="st">{{ activeTab.nodeStatus[n.id]==='ok' ? '✓' : activeTab.nodeStatus[n.id]==='err' ? '✗' : activeTab.nodeStatus[n.id]==='running' ? '⟳' : '' }}</span>
              <span class="x" @mousedown.stop @click.stop="flowDelNode(n.id)">✕</span></div>
            <div class="bd">{{ flowNodeDesc(n) }}</div>
            <span v-for="p in flowInPorts(n)" :key="p" class="port pin" :class="p"
                  :title="p==='left'?'左输入（SQL 里是 l）':p==='right'?'右输入（SQL 里是 r）':'输入'"
                  @mousedown.stop @mouseup="portUp($event, n, p)"></span>
            <span v-if="flowHasOut(n)" class="port pout" title="拖到下一节点的输入口"
                  @mousedown="portDown($event, n)"></span>
          </div>
          <div v-if="!activeTab.graph.nodes.length" class="dg-empty" style="padding:40px;position:absolute">
            用上方按钮添加节点：取数 → 过滤 / JOIN / 聚合 → 输出，拖节点右缘圆点到下一节点左缘完成连线。</div>
        </div>
        <div class="dg-flow-cfg" v-if="selNode">
          <div class="cfg-hd">{{ flowTypeLabel(selNode.type) }} 节点</div>
          <div class="row"><label>名字</label><input v-model="selNode.name" @change="persist" spellcheck="false"></div>
          <template v-if="selNode.type==='source'">
            <div class="row"><label>连接</label><dg-select :model-value="selNode.cfg.conn" :options="realConnOptions"
                 placeholder="选择连接…" @update:model-value="v => { selNode.cfg.conn = v; persist(); }"/></div>
            <div class="row"><label>取数 SQL</label><textarea v-model="selNode.cfg.sql" rows="5" @change="persist"
                 spellcheck="false" placeholder="SELECT * FROM t WHERE ..."></textarea></div>
            <div class="row"><label>行数上限</label><input type="number" v-model.number="selNode.cfg.limit" @change="persist" placeholder="默认 20 万"></div>
            <div class="row"><label>schema</label><input v-model="selNode.cfg.schema" @change="persist" placeholder="未绑库连接需指定"></div>
          </template>
          <template v-else-if="selNode.type==='file'">
            <div class="row"><label>文件路径</label><input v-model="selNode.cfg.path" @change="persist" placeholder="/path/data.csv（csv/parquet/json）"></div>
          </template>
          <template v-else-if="selNode.type==='filter'">
            <div class="row"><label>WHERE</label><textarea v-model="selNode.cfg.where" rows="4" @change="persist"
                 spellcheck="false" placeholder="status = 'paid' AND amount > 100"></textarea></div>
          </template>
          <template v-else-if="selNode.type==='join'">
            <div class="row"><label>类型</label><dg-select :model-value="selNode.cfg.kind || 'INNER'" :options="joinKindOptions"
                 @update:model-value="v => { selNode.cfg.kind = v; persist(); }"/></div>
            <div class="row"><label>ON</label><input v-model="selNode.cfg.on" @change="persist" spellcheck="false" placeholder="l.uid = r.id（l=左输入 r=右输入）"></div>
            <div class="row"><label>SELECT</label><input v-model="selNode.cfg.select" @change="persist" spellcheck="false" placeholder="l.*, r.*"></div>
          </template>
          <template v-else-if="selNode.type==='aggregate'">
            <div class="row"><label>GROUP BY</label><input v-model="selNode.cfg.group" @change="persist" spellcheck="false" placeholder="channel（留空 = 全局聚合）"></div>
            <div class="row"><label>聚合表达式</label><textarea v-model="selNode.cfg.aggs" rows="3" @change="persist"
                 spellcheck="false" placeholder="count(*) AS n, sum(amount) AS total"></textarea></div>
          </template>
          <template v-else-if="selNode.type==='sql'">
            <div class="row"><label>SQL</label><textarea v-model="selNode.cfg.sql" rows="8" @change="persist"
                 spellcheck="false" placeholder="SELECT ...（直接用上游节点名作表名）"></textarea></div>
          </template>
          <template v-else-if="selNode.type==='output'">
            <div class="row"><label>ORDER BY</label><input v-model="selNode.cfg.order_by" @change="persist" spellcheck="false" placeholder="total DESC"></div>
            <div class="row"><label>LIMIT</label><input type="number" v-model.number="selNode.cfg.limit" @change="persist" placeholder="1000"></div>
          </template>
          <div class="row acts">
            <button class="dg-btn" @click="flowPreview(selNode)" title="运行过一次后，各节点都是工作区里的视图，可直接查看">预览数据</button>
            <button class="dg-btn danger" @click="flowDelNode(selNode.id)">删除节点</button>
          </div>
        </div>
      </div>
    </div>
    <div class="dg-hsplit" v-show="activeTab && (activeTab.type==='query' || activeTab.type==='flow')" @mousedown="beginDrag($event, 'y')"></div>
    <div class="dg-results" v-show="activeTab && activeTab.type!=='ddl'">
      <template v-if="activeTab">
        <div v-if="activeTab.type==='data'" class="dg-where">
          <span class="k">WHERE</span>
          <input id="dg-where-input" v-model="activeTab.where" placeholder="status = 'paid' AND amount > 100"
                 @keydown.enter="applyWhere">
          <span class="k">ORDER BY</span>
          <input class="obin" v-model="activeTab.orderBy" placeholder="amount DESC, id"
                 @keydown.enter="applyWhere" title="任意排序表达式，回车应用；点列头快捷设置">
          <button class="dg-btn" @click="applyWhere">应用</button>
          <button v-if="activeTab.where || activeTab.orderBy" class="dg-btn"
                  @click="activeTab.where='';activeTab.orderBy='';applyWhere()">清除</button>
          <button class="dg-btn" @click="startNewRow(null)" title="在网格里填一行，生成 INSERT 走写确认">＋ 新增行</button>
        </div>
        <div v-if="activeTab.explain" class="dg-explain">
          <div class="hd"><b>执行计划</b><span class="act" @click="closeExplain">✕ 关闭</span></div>
          <div v-if="activeTab.explain.loading" class="dg-empty">获取中…</div>
          <plan-node v-else-if="activeTab.explain.tree" :label="'plan'" :node="activeTab.explain.tree" :depth="0"/>
          <table v-else-if="activeTab.explain.rows" class="dg-rt" style="margin:8px">
            <thead><tr><th v-for="c in activeTab.explain.columns" :key="c">{{ c }}</th></tr></thead>
            <tbody><tr v-for="(r,i) in activeTab.explain.rows" :key="i"><td v-for="(v,j) in r" :key="j">{{ cellText(v) }}</td></tr></tbody>
          </table>
        </div>
        <div v-if="activeTab.confirm" class="dg-confirm">
          <h4>确认执行写操作 <span class="lv" :style="{background: lvColor(activeTab.confirm.risk.level)}">{{ activeTab.confirm.risk.level }}</span> <span style="color:var(--dg-muted);font-weight:normal">{{ activeTab.confirm.statement_kind }}</span></h4>
          <div style="font-size:12px;color:var(--dg-muted)">将用 writer 账号<b>直接执行</b>并记入审计（后台旁路，不进审批单）。</div>
          <div class="kv"><span>影响表：{{ (activeTab.confirm.risk.tables||[]).join(", ")||"—" }}</span><span>表行量级：{{ numOr(activeTab.confirm.risk.row_estimate) }}</span><span>含 WHERE：{{ boolText(activeTab.confirm.risk.has_where) }}</span><span>命中索引：{{ boolText(activeTab.confirm.risk.uses_index) }}</span></div>
          <div class="reasons" v-for="r in (activeTab.confirm.risk.reasons||[])" :key="r">• {{ r }}</div>
          <div class="acts"><button class="dg-btn ok" @click="confirmRun">确认执行</button><button class="dg-btn" @click="cancelConfirm">取消</button></div>
        </div>
        <div v-if="activeTab.wfSteps && activeTab.wfSteps.length" class="dg-wfsteps">
          <div v-for="(st,si) in activeTab.wfSteps" :key="si" :class="st.ok?'okline':'errline'">
            {{ st.ok ? "✓" : "✗" }} {{ st.step }}<template v-if="st.rows!=null">（{{ st.rows }} 行）</template>
            <template v-if="st.error">— {{ st.error }}</template></div>
        </div>
        <div v-if="activeTab.err" class="dg-res-err">⚠ {{ activeTab.err }}</div>
        <div v-else-if="activeTab.ok" class="dg-res-ok">✓ 执行成功，影响 {{ activeTab.ok.affected_rows }} 行 · {{ activeTab.ok.duration_ms }} ms</div>
        <template v-else-if="activeTab.result">
          <div class="dg-res-meta">
            <span>{{ activeTab.result.paginated ? "本页 " : "" }}{{ activeTab.result.rows.length }} 行</span>
            <span v-if="activeTab.result.duration_ms!=null">{{ activeTab.result.duration_ms }} ms</span>
            <span v-if="activeTab.result.paginated && activeTab.result.ordered===false" style="color:var(--dg-amber)" title="LIMIT/OFFSET 翻页在无 ORDER BY 时顺序不保证稳定">⚠ 无 ORDER BY</span>
            <span v-if="activeTab.result.paginated" class="pager">
              <button class="pg" :disabled="activeTab.result.page<=0" @click="goPage(activeTab.result.page-1)">‹ 上一页</button>
              <span class="pn">第 {{ activeTab.result.page+1 }} 页</span>
              <button class="pg" :disabled="!activeTab.result.has_next" @click="goPage(activeTab.result.page+1)">下一页 ›</button>
            </span>
            <span v-else-if="activeTab.result.truncated" style="color:var(--dg-amber)">已截断到 max_rows</span>
            <span v-if="selRowCount()" class="rowops">
              已选 {{ selRowCount() }} 行
              <span class="dg-menu"><button class="dg-btn" @click.stop="copyOpen=!copyOpen">复制为 ▾</button>
                <div v-if="copyOpen" class="dg-menu-pop">
                  <button @click="copyRows('tsv')">TSV（贴 Excel）</button>
                  <button @click="copyRows('insert')">INSERT 语句</button>
                  <button @click="copyRows('markdown')">Markdown</button>
                  <button @click="copyRows('json')">JSON</button>
                </div></span>
              <button v-if="activeTab.type==='data' && selRowCount()===1" class="dg-btn"
                      @click="startNewRow(selRis()[0])" title="以选中行为模板新增（主键留空）">克隆</button>
              <button v-if="activeTab.type==='data'" class="dg-btn danger"
                      @click="deleteSelRows" title="按主键生成 DELETE，走写确认">删除</button>
              <a class="act" @click="clearRowSel">✕</a>
            </span>
            <span v-if="activeTab.resQ" class="dg-resq">
              <input id="dg-resq-input" v-model="activeTab.resQ.q" placeholder="在结果中查找…"
                     @input="resQInput" @keydown.enter.exact="resQNext(1)"
                     @keydown.shift.enter="resQNext(-1)" @keydown.esc="closeResQ">
              <span class="cnt">{{ activeTab.resQ.hits.length ? (activeTab.resQ.cur+1) + "/" + activeTab.resQ.hits.length : "0" }}</span>
              <a class="act" @click="resQNext(-1)" title="上一个 (⇧Enter)">‹</a>
              <a class="act" @click="resQNext(1)" title="下一个 (Enter)">›</a>
              <a class="act" @click="closeResQ">✕</a>
            </span>
            <span v-if="activeTab.result.columns.length" class="exp">
              <button v-if="!activeTab.resQ" @click="openResQ" title="在结果中查找（⌘/Ctrl+F）">🔍</button>
              <span class="vswitch">
                <button :class="{on: (activeTab.view||'table')==='table'}" @click="setView('table')">表格</button>
                <button :class="{on: activeTab.view==='chart'}" @click="setView('chart')">图表</button>
              </span>
              <button @click="exportAs('csv')">CSV</button><button @click="exportAs('json')">JSON</button><button @click="exportAs('markdown')">MD</button><button @click="exportAs('xlsx')">XLSX</button>
            </span>
          </div>
          <div v-if="!activeTab.result.columns.length" class="dg-res-empty">语句已执行，无结果集。</div>
          <template v-else-if="activeTab.view==='chart' && activeTab.chart">
            <div class="dg-chart-bar">
              <label>类型 <dg-select :model-value="activeTab.chart.type" :options="chartTypeOptions"
                                     @update:model-value="setChartOpt('type', $event)"/></label>
              <label>X <dg-select :model-value="activeTab.chart.x" :options="chartColOptions"
                                  @update:model-value="setChartOpt('x', $event)"/></label>
              <label>Y <dg-select :model-value="activeTab.chart.y" :options="chartColOptions"
                                  @update:model-value="setChartOpt('y', $event)"/></label>
              <label>聚合 <dg-select :model-value="activeTab.chart.agg||''" :options="chartAggOptions"
                                     @update:model-value="setChartOpt('agg', $event)"/></label>
              <span class="hint" v-if="activeTab.result.paginated">仅当前页数据</span>
            </div>
            <div class="dg-chart" ref="chartEl"></div>
          </template>
          <div v-else class="dg-res-body">
          <div class="dg-res-scroll"><table class="dg-rt">
            <thead><tr>
              <th class="gut" title="点击行号选择行（⇧范围 / ⌘多选）">#</th>
              <th v-for="c in activeTab.result.columns" :key="c"
                  :class="{sortable: activeTab.type==='data'}"
                  @click="activeTab.type==='data' && cycleOrder(c)"
                  :title="activeTab.type==='data' ? '点击排序（走 SQL）' : c">
                {{ c }}{{ orderMark(c) }}
                <span v-if="activeTab.type==='data'" class="funnel" @click.stop="funnel(c)" title="按此列筛选（填入 WHERE）">⧩</span>
              </th>
            </tr></thead>
            <tbody>
            <tr v-if="activeTab.newRow" class="dg-newrow">
              <td class="gut nw" title="填写后回车提交；留空列用默认值，填 NULL 为显式 NULL">＋</td>
              <td v-for="(c,ci) in activeTab.result.columns" :key="'n'+ci">
                <input v-model="activeTab.newRow.values[ci]" :placeholder="c"
                       @keydown.enter="commitNewRow" @keydown.esc="activeTab.newRow=null">
              </td>
            </tr>
            <tr v-for="(row,ri) in activeTab.result.rows" :key="ri" :class="{rsel: activeTab.rowSel && activeTab.rowSel[ri]}">
              <td class="gut" @mousedown.prevent @click.stop="rowClick(ri,$event)">{{ ri+1 }}</td>
              <td v-for="(v,ci) in row" :key="ci" :title="cellTitle(v)" :data-cell="ri+':'+ci"
                  :class="{editable: activeTab.type==='data', vsel: activeTab.vsel && activeTab.vsel.ri===ri && activeTab.vsel.ci===ci,
                           hit: isHit(ri,ci), curhit: isCurHit(ri,ci)}"
                  @click="cellClick(ri,ci)"
                  @dblclick="activeTab.type==='data' && startEdit(ri,ci)">
                <input v-if="activeTab.edit && activeTab.edit.ri===ri && activeTab.edit.ci===ci"
                       id="dg-cell-input" class="dg-cell-edit" v-model="activeTab.edit.val"
                       @keydown.enter="commitEdit" @keydown.esc="cancelEdit" @blur="cancelEdit">
                <template v-else><span v-if="v===null" class="nul">NULL</span><span v-else>{{ cellText(v) }}</span></template>
              </td>
            </tr></tbody>
          </table>
          <div v-if="activeTab.newRow" class="dg-newrow-acts">
            <button class="dg-btn run" @click="commitNewRow">插入（生成 INSERT）</button>
            <button class="dg-btn" @click="activeTab.newRow=null">取消</button>
            <span class="hint">留空列用数据库默认值；填 NULL 为显式 NULL</span>
          </div></div>
          <div v-if="vpOpen && activeTab.type==='data' && activeTab.vsel" class="dg-vp">
            <div class="vp-hd">
              <span class="vt" :class="{on: vpTab==='value'}" @click="vpTab='value'">Value</span>
              <span class="vt" :class="{on: vpTab==='record'}" @click="vpTab='record'">Record</span>
              <span class="vp-x" @click="vpOpen=false">✕</span>
            </div>
            <template v-if="vpTab==='value'">
              <div class="vp-col">{{ activeTab.result.columns[activeTab.vsel.ci] }}</div>
              <textarea class="vp-ta" v-model="vpVal" :disabled="vpNull"
                        placeholder="（空字符串）" spellcheck="false"></textarea>
              <div class="vp-fmt">
                <button class="dg-btn" @click="vpFormat(true)" title="JSON 美化（缩进 2 空格）">格式化 JSON</button>
                <button class="dg-btn" @click="vpFormat(false)" title="JSON 压缩为单行">压缩</button>
              </div>
              <label class="vp-null"><input type="checkbox" v-model="vpNull"> 设为 NULL</label>
              <div class="vp-acts">
                <button class="dg-btn run" :disabled="!vpDirty()" @click="vpSave"
                        title="生成 UPDATE，经风险确认后由 writer 执行">保存（生成 UPDATE）</button>
                <span v-if="vpDirty()" class="vp-dirty">已修改</span>
              </div>
            </template>
            <template v-else>
              <div class="vp-rec">
                <div v-for="r in vpRecord()" :key="r.col" class="vp-rec-row" :class="{cur: r.cur}">
                  <span class="rc">{{ r.col }}</span>
                  <span class="rv"><i v-if="r.val===null" class="nul">NULL</i><template v-else>{{ r.val }}</template></span>
                </div>
              </div>
            </template>
          </div>
          </div>
        </template>
        <div v-else class="dg-res-empty">{{ activeTab.type==='data' ? "加载中…" : "运行查询查看结果（⌘/Ctrl+Enter）。" }}</div>
      </template>
    </div>
  </section>
  <div v-if="ctx.show" class="dg-ctx" :style="{left: ctx.x+'px', top: ctx.y+'px'}" @click.stop>
    <div class="hd">{{ ctx.multi ? ("已选 " + selCount + " 张表") : qualified() }}</div>
    <template v-if="!ctx.multi">
      <button @click="ctxAction('open')">打开表数据</button>
      <button @click="ctxAction('ddl')">查看 DDL</button>
      <button @click="ctxAction('select')">SELECT * → 编辑器</button>
      <button @click="ctxAction('count')">统计行数</button>
      <button @click="ctxAction('copy')">复制表名</button>
      <button @click="ctxAction('import')">导入数据（CSV/粘贴）…</button>
      <button v-if="!isAnalysis" @click="ctxAction('toanalysis')">导入到分析工作区…</button>
    </template>
    <template v-else>
      <button @click="ctxAction('copy')">复制表名列表</button>
    </template>
    <div class="sep"></div>
    <button class="danger" @click="ctxAction('drop')">{{ ctx.multi ? ("DROP " + selCount + " 张表…") : "DROP 表…" }}</button>
  </div>
  <div v-if="tblSearch" class="dg-tblsearch" @click.self="tblSearch=null">
    <div class="box">
      <input id="dg-tblsearch-input" v-model="tblSearch.q" placeholder="输入表名跳转（当前连接跨库搜索）…"
             spellcheck="false" @input="tblSearchInput" @keydown="tblSearchKey">
      <div class="list">
        <div v-if="tblSearch.loading" class="dg-empty">搜索中…</div>
        <div v-else-if="tblSearch.q && !tblSearch.results.length" class="dg-empty">（无匹配表）</div>
        <div v-for="(r,i) in tblSearch.results" :key="i" class="item" :class="{cur: i===tblSearch.sel}"
             @click="tblSearchGo(r)" @mousemove="tblSearch.sel=i">
          <span class="ic ic-table"></span>
          <span class="nm">{{ r.db ? r.db + "." + r.table : r.table }}</span>
        </div>
      </div>
      <div class="ft">↑↓ 选择 · Enter 打开表数据 · Esc 关闭</div>
    </div>
  </div>
  <div v-if="isProd" class="dg-prod-frame"></div>
  <div v-else-if="isStaging" class="dg-prod-frame staging"></div>
  <div id="dg-toast" v-if="toast">{{ toast }}</div>
</div>`
  });

  app.component("tbl-node", TblNode);
  app.component("plan-node", PlanNode);
  app.component("dg-select", DgSelect);
  app.mount("#dbm-console");
})();
