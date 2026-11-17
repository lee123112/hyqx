"""算子8：敏感信息脱敏 —— 核心处理逻辑和 CLI。

职责：
1. 在 `source` / `target` 与声明允许的 metadata 字段上，同步识别并不可逆替换
   手机号、邮箱、身份证/证件号、银行卡/账号、地址等敏感片段；
2. 同一记录内「同类同值」共享占位符序号，保证双语句对可对齐复核；
3. 源/目标命中数量或规范化值不一致时写 `redaction_alignment_warning`，
   严格同步模式下该记录转入 `waiting_manual`，任何情况下都不猜测对应关系、
   不做单边修改；
4. 输出、日志、阶段归档与异常清单只保留类型、数量和规则版本，绝不写原始敏感值。

本算子不调用任何外部模型，不产生置信度或概率，也不存在伪造分数路径。
"""
import argparse
import json
import os
from pathlib import Path
from typing import Any, Dict, List, Optional, Sequence, Tuple

import yaml
from pydantic import ValidationError

from utils.artifacts import finalize_stage_artifacts
from utils.io import iter_decoded_lines, remove_quietly, select_encoding, task_dir
from utils.logger import close_logger, get_logger, log_action
from utils.redactor import (apply_field, build_value_index, compile_rules, find_hits,
                            sync_signature)
from utils.schemas import OperatorConfig
from utils.timeutil import now_iso

# [无血缘改造 2026-10-19] 血缘输出已停用，保留原导入便于回溯：
# from utils.lineage import append_lineage, make_lineage, now_iso

BASE_DIR = Path(__file__).resolve().parent
OPERATOR_NAME = "sensitive_info_deidentification"
OPERATOR_ID = "08"
OPERATOR_VERSION = "v1.0.0"
OUTPUT_FILE = "08_deidentified.jsonl"
REJECT_FILE = "08_deidentification_rejects.jsonl"
ALIGNMENT_WARNING = "redaction_alignment_warning"


class RecordRejected(Exception):
    """结构/类型/规模不合格的记录，必须进异常清单而不是静默丢弃。"""

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


def _base_statistics(cfg: Optional[OperatorConfig] = None, encoding: Optional[str] = None
                     ) -> Dict[str, Any]:
    """统计字段全部来自真实处理结果，不含文件大小等非业务口径。"""
    return {
        "input_records": 0,
        "output_records": 0,
        "rejected_records": 0,
        "hard_rejected_records": 0,
        "waiting_manual_records": 0,
        "redacted_records": 0,
        "unchanged_records": 0,
        "alignment_warnings": 0,
        "redaction_counts": {},
        "redaction_rule_version": cfg.rule_version if cfg else None,
        "total_redactions": 0,
        "encoding": encoding,
    }


def _append_reason(statistics: Dict[str, Any], reason: str) -> None:
    key = f"reason:{reason}"
    statistics[key] = statistics.get(key, 0) + 1


def _add_counts(statistics: Dict[str, Any], types: Dict[str, int]) -> None:
    bucket = statistics["redaction_counts"]
    for kind, count in types.items():
        bucket[kind] = bucket.get(kind, 0) + count
        statistics["total_redactions"] += count


def _safe_record_id(record, compiled) -> Optional[str]:
    """异常清单只保留不含敏感值的短标识；疑似敏感或超长一律置空。"""
    if not isinstance(record, dict):
        return None
    value = record.get("id")
    if isinstance(value, bool):
        return None
    if isinstance(value, int):
        value = str(value)
    if not isinstance(value, str):
        return None
    value = value.strip()
    if not value or len(value) > 128:
        return None
    if compiled and find_hits(value, compiled):
        return None
    return value


