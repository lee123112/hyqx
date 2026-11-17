"""算子13：S/A/C 对齐质量分级 —— 分数解析、严格分级、状态映射、异常清单与 CLI。

边界（严格开闭区间，禁止 round 改变语义；以 DEVELOPMENT_PLAN.md「验收」为准）：
    S: 90 < N <= 100
    A: 80 <= N <= 90
    C:  0 <= N <  80
（计划 §2 正文写「C 为 N<=80」与「验收」的「80 分为 A」冲突，见 README 已知口径）
本节点不加载任何模型，只做「上游分数 -> 0-100 质量分 -> S/A/C 等级」的可解释映射，
因此不存在模型降级或伪造模型分数的路径；缺分/越界/冲突一律进异常清单。
"""
import argparse
import codecs
import json
import os
from pathlib import Path
from typing import Any, Dict, Iterator, List, Optional, Tuple

import yaml
from pydantic import ValidationError

from utils.artifacts import finalize_stage_artifacts
from utils.grading import boundary_rule, grade_of, resolve_quality_score
from utils.io import JsonlWriter, count_jsonl_lines, task_dir
# [无血缘改造 2026-10-19] 血缘输出已停用，保留原导入便于回溯：
# from utils.lineage import append_lineage, make_lineage, now_iso
from utils.logger import close_logger, get_logger, log_action
from utils.schemas import OperatorConfig
from utils.timeutil import now_iso

BASE_DIR = Path(__file__).resolve().parent
OPERATOR_NAME = "alignment_quality_grading"
OPERATOR_ID = "13"
OPERATOR_VERSION = "v1.0.0"
OUTPUT_NAME = "13_graded.jsonl"
REJECT_NAME = "13_rejects.jsonl"


def load_config(config_path=None, overrides: dict = None) -> Tuple[OperatorConfig, dict]:
    """加载并校验配置；未确认字段一律拒绝，保证 config 快照可复现。"""
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
    """按指定编码增量严格解码，逐行 yield 逻辑行（不整文件读入内存）。"""
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
    """选择第一个能严格解码整个文件的编码，保证流式读取可用。"""
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


def _base_statistics(encoding=None) -> Dict[str, Any]:
    return {
        "input_records": 0,
        "graded": 0,
        "rejected_records": 0,
        "S_count": 0,
        "A_count": 0,
        "C_count": 0,
        "waiting_manual_count": 0,
        "accepted_for_quality_check_count": 0,
        "score_conversion_applied_count": 0,
        "min_quality_score": None,
        "max_quality_score": None,
        "encoding": encoding,
    }


def _bump(statistics: Dict[str, Any], key: str) -> None:
    statistics[key] = statistics.get(key, 0) + 1


def _append_reason(statistics: Dict[str, Any], reason: str) -> None:
    _bump(statistics, f"reason:{reason}")


def _unlink(path: Path) -> None:
    try:
        path.unlink()
    except OSError:
        pass


def _reject_entry(lineno: int, record: Optional[dict], reasons: List[str]) -> dict:
    entry: Dict[str, Any] = {"line": lineno, "id": None, "pair_id": None, "reasons": reasons,
                             "record": record}
    if isinstance(record, dict):
        meta = record.get("metadata") if isinstance(record.get("metadata"), dict) else {}
        entry["id"] = record.get("id")
        entry["pair_id"] = meta.get("parent_pair_id") or record.get("pair_id") or record.get("id")
    return entry


def _validate_structure(record: dict, cfg: OperatorConfig) -> List[str]:
    """输入校验与业务处理分离：只检查结构、类型与声明语言。"""
    meta = record.get("metadata")
    if meta is None:
        record["metadata"] = {}
    elif not isinstance(meta, dict):
        return ["invalid_type:metadata"]

    for field in ("source", "target"):
        if field not in record:
            return [f"missing_field:{field}"]
        if not isinstance(record[field], str):
            return [f"invalid_type:{field}"]
        if not record[field].strip():
            return [f"empty_{field}"]

    if cfg.language_check:
        for field, expected in (("source_lang", cfg.source_lang),
                                ("target_lang", cfg.target_lang)):
            value = record.get(field)
            if value is not None and value != expected:
                return ["language_declaration_mismatch"]
    return []


