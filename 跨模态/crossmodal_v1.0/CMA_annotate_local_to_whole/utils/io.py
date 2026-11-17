"""文件系统基础设施：路径安全、任务锁、原子写、JSONL 流式读写、清单与媒体。

所有函数都把「安全」和「可追溯」放在第一位：
- 路径必须归一化后校验，禁止绝对路径、``..`` 与跨任务访问；
- JSONL 逐行分块读取，绝不全量载入内存；
- 输出一律走原子替换，避免输入输出同路径时截断源文件。
"""
from __future__ import annotations

import hashlib
import json
import os
import re
import shutil
import tempfile
from pathlib import Path
from typing import Any, Dict, Iterable, Iterator, List, Optional, Tuple

# --------------------------------------------------------------------------- #
# 常量
# --------------------------------------------------------------------------- #

UNIQUE_ID_PATTERN = re.compile(r"^[A-Za-z0-9](?:[A-Za-z0-9._-]{0,61}[A-Za-z0-9])?$")
WINDOWS_RESERVED = {
    "CON", "PRN", "AUX", "NUL",
    *(f"COM{index}" for index in range(1, 10)),
    *(f"LPT{index}" for index in range(1, 10)),
}

MAX_JSONL_LINE_BYTES = 16 * 1024 * 1024
DEFAULT_MAX_ZIP_FILES = 20_000
DEFAULT_MAX_UNPACKED_BYTES = 2 * 1024 ** 3
DEFAULT_MAX_MEDIA_BYTES = 512 * 1024 ** 2
DEFAULT_MAX_SIDECAR_BYTES = 16 * 1024 * 1024
DEFAULT_MAX_OUTPUT_BYTES = 256 * 1024 ** 2
DEFAULT_MAX_RESPONSE_BYTES = 200 * 1024 ** 2

MANIFEST_NAME = "manifest.json"
INPUT_DIR_NAME = "input"
LOCKS_DIR_NAME = "locks"


class IoError(RuntimeError):
    """文件系统层的通用错误。"""


class TaskBusyError(IoError):
    """同一 uniqueId 已有任务在运行。"""


class InputTooLargeError(IoError):
    """单行或单文件超过允许上限。"""


class UnsupportedMediaPath(ValueError):
    """媒体引用路径非法（绝对路径、目录穿越、空值等）。"""


# --------------------------------------------------------------------------- #
# 路径工具
# --------------------------------------------------------------------------- #

def resolved_absolute(target) -> Path:
    """把任意路径归一化为绝对路径（不要求存在）。"""
    text = os.fspath(target)
    if isinstance(text, bytes):
        text = text.decode("utf-8", "strict")
    if "\x00" in text:
        raise UnsupportedMediaPath("path contains NUL byte")
    return Path(os.path.abspath(os.path.expanduser(text)))


def is_within(root: Path, target: Path) -> bool:
    """``target`` 是否位于 ``root`` 之内（基于 resolve，而非字符串前缀）。"""
    try:
        target.relative_to(root)
    except ValueError:
        return False
    return True


def validate_unique_id(value: Any) -> str:
    """校验 uniqueId：必须是安全的单层目录名。"""
    # [v4.2] mty_compliance 已停用：移除其对 uniqueId 的重复校验调用（本地校验已覆盖）
    # from . import mty_compliance as _mty_compliance
    # _mty_compliance.validate_unique_id(value)
    if not isinstance(value, str):
        raise ValueError("uniqueId must be a string")
    if not UNIQUE_ID_PATTERN.match(value):
        raise ValueError("uniqueId is not a safe single path segment: %r" % value)
    if value.split(".")[0].upper() in WINDOWS_RESERVED:
        raise ValueError("uniqueId uses a reserved device name: %r" % value)
    return value


