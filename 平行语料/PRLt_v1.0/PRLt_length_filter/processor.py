"""算子7：长度过滤 —— 核心处理逻辑和 CLI。

职责：按源文/目标文字符长度和长度比例过滤过短、过长及比例异常的句对，
保留可追踪的过滤原因。只做规则过滤：不改写文本、不做语言检测、不做去重、
不评价语义等价，也不输出任何模型分数。
"""
import argparse
import codecs
import json
import os
import unicodedata
from pathlib import Path
from typing import Any, Dict, Iterator, List, Optional, Tuple

import yaml
from pydantic import ValidationError

from utils.artifacts import finalize_stage_artifacts
from utils.io import JsonlWriter, task_dir
from utils.logger import close_logger, get_logger, log_action
from utils.schemas import OperatorConfig
from utils.timeutil import now_iso

# [无血缘改造 2026-10-19] 血缘输出已停用，保留原导入便于回溯：
# from utils.lineage import append_lineage, make_lineage, now_iso

BASE_DIR = Path(__file__).resolve().parent
OPERATOR_NAME = "length_filtering"
OPERATOR_ID = "06"
OPERATOR_VERSION = "v1.1.0"

OUTPUT_NAME = "06_length_filtered.jsonl"
REJECT_NAME = "06_length_rejects.jsonl"
# 上游为算子6 语言检测；仅用于日志提示和默认输入优先级，不作为硬约束。
EXPECTED_INPUT_NAME = "05_layout_cleaned.jsonl"

SOURCE_FIELD = "source"
TARGET_FIELD = "target"

# 稳定原因名 → 统计字段名。
REASON_COUNTERS = {
    "empty_source": "empty_source",
    "empty_target": "empty_target",
    "source_too_short": "source_too_short",
    "source_too_long": "source_too_long",
    "target_too_short": "target_too_short",
    "target_too_long": "target_too_long",
    "length_ratio_below_min": "length_ratio_below_min",
    "length_ratio_above_max": "length_ratio_above_max",
}

OBSERVED_KEYS = ("source_chars", "target_chars", "length_ratio")


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
    """Incrementally decode a file and yield one logical line at a time."""
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
    """Find the first configured codec that strictly decodes the complete file."""
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


def count_chars(value: str) -> int:
    """NFC 归一后按 Python 字符计数；不删除标点、空格，不按字节统计。"""
    if not isinstance(value, str):
        raise TypeError("文本字段必须是字符串")
    return len(unicodedata.normalize("NFC", value))


def measure_lengths(record: dict) -> Dict[str, Any]:
    """观测一条记录的 source_chars、target_chars 和 length_ratio。"""
    source_chars = count_chars(record.get(SOURCE_FIELD))
    target_chars = count_chars(record.get(TARGET_FIELD))
    # 源文为空时用 max(source_chars, 1) 兜底，保证比例计算不除零。
    length_ratio = round(target_chars / max(source_chars, 1), 6)
    return {
        "source_chars": source_chars,
        "target_chars": target_chars,
        "length_ratio": length_ratio,
    }


def evaluate_length(stats: Dict[str, Any], cfg: OperatorConfig) -> List[str]:
    """返回稳定的违规原因列表；闭区间，边界值视为通过。"""
    source_chars = stats["source_chars"]
    target_chars = stats["target_chars"]
    length_ratio = stats["length_ratio"]
    reasons: List[str] = []

    if source_chars == 0:
        reasons.append("empty_source")
    elif source_chars < cfg.min_source_chars:
        reasons.append("source_too_short")
    elif source_chars > cfg.max_source_chars:
        reasons.append("source_too_long")

    if target_chars == 0:
        reasons.append("empty_target")
    elif target_chars < cfg.min_target_chars:
        reasons.append("target_too_short")
    elif target_chars > cfg.max_target_chars:
        reasons.append("target_too_long")

    # 任一端为空时比例没有意义，只报空值原因，避免重复计数。
    if source_chars > 0 and target_chars > 0:
        if length_ratio < cfg.min_length_ratio:
            reasons.append("length_ratio_below_min")
        elif length_ratio > cfg.max_length_ratio:
            reasons.append("length_ratio_above_max")
    return reasons


def resolve_violation_action(
        record: dict, stats: Dict[str, Any], reasons: List[str], cfg: OperatorConfig,
) -> Tuple[str, Optional[str], Optional[Any]]:
    """Resolve pass/warn/reject without hiding the original violation reasons."""
    if not reasons:
        return "pass", None, None
    if cfg.on_violation == "warn":
        return "warn", None, None

    ratio_reasons = {"length_ratio_below_min", "length_ratio_above_max"}
    if any(reason not in ratio_reasons for reason in reasons):
        return "reject", None, None

    source_lang = record.get("source_lang")
    target_lang = record.get("target_lang")
    if not isinstance(source_lang, str) or not isinstance(target_lang, str):
        return "reject", None, None
    pair = f"{source_lang.lower()}-{target_lang.lower()}"
    policy = cfg.language_pair_ratio_policies.get(pair)
    if policy is None:
        return "reject", None, None

    ratio = stats["length_ratio"]
    if ratio < policy.hard_min_length_ratio or ratio > policy.hard_max_length_ratio:
        return "reject", pair, policy
    return policy.soft_violation_action, pair, policy


