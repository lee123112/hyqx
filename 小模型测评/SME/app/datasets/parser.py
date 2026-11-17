"""Parse uploaded dataset packages for the operator entrypoints."""
from __future__ import annotations

import csv
import io
import json
import zipfile
from pathlib import Path
from typing import Any

from ..core.config import settings
from ..core.envelope import ApiError
from ..core.security import safe_join, sanitize_filename, logical_path

MEASUREMENT_COLUMNS = {
    "baseline_accuracy": ("baseline", "accuracy"),
    "candidate_accuracy": ("candidate", "accuracy"),
    "baseline_f1": ("baseline", "f1"),
    "candidate_f1": ("candidate", "f1"),
    "baseline_lossCv": ("baseline", "lossCv"),
    "candidate_lossCv": ("candidate", "lossCv"),
    "baseline_iou": ("baseline", "iou"),
    "candidate_iou": ("candidate", "iou"),
    "baseline_cer": ("baseline", "cer"),
    "candidate_cer": ("candidate", "cer"),
    "baseline_speakerAccuracy": ("baseline", "speakerAccuracy"),
    "candidate_speakerAccuracy": ("candidate", "speakerAccuracy"),
    "baseline_domainF1": ("baseline", "domainF1"),
    "candidate_domainF1": ("candidate", "domainF1"),
    "baseline_score": ("baseline", "score"),
    "candidate_score": ("candidate", "score"),
}


def _num(value: Any) -> float | int | None:
    if value is None or value == "":
        return None
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    return int(number) if number.is_integer() else number


def _extract_zip(archive: Path, target: Path) -> list[Path]:
    target.mkdir(parents=True, exist_ok=True)
    files: list[Path] = []
    with zipfile.ZipFile(archive) as handle:
        members = handle.infolist()
        if len(members) > settings.max_zip_files:
            raise ApiError(413, "PAYLOAD_TOO_LARGE", "压缩包文件数量超过限制")
        for member in members:
            name = member.filename.replace("\\", "/")
            if not name or name.endswith("/"):
                continue
            if name.startswith("/") or ".." in Path(name).parts or member.is_dir():
                raise ApiError(422, "DATASET_INVALID", "压缩包包含非法路径")
            if (member.external_attr >> 16) & 0o170000 == 0o120000:
                raise ApiError(422, "DATASET_INVALID", "压缩包不允许符号链接")
            destination = safe_join(target, name)
            destination.parent.mkdir(parents=True, exist_ok=True)
            with handle.open(member) as source, destination.open("wb") as output:
                output.write(source.read())
            files.append(destination)
    return files


def _load_rows(path: Path) -> list[dict[str, Any]]:
    suffix = path.suffix.lower()
    if suffix == ".json":
        data = json.loads(path.read_text(encoding="utf-8-sig"))
        rows = data.get("samples") if isinstance(data, dict) else data
        if isinstance(data, dict) and isinstance(data.get("data"), dict):
            rows = data["data"].get("samples")
        if not isinstance(rows, list):
            raise ApiError(422, "DATASET_INVALID", "JSON 数据集必须包含 samples 数组")
        return [row for row in rows if isinstance(row, dict)]
    if suffix in (".jsonl", ".ndjson"):
        rows = []
        for line in path.read_text(encoding="utf-8-sig").splitlines():
            line = line.strip()
            if line:
                item = json.loads(line)
                if isinstance(item, dict):
                    rows.append(item)
        return rows
    if suffix == ".csv":
        text = path.read_text(encoding="utf-8-sig")
        reader = csv.DictReader(io.StringIO(text))
        rows = []
        for raw in reader:
            item: dict[str, Any] = {
                "id": raw.get("id") or raw.get("sample_id"),
                "input": raw.get("input") or raw.get("source") or raw.get("question") or raw.get("instruction"),
                "output": raw.get("output") or raw.get("answer"),
                "title": raw.get("title"), "scene": raw.get("scene"),
                "sourceStep": raw.get("sourceStep") or raw.get("source_step"),
                "file": raw.get("file") or raw.get("fileName") or raw.get("uri"),
                "speaker": raw.get("speaker"), "domain": raw.get("domain"),
                "audio": raw.get("audio"), "text": raw.get("text"),
                "transcript": raw.get("transcript"), "reference": raw.get("reference"),
                "speaker_id": raw.get("speaker_id"), "speakerId": raw.get("speakerId"),
                "language": raw.get("language"),
                "highValue": str(raw.get("highValue", "")).lower() in ("1", "true", "yes"),
            }
            measurements: dict[str, dict[str, Any]] = {"baseline": {}, "candidate": {}}
            for column, (model, metric) in MEASUREMENT_COLUMNS.items():
                if raw.get(column) not in (None, ""):
                    measurements[model][metric] = _num(raw[column])
            if raw.get("measurements"):
                try:
                    parsed = json.loads(raw["measurements"])
                    if isinstance(parsed, dict):
                        measurements = parsed
                except ValueError:
                    pass
            if measurements["baseline"] or measurements["candidate"]:
                item["measurements"] = measurements
            rows.append({key: value for key, value in item.items() if value not in (None, "")})
        return rows
    if suffix == ".txt":
        return [
            {"id": f"S{index:03d}", "input": line.strip()}
            for index, line in enumerate(path.read_text(encoding="utf-8-sig").splitlines(), start=1)
            if line.strip()
        ]
    raise ApiError(422, "DATASET_INVALID", "仅支持 JSON/JSONL/CSV/TXT/ZIP 数据集")


