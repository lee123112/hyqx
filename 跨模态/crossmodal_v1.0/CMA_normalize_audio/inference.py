"""算子 6：音频标准化 —— 配置加载、处理流程（run）与 CLI。

职责（DEVELOPMENT_PLAN §2）：
- ``load_config``：YAML + 请求覆盖统一校验，API 与 CLI 共用；
- ``run``：流式读完整记录 → 逐条业务处理 → 写主清单/异常清单 → 复制并重写媒体 →
  原子提交 → 写 manifest/run.log → 返回内部 RunResult（[v4.2] lineage/stage_context 已停用）；
- CLI：``python inference.py --input <文件> --unique-id <id> --output-root <root>``。

RunResult 是内部结构，HTTP 由 app.py 转成响应信封；本算子没有模型推理，
真实后端是 wave 与 ffmpeg 两个解码器。
"""
from __future__ import annotations

import argparse
import copy
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

from utils import io as io_utils
from utils.audio_processing import (
    AudioProcessingError,
    MediaUnreadableError,
    normalize_audio,
)
from utils.io import UnsupportedMediaPath
# [v4.2] 血缘/阶段归档输出已停用：STAGE_CONTEXT_NAME/build_stage_context/
# load_prior_lineage/make_lineage 不再导入，原调用点已全部注释（utils/lineage.py 存档保留）；
# now_iso 仅为时间工具，保留。
from utils.lineage import now_iso
from utils.logger import close_logger, get_logger, log_action
from utils.schemas import BusinessOverrides, ConfigError, OperatorConfig
from utils.transport import build_manifest, write_manifest

OPERATOR_MAIN_NAME = "06_audio_index.jsonl"
OPERATOR_REJECT_NAME = "06_rejects.jsonl"
AUDIO_DIR = "06_audio"
MEDIA_DIR = "media"
MEDIA_FIELDS = ("image", "video", "audio")

#: HTTP 服务端限额（demo 约定，写进部署血缘；不接受请求放宽）
MAX_UPLOAD_BYTES = 67108864
MAX_FILES = 1000
MAX_UNPACKED_BYTES = 67108864
MAX_RESPONSE_BYTES = 67108864


class InputError(ValueError):
    """根输入不可读或找不到入口。"""


# ---------------------------------------------------------------- 配置加载

def default_config_path() -> Path:
    return Path(__file__).resolve().parent / "configs" / "config.yaml"


def _describe_validation_error(exc: ValidationError) -> str:
    first = exc.errors()[0] if exc.errors() else {}
    location = ".".join(str(part) for part in first.get("loc", ())) or "<root>"
    return "%s: %s" % (location, first.get("msg", "非法取值"))


def load_config(config_path: Optional[str] = None,
                overrides: Optional[dict] = None) -> OperatorConfig:
    """读取部署 YAML 并合并请求覆盖；结构/定义域问题抛 ``ConfigError``。

    ``overrides`` 只允许业务参数（``BusinessOverrides`` 拒绝 ``operator`` 与
    ``stage_id``），因此请求无法改写血缘身份。
    """
    path = Path(config_path) if config_path else default_config_path()
    if not path.is_file():
        raise ConfigError("配置文件不存在：%s" % path.name)
    try:
        raw = yaml.safe_load(path.read_text(encoding="utf-8"))
    except (yaml.YAMLError, OSError) as exc:
        raise ConfigError("配置文件无法解析：%s" % type(exc).__name__) from exc
    if raw is None:
        raw = {}
    if not isinstance(raw, dict):
        raise ConfigError("配置文件顶层必须是对象")

    payload = dict(raw)
    if overrides is not None:
        if not isinstance(overrides, dict):
            raise ConfigError("config 必须是对象")
        try:
            provided = BusinessOverrides.model_validate(overrides).provided()
        except ValidationError as exc:
            raise ConfigError("config 非法——%s" % _describe_validation_error(exc)) from exc
        payload.update(provided)

    try:
        return OperatorConfig.model_validate(payload)
    except ValidationError as exc:
        raise ConfigError("配置非法——%s" % _describe_validation_error(exc)) from exc


# ---------------------------------------------------------------- 路径与阶段血缘

def _safe_stem(sample_id: str) -> str:
    """把 sample_id 收敛成安全文件名主干（去掉分隔符与路径保留字符）。"""
    cleaned = "".join(
        ch if (ch.isalnum() or ch in "-_.") else "_" for ch in str(sample_id)
    ).strip("._")
    return cleaned[:80] or "sample"


