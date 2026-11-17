"""算子3：编码与控制字符清理 —— 核心处理逻辑和 CLI。

[无血缘改造 2026-10-19] 不再生成/追加血缘文件；成功/部分成功返回前打包阶段产物
artifacts/stage_03_<uniqueId>.zip 并返回 stageZip/storageUri。
"""
import argparse
import codecs
import json
import os
import re
from pathlib import Path
from typing import Any, Dict, Iterator, List, Tuple

import yaml
from pydantic import ValidationError

from utils.artifacts import finalize_stage_artifacts
from utils.io import JsonlWriter, remove_files, task_dir, tmp_path_for
from utils.logger import close_logger, get_logger, log_action
from utils.schemas import OperatorConfig
from utils.timeutil import now_iso

# [无血缘改造 2026-10-19] 血缘输出已停用，保留原导入便于回溯：
# from utils.lineage import append_lineage, make_lineage, now_iso

BASE_DIR = Path(__file__).resolve().parent
OPERATOR_NAME = "encoding_cleanup"
OPERATOR_ID = "03"
OPERATOR_VERSION = "v1.0.0"
OUTPUT_NAME = "03_encoding_cleaned.jsonl"
REJECT_NAME = "03_encoding_rejects.jsonl"
# [无血缘改造 2026-10-19] 不再生成 lineage 文件。
# LINEAGE_NAME = "lineage.json"
LOG_NAME = "run.log"
CONTROL_CHAR_RE = re.compile(r"[\x00-\x08\x0B\x0C\x0E-\x1F\x7F-\x9F]")


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


def decode_bytes(data: bytes, encodings: List[str]) -> Tuple[str, str]:
    """Strictly decode bytes and return text plus the codec that succeeded."""
    if not encodings:
        raise ValueError("未配置输入编码")
    errors = []
    for encoding in encodings:
        try:
            return data.decode(encoding), encoding.lower()
        except (LookupError, UnicodeDecodeError) as exc:
            errors.append(type(exc).__name__)
    raise ValueError("输入文件无法按配置编码解码：" + ",".join(errors))


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
    """Find the first configured encoding that decodes the file strictly."""
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


def clean_control_chars(value: Any) -> Tuple[Any, int]:
    """Recursively remove disallowed control characters from JSON values."""
    if isinstance(value, str):
        cleaned, count = CONTROL_CHAR_RE.subn("", value)
        return cleaned, count
    if isinstance(value, list):
        cleaned_items = []
        count = 0
        for item in value:
            cleaned_item, item_count = clean_control_chars(item)
            cleaned_items.append(cleaned_item)
            count += item_count
        return cleaned_items, count
    if isinstance(value, dict):
        cleaned_dict = {}
        count = 0
        for key, item in value.items():
            cleaned_key, key_count = clean_control_chars(key)
            cleaned_item, item_count = clean_control_chars(item)
            cleaned_dict[cleaned_key] = cleaned_item
            count += key_count + item_count
        return cleaned_dict, count
    return value, 0


def _base_statistics(encoding=None) -> Dict[str, Any]:
    return {
        "total": 0,
        "passed": 0,
        "rejected": 0,
        "records_changed": 0,
        "control_chars_removed": 0,
        "encoding": encoding,
    }


def _append_reason(statistics: Dict[str, Any], reason: str) -> None:
    key = f"reason:{reason}"
    statistics[key] = statistics.get(key, 0) + 1


def _failed(unique_id: str, message: str, input_file, out_file,
            statistics: Dict[str, Any], cfg_snapshot: dict, logger,
            started: str) -> dict:
    """统一失败返回：失败分支不生成阶段产物，返回 dict 中 stageZip/storageUri 为 None。"""
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


