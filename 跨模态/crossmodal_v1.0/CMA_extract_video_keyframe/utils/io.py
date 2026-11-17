"""流式 JSONL IO、任务/运行目录、路径安全、任务锁与安全解压。

安全边界（DEVELOPMENT_CONTRACT §3、§4）：
- ``uniqueId`` 只能是单个目录名，拒绝空、点路径、两种分隔符、盘符、UNC、Windows 保留设备名；
- 所有路径判断用 ``resolve()`` + ``relative_to()``，不用字符串 ``startswith``；
- zip 不使用 ``extractall``，逐成员校验后再原子提交。

本文件由算子 5 在本目录内自带，不跨目录 import 兄弟算子。
"""
from __future__ import annotations

import codecs
import json
import os
import re
import shutil
import stat
import tempfile
import threading
import zipfile
from pathlib import Path
from typing import Any, Dict, Iterator, List, Optional, Tuple

#: Windows 保留设备名（含带扩展名的形式）
_RESERVED_NAMES = {
    "con", "prn", "aux", "nul",
    "com1", "com2", "com3", "com4", "com5", "com6", "com7", "com8", "com9",
    "lpt1", "lpt2", "lpt3", "lpt4", "lpt5", "lpt6", "lpt7", "lpt8", "lpt9",
}
_FORBIDDEN_CHARS = set('/\\:*?"<>|')
_URL_SCHEME_RE = re.compile(r"^[a-zA-Z][a-zA-Z0-9+.-]*://")


class TaskBusyError(RuntimeError):
    """同一 uniqueId 已有写操作在进行。"""


class UnsupportedMediaPath(ValueError):
    """媒体路径不满足可搬运的相对路径约定。"""


def validate_unique_id(unique_id: str) -> str:
    """校验 uniqueId 可作为单个目录名使用。"""
    from . import mty_compliance as _mty_compliance
    _mty_compliance.validate_unique_id(unique_id)
    if not isinstance(unique_id, str) or not unique_id.strip():
        raise ValueError("uniqueId 不能为空")
    value = unique_id.strip()
    if value != unique_id:
        raise ValueError("uniqueId 首尾不允许空白字符")
    if value in {".", ".."}:
        raise ValueError("uniqueId 不能是路径占位符")
    if any(ch in _FORBIDDEN_CHARS for ch in value):
        raise ValueError("uniqueId 只能包含单个任务标识，不能包含路径字符")
    if any(ord(ch) < 32 or ord(ch) == 127 for ch in value):
        raise ValueError("uniqueId 不能包含控制字符")
    if value.endswith(".") or value.endswith(" "):
        raise ValueError("uniqueId 不能以点或空格结尾")
    stem = value.split(".")[0].lower()
    if stem in _RESERVED_NAMES:
        raise ValueError("uniqueId 不能使用系统保留设备名")
    return value


def resolved_absolute(path) -> Path:
    """先绝对化再解析：保证返回值一定是绝对路径。

    Windows + Python 3.9 上 ``Path.resolve()`` 对**不存在**的相对路径会直接返回
    相对路径（3.10+ 已修正），会让后续 ``relative_to`` 判断误报"越出任务目录"。
    这里统一先 ``abspath`` 再 ``resolve``，并对返回值做绝对性兜底。
    """
    candidate = Path(os.path.abspath(str(Path(path).expanduser())))
    try:
        resolved = candidate.resolve()
    except OSError:  # pragma: no cover - 极端情况下保留绝对化结果
        return candidate
    return resolved if resolved.is_absolute() else candidate


def resolve_root(output_root) -> Path:
    """解析并创建输出根目录（先创建再解析，保证得到规范绝对路径）。"""
    root = Path(os.path.abspath(str(Path(output_root).expanduser())))
    root.mkdir(parents=True, exist_ok=True)
    resolved = resolved_absolute(root)
    return resolved


def task_dir(output_root, unique_id: str, create: bool = True) -> Path:
    """任务根目录 ``OUTPUT_DIR/<uniqueId>``。"""
    from . import mty_compliance as _mty_compliance
    _mty_compliance.validate_unique_id(unique_id)
    value = validate_unique_id(unique_id)
    root = resolve_root(output_root)
    target = resolved_absolute(root / value)
    if not is_within(root, target) or target == root:
        raise ValueError("uniqueId 指向任务目录之外")
    if create:
        target.mkdir(parents=True, exist_ok=True)
    return target


def is_within(root: Path, candidate: Path) -> bool:
    """候选路径（已 resolve）是否位于 root 之内。"""
    try:
        candidate.relative_to(root)
    except ValueError:
        return False
    return True


def resolve_in_task(task_root: Path, candidate: str) -> Path:
    """把 ``inputFile`` 解析到任务根内。

    允许任务根内绝对路径与相对路径，拒绝跨 uniqueId、符号链接逃逸与任务根外文件。
    """
    if not isinstance(candidate, str) or not candidate.strip():
        raise ValueError("inputFile 不能为空")
    root = resolved_absolute(task_root)
    raw = Path(candidate)
    target = resolved_absolute(raw if raw.is_absolute() else root / candidate)
    if not is_within(root, target):
        raise ValueError("inputFile 越出当前任务目录")
    return target