def _media_paths_of(value: Any) -> List[str]:
    """收集本次输入实际携带的媒体路径。

    ``original_path`` 只是上游来源的追溯字段，可能指向更早阶段的目录，
    不保证随当前文件对象携带；把它当成当前输入路径会误报 ``media_missing``。
    当前可读取文件只由 ``path``（以及视频帧/音频片段的 ``path``）决定。
    """
    paths: List[str] = []
    if isinstance(value, str) and value.strip():
        paths.append(value)
    elif isinstance(value, dict):
        for key in ("path", "segment_path"):
            item = value.get(key)
            if isinstance(item, str) and item.strip():
                paths.append(item)
        for key in ("frames", "segments", "paragraphs"):
            paths.extend(_media_paths_of(value.get(key)))
    elif isinstance(value, list):
        for item in value:
            paths.extend(_media_paths_of(item))
    return paths


def _remap_media_value(value: Any, mapping: Dict[str, str]) -> Any:
    """把媒体引用重写到 run 目录内的新相对路径，保留未知字段。"""
    if isinstance(value, str):
        return mapping.get(value, value)
    if isinstance(value, list):
        return [_remap_media_value(item, mapping) for item in value]
    if isinstance(value, dict):
        item = copy.deepcopy(value)
        current_path = item.get("path")
        for key in ("path", "segment_path", "original_path"):
            current = item.get(key)
            if isinstance(current, str) and current in mapping:
                item[key] = mapping[current]
            elif key == "original_path" and isinstance(current_path, str) \
                    and current_path in mapping:
                # original_path 是来源追溯字段；若上游只携带当前 path，
                # 仍将其收敛到本次包内副本，避免下游把旧阶段路径当作活跃引用。
                item[key] = mapping[current_path]
        for key in ("frames", "segments", "paragraphs"):
            if key in item:
                item[key] = _remap_media_value(item[key], mapping)
        return item
    return value


def resolve_input(task_root: Path, input_file: Optional[str]) -> Path:
    """定位输入入口：阶段内后续用 ``inputFile``，阶段首用上传 manifest。

    任务根内路径由 HTTP 层先行校验；本函数同时接受 CLI 的本地路径，因此对任务根
    外路径不额外拒绝，但目标必须是存在的文件，或含 manifest 的可定位目录。
    """
    if input_file:
        if not isinstance(input_file, str) or not input_file.strip():
            raise InputError("inputFile 不能为空")
        raw = Path(input_file)
        if not raw.is_absolute():
            inside = task_root / input_file
            raw = inside if inside.exists() else Path(input_file)
        target = io_utils.resolved_absolute(raw)
        if target.is_dir():
            from utils.transport import TransportError, resolve_entrypoint

            try:
                entry, _relative = resolve_entrypoint(target)
            except TransportError as exc:
                raise InputError("inputFile 目录无法定位入口：%s" % exc) from exc
            return entry
        if not target.is_file():
            raise InputError("inputFile 不存在：%s" % Path(input_file).name)
        return target

    upload_dir = task_root / "input"
    if not upload_dir.is_dir():
        raise InputError("任务目录缺少 input/ 上传内容，且未提供 inputFile")
    from utils.transport import TransportError, resolve_entrypoint

    try:
        entry, _relative = resolve_entrypoint(upload_dir)
    except TransportError as exc:
        raise InputError("上传入口无法定位：%s" % exc) from exc
    return entry


# [v4.2] 血缘/阶段归档已停用：阶段血缘历史加载函数整体注释存档，不再被调用。
# def load_stage_history(input_path: Path, task_root: Path) -> Tuple[List[dict], List[dict]]:
#     """返回 ``(prior_lineage, stage_lineage)``。"""
#     candidates = [input_path.parent, task_root / "input"]
#     for directory in candidates:
#         context_path = directory / STAGE_CONTEXT_NAME
#         if context_path.is_file():
#             try:
#                 payload = json.loads(context_path.read_text(encoding="utf-8"))
#             except (json.JSONDecodeError, OSError):
#                 continue
#             if not isinstance(payload, dict):
#                 continue
#             prior = [item for item in (payload.get("prior_lineage") or []) if isinstance(item, dict)]
#             stage = [item for item in (payload.get("stage_lineage") or []) if isinstance(item, dict)]
#             return prior, stage
#     for directory in candidates:
#         prior = load_prior_lineage(directory)
#         if prior:
#             return prior, []
#     return [], []


# ---------------------------------------------------------------- 媒体搬运

