"""算子 #17 入口：``run()`` 业务编排 + CLI + 配置快照 + 真实统计 + 血缘。

表1 #17 冻结口径：
- 默认关闭（``enabled=false``）：不加载模型、不产生任何框或时间戳、不隔离，样本原样透传；
- 开启后：图像仅预填「YOLOv8n 官方通用类别 ∩ 与描述可对上」的框，专用设备留给人工；
  音视频沿用转写起止时间预填；
- 只有人工确认的 bbox/时间段可交付；未确认只能 ``pending_manual_review``；
- 标注不完整 → 样本隔离库（``样本-语义不通``）；原始文件/字段/编码问题 → 原始隔离区；
- 模型不可用时只允许 ``degraded``（显式降级、零框）或显式失败，绝不伪造框或分数。
"""
from __future__ import annotations

import argparse
import json
import os
import shutil
import sys
from collections import Counter
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

from typing import Any, Dict, Iterable, List, Optional, Tuple

from utils import detectors, io as io_utils
from utils import lineage as lineage_utils
# [v4.2] 血缘输出已停用：lineage_utils 仅保留 now_iso（时间工具）使用；
# build_node/load_prior_lineage/load_stage_lineage/build_stage_context 等血缘调用已全部注释。
from utils import localization as loc
from utils import logger as logger_utils
from utils import transport
from utils.schemas import (DETECTOR_MODEL, FORMAL_OPERATOR_NUMBER, OPERATOR_NAME,
                           OPERATOR_NAME_ZH, STAGE_ID, ConfigError, load_config)

OPERATOR_DIR = Path(__file__).resolve().parent
#: 输出目录布局：output/<uniqueId>/<operator>/<runId>/
OPERATOR_SLUG = "17_local_to_whole_annotation"

MANIFEST_NAME = transport.MANIFEST_NAME
PROGRESS_EVERY = 1000


class InputError(ValueError):
    """输入不可用：入口缺失、路径越界、任务不存在等。"""


class DependencyError(RuntimeError):
    """外部依赖（检测模型/权重）不可用且不允许降级。"""


def default_output_dir() -> Path:
    return Path(os.environ.get("OUTPUT_DIR") or (OPERATOR_DIR / "output"))


def _now() -> str:
    return lineage_utils.now_iso()


def _dedupe(paths: Iterable[Path]) -> List[Path]:
    seen: List[Path] = []
    for path in paths:
        resolved = io_utils.resolved_absolute(path)
        if resolved not in seen:
            seen.append(resolved)
    return seen


def _media_roots(entry: Path, task_dir: Path) -> List[Path]:
    """素材搜索根：入口同级 → 入口上级 → 任务目录 → 任务 input。"""
    return _dedupe([
        entry.parent,
        entry.parent.parent,
        task_dir,
        task_dir / io_utils.INPUT_DIR_NAME,
    ])


# --------------------------------------------------------------------------- #
# 结果结构
# --------------------------------------------------------------------------- #

# [v4.2] 血缘输出已停用：中间/阶段血缘形态与上游血缘装载函数整段注释存档，
# 不再被 app.py / run() 调用（血缘模块文件已加停用声明）。

# def build_intermediate_lineage(result: Dict[str, Any]) -> Dict[str, Any]:
#     """[v4.2 已停用] 阶段中间形态血缘节点（保留存档，不再被接口层调用）。"""
#     return lineage_utils.intermediate_node(result["node"])
#
#
# def build_stage_payload(result: Dict[str, Any]) -> List[Dict[str, Any]]:
#     """[v4.2 已停用] 阶段末形态血缘数组（保留存档，不再被接口层调用）。"""
#     stage = result.get("stageLineage") or [result["node"]]
#     return lineage_utils.stage_nodes(stage)
#
#
# def _inherit_lineage(stage_id: str, unique_id: str, search_dirs: Iterable[Any], *,
#                      prior_lineage: Optional[List[Dict[str, Any]]] = None,
#                      stage_lineage: Optional[List[Dict[str, Any]]] = None
#                      ) -> Tuple[List[Dict[str, Any]], List[Dict[str, Any]]]:
#     """确定阶段内上游血缘（v4.2 停用存档）。"""
#     prior = [dict(item) for item in (prior_lineage or []) if isinstance(item, dict)]
#     stage = [dict(item) for item in (stage_lineage or []) if isinstance(item, dict)]
#     if not prior:
#         prior = [dict(item) for item in lineage_utils.load_prior_lineage(search_dirs)
#                  if isinstance(item, dict)]
#     if not stage:
#         stage = [dict(item) for item in
#                  lineage_utils.load_stage_lineage(search_dirs, stage_id, unique_id)
#                  if isinstance(item, dict)]
#     return prior, stage


