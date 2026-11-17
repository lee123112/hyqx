"""JSONL 流式读写、任务目录管理与安全路径校验。"""
import codecs
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


def safe_join(base: Path, rel_path: str) -> Path:
    """Resolve ``rel_path`` under ``base`` and reject any directory escape."""
    if not isinstance(rel_path, str) or not rel_path.strip():
        raise ValueError("路径不能为空")
    base = Path(base).resolve()
    target = (base / rel_path).resolve()
    try:
        target.relative_to(base)
    except ValueError as exc:
        raise ValueError("非法路径：不允许访问任务目录之外的文件") from exc
    return target


def iter_decoded_lines(path, encoding: str, chunk_size: int = 65536) -> Iterator[str]:
    """Incrementally decode a file and yield one logical line at a time."""
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


def select_encoding(path, encodings) -> str:
    """Find the first configured codec that strictly decodes the complete file."""
    if not encodings:
        raise ValueError("未配置输入编码")
    errors = []
    for encoding in encodings:
        try:
            for _ in iter_decoded_lines(path, encoding):
                pass
            return encoding.lower()
        except (LookupError, OSError, UnicodeDecodeError) as exc:
            errors.append(type(exc).__name__)
    raise ValueError("输入文件无法按配置编码解码：" + ",".join(errors))


def read_jsonl_stream(path) -> Iterator[Tuple[int, Optional[dict], Optional[str]]]:
    """逐行读取 JSONL，yield (行号, 记录, 错误原因)。损坏行不中断流程。"""
    for lineno, line in enumerate(iter_decoded_lines(path, "utf-8"), 1):
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


