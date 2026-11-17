"""★交付包打包：把包内成员压成真实 ZIP（确定性、原子提交、成员路径安全）。

设计要点（表1 #23「压缩成什么格式：下拉框，默认 zip，只有 zip」）：
- 只支持 ZIP（调用方由配置校验保证 ``archive_format=zip``）；
- 成员路径必须是包内相对路径（拒绝绝对路径、盘符、反斜杠与 ``..`` 穿越）；
- 同一次打包内成员路径唯一（大小写不敏感，避免解压端冲突）；
- 先写 ``<name>.partial``，成功后 ``os.replace`` 原子替换，不产生半截包；
- 用 ZIP_DEFLATED 真实压缩；时间戳统一注入，保证同输入同输出（可复现）。
"""
from __future__ import annotations

import hashlib
import os
import posixpath
import re
import zipfile
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional, Sequence, Tuple

__all__ = [
    "ArchiveError",
    "build_package",
    "sha256_bytes",
    "zip_bytes",
]

_WINDOWS_DRIVE_RE = re.compile(r"^[A-Za-z]:")
_SAFE_MEMBER_RE = re.compile(r"^[^\x00-\x1f]+$")


class ArchiveError(ValueError):
    """包成员或打包参数不合法。"""


def sha256_bytes(payload: bytes) -> str:
    return hashlib.sha256(payload).hexdigest()


def _normalize_member(name: str) -> str:
    if not isinstance(name, str):
        raise ArchiveError("包成员路径必须是字符串")
    raw = name.strip()
    if not raw:
        raise ArchiveError("包成员路径不能为空")
    if "\\" in raw:
        raise ArchiveError("包成员路径必须使用 / 分隔符：%r" % name)
    if raw.startswith("/") or _WINDOWS_DRIVE_RE.match(raw):
        raise ArchiveError("包成员路径必须是相对路径：%r" % name)
    if not _SAFE_MEMBER_RE.match(raw):
        raise ArchiveError("包成员路径包含控制字符：%r" % name)
    normalized = posixpath.normpath(raw)
    if normalized in {"", ".", ".."} or normalized.startswith("../"):
        raise ArchiveError("包成员路径不得越出包根：%r" % name)
    if posixpath.isabs(normalized):
        raise ArchiveError("包成员路径必须是相对路径：%r" % name)
    return normalized


def _normalize_timestamp(timestamp: Optional[Sequence[int]]) -> Tuple[int, int, int, int, int, int]:
    if timestamp is None:
        return (2026, 1, 1, 0, 0, 0)
    values = [int(item) for item in timestamp]
    if len(values) != 6:
        raise ArchiveError("时间戳必须是 6 元组 (year, month, day, hour, minute, second)")
    year, month, day, hour, minute, second = values
    if not (1980 <= year <= 2107):
        raise ArchiveError("ZIP 时间戳年份必须在 1980–2107 之间")
    return (year, month, day, hour, minute, second)


def build_package(target: Path, entries: Iterable[Tuple[str, bytes]],
                  timestamp: Optional[Sequence[int]] = None) -> Dict[str, Any]:
    """把 ``(成员路径, bytes)`` 序列写成一个 ZIP，返回真实统计。"""
    destination = Path(target)
    destination.parent.mkdir(parents=True, exist_ok=True)
    stamp = _normalize_timestamp(timestamp)

    prepared: List[Tuple[str, bytes]] = []
    seen: Dict[str, str] = {}
    total = 0
    for name, payload in entries:
        normalized = _normalize_member(name)
        key = normalized.lower()
        if key in seen:
            raise ArchiveError("包内存在重复成员路径：%r（与 %r 冲突）"
                               % (normalized, seen[key]))
        seen[key] = normalized
        if not isinstance(payload, (bytes, bytearray)):
            raise ArchiveError("包成员内容必须是 bytes：%r" % name)
        prepared.append((normalized, bytes(payload)))
        total += len(payload)

    if not prepared:
        raise ArchiveError("交付包至少需要一个成员（训练文件）")

    temp = destination.with_name(destination.name + ".partial")
    try:
        with zipfile.ZipFile(temp, "w", compression=zipfile.ZIP_DEFLATED,
                             compresslevel=6) as bundle:
            for name, payload in prepared:
                info = zipfile.ZipInfo(name, date_time=stamp)
                info.compress_type = zipfile.ZIP_DEFLATED
                info.external_attr = 0o644 << 16
                bundle.writestr(info, payload)
        os.replace(temp, destination)
    finally:
        if temp.exists():
            try:
                temp.unlink()
            except OSError:                       # pragma: no cover
                pass

    raw = destination.read_bytes()
    return {"names": [name for name, _ in prepared],
            "entries": len(prepared),
            "uncompressed_bytes": total,
            "bytes": len(raw),
            "sha256": sha256_bytes(raw)}


def zip_bytes(target: Path) -> bytes:
    return Path(target).read_bytes()
