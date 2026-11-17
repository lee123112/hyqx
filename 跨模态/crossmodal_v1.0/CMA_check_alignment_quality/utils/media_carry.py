"""媒体搬运：把样本引用的媒体按**原相对路径**复制进本次 run 目录。

为什么必须搬运：阶段末由工程侧把 run 目录打包为内容对象上传给下游阶段；
若媒体只留绝对路径或跨任务引用，下游阶段与人工复核都拿不到字节。
"""
from __future__ import annotations

import shutil
from pathlib import Path
from typing import Dict, Iterable, List, Optional, Set, Tuple

from . import io as io_utils

#: 上游算子随包回传的清单名单（示例：``19_high_confidence_pool.jsonl``）
_SIDECAR_PREFIXES = tuple(str(index) for index in range(0, 30))


class MediaCarrier:
    """按需搬运媒体并统计体积；重复引用只搬一次。"""

    def __init__(self, source_root, run_dir, reserved: Iterable[str] = ()):
        self.source_root = io_utils.resolved_absolute(source_root)
        self.run_dir = io_utils.resolved_absolute(run_dir)
        self.reserved: Set[str] = {str(name).lower() for name in reserved}
        self._sizes: Dict[str, int] = {}
        self._order: List[str] = []

    # -- 路径 ------------------------------------------------------------- #
    def resolve(self, relative_path: str) -> Path:
        """解析媒体相对路径（拒绝绝对路径/``..``/反斜杠/URL）。"""
        return io_utils.resolve_media(self.source_root, relative_path)

    # -- 搬运 ------------------------------------------------------------- #
    def register(self, relative_path: str) -> int:
        if relative_path in self._sizes:
            return self._sizes[relative_path]
        source = self.resolve(relative_path)
        if not source.is_file():
            raise FileNotFoundError(relative_path)
        target = self.run_dir / relative_path
        target.parent.mkdir(parents=True, exist_ok=True)
        if not target.exists() or target.stat().st_size != source.stat().st_size:
            shutil.copy2(str(source), str(target))
        size = target.stat().st_size
        self._sizes[relative_path] = size
        self._order.append(relative_path)
        return size

    def carry(self, relative_path: str) -> str:
        self.register(relative_path)
        return relative_path

    def carry_sidecars(self) -> List[str]:
        """把输入目录中的上游清单/名单随包带走（除本算子自身产物）。"""
        carried: List[str] = []
        if not self.source_root.is_dir():
            return carried
        for path in sorted(self.source_root.iterdir()):
            if not path.is_file():
                continue
            name = path.name
            if path.suffix.lower() != ".jsonl":
                continue
            if name.lower() in self.reserved:
                continue
            if not name[:2].isdigit() or name[2] != "_":
                continue
            target = self.run_dir / name
            target.parent.mkdir(parents=True, exist_ok=True)
            if not target.exists() or target.stat().st_size != path.stat().st_size:
                shutil.copy2(str(path), str(target))
            carried.append(name)
        return carried

    # -- 统计 ------------------------------------------------------------- #
    @property
    def sizes(self) -> Dict[str, int]:
        return dict(self._sizes)

    @property
    def media_files(self) -> List[str]:
        return list(self._order)

    @property
    def total_bytes(self) -> int:
        return int(sum(self._sizes.values()))

    @property
    def count(self) -> int:
        return len(self._sizes)

    def summary(self) -> Dict[str, object]:
        return {
            "files": self.count,
            "bytes": self.total_bytes,
            "paths": list(self._order),
        }


def media_candidates(entries: Iterable[Tuple[str, Optional[str]]]) -> List[str]:
    """从 ``(模态, 相对路径)`` 序列里取出非空且去重的媒体清单。"""
    ordered: List[str] = []
    seen: Set[str] = set()
    for _kind, relative in entries:
        if not relative or relative in seen:
            continue
        seen.add(relative)
        ordered.append(relative)
    return ordered
