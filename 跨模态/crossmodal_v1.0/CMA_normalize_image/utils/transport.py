# [v4.2] 血缘/阶段归档/运输输出已停用：本模块仅保留 crossmodal-files-v1 打包/运行清单能力（阶段链式传输仍需要），血缘运输相关代码保留存档，不再维护。
"""跨模态 demo 传输适配层：文件内容对象与运行清单。

> 本格式 ``crossmodal-files-v1`` 是并行开发阶段选定的 demo 约定，
> 不是甲方正式协议；正式二进制承载协议到位时只替换本适配层。
> 限额 ``max_response_bytes`` 默认 64MiB 同为 demo 限额。

职责：
- ``pack_file_object``：把一次运行目录内被记录/清单引用的文件打成文件内容对象；
- ``restore_file_object``：把内容对象严格恢复到全新空目录，供媒体可读性验证；
- ``build_manifest`` / ``load_manifest``：运行目录的 ``crossmodal-manifest-v1`` 清单。
"""
from __future__ import annotations

import base64
import binascii
import json
import os
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional, Tuple

FILE_OBJECT_FORMAT = "crossmodal-files-v1"
MANIFEST_FORMAT = "crossmodal-manifest-v1"
MANIFEST_NAME = "manifest.json"
ENTRYPOINT_NAMES = ("demo.jsonl", "samples.jsonl")

#: 以 UTF-8 文本承载的扩展名；其余按 base64 二进制承载
TEXT_SUFFIXES = {
    ".json", ".jsonl", ".csv", ".tsv", ".txt", ".md", ".yaml", ".yml", ".log",
}
#: 需要随包搬运的媒体扩展名
MEDIA_SUFFIXES = {
    ".jpg", ".jpeg", ".png", ".bmp", ".gif", ".webp",
    ".mp4", ".mov", ".avi", ".mkv",
    ".mp3", ".wav", ".flac", ".aiff", ".aif", ".m4a",
    ".pdf", ".docx",
}


class TransportError(ValueError):
    """文件内容对象或清单非法。"""


class OutputTooLargeError(TransportError):
    """阶段末文件内容对象超过响应字节限额。"""


def is_text_path(relative_path: str) -> bool:
    return Path(relative_path).suffix.lower() in TEXT_SUFFIXES


def is_media_path(relative_path: str) -> bool:
    return Path(relative_path).suffix.lower() in MEDIA_SUFFIXES


def _validate_relative(name: str) -> str:
    if not isinstance(name, str) or not name.strip():
        raise TransportError("文件路径不能为空")
    value = name.replace("\\", "/").strip()
    if value.startswith("/") or Path(value).is_absolute():
        raise TransportError("文件对象不允许绝对路径：%s" % name)
    parts = [part for part in value.split("/") if part not in ("", ".")]
    if not parts or any(part == ".." for part in parts):
        raise TransportError("文件对象路径非法：%s" % name)
    return "/".join(parts)


def build_manifest(entrypoint: str, artifacts: Iterable[Dict[str, str]]) -> Dict[str, Any]:
    """构造 ``crossmodal-manifest-v1``；所有实际传递文件都必须列出。"""
    listed: List[Dict[str, str]] = []
    seen = set()
    for artifact in artifacts:
        path = _validate_relative(artifact["path"])
        if path == MANIFEST_NAME:
            continue
        if path in seen:
            continue
        seen.add(path)
        listed.append({"path": path, "role": artifact.get("role", "sidecar")})
    return {
        "format": MANIFEST_FORMAT,
        "entrypoint": _validate_relative(entrypoint),
        "artifacts": listed,
    }


def write_manifest(directory, manifest: Dict[str, Any]) -> Path:
    from utils.io import atomic_write_json

    return atomic_write_json(Path(directory) / MANIFEST_NAME, manifest)


def load_manifest(directory) -> Optional[Dict[str, Any]]:
    path = Path(directory) / MANIFEST_NAME
    if not path.is_file():
        return None
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError) as exc:
        raise TransportError("manifest.json 无法解析：%s" % type(exc).__name__) from exc
    if not isinstance(payload, dict) or payload.get("format") != MANIFEST_FORMAT:
        raise TransportError("manifest.json 格式不是 %s" % MANIFEST_FORMAT)
    return payload


def resolve_entrypoint(directory, manifest: Optional[Dict[str, Any]] = None) -> Tuple[Path, Optional[str]]:
    """定位入口 JSONL：有 manifest 用其 entrypoint，否则只接受唯一候选。"""
    base = Path(directory)
    payload = manifest if manifest is not None else load_manifest(base)
    if payload is not None:
        entrypoint = payload.get("entrypoint")
        if not isinstance(entrypoint, str) or not entrypoint:
            raise TransportError("manifest.json 缺少 entrypoint")
        target = base / _validate_relative(entrypoint)
        if not target.is_file():
            raise TransportError("manifest entrypoint 不存在：%s" % entrypoint)
        return target, entrypoint

    candidates = []
    for path in sorted(base.rglob("*.jsonl")):
        relative = path.relative_to(base)
        if not any(part.startswith(".") for part in relative.parts):
            candidates.append(path)
    named = [path for path in candidates if path.name in ENTRYPOINT_NAMES]
    if len(named) == 1:
        return named[0], str(named[0].relative_to(base)).replace("\\", "/")
    if len(candidates) == 1:
        return candidates[0], str(candidates[0].relative_to(base)).replace("\\", "/")
    if not candidates:
        raise TransportError("素材包内没有可识别的 JSONL 入口，且缺少 manifest.json")
    raise TransportError("素材包存在多个 JSONL 入口，必须用 manifest.json 指定 entrypoint")


