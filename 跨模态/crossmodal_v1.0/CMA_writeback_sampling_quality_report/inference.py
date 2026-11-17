"""算子 24「抽检并回写质量报告」推理/编排入口（表1 第 7 环节，stageId=S7，v4.2 §0.3）。

处理流程
--------
1. 解析 ``configs/config.yaml`` + 业务覆盖，校验唯一 ID、入口清单、UTF-8 编码与路径安全；
2. **第一遍流式扫描**：空行/坏行/非对象 → 坏行台账；缺标识/重复标识 → 原始隔离台账；
   其余记录进入抽样框，仅把 ``sample_id + 分层键`` 留在内存（记录体写入临时文件）；
3. 按「场景 / 模态 / 对齐类型」分层抽样（比例 10%–20%，确定性可复现）；
4. **第二遍流式扫描**：逐条判定人工复核结论并写主输出与旁路台账；
5. 生成已回写质量报告（``status=completed`` 或保持 ``pending_writeback``）与运行清单；
6. 校验统计守恒（主输出 + 各类台账 = 输入）。

响应形态（v4.2）：``/predict`` 的 JSON 形态统一返回 ``outputFile``（crossmodal-files-v1）
与 ``result``；multipart 形态返回处理型 ``status`` 摘要。血缘/阶段归档已停用。
"""
from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path
from utils import delivery_handoff
from typing import Any, Dict, List, Mapping, Optional, Sequence, Tuple

from utils import io as io_utils
# [v4.2] 血缘输出已停用：不再导入 lineage 模块（utils/lineage.py 保留存档）
from utils import logger as logger_utils
from utils import schemas
from utils import spot_check as sc
from utils import strata as strata_mod
from utils import transport

BASE_DIR = Path(__file__).resolve().parent
DEFAULT_CONFIG_PATH = BASE_DIR / "configs" / "config.yaml"
DEFAULT_INPUT_DIR_NAME = "input"
DEFAULT_LOG_NAME = sc.LOG_NAME
FAILURE_NAME = "failure.json"

#: 阶段末打包进内容对象的产物（按此顺序）
STAGE_ARTIFACT_ORDER: Tuple[str, ...] = (
    sc.MAIN_NAME, sc.SAMPLED_NAME, sc.DISPUTES_NAME, sc.SAMPLE_QUARANTINE_NAME,
    sc.PENDING_NAME, sc.ORIGIN_QUARANTINE_NAME, sc.REJECTS_NAME, sc.REPORT_NAME,
    sc.CONCLUSION_NAME,
)

_ROLES = {
    sc.MAIN_NAME: "main",
    sc.SAMPLED_NAME: "spot_check_sample",
    sc.DISPUTES_NAME: "disputes",
    sc.SAMPLE_QUARANTINE_NAME: "sample_quarantine",
    sc.PENDING_NAME: "pending_manual",
    sc.ORIGIN_QUARANTINE_NAME: "origin_quarantine",
    sc.REJECTS_NAME: "rejects",
    sc.REPORT_NAME: "quality_report",
    sc.CONCLUSION_NAME: "sampling_conclusion",
    DEFAULT_LOG_NAME: "log",
}


class InputError(ValueError):
    """输入文件缺失、越界或编码非法。"""


class DependencyError(RuntimeError):
    """上游产物缺失且不允许降级。"""


# --------------------------------------------------------------------------- #
# 输入解析
# --------------------------------------------------------------------------- #

