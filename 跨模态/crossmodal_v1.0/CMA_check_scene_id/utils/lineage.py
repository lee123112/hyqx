# [v4.2] 血缘/阶段归档/运输输出已停用：本模块除 now_iso 时间工具外不再被 app.py / inference.py 调用，代码保留存档，不再维护。
"""血缘记录、阶段上下文与阶段血缘视图。

约定（共享契约 §8 与 demo/DEVELOPMENT_CONTRACT.md §3）：
- 本地快照 ``lineage.json`` 保留中间算子的临时 ``outputFile`` 绝对路径；
- 对外阶段末血缘移除当前阶段各记录的临时 ``outputFile``，但保留算子身份、版本、
  时间、状态、配置、统计与逻辑产物名（``artifacts``）；
- ``stage_context.json`` 同时保存 ``stage_lineage``（当前阶段）与 ``prior_lineage``
  （历史阶段），阶段内链式调用据此连续累积同一 ``uniqueId`` 的阶段血缘；
- 历史阶段血缘由工程侧随包回传（``lineage_history.json``），本算子只追加不重写。

算子 #02 属表1 环节「输入核验」，demo 阶段标识固定为 ``S1``：阶段内后续算子从本次
``stage_context.json`` 继续追加；阶段末由工程侧用 ``isLastOperator=true`` 决定。
"""
from __future__ import annotations

import copy
import json
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional

_TZ = timezone(timedelta(hours=8))

#: 血缘记录必须保留的字段（共享契约 §8）
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
                 artifacts: Optional[List[Dict[str, str]]] = None,
                 formal_operator_number: Optional[int] = None) -> Dict[str, Any]:
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
        "model": None,
    }
    if formal_operator_number is not None:
        record["formalOperatorNumber"] = int(formal_operator_number)
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


def append_lineage(path, record: Dict[str, Any],
                   previous: Optional[List[Dict[str, Any]]] = None) -> List[Dict[str, Any]]:
    """按顺序追加一条血缘并写回本地快照，返回完整数组。"""
    records = list(previous) if previous else load_lineage(path)
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


def load_prior_stage_lineage(search_dirs) -> List[Dict[str, Any]]:
    """阶段内链式调用：从上一运行目录的 ``stage_context.json`` 取已累积的阶段血缘。

    正式接口不返回可下载路径，阶段内后续算子拿到的 ``inputFile`` 就是上一中间响应里的
    临时 ``outputFile``；其所在运行目录内的 ``stage_context.json`` 即是本阶段的累积血缘。
    """
    candidates = search_dirs if isinstance(search_dirs, (list, tuple)) else [search_dirs]
    for directory in candidates:
        path = Path(directory) / STAGE_CONTEXT_NAME
        if not path.is_file():
            continue
        try:
            payload = json.loads(path.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError):
            continue
        if not isinstance(payload, dict):
            continue
        stage = payload.get("stage_lineage")
        if isinstance(stage, list):
            return [item for item in stage if isinstance(item, dict)]
    return []


def to_stage_lineage(records: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    """对外阶段末血缘：移除当前阶段各记录的临时 ``outputFile`` 绝对路径。"""
    result: List[Dict[str, Any]] = []
    for record in records:
        item = copy.deepcopy(record)
        item.pop("outputFile", None)
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
