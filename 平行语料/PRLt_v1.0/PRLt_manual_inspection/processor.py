"""算子20：人工抽检复核 —— 队列生成 + 人工结果安全回写。

处理边界（见 DEVELOPMENT_PLAN.md / TECHNICAL_DESIGN.md）：
- 输入是工程侧传入的待复核候选 JSONL（可引用 13 的 C 级、14–17 的低置信/冲突记录）；
- 按 `sample_ratio`、`min_sample_count`、`max_sample_count`、`random_seed` 与触发原因抽样，
  抽样结果可复算；触发命中的记录一定入队；
- 没有工程侧 review payload 时只生成队列；未完成的强制复核返回 `waiting_manual`，
  普通风险抽检默认不阻塞顶层状态，且绝不伪造审核完成；
- 人工结果必须是工程侧显式回写；`accepted/rejected/needs_revision/waiting_manual` 四态，
  缺 reviewer / 缺时间 / 缺原因 / 非法状态 / 目标不存在 → 进异常清单；
- 重复一致提交幂等（跨次运行由任务目录内 `review_state.json` 承载）；互相冲突的结果
  **不自动覆盖**，标记 `review_conflict`；
- 队列记录不复制完整原文，只保留证据摘要与 `source_ref`（输入文件 + 行号）供回查；
- 日志只记录数量与状态分布，不记录人工备注原文、凭据与完整堆栈；
- 输出只写入 `OUTPUT_DIR/<uniqueId>/`，严格 UTF-8，`.tmp` + `os.replace` 原子替换。
- 无血缘改造（2026-10-19）：不再生成/返回任何血缘文件；成功/部分成功/待人工分支返回前
  打包阶段产物 `artifacts/stage_20_<uniqueId>.zip` 并返回 `stageZip`/`storageUri`。
"""
import argparse
import json
import os
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

import yaml
from pydantic import ValidationError

from utils.artifacts import finalize_stage_artifacts
from utils.io import (JsonlWriter, iter_input_records, select_encoding, task_dir,
                      write_json_atomic)
from utils.logger import close_logger, get_logger, log_action
from utils.review import mask_reviewer, review_identity, truncate_reason, validate_review_entry
from utils.sampling import detect_trigger_reasons, lookup, select_for_review
from utils.schemas import OperatorConfig
from utils.timeutil import now_iso

# [无血缘改造 2026-10-19] 血缘输出已停用，保留原导入便于回溯；
# from utils.lineage import append_lineage, make_lineage, now_iso

BASE_DIR = Path(__file__).resolve().parent
OPERATOR_NAME = "manual_spot_check_review"
OPERATOR_ID = "20"
OPERATOR_VERSION = "v1.1.0"
DEFAULT_NAMES = {
    "queue_name": "20_review_queue.jsonl",
    "reviewed_name": "20_reviewed.jsonl",
    "reject_name": "20_review_rejects.jsonl",
    "state_file_name": "review_state.json",
}


# --------------------------------------------------------------------------- #
# 配置与统计骨架
# --------------------------------------------------------------------------- #

def load_config(config_path=None, overrides: dict = None) -> Tuple[OperatorConfig, dict]:
    cfg_path = Path(config_path) if config_path else BASE_DIR / "configs" / "config.yaml"
    raw = yaml.safe_load(cfg_path.read_text(encoding="utf-8")) or {}
    if not isinstance(raw, dict):
        raise ValueError("配置文件必须是对象")
    merged = {key: value for key, value in raw.items() if key != "operator"}
    if overrides:
        merged.update({key: value for key, value in overrides.items() if value is not None})
    cfg = OperatorConfig(**merged)
    snapshot = {"operator": raw.get("operator", {}), **merged}
    return cfg, snapshot


