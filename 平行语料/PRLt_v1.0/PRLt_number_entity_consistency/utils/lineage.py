# [无血缘改造 2026-10-19] 本模块已停用，处理器不再调用；保留便于回溯历史实现。
"""血缘追加与序列化。血缘文件为 JSON 数组，按执行顺序追加。"""
import json
from datetime import datetime, timedelta, timezone
from pathlib import Path

_TZ = timezone(timedelta(hours=8))


def now_iso() -> str:
    return datetime.now(_TZ).isoformat(timespec="seconds")


def append_lineage(lineage_path, record: dict) -> None:
    p = Path(lineage_path)
    if p.exists():
        try:
            data = json.loads(p.read_text(encoding="utf-8"))
            if not isinstance(data, list):
                data = [data]
        except json.JSONDecodeError:
            data = []
    else:
        data = []
    data.append(record)
    p.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")


def make_lineage(operator: str, version: str, unique_id: str,
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