class MediaCarrier:
    """把仍被引用的媒体复制进 run 目录并保持相对路径，供整体搬运。"""

    def __init__(self, source_root: Path, run_dir: Path, logger):
        self.source_root = io_utils.resolved_absolute(source_root)
        self.run_dir = io_utils.resolved_absolute(run_dir)
        self.logger = logger
        self.copied: Dict[str, int] = {}
        self.order: List[str] = []
        self.generated: List[str] = []
        self.names: Dict[str, str] = {}
        self.remap: Dict[Tuple[str, str], str] = {}

    def carry(self, relative: str, sample_id: str) -> str:
        """把输入媒体复制到 ``media/<sample_id>/<文件名>``，返回新的相对路径。"""
        key = (relative, sample_id)
        if key in self.remap:
            return self.remap[key]
        source = io_utils.resolve_media(self.source_root, relative)
        if not source.is_file():
            raise MediaUnreadableError("媒体文件缺失", relative)
        target_rel = self._assign_rel(sample_id, source.name)
        target = self.run_dir / target_rel
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(source, target)
        self.remap[key] = target_rel
        self.copied[target_rel] = target.stat().st_size
        self.order.append(target_rel)
        return target_rel

    def _assign_rel(self, sample_id: str, basename: str) -> str:
        """在 ``media/<sample_id>/`` 下分配不冲突的文件名。"""
        folder = "%s/%s" % (MEDIA_DIR, _safe_stem(sample_id))
        stem, suffix = os.path.splitext(basename)
        candidate = "%s/%s" % (folder, basename)
        index = 2
        taken = set(self.copied)
        while candidate in taken:
            candidate = "%s/%s_%d%s" % (folder, stem, index, suffix)
            index += 1
        return candidate

    def audio_relative(self, sample_id: str) -> str:
        """为 sample_id 分配稳定的输出 WAV 相对路径（同名冲突加序号）。"""
        stem = _safe_stem(sample_id)
        owner = self.names.get(stem)
        if owner is None:
            self.names[stem] = sample_id
        elif owner != sample_id:
            index = 2
            while "%s_%d" % (stem, index) in self.names:
                index += 1
            stem = "%s_%d" % (stem, index)
            self.names[stem] = sample_id
        return "%s/%s.wav" % (AUDIO_DIR, stem)

    def register(self, relative: str) -> None:
        """登记本算子生成的产物（用于 manifest 与血缘 artifacts）。"""
        target = self.run_dir / relative
        self.copied[relative] = target.stat().st_size if target.is_file() else 0
        if relative not in self.order:
            self.order.append(relative)
        if relative not in self.generated:
            self.generated.append(relative)

    @property
    def media_files(self) -> List[str]:
        """run 目录内需要随包搬运的全部媒体（搬运的原始媒体 + 生成的音频）。"""
        return list(self.order)

    @property
    def carried_files(self) -> List[str]:
        """从上游复制进来的原始媒体（不含本算子生成的音频）。"""
        return [path for path in self.order if path not in self.generated]

    @property
    def carried_bytes(self) -> int:
        return sum(self.copied.get(path, 0) for path in self.carried_files)

    @property
    def generated_bytes(self) -> int:
        return sum(self.copied.get(path, 0) for path in self.generated)

    @property
    def total_bytes(self) -> int:
        return sum(self.copied.values())


def make_reject(operator: str, reason: str, message: str,
                lineno: Optional[int] = None, record: Optional[dict] = None,
                sample_id: Optional[str] = None,
                source_reference: Optional[List[str]] = None,
                media_errors: Optional[List[dict]] = None) -> Dict[str, Any]:
    """异常清单条目：坏行保留行号，记录级保留完整样本或可用字段。"""
    item: Dict[str, Any] = {
        "operator": operator,
        "reason": reason, "destination": "原始隔离区", "tag": "原始文件-空",
        "message": message,
        "sample_id": sample_id,
    }
    if lineno is not None:
        item["line"] = lineno
    if record is not None:
        item["record"] = record
    if source_reference:
        item["source_reference"] = list(source_reference)
    if media_errors:
        item["media_errors"] = list(media_errors)
    return item


def _bump(counter: Dict[str, Any], key: str, amount: int = 1) -> None:
    counter[key] = counter.get(key, 0) + amount


def _merge_processing(record: dict, key: str, payload: dict) -> Dict[str, Any]:
    """只替换本节点编号的结果，保留其他节点的 processing 信息。"""
    existing = record.get("processing")
    merged = copy.deepcopy(existing) if isinstance(existing, dict) else {}
    merged[key] = payload
    return merged


def _audio_source_of(value: Any) -> Optional[str]:
    if isinstance(value, str) and value.strip():
        return value.strip()
    if isinstance(value, dict):
        item = value.get("path")
        if isinstance(item, str) and item.strip():
            return item.strip()
    return None


