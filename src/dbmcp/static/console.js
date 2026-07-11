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
  var currentTables = [];     // 供补全 provider 读取（当前连接的表名）
  var seq = 1;

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
        snippets: [], showSnipForm: false, snipDraft: { title: "", note: "" },
        exportOpen: false, editorReady: false, toast: "",
      };
    },
    computed: {
      activeTab: function () {
        var id = this.activeId, ts = this.tabs;
        for (var i = 0; i < ts.length; i++) if (ts[i].id === id) return ts[i];
        return null;
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
        if (t && t.conn !== this.lastLoadedConn) this.loadTables();
        var self = this; this.$nextTick(function () { if (editor) editor.focus(); });
      },

      // ----- 连接 / 表 -----
      loadConnections: function () {
        var self = this;
        return apiGet("/admin/sql/connections").then(function (d) {
          self.connections = (d && d.connections) || [];
          if (!self.tabs.length) self.newTab({});
          else if (self.activeTab) self.loadTables();
        });
      },
      setConn: function (val) { if (this.activeTab) { this.activeTab.conn = val; this.persist(); this.loadTables(); } },
      loadTables: function () {
        var self = this; var t = this.activeTab;
        this.expanded = {}; this.colsByTable = {};
        if (!t || !t.conn) { this.tables = []; this.lastLoadedConn = null; currentTables = []; return; }
        this.lastLoadedConn = t.conn; this.tablesLoading = true;
        apiGet("/admin/sql/tables?conn=" + encodeURIComponent(t.conn)).then(function (d) {
          self.tablesLoading = false;
          self.tables = d && d.ok ? d.tables : [];
          currentTables = self.tables.slice();
          if (d && !d.ok) self.flash(d.error);
        }).catch(function () { self.tablesLoading = false; self.tables = []; });
      },
      toggleTable: function (name) {
        var open = !this.expanded[name];
        this.expanded[name] = open;
        if (open && !this.colsByTable[name]) {
          var self = this, t = this.activeTab;
          apiGet("/admin/sql/table?conn=" + encodeURIComponent(t.conn) + "&table=" + encodeURIComponent(name))
            .then(function (d) {
              if (!d.ok) { self.flash(d.error); return; }
              var pk = d.primary_key || [];
              self.colsByTable[name] = (d.columns || []).map(function (c) {
                return { name: c.name, type: c.type, pk: pk.indexOf(c.name) >= 0 };
              });
            });
        }
      },
      insertSelect: function (name) { this.insertText("SELECT * FROM " + name + " LIMIT 100;"); },
      insertText: function (text) {
        if (!editor) return;
        var sel = editor.getSelection();
        editor.executeEdits("insert", [{ range: sel, text: text, forceMoveMarkers: true }]);
        editor.focus();
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
            else if (d.kind === "write") { t.ok = d; self.loadTables(); }
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
          provideCompletionItems: function (model, position) {
            var w = model.getWordUntilPosition(position);
            var range = { startLineNumber: position.lineNumber, endLineNumber: position.lineNumber,
                          startColumn: w.startColumn, endColumn: w.endColumn };
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
'  <aside class="dg-left">',
'    <div class="dg-conn">',
'      <select :value="activeTab ? activeTab.conn : \'\'" @change="setConn($event.target.value)">',
'        <option value="">选择连接…</option>',
'        <option v-for="c in connections" :key="c.value" :value="c.value">{{c.connection}} · {{c.engine}}<span v-if="c.environment"> ({{c.environment}})</span></option>',
'      </select>',
'    </div>',
'    <div class="dg-tree">',
'      <div class="dg-sec-hd"><span>表</span><span class="act" @click="loadTables" title="刷新">↻</span></div>',
'      <div v-if="!activeTab || !activeTab.conn" class="dg-empty">先选择连接</div>',
'      <div v-else-if="tablesLoading" class="dg-empty">加载中…</div>',
'      <div v-else-if="!tables.length" class="dg-empty">（无表）</div>',
'      <template v-for="t in tables" :key="t">',
'        <div class="dg-item" @click="toggleTable(t)">',
'          <span class="tw">{{ expanded[t] ? "▾" : "▸" }}</span><span class="ic">▧</span>',
'          <span class="nm">{{t}}</span>',
'          <span class="mini" @click.stop="insertSelect(t)" title="SELECT * 到编辑器">↵</span>',
'        </div>',
'        <div v-if="expanded[t]" class="dg-cols">',
'          <div v-if="!colsByTable[t]" class="dg-empty">加载中…</div>',
'          <div v-else v-for="col in colsByTable[t]" :key="col.name" class="dg-col">',
'            <span class="cn" :class="{pk: col.pk}">{{col.name}}</span><span class="ct">{{col.type}}</span>',
'          </div>',
'        </div>',
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
'    <div class="dg-editor"><div ref="editorEl" style="position:absolute;inset:0"></div>',
'      <div v-if="!editorReady" class="dg-editor-loading">编辑器加载中…</div>',
'    </div>',
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
