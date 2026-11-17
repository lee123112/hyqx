# [v4.2] 血缘/阶段归档/运输输出已停用：本模块不再被 app.py / inference.py 调用，代码保留存档，不再维护。
# [v4.2] 注：MTY 运输输出（oss 镜像/血缘运输）已停用；pack_file_object/build_manifest/resolve_entrypoint
# 等文件打包与入口定位函数仍被形态二 outputFile 打包与上传流程复用（改造说明 §3 明确保留）。
"""跨模态 demo 传输适配层：文件内容对象与运行清单。

> 本格式 ``crossmodal-files-v1`` 是并行开发阶段选定的 demo 约定，
> 不是甲方正式协议；正式二进制承载协议到位时只替换本适配层。
> 限额 ``max_response_bytes`` 默认 256MiB（v1.1.1 起，与 #18 传输层一致；
> 原 64MiB 只够 ~45MiB 产物，138 条实跑的 #6 产物 83MB 会被迫走路径引用）。

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

#: 单次响应（``crossmodal-files-v1`` 文件内容对象）字节上限默认值。
#: v1.1.1 起由 64MiB 提到 256MiB：base64 有 4/3 膨胀，64MiB 实际只能装 ~45MiB 产物，
#: 138 条实跑的 #6 产物内联后 83,328,904 字节会直接触发路径引用回退。
#: 仍超限时由 ``inference.build_response`` 落盘 ``<runDir>_form2.zip`` 走路径引用。
DEFAULT_MAX_RESPONSE_BYTES = 256 * 1024 * 1024

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

    # 只按“包内相对路径”过滤隐藏项：绝对路径里常含 .staging-* 之类的工作目录，
    # 用 path.parts 判断会把整个素材包误判为隐藏，导致入口凭空消失。
    candidates = []
    for path in sorted(base.rglob("*.jsonl")):
        try:
            relative_parts = path.relative_to(base).parts
        except ValueError:  # pragma: no cover - rglob 结果必然在 base 之内
            continue
        if any(part.startswith(".") for part in relative_parts):
            continue
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


def estimate_encoded_bytes(relative_path: str, path: Path) -> int:
    """不读内容就估算文件在文件内容对象里的字节数。

    二进制按 base64 精确膨胀（4/3）估算；文本按 UTF-8 原字节估算（JSON 转义还会放大，
    由逐文件编码后的精确累计兜底）。用途只是**超限前提前失败**，不参与最终判定。
    """
    size = path.stat().st_size
    if is_text_path(relative_path):
        return size + 96
    return ((size + 2) // 3) * 4 + 96


def pack_file_object(directory, entrypoint: str, relative_paths: Iterable[str],
                     max_response_bytes: int = DEFAULT_MAX_RESPONSE_BYTES) -> Dict[str, Any]:
    """构造阶段末文件内容对象；超限直接失败，不截断。

    v1.1.1：改为逐文件累计大小并在**读取大文件之前**提前失败。原实现先把所有文件
    读进内存再序列化，几百 MB 的帧序列会在已消耗完整内存后才发现装不下；现在二进制
    文件按 stat 估算先过一遍限额，超限立即抛 ``OutputTooLargeError``（调用方据此
    改走 ``<runDir>_form2.zip`` 路径引用，见 ``inference.build_response``）。
    """
    entry_relative = _validate_relative(entrypoint)
    resolved = collect_payload_files(directory, relative_paths)
    if entry_relative not in dict(resolved):
        raise TransportError("文件内容对象缺少入口文件：%s" % entry_relative)

    # 信封固定开销（format/entrypoint/files 键名与引号）留足余量
    used = 256
    files: List[Dict[str, Any]] = []
    for relative, path in resolved:
        estimate = estimate_encoded_bytes(relative, path)
        if used + estimate > max_response_bytes:
            raise OutputTooLargeError(
                "输出文件内容对象将超过限额 %d 字节（已编码约 %d 字节，"
                "文件 %s 约需 %d 字节，未读取该文件）"
                % (max_response_bytes, used, relative, estimate))
        item = encode_file(relative, path)
        used += len(json.dumps(item, ensure_ascii=False).encode("utf-8")) + 2
        files.append(item)
    if used > max_response_bytes:
        raise OutputTooLargeError(
            "输出文件内容对象 %d 字节超过限额 %d 字节" % (used, max_response_bytes))
    return {"format": FILE_OBJECT_FORMAT, "entrypoint": entry_relative, "files": files}


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
                            max_response_bytes: int = DEFAULT_MAX_RESPONSE_BYTES) -> Dict[str, Any]:
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