def redact_record(record: dict, cfg: OperatorConfig,
                  compiled: Optional[Sequence] = None) -> Dict[str, Any]:
    """处理一条记录。

    返回 `{"action", "record", "types", "total", "warning", "reasons"}`：
    - `action=output`：`record` 为已脱敏记录（含 metadata 脱敏证据）；
    - `action=waiting_manual`：源/目标无法确认对应关系，`record` 为 None，
      调用方只能写异常清单，不得写入主输出。
    """
    compiled = compiled if compiled is not None else compile_rules(cfg)
    if not isinstance(record, dict):
        raise RecordRejected("invalid_record")

    metadata = record.get("metadata")
    if metadata is None:
        metadata = {}
    if not isinstance(metadata, dict):
        raise RecordRejected("invalid_type:metadata")

    text_fields: List[Tuple[str, str, str]] = []
    for field in cfg.text_fields:
        if field not in record:
            raise RecordRejected(f"missing_field:{field}")
        value = record[field]
        if not isinstance(value, str):
            raise RecordRejected(f"invalid_type:{field}")
        text_fields.append((field, value, "text"))

    meta_fields: List[Tuple[str, str, str]] = []
    for field in cfg.metadata_fields:
        if field not in metadata:
            continue
        value = metadata[field]
        if not isinstance(value, str):
            raise RecordRejected(f"invalid_type:metadata.{field}")
        meta_fields.append((field, value, "metadata"))

    all_fields: List[Tuple[str, str]] = [(name, text) for name, text, _ in text_fields + meta_fields]
    plan = {name: find_hits(text, compiled) for name, text in all_fields}
    total = sum(len(hits) for hits in plan.values())
    if total > cfg.max_hits_per_record:
        # 命中规模异常时不降级为「部分替换」，避免残余敏感值被误当成已脱敏。
        raise RecordRejected("hit_limit_exceeded")

    index = build_value_index(all_fields, plan)

    types: Dict[str, int] = {}
    for name, _text in all_fields:
        for hit in plan[name]:
            kind = str(hit["type"])
            types[kind] = types.get(kind, 0) + 1
    types = dict(sorted(types.items()))

    warning = False
    if cfg.sync_check_mode != "off":
        warning = sync_signature(plan["source"], cfg.sync_check_mode) != \
            sync_signature(plan["target"], cfg.sync_check_mode)

    if warning and cfg.strict_sync:
        return {"action": "waiting_manual", "record": None, "types": types,
                "total": total, "warning": True, "reasons": [ALIGNMENT_WARNING]}

    new_record = dict(record)
    for name, text, kind in text_fields:
        new_record[name] = apply_field(text, plan[name], index, cfg.placeholder_template)

    new_metadata = dict(metadata)
    for name, text, kind in meta_fields:
        new_metadata[name] = apply_field(text, plan[name], index, cfg.placeholder_template)
    if total == 0:
        status = "unchanged"
    elif warning:
        status = "redacted_with_warning"
    else:
        status = "redacted"
    new_metadata["redaction_status"] = status
    new_metadata["redaction_types"] = types
    new_metadata["redaction_rule_version"] = cfg.rule_version
    new_metadata["redaction_total"] = total
    new_metadata["redaction_alignment_warning"] = bool(warning)
    new_record["metadata"] = new_metadata

    return {"action": "output", "record": new_record, "types": types,
            "total": total, "warning": warning, "reasons": []}


def _reject_entry(lineno: int, record, reasons: List[str], status: str,
                  cfg: OperatorConfig, compiled, types: Optional[Dict[str, int]] = None,
                  actions: Optional[List[dict]] = None) -> dict:
    """异常清单条目：只含定位信息、原因、类型计数与规则版本，不含任何原文。"""
    entry: Dict[str, Any] = {
        "line": lineno,
        "id": _safe_record_id(record, compiled),
        "record_omitted": "sensitive_content",
        "status": status,
        "reasons": reasons,
        "redaction_rule_version": cfg.rule_version,
    }
    if types:
        entry["redaction_types"] = types
    return entry


def _failed_result(statistics: Dict[str, Any], message: str, logger) -> dict:
    log_action(logger, "失败", message)
    # [无血缘改造 2026-10-19] 血缘输出已停用，保留原实现便于回溯；不再生成 lineage 文件。
    # append_lineage(lineage_file, make_lineage(
    #     OPERATOR_NAME, OPERATOR_VERSION, unique_id, str(input_file), str(out_file),
    #     "failed", started_at, now_iso(), cfg_snapshot, statistics))
    close_logger(logger)
    return {"status": "failed", "outputFile": None, "rejectFile": None,
            "stageZip": None, "storageUri": None, "statistics": statistics,
            "message": message}


