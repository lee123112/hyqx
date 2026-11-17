"""算子 25「管理人工复核任务」推理/编排入口（表1 第 7 环节，stageId=S7）。

处理流程
--------
1. 解析 ``configs/config.yaml`` + 业务覆盖，校验唯一 ID、入口清单、UTF-8 编码与路径安全；
2. **流式扫描**：空行/坏行/非对象 → 坏行台账；缺标识/重复标识 → 原始隔离台账；
   其余记录逐条判定「存疑 / 抽检异议 / 加工退回」三类归属与历史复核轮数；
3. 仅在**手动触发开启**（``enabled=true``）时生成派单任务；超过 2 次复核的直接锁定、
   自动对齐且未被 #24 抽中的不进队列、非三类样本不生成任务；
4. 人工结论只来自输入证据：通过 → 回候选池；不通过 → 样本隔离库；无结论/存疑 →
   保留待人工（记录级 ``status=1`` + ``process_status=waiting_manual``）；
5. 生成管理报告、运行清单与规范日志；
6. 校验统计守恒（主输出 + 各类台账 = 输入，flagged 单列不重复加总）。

[v4.2] 双协议：阶段内/阶段末响应统一返回 ``outputFile``（crossmodal-files-v1）+ ``result``；
血缘/阶段上下文（``lineage.json`` / ``stage_context.json``）输出已停用，代码保留注释存档。
"""
from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path
from utils import delivery_handoff
from typing import Any, Dict, List, Mapping, Optional, Sequence, Tuple

from utils import dispatch as dp
from utils import io as io_utils
# [v4.2] 血缘输出已停用：lineage 仅保留 now_iso（时间工具）等通用函数；
# build_lineage/append_lineage/load_lineage/write_lineage 等血缘调用已全部注释存档。
from utils import lineage as lineage_utils
from utils import logger as logger_utils
from utils import schemas
from utils import transport

BASE_DIR = Path(__file__).resolve().parent
DEFAULT_CONFIG_PATH = BASE_DIR / "configs" / "config.yaml"
DEFAULT_INPUT_DIR_NAME = "input"
DEFAULT_LOG_NAME = dp.LOG_NAME
FAILURE_NAME = "failure.json"

#: 阶段末打包进内容对象的产物（按此顺序）
STAGE_ARTIFACT_ORDER: Tuple[str, ...] = (
    dp.MAIN_NAME, dp.TASKS_NAME, dp.CONCLUSIONS_NAME, dp.RETURN_NAME,
    dp.SAMPLE_QUARANTINE_NAME, dp.PENDING_NAME, dp.NOT_DISPATCHED_NAME,
    dp.ORIGIN_QUARANTINE_NAME, dp.REJECTS_NAME, dp.REPORT_NAME,
)

