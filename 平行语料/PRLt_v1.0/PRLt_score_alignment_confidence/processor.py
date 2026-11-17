"""算子12：双语对齐置信度 —— 证据聚合、加权融合、低置信标记与 CLI。"""
import argparse
import codecs
import json
import os
import re
from pathlib import Path
from typing import Any, Dict, Iterator, List, Optional, Tuple

import yaml
from pydantic import ValidationError

from utils.artifacts import finalize_stage_artifacts
from utils.duplication import containment, count_units, max_repetition, shingles
from utils.io import JsonlWriter, task_dir
# [无血缘改造 2026-10-19] 血缘输出已停用，保留原导入便于回溯：
# from utils.lineage import append_lineage, make_lineage, now_iso
from utils.logger import close_logger, get_logger, log_action
from utils.schemas import OperatorConfig
from utils.scoring import (build_evidence_index, candidate_score,
                           fuse_components, pick_pair_id, resolve_components)
from utils.timeutil import now_iso

BASE_DIR = Path(__file__).resolve().parent
OPERATOR_NAME = "alignment_confidence"
OPERATOR_ID = "12"
OPERATOR_VERSION = "v1.0.0"


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


def _resolve_evidence(value: Optional[str], tdir: Path) -> Optional[Path]:
    """解析旁路证据文件路径（任务目录 / 算子目录 / 工作目录），找不到返回 None。"""
    if not value:
        return None
    raw = Path(value)
    candidates = [raw] if raw.is_absolute() else [tdir / raw, BASE_DIR / raw, Path.cwd() / raw]
    for candidate in candidates:
        if candidate.is_file():
            return candidate
    return None


def _base_statistics(encoding=None) -> Dict[str, Any]:
    return {
        "input_records": 0,
        "scored": 0,
        "rejected": 0,
        "hard_rejected": 0,
        "low_confidence": 0,
        "waiting_manual": 0,
        "conflicts": 0,
        "moved_to_rejects": 0,
        "evidence_incomplete": 0,
        "parent_guard_flagged": 0,
        "encoding": encoding,
    }


def _bump(statistics: Dict[str, Any], key: str) -> None:
    statistics[key] = statistics.get(key, 0) + 1


def _append_reason(statistics: Dict[str, Any], reason: str) -> None:
    _bump(statistics, f"reason:{reason}")


def _reject_entry(lineno: int, record: Optional[dict], reasons: List[str],
                  **extra) -> dict:
    entry: Dict[str, Any] = {"line": lineno, "id": None, "pair_id": None,
                             "source_ref": None, "target_ref": None,
                             "reasons": reasons}
    if isinstance(record, dict):
        meta = record.get("metadata") if isinstance(record.get("metadata"), dict) else {}
        entry["id"] = record.get("id")
        entry["pair_id"] = meta.get("parent_pair_id") or record.get("pair_id") or record.get("id")
        entry["source_ref"] = meta.get("source_ref") or record.get("source_ref")
        entry["target_ref"] = meta.get("target_ref") or record.get("target_ref")
        entry["source"] = record.get("source")
        entry["target"] = record.get("target")
    entry.update(extra)
    return entry


def build_parent_index(input_file: str, encoding: str, cfg: OperatorConfig) -> dict:
    """预扫一遍输入，按父句聚合结构观测：重复单位 + 跨语言长度比 + 上游复核标记。

    融合分是逐条算的，父句级的问题必须在父句层面才能看见；这里只保留聚合所需
    的文本与标记，不做打分，主流程仍然是流式的。
    """
    if not cfg.parent_guard:
        return {}
    grouped: Dict[str, Dict[str, Any]] = {}
    for line in iter_decoded_lines(input_file, encoding):
        if not line.strip():
            continue
        try:
            record = json.loads(line)
        except json.JSONDecodeError:
            continue
        if not isinstance(record, dict):
            continue
        meta = record.get("metadata") if isinstance(record.get("metadata"), dict) else {}
        parent_id = meta.get("parent_pair_id") or record.get("pair_id") or record.get("id")
        if not isinstance(parent_id, str) or not parent_id.strip():
            continue
        bucket = grouped.setdefault(parent_id, {"source": [], "target": [], "status": None,
                                               "flagged_by": None})
        bucket["source"].append(str(record.get("source") or ""))
        bucket["target"].append(str(record.get("target") or ""))
        status = meta.get("review_status")
        if isinstance(status, str) and status.strip():
            bucket["status"] = status
            bucket["flagged_by"] = meta.get("flagged_by")

    index: Dict[str, Dict[str, Any]] = {}
    for parent_id, bucket in grouped.items():
        source_text = "".join(bucket["source"])
        target_text = "".join(bucket["target"])
        source_units = count_units(source_text)
        target_units = count_units(target_text)
        ratio = (source_units / target_units) if target_units else None
        source_rep, src_i, src_j = max_repetition(bucket["source"], "zh")
        target_rep, tgt_i, tgt_j = max_repetition(bucket["target"], "en")
        index[parent_id] = {
            "units": {"source": source_units, "target": target_units},
            "ratio": ratio,
            "source_repetition": source_rep,
            "target_repetition": target_rep,
            "source_refs": [src_i, src_j],
            "target_refs": [tgt_i, tgt_j],
            "upstream_review_status": bucket["status"],
            "upstream_flagged_by": bucket["flagged_by"],
        }

    if cfg.sibling_duplicate_guard:
        _mark_sibling_duplicates(index, grouped, cfg)
    return index