def run(input_file: str, unique_id: str, output_root=None, config_path=None,
        overrides: Optional[dict] = None, logger=None) -> dict:
    """流式脱敏主流程：写主输出与异常清单，生成阶段产物，返回真实统计。"""
    output_root = Path(output_root) if output_root else Path(
        os.getenv("OUTPUT_DIR", BASE_DIR / "output"))
    tdir = task_dir(output_root, unique_id)
    out_file = tdir / OUTPUT_FILE
    reject_file = tdir / REJECT_FILE
    tmp_file = tdir / (OUTPUT_FILE + ".tmp")
    reject_tmp = tdir / (REJECT_FILE + ".tmp")
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
        # [无血缘改造 2026-10-19] 血缘输出已停用，保留原实现便于回溯。
        # append_lineage(lineage_file, make_lineage(
        #     OPERATOR_NAME, OPERATOR_VERSION, unique_id, str(input_file), str(out_file),
        #     "failed", now_iso(), now_iso(), {"config_error": message}, statistics))
        close_logger(logger)
        return {"status": "failed", "outputFile": None, "rejectFile": None,
                "stageZip": None, "storageUri": None, "statistics": statistics,
                "message": message}

    started = now_iso()

    # 规则编译与配置同属启动门禁：正则非法一律 failed，不得带病运行。
    try:
        compiled = compile_rules(cfg)
    except ValueError as exc:
        statistics = _base_statistics(cfg)
        return _failed_result(statistics, f"规则编译失败：{type(exc).__name__}", logger)

    # 输入路径与输出/临时/异常清单路径相同时会截断源文件，直接拒绝执行。
    try:
        input_path = Path(input_file).resolve()
    except OSError:
        input_path = None
    if input_path is not None and input_path in {
            out_file.resolve(), tmp_file.resolve(), reject_file.resolve(),
            reject_tmp.resolve()}:
        statistics = _base_statistics(cfg)
        return _failed_result(statistics,
                              "输入路径与输出路径相同，拒绝执行以避免截断源文件",
                              logger)

    log_action(logger, "输入", f"加载输入文件：{Path(input_file).name}")
    log_action(logger, "配置",
               f"文本字段={cfg.text_fields} 元数据字段={cfg.metadata_fields} "
               f"启用规则={len(compiled)} 同步检查={cfg.sync_check_mode} "
               f"严格同步={cfg.strict_sync} 规则版本={cfg.rule_version}")

    try:
        encoding = select_encoding(input_file, cfg.input_encodings)
    except (OSError, ValueError) as exc:
        statistics = _base_statistics(cfg)
        return _failed_result(statistics,
                              f"输入文件编码处理失败：{type(exc).__name__}，请检查文件编码",
                              logger)

    statistics = _base_statistics(cfg, encoding)
    log_action(logger, "读取", f"解码输入文件：{encoding}")

    try:
        with open(tmp_file, "w", encoding="utf-8") as output_writer, \
                open(reject_tmp, "w", encoding="utf-8") as reject_writer:
            for lineno, line in enumerate(iter_decoded_lines(input_file, encoding), 1):
                if not line.strip():
                    continue
                statistics["input_records"] += 1
                try:
                    record = json.loads(line)
                except json.JSONDecodeError:
                    reject_writer.write(json.dumps(_reject_entry(
                        lineno, None, ["invalid_json"], "rejected", cfg, compiled),
                        ensure_ascii=False) + "\n")
                    statistics["rejected_records"] += 1
                    statistics["hard_rejected_records"] += 1
                    _append_reason(statistics, "invalid_json")
                    continue

                try:
                    outcome = redact_record(record, cfg, compiled)
                except RecordRejected as exc:
                    reject_writer.write(json.dumps(_reject_entry(
                        lineno, record, [exc.reason], "rejected", cfg, compiled),
                        ensure_ascii=False) + "\n")
                    statistics["rejected_records"] += 1
                    statistics["hard_rejected_records"] += 1
                    _append_reason(statistics, exc.reason)
                    continue

                if outcome["action"] == "waiting_manual":
                    reject_writer.write(json.dumps(_reject_entry(
                        lineno, record, outcome["reasons"], "waiting_manual", cfg, compiled,
                        outcome["types"]), ensure_ascii=False) + "\n")
                    statistics["rejected_records"] += 1
                    statistics["waiting_manual_records"] += 1
                    _append_reason(statistics, ALIGNMENT_WARNING)
                    continue

                output_writer.write(json.dumps(outcome["record"], ensure_ascii=False) + "\n")
                statistics["output_records"] += 1
                if outcome["total"] == 0:
                    statistics["unchanged_records"] += 1
                else:
                    statistics["redacted_records"] += 1
                    _add_counts(statistics, outcome["types"])
                if outcome["warning"]:
                    statistics["alignment_warnings"] += 1
    except (OSError, UnicodeDecodeError) as exc:
        remove_quietly(tmp_file)
        remove_quietly(reject_tmp)
        return _failed_result(statistics,
                              f"输入文件读取失败：{type(exc).__name__}",
                              logger)

    if statistics["input_records"] == 0:
        remove_quietly(tmp_file)
        remove_quietly(reject_tmp)
        status, message = "failed", "输入文件为空或无有效记录"
        log_action(logger, "失败", message)
    else:
        os.replace(tmp_file, out_file)
        os.replace(reject_tmp, reject_file)
        message = ""
        if statistics["hard_rejected_records"] > 0:
            status = "partial"
        elif statistics["waiting_manual_records"] > 0:
            status = "waiting_manual"
        else:
            status = "success"
        counts = statistics["redaction_counts"]
        summary = "、".join(f"{kind}={count}" for kind, count in sorted(counts.items())) or "无"
        log_action(logger, "处理",
                   f"脱敏完成：输出 {statistics['output_records']} 条，"
                   f"异常 {statistics['hard_rejected_records']} 条，"
                   f"待人工 {statistics['waiting_manual_records']} 条，"
                   f"替换 {statistics['total_redactions']} 处（{summary}）")
        log_action(logger, "输出",
                   f"写入输出文件：{out_file.name}；异常清单：{reject_file.name}")

    # [无血缘改造 2026-10-19] 不再追加血缘；改为打包本阶段产物并登记对象存储地址。
    # append_lineage(lineage_file, make_lineage(
    #     OPERATOR_NAME, OPERATOR_VERSION, unique_id, str(input_file), str(out_file),
    #     status, started, now_iso(), cfg_snapshot, statistics))
    stage_zip = storage_uri = None
    if status != "failed":
        stage_zip, storage_uri = finalize_stage_artifacts(
            tdir, OPERATOR_ID, unique_id, out_file, reject_file, tdir / "run.log")
        log_action(logger, "归档",
                   f"阶段产物：{Path(stage_zip).name if stage_zip else '无'}；存储地址：{storage_uri}")
    log_action(logger, "完成", f"算子执行完成，状态 {status}")
    close_logger(logger)
    return {"status": status,
            "outputFile": str(out_file) if status != "failed" else None,
            "rejectFile": str(reject_file) if status != "failed" else None,
            "stageZip": stage_zip, "storageUri": storage_uri,
            "statistics": statistics, "message": message}


def main():
    parser = argparse.ArgumentParser(description="算子8：敏感信息脱敏")
    parser.add_argument("--input", required=True, help="输入句对 JSONL 文件")
    parser.add_argument("--unique-id", default="local_demo_file1", help="任务id_文件id")
    parser.add_argument("--config", default=None, help="配置文件路径")
    parser.add_argument("--output-root", default=None, help="产物根目录")
    args = parser.parse_args()
    result = run(args.input, args.unique_id, args.output_root, args.config)
    print(f"status={result['status']} statistics={result['statistics']}")
    print(f"stageZip={result.get('stageZip')} storageUri={result.get('storageUri')}")


if __name__ == "__main__":
    main()
