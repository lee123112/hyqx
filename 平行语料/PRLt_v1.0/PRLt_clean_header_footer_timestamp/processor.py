"""算子5：页眉页脚与时间戳清理 —— 核心处理逻辑和 CLI。

只做版式噪声清理，按配置顺序执行：

1. 整行字幕时间戳（含 `-->` 的时间行恒为字幕噪声，可删）；
2. 整行页码（裸数字等弱模式只在记录首/尾区域生效）；
3. 跨记录高频重复行（需达到 min_repeat_ratio 且位于首尾区域）；
4. 用户自定义页眉页脚整行模式；
5. 具备明确边界的行内字幕时间片段（可选行内页码）。

正文中的日期、时间与版本号默认保留；模式命中但可能影响正文时，按
`uncertain_action` 保留原文并写证据，绝不静默删除。本节点不产生任何模型分数。

[无血缘改造 2026-10-19] 不再生成/追加血缘文件；成功/部分成功/waiting_manual 返回前打包阶段产物
artifacts/stage_05_<uniqueId>.zip 并返回 stageZip/storageUri。
"""
import argparse
import json
import os
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

import yaml
from pydantic import ValidationError

from utils.artifacts import finalize_stage_artifacts
from utils.io import iter_decoded_lines, select_encoding, task_dir
from utils.layout import (
    INNER_WS_RE,
    REGION_BODY,
    compile_patterns,
    match_whole_line,
    normalize_key,
    region_of,
    scan_repeated_lines,
    split_lines,
    strip_inline,
)
from utils.logger import close_logger, get_logger, log_action
from utils.schemas import OperatorConfig
from utils.timeutil import now_iso

# [无血缘改造 2026-10-19] 血缘输出已停用，保留原导入便于回溯：
# from utils.lineage import append_lineage, make_lineage, now_iso

BASE_DIR = Path(__file__).resolve().parent
OPERATOR_NAME = "layout_noise_cleanup"
OPERATOR_ID = "05"
OPERATOR_VERSION = "v1.0.0"
OUTPUT_FILE = "05_layout_cleaned.jsonl"
REJECT_FILE = "05_layout_rejects.jsonl"

_LINE_KINDS = ("timestamp", "page_number", "header_footer", "repeated_line", "blank_line")


class RecordRejected(Exception):
    """结构/类型不合格的记录，必须进异常清单而不是静默丢弃。"""

    def __init__(self, reason: str):
        super().__init__(reason)
        self.reason = reason


def load_config(config_path=None, overrides: Optional[dict] = None
                ) -> Tuple[OperatorConfig, dict]:
    """读取并校验配置，返回 (配置对象, 血缘用配置快照)。"""
    cfg_path = Path(config_path) if config_path else BASE_DIR / "configs" / "config.yaml"
    raw = yaml.safe_load(cfg_path.read_text(encoding="utf-8")) or {}
    if not isinstance(raw, dict):
        raise ValueError("配置文件必须是对象")
    merged = {key: value for key, value in raw.items() if key != "operator"}
    if overrides:
        merged.update(overrides)
    cfg = OperatorConfig(**merged)
    snapshot = {"operator": raw.get("operator", {}), **cfg.model_dump(mode="json")}
    return cfg, snapshot


# --------------------------------------------------------------------------
# 单字段清理
# --------------------------------------------------------------------------


