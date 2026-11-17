#!/usr/bin/env python3
"""算子 5 视频关键帧与片段处理：配置加载、run() 与 CLI。

- ``run`` 返回 DEVELOPMENT_CONTRACT §1 的 RunResult（本地路径，不是 HTTP data）；
- HTTP 响应由 ``app.py`` 调用 ``build_response`` 转换，CLI 复用同一转换；
- 逐行流式读写，先写临时文件、成功后再原子提交。

CLI::

    python inference.py --input dataset/sample/demo.jsonl --unique-id smoke_05 \
        --output-root output/smoke [--config configs/config.yaml] [--is-last]
        [--override sample_fps=0.5]
"""
from __future__ import annotations
import copy

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

from typing import Any, Dict, List, Optional, Tuple

import yaml
from pydantic import ValidationError

if __package__ in (None, ""):  # 允许 python inference.py 直接执行
    sys.path.insert(0, str(Path(__file__).resolve().parent))

from utils import form1_package  # noqa: E402
from utils import video_processing as vp  # noqa: E402
from utils.io import (JsonlWriter, TaskBusyError, TaskLock, UnsupportedMediaPath,  # noqa: E402
                      assert_utf8, atomic_write_json, next_run_id, read_jsonl_stream,
                      resolve_media, task_dir)
# [v4.2] 血缘/阶段归档输出已停用：append_lineage/make_lineage/build_stage_context/
# to_stage_lineage/load_lineage/load_prior_lineage/LINEAGE_NAME/STAGE_CONTEXT_NAME 不再导入，
# 原调用点已全部注释（代码在 utils/lineage.py 存档保留）；now_iso 仅为时间工具，保留。
from utils.lineage import now_iso  # noqa: E402
from utils.logger import close_logger, get_logger, log_action  # noqa: E402
from utils.schemas import BUSINESS_CONFIG_KEYS, OperatorConfig, ensure_finite_numbers  # noqa: E402
from utils.transport import (DEFAULT_MAX_RESPONSE_BYTES, MANIFEST_NAME,  # noqa: E402
                             OutputTooLargeError, TransportError,
                             build_manifest, iter_manifest_paths, load_manifest,
                             pack_file_object, resolve_entrypoint, write_manifest)

DIR = Path(__file__).resolve().parent

#: [v4.2] §4.6 规范参数名 → 内部实现参数名（接口二 config 别名归一）
CONFIG_ALIASES = {"fps": "sample_fps"}

MAIN_NAME = "05_frame_index.jsonl"          # 主清单（下一节点 inputFile 的目标）
REJECT_NAME = "05_rejects.jsonl"            # 异常/剔除清单
OPERATOR_DIR_NAME = "video_keyframes"       # 映射 ID 与任务子目录名
MEDIA_FIELDS = ("image", "video", "audio")  # 本算子负责搬运的媒体字段
PROCESSING_KEY = "05"                       # DATA_SCHEMA §7.1 processing 键
INPUT_DIR_NAME = "input"                    # 阶段首素材落点

#: 业务异常 → 拒绝原因
REJECT_REASONS = (
    (vp.MediaUnreadableError, "media_unreadable"),
    (vp.VideoMetadataError, "invalid_video_metadata"),
    (vp.ResourceLimitError, "resource_limit"),
    (vp.UnsupportedVFRError, "unsupported_vfr"),
    (vp.FrameSaveError, "frame_save_failed"),
    (UnsupportedMediaPath, "unsupported_media_path"),
)
#: 全部拒绝原因（供文档与测试核对）
REJECT_REASON_NAMES = tuple(reason for _exc, reason in REJECT_REASONS) + (
    "invalid_json", "invalid_record", "missing_sample_id", "duplicate_sample_id",
    "media_missing", "invalid_media_field", "internal_error",
)


class ConfigError(ValueError):
    """配置或请求参数错误。"""


# --------------------------------------------------------------------------- #
# 配置
# --------------------------------------------------------------------------- #

def _format_validation_error(error: ValidationError) -> str:
    parts = []
    for item in error.errors():
        location = ".".join(str(step) for step in item.get("loc", ())) or "config"
        parts.append("%s: %s" % (location, item.get("msg", "invalid")))
    return "；".join(parts)


def build_snapshot(cfg: OperatorConfig) -> Dict[str, Any]:
    """拆分为部署快照与业务参数快照（写入血缘 config 字段）。"""
    return {
        "deployment": {
            "operator": cfg.operator.model_dump(),
            "stage_id": cfg.stage_id,
            "io": cfg.io.model_dump(),
            "server": cfg.server.model_dump(),
        },
        "business": {key: getattr(cfg, key) for key in BUSINESS_CONFIG_KEYS},
    }


