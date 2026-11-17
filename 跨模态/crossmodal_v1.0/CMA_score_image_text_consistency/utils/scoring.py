"""算子 #12 评分图文语义一致性 业务核心（表1 v4.1 第 12 行冻结口径）。

冻结口径（跨模态算子开发背景与规则 §4 #12 / 可行性分析 #12 / 数据流说明 §2 #12）：
- 输入：上游 #11 的图文候选配对（``11_candidate_pairs.jsonl``）；
- 处理：用 CLIP-ViT/B-32 计算**真实余弦**；``<隔离线``（默认 0.20）隔离、``隔离线–0.32`` 留样本池并进人工队列、
  ``>=0.32`` 自动对齐；中间分可选 YOLOv8 辅助但**不改变分档**；
- 输出：分数、分档、模型版本、质量标签；评分结果交 #16/#19，隔离记录交 #21；
- 模型不可用时只能显式 ``degraded/pending``，**绝不**用固定/随机/规则分数冒充模型分。

本模块不 import 任何模型库：模型通过 ``encoder`` 参数注入（``utils.backends`` 提供真实实现）。
"""
from __future__ import annotations

import copy
import math
from typing import Any, Dict, Iterable, List, Optional, Sequence, Tuple
from .zip_flow import build_flow_records

# ------------------------------------------------------------------ 身份常量

OPERATOR = "image_text_semantic_scoring"
OPERATOR_LABEL = "#12"
VERSION = "v1.0.0"
STAGE_ID = "S3"
STAGE_NAME = "跨模态数据对齐标注（表1 数据加工）"
FORMAL_OPERATOR_NUMBER = 12

MAIN_NAME = "12_image_text_scores.jsonl"
FLOW_NAME = "12_flow_records.jsonl"
ISOLATION_NAME = "12_isolation.jsonl"
NOT_APPLICABLE_NAME = "12_not_applicable.jsonl"
PENDING_MODEL_NAME = "12_pending_model.jsonl"
REJECTS_NAME = "12_rejects.jsonl"

# ------------------------------------------------------------------ 分档口径

TIER_ISOLATED = "isolated"
TIER_PENDING_MANUAL = "pending_manual"
TIER_AUTO_ALIGNED = "auto_aligned"

TIER_LABELS = {
    TIER_ISOLATED: "样本-语义不通",
    TIER_PENDING_MANUAL: "样本-评分：待人工",
    TIER_AUTO_ALIGNED: "样本-评分：通过",
}
TIER_ACTIONS = {
    TIER_ISOLATED: "isolate",
    TIER_PENDING_MANUAL: "queue_manual_review",
    TIER_AUTO_ALIGNED: "auto_align",
}
TIER_DESTINATIONS = {
    TIER_ISOLATED: "样本隔离库",
    TIER_PENDING_MANUAL: "人工队列",
    TIER_AUTO_ALIGNED: "样本池",
}

#: 表1 质量处置标签（流程状态与业务隔离必须分开表达）
LABEL_ORIGIN_BROKEN = "原始文件-损坏"
LABEL_ORIGIN_UNPARSABLE = "原始文件-无法解析"
LABEL_ORIGIN_ENCODING = "原始文件-编码异常"
LABEL_ORIGIN_MISSING_FIELD = "原始文件-缺字段"
LABEL_FLOW_ROUTE_CLOSED = "流程-路线关闭"
LABEL_FLOW_NOT_APPLICABLE = "流程-不适用图文评分"
LABEL_FLOW_PENDING_MODEL = "流程-待模型评分"

DEST_ORIGIN = "原始隔离区"
DEST_SAMPLE = "样本隔离库"
DEST_MANUAL_QUEUE = "人工队列"
DEST_SAMPLE_POOL = "样本池"
DEST_PIPELINE_WAIT = "留产线（样本池）"

#: 状态枚举
STATUS_SCORED = "scored"
STATUS_PENDING_MODEL = "pending_model"
STATUS_NOT_APPLICABLE = "not_applicable"
STATUS_REJECTED = "rejected"

# ------------------------------------------------------------------ 配置口径

# [round10 标定] 默认 0.25→0.22：round9 同源 140 对最低分 0.2263，0.22 时正例零误
# 隔离；错配负例 88.2% 隔离、0 条达到自动对齐（见 crossmodal_test5/阈值标定.md）。
# [2026-11-05 全量标定] 0.22→0.20：800 条图文正样本误隔离 6→1 条，负样本隔离率 80%→74%，
# 负样本 0/50 达到 0.32 自动档（低分线只决定自动隔离/转人工，故取定义域下限）。
DEFAULT_ISOLATION_THRESHOLD = 0.20
DEFAULT_AUTO_ALIGN_THRESHOLD = 0.32
ISOLATION_RANGE = (0.20, 0.40)
AUTO_ALIGN_RANGE = (0.25, 0.50)
DEFAULT_BATCH_SIZE = 8

