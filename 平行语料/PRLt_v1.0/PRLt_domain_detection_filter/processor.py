"""算子16：领域相关性校验（v1.3.0）—— 关键词相关性打分、阈值判定与 CLI。

处理边界：只读 `source`/`target` 判定与任务关键词的相关性并追加元数据，不改写句对原文。

v1.2.0（2026-11-03，需求 30 号）业务流程：任务通过输入框给出「；」分隔的关键词表 →
本地 Qwen3-Embedding-0.6B 计算句对与关键词的余弦、减去通用文本负样本余弦 →
`score = sigmoid((cos_pos - cos_neg) / T)` → `score >= relevance_threshold`（默认 0.60）
判为相关；低于阈值的记录**保留在主输出**并标记 `review_status=waiting_manual`，进入抽检台，
不再物理拒收。分值是 `logistic_cos_diff`，没有人工相关性 Gold，60% 是业务阈值不是准确率。

v1.3.0（2026-11-04）：打分粒度改为父句/段落聚合。10 号切出的短子句单独打分天然偏低，
实测把大量父句整体相关的记录误杀；现在同一 `parent_pair_id`（或段落）分组只对拼接后的父级
文本打一次分，再把分下发给组内记录，并输出 `relevance_scoring_unit=parent`、
`relevance_group_size`、`relevance_child_score` 供审计。阈值 0.60 保持不变。

`relevance_mode: legacy_domain` 可一键回滚到 v1.1.0 的固定领域分类；规则降级只产出真实
关键词命中证据，`relevance_score` / `domain_confidence` 恒为 None，绝不伪造分数。
"""
import argparse
import codecs
import json
import os
from pathlib import Path
from typing import Any, Dict, Iterator, List, Optional, Tuple

import yaml
from pydantic import ValidationError

from utils.domain import (BackendUnavailable, _parse_relevance_payload,  # noqa: F401
                          _parse_rule_result, normalize_language_pair,
                          resolve_backend, rule_candidates, rule_relevance_evidence)
from utils.artifacts import finalize_stage_artifacts
from utils.io import JsonlWriter, count_lines, task_dir
from utils.logger import close_logger, get_logger, log_action
from utils.relevance import (empty_histogram, join_parent_text, keyword_fingerprint,
                             parent_group_key, score_bucket)
from utils.schemas import OperatorConfig
from utils.timeutil import now_iso

# [无血缘改造 2026-10-19] 血缘输出已停用，保留原导入便于回溯：
# from utils.lineage import append_lineage, make_lineage, now_iso

BASE_DIR = Path(__file__).resolve().parent
OPERATOR_NAME = "domain_recognition"
OPERATOR_ID = "16"
OPERATOR_VERSION = "v1.3.0"
OUTPUT_FILE = "16_domain_checked.jsonl"
REJECT_FILE = "16_rejects.jsonl"
# 句对核心字段：领域判定必须基于两端文本，始终校验存在性。
PAIR_FIELDS = ("source", "target")
# 需要写入异常清单（审计）的状态；规则降级的已知/未知领域只保留在主输出。
AUDIT_STATUSES = {"low_confidence", "unknown_domain", "model_error", "model_unavailable"}
DEGRADED_STATUSES = {"rule_degraded", "model_error", "model_unavailable"}


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
        "passed_records": 0,
        "rejected": 0,
        "output_records": 0,
        "audit_records": 0,
        "physical_reject_records": 0,
        "schema_error": 0,
        "accepted": 0,
        "out_of_scope": 0,
        "low_confidence": 0,
        "degraded": 0,
        "unknown_domain": 0,
        "model_scored_records": 0,
        "model_error_records": 0,
        "waiting_manual": 0,
        "multi_label_records": 0,
        "moved_to_rejects": 0,
        "threshold": None,
        "backend": None,
        "model_version": None,
        "allowed_domains_count": None,
        "encoding": None,
        # ---- 关键词相关性（2026-11-03，需求 30 号）----
        "relevance_mode": None,
        "relevance_threshold": None,
        "relevance_temperature": None,
        "relevance_aggregate": None,
        "relevance_keywords_count": None,
        "relevance_keywords_sha256": None,
        "relevance_pass": 0,
        "relevance_fail": 0,
        "relevance_no_keywords": 0,
        "relevance_disabled": 0,
        "relevance_rule_degraded": 0,
        "relevance_model_error": 0,
        # ---- 父句/段落聚合（2026-11-04）----
        "relevance_parent_aggregation": None,
        "relevance_parent_policy": None,
        "relevance_parent_groups": 0,
        "relevance_parent_scored_units": 0,
        "relevance_parent_aggregated_records": 0,
        "relevance_child_scored_records": 0,
        "relevance_parent_fallback_records": 0,
        "document_inconsistent_documents": 0,
        "document_inconsistent_records": 0,
        # ---- 碎片守卫（2026-11-05）----
        "fragment_flagged_records": 0,
        "score_histogram": empty_histogram(),
        "top_keyword_counts": {},
    }


def _bump(statistics: Dict[str, Any], key: str) -> None:
    statistics[key] = statistics.get(key, 0) + 1


def _append_reason(statistics: Dict[str, Any], reason: str) -> None:
    _bump(statistics, f"reason:{reason}")


def _effective_threshold(cfg: OperatorConfig) -> float:
    """当前生效阈值：相关性模式用 relevance_threshold，legacy 模式用旧键。"""
    if cfg.relevance_mode == "keyword_relevance":
        return cfg.relevance_threshold
    return cfg.domain_confidence_threshold


