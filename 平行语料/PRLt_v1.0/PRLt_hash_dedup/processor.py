"""算子8：哈希去重 —— 核心处理逻辑和 CLI。

职责：对规范化后的“源文 + 目标文”组合做精确去重，保留首次出现，重复记录写入
可追踪清单。只做精确组合键去重：不删除近义句、不做单边去重、不做模糊相似判断、
不评价语义等价，也不输出任何模型分数；不跨 uniqueId 共享去重状态。
"""
import argparse
import codecs
import hashlib
import json
import os
import re
import unicodedata
from pathlib import Path
from typing import Any, Dict, Iterator, List, Tuple

import yaml
from pydantic import ValidationError

from utils.artifacts import finalize_stage_artifacts
from utils.io import JsonlWriter, task_dir
from utils.logger import close_logger, get_logger, log_action
from utils.schemas import PAIR_SEPARATOR, OperatorConfig
from utils.timeutil import now_iso

# [无血缘改造 2026-10-19] 血缘输出已停用，保留原导入便于回溯：
# from utils.lineage import append_lineage, make_lineage, now_iso

BASE_DIR = Path(__file__).resolve().parent
OPERATOR_NAME = "parallel_pair_deduplication"
OPERATOR_ID = "07"
OPERATOR_VERSION = "v1.0.0"

OUTPUT_NAME = "07_deduped.jsonl"
DUPLICATE_NAME = "07_duplicates.jsonl"
REJECT_NAME = "07_rejects.jsonl"
# 上游为算子7 长度过滤；仅用于默认输入优先级和日志提示，不作为硬约束。
EXPECTED_INPUT_NAME = "06_length_filtered.jsonl"

SOURCE_FIELD = "source"
TARGET_FIELD = "target"
WHITESPACE_RE = re.compile(r"\s+")


class MemoryLimitExceeded(Exception):
    """去重状态超过配置上限：明确失败，不静默截断去重范围。"""


def load_config(config_path=None, overrides: dict = None) -> Tuple[OperatorConfig, dict]:
    cfg_path = Path(config_path) if config_path else BASE_DIR / "configs" / "config.yaml"
    raw = yaml.safe_load(cfg_path.read_text(encoding="utf-8")) or {}
    if not isinstance(raw, dict):
        raise ValueError("配置文件必须是对象")
    merged = {key: value for key, value in raw.items() if key != "operator"}
    if overrides:
        merged.update(overrides)
    cfg = OperatorConfig(**merged)
    snapshot = {"operator": raw.get("operator", {}), **cfg.model_dump(),
                "pair_separator": PAIR_SEPARATOR}
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


def canonicalize_text(value: str, cfg: OperatorConfig) -> str:
    """Unicode 归一化 → 可选 casefold → 可选连续空白折叠和首尾裁剪。

    只用于计算去重键，不改写输出记录中的原文；不删除标点。
    """
    if not isinstance(value, str):
        raise TypeError("文本字段必须是字符串")
    text = value
    if cfg.unicode_normalization != "none":
        text = unicodedata.normalize(cfg.unicode_normalization, text)
    if cfg.casefold:
        text = text.casefold()
    if cfg.collapse_whitespace:
        text = WHITESPACE_RE.sub(" ", text).strip()
    return text


def pair_key(source: str, target: str, cfg: OperatorConfig) -> str:
    """hash(canonical(source) + SEP + canonical(target))，带算法前缀。

    键同时使用源文和目标文，避免只对单边建键。规范化是幂等的，传入已规范化文本
    再调用一次不会改变结果，便于用原始记录回放哈希。
    """
    canonical_source = canonicalize_text(source, cfg)
    canonical_target = canonicalize_text(target, cfg)
    payload = canonical_source + PAIR_SEPARATOR + canonical_target
    digest = hashlib.new(cfg.hash_algorithm, payload.encode("utf-8")).hexdigest()
    return f"{cfg.hash_algorithm}:{digest}"


def _base_statistics(encoding=None, cfg: OperatorConfig = None) -> Dict[str, Any]:
    statistics: Dict[str, Any] = {
        "total": 0,
        "passed": 0,
        "rejected": 0,
        "duplicates": 0,
        "unique_pairs": 0,
        "duplicate_pairs": 0,
        "unique_source_count": 0,
        "unique_target_count": 0,
        "duplicate_ratio": 0.0,
        "hash_algorithm": cfg.hash_algorithm if cfg else None,
        "unicode_normalization": cfg.unicode_normalization if cfg else None,
        "casefold": cfg.casefold if cfg else None,
        "collapse_whitespace": cfg.collapse_whitespace if cfg else None,
        "pair_separator": PAIR_SEPARATOR,
        "max_tracked_pairs": cfg.max_tracked_pairs if cfg else None,
        "memory_limit_exceeded": False,
        "encoding": encoding,
    }
    return statistics


