"""流式 JSONL 读写、原子替换、任务目录隔离、任务锁与媒体携带。

设计约束（DEVELOPMENT_CONTRACT §3、通用提示词 §三）：
- ``uniqueId`` 只能是单个安全目录名；用 ``resolve`` + ``relative_to`` 核对真实目标；
- ``inputFile`` 允许任务根下相对路径或任务根内绝对路径，拒绝跨任务与目录穿越；
- 坏行/编码错误必须逐行上报，不静默丢弃；空行单独计数，不与坏行混同；
- 输出先写 ``.tmp`` 再 ``os.replace``，避免与输入同路径时截断源文件。
"""
from __future__ import annotations

import json
import os
import shutil
import time
from contextlib import contextmanager
from pathlib import Path
from typing import Iterator, List, Optional, Sequence, Tuple

_WIN_RESERVED = {"CON", "PRN", "AUX", "NUL"} | {
    "COM%d" % index for index in range(1, 10)} | {"LPT%d" % index for index in range(1, 10)}
_ILLEGAL_CHARS = ("/", "\\", ":", "\0")

BLANK_LINE = "blank_line"


class TaskBusy(RuntimeError):
    """同一 uniqueId 已有写操作在进行。"""


def validate_unique_id(unique_id: object) -> str:
    """uniqueId 必须是单个安全目录名：拒绝空、点路径、分隔符、盘符、UNC 与保留设备名。"""
    # [v4.2] mty_compliance 已停用：移除其对 uniqueId 的重复校验调用（本地校验已覆盖）
    # from . import mty_compliance as _mty_compliance
    # _mty_compliance.validate_unique_id(unique_id)
    if not isinstance(unique_id, str) or not unique_id.strip():
        raise ValueError("uniqueId 不能为空")
    safe = unique_id.strip()
    if safe in {".", ".."} or any(char in safe for char in _ILLEGAL_CHARS):
        raise ValueError("uniqueId 只能是单个安全目录名")
    if safe.upper() in _WIN_RESERVED:
        raise ValueError("uniqueId 使用了 Windows 保留设备名")
    return safe


def task_dir(output_root: Path, unique_id: str, create: bool = True) -> Path:
    """解析 OUTPUT_DIR/<uniqueId>，用 resolve + relative_to 核对真实目标。"""
    # [v4.2] mty_compliance 已停用：移除其对 uniqueId 的重复校验调用（本地校验已覆盖）
    # from . import mty_compliance as _mty_compliance
    # _mty_compliance.validate_unique_id(unique_id)
    safe = validate_unique_id(unique_id)
    root = Path(output_root).resolve()
    target = (root / safe).resolve()
    try:
        target.relative_to(root)
    except ValueError as exc:  # pragma: no cover - 被 validate_unique_id 提前拦截
        raise ValueError("uniqueId 越出输出根目录") from exc
    if create:
        target.mkdir(parents=True, exist_ok=True)
    return target


def resolve_in_task(task: Path, relative: str) -> Path:
    """解析任务目录内路径；拒绝跨 uniqueId、目录穿越和任务根外文件。"""
    if not isinstance(relative, str) or not relative.strip():
        raise ValueError("inputFile 不能为空")
    raw = relative.strip()
    root = Path(task).resolve()
    candidate = Path(raw)
    target = (candidate if candidate.is_absolute() else root / candidate).resolve()
    try:
        target.relative_to(root)
    except ValueError as exc:
        raise ValueError("inputFile 越出任务目录：%s" % relative) from exc
    return target


def read_jsonl(path: Path) -> Iterator[Tuple[int, Optional[dict], Optional[str]]]:
    """逐行流式读取 JSONL，返回 (物理行号, 记录, 错误原因)；坏行不中断、不静默丢弃。

    错误原因：blank_line / encoding_error / json_parse_error / record_not_object。
    """
    with open(str(path), "rb") as handle:
        for lineno, raw in enumerate(handle, 1):
            stripped = raw.strip()
            if not stripped:
                yield lineno, None, BLANK_LINE
                continue
            try:
                text = stripped.decode("utf-8-sig")
            except UnicodeDecodeError:
                yield lineno, None, "encoding_error"
                continue
            try:
                payload = json.loads(text)
            except json.JSONDecodeError:
                yield lineno, None, "json_parse_error"
                continue
            if not isinstance(payload, dict):
                yield lineno, None, "record_not_object"
                continue
            yield lineno, payload, None