def resolve_input(task_root: Path, input_file: Optional[str], *,
                  allow_external_input: bool = False,
                  input_dir_name: str = DEFAULT_INPUT_DIR_NAME) -> Path:
    """定位入口文件：阶段首读任务 ``input/`` 目录，阶段内读上一中间响应路径。"""
    if input_file in (None, ""):
        directory = io_utils.resolved_absolute(Path(task_root) / input_dir_name)
        if not directory.is_dir():
            raise InputError("任务输入目录不存在：%s" % input_dir_name)
        try:
            manifest = transport.load_manifest(directory)
        except transport.TransportError as exc:
            raise InputError(str(exc)) from exc
        try:
            entry, _relative = transport.resolve_entrypoint(directory, manifest)
        except transport.TransportError as exc:
            raise InputError(str(exc)) from exc
        return entry

    if allow_external_input:
        target = io_utils.resolved_absolute(input_file)
    else:
        try:
            target = io_utils.resolve_in_task(task_root, input_file)
        except ValueError as exc:
            raise InputError(str(exc)) from exc
    if not target.is_file():
        raise InputError("输入文件不存在：%s" % input_file)
    return target


def resolve_manifest_entry(source: Path) -> Tuple[Optional[Dict[str, Any]], str]:
    """读取入口文件所在目录的运行清单，返回 ``(清单或 None, 入口相对名)``。"""
    parent = source.parent
    if not parent.is_dir():
        return None, source.name
    try:
        manifest = transport.load_manifest(parent)
    except transport.TransportError:
        return None, source.name
    if manifest is None:
        return None, source.name
    relative = str(manifest.get("entrypoint") or source.name)
    return manifest, io_utils.normalize_zip_member_name(relative)


# --------------------------------------------------------------------------- #
# 主流程
# --------------------------------------------------------------------------- #

def run(input_file: Optional[str] = None, unique_id: Optional[str] = None,
        config_path=None, output_root=None, overrides: Optional[Mapping[str, Any]] = None,
        allow_external_input: bool = False) -> Dict[str, Any]:
    """执行一次运算，返回本次运行的结果字典。"""
    if unique_id in (None, ""):
        raise InputError("缺少 uniqueId")
    started_at = sc.now_iso()
    cleaned = schemas.validate_overrides(dict(overrides) if overrides else None)
    config = schemas.load_config(config_path or DEFAULT_CONFIG_PATH, cleaned)
    business = config.business_dict()
    root = io_utils.resolve_root(
        output_root if output_root not in (None, "") else os.environ.get("OUTPUT_DIR", "output"))
    value = io_utils.validate_unique_id(unique_id)

    with io_utils.TaskLock(root, value):
        task = io_utils.task_dir(root, value)
        operator_dir = task / sc.OPERATOR_NAME
        operator_dir.mkdir(parents=True, exist_ok=True)
        run_id = io_utils.next_run_id(operator_dir)
        run_dir = operator_dir / run_id
        run_dir.mkdir(parents=True, exist_ok=False)
        log = logger_utils.get_logger("%s.%s.%s" % (sc.OPERATOR_NAME, value, run_id),
                                      run_dir / DEFAULT_LOG_NAME)
        try:
            return _execute(task_root=task, run_dir=run_dir, value=value, run_id=run_id,
                            input_file=input_file, business=business, config=config,
                            overrides=cleaned, started_at=started_at, log=log,
                            allow_external_input=allow_external_input)
        except Exception as exc:  # noqa: BLE001 - 需要落盘失败记录后再抛
            logger_utils.log_action(log, "fail", "算子执行失败：%s" % type(exc).__name__)
            _write_failure(run_dir, value, run_id, exc, started_at)
            raise
        finally:
            logger_utils.close_logger(log)


def _write_failure(run_dir: Path, unique_id: str, run_id: str, exc: Exception,
                   started_at: str) -> None:
    payload = {
        "operator": sc.OPERATOR_NAME,
        "operator_number": sc.FORMAL_OPERATOR_NUMBER,
        "operator_version": sc.OPERATOR_VERSION,
        "stage_id": sc.STAGE_ID,
        "uniqueId": unique_id,
        "runId": run_id,
        "errorType": type(exc).__name__,
        "message": str(exc)[:500],
        "startedAt": started_at,
        "failedAt": sc.now_iso(),
    }
    try:
        io_utils.atomic_write_json(Path(run_dir) / FAILURE_NAME, payload)
    except OSError:  # pragma: no cover - 磁盘不可写时不掩盖原始异常
        pass


