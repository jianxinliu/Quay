// 流程页图表：结果集 → ECharts option + WfChart 组件（从 workflows.js 拆出，无外部依赖）。挂到 window.DbmWfChart。
(function () {
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
  window.DbmWfChart = { CHART_PALETTE: CHART_PALETTE, chartRowsFromResult: chartRowsFromResult, multiRowsFromResult: multiRowsFromResult, histBins: histBins, chartOptionFor: chartOptionFor, _emptyOpt: _emptyOpt, defaultChartCfg: defaultChartCfg, WfChart: WfChart };
})();
