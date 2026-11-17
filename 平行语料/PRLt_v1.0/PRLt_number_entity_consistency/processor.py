"""算子17 数字与实体一致性检测：核心处理逻辑与 CLI。

业务边界（对应 DEVELOPMENT_PLAN「核心处理逻辑」）：

- 输入：阶段中间文件，字段 `source` / `target`（语言可配）+ `language_pair` + `metadata`；
- 抽取：数字、百分比、货币、日期、时间、白名单单位、可配置实体；
- 归一：Unicode 数字、千分位、小数尾零、大小写、日期/时间形态、白名单单位换算；
- 匹配：类别 + 归一值一一对应，另有可配容差；漏写 / 错写 / 单位变化 / 实体不一致分别留证；
- 输出：逐条追加 `entity_consistency_status`、`entity_evidence`、`entity_backend`、`entity_rule_version`；
  异常进入独立拒绝清单；`statistics` 全部来自真实处理结果。

本模块不做任何「固定分数 / 假降级」：默认规则后端（backend=rule），模型后端不可用时按门禁
降级为规则（运行状态 degraded）或结构化失败。
"""
import argparse
import json
import os
import re
import sys
import yaml
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

if __package__ in (None, ""):
    sys.path.insert(0, str(Path(__file__).resolve().parent))

from utils.artifacts import finalize_stage_artifacts  # noqa: E402
from utils.extract import CATEGORY_ORDER, ExtractionContext, extract_tokens, normalize_text  # noqa: E402
from utils.io import JsonlWriter, count_lines, task_dir  # noqa: E402
from utils.lang import normalize_language_pair  # noqa: E402
from utils.logger import close_logger, get_logger, log_action  # noqa: E402
from utils.match import analyze_pair, decide_status, zero_category  # noqa: E402
from utils.model_backend import (  # noqa: E402
    BackendUnavailable,
    ModelScoreError,
    parse_model_payload,
    resolve_backend,
)
from utils.schemas import OperatorConfig  # noqa: E402
from utils.timeutil import now_iso  # noqa: E402

# [无血缘改造 2026-10-19] 血缘输出已停用，保留原导入便于回溯：
# from utils.lineage import append_lineage, make_lineage, now_iso  # noqa: E402

OPERATOR_NAME = "entity_consistency"
OPERATOR_ID = "17"
OPERATOR_VERSION = "v1.0.0"
RULE_VERSION = "v1.0.0"
OUTPUT_FILE = "17_entity_checked.jsonl"
REJECT_FILE = "17_entity_rejects.jsonl"
# [无血缘改造 2026-10-19] 不再生成 lineage 文件。
# LINEAGE_FILE = "lineage.json"
LOG_FILE = "run.log"

_BASE_DIR = Path(__file__).resolve().parent
DEFAULT_CONFIG_PATH = _BASE_DIR / "configs" / "config.yaml"


class _AbortRun(Exception):
    """内部信号：需要把整次任务判为失败。"""


# --------------------------------------------------------------------------- #
# 配置与编解码
# --------------------------------------------------------------------------- #

def load_config(config_path=None, overrides: Optional[Dict[str, Any]] = None
                ) -> Tuple[OperatorConfig, Dict[str, Any]]:
    """读取 YAML 配置并叠加任务级覆盖；返回 (配置对象, 配置快照)。"""
    path = Path(config_path) if config_path else DEFAULT_CONFIG_PATH
    raw: Dict[str, Any] = {}
    if path.is_file():
        with open(path, "r", encoding="utf-8") as stream:
            loaded = yaml.safe_load(stream) or {}
        if not isinstance(loaded, dict):
            raise ValueError("配置文件根节点必须是对象")
        raw = loaded
    elif config_path:
        raise ValueError(f"配置文件不存在：{path}")

    for key, value in (overrides or {}).items():
        raw[key] = value

    cfg = OperatorConfig(**raw)
    snapshot = cfg.model_dump(mode="json")
    return cfg, snapshot


def select_encoding(path, candidates) -> str:
    """在候选编码中选出能完整解码文件的第一个编码；全部失败抛 ValueError。"""
    last_error = None
    for encoding in candidates:
        try:
            with open(path, "r", encoding=encoding) as stream:
                while stream.read(65536):
                    pass
            return encoding
        except (UnicodeDecodeError, LookupError) as exc:
            last_error = exc
    raise ValueError(f"无法用配置编码解码输入文件（{last_error}）")