class RunContext:
    """一次 run 的共享上下文（配置、目录、媒体搬运器、日志）。"""

    def __init__(self, cfg: OperatorConfig, processing_key: str, source_root: Path,
                 run_dir: Path, carrier: MediaCarrier, logger):
        self.cfg = cfg
        self.processing_key = processing_key
        self.source_root = io_utils.resolved_absolute(source_root)
        self.run_dir = io_utils.resolved_absolute(run_dir)
        self.carrier = carrier
        self.logger = logger

    @property
    def operator_id(self) -> str:
        return self.cfg.operator.name


def process_record(record: dict, lineno: int, ctx: RunContext,
                   seen_ids: set, stats: Dict[str, Any]):
    """处理一条完整记录，返回 ``(主清单记录, 异常清单记录)``，二者必有一为 None。

    处理顺序：先音频标准化（失败则整条剔除，不搬运其媒体），再搬运仍需引用的
    图文媒体并重写路径，保证 run 目录只保留被成功记录引用的媒体。
    """
    if not ctx.cfg.enabled:
        output=copy.deepcopy(record)
        for field in MEDIA_FIELDS + ("transcription",):
            for relative in _media_paths_of(record.get(field)):
                source = io_utils.resolve_media(ctx.source_root, relative)
                if source.is_file():
                    dest=ctx.run_dir/relative
                    dest.parent.mkdir(parents=True,exist_ok=True)
                    shutil.copy2(source,dest)
                    ctx.carrier.register(relative)
        _bump(stats, "not_applicable")
        return output, None

    operator = ctx.operator_id
    sample_id = record.get("sample_id")
    if not isinstance(sample_id, str) or not sample_id.strip():
        _bump(stats["reject_reasons"], "invalid_record")
        return None, make_reject(
            operator, "invalid_record", "sample_id 缺失或不是非空字符串",
            lineno=lineno, record=record)
    sample_id = sample_id.strip()
    if sample_id in seen_ids:
        _bump(stats, "duplicates")
        _bump(stats["reject_reasons"], "duplicate_sample_id")
        return None, make_reject(
            operator, "duplicate_sample_id", "sample_id 在本次素材内重复，仅保留首次出现",
            lineno=lineno, record=record, sample_id=sample_id)
    seen_ids.add(sample_id)

    output = copy.deepcopy(record)
    output["sample_id"] = sample_id

    audio_value = record.get("audio")
    source_rel = _audio_source_of(audio_value)
    if audio_value is not None and source_rel is None:
        _bump(stats["reject_reasons"], "invalid_record")
        return None, make_reject(
            operator, "invalid_record", "audio 非 null 但缺少可用的 path",
            lineno=lineno, record=record, sample_id=sample_id)

    # ---- 1) 音频优先：失败即剔除，不留下无引用媒体
    audio_payload: Optional[Dict[str, Any]] = None
    audio_target: Optional[str] = None
    result: Optional[Dict[str, Any]] = None
    if source_rel is not None:
        target_rel = ctx.carrier.audio_relative(sample_id)
        target_path = io_utils.resolved_absolute(ctx.run_dir / target_rel)
        try:
            source_path = io_utils.resolve_media(ctx.source_root, source_rel)
        except UnsupportedMediaPath as exc:
            _bump(stats["reject_reasons"], "unsupported_media_path")
            return None, make_reject(
                operator, "unsupported_media_path", str(exc), lineno=lineno,
                record=record, sample_id=sample_id, source_reference=[source_rel],
                media_errors=[{"path": None, "reason": "unsupported_media_path"}])
        try:
            result = normalize_audio(source_path, target_path, ctx.cfg)
        except AudioProcessingError as exc:
            io_utils.cleanup_files(target_path)
            _bump(stats["reject_reasons"], exc.reason)
            return None, make_reject(
                operator, exc.reason, exc.message, lineno=lineno, record=record,
                sample_id=sample_id, source_reference=[source_rel],
                media_errors=[{"path": source_rel, "reason": exc.reason}])
        audio_target = target_rel

    # ---- 2) 图文媒体搬运与重写
    references: List[str] = []
    for field in MEDIA_FIELDS + ("transcription",):
        references.extend(_media_paths_of(record.get(field)))
    references = list(dict.fromkeys(references))

    mapping: Dict[str, str] = {}
    try:
        for relative in references:
            mapping[relative] = ctx.carrier.carry(relative, sample_id)
    except UnsupportedMediaPath as exc:
        if audio_target is not None:
            io_utils.cleanup_files(ctx.run_dir / audio_target)
        _bump(stats["reject_reasons"], "unsupported_media_path")
        return None, make_reject(
            operator, "unsupported_media_path", str(exc), lineno=lineno, record=record,
            sample_id=sample_id, source_reference=references,
            media_errors=[{"path": None, "reason": "unsupported_media_path"}])
    except MediaUnreadableError as exc:
        if audio_target is not None:
            io_utils.cleanup_files(ctx.run_dir / audio_target)
        _bump(stats, "media_missing")
        _bump(stats["reject_reasons"], "media_unreadable")
        return None, make_reject(
            operator, "media_unreadable", exc.message, lineno=lineno, record=record,
            sample_id=sample_id, source_reference=references,
            media_errors=[{"path": exc.detail, "reason": "media_missing"}])

    output["image"] = _remap_media_value(output.get("image"), mapping)
    output["video"] = _remap_media_value(output.get("video"), mapping)
    if "transcription" in output:
        output["transcription"] = _remap_media_value(output["transcription"], mapping)

    # ---- 3) 无音频模态：完整透传并标记 not_applicable
    if result is None or audio_target is None:
        output["audio"] = None
        # [v4.2] 输出记录 status 双字段：int status（1 正常）+ process_status 流程态
        output["processing"] = _merge_processing(record, ctx.processing_key, {
            "status": 1, "process_status": "not_applicable",
            "reason": "no_audio_modality", "metrics": {}})
        _bump(stats, "not_applicable")
        return output, None

    ctx.carrier.register(audio_target)
    output["audio"] = {
        "path": audio_target,
        "sample_rate": result["sample_rate"],
        "channels": result["channels"],
        "duration": result["duration"],
        "segments": [],
        "original_path": mapping.get(source_rel, source_rel),
        "pcm_bits": result["pcm_bits"],
        "clipped": result["clipped"],
        "clip_start": result["clip_start"],
        "clip_end": result["clip_end"],
        "decoder_backend": result["decoder_backend"],
        "source_sample_rate": result["source_sample_rate"],
        "source_channels": result["source_channels"],
        "source_duration": result["source_duration"],
        "frames": result["frames"],
        "bytes": result["bytes"],
        "rms": result["rms"],
    }
    # [v4.2] 输出记录 status 双字段：int status + process_status 流程态
    output["processing"] = _merge_processing(record, ctx.processing_key, {
        "status": 1,
        "process_status": "success",
        "reason": None,
        "metrics": {
            "source_sample_rate": result["source_sample_rate"],
            "source_channels": result["source_channels"],
            "source_duration": result["source_duration"],
            "target_sample_rate": result["sample_rate"],
            "target_channels": result["channels"],
            "frames": result["frames"],
            "decoder_backend": result["decoder_backend"],
            "clipped": result["clipped"],
            "rms": result["rms"],
        },
    })

    _bump(stats, "audio_standardized")
    _bump(stats["decoder_backends"], result["decoder_backend"])
    stats["duration_seconds_total"] = round(
        stats["duration_seconds_total"] + result["duration"], 6)
    if ((result["source_sample_rate"] is not None and result["source_sample_rate"] != ctx.cfg.sample_rate)
            or (result["source_channels"] is not None and result["source_channels"] != ctx.cfg.channels)):
        _bump(stats, "audio_resampled")
    if result["clipped"]:
        _bump(stats, "audio_clipped")
    if result["decoder_backend"] == "ffmpeg":
        _bump(stats, "ffmpeg_fallback")
        if result["encoding_delay_note"]:
            _bump(stats, "ffmpeg_encoding_delay_note")
    if result["rms"] <= 0.0:
        _bump(stats, "silent_output")
    return output, None


