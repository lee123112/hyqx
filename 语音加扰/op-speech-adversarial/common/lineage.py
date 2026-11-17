# -*- coding: utf-8 -*-
"""MTY 血缘（lineage）构造与跨阶段拼接。

口径按《平行语料算子开发背景与规则》§2.4 与《语音加扰算子开发规范》§3.3：

- **中间算子**（``isLastOperator=false``）：``data.lineageFile`` 必须带当前算子的临时
  ``outputFile`` 路径，工程侧读它作为下一算子的 ``inputFile``。
- **阶段末算子**（``isLastOperator=true``）：``data.outputFile`` 返回阶段最终输出**内容**，
  ``data.lineageFile`` 返回本阶段**完整**血缘列表。
- 算子侧只在收到的既有血缘基础上**追加**当前记录，阶段打包由工程侧负责。
"""

from __future__ import annotations

import datetime as _dt
from pathlib import Path
from typing import Any, Dict, List, Optional

from . import iofs

RECORD_KEYS = (
    "operator",
    "operator_version",
    "uniqueId",
    "inputFile",
    "outputFile",
    "status",
    "startedAt",
    "finishedAt",
    "config",
    "statistics",
)


def now_iso() -> str:
    """带时区的 ISO8601（+08:00）。"""
    return _dt.datetime.now().astimezone().isoformat(timespec="seconds")


def make_record(
    operator: str,
    operator_version: str,
    unique_id: str,
    input_file: Optional[str],
    output_file: Optional[str],
    status: str,
    started_at: Optional[str] = None,
    finished_at: Optional[str] = None,
    config: Optional[dict] = None,
    statistics: Optional[dict] = None,
    **extra: Any,
) -> Dict[str, Any]:
    """构造一条血缘记录。``status`` 取 ``success`` / ``partial`` / ``failed`` / ``skipped``。"""
    record: Dict[str, Any] = {
        "operator": operator,
        "operator_version": operator_version,
        "uniqueId": unique_id,
        "inputFile": input_file,
        "outputFile": output_file,
        "status": status,
        "startedAt": started_at or now_iso(),
        "finishedAt": finished_at or now_iso(),
        "config": config or {},
        "statistics": statistics or {},
    }
    for key, value in extra.items():
        if key not in record:
            record[key] = value
    return record


def load_lineage(path) -> List[dict]:
    """归一化上游传入的血缘，**同时接受内存对象与文件路径**。

    平台侧有两种传法，都必须支持：

    1. 把上一算子回的 ``data.lineageFile`` 原样放进 ``lineageFile`` 字段
       （阶段末是 ``list``、中间是 ``dict``）——此时拿到的就是内存对象；
    2. 传一个血缘 JSON 文件路径（早期对接方式）。

    空/不存在一律返回 ``[]``，不抛异常：血缘缺失不应该阻断业务计算。
    """
    if not path:
        return []
    # 传法 1：内存里的 dict / list（最常见）
    if isinstance(path, dict):
        if isinstance(path.get("records"), list):
            return [x for x in path["records"] if isinstance(x, dict)]
        return [path] if path else []
    if isinstance(path, (list, tuple)):
        return [x for x in path if isinstance(x, dict)]

    # 传法 2：文件路径
    p = Path(str(path))
    if not p.is_file():
        return []
    payload = iofs.read_json(p, default=[])
    if isinstance(payload, dict):
        if isinstance(payload.get("records"), list):
            return [x for x in payload["records"] if isinstance(x, dict)]
        return [payload] if payload else []
    if isinstance(payload, list):
        return [x for x in payload if isinstance(x, dict)]
    return []


def append_record(existing: List[dict], record: dict) -> List[dict]:
    """把当前记录追加到既有血缘上，返回新列表（不修改入参）。"""
    out = list(existing or [])
    out.append(record)
    return out


def build_payload(records: List[dict], is_last: bool) -> dict:
    """组装 ``data.lineageFile``。

    - **中间算子**（``is_last=False``）：只回**当前记录**（含 ``outputFile`` 临时路径），
      该路径就是下一个算子要传的 ``inputFile``。保持轻量，不重复整条链。
    - **阶段末算子**（``is_last=True``）：回**完整列表**（上游传入的 + 当前记录），
      由平台侧一次性拿到本阶段的全部血缘。

    「累积」由工程侧负责：下一算子把上一算子回的记录（或列表）作为 ``lineageFile``
    传进来，本模块在它基础上追加，不改名、不重排。
    """
    if is_last:
        return list(records)
    return dict(records[-1]) if records else {}


def summarize(records: List[dict]) -> dict:
    """阶段血缘摘要（写报告/日志用）。"""
    operators = [r.get("operator") for r in records if r.get("operator")]
    return {
        "stages": len(records),
        "operators": operators,
        "statuses": [r.get("status") for r in records],
        "first": records[0].get("startedAt") if records else None,
        "last": records[-1].get("finishedAt") if records else None,
    }
