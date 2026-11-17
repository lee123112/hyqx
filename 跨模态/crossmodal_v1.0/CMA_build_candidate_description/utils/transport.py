# [v4.2] 血缘/阶段归档/运输输出已停用：本模块不再被 app.py / inference.py 调用，代码保留存档，不再维护。
# 例外：crossmodal-files-v1 文件对象打包/恢复/入口定位仍被 app.py 用于阶段链式传输，保持可用。
"""crossmodal-files-v1 文件内容对象的打包与恢复适配层（demo 约定）。"""
from __future__ import annotations

import base64
import json
from pathlib import Path

_TEXT_SUFFIXES = {".json", ".jsonl", ".txt", ".md", ".csv"}
ALLOWED_ENCODINGS = {"utf-8", "base64"}


def pack_file_object(directory: Path, entrypoint: str, paths: list) -> dict:
    directory = Path(directory)
    files = []
    for relative in dict.fromkeys(paths):
        raw = (directory / relative).read_bytes()
        if Path(relative).suffix.lower() in _TEXT_SUFFIXES:
            files.append({"path": relative, "encoding": "utf-8",
                          "content": raw.decode("utf-8")})
        else:
            files.append({"path": relative, "encoding": "base64",
                          "content": base64.b64encode(raw).decode("ascii")})
    return {"format": "crossmodal-files-v1", "entrypoint": entrypoint, "files": files}


def restore_file_object(payload: dict, destination: Path) -> Path:
    if not isinstance(payload, dict) or payload.get("format") != "crossmodal-files-v1":
        raise ValueError("文件对象格式错误")
    entrypoint = payload.get("entrypoint")
    if not isinstance(entrypoint, str) or not entrypoint.strip():
        raise ValueError("文件对象缺少 entrypoint")
    destination = Path(destination)
    destination.mkdir(parents=True, exist_ok=True)
    root = destination.resolve()
    for item in payload.get("files", []):
        if not isinstance(item, dict):
            raise ValueError("文件对象条目非法")
        encoding = item.get("encoding")
        if encoding not in ALLOWED_ENCODINGS:
            raise ValueError("文件对象编码非法")
        relative = Path(str(item.get("path", "")))
        if not str(item.get("path", "")).strip() or relative.is_absolute() \
                or ".." in relative.parts:
            raise ValueError("文件对象路径非法")
        target = (destination / relative).resolve()
        try:
            target.relative_to(root)
        except ValueError as exc:
            raise ValueError("文件对象路径越界") from exc
        try:
            raw = (str(item["content"]).encode("utf-8") if encoding == "utf-8"
                   else base64.b64decode(str(item["content"]), validate=True))
        except (KeyError, ValueError) as exc:
            raise ValueError("文件对象内容非法") from exc
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(raw)
    entry = (destination / entrypoint).resolve()
    try:
        entry.relative_to(root)
    except ValueError as exc:
        raise ValueError("文件对象 entrypoint 越界") from exc
    return entry


def resolve_entrypoint(directory: Path) -> tuple:
    directory = Path(directory)
    manifest = directory / "manifest.json"
    if manifest.is_file():
        payload = json.loads(manifest.read_text(encoding="utf-8"))
        relative = payload.get("entrypoint")
        if not isinstance(relative, str) or not relative.strip():
            raise ValueError("manifest.json 缺少 entrypoint")
        rel_path = Path(relative)
        if rel_path.is_absolute() or ".." in rel_path.parts:
            raise ValueError("manifest.json entrypoint 非法")
        target = (directory / rel_path).resolve()
        try:
            target.relative_to(directory.resolve())
        except ValueError as exc:
            raise ValueError("manifest.json entrypoint 越界") from exc
        if not target.is_file():
            raise ValueError("manifest.json entrypoint 不存在")
        return target, rel_path.as_posix()
    candidates = sorted(p for p in directory.rglob("*.jsonl") if p.is_file())
    if len(candidates) != 1:
        raise ValueError("必须存在 manifest.json 或唯一 JSONL 入口")
    return candidates[0], candidates[0].relative_to(directory).as_posix()
