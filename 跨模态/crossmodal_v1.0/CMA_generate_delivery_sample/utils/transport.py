# [v4.2] 血缘/阶段归档/运输输出已停用：本模块不再被 app.py / inference.py 调用，代码保留存档，不再维护。
"""阶段内文件传递（``crossmodal-files-v1``）：文件对象打包、还原、清单与入口解析。"""
from __future__ import annotations

import base64
import hashlib
import json
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional, Sequence, Tuple

from . import io as io_utils
from . import sampling

__all__ = [
    "DEFAULT_MAX_RESPONSE_BYTES",
    "DEFAULT_PREFERRED_ENTRYPOINTS",
    "FILE_OBJECT_FORMAT",
    "MANIFEST_FORMAT",
    "MANIFEST_NAME",
    "OutputTooLargeError",
    "TransportError",
    "build_manifest",
    "load_manifest",
    "pack_file_object",
    "resolve_entrypoint",
    "restore_file_object",
    "write_manifest",
]

FILE_OBJECT_FORMAT = "crossmodal-files-v1"
MANIFEST_FORMAT = "crossmodal-manifest-v1"
MANIFEST_NAME = "manifest.json"
DEFAULT_MAX_RESPONSE_BYTES = 64 * 1024 * 1024

TEXT_SUFFIXES = {".json", ".jsonl", ".md", ".txt", ".csv", ".yaml", ".yml", ".log"}
BINARY_SUFFIXES = {".xlsx", ".zip", ".gz", ".png", ".jpg", ".jpeg", ".webp",
                   ".wav", ".mp3", ".mp4", ".bin", ".parquet"}
#: 不作为入口的旁路/说明文件（含本算子自己的产物与其他算子的材料清单）
NON_ENTRYPOINT_NAMES = set(sampling.OWN_SIDECARS) | {
    MANIFEST_NAME, "README.md", "requirements.txt",
    "23_isolation_list.jsonl", "23_quality_report.json", "version.json",
    "lineage.json", "stage_context.json",
}
NON_ENTRYPOINT_KEYWORDS = ("isolation", "quarantine", "reject", "quality_report",
                           "sampling_conclusion", "version_notes", "manifest",
                           "lineage", "stage_context", "skipped", "excluded")
DEFAULT_PREFERRED_ENTRYPOINTS: Sequence[str] = (
    sampling.MAIN_NAME, "train/samples.jsonl", "samples.jsonl", "demo.jsonl",
    "records.jsonl", "data.jsonl")


class TransportError(ValueError):
    """文件对象/清单不合法（格式、路径、重复、缺失）。"""


class OutputTooLargeError(TransportError):
    """打包结果超过响应体上限；调用方必须改用挂载目录，禁止截断正文。"""


# --------------------------------------------------------------------------- #
# 清单
# --------------------------------------------------------------------------- #

def build_manifest(entrypoint: str, artifacts: Iterable[Dict[str, Any]],
                   **extra: Any) -> Dict[str, Any]:
    """构造算子产物清单（按路径去重，保持首次出现顺序）。"""
    entries: List[Dict[str, Any]] = []
    seen: set = set()
    for artifact in artifacts:
        path = str(artifact.get("path") or "")
        if not path or path in seen:
            continue
        seen.add(path)
        entry = dict(artifact)
        entry["path"] = path
        entries.append(entry)
    payload: Dict[str, Any] = {"format": MANIFEST_FORMAT, "entrypoint": entrypoint,
                               "artifacts": entries}
    payload.update(extra)
    return payload


def write_manifest(run_dir: Path, manifest: Dict[str, Any]) -> Path:
    return io_utils.atomic_write_json(Path(run_dir) / MANIFEST_NAME, manifest)


def load_manifest(run_dir: Path) -> Dict[str, Any]:
    target = Path(run_dir) / MANIFEST_NAME
    if not target.is_file():
        return {}
    try:
        with open(target, "r", encoding="utf-8") as handle:
            payload = json.load(handle)
    except (json.JSONDecodeError, UnicodeDecodeError, OSError):
        return {}
    return payload if isinstance(payload, dict) else {}


# --------------------------------------------------------------------------- #
# 入口解析
# --------------------------------------------------------------------------- #

def _is_material_list(relative: str) -> bool:
    name = Path(relative).name
    if name in NON_ENTRYPOINT_NAMES:
        return True
    lowered = name.lower()
    return any(keyword in lowered for keyword in NON_ENTRYPOINT_KEYWORDS)


def resolve_entrypoint(run_dir: Path, preferred: Sequence[str] = (),
                       ) -> Tuple[Path, str]:
    """在运行目录中确定入口文件（优先清单声明，其次偏好名，最后唯一候选）。"""
    base = Path(run_dir).resolve()
    manifest = load_manifest(base)
    declared = manifest.get("entrypoint")
    if isinstance(declared, str) and declared.strip():
        target = (base / declared.strip().replace("\\", "/")).resolve()
        if base in target.parents and target.is_file():
            return target, target.relative_to(base).as_posix()

    names = [name for name in list(preferred) + list(DEFAULT_PREFERRED_ENTRYPOINTS) if name]
    for name in names:
        candidate = (base / name).resolve()
        if base in candidate.parents and candidate.is_file():
            return candidate, candidate.relative_to(base).as_posix()

    candidates = [path for path in io_utils.iter_candidate_files(base)
                  if path.suffix.lower() == ".jsonl"
                  and not _is_material_list(path.relative_to(base).as_posix())]
    if len(candidates) == 1:
        return candidates[0], candidates[0].relative_to(base).as_posix()
    if not candidates:
        raise TransportError("任务目录中找不到 JSONL 入口：%s" % base)
    raise TransportError("任务目录中存在多个候选 JSONL 入口，请用 manifest 或 inputFile 指定：%s"
                         % ", ".join(path.name for path in candidates[:10]))


