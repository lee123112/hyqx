"""JSONL 流式读写、任务目录管理与目录穿越防护（与算子 4/11/12 同一工程口径）。"""
import json
from pathlib import Path
from typing import Iterator, Optional, Tuple


def ensure_dir(path) -> Path:
    p = Path(path)
    p.mkdir(parents=True, exist_ok=True)
    return p


def validate_unique_id(unique_id: str) -> str:
    """校验任务标识：uniqueId 只能是单个任务标识，不能当路径用。"""
    if not isinstance(unique_id, str) or not unique_id.strip():
        raise ValueError("uniqueId 不能为空")
    value = unique_id.strip()
    # uniqueId = 任务id_文件id，永远是标识而不是路径；同时拒绝 Windows 与 POSIX
    # 的分隔符，保证本地与容器行为一致。
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


def safe_join(root, unique_id: str, relative_path: str) -> Path:
    """将 uniqueId 目录内的相对路径解析为真实路径，拒绝目录穿越与绝对路径。"""
    value = validate_unique_id(unique_id)
    if not isinstance(relative_path, str) or not relative_path.strip():
        raise ValueError("path 不能为空")
    root_dir = Path(root).resolve()
    base = (root_dir / value).resolve()
    raw = Path(relative_path.strip())
    candidate = raw.resolve() if raw.is_absolute() else (base / raw).resolve()
    try:
        candidate.relative_to(base)
    except ValueError as exc:
        raise ValueError("path 指向任务目录之外") from exc
    return candidate


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


class JsonlWriter:
    """流式写入器：边处理边落盘，避免把整批数据留在内存里。"""

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