def _resolve_language_pair(record: dict) -> Optional[str]:
    """从 language_pair 读取；缺省时回退 source_lang + target_lang。"""
    pair = normalize_language_pair(record.get("language_pair"))
    if pair is None:
        src = record.get("source_lang")
        tgt = record.get("target_lang")
        if isinstance(src, str) and isinstance(tgt, str) and src.strip() and tgt.strip():
            pair = normalize_language_pair(f"{src}-{tgt}")
    return pair


def _validate_structure(record: dict, cfg: OperatorConfig) -> Tuple[Optional[str], Optional[dict]]:
    """结构校验；返回 (失败原因, metadata)。原因非空表示硬异常。"""
    for field in PAIR_FIELDS:
        if field not in record:
            return f"missing_field:{field}", None
        value = record.get(field)
        if not isinstance(value, str):
            return f"invalid_type:{field}", None
        if not value.strip():
            return f"empty_{field}", None

    metadata = record.get("metadata")
    if metadata is None:
        metadata = {}
    if not isinstance(metadata, dict):
        return "invalid_type:metadata", None
    return None, metadata


def _reject_entry(lineno: int, record: Any, reasons: List[str],
                  output: Optional[dict] = None) -> dict:
    """异常/审计条目：只记录标识与判定字段，绝不落盘完整原文。"""
    entry: Dict[str, Any] = {
        "line": lineno,
        "id": None,
        "language_pair": None,
        "reasons": list(reasons),
    }
    if isinstance(record, dict):
        entry["id"] = record.get("id")
        entry["language_pair"] = record.get("language_pair")
    if output is not None:
        entry["domain"] = output.get("domain")
        entry["domain_status"] = output.get("domain_status")
        entry["domain_confidence"] = output.get("domain_confidence")
        entry["backend"] = output.get("backend")
        # Physical rejects are absent from the main output, so retain the
        # compact model decision evidence required to reproduce the gate.
        entry["model_version"] = output.get("model_version")
        entry["domain_threshold"] = output.get("domain_threshold")
        entry["review_status"] = output.get("review_status")
        entry["evidence"] = output.get("evidence") or []
        entry["candidates"] = output.get("candidates") or []
        # 相关性模式的分数与状态：物理拒收后主输出里没有这条，必须留在异常清单。
        entry["relevance_score"] = output.get("relevance_score")
        entry["relevance_status"] = output.get("relevance_status")
        entry["relevance_threshold"] = output.get("relevance_threshold")
        entry["relevance_scoring_unit"] = output.get("relevance_scoring_unit")
        entry["relevance_group_size"] = output.get("relevance_group_size")
        entry["matched_keywords"] = output.get("matched_keywords") or []
    return entry


def _output_record(record: dict, metadata: dict, cfg: OperatorConfig, result: dict,
                   status: str, review: str, reasons: List[str], allowed: List[str],
                   language_pair: Optional[str], checked_at: str,
                   relevance_status: Optional[str] = None,
                   relevance_ctx: Optional[dict] = None) -> dict:
    output = dict(record)
    upstream_fluency_model = output.get("fluency_model_version")
    if upstream_fluency_model is None:
        upstream_fluency_model = (metadata.get("fluency_model_version")
                                  if isinstance(metadata, dict) else None)
    if upstream_fluency_model is None:
        upstream_fluency_model = output.get("model_version")
    relevance = result.get("relevance") or {}
    score = relevance.get("relevance_score")
    output["domain"] = result["domain"]
    output["subdomain"] = result["subdomain"]
    output["domain_confidence"] = result["domain_confidence"]
    output["domain_status"] = status
    output["backend"] = result["backend"]
    output["model_version"] = result["model_version"]
    output["domain_model_version"] = result["model_version"]
    output["fluency_model_version"] = upstream_fluency_model
    output["evidence"] = result["evidence"]
    output["candidates"] = result["candidates"]
    output["allowed_domains"] = list(allowed)
    output["domain_reasons"] = list(reasons)
    output["domain_threshold"] = _effective_threshold(cfg)
    output["review_status"] = review
    output["language_pair"] = language_pair
    output["domain_checked_at"] = checked_at

    # ---- 关键词相关性：语义权威字段（2026-11-03，需求 30 号）----
    ctx = relevance_ctx or {}
    output["relevance_mode"] = cfg.relevance_mode
    output["relevance_status"] = relevance_status
    output["relevance_score"] = score
    output["relevance_threshold"] = ctx.get("threshold")
    output["relevance_temperature"] = ctx.get("temperature")
    output["relevance_aggregate"] = ctx.get("aggregate")
    output["relevance_score_type"] = relevance.get("relevance_score_type")
    output["relevance_calibration"] = relevance.get("relevance_calibration")
    output["relevance_backend"] = relevance.get("relevance_backend") or result["backend"]
    output["relevance_model_version"] = result["model_version"]
    output["relevance_keywords_count"] = ctx.get("keywords_count")
    output["relevance_keywords_sha256"] = ctx.get("keywords_sha256")
    output["matched_keywords"] = relevance.get("matched_keywords") or []
    output["cos_pos"] = relevance.get("cos_pos")
    output["cos_neg"] = relevance.get("cos_neg")
    # 打分粒度审计（2026-11-04）：parent=按父句/段落聚合打分，unit=逐条打分。
    output["relevance_scoring_unit"] = relevance.get("scoring_unit")
    output["relevance_group_size"] = relevance.get("group_size")
    output["relevance_parent_id"] = relevance.get("parent_id")
    output["relevance_child_score"] = relevance.get("child_score")
    # 抽检台（20 号）入队所需的三件套：原因、是否强制、是否阻断交付。
    output["trigger_reason"] = reasons[0] if review == "waiting_manual" and reasons else None
    output["review_requirement"] = "mandatory" if review == "waiting_manual" else "optional"
    output["delivery_blocking"] = status != "accepted"

    meta_out = dict(metadata)
    meta_out.update({
        "domain": result["domain"],
        "subdomain": result["subdomain"],
        "domain_confidence": result["domain_confidence"],
        "domain_status": status,
        "domain_backend": result["backend"],
        "model_version": result["model_version"],
        "domain_model_version": result["model_version"],
        "fluency_model_version": upstream_fluency_model,
        "review_status": review,
        "allowed_domains": list(allowed),
        "relevance_status": relevance_status,
        "relevance_score": score,
        "relevance_threshold": ctx.get("threshold"),
        "relevance_score_type": relevance.get("relevance_score_type"),
        "relevance_calibration": relevance.get("relevance_calibration"),
        "relevance_scoring_unit": relevance.get("scoring_unit"),
        "relevance_group_size": relevance.get("group_size"),
    })
    output["metadata"] = meta_out
    return output


