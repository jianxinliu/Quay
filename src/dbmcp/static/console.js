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
    props: ["tname", "meta", "open", "sub", "selected", "pad"],
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
    <span class="ic">▦</span><span class="nm">{{ tname }}</span>
  </div>
  <template v-if="open">
    <div v-if="!meta" class="dg-empty" :style="{paddingLeft:(pad+22)+'px'}">加载中…</div>
    <template v-else>
      <div class="dg-item sub" :style="{paddingLeft:(pad+16)+'px'}" @click="$emit('togglesub','columns')">
        <span class="tw">{{ sub.columns ? "▾" : "▸" }}</span><span class="ic fold">▤</span>
        <span class="nm">columns</span><span class="cnt">{{ meta.columns.length }}</span></div>
      <template v-if="sub.columns">
        <div v-for="c in meta.columns" :key="c.name" class="dg-col" :style="{paddingLeft:(pad+38)+'px'}">
          <span class="cn" :class="{pk: pkset[c.name]}">{{ c.name }}</span><span class="ct">{{ c.type }}</span></div>
      </template>
      <div class="dg-item sub" :style="{paddingLeft:(pad+16)+'px'}" @click="$emit('togglesub','keys')">
        <span class="tw">{{ sub.keys ? "▾" : "▸" }}</span><span class="ic fold">⚿</span>
        <span class="nm">keys</span><span class="cnt">{{ meta.primary_key.length ? 1 : 0 }}</span></div>
      <template v-if="sub.keys">
        <div v-if="!meta.primary_key.length" class="dg-empty" :style="{paddingLeft:(pad+38)+'px'}">（无主键）</div>
        <div v-else class="dg-col" :style="{paddingLeft:(pad+38)+'px'}">
          <span class="cn pk">PRIMARY</span><span class="ct">{{ meta.primary_key.join(", ") }}</span></div>
      </template>
      <div class="dg-item sub" :style="{paddingLeft:(pad+16)+'px'}" @click="$emit('togglesub','indexes')">
        <span class="tw">{{ sub.indexes ? "▾" : "▸" }}</span><span class="ic fold">≡</span>
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

  var app = Vue.createApp({
    data: function () {
      return {
        connections: [], tabs: [], activeId: null,
        // 树状态（当前连接）
        databases: [], tablesByDb: {}, tableMeta: {},
        openDb: {}, openTf: {}, openTbl: {}, openSub: {},
        tablesLoading: false, lastLoadedConn: null, schemaFilter: "",
        treeCache: {},          // conn -> 树快照（切连接/切页保活）
        selected: {},           // metaKey -> {t, db}（多选）
        dragId: null,
        ctx: { show: false, x: 0, y: 0, table: "", schema: "", multi: false },
        dropPlan: null,         // {items:[{t,db}], running, results}
        delSnip: null,
        snippets: [], showSnipForm: false, snipDraft: { title: "", note: "" },
        exportOpen: false, editorReady: false, toast: "",
        leftW: 264, editorH: 300,
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
        var q = this.schemaFilter.trim().toLowerCase();
        return q ? this.databases.filter(function (d) { return d.toLowerCase().indexOf(q) >= 0; })
                 : this.databases;
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
        var defSchema = opts.schema != null ? opts.schema : (this.activeTab ? this.activeTab.schema : "");
        var id = seq++;
        var tab = { id: id, title: opts.title || ("查询 " + id), conn: opts.conn || def,
                    schema: defSchema || "", type: opts.type || "query", table: opts.table || "",
                    sql: opts.sql || "", result: null, confirm: null, ok: null, err: null, running: false };
        this.tabs.push(tab);
        if (monacoReady) models.set(id, window.monaco.editor.createModel(tab.sql, "sql"));
        this.switchTab(id);
        this.persist();
        return tab;
      },
      closeTab: function (id) {
        var i = this.tabs.findIndex(function (t) { return t.id === id; });
        if (i < 0) return;
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
          if (!self.tabs.length) self.newTab({});
          else if (self.activeTab) self.loadTree();
        });
      },
      setConn: function (val) { if (this.activeTab) { this.activeTab.conn = val; this.persist(); this.loadTree(); } },
      setSchema: function (val) { if (this.activeTab) { this.activeTab.schema = val; this.persist(); } },
      stashTree: function () {
        if (!this.lastLoadedConn) return;
        this.treeCache[this.lastLoadedConn] = {
          databases: this.databases, tablesByDb: this.tablesByDb, tableMeta: this.tableMeta,
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
        this.databases = []; this.tablesByDb = {}; this.tableMeta = {};
        this.openDb = {}; this.openTf = {}; this.openTbl = {}; this.openSub = {};
        this.schemaFilter = ""; this.selected = {};
        if (!t || !t.conn) { this.lastLoadedConn = null; currentTables = []; currentConn = ""; return; }
        this.lastLoadedConn = t.conn; currentConn = t.conn;
        var cached = !force && this.treeCache[t.conn];
        if (cached) {  // 快照恢复：不发任何请求
          this.databases = cached.databases; this.tablesByDb = cached.tablesByDb;
          this.tableMeta = cached.tableMeta; this.openDb = cached.openDb;
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
          this.newTab({ type: "data", title: "count " + t, table: t,
                        sql: "SELECT count(*) AS total FROM " + q, schema: s });
          this.$nextTick(function () { self.run(false); });
        }
        else if (act === "copy") {
          var text = multi ? Object.values(this.selected).map(this.qn).join(", ") : q;
          (navigator.clipboard ? navigator.clipboard.writeText(text) : Promise.reject())
            .then(function () { self.flash("已复制 " + text.slice(0, 60)); })
            .catch(function () { self.flash(text); });
        }
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

      // ---------- 运行 / 分页 / 导出 ----------
      run: function (confirm, page) {
        var self = this, t = this.activeTab;
        if (!t) return;
        if (t.type === "ddl") { this.flash("DDL 为只读视图"); return; }
        if (!t.conn) { this.flash("请先选择连接"); return; }
        var sql = this.currentSql();
        if (!sql.trim()) { this.flash("请输入 SQL"); return; }
        page = page || 0;
        t.running = true; t.err = null; t.ok = null; t.confirm = null;
        if (page === 0) t.result = null;
        apiPost("/admin/sql/run", { conn: t.conn, sql: sql, confirm: confirm ? "1" : null,
                                    page: page, schema: t.schema || null })
          .then(function (d) {
            t.running = false;
            if (!d.ok) { t.err = d.error; self.persist(); return; }
            if (d.kind === "read") t.result = d;
            else if (d.kind === "confirm") t.confirm = { risk: d.risk || {}, statement_kind: d.statement_kind };
            else if (d.kind === "write") { t.ok = d; self.refreshTree(); }
            self.persist();
          }).catch(function (e) { t.running = false; t.err = "" + e; });
      },
      goPage: function (p) { if (p >= 0) this.run(false, p); },
      confirmRun: function () { if (this.activeTab) this.activeTab.confirm = null; this.run(true); },
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

      // ---------- 持久化（全状态保活） ----------
      persist: function () {
        try {
          this.stashTree();
          var tabs = this.tabs.map(function (t) {
            return { id: t.id, title: t.title, conn: t.conn, schema: t.schema || "",
                     type: t.type || "query", table: t.table || "", sql: this.sqlOf(t),
                     result: t.result, ok: t.ok, err: t.err };
          }, this);
          var data = { v: 2, tabs: tabs, activeId: this.activeId, treeCache: this.treeCache,
                       leftW: this.leftW, editorH: this.editorH };
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
                     confirm: null, running: false };
          });
          this.activeId = d.activeId || this.tabs[0].id;
          this.treeCache = d.treeCache || {};
          if (d.leftW) this.leftW = d.leftW;
          if (d.editorH) this.editorH = d.editorH;
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
        monaco.languages.registerCompletionItemProvider("sql", {
          triggerCharacters: ["."],
          provideCompletionItems: function (model, position) {
            var w = model.getWordUntilPosition(position);
            var range = { startLineNumber: position.lineNumber, endLineNumber: position.lineNumber,
                          startColumn: w.startColumn, endColumn: w.endColumn };
            var line = model.getValueInRange({ startLineNumber: position.lineNumber, startColumn: 1,
                                               endLineNumber: position.lineNumber, endColumn: position.column });
            var dot = /([A-Za-z_][\w$]*)\.\s*[\w$]*$/.exec(line);
            if (dot) {
              var table = resolveTable(model.getValue(), dot[1]);
              return fetchCols(table).then(function (cols) {
                return { suggestions: cols.map(function (c) {
                  return { label: c.name, kind: monaco.languages.CompletionItemKind.Field,
                           insertText: c.name, range: range, detail: (c.type || "列") };
                }) };
              });
            }
            return { suggestions: currentTables.map(function (t) {
              return { label: t, kind: monaco.languages.CompletionItemKind.Struct,
                       insertText: t, range: range, detail: "表" };
            }) };
          }
        });
        this.editorReady = true;
      },
    },
    mounted: function () {
      var self = this;
      this.restore();
      this.loadConnections().then(function () { self.loadSnippets(); });
      loadMonaco(function () { self.initEditor(); });
      window.addEventListener("beforeunload", function () { self.persist(); });
      document.addEventListener("click", function () { self.closeCtx(); self.exportOpen = false; });
    },
    template: `
<div class="dg-root">
  <aside class="dg-left" :style="{width: leftW + 'px'}">
    <div class="dg-conn">
      <select :value="activeTab ? activeTab.conn : ''" @change="setConn($event.target.value)">
        <option value="">选择连接…</option>
        <option v-for="c in connections" :key="c.value" :value="c.value">{{ c.connection }} · {{ c.engine }}<template v-if="c.environment"> ({{ c.environment }})</template></option>
      </select>
    </div>
    <div class="dg-tree">
      <div class="dg-sec-hd"><span>{{ needsDb ? "库 / 表" : "表" }}</span>
        <span v-if="selCount" class="selinfo">已选 {{ selCount }} <a @click="clearSel">清除</a></span>
        <span class="act" @click="refreshTree" title="刷新（重新拉取）">↻</span></div>
      <div v-if="!activeTab || !activeTab.conn" class="dg-empty">先选择连接</div>
      <template v-else-if="needsDb">
        <div v-if="databases.length > 6" class="dg-filter"><input v-model="schemaFilter" placeholder="筛选库…"></div>
        <div v-if="!databases.length" class="dg-empty">（加载中或无可用库）</div>
        <div v-else-if="!filteredDatabases.length" class="dg-empty">（无匹配库）</div>
        <template v-for="db in filteredDatabases" :key="db">
          <div class="dg-item" @click="toggleDb(db)">
            <span class="tw">{{ openDb[db] ? "▾" : "▸" }}</span><span class="ic">🗄</span><span class="nm">{{ db }}</span>
          </div>
          <template v-if="openDb[db]">
            <div class="dg-item sub" style="padding-left:22px" @click="toggleTf(db)">
              <span class="tw">{{ openTf[db] ? "▾" : "▸" }}</span><span class="ic fold">▤</span>
              <span class="nm">tables</span><span class="cnt">{{ tablesByDb[db] ? tablesByDb[db].length : "…" }}</span></div>
            <template v-if="openTf[db]">
              <div v-if="!tablesByDb[db]" class="dg-empty" style="padding-left:40px">加载中…</div>
              <div v-else-if="!tablesByDb[db].length" class="dg-empty" style="padding-left:40px">（无表）</div>
              <tbl-node v-else v-for="t in tablesByDb[db]" :key="db+'.'+t"
                :tname="t" :meta="tableMeta[mk(t,db)]" :open="!!openTbl[mk(t,db)]"
                :sub="openSub[mk(t,db)]||{}" :selected="!!selected[mk(t,db)]" :pad="38"
                @toggle="toggleTbl(t,db)" @togglesub="s=>toggleSub(t,db,s)"
                @rowclick="e=>clickTable(e,t,db)" @opendata="openTableTab(t,db)"
                @ctxmenu="e=>openCtx(e,t,db)"/>
            </template>
          </template>
        </template>
      </template>
      <template v-else>
        <div class="dg-item sub" @click="toggleTf('')">
          <span class="tw">{{ openTf[''] ? "▾" : "▸" }}</span><span class="ic fold">▤</span>
          <span class="nm">tables</span><span class="cnt">{{ tablesByDb[''] ? tablesByDb[''].length : "…" }}</span></div>
        <template v-if="openTf['']">
          <div v-if="tablesLoading" class="dg-empty" style="padding-left:24px">加载中…</div>
          <div v-else-if="!tablesByDb[''] || !tablesByDb[''].length" class="dg-empty" style="padding-left:24px">（无表）</div>
          <tbl-node v-else v-for="t in tablesByDb['']" :key="t"
            :tname="t" :meta="tableMeta[mk(t,'')]" :open="!!openTbl[mk(t,'')]"
            :sub="openSub[mk(t,'')]||{}" :selected="!!selected[mk(t,'')]" :pad="22"
            @toggle="toggleTbl(t,'')" @togglesub="s=>toggleSub(t,'',s)"
            @rowclick="e=>clickTable(e,t,'')" @opendata="openTableTab(t,'')"
            @ctxmenu="e=>openCtx(e,t,'')"/>
        </template>
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
    <div class="dg-top">
      <button class="dg-btn run" :disabled="!activeTab || activeTab.running" @click="run(false)">▶ {{ activeTab && activeTab.running ? "执行中…" : (activeTab && activeTab.type==='data' ? "刷新" : "运行") }}</button>
      <button class="dg-btn" @click="formatSql">格式化</button>
      <div class="dg-menu">
        <button class="dg-btn" @click.stop="exportOpen=!exportOpen">导出 ▾</button>
        <div v-if="exportOpen" class="dg-menu-pop">
          <button @click="exportAs('csv')">CSV</button><button @click="exportAs('json')">JSON</button>
          <button @click="exportAs('markdown')">Markdown</button><button @click="exportAs('xlsx')">Excel (.xlsx)</button>
        </div>
      </div>
      <button class="dg-btn" @click="saveSqlFile">保存 .sql</button>
      <span class="sp"></span>
      <label v-if="connMeta && (connMeta.engine==='mysql'||connMeta.engine==='postgres')" class="dg-schema-pick">执行 schema
        <select :value="activeTab?activeTab.schema:''" @change="setSchema($event.target.value)">
          <option value="">{{ connMeta.database ? "默认（"+connMeta.database+"）" : "未指定" }}</option>
          <option v-for="db in databases" :key="db" :value="db">{{ db }}</option>
        </select></label>
      <span class="hint">⌘/Ctrl+Enter 运行 · ⌃Space 补全</span>
    </div>
    <div class="dg-tabs">
      <div v-for="t in tabs" :key="t.id" class="dg-tab" :class="{active: t.id===activeId, drag: t.id===dragId}" @click="switchTab(t.id)"
           draggable="true" @dragstart="onTabDragStart(t.id, $event)" @dragover.prevent @drop="onTabDrop(t.id)" @dragend="dragId=null">
        <span class="ticon" v-if="t.type==='data'">▦</span><span class="ticon" v-else-if="t.type==='ddl'">≔</span>
        <span class="nm">{{ t.title }}</span><span class="x" @click.stop="closeTab(t.id)">✕</span>
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
    <div class="dg-editor-row" v-show="activeTab && activeTab.type!=='data'" :style="editorRowStyle">
      <div class="dg-estatus" :class="execState.cls" :title="execState.tip"><span>{{ execState.icon }}</span></div>
      <div class="dg-editor"><div ref="editorEl" style="position:absolute;inset:0"></div>
        <div v-if="!editorReady" class="dg-editor-loading">编辑器加载中…</div>
      </div>
    </div>
    <div class="dg-hsplit" v-show="activeTab && activeTab.type==='query'" @mousedown="beginDrag($event, 'y')"></div>
    <div class="dg-results" v-show="activeTab && activeTab.type!=='ddl'">
      <template v-if="activeTab">
        <div v-if="activeTab.confirm" class="dg-confirm">
          <h4>确认执行写操作 <span class="lv" :style="{background: lvColor(activeTab.confirm.risk.level)}">{{ activeTab.confirm.risk.level }}</span> <span style="color:var(--dg-muted);font-weight:normal">{{ activeTab.confirm.statement_kind }}</span></h4>
          <div style="font-size:12px;color:var(--dg-muted)">将用 writer 账号<b>直接执行</b>并记入审计（后台旁路，不进审批单）。</div>
          <div class="kv"><span>影响表：{{ (activeTab.confirm.risk.tables||[]).join(", ")||"—" }}</span><span>表行量级：{{ numOr(activeTab.confirm.risk.row_estimate) }}</span><span>含 WHERE：{{ boolText(activeTab.confirm.risk.has_where) }}</span><span>命中索引：{{ boolText(activeTab.confirm.risk.uses_index) }}</span></div>
          <div class="reasons" v-for="r in (activeTab.confirm.risk.reasons||[])" :key="r">• {{ r }}</div>
          <div class="acts"><button class="dg-btn ok" @click="confirmRun">确认执行</button><button class="dg-btn" @click="cancelConfirm">取消</button></div>
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
            <span v-if="activeTab.result.columns.length" class="exp">
              <button @click="exportAs('csv')">CSV</button><button @click="exportAs('json')">JSON</button><button @click="exportAs('markdown')">MD</button><button @click="exportAs('xlsx')">XLSX</button>
            </span>
          </div>
          <div v-if="!activeTab.result.columns.length" class="dg-res-empty">语句已执行，无结果集。</div>
          <div v-else class="dg-res-scroll"><table class="dg-rt">
            <thead><tr><th v-for="c in activeTab.result.columns" :key="c">{{ c }}</th></tr></thead>
            <tbody><tr v-for="(row,ri) in activeTab.result.rows" :key="ri"><td v-for="(v,ci) in row" :key="ci" :title="cellTitle(v)"><span v-if="v===null" class="nul">NULL</span><span v-else>{{ cellText(v) }}</span></td></tr></tbody>
          </table></div>
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
    </template>
    <template v-else>
      <button @click="ctxAction('copy')">复制表名列表</button>
    </template>
    <div class="sep"></div>
    <button class="danger" @click="ctxAction('drop')">{{ ctx.multi ? ("DROP " + selCount + " 张表…") : "DROP 表…" }}</button>
  </div>
  <div id="dg-toast" v-if="toast">{{ toast }}</div>
</div>`
  });

  app.component("tbl-node", TblNode);
  app.mount("#dbm-console");
})();
