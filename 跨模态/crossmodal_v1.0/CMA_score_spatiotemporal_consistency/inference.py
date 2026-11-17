"""算子 15「评分时空一致性」业务入口（CLI + 可复用 ``run``）。

固定口径（表1 #15）：
- 规则型，**无模型**，**默认关闭**；关闭时不做任何时序判定，样本原样透传；
- 开启后用**线性插值**校准视频帧/语音片段/字幕三条时间轴，时间差默认不超过 0.5 秒；
- 三者缺一（含纯图文）→ ``not_applicable``，留样本池，**不隔离、不能写成通过**；
- 超过阈值 → 样本隔离台账（交 #21）；损坏/缺字段/时间戳不可解析/编码异常 → 原始隔离台账；
- 锚点不足时显式标记 ``interpolation.applied=false``，按原始时间轴计算真实差值，
  **绝不伪造已校准或任何分数**；``require_calibration=true`` 时改判待人工校准（不隔离）。

本模块只写 ``OUTPUT_DIR/<uniqueId>/``；正式接口见 ``app.py``。
"""
from __future__ import annotations

import argparse
import json
import os
import shutil
import sys
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

from typing import Any, Dict, List, Optional

import yaml

from utils import io as io_utils
from utils import lineage as lineage_mod
from utils import logger as logger_mod
from utils import temporal
from utils import transport
from utils.schemas import BusinessOverrides, ConfigError, OperatorConfig

OPERATOR = temporal.OPERATOR
OPERATOR_VERSION = temporal.OPERATOR_VERSION
FORMAL_OPERATOR_NUMBER = temporal.FORMAL_OPERATOR_NUMBER
STAGE_ID = temporal.STAGE_ID

MAX_UPLOAD_BYTES = 67108864
MAX_UNPACKED_BYTES = 67108864
MAX_RESPONSE_BYTES = 67108864
MAX_FILES = 1000

BASE_DIR = Path(__file__).resolve().parent
DEFAULT_CONFIG_PATH = BASE_DIR / "configs" / "config.yaml"
INPUT_DIR_NAME = "input"
MEDIA_DIR_NAME = "media"


class InputError(ValueError):
    """输入素材不可用。"""


# --------------------------------------------------------------------------- #
# 配置
# --------------------------------------------------------------------------- #

def default_config_path() -> Path:
    override = os.environ.get("OPERATOR_CONFIG")
    return Path(override) if override else DEFAULT_CONFIG_PATH


def _validation_message(exc: Exception) -> str:
    errors = getattr(exc, "errors", None)
    if callable(errors):
        parts = []
        for item in errors():
            location = ".".join(str(part) for part in item.get("loc", ()))
            parts.append("%s: %s" % (location or "<config>", item.get("msg", "非法")))
        if parts:
            return "；".join(parts)
    return str(exc)


def load_config(config_path=None, overrides: Optional[Dict[str, Any]] = None) -> OperatorConfig:
    """读取 YAML 配置并合并请求覆盖，做结构化与定义域校验。"""
    path = Path(config_path) if config_path else default_config_path()
    if not path.is_file():
        raise ConfigError("配置文件不存在：%s" % path)
    try:
        raw = yaml.safe_load(path.read_text(encoding="utf-8"))
    except (yaml.YAMLError, OSError, UnicodeDecodeError) as exc:
        raise ConfigError("配置文件无法解析：%s" % type(exc).__name__) from exc
    if raw is None:
        raw = {}
    if not isinstance(raw, dict):
        raise ConfigError("配置文件顶层必须是对象")
    payload = dict(raw)
    if overrides is not None:
        if not isinstance(overrides, dict):
            raise ConfigError("config 必须是对象，不能是数组或标量")
        try:
            provided = BusinessOverrides.model_validate(overrides).provided()
        except ConfigError:
            raise
        except Exception as exc:  # noqa: BLE001 - pydantic 校验错误统一为 ConfigError
            raise ConfigError("config 非法：%s" % _validation_message(exc)) from exc
        payload.update(provided)
    try:
        return OperatorConfig.model_validate(payload)
    except ConfigError:
        raise
    except Exception as exc:  # noqa: BLE001
        raise ConfigError("配置非法：%s" % _validation_message(exc)) from exc


