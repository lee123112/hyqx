# [v4.2] 血缘/阶段归档/运输输出已停用：本模块不再被 app.py / inference.py 调用，代码保留存档，不再维护。
"""传输适配层：``crossmodal-files-v1`` 文件内容对象与运行清单。

- 中间节点只把**临时输出路径**写进血缘，主输出文件留在 ``OUTPUT_DIR/<uniqueId>/`` 内；
- 阶段末由工程侧把运行目录打包为 ``crossmodal-files-v1`` 内容对象重新上传；
- 文本文件用 UTF-8 字符串，二进制（图片/音视频）用 base64；
- 只接受包内相对路径，任何绝对路径、``..``、反斜杠一律拒绝。
"""
from __future__ import annotations

import base64
import binascii
import json
import os
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional, Tuple

from . import io as io_utils

FILE_OBJECT_FORMAT = "crossmodal-files-v1"
MANIFEST_FORMAT = "crossmodal-manifest-v1"
MANIFEST_NAME = "manifest.json"

_TEXT_SUFFIXES = {
    ".jsonl", ".json", ".txt", ".md", ".log", ".csv", ".tsv", ".yaml", ".yml",
}


class TransportError(ValueError):
    """文件内容对象或运行清单不合法。"""


class OutputTooLargeError(TransportError):
    """阶段末响应体超过 ``MAX_RESPONSE_BYTES``。"""


def _validate_relative(relative: str) -> str:
    try:
        return io_utils.normalize_zip_member_name(relative)
    except ValueError as exc:  # 统一转成本层异常类型
        raise TransportError(str(exc)) from exc


def _is_text_file(path: Path) -> bool:
    if path.suffix.lower() in _TEXT_SUFFIXES:
        return True
    try:
        sample = path.read_bytes()[:4096]
    except OSError:
        return False
    if b"\x00" in sample:
        return False
    try:
        sample.decode("utf-8")
    except UnicodeDecodeError:
        return False
    return True


def pack_file_object(run_dir, entrypoint: str,
                     relative_paths: Optional[Iterable[str]] = None,
                     max_response_bytes: Optional[int] = None) -> Dict[str, Any]:
    """把运行目录内指定文件打包为 ``crossmodal-files-v1`` 内容对象。"""
    root = io_utils.resolved_absolute(run_dir)
    entry_relative = _validate_relative(entrypoint)
    root.mkdir(parents=True, exist_ok=True)
    entry_path = io_utils.resolved_absolute(root / entry_relative)
    if not io_utils.is_within(root, entry_path) or not entry_path.is_file():
        raise TransportError("入口文件不存在或越出运行目录：%s" % entrypoint)

    candidates = list(relative_paths) if relative_paths is not None else [entry_relative]
    if entry_relative not in candidates:
        candidates.insert(0, entry_relative)

    files: List[Dict[str, Any]] = []
    ordered: List[str] = []
    seen: set = set()
    total_bytes = 0
    for raw in candidates:
        relative = _validate_relative(raw)
        key = relative.lower()
        if key in seen:
            continue
        seen.add(key)
        target = io_utils.resolved_absolute(root / relative)
        if not io_utils.is_within(root, target) or not target.is_file():
            raise TransportError("待打包文件不存在或越出运行目录：%s" % raw)
        payload = target.read_bytes()
        total_bytes += len(payload)
        if max_response_bytes is not None and total_bytes > max_response_bytes:
            raise OutputTooLargeError(
                "阶段末响应体超过 MAX_RESPONSE_BYTES=%d" % max_response_bytes)
        if _is_text_file(target):
            files.append({"path": relative, "encoding": "utf-8",
                          "size": len(payload),
                          "content": payload.decode("utf-8")})
        else:
            files.append({"path": relative, "encoding": "base64",
                          "size": len(payload),
                          "content": base64.b64encode(payload).decode("ascii")})
        ordered.append(relative)

    return {
        "format": FILE_OBJECT_FORMAT,
        "entrypoint": entry_relative,
        "fileCount": len(files),
        "totalBytes": total_bytes,
        "files": files,
    }