def clean_text(text: str, field: str, cfg: OperatorConfig, repeated_keys: Dict,
               compiled: Dict) -> Tuple[str, List[dict], List[dict]]:
    """清理一个文本字段。

    返回 (清理后文本, 删除证据, 不确定证据)。删除证据只记录模式名、行号、
    区域与删除字符数，绝不写入原文。
    """
    lines = split_lines(text)
    total = len(lines)
    kept: List[str] = []
    removals: List[dict] = []
    uncertain: List[dict] = []

    for index, line in enumerate(lines):
        stripped = line.strip()
        region = region_of(index, total, cfg.head_lines, cfg.tail_lines)

        if not stripped:
            if cfg.remove_blank_lines:
                removals.append({
                    "field": field, "kind": "blank_line", "pattern": "blank_line",
                    "scope": "line", "line_index": index, "region": region,
                    "hits": 1, "removed_chars": len(line),
                })
                continue
            kept.append(line)
            continue

        matched = None
        if cfg.remove_timestamps:
            matched = match_whole_line(stripped, compiled["timestamp"], "timestamp")
        if matched is None and cfg.remove_page_numbers:
            matched = match_whole_line(stripped, compiled["page_number"], "page_number")
        if matched is None and cfg.remove_header_footer:
            matched = match_whole_line(stripped, compiled["header_footer"], "header_footer")

        if matched is not None:
            name, kind, boundary_only = matched
            if len(stripped) > cfg.max_whole_line_chars:
                # 命中模式但整行过长，很可能是正文，不能按页眉页脚删除。
                uncertain.append({
                    "field": field, "kind": kind, "pattern": name, "line_index": index,
                    "region": region, "reason": "pattern_line_too_long",
                    "matched_chars": len(stripped),
                })
                kept.append(line)
                continue
            if boundary_only and region == REGION_BODY:
                # 只在首尾区域生效的模式命中正文：按正文保留，不删也不报不确定。
                kept.append(line)
                continue
            removals.append({
                "field": field, "kind": kind, "pattern": name, "scope": "line",
                "line_index": index, "region": region, "hits": 1,
                "removed_chars": len(line),
            })
            continue

        if cfg.remove_repeated_lines:
            key = normalize_key(line)
            if key and repeated_keys.get((field, key)):
                if region != REGION_BODY:
                    removals.append({
                        "field": field, "kind": "repeated_line", "pattern": "repeated_line",
                        "scope": "line", "line_index": index, "region": region,
                        "hits": 1, "removed_chars": len(line),
                    })
                    continue
                kept.append(line)
                continue

        new_line = line
        removed_chars = 0
        if cfg.remove_inline_timestamps:
            new_line, removed = strip_inline(
                new_line, field, index, region, compiled["inline_timestamp"], "timestamp", removals)
            removed_chars += removed
        if cfg.remove_inline_page_markers:
            new_line, removed = strip_inline(
                new_line, field, index, region, compiled["inline_page"], "page_number", removals)
            removed_chars += removed
        if removed_chars:
            new_line = INNER_WS_RE.sub(" ", new_line).strip()
            if not new_line:
                continue
        kept.append(new_line)

    cleaned = "\n".join(kept)
    if cfg.protect_whole_field and text.strip() and not cleaned.strip():
        # 整字段会被清空：无法区分「整条都是版式噪声」与「正文恰好命中模式」，
        # 保留原文并转人工，绝不静默删除正文。
        uncertain.append({
            "field": field, "kind": "field_protection",
            "reason": "whole_field_would_be_empty", "removed_lines": len(removals),
        })
        return text, [], uncertain
    return cleaned, removals, uncertain


# --------------------------------------------------------------------------
# 单条记录处理
# --------------------------------------------------------------------------


def _process_record(record: dict, cfg: OperatorConfig, repeated_keys: Dict,
                    compiled: Dict) -> dict:
    """校验并清理一条记录；不修改入参 record（转人工时要保留原文）。"""
    raw_metadata = record.get("metadata")
    if raw_metadata is None:
        raw_metadata = {}
    if not isinstance(raw_metadata, dict):
        raise RecordRejected("invalid_type:metadata")
    for field in cfg.text_fields:
        if field not in record:
            raise RecordRejected(f"missing_field:{field}")
        if not isinstance(record[field], str):
            raise RecordRejected(f"invalid_type:{field}")
        if not record[field].strip():
            raise RecordRejected(f"empty_field:{field}")

    metadata = dict(raw_metadata)
    output_record = dict(record)
    output_record["metadata"] = metadata

    removed_patterns: List[dict] = []
    uncertain_matches: List[dict] = []
    kind_counts: Dict[str, int] = {}
    fields_cleaned: List[str] = []
    lines_removed = 0
    inline_removed = 0
    chars_removed = 0

    for field in cfg.text_fields:
        cleaned, removals, uncertain = clean_text(
            record[field], field, cfg, repeated_keys, compiled)
        if cleaned != record[field]:
            fields_cleaned.append(field)
        output_record[field] = cleaned
        removed_patterns.extend(removals)
        uncertain_matches.extend(uncertain)
        for item in removals:
            if item.get("scope") == "inline":
                inline_removed += 1
            else:
                lines_removed += 1
            chars_removed += item["removed_chars"]
            kind = item["kind"]
            kind_counts[kind] = kind_counts.get(kind, 0) + 1

    if uncertain_matches:
        status = "waiting_manual"
    elif removed_patterns:
        status = "cleaned"
    else:
        status = "unchanged"

    metadata["layout_cleaning"] = {
        "removed_lines": lines_removed,
        "inline_removed": inline_removed,
        "removed_chars": chars_removed,
        "removed_by_kind": kind_counts,
        "fields_cleaned": fields_cleaned,
        "uncertain_matches": len(uncertain_matches),
    }
    metadata["removed_patterns"] = removed_patterns
    metadata["cleaning_status"] = status

    return {
        "original": record,
        "output": output_record,
        "status": status,
        "removed_patterns": removed_patterns,
        "uncertain_matches": uncertain_matches,
        "lines_removed": lines_removed,
        "inline_removed": inline_removed,
        "chars_removed": chars_removed,
        "kind_counts": kind_counts,
        "changed": bool(removed_patterns),
    }


