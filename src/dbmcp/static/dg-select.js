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
    props: ["modelValue", "options", "placeholder"],  // options: [{value, label, env?, ic?}]
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
      '<div class="dg-sel">'
      + '<button type="button" class="dg-sel-btn" @click.stop="toggle" :title="label">'
      + '<img v-if="selIc" class="dg-eng" :src="selIc.src" :title="selIc.label" alt="">'
      + '<span v-if="selEnv" class="dg-env" :style="{background: envColor(selEnv)}">{{ selEnv }}</span>'
      + '<span class="lb">{{ label }}</span><span class="ar">▾</span></button>'
      + '<div v-if="open" class="dg-sel-pop" @click.stop>'
      +   '<input v-if="options.length > 8" ref="qEl" v-model="q" class="dg-sel-q" placeholder="筛选…">'
      +   '<div class="dg-sel-list">'
      +     '<div v-for="o in filtered" :key="o.value" class="dg-sel-item"'
      +       ' :class="{cur: o.value === modelValue}" @click="pick(o.value)">'
      +       '<img v-if="o.ic" class="dg-eng" :src="o.ic.src" :title="o.ic.label" alt="">'
      +       '<span v-if="o.env" class="dg-env" :style="{background: envColor(o.env)}">{{ o.env }}</span>{{ o.label }}</div>'
      +     '<div v-if="!filtered.length" class="dg-sel-none">（无匹配）</div>'
      +   '</div>'
      + '</div>'
      + '</div>'
  };

  window.ENV_COLORS = ENV_COLORS;
  window.DgSelect = DgSelect;
})();
