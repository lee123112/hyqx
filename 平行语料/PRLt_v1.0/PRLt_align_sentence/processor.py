"""算子10：句子级对齐 —— 核心处理逻辑和 CLI。

职责：在算子9 段落关系的约束下，把中英段落切成句子，用单调 DP 建立 1-1/1-2/2-1 句对，
优先使用本地 Qwen3-Embedding-0.6B 语义相似度；模型不可用时按配置降级为长度+证据规则后端，
状态标记 degraded 且语义分写 null，绝不伪造模型分数。
不负责段落关系（算子9）、最终交付治理（算子14）、语言检测（算子6）和去重（算子8）。
"""
import argparse
import codecs
import json
import os
import re
from functools import lru_cache
from pathlib import Path
from typing import Any, Dict, Iterator, List, Optional, Tuple

import yaml
from pydantic import ValidationError

from utils.aligner import align_paragraph, score_components  # noqa: F401
from utils.embedder import BackendUnavailable, embedding_available
from utils.artifacts import finalize_stage_artifacts
from utils.io import JsonlWriter, task_dir
from utils.logger import close_logger, get_logger, log_action
from utils.schemas import BACKEND_EMBEDDING, OperatorConfig
from utils.segmenter import sentence_ref, split_sentences
from utils.timeutil import now_iso

# [无血缘改造 2026-10-19] 血缘输出已停用，保留原导入便于回溯：
# from utils.lineage import append_lineage, make_lineage, now_iso

BASE_DIR = Path(__file__).resolve().parent
OPERATOR_NAME = "sentence_alignment"
OPERATOR_ID = "10"
OPERATOR_VERSION = "v1.0.0"

OUTPUT_NAME = "10_sentence_pairs.jsonl"
REJECT_NAME = "10_sentence_rejects.jsonl"


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


def resolve_backend(cfg: OperatorConfig) -> Tuple[Optional[str], bool, Optional[str], Optional[str]]:
    """解析实际对齐后端，返回 (后端, 是否降级, 降级原因, 失败原因)。

    - `rule_degraded`：显式规则后端，状态标记 degraded。
    - `qwen3_embedding`：依赖/权重不可用即结构化失败，不静默降级。
    - `auto`：优先 Qwen3-Embedding，不可用时只有 allow_degraded=true 才降级。
    配置值 `labse` 是旧兼容别名，已在 OperatorConfig 中归一化为 qwen3_embedding。
    """
    if cfg.model_backend == "rule_degraded":
        return "rule_degraded", True, "rule_degraded_requested", None
    if cfg.model_service_url or embedding_available():
        return BACKEND_EMBEDDING, False, None, None
    if cfg.model_backend == BACKEND_EMBEDDING:
        return None, False, None, "embedding_unavailable"
    if cfg.allow_degraded:
        return "rule_degraded", True, "embedding_unavailable", None
    return None, False, None, "embedding_unavailable"


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


def _lang_kind(lang_code: str) -> str:
    return "zh" if str(lang_code).lower().startswith("zh") else "en"