def load_config(config_path: Optional[str] = None,
                overrides: Optional[Dict[str, Any]] = None,
                output_root: Optional[str] = None) -> Tuple[OperatorConfig, Dict[str, Any]]:
    """加载并校验配置；HTTP 与 CLI 使用同一函数。

    - ``overrides`` 只允许业务参数白名单，未知键报错；
    - ``operator``/``stage_id``/``io``/``server`` 只能来自服务端配置文件；
    - ``output_root`` 覆盖 ``io.output_root``（CLI/测试用），HTTP 不接收请求指定。
    """
    payload: Dict[str, Any] = {}
    if config_path is not None:
        path = Path(config_path)
        if not path.is_file():
            raise ConfigError("配置文件不存在：%s" % path)
        try:
            raw = yaml.safe_load(path.read_text(encoding="utf-8"))
        except (yaml.YAMLError, OSError) as exc:
            raise ConfigError("配置文件无法解析：%s" % type(exc).__name__) from exc
        if raw is None:
            raw = {}
        if not isinstance(raw, dict):
            raise ConfigError("配置文件根节点必须是对象")
        payload = dict(raw)

    if overrides:
        if not isinstance(overrides, dict):
            raise ConfigError("config 必须是对象")
        # [v4.2] §4.6 规范参数名 fps → 内部 sample_fps；其余键保持不变
        normalized = {CONFIG_ALIASES.get(key, key): value
                      for key, value in overrides.items()}
        unknown = sorted(set(normalized) - set(BUSINESS_CONFIG_KEYS))
        if unknown:
            raise ConfigError("config 只允许覆盖业务参数 %s，收到未知键：%s"
                              % (",".join(BUSINESS_CONFIG_KEYS), ", ".join(unknown)))
        try:
            ensure_finite_numbers(normalized)
        except ValueError as exc:
            raise ConfigError(str(exc)) from exc
        payload.update(normalized)

    if output_root is not None:
        # CLI/测试显式覆盖优先级最高
        if not isinstance(output_root, str) or not output_root.strip():
            raise ConfigError("output_root 必须是非空字符串")
        payload["io"] = dict(payload.get("io") or {})
        payload["io"]["output_root"] = output_root.strip()
    elif not (payload.get("io") or {}).get("output_root"):
        # 配置文件未指定时，用共享契约的 OUTPUT_DIR 环境变量作为输出根
        env_root = os.environ.get("OUTPUT_DIR")
        if env_root and env_root.strip():
            payload["io"] = dict(payload.get("io") or {})
            payload["io"]["output_root"] = env_root.strip()

    try:
        cfg = OperatorConfig(**payload)
    except ValidationError as exc:
        raise ConfigError(_format_validation_error(exc)) from exc
    return cfg, build_snapshot(cfg)


# --------------------------------------------------------------------------- #
# 拒绝清单与统计
# --------------------------------------------------------------------------- #

def _reject_entry(sample_id: Optional[str], line: Optional[int], reason: str,
                  record: Optional[dict] = None, evidence: Optional[dict] = None,
                  source_reference: Optional[dict] = None) -> Dict[str, Any]:
    entry: Dict[str, Any] = {"operator": OPERATOR_DIR_NAME, "reason": reason}
    if sample_id is not None:
        entry["sample_id"] = sample_id
    if line is not None:
        entry["line"] = line
    if record is not None:
        entry["record"] = record
    if source_reference is not None:
        entry["source_reference"] = source_reference
    if evidence is not None:
        entry["evidence"] = evidence
    return entry


def _reason_for(exc: BaseException) -> str:
    for exc_type, reason in REJECT_REASONS:
        if isinstance(exc, exc_type):
            return reason
    return "internal_error"


def _new_statistics(cfg: OperatorConfig) -> Dict[str, Any]:
    return {
        "input": 0,                 # 有效记录数（可解析 dict）
        "input_lines": 0,           # 非空行数
        "valid_records": 0,         # = input
        "bad_lines": 0,
        "output": 0,                # 主清单行数
        "rejected": 0,
        "not_applicable": 0,        # output 子集
        "success": 0,               # 真正完成抽帧的记录数
        "duplicates": 0,
        "frames_total": 0,          # 帧条数（不是样本数）
        "frames_saved": 0,          # 实际落盘并复验的帧文件数
        "media_copied": 0,
        "media_bytes": 0,
        "media_missing": 0,
        "vfr_detected": 0,
        "cropped": 0,
        "empty_input": False,
        "probe_backend": "unknown",
        "requested_sample_fps": cfg.sample_fps,
        "max_frames": cfg.max_frames,
        "max_duration_seconds": cfg.max_duration_seconds,
        "conservation_ok": False,
    }


# --------------------------------------------------------------------------- #
# 媒体搬运
# --------------------------------------------------------------------------- #

def _unique_relative(relative: str, taken: set) -> str:
    """同一次 run 内避免重名（大小写不敏感，Windows/NFS 都安全）。"""
    if relative.lower() not in taken:
        taken.add(relative.lower())
        return relative
    stem, suffix = Path(relative).stem, Path(relative).suffix
    parent = str(Path(relative).parent).replace("\\", "/")
    index = 2
    while True:
        candidate = ("%s/%s_%d%s" % (parent, stem, index, suffix)) if parent != "." \
            else ("%s_%d%s" % (stem, index, suffix))
        if candidate.lower() not in taken:
            taken.add(candidate.lower())
            return candidate
        index += 1


def carry_media(record: Dict[str, Any], entry_dir: Path, run_dir: Path, sample_id: str,
                taken: set) -> Tuple[Dict[str, Any], List[Dict[str, Any]],
                                     List[Dict[str, str]], List[str]]:
    """把记录引用的媒体复制进本次 run 目录并统一重写相对路径。

    返回 ``(更新后的记录, artifacts, 重写映射, 错误原因列表)``；存在错误时调用方按
    拒绝处理，不会写出指向不存在文件的"成功"记录。
    """
    artifacts: List[Dict[str, Any]] = []
    media_map: List[Dict[str, str]] = []
    errors: List[str] = []
    safe_id = vp.safe_component(sample_id)
    updated = dict(record)

    for field in MEDIA_FIELDS:
        value = record.get(field)
        if value is None or value == "":
            continue
        if isinstance(value, str):
            relative: Any = value
            source_obj: Optional[Dict[str, Any]] = None
        elif isinstance(value, dict):
            relative = value.get("path")
            source_obj = dict(value)
            if not relative:
                continue
        else:
            errors.append("invalid_media_field:%s" % field)
            continue
        try:
            origin = resolve_media(entry_dir, str(relative))
        except UnsupportedMediaPath:
            errors.append("unsupported_media_path:%s" % field)
            continue
        if not origin.is_file():
            errors.append("media_missing:%s" % field)
            continue

        target_relative = _unique_relative("media/%s/%s" % (safe_id, origin.name), taken)
        target = run_dir / target_relative
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(origin, target)

        new_obj: Dict[str, Any] = {"path": target_relative, "original_path": str(relative)}
        if source_obj:
            for key, item in source_obj.items():
                if key in ("path", "original_path"):
                    continue
                new_obj[key] = item
        updated[field] = new_obj
        artifacts.append({"path": target_relative, "role": "media"})
        media_map.append({"field": field, "source": str(relative), "target": target_relative})
    return updated, artifacts, media_map, errors


