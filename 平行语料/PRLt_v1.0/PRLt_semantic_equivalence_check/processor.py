"""算子14：语义等价性检测 —— 核心处理逻辑和 CLI。

职责：对已通过对齐分级（S/A 或人工确认合格的 C）的双语句对计算语义等价性，
识别漏译、错译、增译和语义偏差，输出可解释的质量筛选结果。

不负责：段落/句子/子句对齐（09/10/11）、对齐置信度融合（12）、S/A/C 分级（13）、
译文流畅度（15）、领域识别（16）。语言检测只作为"语言方向"规则证据，
真实语言检测属算子6 职责。

模型边界（technical_design §1/§2）：模型后端只负责双语语义相似度；
不可用时按 `fallback_policy` 选择 `rule_degraded` 降级或 `fail` 结构化失败，
`semantic_score` 写 `null`，只用语言方向/长度比例/数字一致性等真实规则证据，
绝不伪造模型分数。
"""
import argparse
import codecs
import json
import os
from pathlib import Path
from typing import Any, Dict, Iterator, List, Optional, Tuple

import yaml
from pydantic import ValidationError

from utils.artifacts import finalize_stage_artifacts
from utils.evidence import (build_labels, build_rule_evidence,
                            guard_incomplete_target, guard_negation_mismatch,
                            guard_number_mismatch, guard_unit_currency_mismatch,
                            normalize_equivalent_units)
from utils.io import JsonlWriter, task_dir
# [无血缘改造 2026-10-19] 血缘输出已停用，保留原导入便于回溯：
# from utils.lineage import append_lineage, make_lineage, now_iso
from utils.logger import close_logger, get_logger, log_action
from utils.schemas import OperatorConfig
from utils.semantic import BackendUnavailable, semantic_available
from utils.timeutil import now_iso

BASE_DIR = Path(__file__).resolve().parent
OPERATOR_NAME = "semantic_equivalence"
OPERATOR_ID = "14"
OPERATOR_VERSION = "v1.0.0"

OUTPUT_NAME = "14_semantic_checked.jsonl"
REJECT_NAME = "14_rejects.jsonl"


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


def resolve_backend(cfg: OperatorConfig, injected: bool = False
                    ) -> Tuple[Optional[str], bool, Optional[str], Optional[str]]:
    """解析实际语义后端，返回 (后端, 是否降级, 降级原因, 失败原因)。

    - `rule_degraded`：显式规则后端，状态标记 degraded，语义分恒为 null。
    - `model`：要求模型；依赖不可用按 fallback_policy 决定降级或结构化失败。
    - `auto`：优先模型，不可用时同样按 fallback_policy 处理。
    注入后端（测试/联调）时按 model 处理，不重复做可用性判定。
    """
    if injected:
        return "model", False, None, None
    if cfg.model_backend == "rule_degraded":
        return "rule_degraded", True, "rule_degraded_requested", None
    if cfg.model_service_url or semantic_available():
        return "model", False, None, None
    if cfg.fallback_policy == "rule_degraded":
        return "rule_degraded", True, "model_unavailable", None
    return None, False, None, "model_unavailable"


def classify_score(score: Optional[float], threshold: float) -> str:
    """真实语义分与阈值比较；没有真实分数时归入 waiting_quality，不参与阈值判定。"""
    if score is None:
        return "waiting_quality"
    return "accepted" if float(score) >= float(threshold) else "rejected"


def iter_decoded_lines(path, encoding: str, chunk_size: int = 65536) -> Iterator[str]:
    """增量解码文件并逐行产出，避免把大文件整体读入内存。"""
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
    """选出第一个可以严格解码整个文件的编码。"""
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


# --------------------------------------------------------------------------- #
# 记录级处理
# --------------------------------------------------------------------------- #

def _declared(record: dict, field: str, fallback: str) -> str:
    value = record.get(field)
    return value.strip() if isinstance(value, str) and value.strip() else fallback


def _grade(record: dict, cfg: OperatorConfig) -> Optional[str]:
    """等级字段优先顶层，其次 metadata；返回大写规范化值。"""
    metadata = record.get("metadata") if isinstance(record.get("metadata"), dict) else {}
    for field in cfg.grade_fields:
        for source in (record, metadata):
            value = source.get(field)
            if isinstance(value, str) and value.strip():
                return value.strip().upper()
    return None


def _manual_confirmed(record: dict, cfg: OperatorConfig) -> bool:
    metadata = record.get("metadata") if isinstance(record.get("metadata"), dict) else {}
    for field in cfg.manual_confirmed_fields:
        for source in (record, metadata):
            value = source.get(field)
            if value is True:
                return True
            if isinstance(value, str) and value.strip().lower() in {"true", "yes", "1"}:
                return True
    return False


def _parent_key(record: dict, cfg: OperatorConfig) -> Optional[str]:
    """取父句标识（算子10 写入的 metadata.parent_pair_id）。

    取不到就返回 None，该条退回子句级判定——不猜父句、不跨记录拼接。
    """
    metadata = record.get("metadata")
    if not isinstance(metadata, dict):
        return None
    for field in cfg.parent_fields:
        value = metadata.get(field)
        if isinstance(value, str) and value.strip():
            return value.strip()
    return None


def _prepare_record(record: dict, cfg: OperatorConfig) -> Dict[str, Any]:
    """结构/准入校验 + 声明语言 + 规则证据（不含模型分与阈值判定）。

    抛 `ValueError` 表示结构或准入失败（进异常清单）。
    """
    metadata = record.get("metadata")
    if metadata is None:
        metadata = {}
        record["metadata"] = metadata
    elif not isinstance(metadata, dict):
        raise ValueError("invalid_type:metadata")

    record_id = record.get("id")
    if not isinstance(record_id, str) or not record_id.strip():
        raise ValueError("missing_field:id")
    record_id = record_id.strip()

    for field in cfg.text_fields:
        if field not in record:
            raise ValueError("missing_field:" + field)
        if not isinstance(record[field], str):
            raise ValueError("invalid_type:" + field)

    source = record["source"].strip()
    target = record["target"].strip()
    if not source:
        raise ValueError("empty_source")
    if not target:
        raise ValueError("empty_target")

    allowed_grades = {value.strip().upper() for value in cfg.allowed_grades}
    grade = _grade(record, cfg)
    if grade is None:
        if not cfg.allow_missing_grade:
            raise ValueError("missing_grade")
    elif grade not in allowed_grades and not (
            cfg.allow_manual_confirmed and _manual_confirmed(record, cfg)):
        raise ValueError("grade_not_eligible")

    declared_source = _declared(record, "source_lang", cfg.source_lang)
    declared_target = _declared(record, "target_lang", cfg.target_lang)
    if cfg.language_check and (declared_source != cfg.source_lang
                               or declared_target != cfg.target_lang):
        raise ValueError("language_declaration_mismatch")

    evidence = build_rule_evidence(source, target, cfg, declared_source, declared_target)

    return {
        "id": record_id,
        "source": source,
        "target": target,
        "grade": grade,
        "declared_source_lang": declared_source,
        "declared_target_lang": declared_target,
        "evidence": evidence,
    }


