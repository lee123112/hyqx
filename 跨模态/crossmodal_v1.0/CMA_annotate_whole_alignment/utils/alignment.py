"""算子 #16 标注整体对齐关系 业务核心（表1 v4.1 第 16 行冻结口径）。

冻结口径（`跨模态算子开发背景与规则.md` §4 #16、本目录 `DEVELOPMENT_PLAN.md`、`TECHNICAL_DESIGN.md`）：

- 输入：#11 候选配对、#12 CLIP 评分结果/模型版本、#14 视文评分
  （``video_text_score`` / ``processing.14``，含视频侧记录级字段）、#24 抽检命中标记、
  人工回写事件（人工结论或人工改写后的描述）；
- 处理：``score >= 0.32`` 自动对齐；``隔离线 <= score < 0.32``（默认 0.20）或抽检命中进人工队列；
  ``score < 隔离线`` 沿用 #12 隔离结论（不对齐，进样本隔离库，**不进人工队列**）；
- 人工三按钮：``aligned``（对齐）/ ``not_aligned``（不对齐）/ ``uncertain``（存疑）；
  **人工点"不对齐"后锁定否决，不会被后续高分改回通过**；
- 人工改写描述后**必须复用同一 CLIP 实例重算**，不得沿用旧分数；重算不可用时如实标记
  ``pending_manual_review`` + ``degraded``（不得伪造分数）；
- **视文路线**：``video+text`` 配对消费 #14 的真实视文分，同样按 ``隔离线/0.32`` 分档；
  ``image+text`` 仍最高优先（``video+image+text`` 走图文）。常规视文分档不读视频/帧文件，
  只有人工改写描述需要重评分时才取代表帧（中间帧）走本地 CLIP；无代表帧即显式延后；
- 输出结论：``auto_aligned`` / ``manual_aligned`` / ``not_aligned`` / ``uncertain`` /
  ``pending_manual_review``。

本模块不 import 任何模型库：CLIP 通过 ``encoder`` 参数注入（``utils.backends`` 提供真实实现）。
"""
from __future__ import annotations

import copy
import hashlib
import math
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional, Sequence, Tuple

from utils import io as op_io

# ------------------------------------------------------------------ 身份常量

OPERATOR = "16_whole_alignment"
OPERATOR_LABEL = "#16"
VERSION = "v1.0.0"
STAGE_ID = "S3"
STAGE_NAME = "数据加工"
FORMAL_OPERATOR_NUMBER = 16

MAIN_NAME = "16_whole_alignment.jsonl"
REVIEW_NAME = "16_review.jsonl"
ISOLATION_NAME = "16_isolation.jsonl"
NOT_APPLICABLE_NAME = "16_not_applicable.jsonl"
REJECTS_NAME = "16_rejects.jsonl"
AUDIT_NAME = "16_audit.jsonl"

SIDECAR_NAMES = (REVIEW_NAME, ISOLATION_NAME, NOT_APPLICABLE_NAME, REJECTS_NAME, AUDIT_NAME)

# ------------------------------------------------------------------ 结论枚举

DECISION_AUTO_ALIGNED = "auto_aligned"
DECISION_MANUAL_ALIGNED = "manual_aligned"
DECISION_NOT_ALIGNED = "not_aligned"
DECISION_UNCERTAIN = "uncertain"
DECISION_PENDING_MANUAL_REVIEW = "pending_manual_review"

DECISIONS = (DECISION_AUTO_ALIGNED, DECISION_MANUAL_ALIGNED, DECISION_NOT_ALIGNED,
             DECISION_UNCERTAIN, DECISION_PENDING_MANUAL_REVIEW)

CONFIRM_AUTO = "auto_threshold"
CONFIRM_HUMAN = "human"
#: 表1 #16 算法 4)：人改描述后用同一 CLIP 实例重算，结论由新分档位给出
CONFIRM_RESCORE = "rescore"
#: [2026-11-05] 时序路线：结论由 #15 的时间轴一致性给出（无 CLIP 分时生效）
CONFIRM_TEMPORAL = "temporal_15"

#: 表1 #16 质量处置：结论 → (label, action, destination)
DECISION_TABLE: Dict[str, Tuple[str, str, str]] = {
    DECISION_AUTO_ALIGNED: ("样本-评分：通过", "keep_in_pool", "样本池"),
    DECISION_MANUAL_ALIGNED: ("样本-评分：通过", "keep_in_pool", "样本池"),
    DECISION_PENDING_MANUAL_REVIEW: ("样本-评分：待人工", "queue_manual_review", "人工队列"),
    DECISION_UNCERTAIN: ("样本-评分：待人工", "queue_manual_review", "人工复核队列(#25)"),
    DECISION_NOT_ALIGNED: ("样本-语义不通", "isolate", "样本隔离库"),
}

LABEL_PASS = "样本-评分：通过"
LABEL_PENDING = "样本-评分：待人工"
LABEL_NOT_ALIGNED = "样本-语义不通"

LABEL_ORIGIN_BROKEN = "原始文件-损坏"
LABEL_ORIGIN_UNPARSABLE = "原始文件-无法解析"
LABEL_ORIGIN_ENCODING = "原始文件-编码异常"
LABEL_ORIGIN_MISSING_FIELD = "原始文件-缺字段"
LABEL_FLOW_NOT_APPLICABLE = "流程-不适用整体对齐"
LABEL_FLOW_INVALID_HUMAN_EVENT = "流程-人工事件非法"

DEST_ORIGIN = "原始隔离区"
DEST_PIPELINE_WAIT = "留产线（样本池）"
DEST_MANUAL_EVENT = "人工事件异常台账"

#: 原始文件类问题（进原始隔离区）：原因 → 表1 标签
ORIGIN_LEDGER: Dict[str, str] = {
    "encoding_error": LABEL_ORIGIN_ENCODING,
    "json_parse_error": LABEL_ORIGIN_UNPARSABLE,
    "record_not_object": LABEL_ORIGIN_UNPARSABLE,
    "missing_field: pair_id": LABEL_ORIGIN_MISSING_FIELD,
    "duplicate_pair_id": LABEL_ORIGIN_MISSING_FIELD,
    "missing_side": LABEL_ORIGIN_MISSING_FIELD,
    "media_path_invalid": LABEL_ORIGIN_UNPARSABLE,
    "media_missing": LABEL_ORIGIN_BROKEN,
    "media_unreadable": LABEL_ORIGIN_BROKEN,
}

#: 分数来源
SOURCE_UPSTREAM = "upstream_12"
SOURCE_RESCORE = "rescore"
SOURCE_NONE = "none"
#: 视文路线（#14）分数来源；image 分仍最高优先
SOURCE_VIDEO_TEXT_SCORE = "video_text_score"
SOURCE_PROCESSING_14 = "processing.14"

#: 上游来源标记（写入 evidence/isolation_source，不新增响应字段）
ORIGIN_UPSTREAM_12 = "12"
ORIGIN_UPSTREAM_14 = "14"
#: [2026-11-05] 时空一致性（#15）结论来源：时序样本没有 CLIP 分，结论由时间轴偏差给出
ORIGIN_UPSTREAM_15 = "15"
SOURCE_PROCESSING_15 = "processing.15"

#: #15 结论 → #16 原因码（写入 processing["16"].reason）
REASON_TEMPORAL_PASSED = "temporal_within_threshold"
REASON_TEMPORAL_EXCEEDED = "temporal_threshold_exceeded"
REASON_TEMPORAL_PENDING = "temporal_pending_calibration"
REASON_TEMPORAL_NOT_APPLICABLE = "temporal_not_applicable"


REASON_MODEL_UNAVAILABLE = "model_unavailable"
#: 视文路线人工改写描述后需要代表帧重评分，但视频里没有可用帧引用
REASON_NO_REPRESENTATIVE_FRAME = "no_representative_frame"

#: 整体—整体对齐的路线枚举（内部结构，不进入响应协议）
KIND_CROSS = "cross"
KIND_INTERNAL = "internal"
KIND_VIDEO_CROSS = "video_cross"
KIND_VIDEO_INTERNAL = "video_internal"
VIDEO_KINDS = (KIND_VIDEO_CROSS, KIND_VIDEO_INTERNAL)
KIND_AUDIO_CROSS = "audio_cross"
KIND_AUDIO_INTERNAL = "audio_internal"
AUDIO_KINDS = (KIND_AUDIO_CROSS, KIND_AUDIO_INTERNAL)

#: 媒体引用字段取值顺序（图像/视频/帧共用）
_MEDIA_REFERENCE_KEYS = ("path", "file", "file_path", "uri")

# ------------------------------------------------------------------ 配置口径

# [2026-11-05 全量标定] 0.25→0.20：与 #12/#14 标定线一致；仅在上游未声明阈值时生效
DEFAULT_ISOLATION_THRESHOLD = 0.20
DEFAULT_AUTO_ALIGN_THRESHOLD = 0.32
ISOLATION_RANGE = (0.20, 0.40)
AUTO_ALIGN_RANGE = (0.25, 0.50)
DEFAULT_BATCH_SIZE = 8

SUPPORTED_LIBRARIES = ("auto", "open_clip", "clip", "transformers", "none")
SUPPORTED_DEVICES = ("auto", "cpu", "cuda")

#: 请求 config 只允许这些业务参数（模型/设备/路径/身份不可被请求改写）
ALLOWED_OVERRIDES = {"isolation_threshold", "auto_align_threshold",
                     "temporal_route", "follow_upstream_thresholds"}

SECTION = "whole_alignment"


class ConfigError(ValueError):
    """配置结构、定义域或枚举非法。"""


class StructuralError(ValueError):
    """输入记录结构不符合 #11 候选对契约（进原始隔离区，不做业务判定）。"""

    def __init__(self, reason: str, label: Optional[str] = None) -> None:
        super().__init__(reason)
        self.reason = reason
        self.label = label or ORIGIN_LEDGER.get(reason, LABEL_ORIGIN_UNPARSABLE)


class RescoreUnavailable(RuntimeError):
    """人工改写描述后必须重评分，但本地 CLIP 不可用且配置不允许降级 → 任务失败。"""


def _number(value: Any, name: str) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise ConfigError("%s 必须是数字，当前为 %r" % (name, value))
    number = float(value)
    if not math.isfinite(number):
        raise ConfigError("%s 必须是有限数字" % name)
    return number


def _bool(value: Any, name: str) -> bool:
    if not isinstance(value, bool):
        raise ConfigError("%s 必须是布尔值，当前为 %r" % (name, value))
    return value