def run(input_file: str, unique_id: str, output_root=None,
        config_path=None, overrides: dict = None, logger=None) -> dict:
    output_root = Path(output_root) if output_root else Path(os.getenv("OUTPUT_DIR", BASE_DIR / "output"))
    tdir = task_dir(output_root, unique_id)
    out_file = tdir / OUTPUT_NAME
    rej_file = tdir / REJECT_NAME
    # [无血缘改造 2026-10-19] 不再生成 lineage 文件。
    # lineage_file = tdir / LINEAGE_NAME
    logger = logger or get_logger(f"{OPERATOR_NAME}.{unique_id}", tdir / LOG_NAME)
    started = now_iso()

    try:
        cfg, cfg_snapshot = load_config(config_path, overrides)
    except (AttributeError, OSError, TypeError, ValueError, ValidationError, yaml.YAMLError) as exc:
        statistics = _base_statistics()
        return _failed(unique_id, f"配置错误：{type(exc).__name__}", input_file, out_file,
                       statistics, {"config_error": type(exc).__name__},
                       logger, started)

    log_action(logger, "输入", f"加载输入文件：{Path(input_file).name}")
    log_action(logger, "配置", f"输入编码={cfg.input_encodings} 清理控制字符={cfg.remove_control_chars}")

    try:
        encoding = select_encoding(input_file, cfg.input_encodings)
    except (OSError, ValueError) as exc:
        statistics = _base_statistics()
        return _failed(unique_id, f"输入文件编码处理失败：{type(exc).__name__}，请检查文件编码",
                       input_file, out_file, statistics, cfg_snapshot,
                       logger, started)

    statistics = _base_statistics(encoding)
    log_action(logger, "读取", f"解码输入文件：{encoding}")

    # 先写 .tmp 再 os.replace 原子替换：产物要么完整要么不存在；
    # 输入路径与输出路径相同时（同 uniqueId 重跑），源文件也不会被截断。
    tmp_out = tmp_path_for(out_file)
    tmp_rej = tmp_path_for(rej_file)
    try:
        with JsonlWriter(tmp_out) as output_writer, JsonlWriter(tmp_rej) as reject_writer:
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
                cleaned, removed = clean_control_chars(record) if cfg.remove_control_chars else (record, 0)
                metadata = cleaned.get("metadata")
                if metadata is None:
                    metadata = {}
                    cleaned["metadata"] = metadata
                if not isinstance(metadata, dict):
                    reject_writer.write({
                        "line": lineno,
                        "record": record,
                        "reasons": ["invalid_type:metadata"],
                    })
                    statistics["rejected"] += 1
                    _append_reason(statistics, "invalid_type:metadata")
                    continue
                metadata["encoding_original"] = encoding
                metadata["encoding_normalized"] = "utf-8"
                metadata["control_chars_removed"] = removed
                output_writer.write(cleaned)
                statistics["passed"] += 1
                statistics["control_chars_removed"] += removed
                if removed:
                    statistics["records_changed"] += 1
        # 写入器关闭后再原子替换（Windows 下不能替换仍被占用的文件）。
        os.replace(tmp_out, out_file)
        os.replace(tmp_rej, rej_file)
    except (OSError, UnicodeDecodeError) as exc:
        remove_files(tmp_out, tmp_rej)
        return _failed(unique_id, f"输入读取或输出写入失败：{type(exc).__name__}",
                       input_file, out_file, statistics, cfg_snapshot,
                       logger, started)

    if statistics["total"] == 0:
        # 空输入没有任何可交付内容，删除空产物，避免下游误读为“已清洗”。
        remove_files(out_file, rej_file)
        return _failed(unique_id, "输入文件为空或无有效记录", input_file, out_file,
                       statistics, cfg_snapshot, logger, started)

    status = "partial" if statistics["rejected"] > 0 else "success"
    log_action(logger, "处理", f"编码清理完成：通过 {statistics['passed']} 条，拦截 {statistics['rejected']} 条，移除控制字符 {statistics['control_chars_removed']} 个")
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
            "statistics": statistics, "message": ""}


def main():
    parser = argparse.ArgumentParser(description="算子3：编码与控制字符清理")
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
