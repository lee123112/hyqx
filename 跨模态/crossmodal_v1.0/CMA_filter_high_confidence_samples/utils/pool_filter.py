"""★业务规则层：表1 #19「筛选高置信样本入池」四道必过项判定。

表1 #19 冻结口径（逐字，不得改写）：
  默认开启；否（必须）；类型 = 规则型；优先级 = P0；模型 = ``--``
  输入：各评分路线、#16 整体结论、授权、时序结论、开关
  处理边界：**评分开启且通过、整体自动/人工对齐、授权可训练、有时间戳样本时序通过，
            四项同时满足入候选池**
  输出和下游：候选池；样本隔离清单；候选池交 #20、#22 和 #24；失败交 #21
  质量处置：必过项失败【样本-语义不通】→ 隔离 → 样本隔离库；
            通过【样本-评分：入池】→ 评分分层（不拦）→ 留样本池
  研发测边界：不得用加权分把已隔离样本加回；**局部标注不作为入池硬条件**

判定顺序（结构化 → 逐门）：
  1. 结构性检查（标识、媒体引用合法性）→ 原始隔离区
  2. 四道必过项中出现**明确失败** → 样本隔离库（主因按 route→whole→license→temporal 取首个）
  3. 尊重上游隔离标记（#21/#17 incomplete/#18 isolated）→ 样本隔离库
  4. 四道必过项中存在**未定结论** → 待人工（流程状态，不隔离）
  5. 四项同时满足 → 入候选池

本模块**不计算任何合成分数**，只输出「哪道门、结论是什么、依据哪个字段」。
"""
from __future__ import annotations

import copy
import json
from typing import Any, Dict, List, Optional, Sequence

from . import upstream

__all__ = [
    "ACTION_ADMIT",
    "ACTION_ISOLATE",
    "ACTION_KEEP",
    "ACTION_ORIGIN",
    "ACTION_PENDING",
    "DEST_ORIGIN",
    "DEST_POOL",
    "DEST_QUARANTINE",
    "DEST_WAIT",
    "LABEL_ADMITTED",
    "LABEL_ISOLATED",
    "LABEL_SENSITIVE",
    "LOG_NAME",
    "MAIN_NAME",
    "OPERATOR",
    "OPERATOR_NUMBER",
    "OPERATOR_VERSION",
    "ORIGIN_LABELS",
    "PENDING_LABELS",
    "PENDING_REASON_BY_GATE",
    "QUARANTINE_NAME",
    "REASON_BY_GATE",
    "REASON_ALL_PASSED",
    "REASON_DUPLICATE_IDENTIFIER",
    "REASON_ENCODING",
    "REASON_INSUFFICIENT_ROUTES",
    "REASON_JSON_PARSE",
    "REASON_LICENSE_MISSING",
    "REASON_LICENSE_NOT_TRAINABLE",
    "REASON_MISSING_IDENTIFIER",
    "REASON_NOT_OBJECT",
    "REASON_NO_ROUTE",
    "REASON_ROUTE_FAILED",
    "REASON_ROUTE_PENDING",
    "REASON_TEMPORAL_FAILED",
    "REASON_TEMPORAL_MISSING",
    "REASON_TEMPORAL_PENDING",
    "REASON_UNSAFE_MEDIA_PATH",
    "REASON_UPSTREAM_ISOLATED",
    "REASON_WHOLE_MISSING",
    "REASON_WHOLE_NOT_ALIGNED",
    "REASON_WHOLE_NOT_REQUIRED_TYPE",
    "REASON_WHOLE_PENDING",
    "REASON_WHOLE_UNCERTAIN",
    "REJECTS_NAME",
    "SOURCE_REFERENCE",
    "STAGE_ID",
    "GATE_ORDER",
    "evaluate_record",
    "make_ledger_entry",
    "resolve_record",
]

OPERATOR = "high_confidence_pool"
OPERATOR_NUMBER = 19
OPERATOR_VERSION = "v1.0.0"
STAGE_ID = "S3"
SOURCE_REFERENCE = "operator_19"

MAIN_NAME = "19_candidate_pool.jsonl"
QUARANTINE_NAME = "19_sample_quarantine.jsonl"
PENDING_NAME = "19_pending.jsonl"
REJECTS_NAME = "19_rejects.jsonl"
LOG_NAME = "19_run.log"