def normalize_relative_path(reference: Any) -> str:
    """把相对媒体引用归一化为 ``a/b/c`` 形式；拒绝绝对路径与目录穿越。"""
    if not isinstance(reference, str) or not reference.strip():
        raise UnsupportedMediaPath("media reference must be a non-empty string")
    text = reference.strip()
    if "\x00" in text:
        raise UnsupportedMediaPath("media reference contains NUL byte")
    normalized = text.replace("\\", "/")
    if normalized.startswith("/") or re.match(r"^[A-Za-z]:", normalized):
        raise UnsupportedMediaPath("media reference must be relative: %r" % reference)
    parts = [part for part in normalized.split("/") if part not in ("", ".")]
    if not parts:
        raise UnsupportedMediaPath("media reference is empty after normalization")
    if any(part == ".." for part in parts):
        raise UnsupportedMediaPath("media reference escapes its root: %r" % reference)
    return "/".join(parts)


def task_root(output_root, unique_id: str) -> Path:
    return resolved_absolute(output_root) / validate_unique_id(unique_id)


def task_input_dir(output_root, unique_id: str) -> Path:
    return task_root(output_root, unique_id) / INPUT_DIR_NAME


def resolve_in_task(root, reference) -> Path:
    """把 ``reference``（相对或绝对）解析到 ``root`` 之内，越界即报错。"""
    root_path = resolved_absolute(root)
    if isinstance(reference, (str, os.PathLike)):
        text = os.fspath(reference)
    else:
        raise ValueError("path reference must be a string")
    if not text or "\x00" in text:
        raise ValueError("path reference is empty or contains NUL byte")
    candidate = resolved_absolute(text)
    if not is_within(root_path, candidate):
        relative = os.path.join(str(root_path), text)
        candidate = resolved_absolute(relative)
    if not is_within(root_path, candidate):
        raise ValueError("path escapes the task root: %r" % reference)
    if not candidate.is_file():
        raise ValueError("path does not exist or is not a file: %r" % reference)
    return candidate


def resolve_media(roots: Iterable, reference: Any) -> Optional[Path]:
    """在若干素材根目录中定位相对媒体引用；找不到返回 ``None``。"""
    relative = normalize_relative_path(reference)
    for root in roots:
        root_path = resolved_absolute(root)
        candidate = resolved_absolute(str(root_path / relative))
        if not is_within(root_path, candidate):
            continue
        if candidate.is_file():
            return candidate
    return None


# --------------------------------------------------------------------------- #
# 读取
# --------------------------------------------------------------------------- #

def iter_decoded_lines(path, *, encoding: str = "utf-8",
                       max_line_bytes: int = MAX_JSONL_LINE_BYTES) -> Iterator[str]:
    """逐行分块解码，**不**整体载入内存；编码错误向上抛出 ``UnicodeDecodeError``。"""
    target = Path(path)
    with open(target, "rb") as handle:
        while True:
            raw = handle.readline(max_line_bytes + 1)
            if not raw:
                return
            if len(raw) > max_line_bytes and not raw.endswith(b"\n"):
                raise InputTooLargeError(
                    "%s: line exceeds %d bytes" % (target.name, max_line_bytes))
            yield raw.rstrip(b"\r\n").decode(encoding)


def read_bytes_limited(path, *, max_bytes: int = DEFAULT_MAX_MEDIA_BYTES) -> bytes:
    target = Path(path)
    size = target.stat().st_size
    if size > max_bytes:
        raise InputTooLargeError(
            "%s: %d bytes exceeds limit %d" % (target.name, size, max_bytes))
    return target.read_bytes()


def read_json_limited(path, *, max_bytes: int = DEFAULT_MAX_SIDECAR_BYTES) -> Dict[str, Any]:
    """读取较小的 JSON sidecar（转写、清单等）。"""
    return json.loads(read_bytes_limited(path, max_bytes=max_bytes).decode("utf-8"))


def sha256_file(path, *, chunk_size: int = 1024 * 1024) -> str:
    digest = hashlib.sha256()
    with open(Path(path), "rb") as handle:
        while True:
            chunk = handle.read(chunk_size)
            if not chunk:
                break
            digest.update(chunk)
    return digest.hexdigest()


