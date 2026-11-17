"""Dataset catalog endpoints: upload/import, production registration, measurements.

Uploads are parsed with :mod:`app.datasets.parser`, persisted through
:mod:`app.datasets.ingest`, and media is unpacked to
``data/datasets/<kind>/<id>/assets`` so that samples only carry API-relative URIs.
"""
from __future__ import annotations

import csv
import io
import json
import mimetypes
import re
import shutil
import zipfile
from pathlib import Path
from typing import Any

from fastapi import APIRouter, Body, File, Form, Query, Request, UploadFile
from fastapi.responses import FileResponse, Response

from ..core.config import settings
from ..core.envelope import ApiError, ok
from ..core.ids import new_dataset_id
from ..core.security import safe_join, sanitize_filename
from ..datasets import ingest
from ..datasets.parser import MEASUREMENT_COLUMNS, _extract_zip, _load_rows, _num
from ..eval import rules, templates
from ..store import db

router = APIRouter(prefix="/api/v1/datasets", tags=["datasets"])

KINDS = ("raw", "production")
FORMATS = ("json", "jsonl", "ndjson", "csv", "txt", "zip")
MEDIA_SUFFIXES = (".jpg", ".jpeg", ".png", ".webp", ".bmp", ".wav", ".mp3",
                  ".flac", ".mp4", ".mov", ".webm", ".avi", ".mkv")
MANIFEST_NAMES = ("dataset.json", "samples.json", "manifest.json", "dataset.jsonl",
                  "samples.jsonl", "dataset.csv", "samples.csv")
MANIFEST_SUFFIXES = (".json", ".jsonl", ".ndjson", ".csv")


def _check_kind(kind: str) -> str:
    if kind not in KINDS:
        raise ApiError(422, "REQUEST_INVALID", "数据集类型仅支持 raw/production")
    return kind


def _check_type(type_: str) -> str:
    type_ = (type_ or "").strip()
    if type_ not in templates.TEMPLATES:
        raise ApiError(422, "CONFIG_INVALID", "请选择有效的数据类型")
    return type_


def _dataset_dir(kind: str, dataset_id: str) -> Path:
    return safe_join(settings.datasets_dir, kind, dataset_id)


def _assets_dir(kind: str, dataset_id: str) -> Path:
    path = _dataset_dir(kind, dataset_id) / "assets"
    path.mkdir(parents=True, exist_ok=True)
    return path


def _asset_uri(kind: str, dataset_id: str, relative: str) -> str:
    return f"/api/v1/datasets/{kind}/{dataset_id}/assets/{relative}"


def _get_dataset(kind: str, dataset_id: str) -> dict[str, Any]:
    _check_kind(kind)
    dataset = db.get_dataset(kind, dataset_id)
    if not dataset:
        raise ApiError(404, "NOT_FOUND", "数据集不存在")
    return dataset


async def _read_upload(file: UploadFile) -> bytes:
    content = await file.read()
    if not content:
        raise ApiError(422, "REQUEST_INVALID", "上传文件为空")
    limit = max(1, settings.max_upload_mb) * 1024 * 1024
    if len(content) > limit:
        raise ApiError(413, "PAYLOAD_TOO_LARGE",
                       f"上传文件超过 {settings.max_upload_mb}MB 限制")
    return content


def _measurements_complete(type_: str, samples: list[dict[str, Any]]) -> bool:
    keys = [metric["key"] for metric in templates.TEMPLATES[type_]["metrics"]]
    for sample in samples:
        values = sample.get("measurements") if isinstance(sample, dict) else None
        if not isinstance(values, dict):
            return False
        for model in ("baseline", "candidate"):
            model_values = values.get(model)
            if not isinstance(model_values, dict):
                return False
            if any(not rules.is_num(model_values.get(key)) for key in keys):
                return False
    return True


def _attach_assets(rows: list[dict[str, Any]], assets: Path, kind: str,
                   dataset_id: str) -> list[dict[str, Any]]:
    """Point samples at served asset URIs; never expose host absolute paths."""
    for row in rows:
        ref = row.get("file") or row.get("fileName") or row.get("uri")
        if isinstance(ref, str) and ref:
            raw = ref.replace("\\", "/")
            if not raw.startswith(("http://", "https://", "demo://", "/api/", "/workspace/")):
                relative = Path(raw)
                if not relative.is_absolute() and ".." not in relative.parts:
                    candidate = safe_join(assets, raw)
                    if candidate.is_file():
                        row["fileName"] = candidate.name
                        row["fileSize"] = candidate.stat().st_size
                        row["uri"] = _asset_uri(
                            kind, dataset_id,
                            candidate.relative_to(assets.resolve()).as_posix())
        # Host paths from an uploaded manifest must never be persisted.
        row.pop("file", None)
    return rows


