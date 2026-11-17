# [v4.2] 血缘/阶段归档/运输输出已停用：本模块不再被 app.py / inference.py 调用，代码保留存档，不再维护。
# [v4.2] 注：仅 now_iso 作为时间工具仍被 inference.py 引用；血缘构建/追加/阶段上下文函数均已停用。
"""血缘记录、阶段上下文与阶段血缘视图。

约定（DATA_SCHEMA §4.6 / DEVELOPMENT_CONTRACT §3）：
- 本地快照 ``lineage.json`` 保留中间算子的临时 ``outputFile`` 绝对路径，便于调试与阶段内传递；
- 对外阶段末血缘移除当前阶段各记录的临时 ``outputFile``，但保留算子身份、版本、时间、
  状态、配置、统计与逻辑产物名（``artifacts``）；
- ``stage_context.json`` 同时保存 ``stage_lineage``（当前阶段）与 ``prior_lineage``（历史阶段）；
- 历史阶段血缘由工程侧随包回传（``lineage_history.json``），本算子只追加不重写。
"""
from __future__ import annotations

import copy
import json
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional

_TZ = timezone(timedelta(hours=8))

#: 血缘记录必须保留的字段（DATA_SCHEMA §4.6）
REQUIRED_FIELDS = ("operator", "operator_version", "uniqueId", "inputFile", "status")

STAGE_CONTEXT_NAME = "stage_context.json"
LINEAGE_NAME = "lineage.json"
LINEAGE_HISTORY_NAME = "lineage_history.json"


def now_iso() -> str:
    return datetime.now(_TZ).isoformat(timespec="seconds")


def make_lineage(operator: str, version: str, unique_id: str, stage_id: str,
                 run_id: str, input_file: Optional[str], output_file: Optional[str],
                 status: str, started_at: str, finished_at: str,
                 config: Dict[str, Any], statistics: Dict[str, Any],
                 artifacts: Optional[List[Dict[str, str]]] = None) -> Dict[str, Any]:
    """构造一条血缘记录。"""
    record: Dict[str, Any] = {
        "operator": operator,
        "operator_version": version,
        "uniqueId": unique_id,
        "stageId": stage_id,
        "runId": run_id,
        "inputFile": input_file,
        "status": status,
        "startedAt": started_at,
        "finishedAt": finished_at,
        "config": config,
        "statistics": statistics,
    }
    if output_file is not None:
        record["outputFile"] = output_file
    if artifacts is not None:
        record["artifacts"] = artifacts
    return record


def load_lineage(path) -> List[Dict[str, Any]]:
    """读取血缘快照（JSON 数组）。缺失或损坏时返回空数组。"""
    target = Path(path)
    if not target.exists():
        return []
    try:
        payload = json.loads(target.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError):
        return []
    if isinstance(payload, dict):
        return [payload]
    if isinstance(payload, list):
        return [item for item in payload if isinstance(item, dict)]
    return []


def append_lineage(path, record: Dict[str, Any]) -> List[Dict[str, Any]]:
    """按顺序追加一条血缘并写回快照，返回完整数组。"""
    records = load_lineage(path)
    records.append(record)
    target = Path(path)
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(json.dumps(records, ensure_ascii=False, indent=2), encoding="utf-8")
    return records


def load_prior_lineage(search_dirs) -> List[Dict[str, Any]]:
    """从输入目录读取已归档的历史阶段血缘（``lineage_history.json``）。"""
    candidates = search_dirs if isinstance(search_dirs, (list, tuple)) else [search_dirs]
    for directory in candidates:
        path = Path(directory) / LINEAGE_HISTORY_NAME
        if path.exists() and path.is_file():
            return load_lineage(path)
    return []


def to_stage_lineage(records: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    """对外阶段末血缘：移除当前阶段各记录的临时 ``outputFile`` 绝对路径。"""
    result: List[Dict[str, Any]] = []
    for record in records:
        item = copy.deepcopy(record)
        item.pop("outputFile", None)
        # artifacts 只保留包内相对名，避免泄漏其他容器的绝对路径
        artifacts = item.get("artifacts")
        if isinstance(artifacts, list):
            item["artifacts"] = [
                {**artifact, "path": Path(str(artifact.get("path", ""))).name}
                for artifact in artifacts if isinstance(artifact, dict)
            ]
        result.append(item)
    return result


def build_stage_context(stage_id: str, unique_id: str, run_id: str, entrypoint: str,
                        stage_lineage: List[Dict[str, Any]],
                        prior_lineage: Optional[List[Dict[str, Any]]] = None) -> Dict[str, Any]:
    return {
        "stageId": stage_id,
        "uniqueId": unique_id,
        "runId": run_id,
        "entrypoint": entrypoint,
        "stage_lineage": stage_lineage,
        "prior_lineage": list(prior_lineage or []),
    }
