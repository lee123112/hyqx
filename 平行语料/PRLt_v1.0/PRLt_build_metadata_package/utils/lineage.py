# [无血缘改造 2026-10-19] 本模块已停用，处理器不再调用；保留便于回溯历史实现。
"""HTTP 血缘 JSONL 追加工具。

每一行都是一个独立的对象，遵循 HTTP 版示例的五类记录模型。算子只写
``stage_segment``，工程侧随后补充 file/oss_supplement/outcome/deliver_supplement。
"""
import json
import os
from datetime import datetime, timedelta, timezone
from pathlib import Path

_TZ = timezone(timedelta(hours=8))


def now_iso() -> str:
    return datetime.now(_TZ).isoformat(timespec="seconds")


def append_lineage(lineage_path, record: dict) -> None:
    p = Path(lineage_path)
    p.parent.mkdir(parents=True, exist_ok=True)
    with p.open("a", encoding="utf-8", newline="\n") as stream:
        stream.write(json.dumps(record, ensure_ascii=False, separators=(",", ":")) + "\n")


def write_lineage_jsonl_atomic(lineage_path, records) -> None:
    p = Path(lineage_path)
    p.parent.mkdir(parents=True, exist_ok=True)
    tmp = p.with_name(p.name + ".tmp")
    with tmp.open("w", encoding="utf-8", newline="\n") as stream:
        for record in records:
            stream.write(json.dumps(record, ensure_ascii=False, separators=(",", ":")) + "\n")
    os.replace(tmp, p)


def make_lineage(operator: str, version: str, unique_id: str,
                 input_file: str, output_file: str, status: str,
                 started_at: str, finished_at: str,
                 config: dict, statistics: dict) -> dict:
    rows_in = int(statistics.get("input_records", statistics.get("input_records", 0)) or 0)
    rows_out = int(statistics.get("delivery_records", statistics.get("output_records", 0)) or 0)
    action = "PROCESS" if status not in {"failed", "rejected"} else "REJECT"
    process = {
        "action": action,
        "detail": "处理完成" if action == "PROCESS" else "处理未通过",
        "rowsIn": rows_in,
        "rowsOut": rows_out,
    }
    if status in {"failed", "rejected"}:
        process["terminal"] = True
    return {
        "type": "stage_segment",
        "writtenBy": "operator",
        "stage": "18",
        "name": "数据集交付",
        "operators": [{
            "seq": 18,
            "tool": {
                "id": "PRLt_build_metadata_package",
                "name": operator,
                "type": "规则型",
                "kind": "algorithm",
                "endpoint": "POST /predict",
                "version": version,
            },
            "request": {"uniqueId": unique_id, "input": str(input_file)},
            "process": process,
            "output": str(output_file) if output_file else None,
            "ts": started_at,
            "costMs": 0,
        }],
    }