def _base_statistics(encoding=None, on_violation=None) -> Dict[str, Any]:
    statistics: Dict[str, Any] = {
        "total": 0,
        "passed": 0,
        "rejected": 0,
        "warned": 0,
        "language_pair_soft_warnings": 0,
        "hard_ratio_rejections": 0,
        "on_violation": on_violation,
        "encoding": encoding,
    }
    for counter in REASON_COUNTERS.values():
        statistics[counter] = 0
    for name in ("source_chars", "target_chars", "length_ratio"):
        statistics[f"min_observed_{name}"] = None
        statistics[f"max_observed_{name}"] = None
    return statistics


def _append_reason(statistics: Dict[str, Any], reason: str) -> None:
    key = f"reason:{reason}"
    statistics[key] = statistics.get(key, 0) + 1
    counter = REASON_COUNTERS.get(reason)
    if counter:
        statistics[counter] += 1


def _observe_lengths(statistics: Dict[str, Any], stats: Dict[str, Any]) -> None:
    """记录实际观测到的最小/最大长度和比例（来自真实输出，不来自配置）。"""
    for name in OBSERVED_KEYS:
        value = stats[name]
        low_key, high_key = f"min_observed_{name}", f"max_observed_{name}"
        current_low, current_high = statistics[low_key], statistics[high_key]
        statistics[low_key] = value if current_low is None else min(current_low, value)
        statistics[high_key] = value if current_high is None else max(current_high, value)


def _evaluate_record(record: dict, cfg: OperatorConfig) -> Tuple[dict, Dict[str, Any], List[str]]:
    """校验字段、观测长度、判定违规，并把 length_stats 写回 metadata。"""
    metadata = record.get("metadata")
    if metadata is None:
        metadata = {}
        record["metadata"] = metadata
    if not isinstance(metadata, dict):
        raise ValueError("invalid_type:metadata")

    for field in (SOURCE_FIELD, TARGET_FIELD):
        if field not in record:
            raise ValueError(f"missing_field:{field}")
        if not isinstance(record[field], str):
            raise ValueError(f"invalid_type:{field}")

    stats = measure_lengths(record)
    reasons = evaluate_length(stats, cfg)
    metadata["length_stats"] = stats
    return record, stats, reasons


