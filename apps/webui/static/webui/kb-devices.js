(function () {
  "use strict";
  const KB = window.KB, $ = (id) => document.getElementById(id), esc = KB.esc;
  let devices = [], editing = null;
  function notice(text) { $("device-notice").textContent = text; }
  async function loadEndpoint() {
    try { const data = await api("endpoint/");
      $("remote-endpoint").value = data.server || "";
      $("remote-endpoint-state").textContent = data.server ? "保存时间：" + new Date(data.updated_at).toLocaleString() : data.detail;
    } catch (e) { $("remote-endpoint-state").textContent = "地址暂时无法读取，请刷新重试"; }
  }
  $("remote-endpoint-refresh").onclick = loadEndpoint;
  $("remote-endpoint-copy").onclick = async function () {
    const field = $("remote-endpoint"); if (!field.value) return;
    try { await navigator.clipboard.writeText(field.value); $("remote-endpoint-state").textContent = "已复制，可粘贴到手机 App"; }
    catch (e) { field.focus(); field.select(); $("remote-endpoint-state").textContent = "请复制已选中的地址"; }
  };
  loadEndpoint();
  async function api(path, options) {
    const result = await KB.api("/api/v1/voice/" + path, options);
    if (!result.ok) throw new Error(KB.errorText(result, "请求失败"));
    return result.body;
  }
  function showSecret(token) {
    $("device-token").value = token;
    $("device-secret").hidden = false;
  }
  async function load() {
    const body = await api("devices/");
    devices = body.results;
    $("device-list").innerHTML = devices.length ? devices.map(d =>
      '<article class="card" style="padding:20px;margin:14px 0" data-device="' + esc(d.id) + '">' +
      '<div class="row"><h2 style="margin:0">' + esc(d.name) + '</h2><span class="badge">' + (d.enabled ? (d.online ? "在线" : "离线") : "已停用") + '</span></div>' +
      '<p class="muted">' + esc(d.device_type) + ' · ' + esc(d.scope_mode === "selected" ? "指定知识库" : "全部可读知识库") + ' · ' + (d.conversation_mode === 'tasks' ? '按任务选择会话 · 最多 4 项并行' : d.conversation_mode === 'per_turn' ? '每条语音一个会话' : '同一会话') + '</p>' +
      '<div class="row"><a class="btn sm" href="/app/?conversation=' + encodeURIComponent(d.conversation_id || "") + '">打开会话</a>' +
      '<button class="btn sm" data-action="edit">设置</button><button class="btn sm" data-action="toggle">' + (d.enabled ? "停用" : "启用") + '</button>' +
      '<button class="btn sm" data-action="rotate">重新发放凭据</button><button class="btn sm" data-action="remove">移除设备</button></div></article>'
    ).join("") : '<div class="card" style="padding:36px;text-align:center">还没有设备。点击“添加设备”开始使用。</div>';
  }
  async function openEditor(device) {
    editing = device || null;
    notice("");
    $("device-editor").hidden = false;
    $("device-editor-title").textContent = editing ? "设备设置" : "添加设备";
    $("device-save").textContent = editing ? "保存设置" : "添加设备";
    $("device-name").value = editing ? editing.name : "";
    $("device-type").disabled = !!editing;
    $("device-scope").value = editing ? editing.scope_mode : "all_authorized";
    $("device-mode").value = editing ? (editing.conversation_mode || "fixed") : "tasks";
    $("device-conversation-row").hidden = $("device-mode").value !== "fixed";
    const conversations = await KB.api("/api/v1/planner/conversations/");
    if (!conversations.ok) throw new Error("读取会话失败");
    const rows = conversations.body.results || [];
    $("device-conversation").innerHTML = '<option value="">新建设备专用会话</option>' + rows.map(c => '<option value="' + c.id + '">' + esc(c.title || "会话 " + c.id) + '</option>').join("");
    if (editing && editing.conversation_id && !rows.some(c => c.id === editing.conversation_id)) {
      const option = document.createElement("option"); option.value = editing.conversation_id;
      option.textContent = "当前设备会话 #" + editing.conversation_id; $("device-conversation").appendChild(option);
    }
    $("device-conversation").value = editing && editing.conversation_id ? String(editing.conversation_id) : "";
    const libs = await KB.api("/api/v1/knowledge/libraries/?active=1&page_size=200");
    if (!libs.ok) throw new Error("读取知识库失败");
    $("device-library-options").innerHTML = (libs.body.results || []).map(l =>
      '<label style="display:block;padding:6px"><input type="checkbox" value="' + esc(l.code) + '"' + (editing && editing.library_codes.includes(l.code) ? ' checked' : '') + '> ' + esc(l.name) + '</label>'
    ).join("") || "没有可选择的知识库";
    $("device-libraries").hidden = $("device-scope").value !== "selected";
    $("device-name").focus();
  }
  function fail(error) { notice(error.message || "操作失败"); }
  document.addEventListener("DOMContentLoaded", function () {
    load().catch(fail);
    $("add-device").addEventListener("click", () => openEditor().catch(fail));
    $("device-cancel").addEventListener("click", () => { $("device-editor").hidden = true; });
    $("device-scope").addEventListener("change", () => { $("device-libraries").hidden = $("device-scope").value !== "selected"; });
    $("device-mode").addEventListener("change", () => { $("device-conversation-row").hidden = $("device-mode").value !== "fixed"; });
    $("device-secret-close").addEventListener("click", () => { $("device-token").value = ""; $("device-secret").hidden = true; });
    $("device-form").addEventListener("submit", async function (event) {
      event.preventDefault();
      const button = $("device-save"); button.disabled = true;
      try {
        const payload = {name: $("device-name").value, conversation_mode: $("device-mode").value, conversation_id: $("device-mode").value === "fixed" ? ($("device-conversation").value || null) : null,
          scope_mode: $("device-scope").value, library_codes: Array.from($("device-library-options").querySelectorAll("input:checked")).map(n => n.value)};
        if (!editing) payload.device_type = $("device-type").value;
        const result = await api(editing ? "devices/" + editing.id + "/" : "devices/", {method: editing ? "PATCH" : "POST", json: payload});
        $("device-editor").hidden = true;
        notice(editing ? "设备设置已保存" : "设备已添加");
        if (result.device_token) showSecret(result.device_token);
        await load();
      } catch (error) { fail(error); } finally { button.disabled = false; }
    });
    $("device-list").addEventListener("click", async function (event) {
      const button = event.target.closest("button[data-action]");
      if (!button) return;
      const d = devices.find(row => row.id === button.closest("[data-device]").dataset.device);
      const action = button.dataset.action;
      button.disabled = true;
      try {
        if (action === "edit") await openEditor(d);
        else if (action === "toggle") await api("devices/" + d.id + "/", {method: "PATCH", json: {enabled: !d.enabled}});
        else if (action === "remove" && window.confirm("移除“" + d.name + "”？已有会话会保留。")) await api("devices/" + d.id + "/", {method: "DELETE"});
        else if (action === "rotate" && window.confirm("重新发放后，原设备凭据立即失效。继续？")) {
          const result = await api("devices/" + d.id + "/rotate-token/", {method: "POST", json: {}}); showSecret(result.device_token);
        }
        await load();
      } catch (error) { fail(error); } finally { button.disabled = false; }
    });
    setInterval(() => { if (!document.hidden && $("device-editor").hidden) load().catch(fail); }, 15000);
  });
})();