_ROLES = {
    dp.MAIN_NAME: "main",
    dp.TASKS_NAME: "review_tasks",
    dp.CONCLUSIONS_NAME: "review_conclusions",
    dp.RETURN_NAME: "sample_pool_return",
    dp.SAMPLE_QUARANTINE_NAME: "sample_quarantine",
    dp.PENDING_NAME: "pending_manual",
    dp.NOT_DISPATCHED_NAME: "not_dispatched",
    dp.ORIGIN_QUARANTINE_NAME: "origin_quarantine",
    dp.REJECTS_NAME: "rejects",
    dp.REPORT_NAME: "review_report",
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
    started_at = lineage_utils.now_iso()
    cleaned = schemas.validate_overrides(dict(overrides) if overrides else None)
    config = schemas.load_config(config_path or DEFAULT_CONFIG_PATH, cleaned)
    business = config.business_dict()
    root = io_utils.resolve_root(
        output_root if output_root not in (None, "") else os.environ.get("OUTPUT_DIR", "output"))
    value = io_utils.validate_unique_id(unique_id)

    with io_utils.TaskLock(root, value):
        task = io_utils.task_dir(root, value)
        operator_dir = task / dp.RUN_STAGE
        operator_dir.mkdir(parents=True, exist_ok=True)
        run_id = io_utils.next_run_id(operator_dir)
        run_dir = operator_dir / run_id
        run_dir.mkdir(parents=True, exist_ok=False)
        log = logger_utils.get_logger("%s.%s.%s" % (dp.RUN_STAGE, value, run_id),
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
        "operator": dp.OPERATOR_NAME,
        "operator_number": dp.FORMAL_OPERATOR_NUMBER,
        "operator_version": dp.OPERATOR_VERSION,
        "stage_id": dp.STAGE_ID,
        "uniqueId": unique_id,
        "runId": run_id,
        "errorType": type(exc).__name__,
        "message": str(exc)[:500],
        "startedAt": started_at,
        "failedAt": lineage_utils.now_iso(),
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
    # [v4.2] 血缘输出已停用：不再读取上游阶段血缘（load_prior_lineage 调用已注释）
    # prior_lineage = lineage_utils.load_prior_lineage([source.parent, task_root / "input"])
    logger_utils.log_action(
        log, "config",
        "触发方式=%s 已开启=%s 复核上限=%d 平台接入=%s"
        % (business.get("trigger"), bool(business.get("enabled")),
           int(business.get("max_review_count")), bool(
               business.get("manual_platform_available"))))

    stats = dp.empty_statistics()
    dispatched_ids: List[str] = []

    with io_utils.JsonlWriter(run_dir / dp.MAIN_NAME) as main_writer, \
            io_utils.JsonlWriter(run_dir / dp.TASKS_NAME) as task_writer, \
            io_utils.JsonlWriter(run_dir / dp.CONCLUSIONS_NAME) as conclusion_writer, \
            io_utils.JsonlWriter(run_dir / dp.RETURN_NAME) as return_writer, \
            io_utils.JsonlWriter(run_dir / dp.SAMPLE_QUARANTINE_NAME) as quarantine_writer, \
            io_utils.JsonlWriter(run_dir / dp.PENDING_NAME) as pending_writer, \
            io_utils.JsonlWriter(run_dir / dp.NOT_DISPATCHED_NAME) as not_dispatched_writer, \
            io_utils.JsonlWriter(run_dir / dp.ORIGIN_QUARANTINE_NAME) as origin_writer, \
            io_utils.JsonlWriter(run_dir / dp.REJECTS_NAME) as reject_writer:
        seen_ids: set = set()
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
                reject_writer.write(dp.build_reject_entry(
                    lineno, "json_parse_error", line, business, value, run_id))
                continue
            if not isinstance(record, dict):
                stats["rejected"] += 1
                reject_writer.write(dp.build_reject_entry(
                    lineno, "record_not_object", line, business, value, run_id))
                continue
            stats["valid"] += 1
            raw_id = record.get("sample_id")
            sample_id = raw_id.strip() if isinstance(raw_id, str) else ""
            if not sample_id:
                stats["origin_quarantined"] += 1
                origin_writer.write(dp.build_origin_ledger(
                    record, sample_id=None, reason="missing_sample_id",
                    label=dp.LABEL_ORIGIN_MISSING_FIELD, lineno=lineno,
                    business=business, unique_id=value, run_id=run_id))
                continue
            if sample_id in seen_ids:
                stats["origin_quarantined"] += 1
                stats["duplicates"] += 1
                origin_writer.write(dp.build_origin_ledger(
                    record, sample_id=sample_id, reason="duplicate_sample_id",
                    label=dp.LABEL_ORIGIN_DUPLICATE, lineno=lineno,
                    business=business, unique_id=value, run_id=run_id))
                continue
            seen_ids.add(sample_id)

            outcome = dp.evaluate_record(record, sample_id=sample_id,
                                         business=business, lineno=lineno)
            if outcome.dispatched:
                outcome.task = dp.build_task_entry(outcome, business, value, run_id)
                task_writer.write(outcome.task)
                conclusion_writer.write(dp.build_conclusion_entry(
                    outcome, business, value, run_id))
                dispatched_ids.append(sample_id)
                if outcome.decision == dp.DECISION_PASSED:
                    return_writer.write(dp.build_ledger(
                        outcome, business, value, run_id, role="sample_pool_return"))
                elif outcome.decision == dp.DECISION_REJECTED:
                    quarantine_writer.write(dp.build_ledger(
                        outcome, business, value, run_id, role="sample_quarantine"))
                else:
                    pending_writer.write(dp.build_ledger(
                        outcome, business, value, run_id, role="pending_manual"))
            else:
                not_dispatched_writer.write(dp.build_ledger(
                    outcome, business, value, run_id, role="not_dispatched"))
                if outcome.decision == dp.DECISION_MAX_REVIEW:
                    quarantine_writer.write(dp.build_ledger(
                        outcome, business, value, run_id, role="sample_quarantine"))
            if outcome.in_main_output:
                main_writer.write(outcome.record)
            dp.accumulate(stats, outcome)

    stats["empty_input"] = stats["input"] == 0
    stats["trigger"] = business.get("trigger")
    stats["enabled"] = bool(business.get("enabled"))
    stats["platform_available"] = bool(business.get("manual_platform_available"))
    stats["dispatched_sample_digest"] = dp.digest_sample_ids(sorted(dispatched_ids))

    failures = dp.conservation_failures(stats)
    stats["conservation_failures"] = failures
    stats["conservation_ok"] = not failures
    status = dp.derive_status(stats)
    stats["report_status"] = status

    logger_utils.log_action(
        log, "scan",
        "扫描完成：行数=%d 空行=%d 坏行=%d 有效=%d 原始隔离=%d（重复=%d）"
        % (stats["input_lines"], stats["blank_lines"], stats["rejected"],
           stats["valid"], stats["origin_quarantined"], stats["duplicates"]))
    logger_utils.log_action(
        log, "dispatch",
        "派单：任务=%d 已派单=%d 待平台=%d 未派单=%d（非三类=%d 未触发=%d "
        "自动对齐未抽中=%d 复核超限=%d 上游已隔离=%d）"
        % (stats["tasks_generated"], stats["dispatched"], stats["waiting_dispatch"],
           stats["not_dispatched"], stats["not_eligible"], stats["not_triggered"],
           stats["auto_aligned_not_sampled"], stats["max_review_reached"],
           stats["not_applicable_records"]))
    logger_utils.log_action(
        log, "conclusion",
        "结论：通过=%d 不通过=%d 存疑=%d 待人工=%d 回候选池=%d 样本隔离=%d"
        % (stats["concluded_passed"], stats["concluded_rejected"],
           stats["concluded_uncertain"], stats["concluded_pending"],
           stats["returned_to_pool"], stats["sample_quarantined"]))
    logger_utils.log_action(
        log, "output",
        "产出：主输出=%d 任务=%d 结论=%d 隔离=%d 待人工=%d 坏行=%d"
        % (stats["output"], stats["tasks_generated"], stats["conclusions_written"],
           stats["sample_quarantined"], stats["concluded_pending"] + stats[
               "concluded_uncertain"], stats["rejected"]))
    logger_utils.log_action(
        log, "conservation",
        "统计守恒校验=%s%s" % ("通过" if stats["conservation_ok"] else "失败",
                               "" if stats["conservation_ok"]
                               else "：" + "; ".join(failures)))

    generated_at = lineage_utils.now_iso()
    report = dp.build_management_report(
        stats, business, unique_id=value, run_id=run_id, generated_at=generated_at,
        status=status, source_name=source.name, source_available=True)
    io_utils.atomic_write_json(run_dir / dp.REPORT_NAME, report)

    artifacts = _build_artifacts(run_dir)
    manifest = transport.build_manifest(dp.MAIN_NAME, [
        {"path": dp.MAIN_NAME, "role": "main"},
        {"path": dp.TASKS_NAME, "role": "review_tasks"},
        {"path": dp.CONCLUSIONS_NAME, "role": "review_conclusions"},
        {"path": dp.RETURN_NAME, "role": "sample_pool_return"},
        {"path": dp.SAMPLE_QUARANTINE_NAME, "role": "sample_quarantine"},
        {"path": dp.PENDING_NAME, "role": "pending_manual"},
        {"path": dp.NOT_DISPATCHED_NAME, "role": "not_dispatched"},
        {"path": dp.ORIGIN_QUARANTINE_NAME, "role": "origin_quarantine"},
        {"path": dp.REJECTS_NAME, "role": "rejects"},
        {"path": dp.REPORT_NAME, "role": "review_report"},
        {"path": DEFAULT_LOG_NAME, "role": "log"},
    ])
    delivery_handoff.propagate(source.parent, run_dir, manifest, 25)
    transport.write_manifest(run_dir, manifest)

    finished_at = lineage_utils.now_iso()
    # [v4.2] 血缘/阶段归档输出已停用：不再构建血缘节点、不再写 lineage.json /
    # stage_context.json（build_lineage/append_lineage 调用整块注释存档）。
    # config_snapshot = schemas.config_snapshot(config, overrides.keys())
    input_reference = str(source) if allow_external_input and input_file not in (None, "") \
        else entrypoint
    # node = lineage_utils.make_lineage(
    #     operator=dp.OPERATOR_NAME, version=dp.OPERATOR_VERSION, unique_id=value,
    #     stage_id=dp.STAGE_ID, run_id=run_id, input_file=input_reference,
    #     output_file=str(run_dir / dp.MAIN_NAME), status=status, started_at=started_at,
    #     finished_at=finished_at, config=config_snapshot, statistics=stats,
    #     artifacts=artifacts, formal_operator_number=dp.FORMAL_OPERATOR_NUMBER,
    #     message="管理人工复核任务完成")
    # history = lineage_utils.load_lineage(run_dir / lineage_utils.LINEAGE_NAME)
    # history.append(node)
    # io_utils.atomic_write_json(run_dir / lineage_utils.LINEAGE_NAME, history)
    # stage_records = lineage_utils.to_stage_lineage(history)
    # context = lineage_utils.build_stage_context(
    #     dp.STAGE_ID, value, run_id, entrypoint, stage_records, prior_lineage, business)
    # io_utils.atomic_write_json(run_dir / lineage_utils.STAGE_CONTEXT_NAME, context)

    logger_utils.log_action(log, "done", "运行完成：runId=%s status=%s" % (run_id, status))
    return {
        "operator": dp.OPERATOR_NAME,
        "operatorLabel": dp.OPERATOR_LABEL,
        "operatorNumber": dp.FORMAL_OPERATOR_NUMBER,
        "operatorVersion": dp.OPERATOR_VERSION,
        "stageId": dp.STAGE_ID,
        "uniqueId": value,
        "runId": run_id,
        "status": status,
        "reviewStatus": report["status"],
        "runDir": str(run_dir),
        "outputFile": str(run_dir / dp.MAIN_NAME),
        "entrypoint": dp.MAIN_NAME,
        "inputFile": input_reference,
        "statistics": stats,
        "artifacts": artifacts,
        "manifest": manifest,
        # [v4.2] 血缘输出已停用：不再回传 lineage / stageContext 键
        # "lineage": node,
        # "stageContext": context,
        "report": report,
        "startedAt": started_at,
        "finishedAt": finished_at,
        "business": business,
    }


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

# [v4.2] 血缘输出已停用：build_intermediate_lineage / build_stage_payload 不再被
# app.py / CLI 调用（run() 已不再返回 lineage 键），函数体注释存档。
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
        limit = int(result.get("business", {}).get("transport", {}).get(
            "max_response_bytes") or _int_env("MAX_RESPONSE_BYTES", 67108864))
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
        prog="CMA_manage_manual_review_task",
        description="按表1 #25 手动触发人工复核派单、登记结论并做样本隔离")
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

    # [v4.2] 响应去 lineageFile：阶段内/阶段末统一返回 outputFile + result，
    # 二者差异仅由 CLI 的 --is-last-operator 决定是否打包内容对象。
    if args.is_last_operator:
        data: Dict[str, Any] = {
            "runId": result["runId"],
            "status": result["status"],
            "reviewStatus": result["reviewStatus"],
            "dataShape": {"outputFile": "file-object"},
            "outputFile": stage_file_object(result),
            "statistics": result["statistics"],
        }
    else:
        data = {
            "runId": result["runId"],
            "status": result["status"],
            "reviewStatus": result["reviewStatus"],
            "dataShape": {"outputFile": "path"},
            "outputFile": result["outputFile"],
            "statistics": result["statistics"],
        }
    sys.stdout.write(json.dumps({"code": 0, "msg": "管理人工复核任务完成", "data": data},
                                ensure_ascii=False) + "\n")
    return 0


if __name__ == "__main__":
    sys.exit(main())
