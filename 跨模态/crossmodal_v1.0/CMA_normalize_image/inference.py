"""算子 4 图像标准化处理：配置加载、主处理流程与 CLI。

职责（DEVELOPMENT_PLAN §1、TECHNICAL_DESIGN §1）：
- 输入：S2 阶段内上游的 03_text_cleaned.jsonl（HTTP inputFile 取上游血缘 outputFile）；
- 输出：04_image_index.jsonl（主清单，每行完整记录）、04_images/、04_rejects.jsonl；
- 对本节点不适用的合法记录（音视频组合、纯文本）透传并标记 not_applicable；
- 保留 sample_id、processing、alignment、qc 与未知 metadata，媒体随输出搬运；
- 本算子只处理图像，不做文本/视频/音频业务判定。
"""
from __future__ import annotations

import argparse
import copy
import json
import os
import re
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

from typing import Any, Dict, Iterable, List, Optional, Tuple

import yaml
from pydantic import ValidationError

#: 内部别名，便于在记录处理中调用
_deepcopy = copy.deepcopy
_copyfile = shutil.copyfile

sys.path.insert(0, str(Path(__file__).resolve().parent))

from utils import image_processing as ip  # noqa: E402
from utils.io import (JsonlWriter, TaskBusyError, TaskLock,  # noqa: E402
                      UnsupportedMediaPath, assert_utf8, atomic_write_json,
                      cleanup_files, next_run_id, read_jsonl_stream, resolve_in_task,
                      resolve_media, task_dir)
# [v4.2] 血缘/阶段归档已停用：make_lineage/append_lineage/build_stage_context/
# load_prior_lineage/to_stage_lineage 不再导入与调用（utils/lineage.py 保留存档）；
# now_iso 仅为时间工具，继续保留。
from utils.lineage import now_iso  # noqa: E402
from utils.logger import close_logger, get_logger, log_action  # noqa: E402
from utils.schemas import (BUSINESS_CONFIG_KEYS, DEPLOYMENT_CONFIG_KEYS,  # noqa: E402
                           OperatorConfig, ensure_finite_numbers)
from utils.transport import build_manifest, write_manifest  # noqa: E402

BASE_DIR = Path(__file__).resolve().parent

MAIN_NAME = "04_image_index.jsonl"
REJECT_NAME = "04_rejects.jsonl"
IMAGES_DIR = "04_images"
ORIGINAL_REL_DIR = "media/original"
MANIFEST_NAME = "manifest.json"
OPERATOR_DIR_NAME = "image_standardization"
MEDIA_FIELDS = ("image", "video", "audio")


# --------------------------------------------------------------------------- #
# 配置
# --------------------------------------------------------------------------- #

def resolve_output_root(cfg: OperatorConfig) -> Path:
    """输出根目录：``OUTPUT_DIR`` 环境变量优先，否则用配置 ``io.output_root``。"""
    return Path(os.getenv("OUTPUT_DIR") or str(BASE_DIR / cfg.io.output_root))


def load_config(config_path: Optional[str] = None,
                overrides: Optional[Dict[str, Any]] = None) -> Tuple[OperatorConfig, Dict[str, Any]]:
    """加载并校验配置。

    ``operator`` / ``stage_id`` / ``io`` / ``server`` 属部署配置：请求 ``overrides``
    中的这些键会被忽略，防止请求改写血缘身份或放宽服务端限额。
    """
    path = Path(config_path) if config_path else BASE_DIR / "configs" / "config.yaml"
    try:
        raw = yaml.safe_load(path.read_text(encoding="utf-8"))
    except (OSError, yaml.YAMLError) as exc:
        raise ValueError("配置文件无法读取：%s" % type(exc).__name__) from exc
    if raw is None:
        raw = {}
    if not isinstance(raw, dict):
        raise ValueError("配置文件必须是对象")

    merged: Dict[str, Any] = dict(raw)
    applied: List[str] = []
    ignored: List[str] = []
    #: [v4.2] §4.5 规范参数名 max_long_edge → 内部实现参数名 max_side
    aliases = {"max_long_edge": "max_side"}
    for key, value in (overrides or {}).items():
        key = aliases.get(key, key)
        if key in DEPLOYMENT_CONFIG_KEYS:
            ignored.append(key)
            continue
        if key not in BUSINESS_CONFIG_KEYS:
            raise ValueError("表1不允许覆盖参数：%s" % key)
        merged[key] = value
        applied.append(key)

    ensure_finite_numbers(merged)
    cfg = OperatorConfig(**merged)

    full = cfg.model_dump()
    business = {key: full[key] for key in BUSINESS_CONFIG_KEYS if key in full}
    snapshot = {
        "business": business,
        "deployment": {
            "operator": full["operator"],
            "stage_id": full["stage_id"],
            "io": full["io"],
            "server": full["server"],
        },
        "overridden_keys": sorted(applied),
        "ignored_deployment_keys": sorted(ignored),
    }
    return cfg, snapshot