ACTION_ADMIT = "admit"
ACTION_ISOLATE = "isolate"
ACTION_PENDING = "pending"
ACTION_ORIGIN = "origin"
ACTION_KEEP = "keep"

STATUS_ADMITTED = "admitted"
STATUS_ISOLATED = "isolated"
STATUS_PENDING = "pending"
STATUS_ORIGIN = "origin"

# [v4.2] status 双字段（§2.10）：输出记录 int status（判定型：1=入池/待人工，
# 0=不入池/原始隔离）+ 原字符串值迁入 process_status，判定逻辑仍用字符串常量。
_STATUS_AS_INT = {
    STATUS_ADMITTED: 1,
    STATUS_ISOLATED: 0,
    STATUS_PENDING: 1,
    STATUS_ORIGIN: 0,
}


def status_int(value: Optional[str]) -> int:
    """记录级字符串状态 → [v4.2] int status；未登记的一律按不入池 0。"""
    return _STATUS_AS_INT.get(value, 0)

DEST_POOL = "candidate_pool"
DEST_QUARANTINE = "sample_quarantine"
DEST_WAIT = "pipeline_wait"
DEST_ORIGIN = "origin_quarantine"

LABEL_ADMITTED = "样本-评分：入池"
LABEL_ISOLATED = "样本-语义不通"
LABEL_SENSITIVE = "样本-敏感"

GATE_ORDER: Sequence[str] = ("route", "whole", "license", "temporal")

REASON_ALL_PASSED = "all_gates_passed"
REASON_ROUTE_FAILED = "route_failed"
REASON_ROUTE_PENDING = "route_pending"
REASON_NO_ROUTE = "no_scoring_route_enabled"
REASON_INSUFFICIENT_ROUTES = "insufficient_enabled_routes"
REASON_WHOLE_NOT_ALIGNED = "whole_not_aligned"
REASON_WHOLE_UNCERTAIN = "whole_uncertain"
REASON_WHOLE_PENDING = "whole_pending_manual_review"
REASON_WHOLE_MISSING = "whole_alignment_missing"
REASON_WHOLE_NOT_REQUIRED_TYPE = "whole_not_required_type"
REASON_LICENSE_NOT_TRAINABLE = "license_not_trainable"
REASON_LICENSE_MISSING = "license_missing"
REASON_TEMPORAL_FAILED = "temporal_failed"
REASON_TEMPORAL_MISSING = "temporal_missing"
REASON_TEMPORAL_PENDING = "temporal_pending"
REASON_UPSTREAM_ISOLATED = "upstream_isolated"
REASON_UNSAFE_MEDIA_PATH = "unsupported_media_path"
REASON_MISSING_IDENTIFIER = "missing_field:identifier"
REASON_DUPLICATE_IDENTIFIER = "duplicate_identifier"
REASON_JSON_PARSE = "json_parse_error"
REASON_NOT_OBJECT = "record_not_object"
REASON_ENCODING = "encoding_error"

#: 主因：某道门**明确失败**时的原因码
REASON_BY_GATE = {
    "route": REASON_ROUTE_FAILED,
    "whole": REASON_WHOLE_NOT_ALIGNED,
    "license": REASON_LICENSE_NOT_TRAINABLE,
    "temporal": REASON_TEMPORAL_FAILED,
}

#: 主因：某道门**结论未定**时的原因码（流程状态，不隔离）
PENDING_REASON_BY_GATE = {
    "route": REASON_NO_ROUTE,
    "whole": REASON_WHOLE_MISSING,
    "license": REASON_LICENSE_MISSING,
    "temporal": REASON_TEMPORAL_MISSING,
}

PENDING_LABELS = {
    REASON_NO_ROUTE: "流程-无评分路线",
    REASON_INSUFFICIENT_ROUTES: "流程-无评分路线",
    REASON_ROUTE_PENDING: "流程-待人工确认",
    REASON_WHOLE_MISSING: "流程-待人工对齐",
    REASON_WHOLE_PENDING: "流程-待人工对齐",
    REASON_WHOLE_UNCERTAIN: "流程-待人工对齐",
    REASON_LICENSE_MISSING: "流程-待补授权",
    REASON_TEMPORAL_MISSING: "流程-待时序判定",
    REASON_TEMPORAL_PENDING: "流程-待时序判定",
}