def _append_reason(statistics: Dict[str, Any], reason: str) -> None:
    key = f"reason:{reason}"
    statistics[key] = statistics.get(key, 0) + 1


def _fingerprint(record: dict, cfg: OperatorConfig) -> Tuple[str, str, str]:
    """校验字段并返回 (dedup_key, canonical_source, canonical_target)。"""
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

    # 固定分隔符 \x1f 必须不出现在原文中，否则源/目标边界会碰撞。
    # 必须在空白折叠之前检查：Python 中 "\x1f".isspace() 为真，折叠后会被替换成空格。
    if PAIR_SEPARATOR in record[SOURCE_FIELD] or PAIR_SEPARATOR in record[TARGET_FIELD]:
        raise ValueError("separator_in_text")

    canonical_source = canonicalize_text(record[SOURCE_FIELD], cfg)
    canonical_target = canonicalize_text(record[TARGET_FIELD], cfg)
    return pair_key(canonical_source, canonical_target, cfg), canonical_source, canonical_target


def _failed(message: str, statistics: Dict[str, Any], logger) -> dict:
    log_action(logger, "失败", message)
    # [无血缘改造 2026-10-19] 血缘输出已停用，保留原实现便于回溯；不再生成 lineage 文件。
    # append_lineage(lineage_file, make_lineage(
    #     OPERATOR_NAME, OPERATOR_VERSION, unique_id,
    #     str(input_file), str(Path(output_root) / unique_id / OUTPUT_NAME), "failed",
    #     started, now_iso(), cfg_snapshot, statistics))
    close_logger(logger)
    return {"status": "failed", "outputFile": None, "duplicateFile": None, "rejectFile": None,
            "stageZip": None, "storageUri": None, "statistics": statistics, "message": message}


