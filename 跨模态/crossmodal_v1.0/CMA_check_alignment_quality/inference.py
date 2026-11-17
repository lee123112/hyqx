"""算子 20「质检对齐产物」编排入口：配置、任务隔离、三阶段流式执行、血缘与阶段视图。

执行结构（TECHNICAL_DESIGN §4）
  阶段 A 解析输入：阶段首读任务 ``input/`` 清单；阶段内读上一算子临时 ``outputFile``；
  阶段 B 逐行流式质检：写主输出/检查明细的临时文件，隔离与坏行按类落盘；
  阶段 C 整池反向检索：只对「无上游结论」的图文样本做 CLIP 反向检索；
  阶段 D 定稿：回填反向结论、决定 flagged、统计守恒、质量报告与偏科清单。

统计守恒：``input == output + rejected + origin_quarantined + sample_quarantined``；
``flagged`` 是主输出旁路子集，不与主记录重复加总。

可直接运行：

    python inference.py --input dataset/sample/demo.jsonl --unique-id smoke --output-root output
"""
from __future__ import annotations

import argparse
import json
import os
import shutil
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

def _stage_output_name():
    """本阶段输出目录名：<NN>_<算子目录名>（编号读包根 map.json）。"""
    import json as _json
    here = Path(__file__).resolve().parent
    try:
        operators = _json.loads((here.parent / "map.json").read_text(encoding="utf-8"))["operators"]
        for item in operators.values():
            if item.get("package") == here.name:
                return "%02d_%s" % (int(item["formalOperatorNumber"]), here.name)
    except Exception:
        pass
    return here.name


def publish_stage_output(run_dir):
    """[2026-10-24] 设置 CM_RESULTS_ROOT 时，把本阶段最终产物直接发布到
    <root>/<NN>_<算子目录>/：JSONL/JSON（入口、台账、统计）+ 内容有变化的媒体；
    与输入完全相同（按内容哈希判定）的透传媒体不复制。未设置时行为不变。

    输入根识别兼容两种任务目录布局：`<task>/input/`（同机暂存）与
    `<task>/` 根目录直接铺开（/uploadFile 解压，入口 JSONL 在任务根）。
    """
    import hashlib
    import os
    import shutil
    base = os.environ.get("CM_RESULTS_ROOT")
    if not base:
        return None
    run_dir = Path(run_dir)
    roots = []
    cursor = run_dir
    for _ in range(6):
        if (cursor / "input").is_dir():
            roots = [cursor / "input"]
            break
        if cursor != run_dir and any(cursor.glob("*.jsonl")):
            roots = [cursor]
            break
        if cursor.parent == cursor:
            break
        cursor = cursor.parent
    dest = Path(base) / _stage_output_name()
    tmp = dest.parent / (dest.name + ".publishing")
    shutil.rmtree(tmp, ignore_errors=True)
    tmp.mkdir(parents=True, exist_ok=True)

    def _iter_files(root):
        for candidate in root.rglob("*"):
            try:
                if not candidate.is_file():
                    continue
            except OSError:
                continue
            if candidate == run_dir or run_dir in candidate.parents:
                continue
            yield candidate

    skip_names = {"manifest.json", "config.snapshot.json", "run.log",
                  "lineage.json", "stage_lineage.json", "stage_context.json"}
    skip_suffixes = {".tmp", ".part", ".partial", ".lock"}
    candidates = []
    for source in sorted(run_dir.rglob("*")):
        try:
            if not source.is_file():
                continue
        except OSError:
            continue
        if source.name in skip_names or source.suffix.lower() in skip_suffixes:
            continue
        candidates.append(source)

    sizes = set()
    for source in candidates:
        if source.suffix.lower() not in (".jsonl", ".json"):
            try:
                sizes.add(source.stat().st_size)
            except OSError:
                pass
    input_digests = {}
    for root in roots:
        for candidate in _iter_files(root):
            try:
                if candidate.stat().st_size not in sizes:
                    continue
                input_digests.setdefault(candidate.stat().st_size, set()).add(
                    hashlib.sha256(candidate.read_bytes()).digest())
            except OSError:
                continue

    for source in candidates:
        if source.suffix.lower() not in (".jsonl", ".json"):
            try:
                digests = input_digests.get(source.stat().st_size)
                if digests and hashlib.sha256(source.read_bytes()).digest() in digests:
                    continue
            except OSError:
                pass
        target = tmp / source.relative_to(run_dir)
        target.parent.mkdir(parents=True, exist_ok=True)
        try:
            os.link(source, target)
        except OSError:
            shutil.copy2(source, target)
    if dest.exists():
        shutil.rmtree(dest, ignore_errors=True)
    tmp.rename(dest)
    return dest

from typing import Any, Dict, List, Optional, Tuple