PARENT_SUFFIX_RE = re.compile(r"-pair\d+$")


def _parent_target_texts(grouped: Dict[str, Dict[str, Any]]) -> Dict[str, str]:
    return {parent_id: "".join(bucket["target"]) for parent_id, bucket in grouped.items()}


def _mark_sibling_duplicates(index: Dict[str, Dict[str, Any]],
                             grouped: Dict[str, Dict[str, Any]],
                             cfg: OperatorConfig) -> None:
    """段内兄弟句对重复：同一段落的两条句对给出同一段译文。

    构造缺陷 `length_ratio` 里整段译文被复制一遍，op-09/10 把它拆成两条句对
    （各自 1-1 比例正常、父句内也无重复），只有把同一段落的兄弟 pair 放在一起
    比才能看见。实测自然语料 895 段（holdout 399 + flores 496）0 命中。
    """
    targets = _parent_target_texts(grouped)
    by_paragraph: Dict[str, List[str]] = {}
    for parent_id in index:
        by_paragraph.setdefault(PARENT_SUFFIX_RE.sub("", parent_id), []).append(parent_id)
    for pairs in by_paragraph.values():
        if len(pairs) < 2:
            continue
        sets = {pid: shingles(targets.get(pid, ""), "en") for pid in pairs}
        for pid in pairs:
            if len(sets[pid]) < 3:
                continue
            for other in pairs:
                if other == pid or len(sets[other]) < 3:
                    continue
                score = containment(sets[pid], sets[other])
                if score >= cfg.repetition_containment:
                    index[pid]["sibling_duplicate"] = {
                        "pair_id": other,
                        "containment": round(score, 6),
                        "threshold": cfg.repetition_containment,
                    }
                    break


def parent_guard_hit(observation: Optional[dict], cfg: OperatorConfig) -> Optional[dict]:
    """父句级守卫判定；命中返回证据字典（写 metadata.parent_guard），否则 None。"""
    if not cfg.parent_guard or not observation:
        return None
    reasons: List[str] = []
    evidence: Dict[str, Any] = {}

    for side, key in (("source", "source_repetition"), ("target", "target_repetition")):
        score = observation.get(key) or 0.0
        if score >= cfg.repetition_containment:
            reasons.append("duplicate_unit")
            evidence["repetition"] = {"side": side, "containment": round(score, 6),
                                      "threshold": cfg.repetition_containment}
            break

    sibling = observation.get("sibling_duplicate")
    if sibling:
        reasons.append("duplicate_sibling_pair")
        evidence["sibling_duplicate"] = sibling

    ratio = observation.get("ratio")
    if isinstance(ratio, (int, float)) and not isinstance(ratio, bool):
        if ratio < cfg.parent_ratio_min or ratio > cfg.parent_ratio_max:
            reasons.append("length_ratio_abnormal")
            evidence["ratio"] = {"value": round(float(ratio), 6),
                                 "min": cfg.parent_ratio_min,
                                 "max": cfg.parent_ratio_max,
                                 "units": observation.get("units")}

    if not reasons:
        return None
    evidence["reasons"] = reasons
    evidence["cap"] = cfg.guard_confidence_cap
    evidence["upstream_review_status"] = observation.get("upstream_review_status")
    return evidence