ORIGIN_LABELS = {
    REASON_JSON_PARSE: "原始文件-无法解析",
    REASON_NOT_OBJECT: "原始文件-无法解析",
    REASON_ENCODING: "原始文件-编码异常",
    REASON_MISSING_IDENTIFIER: "原始文件-缺字段",
    REASON_DUPLICATE_IDENTIFIER: "原始文件-重复标识",
    REASON_UNSAFE_MEDIA_PATH: "原始文件-路径非法",
}

WHOLE_UNCERTAIN_DECISIONS = {"uncertain"}
WHOLE_PENDING_DECISIONS = {"pending_manual_review", "pending", "manual_pending",
                           "waiting_manual", "waiting_human"}
WHOLE_FAILED_DECISIONS = {"not_aligned", "misaligned", "rejected"}
TEMPORAL_PENDING_STATUSES = {"pending_calibration", "pending", "degraded",
                             "waiting_manual", "not_checked"}

_MAX_RECORD_BYTES = 32768


# --------------------------------------------------------------------------- #
# 记录解析
# --------------------------------------------------------------------------- #

def resolve_record(payload: Any) -> Dict[str, Any]:
    """把原始记录解析为判定所需的只读视图（不改动原对象）。"""
    if not isinstance(payload, dict):
        return {"identifier": None, "sample_id": None, "pair_id": None,
                "record": {}, "raw": payload, "media_refs": [],
                "unsafe_media_refs": []}
    flat = upstream.flatten_record(payload)
    sample_id, pair_id = upstream.resolve_identifier(payload)
    return {"identifier": pair_id or sample_id,
            "sample_id": sample_id,
            "pair_id": pair_id,
            "record": flat,
            "raw": payload,
            "media_refs": upstream.collect_media_refs(flat),
            "unsafe_media_refs": upstream.unsafe_media_refs(flat)}


# --------------------------------------------------------------------------- #
# 四道门
# --------------------------------------------------------------------------- #

def _temporal_route_pass(temporal: Dict[str, Any]) -> bool:
    """#15 结论是否可充当「评分路线通过」的兜底证据。

    只认**通过**：``passed`` 为失败时仍由 ``temporal`` 门以 ``temporal_failed``
    隔离，主因不应被改写成 ``route_failed``；``not_applicable``/``skipped``
    （英文窗口、无时间轴）不构成证据，保持「无评分路线」待人工。
    """
    if not temporal.get("present") or not temporal.get("applicable"):
        return False
    if temporal.get("passed") is False:
        return False
    status = temporal.get("status")
    if status and str(status).strip().lower() in ("failed", "isolated", "not_aligned"):
        return False
    if temporal.get("passed") is True:
        return True
    return bool(status) and str(status).strip().lower() == "passed"


def _gate_route(record: Dict[str, Any], config: Any) -> Dict[str, Any]:
    routes = upstream.read_routes(record)
    enabled = upstream.enabled_routes(routes)
    results = {route: {"enabled": routes[route]["enabled"],
                       "present": routes[route]["present"],
                       "status": routes[route]["status"],
                       "tier": routes[route]["tier"],
                       "passed": routes[route]["passed"]}
               for route in upstream.ROUTE_IDS}
    source = 'processing["12"|"13"|"14"]'

    # 时序窗口的 #14 判 not_applicable、#13 可能因转写不可靠判 not_applicable，
    # 于是三条评分路线都未生效。此时把 #15 的「时序通过」作为路线证据，
    # 避免「#15 已判通过、#16 已判对齐」的记录仍卡在 no_scoring_route_enabled。
    if not enabled and bool(getattr(config, "temporal_route_fallback", False)):
        temporal = upstream.read_temporal(record)
        if _temporal_route_pass(temporal):
            enabled = [upstream.TEMPORAL_ROUTE_ID]
            results[upstream.TEMPORAL_ROUTE_ID] = {
                "enabled": True, "present": True,
                "status": temporal["status"], "tier": None, "passed": True}
            source = ('processing["12"|"13"|"14"]（无生效路线时回退 '
                      'processing["15"] 时序通过）')

    if not enabled:
        passed, reason = None, REASON_NO_ROUTE
    elif len(enabled) < int(config.min_enabled_routes):
        passed, reason = None, REASON_INSUFFICIENT_ROUTES
    else:
        failures = [route for route in enabled if results[route]["passed"] is False]
        pending = [route for route in enabled if results[route]["passed"] is None]
        if failures:
            passed, reason = False, REASON_ROUTE_FAILED
        elif pending:
            passed, reason = None, REASON_ROUTE_PENDING
        else:
            passed, reason = True, None

    return {"required": True, "passed": passed, "reason": reason,
            "enabled_routes": list(enabled), "route_results": results,
            "source": source}


