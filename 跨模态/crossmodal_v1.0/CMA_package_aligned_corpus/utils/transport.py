# [v4.2] 血缘/阶段归档/运输输出已停用：本模块不再被 app.py / inference.py 调用，代码保留存档，不再维护。
"""接口文件内容对象与运行清单（跨模态共享格式，禁止改动字段语义）。

- 文件内容对象：``{"format": "crossmodal-files-v1", "entrypoint": ..., "files": [...]}``
  文本文件用 ``utf-8`` 原样内联，二进制/媒体/交付 ZIP 用 ``base64``；
- 运行清单：``manifest.json``（``crossmodal-manifest-v1``），记录本次运行全部产物。
"""
from __future__ import annotations

import base64
import json
import os
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional, Sequence, Tuple

from . import delivery
from . import io as io_utils

__all__ = [
    "ENTRYPOINT_SUFFIXES",
    "FILE_OBJECT_FORMAT",
    "MANIFEST_FORMAT",
    "MANIFEST_NAME",
    "MAX_RESPONSE_BYTES",
    "MEDIA_SUFFIXES",
    "NON_ENTRYPOINT_NAMES",
    "OWN_SIDECARS",
    "OutputTooLargeError",
    "PREFERRED_ENTRYPOINTS",
    "TEXT_SUFFIXES",
    "TransportError",
    "build_manifest",
    "iter_manifest_paths",
    "load_manifest",
    "pack_file_object",
    "resolve_entrypoint",
    "restore_file_object",
    "role_for",
    "write_manifest",
]

FILE_OBJECT_FORMAT = "crossmodal-files-v1"
MANIFEST_FORMAT = "crossmodal-manifest-v1"
MANIFEST_NAME = "manifest.json"

MAX_RESPONSE_BYTES = 64 * 1024 * 1024

TEXT_SUFFIXES = {".jsonl", ".json", ".txt", ".csv", ".md", ".yaml", ".yml", ".log"}
MEDIA_SUFFIXES = {".png", ".jpg", ".jpeg", ".bmp", ".webp", ".gif", ".tif", ".tiff",
                  ".mp4", ".avi", ".mov", ".mkv", ".webm", ".wav", ".mp3", ".flac"}
ENTRYPOINT_SUFFIXES = {".jsonl"}

PREFERRED_ENTRYPOINTS = ("demo.jsonl", "samples.jsonl", "tokens.jsonl",
                         "pairs.jsonl", "candidates.jsonl", "records.jsonl",
                         "input.jsonl")

#: 本算子产物名：不作为「自动识别入口」的候选（避免把旁路台账当业务入口）
OWN_SIDECARS: Sequence[str] = tuple(delivery.OWN_SIDECARS)
NON_ENTRYPOINT_NAMES: Sequence[str] = tuple(delivery.OWN_SIDECARS)


class TransportError(ValueError):
    """文件内容对象或清单不合法。"""


class OutputTooLargeError(TransportError):
    """单次响应超过体积上限。"""


def role_for(path: str) -> str:
    name = os.path.basename(path)
    if name == MANIFEST_NAME:
        return "sidecar"
    suffix = os.path.splitext(name)[1].lower()
    if suffix == ".zip":
        return "package"
    if suffix in MEDIA_SUFFIXES:
        return "media"
    if suffix == ".jsonl":
        return "records"
    if suffix == ".json":
        return "sidecar"
    return "other"


def _safe_relative(value: str) -> str:
    try:
        return io_utils._normalize_relative(value)
    except io_utils.UnsupportedMediaPath as exc:
        raise TransportError(str(exc))


def _is_material_list(name: str) -> bool:
    return (os.path.splitext(name)[1].lower() == ".jsonl"
            and delivery.classify_material_name(name) is not None)


# --------------------------------------------------------------------------- #
# 文件内容对象
# --------------------------------------------------------------------------- #

def pack_file_object(run_dir: Path, entrypoint: str, paths: Sequence[str],
                     max_response_bytes: int = MAX_RESPONSE_BYTES) -> Dict[str, Any]:
    """把运行目录内的一组文件打包成 ``crossmodal-files-v1`` 对象。"""
    base = Path(run_dir).resolve()
    normalized_entry = _safe_relative(entrypoint)
    normalized_paths: List[str] = []
    seen = set()
    for item in paths:
        relative = _safe_relative(item)
        if relative in seen:
            continue
        seen.add(relative)
        normalized_paths.append(relative)
    if normalized_entry not in seen:
        raise TransportError("entrypoint 必须包含在 files 中：%r" % entrypoint)

    files: List[Dict[str, Any]] = []
    total = 0
    for relative in normalized_paths:
        source = (base / relative).resolve()
        if base not in source.parents and source != base:
            raise TransportError("文件越出运行目录：%r" % relative)
        if not source.is_file():
            raise TransportError("文件不存在：%r" % relative)
        raw = source.read_bytes()
        total += len(raw)
        if total > max_response_bytes:
            raise OutputTooLargeError(
                "单次响应超过上限 %d 字节（已打包 %d 字节）" % (max_response_bytes, total))
        suffix = os.path.splitext(relative)[1].lower()
        if suffix in TEXT_SUFFIXES:
            try:
                content = raw.decode("utf-8")
            except UnicodeDecodeError:
                content = base64.b64encode(raw).decode("ascii")
                encoding = "base64"
            else:
                encoding = "utf-8"
        else:
            content = base64.b64encode(raw).decode("ascii")
            encoding = "base64"
        files.append({"path": relative, "role": role_for(relative),
                      "encoding": encoding, "size": len(raw), "content": content})

    return {"format": FILE_OBJECT_FORMAT, "entrypoint": normalized_entry,
            "files": files, "totalBytes": total}