def _failed(message: str, statistics: Dict[str, Any], logger) -> dict:
    log_action(logger, "失败", message)
    # [无血缘改造 2026-10-19] 血缘输出已停用，保留原实现便于回溯；不再生成 lineage 文件。
    # append_lineage(lineage_file, make_lineage(
    #     OPERATOR_NAME, OPERATOR_VERSION, unique_id,
    #     str(input_file), str(Path(output_root) / unique_id / OUTPUT_NAME), "failed",
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
        statistics = _base_statistics()
        return _failed(f"配置错误：{type(exc).__name__}", statistics, logger)

    input_name = Path(input_file).name
    log_action(logger, "输入", f"加载输入文件：{input_name}")
    if input_name != EXPECTED_INPUT_NAME:
        log_action(logger, "警告", f"输入文件名不是上游约定的 {EXPECTED_INPUT_NAME}，按当前文件继续处理")
    log_action(logger, "配置", "源文[{}-{}] 目标文[{}-{}] 比例[{}-{}] 违规策略={}".format(
        cfg.min_source_chars, cfg.max_source_chars,
        cfg.min_target_chars, cfg.max_target_chars,
        cfg.min_length_ratio, cfg.max_length_ratio, cfg.on_violation))

    try:
        encoding = select_encoding(input_file, cfg.input_encodings)
    except (OSError, ValueError) as exc:
        statistics = _base_statistics()
        return _failed(
            f"输入文件编码处理失败：{type(exc).__name__}，本节点要求严格 UTF-8",
            statistics, logger)

    statistics = _base_statistics(encoding=encoding, on_violation=cfg.on_violation)
    log_action(logger, "读取", f"解码输入文件：{encoding}")

    # 先写临时文件再原子替换：产物要么完整要么不存在，也不会截断同名输入。
    out_tmp = out_file.parent / (out_file.name + ".tmp")
    rej_tmp = rej_file.parent / (rej_file.name + ".tmp")

    try:
        with JsonlWriter(out_tmp) as output_writer, JsonlWriter(rej_tmp) as reject_writer:
            for lineno, line in enumerate(iter_decoded_lines(input_file, encoding), 1):
                if not line.strip():
                    continue
                statistics["total"] += 1
                try:
                    record = json.loads(line)
                except json.JSONDecodeError:
                    reject_writer.write({"line": lineno, "record": None, "reasons": ["invalid_json"]})
                    statistics["rejected"] += 1
                    _append_reason(statistics, "invalid_json")
                    continue
                if not isinstance(record, dict):
                    reject_writer.write({"line": lineno, "record": None, "reasons": ["invalid_record"]})
                    statistics["rejected"] += 1
                    _append_reason(statistics, "invalid_record")
                    continue
                try:
                    record, stats, reasons = _evaluate_record(record, cfg)
                except (TypeError, ValueError) as exc:
                    reason = str(exc)
                    reject_writer.write({"line": lineno, "record": record, "reasons": [reason]})
                    statistics["rejected"] += 1
                    _append_reason(statistics, reason)
                    continue

                _observe_lengths(statistics, stats)

                action, policy_pair, policy = resolve_violation_action(record, stats, reasons, cfg)
                if action == "reject":
                    reject_writer.write({"line": lineno, "record": record,
                                         "reasons": reasons, "length_stats": stats})
                    statistics["rejected"] += 1
                    if policy is not None and reasons and all(
                            reason.startswith("length_ratio_") for reason in reasons):
                        statistics["hard_ratio_rejections"] += 1
                    for reason in reasons:
                        _append_reason(statistics, reason)
                    continue

                if action == "warn":
                    record["metadata"]["length_warnings"] = list(reasons)
                    if policy is not None:
                        record["metadata"]["length_ratio_policy"] = {
                            "language_pair": policy_pair,
                            "base_min_length_ratio": cfg.min_length_ratio,
                            "base_max_length_ratio": cfg.max_length_ratio,
                            "hard_min_length_ratio": policy.hard_min_length_ratio,
                            "hard_max_length_ratio": policy.hard_max_length_ratio,
                            "action": "warn",
                        }
                        statistics["language_pair_soft_warnings"] += 1
                        key = f"warning_pair:{policy_pair}"
                        statistics[key] = statistics.get(key, 0) + 1
                    statistics["warned"] += 1
                    for reason in reasons:
                        _append_reason(statistics, reason)
                output_writer.write(record)
                statistics["passed"] += 1
        os.replace(out_tmp, out_file)
        os.replace(rej_tmp, rej_file)
    except (OSError, UnicodeDecodeError) as exc:
        for leftover in (out_tmp, rej_tmp):
            try:
                leftover.unlink()
            except OSError:
                pass
        return _failed(f"输入文件读取失败：{type(exc).__name__}",
                       statistics, logger)

    if statistics["total"] == 0:
        # 空输入没有任何可交付内容，不保留空的通过清单，避免下游误读。
        for empty_artifact in (out_file, rej_file):
            try:
                empty_artifact.unlink()
            except OSError:
                pass
        log_action(logger, "失败", "输入文件为空或无有效记录")
        # [无血缘改造 2026-10-19] 血缘输出已停用，保留原实现便于回溯；不再生成 lineage 文件。
        # append_lineage(lineage_file, make_lineage(
        #     OPERATOR_NAME, OPERATOR_VERSION, unique_id,
        #     str(input_file), str(out_file), "failed", started, now_iso(),
        #     cfg_snapshot, statistics))
        close_logger(logger)
        return {"status": "failed", "outputFile": None, "rejectFile": None,
                "stageZip": None, "storageUri": None, "statistics": statistics,
                "message": "输入文件为空或无有效记录"}

    if statistics["rejected"] > 0:
        status = "partial"
        message = ""
    else:
        status = "success"
        message = ""

    log_action(logger, "处理", "长度过滤完成：通过 {} 条，过滤 {} 条，警告 {} 条；观测源文长度[{}-{}] 目标文长度[{}-{}] 比例[{}-{}]".format(
        statistics["passed"], statistics["rejected"], statistics["warned"],
        statistics["min_observed_source_chars"], statistics["max_observed_source_chars"],
        statistics["min_observed_target_chars"], statistics["max_observed_target_chars"],
        statistics["min_observed_length_ratio"], statistics["max_observed_length_ratio"]))
    log_action(logger, "输出", f"写入输出文件：{out_file.name}；过滤清单：{rej_file.name}")
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
    parser = argparse.ArgumentParser(description="算子7：长度过滤")
    parser.add_argument("--input", required=True, help="输入句对 JSONL 文件（默认来自 05_layout_cleaned.jsonl）")
    parser.add_argument("--unique-id", default="local_demo_file1", help="任务id_文件id")
    parser.add_argument("--config", default=None, help="配置文件路径")
    parser.add_argument("--output-root", default=None, help="产物根目录")
    args = parser.parse_args()
    result = run(args.input, args.unique_id, args.output_root, args.config)
    print(f"status={result['status']} statistics={result['statistics']}")
    print(f"stageZip={result.get('stageZip')} storageUri={result.get('storageUri')}")


if __name__ == "__main__":
    main()