def stage_file_object(result: Dict[str, Any]) -> Dict[str, Any]:
    """阶段文件内容对象：入口 + 旁路清单 + manifest + 搬运的媒体。

    [v4.2] 血缘/阶段归档输出已停用：不再产出 ``lineage.json`` / ``stage_context.json``；
    ``run.log`` 是本地控制文件，按契约 §4 不塞进 ``files``。
    """
    members = [item["path"] if isinstance(item, dict) else str(item)
               for item in result["artifacts"]]
    if MANIFEST_NAME not in members:
        members.append(MANIFEST_NAME)
    return transport.pack_file_object(
        result["runDir"], result["entrypoint"], members,
        max_response_bytes=(
            result["statistics"].get("max_response_bytes")
            or transport.DEFAULT_MAX_RESPONSE_BYTES))


# --------------------------------------------------------------------------- #
# 主流程
# --------------------------------------------------------------------------- #

def run(input_file: Optional[str] = None, unique_id: str = "",
        config_path: Optional[str] = None, output_root: Optional[str] = None,
        overrides: Optional[Dict[str, Any]] = None,
        detector: Optional[Any] = None,
        allow_external_input: bool = False,
        logger: Optional[logger_utils.OperatorLogger] = None,
        # [v4.2] 血缘输出已停用：prior_lineage/stage_lineage 为兼容参数，内部不再继承/使用
        prior_lineage: Optional[List[Dict[str, Any]]] = None,
        stage_lineage: Optional[List[Dict[str, Any]]] = None) -> Dict[str, Any]:
    """执行一次算子运行，返回产物路径与真实统计（[v4.2] 血缘输出已停用，不返回血缘节点）。"""
    unique_id = io_utils.validate_unique_id(unique_id)
    config = load_config(config_path, overrides)

    output_root_path = Path(output_root) if output_root else default_output_dir()
    task_dir = io_utils.task_root(output_root_path, unique_id)
    input_dir = task_dir / io_utils.INPUT_DIR_NAME

    with io_utils.TaskLock(output_root_path, unique_id):
        # 先校验入口：输入非法时不留下空 run 目录与半成品产物
        entry = _resolve_entry(input_file, task_dir, input_dir, allow_external_input)
        media_roots = _media_roots(entry, task_dir)

        run_dir = task_dir / OPERATOR_SLUG / io_utils.next_run_id(task_dir / OPERATOR_SLUG)
        run_dir.mkdir(parents=True, exist_ok=True)
        run_id = run_dir.name
        started_at = _now()
        log_path = run_dir / loc.LOG_NAME
        log = logger or logger_utils.OperatorLogger(
            loc.OPERATOR, loc.OPERATOR_VERSION, unique_id, run_id,
            log_path=log_path)

        # [v4.2] 血缘输出已停用：不再继承/读取上游血缘（代码保留注释存档）
        # prior_nodes, inherited_nodes = _inherit_lineage(
        #     config.stage_id, unique_id, [input_dir, entry.parent, task_dir],
        #     prior_lineage=prior_lineage, stage_lineage=stage_lineage)
        prior_nodes: List[Dict[str, Any]] = []
        inherited_nodes: List[Dict[str, Any]] = []
        log.info("run_start", entrypoint=entry.name, enabled=bool(config.enabled),
                 mediaRoots=len(media_roots), stageId=config.stage_id)

        detector_obj, detector_state = _prepare_detector(config, detector, log)
        stats, artifacts = _process(entry, run_dir, config, detector_obj,
                                    detector_state, media_roots, unique_id,
                                    run_id, log)

        finished_at = _now()
        main_name = (loc.MAIN_NAME_ENABLED if config.enabled
                     else loc.MAIN_NAME_DISABLED)
        output_file = str(run_dir / main_name)

        model_identity = detector_state.get("identity")
        statistics = dict(stats)
        statistics.update({
            "enabled": bool(config.enabled),
            "entrypoint": main_name,
            "detector_backend": config.detector_backend,
            "model": model_identity,
            "allow_degraded": bool(config.allow_degraded),
            "require_whole_aligned": bool(config.require_whole_aligned),
            "max_response_bytes": config.transport.max_response_bytes,
            "pending_sidecar_is_copy": True,
            "conservation": {
                "input_records": stats["input_records"],
                "valid_records": stats["valid_records"],
                "bad_lines": stats["bad_lines"],
                "routes": {
                    "confirmed": stats["confirmed_records"],
                    "pending": stats["pending_records"],
                    "incomplete": stats["incomplete_records"],
                    "not_applicable": stats["not_applicable_records"],
                    "origin_quarantined": stats["origin_quarantined"],
                    "skipped": stats["skipped_records"],
                },
                "checked": [
                    "input_records == valid_records + bad_lines",
                    "valid_records == sum(routes)",
                    "rejects_entries == bad_lines + origin_quarantined",
                    "main_records == kept routes",
                    "pending_sidecar_entries == pending_records",
                ],
            },
        })

        # [v4.2] 血缘输出已停用：不再构建血缘节点、不再写 lineage.json /
        # stage_context.json（代码保留注释存档，不删除）。
        # node = lineage_utils.build_node(
        #     operator=loc.OPERATOR, operator_version=loc.OPERATOR_VERSION,
        #     formal_operator_number=FORMAL_OPERATOR_NUMBER, stage_id=config.stage_id,
        #     unique_id=unique_id, run_id=run_id, operator_dir=OPERATOR_DIR,
        #     entrypoint=main_name, input_file=str(entry), output_file=output_file,
        #     status=statistics["status"], started_at=started_at,
        #     finished_at=finished_at, statistics=statistics, artifacts=artifacts,
        #     run_dir=run_dir, task_dir=task_dir,
        #     model=model_identity,
        #     config=lineage_utils.config_snapshot(config, overrides,
        #                                          config_path=str(config_path)
        #                                          if config_path else None),
        #     reason=statistics.get("status_reason"),
        #     labels={"operator_name_zh": OPERATOR_NAME_ZH,
        #             "stage_label": "S3 数据加工",
        #             "source_reference": loc.SOURCE_REFERENCE})

        # [v4.2] 血缘停用后阶段统计只登记本算子自身
        stage_start = True
        statistics["stage_start"] = stage_start
        statistics["stage_operators"] = [loc.OPERATOR]
        statistics["prior_operators"] = []

        # current_stage = list(inherited_nodes) + [node]
        # lineage_path = run_dir / loc.LINEAGE_NAME
        # io_utils.atomic_write_json(lineage_path, current_stage)
        # stage_context_path = run_dir / loc.STAGE_CONTEXT_NAME
        # io_utils.atomic_write_json(stage_context_path, lineage_utils.build_stage_context(
        #     stage_id=config.stage_id, unique_id=unique_id, run_id=run_id,
        #     entrypoint=main_name, output_file=output_file,
        #     status=statistics["status"], stage_lineage=current_stage,
        #     prior_lineage=prior_nodes, statistics=statistics,
        #     operator=loc.OPERATOR, model=model_identity))

        manifest = transport.build_manifest(main_name, artifacts)
        transport.write_manifest(run_dir, manifest)
        publish_stage_output(run_dir)

        # run.log 是本地控制文件（契约 §4）：
        # 只在本目录登记与留存，不塞进阶段末 crossmodal-files-v1 文件内容对象。
        # [v4.2] 血缘文件已停用：不再登记 lineage.json / stage_context.json 控制文件
        control_files = [
            {"path": loc.LOG_NAME, "role": "log"},
            {"path": MANIFEST_NAME, "role": "sidecar"},
        ]

        log.info("run_end", status=statistics["status"],
                 main=statistics["main_records"],
                 confirmed=statistics["confirmed_records"],
                 pending=statistics["pending_records"],
                 incomplete=statistics["incomplete_records"],
                 rejects=statistics["rejects_entries"],
                 conservationOk=statistics["conservation_ok"])
        if logger is None:
            log.close()

        return {
            "uniqueId": unique_id,
            "runId": run_id,
            "taskDir": str(task_dir),
            "inputDir": str(input_dir),
            "inputFile": str(entry),
            "runDir": str(run_dir),
            "operator": loc.OPERATOR,
            "operatorVersion": loc.OPERATOR_VERSION,
            "formalOperatorNumber": FORMAL_OPERATOR_NUMBER,
            "stageId": config.stage_id,
            "entrypoint": main_name,
            "outputFile": output_file,
            "mainFile": str(run_dir / main_name),
            "pendingFile": str(run_dir / loc.PENDING_NAME),
            "sampleQuarantineFile": str(run_dir / loc.SAMPLE_QUARANTINE_NAME),
            "rejectsFile": str(run_dir / loc.REJECTS_NAME),
            # [v4.2] 血缘输出已停用：不再回传 lineageFile/stageContextFile/
            # stageLineage/priorLineage/node 键
            "logFile": str(log_path),
            "manifestFile": str(run_dir / MANIFEST_NAME),
            "artifacts": artifacts,
            "controlFiles": control_files,
            "stageStart": stage_start,
            "statistics": statistics,
            "status": statistics["status"],
            "model": model_identity,
            "startedAt": started_at,
            "finishedAt": finished_at,
        }