def restore_file_object(payload: Any, dest_dir: Path) -> Path:
    """把文件内容对象写回目录，返回 entrypoint 的绝对路径。"""
    if not isinstance(payload, dict):
        raise TransportError("文件内容对象必须是 JSON 对象")
    if payload.get("format") != FILE_OBJECT_FORMAT:
        raise TransportError("文件内容对象 format 必须是 %s" % FILE_OBJECT_FORMAT)
    entrypoint = _safe_relative(payload.get("entrypoint") or "")
    files = payload.get("files")
    if not isinstance(files, list) or not files:
        raise TransportError("文件内容对象缺少 files")

    dest = Path(dest_dir).resolve()
    dest.mkdir(parents=True, exist_ok=True)
    seen = set()
    entry_path: Optional[Path] = None
    for item in files:
        if not isinstance(item, dict):
            raise TransportError("files 条目必须是对象")
        relative = _safe_relative(item.get("path") or "")
        key = relative.lower()
        if key in seen:
            raise TransportError("files 存在重复路径：%r" % relative)
        seen.add(key)
        target = (dest / relative).resolve()
        if dest not in target.parents:
            raise TransportError("files 条目越出目标目录：%r" % relative)
        content = item.get("content")
        if not isinstance(content, str):
            raise TransportError("files 条目缺少 content：%r" % relative)
        encoding = item.get("encoding") or "utf-8"
        if encoding == "base64":
            try:
                raw = base64.b64decode(content, validate=True)
            except Exception as exc:                     # noqa: BLE001
                raise TransportError("base64 解码失败：%r（%s）" % (relative, exc))
        elif encoding == "utf-8":
            raw = content.encode("utf-8")
        else:
            raise TransportError("不支持的 encoding：%r" % encoding)
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(raw)
        if relative == entrypoint:
            entry_path = target
    if entry_path is None:
        raise TransportError("entrypoint 不在 files 中：%r" % entrypoint)
    return entry_path


# --------------------------------------------------------------------------- #
# 运行清单
# --------------------------------------------------------------------------- #

def build_manifest(entrypoint: str, artifacts: Iterable[Dict[str, Any]]) -> Dict[str, Any]:
    """构造 manifest；按路径去重并跳过 manifest 自身。"""
    items: List[Dict[str, Any]] = []
    seen = set()
    for artifact in artifacts:
        if not isinstance(artifact, dict):
            continue
        path = artifact.get("path")
        if not isinstance(path, str) or not path:
            continue
        relative = _safe_relative(path)
        if relative == MANIFEST_NAME or relative in seen:
            continue
        seen.add(relative)
        item = {"path": relative, "role": artifact.get("role") or role_for(relative)}
        if artifact.get("size") is not None:
            item["size"] = int(artifact["size"])
        items.append(item)
    return {"format": MANIFEST_FORMAT, "entrypoint": _safe_relative(entrypoint),
            "artifacts": items}


def iter_manifest_paths(manifest: Dict[str, Any]) -> List[str]:
    artifacts = manifest.get("artifacts") or []
    return [item["path"] for item in artifacts
            if isinstance(item, dict) and isinstance(item.get("path"), str)]


def write_manifest(run_dir: Path, manifest: Dict[str, Any]) -> Path:
    return io_utils.atomic_write_json(Path(run_dir) / MANIFEST_NAME, manifest)


def load_manifest(run_dir: Path) -> Dict[str, Any]:
    path = Path(run_dir) / MANIFEST_NAME
    try:
        with open(path, "r", encoding="utf-8") as handle:
            payload = json.load(handle)
    except FileNotFoundError:
        raise TransportError("清单不存在：%s" % path)
    except (json.JSONDecodeError, UnicodeDecodeError) as exc:
        raise TransportError("清单不是合法 JSON：%s（%s）" % (path, exc))
    if not isinstance(payload, dict) or payload.get("format") != MANIFEST_FORMAT:
        raise TransportError("清单 format 必须是 %s" % MANIFEST_FORMAT)
    return payload


def resolve_entrypoint(run_dir: Path) -> Tuple[Path, str]:
    """定位输入包的主 JSONL：优先清单，其次唯一的 ``.jsonl``。

    自动识别时会排除本算子的旁路台账与隔离清单；确实只剩本算子入口文件时
    （例如把上一次运行目录直接当输入）才回退使用它，并优先 ``samples.jsonl`` 等
    上游约定名。
    """
    base = Path(run_dir).resolve()
    manifest_path = base / MANIFEST_NAME
    if manifest_path.is_file():
        manifest = load_manifest(base)
        entrypoint = _safe_relative(manifest.get("entrypoint") or "")
        target = (base / entrypoint).resolve()
        if not target.is_file():
            raise TransportError("清单声明的 entrypoint 不存在：%r" % entrypoint)
        return target, entrypoint

    candidates = sorted(
        path for path in base.rglob("*.jsonl")
        if path.is_file() and path.name not in NON_ENTRYPOINT_NAMES
        and not _is_material_list(path.name)
    )
    if not candidates:
        fallback = [base / name for name in (delivery.MAIN_NAME,) if (base / name).is_file()]
        if fallback:
            target = fallback[0]
            return target, target.relative_to(base).as_posix()
        raise TransportError("任务目录中没有找到 .jsonl 输入文件：%s" % base)
    if len(candidates) > 1:
        preferred = [path for path in candidates if path.name in PREFERRED_ENTRYPOINTS]
        if len(preferred) == 1:
            candidates = preferred
        elif len(preferred) > 1:
            candidates = preferred
        else:
            raise TransportError("任务目录中存在多个 .jsonl，无法确定入口：%s"
                                 % ", ".join(path.name for path in candidates))
    target = candidates[0]
    return target, target.relative_to(base).as_posix()