def _execute(*, task_root: Path, run_dir: Path, value: str, run_id: str,
             input_file: Optional[str], business: Mapping[str, Any],
             config: schemas.OperatorConfig, overrides: Mapping[str, Any],
             started_at: str, log, allow_external_input: bool) -> Dict[str, Any]:
    source = resolve_input(task_root, input_file,
                           allow_external_input=allow_external_input)
    logger_utils.log_action(log, "input", "输入文件 %s" % source.name)
    try:
        io_utils.assert_utf8(source)
    except UnicodeDecodeError as exc:
        raise InputError("输入文件不是合法 UTF-8（%s，位置 %s）"
                         % (exc.reason, exc.start)) from exc

    _manifest, entrypoint = resolve_manifest_entry(source)
    # [v4.2] 血缘输出已停用：不再读取历史血缘（utils/lineage.py 保留存档）
    # prior_lineage = lineage_utils.load_prior_lineage([source.parent, task_root / "input"])

    report_path, report_name = sc.find_source_report(source.parent, business)
    if report_path is None:
        report_path = delivery_handoff.source_quality_report(source.parent, run_dir)
        if report_path is not None:
            report_name = "delivery_handoff/materials/quality_report.json"
    source_report = sc.load_source_report(report_path)
    source_available = bool(source_report is not None)
    if not source_available and not business.get("allow_degraded", False):
        raise DependencyError("缺少上游「待回写质量报告」，且 allow_degraded=false")
    if source_available:
        logger_utils.log_action(log, "report", "读取上游待回写报告 %s" % report_name)
    else:
        logger_utils.log_action(log, "report", "未找到上游待回写报告，本次显式降级")

    stats = sc.empty_statistics()
    frame_rows: List[Dict[str, Any]] = []
    seen_ids: set = set()
    frame_path = run_dir / "_frame.partial"
    reject_path = run_dir / sc.REJECTS_NAME
    origin_path = run_dir / sc.ORIGIN_QUARANTINE_NAME

    with io_utils.JsonlWriter(reject_path) as reject_writer, \
            io_utils.JsonlWriter(origin_path) as origin_writer, \
            io_utils.JsonlWriter(frame_path) as frame_writer:
        lineno = 0
        for line in io_utils.iter_decoded_lines(source):
            lineno += 1
            stats["input_lines"] += 1
            if not line.strip():
                stats["blank_lines"] += 1
                continue
            stats["input"] += 1
            try:
                record = json.loads(line)
            except json.JSONDecodeError:
                stats["rejected"] += 1
                reject_writer.write(sc.build_reject_entry(
                    lineno, "json_parse_error", line, business, value, run_id))
                continue
            if not isinstance(record, dict):
                stats["rejected"] += 1
                reject_writer.write(sc.build_reject_entry(
                    lineno, "record_not_object", line, business, value, run_id))
                continue
            stats["valid"] += 1
            raw_id = record.get("sample_id")
            sample_id = raw_id.strip() if isinstance(raw_id, str) else ""
            if not sample_id:
                stats["origin_quarantined"] += 1
                origin_writer.write(sc.build_origin_ledger(
                    record, sample_id=None, reason="missing_sample_id",
                    label=sc.LABEL_ORIGIN_MISSING_FIELD, lineno=lineno,
                    business=business, unique_id=value, run_id=run_id))
                continue
            if sample_id in seen_ids:
                stats["origin_quarantined"] += 1
                stats["duplicates"] += 1
                origin_writer.write(sc.build_origin_ledger(
                    record, sample_id=sample_id, reason="duplicate_sample_id",
                    label=sc.LABEL_ORIGIN_DUPLICATE, lineno=lineno,
                    business=business, unique_id=value, run_id=run_id))
                continue
            seen_ids.add(sample_id)
            key, _applicable = strata_mod.stratum_key(record, config.strata)
            # 上游已隔离的记录保留在主输出但不进入抽样框（表1 不重复定责）
            eligible = not sc.upstream_isolated(record)
            if eligible:
                frame_rows.append({"sample_id": sample_id, "stratum": key})
            frame_writer.write({"lineno": lineno, "sample_id": sample_id,
                                "stratum": list(key), "eligible": eligible,
                                "record": record})

    stats["empty_input"] = stats["input"] == 0
    logger_utils.log_action(
        log, "scan",
        "第一遍扫描完成：行数=%d 空行=%d 坏行=%d 有效记录=%d 抽样框=%d 原始隔离=%d（重复=%d）"
        % (stats["input_lines"], stats["blank_lines"], stats["rejected"],
           stats["valid"], len(frame_rows), stats["origin_quarantined"],
           stats["duplicates"]))

    selection = strata_mod.select_samples(
        frame_rows, rate=float(business["sample_rate"]),
        salt=str(business["sampling_salt"]),
        low=schemas.SAMPLE_RATE_MIN, high=schemas.SAMPLE_RATE_MAX)
    selected_ids = set(selection["selected"])
    stats["sampled"] = len(selected_ids)
    stats["achieved_sample_rate"] = selection["achieved_rate"]
    stats["sample_target"] = selection["target_total"]
    stats["strata"] = {
        label: {
            "scene": item["scene"], "modality": item["modality"],
            "alignment_type": item["alignment_type"], "size": item["size"],
            "target": item["target"], "sampled": item["sampled"],
            "passed": 0, "rejected": 0, "uncertain": 0, "pending": 0,
            "not_sampled": item["size"] - item["sampled"],
        }
        for label, item in selection["strata"].items()
    }
    logger_utils.log_action(
        log, "sample",
        "分层抽样完成：层数=%d 抽样框=%d 目标=%d 抽中=%d 实际抽样率=%.4f salt=%s"
        % (len(selection["strata"]), selection["total"], selection["target_total"],
           len(selected_ids), selection["achieved_rate"], business["sampling_salt"]))

    cancelled_ids: List[str] = []
    strata_detail = stats["strata"]
    with io_utils.JsonlWriter(run_dir / sc.MAIN_NAME) as main_writer, \
            io_utils.JsonlWriter(run_dir / sc.SAMPLED_NAME) as sampled_writer, \
            io_utils.JsonlWriter(run_dir / sc.SAMPLE_QUARANTINE_NAME) as quarantine_writer, \
            io_utils.JsonlWriter(run_dir / sc.DISPUTES_NAME) as dispute_writer, \
            io_utils.JsonlWriter(run_dir / sc.PENDING_NAME) as pending_writer, \
            open(frame_path, "r", encoding="utf-8") as frame_stream:
        for line in frame_stream:
            if not line.strip():
                continue
            row = json.loads(line)
            sample_id = row["sample_id"]
            if (row["record"].get("test_context") or {}).get("simulated_human") is True:
                stats["simulated_human"] = True
            sampled = bool(row.get("eligible", True)) and sample_id in selected_ids
            outcome = sc.evaluate_record(row["record"], sample_id=sample_id,
                                         sampled=sampled, stratum=row["stratum"],
                                         business=business, lineno=row["lineno"])
            if outcome.in_main_output:
                main_writer.write(outcome.record)
                stats["output"] += 1
            if sampled:
                sampled_writer.write(sc.build_sampled_entry(outcome, business, value, run_id))
            bucket = strata_detail.get(outcome.stratum_label)
            if bucket is not None:
                if sampled and outcome.decision == sc.DECISION_PASSED:
                    bucket["passed"] += 1
                elif sampled and outcome.decision == sc.DECISION_REJECTED:
                    bucket["rejected"] += 1
                elif sampled and outcome.decision == sc.DECISION_UNCERTAIN:
                    bucket["uncertain"] += 1
                elif sampled and outcome.decision == sc.DECISION_PENDING:
                    bucket["pending"] += 1
            if outcome.decision == sc.DECISION_PASSED:
                stats["sampled_passed"] += 1
                stats["sampled_in_output"] += 1
            elif outcome.decision == sc.DECISION_REJECTED:
                stats["sampled_rejected"] += 1
                stats["sample_quarantined"] += 1
                stats["disputes"] += 1
                quarantine_writer.write(sc.build_ledger(
                    outcome, business, value, run_id, role="sample_quarantine"))
                dispute_writer.write(sc.build_ledger(
                    outcome, business, value, run_id, role="dispute"))
                if outcome.cancelled_auto_alignment:
                    stats["auto_alignment_cancelled"] += 1
                    cancelled_ids.append(sample_id)
            elif outcome.decision == sc.DECISION_UNCERTAIN:
                stats["sampled_uncertain"] += 1
                stats["sampled_in_output"] += 1
                stats["disputes"] += 1
                dispute_writer.write(sc.build_ledger(
                    outcome, business, value, run_id, role="dispute"))
            elif outcome.decision == sc.DECISION_PENDING:
                stats["sampled_pending"] += 1
                stats["sampled_in_output"] += 1
                stats["pending_manual"] += 1
                pending_writer.write(sc.build_ledger(
                    outcome, business, value, run_id, role="pending_manual"))
            elif outcome.decision == sc.DECISION_NOT_SAMPLED:
                stats["not_sampled"] += 1
            else:  # DECISION_SKIPPED：不进入抽样框，主输出保留
                stats["not_applicable_records"] += 1
            if outcome.review.get("simulated_human") is True:
                stats["simulated_human"] = True
            if outcome.human_verified:
                stats["human_verified_records"] += 1
            if outcome.review_required:
                stats["review_required_records"] += 1
    io_utils.cleanup_files(frame_path)

    logger_utils.log_action(
        log, "judge",
        "人工复核判定：通过=%d 判错=%d 存疑=%d 待人工=%d 取消自动对齐=%d"
        % (stats["sampled_passed"], stats["sampled_rejected"], stats["sampled_uncertain"],
           stats["sampled_pending"], stats["auto_alignment_cancelled"]))

    writeback_complete = (stats["sampled_pending"] == 0
                         and stats["sampled_uncertain"] == 0)
    status = _decide_status(stats, source_available)
    generated_at = sc.now_iso()
    sampling_block = {
        "sample_rate": business["sample_rate"],
        "salt": business["sampling_salt"],
        "strata": list(config.strata),
        "target_total": selection["target_total"],
        "achieved_rate": selection["achieved_rate"],
        "strata_detail": strata_detail,
    }
    report = sc.build_writeback_report(
        source_report, stats, business, unique_id=value, run_id=run_id,
        sampling=sampling_block,
        conclusions=sc.conclusion_counts(stats), cancelled_ids=cancelled_ids,
        disputes=stats["disputes"], pending_manual=stats["pending_manual"],
        writeback_complete=writeback_complete, generated_at=generated_at,
        source_name=report_name, source_available=source_available, status=status)
    io_utils.atomic_write_json(run_dir / sc.REPORT_NAME, report)

    # 表1 #24「输出：抽检结论和报告状态」——抽检结论独立成材，#23 封版门禁需要两份齐备
    conclusion = sc.build_sampling_conclusion(
        stats, business, unique_id=value, run_id=run_id,
        sampling=sampling_block, conclusions=sc.conclusion_counts(stats),
        cancelled_ids=cancelled_ids, strata_detail=strata_detail,
        writeback_complete=writeback_complete, generated_at=generated_at,
        source_name=report_name, source_available=source_available, status=status)
    io_utils.atomic_write_json(run_dir / sc.CONCLUSION_NAME, conclusion)

    stats["writeback"] = {
        "completed": writeback_complete,
        "status": report["status"],
        "version_state": report["writeback"]["version_state"],
        "source_report_available": source_available,
        "source_report_name": report_name,
        "cancelled_auto_alignment": len(cancelled_ids),
    }
    failures = sc.conservation_failures(stats)
    stats["conservation_failures"] = failures
    stats["conservation_ok"] = not failures
    logger_utils.log_action(
        log, "writeback",
        "质量报告回写：status=%s version_state=%s 待回写结论=%d"
        % (report["status"], report["writeback"]["version_state"],
           stats["sampled_pending"] + stats["sampled_uncertain"]))
    logger_utils.log_action(
        log, "output",
        "产出：主输出=%d 抽检样本=%d 判错隔离=%d 异议=%d 待人工=%d 原始隔离=%d 坏行=%d"
        % (stats["output"], stats["sampled"], stats["sample_quarantined"],
           stats["disputes"], stats["pending_manual"], stats["origin_quarantined"],
           stats["rejected"]))
    logger_utils.log_action(
        log, "conservation",
        "统计守恒校验=%s%s" % ("通过" if stats["conservation_ok"] else "失败",
                               "" if stats["conservation_ok"]
                               else "：" + "; ".join(failures)))

    artifacts = _build_artifacts(run_dir)
    manifest = transport.build_manifest(sc.MAIN_NAME, [
        {"path": sc.MAIN_NAME, "role": "main"},
        {"path": sc.SAMPLED_NAME, "role": "spot_check_sample"},
        {"path": sc.DISPUTES_NAME, "role": "disputes"},
        {"path": sc.SAMPLE_QUARANTINE_NAME, "role": "sample_quarantine"},
        {"path": sc.PENDING_NAME, "role": "pending_manual"},
        {"path": sc.ORIGIN_QUARANTINE_NAME, "role": "origin_quarantine"},
        {"path": sc.REJECTS_NAME, "role": "rejects"},
        {"path": sc.REPORT_NAME, "role": "quality_report"},
        {"path": sc.CONCLUSION_NAME, "role": "sampling_conclusion"},
        {"path": DEFAULT_LOG_NAME, "role": "log"},
    ])
    delivery_handoff.propagate(source.parent, run_dir, manifest, 24)
    transport.write_manifest(run_dir, manifest)

    finished_at = sc.now_iso()
    config_snapshot = schemas.config_snapshot(config, overrides.keys())
    input_reference = str(source) if allow_external_input and input_file not in (None, "") \
        else entrypoint
    # [v4.2] 血缘输出已停用：血缘节点构造、lineage.json 与 stage_context.json 写入
    # 全部停用（utils/lineage.py 保留存档，不再调用）。
    # node = lineage_utils.make_lineage(
    #     operator=sc.OPERATOR_NAME, version=sc.OPERATOR_VERSION, unique_id=value,
    #     stage_id=sc.STAGE_ID, run_id=run_id, input_file=input_reference,
    #     output_file=str(run_dir / sc.MAIN_NAME), status=status, started_at=started_at,
    #     finished_at=finished_at, config=config_snapshot, statistics=stats,
    #     artifacts=artifacts, formal_operator_number=sc.FORMAL_OPERATOR_NUMBER,
    #     message="抽检并回写质量报告完成")
    # history = lineage_utils.load_lineage(run_dir / lineage_utils.LINEAGE_NAME)
    # history.append(node)
    # io_utils.atomic_write_json(run_dir / lineage_utils.LINEAGE_NAME, history)
    # stage_records = lineage_utils.to_stage_lineage(history)
    # context = lineage_utils.build_stage_context(
    #     sc.STAGE_ID, value, run_id, entrypoint, stage_records, prior_lineage, business)
    # io_utils.atomic_write_json(run_dir / lineage_utils.STAGE_CONTEXT_NAME, context)

    logger_utils.log_action(log, "done", "运行完成：runId=%s status=%s" % (run_id, status))
    return {
        "operator": sc.OPERATOR_NAME,
        "operatorLabel": sc.OPERATOR_LABEL,
        "operatorNumber": sc.FORMAL_OPERATOR_NUMBER,
        "operatorVersion": sc.OPERATOR_VERSION,
        "stageId": sc.STAGE_ID,
        "uniqueId": value,
        "runId": run_id,
        "status": status,
        "reportStatus": report["status"],
        "runDir": str(run_dir),
        "outputFile": str(run_dir / sc.MAIN_NAME),
        "entrypoint": sc.MAIN_NAME,
        "inputFile": input_reference,
        "statistics": stats,
        "artifacts": artifacts,
        "manifest": manifest,
        # [v4.2] 血缘输出已停用：run() 返回不再包含 lineage/stageContext 键。
        "startedAt": started_at,
        "finishedAt": finished_at,
        "business": business,
    }