# --------------------------------------------------------------------------- #
# 入口解析
# --------------------------------------------------------------------------- #

def _resolve_entry(input_file: Optional[str], task_dir: Path, input_dir: Path,
                   allow_external_input: bool) -> Path:
    if input_file:
        if allow_external_input:
            candidate = io_utils.resolved_absolute(input_file)
            if not candidate.is_file():
                raise InputError("input file does not exist: %s" % input_file)
            return candidate
        try:
            return io_utils.resolve_in_task(task_dir, input_file)
        except ValueError as exc:
            raise InputError(str(exc)) from exc
    if not input_dir.is_dir():
        raise InputError("task %s has no uploaded input directory" % task_dir.name)
    try:
        entry, _name = transport.resolve_entrypoint(input_dir)
    except transport.TransportError as exc:
        raise InputError(str(exc)) from exc
    return entry


def _prepare_detector(config, injected, log) -> Tuple[Optional[Any], Dict[str, Any]]:
    """只在开启路线构建检测后端；关闭时不加载。"""
    if not config.enabled:
        return None, {"available": False, "degraded": False,
                      "reason": "route_disabled", "identity": None}
    if injected is not None:
        identity = injected.model_identity()
        log.info("detector_ready", source="injected", degraded=False)
        return injected, {"available": True, "degraded": False, "reason": None,
                          "identity": identity}
    try:
        detector_obj, reason, identity = detectors.build_detector(config)
    except detectors.DetectorUnavailable as exc:
        log.error("detector_unavailable", reason=str(exc))
        raise DependencyError(str(exc)) from exc

    if detector_obj is not None:
        log.info("detector_ready", source="local_weights", degraded=False,
                 device=identity.get("device"))
        return detector_obj, {"available": True, "degraded": False, "reason": None,
                              "identity": identity}

    disabled = reason == detectors.REASON_DISABLED
    degraded = not disabled
    log.warning("detector_degraded", reason=reason, disabled=disabled)
    return None, {"available": False, "degraded": degraded, "reason": reason,
                  "identity": None}


