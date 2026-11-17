"""算子 14「评分视文语义一致性」业务入口（表1 #14，S3 阶段中间节点）。

职责：
- 加载并校验配置（表1 冻结项 + ``/predict.config`` 白名单覆盖）；
- 单次运行输入、输出都在 ``OUTPUT_DIR/<uniqueId>/runs/<runId>/``，任务之间互不可见；
- 流式处理 JSONL：坏行/缺字段/编码异常/业务拒绝全部进独立清单，绝不静默丢弃；
- 只写真实统计、真实模型身份；模型不可用时按配置显式失败或显式降级。

输出文件（``entrypoint`` 随路线开关变化）：
- 路线开启：``14_video_text_scores.jsonl``（通过 + 待处理 + 时序不适用透传，均带视文节点）
- 路线关闭：``14_passthrough.jsonl``（原样透传，节点 ``process_status=skipped``，不生成视文产物）
- 始终：``14_rejects.jsonl``（原始隔离区）、``14_sample_quarantine.jsonl``（样本隔离库）、
  ``14_pending.jsonl``（待处理台账）、``manifest.json``、``frames/``（被引用帧）
- [v4.2] 不再写 ``lineage.json`` / ``stage_context.json``。
"""
from __future__ import annotations

import argparse
import copy
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

from typing import Any, Dict, List, Optional, Sequence, Tuple

from utils import backends
from utils import io as io_utils
from utils import logger as logger_mod
from utils import schemas
from utils import transport
from utils import video_text
from utils.schemas import ConfigError, OperatorConfig

_TZ = timezone(timedelta(hours=8))


def _now_iso() -> str:
    """[v4.2] 时间工具；不再借用血缘模块。"""
    return datetime.now(_TZ).isoformat(timespec="seconds")

BASE_DIR = Path(__file__).resolve().parent
DEFAULT_OUTPUT_DIR = BASE_DIR / "output"
DEFAULT_CONFIG_PATH = BASE_DIR / "configs" / "config.yaml"

OPERATOR = video_text.OPERATOR
OPERATOR_VERSION = schemas.OPERATOR_VERSION
STAGE_ID = schemas.STAGE_ID
FORMAL_OPERATOR_NUMBER = schemas.FORMAL_OPERATOR_NUMBER
CLIP_NAME = video_text.CLIP_NAME

#: 中间节点临时 outputFile 的固定含义：本次运行的入口 JSONL
MAIN_NAME_ENABLED = video_text.MAIN_NAME_ENABLED
MAIN_NAME_DISABLED = video_text.MAIN_NAME_DISABLED
REJECTS_NAME = video_text.REJECTS_NAME
SAMPLE_QUARANTINE_NAME = video_text.SAMPLE_QUARANTINE_NAME
PENDING_NAME = video_text.PENDING_NAME
NOT_APPLICABLE_NAME = video_text.NOT_APPLICABLE_NAME
LOG_NAME = video_text.LOG_NAME
LINEAGE_NAME = video_text.LINEAGE_NAME
STAGE_CONTEXT_NAME = video_text.STAGE_CONTEXT_NAME

#: 输入暂存目录（由 /uploadFile 写入，阶段首从这里解析入口）
INPUT_DIR_NAME = "input"
#: 运行目录集合名
RUNS_DIR_NAME = "runs"

#: ``DependencyError`` 对下游统一可见（业务模块定义，推理层重导出）
DependencyError = video_text.DependencyError


class InputError(ValueError):
    """输入文件缺失、越出任务目录或无法解析。"""


class ModelUnavailable(DependencyError):
    """CLIP-ViT/B-32 不可用且未允许降级：整个任务显式失败。"""


# --------------------------------------------------------------------------- #
# 路径与环境
# --------------------------------------------------------------------------- #

def default_output_dir() -> Path:
    return Path(os.environ.get("OUTPUT_DIR") or DEFAULT_OUTPUT_DIR)


def default_config_path() -> Path:
    return Path(os.environ.get("OPERATOR_CONFIG") or DEFAULT_CONFIG_PATH)


# --------------------------------------------------------------------------- #
# 配置
# --------------------------------------------------------------------------- #