def _score(scorer, left: str, right: str) -> float:
    """调用一次模型打分并校验返回类型；失败抛 `BackendUnavailable`，绝不补默认分。"""
    if scorer is None:
        raise BackendUnavailable("scorer_missing")
    values = scorer.similarity([left], [right])
    if not values:
        raise BackendUnavailable("empty_score")
    value = values[0]
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise BackendUnavailable("invalid_score_type")
    return float(value)


def _norm_chars(text: str) -> int:
    """去空白后的字符数。短单位豁免按这个口径算，中文全角空格也不计入。"""
    return len("".join(str(text or "").split()))


def _decide_record(prepared: Dict[str, Any], semantic_score: Optional[float],
                   cfg: OperatorConfig, scoring_unit: str, group_size: int,
                   unit_source_chars: int, unit_texts: Tuple[str, str],
                   model_version: Optional[str], judge=None) -> Dict[str, Any]:
    """把（可能来自父句的）语义分落到单条记录：证据 → 标签 → 阈值判定 → 规则守卫。

    短单位（判定单元源文去空白后 <= short_unit_max_chars）改用更宽松的
    short_unit_threshold，其余单位仍用 threshold；实际用的阈值一并写进记录。
    `unit_texts` 是判定单元自己的（源文, 译文）：父句级是整句拼接，子句级就是本子句。
    `judge` 是 P2 语义判别器（Qwen3.5-2B）；只在语义分与全部规则守卫都放行后才调用。
    """
    evidence = prepared["evidence"]
    short_unit = (cfg.short_unit_max_chars > 0
                  and unit_source_chars <= cfg.short_unit_max_chars)
    threshold_applied = cfg.short_unit_threshold if short_unit else cfg.threshold
    if semantic_score is not None:
        evidence["model"] = {
            "score": round(semantic_score, 6),
            "model_version": model_version,
            "scoring_unit": scoring_unit,
            "group_size": group_size,
            "unit_source_chars": unit_source_chars,
            "short_unit": short_unit,
            "threshold_applied": threshold_applied,
        }

    # 标签证据按「本子句自己的原文/译文」算，父句级判定也保留子句粒度的定位信息
    labels = build_labels(prepared["source"], prepared["target"], evidence,
                          semantic_score, threshold_applied, cfg)
    quality_status = classify_score(semantic_score, threshold_applied)

    # 数字守卫（2026-11-03）：三条规则命中任一即拦截。
    #   missing 源有译无的 >=N 位数字（含 19xx/20xx 年份）；
    #   count   同一数字源文出现次数多于译文（漏掉重复出现的年份/编号）；
    #   swap    两边数字序列同长、恰好一位不同（"2 月"→"9 February" 这类小数字替换）。
    # 嵌入分对这种局部错误几乎不敏感（改数字 12 条全部漏过），规则证据是唯一可靠的信号；
    # 纯缺失口径下 1–2 位小数字仍只打标签，swap 规则只在序列形状完全对齐时才判。
    # 按整个判定单元（父句级=父句拼句）比较，不按子句：同一父句的数字分散在不同子句时，
    # 逐子句比较会把两边都算成"缺失"，在真实语料上产生大量假拦截。
    # 等价单位换算（3 年 ↔ 36 months、5 kg ↔ 5000 g）是正确的翻译：先按换算把译文的
    # 数量归一回源文数值，再比数字，避免数字守卫把正确换算判成 swap 假拦截。
    # 数值不等价的（3 年 ↔ 36 years）不改写，仍由数字守卫拦截。
    guard_target, equivalent_pairs = (
        normalize_equivalent_units(unit_texts[0], unit_texts[1],
                                   cfg.unit_guard_relative_tolerance)
        if cfg.unit_currency_guard else (unit_texts[1], []))
    guard = (guard_number_mismatch(unit_texts[0], guard_target,
                                   cfg.number_guard_min_digits)
             if cfg.number_guard else {})
    guard_hit = bool(guard.get("rules"))
    # 单位/币种守卫（2026-11-04）：数字相同但单位不同（3 年 → 3 months）
    # 或币种不同（5000 美元 → 5000 euros）是嵌入分与数字守卫都拦不住的局部错误。
    # 只比较数值相同的成对数量，数字不同仍由上面的 number_guard 负责，不重复报。
    unit_guard = (guard_unit_currency_mismatch(
        unit_texts[0], unit_texts[1], cfg.unit_guard_relative_tolerance)
        if cfg.unit_currency_guard else {})
    unit_guard_hit = bool(unit_guard.get("rules"))
    # 否定守卫（2026-11-04）：译文有否定、原文没有即拦截。只对 zh→en 生效——
    # 反方向的中文否定常被译成肯定式（自然语料误报 17.5%），只在 zh 源文这一侧可靠。
    negation_guard = (guard_negation_mismatch(unit_texts[0], unit_texts[1],
                                              cfg.negation_guard_min_chars)
                      if cfg.negation_guard and cfg.source_lang == "zh" else {})
    negation_guard_hit = bool(negation_guard.get("rules"))
    # 不完整译文守卫（2026-11-05）：原文完整、译文中途断开 => 漏译。
    # 整句余弦对「只译了前半句」不敏感（前半句译对就撑住相似度），规则是唯一可靠信号。
    incomplete_guard = (guard_incomplete_target(unit_texts[0], unit_texts[1],
                                                cfg.incomplete_target_min_chars,
                                                check_dangling_tail=cfg.incomplete_target_dangling_tail)
                        if cfg.incomplete_target_guard else {})
    incomplete_hit = bool(incomplete_guard.get("rules"))
    if quality_status == "waiting_quality" and cfg.degraded_policy == "reject":
        quality_status = "rejected"
        reasons = ["semantic_score_missing"]
    elif quality_status == "rejected":
        reasons = ["semantic_not_equivalent"]
    else:
        reasons = []
    if guard_hit and quality_status == "accepted":
        quality_status = "rejected"
        reasons = ["number_mismatch"]
    elif guard_hit:
        # 已经因语义分被拦的记录，把客观证据也记上，便于人工复核时排序
        reasons.append("number_mismatch")
    if unit_guard_hit:
        # 单位/币种是独立观测，即使已因数字被拦也保留原因
        for rule in unit_guard["rules"]:
            if rule not in reasons:
                reasons.append(rule)
        if quality_status == "accepted":
            quality_status = "rejected"
    if negation_guard_hit:
        # 否定翻转同样是独立观测：命中即拦，并给出译文里多出来的否定词供人工复核
        for rule in negation_guard["rules"]:
            if rule not in reasons:
                reasons.append(rule)
        if quality_status == "accepted":
            quality_status = "rejected"
        labels.append({"label": "mistranslation", "evidence": {
            "type": "negation_added",
            "rules": negation_guard["rules"],
            "target_negations": negation_guard.get("target_negations", []),
            "source_negated_clauses": negation_guard.get("source_negated_clauses"),
            "target_negated_clauses": negation_guard.get("target_negated_clauses"),
        }})
    if incomplete_hit:
        # 漏译同样是独立观测：原文完整、译文断开，即使已经因别的规则被拦也保留原因
        for rule in incomplete_guard["rules"]:
            if rule not in reasons:
                reasons.append(rule)
        if quality_status == "accepted":
            quality_status = "rejected"
        labels.append({"label": "omission", "evidence": {
            "type": "incomplete_translation",
            "source_chars": incomplete_guard.get("source_chars"),
            "target_complete": incomplete_guard.get("target_complete"),
        }})

    # P2 语义判别器（2026-11-05）：只在本条记录已经放行时调用，避免为会被拦下的
    # 记录白付一次 2B 推理。判别结果原样写进 metadata；不可用时 status 记录原因。
    judge_result: Dict[str, Any] = {"rules": [], "status": "disabled"}
    if judge is not None and quality_status == "accepted" and not incomplete_hit:
        judge_result = judge.judge(unit_texts[0], unit_texts[1])
        if judge_result.get("rules"):
            for rule in judge_result["rules"]:
                if rule not in reasons:
                    reasons.append(rule)
            quality_status = "rejected"
            labels.append({"label": "mistranslation", "evidence": {
                "type": "entity_substitution",
                "suspects": judge_result.get("suspects", []),
                "judge_confidence": judge_result.get("confidence"),
                "model_version": judge_result.get("model_version"),
            }})

    return {
        "id": prepared["id"],
        "grade": prepared["grade"],
        "declared_source_lang": prepared["declared_source_lang"],
        "declared_target_lang": prepared["declared_target_lang"],
        "quality_status": quality_status,
        "semantic_score": semantic_score,
        "semantic_scoring_unit": scoring_unit,
        "semantic_group_size": group_size,
        "semantic_short_unit": short_unit,
        "semantic_threshold_applied": threshold_applied,
        "semantic_unit_chars": unit_source_chars,
        "number_guard_missing": guard.get("missing", []),
        "number_guard_count": guard.get("count", []),
        "number_guard_swap": guard.get("swap", []),
        "number_guard_rules": guard.get("rules", []),
        "unit_guard_rules": unit_guard.get("rules", []),
        "unit_guard_unit_mismatches": unit_guard.get("unit_mismatches", []),
        "unit_guard_currency_mismatches": unit_guard.get("currency_mismatches", []),
        "unit_guard_equivalents": equivalent_pairs,
        "negation_guard_rules": negation_guard.get("rules", []),
        "negation_guard_target_negations": negation_guard.get("target_negations", []),
        "negation_guard_source_negations": negation_guard.get("source_negations", []),
        "negation_guard_source_clauses": negation_guard.get("source_negated_clauses", 0),
        "negation_guard_target_clauses": negation_guard.get("target_negated_clauses", 0),
        "incomplete_guard_rules": incomplete_guard.get("rules", []),
        "incomplete_guard_source_complete": incomplete_guard.get("source_complete"),
        "incomplete_guard_target_complete": incomplete_guard.get("target_complete"),
        "incomplete_guard_source_chars": incomplete_guard.get("source_chars"),
        # 悬空虚词：译文最后一段以 and / of / the 这类词收尾时为该词，否则 None
        "incomplete_guard_dangling_tail": incomplete_guard.get("dangling_tail"),
        "judge_rules": judge_result.get("rules", []),
        "judge_status": judge_result.get("status"),
        "judge_suspects": judge_result.get("suspects", []),
        "judge_confidence": judge_result.get("confidence"),
        "evidence": evidence,
        "labels": labels,
        "reasons": reasons,
        "degraded": semantic_score is None,
    }


