# [v4.2] 血缘/阶段归档/运输输出已停用：mty 运输归档不再维护；
# 本模块仅保留 crossmodal-files-v1 阶段文件对象打包/恢复/入口定位（阶段链式传输仍被 app.py 使用）。
"""阶段末 crossmodal-files-v1 文件内容对象的打包与恢复。

该格式是 demo 约定（DEVELOPMENT_CONTRACT §4），不是甲方正式二进制承载协议；
正式协议到位时只替换本适配层，不改变三接口字段。
"""
from __future__ import annotations

import base64
import json
from pathlib import Path
from typing import Dict, List, Optional, Sequence

FORMAT = "crossmodal-files-v1"
MAX_RESPONSE_BYTES = 64 * 1024 * 1024
_TEXT_SUFFIXES = {".json", ".jsonl", ".txt", ".md", ".csv", ".yaml", ".yml"}


def _safe_relative(value: str) -> Path:
    relative = Path(str(value).replace("\\", "/"))
    if not str(value).strip() or relative.is_absolute() or ".." in relative.parts:
        raise ValueError("文件对象路径非法：%s" % value)
    return relative


def pack_file_object(directory: Path, entrypoint: str, paths: Sequence[str]) -> dict:
    """把运行目录内的文件打包成文件内容对象（文本用 utf-8，媒体用 base64）。"""
    directory = Path(directory)
    entry = _safe_relative(entrypoint)
    if not (directory / entry).is_file():
        raise ValueError("入口文件不存在：%s" % entrypoint)
    files: List[dict] = []
    seen = set()
    for raw in paths:
        relative = _safe_relative(raw)
        key = relative.as_posix()
        if key in seen:
            raise ValueError("文件对象存在重复路径：%s" % raw)
        seen.add(key)
        target = directory / relative
        if not target.is_file():
            raise ValueError("待打包文件不存在：%s" % raw)
        data = target.read_bytes()
        if relative.suffix.lower() in _TEXT_SUFFIXES:
            files.append({"path": relative.as_posix(), "encoding": "utf-8",
                          "content": data.decode("utf-8")})
        else:
            files.append({"path": relative.as_posix(), "encoding": "base64",
                          "content": base64.b64encode(data).decode("ascii")})
    payload = {"format": FORMAT, "entrypoint": entry.as_posix(), "files": files}
    if len(json.dumps(payload, ensure_ascii=False).encode("utf-8")) > MAX_RESPONSE_BYTES:
        raise ValueError("output_too_large: 阶段末文件对象超过 %d 字节" % MAX_RESPONSE_BYTES)
    return payload


def restore_file_object(payload: dict, destination: Path) -> Path:
    """把文件内容对象恢复到本地目录，返回入口文件路径。"""
    if not isinstance(payload, dict) or payload.get("format") != FORMAT:
        raise ValueError("文件对象格式错误")
    entrypoint = payload.get("entrypoint")
    if not isinstance(entrypoint, str):
        raise ValueError("文件对象缺少 entrypoint")
    destination = Path(destination)
    destination.mkdir(parents=True, exist_ok=True)
    for item in payload.get("files", []):
        if not isinstance(item, dict) or "path" not in item or "content" not in item:
            raise ValueError("文件对象成员结构非法")
        relative = _safe_relative(item["path"])
        encoding = item.get("encoding", "utf-8")
        if encoding == "utf-8":
            data = str(item["content"]).encode("utf-8")
        elif encoding == "base64":
            data = base64.b64decode(item["content"], validate=True)
        else:
            raise ValueError("文件对象编码不受支持：%s" % encoding)
        target = destination / relative
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(data)
    entry = destination / _safe_relative(entrypoint)
    if not entry.is_file():
        raise ValueError("文件对象恢复后缺少入口：%s" % entrypoint)
    return entry


def resolve_entrypoint(directory: Path) -> tuple:
    """按 manifest.json 定位入口；没有 manifest 时允许唯一 JSONL 自动识别。"""
    directory = Path(directory)
    manifest = directory / "manifest.json"
    if manifest.is_file():
        payload = json.loads(manifest.read_text(encoding="utf-8"))
        relative = _safe_relative(payload.get("entrypoint", ""))
        target = directory / relative
        if not target.is_file():
            raise ValueError("manifest.json entrypoint 不存在")
        return target, relative.as_posix()
    candidates = sorted(directory.rglob("*.jsonl"))
    if len(candidates) != 1:
        raise ValueError("素材包必须包含 manifest.json 或唯一 JSONL 入口")
    return candidates[0], candidates[0].relative_to(directory).as_posix()


def read_manifest(run_dir: Path) -> Optional[Dict]:
    manifest = Path(run_dir) / "manifest.json"
    if not manifest.is_file():
        return None
    return json.loads(manifest.read_text(encoding="utf-8"))