# --------------------------------------------------------------------------- #
# 逐条处理
# --------------------------------------------------------------------------- #

def _new_stats() -> Counter:
    return Counter({
        "input_records": 0, "blank_lines": 0, "valid_records": 0, "bad_lines": 0,
        "confirmed_records": 0, "pending_records": 0, "incomplete_records": 0,
        "not_applicable_records": 0, "skipped_records": 0, "origin_quarantined": 0,
        "rejects_entries": 0, "main_records": 0, "sample_quarantine_entries": 0,
        "pending_sidecar_entries": 0,
        "boxes_prefilled": 0, "boxes_confirmed": 0, "boxes_dropped_unmatched": 0,
        "time_ranges_prefilled": 0, "time_ranges_confirmed": 0,
        "images_processed": 0, "images_unreadable": 0, "images_over_limit": 0,
        "frames_processed": 0, "frames_missing": 0, "frames_over_limit": 0,
        "records_with_frames": 0,
        "records_with_image": 0, "records_with_av": 0,
        "media_carried": 0, "media_missing": 0, "media_path_rejected": 0,
    })


def _origin_quarantine(recorder, stats: Counter, log, *, line: int, reason: str,
                       label: str, record: Dict[str, Any], unique_id: str,
                       run_id: str, event_time: str) -> None:
    stats["origin_quarantined"] += 1
    recorder.rejects.write(loc.make_ledger_entry(
        line=line, reason=reason, label=label, action=loc.ACTION_ISOLATE,
        destination=loc.DEST_ORIGIN_QUARANTINE, event_time=event_time,
        sample_id=loc.record_sample_id(record), record=record, status="rejected",
        unique_id=unique_id, run_id=run_id))
    log.warning("origin_quarantined", line=line, reason=reason, label=label,
                count=stats["origin_quarantined"])


def _carry_media(record: Dict[str, Any], media_roots: List[Path], run_dir: Path,
                 carried: Dict[str, str], stats: Counter) -> None:
    """把记录引用的媒体/侧车原样搬运到本次 run 目录（同名去重，不重复计数）。"""
    for reference in loc.collect_media_refs(record):
        try:
            relative = io_utils.normalize_relative_path(reference)
        except io_utils.UnsupportedMediaPath:
            stats["media_path_rejected"] += 1
            continue
        if relative in carried:
            continue
        located = io_utils.resolve_media(media_roots, relative)
        if located is None:
            stats["media_missing"] += 1
            continue
        try:
            io_utils.atomic_copy(located, run_dir / relative)
        except OSError:
            stats["media_missing"] += 1
            continue
        carried[relative] = located.name
        stats["media_carried"] += 1