def _build_output_record(record: dict, decision: Dict[str, Any], cfg: OperatorConfig,
                         backend: str, model_version: Optional[str]) -> dict:
    metadata = record["metadata"]
    direction = decision["evidence"]["language_direction"]
    metadata["grade"] = decision["grade"]
    metadata["backend"] = backend
    metadata["score_semantics"] = "model" if backend == "model" else "rule_only"
    metadata["model_name"] = cfg.model_name if backend == "model" else None
    metadata["model_version"] = model_version if backend == "model" else None
    metadata["threshold"] = cfg.threshold
    # 短单位豁免：threshold 始终写配置里的主阈值，实际生效的阈值单独记，
    # 下游据此区分"按 0.65 通过"和"按 0.50 通过的短单位"
    metadata["semantic_threshold_applied"] = decision.get("semantic_threshold_applied", cfg.threshold)
    metadata["semantic_short_unit"] = decision.get("semantic_short_unit", False)
    metadata["semantic_unit_chars"] = decision.get("semantic_unit_chars")
    # 数字守卫：rules 命中的规则名 + 各规则的证据，人工复核时直接看这几项
    metadata["semantic_number_guard_rules"] = decision.get("number_guard_rules") or []
    metadata["semantic_number_guard_missing"] = decision.get("number_guard_missing") or []
    metadata["semantic_number_guard_count"] = decision.get("number_guard_count") or []
    metadata["semantic_number_guard_swap"] = decision.get("number_guard_swap") or []
    # 单位/币种守卫：同数字换单位/币种（3 年→3 months、5000 美元→5000 euros）
    metadata["semantic_unit_guard_rules"] = decision.get("unit_guard_rules") or []
    metadata["semantic_unit_guard_unit_mismatches"] = \
        decision.get("unit_guard_unit_mismatches") or []
    metadata["semantic_unit_guard_currency_mismatches"] = \
        decision.get("unit_guard_currency_mismatches") or []
    # 等价换算（3 年↔36 months）：已按单位换算折回源文数值，数字守卫据此放行
    metadata["semantic_unit_guard_equivalents"] = decision.get("unit_guard_equivalents") or []
    # 否定守卫：rules 命中的规则名 + 译文里多出来的否定词（人工复核直接看这两项）
    metadata["semantic_negation_guard_rules"] = decision.get("negation_guard_rules") or []
    metadata["semantic_negation_guard_target_negations"] = \
        decision.get("negation_guard_target_negations") or []
    metadata["semantic_negation_guard_source_negations"] = \
        decision.get("negation_guard_source_negations") or []
    metadata["semantic_negation_guard_source_clauses"] = \
        decision.get("negation_guard_source_clauses")
    metadata["semantic_negation_guard_target_clauses"] = \
        decision.get("negation_guard_target_clauses")
    # 不完整译文守卫：rules 命中的规则名 + 原文/译文是否各有句末收尾（人工复核直接看这两项）
    metadata["semantic_incomplete_guard_rules"] = decision.get("incomplete_guard_rules") or []
    metadata["semantic_incomplete_guard_source_complete"] = \
        decision.get("incomplete_guard_source_complete")
    metadata["semantic_incomplete_guard_target_complete"] = \
        decision.get("incomplete_guard_target_complete")
    metadata["semantic_incomplete_guard_source_chars"] = \
        decision.get("incomplete_guard_source_chars")
    metadata["semantic_incomplete_guard_dangling_tail"] = \
        decision.get("incomplete_guard_dangling_tail")
    # 语义判别器（P2）：status=judged/disabled/unavailable/error/skipped_short，
    # rules/suspects 是模型报出的、原文没有的专名（已按专名形状过滤）
    metadata["semantic_judge_rules"] = decision.get("judge_rules") or []
    metadata["semantic_judge_status"] = decision.get("judge_status")
    metadata["semantic_judge_suspects"] = decision.get("judge_suspects") or []
    metadata["semantic_judge_confidence"] = decision.get("judge_confidence")
    metadata["quality_status"] = decision["quality_status"]
    metadata["semantic_score"] = decision["semantic_score"]
    # 判定单元：parent=该分是整个父句的分（同句多子句共享）；clause=本子句自己的分
    metadata["semantic_scoring_unit"] = decision.get("semantic_scoring_unit", "clause")
    metadata["semantic_group_size"] = decision.get("semantic_group_size", 1)
    metadata["semantic_evidence"] = decision["evidence"]
    metadata["problem_labels"] = decision["labels"]
    metadata["degraded"] = decision["degraded"]
    metadata["detected_source_lang"] = direction["detected_source_lang"]
    metadata["detected_target_lang"] = direction["detected_target_lang"]
    metadata["language_detection_backend"] = direction["language_detection_backend"]
    metadata["language_detection_confidence"] = direction["language_detection_confidence"]
    metadata["language_detection_reason"] = direction["language_detection_reason"]
    return record


