"""生成一套**合规**的负载测试参数，并打印可直接复制的命令。

fail-closed 的配套工具：``config/loadtest_settings`` 缺参数就拒绝启动，
这个脚本负责**自动生成**满足全部约束的一套（独立库名、`kb-lt-` 前缀、独立 Redis DB），
省得人手写错。它自身不做任何修改，只打印。

用法::

    .\\.venv\\Scripts\\python.exe scripts/loadtest_env.py
    .\\.venv\\Scripts\\python.exe scripts/loadtest_env.py --json
"""

from __future__ import annotations

import argparse
import json
import os
import secrets
import sys
from pathlib import Path

BUSINESS_DB = os.environ.get("KB_LOADTEST_FORBIDDEN_DB", "kb")
BUSINESS_PREFIX = os.environ.get("KB_LOADTEST_FORBIDDEN_PREFIX", "kb")
BUSINESS_BROKER_DB = os.environ.get("KB_LOADTEST_FORBIDDEN_BROKER_DB", "0")
REDIS_PORT = os.environ.get("KB_LOADTEST_REDIS_PORT", "56379")


def build(suffix: str, broker_db: str, port: str, run_id: str = "") -> dict[str, str]:
    """生成隔离压测环境。

    **向量前缀改为与 run-id 绑定**：`kb-<run_id>`。以前是共享的
    `kb-lt-<suffix>` —— 它不属于任何 run，清理器按设计拒绝碰它，
    正是历史 42 GiB 里 125 个 `kb-lt-*` 集合清不掉的成因。
    现在同一个 run-id 既用于启动测试服务，也用于 `load_probe --run-id`，
    于是客户端能**精确**回收本次压测建出的集合。
    """
    rid = run_id or suffix
    # 本 run 的隔离目录：压测上传的原件必须落这里，**不能**写主 media/raw。
    run_root = (
        Path(__file__).resolve().parent.parent / "artifacts" / "test-runs" / rid
    )
    return {
        "KB_LOADTEST_RUN_ID": rid,
        "KB_LOADTEST_DB_NAME": f"kb_loadtest_{suffix}",
        "KB_LOADTEST_QDRANT_PREFIX": f"kb-{rid}",
        # 端点显式给出，避免客户端/服务各自被 .env 带到主实例 56333
        "KB_LOADTEST_QDRANT_URL": os.environ.get(
            "KB_TEST_QDRANT_URL", "http://127.0.0.1:56339"
        ).strip(),
        # 隔离目录：loadtest_settings 会强制绑定并校验
        "KB_LOADTEST_RAW_STORE_DIR": str(run_root / "raw"),
        "KB_LOADTEST_VECTOR_DATA_DIR": str(run_root / "vector_data"),
        "KB_LOADTEST_BROKER_DB": broker_db,
        "KB_LOADTEST_LABEL": f"loadtest-{suffix}",
        "KB_LOADTEST_REDIS_PORT": port,
    }


def main(argv: list[str]) -> int:
    parser = argparse.ArgumentParser(description="生成合规的负载测试环境参数")
    parser.add_argument("--suffix", default=secrets.token_hex(4), help="随机后缀（库名用）")
    parser.add_argument(
        "--run-id",
        # **必须默认合法**：load_probe 严格要求 12/32 位小写 hex。
        # 以前默认是 token_hex(4)=8 位且直接当 rid，导致"默认生成永远非法"，
        # 用户照抄打印出来的命令必然被 load_probe 拒绝。
        default=secrets.token_hex(6),
        help="预生成的 run-id（**12 位小写 hex**）。同一个值要传给 load_probe --run-id，"
             "否则前缀对不上（这是设计上的强绑定，不是缺陷）",
    )
    parser.add_argument(
        "--broker-db",
        default="14",
        help=f"独立 Redis DB（不能是业务的 {BUSINESS_BROKER_DB}）",
    )
    parser.add_argument("--port", default=REDIS_PORT, help="测试 Redis 端口")
    parser.add_argument("--json", action="store_true")
    args = parser.parse_args(argv)

    if args.broker_db == BUSINESS_BROKER_DB:
        print(
            f"拒绝：broker DB 不能是业务 DB {BUSINESS_BROKER_DB}",
            file=sys.stderr,
        )
        return 2

    env = build(args.suffix, args.broker_db, args.port, args.run_id)

    if args.json:
        print(json.dumps(env, ensure_ascii=False, indent=2))
        return 0

    print("# ---- 1) 设置环境变量（PowerShell）----")
    for key, value in env.items():
        print(f'$env:{key} = "{value}"')
    print('$env:DJANGO_SETTINGS_MODULE = "config.loadtest_settings"')
    print()
    print("# ---- 2) 建库并迁移（只影响上面这套隔离目标）----")
    print("#   先在容器里建库：")
    print(
        f'#   docker exec knowledgebase-postgres psql -U kb -c "CREATE DATABASE '
        f'{env["KB_LOADTEST_DB_NAME"]};"'
    )
    print('.\\.venv\\Scripts\\python.exe manage.py migrate --no-input')
    print()
    print("# ---- 3) 起测试服务（不要用 8000——那是真实模型配置）----")
    print('.\\.venv\\Scripts\\python.exe manage.py runserver 127.0.0.1:8011 --noreload')
    print()
    print("# ---- 4) 压测（脚本会先校验 /guide/ 的 model_calls_disabled 与 load_test_mode）----")
    print('$env:KB_LOAD_BASE = "http://127.0.0.1:8011"')
    # 客户端必须拿到**本 run 的端点与 run-id**：否则它既对不上服务自述前缀，
    # 也无法在收尾时精确回收（以前这里没设，用户照抄就跑不起来）。
    print(f'$env:KB_QDRANT_URL = "{env["KB_LOADTEST_QDRANT_URL"]}"')
    print(f'$env:KB_LOAD_RUN_ID = "{env["KB_LOADTEST_RUN_ID"]}"')
    print('$env:KB_LOAD_USER = "<该库里的压测账号>"')
    print('$env:KB_LOAD_PASSWORD = "<口令>"')
    print('$env:KB_LOAD_LIBRARY = "load-test-main"   # 必须 load-test- 前缀')
    print(
        f'.\\.venv\\Scripts\\python.exe scripts/load_probe.py upload '
        f'--concurrency 4 --files 5 --run-id {env["KB_LOADTEST_RUN_ID"]}'
    )
    print()
    print("# ---- 5) 收尾：**自动清理**（由 load_probe 的 finally 完成，无需手工）----")
    print("#   load_probe 结束时会：停自己起的服务/worker → 只删本 run 前缀的集合 →")
    print("#   清本 run 的 raw/vector_data → 写小型报告到 artifacts/test-runs/<run_id>/")
    print("#   清理失败会让入口**非零退出**并在报告里列出残留项（不会只报'向量已删'就算全清）。")
    print("#   真实 DB 与只读原件的回收由独占测试运行器负责（见 docs/cleanup-acceptance-steps.md）。")
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