from utils import alignment_qc as qc
from utils import io as io_utils
from utils import logger as logger_utils
from utils import media_probe, reverse_verify as rv, schemas, transport
from utils import upstream_metrics as um
from utils.io import JsonlWriter
from utils.media_carry import MediaCarrier

_TZ = timezone(timedelta(hours=8))


def _now_iso() -> str:
    """[v4.2] 时间工具；不再借用血缘模块。"""
    return datetime.now(_TZ).isoformat(timespec="seconds")

BASE_DIR = Path(__file__).resolve().parent
DEFAULT_CONFIG_PATH = BASE_DIR / "configs" / "config.yaml"
DEFAULT_OUTPUT_DIR = BASE_DIR / "output"

STAGE_ID = qc.STAGE_ID
OPERATOR_NAME = qc.OPERATOR_NAME
OPERATOR_VERSION = qc.OPERATOR_VERSION


class InputError(ValueError):
    """输入不合法（找不到、编码错误、跨任务引用等）。"""


class DependencyError(RuntimeError):
    """必需的后端/权重缺失且配置不允许降级。"""


def default_config_path() -> Path:
    return DEFAULT_CONFIG_PATH


def _output_root(output_root=None) -> Path:
    candidate = output_root or os.environ.get("OUTPUT_DIR") or DEFAULT_OUTPUT_DIR
    return io_utils.resolve_root(candidate)


# --------------------------------------------------------------------------- #
# 阶段 A：输入解析
# --------------------------------------------------------------------------- #

def resolve_input(task_root: Path, input_file: Optional[str],
                  allow_external_input: bool = False) -> Path:
    """定位输入入口文件。

    - 传 ``inputFile``：必须落在当前任务目录内（阶段内串联），
      仅 CLI/离线调试允许 ``allow_external_input=True`` 读仓库样例；
    - 不传：读任务 ``input/`` 目录（阶段首，工程侧已上传）。
    """
    task = io_utils.resolved_absolute(task_root)
    if input_file:
        if allow_external_input:
            target = io_utils.resolved_absolute(input_file)
            if not target.is_file():
                raise InputError("输入文件不存在：%s" % input_file)
            return target
        try:
            target = io_utils.resolve_in_task(task, input_file)
        except ValueError as exc:
            raise InputError(str(exc)) from exc
        if not target.is_file():
            raise InputError("输入文件不存在：%s" % input_file)
        return target

    input_dir = task / "input"
    if not input_dir.is_dir():
        raise InputError("任务输入目录不存在：%s" % input_dir)
    manifest = transport.load_manifest(input_dir)
    try:
        entry, _relative = transport.resolve_entrypoint(input_dir, manifest)
    except transport.TransportError as exc:
        raise InputError(str(exc)) from exc
    return entry


# [v4.2] 血缘输出已停用：不再读取上一算子运行目录的 lineage.json /
# lineage_history.json（原实现保留注释存档，_load_stage_history 已无调用点）。
# def _load_stage_history(source: Path) -> Tuple[List[Dict[str, Any]], List[Dict[str, Any]]]:
#     """返回 ``(阶段内已有血缘, 历史阶段血缘)``。
#
#     阶段内已有血缘来自上一算子的运行目录（``inputFile`` 的同级 ``lineage.json``）；
#     历史阶段血缘由工程侧随包回传 ``lineage_history.json``。
#     """
#     stage_records = lineage_utils.load_lineage(source.parent / lineage_utils.LINEAGE_NAME)
#     prior_records = lineage_utils.load_prior_lineage([source.parent])
#     return stage_records, prior_records


# --------------------------------------------------------------------------- #
# 反向验证辅助
# --------------------------------------------------------------------------- #

#: 指标汇总的互斥分档（``quality_subset_fallback`` 的命中情况一目了然）
_METRIC_BUCKETS = um.METRIC_BUCKETS


def _metric_bucket(item: Dict[str, Any]) -> str:
    """把单条指标结果归入唯一分档（实现来源：``utils.upstream_metrics``）。"""
    return um.metric_bucket(item)


def _reverse_check(status: str, reason: Optional[str],
                   evidence: Dict[str, Any]) -> Dict[str, Any]:
    return {"status": status, "reason": reason, "evidence": evidence}


def _local_reverse_entry(result: Dict[str, Any]) -> Dict[str, Any]:
    """把整池检索结果转成本算子的检查项（保持「不伪造分数」）。"""
    if result.get("check_status") == "checked":
        if result.get("consistent"):
            return _reverse_check(qc.STATUS_OK, None,
                                  {"source": "local", "rank": result.get("rank"),
                                   "similarity": result.get("similarity"),
                                   "pool_size": result.get("pool_size"),
                                   "model": result.get("model")})
        return _reverse_check(qc.STATUS_FAILED, "reverse_inconsistent",
                              {"source": "local", "rank": result.get("rank"),
                               "similarity": result.get("similarity"),
                               "pool_size": result.get("pool_size"),
                               "model": result.get("model")})
    return _reverse_check(qc.STATUS_INSUFFICIENT, result.get("reason") or "insufficient",
                          {"source": "local", "pool_size": result.get("pool_size"),
                           "model": result.get("model")})


