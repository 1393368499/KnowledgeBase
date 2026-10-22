/* 搜索 —— 结构与原型最后生效的 viewSearch() 一致。
 *
 * 两条真实通路，各走各的：
 *   1. 结构化筛选 / 文件名 / 正文 → GET /api/v1/knowledge/files/
 *      （**服务端**过滤：nature→classification、amount_min、department、customer、
 *        file_type、mode=filename|content|hybrid；计数也是服务端的）
 *   2. 语义检索（保留原混合检索能力）→ POST /api/v1/knowledge/search/
 *      （稠密 + 关键词 + 重排；模型不可用时如实标注降级，不伪造）
 *
 * 筛选项与计数来自 /api/v1/knowledge/files/facets/（服务端聚合），
 * 不在前端"已取回的那一页"里统计——那样数字会说谎。
 */
(function () {
  "use strict";

  const KB = window.KB;
  const esc = KB.esc;
  const $ = function (id) { return document.getElementById(id); };

  const PAGE_SIZE = 10;
  const TYPE_ICON = { pdf: "▬", docx: "▭", pptx: "▮", xlsx: "▯", image: "◪", txt: "▫" };
  const HOT_WORDS = ["合同", "质保期", "验收", "结算", "采购", "方案"];
  const HISTORY_KEY = "kb-search-history";

  const state = {
    q: "", mode: "hybrid", department: "", customer: "", nature: "",
    amountMin: "", page: 1,
  };

  //: **请求票据**：每次检索/列表请求发一个新号，响应回来时号不是最新的就丢弃。
  //  为什么必须有它（Codex 独立浏览器审查）：快速改筛选/翻页时先发的慢响应可能
  //  后到，把**新**结果覆盖成旧结果；点「清空」后旧请求的响应甚至会把结果画回来。
  let searchTicket = 0;
  //: 已知总页数（上一次成功响应的真实总数算出来的）。「下一页」按它夹紧页码，
  //  避免把 page 推到总页数之外让后端 404、分页条被隐藏后无法恢复。
  //  分片序号是否真实存在**不在这里另存状态**：由结果卡上是否写了 `data-chunk`
  //  判定（见 `resultCard` / `sendFeedback`），避免多一份可能过期的标记。
  let searchTotalPages = 1;

  //: 原型只有三种模式；hybrid 走混合检索，另两种走结构化文件名/正文
  const MODES = ["hybrid", "filename", "content"];

  // ------------------------------------------------------------ 选项 ----

  function history() {
    try {
      return (localStorage.getItem(HISTORY_KEY) || "").split("|").filter(Boolean);
    } catch (error) {
      return [];
    }
  }

  function remember(word) {
    if (!word) return;
    const next = [word].concat(history().filter(function (item) { return item !== word; }));
    try {
      localStorage.setItem(HISTORY_KEY, next.slice(0, 8).join("|"));
    } catch (error) { /* 隐私模式：静默降级为不记录 */ }
  }

  function renderHot() {
    const words = history();
    $("hot-label").textContent = words.length ? "最近搜索：" : "热词：";
    const list = words.length ? words.slice(0, 6) : HOT_WORDS;
    $("hot-chips").innerHTML = list.map(function (word) {
      return '<button class="quick-chip" data-hot="' + esc(word) + '">' + esc(word) + "</button>";
    }).join(" ");
    document.querySelectorAll("[data-hot]").forEach(function (button) {
      button.addEventListener("click", function () {
        $("q").value = button.getAttribute("data-hot");
        state.q = $("q").value.trim();
        runSearch(true);
      });
    });
  }

  async function loadFacets() {
    // 界面不再显示信息库下拉（原型没有这个控件）；libraries 接口保留在后台。
    const facets = await KB.api("/api/v1/knowledge/files/facets/");
    const data = (facets.ok && facets.body) || {};

    // 客户下拉用服务端聚合（URL 带 customer 时继续生效）
    const customerSelect = $("customer");
    if (customerSelect) {
      customerSelect.innerHTML = '<option value="">不限</option>' +
        (data.customers || []).map(function (item) {
          return '<option value="' + esc(item.value) + '"' +
            (state.customer === item.value ? " selected" : "") + ">" + esc(item.value) +
            " (" + item.count + ")</option>";
        }).join("");
    }

    const fill = function (id, items, keep) {
      $(id).innerHTML = '<option value="">不限</option>' +
        (items || []).map(function (item) {
          return '<option value="' + esc(item.value) + '"' +
            (keep === item.value ? " selected" : "") + ">" + esc(item.value) +
            " (" + item.count + ")</option>";
        }).join("");
    };
    fill("department", data.departments, state.department);
    // 「资料性质」：五项词表（服务端给的真实份数）+ 「待识别」。
    // 「待识别」用 KB.NATURE_UNIDENTIFIED 表达空性质筛选 ——
    // 与文件管理页**同词表、同取值、同语义**（后端两处都认这个取值）。
    const natureSelect = $("nature");
    if (natureSelect) {
      fill("nature", data.natures, state.nature);
      natureSelect.insertAdjacentHTML("beforeend",
        '<option value="' + KB.NATURE_UNIDENTIFIED + '"' +
        (state.nature === KB.NATURE_UNIDENTIFIED ? " selected" : "") + ">" +
        esc(KB.NATURE_PENDING) + "</option>");
    }
  }

  /** 性质筛选的说明：只按**真实结果**说话（0 条就是"没有待识别的资料"）。 */
  function natureResultNote(count) {
    if (state.nature !== KB.NATURE_UNIDENTIFIED) return "";
    if (count > 0) {
      return "当前只显示「" + KB.NATURE_PENDING + "」（还没识别出性质）的资料，共 " +
        count + " 条。";
    }
    return "没有待识别（未识别性质）的资料。";
  }

  // -------------------------------------------------------- 结果渲染 ----

  // 命中分片的"小字摘要"（`.chunk`）**已按要求撤掉**，渲染时不再输出。
  //
  // 用户在 2026-10-16 明确要求（原话）："不要这些红框中的小字"。
  // 背景：真实扫描件的命中分片是连成一行的 OCR 噪声（重复人名、"第 1 页"
  // 之类的页码标记混进正文），字号却最小（12.5px）、颜色最浅，结果是
  // "最不干净的内容占了最显眼的位置"，反而盖住了真正有用的
  // 文件名 + 匹配度 + 「预览」入口。
  //
  // 说明：原型（knowledge-base-prototype.html:1500/2056、样式 :315）里
  // 结果卡本来有这一行，这次是**按用户明确要求**去掉的。要恢复：把被删掉的
  // 那一行摘要（含关键词高亮）加回 ``resultCard`` 的返回值里，高亮函数见本文件
  // git 历史。后端返回的 text / ordinal 字段**没有动**，
  // 「准确 / 不准确」反馈仍按真实分片序号落库。

  function resultCard(item) {
    const icon = TYPE_ICON[item.file_type] || "▫";
    const filenameLabels = {exact_filename: "文件名精确匹配", exact_stem: "文件名精确匹配",
      filename_token: "文件名匹配", filename_contains: "文件名包含关键词"};
    const filenameBadge = filenameLabels[item.filename_match]
      ? '<span class="tag info">' + filenameLabels[item.filename_match] + '</span>' : '';
    // 匹配度：只有检索真的给了分数才显示，不编造
    const score = item.score === undefined || item.score === null
      ? ""
      : '<span class="tag">匹配度 ' + Number(item.score).toFixed(3) + "</span>";
    // 反馈加权徽标：**只在真的有权重时显示**，并带上计数便于复盘
    // （不编造、不把"没反馈"画成 0 权重）
    const weightValue = Number(item.feedback_weight || 0);
    const counts = item.feedback_counts || {};
    const feedback = weightValue === 0
      ? ""
      : '<span class="tag" title="基于 ' +
          Number(counts.accurate || 0) + ' 条准确 / ' +
          Number(counts.inaccurate || 0) + ' 条不准确的反馈调整（有界，仅影响排序）">' +
          "反馈 " + (weightValue > 0 ? "+" : "") + weightValue.toFixed(3) +
        "</span>";
    const fileId = esc(item.file_id || item.id);
    // 「性质」显示与文件管理页**同口径**：五项之一 / 空值「待识别」，
    // 来源与依据/失败原因取 `classification_source` / `classification_note` 进 title。
    // 接口没给来源就只显示取值本身 —— 不写"已自动识别"这类前端猜测。
    const natureTitle = KB.natureTooltip(item);
    // 命中的**分片序号**：后端结果里本来就有 `ordinal`（`pipeline._shape`），
    // 前端此前写死 `chunk_ordinal: 0` —— 于是反馈永远落在"第 0 片"，
    // 同一份资料不同分片的准确/不准确无法区分（独立审计：反馈 chunk_ordinal 恒 0）。
    // 现在：**后端给了 ordinal 才用**，没给就不写 `data-chunk`（写 0 是编造序号，
    // 会让反馈 toast 谎称"分片 #0"）。
    const rawOrdinal = Number(item.ordinal);
    const hasOrdinal = item.ordinal !== undefined && item.ordinal !== null &&
      Number.isFinite(rawOrdinal) && rawOrdinal >= 0;
    // 「准确 / 不准确」反馈必须对应一次**具体检索**：后端 `query` 为空直接 400
    // （views.SearchFeedbackView："反馈必须对应一次具体检索"）。结构化列表模式
    // （只有筛选条件、没有关键词）下没有可回填的 query，所以这里**不渲染**反馈按钮，
    // 而不是渲染出来点一次 400 一次。
    const canFeedback = Boolean(state.q);
    return '<div class="result" data-file="' + fileId + '"' +
      (hasOrdinal ? ' data-chunk="' + rawOrdinal + '"' : "") + ">" +
      '<div class="name"><span class="ic-file t-' + esc(item.file_type || "txt") + '">' +
        esc(icon) + "</span> <b>" + esc(item.original_name || item.name || "") + "</b> " +
        filenameBadge + score + feedback +
      "</div>" +
      '<div class="meta">' +
        "<span>" + esc(item.directory_path || "") + "</span>" +
        "<span>" + esc(item.customer || "—") + " · " + esc(item.department || "—") + "</span>" +
        '<span class="nature-label"' + (natureTitle ? ' title="' + esc(natureTitle) + '"' : "") +
          ">" + esc(KB.natureLabel(item)) + "</span>" +
        (item.amount_wan ? "<span>" + esc(item.amount_wan) + " 万元</span>" : "") +
        (item.page ? "<span>第 " + esc(item.page) + " 页</span>" : "") +
        '<span class="grow"></span>' +
        // 两个常驻控件**一个都不能删**。反馈已接入持久化端点
        // （POST /api/v1/knowledge/search-feedback/），可点、会改判、有审计。
        '<button class="quick-chip" data-preview="' + fileId + '">预览</button>' +
        (canFeedback
          ? '<button class="quick-chip" data-feedback="accurate" ' +
              'title="标记这次命中是准确的（可改判）">准确</button>' +
            '<button class="quick-chip" data-feedback="inaccurate" ' +
              'title="标记这次命中不准确（可改判）">不准确</button>'
          : "") +
      "</div>" +
    "</div>";
  }

  function activeTags() {
    const tags = [];
    if (state.department) tags.push("部门：" + state.department);
    // 客户此前**没进**"已生效条件"：只选了客户时界面显示"输入关键词或选一个筛选条件"，
    // 看起来筛选没生效（后端其实生效了）。条件生效必须可见。
    if (state.customer) tags.push("客户：" + state.customer);
    // 「待识别」是哨兵值，直接显示会露出 `__unidentified__` —— 标签必须是中文口径
    if (state.nature) {
      tags.push("性质：" + (state.nature === KB.NATURE_UNIDENTIFIED
        ? KB.NATURE_PENDING
        : state.nature));
    }
    if (state.amountMin) tags.push("金额 ≥ " + state.amountMin + " 万");
    $("active-tags").innerHTML = tags.map(function (text) {
      return '<span class="tag info">' + esc(text) + "</span>";
    }).join(" ");
  }

  function showEmpty(text) {
    $("search-empty").innerHTML =
      '<div class="empty-state" style="text-align:center;padding:30px 0">' +
      '<p class="muted">' + esc(text) + "</p></div>";
  }

  /** 「性质筛选」的独立提示区：选中「待识别」时说明它到底生效没有。
   *
   * 为什么单独一块：不能覆盖 `#result-count`（那是份数）也不能覆盖
   * `#search-empty`（那是检索结论）。筛选口径与结果显示**必须分开**。
   */
  function showNatureNote(message, kind) {
    KB.notice("search-nature-note", message || "", kind || "warn");
  }

  /** 失败原因：优先服务端 `detail`，其次**字段级错误**，最后才是通用兜底。
   *
   * 为什么不能只取 `detail`（处置清单 OP-21）：金额下限这类校验返回的是字段级对象
   * （`{"amount_min": "金额下限不能为负数"}`），旧实现取不到 detail 就显示"请求失败"，
   * 用户根本不知道是哪一项填错了。服务端现在会给这类错误补 `detail`，这里同时保留
   * 字段级兜底 —— 两层都拿不到才退化成通用文案（绝不猜原因）。
   */
  function searchErrorText(result, fallback) {
    const body = (result && result.body) || {};
    if (typeof body.detail === "string" && body.detail.trim()) return body.detail.trim();
    const rows = body && typeof body === "object" ? Object.keys(body) : [];
    const parts = rows.filter(function (key) {
      return key !== "code" && key !== "detail";
    }).map(function (key) {
      const value = body[key];
      const text = Array.isArray(value) ? value.join("；") : String(value);
      return (key === "non_field_errors" ? "" : key + "：") + text;
    });
    if (parts.length) return parts.join("；");
    if (result && result.network) return "网络请求失败，请检查网络后重试";
    return fallback || "请求失败";
  }

  // -------------------------------------------------------- 两路检索 ----

  /** 当前筛选条件（不含 page）：分页与"重新应用筛选"共用同一份条件拼装。 */
  function structuredQuery() {
    const params = new URLSearchParams();
    if (state.q) {
      params.set("q", state.q);
      params.set("mode", state.mode === "semantic" ? "hybrid" : state.mode);
    }
    if (state.department) params.set("department", state.department);
    if (state.nature) params.set("nature", state.nature);
    if (state.customer) params.set("customer", state.customer);
    if (state.amountMin) params.set("amount_min", state.amountMin);
    params.set("page_size", String(PAGE_SIZE));   // 与本页 10 条/页一致
    return params.toString();
  }

  /** hybrid：走原**混合检索**接口（稠密+关键词+重排）。
   *
   * 结构化条件（部门/客户/性质/金额）**先限制可读文件候选**再送检索，
   * 而不是取回 top50 在前端过滤——后者会漏掉真实命中还谎称全量。
   * 空条件**绝不调用模型**（否则只是白白花钱且结果无意义）。
   */
  async function hybridSearch() {
    const hasStructured = Boolean(state.department || state.customer ||
      state.nature || state.amountMin);
    if (!state.q) {
      // 只有结构化条件、没有关键词：不需要模型，直接列文件
      await structuredSearch();
      return;
    }
    // 票据：与结构化列表**共用同一个计数器**，谁后发谁作废前面的响应。
    const ticket = ++searchTicket;
    // **只提交条件**，不枚举候选：候选收敛在服务端做（无分页、不按 q 预过滤）。
    // 前端先取 200 条会漏掉"语义相关但不含原词"的资料，超过 200 份还会被截断。
    const payload = { q: state.q, limit: PAGE_SIZE };
    if (state.department) payload.department = state.department;
    if (state.customer) payload.customer = state.customer;
    if (state.nature) payload.nature = state.nature;
    if (state.amountMin) payload.amount_min = Number(state.amountMin);
    const result = await KB.api("/api/v1/knowledge/search/", {
      method: "POST", json: payload,
    });
    // 慢响应后到：票据不再是当前号 → **直接丢弃**，绝不覆盖更新的结果。
    if (ticket !== searchTicket) return;
    if (!result.ok) {
      // **失败不是 0 条**：服务端 detail 原样显示，性质说明留空（不许说成"没有资料"）
      lastResultCount = null;
      $("result-count").textContent = "检索失败";
      showEmpty(searchErrorText(result, "检索请求失败"));
      $("search-results").innerHTML = "";
      $("search-pager").style.display = "none";
      return;
    }
    const body = result.body || {};
    const items = body.results || [];
    const report = body.report || {};
    lastResultCount = items.length;
    // 如实说明：只展示了前 N 条，不是全量命中
    const eligible = report.eligible_files;
    $("result-count").textContent = "找到 " + items.length + " 条（最多显示 " + PAGE_SIZE +
      " 条" + (typeof eligible === "number" ? "；符合筛选的资料 " + eligible + " 份" : "") + "）";
    activeTags();
    // 后端 `ordinal` 是真实分片序号；有就带上，没有就不写 `data-chunk`。
    $("search-results").innerHTML = items.map(resultCard).join("");
    if (report.degraded) {
      showEmpty("注意：本次为降级结果——" +
        (report.degraded_reason || "语义检索不可用，已回退到关键字匹配") +
        "。这不代表完整的语义检索结果。");
    } else if (!items.length) {
      showEmpty("没有命中，试试放宽条件或换个词。");
    } else {
      $("search-empty").innerHTML = "";
    }
    // 混合检索是 top-N，没有分页条（保持原型行为）；同时作废"下一页"的夹紧依据。
    searchTotalPages = 1;
    $("search-pager").style.display = "none";
  }

  async function structuredSearch() {
    const ticket = ++searchTicket;
    const result = await KB.api("/api/v1/knowledge/files/?" + structuredQuery() +
      "&page=" + state.page);
    // 慢响应后到（含"点了清空之后才回来"）：直接丢弃，不再画回结果区。
    if (ticket !== searchTicket) return;
    const active = Boolean(state.q || state.department || state.customer ||
      state.nature || state.amountMin);
    if (!result.ok) {
      // **失败不是 0 条**：服务端 detail 原样显示（无权限也走这里），
      // 性质说明一并留空 —— 不能把"查询失败"渲染成"没有待识别的资料"。
      lastResultCount = null;
      $("result-count").textContent = "查询失败";
      showEmpty(searchErrorText(result, "请求失败"));
      $("search-results").innerHTML = "";
      // **分页条不许藏**：页码越界（后端 404）时把它藏起来，用户连"上一页"都点不到，
      // 只能靠猜 —— 这是本次审计确认的死局。这里保留分页条并如实说明原因。
      searchTotalPages = Math.max(1, Math.min(searchTotalPages, state.page));
      state.page = searchTotalPages;
      $("search-page-info").textContent = "第 " + state.page + " / " + searchTotalPages +
        " 页（本页请求失败，可用「上一页」返回）";
      $("search-prev").disabled = state.page <= 1;
      $("search-next").disabled = false;
      $("search-pager").style.display = "flex";
      return;
    }
    const body = result.body || {};
    const rows = body.results || body;
    const total = typeof body.count === "number" ? body.count : rows.length;
    lastResultCount = total;
    $("result-count").textContent = active
      ? "找到 " + total + " 条"
      : "输入关键词或选一个筛选条件";
    activeTags();

    if (!rows.length) {
      $("search-results").innerHTML = "";
      showEmpty(active ? "没有命中，试试放宽条件或换个词。" : "还没有可检索的资料。");
      $("search-pager").style.display = "none";
      return;
    }
    $("search-empty").innerHTML = "";
    // 结构化列表**没有分片序号**（列表响应就没有这个字段）：结果卡因此不写
    // `data-chunk`，反馈文案也不会宣称"分片 #0"。
    $("search-results").innerHTML = rows.map(function (file) {
      return resultCard({
        id: file.id,
        original_name: file.original_name,
        file_type: file.file_type,
        directory_path: file.directory_path,
        customer: file.customer,
        department: file.department,
        // 性质三件套原样透传：取值 / 来源 / 依据或失败原因（展示口径见 resultCard）
        classification: file.classification,
        classification_label: file.classification_label,
        classification_source: file.classification_source,
        classification_note: file.classification_note,
        amount_wan: file.amount_wan,
        snippet: file.summary || "",
      });
    }).join("");
    const pages = Math.max(1, Math.ceil(total / PAGE_SIZE));
    const current = Math.min(state.page, pages);
    // 记住真实总页数：它就是「下一页」夹紧页码、越界按钮禁用的唯一依据。
    searchTotalPages = pages;
    state.page = current;
    $("search-page-info").textContent = "共 " + total + " 条 · 第 " + current + " / " + pages + " 页";
    $("search-prev").disabled = current <= 1;
    $("search-next").disabled = current >= pages;
    $("search-pager").style.display = "flex";
  }

  /** 无查询也无条件：按原型保持结果区**空白**（不是列出全部资料）。 */
  function renderBlank() {
    lastResultCount = null;
    $("result-count").textContent = "输入关键词或选一个筛选条件";
    $("search-results").innerHTML = "";
    $("search-empty").innerHTML = "";
    activeTags();
    $("search-pager").style.display = "none";
  }

  /** 预览：真正打开弹窗并读真实分段。 */
  async function previewFile(id) {
    // **统一入口**：与资料列表/任务列表共用同一个原件预览实现。
    // 旧实现只把 `chunks.text` 拼成段落 —— 那不是原件预览
    // （PDF 原页/表格/水印位置全丢，水印文字混进正文）。
    return KB.previewFile(id);
  }

  /** 提交一次"准确/不准确"反馈（同一命中重复提交 = 改判）。 */
  function sendFeedback(button) {
    const verdict = button.getAttribute("data-feedback");
    // file id 在**结果卡**上（`<div class="result" data-file="…">`），不在按钮上
    const card = button.closest("[data-file]");
    const fileId = card ? card.getAttribute("data-file") : "";
    // 分片序号只取结果卡上的**真实值**：卡上没有 `data-chunk` 就说明后端没给序号
    // （结构化列表本来就没有分片概念）。这时必须**显式声明资料级反馈**
    // （scope=file）—— 提交 chunk_ordinal=0 等于把资料级评价登记成"第 0 片"，
    // 数据语义是错的（Codex 审查）。
    const chunkAttr = card ? card.getAttribute("data-chunk") : null;
    const hasChunkOrdinal = chunkAttr !== null && Number.isFinite(Number(chunkAttr));
    const payload = {
      verdict: verdict,
      query: state.q || "",
      scope: hasChunkOrdinal ? "chunk" : "file",
    };
    if (hasChunkOrdinal) payload.chunk_ordinal = Number(chunkAttr);
    if (fileId) payload.file_id = Number(fileId);
    // 把当前结构化条件一并留存，便于复盘"为什么判它不准确"
    if (state.department) payload.department = state.department;
    if (state.customer) payload.customer = state.customer;
    if (state.nature) payload.nature = state.nature;
    if (state.amountMin) payload.amount_min = state.amountMin;

    button.disabled = true;
    // **必须检查 result.ok**（Codex 独立审查发现）：`KB.api` 对 4xx/5xx 也返回
    // 已兑现的 Promise，旧代码在 `.then()` 里直接说"反馈已记录" —— 403/500/断网
    // 全都显示成功，而数据库里什么都没写。现在按真实状态分支，并把服务端 detail
    // 原样带出来；失败时按钮恢复可点（可重试），绝不谎报已记录。
    KB.api("/api/v1/knowledge/search-feedback/", {
      method: "POST", json: payload,
    }).then(function (result) {
      button.disabled = false;
      if (!result.ok) {
        const detail = (result.body && (result.body.detail || result.body.raw)) ||
          (result.network ? "网络请求失败" : "HTTP " + result.status);
        KB.toast("反馈**没有**记录成功：" + detail + "。可重试。");
        button.textContent = verdict === "accurate" ? "重试 · 准确" : "重试 · 不准确";
        return;
      }
      button.textContent = verdict === "accurate" ? "已标准确" : "已标不准确";
      // 反馈是**检索时实时聚合**的（有界加权），所以这里不假装"列表立刻变了"：
      // 明确告诉用户什么时候会看到效果，以及效果只影响排序。
      // 分片序号**只在后端真的给了序号时**才说：结构化列表没有序号，
      // 硬写"分片 #0"是把编造的序号当事实告诉用户。
      KB.toast("反馈已记录" +
        (hasChunkOrdinal
          ? "（分片 #" + payload.chunk_ordinal + "）"
          : "（资料级：这次结果没有分片序号，反馈按**整份资料**计入加权）") +
        "：下次检索会按反馈加权排序，有界且只影响排序");
    }).catch(function (error) {
      // KB.api 内部已经兜住网络异常；这里只兜"我们自己的代码抛错"，也要如实说
      button.disabled = false;
      button.textContent = verdict === "accurate" ? "重试 · 准确" : "重试 · 不准确";
      KB.toast("反馈提交失败，未记录：" + (error && error.message ? error.message : "未知错误"));
    });
  }

  function bindResultControls() {
    document.querySelectorAll("#search-results [data-preview]").forEach(function (button) {
      button.addEventListener("click", function () {
        previewFile(button.getAttribute("data-preview"));
      });
    });
    document.querySelectorAll("#search-results [data-feedback]").forEach(function (button) {
      button.addEventListener("click", function () {
        sendFeedback(button);
      });
    });
  }

  /** 是否带了任一有效条件（决定"空搜索"）。 */
  function hasAnyCondition() {
    return Boolean(
      state.q || state.department || state.customer || state.nature || state.amountMin
    );
  }

  /* ------------------------------------------------------ 地址栏同步 ----
   *
   * 处置清单 OP-24：条件全都写进了 state 与 DOM，却**从不写回地址栏** ——
   * 于是「清空」之后刷新，`DOMContentLoaded` 又从 `?q=…&nature=…` 把旧条件
   * 读回来，用户看到的结论就是"清空没生效"（而且清空后的页面没法分享）。
   *
   * 用 `replaceState`（不是 pushState）：检索条件变化**不应该**在浏览历史里
   * 堆一串条目，否则用户按一次"后退"退不出去，只会在旧条件之间来回跳。
   */
  const URL_KEYS = ["q", "mode", "department", "nature", "customer", "amount_min"];

  /** 把当前条件写成查询串（保持原有参数顺序，先删掉我们管的这几个键）。 */
  function searchQueryString() {
    const params = new URLSearchParams(location.search);
    URL_KEYS.forEach(function (key) { params.delete(key); });
    if (state.q) {
      params.set("q", state.q);
      params.set("mode", state.mode === "semantic" ? "hybrid" : state.mode);
    }
    if (state.department) params.set("department", state.department);
    if (state.nature) params.set("nature", state.nature);
    if (state.customer) params.set("customer", state.customer);
    if (state.amountMin) params.set("amount_min", state.amountMin);
    return params.toString();
  }

  /** 写地址栏。**只写条件，不写 page**。
   *
   * 为什么连结构化列表的页码也不写：
   *   * `hybrid`（有 q）走 top-N 检索、**没有分页**；`filename/content` 走结构化
   *     列表、有分页。但 `/api/v1/knowledge/files/` **忽略 `mode` 参数**，
   *     所以 `?mode=filename&page=3` 这个地址被分享/刷新时会落到 hybrid 通路并
   *     忽略 page —— 地址栏写着第 3 页、界面却是第一页（一个会撒谎的参数）。
   *   * 本页本来就不从 URL 回填 `page`，写进去没有任何作用，只会误导。
   * 所以地址栏同步的范围就锁定在"用户能据以复现这次检索的条件"上。
   */
  function syncSearchUrl() {
    // **必须显式写 `window.history`**：本文件里有一个局部 `function history()`
    // （读搜索历史词表），裸 `history` 会被解析成那个函数 →
    // `history.replaceState is not a function`（Codex 独立浏览器在清空/初始化时抓到 3 次）。
    // 浏览器 History API 一律走 `window.history`，搜索历史语义不动。
    if (!window.history || typeof window.history.replaceState !== "function") return;
    const query = searchQueryString();
    window.history.replaceState(null, "", location.pathname + (query ? "?" + query : ""));
  }

  /** 清空条件后地址栏必须**真的干净**（可刷新、可分享，不残留旧条件）。 */
  function clearSearchUrl() {
    if (!window.history || typeof window.history.replaceState !== "function") return;
    window.history.replaceState(null, "", location.pathname);
  }

  async function runSearch(resetPage) {
    if (resetPage) state.page = 1;
    // **统一空条件判定**：三种模式下"没有关键词也没有任何筛选"都保持空白。
    // 此前只有 hybridSearch 判空，filename/content 空搜索会把全部资料列出来，
    // 偏离原型（原型结果区初始就是空的）。
    if (!hasAnyCondition()) {
      // 没有条件 = 没有可分享的检索：地址栏同步清干净（OP-24）
      clearSearchUrl();
      showNatureNote("");
      renderBlank();
      return;
    }
    // 条件变化先落地址栏（可刷新/可分享），再发请求
    syncSearchUrl();
    if (state.mode === "hybrid") {
      await hybridSearch();
    } else {
      await structuredSearch();
    }
    // 性质筛选的说明按**真实结果**给（条数由各检索通路写进 lastResultCount）
    showNatureNote(pendingNatureNote(), "info");
    bindResultControls();
    if (state.q) remember(state.q);
    renderHot();
  }

  /** 上一条检索结果的真实条数；null = 这次没拿到结果（失败/被丢弃）。 */
  let lastResultCount = null;

  /** 性质筛选的说明：只按**真实结果**说话。
   *
   * 0 条 = "没有待识别（未识别性质）的资料"；接口失败时 lastResultCount 为 null，
   * 此时**什么都不说**（失败原因由结果区的错误文案负责，不能把错误说成 0 条）。
   */
  function pendingNatureNote() {
    if (state.nature !== KB.NATURE_UNIDENTIFIED) return "";
    if (lastResultCount === null) return "";
    return natureResultNote(lastResultCount);
  }

  // ------------------------------------------------------------ 绑定 ----

  function bind() {
    /** 两个按钮都读取**全部当前条件**（此前"搜索"只读关键词，会丢筛选）。 */
    function collectFilters() {
      state.mode = $("mode").value;
      state.q = $("q").value.trim();
      state.department = $("department").value;
      state.customer = ($("customer") || {}).value || state.customer;
      state.nature = $("nature").value;
      state.amountMin = $("amount_min").value.trim();
    }
    $("search-run").addEventListener("click", function () {
      collectFilters();
      runSearch(true);
    });
    $("q").addEventListener("keydown", function (event) {
      // 输入法组合中的 Enter 是选词，不是"开始检索"（Codex 审查实测）
      if (event.isComposing || event.keyCode === 229) return;
      if (event.key === "Enter") {
        event.preventDefault();
        collectFilters();
        runSearch(true);
      }
    });
    $("search-apply").addEventListener("click", function () {
      collectFilters();
      runSearch(true);
    });
    $("mode").addEventListener("change", function () {
      state.mode = $("mode").value;
      // **只要有任何条件就重跑**（处置清单 OP-22）：旧实现只在有关键词时重跑，
      // 于是"只设了部门/性质/金额"的用户切换模式后**不发请求也没有提示**，
      // 会以为切换已经生效 —— 实际上结果区还是上一次的。
      if (hasAnyCondition()) {
        collectFilters();
        runSearch(true);
        return;
      }
      // 一个条件都没有时保持空白，但明确说明"模式已切、还没生效"
      const picked = $("mode").selectedOptions[0];
      showNatureNote("已切换到「" + ((picked && picked.textContent) || state.mode) +
        "」；输入关键词或选一个筛选条件后会自动生效。", "info");
    });
    $("search-reset").addEventListener("click", function () {
      // **作废所有在途请求**：不递增票据的话，清空前发出的慢响应回来还会把旧结果
      // 画回结果区（审计复现："清空后旧搜索结果又回来了"）。
      searchTicket += 1;
      $("q").value = "";
      $("mode").value = "hybrid";
      $("department").value = "";
      // 客户下拉现在真实存在（search.html），清空也要把它复位，否则会残留筛选
      if ($("customer")) $("customer").value = "";
      $("nature").value = "";
      $("amount_min").value = "";
      state.q = ""; state.mode = "hybrid"; state.department = "";
      // 页码与分页依据一起复位：下一次检索重新从第 1 页算
      state.customer = ""; state.nature = ""; state.amountMin = ""; state.page = 1;
      searchTotalPages = 1;
      // 「待识别」说明条一并清掉（条件已复位，说明不再适用）
      showNatureNote("");
      $("search-results").innerHTML = "";
      $("search-empty").innerHTML = "";
      $("result-count").textContent = "输入关键词或选一个筛选条件";
      activeTags();
      $("search-pager").style.display = "none";
      // **地址栏同步清空**（OP-24）：不清的话刷新会把刚清掉的条件原样恢复，
      // 用户看到的是"清空没生效"。
      clearSearchUrl();
    });
    $("search-prev").addEventListener("click", function () {
      if (state.page > 1) { state.page -= 1; syncSearchUrl(); runSearch(false); }
    });
    $("search-next").addEventListener("click", function () {
      // **先用已知总数夹紧页码**：连续点「下一页」以前会把 page 推到总页数之外，
      // 后端 404 → 分页条被隐藏 → 用户没有任何办法回到有效页。
      const next = Math.min(state.page + 1, searchTotalPages);
      if (next === state.page) return;
      state.page = next;
      syncSearchUrl();
      runSearch(false);
    });
  }

  document.addEventListener("DOMContentLoaded", async function () {
    bind();
    renderHot();
    await loadFacets();
    // 支持从别的页面带条件进来：/app/search/?q=合同&nature=合同
    const params = new URLSearchParams(location.search);
    if (params.get("q")) { state.q = params.get("q"); $("q").value = state.q; }
    if (params.get("mode")) { state.mode = params.get("mode"); $("mode").value = state.mode; }
    if (params.get("department")) {
      state.department = params.get("department");
      $("department").value = state.department;
    }
    if (params.get("nature")) {
      // 支持 `?nature=合同` 与 `?nature=__unidentified__`（待识别）。
      // 「待识别」选项由 loadFacets 注入，因此这里直接赋值即可命中。
      state.nature = params.get("nature");
      $("nature").value = state.nature;
      if ($("nature").value !== state.nature) {
        // 服务端词表里没有这个取值（历史值/词表外的值）：补一个选项，
        // 避免筛选条件被**悄悄丢掉**（赋值失败时 select.value 会停在空串）。
        $("nature").insertAdjacentHTML("beforeend",
          '<option value="' + esc(state.nature) + '" selected>' + esc(state.nature) +
          "</option>");
      }
    }
    if (params.get("customer")) {
      state.customer = params.get("customer");
      const customerSelect = $("customer");
      if (customerSelect) {
        customerSelect.value = state.customer;
        if (!customerSelect.value) {
          // 服务端还没返回该客户时，补一个选项，避免条件被悄悄丢掉
          customerSelect.insertAdjacentHTML("beforeend",
            '<option value="' + esc(state.customer) + '" selected>' +
            esc(state.customer) + "</option>");
        }
      }
    }
    if (params.get("amount_min")) {
      state.amountMin = params.get("amount_min");
      $("amount_min").value = state.amountMin;
    }
    // 默认（无查询无条件）保持空白，不去列全部资料
    await runSearch(false);
  });
})();
