"""算子 8（包内节点 07）：带时间索引的语音转写 —— 配置加载、处理流程（run）与 CLI。

职责（表1 #8，v1.0.1 起收窄）：
- ``load_config``：YAML + 请求覆盖统一校验，API 与 CLI 共用；
- ``run``：流式读完整记录 → 记录级校验/媒体引用收集 → 搬运媒体 → 按需 ASR →
  写主清单/异常清单/待补清单 → 原子提交 → 写 manifest/run.log；
- CLI：``python inference.py --input <文件> --unique-id <id> --output-root <root>``。

本节点**不再生成跨模态配对语义单元**（``semantic_unit`` 固定 ``null``，deprecated）：
配对级单元由 #12–#15 生成；这里只产出 ASR 段落单元（转写文本 + ``start_ms``/``end_ms``
+ 音频片段）。
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

from typing import Any, Dict, List, Optional, Tuple

import yaml
from pydantic import ValidationError

from utils import io as io_utils
from utils.coordination import (
    NODE_KEY,
    RecordError,
    UnitOptions,
    build_unit,
    has_audio_source,
)
from utils.io import UnsupportedMediaPath
# [v4.2] 血缘/阶段归档输出已停用：STAGE_CONTEXT_NAME/build_stage_context/
# load_prior_lineage/make_lineage/to_stage_lineage 不再导入，原调用点已全部注释
# （utils/lineage.py 存档保留）；now_iso 仅为时间工具，保留。
from utils.lineage import now_iso
from utils.logger import close_logger, get_logger, log_action
from utils import transcription as transcription_utils
from utils.schemas import BusinessOverrides, ConfigError, OperatorConfig
from utils.transport import (
    MANIFEST_NAME,
    atomic_pack_file_object,
    build_manifest,
    write_manifest,
)

OPERATOR_MAIN_NAME = "07_candidates.jsonl"
OPERATOR_REJECT_NAME = "07_rejects.jsonl"
#: 待补清单（只登记、不回采；表1 #8「缺少模态 → 退回来源侧」）
SUPPLEMENT_NAME = "07_supplement_requests.jsonl"
PROCESSING_KEY = "07"
#: 上游节点旁路清单（review/rejects）命名，随包携带以便追溯
SIDECAR_PATTERN = re.compile(r"^\d{2}_(rejects|review)\.jsonl$")
#: 表1 #8 步骤 3)：按段落裁开的音频片段目录（相对 run 目录）
SEGMENT_DIR = "06_audio/segments"

#: HTTP 服务端限额（demo 约定，写进部署血缘；不接受请求放宽）
MAX_UPLOAD_BYTES = 67108864
MAX_FILES = 1000
MAX_UNPACKED_BYTES = 67108864
MAX_RESPONSE_BYTES = 67108864


class InputError(ValueError):
    """根输入不可读或找不到入口。"""


# ---------------------------------------------------------------- 配置加载

BASE_DIR = Path(__file__).resolve().parent


def default_config_path() -> Path:
    """默认配置路径：``OPERATOR_CONFIG`` 环境变量 > 本目录 ``configs/config.yaml``。"""
    env_path = os.environ.get("OPERATOR_CONFIG")
    if env_path:
        return Path(env_path)
    return BASE_DIR / "configs" / "config.yaml"


def _describe_validation_error(exc: ValidationError) -> str:
    first = exc.errors()[0] if exc.errors() else {}
    location = ".".join(str(part) for part in first.get("loc", ())) or "<root>"
    return "%s: %s" % (location, first.get("msg", "非法取值"))


def load_config(config_path: Optional[str] = None,
                overrides: Optional[dict] = None) -> OperatorConfig:
    """读取部署 YAML 并合并请求覆盖；结构与定义域问题抛 ``ConfigError``。"""
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
    if overrides:
        if not isinstance(overrides, dict):
            raise ConfigError("config 必须是对象")
        try:
            provided = BusinessOverrides.model_validate(overrides).provided()
        except ValidationError as exc:
            raise ConfigError("config 非法——%s" % _describe_validation_error(exc)) from exc
        # 表1 #8：请求只能切换「是否做语音转写」，不能改模型 / 解码语言（固定项）
        transcription_switch = provided.pop("transcription_enabled", None)
        payload.update(provided)
        if transcription_switch is not None:
            section = payload.get("transcription")
            section = dict(section) if isinstance(section, dict) else {}
            section["enabled"] = transcription_switch
            payload["transcription"] = section

    try:
        cfg = OperatorConfig.model_validate(payload)
    except ValidationError as exc:
        raise ConfigError("配置非法——%s" % _describe_validation_error(exc)) from exc
    if cfg.require_existing_media is not True:
        # run/HTTP/CLI 都不允许关闭存在性校验（TECHNICAL_DESIGN §3）。
        raise ConfigError("配置非法——require_existing_media 必须是 true（运行期强制）")
    return cfg


def config_snapshot(cfg: OperatorConfig, overridden: Optional[dict] = None) -> Dict[str, Any]:
    """血缘里的配置快照：业务参数 + 部署身份 + 被覆盖的键。"""
    snapshot: Dict[str, Any] = {
        "business": cfg.business_dict(),
        "deployment": cfg.deployment_dict(),
        "overridden_keys": sorted(overridden or {}),
    }
    if overridden:
        snapshot["overrides"] = dict(overridden)
    return snapshot


# ---------------------------------------------------------------- 路径与阶段血缘

def _resolve_directory_entry(directory: Path, label: str) -> Path:
    from utils.transport import TransportError, resolve_entrypoint

    try:
        entry, _relative = resolve_entrypoint(directory)
    except TransportError as exc:
        raise InputError("%s无法定位入口：%s" % (label, exc)) from exc
    return entry


def resolve_input(task_root: Path, input_file: Optional[str],
                  allow_external: bool = False) -> Path:
    """定位输入入口：阶段内后续用 ``inputFile``，阶段首用上传 manifest。

    ``allow_external`` 仅供 CLI 使用：本地 fixture 常位于任务目录之外，而 HTTP 层已用
    ``resolve_in_task`` 强制 ``inputFile`` 落在任务根内，因此默认（HTTP/run）保持严格，
    只有 CLI 显式打开时才允许读取任务根外的既有文件（只读，不写入其所在目录）。
    """
    if input_file:
        if not isinstance(input_file, str) or not input_file.strip():
            raise InputError("inputFile 不能为空")
        target: Optional[Path]
        try:
            # 只允许任务根内路径：跨 uniqueId / 任务根外一律拒绝
            target = io_utils.resolve_in_task(task_root, input_file)
        except ValueError as exc:
            if not allow_external:
                raise InputError("inputFile 非法：%s" % exc) from exc
            target = None
        if target is not None:
            if target.is_dir():
                return _resolve_directory_entry(target, "inputFile 目录")
            if target.is_file():
                return target
            if not allow_external:
                raise InputError("inputFile 不存在：%s" % Path(input_file).name)
        # CLI 便捷路径：把入参当作 BASE_DIR 相对路径或绝对路径解析到任务目录之外
        fallback = Path(input_file)
        if not fallback.is_absolute():
            fallback = BASE_DIR / fallback
        fallback = io_utils.resolved_absolute(fallback)
        if fallback.is_dir():
            return _resolve_directory_entry(fallback, "inputFile 目录")
        if not fallback.is_file():
            raise InputError("inputFile 不存在：%s" % Path(input_file).name)
        return fallback

    upload_dir = task_root / "input"
    if not upload_dir.is_dir():
        raise InputError("任务目录缺少 input/ 上传内容，且未提供 inputFile")
    return _resolve_directory_entry(upload_dir, "上传入口")


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
#             prior = [i for i in (payload.get("prior_lineage") or []) if isinstance(i, dict)]
#             stage = [i for i in (payload.get("stage_lineage") or []) if isinstance(i, dict)]
#             if stage or prior:
#                 return prior, stage
#     for directory in candidates:
#         prior = load_prior_lineage(directory)
#         if prior:
#             return prior, []
#     return [], []


# ---------------------------------------------------------------- 媒体搬运

class MediaCarrier:
    """把仍被引用的媒体复制进 run 目录，保持相对路径以便整体搬运。

    路径保持不变（不重命名），因此记录中的相对引用无需改写；重复引用只复制一次。
    """

    def __init__(self, source_root: Path, run_dir: Path, logger) -> None:
        self.source_root = io_utils.resolved_absolute(source_root)
        self.run_dir = io_utils.resolved_absolute(run_dir)
        self.logger = logger
        self.remap: Dict[str, str] = {}
        self.order: List[str] = []
        self.sizes: Dict[str, int] = {}

    def resolve(self, relative: str) -> Path:
        return io_utils.resolve_media(self.source_root, relative)

    def carry(self, relative: str) -> str:
        """复制 ``relative`` 到 run 目录同名相对路径，返回目标相对路径。"""
        if relative in self.remap:
            return self.remap[relative]
        source = self.resolve(relative)
        if not source.is_file():
            raise FileNotFoundError(relative)
        target_rel = relative.replace("\\", "/")
        target = self.run_dir / target_rel
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(source, target)
        self.remap[relative] = target_rel
        if target_rel not in self.order:
            self.order.append(target_rel)
        self.sizes[target_rel] = target.stat().st_size
        return target_rel

    def carry_sidecars(self) -> List[str]:
        """复制上游 review/rejects 旁路清单（存在才复制），保留原文件名。"""
        carried: List[str] = []
        if not self.source_root.is_dir():
            return carried
        for path in sorted(self.source_root.iterdir()):
            if not path.is_file() or not SIDECAR_PATTERN.match(path.name):
                continue
            if path.name in (OPERATOR_REJECT_NAME, OPERATOR_MAIN_NAME):
                continue
            target = self.run_dir / path.name
            if target.exists():
                continue
            shutil.copyfile(path, target)
            self.sizes[path.name] = target.stat().st_size
            self.order.append(path.name)
            carried.append(path.name)
        return carried

    def register(self, relative: str) -> None:
        target = self.run_dir / relative
        size = target.stat().st_size if target.is_file() else 0
        self.sizes[relative] = size
        if relative not in self.order:
            self.order.append(relative)

    @property
    def media_files(self) -> List[str]:
        return list(self.order)

    @property
    def total_bytes(self) -> int:
        return sum(self.sizes.values())


def make_reject(operator: str, reason: str, message: str,
                lineno: Optional[int] = None, record: Optional[dict] = None,
                sample_id: Optional[str] = None,
                source_reference: Optional[List[str]] = None,
                evidence: Optional[dict] = None) -> Dict[str, Any]:
    """异常清单条目：坏行保留行号，记录级保留完整样本或可用字段。"""
    item: Dict[str, Any] = {
        "operator": operator,
        "reason": reason,
        "message": message,
        "sample_id": sample_id,
    }
    if lineno is not None:
        item["line"] = lineno
    if record is not None:
        item["record"] = record
    if source_reference:
        item["source_reference"] = list(source_reference)
    if evidence:
        item["evidence"] = evidence
    return item


def _bump(counter: Dict[str, Any], key: str, amount: int = 1) -> None:
    counter[key] = counter.get(key, 0) + amount


def _remap_record_media(record: Dict[str, Any], mapping: Dict[str, str]) -> Dict[str, Any]:
    """把记录中的媒体引用重写到 run 目录内的相对路径（保留未知字段）。"""
    for field in ("image", "video", "audio"):
        value = record.get(field)
        if isinstance(value, str):
            record[field] = mapping.get(value, value)
            continue
        if not isinstance(value, dict):
            continue
        for key in ("path", "original_path"):
            current = value.get(key)
            if isinstance(current, str) and current in mapping:
                value[key] = mapping[current]
        for collection in ("frames", "segments"):
            items = value.get(collection)
            if isinstance(items, list):
                for item in items:
                    if isinstance(item, dict) and isinstance(item.get("path"), str):
                        item["path"] = mapping.get(item["path"], item["path"])
    return record


class RunContext:
    """一次 run 的共享上下文。"""

    def __init__(self, cfg: OperatorConfig, options: UnitOptions, source_root: Path,
                 run_dir: Path, carrier: MediaCarrier, logger) -> None:
        self.cfg = cfg
        self.options = options
        self.source_root = io_utils.resolved_absolute(source_root)
        self.run_dir = io_utils.resolved_absolute(run_dir)
        self.carrier = carrier
        self.logger = logger
        # 表1 #8：转写设置与惰性转写器（开关关闭时不会加载任何权重）
        try:
            self.transcription = transcription_utils.TranscriptionSettings.from_config(cfg)
        except transcription_utils.TranscriptionConfigError as exc:
            raise ConfigError("配置非法——%s" % exc) from exc
        self.transcriber = transcription_utils.WhisperTranscriber(self.transcription)

    @property
    def operator_id(self) -> str:
        return self.cfg.operator.name


def _attach_visual_time_index(output: dict, gate: dict,
                              stats: Dict[str, Any]) -> None:
    """无声视频兜底：用 #6 已切好的视频分段/关键帧生成**视觉时间索引**。

    与语音 ``time_index`` 分字段存放（``visual_time_index``），下游 #17 明确标注
    来源 ``m08_visual_index``，既有 ``time_index`` 消费方（#11/#19）不受影响。
    有可用关键帧时按帧切分（``source=visual_frame``），否则退回分段级
    （``source=visual_segment``）；没有任何视频分段时写空列表并显式标
    ``visual_time_index_available=false``。
    """
    visual = transcription_utils.visual_time_index(output)
    output["visual_time_index"] = visual
    gate["visual_time_index_available"] = bool(visual)
    if visual:
        frame_entries = sum(1 for item in visual
                            if item.get("granularity") in ("frame", "frame_group"))
        gate["visual_time_index_source"] = "m08_visual_index"
        gate["visual_time_index_entries"] = len(visual)
        gate["visual_time_index_frame_entries"] = frame_entries
        gate["visual_time_index_granularity"] = ("frame" if frame_entries == len(visual)
                                                 else "mixed" if frame_entries
                                                 else "segment")
        _bump(stats, "visual_time_index_built")
        _bump(stats, "visual_time_index_entries", len(visual))
        if frame_entries:
            _bump(stats, "visual_time_index_frame_entries", frame_entries)


def _apply_transcription(output: dict, ctx: RunContext, sample_id: str,
                         stats: Dict[str, Any]) -> Dict[str, Any]:
    """表1 #8 语音转写：按需加载 Whisper-base、按中文解码、按段落裁开音频。

    返回记录级分流结果 ``{"process_status", "reason", "missing_modality"}``：

    * ``not_applicable``：无音频来源（图片/纯文本，或无音轨视频），不加载模型；
    * ``skipped``：总开关或转写开关关闭，不加载模型；
    * ``transcribed``：转写成功，写 ``transcription`` 与 ``time_index``（含 ms）；
    * ``no_speech``：有音轨但空转写，不伪造文本；
    * ``failed``：模型/解码等原因失败，样本仍留在产线（进候选描述）。

    无音频来源的图片/纯文本记录**不写** ``transcription`` 等字段，保持透传。
    """
    settings = ctx.transcription
    reference = transcription_utils.audio_source_reference(output)
    result = {"process_status": transcription_utils.PROCESS_NOT_APPLICABLE,
              "reason": transcription_utils.REASON_NO_MEDIA,
              "missing_modality": []}
    if reference is None:
        # 无 audio/video：不当失败；只有「声明了视频却没有可用音轨」才登记待补
        return result
    source = ctx.run_dir / reference
    if not source.is_file():
        try:
            source = ctx.carrier.resolve(reference)
        except UnsupportedMediaPath:
            source = None
    if source is None or not source.is_file():
        result.update(process_status=transcription_utils.PROCESS_FAILED,
                      reason=transcription_utils.REASON_NO_MEDIA)
        return result

    _bump(stats, "audio_units")
    if not settings.active:
        reason = settings.inactive_reason
        output["transcription"] = transcription_utils.build_payload(
            transcription_utils.STATUS_SKIPPED, reason=reason, settings=settings)
        output["audio_transcript"] = None
        output["time_index"] = []
        output["transcription_gate"] = {
            "transcript_required": True, "transcript_available": False,
            "direct_scoring_allowed": False, "reason": reason,
        }
        # 计数统一在 process_record 按 process_status 落桶，避免重复计数
        _bump(stats["transcription_skip_reasons"], reason)
        result.update(process_status=transcription_utils.PROCESS_SKIPPED, reason=reason)
        return result

    payload = transcription_utils.run_transcription(
        ctx.transcriber, source, sample_id=sample_id, settings=settings,
        segment_dir=ctx.run_dir / SEGMENT_DIR,
        relative_prefix=SEGMENT_DIR)
    # 模型改为「按需加载」：时长/信噪比预检命中的样本在 transcribe() 内直接
    # 返回，不会加载 Whisper；统计按 transcriber 的真实加载状态记账。
    if ctx.transcriber.loaded:
        stats["transcription_model_loaded"] = True
        stats["transcription_loaded"] = True
    output["transcription"] = payload
    output["audio_transcript"] = payload.get("text")
    process_status = transcription_utils.record_process_status(payload)
    reason = payload.get("reason")
    result.update(process_status=process_status, reason=reason)

    if payload["status"] == transcription_utils.STATUS_OK:
        output["time_index"] = transcription_utils.time_index_of(payload)
        output["transcription_gate"] = {
            "transcript_required": True, "transcript_available": True,
            "direct_scoring_allowed": True, "reason": None,
        }
        _bump(stats, "transcription_segments", int(payload.get("paragraph_count") or 0))
        for paragraph in payload.get("paragraphs") or []:
            relative = paragraph.get("segment_path")
            if relative:
                ctx.carrier.register(relative)
                _bump(stats, "segments_written")
        return result

    if payload["status"] == transcription_utils.STATUS_NO_SPEECH:
        # 有音轨但空转写：不伪造文本，进入候选描述；不算缺模态
        output["time_index"] = []
        output["visual_time_index"] = []
        output["transcription_gate"] = {
            "transcript_required": True, "transcript_available": False,
            "direct_scoring_allowed": False, "reason": reason,
            "visual_time_index_available": False,
        }
        output["candidate_description_required"] = True
        _bump(stats["no_speech_reasons"], reason)
        # 预检命中（时长/信噪比/静音）单独计数：不加载模型，也不产出任何文本
        if payload.get("precheck"):
            _bump(stats, "precheck_skipped")
            _bump(stats["precheck_reasons"], reason)
        return result

    # 表1 #8：无音轨 / 转写失败都留在产线中，进入候选描述，不能直接评分
    output["time_index"] = []
    gate = {
        "transcript_required": True, "transcript_available": False,
        "direct_scoring_allowed": False, "reason": reason,
    }
    if reason == transcription_utils.REASON_NO_AUDIO_TRACK:
        # 无声视频不是坏样本：用视觉分段给出时间轴，供 #17 预填（来源显式标注）；
        # 同时登记待补清单（只登记，不回采）。
        _attach_visual_time_index(output, gate, stats)
        result["missing_modality"] = ["audio"]
        _bump(stats["not_applicable_reasons"], reason)
    else:
        # 其它失败（解码失败/模型缺失…）不借视觉分段冒充时间轴，但字段保持同构
        output["visual_time_index"] = []
        gate["visual_time_index_available"] = False
        _bump(stats["transcription_fail_reasons"], reason)
    output["transcription_gate"] = gate
    output["candidate_description_required"] = True
    return result


#: 记录级流程态 → 统计计数键（每个主清单记录恰好落一个桶，保证守恒）
_STATUS_STAT_KEY = {
    transcription_utils.PROCESS_TRANSCRIBED: "transcribed",
    transcription_utils.PROCESS_SKIPPED: "transcription_skipped",
    transcription_utils.PROCESS_NOT_APPLICABLE: "not_applicable",
    transcription_utils.PROCESS_NO_SPEECH: "no_speech",
    transcription_utils.PROCESS_FAILED: "transcription_failed",
}


def _write_processing(output: dict, status: Dict[str, Any], meta: Dict[str, Any]) -> None:
    """把本节点结果写进 ``processing["07"]``（保留其他节点结果）。

    ``status`` int：1 正常流程态（含 not_applicable/skipped/no_speech），0 仅失败。
    """
    process_status = status["process_status"]
    existing = output.get("processing")
    processing = copy.deepcopy(existing) if isinstance(existing, dict) else {}
    processing[NODE_KEY] = {
        "status": 0 if process_status == transcription_utils.PROCESS_FAILED else 1,
        "process_status": process_status,
        "reason": status.get("reason"),
        "metrics": {
            "present_modalities": list(meta.get("present_modalities") or []),
            "media_references": len(meta.get("media_references") or []),
            "asr_units": len(output.get("time_index") or []),
            "missing_modality": list(status.get("missing_modality") or []),
        },
    }
    output["processing"] = processing


def process_record(record: dict, lineno: int, ctx: RunContext,
                   seen_ids: set, stats: Dict[str, Any],
                   supplement_writer) -> Tuple[Optional[dict], Optional[dict]]:
    """处理一条完整记录，返回 ``(主清单记录, 异常清单记录)``，二者必有一为 None。

    顺序：先记录级校验与媒体引用收集（失败即剔除，不搬运媒体），再校验媒体存在性，
    最后搬运并重写引用、按需转写，保证 run 目录只保留被成功记录引用的媒体。
    """
    operator = ctx.operator_id
    raw_sid = record.get("sample_id")
    sample_id = raw_sid.strip() if isinstance(raw_sid, str) and raw_sid.strip() else None
    if sample_id is None:
        _bump(stats["reject_reasons"], "invalid_record")
        return None, make_reject(
            operator, "invalid_record", "sample_id 缺失或不是非空字符串",
            lineno=lineno, record=record)
    if sample_id in seen_ids:
        _bump(stats, "duplicates")
        _bump(stats["reject_reasons"], "duplicate_sample_id")
        return None, make_reject(
            operator, "duplicate_sample_id", "sample_id 在本次素材内重复，仅保留首次出现",
            lineno=lineno, record=record, sample_id=sample_id)
    seen_ids.add(sample_id)

    try:
        output, meta = build_unit(record, ctx.options)
    except RecordError as exc:
        _bump(stats["reject_reasons"], exc.reason)
        if exc.reason.startswith("media"):
            _bump(stats, "media_missing")
        return None, make_reject(
            operator, exc.reason, exc.message, lineno=lineno, record=record,
            sample_id=sample_id, source_reference=exc.source_reference or None)

    references: List[str] = meta["media_references"]
    missing: List[dict] = []
    for relative in references:
        try:
            source = ctx.carrier.resolve(relative)
        except UnsupportedMediaPath as exc:
            _bump(stats["reject_reasons"], "unsupported_media_path")
            return None, make_reject(
                operator, "unsupported_media_path", str(exc), lineno=lineno,
                record=record, sample_id=sample_id, source_reference=references,
                evidence={"media_errors": [{"path": None, "reason": "unsupported_media_path"}]})
        if not source.is_file():
            missing.append({"path": relative, "reason": "media_missing"})
    if missing:
        _bump(stats["reject_reasons"], "media_missing")
        _bump(stats, "media_missing")
        return None, make_reject(
            operator, "media_missing", "引用的媒体文件不存在", lineno=lineno,
            record=record, sample_id=sample_id, source_reference=references,
            evidence={"media_errors": missing})

    mapping: Dict[str, str] = {}
    try:
        for relative in references:
            mapping[relative] = ctx.carrier.carry(relative)
    except (FileNotFoundError, UnsupportedMediaPath, OSError) as exc:
        _bump(stats["reject_reasons"], "media_unreadable")
        _bump(stats, "media_missing")
        return None, make_reject(
            operator, "media_unreadable", "媒体无法复制：%s" % type(exc).__name__,
            lineno=lineno, record=record, sample_id=sample_id,
            source_reference=references,
            evidence={"media_errors": [{"path": str(exc), "reason": "media_unreadable"}]})

    output = _remap_record_media(output, mapping)

    # 表1 #8：媒体已落到 run 目录后再转写，段落裁片与引用指向同一份本地文件
    status = _apply_transcription(output, ctx, sample_id, stats)
    _write_processing(output, status, meta)

    missing_modality = list(status.get("missing_modality") or [])
    if missing_modality:
        # 待补清单：只登记缺模态与来源，不回采、不联网
        output["missing_modality"] = missing_modality
        supplement_writer.write({
            "sample_id": sample_id,
            "missing_modality": missing_modality,
            "reason": status.get("reason"),
            "source_reference": references,
            "process_status": status["process_status"],
            "recorded_at": now_iso(),
            "operator": operator,
        })
        _bump(stats, "supplement_requests")

    _bump(stats, _STATUS_STAT_KEY.get(status["process_status"], "transcription_failed"))
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
        "output": 0, "rejected": 0,
        # 记录级流程态计数：五个桶之和 == output（守恒式见 _execute）
        "not_applicable": 0, "no_speech": 0,
        "supplement_requests": 0,
        "duplicates": 0,
        "media_copied": 0, "media_bytes": 0, "media_missing": 0,
        "sidecar_files": 0,
        "empty_input": False, "conservation_ok": True,
        "artifact_files": 0,
        # 表1 #8 语音转写口径统计
        "audio_units": 0, "transcribed": 0, "transcription_failed": 0,
        "transcription_skipped": 0, "transcription_segments": 0,
        "segments_written": 0, "transcription_model_loaded": False,
        "transcription_loaded": False,
        # 送 ASR 前的时长/信噪比预检：命中数 + 原因分布
        "precheck_skipped": 0,
        "precheck_reasons": {},
        "transcription": {
            "master_switch": bool(cfg.enabled),
            "transcription_switch": bool(cfg.transcription.enabled),
            "effective": bool(cfg.enabled and cfg.transcription.enabled),
            "model": cfg.transcription.model,
            "language": cfg.transcription.language,
            "precheck": bool(cfg.transcription.precheck),
            "min_duration_seconds": float(cfg.transcription.min_duration_seconds),
            "min_snr_db": float(cfg.transcription.min_snr_db),
        },
        "transcription_fail_reasons": {},
        "transcription_skip_reasons": {},
        "not_applicable_reasons": {},
        "no_speech_reasons": {},
        "reject_reasons": {},
        "flags": {},
        "entrypoint": OPERATOR_MAIN_NAME,
        "requested_unit_policy": cfg.unit_policy,
        "requested_max_av_duration_diff_ratio": cfg.max_av_duration_diff_ratio,
        "requested_av_mismatch_action": cfg.av_mismatch_action,
        "require_existing_media_enforced": True,
        "validate_frame_timestamps": cfg.validate_frame_timestamps,
    }


def run(input_file: Optional[str], unique_id: str, config_path: Optional[str] = None,
        output_root: Optional[str] = None, overrides: Optional[dict] = None,
        allow_external_input: bool = False) -> Dict[str, Any]:
    """处理一次任务，返回内部 RunResult（dict）。

    ``status`` 取 success/partial/degraded/failed；``outputFile`` 为本地主清单路径。
    [v4.2] 血缘停用：结果不再含 lineage 相关键。HTTP 由 app.py 转成响应信封。

    ``allow_external_input`` 仅供 CLI：允许读取任务根外的既有 fixture（HTTP 恒为 False）。
    """
    started = now_iso()
    cfg = load_config(config_path, overrides)
    options = UnitOptions.from_mapping(cfg)
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
        produced = False
        try:
            result = _execute(cfg, options, value, run_id, task, run_dir,
                              input_file, started, overrides, logger, allow_external_input)
            produced = True
            return result
        finally:
            # 先关日志再清理：Windows 上 run.log 句柄未释放会导致目录删不掉
            close_logger(logger)
            # 失败且没有产出任何交付物时，不留下半成品 run 目录
            if not produced \
                    and not (run_dir / OPERATOR_MAIN_NAME).exists() \
                    and not (run_dir / OPERATOR_REJECT_NAME).exists():
                shutil.rmtree(run_dir, ignore_errors=True)


def _execute(cfg: OperatorConfig, options: UnitOptions, unique_id: str, run_id: str,
             task: Path, run_dir: Path, input_file: Optional[str], started: str,
             overridden: Optional[dict], logger,
             allow_external_input: bool = False) -> Dict[str, Any]:
    operator = cfg.operator.name
    log_action(logger, "start", "operator=%s uniqueId=%s runId=%s" % (operator, unique_id, run_id))

    source = resolve_input(task, input_file, allow_external=allow_external_input)
    try:
        io_utils.assert_utf8(source)
    except UnicodeDecodeError as exc:
        raise InputError("输入不是严格 UTF-8：%s" % source.name) from exc
    log_action(logger, "input", "entry=%s size=%d" % (source.name, source.stat().st_size))

    # [v4.2] 血缘已停用：不再加载历史/阶段血缘
    # prior_lineage, stage_records = load_stage_history(source, task)
    carrier = MediaCarrier(source.parent, run_dir, logger)
    ctx = RunContext(cfg, options, source.parent, run_dir, carrier, logger)
    stats = _initial_statistics(cfg)

    # 表1 #8 步骤 1)：把两个开关的真实取值写进日志，作为「是否加载模型」的证据
    log_action(logger, "transcription_switch",
               "master=%s transcription=%s effective=%s model=%s language=%s device=%s"
               % (cfg.enabled, cfg.transcription.enabled, ctx.transcription.active,
                  ctx.transcription.model, ctx.transcription.language,
                  cfg.transcription.device))
    if not ctx.transcription.active:
        log_action(logger, "whisper_not_loaded",
                   "未加载 Whisper-base：reason=%s（表1 #8 步骤 1)）"
                   % ctx.transcription.inactive_reason)

    main_path = run_dir / OPERATOR_MAIN_NAME
    reject_path = run_dir / OPERATOR_REJECT_NAME
    supplement_path = run_dir / SUPPLEMENT_NAME
    main_tmp = run_dir / (OPERATOR_MAIN_NAME + ".partial")
    reject_tmp = run_dir / (OPERATOR_REJECT_NAME + ".partial")
    supplement_tmp = run_dir / (SUPPLEMENT_NAME + ".partial")
    main_count = 0
    reject_count = 0
    seen_ids: set = set()

    try:
        with io_utils.JsonlWriter(main_tmp) as main_writer, \
                io_utils.JsonlWriter(reject_tmp) as reject_writer, \
                io_utils.JsonlWriter(supplement_tmp) as supplement_writer:
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
                output, reject = process_record(record, lineno, ctx, seen_ids, stats,
                                                supplement_writer)
                if output is not None:
                    main_writer.write(output)
                    main_count += 1
                else:
                    reject_writer.write(reject)
                    reject_count += 1
    except BaseException:
        io_utils.cleanup_files(main_tmp, reject_tmp, supplement_tmp)
        raise

    os.replace(str(main_tmp), str(main_path))
    os.replace(str(reject_tmp), str(reject_path))
    os.replace(str(supplement_tmp), str(supplement_path))

    sidecars = carrier.carry_sidecars()
    carrier.register(OPERATOR_MAIN_NAME)
    carrier.register(OPERATOR_REJECT_NAME)

    stats["output"] = main_count
    stats["rejected"] = reject_count
    # 成功 = 真正产出转写的记录数（配对单元已不在本节点生成）
    stats["success"] = stats["transcribed"]
    stats["sidecar_files"] = len(sidecars)
    stats["media_copied"] = len(carrier.media_files)
    stats["media_bytes"] = carrier.total_bytes
    stats["conservation_ok"] = (
        stats["input"] == stats["output"] + stats["rejected"]
        and stats["output"] == (
            stats["transcribed"] + stats["transcription_skipped"]
            + stats["not_applicable"] + stats["no_speech"]
            + stats["transcription_failed"])
    )
    if not stats["conservation_ok"]:
        raise InputError(
            "统计不守恒：input=%d output=%d rejected=%d "
            "transcribed=%d skipped=%d not_applicable=%d no_speech=%d failed=%d"
            % (stats["input"], stats["output"], stats["rejected"],
               stats["transcribed"], stats["transcription_skipped"],
               stats["not_applicable"], stats["no_speech"],
               stats["transcription_failed"]))

    if stats["input"] == 0:
        stats["empty_input"] = True
        status, message = "success", "empty_input：输入没有任何数据行"
    elif reject_count == 0:
        status, message = "success", "success"
    elif main_count == 0:
        status, message = "partial", "partial：%d 条记录全部进入异常清单" % reject_count
    else:
        status, message = "partial", "partial：%d 条记录进入异常清单" % reject_count

    artifacts: List[Dict[str, str]] = [
        {"path": OPERATOR_MAIN_NAME, "role": "main"},
        {"path": OPERATOR_REJECT_NAME, "role": "review"},
        # 待补清单是旁路台账（只登记不回采），不计入 media
        {"path": SUPPLEMENT_NAME, "role": "supplement"},
    ]
    artifacts.extend({"path": relative, "role": "media"} for relative in carrier.media_files
                     if relative not in (OPERATOR_MAIN_NAME, OPERATOR_REJECT_NAME)
                     and relative not in sidecars)
    artifacts.extend({"path": name, "role": "sidecar"} for name in sidecars)
    missing = [item["path"] for item in artifacts if not (run_dir / item["path"]).is_file()]
    if missing:
        raise InputError("输出产物缺失：%s" % ",".join(sorted(missing)))
    stats["artifact_files"] = len(artifacts)
    manifest = build_manifest(OPERATOR_MAIN_NAME, artifacts)
    manifest.update({
        "operator": operator,
        "run_id": run_id,
        "unique_id": unique_id,
        "created_at": now_iso(),
        "notes": ("07 只做带时间索引的语音转写（ASR 段落单元）；"
                  "semantic_unit 固定 null（deprecated），配对语义单元由 #12–#15 生成"),
    })
    write_manifest(run_dir, manifest)
    publish_stage_output(run_dir)

    # [v4.2] 血缘/阶段归档已停用：血缘记录构建、lineage.json 与 stage_context.json
    # 落盘全部注释存档，不再写出。
    # record = make_lineage(
    #     operator=operator, version=cfg.operator.version, unique_id=unique_id,
    #     stage_id=cfg.stage_id, run_id=run_id, input_file=str(source),
    #     output_file=str(main_path), status=status, started_at=started,
    #     finished_at=now_iso(),
    #     config=config_snapshot(cfg, overridden), statistics=stats,
    #     artifacts=artifacts)
    # stage_lineage = list(stage_records) + [record]
    # io_utils.atomic_write_json(run_dir / "lineage.json", list(prior_lineage) + stage_lineage)
    # io_utils.atomic_write_json(run_dir / STAGE_CONTEXT_NAME, build_stage_context(
    #     stage_id=cfg.stage_id, unique_id=unique_id, run_id=run_id,
    #     entrypoint=OPERATOR_MAIN_NAME, stage_lineage=stage_lineage,
    #     prior_lineage=prior_lineage))

    log_action(logger, "statistics",
               "input=%d output=%d rejected=%d transcribed=%d skipped=%d "
               "not_applicable=%d no_speech=%d failed=%d supplement=%d media=%d sidecar=%d"
               % (stats["input"], stats["output"], stats["rejected"],
                  stats["transcribed"], stats["transcription_skipped"],
                  stats["not_applicable"], stats["no_speech"],
                  stats["transcription_failed"], stats["supplement_requests"],
                  stats["media_copied"], stats["sidecar_files"]))
    # 表1 #8 完成判据：纯图文默认任务的日志必须写明未加载 Whisper-base
    if ctx.transcriber.loaded:
        # 记录真实权重目录，便于核对「模型来自本算子 weight 还是 MODEL_ROOT」。
        log_action(logger, "whisper_loaded",
                   "Whisper-base 已加载：device=%s model=%s weights=%s"
                   " transcribed=%d failed=%d"
                   % (ctx.transcriber.device, ctx.transcription.model,
                      ctx.transcriber.weights_dir(),
                      stats["transcribed"], stats["transcription_failed"]))
    else:
        # 加载失败时优先上报模型的真实失败原因（如 whisper_weights_missing），
        # 避免把「权重缺失」误报成 no_audio_input。
        not_loaded_reason = (ctx.transcription.inactive_reason
                             or getattr(ctx.transcriber, "load_reason", None)
                             or transcription_utils.REASON_NO_MEDIA)
        log_action(logger, "whisper_not_loaded",
                   "未加载 Whisper-base：reason=%s（表1 #8 完成判据）"
                   % not_loaded_reason)
        stats["transcription"]["not_loaded_reason"] = not_loaded_reason

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


# [v4.2] 血缘输出已停用：中间/阶段血缘构造函数注释存档（app.py 不再调用）。
# def build_intermediate_lineage(result: Dict[str, Any]) -> Dict[str, Any]:
#     """中间算子响应用的当前节点血缘（含临时 outputFile 的本地快照）。"""
#     return copy.deepcopy(result["stageLineage"][-1])
#
#
# def build_stage_payload(result: Dict[str, Any]) -> List[Dict[str, Any]]:
#     """阶段末对外血缘：当前阶段完整数组，剔除临时 outputFile 绝对路径。"""
#     return to_stage_lineage(result["stageLineage"])


def stage_file_object(result: Dict[str, Any],
                      max_response_bytes: int = MAX_RESPONSE_BYTES) -> Dict[str, Any]:
    """阶段末文件内容对象：入口 + 全部媒体/旁路文件 + 运行清单。

    清单本身不是 artifacts 条目（``build_manifest`` 会跳过 manifest.json），
    但必须随包传递，下游恢复后才能不依赖上游目录定位入口。
    """
    paths = [item["path"] for item in result["artifacts"]]
    if (Path(result["runDir"]) / MANIFEST_NAME).is_file():
        paths.append(MANIFEST_NAME)
    return atomic_pack_file_object(
        result["runDir"], result["entrypoint"], paths,
        max_response_bytes=max_response_bytes)


# ---------------------------------------------------------------- CLI

def _emit(payload: Dict[str, Any]) -> None:
    """CLI 结构化输出：优先 UTF-8，失败退回 ASCII 转义，保证始终可解析。"""
    text = json.dumps(payload, ensure_ascii=False, indent=2)
    try:
        sys.stdout.reconfigure(encoding="utf-8")
    except (AttributeError, ValueError):
        text = json.dumps(payload, ensure_ascii=True, indent=2)
    sys.stdout.write(text + "\n")


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="算子 7：跨模态协同预处理")
    parser.add_argument("--input", required=True, help="输入 JSONL 完整记录清单")
    parser.add_argument("--unique-id", required=True, dest="unique_id", help="任务id_文件id")
    parser.add_argument("--output-root", dest="output_root", default=None,
                        help="输出根目录（默认 OUTPUT_DIR 或本目录 output）")
    parser.add_argument("--config", default=None, help="YAML 配置路径")
    parser.add_argument("--override", default=None,
                        help="业务参数覆盖，JSON 字符串，例如 {\"av_mismatch_action\":\"reject\"}")
    parser.add_argument("--is-last", dest="is_last", action="store_true",
                        help="按阶段末算子预览 data 形状（outputFile=file-object）")
    parser.add_argument("--quiet", action="store_true", help="只输出核心摘要")
    return parser


def _parse_overrides(raw: Optional[str]) -> Optional[dict]:
    if not raw:
        return None
    try:
        payload = json.loads(raw)
    except json.JSONDecodeError as exc:
        raise ConfigError("--override 不是合法 JSON") from exc
    if not isinstance(payload, dict):
        raise ConfigError("--override 必须是 JSON 对象")
    return payload


#: [v4.2] 形态二统一响应中的算子英文标识（detection 别名空间成员）
OPERATOR_ENGLISH_ID = "align_semantic_unit"


def _cli_data_keys(status: str, is_last: bool) -> List[str]:
    """[v4.2] 统一响应形状预览：不再区分 isLastOperator，无血缘字段。"""
    if status == "failed":
        return []
    return ["operator", "status", "outputFile", "result"]


def main(argv: Optional[List[str]] = None) -> int:
    args = build_parser().parse_args(argv)
    config_path = args.config or str(default_config_path())
    try:
        overrides = _parse_overrides(args.override)
    except ConfigError as exc:
        _emit({"code": 1, "msg": str(exc), "data": {}})
        return 1

    try:
        result = run(args.input, args.unique_id, config_path=config_path,
                     output_root=args.output_root, overrides=overrides,
                     allow_external_input=True)
    except (ConfigError, InputError, io_utils.TaskBusyError, OSError, ValueError) as exc:
        _emit({"code": 1, "msg": str(exc), "data": {}})
        return 1

    code = 0 if result["status"] in ("success", "partial", "degraded") else 1
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
        keys = _cli_data_keys(result["status"], args.is_last)
        summary = {
            "code": code,
            "msg": result["message"],
            "status": result["status"],
            "runId": result["runId"],
            "runDir": result["runDir"],
            "outputFile": result["outputFile"],
            "rejectsFile": result["rejectFile"],
            "statistics": result["statistics"],
            "dataShape": {key: ("file-object" if key == "outputFile" else "str")
                          for key in keys} if code == 0 else {},
        }
    _emit(summary)
    return code


if __name__ == "__main__":
    sys.exit(main())
