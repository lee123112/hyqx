# 语音加扰（语音对抗平台）算子包
# 无训练过程的算子包：11 个算子共用本镜像，由 inference.py 统一路由。
FROM python:3.9-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    NUMBA_CACHE_DIR=/tmp/numba \
    AVP_DATA_ROOT=/data \
    CONFIG_PATH=

WORKDIR /app

# libsndfile1 是 soundfile 的底层依赖（wav/flac/aiff/mp3 全走它，不引入 ffmpeg）
RUN apt-get update \
    && apt-get install -y --no-install-recommends libsndfile1 \
    && rm -rf /var/lib/apt/lists/*

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY . .

# 启动前静态校验：配置、注册表、11 个算子实现齐全，且公共模块可导入
RUN python -c "import json,sys; m=json.load(open('map.json',encoding='utf-8')); ops=m['operators']; assert len(ops)==11, len(ops); [open(v['processor'],encoding='utf-8').read() for v in ops.values()]" \
    && python -c "import yaml,sys; [yaml.safe_load(open('%s/configs/config.yaml' % d,encoding='utf-8')) for d in __import__('json').load(open('map.json',encoding='utf-8'))['operators']]" \
    && python -c "from common import audio, config, context, iofs, lineage, logger, schemas; print('common ok')" \
    && python -c "import inference; print('inference ok, operators =', len(inference.OPERATORS))"

RUN mkdir -p /data && useradd -m -u 1000 avp && chown -R avp:avp /app /data
USER avp

# 容器内监听 8000；Compose 对外发布 17023，容器名 op-speech-adversarial。
EXPOSE 8000

HEALTHCHECK --interval=30s --timeout=5s --start-period=25s --retries=3 \
    CMD python -c "import urllib.request;urllib.request.urlopen('http://127.0.0.1:8000/health').read()"

CMD ["python", "inference.py", "--serve", "--host", "0.0.0.0", "--port", "8000"]