# --------------------------------------------------------------------------- #
# 媒体引用
# --------------------------------------------------------------------------- #

def collect_media_refs(record: Dict[str, Any]) -> List[Tuple[str, str]]:
    """收集记录仍引用的媒体相对路径，返回 ``(引用位置, 相对路径)``。"""
    refs: List[Tuple[str, str]] = []
    for field in MEDIA_FIELDS:
        value = record.get(field)
        if isinstance(value, str) and value.strip():
            refs.append((field, value.strip()))
        elif isinstance(value, dict):
            for key in ("path", "original_path"):
                item = value.get(key)
                if isinstance(item, str) and item.strip():
                    refs.append(("%s.%s" % (field, key), item.strip()))
            if field == "video":
                for index, frame in enumerate(value.get("frames") or []):
                    if isinstance(frame, dict) and isinstance(frame.get("path"), str):
                        refs.append(("video.frames[%d]" % index, frame["path"]))
            if field == "audio":
                for index, segment in enumerate(value.get("segments") or []):
                    if isinstance(segment, dict) and isinstance(segment.get("path"), str):
                        refs.append(("audio.segments[%d]" % index, segment["path"]))
    return refs


def verify_stage_media(entry_path, run_dir: Path) -> List[str]:
    """校验主清单里所有媒体引用在本次 run 目录内存在且非空（阶段末门禁）。"""
    problems: List[str] = []
    for _lineno, record, error in read_jsonl_stream(entry_path):
        if error:
            continue
        for ref, relative in collect_media_refs(record):
            try:
                target = resolve_media(run_dir, relative)
            except UnsupportedMediaPath:
                problems.append("%s:%s" % (record.get("sample_id"), ref))
                continue
            if not target.is_file() or target.stat().st_size == 0:
                problems.append("%s:%s" % (record.get("sample_id"), ref))
    return problems


def _image_reference(value: Any) -> Tuple[Optional[str], Optional[str]]:
    """取图像相对路径，返回 ``(相对路径或 None, 错误原因或 None)``。"""
    if value is None:
        return None, None
    if isinstance(value, str):
        candidate = value.strip()
        return (candidate, None) if candidate else (None, "invalid_type:image")
    if isinstance(value, dict):
        if "path" not in value:
            return None, None
        candidate = value.get("path")
        if not isinstance(candidate, str) or not candidate.strip():
            return None, "invalid_type:image.path"
        return candidate.strip(), None
    return None, "invalid_type:image"


def _no_image_reason(record: Dict[str, Any]) -> str:
    """图像缺失时判断该记录是否属于本算子不适用的合法形态。"""
    if record.get("video") and record.get("audio"):
        return "video_audio_pair"
    text = record.get("text")
    if isinstance(text, str) and text.strip():
        return "text_only"
    if isinstance(text, dict) and str(text.get("content") or "").strip():
        return "text_only"
    return "no_applicable_modality"


def _sanitize_name(sample_id: str) -> str:
    cleaned = re.sub(r"[^A-Za-z0-9._-]+", "_", sample_id).strip("._")
    return cleaned[:80] or "sample"


def _unique_name(sample_id: str, used: Dict[str, int]) -> str:
    """由 sample_id 生成文件基名；净化后同名时追加序号，避免互相覆盖。"""
    base = _sanitize_name(sample_id)
    name = base
    if name in used:
        index = 2
        while "%s_%d" % (base, index) in used:
            index += 1
        name = "%s_%d" % (base, index)
    used[name] = 1
    return name


def _reject(sample_id: Optional[str], line: int, reason: str,
            record: Optional[dict] = None, evidence: Optional[dict] = None) -> Dict[str, Any]:
    entry: Dict[str, Any] = {
        "sample_id": sample_id,
        "operator": OPERATOR_DIR_NAME,
        "line": line,
        "reason": reason,
        "destination": "原始隔离区", "tag": "原始文件-损坏",
    }
    if record is not None:
        entry["record"] = record
    if evidence:
        entry["evidence"] = evidence
    return entry


