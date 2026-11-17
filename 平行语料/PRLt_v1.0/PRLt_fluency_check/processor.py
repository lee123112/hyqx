"""算子15：译文流畅度检测 —— 模型适配、规则证据、阈值过滤与 CLI。

处理边界：只读 `target`，不改写 source/target 原文；流畅度分数只能来自
本地 Qwen3.5-2B 适配器（或显式配置的 HTTP 服务），规则层只产出可解释证据，
绝不把证据映射为分数。
"""
import argparse
import codecs
import json
import math
import os
from pathlib import Path
from typing import Any, Dict, Iterator, List, Optional, Tuple

import yaml
from pydantic import ValidationError

from utils.artifacts import finalize_stage_artifacts
from utils.fluency import (BackendUnavailable, resolve_backend, rule_defects,
                           rule_evidence, verify_defects)
from utils.io import JsonlWriter, count_lines, task_dir
# [无血缘改造 2026-10-19] 血缘输出已停用，保留原导入便于回溯：
# from utils.lineage import append_lineage, make_lineage, now_iso
from utils.logger import close_logger, get_logger, log_action
from utils.schemas import OperatorConfig
from utils.timeutil import now_iso

BASE_DIR = Path(__file__).resolve().parent
OPERATOR_NAME = "translation_fluency"
OPERATOR_ID = "15"
OPERATOR_VERSION = "v1.1.0"
OUTPUT_FILE = "15_fluency_checked.jsonl"
REJECT_FILE = "15_rejects.jsonl"
EXCERPT_CHARS = 40


def load_config(config_path=None, overrides: dict = None) -> Tuple[OperatorConfig, dict]:
    cfg_path = Path(config_path) if config_path else BASE_DIR / "configs" / "config.yaml"
    raw = yaml.safe_load(cfg_path.read_text(encoding="utf-8")) or {}
    if not isinstance(raw, dict):
        raise ValueError("配置文件必须是对象")
    merged = {key: value for key, value in raw.items() if key != "operator"}
    if overrides:
        merged.update(overrides)
    cfg = OperatorConfig(**merged)
    snapshot = {"operator": raw.get("operator", {}), **merged}
    return cfg, snapshot


def iter_decoded_lines(path, encoding: str, chunk_size: int = 65536) -> Iterator[str]:
    """按指定编码增量严格解码，逐行 yield 逻辑行（不整文件读入内存）。"""
    decoder = codecs.getincrementaldecoder(encoding)(errors="strict")
    buffer = ""
    with open(path, "rb") as stream:
        while True:
            chunk = stream.read(chunk_size)
            if not chunk:
                break
            buffer += decoder.decode(chunk, final=False)
            while "\n" in buffer:
                line, buffer = buffer.split("\n", 1)
                yield line[:-1] if line.endswith("\r") else line
        buffer += decoder.decode(b"", final=True)
    if buffer:
        yield buffer[:-1] if buffer.endswith("\r") else buffer


def select_encoding(path, encodings: List[str]) -> str:
    """选择第一个能严格解码整个文件的编码，保证流式读取可用。"""
    if not encodings:
        raise ValueError("未配置输入编码")
    errors = []
    for encoding in encodings:
        try:
            for _ in iter_decoded_lines(path, encoding):
                pass
            return encoding.lower()
        except (LookupError, OSError, UnicodeDecodeError) as exc:
            errors.append(type(exc).__name__)
    raise ValueError("输入文件无法按配置编码解码：" + ",".join(errors))


def _base_statistics() -> Dict[str, Any]:
    return {
        "input_records": 0,
        # processed/output/audit counters are deliberately distinct from the
        # legacy passed/rejected counters. In mark_and_keep mode a low-quality
        # record is retained in the output and also written to the audit file.
        "processed_records": 0,
        "output_records": 0,
        "audit_records": 0,
        "physical_reject_records": 0,
        "passed_records": 0,
        "rejected_records": 0,
        "hard_rejected_records": 0,
        "moved_to_rejects": 0,
        "scored_records": 0,
        "accepted_records": 0,
        "low_fluency_records": 0,
        "waiting_calibration_records": 0,
        "uncalibrated_score_records": 0,
        "evaluation_only_calibration_records": 0,
        "degraded_records": 0,
        "model_unavailable_records": 0,
        "waiting_manual_records": 0,
        "model_error_records": 0,
        # 规则门禁（连续标点串等结构性硬伤）判为 low_fluency 的条数
        "rule_gate_flagged_records": 0,
        # defect_count_v1：至少有一条核验通过缺陷 / 一条都没有的条数，
        # 以及按类型的缺陷计数与被判为 suspect（未采信）的主张总数。
        "defect_verified_records": 0,
        "defect_clean_records": 0,
        "verified_defect_total": 0,
        "suspect_defect_total": 0,
        "defect_type_counts": {},
        # B2：首次解析失败、用 temperature=0.2 重试后拿到合法输出的条数（分数是重试那次的
        # 真实模型输出，不是修补/补齐的）；以及本次任务实际生效的模型错误容忍上限。
        "model_output_repaired_records": 0,
        "model_error_limit": None,
        "threshold": None,
        "model_version": None,
        "backend": None,
        "encoding": None,
    }


