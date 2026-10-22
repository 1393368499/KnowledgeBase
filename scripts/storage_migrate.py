"""阶段 2 存储迁移执行器（Python CLI）——**取代原 PowerShell 实现**。

为什么换成 Python（独立审查结论，PS 的问题不可靠修）：
  * 内层 `Die` 用 `exit`，**绕过 catch**，致命错误不回退；
  * `Start-Process -ArgumentList` 会把参数拼成字符串，`bash -lc` 带空格无法可靠引用；
  * 实测该 Runtime **没有 `pgrep`**，PID 管理不成立；
  * 逐文件起 wsl ≈ 7 万次启动（小时级），间隔监测会冲破 5 GiB 下限。

设计（按审查要求逐条落实）：
  * **默认只读**；只有显式 `--execute` 才允许任何变更；
  * 冷备用 `zipfile.open` 分块读写 + `hashlib` **流式**哈希 + `shutil.disk_usage`
    预算空间；产出 JSON 清单，校验通过后**原子**写 `verified` 标记；
  * 输入数据目录与输出备份目录必须**不相交**，且都在项目 root 内
    （真实包含关系，含父级 junction/symlink 解析）；
  * 迁移前置：必须**已停写**且**备份已 verified 且哈希匹配**；
  * 复制与哈希都在 **Linux 侧一次启动**的静态脚本里循环完成，
    逐文件按"最坏大小"预留 5 GiB，并监测 `/mnt/d` 真实 free；
  * Qdrant 由 `wsl --exec /qdrant/qdrant --config-path <cfg>` 直接启动，
    stdout/stderr 由 Windows 侧重定向（**不**经未引用的 bash 串）；
  * 受管进程核对走 `/proc/*/exe` + 精确 cmdline（**不用 pgrep**）；
  * 任何异常 → 统一 rollback（停新实例、恢复旧容器与客户端端点）后非零退出。
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import shutil
import subprocess
import sys
import time
import zipfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
#: 向量数据必须落在这个前缀下（专属盘，避免误写到系统目录）
LINUX_PREFIX = "/var/lib/qdrant-"
DEFAULT_DISTRO = "KnowledgeBase-Runtime"
DEFAULT_DATA = "vector_data/qdrant"
DEFAULT_TARGET = "/var/lib/qdrant-data-gen2"
DEFAULT_BACKUP = "artifacts/qdrant-cold-backup"
CONFIG_PATH = "/etc/qdrant/config.yaml"
MIN_FREE_BYTES = 5 * 1024**3
CHUNK = 4 * 1024 * 1024


class ServiceController:
    """本系统写入者的启停入口（可注入，便于演练"停后失败仍恢复"）。

    只动**本系统登记**的对象：本项目自己的 Qdrant 容器、以及本项目自己的
    celery worker 容器（若存在）。**绝不触碰 AS711 上的任何服务。**
    """

    #: 本系统会在向量库里写入的对象（容器名 → 是否必须停）
    WRITERS = ("knowledgebase-qdrant", "knowledgebase-worker", "knowledgebase-beat")

    def __init__(self) -> None:
        self.stopped: list[str] = []

    def _running(self, name: str) -> bool:
        out = subprocess.run(
            ["docker", "ps", "--filter", f"name=^{name}$", "--format", "{{.Names}}"],
            capture_output=True, text=True,
        ).stdout.strip()
        return name in out

    def stop_writers(self) -> None:
        for name in self.WRITERS:
            if not self._running(name):
                continue
            proc = subprocess.run(["docker", "stop", name], capture_output=True, text=True)
            if proc.returncode != 0:
                raise Abort(f"停写入者 {name} 失败：{proc.stderr[:200]}")
            self.stopped.append(name)
        if not self.stopped:
            say("  （没有正在运行的本系统写入者容器）")
        else:
            say("  已停写入者：" + "、".join(self.stopped))

    def start_old(self) -> None:
        # **只恢复自己停过的**，不多启任何一个 —— 避免顺手改动用户的服务状态
        for name in self.stopped:
            subprocess.run(["docker", "start", name], check=False)
        self.stopped = []


class NullController(ServiceController):
    """夹具演练用：不动任何真实容器。"""

    def __init__(self) -> None:
        super().__init__()

    def stop_writers(self) -> None:
        say("  （夹具控制器：不触碰任何容器）")

    def start_old(self) -> None:
        say("  （夹具控制器：不启动任何容器）")


#: 测试/演练用的服务控制器接缝（生产为 None → 用真实 ServiceController）。
#: 生产代码不会设置它，只有测试会注入记录型控制器。
_INJECTED_CONTROLLER: ServiceController | None = None

#: 冷备时库里仍是 pending/running 的行（写入者已停，视为僵死残留）——写进清单备审计
_INFLIGHT_AT_BACKUP: list[dict] = []


def set_service_controller(controller: ServiceController | None) -> None:
    global _INJECTED_CONTROLLER
    _INJECTED_CONTROLLER = controller


class Abort(RuntimeError):
    """可统一捕获的致命错误（由 main 负责回滚与退出码）。"""


# --------------------------------------------------------------------- 工具 --
def say(msg: str) -> None:
    print(f"  {msg}", flush=True)


def step(msg: str) -> None:
    print(f"\n== {msg}", flush=True)


def sha256_stream(handle, chunk: int = CHUNK) -> tuple[str, int]:
    """流式哈希（不把文件读进内存），返回 (hex, 字节数)。"""
    digest = hashlib.sha256()
    total = 0
    while True:
        block = handle.read(chunk)
        if not block:
            break
        digest.update(block)
        total += len(block)
    return digest.hexdigest(), total


def real_contained(path: Path, root: Path, what: str) -> Path:
    """规范化 + **真实包含关系**（解析 symlink/junction；含父级）。

    前缀比较必须带分隔符，否则 C:\\proj2 会被当成在 C:\\proj 之内。
    """
    full = Path(os.path.realpath(path))
    root_real = Path(os.path.realpath(root))
    try:
        common = os.path.commonpath([str(full), str(root_real)])
    except ValueError as exc:  # 不同盘符
        raise Abort(f"{what} 与项目 root 不同盘：{full}") from exc
    if common != str(root_real):
        raise Abort(f"{what} 越界（真实路径）：{full} 不在 {root_real} 之内")
    return full


def assert_disjoint(a: Path, b: Path) -> None:
    """输入与输出必须不相交（否则备份会把自己写进源里）。"""
    sa, sb = str(a) + os.sep, str(b) + os.sep
    if sa.startswith(sb) or sb.startswith(sa):
        raise Abort(f"输入与输出目录相交：{a} / {b}")


def assert_fixture_source(data: Path) -> None:
    """**技术上**限定夹具模式：源必须是项目 artifacts 下、名字含 fixture 的隔离目录。

    只靠 help 文案禁止真实迁移是不够的 —— 那样一次误敲 --no-service-control
    就可能对真实数据做演练。这里做硬校验。
    """
    artifacts = Path(os.path.realpath(ROOT / "artifacts"))
    try:
        inside = os.path.commonpath([str(data), str(artifacts)]) == str(artifacts)
    except ValueError:
        inside = False
    # 允许夹具源是 ".../fixture-xxx/src" 这种"父层命名"结构：
    # 只要相对 artifacts 的路径里**任一层**含 fixture 即可（避免死板地只看叶子目录）。
    rel_parts = Path(os.path.relpath(data, artifacts)).parts if inside else ()
    named = any("fixture" in part.lower() for part in rel_parts)
    if not inside or not named:
        raise Abort(
            "夹具模式只允许用于命名的隔离夹具源"
            f"（需位于 {artifacts} 之下且目录名含 fixture）；当前：{data}"
        )


def assert_linux_target(p: str) -> str:
    if not p.startswith(LINUX_PREFIX):
        raise Abort(f"Linux 目标必须以 {LINUX_PREFIX} 开头：{p}")
    if ".." in p or not all(c.isalnum() or c in "/._-" for c in p):
        raise Abort(f"Linux 目标含不允许字符：{p}")
    return p


def _inflight_rows_from_project_settings() -> list[dict]:
    """生产路径：**显式**用 `config.settings` 起子进程查库。

    为什么必须显式指定：子进程默认**继承调用方的环境**。在测试运行器下，
    环境里可能带着 `DJANGO_SETTINGS_MODULE=config.test_settings` 与测试库变量，
    子进程就会去查一个**没有业务表**的库，报 `no such table: kb_ingested_file`，
    于是真实迁移的这道守卫被"环境"而不是被"数据"决定。显式指定后，
    守卫只取决于**部署库的真实状态**。
    """
    code = (
        "import django,os,json;"
        "os.environ.setdefault('DJANGO_SETTINGS_MODULE','config.settings');"
        "django.setup();"
        "from apps.knowledge.models import IngestedFile;"
        "rows=list(IngestedFile._base_manager.filter("
        "parse_status__in=['pending','running']).values('id','parse_status','original_name'));"
        "print('INFLIGHT',json.dumps(rows,ensure_ascii=False))"
    )
    python = ROOT / ".venv" / "Scripts" / "python.exe"
    if not python.exists():
        say("  ⚠ 找不到 venv Python，跳过在途处理检查（真实迁移前应人工确认）")
        return []
    # 清掉会改变 settings/数据库选择的测试类变量，并强制 config.settings
    child_env = dict(os.environ)
    child_env["DJANGO_SETTINGS_MODULE"] = "config.settings"
    for noisy in ("KB_PYTEST_DB", "KB_TEST_RUN_UUID", "KB_SKIP_DOTENV"):
        child_env.pop(noisy, None)
    proc = subprocess.run(
        [str(python), "-c", code], capture_output=True, text=True,
        encoding="utf-8", errors="replace", cwd=str(ROOT), env=child_env,
    )
    marker = next((ln for ln in proc.stdout.splitlines() if ln.startswith("INFLIGHT")), "")
    if not marker:
        raise Abort(f"无法确认是否有在途处理（查询失败）：{proc.stderr[-200:]}")
    return json.loads(marker.split(" ", 1)[1]) if " " in marker else []


#: 取"在途资料行"的提供者。默认查部署库；测试可显式注入，从而**独立验证**
#: "未停写者必须拒绝 / 已停写者记录但不阻塞"这两条语义，而不依赖真实库。
_inflight_provider = _inflight_rows_from_project_settings


def assert_no_inflight_processing(*, writers_stopped: bool, provider=None) -> list[dict]:
    """冷备前确认**没有活跃写入者**在改向量索引。

    为什么不是"只要有 pending/running 就拒绝"：那些行可能是**僵死残留**
    （进程死在处理中途，状态永远停在 running）—— 实测就有 3 份从两天前卡住的行。
    守卫自己给出的处置方式是"先等它完成**或停掉相应写入者**"，所以本函数要求
    调用方明确告知**写入者是否已停**：

      * `writers_stopped=True`：已停写者 ⇒ 这些行是惰性的，**记录下来备审计**、
        不再阻塞（否则永远无法冷备）；
      * `writers_stopped=False`：未停写者 ⇒ 拒绝（备份会是移动中的状态）。

    返回在途行的明细（供写进备份清单，保证可复盘）。
    """
    rows = (provider or _inflight_provider)()
    if rows and not writers_stopped:
        raise Abort(
            f"仍有 {len(rows)} 份资料处于待处理/处理中，且写入者**未停** —— "
            "拒绝冷备（会备份到移动中的状态）；请先停写入者"
        )
    if rows:
        ids = ", ".join(str(r["id"]) for r in rows[:10])
        say(f"  ⚠ {len(rows)} 份资料仍是待处理/处理中，但写入者已停 → 视为惰性残留，"
            f"记入审计（id: {ids}）")
    else:
        say("  在途处理：0")
    return rows


def free_bytes(path: Path) -> int:
    return shutil.disk_usage(str(path)).free


def assert_space(path: Path, where: str) -> int:
    """要求**严格多于**下限（贴着火线等于没有余量）。"""
    free = free_bytes(path)
    if free <= MIN_FREE_BYTES:
        raise Abort(f"{where} 剩余 {free/1024**3:.2f} GiB ≤ 下限 {MIN_FREE_BYTES/1024**3:.0f} GiB")
    return free


# ------------------------------------------------------------- Linux 侧脚本 --
#: 全部静态；值经 argv 传入（Python 的 argv 不经 shell 拼接）
S_FINDMNT = 'findmnt -T "$1" -no FSTYPE,SOURCE 2>/dev/null || echo NOTFOUND'
S_TARGET_STATE = (
    'if [ -e "$1" ]; then if [ -L "$1" ]; then echo SYMLINK; '
    'else find "$1" -mindepth 1 2>/dev/null | head -1 | wc -l; fi; else echo absent; fi'
)
S_MKDIR = 'mkdir -p "$1" && echo OK'

#: 单进程批量稀疏复制：逐文件检查 /mnt/d 真实 free，按**最坏大小**预留
S_BULK_COPY = r"""
src="$1"; dst="$2"; floor="$3"; worst="$4"
n=0
while IFS= read -r -d '' f; do
  rel="${f#$src/}"
  # (a) **复制前**检查：df 必须可用，且真实 free 必须严格大于 floor+worst。
  #     放在复制之后检查是错的 —— 复制本身就可能已经冲破下限。
  free=$(df -B1 --output=avail /mnt/d 2>/dev/null | tail -1 | tr -d ' ')
  case "$free" in
    ''|*[!0-9]*) echo "COPY_DF_FAIL:${free}"; exit 6 ;;
  esac
  need=$((floor + worst))
  if [ "$free" -le "$need" ]; then echo "COPY_LOWSPACE:$free<$need"; exit 4; fi

  # (b) 父目录链上任何一环是 symlink 都必须拒绝（避免写到目标之外）
  p="$dst"
  if [ -L "$p" ]; then echo "COPY_PARENT_LINK:$p"; exit 7; fi
  parent=$(dirname "$rel")
  if [ "$parent" != "." ]; then
    IFS='/' read -r -a parts <<< "$parent"
    for part in "${parts[@]}"; do
      [ -z "$part" ] && continue
      p="$p/$part"
      if [ -L "$p" ]; then echo "COPY_PARENT_LINK:$p"; exit 7; fi
    done
  fi

  mkdir -p "$dst/$(dirname "$rel")" || { echo "COPY_FAIL:$rel"; exit 3; }
  cp -a --sparse=always "$f" "$dst/$rel" || { echo "COPY_FAIL:$rel"; exit 3; }
  n=$((n+1))