def _rollback_sample_artifacts(run_dir: Path, sample_id: str,
                              media_map: List[Dict[str, str]], taken: set,
                              owned_safe_ids: set) -> None:
    """回滚被拒绝样本的产物：删除已搬运的媒体与半成品帧，不留孤儿文件。

    媒体是在解码**之前**搬运的，若随后抽帧失败，记录只进 ``05_rejects.jsonl``，
    此时 ``media/<sample_id>/`` 与 ``05_frames/<sample_id>/`` 就成了既不在主清单、
    也不在 manifest 里的死文件（还会白占磁盘、白搬一遍大视频）。

    分两步，保证不误删别人：
    1. 按 ``media_map`` 精确删除本次搬运的文件（``taken`` 占位同步释放）；
    2. 目录级回收仅在该 ``safe_id`` 尚无成功记录占用时执行——两个不同 sample_id
       可能归一到同一 ``safe_id``，此时目录是共用的，不能整体删。
    """
    for item in media_map or []:
        try:
            (run_dir / item["target"]).unlink()
        except OSError:
            pass
        taken.discard(item["target"].lower())

    safe_id = vp.safe_component(sample_id)
    if safe_id in owned_safe_ids:
        return
    for prefix in ("media/%s/" % safe_id, "05_frames/%s/" % safe_id):
        directory = run_dir / prefix.rstrip("/")
        if directory.is_dir():
            shutil.rmtree(directory, ignore_errors=True)
        lowered = prefix.lower()
        for key in [item for item in taken if item.startswith(lowered)]:
            taken.discard(key)


# --------------------------------------------------------------------------- #
# 单条记录处理
# --------------------------------------------------------------------------- #

def _video_reference(record: Dict[str, Any]) -> Optional[str]:
    """取视频引用（字符串形式或对象 ``path``），无视频返回 None。"""
    value = record.get("video")
    if isinstance(value, str) and value.strip():
        return value.strip()
    if isinstance(value, dict) and isinstance(value.get("path"), str) and value["path"].strip():
        return value["path"].strip()
    return None


def _mark_not_applicable(record: Dict[str, Any]) -> Dict[str, Any]:
    processing = dict(record.get("processing") or {})
    # [v4.2] 输出记录 status 双字段：int status（1 正常/0 失败）+ process_status 流程态；
    # 无视频属正常流程态（不适用），status=1 不伪装失败。
    processing[PROCESSING_KEY] = {"status": 1, "process_status": "not_applicable",
                                  "reason": "no_video", "metrics": {}}
    record["processing"] = processing
    return record


def _media_paths(record):
    refs=[]
    for field in MEDIA_FIELDS:
        value=record.get(field)
        if isinstance(value,str): refs.append(value)
        elif isinstance(value,dict):
            refs += [value[k] for k in ("path","original_path") if value.get(k)]
            refs += [item["path"] for key in ("frames","segments") for item in value.get(key,[]) if isinstance(item,dict) and item.get("path")]
    return list(dict.fromkeys(refs))


