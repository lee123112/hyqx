"""算子 22 的产物名与业务常量（唯一来源，供 tokens / transport / inference 共用）。

表1 #22 冻结项：
- **样本文件名固定 ``samples.jsonl``**（页面控件只读，配置员不改）；
- 文案里的模态标记固定为 ``<image>`` / ``<audio>`` / ``<video>``；
- 本算子**不压缩、不改版本状态**。
"""
from __future__ import annotations

__all__ = [
    "AUXILIARY_NAMES",
    "FLAGGED_NAME",
    "LOG_NAME",
    "MARKER_AUDIO",
    "MARKER_IMAGE",
    "MARKER_VIDEO",
    "MARKERS",
    "METADATA_INDEX_NAME",
    "METADATA_NAME",
    "MEDIA_MODALITIES",
    "MODALITY_ORDER",
    "OPERATOR",
    "OPERATOR_LABEL",
    "OPERATOR_NUMBER",
    "OPERATOR_VERSION",
    "PENDING_NAME",
    "REJECTS_NAME",
    "ROUTES",
    "ROUTE_FILES",
    "ROUTE_LOCAL_ANNOTATION",
    "ROUTE_ORDER",
    "SAMPLES_FILE_NAME",
    "STAGE_ID",
    "TOKENS_NAME",
    "UNTRAINABLE_NAME",
    "ISOLATED_NAME",
]

OPERATOR = "crossmodal_token"
OPERATOR_LABEL = "生成跨模态Token"
OPERATOR_NUMBER = 22
OPERATOR_VERSION = "v1.0.1"
STAGE_ID = "S5"

#: 表1 #22 配置项 2：样本文件名固定，改成其它值即配置失败
SAMPLES_FILE_NAME = "samples.jsonl"

#: 表1 #22 配置项 1：只读的模态标记
MARKER_IMAGE = "<image>"
MARKER_AUDIO = "<audio>"
MARKER_VIDEO = "<video>"
MARKERS = {"image": MARKER_IMAGE, "audio": MARKER_AUDIO, "video": MARKER_VIDEO}

#: 参与标记的媒体模态（文本本身不写标记）
MEDIA_MODALITIES = ("image", "audio", "video")
MODALITY_ORDER = ("image", "audio", "video", "text")

#: 路线（默认图文；音文/视文/局部标注按开关决定是否产出文件）
ROUTE_IMAGE_TEXT = "image_text"
ROUTE_AUDIO_TEXT = "audio_text"
ROUTE_VIDEO_TEXT = "video_text"
ROUTE_LOCAL_ANNOTATION = "local_annotation"
ROUTE_ORDER = (ROUTE_IMAGE_TEXT, ROUTE_AUDIO_TEXT, ROUTE_VIDEO_TEXT)
ROUTES = ROUTE_ORDER + (ROUTE_LOCAL_ANNOTATION,)

ROUTE_FILES = {
    ROUTE_IMAGE_TEXT: "samples_image_text.jsonl",
    ROUTE_AUDIO_TEXT: "samples_audio_text.jsonl",
    ROUTE_VIDEO_TEXT: "samples_video_text.jsonl",
    ROUTE_LOCAL_ANNOTATION: "samples_local_annotation.jsonl",
}

TOKENS_NAME = "22_tokens.jsonl"
METADATA_INDEX_NAME = "22_metadata_index.jsonl"
#: 旁路 flagged 样本单列清单（质量结论 flagged，仍生成 Token 但需复核）
FLAGGED_NAME = "22_flagged.jsonl"
#: 别名（元数据索引文件的简写）
METADATA_NAME = METADATA_INDEX_NAME
PENDING_NAME = "22_pending.jsonl"
UNTRAINABLE_NAME = "22_untrainable.jsonl"
ISOLATED_NAME = "22_isolated_skipped.jsonl"
REJECTS_NAME = "22_rejects.jsonl"
LOG_NAME = "22_run.log"

#: 除主样本文件外的全部本算子产物名（用于入口自动识别时排除）
AUXILIARY_NAMES = tuple(
    [name for name in ROUTE_FILES.values() if name != SAMPLES_FILE_NAME]
    + [TOKENS_NAME, METADATA_INDEX_NAME, FLAGGED_NAME, PENDING_NAME,
       UNTRAINABLE_NAME, ISOLATED_NAME, REJECTS_NAME])