# --------------------------------------------------------------------------
# 统计与异常清单
# --------------------------------------------------------------------------


def _base_statistics(encoding=None) -> Dict[str, Any]:
    statistics: Dict[str, Any] = {
        "total": 0,
        "passed": 0,
        "rejected": 0,
        "hard_rejected": 0,
        "uncertain_records": 0,
        "records_waiting_manual": 0,
        "records_changed": 0,
        "lines_removed": 0,
        "inline_removed": 0,
        "chars_removed": 0,
        "suppressed_removals_in_rejects": 0,
        "repeated_lines_detected": 0,
        "repeated_line_candidates": 0,
        "frequency_scan_records": 0,
        "frequency_scan_truncated": False,
        "encoding": encoding,
    }
    for kind in _LINE_KINDS:
        statistics[f"removed_by_kind:{kind}"] = 0
    return statistics


def _append_reason(statistics: Dict[str, Any], reason: str) -> None:
    key = f"reason:{reason}"
    statistics[key] = statistics.get(key, 0) + 1


def _append_kind(statistics: Dict[str, Any], kind: str, count: int) -> None:
    key = f"removed_by_kind:{kind}"
    statistics[key] = statistics.get(key, 0) + count


def _write_reject(handle, lineno: int, record, reasons: List[str],
                  removed_patterns, uncertain_matches) -> None:
    entry = {
        "line": lineno,
        "record_id": record.get("id") if isinstance(record, dict) else None,
        "record": record,
        "reject_type": "uncertain" if uncertain_matches else "hard",
        "reasons": list(reasons),
    }
    if removed_patterns:
        entry["removed_patterns"] = removed_patterns
    if uncertain_matches:
        entry["uncertain_matches"] = uncertain_matches
    handle.write(json.dumps(entry, ensure_ascii=False) + "\n")


def _remove_quietly(path) -> None:
    try:
        Path(path).unlink()
    except OSError:
        pass


def _failed_result(unique_id: str, input_file: str, out_file: Path,
                   statistics: Dict[str, Any], message: str, logger,
                   started_at: str, cfg_snapshot: dict) -> dict:
    log_action(logger, "失败", message)
    # [无血缘改造 2026-10-19] 血缘输出已停用，保留原实现便于回溯；不再生成 lineage 文件。
    # append_lineage(lineage_file, make_lineage(
    #     OPERATOR_NAME, OPERATOR_VERSION, unique_id, str(input_file), str(out_file),
    #     "failed", started_at, now_iso(), cfg_snapshot, statistics))
    close_logger(logger)
    return {"status": "failed", "outputFile": None, "rejectFile": None,
            "stageZip": None, "storageUri": None,
            "statistics": statistics, "message": message}


# --------------------------------------------------------------------------
# 主流程
# --------------------------------------------------------------------------