def _resolve_images(spec: Dict[str, Any], media_roots: List[Path], config,
                    stats: Counter, log, *, line: int) -> Tuple[Optional[List[Path]], Optional[str]]:
    """解析并校验图像：返回 (图片路径列表, 原始隔离原因)。"""
    if spec["missing_image_reference"]:
        return None, loc.REASON_IMAGE_MISSING
    resolved: List[Path] = []
    for reference in spec["images"]:
        located = io_utils.resolve_media(media_roots, reference)
        if located is None:
            return None, loc.REASON_IMAGE_MISSING
        if not detectors.image_is_readable(located):
            stats["images_unreadable"] += 1
            return None, loc.REASON_IMAGE_UNREADABLE
        resolved.append(located)
    limit = int(config.max_images_per_record)
    if len(resolved) > limit:
        stats["images_over_limit"] += len(resolved) - limit
        log.warning("images_over_limit", line=line, limit=limit,
                    dropped=len(resolved) - limit)
        resolved = resolved[:limit]
    return resolved, None


def _detect_frames(spec: Dict[str, Any], media_roots: List[Path], config,
                   detector_obj, detector_state: Dict[str, Any], stats: Counter,
                   log, *, line: int) -> Optional[List[Dict[str, Any]]]:
    """无图视频：对 #6 已抽好的关键帧做通用类别检测，框带 ``frame_path`` 供 #18 裁图。

    - 只搬用帧文件与时间戳，**不改写** ``video_timestamp_source``：时间段仍来自转写/
      视觉索引，帧只作为检测证据；
    - 帧文件缺失/不可解码只计数（``frames_missing``），不隔离整条记录；
    - 检测后端不可用时返回 ``None``（零框、显式降级），绝不伪造框。
    """
    if not detector_state.get("available"):
        return None
    frames = list(spec.get("frames") or [])
    limit = int(config.max_frames_per_record)
    if len(frames) > limit:
        stats["frames_over_limit"] += len(frames) - limit
        log.warning("frames_over_limit", line=line, limit=limit,
                    dropped=len(frames) - limit)
        frames = frames[:limit]
    boxes: List[Dict[str, Any]] = []
    for frame in frames:
        located = io_utils.resolve_media(media_roots, frame["path"])
        if located is None or not detectors.image_is_readable(located):
            stats["frames_missing"] += 1
            continue
        try:
            found = detector_obj.detect(
                str(located), conf_threshold=config.detector.conf_threshold,
                iou_threshold=config.detector.iou_threshold,
                max_boxes=config.detector.max_boxes,
                image_size=config.detector.image_size)
        except Exception as exc:  # noqa: BLE001 - 推理失败必须显式失败
            raise DependencyError("detector failed on frame %s: %s"
                                  % (located.name, str(exc)[:160])) from exc
        stats["frames_processed"] += 1
        for item in found:
            enriched = dict(item)
            enriched["image_ref"] = located.name
            enriched["frame_path"] = frame["path"]
            enriched["frame_timestamp"] = frame.get("timestamp")
            enriched["frame_index"] = frame.get("frame_index")
            boxes.append(enriched)
    return boxes


