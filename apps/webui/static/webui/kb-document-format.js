(function () {
  "use strict";
  const fonts = {xiaobiaosong: "方正小标宋简体", fangsong: "仿宋", heiti: "黑体", kaiti: "楷体", simsun: "宋体"};
  const colors = {black: "#000000", red: "#FF0000"};
  const fields = {title_font: "标题字体", body_font: "正文字体", heading_font: "一级标题字体",
    subheading_font: "二级标题字体", title_color: "标题颜色", text_color: "正文颜色"};
  const host = document.getElementById("doc-format-controls");
  Object.keys(fields).forEach(function (key) {
    const label = document.createElement("label");
    label.textContent = fields[key];
    const select = document.createElement("select");
    select.dataset.docFormat = key;
    select.id = "doc-format-" + key;
    select.add(new Option("沿用模板/默认", ""));
    const choices = key.endsWith("_font") ? fonts : {black: "黑色", red: "红色"};
    Object.keys(choices).forEach(function (value) { select.add(new Option(choices[value], value)); });
    label.appendChild(select);
    host.appendChild(label);
  });
  window.KB.docFormat = {
    signature: function (values) {
      const canonical = {};
      Object.keys(values || {}).sort().forEach(function (key) { canonical[key] = values[key]; });
      return JSON.stringify(canonical);
    },
    read: function () {
      const values = {};
      host.querySelectorAll("select").forEach(function (select) { if (select.value) values[select.dataset.docFormat] = select.value; });
      return values;
    },
    set: function (values) {
      host.querySelectorAll("select").forEach(function (select) { select.value = (values || {})[select.dataset.docFormat] || ""; });
    },
    apply: function () {
      const values = this.read();
      const preview = document.getElementById("doc-render");
      const title = document.getElementById("doc-title").value.trim();
      const headings = Array.from(preview.querySelectorAll("h1,h2,h3,h4,h5,h6"));
      headings.forEach(function (node, index) {
        node.classList.toggle("doc-format-title", index === 0 && node.textContent.trim() === title);
      });
      Object.keys(fields).forEach(function (key) {
        const chosen = values[key];
        const value = key.endsWith("_font") ? fonts[chosen] : colors[chosen];
        preview.classList.toggle("has-" + key, Boolean(value));
        if (value) preview.style.setProperty("--doc-" + key, key.endsWith("_font") ? '"' + value + '"' : value);
        else preview.style.removeProperty("--doc-" + key);
      });
      const editor = document.getElementById("doc-body");
      editor.style.fontFamily = fonts[values.body_font] || "";
      editor.style.color = colors[values.text_color] || "";
    }
  };
})();