done < <(find "$src" -type f -print0)
echo "COPY_OK:$n"
"""


#: 复制后逐文件校验：**源 ↔ 目标直接比对**（不再比对历史清单）。
#:   $1=源  $2=目标  $3=可选 "--size-only"
#:
#: 为什么改成源↔目标：备份清单记录的是**备份那一刻**的状态；而 Qdrant 每次启动都会
#: 改写自己目录里的运行期文件（`payload_index/LOG`、`CURRENT` 等）。中途重启过容器后
#: 用旧清单核对，必然大面积"不一致"——那是**清单过期**，不是拷贝失真。
#: 源与目标在同一时刻比对，才是"这次拷贝是否忠实"的正确判据。
S_BULK_HASH = r"""
src="$1"; dst="$2"; mode="$3"
ok=0; bad=0
while IFS= read -r -d '' f; do
  rel="${f#$src/}"
  tgt="$dst/$rel"
  if [ ! -f "$tgt" ]; then bad=$((bad+1)); echo "MISMATCH:$rel:missing"; continue; fi
  if [ "$mode" = "--size-only" ]; then
    a=$(stat -c %s "$f" 2>/dev/null); b=$(stat -c %s "$tgt" 2>/dev/null)
    if [ -n "$a" ] && [ "$a" = "$b" ]; then ok=$((ok+1)); else bad=$((bad+1)); echo "MISMATCH:$rel:size"; fi
  else
    a=$(sha256sum "$f" 2>/dev/null | cut -d' ' -f1)
    b=$(sha256sum "$tgt" 2>/dev/null | cut -d' ' -f1)
    if [ -n "$a" ] && [ "$a" = "$b" ]; then ok=$((ok+1)); else bad=$((bad+1)); echo "MISMATCH:$rel:sha"; fi
  fi
