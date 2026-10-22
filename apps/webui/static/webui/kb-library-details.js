(function () {
  "use strict";
  const KB = window.KB;
  const esc = KB.esc;
  const vectorLabels = {pending: "待向量化", partial: "部分完成", indexed: "已向量化", failed: "向量化失败"};
  const date = function (value) { return value ? new Date(value).toLocaleString() : "—"; };
  function pager(page, count, size, attribute) {
    const pages = Math.max(1, Math.ceil(count / size));
    return '<div class="row" style="gap:10px;margin-top:12px"><button class="btn sm" ' + attribute + '="' +
      (page - 1) + '"' + (page <= 1 ? ' disabled' : '') + '>上一页</button><span class="tiny muted">第 ' +
      page + ' / ' + pages + ' 页，共 ' + count + ' 条</span><button class="btn sm" ' + attribute + '="' +
      (page + 1) + '"' + (page >= pages ? ' disabled' : '') + '>下一页</button></div>';
  }
  KB.openLibraryDetails = function (libraryId) {
    const mask = KB.modal('<div class="modal-head"><b>信息库存储详情</b><span class="grow"></span>' +
      '<button class="btn sm" data-close>关闭</button></div><div class="modal-body paper library-detail-body">' +
      '<div data-library-summary></div><form class="row" data-document-search style="gap:8px;margin:12px 0">' +
      '<input class="input grow" name="q" placeholder="按文档名称搜索" aria-label="搜索库内文档"><button class="btn sm">搜索</button></form>' +
      '<div data-document-list>正在读取…</div></div>', "1080px");
    const list = mask.querySelector("[data-document-list]");
    let revision = 0, keyword = "";
    async function load(page) {
      const request = ++revision;
      list.textContent = "正在读取…";
      const result = await KB.api("/api/v1/knowledge/libraries/" + libraryId +
        "/documents/?page_size=10&page=" + page + "&q=" + encodeURIComponent(keyword));
      if (!mask.isConnected || request !== revision) return;
      if (!result.ok) {
        list.innerHTML = '<div class="hint">' + esc(KB.errorText(result, "详情读取失败")) +
          '</div><button class="btn sm" data-documents-page="' + page + '">重试</button>'; return;
      }
      const body = result.body, library = body.library, totals = body.totals;
      mask.querySelector("[data-library-summary]").innerHTML = '<h3>' + esc(library.name) +
        '</h3><p class="muted small">' + esc(library.description || "") + '</p><div class="row" style="gap:10px;flex-wrap:wrap">' +
        '<span class="tag">' + totals.documents + ' 份文档</span><span class="tag">' + totals.parsed_chunks +
        ' 个解析分段</span><span class="tag">' + totals.embedded_chunks + ' 个向量记录</span></div>';
      list.innerHTML = (body.results || []).map(function (file) {
        return '<details class="stored-document" data-file="' + file.id + '"><summary><b>' + esc(file.original_name) +
          '</b><span class="tag">' + esc(file.status_label) + '</span><span class="tiny muted">解析 ' + file.chunk_count +
          ' 段 · 向量 ' + file.embedded_chunk_count + ' 段 · ' + esc(vectorLabels[file.vector_status] || file.vector_status || "待向量化") +
          '</span></summary><div data-file-content="' + file.id + '"></div></details>';
      }).join("") || '<p class="hint">' + (keyword ? "没有匹配的文档。" : "该信息库还没有存储文档。") + '</p>';
      list.innerHTML += pager(page, body.count, 10, "data-documents-page");
    }
    mask.querySelector("[data-document-search]").addEventListener("submit", function (event) {
      event.preventDefault(); keyword = event.target.elements.q.value.trim(); load(1);
    });
    list.addEventListener("toggle", function (event) {
      const details = event.target;
      if (!details.matches("details[data-file]") || !details.open || details.dataset.loaded) return;
      details.dataset.loaded = "1";
      loadFile(details, 1);
    }, true);
    list.addEventListener("click", function (event) {
      const documents = event.target.closest("[data-documents-page]");
      if (documents && !documents.disabled) load(Number(documents.dataset.documentsPage));
      const chunks = event.target.closest("[data-chunks-page]");
      if (chunks && !chunks.disabled) loadFile(chunks.closest("details[data-file]"), Number(chunks.dataset.chunksPage));
    });
    load(1);
  };
  async function loadFile(details, page) {
    const host = details.querySelector("[data-file-content]");
    const revision = (Number(host.dataset.revision) || 0) + 1;
    host.dataset.revision = revision;
    host.textContent = "正在读取解析与向量化信息…";
    const result = await KB.api("/api/v1/knowledge/files/" + details.dataset.file + "/stored-info/?page_size=20&page=" + page);
    if (!host.isConnected || Number(host.dataset.revision) !== revision) return;
    if (!result.ok) {
      host.innerHTML = '<p class="hint">' + esc(KB.errorText(result, "读取失败")) +
        '</p><button class="btn sm" data-chunks-page="' + page + '">重试</button>'; return;
    }
    const body = result.body, file = body.file, vector = body.vector;
    const field = function (label, value) { return '<div><dt>' + esc(label) + '</dt><dd>' + esc(String(value == null || value === "" ? "—" : value)) + '</dd></div>'; };
    host.innerHTML = '<dl class="stored-metadata">' + field("文件大小", file.size_display) +
      field("资料性质", file.classification) + field("添加时间", date(file.created_at)) +
      field("解析完成时间", date(file.parsed_at)) + field("目录", file.directory_path) +
      field("向量化状态", vectorLabels[vector.status] || vector.status) + field("索引集合", vector.collection) +
      field("索引代次", vector.generation) + field("嵌入模型", vector.model) + field("向量维度", vector.dimension) +
      field("存储方式", {server: "向量服务", embedded: "本地存储"}[vector.storage_mode] || vector.storage_mode) +
      field("内容指纹 SHA-256", file.content_sha256) + '</dl>' +
      (file.parse_error ? '<p class="hint">处理说明：' + esc(file.parse_error) + '</p>' : '') +
      '<h4>解析分段与向量关联</h4><p class="tiny muted">向量数量和状态来自处理记录；点标识用于关联同一文档的解析分段。</p>' +
      body.results.map(function (chunk) {
        return '<section class="stored-chunk"><div class="small"><b>分段 ' + chunk.ordinal + '</b> · ' +
          (chunk.page == null ? "无页码" : '第 ' + chunk.page + ' 页') + ' · 约 ' + chunk.token_estimate +
          ' tokens</div><div class="tiny muted">向量点标识：' + esc(chunk.point_id) +
          '</div><pre class="stored-text">' + esc(chunk.text) + '</pre></section>';
      }).join("") + (!body.results.length ? '<p class="hint">暂无解析分段；' +
        (file.parse_status === "failed" ? '请在文件管理中检查并重试处理。' : file.parse_status === "archived" ? '文档已归档。' : '文档可能尚未完成解析。') + '</p>' : '') +
      pager(page, body.count, 20, "data-chunks-page");
  }
})();