def _validate_and_score(record: dict, cfg: OperatorConfig,
                        sentence_index: Optional[dict],
                        paragraph_index: Optional[dict],
                        parent_index: Optional[dict] = None
                        ) -> Tuple[Optional[dict], List[str]]:
    """校验单条记录并计算置信度；返回 (输出记录, 失败原因列表)。"""
    meta = record.get("metadata")
    if meta is None:
        meta = {}
    if not isinstance(meta, dict):
        return None, ["invalid_type:metadata"]

    for field in ("source", "target"):
        if field not in record:
            return None, [f"missing_field:{field}"]
        if not isinstance(record[field], str):
            return None, [f"invalid_type:{field}"]
        if not record[field].strip():
            return None, [f"empty_{field}"]

    if cfg.language_check:
        for field, expected in (("source_lang", cfg.source_lang),
                                ("target_lang", cfg.target_lang)):
            value = record.get(field)
            if value is not None and value != expected:
                return None, ["language_declaration_mismatch"]

    source_ref = meta.get("source_ref") or record.get("source_ref")
    target_ref = meta.get("target_ref") or record.get("target_ref")
    if cfg.require_refs:
        if not source_ref:
            return None, ["schema_error:missing_source_ref"]
        if not target_ref:
            return None, ["schema_error:missing_target_ref"]

    components, errors = resolve_components(record, cfg, sentence_index, paragraph_index)
    if errors:
        return None, errors
    if not components:
        return None, ["score_missing"]

    confidence, used_weights = fuse_components(components, cfg)
    if confidence is None:
        return None, ["score_missing"]

    candidates = meta.get("candidates")
    conflict_candidates: List[dict] = []
    tie = False
    if isinstance(candidates, list) and candidates:
        scored_candidates = []
        for index, candidate in enumerate(candidates):
            score = candidate_score(candidate, record, components, cfg)
            if score is None:
                continue
            source = candidate.get("source_ref") if isinstance(candidate, dict) else None
            target = candidate.get("target_ref") if isinstance(candidate, dict) else None
            scored_candidates.append({"index": index, "confidence": score,
                                      "source_ref": source, "target_ref": target})
        if scored_candidates:
            scored_candidates.sort(key=lambda item: item["confidence"], reverse=True)
            conflict_candidates = scored_candidates
            confidence = scored_candidates[0]["confidence"]
            if len(scored_candidates) >= 2:
                gap = scored_candidates[0]["confidence"] - scored_candidates[1]["confidence"]
                tie = gap <= cfg.ambiguity_margin

    # 父句级守卫：融合分只看得见单条子句对，看不见父句内的重复/长度失衡。
    # 命中即把本算子对这份对齐的背书收回（封顶），原因留证据；不删记录。
    fused_confidence = confidence
    parent_id = meta.get("parent_pair_id") if isinstance(meta.get("parent_pair_id"), str) else None
    guard_evidence = parent_guard_hit(
        (parent_index or {}).get(parent_id) if parent_id else None, cfg)
    if guard_evidence is not None:
        confidence = min(fused_confidence, cfg.guard_confidence_cap)

    upstream_status = meta.get("review_status")
    upstream_flagged = (isinstance(upstream_status, str)
                        and upstream_status.strip() == "needs_review")
    low_confidence = confidence < cfg.low_confidence_threshold
    if tie:
        review_status = "waiting_manual"
    elif guard_evidence is not None or upstream_flagged or confidence < cfg.review_threshold:
        review_status = "needs_review"
    else:
        review_status = "auto_pass"

    incomplete = bool(set(cfg.enabled_granularities) - set(components))
    append_reasons: List[str] = []
    if tie:
        append_reasons.append("conflict_multi_candidate")
    if low_confidence:
        append_reasons.append("low_confidence")
    if guard_evidence is not None:
        append_reasons.extend(guard_evidence["reasons"])

    pair_id = pick_pair_id(record, cfg.pair_id_fields) or meta.get("parent_pair_id")
    output = dict(record)
    output["pair_id"] = pair_id
    output["granularity"] = meta.get("granularity", "clause_or_phrase")
    output["source_ref"] = source_ref
    output["target_ref"] = target_ref
    output["component_scores"] = components
    output["confidence_components"] = components
    output["weights"] = dict(cfg.component_weights)
    output["used_weights"] = used_weights
    output["alignment_confidence"] = confidence
    output["fused_confidence"] = fused_confidence
    output["confidence_version"] = cfg.confidence_version
    output["conflict_candidates"] = conflict_candidates
    output["review_status"] = review_status
    # Mirror the evidence completeness flag at the record level as well as in
    # metadata. Downstream operators historically inspect both locations.
    output["evidence_incomplete"] = incomplete

    meta_out = dict(meta)
    meta_out["alignment_confidence"] = confidence
    meta_out["fused_confidence"] = fused_confidence
    meta_out["parent_guard"] = guard_evidence
    meta_out["upstream_review_status"] = upstream_status if upstream_flagged else None
    meta_out["confidence_version"] = cfg.confidence_version
    meta_out["review_status"] = review_status
    meta_out["low_confidence"] = low_confidence
    meta_out["evidence_incomplete"] = incomplete
    meta_out["component_scores"] = components
    meta_out["used_weights"] = used_weights
    output["metadata"] = meta_out

    output["_audit_reasons"] = append_reasons
    output["_low_confidence"] = low_confidence
    output["_tie"] = tie
    output["_components"] = sorted(components)
    output["_incomplete"] = incomplete
    return output, []