def _process(entry: Path, run_dir: Path, config, detector_obj, detector_state,
             media_roots: List[Path], unique_id: str, run_id: str,
             log: logger_utils.OperatorLogger) -> Tuple[Dict[str, Any], List[Dict[str, str]]]:
    stats = _new_stats()
    reasons: Counter = Counter()
    carried: Dict[str, str] = {}
    main_name = loc.MAIN_NAME_ENABLED if config.enabled else loc.MAIN_NAME_DISABLED
    recorder = io_utils.OutcomeRecorder(
        run_dir, main_name=main_name, pending_name=loc.PENDING_NAME,
        sample_quarantine_name=loc.SAMPLE_QUARANTINE_NAME,
        rejects_name=loc.REJECTS_NAME)

    encoding_error = False
    file_read_incomplete = False
    line = 0
    event_time = _now()

    try:
        for text in io_utils.iter_decoded_lines(entry):
            line += 1
            if not text.strip():
                stats["blank_lines"] += 1
                continue
            stats["input_records"] += 1
            if stats["input_records"] % PROGRESS_EVERY == 0:
                log.info("progress", lines=stats["input_records"],
                         main=stats["main_records"], rejects=stats["rejects_entries"])

            try:
                record = json.loads(text)
            except (json.JSONDecodeError, ValueError) as exc:
                stats["bad_lines"] += 1
                reasons[loc.REASON_JSON_PARSE_ERROR] += 1
                recorder.rejects.write(loc.make_ledger_entry(
                    line=line, reason=loc.REASON_JSON_PARSE_ERROR,
                    label=loc.LABEL_ORIGIN_UNPARSABLE, action=loc.ACTION_ISOLATE,
                    destination=loc.DEST_ORIGIN_QUARANTINE, event_time=event_time,
                    record=None, status="rejected", unique_id=unique_id,
                    run_id=run_id, extra={"detail": str(exc)[:160]}))
                continue

            if not isinstance(record, dict):
                stats["bad_lines"] += 1
                reasons[loc.REASON_RECORD_NOT_OBJECT] += 1
                recorder.rejects.write(loc.make_ledger_entry(
                    line=line, reason=loc.REASON_RECORD_NOT_OBJECT,
                    label=loc.LABEL_ORIGIN_UNPARSABLE, action=loc.ACTION_ISOLATE,
                    destination=loc.DEST_ORIGIN_QUARANTINE, event_time=event_time,
                    record=None, status="rejected", unique_id=unique_id,
                    run_id=run_id, extra={"json_type": type(record).__name__}))
                continue

            stats["valid_records"] += 1
            _handle_record(record, line=line, run_dir=run_dir, config=config,
                           detector_obj=detector_obj, detector_state=detector_state,
                           media_roots=media_roots, stats=stats, reasons=reasons,
                           recorder=recorder, carried=carried, log=log,
                           unique_id=unique_id, run_id=run_id,
                           event_time=event_time)
    except UnicodeDecodeError as exc:
        encoding_error = True
        file_read_incomplete = True
        log.error("input_encoding_error", line=line, detail=str(exc)[:120])
    except io_utils.InputTooLargeError as exc:
        file_read_incomplete = True
        log.error("input_line_too_large", line=line, detail=str(exc)[:120])
    finally:
        counts = recorder.close()

    stats["rejects_entries"] = counts["rejects"]
    stats["main_records"] = counts["main"]
    stats["pending_sidecar_entries"] = counts["pending"]
    stats["sample_quarantine_entries"] = counts["sample_quarantine"]

    detector_degraded = bool(config.enabled and detector_state.get("degraded"))
    if encoding_error and stats["valid_records"] == 0:
        status, status_reason = "failed", "input_encoding_error"
    elif detector_degraded:
        status, status_reason = "degraded", detector_state.get("reason")
    elif (stats["bad_lines"] or stats["origin_quarantined"]
          or stats["incomplete_records"]):
        status, status_reason = "partial", "records_need_review"
    else:
        status, status_reason = "success", None

    stats["status"] = status
    stats["status_reason"] = status_reason
    stats["empty_input"] = (
        stats["input_records"] == 0 and not encoding_error and not file_read_incomplete)
    stats["encoding_error"] = encoding_error
    stats["file_read_incomplete"] = file_read_incomplete
    stats["detector_available"] = bool(detector_state.get("available"))
    stats["detector_degraded"] = detector_degraded
    stats["detector_disabled"] = detector_state.get("reason") == detectors.REASON_DISABLED
    stats["detector_reason"] = detector_state.get("reason")
    stats["reasons"] = dict(reasons)
    stats["main_name"] = main_name
    stats["conservation_ok"] = _check_conservation(stats, config)

    artifacts: List[Dict[str, str]] = [
        {"path": main_name, "role": "main"},
        {"path": loc.PENDING_NAME, "role": "pending"},
        {"path": loc.SAMPLE_QUARANTINE_NAME, "role": "sample_quarantine"},
        {"path": loc.REJECTS_NAME, "role": "review"},
    ]
    for relative in sorted(carried):
        artifacts.append({"path": relative, "role": "media"})

    return dict(stats), artifacts


