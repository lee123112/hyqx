"""算子 01「接入多模态批次」推理/编排入口（表1 第 1 环节「输入核验」，stageId=S1）。

处理流程
--------
1. 解析 ``configs/config.yaml`` + 业务覆盖，校验 uniqueId、入口清单与任务内路径；
2. **流式逐行**扫描批次清单：空行计数、坏行/非对象/编码错误/超长行进原始隔离台账，
   合法对象按 ``files`` 列表（或单文件简写）展开为若干文件登记项；
3. 每个登记项按「结构 → 链接可搬运 → 格式白名单 → 本地文件可读性 → 授权 →
   来源/模态 → 登记要素 → 通过」判定，**唯一归口**到四个产物之一；
4. 通过项写入工作集（``01_workitems.jsonl``，**只登记原文件链接、不复制媒体**）；
   格式/文件问题 → 原始隔离记录；授权非可训练 → 样本隔离记录；
   缺来源/批次/上游版本、重复登记 → 待补登记退回清单；同时生成批次台账；
5. 校验统计守恒（``input = valid + rejected``、``objects = 四路之和`` 等），
   不成立即结构化失败，不伪造成功；写入运行清单与规范日志。

阶段形态（v4.2）：本算子是**阶段首算子**（请求省略 ``inputFile``，入口取任务 ``input/`` 目录）；
响应统一为 ``outputFile``（crossmodal-files-v1 文件对象）+ ``result`` 业务摘要，
阶段末 ``isLastOperator=true`` 仅表示可回收输出文件，**不再返回/写 ``lineageFile``、``stage_context.json``**。
"""
from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path
from typing import Any, Dict, List, Mapping, Optional, Sequence, Tuple

from utils import intake as it
from utils import io as io_utils
from utils import lineage as lineage_utils
from utils import logger as logger_utils
from utils import schemas
from utils import transport

BASE_DIR = Path(__file__).resolve().parent
DEFAULT_CONFIG_PATH = BASE_DIR / "configs" / "config.yaml"
DEFAULT_INPUT_DIR_NAME = "input"
DEFAULT_LOG_NAME = it.LOG_NAME
FAILURE_NAME = "failure.json"

#: 阶段末打包进内容对象的产物（按此顺序；媒体不复制，只登记链接）
STAGE_ARTIFACT_ORDER: Tuple[str, ...] = it.STAGE_ARTIFACT_ORDER

_ROLES: Dict[str, str] = {
    it.MAIN_NAME: "main",
    it.ORIGIN_QUARANTINE_NAME: "origin_quarantine",
    it.SAMPLE_QUARANTINE_NAME: "sample_quarantine",
    it.RETURN_NAME: "return",
    it.REGISTRY_NAME: "batch_registry",
    DEFAULT_LOG_NAME: "log",
}


class InputError(ValueError):
    """输入文件缺失、越界或编码非法。"""


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
    try:
        return manifest, io_utils.normalize_zip_member_name(relative)
    except ValueError:
        return manifest, source.name


# --------------------------------------------------------------------------- #
# 主流程
# --------------------------------------------------------------------------- #

