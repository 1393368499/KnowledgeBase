# 交付用应用镜像（可复现构建）
#
# ============================ 两条构建路径 ============================
#
# 【路径 A｜交付路径：缓存基础增量构建（快）】
#   基座：`kb-worker:phase2-review-20261015`
#   期望的**本地 image ID**：sha256:63c6532ef74939dee3bb9ceca4abfdc5639ffbcf454c3568774096ba9e4bdea8
#
#     pwsh -File deploy/build-app.ps1        # 会先校验 ID 再构建
#
#   ⚠️ 关于"digest"的**诚实说明**（部署审查纠正）：
#     该镜像的 `.RepoDigests` 是 `kb-worker@sha256:63c6…` —— 这是一个**本地伪 digest**
#     （仓库名没有 registry 主机），**不是** Registry 的 manifest digest。
#     因此**不能**写成 `FROM kb-worker:tag@sha256:63c6…`：BuildKit 会把 `kb-worker`
#     当成 Docker Hub 上的镜像去解析，导致长时间等待或直接失败。
#     正确做法：`FROM` 只写 **tag**，由构建脚本在构建前**严格校验**本地 image ID
#     等于上面那个值（校验不过就拒绝构建，不会"看起来成功但基座不对"）。
#
#   ⚠️ 前提：本机必须已有该基座镜像。若没有，请走【路径 B】。
#
# 【路径 B｜标准路径：完全从源码构建（任何机器可复现，需能访问 PyPI）】
#
#     docker build -f deploy/worker/Dockerfile -t kb-worker:local-build .
#     docker build -f deploy/app/Dockerfile \
#       --build-arg BASE_IMAGE=kb-worker:local-build -t kb-app:delivery .
#
#   唯一外部依赖是 PyPI（requirements.txt 全部固定版本，含 `whitenoise==6.8.2`）。
#   **CI / 他人机器应默认使用路径 B。**
#
# ============================ 为什么这样设计 ============================
#   本机到镜像源网络不稳定，全量安装可能数分钟并中途失败；交付路径复用已验证过的
#   基座层。但"复用"必须显式、可复现、可降级：
#     * 基座按 **本地 image ID** 校验（tag 会移动，ID 不会）；
#     * 提供**不依赖预置镜像**的标准路径；
#     * **绝不** `FROM kb-worker:local` 再打回 `kb-worker:local`（自我覆盖、循环漂移）。
#
# 关键：**必须 COPY 当前源码与 requirements**。历史镜像里的 config/apps/governance
# 是旧源码；只重打 tag 会让本机改动永远进不了容器（曾经真实发生的阻断）。
# requirements.txt 也要 COPY：否则镜像里"实际声明的依赖"与真实安装的 whitenoise 不一致。
ARG BASE_IMAGE=kb-worker:phase2-review-20261015
FROM ${BASE_IMAGE}

USER root

# ---- 旧 Office 解析（.doc/.xls/.ppt）与 DOCX→PDF 导出所需的系统依赖 ----
#
# 为什么必须装在镜像里（独立复现的结论）：
#   * 基座镜像（Debian 13）**既没有 LibreOffice，也没有任何字体**（连 DejaVu 都没有）——
#     所以"靠宿主 Windows 字体"或"退回 PIL 位图字体"在实际容器里必然出问题：
#     1100×360 的 OCR 自检图里整行文字只有约 138×9 像素 → 模型输出重复幻觉；
#     PDF 里的中文也会变成方块。
#   * 因此这里显式装：LibreOffice（writer/calc/impress：格式转换 + PDF 导出）
#     与 **fonts-noto-cjk**（真正的中文字体，供渲染/PDF/自检渲染路径使用）。
#   * 只装需要的组件（--no-install-recommends），装完立刻清 apt 列表。
#   * 转换本身仍走受限 runner（独立 profile、禁宏/禁外链、超时杀进程组、临时目录清理）。
# ---- 先把 Debian 源换成**官方 HTTPS** 并加上有界重试/超时 ----
#
# 为什么必须改（实测阻塞）：基座镜像里的 .sources 用的是 **http://deb.debian.org**，
# 在这台机器的网络下会 HTTP 500 / unexpected EOF，apt 每 60 秒重试一次，构建卡住
# 373 秒后失败。独立实测同一网络里 https 的 InRelease 正常（deb.debian.org 200 /
# 140421 字节；security.debian.org 200 / 43409 字节），所以：
#   * 只用**官方** HTTPS 地址（不引入任何第三方/不明镜像）；
#   * Acquire::Retries=1 + HTTP/HTTPS Timeout=20：失败**尽快**报出来，不无限重试；
#   * 不吞任何错误（这一层失败就是构建失败 —— 见下面那条硬自检）。
ARG DEBIAN_URI=https://deb.debian.org/debian
ARG DEBIAN_SECURITY_URI=https://security.debian.org/debian-security
RUN printf '%s\n' \
      'Types: deb' \
      "URIs: ${DEBIAN_URI}" \
      'Suites: trixie trixie-updates' \
      'Components: main' \
      'Signed-By: /usr/share/keyrings/debian-archive-keyring.pgp' \
      '' \
      'Types: deb' \
      "URIs: ${DEBIAN_SECURITY_URI}" \
      'Suites: trixie-security' \
      'Components: main' \
      'Signed-By: /usr/share/keyrings/debian-archive-keyring.pgp' \
      > /etc/apt/sources.list.d/debian.sources