def _base_statistics(encoding=None, backend=None, model_version=None, device=None,
                     cfg: Optional[OperatorConfig] = None) -> Dict[str, Any]:
    return {
        "total": 0,
        "pairs": 0,
        "rejected": 0,
        "low_confidence": 0,
        "marked_low_confidence": 0,
        "residual_note": 0,
        "boundary_flagged": 0,
        "upstream_flagged": 0,
        "low_confidence_policy": cfg.low_confidence_policy if cfg else None,
        "ambiguous": 0,
        "model_error": 0,
        "unaligned_source": 0,
        "unaligned_target": 0,
        "source_sentences": 0,
        "target_sentences": 0,
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


def _reject(line_no, paragraph_id, reasons, source_ref=None, target_ref=None,
            source=None, target=None, source_refs=None, target_refs=None,
            confidence=None, scores=None, action=None, low_confidence_reason=None,
            ambiguity_margin=None, second_action=None) -> Dict[str, Any]:
    return {
        "line": line_no,
        "id": None,  # 写出前由 _stamp_reject 按段落序号补齐（规范层：拒绝记录也需顶层记录 id）
        "paragraph_id": paragraph_id,
        "source_ref": source_ref,
        "target_ref": target_ref,
        "source": source,
        "target": target,
        "source_refs": source_refs or [],
        "target_refs": target_refs or [],
        "reasons": list(reasons),
        "action": action,
        "confidence": confidence,
        "scores": scores,
        "low_confidence_reason": low_confidence_reason,
        "ambiguity_margin": ambiguity_margin,
        "second_action": second_action,
    }


def _declared(record: dict, field: str, fallback: str) -> str:
    value = record.get(field)
    return value if isinstance(value, str) and value.strip() else fallback


def _ref(record: dict, key: str, fallback: str) -> str:
    metadata = record.get("metadata") or {}
    value = metadata.get(key) if isinstance(metadata, dict) else None
    return value if isinstance(value, str) and value.strip() else fallback


def _round(value):
    if value is None or isinstance(value, bool):
        return value
    if isinstance(value, float):
        return round(value, 6)
    return value


def _build_pair(record, line_no, source_refs, target_refs, source_text, target_text,
                component, action_name, cfg, backend, model_version, paragraph_id, pair_index):
    scores = {key: _round(value) for key, value in component["scores"].items()}
    metadata = {
        "granularity": "sentence",
        "confidence": _round(component["confidence"]),
        "scores": scores,
        "backend": backend,
        "score_semantics": "model" if backend == BACKEND_EMBEDDING else "rule_only",
        "model_version": model_version,
        "source_ref": source_refs[0],
        "target_ref": target_refs[0],
        "source_refs": list(source_refs),
        "target_refs": list(target_refs),
        "paragraph_id": paragraph_id,
        "paragraph_line": line_no,
        "paragraph_source_ref": _ref(record, "source_ref", f"para-{line_no:03d}"),
        "paragraph_target_ref": _ref(record, "target_ref", f"para-{line_no:03d}"),
        "alignment_action": action_name,
        "unit_counts": component["units"],
        "evidence_tokens": {"shared": component["evidence_tokens"]["shared"]},
        "used_weights": component["used_weights"],
        "model_name": cfg.model_name if backend == BACKEND_EMBEDDING else None,
        "device": cfg.device if backend == BACKEND_EMBEDDING else None,
        "window": cfg.window,
        "min_confidence": cfg.min_confidence,
        "review_status": "auto_pass",
        "low_confidence_reason": None,
        "upstream_review_status": _upstream_review_status(record),
        "paragraph_metadata": record.get("metadata"),
    }
    if component.get("confidence_uncapped") is not None:
        # 置信度被残留注记门禁压过：原始分保留，方便复核时区分「模型本来就不高」和「被门禁压低」
        metadata["confidence_uncapped"] = _round(component["confidence_uncapped"])
    return {
        "id": f"{paragraph_id}-pair{pair_index:04d}",
        "source": source_text,
        "target": target_text,
        "source_lang": _declared(record, "source_lang", cfg.source_lang),
        "target_lang": _declared(record, "target_lang", cfg.target_lang),
        "metadata": metadata,
    }


def _upstream_review_status(record: dict) -> Optional[str]:
    """上游（算子08）段落关系上的复核标记；缺失返回 None。

    08 现在会把低置信段落关系以 review_status=needs_review 传给下游，
    句级对齐必须把这个判断继续推进，而不是在句层重新洗成 auto_pass。
    """
    metadata = record.get("metadata")
    if not isinstance(metadata, dict):
        return None
    status = metadata.get("review_status")
    return status if isinstance(status, str) and status.strip() else None


def _low_confidence_reason(component, cfg: OperatorConfig, backend: str) -> str:
    semantic = component["scores"]["semantic"]
    if semantic is not None and semantic < cfg.min_confidence:
        return "low_semantic"
    return "length_ratio_abnormal"


@lru_cache(maxsize=8)
def _compile_residual_patterns_cached(patterns: Tuple[str, ...]):
    compiled = []
    for pattern in patterns:
        try:
            compiled.append(re.compile(pattern, re.IGNORECASE))
        except re.error as exc:
            raise ValueError(f"invalid_regex:residual_note_patterns:{exc}") from exc
    return tuple(compiled)


def compile_residual_patterns(patterns: List[str]):
    """预编译残留注记正则；非法正则在配置层就报错，不等到处理中途崩。

    同一份配置在整次执行里只编译一次（按段落调用，重复编译纯属浪费）。
    """
    return _compile_residual_patterns_cached(tuple(patterns or ()))


def find_residual_note(texts: Dict[str, str], patterns) -> Tuple[Optional[str], Optional[str]]:
    """在译文/原文里找残留编辑注记，返回 (所在侧, 命中片段)。

    句子对齐只看长度比和语义分：一段漏译后接「后半句缺失」这类注记时，
    语义分依然很高、长度比也仍在正常区间，句对会被判成高置信。
    这里补的是文本层面的证据——注记本身就是「这一对不完整」的直接证据。
    """
    for side in ("target", "source"):
        text = texts.get(side) or ""
        if not text:
            continue
        for pattern in patterns:
            match = pattern.search(text)
            if match:
                return side, match.group(0)[:120]
    return None, None


def _strip_closing(text: str, closers) -> str:
    """去掉结尾的成对收尾符号（右引号/右括号），再看真正的句尾字符。"""
    trimmed = (text or "").strip()
    while trimmed and trimmed[-1] in closers:
        trimmed = trimmed[:-1].rstrip()
    return trimmed


def unbalanced_brackets(text: str, pairs) -> bool:
    """引号/括号是否不配对；同一符号成对（英文双引号）时按奇偶判断。"""
    text = text or ""
    for pair in pairs:
        if len(pair) != 2:
            continue
        left, right = pair
        if left == right:
            if text.count(left) % 2:
                return True
        elif text.count(left) != text.count(right):
            return True
    return False


def boundary_hit(source: str, target: str, cfg: OperatorConfig):
    """句界完整性判定；返回 (reason, detail) 或 None。

    1) boundary_mismatch：一侧以续接标点（逗号/冒号/破折号/左引号）结尾，另一侧不是——
       两条句子的句界没对上，双语引语或列举跨了句界；
    2) unclosed_quote：某侧引号/括号不配对且没有句末终止符——句子在中途被截断。

    这类句对的语义分往往仍然很高（两边说的确实是同一件事），长度比也在正常区间，
    融合分发现不了，但句界本身已经说明这一对不完整。
    """
    if not cfg.boundary_gate:
        return None, None
    continuation = set(cfg.boundary_continuation_marks)
    closing = set(cfg.boundary_closing_marks)
    terminals = set(cfg.boundary_terminal_marks)
    trimmed = {
        "source": _strip_closing(source, closing),
        "target": _strip_closing(target, closing),
    }
    tail = {side: text[-1:] if text else "" for side, text in trimmed.items()}
    if (tail["source"] in continuation) != (tail["target"] in continuation):
        return "boundary_mismatch", {
            "source_end": tail["source"], "target_end": tail["target"],
            "cap": cfg.boundary_confidence_cap,
        }
    for side in ("source", "target"):
        if trimmed[side] and trimmed[side][-1:] in terminals:
            continue
        if unbalanced_brackets(source if side == "source" else target, cfg.boundary_quote_pairs):
            return "unclosed_quote", {
                "side": side, "end": tail[side], "cap": cfg.boundary_confidence_cap,
            }
    return None, None


def cap_confidence(component: dict, cap: float) -> dict:
    """把组件置信度压到上限，保留原始置信度便于审计；不改其它分项。"""
    original = component.get("confidence")
    if not isinstance(original, (int, float)) or original <= cap:
        return component
    capped = dict(component)
    capped["confidence"] = cap
    capped["confidence_uncapped"] = original
    return capped


def _process_paragraph(record: dict, line_no: int, cfg: OperatorConfig, backend: str,
                       embedder, statistics: Dict[str, Any]) -> Tuple[List[dict], List[dict]]:
    """处理单条段落记录，返回 (通过句对, 异常条目)，并就地累计统计。"""
    metadata = record.get("metadata")
    if metadata is None:
        record["metadata"] = {}
    elif not isinstance(metadata, dict):
        raise ValueError("invalid_type:metadata")

    for field in cfg.text_fields:
        if field not in record:
            raise ValueError(f"missing_field:{field}")
        if not isinstance(record[field], str):
            raise ValueError(f"invalid_type:{field}")

    source_ref_base = _ref(record, "source_ref", f"para-{line_no:03d}")
    target_ref_base = _ref(record, "target_ref", f"para-{line_no:03d}")
    paragraph_id = record.get("id") if isinstance(record.get("id"), str) else f"para-{line_no:03d}"
    upstream_status = _upstream_review_status(record)

    sources = split_sentences(record["source"], _lang_kind(cfg.source_lang), cfg.zh_terminators)
    targets = split_sentences(record["target"], _lang_kind(cfg.target_lang), cfg.en_terminators)
    statistics["source_sentences"] += len(sources)
    statistics["target_sentences"] += len(targets)

    rejects: List[dict] = []
    if not sources or not targets:
        reason = "empty_source" if not sources else "empty_target"
        rejects.append(_reject(line_no, paragraph_id, [reason]))
        return [], rejects

    source_refs = [sentence_ref(source_ref_base, s["index"]) for s in sources]
    target_refs = [sentence_ref(target_ref_base, t["index"]) for t in targets]
    weights = {"semantic": cfg.semantic_weight, "length": cfg.length_weight,
               "evidence": cfg.evidence_weight}
    cache: Dict[Tuple[Tuple[int, ...], Tuple[int, ...]], dict] = {}
    note_patterns = compile_residual_patterns(cfg.residual_note_patterns)

    if backend == BACKEND_EMBEDDING:
        # DP evaluates many 1-1/1-2/2-1 candidates. Prefetch their component
        # texts so the embedding backend is called in batches, while
        # preserving the exact candidate scores and DP decisions.
        source_texts = set()
        target_texts = set()
        for i in range(len(sources)):
            for j in range(len(targets)):
                if abs(i - j) <= cfg.window:
                    source_texts.add(sources[i]["text"])
                    target_texts.add(targets[j]["text"])
                if cfg.allow_1_to_2 and j + 1 < len(targets) and abs(i - (j + 1)) <= cfg.window:
                    source_texts.add(sources[i]["text"])
                    target_texts.add(" ".join(targets[k]["text"] for k in (j, j + 1)))
                if cfg.allow_2_to_1 and i + 1 < len(sources) and abs((i + 1) - j) <= cfg.window:
                    source_texts.add(" ".join(sources[k]["text"] for k in (i, i + 1)))
                    target_texts.add(targets[j]["text"])
        embedder.encode(sorted(source_texts | target_texts))

    def scorer(source_index: Tuple[int, ...], target_index: Tuple[int, ...]) -> dict:
        key = (source_index, target_index)
        if key not in cache:
            source_text = " ".join(sources[i]["text"] for i in source_index)
            target_text = " ".join(targets[j]["text"] for j in target_index)
            semantic = None
            if backend == BACKEND_EMBEDDING:
                semantic = embedder.similarity([source_text], [target_text])[0]
            component = score_components(source_text, target_text, semantic, weights)
            component["texts"] = {"source": source_text, "target": target_text}
            cache[key] = component
        return cache[key]

    plan = align_paragraph(len(sources), len(targets), scorer, window=cfg.window,
                           skip_penalty=cfg.skip_penalty, allow_1_to_2=cfg.allow_1_to_2,
                           allow_2_to_1=cfg.allow_2_to_1,
                           ambiguity_margin=cfg.ambiguity_margin)
    model_version = embedder.model_version if backend == BACKEND_EMBEDDING else None

    pairs: List[dict] = []
    pair_index = 0
    for choice in plan["actions"]:
        if choice["score"] is None:
            continue
        pair_index += 1
        component = choice["score"]
        action_name = choice["action"]
        key = f"action:{action_name}"
        statistics[key] = statistics.get(key, 0) + 1
        source_ids = choice["source"]
        target_ids = choice["target"]
        margin = choice.get("ambiguity_margin")
        ambiguous = margin is not None and margin <= cfg.ambiguity_margin

        note_side, note_text = None, None
        if note_patterns and cfg.residual_note_policy != "ignore":
            note_side, note_text = find_residual_note(component["texts"], note_patterns)
            if note_side:
                component = cap_confidence(component, cfg.residual_note_confidence_cap)
                statistics["residual_note"] = statistics.get("residual_note", 0) + 1

        boundary_reason, boundary_detail = None, None
        if cfg.boundary_gate and note_side is None:
            boundary_reason, boundary_detail = boundary_hit(
                component["texts"]["source"], component["texts"]["target"], cfg)
            if boundary_reason:
                component = cap_confidence(component, cfg.boundary_confidence_cap)
                statistics["boundary_flagged"] = statistics.get("boundary_flagged", 0) + 1

        # 门禁结论优先于泛化的 low_confidence：置信度正是被门禁压下来的，
        # 只记 low_semantic / length_ratio_abnormal 会把真正的原因藏掉。
        reason = None
        reason_code = None
        if ambiguous:
            reason = reason_code = "conflict_multi_candidate"
        elif note_side and cfg.residual_note_policy != "ignore":
            reason = reason_code = f"{note_side}_residual_note"
        elif boundary_reason:
            reason = reason_code = boundary_reason
        elif component["confidence"] < cfg.min_confidence:
            reason = _low_confidence_reason(component, cfg, backend)
            reason_code = "low_confidence"

        if reason is None:
            pair = _build_pair(record, line_no, [source_refs[i] for i in source_ids],
                               [target_refs[j] for j in target_ids],
                               component["texts"]["source"], component["texts"]["target"],
                               component, action_name, cfg, backend, model_version,
                               paragraph_id, pair_index)
            if upstream_status == "needs_review":
                # 上游段落关系已判低置信：句子级分数再高也不能翻案
                pair["metadata"]["review_status"] = "needs_review"
                pair["metadata"]["low_confidence_reason"] = "upstream_needs_review"
                pair["metadata"]["flagged_by"] = "08"
                statistics["upstream_flagged"] = statistics.get("upstream_flagged", 0) + 1
            pairs.append(pair)
            continue

        reason_key = "ambiguous" if ambiguous else "low_confidence"
        statistics[reason_key] += 1
        _append_reason(statistics, reason_code)
        # 残留注记若显式配成 drop，就走异常清单、不进通过输出；默认 mark_and_keep 与低置信同策略
        note_drop = bool(note_side) and cfg.residual_note_policy == "drop"
        if cfg.low_confidence_policy == "mark_and_keep" and not note_drop:
            # 句子对齐不是内容终裁者：低置信/多候选句对仍然写进通过输出并打标记，
            # 异常清单照旧留审计；否则整段低置信时该段落会从下游彻底消失。
            pair_index += 1
            pair = _build_pair(record, line_no, [source_refs[i] for i in source_ids],
                               [target_refs[j] for j in target_ids],
                               component["texts"]["source"], component["texts"]["target"],
                               component, action_name, cfg, backend, model_version,
                               paragraph_id, pair_index)
            pair["metadata"]["review_status"] = "needs_review"
            pair["metadata"]["low_confidence_reason"] = reason
            pair["metadata"]["flagged_by"] = "09"
            if note_side:
                pair["metadata"]["residual_note"] = note_text
                pair["metadata"]["residual_note_side"] = note_side
            if boundary_reason:
                pair["metadata"]["boundary_gate"] = {
                    "reason": boundary_reason, **(boundary_detail or {}),
                }
            statistics["marked_low_confidence"] = statistics.get("marked_low_confidence", 0) + 1
            pairs.append(pair)
        rejects.append(_reject(
            line_no, paragraph_id, [reason_code],
            source_ref=source_refs[source_ids[0]], target_ref=target_refs[target_ids[0]],
            source=component["texts"]["source"], target=component["texts"]["target"],
            source_refs=[source_refs[i] for i in source_ids],
            target_refs=[target_refs[j] for j in target_ids],
            confidence=_round(component["confidence"]),
            scores={k: _round(v) for k, v in component["scores"].items()},
            action=action_name, low_confidence_reason=reason,
            ambiguity_margin=_round(margin), second_action=choice.get("second_action")))

    for index in plan["skipped_source"]:
        statistics["unaligned_source"] += 1
        _append_reason(statistics, "unaligned_source")
        rejects.append(_reject(line_no, paragraph_id, ["unaligned_source"],
                               source_ref=source_refs[index], source=sources[index]["text"],
                               source_refs=[source_refs[index]]))
    for index in plan["skipped_target"]:
        statistics["unaligned_target"] += 1
        _append_reason(statistics, "unaligned_target")
        rejects.append(_reject(line_no, paragraph_id, ["unaligned_target"],
                               target_ref=target_refs[index], target=targets[index]["text"],
                               target_refs=[target_refs[index]]))

    if len(sources) != len(targets):
        statistics["paragraph_count_mismatch"] = statistics.get("paragraph_count_mismatch", 0) + 1
    return pairs, rejects


def _failed(message: str, statistics, logger) -> dict:
    log_action(logger, "失败", message)
    # [无血缘改造 2026-10-19] 血缘输出已停用，保留原实现便于回溯；不再生成 lineage 文件。
    # append_lineage(lineage_file, make_lineage(
    #     OPERATOR_NAME, OPERATOR_VERSION, unique_id,
    #     str(input_file), str(output_root / unique_id / OUTPUT_NAME), "failed",
    #     started, now_iso(), cfg_snapshot, statistics))
    close_logger(logger)
    return {"status": "failed", "outputFile": None, "rejectFile": None,
            "stageZip": None, "storageUri": None, "statistics": statistics,
            "message": message}


def run(input_file: str, unique_id: str, output_root=None,
        config_path=None, overrides: dict = None, logger=None) -> dict:
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
        return _failed(f"配置错误：{type(exc).__name__}",
                       _base_statistics(), logger)

    log_action(logger, "输入", f"加载输入文件：{Path(input_file).name}")
    log_action(logger, "配置", "方向={}→{} 后端={} 模型={} 设备={} 窗口={} 阈值={}".format(
        cfg.source_lang, cfg.target_lang, cfg.model_backend, cfg.model_name,
        cfg.device, cfg.window, cfg.min_confidence))

    try:
        backend, degraded, degrade_reason, failure = resolve_backend(cfg)
    except (TypeError, ValueError) as exc:
        return _failed(f"后端解析错误：{type(exc).__name__}",
                       _base_statistics(cfg=cfg), logger)

    if failure:
        statistics = _base_statistics(backend=None, device=cfg.device, cfg=cfg)
        message = ("语义模型不可用且未允许降级（{}），算子按结构化失败结束，"
                   "不伪造语义分数".format(degrade_reason or failure))
        return _failed(message, statistics, logger)

    statistics = _base_statistics(backend=backend, device=cfg.device, cfg=cfg)
    if degraded:
        statistics["degraded"] = 1
        log_action(logger, "警告", f"语义模型降级：{degrade_reason}，改用长度+证据规则，语义分写 null")

    embedder = None
    if backend == BACKEND_EMBEDDING:
        from utils.embedder import EmbeddingBackend
        embedder = EmbeddingBackend(cfg.model_name, device=cfg.device, batch_size=cfg.batch_size,
                                    model_service_url=cfg.model_service_url,
                                    timeout_seconds=cfg.model_timeout_seconds)

    try:
        encoding = select_encoding(input_file, cfg.input_encodings)
    except (OSError, ValueError) as exc:
        statistics["encoding"] = None
        return _failed(
            f"输入文件编码处理失败：{type(exc).__name__}，本节点要求严格 UTF-8",
            statistics, logger)

    statistics["encoding"] = encoding
    log_action(logger, "读取", f"解码输入文件：{encoding}")

    out_tmp = out_file.parent / (out_file.name + ".tmp")
    rej_tmp = rej_file.parent / (rej_file.name + ".tmp")

    try:
        with JsonlWriter(out_tmp) as output_writer, JsonlWriter(rej_tmp) as reject_writer:
            reject_seq: Dict[str, int] = {}

            def _stamp_reject(item: Dict[str, Any]) -> Dict[str, Any]:
                """拒绝记录补顶层记录 id：同段落多条拒绝按序号区分；无段落 id 行保持 None。"""
                pid = item.get("paragraph_id")
                if pid is not None:
                    seq = reject_seq.get(str(pid), 0)
                    reject_seq[str(pid)] = seq + 1
                    item["id"] = f"{pid}-rej{seq:04d}"
                return item

            for lineno, line in enumerate(iter_decoded_lines(input_file, encoding), 1):
                if not line.strip():
                    continue
                statistics["total"] += 1
                try:
                    record = json.loads(line)
                except json.JSONDecodeError:
                    reject_writer.write(_stamp_reject(_reject(lineno, None, ["invalid_json"])))
                    statistics["rejected"] += 1
                    _append_reason(statistics, "invalid_json")
                    continue
                if not isinstance(record, dict):
                    reject_writer.write(_stamp_reject(_reject(lineno, None, ["invalid_record"])))
                    statistics["rejected"] += 1
                    _append_reason(statistics, "invalid_record")
                    continue
                try:
                    pairs, rejects = _process_paragraph(record, lineno, cfg, backend,
                                                        embedder, statistics)
                except BackendUnavailable as exc:
                    statistics["rejected"] += 1
                    statistics["model_error"] += 1
                    _append_reason(statistics, "model_error")
                    log_action(logger, "警告",
                               f"第 {lineno} 行模型调用失败：{str(exc).split(':')[0]}，写入异常清单")
                    reject_writer.write(_stamp_reject(_reject(lineno, record.get("id"), ["model_error"])))
                    if statistics["model_error"] >= cfg.max_model_errors:
                        raise
                    continue
                except (ValueError, TypeError) as exc:
                    reason = str(exc)
                    reject_writer.write(_stamp_reject(_reject(lineno, record.get("id"), [reason])))
                    statistics["rejected"] += 1
                    _append_reason(statistics, reason)
                    continue
                for pair in pairs:
                    output_writer.write(pair)
                    statistics["pairs"] += 1
                for item in rejects:
                    reject_writer.write(_stamp_reject(item))
                    statistics["rejected"] += 1
        os.replace(out_tmp, out_file)
        os.replace(rej_tmp, rej_file)
    except BackendUnavailable as exc:
        for leftover in (out_tmp, rej_tmp):
            try:
                leftover.unlink()
            except OSError:
                pass
        reason = str(exc).split(":")[0]
        return _failed(
            f"语义模型不可用：{reason}，算子按结构化失败结束，不伪造语义分数",
            statistics, logger)
    except (OSError, UnicodeDecodeError) as exc:
        for leftover in (out_tmp, rej_tmp):
            try:
                leftover.unlink()
            except OSError:
                pass
        return _failed(f"输入文件读取失败：{type(exc).__name__}",
                       statistics, logger)

    if backend == BACKEND_EMBEDDING and embedder is not None:
        statistics["model_version"] = embedder.model_version
    if statistics["total"] == 0:
        for empty_artifact in (out_file, rej_file):
            try:
                empty_artifact.unlink()
            except OSError:
                pass
        log_action(logger, "失败", "输入文件为空或无有效记录")
        # [无血缘改造 2026-10-19] 血缘输出已停用，保留原实现便于回溯。
        # append_lineage(lineage_file, make_lineage(
        #     OPERATOR_NAME, OPERATOR_VERSION, unique_id, str(input_file), str(out_file),
        #     "failed", started, now_iso(), cfg_snapshot, statistics))
        close_logger(logger)
        return {"status": "failed", "outputFile": None, "rejectFile": None,
                "stageZip": None, "storageUri": None, "statistics": statistics,
                "message": "输入文件为空或无有效记录"}

    if degraded:
        status = "degraded"
        message = (f"语义模型不可用（{degrade_reason}），已降级为长度+证据规则对齐，"
                   "语义分写 null，未输出模型概率")
    elif statistics["rejected"] > 0:
        status = "partial"
        message = ""
    else:
        status = "success"
        message = ""

    log_action(logger, "处理", "句子级对齐完成：句对 {}，异常 {}（低置信 {}，多候选 {}，未对齐源 {}／目标 {}）".format(
        statistics["pairs"], statistics["rejected"], statistics["low_confidence"],
        statistics["ambiguous"], statistics["unaligned_source"], statistics["unaligned_target"]))
    log_action(logger, "输出", f"写入输出文件：{out_file.name}；异常清单：{rej_file.name}")
    # [无血缘改造 2026-10-19] 不再追加血缘；改为打包本阶段产物并登记对象存储地址。
    # append_lineage(lineage_file, make_lineage(
    #     OPERATOR_NAME, OPERATOR_VERSION, unique_id, str(input_file), str(out_file),
    #     status, started, now_iso(), cfg_snapshot, statistics))
    stage_zip, storage_uri = finalize_stage_artifacts(
        tdir, OPERATOR_ID, unique_id, out_file, rej_file, tdir / "run.log")
    log_action(logger, "归档", f"阶段产物：{Path(stage_zip).name if stage_zip else '无'}；存储地址：{storage_uri}")
    log_action(logger, "完成", f"算子执行完成，状态 {status}")
    close_logger(logger)
    return {"status": status, "outputFile": str(out_file), "rejectFile": str(rej_file),
            "stageZip": stage_zip, "storageUri": storage_uri,
            "statistics": statistics, "message": message}


def main():
    parser = argparse.ArgumentParser(description="算子10：句子级对齐")
    parser.add_argument("--input", required=True, help="输入段落句对 JSONL 文件")
    parser.add_argument("--unique-id", default="local_demo_file1", help="任务id_文件id")
    parser.add_argument("--config", default=None, help="配置文件路径")
    parser.add_argument("--output-root", default=None, help="产物根目录")
    args = parser.parse_args()
    result = run(args.input, args.unique_id, args.output_root, args.config)
    print(f"status={result['status']} statistics={result['statistics']}")
    print(f"stageZip={result.get('stageZip')} storageUri={result.get('storageUri')}")


if __name__ == "__main__":
    main()


