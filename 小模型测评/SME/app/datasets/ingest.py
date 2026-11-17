"""Dataset registration and normalization, aligned with `dataset-catalog.js`."""
from __future__ import annotations

import hashlib
import json
from typing import Any

from ..core.envelope import ApiError
from ..core.ids import new_dataset_id, now
from ..eval import rules, templates

MODALITY = {"text": "文本", "image": "图像", "audio": "音频", "video": "视频", "benchmark": "评测语料"}


def sample_hash(samples: list[dict[str, Any]]) -> str:
    payload = json.dumps(samples, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def normalize_dataset_record(value: dict[str, Any], kind: str) -> dict[str, Any]:
    if not isinstance(value, dict):
        raise ApiError(422, "DATASET_INVALID", "数据集记录格式不正确")
    dataset_id = str(value.get("id") or "").strip()
    name = str(value.get("name") or "").strip()
    if not dataset_id or not name:
        raise ApiError(422, "DATASET_INVALID", "数据集编码和名称不能为空")
    type_ = value.get("type") or value.get("evaluationType")
    if type_ not in templates.TEMPLATES:
        raise ApiError(422, "DATASET_INVALID", "请选择有效的数据类型")
    samples = value.get("samples")
    if not isinstance(samples, list) or not samples:
        raise ApiError(422, "DATASET_INVALID", "数据集必须包含可评测的样本")
    seen: set[str] = set()
    for index, sample in enumerate(samples):
        if not isinstance(sample, dict):
            raise ApiError(422, "DATASET_INVALID", f"第 {index + 1} 条样本格式不正确")
        sample_id = str(sample.get("id") or sample.get("sample_id") or "")
        if not sample_id:
            raise ApiError(422, "DATASET_INVALID", f"第 {index + 1} 条样本缺少 ID")
        if sample_id in seen:
            raise ApiError(422, "DATASET_INVALID", f"样本 ID 重复：{sample_id}")
        seen.add(sample_id)
    if kind == "production" and value.get("status") not in ("已完成", "completed"):
        raise ApiError(422, "DATASET_INVALID", "只有已完成的产线产出可以评测")
    if kind not in ("raw", "production"):
        raise ApiError(422, "DATASET_INVALID", "数据集类型不合法")

    evaluation_types = value.get("evaluationTypes")
    if not evaluation_types:
        evaluation_types = (["text", "benchmark"] if kind == "raw" and type_ == "text" else [type_])
    record = {
        "id": dataset_id,
        "name": name,
        "kind": kind,
        "type": type_,
        "evaluationTypes": evaluation_types,
        "modality": MODALITY[type_],
        "pipeline": value.get("pipeline") or "",
        "taskId": value.get("taskId") or None,
        "batchId": value.get("batchId") or dataset_id,
        "version": value.get("version") or "v1",
        "source": value.get("source") or ("本地上传" if kind == "raw" else "系统产线产出"),
        "status": value.get("status") or ("已登记" if kind == "raw" else "已完成"),
        "mode": value.get("mode") or "unmeasured",
        "samples": samples,
        "count": len(samples),
        "createdAt": value.get("createdAt") or now(),
        "standard": value.get("standard") or "通用质量标准",
        "note": value.get("note") or "",
        "sampleHash": sample_hash(samples),
    }
    return record


def register_dataset(value: dict[str, Any], kind: str) -> dict[str, Any]:
    from ..store import db

    return db.save_dataset(normalize_dataset_record(value, kind))


def prepare_dataset_for_task(dataset: dict[str, Any], type_: str) -> list[dict[str, Any]]:
    if type_ not in (dataset.get("evaluationTypes") or [dataset.get("type")]):
        raise ApiError(422, "DATASET_TYPE_MISMATCH", "请选择与评测类型匹配的数据集")
    return dataset.get("samples") or []


def new_raw_dataset(name: str, type_: str, samples: list[dict[str, Any]],
                    *, source: str = "本地上传", mode: str = "unmeasured",
                    note: str = "") -> dict[str, Any]:
    record = normalize_dataset_record({
        "id": new_dataset_id(), "name": name, "type": type_, "samples": samples,
        "source": source, "mode": mode, "status": "已登记", "note": note,
    }, "raw")
    return register_dataset(record, "raw")