#: 请求 config 只允许这些业务参数（模型/设备/路径/身份不可被请求改写）
# [v4.2] 增补 judge_mode（V4.2 §4.12 接口二参数表）：operator/system，默认 operator
ALLOWED_OVERRIDES = {"enabled", "isolation_threshold", "auto_align_threshold",
                     "use_detector_for_borderline", "judge_mode"}

# [v4.2] judge_mode 枚举（V4.2 §2.8）：operator=算子按快照阈值给分档；system=只回指标由平台判定
JUDGE_MODES = ("operator", "system")
DEFAULT_JUDGE_MODE = "operator"

SUPPORTED_LIBRARIES = ("auto", "open_clip", "clip", "transformers", "none")
SUPPORTED_DEVICES = ("auto", "cpu", "cuda")

DEFAULT_MODEL_SECTION: Dict[str, Any] = {
    "backend": "clip_vit_b32",
    "library": "auto",
    "model_name": "ViT-B-32",
    "weights": None,
    "device": "auto",
    "batch_size": DEFAULT_BATCH_SIZE,
    "allow_degraded": True,
}
DEFAULT_DETECTOR_SECTION: Dict[str, Any] = {
    "weights": None,
    "device": "auto",
    "confidence": 0.25,
}

REASON_MODEL_UNAVAILABLE = "model_unavailable"


