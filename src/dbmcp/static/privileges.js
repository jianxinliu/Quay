/*
 * 用户与权限管理（PostgreSQL / MySQL）：账号列表 · 授权明细 · 权限矩阵 · 变更操作。
 *
 * 与查询台同源的做法：独立 Vue 应用，复用 console.css 的 --dg-* 变量与 dg 原语，
 * 数据全走 /admin/privileges/* JSON 接口，**少弹框/一屏**——变更走内联确认条，
 * 不用 confirm()/alert()（浏览器模态会卡死自动化，也不符合本站观感）。
 *
 * 变更路径刻意「两段式」：填参数 → 服务端**构造**语句并回一张确认卡片 → 人看过语句
 * 再点执行。页面从头到尾传不进一条自由 SQL，语句只由 privileges.build 生成。
 */
(function () {
  "use strict";

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
    return fetch(url, {
      method: "POST",
      headers: { "Content-Type": "application/json", Accept: "application/json" },
      body: JSON.stringify(obj || {})
    }).then(parseApi);
  }

  // 每个动作要显示哪些字段。集中在这里，模板只按需渲染，省掉一堆 v-if 组合。
  var ACTIONS = [
    { value: "create_user", label: "新建账号", fields: ["name", "host", "password", "can_login"],
      danger: false },
    { value: "set_password", label: "重置密码", fields: ["name", "host", "password"], danger: false },
    { value: "drop_user", label: "删除账号", fields: ["name", "host"], danger: true },
    { value: "grant", label: "授权 GRANT", fields: ["grantee", "host", "level", "target", "privileges", "with_grant"],
      danger: false },
    { value: "revoke", label: "收回 REVOKE", fields: ["grantee", "host", "level", "target", "privileges"],
      danger: true },
    { value: "default_privileges", label: "默认权限（今后新建的表）",
      fields: ["grantee", "level_fixed_schema", "obj_type", "for_role", "privileges"], danger: false },
    { value: "revoke_default_privileges", label: "取消默认权限",
      fields: ["grantee", "level_fixed_schema", "obj_type", "for_role", "privileges"], danger: true }
  ];

  var PG_LEVELS = [
    { value: "all_tables", label: "schema 下全部现有表" },
    { value: "table", label: "单张表" },
    { value: "schema", label: "schema 本身" },
    { value: "database", label: "整个数据库" }
  ];
  var MYSQL_LEVELS = [
    { value: "database", label: "整个库（db.*）" },
    { value: "table", label: "单张表" },
    { value: "global", label: "全局（*.*）" }
  ];
  // 授权层级 → 取哪一组权限白名单（PG 的两种表级共用一张表）
  var LEVEL_SCOPE = { all_tables: "table", table: "table", schema: "schema",
                      database: "database", global: "global" };

  window.PrivPanel = {
    name: "priv-panel",
    // conn: "project/connection"；database: PG 当前所在的库（查询台左树选的那个）——
    // PG 的账号是**服务器级**的（pg_roles 全局），但授权与权限矩阵是**库级**的，
    // 所以必须跟着当前库走，否则会拿另一个库的 ACL 当成这个库的。
    props: ["conn", "database"],
    emits: ["close"],
    data: function () {
      return {
        connMeta: null, meta: null,
        users: [], userQ: "", sel: null, grants: null,
        tab: "grants",
        schemas: [], schema: "", matrix: null,
        form: { action: "grant", name: "", host: "%", password: "", can_login: true,
                grantee: "", level: "", table: "", database: "", schema: "",
                obj_type: "TABLES", for_role: "", privileges: [], with_grant: false },
        confirm: null, confirmText: "",
        busy: false, err: "", toast: ""
      };
    },
    computed: {
      isProd: function () { return !!(this.connMeta && this.connMeta.environment === "prod"); },
      isPg: function () { return !!(this.meta && this.meta.engine === "postgres"); },
      actionOptions: function () {
        var pg = this.isPg;
        return ACTIONS.filter(function (a) {
          return pg || a.value.indexOf("default_privileges") < 0;
        }).map(function (a) { return { value: a.value, label: a.label }; });
      },
      action: function () {
        var v = this.form.action;
        return ACTIONS.filter(function (a) { return a.value === v; })[0] || ACTIONS[0];
      },
      levelOptions: function () { return this.isPg ? PG_LEVELS : MYSQL_LEVELS; },
      schemaOptions: function () {
        return this.schemas.map(function (s) { return { value: s, label: s }; });
      },
      objTypeOptions: function () {
        return ["TABLES", "SEQUENCES", "FUNCTIONS", "TYPES"].map(function (t) {
          return { value: t, label: t };
        });
      },
      // 当前层级可选的权限项（服务端白名单原样下发，前端只负责渲染）
      privOptions: function () {
        var map = (this.meta && this.meta.privileges) || {};
        var lvl = this.form.action.indexOf("default_privileges") >= 0
          ? ({ TABLES: "table", SEQUENCES: "sequence", SCHEMAS: "schema" }[this.form.obj_type] || "table")
          : (LEVEL_SCOPE[this.form.level] || "table");
        return map[lvl] || [];
      },
      has: function () {
        var f = this.action.fields;
        return function (name) { return f.indexOf(name) >= 0; };
      },
      filteredUsers: function () {
        var q = this.userQ.trim().toLowerCase();
        if (!q) return this.users;
        return this.users.filter(function (u) {
          return (u.name + "@" + (u.host || "")).toLowerCase().indexOf(q) >= 0;
        });
      },
      // 权限矩阵透视：行=表，列=账号，格=该账号在该表上的权限缩写
      matrixView: function () {
        if (!this.matrix) return null;
        var idx = {}, i;
        (this.matrix.columns || []).forEach(function (c, n) { idx[c] = n; });
        var tables = [], byTable = {}, grantees = {}, owners = {};
        (this.matrix.rows || []).forEach(function (r) {
          var t = r[idx["table"]], g = r[idx["grantee"]], p = r[idx["privilege"]];
          var ow = idx["owner"] != null ? r[idx["owner"]] : null;
          if (!byTable[t]) { byTable[t] = {}; tables.push(t); }
          if (ow) owners[t] = ow;
          if (!g) return;                       // relacl 为空 → 只有 owner，没有任何授权
          grantees[g] = true;
          (byTable[t][g] = byTable[t][g] || []).push(p);
        });
        var cols = Object.keys(grantees).sort();
        var rows = tables.map(function (t) {
          return { table: t, owner: owners[t] || "",
                   cells: cols.map(function (g) { return (byTable[t][g] || []).sort(); }) };
        });
        return { grantees: cols, rows: rows, ungranted: rows.filter(function (r) {
          return r.cells.every(function (c) { return !c.length; }); }).length };
      }
    },
    methods: {
      dbQs: function () {
        return this.database ? "&db=" + encodeURIComponent(this.database) : "";
      },
      flash: function (msg) {
        var self = this;
        this.toast = msg;
        setTimeout(function () { if (self.toast === msg) self.toast = ""; }, 2600);
      },
      // 连接由查询台传进来，这里只补一次它的元信息（引擎/环境/有没有 writer）
      loadConnMeta: function () {
        var self = this;
        return apiGet("/admin/privileges/connections").then(function (d) {
          if (!d.ok) { self.err = d.error; return; }
          var hit = (d.connections || []).filter(function (c) { return c.value === self.conn; })[0];
          if (!hit) {
            self.err = "连接 " + self.conn + " 不支持权限管理（目前只支持 PostgreSQL 与 MySQL）。";
            return;
          }
          self.connMeta = hit;
        });
      },
      loadUsers: function () {
        var self = this;
        if (!this.conn) return;
        this.busy = true; this.err = "";
        apiGet("/admin/privileges/users?conn=" + encodeURIComponent(this.conn) + this.dbQs())
          .then(function (d) {
          self.busy = false;
          if (!d.ok) { self.err = d.error; self.users = []; self.meta = null; return; }
          self.meta = { engine: d.engine, role: d.role, environment: d.environment,
                        privileges: d.privileges };
          self.form.level = self.isPg ? "all_tables" : "database";
          var idx = {};
          (d.columns || []).forEach(function (c, n) { idx[c] = n; });
          self.users = (d.rows || []).map(function (r) {
            var u = { name: r[idx["name"]], host: idx["host"] != null ? r[idx["host"]] : "" };
            (d.columns || []).forEach(function (c, n) { u[c] = r[n]; });
            return u;
          });
        });
      },
      loadSchemas: function () {
        var self = this;
        if (!this.conn) return;
        apiGet("/admin/privileges/schemas?conn=" + encodeURIComponent(this.conn)
               + this.dbQs()).then(function (d) {
          if (!d.ok) { self.schemas = []; return; }
          self.schemas = d.databases || [];
          if (!self.schema) {
            self.schema = self.schemas.indexOf("public") >= 0 ? "public" : (self.schemas[0] || "");
          }
        });
      },
      pickUser: function (u) {
        this.sel = u; this.tab = "grants";
        this.form.grantee = u.name;
        this.form.name = u.name;
        if (u.host) this.form.host = u.host;
        this.loadGrants();
      },
      // 单独抽出来：变更执行后要刷新明细，但**不能顺手把人从「权限矩阵」踢回「账号授权」**
      loadGrants: function () {
        var self = this, u = this.sel;
        if (!u) return;
        this.grants = null; this.busy = true;
        apiGet("/admin/privileges/grants?conn=" + encodeURIComponent(this.conn) +
               "&user=" + encodeURIComponent(u.name) +
               "&host=" + encodeURIComponent(u.host || "%") + this.dbQs()).then(function (d) {
          self.busy = false;
          if (!d.ok) { self.err = d.error; return; }
          self.grants = d;
        });
      },
      showMatrix: function () {
        this.tab = "matrix";
        if (!this.matrix) this.loadMatrix();
      },
      loadMatrix: function () {
        var self = this;
        if (!this.conn || !this.schema) return;
        this.busy = true; this.err = "";
        apiGet("/admin/privileges/matrix?conn=" + encodeURIComponent(this.conn) +
               "&schema=" + encodeURIComponent(this.schema) + this.dbQs()).then(function (d) {
          self.busy = false;
          if (!d.ok) { self.err = d.error; self.matrix = null; return; }
          self.matrix = d;
        });
      },
      setSchema: function (v) { this.schema = v; this.loadMatrix(); },
      togglePriv: function (p) {
        var i = this.form.privileges.indexOf(p);
        if (i >= 0) this.form.privileges.splice(i, 1);
        else this.form.privileges.push(p);
      },
      setLevel: function (v) { this.form.level = v; this.form.privileges = []; },
      setObjType: function (v) { this.form.obj_type = v; this.form.privileges = []; },
      setAction: function (v) {
        this.form.action = v; this.confirm = null; this.confirmText = ""; this.err = "";
        this.form.privileges = [];
      },
      // 第一步：把参数发给服务端，让它构造语句并回确认卡片（不执行）
      preview: function () {
        var self = this;
        this.err = ""; this.busy = true; this.confirm = null;
        apiPost("/admin/privileges/run", {
          conn: this.conn, action: this.form.action, params: this.paramsForAction(),
          db: this.database || null
        }).then(function (d) {
          self.busy = false;
          if (!d.ok) { self.err = d.error; return; }
          self.confirm = d; self.confirmText = "";
        });
      },
      // 第二步：带 confirm=1 真执行；prod 还要连接名对上
      execute: function () {
        var self = this;
        if (!this.confirm) return;
        this.busy = true; this.err = "";
        apiPost("/admin/privileges/run", {
          conn: this.conn, action: this.form.action, params: this.paramsForAction(),
          db: this.database || null,
          confirm: "1", confirm_text: this.confirmText,
          expect_fingerprint: this.confirm.fingerprint
        }).then(function (d) {
          self.busy = false;
          if (!d.ok) { self.err = d.error; return; }
          self.confirm = null; self.confirmText = "";
          self.form.password = "";
          self.flash("✓ " + d.summary);
          self.loadUsers();
          self.loadGrants();
          if (self.tab === "matrix") self.loadMatrix();
        });
      },
      paramsForAction: function () {
        var f = this.form;
        return { name: f.name, host: f.host, password: f.password, can_login: f.can_login,
                 grantee: f.grantee, level: f.level, table: f.table,
                 database: f.database, schema: f.schema || this.schema,
                 obj_type: f.obj_type, for_role: f.for_role,
                 privileges: f.privileges, with_grant: f.with_grant };
      },
      // 一键补全「reader 一张表都读不到」的标准修法：USAGE + 全表 SELECT + 默认权限。
      // 三条各自独立走确认，这里只帮忙把表单填好、跳到第一条。
      prefillReadonly: function () {
        this.setAction("grant");
        this.form.level = this.isPg ? "all_tables" : "database";
        this.form.privileges = ["SELECT"];
        this.form.schema = this.schema || "public";
        this.form.database = (this.connMeta && this.connMeta.database) || this.schema || "";
        this.confirm = null;
        this.flash("已填好「只读授权」；PG 记得再做一次 schema USAGE 与默认权限");
      },
      cellText: function (privs) {
        if (!privs.length) return "";
        var SHORT = { SELECT: "S", INSERT: "I", UPDATE: "U", DELETE: "D", TRUNCATE: "T",
                      REFERENCES: "R", TRIGGER: "g" };
        return privs.map(function (p) { return SHORT[p] || p[0]; }).join("");
      },
      cellTitle: function (privs) { return privs.join(", "); },
      onKey: function (e) { if (e.key === "Escape" && !this.confirm) this.$emit("close"); }
    },
    mounted: function () {
      var self = this;
      this.loadConnMeta().then(function () {
        if (self.err) return;
        self.loadUsers();
        self.loadSchemas();
      });
      document.addEventListener("keydown", this.onKey);
    },
    unmounted: function () { document.removeEventListener("keydown", this.onKey); },
    template: `
<div class="pv-modal" @click.self="$emit('close')">
 <div class="pv-root">
  <header class="pv-top">
    <div class="pv-title">用户与权限</div>
    <div class="pv-conn">{{ conn }}</div>
    <div v-if="meta" class="pv-meta">
      <span class="tag">{{ meta.engine }}</span>
      <span class="tag" :class="{warn: !connMeta || !connMeta.has_writer}">
        连接账号 {{ (connMeta && connMeta.admin_user) || "?" }}（{{ meta.role === "writer" ? "writer" : "reader" }}）
      </span>
      <span v-if="isProd" class="tag prod">生产环境</span>
    </div>
    <div class="pv-sp"></div>
    <button class="dg-btn" :disabled="busy" @click="loadUsers()">↻ 刷新</button>
    <button class="pv-x" title="关闭（Esc）" @click="$emit('close')">✕</button>
  </header>

  <div v-if="isProd" class="pv-prodbar">
    ⚠ 生产环境：权限变更会立刻影响线上访问（REVOKE / DROP USER 会当场掐断连接），执行前需输入连接名确认。
  </div>
  <div v-if="err" class="pv-err">⚠ {{ err }}</div>

  <div class="pv-body" v-if="conn">
    <aside class="pv-users">
      <div class="pv-sec">账号 <span class="n">{{ users.length }}</span></div>
      <input class="pv-search" v-model="userQ" placeholder="过滤账号…">
      <ul class="pv-ulist">
        <li v-for="u in filteredUsers" :key="u.name + '@' + (u.host||'')"
            :class="{on: sel && sel.name === u.name && (sel.host||'') === (u.host||'')}"
            @click="pickUser(u)">
          <span class="nm">{{ u.name }}</span>
          <span v-if="u.host" class="hs">@{{ u.host }}</span>
          <span v-if="u.is_superuser" class="fl sup" title="超级用户">super</span>
          <span v-else-if="u.can_login === false" class="fl grp" title="不能登录，通常作为角色组">role</span>
          <span v-if="u.locked === 'Y'" class="fl lock" title="账号已锁定">locked</span>
        </li>
      </ul>
    </aside>

    <section class="pv-main">
      <div class="pv-tabs">
        <button :class="{on: tab === 'grants'}" @click="tab = 'grants'">账号授权</button>
        <button :class="{on: tab === 'matrix'}" @click="showMatrix()">权限矩阵</button>
      </div>

      <div v-if="tab === 'grants'" class="pv-pane">
        <div v-if="!sel" class="pv-empty">从左侧选一个账号，查看它到底有哪些权限。</div>
        <template v-else>
          <div class="pv-who">{{ sel.name }}<span v-if="sel.host">@{{ sel.host }}</span></div>
          <div v-if="busy" class="pv-empty">加载中…</div>
          <template v-else-if="grants">
            <div v-for="g in grants.groups" :key="g.title" class="pv-group">
              <div class="pv-sec">{{ g.title }} <span class="n">{{ g.rows.length }}</span></div>
              <div v-if="!g.rows.length" class="pv-none">（无）</div>
              <div v-else class="pv-tblwrap">
                <table class="pv-tbl">
                  <thead><tr><th v-for="c in g.columns" :key="c">{{ c }}</th></tr></thead>
                  <tbody>
                    <tr v-for="(r, i) in g.rows" :key="i">
                      <td v-for="(v, j) in r" :key="j">{{ v === null ? "—" : v }}</td>
                    </tr>
                  </tbody>
                </table>
              </div>
            </div>
          </template>
        </template>
      </div>

      <div v-else class="pv-pane">
        <div class="pv-row">
          <label>schema / 库</label>
          <dg-select :model-value="schema" :options="schemaOptions" placeholder="选择 schema"
                     @update:modelValue="setSchema"></dg-select>
          <button class="dg-btn" :disabled="busy" @click="loadMatrix()">查询</button>
        </div>
        <div v-if="busy" class="pv-empty">加载中…</div>
        <template v-else-if="matrixView">
          <div v-if="matrixView.ungranted" class="pv-note">
            {{ matrixView.ungranted }} / {{ matrixView.rows.length }} 张表<b>没有给任何账号授权</b>（只有属主能访问）。
            这就是「账号连上了却一张表都读不到」的典型原因。
          </div>
          <div class="pv-tblwrap">
            <table class="pv-tbl pv-matrix">
              <thead>
                <tr>
                  <th class="sticky">表</th><th>属主</th>
                  <th v-for="g in matrixView.grantees" :key="g">{{ g }}</th>
                </tr>
              </thead>
              <tbody>
                <tr v-for="r in matrixView.rows" :key="r.table">
                  <td class="sticky nm">{{ r.table }}</td>
                  <td class="ow">{{ r.owner || "—" }}</td>
                  <td v-for="(c, i) in r.cells" :key="i" class="cell"
                      :class="{yes: c.length}" :title="cellTitle(c)">{{ cellText(c) }}</td>
                </tr>
              </tbody>
            </table>
          </div>
          <div class="pv-legend">S=SELECT · I=INSERT · U=UPDATE · D=DELETE · T=TRUNCATE · R=REFERENCES · g=TRIGGER（悬停看全称）</div>
        </template>
      </div>
    </section>

    <aside class="pv-act">
      <div class="pv-sec">变更</div>
      <div class="pv-row">
        <label>操作</label>
        <dg-select :model-value="form.action" :options="actionOptions"
                   @update:modelValue="setAction"></dg-select>
      </div>

      <div v-if="has('name')" class="pv-row">
        <label>账号名</label><input v-model="form.name" placeholder="如 app_read">
      </div>
      <div v-if="has('grantee')" class="pv-row">
        <label>授予给</label><input v-model="form.grantee" placeholder="账号名">
      </div>
      <div v-if="has('host') && !isPg" class="pv-row">
        <label>host</label><input v-model="form.host" placeholder="%">
      </div>
      <div v-if="has('password')" class="pv-row">
        <label>密码</label><input type="password" v-model="form.password" placeholder="不会写入审计">
      </div>
      <div v-if="has('can_login') && isPg" class="pv-row ck">
        <label><input type="checkbox" v-model="form.can_login"> 可登录（LOGIN）</label>
      </div>

      <div v-if="has('level')" class="pv-row">
        <label>作用范围</label>
        <dg-select :model-value="form.level" :options="levelOptions"
                   @update:modelValue="setLevel"></dg-select>
      </div>
      <div v-if="has('level') && isPg && form.level !== 'database'" class="pv-row">
        <label>schema</label>
        <dg-select :model-value="form.schema || schema" :options="schemaOptions"
                   @update:modelValue="v => form.schema = v"></dg-select>
      </div>
      <div v-if="has('level') && !isPg && form.level !== 'global'" class="pv-row">
        <label>库</label><input v-model="form.database" placeholder="库名">
      </div>
      <div v-if="has('level') && form.level === 'table'" class="pv-row">
        <label>表</label><input v-model="form.table" placeholder="表名">
      </div>
      <div v-if="has('level') && isPg && form.level === 'database'" class="pv-row">
        <label>数据库</label><input v-model="form.database" placeholder="库名">
      </div>

      <div v-if="has('level_fixed_schema')" class="pv-row">
        <label>schema</label>
        <dg-select :model-value="form.schema || schema" :options="schemaOptions"
                   @update:modelValue="v => form.schema = v"></dg-select>
      </div>
      <div v-if="has('obj_type')" class="pv-row">
        <label>对象类型</label>
        <dg-select :model-value="form.obj_type" :options="objTypeOptions"
                   @update:modelValue="setObjType"></dg-select>
      </div>
      <div v-if="has('for_role')" class="pv-row">
        <label>建表者</label><input v-model="form.for_role" placeholder="留空=当前连接账号">
      </div>

      <div v-if="has('privileges')" class="pv-row col">
        <label>权限</label>
        <div class="pv-privs">
          <label v-for="p in privOptions" :key="p" class="pv-chip"
                 :class="{on: form.privileges.indexOf(p) >= 0}">
            <input type="checkbox" :checked="form.privileges.indexOf(p) >= 0"
                   @change="togglePriv(p)"> {{ p }}
          </label>
        </div>
      </div>
      <div v-if="has('with_grant')" class="pv-row ck">
        <label><input type="checkbox" v-model="form.with_grant"> 允许再转授（WITH GRANT OPTION）</label>
      </div>

      <div class="pv-btns">
        <button class="dg-btn" :disabled="busy || !conn" @click="preview()">生成语句</button>
        <button class="dg-btn ghost" :disabled="busy" @click="prefillReadonly()"
                title="填好「给某账号只读权限」的常用参数">只读授权模板</button>
      </div>

      <div v-if="confirm" class="pv-confirm" :class="{danger: action.danger}">
        <div class="hd">{{ confirm.summary }}</div>
        <pre class="sql">{{ confirm.sql }}</pre>
        <div v-if="confirm.has_secret" class="tip">语句含密码；审计与此处显示的都是脱敏后的版本。</div>
        <div v-if="confirm.prod" class="gate">
          <label>生产环境确认：输入连接名 <b>{{ confirm.expect_text }}</b></label>
          <input v-model="confirmText" :placeholder="confirm.expect_text">
        </div>
        <div class="pv-btns">
          <button class="dg-btn danger" :disabled="busy || (confirm.prod && confirmText !== confirm.expect_text)"
                  @click="execute()">确认执行</button>
          <button class="dg-btn ghost" :disabled="busy" @click="confirm = null">取消</button>
        </div>
      </div>
    </aside>
  </div>

  <div v-if="toast" class="pv-toast">{{ toast }}</div>
 </div>
</div>`
  };
})();
