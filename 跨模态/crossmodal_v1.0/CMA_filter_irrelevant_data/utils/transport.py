# [v4.2] 血缘/阶段归档/运输输出已停用：本模块不再被 app.py / inference.py 调用，代码保留存档，不再维护。
"""算子 2 文件内容对象传输适配。"""
from __future__ import annotations

import base64
import json
from pathlib import Path


def pack_file_object(directory: Path, entrypoint: str, paths: list[str]) -> dict:
    files = []
    for relative in dict.fromkeys(paths):
        raw = (Path(directory) / relative).read_bytes()
        if Path(relative).suffix.lower() in {".json", ".jsonl", ".txt", ".md"}:
            files.append({"path": relative, "encoding": "utf-8", "content": raw.decode("utf-8")})
        else:
            files.append({"path": relative, "encoding": "base64", "content": base64.b64encode(raw).decode("ascii")})
    return {"format": "crossmodal-files-v1", "entrypoint": entrypoint, "files": files}


def restore_file_object(payload: dict, destination: Path) -> Path:
    if payload.get("format") != "crossmodal-files-v1":
        raise ValueError("文件对象格式错误")
    destination.mkdir(parents=True, exist_ok=True)
    for item in payload.get("files", []):
        relative = Path(item["path"])
        if relative.is_absolute() or ".." in relative.parts:
            raise ValueError("文件对象路径非法")
        raw = item["content"].encode("utf-8") if item["encoding"] == "utf-8" else base64.b64decode(item["content"], validate=True)
        target = destination / relative
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(raw)
    return destination / payload["entrypoint"]


def resolve_entrypoint(directory: Path) -> tuple[Path, str]:
    manifest = Path(directory) / "manifest.json"
    if manifest.is_file():
        payload = json.loads(manifest.read_text(encoding="utf-8"))
        relative = payload["entrypoint"]
        target = (Path(directory) / relative).resolve()
        target.relative_to(Path(directory).resolve())
        if not target.is_file():
            raise ValueError("入口文件不存在")
        return target, relative
    candidates = sorted(Path(directory).rglob("*.jsonl"))
    if len(candidates) != 1:
        raise ValueError("必须存在唯一 JSONL 入口")
    return candidates[0], str(candidates[0].relative_to(directory)).replace("\\", "/")