def collect_payload_files(directory, relative_paths: Iterable[str]) -> List[Tuple[str, Path]]:
    """把相对路径解析为 (相对路径, 实际文件)，拒绝逃逸与缺失。"""
    base = Path(directory)
    resolved: List[Tuple[str, Path]] = []
    for raw in relative_paths:
        relative = _validate_relative(raw)
        target = (base / relative).resolve()
        try:
            target.relative_to(base.resolve())
        except ValueError as exc:
            raise TransportError("文件路径越出运行目录：%s" % raw) from exc
        if not target.is_file():
            raise TransportError("待打包文件不存在：%s" % raw)
        resolved.append((relative, target))
    return resolved


def encode_file(relative_path: str, path: Path) -> Dict[str, Any]:
    raw = path.read_bytes()
    if is_text_path(relative_path):
        try:
            content = raw.decode("utf-8")
        except UnicodeDecodeError as exc:
            raise TransportError("文本文件不是 UTF-8：%s" % relative_path) from exc
        return {"path": relative_path, "encoding": "utf-8", "content": content}
    return {
        "path": relative_path,
        "encoding": "base64",
        "content": base64.b64encode(raw).decode("ascii"),
    }


def pack_file_object(directory, entrypoint: str, relative_paths: Iterable[str],
                     max_response_bytes: int = 67108864) -> Dict[str, Any]:
    """构造阶段末文件内容对象；超限直接失败，不截断。"""
    entry_relative = _validate_relative(entrypoint)
    files = [
        encode_file(relative, path)
        for relative, path in collect_payload_files(directory, relative_paths)
    ]
    if not any(item["path"] == entry_relative for item in files):
        raise TransportError("文件内容对象缺少入口文件：%s" % entry_relative)
    payload = {"format": FILE_OBJECT_FORMAT, "entrypoint": entry_relative, "files": files}
    size = len(json.dumps(payload, ensure_ascii=False).encode("utf-8"))
    if size > max_response_bytes:
        raise OutputTooLargeError(
            "输出文件内容对象 %d 字节超过限额 %d 字节" % (size, max_response_bytes))
    return payload


def restore_file_object(payload: Dict[str, Any], destination) -> Path:
    """把文件内容对象严格恢复到全新空目录，返回入口路径。"""
    if not isinstance(payload, dict) or payload.get("format") != FILE_OBJECT_FORMAT:
        raise TransportError("data.outputFile 不是 %s 对象" % FILE_OBJECT_FORMAT)
    files = payload.get("files")
    if not isinstance(files, list) or not files:
        raise TransportError("文件内容对象 files 不能为空")

    dest = Path(destination)
    dest.mkdir(parents=True, exist_ok=True)
    if any(dest.iterdir()):
        raise TransportError("恢复目标目录必须为空")

    seen = set()
    entrypoint: Optional[str] = None
    for item in files:
        if not isinstance(item, dict):
            raise TransportError("files 元素必须是对象")
        relative = _validate_relative(item.get("path", ""))
        lowered = relative.lower()
        if lowered in seen:
            raise TransportError("文件对象存在重复路径：%s" % relative)
        seen.add(lowered)
        encoding = item.get("encoding")
        content = item.get("content", "")
        if not isinstance(content, str):
            raise TransportError("文件内容必须是字符串：%s" % relative)
        if encoding == "utf-8":
            raw = content.encode("utf-8")
        elif encoding == "base64":
            try:
                raw = base64.b64decode(content, validate=True)
            except (binascii.Error, ValueError) as exc:
                raise TransportError("base64 解码失败：%s" % relative) from exc
        else:
            raise TransportError("不支持的内容编码：%s" % encoding)
        target = dest / relative
        target.parent.mkdir(parents=True, exist_ok=True)
        with open(target, "wb") as stream:
            stream.write(raw)
        if relative == _validate_relative(payload.get("entrypoint", "")):
            entrypoint = relative

    if entrypoint is None:
        raise TransportError("文件内容对象缺少入口文件")
    return dest / entrypoint


def atomic_pack_file_object(directory, entrypoint: str, relative_paths: Iterable[str],
                            max_response_bytes: int = 67108864) -> Dict[str, Any]:
    """与 ``pack_file_object`` 相同，但把文件读取失败统一转为 TransportError。"""
    try:
        return pack_file_object(directory, entrypoint, relative_paths, max_response_bytes)
    except OSError as exc:
        raise TransportError("读取待打包文件失败：%s" % type(exc).__name__) from exc


def file_sha256(path) -> str:
    import hashlib

    digest = hashlib.sha256()
    with open(path, "rb") as stream:
        for chunk in iter(lambda: stream.read(65536), b""):
            digest.update(chunk)
    return digest.hexdigest()


def iter_manifest_paths(manifest: Dict[str, Any]) -> List[str]:
    entrypoint = manifest.get("entrypoint")
    paths = [entrypoint] if isinstance(entrypoint, str) else []
    for artifact in manifest.get("artifacts") or []:
        if isinstance(artifact, dict) and isinstance(artifact.get("path"), str):
            paths.append(artifact["path"])
    ordered: List[str] = []
    for path in paths:
        if path not in ordered:
            ordered.append(path)
    return ordered


def directory_size(path) -> int:
    total = 0
    for root, _dirs, files in os.walk(path):
        for name in files:
            try:
                total += (Path(root) / name).stat().st_size
            except OSError:
                continue
    return total