def iter_decoded_lines(path, encoding: str, chunk_size: int = 65536):
    """增量解码并按行产出；不把整个文件读入内存。"""
    import codecs

    decoder = codecs.getincrementaldecoder(encoding or "utf-8")()
    buffer = ""
    with open(path, "rb") as stream:
        while True:
            chunk = stream.read(chunk_size)
            if not chunk:
                break
            buffer += decoder.decode(chunk)
            while True:
                index = buffer.find("\n")
                if index < 0:
                    break
                line = buffer[:index]
                buffer = buffer[index + 1:]
                if line.endswith("\r"):
                    line = line[:-1]
                yield line
        buffer += decoder.decode(b"", True)
    if buffer:
        yield buffer.rstrip("\r")


def _iter_records(path, encoding: str):
    """逐行产出 (行号, 记录, 错误码)；损坏行与非法结构不中断流程。"""
    for lineno, line in enumerate(iter_decoded_lines(path, encoding), 1):
        if not line.strip():
            continue
        try:
            record = json.loads(line)
        except json.JSONDecodeError:
            yield lineno, None, "invalid_json"
            continue
        if not isinstance(record, dict):
            yield lineno, None, "invalid_record"
            continue
        yield lineno, record, None


def build_context(cfg: OperatorConfig) -> ExtractionContext:
    return ExtractionContext(cfg)


# --------------------------------------------------------------------------- #
# 业务核心
# --------------------------------------------------------------------------- #

def check_pair(source_text: str, target_text: str, ctx: ExtractionContext) -> Tuple[str, Dict]:
    """单条记录的一致性判定：返回 (状态, evidence_core)。纯函数，便于单测。"""
    evidence = analyze_pair(source_text, target_text, ctx)
    return decide_status(evidence), evidence


def _validate_record(record: Dict, cfg: OperatorConfig):
    """结构 / 语言校验；返回 (拒绝原因列表, 源文本, 目标文本, language_pair)。"""
    reasons: List[str] = []
    source_field, target_field = cfg.text_fields[0], cfg.text_fields[1]
    values: Dict[str, str] = {}

    for field in (source_field, target_field):
        if field not in record:
            reasons.append(f"missing_field:{field}")
            continue
        value = record.get(field)
        if not isinstance(value, str):
            reasons.append(f"invalid_type:{field}")
            continue
        if not value.strip():
            reasons.append(f"empty_{field}")
            continue
        values[field] = value

    metadata = record.get("metadata")
    if metadata is not None and not isinstance(metadata, dict):
        reasons.append("invalid_type:metadata")

    language_pair = None
    if cfg.language_check:
        raw_pair = record.get("language_pair")
        if raw_pair is None:
            source_lang = record.get("source_lang")
            target_lang = record.get("target_lang")
            if isinstance(source_lang, str) and isinstance(target_lang, str):
                raw_pair = f"{source_lang}-{target_lang}"
        language_pair = normalize_language_pair(raw_pair)
        if language_pair is None:
            reasons.append("missing_language_pair")
        elif language_pair != cfg.expected_language_pair():
            reasons.append("language_pair_mismatch")

    if reasons:
        return reasons, None, None, language_pair
    return [], values[source_field], values[target_field], language_pair


def _combine_with_model(rule_status: str, parsed: Dict, evidence_core: Dict, cfg: OperatorConfig):
    """规则结论与模型结论合并；两者不一致或置信不足时保守判为 waiting_manual。"""
    reasons: List[str] = []
    if rule_status == "inconsistent":
        return "inconsistent", reasons

    if parsed["consistent"] is False:
        confidence = parsed["confidence"]
        if confidence is None or confidence >= cfg.model_confidence_threshold:
            reasons.append("model_inconsistent")
            return "inconsistent", reasons
        reasons.append("model_low_confidence")
        return "waiting_manual", reasons

    if rule_status == "waiting_manual" and not evidence_core["scan_truncated"]:
        confidence = parsed["confidence"]
        if confidence is not None and confidence >= cfg.model_confidence_threshold:
            reasons.append("model_resolved")
            return "consistent", reasons
        return "waiting_manual", reasons

    return rule_status, reasons