def _build_parent_index(input_file: str, encoding: str,
                        cfg: OperatorConfig) -> Dict[str, Dict[str, Any]]:
    """预扫描输入，建立父级分组 → 聚合文本索引（2026-11-04）。

    子句级记录（10 号输出）单条只有几个字，与关键词的余弦天然偏低，逐条打分会把
    整体相关的父句误杀。这里按 ``relevance_parent_key_fields`` 分组，把同组 source/target
    拼接成父级文本，主流程只对父级打一次分，再把分下发给组内记录。
    索引只保存聚合文本，不保存整条记录，内存占用与分组数成正比。
    """
    index: Dict[str, Dict[str, Any]] = {}
    if not cfg.relevance_parent_aggregation:
        return index
    fields = cfg.relevance_parent_key_fields
    for line in iter_decoded_lines(input_file, encoding):
        if not line.strip():
            continue
        try:
            record = json.loads(line)
        except json.JSONDecodeError:
            continue
        if not isinstance(record, dict):
            continue
        key = parent_group_key(record, fields)
        source, target = record.get("source"), record.get("target")
        if not key or not isinstance(source, str) or not isinstance(target, str):
            continue
        entry = index.setdefault(key, {"source": [], "target": [], "units": 0})
        entry["source"].append(source)
        entry["target"].append(target)
        entry["units"] += 1
    for entry in index.values():
        entry["source_text"] = join_parent_text(entry.pop("source"), cfg.relevance_parent_max_chars)
        entry["target_text"] = join_parent_text(entry.pop("target"), cfg.relevance_parent_max_chars)
    return index


def _relevance_result_from_payload(payload: dict) -> dict:
    """把适配器返回的相关性 payload 转成主流程使用的领域结果结构（兼容 17/18 门禁键）。"""
    matched = payload.get("matched_keywords") or []
    return {
        "domain": matched[0]["keyword"] if matched else None,
        "subdomain": None,
        "domain_confidence": payload.get("relevance_score"),
        "candidates": [],
        "evidence": payload.get("evidence") or [],
        "model_version": payload.get("model_version"),
        "backend": "model",
        "relevance": payload,
    }


def _score_relevance(classifier, source: str, target: str, keywords: List[str],
                     cfg: OperatorConfig, language_pair: Optional[str],
                     scoring_unit: str, group_size: int,
                     parent_id: Optional[str]) -> dict:
    """调用适配器打分，并标注本次打分的粒度（父级/单条）。"""
    payload = _parse_relevance_payload(
        classifier.score_relevance(
            source, target, keywords,
            language_pair or cfg.expected_language_pair(),
            cfg.relevance_temperature, cfg.relevance_aggregate),
        getattr(classifier, "model_version", None))
    payload["scoring_unit"] = scoring_unit
    payload["group_size"] = group_size
    payload["parent_id"] = parent_id
    payload["child_score"] = None
    return payload