def resolve_media(manifest_root: Path, relative_path: str) -> Path:
    """解析样本中的媒体相对路径：相对 manifest 根，禁绝对、``..``、反斜杠与 URL。"""
    if not isinstance(relative_path, str) or not relative_path.strip():
        raise UnsupportedMediaPath("媒体路径不能为空")
    value = relative_path.strip()
    if _URL_SCHEME_RE.match(value):
        raise UnsupportedMediaPath("媒体路径不允许 URL：%s" % value)
    if "\\" in value:
        raise UnsupportedMediaPath("媒体路径不允许反斜杠：%s" % value)
    if Path(value).is_absolute() or value.startswith("/") or value.startswith("~"):
        raise UnsupportedMediaPath("媒体路径必须是相对路径：%s" % value)
    parts = [part for part in value.split("/") if part not in ("", ".")]
    if any(part == ".." for part in parts):
        raise UnsupportedMediaPath("媒体路径不允许 .. 逃逸：%s" % value)
    root = resolved_absolute(manifest_root)
    target = resolved_absolute(root / value)
    if not is_within(root, target):
        raise UnsupportedMediaPath("媒体路径越出运行目录：%s" % value)
    return target


def iter_decoded_lines(path, encoding: str = "utf-8",
                       chunk_size: int = 65536) -> Iterator[str]:
    """严格增量解码并逐行产出；解码失败直接抛 ``UnicodeDecodeError``（不猜编码）。"""
    decoder = codecs.getincrementaldecoder(encoding)(errors="strict")
    buffer = ""
    with open(path, "rb") as stream:
        while True:
            chunk = stream.read(chunk_size)
            if not chunk:
                break
            buffer += decoder.decode(chunk, final=False)
            while "\n" in buffer:
                line, buffer = buffer.split("\n", 1)
                yield line[:-1] if line.endswith("\r") else line
        buffer += decoder.decode(b"", final=True)
    if buffer:
        yield buffer[:-1] if buffer.endswith("\r") else buffer


def assert_utf8(path) -> None:
    """预检文件是否为严格 UTF-8；不是则抛 ``UnicodeDecodeError``。"""
    for _ in iter_decoded_lines(path, "utf-8"):
        pass


def read_jsonl_stream(path) -> Iterator[Tuple[int, Optional[dict], Optional[str]]]:
    """逐行读取 JSONL，yield ``(行号, 记录, 错误原因)``；坏行不中断流程。"""
    for lineno, line in enumerate(iter_decoded_lines(path, "utf-8"), 1):
        if not line.strip():
            continue
        try:
            obj = json.loads(line)
        except json.JSONDecodeError:
            yield lineno, None, "invalid_json"
            continue
        if not isinstance(obj, dict):
            yield lineno, None, "invalid_record"
            continue
        yield lineno, obj, None


class JsonlWriter:
    """流式写入器：边处理边落盘，配合 ``os.replace`` 原子提交。"""

    def __init__(self, path):
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.count = 0
        self._stream = open(self.path, "w", encoding="utf-8", newline="\n")

    def write(self, record: dict) -> None:
        self._stream.write(json.dumps(record, ensure_ascii=False) + "\n")
        self.count += 1

    def close(self) -> None:
        if not self._stream.closed:
            self._stream.close()

    def __enter__(self) -> "JsonlWriter":
        return self

    def __exit__(self, *exc) -> None:
        self.close()


def write_jsonl(path, records) -> int:
    count = 0
    with JsonlWriter(path) as writer:
        for record in records:
            writer.write(record)
        count = writer.count
    return count


def atomic_write_text(path, text: str) -> Path:
    """同目录临时文件写入后原子替换，避免半截文件被下游读到。"""
    target = Path(path)
    target.parent.mkdir(parents=True, exist_ok=True)
    handle, tmp_name = tempfile.mkstemp(dir=str(target.parent), prefix=target.name + ".", suffix=".tmp")
    try:
        with os.fdopen(handle, "w", encoding="utf-8", newline="\n") as stream:
            stream.write(text)
        os.replace(tmp_name, target)
    except BaseException:
        try:
            os.unlink(tmp_name)
        except OSError:
            pass
        raise
    return target


def atomic_write_json(path, payload: Any, indent: int = 2) -> Path:
    return atomic_write_text(path, json.dumps(payload, ensure_ascii=False, indent=indent))


def cleanup_files(*paths) -> None:
    for path in paths:
        if path is None:
            continue
        try:
            Path(path).unlink()
        except OSError:
            pass