def _base_statistics(encoding: Optional[str] = None) -> Dict[str, Any]:
    return {
        "input_records": 0,
        "candidate_count": 0,
        "triggered_count": 0,
        "trigger_reason_counts": {},
        "sample_pool_count": 0,
        "random_sampled_count": 0,
        "sampled_count": 0,
        "queue_written_count": 0,
        "mandatory_review_count": 0,
        "audit_sample_count": 0,
        "blocking_pending_count": 0,
        "nonblocking_audit_pending_count": 0,
        "reviewed_count": 0,
        "accepted_count": 0,
        "rejected_count": 0,
        "needs_revision_count": 0,
        "reviewed_waiting_manual_count": 0,
        "waiting_manual_count": 0,
        "pending_manual_count": 0,
        "conflict_count": 0,
        "input_rejected_count": 0,
        "payload_entry_count": 0,
        "payload_rejected_count": 0,
        "id_consistency_errors": 0,
        "id_consistency_ok": True,
        "duplicate_review_ignored": 0,
        "reviewed_status_counts": {
            "accepted": 0, "rejected": 0, "needs_revision": 0, "waiting_manual": 0},
        "encoding": encoding,
    }


def _append_reason(statistics: Dict[str, Any], reason: str) -> None:
    key = "reason:%s" % reason
    statistics[key] = statistics.get(key, 0) + 1


# --------------------------------------------------------------------------- #
# 记录与证据
# --------------------------------------------------------------------------- #

def _record_id(record: Dict[str, Any], cfg) -> Optional[str]:
    for field in cfg.record_id_fields:
        if field not in record:
            continue
        value = record[field]
        if value is None or isinstance(value, bool):
            continue
        text = str(value).strip()
        if text:
            return text
    return None


def _evidence_summary(record: Dict[str, Any], cfg, reasons: List[str]) -> Dict[str, Any]:
    """只保留可解释的少量证据字段与文本长度，绝不复制完整原文。"""
    evidence: Dict[str, Any] = {"trigger_reasons": list(reasons)}
    metadata = record.get("metadata") if isinstance(record.get("metadata"), dict) else {}

    for field in cfg.evidence_fields:
        if field in cfg.text_fields:
            continue
        value = lookup(record, field)
        if value is None or isinstance(value, (dict, tuple, set)):
            if isinstance(value, tuple):
                value = list(value)
            else:
                continue
        if isinstance(value, bool) or isinstance(value, (int, float)):
            evidence[field] = value
        elif isinstance(value, str):
            evidence[field] = value[: cfg.max_evidence_chars]
        elif isinstance(value, list):
            items = []
            for item in value[: cfg.max_evidence_items]:
                if isinstance(item, bool) or isinstance(item, (int, float)):
                    items.append(item)
                elif isinstance(item, str):
                    items.append(item[: cfg.max_evidence_chars])
            evidence[field] = items

    for field in cfg.text_fields:
        value = record.get(field)
        if isinstance(value, str):
            evidence["%s_chars" % field] = len(value)

    language_pair = lookup(record, "language_pair")
    if isinstance(language_pair, str) and language_pair.strip():
        evidence["language_pair"] = language_pair.strip()
    elif isinstance(record.get("source_lang"), str) and isinstance(record.get("target_lang"), str):
        evidence["language_pair"] = "%s-%s" % (record["source_lang"], record["target_lang"])
    return evidence


def _scope_fields(record: Dict[str, Any]) -> Dict[str, Any]:
    """Resolve explicit granularity and parent scope without changing input."""
    metadata = record.get("metadata") if isinstance(record.get("metadata"), dict) else {}
    unit_type = record.get("unit_type") or metadata.get("unit_type")
    if not isinstance(unit_type, str) or not unit_type.strip():
        granularity = record.get("granularity") or metadata.get("granularity")
        unit_type = {"sentence": "sentence_pair", "clause": "clause_pair",
                     "phrase": "phrase_pair"}.get(str(granularity).strip(), granularity)
    else:
        unit_type = {"sentence": "sentence_pair", "clause": "clause_pair",
                     "phrase": "phrase_pair"}.get(unit_type.strip(), unit_type.strip())
    parent_pair_id = record.get("parent_pair_id") or metadata.get("parent_pair_id")
    if not isinstance(parent_pair_id, str) or not parent_pair_id.strip():
        parent_pair_id = record.get("id")
    aggregation_scope = record.get("aggregation_scope") or metadata.get("aggregation_scope")
    if not isinstance(aggregation_scope, str) or not aggregation_scope.strip():
        aggregation_scope = "parent_sentence" if unit_type in {"clause_pair", "phrase_pair", "clause", "phrase"} else "sentence"
    return {"unit_type": unit_type, "parent_pair_id": parent_pair_id,
            "aggregation_scope": aggregation_scope}