def _merge_review_status(record: Dict, status: str) -> str:
    """Conservatively carry upstream review state through operator 17.

    Entity consistency is an additional gate; it must never turn an upstream
    waiting/rejected/manual state into ``accepted`` merely because the entity
    scan itself was consistent.
    """
    metadata = record.get("metadata") if isinstance(record.get("metadata"), dict) else {}
    upstream = record.get("review_status")
    if upstream is None:
        upstream = metadata.get("review_status")
    if isinstance(upstream, str):
        upstream = upstream.strip()
        if upstream in {"waiting_manual", "rejected", "needs_revision"}:
            return upstream
        if upstream == "accepted" and status == "consistent":
            return "accepted"
    return "accepted" if status == "consistent" else "waiting_manual"


def _build_output_record(record: Dict, status: str, evidence_core: Dict, backend: str,
                         model_version: Optional[str], model_confidence: Optional[float],
                         model_verdict, extra_reasons: List[str], language_pair) -> Dict:
    output = dict(record)
    reasons = list(evidence_core["conflict_reasons"]) + list(extra_reasons)
    evidence = {
        "rule_version": RULE_VERSION,
        "backend": backend,
        "model_version": model_version,
        "model_confidence": model_confidence,
        "model_verdict": model_verdict,
    }
    evidence.update(evidence_core)

    output["entity_consistency_status"] = status
    output["entity_evidence"] = evidence
    output["entity_backend"] = backend
    output["entity_rule_version"] = RULE_VERSION
    output["entity_model_version"] = model_version
    output["entity_model_confidence"] = model_confidence
    output["entity_conflict_reasons"] = reasons
    output["entity_checked_at"] = now_iso()
    output["review_status"] = _merge_review_status(record, status)
    # Explicit scope mirrors keep sentence and clause branches distinct for
    # packaging, quality reports, and manual review.
    input_metadata = record.get("metadata") if isinstance(record.get("metadata"), dict) else {}
    unit_type = record.get("unit_type") or input_metadata.get("unit_type")
    if not isinstance(unit_type, str) or not unit_type.strip():
        granularity = record.get("granularity") or input_metadata.get("granularity")
        unit_type = {"sentence": "sentence_pair", "clause": "clause_pair",
                     "phrase": "phrase_pair"}.get(str(granularity).strip(), granularity)
    else:
        unit_type = {"sentence": "sentence_pair", "clause": "clause_pair",
                     "phrase": "phrase_pair"}.get(unit_type.strip(), unit_type.strip())
    parent_pair_id = record.get("parent_pair_id") or input_metadata.get("parent_pair_id")
    if not isinstance(parent_pair_id, str) or not parent_pair_id.strip():
        parent_pair_id = record.get("id")
    aggregation_scope = record.get("aggregation_scope") or input_metadata.get("aggregation_scope")
    if not isinstance(aggregation_scope, str) or not aggregation_scope.strip():
        aggregation_scope = "parent_sentence" if unit_type in {"clause_pair", "phrase_pair", "clause", "phrase"} else "sentence"
    output["unit_type"] = unit_type
    output["parent_pair_id"] = parent_pair_id
    output["aggregation_scope"] = aggregation_scope
    if language_pair:
        output["language_pair"] = language_pair

    metadata = dict(record.get("metadata") or {})
    metadata.update({
        "entity_consistency_status": status,
        "entity_backend": backend,
        "entity_rule_version": RULE_VERSION,
        "entity_model_version": model_version,
        "entity_matched_total": evidence_core["totals"]["matched"],
        "entity_missing_in_target_total": evidence_core["totals"]["missing_in_target"],
        "entity_extra_in_target_total": evidence_core["totals"]["extra_in_target"],
        "entity_value_mismatch_total": evidence_core["totals"]["value_mismatch"],
        "entity_unparsed_total": evidence_core["totals"]["unparsed"],
        "review_status": output["review_status"],
        "upstream_review_status": (record.get("review_status")
                                    or metadata.get("review_status")),
        "unit_type": unit_type,
        "parent_pair_id": parent_pair_id,
        "aggregation_scope": aggregation_scope,
    })
    output["metadata"] = metadata
    return output


