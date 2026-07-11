/* 查询台前端：Vue 3 + Monaco。DataGrip 风多 tab IDE，少弹框、一屏操作。
 *
 * Monaco 编辑器实例与各 tab 的 model 放在模块级变量（体积大，不进 Vue 响应式，
 * 避免被 Proxy 包坏）；Vue 只管标签元数据、树、结果、片段等可序列化状态。
 * 数据全走 /admin/sql/* JSON 接口。
 */
(function () {
  "use strict";
  var Vue = window.Vue;

  var editor = null;          // 单个 Monaco 编辑器，切 tab 换 model
  var models = new Map();     // tabId -> ITextModel
  var monacoReady = false;
  var currentTables = [];     // 供补全 provider 读取（当前连接的表名，未绑库时为 库.表）
  var currentConn = "";       // 供补全 provider 按需拉列
  var tableSchema = {};       // 表名 -> schema（未绑库时按需取列要带 schema）
  var colCache = {};          // "conn|schema|table" -> [{name,type}]，列名补全缓存
  var seq = 1;

  // 补全时用于排除“把子句关键字误当别名”的保留词
  var RESERVED = {};
  ("where on group order by having limit join inner left right full outer cross " +
   "union select from set values as using natural").split(" ")
    .forEach(function (w) { RESERVED[w] = 1; });

  // 从 SQL 文本解析 别名/表名 → 真实表名（FROM/JOIN [库.]表 [AS] 别名）
  function resolveTable(sql, ident) {
    var lo = ident.toLowerCase(), map = {};
    // 组1=库(可选) 组2=表 组3=别名(可选)
    var re = /\b(?:from|join)\s+`?(?:([a-zA-Z_][\w$]*)`?\.`?)?([a-zA-Z_][\w$]*)`?(?:\s+(?:as\s+)?`?([a-zA-Z_][\w$]*)`?)?/gi, m;
    while ((m = re.exec(sql))) {
      var schema = m[1], table = m[2], alias = m[3];
      if (schema) tableSchema[table] = schema;         // 记录表所属库，供按 schema 取列
      if (alias && !RESERVED[alias.toLowerCase()]) map[alias.toLowerCase()] = table;
    }
    if (map[lo]) return map[lo];                        // 命中别名
    for (var i = 0; i < currentTables.length; i++) {   // ident 本身是表（可能是 库.表）
      var ct = currentTables[i], bare = ct.indexOf(".") >= 0 ? ct.split(".").pop() : ct;
      if (ct.toLowerCase() === lo || bare.toLowerCase() === lo) return bare;
    }
    return ident;  // 兜底：当作真实表名尝试取列
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

  // ---------- 小工具 ----------
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

  var app = Vue.createApp({
    data: function () {
      return {
        connections: [], tabs: [], activeId: null,
        tables: [], colsByTable: {}, expanded: {}, tablesLoading: false, lastLoadedConn: null,
        databases: [], dbExpanded: {}, tablesByDb: {},  // 未绑库连接：库→表 两级
        snippets: [], showSnipForm: false, snipDraft: { title: "", note: "" },
        exportOpen: false, editorReady: false, toast: "",
        leftW: 264, editorH: 300,  // 可拖动分隔条控制的尺寸
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
      // 未绑定默认库的 MySQL/PG：树需要先列库（否则反射会 None.replace 崩）
      needsDb: function () {
        var m = this.connMeta;
        return !!m && (m.engine === "mysql" || m.engine === "postgres") && !m.database;
      }
    },
    methods: {
      // ----- 通用 -----
      flash: function (m) { var self = this; this.toast = m; clearTimeout(this._tt);
        this._tt = setTimeout(function () { self.toast = ""; }, 2600); },
      lvColor: function (l) { return LVL[l] || "#666"; },
      numOr: function (v) { return v == null ? "未知" : "约 " + v; },
      boolText: function (v) { return v === true ? "是" : v === false ? "否" : "未知"; },
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

      // ----- 标签页 -----
      newTab: function (opts) {
        opts = opts || {};
        var def = this.activeTab ? this.activeTab.conn : (this.connections[0] ? this.connections[0].value : "");
        var id = seq++;
        var tab = { id: id, title: opts.title || ("查询 " + id), conn: opts.conn || def,
                    sql: opts.sql || "", result: null, confirm: null, ok: null, err: null, running: false };
        this.tabs.push(tab);
        if (monacoReady) { models.set(id, window.monaco.editor.createModel(tab.sql, "sql")); }
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
        var m = models.get(id);
        if (editor && m) editor.setModel(m);
        var t = this.activeTab;
        if (t && t.conn !== this.lastLoadedConn) this.loadTree();
        var self = this; this.$nextTick(function () { if (editor) editor.focus(); });
      },

      // ----- 连接 / 库 / 表 -----
      loadConnections: function () {
        var self = this;
        return apiGet("/admin/sql/connections").then(function (d) {
          self.connections = (d && d.connections) || [];
          if (!self.tabs.length) self.newTab({});
          else if (self.activeTab) self.loadTree();
        });
      },
      setConn: function (val) { if (this.activeTab) { this.activeTab.conn = val; this.persist(); this.loadTree(); } },
      colKey: function (table, schema) { return schema ? schema + " " + table : table; },
      loadTree: function () {
        var self = this; var t = this.activeTab;
        this.expanded = {}; this.colsByTable = {}; this.dbExpanded = {}; this.tablesByDb = {};
        this.tables = []; this.databases = []; tableSchema = {};
        if (!t || !t.conn) { this.lastLoadedConn = null; currentTables = []; currentConn = ""; return; }
        this.lastLoadedConn = t.conn; currentConn = t.conn; currentTables = [];
        if (this.needsDb) {  // 未绑库：先列库
          apiGet("/admin/sql/databases?conn=" + encodeURIComponent(t.conn)).then(function (d) {
            self.databases = d && d.ok ? d.databases : [];
            if (d && !d.ok) self.flash(d.error);
          });
          return;
        }
        this.tablesLoading = true;
        apiGet("/admin/sql/tables?conn=" + encodeURIComponent(t.conn)).then(function (d) {
          self.tablesLoading = false;
          self.tables = d && d.ok ? d.tables : [];
          currentTables = self.tables.slice();
          if (d && !d.ok) self.flash(d.error);
        }).catch(function () { self.tablesLoading = false; self.tables = []; });
      },
      toggleDb: function (db) {
        var open = !this.dbExpanded[db];
        this.dbExpanded[db] = open;
        if (open && !this.tablesByDb[db]) {
          var self = this, t = this.activeTab;
          apiGet("/admin/sql/tables?conn=" + encodeURIComponent(t.conn) + "&schema=" + encodeURIComponent(db))
            .then(function (d) {
              if (!d.ok) { self.flash(d.error); self.tablesByDb[db] = []; return; }
              self.tablesByDb[db] = d.tables || [];
              d.tables.forEach(function (tb) {   // 补全：库.表 可选，列名按 schema 取
                tableSchema[tb] = db;
                if (currentTables.indexOf(db + "." + tb) < 0) currentTables.push(db + "." + tb);
              });
            });
        }
      },
      toggleTable: function (name, schema) {
        var ck = this.colKey(name, schema);
        var open = !this.expanded[ck];
        this.expanded[ck] = open;
        if (open && !this.colsByTable[ck]) {
          var self = this, t = this.activeTab;
          var url = "/admin/sql/table?conn=" + encodeURIComponent(t.conn) + "&table=" + encodeURIComponent(name) +
                    (schema ? "&schema=" + encodeURIComponent(schema) : "");
          apiGet(url).then(function (d) {
            if (!d.ok) { self.flash(d.error); return; }
            var pk = d.primary_key || [];
            self.colsByTable[ck] = (d.columns || []).map(function (c) {
              return { name: c.name, type: c.type, pk: pk.indexOf(c.name) >= 0 };
            });
          });
        }
      },
      insertSelect: function (name, schema) {
        var q = schema ? schema + "." + name : name;
        this.insertText("SELECT * FROM " + q + " LIMIT 100;");
      },
      insertText: function (text) {
        if (!editor) return;
        var sel = editor.getSelection();
        editor.executeEdits("insert", [{ range: sel, text: text, forceMoveMarkers: true }]);
        editor.focus();
      },

      // ----- 可拖动分隔条 -----
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
        }
        window.addEventListener("mousemove", move);
        window.addEventListener("mouseup", up);
        document.body.style.userSelect = "none";
      },

      // ----- 运行 / 格式化 / 导出 -----
      run: function (confirm) {
        var self = this, t = this.activeTab;
        if (!t) return;
        if (!t.conn) { this.flash("请先选择连接"); return; }
        var sql = this.currentSql();
        if (!sql.trim()) { this.flash("请输入 SQL"); return; }
        t.running = true; t.err = null; t.ok = null; t.result = null; t.confirm = null;
        this.persist();
        apiPost("/admin/sql/run", { conn: t.conn, sql: sql, confirm: confirm ? "1" : null })
          .then(function (d) {
            t.running = false;
            if (!d.ok) { t.err = d.error; return; }
            if (d.kind === "read") t.result = d;
            else if (d.kind === "confirm") t.confirm = { risk: d.risk || {}, statement_kind: d.statement_kind };
            else if (d.kind === "write") { t.ok = d; self.loadTree(); }
          }).catch(function (e) { t.running = false; t.err = "" + e; });
      },
      confirmRun: function () { if (this.activeTab) this.activeTab.confirm = null; this.run(true); },
      cancelConfirm: function () { if (this.activeTab) this.activeTab.confirm = null; },
      formatSql: function () {
        var t = this.activeTab; if (!t) return;
        apiPost("/admin/sql/format", { conn: t.conn, sql: this.currentSql() }).then(function (d) {
          if (d.ok && d.sql != null) { var m = models.get(t.id); if (m) m.setValue(d.sql); }
        });
      },
      exportAs: function (fmt) {
        this.exportOpen = false;
        var self = this, t = this.activeTab; if (!t) return;
        var fd = new FormData(); fd.append("conn", t.conn); fd.append("sql", this.currentSql()); fd.append("format", fmt);
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

      // ----- 片段 -----
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
      deleteSnippet: function (id) {
        var self = this;
        apiPost("/admin/sql/snippets/delete", { id: id }).then(function (d) {
          if (!d.ok) { self.flash(d.error); return; } self.flash("已删除"); self.loadSnippets();
        });
      },

      // ----- 持久化（刷新保留已开的 tab） -----
      persist: function () {
        try {
          var data = this.tabs.map(function (t) {
            return { id: t.id, title: t.title, conn: t.conn, sql: this.sqlOf(t) };
          }, this);
          localStorage.setItem("dbm-console-tabs", JSON.stringify({ tabs: data, activeId: this.activeId }));
        } catch (e) { /* 忽略 */ }
      },
      restore: function () {
        try {
          var raw = localStorage.getItem("dbm-console-tabs");
          if (!raw) return;
          var d = JSON.parse(raw);
          if (!d.tabs || !d.tabs.length) return;
          this.tabs = d.tabs.map(function (t) {
            return { id: t.id, title: t.title, conn: t.conn, sql: t.sql || "",
                     result: null, confirm: null, ok: null, err: null, running: false };
          });
          this.activeId = d.activeId || this.tabs[0].id;
          seq = Math.max.apply(null, this.tabs.map(function (t) { return t.id; })) + 1;
        } catch (e) { /* 忽略 */ }
      },

      // ----- Monaco 就绪：建 model + 编辑器 + 补全 -----
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
        });
        editor.addCommand(monaco.KeyMod.CtrlCmd | monaco.KeyCode.Enter, function () { self.run(false); });
        editor.onDidBlurEditorText(function () { self.persist(); });
        monaco.languages.registerCompletionItemProvider("sql", {
          triggerCharacters: ["."],
          provideCompletionItems: function (model, position) {
            var w = model.getWordUntilPosition(position);
            var range = { startLineNumber: position.lineNumber, endLineNumber: position.lineNumber,
                          startColumn: w.startColumn, endColumn: w.endColumn };
            // 检测 “表名./别名.” → 提示该表列名（按需拉取、缓存）
            var line = model.getValueInRange({ startLineNumber: position.lineNumber, startColumn: 1,
                                               endLineNumber: position.lineNumber, endColumn: position.column });
            var dot = /([A-Za-z_][\w$]*)\.\s*[\w$]*$/.exec(line);
            if (dot) {
              var table = resolveTable(model.getValue(), dot[1]);
              return fetchCols(table).then(function (cols) {
                return { suggestions: cols.map(function (c) {
                  return { label: c.name, kind: monaco.languages.CompletionItemKind.Field,
                           insertText: c.name, range: range, detail: (c.type || "列") + (c.pk ? " · PK" : "") };
                }) };
              });
            }
            // 否则提示表名
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
    },
    template: [
'<div class="dg-root">',
'  <aside class="dg-left" :style="{width: leftW + \'px\'}">',
'    <div class="dg-conn">',
'      <select :value="activeTab ? activeTab.conn : \'\'" @change="setConn($event.target.value)">',
'        <option value="">选择连接…</option>',
'        <option v-for="c in connections" :key="c.value" :value="c.value">{{c.connection}} · {{c.engine}}<span v-if="c.environment"> ({{c.environment}})</span></option>',
'      </select>',
'    </div>',
'    <div class="dg-tree">',
'      <div class="dg-sec-hd"><span>{{ needsDb ? "库 / 表" : "表" }}</span><span class="act" @click="loadTree" title="刷新">↻</span></div>',
'      <div v-if="!activeTab || !activeTab.conn" class="dg-empty">先选择连接</div>',
'      <template v-else-if="needsDb">',
'        <div v-if="!databases.length" class="dg-empty">（无可用库）</div>',
'        <template v-for="db in databases" :key="db">',
'          <div class="dg-item" @click="toggleDb(db)">',
'            <span class="tw">{{ dbExpanded[db] ? "▾" : "▸" }}</span><span class="ic">🗄</span><span class="nm">{{db}}</span>',
'          </div>',
'          <template v-if="dbExpanded[db]">',
'            <div v-if="!tablesByDb[db]" class="dg-empty" style="padding-left:28px">加载中…</div>',
'            <div v-else-if="!tablesByDb[db].length" class="dg-empty" style="padding-left:28px">（无表）</div>',
'            <template v-for="t in tablesByDb[db]" :key="db+\'.\'+t">',
'              <div class="dg-item" style="padding-left:24px" @click="toggleTable(t, db)">',
'                <span class="tw">{{ expanded[colKey(t,db)] ? "▾" : "▸" }}</span><span class="ic">▧</span>',
'                <span class="nm">{{t}}</span>',
'                <span class="mini" @click.stop="insertSelect(t, db)" title="SELECT * 到编辑器">↵</span>',
'              </div>',
'              <div v-if="expanded[colKey(t,db)]" class="dg-cols">',
'                <div v-if="!colsByTable[colKey(t,db)]" class="dg-empty">加载中…</div>',
'                <div v-else v-for="col in colsByTable[colKey(t,db)]" :key="col.name" class="dg-col">',
'                  <span class="cn" :class="{pk: col.pk}">{{col.name}}</span><span class="ct">{{col.type}}</span>',
'                </div>',
'              </div>',
'            </template>',
'          </template>',
'        </template>',
'      </template>',
'      <template v-else>',
'        <div v-if="tablesLoading" class="dg-empty">加载中…</div>',
'        <div v-else-if="!tables.length" class="dg-empty">（无表）</div>',
'        <template v-for="t in tables" :key="t">',
'          <div class="dg-item" @click="toggleTable(t)">',
'            <span class="tw">{{ expanded[t] ? "▾" : "▸" }}</span><span class="ic">▧</span>',
'            <span class="nm">{{t}}</span>',
'            <span class="mini" @click.stop="insertSelect(t)" title="SELECT * 到编辑器">↵</span>',
'          </div>',
'          <div v-if="expanded[t]" class="dg-cols">',
'            <div v-if="!colsByTable[t]" class="dg-empty">加载中…</div>',
'            <div v-else v-for="col in colsByTable[t]" :key="col.name" class="dg-col">',
'              <span class="cn" :class="{pk: col.pk}">{{col.name}}</span><span class="ct">{{col.type}}</span>',
'            </div>',
'          </div>',
'        </template>',
'      </template>',
'      <div class="dg-sec-hd" style="margin-top:8px"><span>片段</span><span class="act" @click="toggleSnipForm" title="保存当前 SQL 为片段">＋</span></div>',
'      <div v-if="showSnipForm" class="dg-snipform">',
'        <input v-model="snipDraft.title" placeholder="标题">',
'        <textarea v-model="snipDraft.note" rows="2" placeholder="备注（可选）"></textarea>',
'        <div style="display:flex;gap:6px"><button class="dg-btn run" style="flex:1" @click="saveSnippet">保存</button><button class="dg-btn" @click="showSnipForm=false">取消</button></div>',
'      </div>',
'      <div v-if="!snippets.length" class="dg-empty">（暂无片段）</div>',
'      <div v-for="s in snippets" :key="s.id" class="dg-snip" @click="openSnippet(s)">',
'        <div class="t"><span>{{s.title}}</span><span class="x" @click.stop="deleteSnippet(s.id)" title="删除">✕</span></div>',
'        <div v-if="s.note" class="n">{{s.note}}</div>',
'        <div class="c">{{s.connection || "—"}}</div>',
'      </div>',
'    </div>',
'  </aside>',
'  <div class="dg-vsplit" @mousedown="beginDrag($event, \'x\')"></div>',
'  <section class="dg-main">',
'    <div class="dg-top">',
'      <button class="dg-btn run" :disabled="!activeTab || activeTab.running" @click="run(false)">▶ {{ activeTab && activeTab.running ? "执行中…" : "运行" }}</button>',
'      <button class="dg-btn" @click="formatSql">格式化</button>',
'      <div class="dg-menu">',
'        <button class="dg-btn" @click="exportOpen=!exportOpen">导出 ▾</button>',
'        <div v-if="exportOpen" class="dg-menu-pop">',
'          <button @click="exportAs(\'csv\')">CSV</button><button @click="exportAs(\'json\')">JSON</button>',
'          <button @click="exportAs(\'markdown\')">Markdown</button><button @click="exportAs(\'xlsx\')">Excel (.xlsx)</button>',
'        </div>',
'      </div>',
'      <button class="dg-btn" @click="saveSqlFile">保存 .sql</button>',
'      <span class="sp"></span><span class="hint">⌘/Ctrl+Enter 运行 · ⌃Space 补全</span>',
'    </div>',
'    <div class="dg-tabs">',
'      <div v-for="t in tabs" :key="t.id" class="dg-tab" :class="{active: t.id===activeId}" @click="switchTab(t.id)">',
'        <span class="nm">{{t.title}}</span><span class="x" @click.stop="closeTab(t.id)">✕</span>',
'      </div>',
'      <button class="dg-tab-add" @click="newTab({})" title="新建查询">＋</button>',
'    </div>',
'    <div class="dg-editor" :style="{height: editorH + \'px\'}"><div ref="editorEl" style="position:absolute;inset:0"></div>',
'      <div v-if="!editorReady" class="dg-editor-loading">编辑器加载中…</div>',
'    </div>',
'    <div class="dg-hsplit" @mousedown="beginDrag($event, \'y\')"></div>',
'    <div class="dg-results">',
'      <template v-if="activeTab">',
'        <div v-if="activeTab.confirm" class="dg-confirm">',
'          <h4>确认执行写操作 <span class="lv" :style="{background: lvColor(activeTab.confirm.risk.level)}">{{activeTab.confirm.risk.level}}</span> <span style="color:var(--dg-muted);font-weight:normal">{{activeTab.confirm.statement_kind}}</span></h4>',
'          <div style="font-size:12px;color:var(--dg-muted)">将用 writer 账号<b>直接执行</b>并记入审计（后台旁路，不进审批单）。</div>',
'          <div class="kv"><span>影响表：{{(activeTab.confirm.risk.tables||[]).join(", ")||"—"}}</span><span>表行量级：{{numOr(activeTab.confirm.risk.row_estimate)}}</span><span>含 WHERE：{{boolText(activeTab.confirm.risk.has_where)}}</span><span>命中索引：{{boolText(activeTab.confirm.risk.uses_index)}}</span></div>',
'          <div class="reasons" v-for="r in (activeTab.confirm.risk.reasons||[])" :key="r">• {{r}}</div>',
'          <div class="acts"><button class="dg-btn ok" @click="confirmRun">确认执行</button><button class="dg-btn" @click="cancelConfirm">取消</button></div>',
'        </div>',
'        <div v-if="activeTab.err" class="dg-res-err">⚠ {{activeTab.err}}</div>',
'        <div v-else-if="activeTab.ok" class="dg-res-ok">✓ 执行成功，影响 {{activeTab.ok.affected_rows}} 行 · {{activeTab.ok.duration_ms}} ms</div>',
'        <template v-else-if="activeTab.result">',
'          <div class="dg-res-meta">',
'            <span>{{activeTab.result.rows.length}} 行</span>',
'            <span v-if="activeTab.result.duration_ms!=null">{{activeTab.result.duration_ms}} ms</span>',
'            <span v-if="activeTab.result.truncated" style="color:var(--dg-amber)">已截断到 max_rows</span>',
'            <span v-if="activeTab.result.columns.length" class="exp">',
'              <button @click="exportAs(\'csv\')">CSV</button><button @click="exportAs(\'json\')">JSON</button><button @click="exportAs(\'markdown\')">MD</button><button @click="exportAs(\'xlsx\')">XLSX</button>',
'            </span>',
'          </div>',
'          <div v-if="!activeTab.result.columns.length" class="dg-res-empty">语句已执行，无结果集。</div>',
'          <div v-else class="dg-res-scroll"><table class="dg-rt">',
'            <thead><tr><th v-for="c in activeTab.result.columns" :key="c">{{c}}</th></tr></thead>',
'            <tbody><tr v-for="(row,ri) in activeTab.result.rows" :key="ri"><td v-for="(v,ci) in row" :key="ci" :title="cellTitle(v)"><span v-if="v===null" class="nul">NULL</span><span v-else>{{cellText(v)}}</span></td></tr></tbody>',
'          </table></div>',
'        </template>',
'        <div v-else class="dg-res-empty">运行查询查看结果（⌘/Ctrl+Enter）。</div>',
'      </template>',
'    </div>',
'  </section>',
'  <div id="dg-toast" v-if="toast">{{toast}}</div>',
'</div>'
    ].join("")
  });

  app.mount("#dbm-console");
})();