def process_record(record: Dict[str, Any], entry_dir: Path, run_dir: Path,
                   cfg: OperatorConfig, business_cfg: Dict[str, Any], run_id: str,
                   taken: set) -> Tuple[Optional[Dict[str, Any]], List[Dict[str, Any]],
                                        List[Dict[str, str]], Optional[Dict[str, Any]], int]:
    """处理一条完整记录。

    返回 ``(主清单记录, artifacts, 媒体映射, 拒绝项, 实际保存帧数)``；
    拒绝项非空表示该记录只进入 ``05_rejects.jsonl``。
    """
    if not cfg.enabled:
        updated = copy.deepcopy(record)
        artifacts=[]
        for relative in _media_paths(record):
            source=entry_dir/relative
            if source.is_file():
                dest=run_dir/relative
                dest.parent.mkdir(parents=True,exist_ok=True)
                shutil.copy2(source,dest)
                artifacts.append({"path":relative,"role":"media"})
        return updated, artifacts, [], None, 0

    raw_id = record.get("sample_id")
    if not isinstance(raw_id, str) or not raw_id.strip():
        return (None, [], [], _reject_entry(None, None, "missing_sample_id", record=record,
                                            evidence={"field": "sample_id"}), 0)
    sample_id = raw_id.strip()

    updated, artifacts, media_map, errors = carry_media(
        record, entry_dir, run_dir, sample_id, taken)
    if errors:
        return (None, [], media_map, _reject_entry(
            sample_id, None, errors[0].split(":", 1)[0], record=record,
            evidence={"media_errors": errors, "media_rewrite": media_map},
            source_reference={field: record.get(field) for field in MEDIA_FIELDS
                              if record.get(field) is not None}), 0)

    video_relative = _video_reference(record)
    if video_relative is None:
        if record.get("video") is not None and not isinstance(record.get("video"), (str, dict)):
            return (None, [], media_map, _reject_entry(
                sample_id, None, "invalid_media_field", record=record,
                evidence={"media_errors": ["invalid_media_field:video"]}), 0)
        return _mark_not_applicable(updated), artifacts, media_map, None, 0

    video_object = updated.get("video")
    if not isinstance(video_object, dict):
        return (None, [], media_map, _reject_entry(
            sample_id, None, "media_missing", record=record,
            evidence={"media_errors": ["media_missing:video"]},
            source_reference={"video": video_relative}), 0)

    try:
        extracted = vp.extract_frames(run_dir / video_object["path"], run_dir,
                                      {**business_cfg, "sample_id": sample_id})
    except (vp.VideoProcessingError, ValueError, OSError) as exc:
        return (None, [], media_map, _reject_entry(
            sample_id, None, _reason_for(exc), record=record,
            evidence={"message": str(exc), "operator_version": cfg.operator.version},
            source_reference={"video": video_relative}), 0)

    for frame in extracted["frames"]:
        artifacts.append({"path": frame["path"], "role": "media"})

    video_object.update({
        "segments": [{"start": 0.0, "end": extracted["duration"], "frames": extracted["frames"]}],
        "duration": extracted["duration"],
        "fps": extracted["fps"],
        "frame_count": extracted["frame_count"],
        "width": extracted["width"],
        "height": extracted["height"],
        "frames": extracted["frames"],
        "extraction": {
            "operator": OPERATOR_DIR_NAME,
            "decoder_backend": "ffmpeg",
            "operator_version": cfg.operator.version,
            "run_id": run_id,
            "sample_fps": extracted["sample_fps"],
            "planned_frames": extracted["planned_frames"],
            "decoded_frames": extracted["decoded_frames"],
            "timestamp_source": extracted["timestamp_source"],
            "vfr_detected": extracted["vfr_detected"],
            "max_pts_deviation": extracted["max_pts_deviation"],
            "probe": extracted["probe"],
            "warnings": extracted["warnings"],
        },
    })
    if extracted["crop"] is not None:
        video_object["crop"] = extracted["crop"]
    updated["video"] = video_object

    processing = dict(updated.get("processing") or {})
    # [v4.2] 输出记录 status 双字段：int status + process_status 流程态
    processing[PROCESSING_KEY] = {
        "status": 1,
        "process_status": "success",
        "reason": None,
        "metrics": {
            "frames": len(extracted["frames"]),
            "planned_frames": extracted["planned_frames"],
            "decoded_frames": extracted["decoded_frames"],
            "sample_fps": extracted["sample_fps"],
            "duration": extracted["duration"],
            "fps": extracted["fps"],
            "timestamp_source": extracted["timestamp_source"],
            "vfr_detected": extracted["vfr_detected"],
            "crop_applied": extracted["crop"] is not None,
        },
    }
    updated["processing"] = processing
    return updated, artifacts, media_map, None, len(extracted["frames"])


# --------------------------------------------------------------------------- #
# 输入定位与阶段血缘
# --------------------------------------------------------------------------- #

def resolve_input_path(task: Path, input_file: str) -> Path:
    """把 inputFile 解析为可读入口文件。

    - 任务根内路径由调用方（HTTP 层）先行校验；本函数同时接受 CLI 的本地路径，
      因此对任务根外路径不额外拒绝，但必须是存在的文件或含 manifest 的目录；
    - 目录输入按 manifest.json 的 entrypoint 定位入口。
    """
    if not isinstance(input_file, str) or not input_file.strip():
        raise ConfigError("input_file 不能为空")
    raw = Path(input_file)
    if not raw.is_absolute():
        inside = task / input_file
        raw = inside if inside.exists() else Path(input_file)
    target = raw.expanduser().resolve()
    if target.is_dir():
        entry, _relative = resolve_entrypoint(target)
        return entry
    return target


# [v4.2] 血缘/阶段归档已停用：上游血缘播种函数整体注释存档，不再被调用。
# def seed_stage_lineage(entry_dir: Path, cfg: OperatorConfig,
#                        unique_id: str) -> List[Dict[str, Any]]:
#     """读取上游同阶段血缘作为本次阶段血缘的起点。"""
#     context = entry_dir / STAGE_CONTEXT_NAME
#     if context.is_file():
#         try:
#             payload = json.loads(context.read_text(encoding="utf-8"))
#         except (json.JSONDecodeError, OSError):
#             payload = None
#         if isinstance(payload, dict):
#             records = payload.get("stage_lineage") or []
#             return [item for item in records
#                     if isinstance(item, dict)
#                     and item.get("stageId") == cfg.stage_id
#                     and item.get("uniqueId") == unique_id]
#     records = load_lineage(entry_dir / LINEAGE_NAME)
#     seeded: List[Dict[str, Any]] = []
#     keys = set()
#     for record in records:
#         if record.get("stageId") != cfg.stage_id or record.get("uniqueId") != unique_id:
#             continue
#         key = (record.get("operator"), record.get("runId"))
#         if key in keys:
#             continue
#         keys.add(key)
#         seeded.append(record)
#     return seeded


# --------------------------------------------------------------------------- #
# 运行
# --------------------------------------------------------------------------- #

def _failed_result(run_dir: Path, message: str, statistics: Dict[str, Any],
                   run_id: str, logger,
                   entrypoint: str = MAIN_NAME) -> Dict[str, Any]:
    """失败结果：不返回任何成功产物。

    [v4.2] 失败血缘落盘（make_lineage/append_lineage）已停用，仅保留运行日志。
    """
    log_action(logger, "失败", message)
    # [v4.2] 血缘记录构建与追加已停用（原 make_lineage/append_lineage 调用块注释存档）：
    # main_path = Path(run_dir) / entrypoint
    # record = make_lineage(OPERATOR_DIR_NAME, version, unique_id, stage_id, run_id,
    #                       input_file, str(main_path) if main_path.exists() else None,
    #                       "failed", started, now_iso(), snapshot, statistics, [])
    # stage_lineage = append_lineage(lineage_path, record)
    close_logger(logger)
    return _result("failed", None, statistics, message, run_id,
                   str(run_dir), entrypoint, None, str(Path(run_dir) / MANIFEST_NAME), [])