def _decide_status(statistics: Mapping[str, Any], source_available: bool) -> str:
    if statistics.get("input", 0) == 0:
        return "success"
    if not source_available:
        return "degraded"
    if statistics.get("sampled_pending", 0) or statistics.get("sampled_uncertain", 0):
        return "waiting_manual"
    if (statistics.get("rejected", 0) or statistics.get("origin_quarantined", 0)
            or statistics.get("sample_quarantined", 0)
            or statistics.get("not_applicable_records", 0)):
        return "partial"
    return "success"


def _build_artifacts(run_dir: Path) -> List[Dict[str, Any]]:
    artifacts: List[Dict[str, Any]] = []
    for name in list(STAGE_ARTIFACT_ORDER) + [DEFAULT_LOG_NAME, transport.MANIFEST_NAME]:
        path = run_dir / name
        if not path.is_file():
            continue
        entry: Dict[str, Any] = {"path": name, "role": _ROLES.get(name, "artifact"),
                                 "bytes": path.stat().st_size}
        if name.endswith(".jsonl"):
            entry["records"] = io_utils.count_lines(path)
        artifacts.append(entry)
    return artifacts


# --------------------------------------------------------------------------- #
# 阶段返回形态
# --------------------------------------------------------------------------- #
#
# [v4.2] 血缘输出已停用：lineageFile 返回形态不再使用（响应统一见 app.py §3 改造）。
#
# def build_intermediate_lineage(result: Mapping[str, Any]) -> Dict[str, Any]:
#     """``isLastOperator=false``：只返回当前算子血缘，含当前临时 ``outputFile``。"""
#     node = dict(result["lineage"])
#     node["outputFile"] = result["outputFile"]
#     return node
#
#
# def build_stage_payload(result: Mapping[str, Any]) -> List[Dict[str, Any]]:
#     """``isLastOperator=true``：返回完整阶段血缘（已剔除临时 ``outputFile``）。"""
#     node = dict(result["lineage"])
#     node.pop("outputFile", None)
#     return lineage_utils.to_stage_lineage([node])


