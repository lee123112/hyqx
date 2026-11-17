"""JSONL 流式读写与任务目录管理。"""
import json
from pathlib import Path
from typing import Iterator, Optional, Tuple


def ensure_dir(path) -> Path:
    p = Path(path)
    p.mkdir(parents=True, exist_ok=True)
    return p


def validate_unique_id(unique_id: str) -> str:
    """Validate the task identifier before using it as a directory name."""
    if not isinstance(unique_id, str) or not unique_id.strip():
        raise ValueError("uniqueId 不能为空")
    value = unique_id.strip()
    # uniqueId is a task_file identifier, never a path.  Reject both Windows
    # and POSIX separators so validation is stable across local and container
    # environments.
    if value in {".", ".."} or any(ch in value for ch in '/\\:*?"<>|'):
        raise ValueError("uniqueId 只能是单个任务标识，不能包含路径字符")
    return value


def task_dir(output_root, unique_id: str) -> Path:
    value = validate_unique_id(unique_id)
    root = Path(output_root).resolve()
    target = (root / value).resolve()
    try:
        target.relative_to(root)
    except ValueError as exc:
        raise ValueError("uniqueId 指向任务目录之外") from exc
    return ensure_dir(target)


def read_jsonl_stream(path) -> Iterator[Tuple[int, Optional[dict], Optional[str]]]:
    """逐行读取 JSONL，yield (行号, 记录, 错误原因)。损坏行不中断流程。"""
    with open(path, "r", encoding="utf-8") as f:
        for lineno, line in enumerate(f, 1):
            line = line.strip()
            if not line:
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


def write_jsonl(path, records) -> int:
    count = 0
    with open(path, "w", encoding="utf-8") as f:
        for rec in records:
            f.write(json.dumps(rec, ensure_ascii=False) + "\n")
            count += 1
    return count


class JsonlWriter:
    """流式写入器，边处理边落盘。"""

    def __init__(self, path):
        self.path = Path(path)
        self.count = 0
        self._f = open(path, "w", encoding="utf-8")

    def write(self, rec: dict) -> None:
        self._f.write(json.dumps(rec, ensure_ascii=False) + "\n")
        self.count += 1

    def close(self) -> None:
        self._f.close()

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        self.close()