def _source_ref(meta: Dict[str, Any], input_name: str) -> Dict[str, Any]:
    return {"input": input_name, "line": meta["line"], "record_id": meta["record_id"]}


def _queue_record(meta: Dict[str, Any], selection_source: str, cfg, input_name: str) -> Dict[str, Any]:
    mandatory = selection_source == "triggered"
    delivery_blocking = mandatory or cfg.ordinary_sample_blocking
    record = {
        "record_id": meta["record_id"],
        "review_status": cfg.queue_status,
        "trigger_reasons": meta["trigger_reasons"],
        "selection_source": selection_source,
        "review_requirement": "mandatory" if mandatory else "audit_sample",
        "delivery_blocking": delivery_blocking,
        "governance_policy_version": cfg.governance_policy_version,
        "evidence_summary": meta["evidence"],
        "source_ref": _source_ref(meta, input_name),
        "queued_at": now_iso(),
        "operator": OPERATOR_NAME,
        "operator_version": OPERATOR_VERSION,
        "sampling_config": {
            "sample_ratio": cfg.sample_ratio,
            "min_sample_count": cfg.min_sample_count,
            "max_sample_count": cfg.max_sample_count,
            "random_seed": cfg.random_seed,
            "ordinary_sample_blocking": cfg.ordinary_sample_blocking,
        },
    }
    record.update({
        "unit_type": meta.get("unit_type"),
        "parent_pair_id": meta.get("parent_pair_id"),
        "aggregation_scope": meta.get("aggregation_scope"),
        "review_reason": ";".join(meta["trigger_reasons"]) or "random_sample",
    })
    return record


def _reviewed_record(meta: Dict[str, Any], entry: Dict[str, Any], conflict: bool,
                     cfg, input_name: str) -> Dict[str, Any]:
    reason, truncated = truncate_reason(entry.get("review_reason"), cfg)
    record = {
        "record_id": meta["record_id"],
        "review_status": entry["review_status"],
        "reviewer_id": mask_reviewer(entry.get("reviewer_id"), cfg),
        "reviewed_at": entry.get("reviewed_at"),
        "review_reason": reason,
        "review_reason_truncated": truncated,
        "reviewed_grade": entry.get("reviewed_grade"),
        "reviewed": True,
        "review_conflict": bool(conflict),
        "review_source": "review_payload",
        "trigger_reasons": meta["trigger_reasons"],
        "evidence_summary": meta["evidence"],
        "source_ref": _source_ref(meta, input_name),
        "reviewed_recorded_at": now_iso(),
        "operator": OPERATOR_NAME,
        "operator_version": OPERATOR_VERSION,
        "unit_type": meta.get("unit_type"),
        "parent_pair_id": meta.get("parent_pair_id"),
        "aggregation_scope": meta.get("aggregation_scope"),
    }
    return record


# --------------------------------------------------------------------------- #
# 复核状态持久化（跨次运行幂等的基础）
# --------------------------------------------------------------------------- #

def _load_state(path) -> Dict[str, Dict[str, Any]]:
    target = Path(path)
    if not target.is_file():
        return {}
    try:
        data = json.loads(target.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}
    if not isinstance(data, dict):
        return {}
    state: Dict[str, Dict[str, Any]] = {}
    for record_id, entry in data.items():
        if isinstance(entry, dict) and entry.get("review_status"):
            state[str(record_id)] = entry
    return state


def _save_state(path, state: Dict[str, Dict[str, Any]]) -> None:
    write_json_atomic(path, state)


