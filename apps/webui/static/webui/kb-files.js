/* 文件管理 —— 结构与原型最后生效的 viewFiles() 一致。
 *
 * 真实接口：
 *   列表/筛选  GET /api/v1/knowledge/files/?q=&status=&library=&nature=&amount_min=&file_type=&page=
 *   筛选选项    GET /api/v1/knowledge/files/facets/（**服务端统计**，不是本地窗口）
 *   统计卡      GET /api/v1/knowledge/stats/
 *   上传        POST /api/v1/knowledge/files/upload/（multipart；目录上传带 directory_path）
 *   解析/重试   POST /api/v1/knowledge/files/<id>/process/
 *   详情/分段   GET  /api/v1/knowledge/files/<id>/  与  /chunks/
 *   删除        **受控提案**（三次独立确认 + 一次性凭证），绝不直接删
 */
(function () {
  "use strict";

  const KB = window.KB;
  const esc = KB.esc;
  const mono = KB.mono;
  const $ = function (id) { return document.getElementById(id); };

  const PAGE_SIZE = 10;
  const STATUS_TEXT = {
    pending: "待处理", running: "处理中", done: "已完成", failed: "失败",
    archived: "已归档",
  };

  const state = {
    page: 1, q: "", status: "all", library: "all", fileType: "all", session: false,
    /** 性质筛选：`"all"` / 五项词表之一 / `KB.NATURE_UNIDENTIFIED`（待识别）。 */
    nature: "all",
  };
  let currentRows = [];
  let parseBatchBusy = false;
  let statsTimer = null;

  function batchProgress(label) {
    let host = $("files-batch-progress");
    if (!host) {
      host = document.createElement("div");
      host.id = "files-batch-progress";
      $("files-upload-note").insertAdjacentElement("afterend", host);
    }
    return KB.progress.start(host, label);
  }

  // 局部更新处理状态，保留勾选、翻页和筛选条件。
  document.addEventListener("kb:file-progress", function (event) {
    const update = event.detail;
    const file = currentRows.find(function (row) { return row.id === update.id; });
    if (!file) return;
    const changed = file.parse_status !== update.parse_status;
    Object.assign(file, update);
    const row = document.querySelector('[data-file-row="' + file.id + '"]');
    if (row) {
      const chunks = row.querySelector("[data-file-chunks]");
      if (chunks) chunks.textContent = file.chunk_count || 0;
      const time = row.querySelector("[data-file-time]");
      if (time) time.textContent = shortTime(file.parsed_at || file.created_at);
      const button = row.querySelector("[data-parse]");
      if (button) {
        button.disabled = parseBatchBusy || button.dataset.permDenied === "1" ||
          ["running", "done", "archived"].includes(file.parse_status);
        button.textContent = file.parse_status === "failed" ? "重新解析" : "解析";
      }
      let error = row.querySelector(".parse-error");
      if (file.parse_status === "failed" && file.parse_error) {
        if (!error) { error = document.createElement("div"); error.className = "parse-error tiny"; row.lastElementChild.appendChild(error); }
        error.textContent = "解析失败：" + file.parse_error;
      } else if (error) error.remove();
    }
    if (changed && !statsTimer) statsTimer = setTimeout(function () { statsTimer = null; loadStats(); }, 500);
  });
  /** 性质词表（来自 /facets/ 的 `classifications`，顺序即后端 `FileNature`）：
   *  上传弹窗与行内下拉共用，前端**不自己写死词表**，免得两处口径漂移。 */
  let NATURE_VOCABULARY = [];
  /** 上传互斥：目录选择用的是 `<label>`，`disabled` 拦不住它，必须自己挡重入。 */
  let uploadBusy = false;
  /** 列表请求票据：只有最新一次请求的结果允许渲染（防乱序覆盖）。 */
  let loadTicket = 0;

  /** 原型用 MM-DD HH:mm（不是完整日期），取 parsed_at 优先。 */
  function shortTime(value) {
    if (!value) return "—";
    const date = new Date(value);
    if (isNaN(date.getTime())) return "—";
    const pad = function (number) { return String(number).padStart(2, "0"); };
    return pad(date.getMonth() + 1) + "-" + pad(date.getDate()) + " " +
      pad(date.getHours()) + ":" + pad(date.getMinutes());
  }

  function dotClass(status) {
    if (status === "done") return "completed";
    if (status === "running") return "processing";
    if (status === "failed") return "failed";
    return "pending";
  }

  // ------------------------------------------------------------ 统计卡 ----

  async function loadStats() {
    const result = await KB.api("/api/v1/knowledge/stats/");
    const data = (result.ok && result.body) || {};
    $("stat-total").textContent = mono(data.files || 0);
    $("stat-done").textContent = mono(data.done || 0);
    $("stat-processing").textContent = mono(data.processing || 0);
    $("stat-pending").textContent = mono(data.pending || 0);
    $("stat-failed").textContent = mono(data.failed || 0);
    // "来自 AI 会话"按钮：数字是**本库全部会话来源资料的份数**（服务端统计），
    // 不是"当前筛选后剩下的条数" —— 口径写在 title 与 aria-pressed 上（AF-29），
    // 让色觉障碍用户与读屏用户也能判断这个筛选到底开没开。
    const session = $("files-session");
    if (session) {
      session.textContent = "来自 AI 会话 (" + (data.session_files || 0) + ")";
      session.title = "只看通过 AI 助手加进来的资料。" +
        "括号里的 " + (data.session_files || 0) + " 是**全部**会话来源资料的总份数" +
        "（不受本页其他筛选影响）。按 Esc 也可以取消所有筛选。";
    }
    updateSessionFilterState();
  }

  /** 把「来自 AI 会话」的开关状态如实写进无障碍属性（AF-29）。 */
  function updateSessionFilterState() {
    const session = $("files-session");
    if (!session) return;
    session.setAttribute("aria-pressed", state.session ? "true" : "false");
    session.classList.toggle("primary", Boolean(state.session));
  }

  // ------------------------------------------------------ 筛选选项/列表 ----

  function query() {
    const params = new URLSearchParams();
    if (state.q) {
      params.set("q", state.q);
      // 文件管理页的关键词**按文件名**匹配（原型如此）；正文检索在搜索页
      params.set("mode", "filename");
    }
    if (state.status !== "all") params.set("status", state.status);
    if (state.library !== "all") params.set("library", state.library);
    if (state.fileType !== "all") params.set("file_type", state.fileType);
    // 性质筛选：五项走它们各自的词表值；「待识别」走 KB.NATURE_UNIDENTIFIED 哨兵
    // （空串与"不带这个参数"等价，表达不了"只看空性质"——见 kb-common.js 的说明）。
    if (state.nature !== "all") params.set("nature", state.nature);
    if (state.session) params.set("session", "1");
    params.set("page", state.page);
    // 与服务端分页类对齐：本页按 10 条算页，就必须真的请求 10 条
    params.set("page_size", String(PAGE_SIZE));
    return params.toString();
  }

  async function loadFacets() {
    const [facets, libraries] = await Promise.all([
      KB.api("/api/v1/knowledge/files/facets/"),
      KB.api("/api/v1/knowledge/libraries/?page_size=200"),
    ]);
    const data = (facets.ok && facets.body) || {};
    const rows = (libraries.ok && libraries.body &&
      (libraries.body.results || libraries.body)) || [];

    // 按原型显示 **name (份数)**；拼 code 会让下拉变宽，把右侧"全部解析"挤出去
    // （这里同时修掉一个真实缺陷：旧代码把 `innerHTML` 的赋值写在了
    //  `rows.map(...)` 之后 —— 于是"全部信息库"以及真实库里只剩**第一项**。
    //  信息库筛选一直选不全，正是因为括号闭错了位置。）
    $("files-lib").innerHTML = '<option value="all">全部信息库 (' + rows.length + ')</option>' +
      rows.map(function (row) {
        return '<option value="' + esc(row.code) + '"' +
          (state.library === row.code ? " selected" : "") + ">" + esc(row.name) +
          " (" + (row.file_count || 0) + ")</option>";
      }).join("");

    // 「文件类型」下拉：只列**三类 + 其他**（用户 2026-10-16 要求 PDF / Excel /
    // Word），份数由服务端按全库统计给出。裸扩展名（pdf / xlsx…）不再出现在下拉里
    // ——那是「类型」列展示的粒度，两者混在一起会让人以为"类型"和"文件类型"是同一件事。
    const groups = data.file_type_groups || [];
    $("files-type").innerHTML = '<option value="all">文件类型</option>' +
      groups.map(function (item) {
        return '<option value="' + esc(item.value) + '"' +
          (state.fileType === item.value ? " selected" : "") + ">" + esc(item.label) +
          " (" + item.count + ")</option>";
      }).join("");

    // 性质词表（含 count=0 的项）：上传弹窗与行内下拉都要能列出**全部**可选值
    NATURE_VOCABULARY = data.classifications || [];

    // 「性质」筛选下拉 = 全部 + 五项词表 + 「待识别」。
    // 五项的词表值来自服务端（`classifications`），前端不自己抄一份。
    // 「待识别」= 性质还为空的那些资料，取值用 KB.NATURE_UNIDENTIFIED
    // （空串与"不带这个筛选"等价，表达不了"只看待识别"）。
    const natureSelect = $("files-nature");
    if (natureSelect) {      natureSelect.innerHTML = '<option value="all">资料性质</option>' +
        NATURE_VOCABULARY.map(function (value) {
          return '<option value="' + esc(value) + '"' +
            (state.nature === value ? " selected" : "") + ">" + esc(value) + "</option>";
        }).join("") +
        '<option value="' + KB.NATURE_UNIDENTIFIED + '"' +
        (state.nature === KB.NATURE_UNIDENTIFIED ? " selected" : "") + ">" +
        esc(KB.NATURE_PENDING) + "</option>";
    }
  }

  /** 性质筛选的说明：只按**真实结果**说话。
   *
   * 0 条就是"没有待识别（未识别性质）的资料" —— 不猜接口能力、不解释实现细节。
   * 接口失败走另一条分支（loadFiles 里把服务端 detail 显示出来），**不渲染成 0 条**。
   */
  function natureResultNote(count) {
    if (state.nature !== KB.NATURE_UNIDENTIFIED) return "";
    if (count > 0) {
      return "当前只显示「" + KB.NATURE_PENDING + "」（还没识别出性质）的资料，共 " +
        count + " 份。";
    }
    return "没有待识别（未识别性质）的资料。";
  }

  function row10(text) {
    return '<tr><td colspan="10"><div class="empty-state" style="text-align:center;padding:26px 0">' +
      '<p class="muted">' + esc(text) + "</p></div></td></tr>";
  }

  /** 页面提示区（与上传失败提示共用）：有原因就**留在页面上**，不只用一条会消失的 toast。 */
  function showPageNote(message, kind) {
    if (!message) return;
    KB.notice("files-upload-note", message, kind || "warn");
  }

  /** 「性质筛选」的独立提示区：筛选说明与上传结果**互不覆盖**（各写各的容器）。 */
  function showNatureNote(message, kind) {
    KB.notice("files-nature-note", message || "", kind || "warn");
  }

  /* ------------------------------------------------------ 行内动作权限 ----
   *
   * 后端对文件页的行内动作有**真实的库级权限校验**（不是前端能绕过的）：
   *   * 删除 / 改性质 → `library:admin`（`CanManageLibrary`，
   *     见 `apps/planner/operations.py` 与 `services.check_permission`）；
   *   * 解析 / 重新解析 → `library:write`（`CanContributeLibrary`，
   *     `apps/knowledge/views.py` 的 `process` 动作里逐个字段核验）。
   *
   * 旧实现完全不看权限：普通账号能看到并点动这些按钮，走完受控确认流程
   * （删除还要抄一遍文件名）之后才拿到 403 —— 处置清单 AF-26。
   * 这里只做**显示层**：置灰 + 写明原因，**不替代后端校验**（后端永远说了算）。
   *
   * 角色来源是服务端：`LibrarySerializer.my_role`（reader / writer / admin）。
   * 权限信息**取不到时不做任何禁用**（宁可让用户点一次看到真实 403，
   * 也不要把一个本来有权限的按钮错误地关掉）。
   */
  const ROLE_RANK = { reader: 1, writer: 2, admin: 3 };
  let libraryRoles = null;   // Map<libraryId(string), role|null>；null = 还没取到

  function isSystemAdmin() {
    return document.body.getAttribute("data-system-admin") === "1";
  }

  /** 当前账号在该文件的库里是否至少具备 `required` 角色（reader/writer/admin）。 */
  function canActOn(file, required) {
    if (isSystemAdmin()) return true;
    if (!libraryRoles) return true;        // 权限信息未取到：不禁用，交给后端判定
    const key = String(file.library === undefined || file.library === null
      ? "" : file.library);
    if (!libraryRoles.has(key)) return true;   // 不在名单里 = 说不清，交给后端
    const role = libraryRoles.get(key);
    return (ROLE_RANK[role] || 0) >= (ROLE_RANK[required] || 99);
  }

  //: 信息库筛选用的 code → 库 id（上传置灰要按**筛选选中的那个库**判定角色）
  const libraryIdByCode = new Map();

  /** 置灰控件 + 给出**可见的**原因（title 只能悬停看到，不够）。 */
  function denyControl(element, reason) {
    if (!element) return;
    element.disabled = true;
    element.setAttribute("data-perm-denied", "1");
    element.setAttribute("aria-disabled", "true");
    element.title = reason;
  }

  /** 资料所属信息库的显示名（用于把"哪个库、缺什么权限"说清楚）。 */
  function libraryLabelOf(file) {
    const row = (currentRows || []).filter(function (item) {
      return String(item.id) === String((file || {}).id);
    })[0] || {};
    return row.library_name || row.library_code || "该信息库";
  }

  /** 人话权限说明：**说清哪个库、缺什么权限、下一步怎么做**（不写 HTTP 状态码）。 */
  function writeNeedText(file) {
    return "解析：需要你在「" + libraryLabelOf(file) +
      "」有写入权限；你当前在该库没有写入权限。可联系该库管理员把你加为可写入成员。";
  }

  function adminNeedText(file, action) {
    return action + "：需要你在「" + libraryLabelOf(file) +
      "」有管理员权限；你当前在该库不是管理员。可联系该库管理员开通或代为操作。";
  }

  /** 权限说明条：与上传/性质提示**分容器**，谁都不覆盖谁。 */
  function showPermissionNote(message) {
    KB.notice("files-perm-note", message || "", "warn");
  }

  //: 本页渲染时因权限被置灰的动作计数（只为写一行说明，不参与任何判定）
  const deniedActionCounts = { parse: 0, classify: 0, delete: 0 };

  //: 事实态：权限名单**成功取回且为空** = 这个账号在任何信息库都没有成员权限。
  //: 必须与 `libraryRoles === null`（请求失败/权限未知）区分开：
  //:   * 未知 → 不禁用、不解释（交给后端如实报错，不能把有权限的用户锁死）；
  //:   * 确定无库 → 禁用上传/扫描/全部解析，并说明"联系库管理员"。
  let noLibraryMembership = false;

  //: 本次渲染的列表是不是"没有任何筛选却一条都没有"（用于把"无成员权限"与
  //: "筛选没命中"分开说）。由 loadFiles 在 renderRows 之前写入。
  let emptyListUnfiltered = false;

  function updatePermissionNote() {
    // 原始计数（哪一类动作有几份被置灰）落成**隐藏的机器可读状态**：
    // 界面文案会随措辞变化，但验收/排障需要的是计数本身，写一个 data 属性最省事，
    // 也不参与任何产品判定（`hidden`，用户看不到）。
    let stateNode = document.getElementById("files-perm-state");
    if (!stateNode) {
      stateNode = document.createElement("div");
      stateNode.id = "files-perm-state";
      stateNode.hidden = true;
      const host = $("files-perm-note");
      if (host && host.parentNode) host.parentNode.insertBefore(stateNode, host.nextSibling);
      else document.body.appendChild(stateNode);
    }
    stateNode.setAttribute("data-state", JSON.stringify({
      system_admin: isSystemAdmin(),
      roles_known: Boolean(libraryRoles),
      no_membership: noLibraryMembership,
      denied: deniedActionCounts,
    }));
    if (isSystemAdmin()) { showPermissionNote(""); return; }
    if (!libraryRoles) { showPermissionNote(""); return; }
    // **"没有任何成员权限"是事实态，不能被"本页没有被置灰的动作"抹掉**
    // （Codex 角色复验：loadFiles 先写这句，随后 updatePermissionNote 在计数全 0 时
    // 用空串把它清掉了 —— 用户于是看不到"为什么什么都没有"）。
    if (noLibraryMembership) {
      showPermissionNote("你当前在**任何信息库**里都没有成员权限，所以看不到资料；" +
        (emptyListUnfiltered ? "" : "（当前筛选下没有命中，也可能与权限有关）") +
        "上传、扫描目录与「全部解析」也已停用。" +
        "请联系对应信息库的库管理员，把你加入该库（只读也可以先查看与检索）。");
      return;
    }
    const reasons = [];
    if (deniedActionCounts.delete) reasons.push(deniedActionCounts.delete + " 份不能删除、");
    if (deniedActionCounts.classify) reasons.push(deniedActionCounts.classify + " 份不能改性质、");
    if (deniedActionCounts.parse) reasons.push(deniedActionCounts.parse + " 份不能解析");
    if (!reasons.length) { showPermissionNote(""); return; }
    showPermissionNote("权限不足：" + reasons.join("").replace(/、$/, "") +
      "。这些按钮已置灰 —— 不是界面坏了，而是服务端会拒绝。" +
      "删除、改性质需要对应信息库的管理员权限，解析需要该库的写入权限；" +
      "需要这些操作时请联系对应信息库的库管理员开通成员权限。");
  }

  /** 读服务端给的**库级角色**（`LibrarySerializer.my_role`），用于显示层置灰。
   *
   * 只读这一份列表接口，不额外发明权限判定：系统管理员在没有名单时视为全部可管
   * （后端 `readable_library_ids(None)` = 不受限）。取不到就把 map 留成 null，
   * `canActOn` 会**全部放行**（交给后端如实报错），绝不因为一次请求失败就把
   * 有权限的用户锁死。
   */
  async function loadCapabilities() {
    const result = await KB.api("/api/v1/knowledge/libraries/?page_size=200");
    if (!result.ok) {
      libraryRoles = null;
      // 权限**未知**：不设"无成员"事实态（否则一次请求失败就把有权限的用户锁死）
      noLibraryMembership = false;
      updateUploadGate();
      updateBulkActionsGate();
      updatePermissionNote();
      return;
    }
    const rows = (result.body && (result.body.results || result.body)) || [];
    const map = new Map();
    rows.forEach(function (row) {
      map.set(String(row.id), row.my_role || null);
    });
    libraryRoles = map;
    // **成功取回但为空** = 确定没有任何库成员权限（与"未知"严格区分）
    noLibraryMembership = !isSystemAdmin() && map.size === 0;
    libraryIdByCode.clear();
    rows.forEach(function (row) {
      if (row.code) libraryIdByCode.set(String(row.code), row.id);
    });
    updateUploadGate();
    updateBulkActionsGate();
    updatePermissionNote();
  }

  /** 「全部解析」的可用性判定 —— **按库的写入权限**，不是"必须系统管理员"。
   *
   * 后端这条路径的真实要求是逐份资料的 `process` 动作 = `CanContributeLibrary`
   * （writer）。所以：
   *   * 筛选选中了某一个库 → 你在该库是 writer/admin 就能批量解析；
   *   * 筛选是「全部信息库」→ 只有**当前可读的每个库都可写**时才允许
   *     （否则这条路径必然对某些库发出 403 请求）；
   *   * 权限信息取不到，或某个库的角色说不清 → **不禁用**（交给后端如实报错）。
   * 仍然只是**显示层**：后端每一步都会自己核验。
   */
  function bulkParseAllowed() {
    if (isSystemAdmin()) return true;
    if (!libraryRoles) return true;
    if (state.library !== "all") {
      const id = libraryIdByCode.get(state.library);
      if (id === undefined) return true;
      return canActOn({ library: id }, "writer");
    }
    if (!libraryRoles.size) {
      // **确定没有任何库成员权限**（名单成功取回但为空）：批量解析必然 403 → 禁用。
      // 与 `libraryRoles === null`（权限未知）严格区分，后者在上面的 `!libraryRoles` 已放行。
      return false;
    }
    let allWritable = true;
    libraryRoles.forEach(function (role) {
      if (!(ROLE_RANK[role] >= ROLE_RANK.writer)) allWritable = false;
    });
    return allWritable;
  }

  /** 按当前筛选刷新「全部解析」的禁用态 + 可见原因。 */
  function updateBulkActionsGate() {
    const button = $("files-parse-all");
    if (!button) return;
    if (bulkParseAllowed()) {
      if (button.getAttribute("data-perm-denied") === "1") {
        button.disabled = false;
        button.removeAttribute("data-perm-denied");
        button.removeAttribute("aria-disabled");
        button.removeAttribute("title");
      }
      return;
    }
    const reason = noLibraryMembership
      ? "你当前在**任何信息库**里都没有成员权限，所以「全部解析」不可用；" +
        "请联系信息库的库管理员把你加入（至少需要写入权限），加入后这个按钮就会可用。"
      : (state.library === "all"
        ? "「全部解析」会对你可读的**每一个**信息库发起解析，而解析需要各库的写入权限；" +
          "你并不是每个库都有写入权限。请先在上面把「信息库」筛选到你有写入权限的库，" +
          "或改用每一行里的「解析」（那一列已按库权限置灰并写明原因）。"
        : "「全部解析」：需要你在「" + (state.library || "该信息库") +
          "」有写入权限；你当前在该库没有写入权限。可联系该库管理员把你加为可写入成员。");
    denyControl(button, reason);
  }

  /** 上传/扫描目录需要 `library:write`（后端 `CanContributeLibrary`）。
   *
   * 两种要区分的情况（Codex 角色复验）：
   *   * **确定没有任何库成员权限**（名单成功取回且为空）→ 上传/扫描必然被拒，
   *     直接置灰并说明"联系库管理员"；
   *   * 选了"全部信息库"或权限**未知**（请求失败）→ 不知道用户会往哪个库传，
   *     不猜：照旧弹出选库弹窗，由后端对真正选中的库如实报错（宁可不置灰，
   *     也不误伤有权限的用户）；指定了某个库但角色说不清时同样放行交后端。
   */
  function updateUploadGate() {
    if (isSystemAdmin()) { clearUploadGate(); return; }
    if (noLibraryMembership) {
      ["files-upload", "files-folder-button"].forEach(function (elementId) {
        denyControl($(elementId),
          "上传 / 扫描目录：你当前在**任何信息库**里都没有成员权限，所以不可用；" +
          "请联系信息库的库管理员把你加入（写入权限才能上传），加入后即可使用。");
      });
      return;
    }
    if (state.library === "all" || !libraryRoles) {
      clearUploadGate();
      return;
    }
    const id = libraryIdByCode.get(state.library);
    const denied = id !== undefined && !canActOn({ library: id }, "writer");
    if (!denied) { clearUploadGate(); return; }
    const name = ($("files-lib") && $("files-lib").selectedOptions[0]
      ? $("files-lib").selectedOptions[0].textContent : state.library);
    ["files-upload", "files-folder-button"].forEach(function (elementId) {
      denyControl($(elementId), "上传：需要你在「" + name +
        "」有写入权限；你当前在该库没有写入权限。可把「信息库」筛选切到你有写入权限的库，" +
        "或联系该库管理员把你加为可写入成员。");
    });
  }

  /** 撤掉上传控件上的权限置灰（换成别的库 / 权限恢复时不能留一个假的禁用态）。 */
  function clearUploadGate() {
    ["files-upload", "files-folder-button"].forEach(function (elementId) {
      const element = $(elementId);
      if (!element) return;
      if (element.getAttribute("data-perm-denied") !== "1") return;
      element.disabled = false;
      element.removeAttribute("data-perm-denied");
      element.removeAttribute("aria-disabled");
      element.removeAttribute("title");
    });
  }

  async function loadFiles() {
    // **请求票据**：快速改筛选/翻页时，先发的慢响应可能后到并覆盖新结果
    // （Codex 审查：加载没有身份标识，谁后到谁生效）。票据不是最新的就丢弃。
    const ticket = ++loadTicket;
    const result = await KB.api("/api/v1/knowledge/files/?" + query());
    if (ticket !== loadTicket) return;   // 已有更新的请求在跑，这次结果作废
    if (!result.ok) {
      // **接口失败不等于 0 条**：失败必须显示服务端给的原因（权限不足也走这里），
      // 并且**不能**留下"没有待识别资料"这类空态文案 —— 那是把错误说成了事实。
      showNatureNote("");
      showPermissionNote("");
      $("files-body").innerHTML = row10("无法读取资料列表：" +
        ((result.body && result.body.detail) || "请求失败"));
      return;
    }
    const body = result.body || {};
    const rows = body.results || body;
    const total = typeof body.count === "number" ? body.count : rows.length;
    // 性质筛选的说明只按**真实返回**说话（0 条就是"没有待识别的资料"）。
    // 独立提示区（#files-nature-note）：与上传结果提示互不覆盖。
    showNatureNote(natureResultNote(total), "info");
    currentRows = rows;
    // **"一个可读的资料都没有"必须与"筛选没命中"分开说**：前者多半是当前账号
    // 在这些信息库里没有成员权限，页面必须给出**可执行**的下一步，否则用户会把
    // 它读成"资料被清空了"。
    //
    // **注意执行顺序**（Codex 角色复验）：这里不要再自己写权限说明条 ——
    // `renderRows()` 末尾会调 `updatePermissionNote()`，它才是权限说明的**唯一出口**
    // （否则刚写的"没有成员权限"会被它按"本页没有被置灰的动作"清成空串）。
    // 这里只把这次渲染的**事实**（是不是空列表、有没有筛选）交给它。
    const unfiltered = Boolean(
      !state.q && state.status === "all" && state.library === "all"
      && state.fileType === "all" && state.nature === "all" && !state.session
    );
    emptyListUnfiltered = !rows.length && unfiltered;
    renderRows(rows);
    const pages = Math.max(1, Math.ceil(total / PAGE_SIZE));
    // 文案必须按**真实请求的 page** 显示：旧实现用 min(state.page, pages) 只改了
    // 显示的数字，真实请求仍是越界的 state.page —— 于是"第 1/1 页"配着空列表，
    // 用户看到的页码和实际拿到的那一页对不上。这里同时校准 state.page。
    const current = Math.min(Math.max(state.page, 1), pages);
    state.page = current;
    $("files-page-info").textContent = "共 " + total + " 份 · 第 " + current + " / " + pages + " 页";
    updateSelectionNote();    $("files-prev").disabled = current <= 1;
    $("files-next").disabled = current >= pages;
  }

  /**
   * 勾选说明：界面文案必须跟着**真实选中集合**走。
   *
   * 只统计当前 DOM 里真的勾上的行（复选框就是唯一事实来源），
   * 不再维护第二份"已选 N 份"的状态 —— 两份状态一旦不同步就是骗人。
   */
  function updateSelectionNote() {
    const note = $("files-select-note");
    if (!note) return;
    const checked = Array.from(document.querySelectorAll(".file-check"))
      .filter(function (box) { return box.checked; });
    if (!checked.length) {
      note.textContent = "";
      note.setAttribute("data-count", "0");
      return;
    }
    const names = checked.map(function (box) {
      const name = box.getAttribute("data-name") || "";
      return name || ("#" + box.value);
    });
    note.setAttribute("data-count", String(checked.length));
    note.textContent = "已选 " + checked.length + " 份：" +
      names.slice(0, 2).join("、") + (names.length > 2 ? " 等" : "");
  }

  function renderRows(rows) {
    const bodyEl = $("files-body");
    // **每次重渲染都把"全选"归零**：重载/筛选/翻页之后新渲染出来的行 checkbox
    // 全是未选中，而 #check-all 仍是勾着的 —— 视觉与真实状态不一致（已证实缺陷）。
    // 这里只承认"真实选中集合"，DOM 里的复选框就是唯一事实。
    const checkAll = $("check-all");
    if (checkAll) checkAll.checked = false;
    // 每次重渲染都重新统计"因权限被置灰的动作"，说明条才不会残留上一页的计数
    deniedActionCounts.parse = 0;
    deniedActionCounts.classify = 0;
    deniedActionCounts.delete = 0;
    if (!rows.length) {
      bodyEl.innerHTML = row10(state.session
        ? "还没有来自 AI 会话的资料。"
        : "没有符合筛选条件的资料。");
      updateSelectionNote();
      updatePermissionNote();
      return;
    }
    bodyEl.innerHTML = rows.map(function (file) {
      // 显示**完整** directory_path：slice(1) 会把顶层目录名吃掉
      const dir = file.directory_path || "—";
      // 原型每行都有"解析"位；已完成/处理中/**已归档**的保留但禁用，并说明原因。
      // 归档资料为什么必须禁用（独立 UI 验收发现）：后台已经**正确拒绝**对归档件
      // 走普通解析（`pipeline.acquire_lease`/`retry_file` 默认拒绝 ARCHIVED），
      // 所以按钮留 enabled 只会让用户点出一个必然失败的请求。要恢复检索请走
      // 受控「恢复」入口（library:admin + 确认 + 审计）。
      const parseBusy = ["done", "running", "archived"].indexOf(file.parse_status) >= 0;
      const parseTitle = file.parse_status === "done"
        ? "这份资料已处理完成，无需重复解析"
        : (file.parse_status === "running"
          ? "正在处理中，请稍候"
          : (file.parse_status === "archived"
            ? "该资料为历史归档状态，暂不参与解析"
            : ""));
      const parseError = String(file.parse_error || "").trim();
      // 解析失败时必须**看得见原因**：parse_error 是服务端为这次尝试留下的原话
      // （形如 `PipelineError: 模型服务不可用：...`）。旧实现只在详情弹窗里给一行，
      // 列表上只有"失败"两个字 —— 用户无从判断是原件坏了还是模型掉线。
      // 这里在操作列下方原样显示（截断仅用于版面，全文留在 title 里）。
      const parseErrorHtml = (file.parse_status === "failed" && parseError)
        ? '<div class="parse-error tiny" style="margin-top:4px;color:var(--color-danger,#b3261e);word-break:break-all"' +
          ' title="' + esc(parseError) + '">解析失败：' +
          esc(parseError.length > 160 ? parseError.slice(0, 160) + "…" : parseError) +
          "</div>"
        : "";
      // 「性质」列：先如实显示当前取值（空 = 「待识别」，来源/原因在 title 里），
      // 再给行内人工纠正下拉。二者同源（同一个 file 对象），不会一个显示 A、
      // 一个显示 B。
      const natureTitle = KB.natureTooltip(file);
      // `natureCell` **只含单元格内容**（`<span>` + 行内下拉）：外层统一包 `<td>`。
      // 早先它自带 `<td>` 又在行模板里被 `'<td class="tiny">' + natureCell + "</td>"` 包了
      // 一层 → 生成嵌套 `<td>`；浏览器自动修复会插出多余空列，后面的「大小/状态/分段」
      // 整体错位（Codex 用真实浏览器发现）。嵌套是 HTML 解析层面的错误，字符串测试
      // 看不出来，所以另有真实浏览器断言：每行 td 数 == 表头 th 数、各列按序对应。
      const natureCell = '<span class="nature-label"' +
        (natureTitle ? ' title="' + esc(natureTitle) + '"' : "") + ">" +
        esc(KB.natureLabel(file)) + "</span>" + natureSelect(file);
      return '<tr data-file-row="' + file.id + '">' +
        // 行复选框必须有自己的可访问名（4.1.2）：文件名是唯一的识别依据，
        // 直接复用 data-name（同一条数据，视觉与读屏不会各说各话）。
        '<td><input type="checkbox" class="file-check" value="' + file.id +
          '" data-name="' + esc(file.original_name) +
          '" aria-label="选择《' + esc(file.original_name) + '》"></td>' +
        '<td><div class="name">' + KB.fileIcon(file.file_type) +
          '<b title="' + esc(file.original_name) + '">' +
          esc(file.original_name) + "</b></div></td>" +
        '<td class="tiny"><span class="lib-chip">' +
          esc(file.library_name || file.library_code || "—") +
          '</span><br><span class="muted">' + esc(dir) + "</span></td>" +
        '<td class="tiny">' + esc(file.file_type_label || file.file_type || "—") +
          "</td>" +
        '<td class="tiny">' + natureCell + "</td>" +
        '<td class="tiny mono">' + esc(file.size_display || KB.fmtSize(file.size_bytes)) + "</td>" +
        '<td class="file-processing-progress">' + KB.progress.widget('file', file.id, file.progress || {
          state: {done: 'done', failed: 'failed', running: 'running', pending: 'queued'}[file.parse_status] || 'cancelled',
          label: file.status_label || STATUS_TEXT[file.parse_status], percent: file.parse_status === 'done' ? 100 : null,
        }) + '</td>' +
        '<td class="tiny mono" data-file-chunks>' + mono(file.chunk_count || 0) + "</td>" +
        '<td class="tiny mono" data-file-time>' + esc(shortTime(file.parsed_at || file.created_at)) + "</td>" +
        '<td><div class="table-actions">' +
          '<button data-detail="' + file.id + '">详情</button>' +
          '<button data-preview="' + file.id + '">预览</button>' +
          // 权限（**服务端**判定，这里只做显示层，处置清单 AF-26）：
          //   * 解析/重新解析 → 可写入成员（后端 CanContributeLibrary）
          //   * 改性质、删除 → 该库管理员（CanManageLibrary / library:admin）
          // 无权限时按钮置灰并写明原因，**不给一个注定 403 的入口**；
          // 但绝不因此放松后端校验（后端永远说了算）。
          permissionCellHtml(file, parseBusy, parseTitle) +
        "</div>" + parseErrorHtml + "</td></tr>";
    }).join("");
    bindRows();
    updateSelectionNote();
    updatePermissionNote();
    updateUploadGate();
  }

  /** 操作列里的 4 个动作按钮（权限置灰 + 原有状态禁用**同时**生效）。
   *
   * 两种禁用原因并存时，把两个原因都写进 title：否则"已归档所以不能解析"会被
   * 权限原因盖掉，用户按提示去申请权限也还是点不动。
   */
  function permissionCellHtml(file, parseBusy, parseTitle) {
    const needWriter = !canActOn(file, "writer");
    const needAdmin = !canActOn(file, "admin");
    const parts = [];

    /** 拼一个动作按钮：**属性显式拼**（不再用字符串 replace 打补丁，那样很容易
     *  把已有的 `title`（状态机原因）覆盖掉，也很容易漏掉转义）。 */
    const actionButton = function (attrs, label, permissionReason) {
      const bits = ['<button'];
      Object.keys(attrs).forEach(function (key) {
        const value = attrs[key];
        if (value === false || value === undefined || value === null || value === "") return;
        if (value === true) { bits.push(" " + key); return; }
        bits.push(" " + key + '="' + esc(value) + '"');
      });
      if (permissionReason) {
        // 权限原因**排在状态机原因前面**（两个都在时要都能读到）
        const combined = permissionReason + (attrs.title ? " " + attrs.title : "");
        bits.push(' title="' + esc(combined) + '"');
        bits.push(' data-perm-denied="1"');
        bits.push(" disabled");
      }
      bits.push(">" + label + "</button>");
      return bits.join("");
    };

    const parseButton = actionButton(
      { "data-parse": file.id, disabled: parseBusy, title: parseTitle || "" },
      file.parse_status === "failed" ? "重新解析" : "解析",
      needWriter ? writeNeedText(file) : ""
    );

    const deleteButton = actionButton(
      { class: "danger", "data-del": file.id },
      "删除",
      needAdmin ? adminNeedText(file, "删除") : ""
    );

    if (needWriter) deniedActionCounts.parse += 1;
    if (needAdmin) deniedActionCounts.delete += 1;
    parts.push(parseButton, deleteButton);
    return parts.join("");
  }

  /** 「性质」单元格 = **如实显示**（五项之一 / 待识别）+ 行内人工纠正下拉。
   *
   * 显示部分完全按接口返回：
   *   * `classification` 空 → 「待识别」（`KB.natureLabel`，不显示空白、不显示「—」）；
   *   * 来源与依据/失败原因取 `classification_source` / `classification_note`，
   *     写在 `title` 和下拉的悬浮说明里（`KB.natureTooltip`）。接口没给来源就
   *     **不写来源**，绝不替后端说一句"已自动识别"。
   *
   * 下拉部分（保留行内人工纠正）：五项词表 + 留空。写库仍走受控操作
   * `classify_file`（该库管理员权限校验后直接执行 + 审计含旧值→新值）。
   * 人工选中的来源就是 `manual`，所以提示文案必须写清"**不会被自动识别覆盖**"。
   *
   * 历史值（词表之外的旧数据，例如接口导入的「技术文档」）也原样列出来并标注
   * 「历史值」：不能因为它在词表外就让下拉显示成"待识别"——那是篡改事实。
   */
  function natureSelect(file) {
    const current = file.classification || "";
    const known = NATURE_VOCABULARY.indexOf(current) >= 0;
    // 空值这一项就是「待识别」，文案要短（表格里只放得下几个字），解释放进 title。
    //
    // **注意（口径更正）**：服务端把"人工选空"也记为 `classification_source=manual`
    // （`apps/planner/services.py` 的 classify_file："清空（value=\"\"）也必须置 manual"），
    // 而自动识别看到 manual 就**永远跳过**（`apps/knowledge/classification.py` 的
    // `skipped_manual`）。所以"清空后后端会按正文自动识别"是**不成立的承诺** ——
    // 旧 title 正是这么写的，必须改掉（本轮不新增"重新自动识别"的操作，也就不承诺）。
    const options = ['<option value=""' + (current ? "" : " selected") +
      ' title="留空 = 性质为空（待识别）。人工清空会记为人工设置，' +
      '自动识别不会覆盖它；要重新按正文识别需要系统侧的新操作（本页暂不提供）。">' +
      KB.NATURE_PENDING + "</option>"];
    if (current && !known) {
      options.push('<option value="' + esc(current) + '" selected>' + esc(current) +
        "（历史值）</option>");
    }
    NATURE_VOCABULARY.forEach(function (value) {
      options.push('<option value="' + esc(value) + '"' +
        (current === value ? " selected" : "") + ">" + esc(value) + "</option>");
    });
    // 下拉的 title：当前到底是什么（来源/依据）+ 人工选择的后果（不会被覆盖）
    const sourceLabel = KB.natureSourceLabel(file);
    const currentLine = "当前：" + KB.natureLabel(file) +
      (sourceLabel ? "（来源：" + sourceLabel + "）" : "") + "。";
    const tooltip = KB.natureTooltip(file);
    const baseTitle = currentLine + (tooltip ? tooltip + "。" : "") +
      "人工选择五项之一：该信息库管理员选择后直接写入，来源记为人工设置（manual），" +
      "自动识别不会覆盖它；选「" + KB.NATURE_PENDING + "」= 人工清空，" +
      "性质保持为空、不会被自动识别覆盖（要重新按正文识别需要系统侧的新操作，本页暂不提供）。" +
      "只动这一个元数据字段，原件/分段/向量都不变。";
    // 改性质是 `library:admin` 的受控操作：非库管理员拿到的是一个注定 403 的
    // 下拉框（AF-26）。这里置灰并写明原因 —— 仍然只是**显示层**。
    const denied = !canActOn(file, "admin");
    if (denied) deniedActionCounts.classify += 1;
    return '<select class="nature-select" data-nature="' + file.id + '"' +
      (denied ? ' disabled data-perm-denied="1"' : "") +
      ' title="' + esc(denied
        ? adminNeedText(file, "修改资料性质") + baseTitle
        : baseTitle) + '">' +
      options.join("") + "</select>";
  }

  // ------------------------------------------------------ 预览 / 详情 ----

  async function openPreview(id) {
    // **统一入口**：真实原件预览（PDF 按原页渲染 / 图片原图 / 文本保留换行 /
    // DOCX-XLSX 结构化提取并标注"非原始版式" / 原件缺失时明确标注索引文字）。
    // 旧实现把 `chunks.text` 塞进 <p>：那是索引文字，不是原件预览 ——
    // PDF 的原页/表格/水印位置全丢，水印还会混进正文。
    return KB.previewFile(id);
  }

  function openDetail(id) {
    const file = currentRows.filter(function (row) { return row.id === Number(id); })[0];
    if (!file) {
      // **点了必须有反应**（处置清单 AF-28）：旧实现直接 return，用户点了没反应，
      // 只能猜"是不是坏了"。找不到通常是因为列表在这期间被刷新（筛选/翻页）过。
      KB.toast("这份资料不在当前列表里（页面可能刚刷新过）；请刷新后重试", "warn");
      showPageNote("打开详情失败：这份资料不在当前列表中（列表可能已被筛选/翻页刷新）。" +
        "请刷新页面后按资料 ID 重试，或先用文件名搜索它。", "warn");
      return;
    }
    const kv = function (key, value) {
      return '<tr><th style="width:120px">' + esc(key) + "</th><td>" +
        esc(value === null || value === undefined || value === "" ? "—" : String(value)) +
        "</td></tr>";
    };
    KB.modal(
      '<div class="modal-head"><b>资料详情</b><span class="grow"></span><span class="tag">' +
        esc(file.status_label || "") + "</span></div>" +
      '<div class="modal-body">' +
        '<table class="paper" style="width:100%;border-collapse:collapse">' + [
          kv("文件名", file.original_name),
          kv("目录", file.directory_path),
          kv("来源", file.source === "session" ? "AI 会话" : file.source),
          kv("类型", (file.file_type_label || "其他") +
            (file.file_type ? "（." + file.file_type + "）" : "")),
          kv("归属客户", file.customer),
          kv("所属部门", file.department),
          kv("资料性质", KB.natureLabel(file) +
            (KB.natureSourceLabel(file) ? "（来源：" + KB.natureSourceLabel(file) + "）" : "")),
          kv("性质依据/原因", file.classification_note),
          kv("金额（万元）", file.amount_wan),
          kv("文件大小", file.size_display || KB.fmtSize(file.size_bytes)),
          kv("段落数", file.chunk_count),
          '<tr><th>处理进度</th><td>' + KB.progress.widget("file", file.id, file.progress) + '</td></tr>',
          kv("处理尝试", file.parse_attempts),
          kv("内容指纹", (file.content_sha256 || "").slice(0, 16)),
          kv("创建时间", KB.fmtTime(file.created_at)),
          kv("更新时间", KB.fmtTime(file.updated_at)),
          kv("错误信息", file.parse_error),
        ].join("") + "</table>" +
      "</div>" +
      '<div class="modal-foot"><button class="btn sm" data-close>关闭</button></div>'
    );
  }

  // ---------------------------------------------------------- 上传 ----

  /** 上传：**先选信息库**；目录上传把相对路径写进 directory_path。 */
  async function uploadFiles(files) {
    const list = Array.from(files || []);
    if (!list.length) return;
    if (uploadBusy) {
      KB.toast("上一批还在上传，请稍候");
      return;
    }
    const libraries = await KB.api("/api/v1/knowledge/libraries/?page_size=200");
    const allRows = (libraries.ok && libraries.body &&
      (libraries.body.results || libraries.body)) || [];
    // **只列可写入的库**：上传后端校验是 `CanContributeLibrary`（writer），
    // 列出只读库等于给用户一个注定 403 的入口。
    const writable = await KB.fetchWritableLibraries(allRows);
    const rows = writable.rows;
    if (!rows.length) {
      KB.toast("还没有信息库，请先创建");
      return;
    }
    const chosen = await KB.choice({
      title: "上传到哪个信息库",
      note: "共 " + list.length + " 份文件" +
        (state.session ? "；来源记为“会话来源”" : "") +
        (writable.blocked.length
          ? "。已隐去 " + writable.blocked.length +
            " 个你没有写入权限的信息库（列出来也无法上传成功）。"
          : ""),
      options: rows.map(function (row) {
        return { value: row.code, label: row.name + "（" + row.code + "）" };
      }),
      confirmLabel: "开始上传",
    });
    if (!chosen) return;

    // 「性质」：**默认留空 = 由后端按正文内容自动识别**（识别结果与依据以接口
    // 返回的 `classification` / `classification_source` / `classification_note`
    // 为准，前端不预告"已自动识别"）。选了某一项就是人工指定：写入走上传参数
    // `classification`，来源记为人工设置，**不会被自动识别覆盖**。
    // 目录扫描/批量上传常常一时分不清，所以留空是默认值而不是例外。
    const nature = await KB.choice({
      title: "这批资料的「性质」",
      note: "不人工指定 = 由系统按正文内容自动识别（识别结果显示在列表的「性质」列，" +
        "空值会显示「待识别」并给出依据）。也可以在这里人工指定：人工选择记为人工设置，" +
        "自动识别**不会**覆盖它。",
      options: [{
        value: "",
        label: "不人工指定（由系统按正文自动识别）",
        title: "默认：不人工指定，由系统按正文内容识别为 申请/合同/报表/普通文档/涉密文档 之一",
      }].concat(
        NATURE_VOCABULARY.map(function (value) {
          return { value: value, label: value + "（人工指定，不会被自动识别覆盖）" };
        })
      ),
      confirmLabel: "继续上传",
    });
    if (nature === null) return;
    if (nature) {
      // 人工指定必须在界面上**当时**说明清楚（与行内修改同一个口径）
      KB.toast("已人工指定「" + nature + "」：不会被自动识别覆盖");
    }

    // 逐份上传。**每一份都要有可见进度**，失败必须报出**服务端给的原因**
    // （独立审计：旧实现只用原始 fetch、只看 response.ok，失败静默丢弃 ——
    // 用户以为"已提交 1/5"是全部，实际另外 4 份是因为什么被拒都不知道）。
    const failures = [];
    const duplicates = [];
    const uploadProgress = batchProgress("正在上传资料");
    let uploaded = 0;
    uploadBusy = true;
    // **开始新一批时先重置提示区**（处置清单 AF-27）：旧实现只在"本批全成功"时
    // 才清空，所以上传过程中页面上留着的是**上一批**的失败清单，用户会读成本批结果。
    KB.notice("files-upload-note",
      "正在上传 " + list.length + " 份到「" + chosen + "」…结果会写在这里。", "info");
    const uploadButton = $("files-upload");
    const inputButton = $("files-input");
    const folderButton = $("files-folder-button");
    if (uploadButton) uploadButton.disabled = true;
    if (inputButton) inputButton.disabled = true;
    if (folderButton) folderButton.disabled = true;
    try {
      for (let index = 0; index < list.length; index += 1) {
        const file = list[index];
        uploadProgress.update({current: index, total: list.length, percent: Math.floor(index/list.length*100), unit: "份", detail: file.name});
        const form = new FormData();
        form.append("file", file);
        form.append("library", chosen);
        // 性质：留空就**不传这个字段**（= 后端按正文自动识别）；传了就是人工指定
        // （服务端按原值落库，`classification_source` 记为人工设置）
        if (nature) form.append("classification", nature);
        const relative = file.webkitRelativePath || "";
        if (relative) {
          const parts = relative.split("/");
          // **保留整个原相对目录**（此前 slice(1,-1) 会丢掉顶层目录名）
          form.append("directory_path", "/" + parts.slice(0, -1).join("/"));
        }
        // 文件管理页上传永远是"人工上传/目录扫描"，**不能**因为界面上的
        // "来自 AI 会话"筛选开关而误记成会话来源（那是助手添加的口径）。
        form.append("source", relative ? "scan" : "manual");
        KB.toast("正在上传 " + (index + 1) + " / " + list.length + "：" + file.name);
        const result = await KB.api("/api/v1/knowledge/files/upload/", {
          method: "POST", form: form,
        });
        if (!result.ok) {
          const detail = (result.body && (result.body.detail ||
            JSON.stringify(result.body))) || ("HTTP " + result.status);
          failures.push(file.name + "：" + detail);
        } else if (result.body && result.body.created === false &&
                   result.body.reason === "duplicate_content") {
          // 重复内容不新建记录（内容寻址去重）。**不能**把它混进"已提交"里，
          // 否则用户会以为多了一份；同时把服务端说明（例如"此前未处理成功，
          // 已重新排队"或"原件已补齐"）如实转达。
          const note = (result.body.processing && result.body.processing.detail) || "";
          duplicates.push(file.name + (note ? "：" + note : ""));
        }
        uploaded = index + 1;
      }
    } finally {
      uploadProgress.finish(uploaded === list.length && !failures.length,
        "上传结束：" + (uploaded - failures.length) + " / " + list.length + " 份已受理");
      uploadBusy = false;
      if (uploadButton) uploadButton.disabled = false;
      if (inputButton) inputButton.disabled = false;
      if (folderButton) folderButton.disabled = false;
    }
    const ok = list.length - failures.length - duplicates.length;
    if (failures.length || duplicates.length) {
      // 有失败或有去重命中时**不**报"已提交 N/M"就完事：把原因留在页面上
      const parts = ["已提交 " + ok + " / " + list.length + " 份"];
      if (duplicates.length) {
        parts.push(duplicates.length + " 份内容已在库中（同一内容只保留一条记录）：" +
          duplicates.join("；"));
      }
      if (failures.length) {
        parts.push(failures.length + " 份未提交：" + failures.join("；"));
      }
      KB.notice("files-upload-note", parts.join("。") + "。",
        failures.length ? "danger" : "warn");
      KB.toast("已提交 " + ok + " / " + list.length + " 份（详见页面提示）");
    } else {
      const note = $("files-upload-note");
      if (note) note.innerHTML = "";
      KB.toast("已提交 " + ok + " / " + list.length + " 份（可在本页查看处理状态）");
    }
    await refresh();
  }

  // ------------------------------------------------------ 行内动作 ----

  function bindRows() {
    document.querySelectorAll("[data-preview]").forEach(function (button) {
      button.addEventListener("click", function () {
        openPreview(button.getAttribute("data-preview"));
      });
    });
    document.querySelectorAll("[data-detail]").forEach(function (button) {
      button.addEventListener("click", function () {
        openDetail(button.getAttribute("data-detail"));
      });
    });
    document.querySelectorAll("[data-parse]").forEach(function (button) {
      button.addEventListener("click", function () {
        processFile(button.getAttribute("data-parse"), button);
      });
    });
    document.querySelectorAll("[data-nature]").forEach(function (select) {
      select.addEventListener("change", function () {
        classifyFile(select.getAttribute("data-nature"), select.value);
      });
    });
    document.querySelectorAll("[data-del]").forEach(function (button) {
      button.addEventListener("click", function () {
        deleteFile(button.getAttribute("data-del"));
      });
    });
  }

  /**
   * 行内"解析 / 重新解析"。
   *
   * 失败原因**如实转达**：服务端的 `detail`（PipelineError 的原话）必须原样出现，
   * 不能替换成"操作失败"这类通用文案；同时留在页面提示区，不靠会消失的 toast。
   * 请求进行中把该行按钮禁用（连点会重复提交同一份资料）。
   */
  async function processFile(id, button) {
    if (button && button.disabled) return;
    if (button) button.disabled = true;
    try {
      const response = await KB.progress.fileRequest("/api/v1/knowledge/files/" + id + "/process/", {
        method: "POST", json: {},
      });
      if (!response.ok) {
        const reason = (response.body && response.body.detail) ||
          ("HTTP " + response.status + "（服务端未给出原因）");
        KB.toast("解析未受理：" + reason);
        showPageNote("资料 #" + id + " 解析未受理：" + reason, "danger");
        return;
      }
      const detail = (response.body && (response.body.detail || response.body.status)) || "已受理";
      KB.toast("已受理：" + detail);
      await refresh();
    } finally {
      if (button) {
        const file = currentRows.find(function (row) { return row.id === Number(id); });
        button.disabled = button.dataset.permDenied === "1" ||
          Boolean(file && ["done", "running", "archived"].includes(file.parse_status));
      }
    }
  }

  /** 删除：**必须走受控提案**（三次独立确认 + 一次性凭证）。 */
  async function deleteFile(id) {
    const outcome = await KB.controlled({
      intent: "delete_file",
      params: { file_id: Number(id) },
      labels: { title: "删除资料" },
    });
    if (outcome.skipped) {
      KB.toast("已取消，未做任何改动");
      return;
    }
    if (!outcome.ok) {
      KB.toast(outcome.detail);
      return;
    }
    const result = outcome.result || {};
    KB.toast("已删除" + (result.target ? "：" + result.target : "") +
      (result.complete === false ? "（向量清理尚未完成）" : ""));
    await refresh();
  }

  /**
   * 修改性质：**必须走受控提案**（该库管理员权限校验后直接执行 + 审计含旧值→新值）。
   *
   * 为什么不做成普通表单直改：`classification` 是被数据库触发器保护的字段，
   * 普通 PUT/PATCH 会被治理层拒绝。受控通道是**唯一**的写入方式，所以这里必须
   * 走 `classify_file`，并且要在结果里如实说清"只动了这一个元数据字段"。
   *
   * 因为性质现在默认由**正文自动识别**，人工写入的口径必须在提示里讲明：
   * 来源记为 manual（人工设置），**不会被自动识别覆盖** —— 否则用户会以为
   * 自己刚定的性质下次自动识别时又会被改掉。
   *
   * 清空（选「待识别」）也是受控写入：把 `classification` 写成空串，等于
   * "还没识别出来"，之后可由后端按正文重新自动识别。
   *
   * 取消或失败时**重渲染本页行**：下拉回到服务端的真实值，界面不留"好像改成了"
   * 的假状态（这是最容易骗到用户的地方）。
   */
  async function classifyFile(id, next) {
    const file = currentRows.filter(function (row) {
      return row.id === Number(id);
    })[0] || {};
    const previous = file.classification || "";
    const outcome = await KB.controlled({
      intent: "classify_file",
      params: { file_id: Number(id), classification: next || "" },
      labels: { title: next ? "修改资料性质 → " + next : "清空资料性质" },
      extraLines: [
        "目标资料：" + (file.original_name || ("#" + id)),
        "性质：" + (previous || KB.NATURE_PENDING) + " → " + (next || KB.NATURE_PENDING),
        next
          ? "这是**人工设置**（来源记为人工设置，不会被自动识别覆盖）。"
          : "人工选空也记为人工设置：该资料性质**保持为空**（不被自动识别覆盖）。" +
            "若之后要按正文重新识别，需要系统侧的新操作（本页暂不提供）。",
        "只改这一个元数据字段：原件字节、分段、向量与解析状态都不动。",
      ],
    });
    if (outcome.skipped) {
      KB.toast("已取消，性质未改动");
      renderRows(currentRows);
      return;
    }
    if (!outcome.ok) {
      KB.toast(outcome.detail);
      renderRows(currentRows);
      return;
    }
    const result = outcome.result || {};
    if (result.changed === false) {
      KB.toast("该资料的性质本来就是「" + (next || KB.NATURE_PENDING) + "」，未做改动");
    } else if (next) {
      KB.toast("已人工设置「" + next + "」（不会被自动识别覆盖）；审计已记录旧值 → 新值");
    } else {
      KB.toast("已清空性质（现为「" + KB.NATURE_PENDING + "」）；审计已记录旧值 → 新值");
    }
    await refresh();
  }

  /** 全部解析：勾选优先，否则把待处理 + 失败的全部发起。 */
  //: 沿分页取全量的页数上限。到顶仍未取完就**取消**，绝不拿部分当全部。
  const MAX_SCAN_PAGES = 200;

  /** 当前筛选条件下的**全部**资料：沿分页 next 一路取完。
   *
   * 三种"取不全"的情况都必须**抛错取消**，而不是返回部分列表：
   *   - 中途某个请求失败（网络/权限/500）；
   *   - 页数达到上限而 next 仍在（说明还有没取到的）。
   * 静默返回部分结果会导致"全部解析"只处理了一部分却报告全部受理。
   */
  async function fetchAllFiltered() {
    const rows = [];
    const params = new URLSearchParams();
    if (state.q) {
      params.set("q", state.q);
      params.set("mode", "filename");   // 与列表页保持一致：按文件名匹配
    }
    if (state.status !== "all") params.set("status", state.status);
    if (state.library !== "all") params.set("library", state.library);
    if (state.fileType !== "all") params.set("file_type", state.fileType);
    if (state.nature !== "all") params.set("nature", state.nature);
    if (state.session) params.set("session", "1");
    params.set("page_size", "200");

    let url = "/api/v1/knowledge/files/?" + params.toString();
    let guard = 0;
    while (url) {
      if (guard >= MAX_SCAN_PAGES) {
        throw new Error("资料超过 " + MAX_SCAN_PAGES +
          " 页仍未取完；为避免只处理一部分，本次已取消");
      }
      const result = await KB.api(url);
      if (!result.ok) {
        throw new Error(
          (result.body && result.body.detail) || "读取资料列表失败；本次已取消"
        );
      }
      const body = result.body || {};
      rows.push.apply(rows, body.results || body);
      // 只认服务端给的 next：取完为止，绝不假设"一页就是全部"
      url = body.next || null;
      guard += 1;
    }
    return rows;
  }

  //: 单次动作运行期间要一起禁用的控件：连点会重复提交，必须真互斥。
  const BUSY_CONTROLS = [
    "files-kw", "files-lib", "files-type", "files-nature", "files-status",
    "files-session", "files-prev", "files-next", "files-upload", "files-parse-all",
  ];

  /** 每个控件进入忙碌前的 disabled 状态，恢复时**按原值**还原（不一律打开）。 */
  const busyRestore = {};

  function setBusy(on) {
    parseBatchBusy = on;
    BUSY_CONTROLS.forEach(function (id) {
      const element = $(id);
      if (!element) return;
      if (on) {
        if (!(id in busyRestore)) busyRestore[id] = element.disabled;
        element.disabled = true;
      } else if (id in busyRestore) {
        element.disabled = busyRestore[id];
        delete busyRestore[id];
      }
    });
    document.querySelectorAll("[data-parse]").forEach(function (button) {
      const key = "parse:" + button.getAttribute("data-parse");
      if (on) {
        if (!(key in busyRestore)) busyRestore[key] = button.disabled;
        button.disabled = true;
      } else if (key in busyRestore) {
        const file = currentRows.find(function (row) { return row.id === Number(button.getAttribute("data-parse")); });
        button.disabled = button.dataset.permDenied === "1" ||
          Boolean(file && ["done", "running", "archived"].includes(file.parse_status));
        delete busyRestore[key];
      }
    });
  }

  /** 跳过原因的**可读文案**：每类各几份 + 为什么（不合并成一句含糊的话）。 */
  function skippedText(skipped) {
    const parts = [];
    if (skipped.done) {
      parts.push(skipped.done + " 份已完成，无需重复解析");
    }
    if (skipped.running) {
      parts.push(skipped.running + " 份正在处理中，请等它跑完");
    }
    if (skipped.archived) {
      parts.push(skipped.archived + " 份为历史归档状态，已跳过");
    }
    return parts.join("；");
  }

  async function parseAll() {
    const trigger = $("files-parse-all");
    if (parseBatchBusy || (trigger && trigger.disabled)) return;   // 互斥：运行中再点直接忽略
    // 按库的写入权限判定（按钮已置灰；这里再挡一次，因为 setBusy 会临时接管
    // 它的 disabled 状态，程序化触发不该绕过这条规则）。
    if (!bulkParseAllowed()) {
      KB.toast(state.library === "all"
        ? "「全部解析」需要你有每个可读信息库的写入权限；请先筛选到你有写入权限的库，或用每一行的「解析」"
        : "「全部解析」：需要你在「" + (state.library || "该信息库") +
          "」有写入权限；可联系该库管理员开通，或改用每一行的「解析」", "err");
      return;
    }
    setBusy(true);
    try {
      const checked = Array.from(document.querySelectorAll(".file-check"))
        .filter(function (box) { return box.checked; })
        .map(function (box) { return box.value; });
      // 被跳过的份数必须**明确说出来**（旧实现只提"已归档"，已完成/处理中被静默跳过）
      const skipped = { archived: 0, done: 0, running: 0 };
      let targets;
      if (checked.length) {
        targets = currentRows.filter(function (row) {
          return checked.indexOf(String(row.id)) >= 0 &&
            // **勾选也不能绕过归档**：批量解析只处理"待解析/失败"两份状态，
            // 历史归档件仍不可由普通解析改变状态
            ["pending", "failed"].indexOf(row.parse_status) >= 0;
        });
        currentRows.forEach(function (row) {
          if (checked.indexOf(String(row.id)) < 0) return;
          if (row.parse_status in skipped) skipped[row.parse_status] += 1;
        });
      } else {
        // **不能只处理当前页，也不能只取一页**：沿 next 把筛选结果全部取回。
        // 取不全就取消——否则会"只处理一部分却报告全部受理"。
        let rows;
        try {
          rows = await fetchAllFiltered();
        } catch (error) {
          KB.toast(error.message || "读取资料列表失败；本次已取消");
          return;
        }
        rows.forEach(function (row) {
          if (row.parse_status in skipped) skipped[row.parse_status] += 1;
        });
        targets = rows.filter(function (row) {
          return ["pending", "failed"].indexOf(row.parse_status) >= 0;
        });
      }
      const skippedTotal = skipped.archived + skipped.done + skipped.running;
      if (!targets.length) {
        // 一份都没得解析时更要讲清楚为什么，否则用户只看到"没有需要解析的资料"
        KB.toast(skippedTotal
          ? ("没有需要解析的资料：已跳过 " + skippedTotal +
            " 份（" + skippedText(skipped) + "）")
          : "没有需要解析的资料");
        return;
      }
      let ok = 0;
      const failed = [];
      const batch = batchProgress("正在批量解析");
      let completed = 0;
      try {
      for (const row of targets) {
        batch.update({current: completed, total: targets.length, percent: Math.floor(completed/targets.length*100), unit: "份", detail: row.original_name});
        const response = await KB.progress.fileRequest("/api/v1/knowledge/files/" + row.id + "/process/", {
          method: "POST", json: {},
        });
        if (response.ok) {
          ok += 1;
        } else {
          // 失败必须带**服务端给的原因**，不能只报文件名
          const reason = (response.body && response.body.detail) || ("HTTP " + response.status);
          failed.push(row.original_name + "（" + reason + "）");
        }
        completed += 1;
      }
      } finally {
        batch.finish(completed === targets.length && !failed.length,
          "本批解析：成功 " + ok + " / " + targets.length + " 份" + (failed.length ? "，失败 " + failed.length + " 份" : ""));
      }
      // **如实报告**：失败要说清几份 + 原因；被跳过要说清几份 + 为什么
      const parts = ["已受理 " + ok + " / " + targets.length + " 份"];
      if (failed.length) {
        parts.push(failed.length + " 份未能受理：" + failed.slice(0, 3).join("、") +
          (failed.length > 3 ? " 等" : ""));
      }
      if (skippedTotal) {
        parts.push("已跳过 " + skippedTotal + " 份（" + skippedText(skipped) + "）");
      }
      const message = parts.join("；");
      KB.toast(message);
      if (failed.length || skippedTotal) showPageNote(message, failed.length ? "danger" : "warn");
      await refresh();
    } finally {
      setBusy(false);   // 无论成功/失败/取消都要恢复，绝不让界面卡在禁用态
    }
  }

  // ------------------------------------------------------------ 绑定 ----

  function bind() {
    let timer = null;
    $("files-kw").addEventListener("input", function () {
      clearTimeout(timer);
      timer = setTimeout(function () {
        state.q = $("files-kw").value.trim();
        state.page = 1;
        loadFiles();
      }, 300);
    });
    $("files-lib").addEventListener("change", function () {
      state.library = $("files-lib").value;
      state.page = 1;
      // 换了库 → 上传/扫描目录与「全部解析」的写入权限都要跟着重算
      updateUploadGate();
      updateBulkActionsGate();
      loadFiles();
    });
    $("files-type").addEventListener("change", function () {
      state.fileType = $("files-type").value;
      state.page = 1;
      loadFiles();
    });
    $("files-status").addEventListener("change", function () {
      state.status = $("files-status").value;
      state.page = 1;
      loadFiles();
    });
    // 性质筛选（全部 / 五项 / 待识别）。「待识别」是否真能筛由 loadFiles 的
    // 实测判定并在页面上说明 —— 不做任何"选了就当生效"的假设。
    const natureFilter = $("files-nature");
    if (natureFilter) {
      natureFilter.addEventListener("change", function () {
        state.nature = natureFilter.value;
        state.page = 1;
        loadFiles();
      });
    }
    $("files-session").addEventListener("click", function () {
      state.session = !state.session;
      state.page = 1;
      updateSessionFilterState();   // 文案/无障碍状态跟着真实开关走（含 aria-pressed）
      loadFiles();
    });
    $("files-prev").addEventListener("click", function () {
      if (state.page > 1) { state.page -= 1; loadFiles(); }
    });
    $("files-next").addEventListener("click", function () {
      state.page += 1;
      loadFiles();
    });
    $("check-all").addEventListener("change", function () {
      const on = $("check-all").checked;
      document.querySelectorAll(".file-check").forEach(function (box) { box.checked = on; });
      updateSelectionNote();
    });
    // 行复选框：勾选数量一变就更新文案；与服务端"列表还没重载"无关。
    // 事件委托绑在 tbody 上，行被整段重渲染也不会丢监听。
    const rowsBody = $("files-body");
    if (rowsBody) {
      rowsBody.addEventListener("change", function (event) {
        if (!event.target || event.target.className.indexOf("file-check") < 0) return;
        updateSelectionNote();
        // 全选态只在"本页全部勾上"时才是勾着的（归零由 renderRows 负责）
        const boxes = Array.from(document.querySelectorAll(".file-check"));
        const checkAll = $("check-all");
        if (checkAll) {
          checkAll.checked = boxes.length > 0 && boxes.every(function (box) {
            return box.checked;
          });
        }
      });
    }
    $("files-upload").addEventListener("click", function () { $("files-input").click(); });
    $("files-input").addEventListener("change", function (event) {
      uploadFiles(event.target.files);
      event.target.value = "";
    });
    $("files-folder-button").addEventListener("click", function () { $("files-folder").click(); });
    $("files-folder").addEventListener("change", function (event) {
      uploadFiles(event.target.files);
      event.target.value = "";
    });
    $("files-parse-all").addEventListener("click", parseAll);
    // 「重置数据库」按钮及其说明弹窗已整条删除（用户要求）：它没有任何真实动作，
    // 只弹"本按钮不清库"的说明，属于误导入口。可用的受控替代入口：
    //   重置索引（reset_library_index，不删资料/不擦原件）、逐份受控删除、删除信息库。
  }

  async function refresh() {
    await Promise.all([loadStats(), loadFacets(), loadCapabilities()]);
    await loadFiles();
  }

  document.addEventListener("DOMContentLoaded", async function () {
    bind();
    // 支持从别的页面带筛选进来：/app/files/?status=pending&session=1&lib=xxx
    // 性质也支持带上：`?nature=合同` 或 `?nature=__unidentified__`（待识别）
    const params = new URLSearchParams(location.search);
    if (params.get("nature")) {
      state.nature = params.get("nature");
      const natureFilter = $("files-nature");
      if (natureFilter) natureFilter.value = state.nature;
    }
    if (params.get("status")) {
      state.status = params.get("status");
      $("files-status").value = state.status;
    }
    if (params.get("session") === "1") {
      state.session = true;
      updateSessionFilterState();   // 从 URL 带进来时也要写上 aria-pressed（AF-29）
    }
    if (params.get("lib")) state.library = params.get("lib");
    if (params.get("q")) {
      state.q = params.get("q");
      $("files-kw").value = state.q;
    }
    await refresh();
  });
})();
