/* 原型页面装配 —— **结构逐段照搬原型最后生效的 view 函数**，
 * 真实数据只替换其中的"值"，不删卡、不改布局、不编造绿灯。
 *
 * 对应原型：
 *   viewAgent    (原型 2082–2122)：6 状态卡 + 5 指标卡 + 运维动作 + 事件时间线/检索反馈 两列
 *   viewSettings (原型 2125–2173)：远程模型服务表(服务/模型/端点/状态/延迟/测试) + 模型 + 基础设施
 *   viewKnowledge(原型 2176–2195)：4 统计卡 + 各信息库卡片(资料清单 + 去文件管理)
 *   viewTasks    (原型 2198–2219)：**已入库的会话来源资料**表
 *   viewDocs     (原型 2221–2253)：paper 卡片 + h3 分节
 *
 * 口径：没有对应真实能力的位置**保留卡位**并显示"未配置 / 暂无数据"，
 * 不允许删卡或伪造成功。
 */
(function () {
  "use strict";

  const KB = window.KB;
  const esc = KB.esc;
  const mono = KB.mono;

  const STATUS_TEXT = {
    pending: "待处理", processing: "处理中", completed: "已完成",
    failed: "失败", archived: "已归档", done: "已完成", running: "处理中",
    executed: "已执行", expired: "已过期", rejected: "已放弃", proposed: "待确认",
    armed: "待执行", executing: "执行中",
  };

  /** 资料状态 → 原型 status-dot 的 class。 */
  function dotClass(status) {
    if (status === "done" || status === "completed") return "completed";
    if (status === "running" || status === "processing") return "processing";
    if (status === "failed") return "failed";
    if (status === "archived") return "archived";
    return "pending";
  }
  function statusText(status) {
    return STATUS_TEXT[status] || status || "—";
  }

  function shell(title, sub, body) {
    return (
      '<div class="content">' +
      '<header class="page-header"><div><h1>' + esc(title) + "</h1><p>" + esc(sub) + "</p></div></header>" +
      body +
      "</div>"
    );
  }

  function tableRegion(label, table) {
    return '<p class="table-scroll-hint" aria-hidden="true">可横向滚动查看全部列</p>' +
      '<div class="table-region" role="region" tabindex="0" aria-label="' +
      esc(label + "，可横向滚动") + '">' + table + "</div>";
  }

  /** 原型 .stat-card（指标卡）。 */
  function statCard(value, label) {
    return (
      '<div class="stat-card"><div class="stat-value">' + mono(value) +
      '</div><div class="stat-label">' + esc(label) + "</div></div>"
    );
  }

  /** 原型的状态卡：卡位 + 状态点；**没有能力就显示"未配置"灰点**，不给绿灯。 */
  function stateCard(name, state, note) {
    const dot = state === true ? "completed" : state === false ? "failed" : "pending";
    const text = state === true ? "运行正常" : state === false ? "已触发保护" : "未配置";
    return (
      '<div class="stat-card"><div class="card-title">' +
      '<span class="status-dot ' + dot + '"></span>' + esc(name) +
      '</div><div class="hint">' + esc(note || text) + "</div></div>"
    );
  }

  // ============================================================= Agent ====

// 原型 AGENT_ACTIONS 的五个动作，逐一映射到真实能力；没实现的如实说明。
const AGENT_ACTIONS = [
  ["scan", "立即巡检"],
  ["restart_stuck", "重启停滞任务"],
  ["clear_circuit", "重置熔断器"],
  ["learn", "查看学习规则"],
  ["rescan_annotations", "重扫未处理反馈"],
];

//: 有真实意图可映射的动作；其余**如实说明暂不可执行**，不伪造成功
const ACTION_INTENT = {
  // 「立即巡检」原来错配到 list_audit（点名巡检却给审计列表），现在接真实巡检
  scan: "run_patrol",
  restart_stuck: "restart_stalled_task",
  clear_circuit: "reset_breaker",
  // 「重扫未处理反馈」此前未映射；reparse_failed 已是真实实现（净负分资料重跑流水线）
  rescan_annotations: "reparse_failed",
  // 「触发一次学习」：本系统**不做训练**（反馈加权在检索时实时生效），
  // 所以这里接的是**只读的规则快照**，按钮文字也随之改成实话，
  // 而不是保留一个"点了没反应/假成功"的按钮。
  learn: "feedback_learning_report",
};

/** 安全渲染 **加粗**：先转义再替换，绝不直接插 HTML。 */
function renderInline(text) {
  return esc(String(text || "")).replace(/\*\*(.+?)\*\*/g, "<b>$1</b>");
}

function showAgentResult(title, result) {
  result = result || {};
  const warning = result.ok === false || result.failed > 0 || result.incomplete > 0 ||
    (Object.prototype.hasOwnProperty.call(result, "global_reset") && !result.global_reset);
  const rows = result.items || result.reset || [];
  const table = rows.length ? '<table class="table"><thead><tr><th>资料 / 服务</th><th>结果</th>' +
    '<th>说明</th></tr></thead><tbody>' + rows.map(function (row) {
      return '<tr><td>' + esc(row.name || row.service || "—") + '</td><td>' +
        esc(row.status || (row.reset === true ? "已重置" : row.reset === false ? "未重置" : "—")) +
        '</td><td>' + esc(row.detail || row.error || "") + '</td></tr>';
    }).join("") + '</tbody></table>' : '';
  KB.modal('<div class="modal-head"><b>' + esc(title) + ' · 结果</b></div>' +
    '<div class="modal-body paper"><p class="notice-bar' + (warning ? ' danger' : '') + '">' +
    esc(result.detail || result.note || "操作已完成") + '</p>' +
    (result.note && result.detail ? '<p>' + esc(result.note) + '</p>' : '') + table + '</div>' +
    '<div class="modal-foot"><button class="btn sm" data-close>关闭</button></div>');
}

async function loadAgent() {
  const main = document.getElementById("main");
  function readError(response, label) {
    return response.status === 403
      ? "无权限读取" + label + "（只有系统管理员可查看）"
      : KB.errorText(response, "读取" + label + "失败，请刷新重试");
  }
  const [operations, stats, rtMetrics, feedbackList, indexStats, auditList] = await Promise.all([
    KB.api("/api/v1/planner/operations/"),
    KB.api("/api/v1/knowledge/stats/"),
    // **真实运行指标**（仅系统管理员可见；403 时如实降级为"暂无状态"，不编造）
    KB.api("/api/v1/knowledge/runtime-metrics/"),
    // **我自己的检索反馈**（端点一直存在，此前表格却是写死的空态 → 假占位）
    KB.api("/api/v1/knowledge/search-feedback/"),
    // 索引自证（容器里真实点数 vs 登记行）与审计事件：此前**后端有、页面零入口**，
    // 于是"代次切换 / 索引状态 / 审计核对"只有手写 HTTP 才能用。
    KB.api("/api/v1/knowledge/index-stats/"),
    KB.api("/api/v1/audit/events/?page_size=8"),
  ]);
  const ops = (operations.ok && operations.body && operations.body.results) || [];
  const counts = (stats.ok && stats.body) || {};
  // **运行指标 403 必须与"确实没数据"分开说**（处置清单 OP-09）：普通账号读到
  // 403 时旧实现把 rm 当成 null，卡片就渲染成"还没跑过巡检/调度器停了"，
  // 再按 patrolHint 引导用户去点注定 403 的运维动作 —— 把"无权限"说成"没数据"。
  const runtimeDenied = rtMetrics.status === 403;
  const rm = (rtMetrics.ok && rtMetrics.body) || null;
  const breakerScope = (rm && rm.breakers && rm.breakers.scope) || "";
  const breakers = (rm && rm.breakers &&
    (breakerScope === "global" ? rm.breakers.shared : rm.breakers.items)) || [];
  const queue = (rm && rm.broker_queue) || null;
  // 检索反馈：后端只返回**我自己**的行（按 user 过滤），
  // 净分与「重扫未处理反馈」同一口径（净负分才会被覆盖重扫）。
  const feedback = (feedbackList.ok && feedbackList.body && feedbackList.body.results) || [];

  // ---- 索引自证 / 审计（系统管理员） ----
  // 403 与"确实没有数据"必须分开说：普通账号看到"0 个集合"会以为索引丢了。
  const indexDenied = indexStats.status === 403;
  const indexBody = (indexStats.ok && indexStats.body) || {};
  const collections = indexBody.collections || [];
  const fileCounts = indexBody.files || null;
  const auditDenied = auditList.status === 403;
  // 后端审计列表走 DRF 全局分页（**忽略 page_size**，实际返回 20 条）；
  // 卡片上只展示最新 8 条，并把"这是最新 8 条、不是全部"写清楚 ——
  // 不能一边请求 `?page_size=8` 一边显示 20 条（Codex 审计 OP-17）。
  const auditEvents = ((auditList.ok && auditList.body && auditList.body.results) || []).slice(0, 8);
  const auditTotal = (auditList.ok && auditList.body && auditList.body.count) || null;

  // 每个信息库一行「回退到上一代」：走 planner 受控意图（服务端自己解析上一代，
  // 不让前端猜 generation，避免指针与事实分叉）。
  const libraryCodes = [];
  collections.forEach(function (row) {
    if (row.library && libraryCodes.indexOf(row.library) < 0) libraryCodes.push(row.library);
  });
  const generationRows = collections.map(function (row) {
    const exists = row.exists_in_qdrant ? "存在" : "缺失";
    // 退役待删：既不是生效代次，也**不能**被设成生效（后端会拒绝）。如实标出来，
    // 并提示"下次重建/清理会自动重试删除"，而不是把它伪装成普通历史代次。
    const action = !row.library
      ? '<span class="tiny muted">无关联信息库，不可切换</span>'
      : !row.exists_in_qdrant
      ? '<span class="tiny muted">集合缺失，不可切换</span>'
      : row.retiring
      ? '<span class="tiny muted">待删除（下次重建/清理自动重试）</span>'
      : row.build_status && row.build_status !== "ready"
      ? '<span class="tiny muted">构建尚未就绪，不可切换</span>'
      : row.is_active
      ? '<span class="tiny muted">当前生效</span>'
      : '<button class="btn sm" data-gen-activate="' + esc(row.name) + '" data-gen-library="' +
        esc(row.library) + '" data-gen-points="' + esc(String(row.points)) +
        '">设为生效</button>';
    return "<tr><td class='mono'>" + esc(row.name) + "</td><td>" + esc(row.library || "—") +
      "</td><td>" + esc(row.dimension ? String(row.dimension) : "—") + "</td>" +
      "<td>" + esc(exists) + (row.exists_in_qdrant ? " · " + esc(String(row.points)) + " 点" : "") +
      "</td><td>" + (row.retiring ? "<span class='tag'>待删除</span>"
        : row.is_active ? "<span class='tag'>生效中</span>" : "历史代次") +
      "</td><td>" + action + "</td></tr>";
  }).join("");
  const indexCard = indexDenied
    ? '<div class="card"><div class="card-title">索引与代次</div>' +
      '<div class="hint">只有系统管理员能查看索引状态（当前账号无权限）。</div></div>'
    : !indexStats.ok
    ? '<div class="card"><div class="card-title">索引与代次</div><div class="hint">' +
      esc(readError(indexStats, "索引状态")) + '</div></div>'
    : '<div class="card"><div class="card-title">索引与代次（切换/回退走受控操作）</div>' +
      (fileCounts
        ? '<div class="hint">资料 ' + esc(String(fileCounts.total || 0)) + " 份（已解析 " +
          esc(String(fileCounts.parsed || 0)) + " · 失败 " + esc(String(fileCounts.failed || 0)) +
          "）· 分段 " + esc(String(fileCounts.chunks || 0)) + " 条</div>"
        : "") +
      (generationRows
        ? tableRegion("索引与代次", '<table class="table"><thead><tr><th>集合</th><th>库</th><th>维度</th>' +
          "<th>Qdrant</th><th>状态</th><th>操作</th></tr></thead><tbody>" + generationRows +
          "</tbody></table>")
        : '<div class="hint">还没有登记任何向量集合。</div>') +
      (libraryCodes.length
        ? '<div class="row" style="margin-top:8px">' +
          libraryCodes.map(function (code) {
            // 与后端默认回退规则一致：最近登记的非当前、非退役代次。
            const target = collections.filter(function (row) {
              return row.library === code && !row.is_active && !row.retiring;
            }).slice(-1)[0];
            const eligible = target && target.exists_in_qdrant &&
              (!target.build_status || target.build_status === "ready");
            return '<button class="btn sm" data-gen-rollback="' + esc(code) +
              '"' + (eligible ? '' : ' disabled title="没有可用的历史代次"') + '>「' +
              esc(code) + "」回退到上一代</button>";
          }).join("") + "</div>"
        : "") +
      "</div>";
  const auditCard = '<div class="card"><div class="card-title">审计（只读）</div>' +
    (auditDenied
      ? '<div class="hint">只有系统管理员能查看审计事件。</div>'
      : (auditEvents.length
          ? '<div class="hint">按时间倒序显示最新 ' + auditEvents.length + " 条" +
            (auditTotal ? "（共 " + auditTotal + " 条）" : "") + "。</div>" +
            '<div class="timeline">' + auditEvents.map(function (item) {
              return '<div class="ev"><div class="kvs"><span class="mono">' +
                esc(KB.fmtTime(item.created_at)) + '</span><span class="tag">' +
                esc(item.action) + "</span></div><div>" +
                esc(item.resource_kind + " · " + (item.resource_key || "—")) + "</div></div>";
            }).join("") + "</div>"
          : '<div class="hint">' + (auditList.ok ? "还没有审计事件。" :
            esc(readError(auditList, "审计事件"))) + '</div>') +
      '<div class="row" style="margin-top:8px">' +
      '<button class="btn sm" id="audit-verify">校验审计链</button></div>' +
      '<div class="hint" id="audit-verify-note">校验会重算整条哈希链，证明记录没有被改过。</div>') +
    "</div>";

  // ---- "暂无状态"曾经的三张卡，现在都有**真实判定来源** ----
  // 看门狗 / 结果校验来自**巡检留痕**（knowledge.patrol 每次运行写一行）；
  // 双阶段补扫是**实时查询**（一阶段=待清扫+停滞，二阶段=向量未完成）。
  // 后端给不出读数（还没跑过巡检）就保持灰点，绝不编造。
  const watchdog = (rm && rm.watchdog) || null;
  const verification = (rm && rm.verification) || null;
  const resweep = (rm && rm.resweep) || null;
  const stalledTasks = rm && typeof rm.stalled_tasks === "number" ? rm.stalled_tasks : null;
  const feedbackPending =
    rm && typeof rm.feedback_pending === "number" ? rm.feedback_pending : null;
  // 重启次数：本用户**已执行**的「重启停滞任务」条数（后端从审计记录里数出来的）
  const restarts = operations.ok && operations.body && operations.body.summary &&
    typeof operations.body.summary.restart_stalled_executed === "number"
    ? operations.body.summary.restart_stalled_executed
    : null;

  /** 距今多久：只说"多久之前"，不推断下一次。 */
  function ageText(seconds) {
    if (typeof seconds !== "number") return "";
    if (seconds < 90) return Math.round(seconds) + " 秒前";
    if (seconds < 5400) return Math.round(seconds / 60) + " 分钟前";
    return Math.round(seconds / 3600) + " 小时前";
  }

  //: 巡检的四项发现，界面用的中文名（与后端 counts 的键一一对应）
  const FINDING_LABELS = {
    failed_exhausted: "重试耗尽",
    stalled: "停滞",
    vector_incomplete: "向量不完整",
    blob_missing: "原件缺失",
  };

  /** 分项计数只说非零项，避免一屏都是 0。 */
  function issuesText(counts) {
    const parts = [];
    Object.keys(FINDING_LABELS).forEach(function (key) {
      const value = Number((counts || {})[key]) || 0;
      if (value) parts.push(FINDING_LABELS[key] + " " + value);
    });
    return parts.join(" · ");
  }

  const patrolHint = !rtMetrics.ok ? readError(rtMetrics, "运行指标")
    : "还没跑过巡检（可点右侧「立即巡检」）";
  // 没有任何心跳时**不能**显示成"正常"：overdue 是 null（无法判断），界面照实说。
  const patrolUnknown = "心跳未知：还没有任何巡检留痕，无法判断调度器是否在跑（这不等于正常）";
  const watchdogCard = watchdog && watchdog.available
    ? ["解析看门狗", watchdog.overdue === true ? false : watchdog.overdue === false ? watchdog.issues === 0 : null,
       (watchdog.verdict || "") + " · 最近巡检 " + ageText(watchdog.age_seconds) + "（" +
       (watchdog.source === "manual" ? "手动" : "周期") + "）· 本批检查 " + watchdog.checked +
       " 份 · " + (watchdog.issues === 0 ? "未发现异常" : "发现：" + issuesText(watchdog.counts))]
    : ["解析看门狗", null, patrolHint];
  const verificationCard = verification && verification.available
    ? ["结果校验", verification.ok,
       "本批检查 " + verification.checked + " 份（按批轮转）· " +
       (verification.ok ? "无不一致" : "发现：" + issuesText(verification.detail))]
    : ["结果校验", null, patrolHint];
  const resweepCard = resweep && typeof resweep.total === "number"
    ? ["双阶段补扫", resweep.total === 0,
       "待补扫 一阶段 " + (resweep.phase1.total || 0) + " 份（待清扫 " +
       (resweep.phase1.pending || 0) + " + 停滞 " + (resweep.phase1.stalled || 0) +
       "）· 二阶段 " + (resweep.phase2.total || 0) + " 份"]
    : ["双阶段补扫", null, !rtMetrics.ok ? patrolHint : "暂无状态"];

  // ---- 6 张状态卡：**标签与顺序严格照原型** ----
  // 缺指标一律 null → 灰色"暂无状态"；不拿别的能力顶替，也不给假绿灯。
  const backlog = stats.ok ? (counts.pending || 0) + (counts.processing || 0) : null;
  // API 熔断器：有真实快照就照实显示（含**进程内**口径）；没有服务进过熔断器就"暂无状态"
  const breakerOk = breakers.length
    ? breakers.every(function (b) { return b.state === "closed"; })
    : null;
  const breakerText = runtimeDenied
    ? "无权限读取运行指标（只有系统管理员能看到熔断状态）"
    : (breakers.length
        ? breakers.map(function (b) {
            return b.service + "：" + b.state + "（连续失败 " + b.consecutive_failures +
              "/" + b.failure_threshold + "）";
          }).join(" · ") + (breakerScope === "process" ? " · 口径：进程内" : "")
        : (!rtMetrics.ok ? readError(rtMetrics, "运行指标") : "暂无状态"));
  // 资源调控：队列深度是**实测**值（broker 指向非业务库才探测）
  let queueOk = queue && typeof queue.depth === "number" ? true : null;
  // 跨进程模型并发闸门：额度在 Redis 里共享，与上面"进程内"的熔断器**不是同一口径**，
  // 所以这里把 scope 的来源写清楚（Redis 不可用会如实说已降级放行多少次）。
  const gate = (rm && rm.model_concurrency) || null;
  let gateText = "";
  if (gate && gate.enabled !== false) {
    if (gate.available === false) {
      queueOk = false;
      const fellBack = (gate.fallback && gate.fallback.total) || 0;
      gateText = " · 并发闸门：Redis 不可用，已降级放行 " + fellBack + " 次";
    } else {
      const inFlight = Object.keys(gate.in_flight || {}).map(function (name) {
        const n = gate.in_flight[name];
        return name + " " + (n === null || n === undefined ? "?" : n);
      }).join(" / ");
      gateText = " · 并发闸门 " + gate.limit_per_service + "/服务（跨进程共享，在飞 " + inFlight + "）";
    }
  } else if (gate) {
    gateText = " · 并发闸门：已关闭";
  }
  const queueText = queue && typeof queue.depth === "number"
    ? "队列深度 " + queue.depth + "（DB" + queue.url_db + "）" + gateText
    : ((queue && queue.note) || (!rtMetrics.ok ? readError(rtMetrics, "运行指标") : "暂无状态")) + gateText;
  const dogs = [
    watchdogCard,
    // 索引队列：唯一有真实数据的一项（待处理 + 处理中）
    ["索引队列", backlog === 0 ? true : null,
      backlog === null ? readError(stats, "资料处理统计") : backlog === 0 ? "当前可见资料无待处理任务" : "待处理 " + (counts.pending || 0) + " 份 · 处理中 " +
        (counts.processing || 0) + " 份"],
    ["资源调控", queueOk, queueText],
    ["API熔断器", breakerOk, breakerText],
    resweepCard,
    verificationCard,
  ];

  // ---- 5 张指标卡：**标签与顺序严格照原型**，只有 stat-value + stat-label ----
  const breakerFailures = breakers.reduce(function (n, b) {
    return n + (Number(b.consecutive_failures) || 0);
  }, 0);
  const metrics = [
    // 三项曾经是"—"占位，现在是真实读数：停滞任务/待处理反馈来自实时查询，
    // 重启次数来自审计记录；后端给不出时仍然是"—"，不填假数。
    ["停滞任务", stalledTasks === null ? "—" : String(stalledTasks)],
    ["重启操作次数", restarts === null ? "—" : String(restarts)],
    ["熔断失败数", breakers.length ? String(breakerFailures) : "—"],
    // 口径写清楚（Codex 审计 OP-14）：这里的成功率来自**我自己的操作记录**
    // （/planner/operations/ 按 request.user 过滤），不是全站任务成功率；
    // 旧标签会让人误以为是系统整体指标。
    ["我的操作成功率", operations.ok && operations.body.summary && operations.body.summary.settled
      ? Math.round(operations.body.summary.executed / operations.body.summary.settled * 100) + "%" : "—"],
    ["待处理反馈", feedbackPending === null ? "—" : String(feedbackPending)],
  ];

  const timeline = ops.length
    ? ops.slice(0, 8).map(function (op) {
        return (
          '<div class="ev"><div class="kvs"><span class="mono">' + esc(KB.fmtTime(op.created_at)) +
          '</span><span class="tag">' + esc(op.label || op.intent) + "</span></div>" +
          "<div>" + esc(op.target_label || "") + " · " + esc(statusText(op.status)) + "</div></div>"
        );
      }).join("")
    : '<div class="hint">' + (operations.ok ? "还没有操作事件" :
      esc(readError(operations, "操作事件"))) + '</div>';

  // 反馈表：**表头恒定 5 列**（空态也保留，只换 tbody）。
  // "处理结果"只说净分口径下会发生什么，**不假装已经重扫过**；
  // 第 5 列是原型那列「标记已处理」= **分诊留痕**（不改权重、不改重扫口径）。
  const feedbackBody = feedback.length
    ? "<tbody>" + feedback.map(function (item) {
        let outcome = "—";
        if (typeof item.net === "number") {
          outcome = item.net < 0
            ? "净负分 " + item.net + "：会被「重扫未处理反馈」覆盖"
            : "净分 " + item.net + "：无需重扫";
        }
        const triage = item.handled
          ? "<span class='tag'>已处理</span><div class='tiny mono'>" +
            esc(KB.fmtTime(item.handled_at)) + "</div>"
          : "<button class='btn sm' data-anno-done='" + item.id + "'>标记已处理</button>";
        return (
          "<tr><td><b>" + esc(item.file_name || "整次检索（未指定资料）") + "</b>" +
            "<div class='tiny mono'>" + esc(KB.fmtTime(item.created_at)) + "</div></td>" +
          "<td class='tiny' title='检索「" + esc(item.query || "") + "」'>" +
            esc(item.note || ("检索「" + (item.query || "") + "」")) + "</td>" +
          "<td><span class='tag'>" + esc(item.verdict_label || item.verdict) + "</span></td>" +
          "<td class='tiny'>" + esc(outcome) + "</td><td>" + triage + "</td></tr>"
        );
      }).join("") + "</tbody>"
    : '<tbody><tr><td colspan="5"><div class="empty-state" ' +
      'style="text-align:center;padding:20px 0"><p class="muted">' +
      (feedbackList.ok ? "还没有检索反馈：在「搜索」里对结果点「准确 / 不准确」就会记在这里。" :
        esc(readError(feedbackList, "检索反馈"))) +
      "</p></div></td></tr></tbody>";

  main.innerHTML = shell(
    "Agent 控制台",
    "系统运行状态、最近事件与运维动作。",
    '<div class="row" style="margin-bottom:10px"><button class="btn sm" id="agent-refresh">刷新状态</button></div>' +
    '<div class="grid-3">' +
      dogs.map(function (item) { return stateCard(item[0], item[1], item[2]); }).join("") +
    "</div>" +
    '<div class="grid-3" style="margin-top:14px">' +
      metrics.map(function (item) {
        // 指标卡只有 stat-value + stat-label（原型如此，加 hint 会改变卡高）
        return '<div class="stat-card"><div class="stat-value">' + esc(String(item[1])) +
          '</div><div class="stat-label">' + esc(item[0]) + "</div></div>";
      }).join("") +
    "</div>" +
    '<div class="card" style="margin-top:14px">' +
      '<div class="card-title">运维动作</div>' +
      '<div class="row">' +
        AGENT_ACTIONS.map(function (item) {
          return '<button class="btn sm" data-agent-action="' + item[0] + '">' + esc(item[1]) +
            "</button>";
        }).join("") +
      "</div>" +
      // 原型的「最近心跳」在这里显示：本系统的"心跳"就是**最近一次巡检留痕**
      //（knowledge.patrol 由 beat 周期跑，worker 卡死时它会变旧 → 这就是活性信号）。
      // watchdog.overdue 是后端的判决：心跳超过阈值说明 beat/scheduler 可能已经停了，
      // 这时必须在页面上报警，不能只把数字变小了事。
      '<div class="hint" style="margin-top:8px">最近操作：' + ops.length + " 条" +
        (watchdog && watchdog.available
          ? " · 看门狗：" + esc(watchdog.verdict || "周期心跳未知")
          : " · 看门狗心跳：" + patrolHint) + "</div>" +
      (watchdog && watchdog.overdue
        ? '<div class="notice-bar danger" style="margin-top:8px">调度器心跳过期：' +
          esc(watchdog.verdict || "") +
          "（请检查 knowledgebase-scheduler 容器是否还在跑；巡检停了就不会再发现停滞/缺失资料）</div>"
        : "") +
      (watchdog && !watchdog.available
        ? '<div class="notice-bar" style="margin-top:8px">' + esc(patrolUnknown) + "</div>"
        : "") +
    "</div>" +
    '<div class="grid-2">' +
      '<div class="card"><div class="card-title">事件时间线</div><div class="timeline">' +
        timeline + "</div></div>" +
      '<div class="card"><div class="card-title">检索反馈（批注）</div>' +
        tableRegion("检索反馈", '<table class="table"><thead><tr><th>资料</th><th>问题</th><th>状态</th>' +
        "<th>处理结果</th><th>分诊</th></tr></thead>" +
        feedbackBody + "</table>") +
      "</div>" +
    "</div>" +
    '<div class="grid-2" style="margin-top:14px">' + indexCard + auditCard + "</div>"
  );

  document.getElementById("agent-refresh").addEventListener("click", loadAgent);

  // 代次切换/回退：一律走**受控操作**（提案→确认→执行），绝不直连 PUT
  main.querySelectorAll("[data-gen-activate]").forEach(function (button) {
    button.addEventListener("click", async function () {
      const name = button.getAttribute("data-gen-activate");
      const library = button.getAttribute("data-gen-library");
      const outcome = await KB.controlled({
        intent: "activate_generation",
        params: { library: library, collection: name },
        labels: { title: "切换向量代次" },
      });
      if (outcome.skipped) { KB.toast("已取消，索引仍然指向原来的代次"); return; }
      if (!outcome.ok) { KB.toast(outcome.detail); return; }
      KB.toast("已切换到 " + name + "（库指针与每个资料的索引指向在同一事务里改）");
      await loadAgent();
    });
  });
  main.querySelectorAll("[data-gen-rollback]").forEach(function (button) {
    button.addEventListener("click", async function () {
      const code = button.getAttribute("data-gen-rollback");
      const outcome = await KB.controlled({
        intent: "rollback_generation",
        params: { library: code },
        labels: { title: "回退向量代次" },
      });
      if (outcome.skipped) { KB.toast("已取消，没有回退"); return; }
      if (!outcome.ok) { KB.toast(outcome.detail); return; }
      const summary = outcome.result && outcome.result.detail
        ? outcome.result.detail
        : JSON.stringify(outcome.result || {}).slice(0, 140);
      KB.toast("已回退：" + summary);
      await loadAgent();
    });
  });
  // 反馈分诊（原型那列「标记已处理」）：**只留痕**，不改检索加权、
  // 不改「待处理反馈 / 重扫未处理反馈」的口径（后端也只会写 handled_at/by）。
  main.querySelectorAll("[data-anno-done]").forEach(function (button) {
    button.addEventListener("click", async function () {
      const id = button.getAttribute("data-anno-done");
      button.disabled = true;
      const result = await KB.api(
        "/api/v1/knowledge/search-feedback/" + id + "/handle/", { method: "POST" }
      );
      if (!result.ok) {
        button.disabled = false;
        const detail = (result.body && (result.body.detail || result.body.detail)) ||
          ("HTTP " + result.status);
        KB.toast("标记失败：" + detail);
        return;
      }
      KB.toast(result.body.already_handled
        ? "这条反馈此前已经标记过（保留第一次留痕）"
        : "已标记为已处理（只留痕，不改变检索加权）");
      await loadAgent();
    });
  });
  const verifyButton = document.getElementById("audit-verify");
  if (verifyButton) {
    verifyButton.addEventListener("click", async function () {
      const note = document.getElementById("audit-verify-note");
      if (note) KB.notice(note, "校验中…", "");
      const result = await KB.api("/api/v1/audit/verify/");
      if (!result.ok) {
        if (note) KB.notice(note, (result.body && result.body.detail) || "校验失败", "err");
        return;
      }
      const body = result.body || {};
      // 只说后端给的事实：ok 与 checked；不做超出证据的推断。
      const text = body.ok
        ? "校验通过：已重算 " + (body.checked || 0) + " 条记录的哈希链，未发现被改动。"
        : "校验未通过：已检查 " + (body.checked || 0) + " 条记录，发现 " +
          (body.mismatch_count || 1) + " 条不匹配，首条编号 " + (body.broken_at || "?") +
          "。原始记录与摘要均已保留，请核对历史写入来源。";
      if (note) KB.notice(note, text, body.ok ? "ok" : "err");
    });
  }

  // 运维动作：能做的走真实"提案→确认→执行"；不能做的如实说明，不显示成功
  main.querySelectorAll("[data-agent-action]").forEach(function (button) {
    button.addEventListener("click", async function () {
      const key = button.getAttribute("data-agent-action");
      if (button.disabled) return;
      const intent = ACTION_INTENT[key];
      if (!intent) {
        KB.modal(
          '<div class="modal-head"><b>' + esc(button.textContent) + "</b></div>" +
          '<div class="modal-body paper"><p class="notice-bar">这个动作暂时还不能执行。</p>' +
          "<p>为避免误导，这里不会显示成功。</p></div>" +
          '<div class="modal-foot"><button class="btn sm" data-close>关闭</button></div>'
        );
        return;
      }
      button.disabled = true;
      let outcome;
      try {
        outcome = await KB.controlled({
        intent: intent, params: {}, labels: { title: button.textContent },
        });
      } finally {
        button.disabled = false;
      }
      if (outcome.skipped) {
        KB.toast("已取消，未做任何改动");
        return;
      }
      if (!outcome.ok) {
        KB.toast(outcome.detail);
        return;
      }
      await loadAgent();
      showAgentResult(button.textContent, outcome.result);
    });
  });
  // 非管理员：把管理员专属控件置灰并写明原因（后端仍会独立判定权限）
  gateAdminControls(main);
}

// ========================================================== Settings ====

async function loadSettings() {
  const main = document.getElementById("main");
  const [models, guide] = await Promise.all([
    KB.api("/api/v1/knowledge/models/"),
    KB.api("/api/v1/planner/guide/"),
  ]);
  const environment = (guide.ok && guide.body && guide.body.environment) || {};
  const assistant = (guide.ok && guide.body && guide.body.assistant) || {};
  const llm = environment.llm || {};
  const profiles = (models.ok && models.body && models.body.profiles) || [];
  const rerank = (models.ok && models.body && models.body.rerank) || {};
  const ocr = (models.ok && models.body && models.body.ocr) || {};

  // 本页两个接口都只对系统管理员开放：403 与"确实没配置"必须区分开，
  // 否则普通账号看到一整页"未配置"会以为模型没配好。
  const denied = [
    { name: "模型服务配置", result: models },
    { name: "运行配置", result: guide },
  ].filter(function (item) { return item.result.status === 403; });

  // **恰好 4 行**：当前生效的向量 profile + 重排 + OCR + 大模型。
  // 不再把每个 profile 都铺成一行（原型只有 4 类服务）。
  const active = profiles.filter(function (item) { return item.active; })[0] || profiles[0] || {};
  // `target` 是**探测目标的白名单键**（后端只认 embedding/rerank/ocr/llm）。
  // 必须显式挂在行上：早先靠"显示名 → 目标"的映射表，而表里写的是
  // 「向量模型/重排模型/OCR 引擎」，行的真实名字却是「向量服务/重排服务/OCR 服务」，
  // 三个名字都匹配不上 → 全部落到 `|| "embedding"` 兜底 →
  // **四个"测试连通"按钮都只探 BGE**（用户实测反馈）。
  const rows = [
    {
      name: "向量服务",
      target: "embedding",
      model: active.model || "未配置",
      endpoint: active.endpoint || "未配置",
      configured: !!active.endpoint,
      extra: active.dimension ? "维度 " + active.dimension : "",
    },
    {
      name: "重排服务",
      target: "rerank",
      model: rerank.model || "未配置",
      endpoint: rerank.endpoint || "未配置",
      configured: !!rerank.endpoint,
      extra: "",
    },
    {
      name: "OCR 服务",
      target: "ocr",
      model: ocr.model || "未配置",
      endpoint: ocr.endpoint || "未配置",
      configured: !!ocr.endpoint,
      extra: "",
    },
    {
      // LLM 的 model/base_url 来自 guide 的**只读配置**（不探测）
      name: "大模型服务",
      target: "llm",
      model: llm.model || assistant.model || "未配置",
      endpoint: llm.base_url || "未配置",
      configured: llm.configured === true || assistant.configured === true,
      extra: "",
    },
  ];

  const body = rows.map(function (server) {
    return (
      "<tr><td>" + esc(server.name) +
      (server.extra ? ' <span class="tag">' + esc(server.extra) + "</span>" : "") + "</td>" +
      "<td class='mono tiny'>" + esc(server.model) + "</td>" +
      "<td class='mono tiny'>" + esc(server.endpoint) + "</td>" +
      // 只有"配置与否"是已知事实；是否在线必须检测才知道 → 未检测
      "<td><span class='tag " + (server.configured ? "info" : "warn") + "'>" +
        (server.configured ? "已配置" : "未配置") + "</span></td>" +
      // 延迟单元格带上模型名，探测结果按模型回填（不编造、未测就是"未检测"）
      "<td class='mono tiny' data-latency-row='" + esc(server.name) + "'>未检测</td>" +
      "<td><div class='table-actions'><button data-test-model='" + esc(server.name) +
        "' data-test-target='" + esc(server.target) + "'>测试连通</button></div></td></tr>"
    );
  }).join("");

  // **嵌入 profile 明细（当前 1024 / 预留 2048）**：多模型并存时，"当前跑哪一个、
  // 预留的那一个到底配好没有"必须是**看得见**的事实，而不是靠人记。
  //
  // 三条口径（都来自后端 `configured` / `active` / 实测 `probe`，前端不编造）：
  //   * ``未配置`` → 明确显示"未配置（未部署）"，**不给探测按钮**：
  //     未配置的 profile 探了必然失败，把它渲染成"未就绪"会让人以为
  //     "部署了但没起来"，那是两种完全不同的运维事实（尤其是预留的 2048）；
  //   * ``已配置`` → 给"预检"按钮，**只有点了才发起真实调用**（默认不主动探测，
  //     AS711 是共享环境）；
  //   * 预检结果如实显示实测延迟与**实测维度**（`actual_dimension`），
  //     这才是"这套模型真的能返回 N 维"的证据；失败就显示"未就绪"+原因。
  const profileRows = profiles.length
    ? profiles.map(function (item) {
        const configured = item.configured === true;
        const statusTag = item.active
          ? "<span class='tag info'>当前生效</span>"
          : (configured
              ? "<span class='tag'>已配置（预留）</span>"
              : "<span class='tag warn'>未配置（未部署）</span>");
        const action = configured
          ? "<button data-probe-profile='" + esc(item.name) + "'>预检</button>"
          : "<button disabled title='该 profile 未配置 model/端点，无法预检；" +
            "未配置不等于未就绪，也不代表可以启用'>预检</button>";
        return (
          "<tr><td class='mono tiny'>" + esc(item.name) + "</td>" +
          "<td class='mono tiny'>" + esc(item.model || "未配置") + "</td>" +
          "<td class='mono tiny'>" + esc(String(item.dimension || "")) + "</td>" +
          "<td class='mono tiny'>" + esc(item.endpoint || "未配置") + "</td>" +
          "<td>" + statusTag + "</td>" +
          // 未检测就是"未检测"：默认不探测，也不写一个假的"正常"
          "<td class='mono tiny' data-profile-latency='" + esc(item.name) + "'>未检测</td>" +
          "<td><div class='table-actions'>" + action + "</div></td></tr>"
        );
      }).join("")
    : "<tr><td colspan='7' class='hint'>没有可读的 profile 列表（权限不足或后端未返回）。</td></tr>";

  main.innerHTML = shell(
    "系统配置",
    "模型服务、模型与基础设施配置。",
    // **403 必须说出来**：本页的模型配置接口只对系统管理员开放，普通账号打开时
    // 数据是空的 —— 如果只渲染"未配置"，用户会以为模型真的没配（假空态）。
    (denied.length
      ? '<div class="notice-bar" style="margin-bottom:12px">权限不足：' +
        esc(denied[0].name) + " 对当前账号不可见（HTTP 403，需要系统管理员）。" +
        "下面的「未配置」<b>不代表</b>服务真的没配置，只是没有权限读取。</div>"
      : "") +
    '<div class="notice-bar" style="margin-bottom:12px">' +
      (environment.model_calls_disabled
        ? "模型服务当前不可调用；本页不会在后台发起任何探测请求。"
        : "为避免影响线上服务，本页不会主动探测模型服务是否在线。") +
    "</div>" +
    '<div class="card" style="padding:0">' +
      '<div style="padding:12px 14px;border-bottom:1px solid var(--color-divider)" class="row">' +
        '<span class="card-title" style="margin:0">远程模型服务</span>' +
        '<span class="grow"></span>' +
        // 「全部切换为远程 / 启动模型 / 切换为本地推理 / 切换为远程接口」四个控件
        // 已按用户要求**整条删除**（含各自的 title 指引）：本系统不管理 AS711
        // 服务的启停与推理位置，留着禁用按钮只会让人以为"功能被藏起来了"。
      "</div>" +
      tableRegion("远程模型服务", '<table class="table"><thead><tr><th>服务</th><th>模型</th><th>端点</th>' +
      "<th>状态</th><th>延迟</th><th>操作</th></tr></thead><tbody>" + body + "</tbody></table>") +
    "</div>" +
    '<div class="card" style="padding:0;margin-top:14px">' +
      '<div style="padding:12px 14px;border-bottom:1px solid var(--color-divider)" class="row">' +
        '<span class="card-title" style="margin:0">嵌入模型 profile（当前 / 预留）</span>' +
        '<span class="grow"></span>' +
        '<span class="hint">「预检」按需发起一次真实调用并回报<strong>实测维度</strong>；未配置的 profile 不能预检</span>' +
      "</div>" +
      tableRegion("嵌入模型配置", '<table class="table"><thead><tr><th>profile</th><th>模型</th><th>声明维度</th>' +
      "<th>端点</th><th>状态</th><th>预检结果</th><th>操作</th></tr></thead><tbody>" +
      profileRows + "</tbody></table>") +
    "</div>" +
    '<div class="grid-2" style="margin-top:14px">' +
      '<div class="card"><div class="card-title">模型</div>' +
        '<div class="kvs" style="flex-direction:column;gap:6px">' +
          // 只留**事实**：本系统通过远程接口调用 AS711 的四类模型。
          // 原来的「启动模型 / 切换为本地推理 / 切换为远程接口」三个禁用按钮与其
          // 指引文案已按用户要求删除 —— 本系统不提供也不假装提供模型管理能力。
          '<div class="row"><span class="hint" style="width:96px">推理位置</span><span>' +
            '<span class="tag info">远程接口（AS711）</span>' +
          "</span></div>" +
          '<div class="row"><span class="hint" style="width:96px">向量模型</span>' +
            '<span class="mono">' + esc(active.model || "未配置") + "</span></div>" +
          '<div class="row"><span class="hint" style="width:96px">重排模型</span>' +
            '<span class="mono">' + esc(rerank.model || "未配置") + "</span></div>" +
          '<div class="row"><span class="hint" style="width:96px">大模型</span>' +
            '<span class="mono">' + esc(llm.model || assistant.model || "未配置") +
            "</span></div>" +
          '<div class="row"><span class="hint" style="width:96px">OCR 引擎</span>' +
            '<span class="mono">' + esc(ocr.model || "未配置") + "</span></div>" +
        "</div>" +
      "</div>" +
      '<div class="card"><div class="card-title">基础设施配置</div>' +
        '<div class="kvs" style="flex-direction:column;gap:6px">' +
          // 数据库显示**真实引擎**（此前恒显示"未配置"）
          infraRow("数据库", environment.database_engine || "未配置") +
          infraRow("向量索引", environment.qdrant_collection_prefix
            ? "Qdrant · 前缀 " + environment.qdrant_collection_prefix : "未配置") +
          infraRow("存储", "内容寻址只读存储") +
          infraRow("任务队列", environment.upload_process_mode
            ? environment.upload_process_mode +
              (environment.dispatch_disabled ? "（派发已禁用）" : "")
            : "未配置") +
          infraRow("向量服务", active.model || "未配置") +
          infraRow("GPU 集群", "未配置") +
        "</div>" +
      "</div>" +
    "</div>"
  );

  main.querySelectorAll("[data-test-model]").forEach(function (button) {
    button.addEventListener("click", async function () {
      // **防重复点击**：一次探测要打真实模型服务，连点会打出一串请求
      if (button.disabled) return;
      if (environment.model_calls_disabled) {
        KB.toast("模型服务当前不可调用，请稍后重试");
        return;
      }
      const label = button.textContent;
      // **必须显式取名字**：早先这里没有定义 `name`，于是它静默解析成
      // 全局 `window.name`（空串）—— 三个症状都由此而来：
      //   1. 目标映射查不到 → 全部兜底成 embedding（点谁都只探 BGE）；
      //   2. 延迟列按 `name` 过滤 → 恒不匹配，**延迟列永远是"未检测"**；
      //   3. 非嵌入目标的提示里模型名是空的。
      const name = button.getAttribute("data-test-model") || "";
      button.disabled = true;
      button.textContent = "检测中…";
      try {
        // **按需探测**（`?probe=1`）：只有显式点"测试连通"才发起真实调用；
        // 返回值里的延迟是后端**实测**的。
        // **按被点击的那一行**定位目标：直接用按钮上挂的 `data-test-target`
        // （白名单键，后端只认这四个）。不再用"显示名 → 目标"的映射表 ——
        // 那种映射一旦文案改名就会静默兜底成 embedding，导致"点谁都只探 BGE"。
        const ALLOWED_TARGETS = ["embedding", "rerank", "ocr", "llm"];
        const rawTarget = button.getAttribute("data-test-target") || "";
        const target = ALLOWED_TARGETS.indexOf(rawTarget) >= 0 ? rawTarget : "embedding";
        const res = await KB.api(
          "/api/v1/knowledge/models/?probe=1&target=" + encodeURIComponent(target)
        );
        // 只处理**被点击目标**的实测结果：
        //   * embedding 的结果在 `profiles[]` 里；
        //   * rerank / ocr / llm 的结果在 `body.<target>` 里（只证明连通）。
        let measured = [];
        if (target === "embedding") {
          measured = ((res.ok && res.body && res.body.profiles) || []).filter(function (p) {
            return p.probe_latency_ms !== undefined && p.probe_latency_ms !== null;
          });
        } else {
          const one = res.ok && res.body && res.body[target];
          // 有延迟**或**有错误都要带出来：离线/未配置时后端不发请求、只给原因，
          // 若只认"有延迟"，界面会误报成"该服务不支持按需探测"。
          if (one && (one.probe_latency_ms !== undefined || one.probe_error)) {
            measured = [{ model: name, probe_latency_ms: one.probe_latency_ms,
                          probe_error: one.probe_error,
                          skipped: one.skipped,
                          note: one.note,
                          connectivity_only: one.connectivity_only,
                          implemented: one.implemented }];
          }
        }
        let filled = 0;
        measured.forEach(function (p) {
          // 选择器必须与单元格上的属性一致：单元格只有 `data-latency-row`，
          // 旧代码按 `[data-latency]` 找 → **一个都匹配不到**，延迟列因此
          // 永远停在"未检测"（用户截图里正是如此）。
          main.querySelectorAll("[data-latency-row]").forEach(function (cell) {
            // **只更新被点那一行**（用行下标绑定，避免同名模型串行）
            if (cell.getAttribute("data-latency-row") !== name) return;
            cell.textContent = p.probe_error
              ? (p.skipped ? "已禁用" : (p.implemented === false ? "未接入" : "连通失败"))
              : Number(p.probe_latency_ms).toFixed(0) + " ms";
            // 明确标注口径：非嵌入目标只证明**连通**，不冒充"模型能力已验证"
            const kind = p.connectivity_only ? "（仅连通性检查）" : "（真实维度探测）";
            cell.title = (p.probe_error
              ? (p.probe_error + kind)
              : ("实测延迟 " + p.probe_latency_ms + " ms" + kind)) +
              // 服务端的口径说明（例如"小预算下推理型模型正文可能为空，属正常"）
              (p.note ? "；" + p.note : "");
            filled += 1;
          });
        });
        if (!res.ok) {
          KB.toast((res.body && res.body.detail) || "探测失败", "err");
        } else if (!measured.length) {
          KB.toast("该服务不支持按需探测（后端未返回实测延迟）");
        } else {
          KB.toast("探测完成：" + measured.map(function (p) {
            if (p.implemented === false) return p.model + " 未接入按需探测";
            if (p.probe_error) return p.model + " 失败：" + p.probe_error;
            return p.model + " " + Math.round(p.probe_latency_ms) + " ms" +
              (p.connectivity_only ? "（仅连通）" : "");
          }).join("；"));
        }
      } finally {
        button.disabled = false;
        button.textContent = label;
      }
    });
  });
  // **指定已配置 profile 的预检**（含预留的非当前 profile）。
  // 这是"切换之前先验证目标模型真的能返回声明的维度"的唯一入口：
  //   * 只有**已配置**的 profile 有按钮（未配置的不能假装就绪）；
  //   * 默认不探测，点了才发真实调用；
  //   * 结果按 profile 名回填到那一行（匹配不上就不动，绝不串行）。
  main.querySelectorAll("[data-probe-profile]").forEach(function (button) {
    button.addEventListener("click", async function () {
      if (button.disabled) return;
      if (environment.model_calls_disabled) {
        KB.toast("模型服务当前不可调用，请稍后重试");
        return;
      }
      const profileName = button.getAttribute("data-probe-profile") || "";
      const label = button.textContent;
      button.disabled = true;
      button.textContent = "预检中…";
      try {
        // 白名单在**后端**：名字不在已配置表里会被 400 拒掉，前端不传端点/参数
        const res = await KB.api(
          "/api/v1/knowledge/models/?probe=1&target=embedding&profile=" +
            encodeURIComponent(profileName)
        );
        const item = ((res.ok && res.body && res.body.profiles) || []).filter(
          function (p) { return p.name === profileName; }
        )[0];
        let text = "未检测";
        let title = "";
        if (!res.ok) {
          text = "预检失败";
          title = (res.body && res.body.detail) || "请求失败";
          KB.toast(title, "err");
        } else if (!item) {
          text = "无结果";
          title = "后端没有返回该 profile 的预检结果";
          KB.toast(title, "err");
        } else if (item.probe_error) {
          text = "未就绪";
          title = item.probe_error;
          KB.toast(profileName + " 未就绪：" + item.probe_error, "err");
        } else if (
          item.probe_latency_ms !== undefined && item.probe_latency_ms !== null
        ) {
          const actual =
            item.probe && item.probe.actual_dimension
              ? item.probe.actual_dimension
              : "";
          text = Number(item.probe_latency_ms).toFixed(0) + " ms";
          title =
            "实测延迟 " + item.probe_latency_ms + " ms" +
            (actual
              ? "；实测维度 " + actual + "（与声明 " + (item.dimension || "?") + " 一致才判就绪）"
              : "") +
            "（真实维度探测，非仅连通性）";
          KB.toast(profileName + " 已就绪：" + text + (actual ? "（实测 " + actual + " 维）" : ""));
        }
        main.querySelectorAll("[data-profile-latency]").forEach(function (cell) {
          if (cell.getAttribute("data-profile-latency") !== profileName) return;
          cell.textContent = text;
          cell.title = title;
        });
      } finally {
        button.disabled = false;
        button.textContent = label;
      }
    });
  });
  // 「测试连通」是管理员能力：普通账号点它必然 403（后端校验），前端不该装作能用
  gateAdminControls(main);
}

/** 基础设施一行（label 原样照原型，值真实或"未配置"）。 */
function infraRow(label, value) {
  return (
    '<div class="row"><span class="hint" style="width:96px">' +
    esc(label) +
    '</span><span class="mono tiny">' +
    esc(value || "未配置") +
    "</span></div>"
  );
}

// ========================================================= Knowledge ====

async function loadKnowledge() {
  const main = document.getElementById("main");
  const [libraries, stats, operations] = await Promise.all([
    KB.api("/api/v1/knowledge/libraries/?page_size=200"),
    KB.api("/api/v1/knowledge/stats/"),
    KB.api("/api/v1/planner/operations/"),
  ]);
  const rows = (libraries.ok && libraries.body &&
    (libraries.body.results || libraries.body)) || [];
  const counts = (stats.ok && stats.body) || {};
  const ops = (operations.ok && operations.body && operations.body.results) || [];

  // 原型 4 统计：信息库 / 会话资料 / 已处理 / 待确认
  // 仅删除操作计入待确认；旧非删除待办保留在审计记录中。
  const pendingConfirm = ops.filter(function (op) {
    return ["proposed", "confirmed_1", "confirmed_2"].indexOf(op.status) >= 0 &&
      (op.requires_confirmation === true || (op.requires_confirmation === undefined &&
       (String(op.intent).startsWith("delete_") || op.intent === "reset_demo_data")));
  }).length;

  // 每个库**单独抓前 6 份**（用全局第一页会在库多时漏库、误报 0）
  const perLibrary = await Promise.all(rows.map(function (library) {
    // 每库**单独**抓前 6 份：用全局第一页会在库多时漏库、误报 0 份
    return KB.api("/api/v1/knowledge/files/?library=" + encodeURIComponent(library.code) +
      "&page_size=6");
  }));

  // **禁止把"接口失败"画成"还没有信息库"**：401/500 时必须如实报错，
  // 否则用户会以为自己的库被清空了（与"不伪造绿灯"是同一条原则）。
  const loadError = libraries.ok
    ? ""
    : ((libraries.body && libraries.body.detail) || "无法读取信息库列表");
  const cards = loadError
    ? '<div class="card"><div class="hint">加载失败：' + esc(loadError) +
      "（HTTP " + libraries.status + "）。请刷新重试 —— 这不代表「还没有信息库」。</div></div>"
    : rows.length
    ? rows.map(function (library, position) {
        const found = perLibrary[position];
        const files = (found.ok && found.body && (found.body.results || found.body)) || [];
        const total = (found.ok && found.body && typeof found.body.count === "number")
          ? found.body.count
          : (library.file_count || files.length);
        return (
          '<div class="card"><div class="card-title">' + KB.icon("library") +
            esc(library.name) +
            // tag 用服务端给的真实总数
            ' <span class="tag">' + total + " 份资料</span>" +
            (library.is_active ? "" : ' <span class="tag warn">已停用</span>') +
          "</div>" +
          '<ul class="op-output">' +
            (files.slice(0, 6).map(function (file) {
              return "<li>" + esc(file.original_name) +
                ' <span class="muted tiny">· ' + esc(file.department || "—") + " · " +
                esc(statusText(file.parse_status)) + "</span></li>";
            }).join("") || '<li class="muted tiny">还没有资料</li>') +
          "</ul>" +
          '<button class="btn sm" style="margin-right:8px" data-lib-detail="' + library.id + '">查看详细信息</button>' +
          '<button class="btn sm" data-lib-link="' + esc(library.code) +
            '">去文件管理看全部 →</button>' +
          // **删除信息库**：只对**系统管理员**可见（后端还会再验一次权限，
          // 前端隐藏只是减少误触，不是安全边界）。
          // 走既有受控流程（三次独立确认 + 末次输入完整库名），**没有**直连 DELETE。
          (isSystemAdmin()
            ? '<button class="btn sm" style="margin-left:8px" data-lib-reset="' +
              esc(library.code) + '">重置索引</button>' +
              '<button class="btn sm danger" style="margin-left:8px" data-lib-delete="' +
              esc(library.code) + '" data-lib-name="' + esc(library.name) + '">删除信息库</button>'
            : "") +
          "</div>"
        );
      }).join("")
    : '<div class="card"><div class="hint">还没有信息库</div></div>';

  main.innerHTML = shell(
    "信息库",
    "信息库与其资料的真实状态；索引到新目录会自动登记成新库。",
    // 「新建信息库」：仅系统管理员可见（后端还会再验一次权限）；
    // 创建走 planner 受控操作 `create_library`，不是直连写库。
    (isSystemAdmin()
      ? '<div class="row" style="margin-bottom:12px">' +
        '<button class="btn primary sm" id="lib-create-open">新建信息库</button>' +
        "</div>"
      : "") +
    '<div class="grid-3">' +
      statCard(rows.length, "信息库") +
      statCard(counts.session_files || 0, "会话资料") +
      statCard(counts.done || 0, "已处理") +
      statCard(pendingConfirm, "待确认") +
    "</div>" +
    '<div style="margin-top:14px">' + cards + "</div>"
  );

  const createOpen = main.querySelector("#lib-create-open");
  if (createOpen) createOpen.addEventListener("click", function () { openCreateLibraryDialog(); });

  main.querySelectorAll("[data-lib-detail]").forEach(function (button) {
    button.addEventListener("click", function () { KB.openLibraryDetails(button.dataset.libDetail); });
  });

  main.querySelectorAll("[data-lib-link]").forEach(function (button) {
    button.addEventListener("click", function () {
      location.href = "/app/files/?lib=" + encodeURIComponent(button.getAttribute("data-lib-link"));
    });
  });

  main.querySelectorAll("[data-lib-reset]").forEach(function (button) {
    button.addEventListener("click", async function () {
      if (button.disabled) return;      // 防重复点击：受控流程本身不能被并发触发
      button.disabled = true;
      try {
        await resetLibraryIndex(button);
      } finally {
        button.disabled = false;
      }
    });
  });

  main.querySelectorAll("[data-lib-delete]").forEach(function (button) {
    button.addEventListener("click", async function () {
      if (button.disabled) return;      // 防重复点击：受控流程本身不能被并发触发
      button.disabled = true;
      try {
        await deleteLibrary(button);
      } finally {
        button.disabled = false;
      }
    });
  });
}

/** 是否系统管理员（由模板在 `<body>` 上标记；仅用于**显示**，权限由后端判定）。 */
function isSystemAdmin() {
  return document.body.getAttribute("data-system-admin") === "1";
}

/**
 * 管理员专属控件对普通账号必须**置灰并写明原因**（Codex 独立验收）：
 * Agent 控制台的代次切换/回退/运维动作/校验审计链、系统配置的「测试连通」，
 * 后端都会 403 —— 前端还留一个可点的按钮，等于把用户引向注定失败的动作。
 *
 * 这里只做**显示层**处理（disabled + title + 一行可见说明），权限仍由后端判定；
 * 不要用"前端隐藏"代替后端校验。
 */
const ADMIN_ONLY_SELECTORS = [
  "[data-gen-activate]", "[data-gen-rollback]", "#audit-verify",
  "[data-agent-action]", "[data-test-model]", "[data-probe-profile]",
];

function gateAdminControls(root) {
  if (isSystemAdmin()) return;
  const scope = root || document;
  const buttons = scope.querySelectorAll(ADMIN_ONLY_SELECTORS.join(","));
  if (!buttons.length) return;
  buttons.forEach(function (button) {
    button.disabled = true;
    button.title = "需要系统管理员权限：当前账号执行该操作会被服务端拒绝（403）";
  });
  if (!document.getElementById("kb-admin-note")) {
    const note = document.createElement("div");
    note.id = "kb-admin-note";
    note.className = "hint";
    note.textContent = "当前账号不是系统管理员：以下操作按钮已置灰并写明原因，" +
      "不会发出注定被拒绝的请求。";
    const host = scope.querySelector(".grid-2") || scope.querySelector(".card") || scope;
    if (host && host.parentNode) host.parentNode.insertBefore(note, host);
    else if (host && host.insertBefore) host.insertBefore(note, host.firstChild);
  }
}

/**
 * 「新建信息库」弹窗：填库标识 / 名称 / 级别 → 走 planner 受控操作创建。
 *
 * 为什么不做直连 POST 建库：创建信息库是**写操作**，系统对所有写操作有统一口径
 * （受控提案 + 确认 + 凭证 + 服务端权限复核 + 审计）。绕过它就绕过了审计与权限复核。
 * `level` 必须真的生效（后端已校验，非法值报错）。
 */
function openCreateLibraryDialog() {
  const mask = KB.modal(
    '<div class="modal-head"><b>新建信息库</b></div>' +
    '<div class="modal-body paper">' +
      '<div class="field" style="display:block;margin-bottom:10px">' +
        '<label class="muted tiny">库标识（唯一，建议英文/数字/短横线）</label>' +
        '<input id="lib-code" type="text" style="width:100%" placeholder="例如 contract-2026">' +
      "</div>" +
      '<div class="field" style="display:block;margin-bottom:10px">' +
        '<label class="muted tiny">名称</label>' +
        '<input id="lib-name" type="text" style="width:100%" placeholder="例如 合同档案库 2026">' +
      "</div>" +
      '<div class="field" style="display:block;margin-bottom:10px">' +
        '<label class="muted tiny">级别</label>' +
        '<select id="lib-level" style="width:100%">' +
          '<option value="internal">内部（仅成员可读）</option>' +
          '<option value="external">外部（所有登录用户可读）</option>' +
        "</select>" +
      "</div>" +
      '<p class="tiny muted">创建后即可在「文件管理」上传资料；索引会为新库自动建代次。</p>' +
      '<p class="tiny err-text" id="lib-create-notice"></p>' +
    "</div>" +
    '<div class="modal-foot">' +
      '<button class="btn sm" data-close>取消</button>' +
      '<button class="btn primary sm" id="lib-create-submit">创建</button>' +
    "</div>",
    "520px"
  );
  const notice = mask.querySelector("#lib-create-notice");
  const submit = mask.querySelector("#lib-create-submit");
  submit.addEventListener("click", async function () {
    if (submit.disabled) return;              // 防重复提交
    const code = mask.querySelector("#lib-code").value.trim();
    const name = mask.querySelector("#lib-name").value.trim();
    const level = mask.querySelector("#lib-level").value;
    if (!code || !name) {
      KB.notice(notice, "请填写库标识与名称", "warn");
      return;
    }
    submit.disabled = true;
    try {
      const result = await KB.controlled({
        intent: "create_library",
        params: { code: code, name: name, level: level },
        labels: { title: "新建信息库" },
        extraLines: [
          "将创建信息库：" + name + "（" + code + "）",
          "级别：" + (level === "external"
            ? "外部（所有登录用户可读）"
            : "内部（仅成员可读）"),
        ],
      });
      if (!result.ok) {
        // 取消与失败分开反馈；失败如实报错（不谎称已创建）
        KB.notice(notice, result.skipped ? "已取消，未创建" : (result.detail || "创建失败"),
          result.skipped ? "warn" : "err");
        return;
      }
      mask.remove();
      KB.toast("已创建信息库：" + name);
      await loadKnowledge();               // 成功后刷新，新库出现在列表
    } finally {
      submit.disabled = false;
    }
  });
}

/**
 * 重置信息库索引：权限校验后直接执行，保留原件与业务记录。
 *
 * 为什么要有它：适配器/模型换代后，索引可能与资料对不上，需要「按原件重跑一遍」。
 * 但它**绝不等于清库**：业务记录一行不动、原件字节不擦除；只有**逐份强制重跑全部成功**
 * 之后才清理不再生效的旧代次 collection，中途失败只如实报部分完成、**不清理任何旧代次**。
 * 所以这个入口不提供任何「删资料」能力——删除仍只能走 delete_library / delete_file。
 */
async function resetLibraryIndex(button) {
  const code = button.getAttribute("data-lib-reset");

  const result = await KB.controlled({
    intent: "reset_library_index",
    params: { library: code },
    labels: { title: "重置信息库索引" },
    // 影响范围来自**提案响应**（后端权威）：资料份数与已登记集合都由后端算。
    impactFormatter: function (impact) {
      const collections = impact.collections || [];
      return [
        "将重建信息库索引：" + (impact.name || "") + "（" + (impact.code || "") + "）",
        "库内资料记录：" + (impact.file_count || 0) +
          " 份（逐份按原件强制重跑分段与向量；记录与原件一行不动）",
        collections.length
          ? "向量索引：" + collections.length +
            " 个已登记集合（含历史代次，仅在**全部**重建成功后清理旧代次）"
          : "向量索引：该库暂无已登记集合（重建时会自动登记）",
        impact.index_note || "重置只重建分段与向量，不会删除资料。",
      ];
    },
  });

  if (!result.ok) {
    if (result.skipped) {
      KB.toast("已取消，未做任何改动");
      return;
    }
    KB.toast((result.detail || "重置失败") + "（可重试；未完成时不会清理旧代次）");
    return;
  }

  const body = result.result || {};
  const done = body.complete === true;
  // 批次进度（持久续做游标）：把"这是第几批、累计多少、剩余多少"如实说出来 ——
  // 独立复现的缺陷正是"每次都只跑前 50 份，界面却只说'可再执行一次续做'"。
  const run = body.rebuild_run || {};
  // toast 用 textContent（`KB.toast` @ kb-common.js:427-434），markdown 的 `**` 会原样显示，
  // 所以这里用书名号/加粗文案的**纯文本**写法，不要留 `*`。
  if (done) {
    KB.toast("整库索引已重建完成：" + (run.processed_total || body.rebuilt || 0) + "/" +
      (run.scope_size || body.checked || 0) + " 份（旧代次已清理）");
  } else if (run.cursor_done && run.cleanup_allowed === false) {
    // 游标跑完了，但有永久无法重建的份 → 按安全规则不清旧代次（如实说）
    KB.toast("已重建完能重建的部分（" + (run.processed_total || 0) + "/" +
      (run.scope_size || 0) + " 份）；有 " + (body.unrebuildable || []).length +
      " 份永久无法重建（缺原件等），按安全规则未清理旧代次");
  } else {
    KB.toast("第 " + (run.batches_done || 1) + " 批完成：本批 " + (run.batch_this_run || body.rebuilt || 0) +
      " 份，累计 " + (run.processed_total || 0) + "/" + (run.scope_size || body.checked || 0) +
      " 份，剩余 " + (body.remaining || 0) + " 份；未清理任何旧代次。" +
      "再执行一次会从游标继续下一批（不会重复已重建的份）");
  }
  await loadKnowledge();
}

/**
 * 删除信息库：**必须**走 planner 受控操作，且确认卡写清影响范围。
 *
 * 为什么不能直接 DELETE：删库会连带删除库内全部资料、分段与**所有已登记代次**的向量，
 * 属于不可逆动作。系统对破坏性操作有统一约束（三次独立确认 + 末次准确输入目标名称 +
 * 一次性凭证 + 执行前后一致性校验），绕过它就会绕过审计与并发保护。
 */
async function deleteLibrary(button) {
  const code = button.getAttribute("data-lib-delete");

  const result = await KB.controlled({
    intent: "delete_library",
    params: { library: code },
    labels: { title: "删除信息库" },
    // **影响范围完全来自提案响应**（后端权威）：库名/id、资料数量、
    // 全部已登记集合。前端不再自己查集合（此前用 code 去比整数 pk 永远为空，
    // 且 page_size 会被服务端封顶 200 → 显示不全）。
    impactFormatter: function (impact) {
      const collections = impact.collections || [];
      return [
        "将删除信息库：" + (impact.name || "") + "（" + (impact.code || "") + "）",
        "库内资料记录：" + (impact.file_count || 0) + " 份（含其分段）",
        collections.length
          ? "向量索引：" + collections.length + " 个已登记集合（含历史代次）：" +
            collections.join("、")
          : "向量索引：该库暂无已登记集合",
        impact.originals_note || "原件按内容寻址只读保留，不会擦除原始字节。",
      ];
    },
  });

  if (!result.ok) {
    if (result.skipped) {
      KB.toast("已取消，未做任何改动");
      return;
    }
    // 失败**如实反馈**，不虚报完成；用户可重新发起（后端可重试未完成的向量清理）
    KB.toast((result.detail || "删除失败") + "（可重试）");
    return;
  }

  const body = result.result || {};
  const done = body.complete === true;
  // **必须显式取名字**：这里曾经直接写 `name`，而本函数作用域内没有它 →
  // 静默解析成全局 `window.name`（空串），成功回执变成"已删除信息库："后面什么都没有。
  // 受控删除是高危动作，回执必须带主体名称；后端 result 里可能有 name，兜底用用户点击的 code。
  const libraryName = body.name || button.getAttribute("data-lib-name") || code;
  KB.toast(done
    ? "已删除信息库：" + libraryName
    : "资料记录已删除，向量/原件清理尚未全部完成");
  // 删除成功后**刷新页面**：库应从列表消失
  await loadKnowledge();
}

// ============================================================= Tasks ====

/** 「添加记录」翻页的互斥与票据（处置清单 OP-25）。
 *
 * 旧实现每次点击直接 `loadTasks(current±1)`：请求在途时按钮仍可点，快速双击会
 * 发出两个请求，**后到的那个可能是越界的 404**，而失败分支会把整张表替换成
 * "加载失败（HTTP 404）" —— 用户看到的是"接口坏了"，其实只是多点了一下。
 *
 * 两道防线：
 *   * `tasksBusy`：请求在途时把两个翻页按钮置灰（真正的防重复提交）；
 *   * `tasksTicket`：只有最后一次请求的结果允许渲染（防乱序覆盖）。
 * 另加一条：请求页码先用**已知总页数**夹紧，绝不主动去请求一个越界页。
 */
let tasksBusy = false;
let tasksTicket = 0;
let tasksTotalPages = 1;

async function loadTasks(page) {
  const main = document.getElementById("main");
  const size = 10;
  // 请求页码必须**先夹紧**：既不请求越界页（后端 404），也让页码文案与真实请求一致
  const current = Math.max(1, Math.min(Number(page) || 1, tasksTotalPages));
  const ticket = ++tasksTicket;
  const pagers = function () {
    return Array.from(document.querySelectorAll("[data-task-page]"));
  };
  const setPagerBusy = function (busy) {
    pagers().forEach(function (button) { button.disabled = busy; });
  };
  tasksBusy = true;
  setPagerBusy(true);
  try {
    // 原型「添加记录」= **已入库的、来自 AI 会话的资料**（source 精确等于 session）
    const result = await KB.api(
      "/api/v1/knowledge/files/?source=session&page_size=" + size + "&page=" + current
    );
    if (ticket !== tasksTicket) return;   // 已有更新的请求在跑：这次结果作废
    const body = (result.ok && result.body) || {};
    const rows = body.results || [];
    const total = typeof body.count === "number" ? body.count : rows.length;
    const pages = Math.max(1, Math.ceil(total / size));
    tasksTotalPages = pages;
    // 真实页码：上面夹紧用的是"上一次已知的页数"，服务端这次给的 pages 才是权威
    const shownPage = Math.min(current, pages);

    // **表头恒定 7 列**（空态也保留，不能变成一行"暂无数据"）
    const head = "<thead><tr><th>资料</th><th>目标信息库</th><th>性质</th>" +
      "<th>状态</th><th>段落</th><th>添加时间</th><th>操作</th></tr></thead>";
    const tbody = rows.length
      ? "<tbody>" + rows.map(function (file) {
          return (
            "<tr><td><div class='name'>" + KB.fileIcon(file.file_type) + "<b>" +
              esc(file.original_name) + "</b></div></td>" +
            "<td class='tiny'>" + esc(file.library_name || file.library_code || "—") + "</td>" +
            // 性质：与文件管理/搜索页**同一口径**（空值显示「待识别」，不显示「—」；
            // 依据/来源进 title，缺字段就不写，绝不写"已自动识别"）
            "<td class='tiny'><span class='nature-label' title='" +
              esc(KB.natureTooltip ? KB.natureTooltip(file) : "") + "'>" +
              esc(KB.natureLabel ? KB.natureLabel(file) : (file.classification || "待识别")) +
              "</span></td>" +
            "<td><span class='status-dot " + dotClass(file.parse_status) + "'></span>" +
              esc(statusText(file.parse_status)) + "</td>" +
            "<td class='tiny mono'>" + mono(file.chunk_count || 0) + "</td>" +
            "<td class='tiny mono'>" + esc(KB.fmtTime(file.created_at)) + "</td>" +
            "<td><div class='table-actions'><button data-task-preview='" + file.id +
            "'>预览</button></div></td></tr>"
          );
        }).join("") + "</tbody>"
      : (result.ok
        ? '<tbody><tr><td colspan="7"><div class="empty-state" ' +
          'style="text-align:center;padding:24px 0"><p class="muted">' +
          "还没有会话来源的资料（通过助手添加的资料会显示在这里）</p></div></td></tr></tbody>"
        // 接口失败**不能**画成空态：用户会以为资料被清空了
        : '<tbody><tr><td colspan="7"><div class="empty-state" ' +
          'style="text-align:center;padding:24px 0"><p class="muted">加载失败：' +
          esc((result.body && result.body.detail) || "无法读取资料列表") +
          "（HTTP " + result.status + "）。请刷新重试。</p></div></td></tr></tbody>");

    // 数量放在**原分页 footer**，表格上方不再加额外统计行
    main.innerHTML = shell(
      "添加记录",
      "会话里加进来的资料都会留一条记录：来源、目标信息库、处理结果。",
      '<div class="notice-bar" style="margin-bottom:12px">这里只记录<b>已经入库</b>的会话来源资料；' +
        "助手的提案在确认并执行成功之前不会出现在这里。</div>" +
      '<div class="card" style="padding:0">' +
        tableRegion("添加记录", '<table class="table">' + head + tbody + "</table>") +
        '<div style="padding:12px 14px" class="row">' +
          '<span class="hint">共 ' + total + " 条会话来源资料 · 第 " +
            shownPage + " / " + pages + " 页</span>" +
          '<span class="grow"></span>' +
          // `data-task-page` 写的是**真实请求页**（夹紧后那个 current 推出的相邻页），
          // 不是 `shownPage±1` 的猜测值；页码文案与它同源，不会出现"显示第 1 页却请求第 2 页"。
          '<button class="btn sm" data-task-page="' + (current - 1) + '"' +
            (current <= 1 ? " disabled" : "") + ">上一页</button>" +
          '<button class="btn sm" data-task-page="' + (current + 1) + '"' +
            (current >= pages ? " disabled" : "") + ">下一页</button>" +
        "</div>" +
      "</div>"
    );

    main.querySelectorAll("[data-task-page]").forEach(function (button) {
      button.addEventListener("click", function () {
        // 请求在途时按钮已被置灰，这里再挡一次程序化触发
        if (tasksBusy || button.disabled) return;
        const next = Number(button.getAttribute("data-task-page"));
        if (!Number.isFinite(next) || next < 1 || next > tasksTotalPages) return;
        loadTasks(next);
      });
    });
    main.querySelectorAll("[data-task-preview]").forEach(function (button) {
      button.addEventListener("click", function () {
        openTaskPreview(button.getAttribute("data-task-preview"), rows);
      });
    });
  } finally {
    tasksBusy = false;
    // **按真实页码恢复禁用态**，不要一律打开：否则会把"已在第一页/最后一页"
    // 的按钮放成可点，点下去就是越界请求。
    pagers().forEach(function (button) {
      const target = Number(button.getAttribute("data-task-page"));
      button.disabled = !Number.isFinite(target) || target < 1 ||
        target > tasksTotalPages || tasksBusy;
    });
  }
}

/** 与文件管理一致的预览弹窗：读 /chunks/ 真实分段。 */
async function openTaskPreview(id, rows) {
  // **统一入口**：任务列表与资料列表/检索结果共用同一个原件预览实现。
  // 旧实现只显示 `chunks.text`（索引文字），不是原件预览。
  return KB.previewFile(id);
}

// ============================================================== Docs ====

async function loadDocs() {
  const main = document.getElementById("main");
  const result = await KB.api("/api/v1/planner/guide/");
  const steps = (result.ok && result.body && result.body.steps) || [];
  const environment = (result.ok && result.body && result.body.environment) || {};

  // 原型 viewDocs：一篇 card.paper，按 h3 分节
  main.innerHTML = shell(
    "使用说明",
    "系统怎么用，以及它的安全边界。",
    (result.ok
      ? ""
      : // 请求失败**必须说出来**：否则页面上只剩硬编码章节，
        // 用户会以为"说明就这么点"，而真正由后端下发的步骤（steps）其实没取到。
        '<div class="notice-bar danger">使用说明的<b>服务端步骤</b>没能读取：' +
          esc((result.body && result.body.detail) || ("HTTP " + result.status)) +
          "。下面显示的是本页内置的固定说明，不代表后端当前配置。</div>") +
    '<article class="card paper guide-article">' +
      "<h3>能用一句话做什么</h3><ul>" +
        "<li>找资料：<code>搜一下「质保期」</code>、<code>列出失败的资料</code></li>" +
        "<li>看资料：<code>列出资料库里的全部文件</code>、<code>看看运行状态</code></li>" +
        "<li>改资料：<code>解析待处理的资料</code>、<code>删除 xxx.pdf</code>（会先出确认卡）</li>" +
        "<li>运维：<code>重启停滞任务</code>、<code>重置熔断器</code>、<code>查看审计事件</code></li>" +
        "<li>配置：<code>查看嵌入模型配置</code>、<code>查看索引状态</code></li>" +
      "</ul>" +
      steps.map(function (step) {
        return "<h3>" + renderInline(step.title) + "</h3><p>" + renderInline(step.body) +
          "</p>";
      }).join("") +
      "<h3>安全边界</h3><ul>" +
        "<li>删除操作<b>先出确认卡</b>，需要<b>三次独立确认</b>，" +
          "最后一次必须准确输入目标名称，并使用一次性凭证。</li>" +
        "<li>确认有<b>执行窗口</b>；过期后必须重新发起提案。</li>" +
        "<li>已入库的<b>源资料只读</b>，不可编辑或覆盖。</li>" +
        "<li>删除会移除资料记录、关联分段与向量；<b>原件按内容寻址只读保留</b>（可能被其它资料共享）。</li>" +
        "<li>其他操作通过权限和参数校验后可直接执行。模型不可用时，会显示服务端返回的原因。</li>" +
      "</ul>" +
      "<h3>当前环境</h3><ul>" +
        "<li>模型服务：" + (environment.model_calls_disabled ? "当前不可调用" : "已启用") + "</li>" +
        "<li>任务派发：" + (environment.dispatch_disabled ? "已禁用" : "启用") + "</li>" +
      "</ul>" +
    "</article>"
  );
}

KB.pages = {
  loadAgent: loadAgent,
  loadSettings: loadSettings,
  loadKnowledge: loadKnowledge,
  loadTasks: loadTasks,
  loadDocs: loadDocs,
  dotClass: dotClass,
  statusText: statusText,
  statCard: statCard,
  shell: shell,
};
})();