def _mark_non_equivalent(record: dict, decision: Dict[str, Any], cfg: OperatorConfig) -> dict:
    """mark_and_keep：判定非等价的记录留在输出流里，只打标记、不删除。

    规范 4.14 只授予本算子「判语义等价」的职责；放行/拒付的最终决定权在算子 17 的
    quality_status / delivery_eligible 门禁。物理删除会让下游 15/16/17 完全看不到这条
    缺陷（基准实测 70 条缺陷段落里 61 条被删在 14 号），闭环断裂。
    标记口径（下游据此继续判定，不依赖「记录是否存在」）：
      - quality_status=rejected / semantic_status=not_equivalent：算子 17 的语义状态门禁；
      - review_status=rejected：算子 16 的 _merge_review_status 会原样保留，不会被改名放行；
      - delivery_eligible=false：算子 17 respect_delivery_eligible 直接拒付；
      - semantic_reject_reasons：本次判定原因码，人工复核直接看这一项。
    """
    reasons = list(decision.get("reasons") or [])
    record["quality_status"] = "rejected"
    record["semantic_status"] = "not_equivalent"
    record["review_status"] = "rejected"
    record["delivery_eligible"] = False
    record["semantic_reject_reasons"] = reasons
    metadata = record.get("metadata")
    if isinstance(metadata, dict):
        metadata["quality_status"] = "rejected"
        metadata["semantic_status"] = "not_equivalent"
        metadata["review_status"] = "rejected"
        metadata["delivery_eligible"] = False
        metadata["semantic_reject_reasons"] = reasons
        metadata["semantic_reject_policy"] = "mark_and_keep"
        metadata["semantic_reject_original_review_status"] = metadata.get(
            "upstream_review_status") or record.get("upstream_review_status")
    return record