# ---------------------------------------------------------------- run

def _output_root(output_root: Optional[str]) -> Path:
    """输出根：显式参数 > OUTPUT_DIR 环境变量 > 本目录 output。"""
    if output_root:
        return io_utils.resolve_root(output_root)
    env_root = os.environ.get("OUTPUT_DIR")
    if env_root:
        return io_utils.resolve_root(env_root)
    return io_utils.resolve_root(Path(__file__).resolve().parent / "output")


def _initial_statistics(cfg: OperatorConfig) -> Dict[str, Any]:
    return {
        "input": 0, "input_lines": 0, "blank_lines": 0,
        "valid_records": 0, "bad_lines": 0,
        "output": 0, "rejected": 0, "not_applicable": 0, "success": 0,
        "duplicates": 0,
        "media_copied": 0, "media_bytes": 0, "media_missing": 0,
        "media_generated": 0, "audio_bytes": 0,
        "audio_standardized": 0, "audio_resampled": 0, "audio_clipped": 0,
        "duration_seconds_total": 0.0,
        "decoder_backend": "unavailable",
        "decoder_backends": {"wave": 0, "ffmpeg": 0},
        "ffmpeg_fallback": 0, "ffmpeg_encoding_delay_note": 0, "silent_output": 0,
        "empty_input": False, "conservation_ok": True,
        "artifact_files": 0,
        "reject_reasons": {},
        "requested_sample_rate": cfg.sample_rate,
        "requested_channels": cfg.channels,
        "requested_pcm_bits": cfg.pcm_bits,
        "max_duration_seconds": cfg.max_duration_seconds,
        "enabled": cfg.enabled,
    }


