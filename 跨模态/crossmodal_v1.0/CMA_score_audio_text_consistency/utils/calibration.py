"""可选的音文余弦标定层。

标定层只把原始 wav2vec2/BERT 余弦映射到可比较的诊断分数；它不改变表1
正式门禁。任何映射文件都必须显式登记版本、训练集和验证结果，缺失或非法
文件时按未启用处理，避免通过一个未审计的分数变换放宽门禁。
"""
from __future__ import annotations

import json
import math
from pathlib import Path
from typing import Any, Dict


class CalibrationError(ValueError):
    pass


class CosineScoreMapping:
    """可审计的标量仿射映射，仅用于排序/诊断，不参与正式判定。"""

    def __init__(self, *, version: str, scale: float, bias: float,
                 source: str, metadata: Dict[str, Any] | None = None) -> None:
        if not version.strip():
            raise CalibrationError("mapping version is empty")
        if not math.isfinite(scale) or not math.isfinite(bias):
            raise CalibrationError("mapping parameters must be finite")
        self.version = version
        self.scale = float(scale)
        self.bias = float(bias)
        self.source = source
        self.metadata = dict(metadata or {})

    @classmethod
    def from_json(cls, path: str | Path) -> "CosineScoreMapping":
        target = Path(path)
        payload = json.loads(target.read_text(encoding="utf-8"))
        if not isinstance(payload, dict):
            raise CalibrationError("mapping file must contain an object")
        if payload.get("method") != "affine_score_space":
            raise CalibrationError("unsupported mapping method")
        return cls(version=str(payload.get("version") or ""),
                   scale=float(payload.get("scale")), bias=float(payload.get("bias")),
                   source=str(target), metadata=payload)

    def apply(self, raw_cosine: float) -> float:
        value = float(raw_cosine)
        if not math.isfinite(value):
            raise CalibrationError("raw cosine is not finite")
        return max(0.0, min(1.0, self.scale * value + self.bias))

    def snapshot(self) -> Dict[str, Any]:
        return {
            "version": self.version,
            "method": "affine_score_space",
            "scale": self.scale,
            "bias": self.bias,
            "source": self.source,
            "metadata": self.metadata,
            "formal_gate": "raw_cosine_similarity",
            "gate_override_allowed": False,
        }