def _apply_local_reverse(node: Dict[str, Any], business: Dict[str, Any],
                         result: Dict[str, Any]) -> Tuple[bool, bool]:
    """把本地反向检索结论写回节点，返回 ``(是否 flagged, 是否 degraded)``。"""
    entry = _local_reverse_entry(result)
    node["checks"][qc.CHECK_REVERSE_VERIFY] = entry
    node["reverse_verify"] = dict(result, source="local")

    if node.get("reason_code") == "probe_backend_missing":
        # 媒体探测后端缺失已判定待复核，反向结论只做补充记录
        return True, True

    if entry["status"] == qc.STATUS_OK:
        # [v4.2] 记录 status 双字段：int status（1 正常）+ process_status 流程态
        node.update({"decision": qc.DECISION_ACCEPTED, "label": qc.LABEL_PASSED,
                     "destination": qc.DESTINATION_MAIN, "reason_code":
                     "quality_check_passed", "status": 1, "process_status": "success",
                     "reason": "通过文件可打开/模态/解码/静音/文本/指标与反向验证检查"})
        return False, False

    reason = ("reverse_inconsistent" if entry["status"] == qc.STATUS_FAILED
              else (result.get("reason") or "insufficient"))
    degraded = entry["status"] == qc.STATUS_INSUFFICIENT
    node.update({"decision": qc.DECISION_FLAGGED, "label": qc.LABEL_PENDING_REVIEW,
                 "destination": qc.DESTINATION_REVIEW, "reason_code": reason,
                 "status": 1, "process_status": "pending_review",
                 "reason": "本地反向检索未通过或证据不足，标记待人工复核（不拦样本）"})
    return True, degraded


# --------------------------------------------------------------------------- #
# 主流程
# --------------------------------------------------------------------------- #

def run(input_file: Optional[str], unique_id: str, config_path=None,
        output_root=None, overrides: Optional[Dict[str, Any]] = None,
        allow_external_input: bool = False) -> Dict[str, Any]:
    """执行一次质检任务，返回结构化运行结果。"""
    started_at = _now_iso()
    overrides = schemas.validate_overrides(overrides)
    config = schemas.load_config(config_path or DEFAULT_CONFIG_PATH, overrides)
    root = _output_root(output_root)
    value = io_utils.validate_unique_id(unique_id)

    with io_utils.TaskLock(root, value):
        task = io_utils.task_dir(root, value)
        operator_dir = task / OPERATOR_NAME
        run_id = io_utils.next_run_id(operator_dir)
        run_dir = operator_dir / run_id
        run_dir.mkdir(parents=True, exist_ok=True)
        log_path = run_dir / qc.LOG_NAME
        logger = logger_utils.get_logger("operator20.%s.%s" % (value, run_id), log_path)
        try:
            return _execute(config=config, overrides=overrides, task=task,
                            run_dir=run_dir, run_id=run_id, unique_id=value,
                            input_file=input_file,
                            allow_external_input=allow_external_input,
                            logger=logger, started_at=started_at)
        except Exception as exc:
            logger_utils.log_action(logger, "run_failed", "%s: %s"
                                    % (type(exc).__name__, str(exc)[:300]))
            _write_failure(run_dir, type(exc).__name__, str(exc)[:1000], started_at)
            raise
        finally:
            logger_utils.log_action(logger, "close_logger", "关闭日志句柄")
            logger_utils.close_logger(logger)


def _write_failure(run_dir: Path, error_type: str, message: str,
                   started_at: str) -> None:
    try:
        io_utils.atomic_write_json(run_dir / "failure.json", {
            "operator": OPERATOR_NAME, "operator_version": OPERATOR_VERSION,
            "stage_id": STAGE_ID, "status": "failed",
            "errorType": error_type, "message": message,
            "startedAt": started_at, "finishedAt": _now_iso(),
            "hint": "本次未产出任何业务结论；修正配置/输入后重新调用 /predict",
        })
    except Exception:  # noqa: BLE001 - 失败快照本身不能再抛异常
        pass