def _select_manifest(files: list[Path]) -> Path | None:
    manifests = [
        item for item in files
        if item.suffix.lower() in MANIFEST_SUFFIXES and item.name.lower() in MANIFEST_NAMES
    ]
    if not manifests:
        manifests = [item for item in files if item.suffix.lower() in MANIFEST_SUFFIXES]
    return manifests[0] if manifests else None


@router.get("")
def list_datasets(
    kind: str = Query(""),
    type_: str = Query("", alias="type"),
) -> dict:
    kind = kind.strip()
    if kind:
        _check_kind(kind)
    type_ = type_.strip()
    if type_:
        _check_type(type_)
    items = db.list_datasets(kind or None, type_ or None)
    return ok({"items": items, "total": len(items)})


@router.get("/template")
def dataset_template(type_: str = Query("", alias="type")) -> dict:
    type_ = _check_type(type_)
    template = templates.TEMPLATES[type_]
    demo = rules.demoSamples(type_)[:2]
    columns = [f"{model}_{metric}"
               for model, metric in MEASUREMENT_COLUMNS.values()]
    return ok({
        "type": type_,
        "name": template["name"],
        "description": template["description"],
        "formats": list(FORMATS),
        "fields": [
            {"key": "id", "required": True, "description": "样本唯一 ID"},
            {"key": "input", "required": True, "description": "文本内容或媒体引用"},
            {"key": "title", "required": False, "description": "展示标题"},
            {"key": "scene", "required": False, "description": "场景"},
            {"key": "sourceStep", "required": False, "description": "回溯处理步骤"},
            {"key": "parameters", "required": False, "description": "处理参数对象"},
            {"key": "measurements", "required": False, "description": "baseline/candidate 逐条指标"},
        ],
        "metrics": template["metrics"],
        "measurementColumns": columns,
        "samples": demo,
    })


@router.post("/import")
async def import_dataset(
    file: UploadFile = File(...),
    type_: str = Form(..., alias="type"),
    name: str = Form(""),
    mode: str = Form(""),
) -> dict:
    type_ = _check_type(type_)
    mode = (mode or "").strip()
    if mode and mode not in ("demo", "measured", "unmeasured"):
        raise ApiError(422, "REQUEST_INVALID", "mode 仅支持 demo/measured/unmeasured")

    filename = sanitize_filename(file.filename or "dataset.json")
    suffix = Path(filename).suffix.lower()
    if suffix not in (".json", ".jsonl", ".ndjson", ".csv", ".txt", ".zip"):
        raise ApiError(422, "DATASET_INVALID", "仅支持 JSON/JSONL/NDJSON/CSV/TXT/ZIP 数据集")
    content = await _read_upload(file)

    dataset_id = new_dataset_id()
    assets = _assets_dir("raw", dataset_id)
    temp_dir = settings.workspace_dir / dataset_id / "input"
    temp_dir.mkdir(parents=True, exist_ok=True)
    try:
        temp_path = temp_dir / filename
        temp_path.write_bytes(content)
        if suffix == ".zip":
            try:
                files = _extract_zip(temp_path, assets)
            except zipfile.BadZipFile as exc:
                raise ApiError(422, "DATASET_INVALID", "压缩包格式不正确") from exc
            manifest = _select_manifest(files)
            if manifest is None:
                raise ApiError(422, "DATASET_INVALID", "压缩包缺少 JSON/JSONL/CSV 样本清单")
            rows = _load_rows(manifest)
            dataset_name = name.strip() or manifest.stem
        else:
            rows = _load_rows(temp_path)
            dataset_name = name.strip() or Path(filename).stem
        if not rows:
            raise ApiError(422, "DATASET_INVALID", "数据集没有可用样本")
        if len(rows) > settings.max_samples:
            raise ApiError(413, "PAYLOAD_TOO_LARGE",
                           f"单次最多 {settings.max_samples} 条样本")
        rows = _attach_assets(rows, assets, "raw", dataset_id)
        if not mode:
            mode = "measured" if _measurements_complete(type_, rows) else "unmeasured"
        record = ingest.normalize_dataset_record({
            "id": dataset_id,
            "name": dataset_name or f"{type_} 数据集",
            "type": type_,
            "samples": rows,
            "mode": mode,
            "source": "本地上传",
            "status": "已登记",
        }, "raw")
        saved = ingest.register_dataset(record, "raw")
    except Exception:
        shutil.rmtree(_dataset_dir("raw", dataset_id), ignore_errors=True)
        raise
    finally:
        shutil.rmtree(temp_dir, ignore_errors=True)
    return ok(saved)


