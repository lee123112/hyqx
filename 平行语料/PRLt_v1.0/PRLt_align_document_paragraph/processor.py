"""算子9：文档/段落级对齐 —— 核心处理逻辑与 CLI。

读取算子10 生成的对齐计划（`10_align_plan.json`）与计划引用的源/目标
UTF-8 文档，用长度相似度、段落位置先验和可比 token 重叠三项真实分数，
经单调动态规划建立 1:1 / 1:2 / 2:1 段落关系。低置信和未对齐段落进入
异常清单，不静默丢弃；本算子不切句、不做语义模型推理。
"""
import argparse
import json
import os
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

import yaml
from pydantic import ValidationError

from utils.alignment import (ambiguity_gap, build_candidates, paragraph_ref,
                             select_monotonic)
from utils.documents import (load_document, load_plan, plan_includes_paragraph_stage,
                             plan_lang_of, plan_path_of, resolve_document_ref)
from utils.artifacts import finalize_stage_artifacts
from utils.io import JsonlWriter, task_dir
from utils.logger import close_logger, get_logger, log_action
from utils.schemas import OperatorConfig
from utils.timeutil import now_iso

# [无血缘改造 2026-10-19] 血缘输出已停用，保留原导入便于回溯：
# from utils.lineage import append_lineage, make_lineage, now_iso

BASE_DIR = Path(__file__).resolve().parent
OPERATOR_NAME = "document_paragraph_alignment"
OPERATOR_ID = "09"
OPERATOR_VERSION = "v1.0.0"

DEFAULT_PLAN_FILE = "10_align_plan.json"
PAIRS_FILE = "09_paragraph_pairs.jsonl"
REJECT_FILE = "09_paragraph_rejects.jsonl"

PREVIEW_CHARS = 80


def load_config(config_path=None,
                overrides: dict = None) -> Tuple[OperatorConfig, dict]:
    cfg_path = (Path(config_path) if config_path
                else BASE_DIR / "configs" / "config.yaml")
    raw = yaml.safe_load(cfg_path.read_text(encoding="utf-8")) or {}
    if not isinstance(raw, dict):
        raise ValueError("配置文件必须是对象")
    merged = {key: value for key, value in raw.items() if key != "operator"}
    if overrides:
        merged.update(overrides)
    cfg = OperatorConfig(**merged)
    if cfg.weights_sum() <= 0:
        raise ValueError("打分权重之和必须大于 0")
    snapshot = {"operator": raw.get("operator", {}), **merged}
    return cfg, snapshot


def _reject(rejects: List[dict], reason: str, detail: str = "") -> None:
    rejects.append({"reason": reason, "detail": detail})


def _preview(text: str) -> str:
    flat = " ".join(text.split())
    return flat[:PREVIEW_CHARS]


def _base_statistics() -> Dict[str, Any]:
    return {
        "plan_loaded": 0,
        "source_paragraphs": 0,
        "target_paragraphs": 0,
        "empty_paragraphs_skipped": 0,
        "candidates_scored": 0,
        "pairs": 0,
        "pair_1_to_1": 0,
        "pair_1_to_2": 0,
        "pair_2_to_1": 0,
        "unaligned_source": 0,
        "unaligned_target": 0,
        "low_confidence": 0,
        "low_confidence_policy": None,
        "multi_candidate": 0,
        "rejected": 0,
        "min_confidence": None,
        "candidate_window": None,
        "granularity": None,
        "source_lang": None,
        "target_lang": None,
        "encoding": None,
        "backend": None,
        "pairs_written": 0,
        "total_score": 0.0,
    }


def _bump_reason(statistics: Dict[str, Any], reason: str) -> None:
    key = "reason:" + reason.split(":", 1)[0]
    statistics[key] = statistics.get(key, 0) + 1


def _resolve_plan_path(raw: Optional[str], tdir: Path) -> Optional[Path]:
    name = raw or DEFAULT_PLAN_FILE
    candidate = Path(name)
    attempts = [candidate] if candidate.is_absolute() else [
        Path.cwd() / candidate,
        tdir / candidate,
        tdir / "input" / candidate,
        BASE_DIR / candidate,
    ]
    for attempt in attempts:
        if attempt.is_file():
            return attempt.resolve()
    return None


def _pair_id(source_start: int, target_start: int) -> str:
    return "para-p%04d-t%04d" % (source_start + 1, target_start + 1)


