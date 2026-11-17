#!/bin/sh
# Activate an installed Python3.11 environment, or create .venv first.
set -eu
ROOT=$(CDPATH= cd -- "$(dirname -- "$0")/.." && pwd)
cd "$ROOT"
if [ -n "${PYTHON:-}" ]; then
    PYTHON_BIN=$PYTHON
elif [ -x "$ROOT/.venv/bin/python" ]; then
    PYTHON_BIN=$ROOT/.venv/bin/python
elif command -v python3.11 >/dev/null 2>&1; then
    PYTHON_BIN=python3.11
else
    PYTHON_BIN=python3
fi
"$PYTHON_BIN" -c 'import sys; assert sys.version_info[:2] == (3, 11), "Use Python 3.11 for this delivery"'
export PYTHONDONTWRITEBYTECODE=1
export SME_DATA_DIR="${SME_DATA_DIR:-$ROOT/runtime}"
export SME_WORKSPACE_DIR="${SME_WORKSPACE_DIR:-$SME_DATA_DIR/workspace}"
export SME_DB_PATH="${SME_DB_PATH:-$SME_DATA_DIR/evaluation.db}"
export SME_DATASET_DIR="${SME_DATASET_DIR:-$ROOT/dataset}"
export OMP_NUM_THREADS="${OMP_NUM_THREADS:-4}"
export MKL_NUM_THREADS="${MKL_NUM_THREADS:-4}"
export SME_MAX_WORKERS="${SME_MAX_WORKERS:-1}"
export SME_PORT="${SME_PORT:-17024}"
export HF_HUB_OFFLINE=1 TRANSFORMERS_OFFLINE=1 USE_TF=0 USE_FLAX=0
exec "$PYTHON_BIN" -B -m uvicorn inference:app --host "${SME_HOST:-0.0.0.0}" --port "$SME_PORT"