class TaskLock:
    """任务级写锁：锁文件位于任务目录之外，跨进程/容器互斥。

    同一 ``uniqueId`` 同时只允许一个写操作；不同 ``uniqueId`` 可并发。
    同一进程内允许重入（HTTP 层先锁住整个请求，``inference.run`` 再锁一次）。
    """

    _HELD: Dict[str, int] = {}
    _GUARD = threading.Lock()

    def __init__(self, output_root, unique_id: str, timeout: float = 0.0):
        self.value = validate_unique_id(unique_id)
        root = resolve_root(output_root)
        self.lock_dir = root / ".locks"
        self.lock_path = self.lock_dir / ("%s.lock" % self.value)
        self.timeout = timeout
        self._fd: Optional[int] = None
        self._owner = False

    def acquire(self) -> "TaskLock":
        key = str(self.lock_path)
        with TaskLock._GUARD:
            if TaskLock._HELD.get(key):
                TaskLock._HELD[key] += 1
                self._owner = False
                return self
        self.lock_dir.mkdir(parents=True, exist_ok=True)
        try:
            self._fd = os.open(str(self.lock_path), os.O_CREAT | os.O_EXCL | os.O_WRONLY)
        except FileExistsError as exc:
            raise TaskBusyError("任务 %s 正在被另一个写操作占用" % self.value) from exc
        os.write(self._fd, str(os.getpid()).encode("ascii"))
        with TaskLock._GUARD:
            TaskLock._HELD[key] = 1
        self._owner = True
        return self

    def release(self) -> None:
        key = str(self.lock_path)
        if not self._owner:
            with TaskLock._GUARD:
                if TaskLock._HELD.get(key):
                    TaskLock._HELD[key] -= 1
            return
        if self._fd is not None:
            try:
                os.close(self._fd)
            finally:
                self._fd = None
        try:
            self.lock_path.unlink()
        except OSError:
            pass
        with TaskLock._GUARD:
            TaskLock._HELD.pop(key, None)
        self._owner = False

    def __enter__(self) -> "TaskLock":
        return self.acquire()

    def __exit__(self, *exc) -> None:
        self.release()


def next_run_id(operator_dir: Path, prefix: str = "run", width: int = 3) -> str:
    """在算子目录下取下一个可用的 run_id（不覆盖上游或其他 run）。"""
    used = set()
    if operator_dir.exists():
        for child in operator_dir.iterdir():
            if child.is_dir() and child.name.startswith(prefix):
                suffix = child.name[len(prefix):]
                if suffix.isdigit():
                    used.add(int(suffix))
    index = 1
    while index in used:
        index += 1
    return "%s%0*d" % (prefix, width, index)


def remove_task(output_root, unique_id: str) -> bool:
    """删除整个任务目录；锁在任务目录之外，先取锁再清理。"""
    root = resolve_root(output_root)
    with TaskLock(root, unique_id):
        task = task_dir(root, unique_id, create=False)
        if not is_within(root, task) or not task.exists():
            return False
        shutil.rmtree(task)
        return True


def safe_extract_zip(archive, destination, max_files: int = 1000,
                     max_unpacked_bytes: int = 67108864) -> List[str]:
    """安全解压 zip 到空目录，返回相对路径清单。

    拒绝：绝对路径、``..`` 逃逸、符号链接、重复路径、大小写碰撞、超限额。
    """
    dest = Path(destination)
    dest.mkdir(parents=True, exist_ok=True)
    if any(dest.iterdir()):
        raise ValueError("解压目标目录必须为空")

    members: List[Tuple[zipfile.ZipInfo, str]] = []
    seen: Dict[str, str] = {}
    total_bytes = 0
    with zipfile.ZipFile(archive) as zf:
        infos = [info for info in zf.infolist() if not info.is_dir()]
        if len(infos) > max_files:
            raise ValueError("zip 文件数超过限额 %d" % max_files)
        for info in infos:
            name = info.filename
            if not name or name.endswith("/"):
                continue
            if "\\" in name:
                raise ValueError("zip 成员名不允许反斜杠：%s" % name)
            parts = [part for part in name.split("/") if part not in ("", ".")]
            if not parts or any(part == ".." for part in parts):
                raise ValueError("zip 成员路径非法：%s" % name)
            if name.startswith("/") or Path(name).is_absolute():
                raise ValueError("zip 成员不允许绝对路径：%s" % name)
            mode = info.external_attr >> 16
            if mode and stat.S_ISLNK(mode):
                raise ValueError("zip 成员不允许符号链接：%s" % name)
            lowered = "/".join(parts).lower()
            if lowered in seen:
                raise ValueError("zip 存在重复成员（含大小写碰撞）：%s" % name)
            seen[lowered] = name
            total_bytes += info.file_size
            if total_bytes > max_unpacked_bytes:
                raise ValueError("zip 解压总大小超过限额 %d 字节" % max_unpacked_bytes)
            members.append((info, "/".join(parts)))

        written: List[str] = []
        for info, relative in members:
            target = dest / relative
            target.parent.mkdir(parents=True, exist_ok=True)
            with zf.open(info) as source, open(target, "wb") as sink:
                shutil.copyfileobj(source, sink)
            written.append(relative)
    return written


def commit_directory(source, destination) -> None:
    """把校验完成的临时目录原子提交为目标目录（目标必须不存在）。"""
    src, dst = Path(source), Path(destination)
    dst.parent.mkdir(parents=True, exist_ok=True)
    if dst.exists():
        raise ValueError("目标目录已存在：%s" % dst.name)
    os.replace(str(src), str(dst))