def _execute(*, config, overrides, task: Path, run_dir: Path, run_id: str,
             unique_id: str, input_file: Optional[str],
             allow_external_input: bool, logger, started_at: str) -> Dict[str, Any]:
    log_path = run_dir / qc.LOG_NAME
    business = config.business_dict()
    snapshot = schemas.config_snapshot(config, overrides.keys())
    backends = media_probe.available_backends()

    # 阶段 A -----------------------------------------------------------------
    try:
        source = resolve_input(task, input_file, allow_external_input)
    except InputError:
        raise
    if not source.is_file():
        raise InputError("输入文件不存在：%s" % source)
    try:
        io_utils.assert_utf8(source)
    except UnicodeDecodeError as exc:
        raise InputError("输入文件不是严格 UTF-8：%s" % source.name) from exc

    # [v4.2] 血缘输出已停用：不再读取阶段内/历史血缘（utils/lineage.py 保留存档）
    # stage_history, prior_history = _load_stage_history(source)
    stage_history: List[Dict[str, Any]] = []
    prior_history: List[Dict[str, Any]] = []
    media_root = source.parent
    carrier = MediaCarrier(media_root, run_dir, reserved=qc.OUTPUT_NAMES)
    logger_utils.log_action(logger, "resolve_input", "输入=%s 阶段内已有血缘=%d 历史阶段血缘=%d"
                            % (source.name, len(stage_history), len(prior_history)))

    reverse_cfg = qc.reverse_config(business)
    fallback_ratio = float(business.get("quality_subset_fallback", 0.10))

    # 阶段 B：逐行流式质检 ---------------------------------------------------
    main_partial = run_dir / (qc.MAIN_NAME + ".partial")
    checks_partial = run_dir / (qc.CHECKS_NAME + ".partial")
    counters = qc.checks_template()
    metric_accum: Dict[str, Dict[str, int]] = {
        kind: {bucket: 0 for bucket in _METRIC_BUCKETS}
        for kind in um.METRIC_KINDS}
    stats = {"input": 0, "input_lines": 0, "blank_lines": 0, "rejected": 0,
             "output": 0, "origin_quarantined": 0, "sample_quarantined": 0,
             "duplicates": 0, "skipped": 0}
    seen_ids: set = set()
    not_applicable_records = 0
    pool: List[Dict[str, Any]] = []
    targets: List[Dict[str, Any]] = []
    reverse_rows: Dict[str, Dict[str, Any]] = {}
    main_order: Dict[str, int] = {}
    accepted_ids: List[str] = []

    with JsonlWriter(main_partial) as main_writer, \
            JsonlWriter(checks_partial) as checks_writer, \
            JsonlWriter(run_dir / qc.ORIGINAL_QUARANTINE_NAME) as origin_writer, \
            JsonlWriter(run_dir / qc.SAMPLE_QUARANTINE_NAME) as sample_writer, \
            JsonlWriter(run_dir / qc.REJECTS_NAME) as reject_writer:
        for lineno, line in enumerate(io_utils.iter_decoded_lines(source, "utf-8"), 1):
            stats["input_lines"] += 1
            if not line.strip():
                stats["blank_lines"] += 1
                continue
            stats["input"] += 1
            try:
                record = json.loads(line)
            except json.JSONDecodeError:
                stats["rejected"] += 1
                reject_writer.write(qc.build_reject_entry(
                    lineno, "json_parse_error", line, business, unique_id, run_id))
                continue
            if not isinstance(record, dict):
                stats["rejected"] += 1
                reject_writer.write(qc.build_reject_entry(
                    lineno, "record_not_object", line, business, unique_id, run_id))
                continue

            raw_id = record.get("sample_id")
            sample_id = raw_id.strip() if isinstance(raw_id, str) and raw_id.strip() else None
            if sample_id and sample_id in seen_ids:
                stats["duplicates"] += 1
                stats["origin_quarantined"] += 1
                origin_writer.write(qc.build_duplicate_entry(
                    lineno, sample_id, record, business, unique_id, run_id))
                continue
            if sample_id:
                seen_ids.add(sample_id)

            outcome = qc.evaluate_record(
                record, sample_id=sample_id, media_root=media_root, carrier=carrier,
                business=business,
                fallback_selected=bool(sample_id
                                       and um.is_fallback_selected(sample_id, fallback_ratio)),
                lineno=lineno)

            touched = qc.record_check_stats(counters, outcome.checks)
            if touched:
                not_applicable_records += 1
            for kind in um.METRIC_KINDS:
                item = outcome.metrics.get(kind) or {}
                metric_accum[kind][_metric_bucket(item)] += 1

            if outcome.in_main_output:
                stats["output"] += 1
                if outcome.decision == qc.DECISION_SKIPPED:
                    stats["skipped"] += 1
                main_order[outcome.sample_id] = stats["output"]
                main_writer.write(outcome.record)
                accepted_ids.append(outcome.sample_id or "")
                if outcome.pool_entry is not None:
                    pool.append(outcome.pool_entry)
                if outcome.reverse_candidate is not None:
                    targets.append(outcome.reverse_candidate)
                if outcome.reverse is not None:
                    reverse_rows[outcome.sample_id] = rv.to_reverse_record(
                        outcome.sample_id, outcome.reverse, {"line": lineno})
            elif outcome.decision == qc.DECISION_SAMPLE_QUARANTINE:
                stats["sample_quarantined"] += 1
                sample_writer.write(qc.build_sample_ledger(outcome, business,
                                                           unique_id, run_id))
            elif outcome.decision == qc.DECISION_ORIGIN_QUARANTINE:
                stats["origin_quarantined"] += 1
                origin_writer.write(qc.build_origin_ledger(outcome, business,
                                                           unique_id, run_id))
            if outcome.in_main_output:
                checks_writer.write(qc.build_checks_entry(outcome, lineno))
            else:
                checks_writer.write(qc.build_checks_entry(outcome, lineno))

    logger_utils.log_action(logger, "stream_qc",
                            "行=%d 有效=%d 输出=%d 原始隔离=%d 样本隔离=%d 坏行=%d 重复=%d"
                            % (stats["input_lines"], stats["input"], stats["output"],
                               stats["origin_quarantined"], stats["sample_quarantined"],
                               stats["rejected"], stats["duplicates"]))

    # 阶段 C：整池反向检索 ---------------------------------------------------
    local_results: Dict[str, Dict[str, Any]] = {}
    reverse_unavailable_reason: Optional[str] = None
    encoder: Optional[Any] = None
    if reverse_cfg["enabled"] and pool:
        encoder, reason = rv.build_encoder(reverse_cfg["clip"])
        reverse_unavailable_reason = reason
        if encoder is not None:
            logger_utils.log_action(logger, "reverse_encoder",
                                    "编码器=%s 候选池=%d 待本地检索=%d"
                                    % (getattr(encoder, "name", "unknown"), len(pool),
                                       len(targets)))
        else:
            logger_utils.log_action(logger, "reverse_encoder_degraded",
                                    "反向检索不可用（%s），候选池=%d，待本地检索=%d"
                                    % (reason, len(pool), len(targets)))
        if targets:
            verification = rv.verify_pool(pool, encoder,
                                          top_k=reverse_cfg["top_k"],
                                          min_consistency=reverse_cfg["min_consistency"])
            for target in targets:
                sample_id = target["sample_id"]
                result = verification["results"].get(sample_id)
                if result is None:  # pragma: no cover - 池与目标同源，不应发生
                    continue
                local_results[sample_id] = result
                reverse_rows[sample_id] = rv.to_reverse_record(
                    sample_id, dict(result, source="local"), {"line": None})
            logger_utils.log_action(logger, "reverse_verified",
                                    "本地检索=%d 池=%d" % (len(local_results),
                                                          verification["pool_size"]))
            unavailable = sum(1 for item in local_results.values()
                              if item.get("reason") == "model_unavailable")
            if unavailable and not bool(business.get("allow_degraded", True)):
                raise DependencyError(
                    "CLIP 权重不可用（%s），配置 allow_degraded=false 时不允许产出"
                    "未验证的反向结论" % (reverse_unavailable_reason or "unknown"))

    # 阶段 D：定稿 -----------------------------------------------------------
    flagged_count = 0
    degraded_records = 0
    route_counts: Dict[str, int] = {}
    flagged_partial = run_dir / (qc.FLAGGED_NAME + ".partial")
    main_final = run_dir / (qc.MAIN_NAME + ".final")
    checks_final = run_dir / (qc.CHECKS_NAME + ".final")

    with open(main_partial, "r", encoding="utf-8") as source_stream, \
            JsonlWriter(main_final) as main_writer, \
            JsonlWriter(flagged_partial) as flagged_writer:
        for line in source_stream:
            if not line.strip():
                continue
            record = json.loads(line)
            sample_id = record.get("sample_id")
            node = record.get("processing", {}).get("20")
            if node is None:  # pragma: no cover - 主输出必然带本算子节点
                continue
            result = local_results.get(sample_id)
            degraded = False
            if result is not None:
                flagged, degraded = _apply_local_reverse(node, business, result)
            else:
                flagged = node.get("decision") == qc.DECISION_FLAGGED
                degraded = node.get("reason_code") == "probe_backend_missing"
            record["operator_20"] = qc._flat_summary(node)
            main_writer.write(record)

            route = node.get("route") or "+".join(sorted(node.get("modalities") or []))
            if route:
                route_counts[route] = route_counts.get(route, 0) + 1
            if flagged:
                flagged_count += 1
                ledger = {
                    "operator": OPERATOR_NAME,
                    "operator_number": qc.FORMAL_OPERATOR_NUMBER,
                    "operator_version": qc.OPERATOR_VERSION,
                    "stage_id": STAGE_ID,
                    "uniqueId": unique_id,
                    "runId": run_id,
                    "sample_id": sample_id,
                    "action": "flag",
                    "decision": qc.DECISION_FLAGGED,
                    "reason": node.get("reason_code"),
                    "detail": node.get("reason"),
                    "label": node.get("label"),
                    "destination": node.get("destination"),
                    "route": route,
                    "review_required": True,
                    "human_verified": False,
                    "deliverable": False,
                    "source_reference": "operator_20",
                    "detected_at": _now_iso(),
                    "record": record,
                }
                flagged_writer.write(ledger)
            if degraded or node.get("reason_code") == "probe_backend_missing":
                degraded_records += 1

    # Initial streaming totals still contain pending_pool. Recount every final
    # check entry, including quarantine records, after local retrieval settles.
    counters = qc.checks_template()
    with open(checks_partial, "r", encoding="utf-8") as source_stream, \
            JsonlWriter(checks_final) as checks_writer:
        for line in source_stream:
            if not line.strip():
                continue
            item = json.loads(line)
            result = local_results.get(item.get("sample_id"))
            if result is not None:
                entry = _local_reverse_entry(result)
                item.setdefault("checks", {})[qc.CHECK_REVERSE_VERIFY] = entry
                item["reverse_verify"] = dict(result, source="local")
            qc.record_check_stats(counters, item.get("checks") or {})
            checks_writer.write(item)

    # 原子替换定稿产物
    os.replace(str(main_final), str(run_dir / qc.MAIN_NAME))
    os.replace(str(checks_final), str(run_dir / qc.CHECKS_NAME))
    os.replace(str(flagged_partial), str(run_dir / qc.FLAGGED_NAME))
    io_utils.cleanup_files(main_partial, checks_partial)

    # 反向验证结果（按主输出顺序排列，便于人工核对）
    reverse_ordered = sorted(reverse_rows.items(),
                             key=lambda pair: main_order.get(pair[0], 10 ** 9))
    io_utils.write_jsonl(run_dir / qc.REVERSE_NAME, [item for _sid, item in reverse_ordered])

    # 偏科清单与质量报告
    supplement = qc.build_supplement_list(
        route_counts, business.get("topic_skew_threshold", 0.70),
        unique_id=unique_id, run_id=run_id, operator=OPERATOR_NAME,
        operator_version=OPERATOR_VERSION, stage_id=STAGE_ID,
        generated_at=_now_iso())
    io_utils.atomic_write_text(run_dir / qc.SUPPLEMENT_NAME, qc.dump_json(supplement))
    logger_utils.log_action(logger, "topic_skew",
                            "偏科=%s 主路由=%s 占比=%.4f 待补项=%d"
                            % (supplement["skewed"], supplement["dominant_route"],
                               supplement["dominant_share"],
                               len(supplement["supplement_targets"])))

    # 统计与守恒
    reverse_summary = qc.summarize_reverse([item for _sid, item in reverse_ordered])
    origin_lines = _count_lines(run_dir / qc.ORIGINAL_QUARANTINE_NAME)
    sample_lines = _count_lines(run_dir / qc.SAMPLE_QUARANTINE_NAME)
    reject_lines = _count_lines(run_dir / qc.REJECTS_NAME)
    conservation = (stats["input"] == stats["output"] + stats["rejected"]
                    + stats["origin_quarantined"] + stats["sample_quarantined"])
    statistics: Dict[str, Any] = {
        "input": stats["input"],
        "input_lines": stats["input_lines"],
        "blank_lines": stats["blank_lines"],
        "output": stats["output"],
        "rejected": stats["rejected"],
        "origin_quarantined": stats["origin_quarantined"],
        "sample_quarantined": stats["sample_quarantined"],
        "flagged": flagged_count,
        "duplicates": stats["duplicates"],
        "skipped": stats["skipped"],
        "not_applicable": not_applicable_records,
        "checks": counters,
        "metrics": metric_accum,
        "reverse_verification": reverse_summary,
        "route_distribution": route_counts,
        "media_carried": carrier.count,
        "media_bytes": carrier.total_bytes,
        "degraded_records": degraded_records,
        "model_available": bool(encoder is not None),
        "fallback": um.fallback_usage(fallback_ratio, accepted_ids),
        "origin_ledger_lines": origin_lines,
        "sample_ledger_lines": sample_lines,
        "reject_ledger_lines": reject_lines,
        "conservation_ok": bool(conservation),
        "empty_input": stats["input"] == 0,
        "samples_sha256": qc.digest_sample_ids(accepted_ids),
    }
    status, message = _decide_status(stats, degraded_records)
    logger_utils.log_action(
        logger, "statistics",
        "input=%d output=%d flagged=%d origin=%d sample=%d rejected=%d degraded=%d 守恒=%s"
        % (statistics["input"], statistics["output"], flagged_count,
           statistics["origin_quarantined"], statistics["sample_quarantined"],
           statistics["rejected"], degraded_records, conservation))

    report = qc.build_quality_report(
        statistics=statistics, checks=counters, metrics=metric_accum,
        reverse=reverse_summary, topic_skew=supplement, unique_id=unique_id,
        run_id=run_id, operator=OPERATOR_NAME, operator_version=OPERATOR_VERSION,
        stage_id=STAGE_ID, generated_at=_now_iso(),
        config_snapshot=snapshot, reverse_available=bool(encoder is not None),
        routes=route_counts)
    io_utils.atomic_write_text(run_dir / qc.REPORT_NAME, qc.dump_json(report))

    # 产物与运行清单
    sidecars = carrier.carry_sidecars()
    artifacts: List[Dict[str, str]] = [
        {"path": qc.MAIN_NAME, "role": "main"},
        {"path": qc.CHECKS_NAME, "role": "quality_checks"},
        {"path": qc.REVERSE_NAME, "role": "reverse_verification"},
        {"path": qc.ORIGINAL_QUARANTINE_NAME, "role": "origin_quarantine"},
        {"path": qc.SAMPLE_QUARANTINE_NAME, "role": "sample_quarantine"},
        {"path": qc.FLAGGED_NAME, "role": "review_queue"},
        {"path": qc.REJECTS_NAME, "role": "rejects"},
        {"path": qc.REPORT_NAME, "role": "quality_report"},
        {"path": qc.SUPPLEMENT_NAME, "role": "supplement_list"},
        {"path": qc.LOG_NAME, "role": "log"},
        {"path": transport.MANIFEST_NAME, "role": "manifest"},
    ] + [{"path": name, "role": "media"} for name in carrier.media_files] \
      + [{"path": name, "role": "sidecar"} for name in sidecars]

    manifest = transport.build_manifest(qc.MAIN_NAME, artifacts)
    transport.write_manifest(run_dir, manifest)
    publish_stage_output(run_dir)

    finished_at = _now_iso()
    # [v4.2] 血缘/阶段归档输出已停用：不再构建 lineage.json / stage_context.json
    # （代码保留注释存档，不删除；lineage_utils.now_iso 仅作时间工具）
    # node = lineage_utils.make_lineage(
    #     operator=OPERATOR_NAME, version=OPERATOR_VERSION, unique_id=unique_id,
    #     stage_id=STAGE_ID, run_id=run_id, input_file=str(source),
    #     output_file=str(run_dir / qc.MAIN_NAME), status=status,
    #     started_at=started_at, finished_at=finished_at, config=snapshot,
    #     statistics=statistics, artifacts=artifacts,
    #     formal_operator_number=qc.FORMAL_OPERATOR_NUMBER, message=message)
    # lineage_rows = list(stage_history) + [node]
    # io_utils.atomic_write_text(run_dir / lineage_utils.LINEAGE_NAME,
    #                            json.dumps(lineage_rows, ensure_ascii=False, indent=2))
    # stage_payload = lineage_utils.to_stage_lineage(lineage_rows)
    # context = lineage_utils.build_stage_context(
    #     STAGE_ID, unique_id, run_id, qc.MAIN_NAME, stage_payload, prior_history,
    #     business=business)
    # io_utils.atomic_write_text(run_dir / lineage_utils.STAGE_CONTEXT_NAME,
    #                            json.dumps(context, ensure_ascii=False, indent=2))
    logger_utils.log_action(logger, "finalize", "状态=%s 产物=%d 媒体=%d"
                            % (status, len(artifacts), carrier.count))

    result: Dict[str, Any] = {
        "operator": OPERATOR_NAME,
        "operatorVersion": OPERATOR_VERSION,
        "operatorNumber": qc.FORMAL_OPERATOR_NUMBER,
        "stageId": STAGE_ID,
        "uniqueId": unique_id,
        "runId": run_id,
        "taskDir": str(task),
        "runDir": str(run_dir),
        "entrypoint": qc.MAIN_NAME,
        "outputFile": str(run_dir / qc.MAIN_NAME),
        "checksFile": str(run_dir / qc.CHECKS_NAME),
        "reverseFile": str(run_dir / qc.REVERSE_NAME),
        "originQuarantineFile": str(run_dir / qc.ORIGINAL_QUARANTINE_NAME),
        "sampleQuarantineFile": str(run_dir / qc.SAMPLE_QUARANTINE_NAME),
        "flaggedFile": str(run_dir / qc.FLAGGED_NAME),
        "rejectFile": str(run_dir / qc.REJECTS_NAME),
        "reportFile": str(run_dir / qc.REPORT_NAME),
        "supplementFile": str(run_dir / qc.SUPPLEMENT_NAME),
        # [v4.2] 血缘输出已停用：lineageFile/stageContextFile/lineage/stageLineage/
        # priorLineage 不再返回
        "manifestFile": str(run_dir / transport.MANIFEST_NAME),
        "logFile": str(log_path),
        "startedAt": started_at,
        "finishedAt": finished_at,
        "status": status,
        "message": message,
        "statistics": statistics,
        "artifacts": artifacts,
        "reportStatus": qc.REPORT_STATUS,
        "backends": backends,
        "media": carrier.summary(),
    }
    return result