def stage_file_object(result: Mapping[str, Any],
                      max_response_bytes: Optional[int] = None) -> Dict[str, Any]:
    """阶段末 ``outputFile``：当前运行目录的 ``crossmodal-files-v1`` 内容对象。"""
    run_dir = Path(result["runDir"])
    candidates = [name for name in STAGE_ARTIFACT_ORDER if (run_dir / name).is_file()]
    candidates.append(transport.MANIFEST_NAME)
    candidates.extend(delivery_handoff.declared_paths(result.get("manifest", {})))
    limit = max_response_bytes
    if limit is None:
        limit = _int_env("MAX_RESPONSE_BYTES", 67108864)
    return transport.pack_file_object(run_dir, result["entrypoint"], candidates,
                                      max_response_bytes=limit)


def _int_env(name: str, default: int) -> int:
    raw = os.environ.get(name)
    if not raw:
        return default
    try:
        return int(raw)
    except ValueError:
        return default


# --------------------------------------------------------------------------- #
# CLI
# --------------------------------------------------------------------------- #

def _parse_overrides(values: Optional[Sequence[str]]) -> Dict[str, Any]:
    """``--override key=value`` 或 ``--override '{"key": value}'``。"""
    result: Dict[str, Any] = {}
    for item in values or []:
        text = str(item).strip()
        if not text:
            continue
        if text.startswith("{"):
            try:
                payload = json.loads(text)
            except json.JSONDecodeError as exc:
                raise schemas.ConfigError("--override JSON 无法解析：%s" % exc.msg) from exc
            if not isinstance(payload, dict):
                raise schemas.ConfigError("--override JSON 必须是对象")
            result.update(payload)
            continue
        if "=" not in text:
            raise schemas.ConfigError("--override 需要 key=value 形式：%s" % text)
        key, _, raw = text.partition("=")
        key = key.strip()
        raw = raw.strip()
        if not key:
            raise schemas.ConfigError("--override 缺少键名：%s" % text)
        try:
            value: Any = json.loads(raw)
        except json.JSONDecodeError:
            value = raw
        result[key] = value
    return result


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="CMA_writeback_sampling_quality_report",
        description="按场景/模态/对齐类型分层抽检、写回人工复核结论并回写质量报告")
    parser.add_argument("--input", default=None,
                        help="输入 JSONL；缺省时读取 OUTPUT_DIR/<uniqueId>/input/ 下的唯一 JSONL")
    parser.add_argument("--unique-id", required=True, help="任务唯一标识")
    parser.add_argument("--output-root", default=None,
                        help="输出根目录，缺省读环境变量 OUTPUT_DIR，再缺省为 output")
    parser.add_argument("--config", default=None, help="配置文件路径")
    parser.add_argument("--override", action="append", default=None,
                        help="业务覆盖，支持 key=value 或 JSON 对象，可重复")
    parser.add_argument("--is-last-operator", action="store_true",
                        help="阶段末形态：返回 outputFile 文件对象与完整阶段血缘")
    parser.add_argument("--strict-input", action="store_true",
                        help="禁止读取任务目录之外的文件（生产口径）")
    return parser