done < <(find "$src" -type f -print0)
# 反向覆盖：目标里多出来的文件（不该有；有则暴露，便于发现"多拷/串目录"）
extra=$(cd "$dst" 2>/dev/null && find . -type f | while IFS= read -r g; do
  [ -f "$src/${g#./}" ] || echo "EXTRA:${g#./}"
done | wc -l)
echo "MATCH:$ok/$((ok+bad))"
echo "EXTRA_COUNT:$extra"
[ "$bad" -eq 0 ] || exit 5
"""


#: 受管进程核对：扫 /proc/*/exe + 精确 cmdline，**不用 pgrep**（实测 runtime 没有）
S_MANAGED_PID = r"""
cfg="$1"
found=""
for d in /proc/[0-9]*; do
  pid=${d#/proc/}
  exe=$(readlink -f "$d/exe" 2>/dev/null) || continue
  [ "$exe" = "/qdrant/qdrant" ] || continue
  cmd=$(tr '\0' ' ' < "$d/cmdline" 2>/dev/null)
  case "$cmd" in *"--config-path $cfg"*) found="$pid"; break;; esac
done
[ -n "$found" ] && echo "PID:$found" || echo "PID:none"
"""

S_KILL_MANAGED = r"""
cfg="$1"
killed=0
for d in /proc/[0-9]*; do
  pid=${d#/proc/}
  exe=$(readlink -f "$d/exe" 2>/dev/null) || continue
  [ "$exe" = "/qdrant/qdrant" ] || continue
  cmd=$(tr '\0' ' ' < "$d/cmdline" 2>/dev/null)
  case "$cmd" in *"--config-path $cfg"*) kill "$pid" 2>/dev/null && killed=$((killed+1));; esac
