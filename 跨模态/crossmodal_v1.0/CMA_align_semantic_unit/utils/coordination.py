"""算子 8：跨模态协同预处理业务核心（纯函数，无文件系统/网络副作用）。

职责（v1.0.1 起收窄，表1 #8 第 1.1 节）：
- ``build_unit(record, config) -> (record, meta)``：记录级最基础校验（sample_id）
  与媒体引用收集，**不再组对、不再生成 ``semantic_unit``、不再校验图片/视频/音频元数据**；
- ``build_semantic_unit(record, config) -> dict``：兼容旧签名的薄封装；
- ``semantic_unit`` 字段固定为 ``null``（deprecated）：跨模态配对语义单元由 #12–#15
  按各自模态对生成，#8 只产出带时间索引的 ASR 段落单元（``time_index``）。

记录级失败抛 ``RecordError``（带可解释 reason）；配置非法抛 ``UnitConfigError``。
文件存在性、媒体搬运与 ASR 状态判定由 ``inference.run`` 负责，本模块不访问磁盘。
"""
from __future__ import annotations

import copy
import math
from dataclasses import dataclass
from typing import Any, Dict, List, Optional, Tuple

#: 参与模态统计的字段
MEDIA_FIELDS: Tuple[str, ...] = ("image", "video", "audio")
#: 记录级媒体引用字段（含需要一并搬运的原图）
MEDIA_PATH_KEYS: Tuple[str, ...] = ("path", "original_path")

#: 记录级 ``processing`` 节点键（平台 M08，包内编号 07）
NODE_KEY = "07"


class UnitConfigError(ValueError):
    """业务配置非法（枚举未知、数值越界等）。"""


class RecordError(Exception):
    """单条记录无法进入本节点（缺少 sample_id 等记录级缺陷）。"""

    def __init__(self, reason: str, message: str,
                 source_reference: Optional[List[str]] = None) -> None:
        super().__init__(message)
        self.reason = reason
        self.message = message
        self.source_reference: List[str] = list(source_reference or [])


@dataclass(frozen=True)
class UnitOptions:
    """[deprecated] 旧配对参数兼容载体。

    v1.0.1 起 #8 不再组对，``unit_policy`` / 音视频时长比对 / 帧时间戳校验
    均已失效；保留本类只为旧调用方（``inference.RunContext``、旧测试字典）
    仍能构造配置而不报错，**不再参与任何判定**。
    """

    unit_policy: str = "per_sample"
    max_av_duration_diff_ratio: float = 0.20
    av_mismatch_action: str = "review"
    require_existing_media: bool = True
    validate_frame_timestamps: bool = True

    @classmethod
    def from_mapping(cls, config: Optional[Dict[str, Any]]) -> "UnitOptions":
        """从 dict / OperatorConfig 构造；保留旧校验只是为了尽早暴露拼写错误。

        注意：返回值不再影响任何流程；新配置可以完全省略这些键。
        """
        if config is None:
            return cls()
        if isinstance(config, cls):
            return config
        if hasattr(config, "business_dict"):
            data = dict(config.business_dict())
        elif isinstance(config, dict):
            data = dict(config)
        else:
            raise UnitConfigError("config 必须是对象")

        known = {
            "unit_policy", "max_av_duration_diff_ratio", "av_mismatch_action",
            "require_existing_media", "validate_frame_timestamps",
        }
        # 表1 #8 的转写开关与设置由 inference 层消费，不是本模块的归一化参数；
        # 显式忽略以免被误判成拼写错误（它们仍需随血缘快照传递）。
        passthrough = {"enabled", "transcription", "transcription_enabled"}
        unknown = sorted(set(data) - known - passthrough)
        if unknown:
            raise UnitConfigError("未知业务参数：%s" % ",".join(unknown))

        policy = data.get("unit_policy", "per_sample")
        if policy != "per_sample":
            raise UnitConfigError("unit_policy 只支持 per_sample，收到：%s" % policy)

        action = data.get("av_mismatch_action", "review")
        if action not in ("review", "reject"):
            raise UnitConfigError("av_mismatch_action 只支持 review/reject，收到：%s" % action)

        ratio = data.get("max_av_duration_diff_ratio", 0.20)
        if isinstance(ratio, bool) or not isinstance(ratio, (int, float)):
            raise UnitConfigError("max_av_duration_diff_ratio 必须是数值")
        ratio = float(ratio)
        if not math.isfinite(ratio) or not 0.0 <= ratio <= 1.0:
            raise UnitConfigError("max_av_duration_diff_ratio 必须落在 [0,1] 且有限")

        for key in ("require_existing_media", "validate_frame_timestamps"):
            if key in data and not isinstance(data[key], bool):
                raise UnitConfigError("%s 必须是布尔值" % key)

        return cls(
            unit_policy=policy,
            max_av_duration_diff_ratio=ratio,
            av_mismatch_action=action,
            require_existing_media=bool(data.get("require_existing_media", True)),
            validate_frame_timestamps=bool(data.get("validate_frame_timestamps", True)),
        )


# ------------------------------------------------------------------ 字段访问

def safe_stem(sample_id: Any) -> str:
    """把 sample_id 收敛为安全文件名主干（不改业务 sample_id）。"""
    cleaned = "".join(
        ch if (ch.isalnum() or ch in "-_.") else "_" for ch in str(sample_id)
    ).strip("._")
    return cleaned[:80] or "sample"