def config_snapshot(config: OperatorConfig,
                    overrides: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
    """血缘 config 快照：业务口径 + 部署身份 + 实际覆盖键。"""
    snapshot: Dict[str, Any] = {
        "business": config.business_dict(),
        "deployment": config.deployment_dict(),
        "overridden_keys": [],
    }
    if overrides:
        provided = BusinessOverrides.model_validate(overrides).provided()
        snapshot["overridden_keys"] = sorted(provided.keys())
        snapshot["overrides"] = provided
    return snapshot


# --------------------------------------------------------------------------- #
# 路径与素材
# --------------------------------------------------------------------------- #

def default_output_root() -> Path:
    override = os.environ.get("OUTPUT_DIR")
    return Path(override) if override else BASE_DIR / "output"


def resolve_input(input_file: Optional[str], task_root: Path, *,
                  allow_external_input: bool) -> Path:
    """解析入口 JSONL：阶段内后续算子传上一节点临时 outputFile，阶段首从任务输入目录取。"""
    if input_file:
        if allow_external_input:
            candidate = io_utils.resolved_absolute(input_file)
        else:
            candidate = io_utils.resolve_in_task(task_root, input_file)
        if not candidate.is_file():
            raise InputError("输入文件不存在：%s" % candidate.name)
        return candidate
    input_dir = task_root / INPUT_DIR_NAME
    if not input_dir.is_dir():
        raise InputError("任务目录缺少输入素材（%s），且请求未提供 inputFile" % INPUT_DIR_NAME)
    path, _name = transport.resolve_entrypoint(input_dir)
    return path


def _media_roots(input_file: Path, task_root: Path) -> List[Path]:
    """媒体/sidecar 相对路径的解析根：输入文件所在目录、其父目录、任务输入目录、任务根。"""
    roots: List[Path] = []
    for candidate in (input_file.parent, input_file.parent.parent, task_root / INPUT_DIR_NAME,
                      task_root):
        resolved = io_utils.resolved_absolute(candidate)
        if resolved not in roots:
            roots.append(resolved)
    return roots


def _carry_media(run_dir: Path, source_path: Path, relative: str) -> bool:
    """把被引用的媒体/sidecar 按原相对路径复制到本次运行目录（可搬运交付）。"""
    try:
        normalized = io_utils.normalize_zip_member_name(relative.replace("\\", "/"))
    except ValueError:
        return False
    target = run_dir / normalized
    target.parent.mkdir(parents=True, exist_ok=True)
    try:
        shutil.copyfile(source_path, target)
    except OSError:
        return False
    return True


def _carry_record_media(run_dir: Path, media_roots: List[Path], spec: Dict[str, Any],
                        stats: Dict[str, Any]) -> None:
    """尽力把被引用的媒体/sidecar 搬到本次运行目录；缺失只计数，不阻断判定。"""
    for relative in spec.get("media_refs") or []:
        try:
            resolved = temporal.resolve_media_file(media_roots, relative)
        except io_utils.UnsupportedMediaPath:
            stats["media_missing"] += 1
            continue
        if resolved is None:
            stats["media_missing"] += 1
            continue
        if _carry_media(run_dir, resolved, relative):
            stats["media_carried"] += 1
        else:
            stats["media_missing"] += 1


# --------------------------------------------------------------------------- #
# 统计
# --------------------------------------------------------------------------- #

def _initial_statistics(config: OperatorConfig, entrypoint: str) -> Dict[str, Any]:
    return {
        "enabled": bool(config.enabled),
        "entrypoint": entrypoint,
        "input_records": 0,
        "blank_lines": 0,
        "valid_records": 0,
        "bad_lines": 0,
        "origin_quarantined": 0,
        "rejects_entries": 0,
        "passed_records": 0,
        "failed_records": 0,
        "not_applicable_records": 0,
        "pending_calibration_records": 0,
        "skipped_records": 0,
        "main_records": 0,
        "calibrated_records": 0,
        "uncalibrated_records": 0,
        "media_carried": 0,
        "media_missing": 0,
        "max_time_delta_sec": config.max_time_delta_sec,
        "interpolation": config.interpolation,
        "interpolation_method": config.interpolation_method,
        "require_calibration": bool(config.require_calibration),
        "model": None,
        "models_available": False,
        "degraded": False,
        "encoding_error": False,
        "file_read_incomplete": False,
        "empty_input": False,
        "conservation_ok": False,
        "reasons": {},
    }


def _bump_reason(stats: Dict[str, Any], reason: str) -> None:
    reasons = stats["reasons"]
    reasons[reason] = reasons.get(reason, 0) + 1


def _conservation(stats: Dict[str, Any]) -> bool:
    first = stats["input_records"] == stats["valid_records"] + stats["bad_lines"]
    second = stats["valid_records"] == (
        stats["passed_records"] + stats["failed_records"] + stats["not_applicable_records"]
        + stats["pending_calibration_records"] + stats["skipped_records"]
        + stats["origin_quarantined"])
    third = stats["rejects_entries"] == stats["bad_lines"] + stats["origin_quarantined"]
    if stats["enabled"]:
        fourth = stats["main_records"] == (stats["passed_records"]
                                          + stats["not_applicable_records"])
    else:
        fourth = stats["main_records"] == stats["skipped_records"]
    return bool(first and second and third and fourth)


# --------------------------------------------------------------------------- #
# 主流程
# --------------------------------------------------------------------------- #

def run(input_file: Optional[str] = None, unique_id: str = "", config_path=None,
        output_root=None, overrides: Optional[Dict[str, Any]] = None,
        allow_external_input: bool = False) -> Dict[str, Any]:
    """执行一次时空一致性评分；产物只写入 ``OUTPUT_DIR/<uniqueId>/``。"""
    config = load_config(config_path, overrides)
    root = io_utils.resolve_root(output_root if output_root is not None else default_output_root())
    task_root = io_utils.task_dir(root, unique_id)
    source = resolve_input(input_file, task_root, allow_external_input=allow_external_input)

    operator_dir = task_root / OPERATOR
    run_id = io_utils.next_run_id(operator_dir)
    run_dir = operator_dir / run_id
    run_dir.mkdir(parents=True, exist_ok=True)

    log_name = "%s.log" % run_id
    log = logger_mod.get_logger("operator15.%s.%s" % (unique_id, run_id), run_dir / log_name)
    logger_mod.log_action(log, "输入", "uniqueId=%s 输入=%s" % (unique_id, source.name))
    logger_mod.log_action(log, "配置", "enabled=%s 阈值=%.3fs 插值=%s/%s require_calibration=%s"
                          % (config.enabled, config.max_time_delta_sec, config.interpolation,
                             config.interpolation_method, config.require_calibration))

    entrypoint = temporal.MAIN_NAME_ENABLED if config.enabled else temporal.MAIN_NAME_DISABLED
    stats = _initial_statistics(config, entrypoint)
    started_at = lineage_mod.now_iso()
    event_time = started_at
    media_roots = _media_roots(source, task_root)

    main_partial = run_dir / (entrypoint + ".partial")
    rejects_partial = run_dir / (temporal.REJECTS_NAME + ".partial")
    quarantine_partial = run_dir / (temporal.SAMPLE_QUARANTINE_NAME + ".partial")
    pending_partial = run_dir / (temporal.PENDING_NAME + ".partial")
    partials = [main_partial, rejects_partial, quarantine_partial, pending_partial]

    main_writer = io_utils.JsonlWriter(main_partial)
    rejects_writer = io_utils.JsonlWriter(rejects_partial)
    quarantine_writer = io_utils.JsonlWriter(quarantine_partial)
    pending_writer = io_utils.JsonlWriter(pending_partial)

    def _reject(line: int, reason: str, label: str,
                record: Optional[Dict[str, Any]] = None,
                sample_id: Optional[str] = None) -> None:
        stats["origin_quarantined"] += 1
        _bump_reason(stats, reason)
        rejects_writer.write(temporal.make_ledger_entry(
            line=line, reason=reason, label=label, action=temporal.ACTION_RECORD,
            destination=temporal.DEST_ORIGIN_QUARANTINE, event_time=event_time,
            sample_id=sample_id, record=record))

    status = "success"
    message = ""
    try:
        line_number = 0
        try:
            for raw_line in io_utils.iter_decoded_lines(source):
                if not raw_line.strip():
                    stats["blank_lines"] += 1
                    continue
                line_number += 1
                stats["input_records"] += 1
                try:
                    record = json.loads(raw_line)
                except json.JSONDecodeError:
                    stats["bad_lines"] += 1
                    _bump_reason(stats, temporal.REASON_JSON_PARSE_ERROR)
                    rejects_writer.write(temporal.make_ledger_entry(
                        line=line_number, reason=temporal.REASON_JSON_PARSE_ERROR,
                        label=temporal.LABEL_ORIGIN_UNPARSABLE, action=temporal.ACTION_RECORD,
                        destination=temporal.DEST_ORIGIN_QUARANTINE, event_time=event_time))
                    continue
                if not isinstance(record, dict):
                    stats["bad_lines"] += 1
                    _bump_reason(stats, temporal.REASON_RECORD_NOT_OBJECT)
                    rejects_writer.write(temporal.make_ledger_entry(
                        line=line_number, reason=temporal.REASON_RECORD_NOT_OBJECT,
                        label=temporal.LABEL_ORIGIN_UNPARSABLE, action=temporal.ACTION_RECORD,
                        destination=temporal.DEST_ORIGIN_QUARANTINE, event_time=event_time))
                    continue

                stats["valid_records"] += 1
                record = temporal.normalize_native_pair(record)
                sample_id = temporal.record_sample_id(record)
                if not sample_id:
                    # 结构性缺字段：即使路线关闭也属原始文件问题
                    _bump_reason(stats, temporal.REASON_MISSING_SAMPLE_ID)
                    stats["origin_quarantined"] += 1
                    rejects_writer.write(temporal.make_ledger_entry(
                        line=line_number, reason=temporal.REASON_MISSING_SAMPLE_ID,
                        label=temporal.LABEL_ORIGIN_MISSING_FIELD,
                        action=temporal.ACTION_RECORD,
                        destination=temporal.DEST_ORIGIN_QUARANTINE, event_time=event_time,
                        record=record))
                    continue

                if not config.enabled:
                    # 关闭路线：不做任何时序判定、不隔离，仅透传
                    temporal.apply_skipped(record)
                    main_writer.write(record)
                    stats["skipped_records"] += 1
                    stats["main_records"] += 1
                    continue

                try:
                    spec = temporal.resolve_spec(record, media_roots)
                except temporal.TimelineUnreadable:
                    _reject(line_number, temporal.REASON_TIMELINE_UNREADABLE,
                            temporal.LABEL_ORIGIN_DAMAGED, record, sample_id)
                    continue
                except temporal.TimelineError:
                    _reject(line_number, temporal.REASON_TIMELINE_INVALID,
                            temporal.LABEL_ORIGIN_UNPARSABLE, record, sample_id)
                    continue

                _carry_record_media(run_dir, media_roots, spec, stats)

                try:
                    outcome = temporal.evaluate_record(spec, line=line_number,
                                                       config=config, event_time=event_time)
                except temporal.BindingLimitExceeded:
                    _reject(line_number, temporal.REASON_TOO_MANY_BINDINGS,
                            temporal.LABEL_ORIGIN_UNPARSABLE, record, sample_id)
                    continue
                except temporal.TimelineError:
                    _reject(line_number, temporal.REASON_TIMELINE_INVALID,
                            temporal.LABEL_ORIGIN_UNPARSABLE, record, sample_id)
                    continue

                action = outcome["action"]
                reason = outcome["reason"]
                _bump_reason(stats, reason)
                if action == "pass":
                    main_writer.write(outcome["record"])
                    stats["passed_records"] += 1
                    stats["main_records"] += 1
                    if outcome["calibrated"]:
                        stats["calibrated_records"] += 1
                    else:
                        stats["uncalibrated_records"] += 1
                elif action == "not_applicable":
                    main_writer.write(outcome["record"])
                    stats["not_applicable_records"] += 1
                    stats["main_records"] += 1
                elif action == "pending":
                    pending_writer.write(outcome["entry"])
                    stats["pending_calibration_records"] += 1
                else:  # isolate：时间差超限 → 样本隔离库
                    quarantine_writer.write(outcome["entry"])
                    stats["failed_records"] += 1
                    if outcome["calibrated"]:
                        stats["calibrated_records"] += 1
                    else:
                        stats["uncalibrated_records"] += 1
        except UnicodeDecodeError:
            # 文件不是严格 UTF-8：整份文件不可继续读取，显式记账并停止，不静默丢弃
            stats["input_records"] += 1
            stats["bad_lines"] += 1
            stats["encoding_error"] = True
            stats["file_read_incomplete"] = True
            _bump_reason(stats, temporal.REASON_ENCODING_ERROR)
            rejects_writer.write(temporal.make_ledger_entry(
                line=line_number + 1, reason=temporal.REASON_ENCODING_ERROR,
                label=temporal.LABEL_ORIGIN_ENCODING, action=temporal.ACTION_RECORD,
                destination=temporal.DEST_ORIGIN_QUARANTINE, event_time=event_time))
    except BaseException:
        for writer in (main_writer, rejects_writer, quarantine_writer, pending_writer):
            writer.close()
        io_utils.cleanup_files(*partials)
        logger_mod.close_logger(log)
        raise
    finally:
        main_writer.close()
        rejects_writer.close()
        quarantine_writer.close()
        pending_writer.close()

    stats["rejects_entries"] = rejects_writer.count
    stats["empty_input"] = stats["input_records"] == 0 and stats["blank_lines"] == 0
    stats["degraded"] = stats["pending_calibration_records"] > 0

    # 原子提交：先写好 .partial 再替换，避免下游读到半截文件
    main_path = run_dir / entrypoint
    rejects_path = run_dir / temporal.REJECTS_NAME
    quarantine_path = run_dir / temporal.SAMPLE_QUARANTINE_NAME
    pending_path = run_dir / temporal.PENDING_NAME
    try:
        for partial, final in ((main_partial, main_path), (rejects_partial, rejects_path),
                               (quarantine_partial, quarantine_path),
                               (pending_partial, pending_path)):
            os.replace(str(partial), str(final))
    except OSError as exc:
        io_utils.cleanup_files(*partials)
        logger_mod.log_action(log, "警告", "产物提交失败：%s" % type(exc).__name__)
        raise

    if stats["encoding_error"] and stats["valid_records"] == 0:
        status, message = "failed", "输入文件不是严格 UTF-8，无法读取任何记录"
    elif stats["degraded"]:
        status = "degraded"
        message = ("时间轴锚点不足且 require_calibration=true：%d 条记录进入待人工校准台账，"
                   "未判定通过、未隔离" % stats["pending_calibration_records"])
    elif stats["bad_lines"] or stats["origin_quarantined"]:
        status = "partial"
        message = "部分记录进入异常/原始隔离台账"
    else:
        status = "success"
        if not config.enabled:
            message = "时空一致性路线未启用：样本原样透传，未执行时序判定、未隔离"
        else:
            message = ("时空一致性判定完成：通过 %d 条，超限隔离 %d 条，不适用 %d 条"
                       % (stats["passed_records"], stats["failed_records"],
                          stats["not_applicable_records"]))

    stats["conservation_ok"] = _conservation(stats)

    artifacts = [
        {"path": entrypoint, "role": "main"},
        {"path": temporal.REJECTS_NAME, "role": "review"},
        {"path": temporal.SAMPLE_QUARANTINE_NAME, "role": "review"},
        {"path": temporal.PENDING_NAME, "role": "review"},
        {"path": log_name, "role": "log"},
    ]
    manifest = transport.build_manifest(entrypoint, artifacts)
    transport.write_manifest(run_dir, manifest)
    publish_stage_output(run_dir)

    media_paths: List[str] = []
    media_dir = run_dir / MEDIA_DIR_NAME
    if media_dir.is_dir():
        for path in sorted(media_dir.rglob("*")):
            if path.is_file():
                media_paths.append(str(path.relative_to(run_dir)).replace("\\", "/"))
    artifact_paths = [entrypoint, temporal.REJECTS_NAME, temporal.SAMPLE_QUARANTINE_NAME,
                      temporal.PENDING_NAME, log_name, transport.MANIFEST_NAME] + media_paths

    # [v4.2] 血缘/阶段归档输出已停用：不再构建血缘记录、不再写 lineage.json /
    # stage_context.json；这些键也不再随 run() 返回给 app.py。
    # snapshot = config_snapshot(config, overrides)
    # finished_at = lineage_mod.now_iso()
    # record = lineage_mod.make_lineage(
    #     operator=OPERATOR, version=config.operator.version, unique_id=unique_id,
    #     stage_id=config.stage_id, run_id=run_id, input_file=str(source),
    #     output_file=str(main_path), status=status, started_at=started_at,
    #     finished_at=finished_at, config=snapshot, statistics=stats, artifacts=artifacts,
    #     formal_operator_number=FORMAL_OPERATOR_NUMBER, model=None)
    # lineage_path = run_dir / lineage_mod.LINEAGE_NAME
    # io_utils.atomic_write_json(lineage_path, [record])
    #
    # stage_lineage = [record]
    # prior_lineage = lineage_mod.load_prior_lineage(media_roots)
    # stage_context = lineage_mod.build_stage_context(
    #     stage_id=config.stage_id, unique_id=unique_id, run_id=run_id, entrypoint=entrypoint,
    #     stage_lineage=stage_lineage, prior_lineage=prior_lineage)
    # io_utils.atomic_write_json(run_dir / lineage_mod.STAGE_CONTEXT_NAME, stage_context)

    logger_mod.log_action(
        log, "输出", "入口=%s 通过=%d 超限隔离=%d 不适用=%d 待人工校准=%d 透传=%d 原始隔离=%d 坏行=%d"
        % (entrypoint, stats["passed_records"], stats["failed_records"],
           stats["not_applicable_records"], stats["pending_calibration_records"],
           stats["skipped_records"], stats["origin_quarantined"], stats["bad_lines"]))
    logger_mod.log_action(log, "完成", "状态=%s 统计守恒=%s" % (status, stats["conservation_ok"]))
    logger_mod.close_logger(log)

    return {
        "status": status,
        "message": message,
        "outputFile": str(main_path),
        "rejectsFile": str(rejects_path),
        "sampleQuarantineFile": str(quarantine_path),
        "pendingFile": str(pending_path),
        # [v4.2] 血缘输出已停用：不再回传 lineageFile/stageLineage/priorLineage
        "manifestPath": str(run_dir / transport.MANIFEST_NAME),
        "statistics": stats,
        "runId": run_id,
        "runDir": str(run_dir),
        "entrypoint": entrypoint,
        "artifacts": artifacts,
        "artifactPaths": artifact_paths,
        "mediaPaths": media_paths,
    }


# --------------------------------------------------------------------------- #
# 响应形态
# --------------------------------------------------------------------------- #

# [v4.2] 血缘输出已停用：以下两个响应构造函数仅服务旧 lineageFile 形态，保留注释存档。
# def build_intermediate_lineage(result: Dict[str, Any]) -> Dict[str, Any]:
#     """中间态：``data`` 只承载当前节点血缘对象（含临时 outputFile）。"""
#     import copy
#
#     return copy.deepcopy(result["stageLineage"][-1])
#
#
# def build_stage_payload(result: Dict[str, Any]) -> List[Dict[str, Any]]:
#     """阶段末：当前阶段完整血缘数组（已剔除临时 outputFile）。"""
#     return lineage_mod.to_stage_lineage(result["stageLineage"])


def stage_file_object(result: Dict[str, Any],
                      max_response_bytes: Optional[int] = None) -> Dict[str, Any]:
    """阶段末：``crossmodal-files-v1`` 文件内容对象。"""
    limit = MAX_RESPONSE_BYTES if max_response_bytes is None else int(max_response_bytes)
    return transport.atomic_pack_file_object(
        result["runDir"], result["entrypoint"], result["artifactPaths"],
        max_response_bytes=limit)


# --------------------------------------------------------------------------- #
# CLI
# --------------------------------------------------------------------------- #

def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="算子 15 评分时空一致性（表1 #15）：线性插值校准视频帧/语音/字幕时间差")
    parser.add_argument("--input", default=None, help="输入 JSONL 完整记录清单（阶段内可省略）")
    parser.add_argument("--unique-id", dest="unique_id", required=True, help="任务id_文件id")
    parser.add_argument("--output-root", dest="output_root", default=None,
                        help="输出根（默认 OUTPUT_DIR 或算子目录 output/）")
    parser.add_argument("--config", dest="config", default=None, help="YAML 配置路径")
    parser.add_argument("--override", dest="override", default=None,
                        help='业务参数覆盖 JSON，如 {"enabled": true}')
    parser.add_argument("--is-last", dest="is_last", action="store_true",
                        help="按阶段末形态预览 data（文件对象 + 阶段血缘数组）")
    parser.add_argument("--quiet", dest="quiet", action="store_true", help="只输出核心摘要")
    return parser