def run(input_file: str, unique_id: str, output_root=None,
        config_path=None, overrides: dict = None, logger=None) -> dict:
    output_root = Path(output_root) if output_root else Path(os.getenv("OUTPUT_DIR", BASE_DIR / "output"))
    tdir = task_dir(output_root, unique_id)
    out_file = tdir / OUTPUT_NAME
    dup_file = tdir / DUPLICATE_NAME
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
    log_action(logger, "配置", "哈希={} 归一化={} casefold={} 空白折叠={} 分隔符={} 保留首次={} 状态上限={}".format(
        cfg.hash_algorithm, cfg.unicode_normalization, cfg.casefold,
        cfg.collapse_whitespace, repr(PAIR_SEPARATOR), cfg.keep_first, cfg.max_tracked_pairs))

    try:
        encoding = select_encoding(input_file, cfg.input_encodings)
    except (OSError, ValueError) as exc:
        statistics = _base_statistics(cfg=cfg)
        return _failed(
            f"输入文件编码处理失败：{type(exc).__name__}，本节点要求严格 UTF-8",
            statistics, logger)

    statistics = _base_statistics(encoding=encoding, cfg=cfg)
    log_action(logger, "读取", f"解码输入文件：{encoding}")

    seen: Dict[str, Dict[str, Any]] = {}
    unique_sources = set()
    unique_targets = set()
    # 先写临时文件再原子替换：产物要么完整要么不存在，也不会截断同名输入。
    tmp_paths = [out_file.parent / (out_file.name + ".tmp"),
                 dup_file.parent / (dup_file.name + ".tmp"),
                 rej_file.parent / (rej_file.name + ".tmp")]

    try:
        with JsonlWriter(tmp_paths[0]) as output_writer, \
                JsonlWriter(tmp_paths[1]) as duplicate_writer, \
                JsonlWriter(tmp_paths[2]) as reject_writer:
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
                    key, canonical_source, canonical_target = _fingerprint(record, cfg)
                except (TypeError, ValueError) as exc:
                    reason = str(exc)
                    reject_writer.write({"line": lineno, "record": record, "reasons": [reason]})
                    statistics["rejected"] += 1
                    _append_reason(statistics, reason)
                    continue

                unique_sources.add(canonical_source)
                unique_targets.add(canonical_target)

                first = seen.get(key)
                if first is not None:
                    duplicate_writer.write({
                        "line": lineno,
                        "duplicate_of": {"line": first["line"], "id": first["id"]},
                        "dedup_key": key,
                        "reasons": ["duplicate_pair"],
                        "record": record,
                    })
                    statistics["duplicates"] += 1
                    _append_reason(statistics, "duplicate_pair")
                    continue

                if len(seen) >= cfg.max_tracked_pairs:
                    limit_exceeded = True
                    raise MemoryLimitExceeded(
                        f"去重状态超过上限 {cfg.max_tracked_pairs}，为避免静默截断去重范围已中止")

                seen[key] = {"line": lineno, "id": record.get("id")}
                record["metadata"]["dedup"] = {
                    "dedup_key": key,
                    "hash_algorithm": cfg.hash_algorithm,
                    "unicode_normalization": cfg.unicode_normalization,
                    "casefold": cfg.casefold,
                    "collapse_whitespace": cfg.collapse_whitespace,
                    "pair_separator": PAIR_SEPARATOR,
                    "first_line": lineno,
                    "first_id": record.get("id"),
                    "is_duplicate": False,
                }
                output_writer.write(record)
                statistics["passed"] += 1

        # 写入器已关闭后再原子替换（Windows 下不能替换仍被占用的文件）。
        os.replace(tmp_paths[0], out_file)
        os.replace(tmp_paths[1], dup_file)
        os.replace(tmp_paths[2], rej_file)
    except MemoryLimitExceeded as exc:
        for leftover in tmp_paths:
            try:
                leftover.unlink()
            except OSError:
                pass
        statistics["memory_limit_exceeded"] = True
        return _failed(str(exc), statistics, logger)
    except (OSError, UnicodeDecodeError) as exc:
        for leftover in tmp_paths:
            try:
                leftover.unlink()
            except OSError:
                pass
        return _failed(f"输入文件读取失败：{type(exc).__name__}",
                       statistics, logger)

    statistics["unique_pairs"] = statistics["passed"]
    statistics["duplicate_pairs"] = statistics["duplicates"]
    statistics["unique_source_count"] = len(unique_sources)
    statistics["unique_target_count"] = len(unique_targets)
    if statistics["passed"]:
        statistics["duplicate_ratio"] = round(statistics["duplicates"] / statistics["passed"], 6)

    if statistics["total"] == 0:
        # 空输入没有任何可交付内容，不保留空清单，避免下游误读。
        for empty_artifact in (out_file, dup_file, rej_file):
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
        return {"status": "failed", "outputFile": None, "duplicateFile": None, "rejectFile": None,
                "stageZip": None, "storageUri": None, "statistics": statistics,
                "message": "输入文件为空或无有效记录"}

    status = "partial" if statistics["rejected"] > 0 else "success"
    log_action(logger, "处理", "哈希去重完成：保留 {} 条，重复 {} 条，拦截 {} 条；唯一源文 {} 个，唯一目标文 {} 个".format(
        statistics["passed"], statistics["duplicates"], statistics["rejected"],
        statistics["unique_source_count"], statistics["unique_target_count"]))
    log_action(logger, "输出", f"写入输出文件：{out_file.name}；重复清单：{dup_file.name}；异常清单：{rej_file.name}")
    # [无血缘改造 2026-10-19] 不再追加血缘；改为打包本阶段产物并登记对象存储地址。
    # 阶段产物按契约只含主输出、异常清单与 run.log（重复清单为业务产物，不进入 stageZip）。
    # append_lineage(lineage_file, make_lineage(
    #     OPERATOR_NAME, OPERATOR_VERSION, unique_id,
    #     str(input_file), str(out_file), status, started, now_iso(),
    #     cfg_snapshot, statistics))
    stage_zip, storage_uri = finalize_stage_artifacts(
        tdir, OPERATOR_ID, unique_id, out_file, rej_file, tdir / "run.log")
    log_action(logger, "归档", f"阶段产物：{Path(stage_zip).name if stage_zip else '无'}；存储地址：{storage_uri}")
    log_action(logger, "完成", f"算子执行完成，状态 {status}")
    close_logger(logger)
    return {"status": status, "outputFile": str(out_file), "duplicateFile": str(dup_file),
            "rejectFile": str(rej_file), "stageZip": stage_zip, "storageUri": storage_uri,
            "statistics": statistics, "message": ""}


def main():
    parser = argparse.ArgumentParser(description="算子8：哈希去重")
    parser.add_argument("--input", required=True, help="输入句对 JSONL 文件（默认来自 06_length_filtered.jsonl）")
    parser.add_argument("--unique-id", default="local_demo_file1", help="任务id_文件id")
    parser.add_argument("--config", default=None, help="配置文件路径")
    parser.add_argument("--output-root", default=None, help="产物根目录")
    args = parser.parse_args()
    result = run(args.input, args.unique_id, args.output_root, args.config)
    print(f"status={result['status']} statistics={result['statistics']}")
    print(f"stageZip={result.get('stageZip')} storageUri={result.get('storageUri')}")


if __name__ == "__main__":
    main()