def run(input_file: str, unique_id: str, output_root=None, config_path=None,
        overrides: Optional[dict] = None, logger=None) -> dict:
    output_root = Path(output_root) if output_root else Path(
        os.getenv("OUTPUT_DIR", BASE_DIR / "output"))
    tdir = task_dir(output_root, unique_id)
    out_file = tdir / OUTPUT_FILE
    reject_file = tdir / REJECT_FILE
    tmp_file = tdir / (OUTPUT_FILE + ".tmp")
    # [无血缘改造 2026-10-19] 不再生成 lineage 文件。
    # lineage_file = tdir / "lineage.json"
    logger = logger or get_logger(f"{OPERATOR_NAME}.{unique_id}", tdir / "run.log")

    try:
        cfg, cfg_snapshot = load_config(config_path, overrides)
    except (AttributeError, OSError, TypeError, ValueError, ValidationError,
            yaml.YAMLError) as exc:
        message = f"配置错误：{type(exc).__name__}"
        statistics = _base_statistics()
        log_action(logger, "失败", message)
        # [无血缘改造 2026-10-19] 血缘输出已停用，保留原实现便于回溯；不再生成 lineage 文件。
        # append_lineage(lineage_file, make_lineage(
        #     OPERATOR_NAME, OPERATOR_VERSION, unique_id, str(input_file), str(out_file),
        #     "failed", now_iso(), now_iso(), {"config_error": message}, statistics))
        close_logger(logger)
        return {"status": "failed", "outputFile": None, "rejectFile": None,
                "stageZip": None, "storageUri": None, "statistics": statistics,
                "message": message}

    started = now_iso()

    # 输入路径等于输出/临时/异常清单路径会截断源文件，直接拒绝执行。
    try:
        input_path = Path(input_file).resolve()
    except OSError:
        input_path = None
    if input_path is not None and input_path in {
            out_file.resolve(), tmp_file.resolve(), reject_file.resolve()}:
        statistics = _base_statistics()
        return _failed_result(unique_id, input_file, out_file, statistics,
                              "输入路径与输出路径相同，拒绝执行以避免截断源文件",
                              logger, started, cfg_snapshot)

    log_action(logger, "输入", f"加载输入文件：{Path(input_file).name}")
    log_action(logger, "配置",
               f"文本字段={cfg.text_fields} 时间戳={cfg.remove_timestamps}"
               f"(行内={cfg.remove_inline_timestamps}) 页码={cfg.remove_page_numbers}"
               f"(行内={cfg.remove_inline_page_markers}) 页眉页脚={cfg.remove_header_footer} "
               f"重复行={cfg.remove_repeated_lines}(ratio={cfg.min_repeat_ratio}) "
               f"不确定动作={cfg.uncertain_action}")

    try:
        encoding = select_encoding(input_file, cfg.input_encodings)
    except (OSError, ValueError) as exc:
        statistics = _base_statistics()
        return _failed_result(unique_id, input_file, out_file, statistics,
                              f"输入文件编码处理失败：{type(exc).__name__}，请检查文件编码",
                              logger, started, cfg_snapshot)

    statistics = _base_statistics(encoding)
    log_action(logger, "读取", f"解码输入文件：{encoding}")

    compiled = compile_patterns(cfg)
    repeated_keys: Dict = {}
    if cfg.remove_repeated_lines:
        try:
            scan = scan_repeated_lines(input_file, encoding, cfg)
        except (OSError, UnicodeDecodeError) as exc:
            return _failed_result(unique_id, input_file, out_file, statistics,
                                  f"重复行扫描失败：{type(exc).__name__}",
                                  logger, started, cfg_snapshot)
        repeated_keys = scan["keys"]
        statistics["frequency_scan_records"] = scan["scanned"]
        statistics["repeated_line_candidates"] = scan["candidates"]
        statistics["frequency_scan_truncated"] = scan["truncated"]
        statistics["repeated_lines_detected"] = len(repeated_keys)
        log_action(logger, "扫描",
                   f"重复行扫描完成：记录 {scan['scanned']} 条，候选 {scan['candidates']} 条，"
                   f"命中高频重复行 {len(repeated_keys)} 条，频率表截断={scan['truncated']}")

    try:
        with open(tmp_file, "w", encoding="utf-8", newline="\n") as output_handle, \
                open(reject_file, "w", encoding="utf-8", newline="\n") as reject_handle:
            for lineno, line in enumerate(iter_decoded_lines(input_file, encoding), 1):
                if not line.strip():
                    continue
                statistics["total"] += 1
                try:
                    record = json.loads(line)
                except json.JSONDecodeError:
                    _write_reject(reject_handle, lineno, None, ["invalid_json"], None, None)
                    statistics["rejected"] += 1
                    statistics["hard_rejected"] += 1
                    _append_reason(statistics, "invalid_json")
                    continue
                if not isinstance(record, dict):
                    _write_reject(reject_handle, lineno, None, ["invalid_record"], None, None)
                    statistics["rejected"] += 1
                    statistics["hard_rejected"] += 1
                    _append_reason(statistics, "invalid_record")
                    continue

                try:
                    outcome = _process_record(record, cfg, repeated_keys, compiled)
                except RecordRejected as exc:
                    _write_reject(reject_handle, lineno, record, [exc.reason], None, None)
                    statistics["rejected"] += 1
                    statistics["hard_rejected"] += 1
                    _append_reason(statistics, exc.reason)
                    continue

                uncertain = outcome["uncertain_matches"]
                if uncertain and cfg.uncertain_action == "waiting_manual":
                    reasons = sorted({f"waiting_manual:{item['reason']}" for item in uncertain})
                    _write_reject(reject_handle, lineno, record, reasons,
                                  outcome["removed_patterns"], uncertain)
                    statistics["rejected"] += 1
                    statistics["uncertain_records"] += 1
                    statistics["records_waiting_manual"] += 1
                    statistics["suppressed_removals_in_rejects"] += len(outcome["removed_patterns"])
                    for item in uncertain:
                        _append_reason(statistics, f"uncertain:{item['reason']}")
                    continue

                if uncertain:
                    statistics["uncertain_records"] += 1
                    statistics["records_waiting_manual"] += 1
                    for item in uncertain:
                        _append_reason(statistics, f"uncertain:{item['reason']}")

                output_handle.write(json.dumps(outcome["output"], ensure_ascii=False) + "\n")
                statistics["passed"] += 1
                if outcome["changed"]:
                    statistics["records_changed"] += 1
                statistics["lines_removed"] += outcome["lines_removed"]
                statistics["inline_removed"] += outcome["inline_removed"]
                statistics["chars_removed"] += outcome["chars_removed"]
                for kind, count in outcome["kind_counts"].items():
                    _append_kind(statistics, kind, count)
    except (OSError, UnicodeDecodeError) as exc:
        _remove_quietly(tmp_file)
        _remove_quietly(reject_file)
        return _failed_result(unique_id, input_file, out_file, statistics,
                              f"输入文件读取失败：{type(exc).__name__}",
                              logger, started, cfg_snapshot)

    if statistics["total"] == 0:
        _remove_quietly(tmp_file)
        _remove_quietly(reject_file)
        status, message = "failed", "输入文件为空或无有效记录"
    else:
        os.replace(tmp_file, out_file)
        message = ""
        if statistics["hard_rejected"] > 0:
            status = "partial"
        elif statistics["uncertain_records"] > 0:
            status = "waiting_manual"
        else:
            status = "success"
        log_action(logger, "处理",
                   f"版式清理完成：通过 {statistics['passed']} 条，拦截 {statistics['rejected']} 条，"
                   f"删除整行 {statistics['lines_removed']} 行、行内 {statistics['inline_removed']} 处，"
                   f"待人工 {statistics['uncertain_records']} 条")
        log_action(logger, "输出",
                   f"写入输出文件：{out_file.name}；异常清单：{reject_file.name}")

    if status == "failed":
        log_action(logger, "失败", message)

    # [无血缘改造 2026-10-19] 不再追加血缘；成功/部分成功/waiting_manual 一律打包阶段产物。
    # append_lineage(lineage_file, make_lineage(
    #     OPERATOR_NAME, OPERATOR_VERSION, unique_id, str(input_file), str(out_file),
    #     status, started, now_iso(), cfg_snapshot, statistics))
    stage_zip, storage_uri = (None, None)
    if status != "failed":
        stage_zip, storage_uri = finalize_stage_artifacts(
            tdir, OPERATOR_ID, unique_id, out_file, reject_file, tdir / "run.log")
        log_action(logger, "归档",
                   f"阶段产物：{Path(stage_zip).name if stage_zip else '无'}；存储地址：{storage_uri}")
        log_action(logger, "完成", f"算子执行完成，状态 {status}")
    close_logger(logger)

    return {
        "status": status,
        "outputFile": str(out_file) if status != "failed" else None,
        "rejectFile": str(reject_file) if status != "failed" else None,
        "stageZip": stage_zip,
        "storageUri": storage_uri,
        "statistics": statistics,
        "message": message,
    }


def main():
    parser = argparse.ArgumentParser(description="算子5：页眉页脚与时间戳清理")
    parser.add_argument("--input", required=True, help="输入句对 JSONL 文件")
    parser.add_argument("--unique-id", default="local_demo_file1", help="任务id_文件id")
    parser.add_argument("--config", default=None, help="配置文件路径")
    parser.add_argument("--output-root", default=None, help="产物根目录")
    args = parser.parse_args()
    result = run(args.input, args.unique_id, args.output_root, args.config)
    print(f"status={result['status']}")
    print("statistics=" + json.dumps(result["statistics"], ensure_ascii=False, sort_keys=True))
    print(f"stageZip={result.get('stageZip')} storageUri={result.get('storageUri')}")


if __name__ == "__main__":
    main()