def atomic_write_jsonl(path: Path, records: Sequence[dict],
                       source_path: Optional[Path] = None) -> int:
    """先写临时文件再 os.replace，避免与输入同路径时截断源文件。"""
    target = Path(path)
    if source_path is not None and Path(source_path).resolve() == target.resolve():
        raise ValueError("输出路径与输入路径相同，拒绝写入以免截断源文件")
    target.parent.mkdir(parents=True, exist_ok=True)
    tmp = target.with_name(target.name + ".tmp")
    count = 0
    try:
        with open(str(tmp), "w", encoding="utf-8", newline="\n") as handle:
            for record in records:
                handle.write(json.dumps(record, ensure_ascii=False) + "\n")
                count += 1
        os.replace(str(tmp), str(target))
    finally:
        if tmp.exists():
            tmp.unlink()
    return count


def next_run_id(parent: Path) -> str:
    """扫描已有 runNNN 目录，返回下一个 run_id，避免重复运行互相覆盖。"""
    parent = Path(parent)
    highest = 0
    if parent.is_dir():
        for child in parent.iterdir():
            if child.is_dir() and child.name.startswith("run") and child.name[3:].isdigit():
                highest = max(highest, int(child.name[3:]))
    return "run%03d" % (highest + 1)


@contextmanager
def task_lock(output_root: Path, unique_id: str, timeout: float = 0.0,
              poll_interval: float = 0.05):
    """任务级排他锁；锁文件放在任务目录之外的 <output_root>/.locks。"""
    safe = validate_unique_id(unique_id)
    lock_root = Path(output_root).resolve() / ".locks"
    lock_root.mkdir(parents=True, exist_ok=True)
    lock_path = lock_root / (safe + ".lock")
    deadline = time.monotonic() + max(timeout, 0.0)
    handle = None
    while True:
        try:
            handle = os.open(str(lock_path), os.O_CREAT | os.O_EXCL | os.O_WRONLY)
            break
        except FileExistsError:
            if time.monotonic() >= deadline:
                raise TaskBusy("task_busy: 同一 uniqueId 正在处理中")
            time.sleep(poll_interval)
    try:
        os.write(handle, str(os.getpid()).encode("ascii"))
        yield lock_path
    finally:
        os.close(handle)
        try:
            os.unlink(str(lock_path))
        except FileNotFoundError:  # pragma: no cover
            pass


def safe_media_path(value: object) -> str:
    """媒体引用必须是可搬运的相对路径：拒绝 URL/UNC/绝对路径(含盘符)/``..``/反斜杠逃逸。"""
    if not isinstance(value, str) or not value.strip():
        raise ValueError("媒体引用不能为空")
    raw = value.strip()
    if "://" in raw or raw.startswith("\\\\"):
        raise ValueError("媒体引用不支持 URL 或 UNC 路径")
    candidate = raw.replace("\\", "/")
    pure = Path(candidate)
    # Windows 下 ``Path("/abs/x").is_absolute()`` 为 False（无盘符），
    # 因此必须显式拒绝以 "/" 开头和带盘符的写法，否则跨平台会漏掉绝对路径。
    rooted = candidate.startswith("/") or (len(candidate) > 1 and candidate[1] == ":")
    if pure.is_absolute() or rooted or ".." in pure.parts:
        raise ValueError("媒体引用必须是包内相对路径")
    while candidate.startswith("./"):
        candidate = candidate[2:]
    if not candidate:
        raise ValueError("媒体引用不能为空")
    return candidate


def copy_media(run_dir: Path, media_root: Path, paths: Sequence[str]) -> Tuple[List[str], List[str]]:
    """把被引用的媒体按相对路径复制进本次运行目录；返回 (已携带, 缺失)。"""
    carried: List[str] = []
    missing: List[str] = []
    run_dir = Path(run_dir)
    root = Path(media_root).resolve()
    for relative in paths:
        source = (root / relative).resolve()
        try:
            source.relative_to(root)
        except ValueError:
            missing.append(relative)
            continue
        if not source.is_file():
            missing.append(relative)
            continue
        target = run_dir / relative
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(str(source), str(target))
        carried.append(relative)
    return carried, missing
