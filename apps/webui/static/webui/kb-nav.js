/* 左侧导航 + 顶部徽标 + 重置入口 —— 对应原型 renderSidebar() 与启动段。
 *
 * 原型用 hash 路由（#/files）；本项目用**真实路径路由**（/app/files/），
 * 这样保留服务端会话认证、深链和登录跳转。导航项、图标、顺序与原型 NAV 一致。
 */
(function () {
  "use strict";

  const KB = window.KB;

  // 与原型 NAV 完全一致（顺序、图标、文案）
  const NAV = [
    ["/app/", "首页", "◆"],
    ["/app/files/", "文件管理", "▤"],
    ["/app/templates/", "模板管理", "▧"],
    ["/app/search/", "搜索", "◎"],
    ["/app/agent/", "Agent 控制台", "◈"],
    ["/app/settings/", "系统配置", "⚙︎"],
    ["/app/knowledge/", "信息库", "▥"],
    ["/app/tasks/", "添加记录", "≡"],
    ["/app/docs/", "使用说明", "▫"],
  ];

  function normalize(path) {
    if (!path.endsWith("/")) return path + "/";
    return path;
  }

  /** 最近一次渲染时已知的"当前会话"（供不带参的骨架渲染复用，避免高亮被冲掉）。 */
  let lastActiveSession = null;

  /**
   * 当前页在导航里的**归属路径**：`/app/assistant/` 是首页会话页的另一个入口
   * （`apps/webui/urls.py` 同时挂了 `""` 与 `"assistant/"`），NAV 里只有 `/app/`。
   * 不做归一的话，从 `/app/assistant/` 进来时导航**一个都不高亮**（Codex 审计 OP-16）。
   */
  function navHome(path) {
    const normalized = normalize(path);
    if (normalized === "/app/assistant/") return "/app/";
    return normalized;
  }

  /** 渲染左侧导航（结构同原型：sidebar-menu + session-block + sidebar-foot）。 */
  function renderSidebar(options) {
    const box = document.getElementById("sidebar");
    if (!box) return;
    const current = navHome(location.pathname);
    const opts = options || {};
    const sessions = opts.sessions || [];
    // **记住最近一次带 active 的渲染**：初始骨架渲染（DOMContentLoaded 里不传参）
    // 之后可能被首页的二次渲染覆盖成"无高亮"（Codex 审计 AF-18）。
    if (opts.activeSession !== undefined) lastActiveSession = opts.activeSession;
    const activeSession = opts.activeSession !== undefined
      ? opts.activeSession
      : lastActiveSession;

    const nav = NAV.map(function ([href, label, icon]) {
      const on = normalize(href) === current;
      return (
        '<a class="nav-item' + (on ? " on" : "") + '" href="' + href + '"' +
        ' style="text-decoration:none">' +
        '<span class="nav-icon">' + icon + "</span>" +
        '<span class="nav-label">' + KB.esc(label) + "</span></a>"
      );
    }).join("");

    const sessionHtml = sessions.length
      ? sessions
          .map(function (s) {
            const on = s.id === activeSession;
            return (
              '<div class="session-item' + (on ? " active" : "") + '" data-session="' + s.id + '">' +
              '<div class="session-item-title">' + KB.esc(s.title) + "</div>" +
              '<div class="session-item-meta">' + (s.count || 0) + " 条消息</div>" +
              // 删除会话：走 planner 受控操作（提案 → 确认 → 执行），
              // **没有**直连 DELETE；点击必须 stopPropagation，否则会先跳转到该会话。
              '<button class="session-del" data-session-del="' + s.id +
                '" title="删除这个会话" aria-label="删除会话">×</button>' +
              "</div>"
            );
          })
          .join("")
      : (opts.sessionError
        // 接口失败时**不能**说"还没有会话"：用户会以为会话被删光了
        ? '<div class="tiny muted" style="padding:10px 16px">会话列表加载失败：' +
          KB.esc(opts.sessionError) + "</div>"
        : '<div class="tiny muted" style="padding:10px 16px">还没有会话</div>');

    box.innerHTML =
      '<ul class="sidebar-menu">' + nav + "</ul>" +
      '<div class="session-block">' +
        '<div class="session-block-head">' +
          '<span class="session-block-title">会话 · ' + sessions.length + "</span>" +
          '<button class="btn ghost sm" id="new-session" title="新建会话">＋</button>' +
        "</div>" +
        '<div class="session-list" id="session-list">' + sessionHtml + "</div>" +
      "</div>" +
      '<div class="sidebar-foot">' +
        "<span>会话保存在服务端</span>" +
        '<a class="btn ghost sm" href="/app/docs/" title="使用说明" style="text-decoration:none">≡</a>' +
      "</div>";
  }

  /** 顶部徽标显示**真实**状态（来自 /planner/guide/ 的 environment）。 */
  async function renderEnvBadge() {
    const badge = document.getElementById("env-badge");
    if (!badge) return;
    const result = await KB.api("/api/v1/planner/guide/");
    if (!result.ok) {
      badge.textContent = "后端不可用";
      badge.title = (result.body && result.body.detail) || "";
      return;
    }
    const environment = (result.body && result.body.environment) || {};
    const assistant = (result.body && result.body.assistant) || {};
    const parts = [];
    // 只陈述已知事实，不暗示"在线"（本页不探测）。
    // 但要把"**已配置但未探测**"和"**根本没配置**"分开说 ——
    // 旧文案一律写"模型服务未检测"，用户会误读成"模型不可用"
    // （真实反馈：看到徽标后以为模型挂了）。
    if (environment.model_calls_disabled) {
      parts.push("模型调用已禁用");
    } else if (assistant.configured === true) {
      parts.push("模型已配置 · 未探测");
    } else if (assistant.configured === false) {
      parts.push("模型未配置");
    } else {
      parts.push("模型状态未知");
    }
    if (environment.load_test_mode) parts.push("负载测试");
    if (environment.dispatch_disabled) parts.push("派发已禁用");
    badge.textContent = parts.join(" · ");
    badge.title =
      "助手 configured=" + String(assistant.configured) +
      "；本页不主动探测（AS711 为共享环境），" +
      "如需实测请到「系统配置」点「测试连通」";
  }

  // 「清理与重置」入口已按要求**移除**（含它的 handler）。
  // 说明：该入口只是跳转说明，不是真实清理动作；清理一律走
  // 文件管理/信息库页的受控删除（三次独立确认 + 末次输入目标名称）。
  // 这里不再保留任何绑定，避免"模板去掉了、JS 还在找元素"的死代码。

  KB.nav = {
    renderSidebar: renderSidebar,
    renderEnvBadge: renderEnvBadge,
    renderSessions: renderSessions,
    NAV: NAV,
  };

  /** 当前 URL 上的会话 id（`?conversation=…`），没有则 null。 */
  function currentActiveSession() {
    const value = new URLSearchParams(location.search).get("conversation");
    return value ? Number(value) : null;
  }

  /**
   * 删除会话：**必须**走 planner 受控操作（提案 → 确认 → 执行 + 审计）。
   *
   * 为什么不直接 DELETE：会话关联着受控操作记录（提案/确认/执行的审计来源），
   * 后端在受控流程里会**保留**这些记录、只解除关联；绕过流程就绕过了这套保护与审计。
   */
  async function deleteConversation(id, button) {
    const item = button.closest("[data-session]");
    const titleNode = item && item.querySelector(".session-item-title");
    const metaNode = item && item.querySelector(".session-item-meta");
    const title = (titleNode && titleNode.textContent) || "未命名会话";
    const meta = (metaNode && metaNode.textContent) || "";

    const result = await KB.controlled({
      intent: "delete_conversation",
      params: { conversation: Number(id) },
      labels: { title: "删除会话" },
      extraLines: [
        "将删除会话：" + title + (meta ? "（" + meta + "）" : ""),
        "该会话关联的受控操作记录与凭证按审计要求保留（只解除关联，不销毁）。",
      ],
    });

    if (!result.ok) {
      if (result.skipped) {
        KB.toast("已取消，未做任何改动");
        return;
      }
      KB.toast((result.detail || "删除失败") + "（可重试）");
      return;
    }

    const body = result.result || {};
    KB.toast("已删除会话：" + title + (body.detached_operations
      ? "（保留 " + body.detached_operations + " 条操作审计记录）" : ""));

    // 删掉的正是当前打开的会话 → 回首页（否则会立刻又被拉回一个不存在的会话）；
    // 其它情况只刷新左侧列表。
    if (currentActiveSession() === Number(id)) {
      location.href = "/app/";
      return;
    }
    await renderSessions(currentActiveSession());
  }

  /** 会话列表来自**服务端**（`/planner/conversations/`），不是首页的本地数组。
   *
   * 原型把会话放在 localStorage 里，所以只有首页有数据；本系统每个页面
   * 都从同一个真实接口取，左侧栏在任何页面都能正确显示会话。
   */
  async function renderSessions(activeSession) {
    const result = await KB.api("/api/v1/planner/conversations/");
    const rows = (result.ok && result.body && result.body.results) || [];
    const sessions = rows.map(function (row) {
      return { id: row.id, title: row.title || "未命名会话", count: row.count || 0 };
    });
    // **按真实 active 高亮**：调用方（首页）传入当前会话 id；
    // 其它页面没有"当前会话"概念时不强行高亮一个，避免误导。
    const active = activeSession === undefined ? null : activeSession;
    renderSidebar({
      sessions: sessions,
      activeSession: active,
      sessionError: result.ok
        ? ""
        : ((result.body && result.body.detail) || "无法读取会话列表"),
    });

    const list = document.getElementById("session-list");
    if (list) {
      list.querySelectorAll("[data-session]").forEach(function (item) {
        item.addEventListener("click", async function () {
          // 离开当前会话前先问文档面板：有未保存改动就走确认（否则编辑被静默丢掉）
          if (!(await leaveOkay("切换会话"))) return;
          location.href = "/app/?conversation=" + item.getAttribute("data-session");
        });
      });
      // 删除按钮：**先阻止冒泡**，否则点击会触发上面的跳转（跳走后再弹确认框）
      list.querySelectorAll("[data-session-del]").forEach(function (button) {
        button.addEventListener("click", async function (event) {
          event.stopPropagation();
          event.preventDefault();
          if (button.disabled) return;   // 防重复点击（会走两次受控流程）
          // 删掉的如果是当前会话，页面会跳走 —— 未保存的编辑同样要先问
          if (!(await leaveOkay("删除会话"))) return;
          button.disabled = true;
          try {
            await deleteConversation(button.getAttribute("data-session-del"), button);
          } finally {
            button.disabled = false;
          }
        });
      });
    }
    const button = document.getElementById("new-session");
    if (button) {
      button.addEventListener("click", async function () {
        if (!(await leaveOkay("新建会话"))) return;
        // 清掉 ?conversation=…，否则会立刻又被拉回旧会话
        location.href = "/app/";
      });
    }
    return sessions;
  };

  /**
   * 离开前的统一守卫：首页的文档面板（kb-assistant.js）会注册
   * `KB.docDirtyGuard`；没有注册（非首页）时直接放行。
   * 返回 true = 可以继续。
   */
  async function leaveOkay(what) {
    if (window.KB && typeof KB.docDirtyGuard === "function") {
      try {
        return await KB.docDirtyGuard(what);
      } catch (err) {
        return true;   // 守卫自己出错不能把用户永久卡在页面上
      }
    }
    return true;
  }

  /** 当前是否有**未保存的文档编辑**（kb-assistant.js 注册；非首页恒 false）。 */
  function docDirty() {
    return Boolean(window.KB && typeof KB.docIsDirty === "function" && KB.docIsDirty());
  }

  /**
   * **站内导航拦截**（Codex 第三批 C：点左侧"文件管理"或品牌/说明链接直接离开首页，
   * 未保存的正文就没了）。只有"本窗口的普通左键导航"才拦：
   *   * 允许 Ctrl/Cmd/Shift/中键（新标签/新窗口）与 target=_blank、download；
   *   * 没有未保存内容时**完全不介入**（零打扰，原生行为不变）；
   *   * 确认通过后由我们发起导航，所以不会重复弹窗；取消则留在原页。
   * 挂成**捕获阶段**并在拦下时 stopPropagation：否则 data-nav 的原有处理器
   * 会立刻 location.href，抢在我们确认之前跳走。
   */
  document.addEventListener("click", function (event) {
    if (event.defaultPrevented || event.button !== 0) return;
    if (event.metaKey || event.ctrlKey || event.shiftKey || event.altKey) return;
    const target = event.target.closest ? event.target.closest("a[href], [data-nav]") : null;
    if (!target) return;
    if (target.getAttribute("target") === "_blank" || target.hasAttribute("download")) return;
    const href = target.getAttribute("href") || target.getAttribute("data-nav");
    if (!href) return;
    let url;
    try {
      url = new URL(href, location.href);
    } catch (err) {
      return;
    }
    if (url.origin !== location.origin) return;      // 外链不拦
    if (!docDirty()) return;                          // 没脏：交给原生导航
    event.preventDefault();
    event.stopPropagation();
    const label = (target.textContent || "").trim().slice(0, 20) || url.pathname;
    leaveOkay("打开「" + label + "」").then(function (okay) {
      if (okay) location.href = url.href;
    });
  }, true);

  // 「退出」是**表单 POST**（不是链接）：未保存内容同样要拦一次。
  // 用 form.submit() 提交不会再次触发 submit 事件，因此只会问一次。
  document.addEventListener("submit", function (event) {
    const form = event.target;
    if (!form || !form.getAttribute) return;
    const action = form.getAttribute("action") || "";
    if (action.indexOf("logout") === -1) return;
    if (!docDirty()) return;
    event.preventDefault();
    event.stopPropagation();
    leaveOkay("退出登录").then(function (okay) {
      if (okay) form.submit();
    });
  }, true);

  document.addEventListener("DOMContentLoaded", function () {
    renderSidebar();               // 先出骨架，避免左侧栏空白闪烁
    renderEnvBadge();
      renderSessions();              // 再用真实会话数据补上
  });
})();