def _audit_reasons(output_record: Dict) -> List[str]:
    """审计条目的原因码：既给 `type:category`，也给可聚合的 `type`。"""
    codes = set()
    for reason in output_record.get("entity_conflict_reasons", []):
        codes.add(reason)
        if ":" in reason:
            codes.add(reason.split(":", 1)[0])
    return sorted(codes)


def _reject_entry(lineno: int, record, reasons: List[str], output_record=None) -> Dict:
    """拒绝 / 审计条目：只带定位与原因码，绝不携带原文。"""
    entry: Dict[str, Any] = {"line": lineno, "reasons": sorted(set(reasons))}
    if isinstance(record, dict):
        if "id" in record:
            entry["id"] = record.get("id")
        if "language_pair" in record:
            entry["language_pair"] = record.get("language_pair")
    if output_record is not None:
        entry["entity_consistency_status"] = output_record.get("entity_consistency_status")
        entry["entity_backend"] = output_record.get("entity_backend")
        entry["entity_conflict_reasons"] = sorted(set(output_record.get("entity_conflict_reasons", [])))
    return entry


def _base_statistics() -> Dict[str, Any]:
    return {
        "input_records": 0,
        "passed_records": 0,
        "rejected": 0,
        "schema_error": 0,
        "audit_records": 0,
        "moved_to_rejects": 0,
        "consistent": 0,
        "inconsistent": 0,
        "waiting_manual": 0,
        "model_scored_records": 0,
        "model_error_records": 0,
        "matched_total": 0,
        "missing_in_target_total": 0,
        "extra_in_target_total": 0,
        "value_mismatch_total": 0,
        "unparsed_tokens": 0,
        "category_counts": {},
        "backend": None,
        "model_version": None,
        "degrade_reason": None,
        "rule_version": RULE_VERSION,
        "encoding": None,
    }


def _accumulate(stats: Dict, evidence_core: Dict) -> None:
    totals = evidence_core["totals"]
    stats["matched_total"] += totals["matched"]
    stats["missing_in_target_total"] += totals["missing_in_target"]
    stats["extra_in_target_total"] += totals["extra_in_target"]
    stats["value_mismatch_total"] += totals["value_mismatch"]
    stats["unparsed_tokens"] += totals["unparsed"]
    for name, counts in evidence_core["category_counts"].items():
        bucket = stats["category_counts"].setdefault(name, zero_category())
        for key, value in counts.items():
            bucket[key] += value


# --------------------------------------------------------------------------- #
# 主流程
# --------------------------------------------------------------------------- #