def _result(status: str, output_file: Optional[str],
            statistics: Dict[str, Any], message: str, run_id: Optional[str],
            run_dir: Optional[str], entrypoint: str, rejects_file: Optional[str],
            manifest_file: Optional[str],
            artifacts: List[Dict[str, Any]]) -> Dict[str, Any]:
    """RunResult 及供 app.py/CLI 转换的内部字段。

    [v4.2] 血缘输出已停用：返回 dict 不再包含 lineage/lineageFile/stage_lineage/
    prior_lineage 任何血缘键。
    """
    return {
        "status": status,
        "outputFile": output_file,
        "statistics": statistics,
        "message": message,
        # --- 内部字段：不直接作为 HTTP data 外泄 ---
        "runId": run_id,
        "runDir": run_dir,
        "entrypoint": entrypoint,
        "rejectsFile": rejects_file,
        "manifestFile": manifest_file,
        "artifacts": artifacts,
    }


def _run_locked(cfg: OperatorConfig, snapshot: Dict[str, Any], task: Path,
                unique_id: str, input_file: str) -> Dict[str, Any]:
    """取锁后执行一次完整运行（任务目录、run 目录、血缘、产物一起落地）。"""
    operator_dir = task / OPERATOR_DIR_NAME
    operator_dir.mkdir(parents=True, exist_ok=True)
    run_id = next_run_id(operator_dir)
    run_dir = operator_dir / run_id
    run_dir.mkdir(parents=True, exist_ok=False)
    # [v4.2] 血缘落盘已停用：不再创建 lineage.json 路径
    # lineage_path = run_dir / LINEAGE_NAME
    logger = get_logger("%s.%s.%s" % (OPERATOR_DIR_NAME, unique_id, run_id),
                        run_dir / "run.log")
    # [v4.2] started/version 原仅供血缘记录使用，血缘停用后不再赋值（now_iso 导入保留）
    # started = now_iso()
    stats = _new_statistics(cfg)
    # stage_id = cfg.stage_id  # [v4.2] 原仅供血缘/阶段上下文使用，已停用
    # version = cfg.operator.version
    log_action(logger, "配置", "视频兜底已开启：FFmpeg最多8帧" if cfg.enabled else "视频兜底关闭，不执行")

    try:
        input_path = resolve_input_path(task, input_file)
    except (ConfigError, TransportError, ValueError) as exc:
        stats["error"] = "invalid_input"
        log_action(logger, "输入", "无法解析 inputFile：%s" % type(exc).__name__)
        return _failed_result(run_dir, "inputFile 无法解析为入口文件：%s" % exc, stats,
                              run_id, logger)

    entry_dir = input_path.parent
    # [v4.2] 血缘已停用：不再加载历史血缘、不再播种上游阶段血缘
    # prior_lineage = load_prior_lineage([entry_dir, task / INPUT_DIR_NAME, task])
    # for upstream in seed_stage_lineage(entry_dir, cfg, unique_id):
    #     append_lineage(lineage_path, upstream)
    log_action(logger, "输入", "inputFile=%s lines_pending" % input_path.name)

    if not input_path.is_file():
        stats["error"] = "input_not_found"
        return _failed_result(run_dir, "输入文件不存在：%s" % input_path.name, stats,
                              run_id, logger)
    try:
        assert_utf8(input_path)
    except UnicodeDecodeError:
        stats["error"] = "invalid_encoding"
        return _failed_result(run_dir, "输入文件不是严格 UTF-8，按 invalid_encoding 结束",
                              stats, run_id, logger)
    except OSError as exc:
        stats["error"] = "input_unreadable"
        return _failed_result(run_dir, "输入文件读取失败：%s" % type(exc).__name__, stats,
                              run_id, logger)

    business_cfg = dict(snapshot["business"])
    out_path = run_dir / MAIN_NAME
    reject_path = run_dir / REJECT_NAME
    out_tmp = run_dir / (MAIN_NAME + ".tmp")
    reject_tmp = run_dir / (REJECT_NAME + ".tmp")
    artifacts: List[Dict[str, Any]] = [
        {"path": MAIN_NAME, "role": "main"},
        {"path": REJECT_NAME, "role": "review"},
    ]
    taken = {MAIN_NAME.lower(), REJECT_NAME.lower(), MANIFEST_NAME.lower()}
    seen_ids: Dict[str, int] = {}
    #: 已成功入主清单的 safe_id，用于判断样本目录是否被他人共用（回滚时不能误删）
    owned_safe_ids: set = set()
    interrupted: Optional[str] = None

    try:
        with JsonlWriter(out_tmp) as main_writer, JsonlWriter(reject_tmp) as reject_writer:
            for lineno, record, error in read_jsonl_stream(input_path):
                stats["input_lines"] += 1
                if error is not None:
                    stats["bad_lines"] += 1
                    reject_writer.write(_reject_entry(None, lineno, error))
                    log_action(logger, "坏行", "line=%d reason=%s" % (lineno, error))
                    continue
                stats["valid_records"] += 1
                sample_id = record.get("sample_id")
                if isinstance(sample_id, str) and sample_id.strip():
                    sample_id = sample_id.strip()
                    if sample_id in seen_ids:
                        # 重复样本进入 05_rejects.jsonl，属于已处理的拒绝记录：
                        # 必须与 output 一起参与守恒（valid = output + rejected）
                        stats["duplicates"] += 1
                        stats["rejected"] += 1
                        reject_writer.write(_reject_entry(
                            sample_id, lineno, "duplicate_sample_id", record=record,
                            evidence={"first_line": seen_ids[sample_id]}))
                        log_action(logger, "重复", "sample=%s line=%d" % (sample_id, lineno))
                        continue
                    seen_ids[sample_id] = lineno
                try:
                    out_record, rec_artifacts, media_map, reject, frames = process_record(
                        record, entry_dir, run_dir, cfg, business_cfg, run_id, taken)
                except Exception as exc:  # noqa: BLE001 - 单条记录不得中断整批
                    out_record, rec_artifacts, media_map, frames = None, [], [], 0
                    reject = _reject_entry(
                        sample_id if isinstance(sample_id, str) else None, lineno,
                        "internal_error", record=record,
                        evidence={"message": "%s: %s" % (type(exc).__name__, exc)})

                if reject is not None:
                    stats["rejected"] += 1
                    reason = str(reject.get("reason", ""))
                    if reason.startswith("media_missing"):
                        stats["media_missing"] += 1
                    if isinstance(sample_id, str) and sample_id.strip():
                        # 拒绝记录只进 05_rejects.jsonl：回收它的媒体/帧，不留孤儿产物
                        _rollback_sample_artifacts(run_dir, sample_id.strip(), media_map,
                                                   taken, owned_safe_ids)
                    reject.setdefault("line", lineno)
                    reject_writer.write(reject)
                    log_action(logger, "拒绝", "line=%d reason=%s" % (lineno, reason))
                    continue

                stats["output"] += 1
                record_processing = (out_record.get("processing") or {}).get(PROCESSING_KEY) or {}
                # [v4.2] 流程态读取同步迁移到 process_status（status 已为 int）
                if record_processing.get("process_status") == "not_applicable":
                    stats["not_applicable"] += 1
                else:
                    stats["success"] += 1
                    stats["frames_total"] += frames
                    stats["frames_saved"] += frames
                    stats["media_copied"] += len(media_map)
                    video_object = out_record.get("video") or {}
                    if not isinstance(video_object, dict):
                        video_object = {}
                    extraction = video_object.get("extraction") or {}
                    if video_object.get("crop"):
                        stats["cropped"] += 1
                    if extraction.get("vfr_detected"):
                        stats["vfr_detected"] += 1
                    probe_backend = (extraction.get("probe") or {}).get("backend")
                    if probe_backend:
                        stats["probe_backend"] = probe_backend
                # 记下该样本目录已有主清单记录，后续同名 safe_id 的拒绝不得整体回收目录
                if isinstance(sample_id, str) and sample_id.strip():
                    owned_safe_ids.add(vp.safe_component(sample_id.strip()))
                artifacts.extend(rec_artifacts)
                main_writer.write(out_record)
    except Exception as exc:  # noqa: BLE001 - 文件系统/编码等异常：明确失败，不留下假成功
        interrupted = "internal_error"
        stats["error"] = "internal_error"
        log_action(logger, "中断", "处理中断：%s: %s" % (type(exc).__name__, exc))

    stats["input"] = stats["valid_records"]
    stats["empty_input"] = (stats["input_lines"] == 0)
    stats["conservation_ok"] = (stats["output"] + stats["rejected"] == stats["valid_records"])

    if interrupted is not None:
        for path in (out_tmp, reject_tmp):
            try:
                Path(path).unlink()
            except OSError:
                pass
        return _failed_result(run_dir, "处理中断：%s" % interrupted, stats,
                              run_id, logger)

    if not stats["conservation_ok"]:
        stats["error"] = "conservation_broken"
        return _failed_result(run_dir, "统计不守恒：output+rejected != valid_records", stats,
                              run_id, logger)

    os.replace(str(out_tmp), str(out_path))
    os.replace(str(reject_tmp), str(reject_path))

    missing_artifacts = [item["path"] for item in artifacts
                         if not (run_dir / item["path"]).is_file()]
    if missing_artifacts:
        stats["error"] = "artifact_missing"
        return _failed_result(run_dir, "产物缺失：%s" % ", ".join(missing_artifacts[:3]),
                              stats, run_id, logger)

    for artifact in artifacts:
        if artifact["role"] == "media":
            stats["media_bytes"] += (run_dir / artifact["path"]).stat().st_size

    manifest = build_manifest(MAIN_NAME, artifacts)
    write_manifest(run_dir, manifest)
    publish_stage_output(run_dir)
    stats["artifact_files"] = len(manifest["artifacts"]) + 1

    if stats["valid_records"] == 0:
        if stats["input_lines"] > 0 and stats["bad_lines"] > 0:
            status, message = "failed", "输入 %d 行全部无法解析为完整记录（bad_lines=%d）" % (
                stats["input_lines"], stats["bad_lines"])
            stats["error"] = "invalid_input_records"
        elif stats["empty_input"]:
            status, message = "success", "输入为空（empty_input=true），已输出空主清单"
        else:
            status, message = "success", "输入没有可处理记录"
    elif stats["rejected"] > 0:
        status = "partial"
        message = ("partial: 有效 %d 条，产出 %d 条（抽帧成功 %d 条，不适用 %d 条），"
                   "拒绝 %d 条（其中重复 %d 条）") % (
            stats["valid_records"], stats["output"], stats["success"],
            stats["not_applicable"], stats["rejected"], stats["duplicates"])
    else:
        status = "success"
        message = "success: 有效 %d 条，抽帧 %d 帧，不适用 %d 条" % (
            stats["valid_records"], stats["frames_saved"], stats["not_applicable"])

    if status == "failed":
        log_action(logger, "失败", message)
    else:
        log_action(logger, status, message)

    # [v4.2] 血缘/阶段归档已停用：血缘记录构建、追加与 stage_context.json 落盘全部注释存档
    # record = make_lineage(OPERATOR_DIR_NAME, version, unique_id, stage_id, run_id,
    #                       str(input_path), str(out_path), status, started, now_iso(),
    #                       snapshot, stats, artifacts)
    # stage_lineage = append_lineage(lineage_path, record)
    # stage_context = build_stage_context(stage_id, unique_id, run_id, MAIN_NAME,
    #                                     stage_lineage, prior_lineage)
    # atomic_write_json(run_dir / STAGE_CONTEXT_NAME, stage_context)
    close_logger(logger)

    result = _result(status, None if status == "failed" else str(out_path),
                     stats, message, run_id, str(run_dir), MAIN_NAME,
                     str(reject_path), str(run_dir / MANIFEST_NAME), artifacts)
    # [v4.2] 内部保留本次输入路径（不直接外泄），build_response 转任务相对路径供阶段首校验
    result["inputFile"] = str(input_path)
    return result