def _handle_record(record: Dict[str, Any], *, line: int, run_dir: Path, config,
                   detector_obj, detector_state, media_roots: List[Path],
                   stats: Counter, reasons: Counter, recorder, carried: Dict[str, str],
                   log: logger_utils.OperatorLogger, unique_id: str, run_id: str,
                   event_time: str) -> None:
    # 关闭路线：不校验模态、不加载模型、不产生框或时间戳，只做关键字段检查
    if not config.enabled:
        if loc.record_sample_id(record) is None:
            reasons[loc.REASON_MISSING_SAMPLE_ID] += 1
            _origin_quarantine(recorder, stats, log, line=line,
                               reason=loc.REASON_MISSING_SAMPLE_ID,
                               label=loc.LABEL_ORIGIN_MISSING_FIELD,
                               record=record, unique_id=unique_id, run_id=run_id,
                               event_time=event_time)
            return
        loc.apply_skipped(record, event_time=event_time, line=line)
        recorder.main.write(record)
        stats["skipped_records"] += 1
        _carry_media(record, media_roots, run_dir, carried, stats)
        return

    try:
        spec = loc.resolve_input_spec(record, media_roots)
    except loc.LocalizationError as exc:
        reasons[exc.reason_code] += 1
        _origin_quarantine(recorder, stats, log, line=line, reason=exc.reason_code,
                           label=exc.label, record=record, unique_id=unique_id,
                           run_id=run_id, event_time=event_time)
        return

    modality = spec["modality"]
    if modality["missing_image_reference"]:
        reasons[loc.REASON_IMAGE_MISSING] += 1
        _origin_quarantine(recorder, stats, log, line=line,
                           reason=loc.REASON_IMAGE_MISSING,
                           label=loc.LABEL_ORIGIN_MISSING_FIELD, record=record,
                           unique_id=unique_id, run_id=run_id, event_time=event_time)
        return
    if modality["has_image"]:
        stats["records_with_image"] += 1
    if modality["has_av"]:
        stats["records_with_av"] += 1

    boxes: Optional[List[Dict[str, Any]]] = None
    if modality["has_image"]:
        # 媒体存在性与可解码性必须先于模型检查：原始文件缺陷与模型可用性无关
        images, isolation = _resolve_images(spec, media_roots, config, stats, log,
                                            line=line)
        if isolation:
            reasons[isolation] += 1
            _origin_quarantine(recorder, stats, log, line=line, reason=isolation,
                               label=loc.REASON_LABELS.get(
                                   isolation, loc.LABEL_ORIGIN_MISSING_FIELD),
                               record=record, unique_id=unique_id, run_id=run_id,
                               event_time=event_time)
            return
        if detector_state.get("available"):
            boxes = []
            for located in images or []:
                try:
                    found = detector_obj.detect(
                        str(located), conf_threshold=config.detector.conf_threshold,
                        iou_threshold=config.detector.iou_threshold,
                        max_boxes=config.detector.max_boxes,
                        image_size=config.detector.image_size)
                except Exception as exc:  # noqa: BLE001 - 推理失败必须显式失败
                    raise DependencyError("detector failed on %s: %s"
                                          % (located.name, str(exc)[:160])) from exc
                stats["images_processed"] += 1
                for item in found:
                    enriched = dict(item)
                    enriched["image_ref"] = located.name
                    boxes.append(enriched)
        # 不可用时 boxes 保持 None：显式降级，不产生任何框
    elif spec.get("frames"):
        stats["records_with_frames"] += 1
        boxes = _detect_frames(spec, media_roots, config, detector_obj,
                               detector_state, stats, log, line=line)

    try:
        outcome = loc.evaluate_record(spec, line=line, config=config,
                                      event_time=event_time, boxes=boxes,
                                      detector_state=detector_state)
    except loc.LocalizationError as exc:
        reasons[exc.reason_code] += 1
        _origin_quarantine(recorder, stats, log, line=line, reason=exc.reason_code,
                           label=exc.label, record=record, unique_id=unique_id,
                           run_id=run_id, event_time=event_time)
        return
    action = outcome["action"]
    block = outcome["record"]["local_annotations"]
    dropped = block["dropped_boxes"]
    stats["boxes_prefilled"] += block["prefilled"]["box_count"]
    stats["time_ranges_prefilled"] += block["prefilled"]["time_range_count"]
    stats["boxes_dropped_unmatched"] += dropped
    stats["boxes_confirmed"] += len(block["boxes"])
    stats["time_ranges_confirmed"] += len(block["time_ranges"])
    reasons[block["reason_code"]] += 1

    if action == "confirm":
        stats["confirmed_records"] += 1
    elif action == "pending":
        stats["pending_records"] += 1
    elif action == "incomplete":
        stats["incomplete_records"] += 1
    else:
        stats["not_applicable_records"] += 1

    if action == loc.ROUTE_INCOMPLETE:
        # 表1：工具开启后标注不完整 → 样本隔离库，不留在可交付链上
        recorder.sample_quarantine.write(outcome["entry"])
    else:
        recorder.main.write(outcome["record"])
        if action == loc.ROUTE_PENDING:
            recorder.pending.write(outcome["entry"])

    _carry_media(outcome["record"], media_roots, run_dir, carried, stats)

    if action != "confirm":
        log.info("record_%s" % action, line=line, sampleId=spec["sample_id"],
                 reason=block["reason_code"], candidates=block["candidate_count"])
    elif line % PROGRESS_EVERY == 0:
        log.info("progress", lines=line, confirmed=stats["confirmed_records"])


def _check_conservation(stats: Counter, config) -> bool:
    routes = (stats["confirmed_records"] + stats["pending_records"]
              + stats["incomplete_records"] + stats["not_applicable_records"]
              + stats["origin_quarantined"] + stats["skipped_records"])
    kept = (stats["confirmed_records"] + stats["pending_records"]
            + stats["not_applicable_records"])
    expected_main = kept if config.enabled else stats["skipped_records"]
    return bool(
        stats["input_records"] == stats["valid_records"] + stats["bad_lines"]
        and stats["valid_records"] == routes
        and stats["rejects_entries"] == stats["bad_lines"] + stats["origin_quarantined"]
        and stats["main_records"] == expected_main
        and stats["pending_sidecar_entries"] == stats["pending_records"]
    )


