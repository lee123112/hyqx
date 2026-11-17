# [v4.2] 血缘/阶段归档/运输输出已停用：mty 运输归档不再维护；
# 本模块仅保留 crossmodal-files-v1 阶段文件对象打包/恢复/入口定位（阶段链式传输仍被 app.py 使用）。
"""阶段间传输：``crossmodal-files-v1`` 文件对象、清单与入口解析。

约定（跨模态阶段统一）：
- 阶段中间算子只回传 ``lineageFile``（内含临时 ``outputFile`` 绝对路径）；
- 阶段末算子回传最终 ``outputFile`` **文件内容对象** + 完整阶段 ``lineageFile`` 数组；
- 文件对象内文本用 ``utf-8`` 明文、二进制媒体用 ``base64``，两者都由 sha256 校验。
"""
from __future__ import annotations

import base64
from datetime import datetime, timezone
from pathlib import Path, PurePosixPath
from typing import Any, Dict, Iterable, List, Optional, Tuple

from utils import io as io_utils

FILE_OBJECT_FORMAT = "crossmodal-files-v1"
MANIFEST_NAME = io_utils.MANIFEST_NAME
DEFAULT_MAX_MEDIA_BYTES = io_utils.DEFAULT_MAX_MEDIA_BYTES
DEFAULT_MAX_RESPONSE_BYTES = io_utils.DEFAULT_MAX_RESPONSE_BYTES

TEXT_SUFFIXES = {".jsonl", ".json", ".txt", ".csv", ".tsv", ".md", ".yaml", ".yml"}
TEXT_ENCODING = "utf-8"
BASE64_ENCODING = "base64"


class TransportError(RuntimeError):
    """阶段间传输不可完成（清单缺失、文件越界、编码异常等）。"""


class OutputTooLargeError(TransportError):
    """回传产物超过允许体积。"""


def _now() -> str:
    return datetime.now(timezone.utc).astimezone().isoformat(timespec="seconds")


def _normalize_member(path: Any) -> str:
    text = str(path).replace("\\", "/").strip()
    if not text:
        raise TransportError("empty member path")
    pure = PurePosixPath(text)
    if pure.is_absolute() or any(part == ".." for part in pure.parts):
        raise TransportError("member path escapes the run directory: %r" % path)
    parts = [part for part in pure.parts if part not in ("", ".")]
    if not parts:
        raise TransportError("member path normalizes to nothing: %r" % path)
    return "/".join(parts)


def _encode_member(run_dir: Path, member: str) -> Dict[str, Any]:
    target = run_dir / member
    resolved = io_utils.resolved_absolute(target)
    if not io_utils.is_within(run_dir, resolved):
        raise TransportError("member escapes the run directory: %r" % member)
    if not resolved.is_file():
        raise TransportError("member does not exist: %r" % member)

    size = resolved.stat().st_size
    if size > DEFAULT_MAX_MEDIA_BYTES:
        raise TransportError("member %s exceeds %d bytes"
                             % (member, DEFAULT_MAX_MEDIA_BYTES))
    raw = resolved.read_bytes()
    is_text = resolved.suffix.lower() in TEXT_SUFFIXES
    if is_text:
        try:
            content = raw.decode(TEXT_ENCODING)
        except UnicodeDecodeError as exc:
            raise TransportError("member %s is not valid %s: %s"
                                 % (member, TEXT_ENCODING, exc)) from exc
        encoding = TEXT_ENCODING
        payload_size = len(raw)
    else:
        content = base64.b64encode(raw).decode("ascii")
        encoding = BASE64_ENCODING
        payload_size = len(raw)
    return {
        "path": member,
        "encoding": encoding,
        "bytes": payload_size,
        "sha256": io_utils.sha256_bytes(raw),
        "content": content,
    }


def pack_file_object(run_dir, entrypoint: str, members: Iterable[str], *,
                     max_response_bytes: int = DEFAULT_MAX_RESPONSE_BYTES) -> Dict[str, Any]:
    """把产物目录打成一个 ``crossmodal-files-v1`` 文件内容对象。"""
    run_path = io_utils.resolved_absolute(run_dir)
    if not run_path.is_dir():
        raise TransportError("run directory does not exist: %s" % run_path)

    normalized_entry = _normalize_member(entrypoint)
    ordered: List[str] = []
    seen = set()
    for member in members:
        name = _normalize_member(member)
        if name in seen:
            continue
        seen.add(name)
        ordered.append(name)
    if normalized_entry not in seen:
        raise TransportError("entrypoint %r is not part of the packed files"
                             % entrypoint)

    # 入口文件放最前，便于下游直接流式读取
    ordered.sort(key=lambda name: (name != normalized_entry, name))

    files: List[Dict[str, Any]] = []
    total = 0
    for name in ordered:
        encoded = _encode_member(run_path, name)
        total += encoded["bytes"]
        if total > max_response_bytes:
            raise OutputTooLargeError(
                "packed output exceeds %d bytes" % max_response_bytes)
        files.append(encoded)

    return {
        "format": FILE_OBJECT_FORMAT,
        "entrypoint": normalized_entry,
        "files": files,
        "totalBytes": total,
        "generatedAt": _now(),
    }