def _positive_int(value: Any, name: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value < 1:
        raise ConfigError("%s 必须是正整数，当前为 %r" % (name, value))
    return value


def validate_config(cfg: dict) -> dict:
    """校验并规范化配置；返回深拷贝，不修改入参。

    关键差异（表1 #16）：**本工具必选、默认开启且不能关闭**，因此
    ``whole_alignment.enabled=false`` 与 ``rescore_modified_description=false`` 都是配置错误。
    """
    if not isinstance(cfg, dict):
        raise ConfigError("配置必须是对象")

    raw_section = cfg.get(SECTION)
    if raw_section is None:
        raw_section = {}
    if not isinstance(raw_section, dict):
        raise ConfigError("%s 必须是对象" % SECTION)

    enabled = _bool(raw_section.get("enabled", True), "%s.enabled" % SECTION)
    if not enabled:
        raise ConfigError("whole_alignment.enabled 必须为 true：表1 #16 为必选工具，不能关闭")
    rescore_modified = _bool(raw_section.get("rescore_modified_description", True),
                             "%s.rescore_modified_description" % SECTION)
    if not rescore_modified:
        raise ConfigError("rescore_modified_description 必须为 true：表1 #16 要求人工改描述后"
                          "必须复用同一 CLIP 实例重算，不得沿用旧分数")
    #: [2026-11-05] 时序路线：为 true（默认）时，无 CLIP 分但有 #15 结论的样本
    #: 按 #15 的时间轴结论分档；false 只用于回归对比（复现"全部 pending/no_score"旧行为）。
    temporal_route = _bool(raw_section.get("temporal_route", True),
                           "%s.temporal_route" % SECTION)
    #: [2026-11-05] 跟随上游算子的标定线（#12 0.22 / #14 0.23），避免同一份 CLIP 分
    #: 在 #12 判通过、在 #16 判不对齐。false 只用于回归对比（固定用本算子阈值）。
    follow_upstream = _bool(raw_section.get("follow_upstream_thresholds", True),
                            "%s.follow_upstream_thresholds" % SECTION)
    low = _number(raw_section.get("isolation_threshold", DEFAULT_ISOLATION_THRESHOLD),
                  "%s.isolation_threshold" % SECTION)
    high = _number(raw_section.get("auto_align_threshold", DEFAULT_AUTO_ALIGN_THRESHOLD),
                   "%s.auto_align_threshold" % SECTION)

    if not (ISOLATION_RANGE[0] <= low <= ISOLATION_RANGE[1]):
        raise ConfigError("isolation_threshold 必须在 %.2f–%.2f，当前为 %r"
                          % (ISOLATION_RANGE[0], ISOLATION_RANGE[1], low))
    if not (AUTO_ALIGN_RANGE[0] <= high <= AUTO_ALIGN_RANGE[1]):
        raise ConfigError("auto_align_threshold 必须在 %.2f–%.2f，当前为 %r"
                          % (AUTO_ALIGN_RANGE[0], AUTO_ALIGN_RANGE[1], high))
    if high <= low:
        raise ConfigError("auto_align_threshold 必须高于 isolation_threshold")

    raw_model = cfg.get("model") or {}
    if not isinstance(raw_model, dict):
        raise ConfigError("model 必须是对象")
    library = str(raw_model.get("library") or "auto").strip().lower()
    if library not in SUPPORTED_LIBRARIES:
        raise ConfigError("model.library 非法：%s（可选 %s）"
                          % (library, "/".join(SUPPORTED_LIBRARIES)))
    device = str(raw_model.get("device") or "auto").strip().lower()
    if device not in SUPPORTED_DEVICES:
        raise ConfigError("model.device 非法：%s（可选 %s）"
                          % (device, "/".join(SUPPORTED_DEVICES)))
    model_name = raw_model.get("model_name") or "ViT-B-32"
    if not isinstance(model_name, str) or not model_name.strip():
        raise ConfigError("model.model_name 不能为空")
    weights = raw_model.get("weights")
    if weights is not None and (not isinstance(weights, str) or not weights.strip()):
        raise ConfigError("model.weights 必须是本地路径字符串或 null")
    batch_size = _positive_int(raw_model.get("batch_size", DEFAULT_BATCH_SIZE),
                               "model.batch_size")
    allow_degraded = _bool(raw_model.get("allow_degraded", True), "model.allow_degraded")

    normalized = copy.deepcopy(cfg)
    normalized["operator"] = OPERATOR
    normalized["operator_version"] = VERSION
    normalized["stage_id"] = STAGE_ID
    normalized[SECTION] = {
        "enabled": True,
        "isolation_threshold": low,
        "auto_align_threshold": high,
        "rescore_modified_description": True,
        "temporal_route": temporal_route,
        "follow_upstream_thresholds": follow_upstream,
    }
    normalized["model"] = {
        "backend": "clip_vit_b32",
        "library": library,
        "model_name": model_name.strip(),
        "weights": weights.strip() if isinstance(weights, str) else None,
        "device": device,
        "batch_size": batch_size,
        "allow_degraded": allow_degraded,
    }
    return normalized


def apply_overrides(cfg: dict, overrides: Optional[dict]) -> dict:
    """把请求级 config 合并到配置；只允许业务阈值，身份/模型/路径项不可被改写。"""
    merged = copy.deepcopy(cfg)
    if overrides is None:
        return merged
    if not isinstance(overrides, dict):
        raise ConfigError("config 必须是对象")
    unknown = sorted(set(overrides) - ALLOWED_OVERRIDES)
    if unknown:
        raise ConfigError("不支持的配置项：%s" % unknown[0])
    section = merged.get(SECTION)
    if not isinstance(section, dict):
        section = {}
        merged[SECTION] = section
    for key, value in overrides.items():
        section[key] = value
    return merged


def thresholds(cfg: dict) -> Tuple[float, float]:
    section = cfg[SECTION]
    return float(section["isolation_threshold"]), float(section["auto_align_threshold"])


def effective_thresholds(cfg: dict, upstream: Optional[dict] = None
                         ) -> Tuple[float, float, str]:
    """#16 实际使用的分档阈值与来源。

    [2026-11-05] 默认**跟随上游算子自带的标定线**（#12 ``thresholds`` /
    #14 ``low_score_threshold``）：同一份 CLIP 分不能在一个算子里判通过、
    在另一个算子里判不对齐。上游节点没有声明阈值、或
    ``follow_upstream_thresholds=false`` 时才回落到本算子配置。

    自动对齐阈值只在上游给出更高且仍高于隔离线时采用；否则保持本算子配置。
    返回 ``(isolation, auto_align, source)``，``source`` 为 ``upstream``/``config``。
    """
    section = cfg[SECTION]
    low = float(section["isolation_threshold"])
    high = float(section["auto_align_threshold"])
    upstream = upstream or {}
    declared = upstream.get("thresholds")
    if not section.get("follow_upstream_thresholds", True) \
            or not isinstance(declared, dict) or upstream.get("score") is None:
        return low, high, "config"
    used = False
    declared_low = declared.get("review")
    if isinstance(declared_low, (int, float)) and not isinstance(declared_low, bool):
        low = float(declared_low)
        used = True
    declared_high = declared.get("auto")
    if isinstance(declared_high, (int, float)) and not isinstance(declared_high, bool) \
            and float(declared_high) > low and float(declared_high) > high:
        high = float(declared_high)
    if not used:
        return float(section["isolation_threshold"]), high, "config"
    if high <= low:  # 上游只给了更高的隔离线：保留可用的自动档上界
        high = min(low + 0.10, AUTO_ALIGN_RANGE[1])
    return low, high, "upstream"


# ------------------------------------------------------------------ 通用取值

def _dict(value: Any) -> Dict[str, Any]:
    return value if isinstance(value, dict) else {}


def _number_or_none(value: Any) -> Optional[float]:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    number = float(value)
    return number if math.isfinite(number) else None


def round_score(value: Optional[float]) -> Optional[float]:
    return None if value is None else round(float(value), 6)


def text_content(record: dict) -> Optional[str]:
    """文本描述取值顺序（与 #10/#12 一致）：`text.content` → `metadata.description` 等。"""
    record = _dict(record)
    text = record.get("text")
    if isinstance(text, str) and text.strip():
        return text.strip()
    if isinstance(text, dict):
        for key in ("content", "description", "caption"):
            value = text.get(key)
            if isinstance(value, str) and value.strip():
                return value.strip()
    for key in ("description", "caption"):
        value = record.get(key)
        if isinstance(value, str) and value.strip():
            return value.strip()
    for holder in (_dict(record.get("metadata")), _dict(_dict(record.get("processing")).get("09"))):
        for key in ("description", "caption"):
            value = holder.get(key)
            if isinstance(value, str) and value.strip():
                return value.strip()
    return None


def image_reference(record: dict) -> Optional[str]:
    """图像引用取值：`image.path` / `image.file` / `image.file_path` / `image.uri`，或字符串。"""
    record = _dict(record)
    for key in ("image", "image_reference", "image_path"):
        reference = _frame_reference(record.get(key))
        if reference is not None:
            return reference
    return None


def video_reference(record: dict) -> Optional[str]:
    """视频引用取值：`video.path` / `video.file` / `video.file_path` / `video.uri`，或字符串。"""
    record = _dict(record)
    for key in ("video", "video_reference", "video_path", "video_file"):
        reference = _frame_reference(record.get(key))
        if reference is not None:
            return reference
    return None


def audio_reference(record: dict) -> Optional[str]:
    for key in ("audio", "audio_reference", "audio_path", "audio_file"):
        reference = _frame_reference(_dict(record).get(key))
        if reference is not None:
            return reference
    return None


def _frame_reference(frame: Any) -> Optional[str]:
    """单帧引用取值：字符串，或帧对象内的 `path`/`file`/`file_path`/`uri`。"""
    if isinstance(frame, str) and frame.strip():
        return frame.strip()
    if isinstance(frame, dict):
        for key in _MEDIA_REFERENCE_KEYS:
            value = frame.get(key)
            if isinstance(value, str) and value.strip():
                return value.strip()
    return None


def video_frames(record: dict) -> List[str]:
    """按时间顺序展开视频帧引用：``video.segments[].frames`` → ``video.frames``。"""
    video = _dict(_dict(record).get("video"))
    frames: List[str] = []
    segments = video.get("segments")
    for segment in segments if isinstance(segments, list) else []:
        if not isinstance(segment, dict):
            continue
        entries = segment.get("frames")
        if not isinstance(entries, list) or not entries:
            entries = segment.get("keyframes")
        for frame in entries if isinstance(entries, list) else []:
            reference = _frame_reference(frame)
            if reference is not None:
                frames.append(reference)
    if not frames:
        entries = video.get("frames")
        if not isinstance(entries, list) or not entries:
            entries = video.get("keyframes")
        for frame in entries if isinstance(entries, list) else []:
            reference = _frame_reference(frame)
            if reference is not None:
                frames.append(reference)
    return frames


def representative_frame_reference(record: dict) -> Optional[str]:
    """代表帧 = 按时间顺序展开后的中间帧；没有帧时返回 ``None``（绝不编造路径）。"""
    frames = video_frames(record)
    if not frames:
        return None
    return frames[len(frames) // 2]


def _batch_id(pair: dict, sides: Sequence[dict]) -> Optional[str]:
    for holder in [pair] + list(sides):
        value = holder.get("batch_id")
        if isinstance(value, str) and value.strip():
            return value.strip()
        metadata = _dict(holder.get("metadata"))
        value = metadata.get("batch_id")
        if isinstance(value, str) and value.strip():
            return value.strip()
    return None


def retry_count(record: dict) -> int:
    """退回次数取值：`metadata.retry_count` → `retry_count` → `processing["NN"].retry_count`。"""
    record = _dict(record)
    candidates: List[Any] = [_dict(record.get("metadata")).get("retry_count"),
                             record.get("retry_count")]
    processing = _dict(record.get("processing"))
    for key in ("16", "12", "11"):
        node = processing.get(key)
        if isinstance(node, dict):
            candidates.append(node.get("retry_count"))
    for value in candidates:
        if isinstance(value, int) and not isinstance(value, bool) and value >= 0:
            return value
    return 0


# ------------------------------------------------------------------ 记录规范化

def _side_record(side: Any) -> dict:
    if isinstance(side, dict):
        nested = side.get("record")
        if isinstance(nested, dict):
            # #11 may retain namespaces/references on its wrapper. Empty inner
            # values must not mask actual media or upstream evidence there.
            merged = dict(side)
            merged.pop("record", None)
            for key, value in nested.items():
                if key in ("processing", "alignment", "metadata"):
                    merged[key] = dict(_dict(merged.get(key)), **_dict(value))
                elif value is not None and value != "" and value != {}:
                    merged[key] = value
            return merged
        return side
    return {}


def _side_modalities(side: Any) -> List[str]:
    if not isinstance(side, dict):
        return []
    declared = side.get("modalities")
    if isinstance(declared, list):
        return [str(item) for item in declared if isinstance(item, str)]
    return []


def normalize_record(raw: dict) -> Dict[str, Any]:
    """把一行输入规范化为 ``{pair_id, pair, sides, ledger}``。

    支持两种形态：
    1. **#11 候选对**（推荐）：顶层含 ``pair_id`` 与 ``left``/``right``；
    2. **上游台账包裹**：顶层是 ``#12`` 的隔离/异常台账条目，其 ``record`` 内是候选对，
       此时保留台账元信息（``label``/``reason``/``destination``）供"沿用 #12 隔离结论"使用；
    3. **扁平单记录**：本身含 ``sample_id`` + 图文（无 ``pair_id`` 时合成 ``single-<id>``）。
    """
    ledger = None
    payload = raw
    nested = raw.get("record")
    if isinstance(nested, dict) and ("pair_id" in nested or "left" in nested or "right" in nested):
        ledger = {key: raw.get(key) for key in
                  ("line", "sample_id", "reason", "label", "action", "destination",
                   "event_time", "batch_id", "retry_count", "tier") if key in raw}
        payload = nested

    pair_id = payload.get("pair_id")
    if isinstance(pair_id, str) and pair_id.strip():
        left = payload.get("left")
        right = payload.get("right")
        if not isinstance(left, dict) or not isinstance(right, dict):
            raise StructuralError("missing_side")
        return {"pair_id": pair_id.strip(), "pair": payload,
                "sides": [left, right], "ledger": ledger}

    sample_id = payload.get("sample_id")
    if not isinstance(sample_id, str) or not sample_id.strip():
        raise StructuralError("missing_field: pair_id")
    sample_id = sample_id.strip()
    return {"pair_id": "single-%s" % sample_id, "pair": payload,
            "sides": [{"sample_id": sample_id, "modalities": None, "record": payload}],
            "ledger": ledger}


def event_keys(normalized: Dict[str, Any]) -> List[str]:
    """人工事件可用的匹配键：pair_id → 两侧 sample_id → 记录内 sample_id。"""
    keys = [normalized["pair_id"]]
    for side in normalized["sides"]:
        sample_id = side.get("sample_id") if isinstance(side, dict) else None
        if isinstance(sample_id, str) and sample_id.strip():
            keys.append(sample_id.strip())
    record_id = normalized["pair"].get("sample_id")
    if isinstance(record_id, str) and record_id.strip():
        keys.append(record_id.strip())
    deduped: List[str] = []
    for key in keys:
        if key not in deduped:
            deduped.append(key)
    return deduped


def _is_video_side(side: Any, record: dict) -> bool:
    """侧记录是否携带视频：有视频引用，或侧声明 ``modalities`` 含 ``video``。"""
    if video_reference(record) is not None:
        return True
    return "video" in _side_modalities(side)


def resolve_orientation(sides: Sequence[dict]) -> Dict[str, Any]:
    """判定该候选对走哪条整体对齐路线，返回图像/视频侧与文本侧下标。

    路线优先级（表1 #16 原图文口径不变，新增视文路线）：

    1. ``cross``：一侧图像 + 另一侧文本（原图文路线，**最高优先**）；
    2. ``internal``：同一记录内同时有图像与文本（原图文路线）；
    3. ``video_cross`` / ``video_internal``：视频 + 文本（#14 视文路线）；
    4. 都不成立 → 不适用（``kind=None``），留产线不隔离。

    因此 ``video+image+text`` 仍走图像路线；只有完全没有图像对可用时才走视文。
    """
    records = [_side_record(side) for side in sides]
    image_sides = [index for index, record in enumerate(records)
                   if image_reference(record) is not None]
    text_sides = [index for index, record in enumerate(records)
                  if text_content(record) is not None]
    video_sides = [index for index, side in enumerate(sides)
                   if _is_video_side(side, records[index])]
    audio_sides = [index for index, side in enumerate(sides)
                   if audio_reference(records[index]) is not None
                   or "audio" in _side_modalities(side)]

    cross = [(image_index, text_index) for image_index in image_sides
             for text_index in text_sides if image_index != text_index]
    if cross:
        image_index, text_index = cross[0]
        return {"kind": KIND_CROSS, "image_side": image_index, "text_side": text_index,
                "video_side": None, "reason": None, "image_record": records[image_index],
                "text_record": records[text_index], "video_record": None}
    internal = [index for index in image_sides if index in text_sides]
    if internal:
        index = internal[0]
        return {"kind": KIND_INTERNAL, "image_side": index, "text_side": index,
                "video_side": None, "reason": None, "image_record": records[index],
                "text_record": records[index], "video_record": None}

    video_cross = [(video_index, text_index) for video_index in video_sides
                   for text_index in text_sides if video_index != text_index]
    if video_cross:
        video_index, text_index = video_cross[0]
        return {"kind": KIND_VIDEO_CROSS, "image_side": None, "text_side": text_index,
                "video_side": video_index, "reason": None, "image_record": None,
                "text_record": records[text_index], "video_record": records[video_index]}
    video_internal = [index for index in video_sides if index in text_sides]
    if video_internal:
        index = video_internal[0]
        return {"kind": KIND_VIDEO_INTERNAL, "image_side": None, "text_side": index,
                "video_side": index, "reason": None, "image_record": None,
                "text_record": records[index], "video_record": records[index]}

    audio_choices = [(media_index, text_index) for media_index in audio_sides
                     for text_index in text_sides if media_index != text_index]
    audio_choices += [(index, index) for index in audio_sides if index in text_sides]
    if audio_choices:
        media_index, text_index = audio_choices[0]
        return {"kind": KIND_AUDIO_CROSS if media_index != text_index else KIND_AUDIO_INTERNAL,
                "image_side": None, "video_side": None, "audio_side": media_index,
                "text_side": text_index, "reason": None, "image_record": None,
                "text_record": records[text_index], "video_record": None,
                "audio_record": records[media_index]}

    if not image_sides and not video_sides and not text_sides:
        reason = "no_image_text"
    elif not image_sides and not video_sides:
        reason = "no_image"
    else:
        reason = "no_text"
    return {"kind": None, "image_side": None, "text_side": None, "video_side": None,
            "reason": reason, "image_record": None, "text_record": None,
            "video_record": None}


# ------------------------------------------------------------------ 上游评分解析

def _video_score_value(node: Any) -> Tuple[Optional[float], Optional[str]]:
    """从 #14 视文节点取真实分数：``segment_mean`` → ``clip_score`` → ``score`` → ``video_text_score``。

    节点也可以是裸标量（测试台/精简上游只给一个数）。取不到就返回 ``(None, None)``，
    本函数**不做任何默认值/推算**。
    """
    if isinstance(node, bool) or node is None:
        return None, None
    if isinstance(node, (int, float)):
        score = _number_or_none(node)
        return (score, None) if score is not None else (None, None)
    node = _dict(node)
    for key in ("segment_mean", "clip_score", "score", "video_text_score"):
        value = node.get(key)
        if isinstance(value, (int, float)) and not isinstance(value, bool):
            score = _number_or_none(value)
            if score is not None:
                return score, key
    return None, None


def _video_model_fields(node: dict) -> Tuple[Optional[str], Optional[str]]:
    """#14 节点模型标识：``model{name,version}`` / ``model`` 字符串 / ``clip_model`` 兼容。"""
    model_node = node.get("model")
    if isinstance(model_node, dict):
        name = model_node.get("name") or node.get("clip_model") or node.get("model_name")
        version = (model_node.get("version") or node.get("model_version")
                   or node.get("clip_model_version"))
        return name, version
    name = model_node if isinstance(model_node, str) and model_node.strip() else None
    name = name or node.get("clip_model") or node.get("model_name")
    version = node.get("model_version") or node.get("clip_model_version")
    return name, version


def _video_node_info(node: Any, source: str) -> Optional[Dict[str, Any]]:
    """把单个 #14 视文节点转成统一上游信息；不是对象/标量时返回 ``None``。"""
    if node is None or isinstance(node, bool) or not isinstance(node, (int, float, dict)):
        return None
    node_dict = _dict(node)
    score, score_field = _video_score_value(node)
    if score is not None and not -1 <= score <= 1:
        score = None
    tier = node_dict.get("tier")
    if tier is None:
        tier = node_dict.get("process_status")
    model, model_version = _video_model_fields(node_dict)
    info: Dict[str, Any] = {
        "score": score,
        # 没有真实分数时不把来源写成已取到分（score_source 由调用方按此渲染）
        "source": source if score is not None else None,
        "model": model, "model_version": model_version, "tier": tier,
        "isolated": False, "reason": "upstream_video_score_unavailable",
        "origin": ORIGIN_UPSTREAM_14, "score_field": score_field,
        "status": node_dict.get("status"),
        "thresholds": _node_thresholds(node_dict),
        "usable": _score_route_usable(node_dict),
        "degraded": bool(node_dict.get("degraded")),
    }
    if _score_route_failed(node_dict):
        info.update(isolated=True, reason=node_dict.get("reason") or "upstream_route_failed")
        return info
    if score is not None:
        info["reason"] = None
        return info
    if tier == "isolated":
        info.update(tier="isolated", isolated=True, reason="upstream_tier_isolated")
    return info


def _score_route_usable(node: dict) -> bool:
    """Honor explicit route state; legacy scalar CLIP inputs remain supported."""
    if "route_enabled" in node and node["route_enabled"] is not True:
        return False
    if "passed" in node and node["passed"] is not True:
        return False
    state = node.get("process_status") or node.get("status")
    return not node.get("degraded") and node.get("tier") != "isolated" and state not in (
        "failed", "isolated", "skipped", "pending", "not_applicable", "degraded")


def _score_route_failed(node: dict) -> bool:
    return (node.get("route_enabled") is not False and not node.get("degraded")
            and (node.get("passed") is False
                 or node.get("tier") == "isolated"
                 or (node.get("process_status") or node.get("status")) in ("failed", "isolated")))


def _audio_upstream(pair: dict, sides: Sequence[dict]) -> Optional[Dict[str, Any]]:
    """Consume #13's final CER, including the optimized homophone decision.

    Current #13 stores strict and homophone metrics separately and decides with
    their minimum; explicit final metrics take precedence when provided.
    The metric stays separate from CLIP and its larger-is-better buckets.
    """
    holders = [(pair, "")] + [(_side_record(side), "%s.record." % name)
                              for name, side in zip(("left", "right"), sides)]
    for holder, prefix in holders:
        for node, source in ((_dict(holder.get("processing")).get("13"), "processing.13"),
                             (holder.get("audio_text"), "audio_text"),
                             (holder.get("audio_text_score"), "audio_text_score")):
            if not isinstance(node, dict) or not node:
                continue
            metric, field = None, None
            for key in ("final_edit_distance_ratio", "edit_distance_ratio_final", "final_cer"):
                if key in node:
                    metric, field = _number_or_none(node[key]), key
                    break
            if field is None:
                field = "edit_distance_ratio" if "edit_distance_ratio" in node else "cer"
                metric = _number_or_none(node.get(field))
                homophone = _number_or_none(node.get("edit_distance_ratio_homophone"))
                if (metric is not None and metric >= 0 and homophone is not None and homophone >= 0
                        and node.get("homophone_tolerance") is True
                        and not node.get("pinyin_table_unavailable") and homophone < metric):
                    metric, field = homophone, "edit_distance_ratio_homophone"
            valid_metric = metric is not None and metric >= 0
            limit = _number_or_none(node.get("edit_distance_ratio_max"))
            valid_limit = limit is not None and 0 <= limit <= 1
            enabled = node.get("route_enabled") is True
            usable = enabled and node.get("passed") is True and _score_route_usable(node)
            usable = usable and valid_metric and valid_limit
            model, model_version = _video_model_fields(node)
            return {"score": None, "source": prefix + source,
                    "model": model or node.get("transcript_model"),
                    "model_version": model_version or node.get("transcript_model_version"),
                    "tier": None, "isolated": enabled and _score_route_failed(node),
                    "reason": node.get("reason") or "upstream_audio_score_unavailable",
                    "origin": "13", "temporal": None, "thresholds": None,
                    "audio": True, "score_metric": "CER", "score_value": metric if valid_metric else None,
                    "score_field": field, "score_direction": "lower_is_better", "score_threshold": limit,
                    "usable": bool(usable), "degraded": bool(node.get("degraded")),
                    "route_enabled": enabled, "route_passed": node.get("passed")}
    return None


def _node_thresholds(node: Any) -> Optional[Dict[str, Optional[float]]]:
    """取上游算子节点自带的阈值（#12 ``thresholds`` / #14 ``low_score_threshold``）。

    [2026-11-05] 修复阈值口径打架：#12 标定 0.22、#16 固定 0.25，同一份 CLIP 分在
    #12 判"通过"、在 #16 判"不对齐"并进隔离库。这里把上游的真实阈值带上来，
    由 #16 决定是否跟随（``follow_upstream_thresholds``）。
    """
    node = _dict(node)
    if not node:
        return None
    block = _dict(node.get("thresholds"))
    low = _number_or_none(block.get("isolation"))
    if low is None:
        low = _number_or_none(block.get("review"))
    if low is None:
        low = _number_or_none(node.get("low_score_threshold"))
    high = _number_or_none(block.get("auto_align"))
    if high is None:
        high = _number_or_none(block.get("auto"))
    if low is None and high is None:
        return None
    if low is not None and high is not None and high <= low:
        high = None
    return {"review": low, "auto": high}


def _video_upstream(pair: dict, sides: Optional[Sequence[dict]] = None,
                    video_side: Optional[int] = None) -> Optional[Dict[str, Any]]:
    """解析 #14 视文分：pair 顶层 → pair.processing.14 → 视频侧记录的同名字段。

    #14 的 ``video_text_score`` 是**记录级**字段（写在视频侧 ``record`` 内），而 #16 的输入是
    pair 级：两种形态都接受。侧记录只读**视频侧**，避免误用文本侧同名字段。
    没有任何 #14 节点时返回 ``None``（保持"无上游分数"的既有语义）。
    """
    pair = _dict(pair)
    candidates: List[Tuple[Any, str]] = [
        (pair.get("video_text_score"), SOURCE_VIDEO_TEXT_SCORE),
        (_dict(pair.get("processing")).get("14"), SOURCE_PROCESSING_14),
    ]

    indexes: List[int] = []
    if isinstance(video_side, int) and sides and 0 <= video_side < len(sides):
        indexes.append(video_side)
    for index, side in enumerate(sides or []):
        if index in indexes:
            continue
        if _is_video_side(side, _side_record(side)):
            indexes.append(index)
    for index in indexes:
        record = _side_record(sides[index])
        candidates.append((record.get("video_text_score"), SOURCE_VIDEO_TEXT_SCORE))
        candidates.append((_dict(record.get("processing")).get("14"),
                           SOURCE_PROCESSING_14))

    unscored: Optional[Dict[str, Any]] = None
    for index, (node, source) in enumerate(candidates):
        # The scalar alias carries no route state. An explicit processing.14
        # failure/degradation on the same holder cannot be hidden by that alias.
        if source == SOURCE_VIDEO_TEXT_SCORE and index + 1 < len(candidates):
            declared = _dict(candidates[index + 1][0])
            if declared and not _score_route_usable(declared):
                node, source = declared, SOURCE_PROCESSING_14
        info = _video_node_info(node, source)
        if info is None:
            continue
        if info["score"] is not None:
            return info
        if info["isolated"] or info.get("usable") is False:
            return info
        unscored = unscored or info
    return unscored


def _temporal_from_node(node: Any) -> Optional[Dict[str, Any]]:
    """把一个 ``processing["15"]`` 节点归一为结论；不是 #15 节点返回 ``None``。"""
    if not isinstance(node, dict):
        return None
    status = node.get("process_status") or node.get("decision") or node.get("status")
    if not isinstance(status, str) or not status.strip():
        return None
    if node.get("operator") not in (None, "temporal_consistency"):
        return None
    declared_status = status.strip().lower()
    status = declared_status
    # Processing-node strings must not erase explicit gate evidence. The
    # original #15 contract omits these optional flags and remains supported.
    if node.get("passed") is False:
        status = "failed"
    elif node.get("route_enabled") is False:
        status = "skipped"
    elif node.get("degraded") or (declared_status == "passed" and "passed" in node
                                  and node["passed"] is not True):
        status = "pending_calibration"
    result = {
        "status": status,
        "reason_code": node.get("reason_code"),
        "label": node.get("label"),
        "review_required": bool(node.get("review_required")),
        "source": SOURCE_PROCESSING_15,
    }
    for key in ("passed", "route_enabled", "degraded"):
        if key in node:
            result[key] = node[key]
    if status != declared_status:
        result["declared_status"] = declared_status
    return result


def temporal_upstream(pair: dict, sides: Optional[Sequence[dict]] = None
                      ) -> Optional[Dict[str, Any]]:
    """解析 #15 时空一致性结论：pair 顶层 → 两侧侧记录的 ``processing["15"]``。

    [2026-11-05] 时序样本没有 #12/#14 的 CLIP 分（#14 已对其判 not_applicable），
    此前 #16 读不到任何分数就只能给 ``pending_manual_review/no_score``，
    等于把 #15 已经算好的结论丢掉。这里把 #15 的结论接进 #16 的判定链。
    """
    pair = _dict(pair)
    found = _temporal_from_node(_dict(pair.get("processing")).get("15"))
    if found is not None:
        return found
    for side in sides or []:
        record = _side_record(side)
        found = _temporal_from_node(_dict(record.get("processing")).get("15"))
        if found is not None:
            return found
    return None


def resolve_upstream(pair: dict, ledger: Optional[dict] = None,
                     sides: Optional[Sequence[dict]] = None,
                     video_side: Optional[int] = None,
                     temporal_route: bool = True,
                     audio_route: bool = False) -> Dict[str, Any]:
    """解析上游真实评分、模型标识与隔离结论（绝不生成任何分数）。

    取分优先级：``alignment.whole``（上一轮 #16）→ #12 图文分（``image_text_score`` /
    ``processing.12`` / ``alignment.image_text`` / 顶层标量）→ #14 视文分
    （``video_text_score`` / ``processing.14``，含视频侧记录级字段）→ 上游台账。
    已定位的视文对读取当前 #14 路线，图文对保留原有 #12 优先级。
    """
    info: Dict[str, Any] = {
        "score": None, "source": None, "model": None, "model_version": None,
        "tier": None, "isolated": False, "reason": "no_upstream_score", "origin": None,
        "temporal": None, "thresholds": None,
    }
    pair = _dict(pair)

    # Audio-only pairs cannot borrow a legacy image/CLIP score. Applicable #15
    # decisions retain the optimized priority; no-timeline does not mask CER.
    if audio_route:
        temporal = temporal_upstream(pair, sides=sides) if temporal_route else None
        audio = _audio_upstream(pair, sides or [])
        if temporal is not None and (temporal["status"] not in ("not_applicable", "skipped")
                                     or audio is None):
            info.update(source=SOURCE_PROCESSING_15, origin=ORIGIN_UPSTREAM_15,
                        temporal=temporal, reason="temporal_%s" % temporal["status"])
            return info
        if audio is not None:
            info.update(audio)
        return info

    def score_state(node):
        info.update(usable=_score_route_usable(node), degraded=bool(node.get("degraded")))
        if _score_route_failed(node) or (node.get("tier") == "isolated"
                                         and node.get("route_enabled") is not False):
            info.update(isolated=True, reason=node.get("reason") or "upstream_tier_isolated")
        if info.get("score") is not None and not -1 <= info["score"] <= 1:
            info.update(score=None, reason="upstream_score_invalid")
        return info

    if video_side is not None:
        # An active pair-level quarantine is binding even when this view no
        # longer carries an image. Reuse the decision only, never its CLIP score.
        image_state = (_dict(_dict(pair.get("processing")).get("12"))
                       or _dict(pair.get("image_text_score"))
                       or _dict(_dict(pair.get("alignment")).get("image_text")))
        state = image_state.get("process_status") or image_state.get("status")
        if state not in ("not_applicable", "skipped", "route_disabled") and _score_route_failed(image_state):
            info.update(origin=ORIGIN_UPSTREAM_12, tier=image_state.get("tier"))
            return score_state(image_state)

    holders = [pair] + [_side_record(side) for side in sides or []]

    def current_nodes(holder):
        processing = _dict(holder.get("processing"))
        if video_side is not None:
            return (holder.get("video_text_score"), processing.get("14"))
        return (holder.get("image_text_score"), processing.get("12"),
                _dict(holder.get("alignment")).get("image_text"),
                holder.get("video_text_score"), processing.get("14"))

    blocked_current = any(isinstance(node, dict) and node and not _score_route_usable(node)
                          for holder in holders for node in current_nodes(holder))

    previous = _dict(_dict(pair.get("alignment")).get("whole"))
    if isinstance(previous.get("clip_score"), (int, float)) \
            and not isinstance(previous.get("clip_score"), bool) and not blocked_current:
        info.update(score=_number_or_none(previous.get("clip_score")),
                    source="alignment.whole", model=previous.get("model"),
                    model_version=previous.get("model_version"), origin="previous_16")
        return info

    if video_side is not None:
        # A video/text orientation has no active image/text pairing. This applies
        # equally to root aliases and old scores on either preserved side.
        return _fallback_upstream(info, pair, ledger, sides, video_side, temporal_route)

    score_node = pair.get("image_text_score")
    if isinstance(score_node, dict):
        declared = _dict(_dict(pair.get("processing")).get("12"))
        if declared and not _score_route_usable(declared):
            # The canonical node supplies state even when the dict alias only
            # retained an older score (legacy #12 need not expose `passed`).
            score_node = dict(score_node, **declared)
        score = _number_or_none(score_node.get("clip_score"))
        model_node = _dict(score_node.get("model"))
        tier = score_node.get("tier")
        if score is not None:
            info.update(score=score, source="image_text_score", tier=tier,
                        model=model_node.get("name"), model_version=model_node.get("version"),
                        origin=ORIGIN_UPSTREAM_12,
                        thresholds=_node_thresholds(score_node))
            return score_state(score_node)
        if tier == "isolated":
            info.update(tier=tier, isolated=True, reason="upstream_tier_isolated",
                        origin=ORIGIN_UPSTREAM_12)
            return score_state(score_node)
        if not _score_route_usable(score_node):
            info.update(origin=ORIGIN_UPSTREAM_12)
            return score_state(score_node)

    node = _dict(_dict(pair.get("processing")).get("12"))
    if node:
        score = _number_or_none(node.get("clip_score"))
        tier = node.get("tier")
        if score is not None:
            info.update(score=score, source="processing.12", tier=tier,
                        model=node.get("model"), model_version=node.get("model_version"),
                        origin=ORIGIN_UPSTREAM_12,
                        thresholds=_node_thresholds(node))
            return score_state(node)
        if tier == "isolated":
            info.update(tier=tier, isolated=True, reason="upstream_tier_isolated",
                        origin=ORIGIN_UPSTREAM_12)
            return score_state(node)
        if not _score_route_usable(node):
            info.update(origin=ORIGIN_UPSTREAM_12)
            return score_state(node)

    image_text = _dict(_dict(pair.get("alignment")).get("image_text"))
    if image_text:
        score = _number_or_none(image_text.get("clip_score") if image_text.get("clip_score") is not None
                                else image_text.get("score"))
        if score is not None:
            model_node = _dict(image_text.get("model"))
            info.update(score=score, source="alignment.image_text",
                        tier=image_text.get("tier"), model=model_node.get("name"),
                        model_version=model_node.get("version") or image_text.get("model_version"),
                        origin=ORIGIN_UPSTREAM_12,
                        thresholds=_node_thresholds(image_text))
            return score_state(image_text)

    for key in ("image_text_score", "clip_score"):
        score = _number_or_none(pair.get(key))
        if score is not None:
            info.update(score=score, source="top_level.%s" % key,
                        origin=ORIGIN_UPSTREAM_12)
            return info

    # #11 preserves record-level namespaces. Root scores retain their priority.
    for side in sides or []:
        side_info = resolve_upstream(_side_record(side), temporal_route=False)
        if side_info.get("origin") in (ORIGIN_UPSTREAM_12, "previous_16"):
            return side_info

    return _fallback_upstream(info, pair, ledger, sides, video_side, temporal_route)


def _fallback_upstream(info, pair, ledger, sides, video_side, temporal_route):
    video_info = _video_upstream(pair, sides=sides, video_side=video_side)
    video_pending = (video_side is not None and video_info is not None
                     and video_info.get("usable") is False
                     and video_info.get("tier") not in ("not_applicable", "skipped", "route_disabled"))
    if video_info is not None and (video_info.get("score") is not None or video_info.get("isolated")
                                   or video_pending):
        info.update(video_info)
        return info

    # [2026-11-05] 时序路线：没有任何 CLIP 分时，用 #15 的时间轴结论（不覆盖上面的分）
    temporal_info = temporal_upstream(pair, sides=sides) if temporal_route else None
    audio_info = _audio_upstream(pair, sides or [])
    if temporal_info is not None and (temporal_info["status"] not in ("not_applicable", "skipped")
                                      or audio_info is None):
        info.update(source=SOURCE_PROCESSING_15, origin=ORIGIN_UPSTREAM_15,
                    temporal=temporal_info,
                    reason="temporal_%s" % temporal_info["status"])
        return info

    # Keep #15 independent: CER is only a fallback after the existing temporal
    # decision, so language/reliability gates cannot mask the timeline route.
    if audio_info is not None:
        info.update(audio_info)
        return info
    if video_info is not None:
        info.update(video_info)
        return info

    # 上游台账（#12 隔离清单）直接承载"语义不通"结论
    if isinstance(ledger, dict):
        label = ledger.get("label")
        tier = ledger.get("tier")
        if label == LABEL_NOT_ALIGNED or tier == "isolated":
            info.update(tier="isolated", isolated=True, reason="upstream_ledger_isolated",
                        origin="bundle")
            return info
    whole = _dict(_dict(pair.get("alignment")).get("whole"))
    if whole.get("status") == "rejected":
        info.update(tier="isolated", isolated=True, reason="upstream_status_rejected",
                    origin="legacy")
    return info


def sampling_hit(record: dict) -> Tuple[bool, Optional[str]]:
    """#24 抽检命中标记；命中样本即使高分也必须走人工队列。"""
    record = _dict(record)
    metadata = _dict(record.get("metadata"))
    if metadata.get("sampling_hit") is True:
        return True, "metadata.sampling_hit"
    sampling = record.get("sampling") if isinstance(record.get("sampling"), dict) \
        else metadata.get("sampling")
    if isinstance(sampling, dict) and sampling.get("hit") is True:
        return True, "sampling.hit"
    node = _dict(_dict(record.get("processing")).get("24"))
    for key in ("sampling_hit", "hit"):
        if node.get(key) is True:
            return True, "processing.24.%s" % key
    whole = _dict(_dict(record.get("alignment")).get("whole"))
    if whole.get("sampling_hit") is True:
        return True, "alignment.whole.sampling_hit"
    return False, None


def human_veto_locked(record: dict) -> bool:
    """人工"不对齐"锁定：一旦人工判不对齐，后续高分不得改回通过。"""
    whole = _dict(_dict(_dict(record).get("alignment")).get("whole"))
    if whole.get("human_veto_locked") is True:
        return True
    return whole.get("confirmation_method") == CONFIRM_HUMAN \
        and whole.get("decision") == DECISION_NOT_ALIGNED


# ------------------------------------------------------------------ 人工事件

_HUMAN_DECISIONS = {
    "aligned": "aligned", "对齐": "aligned", "aligned_after_edit": "aligned",
    "not_aligned": "not_aligned", "不对齐": "not_aligned",
    "uncertain": "uncertain", "存疑": "uncertain",
}

_EVENT_KINDS = {
    "human_event_missing_target": "事件缺少 pair_id/sample_id",
    "human_event_missing_reviewer": "事件缺少 reviewer_id（人工事件权限校验）",
    "human_event_missing_reviewed_at": "事件缺少 reviewed_at",
    "human_event_unknown_decision": "事件 decision 非 对齐/不对齐/存疑",
    "human_event_invalid_description": "事件 new_description 非法",
    "human_event_no_action": "事件既无结论也无改写描述",
    "human_event_target_not_found": "事件无法匹配任何输入记录",
    "human_event_encoding_error": "事件文件存在非 UTF-8 行",
    "human_event_json_parse_error": "事件文件存在 JSON 语法错误行",
    "human_event_record_not_object": "事件行不是 JSON 对象",
}


class HumanEventSet(object):
    """人工回写事件集合：按事件键索引，保留被覆盖与非法事件供审计。"""

    __slots__ = ("by_key", "superseded", "rejected", "total", "blank_lines")

    def __init__(self) -> None:
        self.by_key: Dict[str, dict] = {}
        self.superseded: List[dict] = []
        self.rejected: List[dict] = []
        self.total = 0
        self.blank_lines = 0

    def lookup(self, keys: Sequence[str]) -> Optional[dict]:
        for key in keys:
            event = self.by_key.get(key)
            if event is not None:
                return event
        return None


def normalize_event(raw: dict, line: int) -> Tuple[Optional[dict], Optional[dict]]:
    """校验并规范化一条人工事件；返回 ``(事件, 拒绝条目)``，二者互斥。"""
    if not isinstance(raw, dict):
        return None, _reject_event(line, "human_event_record_not_object", None)

    key_kind = None
    key_value = None
    for candidate in ("pair_id", "sample_id"):
        value = raw.get(candidate)
        if isinstance(value, str) and value.strip():
            key_kind, key_value = candidate, value.strip()
            break
    if key_value is None:
        return None, _reject_event(line, "human_event_missing_target", raw)

    reviewer = raw.get("reviewer_id")
    if not isinstance(reviewer, str) or not reviewer.strip():
        return None, _reject_event(line, "human_event_missing_reviewer", raw)
    reviewed_at = raw.get("reviewed_at")
    if not isinstance(reviewed_at, str) or not reviewed_at.strip():
        return None, _reject_event(line, "human_event_missing_reviewed_at", raw)

    decision = None
    if raw.get("decision") is not None:
        normalized = _HUMAN_DECISIONS.get(str(raw.get("decision")).strip())
        if normalized is None:
            return None, _reject_event(line, "human_event_unknown_decision", raw)
        decision = normalized

    new_description = raw.get("new_description")
    if new_description is not None:
        if not isinstance(new_description, str) or not new_description.strip():
            return None, _reject_event(line, "human_event_invalid_description", raw)
        new_description = new_description.strip()
    if decision is None and new_description is None:
        return None, _reject_event(line, "human_event_no_action", raw)

    comment = raw.get("comment")
    if comment is not None:
        comment = str(comment)
    previous = raw.get("previous_decision")
    if previous is not None:
        previous = str(previous)
    task_id = raw.get("review_task_id")
    if task_id is not None:
        task_id = str(task_id)
    event = {
        "key": key_value,
        "key_kind": key_kind,
        "decision": decision,
        "reviewer_id": reviewer.strip(),
        "reviewed_at": reviewed_at.strip(),
        "review_task_id": task_id,
        "comment": comment,
        "previous_decision": previous,
        "new_description": new_description,
        "simulated_human": raw.get("simulated_human") is True,
        "line": line,
    }
    return event, None


def _reject_event(line: int, reason: str, raw: Any) -> dict:
    summary: Dict[str, Any] = {}
    if isinstance(raw, dict):
        for key in ("pair_id", "sample_id", "decision", "reviewer_id"):
            value = raw.get(key)
            if isinstance(value, str) and value.strip():
                summary[key] = value.strip()
    return {
        "kind": "human_event_rejected",
        "line": line,
        "reason": reason,
        "message": _EVENT_KINDS.get(reason, reason),
        "label": LABEL_FLOW_INVALID_HUMAN_EVENT,
        "stage": STAGE_NAME,
        "operator": OPERATOR,
        "action": "reject_event",
        "destination": DEST_MANUAL_EVENT,
        "event_summary": summary,
    }


def load_human_events(lines: Iterable[Tuple[int, Optional[dict], Optional[str]]]) -> HumanEventSet:
    """流式读取人工回写事件 JSONL；非法事件逐行进审计台账，不静默丢弃。"""
    events = HumanEventSet()
    for lineno, raw, error in lines:
        if error == op_io.BLANK_LINE:
            events.blank_lines += 1
            continue
        events.total += 1
        if error is not None:
            events.rejected.append(_reject_event(
                lineno, "human_event_%s" % error, None))
            continue
        event, rejected = normalize_event(raw, lineno)
        if rejected is not None:
            events.rejected.append(rejected)
            continue
        if event is None:  # pragma: no cover - normalize_event 二者互斥
            continue
        existing = events.by_key.get(event["key"])
        if existing is None:
            events.by_key[event["key"]] = event
            continue
        keep, drop = (event, existing) if event["reviewed_at"] >= existing["reviewed_at"] \
            else (existing, event)
        events.by_key[event["key"]] = keep
        events.superseded.append({
            "kind": "human_event_superseded",
            "key": drop["key"],
            "line": drop["line"],
            "reviewed_at": drop["reviewed_at"],
            "kept_reviewed_at": keep["reviewed_at"],
            "reason": "同键事件已被更晚 reviewed_at 覆盖",
            "event_time": None,
        })
    return events


def inline_event(record: dict) -> Optional[dict]:
    """记录内联的人工结论（兼容上游把回写结果直接写进记录）。"""
    record = _dict(record)
    candidates = [
        _dict(record.get("metadata")).get("human_review"),
        _dict(_dict(record.get("alignment")).get("whole")).get("human_review"),
        _dict(_dict(record.get("processing")).get("16")).get("human_review"),
    ]
    for candidate in candidates:
        if isinstance(candidate, dict):
            event, rejected = normalize_event(candidate, -1)
            if event is not None:
                return event
            if rejected is not None:
                rejected["reason"] = "inline_%s" % rejected["reason"]
                return None
    return None


# ------------------------------------------------------------------ 重评分

def perform_rescore(encoder: Any, image_target: Any, new_text: str) -> Dict[str, Any]:
    """用注入的同一 CLIP 实例对"人工改写后的描述"重算真实余弦；不可用即如实上报。"""
    from utils import backends

    if encoder is None:
        return {"status": REASON_MODEL_UNAVAILABLE, "score": None, "model": None}
    try:
        text_vectors = encoder.encode_texts([new_text])
        image_vectors, errors = encoder.encode_images([image_target])
    except backends.BackendUnavailable as exc:
        return {"status": "inference_failed: %s" % type(exc).__name__, "score": None,
                "model": None}
    if errors and errors[0]:
        return {"status": errors[0], "score": None, "model": None}
    if not text_vectors or not image_vectors or image_vectors[0] is None:
        return {"status": "empty_embedding", "score": None, "model": None}
    score = backends.cosine_similarity(text_vectors[0], image_vectors[0])
    identity = encoder.model_identity() if hasattr(encoder, "model_identity") else None
    return {"status": "ok", "score": round_score(score), "model": identity}


def _description_summary(text: Optional[str], limit: int = 30) -> Optional[str]:
    if not isinstance(text, str) or not text.strip():
        return None
    stripped = text.strip()
    return stripped if len(stripped) <= limit else stripped[:limit] + "…"


def _description_ref(text: str) -> Dict[str, Any]:
    """新描述引用：只存哈希/长度/短预览，不把完整原文写进血缘或日志。"""
    stripped = text.strip()
    return {
        "sha256": hashlib.sha256(stripped.encode("utf-8")).hexdigest(),
        "length": len(stripped),
        "preview": stripped[:40],
    }


# ------------------------------------------------------------------ 决策

def bucket_by_score(score: Optional[float], low: float, high: float,
                    sampling: bool = False) -> Tuple[Optional[str], str]:
    """表1 #16 算法 1)~3)：只按 0.25 / 0.32 两个阈值分档。

    ``sampling`` 仅为「被抽检抽中」这一条附加规则服务（算法 2)），
    人改描述后的重分档（算法 4)）不重放该规则，因为样本本就已在人工队列中。
    """
    if score is None:
        return None, "no_score"
    if score < low:
        return DECISION_NOT_ALIGNED, "score_below_isolation"
    if sampling:
        return DECISION_PENDING_MANUAL_REVIEW, "sampling_hit"
    if score < high:
        return DECISION_PENDING_MANUAL_REVIEW, "borderline_score"
    return DECISION_AUTO_ALIGNED, "score_above_auto_threshold"


def decide(pair_id: str, *, cfg: dict, upstream: Dict[str, Any], sampling: bool,
           sampling_source: Optional[str], event: Optional[dict], veto_locked: bool,
           encoder: Any, image_target: Any, prior_decision: Optional[str],
           review_task_id: str, now: str, batch_id: Optional[str],
           retry_count_value: int,
           rescore_unavailable_reason: Optional[str] = None) -> Dict[str, Any]:
    """按表1 #16 顺序给出整体对齐结论（纯函数，便于单测）。

    ``rescore_unavailable_reason``：人工改描述但媒体不可用时的显式原因
    （如视文路线无代表帧），只用来说明 ``description_edit.process_status``，不产生分数。
    """
    low, high, threshold_source = effective_thresholds(cfg, upstream)
    allow_degraded = bool(cfg["model"]["allow_degraded"])
    notes: List[str] = []

    score = upstream["score"]
    score_source = upstream["source"] or SOURCE_NONE
    model_name = upstream["model"]
    model_version = upstream["model_version"]
    rescore_info: Optional[Dict[str, Any]] = None
    rescored = False
    hash_before = upstream["score"]
    new_description = event["new_description"] if event else None

    if new_description is not None:
        if image_target is None:
            # 视文路线：改描述必须用代表帧重算；没有可用帧时显式延后，不假装重算过
            result = {"status": (rescore_unavailable_reason
                                 or REASON_NO_REPRESENTATIVE_FRAME),
                      "score": None, "model": None}
        else:
            result = perform_rescore(encoder, image_target, new_description)
        rescore_info = result
        if result["status"] == "ok":
            score = result["score"]
            score_source = SOURCE_RESCORE
            rescored = True
            identity = result.get("model") or {}
            model_name = identity.get("name") or model_name
            model_version = identity.get("version") or model_version
        else:
            notes.append("rescore_status=%s" % result["status"])
            if not allow_degraded and result["status"] == REASON_MODEL_UNAVAILABLE:
                raise RescoreUnavailable(
                    "人工改写描述后必须重评分，但本地 CLIP 不可用且 allow_degraded=false")

    decision: str
    reason: Optional[str] = None
    confirmation = CONFIRM_AUTO
    human_verified = False

    #: 表1 #16 算法 3)：低于 0.25 不自行编造结论，沿用评分工具（#12）的隔离结果
    isolation_source: Dict[str, Any] = {
        "origin": upstream.get("origin"),
        "tier": upstream.get("tier"),
        "isolated": bool(upstream.get("isolated")),
        "upstream_reason": upstream.get("reason"),
        "upstream_score": round_score(upstream.get("score")),
        "reused": False,
    }

    if veto_locked or (event is not None and event["decision"] == "not_aligned"):
        # 边界：人工点了不对齐，后续任何高分都不能改回通过
        decision = DECISION_NOT_ALIGNED
        confirmation = CONFIRM_HUMAN
        human_verified = event is not None and event["decision"] == "not_aligned"
        reason = "human_veto_locked" if veto_locked else "human_not_aligned"
    elif new_description is not None and rescored:
        # 表1 #16 算法 4)：人补写/改写描述后，必须用同一 CLIP 实例对新文本重算，
        # 并「仍按 0.25 / 0.32 分档」——不沿用旧分，也不被事件里的旧结论直接覆盖。
        decision, reason = bucket_by_score(score, low, high)
        reason = "rescore_%s" % reason
        confirmation = CONFIRM_RESCORE
        human_verified = True
        if decision == DECISION_NOT_ALIGNED:
            isolation_source["reused"] = True
    elif new_description is not None and rescore_info is not None \
            and rescore_info["status"] != "ok":
        decision = DECISION_PENDING_MANUAL_REVIEW
        reason = "rescore_%s" % rescore_info["status"]
    elif event is not None and event["decision"] == "aligned":
        decision = DECISION_MANUAL_ALIGNED
        confirmation = CONFIRM_HUMAN
        human_verified = True
        reason = "human_aligned"
    elif event is not None and event["decision"] == "uncertain":
        decision = DECISION_UNCERTAIN
        confirmation = CONFIRM_HUMAN
        human_verified = True
        reason = "human_uncertain"
    elif upstream.get("audio"):
        confirmation = "audio_13"
        metric, limit = upstream.get("score_value"), upstream.get("score_threshold")
        if upstream.get("isolated"):
            decision, reason = DECISION_NOT_ALIGNED, upstream["reason"]
            isolation_source["reused"] = True
        elif not upstream.get("usable"):
            decision, reason = DECISION_PENDING_MANUAL_REVIEW, "upstream_audio_score_unavailable"
        elif metric > limit:
            decision, reason = DECISION_NOT_ALIGNED, "audio_cer_exceeds_threshold"
            isolation_source["reused"] = True
        elif sampling:
            decision, reason = DECISION_PENDING_MANUAL_REVIEW, "sampling_hit"
        else:
            decision, reason = DECISION_AUTO_ALIGNED, "audio_cer_within_threshold"
    elif upstream.get("isolated"):
        decision, reason = DECISION_NOT_ALIGNED, upstream["reason"]
        isolation_source["reused"] = True
    elif upstream.get("usable") is False and score is not None:
        decision, reason = DECISION_PENDING_MANUAL_REVIEW, "upstream_score_unavailable"
    elif score is None and isinstance(upstream.get("temporal"), dict):
        # [2026-11-05] 时序路线：没有 CLIP 分时，#15 的时间轴结论即 #16 的结论。
        # 此前该分支走 ``no_score`` → 全部押给人工队列，#15 判通过的样本也进不了样本池。
        temporal = upstream["temporal"]
        status = str(temporal.get("status") or "").strip().lower()
        confirmation = CONFIRM_TEMPORAL
        if status == "passed":
            decision = DECISION_AUTO_ALIGNED
            reason = REASON_TEMPORAL_PASSED
        elif status in ("failed", "isolated", "not_aligned"):
            decision = DECISION_NOT_ALIGNED
            reason = REASON_TEMPORAL_EXCEEDED
            isolation_source["reused"] = True
        elif status == "pending_calibration":
            decision = DECISION_PENDING_MANUAL_REVIEW
            reason = REASON_TEMPORAL_PENDING
        elif status == "skipped":
            decision = DECISION_PENDING_MANUAL_REVIEW
            reason = "temporal_skipped"
        else:
            # not_applicable（窗口没有可用时间轴）等：不猜结论，交人工
            decision = DECISION_PENDING_MANUAL_REVIEW
            reason = REASON_TEMPORAL_NOT_APPLICABLE
        isolation_source["temporal"] = dict(temporal)
    elif score is None:
        if upstream["isolated"]:
            decision = DECISION_NOT_ALIGNED
            reason = upstream["reason"]
            isolation_source["reused"] = True
        else:
            decision = DECISION_PENDING_MANUAL_REVIEW
            reason = "no_score"
    else:
        decision, reason = bucket_by_score(score, low, high, sampling=sampling)
        if decision == DECISION_NOT_ALIGNED:
            # 沿用 #12 的隔离记录，而不是自造一个同级结论
            isolation_source["reused"] = True
            if upstream["isolated"]:
                reason = upstream["reason"] or "upstream_isolated"

    if rescored and score is not None and score < low:
        notes.append("rescore_below_isolation")
    if veto_locked and event is None:
        confirmation = CONFIRM_HUMAN

    degraded = bool(upstream.get("degraded") or (new_description is not None and rescore_info is not None
                    and rescore_info["status"] != "ok"))
    if degraded and decision == DECISION_AUTO_ALIGNED:
        # 重评分失败时不得用旧分数给出"自动对齐"
        decision = DECISION_PENDING_MANUAL_REVIEW
        reason = "rescore_%s" % rescore_info["status"]

    description_edit = None
    if new_description is not None:
        description_edit = {
            "old_description_summary": _description_summary(upstream.get("old_description")),
            "new_description_ref": _description_ref(new_description),
            "score_before": round_score(hash_before),
            "score_after": round_score(score) if rescored else None,
            "model_version": model_version,
            # [v4.2] status 双字段：int status（1 成功/0 失败）+ 原字符串迁入 process_status
            "status": (1 if rescore_info["status"] == "ok" else 0) if rescore_info else None,
            "process_status": rescore_info["status"] if rescore_info else None,
        }

    label, action, destination = DECISION_TABLE[decision]
    return {
        "decision": decision,
        "reason": reason,
        "label": label,
        "action": action,
        "destination": destination,
        "confirmation_method": confirmation,
        "human_verified": human_verified,
        "rescored": rescored,
        "rescore_status": rescore_info["status"] if rescore_info else None,
        "clip_score": round_score(score),
        "score_source": score_source,
        "model": model_name,
        "model_version": model_version,
        "veto_locked": bool(veto_locked or (event is not None
                                           and event["decision"] == "not_aligned")),
        "sampling_hit": bool(sampling),
        "description_edit": description_edit,
        "degraded": degraded,
        #: 表1 #16 算法 4) 的追溯字段：档位到底由新重评分、上游 CLIP 分还是 #15 给出
        "bucketed_by": ("audio_13" if upstream.get("audio") else
                        CONFIRM_RESCORE if (new_description is not None and rescored)
                        else (CONFIRM_TEMPORAL if (score is None and confirmation == CONFIRM_TEMPORAL)
                              else ("upstream_score" if score is not None else "none"))),
        "human_rewritten": new_description is not None,
        "human_decision": event["decision"] if event else None,
        "isolation_source": isolation_source,
        "notes": notes,
        "thresholds": {"review": low, "auto": high},
        #: [2026-11-05] 本次分档用的是本算子配置还是上游算子标定线
        "threshold_source": threshold_source,
        "review_task_id": review_task_id if decision in (
            DECISION_PENDING_MANUAL_REVIEW, DECISION_UNCERTAIN) or event else None,
        "reviewer_id": event["reviewer_id"] if event else None,
        "reviewed_at": event["reviewed_at"] if event else None,
        "previous_decision": (event.get("previous_decision") if event else None)
        or prior_decision,
        "comment": event["comment"] if event else None,
        "event": event,
        "sampling_source": sampling_source,
        "upstream": upstream,
        "score_metric": upstream.get("score_metric"),
        "score_value": round_score(upstream.get("score_value")),
        "score_direction": upstream.get("score_direction"),
        "score_threshold": upstream.get("score_threshold"),
        "score_field": upstream.get("score_field"),
        "now": now,
        "batch_id": batch_id,
        "retry_count": retry_count_value,
    }


# ------------------------------------------------------------------ 结果容器

class AlignmentResult(object):
    """一次整体对齐运行的产物与真实统计。"""

    __slots__ = ("main", "review", "isolation", "not_applicable", "rejects", "audit",
                 "stats")

    def __init__(self, main: List[dict], review: List[dict], isolation: List[dict],
                 not_applicable: List[dict], rejects: List[dict], audit: List[dict],
                 stats: Dict[str, Any]) -> None:
        self.main = main
        self.review = review
        self.isolation = isolation
        self.not_applicable = not_applicable
        self.rejects = rejects
        self.audit = audit
        self.stats = stats


def empty_stats() -> Dict[str, Any]:
    return {
        "input": 0, "blank_lines": 0, "valid": 0, "applicable": 0, "not_applicable": 0,
        "bad_lines": 0, "structural_rejects": 0,
        "auto_aligned": 0, "manual_aligned": 0, "pending_manual_review": 0,
        "uncertain": 0, "not_aligned": 0,
        "review_entries": 0, "audit_entries": 0, "reject_entries": 0,
        "human_events_total": 0, "human_events_applied": 0, "human_events_rejected": 0,
        "human_events_superseded": 0, "human_events_unmatched": 0,
        "rescored": 0, "rescore_requested": 0, "rescore_deferred": 0,
        "sampling_hits": 0, "veto_locked": 0, "media_carried": 0, "media_missing": 0,
        "models_available": False, "degraded": False, "degradation_reason": None,
        "conservation_ok": False, "empty_input": False,
    }


def make_ledger(line: int, pair_id: Optional[str], sample_id: Optional[str], reason: str,
                label: str, action: str, destination: str, now: str,
                batch_id: Optional[str] = None, retry_count_value: int = 0,
                evidence: Optional[dict] = None, record: Optional[dict] = None,
                extra: Optional[dict] = None) -> Dict[str, Any]:
    """统一质量台账条目（表1 §2.3 字段）。"""
    item: Dict[str, Any] = {
        "line": line,
        "pair_id": pair_id,
        "sample_id": sample_id,
        "reason": reason,
        "label": label,
        "stage": STAGE_NAME,
        "operator": OPERATOR,
        "operator_label": OPERATOR_LABEL,
        "action": action,
        "destination": destination,
        "event_time": now,
        "batch_id": batch_id,
        "retry_count": retry_count_value,
        "evidence": evidence or {},
    }
    if record is not None:
        item["record"] = record
    if extra:
        item.update(extra)
    return item


def _origin_reject(line: int, sample_id: Optional[str], reason: str, now: str,
                   batch_id: Optional[str] = None, raw: Optional[dict] = None) -> dict:
    label = ORIGIN_LEDGER.get(reason, LABEL_ORIGIN_UNPARSABLE)
    item = make_ledger(line, None, sample_id, reason, label, "记录并隔离", DEST_ORIGIN, now,
                       batch_id=batch_id, evidence={"stage": "原始文件问题，进原始隔离区"})
    if raw is not None:
        item["source_reference"] = {"pair_id": raw.get("pair_id"),
                                    "sample_id": raw.get("sample_id")}
    return item


# ------------------------------------------------------------------ 主规划

def _media_target(media_root: Any, reference: Optional[str]) -> Tuple[Optional[str], Any, Optional[str]]:
    """校验并解析图像引用；返回 ``(相对路径, 绝对路径, 错误原因)``。"""
    if reference is None:
        return None, None, "media_missing"
    try:
        relative = op_io.safe_media_path(reference)
    except ValueError:
        return None, None, "media_path_invalid"
    root = Path(media_root).resolve()
    target = (root / relative).resolve()
    try:
        target.relative_to(root)
    except ValueError:
        return None, None, "media_path_invalid"
    if not target.is_file():
        return relative, target, "media_missing"
    return relative, target, None


def plan_alignment(lines: Iterable[Tuple[int, Optional[dict], Optional[str]]], cfg: dict,
                   *, unique_id: str, media_root: Any, now: str,
                   encoder: Any = None, events: Optional[HumanEventSet] = None,
                   model_reason: Optional[str] = None) -> AlignmentResult:
    """流式消费上游候选对，产出主清单/人工队列/隔离台账/不适用台账/异常台账/审计台账。

    ``encoder is None`` 表示本地 CLIP 不可用：常规分档仍按 #12 的真实分数完成，
    只有"人工改写描述"的记录会显式延后为 ``pending_manual_review``（**绝不伪造新分数**）。
    """
    validated = validate_config(cfg)
    low, high = thresholds(validated)
    events = events or HumanEventSet()

    main: List[dict] = []
    review: List[dict] = []
    isolation: List[dict] = []
    not_applicable: List[dict] = []
    rejects: List[dict] = []
    audit: List[dict] = list(events.rejected)
    stats = empty_stats()
    stats["models_available"] = encoder is not None
    stats["human_events_total"] = events.total
    stats["human_events_rejected"] = len(events.rejected)
    stats["human_events_superseded"] = len(events.superseded)
    audit.extend(events.superseded)

    seen_pair_ids = set()
    matched_event_keys = set()

    for lineno, raw, error in lines:
        if error == op_io.BLANK_LINE:
            stats["blank_lines"] += 1
            continue
        stats["input"] += 1
        if error is not None:
            stats["bad_lines"] += 1
            rejects.append(_origin_reject(lineno, None, error, now))
            continue

        raw = raw or {}
        try:
            normalized = normalize_record(raw)
        except StructuralError as exc:
            stats["structural_rejects"] += 1
            rejects.append(_origin_reject(lineno, raw.get("sample_id"), exc.reason, now,
                                          batch_id=raw.get("batch_id"), raw=raw))
            continue

        pair = normalized["pair"]
        pair_id = normalized["pair_id"]
        if pair_id in seen_pair_ids:
            stats["structural_rejects"] += 1
            rejects.append(_origin_reject(lineno, pair_id, "duplicate_pair_id", now,
                                          batch_id=pair.get("batch_id"), raw=pair))
            continue
        seen_pair_ids.add(pair_id)

        batch_id = _batch_id(pair, normalized["sides"])
        retries = retry_count(pair)
        orientation = resolve_orientation(normalized["sides"])
        if orientation["kind"] is None:
            stats["not_applicable"] += 1
            not_applicable.append(make_ledger(
                lineno, pair_id, pair.get("sample_id"), orientation["reason"],
                LABEL_FLOW_NOT_APPLICABLE, "pass_through", DEST_PIPELINE_WAIT, now,
                batch_id=batch_id, retry_count_value=retries,
                evidence={"stage": "流程状态：本配对不含图文整体对齐所需模态，留产线不隔离",
                          "modality_pair": pair.get("modality_pair")},
                record=pair, extra={"entry_kind": "not_applicable"}))
            continue

        keys = event_keys(normalized)
        event = events.lookup(keys)
        if event is None:
            event = inline_event(pair)
        if event is not None:
            matched_event_keys.add(event["key"])

        upstream = resolve_upstream(pair, normalized.get("ledger"),
                                    sides=normalized["sides"],
                                    video_side=orientation.get("video_side"),
                                    audio_route=orientation["kind"] in AUDIO_KINDS,
                                    temporal_route=bool(
                                        validated[SECTION].get("temporal_route", True)))
        upstream["old_description"] = text_content(orientation["text_record"])
        sampling, sampling_source = sampling_hit(pair)
        records = [_side_record(side) for side in normalized["sides"]]
        if not sampling:
            for record in records:
                sampling, sampling_source = sampling_hit(record)
                if sampling:
                    break
        veto_locked = human_veto_locked(pair) or any(human_veto_locked(record) for record in records)
        prior_decision = _dict(_dict(pair.get("alignment")).get("whole")).get("decision")

        audio_route = orientation["kind"] in AUDIO_KINDS or bool(upstream.get("audio"))
        video_route = orientation["kind"] in VIDEO_KINDS and not audio_route
        needs_media = bool(event and event.get("new_description"))
        if audio_route:
            # CLIP cannot rescore CER. A changed description needs a fresh #13
            # evaluation; the previous metric never confirms the new text.
            media_relative, media_target = None, None
            media_error = "audio_route_rescore_required" if needs_media else None
        elif video_route:
            # 视文路线：常规分档只用 #14 上游分，不读视频/帧文件；只有人工改写描述
            # 需要重评分时才取代表帧（中间帧）。取不到就显式延后，绝不编造路径/分数。
            media_relative, media_target, media_error = None, None, None
            if needs_media:
                frame_reference = representative_frame_reference(orientation["video_record"])
                media_relative, media_target, media_error = _media_target(
                    media_root, frame_reference)
                if frame_reference is None:
                    media_error = REASON_NO_REPRESENTATIVE_FRAME
                if media_error is not None:
                    # 帧缺失/不可读时绝不把路径交给 CLIP（否则替身/后端可能产出假分）
                    media_target = None
                    stats["media_missing"] += 1
        else:
            reference = image_reference(orientation["image_record"])
            media_relative, media_target, media_error = _media_target(media_root, reference)
            if media_error is not None and needs_media:
                stats["structural_rejects"] += 1
                rejects.append(_origin_reject(lineno, pair.get("sample_id"), media_error, now,
                                              batch_id=batch_id, raw=pair))
                continue
            if media_error is not None:
                stats["media_missing"] += 1
        stats["applicable"] += 1

        review_task_id = "16-%s-%s" % (unique_id, pair_id)
        outcome = decide(
            pair_id, cfg=validated, upstream=upstream, sampling=sampling,
            sampling_source=sampling_source, event=event, veto_locked=veto_locked,
            encoder=encoder, image_target=media_target, prior_decision=prior_decision,
            review_task_id=review_task_id, now=now, batch_id=batch_id,
            retry_count_value=retries,
            rescore_unavailable_reason=media_error if (video_route or audio_route) else None)

        output = _build_output(pair, outcome, lineno, now,
                               _image_reference_of(normalized, orientation, media_relative))
        if video_route and media_relative:
            output["video_frame_reference"] = {"path": media_relative,
                                                "side": "left" if orientation["video_side"] == 0 else "right"}
        decision = outcome["decision"]
        if decision in (DECISION_AUTO_ALIGNED, DECISION_MANUAL_ALIGNED):
            stats["auto_aligned" if decision == DECISION_AUTO_ALIGNED
                  else "manual_aligned"] += 1
            main.append(output)
        elif decision in (DECISION_PENDING_MANUAL_REVIEW, DECISION_UNCERTAIN):
            stats[decision] += 1
            main.append(output)
            review.append(make_ledger(
                lineno, pair_id, pair.get("sample_id"), outcome["reason"],
                outcome["label"], outcome["action"], outcome["destination"], now,
                batch_id=batch_id, retry_count_value=retries,
                evidence=_ledger_evidence(outcome),
                record=output, extra={"entry_kind": "human_queue",
                                      "review_task_id": outcome["review_task_id"]}))
        else:
            stats["not_aligned"] += 1
            isolation.append(make_ledger(
                lineno, pair_id, pair.get("sample_id"), outcome["reason"],
                outcome["label"], outcome["action"], outcome["destination"], now,
                batch_id=batch_id, retry_count_value=retries,
                evidence=_ledger_evidence(outcome),
                record=output, extra={"entry_kind": "sample_isolation"}))

        if event is not None:
            stats["human_events_applied"] += 1
            audit.append(_audit_entry(event, pair_id, outcome, now))
        stats["rescored"] += 1 if outcome["rescored"] else 0
        stats["rescore_requested"] += 1 if outcome.get("description_edit") else 0
        if outcome.get("description_edit") and not outcome["rescored"]:
            stats["rescore_deferred"] += 1
        stats["sampling_hits"] += 1 if outcome["sampling_hit"] else 0
        stats["veto_locked"] += 1 if outcome["veto_locked"] else 0
        stats["_upstream_degraded"] = bool(stats.get("_upstream_degraded") or upstream.get("degraded"))

    # 未匹配到任何记录的事件 → 审计异常，不静默丢弃
    unmatched_events = 0
    for key, event in events.by_key.items():
        if key in matched_event_keys:
            continue
        unmatched_events += 1
        audit.append(_reject_event(event["line"], "human_event_target_not_found",
                                  {"pair_id": event["key"], "decision": event["decision"],
                                   "reviewer_id": event["reviewer_id"]}))

    stats["human_events_rejected"] = len(events.rejected) + unmatched_events
    stats["human_events_unmatched"] = unmatched_events
    stats["review_entries"] = len(review)
    stats["audit_entries"] = len(audit)
    stats["reject_entries"] = len(rejects)
    stats["output"] = len(main)
    stats["rejected"] = len(rejects)
    stats["valid"] = stats["input"] - stats["bad_lines"] - stats["structural_rejects"]
    stats["degraded"] = bool(stats["rescore_deferred"] or stats.get("_upstream_degraded"))
    stats["degradation_reason"] = (model_reason or "upstream_or_rescore_degraded") if stats["degraded"] else None
    stats["empty_input"] = stats["input"] == 0
    stats["thresholds"] = {"isolation": low, "auto_align": high}
    _check_conservation(stats, main, review, isolation, not_applicable, rejects)

    result = AlignmentResult(main, review, isolation, not_applicable, rejects, audit, stats)
    result.stats["_media_refs"] = _media_references(main, isolation, review)
    return result


def _media_references(main: Sequence[dict], isolation: Sequence[dict],
                      review: Sequence[dict]) -> List[str]:
    """Collect original image/audio/video and all frame references safely."""
    found = set()

    def collect(value):
        reference = _frame_reference(value)
        if reference is not None:
            try:
                found.add(op_io.safe_media_path(reference))
            except ValueError:
                pass
        if isinstance(value, dict):
            for key in ("frames", "keyframes", "segments"):
                entries = value.get(key)
                if isinstance(entries, list):
                    for entry in entries:
                        collect(entry)
        elif isinstance(value, list):
            for entry in value:
                collect(entry)

    for record in list(main) + [item.get("record") for item in isolation] \
            + [item.get("record") for item in review]:
        record = _dict(record)
        holders = [record] + [_side_record(record.get(side)) for side in ("left", "right")]
        for holder in holders:
            for key in ("image", "image_path", "image_reference", "video", "video_path",
                        "video_reference", "video_frame_reference", "audio", "audio_path", "audio_reference"):
                collect(holder.get(key))
    return sorted(found)


def _ledger_evidence(outcome: dict) -> dict:
    return {
        "decision": outcome["decision"],
        "clip_score": outcome["clip_score"],
        "score_metric": outcome["score_metric"],
        "score_value": outcome["score_value"],
        "score_direction": outcome["score_direction"],
        "score_threshold": outcome["score_threshold"],
        "score_field": outcome["score_field"],
        "score_source": outcome["score_source"],
        "confirmation_method": outcome["confirmation_method"],
        "human_verified": outcome["human_verified"],
        "rescored": outcome["rescored"],
        "thresholds": outcome["thresholds"],
        "stage": "样本业务状态",
    }


def _audit_entry(event: dict, pair_id: str, outcome: dict, now: str) -> dict:
    return {
        "kind": "human_event",
        "line": event["line"],
        "key": event["key"],
        "key_kind": event["key_kind"],
        "pair_id": pair_id,
        "decision": event["decision"],
        "reviewer_id": event["reviewer_id"],
        "reviewed_at": event["reviewed_at"],
        "simulated_human": event.get("simulated_human", False),
        "review_task_id": event["review_task_id"],
        "previous_decision": outcome["previous_decision"],
        "applied_decision": outcome["decision"],
        "new_description_ref": (outcome["description_edit"] or {}).get("new_description_ref"),
        "rescored": outcome["rescored"],
        "rescore_status": outcome["rescore_status"],
        "label": outcome["label"],
        "action": outcome["action"],
        "destination": outcome["destination"],
        "event_time": now,
    }


def _image_reference_of(normalized: Dict[str, Any], orientation: Dict[str, Any],
                        media_relative: Optional[str]) -> Optional[dict]:
    """本次实际使用的媒体侧引用（相对路径 + 侧别 + sample_id），供下游可追溯。

    图文路线取图像侧；视文路线只在"人工改描述重评分"时取代表帧，侧别取视频侧。
    """
    if media_relative is None:
        return None
    if orientation.get("kind") in VIDEO_KINDS + AUDIO_KINDS:
        return None
    index = orientation.get("image_side")
    if index is None:
        index = orientation.get("video_side")
    sides = normalized["sides"]
    side_name = {0: "left", 1: "right"}.get(index, "left")
    sample_id = None
    if isinstance(index, int) and 0 <= index < len(sides):
        value = sides[index].get("sample_id")
        if isinstance(value, str):
            sample_id = value
    if sample_id is None:
        sample_id = normalized["pair"].get("sample_id")
    return {"side": side_name, "sample_id": sample_id, "path": media_relative}


def _build_output(pair: dict, outcome: dict, lineno: int, now: str,
                  image_reference_value: Optional[dict] = None) -> dict:
    """把结论写回记录：只追加 #16 的命名空间，不覆盖上游结论。"""
    output = copy.deepcopy(pair)
    whole = {
        "decision": outcome["decision"],
        "clip_score": outcome["clip_score"],
        "score_metric": outcome["score_metric"],
        "score_value": outcome["score_value"],
        "score_direction": outcome["score_direction"],
        "score_threshold": outcome["score_threshold"],
        "score_field": outcome["score_field"],
        "score_source": outcome["score_source"],
        "threshold_review": outcome["thresholds"]["review"],
        "threshold_auto": outcome["thresholds"]["auto"],
        #: [2026-11-05] 阈值来源：upstream（跟随上游标定线）/ config（本算子配置）
        "threshold_source": outcome["threshold_source"],
        "confirmation_method": outcome["confirmation_method"],
        "human_verified": outcome["human_verified"],
        "rescored": outcome["rescored"],
        "rescore_status": outcome["rescore_status"],
        "model": outcome["model"],
        "model_version": outcome["model_version"],
        "review_task_id": outcome["review_task_id"],
        "reviewer_id": outcome["reviewer_id"],
        "reviewed_at": outcome["reviewed_at"],
        "previous_decision": outcome["previous_decision"],
        "comment": outcome["comment"],
        "human_veto_locked": outcome["veto_locked"],
        "sampling_hit": outcome["sampling_hit"],
        #: 表1 #16 算法 4) 追溯：档位由新重评分给出还是沿用上游分
        "bucketed_by": outcome["bucketed_by"],
        "human_rewritten": outcome["human_rewritten"],
        "human_decision": outcome["human_decision"],
        #: 表1 #16 算法 3) 追溯：低于 0.25 时沿用 #12 的隔离记录
        "isolation_source": outcome["isolation_source"],
        "evidence": {
            "score_source": outcome["score_source"],
            "upstream": {
                "origin": outcome["upstream"].get("origin"),
                "source": outcome["upstream"].get("source"),
                "tier": outcome["upstream"].get("tier"),
                "isolated": bool(outcome["upstream"].get("isolated")),
                "reason": outcome["upstream"].get("reason"),
                "score_field": outcome["upstream"].get("score_field"),
                "usable": outcome["upstream"].get("usable"),
            },
            #: [2026-11-05] 时序路线：#15 的时间轴结论（无 CLIP 分时由它给出档位）
            "temporal": outcome["upstream"].get("temporal"),
            "sampling": {"hit": outcome["sampling_hit"], "source": outcome["sampling_source"]},
            "human_event": None if outcome["event"] is None else {
                "key": outcome["event"]["key"], "key_kind": outcome["event"]["key_kind"],
                "decision": outcome["event"]["decision"],
                "reviewer_id": outcome["event"]["reviewer_id"],
                "reviewed_at": outcome["event"]["reviewed_at"],
                "simulated_human": outcome["event"].get("simulated_human", False),
                "line": outcome["event"]["line"],
                "inline": outcome["event"]["line"] < 0,
            },
            "rescoring": None if outcome["rescore_status"] is None else {
                # [v4.2] status 双字段：int status + 原字符串迁入 process_status
                "status": 1 if outcome["rescore_status"] == "ok" else 0,
                "process_status": outcome["rescore_status"],
                "score_before": (outcome["description_edit"] or {}).get("score_before"),
                "score_after": (outcome["description_edit"] or {}).get("score_after"),
                "model_version": (outcome["description_edit"] or {}).get("model_version"),
            },
            "label": outcome["label"],
            "action": outcome["action"],
            "destination": outcome["destination"],
            "reason": outcome["reason"],
            "notes": outcome["notes"],
        },
    }
    if outcome["event"] is not None:
        whole["simulated_human"] = outcome["event"].get("simulated_human", False)
    if outcome["description_edit"] is not None:
        whole["description_edit"] = outcome["description_edit"]
        new_description = outcome["event"]["new_description"] if outcome["event"] else None
        if new_description:
            metadata = output.get("metadata")
            if not isinstance(metadata, dict):
                metadata = {}
                output["metadata"] = metadata
            metadata["human_description"] = {
                "content": new_description,
                "reviewer_id": outcome["reviewer_id"],
                "reviewed_at": outcome["reviewed_at"],
                "simulated_human": outcome["event"].get("simulated_human", False),
                "sha256": outcome["description_edit"]["new_description_ref"]["sha256"],
            }
    alignment = output.get("alignment")
    if not isinstance(alignment, dict):
        alignment = {}
        output["alignment"] = alignment
    alignment["whole"] = whole

    processing = output.get("processing")
    if not isinstance(processing, dict):
        processing = {}
        output["processing"] = processing
    processing["16"] = {
        # [v4.2] status 双字段：本算子为处理型（1=处理成功/0=处理失败），
        # 原字符串结论（auto_aligned/pending_manual_review/...）迁入 process_status
        "status": 1,
        "process_status": outcome["decision"],
        "reason": outcome["reason"],
        "operator": OPERATOR,
        "operator_version": VERSION,
        "label": outcome["label"],
        "action": outcome["action"],
        "destination": outcome["destination"],
        "event_time": now,
        "batch_id": outcome["batch_id"],
        "retry_count": outcome["retry_count"],
        "clip_score": outcome["clip_score"],
        "score_metric": outcome["score_metric"],
        "score_value": outcome["score_value"],
        "score_direction": outcome["score_direction"],
        "score_threshold": outcome["score_threshold"],
        "score_field": outcome["score_field"],
        "score_source": outcome["score_source"],
        #: [2026-11-05] 档位来源：rescore / upstream_score / temporal_15 / none
        "bucketed_by": outcome["bucketed_by"],
        "threshold_review": outcome["thresholds"]["review"],
        "threshold_auto": outcome["thresholds"]["auto"],
        "threshold_source": outcome["threshold_source"],
        #: [2026-11-05] 时序路线引用的 #15 结论（非时序样本为 null）
        "temporal": outcome["upstream"].get("temporal"),
        "human_verified": outcome["human_verified"],
        "rescored": outcome["rescored"],
        "line": lineno,
    }
    if image_reference_value:
        output["image_reference"] = image_reference_value
    return output


def _check_conservation(stats: dict, main: Sequence[dict], review: Sequence[dict],
                        isolation: Sequence[dict], not_applicable: Sequence[dict],
                        rejects: Sequence[dict]) -> None:
    """逐条校验守恒式；任一条不成立即 conservation_ok=false 并列出失败项。"""
    failures: List[str] = []
    checks = [
        ("input == main + isolation + not_applicable + rejects",
         stats["input"] == len(main) + len(isolation) + len(not_applicable) + len(rejects)),
        ("input == valid + bad_lines + structural_rejects",
         stats["input"] == stats["valid"] + stats["bad_lines"] + stats["structural_rejects"]),
        ("valid == applicable + not_applicable",
         stats["valid"] == stats["applicable"] + stats["not_applicable"]),
        ("applicable == auto + manual + pending + uncertain + not_aligned",
         stats["applicable"] == stats["auto_aligned"] + stats["manual_aligned"]
         + stats["pending_manual_review"] + stats["uncertain"] + stats["not_aligned"]),
        ("main == auto + manual + pending + uncertain",
         len(main) == stats["auto_aligned"] + stats["manual_aligned"]
         + stats["pending_manual_review"] + stats["uncertain"]),
        ("isolation == not_aligned", len(isolation) == stats["not_aligned"]),
        ("review == pending + uncertain",
         len(review) == stats["pending_manual_review"] + stats["uncertain"]),
        ("reject_entries == bad_lines + structural_rejects",
         stats["reject_entries"] == stats["bad_lines"] + stats["structural_rejects"]),
    ]
    for name, ok in checks:
        if not ok:
            failures.append(name)
    stats["conservation_ok"] = not failures
    stats["conservation_failures"] = failures