# --------------------------------------------------------------------------- #
# CLI
# --------------------------------------------------------------------------- #

def _stage_local_input(task_input_dir: Path, source: Path) -> Path:
    """CLI 便利：把任务外的入口文件与同级 media/ 复制进任务 input。

    这样即使命令行直接指向仓库样例，业务路径仍然只读任务内文件（与 HTTP 一致）。
    """
    task_input_dir.mkdir(parents=True, exist_ok=True)
    target = task_input_dir / source.name
    if source.resolve() != target.resolve():
        shutil.copyfile(source, target)
    media_dir = source.parent / "media"
    if media_dir.is_dir():
        target_media = task_input_dir / "media"
        target_media.mkdir(parents=True, exist_ok=True)
        for item in sorted(media_dir.iterdir()):
            if item.is_file():
                shutil.copyfile(item, target_media / item.name)
    manifest = source.parent / MANIFEST_NAME
    if manifest.is_file() and not (task_input_dir / MANIFEST_NAME).exists():
        shutil.copyfile(manifest, task_input_dir / MANIFEST_NAME)
    return target


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="inference.py",
        description="算子 #17 标注局部对整体：图像通用类别预填 + 音视频转写时间段预填，人工确认后方可交付")
    parser.add_argument("--input", "--input-file", dest="input_file",
                        help="入口 JSONL（任务内相对路径或外部样例文件，外部会自动暂存进任务 input）")
    parser.add_argument("--unique-id", dest="unique_id", default="cli_task")
    parser.add_argument("--config", dest="config_path", default=None)
    parser.add_argument("--output-root", dest="output_root", default=None)
    parser.add_argument("--enable", action="store_true", help="开启工具（表1 默认关闭）")
    parser.add_argument("--disable", action="store_true", help="显式关闭工具")
    parser.add_argument("--allow-degraded", dest="allow_degraded", action="store_true",
                        default=None, help="模型不可用时显式降级")
    parser.add_argument("--no-degraded", dest="allow_degraded", action="store_false",
                        help="模型不可用时显式失败")
    parser.add_argument("--max-time-ranges", type=int, default=None)
    parser.add_argument("--max-text-chars", type=int, default=None)
    parser.add_argument("--max-images-per-record", type=int, default=None)
    parser.add_argument("--no-detector", action="store_true",
                        help="本次不加载检测后端（等价 detector_backend=none，仅限本地调试）")
    parser.add_argument("--print-lineage", action="store_true")
    return parser


def _overrides_from_args(args) -> Dict[str, Any]:
    overrides: Dict[str, Any] = {}
    if args.enable:
        overrides["enabled"] = True
    if args.disable:
        overrides["enabled"] = False
    if args.allow_degraded is not None:
        overrides["allow_degraded"] = bool(args.allow_degraded)
    if args.max_time_ranges is not None:
        overrides["max_time_ranges"] = args.max_time_ranges
    if args.max_text_chars is not None:
        overrides["max_text_chars"] = args.max_text_chars
    if args.max_images_per_record is not None:
        overrides["max_images_per_record"] = args.max_images_per_record
    return overrides


def main(argv: Optional[List[str]] = None) -> int:
    args = build_parser().parse_args(argv)
    overrides = _overrides_from_args(args)

    try:
        output_root = Path(args.output_root) if args.output_root else default_output_dir()
        input_file = args.input_file
        if input_file:
            candidate = io_utils.resolved_absolute(input_file)
            if not candidate.is_file():
                raise InputError("input file does not exist: %s" % input_file)
            task_input = io_utils.task_input_dir(output_root, args.unique_id)
            inside = True
            try:
                io_utils.resolve_in_task(
                    io_utils.task_root(output_root, args.unique_id), candidate)
            except ValueError:
                inside = False
            if not inside:
                input_file = str(_stage_local_input(task_input, candidate))

        result = run(input_file=input_file, unique_id=args.unique_id,
                     config_path=args.config_path, output_root=str(output_root),
                     overrides=overrides)
    except (InputError, ConfigError, io_utils.TaskBusyError, DependencyError,
            ValueError, OSError) as exc:
        sys.stderr.write("operator 17 failed: %s\n" % exc)
        return 1

    summary = {
        "uniqueId": result["uniqueId"],
        "runId": result["runId"],
        "status": result["status"],
        "entrypoint": result["entrypoint"],
        "outputFile": result["outputFile"],
        "statistics": result["statistics"],
    }
    sys.stdout.write(json.dumps(summary, ensure_ascii=False, indent=2) + "\n")
    # [v4.2] 血缘输出已停用：--print-lineage 不再打印阶段血缘（参数保留兼容）
    # if args.print_lineage:
    #     sys.stdout.write(json.dumps(build_stage_payload(result),
    #                                 ensure_ascii=False, indent=2) + "\n")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
