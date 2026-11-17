# 跨模态算子包（表1 #01–#26）· 多算子打包镜像 · v1.0 正式版
#
# 上传结构对照甲方《开发规范》「算子上层目录」：Dockerfile 放在算子目录上级。
#   opcrossmodalpool/（发布包根目录，解压后目录名）  ← 本文件所在层级
#   ├── Dockerfile                 ← 容器化打包文件（1 个，覆盖 26 个算子）
#   ├── inference.py               ← 主推理服务入口（读 map.json，按 operatorId 路由）
#   ├── weight/                    ← 仅说明/清单；实际权重已内置到各算子目录 weight/（随镜像构建）
#   ├── map.json                   ← 算子映射表（编号 → 目录/端口/入口/权重内置关系）
#   └── CMA_xxx/  ×26              ← 算子目录：dataset/ utils/ configs/ tests/ output/ README.md
#                                    └── weight/  ← 该算子运行所需模型（#8/#12/#14/#16/#17/#18/#20）
#
# v1.0 正式版要点：每个需要模型的算子自带 weight/，单算子目录可独立复制、独立启动；
# 不再要求挂载包根 weight/ 或设置 MODEL_ROOT。MODEL_ROOT 仅保留为可选覆盖（外挂统一模型盘时）。
#
# 构建（在本包根目录内执行，发布包 v1.0.0）：
#   默认官方源：
#     docker build -t opcrossmodalpool:1.0.0 .
#   国内/内网下载慢时换源（三个参数可任选；基础镜像也可换成内网 registry）：
#     docker build -t opcrossmodalpool:1.0.0 \
#       --build-arg PYTHON_IMAGE=docker.m.daocloud.io/library/python:3.11-slim \
#       --build-arg PIP_INDEX_URL=https://pypi.tuna.tsinghua.edu.cn/simple \
#       --build-arg APT_MIRROR=mirrors.tuna.tsinghua.edu.cn .
#   compose 构建：在包根目录 .env 中写同名变量后 `docker compose build`（compose 已透传 build args）。
#
#   构建参数（只影响下载来源，不写入镜像）：
#     PYTHON_IMAGE      基础镜像（默认 python:3.11-slim；需保持 Debian slim 变体）
#     PIP_INDEX_URL     pip 源（默认 https://pypi.org/simple）
#     PIP_TRUSTED_HOST  私有 HTTP PyPI 的 host（可选；https 源不需要）
#     APT_MIRROR        Debian apt 镜像主机名（可选；只填主机，如 mirrors.tuna.tsinghua.edu.cn）
#     PIP_TIMEOUT       pip 下载超时秒数（默认 120）
#     PIP_RETRIES       pip 下载重试次数（默认 5）
#
# 运行（权重已随镜像内置到各算子目录；只需挂输出目录；可选设置 MODEL_ROOT 覆盖）：
#   docker run --rm -p 8100:8100 \
#     -e OUTPUT_DIR=/data/output -v /host/crossmodal_output:/data/output \
#     opcrossmodalpool:1.0.0
#   # 如需改用外部统一模型盘（不推荐，会绕过各算子内置权重校验）：
#   #   -v /host/models:/models:ro -e MODEL_ROOT=/models
#
# 调用（一个入口、一个端口，用 operatorId 选算子；M12 / 12 / 目录名 / 英文 id 均可）：
#   curl -X POST "http://127.0.0.1:8100/predict?operatorId=M12" -H "Content-Type: application/json" -d @req.json
#   # 或把 operatorId 放进 body：{"operatorId":"M12","uniqueId":"...","inputFile":"..."}
#   # 池化状态：GET /health、GET /ready、GET /operators

ARG PYTHON_IMAGE=python:3.11-slim

FROM ${PYTHON_IMAGE}

# 换源参数只在本镜像构建阶段生效，不写入运行环境。
# 默认走国内源：pip 用清华 TUNA PyPI，apt 用清华 TUNA Debian 镜像（比官方源快很多）。
#   想改回官方源：PIP_INDEX_URL=https://pypi.org/simple APT_MIRROR=off
#   内网/其他镜像：PIP_INDEX_URL=<内网 pypi>/simple，APT_MIRROR=<内网 debian 主机名>
ARG PIP_INDEX_URL=https://pypi.tuna.tsinghua.edu.cn/simple
ARG PIP_TRUSTED_HOST=
ARG APT_MIRROR=mirrors.tuna.tsinghua.edu.cn
ARG PIP_TIMEOUT=120
ARG PIP_RETRIES=5
# ---- 推理设备 / torch 轮子通道（纯 CPU 部署用）----
# 默认用 PIP_INDEX_URL 源里的 torch 轮子：x86_64 上是 CUDA 构建（无 GPU 时可正常按 CPU 运行，只是体积大），
# aarch64 上是 CPU 构建。要显式装 CPU 轮子（体积小、不依赖 CUDA 运行库），构建时传：
#   TORCH_INDEX_URL=https://download.pytorch.org/whl/cpu
# 国内镜像站若提供 pytorch 轮子目录（pytorch-wheels/cpu 一类），也可指过去；可用性以现场网络为准。
# 说明：本包代码只解析 cpu / cuda 两种设备（auto 在无 CUDA 时退回 cpu），不依赖 NVIDIA 显卡；
#       华为昇腾节点按普通 CPU 容器调度即可（无需 nvidia runtime、无需 npu 设备挂载）。
ARG TORCH_INDEX_URL=
ARG TORCH_VERSION=2.3.1
ARG TORCHVISION_VERSION=0.18.1

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PIP_NO_CACHE_DIR=1 \
    PORT=8100 \
    OUTPUT_DIR=/data/output \
    POOL_AUTOSTART=0 \
    POOL_STARTUP_TIMEOUT=60 \
    TZ=Asia/Shanghai \
    HF_HUB_OFFLINE=1 \
    TRANSFORMERS_OFFLINE=1 \
    YOLO_OFFLINE=1