def run(input_file, unique_id: str, output_root=None, config_path=None,
        overrides: Optional[Dict[str, Any]] = None, logger=None,
        model_checker=None) -> Dict[str, Any]:
    """执行一次一致性检测。

    返回 {status, outputFile, rejectFile, stageZip, storageUri, statistics, message}；
    status ∈ success/partial/degraded/waiting_manual/failed。
    """
    output_root = Path(output_root) if output_root else (Path.cwd() / "output")
    task_path = task_dir(output_root, unique_id)
    out_file = task_path / OUTPUT_FILE
    reject_file = task_path / REJECT_FILE
    tmp_out = task_path / (OUTPUT_FILE + ".tmp")
    tmp_reject = task_path / (REJECT_FILE + ".tmp")
    # [无血缘改造 2026-10-19] 不再生成 lineage 文件。
    # lineage_path = task_path / LINEAGE_FILE

    own_logger = logger is None
    logger = logger or get_logger(f"{OPERATOR_NAME}.{unique_id}", log_file=task_path / LOG_FILE)

    input_path = Path(input_file).expanduser()
    started = now_iso()
    stats = _base_statistics()
    snapshot: Dict[str, Any] = {}
    written_reject = False

    def finish(status: str, message: str, output=None) -> Dict[str, Any]:
        finished = now_iso()
        # [无血缘改造 2026-10-19] 血缘输出已停用，保留原实现便于回溯；不再生成 lineage 文件。
        # append_lineage(lineage_path, make_lineage(
        #     OPERATOR_NAME, OPERATOR_VERSION, unique_id, str(input_path),
        #     str(output) if output else None, status, started, finished, snapshot, stats))
        log_action(logger, "完成", f"状态={status} 输入={stats['input_records']} "
                                 f"通过={stats['passed_records']} 拒绝={stats['rejected']} 说明={message}")
        stage_zip = storage_uri = None
        if status != "failed" and output:
            # [无血缘改造 2026-10-19] 不再追加血缘；改为打包本阶段产物并登记对象存储地址。
            stage_zip, storage_uri = finalize_stage_artifacts(
                task_path, OPERATOR_ID, unique_id, output,
                reject_file if written_reject else None, task_path / LOG_FILE)
            log_action(logger, "归档", f"阶段产物：{Path(stage_zip).name if stage_zip else '无'}；"
                                     f"存储地址：{storage_uri}")
        if own_logger:
            close_logger(logger)
        return {
            "status": status,
            "outputFile": str(output) if output else None,
            "rejectFile": str(reject_file) if written_reject else None,
            "stageZip": stage_zip,
            "storageUri": storage_uri,
            "statistics": stats,
            "message": message,
        }

    # 1) 配置
    try:
        cfg, snapshot = load_config(config_path, overrides)
    except Exception as exc:  # noqa: BLE001 - 配置错误一律结构化失败
        snapshot = {}
        return finish("failed", f"配置错误：{type(exc).__name__}")

    stats["category_counts"] = {name: zero_category() for name in cfg.normalized_categories()}
    log_action(logger, "配置", f"file={config_path or DEFAULT_CONFIG_PATH} "
                             f"categories={','.join(cfg.normalized_categories())} "
                             f"backend={cfg.backend} tolerance=abs:{cfg.numeric_absolute_tolerance}/"
                             f"rel:{cfg.numeric_relative_tolerance}")

    # 2) 后端门禁
    try:
        backend, checker, degrade_reason = resolve_backend(cfg, model_checker)
    except BackendUnavailable as exc:
        stats["backend"] = "unavailable"
        return finish("failed", f"模型后端不可用：{exc}")

    stats["backend"] = backend
    stats["model_version"] = getattr(checker, "model_version", None) if backend == "model" else None
    if degrade_reason:
        stats["degrade_reason"] = degrade_reason
        log_action(logger, "降级", f"reason={degrade_reason} fallback=rule")
    else:
        log_action(logger, "后端", f"backend={backend}")

    context = build_context(cfg)

    # 3) 输入检查
    if not input_path.is_file():
        return finish("failed", "输入文件不存在")
    try:
        encoding = select_encoding(input_path, cfg.input_encodings)
    except ValueError as exc:
        return finish("failed", f"编码错误：{exc}")
    stats["encoding"] = encoding

    try:
        if input_path.resolve() == out_file.resolve():
            return finish("failed", "输入文件与输出文件路径相同，拒绝就地覆盖")
    except OSError:
        pass

    log_action(logger, "输入", f"file={input_path} encoding={encoding} outputRoot={task_path}")

    # 4) 主循环
    out_writer = JsonlWriter(tmp_out)
    reject_writer = JsonlWriter(tmp_reject)
    model_errors = 0
    try:
        for lineno, record, error in _iter_records(input_path, encoding):
            stats["input_records"] += 1

            if error:
                stats["schema_error"] += 1
                reject_writer.write(_reject_entry(lineno, None, [error]))
                continue

            reasons, source_text, target_text, language_pair = _validate_record(record, cfg)
            if reasons:
                stats["schema_error"] += 1
                reject_writer.write(_reject_entry(lineno, record, reasons))
                continue

            rule_status, evidence_core = check_pair(source_text, target_text, context)
            status = rule_status
            model_version = None
            model_confidence = None
            model_verdict = None
            extra_reasons: List[str] = []

            if backend == "model":
                try:
                    payload = checker.check(source_text, target_text)
                    parsed = parse_model_payload(payload, getattr(checker, "model_version", None))
                except ModelScoreError as exc:
                    model_errors += 1
                    stats["model_error_records"] += 1
                    extra_reasons.append("model_error")
                    status = "waiting_manual"
                    log_action(logger, "模型错误", f"line={lineno} reason={type(exc).__name__}")
                except Exception as exc:  # noqa: BLE001 - 模型异常逐条兜底，不静默
                    model_errors += 1
                    stats["model_error_records"] += 1
                    extra_reasons.append("model_error")
                    status = "waiting_manual"
                    log_action(logger, "模型错误", f"line={lineno} reason={type(exc).__name__}")
                else:
                    stats["model_scored_records"] += 1
                    model_version = parsed["model_version"]
                    model_confidence = parsed["confidence"]
                    model_verdict = parsed["consistent"]
                    if model_version:
                        stats["model_version"] = model_version
                    status, extra_reasons = _combine_with_model(
                        rule_status, parsed, evidence_core, cfg)

                if model_errors > cfg.max_model_errors:
                    raise _AbortRun(f"模型错误条数超过上限（>{cfg.max_model_errors}）")

            if status == "consistent":
                stats["consistent"] += 1
            elif status == "inconsistent":
                stats["inconsistent"] += 1
            else:
                stats["waiting_manual"] += 1

            _accumulate(stats, evidence_core)

            output_record = _build_output_record(
                record, status, evidence_core, backend, model_version, model_confidence,
                model_verdict, extra_reasons, language_pair)

            moved = (
                (status == "inconsistent" and cfg.inconsistent_policy == "move_to_rejects")
                or (status == "waiting_manual" and cfg.waiting_manual_policy == "move_to_rejects")
            )
            if moved:
                stats["moved_to_rejects"] += 1
                reject_writer.write(_reject_entry(
                    lineno, record, [f"policy:{status}"] + _audit_reasons(output_record), output_record))
            else:
                out_writer.write(output_record)
                if status != "consistent":
                    stats["audit_records"] += 1
                    reject_writer.write(_reject_entry(
                        lineno, record, _audit_reasons(output_record), output_record))

        out_writer.close()
        reject_writer.close()
    except _AbortRun as exc:
        out_writer.abort()
        reject_writer.abort()
        return finish("failed", str(exc))
    except Exception as exc:  # noqa: BLE001 - 任何未预期异常都必须结构化暴露
        out_writer.abort()
        reject_writer.abort()
        return finish("failed", f"处理异常：{type(exc).__name__}")

    if stats["input_records"] == 0:
        for temp in (tmp_out, tmp_reject):
            try:
                temp.unlink()
            except OSError:
                pass
        return finish("failed", "输入文件为空或不含有效记录")

    # 5) 原子替换落盘
    os.replace(tmp_out, out_file)
    if reject_writer.count:
        os.replace(tmp_reject, reject_file)
        written_reject = True
    else:
        try:
            tmp_reject.unlink()
        except OSError:
            pass

    stats["passed_records"] = count_lines(out_file)
    stats["rejected"] = count_lines(reject_file) if written_reject else 0

    if stats["schema_error"] or stats["inconsistent"] or stats["moved_to_rejects"]:
        status = "partial"
    elif stats["degrade_reason"]:
        status = "degraded"
    elif stats["waiting_manual"]:
        status = "waiting_manual"
    else:
        status = "success"

    return finish(status, "ok", out_file)


# --------------------------------------------------------------------------- #
# CLI
# --------------------------------------------------------------------------- #

def parse_args(argv=None):
    parser = argparse.ArgumentParser(description="算子17 数字与实体一致性检测")
    parser.add_argument("--input", required=True, help="待检测的 JSONL 输入文件")
    parser.add_argument("--unique-id", required=True, help="任务标识（uniqueId）")
    parser.add_argument("--output-root", default=None, help="输出根目录，默认 ./output")
    parser.add_argument("--config", default=None, help="配置文件路径，默认 configs/config.yaml")
    return parser.parse_args(argv)


def main(argv=None) -> int:
    args = parse_args(argv)
    result = run(args.input, args.unique_id, args.output_root, args.config)
    try:
        print(json.dumps(result, ensure_ascii=False, indent=2))
    except UnicodeEncodeError:
        # 控制台编码（如 cp437）无法表示中文时退化为转义输出，保证 CLI 不崩溃
        print(json.dumps(result, ensure_ascii=True, indent=2))
    return 0 if result["status"] in {"success", "partial", "degraded", "waiting_manual"} else 1


if __name__ == "__main__":
    raise SystemExit(main())
