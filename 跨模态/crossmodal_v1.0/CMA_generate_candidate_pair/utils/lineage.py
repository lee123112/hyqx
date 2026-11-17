# [v4.2] 血缘/阶段归档/运输输出已停用：本模块不再被 app.py / inference.py 调用（仅 now_iso 时间工具
# 与 snapshot 通用落盘仍被引用），血缘构建/追加/拆分代码保留存档，不再维护。
"""血缘记录：构建节点、追加任务历史、拆分阶段/跨阶段血缘、输出阶段快照。"""
from __future__ import annotations

import copy
import json
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Dict, List, Optional, Sequence, Tuple

_TZ = timezone(timedelta(hours=8))
HISTORY_NAME = "lineage.jsonl"

# 中间节点必须保留的字段；阶段末对外可移除临时 outputFile
REQUIRED_FIELDS = ("operator", "operator_version", "uniqueId", "inputFile", "status",
                   "startedAt", "finishedAt", "config", "statistics")


def now_iso() -> str:
    return datetime.now(_TZ).isoformat(timespec="seconds")


def build_lineage(operator: str, version: str, unique_id: str, stage_id: str,
                  input_file: str, output_file: Optional[str], status: str,
                  started_at: str, finished_at: str, config: dict, statistics: dict,
                  run_id: Optional[str] = None, model: Optional[dict] = None,
                  artifacts: Optional[Sequence[str]] = None) -> dict:
    return {
        "operator": operator,
        "operator_version": version,
        "uniqueId": unique_id,
        "stageId": stage_id,
        "inputFile": input_file,
        "outputFile": output_file,
        "status": status,
        "startedAt": started_at,
        "finishedAt": finished_at,
        "config": config,
        "statistics": statistics,
        "runId": run_id,
        "model": model,
        "artifacts": list(artifacts or []),
    }


def history_path(task: Path) -> Path:
    return Path(task) / HISTORY_NAME


def append_lineage(task: Path, record: dict) -> Path:
    path = history_path(task)
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(str(path), "a", encoding="utf-8") as handle:
        handle.write(json.dumps(record, ensure_ascii=False) + "\n")
    return path


def load_lineage_history(task: Path) -> List[dict]:
    path = history_path(task)
    if not path.is_file():
        return []
    history = []
    with open(str(path), "r", encoding="utf-8") as handle:
        for line in handle:
            line = line.strip()
            if not line:
                continue
            try:
                item = json.loads(line)
            except json.JSONDecodeError:
                continue
            if isinstance(item, dict):
                history.append(item)
    return history


def _key(node: dict) -> Tuple:
    return (node.get("stageId"), node.get("uniqueId"), node.get("runId"),
            node.get("operator"), node.get("startedAt"))


def partition_lineage(history: Sequence[dict], stage_id: str,
                      unique_id: str) -> Tuple[List[dict], List[dict]]:
    """按 stageId+uniqueId 拆分当前阶段与跨阶段历史，并按稳定键去重。"""
    stage: List[dict] = []
    prior: List[dict] = []
    seen = set()
    for node in history:
        if node.get("stageId") == stage_id and node.get("uniqueId") == unique_id:
            key = _key(node)
            if key in seen:
                continue
            seen.add(key)
            stage.append(node)
        else:
            prior.append(node)
    return stage, prior


def public_node(node: dict) -> dict:
    """阶段末对外血缘：移除本节点临时 outputFile，保留可追溯信息。"""
    visible = copy.deepcopy(node)
    visible.pop("outputFile", None)
    return visible


def snapshot(run_dir: Path, name: str, payload: object) -> Path:
    path = Path(run_dir) / name
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    return path