def load_config(config_path=None,
                overrides: Optional[Dict[str, Any]] = None) -> OperatorConfig:
    """读取 YAML 配置并与白名单覆盖项合并后严格校验。"""
    path = Path(config_path) if config_path else default_config_path()
    if not path.is_file():
        raise ConfigError("配置文件不存在：%s" % path)

    try:
        import yaml

        payload = yaml.safe_load(path.read_text(encoding="utf-8"))
    except ConfigError:
        raise
    except Exception as exc:  # noqa: BLE001 - YAML 解析失败必须显式报错
        raise ConfigError("配置文件无法解析（%s）：%s" % (type(exc).__name__, path.name)) from exc

    if payload is None:
        payload = {}
    if not isinstance(payload, dict):
        raise ConfigError("配置文件根节点必须是映射")

    merged = _deep_copy(payload)
    if overrides:
        if not isinstance(overrides, dict):
            raise ConfigError("config 覆盖项必须是对象")
        unknown = [key for key in overrides if key not in schemas.OVERRIDABLE_KEYS]
        if unknown:
            raise ConfigError("config 覆盖项不允许的键：%s" % ", ".join(sorted(unknown)))
        try:
            parsed = schemas.BusinessOverrides.model_validate(overrides)
        except Exception as exc:  # noqa: BLE001
            raise schemas.to_config_error(exc, "config 覆盖项") from exc
        for key, value in parsed.model_dump(exclude_none=True).items():
            if key == "clip":
                merged["clip"] = _deep_copy(dict(merged.get("clip") or {}, **value))
            else:
                merged[key] = value

    try:
        return OperatorConfig.model_validate(merged)
    except Exception as exc:  # noqa: BLE001
        raise schemas.to_config_error(exc, "配置") from exc


def _deep_copy(value: Any) -> Any:
    return copy.deepcopy(value)