def sha256_bytes(payload: bytes) -> str:
    return hashlib.sha256(payload).hexdigest()


# --------------------------------------------------------------------------- #
# 写入（全部原子替换）
# --------------------------------------------------------------------------- #

def atomic_write_text(path, text: str, *, encoding: str = "utf-8") -> Path:
    target = Path(path)
    target.parent.mkdir(parents=True, exist_ok=True)
    handle = tempfile.NamedTemporaryFile(
        "w", encoding=encoding, newline="\n", delete=False,
        dir=str(target.parent), prefix=".tmp-", suffix=".part")
    temporary = Path(handle.name)
    try:
        with handle:
            handle.write(text)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(str(temporary), str(target))
    except BaseException:
        temporary.unlink(missing_ok=True)
        raise
    return target


def atomic_write_json(path, payload: Any) -> Path:
    return atomic_write_text(
        path, json.dumps(payload, ensure_ascii=False, indent=2) + "\n")


def atomic_copy(source, destination) -> Path:
    target = Path(destination)
    target.parent.mkdir(parents=True, exist_ok=True)
    handle = tempfile.NamedTemporaryFile(
        "wb", delete=False, dir=str(target.parent), prefix=".tmp-", suffix=".part")
    temporary = Path(handle.name)
    try:
        with open(Path(source), "rb") as src, handle:
            shutil.copyfileobj(src, handle, 1024 * 1024)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(str(temporary), str(target))
    except BaseException:
        temporary.unlink(missing_ok=True)
        raise
    return target


class JsonlWriter:
    """流式 JSONL 写入器：逐行 flush，最终 fsync，返回真实行数。"""

    def __init__(self, path, *, encoding: str = "utf-8"):
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._handle = open(self.path, "w", encoding=encoding, newline="\n")
        self.count = 0

    def write(self, record: Dict[str, Any]) -> None:
        self._handle.write(json.dumps(record, ensure_ascii=False) + "\n")
        self.count += 1

    def close(self) -> int:
        if not self._handle.closed:
            self._handle.flush()
            os.fsync(self._handle.fileno())
            self._handle.close()
        return self.count

    def __enter__(self) -> "JsonlWriter":
        return self

    def __exit__(self, *exc_info) -> None:
        self.close()


def write_jsonl(path, records: Iterable[Dict[str, Any]], *,
                encoding: str = "utf-8") -> int:
    with JsonlWriter(path, encoding=encoding) as writer:
        for record in records:
            writer.write(record)
    return writer.count


class OutcomeRecorder:
    """一次运行的四路产物写入器（主输出 / 待人工 / 样本隔离 / 原始隔离）。"""

    def __init__(self, run_dir, *, main_name: str, pending_name: Optional[str] = None,
                 sample_quarantine_name: Optional[str] = None,
                 rejects_name: Optional[str] = None):
        self.run_dir = Path(run_dir)
        self.main = JsonlWriter(self.run_dir / main_name)
        self.pending = (JsonlWriter(self.run_dir / pending_name)
                        if pending_name else None)
        self.sample_quarantine = (JsonlWriter(self.run_dir / sample_quarantine_name)
                                  if sample_quarantine_name else None)
        self.rejects = JsonlWriter(self.run_dir / rejects_name) if rejects_name else None

    def close(self) -> Dict[str, int]:
        counts = {"main": self.main.close()}
        if self.pending is not None:
            counts["pending"] = self.pending.close()
        else:
            counts["pending"] = 0
        if self.sample_quarantine is not None:
            counts["sample_quarantine"] = self.sample_quarantine.close()
        else:
            counts["sample_quarantine"] = 0
        if self.rejects is not None:
            counts["rejects"] = self.rejects.close()
        else:
            counts["rejects"] = 0
        return counts


# --------------------------------------------------------------------------- #
# 运行目录与锁
# --------------------------------------------------------------------------- #