def restore_file_object(payload: Dict[str, Any], destination) -> Path:
    """把 ``crossmodal-files-v1`` 内容对象还原到目录，返回入口文件路径。"""
    if not isinstance(payload, dict) or payload.get("format") != FILE_OBJECT_FORMAT:
        raise TransportError("不是 %s 内容对象" % FILE_OBJECT_FORMAT)
    files = payload.get("files")
    if not isinstance(files, list) or not files:
        raise TransportError("内容对象缺少 files")
    entry_relative = payload.get("entrypoint")
    if not isinstance(entry_relative, str) or not entry_relative:
        raise TransportError("内容对象缺少 entrypoint")

    dest = Path(destination)
    dest.mkdir(parents=True, exist_ok=True)
    seen: set = set()
    entry_path: Optional[Path] = None
    for item in files:
        if not isinstance(item, dict):
            raise TransportError("files 成员必须是对象")
        relative = _validate_relative(str(item.get("path", "")))
        key = relative.lower()
        if key in seen:
            raise TransportError("内容对象存在重复路径：%s" % relative)
        seen.add(key)
        encoding = item.get("encoding")
        content = item.get("content")
        if not isinstance(content, str):
            raise TransportError("成员 %s 缺少 content" % relative)
        target = dest / relative
        target.parent.mkdir(parents=True, exist_ok=True)
        if encoding == "utf-8":
            # 显式 newline="\n"：避免 Windows 上把换行写成 CRLF（保持字节可复现）
            with open(target, "w", encoding="utf-8", newline="\n") as stream:
                stream.write(content)
        elif encoding == "base64":
            try:
                target.write_bytes(base64.b64decode(content, validate=True))
            except (binascii.Error, ValueError) as exc:
                raise TransportError("成员 %s 的 base64 非法" % relative) from exc
        else:
            raise TransportError("成员 %s 的 encoding 非法：%r" % (relative, encoding))
        if relative == _validate_relative(entry_relative):
            entry_path = target

    if entry_path is None:
        raise TransportError("内容对象不包含 entrypoint 文件")
    return entry_path


def build_manifest(entrypoint: str, entries: Iterable[Dict[str, Any]]) -> Dict[str, Any]:
    artifacts: List[Dict[str, Any]] = []
    seen: set = set()
    for entry in entries:
        relative = _validate_relative(str(entry["path"]))
        key = relative.lower()
        if key in seen:
            continue
        seen.add(key)
        artifact = dict(entry)
        artifact["path"] = relative
        artifacts.append(artifact)
    entry_relative = _validate_relative(entrypoint)
    if entry_relative not in [item["path"] for item in artifacts]:
        artifacts.insert(0, {"path": entry_relative, "role": "main"})
    return {"format": MANIFEST_FORMAT, "entrypoint": entry_relative,
            "artifacts": artifacts}


def write_manifest(run_dir, manifest: Dict[str, Any]) -> Path:
    return io_utils.atomic_write_json(Path(run_dir) / MANIFEST_NAME, manifest)


def load_manifest(run_dir) -> Optional[Dict[str, Any]]:
    """读取运行清单；不存在返回 ``None``，格式错误抛 ``TransportError``。"""
    path = Path(run_dir) / MANIFEST_NAME
    if not path.is_file():
        return None
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError) as exc:
        raise TransportError("运行清单无法解析：%s" % path.name) from exc
    if not isinstance(payload, dict) or payload.get("format") != MANIFEST_FORMAT:
        raise TransportError("运行清单格式不是 %s" % MANIFEST_FORMAT)
    if not isinstance(payload.get("entrypoint"), str) or not payload["entrypoint"]:
        raise TransportError("运行清单缺少 entrypoint")
    return payload


def iter_manifest_paths(manifest: Dict[str, Any]) -> List[str]:
    ordered: List[str] = []
    seen: set = set()

    def push(value: Any) -> None:
        if not isinstance(value, str) or not value:
            return
        relative = _validate_relative(value)
        key = relative.lower()
        if key in seen:
            return
        seen.add(key)
        ordered.append(relative)

    push(manifest.get("entrypoint"))
    for item in manifest.get("artifacts") or []:
        if isinstance(item, dict):
            push(item.get("path"))
    return ordered


def resolve_entrypoint(run_dir, manifest: Optional[Dict[str, Any]] = None
                       ) -> Tuple[Path, str]:
    """定位输入入口文件；返回 ``(绝对路径, 运行目录内相对路径)``。

    无清单时要求运行目录内**恰好一个** ``.jsonl``（避免猜错上游），
    扫描时跳过 ``.`` 开头的目录。
    """
    root = io_utils.resolved_absolute(run_dir)
    if manifest is not None:
        relative = _validate_relative(str(manifest["entrypoint"]))
        target = io_utils.resolved_absolute(root / relative)
        if not io_utils.is_within(root, target) or not target.is_file():
            raise TransportError("清单入口文件不存在：%s" % relative)
        return target, relative

    if not root.exists():
        raise TransportError("输入目录不存在：%s" % root)

    found: List[Tuple[Path, str]] = []
    for current, dirs, filenames in os.walk(root):
        dirs[:] = [name for name in dirs if not name.startswith(".")]
        for name in filenames:
            if name.startswith(".") or not name.endswith(".jsonl"):
                continue
            if name == MANIFEST_NAME:
                continue
            full = io_utils.resolved_absolute(Path(current) / name)
            relative = full.relative_to(root).as_posix()
            if relative.count("/") > 0:
                continue
            found.append((full, relative))
    if not found:
        raise TransportError("输入目录内未找到 JSONL 入口文件")
    if len(found) > 1:
        raise TransportError("输入目录内存在多个 JSONL，无法确定入口文件")
    return found[0]