def _bump(stats: Dict[str, Any], key: str, amount: int = 1) -> None:
    stats[key] = stats.get(key, 0) + amount


def _add_reason(stats: Dict[str, Any], reason: str) -> None:
    _bump(stats, "reason:%s" % reason)


def _null_ref(record: Dict[str, Any], ref: str) -> None:
    """把无法搬运/处理的媒体引用置 null，避免留下指向不存在文件的活跃引用。"""
    if ref in MEDIA_FIELDS:
        record[ref] = None
        return
    field, _, key = ref.partition(".")
    value = record.get(field)
    if not isinstance(value, dict):
        return
    if key in ("path", "original_path"):
        value[key] = None
        return
    for name in ("frames", "segments"):
        prefix = name + "["
        if key.startswith(prefix) and key.endswith("]"):
            try:
                index = int(key[len(prefix):-1])
            except ValueError:
                return
            items = value.get(name)
            if isinstance(items, list) and 0 <= index < len(items) and isinstance(items[index], dict):
                items[index]["path"] = None
            return


def _carry_media(record: Dict[str, Any], media_root: Path, run_dir: Path,
                 copied: Dict[str, str], stats: Dict[str, Any],
                 skip: Iterable[str] = ()) -> Tuple[Optional[str], Dict[str, Any], Dict[str, str]]:
    """校验并搬运记录引用的媒体；返回 ``(错误原因, 搬运副本, 出错引用)``。

    ``skip`` 中的顶层字段（如 ``image``）由调用方单独处理，不在此搬运。
    """
    skip_fields = set(skip)
    carried: Dict[str, Any] = _deepcopy(record)
    errors: Dict[str, str] = {}
    for ref, relative in collect_media_refs(record):
        if ref.split(".")[0] in skip_fields:
            continue
        try:
            source = resolve_media(media_root, relative)
        except UnsupportedMediaPath:
            errors[ref] = relative
            _null_ref(carried, ref)
            return "media_path_unsupported:%s" % ref, carried, errors
        if not source.is_file() or source.stat().st_size == 0:
            _bump(stats, "missing_media")
            errors[ref] = relative
            _null_ref(carried, ref)
            return "media_missing:%s" % ref, carried, errors
        if relative not in copied:
            destination = run_dir / relative
            destination.parent.mkdir(parents=True, exist_ok=True)
            _copyfile(source, destination)
            copied[relative] = "media"
            _bump(stats, "media_copied")
    return None, carried, errors


def _build_image_object(record: Dict[str, Any], result: Dict[str, Any], output_rel: str,
                        original_rel: str) -> Dict[str, Any]:
    """在保留上游 image 字段的前提下，写入本算子的标准化结果。"""
    previous = record.get("image")
    image_object: Dict[str, Any] = dict(previous) if isinstance(previous, dict) else {}
    transform = result["transform"]
    image_object.update({
        "path": output_rel,
        "original_path": original_rel,
        "width": result["width"],
        "height": result["height"],
        "format": result["format"],
        "original_width": transform["original_width"],
        "original_height": transform["original_height"],
        "original_format": transform["original_format"],
        "transform": transform,
    })
    return image_object