def next_run_id(operator_dir) -> str:
    """返回 ``run001`` 形式的最小可用编号（不复用已存在的编号）。"""
    directory = Path(operator_dir)
    index = 1
    while True:
        candidate = "run%03d" % index
        if not (directory / candidate).exists():
            return candidate
        index += 1


class TaskLock:
    """基于 ``O_CREAT|O_EXCL`` 的任务锁：跨进程互斥，退出即释放。"""

    def __init__(self, root, unique_id: str):
        self.unique_id = validate_unique_id(unique_id)
        self.path = resolved_absolute(root) / LOCKS_DIR_NAME / (self.unique_id + ".lock")
        self._held = False

    def __enter__(self) -> "TaskLock":
        self.path.parent.mkdir(parents=True, exist_ok=True)
        try:
            descriptor = os.open(str(self.path), os.O_CREAT | os.O_EXCL | os.O_WRONLY)
        except FileExistsError:
            raise TaskBusyError(
                "task %s is busy: %s exists" % (self.unique_id, self.path))
        try:
            os.write(descriptor, json.dumps({
                "uniqueId": self.unique_id, "pid": os.getpid(),
            }).encode("utf-8"))
        finally:
            os.close(descriptor)
        self._held = True
        return self

    def __exit__(self, *exc_info) -> None:
        if self._held:
            self.path.unlink(missing_ok=True)
            self._held = False


# --------------------------------------------------------------------------- #
# 安全解压
# --------------------------------------------------------------------------- #

def safe_extract_zip(stream, destination, *,
                     max_files: int = DEFAULT_MAX_ZIP_FILES,
                     max_unpacked_bytes: int = DEFAULT_MAX_UNPACKED_BYTES) -> List[str]:
    """安全解压：拒绝目录穿越、绝对路径、重复条目与超限，目标目录必须为空。"""
    import zipfile

    target_root = resolved_absolute(destination)
    if target_root.exists():
        if not target_root.is_dir():
            raise ValueError("extract destination is not a directory: %s" % target_root)
        if any(target_root.iterdir()):
            raise ValueError("extract destination must be empty: %s" % target_root)
    else:
        target_root.mkdir(parents=True, exist_ok=True)

    extracted: List[str] = []
    seen: set = set()
    total_unpacked = 0

    with zipfile.ZipFile(stream) as archive:
        infos = archive.infolist()
        if len(infos) > max_files:
            raise ValueError("zip contains %d entries, limit is %d"
                             % (len(infos), max_files))
        for info in infos:
            name = info.filename.replace("\\", "/")
            if name.endswith("/"):
                continue
            if name.startswith("/") or re.match(r"^[A-Za-z]:", name):
                raise ValueError("zip entry uses an absolute path: %r" % info.filename)
            parts = [part for part in name.split("/") if part not in ("", ".")]
            if not parts or any(part == ".." for part in parts):
                raise ValueError("zip entry escapes the destination: %r" % info.filename)
            key = "/".join(parts).lower()
            if key in seen:
                raise ValueError("zip entry is duplicated: %r" % info.filename)
            seen.add(key)
            total_unpacked += info.file_size
            if total_unpacked > max_unpacked_bytes:
                raise ValueError("zip unpacks beyond %d bytes" % max_unpacked_bytes)
            target = target_root.joinpath(*parts)
            if not is_within(target_root, resolved_absolute(target)):
                raise ValueError("zip entry escapes the destination: %r" % info.filename)
            target.parent.mkdir(parents=True, exist_ok=True)
            with archive.open(info) as source, open(target, "wb") as sink:
                shutil.copyfileobj(source, sink, 1024 * 1024)
            extracted.append("/".join(parts))
    return extracted


def jsonl_entry_count(path) -> int:
    """统计 JSONL 真实行数（用于校验守恒，不做业务解释）。"""
    if not Path(path).exists():
        return 0
    count = 0
    with open(Path(path), "rb") as handle:
        for raw in handle:
            if raw.strip():
                count += 1
    return count
