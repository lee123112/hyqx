# Standard Dockerfile syntax: compatible with the classic builder (no buildx required).
ARG PYTHON_IMAGE=m.daocloud.io/docker.io/library/python:3.11-slim
FROM ${PYTHON_IMAGE}

ARG APT_MIRROR=https://mirrors.tuna.tsinghua.edu.cn/debian
ARG APT_SECURITY_MIRROR=https://mirrors.tuna.tsinghua.edu.cn/debian-security
ARG PIP_INDEX_URL=https://mirrors.tuna.tsinghua.edu.cn/pypi/web/simple
ARG TORCH_INDEX_URL=https://mirrors.nju.edu.cn/pytorch/whl/cpu
# Keep the CPU pair from the server's build log, rather than fetching CUDA wheels from PyPI.
ARG TORCH_VERSION=2.15.0+cpu
ARG TORCHVISION_VERSION=0.29.2+cpu

ENV PYTHONUNBUFFERED=1 PIP_NO_CACHE_DIR=1 PYTHONDONTWRITEBYTECODE=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1 PIP_DEFAULT_TIMEOUT=120 PIP_RETRIES=8 \
    PIP_INDEX_URL=${PIP_INDEX_URL} PIP_EXTRA_INDEX_URL="" PIP_CONFIG_FILE=/dev/null \
    SME_HOST=0.0.0.0 SME_PORT=6289 \
    SME_DATA_DIR=/app/data SME_WORKSPACE_DIR=/app/data/workspace \
    SME_DB_PATH=/app/data/evaluation.db SME_DATASET_DIR=/app/dataset \
    HF_HUB_OFFLINE=1 TRANSFORMERS_OFFLINE=1 USE_TF=0 USE_FLAX=0

# Rewrite both DEB822 (*.sources) and traditional (*.list) Debian repositories.
RUN set -eux; \
    for source_file in /etc/apt/sources.list /etc/apt/sources.list.d/*.sources /etc/apt/sources.list.d/*.list; do \
        if [ -f "$source_file" ]; then \
            sed -i \
                -e "s|https\?://deb.debian.org/debian-security|${APT_SECURITY_MIRROR}|g" \
                -e "s|https\?://security.debian.org/debian-security|${APT_SECURITY_MIRROR}|g" \
                -e "s|https\?://deb.debian.org/debian|${APT_MIRROR}|g" \
                "$source_file"; \
        fi; \
    done; \
    apt-get -o Acquire::Retries=5 -o Acquire::http::Timeout=120 -o Acquire::https::Timeout=120 update; \
    DEBIAN_FRONTEND=noninteractive apt-get install -y --no-install-recommends ca-certificates libglib2.0-0 libgomp1; \
    rm -rf /var/lib/apt/lists/*

WORKDIR /app
COPY requirements.txt .

# Only fetch the two CPU wheels from the PyTorch mirror. Resolve all their ordinary
# dependencies from the domestic PyPI mirror; some PyTorch indexes link those abroad.
RUN set -eux; \
    mkdir -p /tmp/torch-wheels; \
    printf 'torch==%s\ntorchvision==%s\n' "$TORCH_VERSION" "$TORCHVISION_VERSION" > /tmp/torch-constraints.txt; \
    python -m pip download --no-deps --only-binary=:all: \
        --index-url "$TORCH_INDEX_URL" --dest /tmp/torch-wheels \
        "torch==$TORCH_VERSION" "torchvision==$TORCHVISION_VERSION"; \
    python -m pip install --index-url "$PIP_INDEX_URL" \
        --constraint /tmp/torch-constraints.txt /tmp/torch-wheels/*.whl; \
    python -m pip install --index-url "$PIP_INDEX_URL" \
        --constraint /tmp/torch-constraints.txt -r requirements.txt; \
    python -m pip check; \
    python -c "import torch, torchvision; assert torch.version.cuda is None, 'Expected CPU-only PyTorch'; print('CPU packages:', torch.__version__, torchvision.__version__)"; \
    rm -rf /tmp/torch-wheels /tmp/torch-constraints.txt

COPY app ./app
COPY common ./common
COPY configs ./configs
COPY web ./web
COPY tools ./tools
COPY examples ./examples
COPY dataset/reference ./dataset/reference
COPY SME_EVAL_TEXT_001 ./SME_EVAL_TEXT_001
COPY SME_EVAL_IMAGE_001 ./SME_EVAL_IMAGE_001
COPY SME_EVAL_AUDIO_001 ./SME_EVAL_AUDIO_001
COPY SME_EVAL_VIDEO_001 ./SME_EVAL_VIDEO_001
COPY SME_EVAL_BENCHMARK_001 ./SME_EVAL_BENCHMARK_001
COPY inference.py main.py map.json RELEASE.json ./

RUN mkdir -p /app/data/workspace /app/dataset/business
EXPOSE 6289
HEALTHCHECK --interval=30s --timeout=5s --start-period=30s --retries=3 \
    CMD python -c "import json,urllib.request; r=json.load(urllib.request.urlopen('http://127.0.0.1:6289/health', timeout=3)); assert r['code']==0 and r['data']['operatorCount']==5"
CMD ["uvicorn", "inference:app", "--host", "0.0.0.0", "--port", "6289"]