def _reject_entry(lineno: int, record, reasons: List[str], cfg: OperatorConfig,
                  backend: str, model_version: Optional[str],
                  decision: Optional[Dict[str, Any]] = None,
                  kept_in_output: bool = False) -> Dict[str, Any]:
    record = record if isinstance(record, dict) else {}
    return {
        "line": lineno,
        "id": (decision or {}).get("id") or record.get("id"),
        "source": record.get("source"),
        "target": record.get("target"),
        "reasons": list(reasons),
        # mark_and_keep 时异常清单只是审计条目，记录仍在主输出里
        "kept_in_output": kept_in_output,
        "quality_status": "rejected" if decision is None else decision["quality_status"],
        "backend": backend,
        "model_version": model_version,
        "semantic_score": None if decision is None else decision["semantic_score"],
        "semantic_scoring_unit": None if decision is None else decision.get("semantic_scoring_unit"),
        "semantic_group_size": None if decision is None else decision.get("semantic_group_size"),
        "semantic_short_unit": None if decision is None else decision.get("semantic_short_unit"),
        "semantic_threshold_applied": ((cfg.threshold if cfg else None) if decision is None
                                       else decision.get("semantic_threshold_applied")),
        "semantic_unit_chars": None if decision is None else decision.get("semantic_unit_chars"),
        "semantic_number_guard_rules": None if decision is None else decision.get("number_guard_rules"),
        "semantic_number_guard_missing": None if decision is None else decision.get("number_guard_missing"),
        "semantic_number_guard_count": None if decision is None else decision.get("number_guard_count"),
        "semantic_number_guard_swap": None if decision is None else decision.get("number_guard_swap"),
        "semantic_unit_guard_rules": None if decision is None else decision.get("unit_guard_rules"),
        "semantic_unit_guard_unit_mismatches": (
            None if decision is None else decision.get("unit_guard_unit_mismatches")),
        "semantic_unit_guard_currency_mismatches": (
            None if decision is None else decision.get("unit_guard_currency_mismatches")),
        "semantic_unit_guard_equivalents": (
            None if decision is None else decision.get("unit_guard_equivalents")),
        "semantic_negation_guard_rules": (
            None if decision is None else decision.get("negation_guard_rules")),
        "semantic_negation_guard_target_negations": (
            None if decision is None else decision.get("negation_guard_target_negations")),
        "semantic_negation_guard_source_negations": (
            None if decision is None else decision.get("negation_guard_source_negations")),
        "semantic_negation_guard_source_clauses": (
            None if decision is None else decision.get("negation_guard_source_clauses")),
        "semantic_negation_guard_target_clauses": (
            None if decision is None else decision.get("negation_guard_target_clauses")),
        "semantic_incomplete_guard_rules": (
            None if decision is None else decision.get("incomplete_guard_rules")),
        "semantic_incomplete_guard_source_complete": (
            None if decision is None else decision.get("incomplete_guard_source_complete")),
        "semantic_incomplete_guard_target_complete": (
            None if decision is None else decision.get("incomplete_guard_target_complete")),
        "semantic_incomplete_guard_source_chars": (
            None if decision is None else decision.get("incomplete_guard_source_chars")),
        "semantic_incomplete_guard_dangling_tail": (
            None if decision is None else decision.get("incomplete_guard_dangling_tail")),
        "semantic_judge_rules": None if decision is None else decision.get("judge_rules"),
        "semantic_judge_status": None if decision is None else decision.get("judge_status"),
        "semantic_judge_suspects": None if decision is None else decision.get("judge_suspects"),
        "semantic_judge_confidence": None if decision is None else decision.get("judge_confidence"),
        "semantic_evidence": None if decision is None else decision["evidence"],
        "labels": [] if decision is None else decision["labels"],
        "threshold": cfg.threshold if cfg else None,
    }