# 系统依赖（非 pip 包）：
#   · ffmpeg / ffprobe —— #6 抽视频关键帧、#7 音频标准化必需（缺失时这两个算子会显式报依赖不可用）；
#   · tzdata —— 让 TZ=Asia/Shanghai 在日志时间戳中按名称生效（slim 基础镜像不自带）；
#   · libgomp1 —— torch CPU 推理的 OpenMP 运行库（slim 镜像不自带，缺了 import torch 会报 libgomp.so.1）。
# 国内构建慢时用 APT_MIRROR 换 Debian 镜像（默认已是清华源；只替换主机名，不动 /debian 路径）；
# 传 APT_MIRROR=off 可保持基础镜像自带源。
RUN export DEBIAN_FRONTEND=noninteractive; \
    if [ -n "${APT_MIRROR}" ] && [ "${APT_MIRROR}" != "off" ]; then \
        APT_HOST="${APT_MIRROR#*://}"; \
        APT_HOST="${APT_HOST%/}"; \
        for f in /etc/apt/sources.list /etc/apt/sources.list.d/*.sources; do \
            [ -f "$f" ] && sed -i "s|deb.debian.org|${APT_HOST}|g; s|security.debian.org|${APT_HOST}|g" "$f"; \
        done; \
        true; \
    fi \
    && apt-get update \
    && apt-get install -y --no-install-recommends ffmpeg tzdata libgomp1 \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app

# 依赖：上层 requirements.txt 已合并 26 个算子的依赖（原各算子 requirements.txt 已归档）
COPY requirements.txt ./requirements.txt

# 可选：先按指定索引装 CPU 版 torch/torchvision，并把这两行从 requirements 里去掉，
# 避免默认源再装一份带 CUDA 的同版本轮子（不传 TORCH_INDEX_URL 时本步骤跳过，行为与原来一致）。
RUN if [ -n "${TORCH_INDEX_URL}" ]; then \
        sed -i -E '/^torch==/d; /^torchvision==/d' requirements.txt; \
        pip install --no-cache-dir --index-url "${TORCH_INDEX_URL}" \
            --timeout "${PIP_TIMEOUT}" --retries "${PIP_RETRIES}" \
            "torch==${TORCH_VERSION}" "torchvision==${TORCHVISION_VERSION}"; \
    fi

# pip 依赖：--index-url / --trusted-host 只影响本次构建下载；超时与重试适配慢网络
RUN if [ -n "${PIP_TRUSTED_HOST}" ]; then \
        pip install --no-cache-dir --index-url "${PIP_INDEX_URL}" \
            --trusted-host "${PIP_TRUSTED_HOST}" \
            --timeout "${PIP_TIMEOUT}" --retries "${PIP_RETRIES}" \
            -r requirements.txt; \
    else \
        pip install --no-cache-dir --index-url "${PIP_INDEX_URL}" \
            --timeout "${PIP_TIMEOUT}" --retries "${PIP_RETRIES}" \
            -r requirements.txt; \
    fi

# 代码：上层四件 + 26 个算子目录（含各算子自带 weight/；v1.0 正式版权重随镜像构建，不排除）
COPY . ./

# 启动自检：注册表可加载、26 个算子目录与规范要求的关键项齐全（不拉起子进程）
RUN python inference.py --preflight

EXPOSE 8100

HEALTHCHECK --interval=30s --timeout=5s --start-period=20s --retries=3 \
    CMD python -c "import os,urllib.request;urllib.request.urlopen('http://127.0.0.1:%s/health' % os.environ.get('PORT','8100'), timeout=3)" || exit 1

# 单入口 uvicorn：算子子进程由 inference.py 按需拉起（各自监听 map.json 中的端口，仅容器内可见）
CMD ["sh", "-c", "uvicorn inference:app --host 0.0.0.0 --port ${PORT:-8100} --log-level info"]