def _gate_whole(record: Dict[str, Any], config: Any) -> Dict[str, Any]:
    info = upstream.read_whole_alignment(record)
    decision = info["decision"]
    allowed = tuple(config.alignment_choices)

    if decision is None:
        passed, reason = None, REASON_WHOLE_MISSING
    elif decision in WHOLE_UNCERTAIN_DECISIONS:
        passed, reason = None, REASON_WHOLE_UNCERTAIN
    elif decision in WHOLE_PENDING_DECISIONS or decision == "not_applicable":
        passed, reason = None, REASON_WHOLE_PENDING
    elif decision in WHOLE_FAILED_DECISIONS:
        passed, reason = False, REASON_WHOLE_NOT_ALIGNED
    elif decision in allowed:
        passed, reason = True, None
    else:
        passed, reason = False, REASON_WHOLE_NOT_REQUIRED_TYPE

    return {"required": True, "passed": passed, "reason": reason,
            "decision": decision, "source": info["source"],
            "allowed_alignments": list(allowed),
            "human_veto_locked": bool(info["human_veto_locked"])}


def _gate_license(record: Dict[str, Any], config: Any) -> Dict[str, Any]:
    value, source = upstream.read_license(record)
    flag = upstream.is_trainable(value)
    if flag is None:
        passed, reason = None, REASON_LICENSE_MISSING
    elif flag:
        passed, reason = True, None
    else:
        passed, reason = False, REASON_LICENSE_NOT_TRAINABLE
    return {"required": True, "passed": passed, "reason": reason,
            "value": value, "source": source,
            "requirement": config.training_authorization}


def _gate_temporal(record: Dict[str, Any], config: Any) -> Dict[str, Any]:
    temporal = upstream.read_temporal(record)
    timeline = upstream.timeline_present(record)
    required = bool(config.require_temporal_if_present) and timeline
    base = {"required": required, "applicable": bool(timeline),
            "status": temporal["status"], "source": temporal["source"],
            "passed_flag": temporal["passed"],
            "reason_code": temporal["reason_code"],
            "threshold_sec": temporal["threshold_sec"],
            "max_delta_sec": temporal["max_delta_sec"]}

    if not required:
        base.update({"passed": True, "reason": None})
        return base

    status = temporal["status"]
    if not temporal["present"]:
        base.update({"passed": None, "reason": REASON_TEMPORAL_MISSING})
    elif temporal["passed"] is False or (status and status.lower() == "failed"):
        base.update({"passed": False, "reason": REASON_TEMPORAL_FAILED})
    elif temporal["passed"] is True or (status and status.lower() == "passed"):
        base.update({"passed": True, "reason": None})
    elif status and status.lower() in ("not_applicable", "skipped"):
        base.update({"passed": True, "reason": None, "applicable": False,
                     "required": False})
    elif status and status.lower() in TEMPORAL_PENDING_STATUSES:
        base.update({"passed": None, "reason": REASON_TEMPORAL_PENDING})
    else:
        base.update({"passed": None, "reason": REASON_TEMPORAL_PENDING})
    return base


# --------------------------------------------------------------------------- #
# 判定
# --------------------------------------------------------------------------- #

def _base_node(spec: Dict[str, Any], event_time: str) -> Dict[str, Any]:
    return {"operator": OPERATOR,
            "operator_number": OPERATOR_NUMBER,
            "operator_version": OPERATOR_VERSION,
            "stage_id": STAGE_ID,
            "source_reference": SOURCE_REFERENCE,
            "identifier": spec["identifier"],
            "sample_id": spec["sample_id"],
            "pair_id": spec["pair_id"],
            "rule_based": True,
            "model": None,
            "score_fields_produced": False,
            "local_annotation_hard_requirement": False,
            "human_verified": False,
            "review_required": True,
            "evaluated_at": event_time}


def _finish(node: Dict[str, Any], *, action: str, decision: str, status: str,
            reason: str, label: str, label_detail: Optional[str],
            destination: str, detail: str, gates: Dict[str, Any],
            evidence: Dict[str, Any]) -> Dict[str, Any]:
    # [v4.2] status 双字段：int status + 原字符串迁入 process_status
    node.update({"action": action, "decision": decision,
                 "status": status_int(status), "process_status": status,
                 "reason_code": reason, "label": label,
                 "label_detail": label_detail, "destination": destination,
                 "detail": detail, "gates": gates, "evidence": evidence})
    return node


