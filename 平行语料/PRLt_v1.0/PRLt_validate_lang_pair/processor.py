"""算子2：语言与句对结构校验 —— 核心处理逻辑（CLI 与 FastAPI 共用）。

[无血缘改造 2026-10-19] 不再生成/追加血缘文件；成功/部分成功返回前打包阶段产物
artifacts/stage_02_<uniqueId>.zip 并返回 stageZip/storageUri。
"""
import argparse
import os
from collections import Counter
from pathlib import Path
from typing import List, Tuple

import yaml
from pydantic import ValidationError

from utils.artifacts import finalize_stage_artifacts
from utils.io import JsonlWriter, read_jsonl_stream, task_dir
from utils.logger import close_logger, get_logger, log_action
from utils.schemas import OperatorConfig
from utils.timeutil import now_iso

# [无血缘改造 2026-10-19] 血缘输出已停用，保留原导入便于回溯：
# from utils.lineage import append_lineage, make_lineage, now_iso

BASE_DIR = Path(__file__).resolve().parent
OPERATOR_NAME = "structure_validation"
OPERATOR_ID = "02"
OPERATOR_VERSION = "v1.0.0"


def load_config(config_path=None, overrides: dict = None) -> Tuple[OperatorConfig, dict]:
    cfg_path = Path(config_path) if config_path else BASE_DIR / "configs" / "config.yaml"
    raw = yaml.safe_load(cfg_path.read_text(encoding="utf-8")) or {}
    merged = {k: v for k, v in raw.items() if k != "operator"}
    if overrides:
        merged.update(overrides)
    cfg = OperatorConfig(**merged)
    snapshot = {"operator": raw.get("operator", {}), **merged}
    return cfg, snapshot


def validate_record(rec: dict, cfg: OperatorConfig) -> List[str]:
    reasons: List[str] = []
    for field in cfg.required_fields:
        if field not in rec or rec[field] is None:
            reasons.append(f"missing_field:{field}")
    for field in ("id", "source", "target"):
        if field in rec and rec[field] is not None and not isinstance(rec[field], str):
            reasons.append(f"invalid_type:{field}")
    if "metadata" in rec and not isinstance(rec["metadata"], dict):
        reasons.append("invalid_type:metadata")
    for field in cfg.empty_check_fields:
        if field in rec:
            value = rec.get(field)
            if value is None:
                reasons.append(f"empty_field:{field}")
            elif isinstance(value, str) and not value.strip():
                reasons.append(f"empty_field:{field}")
    if cfg.check_declared_lang:
        src = rec.get("source_lang")
        tgt = rec.get("target_lang")
        if src is not None and src != cfg.source_lang:
            reasons.append(f"lang_mismatch:source({src}!={cfg.source_lang})")
        if tgt is not None and tgt != cfg.target_lang:
            reasons.append(f"lang_mismatch:target({tgt}!={cfg.target_lang})")
    return reasons