def _count_lines(path: Path) -> int:
    if not path.is_file():
        return 0
    with open(path, "r", encoding="utf-8") as stream:
        return sum(1 for line in stream if line.strip())


def _decide_status(stats: Dict[str, int], degraded_records: int) -> Tuple[str, str]:
    """运行状态：空输入 → success；有降级 → degraded；有处置 → partial；否则 success。"""
    if stats["input"] == 0:
        return "success", "输入为空，按空文件成功处理"
    if degraded_records > 0:
        return "degraded", ("存在 %d 条样本因后端/权重缺失未能完成全部检查，已标记待人工复核"
                            % degraded_records)
    if stats["origin_quarantined"] or stats["sample_quarantined"] or stats["rejected"]:
        return "partial", ("完成质检并产出隔离建议：原始隔离=%d 样本隔离=%d 坏行=%d"
                           % (stats["origin_quarantined"], stats["sample_quarantined"],
                              stats["rejected"]))
    return "success", "全部样本通过质检"


# --------------------------------------------------------------------------- #
# 响应构造（供 app.py 复用）
# --------------------------------------------------------------------------- #

# [v4.2] 血缘输出已停用：下列阶段形态函数不再被 app.py 调用，保留存档。
def build_intermediate_lineage(result: Dict[str, Any]) -> Dict[str, Any]:
    """[v4.2 已停用] 中间节点血缘对象；响应统一改为 outputFile + result。"""
    return {}