def _dominant_backend(backends: Dict[str, int]) -> str:
    """汇总本次 run 实际使用的解码后端：主后端 > 混合 > 无。"""
    wave = backends.get("wave", 0)
    ffmpeg = backends.get("ffmpeg", 0)
    if wave and ffmpeg:
        return "mixed"
    if ffmpeg:
        return "ffmpeg"
    if wave:
        return "wave"
    return "unavailable"


def run(input_file: Optional[str], unique_id: str, config_path: Optional[str] = None,
        output_root: Optional[str] = None, overrides: Optional[dict] = None) -> Dict[str, Any]:
    """处理一次任务，返回内部 RunResult（dict）。

    ``status`` 取 success/partial/degraded/failed；``outputFile`` 为本地主清单路径。
    [v4.2] 血缘停用：结果不再含 lineage 相关键。HTTP 由 app.py 转成信封。
    """
    started = now_iso()
    cfg = load_config(config_path, overrides)
    root = _output_root(output_root)
    value = io_utils.validate_unique_id(unique_id)

    with io_utils.TaskLock(root, value):
        task = io_utils.task_dir(root, value)
        operator_dir = task / cfg.operator.name
        run_id = io_utils.next_run_id(operator_dir)
        run_dir = operator_dir / run_id
        run_dir.mkdir(parents=True, exist_ok=True)
        logger = get_logger("%s.%s.%s" % (cfg.operator.name, value, run_id),
                            run_dir / "run.log")
        try:
            return _execute(cfg, value, run_id, task, run_dir, input_file, started, logger)
        finally:
            close_logger(logger)


