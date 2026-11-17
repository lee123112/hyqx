"""输入输出基础能力：路径安全、流式解码、原子写入、任务锁、安全解压。

跨模态共享契约要求：
- 输出只能写入 ``OUTPUT_DIR/<uniqueId>/``，拒绝空/非法/绝对路径与目录穿越；
- JSONL 尽量流式处理；损坏行不得静默丢弃；
- 输出 UTF-8，并用原子替换避免输入输出同路径时截断源文件。
"""
from __future__ import annotations

import codecs
import json
import os
import posixpath
import re
import shutil
import time
import zipfile
from pathlib import Path
from typing import Any, Dict, Iterator, List, Optional, Sequence

__all__ = [
    "TaskBusyError",
    "UnsupportedMediaPath",
    "JsonlWriter",
    "TaskLock",
    "atomic_write_json",
    "atomic_write_text",
    "iter_decoded_lines",
    "remove_task",
    "resolve_in_task",
    "resolve_media",
    "safe_extract_zip",
    "validate_unique_id",
    "write_jsonl_atomic",
]

# --------------------------------------------------------------------------- #
# uniqueId 与路径安全
# --------------------------------------------------------------------------- #

_UNIQUE_ID_RE = re.compile(r"^[A-Za-z0-9\u4e00-\u9fff][A-Za-z0-9\u4e00-\u9fff._-]{0,63}$")
_RESERVED_NAMES = {
    "CON", "PRN", "AUX", "NUL",
    "COM1", "COM2", "COM3", "COM4", "COM5", "COM6", "COM7", "COM8", "COM9",
    "LPT1", "LPT2", "LPT3", "LPT4", "LPT5", "LPT6", "LPT7", "LPT8", "LPT9",
}
_WINDOWS_DRIVE_RE = re.compile(r"^[A-Za-z]:")
_URL_SCHEME_RE = re.compile(r"^[A-Za-z][A-Za-z0-9+.\-]*://")


class UnsupportedMediaPath(ValueError):
    """媒体引用不合法（绝对路径、目录穿越、协议地址、Windows 路径等）。"""


def validate_unique_id(value: Any) -> str:
    """校验任务标识；非法即抛 ``ValueError``。"""
    # [v4.2] mty_compliance 已停用：移除其对 uniqueId 的重复校验调用（本地校验已覆盖）
    # from . import mty_compliance as _mty_compliance
    # _mty_compliance.validate_unique_id(value)
    if not isinstance(value, str):
        raise ValueError("uniqueId 必须是字符串")
    candidate = value.strip()
    if not candidate:
        raise ValueError("uniqueId 不能为空")
    if candidate != value:
        raise ValueError("uniqueId 不能包含首尾空白")
    if not _UNIQUE_ID_RE.match(candidate):
        raise ValueError("uniqueId 只能包含中英文、数字、下划线、连字符和点：%r" % value)
    if candidate.endswith("."):
        raise ValueError("uniqueId 不能以点结尾：%r" % value)
    stem = candidate.split(".")[0]
    if stem.upper() in _RESERVED_NAMES:
        raise ValueError("uniqueId 不能使用系统保留名：%r" % value)
    if candidate in {".", ".."}:
        raise ValueError("uniqueId 不能是 . 或 ..")
    return candidate


def _normalize_relative(value: str) -> str:
    """把相对引用规范化为 posix 形式，并拒绝穿越/绝对/盘符/协议。"""
    if not isinstance(value, str):
        raise UnsupportedMediaPath("媒体引用必须是字符串")
    raw = value.strip()
    if not raw:
        raise UnsupportedMediaPath("媒体引用不能为空")
    if "\\" in raw:
        raise UnsupportedMediaPath("媒体引用必须使用 / 分隔符：%r" % value)
    if raw.startswith("~"):
        raise UnsupportedMediaPath("媒体引用不能使用用户目录：%r" % value)
    if raw.startswith("/"):
        raise UnsupportedMediaPath("媒体引用必须是相对路径：%r" % value)
    if _WINDOWS_DRIVE_RE.match(raw):
        raise UnsupportedMediaPath("媒体引用不能包含盘符：%r" % value)
    if _URL_SCHEME_RE.match(raw):
        raise UnsupportedMediaPath("媒体引用不能是网络地址：%r" % value)
    normalized = posixpath.normpath(raw)
    if normalized in {"", ".", ".."} or normalized.startswith("../"):
        raise UnsupportedMediaPath("媒体引用不得越出任务目录：%r" % value)
    if posixpath.isabs(normalized):
        raise UnsupportedMediaPath("媒体引用必须是相对路径：%r" % value)
    return normalized


