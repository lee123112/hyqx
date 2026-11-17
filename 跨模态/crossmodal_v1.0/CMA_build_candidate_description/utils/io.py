"""流式 JSONL 读写与任务目录隔离。"""
from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Iterator, Tuple

_RESERVED = {"CON", "PRN", "AUX", "NUL", "COM1", "COM2", "COM3", "COM4",
             "LPT1", "LPT2", "LPT3"}


def read_jsonl(path: Path) -> Iterator[Tuple[int, dict | None, str | None]]:
    """逐行读取 JSONL，返回 (行号, 记录, 错误原因)。坏行不中断，空行跳过。"""
    with open(path, "r", encoding="utf-8", errors="replace") as f:
        for lineno, line in enumerate(f, 1):
            line = line.strip()
            if not line:
                continue
            try:
                obj = json.loads(line)
            except json.JSONDecodeError:
                yield lineno, None, "json_parse_error"
                continue
            if not isinstance(obj, dict):
                yield lineno, None, "record_not_object"
            else:
                yield lineno, obj, None


class AtomicJsonlWriter:
    """先写同目录临时文件，成功后再原子替换，避免截断同名源文件。"""

    def __init__(self, path: Path):
        self.path = Path(path)
        self.tmp = self.path.with_name(self.path.name + ".tmp")
        self.count = 0
        self._fh = None

    def __enter__(self) -> "AtomicJsonlWriter":
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._fh = open(self.tmp, "w", encoding="utf-8", newline="\n")
        return self

    def write(self, record: dict) -> None:
        self._fh.write(json.dumps(record, ensure_ascii=False) + "\n")
        self.count += 1

    def __exit__(self, exc_type, exc, tb) -> bool:
        self._fh.close()
        if exc_type is None:
            os.replace(str(self.tmp), str(self.path))
        else:
            try:
                self.tmp.unlink()
            except OSError:
                pass
        return False


def write_jsonl_atomic(path: Path, records) -> int:
    with AtomicJsonlWriter(path) as writer:
        for rec in records:
            writer.write(rec)
    return writer.count


def write_jsonl(path: Path, records) -> int:
    """兼容别名，行为与 write_jsonl_atomic 一致（原子替换）。"""
    return write_jsonl_atomic(path, records)


def task_dir(output_root: Path, unique_id: str, create: bool = True) -> Path:
    """按 uniqueId 隔离的任务目录，拒绝空、路径分隔符、穿越和保留设备名。"""
    # [v4.2] mty_compliance 已停用：移除其对 uniqueId 的重复校验调用（本地校验已覆盖）
    # from . import mty_compliance as _mty_compliance
    # _mty_compliance.validate_unique_id(unique_id)
    if not isinstance(unique_id, str) or not unique_id.strip():
        raise ValueError("uniqueId 不能为空")
    safe = unique_id.strip()
    if safe in {".", ".."} or "/" in safe or "\\" in safe or ":" in safe:
        raise ValueError("uniqueId 只能是单个安全目录名")
    if safe.upper() in _RESERVED:
        raise ValueError("uniqueId 使用了 Windows 保留设备名")
    root = Path(output_root).resolve()
    target = (root / safe).resolve()
    try:
        target.relative_to(root)
    except ValueError as exc:
        raise ValueError("uniqueId 越出输出根目录") from exc
    if create:
        target.mkdir(parents=True, exist_ok=True)
    return target


def resolve_in_task(task: Path, rel_path: str) -> Path:
    """解析任务目录内路径，禁止目录穿越与任务外文件。"""
    task = Path(task).resolve()
    raw = Path(rel_path)
    candidate = raw if raw.is_absolute() else task / raw
    resolved = candidate.resolve()
    try:
        resolved.relative_to(task)
    except ValueError:
        raise ValueError(f"路径越出任务目录: {rel_path}")
    return resolved
