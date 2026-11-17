"""算子11：子句与短语级对齐 —— 核心处理逻辑和 CLI。

职责：在算子10 句子级句对内做可解释的子句/短语切分，并用双语词典命中、位置一致、
长度比和父句置信度做单调匹配（1-1 / 1-2 / 2-1 / 跳过），输出最小粒度对齐证据。

不负责：句子/段落对齐（算子10）、语言检测（算子6）、最终置信度治理（算子12）、
语义等价检测（算子14）。模型不可用时只能输出 `rule_degraded` 和真实规则证据，
`semantic_score` 写 `null`，绝不伪造模型分数。
"""
import argparse
import codecs
import json
import os
from pathlib import Path
from typing import Any, Dict, Iterator, List, Optional, Tuple

import yaml
from pydantic import ValidationError

# [无血缘改造 2026-10-19] 血缘输出已停用，保留原导入便于回溯：
# from utils.lineage import append_lineage, make_lineage, now_iso
from utils.artifacts import finalize_stage_artifacts
from utils.duplication import max_repetition
from utils.logger import close_logger, get_logger, log_action
from utils.phrase_aligner import align_units, merge_texts, score_units
from utils.schemas import OperatorConfig
from utils.segmenter import build_units, clause_ref
from utils.semantic import BackendUnavailable, semantic_available
from utils.timeutil import now_iso

BASE_DIR = Path(__file__).resolve().parent
OPERATOR_NAME = "clause_phrase_alignment"
OPERATOR_ID = "11"
OPERATOR_VERSION = "v1.0.0"