def main(argv: Optional[Sequence[str]] = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        overrides = _parse_overrides(args.override)
        result = run(input_file=args.input, unique_id=args.unique_id,
                     config_path=args.config, output_root=args.output_root,
                     overrides=overrides,
                     allow_external_input=not args.strict_input)
    except (schemas.ConfigError, InputError, io_utils.TaskBusyError, ValueError) as exc:
        sys.stderr.write(json.dumps({"code": 1, "msg": str(exc), "data": {}},
                                    ensure_ascii=False) + "\n")
        return 2
    except Exception as exc:  # noqa: BLE001 - CLI 也要给结构化输出
        sys.stderr.write(json.dumps(
            {"code": 1, "msg": "%s: %s" % (type(exc).__name__, str(exc)[:400]),
             "data": {}}, ensure_ascii=False) + "\n")
        return 3

    # [v4.2] 血缘输出已停用：CLI 不再回传 lineageFile（两种形态统一）
    data: Dict[str, Any] = {
        "runId": result["runId"],
        "status": result["status"],
        "reportStatus": result["reportStatus"],
        "outputFile": stage_file_object(result) if args.is_last_operator
        else result["outputFile"],
        "statistics": result["statistics"],
    }
    sys.stdout.write(json.dumps({"code": 0, "msg": "抽检并回写质量报告完成", "data": data},
                                ensure_ascii=False) + "\n")
    return 0


if __name__ == "__main__":
    sys.exit(main())