def _classify(record: dict, cfg: OperatorConfig, backend: str,
              classifier, language_pair: Optional[str],
              keywords: List[str], scoring: Optional[dict] = None) -> dict:
    """按后端判定领域/相关性；模型分支异常向上抛由调用方按 model_error 处理。

    ``scoring`` 为父级聚合上下文（2026-11-04）：
    ``{"parent_id", "source_text", "target_text", "units", "policy"}``。
    传入时按父级文本打分，子记录继承父级分；``policy=max`` 时再算自身分取大。
    """
    if cfg.relevance_mode == "keyword_relevance" and not keywords:
        # 没有关键词就不调用模型：按 empty_keyword_policy 决定"全部进抽检台"或"不筛选"，
        # 绝不把空词表当成"全部相关"。
        return {
            "domain": None,
            "subdomain": None,
            "domain_confidence": None,
            "candidates": [],
            "evidence": [],
            "model_version": getattr(classifier, "model_version", None),
            "backend": "model" if backend == "model" else "rule_degraded",
            "relevance": None,
        }
    if backend == "model" and cfg.relevance_mode == "keyword_relevance":
        if scoring:
            # 父级聚合：对整组拼接后的父句/段落打一次分（2026-11-04）。
            # policy=max 的自身分比较放在主循环做：父级结果要缓存给组内其他记录复用，
            # 不能把某一条子记录的自身分写进缓存。
            payload = _score_relevance(
                classifier, scoring["source_text"], scoring["target_text"], keywords,
                cfg, language_pair, "parent", scoring["units"], scoring["parent_id"])
        else:
            payload = _score_relevance(
                classifier, record.get("source", ""), record.get("target", ""),
                keywords, cfg, language_pair, "unit", 1, None)
        # 兼容门禁键：domain_confidence 映射为相关性分，domain 映射为主关键词。
        # 17/18 号仍按 domain_status / domain 工作，无需改代码。
        return _relevance_result_from_payload(payload)
    if backend == "model":
        payload = classifier.classify(
            record.get("source", ""), record.get("target", ""),
            language_pair or cfg.expected_language_pair())
        # 后端适配器（HTTP/本地）返回已校验结构；注入的测试替身可能省略可选键。
        return {
            "domain": payload["domain"],
            "subdomain": payload.get("subdomain"),
            "domain_confidence": payload["domain_confidence"],
            "candidates": payload.get("candidates") or [],
            "evidence": payload.get("evidence") or [],
            "model_version": payload.get("model_version"),
            "backend": "model",
        }
    fields = {field: record.get(field) for field in cfg.text_fields}
    if cfg.relevance_mode == "keyword_relevance":
        # 规则降级：只报真实词面命中，relevance_score 恒为 None（绝不伪造分数）。
        evidence = rule_relevance_evidence(fields, keywords)
        return {
            "domain": evidence[0]["keyword"] if evidence else None,
            "subdomain": None,
            "domain_confidence": None,
            "candidates": [],
            "evidence": evidence,
            "model_version": None,
            "backend": "rule_degraded",
            "relevance": {
                "relevance_score": None,
                "matched_keywords": [
                    {"keyword": item["keyword"], "cosine": None} for item in evidence
                ],
                "relevance_score_type": None,
                "relevance_calibration": None,
                "relevance_backend": "rule_degraded",
                "evidence": [],
            },
        }
    parsed = _parse_rule_result(rule_candidates(fields, cfg), cfg)
    parsed["backend"] = "rule_degraded"
    return parsed


def _fragment_hit(record: dict, cfg: OperatorConfig) -> Optional[dict]:
    """碎片守卫：源与目标**两侧都**去空白后 <= fragment_min_chars。返回证据或 None。

    只要求「任一侧很短」会误伤正常的短术语对（「增编」/Addendum、「更正」/Corrigendum）
    以及上游切分留下的单侧碎屑：自然语料 844 条子句对里 39 条（4.6%）会命中。
    两侧都短才是「不可判的碎片」——构造缺陷的 20 条 fragment 全是「银行」/bank（2/4 字），
    该口径在自然语料上 0 命中（2026-11-05 实测）。
    单侧碎屑由算子10 的 fragment_too_short 标记接力，不在本算子重复拦截。
    """
    if not cfg.fragment_guard:
        return None
    source_chars = len("".join(str(record.get("source") or "").split()))
    target_chars = len("".join(str(record.get("target") or "").split()))
    if source_chars > cfg.fragment_min_chars or target_chars > cfg.fragment_min_chars:
        return None
    return {"source_chars": source_chars, "target_chars": target_chars,
            "min_chars": cfg.fragment_min_chars}


def _decide_relevance(result: dict, cfg: OperatorConfig,
                      keywords: List[str]) -> Tuple[str, str, List[str], str]:
    """关键词相关性判定，返回 (domain_status, review_status, reasons, relevance_status)。

    ``domain_status`` 保持旧取值（accepted/low_confidence/unknown_domain/rule_degraded），
    让 17/18 号零改动；语义权威是 ``relevance_status``。
    """
    if not keywords:
        if cfg.empty_keyword_policy == "pass_all":
            return "accepted", "accepted", ["relevance_disabled"], "disabled"
        return "unknown_domain", "waiting_manual", ["no_keywords"], "no_keywords"
    if result["backend"] != "model":
        return "rule_degraded", "waiting_manual", ["rule_degraded"], "rule_degraded"

    relevance = result.get("relevance") or {}
    score = relevance.get("relevance_score")
    if score is None:
        # 模型返回了空分数：按模型错误处理，绝不当作通过。
        return "model_error", "waiting_manual", ["model_error"], "model_error"
    if score >= cfg.relevance_threshold:
        return "accepted", "accepted", [], "relevant"

    if cfg.low_relevance_action == "reject":
        return "low_confidence", "rejected", ["low_relevance"], "low_relevance"
    if cfg.low_relevance_action == "keep":
        return "accepted", "accepted", ["low_relevance_kept"], "low_relevance"
    return "low_confidence", "waiting_manual", ["low_relevance"], "low_relevance"


def _document_key(record: dict) -> Optional[str]:
    """文档键：记录 id 的 ``::`` 前缀（``<document>::<para>-<sentence>-...``）。

    全链路 id 都按这个约定拼接，段落/句子/子句都在同一份文档下；取不到就跳过，
    不猜测、不用文本相似度凑分组。
    """
    record_id = record.get("id")
    if not isinstance(record_id, str) or "::" not in record_id:
        return None
    document_id = record_id.split("::", 1)[0].strip()
    return document_id or None