OUTPUT_NAME = "11_clause_pairs.jsonl"
REJECT_NAME = "11_clause_rejects.jsonl"


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
    """解析实际对齐后端，返回 (后端, 是否降级, 降级原因, 失败原因)。

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
    input_path = Path(path)
    if not input_path.exists():
        raise FileNotFoundError(f"输入文件不存在：{input_path}")
    if not input_path.is_file():
        raise ValueError(f"输入路径不是文件：{input_path}")
    if input_path.stat().st_size == 0:
        raise ValueError("输入文件为空")
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


def _lang_kind(lang_code: str) -> str:
    return "zh" if str(lang_code).lower().startswith("zh") else "en"


def _base_statistics(encoding=None, backend=None, model_version=None, device=None,
                     cfg: Optional[OperatorConfig] = None) -> Dict[str, Any]:
    return {
        "input_pairs": 0,
        "input_clauses": 0,
        "aligned_clauses": 0,
        "rejected_clauses": 0,
        "conflicts": 0,
        "low_confidence": 0,
        "no_evidence": 0,
        # 2026-11-04 碎片/跨位门禁：拒收条数
        "fragments": 0,
        "cross_position": 0,
        "repetition_flagged": 0,
        "upstream_flagged": 0,
        "unaligned_source": 0,
        "unaligned_target": 0,
        "model_error": 0,
        "degraded": 0,
        "backend": backend,
        "model_version": model_version,
        "device": device,
        "window": cfg.window if cfg else None,
        "min_confidence": cfg.min_confidence if cfg else None,
        "encoding": encoding,
    }


def _append_reason(statistics: Dict[str, Any], reason: str) -> None:
    key = f"reason:{reason}"
    statistics[key] = statistics.get(key, 0) + 1


def _reject(line_no, pair_id, reasons, source_ref=None, target_ref=None,
            source=None, target=None, source_refs=None, target_refs=None,
            confidence=None, scores=None, alignment=None, low_confidence_reason=None,
            ambiguity_margin=None, second_alignment=None) -> Dict[str, Any]:
    return {
        "line": line_no,
        "id": None,  # 写出前由 _stamp_reject 按句对序号补齐（规范层：拒绝记录也需顶层记录 id）
        "pair_id": pair_id,
        "source_ref": source_ref,
        "target_ref": target_ref,
        "source": source,
        "target": target,
        "source_refs": source_refs or [],
        "target_refs": target_refs or [],
        "reasons": list(reasons),
        "alignment": alignment,
        "confidence": confidence,
        "scores": scores,
        "low_confidence_reason": low_confidence_reason,
        "ambiguity_margin": ambiguity_margin,
        "second_alignment": second_alignment,
    }


def _declared(record: dict, field: str, fallback: str) -> str:
    value = record.get(field)
    return value if isinstance(value, str) and value.strip() else fallback


def _ref(record: dict, key: str) -> Optional[str]:
    """父句引用优先取 metadata，其次取顶层同名字段。"""
    metadata = record.get("metadata")
    if isinstance(metadata, dict):
        value = metadata.get(key)
        if isinstance(value, str) and value.strip():
            return value.strip()
    value = record.get(key)
    return value.strip() if isinstance(value, str) and value.strip() else None


def _parent_confidence(record: dict) -> Optional[float]:
    """父句置信度：metadata.confidence / alignment_confidence，或顶层同名字段。"""
    metadata = record.get("metadata") if isinstance(record.get("metadata"), dict) else {}
    for source in (metadata, record):
        for key in ("alignment_confidence", "confidence"):
            value = source.get(key)
            if isinstance(value, bool):
                continue
            if isinstance(value, (int, float)) and 0.0 <= float(value) <= 1.0:
                return float(value)
    return None


def _round(value):
    if value is None or isinstance(value, bool):
        return value
    if isinstance(value, float):
        return round(value, 6)
    return value


def _low_confidence_reason(component: Dict, cfg: OperatorConfig) -> str:
    semantic = component["scores"]["semantic"]
    if semantic is not None and semantic < cfg.min_confidence:
        return "low_semantic"
    return "length_ratio_abnormal"


def _unit_type(source_units: List[Dict], source_ids: Tuple[int, ...]) -> str:
    return "phrase" if all(source_units[index]["unit_type"] == "phrase"
                           for index in source_ids) else "clause"


def _visible_chars(text: str) -> int:
    """去空白后的字符数；中文按字、英文按字符，只用于碎片门禁。"""
    return len("".join(str(text or "").split()))


def _fragment_gate(component: Dict, action_name: str,
                   source_ids: Tuple[int, ...], target_ids: Tuple[int, ...],
                   cfg: OperatorConfig) -> Optional[Tuple[str, Dict[str, Any]]]:
    """碎片/跨位门禁；返回 (reason, detail) 或 None。

    - fragment_too_short：源或目标任一侧去空白后 <= fragment_min_chars；
    - cross_position_mismatch：1-2 / 2-1 对，源与目标起始序号差 > cross_position_max_gap。
    """
    if cfg.fragment_gate:
        source_chars = _visible_chars(component["texts"]["source"])
        target_chars = _visible_chars(component["texts"]["target"])
        if (source_chars <= cfg.fragment_min_chars
                or target_chars <= cfg.fragment_min_chars):
            return "fragment_too_short", {
                "source_chars": source_chars, "target_chars": target_chars,
                "min_chars": cfg.fragment_min_chars,
            }
    if cfg.cross_position_gate and action_name in {"1-2", "2-1"}:
        gap = abs(int(source_ids[0]) - int(target_ids[0]))
        if gap > cfg.cross_position_max_gap:
            return "cross_position_mismatch", {
                "source_start": int(source_ids[0]), "target_start": int(target_ids[0]),
                "gap": gap, "max_gap": cfg.cross_position_max_gap,
            }
    return None


def _build_pair(source_units, target_units, source_ids, target_ids, component, action_name,
                source_refs, target_refs, pair_id, source_ref, target_ref, parent_confidence,
                cfg, backend, model_version, declared_source, declared_target, pair_index):
    scores = {key: _round(value) for key, value in component["scores"].items()}
    confidence = _round(component["confidence"])
    metadata = {
        "granularity": "clause_or_phrase",
        "unit_type": _unit_type(source_units, source_ids),
        "alignment": action_name,
        "confidence": confidence,
        "scores": scores,
        "evidence": component["evidence"],
        "backend": backend,
        "score_semantics": "model" if backend == "model" else "rule_only",
        "model_name": cfg.model_name if backend == "model" else None,
        "model_version": model_version,
        "device": cfg.device if backend == "model" else None,
        "source_ref": source_refs[source_ids[0]],
        "target_ref": target_refs[target_ids[0]],
        "source_refs": [source_refs[index] for index in source_ids],
        "target_refs": [target_refs[index] for index in target_ids],
        "parent_pair_id": pair_id,
        "parent_source_ref": source_ref,
        "parent_target_ref": target_ref,
        "parent_confidence": _round(parent_confidence),
        "unit_counts": component["units"],
        "used_weights": component["used_weights"],
        "window": cfg.window,
        "min_confidence": cfg.min_confidence,
        "review_status": ("auto_pass" if confidence is not None
                          and confidence >= cfg.review_threshold else "needs_review"),
        "status": "degraded" if backend == "rule_degraded" else "success",
        "low_confidence_reason": None,
        "parent_metadata": source_units[0].get("_parent_metadata"),
    }
    return {
        "id": f"{pair_id}-clause{pair_index:04d}",
        "source": component["texts"]["source"],
        "target": component["texts"]["target"],
        "source_lang": declared_source,
        "target_lang": declared_target,
        "metadata": metadata,
    }


def _process_pair(record: dict, line_no: int, cfg: OperatorConfig, backend: str,
                  scorer, statistics: Dict[str, Any]) -> Tuple[List[dict], List[dict]]:
    """处理单条句子级记录，返回 (通过子句对, 异常条目)，并就地累计统计。"""
    metadata = record.get("metadata")
    if metadata is None:
        record["metadata"] = {}
    elif not isinstance(metadata, dict):
        raise ValueError("invalid_type:metadata")

    pair_id = None
    for field in cfg.pair_id_fields:
        value = record.get(field)
        if value is None:
            continue
        if not isinstance(value, str):
            raise ValueError(f"invalid_type:{field}")
        if value.strip():
            pair_id = value.strip()
            break
    if pair_id is None:
        raise ValueError("missing_field:pair_id")

    for field in cfg.text_fields:
        if field not in record:
            raise ValueError(f"missing_field:{field}")
        if not isinstance(record[field], str):
            raise ValueError(f"invalid_type:{field}")

    source_text = record["source"].strip()
    target_text = record["target"].strip()
    if not source_text:
        return [], [_reject(line_no, pair_id, ["empty_source"])]
    if not target_text:
        return [], [_reject(line_no, pair_id, ["empty_target"])]

    source_ref = _ref(record, "source_ref")
    target_ref = _ref(record, "target_ref")
    if cfg.require_parent_ref and not source_ref:
        raise ValueError("schema_error:missing_source_ref")
    if cfg.require_parent_ref and not target_ref:
        raise ValueError("schema_error:missing_target_ref")
    source_ref = source_ref or f"{pair_id}-src"
    target_ref = target_ref or f"{pair_id}-tgt"

    declared_source = _declared(record, "source_lang", cfg.source_lang)
    declared_target = _declared(record, "target_lang", cfg.target_lang)
    if cfg.language_check and (declared_source != cfg.source_lang
                               or declared_target != cfg.target_lang):
        raise ValueError("language_declaration_mismatch")

    parent_confidence = _parent_confidence(record)

    source_units = build_units(source_text, _lang_kind(cfg.source_lang), cfg)
    target_units = build_units(target_text, _lang_kind(cfg.target_lang), cfg)
    statistics["input_clauses"] += len(source_units) + len(target_units)
    for unit in source_units + target_units:
        unit["_parent_metadata"] = record.get("metadata")
    if not source_units:
        return [], [_reject(line_no, pair_id, ["split_failed:empty_source"])]
    if not target_units:
        return [], [_reject(line_no, pair_id, ["split_failed:empty_target"])]

    source_refs = [clause_ref(source_ref, unit["index"]) for unit in source_units]
    target_refs = [clause_ref(target_ref, unit["index"]) for unit in target_units]
    weights = {"semantic": cfg.semantic_weight, "dictionary": cfg.dictionary_weight,
               "position": cfg.position_weight, "length": cfg.length_weight,
               "parent": cfg.parent_weight}
    cache: Dict[Tuple[Tuple[int, ...], Tuple[int, ...]], dict] = {}

    if backend == "model" and scorer is not None and hasattr(scorer, "encode"):
        # Preload all candidate clause/phrase texts in batches before DP. The
        # scorer still computes the same pairwise cosine values from its cache.
        source_texts = set()
        target_texts = set()
        for i in range(len(source_units)):
            for j in range(len(target_units)):
                if abs(i - j) <= cfg.window:
                    source_texts.add(merge_texts(source_units, (i,)))
                    target_texts.add(merge_texts(target_units, (j,)))
                if cfg.allow_1_to_2 and j + 1 < len(target_units) and abs(i - (j + 1)) <= cfg.window:
                    source_texts.add(merge_texts(source_units, (i,)))
                    target_texts.add(merge_texts(target_units, (j, j + 1)))
                if cfg.allow_2_to_1 and i + 1 < len(source_units) and abs((i + 1) - j) <= cfg.window:
                    source_texts.add(merge_texts(source_units, (i, i + 1)))
                    target_texts.add(merge_texts(target_units, (j,)))
        scorer.encode(sorted(source_texts | target_texts))

    def unit_scorer(source_index: Tuple[int, ...], target_index: Tuple[int, ...]) -> dict:
        key = (source_index, target_index)
        if key not in cache:
            joined_source = merge_texts(source_units, source_index)
            joined_target = merge_texts(target_units, target_index)
            semantic = None
            if backend == "model" and scorer is not None:
                semantic = scorer.similarity([joined_source], [joined_target])[0]
            cache[key] = score_units(
                joined_source, joined_target, source_index, target_index,
                len(source_units), len(target_units), parent_confidence,
                cfg.terminology, weights, semantic)
        return cache[key]

    plan = align_units(len(source_units), len(target_units), unit_scorer,
                       window=cfg.window, skip_penalty=cfg.skip_penalty,
                       allow_1_to_2=cfg.allow_1_to_2, allow_2_to_1=cfg.allow_2_to_1,
                       ambiguity_margin=cfg.ambiguity_margin)
    model_version = scorer.model_version if (backend == "model" and scorer is not None) else None

    pairs: List[dict] = []
    rejects: List[dict] = []
    pair_index = 0
    for choice in plan["actions"]:
        if choice["score"] is None:
            continue
        component = choice["score"]
        action_name = choice["action"]
        source_ids, target_ids = choice["source"], choice["target"]
        statistics[f"action:{action_name}"] = statistics.get(f"action:{action_name}", 0) + 1

        margin = choice.get("ambiguity_margin")
        ambiguous = margin is not None and margin <= cfg.ambiguity_margin
        reason = None
        detail = None
        if cfg.require_lexicon_evidence and component["scores"]["dictionary"] is None:
            reason = "no_evidence"
        elif ambiguous:
            reason = "conflict_multi_candidate"
        elif component["confidence"] < cfg.min_confidence:
            reason = "low_confidence"
            detail = _low_confidence_reason(component, cfg)

        # 碎片 / 跨位门禁：高置信度也拦（碎片本身不可判，不是置信度问题）
        if reason is None:
            gate = _fragment_gate(component, action_name, source_ids, target_ids, cfg)
            if gate is not None:
                reason, detail = gate
                key = "fragments" if reason == "fragment_too_short" else "cross_position"
                statistics[key] += 1
                _append_reason(statistics, reason)
                rejects.append(_reject(
                    line_no, pair_id, [reason],
                    source_ref=source_refs[source_ids[0]],
                    target_ref=target_refs[target_ids[0]],
                    source=component["texts"]["source"],
                    target=component["texts"]["target"],
                    source_refs=[source_refs[index] for index in source_ids],
                    target_refs=[target_refs[index] for index in target_ids],
                    confidence=_round(component["confidence"]),
                    scores={name: _round(value) for name, value in component["scores"].items()},
                    alignment=action_name, low_confidence_reason=reason,
                    ambiguity_margin=_round(margin), second_alignment=choice.get("second_action")))
                if cfg.fragment_policy == "mark_and_keep":
                    # 门禁结论要传下去：「这是碎片」是一个判断，不是「这条不存在」。
                    # 删掉记录会让下游（11 领域、12 置信度、17 交付）永远看不到它。
                    pair_index += 1
                    pair = _build_pair(
                        source_units, target_units, source_ids, target_ids, component,
                        action_name, source_refs, target_refs, pair_id, source_ref,
                        target_ref, parent_confidence, cfg, backend, model_version,
                        declared_source, declared_target, pair_index)
                    pair["metadata"]["review_status"] = "needs_review"
                    pair["metadata"]["low_confidence_reason"] = reason
                    pair["metadata"]["flagged_by"] = "10"
                    pair["metadata"]["gate"] = detail
                    pairs.append(pair)
                continue

        if reason is None:
            pair_index += 1
            pairs.append(_build_pair(
                source_units, target_units, source_ids, target_ids, component, action_name,
                source_refs, target_refs, pair_id, source_ref, target_ref, parent_confidence,
                cfg, backend, model_version, declared_source, declared_target, pair_index))
            continue

        if reason == "no_evidence":
            statistics["no_evidence"] += 1
        elif reason == "conflict_multi_candidate":
            statistics["conflicts"] += 1
        else:
            statistics["low_confidence"] += 1
        _append_reason(statistics, reason)
        rejects.append(_reject(
            line_no, pair_id, [reason],
            source_ref=source_refs[source_ids[0]], target_ref=target_refs[target_ids[0]],
            source=component["texts"]["source"], target=component["texts"]["target"],
            source_refs=[source_refs[index] for index in source_ids],
            target_refs=[target_refs[index] for index in target_ids],
            confidence=_round(component["confidence"]),
            scores={key: _round(value) for key, value in component["scores"].items()},
            alignment=action_name, low_confidence_reason=detail,
            ambiguity_margin=_round(margin), second_alignment=choice.get("second_action")))

    for index in plan["skipped_source"]:
        statistics["unaligned_source"] += 1
        _append_reason(statistics, "unaligned_source")
        rejects.append(_reject(line_no, pair_id, ["unaligned_source"],
                               source_ref=source_refs[index],
                               source=source_units[index]["text"],
                               source_refs=[source_refs[index]]))
    for index in plan["skipped_target"]:
        statistics["unaligned_target"] += 1
        _append_reason(statistics, "unaligned_target")
        rejects.append(_reject(line_no, pair_id, ["unaligned_target"],
                               target_ref=target_refs[index],
                               target=target_units[index]["text"],
                               target_refs=[target_refs[index]]))

    _apply_parent_guards(pairs, source_units, target_units, source_refs, target_refs,
                         record, cfg, statistics)
    return pairs, rejects


def _apply_parent_guards(pairs: List[dict], source_units: List[dict],
                         target_units: List[dict], source_refs: List[str],
                         target_refs: List[str], record: dict,
                         cfg: OperatorConfig, statistics: Dict[str, Any]) -> None:
    """父句级门禁：重复单位 + 上游复核标记；只改 review_status，不删记录。

    单个子句对的置信度看不见父句内部的结构问题（同一段源文被对齐了两遍、
    同一句译文被贴了两次），这类父句里每个子句对语义都很高，但整份对齐不可信。
    因此判定不在子句对层面做，而在父句层面做，然后把结论盖到该父句的每条记录上。
    """
    if not pairs:
        return

    if cfg.repetition_guard:
        for side, units, refs in (("source", source_units, source_refs),
                                  ("target", target_units, target_refs)):
            score, left, right = max_repetition([unit["text"] for unit in units],
                                                _lang_kind(cfg.source_lang
                                                           if side == "source"
                                                           else cfg.target_lang))
            if score >= cfg.repetition_containment:
                evidence = {
                    "side": side,
                    "containment": _round(score),
                    "threshold": cfg.repetition_containment,
                    "refs": [refs[left], refs[right]],
                    "texts": [units[left]["text"], units[right]["text"]],
                }
                statistics["repetition_flagged"] = statistics.get("repetition_flagged", 0) + 1
                _append_reason(statistics, "duplicate_unit")
                for pair in pairs:
                    metadata = pair["metadata"]
                    metadata["review_status"] = "needs_review"
                    metadata["low_confidence_reason"] = "duplicate_unit"
                    metadata["flagged_by"] = "10"
                    metadata["repetition"] = evidence
                break

    upstream = record.get("metadata")
    upstream_status = upstream.get("review_status") if isinstance(upstream, dict) else None
    if isinstance(upstream_status, str) and upstream_status.strip() == "needs_review":
        statistics["upstream_flagged"] = statistics.get("upstream_flagged", 0) + 1
        for pair in pairs:
            metadata = pair["metadata"]
            if metadata.get("review_status") == "needs_review":
                continue
            metadata["review_status"] = "needs_review"
            metadata["low_confidence_reason"] = "upstream_needs_review"
            metadata["flagged_by"] = upstream.get("flagged_by") or "upstream"


def _failed(output_root, unique_id: str, message: str, input_file, statistics,
            cfg_snapshot: dict, logger, started: str) -> dict:
    log_action(logger, "失败", message)
    # [无血缘改造 2026-10-19] 血缘输出已停用，保留原实现便于回溯；不再生成 lineage 文件。
    # append_lineage(lineage_file, make_lineage(
    #     OPERATOR_NAME, OPERATOR_VERSION, unique_id, str(input_file),
    #     str(Path(output_root) / unique_id / OUTPUT_NAME), "failed",
    #     started, now_iso(), cfg_snapshot, statistics))
    close_logger(logger)
    return {"status": "failed", "outputFile": None, "rejectFile": None,
            "stageZip": None, "storageUri": None,
            "statistics": statistics, "message": message}


def run(input_file: str, unique_id: str, output_root=None, config_path=None,
        overrides: dict = None, logger=None, semantic_backend=None) -> dict:
    from utils.io import JsonlWriter, task_dir

    output_root = Path(output_root) if output_root else Path(os.getenv("OUTPUT_DIR", BASE_DIR / "output"))
    tdir = task_dir(output_root, unique_id)
    out_file = tdir / OUTPUT_NAME
    rej_file = tdir / REJECT_NAME
    # [无血缘改造 2026-10-19] 不再生成 lineage 文件。
    # lineage_file = tdir / "lineage.json"
    logger = logger or get_logger(f"{OPERATOR_NAME}.{unique_id}", tdir / "run.log")
    started = now_iso()

    try:
        cfg, cfg_snapshot = load_config(config_path, overrides)
    except (AttributeError, OSError, TypeError, ValueError, ValidationError, yaml.YAMLError) as exc:
        statistics = _base_statistics()
        message = f"配置错误：{type(exc).__name__}"
        return _failed(output_root, unique_id, message, input_file, statistics,
                       {"config_error": type(exc).__name__}, logger, started)

    log_action(logger, "输入", f"加载输入文件：{Path(input_file).name}")
    log_action(logger, "配置", "方向={}→{} 后端={} 模型={} 窗口={} 阈值={}".format(
        cfg.source_lang, cfg.target_lang, cfg.model_backend, cfg.model_name,
        cfg.window, cfg.min_confidence))

    try:
        backend, degraded, degrade_reason, failure = resolve_backend(
            cfg, injected=semantic_backend is not None)
    except (TypeError, ValueError) as exc:
        statistics = _base_statistics(cfg=cfg)
        return _failed(output_root, unique_id, f"后端解析错误：{type(exc).__name__}",
                       input_file, statistics, cfg_snapshot, logger, started)

    if failure:
        statistics = _base_statistics(backend=None, device=cfg.device, cfg=cfg)
        message = (f"语义模型不可用（{failure}）且 fallback_policy=fail，算子按结构化失败结束，"
                   "不伪造模型分数")
        return _failed(output_root, unique_id, message, input_file, statistics,
                       cfg_snapshot, logger, started)

    scorer = semantic_backend
    if backend == "model" and scorer is None:
        from utils.semantic import MdebertaSemanticScorer
        scorer = MdebertaSemanticScorer(cfg.model_name, cfg.model_path, cfg.device, cfg.batch_size,
                                    cfg.model_service_url, cfg.model_timeout_seconds)
        try:
            scorer.load()
        except BackendUnavailable as exc:
            if cfg.fallback_policy == "rule_degraded":
                backend, degraded, degrade_reason, scorer = (
                    "rule_degraded", True, f"model_load_failed:{str(exc).split(':')[0]}", None)
            else:
                statistics = _base_statistics(backend=None, device=cfg.device, cfg=cfg)
                message = (f"语义模型加载失败（{str(exc).split(':')[0]}）且 fallback_policy=fail，"
                           "算子按结构化失败结束，不伪造模型分数")
                return _failed(output_root, unique_id, message, input_file, statistics,
                               cfg_snapshot, logger, started)

    statistics = _base_statistics(backend=backend, device=cfg.device, cfg=cfg)
    if degraded:
        statistics["degraded"] = 1
        log_action(logger, "警告",
                   f"语义模型降级：{degrade_reason}，改用词典+位置+长度+父句规则证据，语义分写 null")

    try:
        encoding = select_encoding(input_file, cfg.input_encodings)
    except FileNotFoundError as exc:
        return _failed(output_root, unique_id,
                       f"输入文件不存在：{Path(input_file)}，请等待上游算子生成正式 outputFile",
                       input_file, statistics, cfg_snapshot, logger, started)
    except (OSError, ValueError) as exc:
        return _failed(output_root, unique_id,
                       f"输入文件编码处理失败：{type(exc).__name__}，本节点要求严格 UTF-8",
                       input_file, statistics, cfg_snapshot, logger, started)

    statistics["encoding"] = encoding
    log_action(logger, "读取", f"解码输入文件：{encoding}")

    out_tmp = out_file.parent / (out_file.name + ".tmp")
    rej_tmp = rej_file.parent / (rej_file.name + ".tmp")
    reusable = True

    try:
        with JsonlWriter(out_tmp) as output_writer, JsonlWriter(rej_tmp) as reject_writer:
            reject_seq: Dict[str, int] = {}

            def _stamp_reject(item: Dict[str, Any]) -> Dict[str, Any]:
                """拒绝记录补顶层记录 id：同句对多条拒绝按序号区分；无句对 id 行保持 None。"""
                pid = item.get("pair_id")
                if pid is not None:
                    seq = reject_seq.get(str(pid), 0)
                    reject_seq[str(pid)] = seq + 1
                    item["id"] = f"{pid}-rej{seq:04d}"
                return item

            for lineno, line in enumerate(iter_decoded_lines(input_file, encoding), 1):
                if not line.strip():
                    continue
                statistics["input_pairs"] += 1
                try:
                    record = json.loads(line)
                except json.JSONDecodeError:
                    reject_writer.write(_stamp_reject(_reject(lineno, None, ["invalid_json"])))
                    statistics["rejected_clauses"] += 1
                    _append_reason(statistics, "invalid_json")
                    continue
                if not isinstance(record, dict):
                    reject_writer.write(_stamp_reject(_reject(lineno, None, ["invalid_record"])))
                    statistics["rejected_clauses"] += 1
                    _append_reason(statistics, "invalid_record")
                    continue
                try:
                    pairs, rejects = _process_pair(record, lineno, cfg, backend, scorer, statistics)
                except BackendUnavailable as exc:
                    statistics["rejected_clauses"] += 1
                    statistics["model_error"] += 1
                    _append_reason(statistics, "model_error")
                    log_action(logger, "警告",
                               f"第 {lineno} 行模型调用失败：{str(exc).split(':')[0]}，写入异常清单")
                    reject_writer.write(_stamp_reject(_reject(lineno, record.get("id"), ["model_error"])))
                    if statistics["model_error"] >= cfg.max_model_errors:
                        reusable = False
                        raise
                    continue
                except (ValueError, TypeError) as exc:
                    reason = str(exc)
                    reject_writer.write(_stamp_reject(_reject(lineno, record.get("pair_id") or record.get("id"),
                                                            [reason])))
                    statistics["rejected_clauses"] += 1
                    _append_reason(statistics, reason)
                    continue
                for pair in pairs:
                    output_writer.write(pair)
                    statistics["aligned_clauses"] += 1
                for item in rejects:
                    reject_writer.write(_stamp_reject(item))
                    statistics["rejected_clauses"] += 1
        os.replace(out_tmp, out_file)
        os.replace(rej_tmp, rej_file)
    except BackendUnavailable as exc:
        for leftover in (out_tmp, rej_tmp):
            try:
                leftover.unlink()
            except OSError:
                pass
        return _failed(output_root, unique_id,
                       f"语义模型不可用：{str(exc).split(':')[0]}，算子按结构化失败结束，不伪造模型分数",
                       input_file, statistics, cfg_snapshot, logger, started)
    except (OSError, UnicodeDecodeError) as exc:
        for leftover in (out_tmp, rej_tmp):
            try:
                leftover.unlink()
            except OSError:
                pass
        return _failed(output_root, unique_id, f"输入文件读取失败：{type(exc).__name__}",
                       input_file, statistics, cfg_snapshot, logger, started)

    if reusable and backend == "model" and scorer is not None:
        statistics["model_version"] = scorer.model_version
    if statistics["input_pairs"] == 0:
        for empty_artifact in (out_file, rej_file):
            try:
                empty_artifact.unlink()
            except OSError:
                pass
        return _failed(output_root, unique_id, "输入文件为空或无有效记录",
                       input_file, statistics, cfg_snapshot, logger, started)

    if degraded:
        status = "degraded"
        message = (f"模型不可用（{degrade_reason}），已降级为词典+位置+长度+父句规则对齐，"
                   "语义分写 null，未输出模型概率")
    elif statistics["rejected_clauses"] > 0:
        status = "partial"
        message = ""
    else:
        status = "success"
        message = ""

    log_action(logger, "处理", "子句级对齐完成：子句对 {}，异常 {}（冲突 {}，低置信 {}，无证据 {}，"
                               "碎片 {}，跨位 {}，未对齐源 {}／目标 {}）".format(
        statistics["aligned_clauses"], statistics["rejected_clauses"],
        statistics["conflicts"], statistics["low_confidence"], statistics["no_evidence"],
        statistics["fragments"], statistics["cross_position"],
        statistics["unaligned_source"], statistics["unaligned_target"]))
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
    parser = argparse.ArgumentParser(description="算子11：子句与短语级对齐")
    parser.add_argument("--input", required=True, help="输入句子级句对 JSONL 文件")
    parser.add_argument("--unique-id", default="local_demo_file1", help="任务id_文件id")
    parser.add_argument("--config", default=None, help="配置文件路径")
    parser.add_argument("--output-root", default=None, help="产物根目录")
    args = parser.parse_args()
    result = run(args.input, args.unique_id, args.output_root, args.config)
    print(f"status={result['status']} statistics={result['statistics']}")
    print(f"stageZip={result.get('stageZip')} storageUri={result.get('storageUri')}")


if __name__ == "__main__":
    main()

