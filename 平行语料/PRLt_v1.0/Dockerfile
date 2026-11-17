FROM python:3.12-slim

# 镜像源可通过构建参数覆盖，默认使用清华镜像。
ARG PIP_INDEX_URL=https://pypi.tuna.tsinghua.edu.cn/simple

WORKDIR /opt/pretrain-parallel-corpus

# torch 2.6 CPU 轮子需要 libgomp；其余依赖均为 manylinux 轮子。
RUN apt-get update \
    && apt-get install -y --no-install-recommends libgomp1 \
    && rm -rf /var/lib/apt/lists/*

# 正式版模型全部本地化：默认离线加载，避免容器内联网回退。
ENV HF_HUB_OFFLINE=1 \
    TRANSFORMERS_OFFLINE=1

COPY requirements.txt .
RUN pip install --no-cache-dir -i ${PIP_INDEX_URL} -r requirements.txt

COPY . .

ENV OPERATOR_POOL_ROOT=/var/lib/pretrain-parallel-corpus
RUN mkdir -p /var/lib/pretrain-parallel-corpus

EXPOSE 6291
CMD ["uvicorn", "inference:app", "--host", "0.0.0.0", "--port", "6291"]