def _build_output(record: dict, meta: dict, score: float, info: dict,
                  cfg: OperatorConfig) -> Tuple[dict, str, str]:
    """生成通过记录：追加等级字段，保留并转存上游真实证据。"""
    grade_key = grade_of(score, cfg.grade_s_threshold, cfg.grade_a_threshold)
    label = cfg.grade_labels[grade_key]
    status = cfg.grade_status_map[grade_key]
    rule = boundary_rule(cfg.grade_labels, cfg.grade_s_threshold, cfg.grade_a_threshold)
    delivery = grade_key in cfg.delivery_eligible_grades

    output = dict(record)
    output["quality_score"] = score
    output["quality_grade"] = label
    output["grade_rule_version"] = cfg.grade_rule_version
    output["review_status"] = status
    output["grade_boundary_rule"] = rule
    output["score_source"] = info["score_source"]
    output["score_scale"] = info["score_scale"]
    output["raw_confidence"] = info["raw_confidence"]
    output["raw_quality_score"] = info["raw_quality_score"]
    output["score_conversion_version"] = cfg.score_conversion_version
    output["delivery_eligible"] = delivery

    meta_out = dict(meta)
    upstream_review = record.get("review_status")
    if upstream_review is None:
        upstream_review = meta.get("review_status")
    if upstream_review is not None and upstream_review != status:
        # 上游复核状态不覆盖，转存到独立字段留痕。
        meta_out["upstream_review_status"] = upstream_review
    meta_out["quality_score"] = score
    meta_out["quality_grade"] = label
    meta_out["grade_rule_version"] = cfg.grade_rule_version
    meta_out["grade_boundary_rule"] = rule
    meta_out["review_status"] = status
    meta_out["score_source"] = info["score_source"]
    meta_out["score_scale"] = info["score_scale"]
    meta_out["score_conversion_applied"] = info["score_conversion_applied"]
    meta_out["delivery_eligible"] = delivery
    output["metadata"] = meta_out
    return output, grade_key, status


def grade_record(record: dict, cfg: OperatorConfig) -> Tuple[Optional[dict], List[str], Optional[dict]]:
    """对单条记录分级；返回 (输出记录, 失败原因, 分级明细)。"""
    reasons = _validate_structure(record, cfg)
    if reasons:
        return None, reasons, None

    score, score_reasons, info = resolve_quality_score(record, cfg)
    if score_reasons:
        return None, score_reasons, None

    output, grade_key, status = _build_output(record, record["metadata"], score, info, cfg)
    detail = {"grade_key": grade_key, "status": status, "score": score, "info": info}
    return output, [], detail