@router.post("/production")
def register_production(payload: dict[str, Any] = Body(...)) -> dict:
    if not isinstance(payload, dict):
        raise ApiError(422, "REQUEST_INVALID", "请求体格式不正确")
    value = dict(payload)
    if not str(value.get("id") or "").strip():
        value["id"] = new_dataset_id()
    if not str(value.get("name") or "").strip():
        type_ = str(value.get("type") or value.get("evaluationType") or "").strip()
        label = templates.TEMPLATES[type_]["name"] if type_ in templates.TEMPLATES else "数据集"
        value["name"] = f"产线产出 · {label}"
    if not str(value.get("source") or "").strip():
        value["source"] = "系统产线产出"
    samples = value.get("samples")
    if isinstance(samples, list) and len(samples) > settings.max_samples:
        raise ApiError(413, "PAYLOAD_TOO_LARGE",
                       f"单次最多 {settings.max_samples} 条样本")
    saved = ingest.register_dataset(value, "production")
    return ok(saved)


def _parse_measurement_rows(filename: str, content: bytes) -> list[dict[str, Any]]:
    suffix = Path(filename).suffix.lower()
    try:
        text = content.decode("utf-8-sig")
    except UnicodeDecodeError as exc:
        raise ApiError(422, "REQUEST_INVALID", "指标文件必须为 UTF-8 编码") from exc
    if suffix == ".json":
        try:
            parsed = json.loads(text)
        except ValueError as exc:
            raise ApiError(422, "REQUEST_INVALID", "指标 JSON 格式不正确") from exc
        rows = parsed.get("samples") if isinstance(parsed, dict) else parsed
        if isinstance(parsed, dict) and isinstance(parsed.get("data"), dict):
            rows = parsed["data"].get("samples", rows)
        if not isinstance(rows, list):
            raise ApiError(422, "REQUEST_INVALID", "指标 JSON 必须包含 samples 数组")
        return [row for row in rows if isinstance(row, dict)]
    if suffix in (".jsonl", ".ndjson"):
        rows = []
        for line in text.splitlines():
            line = line.strip()
            if not line:
                continue
            try:
                item = json.loads(line)
            except ValueError as exc:
                raise ApiError(422, "REQUEST_INVALID", "指标 JSONL 第 "
                               f"{len(rows) + 1} 行格式不正确") from exc
            if isinstance(item, dict):
                rows.append(item)
        return rows
    if suffix == ".csv":
        return [dict(row) for row in csv.DictReader(io.StringIO(text))]
    raise ApiError(422, "REQUEST_INVALID", "指标文件仅支持 JSON/JSONL/CSV")


def _extract_measurements(row: dict[str, Any]) -> dict[str, dict[str, Any]]:
    value = row.get("measurements")
    result: dict[str, dict[str, Any]] = {"baseline": {}, "candidate": {}}
    if isinstance(value, dict):
        for model in ("baseline", "candidate"):
            model_values = value.get(model)
            if isinstance(model_values, dict):
                result[model].update(model_values)
    for model in ("baseline", "candidate"):
        model_values = row.get(model)
        if isinstance(model_values, dict):
            result[model].update(model_values)
    for column, (model, metric) in MEASUREMENT_COLUMNS.items():
        raw = row.get(column)
        if raw not in (None, ""):
            number = _num(raw)
            if number is not None:
                result[model][metric] = number
    return {model: values for model, values in result.items() if values}