def cli_data_shape(status: str, is_last: bool) -> Dict[str, str]:
    # [v4.2] 响应形态不再区分中间态/阶段末，统一 outputFile + result
    if status == "failed":
        return {}
    return {"outputFile": "file-object", "result": "summary"}


def _summary(result: Dict[str, Any], is_last: bool, quiet: bool) -> Dict[str, Any]:
    stats = result["statistics"]
    payload: Dict[str, Any] = {
        "dataShape": cli_data_shape(result["status"], is_last),
        "entrypoint": result["entrypoint"],
        "status": result["status"],
        "runId": result["runId"],
        "runDir": result["runDir"],
        "statistics": {
            "input_records": stats["input_records"],
            "valid_records": stats["valid_records"],
            "blank_lines": stats["blank_lines"],
            "bad_lines": stats["bad_lines"],
            "passed_records": stats["passed_records"],
            "failed_records": stats["failed_records"],
            "not_applicable_records": stats["not_applicable_records"],
            "pending_calibration_records": stats["pending_calibration_records"],
            "skipped_records": stats["skipped_records"],
            "origin_quarantined": stats["origin_quarantined"],
            "rejects_entries": stats["rejects_entries"],
            "main_records": stats["main_records"],
            "calibrated_records": stats["calibrated_records"],
            "uncalibrated_records": stats["uncalibrated_records"],
            "media_carried": stats["media_carried"],
            "media_missing": stats["media_missing"],
            "conservation_ok": stats["conservation_ok"],
        },
    }
    if not quiet:
        payload["outputFile"] = result["outputFile"]
        payload["ledgers"] = {
            "rejects": result["rejectsFile"],
            "sampleQuarantine": result["sampleQuarantineFile"],
            "pending": result["pendingFile"],
        }
        payload["reasons"] = stats["reasons"]
    return payload


def _emit(payload: Dict[str, Any]) -> None:
    sys.stdout.write(json.dumps(payload, ensure_ascii=False) + "\n")
    sys.stdout.flush()


def main(argv: Optional[List[str]] = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    overrides = None
    if args.override:
        try:
            overrides = json.loads(args.override)
        except json.JSONDecodeError:
            _emit({"code": 1, "msg": "ConfigError: --override 必须是 JSON 对象", "data": {}})
            return 1
    try:
        result = run(args.input, args.unique_id, config_path=args.config,
                     output_root=args.output_root, overrides=overrides,
                     allow_external_input=True)
    except (ConfigError, InputError, io_utils.TaskBusyError, OSError, ValueError) as exc:
        _emit({"code": 1, "msg": "%s: %s" % (type(exc).__name__, exc), "data": {}})
        return 1
    code = 0 if result["status"] in ("success", "partial", "degraded") else 1
    _emit({"code": code, "msg": result["message"],
           "data": _summary(result, args.is_last, args.quiet)})
    return code


if __name__ == "__main__":
    raise SystemExit(main())