RUN set -eux; \
    apt-get \
      -o Acquire::Retries=1 \
      -o Acquire::http::Timeout=20 \
      -o Acquire::https::Timeout=20 \
      update; \
    apt-get \
      -o Acquire::Retries=1 \
      -o Acquire::http::Timeout=20 \
      -o Acquire::https::Timeout=20 \
      install -y --no-install-recommends \
        libreoffice-writer \
        libreoffice-calc \
        libreoffice-impress \
        libreoffice-core \
        fonts-noto-cjk \
        fonts-dejavu-core \
        fontconfig; \
    rm -rf /var/lib/apt/lists/*

# ---- 构建期**硬自检**：缺组件就必须让构建失败 ----
#
# 审查纠正（真实缺陷）：上一版把整条 apt 链写成 `… && fc-cache … || true` ——
# `&&` 与 `||` 同优先级左结合，`|| true` 兜住的是**整条链**，于是 apt-get update /
# install 任何一步失败都会被吞成"成功"，最后构建出一个既没有 LibreOffice 也没有
# 中文字体的坏镜像。现在 apt 链一步都不吞（`set -eux` + 无 `|| true`），这条自检
# 再验三件事：`soffice` 真能执行、fontconfig 真找到了**中文字体**、PIL 真能用它
# 渲染出**有实际墨迹**的中文（这正是独立复现里 OCR 自检样图退化成 138×9 像素、
# 模型输出幻觉的根因）。
RUN soffice --version \
 && fc-list :lang=zh | head -5 \
 && fc-list :lang=zh | grep -qi noto \
 && python -c "import subprocess;from PIL import Image,ImageDraw,ImageFont;p=subprocess.check_output(['fc-match','-f','%{file}','Noto Sans CJK SC']).decode().strip();assert p, 'fc-match 没找到中文字体';assert ('noto' in p.lower() or 'cjk' in p.lower()), ('字体不是 CJK：%s' % p);f=ImageFont.truetype(p,48);im=Image.new('L',(420,80),0);ImageDraw.Draw(im).text((5,5),'质保期 12 个月',fill=255,font=f);ink=sum(1 for v in im.getdata() if v>0);assert ink>800, ('中文渲染墨迹过少：%d' % ink);print('CJK font ok:', p, 'ink', ink)"

# 仅新增依赖：whitenoise（静态资源进程内交付，少一个常驻容器）。
# 版本与 requirements.txt 保持一致；基座镜像已含其余全部依赖。
RUN pip install --no-cache-dir "whitenoise==6.8.2" "python-pptx==1.0.2" "XlsxWriter==3.2.9" "psutil==7.2.2"

# ---- 用**当前源码**覆盖历史层（交付必须包含本机最新代码）----
COPY requirements.txt ./
COPY config/ ./config/
COPY apps/ ./apps/
COPY governance/ ./governance/
COPY manage.py ./

# 静态文件收集目录（STATIC_ROOT=<项目根>/staticfiles），交给运行用户
RUN mkdir -p /app/staticfiles /app/media/templates && chown -R kb:kb /app

USER kb

WORKDIR /app