def process_record(record: Dict[str, Any], line: int, business_cfg: Dict[str, Any],
                   media_root: Path, run_dir: Path, copied: Dict[str, str],
                   generated: Dict[str, str], used_names: Dict[str, int],
                   stats: Dict[str, Any]) -> Tuple[str, Dict[str, Any]]:
    """处理单条有效记录，返回 ``(去向, 记录或异常条目)``。

    去向：``output``（主清单，含 not_applicable）、``reject``（异常清单）。
    """
    if not business_cfg.get("enabled", False):
        out = _deepcopy(record)
        for _, relative in collect_media_refs(record):
            try:
                source = resolve_media(media_root, relative)
                if source.is_file() and relative not in copied:
                    dest = run_dir / relative
                    dest.parent.mkdir(parents=True, exist_ok=True)
                    _copyfile(source, dest)
                    copied[relative] = "media"
            except UnsupportedMediaPath:
                pass
        _bump(stats, "not_applicable")
        return "output", out

    sample_id = record.get("sample_id")
    if sample_id is None:
        return "reject", _reject(None, line, "missing_field:sample_id", record=record)
    if not isinstance(sample_id, str) or not sample_id.strip():
        return "reject", _reject(None, line, "invalid_type:sample_id", record=record)

    processing = record.get("processing")
    if processing is not None and not isinstance(processing, dict):
        return "reject", _reject(sample_id, line, "invalid_type:processing", record=record)

    image_value = record.get("image")
    if image_value is not None and not isinstance(image_value, (str, dict)):
        return "reject", _reject(sample_id, line, "invalid_type:image", record=record)

    image_ref, ref_error = _image_reference(image_value)
    if ref_error:
        return "reject", _reject(sample_id, line, ref_error, record=record)

    # --- 无图像：合法不适用形态透传，其余为无适用模态 ---
    if image_ref is None:
        reason = _no_image_reason(record)
        if reason == "no_applicable_modality":
            return "reject", _reject(sample_id, line, reason, record=record)
        carried_error, carried, errors = _carry_media(
            record, media_root, run_dir, copied, stats)
        if carried_error:
            return "reject", _reject(sample_id, line, carried_error, record=carried,
                                     evidence={"media_errors": errors})
        out = carried
        out.setdefault("processing", {})
        out["processing"]["04"] = {
            # [v4.2] 输出记录 status 双字段：int status（1=成功/通过）+ process_status 流程态
            "status": 1,
            "process_status": "not_applicable",
            "reason": reason,
            "metrics": {"images": 0},
        }
        _bump(stats, "not_applicable")
        _add_reason(stats, reason)
        return "output", out

    # --- 有图像：先搬运非图像媒体（视频/音频透传），再标准化图像 ---
    carried_error, carried, errors = _carry_media(
        record, media_root, run_dir, copied, stats, skip=("image",))
    if carried_error:
        return "reject", _reject(sample_id, line, carried_error, record=carried,
                                 evidence={"media_errors": errors})

    try:
        source = resolve_media(media_root, image_ref)
    except UnsupportedMediaPath:
        _null_ref(carried, "image")
        return "reject", _reject(sample_id, line, "media_path_unsupported:image",
                                 record=carried,
                                 evidence={"source_reference": {"image": image_ref}})
    if not source.is_file() or source.stat().st_size == 0:
        _bump(stats, "missing_media")
        _null_ref(carried, "image")
        return "reject", _reject(sample_id, line, "media_missing:image", record=carried,
                                 evidence={"source_reference": {"image": image_ref}})

    stem = _unique_name(sample_id, used_names)
    output_ext = ip.suffix_for_format(business_cfg["output_format"])
    original_ext = source.suffix.lower() or ".bin"
    output_rel = "%s/%s%s" % (IMAGES_DIR, stem, output_ext)
    original_rel = "%s/%s%s" % (ORIGINAL_REL_DIR, stem, original_ext)
    output_path = run_dir / output_rel
    original_path = run_dir / original_rel

    try:
        original_path.parent.mkdir(parents=True, exist_ok=True)
        _copyfile(source, original_path)
        generated[original_rel] = "media"
        result = ip.standardize_image(source, output_path, business_cfg)
    except ip.ImageProcessingError as exc:
        _bump(stats, "image_processing_failed")
        cleanup_files(original_path, output_path)
        generated.pop(original_rel, None)
        _null_ref(carried, "image")
        return "reject", _reject(sample_id, line, "image_%s" % exc.reason, record=carried,
                                 evidence={"detail": exc.detail,
                                           "source_reference": {"image": image_ref}})
    except OSError as exc:
        _bump(stats, "image_processing_failed")
        cleanup_files(original_path, output_path)
        generated.pop(original_rel, None)
        _null_ref(carried, "image")
        return "reject", _reject(sample_id, line, "image_io_error", record=carried,
                                 evidence={"detail": type(exc).__name__,
                                           "source_reference": {"image": image_ref}})

    generated[output_rel] = "media"
    out = carried
    out.setdefault("processing", {})
    out["image"] = _build_image_object(record, result, output_rel, original_rel)
    out["processing"]["04"] = {
        # [v4.2] 输出记录 status 双字段：int status（1=成功）+ process_status 流程态
        "status": 1,
        "process_status": "success",
        "reason": None,
        "metrics": {
            "images": 1,
            "width": result["width"],
            "height": result["height"],
            "resized": result["transform"]["resized"],
                        "original_format": result["transform"]["original_format"],
        },
    }
    _bump(stats, "images_standardized")
    _bump(stats, "originals_copied")
    if result["transform"]["resized"]:
        _bump(stats, "images_resized")
    else:
        _bump(stats, "images_size_kept")
    return "output", out