def _flat_summary(node: Dict[str, Any]) -> Dict[str, Any]:
    gates = node.get("gates") or {}
    gates_passed = {name: gates.get(name, {}).get("passed") for name in GATE_ORDER}
    enabled_routes = (gates.get("route") or {}).get("enabled_routes") or []
    return {"operator_number": node["operator_number"],
            "operator_version": node["operator_version"],
            "stage_id": node["stage_id"],
            "identifier": node["identifier"],
            "status": node["status"],
            "process_status": node.get("process_status"),
            "decision": node["decision"],
            "action": node["action"],
            "destination": node["destination"],
            "reason_code": node["reason_code"],
            "label": node["label"],
            "label_detail": node["label_detail"],
            "gates_passed": gates_passed,
            "enabled_routes": list(enabled_routes),
            "local_annotation_hard_requirement": False,
            "score_fields_produced": False,
            "human_verified": False,
            "review_required": True,
            "source_reference": SOURCE_REFERENCE,
            "evaluated_at": node["evaluated_at"]}


def evaluate_record(spec: Dict[str, Any], *, config: Any, event_time: str,
                    media_missing: int = 0) -> Dict[str, Any]:
    """对单条记录执行表1 #19 的四道必过项判定。

    返回 ``{"action", "status", "node", "output", "reason", "label",
    "label_detail", "destination", "detail", "gates", "evidence"}``。
    ``output`` 仅在 ``action == "admit"`` 时有意义。
    """
    record: Dict[str, Any] = spec.get("record") or {}
    node = _base_node(spec, event_time)

    # --- 1. 结构性检查 --------------------------------------------------- #
    if not spec.get("identifier"):
        gates: Dict[str, Any] = {}
        evidence = {"media_refs": len(spec.get("media_refs") or []),
                    "media_missing": int(media_missing),
                    "gates_failed": [], "gates_pending": []}
        node = _finish(node, action=ACTION_ORIGIN, decision=ACTION_ORIGIN,
                       status=STATUS_ORIGIN, reason=REASON_MISSING_IDENTIFIER,
                       label=ORIGIN_LABELS[REASON_MISSING_IDENTIFIER],
                       label_detail=None, destination=DEST_ORIGIN,
                       detail="记录既没有 sample_id 也没有 pair_id，进入原始隔离区",
                       gates=gates, evidence=evidence)
        return _outcome(node, None)

    unsafe = spec.get("unsafe_media_refs") or []
    if unsafe:
        gates = {}
        evidence = {"media_refs": len(spec.get("media_refs") or []),
                    "unsafe_media_refs": list(unsafe),
                    "media_missing": int(media_missing),
                    "gates_failed": [], "gates_pending": []}
        node = _finish(node, action=ACTION_ORIGIN, decision=ACTION_ORIGIN,
                       status=STATUS_ORIGIN, reason=REASON_UNSAFE_MEDIA_PATH,
                       label=ORIGIN_LABELS[REASON_UNSAFE_MEDIA_PATH],
                       label_detail=None, destination=DEST_ORIGIN,
                       detail="媒体引用不可搬运（绝对路径/穿越/协议地址）：%s"
                              % ", ".join(unsafe[:3]),
                       gates=gates, evidence=evidence)
        return _outcome(node, None)

    # --- 2. 四道门 -------------------------------------------------------- #
    gates = {"route": _gate_route(record, config),
             "whole": _gate_whole(record, config),
             "license": _gate_license(record, config),
             "temporal": _gate_temporal(record, config)}

    failed = [name for name in GATE_ORDER if gates[name]["passed"] is False]
    pending = [name for name in GATE_ORDER if gates[name]["passed"] is None]

    isolation = upstream.read_upstream_isolation(record)
    local = upstream.read_local_annotation(record)
    evidence = {
        "enabled_routes": list(gates["route"]["enabled_routes"]),
        "route_results": {route: {"status": item["status"], "tier": item["tier"],
                                  "passed": item["passed"], "enabled": item["enabled"]}
                          for route, item in gates["route"]["route_results"].items()},
        "whole_decision": gates["whole"]["decision"],
        "whole_source": gates["whole"]["source"],
        "allowed_alignments": list(gates["whole"]["allowed_alignments"]),
        "license_value": gates["license"]["value"],
        "license_source": gates["license"]["source"],
        "temporal_status": gates["temporal"]["status"],
        "temporal_applicable": gates["temporal"]["applicable"],
        "temporal_source": gates["temporal"]["source"],
        "local_annotation": local,
        "upstream_isolated": bool(isolation["isolated"]),
        "upstream_isolation_source": isolation["source"],
        "media_refs": len(spec.get("media_refs") or []),
        "media_missing": int(media_missing),
        "gates_failed": list(failed),
        "gates_pending": list(pending),
    }

    # --- 3. 明确失败 → 样本隔离库 ---------------------------------------- #
    if failed:
        primary = failed[0]
        reason = gates[primary]["reason"] or REASON_BY_GATE[primary]
        label_detail = (LABEL_SENSITIVE
                        if reason == REASON_LICENSE_NOT_TRAINABLE else None)
        node = _finish(node, action=ACTION_ISOLATE, decision=ACTION_ISOLATE,
                       status=STATUS_ISOLATED, reason=reason,
                       label=LABEL_ISOLATED, label_detail=label_detail,
                       destination=DEST_QUARANTINE,
                       detail=_failure_detail(gates, failed),
                       gates=gates, evidence=evidence)
        return _outcome(node, None)

    # --- 4. 尊重上游隔离标记 → 样本隔离库 -------------------------------- #
    if isolation["isolated"]:
        node = _finish(node, action=ACTION_ISOLATE, decision=ACTION_ISOLATE,
                       status=STATUS_ISOLATED, reason=REASON_UPSTREAM_ISOLATED,
                       label=LABEL_ISOLATED, label_detail=None,
                       destination=DEST_QUARANTINE,
                       detail="上游已判为样本隔离（%s），不得用分数加回"
                              % (isolation["source"] or "unknown"),
                       gates=gates, evidence=evidence)
        return _outcome(node, None)

    # --- 5. 结论未定 → 待人工（流程状态，不隔离） ------------------------ #
    if pending:
        primary = pending[0]
        reason = gates[primary]["reason"] or PENDING_REASON_BY_GATE[primary]
        node = _finish(node, action=ACTION_PENDING, decision=ACTION_PENDING,
                       status=STATUS_PENDING, reason=reason,
                       label=PENDING_LABELS.get(reason, "流程-待人工确认"),
                       label_detail=None, destination=DEST_WAIT,
                       detail=_pending_detail(gates, pending),
                       gates=gates, evidence=evidence)
        return _outcome(node, None)

    # --- 6. 四项同时满足 → 候选池 ---------------------------------------- #
    node = _finish(node, action=ACTION_KEEP, decision=ACTION_ADMIT,
                   status=STATUS_ADMITTED, reason=REASON_ALL_PASSED,
                   label=LABEL_ADMITTED, label_detail=None,
                   destination=DEST_POOL,
                   detail="四项必过项同时满足：评分路线通过、整体对齐、授权可训练、时序满足",
                   gates=gates, evidence=evidence)
    output = _build_output(spec, node)
    return _outcome(node, output)


