# [v4.2] 血缘/阶段归档/运输输出已停用：本模块不再被 app.py / inference.py 调用，代码保留存档，不再维护。
"""血缘与配置快照。

按 `demo/DEVELOPMENT_CONTRACT.md` 的输出目录约定：
- ``lineage.json``：本阶段血缘**数组**（上游已提供则读入并按顺序追加，不能只存当前一条）；
- ``stage_context.json``：包含 ``stageId`` / ``uniqueId`` / ``entrypoint`` /
  ``stage_lineage`` / ``prior_lineage``；
- 中间节点保留临时 ``outputFile``（便于阶段内传递与调试），阶段末对外剔除。
"""
from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional, Tuple

RUN_STATUSES = ("success", "partial", "degraded", "failed")

LINEAGE_NAME = "lineage.json"
STAGE_CONTEXT_NAME = "stage_context.json"
LINEAGE_HISTORY_NAME = "lineage_history.json"
LOG_NAME = "run.log"
STAGE_CONTEXT_FORMAT = "crossmodal-stage-context-v1"

#: 血缘记录必须保留的字段
REQUIRED_FIELDS = ("operator", "operator_version", "uniqueId", "inputFile",
                   "status", "stageId", "startedAt", "finishedAt", "config",
                   "statistics")

#: 这些键属于部署/模型口径，请求侧不可改写（快照中单独分组以便审计）
DEPLOYMENT_KEYS = ("operator", "stage_id", "detector_backend", "model_name", "detector")


def now_iso() -> str:
    return datetime.now(timezone.utc).astimezone().isoformat(timespec="seconds")


def flatten_override_keys(overrides: Optional[Dict[str, Any]]) -> List[str]:
    """把请求覆盖参数摊平成 ``a`` / ``detector.conf_threshold`` 形式并排序。"""
    if not overrides:
        return []
    keys: List[str] = []
    for key, value in overrides.items():
        if isinstance(value, dict):
            for inner in value:
                keys.append("%s.%s" % (key, inner))
        else:
            keys.append(str(key))
    return sorted(keys)


def config_snapshot(config, overrides: Optional[Dict[str, Any]] = None,
                    *, config_path: Optional[str] = None) -> Dict[str, Any]:
    """记录完整生效配置：部署分组 + 业务分组 + 请求覆盖键清单。"""
    dumped = config.model_dump(mode="json")
    deployment = {key: dumped.get(key) for key in DEPLOYMENT_KEYS}
    return {
        "configFile": str(config_path) if config_path else None,
        "deployment": deployment,
        "business": dumped,
        "overridden_keys": flatten_override_keys(overrides),
        "capturedAt": now_iso(),
    }


