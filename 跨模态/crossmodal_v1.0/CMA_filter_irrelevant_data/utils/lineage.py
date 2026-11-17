# [v4.2] 血缘/阶段归档/运输输出已停用：本模块不再被 app.py / inference.py 调用，代码保留存档，不再维护。
"""血缘记录：追加写入任务目录 lineage.jsonl，支持跨阶段拼接。"""
import json
from datetime import datetime, timezone, timedelta
from pathlib import Path

_TZ = timezone(timedelta(hours=8))


def now_iso() -> str:
    return datetime.now(_TZ).isoformat(timespec="seconds")


def build_lineage(operator: str, version: str, unique_id: str,
                  input_file: str, output_file: str, status: str,
                  started_at: str, finished_at: str,
                  config: dict, statistics: dict) -> dict:
    return {
        "operator": operator,
        "operator_version": version,
        "uniqueId": unique_id,
        "inputFile": input_file,
        "outputFile": output_file,
        "status": status,
        "startedAt": started_at,
        "finishedAt": finished_at,
        "config": config,
        "statistics": statistics,
    }


def append_lineage(task: Path, record: dict) -> Path:
    path = task / "lineage.jsonl"
    with open(path, "a", encoding="utf-8") as f:
        f.write(json.dumps(record, ensure_ascii=False) + "\n")
    return path
