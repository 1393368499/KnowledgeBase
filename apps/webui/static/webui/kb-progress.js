/* 统一进度显示：百分比来自真实工作量，未知总量使用不定进度条。 */
(function () {
  "use strict";
  const KB = window.KB, esc = KB.esc;
  const fileRequests = new Set(), operationRequests = new Set(), seenFiles = new Set();
  let timer = null, polling = false;

  function elapsedLabel(started) {
    const seconds = Math.max(0, Math.floor(Date.now()/1000 - Number(started)));
    return "已用时 " + (seconds >= 60 ? Math.floor(seconds/60) + "分 " : "") + seconds%60 + "秒";
  }

  function markup(progress) {
    const p = progress || {}, state = p.state || "running";
    const known = p.percent !== null && p.percent !== undefined && Number.isFinite(Number(p.percent));
    const value = known ? Math.max(0, Math.min(100, Number(p.percent))) : null;
    const count = p.total > 0 && p.current !== null && p.current !== undefined
      ? p.current + " / " + p.total + " " + (p.unit || "") + (known ? " · " + value + "%" : "")
      : (known ? value + "%" : "");
    const label = p.label || "正在处理";
    return '<div class="work-progress state-' + esc(state) + '">' +
      '<div class="work-progress-heading"><span>' + esc(label) + '</span><span>' + esc(count) + '</span></div>' +
      '<progress max="100"' + (known ? ' value="' + value + '"' :
        (["running", "queued"].includes(state) ? "" : ' value="0"')) +
      ' aria-label="' + esc(label) + '"></progress>' +
      '<div class="work-progress-detail"><span>' + esc(p.detail || "") + '</span>' +
      (p.started_at && state === "running" ? '<span data-progress-clock="' + Number(p.started_at) + '">' + elapsedLabel(p.started_at) + '</span>' : "") +
      '</div></div>';
  }

  function widget(kind, id, progress) {
    return '<div data-' + kind + '-progress="' + esc(id) + '" data-progress-state="' +
      esc((progress || {}).state || "queued") + '">' + markup(progress) + '</div>';
  }

  function elements(kind, id) {
    return Array.from(document.querySelectorAll('[data-' + kind + '-progress="' + id + '"]'));
  }

  function paint(kind, id, progress) {
    progress = progress || {state: "running", label: "正在读取进度"};
    elements(kind, id).forEach(function (node) {
      node.hidden = kind === "operation" && progress.state === "queued";
      node.dataset.progressState = progress.state;
      node.innerHTML = markup(progress);
    });
  }

  function activeIds(kind) {
    return Array.from(new Set(Array.from(document.querySelectorAll('[data-' + kind + '-progress]'))
      .filter(function (node) { return !node.hidden && ["running", "queued"].includes(node.dataset.progressState); })
      .map(function (node) { return Number(node.getAttribute('data-' + kind + '-progress')); })
      .filter(function (id) { return id > 0; })));
  }

  function schedule(delay) {
    if (timer || polling || (!activeIds("file").length && !activeIds("operation").length)) return;
    timer = setTimeout(poll, delay === undefined ? 1800 : delay);
  }

  async function pollKind(kind) {
    const ids = activeIds(kind);
    for (let index = 0; index < ids.length; index += 100) {
      const batch = ids.slice(index, index + 100);
      const url = kind === "file" ? "/api/v1/knowledge/files/progress/" : "/api/v1/planner/operations/progress/";
      const response = await KB.api(url + "?ids=" + batch.join(","));
      if (!response.ok) {
        batch.forEach(function (id) {
          elements(kind, id).forEach(function (node) {
            const detail = node.querySelector(".work-progress-detail > span");
            if (detail) detail.textContent = "进度暂时无法更新，正在重试";
          });
        });
        return;
      }
      const rows = (response.body || {}).results || [];
      const found = new Set(rows.map(function (row) { return row.id; }));
      batch.filter(function (id) { return !found.has(id); }).forEach(function (id) {
        paint(kind, id, {state: "cancelled", label: "资料或操作不存在，或已无权查看", percent: null});
      });
      rows.forEach(function (row) {
        if (kind === "file") {
          if (row.parse_status === "running") seenFiles.add(row.id);
          if (fileRequests.has(row.id) && !seenFiles.has(row.id) && ["done", "failed"].includes(row.parse_status)) return;
        } else if (operationRequests.has(row.id) && row.progress.state === "queued") {
          return;
        }
        paint(kind, row.id, row.progress);
        document.dispatchEvent(new CustomEvent("kb:" + kind + "-progress", {detail: row}));
      });
    }
  }

  async function poll() {
    timer = null;
    polling = true;
    try { await Promise.all([pollKind("file"), pollKind("operation")]); }
    catch (error) { /* 请求失败仍保留已知状态，下一轮继续查询。 */ }
    finally { polling = false; schedule(document.hidden ? 8000 : 1800); }
  }

  function start(host, label) {
    const parent = typeof host === "string" ? document.getElementById(host) : host;
    if (parent) parent.querySelectorAll(":scope > [data-request-progress]").forEach(function (node) {
      if (node.dataset.requestProgress !== "running") node.remove();
    });
    const element = document.createElement("div");
    element.dataset.requestProgress = "running";
    if (parent) parent.appendChild(element);
    const started = Date.now() / 1000;
    function update(p) { element.innerHTML = markup(Object.assign({state: "running", label: label, started_at: started}, p || {})); }
    update();
    return {element: element, update: update, finish: function (ok, text) {
      element.dataset.requestProgress = ok ? "done" : "failed";
      update({state: ok ? "done" : "failed", label: text || (ok ? "请求已完成" : "请求未完成"), percent: ok ? 100 : null});
      setTimeout(function () { element.remove(); }, ok ? 1400 : 5000);
    }};
  }

  async function fileRequest(url, options) {
    const match = /\/files\/(\d+)\/process\//.exec(url);
    const id = match ? Number(match[1]) : null;
    if (id) {
      fileRequests.add(id);
      seenFiles.delete(id);
      paint("file", id, {state: "running", label: "正在提交解析", started_at: Date.now()/1000});
      schedule(150);
    }
    try {
      const response = await KB.api(url, options);
      if (id && response.body && response.body.file) {
        const file = response.body.file;
        paint("file", id, file.progress);
        document.dispatchEvent(new CustomEvent("kb:file-progress", {detail: file}));
      }
      return response;
    } finally { if (id) fileRequests.delete(id); schedule(); }
  }

  KB.progress = {
    markup: markup, widget: widget, start: start, paint: paint, fileRequest: fileRequest,
    mount: function () { schedule(100); },
    beginOperation: function (id) {
      operationRequests.add(Number(id));
      paint("operation", id, {state: "running", label: "正在提交执行", started_at: Date.now()/1000});
      schedule(100);
    },
    endOperation: function (id) { operationRequests.delete(Number(id)); schedule(); },
    isOperationRequest: function (id) { return operationRequests.has(Number(id)); }
  };

  setInterval(function () {
    document.querySelectorAll("[data-progress-clock]").forEach(function (node) {
      node.textContent = elapsedLabel(node.dataset.progressClock);
    });
  }, 1000);
  document.addEventListener("DOMContentLoaded", function () {
    const main = document.getElementById("main");
    if (main) new MutationObserver(function () { schedule(150); }).observe(main, {childList: true, subtree: true});
    schedule(100);
  });
}());