def _bump(statistics: Dict[str, Any], key: str) -> None:
    statistics[key] = statistics.get(key, 0) + 1


def _append_reason(statistics: Dict[str, Any], reason: str) -> None:
    _bump(statistics, f"reason:{reason}")


def compute_score(defects: List[dict], tokens: int, cfg) -> Tuple[float, float, int]:
    """把「已核验缺陷」折算成 (0-100 展示分, 每百词加权错误率, 计分分母)。

    score = 100 × max(0, 1 − rate / score_scale)
    rate  = Σ severity_weight / max(词数, token_floor) × 100

    这是确定性映射，不是校准概率：0 条核验缺陷恒等于 100 分，
    没有核验缺陷就不可能出现低分——「先给分再编理由」在结构上被切断。
    """
    weight = 0.0
    minor_weight = float(cfg.severity_weights.get("minor", 0.4))
    for item in defects:
        weight += float(cfg.severity_weights.get(item.get("severity"), minor_weight))
    denominator = max(int(tokens), int(cfg.token_floor))
    rate = weight / denominator * 100.0
    score = 100.0 * max(0.0, 1.0 - rate / float(cfg.score_scale))
    return round(score, 2), round(rate, 3), denominator


def _excerpt(value: Any) -> Optional[str]:
    """异常清单只保留有限长度摘录，避免落盘完整原文。"""
    if not isinstance(value, str):
        return None
    return value if len(value) <= EXCERPT_CHARS else value[:EXCERPT_CHARS] + "…"


def _reject_entry(lineno: int, record: Any, reasons: List[str],
                  output: Optional[dict] = None) -> dict:
    entry: Dict[str, Any] = {
        "line": lineno,
        "id": None,
        "target_lang": None,
        "target_excerpt": None,
        "reasons": list(reasons),
    }
    if isinstance(record, dict):
        entry["id"] = record.get("id")
        entry["target_lang"] = record.get("target_lang")
        entry["target_excerpt"] = _excerpt(record.get("target"))
    if output is not None:
        entry["fluency_score"] = output.get("fluency_score")
        entry["fluency_status"] = output.get("fluency_status")
        entry["fluency_backend"] = output.get("fluency_backend")
    return entry


def _validate_structure(record: dict, cfg: OperatorConfig) -> Tuple[Optional[str], Optional[dict]]:
    """结构校验；返回 (失败原因, metadata)。原因非空表示硬异常。"""
    if cfg.text_field not in record:
        return f"missing_field:{cfg.text_field}", None
    value = record.get(cfg.text_field)
    if not isinstance(value, str):
        return f"invalid_type:{cfg.text_field}", None
    if not value.strip():
        return f"empty_{cfg.text_field}", None

    metadata = record.get("metadata")
    if metadata is None:
        metadata = {}
    if not isinstance(metadata, dict):
        return "invalid_type:metadata", None

    if cfg.language_check:
        declared = record.get("target_lang")
        if declared is not None and declared != cfg.target_lang:
            return "language_declaration_mismatch", None
    return None, metadata