def run(input_file: str, unique_id: str, output_root=None,
        config_path=None, overrides: dict = None, logger=None) -> dict:
    output_root = Path(output_root) if output_root else Path(os.getenv("OUTPUT_DIR", BASE_DIR / "output"))
    tdir = task_dir(output_root, unique_id)
    out_file = tdir / "12_scored.jsonl"
    rej_file = tdir / "12_rejects.jsonl"
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

    statistics = _base_statistics()
    statistics["confidence_version"] = cfg.confidence_version
    statistics["low_confidence_threshold"] = cfg.low_confidence_threshold
    statistics["review_threshold"] = cfg.review_threshold
    statistics["enabled_granularities"] = list(cfg.enabled_granularities)
    statistics["weights"] = dict(cfg.component_weights)

    started = now_iso()
    log_action(logger, "输入", f"加载输入文件：{Path(input_file).name}")
    log_action(logger, "配置", f"粒度={cfg.enabled_granularities} 权重={cfg.component_weights} "
                              f"低置信阈值={cfg.low_confidence_threshold} 策略={cfg.low_confidence_policy}")

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
    sentence_index: Optional[dict] = None
    paragraph_index: Optional[dict] = None
    for component, configured in (("sentence", cfg.sentence_evidence_file),
                                  ("paragraph", cfg.paragraph_evidence_file)):
        resolved = _resolve_evidence(configured, tdir)
        if configured and resolved is None:
            log_action(logger, "警告", f"{component} 证据文件不存在，缺失分项不补分")
            continue
        if resolved is None:
            continue
        index = build_evidence_index(resolved, cfg.evidence_id_fields,
                                     cfg.evidence_confidence_fields)
        statistics[f"evidence_index_{component}"] = len(index)
        log_action(logger, "证据", f"{component} 证据索引条目：{len(index)}")
        if component == "sentence":
            sentence_index = index
        else:
            paragraph_index = index

    log_action(logger, "读取", f"解码输入文件：{encoding}")
    parent_index: dict = {}
    if cfg.parent_guard:
        try:
            parent_index = build_parent_index(input_file, encoding, cfg)
        except (OSError, UnicodeDecodeError) as exc:
            message = f"父句索引构建失败：{type(exc).__name__}"
            log_action(logger, "失败", message)
            close_logger(logger)
            return {"status": "failed", "outputFile": str(out_file), "rejectFile": str(rej_file),
                    "stageZip": None, "storageUri": None, "statistics": statistics,
                    "message": message}
        statistics["parent_index_entries"] = len(parent_index)
        log_action(logger, "证据", f"父句级守卫索引：{len(parent_index)} 个父句")
    try:
        with JsonlWriter(out_file) as scored_writer, JsonlWriter(rej_file) as reject_writer:
            for lineno, line in enumerate(iter_decoded_lines(input_file, encoding), 1):
                if not line.strip():
                    continue
                statistics["input_records"] += 1
                try:
                    record = json.loads(line)
                except json.JSONDecodeError:
                    reject_writer.write(_reject_entry(lineno, None, ["invalid_json"]))
                    statistics["hard_rejected"] += 1
                    _append_reason(statistics, "invalid_json")
                    continue
                if not isinstance(record, dict):
                    reject_writer.write(_reject_entry(lineno, None, ["invalid_record"]))
                    statistics["hard_rejected"] += 1
                    _append_reason(statistics, "invalid_record")
                    continue

                output, reasons = _validate_and_score(record, cfg,
                                                      sentence_index, paragraph_index,
                                                      parent_index)
                if reasons:
                    reject_writer.write(_reject_entry(lineno, record, reasons))
                    statistics["hard_rejected"] += 1
                    for reason in reasons:
                        _append_reason(statistics, reason)
                    continue

                audit_reasons = output.pop("_audit_reasons")
                low_confidence = output.pop("_low_confidence")
                tie = output.pop("_tie")
                available = output.pop("_components")
                incomplete = output.pop("_incomplete")

                for component in available:
                    _bump(statistics, f"component_available:{component}")
                if incomplete:
                    statistics["evidence_incomplete"] += 1
                if low_confidence:
                    statistics["low_confidence"] += 1
                if tie:
                    statistics["conflicts"] += 1
                    statistics["waiting_manual"] += 1
                guard = (output.get("metadata") or {}).get("parent_guard")
                if guard:
                    statistics["parent_guard_flagged"] = statistics.get("parent_guard_flagged", 0) + 1
                    for reason in guard.get("reasons", []):
                        _append_reason(statistics, f"guard:{reason}")

                moved = low_confidence and cfg.low_confidence_policy == "move_to_rejects"
                if not moved:
                    scored_writer.write(output)
                else:
                    statistics["moved_to_rejects"] += 1

                if audit_reasons:
                    reject_writer.write(_reject_entry(
                        lineno, record, audit_reasons,
                        confidence=output.get("alignment_confidence"),
                        component_scores=output.get("component_scores"),
                        review_status=output.get("review_status")))
                    for reason in audit_reasons:
                        _append_reason(statistics, reason)
    except (OSError, UnicodeDecodeError) as exc:
        message = f"输入文件读取失败：{type(exc).__name__}"
        log_action(logger, "失败", message)
        # [无血缘改造 2026-10-19] 血缘输出已停用，保留原实现便于回溯；不再生成 lineage 文件。
        # append_lineage(lineage_file, make_lineage(
        #     OPERATOR_NAME, OPERATOR_VERSION, unique_id,
        #     str(input_file), str(out_file), "failed", started, now_iso(),
        #     cfg_snapshot, statistics))
        close_logger(logger)
        return {"status": "failed", "outputFile": str(out_file), "rejectFile": str(rej_file),
                "stageZip": None, "storageUri": None, "statistics": statistics,
                "message": message}

    statistics["scored"] = scored_writer_count(out_file)
    statistics["rejected"] = scored_writer_count(rej_file)

    if statistics["input_records"] == 0:
        status = "failed"
        message = "输入文件为空或无有效记录"
    elif statistics["hard_rejected"] > 0 or statistics["moved_to_rejects"] > 0:
        status = "partial"
        message = ""
    elif statistics["low_confidence"] > 0 or statistics["conflicts"] > 0 \
            or statistics["evidence_incomplete"] > 0:
        status = "degraded"
        message = ""
    else:
        status = "success"
        message = ""

    log_action(logger, "处理", f"置信度融合完成：通过 {statistics['scored']} 条，"
                              f"硬异常 {statistics['hard_rejected']} 条，"
                              f"低置信 {statistics['low_confidence']} 条，"
                              f"冲突 {statistics['conflicts']} 条，"
                              f"证据不全 {statistics['evidence_incomplete']} 条")
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


def scored_writer_count(path) -> int:
    """统计 JSONL 文件行数（真实落盘行数，用于与 statistics 对齐）。"""
    count = 0
    with open(path, "r", encoding="utf-8") as stream:
        for line in stream:
            if line.strip():
                count += 1
    return count


def main():
    parser = argparse.ArgumentParser(description="算子12：双语对齐置信度")
    parser.add_argument("--input", required=True, help="输入子句/短语对齐 JSONL 文件")
    parser.add_argument("--unique-id", default="local_demo_file1", help="任务id_文件id")
    parser.add_argument("--config", default=None, help="配置文件路径")
    parser.add_argument("--output-root", default=None, help="产物根目录")
    args = parser.parse_args()
    result = run(args.input, args.unique_id, args.output_root, args.config)
    print(f"status={result['status']} statistics={result['statistics']}")
    print(f"stageZip={result.get('stageZip')} storageUri={result.get('storageUri')}")


if __name__ == "__main__":
    main()
