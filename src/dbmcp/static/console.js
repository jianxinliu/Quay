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
  var bmCollection = null;    // 书签装饰集合（切 tab 时重设为该 tab 的书签）
  var execCollection = null;  // 执行状态字形装饰（在被执行语句行左侧显示 ⟳/✓/✗）
  var stmtBoxCol = null;      // 缺分号波浪线装饰
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
  // 列类型 → 表头小图标（DataGrip 风）
  var COL_GLYPH = { number: "#", string: "T", datetime: "◷", date: "◷", time: "◷",
                    json: "{}", bool: "☑", binary: "⬡", "": "·" };

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
  // 语句起始关键字（换行后遇到它、且不在括号内 → 可能是新语句）
  var STMT_KW = { SELECT: 1, WITH: 1, INSERT: 1, UPDATE: 1, DELETE: 1, CREATE: 1, ALTER: 1,
    DROP: 1, TRUNCATE: 1, REPLACE: 1, SHOW: 1, EXPLAIN: 1, USE: 1, CALL: 1, GRANT: 1,
    REVOKE: 1, RENAME: 1, ANALYZE: 1, OPTIMIZE: 1, MERGE: 1 };
  // 若当前累计语句已含这些词，则后面新行的 SELECT/WITH 多半是**续接**（CTE / INSERT…SELECT /
  // CREATE…AS SELECT / UNION 等），不切分。
  var CONT_KW = /\b(WITH|INSERT|REPLACE|CREATE|UNION|INTERSECT|EXCEPT|MERGE)\b/i;

  // 把编辑器文本拆成语句区间。分隔符：分号 `;`；以及**换行/空行**——下一行以语句关键字开头、
  // 且不在括号内时视为新语句（「不刻板依赖分号」）。多行语句（SELECT…\nFROM…）、CTE、
  // INSERT…SELECT、子查询、UNION 都能正确保持为一条（靠括号深度 + 续接词判断）。
  function stmtRanges(text) {
    var ranges = [], start = 0, i = 0, n = text.length, depth = 0;
    function push(end) { if (text.slice(start, end).trim()) ranges.push({ s: start, e: end }); }
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
      } else if (c === "(") { depth++; i++; }
      else if (c === ")") { if (depth > 0) depth--; i++; }
      else if (c === ";") {
        push(i + 1); i++; start = i;
      } else if (c === "\n") {
        // 跳过后续空白/空行，看下一非空行的首个关键字
        var j = i + 1;
        while (j < n && (text[j] === " " || text[j] === "\t" || text[j] === "\r" || text[j] === "\n")) j++;
        var boundary = false;
        if (j < n && depth === 0) {
          var mk = text.slice(j, j + 20).match(/^([A-Za-z_]+)/);
          var kw = mk ? mk[1].toUpperCase() : "";
          if (STMT_KW[kw]) {
            // SELECT/WITH 有歧义（可能是续接）：当前语句已含续接词就不切；其它关键字直接切
            boundary = (kw === "SELECT" || kw === "WITH") ? !CONT_KW.test(text.slice(start, i)) : true;
          }
        }
        if (boundary) { push(i); start = j; i = j; } else i++;
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

  // 只在响应确为 JSON 时解析；否则（登录页/错误页 HTML、代理页等）转成可读错误对象，
  // 避免对 "<!doctype ..." 直接 r.json() 抛出「Unexpected token '<'」误导用户。
  function parseApi(r) {
    if ((r.headers.get("content-type") || "").indexOf("application/json") !== -1) return r.json();
    return r.text().then(function () {
      var msg = r.status === 401 ? "登录已过期，请刷新页面重新登录"
              : r.status === 403 ? "请求被拒绝：管理后台只能从本机访问"
              : "服务返回了非预期响应（HTTP " + (r.status || "?") + "），请刷新页面重试";
      return { ok: false, error: msg };
    });
  }
  function apiGet(url) {
    return fetch(url, { headers: { Accept: "application/json" } }).then(parseApi);
  }
  function apiPost(url, obj) {
    var fd = new FormData();
    for (var k in obj) if (obj[k] != null) fd.append(k, obj[k]);
    return fetch(url, { method: "POST", headers: { Accept: "application/json" }, body: fd }).then(parseApi);
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

  // 递归把 EXPLAIN JSON 计划里的「表访问」摘出来做直观概览，兼容三种格式：
  // ① MySQL 旧 FORMAT=JSON（access_type ALL/ref…、cost_info、rows_examined_per_scan）
  // ② MySQL 新 JSON（schema v2.0，operation/estimated_rows/estimated_total_cost，access_type "table"=全表扫描）
  // ③ PG FORMAT JSON（Relation Name / Node Type / Plan Rows / Total Cost）
  function walkPlan(node, acc) {
    if (!node || typeof node !== "object") return;
    if (Array.isArray(node)) { node.forEach(function (x) { walkPlan(x, acc); }); return; }
    if (node.table_name) {                       // MySQL 表节点（新旧格式都有 table_name）
      var ci = node.cost_info || {};
      var at = (node.access_type || "").toLowerCase();
      acc.push({ table: node.table_name, access: node.access_type || "", operation: node.operation || "",
        rows: node.estimated_rows != null ? node.estimated_rows
              : (node.rows_examined_per_scan != null ? node.rows_examined_per_scan : node.rows_produced_per_join),
        key: node.key || "", filtered: node.filtered != null ? node.filtered : null,
        cost: node.estimated_total_cost != null ? node.estimated_total_cost
              : (ci.read_cost != null ? ci.read_cost : (ci.prefix_cost != null ? ci.prefix_cost : null)),
        warn: at === "all" || at === "table" });   // 旧格式 ALL / 新格式 table = 全表扫描
    } else if (node["Relation Name"] || (node["Node Type"] && /scan/i.test(node["Node Type"]))) {  // PG 扫描节点
      acc.push({ table: node["Relation Name"] || node["Node Type"], access: node["Node Type"] || "",
        operation: "", rows: node["Plan Rows"], key: node["Index Name"] || "", filtered: null,
        cost: node["Total Cost"] != null ? node["Total Cost"] : null,
        warn: /seq scan/i.test(node["Node Type"] || "") });
    }
    Object.keys(node).forEach(function (k) { walkPlan(node[k], acc); });
  }

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
        // tab 分组折叠状态（按连接名），持久化到 localStorage
        tabGroupCollapsed: (function () { try { return JSON.parse(localStorage.getItem("dbm-tabgrp-collapsed") || "{}"); } catch (e) { return {}; } })(),
        clockTick: 0,      // 每 200ms +1，驱动执行计时秒表刷新
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
        dragGroup: null,        // 拖动的组（连接 key），用于整体调整组顺序
        ctx: { show: false, x: 0, y: 0, table: "", schema: "", multi: false },
        tabCtx: { show: false, x: 0, y: 0, id: null },  // 编辑区 tab 右键菜单
        renamingId: null, renameVal: "",                // tab 就地改名
        acc: { tree: true, bm: true, wf: true, hist: false, snip: true },  // 左栏手风琴各区展开状态
        dropPlan: null,         // {items:[{t,db}], running, results}
        delSnip: null, delWf: null, wfAsk: null,
        history: [], showHistory: false,
        snippets: [], showSnipForm: false, snipDraft: { title: "", note: "" },
        snipAllConns: false,   // 片段列表：默认只显示当前连接的，切「全部」看所有连接

        exportOpen: false, copyOpen: false, submitOpen: false, editorReady: false, toast: "",
        bmOpen: false, bmTick: 0,   // 书签下拉菜单；bmTick 强制刷新预览文本
        colMenu: { show: false, x: 0, y: 0, ci: -1 },
        sug: { open: false, items: [], sel: 0, which: "", word: "" },  // WHERE/ORDER BY 字段提示
        schemaShow: {}, schemaDefault: {}, schemaPickOpen: false,
        vpOpen: false, vpTab: "value", vpVal: "", vpNull: false,
        leftW: 264, editorH: 300, dataLogH: 150,
        theme: "dark",          // 系统设置：dark | light（浅色主题）
        minimapOn: true,        // 系统设置：编辑器 minimap 是否显示（DB 配置，默认开）
        editorFontSize: 13,     // 系统设置：编辑器字号（sql_font_size）
        editorWordWrap: false,  // 系统设置：编辑器自动换行（sql_word_wrap）
        schemaFloatRight: 16,   // schema 浮层距编辑器右缘距离（动态避开 minimap）
        linkDraft: null,        // 画布拉线中 {from, x, y}（画布内坐标）
        aiEnabled: false,       // 系统设置：AI 辅助写 SQL 是否开启（决定「✨ AI」按钮是否出现）
        aiPanel: null,          // AI 生成面板：{question, explain, samples, tables, picked, filter, loading, running, error}
        wfAi: null,             // AI 生成流程面板：{question, conn, tables, picked, filter, loading, running, error, pos}
      };
    },
    computed: {
      activeTab: function () {
        var id = this.activeId, ts = this.tabs;
        for (var i = 0; i < ts.length; i++) if (ts[i].id === id) return ts[i];
        return null;
      },
      // 编辑区 tab 按连接分组：同连接聚成一簇，组头显示连接名+环境，可折叠（否则 tab 多了太乱）
      tabGroups: function () {
        var self = this, order = [], map = {};
        this.tabs.forEach(function (t) {
          var key = t.conn || "";
          if (!(key in map)) { map[key] = { conn: key, tabs: [], meta: self.tabConn(t) }; order.push(key); }
          map[key].tabs.push(t);
        });
        return order.map(function (k) { return map[k]; });
      },
      // 当前 query tab 的书签列表（行号 + 该行 SQL 预览），供左栏「书签」区直接跳转/执行
      bmList: function () {
        var _ = this.bmTick;   // 编辑后手动 +1 触发预览刷新
        var t = this.activeTab;
        if (!t || t.type !== "query" || !t.bookmarks || !t.bookmarks.length) return [];
        var m = models.get(t.id);
        return t.bookmarks.slice().sort(function (a, b) { return a - b; }).map(function (ln) {
          var txt = m ? (m.getLineContent(ln) || "").trim() : "";
          return { line: ln, text: txt || "(空行)" };
        });
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
      aiFabStyle: function () {
        // ✨AI 按钮浮在编辑器右上角（对齐 schema 选择器）；有 schema 选择器时左移让位
        var m = this.connMeta, t = this.activeTab;
        var hasSchema = !!m && (m.engine === "mysql" || m.engine === "postgres")
          && t && t.type === "query";
        // 右缘一律基于 schemaFloatRight（已动态避开 minimap）；有 schema 选择器时再左移让位
        return { top: "8px", left: "auto",
                 right: (this.schemaFloatRight + (hasSchema ? 232 : 0)) + "px" };
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
      // 片段与连接绑定：默认只列当前 tab 连接的片段（避免跨连接错乱）；无连接或「全部」则列全部
      visibleSnippets: function () {
        var conn = this.activeTab ? this.activeTab.conn : "";
        if (this.snipAllConns || !conn) return this.snippets;
        return this.snippets.filter(function (s) { return s.connection === conn; });
      },
      selCount: function () { return Object.keys(this.selected).length; },
      // 执行计时（客户端秒表，随 clockTick 每 200ms 刷新，平滑准确）——从本 tab 开跑那一刻算起
      runElapsed: function () {
        this.clockTick;  // 依赖：让本 computed 随定时器重算
        var t = this.activeTab;
        if (!t || !t.running || !t.jobRunAt) return "0.0s";
        return ((Date.now() - t.jobRunAt) / 1000).toFixed(1) + "s";
      },
      editorRowStyle: function () {
        var t = this.activeTab;
        if (t && t.type === "ddl") return { flex: "1", height: "auto" };
        return { height: this.editorH + "px" };
      },
      connOptions: function () {
        // Redis 连接走独立的 /admin/redis 控制台，不在 SQL 查询台里
        var sqlConns = this.connections.filter(function (c) { return c.engine !== "redis"; });
        var opts = [{ value: "", label: "选择连接…", env: "" }].concat(sqlConns.map(function (c) {
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
      connName: function () {  // 连接名（"project/connection" → connection），prod 写闸门需用户输入匹配
        var t = this.activeTab; if (!t || !t.conn) return "";
        return t.conn.split("/").slice(1).join("/") || t.conn;
      },
      envInfo: function () {
        var m = this.connMeta;
        if (!m || !m.environment) return null;
        return { env: m.environment, color: ENV_COLORS[m.environment] || "#64748b" };
      },
      // 执行中的骨架列：数据 tab 的列由表结构预先知道 → 加载时先渲染列头（查询 tab 未知则空）
      loadingCols: function () {
        var t = this.activeTab;
        if (!t || t.type !== "data" || !t.table) return [];
        var meta = this.tableMeta[this.mk(t.table, t.schema)];
        return meta && meta.columns ? meta.columns.map(function (c) { return c.name; }) : [];
      },
      // EXPLAIN 概览：从 JSON 计划摘出表访问列表 + 总成本 + 全表扫描数（直观呈现，替代只读原始树）
      explainInfo: function () {
        var t = this.activeTab;
        if (!t || !t.explain || !t.explain.tree) return null;
        var tree = t.explain.tree, scans = [];
        walkPlan(tree, scans);
        var cost = null;
        if (tree.query_block && tree.query_block.cost_info) cost = tree.query_block.cost_info.query_cost;  // 旧 MySQL
        else if (tree.query_plan && tree.query_plan.estimated_total_cost != null) cost = tree.query_plan.estimated_total_cost;  // 新 MySQL
        else if (Array.isArray(tree) && tree[0] && tree[0].Plan) cost = tree[0].Plan["Total Cost"];  // PG
        return { cost: cost, scans: scans, warnCount: scans.filter(function (s) { return s.warn; }).length };
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
      // EXPLAIN 概览：数字千分位；访问类型着色（全表扫描红/范围·全索引扫描琥珀/命中索引绿）
      fmtN: function (v) { var n = Number(v); return isFinite(n) ? n.toLocaleString("en-US") : v; },
      accessClass: function (a) {
        var s = (a || "").toLowerCase();
        if (s === "all" || s === "table" || s.indexOf("seq scan") >= 0) return "bad";   // 全表扫描
        if (s === "index" || s.indexOf("range") >= 0) return "warn";        // 全索引扫描 / 范围
        if (s.indexOf("scan") >= 0 || s.indexOf("ref") >= 0 || s === "const"
            || s === "system" || s.indexOf("lookup") >= 0) return "good";   // 命中索引/主键
        return "mut";
      },
      // 访问类型归一化标签（全表扫描等以中文醒目呈现，其余保留原值）
      accessLabel: function (a) {
        var s = (a || "").toLowerCase();
        if (s === "all" || s === "table") return "全表扫描";
        if (s.indexOf("seq scan") >= 0) return "全表扫描";
        if (s === "index") return "全索引扫描";
        return a || "—";
      },
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
                    pinned: false, snippetId: opts.snippetId || null,   // 已保存到服务端片段库的 id（⌘S 覆盖同一条）
                    snipNote: opts.snipNote || "",                     // 片段备注（覆盖保存时保留，不被清空）
                    savedSql: opts.sql || "", dirty: false,            // 未保存改动标记（标题后 *）
                    bookmarks: opts.bookmarks || [],                   // 书签行号（编辑器字形边栏）
                    // data tab：WHERE 条 / ORDER BY 表达式（走 SQL 重查，跨页正确）
                    where: opts.where || "", orderBy: "", lastPage: 0,
                    pendingSql: null, readSql: null, explain: null, edit: null,
                    wfName: opts.wfName || "", wfSteps: null, vsel: null,
                    view: "table", chart: null,
                    graph: opts.graph || null, sel: null, nodeStatus: {},
                    rowSel: {}, lastSelRi: -1, newRow: null, resQ: null,
                    // 暂存式编辑：改动先攒着，工具栏「提交」才写库
                    edits: {}, dels: {}, adds: [], submit: null, submitting: false, refreshWarn: false,
                    colDisplay: opts.colDisplay || {},   // 列显示类型（仅展示，不写库）
                    // 异步任务态：开跑时刻（客户端秒表用）+ 被执行语句的字形状态标记 + 上次结果状态
                    jobRunAt: 0, execMarks: [], execIdx: 0,   // execMarks: [{line,state}]，多语句时每条一个
                    seq: null,   // 多语句顺序执行状态 {list, i}（瞬时，不持久化）
                    results: [], resultIdx: 0, isPaging: false };  // 结果 tab（每次执行新增一个）
        this.tabs.push(tab);
        if (monacoReady) models.set(id, window.monaco.editor.createModel(tab.sql, "sql"));
        this.switchTab(id);
        this.persist();
        return tab;
      },
      // 每个 tab 显示所属连接 + 环境（防止本地/线上混淆）
      tabConn: function (t) {
        if (!t.conn) return { name: "无连接", env: "none" };
        if (t.conn.indexOf("analysis/") === 0) return { name: "⚗ " + t.conn.slice(9), env: "none" };
        var m = this.connections.find(function (c) { return c.value === t.conn; });
        return { name: m ? m.connection : t.conn.split("/").pop(), env: (m && m.environment) || "none" };
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
      // 当前编辑器内容 vs 上次保存快照 → 更新活动 tab 的改动标记（标题后 *）
      markDirty: function () {
        var t = this.activeTab;
        if (t && t.type === "query") t.dirty = (this.sqlOf(t) !== (t.savedSql || ""));
      },
      // ---- tab 就地改名（双击标题 / 右键菜单）----
      beginRename: function (id) {
        var t = this.tabs.find(function (x) { return x.id === id; });
        if (!t) return;
        this.closeTabCtx();
        this.renamingId = id; this.renameVal = t.title;
        var self = this;
        this.$nextTick(function () {
          var el = document.querySelector(".dg-tab .rename-in");
          if (el) { el.focus(); el.select(); }
        });
      },
      commitRename: function () {
        var id = this.renamingId; if (id == null) return;
        var t = this.tabs.find(function (x) { return x.id === id; });
        this.renamingId = null;
        var name = (this.renameVal || "").trim();
        if (!t || !name || name === t.title) return;
        t.title = name;
        // 若该 tab 已存为片段，同步改片段标题（用已保存的 SQL，不提交未保存改动）
        if (t.snippetId) {
          var self = this;
          apiPost("/admin/sql/snippets/save", {
            id: t.snippetId, title: name, note: t.snipNote || "",
            sql: t.savedSql || this.sqlOf(t), connection: t.conn
          }).then(function (d) { if (d.ok) self.loadSnippets(); });
        }
        this.persist();
      },
      cancelRename: function () { this.renamingId = null; },
      // ---- 编辑区 tab 右键菜单 ----
      openTabCtx: function (e, id) {
        e.preventDefault();
        this.tabCtx = { show: true, x: Math.min(e.clientX, window.innerWidth - 170), y: e.clientY + 4, id: id };
      },
      closeTabCtx: function () { this.tabCtx.show = false; },
      tabCtxTarget: function () { var id = this.tabCtx.id; return this.tabs.find(function (x) { return x.id === id; }); },
      tabCtxPin: function () { var t = this.tabCtxTarget(); if (t) this.togglePin(t.id); this.closeTabCtx(); },
      tabCtxClose: function () { var id = this.tabCtx.id; this.closeTabCtx(); this.closeTab(id); },
      closeOthers: function (id) {
        this.closeTabCtx();
        var self = this;
        this.tabs.filter(function (t) { return t.id !== id && !t.pinned; })
                 .forEach(function (t) { self.closeTab(t.id); });
        if (this.activeId !== id && this.tabs.some(function (t) { return t.id === id; })) this.switchTab(id);
      },
      closeAll: function () {
        this.closeTabCtx();
        var self = this;
        var kept = this.tabs.filter(function (t) { return t.pinned; });
        this.tabs.filter(function (t) { return !t.pinned; }).forEach(function (t) {
          var m = models.get(t.id); if (m) { m.dispose(); models.delete(t.id); }
        });
        this.tabs = kept;
        if (!this.tabs.length) { this.newTab({}); return; }
        this.switchTab(this.tabs[0].id);
        this.persist();
      },
      toggleAcc: function (key) {
        this.acc[key] = !this.acc[key];
        if (key === "hist" && this.acc.hist && !this.history.length) this.loadHistory();
        this.persistAcc();
      },
      // ---- 编辑器书签 ----
      applyBookmarks: function () {
        if (!bmCollection || !window.monaco) return;
        var t = this.activeTab, lines = (t && t.bookmarks) || [];
        bmCollection.set(lines.map(function (ln) {
          return { range: new window.monaco.Range(ln, 1, ln, 1),
            options: { glyphMarginClassName: "dg-bm-glyph", glyphMarginHoverMessage: { value: "书签（点击切换）" },
              isWholeLine: true, className: "dg-bm-linebg",
              overviewRuler: { color: "#e0a83e", position: window.monaco.editor.OverviewRulerLane.Left } } };
        }));
      },
      toggleBookmark: function (line) {
        var t = this.activeTab; if (!t || !editor) return;
        if (line == null) { var p = editor.getPosition(); line = p ? p.lineNumber : 1; }
        if (!t.bookmarks) t.bookmarks = [];
        var i = t.bookmarks.indexOf(line);
        if (i >= 0) t.bookmarks.splice(i, 1); else t.bookmarks.push(line);
        t.bookmarks.sort(function (a, b) { return a - b; });
        this.bmTick++; this.applyBookmarks(); this.persist();
      },
      clearBookmarks: function () {
        var t = this.activeTab; if (!t) return;
        t.bookmarks = []; this.applyBookmarks(); this.persist(); this.bmOpen = false;
      },
      // 跳到书签行（放置光标，方便 ⌘Enter 执行该句）
      jumpBookmark: function (ln) {
        if (!editor) return;
        editor.setPosition({ lineNumber: ln, column: 1 });
        editor.revealLineInCenter(ln); editor.focus(); this.bmOpen = false;
      },
      // 跳到书签行并直接执行该句（光标处执行）
      runBookmark: function (ln) {
        if (!editor) return;
        editor.setPosition({ lineNumber: ln, column: 1 }); this.bmOpen = false;
        var self = this; this.$nextTick(function () { self.run(false); });
      },
      gotoBookmark: function (dir) {
        var t = this.activeTab; if (!t || !t.bookmarks || !t.bookmarks.length || !editor) return;
        var cur = editor.getPosition() ? editor.getPosition().lineNumber : 1;
        var bm = t.bookmarks.slice().sort(function (a, b) { return a - b; });
        var target = null, k;
        if (dir > 0) { for (k = 0; k < bm.length; k++) { if (bm[k] > cur) { target = bm[k]; break; } } if (target == null) target = bm[0]; }
        else { for (k = bm.length - 1; k >= 0; k--) { if (bm[k] < cur) { target = bm[k]; break; } } if (target == null) target = bm[bm.length - 1]; }
        editor.setPosition({ lineNumber: target, column: 1 });
        editor.revealLineInCenter(target); editor.focus();
      },
      persistAcc: function () { try { localStorage.setItem("dbm-console-acc", JSON.stringify(this.acc)); } catch (e) {} },
      switchTab: function (id) {
        this.activeId = id;
        var t = this.activeTab;
        var m = models.get(id);
        if (editor && m) { editor.setModel(m); editor.updateOptions({ readOnly: !!t && t.type === "ddl" }); this.applyBookmarks(); this.applyExecGlyph(); this.applyStmtBox(); }
        if (t && t.conn !== this.lastLoadedConn) this.loadTree();
        var self = this;
        this.$nextTick(function () { if (editor && t && t.type === "query") editor.focus(); });
        if (t && t.view === "chart" && t.result) this.renderChart(); else this.disposeChart();
        if (t && t.type === "data") {
          this.loadHistory();  // 底部执行记录面板用
          // 预取表结构：切到（含刷新后恢复的）数据 tab 就备好列类型，
          // 时间列编辑才能立刻用日期选择器（否则首次编辑取不到类型退化成文本框）
          if (t.table) { var k = this.mk(t.table, t.schema); if (!this.tableMeta[k]) this.fetchMeta(t.table, t.schema); }
        }
        this.scheduleLint();
      },
      toggleTabGroup: function (conn) {
        this.tabGroupCollapsed[conn] = !this.tabGroupCollapsed[conn];
        try { localStorage.setItem("dbm-tabgrp-collapsed", JSON.stringify(this.tabGroupCollapsed)); } catch (e) {}
      },
      onTabDragStart: function (id, e) { this.dragGroup = null; this.dragId = id; if (e.dataTransfer) e.dataTransfer.effectAllowed = "move"; },
      onTabDrop: function (targetId) {
        var self = this;
        var fromT = this.tabs.find(function (t) { return t.id === self.dragId; });
        var toT = this.tabs.find(function (t) { return t.id === targetId; });
        this.dragId = null;
        if (!fromT || !toT || fromT === toT) return;
        var cross = (fromT.conn || "") !== (toT.conn || "");
        this.tabs.splice(this.tabs.indexOf(fromT), 1);
        this.tabs.splice(this.tabs.indexOf(toT), 0, fromT);   // 落在目标之前
        // 跨组拖动 = 把这个 tab 并入目标连接的组（并改到该连接）；同组则只是重排
        if (cross) this.moveTabToConn(fromT, toT.conn || "");
        this.persist();
      },
      // 把某个 tab 并入另一连接的组：改连接、清掉属于旧连接的结果/暂存改动（SQL/类型/表名保留）。
      // 让「分错组」的 tab 能被拖到正确的组；query tab 顺带获得「同一条 SQL 换库跑」的能力。
      moveTabToConn: function (tab, val) {
        if (!tab || (tab.conn || "") === (val || "")) return;
        tab.conn = val;
        tab.result = null; tab.ok = null; tab.err = null; tab.confirm = null; tab.explain = null;
        tab.readSql = null; tab.edits = {}; tab.dels = {}; tab.adds = []; tab.submit = null;
        var self = this;
        if (this.activeId === tab.id) this.$nextTick(function () { self.loadTree(); });   // 活动 tab 则刷新左树到新连接
      },
      onGroupDragStart: function (conn, e) { this.dragId = null; this.dragGroup = conn; if (e.dataTransfer) e.dataTransfer.effectAllowed = "move"; },
      onGroupDrop: function (targetConn) {
        // 拖的是单个 tab 落到组头上 → 把它并入这个连接的组（放到该组末尾）
        if (this.dragId != null) {
          var self = this; var fromT = this.tabs.find(function (t) { return t.id === self.dragId; });
          this.dragId = null;
          if (fromT && (fromT.conn || "") !== (targetConn || "")) {
            this.tabs.splice(this.tabs.indexOf(fromT), 1);
            var lastIdx = -1;
            this.tabs.forEach(function (t, i) { if ((t.conn || "") === (targetConn || "")) lastIdx = i; });
            this.tabs.splice(lastIdx + 1, 0, fromT);   // 目标组末尾
            this.moveTabToConn(fromT, targetConn || "");
            this.persist();
          }
          return;
        }
        // 拖的是组头 → 调整组（连接）的先后顺序：把整组移到目标组之前
        var g = this.dragGroup; this.dragGroup = null;
        if (g == null || g === targetConn) return;
        var moved = this.tabs.filter(function (t) { return (t.conn || "") === g; });
        var rest = this.tabs.filter(function (t) { return (t.conn || "") !== g; });
        var idx = rest.findIndex(function (t) { return (t.conn || "") === targetConn; });
        if (idx < 0) idx = rest.length;
        this.tabs = rest.slice(0, idx).concat(moved, rest.slice(idx));
        this.persist();
      },
      // 让扁平 tabs 数组按组连续排列（组顺序=首次出现），使拖动/分组行为可预期
      normalizeTabOrder: function () {
        var order = [], map = {};
        this.tabs.forEach(function (t) {
          var k = t.conn || ""; if (!(k in map)) { map[k] = []; order.push(k); } map[k].push(t);
        });
        var flat = []; order.forEach(function (k) { flat = flat.concat(map[k]); });
        this.tabs = flat;
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
      // 一个词是不是当前连接里已知的表？是则返回 {table, schema}（供 ⌘+点击跳 DDL）
      tableForWord: function (word) {
        if (!word) return null;
        if (Object.prototype.hasOwnProperty.call(tableSchema, word))
          return { table: word, schema: tableSchema[word] };
        if (currentTables.indexOf(word) >= 0) return { table: word, schema: "" };
        return null;
      },
      // schema 浮层右缘定位：停在 minimap 左侧，避免遮挡缩略图（minimap 关时贴编辑器右缘）
      syncSchemaFloat: function () {
        if (!editor) return;
        try {
          var info = editor.getLayoutInfo();
          var mm = info.minimap;
          // minimapLeft 是 minimap 区域左起点；width 为编辑器总宽 → 右缘偏移 = 总宽 - minimapLeft
          var off = (mm && mm.minimapWidth > 0) ? (info.width - mm.minimapLeft + 8) : 16;
          this.schemaFloatRight = Math.max(16, Math.round(off));
        } catch (e) { this.schemaFloatRight = 16; }
      },

      // ---------- 连接 / 树（带每连接快照缓存，保活） ----------
      loadConnections: function () {
        var self = this;
        return apiGet("/admin/sql/connections").then(function (d) {
          self.connections = (d && d.connections) || [];
          self.workspaces = (d && d.workspaces) || [];
          self.aiEnabled = !!(d && d.ai_enabled);
          self.loadWorkflows();
          if (!self.tabs.length) self.newTab({});
          else if (self.activeTab) self.loadTree();
        });
      },
      // 选连接 = 切到「该连接的组」：已有该连接的 tab 就激活最近一个，否则在它自己的组里新建空编辑器。
      // 当前 tab 原地不动、留在自己组里——绝不把它的连接改掉搬进别的组（否则打开新连接会把当前编辑器混进目标组）。
      setConn: function (val) {
        var t = this.activeTab; if (t && t.conn === val) return;
        var existing = null;
        for (var i = this.tabs.length - 1; i >= 0; i--)   // 取该连接最近（末尾）的 tab
          if ((this.tabs[i].conn || "") === val) { existing = this.tabs[i]; break; }
        if (existing) this.switchTab(existing.id);   // switchTab 会按新连接刷新左树
        else this.newTab({ conn: val });             // 新 tab 进入该连接自己的组
      },
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
      // 清理反射出来的类型串：去掉 collate/character set（字符编码不是类型，不展示）
      cleanType: function (ty) {
        return String(ty || "").replace(/\s+collate\s+\S+/ig, "").replace(/\s+character set\s+\S+/ig, "").trim();
      },
      fetchMeta: function (t, db) {
        var self = this, tab = this.activeTab, k = this.mk(t, db);
        return apiGet("/admin/sql/table?conn=" + encodeURIComponent(tab.conn) + "&table=" + encodeURIComponent(t)
               + (db ? "&schema=" + encodeURIComponent(db) : "")).then(function (d) {
          if (!d.ok) { self.flash(d.error); return; }
          var cols = (d.columns || []).map(function (c) {
            return Object.assign({}, c, { type: self.cleanType(c.type) });  // 类型去掉字符编码
          });
          self.tableMeta[k] = { columns: cols, indexes: d.indexes || [],
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

      // ---------- AI 辅助生成 SQL（只生成、不执行；产物插到光标处） ----------
      openAiPanel: function () {
        var t = this.activeTab; if (!t || !t.conn) { this.flash("请先选择连接"); return; }
        this.aiPanel = { question: "", explain: false, samples: false,
                         schema: this.aiSchema() || (this.databases.length ? this.databases[0] : ""),
                         tables: [], picked: {}, filter: "", loading: true,
                         running: false, error: "", sessionId: "", turns: [],
                         mode: "replace", lastRange: null, pos: { left: 10, top: 8 } };
        var self = this;
        this.$nextTick(function () {  // 默认停在编辑器右上角（视口坐标，position:fixed）
          var host = self.$refs.editorEl && self.$refs.editorEl.parentElement;
          if (host && self.aiPanel) {
            var r = host.getBoundingClientRect();
            self.aiPanel.pos.left = Math.max(10, r.right - 448);
            self.aiPanel.pos.top = r.top + 8;
          }
        });
        this.aiLoadTables();
      },
      closeAiPanel: function () { this.aiPanel = null; },
      aiSchema: function () {
        var t = this.activeTab; if (!t) return "";
        return t.schema || this.schemaDefault[t.conn] || "";
      },
      aiLoadTables: function () {
        var self = this, p = this.aiPanel, t = this.activeTab; if (!t || !p) return;
        p.loading = true; p.tables = []; p.error = "";
        var qs = "?conn=" + encodeURIComponent(t.conn);
        if (p.schema) qs += "&schema=" + encodeURIComponent(p.schema);
        apiGet("/admin/sql/tables" + qs).then(function (d) {
          if (!self.aiPanel) return;
          p.loading = false;
          if (d && d.ok) p.tables = d.tables || [];
          else p.error = (d && d.error) || "无法加载表列表";
        }).catch(function (e) {
          if (self.aiPanel) { p.loading = false; p.error = String(e); }
        });
      },
      // 切换面板浏览的 schema：重拉表列表，但保留已勾选（picked 存的是 schema.table 限定名，跨库累计）
      aiSetSchema: function (sc) {
        var p = this.aiPanel; if (!p || p.schema === sc) return;
        p.schema = sc; p.filter = ""; this.aiLoadTables();
      },
      // 当前浏览 schema 下把裸表名限定为 schema.table（无 schema 则原样）
      aiQual: function (name) {
        var p = this.aiPanel; return (p && p.schema) ? p.schema + "." + name : name;
      },
      aiTogglePick: function (name) {
        var p = this.aiPanel; if (!p) return;
        var q = this.aiQual(name);
        if (p.picked[q]) delete p.picked[q]; else p.picked[q] = true;
      },
      aiTogglePickQual: function (q) {  // 从已选 chip 移除
        var p = this.aiPanel; if (p && p.picked[q]) delete p.picked[q];
      },
      aiPickCount: function () { return this.aiPanel ? Object.keys(this.aiPanel.picked).length : 0; },
      aiPickedList: function () { return this.aiPanel ? Object.keys(this.aiPanel.picked) : []; },
      aiVisibleTables: function () {
        var p = this.aiPanel; if (!p) return [];
        var f = (p.filter || "").toLowerCase().trim();
        if (!f) return p.tables;
        return p.tables.filter(function (t) { return t.toLowerCase().indexOf(f) >= 0; });
      },
      aiWrapComment: function (expl) {
        // 解释以 -- 注释写在 SQL 上方：完整展示（不截断），按 ~40 字折行，但不切断英文单词/标识符。
        // 切成单元：ASCII 词/数字/标识符整体保留，中文与标点逐字符——这样只在单元之间断行。
        var width = 40;
        var txt = (expl || "").replace(/\s+/g, " ").trim();
        if (!txt) return "";
        var units = txt.match(/[A-Za-z0-9_.]+|\s|[^\sA-Za-z0-9_.]/g) || [];
        var lines = [], cur = "";
        units.forEach(function (u) {
          if (u === " ") { if (cur) cur += " "; return; }
          if (cur.length + u.length > width && cur.length) { lines.push(cur); cur = ""; }
          cur += u;
        });
        if (cur) lines.push(cur);
        return lines.map(function (l) { return "-- " + l.replace(/\s+$/, ""); }).join("\n") + "\n";
      },
      aiDragStart: function (e) {
        var p = this.aiPanel; if (!p) return;
        var sx = e.clientX, sy = e.clientY, sl = p.pos.left, st = p.pos.top;
        function move(ev) {
          p.pos.left = Math.max(0, sl + ev.clientX - sx);
          p.pos.top = Math.max(0, st + ev.clientY - sy);
        }
        function up() {
          window.removeEventListener("mousemove", move);
          window.removeEventListener("mouseup", up);
        }
        window.addEventListener("mousemove", move);
        window.addEventListener("mouseup", up);
      },
      aiInsertAt: function (text, range) {
        // range=null 插到光标处；否则替换该区间。返回插入后文本所占的新区间。
        if (!editor) return null;
        var mono = window.monaco;
        var r = range ? new mono.Range(range.startLineNumber, range.startColumn,
                                       range.endLineNumber, range.endColumn)
                      : editor.getSelection();
        var sl = r.startLineNumber, sc = r.startColumn;
        editor.executeEdits("dbm-ai", [{ range: r, text: text, forceMoveMarkers: true }]);
        var lines = text.split("\n");
        var el = sl + lines.length - 1;
        var ec = lines.length === 1 ? sc + text.length : lines[lines.length - 1].length + 1;
        editor.focus();
        return { startLineNumber: sl, startColumn: sc, endLineNumber: el, endColumn: ec };
      },
      aiGenerate: function () {
        var self = this, p = this.aiPanel, t = this.activeTab;
        if (!p || !t || p.running) return;
        var q = (p.question || "").trim();
        if (!q) { p.error = "请填写你想查什么"; return; }
        var picked = Object.keys(p.picked);
        p.running = true; p.error = "";
        var body = { conn: t.conn, question: q,
                     tables: JSON.stringify(picked),
                     explain: p.explain ? "1" : null,
                     include_samples: p.samples ? "1" : null,
                     session_id: p.sessionId || null };
        // picked 已是 schema.table 限定名（跨库累计）；schema 仅作「整库」模式的默认库
        var sc = p.schema || this.aiSchema(); if (sc) body.schema = sc;
        apiPost("/admin/sql/ai", body).then(function (d) {
          if (!self.aiPanel) return;
          p.running = false;
          if (!d || !d.ok) { p.error = (d && d.error) || "生成失败"; return; }
          var text = "";
          if (p.explain && d.explanation) text = self.aiWrapComment(d.explanation);
          text += (d.sql || "").replace(/\s+$/, "") + "\n";
          // 首轮：插到光标处。追问：按选择「替换上一条」或「追加在后面」。
          if (p.sessionId && p.lastRange && p.mode === "replace") {
            p.lastRange = self.aiInsertAt(text, p.lastRange);
          } else if (p.sessionId && p.lastRange && p.mode === "append") {
            var end = { startLineNumber: p.lastRange.endLineNumber, startColumn: p.lastRange.endColumn,
                        endLineNumber: p.lastRange.endLineNumber, endColumn: p.lastRange.endColumn };
            p.lastRange = self.aiInsertAt("\n" + text, end);
          } else {
            p.lastRange = self.aiInsertAt(text, null);
          }
          // 保持会话：记录本轮、带回 session_id，面板不关，可继续追问
          p.turns.push(q);
          if (d.session_id) p.sessionId = d.session_id;
          p.question = "";
        }).catch(function (e) { if (self.aiPanel) { p.running = false; p.error = String(e); } });
      },

      // ---------- AI 生成流程（DAG 画布，只生成不执行） ----------
      openWfAi: function () {
        var conn = this.realConnOptions[0] ? this.realConnOptions[0].value : "";
        this.wfAi = { question: "", conn: conn, tables: [], picked: {}, filter: "",
                      loading: !!conn, running: false, error: "", pos: { left: 60, top: 70 } };
        if (conn) this.wfAiLoadTables();
      },
      wfAiSetConn: function (v) {
        if (!this.wfAi) return;
        this.wfAi.conn = v; this.wfAi.picked = {}; this.wfAi.tables = []; this.wfAi.loading = true;
        this.wfAiLoadTables();
      },
      wfAiLoadTables: function () {
        var self = this, w = this.wfAi; if (!w || !w.conn) return;
        apiGet("/admin/sql/tables?conn=" + encodeURIComponent(w.conn)).then(function (d) {
          if (!self.wfAi) return;
          self.wfAi.loading = false;
          if (d && d.ok) self.wfAi.tables = d.tables || [];
          else self.wfAi.error = (d && d.error) || "无法加载表列表";
        }).catch(function (e) { if (self.wfAi) { self.wfAi.loading = false; self.wfAi.error = String(e); } });
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
      wfAiDragStart: function (e) {
        var w = this.wfAi; if (!w) return;
        var sx = e.clientX, sy = e.clientY, sl = w.pos.left, st = w.pos.top;
        function mv(ev) { w.pos.left = Math.max(0, sl + ev.clientX - sx); w.pos.top = Math.max(0, st + ev.clientY - sy); }
        function up() { window.removeEventListener("mousemove", mv); window.removeEventListener("mouseup", up); }
        window.addEventListener("mousemove", mv); window.addEventListener("mouseup", up);
      },
      wfAiGenerate: function () {
        var self = this, w = this.wfAi, t = this.activeTab;
        if (!w || !t || w.running) return;
        if (!(w.question || "").trim()) { w.error = "请描述你想做的流程"; return; }
        if (!w.conn) { w.error = "请选择取数连接"; return; }
        w.running = true; w.error = "";
        apiPost("/admin/workflows/ai", { conn: w.conn, question: w.question,
                                         tables: JSON.stringify(Object.keys(w.picked)) }).then(function (d) {
          if (!self.wfAi) return;
          w.running = false;
          if (!d || !d.ok) { w.error = (d && d.error) || "生成失败"; return; }
          t.graph = { nodes: (d.graph && d.graph.nodes) || [], edges: (d.graph && d.graph.edges) || [] };
          t.sel = null; t.nodeStatus = {};
          self.wfAi = null;
          self.persist();
          self.flash("已生成流程，请审阅后点「运行流程」");
        }).catch(function (e) { if (self.wfAi) { w.running = false; w.error = String(e); } });
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
      // 被执行语句的起始行号（字形边栏放执行状态图标）：选区取选区首行；单语句取首个非空字符行；
      // 多语句取光标所在语句的起始行。
      execLineFor: function () {
        if (!editor) return 1;
        var sel = editor.getSelection();
        if (sel && !sel.isEmpty()) return sel.startLineNumber;
        var model = editor.getModel(), text = model.getValue();
        var ranges = stmtRanges(text);
        var off;
        if (ranges.length <= 1) { var m = text.match(/\S/); off = m ? m.index : 0; }
        else {
          var cur = model.getOffsetAt(editor.getPosition()), k = ranges.length - 1;
          for (var i = 0; i < ranges.length; i++) { if (cur <= ranges[i].e) { k = i; break; } }
          off = ranges[k].s;
        }
        // 语句区间起点可能落在前一句结尾的换行/空白上 → 前进到首个非空白字符，图标才对准语句行
        while (off < text.length && /\s/.test(text[off])) off++;
        return model.getPositionAt(off).lineNumber;
      },
      // 选区里跨多条语句 → 返回 [{sql, line}]（line=该语句在编辑器里的起始行，供每条独立字形状态）；
      // 无选区或只有一条则返回 null（走普通单条执行）。
      seqItems: function () {
        if (!editor) return null;
        var sel = editor.getSelection();
        if (!sel || sel.isEmpty()) return null;
        var model = editor.getModel();
        var selText = model.getValueInRange(sel);
        var ranges = stmtRanges(selText);
        if (ranges.length <= 1) return null;
        var base = model.getOffsetAt(sel.getStartPosition());
        var out = [];
        ranges.forEach(function (r) {
          var raw = selText.slice(r.s, r.e);
          var stmt = raw.replace(/;\s*$/, "").trim();
          if (!stmt) return;
          var lead = raw.match(/^\s*/)[0].length;   // 跳过前导空白，行号对准语句首行
          out.push({ sql: stmt, line: model.getPositionAt(base + r.s + lead).lineNumber });
        });
        return out;
      },
      // 光标所在语句的内容区间（去前后空白）+ 是否缺尾分号，供「当前语句边框高亮」用。
      stmtRangeAtCursor: function () {
        if (!editor) return null;
        var model = editor.getModel(); if (!model) return null;
        var text = model.getValue();
        var ranges = stmtRanges(text);
        if (!ranges.length) return null;
        var off = model.getOffsetAt(editor.getPosition());
        var r = null;
        for (var i = 0; i < ranges.length; i++) { if (off <= ranges[i].e) { r = ranges[i]; break; } }
        if (!r) r = ranges[ranges.length - 1];
        var s = r.s, e = r.e;
        while (s < e && /\s/.test(text[s])) s++;         // 去前导空白/换行
        while (e > s && /\s/.test(text[e - 1])) e--;      // 去尾部空白/换行
        if (s >= e) return null;
        return { start: model.getPositionAt(s), end: model.getPositionAt(e), noSemi: text[e - 1] !== ";" };
      },
      // 当前语句边框高亮：框住 ⌘↵ 将执行的那条 SQL；缺尾分号时末尾加轻微波浪线。
      // 有选区时不画（选区本身就是执行目标，且和框选视觉冲突）。
      // 拿当前编辑器里的边框 overlay（按 DOM 现查现取/现建，避免缓存到失效的旧节点）
      _boxEl: function () {
        if (!editor || !editor.getDomNode) return null;
        var dom = editor.getDomNode(); if (!dom) return null;
        var box = dom.querySelector(".dg-stmt-boxel");
        if (box) return box;
        var guard = dom.querySelector(".overflow-guard"); if (!guard) return null;
        box = document.createElement("div");
        box.className = "dg-stmt-boxel";
        guard.appendChild(box);
        return box;
      },
      applyStmtBox: function () {
        if (!stmtBoxCol || !window.monaco || !editor) return;
        var monaco = window.monaco, t = this.activeTab, sel = editor.getSelection();
        var boxEl = this._boxEl();
        var hide = function () { stmtBoxCol.clear(); if (boxEl) boxEl.style.display = "none"; };
        if (!t || t.type !== "query" || (sel && !sel.isEmpty())) { hide(); return; }
        var info = this.stmtRangeAtCursor();
        if (!info) { hide(); return; }
        var model = editor.getModel(), s = info.start.lineNumber, e = info.end.lineNumber;
        // 外框：贴合语句内容的包围盒（右缘=最宽那行的行尾，而非整个编辑器宽度）
        if (boxEl) {
          var stmtBoxEl = boxEl;
          var st = editor.getScrollTop(), lh = editor.getOption(monaco.editor.EditorOption.lineHeight);
          var cl = editor.getLayoutInfo().contentLeft, sl = editor.getScrollLeft();
          var fi = editor.getOption(monaco.editor.EditorOption.fontInfo);
          var top = editor.getTopForLineNumber(s) - st;
          var bottom = editor.getTopForLineNumber(e) + lh - st;
          var left = cl - sl;
          // 右缘：取各行行尾像素的最大值（getScrolledVisiblePosition 精确含中文全角），
          // 都不可见时退回等宽字符估算
          var right = null;
          for (var ln = s; ln <= e; ln++) {
            var vp = editor.getScrolledVisiblePosition({ lineNumber: ln, column: model.getLineMaxColumn(ln) });
            if (vp) right = right == null ? vp.left : Math.max(right, vp.left);
          }
          if (right == null) {
            var maxCol = 1;
            for (var l2 = s; l2 <= e; l2++) maxCol = Math.max(maxCol, model.getLineMaxColumn(l2));
            right = cl + (maxCol - 1) * (fi.typicalHalfwidthCharacterWidth || 7.2) - sl;
          }
          stmtBoxEl.style.left = (left - 3) + "px";
          stmtBoxEl.style.top = top + "px";
          stmtBoxEl.style.width = Math.max(20, right - left + 8) + "px";
          stmtBoxEl.style.height = (bottom - top) + "px";
          stmtBoxEl.style.display = "block";
        }
        // 缺分号：末字符加轻微波浪线（非报错，仅提示）
        if (info.noSemi && info.end.column > 1) {
          stmtBoxCol.set([{ range: new monaco.Range(info.end.lineNumber, info.end.column - 1,
            info.end.lineNumber, info.end.column), options: { className: "dg-stmt-nosemi" } }]);
        } else stmtBoxCol.clear();
      },
      // 执行状态字形："run"（转圈）/"ok"（✓）/"err"（✗）/""（清除），画在被执行语句行左侧字形边栏。
      // 更新「当前条」（execIdx）的状态；多语句顺序执行时每条各有一个 mark、各自保留 run→✓/✗。
      setExecGlyph: function (t, state) {
        if (t.type === "query") {
          if (!state) t.execMarks = [];
          else if (t.execMarks && t.execMarks[t.execIdx]) t.execMarks[t.execIdx].state = state;
        }
        if (t.id === this.activeId) this.applyExecGlyph();
      },
      applyExecGlyph: function () {
        if (!execCollection || !window.monaco) return;
        var t = this.activeTab;
        if (!t || t.type !== "query" || !t.execMarks || !t.execMarks.length) { execCollection.clear(); return; }
        var cmap = { run: "dg-exec-run", ok: "dg-exec-ok", err: "dg-exec-err" };
        var decos = [];
        t.execMarks.forEach(function (m) {
          var cls = cmap[m.state];
          if (cls && m.line) decos.push({ range: new window.monaco.Range(m.line, 1, m.line, 1),
            options: { glyphMarginClassName: cls, isWholeLine: false } });
        });
        execCollection.set(decos);
      },
      run: function (confirm, page, sqlOverride, isPage) {
        var self = this, t = this.activeTab;
        if (!t) return;
        if (t.type === "ddl") { this.flash("DDL 为只读视图"); return; }
        if (t.type === "flow" && sqlOverride == null) { this.runFlow(); return; }
        if (!t.conn) { this.flash("请先选择连接"); return; }
        // 同一 tab 已有查询在执行 → 直接拒绝（不排队、不并发），提示先取消
        if (t.running) { this.flash("当前查询仍在执行，请先取消或等待完成"); return; }
        t.isPaging = !!isPage;  // 翻页更新当前结果 tab；否则新执行=新结果 tab
        // 确认执行：捕获确认元数据（指纹 H1），下面 t.confirm 会被清空
        var confData = confirm ? t.confirm : null;
        var sql;
        if (sqlOverride != null) sql = sqlOverride;
        else if (confirm && t.pendingSql) sql = t.pendingSql;   // 确认执行的是刚才那条
        else if (t.type === "data" && t.table) sql = this.buildDataSql(t);
        else sql = this.stmtAtCursor();
        if (!sql.trim()) { this.flash("请输入 SQL"); return; }
        page = page || 0;
        // 编辑器里选中多条语句 → 拆开按顺序逐条执行（每条一个结果页 + 各自独立的执行状态图标），
        // 不整体当成写操作被拒。递归调用时带 sqlOverride，已是单条、不再进这个分支。
        if (sqlOverride == null && t.type === "query" && !isPage) {
          var items = this.seqItems();
          if (items && items.length > 1) {
            t.seq = { list: items.map(function (x) { return x.sql; }), i: 0 };
            t.execMarks = items.map(function (x) { return { line: x.line, state: "" }; });
            t.execIdx = 0;
            this.run(false, 0, items[0].sql);
            return;
          }
          t.seq = null;
        }
        t.pendingSql = sql;
        t.running = true; t.err = null; t.ok = null; t.confirm = null; t.explain = null; t.edit = null; t.wfSteps = null;
        t.jobRunAt = Date.now();  // 客户端秒表起点
        // 执行状态字形定位：序列中沿用建立时算好的每条 marks（只切当前条）；
        // 普通单条执行在光标语句行放一个 mark；翻页/重载沿用上次 marks。
        if (t.type === "query") {
          if (t.seq) t.execIdx = t.seq.i;
          else if (sqlOverride == null && !isPage) { t.execMarks = [{ line: this.execLineFor(), state: "" }]; t.execIdx = 0; }
        }
        this.setExecGlyph(t, "run");
        t.rowSel = {}; t.lastSelRi = -1; t.resQ = null;  // 重查后行号会变，行选择/搜索作废
        if (page === 0) t.result = null;
        // 异步任务：查询在服务端执行，切页/刷新不中断；job_id 持久化，回来续接轮询。
        // 数据 tab（双击表名打开）用 parallel=1 → 服务端独立 key 并行，不占用连接串行名额。
        apiPost("/admin/sql/run_async", { conn: t.conn, sql: sql, confirm: confirm ? "1" : null,
                                          page: page, schema: t.schema || null,
                                          parallel: t.type === "data" ? "1" : null,
                                          expect_fingerprint: confData ? (confData.fingerprint || null) : null })
          .then(function (d) {
            // 连接忙被拒绝 / 其它提交错误 → 作为一个「出错结果页」呈现（不是顶部横幅）
            if (!d.ok) { t.running = false; self.setExecGlyph(t, "err"); self.pushOutcome(t, sql, { err: d.error }); self.persist(); return; }
            t.jobId = d.job_id; t.jobPage = page;
            self.persist();
            self.pollJob(t.id, d.job_id, page);
          }).catch(function (e) { t.running = false; self.setExecGlyph(t, "err"); self.pushOutcome(t, sql, { err: "" + e }); });
      },
      pollJob: function (tabId, jobId, page) {
        var self = this;
        var t = this.tabs.find(function (x) { return x.id === tabId; });
        if (!t || t.jobId !== jobId) return;  // tab 已关/已发起新查询
        apiGet("/admin/sql/job?id=" + jobId).then(function (d) {
          var t2 = self.tabs.find(function (x) { return x.id === tabId; });
          if (!t2 || t2.jobId !== jobId) return;
          if (!d.ok) { t2.running = false; t2.jobId = null; self.setExecGlyph(t2, "err"); self.pushOutcome(t2, t2.pendingSql, { err: d.error || "任务丢失" }); self._seqAdvance(t2, false); self.persist(); return; }
          if (d.status === "running") {
            t2.running = true;
            // 客户端秒表锚定到服务端真实耗时（刷新后续接、以及首轮校准都准确，避免走得偏快/偏慢）
            if (!t2.jobRunAt) t2.jobRunAt = Date.now() - (d.elapsed_ms || 0);
            setTimeout(function () { self.pollJob(tabId, jobId, page); }, 400);
            return;
          }
          t2.running = false; t2.jobId = null;
          // 出错 / 取消 → 也进一个结果页（而非顶部横幅），与成功结果统一在结果 tab 里
          if (d.status === "canceled") { self.setExecGlyph(t2, "err"); self.pushOutcome(t2, t2.pendingSql, { err: d.error || "已取消" }); self._seqAdvance(t2, false); self.persist(); return; }
          if (d.status === "error") { self.setExecGlyph(t2, "err"); self.pushOutcome(t2, t2.pendingSql, { err: d.error }); self._seqAdvance(t2, false); self.persist(); return; }
          var r = d.result || {};
          if (r.kind === "workflow") {
            // workflow 运行结果：步骤清单 + 输出预览（输出复用结果表格）
            self.setExecGlyph(t2, r.ok ? "ok" : "err");
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
          else if (r.kind === "read") {
            t2.readSql = t2.pendingSql; t2.lastPage = page;
            self.setExecGlyph(t2, "ok");
            if (t2.type === "data" && t2.table) {
              t2.result = r;
              // 数据 tab 预取表结构，保证时间列编辑能用日期选择器（依赖列类型）
              var mk = self.mk(t2.table, t2.schema);
              if (!self.tableMeta[mk]) self.fetchMeta(t2.table, t2.schema);
              if (t2.id === self.activeId) self.loadHistory();  // 底部执行记录及时反映刚才的过滤/排序 SQL
            } else {
              // 查询 tab：每次新执行新增一个结果 tab；翻页只更新当前结果 tab
              self.pushOutcome(t2, t2.pendingSql, { result: r });
            }
          }
          else if (r.kind === "error") {
            // 后端判定的语法错误（ParseError）：直接报错，不弹「确认写操作」
            self.setExecGlyph(t2, "err");
            self.pushOutcome(t2, t2.pendingSql, { err: r.error || "SQL 语法错误" });
            t2.confirm = null;
          }
          else if (r.kind === "confirm") {
            self.setExecGlyph(t2, "");  // 待人工确认，非终态
            // 存下指纹（H1 确认时回传绑定）+ prod 标记（仅用于红色视觉警示）
            t2.confirm = { risk: r.risk || {}, statement_kind: r.statement_kind,
                           fingerprint: r.fingerprint || "", prod: !!r.prod };
          }
          else if (r.kind === "write") {
            self.setExecGlyph(t2, "ok");
            t2.ok = r;
            if (t2.type === "data") setTimeout(function () { self.run(false, t2.lastPage); }, 60);
            else self.refreshTree();
          }
          // 多语句顺序执行：本条读/写成功即跑下一条；confirm（写需人工确认）在此停住等确认
          self._seqAdvance(t2, r.kind === "read" || r.kind === "write");
          self.persist();
          if (t2.id === self.activeId && t2.view === "chart" && t2.result) self.renderChart();
        }).catch(function () {  // 网络抖动：稍后重试
          setTimeout(function () { self.pollJob(tabId, jobId, page); }, 1200);
        });
      },
      cancelJob: function () {
        var t = this.activeTab;
        if (!t || !t.jobId) return;
        var self = this, jid = t.jobId;
        apiPost("/admin/sql/cancel", { id: jid }).then(function (d) {
          // 排队中会立刻变 canceled；运行中发出 KILL 后由下一次轮询收敛为 canceled
          self.flash(d.ok ? "已请求取消" : "任务已结束，无需取消");
        }).catch(function (e) { self.flash("取消失败：" + e); });
      },
      // 多语句顺序执行的推进：上一条成功(ok=true)则跑下一条；失败/取消/需确认则中止整个序列。
      _seqAdvance: function (t, ok) {
        if (!t || !t.seq) return;
        if (!ok) { t.seq = null; return; }
        t.seq.i++;
        if (t.seq.i >= t.seq.list.length) { t.seq = null; return; }
        var self = this, next = t.seq.list[t.seq.i];
        setTimeout(function () { self.run(false, 0, next); }, 30);
      },
      goPage: function (p) {
        var t = this.activeTab;
        if (p < 0 || !t) return;
        // 翻页沿用上次执行的读语句（光标可能已移动到别的语句上），isPage=true → 更新当前结果 tab
        this.run(false, p, t.type === "data" ? null : t.readSql, true);
      },
      // 把一次执行结果（成功/出错/取消）落成结果页：查询 tab 进结果 tab 条；其它类型 tab 用横幅
      pushOutcome: function (t, sql, o) {
        var err = o.err != null ? o.err : null, result = o.result || null;
        if (t.type !== "query") { t.err = err; t.result = err ? null : result; return; }
        t.err = err; t.result = result;
        if (t.isPaging && t.results[t.resultIdx]) {
          var e = t.results[t.resultIdx]; e.sql = sql; e.result = result; e.err = err;  // 翻页/重载更新当前结果页
        } else {
          var nid = t.results.length ? t.results[t.results.length - 1].rid + 1 : 1;
          t.results.push({ rid: nid, sql: sql, result: result, err: err });
          if (t.results.length > 10) t.results.shift();  // 上限，防 localStorage 膨胀
          t.resultIdx = t.results.length - 1;
        }
      },
      // 结果 tab（查询 tab 每次执行新增一个）——点击结果页切换展示
      selectResult: function (i) {
        var t = this.activeTab, entry = t && t.results[i]; if (!entry) return;
        t.resultIdx = i; t.readSql = entry.sql; t.ok = null;
        if (entry.err) { t.err = entry.err; t.result = null; this.persist(); return; }
        t.err = null;
        if (entry.result) {
          t.result = entry.result; this.persist();
          if (t.view === "chart") this.$nextTick(this.renderChart);
        } else {
          // 刷新后被释放的历史结果 → 用户点击时才重跑该 SQL 恢复（isPaging=true 更新本结果 tab）
          t.result = null;
          this.run(false, 0, entry.sql, true);
        }
      },
      // 关闭结果页：只切换到相邻页展示，**绝不触发重查**（历史 bug：关一个页会把邻页重跑一遍）
      closeResult: function (i) {
        var t = this.activeTab; if (!t || !t.results[i]) return;
        var beforeActive = i < t.resultIdx;
        t.results.splice(i, 1);
        if (!t.results.length) { t.result = null; t.err = null; t.ok = null; t.resultIdx = 0; this.persist(); return; }
        var ni = beforeActive ? t.resultIdx - 1 : Math.min(t.resultIdx, t.results.length - 1);
        var entry = t.results[ni];
        t.resultIdx = ni; t.readSql = entry.sql; t.ok = null;
        t.err = entry.err || null;
        t.result = entry.err ? null : (entry.result || null);  // 被释放的历史结果显示占位，可点该页手动重跑
        this.persist();
        if (t.result && t.view === "chart") this.$nextTick(this.renderChart);
      },
      confirmRun: function () {
        var t = this.activeTab; if (!t || !t.confirm) return;
        this.run(true);  // run() 会捕获 t.confirm 后再清空，指纹随请求带出
      },
      // data tab：WHERE 条应用 / 列头点击循环排序（走 SQL 重查第 0 页）
      applyWhere: function () { this.run(false, 0); },
      // ---------- WHERE / ORDER BY 字段提示 ----------
      filterCols: function () {
        var t = this.activeTab; if (!t) return [];
        if (t.result && t.result.columns && t.result.columns.length) return t.result.columns;
        var meta = this.tableMeta[this.mk(t.table, t.schema)];
        return meta ? meta.columns.map(function (c) { return c.name; }) : [];
      },
      onFilterInput: function (which, e) {
        var el = e.target;
        if (which === "where") this.activeTab.where = el.value; else this.activeTab.orderBy = el.value;
        var m = el.value.slice(0, el.selectionStart).match(/[\w.]*$/);
        var word = m ? m[0] : "";
        if (!word) { this.sug.open = false; return; }
        var lw = word.toLowerCase();
        var items = this.filterCols().filter(function (c) {
          return c.toLowerCase().indexOf(lw) >= 0 && c.toLowerCase() !== lw;
        }).slice(0, 12);
        if (!items.length) { this.sug.open = false; return; }
        this.sug = { open: true, items: items, sel: 0, which: which, word: word };
      },
      pickSug: function (col) {
        var which = this.sug.which;
        var id = which === "where" ? "dg-where-input" : "dg-order-input";
        var el = document.getElementById(id);
        var cur = which === "where" ? this.activeTab.where : this.activeTab.orderBy;
        var pos = el ? el.selectionStart : cur.length;
        var before = cur.slice(0, pos).replace(/[\w.]*$/, col), after = cur.slice(pos);
        if (which === "where") this.activeTab.where = before + after; else this.activeTab.orderBy = before + after;
        this.sug.open = false;
        this.$nextTick(function () { if (el) { el.focus(); el.setSelectionRange(before.length, before.length); } });
      },
      // WHERE / ORDER BY 输入框的引号/括号自动闭合（对齐编辑器体验）；处理了返回 true
      acHandle: function (which, e) {
        if (e.ctrlKey || e.metaKey || e.altKey) return false;
        var PAIR = { "(": ")", "[": "]", "{": "}", "'": "'", '"': '"', "`": "`" };
        var el = e.target, val = el.value, s = el.selectionStart, ep = el.selectionEnd, k = e.key, self = this;
        var setVal = function (nv, caret) {
          if (which === "where") self.activeTab.where = nv; else self.activeTab.orderBy = nv;
          self.$nextTick(function () { el.value = nv; el.setSelectionRange(caret, caret); });
        };
        // 退格夹在空成对符号中间 → 连右符号一起删
        if (k === "Backspace" && s === ep && s > 0 && PAIR[val[s - 1]] === val[s]) {
          e.preventDefault(); setVal(val.slice(0, s - 1) + val.slice(s + 1), s - 1); return true;
        }
        if (k.length !== 1) return false;
        // 光标后正好是同一个右符号 → 只跳过、不重复插入
        if (s === ep && val[s] === k && (k === ")" || k === "]" || k === "}" || k === "'" || k === '"' || k === "`")) {
          e.preventDefault(); el.setSelectionRange(s + 1, s + 1); return true;
        }
        if (PAIR[k]) {
          e.preventDefault();
          var sel = val.slice(s, ep);   // 有选中则包裹，无选中则插入空成对符
          setVal(val.slice(0, s) + k + sel + PAIR[k] + val.slice(ep), s + 1 + sel.length);
          return true;
        }
        return false;
      },
      filterKey: function (which, e) {
        if (this.acHandle(which, e)) { this.sug.open = false; return; }
        if (this.sug.open && this.sug.which === which) {
          if (e.key === "ArrowDown") { e.preventDefault(); this.sug.sel = Math.min(this.sug.sel + 1, this.sug.items.length - 1); return; }
          if (e.key === "ArrowUp") { e.preventDefault(); this.sug.sel = Math.max(this.sug.sel - 1, 0); return; }
          if (e.key === "Tab" || e.key === "Enter") { e.preventDefault(); this.pickSug(this.sug.items[this.sug.sel]); return; }
          if (e.key === "Escape") { e.preventDefault(); this.sug.open = false; return; }
        }
        if (e.key === "Enter") this.applyWhere();
      },
      blurSug: function () { var self = this; setTimeout(function () { self.sug.open = false; }, 150); },
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
      // 按方言给标识符（表名/列名）加引号 —— 否则保留字或特殊字符的列名（如 key/order/desc）会 SQL 语法错误
      qid: function (name) {
        var m = this.connMeta, eng = m ? m.engine : "";
        if (eng === "mysql") return "`" + String(name).replace(/`/g, "``") + "`";
        return '"' + String(name).replace(/"/g, '""') + '"';   // postgres / sqlite / duckdb
      },
      qtable: function (t) {
        return t.schema ? this.qid(t.schema) + "." + this.qid(t.table) : this.qid(t.table);
      },
      startEdit: function (ri, ci) {
        var t = this.activeTab;
        if (!t || t.type !== "data" || !t.result) return;
        var k = this.mk(t.table, t.schema), self = this;
        // 元数据未就绪：先拉列类型，到了再进入编辑——否则时间列取不到类型，退化成文本框
        if (!this.tableMeta[k]) {
          this.fetchMeta(t.table, t.schema).then(function () { self.startEdit(ri, ci); });
          return;
        }
        var v = t.result.rows[ri][ci], key = ri + ":" + ci, kind = this.dtKind(ci);
        var cur = (t.edits && key in t.edits) ? t.edits[key] : (v == null ? "NULL" : this.cellText(v));
        var raw = cur === "NULL" ? "" : cur, init;
        if (kind === "datetime") init = this.toDatetimeLocal(raw);
        else if (kind === "date") init = raw.slice(0, 10);
        else if (kind === "time") init = raw.slice(0, 8);
        else init = cur;
        t.edit = { ri: ri, ci: ci, val: init, dt: !!kind, dtKind: kind };
        this.$nextTick(function () {
          var el = document.getElementById("dg-cell-input");
          if (el) { el.focus(); if (!kind) el.select(); }   // 时间选择器不 select
        });
      },

      cancelEdit: function () { if (this.activeTab) this.activeTab.edit = null; },
      commitEdit: function () {
        var t = this.activeTab;
        if (!t || !t.edit || !t.result) return;
        var ri = t.edit.ri, ci = t.edit.ci, key = ri + ":" + ci, kind = t.edit.dtKind;
        var oldV = t.result.rows[ri][ci], newRaw;
        if (kind === "datetime") newRaw = t.edit.val ? this.fromDatetimeLocal(t.edit.val) : "NULL";
        else if (kind === "date" || kind === "time") newRaw = t.edit.val || "NULL";
        else newRaw = t.edit.val;
        t.edit = null;
        // 暂存：不立即执行，等工具栏「提交」。改回原值则撤销暂存。
        if (newRaw === (oldV == null ? "NULL" : this.cellText(oldV))) delete t.edits[key];
        else t.edits[key] = newRaw;
        this.persist();
      },

      // ---------- 暂存式编辑：攒改动 → 工具栏提交 ----------
      isEditedCell: function (ri, ci) {
        var t = this.activeTab; return !!(t && t.edits && (ri + ":" + ci) in t.edits);
      },
      isDelRow: function (ri) { var t = this.activeTab; return !!(t && t.dels && t.dels[ri]); },
      cellNull: function (ri, ci) {
        var t = this.activeTab, key = ri + ":" + ci;
        if (t.edits && key in t.edits) return t.edits[key] === "NULL";
        return t.result.rows[ri][ci] === null;
      },
      cellShow: function (ri, ci) {
        var t = this.activeTab, key = ri + ":" + ci;
        var edited = t.edits && key in t.edits;
        var raw = edited ? t.edits[key] : t.result.rows[ri][ci];
        if (edited ? raw === "NULL" : raw == null) return "NULL";
        // 列显示类型：数值列按时间戳格式化展示（铁律：仅展示，底层值不变）
        var disp = this.displayTypeOf(ci);
        if (disp && disp !== "number" && this.isNumericCol(ci) && raw !== "" && isFinite(+raw)) {
          return this.formatTs(+raw, disp);
        }
        return this.cellText(raw);
      },
      // ---------- 列类型识别 + 显示类型（Change Display Type，仅展示不写库） ----------
      colType: function (ci) {
        var t = this.activeTab; if (!t || t.type !== "data" || !t.result) return "";
        var meta = this.tableMeta[this.mk(t.table, t.schema)]; if (!meta) return "";
        var col = t.result.columns[ci];
        var c = (meta.columns || []).find(function (x) { return x.name === col; });
        return c ? String(c.type || "").toLowerCase() : "";
      },
      isNumericCol: function (ci) {
        return /\b(bigint|smallint|mediumint|tinyint|int|integer|decimal|numeric|float|double|real|bit)\b/.test(this.colType(ci));
      },
      // 列类型归类：数据 tab 用表结构类型，查询 tab 从本页数据推断
      colCat: function (ci) {
        var t = this.activeTab; if (!t || !t.result) return "";
        var type = t.type === "data" ? this.colType(ci) : "";
        if (type) {
          if (/json/.test(type)) return "json";
          if (/int|decimal|numeric|float|double|real|bit|serial/.test(type)) return "number";
          if (/datetime|timestamp/.test(type)) return "datetime";
          if (type === "date") return "date";
          if (type === "time") return "time";
          if (/bool/.test(type)) return "bool";
          if (/blob|binary|bytea/.test(type)) return "binary";
          return "string";
        }
        // 查询 tab：优先用后端返回的权威列类型（大整数以字符串传输，仍标为 number）
        var bt = t.result.column_types;
        if (bt && bt[ci]) return bt[ci];
        var rows = t.result.rows;   // 后端未给类型的列（如字符串）：扫本页首个非空值推断
        for (var i = 0; i < rows.length && i < 40; i++) {
          var v = rows[i][ci]; if (v != null) return this.inferCat(v);
        }
        return "";
      },
      inferCat: function (v) {
        if (typeof v === "number") return "number";
        if (typeof v === "boolean") return "bool";
        if (typeof v === "object") return "json";
        var s = String(v).trim();
        if (/^-?\d+(\.\d+)?$/.test(s)) return "number";
        if (s[0] === "{" || s[0] === "[") return "json";
        if (/^\d{4}-\d{2}-\d{2}[ T]\d{2}:\d{2}/.test(s)) return "datetime";
        if (/^\d{4}-\d{2}-\d{2}$/.test(s)) return "date";
        return "string";
      },
      colGlyph: function (ci) { return COL_GLYPH[this.colCat(ci)] || "·"; },
      isJsonCol: function (ci) { return this.colType(ci).indexOf("json") >= 0; },
      isDatetimeCol: function (ci) {
        var t = this.colType(ci); return /datetime|timestamp/.test(t) || t === "date" || t === "time";
      },
      dtKind: function (ci) {   // 'datetime' | 'date' | 'time' | null，决定用哪种选择器
        var t = this.colType(ci);
        if (/datetime|timestamp/.test(t)) return "datetime";
        if (t === "date") return "date";
        if (t === "time") return "time";
        return null;
      },
      dtInputType: function (kind) {
        return kind === "datetime" ? "datetime-local" : kind === "date" ? "date"
             : kind === "time" ? "time" : "text";
      },
      displayTypeOf: function (ci) {
        var t = this.activeTab; if (!t || !t.result) return "number";
        return (t.colDisplay && t.colDisplay[t.result.columns[ci]]) || "number";
      },
      formatTs: function (n, disp) {
        var ms = disp === "ts_s" ? n * 1000 : disp === "ts_us" ? n / 1000 : n;
        var d = new Date(ms); if (isNaN(d.getTime())) return String(n);
        return this.fmtDateLocal(d);
      },
      fmtDateLocal: function (d) {
        function p(x) { return (x < 10 ? "0" : "") + x; }
        return d.getFullYear() + "-" + p(d.getMonth() + 1) + "-" + p(d.getDate()) + " " +
               p(d.getHours()) + ":" + p(d.getMinutes()) + ":" + p(d.getSeconds());
      },
      openColMenu: function (e, ci) {
        var t = this.activeTab; if (!t || t.type !== "data") return;
        if (!this.isNumericCol(ci)) return;   // 仅数值列可改显示类型（当前支持时间戳）
        e.preventDefault();
        this.colMenu = { show: true, x: e.clientX, y: e.clientY, ci: ci };
      },
      setDisplayType: function (disp) {
        var t = this.activeTab, ci = this.colMenu.ci;
        if (t && ci >= 0) {
          var col = t.result.columns[ci];
          if (disp === "number") delete t.colDisplay[col]; else t.colDisplay[col] = disp;
          this.persist();
        }
        this.colMenu.show = false;
      },
      closeColMenu: function () { this.colMenu.show = false; },
      // datetime 列编辑用日期选择器：SQL "YYYY-MM-DD HH:MM:SS" ↔ datetime-local "…T…"
      toDatetimeLocal: function (s) {
        if (s == null || s === "NULL" || s === "") return "";
        return String(s).replace(" ", "T").slice(0, 19);
      },
      fromDatetimeLocal: function (s) {
        if (!s) return "NULL";
        s = s.replace("T", " ");
        if (/ \d\d:\d\d$/.test(s)) s += ":00";   // 补秒
        return s;
      },
      onCellBlur: function () {
        var t = this.activeTab;
        if (t && t.edit && !t.edit.dt) this.cancelEdit();   // datetime 选择器交互会失焦，不取消
      },
      onCellChange: function () {
        var t = this.activeTab;
        if (t && t.edit && t.edit.dt) this.commitEdit();     // 选好日期即提交暂存
      },
      hasPending: function () {
        var t = this.activeTab; if (!t) return false;
        return !!((t.edits && Object.keys(t.edits).length) ||
                  (t.dels && Object.keys(t.dels).length) || (t.adds && t.adds.length));
      },
      pendingCount: function () {
        var t = this.activeTab; if (!t) return 0;
        return Object.keys(t.edits || {}).length + Object.keys(t.dels || {}).length + (t.adds || []).length;
      },
      clearPending: function (t) { t.edits = {}; t.dels = {}; t.adds = []; },
      addRow: function (cloneRi) {
        var t = this.activeTab; if (!t || t.type !== "data" || !t.result) return;
        var k = this.mk(t.table, t.schema); if (!this.tableMeta[k]) this.fetchMeta(t.table, t.schema);
        var self = this, meta = this.tableMeta[k];
        var vals = t.result.columns.map(function (c, ci) {
          if (cloneRi == null) return "";
          if (meta && (meta.primary_key || []).indexOf(c) >= 0) return "";  // 主键留自增/默认
          var v = t.result.rows[cloneRi][ci];
          return v == null ? "NULL" : self.cellText(v);
        });
        t.adds.push({ values: vals }); this.persist();
      },
      removeAdd: function (idx) {
        var t = this.activeTab; if (t && t.adds) { t.adds.splice(idx, 1); this.persist(); }
      },
      toggleDelSelected: function () {
        var t = this.activeTab; if (!t) return;
        var ris = this.selRis();
        if (!ris.length) { this.flash("先点行号选中要删除的行"); return; }
        ris.forEach(function (ri) { if (t.dels[ri]) delete t.dels[ri]; else t.dels[ri] = true; });
        this.clearRowSel(); this.persist();
      },
      pkConds: function (t, ri) {
        var k = this.mk(t.table, t.schema), meta = this.tableMeta[k];
        if (!meta) { this.fetchMeta(t.table, t.schema); return { err: "表结构加载中，请稍后再试" }; }
        var pk = meta.primary_key || [];
        if (!pk.length) return { err: "该表无主键，改动/删除无法定位行" };
        var cols = t.result.columns, row = t.result.rows[ri], self = this, miss = false;
        var conds = pk.map(function (p) {
          var idx = cols.indexOf(p); if (idx < 0) { miss = true; return null; }
          var pv = row[idx];
          return self.qid(p) + (pv == null ? " IS NULL" : " = " + self.sqlLit(self.cellText(pv), pv));
        });
        if (miss) return { err: "结果集缺少主键列，无法定位行" };
        return { conds: conds };
      },
      // 汇总所有暂存改动为 SQL 语句数组（INSERT/UPDATE/DELETE）
      buildPendingStatements: function (t) {
        var q = this.qtable(t), cols = t.result.columns, self = this;
        var stmts = [];
        (t.adds || []).forEach(function (add) {
          var c = [], v = [];
          for (var i = 0; i < cols.length; i++) {
            var raw = add.values[i]; if (raw === "" || raw == null) continue;
            c.push(self.qid(cols[i])); v.push(self.sqlLit(raw, null));
          }
          if (c.length) stmts.push("INSERT INTO " + q + " (" + c.join(", ") + ") VALUES (" + v.join(", ") + ")");
        });
        var byRow = {};
        Object.keys(t.edits || {}).forEach(function (key) {
          var p = key.split(":"); (byRow[p[0]] = byRow[p[0]] || []).push({ ci: +p[1], val: t.edits[key] });
        });
        for (var ri in byRow) {
          var pc = this.pkConds(t, ri); if (pc.err) return { err: pc.err };
          var row = t.result.rows[ri];
          var sets = byRow[ri].map(function (e) { return self.qid(cols[e.ci]) + " = " + self.sqlLit(e.val, row[e.ci]); });
          stmts.push("UPDATE " + q + " SET " + sets.join(", ") + " WHERE " + pc.conds.join(" AND "));
        }
        var delRis = Object.keys(t.dels || {});
        for (var di = 0; di < delRis.length; di++) {
          var pc2 = this.pkConds(t, delRis[di]); if (pc2.err) return { err: pc2.err };
          stmts.push("DELETE FROM " + q + " WHERE " + pc2.conds.join(" AND "));
        }
        return { statements: stmts };
      },
      submitPending: function (mode) {
        this.submitOpen = false;
        var t = this.activeTab; if (!t) return;
        if (!this.hasPending()) { this.flash("没有未提交的改动"); return; }
        var res = this.buildPendingStatements(t);
        if (res.err) { this.flash(res.err); return; }
        if (!res.statements.length) { this.flash("无有效改动（新增行至少填一列）"); return; }
        if (mode === "sql") t.submit = { statements: res.statements, sql: res.statements.join(";\n") + ";" };
        else this.doSubmit(res.statements);
      },
      cancelSubmit: function () { if (this.activeTab) this.activeTab.submit = null; },
      doSubmit: function (statements) {
        var t = this.activeTab; if (!t) return;
        var stmts = statements || (t.submit && t.submit.statements);
        if (!stmts || !stmts.length) return;
        var self = this; t.submit = null; t.submitting = true; t.err = null;
        var chain = Promise.resolve(), fails = [], okc = 0;
        stmts.forEach(function (sql) {
          chain = chain.then(function () {
            return apiPost("/admin/sql/run", { conn: t.conn, sql: sql, confirm: "1" }).then(function (d) {
              if (d && d.ok) okc++; else fails.push((d && d.error) || ("失败：" + sql));
            }).catch(function (e) { fails.push("" + e); });
          });
        });
        chain.then(function () {
          t.submitting = false; self.clearPending(t);
          self.flash(fails.length ? ("提交完成：成功 " + okc + " 条，失败 " + fails.length + " —— " + fails[0])
                                  : ("已提交 " + okc + " 条改动"));
          self.loadHistory();
          self.run(false, t.result ? t.result.page : 0);  // 重新按条件查询刷新
        });
      },
      refreshData: function () {
        var t = this.activeTab; if (!t) return;
        if (this.hasPending()) { t.refreshWarn = true; return; }
        this.run(false, t.result ? t.result.page : 0);
      },
      doRefresh: function () {
        var t = this.activeTab; if (!t) return;
        t.refreshWarn = false; this.clearPending(t);
        this.run(false, t.result ? t.result.page : 0);
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
        // 只 lint **光标所在的那条语句**（按 stmtRanges 拆分，与执行/边框同一套），
        // 避免把「无分号/换行分隔的多条」当成一条、误报到下一条上（红波浪线标错位置）。
        var info = this.stmtRangeAtCursor();
        if (!info) { clear(); return; }
        var startLine = info.start.lineNumber, endLine = info.end.lineNumber, lines = [];
        for (var ln = startLine; ln <= endLine; ln++) lines.push(model.getLineContent(ln));
        var sql = lines.join("\n");
        if (!sql.trim()) { clear(); return; }
        apiPost("/admin/sql/lint", { sql: sql, dialect: dialect }).then(function (d) {
          if (!d.ok || editor.getModel() !== model) return;
          var markers = (d.errors || []).map(function (e) {
            var line = Math.min((e.line || 1) + startLine - 1, model.getLineCount());   // 回填到原文行号
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
        if (!t || !t.result || (t.type !== "data" && t.type !== "query")) return;
        t.vsel = { ri: ri, ci: ci };
        var v = t.result.rows[ri][ci], key = ri + ":" + ci;
        var staged = t.type === "data" && t.edits && key in t.edits;   // 面板显示已暂存的改动值（若有）
        this.vpVal = staged ? (t.edits[key] === "NULL" ? "" : t.edits[key]) : (v == null ? "" : this.cellText(v));
        this.vpNull = staged ? t.edits[key] === "NULL" : v == null;
        // JSON 列默认进 Record（格式化视图），其余保留上次选择
        this.vpOpen = true;
        this.vpTab = this.colCat(ci) === "json" ? "record" : (this.vpTab || "value");
        if (t.type === "data") {   // 查询 tab 无表/主键，不预取
          var k = this.mk(t.table, t.schema);
          if (!this.tableMeta[k]) this.fetchMeta(t.table, t.schema);
        }
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
        var key = t.vsel.ri + ":" + t.vsel.ci;
        var newRaw = this.vpNull ? "NULL" : this.vpVal;
        var oldV = t.result.rows[t.vsel.ri][t.vsel.ci];
        // 暂存到 edits，等工具栏「提交」统一写库（铁律：展示态不写库）
        if (newRaw === (oldV == null ? "NULL" : this.cellText(oldV))) delete t.edits[key];
        else t.edits[key] = newRaw;
        this.persist();
        this.flash("已暂存改动，点工具栏「提交」写库");
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
          var val = row[i] == null ? null : self.cellText(row[i]);
          var pretty = val, isJson = false;
          if (val != null) {
            var tt = ("" + val).trim();
            if (tt && (tt[0] === "{" || tt[0] === "[")) {
              try { pretty = JSON.stringify(JSON.parse(tt), null, 2); isJson = true; } catch (e) { /* 非 JSON */ }
            }
          }
          return { col: c, val: val, pretty: pretty, isJson: isJson,
                   type: self.colType(i) || self.colCat(i) || "", cur: i === t.vsel.ci };
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
        var self = this, t = this.activeTab; if (!t || t.type === "ddl") return;
        var model = models.get(t.id), sel = editor && editor.getSelection();
        var hasSel = sel && !sel.isEmpty();
        var sql = hasSel ? model.getValueInRange(sel) : this.currentSql();
        if (!sql.trim()) return;
        apiPost("/admin/sql/format", { conn: t.conn, sql: sql }).then(function (d) {
          if (!d.ok || d.sql == null) { if (d.error) self.flash(d.error); return; }
          if (hasSel && editor) editor.executeEdits("dbm-fmt", [{ range: sel, text: d.sql }]);
          else if (model) model.setValue(d.sql);
        });
      },
      // EXPLAIN ANALYZE：实际执行并返回分析（MySQL 8.0.18+/PG），结果进结果表
      explainAnalyze: function () {
        var t = this.activeTab; if (!t || !t.conn) { this.flash("请先选择连接"); return; }
        var sql = t.type === "data" ? this.buildDataSql(t) : this.stmtAtCursor();
        if (!sql.trim()) { this.flash("请输入 SQL"); return; }
        this.run(false, 0, "EXPLAIN ANALYZE " + sql);
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
      // ⌘/Ctrl+S：把当前 SQL 保存到服务端片段库（服务管理的 dbm.sqlite3，不落用户磁盘）。
      // 已保存过的 tab（有 snippetId）直接原地覆盖同一条；新 tab 弹命名表单。
      saveCurrent: function () {
        var t = this.activeTab; if (!t) return;
        if (!this.currentSql().trim()) { this.flash("空 SQL，无需保存"); return; }
        if (t.snippetId) { this.saveSnippet(); return; }
        if (!this.showSnipForm) this.toggleSnipForm();
      },
      exportSqlFile: function () {
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
        // 有 snippetId（⌘S 覆盖）时用 tab 现有标题；否则走命名表单要求填标题
        var updating = !!t.snippetId;
        var title = updating ? (t.title || "").trim() : this.snipDraft.title.trim();
        if (!title) { this.flash("请填写标题"); return; }
        var note = updating ? (t.snipNote || "") : this.snipDraft.note;
        apiPost("/admin/sql/snippets/save", {
          id: t.snippetId || "", title: title, note: note,
          sql: this.currentSql(), connection: t.conn
        }).then(function (d) {
          if (!d.ok) {
            // 覆盖时目标片段已被删除 → 断开链接，改走命名表单另存为新片段
            if (updating) { t.snippetId = null; self.flash((d.error || "保存失败") + "，已断开链接，请重新保存"); self.toggleSnipForm(); }
            else self.flash(d.error);
            return;
          }
          t.snippetId = d.snippet.id; t.title = d.snippet.title; t.snipNote = d.snippet.note;
          t.savedSql = self.currentSql(); t.dirty = false;   // 保存后清除改动标记
          self.showSnipForm = false; self.flash("已保存到片段库：" + d.snippet.title);
          self.loadSnippets(); self.persist();
        });
      },
      openSnippet: function (s) {
        var conn = this.connections.some(function (c) { return c.value === s.connection; })
          ? s.connection : (this.activeTab ? this.activeTab.conn : "");
        this.newTab({ title: s.title, sql: s.sql, conn: conn, snippetId: s.id, snipNote: s.note });
      },
      _snipFileName: function (title) {
        var name = (title || "snippet").replace(/[^\w一-龥.-]+/g, "_");
        return /\.sql$/i.test(name) ? name : name + ".sql";
      },
      // 单条片段下载到本地 .sql
      downloadSnippet: function (s) {
        var head = "-- " + (s.title || "") + (s.connection ? "  [" + s.connection + "]" : "") +
                   (s.note ? "\n-- " + s.note : "") + "\n\n";
        download(new Blob([head + (s.sql || "")], { type: "application/sql" }), this._snipFileName(s.title));
      },
      // 全部片段合并导出为一个 .sql（避免多次下载弹窗）
      downloadAllSnippets: function () {
        if (!this.snippets.length) { this.flash("暂无片段可导出"); return; }
        var parts = this.snippets.map(function (s) {
          return "-- ===== " + (s.title || "") + (s.connection ? "  [" + s.connection + "]" : "") + " =====" +
                 (s.note ? "\n-- " + s.note : "") + "\n" + (s.sql || "").trim() + "\n";
        });
        download(new Blob([parts.join("\n")], { type: "application/sql" }), "snippets_" + this.snippets.length + ".sql");
        this.flash("已导出 " + this.snippets.length + " 条片段");
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
                     snippetId: t.snippetId || null, snipNote: t.snipNote || "",
                     savedSql: t.savedSql || "", dirty: !!t.dirty, bookmarks: t.bookmarks || [],
                     where: t.where || "", orderBy: t.orderBy || "",
                     lastPage: t.lastPage || 0, readSql: t.readSql, explain: t.explain,
                     jobId: t.jobId || null, jobPage: t.jobPage || 0, pendingSql: t.pendingSql,
                     wfName: t.wfName || "", wfSteps: t.wfSteps || null,
                     edits: t.edits || {}, dels: t.dels || {}, adds: t.adds || [],
                     colDisplay: t.colDisplay || {},
                     // 只持久化「当前」结果 tab 的行数据，其余只留 SQL 骨架（点击时重跑）
                     results: (t.results || []).map(function (rt, i) {
                       return { rid: rt.rid, sql: rt.sql, err: rt.err || null,
                                result: i === t.resultIdx ? rt.result : null };
                     }),
                     resultIdx: t.resultIdx || 0,
                     view: t.view || "table", chart: t.chart || null,
                     graph: t.graph || null };
          }, this);
          var activeId = this.activeId;
          var data = { v: 2, tabs: tabs, activeId: activeId, treeCache: this.treeCache,
                       leftW: this.leftW, editorH: this.editorH, dataLogH: this.dataLogH,
                       schemaShow: this.schemaShow, schemaDefault: this.schemaDefault };
          function dropResults(pred) {
            tabs.forEach(function (t) {
              if (!pred(t)) return;
              t.result = null;
              (t.results || []).forEach(function (rt) { rt.result = null; });
            });
          }
          var s = JSON.stringify(data);
          if (s.length > 3000000) {  // 逐级降级：先丢非当前 tab 的结果集（保 SQL/树），再丢全部
            dropResults(function (t) { return t.id !== activeId; });
            s = JSON.stringify(data);
            if (s.length > 3800000) { dropResults(function () { return true; }); s = JSON.stringify(data); }
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
                     snippetId: t.snippetId || null, snipNote: t.snipNote || "",
                     savedSql: t.savedSql != null ? t.savedSql : (t.sql || ""), dirty: !!t.dirty,
                     bookmarks: t.bookmarks || [],
                     where: t.where || "",
                     orderBy: t.orderBy || (t.orderCol ? t.orderCol + " " + (t.orderDir || "ASC") : ""),
                     lastPage: t.lastPage || 0, readSql: t.readSql || null, explain: t.explain || null,
                     jobId: t.jobId || null, jobPage: t.jobPage || 0,
                     pendingSql: t.pendingSql || null, edit: null, confirm: null,
                     wfName: t.wfName || "", wfSteps: t.wfSteps || null, vsel: null,
                     view: t.view || "table", chart: t.chart || null,
                     graph: t.graph || null, sel: null, nodeStatus: {},
                     rowSel: {}, lastSelRi: -1, newRow: null, resQ: null,
                     edits: t.edits || {}, dels: t.dels || {}, adds: t.adds || [],
                     submit: null, submitting: false, refreshWarn: false,
                     colDisplay: t.colDisplay || {},
                     execMarks: [], execIdx: 0, seq: null,
                     results: t.results || [], resultIdx: t.resultIdx || 0, isPaging: false,
                     running: !!t.jobId };  // 有未完成任务 → 恢复后续接轮询
          });
          this.activeId = d.activeId || this.tabs[0].id;
          this.treeCache = d.treeCache || {};
          if (d.leftW) this.leftW = d.leftW;
          if (d.editorH) this.editorH = d.editorH;
          if (d.dataLogH != null) this.dataLogH = d.dataLogH;
          this.schemaShow = d.schemaShow || {};
          this.schemaDefault = d.schemaDefault || {};
          seq = Math.max.apply(null, this.tabs.map(function (t) { return t.id; })) + 1;
        } catch (e) { /* 损坏则从空开始 */ }
      },

      // ---------- 拖动分隔条 ----------
      beginDrag: function (e, axis) {
        var self = this, start = axis === "x" ? e.clientX : e.clientY;
        var base = axis === "x" ? this.leftW : axis === "log" ? this.dataLogH : this.editorH;
        e.preventDefault();
        function move(ev) {
          var delta = (axis === "x" ? ev.clientX : ev.clientY) - start;
          if (axis === "x") self.leftW = Math.max(180, Math.min(560, base + delta));
          // log 面板在底部：手柄在其上方，往下拖变矮 → base - delta
          else if (axis === "log") self.dataLogH = Math.max(0, Math.min(window.innerHeight - 240, base - delta));
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
        // 显式给 sql 语言配上自动闭合/包裹对：不依赖懒加载的 basic-languages/sql 配置
        monaco.languages.setLanguageConfiguration("sql", {
          autoClosingPairs: [
            { open: "(", close: ")" }, { open: "[", close: "]" }, { open: "{", close: "}" },
            { open: "'", close: "'" }, { open: '"', close: '"' }, { open: "`", close: "`" },
          ],
          surroundingPairs: [
            { open: "(", close: ")" }, { open: "[", close: "]" }, { open: "{", close: "}" },
            { open: "'", close: "'" }, { open: '"', close: '"' }, { open: "`", close: "`" },
          ],
          brackets: [["(", ")"], ["[", "]"], ["{", "}"]],
        });
        this.tabs.forEach(function (t) {
          if (!models.has(t.id)) models.set(t.id, monaco.editor.createModel(t.sql || "", "sql"));
        });
        var active = models.get(this.activeId) || (this.tabs[0] && models.get(this.tabs[0].id)) || null;
        editor = monaco.editor.create(this.$refs.editorEl, {
          model: active, language: "sql",
          theme: this.theme === "light" ? "vs" : "vs-dark", automaticLayout: true,
          fontSize: this.editorFontSize, wordWrap: this.editorWordWrap ? "on" : "off",
          minimap: { enabled: this.minimapOn }, scrollBeyondLastLine: false, tabSize: 2,
          glyphMargin: true,   // 书签图标显示在行号左侧的字形边栏
          fontFamily: "'JetBrains Mono', ui-monospace, Menlo, Consolas, monospace",
          renderWhitespace: "selection",
          // 引号/括号自动闭合 + 选中后包裹（always 不依赖懒加载的 SQL 语言配置）
          autoClosingBrackets: "always", autoClosingQuotes: "always", autoSurround: "languageDefined",
          readOnly: !!this.activeTab && this.activeTab.type === "ddl",
          // hover/补全等浮层渲染到 body 层的固定容器：不再被编辑器容器裁切，
          // Monaco 会按真实可视空间决定弹在光标上方还是下方（顶部空间不够就弹下面）
          fixedOverflowWidgets: true,
        });
        // schema 浮层动态避开 minimap：让浮层右缘停在 minimap 左侧（minimap 关闭时贴右缘）
        editor.onDidLayoutChange(function () { self.syncSchemaFloat(); });
        this.syncSchemaFloat();
        // 编辑器里 ⌘/Ctrl+点击已知表名 → 打开该表 DDL（无独立 provider，直接拦截鼠标）
        editor.onMouseDown(function (e) {
          if (!e.event || !(e.event.metaKey || e.event.ctrlKey) || !e.event.leftButton) return;
          var at = self.activeTab;
          if (!at || at.type !== "query" || !e.target || !e.target.position) return;
          var model = editor.getModel(); if (!model) return;
          var w = model.getWordAtPosition(e.target.position); if (!w) return;
          var info = self.tableForWord(w.word); if (!info) return;
          // 检查前面是否有 `库.` 限定，优先用它作为 schema
          var line = model.getLineContent(e.target.position.lineNumber);
          var mm = line.slice(0, w.startColumn - 1).match(/([A-Za-z_][\w$]*)\s*\.\s*$/);
          var schema = mm ? mm[1] : (info.schema || at.schema || "");
          e.event.preventDefault(); e.event.stopPropagation();
          self.openDdlTab(info.table, schema);
        });
        // ⌘/Ctrl+hover 表名 → 加下划线提示「可点击跳 DDL」（配合上面的 ⌘/Ctrl+点击）
        var linkCol = editor.createDecorationsCollection();
        var linkAt = null;
        function clearLink() { if (linkAt) { linkCol.clear(); linkAt = null; } }
        editor.onMouseMove(function (e) {
          var at = self.activeTab;
          if (!e.event || !(e.event.metaKey || e.event.ctrlKey) || !at || at.type !== "query"
              || !e.target || !e.target.position) { clearLink(); return; }
          var model = editor.getModel(); if (!model) { clearLink(); return; }
          var w = model.getWordAtPosition(e.target.position);
          if (!w || !self.tableForWord(w.word)) { clearLink(); return; }
          var ln = e.target.position.lineNumber;
          if (linkAt && linkAt.ln === ln && linkAt.sc === w.startColumn) return;  // 同一处，免重设
          linkAt = { ln: ln, sc: w.startColumn };
          linkCol.set([{ range: new monaco.Range(ln, w.startColumn, ln, w.endColumn),
            options: { inlineClassName: "dg-table-link" } }]);
        });
        editor.onMouseLeave(function () { clearLink(); });
        editor.onKeyUp(function () { clearLink(); });   // 松开 ⌘/Ctrl → 去下划线
        // 执行：右键菜单置顶 + 快捷键显示在右侧（keybindings 让 Monaco 自动渲染 ⌘↵）
        editor.addAction({ id: "dbm-run", label: "执行（选中则跑选中，否则光标处语句）",
          keybindings: [monaco.KeyMod.CtrlCmd | monaco.KeyCode.Enter],
          contextMenuGroupId: "0_run", contextMenuOrder: 0, run: function () { self.run(false); } });
        // ⌘/Ctrl+S：保存当前 SQL 到服务端片段库（Monaco 拦截，阻止浏览器「保存网页」）
        editor.addCommand(monaco.KeyMod.CtrlCmd | monaco.KeyCode.KeyS, function () { self.saveCurrent(); });
        // 书签：⌘/Ctrl+B 切换、F2/⇧F2 跳下一个/上一个、点字形边栏切换
        bmCollection = editor.createDecorationsCollection();
        execCollection = editor.createDecorationsCollection();  // 执行状态字形（语句行左侧 ⟳/✓/✗）
        stmtBoxCol = editor.createDecorationsCollection();       // 缺分号波浪线
        // 光标移动/选区变化/滚动 → 更新当前语句边框（框住 ⌘↵ 将执行的那条 SQL）+ 重 lint
        editor.onDidChangeCursorPosition(function () { self.applyStmtBox(); self.scheduleLint(); });
        editor.onDidChangeCursorSelection(function () { self.applyStmtBox(); });
        editor.onDidScrollChange(function () { self.applyStmtBox(); });
        editor.addCommand(monaco.KeyMod.CtrlCmd | monaco.KeyCode.KeyB, function () { self.toggleBookmark(); });
        editor.addCommand(monaco.KeyCode.F2, function () { self.gotoBookmark(1); });
        editor.addCommand(monaco.KeyMod.Shift | monaco.KeyCode.F2, function () { self.gotoBookmark(-1); });
        editor.onMouseDown(function (e) {
          if (e.target && e.target.type === monaco.editor.MouseTargetType.GUTTER_GLYPH_MARGIN) {
            var ln = e.target.position && e.target.position.lineNumber; if (ln) self.toggleBookmark(ln);
          }
        });
        this.applyBookmarks();
        editor.onDidBlurEditorText(function () { self.persist(); });
        editor.onDidChangeModelContent(function () { self.scheduleLint(); self.markDirty(); if (self.bmOpen || self.acc.bm) self.bmTick++;
          // 编辑内容后清掉旧的执行状态图标（行号已变，图标会错位/误导）
          var at = self.activeTab; if (at && !at.running && at.execMarks && at.execMarks.length) { at.execMarks = []; if (execCollection) execCollection.clear(); }
          self.applyStmtBox();   // 内容变化 → 重算当前语句边框
        });  // 实时语法检查 + 改动标记 + 书签预览刷新
        this.scheduleLint();
        this.applyStmtBox();     // 初始化时先画一次
        // 右键菜单：格式化（选中/全部）/ EXPLAIN / EXPLAIN ANALYZE
        editor.addAction({ id: "dbm-format", label: "格式化 SQL（有选中则格式化选中，否则全部）",
          contextMenuGroupId: "dbm", contextMenuOrder: 1, run: function () { self.formatSql(); } });
        editor.addAction({ id: "dbm-explain", label: "EXPLAIN（执行计划）",
          contextMenuGroupId: "dbm", contextMenuOrder: 2, run: function () { self.explainStmt(); } });
        editor.addAction({ id: "dbm-explain-analyze", label: "EXPLAIN ANALYZE（实际执行并分析）",
          contextMenuGroupId: "dbm", contextMenuOrder: 3, run: function () { self.explainAnalyze(); } });
        editor.addAction({ id: "dbm-export-sql", label: "导出为 .sql 文件（下载到本地）",
          contextMenuGroupId: "dbm", contextMenuOrder: 4, run: function () { self.exportSqlFile(); } });
        editor.addAction({ id: "dbm-bm-toggle", label: "给当前行加/去书签（⌘/Ctrl+B）",
          contextMenuGroupId: "dbmbm", contextMenuOrder: 1, run: function () { self.toggleBookmark(); } });
        editor.addAction({ id: "dbm-bm-next", label: "跳到下一个书签（F2）",
          contextMenuGroupId: "dbmbm", contextMenuOrder: 2, run: function () { self.gotoBookmark(1); } });
        editor.addAction({ id: "dbm-bm-prev", label: "跳到上一个书签（⇧F2）",
          contextMenuGroupId: "dbmbm", contextMenuOrder: 3, run: function () { self.gotoBookmark(-1); } });
        editor.addAction({ id: "dbm-bm-clear", label: "清除本页所有书签",
          contextMenuGroupId: "dbmbm", contextMenuOrder: 4, run: function () { self.clearBookmarks(); } });
        // 函数文档 hover：光标悬停在 MySQL 内置函数上 → 中文说明 + 用法 + 官方文档链接
        monaco.languages.registerHoverProvider("sql", {
          provideHover: function (model, position) {
            var w = model.getWordAtPosition(position); if (!w) return null;
            var doc = window.SQL_FUNCS_DOC && window.SQL_FUNCS_DOC[w.word.toUpperCase()];
            if (!doc) return null;
            // 单个 markdown 块（多块在编辑器顶部易被裁切，只剩最后一行）
            var md = "**" + w.word.toUpperCase() + "()**" + (doc.group ? "　·　" + doc.group : "") + "\n\n" +
                     (doc.summary || "") + "\n\n`" + (doc.syntax || "") + "`" +
                     (doc.url ? "\n\n[📖 MySQL 官方文档](" + doc.url + ")" : "");
            return { range: new monaco.Range(position.lineNumber, w.startColumn, position.lineNumber, w.endColumn),
              contents: [{ value: md }] };
          }
        });
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
    watch: {
      theme: function (v) {
        if (window.monaco) window.monaco.editor.setTheme(v === "light" ? "vs" : "vs-dark");
      }
    },
    mounted: function () {
      var self = this;
      this.restore();
      this.normalizeTabOrder();   // 保活恢复后按组连续排列，使拖动/分组顺序可预期
      // 执行计时秒表：任一 tab 在跑时每 200ms 触发一次重算（clockTick 驱动 runElapsed）
      setInterval(function () {
        if (self.tabs.some(function (t) { return t.running; })) self.clockTick++;
      }, 200);
      try { var a = JSON.parse(localStorage.getItem("dbm-console-acc") || "null"); if (a) Object.assign(this.acc, a); } catch (e) {}
      // 切页/刷新前发起的查询在服务端继续跑：凭持久化的 job_id 续接轮询
      this.tabs.forEach(function (t) {
        if (t.jobId) self.pollJob(t.id, t.jobId, t.jobPage || 0);
      });
      apiGet("/admin/settings/get").then(function (d) {
        if (d && d.ok && d.settings) {
          self.theme = d.settings.theme || "dark";
          self.minimapOn = d.settings.sql_minimap !== false;  // 缺省视为开启
          if (d.settings.sql_font_size) self.editorFontSize = +d.settings.sql_font_size;
          self.editorWordWrap = !!d.settings.sql_word_wrap;
          if (editor) {
            editor.updateOptions({ minimap: { enabled: self.minimapOn }, fontSize: self.editorFontSize,
              wordWrap: self.editorWordWrap ? "on" : "off" });
            self.syncSchemaFloat();
          }
        }
      }).catch(function () {});
      this.loadConnections().then(function () { self.loadSnippets(); });
      loadMonaco(function () { self.initEditor(); });
      window.addEventListener("beforeunload", function () { self.persist(); });
      document.addEventListener("click", function () { self.closeCtx(); self.closeTabCtx(); self.exportOpen = false; self.copyOpen = false; self.schemaPickOpen = false; });
      // ⌘/Ctrl+F：焦点不在 Monaco 时打开网格内搜索（Monaco 自己的查找不受影响）
      // ⌘/Ctrl+P：全局表名搜索（跨库，回车直达表数据）
      document.addEventListener("keydown", function (e) {
        if ((e.metaKey || e.ctrlKey) && e.key === "f" && editor && !editor.hasTextFocus()) {
          var t = self.activeTab;
          if (t && t.result && t.result.columns.length) { e.preventDefault(); self.openResQ(); }
        }
        if ((e.metaKey || e.ctrlKey) && e.key === "p") { e.preventDefault(); self.openTblSearch(); }
        // ⌘/Ctrl+S：焦点不在 Monaco 时也保存到服务端（Monaco 内由 editor 命令处理）
        if ((e.metaKey || e.ctrlKey) && e.key === "s" && (!editor || !editor.hasTextFocus())) {
          e.preventDefault(); self.saveCurrent();
        }
      });
      window.addEventListener("resize", function () { if (chartInst) chartInst.resize(); });
      // 恢复的活动 tab 若在图表视图，重画
      var at = this.activeTab;
      if (at && at.view === "chart" && at.result) this.renderChart();
    },
    template: `
<div class="dg-root" :class="{'env-prod': isProd, 'env-staging': isStaging, 'theme-light': theme==='light'}">
  <aside class="dg-left" :style="{width: leftW + 'px'}">
    <div class="dg-conn" :class="{'env-prod': isProd, 'env-staging': isStaging}">
      <dg-select :model-value="activeTab ? activeTab.conn : ''" :options="connOptions"
                 placeholder="选择连接…" @update:model-value="setConn"/>
    </div>
    <div class="dg-acc">
      <div class="dg-sec-hd acc-hd" @click="toggleAcc('tree')"><span class="caret">{{ acc.tree ? '▾' : '▸' }}</span><span>{{ needsDb ? "库 / 表" : "表" }}</span>
        <span class="acc-acts" @click.stop>
        <span v-if="selCount" class="selinfo">已选 {{ selCount }} <a @click="clearSel">清除</a></span>
        <span class="act" @click="openTblSearch" title="全局搜表跳转（⌘/Ctrl+P）">⌕</span>
        <span class="act" @click="refreshTree" title="刷新（重新拉取）">↻</span></span></div>
      <div v-show="acc.tree" class="acc-body grow">
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
      </div>
      <div class="dg-sec-hd acc-hd" @click="toggleAcc('bm')"><span class="caret">{{ acc.bm ? '▾' : '▸' }}</span><span>书签{{ bmList.length ? "（"+bmList.length+"）" : "" }}</span>
        <span class="acc-acts" @click.stop><span v-if="bmList.length" class="act" @click="clearBookmarks" title="清除本页所有书签">清空</span></span></div>
      <div v-show="acc.bm" class="acc-body cap">
        <div v-if="!bmList.length" class="dg-empty" style="line-height:1.5">给某句 SQL 加书签：把光标放到那行，按 <b>⌘/Ctrl+B</b> 或点行号左侧空白。之后在这里点它直接跳转/执行。</div>
        <div v-for="b in bmList" :key="b.line" class="dg-bm-row" @click="jumpBookmark(b.line)" :title="b.text">
          <span class="ln">{{ b.line }}</span>
          <span class="tx">{{ b.text }}</span>
          <span class="run" @click.stop="runBookmark(b.line)" title="跳转并执行这句">▶</span>
        </div>
      </div>
      <div class="dg-sec-hd acc-hd" @click="toggleAcc('wf')"><span class="caret">{{ acc.wf ? '▾' : '▸' }}</span><span>工作流</span>
        <span class="acc-acts" @click.stop>
        <span class="act" @click="newFlowTab()" title="新建可视化流程（DAG 画布）">＋流程</span>
        <span class="act" @click="loadWorkflows" title="刷新">↻</span></span></div>
      <div v-show="acc.wf" class="acc-body cap">
      <div v-if="!wfs.length" class="dg-empty">（暂无：点「＋流程」画一个，或在工作区写脚本点「存工作流」）</div>
      <div v-for="w in wfs" :key="w.name" class="dg-snip" @click="loadWorkflow(w)" :title="'工作区 '+w.workspace+(w.graph?' · 点击打开画布':' · 点击载入脚本')">
        <div class="t"><span>{{ w.graph ? "⧉" : "⚙" }} {{ w.name }}</span>
          <span class="x" style="opacity:1;color:var(--dg-green)" @click.stop="runWorkflow(w)" title="重跑（重拉数据+执行脚本）">▶</span>
          <span class="x" :class="{arm: delWf===w.name}" @click.stop="askDeleteWf(w.name)">{{ delWf===w.name ? "确认?" : "✕" }}</span></div>
        <div class="c">⚗ {{ w.workspace }} · {{ w.sources.length }} 源 · {{ fmtTs(w.updated_at) }}</div>
      </div>
      </div>
      <div class="dg-sec-hd acc-hd" @click="toggleAcc('hist')"><span class="caret">{{ acc.hist ? '▾' : '▸' }}</span><span>历史</span>
        <span class="acc-acts" @click.stop><span class="act" @click="loadHistory" title="刷新">↻</span></span></div>
      <div v-show="acc.hist" class="acc-body cap">
        <div v-if="!history.length" class="dg-empty">（暂无历史）</div>
        <div v-for="(h,hi) in history" :key="hi" class="dg-hist" @click="openHistory(h)" :title="h.sql">
          <span class="st" :class="h.status==='ok'?'ok':'bad'">●</span>
          <span class="sq">{{ h.sql }}</span>
          <span class="tm">{{ fmtTs(h.ts).slice(5) }}</span>
        </div>
      </div>
      <div class="dg-sec-hd acc-hd" @click="toggleAcc('snip')"><span class="caret">{{ acc.snip ? '▾' : '▸' }}</span><span>片段{{ visibleSnippets.length ? "（"+visibleSnippets.length+"）" : "" }}</span><span class="acc-acts" @click.stop><span v-if="snippets.length" class="act" @click="downloadAllSnippets" title="全部导出为一个 .sql 文件">⬇</span><span class="act" @click="toggleSnipForm" title="保存当前 SQL 为片段">＋</span></span></div>
      <div v-show="acc.snip" class="acc-body cap">
      <div v-if="showSnipForm" class="dg-snipform">
        <input v-model="snipDraft.title" placeholder="标题">
        <textarea v-model="snipDraft.note" rows="2" placeholder="备注（可选）"></textarea>
        <div style="display:flex;gap:6px"><button class="dg-btn run" style="flex:1" @click="saveSnippet">保存</button><button class="dg-btn" @click="showSnipForm=false">取消</button></div>
      </div>
      <div v-if="activeTab && activeTab.conn" class="dg-snip-scope"><label><input type="checkbox" v-model="snipAllConns"> 显示全部连接的片段</label></div>
      <div v-if="!visibleSnippets.length" class="dg-empty">{{ snippets.length ? "（本连接暂无片段，可勾选「全部」查看）" : "（暂无片段）" }}</div>
      <div v-for="s in visibleSnippets" :key="s.id" class="dg-snip" @click="openSnippet(s)">
        <div class="t"><span>{{ s.title }}</span><span class="snip-acts"><span class="dl" @click.stop="downloadSnippet(s)" title="下载为 .sql">⬇</span><span class="x" :class="{arm: delSnip===s.id}" @click.stop="askDeleteSnippet(s.id)">{{ delSnip===s.id ? "确认?" : "✕" }}</span></span></div>
        <div v-if="s.note" class="n">{{ s.note }}</div>
        <div class="c">{{ s.connection || "—" }} · {{ fmtTs(s.updated_at) }}</div>
      </div>
      </div>
    </div>
  </aside>
  <div class="dg-vsplit" @mousedown="beginDrag($event, 'x')"></div>
  <section class="dg-main">
    <div v-if="isProd" class="dg-prod-ribbon" title="生产环境 · PROD · 写操作将影响线上数据，请谨慎"></div>
    <div v-else-if="isStaging" class="dg-prod-ribbon staging" title="预发布 · STAGING 环境"></div>
    <!-- 顶部栏只在「流程」或「分析工作区脚本」时显示（承载 运行流程/存工作流）；
         普通 SQL 编辑器整条去掉：执行走 ⌘Enter/右键，schema 选择器浮在编辑器内 -->
    <div class="dg-top" v-if="activeTab && (activeTab.type==='flow' || (isAnalysis && activeTab.type==='query'))">
      <button v-if="activeTab.type==='flow'" class="dg-btn run" :disabled="activeTab.running" @click="run(false)">▶ {{ activeTab.running ? "执行中…" : "运行流程" }}</button>
      <button class="dg-btn" @click="saveWorkflow" title="把当前脚本/流程图存为可重跑的 workflow">存工作流</button>
    </div>
    <div class="dg-tabs">
      <template v-for="g in tabGroups" :key="g.conn">
        <div class="dg-tabgrp-hd" :class="['env-'+g.meta.env, {collapsed: tabGroupCollapsed[g.conn], drag: g.conn===dragGroup}]"
             @click="toggleTabGroup(g.conn)" :title="'连接：'+(g.conn||'无')+' · 点击折叠/展开 · 拖动可调整组顺序'"
             draggable="true" @dragstart="onGroupDragStart(g.conn, $event)" @dragover.prevent @drop="onGroupDrop(g.conn)" @dragend="dragGroup=null">
          <span class="car">{{ tabGroupCollapsed[g.conn] ? '▸' : '▾' }}</span>
          <span class="gnm">{{ g.meta.name }}</span>
          <span class="gcount">{{ g.tabs.length }}</span>
        </div>
        <template v-if="!tabGroupCollapsed[g.conn]">
          <div v-for="t in g.tabs" :key="t.id" class="dg-tab" :class="{active: t.id===activeId, drag: t.id===dragId}" @click="switchTab(t.id)"
               @contextmenu="openTabCtx($event, t.id)"
               draggable="true" @dragstart="onTabDragStart(t.id, $event)" @dragover.prevent @drop="onTabDrop(t.id)" @dragend="dragId=null">
            <span class="ticon" v-if="t.type==='data'">▦</span><span class="ticon" v-else-if="t.type==='ddl'">≔</span>
            <input v-if="renamingId===t.id" class="rename-in" v-model="renameVal"
                   @click.stop @keydown.enter="commitRename" @keydown.esc="cancelRename" @blur="commitRename">
            <span v-else class="nm" @dblclick.stop="beginRename(t.id)" title="双击改名 · 右键更多">{{ t.title }}<span v-if="t.dirty && t.type==='query'" class="dirty" title="有未保存改动">*</span></span>
            <span class="pin" :class="{on: t.pinned}" @click.stop="togglePin(t.id)"
                  :title="t.pinned ? '取消固定' : '固定（防误关）'">⚲</span>
            <span v-if="!t.pinned" class="x" @click.stop="closeTab(t.id)">✕</span>
          </div>
        </template>
      </template>
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
      <div class="dg-editor"><div ref="editorEl" style="position:absolute;inset:0"></div>
        <div v-if="!editorReady" class="dg-editor-loading">编辑器加载中…</div>
        <button v-if="aiEnabled && !aiPanel && activeTab && activeTab.type==='query'"
                class="dg-ai-fab" :style="aiFabStyle" @click="openAiPanel"
                title="用 AI 按表结构生成 SQL（插入光标处，不执行）">✨ AI</button>
        <!-- AI 生成面板：可拖动的浮层小卡片，浮在编辑器上（position:absolute，不挤占编辑区） -->
        <div v-if="aiPanel" class="dg-ai-pop" :style="{left: aiPanel.pos.left + 'px', top: aiPanel.pos.top + 'px'}">
          <div class="dg-ai-head" @mousedown="aiDragStart"><span class="t">✨ AI 生成 SQL</span>
            <span class="x" @mousedown.stop @click="closeAiPanel" :title="aiPanel.turns.length ? '完成' : '关闭'">✕</span></div>
          <div v-if="aiPanel.turns.length" class="dg-ai-turns">
            <div v-for="(qt,qi) in aiPanel.turns" :key="qi" class="row">{{ qi+1 }}. {{ qt }} ✓</div>
          </div>
          <textarea class="dg-ai-q" v-model="aiPanel.question" rows="2" spellcheck="false"
                    :placeholder="aiPanel.sessionId ? '继续追问，例如：改成按周分组' : '描述你想查什么，例如：每天新增订单数'"
                    @keydown.meta.enter.stop="aiGenerate" @keydown.ctrl.enter.stop="aiGenerate"></textarea>
          <div class="dg-ai-scope" v-if="!aiPanel.sessionId">
            <div class="dg-ai-scope-hd">
              <span>表 · 已选 {{ aiPickCount() }}（不选=整库）</span>
              <input v-model="aiPanel.filter" placeholder="筛选…" class="dg-ai-filter">
            </div>
            <div v-if="databases.length" class="dg-ai-schema">
              <span>库</span>
              <select :value="aiPanel.schema" @change="aiSetSchema($event.target.value)">
                <option v-for="db in databases" :key="db" :value="db">{{ db }}</option>
              </select>
              <span class="hint">可切库跨 schema 勾选</span>
            </div>
            <div v-if="aiPickCount()" class="dg-ai-picked">
              <span v-for="q in aiPickedList()" :key="q" class="chip" @click="aiTogglePickQual(q)" title="点击移除">{{ q }} ✕</span>
            </div>
            <div v-if="aiPanel.loading" class="dg-ai-empty">加载表…</div>
            <div v-else-if="aiPanel.tables.length" class="dg-ai-tables">
              <label v-for="tb in aiVisibleTables()" :key="tb" class="dg-ai-tbl">
                <input type="checkbox" :checked="!!aiPanel.picked[aiQual(tb)]" @change="aiTogglePick(tb)"> {{ tb }}
              </label>
            </div>
            <div v-else class="dg-ai-empty">（无法列表，可整库生成或切换上面的库）</div>
          </div>
          <div v-if="aiPanel.sessionId" class="dg-ai-mode">
            <span>插入：</span>
            <label><input type="radio" value="replace" v-model="aiPanel.mode"> 替换上一条</label>
            <label><input type="radio" value="append" v-model="aiPanel.mode"> 追加在后面</label>
          </div>
          <div class="dg-ai-foot">
            <label class="dg-ai-opt"><input type="checkbox" v-model="aiPanel.explain"> 解释思路</label>
            <label class="dg-ai-opt" v-if="!aiPanel.sessionId"><input type="checkbox" v-model="aiPanel.samples"> 样本行</label>
            <span class="sp"></span>
            <button class="dg-btn run" :disabled="aiPanel.running" @click="aiGenerate">{{ aiPanel.running ? "生成中…" : (aiPanel.sessionId ? "追问 ⌘↵" : "生成 ⌘↵") }}</button>
          </div>
          <div v-if="aiPanel.error" class="dg-ai-err">{{ aiPanel.error }}</div>
        </div>
        <!-- 执行中浮条：计时 + 取消（取消运行中的查询会向 DB 发 KILL）。执行状态图标在语句行左侧字形边栏 -->
        <div v-if="activeTab && activeTab.running" class="dg-run-pill">
          <span class="dg-run-ico">⟳</span>
          <span class="dg-run-txt">执行中 {{ runElapsed }}</span>
          <button class="dg-run-cancel" @click="cancelJob"
                  title="取消执行：向数据库发 KILL QUERY 中断正在跑的查询">取消</button>
        </div>
        <!-- 执行 schema 选择器浮在编辑器右上角（原顶部栏整条已去掉） -->
        <label v-if="connMeta && (connMeta.engine==='mysql'||connMeta.engine==='postgres') && activeTab && activeTab.type==='query'"
               class="dg-schema-float" :style="{right: schemaFloatRight + 'px'}" title="选择语句执行所在的库 / schema">
          <span class="lb">schema</span>
          <dg-select :model-value="activeTab?activeTab.schema:''" :options="schemaOptions"
                     placeholder="未指定" @update:model-value="setSchema"/>
        </label>
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
        <button v-if="aiEnabled" class="dg-btn" style="border-color:var(--dg-accent);color:var(--dg-text)"
                @click="openWfAi" title="用 AI 按需求生成整张流程图（载到画布，不执行）">✨ AI 生成流程</button>
        <span class="hint" style="margin-left:auto">拖节点右缘圆点 → 下一节点左缘连线 · 点 ✕ 删连线 · 工作区 {{ (activeTab.conn||'').split('/')[1] }}</span>
      </div>
      <div v-if="wfAi" class="dg-ai-pop" :style="{left: wfAi.pos.left + 'px', top: wfAi.pos.top + 'px'}">
        <div class="dg-ai-head" @mousedown="wfAiDragStart"><span class="t">✨ AI 生成流程</span>
          <span class="x" @mousedown.stop @click="wfAi=null" title="关闭">✕</span></div>
        <div class="dg-ai-empty" style="margin-bottom:4px">描述你要做的分析，AI 生成整张 DAG（校验通过后载到画布，不执行）。会覆盖当前画布。</div>
        <textarea class="dg-ai-q" v-model="wfAi.question" rows="2" spellcheck="false"
                  placeholder="例如：从订单表按用户聚合总消费额，关联用户表取城市，输出消费前 20 名"
                  @keydown.meta.enter.stop="wfAiGenerate" @keydown.ctrl.enter.stop="wfAiGenerate"></textarea>
        <div class="dg-ai-scope">
          <div class="dg-ai-scope-hd"><span>取数连接</span></div>
          <dg-select :model-value="wfAi.conn" :options="realConnOptions" placeholder="选择连接…"
                     @update:model-value="wfAiSetConn"/>
        </div>
        <div class="dg-ai-scope">
          <div class="dg-ai-scope-hd"><span>表 · 已选 {{ wfAi.picked ? Object.keys(wfAi.picked).length : 0 }}（不选=整库）</span>
            <input v-model="wfAi.filter" placeholder="筛选…" class="dg-ai-filter"></div>
          <div v-if="wfAi.loading" class="dg-ai-empty">加载表…</div>
          <div v-else-if="wfAi.tables.length" class="dg-ai-tables">
            <label v-for="tb in wfAiVisibleTables()" :key="tb" class="dg-ai-tbl">
              <input type="checkbox" :checked="!!wfAi.picked[tb]" @change="wfAiTogglePick(tb)"> {{ tb }}
            </label>
          </div>
          <div v-else class="dg-ai-empty">（选个连接以列表；也可不选表按整库生成）</div>
        </div>
        <div v-if="wfAi.error" class="dg-ai-err">{{ wfAi.error }}</div>
        <div class="dg-ai-foot"><span class="sp"></span>
          <button class="dg-btn run" :disabled="wfAi.running" @click="wfAiGenerate">{{ wfAi.running ? "生成中…（约 10–60s）" : "生成 ⌘↵" }}</button>
          <button class="dg-btn" :disabled="wfAi.running" @click="wfAi=null">取消</button>
        </div>
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
        <div v-if="activeTab.type==='data'" class="dg-toolbar">
          <button class="dg-btn ic" @click="refreshData" title="刷新：按当前条件重新查询（有未提交改动会提醒）">↻</button>
          <span class="tb-sep"></span>
          <button class="dg-btn ic" @click="addRow(null)" title="加行：新增一行，提交时生成 INSERT">＋</button>
          <button class="dg-btn ic" :disabled="!selRowCount()" @click="toggleDelSelected"
                  title="减行：标记/取消标记选中行删除，提交时生成 DELETE">－</button>
          <span class="tb-sep"></span>
          <span class="dg-menu">
            <button class="dg-btn run" :disabled="!hasPending()" @click.stop="submitOpen=!submitOpen">
              提交<template v-if="pendingCount()">（{{ pendingCount() }}）</template> ▾</button>
            <div v-if="submitOpen" class="dg-menu-pop">
              <button @click="submitPending('sql')">SQL 确认后提交</button>
              <button @click="submitPending('direct')">直接提交</button>
            </div>
          </span>
          <button class="dg-btn" @click="openDdlTab(activeTab.table, activeTab.schema)"
                  title="在新 tab 查看格式化的建表语句">DDL</button>
          <span v-if="hasPending()" class="tb-pending">● {{ pendingCount() }} 处未提交</span>
        </div>
        <div v-if="activeTab.type==='data'" class="dg-where">
          <span class="k">WHERE</span>
          <span class="dg-sug-wrap">
            <input id="dg-where-input" :value="activeTab.where" placeholder="status = 'paid' AND amount > 100"
                   @input="onFilterInput('where',$event)" @keydown="filterKey('where',$event)" @blur="blurSug">
            <div v-if="sug.open && sug.which==='where'" class="dg-sug">
              <div v-for="(c,i) in sug.items" :key="c" class="item" :class="{sel:i===sug.sel}"
                   @mousedown.prevent="pickSug(c)">{{ c }}</div>
            </div>
          </span>
          <span class="k">ORDER BY</span>
          <span class="dg-sug-wrap obwrap">
            <input id="dg-order-input" class="obin" :value="activeTab.orderBy" placeholder="amount DESC, id"
                   @input="onFilterInput('orderBy',$event)" @keydown="filterKey('orderBy',$event)" @blur="blurSug"
                   title="任意排序表达式，回车应用；点列头快捷设置">
            <div v-if="sug.open && sug.which==='orderBy'" class="dg-sug">
              <div v-for="(c,i) in sug.items" :key="c" class="item" :class="{sel:i===sug.sel}"
                   @mousedown.prevent="pickSug(c)">{{ c }}</div>
            </div>
          </span>
          <button class="dg-btn" @click="applyWhere">应用</button>
          <button v-if="activeTab.where || activeTab.orderBy" class="dg-btn"
                  @click="activeTab.where='';activeTab.orderBy='';applyWhere()">清除</button>
        </div>
        <div v-if="activeTab.explain" class="dg-explain">
          <div class="hd"><b>执行计划</b><span class="act" @click="closeExplain">✕ 关闭</span></div>
          <div v-if="activeTab.explain.loading" class="dg-empty">获取中…</div>
          <template v-else-if="activeTab.explain.tree">
            <!-- 直观概览：全表扫描告警 + 总成本 + 逐表访问方式/预估行/索引 -->
            <div class="ex-sum">
              <span v-if="explainInfo.warnCount" class="ex-warn">⚠ {{ explainInfo.warnCount }} 处全表扫描</span>
              <span v-else class="ex-ok">✓ 无全表扫描</span>
              <span v-if="explainInfo.cost!=null" class="ex-cost">总成本 {{ explainInfo.cost }}</span>
            </div>
            <table v-if="explainInfo.scans.length" class="ex-tbl">
              <thead><tr><th>访问方式</th><th>表 / 操作</th><th>预估行</th><th>索引</th><th>filtered</th><th>成本</th></tr></thead>
              <tbody>
                <tr v-for="(s,i) in explainInfo.scans" :key="i" :class="{warn:s.warn}">
                  <td><span class="ex-acc" :class="accessClass(s.access)">{{ accessLabel(s.access) }}</span></td>
                  <td class="ex-tb">{{ s.table }}<span v-if="s.operation" class="ex-op">{{ s.operation }}</span></td>
                  <td>{{ s.rows!=null ? fmtN(s.rows) : '—' }}</td>
                  <td>{{ s.key || '（无索引）' }}</td>
                  <td>{{ s.filtered!=null ? s.filtered+'%' : '—' }}</td>
                  <td>{{ s.cost!=null ? s.cost : '—' }}</td>
                </tr>
              </tbody>
            </table>
            <details class="ex-raw"><summary>完整计划树</summary>
              <plan-node :label="'plan'" :node="activeTab.explain.tree" :depth="0"/></details>
          </template>
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
          <div v-if="activeTab.confirm.prod" class="dg-prod-warn">⚠ 生产环境写操作 —— 将直接影响线上数据，请确认无误。</div>
          <div class="acts"><button class="dg-btn ok"
            @click="confirmRun">确认执行</button><button class="dg-btn" @click="cancelConfirm">取消</button></div>
        </div>
        <div v-if="activeTab.wfSteps && activeTab.wfSteps.length" class="dg-wfsteps">
          <div v-for="(st,si) in activeTab.wfSteps" :key="si" :class="st.ok?'okline':'errline'">
            {{ st.ok ? "✓" : "✗" }} {{ st.step }}<template v-if="st.rows!=null">（{{ st.rows }} 行）</template>
            <template v-if="st.error">— {{ st.error }}</template></div>
        </div>
        <div v-if="activeTab.refreshWarn" class="dg-confirm">
          <h4>有未提交的改动</h4>
          <div style="font-size:13px;color:var(--dg-muted)">刷新会丢弃当前所有未提交改动（{{ pendingCount() }} 处），确定继续？</div>
          <div class="acts"><button class="dg-btn danger" @click="doRefresh">继续刷新（丢弃改动）</button>
            <button class="dg-btn" @click="activeTab.refreshWarn=false">取消</button></div>
        </div>
        <div v-if="activeTab.submit" class="dg-confirm">
          <h4>确认提交 · {{ activeTab.submit.statements.length }} 条语句</h4>
          <div style="font-size:12px;color:var(--dg-muted)">将由 writer 账号依次执行并记入审计（后台旁路，不进审批单）。</div>
          <pre class="dg-submit-sql">{{ activeTab.submit.sql }}</pre>
          <div v-if="isProd" class="dg-prod-warn">⚠ 生产环境 —— 将直接影响线上数据，请确认无误。</div>
          <div class="acts"><button class="dg-btn ok"
            @click="doSubmit()">确认提交（writer 执行）</button>
            <button class="dg-btn" @click="cancelSubmit">取消</button></div>
        </div>
        <!-- 结果 tab 条：查询 tab 有结果（含出错/取消的结果页）就展示；点结果页只切换展示，关闭不触发重查 -->
        <div v-if="activeTab.type==='query' && activeTab.results && activeTab.results.length" class="dg-restabs">
          <div v-for="(rt,i) in activeTab.results" :key="rt.rid" class="rtab" :class="{on: i===activeTab.resultIdx, err: !!rt.err}"
               :title="rt.sql" @click="selectResult(i)">
            <span class="rl"><template v-if="rt.err">✗ </template>结果 {{ i+1 }}</span>
            <span class="rx" @click.stop="closeResult(i)">✕</span>
          </div>
        </div>
        <div v-if="activeTab.err" class="dg-res-err">⚠ {{ activeTab.err }}</div>
        <div v-else-if="activeTab.ok" class="dg-res-ok">✓ 执行成功，影响 {{ activeTab.ok.affected_rows }} 行 · {{ activeTab.ok.duration_ms }} ms</div>
        <template v-else-if="activeTab.result">
          <div class="dg-res-meta">
            <span>{{ activeTab.result.paginated ? "本页 " : "" }}{{ activeTab.result.rows.length }} 行</span>
            <span v-if="activeTab.result.duration_ms!=null">{{ activeTab.result.duration_ms }} ms</span>
            <span v-if="activeTab.result.paginated && activeTab.result.ordered===false" style="color:var(--dg-amber)" title="LIMIT/OFFSET 翻页在无 ORDER BY 时顺序不保证稳定">⚠ 无 ORDER BY</span>
            <span v-if="activeTab.result.paginated" class="pager">
              <button class="pg ic" :disabled="activeTab.result.page<=0" @click="goPage(activeTab.result.page-1)" title="上一页">‹</button>
              <span class="pn">第 {{ activeTab.result.page+1 }} 页</span>
              <button class="pg ic" :disabled="!activeTab.result.has_next" @click="goPage(activeTab.result.page+1)" title="下一页">›</button>
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
                      @click="addRow(selRis()[0])" title="以选中行为模板暂存新增（主键留空）">克隆</button>
              <button v-if="activeTab.type==='data'" class="dg-btn danger"
                      @click="toggleDelSelected" title="标记选中行删除（提交时执行）">标记删除</button>
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
                <button :class="{on: (activeTab.view||'table')==='table'}" @click="setView('table')" title="表格视图">▦</button>
                <button :class="{on: activeTab.view==='chart'}" @click="setView('chart')" title="图表视图">📊</button>
              </span>
              <span class="dg-menu">
                <button class="exp-dl" @click.stop="exportOpen=!exportOpen" title="导出结果">⬇</button>
                <div v-if="exportOpen" class="dg-menu-pop">
                  <button @click="exportAs('csv')">CSV</button><button @click="exportAs('json')">JSON</button>
                  <button @click="exportAs('markdown')">Markdown</button><button @click="exportAs('xlsx')">Excel (.xlsx)</button>
                </div>
              </span>
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
              <th v-for="(c,ci) in activeTab.result.columns" :key="c"
                  :class="{sortable: activeTab.type==='data'}"
                  @click="activeTab.type==='data' && cycleOrder(c)"
                  @contextmenu="activeTab.type==='data' && openColMenu($event, ci)"
                  :title="activeTab.type==='data' ? '点击排序（走 SQL）· 右键改显示类型' : c">
                <span class="ctype" :class="'ct-'+colCat(ci)" :title="'类型：'+(colCat(ci)||'未知')">{{ colGlyph(ci) }}</span>{{ c }}{{ orderMark(c) }}
                <span v-if="activeTab.type==='data' && displayTypeOf(ci)!=='number'" class="disp-badge" title="按时间戳展示（仅展示，不改值）">🕒</span>
                <span v-if="activeTab.type==='data'" class="funnel" @click.stop="funnel(c)" title="按此列筛选（填入 WHERE）">⧩</span>
              </th>
            </tr></thead>
            <tbody>
            <!-- 暂存的新增行 -->
            <tr v-for="(add,ai) in activeTab.adds" :key="'a'+ai" class="dg-newrow">
              <td class="gut nw" @click="removeAdd(ai)" title="移除此新增行">✕</td>
              <td v-for="(c,ci) in activeTab.result.columns" :key="'a'+ai+'-'+ci">
                <input v-model="add.values[ci]" :placeholder="c" @keydown.esc="removeAdd(ai)">
              </td>
            </tr>
            <tr v-for="(row,ri) in activeTab.result.rows" :key="ri"
                :class="{rsel: activeTab.rowSel && activeTab.rowSel[ri], delrow: isDelRow(ri)}">
              <td class="gut" @mousedown.prevent @click.stop="rowClick(ri,$event)"
                  :title="isDelRow(ri) ? '已标记删除（提交时执行）' : '点击选择行'">{{ isDelRow(ri) ? '␡' : ri+1 }}</td>
              <td v-for="(v,ci) in row" :key="ci" :title="cellTitle(v)" :data-cell="ri+':'+ci"
                  :class="{editable: activeTab.type==='data', vsel: activeTab.vsel && activeTab.vsel.ri===ri && activeTab.vsel.ci===ci,
                           edited: isEditedCell(ri,ci), hit: isHit(ri,ci), curhit: isCurHit(ri,ci)}"
                  @click="cellClick(ri,ci)"
                  @dblclick="activeTab.type==='data' && startEdit(ri,ci)">
                <input v-if="activeTab.edit && activeTab.edit.ri===ri && activeTab.edit.ci===ci"
                       id="dg-cell-input" class="dg-cell-edit"
                       :type="dtInputType(activeTab.edit.dtKind)" step="1"
                       v-model="activeTab.edit.val"
                       @keydown.enter="commitEdit" @keydown.esc="cancelEdit"
                       @blur="onCellBlur" @change="onCellChange">
                <template v-else-if="activeTab.type==='data'"><span v-if="cellNull(ri,ci)" class="nul">NULL</span><span v-else>{{ cellShow(ri,ci) }}</span></template>
                <template v-else><span v-if="v===null" class="nul">NULL</span><span v-else>{{ cellText(v) }}</span></template>
              </td>
            </tr></tbody>
          </table></div>
          <div v-if="vpOpen && (activeTab.type==='data' || activeTab.type==='query') && activeTab.vsel" class="dg-vp">
            <div class="vp-hd">
              <span class="vt" :class="{on: vpTab==='value'}" @click="vpTab='value'">Value</span>
              <span class="vt" :class="{on: vpTab==='record'}" @click="vpTab='record'">Record</span>
              <span class="vp-x" @click="vpOpen=false">✕</span>
            </div>
            <template v-if="vpTab==='value'">
              <div class="vp-col">{{ activeTab.result.columns[activeTab.vsel.ci] }}</div>
              <textarea class="vp-ta" v-model="vpVal" :disabled="vpNull" :readonly="activeTab.type!=='data'"
                        placeholder="（空字符串）" spellcheck="false"></textarea>
              <div class="vp-fmt">
                <button class="dg-btn" @click="vpFormat(true)" title="JSON 美化（缩进 2 空格）">格式化 JSON</button>
                <button class="dg-btn" @click="vpFormat(false)" title="JSON 压缩为单行">压缩</button>
              </div>
              <template v-if="activeTab.type==='data'">
                <label class="vp-null"><input type="checkbox" v-model="vpNull"> 设为 NULL</label>
                <div class="vp-acts">
                  <button class="dg-btn run" :disabled="!vpDirty()" @click="vpSave"
                          title="暂存改动，点工具栏「提交」写库">保存改动（暂存）</button>
                  <span v-if="vpDirty()" class="vp-dirty">已修改</span>
                </div>
              </template>
              <div v-else class="vp-null">只读 · 查询结果不可就地编辑（可格式化查看）</div>
            </template>
            <template v-else>
              <div class="vp-rec">
                <div v-for="r in vpRecord()" :key="r.col" class="vp-rec-row" :class="{cur: r.cur}">
                  <div class="rc-hd"><span class="rc">{{ r.col }}</span><span class="rt" v-if="r.type">{{ r.type }}</span></div>
                  <div class="rv"><i v-if="r.val===null" class="nul">NULL</i>
                    <pre v-else-if="r.isJson" class="rj">{{ r.pretty }}</pre>
                    <template v-else>{{ r.val }}</template></div>
                </div>
              </div>
            </template>
          </div>
          </div>
          <!-- 表数据视图：底部可拖拽的 SQL 执行记录面板 -->
          <template v-if="activeTab.type==='data' && dataLogH>0">
            <div class="dg-loghandle" @mousedown="beginDrag($event,'log')" title="拖动调整高度"></div>
            <div class="dg-datalog" :style="{height: dataLogH+'px'}">
              <div class="dl-hd">SQL 执行记录 <span class="muted">· 本连接最近</span>
                <span class="dl-x" @click="loadHistory" title="刷新">↻</span></div>
              <div class="dl-body">
                <div v-if="!history.length" class="dl-empty">（暂无记录）</div>
                <div v-for="(h,hi) in history" :key="hi" class="dl-row" :title="h.sql" @click="openHistory(h)">
                  <span class="st" :class="h.status==='ok'?'ok':'bad'">{{ h.status==='ok'?'✓':'✗' }}</span>
                  <span class="sql">{{ h.sql }}</span>
                  <span class="tm">{{ fmtTs(h.ts).slice(5) }}</span>
                </div>
              </div>
            </div>
          </template>
        </template>
        <!-- 执行中：结果区显示加载态（数据 tab 先渲染已知列头 + 骨架行，查询 tab 转圈提示） -->
        <div v-else-if="activeTab.running" class="dg-res-loading">
          <div class="dg-res-loadbar"><span class="dg-run-ico">⟳</span>
            <span>查询执行中… {{ runElapsed }}</span>
            <button class="dg-btn" @click="cancelJob" title="取消执行：向数据库发 KILL QUERY">取消</button></div>
          <div v-if="loadingCols.length" class="dg-res-scroll"><table class="dg-rt">
            <thead><tr><th class="gut">#</th><th v-for="c in loadingCols" :key="c">{{ c }}</th></tr></thead>
            <tbody><tr v-for="n in 8" :key="n" class="dg-skel-row"><td class="gut"></td>
              <td v-for="c in loadingCols" :key="c"><span class="dg-skel"></span></td></tr></tbody>
          </table></div>
        </div>
        <div v-else class="dg-res-empty">{{ activeTab.type==='data' ? "加载中…" : "运行查询查看结果（⌘/Ctrl+Enter）。" }}</div>
      </template>
    </div>
  </section>
  <div v-if="tabCtx.show" class="dg-ctx" :style="{left: tabCtx.x+'px', top: tabCtx.y+'px'}" @click.stop>
    <button @click="beginRename(tabCtx.id)">改名</button>
    <button @click="tabCtxPin">{{ tabCtxTarget() && tabCtxTarget().pinned ? '取消固定' : '固定（防误关）' }}</button>
    <div class="sep"></div>
    <button @click="tabCtxClose">关闭</button>
    <button @click="closeOthers(tabCtx.id)">关闭其他</button>
    <button @click="closeAll">关闭全部</button>
  </div>
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
  <!-- 列显示类型菜单（数值列右键；仅展示不改值） -->
  <template v-if="colMenu.show">
    <div class="dg-ctx-backdrop" @click="closeColMenu" @contextmenu.prevent="closeColMenu"></div>
    <div class="dg-ctx dg-colmenu" :style="{left: colMenu.x+'px', top: colMenu.y+'px'}" @click.stop>
      <div class="hd">更改显示类型 · {{ activeTab && activeTab.result ? activeTab.result.columns[colMenu.ci] : '' }}</div>
      <button :class="{on: displayTypeOf(colMenu.ci)==='number'}" @click="setDisplayType('number')">Number（原值）</button>
      <button :class="{on: displayTypeOf(colMenu.ci)==='ts_s'}" @click="setDisplayType('ts_s')">Timestamp（秒）</button>
      <button :class="{on: displayTypeOf(colMenu.ci)==='ts_ms'}" @click="setDisplayType('ts_ms')">Timestamp（毫秒）</button>
      <button :class="{on: displayTypeOf(colMenu.ci)==='ts_us'}" @click="setDisplayType('ts_us')">Timestamp（微秒）</button>
    </div>
  </template>
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