# --------------------------------------------------------------------------- #
# 文件对象打包 / 还原
# --------------------------------------------------------------------------- #

def _safe_relative(value: Any) -> str:
    """把引用规范化为相对路径；不合法一律转成 ``TransportError``。"""
    try:
        return io_utils._normalize_relative(value)
    except io_utils.UnsupportedMediaPath as exc:
        raise TransportError(str(exc))


def _encode_file(path: Path, relative: str, max_response_bytes: int
                 ) -> Tuple[Dict[str, Any], int]:
    payload = path.read_bytes()
    suffix = path.suffix.lower()
    is_text = suffix in TEXT_SUFFIXES and suffix not in BINARY_SUFFIXES
    entry: Dict[str, Any] = {"path": relative, "size": len(payload),
                             "sha256": hashlib.sha256(payload).hexdigest()}
    if is_text:
        try:
            text = payload.decode("utf-8")
        except UnicodeDecodeError:
            is_text = False
    if is_text:
        entry["encoding"] = "utf-8"
        entry["content"] = text
    else:
        entry["encoding"] = "base64"
        entry["content"] = base64.b64encode(payload).decode("ascii")
    return entry, len(entry["content"])


def pack_file_object(run_dir: Path, entrypoint: str, files: Sequence[str],
                     max_response_bytes: int = DEFAULT_MAX_RESPONSE_BYTES
                     ) -> Dict[str, Any]:
    """把运行目录内的产物打包成 ``crossmodal-files-v1`` 文件对象。

    超过 ``max_response_bytes`` 时抛 ``OutputTooLargeError``，**不截断**正文。
    """
    base = Path(run_dir).resolve()
    entry_relative = _safe_relative(entrypoint)
    entry_path = (base / entry_relative).resolve()
    if base not in entry_path.parents or not entry_path.is_file():
        raise TransportError("entrypoint 不在运行目录内或不存在：%s" % entrypoint)

    entries: List[Dict[str, Any]] = []
    seen: set = set()
    total = 0
    for name in files:
        relative = _safe_relative(name)
        key = relative.lower()
        if key in seen:
            raise TransportError("文件对象路径重复：%s" % relative)
        seen.add(key)
        target = (base / relative).resolve()
        if base not in target.parents:
            raise TransportError("文件越出运行目录：%s" % relative)
        if not target.is_file():
            raise TransportError("文件不存在：%s" % relative)
        entry, size = _encode_file(target, relative, max_response_bytes)
        total += size
        if total > int(max_response_bytes):
            raise OutputTooLargeError(
                "响应体超过上限（%d 字节），请改用挂载目录读取" % max_response_bytes)
        entries.append(entry)

    if entry_relative not in {item["path"] for item in entries}:
        raise TransportError("文件对象缺少 entrypoint：%s" % entry_relative)
    return {"format": FILE_OBJECT_FORMAT, "entrypoint": entry_relative,
            "file_count": len(entries), "total_bytes": total, "files": entries}


def restore_file_object(payload: Dict[str, Any], dest_dir: Path) -> Path:
    """把文件对象还原到磁盘，返回入口文件路径（供阶段内下一算子读取）。"""
    if not isinstance(payload, dict) or payload.get("format") != FILE_OBJECT_FORMAT:
        raise TransportError("文件对象格式不合法（需要 %s）" % FILE_OBJECT_FORMAT)
    files = payload.get("files")
    if not isinstance(files, list) or not files:
        raise TransportError("文件对象缺少 files")
    entrypoint = payload.get("entrypoint")
    if not isinstance(entrypoint, str) or not entrypoint.strip():
        raise TransportError("文件对象缺少 entrypoint")

    dest = Path(dest_dir).resolve()
    dest.mkdir(parents=True, exist_ok=True)
    written: Dict[str, Path] = {}
    seen: set = set()
    for item in files:
        if not isinstance(item, dict):
            raise TransportError("files 元素必须是对象")
        relative = _safe_relative(item.get("path"))
        key = relative.lower()
        if key in seen:
            raise TransportError("文件对象路径重复：%s" % relative)
        seen.add(key)
        encoding = item.get("encoding")
        content = item.get("content")
        if encoding == "base64":
            if not isinstance(content, str):
                raise TransportError("base64 内容必须是字符串：%s" % relative)
            raw = base64.b64decode(content.encode("ascii"))
        elif encoding == "utf-8":
            if not isinstance(content, str):
                raise TransportError("utf-8 内容必须是字符串：%s" % relative)
            raw = content.encode("utf-8")
        else:
            raise TransportError("未知编码：%r" % (encoding,))
        target = (dest / relative).resolve()
        if dest not in target.parents:
            raise TransportError("文件越出目标目录：%s" % relative)
        io_utils.atomic_write_bytes(target, raw)
        written[relative] = target

    entry_relative = _safe_relative(entrypoint)
    if entry_relative not in written:
        raise TransportError("文件对象缺少 entrypoint：%s" % entrypoint)
    return written[entry_relative]