def run(input_file: str, unique_id: str, output_root=None,
        config_path=None, overrides: dict = None, logger=None) -> dict:
    output_root = Path(output_root) if output_root else Path(os.getenv("OUTPUT_DIR", BASE_DIR / "output"))
    tdir = task_dir(output_root, unique_id)
    out_file = tdir / "02_validated.jsonl"
    rej_file = tdir / "02_rejects.jsonl"
    # [无血缘改造 2026-10-19] 不再生成 lineage 文件。
    # lineage_file = tdir / "lineage.json"
    logger = logger or get_logger(f"{OPERATOR_NAME}.{unique_id}", tdir / "run.log")
    try:
        cfg, cfg_snapshot = load_config(config_path, overrides)
    except (AttributeError, OSError, TypeError, ValueError, ValidationError, yaml.YAMLError) as exc:
        message = f"配置错误：{type(exc).__name__}"
        statistics = {"total": 0, "passed": 0, "rejected": 0, "warned": 0}
        log_action(logger, "失败", message)
        # [无血缘改造 2026-10-19] 血缘输出已停用，保留原实现便于回溯；不再生成 lineage 文件。
        # append_lineage(lineage_file, make_lineage(
        #     OPERATOR_NAME, OPERATOR_VERSION, unique_id,
        #     str(input_file), str(out_file), "failed", now_iso(), now_iso(),
        #     {"config_error": message}, statistics))
        close_logger(logger)
        return {"status": "failed", "outputFile": None, "rejectFile": None,
                "stageZip": None, "storageUri": None, "statistics": statistics,
                "message": message}

    started = now_iso()
    log_action(logger, "输入", f"加载输入文件：{Path(input_file).name}")
    log_action(logger, "配置", f"必填字段={cfg.required_fields} 语言方向={cfg.source_lang}->{cfg.target_lang} 策略={cfg.on_violation}")

    stats = Counter({"total": 0, "passed": 0, "rejected": 0, "warned": 0})
    status = "success"
    try:
        with JsonlWriter(out_file) as w_out, JsonlWriter(rej_file) as w_rej:
            for lineno, rec, err in read_jsonl_stream(input_file):
                stats["total"] += 1
                if err:
                    w_rej.write({"line": lineno, "record": None, "reasons": [err]})
                    stats["rejected"] += 1
                    stats[f"reason:{err}"] += 1
                    continue
                reasons = validate_record(rec, cfg)
                if not reasons:
                    rec.setdefault("metadata", {})
                    w_out.write(rec)
                    stats["passed"] += 1
                elif cfg.on_violation == "warn":
                    if not isinstance(rec.get("metadata"), dict):
                        rec["metadata"] = {}
                    rec["metadata"].setdefault("warnings", []).extend(reasons)
                    w_out.write(rec)
                    stats["passed"] += 1
                    stats["warned"] += 1
                    for r in reasons:
                        stats[f"reason:{r}"] += 1
                else:
                    w_rej.write({"line": lineno, "record": rec, "reasons": reasons})
                    stats["rejected"] += 1
                    for r in reasons:
                        stats[f"reason:{r}"] += 1
    except (UnicodeDecodeError, OSError) as e:
        status = "failed"
        log_action(logger, "失败", f"输入文件无法读取：{type(e).__name__}")
        statistics = dict(stats)
        # [无血缘改造 2026-10-19] 血缘输出已停用，保留原实现便于回溯；不再生成 lineage 文件。
        # finished = now_iso()
        # append_lineage(lineage_file, make_lineage(
        #     OPERATOR_NAME, OPERATOR_VERSION, unique_id,
        #     str(input_file), str(out_file), status, started, finished,
        #     cfg_snapshot, statistics))
        close_logger(logger)
        return {"status": status, "outputFile": None, "rejectFile": None,
                "stageZip": None, "storageUri": None, "statistics": statistics,
                "message": f"输入文件读取失败：{type(e).__name__}，请检查文件编码是否为 UTF-8 及路径是否正确"}

    if stats["total"] == 0:
        status = "failed"
        message = "输入文件为空或无有效记录"
    elif stats["rejected"] > 0:
        status = "partial"
        message = ""
    else:
        message = ""

    statistics = dict(stats)
    log_action(logger, "处理", f"结构校验完成：通过 {stats['passed']} 条，拦截 {stats['rejected']} 条")
    log_action(logger, "输出", f"写入输出文件：{out_file.name}；异常清单：{rej_file.name}")

    # [无血缘改造 2026-10-19] 不再追加血缘；改为打包本阶段产物并登记对象存储地址。
    # finished = now_iso()
    # append_lineage(lineage_file, make_lineage(
    #     OPERATOR_NAME, OPERATOR_VERSION, unique_id,
    #     str(input_file), str(out_file), status, started, finished,
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
    ap = argparse.ArgumentParser(description="算子2：语言与句对结构校验")
    ap.add_argument("--input", required=True, help="输入句对 JSONL 文件")
    ap.add_argument("--unique-id", default="local_demo_file1", help="任务id_文件id")
    ap.add_argument("--config", default=None, help="配置文件路径")
    ap.add_argument("--output-root", default=None, help="产物根目录")
    args = ap.parse_args()
    result = run(args.input, args.unique_id, args.output_root, args.config)
    print(f"status={result['status']} statistics={result['statistics']}")
    print(f"stageZip={result.get('stageZip')} storageUri={result.get('storageUri')}")


if __name__ == "__main__":
    main()
