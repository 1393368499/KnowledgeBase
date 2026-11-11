(function () {
  "use strict";
  const KB = window.KB;
  const esc = KB.esc;
  const form = document.getElementById("template-upload-form");
  const list = document.getElementById("template-list");
  let revision = 0;
  async function load() {
    const request = ++revision;
    list.textContent = "正在读取模板…";
    const params = new URLSearchParams({category: document.getElementById("template-filter").value,
      q: document.getElementById("template-keyword").value});
    const result = await KB.api("/api/v1/planner/templates/?" + params);
    if (request !== revision) return;
    if (!result.ok) { list.textContent = "读取失败：" + KB.errorText(result, "请重试"); return; }
    const rows = result.body.results || [];
    document.getElementById("template-count").textContent = rows.length + " 份";
    list.innerHTML = rows.length ? '<ul class="template-index">' + rows.map(function (row) {
      return '<li class="template-entry"><div class="template-entry-meta"><div class="row"><b>' + esc(row.name) +
        '</b><span class="tag">' + esc(row.category_label) + '</span></div><p class="muted small">' +
        esc(row.description || "暂无用途说明") + '</p><div class="tiny muted">' + esc(row.original_name) +
        ' · ' + Math.ceil(row.size_bytes / 1024) + ' KB · ' + row.text_chars + ' 字' +
        ' · 添加于 ' + esc(new Date(row.created_at).toLocaleString()) + '</div></div>' +
        '<div class="row"><button class="btn sm" data-preview="' + row.id +
        '">查看详情</button><a class="btn sm" href="/api/v1/planner/templates/' + row.id +
        '/download/">下载原件</a>' + (row.can_delete ? '<button class="btn sm danger" data-delete="' + row.id +
        '" data-name="' + esc(row.name) + '">删除</button>' : '') + '</div></li>';
    }).join("") + '</ul>' : '<p class="hint">没有符合条件的模板。可在上方添加模板。</p>';
  }
  form.elements.file.addEventListener("change", function () {
    const file = form.elements.file.files[0];
    if (!file) return;
    if (/\.(pptx?|potx)$/i.test(file.name)) form.elements.category.value = "presentation";
    else if (/\.(xlsx|csv)$/i.test(file.name)) form.elements.category.value = "other";
  });
  form.addEventListener("submit", async function (event) {
    event.preventDefault();
    const button = form.querySelector("button[type=submit]");
    if (button.disabled) return;
    button.disabled = true;
    try {
      const result = await KB.api("/api/v1/planner/templates/", {method: "POST", form: new FormData(form)});
      KB.notice(document.getElementById("template-notice"), result.ok ? "模板已添加，可在会话确认卡或文档侧栏选择。" :
        KB.errorText(result, "上传失败"), result.ok ? "ok" : "err");
      if (result.ok) { form.reset(); await load(); }
    } finally { button.disabled = false; }
  });
  list.addEventListener("click", async function (event) {
    const preview = event.target.closest("[data-preview]");
    if (preview) {
      const result = await KB.api("/api/v1/planner/templates/" + preview.dataset.preview + "/");
      if (!result.ok) { KB.toast(KB.errorText(result, "详情读取失败"), "err"); return; }
      const row = result.body;
      KB.modal('<div class="modal-head"><b>' + esc(row.name) + '</b><span class="tag">' +
        esc(row.category_label) + '</span></div><div class="modal-body paper"><p>' + esc(row.description || "") +
        '</p><p class="tiny muted">' + esc(row.original_name) + ' · ' + row.text_chars +
        ' 字</p><pre class="stored-text">' + esc(row.content_text) +
        '</pre></div><div class="modal-foot"><button class="btn sm" data-close>关闭</button></div>', "860px");
    }
    const remove = event.target.closest("[data-delete]");
    if (remove && !remove.disabled) {
      const confirmed = await KB.confirmDialog({title: "删除模板？", body: "删除「" + remove.dataset.name +
        "」后无法再选择它，已生成的文档保留。", confirmLabel: "删除模板", danger: true});
      if (!confirmed || !confirmed.confirmed) return;
      remove.disabled = true;
      const result = await KB.api("/api/v1/planner/templates/" + remove.dataset.delete + "/", {method: "DELETE"});
      if (result.ok) { KB.toast("模板已删除", "ok"); await load(); }
      else { KB.toast(KB.errorText(result, "删除失败"), "err"); remove.disabled = false; }
    }
  });
  document.getElementById("template-filter").addEventListener("change", load);
  document.getElementById("template-refresh").addEventListener("click", load);
  document.getElementById("template-search").addEventListener("submit", function (event) { event.preventDefault(); load(); });
  load();
})();