class ConfigError(ValueError):
    """配置结构、定义域或枚举非法。"""


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
    """校验并规范化配置；返回深拷贝，不修改入参。"""
    if not isinstance(cfg, dict):
        raise ConfigError("配置必须是对象")

    raw_section = cfg.get("image_text_scoring")
    if raw_section is None:
        raw_section = {}
    if not isinstance(raw_section, dict):
        raise ConfigError("image_text_scoring 必须是对象")

    enabled = _bool(raw_section.get("enabled", True), "image_text_scoring.enabled")
    low = _number(raw_section.get("isolation_threshold", DEFAULT_ISOLATION_THRESHOLD),
                  "image_text_scoring.isolation_threshold")
    high = _number(raw_section.get("auto_align_threshold", DEFAULT_AUTO_ALIGN_THRESHOLD),
                   "image_text_scoring.auto_align_threshold")
    use_detector = _bool(raw_section.get("use_detector_for_borderline", False),
                         "image_text_scoring.use_detector_for_borderline")
    # [v4.2] judge_mode（V4.2 §4.12）：operator/system，默认 operator
    judge_mode = str(raw_section.get("judge_mode", DEFAULT_JUDGE_MODE)).strip().lower()
    if judge_mode not in JUDGE_MODES:
        raise ConfigError("judge_mode 非法：%s（可选 operator/system）" % judge_mode)

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
    model_name = raw_model.get("model_name") or DEFAULT_MODEL_SECTION["model_name"]
    if not isinstance(model_name, str) or not model_name.strip():
        raise ConfigError("model.model_name 不能为空")
    weights = raw_model.get("weights")
    if weights is not None and (not isinstance(weights, str) or not weights.strip()):
        raise ConfigError("model.weights 必须是本地路径字符串或 null")
    batch_size = _positive_int(raw_model.get("batch_size", DEFAULT_BATCH_SIZE),
                               "model.batch_size")
    allow_degraded = _bool(raw_model.get("allow_degraded", True), "model.allow_degraded")

    raw_detector = cfg.get("detector") or {}
    if not isinstance(raw_detector, dict):
        raise ConfigError("detector 必须是对象")
    detector_weights = raw_detector.get("weights")
    if detector_weights is not None and (not isinstance(detector_weights, str)
                                        or not detector_weights.strip()):
        raise ConfigError("detector.weights 必须是本地路径字符串或 null")
    detector_device = str(raw_detector.get("device") or "auto").strip().lower()
    if detector_device not in SUPPORTED_DEVICES:
        raise ConfigError("detector.device 非法：%s" % detector_device)
    detector_confidence = _number(raw_detector.get("confidence", 0.25),
                                  "detector.confidence")
    if not (0.0 < detector_confidence <= 1.0):
        raise ConfigError("detector.confidence 必须在 (0, 1] 区间")

    normalized = copy.deepcopy(cfg)
    normalized["operator"] = OPERATOR
    normalized["operator_version"] = VERSION
    normalized["stage_id"] = STAGE_ID
    normalized["image_text_scoring"] = {
        "enabled": enabled,
        "isolation_threshold": low,
        "auto_align_threshold": high,
        "use_detector_for_borderline": use_detector,
        "judge_mode": judge_mode,
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
    normalized["detector"] = {
        "weights": detector_weights.strip() if isinstance(detector_weights, str) else None,
        "device": detector_device,
        "confidence": detector_confidence,
    }
    return normalized


def apply_overrides(cfg: dict, overrides: Optional[dict]) -> dict:
    """把请求级 config 合并到配置；只允许业务参数，身份/模型/路径项不可被改写。"""
    merged = copy.deepcopy(cfg)
    if overrides is None:
        return merged
    if not isinstance(overrides, dict):
        raise ConfigError("config 必须是对象")
    unknown = sorted(set(overrides) - ALLOWED_OVERRIDES)
    if unknown:
        raise ConfigError("不支持的配置项：%s" % unknown[0])
    section = merged.get("image_text_scoring")
    if not isinstance(section, dict):
        section = {}
        merged["image_text_scoring"] = section
    for key, value in overrides.items():
        section[key] = value
    return merged


# ------------------------------------------------------------------ 数学与分档

def cosine_similarity(left: Sequence[float], right: Sequence[float]) -> float:
    """真实余弦相似度；向量必须等长，任一为零向量时返回 0.0（可解释而非报错）。"""
    left_values = [float(item) for item in left]
    right_values = [float(item) for item in right]
    if len(left_values) != len(right_values):
        raise ValueError("向量维度不一致：%d vs %d" % (len(left_values), len(right_values)))
    dot = math.fsum(a * b for a, b in zip(left_values, right_values))
    left_norm = math.sqrt(math.fsum(value * value for value in left_values))
    right_norm = math.sqrt(math.fsum(value * value for value in right_values))
    if left_norm == 0.0 or right_norm == 0.0:
        return 0.0
    return max(-1.0, min(1.0, dot / (left_norm * right_norm)))


def bucket_tier(score: float, isolation_threshold: float,
                auto_align_threshold: float) -> str:
    """表1 固定分档：``<隔离阈值`` 隔离；``[隔离, 自动)`` 人工；``>=自动`` 自动对齐。"""
    if score < isolation_threshold:
        return TIER_ISOLATED
    if score < auto_align_threshold:
        return TIER_PENDING_MANUAL
    return TIER_AUTO_ALIGNED


# ------------------------------------------------------------------ 字段抽取

_PATH_KEYS = ("path", "file", "file_path", "uri", "relative_path")
_IMAGE_KEYS = ("image", "images", "image_reference", "image_path")


def extract_description(record: dict) -> Dict[str, Any]:
    """按 #9 的输出约定取出候选描述与来源（与 #10 的抽取顺序保持一致）。"""
    source: Optional[str] = None
    value: Any = None
    text = record.get("text")
    if isinstance(text, dict):
        value = text.get("content")
        source = text.get("description_source")
    elif isinstance(text, str):
        value = text

    metadata = record.get("metadata") if isinstance(record.get("metadata"), dict) else {}
    if not (isinstance(value, str) and value.strip()):
        candidate = metadata.get("description")
        if isinstance(candidate, str) and candidate.strip():
            value = candidate
            source = source if source is not None else metadata.get("description_source")

    if source is None:
        processing = record.get("processing") if isinstance(record.get("processing"), dict) else {}
        node = processing.get("09") if isinstance(processing.get("09"), dict) else {}
        source = node.get("description_source") or node.get("source")
    if source is None:
        source = metadata.get("description_source")

    return {"value": value.strip() if isinstance(value, str) else "",
            "source": source if isinstance(source, str) else None}


def image_reference(record: dict) -> Optional[str]:
    """Resolve native image references without rewriting the business record."""
    def reference(value):
        if isinstance(value, str) and value.strip():
            return value.strip()
        if isinstance(value, list):
            for child in value:
                found = reference(child)
                if found:
                    return found
        if isinstance(value, dict):
            for key in _PATH_KEYS + _IMAGE_KEYS:
                found = reference(value.get(key))
                if found:
                    return found
        return None

    for container in (record, record.get('media')):
        if isinstance(container, dict):
            for key in _IMAGE_KEYS:
                found = reference(container.get(key))
                if found:
                    return found
    return None


def _declares_image(record: dict) -> bool:
    """A declared but unresolved image cannot be waived as absent modality."""
    for container in (record, record.get('media')):
        if not isinstance(container, dict):
            continue
        if any(key in container and container[key] is not None for key in _IMAGE_KEYS):
            return True
    for key in ('modality', 'modalities', 'modality_pair'):
        values = record.get(key)
        values = values if isinstance(values, list) else [values]
        if any(isinstance(value, str) and value.strip().lower() in ('image', 'images')
               for value in values):
            return True
    return False


def safe_media_path(value: object) -> str:
    """媒体路径必须是包内相对路径：拒绝 URL、UNC、绝对路径与目录穿越。"""
    if not isinstance(value, str) or not value.strip():
        raise ValueError("媒体路径不能为空")
    raw = value.strip().replace("\\", "/")
    if raw.lower().startswith(("http://", "https://", "file://", "ftp://", "//")):
        raise ValueError("媒体路径不支持 URL/UNC：%s" % value)
    while raw.startswith("./"):
        raw = raw[2:]
    if raw.startswith("/") or ":" in raw:
        raise ValueError("媒体路径必须是相对路径：%s" % value)
    parts = [part for part in raw.split("/") if part not in ("", ".")]
    if not parts or any(part == ".." for part in parts):
        raise ValueError("媒体路径不允许目录穿越：%s" % value)
    return "/".join(parts)


def tokenize_keywords(text: Optional[str]) -> List[str]:
    """确定性关键词切分（jieba 可用时用 jieba，否则内置规则）；不加载模型。"""
    if not isinstance(text, str) or not text.strip():
        return []
    lowered = text.strip().lower()
    try:
        import jieba

        tokens = [word.strip() for word in jieba.lcut(lowered)
                  if len(word.strip()) >= 2]
        if tokens:
            return sorted(set(tokens))
    except Exception:  # noqa: BLE001 - 依赖缺失时退回内置切分
        pass
    import re

    tokens = [word for word in re.findall(r"[0-9a-z]{3,}", lowered)]
    for run in re.findall(r"[\u3400-\u4dbf\u4e00-\u9fff]+", lowered):
        if 2 <= len(run) <= 8:
            tokens.append(run)
        for index in range(len(run) - 1):
            tokens.append(run[index:index + 2])
    return sorted(set(tokens))


# --------------------------------------------------------------- 配对取向选择

ORIENTATIONS = ("left_text_right_image", "right_text_left_image",
                "left_internal_text_image", "right_internal_text_image")


def _side_summary(side: object) -> Dict[str, Any]:
    if not isinstance(side, dict):
        return {"sample_id": None, "record": {}}
    record = side.get("record") if isinstance(side.get("record"), dict) else {}
    sample_id = side.get("sample_id")
    if not isinstance(sample_id, str) or not sample_id.strip():
        sample_id = record.get("sample_id")
    return {"sample_id": sample_id if isinstance(sample_id, str) else None, "record": record}


def select_orientation(pair: dict) -> Dict[str, Any]:
    """为一条候选对选择图文评分取向。

    优先级：跨记录（左文右图 → 右文左图）→ 同记录内部（左 → 右）。
    没有任何图文组合时返回 ``{"ok": False, "reason": ...}``，由上层按"流程-不适用"处理，
    **不进入隔离区**。
    """
    left = _side_summary(pair.get("left"))
    right = _side_summary(pair.get("right"))
    sides = {
        "left": {"sample_id": left["sample_id"],
                 "description": extract_description(left["record"]),
                 "image": image_reference(left["record"])},
        "right": {"sample_id": right["sample_id"],
                  "description": extract_description(right["record"]),
                  "image": image_reference(right["record"])},
    }

    def candidate(orientation: str, text_side: str, image_side: str):
        text_side_info = sides[text_side]
        image_side_info = sides[image_side]
        if not text_side_info["description"]["value"] or not image_side_info["image"]:
            return None
        return {
            "ok": True,
            "orientation": orientation,
            "text_side": text_side,
            "image_side": image_side,
            "text": text_side_info["description"]["value"],
            "description_source": text_side_info["description"]["source"],
            "text_sample_id": text_side_info["sample_id"],
            "image_sample_id": image_side_info["sample_id"],
            "image_path": image_side_info["image"],
        }

    available = [
        candidate("left_text_right_image", "left", "right"),
        candidate("right_text_left_image", "right", "left"),
        candidate("left_internal_text_image", "left", "left"),
        candidate("right_internal_text_image", "right", "right"),
    ]
    alternatives = [item["orientation"] for item in available if item is not None]
    chosen = next((item for item in available if item is not None), None)
    if chosen is not None:
        chosen["alternatives"] = alternatives
        return chosen

    has_image = any(info["image"] for info in sides.values())
    declared_image = any(_declares_image(value) for value in
                         (pair, left['record'], right['record'],
                          pair.get('left') or {}, pair.get('right') or {})
                         if isinstance(value, dict))
    has_text = any(info["description"]["value"] for info in sides.values())
    if not has_image and declared_image:
        reason = "image_reference_unresolved"
    elif not has_image and not has_text:
        reason = "no_image_text"
    elif not has_image:
        reason = "no_image"
    else:
        reason = "no_description"
    return {"ok": False, "reason": reason, "alternatives": []}


# ------------------------------------------------------------------ 结果容器

class ScoringResult(object):
    """一次评分运行的产物与真实统计。"""

    __slots__ = ("main", "isolation", "not_applicable", "pending_model", "rejects", "stats", "pipeline")

    def __init__(self, main: List[dict], isolation: List[dict],
                 not_applicable: List[dict], pending_model: List[dict],
                 rejects: List[dict], stats: Dict[str, Any], pipeline=None) -> None:
        self.main = main
        self.isolation = isolation
        self.not_applicable = not_applicable
        self.pending_model = pending_model
        self.rejects = rejects
        self.stats = stats
        self.pipeline = list(main) if pipeline is None else pipeline


def empty_stats() -> Dict[str, Any]:
    return {
        "input": 0, "pairs_total": 0, "bad_lines": 0, "structural_rejects": 0,
        "image_text_pairs": 0, "not_applicable_pairs": 0, "media_rejected": 0,
        "scored": 0, "auto_aligned": 0, "pending_manual": 0, "isolated": 0,
        "pending_model": 0, "route_enabled": True, "degraded": False,
        "runtime_degraded": False, "degradation_reason": None,
        "detector_applied": 0, "detector_unavailable": 0, "conservation_ok": False,
    }


def _retry_count(record: dict) -> int:
    candidates: List[Any] = []
    metadata = record.get("metadata") if isinstance(record.get("metadata"), dict) else {}
    candidates.append(metadata.get("retry_count"))
    candidates.append(record.get("retry_count"))
    processing = record.get("processing") if isinstance(record.get("processing"), dict) else {}
    for key in ("12", "11", "09"):
        node = processing.get(key)
        if isinstance(node, dict):
            candidates.append(node.get("retry_count"))
    for value in candidates:
        if isinstance(value, int) and not isinstance(value, bool) and value >= 0:
            return value
    return 0


def make_ledger(line: int, sample_id: Optional[str], reason: str, label: str,
                action: str, destination: str, now: str,
                operator: str = OPERATOR_LABEL, batch_id: Optional[str] = None,
                retry_count: int = 0, evidence: Optional[dict] = None,
                record: Optional[dict] = None,
                extra: Optional[dict] = None) -> Dict[str, Any]:
    """统一质量台账条目（表1 §2.3 字段）。"""
    item: Dict[str, Any] = {
        "line": line,
        "pair_id": (record or {}).get("pair_id") if isinstance(record, dict) else None,
        "sample_id": sample_id,
        "reason": reason,
        "label": label,
        "stage": "数据加工",
        "operator": operator,
        "action": action,
        "destination": destination,
        "event_time": now,
        "batch_id": batch_id,
        "retry_count": retry_count,
        "evidence": evidence or {},
    }
    if record is not None:
        item["record"] = record
    if extra:
        item.update(extra)
    return item


#: 原始文件类问题（进原始隔离区）：原因 → 表1 标签
_ORIGIN_LEDGER = {
    "encoding_error": LABEL_ORIGIN_ENCODING,
    "json_parse_error": LABEL_ORIGIN_UNPARSABLE,
    "record_not_object": LABEL_ORIGIN_UNPARSABLE,
    "missing_field: pair_id": LABEL_ORIGIN_MISSING_FIELD,
    "duplicate_pair_id": LABEL_ORIGIN_MISSING_FIELD,
    "media_missing": LABEL_ORIGIN_BROKEN,
    "media_unreadable": LABEL_ORIGIN_BROKEN,
    "media_path_invalid": LABEL_ORIGIN_UNPARSABLE,
}


def _planner_batch_size(cfg: dict) -> int:
    section = cfg.get("model") if isinstance(cfg.get("model"), dict) else {}
    try:
        return max(1, int(section.get("batch_size") or DEFAULT_BATCH_SIZE))
    except (TypeError, ValueError):
        return DEFAULT_BATCH_SIZE


def plan_scoring(lines: Iterable[Tuple[int, Optional[dict], Optional[str]]], cfg: dict,
                 *, unique_id: str, media_root, now: str,
                 encoder: Optional[Any] = None, detector: Optional[Any] = None,
                 detector_reason: Optional[str] = None,
                 degraded_reason: Optional[str] = None,
                 degraded_detail: Optional[str] = None) -> ScoringResult:
    """流式消费候选对，产出主清单、隔离清单、不适用清单、待模型清单与异常清单。

    ``encoder is None`` 表示没有可用的真实模型：全部图文对进入"待模型"清单，
    ``clip_score`` 保持 ``null``，**不产生任何分数**。
    """
    import os
    from pathlib import Path

    validated = validate_config(cfg)
    section = validated["image_text_scoring"]
    model_cfg = validated["model"]
    route_enabled = bool(section["enabled"])
    low = float(section["isolation_threshold"])
    high = float(section["auto_align_threshold"])
    use_detector = bool(section["use_detector_for_borderline"])
    batch_size = _planner_batch_size(validated)
    root = Path(media_root).resolve()

    stats = empty_stats()
    stats["route_enabled"] = route_enabled
    main: List[dict] = []
    isolation: List[dict] = []
    not_applicable: List[dict] = []
    pending_model: List[dict] = []
    rejects: List[dict] = []
    seen_pair_ids: Dict[str, int] = {}

    state = {"encoder": encoder,
             # 业务状态码（稳定，进入待模型记录）与技术细节（进入统计与排障）分开表达
             "degraded_reason": degraded_reason or REASON_MODEL_UNAVAILABLE,
             "degraded_detail": degraded_detail or degraded_reason or REASON_MODEL_UNAVAILABLE,
             "runtime_degraded": False, "buffer": [],
             "model_identity": encoder.model_identity() if encoder is not None else None}

    def model_identity() -> Optional[dict]:
        if state["model_identity"] is None and state["encoder"] is not None:
            state["model_identity"] = state["encoder"].model_identity()
        return state["model_identity"]

    def attach_common(record: dict, item: dict, status: str, tier: Optional[str],
                      label: str, action: str, destination: str,
                      event_time: str, status_int: int = 1) -> dict:
        # [v4.2] status 双字段（V4.2 §2.10）：输出记录的字符串 status 一律改为
        # int status（1 不过滤/0 过滤）+ process_status（原字符串流程态）。
        orientation = item["orientation"]
        batch_id = item.get("batch_id")
        retry = item.get("retry_count", 0)
        record["text_sample_id"] = orientation["text_sample_id"]
        record["image_sample_id"] = orientation["image_sample_id"]
        record["text_reference"] = {
            "side": orientation["text_side"],
            "sample_id": orientation["text_sample_id"],
            "chars": len(orientation["text"]),
            "description_source": orientation["description_source"],
        }
        record["image_reference"] = {
            "side": orientation["image_side"],
            "sample_id": orientation["image_sample_id"],
            "path": item["media_relative"],
        }
        processing = record.get("processing")
        if not isinstance(processing, dict):
            processing = {}
        processing["12"] = {"status": status_int, "process_status": status,
                            "tier": tier, "label": label,
                            "operator": OPERATOR_LABEL}
        record["processing"] = processing
        record["scoring"] = {
            "operator": OPERATOR_LABEL,
            "operator_version": VERSION,
            "stage": "数据加工",
            "status": status_int,
            "process_status": status,
            "tier": tier,
            "label": label,
            "action": action,
            "destination": destination,
            "event_time": event_time,
            "batch_id": batch_id,
            "retry_count": retry,
        }
        return record

    def detector_status(tier: Optional[str], item: dict) -> Dict[str, Any]:
        from utils import backends

        if not use_detector:
            return backends.detector_evidence("not_requested")
        if tier != TIER_PENDING_MANUAL:
            return backends.detector_evidence(
                "not_applicable", reason="not_borderline",
                keywords=tokenize_keywords(item["orientation"]["text"]))
        if detector is None:
            return backends.detector_evidence(
                "unavailable", reason=detector_reason or "detector_not_available",
                keywords=tokenize_keywords(item["orientation"]["text"]))
        try:
            found = detector.detect(item["target"])
        except Exception as exc:  # noqa: BLE001 - 检测失败只记录，不影响分档
            return backends.detector_evidence(
                "unavailable", reason="detector_runtime_failure: %s" % type(exc).__name__,
                keywords=tokenize_keywords(item["orientation"]["text"]))
        return backends.detector_evidence(
            "applied", labels=found.get("labels"),
            keywords=tokenize_keywords(item["orientation"]["text"]), reason=None)

    def emit_scored(item: dict, score: float) -> None:
        tier = bucket_tier(score, low, high)
        evidence_detector = detector_status(tier, item)
        if evidence_detector["status"] == "applied":
            stats["detector_applied"] += 1
        elif evidence_detector["status"] == "unavailable":
            stats["detector_unavailable"] += 1
        pair = copy.deepcopy(item["pair"])
        label = TIER_LABELS[tier]
        action = TIER_ACTIONS[tier]
        destination = TIER_DESTINATIONS[tier]
        # [v4.2] 判定型 status：隔离档为过滤（0），其余为不过滤（1）
        status_int = 0 if tier == TIER_ISOLATED else 1
        record = attach_common(pair, item, STATUS_SCORED, tier, label, action,
                               destination, now, status_int=status_int)
        record["image_text_score"] = {
            "operator": OPERATOR_LABEL,
            "clip_score": round(float(score), 6),
            "score_semantics": "cosine",
            "tier": tier,
            "label": label,
            "action": action,
            "destination": destination,
            "status": status_int,
            "process_status": STATUS_SCORED,
            "reason": None,
            "orientation": item["orientation"]["orientation"],
            "orientation_alternatives": item["orientation"].get("alternatives", []),
            "thresholds": {"isolation": low, "auto_align": high},
            "model": model_identity(),
            "detector": evidence_detector,
            "event_time": now,
            "batch_id": item.get("batch_id"),
            "retry_count": item.get("retry_count", 0),
        }
        stats["scored"] += 1
        if tier == TIER_ISOLATED:
            stats["isolated"] += 1
            isolation.append(make_ledger(
                item["line"], item["orientation"]["text_sample_id"], "clip_below_threshold",
                label, action, destination, now,
                batch_id=item.get("batch_id"), retry_count=item.get("retry_count", 0),
                evidence={"clip_score": round(float(score), 6), "tier": tier,
                          "thresholds": {"isolation": low, "auto_align": high},
                          "orientation": item["orientation"]["orientation"],
                          "image_reference": record["image_reference"],
                          "text_reference": record["text_reference"],
                          "detector": evidence_detector},
                record=record, extra={"pair_id": record.get("pair_id")}))
        else:
            if tier == TIER_AUTO_ALIGNED:
                stats["auto_aligned"] += 1
            else:
                stats["pending_manual"] += 1
            main.append(record)

    def emit_pending(item: dict, reason: str) -> None:
        pair = copy.deepcopy(item["pair"])
        # [v4.2] 待模型是正常流程态（不过滤）：status=1 + process_status=pending_model
        record = attach_common(pair, item, STATUS_PENDING_MODEL, None,
                               LABEL_FLOW_PENDING_MODEL, "wait_model",
                               DEST_PIPELINE_WAIT, now, status_int=1)
        record["image_text_score"] = {
            "operator": OPERATOR_LABEL,
            "clip_score": None,
            "score_semantics": None,
            "tier": None,
            "label": LABEL_FLOW_PENDING_MODEL,
            "action": "wait_model",
            "destination": DEST_PIPELINE_WAIT,
            "status": 1,
            "process_status": STATUS_PENDING_MODEL,
            "reason": reason,
            "degradation_detail": state["degraded_detail"],
            "orientation": item["orientation"]["orientation"],
            "thresholds": {"isolation": low, "auto_align": high},
            "model": None,
            "detector": {"status": "not_requested", "reason": None, "labels": [],
                         "matched_keywords": [], "matched_count": 0,
                         "tier_unchanged": True},
            "event_time": now,
            "batch_id": item.get("batch_id"),
            "retry_count": item.get("retry_count", 0),
        }
        record["reason"] = reason
        record["label"] = LABEL_FLOW_PENDING_MODEL
        record["destination"] = DEST_PIPELINE_WAIT
        stats["pending_model"] += 1
        pending_model.append(record)

    def flush() -> None:
        buffer = state["buffer"]
        if not buffer:
            return
        items = list(buffer)
        buffer.clear()
        if state["encoder"] is None:
            reason = state["degraded_reason"] or REASON_MODEL_UNAVAILABLE
            for item in items:
                emit_pending(item, reason)
            return
        try:
            text_vectors = state["encoder"].encode_texts(
                [item["orientation"]["text"] for item in items])
            image_vectors, errors = state["encoder"].encode_images(
                [item["target"] for item in items])
        except Exception as exc:  # noqa: BLE001 - 运行期后端整体失败：明确降级
            state["runtime_degraded"] = True
            state["encoder"] = None
            state["model_identity"] = None
            detail = "runtime_backend_failure: %s: %s" % (type(exc).__name__,
                                                          str(exc)[:120])
            state["degraded_reason"] = detail
            state["degraded_detail"] = detail
            for item in items:
                emit_pending(item, state["degraded_reason"])
            return
        for item, text_vector, image_vector, error in zip(items, text_vectors,
                                                          image_vectors, errors):
            if image_vector is None:
                media_reject(item, error or "media_unreadable")
                continue
            emit_scored(item, cosine_similarity(text_vector, image_vector))

    def media_reject(item: dict, reason: str) -> None:
        stats["media_rejected"] += 1
        label = _ORIGIN_LEDGER.get(reason, LABEL_ORIGIN_BROKEN)
        rejects.append(make_ledger(
            item["line"], item["orientation"]["text_sample_id"], reason, label,
            "record_and_isolate", DEST_ORIGIN, now,
            batch_id=item.get("batch_id"),
            evidence={"image_reference": item["orientation"]["image_path"],
                      "stage": "原始文件问题", "reason": reason},
            record=item["pair"], extra={"pair_id": item["pair"].get("pair_id")}))

    for lineno, record, error in lines:
        stats["input"] += 1
        if error is not None:
            stats["bad_lines"] += 1
            rejects.append(make_ledger(lineno, None, error,
                                       _ORIGIN_LEDGER.get(error, LABEL_ORIGIN_UNPARSABLE),
                                       "record_and_isolate", DEST_ORIGIN, now))
            continue
        pair_id = record.get("pair_id") if isinstance(record, dict) else None
        if not isinstance(pair_id, str) or not pair_id.strip():
            stats["structural_rejects"] += 1
            rejects.append(make_ledger(lineno, record.get("sample_id") if record else None,
                                       "missing_field: pair_id", LABEL_ORIGIN_MISSING_FIELD,
                                       "record_and_isolate", DEST_ORIGIN, now,
                                       batch_id=record.get("batch_id") if record else None))
            continue
        pair_id = pair_id.strip()
        if pair_id in seen_pair_ids:
            stats["structural_rejects"] += 1
            rejects.append(make_ledger(lineno, record.get("sample_id"),
                                       "duplicate_pair_id", LABEL_ORIGIN_MISSING_FIELD,
                                       "record_and_isolate", DEST_ORIGIN, now,
                                       batch_id=record.get("batch_id"),
                                       evidence={"first_line": seen_pair_ids[pair_id]},
                                       extra={"pair_id": pair_id}))
            continue
        seen_pair_ids[pair_id] = lineno
        stats["pairs_total"] += 1

        orientation = select_orientation(record)
        if not orientation.get("ok"):
            stats["not_applicable_pairs"] += 1
            not_applicable.append(make_ledger(
                lineno, record.get("sample_id"), orientation.get("reason", "no_image_text"),
                LABEL_FLOW_NOT_APPLICABLE, "pass_through", DEST_PIPELINE_WAIT, now,
                batch_id=record.get("batch_id"),
                evidence={"modality_pair": record.get("modality_pair"),
                          "stage": "流程状态，不隔离"},
                record=record, extra={"pair_id": pair_id}))
            continue
        if not route_enabled:
            stats["not_applicable_pairs"] += 1
            not_applicable.append(make_ledger(
                lineno, orientation["text_sample_id"], "route_closed", LABEL_FLOW_ROUTE_CLOSED,
                "pass_through", DEST_PIPELINE_WAIT, now,
                batch_id=record.get("batch_id"),
                evidence={"stage": "路线未开启，样本留产线，不写图文交付"},
                record=record, extra={"pair_id": pair_id}))
            continue

        stats["image_text_pairs"] += 1
        try:
            media_relative = safe_media_path(orientation["image_path"])
        except ValueError as exc:
            media_reject({"line": lineno, "pair": record, "orientation": orientation,
                          "batch_id": record.get("batch_id"), "reason": str(exc)},
                         "media_path_invalid")
            continue
        target = (root / media_relative).resolve()
        try:
            target.relative_to(root)
        except ValueError:
            media_reject({"line": lineno, "pair": record, "orientation": orientation,
                          "batch_id": record.get("batch_id")},
                         "media_path_invalid")
            continue
        if not os.path.isfile(str(target)):
            media_reject({"line": lineno, "pair": record, "orientation": orientation,
                          "batch_id": record.get("batch_id")}, "media_missing")
            continue

        state["buffer"].append({
            "line": lineno,
            "pair": record,
            "orientation": orientation,
            "media_relative": media_relative,
            "target": target,
            "batch_id": record.get("batch_id"),
            "retry_count": _retry_count(record),
        })
        if len(state["buffer"]) >= batch_size:
            flush()

    flush()

    stats["degraded"] = state["encoder"] is None
    stats["runtime_degraded"] = bool(state["runtime_degraded"])
    # 统计里放技术细节（便于排障）；待模型记录里放稳定业务状态码
    stats["degradation_reason"] = state["degraded_detail"] if state["encoder"] is None else None
    stats["conservation_ok"] = (
        stats["input"] == len(main) + len(isolation) + len(pending_model)
        + len(not_applicable) + len(rejects))
    stats["empty_input"] = stats["input"] == 0
    pipeline = build_flow_records(main, pending_model, not_applicable, list(seen_pair_ids), route_enabled)
    stats['forwarded'] = len(pipeline)
    return ScoringResult(main, isolation, not_applicable, pending_model, rejects, stats, pipeline=pipeline)
