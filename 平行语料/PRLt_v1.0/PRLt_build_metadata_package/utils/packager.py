"""数据集打包工具：sha256、TSV 流式写入、原子 JSON 写入与 ZIP 归档。

约定：
- 所有产物先写 ``*.tmp``，句柄关闭后再 ``os.replace`` 原子替换，避免把输入文件当
  输出文件时被截断，也避免下游读到半成品。
- ``sha256`` 与 ``bytes`` 一律从真实落盘文件读取计算，不使用估算值。
- ZIP 归档不包含自身哈希：zip 的 sha256 由血缘 ``statistics.package_sha256`` 承载，
  避免清单自引用（manifest 在 zip 内部，无法包含 zip 自身哈希）。
"""
import csv
import hashlib
import json
import os
import zipfile
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional, Tuple

_CHUNK = 1024 * 256


def sha256_file(path) -> str:
    """流式计算文件 sha256，不把整文件读入内存。"""
    digest = hashlib.sha256()
    with open(path, "rb") as stream:
        while True:
            chunk = stream.read(_CHUNK)
            if not chunk:
                break
            digest.update(chunk)
    return digest.hexdigest()


def file_meta(path, records: Optional[int] = None) -> Dict[str, Any]:
    """返回真实文件元数据：名称、绝对路径、字节数与 sha256。"""
    p = Path(path)
    meta: Dict[str, Any] = {
        "name": p.name,
        "path": str(p),
        "bytes": p.stat().st_size,
        "sha256": sha256_file(p),
    }
    if records is not None:
        meta["records"] = records
    return meta


def flatten_for_tsv(value: Any) -> str:
    """把任意 JSON 值压平成 TSV 单元格文本（容器序列化为紧凑 JSON）。"""
    if value is None:
        return ""
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, str):
        return value
    if isinstance(value, (int, float)):
        return repr(value)
    return json.dumps(value, ensure_ascii=False, separators=(",", ":"))


class TsvWriter:
    """TSV 流式写入器：固定列顺序，含分隔符/换行/引号的单元格自动引用。"""

    def __init__(self, path, fields: Iterable[str], delimiter: str = "\t"):
        self.path = Path(path)
        self.fields: List[str] = list(fields)
        self.count = 0
        self._f = open(path, "w", encoding="utf-8", newline="")
        self._writer = csv.writer(
            self._f, delimiter=delimiter, quoting=csv.QUOTE_MINIMAL, lineterminator="\n")
        self._writer.writerow(self.fields)

    def write(self, record: Dict[str, Any]) -> None:
        self._writer.writerow([flatten_for_tsv(record.get(field)) for field in self.fields])
        self.count += 1

    def close(self) -> None:
        self._f.close()

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        self.close()

    def abort(self) -> None:
        self.close()
        try:
            self.path.unlink()
        except OSError:
            pass


def write_json_atomic(path, data) -> Path:
    """原子写入 JSON（UTF-8，缩进 2）。"""
    target = Path(path)
    tmp = target.with_name(target.name + ".tmp")
    tmp.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
    os.replace(tmp, target)
    return target


def write_zip_atomic(package_path, members: List[Tuple[Path, str]],
                     compression: str = "deflated") -> Path:
    """原子生成 ZIP。members 为 (真实文件路径, 归档内名称) 列表。"""
    target = Path(package_path)
    tmp = target.with_name(target.name + ".tmp")
    method = zipfile.ZIP_STORED if compression == "stored" else zipfile.ZIP_DEFLATED
    with zipfile.ZipFile(tmp, "w", compression=method) as archive:
        for source, arcname in members:
            archive.write(source, arcname=arcname)
    os.replace(tmp, target)
    return target


def remove_quietly(*paths) -> None:
    for path in paths:
        try:
            Path(path).unlink()
        except OSError:
            pass
