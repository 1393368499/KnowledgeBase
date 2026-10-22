/* 极简但够用的 Markdown 渲染器（**零依赖、默认转义 → 防 XSS**）。
 *
 * 为什么自己写而不是引第三方：
 * - 供应链：这是内部知识库，没必要为一个渲染器再引一个包；
 * - 可控：所有文本先转义再按白名单生成标签，不执行任何 HTML。
 *
 * 支持：标题(#~####)、无序/有序列表、围栏代码块、引用、表格、分隔线，
 * 以及行内 **粗体** / *斜体* / `代码` / [文字](链接)。
 */
(function () {
  "use strict";

  const KB = (window.KB = window.KB || {});

  function escapeHtml(text) {
    return String(text)
      .replace(/&/g, "&amp;")
      .replace(/</g, "&lt;")
      .replace(/>/g, "&gt;")
      .replace(/"/g, "&quot;")
      .replace(/'/g, "&#39;");
  }

  /** 行内元素：先转义，再只替换白名单语法。链接只允许 http/https/相对路径。 */
  function inline(text) {
    let out = escapeHtml(text);
    out = out.replace(/`([^`]+)`/g, function (_m, code) {
      return "<code>" + code + "</code>";
    });
    out = out.replace(/\*\*([^*]+)\*\*/g, "<strong>$1</strong>");
    out = out.replace(/(^|[^*])\*([^*]+)\*/g, "$1<em>$2</em>");
    out = out.replace(/\[([^\]]+)\]\(([^)\s]+)\)/g, function (match, label, href) {
      const safe = /^(https?:\/\/|\/)/i.test(href);
      if (!safe) return label;              // 非白名单协议：只留文字，不生成链接
      return '<a href="' + href + '" rel="noopener noreferrer">' + label + "</a>";
    });
    return out;
  }

  /** 渲染 Markdown → HTML 字符串。调用方负责塞进容器。 */
  KB.renderMarkdown = function (markdown) {
    const lines = String(markdown || "").split(/\r?\n/);
    const html = [];
    let inCode = false;
    let listType = "";     // "ul" | "ol"
    let inTable = false;
    let paragraph = [];

    function flushParagraph() {
      if (paragraph.length) {
        html.push("<p>" + inline(paragraph.join(" ")) + "</p>");
        paragraph = [];
      }
    }
    function closeList() {
      if (listType) {
        html.push("</" + listType + ">");
        listType = "";
      }
    }
    function closeTable() {
      if (inTable) {
        html.push("</tbody></table>");
        inTable = false;
      }
    }

    lines.forEach(function (raw) {
      const line = raw.replace(/\s+$/, "");

      // 围栏代码块
      if (/^\s*```/.test(line)) {
        flushParagraph();
        closeList();
        closeTable();
        if (inCode) {
          html.push("</code></pre>");
          inCode = false;
        } else {
          html.push("<pre><code>");
          inCode = true;
        }
        return;
      }
      if (inCode) {
        html.push(escapeHtml(raw));
        return;
      }

      // 空行
      if (!line.trim()) {
        flushParagraph();
        closeList();
        closeTable();
        return;
      }

      // 分隔线
      if (/^\s*([-*_])\1{2,}\s*$/.test(line)) {
        flushParagraph();
        closeList();
        closeTable();
        html.push("<hr>");
        return;
      }

      // 标题
      const heading = line.match(/^(#{1,4})\s+(.*)$/);
      if (heading) {
        flushParagraph();
        closeList();
        closeTable();
        const level = Math.min(heading[1].length + 1, 5); // # → h2（页面已有 h2 语义）
        html.push("<h" + level + ">" + inline(heading[2]) + "</h" + level + ">");
        return;
      }

      // 引用
      if (/^\s*>\s?/.test(line)) {
        flushParagraph();
        closeList();
        closeTable();
        html.push("<blockquote>" + inline(line.replace(/^\s*>\s?/, "")) + "</blockquote>");
        return;
      }

      // 表格（| a | b |，第二行是 |---|---|）
      if (/^\s*\|.*\|\s*$/.test(line)) {
        const cells = line.trim().replace(/^\||\|$/g, "").split("|").map(function (c) {
          return c.trim();
        });
        if (!inTable) {
          flushParagraph();
          closeList();
          html.push("<table><thead><tr>");
          cells.forEach(function (cell) {
            html.push("<th>" + inline(cell) + "</th>");
          });
          html.push("</tr></thead><tbody>");
          inTable = true;
          return;
        }
        if (cells.every(function (cell) { return /^:?-{2,}:?$/.test(cell); })) {
          return; // 分隔行：跳过
        }
        html.push("<tr>");
        cells.forEach(function (cell) {
          html.push("<td>" + inline(cell) + "</td>");
        });
        html.push("</tr>");
        return;
      }
      closeTable();

      // 列表
      const bullet = line.match(/^\s*[-*+]\s+(.*)$/);
      const ordered = line.match(/^\s*\d+[.)]\s+(.*)$/);
      if (bullet || ordered) {
        flushParagraph();
        const want = bullet ? "ul" : "ol";
        if (listType !== want) {
          closeList();
          html.push("<" + want + ">");
          listType = want;
        }
        html.push("<li>" + inline((bullet || ordered)[1]) + "</li>");
        return;
      }
      closeList();

      paragraph.push(line.trim());
    });

    flushParagraph();
    closeList();
    closeTable();
    if (inCode) html.push("</code></pre>"); // 未闭合的围栏也要收尾
    return html.join("\n");
  };

  /** 把 Markdown 渲染进指定容器（内部用 innerHTML，但内容是转义过的白名单标签）。 */
  KB.renderMarkdownInto = function (elementId, markdown) {
    const box = document.getElementById(elementId);
    if (!box) return;
    box.innerHTML = KB.renderMarkdown(markdown);
  };
})();
