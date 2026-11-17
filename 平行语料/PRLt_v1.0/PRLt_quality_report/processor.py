"""算子19：数据质量报告 —— 汇总产线阶段统计并计算转化率/留存率。

处理边界：
- 本节点是「数据集交付」阶段的**规则型汇总算子**，不重复执行上游清洗、不加载任何模型、
  也不重新计算上游质量分/对齐置信度；所有数字都来自真实输入计数。
- 只消费「统计事实」：阶段统计记录、交付汇总记录、可选交付明细记录，以及工程侧可选的
  `stage_statistics.json` / `18_manifest.json`。
- 业务单元以 `unit_type` 为准（默认 `sentence_pair`），**绝不用文件大小替代业务单元计数**。
- 分母为零、关键字段缺失、统计单位不一致、口径不一致或阶段链断裂时返回 `failed`，
  并把原因写入 `19_rejects.jsonl`；指标不达标时返回 `partial` 且 `passed=false`。
- 报告可追溯 task_id、pipeline_version、unit_type、分母时点、配置版本与阶段链。
- 无血缘改造（2026-10-19）：不再读取/生成/返回任何血缘文件，报告不含 lineage_refs。
"""
import argparse
import json
import os
from collections import Counter
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

import yaml
from pydantic import ValidationError

from utils.artifacts import finalize_stage_artifacts
from utils.io import (JsonlWriter, count_lines, iter_input_records, read_json_document,
                      select_encoding, task_dir, write_json_atomic)
from utils.logger import close_logger, get_logger, log_action
from utils.report import (GROUP_DIMENSION_FIELDS, KNOWN_RECORD_TYPES, build_scope,
                          build_thresholds, classify_record, compute_metrics,
                          most_common, normalize_unit_type, parse_delivery_summary,
                          parse_stage_record, reject_entry, stage_continuity_reasons,
                          sum_grades)
from utils.schemas import STAGE_COUNTER_FIELDS, OperatorConfig
from utils.timeutil import now_iso

# [无血缘改造 2026-10-19] 血缘输出已停用，保留原导入便于回溯：
# from utils.lineage import append_lineage, make_lineage, now_iso

