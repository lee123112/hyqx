# [v4.2] 血缘/阶段归档/运输输出已停用：本模块不再被 app.py / inference.py 调用，代码保留存档，不再维护。
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
    "read_lineage",
    "statistics_snapshot",
    "write_lineage",
    "write_stage_context",
]


LINEAGE_NAME = "lineage.json"
STAGE_CONTEXT_NAME = "stage_context.json"


def now_iso() -> str:
    return datetime.now().astimezone().isoformat(timespec="seconds")


def config_snapshot(config: Any) -> Dict[str, Any]:
    """配置快照：只记录业务开关、阈值与传输约束，不含凭据、路径或模型权重。"""
    return {"operator": {"number": config.operator.number,
                         "id": config.operator.id,
                         "name": config.operator.name,
                         "label": config.operator.label,
                         "version": config.operator.version,
                         "enabled": bool(config.operator.enabled)},
            "stage_id": config.stage_id,
            "transport": {"format": config.transport.format,
                          "max_response_bytes": config.transport.max_response_bytes},
            "business": config.business_snapshot()}


#: 统计快照中允许原样保留的复合结构上限（字符数），超出则退化为元素个数
MAX_SNAPSHOT_CHARS = 4096


def statistics_snapshot(statistics: Dict[str, Any]) -> Dict[str, Any]:
    """统计快照：标量原样保留，小型列表/字典保留，巨型结构退化为元素个数。"""
    snapshot: Dict[str, Any] = {}
    for key, value in statistics.items():
        if isinstance(value, (str, int, float, bool)) or value is None:
            snapshot[key] = value
            continue
        if isinstance(value, (list, tuple, dict)):
            try:
                encoded = json.dumps(value, ensure_ascii=False)
            except (TypeError, ValueError):
                snapshot[key] = len(value)
                continue
            snapshot[key] = value if len(encoded) <= MAX_SNAPSHOT_CHARS else len(value)
            continue
        snapshot[key] = str(value)[:120]
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
    except (json.JSONDecodeError, UnicodeDecodeError, OSError):
        return []
    if isinstance(payload, list):
        return [item for item in payload if isinstance(item, dict)]
    if isinstance(payload, dict):
        return [payload]
    return []
