# [v4.2] 血缘/阶段归档/运输输出已停用：本模块的血缘/阶段上下文功能不再被 app.py /
# inference.py 调用，代码保留存档不再维护；now_iso/config_snapshot/artifact_entry
# 为通用工具仍被 inference.py 保留使用。
"""血缘与阶段上下文构造：真实统计、真实配置快照、可追溯的产物列表。"""
from __future__ import annotations

import json
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional

from . import io as io_utils

__all__ = [
    "LINEAGE_NAME",
    "STAGE_CONTEXT_NAME",
    "artifact_entry",
    "config_snapshot",
    "now_iso",
    "statistics_snapshot",
    "write_lineage",
]


LINEAGE_NAME = "lineage.json"
STAGE_CONTEXT_NAME = "stage_context.json"


def now_iso() -> str:
    return datetime.now().astimezone().isoformat(timespec="seconds")


def config_snapshot(config: Any) -> Dict[str, Any]:
    """配置快照：只记录业务开关、阈值与模型配方，不含任何凭据。"""
    return {"operator": {"name": config.operator.name,
                         "version": config.operator.version},
            "stage_id": config.stage_id,
            "business": config.business_snapshot(),
            "clip": config.clip_snapshot()}


def statistics_snapshot(statistics: Dict[str, Any]) -> Dict[str, Any]:
    """统计快照：只保留标量，避免把大对象写进血缘。"""
    snapshot: Dict[str, Any] = {}
    for key, value in statistics.items():
        if isinstance(value, (str, int, float, bool)) or value is None:
            snapshot[key] = value
        elif isinstance(value, (list, tuple)):
            snapshot[key] = len(value)
    return snapshot


def artifact_entry(relative_path: str, role: str,
                   size: Optional[int] = None) -> Dict[str, Any]:
    item: Dict[str, Any] = {"path": relative_path, "role": role}
    if size is not None:
        item["size"] = int(size)
    return item


def write_lineage(path: Path, records: Iterable[Dict[str, Any]]) -> Path:
    rows = list(records)
    return io_utils.atomic_write_json(Path(path), rows)


def write_stage_context(path: Path, payload: Dict[str, Any]) -> Path:
    return io_utils.atomic_write_json(Path(path), payload)


def read_lineage(path: Path) -> List[Dict[str, Any]]:
    target = Path(path)
    if not target.is_file():
        return []
    try:
        with open(target, "r", encoding="utf-8") as handle:
            payload = json.load(handle)
    except (json.JSONDecodeError, UnicodeDecodeError):
        return []
    if isinstance(payload, list):
        return [item for item in payload if isinstance(item, dict)]
    if isinstance(payload, dict):
        return [payload]
    return []
