/* 静态检查 kb-markdown.js：语法 + 用 DOM 桩验证渲染与转义（不启动浏览器）。 */
const fs = require('fs');

const src = fs.readFileSync('apps/webui/static/webui/kb-markdown.js', 'utf8');
global.window = {};
new Function(src)();               // 语法检查 + 挂载到 window.KB
const render = global.window.KB.renderMarkdown;

const md = [
  '# 标题',
  '',
  '**粗体** 与 `代码` 与 [链接](https://x.com) 与 [坏](javascript:alert(1))',
  '',
  '- 甲',
  '- 乙',
  '',
  '1. 一',
  '2. 二',
  '',
  '```',
  '<b>raw</b>',
  '```',
  '',
  '| A | B |',
  '|---|---|',
  '| 1 | 2 |',
  '',
  '> 引用',
  '',
  '---',
  '',
  '<script>alert(1)</script>',
].join('\n');

const html = render(md);
const checks = {
  '标题': html.includes('<h2>标题</h2>'),
  '粗体': html.includes('<strong>粗体</strong>'),
  '行内代码': html.includes('<code>代码</code>'),
  '正常链接保留': html.includes('href="https://x.com"'),
  '危险协议被剥离': !html.includes('javascript:alert'),
  '无序列表': html.includes('<ul>') && html.includes('<li>甲</li>'),
  '有序列表': html.includes('<ol>') && html.includes('<li>一</li>'),
  '代码块被转义': html.includes('&lt;b&gt;raw&lt;/b&gt;'),
  '表格': html.includes('<th>A</th>') && html.includes('<td>1</td>'),
  '引用': html.includes('<blockquote>引用</blockquote>'),
  '分隔线': html.includes('<hr>'),
  '原始 script 被转义': html.includes('&lt;script&gt;') && !html.includes('<script>'),
};

let failed = 0;
for (const [name, ok] of Object.entries(checks)) {
  if (!ok) failed += 1;
  console.log((ok ? 'PASS' : 'FAIL') + ' ' + name);
}
console.log(failed ? `${failed} FAILED` : 'all markdown checks passed');
process.exit(failed ? 1 : 0);