def run(input_file: str, unique_id: str, config_path: Optional[str] = None,
        output_root: Optional[str] = None,
        overrides: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
    """共享入口：返回 RunResult（本地路径），HTTP 由 app.py 转换。

    ``TaskBusyError``、``ConfigError``、``ValueError`` 均转换为可解释的失败结果，
    不向调用方抛出未分类异常。
    """
    try:
        cfg, snapshot = load_config(config_path, overrides, output_root)
    except ConfigError as exc:
        return _result("failed", None, {"error": "invalid_config"}, str(exc),
                       None, None, MAIN_NAME, None, None, [])
    try:
        root = Path(cfg.io.output_root).expanduser().resolve()
        task = task_dir(root, unique_id)
    except ValueError as exc:
        return _result("failed", None, {"error": "invalid_unique_id"}, str(exc),
                       None, None, MAIN_NAME, None, None, [])
    try:
        with TaskLock(root, unique_id):
            return _run_locked(cfg, snapshot, task, unique_id, input_file)
    except TaskBusyError as exc:
        return _result("failed", None, {"error": "task_busy"}, str(exc),
                       None, None, MAIN_NAME, None, None, [])


# --------------------------------------------------------------------------- #
# 响应转换与媒体校验
# --------------------------------------------------------------------------- #

#: [v4.2] 形态二统一响应中的算子英文标识（detection 别名空间成员）
OPERATOR_ENGLISH_ID = "extract_video_keyframe"

#: [v4.2] 形态二信封 data.status 固定文案（接口级字段，非输出记录；记录级一律 int status +
#: process_status，见 _mark_not_applicable/process_record/_business_summary）
ENVELOPE_STATUS_SUCCESS = "success"


def _business_summary(result: Dict[str, Any]) -> Dict[str, Any]:
    """形态二 ``data.result`` 业务摘要：内部绝对路径转任务相对路径（[v4.2] §3）。

    摘要记录遵循 status 双字段口径：int ``status``（1 处理成功）+ ``process_status``
    承载 run 级流程态（success/partial）。
    """
    summary: Dict[str, Any] = {
        "status": 1,
        "process_status": result.get("status"),
        "statistics": result.get("statistics") or {},
        "entrypoint": result.get("entrypoint"),
    }
    run_dir = result.get("runDir")
    input_file = result.get("inputFile")
    if run_dir and input_file:
        try:
            task = Path(run_dir).resolve().parent.parent
            summary["input"] = Path(input_file).resolve().relative_to(task).as_posix()
        except (ValueError, OSError):
            pass
    return summary


def build_response(result: Dict[str, Any], is_last: bool,
                   max_response_bytes: int = DEFAULT_MAX_RESPONSE_BYTES
                   ) -> Tuple[int, str, Dict[str, Any]]:
    """RunResult → (code, msg, data)。

    [v4.2] 形态二响应统一：不再区分阶段末/非阶段末，去除 ``lineageFile``，始终返回
    ``outputFile``（crossmodal-files-v1 文件内容对象）；``isLastOperator`` 保留严格校验
    （契约兼容）但不再改变响应形态；失败路径 ``data={"status": "failed"}``，
    不返回临时路径冒充文件内容。
    """
    status = result.get("status")
    message = result.get("message") or str(status)
    if status == "failed":
        return (1, "failed: %s" % message if not message.startswith("failed") else message,
                {"status": "failed"})

    run_dir = Path(result["runDir"]) if result.get("runDir") else None
    if run_dir is None or not run_dir.is_dir():
        return 1, "failed: 运行目录缺失，无法打包输出", {"status": "failed"}
    manifest = None
    try:
        manifest = load_manifest(run_dir) or build_manifest(
            result.get("entrypoint") or MAIN_NAME, result.get("artifacts") or [])
        payload = pack_file_object(run_dir, manifest["entrypoint"],
                                   iter_manifest_paths(manifest), max_response_bytes)
    except OutputTooLargeError as exc:
        # [11.3-F24] 内联文件内容对象超过响应上限时不再整单判失败：把本次产物
        # 落盘成 <runDir>_form2.zip，改返形态一同样的路径引用（mode=path-reference）。
        # v1.1.1 起默认上限 256MiB（原 64MiB）：138 条实跑 #6 产物 83,328,904 字节
        # 已能内联；812 条全量（单 run 约 900MB）仍超限，走本回退拿 ZIP 路径引用，
        # 语义与形态一一致，产物完整。
        try:
            reference = form1_package.build_reference(
                run_dir, (manifest or {}).get("entrypoint"),
                zip_path=run_dir.parent / (run_dir.name + "_form2.zip"))
        except (OSError, ValueError) as fallback_exc:
            return (1, "output_too_large: %s (回退落盘失败: %s)"
                    % (exc, fallback_exc), {"status": "failed"})
        return 0, message, {
            "operator": OPERATOR_ENGLISH_ID,
            "status": ENVELOPE_STATUS_SUCCESS,
            "outputFile": reference,
            "response_mode": "path-reference",
            "response_fallback": "output_too_large",
            "response_limit_bytes": int(max_response_bytes),
            "result": _business_summary(result),
        }
    except (TransportError, OSError) as exc:
        return 1, "transport_error: %s: %s" % (type(exc).__name__, exc), {"status": "failed"}
    return 0, message, {
        "operator": OPERATOR_ENGLISH_ID,
        "status": ENVELOPE_STATUS_SUCCESS,
        "outputFile": payload,
        "response_mode": "inline",
        "result": _business_summary(result),
    }


def verify_media_references(entry_path, run_dir) -> Dict[str, Any]:
    """核对主清单记录引用的媒体（含帧）在本次 run 目录中真实存在且非空。"""
    run_dir = Path(run_dir)
    checked = 0
    missing: List[str] = []
    empty: List[str] = []
    samples = 0
    for _lineno, record, error in read_jsonl_stream(entry_path):
        if error is not None:
            continue
        samples += 1
        references: List[str] = []
        for field in MEDIA_FIELDS:
            value = record.get(field)
            if isinstance(value, dict) and isinstance(value.get("path"), str):
                references.append(value["path"])
            elif isinstance(value, str) and value:
                references.append(value)
        video = record.get("video")
        if isinstance(video, dict):
            for frame in video.get("frames") or []:
                if isinstance(frame, dict) and isinstance(frame.get("path"), str):
                    references.append(frame["path"])
        for relative in references:
            try:
                target = resolve_media(run_dir, relative)
            except UnsupportedMediaPath:
                missing.append(relative)
                continue
            if not target.is_file():
                missing.append(relative)
            elif target.stat().st_size == 0:
                empty.append(relative)
            else:
                checked += 1
    return {"samples": samples, "checked": checked, "missing": missing, "empty": empty,
            "ok": not missing and not empty}


# --------------------------------------------------------------------------- #
# CLI
# --------------------------------------------------------------------------- #

def _parse_overrides(items: Optional[List[str]]) -> Dict[str, Any]:
    overrides: Dict[str, Any] = {}
    for item in items or []:
        if "=" not in item:
            raise ConfigError("--override 需要 KEY=VALUE 形式：%s" % item)
        key, _, raw = item.partition("=")
        key = key.strip()
        if not key:
            raise ConfigError("--override 的键不能为空")
        try:
            overrides[key] = json.loads(raw)
        except json.JSONDecodeError:
            overrides[key] = raw
    return overrides


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="算子 5 视频关键帧与片段处理：抽取关键帧并输出完整记录清单")
    parser.add_argument("--input", required=True, help="输入 JSONL（或含 manifest 的目录）")
    parser.add_argument("--unique-id", required=True, help="任务标识（单个目录名）")
    parser.add_argument("--output-root", default=None, help="输出根目录（默认取配置）")
    parser.add_argument("--config", default=None, help="配置文件路径（默认 configs/config.yaml）")
    parser.add_argument("--is-last", action="store_true", help="按阶段末模式输出文件内容对象")
    parser.add_argument("--override", action="append", default=None,
                        help="覆盖业务参数，可重复：--override sample_fps=0.5")
    parser.add_argument("--quiet", action="store_true", help="只输出精简摘要")
    return parser