def _output_record(record: dict, metadata: dict, cfg: OperatorConfig, backend: str,
                   score: Optional[float], fluent: Optional[bool], status: str,
                   model_version: Optional[str], grammar_errors: List[dict],
                   evidence: List[dict], checked_at: str,
                   score_semantics: Optional[str] = None,
                   business_gate_approved: Optional[bool] = None,
                   model_output_repaired: Optional[bool] = None,
                   rule_reasons: Optional[List[str]] = None,
                   defect_info: Optional[dict] = None) -> dict:
    output = dict(record)
    unit_type = output.get("unit_type") or metadata.get("unit_type")
    if not isinstance(unit_type, str) or not unit_type.strip():
        granularity = output.get("granularity") or metadata.get("granularity")
        unit_type = {"sentence": "sentence_pair", "clause": "clause_pair",
                     "phrase": "phrase_pair", "clause_or_phrase": "clause_pair"}.get(
                         str(granularity).strip(), granularity)
    else:
        unit_type = {"sentence": "sentence_pair", "clause": "clause_pair",
                     "phrase": "phrase_pair", "clause_or_phrase": "clause_pair"}.get(
                         unit_type.strip(), unit_type.strip())
    parent_pair_id = output.get("parent_pair_id") or metadata.get("parent_pair_id")
    if not isinstance(parent_pair_id, str) or not parent_pair_id.strip():
        parent_pair_id = metadata.get("parent_pair_id") or output.get("id")
    aggregation_scope = output.get("aggregation_scope") or metadata.get("aggregation_scope")
    if not isinstance(aggregation_scope, str) or not aggregation_scope.strip():
        aggregation_scope = "parent_sentence" if unit_type in {"clause_pair", "phrase_pair"} else "sentence"
    output["unit_type"] = unit_type
    output["parent_pair_id"] = parent_pair_id
    output["aggregation_scope"] = aggregation_scope
    output["fluency_score"] = score
    output["fluency_score_semantics"] = score_semantics
    output["fluency_business_gate_approved"] = business_gate_approved
    output["fluent"] = fluent
    output["fluency_status"] = status
    output["fluency_backend"] = backend
    output["model_version"] = model_version
    output["fluency_model_version"] = model_version
    output["grammar_errors"] = grammar_errors
    output["fluency_evidence"] = evidence
    output["fluency_model_output_repaired"] = model_output_repaired
    output["fluency_threshold"] = cfg.fluency_threshold
    output["fluency_checked_at"] = checked_at
    # defect_count_v1 的附加字段（缺陷清单/计数/错误率/分词数）；rubric 口径下为 None。
    defect_fields = dict(defect_info or {})
    scoring_mode = defect_fields.pop("scoring_mode", cfg.scoring_mode)
    output["fluency_scoring_mode"] = scoring_mode
    output.update(defect_fields)

    meta_out = dict(metadata)
    if rule_reasons:
        reason = "punctuation_disorder"
        previous = str(meta_out.get("low_confidence_reason") or "").strip()
        merged = f"{previous}+{reason}" if previous else reason
        meta_out["review_status"] = meta_out.get("review_status") or "needs_review"
        meta_out["low_confidence_reason"] = merged
        meta_out["flagged_by"] = "15_rule"
        output["review_status"] = meta_out["review_status"]
        output["low_confidence_reason"] = merged
        output["flagged_by"] = "15_rule"
    meta_out.update({
        "fluency_score": score,
        "fluency_score_semantics": score_semantics,
        "fluency_business_gate_approved": business_gate_approved,
        "fluent": fluent,
        "fluency_status": status,
        "fluency_backend": backend,
        "model_version": model_version,
        "fluency_model_version": model_version,
        "grammar_errors": grammar_errors,
        "fluency_evidence": evidence,
        "fluency_model_output_repaired": model_output_repaired,
        "fluency_threshold": cfg.fluency_threshold,
        "fluency_checked_at": checked_at,
        "unit_type": unit_type,
        "parent_pair_id": parent_pair_id,
        "aggregation_scope": aggregation_scope,
        "fluency_scoring_mode": scoring_mode,
        **defect_fields,
    })
    output["metadata"] = meta_out
    return output