def config_snapshot(config: OperatorConfig,
                    overrides: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
    """血缘里的配置快照：业务项 + 部署项 + 本次被覆盖的键。"""
    overridden: List[str] = []
    if overrides:
        for key, value in overrides.items():
            if key == "clip" and isinstance(value, dict):
                overridden.extend("clip.%s" % sub for sub in sorted(value))
            else:
                overridden.append(key)
    return {
        "business": {
            "enabled": bool(config.enabled),
            "low_score_threshold": config.low_score_threshold,
            "max_frames_per_segment": config.max_frames_per_segment,
            "max_text_chars": config.max_text_chars,
            "allow_degraded": bool(config.allow_degraded),
            "temporal_route_exclusion": bool(config.temporal_route_exclusion),
            "clip_model": CLIP_NAME,
            "clip_arch": config.clip.model_name,
            "clip_library": config.clip.library,
            "clip_weights": config.clip.weights,
            "clip_device": config.clip.device,
            "clip_batch_size": config.clip.batch_size,
        },
        "deployment": {
            "stage_id": config.stage_id,
            "operator": config.operator.name,
            "operator_version": config.operator.version,
            "formal_operator_number": FORMAL_OPERATOR_NUMBER,
        },
        "overridden_keys": sorted(set(overridden)),
    }


def clip_backend_payload(config: OperatorConfig) -> Dict[str, Any]:
    """传给 ``utils.backends`` 的模型配置片段（含 allow_degraded 语义）。"""
    return {
        "clip": {
            "library": config.clip.library,
            "model_name": config.clip.model_name,
            "weights": config.clip.weights,
            "device": config.clip.device,
            "batch_size": config.clip.batch_size,
        },
        "allow_degraded": bool(config.allow_degraded),
    }


# --------------------------------------------------------------------------- #
# 输入解析
# --------------------------------------------------------------------------- #

def resolve_input(input_file: Optional[str], task_root: Path,
                  allow_external_input: bool = False) -> Path:
    """解析输入文件：阶段首从任务输入目录取入口，阶段内只接受任务内路径。"""
    task_root = io_utils.resolved_absolute(task_root)
    if input_file:
        candidate = Path(input_file)
        if not candidate.is_absolute():
            # HTTP paths are task-relative; library/CLI paths keep cwd semantics.
            candidate = (Path.cwd() if allow_external_input else task_root) / candidate
        candidate = io_utils.resolved_absolute(candidate)
        if io_utils.is_within(task_root, candidate):
            if candidate.is_file():
                return candidate
            raise InputError("inputFile 在任务目录内但不存在：%s" % Path(input_file).name)
        if allow_external_input:
            if candidate.is_file():
                return candidate
            raise InputError("inputFile 不存在：%s" % input_file)
        raise InputError("inputFile 越出当前任务目录（拒绝跨任务读取）")

    input_dir = task_root / INPUT_DIR_NAME
    if not input_dir.is_dir():
        raise InputError("阶段首调用但任务输入目录不存在，请先调用 /uploadFile")
    try:
        entrypoint, _relative = transport.resolve_entrypoint(input_dir)
    except transport.TransportError as exc:
        raise InputError("无法解析任务输入入口：%s" % exc) from exc
    return entrypoint


def build_media_roots(input_path: Path, task_root: Path) -> List[Path]:
    """媒体相对路径的候选解析根（输入目录优先，其次任务根）。"""
    roots: List[Path] = []
    for candidate in (input_path.parent, input_path.parent.parent,
                      task_root / INPUT_DIR_NAME, task_root):
        resolved = io_utils.resolved_absolute(candidate)
        if resolved not in roots:
            roots.append(resolved)
    return roots


# --------------------------------------------------------------------------- #
# 媒体搬运
# --------------------------------------------------------------------------- #

def carry_frames(run_dir: Path, media_roots: Sequence[Path],
                 relatives: Sequence[str]) -> Tuple[int, int, List[str]]:
    """把被引用的帧复制进运行目录；返回 ``(carried, missing, relative_paths)``。

    同名不同内容时追加短摘要后缀，避免不同任务的帧互相覆盖。
    """
    target_dir = run_dir / "frames"
    carried_paths: List[str] = []
    carried = 0
    missing = 0
    seen: Dict[str, int] = {}
    for relative in relatives:
        try:
            source = video_text.resolve_media_file(media_roots, relative)
        except io_utils.UnsupportedMediaPath:
            missing += 1
            continue
        if source is None:
            missing += 1
            continue
        name = Path(relative).name
        destination = target_dir / name
        if destination.exists():
            same_size = destination.stat().st_size == source.stat().st_size
            if not same_size:
                digest = backends.sha256_of_file(source) or "dup"
                name = "%s_%s%s" % (Path(relative).stem, digest[:8], Path(relative).suffix)
                destination = target_dir / name
            elif name in seen:
                carried += 1
                continue
        destination.parent.mkdir(parents=True, exist_ok=True)
        if not destination.exists():
            shutil.copyfile(source, destination)
        seen[name] = seen.get(name, 0) + 1
        relative_dest = str(destination.relative_to(run_dir)).replace("\\", "/")
        if relative_dest not in carried_paths:
            carried_paths.append(relative_dest)
        carried += 1
    return carried, missing, carried_paths


# --------------------------------------------------------------------------- #
# 统计
# --------------------------------------------------------------------------- #

def _initial_statistics(config: OperatorConfig) -> Dict[str, Any]:
    return {
        "route_enabled": bool(config.enabled),
        "input_records": 0,
        "valid_records": 0,
        "output_records": 0,
        "blank_lines": 0,
        "bad_lines": 0,
        "passed_records": 0,
        "sample_isolated": 0,
        "origin_quarantined": 0,
        "pending_records": 0,
        "skipped_records": 0,
        "not_applicable_records": 0,
        "temporal_route_not_applicable": 0,
        "non_video_route_not_applicable": 0,
        "scored_records": 0,
        "segment_below_threshold": 0,
        "no_video_text_pair": 0,
        "no_frames": 0,
        "missing_description": 0,
        "model_unavailable": 0,
        "score_error_records": 0,
        "frames_carried": 0,
        "frames_missing": 0,
        "media_carried": 0,
        "media_missing": 0,
        "models_available": False,
        "model_source": None,
        "clip_model": CLIP_NAME,
        "low_score_threshold": config.low_score_threshold,
        "max_frames_per_segment": config.max_frames_per_segment,
        "degraded": False,
        "encoding_error": False,
        "file_read_incomplete": False,
        "empty_input": False,
        "conservation_ok": False,
        "conservation_failures": [],
    }


def _apply_conservation(statistics: Dict[str, Any]) -> bool:
    """统计守恒校验：所有分桶必须与总数自洽（不允许静默丢样本）。"""
    checks = {
        "input_equals_valid_plus_bad":
            statistics["input_records"] == statistics["valid_records"] + statistics["bad_lines"],
        "valid_equals_buckets":
            statistics["valid_records"] == (statistics["origin_quarantined"]
                                            + statistics["sample_isolated"]
                                            + statistics["pending_records"]
                                            + statistics["passed_records"]
                                            + statistics["skipped_records"]
                                            + statistics["not_applicable_records"]),
        "output_equals_retained_buckets":
            statistics["output_records"] == (statistics["passed_records"]
                                             + statistics["pending_records"]
                                             + statistics["skipped_records"]
                                             + statistics["not_applicable_records"]),
        "isolated_equals_below":
            statistics["sample_isolated"] == statistics["segment_below_threshold"],
        "scored_equals_pass_plus_below":
            statistics["scored_records"] == (statistics["passed_records"]
                                             + statistics["segment_below_threshold"]),
        "pending_breakdown":
            statistics["pending_records"] == (statistics["no_video_text_pair"]
                                             + statistics["no_frames"]
                                             + statistics["missing_description"]
                                             + statistics["model_unavailable"]
                                             + statistics["score_error_records"]),
    }
    failures = [name for name, ok in checks.items() if not ok]
    statistics["conservation_failures"] = failures
    statistics["conservation_ok"] = not failures
    return not failures


_PENDING_REASON_KEYS = {
    video_text.REASON_NO_VIDEO_TEXT_PAIR: "no_video_text_pair",
    video_text.REASON_NO_FRAMES: "no_frames",
    video_text.REASON_MISSING_DESCRIPTION: "missing_description",
    video_text.REASON_MODEL_UNAVAILABLE: "model_unavailable",
    video_text.REASON_SCORE_ERROR: "score_error_records",
}


# --------------------------------------------------------------------------- #
# 主流程
# --------------------------------------------------------------------------- #

def run(input_file: Optional[str] = None, unique_id: str = "",
        config_path=None, output_root=None,
        overrides: Optional[Dict[str, Any]] = None,
        allow_external_input: bool = True,
        encoder: Any = None) -> Dict[str, Any]:
    """执行一次算子运行；返回本次运行的完整结果描述（含真实统计与血缘）。

    ``allow_external_input``：
    - 默认 ``True``（库/CLI 入口）：允许读取任意本地路径，便于本地复算与 smoke；
    - HTTP 入口显式传 ``False``：``inputFile`` 必须落在当前任务目录内，
      杜绝跨任务读取与目录穿越。
    """
    config = load_config(config_path, overrides)
    value = io_utils.validate_unique_id(unique_id or "")
    root = io_utils.resolve_root(output_root or default_output_dir())
    task_root = io_utils.task_dir(root, value)
    started_at = _now_iso()

    with io_utils.TaskLock(root, value):
        input_path = resolve_input(input_file, task_root, allow_external_input)

        runs_dir = task_root / RUNS_DIR_NAME
        runs_dir.mkdir(parents=True, exist_ok=True)
        run_id = io_utils.next_run_id(runs_dir, "run")
        run_dir = runs_dir / run_id
        run_dir.mkdir(parents=True, exist_ok=False)

        # 日志名带完整运行目录，避免同名 logger 复用旧 FileHandler 导致新运行目录缺日志
        logger = logger_mod.get_logger("op14.%s.%s.%s" % (value, run_id, run_dir),
                                       log_file=run_dir / LOG_NAME)
        statistics = _initial_statistics(config)
        media_roots = build_media_roots(input_path, task_root)

        models: Dict[str, Any] = {"available": False, "reason": None, "source": None,
                                  "identity": None}
        encoder_obj: Optional[Any] = None
        if config.enabled and _needs_video_encoder(input_path, config):
            encoder_obj, models = _prepare_encoder(config, encoder, statistics, logger)
        else:
            models["reason"] = "no_applicable_video_records" if config.enabled else "route_disabled"

        statistics["models_available"] = bool(models["available"])
        statistics["model_source"] = models["source"]

        entrypoint = MAIN_NAME_ENABLED if config.enabled else MAIN_NAME_DISABLED
        main_path = run_dir / entrypoint
        rejects_path = run_dir / REJECTS_NAME
        quarantine_path = run_dir / SAMPLE_QUARANTINE_NAME
        pending_path = run_dir / PENDING_NAME
        not_applicable_path = run_dir / NOT_APPLICABLE_NAME

        logger_mod.log_action(logger, "start_run",
                              "uniqueId=%s run=%s route_enabled=%s 输入=%s"
                              % (value, run_id, bool(config.enabled), input_path.name))

        media_artifacts: List[Dict[str, str]] = []
        status = "success"
        try:
            with io_utils.JsonlWriter(main_path) as main_writer, \
                    io_utils.JsonlWriter(rejects_path) as rejects_writer, \
                    io_utils.JsonlWriter(quarantine_path) as quarantine_writer, \
                    io_utils.JsonlWriter(pending_path) as pending_writer, \
                    io_utils.JsonlWriter(not_applicable_path) as not_applicable_writer:
                media_artifacts, status = _process_lines(
                    input_path=input_path, config=config, statistics=statistics,
                    encoder=encoder_obj, media_roots=media_roots, run_dir=run_dir,
                    main_writer=main_writer, rejects_writer=rejects_writer,
                    quarantine_writer=quarantine_writer, pending_writer=pending_writer,
                    not_applicable_writer=not_applicable_writer,
                    logger=logger)
                statistics["output_records"] = main_writer.count
        finally:
            pass

        statistics["empty_input"] = statistics["input_records"] == 0
        _apply_conservation(statistics)

        finished_at = _now_iso()
        artifacts = [
            {"path": entrypoint, "role": "main"},
            {"path": REJECTS_NAME, "role": "ledger"},
            {"path": SAMPLE_QUARANTINE_NAME, "role": "ledger"},
            {"path": PENDING_NAME, "role": "ledger"},
            {"path": NOT_APPLICABLE_NAME, "role": "ledger"},
            {"path": LOG_NAME, "role": "log"},
        ]
        manifest = transport.build_manifest(entrypoint, artifacts + media_artifacts)
        manifest_path = transport.write_manifest(run_dir, manifest)
        publish_stage_output(run_dir)

        # [v4.2] 血缘输出已停用：不再继承上游血缘（仅保留时间工具 now_iso）
        # prior_lineage: List[Dict[str, Any]] = []
        # prior_lineage = lineage_mod.load_prior_lineage(
        #     [input_path.parent, task_root / INPUT_DIR_NAME, task_root])
        # [v4.2] 血缘输出已停用：不再构建/追加血缘记录、不再写 stage_context.json
        # （代码保留注释存档，不删除；utils/lineage.py 仍保留 now_iso 时间工具）
        # record = lineage_mod.make_lineage(
        #     operator=OPERATOR, version=OPERATOR_VERSION, unique_id=value,
        #     stage_id=STAGE_ID, run_id=run_id, input_file=str(input_path),
        #     output_file=str(main_path), status=status, started_at=started_at,
        #     finished_at=finished_at, config=config_snapshot(config, overrides),
        #     statistics=statistics, artifacts=artifacts + media_artifacts,
        #     formal_operator_number=FORMAL_OPERATOR_NUMBER,
        #     model=models["identity"] if models["available"] else None)
        # lineage_path = run_dir / LINEAGE_NAME
        # lineage_mod.append_lineage(lineage_path, record)
        # stage_lineage = lineage_mod.to_stage_lineage([record])
        # context = lineage_mod.build_stage_context(
        #     STAGE_ID, value, run_id, entrypoint, stage_lineage, prior_lineage)
        # context_path = io_utils.atomic_write_json(run_dir / STAGE_CONTEXT_NAME, context)

        logger_mod.log_action(
            logger, "finish_run",
            "status=%s 通过=%d 隔离=%d 原始隔离=%d 待处理=%d 透传=%d 不适用=%d 坏行=%d 守恒=%s"
            % (status, statistics["passed_records"], statistics["sample_isolated"],
               statistics["origin_quarantined"], statistics["pending_records"],
               statistics["skipped_records"], statistics["not_applicable_records"],
               statistics["bad_lines"], statistics["conservation_ok"]))
        logger_mod.close_logger(logger)

    artifact_paths = [item["path"] for item in artifacts]
    media_paths = [item["path"] for item in media_artifacts]
    pack_paths: List[str] = []
    for path in artifact_paths + media_paths:
        if path not in pack_paths:
            pack_paths.append(path)
    return {
        "status": status,
        "message": _status_message(status, statistics),
        "operator": OPERATOR,
        "operatorVersion": OPERATOR_VERSION,
        "formalOperatorNumber": FORMAL_OPERATOR_NUMBER,
        "stageId": STAGE_ID,
        "uniqueId": value,
        "runId": run_id,
        "stageStart": input_file is None,
        "inputFile": str(input_path),
        "runDir": str(run_dir),
        "entrypoint": entrypoint,
        "outputFile": str(main_path),
        "rejectsFile": str(rejects_path),
        "sampleQuarantineFile": str(quarantine_path),
        "pendingFile": str(pending_path),
        "notApplicableFile": str(not_applicable_path),
        # [v4.2] 血缘输出已停用：lineageFile/contextPath/stageLineage/priorLineage 不再返回
        "manifestPath": str(manifest_path),
        "artifacts": artifacts,
        "mediaArtifacts": media_artifacts,
        "artifactPaths": artifact_paths,
        "mediaPaths": media_paths,
        "packPaths": pack_paths,
        "statistics": statistics,
        "models": models,
        "model": models["identity"] if models["available"] else None,
        "clipModel": CLIP_NAME,
        "startedAt": started_at,
        "finishedAt": finished_at,
    }


def _needs_video_encoder(input_path: Path, config: OperatorConfig) -> bool:
    """Skip model setup only when every parsed record has a known route waiver."""
    try:
        for line in io_utils.iter_decoded_lines(input_path):
            try:
                record = json.loads(line)
            except (ValueError, TypeError):
                continue
            if not isinstance(record, dict):
                continue
            if config.temporal_route_exclusion and video_text.temporal_route_evidence(record) is not None:
                continue
            if video_text.non_video_route_evidence(record) is None:
                return True
    except (UnicodeDecodeError, OSError):
        return True
    return False


def _prepare_encoder(config: OperatorConfig, injected: Any,
                     statistics: Dict[str, Any],
                     logger) -> Tuple[Optional[Any], Dict[str, Any]]:
    """准备编码器：优先复用注入/已登记实例，其次按配置加载；不可用即显式失败。"""
    payload = clip_backend_payload(config)
    signature = backends.model_signature(payload["clip"])
    models: Dict[str, Any] = {"available": False, "reason": None, "source": None,
                              "identity": None}

    encoder_obj = injected
    source = "injected" if injected is not None else None
    if encoder_obj is None:
        encoder_obj = backends.get_registered_encoder(signature)
        if encoder_obj is not None:
            source = "registered"

    if encoder_obj is not None:
        identity = None
        if hasattr(encoder_obj, "model_identity"):
            try:
                identity = encoder_obj.model_identity()
            except Exception:  # noqa: BLE001 - 身份取不到不阻断评分，但显式记录
                identity = None
        models.update(available=True, source=source or "injected", identity=identity)
        logger_mod.log_action(logger, "model_ready",
                              "来源=%s 模型=%s" % (models["source"], CLIP_NAME))
        return encoder_obj, models

    try:
        encoder_obj, reason, identity = backends.build_clip_backend(payload)
    except backends.BackendUnavailable as exc:
        raise ModelUnavailable(
            "CLIP-ViT/B-32 不可用且未允许降级：%s" % exc) from exc

    if encoder_obj is not None:
        models.update(available=True, source="loaded", identity=identity)
        logger_mod.log_action(logger, "model_ready",
                              "来源=loaded 模型=%s 设备=%s"
                              % (CLIP_NAME, (identity or {}).get("device")))
        return encoder_obj, models

    models["reason"] = reason
    if not config.allow_degraded:
        raise ModelUnavailable("CLIP-ViT/B-32 不可用且未允许降级：%s" % reason)
    models["source"] = None
    logger_mod.log_action(logger, "model_unavailable",
                          "显式降级，不产出任何分数；原因=%s" % reason)
    return None, models


def _status_message(status: str, statistics: Dict[str, Any]) -> str:
    if status == "failed":
        return "输入无法解析：有效记录 0 条，异常 %d 行" % statistics["bad_lines"]
    if status == "degraded":
        return "CLIP 不可用，已显式降级：%d 条进入待处理，未产出任何分数" \
            % statistics["pending_records"]
    return "处理完成：通过 %d，隔离 %d，原始隔离 %d，待处理 %d，透传 %d，不适用 %d" % (
        statistics["passed_records"], statistics["sample_isolated"],
        statistics["origin_quarantined"], statistics["pending_records"],
        statistics["skipped_records"], statistics["not_applicable_records"])


def _process_lines(*, input_path: Path, config: OperatorConfig,
                   statistics: Dict[str, Any], encoder: Optional[Any],
                   media_roots: Sequence[Path], run_dir: Path,
                   main_writer, rejects_writer, quarantine_writer, pending_writer,
                   not_applicable_writer, logger) -> Tuple[List[Dict[str, str]], str]:
    """流式处理入口 JSONL；返回 ``(媒体产物清单, 状态)``。"""
    seen_identifiers = set()
    media_artifacts: List[Dict[str, str]] = []
    status = "success"
    line_no = 0

    try:
        for raw in io_utils.iter_decoded_lines(input_path, "utf-8"):
            line_no += 1
            if not raw.strip():
                statistics["blank_lines"] += 1
                continue
            statistics["input_records"] += 1

            try:
                record = json.loads(raw)
            except json.JSONDecodeError:
                statistics["bad_lines"] += 1
                rejects_writer.write(video_text.make_ledger_entry(
                    line=line_no, reason=video_text.REASON_JSON_PARSE_ERROR,
                    label=video_text.LABEL_ORIGIN_UNPARSABLE, action="reject",
                    destination=video_text.DEST_ORIGIN,
                    event_time=_now_iso()))
                continue

            if not isinstance(record, dict):
                statistics["bad_lines"] += 1
                rejects_writer.write(video_text.make_ledger_entry(
                    line=line_no, reason=video_text.REASON_RECORD_NOT_OBJECT,
                    label=video_text.LABEL_ORIGIN_UNPARSABLE, action="reject",
                    destination=video_text.DEST_ORIGIN,
                    event_time=_now_iso()))
                continue

            statistics["valid_records"] += 1
            spec = video_text.resolve_pair(record)
            identifier = spec.get("identifier")
            if not identifier:
                statistics["origin_quarantined"] += 1
                rejects_writer.write(video_text.make_ledger_entry(
                    line=line_no, reason="missing_field: sample_id",
                    label=video_text.LABEL_ORIGIN_MISSING_FIELD, action="reject",
                    destination=video_text.DEST_ORIGIN,
                    event_time=_now_iso(),
                    sample_id=spec.get("sample_id"), pair_id=spec.get("pair_id"),
                    batch_id=spec.get("batch_id")))
                continue
            if identifier in seen_identifiers:
                statistics["origin_quarantined"] += 1
                rejects_writer.write(video_text.make_ledger_entry(
                    line=line_no, reason="duplicate_identifier",
                    label=video_text.LABEL_ORIGIN_MISSING_FIELD, action="reject",
                    destination=video_text.DEST_ORIGIN,
                    event_time=_now_iso(),
                    sample_id=spec.get("sample_id"), pair_id=spec.get("pair_id"),
                    batch_id=spec.get("batch_id")))
                continue
            seen_identifiers.add(identifier)

            if not config.enabled:
                # 关闭态也回填本次配置生效值（阈值/帧预算），与开启态输出口径一致
                node = video_text.skipped_node(config)
                main_writer.write(_attach_node(record, node))
                statistics["skipped_records"] += 1
                continue

            if spec.get("frames"):
                carried, missing, carried_paths = carry_frames(
                    run_dir, media_roots, [item["path"] for item in spec["frames"]])
                statistics["frames_carried"] += carried
                statistics["frames_missing"] += missing
                statistics["media_carried"] += carried
                statistics["media_missing"] += missing
                for path in carried_paths:
                    if not any(item["path"] == path for item in media_artifacts):
                        media_artifacts.append({"path": path, "role": "media"})

            outcome = video_text.evaluate_record(
                spec, line_no, media_roots=media_roots, config=config,
                encoder=encoder, event_time=_now_iso())
            action = outcome["action"]

            if action == video_text.ACTION_PASS:
                main_writer.write(outcome["record"])
                statistics["passed_records"] += 1
                statistics["scored_records"] += 1
            elif action == video_text.ACTION_ISOLATE:
                quarantine_writer.write(outcome["entry"])
                statistics["sample_isolated"] += 1
                statistics["segment_below_threshold"] += 1
                statistics["scored_records"] += 1
            elif action == video_text.ACTION_NOT_APPLICABLE:
                # 不评分、不隔离：主流保留完整记录给 #15，同时登记不适用台账。
                main_writer.write(_attach_node(record, outcome["node"]))
                not_applicable_writer.write(outcome["entry"])
                statistics["not_applicable_records"] += 1
                if outcome["reason"] == video_text.REASON_TEMPORAL_ROUTE:
                    statistics["temporal_route_not_applicable"] += 1
                elif outcome["reason"] == video_text.REASON_NON_VIDEO_ROUTE:
                    statistics["non_video_route_not_applicable"] += 1
            elif action == video_text.ACTION_ORIGIN:
                rejects_writer.write(outcome["entry"])
                statistics["origin_quarantined"] += 1
            else:
                node = outcome["node"]
                statistics["pending_records"] += 1
                key = _PENDING_REASON_KEYS.get(outcome["reason"])
                if key:
                    statistics[key] += 1
                if outcome.get("degraded"):
                    statistics["degraded"] = True
                pending_writer.write(outcome["entry"])
                main_writer.write(_attach_node(record, node))
    except UnicodeDecodeError:
        statistics["input_records"] += 1
        statistics["bad_lines"] += 1
        statistics["encoding_error"] = True
        statistics["file_read_incomplete"] = True
        rejects_writer.write(video_text.make_ledger_entry(
            line=line_no + 1, reason=video_text.REASON_ENCODING_ERROR,
            label=video_text.LABEL_ORIGIN_ENCODING, action="reject",
            destination=video_text.DEST_ORIGIN, event_time=_now_iso()))
        logger_mod.log_action(logger, "encoding_error", "文件不是严格 UTF-8，已停止读取")
    except OSError as exc:
        statistics["file_read_incomplete"] = True
        logger_mod.log_action(logger, "read_error", "读取输入失败：%s" % type(exc).__name__)

    if statistics["valid_records"] == 0:
        status = "failed" if (statistics["bad_lines"] or statistics["encoding_error"]
                             or statistics["file_read_incomplete"]) else "success"
    elif statistics["model_unavailable"] > 0:
        status = "degraded"

    logger_mod.log_action(
        logger, "read_input",
        "读入记录=%d 有效=%d 坏行=%d 空行=%d" % (
            statistics["input_records"], statistics["valid_records"],
            statistics["bad_lines"], statistics["blank_lines"]))
    return media_artifacts, status


def _attach_node(record: Dict[str, Any], node: Dict[str, Any]) -> Dict[str, Any]:
    """把算子节点挂到记录上：``processing["14"]`` + 顶层 ``video_text_score``。"""
    output = dict(record)
    processing = dict(output.get("processing") or {})
    processing["14"] = node
    output["processing"] = processing
    output["video_text_score"] = node
    output["video_text_score_value"] = node.get("video_text_score")
    return output


# --------------------------------------------------------------------------- #
# 阶段返回形态（供 app / processor 组装）
# --------------------------------------------------------------------------- #

# [v4.2] 血缘输出已停用：下列阶段形态函数不再被 app.py 调用，保留存档。
def build_intermediate_lineage(result: Dict[str, Any]) -> Dict[str, Any]:
    """[v4.2 已停用] 中间节点血缘；响应统一改为 outputFile + result。"""
    return {}


def build_stage_payload(result: Dict[str, Any]) -> List[Dict[str, Any]]:
    """[v4.2 已停用] 阶段末完整阶段血缘；响应统一改为 outputFile + result。"""
    return []


def stage_file_object(result: Dict[str, Any]) -> Dict[str, Any]:
    """``isLastOperator=true``：最终文件内容对象（含入口、清单、台账与帧）。

    ``packPaths`` 已去重（含台账与抽帧），这里只追加传输层清单，避免重复成员。
    """
    paths = list(result.get("packPaths") or [])
    if transport.MANIFEST_NAME not in paths:
        paths.append(transport.MANIFEST_NAME)
    return transport.pack_file_object(result["runDir"], result["entrypoint"], paths)


def cli_data_shape(status: str, is_last: bool) -> Dict[str, str]:
    """CLI 的 ``data`` 结构声明（与 HTTP 完全一致的返回口径）。"""
    if status == "failed":
        return {}
    # [v4.2] 血缘输出已停用：两种形态统一 outputFile + result
    return {"outputFile": "file-object", "result": "object"}


def resolve_overrides(config_obj: OperatorConfig, overrides: Optional[Dict[str, Any]]
                      ) -> Optional[Dict[str, Any]]:
    return overrides


# --------------------------------------------------------------------------- #
# CLI
# --------------------------------------------------------------------------- #

def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="processor.py",
        description="算子 14 评分视文语义一致性（S3，表1 #14）：CLIP 视文余弦评分与语义隔离")
    parser.add_argument("--input", default=None,
                       help="输入 JSONL（阶段内传上一节点 outputFile；阶段首可省略）")
    parser.add_argument("--unique-id", default="task_file", help="任务隔离目录名")
    parser.add_argument("--config", default=None, help="配置文件路径")
    parser.add_argument("--output-root", default=None, help="输出根目录")
    parser.add_argument("--is-last-operator", action="store_true",
                       help="是否阶段末算子（true 返回 outputFile + 完整阶段血缘）")
    parser.add_argument("--enabled", action="store_true", help="覆盖配置：启用视文评分路线")
    parser.add_argument("--disable", action="store_true", help="覆盖配置：关闭视文评分路线")
    parser.add_argument("--low-score-threshold", type=float, default=None,
                       help="覆盖配置：低分阈值（0.20–0.40）")
    parser.add_argument("--max-frames", type=int, default=None,
                       help="覆盖配置：整段视频合计最多帧数（1–8）")
    parser.add_argument("--clip-weights", default=None, help="覆盖配置：本地 CLIP 权重路径")
    parser.add_argument("--allow-degraded", action="store_true",
                       help="覆盖配置：允许显式降级（不产出任何分数）")
    return parser


