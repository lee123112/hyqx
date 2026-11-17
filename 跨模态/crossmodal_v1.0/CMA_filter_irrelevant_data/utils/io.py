"""流式 JSONL 读写与任务目录管理。"""
from __future__ import annotations
import json
from pathlib import Path
from typing import Iterator, Tuple


def read_jsonl(path: Path) -> Iterator[Tuple[int, dict | None, str | None]]:
    """逐行读取 JSONL，返回 (行号, 记录, 错误原因)。坏行不中断。"""
    with open(path, "r", encoding="utf-8", errors="replace") as f:
        for lineno, line in enumerate(f, 1):
            line = line.strip()
            if not line:
                continue
            try:
                obj = json.loads(line)
                if not isinstance(obj, dict):
                    yield lineno, None, "record_not_object"
                else:
                    yield lineno, obj, None
            except json.JSONDecodeError:
                yield lineno, None, "json_parse_error"


def write_jsonl(path: Path, records) -> int:
    path.parent.mkdir(parents=True, exist_ok=True)
    count = 0
    with open(path, "w", encoding="utf-8") as f:
        for rec in records:
            f.write(json.dumps(rec, ensure_ascii=False) + "\n")
            count += 1
    return count


def task_dir(output_root: Path, unique_id: str, create: bool = True) -> Path:
    """按 uniqueId 隔离的任务目录。"""
    from . import mty_compliance as _mty_compliance
    _mty_compliance.validate_unique_id(unique_id)
    if not isinstance(unique_id, str) or not unique_id.strip():
        raise ValueError("uniqueId 不能为空")
    safe = unique_id.strip()
    if safe in {".", ".."} or "/" in safe or "\\" in safe or ":" in safe:
        raise ValueError("uniqueId 只能是单个安全目录名")
    if safe.upper() in {"CON", "PRN", "AUX", "NUL", "COM1", "LPT1"}:
        raise ValueError("uniqueId 使用了 Windows 保留设备名")
    root = Path(output_root).resolve()
    d = (root / safe).resolve()
    try:
        d.relative_to(root)
    except ValueError as exc:
        raise ValueError("uniqueId 越出输出根目录") from exc
    if create:
        d.mkdir(parents=True, exist_ok=True)
    return d


def resolve_in_task(task: Path, rel_path: str) -> Path:
    """解析任务目录内路径，防止目录逃逸。"""
    p = (task / rel_path).resolve()
    try:
        p.relative_to(task.resolve())
    except ValueError:
        raise ValueError(f"路径越出任务目录: {rel_path}")
    return p