BASE_DIR = Path(__file__).resolve().parent
OPERATOR_NAME = "data_quality_report"
OPERATOR_ID = "19"
OPERATOR_VERSION = "v1.0.0"
# [无血缘改造 2026-10-19] 不再读取上游血脉侧输入。
# DELIVERY_OPERATOR_NAME = "metadata_dataset_packaging"
DEFAULT_NAMES = {
    "report_name": "19_quality_report.json",
    "group_detail_name": "19_quality_report.jsonl",
    "reject_name": "19_rejects.jsonl",
}
GROUP_COUNT_KEYS = {
    "task_id": "task_id_counts",
    "language_pair": "language_pair_counts",
    "source_type": "source_type_counts",
    "domain": "domain_counts",
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
        merged.update(overrides)
    cfg = OperatorConfig(**merged)
    snapshot = {"operator": raw.get("operator", {}), **merged}
    return cfg, snapshot


def _base_statistics() -> Dict[str, Any]:
    return {
        "input_records": 0,
        "stage_records": 0,
        "dataset_records": 0,
        "delivery_summaries": 0,
        "rejected_records": 0,
        "side_inputs": [],
        "stages_seen": [],
        "encoding": None,
        "unit_types": {},
        "pipeline_versions": {},
        "pipeline_types": {},
        "task_id": None,
        "task_id_source": None,
        "low_quality_input_count": None,
        "converted_ai_ready_count": None,
        "pre_final_high_quality_count": None,
        "retained_high_quality_count": None,
        "low_quality_conversion_rate": None,
        "high_quality_retention_rate": None,
        "passed": False,
        "status": None,
        "execution_status": None,
        "evaluation_status": None,
        "group_totals": {},
        # [无血缘改造 2026-10-19] 报告不再引用血缘字段。
        # "lineage_ref_count": 0,
        "report_version": None,
        "config_version": None,
    }


def _bump(statistics: Dict[str, Any], key: str) -> None:
    statistics[key] = statistics.get(key, 0) + 1


def _append_reason(statistics: Dict[str, Any], reason: str) -> None:
    _bump(statistics, "reason:%s" % reason)


def _append_warning(statistics: Dict[str, Any], code: str) -> None:
    _bump(statistics, "warning:%s" % code)


def _dedupe(items: List[str]) -> List[str]:
    seen = set()
    ordered = []
    for item in items:
        if item not in seen:
            seen.add(item)
            ordered.append(item)
    return ordered


# --------------------------------------------------------------------------- #
# 任务标识与维度取值
# --------------------------------------------------------------------------- #

def derive_task_id(cfg: OperatorConfig, unique_id: str) -> Tuple[str, str]:
    """uniqueId = 任务id_文件id；无法拆分时按整体作为任务标识。"""
    separator = cfg.task_id_prefix_separator
    if separator and separator in unique_id:
        return unique_id.rsplit(separator, 1)[0], "unique_id_prefix"
    return unique_id, "unique_id"


def _dimension_value(cfg: OperatorConfig, state: dict, record: dict, dim: str) -> str:
    for field in GROUP_DIMENSION_FIELDS.get(dim, (dim,)):
        value = record.get(field)
        if isinstance(value, str) and value.strip():
            return value.strip()
        if value is not None and not isinstance(value, (dict, list, bool)):
            return str(value)
    if dim == "task_id":
        return state["derived_task_id"]
    return "unknown"


# [无血缘改造 2026-10-19] 不再从输入记录收集血缘引用（lineage_ref/lineage_refs）。
# def _record_refs(record: dict) -> List[str]:
#     refs: List[str] = []
#     single = record.get("lineage_ref")
#     if isinstance(single, str) and single.strip():
#         refs.append(single.strip())
#     many = record.get("lineage_refs")
#     if isinstance(many, list):
#         refs.extend(str(item).strip() for item in many
#                     if isinstance(item, str) and item.strip())
#     return refs


# --------------------------------------------------------------------------- #
# 汇总累加
# --------------------------------------------------------------------------- #

def _empty_state(cfg: OperatorConfig, unique_id: str) -> dict:
    derived, derived_source = derive_task_id(cfg, unique_id)
    return {
        "stages": {},
        "stage_records": Counter(),
        "stage_operators": {},
        "stages_order": [],
        "summaries": [],
        "group_units": {dim: Counter() for dim in cfg.group_dimensions},
        "group_hq": {dim: Counter() for dim in cfg.group_dimensions},
        "dataset_records": 0,
        "formal_ai_ready_records": 0,
        "dataset_grade_counts": Counter(),
        "formal_ai_ready_grade_counts": Counter(),
        # [无血缘改造 2026-10-19] 不再收集血缘引用。
        # "lineage_refs": [],
        "unit_types": Counter(),
        "stage_unit_types": {},
        "pipeline_versions": Counter(),
        "pipeline_types": Counter(),
        "task_ids": Counter(),
        "missing_reasons": [],
        "rejected_stages": [],
        "derived_task_id": derived,
        "derived_task_id_source": derived_source,
    }


def _absorb_common(cfg: OperatorConfig, state: dict, record: dict) -> None:
    unit = normalize_unit_type(cfg, record.get("unit_type"))
    if unit:
        state["unit_types"][unit] += 1
    for key, counter_name in (("pipeline_version", "pipeline_versions"),
                              ("pipeline_type", "pipeline_types")):
        value = record.get(key)
        if isinstance(value, str) and value.strip():
            state[counter_name][value.strip()] += 1
    task = record.get(cfg.task_id_field)
    if isinstance(task, str) and task.strip():
        state["task_ids"][task.strip()] += 1
    # [无血缘改造 2026-10-19] 不再收集输入记录中的血缘引用。
    # for ref in _record_refs(record):
    #     if ref not in state["lineage_refs"]:
    #         state["lineage_refs"].append(ref)


def _absorb_stage(cfg: OperatorConfig, state: dict, record: dict,
                  counters: Dict[str, int]) -> None:
    stage = record["stage"]
    bucket = state["stages"].setdefault(stage, {f: 0 for f in STAGE_COUNTER_FIELDS})
    for field, value in counters.items():
        bucket[field] += value
    state["stage_records"][stage] += 1
    unit = normalize_unit_type(cfg, record.get("unit_type"))
    if unit:
        state["stage_unit_types"].setdefault(stage, Counter())[unit] += 1
    if stage not in state["stages_order"]:
        state["stages_order"].append(stage)
    operator = record.get("operator")
    if isinstance(operator, str) and operator.strip():
        state["stage_operators"].setdefault(stage, [])
        if operator not in state["stage_operators"][stage]:
            state["stage_operators"][stage].append(operator)


def _note_rejected_stage(state: dict, record: dict) -> None:
    """被拒的阶段统计记录仍证明该阶段存在于产线，用于阶段链连续性判定。"""
    stage = record.get("stage")
    if isinstance(stage, str) and stage.strip() and stage.strip() not in state["rejected_stages"]:
        state["rejected_stages"].append(stage.strip())


def _absorb_dataset_record(cfg: OperatorConfig, state: dict, record: dict) -> None:
    state["dataset_records"] += 1
    formal = record.get("delivery_status") in (None, "delivered")
    if formal:
        state["formal_ai_ready_records"] += 1
    grade = record.get("quality_grade")
    if grade is None:
        grade = record.get("grade")
    grade = str(grade) if grade is not None else None
    if grade:
        state["dataset_grade_counts"][grade] += 1
        if formal:
            state["formal_ai_ready_grade_counts"][grade] += 1
    if not formal:
        return
    high_quality = {str(g) for g in cfg.high_quality_grades}
    for dim in cfg.group_dimensions:
        value = _dimension_value(cfg, state, record, dim)
        state["group_units"][dim][value] += 1
        if grade and grade in high_quality:
            state["group_hq"][dim][value] += 1


def _consume_input(cfg: OperatorConfig, input_file: str, encoding: str,
                   reject_writer: JsonlWriter, statistics: Dict[str, Any],
                   unique_id: str) -> dict:
    state = _empty_state(cfg, unique_id)
    for lineno, record, error in iter_input_records(input_file, encoding, cfg.input_format):
        statistics["input_records"] += 1
        if error == "invalid_json":
            reject_writer.write(reject_entry(lineno, "unknown", ["invalid_json"]))
            _append_warning(statistics, "invalid_json")
            continue
        if not isinstance(record, dict):
            reject_writer.write(reject_entry(lineno, "unknown", ["invalid_record"]))
            _append_warning(statistics, "invalid_record")
            continue

        kind = classify_record(record)
        if kind not in KNOWN_RECORD_TYPES:
            reject_writer.write(reject_entry(lineno, kind, ["unknown_record_type"],
                                            record=record))
            _append_warning(statistics, "unknown_record_type")
            continue

        _absorb_common(cfg, state, record)
        if kind == "stage_statistics":
            entry, counters, reasons = parse_stage_record(cfg, record, lineno)
            if entry is not None:
                reject_writer.write(entry)
                for reason in reasons:
                    _append_reason(statistics, reason)
                state["missing_reasons"].extend(reasons)
                _note_rejected_stage(state, record)
                continue
            _absorb_stage(cfg, state, record, counters)
            statistics["stage_records"] += 1
        elif kind == "delivery_summary":
            entry, summary, reasons = parse_delivery_summary(cfg, record, lineno)
            if entry is not None:
                reject_writer.write(entry)
                for reason in reasons:
                    _append_reason(statistics, reason)
                state["missing_reasons"].extend(reasons)
                continue
            summary["_source"] = "input"
            state["summaries"].append(summary)
            statistics["delivery_summaries"] += 1
        else:
            _absorb_dataset_record(cfg, state, record)
            statistics["dataset_records"] += 1
    return state


# --------------------------------------------------------------------------- #
# 侧输入（可选：工程侧提供的统计/清单/血缘）
# --------------------------------------------------------------------------- #

def _resolve_side_file(name: Optional[str], input_file: str, tdir: Path) -> Optional[Path]:
    if not name:
        return None
    raw = Path(name)
    candidates = [raw] if raw.is_absolute() else [
        Path(input_file).resolve().parent / raw,
        Path(tdir) / raw,
        BASE_DIR / raw,
    ]
    # [无血缘改造 2026-10-19] 侧输入仅保留业务统计/清单；不再查找 lineage 文件。
    # 兼容同义 JSONL 后缀（如工程侧提供 .jsonl 形式的同名统计文件）。
    if raw.suffix == ".json":
        candidates.extend([candidate.parent / (candidate.name + "l")
                           for candidate in list(candidates)])
    for candidate in candidates:
        target = candidate.resolve()
        if target.is_file():
            return target
    return None


def _read_json_records(path) -> List[Any]:
    try:
        document = read_json_document(path)
    except (OSError, UnicodeDecodeError, json.JSONDecodeError):
        # 行式 JSONL 回退：整体解析失败时按行解析（如 18 的 lineage.jsonl）
        try:
            text = Path(path).read_text(encoding="utf-8")
        except (OSError, UnicodeDecodeError):
            return []
        rows: List[Any] = []
        for line in text.splitlines():
            if not line.strip():
                continue
            try:
                rows.append(json.loads(line))
            except json.JSONDecodeError:
                return []
        return rows
    if isinstance(document, list):
        return document
    if isinstance(document, dict):
        for key in ("records", "stage_statistics", "stages"):
            value = document.get(key)
            if isinstance(value, list):
                return value
        return [document]
    return []


def _summary_from_manifest(manifest: dict) -> dict:
    stats = manifest.get("statistics") if isinstance(manifest.get("statistics"), dict) else {}
    delivery = manifest.get("record_count")
    waiting = stats.get("delivered_waiting_manual_records")
    formal = stats.get("formal_ai_ready_count")
    if formal is None and isinstance(delivery, int):
        formal = max(delivery - waiting, 0) if isinstance(waiting, int) else delivery
    delivery_grades = stats.get("delivery_grade_counts")
    formal_grades = stats.get("formal_delivery_grade_counts")
    if formal_grades is None and isinstance(delivery_grades, dict):
        formal_grades = {} if isinstance(waiting, int) and waiting >= delivery else delivery_grades
    return {
        "record_type": "delivery_summary",
        "task_id": manifest.get("task_id"),
        "pipeline_type": manifest.get("pipeline_type"),
        "pipeline_version": manifest.get("pipeline_version"),
        "unit_type": manifest.get("unit_type"),
        "language_pair": manifest.get("language_pair"),
        "delivery_records": delivery,
        "formal_ai_ready_count": formal,
        "formal_ai_ready_explicit": ("formal_ai_ready_count" in stats
                                      or "delivered_waiting_manual_records" in stats),
        "waiting_manual_count": waiting,
        "grade_counts": stats.get("grade_counts"),
        "delivery_grade_counts": delivery_grades,
        "formal_delivery_grade_counts": formal_grades,
        "language_pair_counts": manifest.get("language_pair_counts"),
        "source_type_counts": manifest.get("source_types"),
    }


# [无血缘改造 2026-10-19] 不再从上游血缘推导交付汇总。
# def _summary_from_lineage(entry: dict) -> Optional[dict]:
#     ...


def _load_side_inputs(cfg: OperatorConfig, input_file: str, tdir: Path, state: dict,
                      reject_writer: JsonlWriter, statistics: Dict[str, Any], logger) -> None:
    stage_path = _resolve_side_file(cfg.stage_statistics_file, input_file, tdir)
    if stage_path:
        statistics["side_inputs"].append(stage_path.name)
        log_action(logger, "来源", "读取侧输入阶段统计：%s" % stage_path.name)
        for index, item in enumerate(_read_json_records(stage_path), 1):
            if not isinstance(item, dict):
                reject_writer.write(reject_entry(index, "unknown", ["invalid_record"]))
                _append_warning(statistics, "invalid_record")
                continue
            _absorb_common(cfg, state, item)
            entry, counters, reasons = parse_stage_record(cfg, item, index)
            if entry is not None:
                entry["source"] = stage_path.name
                reject_writer.write(entry)
                for reason in reasons:
                    _append_reason(statistics, reason)
                state["missing_reasons"].extend(reasons)
                _note_rejected_stage(state, item)
                continue
            _absorb_stage(cfg, state, item, counters)
            statistics["stage_records"] += 1

    manifest_path = _resolve_side_file(cfg.manifest_file, input_file, tdir)
    if manifest_path:
        statistics["side_inputs"].append(manifest_path.name)
        log_action(logger, "来源", "读取侧输入交付清单：%s" % manifest_path.name)
        try:
            manifest = read_json_document(manifest_path)
        except (OSError, UnicodeDecodeError, json.JSONDecodeError):
            manifest = None
        if isinstance(manifest, dict):
            summary = _summary_from_manifest(manifest)
            summary["_source"] = manifest_path.name
            state["summaries"].append(summary)
            statistics["delivery_summaries"] += 1
            _absorb_common(cfg, state, summary)

    # [无血缘改造 2026-10-19] 不再读取上游血缘侧输入，也不再追加 lineage_refs；
    # 交付数量来源仅保留业务输入记录与 18_manifest.json 侧输入。
    # lineage_path = _resolve_side_file(cfg.upstream_lineage_file, input_file, tdir)
    # if lineage_path:
    #     upstream = [item for item in _read_json_records(lineage_path)
    #                 if isinstance(item, dict) and item.get("operator") != OPERATOR_NAME]
    #     if upstream:
    #         statistics["side_inputs"].append(lineage_path.name)
    #         log_action(logger, "来源", "读取侧输入阶段血缘：%s（%d 条）"
    #                    % (lineage_path.name, len(upstream)))
    #         for index, entry in enumerate(upstream):
    #             ref = "%s#%d:%s" % (lineage_path.name, index, entry.get("operator"))
    #             if ref not in state["lineage_refs"]:
    #                 state["lineage_refs"].append(ref)
    #         if cfg.use_lineage_statistics:
    #             for entry in reversed(upstream):
    #                 if entry.get("operator") != DELIVERY_OPERATOR_NAME:
    #                     continue
    #                 summary = _summary_from_lineage(entry)
    #                 if summary is not None:
    #                     summary["_source"] = lineage_path.name
    #                     state["summaries"].append(summary)
    #                     statistics["delivery_summaries"] += 1
    #                 break


# --------------------------------------------------------------------------- #
# 指标来源解析
# --------------------------------------------------------------------------- #

def _resolve_metrics_inputs(cfg: OperatorConfig, state: dict) -> dict:
    """解析两个比率的分母/分子，并记录来源；缺失一律返回 None。"""
    reasons: List[str] = []
    sources: Dict[str, Optional[str]] = {}

    # 分母固定为「产线首个阶段」的有效输入，绝不退化为后面阶段以免抬高转化率。
    if cfg.low_quality_input_stage:
        first_stage = cfg.low_quality_input_stage if cfg.low_quality_input_stage in state["stages"] else None
        missing_first = first_stage is None
    else:
        first_stage = next((stage for stage in cfg.stage_order if stage in state["stages"]), None)
        missing_first = first_stage is None
    low_quality_input_count = state["stages"][first_stage]["input_units"] if first_stage else None
    sources["low_quality_input_count"] = (
        "stage_statistics:%s.input_units" % first_stage if first_stage else None)
    if missing_first and state["stages"]:
        reasons.append("missing_denominator_source:low_quality_input_count")

    delivery_pairs = [(summary.get("_source", "input"),
                       summary.get("formal_ai_ready_count",
                                   summary.get("delivery_records")))
                      for summary in state["summaries"]
                      if summary.get("formal_ai_ready_count",
                                     summary.get("delivery_records")) is not None]
    if cfg.require_source_consistency and len({value for _, value in delivery_pairs}) > 1:
        reasons.append("delivery_statistics_conflict:delivery_records")
    summary_delivery = delivery_pairs[0][1] if delivery_pairs else None
    summary_delivery_source = delivery_pairs[0][0] if delivery_pairs else None
    dataset_delivery = (state["formal_ai_ready_records"]
                        if state["dataset_records"] > 0 else None)
    if (summary_delivery is not None and dataset_delivery is not None
            and summary_delivery != dataset_delivery):
        reasons.append("delivery_count_mismatch:summary=%d,dataset=%d"
                       % (summary_delivery, dataset_delivery))
    if summary_delivery is not None:
        converted = summary_delivery
        sources["converted_ai_ready_count"] = "%s.formal_ai_ready_count" % summary_delivery_source
    else:
        converted = dataset_delivery
        sources["converted_ai_ready_count"] = "dataset_record.formal_ai_ready_count" if dataset_delivery is not None else None

    grades = cfg.high_quality_grades
    pre = None
    for summary in state["summaries"]:
        value = summary.get("pre_final_high_quality_count")
        if value is not None:
            pre = value
            sources["pre_final_high_quality_count"] = (
                "%s.pre_final_high_quality_count" % summary.get("_source", "input"))
            break
    if pre is None:
        for summary in state["summaries"]:
            value = sum_grades(summary.get("grade_counts"), grades)
            if value is not None:
                pre = value
                sources["pre_final_high_quality_count"] = "%s.grade_counts(%s)" % (
                    summary.get("_source", "input"), "/".join(str(g) for g in grades))
                break
    if pre is None and state["dataset_grade_counts"]:
        pre = sum_grades(state["dataset_grade_counts"], grades)
        sources["pre_final_high_quality_count"] = "dataset_record.quality_grade"

    retained = None
    # 优先使用清单明确区分的正式 AI-Ready 等级统计；普通 delivery_grade_counts
    # 可能包含 waiting_manual 临时记录，不能直接当作正式留存分子。
    for summary in state["summaries"]:
        value = sum_grades(summary.get("formal_delivery_grade_counts"), grades)
        if value is not None:
            retained = value
            sources["retained_high_quality_count"] = "%s.formal_delivery_grade_counts(%s)" % (
                summary.get("_source", "input"), "/".join(str(g) for g in grades))
            break
    if retained is None:
        for summary in state["summaries"]:
            if summary.get("formal_ai_ready_explicit") and summary.get("formal_ai_ready_count") == 0:
                retained = 0
                sources["retained_high_quality_count"] = "%s.formal_ai_ready_count" % summary.get("_source", "input")
                break
    for summary in state["summaries"]:
        if retained is not None:
            break
        value = summary.get("retained_high_quality_count")
        if value is not None:
            retained = value
            sources["retained_high_quality_count"] = (
                "%s.retained_high_quality_count" % summary.get("_source", "input"))
            break
    if retained is None:
        for summary in state["summaries"]:
            value = sum_grades(summary.get("delivery_grade_counts"), grades)
            if value is not None:
                retained = value
                sources["retained_high_quality_count"] = "%s.delivery_grade_counts(%s)" % (
                    summary.get("_source", "input"), "/".join(str(g) for g in grades))
                break
    if retained is None and state["formal_ai_ready_grade_counts"]:
        retained = sum_grades(state["formal_ai_ready_grade_counts"], grades)
        sources["retained_high_quality_count"] = "dataset_record.formal_ai_ready_quality_grade"

    return {
        "low_quality_input_count": low_quality_input_count,
        "converted_ai_ready_count": converted,
        "pre_final_high_quality_count": pre,
        "retained_high_quality_count": retained,
        "sources": sources,
        "reasons": reasons,
    }


def _resolve_groups(cfg: OperatorConfig, state: dict, converted: Optional[int]
                    ) -> Tuple[dict, dict, List[str], List[str]]:
    """分组统计：优先使用真实交付明细记录，其次使用交付汇总的聚合计数。"""
    reasons: List[str] = []
    warnings: List[str] = []
    group_statistics: Dict[str, Any] = {}
    group_totals: Dict[str, int] = {}

    if state["dataset_records"] > 0:
        for dim in cfg.group_dimensions:
            units = state["group_units"][dim]
            if not units:
                warnings.append("group_dimension_unavailable:%s" % dim)
                continue
            high_quality = state["group_hq"][dim]
            group_statistics[dim] = {
                key: {"units": units[key], "high_quality_units": high_quality.get(key, 0)}
                for key in sorted(units)
            }
            group_totals[dim] = sum(units.values())
    else:
        for dim in cfg.group_dimensions:
            counts = None
            key_name = GROUP_COUNT_KEYS.get(dim, "%s_counts" % dim)
            for summary in state["summaries"]:
                value = summary.get(key_name)
                if isinstance(value, dict) and value:
                    counts = {str(key): int(val) for key, val in value.items()}
                    break
            if not counts:
                warnings.append("group_dimension_unavailable:%s" % dim)
                continue
            group_statistics[dim] = {
                key: {"units": value, "high_quality_units": None}
                for key, value in sorted(counts.items())
            }
            group_totals[dim] = sum(counts.values())

    if cfg.require_group_consistency and converted is not None:
        for dim, total in group_totals.items():
            if total != converted:
                reasons.append("group_total_mismatch:%s:groups=%d,total=%d"
                               % (dim, total, converted))
    return group_statistics, group_totals, reasons, warnings


def _consistency_reasons(cfg: OperatorConfig, state: dict) -> Tuple[List[str], List[str]]:
    """一致性检查：返回 (阻断原因, 警告)。

    混合统计单元（句对/子句对并存）在 allow_mixed_unit_types=true 时降级为警告：
    一期链路 09→10→11 按工具清单设计即切换单元口径，报告按阶段各自 unit_type 呈现。
    """
    reasons: List[str] = []
    warnings: List[str] = []
    if len(state["unit_types"]) > 1:
        if cfg.allow_mixed_unit_types:
            warnings.append("unit_type_mixed:%s" % ",".join(sorted(state["unit_types"])))
        else:
            reasons.append("unit_type_inconsistent")
    if len(state["pipeline_versions"]) > 1:
        reasons.append("pipeline_version_inconsistent")
    if len(state["pipeline_types"]) > 1:
        reasons.append("pipeline_type_inconsistent")
    if cfg.require_stage_continuity:
        present = state["stages_order"] + [stage for stage in state["rejected_stages"]
                                          if stage not in state["stages_order"]]
        reasons.extend(stage_continuity_reasons(cfg, present))
    return reasons, warnings


# --------------------------------------------------------------------------- #
# 报告组装
# --------------------------------------------------------------------------- #

def _build_stage_output(state: dict) -> Tuple[dict, dict]:
    stage_statistics: Dict[str, Any] = {}
    for stage, counters in sorted(state["stages"].items()):
        payload = dict(counters)
        payload["records"] = state["stage_records"][stage]
        payload["operators"] = list(state["stage_operators"].get(stage, []))
        units = state["stage_unit_types"].get(stage)
        # 分阶段单元口径：混合单元链路下各阶段按自己的 unit_type 呈现
        payload["unit_type"] = max(units, key=units.get) if units else None
        stage_statistics[stage] = payload
    stage_totals = {field: sum(state["stages"][stage][field] for stage in state["stages"])
                    for field in STAGE_COUNTER_FIELDS}
    stage_totals["stages"] = len(state["stages"])
    return stage_statistics, stage_totals


def _failure(logger, unique_id, input_file, report_file, cfg_snapshot,
             statistics, message, started, reject_file=None) -> dict:
    log_action(logger, "失败", message)
    # [无血缘改造 2026-10-19] 血缘输出已停用，保留原实现便于回溯；失败分支不生成阶段产物。
    # append_lineage(lineage_file, make_lineage(
    #     OPERATOR_NAME, OPERATOR_VERSION, unique_id, str(input_file), str(report_file),
    #     "failed", started, now_iso(), cfg_snapshot, statistics))
    close_logger(logger)
    return {"status": "failed", "outputFile": None, "rejectFile": reject_file,
            "reportFile": None, "groupDetailFile": None,
            "stageZip": None, "storageUri": None,
            "statistics": statistics,
            "message": message, "report": None}


# --------------------------------------------------------------------------- #
# 主流程
# --------------------------------------------------------------------------- #

def run(input_file: str, unique_id: str, output_root=None, config_path=None,
        overrides: dict = None, logger=None) -> dict:
    output_root = Path(output_root) if output_root else Path(
        os.getenv("OUTPUT_DIR", BASE_DIR / "output"))
    tdir = task_dir(output_root, unique_id)
    # [无血缘改造 2026-10-19] 不再生成 lineage 文件。
    # lineage_file = tdir / "lineage.json"

    try:
        cfg, cfg_snapshot = load_config(config_path, overrides)
    except (AttributeError, OSError, TypeError, ValueError, ValidationError, yaml.YAMLError) as exc:
        logger = logger or get_logger("%s.%s" % (OPERATOR_NAME, unique_id), tdir / "run.log")
        message = "配置错误：%s" % type(exc).__name__
        return _failure(logger, unique_id, input_file,
                        tdir / DEFAULT_NAMES["report_name"], {"config_error": message},
                        _base_statistics(), message, now_iso())

    report_file = tdir / cfg.report_name
    group_file = tdir / cfg.group_detail_name
    reject_file = tdir / cfg.reject_name
    log_file = tdir / "run.log"
    tmp_group = group_file.with_name(group_file.name + ".tmp")
    tmp_reject = reject_file.with_name(reject_file.name + ".tmp")

    logger = logger or get_logger("%s.%s" % (OPERATOR_NAME, unique_id), log_file)
    started = now_iso()
    statistics = _base_statistics()
    statistics["report_version"] = cfg.report_version
    statistics["config_version"] = cfg.config_version

    protected = {path.resolve() for path in
                 (report_file, group_file, reject_file, log_file)}
    if Path(input_file).resolve() in protected:
        return _failure(logger, unique_id, input_file, report_file,
                        cfg_snapshot, statistics,
                        "输入文件与输出文件路径相同，拒绝覆盖输入", started)

    log_action(logger, "输入", "加载统计输入文件：%s" % Path(input_file).name)
    log_action(logger, "配置", "转化率阈值=%.2f 留存率阈值=%.2f 高品质等级=%s 单位=%s"
               % (cfg.low_quality_conversion_threshold, cfg.high_quality_retention_threshold,
                  cfg.high_quality_grades, cfg.default_unit_type))

    blocking_extra: List[str] = []
    message = ""
    encoding: Optional[str] = None
    try:
        encoding = select_encoding(input_file, cfg.input_encodings)
    except (OSError, ValueError) as exc:
        message = "输入文件编码处理失败：%s，请检查文件编码" % type(exc).__name__
        blocking_extra.append("encoding_error:%s" % type(exc).__name__)
        log_action(logger, "失败", message)
    else:
        statistics["encoding"] = encoding
        log_action(logger, "读取", "解码输入文件：%s" % encoding)

    reject_writer = JsonlWriter(tmp_reject)
    state = _empty_state(cfg, unique_id)
    if encoding is not None:
        try:
            state = _consume_input(cfg, input_file, encoding, reject_writer,
                                   statistics, unique_id)
            _load_side_inputs(cfg, input_file, tdir, state, reject_writer,
                              statistics, logger)
        except (OSError, UnicodeDecodeError) as exc:
            blocking_extra.append("read_error:%s" % type(exc).__name__)
            message = message or "输入文件读取失败：%s" % type(exc).__name__
            log_action(logger, "失败", message)
            state = _empty_state(cfg, unique_id)
            for key in ("input_records", "stage_records", "dataset_records",
                        "delivery_summaries"):
                statistics[key] = 0
        if statistics["input_records"] == 0 and not blocking_extra:
            # 算子18在正式 AI-Ready 数量为 0 时会合法地产生空交付明细。
            # 只要业务清单已被读取，这表示“成功读取了零交付结果”，
            # 应由指标层返回 BLOCKED，而不是把 19 的执行误报为读取失败。
            # [无血缘改造 2026-10-19] 不再要求 lineage.json 侧输入。
            valid_empty_delivery = (
                statistics["delivery_summaries"] > 0
                and "18_manifest.json" in statistics["side_inputs"]
            )
            if not valid_empty_delivery:
                blocking_extra.append("empty_input")
                message = message or "输入文件为空或无有效记录"
                log_action(logger, "失败", message)
            else:
                log_action(logger, "读取", "正式交付明细为空，按零交付结果继续进行统计可计算性审计")

    # ---------------- 指标与分组 ----------------
    resolved = _resolve_metrics_inputs(cfg, state)
    metric_scope_blocking = []
    if len(state["unit_types"]) > 1 and not cfg.allow_mixed_unit_types:
        metric_scope_blocking.append("unit_type_inconsistent")
    if len(state["pipeline_versions"]) > 1:
        metric_scope_blocking.append("pipeline_version_inconsistent")
    if len(state["pipeline_types"]) > 1:
        metric_scope_blocking.append("pipeline_type_inconsistent")
    metrics = compute_metrics(
        cfg, resolved["low_quality_input_count"], resolved["converted_ai_ready_count"],
        resolved["pre_final_high_quality_count"], resolved["retained_high_quality_count"],
        scope_blocking=metric_scope_blocking)
    group_statistics, group_totals, group_reasons, group_warnings = _resolve_groups(
        cfg, state, resolved["converted_ai_ready_count"])
    consistency, consistency_warnings = _consistency_reasons(cfg, state)

    # [无血缘改造 2026-10-19] 不再统计 lineage_refs / lineage 缺失，报告不含血缘字段。
    # lineage_refs = list(state["lineage_refs"])
    # lineage_unavailable = not lineage_refs

    missing_blocking: List[str] = []
    missing_warnings: List[str] = []
    if state["missing_reasons"]:
        if cfg.missing_field_policy == "failed":
            missing_blocking = _dedupe(state["missing_reasons"])
        elif cfg.missing_field_policy == "partial":
            missing_warnings = _dedupe(state["missing_reasons"])

    blocking = _dedupe(blocking_extra + list(resolved["reasons"]) + list(metrics["blocking_reasons"])
                       + consistency + group_reasons + missing_blocking)
    threshold_reasons = list(metrics["threshold_reasons"])
    failure_reasons = _dedupe(blocking + threshold_reasons)
    warnings = _dedupe(group_warnings + missing_warnings + consistency_warnings)

    # 指标级异常（分母为零、血缘断裂、统计口径不一致等）同样写入独立异常清单。
    statistic_metric_rejects = 0
    for reason in blocking:
        if reason.startswith("missing_field:"):
            continue
        reject_writer.write(reject_entry(0, "metrics", [reason]))
        statistic_metric_rejects += 1
    reject_writer.close()
    os.replace(tmp_reject, reject_file)
    statistics["rejected_records"] = count_lines(reject_file)
    statistics["metric_reject_records"] = statistic_metric_rejects

    passed = bool(metrics["passed"]) and not blocking_extra

    if blocking:
        status = "failed"
    elif threshold_reasons:
        status = cfg.below_threshold_status
    elif statistics["rejected_records"] > 0:
        status = "partial"
    else:
        status = "success"

    # ``status`` is kept for the existing API contract and may be ``failed``
    # when a business metric is BLOCKED.  ``execution_status`` separates an
    # actual read/processing failure from a successfully generated but
    # not-evaluable report.
    execution_status = "failed" if blocking_extra else "success"
    evaluation_status = (
        "BLOCKED" if any(item["status"] == "BLOCKED"
                         for item in metrics["metric_status"].values())
        else "EVALUATED"
    )

    # ---------------- 报告 ----------------
    task_id = most_common(state["task_ids"]) or state["derived_task_id"]
    task_id_source = "records" if state["task_ids"] else state["derived_task_id_source"]
    pipeline_type = most_common(state["pipeline_types"], cfg.default_pipeline_type)
    pipeline_version = most_common(state["pipeline_versions"], cfg.default_pipeline_version)
    unit_type = most_common(state["unit_types"], cfg.default_unit_type)
    generated_at = now_iso()

    stage_statistics, stage_totals = _build_stage_output(state)

    statistics.update({
        "stages_seen": list(state["stages_order"]),
        "unit_types": dict(state["unit_types"]),
        "pipeline_versions": dict(state["pipeline_versions"]),
        "pipeline_types": dict(state["pipeline_types"]),
        "task_id": task_id,
        "task_id_source": task_id_source,
        "low_quality_input_count": resolved["low_quality_input_count"],
        "converted_ai_ready_count": resolved["converted_ai_ready_count"],
        "pre_final_high_quality_count": resolved["pre_final_high_quality_count"],
        "retained_high_quality_count": resolved["retained_high_quality_count"],
        "low_quality_conversion_rate": metrics["low_quality_conversion_rate"],
        "high_quality_retention_rate": metrics["high_quality_retention_rate"],
        "group_totals": group_totals,
        # [无血缘改造 2026-10-19] 报告不再引用血缘字段。
        # "lineage_ref_count": len(lineage_refs),
        "passed": passed,
        "status": status,
        "execution_status": execution_status,
        "evaluation_status": evaluation_status,
        "metric_status": metrics["metric_status"],
        "metric_scope_blocking": metric_scope_blocking,
    })

    report = {
        "report_version": cfg.report_version,
        "operator": OPERATOR_NAME,
        "operator_version": OPERATOR_VERSION,
        "uniqueId": unique_id,
        "task_id": task_id,
        "task_id_source": task_id_source,
        "pipeline_type": pipeline_type,
        "pipeline_version": pipeline_version,
        "unit_type": unit_type,
        # 混合单元口径：一期链路 09→10→11 切换统计单元（句对→子句对），分阶段口径见 stage_statistics
        "unit_type_scope": "mixed" if len(state["unit_types"]) > 1 else "single",
        "unit_types": dict(state["unit_types"]),
        "input_unit_count": resolved["low_quality_input_count"],
        "low_quality_input_count": resolved["low_quality_input_count"],
        "converted_ai_ready_count": resolved["converted_ai_ready_count"],
        "low_quality_conversion_rate": metrics["low_quality_conversion_rate"],
        "pre_final_high_quality_count": resolved["pre_final_high_quality_count"],
        "retained_high_quality_count": resolved["retained_high_quality_count"],
        "high_quality_retention_rate": metrics["high_quality_retention_rate"],
        "metric_status": metrics["metric_status"],
        "thresholds": build_thresholds(cfg),
        "passed": passed,
        "status": status,
        "execution_status": execution_status,
        "evaluation_status": evaluation_status,
        "statistics_scope": build_scope(
            cfg, resolved["sources"], task_id, pipeline_version, unit_type,
            resolved["low_quality_input_count"], resolved["converted_ai_ready_count"],
            resolved["pre_final_high_quality_count"], resolved["retained_high_quality_count"]),
        "stage_statistics": stage_statistics,
        "stage_totals": stage_totals,
        "group_statistics": group_statistics,
        "group_totals": group_totals,
        # [无血缘改造 2026-10-19] 报告去掉 lineage_refs 字段。
        # "lineage_refs": lineage_refs,
        "generated_at": generated_at,
        "failure_reasons": failure_reasons,
        "warnings": warnings,
        "statistics": statistics,
        "config": cfg_snapshot,
        "encoding": statistics["encoding"],
    }

    write_json_atomic(report_file, report)
    group_detail_file: Optional[str] = None
    if cfg.write_group_detail:
        with JsonlWriter(tmp_group) as group_writer:
            for dim in sorted(group_statistics):
                for key in sorted(group_statistics[dim]):
                    payload = group_statistics[dim][key]
                    group_writer.write({
                        "dimension": dim,
                        "key": key,
                        "units": payload["units"],
                        "high_quality_units": payload["high_quality_units"],
                    })
        os.replace(tmp_group, group_file)
        group_detail_file = str(group_file)

    log_action(logger, "汇总", "汇总阶段 %d 个；输入 %d 条（阶段 %d / 交付汇总 %d / 交付明细 %d），"
                              "拦截 %d 条（其中指标级异常 %d 条）"
               % (len(state["stages"]), statistics["input_records"],
                  statistics["stage_records"], statistics["delivery_summaries"],
                  statistics["dataset_records"], statistics["rejected_records"],
                  statistics.get("metric_reject_records", 0)))
    log_action(logger, "指标", "低质量输入 %s，AI-Ready 合格 %s，转化率 %s；高品质候选 %s，"
                              "留存 %s，留存率 %s"
               % (resolved["low_quality_input_count"], resolved["converted_ai_ready_count"],
                  metrics["low_quality_conversion_rate"],
                  resolved["pre_final_high_quality_count"],
                  resolved["retained_high_quality_count"],
                  metrics["high_quality_retention_rate"]))
    log_action(logger, "输出", "写入质量报告：%s；分组明细：%s；异常清单：%s"
               % (report_file.name, group_file.name if group_detail_file else "未生成",
                  reject_file.name))

    # [无血缘改造 2026-10-19] 不再追加血缘；改为打包本阶段产物并登记对象存储地址。
    # append_lineage(lineage_file, make_lineage(
    #     OPERATOR_NAME, OPERATOR_VERSION, unique_id, str(input_file), str(report_file),
    #     status, started, now_iso(), cfg_snapshot, statistics))
    stage_zip = storage_uri = None
    if status != "failed":
        stage_zip, storage_uri = finalize_stage_artifacts(
            tdir, OPERATOR_ID, unique_id, report_file, reject_file, log_file)
        log_action(logger, "归档", "阶段产物：%s；存储地址：%s"
                   % (Path(stage_zip).name if stage_zip else "无", storage_uri))
    log_action(logger, "完成", "算子执行完成，状态 %s，passed=%s" % (status, passed))
    close_logger(logger)

    return {
        "status": status,
        "outputFile": None if status == "failed" else str(report_file),
        "rejectFile": str(reject_file),
        "reportFile": str(report_file),
        "groupDetailFile": group_detail_file,
        "stageZip": stage_zip,
        "storageUri": storage_uri,
        "statistics": statistics,
        "message": message,
        "report": report,
    }


def main():
    parser = argparse.ArgumentParser(description="算子19：数据质量报告")
    parser.add_argument("--input", required=True, help="统计输入 JSONL 文件")
    parser.add_argument("--unique-id", default="local_demo_file1", help="任务id_文件id")
    parser.add_argument("--config", default=None, help="配置文件路径")
    parser.add_argument("--output-root", default=None, help="产物根目录")
    args = parser.parse_args()
    result = run(args.input, args.unique_id, args.output_root, args.config)
    print("status=%s passed=%s" % (result["status"],
                                   result["statistics"].get("passed")))
    print("statistics=%s" % result["statistics"])
    print("stageZip=%s storageUri=%s" % (result.get("stageZip"), result.get("storageUri")))
    if result["message"]:
        print("message=%s" % result["message"])


if __name__ == "__main__":
    main()