def _overrides_from_args(args: argparse.Namespace) -> Dict[str, Any]:
    overrides: Dict[str, Any] = {}
    if args.enabled:
        overrides["enabled"] = True
    if args.disable:
        overrides["enabled"] = False
    if args.low_score_threshold is not None:
        overrides["low_score_threshold"] = args.low_score_threshold
    if args.max_frames is not None:
        overrides["max_frames_per_segment"] = args.max_frames
    if args.allow_degraded:
        overrides["allow_degraded"] = True
    if args.clip_weights:
        overrides["clip"] = {"weights": args.clip_weights}
    return overrides


def _prepare_stdout() -> None:
    """CLI 的 stdout 固定为 UTF-8，保证输出的 JSON 在任何平台都合法。

    Windows 控制台若是 GBK 代码页，显示中文可能乱码（可 ``chcp 65001``）；
    但管道/文件读取方一定拿到合法 UTF-8 JSON。
    """
    reconfigure = getattr(sys.stdout, "reconfigure", None)
    if callable(reconfigure):
        try:
            reconfigure(encoding="utf-8", errors="replace")
        except (ValueError, OSError, LookupError):
            pass


def main(argv: Optional[Sequence[str]] = None) -> int:
    _prepare_stdout()
    args = build_parser().parse_args(argv)
    overrides = _overrides_from_args(args) or None
    try:
        result = run(input_file=args.input, unique_id=args.unique_id,
                     config_path=args.config, output_root=args.output_root,
                     overrides=overrides, allow_external_input=True)
    except (ConfigError, InputError, io_utils.TaskBusyError, DependencyError) as exc:
        payload = {"status": "failed", "error": type(exc).__name__, "message": str(exc),
                   "data": {}}
        print(json.dumps(payload, ensure_ascii=False, indent=2))
        return 2

    # [v4.2] 统一返回 outputFile（crossmodal-files-v1），不再返回 lineageFile
    data = {"outputFile": stage_file_object(result),
            "result": {
                "uniqueId": result["uniqueId"], "runId": result["runId"],
                "status": result["status"], "statistics": result["statistics"],
            }}

    payload = {
        "status": result["status"],
        "message": result["message"],
        "stageId": result["stageId"],
        "uniqueId": result["uniqueId"],
        "runId": result["runId"],
        "runDir": result["runDir"],
        "entrypoint": result["entrypoint"],
        "statistics": result["statistics"],
        "models": result["models"],
        "data": data,
        "dataShape": cli_data_shape(result["status"], args.is_last_operator),
    }
    if result["status"] in ("success", "degraded"):
        payload["outputFile"] = result["outputFile"]
        payload["rejectsFile"] = result["rejectsFile"]
        payload["sampleQuarantineFile"] = result["sampleQuarantineFile"]
        payload["pendingFile"] = result["pendingFile"]
    print(json.dumps(payload, ensure_ascii=False, indent=2))
    return 0 if result["status"] in ("success", "degraded") else 1


if __name__ == "__main__":  # pragma: no cover
    sys.exit(main())