def build_node(*, operator: str, operator_version: str, formal_operator_number: int,
               stage_id: str, unique_id: str, run_id: str, operator_dir: Any,
               entrypoint: str, input_file: Optional[str], output_file: str,
               status: str, started_at: str, finished_at: str,
               statistics: Dict[str, Any], artifacts: Iterable[Dict[str, Any]],
               model: Optional[Dict[str, Any]], config: Dict[str, Any],
               reason: Optional[str] = None,
               run_dir: Optional[Any] = None,
               task_dir: Optional[Any] = None,
               labels: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
    """构造一个血缘节点；``outputFile`` 为本次运行的临时产物路径。"""
    if status not in RUN_STATUSES:
        raise ValueError("unknown run status: %r" % status)
    return {
        "operator": operator,
        "operator_name_zh": (labels or {}).get("operator_name_zh"),
        "operator_version": operator_version,
        "formalOperatorNumber": formal_operator_number,
        "stageId": stage_id,
        "uniqueId": unique_id,
        "runId": run_id,
        "operatorDir": str(Path(operator_dir)),
        "taskDir": str(Path(task_dir)) if task_dir else None,
        "runDir": str(Path(run_dir)) if run_dir else None,
        "entrypoint": entrypoint,
        "inputFile": input_file,
        "outputFile": output_file,
        "status": status,
        "reason": reason,
        "startedAt": started_at,
        "finishedAt": finished_at,
        "config": config,
        "statistics": statistics,
        "artifacts": list(artifacts),
        "model": model,
        "label": (labels or {}).get("stage_label"),
        "sourceReference": (labels or {}).get("source_reference"),
    }


def public_node(node: Dict[str, Any]) -> Dict[str, Any]:
    """对外形态：剔除容器/任务内局部路径 ``outputFile``。"""
    return {key: value for key, value in dict(node).items() if key != "outputFile"}


def intermediate_node(node: Dict[str, Any]) -> Dict[str, Any]:
    """阶段中间形态：只保留当前节点，并确保带临时 ``outputFile``。"""
    snapshot = dict(node)
    if not snapshot.get("outputFile"):
        raise ValueError("intermediate lineage node requires a temporary outputFile")
    return snapshot


def stage_nodes(nodes: Iterable[Dict[str, Any]]) -> List[Dict[str, Any]]:
    """阶段末形态：返回完整阶段节点数组，剔除临时 ``outputFile``。"""
    return [public_node(node) for node in nodes]


# --------------------------------------------------------------------------- #
# 上下游血缘读写
# --------------------------------------------------------------------------- #

def load_lineage(path) -> List[Dict[str, Any]]:
    """读取血缘文件；既接受单对象也接受数组，缺失/损坏时返回空数组。"""
    target = Path(str(path))
    if not target.is_file():
        return []
    try:
        payload = json.loads(target.read_text(encoding="utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError, OSError):
        return []
    if isinstance(payload, dict):
        return [payload]
    if isinstance(payload, list):
        return [item for item in payload if isinstance(item, dict)]
    return []


def load_prior_lineage(search_dirs) -> List[Dict[str, Any]]:
    """读取已归档的历史阶段血缘（``lineage_history.json``），只追加不重写。"""
    for directory in _iter_dirs(search_dirs):
        rows = load_lineage(Path(directory) / LINEAGE_HISTORY_NAME)
        if rows:
            return rows
    return []


def load_stage_lineage(search_dirs, stage_id: str,
                       unique_id: str) -> List[Dict[str, Any]]:
    """读取上游同阶段、同 uniqueId 的已有节点，用于按顺序追加。"""
    for directory in _iter_dirs(search_dirs):
        rows = load_lineage(Path(directory) / LINEAGE_NAME)
        if not rows:
            continue
        stage, _prior = partition_lineage(rows, stage_id, unique_id)
        if stage:
            return stage
    return []


def _iter_dirs(search_dirs) -> List[Path]:
    if isinstance(search_dirs, (list, tuple)):
        candidates = list(search_dirs)
    else:
        candidates = [search_dirs]
    seen: List[Path] = []
    for item in candidates:
        if item is None:
            continue
        path = Path(str(item))
        if path not in seen:
            seen.append(path)
    return seen


def partition_lineage(rows: Iterable[Dict[str, Any]], stage_id: str,
                      unique_id: str) -> Tuple[List[Dict[str, Any]], List[Dict[str, Any]]]:
    """按阶段与任务拆分：(当前阶段节点, 其他阶段节点)。"""
    stage: List[Dict[str, Any]] = []
    prior: List[Dict[str, Any]] = []
    for row in rows:
        if not isinstance(row, dict):
            continue
        if row.get("stageId") == stage_id and row.get("uniqueId") == unique_id:
            stage.append(row)
        else:
            prior.append(row)
    return stage, prior


def build_stage_context(*, stage_id: str, unique_id: str, run_id: str,
                        entrypoint: str, output_file: str, status: str,
                        stage_lineage: List[Dict[str, Any]],
                        prior_lineage: List[Dict[str, Any]],
                        statistics: Optional[Dict[str, Any]] = None,
                        operator: Optional[str] = None,
                        model: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
    """阶段上下文：交给阶段内后续算子继续追加。"""
    return {
        "format": STAGE_CONTEXT_FORMAT,
        "operator": operator,
        "stageId": stage_id,
        "uniqueId": unique_id,
        "runId": run_id,
        "entrypoint": entrypoint,
        "outputFile": output_file,
        "status": status,
        "stage_lineage": stage_nodes(stage_lineage),
        "prior_lineage": list(prior_lineage),
        "stageOperators": [node.get("operator") for node in stage_lineage
                           if node.get("operator")],
        "priorOperators": [node.get("operator") for node in prior_lineage
                           if node.get("operator")],
        "model": model,
        "statistics": statistics,
        "createdAt": now_iso(),
    }
