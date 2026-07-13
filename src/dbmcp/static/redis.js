/*
 * Redis 控制台（对标 Medis）：库→键前缀树 · 命令窗口（Monaco，选中/光标行执行）·
 * 结果区 / 键查看器 · 命令文档面板。独立 Vue 应用，数据全走 /admin/redis/* JSON 接口。
 * 复用 console.css 的后台外壳 + --dg-* 变量 + dg 原语，redis.css 补充本页专属布局。
 */
(function () {
  "use strict";

  var monacoReady = false;
  var editor = null;
  var STORE_KEY = "dbm-redis-v1";

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

  var TYPE_COLOR = {
    string: "#57965c", hash: "#8a63d2", list: "#3574f0", set: "#c9803a",
    zset: "#d9534f", stream: "#3aa9a0", none: "#7a7e85"
  };

  // 前缀树某节点下的键总数（含所有子文件夹）
  function countLeaves(node) {
    var n = node.leaves.length;
    for (var k in node.children) n += countLeaves(node.children[k]);
    return n;
  }

  var app = Vue.createApp({
    data: function () {
      return {
        conns: [], conn: "",
        dbs: [], db: null,
        keys: [], truncated: false, keysLoading: false,
        openFolder: {},               // 前缀路径 -> 展开
        filter: "",                   // 键匹配（SCAN match，默认 *）
        sel: null,                    // 当前选中键名
        keyView: null, keyLoading: false,
        cmdResult: null, cmdErr: null, cmdConfirm: null, running: false,
        doc: null, docCmd: "",        // 当前命令文档 + 命令名
        view: "result",               // result | key —— 主区展示命令结果还是键详情
        editorReady: false, toast: "",
        leftW: 300, docW: 300, editorH: 200,
        theme: "dark", pageSize: 100, // 系统设置：主题 + 结果每页行数
        keyPage: 0, cmdPage: 0,       // 键详情 / 命令结果分页页码
        selField: null,               // 键详情里选中的字段/行 {k, v}
        fmtJson: true,                 // 详情面板：是否美化 JSON
        ctx: { show: false, x: 0, y: 0, kind: "", key: "", path: "" }, // 键树右键菜单
      };
    },
    computed: {
      connMeta: function () {
        for (var i = 0; i < this.conns.length; i++)
          if (this.conns[i].value === this.conn) return this.conns[i];
        return null;
      },
      isProd: function () { var m = this.connMeta; return !!m && m.environment === "prod"; },
      isStaging: function () { var m = this.connMeta; return !!m && m.environment === "staging"; },
      // 键前缀树：按 ":" 分层，返回可渲染的扁平节点列表（含缩进/展开态）
      keyNodes: function () {
        var root = { children: {}, leaves: [] };
        this.keys.forEach(function (k) {
          var parts = k.key.split(":");
          var node = root;
          for (var i = 0; i < parts.length - 1; i++) {
            var p = parts[i];
            if (!node.children[p]) node.children[p] = { children: {}, leaves: [], name: p };
            node = node.children[p];
          }
          node.leaves.push({ key: k.key, type: k.type, label: parts[parts.length - 1] });
        });
        var out = [];
        var open = this.openFolder;
        (function walk(node, prefix, depth) {
          var names = Object.keys(node.children).sort();
          names.forEach(function (nm) {
            var path = prefix ? prefix + ":" + nm : nm;
            var child = node.children[nm];
            var count = countLeaves(child);
            out.push({ kind: "folder", path: path, name: nm, depth: depth, count: count,
                       open: !!open[path] });
            if (open[path]) walk(child, path, depth + 1);
          });
          node.leaves.sort(function (a, b) { return a.key < b.key ? -1 : 1; });
          node.leaves.forEach(function (lf) {
            out.push({ kind: "key", key: lf.key, name: lf.label, type: lf.type, depth: depth });
          });
        })(root, "", 0);
        return out;
      },
      // 结果分页（默认每页 pageSize 行，来自系统设置）
      keyRowsAll: function () { return this.keyView ? this.kvRows(this.keyView) : []; },
      keyRowsPaged: function () {
        var s = this.keyPage * this.pageSize; return this.keyRowsAll.slice(s, s + this.pageSize);
      },
      keyPageCount: function () {
        return Math.max(1, Math.ceil(this.keyRowsAll.length / this.pageSize));
      },
      cmdRowsAll: function () {
        return this.cmdResult ? this.resultRows(this.cmdResult.value) : null;
      },
      cmdRowsPaged: function () {
        if (!this.cmdRowsAll) return null;
        var s = this.cmdPage * this.pageSize; return this.cmdRowsAll.slice(s, s + this.pageSize);
      },
      cmdPageCount: function () {
        return this.cmdRowsAll ? Math.max(1, Math.ceil(this.cmdRowsAll.length / this.pageSize)) : 1;
      }
    },
    methods: {
      flash: function (m) { var self = this; this.toast = m; clearTimeout(this._tt);
        this._tt = setTimeout(function () { self.toast = ""; }, 2600); },
      tcolor: function (t) { return TYPE_COLOR[t] || "#7a7e85"; },
      fmtTtl: function (ttl) {
        if (ttl === -1 || ttl == null) return "永久";
        if (ttl === -2) return "已过期";
        if (ttl < 60) return ttl + " 秒";
        if (ttl < 3600) return Math.floor(ttl / 60) + " 分";
        if (ttl < 86400) return (ttl / 3600).toFixed(1) + " 时";
        return (ttl / 86400).toFixed(1) + " 天";
      },
      fmtBytes: function (b) {
        if (b == null) return "—";
        if (b < 1024) return b + " B";
        if (b < 1048576) return (b / 1024).toFixed(1) + " KB";
        return (b / 1048576).toFixed(1) + " MB";
      },
      cellText: function (v) {
        if (v == null) return "";
        if (typeof v === "object") return JSON.stringify(v);
        return String(v);
      },
      // 字段详情面板：把值智能格式化——对象/数组→美化 JSON；是 JSON 字符串→美化；否则原样
      formatValue: function (v) {
        if (v == null) return "";
        if (typeof v === "object") {
          return this.fmtJson ? JSON.stringify(v, null, 2) : JSON.stringify(v);
        }
        var s = String(v);
        if (this.fmtJson) {
          var t = s.trim();
          if (t && (t[0] === "{" || t[0] === "[" || t[0] === '"')) {
            try { return JSON.stringify(JSON.parse(t), null, 2); } catch (e) { /* 非 JSON，原样 */ }
          }
        }
        return s;
      },
      selectField: function (r) { this.selField = { k: r.k, v: r.v }; },

      // ---------- 键树右键菜单 ----------
      openCtx: function (e, kind, val) {
        e.preventDefault();
        this.ctx = { show: true, x: e.clientX, y: e.clientY, kind: kind,
                     key: kind === "key" ? val : "", path: kind === "folder" ? val : "" };
      },
      closeCtx: function () { this.ctx.show = false; },
      copyText: function (t) {
        var self = this;
        if (navigator.clipboard) navigator.clipboard.writeText(t).then(function () { self.flash("已复制"); });
        else this.flash("浏览器不支持剪贴板");
        this.closeCtx();
      },
      ctxCopyKey: function () { this.copyText(this.ctx.key); },
      ctxCopyPrefix: function () { this.copyText(this.ctx.path); },
      qkey: function (k) { return /[\s"']/.test(k) ? '"' + k.replace(/"/g, '\\"') + '"' : k; },
      ctxDeleteKey: function () {
        var k = this.ctx.key; this.closeCtx();
        this.execCommand("DEL " + this.qkey(k), false);  // 走写确认旁路（confirm 条）
      },
      ctxDeleteFolder: function () {
        var prefix = this.ctx.path + ":", exact = this.ctx.path;
        // 该目录下所有已加载的键（前缀匹配，含目录名本身作为独立键的情况）
        var names = this.keys.filter(function (x) {
          return x.key.indexOf(prefix) === 0 || x.key === exact;
        }).map(function (x) { return x.key; });
        this.closeCtx();
        if (!names.length) { this.flash("该目录下没有已加载的键"); return; }
        var self = this;
        var cmd = "DEL " + names.map(function (n) { return self.qkey(n); }).join(" ");
        this.execCommand(cmd, false);  // 返回风险确认条，确认后批量删
      },

      // ---------- 连接 / 库 ----------
      loadConnections: function () {
        var self = this;
        return apiGet("/admin/redis/connections").then(function (d) {
          self.conns = (d && d.connections) || [];
          if (!self.conn && self.conns.length) self.setConn(self.conns[0].value);
          else if (self.conn) self.loadDbs();
        });
      },
      setConn: function (val) {
        this.conn = val; this.db = null; this.dbs = []; this.keys = [];
        this.sel = null; this.keyView = null; this.openFolder = {};
        this.persist();
        if (val) this.loadDbs();
      },
      loadDbs: function () {
        var self = this;
        if (!this.conn) return;
        return apiGet("/admin/redis/databases?conn=" + encodeURIComponent(this.conn)).then(function (d) {
          if (!d.ok) { self.flash(d.error); self.dbs = []; return; }
          self.dbs = d.databases || [];
          var valid = self.dbs.some(function (x) { return x.db === self.db; });
          if (self.db != null && valid) self.loadKeys();       // 恢复的库：直接加载键
          else if (self.dbs.length) self.selectDb(self.dbs[0].db);  // 默认选第一个有数据的库
        });
      },
      selectDb: function (db) {
        this.db = db; this.sel = null; this.keyView = null; this.openFolder = {};
        this.persist();
        this.loadKeys();
      },
      loadKeys: function () {
        var self = this;
        if (this.conn == null || this.db == null) return;
        this.keysLoading = true;
        var pat = this.filter.trim() || "*";
        apiGet("/admin/redis/keys?conn=" + encodeURIComponent(this.conn)
               + "&db=" + this.db + "&pattern=" + encodeURIComponent(pat)).then(function (d) {
          self.keysLoading = false;
          if (!d.ok) { self.flash(d.error); self.keys = []; return; }
          self.keys = d.keys || [];
          self.truncated = !!d.truncated;
        }).catch(function () { self.keysLoading = false; });
      },
      refreshAll: function () { this.loadDbs(); this.loadKeys(); },
      toggleFolder: function (path) {
        this.openFolder[path] = !this.openFolder[path];
      },

      // ---------- 键详情 ----------
      viewKey: function (key) {
        var self = this;
        this.sel = key; this.view = "key"; this.keyLoading = true; this.keyView = null;
        this.keyPage = 0; this.selField = null;
        apiGet("/admin/redis/value?conn=" + encodeURIComponent(this.conn)
               + "&db=" + this.db + "&key=" + encodeURIComponent(key)).then(function (d) {
          self.keyLoading = false;
          if (!d.ok) { self.flash(d.error); return; }
          self.keyView = d;
        }).catch(function () { self.keyLoading = false; });
      },
      // hash/zset 展开成行，供表格渲染
      kvRows: function (kv) {
        if (!kv) return [];
        if (kv.type === "hash") return Object.keys(kv.fields || {}).map(function (f) {
          return { k: f, v: kv.fields[f] }; });
        if (kv.type === "zset") return (kv.members || []).map(function (m) {
          return { k: m.member, v: m.score }; });
        if (kv.type === "list") return (kv.items || []).map(function (v, i) {
          return { k: i, v: v }; });
        if (kv.type === "set") return (kv.members || []).map(function (v, i) {
          return { k: i, v: v }; });
        return [];
      },

      // ---------- 命令窗口 ----------
      loadSettings: function () {
        var self = this;
        return apiGet("/admin/settings/get").then(function (d) {
          if (d && d.ok && d.settings) {
            self.theme = d.settings.theme || "dark";
            self.pageSize = d.settings.redis_page_size || 100;
          }
        }).catch(function () { /* 用默认 */ });
      },
      onEditorReady: function () {
        var self = this;
        editor = window.monaco.editor.create(this.$refs.editorEl, {
          value: "", language: "redis", theme: this.theme === "light" ? "vs" : "vs-dark",
          automaticLayout: true,
          fontSize: 13, fontFamily: "'JetBrains Mono', ui-monospace, Menlo, monospace",
          minimap: { enabled: false }, lineNumbers: "on", scrollBeyondLastLine: false,
          renderLineHighlight: "line",
        });
        this.editorReady = true;
        editor.addCommand(window.monaco.KeyMod.CtrlCmd | window.monaco.KeyCode.Enter,
          function () { self.runSelected(); });
        editor.onDidChangeCursorPosition(function () { self.refreshDoc(); });
        // 首个命令模板，便于上手
        editor.setValue("PING\nINFO server\nSCAN 0 COUNT 20");
        this.refreshDoc();
      },
      // 当前要执行的命令：有选区跑选区，否则跑光标所在行
      currentCommand: function () {
        if (!editor) return "";
        var sel = editor.getSelection();
        var model = editor.getModel();
        if (sel && !sel.isEmpty()) return model.getValueInRange(sel).trim();
        var line = editor.getPosition().lineNumber;
        return (model.getLineContent(line) || "").trim();
      },
      runSelected: function () {
        var cmd = this.currentCommand();
        if (!cmd) { this.flash("光标所在行没有命令"); return; }
        this.execCommand(cmd, false);
      },
      execCommand: function (cmd, confirm) {
        var self = this;
        this.running = true; this.cmdErr = null; this.cmdPage = 0;
        if (!confirm) { this.cmdResult = null; this.cmdConfirm = null; }
        this.view = "result";
        apiPost("/admin/redis/run", { conn: this.conn, db: this.db, command: cmd, confirm: confirm ? "1" : "" })
          .then(function (d) {
            self.running = false;
            if (!d.ok) { self.cmdErr = d.error; return; }
            if (d.kind === "confirm") { self.cmdConfirm = { cmd: cmd, risk: d.risk }; return; }
            self.cmdConfirm = null;
            self.cmdResult = { kind: d.kind, command: d.command, value: d.value,
                               duration_ms: d.duration_ms };
            // 写命令后刷新键列表（可能增删键）
            if (d.kind === "write") { self.loadKeys(); self.loadDbs(); }
          }).catch(function (e) { self.running = false; self.cmdErr = String(e); });
      },
      confirmWrite: function () {
        if (this.cmdConfirm) this.execCommand(this.cmdConfirm.cmd, true);
      },
      cancelConfirm: function () { this.cmdConfirm = null; },
      // 结果值渲染成可读结构：数组/对象/标量
      resultRows: function (v) {
        if (Array.isArray(v)) return v.map(function (x, i) { return { k: i, v: x }; });
        if (v && typeof v === "object") return Object.keys(v).map(function (k) { return { k: k, v: v[k] }; });
        return null;
      },

      // ---------- 命令文档面板 ----------
      refreshDoc: function () {
        var self = this;
        var cmd = this.currentCommand() || (editor ? (editor.getModel().getLineContent(
          editor.getPosition().lineNumber) || "") : "");
        var token = (cmd || "").trim().split(/\s+/)[0];
        if (!token) { this.doc = null; this.docCmd = ""; return; }
        var up = token.toUpperCase();
        if (this.docCmd === up && this.doc) return;
        this.docCmd = up;
        apiGet("/admin/redis/command-doc?cmd=" + encodeURIComponent(token)).then(function (d) {
          if (self.docCmd === up) self.doc = d.doc || { unknown: up };
        });
      },

      // ---------- 持久化（轻量：记住选中的连接/库/尺寸） ----------
      persist: function () {
        try {
          localStorage.setItem(STORE_KEY, JSON.stringify({
            conn: this.conn, db: this.db, leftW: this.leftW, docW: this.docW, editorH: this.editorH,
          }));
        } catch (e) { /* ignore */ }
      },
      restore: function () {
        try {
          var s = JSON.parse(localStorage.getItem(STORE_KEY) || "{}");
          if (s.conn) this.conn = s.conn;
          if (s.db != null) this.db = s.db;
          if (s.leftW) this.leftW = s.leftW;
          if (s.docW) this.docW = s.docW;
          if (s.editorH) this.editorH = s.editorH;
        } catch (e) { /* ignore */ }
      },
      beginDrag: function (e, which) {
        var self = this, sx = e.clientX, sy = e.clientY;
        var lw = this.leftW, dw = this.docW, eh = this.editorH;
        function move(ev) {
          if (which === "left") self.leftW = Math.max(200, Math.min(560, lw + ev.clientX - sx));
          else if (which === "doc") self.docW = Math.max(200, Math.min(560, dw - (ev.clientX - sx)));
          else if (which === "editor") self.editorH = Math.max(100, Math.min(500, eh + ev.clientY - sy));
        }
        function up() { document.removeEventListener("mousemove", move);
          document.removeEventListener("mouseup", up); self.persist(); }
        document.addEventListener("mousemove", move);
        document.addEventListener("mouseup", up);
      },
    },
    mounted: function () {
      var self = this;
      this.restore();
      this.loadSettings().then(function () {
        self.loadConnections();
        loadMonaco(function () { self.$nextTick(function () { self.onEditorReady(); }); });
      });
      document.addEventListener("click", function () { self.closeCtx(); });
    },
    template: `
<div class="rd-root" :class="{'env-prod': isProd, 'env-staging': isStaging, 'theme-light': theme==='light'}">
  <aside class="rd-left" :style="{width: leftW + 'px'}">
    <div class="rd-conn">
      <select :value="conn" @change="setConn($event.target.value)">
        <option value="">选择 Redis 连接…</option>
        <option v-for="c in conns" :key="c.value" :value="c.value">{{ c.connection }} · {{ c.environment || 'local' }}</option>
      </select>
    </div>
    <div class="rd-search">
      <input v-model="filter" @keydown.enter="loadKeys" placeholder="键匹配（如 offer:* ，回车扫描）">
      <button class="dg-btn" @click="refreshAll" title="刷新库与键">↻</button>
    </div>
    <div class="rd-tree">
      <div v-if="!conn" class="rd-empty">先选择连接</div>
      <template v-else>
        <div class="rd-sec">键列表 <span v-if="keysLoading">（扫描中…）</span>
          <span v-else>（{{ keys.length }}{{ truncated ? '+，已截断' : '' }}）</span></div>
        <div v-if="db!=null && !keys.length && !keysLoading" class="rd-empty">（无匹配键）</div>
        <template v-for="n in keyNodes" :key="n.kind==='folder'?'f:'+n.path:'k:'+n.key">
          <div v-if="n.kind==='folder'" class="rd-item folder" :style="{paddingLeft: (8 + n.depth*14) + 'px'}"
               @click="toggleFolder(n.path)" @contextmenu="openCtx($event,'folder',n.path)">
            <span class="tw">{{ n.open ? '▾' : '▸' }}</span><span class="ic">▸</span>
            <span class="nm">{{ n.name }}</span><span class="cnt">{{ n.count }}</span>
          </div>
          <div v-else class="rd-item key" :class="{on: sel===n.key}"
               :style="{paddingLeft: (8 + n.depth*14) + 'px'}" @click="viewKey(n.key)"
               @contextmenu="openCtx($event,'key',n.key)">
            <span class="badge" :style="{background: tcolor(n.type)}">{{ n.type.toUpperCase() }}</span>
            <span class="nm" :title="n.key">{{ n.name }}</span>
          </div>
        </template>
      </template>
    </div>
    <div v-if="ctx.show" class="rd-ctx" :style="{left: ctx.x + 'px', top: ctx.y + 'px'}" @click.stop>
      <template v-if="ctx.kind==='key'">
        <div class="mi" @click="ctxCopyKey">复制键名</div>
        <div class="mi danger" @click="ctxDeleteKey">删除键</div>
      </template>
      <template v-else>
        <div class="mi" @click="ctxCopyPrefix">复制前缀</div>
        <div class="mi danger" @click="ctxDeleteFolder">删除该目录下所有键</div>
      </template>
    </div>
    <div class="rd-dbbar" v-if="db!=null">
      <span>数据库</span>
      <select :value="db" @change="selectDb(Number($event.target.value))">
        <option v-for="d in dbs" :key="d.db" :value="d.db">db{{ d.db }}（{{ d.keys }}）</option>
      </select>
    </div>
  </aside>
  <div class="rd-vsplit" @mousedown="beginDrag($event,'left')"></div>

  <section class="rd-main">
    <div v-if="isProd" class="rd-ribbon">⚠ 生产环境 · PROD · 写命令将影响线上数据，请谨慎</div>
    <div v-else-if="isStaging" class="rd-ribbon staging">预发布 · STAGING 环境</div>
    <div class="rd-cmdbar">
      <span class="ttl">命令窗口</span>
      <span class="hint">每行一条命令；选中或光标所在行 ⌘/Ctrl+Enter 执行</span>
      <button class="dg-btn run" :disabled="running || !conn" @click="runSelected">
        {{ running ? '执行中…' : '运行选中命令 ⌘⏎' }}</button>
    </div>
    <div class="rd-editor" :style="{height: editorH + 'px'}">
      <div ref="editorEl" style="position:absolute;inset:0"></div>
      <div v-if="!editorReady" class="rd-loading">编辑器加载中…</div>
    </div>
    <div class="rd-hsplit" @mousedown="beginDrag($event,'editor')"></div>

    <div class="rd-result">
      <div class="rd-tabs">
        <span class="t" :class="{on: view==='result'}" @click="view='result'">执行结果</span>
        <span class="t" :class="{on: view==='key'}" @click="view='key'" v-if="sel">键详情</span>
      </div>
      <div class="rd-rbody">
        <!-- 写命令确认条 -->
        <div v-if="cmdConfirm" class="rd-confirm">
          <div class="hd">确认执行写命令
            <span class="lv">{{ cmdConfirm.risk.level }}</span>
            <code>{{ cmdConfirm.cmd }}</code></div>
          <div class="rs" v-for="r in (cmdConfirm.risk.reasons||[])" :key="r">• {{ r }}</div>
          <div class="acts">
            <button class="dg-btn ok" @click="confirmWrite">确认执行（writer 直接执行并审计）</button>
            <button class="dg-btn" @click="cancelConfirm">取消</button>
          </div>
        </div>

        <!-- 键详情视图 -->
        <template v-if="view==='key'">
          <div v-if="keyLoading" class="rd-empty">加载中…</div>
          <template v-else-if="keyView">
            <div class="rd-keymeta">
              <span class="badge" :style="{background: tcolor(keyView.type)}">{{ keyView.type.toUpperCase() }}</span>
              <b class="kn">{{ keyView.key }}</b>
              <span>TTL：{{ fmtTtl(keyView.ttl) }}</span>
              <span>内存：{{ fmtBytes(keyView.memory_bytes) }}</span>
              <span v-if="keyView.encoding">编码：{{ keyView.encoding }}</span>
              <span v-if="keyView.length!=null">元素：{{ keyView.length }}</span>
            </div>
            <div v-if="keyView.type==='string'" class="rd-strval">{{ keyView.value }}</div>
            <template v-else>
              <div v-if="keyPageCount>1" class="rd-pager">
                <button class="dg-btn" :disabled="keyPage<=0" @click="keyPage--">‹ 上一页</button>
                <span>第 {{ keyPage+1 }} / {{ keyPageCount }} 页 · 共 {{ keyRowsAll.length }} 行</span>
                <button class="dg-btn" :disabled="keyPage>=keyPageCount-1" @click="keyPage++">下一页 ›</button>
              </div>
              <table class="rd-kt">
                <thead><tr>
                  <th>{{ keyView.type==='zset' ? '成员' : (keyView.type==='hash' ? '字段' : '#') }}</th>
                  <th>{{ keyView.type==='zset' ? '分值' : '内容' }}</th></tr></thead>
                <tbody><tr v-for="(r,i) in keyRowsPaged" :key="i"
                           :class="{on: selField && selField.k===r.k}" @click="selectField(r)">
                  <td class="rk">{{ r.k }}</td><td class="rv">{{ cellText(r.v) }}</td></tr></tbody>
              </table>
              <div class="rd-hint2">点某行 → 右侧查看完整内容并格式化 JSON</div>
            </template>
          </template>
          <div v-else class="rd-empty">（点左侧键查看内容）</div>
        </template>

        <!-- 执行结果视图 -->
        <template v-else>
          <div v-if="cmdErr" class="rd-err">⚠ {{ cmdErr }}</div>
          <template v-else-if="cmdResult">
            <div class="rd-resmeta">
              <span class="badge cmd">{{ cmdResult.command }}</span>
              <span v-if="cmdResult.kind==='write'" class="wtag">写 · 已执行</span>
              <span v-if="cmdResult.duration_ms!=null">{{ cmdResult.duration_ms }} ms</span>
            </div>
            <template v-if="cmdRowsAll">
              <div v-if="cmdPageCount>1" class="rd-pager">
                <button class="dg-btn" :disabled="cmdPage<=0" @click="cmdPage--">‹ 上一页</button>
                <span>第 {{ cmdPage+1 }} / {{ cmdPageCount }} 页 · 共 {{ cmdRowsAll.length }} 行</span>
                <button class="dg-btn" :disabled="cmdPage>=cmdPageCount-1" @click="cmdPage++">下一页 ›</button>
              </div>
              <table class="rd-kt">
                <tbody><tr v-for="(r,i) in cmdRowsPaged" :key="i">
                  <td class="rk">{{ r.k }}</td><td class="rv">{{ cellText(r.v) }}</td></tr></tbody>
              </table>
            </template>
            <div v-else class="rd-strval">{{ cellText(cmdResult.value) }}</div>
          </template>
          <div v-else class="rd-empty">（执行命令后在此显示结果）</div>
        </template>
      </div>
    </div>
  </section>

  <div class="rd-vsplit" @mousedown="beginDrag($event,'doc')"></div>
  <aside class="rd-doc" :style="{width: docW + 'px'}">
    <!-- 键详情且选中了某行：字段详情面板（对标 Medis）；否则命令文档 -->
    <template v-if="view==='key' && selField">
      <div class="rd-doc-hd">字段详情</div>
      <div class="rd-field">
        <div class="fl">字段</div>
        <div class="fv name">{{ selField.k }}</div>
        <div class="fl">内容
          <label class="fmt"><input type="checkbox" v-model="fmtJson"> 格式化 JSON</label></div>
        <pre class="fv content">{{ formatValue(selField.v) }}</pre>
      </div>
    </template>
    <template v-else>
      <div class="rd-doc-hd">命令文档</div>
      <div v-if="!doc" class="rd-empty">把光标放到某条命令上，这里显示它的文档</div>
      <div v-else-if="doc.unknown" class="rd-empty">未收录命令：<b>{{ doc.unknown }}</b></div>
      <div v-else class="rd-doc-body">
        <div class="cmd">{{ docCmd }}</div>
        <div class="grp">{{ doc.group }}</div>
        <div class="sum">{{ doc.summary }}</div>
        <div class="syn-hd">语法</div>
        <pre class="syn">{{ doc.syntax }}</pre>
        <a class="link" :href="doc.url" target="_blank" rel="noopener">在 redis.io 查看完整文档 →</a>
      </div>
    </template>
  </aside>

  <div v-if="toast" class="rd-toast">{{ toast }}</div>
</div>`
  });

  app.mount("#dbm-redis");
})();