def _apply_document_consistency(path: Path, statistics: Dict[str, Any],
                                cfg: OperatorConfig) -> None:
    """回扫输出文件：文档内低相关占比达标时，把剩下「通过」的单位改判待复核。

    模型分只看每条单位与关键词的相似度，看不见这份文档整体是否跑题：跑题文档里
    恰好命中关键词的那几条会拿到高分。判定用的是本算子自己已经算出的结论
    （relevance_status=low_relevance 的占比），不重新打分、不改分数。
    """
    rows: List[dict] = []
    for line in path.read_text(encoding="utf-8").splitlines():
        if line.strip():
            rows.append(json.loads(line))

    grouped: Dict[str, List[int]] = {}
    for index, record in enumerate(rows):
        key = _document_key(record)
        if key:
            grouped.setdefault(key, []).append(index)

    flagged_documents = 0
    flagged_records = 0
    for key, indexes in grouped.items():
        if len(indexes) < cfg.document_consistency_min_units:
            continue
        low = sum(1 for index in indexes
                  if rows[index].get("relevance_status") == "low_relevance")
        ratio = low / len(indexes)
        if ratio < cfg.document_consistency_min_low_ratio:
            continue
        marked = 0
        for index in indexes:
            record = rows[index]
            if record.get("relevance_status") != "relevant":
                continue
            record["relevance_status"] = "manual_review"
            record["domain_status"] = "low_confidence"
            record["review_status"] = "waiting_manual"
            record["trigger_reason"] = "document_topic_inconsistent"
            record["review_requirement"] = "mandatory"
            record["delivery_blocking"] = True
            reasons = record.get("domain_reasons")
            record["domain_reasons"] = list(reasons or []) + ["document_topic_inconsistent"]
            record["document_consistency"] = {
                "document_id": key, "units": len(indexes), "low_units": low,
                "low_ratio": round(ratio, 6),
                "min_low_ratio": cfg.document_consistency_min_low_ratio,
            }
            meta = record.get("metadata")
            if isinstance(meta, dict):
                meta["relevance_status"] = "manual_review"
                meta["domain_status"] = "low_confidence"
                meta["review_status"] = "waiting_manual"
            marked += 1
        if marked:
            flagged_documents += 1
            flagged_records += marked

    path.write_text("\n".join(json.dumps(record, ensure_ascii=False) for record in rows)
                    + "\n", encoding="utf-8")
    statistics["document_inconsistent_documents"] = flagged_documents
    statistics["document_inconsistent_records"] = flagged_records


def _decide(result: dict, cfg: OperatorConfig, allowed: List[str],
            keywords: Optional[List[str]] = None) -> Tuple[str, str, List[str], Optional[str]]:
    """返回 (domain_status, review_status, reasons, relevance_status)。

    相关性模式走 :func:`_decide_relevance`（relevance_status 非空）；
    legacy 模式保持 v1.1.0 行为（relevance_status 为 None），范围外优先级高于低置信。
    """
    keywords = keywords or []
    if cfg.relevance_mode == "keyword_relevance":
        return _decide_relevance(result, cfg, keywords)
    domain = result["domain"]
    if result["backend"] == "model" and domain in {"unknown", "unknown_domain"}:
        return "unknown_domain", "waiting_manual", ["unknown_domain"], None
    in_scope = cfg.allow_all_domains or (domain is not None and domain in allowed)

    if result["backend"] == "model":
        confidence = result["domain_confidence"]
        low = confidence is not None and confidence < cfg.domain_confidence_threshold
        reasons: List[str] = []
        if not in_scope:
            reasons.append("out_of_scope")
        if low:
            reasons.append("low_confidence")
        if len(result["candidates"]) > 1:
            reasons.append("multi_candidate")
        if not in_scope:
            return "out_of_scope", "rejected", reasons, None
        if low:
            return "low_confidence", "waiting_manual", reasons, None
        return "accepted", "accepted", reasons, None

    # 规则降级：无置信度可比，命中允许领域也只能待人工确认。
    if domain is None:
        return "unknown_domain", "waiting_manual", ["unknown_domain"], None
    if not in_scope:
        return "out_of_scope", "rejected", ["out_of_scope"], None
    reasons = ["rule_degraded"]
    if len(result["candidates"]) > 1:
        reasons.append("multi_candidate")
    return "rule_degraded", "waiting_manual", reasons, None