def _process_group(members: List[Tuple[int, dict]], cfg: OperatorConfig, backend: str,
                   scorer, model_version: Optional[str], parent_key: Optional[str],
                   output_writer, reject_writer, logger,
                   statistics: Dict[str, Any], judge=None) -> None:
    """处理一个「评分单元」：父句级=同句多子句共享一个分数；子句级=单条一个分数。

    - 准入/结构失败逐条写异常清单，其余子句照常成组（不因一条坏了丢掉整句）；
    - 父句级模型失败时整组记 model_error（句级分不可分，不按子句重试、不补默认分）；
    - 子句级模型失败只影响该条，与原实现一致。

    抛 `BackendUnavailable` 表示模型错误已达上限，由上层按结构化失败收尾。
    """
    prepared: List[Tuple[int, dict, Dict[str, Any]]] = []
    for lineno, record in members:
        try:
            prepared.append((lineno, record, _prepare_record(record, cfg)))
        except (ValueError, TypeError) as exc:
            reason = str(exc)
            reject_writer.write(_reject_entry(lineno, record, [reason],
                                              cfg, backend, model_version))
            statistics["rejected"] += 1
            _append_reason(statistics, reason)

    if not prepared:
        return

    if parent_key is not None:
        scoring_unit, group_size = "parent", len(prepared)
        statistics["parent_groups"] += 1
        # 短单位和数字守卫都按「实际打分的那个单元」算：父句级=整句拼接后的文本
        unit_left = " ".join(item["source"] for _, _, item in prepared)
        unit_right = " ".join(item["target"] for _, _, item in prepared)
        unit_chars = [_norm_chars(unit_left)] * group_size
        # 守卫另用换行拼接：空格是千分位分隔符，A 子句结尾的 "123" 和 B 子句开头的 "456"
        # 一旦用空格相连就会被读成 123456，凭空造出一个不存在的数字。
        unit_texts = [("\n".join(item["source"] for _, _, item in prepared),
                       "\n".join(item["target"] for _, _, item in prepared))] * group_size
    else:
        scoring_unit, group_size = "clause", 1
        unit_chars = [_norm_chars(item["source"]) for _, _, item in prepared]
        unit_texts = [(item["source"], item["target"]) for _, _, item in prepared]

    scores: List[Optional[float]] = [None] * len(prepared)
    errored = set()

    if backend == "model":
        if scoring_unit == "parent":
            left = " ".join(item["source"] for _, _, item in prepared)
            right = " ".join(item["target"] for _, _, item in prepared)
            try:
                value = _score(scorer, left, right)
            except BackendUnavailable as exc:
                for index, (lineno, record, _) in enumerate(prepared):
                    statistics["model_error"] += 1
                    statistics["rejected"] += 1
                    _append_reason(statistics, "model_error")
                    reject_writer.write(_reject_entry(lineno, record, ["model_error"],
                                                      cfg, backend, model_version))
                log_action(logger, "警告",
                           f"父句 {parent_key} 模型调用失败：{str(exc).split(':')[0]}，"
                           f"该句 {len(prepared)} 条子句写入异常清单")
                if statistics["model_error"] >= cfg.max_model_errors:
                    raise
                return
            scores = [value] * group_size
            statistics["parent_scored"] += group_size
            statistics["scored_units"] += 1
        else:
            for index, (lineno, record, item) in enumerate(prepared):
                try:
                    scores[index] = _score(scorer, item["source"], item["target"])
                except BackendUnavailable as exc:
                    errored.add(index)
                    statistics["model_error"] += 1
                    statistics["rejected"] += 1
                    _append_reason(statistics, "model_error")
                    log_action(logger, "警告",
                               f"第 {lineno} 行模型调用失败：{str(exc).split(':')[0]}，写入异常清单")
                    reject_writer.write(_reject_entry(lineno, record, ["model_error"],
                                                      cfg, backend, model_version))
                    if statistics["model_error"] >= cfg.max_model_errors:
                        raise
                    continue
            statistics["scored_units"] += len(prepared) - len(errored)

    for index, (lineno, record, item) in enumerate(prepared):
        if index in errored:
            continue
        decision = _decide_record(item, scores[index], cfg, scoring_unit, group_size,
                                  unit_chars[index], unit_texts[index], model_version,
                                  judge=judge)
        if decision["semantic_short_unit"]:
            statistics["short_unit_records"] += 1
            score = decision["semantic_score"]
            # 按主阈值会被拦、按短单位阈值放行的记录数——这就是本次改动的直接效果
            if (score is not None and score < cfg.threshold
                    and decision["quality_status"] == "accepted"):
                statistics["short_unit_rescued"] += 1
        # 数字守卫单独拦下的条数（语义分本来是过的）——这是本次改动的直接效果
        if decision["reasons"][:1] == ["number_mismatch"]:
            statistics["number_guard_rejected"] += 1
            for rule in decision.get("number_guard_rules") or []:
                statistics["number_guard_rule_hits"][rule] += 1
        if decision.get("unit_guard_rules"):
            statistics["unit_guard_rejected"] += 1
            for rule in decision["unit_guard_rules"]:
                statistics["unit_guard_rule_hits"][rule] += 1
        # 否定守卫单独拦下的条数（语义分本来是过的）——这是本次改动的直接效果
        # [2026-11-05] 规则名不止 negation_added（还有 negation_clause_added /
        # negation_frame_added），计数按前缀匹配、命中表按需建键，避免 KeyError。
        if decision["reasons"][:1] and decision["reasons"][0].startswith("negation"):
            statistics["negation_guard_rejected"] += 1
            for rule in decision.get("negation_guard_rules") or []:
                hits = statistics["negation_guard_rule_hits"]
                hits[rule] = hits.get(rule, 0) + 1
        # 不完整译文守卫单独拦下的条数（语义分本来是过的）
        if decision["reasons"][:1] == ["incomplete_translation"]:
            statistics["incomplete_guard_rejected"] += 1
        # 语义判别器：调用次数与判拦条数分开记，便于区分"没启用"和"启用了但没拦住"
        judge_status = decision.get("judge_status")
        if judge is not None and judge_status is not None:
            statistics["judge_status"][judge_status] = \
                statistics["judge_status"].get(judge_status, 0) + 1
        if decision["reasons"][:1] == ["entity_not_in_source"]:
            statistics["judge_rejected"] += 1
        if decision["degraded"]:
            statistics["degraded"] += 1
        if decision["semantic_score"] is not None:
            statistics["model_scored"] += 1
        if decision["quality_status"] in {"accepted", "waiting_quality"}:
            output_writer.write(_build_output_record(record, decision, cfg, backend, model_version))
            statistics[decision["quality_status"]] += 1
        elif cfg.non_equivalent_policy == "mark_and_keep" and not decision["degraded"]:
            # 非等价记录留在输出流里打标记，异常清单只留审计条目（不删记录，见函数注释）。
            # 下游 15/16/17 因此仍能看到这条缺陷，最终拒付由 17 号的语义/交付门禁决定。
            # 降级记录（semantic_score 缺失）不走这条：它们的去向由 degraded_policy 决定，
            # 那是「没有分数可判」而不是「判为非等价」，degraded_policy=reject 时必须真删。
            output_writer.write(_mark_non_equivalent(
                _build_output_record(record, decision, cfg, backend, model_version),
                decision, cfg))
            reject_writer.write(_reject_entry(lineno, record, decision["reasons"],
                                              cfg, backend, model_version, decision,
                                              kept_in_output=True))
            statistics["kept_rejected"] += 1
            statistics["rejected"] += 1
            _append_reason(statistics, decision["reasons"][0])
        else:
            reject_writer.write(_reject_entry(lineno, record, decision["reasons"],
                                              cfg, backend, model_version, decision))
            statistics["rejected"] += 1
            statistics["rejected_dropped"] += 1
            _append_reason(statistics, decision["reasons"][0])


def _base_statistics(cfg: Optional[OperatorConfig], backend: Optional[str] = None) -> Dict[str, Any]:
    return {
        "input_records": 0,
        "model_scored": 0,
        "degraded": 0,
        "accepted": 0,
        # rejected=判为非等价的记录数（不论去向）；kept_rejected=留在输出流的条数；
        # rejected_dropped=只写异常清单、从输出流删除的条数（non_equivalent_policy=drop 时才有）
        "rejected": 0,
        "kept_rejected": 0,
        "rejected_dropped": 0,
        "waiting_quality": 0,
        "output_records": 0,
        "model_error": 0,
        # 判定单元：parent=同句多子句合成一条文本打一次分（句级判定）；
        # scored_units=实际模型调用次数（父句级=父句数，子句级=子句数）
        "scoring_unit": cfg.scoring_unit if cfg else None,
        "scored_units": 0,
        "parent_groups": 0,
        "parent_scored": 0,
        "threshold": cfg.threshold if cfg else None,
        # 短单位豁免：records=按短单位阈值判定的记录数，rescued=按主阈值会被拦、按短阈值放行的数
        "short_unit_records": 0,
        "short_unit_rescued": 0,
        # 数字守卫：语义分本来会通过、因数字问题被单独拦下的条数；
        # rule_hits 是按规则（missing/count/swap）拆开的条数，一条记录可命中多条规则
        "number_guard_rejected": 0,
        "number_guard_rule_hits": {"missing": 0, "count": 0, "swap": 0},
        # 单位/币种守卫：同数字换单位/币种的条数与规则拆分（unit/currency）
        "unit_guard_rejected": 0,
        "unit_guard_rule_hits": {"unit_mismatch": 0, "currency_mismatch": 0},
        # 否定守卫：语义分本来会通过、因"译文多出否定"被单独拦下的条数
        "negation_guard_rejected": 0,
        "negation_guard_rule_hits": {"negation_added": 0, "negation_clause_added": 0,
                                     "negation_frame_added": 0},
        # 不完整译文守卫：语义分本来会通过、因原文完整而译文断开被单独拦下的条数
        "incomplete_guard_rejected": 0,
        # 语义判别器：judge_status 按状态计数（judged/error/unavailable/skipped_short），
        # judge_rejected=判别器单独拦下的条数（语义分与全部规则守卫都放行之后）
        "judge_rejected": 0,
        "judge_status": {},
        "backend": backend,
        "model_version": None,
        "degraded_policy": cfg.degraded_policy if cfg else None,
        "encoding": None,
    }


