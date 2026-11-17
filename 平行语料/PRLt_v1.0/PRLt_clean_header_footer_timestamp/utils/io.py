"""JSONL 流式读写、任务目录管理与目录穿越防护（与算子 4/16 同一工程口径）。"""
import codecs
import json
from pathlib import Path
from typing import Iterator, List, Optional, Tuple


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


def iter_decoded_lines(path, encoding: str, chunk_size: int = 65536) -> Iterator[str]:
    """增量严格解码，一次 yield 一行，避免把整个文件读进内存。"""
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
    """找到第一个能严格解码整个文件的编码，全部失败则抛出 ValueError。"""
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


def count_lines(path) -> int:
    """统计真实落盘的非空行数（用于与 statistics 对齐）。"""
    count = 0
    with open(path, "r", encoding="utf-8") as stream:
        for line in stream:
            if line.strip():
                count += 1
    return count


def read_jsonl_stream(path) -> Iterator[Tuple[int, Optional[dict], Optional[str]]]:
    """逐行读取 JSONL，yield (行号, 记录, 错误原因)；损坏行不中断流程。"""
    with open(path, "r", encoding="utf-8") as stream:
        for lineno, line in enumerate(stream, 1):
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