def run(input_file: str, unique_id: str, output_root=None,
        config_path=None, overrides: dict = None, logger=None,
        model_classifier=None) -> dict:
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
        allowed = cfg.normalized_allowed_domains()
        # 关键词表按输入框契约解析（「；」分隔）；超限/超长在配置阶段就报错。
        keywords = cfg.parsed_relevance_keywords()
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
    statistics["threshold"] = _effective_threshold(cfg)
    statistics["allowed_domains_count"] = len(allowed)
    relevance_ctx = {
        "threshold": cfg.relevance_threshold,
        "temperature": cfg.relevance_temperature,
        "aggregate": cfg.relevance_aggregate,
        "keywords_count": len(keywords),
        "keywords_sha256": keyword_fingerprint(keywords)[:16],
    }
    statistics.update({
        "relevance_mode": cfg.relevance_mode,
        "relevance_threshold": cfg.relevance_threshold,
        "relevance_temperature": cfg.relevance_temperature,
        "relevance_aggregate": cfg.relevance_aggregate,
        "relevance_keywords_count": len(keywords),
        "relevance_keywords_sha256": relevance_ctx["keywords_sha256"],
    })
    log_action(logger, "输入", f"加载输入文件：{Path(input_file).name}")
    log_action(logger, "配置", f"后端={cfg.backend} 方向={cfg.expected_language_pair()} "
                              f"模式={cfg.relevance_mode} 阈值={_effective_threshold(cfg)} "
                              f"关键词数={len(keywords)} 允许领域数={len(allowed)} "
                              f"降级策略={cfg.fallback_policy}")
    if cfg.relevance_mode == "keyword_relevance" and not keywords:
        log_action(logger, "警告",
                   f"未提供领域关键词，按 empty_keyword_policy={cfg.empty_keyword_policy} 处理")

    try:
        backend, classifier, degrade_reason = resolve_backend(cfg, model_classifier)
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
    statistics["model_version"] = getattr(classifier, "model_version", None) if backend == "model" else None
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

    # 父句/段落聚合（2026-11-04）：预扫描建立分组索引，主循环只按父级打一次分。
    parent_index: Dict[str, Dict[str, Any]] = {}
    parent_cache: Dict[str, dict] = {}
    statistics["relevance_parent_aggregation"] = cfg.relevance_parent_aggregation
    statistics["relevance_parent_policy"] = cfg.relevance_parent_policy
    if cfg.relevance_mode == "keyword_relevance" and cfg.relevance_parent_aggregation:
        try:
            parent_index = _build_parent_index(input_file, encoding, cfg)
        except (OSError, UnicodeDecodeError) as exc:
            message = f"父级分组预扫描失败：{type(exc).__name__}"
            log_action(logger, "失败", message)
            close_logger(logger)
            return {"status": "failed", "outputFile": None, "rejectFile": None,
                    "stageZip": None, "storageUri": None, "statistics": statistics,
                    "message": message}
        statistics["relevance_parent_groups"] = sum(
            1 for entry in parent_index.values()
            if entry["units"] >= cfg.relevance_parent_min_units)
        log_action(logger, "聚合",
                   f"父级分组 {len(parent_index)} 个，其中 {statistics['relevance_parent_groups']} 个"
                   f"（>= {cfg.relevance_parent_min_units} 条）按父级文本打分，策略={cfg.relevance_parent_policy}")

    checked_at = now_iso()
    model_errors = 0
    abort_message: Optional[str] = None
    log_action(logger, "读取", f"解码输入文件：{encoding}")
    try:
        with JsonlWriter(tmp_file) as checked_writer, JsonlWriter(tmp_rej) as reject_writer:
            for lineno, line in enumerate(iter_decoded_lines(input_file, encoding), 1):
                if not line.strip():
                    continue
                statistics["input_records"] += 1
                try:
                    record = json.loads(line)
                except json.JSONDecodeError:
                    reject_writer.write(_reject_entry(lineno, None, ["invalid_json"]))
                    _bump(statistics, "schema_error")
                    _bump(statistics, "physical_reject_records")
                    _append_reason(statistics, "invalid_json")
                    continue
                if not isinstance(record, dict):
                    reject_writer.write(_reject_entry(lineno, None, ["invalid_record"]))
                    _bump(statistics, "schema_error")
                    _bump(statistics, "physical_reject_records")
                    _append_reason(statistics, "invalid_record")
                    continue

                reason, metadata = _validate_structure(record, cfg)
                language_pair: Optional[str] = None
                if not reason and cfg.language_check:
                    language_pair = _resolve_language_pair(record)
                    if language_pair is None:
                        reason = "missing_language_pair"
                    elif language_pair != cfg.expected_language_pair():
                        reason = "language_pair_mismatch"
                if not reason and not cfg.language_check:
                    language_pair = _resolve_language_pair(record)
                if reason:
                    reject_writer.write(_reject_entry(lineno, record, [reason]))
                    _bump(statistics, "schema_error")
                    _bump(statistics, "physical_reject_records")
                    _append_reason(statistics, reason)
                    continue

                # 父级聚合：同一父级分组只打一次分，组内记录复用该结果。
                scoring: Optional[dict] = None
                parent_key: Optional[str] = None
                cached_result: Optional[dict] = None
                if parent_index:
                    parent_key = parent_group_key(record, cfg.relevance_parent_key_fields)
                    group = parent_index.get(parent_key) if parent_key else None
                    if group and group["units"] >= cfg.relevance_parent_min_units:
                        scoring = {
                            "parent_id": parent_key,
                            "source_text": group["source_text"],
                            "target_text": group["target_text"],
                            "units": group["units"],
                            "policy": cfg.relevance_parent_policy,
                        }
                        cached_result = parent_cache.get(parent_key)

                model_error = False
                if cached_result is not None:
                    result = dict(cached_result)
                else:
                    try:
                        result = _classify(record, cfg, backend, classifier, language_pair,
                                           keywords, scoring)
                    except Exception as exc:  # 单条模型异常按 model_error 处理，绝不伪造分数
                        if scoring:
                            # 父级文本打分失败时退回逐条打分，不让整个父句组一起判错。
                            _bump(statistics, "relevance_parent_fallback_records")
                            log_action(logger, "警告",
                                       f"第 {lineno} 条父级打分失败，退回逐条打分："
                                       f"{type(exc).__name__}")
                            scoring = None
                            try:
                                result = _classify(record, cfg, backend, classifier,
                                                   language_pair, keywords, None)
                            except Exception as fallback_exc:
                                model_error = True
                                model_errors += 1
                                log_action(logger, "警告",
                                           f"第 {lineno} 条模型推理失败："
                                           f"{type(fallback_exc).__name__}")
                                result = {
                                    "domain": None, "subdomain": None,
                                    "domain_confidence": None, "candidates": [],
                                    "evidence": [],
                                    "model_version": getattr(classifier, "model_version", None),
                                    "backend": "model", "relevance": None,
                                }
                        else:
                            model_error = True
                            model_errors += 1
                            log_action(logger, "警告",
                                       f"第 {lineno} 条模型推理失败：{type(exc).__name__}")
                            result = {
                                "domain": None, "subdomain": None,
                                "domain_confidence": None, "candidates": [],
                                "evidence": [],
                                "model_version": getattr(classifier, "model_version", None),
                                "backend": "model", "relevance": None,
                            }
                    else:
                        if scoring and result.get("relevance"):
                            parent_cache[parent_key] = result
                            _bump(statistics, "relevance_parent_scored_units")
                if model_errors > cfg.max_model_errors:
                    abort_message = f"模型错误超过上限 {cfg.max_model_errors}，整次任务失败"
                    break

                # policy=max：父级分之外再算自身分取大；父级结果已缓存，子记录复制后再改。
                if (scoring and not model_error and cfg.relevance_parent_policy == "max"
                        and result.get("relevance")):
                    try:
                        child = _score_relevance(
                            classifier, record.get("source", ""), record.get("target", ""),
                            keywords, cfg, language_pair, "unit", 1, parent_key)
                    except Exception as exc:
                        _bump(statistics, "relevance_parent_fallback_records")
                        log_action(logger, "警告",
                                   f"第 {lineno} 条自身分打分失败，沿用父级分："
                                   f"{type(exc).__name__}")
                    else:
                        parent_relevance = result["relevance"]
                        child_score = child.get("relevance_score")
                        _bump(statistics, "relevance_child_scored_records")
                        if (child_score or 0.0) > (parent_relevance.get("relevance_score") or 0.0):
                            child["child_score"] = child_score
                            result = _relevance_result_from_payload(child)
                        else:
                            result = dict(result)
                            result["relevance"] = dict(parent_relevance)
                            result["relevance"]["child_score"] = child_score

                relevance_status: Optional[str] = None
                fragment = _fragment_hit(record, cfg)
                if fragment is not None:
                    # 碎片不做相关性判定：关键词分对「银行 / bank」没有意义，
                    # 硬判会给出一个看似正常的分数，把「无法判定」伪装成「已判定」。
                    status, review = "low_confidence", "waiting_manual"
                    reasons = ["fragment_too_short"]
                    relevance_status = "low_relevance"
                    _bump(statistics, "fragment_flagged_records")
                    _bump(statistics, "low_confidence")
                elif model_error:
                    status, review, reasons = "model_error", "waiting_manual", ["model_error"]
                    relevance_status = "model_error"
                    _bump(statistics, "model_error_records")
                    _bump(statistics, "degraded")
                else:
                    status, review, reasons, relevance_status = _decide(
                        result, cfg, allowed, keywords)
                    if status == "accepted":
                        _bump(statistics, "accepted")
                    elif status == "out_of_scope":
                        _bump(statistics, "out_of_scope")
                    elif status == "low_confidence":
                        _bump(statistics, "low_confidence")
                    elif status == "unknown_domain":
                        _bump(statistics, "unknown_domain")
                        # Open-set model abstention is a valid model decision,
                        # not a backend degradation. Rule-only unknowns remain
                        # degraded because no model evidence exists.
                        if result["backend"] != "model":
                            _bump(statistics, "degraded")
                    elif status == "rule_degraded":
                        _bump(statistics, "degraded")
                    if result["backend"] == "model" and not model_error:
                        _bump(statistics, "model_scored_records")
                if len(result["candidates"]) > 1:
                    _bump(statistics, "multi_label_records")
                if review == "waiting_manual":
                    _bump(statistics, "waiting_manual")

                # 相关性统计：分档直方图 + 主关键词计数（只统计模型真实给分的记录）。
                if cfg.relevance_mode == "keyword_relevance":
                    counter = {
                        "relevant": "relevance_pass",
                        "low_relevance": "relevance_fail",
                        "no_keywords": "relevance_no_keywords",
                        "disabled": "relevance_disabled",
                        "rule_degraded": "relevance_rule_degraded",
                        "model_error": "relevance_model_error",
                    }.get(relevance_status or "unknown")
                    if counter:
                        _bump(statistics, counter)
                    relevance = result.get("relevance") or {}
                    score = relevance.get("relevance_score")
                    statistics["score_histogram"][score_bucket(score)] = \
                        statistics["score_histogram"].get(score_bucket(score), 0) + 1
                    # 打分粒度审计：纳入父级分组（组内 >=2 条）的记录与"父级+自身取大"的自身分记录。
                    # 自身分计数在 policy=max 分支内完成（每条只算一次），这里不重复累加。
                    if (relevance.get("group_size") or 1) > 1:
                        _bump(statistics, "relevance_parent_aggregated_records")
                    matched = relevance.get("matched_keywords") or []
                    if relevance_status == "relevant" and matched:
                        top = matched[0]["keyword"]
                        counts = statistics["top_keyword_counts"]
                        counts[top] = counts.get(top, 0) + 1

                output = _output_record(
                    record, metadata, cfg, result, status, review, reasons,
                    allowed, language_pair, checked_at,
                    relevance_status=relevance_status, relevance_ctx=relevance_ctx)

                moved = False
                if cfg.relevance_mode == "keyword_relevance":
                    # 低相关去向完全由 low_relevance_action 决定（默认进抽检台）。
                    if relevance_status == "low_relevance" and cfg.low_relevance_action == "reject":
                        moved = True
                elif status == "low_confidence" and cfg.low_confidence_policy == "move_to_rejects":
                    moved = True
                if status in {"rule_degraded", "unknown_domain", "model_error", "model_unavailable"} \
                        and cfg.degraded_policy == "move_to_rejects":
                    moved = True

                if status == "out_of_scope" or moved:
                    _bump(statistics, "physical_reject_records")
                    if moved:
                        _bump(statistics, "moved_to_rejects")
                    reject_writer.write(_reject_entry(lineno, record, reasons, output))
                    _append_reason(statistics, reasons[0] if reasons else status)
                else:
                    checked_writer.write(output)
                    if status in AUDIT_STATUSES:
                        reject_writer.write(_reject_entry(lineno, record, reasons, output))
                        _bump(statistics, "audit_records")
                        _append_reason(statistics, status)
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

    if abort_message or statistics["input_records"] == 0:
        for path in (tmp_file, tmp_rej):
            try:
                path.unlink()
            except OSError:
                pass
        message = abort_message or "输入文件为空或无有效记录"
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

    # 文档主题一致性（2026-11-05）：逐条打分看不见文档环境，必须在整批写完后回扫一次。
    # 只在存在低相关单位、且占比达到阈值的文档里改判——自然文档低相关占比为 0 时不触发。
    if cfg.document_consistency_guard and cfg.relevance_mode == "keyword_relevance":
        try:
            _apply_document_consistency(tmp_file, statistics, cfg)
        except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
            log_action(logger, "警告",
                       f"文档主题一致性回扫失败，保留逐条判定结果：{type(exc).__name__}")

    os.replace(tmp_file, out_file)
    os.replace(tmp_rej, rej_file)
    statistics["passed_records"] = count_lines(out_file)
    statistics["rejected"] = count_lines(rej_file)
    statistics["output_records"] = statistics["passed_records"]
    statistics["top_keyword_counts"] = dict(sorted(
        statistics["top_keyword_counts"].items(), key=lambda item: (-item[1], item[0]))[:10])

    if statistics["schema_error"] > 0 or statistics["out_of_scope"] > 0:
        status = "partial"
        message = ""
    elif statistics["degraded"] > 0:
        status = "degraded"
        message = ""
    elif statistics["waiting_manual"] > 0:
        status = "waiting_manual"
        message = ""
    else:
        status = "success"
        message = ""

    log_action(logger, "处理", f"领域识别完成：通过 {statistics['passed_records']} 条，"
                              f"接受 {statistics['accepted']} 条，范围外 {statistics['out_of_scope']} 条，"
                              f"低置信 {statistics['low_confidence']} 条，降级 {statistics['degraded']} 条，"
                              f"结构异常 {statistics['schema_error']} 条")
    if cfg.relevance_mode == "keyword_relevance":
        log_action(logger, "相关性",
                   f"模式={cfg.relevance_mode} 阈值={cfg.relevance_threshold} "
                   f"关键词={len(keywords)} 相关 {statistics['relevance_pass']} 条，"
                   f"低相关 {statistics['relevance_fail']} 条，"
                   f"无关键词 {statistics['relevance_no_keywords']} 条，"
                   f"规则降级 {statistics['relevance_rule_degraded']} 条，"
                   f"模型错误 {statistics['relevance_model_error']} 条")
        log_action(logger, "聚合",
                   f"父级分组 {statistics['relevance_parent_groups']} 个，"
                   f"按父级打分 {statistics['relevance_parent_scored_units']} 组 / "
                   f"覆盖 {statistics['relevance_parent_aggregated_records']} 条记录，"
                   f"父级打分失败退回逐条 {statistics['relevance_parent_fallback_records']} 条")
    log_action(logger, "输出", f"写入输出文件：{out_file.name}；异常清单：{rej_file.name}")
    # [无血缘改造 2026-10-19] 不再追加血缘；改为打包本阶段产物并登记对象存储地址。
    # append_lineage(lineage_file, make_lineage(
    #     OPERATOR_NAME, OPERATOR_VERSION, unique_id,
    #     str(input_file), str(out_file), status, started, now_iso(),
    #     cfg_snapshot, statistics))
    stage_zip, storage_uri = finalize_stage_artifacts(
        tdir, OPERATOR_ID, unique_id, out_file, rej_file, tdir / "run.log")
    log_action(logger, "归档", f"阶段产物：{Path(stage_zip).name if stage_zip else '无'}；存储地址：{storage_uri}")
    log_action(logger, "完成", f"算子执行完成，状态 {status}")
    close_logger(logger)
    return {"status": status, "outputFile": str(out_file), "rejectFile": str(rej_file),
            "stageZip": stage_zip, "storageUri": storage_uri,
            "statistics": statistics, "message": message}


def main():
    parser = argparse.ArgumentParser(description="算子16：领域相关性校验")
    parser.add_argument("--input", required=True, help="输入句对 JSONL 文件（15_fluency_checked.jsonl）")
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