def run(input_file: str, unique_id: str, output_root=None,
        config_path=None, overrides: dict = None, logger=None) -> dict:
    output_root = Path(output_root) if output_root else Path(os.getenv("OUTPUT_DIR", BASE_DIR / "output"))
    tdir = task_dir(output_root, unique_id)
    out_file = tdir / OUTPUT_NAME
    rej_file = tdir / REJECT_NAME
    # [无血缘改造 2026-10-19] 不再生成 lineage 文件。
    # lineage_file = tdir / "lineage.json"
    logger = logger or get_logger(f"{OPERATOR_NAME}.{unique_id}", tdir / "run.log")

    try:
        cfg, cfg_snapshot = load_config(config_path, overrides)
    except (AttributeError, OSError, TypeError, ValueError, ValidationError, yaml.YAMLError) as exc:
        message = f"配置错误：{type(exc).__name__}"
        statistics = _base_statistics()
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

    statistics = _base_statistics()
    statistics["grade_rule_version"] = cfg.grade_rule_version
    statistics["grade_boundary_rule"] = boundary_rule(
        cfg.grade_labels, cfg.grade_s_threshold, cfg.grade_a_threshold)
    statistics["score_scale"] = cfg.score_scale
    statistics["score_conversion_version"] = cfg.score_conversion_version

    started = now_iso()
    log_action(logger, "输入", f"加载输入文件：{Path(input_file).name}")
    log_action(logger, "配置", f"边界={statistics['grade_boundary_rule']} "
                              f"量纲={cfg.score_scale} 规则版本={cfg.grade_rule_version}")

    input_path = Path(input_file).resolve()
    out_tmp = Path(str(out_file) + ".tmp")
    rej_tmp = Path(str(rej_file) + ".tmp")

    def _fail(message: str) -> dict:
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

    if not input_path.is_file():
        return _fail("输入文件不存在")
    if input_path in {out_file.resolve(), rej_file.resolve(),
                      out_tmp.resolve(), rej_tmp.resolve()}:
        # 输出/临时路径就是输入文件时不得继续，否则会截断真实数据。
        return _fail("输入路径与输出路径相同，拒绝执行以避免截断输入")

    try:
        encoding = select_encoding(input_file, cfg.input_encodings)
    except (OSError, ValueError):
        return _fail("输入文件编码处理失败，请检查文件编码")

    statistics["encoding"] = encoding
    log_action(logger, "读取", f"解码输入文件：{encoding}")

    write_ok = False
    try:
        with JsonlWriter(out_tmp) as graded_writer, JsonlWriter(rej_tmp) as reject_writer:
            for lineno, line in enumerate(iter_decoded_lines(input_file, encoding), 1):
                if not line.strip():
                    continue
                statistics["input_records"] += 1
                try:
                    record = json.loads(line)
                except json.JSONDecodeError:
                    reject_writer.write(_reject_entry(lineno, None, ["invalid_json"]))
                    _append_reason(statistics, "invalid_json")
                    continue
                if not isinstance(record, dict):
                    reject_writer.write(_reject_entry(lineno, None, ["invalid_record"]))
                    _append_reason(statistics, "invalid_record")
                    continue

                output, reasons, detail = grade_record(record, cfg)
                if reasons:
                    reject_writer.write(_reject_entry(lineno, record, reasons))
                    for reason in reasons:
                        _append_reason(statistics, reason)
                    continue

                graded_writer.write(output)
                grade_key = detail["grade_key"]
                _bump(statistics, f"{grade_key}_count")
                _bump(statistics, f"score_source:{detail['info']['score_source']}")
                if detail["info"]["score_conversion_applied"]:
                    statistics["score_conversion_applied_count"] += 1
                score = detail["score"]
                if statistics["min_quality_score"] is None or score < statistics["min_quality_score"]:
                    statistics["min_quality_score"] = score
                if statistics["max_quality_score"] is None or score > statistics["max_quality_score"]:
                    statistics["max_quality_score"] = score
                if detail["status"] == "waiting_manual":
                    statistics["waiting_manual_count"] += 1
                if detail["status"] == "accepted_for_quality_check":
                    statistics["accepted_for_quality_check_count"] += 1
        write_ok = True
    except (OSError, UnicodeDecodeError):
        _unlink(out_tmp)
        _unlink(rej_tmp)
        return _fail("输入文件读取失败")

    if not write_ok:
        _unlink(out_tmp)
        _unlink(rej_tmp)
        return _fail("输入文件读取失败")

    if statistics["input_records"] == 0:
        # 空输入：不留下空产物，避免被下游当有效结果消费。
        _unlink(out_tmp)
        _unlink(rej_tmp)
        message = "输入文件为空或无有效记录"
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

    # 原子替换：保证下游读到的要么是完整新产物，要么是旧产物。
    os.replace(str(out_tmp), str(out_file))
    os.replace(str(rej_tmp), str(rej_file))
    statistics["graded"] = count_jsonl_lines(out_file)
    statistics["rejected_records"] = count_jsonl_lines(rej_file)

    if statistics["rejected_records"] > 0:
        status = "partial"
    elif statistics["C_count"] > 0:
        status = "waiting_manual"
    else:
        status = "success"
    message = ""

    log_action(logger, "处理", f"分级完成：S={statistics['S_count']} A={statistics['A_count']} "
                              f"C={statistics['C_count']}；待人工 {statistics['waiting_manual_count']} 条；"
                              f"异常 {statistics['rejected_records']} 条")
    log_action(logger, "输出", f"写入输出文件：{out_file.name}；异常清单：{rej_file.name}")
    # [无血缘改造 2026-10-19] 血缘输出已停用，保留原实现便于回溯；不再生成 lineage 文件。
    # append_lineage(lineage_file, make_lineage(
    #     OPERATOR_NAME, OPERATOR_VERSION, unique_id,
    #     str(input_file), str(out_file), status, started, now_iso(),
    #     cfg_snapshot, statistics))
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
    parser = argparse.ArgumentParser(description="算子13：S/A/C 对齐质量分级")
    parser.add_argument("--input", required=True, help="输入 12_scored.jsonl")
    parser.add_argument("--unique-id", default="local_demo_file1", help="任务id_文件id")
    parser.add_argument("--config", default=None, help="配置文件路径")
    parser.add_argument("--output-root", default=None, help="产物根目录")
    args = parser.parse_args()
    result = run(args.input, args.unique_id, args.output_root, args.config)
    print(f"status={result['status']} statistics={result['statistics']}")
    print(f"stageZip={result.get('stageZip')} storageUri={result.get('storageUri')}")


if __name__ == "__main__":
    main()
