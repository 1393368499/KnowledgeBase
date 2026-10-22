/* 静态审计 JS：找出"被调用但未定义"的顶层标识符。
 *
 * 为什么需要它（真实事故）：`kb-assistant.js` 的 `loadJobs` 曾被误删，
 * 而 `node -e "new Function(src)"` **解析通过**——因为语法完全合法，
 * 只是运行时 ReferenceError。更糟的是它让 `init` 里后续的 `loadGuide()`
 * 永远不执行（使用说明一直"加载中"）。
 * 解析通过 ≠ 接线正确，所以要在静态阶段就把"调用了但没定义"找出来。
 *
 * 用法：node scripts/check_js_symbols.js
 */

"use strict";

const fs = require("fs");
const path = require("path");

// **自动发现**，不硬编码文件清单。
// 教训：这里原本写死 5 个文件，漏掉了 kb-pages.js 与 kb-nav.js ——
// 于是脚本一直"PASS"，而 kb-pages.js 里的 `infraRow` 其实从未定义，
// 直到浏览器打开设置页才报 ReferenceError。安全网自己有个洞最要命。
const WEBUI_DIR = "apps/webui/static/webui";
const FILES = fs
  .readdirSync(WEBUI_DIR)
  .filter((name) => name.endsWith(".js"))
  .sort()
  .map((name) => `${WEBUI_DIR}/${name}`);

/** 语言/宿主内置与全局对象：出现在调用位置也不算"未定义"。 */
const BUILTINS = new Set([
  "if", "for", "while", "switch", "catch", "return", "typeof", "new", "delete",
  "function", "await", "async", "else", "do", "try", "finally", "throw", "case",
  "of", "in", "void", "yield", "super", "this", "var", "let", "const",
  // 宿主/标准库
  "Array", "Object", "String", "Number", "Boolean", "Math", "JSON", "Date",
  "Promise", "Set", "Map", "WeakMap", "RegExp", "Error", "TypeError", "Symbol",
  "parseInt", "parseFloat", "isNaN", "isFinite", "encodeURIComponent",
  "decodeURIComponent", "setTimeout", "clearTimeout", "setInterval",
  "clearInterval", "requestAnimationFrame", "fetch", "alert", "confirm", "prompt",
  "console", "document", "window", "localStorage", "sessionStorage", "navigator",
  "FormData", "URL", "URLSearchParams", "Blob", "File", "FileReader", "Intl",
  "structuredClone", "queueMicrotask", "globalThis",
  // 本项目其它文件提供的全局
  "KB",
]);

/** 找定义：function 声明、const/let/var 赋值、类、以及形参（粗粒度）。 */
function collectDefined(source) {
  const defined = new Set();
  const patterns = [
    /\bfunction\s+([A-Za-z_$][\w$]*)/g,          // function foo(
    /\bclass\s+([A-Za-z_$][\w$]*)/g,             // class Foo
    /\b(?:const|let|var)\s+([A-Za-z_$][\w$]*)/g, // const foo =
    /\b([A-Za-z_$][\w$]*)\s*[:=]\s*(?:async\s*)?(?:function|\()/g, // foo: function / foo: (
  ];
  for (const pattern of patterns) {
    let match;
    while ((match = pattern.exec(source)) !== null) defined.add(match[1]);
  }
  // 形参与解构参数（避免把参数当未定义）
  const paramPatterns = [
    /\(([^)]*)\)\s*(?:=>|\{)/g,
    /\bfunction\s+[A-Za-z_$][\w$]*\s*\(([^)]*)\)/g,
    // **匿名**函数：`function (resolve) {`（Promise 参数就长这样，
    // 早期漏了这条，于是 resolve 被误报成"未定义"）
    /\bfunction\s*\(([^)]*)\)/g,
    // 箭头函数单参数无括号：`x => x + 1`
    /(?:^|[^\w$])([A-Za-z_$][\w$]*)\s*=>/g,
  ];
  for (const pattern of paramPatterns) {
    let match;
    while ((match = pattern.exec(source)) !== null) {
      match[1].split(",").forEach(function (raw) {
        const name = raw.trim().replace(/[.…\s].*$/, "").replace(/^\{|\}$/g, "").trim();
        if (/^[A-Za-z_$][\w$]*$/.test(name)) defined.add(name);
      });
    }
  }
  // catch(e) / for (const x of ...)
  let match;
  const catchPattern = /\bcatch\s*\(\s*([A-Za-z_$][\w$]*)/g;
  while ((match = catchPattern.exec(source)) !== null) defined.add(match[1]);
  return defined;
}

/** 找调用位置：identifier( ，排除属性访问 obj.method( 与被调用的关键字。 */
function collectCalls(source) {
  const calls = new Set();
  const pattern = /(^|[^\w$.])([A-Za-z_$][\w$]*)\s*\(/g;
  let match;
  while ((match = pattern.exec(source)) !== null) {
    const name = match[2];
    if (!BUILTINS.has(name)) calls.add(name);
  }
  return calls;
}

let problems = 0;
for (const relative of FILES) {
  const full = path.resolve(relative);
  if (!fs.existsSync(full)) {
    console.log("SKIP " + relative + "（不存在）");
    continue;
  }
  const source = fs.readFileSync(full, "utf8");
  try {
    new Function(source); // 先保证语法合法
  } catch (error) {
    console.log("FAIL " + relative + " 语法错误: " + error.message);
    problems += 1;
    continue;
  }
  // 去掉注释再做符号扫描：注释里写原型函数名（例如 renderOperation()）会误报。
  const scanned = source
    .replace(/\/\*[\s\S]*?\*\//g, " ")
    .replace(/(^|[^:])\/\/[^\n]*/g, "$1 ");
  const defined = collectDefined(scanned);
  const missing = [];
  for (const name of collectCalls(scanned)) {
    if (!defined.has(name)) missing.push(name);
  }
  if (missing.length) {
    console.log("FAIL " + relative + " 调用了但未定义: " + missing.join(", "));
    problems += 1;
  } else {
    console.log("PASS " + relative);
  }
}

// 额外：断言 init 里调用的每个函数都有定义（针对性防止"漏一段"）
const assistant = fs.readFileSync(path.resolve(FILES[0]), "utf8");
const initBlock = assistant.slice(assistant.lastIndexOf("(async function init()"));
const initCalls = [...initBlock.matchAll(/await\s+([A-Za-z_$][\w$]*)\s*\(/g)].map((m) => m[1]);
const assistantDefined = collectDefined(assistant);
const initMissing = initCalls.filter((name) => !assistantDefined.has(name));
if (initMissing.length) {
  console.log("FAIL init 调用了未定义函数: " + initMissing.join(", "));
  problems += 1;
} else {
  console.log("PASS init 的所有调用都有定义: " + initCalls.join(", "));
}

console.log(problems ? `\n${problems} 项失败` : "\n全部通过");
process.exit(problems ? 1 : 0);
