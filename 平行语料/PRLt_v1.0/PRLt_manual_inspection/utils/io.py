"""JSONL/JSON 流式读写、任务目录隔离与 uniqueId 路径安全校验。"""
import codecs
import json
import os
from pathlib import Path
from typing import Any, Iterator, List, Optional, Tuple


def ensure_dir(path) -> Path:
    p = Path(path)
    p.mkdir(parents=True, exist_ok=True)
    return p


def validate_unique_id(unique_id: str) -> str:
    """uniqueId = 任务id_文件id，绝不作为路径使用。"""
    if not isinstance(unique_id, str) or not unique_id.strip():
        raise ValueError("uniqueId 不能为空")
    value = unique_id.strip()
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


def iter_decoded_lines(path, encoding: str = "utf-8", chunk_size: int = 65536) -> Iterator[str]:
    """按指定编码增量严格解码，逐行 yield 逻辑行（不整文件读入内存）。"""
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


def select_encoding(path, encodings: List[str]) -> str:
    """选择第一个能严格解码整个文件的编码，保证流式读取可用。"""
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


def iter_input_records(path, encoding: str = "utf-8",
                       input_format: str = "auto") -> Iterator[Tuple[int, Optional[Any], Optional[str]]]:
    """逐条 yield (行号, 记录, 错误原因)；损坏行不中断流程，交调用方入异常清单。"""
    fmt = input_format
    if fmt == "auto":
        fmt = "json" if Path(path).suffix.lower() == ".json" else "jsonl"
    if fmt == "json":
        try:
            doc = json.loads(Path(path).read_text(encoding=encoding))
        except (OSError, UnicodeDecodeError, json.JSONDecodeError):
            yield 0, None, "invalid_json"
            return
        items = doc if isinstance(doc, list) else [doc]
        for index, item in enumerate(items, 1):
            yield index, item, None
        return
    for lineno, line in enumerate(iter_decoded_lines(path, encoding), 1):
        if not line.strip():
            continue
        try:
            obj = json.loads(line)
        except json.JSONDecodeError:
            yield lineno, None, "invalid_json"
            continue
        yield lineno, obj, None


def read_json_document(path) -> Any:
    return json.loads(Path(path).read_text(encoding="utf-8"))


def count_lines(path) -> int:
    count = 0
    with open(path, "rb") as stream:
        for chunk in iter(lambda: stream.read(65536), b""):
            count += chunk.count(b"\n")
    return count


def write_json_atomic(path, payload) -> None:
    """先写 .tmp 再原子替换，避免下游读到半成品。"""
    target = Path(path)
    tmp = target.with_name(target.name + ".tmp")
    tmp.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    os.replace(tmp, target)


def remove_quietly(*paths) -> None:
    for path in paths:
        try:
            Path(path).unlink()
        except OSError:
            pass


class JsonlWriter:
    """流式写入器；先写 `.tmp`，由调用方在整次运行成功后 `commit()` 原子替换。"""

    def __init__(self, path):
        self.path = Path(path)
        self.tmp_path = self.path.with_name(self.path.name + ".tmp")
        self.count = 0
        self._closed = False
        self._f = open(self.tmp_path, "w", encoding="utf-8", newline="\n")

    def write(self, rec: dict) -> None:
        self._f.write(json.dumps(rec, ensure_ascii=False) + "\n")
        self.count += 1

    def close(self) -> None:
        if not self._closed:
            self._f.close()
            self._closed = True

    def commit(self) -> None:
        self.close()
        os.replace(self.tmp_path, self.path)

    def abort(self) -> None:
        self.close()
        remove_quietly(self.tmp_path)

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc, tb):
        if exc_type is None:
            self.close()
        else:
            self.abort()
        return False