def _build_record(chosen: dict, source_lang: str, target_lang: str) -> dict:
    metadata = {
        "granularity": "paragraph",
        "source_ref": "+".join(chosen["source_refs"]),
        "target_ref": "+".join(chosen["target_refs"]),
        "source_refs": list(chosen["source_refs"]),
        "target_refs": list(chosen["target_refs"]),
        "relation": chosen["relation"],
        "confidence": chosen["confidence"],
        "scores": dict(chosen["scores"]),
        "score_breakdown": {"weighted": dict(chosen["weighted"]),
                            "weights": dict(chosen["weights"]),
                            "penalty": chosen["penalty"]},
        "length_evidence": dict(chosen["length_evidence"]),
        "backend": "rule_degraded",
        "review_status": "auto_pass",
        "low_confidence_reason": None,
        "ambiguity": False,
        "ambiguity_evaluated": None,
        "ambiguity_gap": None,
        "ambiguity_runner_up": None,
    }
    return {
        "id": _pair_id(int(chosen["source_start"]), int(chosen["target_start"])),
        "source": chosen["source_text"],
        "target": chosen["target_text"],
        "source_lang": source_lang,
        "target_lang": target_lang,
        "metadata": metadata,
    }


def run(unique_id: str,
        plan_file: Optional[str] = None,
        output_root=None,
        config_path=None,
        overrides: dict = None,
        extra_input_root=None,
        logger=None) -> dict:
    output_root = (Path(output_root) if output_root
                   else Path(os.getenv("OUTPUT_DIR", BASE_DIR / "output")))
    try:
        tdir = task_dir(output_root, unique_id)
    except ValueError as exc:
        return {"status": "failed", "outputFile": None, "rejectFile": None,
                "stageZip": None, "storageUri": None, "statistics": _base_statistics(),
                "message": str(exc)}

    out_file = tdir / PAIRS_FILE
    rej_file = tdir / REJECT_FILE
    # [无血缘改造 2026-10-19] 不再生成 lineage 文件。
    # lineage_file = tdir / "lineage.json"
    logger = logger or get_logger(f"{OPERATOR_NAME}.{unique_id}",
                                  tdir / "run.log")
    rejects: List[dict] = []
    statistics = _base_statistics()
    input_ref_display = str(plan_file or "")
    document_id = Path(plan_file).stem if plan_file else unique_id

    def _finish(status: str, message: str, started: str,
                cfg_snapshot: dict) -> dict:
        statistics["rejected"] = len(rejects)
        with JsonlWriter(rej_file) as writer:
            for index, record in enumerate(rejects, start=1):
                record.setdefault("document_id", document_id)
                record.setdefault("parent_input_id", document_id)
                record.setdefault("id", f"{document_id}-reject-{index:06d}")
                record.setdefault("reasons", [record["reason"]] if record.get("reason") else [])
                record.setdefault("decision", "rejected")
                writer.write(record)
        # [无血缘改造 2026-10-19] 不再追加血缘；改为在成功/部分成功返回前打包阶段产物。
        # append_lineage(lineage_file, make_lineage(
        #     OPERATOR_NAME, OPERATOR_VERSION, unique_id,
        #     input_ref_display, str(out_file), status, started, now_iso(),
        #     cfg_snapshot, statistics))
        stage_zip = storage_uri = None
        if status != "failed":
            stage_zip, storage_uri = finalize_stage_artifacts(
                tdir, OPERATOR_ID, unique_id, out_file, rej_file, tdir / "run.log")
            log_action(logger, "归档",
                       f"阶段产物：{Path(stage_zip).name if stage_zip else '无'}；存储地址：{storage_uri}")
        if status == "failed":
            log_action(logger, "失败", message or "段落级对齐失败")
        log_action(logger, "完成", f"算子执行完成，状态 {status}")
        close_logger(logger)
        return {"status": status,
                "outputFile": str(out_file) if status != "failed" else None,
                "rejectFile": str(rej_file),
                "stageZip": stage_zip, "storageUri": storage_uri,
                "statistics": statistics, "message": message}

    # 1) 配置
    try:
        cfg, cfg_snapshot = load_config(config_path, overrides)
    except (AttributeError, OSError, TypeError, ValueError, ValidationError,
            yaml.YAMLError) as exc:
        message = f"配置错误：{type(exc).__name__}"
        log_action(logger, "失败", message)
        return _finish("failed", message, now_iso(),
                       {"config_error": type(exc).__name__})

    started = now_iso()
    log_action(logger, "配置", "窗口=%s 阈值=%s 权重=%s/%s/%s 降级策略=%s" % (
        cfg.candidate_window, cfg.min_confidence, cfg.length_weight,
        cfg.position_weight, cfg.overlap_weight, cfg.fallback_policy))

    # 2) 计划文件
    plan_path = _resolve_plan_path(plan_file, tdir)
    if plan_path is None:
        _reject(rejects, "plan_not_found", Path(plan_file or DEFAULT_PLAN_FILE).name)
        _bump_reason(statistics, "plan_not_found")
        log_action(logger, "失败", "对齐计划文件不存在")
        return _finish("failed", "对齐计划文件不存在（需要算子10 的产物）",
                       started, cfg_snapshot)
    input_ref_display = str(plan_path)
    try:
        plan, plan_reasons = load_plan(plan_path)
    except (OSError, UnicodeDecodeError, ValueError) as exc:
        _reject(rejects, "plan_read_error", type(exc).__name__)
        _bump_reason(statistics, "plan_read_error")
        log_action(logger, "失败", f"计划读取失败：{type(exc).__name__}")
        return _finish("failed", "对齐计划读取失败", started, cfg_snapshot)
    for reason in plan_reasons:
        _reject(rejects, reason)
        _bump_reason(statistics, reason)
    if plan is None:
        log_action(logger, "失败", "对齐计划无有效对象")
        return _finish("failed", "对齐计划无有效对象记录", started, cfg_snapshot)
    statistics["plan_loaded"] = 1
    source_context = plan.get("source") if isinstance(plan.get("source"), dict) else {}
    metadata_context = plan.get("metadata") if isinstance(plan.get("metadata"), dict) else {}
    declared_document_id = next((value for value in (
        plan.get("document_id"), source_context.get("document_id"),
        metadata_context.get("document_id"))
        if isinstance(value, str) and value.strip()), None)
    document_id = declared_document_id.strip() if declared_document_id else plan_path.stem
    log_action(logger, "输入", f"加载对齐计划：{plan_path.name}")

    # 3) 阶段路由：计划未包含段落级阶段时本算子不应执行
    if not plan_includes_paragraph_stage(plan):
        _reject(rejects, "stage_not_in_plan", "计划未路由到算子9（段落级）")
        _bump_reason(statistics, "stage_not_in_plan")
        log_action(logger, "失败", "计划未包含段落级阶段")
        return _finish("failed", "计划未路由到段落级阶段", started, cfg_snapshot)

    # 4) 降级策略：本 demo 只有规则后端，不使用任何语义模型分数
    policy = plan.get("fallback_policy")
    policy = policy if isinstance(policy, str) and policy.strip() else (
        cfg.fallback_policy)
    if policy == "fail" or cfg.backend == "model":
        detail = ("backend=model 未接入语义模型" if cfg.backend == "model"
                  else "fallback_policy=fail 且无语义模型可用")
        _reject(rejects, "model_unavailable", detail)
        _bump_reason(statistics, "model_unavailable")
        log_action(logger, "失败", "无语义模型可用，且不允许规则降级")
        return _finish("failed", "无语义模型可用，拒绝规则降级", started,
                       cfg_snapshot)
    statistics["backend"] = cfg.backend
    log_action(logger, "降级", "使用规则后端 backend=rule_degraded，未使用模型分数")

    # 5) 源/目标文档路径解析（默认限制在任务目录内）
    source_ref = plan_path_of(plan, "source")
    target_ref = plan_path_of(plan, "target")
    if not source_ref or not target_ref:
        _reject(rejects, "plan_missing_document_path", "计划缺少 source.path/target.path")
        _bump_reason(statistics, "plan_missing_document_path")
        log_action(logger, "失败", "计划缺少文档路径")
        return _finish("failed", "计划缺少 source.path 或 target.path",
                       started, cfg_snapshot)
    allowed_roots: List[Path] = [tdir, tdir / "input", plan_path.parent]
    if extra_input_root is not None:
        allowed_roots.append(Path(extra_input_root))
    try:
        source_path = resolve_document_ref(source_ref, allowed_roots,
                                          cfg.allow_external_document_paths)
        target_path = resolve_document_ref(target_ref, allowed_roots,
                                          cfg.allow_external_document_paths)
    except ValueError as exc:
        reason = str(exc)
        _reject(rejects, reason)
        _bump_reason(statistics, reason)
        log_action(logger, "失败", f"文档路径校验失败：{reason}")
        return _finish("failed", reason, started, cfg_snapshot)

    source_lang = plan_lang_of(plan, "source", cfg.source_lang)
    target_lang = plan_lang_of(plan, "target", cfg.target_lang)
    statistics["source_lang"] = source_lang
    statistics["target_lang"] = target_lang
    statistics["granularity"] = plan.get("granularity", "paragraph")
    statistics["min_confidence"] = cfg.min_confidence
    statistics["candidate_window"] = cfg.candidate_window
    statistics["low_confidence_policy"] = cfg.low_confidence_policy

    # 6) 严格编码读取与段落切分
    try:
        source_paragraphs, source_encoding, source_empty = load_document(
            source_path, cfg.input_encodings)
        target_paragraphs, target_encoding, target_empty = load_document(
            target_path, cfg.input_encodings)
    except (LookupError, OSError, UnicodeDecodeError, ValueError) as exc:
        _reject(rejects, "input_encoding_invalid", type(exc).__name__)
        _bump_reason(statistics, "input_encoding_invalid")
        log_action(logger, "失败", f"文档编码处理失败：{type(exc).__name__}")
        return _finish("failed", "文档编码处理失败，请检查文件编码", started,
                       cfg_snapshot)

    statistics["encoding"] = (source_encoding if source_encoding == target_encoding
                              else f"{source_encoding}+{target_encoding}")
    statistics["source_paragraphs"] = len(source_paragraphs)
    statistics["target_paragraphs"] = len(target_paragraphs)
    statistics["empty_paragraphs_skipped"] = source_empty + target_empty
    log_action(logger, "读取", "段落数：源 %d，目标 %d，空段落丢弃 %d，编码 %s" % (
        len(source_paragraphs), len(target_paragraphs), source_empty + target_empty,
        statistics["encoding"]))
    if not source_paragraphs:
        _reject(rejects, "empty_document:source", "源文档没有有效段落")
        _bump_reason(statistics, "empty_document")
    if not target_paragraphs:
        _reject(rejects, "empty_document:target", "目标文档没有有效段落")
        _bump_reason(statistics, "empty_document")
    if not source_paragraphs or not target_paragraphs:
        log_action(logger, "失败", "文档为空，无法建立段落关系")
        return _finish("failed", "文档为空或无有效段落", started, cfg_snapshot)

    # 7) 候选打分与单调 DP
    try:
        candidates = build_candidates(
            source_paragraphs=source_paragraphs,
            target_paragraphs=target_paragraphs,
            source_lang=source_lang, target_lang=target_lang,
            candidate_window=cfg.candidate_window,
            allow_1_to_2=cfg.allow_1_to_2, allow_2_to_1=cfg.allow_2_to_1,
            length_weight=cfg.length_weight,
            position_weight=cfg.position_weight,
            overlap_weight=cfg.overlap_weight,
        )
        selected, unaligned_source, unaligned_target, total = select_monotonic(
            candidates, len(source_paragraphs), len(target_paragraphs),
            cfg.group_penalty)
    except (TypeError, ValueError, ZeroDivisionError) as exc:
        _reject(rejects, "alignment_error", type(exc).__name__)
        _bump_reason(statistics, "alignment_error")
        log_action(logger, "失败", f"段落对齐计算失败：{type(exc).__name__}")
        return _finish("failed", "段落对齐计算失败", started, cfg_snapshot)
    statistics["candidates_scored"] = len(candidates)
    statistics["total_score"] = total
    statistics["unaligned_source"] = len(unaligned_source)
    statistics["unaligned_target"] = len(unaligned_target)

    # 8) 写通过输出与异常清单
    try:
        with JsonlWriter(out_file) as writer:
            for chosen in selected:
                record = _build_record(chosen, source_lang, target_lang)
                record["document_id"] = document_id
                record["parent_input_id"] = document_id
                record["metadata"]["document_id"] = document_id
                confidence = float(chosen["confidence"])
                relation = chosen["relation"]
                if confidence < cfg.min_confidence:
                    statistics["low_confidence"] += 1
                    rejects.append({
                        "id": record["id"],
                        "source": chosen["source_text"],
                        "target": chosen["target_text"],
                        "decision": ("retained" if cfg.low_confidence_policy == "mark_and_keep"
                                     else "rejected"),
                        "reason": "low_confidence",
                        "detail": ("已按 mark_and_keep 写入通过输出并标记复核"
                                   if cfg.low_confidence_policy == "mark_and_keep"
                                   else "置信度低于阈值，未写入通过输出"),
                        "source_refs": list(chosen["source_refs"]),
                        "target_refs": list(chosen["target_refs"]),
                        "relation": relation,
                        "confidence": confidence,
                        "scores": dict(chosen["scores"]),
                        "length_evidence": dict(chosen["length_evidence"]),
                        "text_preview": _preview(chosen["source_text"]),
                    })
                    if cfg.low_confidence_policy == "drop":
                        continue
                    # mark_and_keep：不下结论、不删记录，把「低置信」这一判断连同证据
                    # 交给下游（09/10/12）。静默删除会让缺陷段落整段从链路消失。
                    record["metadata"]["review_status"] = "needs_review"
                    record["metadata"]["low_confidence_reason"] = "low_confidence"
                    record["metadata"]["flagged_by"] = "08"
                    writer.write(record)
                    statistics["pairs_written"] += 1
                    statistics["pair_" + relation.replace(":", "_to_")] += 1
                    continue
                gap, runner_up, evaluated = ambiguity_gap(
                    candidates, len(source_paragraphs), len(target_paragraphs),
                    cfg.group_penalty, chosen, total)
                record["metadata"]["ambiguity_evaluated"] = evaluated
                record["metadata"]["ambiguity_gap"] = gap
                if evaluated and gap <= cfg.ambiguity_margin:
                    statistics["multi_candidate"] += 1
                    record["metadata"]["ambiguity"] = True
                    record["metadata"]["review_status"] = "needs_review"
                    record["metadata"]["flagged_by"] = "08"
                    record["metadata"]["low_confidence_reason"] = (
                        "conflict_multi_candidate")
                    runner_up_info = None
                    if runner_up is not None:
                        runner_up_info = {
                            "source_refs": list(runner_up["source_refs"]),
                            "target_refs": list(runner_up["target_refs"]),
                            "relation": runner_up["relation"],
                            "confidence": runner_up["confidence"],
                        }
                    record["metadata"]["ambiguity_runner_up"] = runner_up_info
                    rejects.append({
                        "id": record["id"],
                        "source": chosen["source_text"],
                        "target": chosen["target_text"],
                        "decision": "retained",
                        "reason": "conflict_multi_candidate",
                        "detail": "存在接近的第二优对齐方案，关系保留但需下游复核",
                        "source_refs": list(chosen["source_refs"]),
                        "target_refs": list(chosen["target_refs"]),
                        "confidence": confidence,
                        "ambiguity_gap": gap,
                        "runner_up": runner_up_info,
                    })
                writer.write(record)
                statistics["pairs_written"] += 1
                statistics["pair_" + relation.replace(":", "_to_")] += 1
        for index in unaligned_source:
            rejects.append({
                "id": "unaligned-source-" + paragraph_ref(index),
                "source": source_paragraphs[index],
                "target": None,
                "source_refs": [paragraph_ref(index)],
                "target_refs": [],
                "reason": "unaligned_paragraph:source",
                "detail": "源段落没有通过阈值的关系",
                "ref": paragraph_ref(index),
                "text_preview": _preview(source_paragraphs[index]),
            })
        for index in unaligned_target:
            rejects.append({
                "id": "unaligned-target-" + paragraph_ref(index),
                "source": None,
                "target": target_paragraphs[index],
                "source_refs": [],
                "target_refs": [paragraph_ref(index)],
                "reason": "unaligned_paragraph:target",
                "detail": "目标段落没有通过阈值的关系",
                "ref": paragraph_ref(index),
                "text_preview": _preview(target_paragraphs[index]),
            })
    except OSError as exc:
        _reject(rejects, "output_write_error", type(exc).__name__)
        _bump_reason(statistics, "output_write_error")
        log_action(logger, "失败", f"输出写入失败：{type(exc).__name__}")
        return _finish("failed", "输出写入失败", started, cfg_snapshot)

    statistics["pairs"] = statistics["pairs_written"]
    log_action(logger, "处理", "段落关系：通过 %d，低置信 %d，多候选 %d，未对齐 %d/%d" % (
        statistics["pairs"], statistics["low_confidence"],
        statistics["multi_candidate"], statistics["unaligned_source"],
        statistics["unaligned_target"]))
    log_action(logger, "输出", f"写入输出文件：{out_file.name}；异常清单：{rej_file.name}")
    return _finish("success", "", started, cfg_snapshot)


def main():
    parser = argparse.ArgumentParser(description="算子9：文档/段落级对齐")
    parser.add_argument("--input", default=None,
                        help="算子10 的对齐计划（JSON/JSONL），默认任务目录下 10_align_plan.json")
    parser.add_argument("--plan", default=None, help="--input 的别名")
    parser.add_argument("--unique-id", default="local_demo_file1", help="任务id_文件id")
    parser.add_argument("--config", default=None, help="配置文件路径")
    parser.add_argument("--output-root", default=None, help="产物根目录")
    args = parser.parse_args()

    result = run(
        args.unique_id,
        plan_file=args.plan or args.input,
        output_root=args.output_root,
        config_path=args.config,
    )
    print("status=%s statistics=%s message=%s" % (
        result["status"], result["statistics"], result.get("message", "")))
    print("stageZip=%s storageUri=%s" % (
        result.get("stageZip"), result.get("storageUri")))


if __name__ == "__main__":
    main()