def _failure_detail(gates: Dict[str, Any], failed: List[str]) -> str:
    parts = []
    if "route" in failed:
        enabled = gates["route"]["enabled_routes"]
        broken = [route for route in enabled
                  if gates["route"]["route_results"][route]["passed"] is False]
        parts.append("评分路线未通过（%s）" % ", ".join(broken or enabled))
    if "whole" in failed:
        parts.append("整体结论非自动/人工对齐（%s）"
                     % (gates["whole"]["decision"] or "缺失"))
    if "license" in failed:
        parts.append("授权不可训练（%s）" % (gates["license"]["value"] or "缺失"))
    if "temporal" in failed:
        parts.append("时间轴时序不通过（%s）"
                     % (gates["temporal"]["reason_code"] or "超阈值"))
    return "必过项失败：" + "；".join(parts)


def _pending_detail(gates: Dict[str, Any], pending: List[str]) -> str:
    labels = {"route": "评分路线", "whole": "整体对齐结论", "license": "授权结论",
              "temporal": "时序结论"}
    return "必过项结论未定，留产线待人工：" + "、".join(labels[name] for name in pending)


def _outcome(node: Dict[str, Any], output: Optional[Dict[str, Any]]) -> Dict[str, Any]:
    summary = _flat_summary(node)
    if output is not None:
        output.setdefault("processing", {})
        output["processing"]["19"] = node
        output["operator_19"] = summary
    # action 使用判定动作（admit/isolate/pending/origin）；节点内的 action 表示
    # 对样本的处置（keep/isolate/waiting_manual/origin_quarantine），两者语义不同。
    return {"action": node["decision"], "status": node["status"], "node": node,
            "output": output, "summary": summary, "reason": node["reason_code"],
            "label": node["label"], "label_detail": node["label_detail"],
            "destination": node["destination"], "detail": node["detail"],
            "gates": node["gates"], "evidence": node["evidence"]}


