// 查询台 AI 面板用的 Markdown 渲染（从 console.js 拆出，无外部依赖）。挂到 window.DbmMd。
(function () {
  // Markdown → HTML（只供 AI 诊断文本用，不引外部库）：
  // 支持 GFM 表格（| a | b | + | --- | --- |）、```代码块```、# 标题、-/*/+/ 与 1./
  // 列表（含缩进嵌套、条目内续行与代码块、空行后接代码块仍属同一列表，编号不断）；
  // **粗体**、*斜体*、~~删除线~~、`行内代码`、> 引用（可嵌套）、--- 分隔线、空行分段。
  // 输入来自本机 AI 出口（纯排版文本），先整体转义再按块处理，不存在注入路径。
  function escHtml(s) {
    return String(s).replace(/[&<>"']/g, function (c) {
      return { "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c];
    });
  }

  function mdIndent(s) {
    var m = /^(\s*)/.exec(s);
    return m ? m[1].replace(/\t/g, "  ").length : 0;
  }

  // 代码块去掉一层最小公共缩进（列表条目内的 ``` 围栏内容通常带缩进）
  function mdDedent(buf) {
    var min = Infinity;
    buf.forEach(function (l2) { if (l2.trim()) { var w = mdIndent(l2); if (w < min) min = w; } });
    if (!isFinite(min) || min <= 0) return buf;
    return buf.map(function (l2) { return l2.slice(min); });
  }

  // 列表项标记：返回 {indent, ordered, text}；非列表行返回 null
  function listMarker(s) {
    var m = /^(\s*)(?:([-*+])|(\d+)[.)])\s+/.exec(s);
    if (!m) return null;
    return { indent: mdIndent(m[0]), ordered: !!m[3], text: s.slice(m[0].length) };
  }

  // 表格行切分：按 | 切，但忽略反引号代码区内的 |（如 `a|b`）
  function splitRowCells(s) {
    var t = s.trim();
    if (t.charAt(0) === "|") t = t.slice(1);
    if (t.length && t.charAt(t.length - 1) === "|") t = t.slice(0, -1);
    var cells = [], cur = "", inCode = false;
    for (var k = 0; k < t.length; k++) {
      var ch = t.charAt(k);
      if (ch === "`") inCode = !inCode;
      if (ch === "|" && !inCode) { cells.push(cur); cur = ""; }
      else cur += ch;
    }
    cells.push(cur);
    return cells;
  }

  function mdInline(t) {
    return escHtml(t)
      .replace(/\*\*(.+?)\*\*/g, "<strong>$1</strong>")
      .replace(/__(.+?)__/g, "<strong>$1</strong>")
      .replace(/~~(.+?)~~/g, "<s>$1</s>")
      .replace(/(^|[^*\w])\*([^\s*][^*]*?)\*(?![*\w])/g, "$1<em>$2</em>")
      .replace(/`([^`]+)`/g, "<code>$1</code>");
  }

  // 围栏允许任意缩进：列表条目内的代码块缩进会超过 3 空格（相对列表层级的缩进）
  var _FENCE = /^[ \t]*```/;
  var _HR = /^\s*(?:-{3,}|\*{3,}|_{3,})\s*$/;
  var _HEAD = /^(#{1,6})\s+(.*)$/;
  var _TABLE_SEP = /^\s*\|?[\s:]*-{2,}[\s:|-]*\|?\s*$/;
  var _BLOCK_OPEN = /^(?:<pre|<div|<ul|<ol|<blockquote|<table)/;
  var _BLOCK_CLOSE = /(?:<\/pre>|<\/div>|<\/ul>|<\/ol>|<\/blockquote>|<\/table>)$/;

  // 把已收集的条目片段拼成 li 正文：纯文本片段走 mdInline 并用 <br/> 连接，
  // 块级片段（代码块/子列表）直接拼接、不加 <br/>，避免出现多余空行。
  function mdItemBody(parts) {
    var body = "";
    parts.forEach(function (seg, k) {
      if (k === 0) { body = _BLOCK_OPEN.test(seg) ? seg : mdInline(seg); return; }
      var blockish = _BLOCK_OPEN.test(seg) || _BLOCK_CLOSE.test(body);
      body += blockish ? seg : "<br/>" + mdInline(seg);
    });
    return body;
  }

  // 解析一层列表（baseIndent = 本层标记的缩进）。嵌套靠缩进递归，编号/项目符号不断。
  function renderMdList(lines, i, n, baseIndent) {
    var ordered = null, lis = [];
    while (i < n) {
      var m = listMarker(lines[i]);
      if (!m || m.indent < baseIndent) break;
      if (m.indent > baseIndent) {  // 更深缩进 = 嵌套子列表，挂到上一个条目
        var sub = renderMdList(lines, i, n, m.indent);
        if (lis.length) lis[lis.length - 1].parts.push(sub.html);
        i = sub.next;
        continue;
      }
      if (ordered === null) ordered = m.ordered;
      var parts = [m.text];
      i++;
      while (i < n) {
        var l = lines[i];
        if (_FENCE.test(l)) {  // 条目内的代码块
          var buf = [], lang = l.trim().slice(3).trim();
          i++;
          while (i < n && !_FENCE.test(lines[i])) { buf.push(lines[i]); i++; }
          if (i < n) i++;
          parts.push("<pre" + (lang ? " class=\"lang-" + escHtml(lang) + "\"" : "") + ">"
                     + escHtml(mdDedent(buf).join("\n")) + "</pre>");
          continue;
        }
        if (!l.trim()) {  // 空行：往后看决定是续接还是列表结束
          var j = i + 1;
          while (j < n && !lines[j].trim()) j++;
          if (j >= n) break;
          var nx = lines[j];
          if (_FENCE.test(nx)) { i = j; continue; }  // 空行后接代码块 → 属于本条目
          var m2 = listMarker(nx);
          if (m2 && m2.indent >= baseIndent) { i = j; break; }  // 下一项
          if (m2 || mdIndent(nx) <= baseIndent) { i = j; break; }  // 外层内容（含更浅的列表标记）→ 越过空行交还外层，避免外层列表因停在空行上而断开
          i = j; continue;  // 缩进续行
        }
        if (listMarker(l)) break;  // 下一项或嵌套，交给外层循环
        parts.push(l.replace(/^ {0,3}/, ""));  // 条目内续行
        i++;
      }
      lis.push({ parts: parts });
    }
    var html = lis.map(function (it) { return "<li>" + mdItemBody(it.parts) + "</li>"; }).join("");
    return { html: "<" + (ordered ? "ol" : "ul") + ">" + html + "</" + (ordered ? "ol" : "ul") + ">", next: i };
  }

  function renderMd(text) {
    var src = String(text || "").replace(/\r\n?/g, "\n");
    var lines = src.split("\n"), out = [], i = 0, n = lines.length;
    while (i < n) {
      var line = lines[i];
      if (!line.trim()) { i++; continue; }
      if (_HR.test(line)) { out.push("<hr/>"); i++; continue; }
      if (_FENCE.test(line)) {
        var buf = [], lang = line.trim().slice(3).trim();
        i++;
        while (i < n && !_FENCE.test(lines[i])) { buf.push(lines[i]); i++; }
        if (i < n) i++;
        out.push("<pre" + (lang ? " class=\"lang-" + escHtml(lang) + "\"" : "") + ">"
                 + escHtml(mdDedent(buf).join("\n")) + "</pre>");
        continue;
      }
      // GFM 表格：表头行以 | 开头，且紧接一行 | --- | 分隔线
      if (/^\s*\|/.test(line) && i + 1 < n && /\|/.test(lines[i + 1])
          && _TABLE_SEP.test(lines[i + 1])) {
        var head = splitRowCells(line);
        var aligns = splitRowCells(lines[i + 1]).map(function (c) {
          var s2 = c.trim(), a = /^:/.test(s2), b = /:$/.test(s2);
          return (a && b) ? "center" : (a ? "left" : (b ? "right" : ""));
        });
        i += 2;
        var rows = [];
        while (i < n && lines[i].trim() && /^\s*\|/.test(lines[i])) {
          rows.push(splitRowCells(lines[i])); i++;
        }
        var thead = head.map(function (c, k) {
          return "<th" + (aligns[k] ? " class=\"al-" + aligns[k] + "\"" : "") + ">"
                 + mdInline(c.trim()) + "</th>";
        }).join("");
        var tbody = rows.map(function (r) {
          return "<tr>" + r.map(function (c, k) {
            return "<td" + (aligns[k] ? " class=\"al-" + aligns[k] + "\"" : "") + ">"
                   + mdInline(c.trim()) + "</td>";
          }).join("") + "</tr>";
        }).join("");
        out.push("<div class=\"md-table\"><table><thead><tr>" + thead + "</tr></thead><tbody>"
                 + tbody + "</tbody></table></div>");
        continue;
      }
      var hm = _HEAD.exec(line);
      if (hm) {
        var lv = Math.min(hm[1].length, 4);
        out.push("<h" + lv + ">" + mdInline(hm[2].trim()) + "</h" + lv + ">");
        i++; continue;
      }
      if (/^>\s?/.test(line)) {  // 引用：剥掉 > 后递归，支持嵌套与引用内富文本
        var q = [];
        while (i < n && /^>\s?/.test(lines[i])) { q.push(lines[i].replace(/^>\s?/, "")); i++; }
        out.push("<blockquote>" + renderMd(q.join("\n")) + "</blockquote>");
        continue;
      }
      if (listMarker(line)) {
        var r = renderMdList(lines, i, n, listMarker(line).indent);
        out.push(r.html); i = r.next; continue;
      }
      var para = [];  // 普通段落：连续非块行合并
      while (i < n && lines[i].trim()
             && !_FENCE.test(lines[i]) && !_HR.test(lines[i]) && !_HEAD.test(lines[i])
             && !/^>\s?/.test(lines[i]) && !listMarker(lines[i])
             && !/^\s*\|/.test(lines[i])) {
        para.push(lines[i]); i++;
      }
      if (!para.length) { para.push(lines[i]); i++; }  // 兜底：单行不匹配任何块（如 | 开头但非表格），也要输出并推进，避免死循环
      out.push("<p>" + mdInline(para.join(" ")) + "</p>");
    }
    return out.join("");
  }
  window.DbmMd = { escHtml: escHtml, listMarker: listMarker, mdDedent: mdDedent, mdIndent: mdIndent, mdInline: mdInline, mdItemBody: mdItemBody, renderMd: renderMd, renderMdList: renderMdList, splitRowCells: splitRowCells };
})();
