param([string]$Python = 'python', [int]$Port = 17024)
$ErrorActionPreference = 'Stop'
$root = Split-Path -Parent $PSScriptRoot
Set-Location -LiteralPath $root
$env:PYTHONDONTWRITEBYTECODE = '1'
$env:SME_PORT = [string]$Port
$env:SME_DATA_DIR = Join-Path $root 'runtime'
$env:SME_WORKSPACE_DIR = Join-Path $root 'runtime\workspace'
$env:SME_DB_PATH = Join-Path $root 'runtime\evaluation.db'
$env:SME_DATASET_DIR = Join-Path $root 'dataset'
$env:SME_TEXT_MODEL_PATH = Join-Path $root 'SME_EVAL_TEXT_001\models\bert-base-chinese'
$env:SME_WHISPER_MODEL = Join-Path $root 'SME_EVAL_AUDIO_001\models\whisper-base'
$env:SME_CLIP_MODEL = Join-Path $root 'SME_EVAL_VIDEO_001\models\clip\ViT-L-14.pt'
$env:HF_HUB_OFFLINE = '1'
$env:TRANSFORMERS_OFFLINE = '1'
$env:USE_TF = '0'
$env:USE_FLAX = '0'
if (-not $env:SME_MAX_WORKERS) { $env:SME_MAX_WORKERS = '1' }
if (-not $env:OMP_NUM_THREADS) { $env:OMP_NUM_THREADS = '4' }
if (-not $env:MKL_NUM_THREADS) { $env:MKL_NUM_THREADS = '4' }
& $Python -B -m uvicorn inference:app --host 0.0.0.0 --port $Port
exit $LASTEXITCODE