def _execute(cfg: OperatorConfig, unique_id: str, run_id: str, task: Path, run_dir: Path,
             input_file: Optional[str], started: str, logger) -> Dict[str, Any]:
    operator = cfg.operator.name
    log_action(logger, "配置", "音频兜底已开启：FFmpeg 16000Hz单声道wav，不裁剪" if cfg.enabled else "音频兜底关闭，不执行")
    log_action(logger, "start", "operator=%s uniqueId=%s runId=%s" % (operator, unique_id, run_id))

    source = resolve_input(task, input_file)
    try:
        io_utils.assert_utf8(source)
    except UnicodeDecodeError as exc:
        raise InputError("输入不是严格 UTF-8：%s" % source.name) from exc
    log_action(logger, "input", "entry=%s size=%d" % (source.name, source.stat().st_size))

    # [v4.2] 血缘已停用：不再加载历史/阶段血缘
    # prior_lineage, stage_records = load_stage_history(source, task)
    carrier = MediaCarrier(source.parent, run_dir, logger)
    ctx = RunContext(cfg, "06", source.parent, run_dir, carrier, logger)
    stats = _initial_statistics(cfg)

    main_path = run_dir / OPERATOR_MAIN_NAME
    reject_path = run_dir / OPERATOR_REJECT_NAME
    main_tmp = run_dir / (OPERATOR_MAIN_NAME + ".partial")
    reject_tmp = run_dir / (OPERATOR_REJECT_NAME + ".partial")
    main_count = 0
    reject_count = 0
    seen_ids: set = set()

    try:
        with io_utils.JsonlWriter(main_tmp) as main_writer, \
                io_utils.JsonlWriter(reject_tmp) as reject_writer:
            for lineno, line in enumerate(io_utils.iter_decoded_lines(source, "utf-8"), 1):
                stats["input_lines"] += 1
                if not line.strip():
                    stats["blank_lines"] += 1
                    continue
                stats["input"] += 1
                try:
                    record = json.loads(line)
                except json.JSONDecodeError:
                    stats["bad_lines"] += 1
                    _bump(stats["reject_reasons"], "invalid_json")
                    reject_writer.write(make_reject(
                        operator, "invalid_json", "JSON 解析失败", lineno=lineno))
                    reject_count += 1
                    continue
                if not isinstance(record, dict):
                    stats["bad_lines"] += 1
                    _bump(stats["reject_reasons"], "invalid_record")
                    reject_writer.write(make_reject(
                        operator, "invalid_record", "顶层不是 JSON 对象", lineno=lineno))
                    reject_count += 1
                    continue
                stats["valid_records"] += 1
                output, reject = process_record(record, lineno, ctx, seen_ids, stats)
                if output is not None:
                    main_writer.write(output)
                    main_count += 1
                else:
                    reject_writer.write(reject)
                    reject_count += 1
    except BaseException:
        io_utils.cleanup_files(main_tmp, reject_tmp)
        raise

    os.replace(str(main_tmp), str(main_path))
    os.replace(str(reject_tmp), str(reject_path))

    stats["output"] = main_count
    stats["rejected"] = reject_count
    stats["success"] = stats["audio_standardized"]
    stats["media_copied"] = len(carrier.carried_files)
    stats["media_bytes"] = carrier.carried_bytes
    stats["media_generated"] = len(carrier.generated)
    stats["audio_bytes"] = carrier.generated_bytes
    stats["decoder_backend"] = _dominant_backend(stats["decoder_backends"])
    stats["conservation_ok"] = stats["input"] == stats["output"] + stats["rejected"]
    if not stats["conservation_ok"]:
        raise InputError("统计不守恒：input=%d output=%d rejected=%d"
                         % (stats["input"], stats["output"], stats["rejected"]))

    if stats["input"] == 0:
        stats["empty_input"] = True
        status, message = "success", "empty_input：输入没有任何数据行"
    elif reject_count == 0:
        status, message = "success", "success"
    elif main_count == 0 and set(stats["reject_reasons"]) == {"dependency_missing"}:
        status, message = "failed", "dependency_missing：全部音频记录因解码后端缺失失败"
    else:
        status, message = "partial", "partial：%d 条记录进入异常清单" % reject_count

    artifacts: List[Dict[str, str]] = [
        {"path": OPERATOR_MAIN_NAME, "role": "main"},
        {"path": OPERATOR_REJECT_NAME, "role": "review"},
    ]
    artifacts.extend({"path": relative, "role": "media"} for relative in carrier.media_files)
    missing = [item["path"] for item in artifacts if not (run_dir / item["path"]).is_file()]
    if missing:
        raise InputError("输出产物缺失：%s" % ",".join(sorted(missing)))
    stats["artifact_files"] = len(artifacts)
    write_manifest(run_dir, build_manifest(OPERATOR_MAIN_NAME, artifacts))
    publish_stage_output(run_dir)

    # [v4.2] 血缘/阶段归档已停用：血缘记录构建、lineage.json 与 stage_context.json
    # 落盘全部注释存档，不再写出。
    # record = make_lineage(
    #     operator=operator, version=cfg.operator.version, unique_id=unique_id,
    #     stage_id=cfg.stage_id, run_id=run_id, input_file=str(source),
    #     output_file=str(main_path), status=status, started_at=started,
    #     finished_at=now_iso(), config=cfg.business_dict(), statistics=stats,
    #     artifacts=artifacts)
    # stage_lineage = list(stage_records) + [record]
    # io_utils.atomic_write_json(run_dir / "lineage.json", list(prior_lineage) + stage_lineage)
    # io_utils.atomic_write_json(run_dir / STAGE_CONTEXT_NAME, build_stage_context(
    #     stage_id=cfg.stage_id, unique_id=unique_id, run_id=run_id,
    #     entrypoint=OPERATOR_MAIN_NAME, stage_lineage=stage_lineage,
    #     prior_lineage=prior_lineage))

    log_action(logger, "statistics", "input=%d output=%d rejected=%d not_applicable=%d media=%d"
               % (stats["input"], stats["output"], stats["rejected"],
                  stats["not_applicable"], stats["media_copied"]))
    log_action(logger, "finish", "status=%s runId=%s" % (status, run_id))

    # [v4.2] 血缘输出已停用：返回 dict 不再包含 lineageFile/stageLineage/priorLineage；
    # 内部保留 inputFile（不直接外泄），app.py 转任务相对路径供阶段首校验。
    return {
        "status": status,
        "outputFile": str(main_path),
        "rejectFile": str(reject_path),
        "manifestPath": str(run_dir / "manifest.json"),
        "statistics": stats,
        "message": message,
        "runId": run_id,
        "runDir": str(run_dir),
        "entrypoint": OPERATOR_MAIN_NAME,
        "artifacts": artifacts,
        "inputFile": str(source),
    }


# [v4.2] 血缘输出已停用：中间血缘构造函数注释存档（app.py 不再调用）。
# def build_intermediate_lineage(result: Dict[str, Any]) -> Dict[str, Any]:
#     """中间算子响应用的当前节点血缘（含临时 outputFile 的本地快照）。"""
#     return copy.deepcopy(result["stageLineage"][-1])