def _resolve_payload_file(value: str, tdir: Path, input_file: Path) -> Optional[Path]:
    candidate = Path(value)
    if candidate.is_absolute():
        return candidate if candidate.is_file() else None
    for base in (tdir, tdir / "input", input_file.parent, BASE_DIR, Path.cwd()):
        target = (base / candidate)
        if target.is_file():
            return target
    return None


# --------------------------------------------------------------------------- #
# 主流程
# --------------------------------------------------------------------------- #

def _failure(logger, unique_id, input_file, snapshot, statistics,
             message: str, started: str, status: str = "failed") -> Dict[str, Any]:
    log_action(logger, "失败", message)
    # [无血缘改造 2026-10-19] 不再追加血缘；失败分支不生成阶段产物zip。
    close_logger(logger)
    return {"status": status, "outputFile": None, "queueFile": None, "reviewedFile": None,
            "rejectFile": None, "stageZip": None, "storageUri": None,
            "statistics": statistics, "message": message}


def run(input_file: str, unique_id: str, output_root=None,
        config_path=None, overrides: dict = None, logger=None) -> Dict[str, Any]:
    output_root = Path(output_root) if output_root else Path(os.getenv("OUTPUT_DIR", BASE_DIR / "output"))
    tdir = task_dir(output_root, unique_id)
    # [无血缘改造 2026-10-19] 不再生成 lineage 文件。
    # lineage_file = tdir / "lineage.json"
    logger = logger or get_logger("%s.%s" % (OPERATOR_NAME, unique_id), tdir / "run.log")

    try:
        cfg, snapshot = load_config(config_path, overrides)
    except (AttributeError, OSError, TypeError, ValueError, ValidationError, yaml.YAMLError) as exc:
        message = "配置错误：%s" % type(exc).__name__
        statistics = _base_statistics()
        statistics["config_error"] = message
        return _failure(logger, unique_id, input_file,
                        {"config_error": message}, statistics, message, now_iso())

    queue_file = tdir / cfg.queue_name
    reviewed_file = tdir / cfg.reviewed_name
    reject_file = tdir / cfg.reject_name
    state_file = tdir / cfg.state_file_name

    started = now_iso()
    input_path = Path(input_file)
    for target in (queue_file, reviewed_file, reject_file, state_file):
        try:
            same = Path(input_file).resolve() == target.resolve()
        except OSError:
            same = False
        if same:
            statistics = _base_statistics()
            return _failure(logger, unique_id, input_file, snapshot, statistics,
                            "输入路径与输出路径相同，拒绝执行以避免截断源文件", started)

    log_action(logger, "输入", "加载输入文件：%s" % input_path.name)
    try:
        encoding = select_encoding(input_file, cfg.input_encodings)
    except (OSError, ValueError) as exc:
        statistics = _base_statistics()
        message = "输入文件编码处理失败：%s，请检查文件编码" % type(exc).__name__
        return _failure(logger, unique_id, input_file, snapshot, statistics,
                        message, started)

    statistics = _base_statistics(encoding)
    log_action(logger, "配置", "抽样 sample_ratio=%s seed=%s min=%s max=%s 触发原因=%s" % (
        cfg.sample_ratio, cfg.random_seed, cfg.min_sample_count, cfg.max_sample_count,
        ",".join(cfg.trigger_reasons)))

    queue_writer = JsonlWriter(queue_file)
    reviewed_writer = JsonlWriter(reviewed_file)
    reject_writer = JsonlWriter(reject_file)

    try:
        # ---- 阶段1：流式读取候选，只保留证据摘要与引用 ----
        candidates: Dict[str, Dict[str, Any]] = {}
        order: List[str] = []
        for lineno, record, error in iter_input_records(input_file, encoding, cfg.input_format):
            statistics["input_records"] += 1
            if error:
                reject_writer.write({"line": lineno, "record_type": "input",
                                     "record": None, "reasons": [error]})
                statistics["input_rejected_count"] += 1
                _append_reason(statistics, error)
                continue
            if not isinstance(record, dict):
                reject_writer.write({"line": lineno, "record_type": "input",
                                     "record": None, "reasons": ["invalid_record"]})
                statistics["input_rejected_count"] += 1
                _append_reason(statistics, "invalid_record")
                continue
            record_id = _record_id(record, cfg)
            if not record_id:
                reject_writer.write({"line": lineno, "record_type": "input",
                                     "record": None, "reasons": ["missing_field:record_id"]})
                statistics["input_rejected_count"] += 1
                _append_reason(statistics, "missing_field:record_id")
                continue
            if record_id in candidates:
                reject_writer.write({"line": lineno, "record_type": "input",
                                     "record": {"record_id": record_id},
                                     "reasons": ["duplicate_record_id"]})
                statistics["input_rejected_count"] += 1
                _append_reason(statistics, "duplicate_record_id")
                continue
            reasons = detect_trigger_reasons(record, cfg)
            for reason in reasons:
                statistics["trigger_reason_counts"][reason] = (
                    statistics["trigger_reason_counts"].get(reason, 0) + 1
                )
            candidates[record_id] = {
                "record_id": record_id,
                "line": lineno,
                "trigger_reasons": reasons,
                "evidence": _evidence_summary(record, cfg, reasons),
                **_scope_fields(record),
            }
            order.append(record_id)
            statistics["candidate_count"] += 1

        log_action(logger, "读取", "候选记录 %d 条，输入异常 %d 条" % (
            statistics["candidate_count"], statistics["input_rejected_count"]))

        if statistics["candidate_count"] == 0:
            if reject_writer.count > 0:
                reject_writer.commit()
            else:
                reject_writer.abort()
            queue_writer.abort()
            reviewed_writer.abort()
            statistics["waiting_manual_count"] = 0
            return _failure(logger, unique_id, input_file, snapshot, statistics,
                            "输入文件为空或无有效候选记录", started)

        # ---- 阶段2：抽样 ----
        selection = select_for_review([candidates[rid] for rid in order], cfg)
        statistics["triggered_count"] = selection["triggered_count"]
        statistics["sample_pool_count"] = selection["sample_pool_count"]
        statistics["random_sampled_count"] = selection["random_sampled_count"]
        statistics["sampled_count"] = selection["sampled_count"]

        # ---- 阶段3：人工结果 payload（可选，工程侧显式回写） ----
        raw_payload: List[Tuple[str, int, Any]] = []
        for index, item in enumerate(cfg.review_payload or [], 1):
            raw_payload.append(("config", index, item))
        if cfg.review_payload_file:
            resolved = _resolve_payload_file(cfg.review_payload_file, tdir, input_path)
            if resolved is None:
                reject_writer.write({"line": 0, "record_type": "payload",
                                     "record": {"file": Path(cfg.review_payload_file).name},
                                     "reasons": ["review_payload_not_found"]})
                statistics["payload_rejected_count"] += 1
                _append_reason(statistics, "review_payload_not_found")
            else:
                for lineno, record, error in iter_input_records(resolved, encoding, cfg.input_format):
                    if error:
                        reject_writer.write({"line": lineno, "record_type": "payload",
                                             "record": None, "reasons": [error]})
                        statistics["payload_rejected_count"] += 1
                        _append_reason(statistics, error)
                        continue
                    raw_payload.append(("file", lineno, record))
        statistics["payload_entry_count"] = len(raw_payload)

        # ---- 阶段4：校验 + 幂等合并（冲突不覆盖） ----
        state = _load_state(state_file)
        applied: Dict[str, Dict[str, Any]] = {}
        conflicting: set = set()
        for source, lineno, raw in raw_payload:
            entry, reasons = validate_review_entry(raw, cfg)
            if reasons:
                reject_writer.write({"line": lineno, "record_type": "payload",
                                     "record": {"source": source}, "reasons": reasons})
                statistics["payload_rejected_count"] += 1
                for reason in reasons:
                    _append_reason(statistics, reason)
                continue
            record_id = entry["record_id"]
            if cfg.require_known_target and record_id not in candidates:
                reject_writer.write({"line": lineno, "record_type": "payload",
                                     "record": {"source": source, "record_id": record_id},
                                     "reasons": ["review_target_not_found"]})
                statistics["payload_rejected_count"] += 1
                _append_reason(statistics, "review_target_not_found")
                continue
            if record_id in state:
                if review_identity(state[record_id]) == review_identity(entry):
                    statistics["duplicate_review_ignored"] += 1
                else:
                    conflicting.add(record_id)
                    reject_writer.write({"line": lineno, "record_type": "payload",
                                         "record": {"record_id": record_id,
                                                    "applied_status": state[record_id].get("review_status"),
                                                    "incoming_status": entry["review_status"]},
                                         "reasons": ["review_conflict"]})
                    statistics["conflict_count"] += 1
                    _append_reason(statistics, "review_conflict")
                continue
            if record_id in applied:
                if review_identity(applied[record_id]) == review_identity(entry):
                    statistics["duplicate_review_ignored"] += 1
                else:
                    conflicting.add(record_id)
                    applied.pop(record_id, None)
                    reject_writer.write({"line": lineno, "record_type": "payload",
                                         "record": {"record_id": record_id,
                                                    "incoming_status": entry["review_status"]},
                                         "reasons": ["review_conflict"]})
                    statistics["conflict_count"] += 1
                    _append_reason(statistics, "review_conflict")
                continue
            applied[record_id] = entry

        final_state = dict(state)
        final_state.update(applied)
        if applied:
            _save_state(state_file, final_state)

        # ---- 阶段5：写队列与已复核 ----
        input_name = input_path.name
        queue_written = 0
        mandatory_pending = 0
        audit_pending = 0
        expected_pending_ids: List[str] = []
        queued_ids: List[str] = []
        for item in selection["selected"]:
            record_id = item["record_id"]
            if record_id in final_state:
                continue  # 已复核，不再进入待人工队列
            expected_pending_ids.append(record_id)
            queue_writer.write(_queue_record(candidates[record_id], item["selection_source"],
                                             cfg, input_name))
            queued_ids.append(record_id)
            queue_written += 1
            if item["selection_source"] == "triggered" or cfg.ordinary_sample_blocking:
                mandatory_pending += 1
            else:
                audit_pending += 1

        # Internal contract check: every selected-unreviewed candidate must
        # appear exactly once in the queue, and no queue id may be unknown or
        # duplicated. This catches accidental filtering or status overwrites
        # before downstream operators consume the queue.
        duplicate_queue_ids = len(queued_ids) - len(set(queued_ids))
        missing_queue_ids = set(expected_pending_ids) - set(queued_ids)
        extra_queue_ids = set(queued_ids) - set(expected_pending_ids)
        id_errors = duplicate_queue_ids + len(missing_queue_ids) + len(extra_queue_ids)
        if id_errors:
            statistics["id_consistency_errors"] = id_errors
            statistics["id_consistency_ok"] = False
            reject_writer.write({
                "line": 0,
                "record_type": "internal",
                "record": None,
                "reasons": ["queue_id_mismatch"],
                "details": {
                    "duplicate_count": duplicate_queue_ids,
                    "missing_ids": sorted(missing_queue_ids),
                    "extra_ids": sorted(extra_queue_ids),
                },
            })
            _append_reason(statistics, "queue_id_mismatch")
        statistics["queue_written_count"] = queue_written
        statistics["mandatory_review_count"] = selection["triggered_count"]
        statistics["audit_sample_count"] = selection["audit_sample_count"]
        statistics["blocking_pending_count"] = mandatory_pending
        statistics["nonblocking_audit_pending_count"] = audit_pending
        statistics["pending_manual_count"] = mandatory_pending
        statistics["waiting_manual_count"] = mandatory_pending

        status_counts = {"accepted": 0, "rejected": 0, "needs_revision": 0, "waiting_manual": 0}
        reviewed_count = 0
        for record_id in order:
            if record_id not in final_state:
                continue
            entry = final_state[record_id]
            reviewed_writer.write(_reviewed_record(candidates[record_id], entry,
                                                   record_id in conflicting, cfg, input_name))
            reviewed_count += 1
            current = entry.get("review_status")
            if current in status_counts:
                status_counts[current] += 1
        statistics["reviewed_count"] = reviewed_count
        statistics["accepted_count"] = status_counts["accepted"]
        statistics["rejected_count"] = status_counts["rejected"]
        statistics["needs_revision_count"] = status_counts["needs_revision"]
        statistics["reviewed_waiting_manual_count"] = status_counts["waiting_manual"]
        statistics["reviewed_status_counts"] = status_counts

        rejected_total = (statistics["input_rejected_count"]
                          + statistics["payload_rejected_count"]
                          + statistics["conflict_count"])
        statistics["rejected_total"] = rejected_total
        if not statistics["id_consistency_ok"]:
            status = "failed"
            message = "人工复核队列 ID 一致性检查失败"
        elif rejected_total > 0:
            status = "partial"
            message = ""
        elif mandatory_pending > 0:
            status = "waiting_manual"
            message = "存在强制人工复核记录，未收到人工结果"
        else:
            status = "success"
            message = ("普通合格记录已进入非阻塞风险抽检队列"
                       if audit_pending else "")

        # 三个产物固定产出（异常清单无内容时也是合法空文件），便于下游稳定消费。
        queue_writer.commit()
        reviewed_writer.commit()
        reject_writer.commit()
    except (OSError, UnicodeDecodeError, ValueError) as exc:
        queue_writer.abort()
        reviewed_writer.abort()
        reject_writer.abort()
        message = "处理失败：%s" % type(exc).__name__
        return _failure(logger, unique_id, input_file, snapshot, statistics,
                        message, started)

    log_action(logger, "抽样", "抽样入队 %d 条（触发 %d，随机 %d），人工结果条目 %d 条" % (
        statistics["sampled_count"], statistics["triggered_count"],
        statistics["random_sampled_count"], statistics["payload_entry_count"]))
    log_action(logger, "处理", "已复核 %d 条（accepted=%d rejected=%d needs_revision=%d），待人工 %d 条，冲突 %d 条，拒绝 %d 条" % (
        statistics["reviewed_count"], statistics["accepted_count"],
        statistics["rejected_count"], statistics["needs_revision_count"],
        statistics["queue_written_count"], statistics["conflict_count"], rejected_total))
    log_action(logger, "输出", "写入 %s / %s / %s" % (
        queue_file.name, reviewed_file.name, reject_file.name))

    # [无血缘改造 2026-10-19] 不再追加血缘；成功/部分成功/待人工分支生成阶段产物。
    stage_zip = storage_uri = None
    if status != "failed":
        stage_zip, storage_uri = finalize_stage_artifacts(
            tdir, OPERATOR_ID, unique_id, queue_file, reject_file, tdir / "run.log")
        log_action(logger, "归档", "阶段产物：%s；存储地址：%s"
                   % (Path(stage_zip).name if stage_zip else "无", storage_uri))
    log_action(logger, "完成", "算子执行完成，状态 %s" % status)
    close_logger(logger)
    return {
        "status": status,
        "outputFile": str(queue_file),
        "queueFile": str(queue_file),
        "reviewedFile": str(reviewed_file),
        "rejectFile": str(reject_file),
        "stageZip": stage_zip,
        "storageUri": storage_uri,
        "statistics": statistics,
        "message": message,
    }


def main():
    parser = argparse.ArgumentParser(description="算子20：人工抽检复核")
    parser.add_argument("--input", required=True, help="待复核候选 JSONL 文件")
    parser.add_argument("--unique-id", default="local_demo_file1", help="任务id_文件id")
    parser.add_argument("--config", default=None, help="配置文件路径")
    parser.add_argument("--output-root", default=None, help="产物根目录")
    parser.add_argument("--review-file", default=None, help="工程侧人工结果 JSONL（可选）")
    args = parser.parse_args()
    overrides = {"review_payload_file": args.review_file} if args.review_file else None
    result = run(args.input, args.unique_id, args.output_root, args.config, overrides)
    print("status=%s statistics=%s" % (result["status"], result["statistics"]))
    print("stageZip=%s storageUri=%s" % (result.get("stageZip"), result.get("storageUri")))


if __name__ == "__main__":
    main()
