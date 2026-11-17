#!/usr/bin/env bash
# 跨模态算子包 v1.0.0 · 服务器一键构建启动 + 健康等待
#
# 用法（在发布包根目录，docker-compose.yml 与 Dockerfile 同级）：
#   bash deploy.sh
#
# 脚本会做三件事：
#   1) 没有 .env 时从 .env.example 复制一份
#   2) 用 SHA256SUMS.txt 校验 18 个内置模型权重
#   3) docker compose 构建启动，并等待 /health 通过
#
# 可选环境变量（也可写同目录 .env）：
#   HOST_PORT         宿主机对外端口，默认 17059
#   OUTPUT_HOST_DIR   宿主机产物持久化目录（默认见 docker-compose.yml）
#   PYTHON_IMAGE / PIP_INDEX_URL / PIP_TRUSTED_HOST / APT_MIRROR  构建换源（默认已是国内源：清华 TUNA）
#   TORCH_INDEX_URL / TORCH_VERSION / TORCHVISION_VERSION          可选：装 CPU 版 torch（镜像小约 2 GB）
#   OMP_NUM_THREADS                                                可选：CPU 推理线程数
#
# 说明：本包模型全部由各算子本地 weight/ 加载，不需要模型网关地址或 Key；
#       推理设备为纯 CPU（8 个模型算子走 cpu，不需要 CUDA/NPU）。
set -euo pipefail

cd "$(dirname "$0")"

read_env() {
  [[ -f .env ]] || return 1
  awk -F= -v k="$1" '$1==k {print $2; exit}' .env
}

HOST_PORT="${HOST_PORT:-$(read_env HOST_PORT || true)}"
HOST_PORT="${HOST_PORT:-17059}"
HEALTH_PATH="${HEALTH_PATH:-/health}"

if docker compose version >/dev/null 2>&1; then
  COMPOSE=(docker compose)
elif command -v docker-compose >/dev/null 2>&1; then
  COMPOSE=(docker-compose)
else
  echo "ERROR: 未找到 Docker Compose（docker compose / docker-compose）" >&2
  exit 1
fi

echo "== 0. 准备运行环境变量（.env）=="
if [[ ! -f .env ]]; then
  cp .env.example .env
  echo "已从 .env.example 生成 .env（OUTPUT_DIR/HOST_PORT 等按需修改）"
fi

echo "== 1. 校验内置模型权重（18 个文件，SHA256SUMS.txt）=="
if command -v sha256sum >/dev/null 2>&1; then
  sha256sum -c SHA256SUMS.txt --quiet
  echo "OK: 权重与清单一致"
else
  echo "提示: 无 sha256sum，跳过宿主机权重校验（容器内会在验收脚本中校验）"
fi

echo "== 2. 构建并启动 opcrossmodalpool:1.0.0（宿主机 ${HOST_PORT} -> 容器 8100）=="
"${COMPOSE[@]}" -f docker-compose.yml up -d --build

echo "== 3. 等待健康检查 ${HEALTH_PATH}（最多 180 秒；首次构建可能较久）=="
for _ in $(seq 1 90); do
  if curl -fsS --max-time 3 "http://127.0.0.1:${HOST_PORT}${HEALTH_PATH}" >/tmp/opcrossmodalpool-health.json 2>/dev/null; then
    echo "OK: 健康检查通过"
    cat /tmp/opcrossmodalpool-health.json
    echo
    docker ps --filter "name=opcrossmodalpool"
    "${COMPOSE[@]}" -f docker-compose.yml ps
    echo
    echo "下一步: bash verify_service.sh（完整验收：接口、权重和预检）"
    exit 0
  fi
  sleep 2
done

echo "ERROR: 健康检查超时（180 秒），最近 120 行日志：" >&2
"${COMPOSE[@]}" -f docker-compose.yml logs --tail=120 >&2 || true
exit 1