@router.post("/{kind}/{dataset_id}/measurements")
async def upload_measurements(
    kind: str,
    dataset_id: str,
    file: UploadFile = File(...),
) -> dict:
    dataset = _get_dataset(kind, dataset_id)
    filename = sanitize_filename(file.filename or "measurements.json")
    content = await _read_upload(file)
    rows = _parse_measurement_rows(filename, content)
    if not rows:
        raise ApiError(422, "REQUEST_INVALID", "指标文件没有可用记录")

    uploaded: dict[str, dict[str, dict[str, Any]]] = {}
    for index, row in enumerate(rows):
        sample_id = str(row.get("id") or row.get("sampleId") or row.get("sample_id")
                        or "").strip()
        if not sample_id:
            raise ApiError(422, "REQUEST_INVALID", f"第 {index + 1} 条指标缺少样本 ID")
        measurements = _extract_measurements(row)
        if not measurements:
            raise ApiError(422, "MEASUREMENTS_INCOMPLETE",
                           f"{sample_id} 缺少 baseline/candidate 指标")
        uploaded[sample_id] = measurements

    type_ = str(dataset.get("type") or "")
    metrics = templates.TEMPLATES[type_]["metrics"]
    samples = dataset.get("samples") or []
    merged = 0
    known: set[str] = set()
    for sample in samples:
        sample_id = str(sample.get("id"))
        known.add(sample_id)
        provided = uploaded.get(sample_id)
        if not provided:
            continue
        current = sample.get("measurements")
        if not isinstance(current, dict):
            current = {"baseline": {}, "candidate": {}}
        for model in ("baseline", "candidate"):
            target = current.get(model)
            if not isinstance(target, dict):
                target = {}
                current[model] = target
            target.update(provided.get(model) or {})
        sample["measurements"] = current
        merged += 1
    ignored = len(set(uploaded) - known)

    missing: list[dict[str, Any]] = []
    for sample in samples:
        values = sample.get("measurements") if isinstance(sample.get("measurements"), dict) else {}
        for model in ("baseline", "candidate"):
            model_values = values.get(model) if isinstance(values.get(model), dict) else {}
            for metric in metrics:
                key = metric["key"]
                maximum = (float("inf") if dataset.get("type") == "audio" and key == "cer"
                           else 1 if key in ("iou", "lossCv") else 100)
                value = model_values.get(key)
                if not rules.is_num(value) or value < 0 or value > maximum:
                    missing.append({"sampleId": sample.get("id"), "model": model,
                                    "metric": key})
    if missing:
        raise ApiError(
            422, "MEASUREMENTS_INCOMPLETE",
            f"{len(missing)} 项指标缺失或越界，请补齐 baseline/candidate 全部指标",
            details=missing[:20],
        )

    dataset["samples"] = samples
    dataset["count"] = len(samples)
    dataset["mode"] = "measured"
    dataset["sampleHash"] = ingest.sample_hash(samples)
    saved = db.save_dataset(dataset)
    return ok({"dataset": saved, "merged": merged, "ignored": ignored})


@router.get("/{kind}/{dataset_id}/assets/{path:path}")
def get_asset(kind: str, dataset_id: str, path: str, request: Request) -> Response:
    _get_dataset(kind, dataset_id)
    assets = _dataset_dir(kind, dataset_id) / "assets"
    target = safe_join(assets, path)
    if not target.is_file():
        raise ApiError(422, "ASSET_MISSING", "媒体资产不存在")
    size = target.stat().st_size
    media_type = mimetypes.guess_type(target.name)[0] or "application/octet-stream"
    range_header = request.headers.get("range", "").strip()
    match = re.fullmatch(r"bytes=(\d*)-(\d*)", range_header)
    if match:
        start_text, end_text = match.group(1), match.group(2)
        if not start_text and not end_text:
            raise ApiError(416, "REQUEST_INVALID", "Range 请求格式不正确")
        if not start_text:
            length = int(end_text)
            start = max(0, size - length)
            end = size - 1
        else:
            start = int(start_text)
            end = int(end_text) if end_text else size - 1
            end = min(end, size - 1)
        if start >= size or start > end:
            return Response(status_code=416,
                            headers={"Content-Range": f"bytes */{size}"})
        with target.open("rb") as handle:
            handle.seek(start)
            chunk = handle.read(end - start + 1)
        return Response(
            content=chunk, status_code=206, media_type=media_type,
            headers={
                "Content-Range": f"bytes {start}-{end}/{size}",
                "Accept-Ranges": "bytes",
                "Content-Length": str(len(chunk)),
            },
        )
    return FileResponse(str(target), media_type=media_type,
                        headers={"Accept-Ranges": "bytes"})


@router.get("/{kind}/{dataset_id}")
def get_dataset(kind: str, dataset_id: str) -> dict:
    return ok(_get_dataset(kind, dataset_id))


@router.delete("/{kind}/{dataset_id}")
def delete_dataset(kind: str, dataset_id: str) -> dict:
    _get_dataset(kind, dataset_id)
    db.delete_dataset(kind, dataset_id)
    shutil.rmtree(_dataset_dir(kind, dataset_id), ignore_errors=True)
    return ok({"kind": kind, "id": dataset_id, "deleted": True})