# --------------------------------------------------------------------------- #
# 运行流程
# --------------------------------------------------------------------------- #

def _initial_stats() -> Dict[str, Any]:
    return {
        "total_lines": 0,
        "valid_records": 0,
        "bad_lines": 0,
        "output": 0,
        "rejected": 0,
        "not_applicable": 0,
        "flagged": 0,
        "images_standardized": 0,
        "images_resized": 0,
        "images_size_kept": 0,
        "originals_copied": 0,
        "blur_flagged": 0,
        "blur_rejected": 0,
        "image_processing_failed": 0,
        "media_copied": 0,
        "missing_media": 0,
        "duplicates": 0,
    }


def _resolve_media_root(input_path: Path) -> Path:
    """媒体根目录：输入文件向上最外层含 ``manifest.json`` 的目录，否则为其父目录。"""
    directory = input_path.parent
    root = directory
    while True:
        if (directory / MANIFEST_NAME).is_file():
            root = directory
        parent = directory.parent
        if parent == directory:
            break
        directory = parent
    return root


# [v4.2] 血缘/阶段归档已停用：历史血缘读取函数归档，不再调用（其依赖的
# load_prior_lineage / STAGE_CONTEXT_NAME 已不再导入）。
# def load_stage_history(input_path: Path, task_root: Path) -> Tuple[List[dict], List[dict]]:
#     """读取输入携带的历史阶段与当前阶段已完成节点。"""
#     candidates = [input_path.parent, task_root / "input"]
#     for directory in candidates:
#         context_path = directory / STAGE_CONTEXT_NAME
#         if not context_path.is_file():
#             continue
#         try:
#             payload = json.loads(context_path.read_text(encoding="utf-8"))
#         except (json.JSONDecodeError, OSError):
#             continue
#         if not isinstance(payload, dict):
#             continue
#         prior = [item for item in (payload.get("prior_lineage") or [])
#                  if isinstance(item, dict)]
#         stage = [item for item in (payload.get("stage_lineage") or [])
#                  if isinstance(item, dict)]
#         if prior or stage:
#             return prior, stage
#     return load_prior_lineage(candidates), []