def restore_file_object(payload: Any, destination) -> Path:
    """把文件内容对象还原到磁盘；目标目录必须为空。"""
    if not isinstance(payload, dict):
        raise TransportError("file object must be an object")
    if payload.get("format") != FILE_OBJECT_FORMAT:
        raise TransportError("unsupported file object format: %r"
                             % payload.get("format"))
    files = payload.get("files")
    if not isinstance(files, list) or not files:
        raise TransportError("file object contains no files")

    entrypoint = _normalize_member(payload.get("entrypoint") or "")
    target_root = io_utils.resolved_absolute(destination)
    if target_root.exists():
        if not target_root.is_dir():
            raise TransportError("restore destination is not a directory")
        if any(target_root.iterdir()):
            raise TransportError("restore destination must be empty: %s" % target_root)
    else:
        target_root.mkdir(parents=True, exist_ok=True)

    seen = set()
    entry_path: Optional[Path] = None
    for item in files:
        if not isinstance(item, dict):
            raise TransportError("file entry must be an object")
        name = _normalize_member(item.get("path"))
        key = name.lower()
        if key in seen:
            raise TransportError("duplicated file entry: %r" % name)
        seen.add(key)
        encoding = item.get("encoding")
        content = item.get("content")
        if encoding == TEXT_ENCODING:
            if not isinstance(content, str):
                raise TransportError("text content must be a string: %r" % name)
            data = content.encode(TEXT_ENCODING)
        elif encoding == BASE64_ENCODING:
            if not isinstance(content, str):
                raise TransportError("base64 content must be a string: %r" % name)
            try:
                data = base64.b64decode(content, validate=True)
            except Exception as exc:  # noqa: BLE001 - 统一转成传输层错误
                raise TransportError("invalid base64 for %r: %s" % (name, exc)) from exc
        else:
            raise TransportError("unsupported encoding %r for %r" % (encoding, name))

        if "sha256" in item and item["sha256"] and io_utils.sha256_bytes(data) != item["sha256"]:
            raise TransportError("checksum mismatch for %r" % name)

        target = target_root.joinpath(*name.split("/"))
        if not io_utils.is_within(target_root, io_utils.resolved_absolute(target)):
            raise TransportError("file entry escapes the destination: %r" % name)
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(data)
        if name == entrypoint:
            entry_path = target

    if entry_path is None:
        raise TransportError("entrypoint %r is missing from the file object" % entrypoint)
    return entry_path


# --------------------------------------------------------------------------- #
# 清单
# --------------------------------------------------------------------------- #

def build_manifest(entrypoint: str, artifacts: Iterable[Dict[str, Any]]) -> Dict[str, Any]:
    """构造入口清单；``manifest.json`` 自身不入列，避免自我引用。"""
    normalized_entry = _normalize_member(entrypoint)
    listed: List[Dict[str, Any]] = []
    seen = set()
    for artifact in artifacts:
        if not isinstance(artifact, dict) or "path" not in artifact:
            raise TransportError("artifact must be an object with a path")
        name = _normalize_member(artifact["path"])
        if name == MANIFEST_NAME or name in seen:
            continue
        seen.add(name)
        listed.append({"path": name, "role": str(artifact.get("role") or "artifact")})
    return {
        "format": "crossmodal-manifest-v1",
        "entrypoint": normalized_entry,
        "artifacts": listed,
        "generatedAt": _now(),
    }


def write_manifest(run_dir, manifest: Dict[str, Any]) -> Path:
    return io_utils.atomic_write_json(Path(run_dir) / MANIFEST_NAME, manifest)


def load_manifest(directory) -> Optional[Dict[str, Any]]:
    path = Path(directory) / MANIFEST_NAME
    if not path.is_file():
        return None
    try:
        return io_utils.read_json_limited(path)
    except Exception as exc:  # noqa: BLE001 - 清单损坏按缺失处理并保持可追溯
        raise TransportError("manifest is unreadable: %s" % exc) from exc


def resolve_entrypoint(directory) -> Tuple[Path, str]:
    """确定输入入口：优先清单声明的 entrypoint，否则要求目录内 JSONL 唯一。"""
    root = io_utils.resolved_absolute(directory)
    if not root.is_dir():
        raise TransportError("input directory does not exist: %s" % root)

    manifest = load_manifest(root)
    if manifest is not None:
        declared = manifest.get("entrypoint")
        if not isinstance(declared, str) or not declared.strip():
            raise TransportError("manifest does not declare an entrypoint")
        try:
            name = _normalize_member(declared)
        except TransportError as exc:
            raise TransportError("manifest entrypoint is unsafe: %s" % exc) from exc
        candidate = root.joinpath(*name.split("/"))
        if not io_utils.is_within(root, io_utils.resolved_absolute(candidate)):
            raise TransportError("manifest entrypoint escapes the input directory")
        if not candidate.is_file():
            raise TransportError("manifest entrypoint does not exist: %s" % candidate)
        return candidate, name

    candidates = sorted(path for path in root.glob("*.jsonl") if path.is_file())
    if not candidates:
        raise TransportError("no JSONL entrypoint found in %s" % root)
    if len(candidates) > 1:
        raise TransportError(
            "multiple JSONL files found and no manifest.json to disambiguate: %s"
            % ", ".join(path.name for path in candidates))
    return candidates[0], candidates[0].name


def media_files_in(directory) -> List[str]:
    """列出目录内（递归）可搬运的媒体/侧车文件，返回相对 posix 路径。"""
    root = io_utils.resolved_absolute(directory)
    found: List[str] = []
    for path in sorted(root.rglob("*")):
        if not path.is_file() or path.name == MANIFEST_NAME:
            continue
        suffix = path.suffix.lower()
        if suffix in TEXT_SUFFIXES and suffix != ".jsonl":
            found.append(path.relative_to(root).as_posix())
        elif suffix not in TEXT_SUFFIXES:
            found.append(path.relative_to(root).as_posix())
    return found