def _emit(payload: Dict[str, Any]) -> None:
    """CLI 结构化输出：优先 UTF-8，失败退回 ASCII 转义，保证始终可解析。"""
    text = json.dumps(payload, ensure_ascii=False, indent=2)
    try:
        sys.stdout.reconfigure(encoding="utf-8")
    except (AttributeError, ValueError):
        text = json.dumps(payload, ensure_ascii=True, indent=2)
    sys.stdout.write(text + "\n")


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="算子 6：音频标准化（PCM WAV 标准化）")
    parser.add_argument("--input", required=True, help="输入 JSONL 完整记录清单")
    parser.add_argument("--unique-id", required=True, dest="unique_id", help="任务标识")
    parser.add_argument("--output-root", dest="output_root", default=None,
                        help="输出根目录（默认 OUTPUT_DIR 或本目录 output）")
    parser.add_argument("--config", default=None, help="YAML 配置路径")
    parser.add_argument("--override", dest="override", action="append", default=None,
                        metavar="KEY=VALUE",
                        help="业务参数覆盖，可重复，例如 --override sample_rate=8000")
    parser.add_argument("--overrides", dest="overrides_json", default=None,
                        help="业务参数覆盖，JSON 对象字符串，例如 {\"sample_rate\":8000}")
    parser.add_argument("--is-last", dest="is_last", action="store_true",
                        help="按阶段末算子预览 data 形状（outputFile=file-object）")
    parser.add_argument("--quiet", action="store_true", help="只输出核心摘要")
    return parser


def _parse_scalar(raw: str) -> Any:
    """把 ``KEY=VALUE`` 的 VALUE 解析成 JSON 标量，失败则按原始字符串。"""
    try:
        return json.loads(raw)
    except json.JSONDecodeError:
        return raw


def _parse_overrides(pairs: Optional[List[str]],
                     json_text: Optional[str] = None) -> Optional[dict]:
    """合并 ``--overrides`` JSON 与重复的 ``--override KEY=VALUE``。"""
    merged: Dict[str, Any] = {}
    if json_text:
        try:
            payload = json.loads(json_text)
        except json.JSONDecodeError as exc:
            raise ConfigError("--overrides 不是合法 JSON") from exc
        if not isinstance(payload, dict):
            raise ConfigError("--overrides 必须是 JSON 对象")
        merged.update(payload)
    for item in pairs or []:
        if "=" not in item:
            raise ConfigError("--override 需要 KEY=VALUE 形式：%s" % item)
        key, _, value = item.partition("=")
        key = key.strip()
        if not key:
            raise ConfigError("--override 的 KEY 不能为空")
        merged[key] = _parse_scalar(value.strip())
    return merged or None


#: [v4.2] 形态二统一响应中的算子英文标识（detection 别名空间成员）
OPERATOR_ENGLISH_ID = "normalize_audio"


def _cli_data_shape(result: Dict[str, Any], is_last: bool) -> Dict[str, str]:
    """预览 app.py 统一响应的 data 键与取值形状（不实际打包）。

    [v4.2] 不再区分 isLastOperator：统一 operator/status/outputFile/result，无血缘字段。
    """
    if result["status"] == "failed":
        return {}
    return {"operator": "str", "status": "str",
            "outputFile": "crossmodal-files-v1", "result": "object"}


def main(argv: Optional[List[str]] = None) -> int:
    args = build_parser().parse_args(argv)
    config_path = args.config or str(default_config_path())
    try:
        overrides = _parse_overrides(args.override, args.overrides_json)
    except ConfigError as exc:
        _emit({"code": 1, "msg": str(exc), "data": {}})
        return 1

    try:
        result = run(args.input, args.unique_id, config_path=config_path,
                     output_root=args.output_root, overrides=overrides)
    except (ConfigError, InputError, io_utils.TaskBusyError, OSError, ValueError) as exc:
        _emit({"code": 1, "msg": str(exc), "data": {}})
        return 1

    code = 0 if result["status"] in ("success", "partial") else 1
    # [v4.2] 血缘输出已停用：摘要不再含 lineageFile（run() 结果亦无此键）
    if args.quiet:
        summary: Dict[str, Any] = {
            "code": code,
            "msg": result["message"],
            "status": result["status"],
            "outputFile": result["outputFile"],
            "statistics": result["statistics"],
        }
    else:
        summary = {
            "code": code,
            "msg": result["message"],
            "status": result["status"],
            "runId": result["runId"],
            "runDir": result["runDir"],
            "outputFile": result["outputFile"],
            "rejectsFile": result["rejectFile"],
            "statistics": result["statistics"],
            "dataShape": _cli_data_shape(result, args.is_last),
        }
    _emit(summary)
    return code


if __name__ == "__main__":
    sys.exit(main())