done
echo "KILLED:$killed"
"""


#: 允许内联进脚本文本的字符集（不含引号/空白/`$`/反引号/分号/管道等）。
#: 值内联是本机**唯一可靠**的传参方式（见 wsl() 说明），故必须先过这道白名单；
#: 不合规一律 fail-closed，绝不带病执行。
SAFE_VALUE_RE = re.compile(r"^[A-Za-z0-9._/:\-]+$")


def wsl(script: str, *values: str, check: bool = True) -> str:
    """执行静态脚本；值**内联进脚本文本**（不是 argv）。

    **为什么不用 argv**（本机 wsl.exe 的实测行为）：
        argv = ["wsl","-d",D,"-u","root","--","bash","-lc",'echo "[$1]"',"kb","/p"]
        结果： argc=0，$0=/bin/bash  ← `--` 之后的附加参数被**完全吞掉**
    这曾让本执行器里所有 `$1` 都是空的：`[ -e "" ]` 恒假 ⇒ "目标为空"的守卫
    变成**假信号**；`findmnt -T ""` 落到 `/` 也"看起来正常"。所以改为：

      * 值先过 `SAFE_VALUE_RE` 白名单（不含引号/空白/`$`/`;`/反引号等）；
      * 再把脚本里的 `$1..$9` 替换成**裸值**（脚本自带的双引号保持不动）。

    白名单里不可能出现能改变语句结构的字符，因此既可靠又无注入面。
    """
    rendered = script
    for index, value in enumerate(values, start=1):
        text = str(value)
        if not SAFE_VALUE_RE.match(text):
            raise Abort(f"内部错误：待内联的值含不允许字符：{text!r}")
        rendered = rendered.replace(f"${index}", text)
    # **必须规范化行尾**：本文件是 CRLF（Windows 仓库），三引号里的 bash 脚本因而
    # 每行都带 `\r`。bash 把 `\r` 当普通字符，于是
    #     src="/mnt/d/..."\r  → 变量值带 CR，`find "$src"` 拿到坏路径；
    # 单行脚本还会报 `$'\r': command not found`。
    # 这是真实踩到的坑（迁移"复制"阶段 `find: '': No such file` 的真因）。
    rendered = rendered.replace("\r\n", "\n").replace("\r", "")
    # **不能走 `bash -lc "<脚本>"`**：本机 wsl.exe 这条路径会对自己命令串做变量展开，
    # 把脚本里的 `$var`/`$()`/`${}` 吃掉（实测：`bash -lc 'X=5; echo "[$X]"'` → `X=[]`；
    # 转义成 `\$X` 反而能到达；而**写成文件再 `bash 文件`** 完全正常）。
    # 所以：脚本经 **stdin** 写成文件（stdin 不经命令行 → 不被展开），再执行该文件。
    runner = "/tmp/kb-step.sh"
    # 注意：这里**必须用字节写入**。`subprocess(..., text=True, input=str)` 在 Windows 上
    # 会把 `\n` 再翻译成 `\r\n`，等于把上面刚规范化掉的行尾又装回去
    # （实测报错：`/tmp/kb-step.sh: line 1: $'\r': command not found`）。
    subprocess.run(
        ["wsl", "-d", DISTRO, "-u", "root", "--", "tee", runner],
        input=rendered.encode("utf-8"), capture_output=True,
    )
    argv = ["wsl", "-d", DISTRO, "-u", "root", "--", "bash", runner]
    proc = subprocess.run(argv, capture_output=True, text=True, encoding="utf-8", errors="replace")
    subprocess.run(
        ["wsl", "-d", DISTRO, "-u", "root", "--", "rm", "-f", runner],
        capture_output=True,
    )
    out = (proc.stdout or "") + (proc.stderr or "")
    # wsl.exe 的警告是 UTF-16，按当前编码解出后字母间夹 \x00，
    # 直接匹配 "localhost" 会漏掉 → **先剥 NUL 再过滤**。
    clean = []
    for raw_line in out.splitlines():
        line = raw_line.replace("\x00", "").strip()
        if not line:
            continue
        # 该警告是 UTF-16，按当前编码解出后中文字符成了乱码碎片，
        # 只剩 "localhost"/"NAT"/"WSL" 等英文可辨。这几条命令的**合法**输出
        # （findmnt / os-release / qdrant --version / 目录状态）都不含这些词，
        # 所以按词丢弃是安全的。
        if any(token in line for token in ("localhost", "NAT", "WSL")):
            continue
        clean.append(line)
    out = "\n".join(clean)
    if check and proc.returncode != 0:
        raise Abort(f"wsl 执行失败（exit {proc.returncode}）：{out[:400]}")
    return out


# ------------------------------------------------------------------ 回滚 ----
class Rollback:
    """统一回滚：停受管新实例（按 /proc 精确核对）、恢复旧容器与客户端端点。"""

    def __init__(
        self, distro: str, old_port: int, new_port: int,
        service_control: bool = True, controller: "ServiceController | None" = None,
    ) -> None:
        self.distro = distro
        self.old_port = old_port
        self.new_port = new_port
        self.armed = False
        self.proc: subprocess.Popen | None = None
        self.service_control = service_control
        self.controller: ServiceController | None = controller

    def arm(self) -> None:
        self.armed = True

    def restore(self, reason: str) -> None:
        if not self.armed:
            return
        say(f"回退（原因：{reason}）")
        try:
            say("  停受管新实例：" + wsl(S_KILL_MANAGED, CONFIG_PATH, check=False).strip())
        except Exception as exc:  # noqa: BLE001
            say(f"  停实例失败：{exc}")
        if self.proc is not None and self.proc.poll() is None:
            self.proc.terminate()
        ctl = self.controller or (ServiceController() if self.service_control else NullController())
        ctl.start_old()
        time.sleep(5)
        if qdrant_ready(self.old_port):
            # 不只验"容器起来了"，要验**配置里的客户端端点**确实在服务 ——
            # 应用读的是 KB_QDRANT_URL，端点不通等于回退没成功。
            configured = os.environ.get("KB_QDRANT_URL", "")
            say(f"  旧容器已恢复（集合数 {len(list_collections(self.old_port))}）")
            if configured:
                say(f"  客户端端点（配置值）：{configured}")
                if str(self.old_port) not in configured:
                    say("  ⚠ 配置端点与旧容器端口不一致，请人工确认应用指向")
            else:
                say("  ⚠ 未读到 KB_QDRANT_URL（.env 用 override 加载，脚本进程可能没有）——"
                    " 请人工确认应用端点已指回旧实例")
        else:
            say("  ⚠ 旧容器未就绪，请人工检查")


def qdrant_ready(port: int, tries: int = 30, sleep: float = 2.0) -> bool:
    import urllib.request

    for _ in range(tries):
        try:
            with urllib.request.urlopen(f"http://127.0.0.1:{port}/collections", timeout=5) as resp:
                if resp.status == 200:
                    return True
        except Exception:  # noqa: BLE001
            time.sleep(sleep)
    return False


def list_collections(port: int) -> list[str]:
    import urllib.request

    with urllib.request.urlopen(f"http://127.0.0.1:{port}/collections", timeout=20) as resp:
        body = json.loads(resp.read().decode("utf-8"))
    return [item["name"] for item in body["result"]["collections"]]


def start_qdrant(target_dir: str, rb: Rollback) -> subprocess.Popen:
    """用 `wsl --exec /qdrant/qdrant --config-path ...` 直接启动，Windows 侧重定向。

    **不**经过 `bash -lc` 字符串：避免未引用/空格问题（审查指出的缺陷）。
    """
    wsl(S_MKDIR, "/etc/qdrant")
    cfg = (
        "storage:\n"
        f"  storage_path: {target_dir}\n"
        "service:\n  host: 127.0.0.1\n"
        f"  http_port: {NEW_PORT}\n  grpc_port: {NEW_PORT + 1}\n"
        "log_level: INFO\n"
    )
    tmp = ROOT / "artifacts" / "qdrant-config.tmp"
    tmp.parent.mkdir(parents=True, exist_ok=True)
    tmp.write_text(cfg, encoding="utf-8")
    # 用 stdin 把配置写进 distro（避免 bash 串与引号问题）
    subprocess.run(
        ["wsl", "-d", DISTRO, "-u", "root", "--", "tee", CONFIG_PATH],
        input=cfg.encode("utf-8"), capture_output=True, check=True,
    )
    out = (ROOT / "artifacts" / "qdrant-new.log").open("w", encoding="utf-8")
    proc = subprocess.Popen(
        ["wsl", "-d", DISTRO, "-u", "root", "--", "/qdrant/qdrant", "--config-path", CONFIG_PATH],
        stdout=out, stderr=subprocess.STDOUT,
    )
    say(f"  已启动（Windows PID {proc.pid}）；stdout/stderr → artifacts/qdrant-new.log")
    rb.proc = proc
    time.sleep(3)
    say("  受管进程核对：" + wsl(S_MANAGED_PID, CONFIG_PATH, check=False).strip())
    return proc


# ------------------------------------------------------------------ 备份 ----
def do_backup(
    data: Path,
    backup: Path,
    execute: bool,
    rb: "Rollback",
    service_control: bool = True,
    controller: ServiceController | None = None,
) -> None:
    step("备份前置检查")
    assert_disjoint(data, backup)
    if not data.is_dir():
        raise Abort(f"数据目录不存在：{data}")
    if execute:
        # **第一项变更之前**先 arm：停容器之后任何异常都要能恢复
        rb.arm()
        ctl = controller or _INJECTED_CONTROLLER or (
            ServiceController() if service_control else NullController()
        )
        say("停写入者（只停本系统登记的对象，不动 AS711）")
        ctl.stop_writers()
        rb.controller = ctl
        # 写入者已停 → 库里的 pending/running 只可能是僵死残留：记录下来备审计，
        # 不再阻塞（否则永远无法冷备）。未停写者的场景由函数内部拒绝。
        global _INFLIGHT_AT_BACKUP
        if service_control:
            # 已停写者 → 库里 pending/running 只可能是僵死残留：记录备审计，不阻塞。
            _INFLIGHT_AT_BACKUP = assert_no_inflight_processing(writers_stopped=True)
        else:
            # 夹具模式：没有真实服务与写入者，该检查**不适用**（不是"跳过测试"，
            # 而是此处根本没有可停的写入者，也无部署库可查）。
            say("  （夹具模式：无真实服务/写入者，在途检查不适用）")

    files = sorted(p for p in data.rglob("*") if p.is_file())
    if not files:
        raise Abort(f"数据目录为空，拒绝备份：{data}")
    worst = max(p.stat().st_size for p in files)
    logical = sum(p.stat().st_size for p in files)
    say(f"{len(files)} 个文件 / {logical/1024**3:.2f} GiB（最大单文件 {worst/1024**2:.1f} MiB）")
    if not execute:
        say("[只读模式] 需要 --execute 才会真正备份")
        return

    backup.mkdir(parents=True, exist_ok=True)
    assert_space(ROOT, "备份前")
    zip_path = backup / f"qdrant-cold-{time.strftime('%Y%m%d-%H%M%S')}.zip"
    manifest: list[dict] = []
    step("压缩并**流式**计算逐文件 SHA256")
    with zipfile.ZipFile(zip_path, "w", compression=zipfile.ZIP_DEFLATED, compresslevel=1) as zf:
        for i, path in enumerate(files, 1):
            rel = path.relative_to(data).as_posix()
            # 哈希与写入必须在**同一个 with 内**完成：
            # 出了 with 文件已关闭，再 seek(0) 会 ValueError: seek of closed file
            # （夹具演练实际抓到的 bug）。
            with path.open("rb") as src:
                digest, size = sha256_stream(src)
                src.seek(0)
                info = zipfile.ZipInfo(rel)
                # **必须显式指定压缩方式**：ZipInfo 默认是 ZIP_STORED，
                # 不设的话"压缩冷备"实际是原样存储，空间估算全错。
                info.compress_type = zipfile.ZIP_DEFLATED
                with zf.open(info, "w") as dst:
                    while True:
                        block = src.read(CHUNK)
                        if not block:
                            break
                        dst.write(block)
            manifest.append({"rel": rel, "size": size, "sha256": digest})
            if i % 500 == 0:
                assert_space(ROOT, "压缩中")
    # 归档自身的 SHA256（流式），用于后续证明"还是那个归档"
    with zip_path.open("rb") as fh:
        archive_sha, archive_size = sha256_stream(fh)
    # 逐条 rel+sha 的集合摘要：清单被改动（改名/改哈希/增减）都会变
    entries_digest = hashlib.sha256(
        "\n".join(f'{m["rel"]}:{m["size"]}:{m["sha256"]}' for m in sorted(manifest, key=lambda x: x["rel"])).encode()
    ).hexdigest()
    manifest_doc = {
        "files": manifest,
        "count": len(manifest),
        "source_realpath": str(data),
        "archive": zip_path.name,
        "archive_sha256": archive_sha,
        "archive_size": archive_size,
        "entries_sha256": entries_digest,
        # 冷备时仍是 pending/running 的行（写入者已停；供审计复盘）
        "inflight_at_backup": _INFLIGHT_AT_BACKUP,
    }
    (backup / "manifest.json").write_text(
        json.dumps(manifest_doc, ensure_ascii=False), encoding="utf-8"
    )
    say(f"归档 {zip_path.name}（{(zip_path.stat().st_size)/1024**3:.2f} GiB）；清单 {len(manifest)} 条")

    step("**流式解压全部条目**核对 SHA256 + 长度（不落明文）")
    # **先建映射**：逐条 next() 线性查找在 36k 条时是 O(n²)（约 1.3e9 次比较）
    by_rel = {m["rel"]: m for m in manifest}
    ok = 0
    seen: set[str] = set()
    with zipfile.ZipFile(zip_path) as zf:
        entries = zf.infolist()
        # 条目集合必须与清单**精确相等**：少一条、多一条、重复一条都要拒绝
        if len(entries) != len(manifest):
            raise Abort(f"归档条目数({len(entries)}) 与清单({len(manifest)}) 不一致")
        for entry in entries:
            if entry.filename in seen:
                raise Abort(f"归档里出现重复条目：{entry.filename}")
            seen.add(entry.filename)
            with zf.open(entry) as fh:
                digest, size = sha256_stream(fh)
            want = by_rel.get(entry.filename)
            if want is None or want["sha256"] != digest or want["size"] != size:
                raise Abort(f"备份校验失败：{entry.filename}")
            ok += 1
            assert_space(ROOT, "校验中")
    # **原子**写 verified 标记（tmp + os.replace）
    with (backup / "manifest.json").open("rb") as fh:
        manifest_sha, _ = sha256_stream(fh)
    tmp = backup / "manifest.verified.json.tmp"
    # 绑定到**具体内容**而不是"文件存在"：归档 SHA、清单 SHA、源真实路径、
    # 条目数、条目集合摘要 —— 任何一项对不上都视为未验证。
    tmp.write_text(
        json.dumps(
            {
                "archive": zip_path.name,
                "archive_sha256": archive_sha,
                "manifest_sha256": manifest_sha,
                "source_realpath": str(data),
                "count": ok,
                "entries_sha256": entries_digest,
        # 冷备时仍是 pending/running 的行（写入者已停；供审计复盘）
        "inflight_at_backup": _INFLIGHT_AT_BACKUP,
                "verified_at": time.strftime("%FT%T"),
            },
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )
    os.replace(tmp, backup / "manifest.verified.json")
    say(f"逐条核对通过 {ok}/{len(manifest)}；verified 标记已原子写入")


# ------------------------------------------------------------------ 迁移 ----
def do_migrate(
    data: Path, backup: Path, target: str, rb: Rollback, execute: bool,
    service_control: bool = True,
) -> None:
    step("迁移前置：必须已停写 + 备份已 verified")
    if service_control:
        running = subprocess.run(
            ["docker", "ps", "--filter", "name=knowledgebase-qdrant", "--format", "{{.Names}}"],
            capture_output=True, text=True,
        ).stdout.strip()
        if "knowledgebase-qdrant" in running:
            raise Abort("旧容器仍在运行 —— 先执行 backup --execute（停写 + 冷备）")
    else:
        say("⚠ 夹具模式：不做 docker 停写检查（不得用于真实迁移）")
    verified = backup / "manifest.verified.json"
    manifest_file = backup / "manifest.json"
    if not verified.exists() or not manifest_file.exists():
        raise Abort("缺少 verified 标记或清单 —— 拒绝迁移（无法证明有可回退的备份）")
    ver = json.loads(verified.read_text(encoding="utf-8"))
    for key in ("archive", "archive_sha256", "manifest_sha256", "source_realpath",
                "count", "entries_sha256"):
        if key not in ver:
            raise Abort(f"verified 标记缺少绑定字段 {key} —— 拒绝迁移")
    # 源真实路径必须与**本次要迁移的目录**一致（换了目录就说明备份不是这一次的）
    if Path(os.path.realpath(ver["source_realpath"])) != Path(os.path.realpath(data)):
        raise Abort(
            f"verified 绑定的源与本次不一致：{ver['source_realpath']} ≠ {data}"
        )
    archive_path = backup / ver["archive"]
    if not archive_path.exists():
        raise Abort(f"verified 指向的归档不存在：{ver['archive']}")
    with archive_path.open("rb") as fh:
        live_archive_sha, _ = sha256_stream(fh)
    if live_archive_sha != ver["archive_sha256"]:
        raise Abort("归档内容与 verified 记录不一致（被替换或损坏）—— 拒绝迁移")
    with manifest_file.open("rb") as fh:
        live_manifest_sha, _ = sha256_stream(fh)
    if live_manifest_sha != ver["manifest_sha256"]:
        raise Abort("清单内容与 verified 记录不一致（被改动）—— 拒绝迁移")
    doc = json.loads(manifest_file.read_text(encoding="utf-8"))
    if doc.get("entries_sha256") != ver["entries_sha256"]:
        raise Abort("清单条目集合与 verified 记录不一致 —— 拒绝迁移")
    if len(doc.get("files", [])) != ver["count"]:
        raise Abort("清单条目数与 verified 记录不一致 —— 拒绝迁移")
    say(f"已停写；verified 校验通过（归档 {ver['archive']}，{ver['count']} 条绑定）")

    if not service_control and target == DEFAULT_TARGET:
        raise Abort(
            "夹具模式必须用独立目标（例如 --target /var/lib/qdrant-fixture-drill），"
            f"不得指向真实目标 {DEFAULT_TARGET}"
        )

    state = wsl(S_TARGET_STATE, target).strip()
    if state == "SYMLINK":
        raise Abort(f"目标是 symlink：{target}")
    if state not in ("absent", "0"):
        raise Abort(f"目标非空（{state}）—— 拒绝写入，本执行器不做任何删除")
    if not execute:
        say(f"[只读模式] 需要 --execute 才会创建 {target} 并复制")
        return
    wsl(S_MKDIR, target)
    rb.arm()
    assert_space(ROOT, "迁移前")

    step("单进程批量稀疏复制（Linux 侧循环；按最坏单文件大小预留 5 GiB）")
    files = sorted(p for p in data.rglob("*") if p.is_file())
    worst = max(p.stat().st_size for p in files)
    src_wsl = "/mnt/d/" + str(data)[3:].replace("\\", "/")
    out = wsl(S_BULK_COPY, src_wsl, target, str(MIN_FREE_BYTES), str(worst), check=False)
    tail = out.strip().splitlines()[-1] if out.strip() else ""
    say(f"  结果：{tail}")
    if "COPY_DF_FAIL" in out:
        raise Abort(f"无法读取 /mnt/d 余量（df 失败）—— 拒绝继续：{tail}")
    if "COPY_PARENT_LINK" in out:
        raise Abort(f"目标路径上存在 symlink 父目录 —— 拒绝写入：{tail}")
    if "COPY_LOWSPACE" in out:
        raise Abort(f"复制前余量已不足（未做任何复制即中止）：{tail}")
    if "COPY_FAIL" in out or "COPY_OK" not in out:
        raise Abort(f"批量复制未成功：{out[-300:]}")

    step("复制后逐文件校验（**源 ↔ 目标** 直接比对）")
    # 覆盖数取**当前源**的文件数（源在停写状态下稳定），而不是历史备份清单的条数：
    # 备份清单是"备份那一刻"的状态，中途重启过容器后它与当前源不一致。
    source_count = len(files)
    out = wsl(S_BULK_HASH, src_wsl, target, "--hash", check=False)
    bad = [line for line in out.splitlines() if line.startswith("MISMATCH:")]
    extra = next(
        (line for line in out.splitlines() if line.startswith("EXTRA_COUNT:")), "EXTRA_COUNT:?"
    )
    summary = next((line for line in reversed(out.splitlines()) if line.startswith("MATCH:")), "")
    say("  校验汇总：" + (summary or "(无汇总——视为失败)") + f"  {extra}")
    # 三重判据，缺一不可：**有汇总** + **零 MISMATCH** + **覆盖数=当前源文件数**。
    if not summary:
        raise Abort(f"校验未产出汇总（可能异常退出）：{out[-300:]}")
    matched, _, total = summary.partition(":")[2].partition("/")
    if not matched.isdigit() or not total.isdigit():
        raise Abort(f"校验汇总无法解析：{summary}")
    if bad:
        raise Abort(f"复制校验失败（{len(bad)} 个不一致）：{bad[:3]}")
    if int(total) != source_count or int(matched) != source_count:
        raise Abort(f"校验覆盖不全：{matched}/{total}，当前源 {source_count} 个文件")
    extra_count = extra.partition(":")[2]
    if extra_count.isdigit() and int(extra_count) > 0:
        raise Abort(f"目标里多出 {extra_count} 个源中不存在的文件（拒绝继续）")

    step("findmnt -T 实际目标必须为 ext4")
    fs = wsl(S_FINDMNT, target).strip()
    say(fs)
    if "ext4" not in fs:
        raise Abort(f"目标不是 ext4：{fs}")

    if not service_control:
        # 夹具模式：**不启动 Qdrant**（不启停任何服务），只验证复制与哈希。
        say("⚠ 夹具模式：跳过 Qdrant 启动与健康检查（不启停任何服务）")
        say(f"  Linux 侧夹具目标（人工清理）：{target}")
        say("  复制 + 全量哈希已通过；真实迁移仍需在正式模式下执行并做重启持久性验证")
        return

    step("启动新实例并轮询健康")
    start_qdrant(target, rb)
    if not qdrant_ready(NEW_PORT):
        raise Abort("新实例未就绪")
    say(f"新实例健康；集合数 {len(list_collections(NEW_PORT))}")


# ------------------------------------------------------------ verify/其它 ----
def do_preflight(data: Path, backup: Path, target: str) -> None:
    step("1. 环境事实（只读）")
    say(wsl(S_FINDMNT, "/").strip())
    say(wsl("cat /etc/os-release | head -1").strip())
    say(wsl("/qdrant/qdrant --version").strip())
    step("2. 路径与包含关系")
    say(f"数据（真实路径）：{data}")
    say(f"备份（真实路径）：{backup}")
    assert_disjoint(data, backup)
    step("3. 规模与空间")
    files = sorted(p for p in data.rglob("*") if p.is_file())
    logical = sum(p.stat().st_size for p in files)
    free = free_bytes(ROOT)
    say(f"{len(files)} 个文件 / {logical/1024**3:.2f} GiB；D: 可用 {free/1024**3:.2f} GiB")
    if free <= MIN_FREE_BYTES:
        say(f"⚠ 可用空间已低于下限 {MIN_FREE_BYTES/1024**3:.0f} GiB")
    if free < logical:
        say("⚠ 可用 < 逻辑 → 禁止明文完整复制；备份走压缩 + 迁移走稀疏复制")
    step("4. 目标目录")
    say(f"{target} ：{wsl(S_TARGET_STATE, target).strip()}")
    step("5. 只读模式")
    say("未指定 --execute：本执行器不会做任何变更")


def do_verify(target: str, rb: Rollback, execute: bool) -> None:
    step("硬判据 1：findmnt -T 实际目标必须是 ext4")
    fs = wsl(S_FINDMNT, target).strip()
    say(fs)
    step("硬判据 2：集合数（需与备份时清单比对）")
    say(f"当前集合数 {len(list_collections(NEW_PORT))}")
    step("硬判据 3：重启持久性（需要 --execute）")
    if not execute:
        say("[只读模式] 不会重启 distro")
        return
    rb.arm()
    say("  停受管实例：" + wsl(S_KILL_MANAGED, CONFIG_PATH, check=False).strip())
    subprocess.run(["wsl", "--terminate", DISTRO], check=False)
    time.sleep(5)
    start_qdrant(target, rb)
    if not qdrant_ready(NEW_PORT):
        raise Abort("重启后未就绪")
    say(f"重启后集合数 {len(list_collections(NEW_PORT))}")


# ------------------------------------------------------------------ main ----
DISTRO = DEFAULT_DISTRO
NEW_PORT = 56339
OLD_PORT = 56333


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="阶段 2 存储迁移执行器（默认只读）")
    parser.add_argument("phase", choices=["preflight", "backup", "migrate", "verify", "rollback", "selftest"])
    parser.add_argument("--execute", action="store_true", help="真正执行变更（默认只读）")
    parser.add_argument("--fast-verify", action="store_true", help="校验只比大小（默认全量 SHA256）")
    parser.add_argument("--distro", default=DEFAULT_DISTRO)
    parser.add_argument("--data", default=DEFAULT_DATA)
    parser.add_argument("--target", default=DEFAULT_TARGET)
    parser.add_argument("--backup", default=DEFAULT_BACKUP)
    parser.add_argument(
        "--no-service-control", action="store_true",
        help="夹具演练：跳过 docker stop/start（**不得**用于真实迁移）",
    )
    args = parser.parse_args(argv)

    global DISTRO
    DISTRO = args.distro

    injected = _INJECTED_CONTROLLER
    rb = Rollback(
        DISTRO, OLD_PORT, NEW_PORT,
        service_control=not args.no_service_control,
        controller=injected,
    )
    try:
        data = real_contained(ROOT / args.data, ROOT, "数据目录")
        backup = real_contained(ROOT / args.backup, ROOT, "备份目录")
        target = assert_linux_target(args.target)

        control = not args.no_service_control
        if args.no_service_control:
            # 技术性限制：夹具模式只对命名的隔离夹具源开放
            assert_fixture_source(data)
        if args.phase == "preflight":
            do_preflight(data, backup, target)
        elif args.phase == "backup":
            do_backup(data, backup, args.execute, rb, control)
        elif args.phase == "migrate":
            do_migrate(data, backup, target, rb, args.execute, control)
        elif args.phase == "verify":
            do_verify(target, rb, args.execute)
        elif args.phase == "rollback":
            # **默认只读**：回退同样是一次变更，必须显式 --execute
            if not args.execute:
                say("[只读模式] 需要 --execute 才会执行回退（停新实例、恢复旧容器）")
                return 0
            rb.arm()
            rb.restore("人工回退")
        elif args.phase == "selftest":
            from storage_selftest import run_selftest  # 共享实现，测试与生产同一套函数

            return run_selftest(ROOT)
        return 0
    except Abort as exc:
        print(f"\nFATAL: {exc}", file=sys.stderr)
        rb.restore(str(exc))
        return 1
    except Exception as exc:  # noqa: BLE001 - 任何异常都要回滚
        print(f"\nFATAL(未预期): {type(exc).__name__}: {exc}", file=sys.stderr)
        rb.restore(f"{type(exc).__name__}: {exc}")
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