def build_stage_payload(result: Dict[str, Any]) -> List[Dict[str, Any]]:
    """[v4.2 已停用] 阶段末完整阶段血缘；响应统一改为 outputFile + result。"""
    return []


def stage_file_object(result: Dict[str, Any]) -> Dict[str, Any]:
    """阶段末的文件内容对象：入口主清单 + 报告 + 隔离明细 + 媒体。"""
    run_dir = Path(result["runDir"])
    paths = [qc.MAIN_NAME] + [artifact["path"] for artifact in result["artifacts"]
                              if artifact["path"] != qc.MAIN_NAME]
    return transport.pack_file_object(run_dir, qc.MAIN_NAME, paths)


# --------------------------------------------------------------------------- #
# CLI
# --------------------------------------------------------------------------- #

def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="算子 20 质检对齐产物（CLI 与 HTTP 共用同一套业务实现）")
    parser.add_argument("--input", dest="input_file", default=None,
                        help="输入 JSONL 或运行目录（不传则读 OUTPUT_DIR/<uniqueId>/input/）")
    parser.add_argument("--unique-id", dest="unique_id", required=True,
                        help="任务标识（只能作为单个目录名）")
    parser.add_argument("--config", dest="config_path", default=str(DEFAULT_CONFIG_PATH),
                        help="配置文件路径")
    parser.add_argument("--output-root", dest="output_root", default=None,
                        help="输出根目录（默认 OUTPUT_DIR 或 ./output）")
    parser.add_argument("--override", dest="overrides", action="append", default=[],
                        help="业务覆盖，形如 key=value，可重复；嵌套用点号")
    return parser


