# 阶段 2：Qdrant 存储迁移方案（Windows bind mount → ext4）

> 独立审查条目 11：本文件此前**不存在**却被矩阵引用。现已补齐。
> **状态：脚本可运行，但业务数据尚未迁移**（等审查方执行）。

## 1. 根因（实测确证，非推测）

```
docker inspect knowledgebase-qdrant
  → bind  D:\AProjectScript\KnowledgeBase\vector_data\qdrant  →  /qdrant/storage (rw)
```

该目录 **36,441 个文件 / 69,960,930,229 字节（≈65.2 GiB）**、**183 个集合**，
底层是 DrvFs/9p（**非完整 POSIX**：mmap/flock/O_DIRECT 语义不全）。
Qdrant 读取 segment 时会 `unwrap()` panic（`OutputTooSmall`），重启还可能丢段。
参考 qdrant/qdrant#6758。

**所以不能**靠"逐库降级"或"原地重建一次"了事 —— 必须把数据放到真实 Linux 文件系统。

## 2. 已就绪的运行环境（Codex 准备并验证）

| 项 | 值 |
|---|---|
| 发行版 | `KnowledgeBase-Runtime`（WSL2） |
| 内核文件系统 | `findmnt /` → **`/dev/sdf ext4`** |
| 发行版 | Debian GNU/Linux 13 (trixie) |
| Qdrant | `/qdrant/qdrant --version` → **1.15.5**（与容器同版本，**本阶段不升级**） |
| 物理文件 | `vector_data/wsl-runtime/ext4.vhdx`（落在项目 root） |
| rootfs 来源 | 官方 `qdrant/qdrant:v1.15.5` 镜像导入（digest `0fb88974…`） |
| 网络实证 | 隔离 Qdrant 在 56339/56340，Windows localhost 与 Docker `host.docker.internal` 均 health 通过 |
| 持久性实证 | 仅重启该专属 distro，测试点（id=1/score=1）保留 |

## 3. 磁盘约束（关键，不可假设放得下）

| 项 | 值 |
|---|---|
| D: 剩余 | **49.34 GiB**（实测） |
| 数据逻辑大小 | **65.2 GiB** |
| 结论 | 可用 < 逻辑 → **禁止明文两次完整复制** |

策略：
- 冷备用 **压缩归档**（.NET ZipArchive Fastest，无新依赖），不复制明文副本；
- 迁移用 `cp -a --sparse=always`（零区不占实际分配）。
  独立探针实测：**33,579,008 字节 → 实际分配 24,576 字节，SHA 一致**；
- **全程监测剩余空间，保留 ≥5 GiB**，不足即中止并**保留旧目录**；
- 抽样零占比（最大 12 文件头/中/尾共 36 MiB，零字节 99.803%）**只是抽样**，
  **不能**据此断定总体一定放得下。

## 4. 脚本与安全红线

`scripts/phase2_storage.ps1`，默认**只打印计划**，`-Execute` 才动手。

| 红线 | 落实 |
|---|---|
| **绝不 `rm -rf`** | 目标为**全新唯一目录** `/var/lib/qdrant-gen2`；已存在且非空即 `Die` |
| 不拼危险命令 | 无 `Invoke-Expression`；`wsl` 原生参数调用 + 退出码判定 |
| 路径安全 | 绝对路径校验 + 危险字符拒绝（自检覆盖） |
| 冷备可验证 | 压缩归档后**抽样解压并逐文件 SHA256 比对**，不一致即失败 |
| 失败回退 | 健康检查失败自动停新实例、启旧容器；`rollback` 阶段手动可回退 |
| 旧数据不动 | 旧目录**自始至终不删除、不移动**；不删除任何集合 |
| 重启持久性 | 仅 `wsl --terminate` 该专属 distro，重启后比对集合数与查询 |

### 阶段

| 阶段 | 作用 |
|---|---|
| `preflight` | 只读体检：环境事实、数据规模、空间与稀疏抽样、目标目录守卫 |
| `backup` | 停写 → 记录集合清单 → **压缩冷备** → 解压校验 |
| `migrate` | 目标唯一空目录守卫 → 空间守卫 → 稀疏复制 → 文件数核对 → 写 config.yaml → 启动 → **Windows 侧健康轮询** |
| `verify` | 三条硬判据：`findmnt` 必须 ext4；集合清单一致；**重启后查询与集合数不变** |
| `rollback` | 停新实例、启旧容器 |
| `selftest` | **临时夹具自检**（不碰真实数据/服务） |

## 5. 夹具自检结果（已验证）

```
selftest 12/12 通过（未触碰真实数据与任何服务）
  PASS 接受绝对路径 / 拒绝危险字符路径
  PASS 非空目标目录会被拒绝
  PASS 余量不足判不安全
  PASS 压缩归档 1,114,112 → 71,322 字节（压缩比 6.4%）
  PASS 归档条目数 = 源文件数（12）
  PASS 解压后逐文件 SHA256 与原文件一致（12/12）
```

`preflight` 实测输出：`ext4 /dev/sdf`、Debian 13、qdrant 1.15.5、
65.2 GiB / 36,441 文件、D: 剩 49.34 GiB（判定禁止明文复制）、
目标目录 `absent`。

**夹具还抓到一个真实 bug 并已修**：`ZipArchive` 在 `System.IO.Compression`、
`ZipFileExtensions` 在 `System.IO.Compression.FileSystem`，
只加载后者会让**真实冷备在跑很久之后才失败**。

## 6. 尚未执行 / 尚未闭合

- `backup` / `migrate` / `verify` / `rollback` **尚未对真实 65 GiB 执行**
  （等审查方决定）；
- 压缩后实际占用**未知**，因此"是否放得下"仍无定论；
- `kb-9-g1` 等异常集合需迁移后以**新 generation** 重建（旧集合保留可回退）；
- 迁移需**停写窗口**，同一集合不可双写。