def _attach_media(rows: list[dict[str, Any]], base: Path, logical_root: str,
                  archive_root: Path | None = None) -> list[dict[str, Any]]:
    for row in rows:
        raw = row.get("file") or row.get("fileName") or row.get("uri") or row.get("audio") or row.get("path")
        input_path = Path(str(row.get('input') or ''))
        if not raw and not input_path.is_absolute() and input_path.suffix.lower() in (
                ".png", ".jpg", ".jpeg", ".webp", ".bmp", ".wav", ".mp3", ".flac",
                ".mp4", ".mov", ".webm", ".avi", ".mkv"):
            raw = row.get("input")
        if not raw or str(raw).startswith(("http://", "https://", "/workspace/")):
            continue
        candidate = safe_join(base, str(raw))
        if not candidate.exists() and archive_root is not None:
            candidate = safe_join(archive_root, str(raw))
        if not candidate.is_file():
            continue
        row["file"] = str(candidate)
        row["fileName"] = Path(str(raw)).name
        row["uri"] = logical_path(candidate, "")
        if row.get("input") == raw:
            row["input"] = row["uri"]
        if row.get("audio") == raw:
            row["audio"] = row["uri"]
    return rows


def parse_dataset(path: Path, unique_id: str, input_root: Path) -> dict[str, Any]:
    """Return {samples, files, base, name, kind}."""
    if not path.is_file():
        raise ApiError(404, "NOT_FOUND", "数据集文件不存在")
    if path.stat().st_size > settings.max_upload_mb * 1024 * 1024:
        raise ApiError(413, "PAYLOAD_TOO_LARGE", "数据集文件超过大小限制")
    base = path.parent
    files: list[Path] = []
    name = path.stem
    archive_root = None
    if path.suffix.lower() == ".zip":
        base = input_root / "extracted"
        archive_root = base
        files = _extract_zip(path, base)
        manifests = [
            item for item in files
            if item.suffix.lower() in (".json", ".jsonl", ".ndjson", ".csv")
            and item.name.lower() in ("dataset.json", "samples.json", "manifest.json",
                                      "dataset.jsonl", "samples.jsonl", "dataset.csv",
                                      "samples.csv")
        ]
        if not manifests:
            manifests = [item for item in files if item.suffix.lower() in (".json", ".jsonl", ".ndjson", ".csv")]
        if not manifests:
            raise ApiError(422, "DATASET_INVALID", "压缩包缺少 JSON/JSONL/CSV 样本清单")
        manifest = manifests[0]
        rows = _load_rows(manifest)
        base = manifest.parent
        logical_root = f"/workspace/{unique_id}/input/extracted"
        name = manifest.stem
    else:
        rows = _load_rows(path)
        logical_root = f"/workspace/{unique_id}/input"

    rows = _attach_media(rows, base, logical_root, archive_root)
    if not rows:
        raise ApiError(422, "DATASET_INVALID", "数据集没有可用样本")
    if len(rows) > settings.max_samples:
        raise ApiError(413, "PAYLOAD_TOO_LARGE", f"单次最多 {settings.max_samples} 条样本")
    media = [item for item in (files or list(base.iterdir()))
             if item.is_file() and item.suffix.lower() in
             (".jpg", ".jpeg", ".png", ".webp", ".bmp", ".wav", ".mp3", ".flac",
              ".mp4", ".mov", ".webm", ".avi", ".mkv")]
    return {"samples": rows, "files": [str(item) for item in media], "base": str(base),
            "name": name, "kind": path.suffix.lower().lstrip(".") or "dir"}