def _parse_overrides(items: List[str]) -> Dict[str, Any]:
    overrides: Dict[str, Any] = {}
    for item in items or []:
        if "=" not in item:
            raise schemas.ConfigError("override 必须形如 key=value：%s" % item)
        key, raw = item.split("=", 1)
        try:
            value: Any = json.loads(raw)
        except json.JSONDecodeError:
            value = raw
        cursor = overrides
        parts = key.split(".")
        for part in parts[:-1]:
            cursor = cursor.setdefault(part, {})
        cursor[parts[-1]] = value
    return overrides


def main(argv: Optional[List[str]] = None) -> int:
    args = _build_parser().parse_args(argv)
    try:
        result = run(input_file=args.input_file, unique_id=args.unique_id,
                     config_path=args.config_path, output_root=args.output_root,
                     overrides=_parse_overrides(args.overrides),
                     allow_external_input=True)
    except (schemas.ConfigError, InputError, io_utils.TaskBusyError) as exc:
        print(json.dumps({"code": 1, "msg": str(exc), "data": {}},
                         ensure_ascii=False), file=sys.stderr)
        return 2
    except DependencyError as exc:
        print(json.dumps({"code": 1, "msg": str(exc), "data": {}},
                         ensure_ascii=False), file=sys.stderr)
        return 3
    print(json.dumps({
        "code": 0, "msg": result["message"],
        "data": {"runId": result["runId"], "status": result["status"],
                 "outputFile": result["outputFile"], "runDir": result["runDir"],
                 "statistics": result["statistics"]},
    }, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    sys.exit(main())
