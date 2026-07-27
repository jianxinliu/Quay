/* 流程独立页：Vue 3 无构建 + SPA 路由（列表页 / 详情页由 URL hash 判断）。
 *
 * URL：
 *   /admin/workflows                → 列表页
 *   /admin/workflows#name=xxx       → 详情页（编辑指定 workflow）
 *   /admin/workflows#new            → 新建（弹小卡片）
 *
 * 蓝图视图（DAG 画布）与模块视图（拓扑序卡片链）后续 task 加入；本文件先建骨架。
 * DgSelect / ENV_COLORS 从 window 引用（dg-select.js 先加载）。
 */
(function () {
  "use strict";
  var API = "/admin/workflows";

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
    var h = (location.hash || "").replace(/^#/, "");
    if (!h) return { view: "list" };
    if (h === "new") return { view: "new" };
    var m = /^name=(.+)$/.exec(h);
    if (m) return { view: "detail", name: decodeURIComponent(m[1]) };
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

  var App = {
    data: function () {
      return {
        route: parseHash(),
        wfs: [],
        wsList: [],
        loading: false,
        err: "",
        // 新建对话框
        newForm: { name: "", workspace: "", newWorkspace: "" },
        // 详情页当前 workflow
        current: null,
        // 详情页视图切换：blueprint | modules
        viewMode: "blueprint",
        // 运行输出
        runOut: null,
        runBusy: false
      };
    },
    computed: {
      workspaceOptions: function () {
        var opts = (this.wsList || []).map(function (w) {
          return { value: w.workspace, label: w.workspace + (w.datasets ? " (" + w.datasets.length + " 数据集)" : "") };
        });
        opts.push({ value: "__new__", label: "＋ 新建工作区…" });
        return opts;
      }
    },
    methods: {
      // ---------- 数据加载 ----------
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
      onHashChange: function () {
        this.route = parseHash();
        if (this.route.view === "detail") this.loadOne(this.route.name);
        if (this.route.view === "new") this.refreshWorkspaces();
      },
      loadOne: function (name) {
        var self = this;
        self.current = null;
        apiGet(API + "/list").then(function (d) {
          if (!d || !d.ok) { self.err = (d && d.error) || "加载失败"; return; }
          var hit = (d.workflows || []).filter(function (w) { return w.name === name; })[0];
          if (!hit) { self.err = "workflow 不存在：" + name; return; }
          self.current = hit;
          self.viewMode = "blueprint";
          self.runOut = null;
        });
      },
      // ---------- 列表页动作 ----------
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
      // ---------- 新建 ----------
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
        // 空 graph 骨架，等到详情页里加节点
        var graph = { nodes: [], edges: [] };
        var self = this;
        apiPost(API + "/save", {
          name: name, workspace: ws, script: "", graph: JSON.stringify(graph)
        }).then(function (d) {
          if (!d.ok) { alert("创建失败：" + (d.error || "")); return; }
          location.hash = "#name=" + encodeURIComponent(name);
          self.refresh();
        });
      },
      // ---------- 详情页动作（骨架，具体蓝图/模块视图后续 task 补） ----------
      runCurrent: function () {
        if (!this.current) return;
        var self = this;
        self.runBusy = true;
        self.runOut = null;
        // 调用现有异步 job 接口，轮询结果
        apiPost(API + "/run", { name: self.current.name }).then(function (d) {
          if (!d.ok) { self.runBusy = false; alert("启动失败：" + (d.error || "")); return; }
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
            self.runOut = d;
          });
        }, 500);
      }
    },
    mounted: function () {
      var self = this;
      window.addEventListener("hashchange", this.onHashChange);
      this.refresh();
      this.refreshWorkspaces();
      if (this.route.view === "detail") this.loadOne(this.route.name);
    },
    unmounted: function () {
      window.removeEventListener("hashchange", this.onHashChange);
    },
    template:
      '<div class="wf-root">'
      // ============ 列表页 ============
      + '<div v-if="route.view===\'list\'" class="wf-list">'
      +   '<div class="wf-list-hd">'
      +     '<h2>流程</h2>'
      +     '<button class="dg-btn run" @click="openNew">＋ 新建流程</button>'
      +   '</div>'
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
      // ============ 详情页（骨架，蓝图/模块留后续 task 补） ============
      + '<div v-else-if="route.view===\'detail\'" class="wf-detail">'
      +   '<div class="wf-detail-hd">'
      +     '<a href="#" @click.prevent="backToList">← 返回列表</a>'
      +     '<b v-if="current">{{ current.name }}</b>'
      +     '<span v-if="current" class="wf-ws">{{ current.workspace }}</span>'
      +     '<div class="wf-tabs">'
      +       '<button :class="{active: viewMode===\'blueprint\'}" @click="viewMode=\'blueprint\'">蓝图</button>'
      +       '<button :class="{active: viewMode===\'modules\'}" @click="viewMode=\'modules\'">模块</button>'
      +     '</div>'
      +     '<button class="dg-btn run" @click="runCurrent" :disabled="runBusy">'
      +       '{{ runBusy ? "运行中…" : "▶ 运行" }}</button>'
      +   '</div>'
      +   '<div v-if="!current" class="wf-empty">加载中…</div>'
      +   '<div v-else class="wf-detail-body">'
      +     '<div v-if="viewMode===\'blueprint\'" class="wf-blueprint">'
      +       '<div class="wf-placeholder">蓝图视图（DAG 画布）— 下一 task 会从查询台搬过来</div>'
      +     '</div>'
      +     '<div v-else class="wf-modules">'
      +       '<div class="wf-placeholder">模块视图（拓扑序卡片链 + schema 感知）— 下一 task 会实现</div>'
      +     '</div>'
      +     '<div v-if="runOut" class="wf-run-out">'
      +       '<h3>运行结果</h3>'
      +       '<pre>{{ JSON.stringify(runOut.result || runOut, null, 2).slice(0, 2000) }}</pre>'
      +     '</div>'
      +   '</div>'
      + '</div>'
      + '</div>'
  };
  // fmtTime 是模块级纯函数，template 里需通过 methods 暴露（Vue3 单文件里只认 methods/computed/data）
  App.methods.fmtTime = fmtTime;

  window.addEventListener("DOMContentLoaded", function () {
    var app = window.Vue.createApp(App);
    if (window.DgSelect) app.component("dg-select", window.DgSelect);
    app.mount("#wf-app");
  });
})();
