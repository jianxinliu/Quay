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
    props: ["graph", "conn", "aiEnabled"],
    emits: ["update:graph", "run"],
    data: function () {
      return {
        sel: null,            // 选中的节点 id
        linkDraft: null,      // 拉线中 {from, x, y}
        nodeStatus: {},       // {nodeId: 'running'|'ok'|'err'}
        connOptions: [],
        realConnOptions: []
      };
    },
    computed: {
      selNode: function () {
        var self = this;
        return (this.graph.nodes || []).find(function (n) { return n.id === self.sel; }) || null;
      },
      joinKindOptions: function () {
        return [
          { value: "INNER", label: "INNER" },
          { value: "LEFT", label: "LEFT" },
          { value: "RIGHT", label: "RIGHT" },
          { value: "FULL", label: "FULL" }
        ];
      }
    },
    methods: {
      persist: function () { this.$emit("update:graph", this.graph); },
      typeLabel: function (t) {
        return { source: "取数", file: "文件", filter: "过滤", join: "JOIN",
                 aggregate: "聚合", sql: "SQL", output: "输出" }[t] || t;
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
        return "";
      },
      addNode: function (type) {
        var prefix = { source: "src", file: "file", filter: "flt", join: "join",
                       aggregate: "agg", sql: "sql", output: "out" }[type] || "n";
        var i = 1;
        var names = {};
        (this.graph.nodes || []).forEach(function (n) { names[n.name] = 1; });
        while (names[prefix + i]) i++;
        var id = "n" + Date.now().toString(36) + Math.floor(Math.random() * 1e4).toString(36);
        var cfg = type === "join" ? { kind: "INNER", on: "", select: "", ports_n: 2 }
                : type === "aggregate" ? { group: "", aggs: "" }
                : type === "source" ? { conn: "", sql: "", limit: null }
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
        delete this.nodeStatus[id];
        this.persist();
      },
      addJoinPort: function () {
        var n = this.selNode;
        if (!n || n.type !== "join") return;
        var cur = joinPortsN(n);
        if (cur >= JOIN_MAX_PORTS) return;
        n.cfg.ports_n = cur + 1;
        this.persist();
      },
      delJoinPort: function () {
        var n = this.selNode;
        if (!n || n.type !== "join") return;
        var cur = joinPortsN(n);
        if (cur <= 2) return;
        n.cfg.ports_n = cur - 1;
        // 断开超出的端口连线
        var removed = "in_" + (cur);
        this.graph.edges = (this.graph.edges || []).filter(function (e) {
          return !(e.to === n.id && (e.port || "in") === removed);
        });
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
      loadConnOptions: function () {
        var self = this;
        apiGet("/admin/sql/connections").then(function (d) {
          if (!d || !d.ok) return;
          self.connOptions = (d.connections || []).map(function (c) {
            return { value: c.project + "/" + c.connection, label: c.project + "/" + c.connection,
                     env: c.environment };
          });
          // 真实连接（不含 analysis 沙箱）
          self.realConnOptions = self.connOptions.filter(function (o) {
            return o.value.indexOf("analysis/") !== 0;
          });
        });
      }
    },
    mounted: function () { this.loadConnOptions(); },
    template:
      '<div class="wf-blueprint">'
      // 顶栏
      + '<div class="wf-bp-bar">'
      +   '<button class="dg-btn" @click="addNode(\'source\')" title="从任意连接取数为数据集">＋取数</button>'
      +   '<button class="dg-btn" @click="addNode(\'file\')" title="导入本地 CSV/Parquet/JSON">＋文件</button>'
      +   '<button class="dg-btn" @click="addNode(\'filter\')">＋过滤</button>'
      +   '<button class="dg-btn" @click="addNode(\'join\')">＋JOIN</button>'
      +   '<button class="dg-btn" @click="addNode(\'aggregate\')">＋聚合</button>'
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
      +       ' :class="[n.type, {sel: sel===n.id}, nodeStatus[n.id] || \'\']"'
      +       ' :style="{left: n.x + \'px\', top: n.y + \'px\'}"'
      +       ' @mousedown="nodeDown($event, n)">'
      +       '<div class="hd"><span class="ty">{{ typeLabel(n.type) }}</span>'
      +         '<span class="nm">{{ n.name }}</span>'
      +         '<span class="st">{{ nodeStatus[n.id]===\'ok\' ? \'✓\' : nodeStatus[n.id]===\'err\' ? \'✗\' : nodeStatus[n.id]===\'running\' ? \'⟳\' : \'\' }}</span>'
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
      +       '用上方按钮添加节点：取数 → 过滤 / JOIN / 聚合 → 输出，拖节点右缘圆点到下一节点左缘完成连线。</div>'
      +   '</div>'
      +   '<div class="wf-bp-cfg" v-if="selNode">'
      +     '<div class="cfg-hd">{{ typeLabel(selNode.type) }} 节点</div>'
      +     '<div class="row"><label>名字</label><input v-model="selNode.name" @change="persist" spellcheck="false"></div>'
      +     '<template v-if="selNode.type===\'source\'">'
      +       '<div class="row"><label>连接</label><dg-select :model-value="selNode.cfg.conn" :options="realConnOptions" '
      +         'placeholder="选择连接…" @update:model-value="v => { selNode.cfg.conn = v; persist(); }"/></div>'
      +       '<div class="row"><label>取数 SQL</label><textarea v-model="selNode.cfg.sql" rows="5" @change="persist" '
      +         'spellcheck="false" placeholder="SELECT * FROM t WHERE ..."></textarea></div>'
      +       '<div class="row"><label>行数上限</label><input type="number" v-model.number="selNode.cfg.limit" @change="persist" placeholder="默认 20 万"></div>'
      +       '<div class="row"><label>schema</label><input v-model="selNode.cfg.schema" @change="persist" placeholder="未绑库连接需指定"></div>'
      +     '</template>'
      +     '<template v-else-if="selNode.type===\'file\'">'
      +       '<div class="row"><label>文件路径</label><input v-model="selNode.cfg.path" @change="persist" placeholder="/path/data.csv（csv/parquet/json）"></div>'
      +     '</template>'
      +     '<template v-else-if="selNode.type===\'filter\'">'
      +       '<div class="row"><label>WHERE</label><textarea v-model="selNode.cfg.where" rows="4" @change="persist" '
      +         'spellcheck="false" placeholder="status = \'paid\' AND amount > 100"></textarea></div>'
      +     '</template>'
      +     '<template v-else-if="selNode.type===\'join\'">'
      +       '<div class="row"><label>类型</label><dg-select :model-value="selNode.cfg.kind || \'INNER\'" :options="joinKindOptions" '
      +         '@update:model-value="v => { selNode.cfg.kind = v; persist(); }"/></div>'
      +       '<div class="row"><label>输入端口数 <span class="wf-hint">{{ joinPortsCount(selNode) }}/{{ maxPorts }}</span></label>'
      +         '<div class="wf-btn-grp">'
      +           '<button class="dg-btn sm" @click="delJoinPort" :disabled="joinPortsCount(selNode) <= 2">−</button>'
      +           '<span class="wf-port-cnt">{{ joinPortsCount(selNode) }}</span>'
      +           '<button class="dg-btn sm" @click="addJoinPort" :disabled="joinPortsCount(selNode) >= maxPorts">＋</button>'
      +         '</div>'
      +       '</div>'
      +       '<div class="row"><label>ON</label><input v-model="selNode.cfg.on" @change="persist" spellcheck="false" '
      +         ':placeholder="joinOnHint(selNode)"></div>'
      +       '<div class="row"><label>SELECT</label><input v-model="selNode.cfg.select" @change="persist" spellcheck="false" '
      +         ':placeholder="joinSelectHint(selNode)"></div>'
      +     '</template>'
      +     '<template v-else-if="selNode.type===\'aggregate\'">'
      +       '<div class="row"><label>GROUP BY</label><input v-model="selNode.cfg.group" @change="persist" spellcheck="false" placeholder="channel（留空 = 全局聚合）"></div>'
      +       '<div class="row"><label>聚合表达式</label><textarea v-model="selNode.cfg.aggs" rows="3" @change="persist" '
      +         'spellcheck="false" placeholder="count(*) AS n, sum(amount) AS total"></textarea></div>'
      +     '</template>'
      +     '<template v-else-if="selNode.type===\'sql\'">'
      +       '<div class="row"><label>SQL</label><textarea v-model="selNode.cfg.sql" rows="8" @change="persist" '
      +         'spellcheck="false" placeholder="SELECT ...（直接用上游节点名作表名）"></textarea></div>'
      +     '</template>'
      +     '<template v-else-if="selNode.type===\'output\'">'
      +       '<div class="row"><label>ORDER BY</label><input v-model="selNode.cfg.order_by" @change="persist" spellcheck="false" placeholder="total DESC"></div>'
      +       '<div class="row"><label>LIMIT</label><input type="number" v-model.number="selNode.cfg.limit" @change="persist" placeholder="1000"></div>'
      +     '</template>'
      +     '<div class="row acts">'
      +       '<button class="dg-btn danger" @click="delNode(selNode.id)">删除节点</button>'
      +     '</div>'
      +   '</div>'
      + '</div>'
      + '</div>'
  };
  // 加两个辅助 method 到 Blueprint（template 用）
  Blueprint.computed.maxPorts = function () { return JOIN_MAX_PORTS; };
  Blueprint.methods.joinPortsCount = function (n) { return joinPortsN(n); };
  Blueprint.methods.joinOnHint = function (n) {
    var pn = joinPortsN(n);
    if (pn === 2) return "a.uid = b.id（in_1=a, in_2=b）";
    var parts = [];
    for (var i = 0; i < pn - 1; i++) {
      parts.push("abcdefghijklmnop".charAt(i) + ".x = " + "abcdefghijklmnop".charAt(i + 1) + ".y");
    }
    return parts.join(" AND ") + "（" + pn + " 路：" + "abcdefghijklmnop".substr(0, pn).split("").join("/") + "）";
  };
  Blueprint.methods.joinSelectHint = function (n) {
    var pn = joinPortsN(n);
    var arr = [];
    for (var i = 0; i < pn; i++) arr.push("abcdefghijklmnop".charAt(i) + ".*");
    return arr.join(", ") + "（留空即为此默认）";
  };
  Blueprint.methods.portY = portY;

  // ---------- 主 App ----------
  var App = {
    components: { "wf-blueprint": Blueprint },
    data: function () {
      return {
        route: parseHash(),
        wfs: [],
        wsList: [],
        loading: false,
        err: "",
        newForm: { name: "", workspace: "", newWorkspace: "" },
        current: null,        // {name, workspace, graph, ...}
        viewMode: "blueprint",
        runOut: null,
        runBusy: false,
        runErr: "",
        saveTimer: null
      };
    },
    computed: {
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
      onHashChange: function () {
        this.route = parseHash();
        this.runOut = null; this.runErr = "";
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
        var graph = { nodes: [], edges: [] };
        apiPost(API + "/save", {
          name: name, workspace: ws, script: "", graph: JSON.stringify(graph)
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
        // 先落盘（防未保存修改），再跑
        this.save().then(function () {
          return apiPost(API + "/run", { name: self.current.name });
        }).then(function (d) {
          if (!d.ok) { self.runBusy = false; self.runErr = d.error || "启动失败"; return; }
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
            if (d.status === "done") { self.runOut = d.result || null; }
            else { self.runErr = d.error || "运行失败"; }
          });
        }, 500);
      }
    },
    mounted: function () {
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
      // ============ 详情页 ============
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
      +     '<wf-blueprint v-if="viewMode===\'blueprint\'" :graph="current.graph" :conn="\'analysis/\' + current.workspace" '
      +       '@update:graph="onGraphUpdate" @run="runCurrent"/>'
      +     '<div v-else class="wf-modules">'
      +       '<div class="wf-placeholder">模块视图（拓扑序卡片链 + schema 感知）— 下一 task 会实现</div>'
      +     '</div>'
      +     '<div v-if="runErr" class="wf-run-out"><h3>运行失败</h3><pre>{{ runErr }}</pre></div>'
      +     '<div v-if="runOut" class="wf-run-out">'
      +       '<h3>运行结果</h3>'
      +       '<pre>{{ JSON.stringify(runOut, null, 2).slice(0, 2000) }}</pre>'
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
