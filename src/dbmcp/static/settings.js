/* 系统设置页的三件交互：搜索过滤、开关、改动条。原生 JS，无框架。

   核心是「脏值追踪」：进页面时记下每个字段的初始值，之后任何输入都与它比对——
   改动条上的「N 项改动」和「放弃」都由这一份初始快照驱动，保存成功后重设快照。 */
(function () {
  const page = document.querySelector(".set-page");
  if (!page) return;

  const form = page.querySelector("form.settings-form");
  const bar = page.querySelector(".set-bar");

  /* ---------------- 脏值追踪 ----------------
     每个可改的东西抽象成一个 {read, write} 存取器，而不是直接拿 DOM 元素比：
     - 单选组要按**组**算，读的是「这组选中的是哪个值」。按元素算的话，切一次渠道
       会被数成两处改动（一个取消选中、一个选中），而且 radio 的 .value 从来不变、
       根本检测不到改动。
     - 复选框看 .checked，其余看 .value。
     - 密码框留空 = 不改：不能拿空值去跟「已存储」的状态比。 */
  const acc = [];
  if (form) {
    const seenRadio = new Set();
    [...form.elements].forEach((el) => {
      if (!el.name) return;
      // 「放弃」是程序改值，而程序赋值不会自己触发事件——但页面上好几处联动都挂在
      // change/input 上（通知渠道字段的显隐、AI 后端的显隐、数字的换算读数）。
      // 所以每个 write 都补发一次事件，否则放弃之后值回去了、界面还停在原样。
      const fire = (target) => {
        target.dispatchEvent(new Event("input", { bubbles: true }));
        target.dispatchEvent(new Event("change", { bubbles: true }));
      };
      if (el.type === "radio") {
        if (seenRadio.has(el.name)) return;
        seenRadio.add(el.name);
        const name = el.name;
        const group = form.elements[name];
        acc.push({
          read: () => group.value,
          write: (v) => {
            group.value = v;
            const on = form.querySelector(`input[name="${name}"]:checked`);
            if (on) fire(on);
          },
        });
      } else if (el.type === "checkbox") {
        acc.push({ read: () => el.checked, write: (v) => { el.checked = v; fire(el); } });
      } else {
        acc.push({
          read: () => el.value, write: (v) => { el.value = v; fire(el); },
          skipWhenBlank: el.type === "password",
        });
      }
    });
  }
  let baseline = snapshot();

  function snapshot() {
    return acc.map((a) => a.read());
  }
  function changedCount() {
    return acc.reduce((n, a, i) => {
      if (a.skipWhenBlank && a.read() === "") return n;
      return n + (a.read() !== baseline[i] ? 1 : 0);
    }, 0);
  }
  function dirtyCheck() {
    if (!bar) return;
    const n = changedCount();
    bar.classList.toggle("on", n > 0);
    const label = bar.querySelector(".n");
    if (label) label.innerHTML = `<b>${n}</b> 项改动未保存`;
    const msg = bar.querySelector(".msg");
    if (msg && n > 0) msg.textContent = "";
  }
  if (form) {
    form.addEventListener("input", dirtyCheck);
    form.addEventListener("change", dirtyCheck);
  }

  /* ---------------- 开关 ----------------
     值由隐藏 input 承载、复选框本身不带 name：未勾选的复选框根本不会进 FormData，
     而保存接口要的是显式的 "true"/"false"。 */
  page.querySelectorAll(".sw input[type=checkbox]").forEach((box) => {
    const hidden = document.getElementsByName(box.dataset.field)[0];
    const state = box.closest(".sw").querySelector(".state");
    const sync = () => {
      if (hidden) hidden.value = box.checked ? "true" : "false";
      if (state) state.textContent = box.checked ? box.dataset.on : box.dataset.off;
      dirtyCheck();
    };
    box.addEventListener("change", sync);
    sync();
  });

  /* ---------------- 读数换算 ----------------
     400000 → ≈114k token、67108864 → 64 MB。让人自己算是设置页最常见的失礼，
     而且改完立刻要看到新读数，不能等保存后刷新才知道自己填了多大。 */
  const CHARS_PER_TOKEN = 3.5;
  function humanBytes(n) {
    n = Number(n);
    if (!isFinite(n) || n <= 0) return "";
    const units = ["B", "KB", "MB", "GB", "TB"];
    let i = 0;
    while (n >= 1024 && i < units.length - 1) { n /= 1024; i++; }
    return (i === 0 ? n.toFixed(0) : n.toFixed(n < 10 ? 1 : 0)) + " " + units[i];
  }
  function readout(input) {
    const el = input.closest(".ctl")?.querySelector(".read");
    if (!el || !el.dataset.kind) return;
    const v = Number(input.value);
    if (el.dataset.kind === "bytes") {
      el.textContent = v > 0 ? "= " + humanBytes(v) : "";
    } else if (el.dataset.kind === "tokens") {
      el.textContent = v > 0 ? "≈ " + Math.round(v / CHARS_PER_TOKEN / 1000) + "k token"
        : (Number(input.value) === 0 ? "不限制" : "");
    }
  }
  page.querySelectorAll(".ctl input[type=number]").forEach((i) => {
    readout(i);
    i.addEventListener("input", () => readout(i));
  });

  bar?.querySelector(".reset")?.addEventListener("click", () => {
    acc.forEach((a, i) => a.write(baseline[i]));
    dirtyCheck();
  });

  bar?.querySelector(".save")?.addEventListener("click", () => form?.requestSubmit());

  /* 保存：接管提交，成功后把当前值设为新基线（改动条自然收起） */
  form?.addEventListener("submit", async (e) => {
    e.preventDefault();
    const msg = bar?.querySelector(".msg");
    const save = bar?.querySelector(".save");
    if (msg) msg.textContent = "保存中…";
    if (save) save.disabled = true;
    try {
      const r = await fetch("/admin/settings/save", { method: "POST", body: new FormData(form) });
      const d = await r.json();
      if (!d.ok) throw new Error(d.error || "保存失败");
      // 密码类字段保存后清空：它的值已经进了钥匙串，不该继续留在表单里
      form.querySelectorAll("input[type=password]").forEach((p) => { p.value = ""; });
      baseline = snapshot();
      dirtyCheck();
      if (msg) msg.textContent = "已保存 · 查询台与 Redis 页重新打开后生效";
      bar?.classList.add("on");
      setTimeout(() => { if (!changedCount()) bar?.classList.remove("on"); }, 2600);
    } catch (err) {
      if (msg) msg.textContent = "保存失败：" + (err.message || err);
      bar?.classList.add("on");
    } finally {
      if (save) save.disabled = false;
    }
  });

  // 改了没保存就离开，浏览器给一次确认——设置页最容易发生的丢失
  window.addEventListener("beforeunload", (e) => {
    if (changedCount()) { e.preventDefault(); e.returnValue = ""; }
  });

  /* ---------------- 搜索 ----------------
     十几个旋钮里找一个，比在四个 tab 之间来回翻快得多。匹配名字与说明，
     整个分区都没命中就把分区一起收起，免得留一排空标题。 */
  const search = page.querySelector(".set-search input");
  search?.addEventListener("input", () => {
    const q = search.value.trim().toLowerCase();
    let hits = 0;
    page.querySelectorAll(".set-sec").forEach((sec) => {
      let secHit = 0;
      sec.querySelectorAll(".set-row").forEach((row) => {
        // 用 textContent 而不是 innerText：收起的分区里 innerText 为空，
        // 那些行会永远搜不到——而「搜得到」正是折叠的前提
        const match = !q || row.textContent.toLowerCase().includes(q);
        row.classList.toggle("hide", !match);
        if (match) secHit++;
      });
      sec.classList.toggle("hide", q ? secHit === 0 : false);
      const det = sec.querySelector("details");
      if (det && q && secHit) det.open = true;
      hits += secHit;
    });
    const empty = page.querySelector(".set-empty");
    if (empty) empty.style.display = q && !hits ? "" : "none";
  });

  dirtyCheck();
})();