def resolve_media(base_dir: Path, value: str) -> Path:
    """把合法相对引用解析为 ``base_dir`` 下的绝对路径（不检查文件是否存在）。"""
    normalized = _normalize_relative(value)
    base = Path(base_dir).resolve()
    target = (base / normalized).resolve()
    if target != base and base not in target.parents:
        raise UnsupportedMediaPath("媒体引用越出任务目录：%r" % value)
    return target


def resolve_in_task(task_dir: Path, value: str) -> Path:
    """把阶段内文件引用解析为任务目录内的绝对路径；越界即抛 ``ValueError``。"""
    if not isinstance(value, str) or not value.strip():
        raise ValueError("inputFile 不能为空")
    normalized = _normalize_relative(value)
    base = Path(task_dir).resolve()
    target = (base / normalized).resolve()
    if target != base and base not in target.parents:
        raise ValueError("inputFile 必须位于任务目录内：%r" % value)
    return target


# --------------------------------------------------------------------------- #
# 流式读取
# --------------------------------------------------------------------------- #

def iter_decoded_lines(path: Path, encoding: str = "utf-8",
                       chunk_size: int = 1 << 20) -> Iterator[str]:
    """流式解码文本行（不含换行符）。

    编码错误会以 ``UnicodeDecodeError`` 抛出，由调用方计入拒绝清单，
    绝不静默跳过。
    """
    decoder = codecs.getincrementaldecoder(encoding)(errors="strict")
    with open(path, "rb") as handle:
        buffer = ""
        while True:
            chunk = handle.read(chunk_size)
            if not chunk:
                break
            buffer += decoder.decode(chunk)
            while "\n" in buffer:
                line, buffer = buffer.split("\n", 1)
                yield line
        buffer += decoder.decode(b"", final=True)
        if buffer:
            yield buffer


# --------------------------------------------------------------------------- #
# 原子写入
# --------------------------------------------------------------------------- #

_SWAP_SUFFIX = ".swp"


def atomic_write_text(path: Path, text: str, encoding: str = "utf-8") -> Path:
    """同目录临时文件 + ``os.replace``，保证不会出现半截文件。"""
    target = Path(path)
    target.parent.mkdir(parents=True, exist_ok=True)
    temp = target.with_name(target.name + _SWAP_SUFFIX)
    try:
        with open(temp, "w", encoding=encoding, newline="\n") as handle:
            handle.write(text)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temp, target)
    finally:
        if temp.exists():
            try:
                temp.unlink()
            except OSError:       # pragma: no cover - 清理失败不影响主流程
                pass
    return target


def atomic_write_json(path: Path, payload: Any, encoding: str = "utf-8") -> Path:
    text = json.dumps(payload, ensure_ascii=False, indent=2)
    return atomic_write_text(path, text + "\n", encoding=encoding)


class JsonlWriter:
    """JSONL 写出器：写同目录临时文件，``close`` 时原子替换目标。

    这样即使 ``inputFile`` 与 ``outputFile`` 指向同一路径，也不会截断源文件。
    """

    def __init__(self, path: Path, encoding: str = "utf-8", atomic: bool = True):
        self.path = Path(path)
        self.encoding = encoding
        self.atomic = atomic
        self.count = 0
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._target = (self.path.with_name(self.path.name + _SWAP_SUFFIX)
                        if atomic else self.path)
        self._handle = open(self._target, "w", encoding=encoding, newline="\n")

    def write(self, payload: Any) -> None:
        self._handle.write(json.dumps(payload, ensure_ascii=False) + "\n")
        self.count += 1

    def flush(self) -> None:
        self._handle.flush()

    def close(self) -> None:
        if self._handle.closed:
            return
        self._handle.flush()
        os.fsync(self._handle.fileno())
        self._handle.close()
        if self.atomic:
            os.replace(self._target, self.path)

    def __enter__(self) -> "JsonlWriter":
        return self

    def __exit__(self, exc_type, exc, traceback) -> None:
        if exc_type is not None and self.atomic:
            try:
                self._handle.close()
            finally:
                if self._target.exists():
                    self._target.unlink()
            return
        self.close()


