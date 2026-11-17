"""Audited dataset-level ASR training endpoint."""
from pathlib import Path
from typing import Literal

from fastapi import APIRouter
from pydantic import BaseModel, Field, ConfigDict

from ..core.config import settings
from ..core.envelope import ApiError, ok
from ..core.model_assets import bundled_model_path
from ..experiments.audio_protocol import AudioProtocolSpec, run_audio_protocol

router = APIRouter(prefix="/api/v1/eval", tags=["audio-protocol"])


class AudioProtocolRequest(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)
    oursManifest: str
    refManifest: str
    evalManifest: str
    modelDir: str | None = None
    seeds: list[int] = Field(default_factory=lambda: [42, 43, 44], min_length=1, max_length=10)
    epochs: int = Field(default=1, ge=1, le=100)
    batchSize: int = Field(default=1, ge=1, le=32)
    learningRate: float = Field(default=1e-5, gt=0, le=.01, allow_inf_nan=False)
    trainingMode: Literal["full", "decoder"] = "decoder"
    device: Literal["auto", "cpu", "cuda"] = "auto"
    language: Literal["chinese", "english"] = "chinese"
    purpose: Literal["smoke", "exploration", "quality", "formal"] = "exploration"
    maxDurationGap: float = Field(default=.05, ge=0, le=.1, allow_inf_nan=False)
    maxNewTokens: int = Field(default=128, ge=1, le=400)
    maxLeakagePairs: int = Field(default=4_000_000, ge=1)


def _allowed_roots():
    return [settings.project_root.parent.resolve(), settings.dataset_dir.resolve(), settings.data_dir.resolve()]


def _path(raw: str, directory: bool = False) -> Path:
    if not raw.strip():
        raise ApiError(422, "REQUEST_INVALID", "清单或模型路径不能为空")
    path = Path(raw).expanduser()
    if not path.is_absolute():
        path = settings.project_root.parent / path
    path = path.resolve()
    if not any(path.is_relative_to(root) for root in _allowed_roots()):
        raise ApiError(422, "REQUEST_INVALID", "音频协议路径超出允许的数据目录")
    if not (path.is_dir() if directory else path.is_file()):
        raise ApiError(422, "ASSET_MISSING", "音频协议清单或模型目录不存在")
    return path


@router.post("/audio/protocol")
def run_audio_quality_protocol(payload: AudioProtocolRequest):
    return ok(run_audio_protocol(AudioProtocolSpec(
        ours_manifest=_path(payload.oursManifest), ref_manifest=_path(payload.refManifest),
        eval_manifest=_path(payload.evalManifest), model_dir=(
            _path(payload.modelDir, directory=True) if payload.modelDir else
            bundled_model_path('audio')),
        seeds=tuple(payload.seeds), epochs=payload.epochs, batch_size=payload.batchSize,
        learning_rate=payload.learningRate, training_mode=payload.trainingMode,
        device=payload.device, language=payload.language, purpose=payload.purpose,
        max_duration_gap=payload.maxDurationGap, max_new_tokens=payload.maxNewTokens,
        max_leakage_pairs=payload.maxLeakagePairs)))
