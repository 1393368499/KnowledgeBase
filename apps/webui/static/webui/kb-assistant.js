/* 首页（AI 会话）—— 结构与原型最后生效的 viewHome / welcomeHtml / composerHtml 一致。
 *
 * 取值全部来自真实后端；**没有**演示数据、不伪造成功。
 * 复用并保留：操作卡 + 三次独立确认 + 执行窗口 + 来源勾选 + 文档编辑/保存/MD+HTML 下载。
 *
 * 与原型的两处**有意差异**（需求要求）：
 *   1. 空会话时**停在顶部**（原型无脑滚到底是它自身缺陷）；只有存在消息时才滚到底。
 *   2. 原型把会话存在 localStorage；这里走服务端 `/planner/conversations/`。
 */
(function () {
  "use strict";

  const KB = window.KB;
  const esc = KB.esc;
  const mono = KB.mono;
  const $ = function (id) { return document.getElementById(id); };

  async function assistantApi(url, options) {
    const opts = options || {};
    if (!opts.method || opts.method.toUpperCase() === "GET") return KB.api(url, opts);
    let label = "正在提交操作";
    if (url.includes("/chat/")) label = "正在分析需求、检索资料并生成回复";
    else if (url.includes("/execute/")) label = "正在执行操作";
    else if (url.includes("/confirm/")) label = "正在确认操作";
    else if (url.includes("/abandon/")) label = "正在放弃待办";
    else if (url.includes("/sources/")) label = "正在保存参考文件选择";
    else if (url.includes("/documents/")) label = opts.method === "DELETE" ? "正在删除文档" :
      (/\/documents\/$/.test(url) ? "正在检索资料并生成文档" : "正在保存文档");
    else if (url.includes("/upload/")) label = "正在上传资料";
    else if (url.includes("/templates/")) label = opts.method === "DELETE" ? "正在删除模板" : "正在上传模板";
    let host = url.includes("/documents/") ? $("doc-action-progress") : $("chat-action-progress");
    const dialogs = document.querySelectorAll(".mask .modal");
    if (dialogs.length) {
      const dialog = dialogs[dialogs.length - 1];
      let local = dialog.querySelector(".modal-request-progress");
      if (!local) {
        local = document.createElement("div");
        local.className = "modal-request-progress";
        local.setAttribute("aria-live", "polite");
        dialog.querySelector(".modal-head").insertAdjacentElement("afterend", local);
      }
      host = local;
    }
    const task = KB.progress.start(host, label);
    try {
      const response = await KB.api(url, opts);
      task.finish(response.ok);
      const file = response.body && response.body.file;
      if (response.ok && file && ["pending", "running"].includes(file.parse_status)) {
        const host = $("chat-action-progress");
        if (host && !host.querySelector('[data-file-progress="' + file.id + '"]')) {
          host.insertAdjacentHTML("beforeend", '<div class="tiny">' + esc(file.original_name || "上传资料") +
            KB.progress.widget("file", file.id, file.progress) + '</div>');
          KB.progress.mount();
        }
      }
      return response;
    } catch (error) { task.finish(false); throw error; }
  }

  document.addEventListener("kb:operation-progress", function (event) {
    const row = event.detail;
    const cards = Array.from(document.querySelectorAll('.op-card[data-op="' + row.id + '"]'));
    if (row.status === "executing") {
      cards.forEach(function (card) {
        card.querySelector(".op-state").textContent = "执行中";
        card.classList.remove("state-armed");
        card.classList.add("state-executing");
        card.querySelectorAll(".op-actions button").forEach(function (button) { button.disabled = true; });
      });
      return;
    }
    if (row.progress.state === "queued" && !KB.progress.isOperationRequest(row.id)) {
      cards.forEach(function (card) {
        card.querySelector(".op-state").textContent = OP_STATE_TEXT[row.status] || row.status;
        card.querySelectorAll(".op-actions button").forEach(function (button) { button.disabled = false; });
      });
    }
    if (row.status === "executed" && KB.progress.isOperationRequest(row.id)) return;
    if (row.status === "executed" && cards.length) {
      cards.forEach(function (card) { card.remove(); });
      offerResultEntry(row.result || {});
    } else if (row.status === "failed") {
      cards.forEach(function (card) {
        const state = card.querySelector(".op-state");
        if (state) state.textContent = "执行失败";
        const actions = card.querySelector(".op-actions");
        if (actions) actions.remove();
        KB.notice(card.querySelector(".proposal-notice"), row.error || "执行失败", "err");
      });
    }
  });

  //: 参数中文名（与工具弹窗里的表一致；找不到就用英文键名，不编造）
  const PARAM_LABELS_FOR_FORM = {
    library: "信息库", file_id: "资料 ID", collection: "索引代次（集合名）",
    profile: "嵌入配置", query: "关键词", customer: "客户", department: "部门",
    amount: "金额", status: "状态", action: "事件类型", limit: "条数",
    to_generation: "回退到第几代", title: "标题", topic: "主题",
    sources: "来源资料 ID（逗号分隔）", document_id: "文档 ID", code: "信息库代码",
    name: "信息库名称", level: "级别", task_id: "任务 ID", format: "导出格式",
  };

  let conversationId = null;
  let currentDocId = null;
// 当前打开草稿的模板类别（"official" = 公文模板）。只用于预览排版口径，
// 不参与保存：文档内容与模板的绑定关系由服务端 template_id 固化。
let currentDocTemplateCategory = "";
  // 「打开文档」的序号票：面板自动打开最新一份时，若期间已经有人**显式**打开过
  // 某一份草稿（例如操作执行完 offerResultEntry 打开刚生成的那份），
  // 自动打开就必须放弃，否则会把刚生成/刚打开的文档顶掉。
  let docOpenTicket = 0;
  const chosenSources = new Set();

  // ---- 「知识库调用选择」：这次提问要检索哪些信息库 -------------------------
  //
  // 空数组 = **不限定**（检索全部有权查看的库）= 默认值。刻意与"显式传空数组"
  // 分开：后端的 `library_codes: []` 是 400（空数组不是"全库"的同义词），
  // 所以这里为空时**根本不发这个字段**，把"全库"这一种含义只留给"用户没选"。
  let scopeCodes = [];      // string[]：用户勾选的库 code（顺序 = 勾选顺序）
  let scopeRows = null;     // 最近一次成功读到的库列表；null = 还没读到 / 读失败
  let scopeBusy = false;    // 弹窗互斥（避免连点开出两个弹窗）

  // ---- 文档模板 ------------------------------------------------------------
  //
  // `null` 与 `[]` 是**两件事**：null = 列表没读到（要如实说"读取失败"），
  // [] = 真的还没有模板。混在一起会把一次请求失败说成"还没有模板"。
  let templateRows = null;
  let templateBusy = false;

  /** 把 code 映射成展示名；**读不到名字就显示 code 本身**（不编造中文名）。 */
  function scopeNames(codes) {
    const byCode = {};
    (scopeRows || []).forEach(function (row) { byCode[row.code] = row; });
    return (codes || []).map(function (code) {
      const row = byCode[code];
      return { code: code, name: row ? (row.name || "") : "" };
    });
  }

  function renderScopeBar() {
    const bar = $("kb-scope-bar");
    const chips = $("kb-scope-chips");
    if (!bar || !chips) return;
    if (!scopeCodes.length) {
      bar.hidden = true;
      chips.innerHTML = "";
      return;
    }
    bar.hidden = false;
    chips.innerHTML = scopeNames(scopeCodes).map(function (item) {
      return '<span class="kb-scope-chip" title="信息库 code（同名库靠它区分）">' +
        esc(item.name ? item.name + "（" + item.code + "）" : item.code) + "</span>";
    }).join("");
  }

  async function loadScopeRows(force) {
    if (scopeRows && !force) return scopeRows;
    const result = await assistantApi("/api/v1/knowledge/libraries/?active=1");
    if (!result.ok) {
      scopeRows = null;
      return null;
    }
    scopeRows = (result.body && result.body.results) || [];
    return scopeRows;
  }

  /** 知识库多选弹窗：勾选 / 「用全部库」/ 取消。
   *
   * 结算口径照抄 `KB.modalChoice` 的教训：Esc / 遮罩 / 取消 / 确定**只结算一次**，
   * 绝不能留下悬挂的 Promise —— 悬挂会让互斥标志永远为 true，用户看到的是
   * "点了没反应"（kb-common.js 里为这个坑单独写过注释）。
   */
  function pickScopeModal(rows, selected) {
    return new Promise(function (resolve) {
      let settled = false;
      const settle = function (value) {
        if (settled) return;
        settled = true;
        resolve(value);
      };
      const options = rows.length
        ? rows.map(function (row) {
            const on = selected.indexOf(row.code) >= 0 ? " checked" : "";
            return '<label class="kb-scope-item">' +
              '<input type="checkbox" data-scope-code="' + esc(row.code) + '"' + on + ">" +
              "<span>" + esc(row.name || row.code) +
              ' <span class="muted tiny">（' + esc(row.code) + "）</span></span></label>";
          }).join("")
        : '<div class="tiny" style="color:#a33">没有读到可用的信息库。' +
          "这<strong>不等于</strong>「你没有库」—— 可能只是这次请求失败，请关掉重试。</div>";
      const mask = KB.modal(
        '<div class="modal-head"><b>选择这次要检索的信息库</b></div>' +
        '<div class="modal-body paper">' +
        '<div class="tiny muted">勾选后，这次提问<strong>只</strong>依据这些库里的资料回答；' +
        "一个都不勾 = 检索你有权查看的全部信息库。这里只影响检索范围，" +
        "不会改动任何资料。</div>" +
        '<div class="kb-scope-list">' + options + "</div></div>" +
        '<div class="modal-foot">' +
        '<button class="btn ghost sm" data-cancel="1">取消</button>' +
        '<button class="btn ghost sm" data-scope-all="1">用全部库</button>' +
        '<button class="btn primary sm" data-ok="1">确定</button></div>',
        "520px",
        { onClose: function () { settle(null); } }
      );
      const cancel = mask.querySelector("[data-cancel]");
      if (cancel) {
        cancel.addEventListener("click", function () { settle(null); mask.remove(); });
      }
      const all = mask.querySelector("[data-scope-all]");
      if (all) {
        all.addEventListener("click", function () { settle([]); mask.remove(); });
      }
      const okay = mask.querySelector("[data-ok]");
      if (okay) {
        okay.addEventListener("click", function () {
          const picked = Array.prototype.slice
            .call(mask.querySelectorAll("[data-scope-code]"))
            .filter(function (box) { return box.checked; })
            .map(function (box) { return box.getAttribute("data-scope-code"); });
          settle(picked);
          mask.remove();
        });
      }
    });
  }

  async function openKnowledgeScope() {
    if (scopeBusy) return;
    scopeBusy = true;
    try {
      // **每次打开都重读**：库的可见性会随授权变化，用缓存会让用户勾到一个已经没权限的库
      const rows = await loadScopeRows(true);
      if (!rows) {
        KB.toast("信息库列表读取失败，请稍后重试（当前选择未改动）", "err");
        return;
      }
      const picked = await pickScopeModal(rows, scopeCodes);
      if (picked === null) return;            // 取消 = 保持原样
      if (picked.length > 20) {
        // 后端上限 20：先在本地截断并**如实告知**，不静默丢弃用户的选择
        KB.toast("最多只能限定 20 个信息库，已保留前 20 个", "warn");
      }
      scopeCodes = picked.slice(0, 20);
      renderScopeBar();
    } finally {
      scopeBusy = false;
    }
  }

  /** 「本次检索范围」的持久回执（来自助手消息 payload，刷新/翻历史都还在）。
   *
   * 只在**真的限定了范围**或**发生了回落**时出现：默认的全库检索每次顶一条
   * "检索了全部库"纯属噪声。回落（勾的库没权限）必须显示，否则用户会以为
   * 自己勾的库生效了。
   */
  function scopeReceiptHtml(payload) {
    const rows = (payload && payload.searched_libraries) || [];
    const requested = (payload && payload.requested_codes) || [];
    const note = (payload && payload.scope_note) || "";
    if (!note && !requested.length) return "";
    const parts = [];
    if (note) {
      parts.push('<div class="notice-bar warn" style="margin:8px 0">' + esc(note) + "</div>");
    }
    if (requested.length) {
      parts.push('<div class="tiny muted" style="margin:2px 0 6px">本次检索范围：' +
        (rows.length
          ? esc(rows.map(function (row) { return row.name || row.code; }).join("、"))
          : "（没有可用的信息库）") + "</div>");
    }
    return parts.join("");
  }

  // ---------------------------------------------------------- 文档模板 ----

  function templateOptionHtml(selectedId) {
    const rows = templateRows || [];
    const parts = ['<option value="">不使用模板（按通用文档格式撰写）</option>'];
    let hit = false;
    rows.forEach(function (row) {
      const sel = Number(selectedId) === Number(row.id);
      if (sel) hit = true;
      parts.push('<option value="' + row.id + '"' + (sel ? " selected" : "") + ">" +
        esc((row.category_label ? "[" + row.category_label + "] " : "") + row.name) + "</option>");
    });
    if (selectedId && !hit) {
      // 原来选的模板已被删除：**如实显示**，绝不悄悄退回"不使用模板"
      parts.push('<option value="' + esc(String(selectedId)) + '" selected>（原选的模板已不存在，id=' +
        esc(String(selectedId)) + "）</option>");
    }
    if (!rows.length) {
      const label = templateRows === null
        ? "（模板列表读取失败，展开「文档模板」可重试）"
        : "（还没有上传文档模板）";
      parts.push('<option value="" disabled>' + esc(label) + "</option>");
    }
    return parts.join("");
  }

  /** 模板列表到位后刷新所有待确认卡上的下拉（卡片可能先于列表渲染出来）。 */
  function refreshCardTemplateSelects() {
    Array.prototype.forEach.call(document.querySelectorAll("[data-op-template]"),
      function (select) {
        const selected = select.getAttribute("data-selected-template") || "";
        select.innerHTML = templateOptionHtml(selected);
        select.setAttribute("data-prev-value", selected);
      });
  }

  function renderDocTemplateSelect() {
    const select = $("doc-template");
    if (!select) return;
    const keep = select.value;
    select.innerHTML = '<option value="">不使用模板</option>';
    (templateRows || []).forEach(function (row) {
      const option = document.createElement("option");
      option.value = String(row.id);
      option.textContent = (row.category_label ? "[" + row.category_label + "] " : "") + row.name;
      select.appendChild(option);
    });
    if (templateRows === null) {
      const option = document.createElement("option");
      option.value = "";
      option.textContent = "模板列表读取失败";
      option.disabled = true;
      select.appendChild(option);
    }
    // 保留用户原来的选择（还在列表里时才保留）
    const stillThere = Array.prototype.some.call(select.options, function (option) {
      return option.value === keep;
    });
    if (keep && stillThere) select.value = keep;
  }

  function renderTemplateList() {
    const box = $("tpl-list");
    const count = $("tpl-count");
    if (!box || templateRows === null) return;
    if (count) {
      count.textContent = templateRows.length ? templateRows.length + " 份" : "还没有模板";
    }
    if (!templateRows.length) {
      box.innerHTML = '<div class="tiny muted">还没有上传模板。上传一份后，下面的' +
        "「生成草稿」和对话里生成的待确认卡都能套用它的格式（不选则不用模板）。</div>";
      return;
    }
    box.innerHTML = "";
    templateRows.forEach(function (row) {
      const line = document.createElement("div");
      line.className = "tpl-row";
      const item = document.createElement("button");
      item.className = "tpl-item";
      item.type = "button";
      item.title = "查看这份模板的正文（" + (row.original_name || "") + "）";
      item.innerHTML = esc(row.name) + ' <span class="tpl-meta">· ' +
        esc(row.suffix || "") + " · " + esc(String(row.text_chars || 0)) + " 字</span>";
      item.addEventListener("click", function () { showTemplateBody(row.id); });
      const download = document.createElement("button");
      download.className = "doc-del";
      download.type = "button";
      download.title = "下载模板原件";
      download.textContent = "⤓";
      download.addEventListener("click", function (event) {
        event.stopPropagation();
        window.open("/api/v1/planner/templates/" + row.id + "/download/", "_blank");
      });
      const remove = document.createElement("button");
      remove.className = "doc-del";
      remove.type = "button";
      remove.title = "删除这份模板（已生成的文档不受影响，仍保留当时的模板名）";
      remove.textContent = "✕";
      remove.addEventListener("click", function (event) {
        event.stopPropagation();
        deleteTemplateById(row.id, row.name);
      });
      line.appendChild(item);
      line.appendChild(download);
      // 别人的模板不给删（后端只允许上传者本人或系统管理员）——
      // 画一个点下去必定 403 的 ✕ 属于骗用户；后端返回 can_delete=false 时不画。
      if (row.can_delete !== false) line.appendChild(remove);
      box.appendChild(line);
    });
  }

  async function loadTemplates(force) {
    if (templateRows && !force) return templateRows;
    const result = await assistantApi("/api/v1/planner/templates/");
    if (!result.ok) {
      templateRows = null;
      // 读不到 ≠ 没有模板：绝**不**渲染成"还没有上传模板"
      const list = $("tpl-list");
      if (list) {
        list.innerHTML = '<div style="color:#a33">模板列表读取失败：' +
          esc(KB.errorText(result, "请求失败")) + "（可重试；这不是「还没有模板」）</div>";
      }
      const count = $("tpl-count");
      if (count) count.textContent = "读取失败";
      // 这里**不**重绘底栏的「撰写模板」下拉：读失败不该把用户已经选好的模板清掉。
      // 待确认卡上的下拉必须重绘，好让"读失败"如实显示出来。
      refreshCardTemplateSelects();
      return null;
    }
    templateRows = (result.body && result.body.results) || [];
    renderTemplateList();
    renderDocTemplateSelect();
    refreshCardTemplateSelects();
    return templateRows;
  }

  /** 看一眼模板正文：只凭文件名挑模板等于盲选。 */
  async function showTemplateBody(id) {
    const result = await assistantApi("/api/v1/planner/templates/" + id + "/");
    if (!result.ok) {
      KB.toast("模板内容读取失败：" + KB.errorText(result, "请求失败"), "err");
      return;
    }
    const body = result.body || {};
    KB.modal(
      '<div class="modal-head"><b>' + esc(body.name || "文档模板") + "</b>" +
      '<span class="grow"></span>' +
      '<button class="btn ghost sm" data-close="1">✕</button></div>' +
      '<div class="modal-body paper">' +
      '<div class="tiny muted">上传文件名：' + esc(body.original_name || "-") + " · " +
      esc(String(body.text_chars || 0)) + " 字。模板只决定格式（段落结构、称呼、落款位置），" +
      "正文内容仍然来自检索到的资料片段。</div>" +
      '<pre style="white-space:pre-wrap;word-break:break-word;font-size:12.5px;' +
      'line-height:1.7;margin:8px 0 0">' + esc(body.content_text || "（没有存正文）") +
      "</pre></div>" +
      '<div class="modal-foot"><button class="btn ghost sm" data-close="1">关闭</button></div>',
      "680px"
    );
  }

  async function uploadTemplate(file) {
    if (!file || templateBusy) return;
    templateBusy = true;
    KB.notice($("tpl-notice"), "正在上传模板：" + file.name + "…", "warn");
    const form = new FormData();
    form.append("file", file);
    let result;
    try {
      result = await assistantApi("/api/v1/planner/templates/", { method: "POST", form: form });
    } finally {
      templateBusy = false;
    }
    if (!result.ok) {
      // **失败原因原样带出来**（"提取不到文字"这类原因用户必须看到，不能只说"上传失败"）
      const detail = KB.errorText(result, "请求失败");
      KB.notice($("tpl-notice"), "上传失败：" + detail, "err");
      KB.toast("模板上传失败：" + detail, "err");
      return;
    }
    KB.notice($("tpl-notice"), "已上传模板「" + (result.body.name || file.name) +
      "」：可在「撰写模板」里选它，或在对话里的待确认卡上选。", "ok");
    await loadTemplates(true);
  }

  async function deleteTemplateById(id, name) {
    const label = (name || "").trim() || "（未命名模板）";
    const answer = await KB.confirmDialog({
      title: "删除这份文档模板？",
      body: "将删除模板「" + label + "」及其原件。已经用它生成的文档不受影响：" +
        "那些文档仍保留当时的模板名快照，也不会回写任何来源资料。",
      targetLabel: label,
      confirmLabel: "删除模板",
    });
    if (!answer || !answer.confirmed) return;
    const result = await assistantApi("/api/v1/planner/templates/" + id + "/", { method: "DELETE" });
    if (!result.ok) {
      KB.notice($("tpl-notice"), "删除失败：" + KB.errorText(result, "请求失败"), "err");
      return;
    }
    const used = result.body && result.body.documents_referencing;
    KB.notice($("tpl-notice"),
      "已删除模板" + (used ? "；有 " + used + " 份文档用过它（它们的模板名快照保留）" : ""),
      "ok");
    await loadTemplates(true);
  }

  /** 待确认卡上改模板：**立刻持久化到这条操作上**，而不是只改界面。 */
  async function setCardTemplate(select) {
    const operationId = select.getAttribute("data-op-template");
    const card = select.closest(".op-card");
    const notice = card ? card.querySelector(".proposal-notice") : null;
    const previous = select.getAttribute("data-prev-value") || "";
    select.disabled = true;
    const result = await assistantApi(
      "/api/v1/planner/operations/" + operationId + "/template/",
      { method: "POST", json: { template_id: select.value ? Number(select.value) : null } }
    );
    select.disabled = false;
    if (!result.ok) {
      // 失败**退回原值**：绝不留下"界面显示套了模板、实际没套"的假象
      select.value = previous;
      const detail = KB.errorText(result, "请求失败");
      if (notice) notice.textContent = "模板未保存：" + detail;
      KB.toast("模板未保存：" + detail, "err");
      return;
    }
    select.setAttribute("data-prev-value", select.value);
    select.setAttribute("data-selected-template", select.value);
    if (notice) notice.textContent = (result.body && result.body.message) || "已更新模板选择";
  }

  /** 待确认卡必须明确选择至少一份参考文件。 */
  function sourcePickerHtml(op) {
    const selected = {};
    (op.source_file_ids || []).forEach(function (id) { selected[Number(id)] = true; });
    const rows = op.source_candidates || [];
    const items = rows.length
      ? rows.map(function (row) {
          const checked = selected[Number(row.id)] ? " checked" : "";
          return '<label class="op-source-item">' +
            '<input type="checkbox" data-op-source="' + op.id + '" value="' + row.id + '"' + checked +
            ' data-saved-checked="' + (checked ? '1' : '0') + '">' +
            '<span class="op-source-name">' + esc(row.name) + "</span>" +
            '<span class="tiny muted">' + esc(row.library || row.library_code || "") + "</span>" +
            "</label>";
        }).join("")
      : '<div class="tiny muted">当前库里还没有已处理完成的资料可供参考。</div>';
    const count = rows.filter(function (row) { return selected[Number(row.id)]; }).length;
    const hint = count ? "已选 " + count + " / " + rows.length + " 份参考文件" : "请至少选择一份参考文件";
    return '<div class="op-sources">' +
      '<div class="op-sources-head"><label>参考文件（可多选）</label>' +
      '<button type="button" class="btn sm" data-op-source-all="' + op.id +
      '" title="全选当前列表中的参考文件"' + (!rows.length || count === rows.length ? ' disabled' : '') + '>全选</button>' +
      '<span class="tiny muted" data-op-source-hint="' + op.id + '">' + esc(hint) + "</span></div>" +
      '<div class="op-source-search"><input type="search" data-op-source-search="' + op.id +
      '" placeholder="搜索文件名" aria-label="搜索参考文件名" title="筛选当前参考文件列表">' +
      '<span class="tiny muted" data-op-source-matches="' + op.id + '" role="status"></span></div>' +
      '<div class="op-source-list">' + items + "</div>" +
      '<div class="tiny muted" data-op-source-empty hidden>没有匹配的参考文件，请更换关键词。</div>' +
      '<div class="tiny muted"><strong>至少选择一份参考文件</strong>；' +
      "只使用所选资料撰写，改选会立即保存到这张待办上。</div>" +
      "</div>";
  }

  function filterCardSources(card) {
    const input = card.querySelector("[data-op-source-search]");
    const query = input ? input.value.trim().toLocaleLowerCase() : "";
    const items = Array.from(card.querySelectorAll(".op-source-item"));
    items.forEach(function (item) {
      item.hidden = !item.querySelector(".op-source-name").textContent.toLocaleLowerCase().includes(query);
    });
    const visible = items.filter(function (item) { return !item.hidden; });
    const matches = card.querySelector("[data-op-source-matches]");
    if (matches) matches.textContent = query ? "找到 " + visible.length + " / " + items.length + " 份" : "";
    const empty = card.querySelector("[data-op-source-empty]");
    if (empty) empty.hidden = !query || visible.length > 0 || items.length === 0;
    const all = card.querySelector("[data-op-source-all]");
    if (all) {
      all.textContent = query ? "全选结果" : "全选";
      all.title = query ? "选择全部搜索结果，保留其他已选文件" : "全选当前列表中的参考文件";
      all.disabled = card.dataset.sourcesBusy === "1" || !visible.length ||
        visible.every(function (item) { return item.querySelector("[data-op-source]").checked; });
    }
  }

  function syncCardSources(card) {
    const boxes = Array.from(card.querySelectorAll("[data-op-source]"));
    const count = boxes.filter(function (box) { return box.checked; }).length;
    const busy = card.dataset.sourcesBusy === "1";
    boxes.forEach(function (box) { box.disabled = busy; });
    const all = card.querySelector("[data-op-source-all]");
    if (all) all.disabled = busy || !boxes.length || count === boxes.length;
    const confirm = card.querySelector("[data-op-confirm]");
    if (confirm) {
      confirm.disabled = busy || card.dataset.sourcesUncertain === "1" || count === 0;
      confirm.title = busy ? "正在保存参考文件选择" :
        (card.dataset.sourcesUncertain === "1" ? "请刷新后核对参考文件选择" : (count ? "" : "请先选择至少一份参考文件"));
    }
    const hint = card.querySelector("[data-op-source-hint]");
    if (hint) hint.textContent = busy ? "正在保存参考文件选择…" :
      (count ? "已选 " + count + " / " + boxes.length + " 份参考文件" : "请至少选择一份参考文件");
    filterCardSources(card);
  }

  /** 待确认卡上改参考文件：**立刻持久化到这条操作上**，失败就回到服务端的真实状态。 */
  async function setCardSources(card) {
    if (card.dataset.sourcesBusy === "1") return;
    const operationId = card.getAttribute("data-op");
    const notice = card.querySelector(".proposal-notice");
    const boxes = Array.prototype.slice.call(card.querySelectorAll("[data-op-source]"));
    const fileIds = boxes.filter(function (box) { return box.checked; })
      .map(function (box) { return Number(box.value); });
    if (!fileIds.length) {
      boxes.forEach(function (box) { box.checked = box.dataset.savedChecked === "1"; });
      syncCardSources(card);
      KB.notice(notice, "至少选择一份参考文件，不能取消最后一份。", "warn");
      return;
    }
    card.dataset.sourcesBusy = "1";
    syncCardSources(card);
    try {
    const result = await assistantApi(
      "/api/v1/planner/operations/" + operationId + "/sources/",
      { method: "POST", json: { file_ids: fileIds } }
    );
    if (!result.ok) {
      const detail = KB.errorText(result, "请求失败");
      if (notice) notice.textContent = "参考文件未保存：" + detail;
      KB.toast("参考文件未保存：" + detail, "err");
      const rows = await loadOperations();
      const latest = rows && rows.find(function (row) { return Number(row.id) === Number(operationId); });
      card.dataset.sourcesUncertain = latest ? "0" : "1";
      const saved = latest ? new Set((latest.source_file_ids || []).map(Number)) : null;
      boxes.forEach(function (box) {
        box.checked = saved ? saved.has(Number(box.value)) : box.dataset.savedChecked === "1";
        box.dataset.savedChecked = box.checked ? "1" : "0";
      });
      if (!latest && notice) notice.textContent += "；请刷新后核对选择，再继续确认。";
      return;
    }
    const saved = new Set((result.body.source_file_ids || fileIds).map(Number));
    card.dataset.sourcesUncertain = "0";
    boxes.forEach(function (box) {
      box.checked = saved.has(Number(box.value));
      box.dataset.savedChecked = box.checked ? "1" : "0";
    });
    if (notice) notice.textContent = (result.body && result.body.message) || "已更新参考文件";
    } finally {
      card.dataset.sourcesBusy = "0";
      syncCardSources(card);
    }
  }

  // ---- 文档编辑状态（保存中 / 已保存 / 有未保存改动 / 保存失败）--------------
  //
  // 为什么要**基线比对**而不是"一个 dirty 标志"：用户可能改了又改回来、或者
  // 切到预览再切回来。基线（上次打开/保存时的标题·主题·正文）才能如实回答
  // "现在这份和已保存的那份一样吗"，从而决定保存按钮该不该亮、切换文档要不要拦。
  let docBaseline = { title: "", topic: "", body: "", format: "{}" };
  let docSavedAt = "";
  let docHint = "";
  // 「加载某一份草稿」的票：连点 A、B 时保证**后点的那份**赢（先发的慢响应被丢弃）
  let docLoadTicket = 0;
  // 首页「文件」上传的互斥（上传中不接受第二批）
  let uploadBusy = false;
  // 「用户编辑版本号」：每次输入 +1。请求发出时记下版本，响应回来若版本变了，
  // 就说明**请求期间用户又编辑过** —— 这时绝不能拿响应覆盖编辑器（Codex 第三批 B）。
  let docEditRevision = 0;
  // 进行中的动作（保存/生成）：状态条优先显示它，且**不会被普通的 UI 刷新覆盖**
  // （第一版把"保存中/已生成"写进 docStatusText，随后 updateDocUI 又按 dirty 重算，
  // 用户看到的是"未保存…"，等于反馈丢了 —— 第一轮浏览器验收实测）。
  let docBusy = null;      // {kind: "saving"|"generating"|"exporting", text}
  // 一次性结果条（成功/失败/警告）：用户下一次编辑或切换草稿时清除
  let docFlash = null;     // {kind: "saved"|"error"|"warn", text}

  // ------------------------------------------------------------ 欢迎态 ----

  function greeting() {
    const hour = new Date().getHours();
    if (hour < 5) return "这么晚还在整理资料";
    if (hour < 11) return "早上好";
    if (hour < 13) return "中午好";
    if (hour < 18) return "下午好";
    return "晚上好";
  }

  /** 原型 welcomeHtml()：问候 + 标题 + 5 统计 + 6 快捷句 + 4 能力卡 + 资料库区 + 底部说明。 */
  function welcomeHtml(counts, statsFailed) {
    // 统计读取失败时**不显示 0**：0 会被读成"库里什么都没有"，而事实是"这次没读到"。
    const dash = function (value) { return statsFailed ? "—" : value; };
    const stats = {
      total: dash(counts.files || 0),
      done: dash(counts.done || 0),
      session: dash(counts.session_files || 0),
      pending: dash((counts.pending || 0) + (counts.processing || 0)),
      kbs: dash(counts.libraries || 0),
    };
    const quick = [
      "列出资料库里的全部文件", "列出失败的资料", "搜一下「合同」",
      "看看运行状态", "测试向量服务连通", "你能做什么",
    ];
    const caps = [
      ["/app/files/", "文件管理", "上传、解析、删除、目录索引与预览，都在文件管理里完成"],
      ["/app/search/", "搜索", "按关键词、部门、金额或性质过滤，结果可预览可反馈"],
      ["/app/agent/", "Agent 控制台", "看门狗运行状态、事件时间线，以及各条检索反馈的处理"],
      ["/app/settings/", "系统配置", "远程模型服务、模型与基础设施配置"],
    ];
    const stat = function (value, label, href) {
      return '<button class="home-stat" data-nav="' + href + '"><b>' + mono(value) +
        "</b><i>" + esc(label) + "</i></button>";
    };
    return '<div class="home-wrap">' +
      '<header class="home-head">' +
        '<div class="home-hello">' + esc(greeting()) + "</div>" +
        '<h1 class="home-title">知识库里的事，说一句话就行</h1>' +
        '<p class="home-sub">每个动作都能在这里用日常说法完成：上传、搜索、目录收录、' +
          "看运行状态、查看模型服务配置。" +
          "不可撤销的动作（比如删除资料）会先出一张卡，等你确认才动。</p>" +
      "</header>" +
      '<section class="home-stats">' +
        stat(stats.total, "文件库资料", "/app/files/") +
        stat(stats.done, "已处理", "/app/files/") +
        stat(stats.session, "会话资料", "/app/files/?session=1") +
        stat(stats.kbs, "信息库", "/app/knowledge/") +
        stat(stats.pending, "待处理", "/app/files/?status=pending") +
      "</section>" +
      '<section class="home-block">' +
        '<div class="home-block-head"><span class="ic-spark">◆</span> 一开口就能做 ' +
          "<em>点一下 = 把这句话发给我</em></div>" +
        '<div class="home-ops">' +
          quick.map(function (text) {
            return '<button class="home-op" data-say="' + esc(text) + '">' + esc(text) + "</button>";
          }).join("") +
        "</div>" +
        '<div class="home-op-note">也可以直接打字，例如「删除失败的那个 xxx.pdf」' +
          "「看看产品部都有哪些资料」。</div>" +
      "</section>" +
      '<section class="home-block">' +
        '<div class="home-block-head"><span class="ic-spark">◆</span> 要去页面里细看的事 ' +
          "<em>都是真实页面，没有死链接</em></div>" +
        '<div class="home-cards">' +
          caps.map(function (item) {
            return '<button class="home-card" data-nav="' + item[0] + '">' +
              '<span class="home-card-icon">▪</span>' +
              '<span class="home-card-body"><b>' + esc(item[1]) + "</b><i>" + esc(item[2]) +
              "</i></span></button>";
          }).join("") +
        "</div>" +
      "</section>" +
      '<section class="home-block">' +
        '<div class="home-block-head"><span class="ic-lib">▤</span> 当前资料库</div>' +
        '<div class="home-tags">' +
          '<span class="tag">共 ' + mono(stats.total) + " 份</span>" +
          '<span class="tag warn">待处理 ' + mono(stats.pending) + "</span>" +
          '<span class="tag ok">已处理 ' + mono(stats.done) + "</span>" +
          '<span class="tag">会话来源 ' + mono(stats.session) + "</span>" +
        "</div>" +
        '<div class="home-links">' +
          '<button class="btn sm" data-nav="/app/files/">文件管理</button>' +
          '<button class="btn sm" data-nav="/app/search/">搜索</button>' +
          '<button class="btn sm" data-nav="/app/agent/">Agent 控制台</button>' +
          '<button class="btn sm" data-nav="/app/settings/">系统配置</button>' +
          '<button class="btn sm" data-nav="/app/docs/">使用说明</button>' +
        "</div>" +
      "</section>" +
      '<div class="home-foot">' +
        "资料都是真实入库的：上传会真的落盘并建立索引，删除需要三次确认。" +
        '<a href="/app/docs/">看使用说明 →</a>' +
      "</div>" +
    "</div>";
  }

  // -------------------------------------------------------------- 消息 ----

  /** 原型 renderBold()：先转义，再把 **粗体** 渲染成 <b>。 */
  function renderBold(text) {
    return esc(text).replace(/\*\*(.+?)\*\*/g, "<b>$1</b>");
  }

  /** 原型 renderMessage() 的结构。 */
  /** 被拒提案的**持久**提示（独立审查条目 57）。
   *
   * 为什么要有：模型可能只提工具、不给正文；若提案全被拒（协议错误 / 无权限 /
   * 参数不合法），后端只在本次 HTTP 响应里给原因，刷新后就只剩一个**空气泡**，
   * 用户不知道"AI 提了 X，但因为 Y 被拒"。所以原因随消息 payload 持久化
   * （`rejected_proposals`），这里读 payload 渲染，不做任何推测。
   *
   * 注意：真正的"模型什么都没返回"（空正文且无工具调用）在产品里会被后端判为
   * 失败并落一条 **system** 消息（`助手不可用：…`），不会走到这里 —— 所以这里
   * 不需要也不应该再编一个"空回答"分支。
   */
  function rejectedHtml(payload) {
    const rows = (payload && payload.rejected_proposals) || [];
    if (!rows.length) return "";
    return '<div class="notice-bar" style="margin:8px 0">AI 建议了 ' + rows.length +
      " 个操作，但都没有创建待办：" +
      '<ul class="op-output" style="margin:6px 0 0 16px">' +
      rows.slice(0, 10).map(function (row) {
        return "<li>" + esc(row.intent) +
          (row.reason ? ' <span class="muted tiny">· ' + esc(row.reason) + "</span>" : "") +
          "</li>";
      }).join("") +
      "</ul></div>";
  }

  /** 工具的中文名（只用于**如实**说明"这一轮发起了什么"，不改变任何行为）。 */
  const TOOL_NAMES = {
    list_files: "列出资料", search_knowledge: "检索资料", read_file: "查看资料详情",
    index_stats: "查看索引状态", export_document: "导出派生文档",
    restart_stalled_task: "重启停滞任务", compose_document: "编写新文档（需选参考文件）",
    delete_file: "删除资料", parse_file: "解析资料", upload_file: "上传资料",
  };

  /** 助手消息**正文为空**时如实说明这一轮发生了什么（绝不编造一段回答）。
   *
   * 真实场景（用户实测反馈："这个报告这怎么没写？"）：用户说"写一个出行报告"，
   * 模型这一轮可能只发起了一次工具调用（早期是 search_knowledge，现在是
   * compose_document 提案），助手消息正文确实是空的 —— 界面于是只剩一个空白
   * 气泡，用户看到的就是"模型什么都没干"。这里把 payload.tool_calls 里**真实存在**
   * 的调用名讲清楚，并指向下方的待确认卡片。
   */
  function emptyReplyHtml(payload) {
    const calls = (payload && payload.tool_calls) || [];
    if (!calls.length) {
      return '<span class="muted">（这一轮没有正文回答）</span>';
    }
    const names = calls.map(function (call) {
      return TOOL_NAMES[call.name] || call.name;
    }).join("、");
    return '<span class="muted">（这一轮没有正文回答，只发起了操作：' + esc(names) +
      "。检索类操作已直接执行；写操作会在下方生成待确认卡片。）</span>";
  }

  function renderMessage(message) {
    const mine = message.role === "user";
    const content = message.content || "";
    // 空白气泡会被读成"模型什么都没做"：正文为空时给出**基于真实 payload** 的说明
    const bubble = content
      ? renderBold(content)
      : (mine ? renderBold("") : emptyReplyHtml(message.payload));
    return '<div class="msg-row ' + (mine ? "role-user" : "role-assistant") + ' fade-in">' +
      '<div class="msg-avatar">' + (mine ? "我" : "AI") + "</div>" +
      '<div class="msg-body">' +
        '<div class="msg-bubble">' + bubble + "</div>" +
        (message.degradedHtml || "") +
        (message.scopeHtml || "") +
        (message.rejectedHtml || "") +
        (message.operationHtml || "") +
        '<div class="msg-meta">' + esc(KB.fmtTime(message.created_at)) +
          (mine ? "" : (message.meta ? " · " + esc(message.meta) : "")) + "</div>" +
      "</div>" +
    "</div>";
  }

  const OP_STATE_TEXT = {
    proposed: "等待确认", confirmed_1: "第 1 次确认", confirmed_2: "第 2 次确认",
    armed: "待执行", executing: "执行中", executed: "已完成",
    rejected: "已放弃", expired: "已过期", failed: "未完成",
  };

  /** 派生文档状态的中文说法（处置清单 AF-16）。
   *
   * 为什么在前端映射而不是等后端补 `status_label`：接口只回 `status`（英文枚举），
   * 界面直接把 `draft` 显示给用户；映射表放在**显示层**，不必改后端契约。
   * 未知取值**原样回显**（不编造中文），确实没有状态就不写 —— 不猜。
   */
  const DOCUMENT_STATUS_TEXT = {
    draft: "草稿",
    edited: "已编辑",
    exported: "已导出",
  };

  function documentStatusLabel(row) {
    const raw = String((row || {}).status || "").trim();
    if (!raw) return "";
    return DOCUMENT_STATUS_TEXT[raw] || raw;
  }

  /** 原型 renderOperation() 的 op-card 结构；确认流程接真实后端。 */
  function renderOperationCard(op) {
    const armed = op.status === "armed";
    const needConfirm = ["proposed", "confirmed_1", "confirmed_2"].indexOf(op.status) >= 0;
    // 「放弃」= 放弃这条待办（后端标记为终态 rejected）。**只对没执行完的卡片出现**：
    // 已执行/已过期/失败的卡片没有"放弃"可言，给按钮只会误导。
    //
    // 文案必须是「放弃」而不是「删除」（处置清单 AF-09）：这个按钮**不删任何资料**，
    // 只把这条待办标记为已放弃（审计保留）。在"删除资料"是本系统最危险动作的语境下，
    // 一个写着「删除」的按钮会被读成"删除资料" —— 会让人不敢点，或者误点。
    const abandonable = ["proposed", "confirmed_1", "confirmed_2", "armed"].indexOf(op.status) >= 0;
    const abandonButton = abandonable
      ? '<button class="btn ghost sm" data-op-abandon="' + op.id +
        '" title="放弃这条待办：标记为已放弃并保留操作审计，不会删除任何资料">放弃</button>'
      : "";
    const details = [];
    if (op.target_label) details.push("目标：" + op.target_label);
    // 「本次检索范围」：用户勾选的信息库（执行层按它收敛检索集合）。
    // 直接在卡片上显示，才能解释"为什么只/也搜出了这些库的资料"；
    // 未勾库时后端给空数组，这里不显示（旧行为不变）。
    const scopeLabels = (op.library_scope_labels && op.library_scope_labels.length)
      ? op.library_scope_labels
      : (op.library_scope || []);
    if (scopeLabels.length) details.push("检索范围：" + scopeLabels.join("、"));
    // 「编写新文档」的待确认卡：把**当前**选中的模板写进明细。
    // 卡片进入 armed/executed（下拉框消失）之后，这是唯一能看出"套没套模板"的地方。
    const opParams = op.params || {};
    if (op.intent === "compose_document" && opParams.template_id) {
      const matched = (templateRows || []).filter(function (row) {
        return Number(row.id) === Number(opParams.template_id);
      })[0];
      details.push("文档模板：" + (matched ? matched.name : "id=" + opParams.template_id +
        "（已不在模板列表里）"));
    }
    if (op.required_confirmations > 1) {
      details.push("确认进度：" + op.confirmations + " / " + op.required_confirmations +
        "（删除类操作需要三次独立确认）");
    }
    if (op.armed_expires_at) details.push("执行窗口至：" + KB.fmtTime(op.armed_expires_at));

    return '<div class="op-card state-' + esc(op.status) + '" data-op="' + op.id + '">' +
      '<div class="op-head">' +
        '<span class="op-title">' + esc(op.label || op.intent) + "</span>" +
        '<span class="op-state">' + esc(OP_STATE_TEXT[op.status] || op.status) + "</span>" +
      "</div>" +
      '<div class="op-body">' +
        (details.length
          ? '<ul class="op-details">' +
            details.map(function (text) { return "<li>" + esc(text) + "</li>"; }).join("") + "</ul>"
          : "") +
        (op.intent === "compose_document" && needConfirm
          ? '<div class="op-template">' +
              '<label for="op-template-' + op.id + '">文档模板</label>' +
              '<select id="op-template-' + op.id + '" data-op-template="' + op.id + '"' +
              ' data-selected-template="' + esc(String(opParams.template_id || "")) + '"' +
              ' data-prev-value="' + esc(String(opParams.template_id || "")) + '"' +
              ' title="选一份文档模板，按它的格式撰写；不选则按通用格式">' +
              templateOptionHtml(opParams.template_id) +
              "</select>" +
              '<span class="tiny muted"><strong>不选模板就不用模板</strong>（按通用文档格式撰写）；' +
              "改选会立即保存到这张待办上，然后点「继续确认」。</span>" +
            "</div>"
          : "") +
        (op.intent === "compose_document" && needConfirm ? sourcePickerHtml(op) : "") +
        (op.error ? '<div class="err-text">原因：' + esc(op.error) + "</div>" : "") +
        (needConfirm
          ? '<div class="op-actions">' +
              '<button class="btn primary sm" data-op-confirm="' + op.id + '"' +
              (op.intent === "compose_document" && !(op.source_candidates || []).some(function (row) {
                return (op.source_file_ids || []).map(Number).includes(Number(row.id));
              }) ? ' disabled title="请先选择至少一份参考文件"' : '') + '>继续确认</button>' +
              abandonButton +
              '<span class="hint">在你确认之前，不会有任何改动。</span></div>'
          : "") +
        (armed
          ? (op.armed_expired
              ? '<div class="op-warning"><span class="ic-warn">!</span> 确认已过期，需重新发起提案。</div>' +
                (abandonButton
                  ? '<div class="op-actions">' + abandonButton + "</div>"
                  : "")
              : '<div class="op-actions"><button class="btn primary sm" data-op-execute="' + op.id +
                '">执行</button>' + abandonButton + '<span class="hint">执行窗口至 ' +
                esc(KB.fmtTime(op.armed_expires_at)) + "</span></div>")
          : "") +
        (op.resultHtml || "") +
        '<div data-operation-progress="' + op.id + '" data-progress-state="' +
          (op.status === "executing" ? "running" : "idle") + '"' +
          (op.status === "executing" ? "" : " hidden") + '>' +
          (op.status === "executing" ? KB.progress.markup(op.progress || {state: "running", label: "正在执行"}) : "") + '</div>' +
        '<div class="proposal-notice tiny"></div>' +
      "</div>" +
    "</div>";
  }

  /** 执行结果：把后端真实返回展示出来（不伪造）。 */
  function resultHtml(result) {
    if (!result || typeof result !== "object") return "";
    const parts = [];
    if (typeof result.count === "number") parts.push("共 " + mono(result.count) + " 条");
    if (result.detail) parts.push(String(result.detail));
    const items = result.items || [];
    const list = items.length
      ? '<ul class="op-output">' + items.slice(0, 20).map(function (item) {
          const name = item.name || item.original_name || "";
          const meta = [item.library || item.library_code, item.status].filter(Boolean).join(" · ");
          return "<li>" + esc(name) +
            (meta ? ' <span class="muted tiny">· ' + esc(meta) + "</span>" : "") + "</li>";
        }).join("") + "</ul>"
      : "";
    return '<div class="op-result">' +
      (parts.length ? '<div class="op-summary">' + esc(parts.join("；")) + "</div>" : "") +
      list + "</div>";
  }

  // -------------------------------------------------------------- 渲染 ----

  async function loadOperations() {
    const result = await assistantApi("/api/v1/planner/operations/");
    if (!result.ok) {
      // **失败返回 null（不是空数组）**：空数组会被下游渲染成"暂无待办操作/操作不存在"，
      // 把一次请求失败说成"真的没有"（处置清单 AF-07）。
      return null;
    }
    return ((result.body && result.body.results) || []).filter(function (op) {
      return ["archive_file", "unarchive_file"].indexOf(op.intent) < 0;
    });
  }

  function scrollToEnd() {
    const scroll = $("chat-scroll");
    if (scroll) scroll.scrollTop = scroll.scrollHeight;
  }

  async function renderHome(forceWelcome) {
    const inner = $("chat-inner");
    const stats = await assistantApi("/api/v1/knowledge/stats/");
    const counts = (stats.ok && stats.body) || {};
    // 统计读不到时**不能**显示成 0（那看起来像"库里什么都没有"）：
    // 首页会顶一条可见提示，数字位显示 "—"。
    const statsFailed = !stats.ok;

    if (!conversationId || forceWelcome) {
      conversationId = null;
      $("chat-title").textContent = "智库";
      $("chat-meta").textContent = "首页 · 一句话完成知识库的事";
      inner.innerHTML = welcomeHtml(counts, statsFailed);
      if (statsFailed) {
        // 用**独立提示条**而不是 KB.notice（后者会清空容器内容，可能连带抹掉别的节点）
        const host = $("composer-box");
        if (host && host.parentNode) {
          let note = document.getElementById("kb-stats-warning");
          if (!note) {
            note = document.createElement("div");
            note.id = "kb-stats-warning";
            note.className = "notice warn";
            note.style.margin = "6px 0";
            host.parentNode.insertBefore(note, host);
          }
          note.textContent = "统计读取失败：" + KB.errorText(stats, "请求失败") +
            "（上方数字显示为「—」，不代表库里是 0）";
        }
      }
      $("chat-scroll").scrollTop = 0;   // 空会话停在顶部
    } else {
      const result = await assistantApi("/api/v1/planner/conversations/" + conversationId + "/");
      const messages = (result.ok && result.body && result.body.messages) || [];
      // **操作来自 response.operations**（消息本身没有 operation_id）。
      // 之前依赖 message.operation_id → 所有待确认卡都被丢掉。
      const operations = ((result.ok && result.body && result.body.operations) || []).filter(function (op) {
        return ["archive_file", "unarchive_file"].indexOf(op.intent) < 0;
      });
      inner.innerHTML = messages.map(function (message) {
        const payload = message.payload || {};
        // **system 消息不是 AI 回答**：它是系统留痕（例如"助手不可用：…"）。
        // 之前一律走 renderMessage → 渲染成带「AI」头像的气泡，等于把系统提示
        // 伪装成模型说的话，用户会以为模型回了这句。
        if (message.role === "system") {
          return '<div class="notice-bar fade-in" style="margin:8px 0">' +
            esc(message.content || "（系统提示）") + "</div>";
        }
        return renderMessage({
          role: message.role,
          content: message.content,
          created_at: message.created_at,
          meta: message.role === "assistant" ? "模型回答" : "",
          // 正文为空时据此如实说明"发起了什么操作"（不编造回答）
          payload: payload,
          // 持久提示：来自消息 payload，刷新/翻历史都还在
          degradedHtml: payload.retrieval_degraded
            ? '<div class="notice-bar" style="margin:8px 0">注意：本次检索只覆盖了部分信息库（' +
              esc(payload.retrieval_degraded_reason || "部分向量检索不可用") +
              "），回答可能不完整。</div>"
            : "",
          // 「知识库调用选择」的如实回执：这一轮**实际**检索了哪几个库；
          // 勾了库却因权限被回落成全库时，后端给出 scope_note，这里原样显示。
          scopeHtml: scopeReceiptHtml(payload),
          // 被拒提案 / 空回答：同样来自 payload，刷新后不会消失
          rejectedHtml: rejectedHtml(payload),
        });
      }).join("");

      // 操作卡单独铺在消息之后（服务端顺序：最新在前，这里按时间正序展示）
      const cards = operations.slice().reverse().map(function (op) {
        const withResult = Object.assign({}, op);
        // executed 的操作把真实结果一并渲染出来
        withResult.resultHtml = op.status === "executed" ? resultHtml(op.result || {}) : "";
        return renderOperationCard(withResult);
      }).join("");
      if (cards) inner.insertAdjacentHTML("beforeend", '<div class="op-list">' + cards + "</div>");

      $("chat-title").textContent = (result.body && result.body.title) || "会话";
      $("chat-meta").textContent = messages.length + " 条消息" +
        (operations.length ? " · " + operations.length + " 个操作" : "");
      scrollToEnd();                    // 有消息才滚到底
    }
    bindHome(inner);
    // 待确认卡上的「文档模板」下拉需要**真实**的模板列表：没读到就取一次。
    // 取不到不阻塞页面（下拉里会显示"模板列表读取失败"），也绝不影响确认流程本身。
    if (inner.querySelector("[data-op-template]")) await loadTemplates();
    if (KB.nav && KB.nav.renderSessions) await KB.nav.renderSessions(conversationId);
  }

  // ------------------------------------------------------------ 交互绑定 ----

  /** 事件委托：只在容器上挂**一个**监听。
   *
   * 为什么不用"给每个按钮绑事件 + dataset 标记"：renderHome 会替换 innerHTML，
   * 但容器元素本身没变，容器上的 dataset 标记会一直留着 → 新按钮再也绑不上。
   * 委托挂在容器上，重渲染后依然有效，且天然不会重复触发。
   */
  function bindHome(container) {
    const inner = container || $("chat-inner");
    if (!inner || inner.dataset.kbDelegated === "1") return;
    inner.dataset.kbDelegated = "1";
    inner.addEventListener("click", function (event) {
      // 发送失败后的重试：**复用原文**，不丢用户输入（处置清单 AF-08）
      const retry = event.target.closest("#kb-send-retry, [data-retry-send]");
      if (retry) {
        const box = retry.closest("#kb-send-failure");
        const wrap = retry.closest("[data-retry-wrap]");
        const text = (box && box.dataset.retryText) ||
          retry.getAttribute("data-retry-send") || "";
        // 先把承载它的提示条目整条移掉（重试会重新插入新的失败说明）
        if (box && box.parentNode) box.parentNode.removeChild(box);
        if (wrap && wrap.parentNode) wrap.parentNode.removeChild(wrap);
        // 原文放回输入框：即使重试再次失败，用户也不会丢掉自己写的内容
        const area = $("composer");
        if (area) area.value = text;
        send(text);
        return;
      }
      const nav = event.target.closest("[data-nav]");
      if (nav) {
        location.href = nav.getAttribute("data-nav");
        return;
      }
      const say = event.target.closest("[data-say]");
      if (say) {
        send(say.getAttribute("data-say"));
        return;
      }
      const confirm = event.target.closest("[data-op-confirm]");
      if (confirm) {
        confirmFlow(Number(confirm.getAttribute("data-op-confirm")), confirm);
        return;
      }
      const selectAll = event.target.closest("[data-op-source-all]");
      if (selectAll) {
        if (selectAll.disabled) return;
        const card = selectAll.closest(".op-card");
        card.querySelectorAll("[data-op-source]").forEach(function (box) {
          if (!box.closest(".op-source-item").hidden) box.checked = true;
        });
        setCardSources(card);
        return;
      }
      const exec = event.target.closest("[data-op-execute]");
      if (exec) {
        executeCard(exec);
        return;
      }
      const abandon = event.target.closest("[data-op-abandon]");
      if (abandon) {
        abandonCard(abandon);
        return;
      }
    });
    // 待确认卡上的「文档模板」下拉：**change 不是 click**，必须单独挂一个委托。
    // 放在同一个容器上：renderHome/renderOperationCard 会整体替换 innerHTML，
    // 但容器本身没换，委托仍然有效（与上面的 click 同一个理由）。
    inner.addEventListener("input", function (event) {
      const search = event.target.closest("[data-op-source-search]");
      if (search) filterCardSources(search.closest(".op-card"));
    });
    inner.addEventListener("change", function (event) {
      const select = event.target.closest("[data-op-template]");
      if (select) {
        setCardTemplate(select);
        return;
      }
      const box = event.target.closest("[data-op-source]");
      if (box) {
        const card = box.closest(".op-card");
        if (card) setCardSources(card);
      }
    });
  }

  /** 执行一张已确认的卡：成功后**更新原卡**并给出结果入口。 */
  async function executeCard(button) {
    const card = button.closest(".op-card");
    const notice = card.querySelector(".proposal-notice");
    button.disabled = true;
    card.querySelectorAll(".op-actions button").forEach(function (action) { action.disabled = true; });
    const operationId = Number(button.getAttribute("data-op-execute"));
    KB.progress.beginOperation(operationId);
    let done;
    try {
    done = await assistantApi(
      "/api/v1/planner/operations/" + button.getAttribute("data-op-execute") + "/execute/",
      { method: "POST", json: {} }
    );
    } finally { KB.progress.endOperation(operationId); }
    if (!done.ok) {
      KB.notice(notice, (done.body && done.body.detail) || "执行失败", "err");
      return;
    }
    const stateEl = card.querySelector(".op-state");
    if (stateEl) stateEl.textContent = "已完成";
    card.classList.remove("state-armed");
    card.classList.add("state-executed");
    const actions = card.querySelector(".op-actions");
    if (actions) actions.remove();
    const result = done.body.result || {};
    // **执行完就把卡撤掉**（2026-10-19 用户要求：卡片只用于确认操作，确认完毕后就消失）。
    // 结果该进哪里就进哪里（例如生成的文档直接在文档栏打开），会话区只留对话本身。
    card.remove();
    KB.toast("已执行");
    offerResultEntry(result);
  }

  /** 放弃一张待办卡：**不是**删除资料，只是不再继续（后端标记为已放弃并留审计）。 */
  async function abandonCard(button) {
    const card = button.closest(".op-card");
    const notice = card ? card.querySelector(".proposal-notice") : null;
    const answer = await KB.confirmDialog({
      title: "放弃这条待办",
      body: "放弃后它会从待办列表消失，并标记为已放弃。" +
        "它本来就没有执行过，所以不会删除任何资料；" +
        "操作记录会按审计要求保留（是保留历史，不是删除历史）。",
      confirmLabel: "放弃",
    });
    if (!answer.confirmed) return;
    button.disabled = true;
    const done = await assistantApi(
      "/api/v1/planner/operations/" + button.getAttribute("data-op-abandon") + "/abandon/",
      { method: "POST", json: {} }
    );
    if (!done.ok) {
      const detail = (done.body && done.body.detail) || "放弃失败";
      if (notice) KB.notice(notice, detail, "err");
      else KB.toast(detail);
      button.disabled = false;
      return;
    }
    // 待办列表：整块重载，条目直接消失（服务端不再返回已放弃的操作）
    const pendingBox = button.closest("#pending-ops");
    if (pendingBox) {
      KB.toast("已放弃该待办");
      await loadPendingOps(pendingBox);
      return;
    }
    // 会话里的卡：终态就**移除**——卡片只用于"等待用户确认"的操作
    // （2026-10-19 用户要求：确认完毕后就消失；历史去「我的操作记录」查）。
    if (card) {
      card.remove();
    }
    KB.toast("已放弃该待办");
  }

  /** 按**真实结果**给出后续入口：文档 → 右侧文档栏；上传 → 选择文件。 */
  function offerResultEntry(result) {
    if (!result || typeof result !== "object") return;
    const documentId = result.document_id || result.doc_id;
    if (documentId) {
      toggleDocs(true);
      openDocument(documentId);
      return;
    }
    if (result.action_required === "open_upload" || result.upload_url) {
      // 上传必须由用户亲自选文件：给出真实的文件选择入口
      const mask = KB.modal(
        '<div class="modal-head"><b>上传资料</b></div>' +
        '<div class="modal-body paper"><p>' +
          esc(result.detail || "请选择要上传的文件。") + "</p>" +
          '<input type="file" id="result-upload" multiple>' +
        "</div>" +
        '<div class="modal-foot"><button class="btn sm" data-close>关闭</button></div>'
      );
      mask.querySelector("#result-upload").addEventListener("change", function (event) {
        uploadFiles(event.target.files);
        mask.remove();
      });
    }
  }

  // -------------------------------------------------- 三次独立确认弹窗 ----

  /** 逐次确认：**从服务端进度继续**；取消/失败不推进也不倒退。 */
  async function confirmFlow(operationId, triggerButton) {
    const card = triggerButton.closest(".op-card");
    const notice = card.querySelector(".proposal-notice");
    if (card.querySelector(".op-sources") &&
        (card.dataset.sourcesBusy === "1" || card.dataset.sourcesUncertain === "1" || !card.querySelector("[data-op-source]:checked"))) {
      KB.notice(notice, "请先选择至少一份参考文件，并等待选择保存完成。", "warn");
      return;
    }
    triggerButton.disabled = true;

    const list = await loadOperations();
    if (list === null) {
      // 读不到操作列表 ≠ 操作不存在：说清原因并**恢复按钮**（AF-06/AF-07）
      KB.notice(notice, "操作列表读取失败，请重试（这不代表操作不存在）", "err");
      triggerButton.disabled = false;
      return;
    }
    const op = list.filter(function (row) { return row.id === operationId; })[0];
    if (!op) {
      KB.notice(notice, "操作不存在或无权访问", "err");
      // **必须恢复按钮**：否则这张卡片的「继续确认」永久点不动
      // （同类失败分支都恢复了，只有这条漏掉 —— 处置清单 AF-06）。
      triggerButton.disabled = false;
      return;
    }
    // resume：重新签发**当前步骤**的一次性凭证（旧凭证作废）
    const resumed = await assistantApi(
      "/api/v1/planner/operations/" + operationId + "/resume/",
      { method: "POST", json: {} }
    );
    if (!resumed.ok) {
      KB.notice(notice, (resumed.body && resumed.body.detail) || "无法恢复该操作", "err");
      triggerButton.disabled = false;
      return;
    }
    let token = resumed.body.confirm_token;
    const total = Number(op.required_confirmations || (op.requires_destructive_flow ? 3 : 1));
    let step = Number(resumed.body.confirmations || op.confirmations || 0) + 1;

    for (; step <= total; step += 1) {
      const isFinal = step === total;
      // 只有**破坏性**（删除类）且是最后一步才要求输入名称；
      // 单次只读确认不该逼用户抄文件名。
      const destructive = !!op.requires_destructive_flow;
      const needName = isFinal && destructive;
      const answer = await KB.confirmDialog({
        title: "第 " + step + " 次确认（共 " + total + " 次）",
        body: needName
          ? "这是最后一次确认。确认后将执行，且不可撤销。"
          : isFinal
            ? "这是最后一次确认。"
            : "请独立确认第 " + step + " 次；之后还需要 " + (total - step) + " 次确认。",
        targetLabel: op.target_label,
        requireTypedName: needName,
        confirmLabel: needName ? "确认并允许执行" : "确认",
      });
      if (!answer.confirmed) {
        KB.notice(notice, "已在第 " + step + " 次确认取消，流程停在这里。", "warn");
        triggerButton.disabled = false;
        return;
      }
      // **原样比对**，不做 trim（合法文件名可能自带首尾空格）
      if (needName && answer.typedName !== op.target_label) {
        KB.notice(notice, "名称不一致，未执行；请重新确认。", "err");
        triggerButton.disabled = false;
        return;
      }
      const result = await assistantApi(
        "/api/v1/planner/operations/" + operationId + "/confirm/",
        { method: "POST", json: { token: token, typed_name: answer.typedName } }
      );
      if (!result.ok) {
        KB.notice(notice, (result.body && result.body.detail) || "确认失败，可重试。", "err");
        triggerButton.disabled = false;
        return;
      }
      token = result.body.next_confirm_token || "";
      KB.notice(notice, result.body.message || "第 " + step + " 次确认完成", "ok");
    }
    // 刷新卡所在的容器：在工具弹窗里确认完就刷新弹窗，不要丢回首页
    const host = triggerButton.closest("#pending-ops");
    if (host) {
      await loadPendingOps(host);
    } else {
      await renderHome();
    }
  }

  // ---------------------------------------------------------------- 发送 ----

  /** 发送中的**可见**反馈（处置清单 AF-15）。
   *
   * 旧实现只有"发送键变灰"：模型回答可能几十秒，界面看起来和"卡住了"一模一样，
   * 用户只能反复按回车（而那些回车被静默吞掉）。现在：
   *   * 按钮文案变「发送中…」；
   *   * 会话区底部出现一条"正在生成回答…"的进行中提示（成功/失败都会撤掉）。
   */
  function showSendingIndicator() {
    const inner = $("chat-inner");
    if (!inner) return;
    let node = document.getElementById("kb-sending");
    if (!node) {
      node = document.createElement("div");
      node.id = "kb-sending";
      node.className = "notice-bar fade-in";
      node.style.margin = "10px 0";
      node.textContent = "正在生成回答…（模型回答可能要几十秒，请稍候）";
      inner.appendChild(node);
    }
    scrollToEnd();
  }

  function clearSendingIndicator() {
    const node = document.getElementById("kb-sending");
    if (node && node.parentNode) node.parentNode.removeChild(node);
  }

  async function send(text) {
    const content = String(text || "").trim();
    if (!content) return;
    const sendButton = $("btn-send");
    // 防连续重复提交：发送期间禁用，异常/结束都在 finally 里恢复
    if (sendButton && sendButton.disabled) {
      // 旧实现直接 `return`，用户按了回车却"什么都没发生"（处置清单 AF-15）。
      // 这里明确说一句在途状态，避免把"正在生成"读成"界面卡住了"。
      KB.toast("上一条还在发送中，请稍候…", "warn");
      return;
    }
    const originalLabel = sendButton ? sendButton.textContent : "";
    if (sendButton) {
      sendButton.disabled = true;
      sendButton.textContent = "发送中…";
    }
    showSendingIndicator();
    try {
      await sendOnce(content);
    } finally {
      clearSendingIndicator();
      if (sendButton) {
        sendButton.disabled = false;
        sendButton.textContent = originalLabel;
      }
    }
  }

  /**
   * 发送失败的**持久**错误条目（处置清单 AF-08）。
   *
   * 为什么必须留在会话区而不只是 1.8 秒的 toast：
   *   * 服务端原因（429 / 403 / 500 的 `detail`，或断网原因）在 toast 消失后
   *     就再也找不到了，用户只能反复重试同一条注定失败的消息；
   *   * 失败时那条用户气泡**并没有落库**（刷新就没了），界面必须说明这件事，
   *     否则用户会以为消息已经发出去了。
   *
   * 条目带**重试**按钮：点击后把原文放回输入框并重发（不丢用户输入）。
   * 重试成功后 `renderHome()` 会重建整个会话区，这条错误条目自然消失。
   */
  /**
   * 发送失败后的**持久**错误条目（处置清单 AF-08）。
   *
   * **文案必须准确**（用户 2026-10-16 指出）：后端在调用模型/检索**之前**就已经
   * `Message.objects.create` 写入了用户消息，而 `ChatView` 没有整体事务 ——
   * 所以 500 或"响应中途断网"完全可能发生在**用户消息已落库之后**。
   * 旧文案对所有非 503 失败都断言"没有写进会话记录，刷新不会保留"，
   * 在那种场景是**错误断言**，还会诱导用户盲目重试造成重复提交。
   *
   * 现在按状态分类说事实：
   *   * 400（参数/条件被拒）：可以说"请求被服务端拒绝"，未执行；
   *   * 其它（500 / 网络中断 / 未知）：只能说"没收到成功回执"，**不能**断言没落库；
   *   * 503 有专门提示（调用方处理），不走这里。
   */
  function insertSendFailure(content, detail, status) {
    const inner = $("chat-inner");
    if (!inner) return;
    const rejected = Number(status) === 400;
    const node = document.createElement("div");
    node.className = "notice-bar danger";
    node.id = "kb-send-failure";
    node.style.margin = "10px 0";
    node.dataset.retryText = content;
    const title = document.createElement("div");
    title.innerHTML = rejected
      ? "<b>请求被服务端拒绝（没有执行）。</b>"
      : "<b>没有收到成功回执。</b>";
    const reason = document.createElement("div");
    reason.textContent = "服务端原因：" + detail;
    const note = document.createElement("div");
    note.className = "tiny";
    note.textContent = rejected
      ? "服务端明确拒绝了这次请求（参数或前置条件不满足），可以按上面的原因改一改再发；" +
        "直接重试通常还是会失败（重试会用你原来的原文）。"
      : "**暂时无法确认这条消息是否已经保存**：服务端可能已经在写出错前把它写进了会话记录。" +
        "请先核对上面的会话记录，再决定是否重试（重试会用你原来的原文，可能重复提交）。";
    const retry = document.createElement("button");
    retry.className = "btn sm";
    retry.id = "kb-send-retry";
    retry.type = "button";
    retry.style.marginTop = "6px";
    retry.textContent = "重试发送";
    node.appendChild(title);
    node.appendChild(reason);
    node.appendChild(note);
    node.appendChild(retry);
    // 同一次会话里只留最近一条失败说明（避免连续失败堆成一串）
    const stale = document.getElementById("kb-send-failure");
    if (stale && stale.parentNode) stale.parentNode.removeChild(stale);
    inner.appendChild(node);
    scrollToEnd();
  }

  async function sendOnce(content) {
    const inner = $("chat-inner");

    if (!conversationId) {
      inner.innerHTML = "";
      $("chat-title").textContent = "新会话";
      $("chat-meta").textContent = "1 条消息";
    }
    inner.insertAdjacentHTML(
      "beforeend",
      renderMessage({ role: "user", content: content, created_at: new Date().toISOString() })
    );
    scrollToEnd();
    const area = $("composer");
    if (area) area.value = "";

    const result = await assistantApi("/api/v1/planner/chat/", {
      method: "POST",
      json: scopeCodes.length
        // 「知识库调用选择」：只在这些库里检索。**为空时省略字段**（= 全部可读库）——
        // 后端把显式空数组判成 400，因为"空数组"与"没选"必须能被区分开。
        ? { message: content, conversation_id: conversationId, library_codes: scopeCodes.slice() }
        : { message: content, conversation_id: conversationId },
    });

    if (result.status === 503) {
      // 这是**系统状态提示**，不是模型回答：不能伪装成 AI 气泡，
      // 否则用户会以为模型说了这句话。
      const detail = (result.body && result.body.detail) || "智能助手当前不可用";
      inner.insertAdjacentHTML(
        "beforeend",
        '<div class="notice-bar fade-in" data-retry-wrap="1" style="margin:12px 0">' +
          "模型服务当前不可用，这次没有生成回答。" + esc(detail) +
          '<div class="row" style="margin-top:6px">' +
          '<button class="btn sm" data-retry-send="' + esc(content) + '">重试发送</button>' +
          "</div></div>"
      );
      scrollToEnd();
      return;
    }
    if (!result.ok) {
      // 服务端原因**原样**带出来（旧实现只弹一句"请求失败，请稍后重试"，
      // 把 detail 丢掉，且提示 1.8 秒就消失）。
      const detail = KB.errorText(result, "请求失败");
      KB.toast("发送失败：" + detail, "err");
      insertSendFailure(content, detail, result.status);
      return;
    }
    conversationId = result.body.conversation_id;
    // 提示不在这里插 DOM：renderHome 会替换 inner，插了也会被冲掉。
    // 降级状态已写进助手消息 payload，由 renderMessage 渲染出**持久**提示。
    await renderHome();
  }

  // ------------------------------------------------------------ 工具栏 ----

  async function openTools() {
    const mask = KB.modal(
      '<div class="modal-head"><b>待办操作</b></div>' +
      '<div class="modal-body paper">' +
        '<div id="pending-ops" class="tiny muted">加载中…</div>' +
      '</div>' +
      '<div class="modal-foot"><button class="btn sm" data-close>关闭</button></div>'
    );
    await loadPendingOps(mask.querySelector("#pending-ops"));
  }

  /** 按 catalog 的 params 定义生成**中文表单**，做类型校验后提案。 */
  async function openParamForm(intent, spec) {
    const params = spec.params || {};
    const keys = Object.keys(params);
    const fields = keys.map(function (key) {
      const raw = String(params[key]);
      const optional = raw.indexOf("?") >= 0;
      const base = raw.replace("?", "");
      const label = (PARAM_LABELS_FOR_FORM[key] || key) + (optional ? "（可选）" : "");
      const inputType = base === "int" ? "number" : "text";
      const placeholder = base === "list" ? "多个用逗号分隔" : (optional ? "可留空" : "必填");
      return '<div class="field" style="display:block;margin-bottom:10px">' +
        '<label style="display:block;font-size:11.5px;color:var(--color-text-secondary);' +
        'margin-bottom:4px">' + esc(label) + "</label>" +
        '<input data-param="' + esc(key) + '" data-kind="' + esc(base) + '"' +
        (optional ? "" : ' data-required="1"') +
        ' type="' + inputType + '" placeholder="' + esc(placeholder) + '"' +
        ' style="width:100%;border:1px solid var(--color-border);border-radius:2px;' +
        'padding:6px 8px;font-size:13px"></div>';
    }).join("");

    const mask = KB.modal(
      '<div class="modal-head"><b>' + esc(spec.label || intent) + "</b></div>" +
      '<div class="modal-body paper">' +
        '<p class="tiny muted">' + esc(spec.summary || "") + "</p>" + fields +
        '<div id="param-error" class="tiny"></div>' +
      "</div>" +
      '<div class="modal-foot">' +
        '<button class="btn sm" data-cancel>取消</button>' +
        '<button class="btn primary sm" data-ok>生成提案</button>' +
      "</div>"
    );

    // 取消：KB.modal 只认 [data-close]，这里显式绑定 [data-cancel]，否则点了没反应
    const cancelButton = mask.querySelector("[data-cancel]");
    if (cancelButton) {
      cancelButton.addEventListener("click", function () {
        if (mask.parentNode) mask.remove();
      });
    }
    mask.querySelector("[data-ok]").addEventListener("click", function () {
      const errorBox = mask.querySelector("#param-error");
      const collected = {};
      let failure = "";
      mask.querySelectorAll("[data-param]").forEach(function (input) {
        if (failure) return;
        const key = input.getAttribute("data-param");
        const kind = input.getAttribute("data-kind");
        const required = input.hasAttribute("data-required");
        const value = input.value.trim();
        if (!value) {
          if (required) failure = "请填写：" + (PARAM_LABELS_FOR_FORM[key] || key);
          return;
        }
        if (kind === "int") {
          const number = Number(value);
          if (!Number.isInteger(number) || number <= 0) {
            failure = (PARAM_LABELS_FOR_FORM[key] || key) + " 必须是正整数";
            return;
          }
          collected[key] = number;
          return;
        }
        if (kind === "list") {
          collected[key] = value.split(",").map(function (item) {
            return item.trim();
          }).filter(Boolean);
          return;
        }
        collected[key] = value;
      });
      if (failure) {
        errorBox.innerHTML = '<span class="err-text">' + esc(failure) + "</span>";
        return;
      }
      mask.remove();
      propose(intent, collected);
    });
  }

  /** 待办操作：服务端里**未绑定会话**、且尚未结束的操作（持久，不是临时 DOM）。 */
  async function loadPendingOps(box) {
    if (!box) return;
    const rows = await loadOperations();
    if (rows === null) {
      box.textContent = "（待办操作读取失败：可重试。这不等于「没有待办」）";
      return;
    }
    const pending = rows.filter(function (op) {
      return !op.conversation_id &&
        ["proposed", "confirmed_1", "confirmed_2", "armed", "executing"].indexOf(op.status) >= 0;
    });
    if (!pending.length) {
      box.textContent = "（暂无待办操作）";
      return;
    }
    box.innerHTML = "";
    pending.forEach(function (op) {
      const host = document.createElement("div");
      host.innerHTML = renderOperationCard(op);
      box.appendChild(host.firstChild);
    });
    // innerHTML 被清空后必须**允许重新绑定**（dataset 标志留在 box 上，
    // 若不清掉，新卡片的按钮就永远没有事件）。
    box.dataset.kbBound = "";
    bindHome(box);
    KB.progress.mount();
  }

  async function loadJobs(box) {
    if (!box) return;
    const result = await assistantApi("/api/v1/planner/deletions/");
    if (!result.ok) {
      box.textContent = "（清理任务读取失败：" + KB.errorText(result, "请求失败") +
        "；这不等于「没有清理任务」）";
      return;
    }
    const rows = (result.body && result.body.results) || [];
    if (!rows.length) {
      box.textContent = "（暂无清理任务）";
      return;
    }
    box.innerHTML = "";
    rows.forEach(function (job) {
      const item = document.createElement("div");
      item.className = "row";
      item.style.marginBottom = "6px";
      const label = document.createElement("span");
      label.textContent = (job.intent || job.target_kind) + " · " + job.target_ref +
        " · " + (job.complete ? "已完成" : "未完成");
      item.appendChild(label);
      if (!job.complete) {
        const retry = document.createElement("button");
        retry.className = "btn sm";
        retry.textContent = "重试清理";
        retry.addEventListener("click", async function () {
          retry.disabled = true;
          const done = await assistantApi("/api/v1/planner/deletions/" + job.id + "/", {
            method: "POST", json: {},
          });
          KB.toast(done.ok && done.body.complete ? "清理已完成" : "仍未清理干净，可再重试");
          await loadJobs(box);
        });
        item.appendChild(retry);
      }
      box.appendChild(item);
    });
  }

  /** 手动提案：生成操作卡贴回会话（需要确认的等用户逐次确认）。 */
  async function propose(intent, params) {
    const result = await assistantApi("/api/v1/planner/operations/propose/", {
      method: "POST",
      json: { intent: intent, params: params || {}, conversation_id: conversationId },
    });
    if (!result.ok) {
      KB.toast((result.body && result.body.detail) || "无法生成提案");
      return;
    }
    if (conversationId) {
      // 有真实会话：重新拉取该会话（操作卡来自服务端 operations），
      // 不做"瞬时 DOM 假卡"——刷新后仍然在。
      await renderHome();
      KB.toast("已生成操作提案，请在会话里确认");
      return;
    }
    // 没有会话：交给**服务端待办**（/planner/operations/ 里 conversation 为空的操作），
    // 在待办操作弹窗里持久展示，而不是贴在临时 DOM 上。
    KB.toast("已生成待办操作，可在「待办操作」里确认");
    openTools();
  }

  // -------------------------------------------------------- 文档侧栏 ----

  function toggleDocs(open) {
    const pane = $("doc-pane");
    const main = $("chat-main");
    const button = $("btn-docs");
    const shouldOpen = open === undefined ? !pane.classList.contains("open") : open;
    // 收起面板时若有未保存改动：先确认（否则正文就那样消失了，用户以为"白写了"）
    if (!shouldOpen && pane.classList.contains("open") && isDocDirty()) {
      confirmDiscardDocEdits("收起文档面板").then(function (okay) {
        if (okay) toggleDocsForced(false);
      });
      return;
    }
    toggleDocsForced(shouldOpen);
  }

  function toggleDocsForced(shouldOpen) {
    const pane = $("doc-pane");
    const main = $("chat-main");
    const button = $("btn-docs");
    pane.classList.toggle("open", shouldOpen);
    main.classList.toggle("doc-open", shouldOpen);
    button.textContent = shouldOpen ? "文档 ◂" : "文档 ▸";
    if (shouldOpen) {
      refreshDocPane();
      renderPreview();
      updateDocUI();
      // 每次都重读模板列表（一次很小的 GET）：既能看到别人新传的模板，
      // 也给了"上次读取失败"一个天然的**重试**入口 —— 否则失败态会一直挂在那里。
      loadTemplates(true);
    }
  }

  /** 面板打开时：列出草稿，并在"当前没打开任何草稿"时自动选中最新一份。
   *
   * 两个真实问题（用户实测反馈）：
   * 1. 打开面板只看到占位文字「打开一份文档后可在此编辑」，底栏「删除草稿」
   *    因为 ``currentDocId`` 为空只会弹一句"请先打开一份文档"；
   * 2. 列表里**没有行内删除入口**，要删一份草稿必须先点标题打开它，
   *    用户于是认为"删不掉"。现在两者都补齐：行内 ✕ 直接删，且默认打开最新一份。
   */
  async function refreshDocPane() {
    const ticket = ++docOpenTicket;
    const rows = await listDocuments();
    // 期间有人显式打开过文档（票变了）→ 不抢，免得把刚生成的那份顶掉
    if (ticket !== docOpenTicket) return;
    // 不覆盖用户正在输入的内容：只有编辑器是空的才自动打开；
    // 也不能在有未保存改动时抢（那种情况连"打开"都要先确认）
    if (!currentDocId && !editorHasInput() && !isDocDirty() && rows.length) {
      await openDocument(rows[0].id, { skipGuard: true });
    } else {
      updateDocUI();
    }
  }

  function editorHasInput() {
    return Boolean(
      $("doc-title").value.trim() ||
      $("doc-topic").value.trim() ||
      $("doc-body").value.trim()
    );
  }

  /** 当前编辑器内容（保存/脏检查/导出都用它，避免三处各读一遍 DOM）。 */
  function readEditor() {
    return {
      title: $("doc-title").value,
      topic: $("doc-topic").value,
      body: $("doc-body").value,
      format: KB.docFormat.signature(KB.docFormat.read()),
    };
  }

  /** 把"这一次打开/保存成功时的内容"记成基线（脏检查的参照物）。 */
  function setBaselineFromEditor() {
    docBaseline = readEditor();
  }

  /** 编辑器是否与"已保存的那一版"不同（含标题/主题，不只是正文）。 */
  function isDocDirty() {
    const now = readEditor();
    return now.title !== docBaseline.title ||
      now.topic !== docBaseline.topic ||
      now.body !== docBaseline.body || now.format !== docBaseline.format;
  }

  /** 记一条"一次性结果"（成功/失败/警告），显示在状态条上。 */
  function setDocFlash(kind, text) {
    docFlash = { kind: kind, text: text };
    updateDocUI();
  }

  /**
   * 兼容入口：把老的 ``setDocStatus(kind, text)`` 语义映射到 busy/flash 模型。
   *
   * 为什么保留它：状态条必须**只由一处刷新**（updateDocUI），而 busy/flash
   * 才不会被普通刷新按 dirty 重算掉。"进行中"用 busy，"结果/警告"用 flash。
   */
  function setDocStatus(kind, text) {
    if (kind === "saving") {
      docBusy = { kind: "saving", text: text };
      docFlash = null;
    } else if (kind === "error") {
      docBusy = null;
      docFlash = { kind: "error", text: text };
    } else if (kind === "saved") {
      docBusy = null;
      docFlash = { kind: "saved", text: text };
    } else if (kind === "dirty" || kind === "warn") {
      docBusy = null;
      docFlash = { kind: "warn", text: text };
    } else {
      docBusy = null;
      docFlash = null;
    }
    updateDocUI();
  }

  /**
   * 刷新文档面板的**可见状态与按钮可用性**（唯一的 UI 同步点）。
   *
   * 三条硬要求（用户 + Codex 审查）：
   *  1. 已有正文时**不再显示**「生成草稿」（那是"让 AI 写一份新的"，
   *     摆在已写好的正文旁边既没语义又容易误点出重复草稿）；改成「新建草稿」；
   *  2. 每个禁用按钮都要有**理由**：写进 title，也写进底栏 #doc-hint；
   *  3. 保存状态（保存中 / 已保存 / 有未保存的改动 / 保存失败）显示在
   *     **滚动区之外**的 #doc-status 上，不依赖用户滚到最底下才发现。
   *
   * 优先级：**进行中的动作（docBusy）> 一次性结果（docFlash）> 脏 > 已保存 > 空白**。
   * 这样"AI 正在撰写…""已保存""保存失败""部分信息库未检索到"这类消息
   * 不会被下一次普通刷新按 dirty 重算掉（第一轮实现正是这么丢的）。
   */
  function updateDocUI() {
    const hasDoc = Boolean(currentDocId);
    const dirty = hasDoc ? isDocDirty() : editorHasInput();
    const createButton = $("doc-create");
    const newButton = $("doc-new");
    const saveButton = $("doc-save");
    const statusBox = $("doc-status");
    const hint = $("doc-hint");

    // 「生成草稿」= "让 AI 写一份**新**草稿"，只有**还没有打开任何草稿记录**时才出现；
    // 一旦打开了一份草稿（currentDocId 有值），它就换成「新建草稿」——
    // 否则页面右栏会出现"已经写好正文了还摆着生成草稿"的怪状态（用户原话）。
    // 判据是**有没有文档记录**，不是"编辑器有没有字"：用户先填标题/主题再点生成，
    // 恰恰是正常用法（按标题+主题生成）；若按字判断，一填标题按钮就没了（实测踩到）。
    if (createButton) {
      createButton.hidden = hasDoc;
      if (!hasDoc) {
        // 有手写正文时，同一个按钮改叫「保存为新草稿」并**真的保存用户手写的内容**
        // （不叫模型）：既不再是"必须让 AI 覆盖你的手写稿"，也不再是误导文案。
        const handwritten = Boolean($("doc-body").value.trim());
        createButton.textContent = handwritten ? "保存为新草稿" : "生成草稿";
        createButton.title = handwritten
          ? "把你手写的标题/主题/正文直接存成一份草稿（不调用模型，不是 AI 生成）"
          : "按标题/主题让 AI 撰写一份新草稿（会调用模型，几十秒）";
      }
    }
    if (newButton) newButton.hidden = !hasDoc;

    // 状态文字：进行中的动作 > 一次性结果 > 脏 > 已保存 > 空白
    let kind = "idle";
    let text = "空白草稿：填好标题/主题后点「生成草稿」；想自己写就直接写下正文，点「保存为新草稿」";
    if (docBusy) {
      kind = "saving";
      text = docBusy.text;
    } else if (docFlash) {
      kind = docFlash.kind === "warn" ? "dirty" : docFlash.kind;
      text = docFlash.text;
    } else if (hasDoc && dirty) {
      kind = "dirty";
      text = "有未保存的改动";
    } else if (hasDoc) {
      kind = "saved";
      text = "已保存" + (docSavedAt ? " · " + docSavedAt : "") + "（当前编辑器与服务器一致）";
    } else if (dirty) {
      kind = "dirty";
      text = "未保存：还没有文档记录（有手写正文就点「保存为新草稿」；要 AI 写就点「生成草稿」）";
    }
    if (statusBox) {
      statusBox.className = "doc-status " + kind;
      statusBox.textContent = text;
    }

    // 按钮可用性 + 理由
    const busy = Boolean(docBusy);
    if (saveButton) {
      if (!hasDoc) {
        saveButton.disabled = true;
        saveButton.title = "还没有草稿记录：手写内容用「保存为新草稿」，AI 写作先用「生成草稿」";
      } else if (busy) {
        saveButton.disabled = true;
        saveButton.title = "正在" + (docBusy.kind === "saving" ? "保存" : "处理") + "，完成后可再保存";
      } else if (!dirty) {
        saveButton.disabled = true;
        saveButton.title = "没有未保存的改动（编辑后这里会自动变成可点）";
      } else {
        saveButton.disabled = false;
        saveButton.title = "把标题、主题与正文写入服务器（只改这份草稿，来源资料不动）";
      }
      saveButton.classList.toggle("primary", Boolean(hasDoc && dirty && !busy));
    }
    ["#doc-download", "#doc-download-html", "#doc-download-docx", "#doc-download-pdf", "#doc-download-pptx"]
      .forEach(function (selector) {
        const button = document.querySelector(selector);
        if (!button) return;
        button.disabled = !hasDoc || busy;
        if (!hasDoc) {
          button.title = "先打开或生成一份草稿，才能导出（导出会先保存当前内容，保证导出的是眼前这一版）";
        } else {
          button.title = button.getAttribute("data-base-title") ||
            "导出当前编辑器里的这一版（有未保存改动会先保存）";
        }
      });
    const deleteButton = $("doc-delete");
    if (deleteButton) {
      deleteButton.disabled = !hasDoc;
      deleteButton.title = hasDoc
        ? "删除这份派生草稿（不影响来源资料与原件）"
        : "先打开一份草稿才能删除；列表里每一行右侧的 ✕ 可以直接删";
    }

    // 底栏一行说明：写清"现在能做什么/为什么某按钮是灰的"
    if (hint) {
      let text2 = "";
      let warn = false;
      if (docHint) {
        text2 = docHint;
      } else if (!hasDoc) {
        text2 = editorHasInput()
          ? "还没有文档记录：点「生成草稿」让 AI 按标题/主题写一份新草稿（当前手写内容会作为新草稿的起点被覆盖，请先确认）。"
          : "空白草稿：填标题/主题 → 生成草稿；生成后即可编辑、预览、保存与导出。";
      } else if (dirty) {
        text2 = "改动还没保存：切到别的草稿、换会话或收起面板前都会先问你一次。";
        warn = true;
      } else {
        text2 = "当前内容已与服务器一致；「下载/导出」会按这一版生成文件。";
      }
      hint.textContent = text2;
      hint.classList.toggle("warn", warn);
    }
  }

  /** 「编辑 / 预览」两个互斥视图的切换（预览按**当前编辑器内容**重渲染）。 */
  function switchDocTab(which) {
    const edit = which !== "preview";
    $("doc-tab-edit").classList.toggle("on", edit);
    $("doc-tab-preview").classList.toggle("on", !edit);
    $("doc-tab-edit").setAttribute("aria-selected", edit ? "true" : "false");
    $("doc-tab-preview").setAttribute("aria-selected", edit ? "false" : "true");
    $("doc-edit-panel").hidden = !edit;
    $("doc-preview-panel").hidden = edit;
    if (!edit) renderPreview();
  }

  /** 有未保存改动时先征求确认；返回 true = 可以继续（并且**真的**把改动丢弃）。 */
  async function confirmDiscardDocEdits(what) {
    if (!isDocDirty()) return true;
    const title = $("doc-title").value.trim() || "（未命名草稿）";
    const answer = await KB.confirmDialog({
      title: "有未保存的改动",
      body: what + "会丢掉这份草稿里还没保存的改动。要先保存吗？" +
        "（点「取消」返回继续编辑；确定丢弃后无法恢复）",
      targetLabel: title,
      confirmLabel: "丢弃改动并继续",
    });
    if (!answer || !answer.confirmed) return false;
    // **确认丢弃就必须真的丢弃**（Codex 审查：旧实现只 return true，编辑器里
    // 还是那份"已被丢弃"的文字，关掉再打开又出现，beforeunload 还会再弹一次）。
    // 口径：恢复到"上一次保存/打开时的那一版"（没有文档记录时就是清空）。
    $("doc-title").value = docBaseline.title;
    $("doc-topic").value = docBaseline.topic;
    $("doc-body").value = docBaseline.body;
    KB.docFormat.set(JSON.parse(docBaseline.format || "{}"));
    docEditRevision += 1;
    if (!currentDocId && !editorHasInput()) {
      renderPreview();
      docFlash = null;
    }
    renderPreview();
    updateDocUI();
    return true;
  }

  /** 「新建草稿」：清空编辑器，回到"空白草稿"（先确认未保存改动）。 */
  async function startNewDraft() {
    if (!(await confirmDiscardDocEdits("新建草稿"))) return;
    currentDocId = null;
    currentDocTemplateCategory = "";
    docOpenTicket += 1;
    // **作废所有未完成的加载**：否则一个还在路上的 GET 回来会把新建的空白
    // 编辑器又填上旧文档（Codex 第三批 B）。
    docLoadTicket += 1;
    docSavedAt = "";
    docHint = "";
    docBusy = null;
    docFlash = null;
    // 新建草稿不能继承上一份草稿/上一次会话的「指定来源」（AF-13）
    clearChosenSources();
    clearDocumentEditor();
    setBaselineFromEditor();
    switchDocTab("edit");
    setDocStatus("idle");
    await listDocuments();
  }

  /** 从 Content-Disposition 里取文件名（RFC 5987 filename* 优先，中文原样）。 */
  function filenameFromDisposition(header, fallback) {
    const value = String(header || "");
    const star = value.match(/filename\*=UTF-8''([^;]+)/i);
    if (star) {
      try { return decodeURIComponent(star[1].trim()); } catch (err) { /* 回落 */ }
    }
    const plain = value.match(/filename="?([^";]+)"?/i);
    return plain ? plain[1] : fallback;
  }

  /**
   * 导出：**必须先保存再导出**（导出要的是眼前这一版，不是服务器上的旧版），
   * 用 fetch + Blob 下载（不再 `location.href` —— 导出失败时那会导航到 JSON
   * 错误页，用户看不到原因），失败原因原样显示在状态条与 toast 里。
   */
  async function downloadDoc(format, button) {
    if (!currentDocId) {
      setDocStatus("error", "还没有可导出的草稿：先打开或生成一份");
      return;
    }
    // **冻结导出目标**：保存/导出期间用户可能切到另一份草稿，那时用
    // `currentDocId` 就会导出别的东西（Codex 审查）。
    const targetId = currentDocId;
    if (isDocDirty()) {
      const saved = await saveDocument();
      if (!saved) {
        setDocStatus("error", "导出已取消：当前改动保存失败，导出会拿到旧版本");
        return;
      }
    }
    if (currentDocId !== targetId) {
      setDocStatus("error", "导出已取消：你在保存期间切换到了另一份草稿；请对当前这份重新点导出");
      return;
    }
    if (isDocDirty()) {
      // 保存期间用户又输入了新内容 → 现在导出的仍然是旧版本，必须停手说清楚
      setDocStatus("error",
        "导出已取消：保存期间你又改了内容（导出必须是你眼前的这一版）。" +
        "请等保存完成、状态条显示「已保存」后再导出");
      return;
    }
    const url = "/api/v1/planner/documents/" + targetId +
      "/download/?export_format=" + encodeURIComponent(format);
    const label = button ? button.textContent : "";
    const exportProgress = KB.progress.start("doc-action-progress", "正在导出 " + format + " 文档");
    let exportSucceeded = false;
    docBusy = { kind: "exporting", text: "导出中…（" + format + "）" };
    if (button) { button.disabled = true; button.textContent = "导出中…"; }
    try {
      const response = await fetch(url, { credentials: "same-origin" });
      if (!response.ok) {
        let detail = "HTTP " + response.status;
        let code = "";
        try {
          const body = await response.json();
          if (body && body.detail) detail = body.detail;
          if (body && body.code) code = body.code;
        } catch (err) { /* 非 JSON：用状态码 */ }
        // **未认证不能把登录页当文件存下来**：明确跳登录（Codex 追加审核 1）。
        if (response.status === 401 || code === "not_authenticated") {
          setDocFlash("error", "登录已过期，正在跳转到登录页…本次导出已取消");
          window.location.href = "/app/login/?next=" +
            encodeURIComponent(window.location.pathname + window.location.search);
          return;
        }
        setDocStatus("error", "导出失败（" + format + "）：" + detail);
        KB.toast("导出失败：" + detail);
        return;
      }
      const blob = await response.blob();
      const fallback = (($("doc-title").value.trim() || "文档") + "." + format);
      const name = filenameFromDisposition(
        response.headers.get("Content-Disposition"), fallback);
      const objectUrl = URL.createObjectURL(blob);
      const anchor = document.createElement("a");
      anchor.href = objectUrl;
      anchor.download = name;
      document.body.appendChild(anchor);
      anchor.click();
      anchor.remove();
      setTimeout(function () { URL.revokeObjectURL(objectUrl); }, 10000);
      setDocStatus("saved", "已导出 " + name + "（" + Math.max(1, Math.round(blob.size / 1024)) + " KB）");
      KB.toast("已导出 " + name);
      exportSucceeded = true;
    } catch (err) {
      setDocStatus("error", "导出失败：" + ((err && err.message) || "未知错误"));
      KB.toast("导出失败，请重试");
    } finally {
      exportProgress.finish(exportSucceeded, exportSucceeded ? "文档已导出" : "导出未完成");
      docBusy = null;
      if (button) { button.disabled = !currentDocId; button.textContent = label; }
      updateDocUI();
    }
  }

  async function listDocuments() {
    const box = $("doc-list");
    const result = await assistantApi("/api/v1/planner/documents/");
    if (!result.ok) {
      // **接口失败不能渲染成"还没有"**（处置清单 AF-07）：空态与失败是两件事，
      // 混在一起用户会以为自己的草稿没了，而其实只是这次请求失败。
      box.innerHTML = '<div class="tiny" style="color:#a33">草稿列表读取失败：' +
        esc(KB.errorText(result, "请求失败")) + "（可重试；这不是「还没有草稿」）</div>";
      updateDocUI();
      return [];
    }
    const rows = (result.body && result.body.results) || [];
    if (!rows.length) {
      box.innerHTML = '<div class="tiny muted">还没有派生文档</div>';
      return rows;
    }
    box.innerHTML = "";
    rows.forEach(function (row) {
      // 一行 = 「打开」按钮 + 行内「删除」按钮。
      // 行内删除是**新增入口**（原来只能在底栏删"当前打开的那一份"）：
      // 列表里每一份草稿都能就地删掉，不用先打开。
      const line = document.createElement("div");
      line.className = "doc-list-row";
      const button = document.createElement("button");
      button.className = "doc-list-item" + (row.id === currentDocId ? " on" : "");
      button.textContent = row.title + " · " + documentStatusLabel(row);
      button.title = row.title;
      button.addEventListener("click", function () { openDocument(row.id); });
      const remove = document.createElement("button");
      remove.className = "doc-del";
      remove.type = "button";
      remove.setAttribute("data-del-doc", String(row.id));
      remove.title = "删除这份派生草稿（来源资料与原始文件不会被删除）";
      remove.textContent = "✕";
      remove.addEventListener("click", function (event) {
        // 行内按钮和"打开"按钮是兄弟节点，不冒泡，但显式停一下更稳妥
        event.stopPropagation();
        deleteDocumentById(row.id, row.title);
      });
      line.appendChild(button);
      line.appendChild(remove);
      box.appendChild(line);
    });
    return rows;
  }

  /**
   * 打开一份草稿。
   *
   * 三处并发/丢数据防护（Codex 独立审查点名）：
   *  1. **乱序返回**：连点 A、B 两份草稿时，慢的 A 可能后到并覆盖 B。
   *     这里给每次加载发一张票（``docLoadTicket``），回来时票不是最新的就丢弃；
   *  2. **未保存改动**：切文档前先确认（见 ``confirmDiscardDocEdits``）；
   *  3. **失败必须可见**：旧实现失败就 ``return``，界面毫无反应 ——
   *     现在把服务端原因写进状态条。
   */
  async function openDocument(id, options) {
    const opts = options || {};
    if (!opts.skipGuard && !(await confirmDiscardDocEdits("切换到另一份草稿"))) return false;
    // 显式打开：作废任何"待自动打开"的申请
    docOpenTicket += 1;
    const ticket = ++docLoadTicket;
    // **请求发出时的编辑版本**：响应回来若版本变了，说明用户在等待期间又打了字
    // （Codex 第三批 B：旧实现会拿响应直接覆盖，静默丢掉用户刚写的字）
    const revisionAtRequest = docEditRevision;
    const result = await assistantApi("/api/v1/planner/documents/" + id + "/");
    // 期间用户又点了别的草稿 → 这次的结果已经过期，丢弃（不能覆盖新选的那份）
    if (ticket !== docLoadTicket) return false;
    if (!result.ok) {
      setDocStatus("error", "打开失败：" + KB.errorText(result, "无法读取这份草稿"));
      return false;
    }
    if (!opts.force && docEditRevision !== revisionAtRequest) {
      // 不覆盖编辑器：把话说明白，并把这份草稿留到列表里让用户自己决定
      setDocFlash("warn",
        "你刚才在等待打开时又改了内容，编辑器保持原样（没有用服务器版本覆盖）。" +
        "要打开「" + (result.body.title || ("#" + id)) + "」请再点一次列表里的它（会先确认丢弃当前改动）");
      await listDocuments();
      return false;
    }
    currentDocId = id;
    // 预览要按模板口径排版：公文模板（category=official）走公文排印，
    // 其余模板/无模板走通用 Markdown 渲染（见 renderPreview / .doc-render.is-official）。
    currentDocTemplateCategory = result.body.template_category || "";
    // 打开的草稿是**另一份**：来源勾选必须跟着它清空（AF-13）。
    // 它在服务端已经固化了 `sources`（显示在 #doc-sources），面板里的勾选
    // 只影响**下一份**草稿；留着旧勾选等于悄悄改写下一份的检索范围。
    clearChosenSources();
    $("doc-title").value = result.body.title || "";
    $("doc-topic").value = result.body.topic || "";
    $("doc-body").value = result.body.content_markdown || "";
    KB.docFormat.set(result.body.format_options || {});
    const sources = result.body.sources || [];
    $("doc-sources").textContent = "来源：" + sources.length + " 份" +
      (sources.length
        ? "（" + sources.map(function (item) { return item.name; }).join("、") + "）"
        : "");
    docSavedAt = KB.fmtTime ? KB.fmtTime(result.body.updated_at) : "";
    docHint = "";
    // 用户主动打开时清掉上一条一次性结果；生成流程自己会在之后设置提示
    if (!opts.keepFlash) docFlash = null;
    setBaselineFromEditor();
    switchDocTab("edit");
    renderPreview();
    updateDocUI();
    await listDocuments();
    return true;
  }

  // 公文的分层靠「一、」「（一）」「1.」标出，不是 Markdown 标题；落款单位与成文日期
  // 也要居右。预览与 Word 导出用同一套口径：这里给渲染出来的段落打类，样式见
  // .paper.is-official .gb-h1/.gb-h2/.gb-h3/.gb-signature。
  const GB_H1 = /^[一二三四五六七八九十百]+、/;
  const GB_H2 = /^（[一二三四五六七八九十百]+）/;
  const GB_H3 = /^\d+[．.]\s*\S/;
  const GB_FILE = /\.(pdf|docx?|xlsx?|pptx?|txt|md|csv|jpe?g|png)$/i;
  const GB_DATE = /^\s*(\d{4}\s*年\s*\d{1,2}\s*月\s*\d{1,2}\s*日|\d{4}-\d{1,2}-\d{1,2})\s*$/;

  function decorateOfficialPreview(root) {
    if (!root) return;
    const nodes = Array.prototype.slice.call(root.children);
    const lines = [];
    nodes.forEach(function (el) {
      const tag = (el.tagName || "").toLowerCase();
      if (!/^h[1-5]$/.test(tag) && tag !== "p") return;
      const text = (el.textContent || "").trim();
      if (text) lines.push({ el: el, text: text, tag: tag });
    });
    // 1) 分条标题：只认短行、不以标点收尾、且不是文件名（附件清单）
    lines.forEach(function (item) {
      if (item.el.classList.contains("gb-title")) return;
      if (item.text.length > 40 || /[。；，！？：]$/.test(item.text)) return;
      if (GB_FILE.test(item.text)) return;
      if (GB_H1.test(item.text)) item.el.classList.add("gb-h1");
      else if (GB_H2.test(item.text)) item.el.classList.add("gb-h2");
      else if (GB_H3.test(item.text)) item.el.classList.add("gb-h3");
    });
    // 2) 落款：最后一个成文日期行 + 它上面那行单位名（短行、不以标点收尾）
    for (let i = lines.length - 1; i >= 0; i -= 1) {
      if (!GB_DATE.test(lines[i].text)) continue;
      lines[i].el.classList.add("gb-signature");
      const prev = i > 0 ? lines[i - 1] : null;
      if (prev && prev.tag === "p" && prev.text.length <= 60 &&
          !/[。；，！？]$/.test(prev.text)) {
        prev.el.classList.add("gb-signature");
      }
      break;
    }
    // 3) 首行标题（Markdown `#` 被渲染成 h2）按公文标题排
    const first = nodes.find(function (el) {
      return /^h[1-5]$/.test((el.tagName || "").toLowerCase());
    });
    if (first) first.classList.add("gb-title");
  }

  // Markdown 会把相邻两行合并成一段，而公文要求"落款单位"和"成文日期"各占一段；
  // 预览前先给最后那几行落款补空行（只影响预览，不改编辑器里的正文）。
  function prepareOfficialMarkdown(text) {
    const lines = (text || "").split("\n");
    const title = ($("doc-title").value || "").trim();
    while (lines.length && !lines[0].trim()) lines.shift();
    if (lines.length && lines[0].replace(/^#{1,6}\s+/, "").replace(/\*\*|__|`/g, "").trim() === title) {
      lines.shift();
    }
    if (title) lines.unshift("# " + title, "");
    // 模型可能只在分条标题后换一行；补段落边界，避免标题和正文合成同一段。
    for (let i = lines.length - 1; i >= 0; i -= 1) {
      const line = lines[i].trim();
      if (line.length <= 40 && !/[。；，！？：]$/.test(line) && !GB_FILE.test(line) &&
          (GB_H1.test(line) || GB_H2.test(line) || GB_H3.test(line))) {
        lines.splice(i + 1, 0, "");
        lines.splice(i, 0, "");
      }
    }
    for (let i = lines.length - 1; i >= 0; i -= 1) {
      const trimmed = (lines[i] || "").trim();
      if (!GB_DATE.test(trimmed)) continue;
      lines.splice(i, 0, "");
      const prevIndex = i - 1;
      const prev = prevIndex >= 0 ? (lines[prevIndex] || "").trim() : "";
      if (prev && prev.length <= 60 && !/[。；，！？]$/.test(prev) &&
          !/^[#>*\-]/.test(prev)) {
        lines.splice(prevIndex, 0, "");
      }
      break;
    }
    return lines.join("\n");
  }

  function renderPreview() {
    const target = $("doc-render");
    const official = currentDocTemplateCategory === "official";
    if (target) {
      // 公文模板的预览要"像公文"：标题居中、正文仿宋三号、首行缩进两字符、
      // 固定 28 磅行距（口径与模板原件一致）。非公文模板保持通用 Markdown 排版。
      target.classList.toggle("is-official", official);
    }
    const raw = $("doc-body").value || "";
    if (KB.renderMarkdownInto) {
      KB.renderMarkdownInto("doc-render", official ? prepareOfficialMarkdown(raw) : raw);
    }
    if (official) decorateOfficialPreview(target);
    KB.docFormat.apply();
  }

  // 防重复生成：模型撰写一次几十秒（文档 profile 超时 180s），
  // 期间用户很容易再点一次 —— 那会并发发起多次模型调用、生成多份文档。
  // 用一个模块内标志 + 按钮禁用双保险（标志防"程序化触发"，禁用给出可见反馈）。
  let docGenerating = false;

  async function createDocument() {
    if (docGenerating) return;
    // 已打开一份草稿时**不允许**用生成去顶掉它（用户必须自己先点「新建草稿」，
    // 那里会拦一次未保存改动）。这样 AI 永远不会覆盖用户正在编辑的内容。
    if (currentDocId) {
      setDocStatus("error",
        "已打开一份草稿：如要让 AI 另写一份，请先点「新建草稿」（会先确认未保存改动）");
      return;
    }
    // **有手写正文时这个按钮的含义是"保存为新草稿"**（用户自己的稿子，不叫模型）：
    // 文案误导问题（用户："既然正文已经生成，生成草稿有啥用"）在这里根治 ——
    // 一方面按钮在已有草稿时不出现，另一方面空白态下手写内容走**保存**而不是
    // "让 AI 覆盖你写的正文"。
    const hasHandwritten = Boolean($("doc-body").value.trim());
    if (hasHandwritten) {
      await saveNewAuthoredDocument();
      return;
    }
    const title = $("doc-title").value.trim();
    const topic = $("doc-topic").value.trim();
    if (!title && !topic) {
      setDocStatus("error", "请先填标题或主题，再点「生成草稿」");
      KB.notice($("doc-notice"), "请先填标题或主题", "warn");
      return;
    }
    docGenerating = true;
    const trigger = $("doc-create");
    if (trigger) trigger.disabled = true;
    setDocStatus("saving", "AI 正在撰写新草稿…（这一步会叫一次模型，可能几十秒）");
    // 生成期间用户若继续输入/切走，回来时**不许**顶掉他的编辑器（Codex 第三批）
    const revisionAtRequest = docEditRevision;
    const ticketAtRequest = docLoadTicket;
    try {
    // **未勾选任何来源时必须省略该字段**（而不是传空数组）：
    // 服务端把"显式给了来源"视为强约束 —— 空数组是**非法**的（400），
    // 传了就会把正常的"全库写作"打成错误。省略字段 = 按主题全库检索。
    const docPayload = {
      title: title || topic,
      topic: topic,
    };
    const pickedSources = Array.from(chosenSources).map(Number);
    if (pickedSources.length) {
      docPayload.source_file_ids = pickedSources;
    }
    // 文档模板（可选）：**不选就不发这个字段** = 按通用格式撰写（用户要求的默认行为）。
    docPayload.format_options = KB.docFormat.read();
    const templateSelect = $("doc-template");
    if (templateSelect && templateSelect.value) {
      docPayload.template_id = Number(templateSelect.value);
    }
    const result = await assistantApi("/api/v1/planner/documents/", {
      method: "POST",
      json: docPayload,
    });
    if (result.status === 503) {
      // **保留服务端的真实原因**：后端会把"为什么不可用"讲清楚
      // （例如"被 token 上限截断 / 推理占用预算"）。早先这里写死一句
      // "智能助手当前不可用"，把具体原因**丢掉了** —— 用户只能看到笼统的
      // "不可用"，误以为是模型挂了。现在把 detail 原样带出来。
      const detail = (result.body && result.body.detail) || "未提供原因";
      setDocStatus("error", "智能助手当前不可用，本次没有生成草稿。原因：" + detail);
      KB.notice($("doc-notice"),
        "智能助手当前不可用，暂时无法撰写新文档（已入库资料未受影响）。原因：" +
        detail, "warn");
      return;
    }
    if (!result.ok) {
      const detail = KB.errorText(result, "撰写失败");
      setDocStatus("error", "撰写失败：" + detail);
      KB.notice($("doc-notice"), detail, "err");
      return;
    }
    if (result.body.retrieval_degraded) {
      // 这条"只检索到部分信息库"的警告必须**留在状态条上**（它是结果的一部分），
      // 不能被随后的 openDocument/setDocStatus("saved") 覆盖（Codex 审查点名）。
      pendingGenerationFlash = {
        kind: "warn",
        text: "草稿已生成，但本次只检索到部分信息库（" +
          (result.body.retrieval_degraded_reason || "部分向量检索不可用") +
          "），内容可能不完整。",
      };
      KB.notice($("doc-notice"),
        "草稿已生成，但本次只检索到部分信息库（" +
        (result.body.retrieval_degraded_reason || "部分向量检索不可用") +
        "），内容可能不完整。", "warn");
    } else {
      KB.notice($("doc-notice"), "已生成草稿，可编辑后保存或下载。", "ok");
    }
    // **生成期间用户又编辑过 / 又点了别的草稿** → 不打开新草稿，也别动他的编辑器
    if (docEditRevision !== revisionAtRequest || docLoadTicket !== ticketAtRequest) {
      await listDocuments();
      setDocFlash("warn",
        "AI 已写好一份新草稿「" + (result.body.title || "") +
        "」，但你在生成期间又编辑了内容，编辑器保持原样（没有覆盖）。" +
        "想打开它请点列表里的这一份（会先确认丢弃当前改动）");
      return;
    }
    // 打开这份**新**草稿：它绝不会覆盖用户已打开的旧草稿（那是另一条记录）
    const opened = await openDocument(result.body.document_id,
                                     { skipGuard: true, keepFlash: true });
    docHint = "";
    if (!opened) {
      await listDocuments();
      setDocFlash("warn",
        "AI 写好了一份新草稿「" + (result.body.title || "") +
        "」，但没有成功打开它；请点列表里的这一份再试");
      return;
    }
    // **消费**生成过程中攒下的提示（例如"只检索到部分信息库"）：
    // 不能用一句"已生成"把它盖掉，否则那条降级警告就只剩滚动区底部的不可见小字。
    setDocFlash(pendingGenerationFlash ? pendingGenerationFlash.kind : "saved",
                pendingGenerationFlash
                  ? pendingGenerationFlash.text
                  : "已生成新草稿（尚未编辑）：可直接编辑、预览或导出" +
                    (result.body.template_name
                      ? "；已套用模板「" + result.body.template_name + "」"
                      : "（未使用模板）"));
    pendingGenerationFlash = null;
    } finally {
      // 无论成功、失败还是提前 return，都必须解除占用，
      // 否则用户被永久锁死、再也点不动"撰写"。
      docGenerating = false;
      docBusy = null;
      if (trigger) trigger.disabled = false;
      updateDocUI();
    }
  }

  /** 生成流程里要保留的提示（避免被判成"被覆盖"）。 */
  let pendingGenerationFlash = null;

  /**
   * 「保存为新草稿」：把用户**手写**的标题/主题/正文直接落成一份派生文档。
   *
   * 为什么不走 AI：按钮文案不能既不诚实又做不到 —— 面板提示写着手写，按钮却只
   * 能"让 AI 覆盖"，而且没有文档记录时「保存」永远禁用（Codex 审查点名）。
   * 这里走 POST /documents/ 的 ``content_markdown`` 分支：服务端直接建记录、不叫模型。
   */
  async function saveNewAuthoredDocument() {
    // 与普通保存同一套并发保护（Codex 审查：新路径不能又引入同一个丢字缺陷）：
    // 互斥 + 冻结提交内容 + 记录编辑版本，响应回来若用户又编辑过就不覆盖编辑器。
    if (docBusy) return;
    const title = $("doc-title").value.trim();
    const topic = $("doc-topic").value.trim();
    const snapshot = readEditor();
    const body = snapshot.body;
    if (!title) {
      setDocStatus("error", "保存为新草稿需要标题（导出文件名与列表都取它）");
      return;
    }
    const revisionAtRequest = docEditRevision;
    docBusy = { kind: "saving", text: "正在保存手写草稿…" };
    docFlash = null;
    updateDocUI();
    try {
      const result = await assistantApi("/api/v1/planner/documents/", {
        method: "POST",
        json: { title: title, topic: topic, content_markdown: body, format_options: KB.docFormat.read() },
      });
      if (!result.ok) {
        setDocStatus("error", "保存为新草稿失败：" + KB.errorText(result, "保存失败"));
        return;
      }
      if (docEditRevision !== revisionAtRequest) {
        // 请求期间用户又改了：**不打开**新草稿（打开会把他的新输入顶掉）
        await listDocuments();
        setDocFlash("warn",
          "手写草稿已保存为「" + (result.body.title || "") +
          "」，但你在保存期间又改了内容，编辑器保持原样；" +
          "要打开这份草稿请点列表里的它（会先确认丢弃当前改动）");
        return;
      }
      const opened = await openDocument(result.body.document_id,
                                       { skipGuard: true, keepFlash: true });
      if (opened) {
        setDocFlash("saved",
          "已保存为你自己写的草稿（非 AI 生成）：可继续编辑、预览或导出");
      } else {
        await listDocuments();
        setDocFlash("warn", "草稿已保存，但打开它没有成功；请点列表里的这一份再试");
      }
    } finally {
      docBusy = null;
      updateDocUI();
    }
  }

  /**
   * 保存：把**标题、主题、正文**一起写入服务器（旧实现只发正文，
   * 于是标题/主题改完是"假保存" —— Codex 独立审查点名）。
   *
   * **并发正确性（Codex 第三批 A）**：请求发出前把"要提交的那一版"冻结成
   * ``snapshot`` 并记下目标 id；成功后的基线**只用服务端确认的这一版** ——
   * 若用户在保存期间又输入了新内容，那份新内容必须继续算"未保存"，
   * 绝不能因为"保存返回成功"就把新输入也算成已保存。目标若已被切换，
   * 不能拿这次结果去污染新目标的基线/状态。
   *
   * 返回 true 表示确实保存成功（导出流程据此决定要不要继续）。
   */
  async function saveDocument() {
    if (!currentDocId) {
      setDocStatus("error", "还没有可保存的草稿：手写内容请点「保存为新草稿」");
      return false;
    }
    if (docBusy) return false;
    if (!isDocDirty()) {
      setDocFlash("saved", "没有需要保存的改动（编辑器与服务器一致）");
      return true;
    }
    const targetId = currentDocId;
    const snapshot = readEditor();
    if (!snapshot.title.trim()) {
      setDocStatus("error", "标题不能为空：导出文件名与文档列表都取标题");
      return false;
    }
    docBusy = { kind: "saving", text: "保存中…（正在写入服务器）" };
    docFlash = null;
    updateDocUI();
    try {
      const result = await assistantApi("/api/v1/planner/documents/" + targetId + "/", {
        method: "PUT",
        json: {
          content_markdown: snapshot.body,
          title: snapshot.title.trim(),
          topic: snapshot.topic,
          format_options: JSON.parse(snapshot.format),
        },
      });
      if (!result.ok) {
        setDocFlash("error", "保存失败：" + KB.errorText(result, "保存失败"));
        return false;
      }
      if (currentDocId !== targetId) {
        // 目标已经切换：不污染新目标的状态（这次成功属于上一份）
        return true;
      }
      const confirmed = {
        title: result.body.title !== undefined ? result.body.title : snapshot.title,
        topic: result.body.topic !== undefined ? result.body.topic : snapshot.topic,
        body: result.body.content_markdown !== undefined
          ? result.body.content_markdown : snapshot.body,
        format: KB.docFormat.signature(result.body.format_options || {}),
      };
      docBaseline = confirmed;   // 基线 = 服务端确认的那一版
      docSavedAt = KB.fmtTime ? KB.fmtTime(result.body.updated_at) : "";
      docHint = "";
      const current = readEditor();
      const typedDuringSave = current.title !== confirmed.title ||
        current.topic !== confirmed.topic || current.body !== confirmed.body || current.format !== confirmed.format;
      setDocFlash("saved", typedDuringSave
        ? "已保存上一版；你在保存期间的新改动还**没有**保存（状态见下方按钮）"
        : "已保存（标题、主题、正文都已写入；来源资料与原件未变）");
      await listDocuments();
      return true;
    } finally {
      docBusy = null;
      updateDocUI();
    }
  }

  /** 清空编辑器（删掉当前打开的草稿后调用，避免留着已删文档的正文）。 */
  function clearDocumentEditor() {
    currentDocId = null;
    currentDocTemplateCategory = "";
    $("doc-title").value = "";
    $("doc-topic").value = "";
    $("doc-body").value = "";
    KB.docFormat.set({});
    renderPreview();
    $("doc-sources").textContent = "";
    setBaselineFromEditor();
    updateDocUI();
  }

  /** 删除指定草稿（行内 ✕ 与底栏「删除草稿」都走这里，行为一致）。 */
  async function deleteDocumentById(id, title) {
    // 删除是不可逆的（草稿没有回收站），所以二次确认里把"删什么、不删什么"说清楚：
    // 删的是这份派生草稿，来源资料与原件一个字节都不动。
    const label = (title || "").trim() || "（未命名草稿）";
    const answer = await KB.confirmDialog({
      title: "删除这份派生草稿？",
      body: "将删除草稿「" + label + "」。来源资料与原始文件不会被删除，也不会被修改。",
      targetLabel: label,
      confirmLabel: "删除草稿",
    });
    if (!answer || !answer.confirmed) return false;
    const result = await assistantApi("/api/v1/planner/documents/" + id + "/", {
      method: "DELETE",
    });
    if (!result.ok) {
      const detail = KB.errorText(result, "删除失败");
      // 删除的反馈也走**固定可见**的状态条（#doc-notice 在滚动区底部，看不到）
      setDocFlash("error", "删除失败：" + detail);
      KB.notice($("doc-notice"), detail, "err");
      return false;
    }
    if (Number(id) === Number(currentDocId)) {
      clearDocumentEditor();
      setDocStatus("idle");
    }
    setDocFlash("saved", "已删除这份草稿（来源资料与原件未动）");
    KB.notice($("doc-notice"), "已删除这份草稿（来源资料未动）。", "ok");
    await listDocuments();
    updateDocUI();
    return true;
  }

  async function deleteDocument() {
    if (!currentDocId) {
      KB.notice($("doc-notice"), "请先打开一份文档", "warn");
      return;
    }
    const title = $("doc-title").value.trim() || "（未命名草稿）";
    await deleteDocumentById(currentDocId, title);
  }

  async function searchSources(moreUrl) {
    const box = $("src-results");
    const query = $("src-query").value.trim();
    if (!moreUrl) box.textContent = "搜索中…";
    // **服务端**先按 status=done 过滤（不是取回一堆再在前端筛），
    // 并且显式分页：能作为来源的资料才会被算进来。
    const params = new URLSearchParams();
    params.set("status", "done");
    params.set("page_size", "50");
    if (query) {
      params.set("q", query);
      params.set("mode", "filename");
    }
    const result = await assistantApi(moreUrl || ("/api/v1/knowledge/files/?" + params.toString()));
    if (!result.ok) {
      // 失败必须**明确显示**，不能留一片空白让人误以为"没有资料"
      const message = (result.body && result.body.detail) || "读取资料列表失败";
      const stale = box.querySelector("[data-more]");
      if (stale) stale.remove();
      if (moreUrl) {
        // 加载更多失败：保留已列出的来源，只追加错误提示
        box.insertAdjacentHTML(
          "beforeend",
          '<div class="err-text">加载更多失败：' + esc(message) + "</div>"
        );
      } else {
        box.innerHTML = '<div class="err-text">读取资料失败：' + esc(message) + "</div>";
      }
      return;
    }
    const rows = (result.ok && result.body && (result.body.results || result.body)) || [];
    const usable = rows;
    // 还有下一页时给出"加载更多"，不假装 50 条就是全部
    const nextUrl = (result.ok && result.body && result.body.next) || null;
    if (!moreUrl) box.innerHTML = "";
    if (!usable.length) {
      if (!moreUrl) box.textContent = "没有已处理完成的资料可作来源";
      return;
    }
    usable.slice(0, 50).forEach(function (row) {
      const label = document.createElement("label");
      label.className = "src-item";
      const check = document.createElement("input");
      check.type = "checkbox";
      check.checked = chosenSources.has(String(row.id));
      check.addEventListener("change", function () {
        if (check.checked) chosenSources.add(String(row.id));
        else chosenSources.delete(String(row.id));
        updateChosen();
      });
      label.appendChild(check);
      label.appendChild(document.createTextNode(
        " " + row.original_name + "（" + (row.library_code || "") + " · " +
        (row.chunk_count || 0) + " 段）"
      ));
      box.appendChild(label);
    });
    if (nextUrl) {
      const more = document.createElement("button");
      more.className = "btn sm";
      more.setAttribute("data-more", "1");
      more.textContent = "加载更多";
      more.style.marginTop = "6px";
      more.addEventListener("click", function () {
        // 防重复：点一次立即禁用并显示"加载中…"。
        // 不要传事件对象进 searchSources —— 那会被当成 URL。
        more.disabled = true;
        more.textContent = "加载中…";
        searchSources(nextUrl);
      });
      box.appendChild(more);
    }
  }

  function updateChosen() {
    const box = $("src-chosen");
    if (!box) return;
    box.textContent = "已勾选 " + chosenSources.size + " 份" +
      (chosenSources.size ? "（将只依据这些资料撰写）" : "（将按主题自动检索全库）");
  }

  /**
   * 清空「指定来源」勾选（处置清单 AF-13）。
   *
   * 为什么必须清：`chosenSources` 是模块级 Set，勾选会**跨草稿、跨会话**残留；
   * 而 `createDocument` 一旦发现它非空就会把它当 `source_file_ids` 发出去，
   * 服务端把"显式给了来源"当作**强约束** —— 于是一份新草稿会被上一次会话里
   * 挑的来源悄悄限定检索范围，而结果看起来完全正常（最难发现的那种错）。
   *
   * 同时清 DOM：`#src-results` 里已经渲染出来的复选框也必须真的取消勾选，
   * 不能只清内存里的 Set（那样界面还打着勾，用户以为还在生效）。
   */
  function clearChosenSources() {
    chosenSources.clear();
    const box = $("src-results");
    if (box) {
      box.querySelectorAll('input[type="checkbox"]').forEach(function (check) {
        check.checked = false;
      });
    }
    updateChosen();
  }

  /**
   * 把地址栏回到"没有会话"的状态（处置清单 AF-11）。
   *
   * 旧实现里头部「＋ 新会话」只调 `renderHome(true)`，**不动 URL**；而刷新时
   * `conversationFromUrl()` 会从 `?conversation=` 把旧会话读回来 —— 于是
   * "新建会话"看起来生效了，一刷新（或前进/后退）又回到旧会话；
   * 同一页的左侧栏同名按钮走的是 `location.href = "/app/"`，两者行为相反。
   *
   * 用 `replaceState`（不是 `pushState`）是**故意**的：新建会话不是一个值得
   * 回退的浏览历史节点，否则用户按"后退"会退回一个已被清空的会话地址。
   */
  function clearConversationFromUrl() {
    // 显式 `window.history`：避免将来本文件出现同名局部函数时被遮蔽
    // （kb-search.js 就踩过 `history.replaceState is not a function`）。
    if (!window.history || typeof window.history.replaceState !== "function") return;
    const params = new URLSearchParams(location.search);
    if (!params.has("conversation")) return;
    params.delete("conversation");
    const query = params.toString();
    window.history.replaceState(null, "", location.pathname + (query ? "?" + query : ""));
  }

  /** 上传：**先让用户选信息库**（不能无声默认写第一个库），
   * 来源标记为 session（会话来源）。 */
  async function uploadFiles(files) {
    const list = Array.from(files || []);
    if (!list.length) return;
    // **互斥必须在最前面**（处置清单 AF-02）：旧实现把 uploadBusy 判定放在
    // 选库弹窗**之后**，两次点击可以在置位之前都通过 → 并发重复提交。
    if (uploadBusy) {
      KB.toast("上一批还在上传，请稍候");
      return;
    }
    uploadBusy = true;

    const libraries = await assistantApi("/api/v1/knowledge/libraries/?page_size=200");
    const allRows = (libraries.ok && libraries.body &&
      (libraries.body.results || libraries.body)) || [];
    // **只列可写入的库**：上传后端校验是 `CanContributeLibrary`（writer），
    // 把只读库列进选择框等于给用户一个注定 403 的入口。
    const writable = await KB.fetchWritableLibraries(allRows);
    const rows = writable.rows;
    if (!rows.length) {
      KB.toast("还没有信息库，请先创建一个");
      uploadBusy = false;
      return;
    }

    // 选库确认弹窗：**走 KB.modalChoice**（Esc 也会结算 Promise）。
    // 旧实现是自建 `new Promise` + 只在 [data-cancel]/遮罩 click 上结算，
    // 于是按 Esc 关掉弹窗后 Promise 永不结算、`uploadBusy` 永远是 true，
    // 下一批上传被"上一批还在上传"挡住 —— 用户看到的就是"点了没反应"。
    const choice = await KB.modalChoice(
      '<div class="modal-head"><b>上传到哪个信息库</b></div>' +
      '<div class="modal-body paper">' +
        '<p class="tiny muted">共 ' + list.length + ' 份文件；上传后来源记为"会话来源"。</p>' +
        (writable.blocked.length
          ? '<p class="tiny muted">已隐去 ' + writable.blocked.length +
            ' 个你没有写入权限的信息库（列出来也无法上传成功）。</p>'
          : "") +
        '<select id="upload-lib" data-choice-value style="border:1px solid var(--color-border);' +
        'border-radius:2px;padding:6px 8px;font-size:13px;width:100%">' +
        rows.map(function (row) {
          return '<option value="' + esc(row.code) + '">' + esc(row.name) +
            "（" + esc(row.code) + "）</option>";
        }).join("") +
        "</select>" +
      "</div>" +
      '<div class="modal-foot">' +
        '<button class="btn sm" data-cancel>取消</button>' +
        '<button class="btn primary sm" data-ok>开始上传</button>' +
      "</div>"
    );
    if (!choice) {
      uploadBusy = false;      // 取消（含 Esc）也要解锁，否则下一批永远被"上一批还在上传"挡住
      return;
    }

    let ok = 0;
    const failures = [];
    const duplicates = [];
    // 互斥已在函数入口置位（AF-02），这里直接进入上传循环
    try {
      for (let index = 0; index < list.length; index += 1) {
        const file = list[index];
        const form = new FormData();
        form.append("file", file);
        form.append("library", choice);
        form.append("source", "session");   // 会话来源口径
        KB.toast("正在上传 " + (index + 1) + " / " + list.length + "：" + file.name);
        // 用 KB.api：断网不会 reject（401 由它统一跳登录），失败原因原样带出来 ——
        // 旧实现裸 fetch + 只看 response.ok，逐份失败原因被**静默丢弃**，
        // 只用 1.8 秒 toast 报"已提交 ok / N 份"（Codex 独立审查点名）。
        const result = await assistantApi("/api/v1/knowledge/files/upload/", {
          method: "POST", form: form,
        });
        if (!result.ok) {
          failures.push(file.name + "：" + KB.errorText(result, "上传失败"));
        } else if (result.body && result.body.created === false &&
                   result.body.reason === "duplicate_content") {
          const note = (result.body.processing && result.body.processing.detail) || "";
          duplicates.push(file.name + (note ? "：" + note : ""));
        } else {
          ok += 1;
        }
      }
    } finally {
      uploadBusy = false;
      // **必须清空 input.value**：否则同一个文件再选一次不会触发 change（"没反应"）。
      const input = $("file-input");
      if (input) input.value = "";
    }
    if (failures.length || duplicates.length) {
      const parts = ["已提交 " + ok + " / " + list.length + " 份到 " + choice];
      if (duplicates.length) parts.push("已在库中（同一内容只留一条）：" + duplicates.join("；"));
      if (failures.length) parts.push("未提交：" + failures.join("；"));
      KB.toast(parts.join("。"));
      // 失败原因必须**留在页面上**（不只是 1.8 秒的 toast）：插在输入框上方。
      // 注意不能拿 #chat-inner 当容器 —— KB.notice 会先 innerHTML="" ，那会把会话清空。
      const host = $("composer-box");
      if (host && host.parentNode) {
        let box = document.getElementById("kb-upload-failures");
        if (!box) {
          box = document.createElement("div");
          box.id = "kb-upload-failures";
          box.className = "notice warn";
          box.style.margin = "6px 0";
          host.parentNode.insertBefore(box, host);
        }
        box.textContent = "上传结果：" + parts.join("。") + "（原因来自服务端，可重试）";
      }
    } else {
      KB.toast("已提交 " + ok + " / " + list.length + " 份到 " + choice);
    }
    await renderHome();
  }

  // ---------------------------------------------------------------- 启动 ----

  function bindChrome() {
    $("btn-send").addEventListener("click", function () {
      if (!$("composer").value.trim()) return;
      send($("composer").value);
    });
    $("composer").addEventListener("keydown", function (event) {
      // **输入法组合中的 Enter 不是"发送"**（Codex 审查实测：中文输入法下
      // 用 Enter 选词也会发出一次 chat 请求）。isComposing 是标准字段，
      // keyCode 229 是旧浏览器/部分 IME 的等价信号，两者都要挡。
      if (event.isComposing || event.keyCode === 229) return;
      if (event.key === "Enter" && !event.shiftKey) {
        event.preventDefault();
        $("btn-send").click();
      }
    });
    $("btn-new-session").addEventListener("click", async function () {
      // 新建会话会清空会话区：文档面板里未保存的编辑必须先问一次
      if (!(await confirmDiscardDocEdits("新建会话"))) return;
      conversationId = null;
      // **必须同时做这两件事**（处置清单 AF-11 / AF-13）：
      //   1. 去掉 `?conversation=`，否则刷新/前进后退会回到旧会话；
      //   2. 清空「指定来源」勾选，否则上一个会话挑的来源会被带到新会话，
      //      用户以为新会话按自己的主题写，实际被旧勾选限定了检索范围。
      clearConversationFromUrl();
      clearChosenSources();
      renderHome(true);
    });
    $("btn-tools").addEventListener("click", openTools);
    $("btn-docs").addEventListener("click", function () { toggleDocs(); });
    $("btn-doc-close").addEventListener("click", function () { toggleDocs(false); });
    $("btn-upload").addEventListener("click", function () { $("file-input").click(); });
    $("file-input").addEventListener("change", function (event) { uploadFiles(event.target.files); });
    // 「知识库调用选择」：多选要检索的信息库（空 = 全部有权查看的库）
    $("btn-kb-scope").addEventListener("click", openKnowledgeScope);
    $("btn-kb-scope-clear").addEventListener("click", function () {
      scopeCodes = [];
      renderScopeBar();
    });
    // 文档模板：上传 / 展开时按需重读
    $("btn-tpl-upload").addEventListener("click", function () { $("tpl-input").click(); });
    $("tpl-input").addEventListener("change", function (event) {
      const file = event.target.files && event.target.files[0];
      // **先清空 input**：否则连续选同一个文件（例如改好格式再传一次）不会再触发 change，
      // 用户会以为"点了没反应"。
      event.target.value = "";
      uploadTemplate(file);
    });
    const tplBlock = $("tpl-block");
    if (tplBlock) {
      tplBlock.addEventListener("toggle", function () {
        // 展开时若从未成功读到列表（失败态），顺手重试一次
        if (tplBlock.open && templateRows === null) loadTemplates(true);
      });
    }
    $("doc-create").addEventListener("click", createDocument);
    $("doc-new").addEventListener("click", startNewDraft);
    $("doc-save").addEventListener("click", function () { saveDocument(); });
    $("doc-tab-edit").addEventListener("click", function () { switchDocTab("edit"); });
    $("doc-tab-preview").addEventListener("click", function () { switchDocTab("preview"); });
    // 导出：交给 downloadDoc（会先保存当前版本；失败原因显示在状态条 + toast，
    // 不再 `location.href` 导航到 JSON 错误页）
    document.querySelectorAll(
      "#doc-download, #doc-download-html, #doc-download-docx, #doc-download-pdf, #doc-download-pptx"
    ).forEach(function (button) {
      if (!button.getAttribute("data-base-title")) {
        button.setAttribute("data-base-title", button.title || "");
      }
      button.addEventListener("click", function () {
        const format = button.id.replace("doc-download", "").replace(/^-/, "") || "md";
        downloadDoc(format, button);
      });
    });
    $("doc-delete").addEventListener("click", function () { deleteDocument(); });

    $("doc-format-controls").addEventListener("change", function () {
      docEditRevision += 1; docFlash = null; renderPreview(); updateDocUI();
    });
    // **脏检查 + 编辑版本号**：标题/主题/正文任一改动都要立刻反映到状态条与保存
    // 按钮上；同时 docEditRevision +1，供"请求期间是否又编辑过"的并发判断使用。
    ["doc-title", "doc-topic", "doc-body"].forEach(function (id) {
      const element = $(id);
      if (!element) return;
      element.addEventListener("input", function () {
        docEditRevision += 1;
        docFlash = null;          // 用户一动手，上一条结果提示就撤掉（避免误读成"已保存"）
        updateDocUI();
      });
      // 非输入方式（粘贴/撤销/程序化赋值）也要跟手
      element.addEventListener("change", function () {
        docEditRevision += 1;
        updateDocUI();
      });
    });
    updateDocUI();
    // 关标签/刷新/外链跳转的兜底（站内导航用的是下面的点击拦截 + KB.docDirtyGuard）
    window.addEventListener("beforeunload", function (event) {
      if (isDocDirty()) {
        event.preventDefault();
        event.returnValue = "";
        return "";
      }
      return undefined;
    });
    // **必须包一层**：直接把 searchSources 作为处理器时，浏览器会把 MouseEvent
    // 当作第一个参数（moreUrl）传进去，于是拿事件对象去请求 → 列表空白。
    $("src-search").addEventListener("click", function () { searchSources(); });
    $("src-query").addEventListener("keydown", function (event) {
      if (event.isComposing || event.keyCode === 229) return;   // 输入法选词的 Enter
      if (event.key === "Enter") searchSources();
    });
  }

  /** 支持从左侧会话列表直接进入某个会话：/app/?conversation=<id> */
  function conversationFromUrl() {
    const value = new URLSearchParams(location.search).get("conversation");
    return value ? Number(value) : null;
  }

  /**
   * 给**别的脚本**用的离开前守卫（kb-nav.js 的会话切换/新建会话/删除会话都会调）。
   *
   * 为什么做成全局钩子：会话导航在 kb-nav.js 里用 `location.href` 直接跳，
   * 文档面板的未保存改动会被静默丢掉；本文件（kb-assistant.js）只在首页加载，
   * 所以把判断放在这里、由导航方 **await** 一次，职责最清楚。
   * 返回 true = 可以继续（无改动或用户确认丢弃）。
   */
  KB.docDirtyGuard = function (what) {
    return confirmDiscardDocEdits(what || "离开当前页面");
  };

  /** 给全局导航/关闭标签的守卫用：当前是否有未保存的文档编辑。 */
  KB.docIsDirty = function () {
    return isDocDirty();
  };

  document.addEventListener("DOMContentLoaded", async function () {
    bindChrome();
    toggleDocsForced(false);                 // 文档栏**默认收起**（不触发脏检查）
    renderScopeBar();                        // 默认不限定检索范围（隐藏回显条）
    loadTemplates();                         // 失败也只影响模板区，不阻塞首页
    conversationId = conversationFromUrl();
    await renderHome(!conversationId);
  });
})();