def _append_reason(statistics: Dict[str, Any], reason: str) -> None:
    key = f"reason:{reason}"
    statistics[key] = statistics.get(key, 0) + 1


def _remove(paths) -> None:
    for path in paths:
        try:
            Path(path).unlink()
        except OSError:
            pass


# --------------------------------------------------------------------------- #
# 主流程
# --------------------------------------------------------------------------- #

def run(input_file: str, unique_id: str, output_root=None, config_path=None,
        overrides: dict = None, logger=None, semantic_backend=None) -> dict:
    output_root = Path(output_root) if output_root else Path(os.getenv("OUTPUT_DIR", BASE_DIR / "output"))
    tdir = task_dir(output_root, unique_id)
    out_file = tdir / OUTPUT_NAME
    rej_file = tdir / REJECT_NAME
    # [无血缘改造 2026-10-19] 不再生成 lineage 文件。
    # lineage_file = tdir / "lineage.json"
    logger = logger or get_logger(f"{OPERATOR_NAME}.{unique_id}", tdir / "run.log")
    started = now_iso()

    def failed(message: str, statistics: Dict[str, Any], cfg_snapshot: dict) -> dict:
        log_action(logger, "失败", message)
        # [无血缘改造 2026-10-19] 血缘输出已停用，保留原实现便于回溯；不再生成 lineage 文件。
        # append_lineage(lineage_file, make_lineage(
        #     OPERATOR_NAME, OPERATOR_VERSION, unique_id, str(input_file),
        #     str(out_file), "failed", started, now_iso(), cfg_snapshot, statistics))
        close_logger(logger)
        return {"status": "failed", "outputFile": None, "rejectFile": None,
                "stageZip": None, "storageUri": None, "statistics": statistics, "message": message}

    try:
        cfg, cfg_snapshot = load_config(config_path, overrides)
    except (AttributeError, OSError, TypeError, ValueError, ValidationError, yaml.YAMLError) as exc:
        message = f"配置错误：{type(exc).__name__}"
        return failed(message, _base_statistics(None), {"config_error": type(exc).__name__})

    log_action(logger, "输入", f"加载输入文件：{Path(input_file).name}")
    log_action(logger, "配置", "方向={}→{} 后端={} 模型={} 阈值={} 判定单元={} 短单位<={}字阈值={} "
                               "数字守卫={} 降级去向={} 非等价去向={}".format(
        cfg.source_lang, cfg.target_lang, cfg.model_backend, cfg.model_name,
        cfg.threshold, cfg.scoring_unit,
        cfg.short_unit_max_chars if cfg.short_unit_max_chars > 0 else "关闭",
        cfg.short_unit_threshold,
        (f"缺失/重复/{cfg.number_guard_min_digits}位数起+序列替换" if cfg.number_guard
         else "关闭"),
        cfg.degraded_policy, cfg.non_equivalent_policy))

    try:
        backend, degraded, degrade_reason, failure = resolve_backend(
            cfg, injected=semantic_backend is not None)
    except (TypeError, ValueError) as exc:
        return failed(f"后端解析错误：{type(exc).__name__}", _base_statistics(cfg), cfg_snapshot)

    if failure:
        message = (f"语义模型不可用（{failure}）且 fallback_policy=fail，"
                   "算子按结构化失败结束，不伪造模型分数")
        return failed(message, _base_statistics(cfg, backend=None), cfg_snapshot)

    scorer = semantic_backend
    if backend == "model" and scorer is None:
        from utils.semantic import EmbeddingSemanticScorer
        scorer = EmbeddingSemanticScorer(cfg.model_name, cfg.model_path, cfg.device, cfg.batch_size,
                                         cfg.model_service_url, cfg.model_timeout_seconds)
        try:
            scorer.load()
        except BackendUnavailable as exc:
            if cfg.fallback_policy == "rule_degraded":
                backend, degraded, scorer = "rule_degraded", True, None
                degrade_reason = "model_load_failed:" + str(exc).split(":")[0]
            else:
                message = (f"语义模型加载失败（{str(exc).split(':')[0]}）且 fallback_policy=fail，"
                           "算子按结构化失败结束，不伪造模型分数")
                return failed(message, _base_statistics(cfg, backend=None), cfg_snapshot)

    statistics = _base_statistics(cfg, backend=backend)
    statistics["degrade_reason"] = degrade_reason
    model_version = getattr(scorer, "model_version", None) if (backend == "model" and scorer is not None) else None
    statistics["model_version"] = model_version
    if degraded:
        log_action(logger, "警告",
                   f"语义模型降级：{degrade_reason}，改用语言方向+长度比例+数字一致性规则证据，语义分写 null")

    # P2 语义判别器（2026-11-05）：只在真模型后端且没有注入外部打分器时构造。
    # 注入 backend 的场景（单测/集成测试）不加载 4.3GB 权重；判别器自身不可用时
    # 只写 judge_status，不产生规则，不会伪造拦截。
    judge = None
    if cfg.semantic_judge and backend == "model" and semantic_backend is None:
        from utils.judge import EntityJudge
        # 相对路径按本算子目录解析（与 model_path 同一口径：../models/Qwen3.5-2B 指向
        # PRLt_handoff/models/），不按 cwd 解析——任务可能在任意目录下被调用。
        judge_path = Path(cfg.semantic_judge_model_path) if cfg.semantic_judge_model_path \
            else Path("../models/Qwen3.5-2B")
        if not judge_path.is_absolute():
            judge_path = (BASE_DIR / judge_path).resolve()
        judge = EntityJudge(judge_path, cfg.semantic_judge_min_chars)
        statistics["judge_model_path"] = str(judge_path)
        if judge.available():
            log_action(logger, "配置", f"语义判别器已启用：{judge_path.name}（懒加载）")
        else:
            log_action(logger, "警告", f"语义判别器权重缺失：{judge_path}，本条规则不生效")

    try:
        encoding = select_encoding(input_file, cfg.input_encodings)
    except (OSError, ValueError) as exc:
        return failed(f"输入文件编码处理失败：{type(exc).__name__}，本节点要求严格 UTF-8",
                      statistics, cfg_snapshot)

    statistics["encoding"] = encoding
    log_action(logger, "读取", f"解码输入文件：{encoding}")

    out_tmp = out_file.parent / (out_file.name + ".tmp")
    rej_tmp = rej_file.parent / (rej_file.name + ".tmp")

    # 父句级判定只在模型后端生效：同 parent_pair_id 的连续记录攒成一组，拼成整句打一次分。
    # 规则降级没有分数，分组无意义，按单条处理即可。
    grouping = backend == "model" and cfg.scoring_unit == "parent"
    pending: List[Tuple[int, dict]] = []
    holder: Dict[str, Optional[str]] = {"key": None}

    try:
        with JsonlWriter(out_tmp) as output_writer, JsonlWriter(rej_tmp) as reject_writer:
            def flush() -> None:
                if not pending:
                    return
                try:
                    _process_group(pending, cfg, backend, scorer, model_version, holder["key"],
                                   output_writer, reject_writer, logger, statistics, judge)
                finally:
                    pending.clear()
                    holder["key"] = None

            for lineno, line in enumerate(iter_decoded_lines(input_file, encoding), 1):
                if not line.strip():
                    continue
                statistics["input_records"] += 1
                try:
                    record = json.loads(line)
                except json.JSONDecodeError:
                    flush()
                    reject_writer.write(_reject_entry(lineno, None, ["invalid_json"],
                                                      cfg, backend, model_version))
                    statistics["rejected"] += 1
                    _append_reason(statistics, "invalid_json")
                    continue
                if not isinstance(record, dict):
                    flush()
                    reject_writer.write(_reject_entry(lineno, None, ["invalid_record"],
                                                      cfg, backend, model_version))
                    statistics["rejected"] += 1
                    _append_reason(statistics, "invalid_record")
                    continue

                key = _parent_key(record, cfg) if grouping else None
                if key is None:
                    # 没有父句标识（或配置子句级）：单条自成一组，等价于原逐条行为
                    flush()
                    _process_group([(lineno, record)], cfg, backend, scorer, model_version,
                                   None, output_writer, reject_writer, logger, statistics, judge)
                    continue
                if pending and key != holder["key"]:
                    flush()
                holder["key"] = key
                pending.append((lineno, record))

            flush()
        os.replace(out_tmp, out_file)
        os.replace(rej_tmp, rej_file)
    except BackendUnavailable as exc:
        _remove((out_tmp, rej_tmp))
        message = (f"语义模型错误超过上限（{str(exc).split(':')[0]}），"
                   "算子按结构化失败结束，不伪造模型分数")
        return failed(message, statistics, cfg_snapshot)
    except (OSError, UnicodeDecodeError) as exc:
        _remove((out_tmp, rej_tmp))
        return failed(f"输入文件读取失败：{type(exc).__name__}", statistics, cfg_snapshot)

    # mark_and_keep：非等价记录也在输出流里，输出行数 = 放行 + 待复核 + 标记后保留
    statistics["output_records"] = (statistics["accepted"] + statistics["waiting_quality"]
                                    + statistics["kept_rejected"])

    if statistics["input_records"] == 0:
        _remove((out_file, rej_file))
        return failed("输入文件为空或无有效记录", statistics, cfg_snapshot)

    if degraded:
        status = "degraded"
        message = (f"模型不可用（{degrade_reason}），已降级为语言方向+长度比例+数字一致性规则证据，"
                   "semantic_score 写 null，未输出模型概率")
    elif statistics["rejected"] > 0:
        status = "partial"
        message = ""
    else:
        status = "success"
        message = ""

    if judge is not None and judge.retries:
        # 解析失败后加严重试的次数，和最终仍失败（status=error）分开记
        statistics["judge_status"]["retried"] = judge.retries
    log_action(logger, "处理", "语义等价检测完成：通过 {}，待复核 {}，拦截 {}"
                               "（其中保留在输出流 {}、从输出流删除 {}，策略 {}），"
                               "输出 {} 行（模型打分 {}，"
                               "判定单元 {}，模型调用 {} 次，父句 {} 组，"
                               "短单位 {} 条其中放行 {} 条，数字守卫拦下 {} 条"
                               "（缺失 {} / 重复 {} / 替换 {}），"
                               "单位/币种守卫拦下 {} 条，否定守卫拦下 {} 条，"
                               "不完整译文守卫拦下 {} 条，语义判别器拦下 {} 条"
                               "（状态 {}），降级 {}，模型错误 {}）".format(
        statistics["accepted"], statistics["waiting_quality"], statistics["rejected"],
        statistics["kept_rejected"], statistics["rejected_dropped"],
        cfg.non_equivalent_policy, statistics["output_records"],
        statistics["model_scored"], statistics["scoring_unit"], statistics["scored_units"],
        statistics["parent_groups"], statistics["short_unit_records"],
        statistics["short_unit_rescued"], statistics["number_guard_rejected"],
        statistics["number_guard_rule_hits"]["missing"],
        statistics["number_guard_rule_hits"]["count"],
        statistics["number_guard_rule_hits"]["swap"],
        statistics["unit_guard_rejected"], statistics["negation_guard_rejected"],
        statistics["incomplete_guard_rejected"], statistics["judge_rejected"],
        statistics["judge_status"] or "未启用",
        statistics["degraded"], statistics["model_error"]))
    log_action(logger, "输出", f"写入输出文件：{out_file.name}；异常清单：{rej_file.name}")
    # [无血缘改造 2026-10-19] 血缘输出已停用，保留原实现便于回溯；不再生成 lineage 文件。
    # append_lineage(lineage_file, make_lineage(
    #     OPERATOR_NAME, OPERATOR_VERSION, unique_id, str(input_file), str(out_file),
    #     status, started, now_iso(), cfg_snapshot, statistics))
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
    parser = argparse.ArgumentParser(description="算子14：语义等价性检测")
    parser.add_argument("--input", required=True, help="输入已分级句对 JSONL 文件")
    parser.add_argument("--unique-id", default="local_demo_file1", help="任务id_文件id")
    parser.add_argument("--config", default=None, help="配置文件路径")
    parser.add_argument("--output-root", default=None, help="产物根目录")
    args = parser.parse_args()
    result = run(args.input, args.unique_id, args.output_root, args.config)
    print(f"status={result['status']} statistics={result['statistics']}")
    print(f"stageZip={result.get('stageZip')} storageUri={result.get('storageUri')}")


if __name__ == "__main__":
    main()
