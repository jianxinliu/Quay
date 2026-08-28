/* 共享自绘下拉组件（原生 <select> 弹出列表无法样式化，深色 UI 里很扎眼）。
 *
 * 查询台 console.js / Redis 控制台 / 流程页 workflows.js 共用。挂到 window.DgSelect
 * 与 window.ENV_COLORS，业务页启动时 app.component("dg-select", window.DgSelect)。
 * 支持筛选（选项 > 8 显示搜索框）、环境色徽章、引擎图标。
 */
(function () {
  "use strict";
  var ENV_COLORS = { local: "#64748b", dev: "#2563eb", staging: "#d97706", prod: "#dc2626" };

  var DgSelect = {
    name: "dg-select",
    // drop: 弹出方向。"down"（默认）｜"up"。放在容器底部的选择器（如 Redis 的库切换器）
    // 必须给 "up"，否则弹层会向下冲出面板、被裁掉半截。
    props: ["modelValue", "options", "placeholder", "drop"],  // options: [{value, label, env?, ic?}]
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
      selIc: function () {
        var v = this.modelValue;
        for (var i = 0; i < this.options.length; i++)
          if (this.options[i].value === v) return this.options[i].ic || null;
        return null;
      },
      envColor: function () { return function (e) { return ENV_COLORS[e] || "#64748b"; }; },
      filtered: function () {
        var q = this.q.trim().toLowerCase();
        return q ? this.options.filter(function (o) { return o.label.toLowerCase().indexOf(q) >= 0; })
                 : this.options;
      },
      // 选项带 group 字段时插入不可点的分组标题（替代原生 <optgroup>）。
      // 筛选后空掉的分组自然不会出现——标题只在其下确实有项时才插。
      rows: function () {
        var out = [], last = null;
        this.filtered.forEach(function (o) {
          var g = o.group || null;
          if (g && g !== last) out.push({ group: g });
          last = g;
          out.push({ o: o });
        });
        return out;
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
      onDocClick: function (e) { if (!this.$el.contains(e.target)) this.open = false; }
    },
    mounted: function () { document.addEventListener("click", this.onDocClick); },
    unmounted: function () { document.removeEventListener("click", this.onDocClick); },
    template:
      '<div class="dg-sel" :class="{up: drop === \'up\'}">'
      + '<button type="button" class="dg-sel-btn" @click.stop="toggle" :title="label">'
      + '<img v-if="selIc" class="dg-eng" :src="selIc.src" :title="selIc.label" alt="">'
      + '<span v-if="selEnv" class="dg-env" :style="{background: envColor(selEnv)}">{{ selEnv }}</span>'
      + '<span class="lb">{{ label }}</span><span class="ar">{{ drop === \'up\' ? "▴" : "▾" }}</span></button>'
      + '<div v-if="open" class="dg-sel-pop" @click.stop>'
      +   '<input v-if="options.length > 8" ref="qEl" v-model="q" class="dg-sel-q" placeholder="筛选…">'
      +   '<div class="dg-sel-list">'
      +     '<template v-for="(r, i) in rows" :key="i">'
      +       '<div v-if="r.group" class="dg-sel-group">{{ r.group }}</div>'
      +       '<div v-else class="dg-sel-item" :class="{cur: r.o.value === modelValue}" @click="pick(r.o.value)">'
      +         '<img v-if="r.o.ic" class="dg-eng" :src="r.o.ic.src" :title="r.o.ic.label" alt="">'
      +         '<span v-if="r.o.env" class="dg-env" :style="{background: envColor(r.o.env)}">{{ r.o.env }}</span>{{ r.o.label }}</div>'
      +     '</template>'
      +     '<div v-if="!filtered.length" class="dg-sel-none">（无匹配）</div>'
      +   '</div>'
      + '</div>'
      + '</div>'
  };

  window.ENV_COLORS = ENV_COLORS;
  window.DgSelect = DgSelect;
})();