def _force_utf8_console() -> None:
    """Windows 控制台默认 GBK，显式改为 UTF-8，保证 JSON 输出可被管道读取。"""
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(encoding="utf-8")
        except Exception:  # noqa: BLE001 - 不支持 reconfigure 时忽略
            pass


def _shape_label(value: Any) -> str:
    """描述 data 中各字段的实际类型，避免摘要与真实信封不一致。"""
    if isinstance(value, dict):
        return "object"
    if isinstance(value, list):
        return "array"
    if value is None:
        return "null"
    return type(value).__name__


def main(argv: Optional[List[str]] = None) -> int:
    _force_utf8_console()
    args = build_parser().parse_args(argv)
    config_path = args.config or str(DIR / "configs" / "config.yaml")
    try:
        overrides = _parse_overrides(args.override)
    except ConfigError as exc:
        print(json.dumps({"code": 1, "msg": str(exc), "data": {}}, ensure_ascii=False))
        return 1

    result = run(args.input, args.unique_id, config_path=config_path,
                 output_root=args.output_root, overrides=overrides)
    code, message, data = build_response(result, args.is_last)
    # [v4.2] 血缘输出已停用：摘要不再含 lineageFile（run() 结果亦无此键）
    if args.quiet:
        summary = {
            "code": code,
            "msg": message,
            "status": result.get("status"),
            "outputFile": result.get("outputFile"),
            "statistics": result.get("statistics"),
        }
    else:
        summary = {"code": code, "msg": message, "status": result.get("status"),
                   "runId": result.get("runId"), "runDir": result.get("runDir"),
                   "outputFile": result.get("outputFile"),
                   "rejectsFile": result.get("rejectsFile"),
                   "statistics": result.get("statistics"),
                   "dataShape": {key: _shape_label(value)
                                 for key, value in data.items()} if code == 0 else {}}
    print(json.dumps(summary, ensure_ascii=False, indent=2))
    return 0 if code == 0 else 1


if __name__ == "__main__":
    raise SystemExit(main())