def text_content_of(value: Any) -> Optional[str]:
    """取文本内容：``str`` 直接使用，对象取 ``content``，``null`` 返回 None。

    [v1.0.1] 只用于模态统计；类型不符不再判失败（#8 不做文本强校验）。
    """
    if value is None:
        return None
    if isinstance(value, str):
        return value
    if isinstance(value, dict):
        content = value.get("content")
        return content if isinstance(content, str) else None
    return None


def media_path_of(value: Any) -> Optional[str]:
    """取模态对象的相对媒体路径；``str`` 视为路径。

    保留给旧调用方；类型不符返回 ``None`` 而不是抛错。
    """
    if value is None:
        return None
    if isinstance(value, str):
        return value.strip() or None
    if isinstance(value, dict):
        path = value.get("path")
        if isinstance(path, str) and path.strip():
            return path.strip()
    return None


def media_references(record: Dict[str, Any]) -> List[str]:
    """收集记录中所有需要搬运的媒体相对路径（含原图/关键帧/音频片段）。"""
    references: List[str] = []
    for field in MEDIA_FIELDS:
        value = record.get(field)
        if isinstance(value, str):
            if value.strip():
                references.append(value.strip())
            continue
        if not isinstance(value, dict):
            continue
        for key in MEDIA_PATH_KEYS:
            item = value.get(key)
            if isinstance(item, str) and item.strip():
                references.append(item.strip())
        frames = value.get("frames")
        if isinstance(frames, list):
            for frame in frames:
                if isinstance(frame, dict) and isinstance(frame.get("path"), str) and frame["path"].strip():
                    references.append(frame["path"].strip())
        segments = value.get("segments")
        if isinstance(segments, list):
            for segment in segments:
                if isinstance(segment, dict) and isinstance(segment.get("path"), str) and segment["path"].strip():
                    references.append(segment["path"].strip())
    return list(dict.fromkeys(references))


def present_modalities(record: Dict[str, Any]) -> List[str]:
    """按出现顺序列出记录实际携带的模态（``text/image/video/audio``）。"""
    present: List[str] = []
    if (text_content_of(record.get("text")) or "").strip():
        present.append("text")
    for field in MEDIA_FIELDS:
        if record.get(field) is not None:
            present.append(field)
    return present


def has_audio_source(record: Dict[str, Any]) -> bool:
    """是否存在可转写来源：``audio`` 字段，或（可能带音轨的）``video`` 字段。"""
    return record.get("audio") is not None or record.get("video") is not None


# ------------------------------------------------------------------ 主流程

def _merge_processing(record: Dict[str, Any], key: str, payload: Dict[str, Any]) -> Dict[str, Any]:
    """只替换本节点编号的结果，保留其他节点的 processing 信息。"""
    existing = record.get("processing")
    merged = copy.deepcopy(existing) if isinstance(existing, dict) else {}
    merged[key] = payload
    return merged


def build_unit(record: Dict[str, Any], config: Any) -> Tuple[Dict[str, Any], Dict[str, Any]]:
    """记录级最基础校验 + 媒体引用收集，返回 ``(输出记录, 统计元信息)``。

    v1.0.1 起本函数**不做**跨模态配对判定、不生成 ``semantic_unit``、不校验
    图片尺寸/视频帧时间戳/音频采样率等上游字段（上游缺字段不应拒掉整条记录）。
    记录级流程态（transcribed/skipped/not_applicable/no_speech/failed）由
    ``inference._apply_transcription`` 依据 ASR 结果写入。

    ``meta`` 供 run 统计与旁路记录使用：
    ``sample_id`` / ``media_references`` / ``present_modalities`` / ``has_audio_source``。
    """
    if not isinstance(record, dict):
        raise RecordError("invalid_record", "顶层不是 JSON 对象")
    # 仍保留旧参数的拼写/取值校验（尽早暴露配置错误）；返回值不参与判定
    UnitOptions.from_mapping(config)

    raw_sid = record.get("sample_id")
    if not isinstance(raw_sid, str) or not raw_sid.strip():
        raise RecordError("invalid_record", "sample_id 缺失或不是非空字符串")
    sample_id = raw_sid.strip()

    output = copy.deepcopy(record)
    output["sample_id"] = sample_id
    # [v1.0.1] deprecated：固定 null，绝不生成。跨模态配对单元由 #12–#15 负责。
    output["semantic_unit"] = None
    # alignment/qc 保持初始化语义（#17 读顶层 alignment，不能删）
    output["alignment"] = _ensure_alignment(record)
    output["qc"] = _ensure_qc(record)

    references = media_references(record)
    meta: Dict[str, Any] = {
        "sample_id": sample_id,
        "media_references": references,
        "present_modalities": present_modalities(record),
        "has_audio_source": has_audio_source(record),
    }
    return output, meta


def _ensure_alignment(record: Dict[str, Any]) -> Any:
    """初始化缺失的 alignment；已有值原样保留（不覆盖上游结果）。"""
    existing = record.get("alignment")
    if isinstance(existing, dict):
        return copy.deepcopy(existing)
    if existing is None:
        return {"whole": None, "local": []}
    return existing


def _ensure_qc(record: Dict[str, Any]) -> Any:
    """初始化缺失的 qc；已有值原样保留。"""
    existing = record.get("qc")
    if isinstance(existing, dict):
        return copy.deepcopy(existing)
    if existing is None:
        return {}
    return existing


def build_semantic_unit(record: Dict[str, Any], config: Any) -> Dict[str, Any]:
    """TECHNICAL_DESIGN §3 冻结签名：校验记录并返回输出记录（不再生成语义单元）。"""
    output, _meta = build_unit(record, config)
    return output