def run(input_file: Optional[str] = None, unique_id: Optional[str] = None,
        config_path=None, output_root=None, overrides: Optional[Mapping[str, Any]] = None,
        allow_external_input: bool = False) -> Dict[str, Any]:
    """执行一次批次接入，返回本次运行的结果字典。"""
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
        operator_dir = task / it.RUN_STAGE
        operator_dir.mkdir(parents=True, exist_ok=True)
        run_id = io_utils.next_run_id(operator_dir)
        run_dir = operator_dir / run_id
        run_dir.mkdir(parents=True, exist_ok=False)
        log = logger_utils.get_logger("%s.%s.%s" % (it.RUN_STAGE, value, run_id),
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
        "operator": it.OPERATOR_NAME,
        "operator_number": it.FORMAL_OPERATOR_NUMBER,
        "operator_version": it.OPERATOR_VERSION,
        "stage_id": it.STAGE_ID,
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


class _Writers:
    """四个 JSONL 产物的原子写入器集合（全部成功才整体提交）。"""

    def __init__(self, run_dir: Path):
        self.run_dir = run_dir
        self.main = io_utils.PartialJsonlWriter(run_dir / it.MAIN_NAME)
        self.origin = io_utils.PartialJsonlWriter(run_dir / it.ORIGIN_QUARANTINE_NAME)
        self.sample = io_utils.PartialJsonlWriter(run_dir / it.SAMPLE_QUARANTINE_NAME)
        self.back = io_utils.PartialJsonlWriter(run_dir / it.RETURN_NAME)

    def __enter__(self) -> "_Writers":
        for writer in (self.main, self.origin, self.sample, self.back):
            writer.__enter__()
        return self

    def __exit__(self, exc_type, exc, tb) -> bool:
        for writer in (self.main, self.origin, self.sample, self.back):
            writer.__exit__(exc_type, exc, tb)
        return False

    def write(self, outcome: it.Outcome, *, business: Mapping[str, Any], unique_id: str,
              run_id: str, lineno: int) -> None:
        if outcome.decision == it.DECISION_REGISTERED:
            self.main.write(it.build_workitem(outcome, unique_id=unique_id,
                                             run_id=run_id, lineno=lineno))
        elif outcome.decision == it.DECISION_ORIGIN:
            self.origin.write(it.build_ledger(outcome, unique_id=unique_id,
                                             run_id=run_id, lineno=lineno))
        elif outcome.decision == it.DECISION_SAMPLE:
            self.sample.write(it.build_ledger(outcome, unique_id=unique_id,
                                             run_id=run_id, lineno=lineno))
        else:
            self.back.write(it.build_ledger(outcome, unique_id=unique_id,
                                            run_id=run_id, lineno=lineno))


def _reject(writers: "_Writers", stats: Dict[str, Any], lineno: int, reason_code: str,
            preview: Any, business: Mapping[str, Any], unique_id: str,
            run_id: str) -> None:
    """坏行/非对象/编码错误/超长行：计入 ``rejected`` 并写入原始隔离台账。"""
    it.note_reject(stats, reason_code)
    writers.origin.write(it.build_reject_entry(lineno, reason_code, preview, business,
                                               unique_id=unique_id, run_id=run_id))


def _execute(*, task_root: Path, run_dir: Path, value: str, run_id: str,
             input_file: Optional[str], business: Mapping[str, Any],
             config: schemas.OperatorConfig, overrides: Mapping[str, Any],
             started_at: str, log, allow_external_input: bool) -> Dict[str, Any]:
    source = resolve_input(task_root, input_file,
                           allow_external_input=allow_external_input)
    logger_utils.log_action(log, "input", "输入文件 %s" % source.name)
    _manifest, entrypoint = resolve_manifest_entry(source)
    # [v4.2] 血缘输出已停用：不再读取上游阶段血缘（代码保留注释存档）
    # prior_lineage = lineage_utils.load_prior_lineage(
    #     [source.parent, task_root / DEFAULT_INPUT_DIR_NAME])
    local_dirs = [source.parent]
    enabled = bool(business.get("enabled", True))
    logger_utils.log_action(
        log, "config",
        "总开关=%s 文件检查=%s 去重=%s 来源模态校验=%s"
        % ("开启" if enabled else "关闭", business.get("file_check_mode"),
           bool(business.get("dedupe")), bool(business.get("check_source_modality"))))

    stats = it.empty_statistics()
    stats["enabled"] = enabled
    stats["file_check_mode"] = business.get("file_check_mode")
    outcomes: List[it.Outcome] = []
    seen_keys: set = set()

    with _Writers(run_dir) as writers:
        if not enabled:
            logger_utils.log_action(log, "disabled", "总开关关闭：本环节不跑，未登记任何对象")
        else:
            lineno = 0
            for raw in io_utils.iter_raw_lines(source):
                lineno += 1
                stats["input_lines"] = int(stats["input_lines"]) + 1
                if len(raw) > io_utils.MAX_LINE_BYTES:
                    _reject(writers, stats, lineno, "line_too_long", None, business,
                            value, run_id)
                    continue
                if not raw.strip():
                    stats["blank_lines"] = int(stats["blank_lines"]) + 1
                    continue
                stats["input"] = int(stats["input"]) + 1
                try:
                    text = io_utils.decode_line(raw, first=lineno == 1)
                except UnicodeDecodeError:
                    _reject(writers, stats, lineno, "invalid_utf8", None, business,
                            value, run_id)
                    continue
                try:
                    record = json.loads(text)
                except json.JSONDecodeError:
                    _reject(writers, stats, lineno, "json_parse_error", text, business,
                            value, run_id)
                    continue
                if not isinstance(record, dict):
                    _reject(writers, stats, lineno, "record_not_object", text, business,
                            value, run_id)
                    continue

                stats["valid"] = int(stats["valid"]) + 1
                raw_id = record.get("sample_id")
                sample_id = raw_id.strip() if isinstance(raw_id, str) else ""
                objects = it.expand_objects(record)
                if len(objects) > 1:
                    stats["records_with_multiple_files"] = int(
                        stats["records_with_multiple_files"]) + 1
                first = objects[0] if objects else {}
                if first and first.get("file_link") is None \
                        and not first.get("invalid_entry"):
                    stats["records_without_files"] = int(
                        stats["records_without_files"]) + 1

                for obj in objects:
                    outcome = it.evaluate_object(
                        record, sample_id=sample_id, obj=obj, business=business,
                        lineno=lineno, local_dirs=local_dirs)
                    if outcome.decision == it.DECISION_REGISTERED \
                            and business.get("dedupe", True):
                        key = it.registration_key(outcome)
                        if key in seen_keys:
                            outcome = it.mark_duplicate(outcome)
                            stats["duplicates"] = int(stats["duplicates"]) + 1
                        else:
                            seen_keys.add(key)
                    it.accumulate(stats, outcome)
                    outcomes.append(outcome)
                    writers.write(outcome, business=business, unique_id=value,
                                  run_id=run_id, lineno=lineno)

    stats["empty_input"] = int(stats["input"]) == 0
    failures = it.conservation_failures(stats)
    generated_at = lineage_utils.now_iso()
    registry = it.build_batch_registry(outcomes, business=business, unique_id=value,
                                       run_id=run_id, statistics=stats,
                                       generated_at=generated_at)
    stats["batches"] = len(registry["batches"])
    io_utils.atomic_write_json(run_dir / it.REGISTRY_NAME, registry)

    stats["conservation_failures"] = failures
    stats["conservation_ok"] = not failures
    status = it.derive_status(dict(stats), dict(business))
    stats["report_status"] = status

    logger_utils.log_action(
        log, "scan",
        "扫描完成：行数=%d 空行=%d 坏行=%d 有效记录=%d 登记项=%d"
        % (stats["input_lines"], stats["blank_lines"], stats["rejected"],
           stats["valid"], stats["objects"]))
    logger_utils.log_action(
        log, "register",
        "登记：可加工=%d（link_only=%d local_verified=%d 去重退回=%d 批次=%d）"
        % (stats["registered"], stats["link_only"], stats["local_verified"],
           stats["duplicates"], stats["batches"]))
    logger_utils.log_action(
        log, "quarantine",
        "隔离：原始文件-无法解析=%d 样本-敏感=%d 待补登记=%d"
        % (stats["origin_quarantined"], stats["sample_quarantined"],
           stats["returned"]))
    logger_utils.log_action(
        log, "conservation",
        "统计守恒校验=%s%s" % ("通过" if not failures else "失败",
                               "" if not failures else "：" + "; ".join(failures)))
    if failures:
        raise RuntimeError("统计守恒校验失败：%s" % "; ".join(failures))

    artifacts = _build_artifacts(run_dir)
    manifest = transport.build_manifest(it.MAIN_NAME, [
        {"path": it.MAIN_NAME, "role": "main"},
        {"path": it.ORIGIN_QUARANTINE_NAME, "role": "origin_quarantine"},
        {"path": it.SAMPLE_QUARANTINE_NAME, "role": "sample_quarantine"},
        {"path": it.RETURN_NAME, "role": "return"},
        {"path": it.REGISTRY_NAME, "role": "batch_registry"},
        {"path": DEFAULT_LOG_NAME, "role": "log"},
    ])
    transport.write_manifest(run_dir, manifest)

    finished_at = lineage_utils.now_iso()
    input_reference = str(source) if allow_external_input and input_file not in (None, "") \
        else entrypoint
    # [v4.2] 血缘/阶段归档输出已停用：不再构建血缘记录，不再写 lineage.json /
    # stage_context.json（原 make_lineage/load_lineage/to_stage_lineage/
    # build_stage_context 调用块注释存档）
    # node = lineage_utils.make_lineage(
    #     operator=it.OPERATOR_NAME, version=it.OPERATOR_VERSION, unique_id=value,
    #     stage_id=it.STAGE_ID, run_id=run_id, input_file=input_reference,
    #     output_file=str(run_dir / it.MAIN_NAME), status=status, started_at=started_at,
    #     finished_at=finished_at, config=config_snapshot, statistics=stats,
    #     artifacts=artifacts, formal_operator_number=it.FORMAL_OPERATOR_NUMBER,
    #     message="接入多模态批次完成")
    # history = lineage_utils.load_lineage(run_dir / lineage_utils.LINEAGE_NAME)
    # history.append(node)
    # io_utils.atomic_write_json(run_dir / lineage_utils.LINEAGE_NAME, history)
    # stage_records = lineage_utils.to_stage_lineage(history)
    # context = lineage_utils.build_stage_context(
    #     it.STAGE_ID, value, run_id, entrypoint, stage_records, prior_lineage, business)
    # io_utils.atomic_write_json(run_dir / lineage_utils.STAGE_CONTEXT_NAME, context)

    logger_utils.log_action(log, "done", "运行完成：runId=%s status=%s" % (run_id, status))
    return {
        "operator": it.OPERATOR_NAME,
        "operatorLabel": it.OPERATOR_LABEL,
        "operatorNumber": it.FORMAL_OPERATOR_NUMBER,
        "operatorVersion": it.OPERATOR_VERSION,
        "stageId": it.STAGE_ID,
        "uniqueId": value,
        "runId": run_id,
        "status": status,
        "runDir": str(run_dir),
        "outputFile": str(run_dir / it.MAIN_NAME),
        "entrypoint": it.MAIN_NAME,
        "inputFile": input_reference,
        "statistics": stats,
        "artifacts": artifacts,
        "manifest": manifest,
        # [v4.2] 血缘输出已停用：lineage/stageContext 键不再回传
        # "lineage": node,
        # "stageContext": context,
        "registry": registry,
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

# [v4.2] 血缘输出已停用：build_intermediate_lineage/build_stage_payload 不再被
# app.py / CLI 调用（原实现依赖 result["lineage"]，代码保留注释存档）
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
    candidates.append(DEFAULT_LOG_NAME)
    candidates.append(transport.MANIFEST_NAME)
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
        prog="CMA_receive_multimodal_batch",
        description="按表1 #01 接入并登记多模态批次：校验来源/格式白名单/授权，"
                    "只保存原文件链接，输出可加工清单与两类隔离记录")
    parser.add_argument("--input", default=None,
                        help="批次登记清单 JSONL；缺省时读取 OUTPUT_DIR/<uniqueId>/input/ 下的唯一 JSONL")
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


def _ensure_utf8_stdout() -> None:
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(encoding="utf-8")  # type: ignore[attr-defined]
        except (AttributeError, ValueError):  # pragma: no cover - 非标准流
            pass


def main(argv: Optional[Sequence[str]] = None) -> int:
    _ensure_utf8_stdout()
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

    # [v4.2] 阶段返回形态统一：不再区分阶段末/非阶段末，去除 lineageFile；
    # --is-last-operator 仅保留参数兼容，响应始终为 outputFile 文件内容对象
    data: Dict[str, Any] = {
        "runId": result["runId"],
        "status": result["status"],
        "dataShape": {"outputFile": "file-object"},
        "outputFile": stage_file_object(result),
        "statistics": result["statistics"],
    }
    sys.stdout.write(json.dumps({"code": 0, "msg": "接入多模态批次完成", "data": data},
                                ensure_ascii=False) + "\n")
    return 0 if result["status"] != "failed" else 1


if __name__ == "__main__":
    sys.exit(main())