def _failed_result(unique_id: Optional[str], stage_id: str, started_at: str, code: str,
                   message: str, cfg: Optional[OperatorConfig] = None,
                   snapshot: Optional[Dict[str, Any]] = None,
                   input_file: Optional[str] = None, run_id: Optional[str] = None,
                   run_dir: Optional[Path] = None,
                   stats: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
    """结构化失败结果：不写入产物、不冒充成功，但仍携带守恒统计。"""
    statistics = stats if stats is not None else _initial_stats()
    statistics.setdefault("output", 0)
    statistics.setdefault("rejected", 0)
    statistics.setdefault("valid_records", 0)
    statistics.setdefault("encoding", "utf-8")
    statistics["conservation_ok"] = bool(
        statistics.get("output", 0) + statistics.get("rejected", 0)
        == statistics.get("valid_records", 0))
    return {
        "ok": False,
        "status": "failed",
        "code": code,
        "message": message,
        "operator": OPERATOR_DIR_NAME,
        "operator_version": (cfg.operator.version if cfg else "v1.0.0"),
        "stage_id": stage_id,
        "unique_id": unique_id,
        "run_id": run_id,
        "input_file": input_file,
        "output_file": None,
        "started_at": started_at,
        "finished_at": now_iso(),
        "statistics": statistics,
        "config": (snapshot or {}),
        # [v4.2] 血缘输出已停用：run() 返回不再携带 lineage/stage_context 键。
        # "lineage": None,
        # "stage_context": None,
        "artifacts": [],
        "media_problems": [],
        "is_last_operator": False,
        "run_dir": str(run_dir) if run_dir is not None else None,
    }


def _run_locked(cfg: OperatorConfig, snapshot: Dict[str, Any], root: Path,
                unique_id: str, input_file: Optional[str], last_operator: bool,
                started_at: str) -> Dict[str, Any]:
    stats = _initial_stats()
    stage_id = cfg.stage_id

    if not input_file:
        return _failed_result(unique_id, stage_id, started_at, "missing_input_file",
                              "缺少 inputFile", cfg, snapshot,
                              input_file=input_file, stats=stats)

    task = task_dir(root, unique_id)
    if not ip.pillow_available():
        return _failed_result(unique_id, stage_id, started_at, "dependency_missing:pillow",
                              "缺少 Pillow，无法解码图像", cfg, snapshot,
                              input_file=input_file, stats=stats)

    external_input = False
    input_path: Optional[Path] = None
    try:
        in_task_candidate = resolve_in_task(task, input_file)
    except ValueError:
        in_task_candidate = None
    if in_task_candidate is not None and in_task_candidate.is_file():
        input_path = in_task_candidate
    else:
        # CLI 便捷路径：本地 fixture 可位于任务目录之外（HTTP 层已用 resolve_in_task
        # 强制 inputFile 位于任务目录内，此处只读取既有文件，不写入其所在目录）。
        fallback = Path(input_file)
        if not fallback.is_absolute():
            fallback = BASE_DIR / fallback
        input_path = fallback.resolve()
        external_input = True
    if not input_path.is_file():
        return _failed_result(unique_id, stage_id, started_at, "input_file_not_found",
                              "inputFile 不存在：%s" % input_path.name, cfg, snapshot,
                              input_file=input_file, stats=stats)

    try:
        assert_utf8(input_path)
    except UnicodeDecodeError:
        return _failed_result(unique_id, stage_id, started_at, "invalid_input_encoding",
                              "输入不是严格 UTF-8", cfg, snapshot,
                              input_file=input_file, stats=stats)

    operator_dir = task / OPERATOR_DIR_NAME
    run_id = next_run_id(operator_dir)
    run_dir = operator_dir / run_id
    run_dir.mkdir(parents=True, exist_ok=True)
    log_path = run_dir / "run.log"
    logger = get_logger("image_standardization", log_path)
    media_root = _resolve_media_root(input_path)
    # [v4.2] 血缘已停用：不再读取历史阶段血缘归档（load_stage_history 保留存档）。
    # prior_lineage, stage_records = load_stage_history(input_path, task)

    business_cfg = snapshot["business"]
    copied: Dict[str, str] = {}
    generated: Dict[str, str] = {}
    used_names: Dict[str, int] = {}
    seen_sample_ids: Dict[str, int] = {}

    log_action(logger, "配置", "图像兜底已开启：PIL解码，不评清晰度" if cfg.enabled else "图像兜底关闭，不执行")
    log_action(logger, "RUN_START", "run=%s input=%s external=%s" % (
        run_id, input_path.name, external_input))
    main_path = run_dir / MAIN_NAME
    reject_path = run_dir / REJECT_NAME
    encoding_error = False
    main_writer = JsonlWriter(main_path)
    reject_writer = JsonlWriter(reject_path)
    try:
        for lineno, record, error in read_jsonl_stream(input_path):
            stats["total_lines"] = lineno
            if error:
                stats["bad_lines"] += 1
                _add_reason(stats, error)
                reject_writer.write(_reject(None, lineno, error))
                continue
            stats["valid_records"] += 1
            sample_id = record.get("sample_id")
            if isinstance(sample_id, str) and sample_id in seen_sample_ids:
                stats["duplicates"] += 1
                stats["rejected"] += 1
                _add_reason(stats, "duplicate_sample_id")
                reject_writer.write(_reject(
                    sample_id, lineno, "duplicate_sample_id", record=record,
                    evidence={"first_line": seen_sample_ids[sample_id]}))
                continue
            if isinstance(sample_id, str):
                seen_sample_ids[sample_id] = lineno
            destination, payload = process_record(
                record, lineno, business_cfg, media_root, run_dir,
                copied, generated, used_names, stats)
            if destination == "output":
                main_writer.write(payload)
                stats["output"] += 1
            else:
                reject_writer.write(payload)
                stats["rejected"] += 1
                _add_reason(stats, str(payload.get("reason") or "unknown"))
    except UnicodeDecodeError:
        encoding_error = True
    finally:
        main_writer.close()
        reject_writer.close()

    if encoding_error:
        cleanup_files(main_path, reject_path, log_path)
        close_logger(logger)
        return _failed_result(unique_id, stage_id, started_at, "invalid_input_encoding",
                              "输入不是严格 UTF-8", cfg, snapshot, input_file=input_file,
                              run_id=run_id, run_dir=run_dir, stats=stats)

    # 空输入必须失败并删除空产物，绝不用空数组冒充成功
    if stats["total_lines"] == 0:
        cleanup_files(main_path, reject_path, log_path)
        close_logger(logger)
        return _failed_result(unique_id, stage_id, started_at, "empty_input",
                              "输入为空，未产生任何记录", cfg, snapshot,
                              input_file=input_file, run_id=run_id, run_dir=run_dir,
                              stats=stats)

    artifacts: List[Dict[str, str]] = [{"path": MAIN_NAME, "role": "main"}]
    if stats["rejected"] > 0 and reject_path.is_file():
        artifacts.append({"path": REJECT_NAME, "role": "sidecar"})
    else:
        cleanup_files(reject_path)
    media_paths: Dict[str, str] = {}
    for relative in list(copied) + list(generated):
        media_paths[relative] = "media"
    for relative in sorted(media_paths):
        artifacts.append({"path": relative, "role": media_paths[relative]})

    manifest = build_manifest(MAIN_NAME, artifacts)
    write_manifest(run_dir, manifest)
    publish_stage_output(run_dir)

    stats["encoding"] = "utf-8"
    stats["output_format"] = business_cfg["output_format"]
    stats["max_side"] = business_cfg["max_side"]
    stats["allow_upscale"] = False
    stats["conservation_ok"] = bool(
        stats["output"] + stats["rejected"] == stats["valid_records"])

    if stats["output"] == 0:
        status, code = "partial", "no_output"
        message = "处理 %d 条：输出 0、拒绝 %d、坏行 %d" % (
            stats["total_lines"], stats["rejected"], stats["bad_lines"])
    elif stats["rejected"] or stats["bad_lines"]:
        status, code = "partial", "ok_with_rejects"
        message = "处理 %d 条：输出 %d（含不适用 %d）、拒绝 %d、坏行 %d" % (
            stats["total_lines"], stats["output"], stats["not_applicable"],
            stats["rejected"], stats["bad_lines"])
    else:
        status, code = "success", "ok"
        message = "处理 %d 条：输出 %d（含不适用 %d）" % (
            stats["total_lines"], stats["output"], stats["not_applicable"])

    finished_at = now_iso()
    # [v4.2] 血缘/阶段归档已停用：不再构造血缘记录、追加血缘快照或写 stage_context.json。
    # record = make_lineage(
    #     operator=OPERATOR_DIR_NAME,
    #     version=cfg.operator.version,
    #     unique_id=unique_id,
    #     stage_id=stage_id,
    #     run_id=run_id,
    #     input_file=input_file,
    #     output_file=str(main_path),
    #     status=status,
    #     started_at=started_at,
    #     finished_at=finished_at,
    #     config=snapshot,
    #     statistics=stats,
    #     artifacts=artifacts,
    # )
    # append_lineage(task / LINEAGE_NAME, record)
    #
    # current_stage = list(stage_records) + [record]
    # stage_lineage = to_stage_lineage(current_stage) if last_operator else current_stage
    # atomic_write_json(run_dir / LINEAGE_NAME, list(prior_lineage) + current_stage)
    # stage_context = build_stage_context(stage_id, unique_id, run_id, MAIN_NAME,
    #                                     stage_lineage, prior_lineage)
    # atomic_write_json(run_dir / STAGE_CONTEXT_NAME, stage_context)

    log_action(logger, "RUN_DONE",
               "run=%s status=%s output=%d rejected=%d bad=%d not_applicable=%d" % (
                   run_id, status, stats["output"], stats["rejected"],
                   stats["bad_lines"], stats["not_applicable"]))
    close_logger(logger)

    return {
        "ok": status in ("success", "partial"),
        "status": status,
        "code": code,
        "message": message,
        "operator": OPERATOR_DIR_NAME,
        "operator_version": cfg.operator.version,
        "stage_id": stage_id,
        "unique_id": unique_id,
        "run_id": run_id,
        "input_file": input_file,
        "output_file": str(main_path),
        "started_at": started_at,
        "finished_at": finished_at,
        "statistics": stats,
        "config": snapshot,
        # [v4.2] 血缘输出已停用：run() 返回不再携带 lineage/stage_context 键。
        # "lineage": record,
        # "stage_context": stage_context,
        "artifacts": artifacts,
        "media_problems": [],
        "is_last_operator": bool(last_operator),
        "run_dir": str(run_dir),
    }


def run(input_file: Optional[str], unique_id: str, last_operator: bool = False,
        output_root: Optional[str] = None, *, config_path: Optional[str] = None,
        overrides: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
    """执行一次图像标准化处理。

    ``output_root`` 为空时使用 ``OUTPUT_DIR`` 环境变量或配置 ``io.output_root``；
    返回值是结构化结果（含血缘、统计与守恒信息），不抛业务异常。
    """
    started_at = now_iso()
    try:
        cfg, snapshot = load_config(config_path, overrides)
    except (ValueError, ValidationError) as exc:
        return _failed_result(unique_id, "S2", started_at, "invalid_config", _explain(exc))

    root = Path(output_root) if output_root is not None else resolve_output_root(cfg)
    root = Path(root).expanduser().resolve()
    root.mkdir(parents=True, exist_ok=True)

    try:
        with TaskLock(root, unique_id):
            return _run_locked(cfg, snapshot, root, unique_id, input_file,
                               last_operator, started_at)
    except TaskBusyError as exc:
        return _failed_result(unique_id, cfg.stage_id, started_at, "task_busy",
                              str(exc), cfg, snapshot, input_file=input_file)
    except (ValueError, ValidationError) as exc:
        return _failed_result(unique_id, cfg.stage_id, started_at, "invalid_request",
                              _explain(exc), cfg, snapshot, input_file=input_file)


def _explain(exc: BaseException) -> str:
    if isinstance(exc, ValidationError):
        first = exc.errors()[0]
        location = ".".join(str(part) for part in first.get("loc", ()))
        return "配置校验失败 %s: %s" % (location, first.get("msg", ""))
    return str(exc)


# --------------------------------------------------------------------------- #
# CLI
# --------------------------------------------------------------------------- #

def build_arg_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="算子 4 图像标准化处理：读取上游 JSONL，输出 04_image_index.jsonl")
    parser.add_argument("--input", required=True, help="输入 JSONL 路径")
    parser.add_argument("--unique-id", required=True, dest="unique_id", help="任务 uniqueId")
    parser.add_argument("--output-root", default=None, dest="output_root",
                        help="输出根目录（默认 OUTPUT_DIR 或配置 io.output_root）")
    parser.add_argument("--config", default=None, help="配置文件路径")
    parser.add_argument("--override", action="append", default=[], metavar="KEY=VALUE",
                        help="覆盖业务参数，可重复，值为 JSON 字面量")
    parser.add_argument("--last-operator", action="store_true", dest="last_operator",
                        help="标记为阶段末算子，返回剥离临时路径的阶段血缘")
    return parser


def parse_overrides(items: Iterable[str]) -> Dict[str, Any]:
    result: Dict[str, Any] = {}
    for item in items:
        key, separator, raw = str(item).partition("=")
        key = key.strip()
        if not separator or not key:
            raise ValueError("--override 需要 KEY=VALUE 形式：%s" % item)
        try:
            result[key] = json.loads(raw)
        except json.JSONDecodeError:
            result[key] = raw
    return result


def main(argv: Optional[List[str]] = None) -> int:
    for stream in (sys.stdout, sys.stderr):
        try:  # Windows 控制台默认 cp936，显式切到 UTF-8 以正确显示中文
            stream.reconfigure(encoding="utf-8")
        except (AttributeError, ValueError):
            pass
    args = build_arg_parser().parse_args(argv)
    try:
        overrides = parse_overrides(args.override)
        result = run(args.input, args.unique_id, args.last_operator, args.output_root,
                     config_path=args.config, overrides=overrides)
    except (ValueError, TaskBusyError) as exc:
        print(json.dumps({"ok": False, "status": "failed", "code": "invalid_request",
                          "message": str(exc)}, ensure_ascii=False))
        return 1

    payload = {
        "ok": result["ok"],
        "status": result["status"],
        "code": result["code"],
        "message": result["message"],
        "operator": result["operator"],
        "stageId": result["stage_id"],
        "uniqueId": result["unique_id"],
        "runId": result.get("run_id"),
        "inputFile": result.get("input_file"),
        "outputFile": result.get("output_file"),
        "statistics": result["statistics"],
        # [v4.2] 血缘输出已停用：CLI 摘要不再包含 lineage/stageContext。
        "artifacts": result.get("artifacts", []),
        "runDir": result.get("run_dir"),
    }
    print(json.dumps(payload, ensure_ascii=False, indent=2))
    return 0 if result["status"] in ("success", "partial") else 1


if __name__ == "__main__":
    raise SystemExit(main())

