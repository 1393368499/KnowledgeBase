/* 公共：会话感知的 fetch 封装 + 信息库下拉 + 通知条。
 *
 * 关键点（用户要求）：
 * - 用 **session cookie** 调 API，不在浏览器里存 Basic 口令；
 * - 任何 401 都跳登录页（而不是弹浏览器原生 Basic 对话框）；
 * - 库下拉只列当前用户**可读**的库（后端已按权限过滤）。
 */
(function () {
  "use strict";

  const KB = (window.KB = {});

  /** 读 Django 的 CSRF cookie。
   *
   * 为什么必须有：DRF 的 SessionAuthentication 对**写请求**强制 CSRF 校验，
   * 少了这个头后端会回 403 "CSRF Failed: CSRF token missing."。
   * 登录表单带了 {% csrf_token %}，所以 cookie 早就种好了，这里只是取来用。
   */
  KB.csrfToken = function () {
    const match = document.cookie.match(/(?:^|;\s*)csrftoken=([^;]+)/);
    return match ? decodeURIComponent(match[1]) : "";
  };

  /**
   * 统一的 API 封装。**任何失败都返回同一个形状**，调用方只需看 `result.ok`：
   *
   *   {ok: true,  status, body}                      业务成功
   *   {ok: false, status, body, network: false}      4xx/5xx（body.detail 是服务端原因）
   *   {ok: false, status: 0, body, network: true}    **网络层失败**（断网/DNS/连接被拒）
   *
   * 为什么要补网络分支（Codex 独立审查发现）：旧实现直接 `await fetch`，断网时
   * 这个 Promise 会 **reject**，而调用方大多没写 `.catch` → 事件处理器静默失败、
   * 按钮永久 disabled、界面上一片空白，用户以为"点了没反应"。现在网络异常也被
   * 兜成 `ok:false` 的普通返回值，且带可读的 `body.detail`。
   *
   * 401 仍然跳登录页（session 过期的语义不能被当成普通失败吞掉），
   * 但改为返回失败结果而不是抛异常 —— 调用方不会被未捕获的异常打断。
   */
  KB.api = async function (url, options) {
    const opts = Object.assign({ credentials: "same-origin", headers: {} }, options || {});
    const method = (opts.method || "GET").toUpperCase();
    if (method !== "GET" && method !== "HEAD") {
      opts.headers["X-CSRFToken"] = KB.csrfToken();
    }
    if (opts.json !== undefined) {
      opts.headers["Content-Type"] = "application/json";
      opts.body = JSON.stringify(opts.json);
      delete opts.json;
    }
    if (opts.form !== undefined) {
      opts.body = opts.form;
      delete opts.form;
    }
    let response;
    try {
      response = await fetch(url, opts);
    } catch (err) {
      // 网络层失败：明确区分于"服务端返回了错误"
      const reason = (err && err.message) ? err.message : "未知网络错误";
      return {
        ok: false,
        status: 0,
        network: true,
        body: { detail: "网络请求失败（" + reason + "）；请检查网络或稍后重试" },
      };
    }
    let text = "";
    try {
      text = await response.text();
    } catch (err) {
      // **读取响应体本身也可能失败**（连接在流中途断开/内容被截断）：这时 fetch
      // 已经 resolve 过了，只 catch fetch 是不够的。仍按网络失败如实返回。
      const reason = (err && err.message) ? err.message : "读取响应失败";
      return {
        ok: false,
        status: response.status,
        network: true,
        body: { detail: "读取响应失败（" + reason + "）；请稍后重试" },
      };
    }
    let body = null;
    try {
      body = text ? JSON.parse(text) : null;
    } catch (err) {
      body = { raw: text };
    }
    // **只把"未认证"当会话过期**：401，或后端明确给出的 code=not_authenticated。
    // 权限 403 / CSRF 403 必须原地如实报错（旧实现只认 401，而 DRF 的
    // SessionAuthentication 会把"没登录"渲染成 403 → 会话过期时的跳转是坏的；
    // 反过来若把权限 403 也跳登录，就会把用户从有权限的页面赶走）。
    const notAuthenticated = response.status === 401 ||
      (body && body.code === "not_authenticated");
    if (notAuthenticated) {
      // 只跳一次（多个并发请求同时未认证时不要反复覆盖 next）
      if (!KB._redirectingToLogin) {
        KB._redirectingToLogin = true;
        window.location.href =
          "/app/login/?next=" + encodeURIComponent(window.location.pathname + window.location.search);
      }
      return {
        ok: false,
        status: response.status || 401,
        network: false,
        body: { detail: "登录已过期，正在跳转到登录页…", code: "not_authenticated" },
      };
    }
    return { status: response.status, ok: response.ok, network: false, body: body };
  };

  /** 把 `KB.api` 的失败结果翻译成一句**能显示给用户**的话（各处统一口径）。 */
  KB.errorText = function (result, fallback) {
    if (result && result.network) {
      return (result.body && result.body.detail) || "网络请求失败，请稍后重试";
    }
    const detail = result && result.body && (result.body.detail || result.body.raw);
    if (detail) return String(detail).slice(0, 300);
    return (fallback || "请求失败") + "（HTTP " + ((result && result.status) || "?") + "）";
  };

  KB.notice = function (target, text, kind) {
    const box = typeof target === "string" ? document.getElementById(target) : target;
    if (!box) return;
    if (!text) {
      box.innerHTML = "";
      return;
    }
    box.innerHTML = "";
    const div = document.createElement("div");
    div.className = "notice " + (kind || "");
    div.textContent = text;
    box.appendChild(div);
  };

  KB.loadLibraries = async function (selectId, options) {
    const opts = options || {};
    const select = document.getElementById(selectId);
    if (!select) return [];
    const result = await KB.api("/api/v1/knowledge/libraries/?active=1");
    const rows = (result.body && (result.body.results || result.body)) || [];
    select.innerHTML = "";
    // **必须保留一个"全部"空选项，并且让它默认选中**：
    // 清空后如果直接追加真实库，浏览器会自动选中第一项——页面文案写着
    // "全部信息库"，实际却只在第一个库里搜（实测踩到：默认选中了合同库）。
    if (opts.placeholder) {
      const opt = document.createElement("option");
      opt.value = "";
      opt.textContent = opts.placeholder;
      opt.dataset.name = opts.placeholder;
      opt.selected = true;
      select.appendChild(opt);
    }
    rows.forEach(function (row) {
      const opt = document.createElement("option");
      opt.value = row.code;
      opt.textContent = row.name + "（" + row.code + "）";
      opt.dataset.id = String(row.id);
      opt.dataset.name = row.name || row.code;
      select.appendChild(opt);
    });
    return rows;
  };

  KB.statusTag = function (status) {
    const span = document.createElement("span");
    span.className = "tag " + (status || "");
    const labels = {
      pending: "待处理",
      running: "处理中",
      done: "已完成",
      failed: "失败",
      archived: "已归档",
    };
    span.textContent = labels[status] || status || "-";
    return span;
  };

  /* ------------------------------------------------------------------ 资料「性质」
   *
   * 「性质」= `classification`，由后端按**正文内容**自动识别（五项固定词表：
   * 申请 / 合同 / 报表 / 普通文档 / 涉密文档），也允许人工指定。
   *
   * 与其他口径一样，前端**不猜**：只如实展示接口返回的三个字段 ——
   *   * `classification`       取值之一；**空字符串 = 还没识别出来**（不是"未设置"）
   *   * `classification_source` "" / auto_rule / auto_model / manual 的中文说法
   *   * `classification_note`   一句话依据，或**失败原因**（为什么没识别出来）
   * 后端还没上这三个新字段时（当前状态）：`classification_label` 缺失就退回原值，
   * source/note 缺失就**什么都不说**，绝不编一句"已自动识别"。空值一律显示「待识别」。
   */
  KB.NATURE_PENDING = "待识别";

  /** 六项筛选用的哨兵值：五项词表之外多一个「待识别」。**两端唯一取值**。
   *
   * 为什么不用空串：列表接口 `nature=` 为空时与"不带该参数"完全等价（等于不筛），
   * 空串无法表达"只看空性质"。这个值必需 **后端一起支持** 才真的能筛空性质；
   * 调用方必须按接口返回如实说明（见 kb-files.js / kb-search.js 的说明条），
   * 不能因为下拉里有这一项就宣称它生效了。
   */
  KB.NATURE_UNIDENTIFIED = "__unidentified__";

  KB.NATURE_SOURCE_LABELS = {
    auto_rule: "规则识别",
    auto_model: "模型识别",
    manual: "人工设置",
  };

  /** 性质显示文案：空值 → 「待识别」（**不显示空白，也不显示「—」**）。 */
  KB.natureLabel = function (item) {
    const row = item || {};
    const value = row.classification === null || row.classification === undefined
      ? ""
      : String(row.classification).trim();
    // `classification_label` 是后端给的显示名；缺失时**用 `classification` 原值**兜底
    const label = row.classification_label === null || row.classification_label === undefined
      ? ""
      : String(row.classification_label).trim();
    if (label) return label;
    return value || KB.NATURE_PENDING;
  };

  /** 来源的中文说法；未知来源**原样回显**（不翻译、不丢弃），确实没有就返回空串。 */
  KB.natureSourceLabel = function (item) {
    const source = ((item || {}).classification_source || "").trim();
    if (!source) return "";
    return KB.NATURE_SOURCE_LABELS[source] || source;
  };

  /** 悬浮说明：来源（规则识别 / 模型识别 / 人工设置）+ 依据或失败原因（一句话）。
   *
   * 只有真的拿到了才写：什么字段都没有就返回空串 —— 宁可没有 tooltip，
   * 也不能用"已自动识别"这类前端猜测去填满它。
   */
  KB.natureTooltip = function (item) {
    const row = item || {};
    const parts = [];
    const sourceLabel = KB.natureSourceLabel(row);
    if (sourceLabel) parts.push("来源：" + sourceLabel);
    const note = row.classification_note === null || row.classification_note === undefined
      ? ""
      : String(row.classification_note).trim();
    if (note) parts.push("依据/原因：" + note);
    return parts.join("；");
  };

  /** 与原型同款的 HTML 转义：所有拼进 innerHTML 的文本都必须先过它。 */
  KB.esc = function (value) {
    return String(value === undefined || value === null ? "" : value).replace(
      /[&<>"']/g,
      function (c) {
        return { "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c];
      }
    );
  };

  /** 与原型 MONO 一致：数字加千分位。 */
  KB.mono = function (value) {
    return typeof value === "number" ? value.toLocaleString("zh-CN") : String(value === undefined || value === null ? "" : value);
  };

  /** 与原型 fmtSize 一致。 */
  KB.fmtSize = function (bytes) {
    if (!bytes) return "0 B";
    const units = ["B", "KB", "MB", "GB"];
    let value = bytes;
    let index = 0;
    while (value >= 1024 && index < units.length - 1) {
      value /= 1024;
      index += 1;
    }
    return (index === 0 ? value : value.toFixed(1)) + " " + units[index];
  };

  /** 与原型 fmtTime 一致（YYYY-MM-DD HH:mm）。 */
  KB.fmtTime = function (iso) {
    if (!iso) return "—";
    const date = new Date(iso);
    if (isNaN(date.getTime())) return "—";
    const pad = function (n) { return String(n).padStart(2, "0"); };
    return (
      date.getFullYear() + "-" + pad(date.getMonth() + 1) + "-" + pad(date.getDate()) +
      " " + pad(date.getHours()) + ":" + pad(date.getMinutes())
    );
  };

  /**
   * **统一原件预览**（三个入口共用：资料列表 / 检索结果 / 任务列表）。
   *
   * 旧行为（用户复现的缺陷）：各入口把后端 `chunks.text` 塞进 `<p>` ——
   * PDF 的原页/表格/水印位置全丢，文字挤成一段，水印还会混进正文。
   * 现在：**显示原件本身**。
   *   * PDF：服务端按**原页**渲染 PNG（保留表格与水印位置，不做任何重排），
   *     客户端只做翻页/缩放（页码与缩放都有界）；
   *   * 图片：直接显示原图；
   *   * TXT/MD：**保留换行**并转义后显示；
   *   * DOCX/XLSX：**结构化提取**并按顺序渲染，明确标注"非原始版式"；
   *   * 原件缺失/不支持：**明确说明**，只能显示索引文字时如实标注为"索引文字"。
   * 预览只读取本地原件并回给已授权用户，**不涉及任何模型外呼**。
   */
  KB.previewFile = async function (fileId) {
    const result = await KB.api("/api/v1/knowledge/files/" + fileId + "/preview/");
    if (!result.ok || !result.body) {
      // **如实报出服务端原因**：旧实现一律说"可能没有权限"，把
      // 404（原件缺失/不可读）、422（页码越界）、500（渲染失败）和真的 403
      // 混成同一句话，还把 `detail` 丢掉 —— 用户拿不到任何可执行的线索。
      // 现在带上 HTTP 状态 + 服务端 detail（`KB.errorText` 已就绪）。
      const detail = KB.errorText(result, "无法打开预览");
      KB.toast("无法打开预览：" + detail);
      return;
    }
    const meta = result.body;
    const head =
      '<div class="modal-head"><b>' + KB.esc(meta.original_name || "资料预览") + "</b>" +
      '<span class="grow"></span>' +
      '<span class="tag">' + KB.esc(meta.kind || "") + "</span>" +
      '<button class="btn sm" data-close>关闭</button></div>';

    const body = [];
    body.push(
      '<div class="muted tiny" style="margin-bottom:8px">' +
      KB.esc(meta.library_code || "") +
      (meta.size_bytes ? " · " + KB.fmtSize(meta.size_bytes) : "") +
      (meta.original_present ? "" : " · 原件缺失") +
      "</div>"
    );

    if (!meta.original_present) {
      // **诚实**：原件缺失时明确说明，并把索引文字单独标注出来
      body.push(
        '<p class="err-text">原件缺失或不可访问：' + KB.esc(meta.note || "") +
        "。以下内容为<strong>索引文字</strong>，不代表原件版式。</p>"
      );
      body.push(await kbIndexText(fileId));
    } else if (meta.kind === "pdf" && meta.page_count) {
      body.push(pdfViewerHtml(meta));
    } else if (meta.kind === "image") {
      body.push(
        '<div class="preview-scroll">' +
        '<div class="preview-loading" data-img-loading>正在加载原图…</div>' +
        '<div class="preview-error" data-img-error hidden>' +
        "<div>图片加载失败</div>" +
        '<div class="row" style="justify-content:center;margin-top:8px">' +
        '<button class="btn sm" data-img-retry>重试</button>' +
        '<a class="btn sm" href="' + KB.esc(meta.download_url) + '">下载原件</a>' +
        "</div></div>" +
        '<img class="preview-img" data-image src="' + KB.esc(meta.download_url) +
        '" alt="' + KB.esc(meta.original_name) + '"></div>'
      );
    } else if (meta.kind === "text") {
      body.push(
        '<pre class="preview-text">' + KB.esc(meta.text || "") + "</pre>" +
        (meta.truncated ? '<p class="muted tiny">内容过长，已截断显示。</p>' : "")
      );
    } else if (meta.blocks && meta.blocks.length) {
      // DOCX/XLSX：结构化提取 —— **不是原始版式**，必须说清楚；
      // 命中后端上限时**明确提示截断**（不悄悄少显示）。
      body.push('<p class="muted tiny">' + KB.esc(meta.note || "") + "</p>");
      body.push('<div class="preview-blocks">' +
        meta.blocks.map(blockHtml).join("") + "</div>");
      if (meta.blocks_truncated) {
        body.push(
          '<p class="muted tiny">内容较多，已按上限截断显示（' +
          "表格最多 3 个、每表最多 100 行 / 20 列、单块最多 2000 字）；" +
          "完整内容请下载原件。</p>"
        );
      }
    } else {
      body.push('<p class="muted">' + KB.esc(meta.note || "该格式暂无原版渲染") + "</p>");
      body.push(await kbIndexText(fileId));
    }
    if (meta.parse_error) {
      body.push('<p class="err-text">' + KB.esc(meta.parse_error) + "</p>");
    }

    // **原件缺失时不给"下载原件"按钮** —— 那只会点出一个 404，
    // 用户会以为"下载坏了"，而不是"原件本来就不在"。
    const downloadBtn = meta.original_present
      ? '<a class="btn sm" href="' + KB.esc(meta.download_url) + '">下载原件</a>'
      : '<span class="muted tiny">原件缺失，无法下载</span>';
    const mask = KB.modal(
      head + '<div class="modal-body paper">' + body.join("") + "</div>" +
      '<div class="modal-foot">' + downloadBtn +
      '<button class="btn sm" data-close>关闭</button></div>',
      "860px"
    );
    if (meta.kind === "pdf" && meta.page_count) bindPdfViewer(mask, meta);
    if (meta.kind === "image" && meta.original_present) bindImageViewer(mask, meta);
    return mask;
  };

  /** 原图的加载态与失败态（同样**不静默破图**）。 */
  function bindImageViewer(mask, meta) {
    const img = mask.querySelector("[data-image]");
    const loading = mask.querySelector("[data-img-loading]");
    const errorBox = mask.querySelector("[data-img-error]");
    let attempt = 0;
    if (!img) return;
    img.addEventListener("load", function () {
      if (loading) loading.hidden = true;
    });
    img.addEventListener("error", function () {
      if (loading) loading.hidden = true;
      img.hidden = true;
      if (errorBox) errorBox.hidden = false;
    });
    mask.addEventListener("click", function (event) {
      if (!event.target.closest("[data-img-retry]")) return;
      attempt += 1;
      img.hidden = false;
      if (errorBox) errorBox.hidden = true;
      if (loading) loading.hidden = false;
      img.src = meta.download_url + "?_r=" + attempt;
    });
  }

  /** 索引文字（**只在原件缺失/不支持时**显示，并明确标注来源）。 */
  async function kbIndexText(fileId) {
    const chunks = await KB.api("/api/v1/knowledge/files/" + fileId + "/chunks/?page_size=50");
    const rows = (chunks.ok && chunks.body && chunks.body.results) || [];
    if (!rows.length) {
      return '<p class="muted">这份资料还没有可显示的索引文字（可能尚未处理完成）。</p>';
    }
    return (
      '<h4>索引文字（非原件）</h4>' +
      rows.map(function (chunk) {
        return "<h5>第 " + ((chunk.ordinal || 0) + 1) + " 节</h5><p>" +
          KB.esc(chunk.text) + "</p>";
      }).join("")
    );
  }

  /** 结构化块 → 安全 HTML（段落按顺序；表格 cells 全部转义）。 */
  function blockHtml(block) {
    if (block.type === "paragraph") {
      return "<p>" + KB.esc(block.text || "") + "</p>";
    }
    if (block.type === "table") {
      const rows = block.rows || [];
      const head = rows.length
        ? "<thead><tr>" + rows[0].map(function (cell) {
            return "<th>" + KB.esc(cell || "") + "</th>";
          }).join("") + "</tr></thead>"
        : "";
      const rest = rows.slice(1).map(function (row) {
        return "<tr>" + row.map(function (cell) {
          return "<td>" + KB.esc(cell || "") + "</td>";
        }).join("") + "</tr>";
      }).join("");
      return (block.title ? '<h4>' + KB.esc(block.title) + "</h4>" : "") +
        '<table class="table">' + head + "<tbody>" + rest + "</tbody></table>";
    }
    return "";
  }

  /** PDF 查看器骨架：翻页 + **真实尺寸缩放**（默认适合宽度）。 */
  function pdfViewerHtml(meta) {
    return (
      '<div class="preview-toolbar row">' +
      '<button class="btn sm" data-page="prev">上一页</button>' +
      '<span class="mono tiny" data-page-label>1 / ' + meta.page_count + "</span>" +
      '<button class="btn sm" data-page="next">下一页</button>' +
      '<span class="grow"></span>' +
      '<button class="btn sm" data-zoom="out">缩小</button>' +
      '<span class="mono tiny" data-zoom-label>适合宽度</span>' +
      '<button class="btn sm" data-zoom="in">放大</button>' +
      '<button class="btn sm" data-zoom="fit">适合宽度</button>' +
      "</div>" +
      '<div class="preview-scroll">' +
      '<div class="preview-loading" data-loading>正在加载页面…</div>' +
      '<div class="preview-error" data-error hidden>' +
      "<div>页面加载失败</div>" +
      '<div class="row" style="justify-content:center;margin-top:8px">' +
      '<button class="btn sm" data-retry>重试</button>' +
      '<a class="btn sm" href="' + KB.esc(meta.download_url) + '">下载原件</a>' +
      "</div></div>" +
      '<img class="preview-img" data-page-img alt="第 1 页">' +
      "</div>"
    );
  }

  function bindPdfViewer(mask, meta) {
    let page = 1;
    // **显示缩放**（相对"适合宽度"）：1 = 适合宽度（占满容器宽），
    // >1 = 比容器更宽 → 画布**横向滚动**。
    // 这才是真实的尺寸变化；旧实现用 `max-width:100%` 把大页压回同一宽度，
    // 按钮只提高了像素密度、页面看起来没变大。
    let displayZoom = 1;
    const ZOOM_MIN = 0.5;
    const ZOOM_MAX = 3;
    const ZOOM_STEP = 0.25;
    // 服务端渲染倍率固定在一个较清晰且有界的值（决定像素密度）；
    // **显示尺寸**由 displayZoom 决定。
    const RENDER_SCALE = 2;
    const img = mask.querySelector("[data-page-img]");
    const pageLabel = mask.querySelector("[data-page-label]");
    const zoomLabel = mask.querySelector("[data-zoom-label]");
    const loading = mask.querySelector("[data-loading]");
    const errorBox = mask.querySelector("[data-error]");
    const zoomOut = mask.querySelector('[data-zoom="out"]');
    const zoomIn = mask.querySelector('[data-zoom="in"]');
    let attempt = 0;

    const applyZoom = function () {
      displayZoom = Math.max(ZOOM_MIN, Math.min(ZOOM_MAX, displayZoom));
      img.style.width = Math.round(displayZoom * 100) + "%";
      zoomLabel.textContent = displayZoom === 1
        ? "适合宽度"
        : Math.round(displayZoom * 100) + "%";
      if (zoomOut) zoomOut.disabled = displayZoom <= ZOOM_MIN;
      if (zoomIn) zoomIn.disabled = displayZoom >= ZOOM_MAX;
    };

    const load = function () {
      // 页码**必须有界**：越界不请求、按钮禁用
      page = Math.max(1, Math.min(meta.page_count, page));
      if (loading) loading.hidden = false;
      if (errorBox) errorBox.hidden = true;
      img.hidden = false;
      // `_r` 为缓存破坏参数：重试时强制重新取图
      img.src = meta.page_url.replace("{page}", String(page)) +
        "?scale=" + RENDER_SCALE + "&_r=" + attempt;
      // alt 必须跟着页码走：否则翻到第 3 页，无障碍/兜底文案仍写着"第 1 页"
      img.alt = "第 " + page + " 页";
      pageLabel.textContent = page + " / " + meta.page_count;
      const prev = mask.querySelector('[data-page="prev"]');
      const next = mask.querySelector('[data-page="next"]');
      if (prev) prev.disabled = page <= 1;
      if (next) next.disabled = page >= meta.page_count;
    };

    img.addEventListener("load", function () {
      if (loading) loading.hidden = true;
    });
    img.addEventListener("error", function () {
      // **绝不静默破图**：明确报错，并给出重试与下载原件的出路
      if (loading) loading.hidden = true;
      img.hidden = true;
      if (errorBox) errorBox.hidden = false;
    });

    mask.addEventListener("click", function (event) {
      const pageBtn = event.target.closest("[data-page]");
      if (pageBtn) {
        page += pageBtn.getAttribute("data-page") === "next" ? 1 : -1;
        load();
        return;
      }
      const zoomBtn = event.target.closest("[data-zoom]");
      if (zoomBtn) {
        const action = zoomBtn.getAttribute("data-zoom");
        if (action === "fit") displayZoom = 1;
        else if (action === "in") displayZoom += ZOOM_STEP;
        else displayZoom -= ZOOM_STEP;
        applyZoom();
        return;
      }
      if (event.target.closest("[data-retry]")) {
        attempt += 1;
        load();
      }
    });
    applyZoom();
    load();
  }

  /** 与原型 toast 一致（居中底部提示）。
   *
   * 第二参数 `kind`（"ok" / "warn" / "err"）**过去被静默丢弃** —— 于是"已取消"
   * 和"解析未受理"长得一模一样，失败在一闪而过的提示里读不出来（处置清单
   * OP-18）。调用点早就传了这个参数（例如 `kb-pages.js` 的 `KB.toast(..., "err")`），
   * 所以这里只是把它接上：样式类在 kb-home.css（原型 CSS 只读）。
   * 不传 kind 时行为与旧版完全一致。
   */
  KB.toast = function (text, kind) {
    const node = document.getElementById("toast");
    if (!node) return;
    node.textContent = text;
    node.classList.remove("toast-ok", "toast-warn", "toast-err");
    if (kind) node.classList.add("toast-" + kind);
    node.classList.add("on");
    clearTimeout(node._timer);
    node._timer = setTimeout(function () { node.classList.remove("on"); }, 1800);
  };

  /** **受控操作**：提案 → 逐次确认 → 执行。删除/写入类一律走这里。
   *
   * 为什么放在公共层：文件管理与搜索页都要删除资料，绝不能各写一套。
   * 语义与首页一致（服务端说了算）：
   *   - 提案只是"待确认"，不产生任何改动；
   *   - 破坏性操作**三次独立确认**，最后一次要求**原样**输入目标名称；
   *   - 取消/失败不推进也不倒退；
   *   - 确认有执行窗口，过期必须重新提案（服务端会拒）。
   *
   * 返回 {ok, skipped, result, detail}；skipped=true 表示用户在确认过程中取消。
   */
  KB.controlled = async function (options) {
    const intent = options.intent;
    const params = options.params || {};
    const labels = options.labels || {};

    const proposed = await KB.api("/api/v1/planner/operations/propose/", {
      method: "POST",
      json: { intent: intent, params: params, conversation_id: null },
    });
    if (!proposed.ok) {
      return { ok: false, detail: (proposed.body && proposed.body.detail) || "无法生成操作提案" };
    }
    const body = proposed.body;
    if (body.auto_executed && body.status === "executed") {
      return { ok: true, result: body.result || {}, operation_id: body.operation_id };
    }
    if (body.requires_confirmation === false) {
      return { ok: false, detail: "操作未完成，请查看执行记录", operation_id: body.operation_id };
    }
    const destructive = !!body.requires_destructive_flow;
    const total = Number(body.required_confirmations || (destructive ? 3 : 1));
    // **影响范围来自提案响应**（后端权威）：前端只负责排版，
    // 不再自己去查集合/分页统计（那样会因分页上限与字段类型而显示不全甚至为空）。
    const impactLines =
      options.impactFormatter && body.impact
        ? options.impactFormatter(body.impact) || []
        : options.extraLines || [];
    let token = body.confirm_token;

    for (let step = 1; step <= total; step += 1) {
      const isFinal = step === total;
      const needName = isFinal && destructive;
      const answer = await KB.confirmDialog({
        title: (labels.title || body.label) + " · 第 " + step + " 次确认（共 " + total + " 次）",
        body: (needName
          ? "这是最后一次确认。确认后将执行，且不可撤销。"
          : isFinal
            ? "这是最后一次确认。"
            : "请独立确认第 " + step + " 次；之后还需要 " + (total - step) + " 次确认。") +
          // 影响说明（由调用方用**提案返回的** impact 排版；确认卡必须写清删什么）
          (impactLines.length ? "\n\n" + impactLines.join("\n") : ""),
        targetLabel: body.target_label,
        requireTypedName: needName,
        confirmLabel: needName ? "确认并允许执行" : "确认",
      });
      if (!answer.confirmed) {
        return { ok: false, skipped: true, detail: "已取消，未做任何改动", operation_id: body.operation_id };
      }
      if (needName && answer.typedName !== body.target_label) {
        return { ok: false, detail: "名称不一致，未执行", operation_id: body.operation_id };
      }
      const confirmed = await KB.api(
        "/api/v1/planner/operations/" + body.operation_id + "/confirm/",
        { method: "POST", json: { token: token, typed_name: answer.typedName } }
      );
      if (!confirmed.ok) {
        return {
          ok: false,
          detail: (confirmed.body && confirmed.body.detail) || "确认失败，可重试",
          operation_id: body.operation_id,
        };
      }
      token = confirmed.body.next_confirm_token || "";
    }

    const done = await KB.api(
      "/api/v1/planner/operations/" + body.operation_id + "/execute/",
      { method: "POST", json: {} }
    );
    if (!done.ok) {
      return {
        ok: false,
        detail: (done.body && done.body.detail) || "执行失败",
        operation_id: body.operation_id,
      };
    }
    return { ok: true, result: done.body.result || {}, operation_id: body.operation_id };
  };

  /** 独立确认弹窗；遮罩 / ✕ / **Esc** / 取消**一定会结算** Promise（否则按钮会卡死）。
   *
   * Esc 与「取消」语义完全一致：结算 `{confirmed:false}`，调用方（`KB.controlled`
   * 的三次确认流程）据此返回 `skipped`，**不推进、不写库**。
   */
  KB.confirmDialog = function (options) {
    return new Promise(function (resolve) {
      let settled = false;
      function finish(value) {
        if (settled) return;
        settled = true;
        resolve(value);
      }
      KB.modal(
        '<div class="modal-head"><b>' + KB.esc(options.title) + "</b></div>" +
        '<div class="modal-body paper">' +
          // 确认卡里的影响说明是多行文本（换行分隔的几条），<p> 会把换行折成空格，
          // 于是"只改这一个字段"和"目标资料"会连成一句话读不通。这里显式转换。
          "<p>" + KB.esc(options.body).replace(/\n/g, "<br>") + "</p>" +
          (options.targetLabel
            ? '<p class="notice-bar">目标：<b>' + KB.esc(options.targetLabel) + "</b></p>"
            : "") +
          (options.requireTypedName
            ? '<p class="tiny muted">请输入上面显示的完整名称（必须完全一致）：</p>' +
              '<input id="kb-confirm-name" type="text" placeholder="准确输入目标名称" ' +
              'style="border:1px solid var(--color-border);padding:6px 8px;font-size:13px;width:100%">'
            : "") +
        "</div>" +
        '<div class="modal-foot">' +
          '<button class="btn sm" data-cancel>取消</button>' +
          '<button class="btn primary sm" data-ok>' +
          KB.esc(options.confirmLabel || "确认") + "</button>" +
        "</div>",
        "",
        // 任何关闭路径（Esc / 遮罩 / ✕）都按"取消"结算；要求抄写名称时焦点直接进输入框
        {
          onClose: function () { finish({ confirmed: false, typedName: "" }); },
          initialFocus: options.requireTypedName
            ? "#kb-confirm-name"
            : "[data-cancel]",
        }
      );
      const mask = KB.lastMask;
      mask.querySelector("[data-cancel]").addEventListener("click", function () {
        finish({ confirmed: false, typedName: "" });
        mask.remove();
      });
      mask.querySelector("[data-ok]").addEventListener("click", function () {
        const input = mask.querySelector("#kb-confirm-name");
        finish({ confirmed: true, typedName: input ? input.value : "" });
        mask.remove();
      });
    });
  };

  /** **自建**的 Promise 弹窗（`new Promise` + `KB.modal`）必须走这里。
   *
   * 为什么：公共的 Esc 处理走 `closeModal()` **直接移除遮罩**，不会触发任何
   * `[data-cancel]` / 遮罩 click 监听 —— 自建弹窗如果只在 click 上结算，按 Esc
   * 关掉之后 Promise **永久悬挂**，调用方的互斥标志（例如上传的 `uploadBusy`）
   * 就永远是 true，下一批操作会被"上一批还在..."挡住（用户会读成"点了没反应"）。
   *
   * 这里把"结算"收敛成一次：Esc / 遮罩 / ✕ / 取消按钮 全部只结算一个值，
   * 且**不会**重复 resolve（`settled` 守卫）。
   */
  KB.modalChoice = function (html, width) {
    return new Promise(function (resolve) {
      let settled = false;
      const settle = function (value) {
        if (settled) return;
        settled = true;
        resolve(value);
      };
      const mask = KB.modal(html, width || "", { onClose: function () { settle(null); } });
      const cancel = mask.querySelector("[data-cancel]");
      if (cancel) {
        cancel.addEventListener("click", function () {
          settle(null);
          mask.remove();
        });
      }
      const okay = mask.querySelector("[data-ok]");
      if (okay) {
        okay.addEventListener("click", function () {
          const picker = mask.querySelector("[data-choice-value]") ||
            mask.querySelector("select, input");
          settle(picker ? picker.value : "");
          mask.remove();
        });
      }
      return mask;
    });
  };

  /** 库列表里**当前账号真的能写入**的那些库（`my_role` = writer / admin）。
   *
   * 为什么必须过滤：上传/目录扫描的后端校验是 `CanContributeLibrary`（writer）。
   * 把只读库列进"上传到哪个信息库"的选择框，等于给用户一个**注定 403** 的入口。
   * 已经拿到完整列表时优先用 `rows` 参数，避免重复请求。
   *
   * 返回 `{rows, blocked, all}`：`blocked` = 被过滤掉的只读库（用于如实说明），
   * `all` = 未过滤的全部库（过滤后为空时兜底——宁可让后端报错，也不要让用户
   * 以为"一个库都没有"）。
   */
  KB.fetchWritableLibraries = async function (rows) {
    let all = rows;
    if (!all) {
      const result = await KB.api("/api/v1/knowledge/libraries/?page_size=200");
      all = (result.ok && result.body &&
        (result.body.results || result.body)) || [];
    }
    const writable = (all || []).filter(function (row) {
      return row.my_role === "writer" || row.my_role === "admin";
    });
    const blocked = (all || []).filter(function (row) {
      return writable.indexOf(row) < 0;
    });
    return { rows: writable.length ? writable : (all || []), blocked: blocked, all: all || [] };
  };

  /** 通用"选一个"弹窗；返回选中值或 null（取消）。   *
   * 用途：上传前**必须**让用户选信息库（不能无声默认写第一个库）。
   * 与 KB.confirmDialog 一样，遮罩/取消都会结算 Promise，不会悬挂。
   *
   * 选项支持可选的 `title`（悬浮说明）：上传弹窗用它讲清"留空 = 按正文自动识别"、
   * "选中 = 人工指定，不会被自动识别覆盖"——这些关键差别写在选项上比写在标题里
   * 更容易被看到。没给 `title` 的选项行为完全不变。
   */
  KB.choice = function (options) {
    return new Promise(function (resolve) {
      let settled = false;
      function finish(value) {
        if (settled) return;
        settled = true;
        resolve(value);
      }
      KB.modal(
        '<div class="modal-head"><b>' + KB.esc(options.title || "请选择") + "</b></div>" +
        '<div class="modal-body paper">' +
          (options.note ? '<p class="tiny muted">' + KB.esc(options.note) + "</p>" : "") +
          '<select id="kb-choice" style="border:1px solid var(--color-border);' +
          'border-radius:2px;padding:6px 8px;font-size:13px;width:100%">' +
          (options.options || []).map(function (item) {
            return '<option value="' + KB.esc(item.value) + '"' +
              (item.title ? ' title="' + KB.esc(item.title) + '"' : "") + ">" +
              KB.esc(item.label) + "</option>";
          }).join("") +
          "</select>" +
        "</div>" +
        '<div class="modal-foot">' +
          '<button class="btn sm" data-cancel>取消</button>' +
          '<button class="btn primary sm" data-ok>' +
          KB.esc(options.confirmLabel || "确定") + "</button>" +
        "</div>",
        "",
        // Esc / 遮罩 / ✕ 一律等价于「取消」（返回 null，调用方据此不推进）
        { onClose: function () { finish(null); } }
      );
      const mask = KB.lastMask;
      mask.querySelector("[data-cancel]").addEventListener("click", function () {
        finish(null);
        mask.remove();
      });
      mask.querySelector("[data-ok]").addEventListener("click", function () {
        finish(mask.querySelector("#kb-choice").value);
        mask.remove();
      });
    });
  };

  /* ------------------------------------------------------- 弹窗栈 ----
   *
   * 三个必须做到的事（处置清单 AF-14 / AF-30）：
   *   1. **Esc 关闭最上层弹窗**，且语义与「取消」完全一致 —— 受控确认流程里
   *      Esc = 取消，不推进、不写库（结算 Promise 的 cancelled 值）；
   *   2. 打开时**焦点进入弹窗**（否则键盘用户还在页面上，Esc/Tab 都不在弹窗里）；
   *   3. 关闭后**焦点回到触发元素**（`document.activeElement` 在打开时记下来）。
   *
   * 旧实现的唯一 Escape 处理器属于**侧栏抽屉**（`KB.initSidebar`），弹窗一个
   * 都关不掉；而且在确认弹窗里按 Esc 完全没反应，用户只能去找取消按钮。
   *
   * 为什么用栈：弹窗可以叠（上传选库 → 上传性质；工具栏 → 待办确认卡）。
   * Esc 只能关**最上面那一层**，否则一次按键就把底层流程一起拆了。
   */
  const modalStack = [];
  const MODAL_FOCUSABLE =
    'button:not([disabled]), [href], input:not([disabled]), select:not([disabled]), ' +
    'textarea:not([disabled]), [tabindex]:not([tabindex="-1"])';

  /** 结算式移除：关闭回调 + 焦点归还只做一次（remove/closeModal 双路径共用）。 */
  function closeModal(mask) {
    if (!mask || mask.dataset.kbClosed === "1") return;
    mask.dataset.kbClosed = "1";
    const index = modalStack.indexOf(mask);
    if (index >= 0) modalStack.splice(index, 1);
    const onClose = mask._kbOnClose;
    mask._kbOnClose = null;
    if (mask.parentNode) mask.parentNode.removeChild(mask);
    // **焦点归还触发元素**（还可见时才给，避免把焦点丢到已隐藏的节点上）
    const restore = mask._kbRestoreFocus;
    mask._kbRestoreFocus = null;
    if (restore && restore.isConnected && typeof restore.focus === "function") {
      try { restore.focus(); } catch (err) { /* 焦点归还失败不影响关闭 */ }
    }
    if (typeof onClose === "function") {
      try { onClose(); } catch (err) { /* 回调自身出错不能阻断其它弹窗 */ }
    }
  }
  KB.closeModal = closeModal;

  /** Esc：只关栈顶那一层（`preventDefault` 避免连带上层导航/抽屉的 Escape）。 */
  document.addEventListener("keydown", function (event) {
    if (event.key !== "Escape" && event.key !== "Esc") return;
    if (!modalStack.length) return;
    const top = modalStack[modalStack.length - 1];
    event.preventDefault();
    event.stopPropagation();
    closeModal(top);
  }, true);

  /** 与原型 modal() 一致：遮罩 + 弹层，点遮罩或 [data-close] 关闭。
   *
   * `options` 可选：
   *   * `onClose`  —— 任何关闭路径都会调用（Esc / 遮罩 / ✕ / 程序化关闭）；
   *   * `initialFocus` —— 打开后要聚焦的元素选择器（默认第一个可聚焦元素）。
   */
  KB.modal = function (html, width, options) {
    // 允许 `KB.modal(html, {onClose: …})`：第二参数是对象时按 options 处理
    if (width && typeof width === "object") {
      options = width;
      width = "";
    }
    const opts = options || {};
    const mask = KB.el("div", "mask");
    mask.innerHTML =
      '<div class="modal"' + (width ? ' style="width:' + width + '"' : "") + ">" + html + "</div>";
    const startedFrom = document.activeElement;
    const requestClose = function () { closeModal(mask); };
    // 记录"打开时的焦点"，关闭时还回去（Esc 关闭同样生效）
    mask._kbRestoreFocus =
      startedFrom && startedFrom !== document.body ? startedFrom : null;
    mask._kbOnClose = typeof opts.onClose === "function" ? opts.onClose : null;
    // 覆盖 `remove()`：**所有**现有调用点（以及 KB.confirmDialog / KB.choice 的
    // finish）都用它关闭弹窗，包一层就能保证焦点归还与关闭回调不依赖每个调用点。
    mask.remove = requestClose;
    mask.addEventListener("click", function (event) {
      if (event.target === mask || event.target.closest("[data-close]")) requestClose();
    });
    document.body.appendChild(mask);
    modalStack.push(mask);
    // 焦点进入弹窗：优先 `[autofocus]`（有无障碍语义），否则第一个可聚焦元素，
    // 最后兜底弹层本身（需要 tabindex 才可聚焦）。
    const modal = mask.querySelector(".modal");
    const preferred = (opts.initialFocus && mask.querySelector(opts.initialFocus)) ||
      mask.querySelector("[autofocus]") ||
      (modal && modal.querySelector(MODAL_FOCUSABLE));
    const target = preferred || modal;
    if (target) {
      if (target === modal && !modal.hasAttribute("tabindex")) {
        modal.setAttribute("tabindex", "-1");
      }
      try { target.focus(); } catch (err) { /* 聚焦失败不影响弹窗可用 */ }
    }
    // 记录"本次打开的弹窗"：调用方（确认/选择弹窗）必须用它，
    // 不能用 document.querySelector(".mask")——在已有弹窗之上再开一层时会选到父遮罩。
    KB.lastMask = mask;
    return mask;
  };

  KB.el = function (tag, className, text) {
    const node = document.createElement(tag);
    if (className) node.className = className;
    if (text !== undefined && text !== null) node.textContent = String(text);
    return node;
  };

  /** 移动端侧栏抽屉：点 ☰ 展开，点遮罩/导航项/Esc 关闭。
   *
   * 桌面端不启用（CSS 里 .sidebar 常驻固定侧栏，按钮 display:none）。
   *
   * 为什么必须有：原型的 @media (max-width:960px){ .sidebar{display:none} } 让
   * 窄屏上**没有任何导航入口**（base.html 的 sidebar 是唯一导航容器），
   * 手机/窄窗口只能手输 URL 才能换页。抽屉本身在桌面端完全不生效。
   */
  KB.initSidebar = function () {
    const button = document.getElementById("sidebar-toggle");
    const sidebar = document.getElementById("sidebar");
    const mask = document.getElementById("sidebar-mask");
    if (!button || !sidebar) return;

    function setOpen(open) {
      sidebar.classList.toggle("open", open);
      if (mask) mask.classList.toggle("on", open);
      button.setAttribute("aria-expanded", open ? "true" : "false");
      button.title = open ? "收起导航" : "展开导航";
    }

    button.addEventListener("click", function () {
      setOpen(!sidebar.classList.contains("open"));
    });
    if (mask) {
      mask.addEventListener("click", function () { setOpen(false); });
    }
    // 点了导航项/会话就关闭：窄屏下抽屉不能继续盖住刚打开的内容
    sidebar.addEventListener("click", function (event) {
      if (event.target.closest(".nav-item, [data-session]")) setOpen(false);
    });
    document.addEventListener("keydown", function (event) {
      if (event.key === "Escape") setOpen(false);
    });
    // 视口回到桌面宽度时复位，避免从抽屉状态切回来后残留遮罩
    window.addEventListener("resize", function () {
      if (window.innerWidth > 960) setOpen(false);
    });
  };

  // 所有页面都在（base.html 每个页面都加载它），所以在这里统一初始化一次。
  document.addEventListener("DOMContentLoaded", function () {
    KB.initSidebar();
  });
})();