def write_jsonl_atomic(path: Path, rows: Sequence[Any], encoding: str = "utf-8") -> int:
    with JsonlWriter(path, encoding=encoding) as writer:
        for row in rows:
            writer.write(row)
    return len(rows)


# --------------------------------------------------------------------------- #
# 任务锁与任务清理
# --------------------------------------------------------------------------- #

class TaskBusyError(RuntimeError):
    """同一 uniqueId 已有运行占用（并发保护）。"""


class TaskLock:
    """基于目录创建的原子任务锁（进程内/进程间均生效）。"""

    def __init__(self, output_root: Path, unique_id: str):
        self.output_root = Path(output_root)
        self.unique_id = validate_unique_id(unique_id)
        self.path = self.output_root / self.unique_id / ".lock"
        self._held = False

    def acquire(self) -> "TaskLock":
        self.output_root.mkdir(parents=True, exist_ok=True)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        try:
            os.mkdir(self.path)
        except FileExistsError:
            raise TaskBusyError("任务 %s 正在运行" % self.unique_id)
        self._held = True
        try:
            (self.path / "owner").write_text(
                "%s\n%s\n" % (os.getpid(), time.strftime("%Y-%m-%dT%H:%M:%S")),
                encoding="utf-8")
        except OSError:            # pragma: no cover - 仅用于排查
            pass
        return self

    def release(self) -> None:
        if not self._held:
            return
        shutil.rmtree(self.path, ignore_errors=True)
        self._held = False

    def __enter__(self) -> "TaskLock":
        return self.acquire()

    def __exit__(self, exc_type, exc, traceback) -> None:
        self.release()


def remove_task(output_root: Path, unique_id: str) -> bool:
    """删除任务目录；不存在返回 ``False``（幂等）。"""
    target = Path(output_root) / validate_unique_id(unique_id)
    if not target.exists():
        return False
    if target.is_dir():
        shutil.rmtree(target, ignore_errors=True)
    else:
        target.unlink()
    return True


# --------------------------------------------------------------------------- #
# 安全解压
# --------------------------------------------------------------------------- #

def safe_extract_zip(archive: Path, dest_dir: Path,
                     max_unpacked_bytes: int = 512 * 1024 * 1024) -> List[str]:
    """安全解压 zip：拒绝穿越、绝对路径、大小写重复与超限。

    只解压普通文件（忽略目录条目），返回写入的相对路径列表（posix 风格）。
    """
    dest = Path(dest_dir).resolve()
    written: List[str] = []
    seen: set = set()
    total = 0
    with zipfile.ZipFile(archive) as bundle:
        for info in bundle.infolist():
            if info.is_dir():
                continue
            normalized = _normalize_relative(info.filename.replace("\\", "/"))
            key = normalized.lower()
            if key in seen:
                raise ValueError("压缩包内存在重复路径：%r" % info.filename)
            seen.add(key)
            total += int(info.file_size)
            if total > max_unpacked_bytes:
                raise ValueError("压缩包解压后超过限制（%d 字节）" % max_unpacked_bytes)
            target = (dest / normalized).resolve()
            if dest not in target.parents:
                raise ValueError("压缩包条目越出目标目录：%r" % info.filename)
            target.parent.mkdir(parents=True, exist_ok=True)
            with bundle.open(info) as source, open(target, "wb") as sink:
                shutil.copyfileobj(source, sink)
            written.append(normalized)
    return written
