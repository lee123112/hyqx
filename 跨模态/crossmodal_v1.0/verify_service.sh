#!/usr/bin/env bash
# 跨模态算子包 v1.0.0 · 部署验收脚本
#
# 用法（在发布包根目录）：
#   bash verify_service.sh          # 容器已运行时，在容器内自检（推荐）
#   bash verify_service.sh --local  # 不进入容器，直接请求宿主机 HOST_PORT（默认 17059）
#
# 校验内容：/health、/ready、/operators(26)、
#           内置权重 SHA-256、ffmpeg/ffprobe、注册表 preflight、无模型网关变量。
set -uo pipefail

cd "$(dirname "$0")"

read_env() {
  [[ -f .env ]] || return 1
  awk -F= -v k="$1" '$1==k {print $2; exit}' .env
}

HOST_PORT="${HOST_PORT:-$(read_env HOST_PORT || true)}"
HOST_PORT="${HOST_PORT:-17059}"
MODE="${1:-}"

if [[ "$MODE" == "--local" ]]; then
  PY="$(command -v python3 || command -v python || true)"
  if [[ -z "$PY" ]]; then
    echo "ERROR: 宿主机没有 python3/python，无法本地验收；请改用容器内模式" >&2
    exit 1
  fi
  exec "$PY" verify_service.py --host 127.0.0.1 --port "$HOST_PORT" --no-preflight
fi

if ! docker ps --filter name=opcrossmodalpool --format '{{.Names}}' | grep -q '^opcrossmodalpool$'; then
  echo "ERROR: 容器 opcrossmodalpool 未运行。先执行 bash deploy.sh，或用 --local 直连宿主机端口" >&2
  exit 1
fi

docker exec opcrossmodalpool python verify_service.py --host 127.0.0.1 --port 8100