def _build_output(spec: Dict[str, Any], node: Dict[str, Any]) -> Dict[str, Any]:
    raw = spec.get("raw")
    output = copy.deepcopy(raw) if isinstance(raw, dict) else {}
    return output


# --------------------------------------------------------------------------- #
# 台账条目
# --------------------------------------------------------------------------- #

def bound_payload(payload: Any, limit: int = _MAX_RECORD_BYTES) -> Any:
    """把原始记录裁剪到可写入隔离清单的大小（不破坏可诊断性）。"""
    if payload is None:
        return None
    try:
        text = json.dumps(payload, ensure_ascii=False)
    except (TypeError, ValueError):
        return {"_type": type(payload).__name__, "_repr": repr(payload)[:limit]}
    if len(text.encode("utf-8")) <= limit:
        return payload
    return {"_truncated": True, "_original_bytes": len(text.encode("utf-8")),
            "_preview": text[:limit]}


def make_ledger_entry(*, line: int, spec: Dict[str, Any], outcome: Dict[str, Any],
                      detail: str, detected_at: str,
                      include_record: bool = True) -> Dict[str, Any]:
    """构造隔离 / 待人工 / 原始隔离清单条目。"""
    node = outcome["node"]
    entry = {
        "line": int(line),
        "identifier": spec.get("identifier"),
        "sample_id": spec.get("sample_id"),
        "pair_id": spec.get("pair_id"),
        "operator": OPERATOR,
        "operator_number": OPERATOR_NUMBER,
        "operator_version": OPERATOR_VERSION,
        "stage_id": STAGE_ID,
        "source_reference": SOURCE_REFERENCE,
        "status": node["status"],
        "process_status": node.get("process_status"),
        "decision": node["decision"],
        "action": node["action"],
        "destination": node["destination"],
        "reason": node["reason_code"],
        "label": node["label"],
        "label_detail": node["label_detail"],
        "detail": detail or node["detail"],
        "gates": node["gates"],
        "evidence": node["evidence"],
        "local_annotation_hard_requirement": False,
        "score_fields_produced": False,
        "human_verified": False,
        "review_required": True,
        "detected_at": detected_at,
    }
    if include_record:
        entry["record"] = bound_payload(spec.get("raw"))
    return entry


def make_reject_entry(*, line: int, reason: str, detail: str, detected_at: str,
                      raw_text: Optional[str] = None,
                      identifier: Optional[str] = None) -> Dict[str, Any]:
    """构造原始隔离区条目（坏行、非对象、缺字段、重复标识、编码异常）。"""
    label = ORIGIN_LABELS.get(reason, "原始文件-无法解析")
    entry = {
        "line": int(line),
        "identifier": identifier,
        "sample_id": identifier,
        "pair_id": None,
        "operator": OPERATOR,
        "operator_number": OPERATOR_NUMBER,
        "operator_version": OPERATOR_VERSION,
        "stage_id": STAGE_ID,
        "source_reference": SOURCE_REFERENCE,
        # [v4.2] status 双字段：原始隔离（不入池）int status=0 + process_status
        "status": status_int(STATUS_ORIGIN),
        "process_status": STATUS_ORIGIN,
        "decision": ACTION_ORIGIN,
        "action": "origin_quarantine",
        "destination": DEST_ORIGIN,
        "reason": reason,
        "label": label,
        "label_detail": None,
        "detail": detail,
        "gates": {},
        "evidence": {"gates_failed": [], "gates_pending": []},
        "local_annotation_hard_requirement": False,
        "score_fields_produced": False,
        "human_verified": False,
        "review_required": True,
        "detected_at": detected_at,
    }
    if raw_text is not None:
        text = raw_text.replace("\r", " ").replace("\n", " ")
        if len(text) > 512:
            text = text[:512] + "…"
        entry["raw"] = text
    return entry