def run(input_file: str, unique_id: str, output_root=None,
        config_path=None, overrides: dict = None, logger=None,
        model_scorer=None) -> dict:
    output_root = Path(output_root) if output_root else Path(os.getenv("OUTPUT_DIR", BASE_DIR / "output"))
    tdir = task_dir(output_root, unique_id)
    out_file = tdir / OUTPUT_FILE
    rej_file = tdir / REJECT_FILE
    tmp_file = tdir / (OUTPUT_FILE + ".tmp")
    tmp_rej = tdir / (REJECT_FILE + ".tmp")
    # [无血缘改造 2026-10-19] 不再生成 lineage 文件。
    # lineage_file = tdir / "lineage.json"
    logger = logger or get_logger(f"{OPERATOR_NAME}.{unique_id}", tdir / "run.log")

    try:
        cfg, cfg_snapshot = load_config(config_path, overrides)
    except (AttributeError, OSError, TypeError, ValueError, ValidationError, yaml.YAMLError) as exc:
        message = f"配置错误：{type(exc).__name__}"
        statistics = _base_statistics()
        log_action(logger, "失败", message)
        # [无血缘改造 2026-10-19] 血缘输出已停用，保留原实现便于回溯；不再生成 lineage 文件。
        # append_lineage(lineage_file, make_lineage(
        #     OPERATOR_NAME, OPERATOR_VERSION, unique_id,
        #     str(input_file), str(out_file), "failed", now_iso(), now_iso(),
        #     {"config_error": message}, statistics))
        close_logger(logger)
        return {"status": "failed", "outputFile": None, "rejectFile": None,
                "stageZip": None, "storageUri": None, "statistics": statistics,
                "message": message}

    started = now_iso()
    statistics = _base_statistics()
    statistics["threshold"] = cfg.fluency_threshold
    statistics["scoring_mode"] = cfg.scoring_mode
    log_action(logger, "输入", f"加载输入文件：{Path(input_file).name}")
    log_action(logger, "配置", f"后端={cfg.backend} 方向={cfg.source_lang}->{cfg.target_lang} "
                              f"阈值={cfg.fluency_threshold} 降级策略={cfg.fallback_policy}")

    try:
        backend, scorer, degrade_reason = resolve_backend(cfg, model_scorer)
    except BackendUnavailable as exc:
        message = f"模型后端不可用且未选择降级：{exc}"
        statistics["backend"] = "unavailable"
        log_action(logger, "失败", message)
        # [无血缘改造 2026-10-19] 血缘输出已停用，保留原实现便于回溯；不再生成 lineage 文件。
        # append_lineage(lineage_file, make_lineage(
        #     OPERATOR_NAME, OPERATOR_VERSION, unique_id,
        #     str(input_file), str(out_file), "failed", started, now_iso(),
        #     cfg_snapshot, statistics))
        close_logger(logger)
        return {"status": "failed", "outputFile": None, "rejectFile": None,
                "stageZip": None, "storageUri": None, "statistics": statistics,
                "message": message}

    statistics["backend"] = backend
    statistics["model_version"] = getattr(scorer, "model_version", None) if backend == "model" else None
    if degrade_reason:
        statistics["degrade_reason"] = degrade_reason
        log_action(logger, "降级", f"进入规则降级后端：{degrade_reason}")
    else:
        log_action(logger, "后端", f"使用模型后端：{statistics['model_version'] or 'model'}")

    try:
        encoding = select_encoding(input_file, cfg.input_encodings)
    except (OSError, ValueError) as exc:
        message = f"输入文件编码处理失败：{type(exc).__name__}，请检查文件编码"
        log_action(logger, "失败", message)
        # [无血缘改造 2026-10-19] 血缘输出已停用，保留原实现便于回溯；不再生成 lineage 文件。
        # append_lineage(lineage_file, make_lineage(
        #     OPERATOR_NAME, OPERATOR_VERSION, unique_id,
        #     str(input_file), str(out_file), "failed", started, now_iso(),
        #     cfg_snapshot, statistics))
        close_logger(logger)
        return {"status": "failed", "outputFile": None, "rejectFile": None,
                "stageZip": None, "storageUri": None, "statistics": statistics,
                "message": message}

    statistics["encoding"] = encoding
    if Path(input_file).resolve() == out_file.resolve():
        message = "输入文件与输出文件路径相同，拒绝覆盖输入"
        log_action(logger, "失败", message)
        # [无血缘改造 2026-10-19] 血缘输出已停用，保留原实现便于回溯；不再生成 lineage 文件。
        # append_lineage(lineage_file, make_lineage(
        #     OPERATOR_NAME, OPERATOR_VERSION, unique_id,
        #     str(input_file), str(out_file), "failed", started, now_iso(),
        #     cfg_snapshot, statistics))
        close_logger(logger)
        return {"status": "failed", "outputFile": None, "rejectFile": None,
                "stageZip": None, "storageUri": None, "statistics": statistics,
                "message": message}

    checked_at = now_iso()
    model_errors = 0
    abort_message: Optional[str] = None
    log_action(logger, "读取", f"解码输入文件：{encoding}")
    try:
        with JsonlWriter(tmp_file) as checked_writer, JsonlWriter(tmp_rej) as reject_writer:
            # 第一段：解码 + 结构校验 + 规则证据；硬拒绝即时写出，有效记录收集待打分
            pending: List[tuple] = []  # (lineno, record, metadata, rule_evidence)
            for lineno, line in enumerate(iter_decoded_lines(input_file, encoding), 1):
                if not line.strip():
                    continue
                statistics["input_records"] += 1
                try:
                    record = json.loads(line)
                except json.JSONDecodeError:
                    reject_writer.write(_reject_entry(lineno, None, ["invalid_json"]))
                    _bump(statistics, "hard_rejected_records")
                    _append_reason(statistics, "invalid_json")
                    continue
                if not isinstance(record, dict):
                    reject_writer.write(_reject_entry(lineno, None, ["invalid_record"]))
                    _bump(statistics, "hard_rejected_records")
                    _append_reason(statistics, "invalid_record")
                    continue

                reason, metadata = _validate_structure(record, cfg)
                if reason:
                    reject_writer.write(_reject_entry(lineno, record, [reason]))
                    _bump(statistics, "hard_rejected_records")
                    _append_reason(statistics, reason)
                    continue

                target = record[cfg.text_field]
                pending.append((lineno, record, metadata, rule_evidence(target, cfg.target_lang)))

            # [2026-11-02 B2] 容忍上限 = max(绝对下限, ceil(比例 × 输入条数))：
            # 小批次按原来的 20 条兜底，大批次按 2% 放大，避免 5000 条时容忍度只剩 0.4%。
            model_error_limit = max(
                cfg.max_model_errors,
                math.ceil(cfg.max_model_error_rate * statistics["input_records"]))
            statistics["model_error_limit"] = model_error_limit

            def _finalize(lineno, record, metadata, evidence, payload_result) -> bool:
                """逐条裁决与写出；payload_result：None=规则后端 / Exception / 模型 payload。
                返回 False 表示模型错误超上限应中止。"""
                nonlocal model_errors, abort_message
                score: Optional[float] = None
                fluent: Optional[bool] = None
                grammar_errors: List[dict] = []
                model_evidence: List[dict] = []
                model_version: Optional[str] = None
                score_semantics: Optional[str] = None
                business_gate_approved: Optional[bool] = None
                model_output_repaired: Optional[bool] = None
                model_error = False
                defect_info: Dict[str, Any] = {}

                if backend == "model":
                    if isinstance(payload_result, Exception):
                        # 单条模型异常按 model_error 处理，绝不伪造分数
                        model_error = True
                        model_errors += 1
                        log_action(logger, "警告",
                                   f"第 {lineno} 条模型推理失败：{type(payload_result).__name__}")
                    elif "defects" in payload_result:
                        # ---- defect_count_v1：模型只提主张，核验通过才计分 ----
                        payload = payload_result
                        model_version = payload.get("model_version")
                        score_semantics = payload.get("score_semantics") or "defect_count_v1"
                        business_gate_approved = payload.get("business_gate_approved")
                        model_output_repaired = payload.get("model_output_repaired")
                        target_text = record.get(cfg.text_field) or ""
                        token_count = len(target_text.split())
                        claimed = [item for item in (payload.get("defects") or [])
                                   if isinstance(item, dict)]
                        verified_model, suspects = verify_defects(target_text, claimed, cfg)
                        # 规则已确证的结构性缺陷与模型主张一起计分；总数受 max_defects 约束。
                        verified_all = (verified_model + rule_defects(evidence))[:cfg.max_defects]
                        score, rate, denominator = compute_score(
                            verified_all, token_count, cfg)
                        grammar_errors = [item.get("span") for item in claimed]
                        counts: Dict[str, int] = {}
                        for item in verified_all:
                            counts[item["type"]] = counts.get(item["type"], 0) + 1
                        if verified_all:
                            _bump(statistics, "defect_verified_records")
                        else:
                            _bump(statistics, "defect_clean_records")
                        statistics["verified_defect_total"] += len(verified_all)
                        statistics["suspect_defect_total"] += len(suspects)
                        for kind, number in counts.items():
                            statistics["defect_type_counts"][kind] = \
                                statistics["defect_type_counts"].get(kind, 0) + number
                        for item in verified_all:
                            model_evidence.append({
                                "type": "defect_verified",
                                "detail": f"{item['type']}: {item['span']} -> {item['fix']}"[:300]})
                        for item in suspects:
                            model_evidence.append({
                                "type": "defect_suspect",
                                "detail": f"{item['type']}({item['reason']}): {item['span']}"[:300]})
                        if model_output_repaired is True:
                            _bump(statistics, "model_output_repaired_records")
                        if business_gate_approved is None:
                            # 与 rubric 口径一致：缺省视为「未批准」，不拿缺失字段当业务批准。
                            business_gate_approved = False
                        defect_info = {
                            "scoring_mode": "defect_count_v1",
                            "fluency_defects": verified_all,
                            "fluency_suspects": suspects,
                            "fluency_defect_counts": counts,
                            "fluency_defect_rate_per_100_tokens": rate,
                            "fluency_tokens": token_count,
                            "fluency_token_denominator": denominator,
                            "fluency_verified_ratio": round(
                                len(verified_model) / max(1, len(verified_model) + len(suspects)), 3),
                            "fluency_low_confidence_length":
                                token_count < cfg.low_confidence_length_tokens,
                        }
                    else:
                        payload = payload_result
                        score = payload["fluency_score"]
                        grammar_errors = payload["grammar_errors"]
                        model_evidence = payload["evidence"]
                        model_version = payload["model_version"]
                        score_semantics = payload.get("score_semantics") or "model_score_0_100"
                        business_gate_approved = payload.get("business_gate_approved")
                        model_output_repaired = payload.get("model_output_repaired")
                        if model_output_repaired is True:
                            _bump(statistics, "model_output_repaired_records")
                        if business_gate_approved is None:
                            # 缺省按「未批准」处理：模型没显式声明可用于业务门禁时，
                            # 不能拿缺失字段当成业务批准（否则任何未实现的适配器都会
                            # 悄悄获得放行/拦截权）。规范 4.15：未校准分数只作辅助证据。
                            business_gate_approved = False
                    if model_errors > model_error_limit:
                        abort_message = (
                            f"模型错误超过上限 {model_error_limit}"
                            f"（绝对下限 {cfg.max_model_errors} / 比例 {cfg.max_model_error_rate:.2%}"
                            f" × {statistics['input_records']} 条取大），整次任务失败")
                        return False

                if model_error:
                    status = "model_error"
                    _bump(statistics, "model_error_records")
                    _bump(statistics, "degraded_records")
                elif score is not None:
                    _bump(statistics, "scored_records")
                    calibrated = (not cfg.require_calibrated_score or
                                  score_semantics in cfg.accepted_score_semantics)
                    if cfg.require_approved_calibration and score_semantics == \
                            "calibrated_probability_percent":
                        calibrated = calibrated and business_gate_approved is True
                    if not calibrated:
                        status, fluent = "waiting_calibration", None
                        _bump(statistics, "waiting_calibration_records")
                        if score_semantics in cfg.accepted_score_semantics:
                            _bump(statistics, "evaluation_only_calibration_records")
                        else:
                            _bump(statistics, "uncalibrated_score_records")
                    elif score >= cfg.fluency_threshold:
                        status, fluent = "accepted", True
                        _bump(statistics, "accepted_records")
                    else:
                        status, fluent = "low_fluency", False
                        _bump(statistics, "low_fluency_records")
                    if status != "waiting_calibration" and any(isinstance(item, dict) and str(
                            item.get("score_semantics") or "").startswith("uncalibrated_")
                           for item in model_evidence):
                        _bump(statistics, "uncalibrated_score_records")
                else:
                    status = "waiting_manual" if cfg.allow_degraded_delivery else "model_unavailable"
                    _bump(statistics, "degraded_records")
                    if status == "waiting_manual":
                        _bump(statistics, "waiting_manual_records")
                    else:
                        _bump(statistics, "model_unavailable_records")

                # ---- 规则门禁：结构性硬伤 → low_fluency ----
                # 未校准的模型分不参与放行/拦截（规范 4.15），但"连续标点串"这类
                # 结构性缺陷不需要模型校准就能确证。规则只改状态与复核标记，
                # fluency_score 原样保留（模型给了分就照抄，没给仍是 null），
                # 绝不把规则结论写成模型分数。
                rule_hits: List[str] = []
                # 只对走完模型流程的记录生效（accepted / waiting_calibration /
                # waiting_manual）；model_unavailable / model_error 本身已经不在
                # accepted 路径上（下游 17 不会放行），不需要规则再改判。
                status_counter = {"accepted": "accepted_records",
                                  "waiting_calibration": "waiting_calibration_records",
                                  "waiting_manual": "waiting_manual_records"}
                if cfg.rule_gate and status in status_counter:
                    rule_hits = [item["type"] for item in evidence
                                 if isinstance(item, dict)
                                 and item.get("type") in cfg.rule_gate_kinds]
                    if rule_hits:
                        statistics[status_counter[status]] -= 1
                        status, fluent = "low_fluency", False
                        _bump(statistics, "low_fluency_records")
                        _bump(statistics, "rule_gate_flagged_records")

                output = _output_record(
                    record, metadata, cfg, backend, score, fluent, status,
                    model_version, grammar_errors, model_evidence + evidence, checked_at,
                    score_semantics, business_gate_approved, model_output_repaired,
                    rule_hits, defect_info)

                moved = False
                if status == "low_fluency" and cfg.low_fluency_policy == "move_to_rejects":
                    moved = True
                if status in {"model_unavailable", "waiting_manual", "model_error"} \
                        and cfg.degraded_policy == "move_to_rejects":
                    moved = True
                if moved:
                    _bump(statistics, "moved_to_rejects")
                else:
                    checked_writer.write(output)

                if status != "accepted":
                    # This is an audit entry unless the record was physically
                    # moved out of the checked output.
                    if not moved:
                        _bump(statistics, "audit_records")
                    reject_writer.write(_reject_entry(lineno, record, [status], output))
                    _append_reason(statistics, status)
                return True

            # 第二段：按块批量模型打分（适配器提供 <入口>_batch 时），再逐条裁决写出；
            # 整批失败回退逐条调用，无批量入口的评分器（含测试注入桩）同样逐条
            score_batch_size = 32  # 分块大小：8GB 显存安全余量
            use_batch = backend == "model" and hasattr(scorer, "score_batch")
            if backend == "model":
                log_action(logger, "打分", "批量打分（块大小 %d）" % score_batch_size
                           if use_batch else "逐条打分（评分器无批量入口）")
            for start in range(0, len(pending), score_batch_size):
                chunk = pending[start:start + score_batch_size]
                if backend != "model":
                    results = [None] * len(chunk)
                else:
                    results = None
                    if use_batch:
                        try:
                            results = scorer.score_batch(
                                [rec[cfg.text_field] for _, rec, _, _ in chunk], cfg.target_lang)
                        except Exception as exc:  # 整批失败回退逐条，链路韧性优先
                            log_action(logger, "警告",
                                       f"批量打分失败回退逐条：{type(exc).__name__}")
                            results = None
                    if results is None:
                        results = []
                        for _, rec, _, _ in chunk:
                            try:
                                results.append(scorer.score(rec[cfg.text_field], cfg.target_lang))
                            except Exception as exc:
                                results.append(exc)
                for (lineno, record, metadata, evidence), payload_result in zip(chunk, results):
                    if not _finalize(lineno, record, metadata, evidence, payload_result):
                        break
                if abort_message:
                    break
    except (OSError, UnicodeDecodeError) as exc:
        for path in (tmp_file, tmp_rej):
            try:
                path.unlink()
            except OSError:
                pass
        message = f"输入文件读取失败：{type(exc).__name__}"
        log_action(logger, "失败", message)
        # [无血缘改造 2026-10-19] 血缘输出已停用，保留原实现便于回溯；不再生成 lineage 文件。
        # append_lineage(lineage_file, make_lineage(
        #     OPERATOR_NAME, OPERATOR_VERSION, unique_id,
        #     str(input_file), str(out_file), "failed", started, now_iso(),
        #     cfg_snapshot, statistics))
        close_logger(logger)
        return {"status": "failed", "outputFile": None, "rejectFile": None,
                "stageZip": None, "storageUri": None, "statistics": statistics,
                "message": message}

    if abort_message:
        for path in (tmp_file, tmp_rej):
            try:
                path.unlink()
            except OSError:
                pass
        log_action(logger, "失败", abort_message)
        # [无血缘改造 2026-10-19] 血缘输出已停用，保留原实现便于回溯；不再生成 lineage 文件。
        # append_lineage(lineage_file, make_lineage(
        #     OPERATOR_NAME, OPERATOR_VERSION, unique_id,
        #     str(input_file), str(out_file), "failed", started, now_iso(),
        #     cfg_snapshot, statistics))
        close_logger(logger)
        return {"status": "failed", "outputFile": None, "rejectFile": None,
                "stageZip": None, "storageUri": None, "statistics": statistics,
                "message": abort_message}

    os.replace(tmp_file, out_file)
    os.replace(tmp_rej, rej_file)
    statistics["processed_records"] = statistics["input_records"]
    statistics["output_records"] = count_lines(out_file)
    # Keep legacy names for API compatibility. ``passed_records`` means
    # records physically retained in the output, while ``rejected_records``
    # remains the number of audit/reject-file lines.
    statistics["passed_records"] = statistics["output_records"]
    statistics["rejected_records"] = count_lines(rej_file)
    statistics["physical_reject_records"] = (
        statistics["hard_rejected_records"] + statistics["moved_to_rejects"]
    )

    if statistics["input_records"] == 0:
        status = "failed"
        message = "输入文件为空或无有效记录"
    elif statistics["hard_rejected_records"] > 0 or statistics["moved_to_rejects"] > 0:
        status = "partial"
        message = ""
    elif statistics["degraded_records"] > 0:
        status = "degraded"
        message = ""
    elif statistics["waiting_calibration_records"] > 0:
        status = "waiting_calibration"
        message = "模型分数尚未校准，未执行正式流畅度阈值判定"
    else:
        status = "success"
        message = ""

    log_action(logger, "处理", f"流畅度检测完成：输出保留 {statistics['output_records']} 条，"
                              f"接受 {statistics['accepted_records']} 条，"
                              f"硬异常 {statistics['hard_rejected_records']} 条，"
                              f"有分 {statistics['scored_records']} 条，"
                              f"低流畅 {statistics['low_fluency_records']} 条，"
                              f"降级 {statistics['degraded_records']} 条，"
                              f"模型错误 {statistics['model_error_records']} 条")
    log_action(logger, "输出", f"写入输出文件：{out_file.name}；异常清单：{rej_file.name}")
    # [无血缘改造 2026-10-19] 血缘输出已停用，保留原实现便于回溯；不再生成 lineage 文件。
    # append_lineage(lineage_file, make_lineage(
    #     OPERATOR_NAME, OPERATOR_VERSION, unique_id,
    #     str(input_file), str(out_file), status, started, now_iso(),
    #     cfg_snapshot, statistics))
    if status == "failed":
        # 空输入等失败场景不对外暴露可被下一算子消费的临时路径，也不生成阶段产物。
        log_action(logger, "完成", f"算子执行完成，状态 {status}")
        close_logger(logger)
        return {"status": status, "outputFile": None, "rejectFile": None,
                "stageZip": None, "storageUri": None, "statistics": statistics,
                "message": message}
    stage_zip, storage_uri = finalize_stage_artifacts(
        tdir, OPERATOR_ID, unique_id, out_file, rej_file, tdir / "run.log")
    log_action(logger, "归档",
               f"阶段产物：{Path(stage_zip).name if stage_zip else '无'}；存储地址：{storage_uri}")
    log_action(logger, "完成", f"算子执行完成，状态 {status}")
    close_logger(logger)
    return {"status": status, "outputFile": str(out_file), "rejectFile": str(rej_file),
            "stageZip": stage_zip, "storageUri": storage_uri,
            "statistics": statistics, "message": message}


def main():
    parser = argparse.ArgumentParser(description="算子15：译文流畅度检测")
    parser.add_argument("--input", required=True, help="输入句对 JSONL 文件（14_semantic_checked.jsonl）")
    parser.add_argument("--unique-id", default="local_demo_file1", help="任务id_文件id")
    parser.add_argument("--config", default=None, help="配置文件路径")
    parser.add_argument("--output-root", default=None, help="产物根目录")
    args = parser.parse_args()
    result = run(args.input, args.unique_id, args.output_root, args.config)
    print(f"status={result['status']} statistics={result['statistics']}")
    print(f"stageZip={result.get('stageZip')} storageUri={result.get('storageUri')}")
    if result["message"]:
        print(f"message={result['message']}")


if __name__ == "__main__":
    main()
