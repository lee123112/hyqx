"""表1 #01「接入多模态批次」业务规则模块（batch intake & registration）。

表1 v4.1 冻结口径
-----------------

- **环节**：输入核验（``stageId=S1``，本算子是**阶段首算子**），系统功能型、规则型、无模型、P0；
- **总开关**：默认开启；关闭时本环节不跑（只产出空台账，`status=disabled`）；
- **数据从哪里来**：采集平台批次 / 预训练文本产线交付 / 预训练图像产线交付 /
  预训练音频产线交付 / 预训练视频产线交付（**只有这五类**，不能手填别的）；
- **允许的文件格式**：文本 ``txt/json/csv``；图像 ``jpg/png/webp``；
  音频 ``wav/mp3/flac``；视频 ``mp4/mov/avi``（不在这张清单里的文件进原始隔离区）；
- **授权**：只有「可训练」可以进入加工，待确认/不可训练/无法确认进样本隔离库；
- **过程**：校验来源、格式白名单和授权；通过项写工作集，**原文件只保存链接**（不复制）；
- **输出**：可加工对象清单（交 #2（开启时）或 #3）+ 原始隔离记录 + 样本隔离记录 + 批次台账。

四路分流（判定优先级：结构 → 链接 → 格式 → 本地文件 → 授权 → 来源/模态 → 登记要素 → 通过）
-----------------------------------------------------------------------------------

============== ========================================== ================== ==============
分流           触发条件                                    统一标签            去向
============== ========================================== ================== ==============
主输出         全部校验通过                                ``流程-可加工``      工作集
原始隔离       缺 ``sample_id``、链接不可搬运、格式不在     ``原始文件-无法解析`` 原始隔离区
               白名单、本地文件空/打不开/文件头不符
样本隔离       授权不是「可训练」                          ``样本-敏感``        样本隔离库
待补登记       缺来源/来源ID/批次号/上游版本、重复登记、     ``流程-待补登记``     采集平台批次登记
               声明模态与扩展名不一致
============== ========================================== ================== ==============

「原始文件问题优先」：一条记录只归入一个台账，保证统计唯一归口；
坏行/非对象/编码错误进原始隔离台账的拒绝子类，**不静默丢弃**。

本文件由算子 01 在本目录内自带，不跨目录 import 兄弟算子。
"""
from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Dict, List, Optional, Sequence, Tuple

from .io import is_within, resolved_absolute

# --------------------------------------------------------------------------- #
# 身份与产物
# --------------------------------------------------------------------------- #

OPERATOR_NAME = "multimodal_batch_intake"
OPERATOR_LABEL = "接入多模态批次"
OPERATOR_VERSION = "v1.0.0"
STAGE_ID = "S1"
STAGE_LABEL = "输入核验"
RUN_STAGE = "multimodal_batch_intake"
FORMAL_OPERATOR_NUMBER = 1
DEFAULT_PORT = 8101

MAIN_NAME = "01_workitems.jsonl"
ORIGIN_QUARANTINE_NAME = "01_origin_quarantine.jsonl"
SAMPLE_QUARANTINE_NAME = "01_sample_quarantine.jsonl"
RETURN_NAME = "01_return.jsonl"
REGISTRY_NAME = "01_batch_registry.json"
LOG_NAME = "01_run.log"

STAGE_ARTIFACT_ORDER: Tuple[str, ...] = (
    MAIN_NAME, ORIGIN_QUARANTINE_NAME, SAMPLE_QUARANTINE_NAME, RETURN_NAME,
    REGISTRY_NAME,
)

REGISTRY_FORMAT = "crossmodal-batch-registry-v1"
TRANSPORT_FORMAT = "crossmodal-files-v1"
DEFAULT_MAX_RESPONSE_BYTES = 67108864

# --------------------------------------------------------------------------- #
# 冻结：来源
# --------------------------------------------------------------------------- #

SOURCE_PLATFORM = "collection_platform"
SOURCE_TEXT = "text_pipeline"
SOURCE_IMAGE = "image_pipeline"
SOURCE_AUDIO = "audio_pipeline"
SOURCE_VIDEO = "video_pipeline"

SOURCES: Tuple[str, ...] = (
    SOURCE_PLATFORM, SOURCE_TEXT, SOURCE_IMAGE, SOURCE_AUDIO, SOURCE_VIDEO,
)

SOURCE_LABELS: Dict[str, str] = {
    SOURCE_PLATFORM: "采集平台批次",
    SOURCE_TEXT: "预训练文本产线交付",
    SOURCE_IMAGE: "预训练图像产线交付",
    SOURCE_AUDIO: "预训练音频产线交付",
    SOURCE_VIDEO: "预训练视频产线交付",
}

#: 来源规范化别名（键一律小写；中文不受 lower 影响）
SOURCE_ALIASES: Dict[str, str] = {}
for _code, _label in SOURCE_LABELS.items():
    for _alias in (_code, _label):
        SOURCE_ALIASES[_alias.lower()] = _code
SOURCE_ALIASES.update({
    "采集平台": SOURCE_PLATFORM,
    "采集平台批次登记": SOURCE_PLATFORM,
    "platform": SOURCE_PLATFORM,
    "collection-platform": SOURCE_PLATFORM,
    "预训练文本产线": SOURCE_TEXT,
    "文本产线交付": SOURCE_TEXT,
    "text-pipeline": SOURCE_TEXT,
    "预训练图像产线": SOURCE_IMAGE,
    "图像产线交付": SOURCE_IMAGE,
    "image-pipeline": SOURCE_IMAGE,
    "预训练音频产线": SOURCE_AUDIO,
    "音频产线交付": SOURCE_AUDIO,
    "audio-pipeline": SOURCE_AUDIO,
    "预训练视频产线": SOURCE_VIDEO,
    "视频产线交付": SOURCE_VIDEO,
    "video-pipeline": SOURCE_VIDEO,
})

#: 单模态产线只登记对应模态；采集平台批次不限制模态（None 表示不限）
SOURCE_MODALITIES: Dict[str, Optional[Tuple[str, ...]]] = {
    SOURCE_PLATFORM: None,
    SOURCE_TEXT: ("text",),
    SOURCE_IMAGE: ("image",),
    SOURCE_AUDIO: ("audio",),
    SOURCE_VIDEO: ("video",),
}

# --------------------------------------------------------------------------- #
# 冻结：允许的文件格式白名单
# --------------------------------------------------------------------------- #

FORMAT_GROUPS: Dict[str, Tuple[str, ...]] = {
    "text": ("txt", "json", "csv"),
    "image": ("jpg", "png", "webp"),
    "audio": ("wav", "mp3", "flac"),
    "video": ("mp4", "mov", "avi"),
}
WHITELISTED_EXTENSIONS: Tuple[str, ...] = tuple(
    ext for exts in FORMAT_GROUPS.values() for ext in exts)
EXT_TO_MODALITY: Dict[str, str] = {
    ext: modality for modality, exts in FORMAT_GROUPS.items() for ext in exts}

#: 文件头签名所需常量（纯标准库，不做解码、不依赖第三方）
_TEXT_EXTENSIONS = frozenset(FORMAT_GROUPS["text"])
_RIFF_BRAND = {"webp": b"WEBP", "wav": b"WAVE", "avi": b"AVI "}

# --------------------------------------------------------------------------- #
# 冻结：授权
# --------------------------------------------------------------------------- #

LICENSE_TRAINABLE = "trainable"
LICENSE_UN_TRAINABLE = "untrainable"
LICENSE_PENDING = "pending"
LICENSE_MISSING = "missing"
LICENSE_UNKNOWN = "unknown"

#: 表1 只允许「可训练」进入加工
ALLOWED_LICENSE_LABEL = "可训练"
LICENSE_ALIASES: Dict[str, str] = {
    "可训练": LICENSE_TRAINABLE,
    "trainable": LICENSE_TRAINABLE,
    "可训练数据": LICENSE_TRAINABLE,
    "不可训练": LICENSE_UN_TRAINABLE,
    "untrainable": LICENSE_UN_TRAINABLE,
    "非可训练": LICENSE_UN_TRAINABLE,
    "not_trainable": LICENSE_UN_TRAINABLE,
    "待确认": LICENSE_PENDING,
    "pending": LICENSE_PENDING,
    "to_be_confirmed": LICENSE_PENDING,
    "待定": LICENSE_PENDING,
}
LICENSE_LABELS: Dict[str, str] = {
    LICENSE_TRAINABLE: "可训练",
    LICENSE_UN_TRAINABLE: "不可训练",
    LICENSE_PENDING: "待确认",
    LICENSE_MISSING: "未提供",
    LICENSE_UNKNOWN: "无法确认",
}
#: 授权嵌套对象里可能承载结论的键
_LICENSE_KEYS = ("conclusion", "license", "verdict", "value", "result", "status")

# --------------------------------------------------------------------------- #
# 分流与台账常量
# --------------------------------------------------------------------------- #

DECISION_REGISTERED = "registered"
DECISION_ORIGIN = "origin_quarantine"
DECISION_SAMPLE = "sample_quarantine"
DECISION_RETURN = "returned"

# [v4.2] status 双字段（§2.10）：输出记录 int status（本算子处理型：1=登记成功/正常流程态，
# 0=失败/其他去向）+ 原字符串迁入 process_status（registered/origin_quarantine/...）
_STATUS_AS_INT = {
    DECISION_REGISTERED: 1,
    DECISION_ORIGIN: 0,
    DECISION_SAMPLE: 0,
    DECISION_RETURN: 0,
}


def status_int(value: Any) -> int:
    """记录级字符串状态 → [v4.2] int status；未登记的一律按正常流程态 1。"""
    return _STATUS_AS_INT.get(value, 1)


LABEL_REGISTERED = "流程-可加工"
LABEL_ORIGIN = "原始文件-无法解析"
LABEL_SAMPLE = "样本-敏感"
LABEL_RETURN = "流程-待补登记"

DESTINATION_WORKSET = "工作集"
DESTINATION_ORIGIN = "原始隔离区"
DESTINATION_SAMPLE = "样本隔离库"
DESTINATION_RETURN = "采集平台批次登记"

ACTION_REGISTER = "register"
ACTION_ISOLATE = "isolate"
ACTION_RETURN = "return"

#: 文件可读性检查模式
FILE_CHECK_MODE_WHITELIST = "whitelist"
FILE_CHECK_MODE_HEADER = "header"
FILE_CHECK_MODE_STRICT = "strict"
FILE_CHECK_MODES: Tuple[str, ...] = (
    FILE_CHECK_MODE_WHITELIST, FILE_CHECK_MODE_HEADER, FILE_CHECK_MODE_STRICT)

FILE_CHECK_LINK_ONLY = "link_only"
FILE_CHECK_LOCAL_VERIFIED = "local_verified"

#: 拒绝行预览长度上限（日志与台账都不留完整原文）
MAX_PREVIEW_CHARS = 200

#: 登记要素（缺失即退回补登记）
REGISTRATION_FIELDS: Tuple[str, ...] = ("source", "source_id", "batch_id",
                                        "upstream_version")

REASON_TEXT: Dict[str, str] = {
    "missing_sample_id": "缺少 sample_id，无法登记为可加工对象",
    "file_entry_not_object": "files 成员不是对象，无法解析文件登记项",
    "missing_file_link": "缺少文件链接（oss/相对路径）",
    "link_not_transportable": "文件链接不可搬运（绝对路径/反斜杠/目录穿越/URL 写法非法）",
    "format_not_allowed": "文件格式不在表1 白名单",
    "modality_mismatch": "声明模态与扩展名推断不一致",
    "file_empty": "本地文件为空",
    "file_unreadable": "本地文件无法打开或读取",
    "file_signature_mismatch": "本地文件头与扩展名不匹配",
    "file_not_accessible": "严格模式下未能访问到本地文件",
    "license_untrainable": "授权结论为不可训练",
    "license_pending": "授权结论为待确认",
    "license_missing": "缺少授权结论",
    "license_unknown": "授权结论无法识别",
    "incomplete_registration": "缺少登记要素",
    "source_modality_mismatch": "来源产线模态与文件模态不一致",
    "duplicate_registration": "同一对象重复登记",
    "json_parse_error": "该行不是合法 JSON",
    "record_not_object": "该行不是 JSON 对象",
    "invalid_utf8": "该行不是合法 UTF-8",
    "line_too_long": "该行超过单行字节上限",
}

_LICENSE_REASON = {
    LICENSE_UN_TRAINABLE: "license_untrainable",
    LICENSE_PENDING: "license_pending",
    LICENSE_MISSING: "license_missing",
    LICENSE_UNKNOWN: "license_unknown",
}

_LINK_KEYS: Tuple[str, ...] = ("oss_uri", "oss", "uri", "url", "link", "path",
                               "file_path", "file", "source_file")
_MODALITY_KEYS: Tuple[str, ...] = ("modality", "media_type")
_SIZE_KEYS: Tuple[str, ...] = ("size_bytes", "bytes", "size")
_LIST_KEYS: Tuple[str, ...] = ("files", "media", "objects")

#: 默认业务参数（与 ``utils.schemas.OperatorConfig.business_dict`` 同源同键）
DEFAULT_BUSINESS: Dict[str, Any] = {
    "operator": OPERATOR_NAME,
    "operator_label": OPERATOR_LABEL,
    "operator_number": FORMAL_OPERATOR_NUMBER,
    "operator_version": OPERATOR_VERSION,
    "stage_id": STAGE_ID,
    "enabled": True,
    "file_check_mode": FILE_CHECK_MODE_HEADER,
    "dedupe": True,
    "check_source_modality": False,
    "allowed_license": ALLOWED_LICENSE_LABEL,
    "sources": list(SOURCES),
    "file_formats": {key: list(value) for key, value in FORMAT_GROUPS.items()},
    "transport": {"format": TRANSPORT_FORMAT,
                  "max_response_bytes": DEFAULT_MAX_RESPONSE_BYTES},
}


# --------------------------------------------------------------------------- #
# 通用小工具
# --------------------------------------------------------------------------- #

def _deep_merge(base: Dict[str, Any], patch: Optional[Dict[str, Any]]) -> Dict[str, Any]:
    result: Dict[str, Any] = dict(base)
    for key, value in (patch or {}).items():
        if isinstance(value, dict) and isinstance(result.get(key), dict):
            result[key] = _deep_merge(result[key], value)
        else:
            result[key] = value
    return result


def clean_text(value: Any) -> str:
    """取字符串并去首尾空白；非字符串返回空串。"""
    if value is None:
        return ""
    if isinstance(value, str):
        return value.strip()
    if isinstance(value, bool):
        return ""
    if isinstance(value, (int, float)):
        return str(value)
    return ""


def default_business(**overrides: Any) -> Dict[str, Any]:
    """业务参数字典（测试与核心业务共用同一套默认值）。"""
    return _deep_merge(DEFAULT_BUSINESS, overrides)


def source_label(code: Optional[str]) -> str:
    if not code:
        return ""
    return SOURCE_LABELS.get(code, code)


def normalize_source(value: Any) -> Optional[str]:
    text = clean_text(value)
    if not text:
        return None
    return SOURCE_ALIASES.get(text.lower())


def normalize_license(value: Any) -> str:
    """把授权结论归一为 ``trainable / untrainable / pending / missing / unknown``。"""
    if isinstance(value, dict):
        value = next((value[key] for key in _LICENSE_KEYS if value.get(key) not in
                      (None, "", [], {})), None)
    text = clean_text(value)
    if not text:
        return LICENSE_MISSING
    return LICENSE_ALIASES.get(text.lower(), LICENSE_UNKNOWN)


def extension_of(link: Any) -> str:
    """取链接的扩展名（小写、去查询串）；无扩展名返回空串。"""
    text = clean_text(link)
    if not text:
        return ""
    text = text.split("?", 1)[0].split("#", 1)[0]
    name = text.replace("\\", "/").rsplit("/", 1)[-1]
    if "." not in name:
        return ""
    ext = name.rsplit(".", 1)[-1].strip().lower()
    if not ext or not all(ch.isalnum() for ch in ext):
        return ""
    return ext


def infer_modality(ext: Any) -> Optional[str]:
    return EXT_TO_MODALITY.get(clean_text(ext).lower() or None)


def classify_link(link: Any) -> Dict[str, Any]:
    """判定文件链接是否可搬运；返回 ``{value, scheme, remote, relative}``。

    远端（``oss/s3/http(s)/gs`` 等带 scheme）只登记链接；
    相对路径必须是包内可搬运相对路径，否则抛 ``ValueError``。
    """
    text = clean_text(link)
    if not text:
        raise ValueError("文件链接不能为空")
    if "://" in text:
        scheme = text.split("://", 1)[0].strip().lower()
        if not scheme or not all(ch.isalnum() or ch in "+-." for ch in scheme):
            raise ValueError("文件链接的 scheme 非法：%s" % text)
        return {"value": text, "scheme": scheme, "remote": True, "relative": None}
    # 非远端：必须是包内相对路径（复用 zip 成员校验，拒绝盘符/绝对/穿越/反斜杠）
    from .io import normalize_zip_member_name

    relative = normalize_zip_member_name(text)
    return {"value": text, "scheme": None, "remote": False, "relative": relative}


def signature_ok(ext: str, head: bytes) -> bool:
    """文件头签名是否与扩展名一致（文本格式不做内容校验）。"""
    ext = clean_text(ext).lower()
    if ext in _TEXT_EXTENSIONS:
        return True
    if ext in _RIFF_BRAND:
        return len(head) >= 12 and head[:4] == b"RIFF" and head[8:12] == _RIFF_BRAND[ext]
    if ext in ("mp4", "mov"):
        return len(head) >= 8 and head[4:8] == b"ftyp"
    if ext == "png":
        return head.startswith(b"\x89PNG\r\n\x1a\n")
    if ext == "jpg":
        return head.startswith(b"\xff\xd8\xff")
    if ext == "flac":
        return head.startswith(b"fLaC")
    if ext == "mp3":
        return head.startswith(b"ID3") or head[:2] in (b"\xff\xfb", b"\xff\xf3",
                                                       b"\xff\xf2")
    return False


def check_local_file(*, ext: str, link_info: Dict[str, Any],
                     local_dirs: Sequence[Any],
                     mode: str) -> Tuple[str, str, str]:
    """本地可读性/文件头检查；返回 ``(检查状态, 原因码, 原因文本)``。

    - ``whitelist``：只校验格式白名单，不打开文件；
    - ``header``（默认）：能定位到本地文件时校验非空 + 文件头签名；
    - ``strict``：必须能定位并打开本地文件，否则进原始隔离区。
    """
    if mode == FILE_CHECK_MODE_WHITELIST:
        return FILE_CHECK_LINK_ONLY, "", ""
    if link_info.get("remote"):
        if mode == FILE_CHECK_MODE_STRICT:
            return "rejected", "file_not_accessible", "严格模式不允许仅登记远端链接"
        return FILE_CHECK_LINK_ONLY, "", ""
    relative = link_info.get("relative") or ""
    for base in local_dirs:
        root = resolved_absolute(base)
        candidate = resolved_absolute(root / relative)
        if not is_within(root, candidate) or not candidate.is_file():
            continue
        try:
            if candidate.stat().st_size == 0:
                return "rejected", "file_empty", "%s：本地文件为空" % relative
            with open(candidate, "rb") as stream:
                head = stream.read(64)
        except OSError:
            return "rejected", "file_unreadable", "%s：本地文件无法打开" % relative
        if not signature_ok(ext, head):
            return ("rejected", "file_signature_mismatch",
                    "%s：文件头与扩展名 .%s 不匹配" % (relative, ext))
        return FILE_CHECK_LOCAL_VERIFIED, "", ""
    if mode == FILE_CHECK_MODE_STRICT:
        return "rejected", "file_not_accessible", "%s：未在任务目录内找到本地文件" % relative
    return FILE_CHECK_LINK_ONLY, "", ""


def _preview(text: Any) -> str:
    if not isinstance(text, str):
        text = "" if text is None else str(text)
    cleaned = "".join(ch if ch >= " " or ch == "\t" else " " for ch in text).strip()
    return cleaned[:MAX_PREVIEW_CHARS]


# --------------------------------------------------------------------------- #
# 展开记录中的文件登记项
# --------------------------------------------------------------------------- #

def _link_from(mapping: Dict[str, Any]) -> Optional[str]:
    for key in _LINK_KEYS:
        raw = mapping.get(key)
        if isinstance(raw, str) and raw.strip():
            return raw.strip()
        if isinstance(raw, dict):
            nested = _link_from(raw)
            if nested:
                return nested
        if isinstance(raw, list):
            for item in raw:
                if isinstance(item, dict):
                    nested = _link_from(item)
                    if nested:
                        return nested
    return None


def _modality_from(mapping: Dict[str, Any]) -> Optional[str]:
    for key in _MODALITY_KEYS:
        text = clean_text(mapping.get(key)).lower()
        if text:
            return text
    return None


def _size_from(mapping: Dict[str, Any]) -> Optional[int]:
    for key in _SIZE_KEYS:
        value = mapping.get(key)
        if isinstance(value, bool):
            continue
        if isinstance(value, int) and value >= 0:
            return int(value)
        if isinstance(value, str) and value.strip().isdigit():
            return int(value.strip())
    return None


def _object_from_entry(entry: Any, index: Optional[int]) -> Dict[str, Any]:
    if not isinstance(entry, dict):
        return {"file_index": index, "file_link": None, "declared_modality": None,
                "size_bytes": None, "invalid_entry": True, "entry": None}
    return {"file_index": index, "file_link": _link_from(entry),
            "declared_modality": _modality_from(entry),
            "size_bytes": _size_from(entry), "invalid_entry": False,
            "entry": entry}


def expand_objects(record: Dict[str, Any]) -> List[Dict[str, Any]]:
    """把一条批次登记记录展开为若干「文件登记项」。

    支持 ``files`` / ``media`` / ``objects`` 列表，以及记录级单文件简写
    （``file_path`` / ``oss_uri`` / ``url`` / ``path`` / ``file`` + ``modality``）。
    """
    if not isinstance(record, dict):
        return []
    for key in _LIST_KEYS:
        value = record.get(key)
        if isinstance(value, list) and value:
            return [_object_from_entry(item, index) for index, item in enumerate(value)]

    shorthand: Dict[str, Any] = {}
    for key in _LINK_KEYS:
        if record.get(key) not in (None, "", [], {}):
            shorthand[key] = record[key]
    modality = _modality_from(record)
    if modality:
        shorthand["modality"] = modality
    size = _size_from(record)
    if size is not None:
        shorthand["size_bytes"] = size
    if not shorthand:
        return [{"file_index": None, "file_link": None, "declared_modality": None,
                 "size_bytes": None, "invalid_entry": False, "entry": None}]
    return [_object_from_entry(shorthand, None)]


# --------------------------------------------------------------------------- #
# 评估结果
# --------------------------------------------------------------------------- #

@dataclass
class Outcome:
    """一条文件登记项的判定结果（唯一归口，只属于一个台账）。"""

    sample_id: str
    decision: str = DECISION_RETURN
    reason_code: str = ""
    reason: str = ""
    label: str = LABEL_RETURN
    action: str = ACTION_RETURN
    destination: str = DESTINATION_RETURN
    file_link: Optional[str] = None
    modality: Optional[str] = None
    format_ext: Optional[str] = None
    declared_modality: Optional[str] = None
    license_state: str = ""
    source: Optional[str] = None
    file_check: str = ""
    missing_fields: List[str] = field(default_factory=list)
    file_index: Optional[int] = None
    lineno: int = 0
    size_bytes: Optional[int] = None
    record: Optional[Dict[str, Any]] = None


def _as_origin(outcome: Outcome, reason_code: str, reason: str) -> Outcome:
    outcome.decision = DECISION_ORIGIN
    outcome.reason_code = reason_code
    outcome.reason = reason or REASON_TEXT.get(reason_code, reason_code)
    outcome.label = LABEL_ORIGIN
    outcome.action = ACTION_ISOLATE
    outcome.destination = DESTINATION_ORIGIN
    return outcome


def _as_sample(outcome: Outcome, reason_code: str, reason: str = "") -> Outcome:
    outcome.decision = DECISION_SAMPLE
    outcome.reason_code = reason_code
    outcome.reason = reason or REASON_TEXT.get(reason_code, reason_code)
    outcome.label = LABEL_SAMPLE
    outcome.action = ACTION_ISOLATE
    outcome.destination = DESTINATION_SAMPLE
    return outcome


def _as_return(outcome: Outcome, reason_code: str, reason: str = "",
               missing: Optional[List[str]] = None) -> Outcome:
    outcome.decision = DECISION_RETURN
    outcome.reason_code = reason_code
    outcome.reason = reason or REASON_TEXT.get(reason_code, reason_code)
    outcome.label = LABEL_RETURN
    outcome.action = ACTION_RETURN
    outcome.destination = DESTINATION_RETURN
    if missing:
        outcome.missing_fields = list(missing)
    return outcome


def evaluate_object(record: Dict[str, Any], *, sample_id: str, obj: Dict[str, Any],
                    business: Dict[str, Any], lineno: int,
                    local_dirs: Optional[Sequence[Any]] = None) -> Outcome:
    """评估一个文件登记项，返回唯一归口的判定结果。"""
    outcome = Outcome(sample_id=sample_id, lineno=lineno, record=record,
                      declared_modality=obj.get("declared_modality"),
                      file_index=obj.get("file_index"),
                      size_bytes=obj.get("size_bytes"))

    # 1) 结构：缺 sample_id 无法登记（原始隔离区）
    if not sample_id:
        return _as_origin(outcome, "missing_sample_id",
                          "记录缺少 sample_id，无法登记为可加工对象")
    if obj.get("invalid_entry"):
        return _as_origin(outcome, "file_entry_not_object",
                          "files 成员不是对象，无法解析文件登记项")

    link = obj.get("file_link")
    if not link:
        return _as_return(outcome, "missing_file_link", missing=["file_link"])

    # 2) 链接必须可搬运（拒绝绝对路径/盘符/反斜杠/目录穿越）
    try:
        info = classify_link(link)
    except ValueError as exc:
        return _as_origin(outcome, "link_not_transportable",
                          "文件链接不可搬运：%s" % exc)
    outcome.file_link = info["value"]

    # 3) 格式白名单（表1 冻结，不允许手填别的格式）
    ext = extension_of(link)
    outcome.format_ext = ext or None
    modality = infer_modality(ext)
    if modality is None:
        return _as_origin(outcome, "format_not_allowed",
                          "扩展名 .%s 不在表1 白名单" % (ext or "?"))
    outcome.modality = modality

    declared = clean_text(obj.get("declared_modality")).lower()
    if declared and declared != modality:
        return _as_return(outcome, "modality_mismatch",
                          "声明模态 %s 与扩展名推断 %s 不一致" % (declared, modality))

    # 4) 本地文件可读性（按 file_check_mode；原始文件问题优先）
    state, reason_code, reason = check_local_file(
        ext=ext, link_info=info, local_dirs=list(local_dirs or []),
        mode=clean_text(business.get("file_check_mode")) or FILE_CHECK_MODE_HEADER)
    outcome.file_check = state
    if state == "rejected":
        return _as_origin(outcome, reason_code, reason)

    # 5) 授权：只有「可训练」可以进入加工
    license_state = normalize_license(
        record.get("license", record.get("authorization",
                                         record.get("license_conclusion"))))
    outcome.license_state = license_state
    if license_state != LICENSE_TRAINABLE:
        return _as_sample(outcome, _LICENSE_REASON.get(license_state,
                                                       "license_unknown"))

    # 6) 来源与模态一致性（表1 未明确要求，默认关闭；开启时按产线模态校验）
    source = normalize_source(record.get("source"))
    outcome.source = source
    if business.get("check_source_modality") and source in SOURCE_MODALITIES:
        allowed = SOURCE_MODALITIES.get(source)
        if allowed and modality not in allowed:
            return _as_return(outcome, "source_modality_mismatch",
                              "来源 %s 只登记 %s，实际为 %s"
                              % (source_label(source), "/".join(allowed), modality))

    # 7) 登记要素齐全
    missing: List[str] = []
    if source is None:
        missing.append("source")
    if not clean_text(record.get("source_id")):
        missing.append("source_id")
    if not clean_text(record.get("batch_id")):
        missing.append("batch_id")
    if not clean_text(record.get("upstream_version")):
        missing.append("upstream_version")
    if missing:
        return _as_return(outcome, "incomplete_registration",
                          "缺少登记要素：%s" % "、".join(missing), missing=missing)

    # 8) 通过：写工作集，只保存原文件链接
    outcome.decision = DECISION_REGISTERED
    outcome.reason_code = DECISION_REGISTERED
    outcome.reason = "校验通过，写入工作集（仅登记原文件链接）"
    outcome.label = LABEL_REGISTERED
    outcome.action = ACTION_REGISTER
    outcome.destination = DESTINATION_WORKSET
    return outcome


def mark_duplicate(outcome: Outcome) -> Outcome:
    """把通过项标记为重复登记（退回补登记，不做第二次登记）。"""
    return _as_return(outcome, "duplicate_registration",
                      "同一 sample_id + 文件链接重复登记，已保留首次登记")


def registration_key(outcome: Outcome) -> Tuple[str, str, str]:
    """重复登记判定键：``sample_id + 归一化链接 + 模态``。"""
    return (outcome.sample_id, clean_text(outcome.file_link).lower(),
            outcome.modality or "")


# --------------------------------------------------------------------------- #
# 台账构造
# --------------------------------------------------------------------------- #

def _common_entry(outcome: Outcome, *, unique_id: str, run_id: str,
                  lineno: int) -> Dict[str, Any]:
    return {
        "stage": STAGE_ID,
        "stage_label": STAGE_LABEL,
        "operator": OPERATOR_NAME,
        "operator_number": FORMAL_OPERATOR_NUMBER,
        "operator_version": OPERATOR_VERSION,
        "uniqueId": unique_id,
        "runId": run_id,
        "lineno": lineno,
        "sample_id": outcome.sample_id,
        "label": outcome.label,
        "action": outcome.action,
        "destination": outcome.destination,
        "reason_code": outcome.reason_code,
        "reason": outcome.reason,
    }


def build_workitem(outcome: Outcome, *, unique_id: str, run_id: str,
                   lineno: int) -> Dict[str, Any]:
    """主输出（可加工对象清单）：透传上游字段，只追加本算子命名空间。"""
    record: Dict[str, Any] = dict(outcome.record or {})
    record["operator_01"] = {
        "operator": OPERATOR_NAME,
        "operator_label": OPERATOR_LABEL,
        "operator_number": FORMAL_OPERATOR_NUMBER,
        "operator_version": OPERATOR_VERSION,
        "stage": STAGE_ID,
        "stage_label": STAGE_LABEL,
        "sample_id": outcome.sample_id,
        "lineno": lineno,
        "source": outcome.source,
        "source_label": source_label(outcome.source),
        "source_id": clean_text(record.get("source_id")),
        "batch_id": clean_text(record.get("batch_id")),
        "upstream_version": clean_text(record.get("upstream_version")),
        "license": ALLOWED_LICENSE_LABEL,
        "modality": outcome.modality,
        "format_ext": outcome.format_ext,
        "file_link": outcome.file_link,
        "file_check": outcome.file_check,
        "size_bytes": outcome.size_bytes,
        "media_policy": "link_only_no_copy",
        # [v4.2] status 双字段：int status + 原字符串迁入 process_status
        "status": status_int(DECISION_REGISTERED),
        "process_status": DECISION_REGISTERED,
        "label": outcome.label,
        "action": outcome.action,
        "destination": outcome.destination,
        "registered": True,
        "uniqueId": unique_id,
        "runId": run_id,
    }
    existing = record.get("processing")
    processing: Dict[str, Any] = dict(existing) if isinstance(existing, dict) else {}
    processing["01"] = {
        # [v4.2] status 双字段：int status + 原字符串迁入 process_status
        "status": status_int(DECISION_REGISTERED),
        "process_status": DECISION_REGISTERED,
        "label": outcome.label,
        "action": outcome.action,
        "destination": outcome.destination,
        "source": outcome.source,
        "batch_id": clean_text(record.get("batch_id")),
        "license": ALLOWED_LICENSE_LABEL,
        "modality": outcome.modality,
        "reg_format": outcome.format_ext,
        "file_check": outcome.file_check,
    }
    record["processing"] = processing
    return record


def build_ledger(outcome: Outcome, *, unique_id: str, run_id: str,
                 lineno: int) -> Dict[str, Any]:
    """旁路台账行（原始隔离 / 样本隔离 / 待补登记）。"""
    record = outcome.record or {}
    entry = _common_entry(outcome, unique_id=unique_id, run_id=run_id, lineno=lineno)
    entry.update({
        "role": outcome.decision,
        "source": outcome.source,
        "source_raw": clean_text(record.get("source")) or None,
        "source_id": clean_text(record.get("source_id")) or None,
        "batch_id": clean_text(record.get("batch_id")) or None,
        "upstream_version": clean_text(record.get("upstream_version")) or None,
        "license_state": outcome.license_state or None,
        "file_link": outcome.file_link,
        "modality": outcome.modality,
        "declared_modality": outcome.declared_modality,
        "format_ext": outcome.format_ext,
        "file_check": outcome.file_check,
        "file_index": outcome.file_index,
    })
    if outcome.missing_fields:
        entry["missing_fields"] = list(outcome.missing_fields)
    entry["original_record"] = record
    return entry


def build_entry(outcome: Outcome, *, business: Dict[str, Any], unique_id: str,
                run_id: str, lineno: int, role: str) -> Dict[str, Any]:
    """统一入口：``role='workitem'`` 走主输出，其余走旁路台账。"""
    if role == "workitem":
        return build_workitem(outcome, unique_id=unique_id, run_id=run_id,
                              lineno=lineno)
    return build_ledger(outcome, unique_id=unique_id, run_id=run_id, lineno=lineno)


def build_reject_entry(lineno: int, reason_code: str, raw_line: Any,
                       business: Dict[str, Any], *, unique_id: str,
                       run_id: str) -> Dict[str, Any]:
    """损坏行/非对象/编码错误/超长行的拒绝台账行（只留受限预览）。"""
    preview = _preview(raw_line)
    return {
        "stage": STAGE_ID,
        "stage_label": STAGE_LABEL,
        "operator": OPERATOR_NAME,
        "operator_number": FORMAL_OPERATOR_NUMBER,
        "operator_version": OPERATOR_VERSION,
        "uniqueId": unique_id,
        "runId": run_id,
        "lineno": lineno,
        "role": DECISION_ORIGIN,
        "sample_id": None,
        "label": LABEL_ORIGIN,
        "action": ACTION_ISOLATE,
        "destination": DESTINATION_ORIGIN,
        "reason_code": reason_code,
        "reason": REASON_TEXT.get(reason_code, reason_code),
        "preview": preview,
        "preview_truncated": len(raw_line if isinstance(raw_line, str) else "")
        > MAX_PREVIEW_CHARS,
    }


def build_batch_registry(outcomes: List[Outcome], *, business: Dict[str, Any],
                         unique_id: str, run_id: str, statistics: Dict[str, Any],
                         generated_at: str) -> Dict[str, Any]:
    """批次台账：按（来源, 批次号）汇总登记与隔离情况。"""
    groups: Dict[Tuple[str, str], Dict[str, Any]] = {}
    for outcome in outcomes:
        record = outcome.record or {}
        source = outcome.source or normalize_source(record.get("source")) or "unknown"
        batch_id = clean_text(record.get("batch_id")) or "unknown"
        key = (source, batch_id)
        group = groups.get(key)
        if group is None:
            group = {
                "source": source,
                "source_label": source_label(source) if source in SOURCES else "未登记来源",
                "source_id": clean_text(record.get("source_id")) or None,
                "batch_id": batch_id,
                "upstream_version": clean_text(record.get("upstream_version")) or None,
                "license": ALLOWED_LICENSE_LABEL,
                "registered": 0,
                "origin_quarantined": 0,
                "sample_quarantined": 0,
                "returned": 0,
                "modalities": {},
                "file_links": [],
            }
            groups[key] = group
        field = _DECISION_KEY[outcome.decision]
        group[field] = int(group.get(field, 0)) + 1
        if outcome.decision == DECISION_REGISTERED:
            modality = outcome.modality or "unknown"
            group["modalities"][modality] = int(
                group["modalities"].get(modality, 0)) + 1
            group["file_links"].append(outcome.file_link)

    batches = sorted(groups.values(), key=lambda item: (item["source"], item["batch_id"]))
    for batch in batches:
        batch["object_count"] = sum(int(batch.get(name, 0))
                                    for name in _DECISION_KEYS)
    return {
        "format": REGISTRY_FORMAT,
        "operator": OPERATOR_NAME,
        "operator_label": OPERATOR_LABEL,
        "operator_number": FORMAL_OPERATOR_NUMBER,
        "operator_version": OPERATOR_VERSION,
        "stageId": STAGE_ID,
        "uniqueId": unique_id,
        "runId": run_id,
        "generatedAt": generated_at,
        "entrypoint": MAIN_NAME,
        "enabled": bool(business.get("enabled", True)),
        "file_check_mode": business.get("file_check_mode"),
        "batches": batches,
        "totals": {
            "batches": len(batches),
            "objects": statistics.get("objects", 0),
            "registered": statistics.get("registered", 0),
            "origin_quarantined": statistics.get("origin_quarantined", 0),
            "sample_quarantined": statistics.get("sample_quarantined", 0),
            "returned": statistics.get("returned", 0),
            "rejected": statistics.get("rejected", 0),
        },
    }


# --------------------------------------------------------------------------- #
# 统计与守恒
# --------------------------------------------------------------------------- #

_REASON_BUCKET = {
    DECISION_ORIGIN: "origin_reasons",
    DECISION_SAMPLE: "sample_reasons",
    DECISION_RETURN: "return_reasons",
}

#: 判定结果 → statistics 计数键（decision 值不带 d，统计键为过去式，必须显式映射）
_DECISION_KEY = {
    DECISION_REGISTERED: "registered",
    DECISION_ORIGIN: "origin_quarantined",
    DECISION_SAMPLE: "sample_quarantined",
    DECISION_RETURN: "returned",
}
#: 四路统计键（守恒校验与批次台账共用，顺序固定）
_DECISION_KEYS: Tuple[str, ...] = tuple(_DECISION_KEY[item] for item in (
    DECISION_REGISTERED, DECISION_ORIGIN, DECISION_SAMPLE, DECISION_RETURN))


def empty_statistics() -> Dict[str, Any]:
    return {
        "input_lines": 0,
        "blank_lines": 0,
        "input": 0,
        "rejected": 0,
        "valid": 0,
        "objects": 0,
        "registered": 0,
        "origin_quarantined": 0,
        "sample_quarantined": 0,
        "returned": 0,
        "duplicates": 0,
        "link_only": 0,
        "local_verified": 0,
        "batches": 0,
        "records_without_files": 0,
        "records_with_multiple_files": 0,
        "by_source": {},
        "by_modality": {},
        "by_format": {},
        "license_counts": {},
        "origin_reasons": {},
        "sample_reasons": {},
        "return_reasons": {},
        "reject_reasons": {},
        "enabled": True,
        "file_check_mode": FILE_CHECK_MODE_HEADER,
        "empty_input": True,
        "conservation_ok": True,
        "conservation_failures": [],
        "report_status": "empty",
    }


def _bump(mapping: Dict[str, int], key: Any) -> None:
    name = str(key if key not in (None, "") else "unknown")
    mapping[name] = int(mapping.get(name, 0)) + 1


def accumulate(stats: Dict[str, Any], outcome: Outcome) -> None:
    """把一个判定结果累加进真实统计（每个登记项恰好计入一次）。

    只累加**登记项级**计数：``objects``、四路分流、来源/模态/格式分布、
    原因码、授权与文件检查状态。``input`` / ``valid`` / ``rejected`` /
    ``blank_lines`` / ``input_lines`` 是**行级**计数，由调用方按物理行统计
    （``input = valid + rejected`` 与 ``objects = 四路之和`` 一起构成守恒校验）。
    """
    stats["objects"] = int(stats.get("objects", 0)) + 1
    key = _DECISION_KEY[outcome.decision]
    stats[key] = int(stats.get(key, 0)) + 1
    if outcome.decision == DECISION_REGISTERED:
        _bump(stats["by_source"], outcome.source)
        _bump(stats["by_modality"], outcome.modality)
        _bump(stats["by_format"], outcome.format_ext)
    bucket = _REASON_BUCKET.get(outcome.decision)
    if bucket and outcome.reason_code:
        _bump(stats[bucket], outcome.reason_code)
    if outcome.license_state:
        _bump(stats["license_counts"], outcome.license_state)
    if outcome.file_check == FILE_CHECK_LINK_ONLY:
        stats["link_only"] = int(stats.get("link_only", 0)) + 1
    elif outcome.file_check == FILE_CHECK_LOCAL_VERIFIED:
        stats["local_verified"] = int(stats.get("local_verified", 0)) + 1


def note_reject(stats: Dict[str, Any], reason_code: str) -> None:
    """登记一个被拒绝的物理行（坏行/非对象/编码错误/超长行）。

    被拒绝的行不进入 ``objects`` 守恒（那是登记项级计数），
    只计入 ``rejected`` 与 ``reject_reasons``，且它们的台账行写入原始隔离记录文件。
    """
    stats["rejected"] = int(stats.get("rejected", 0)) + 1
    _bump(stats.setdefault("reject_reasons", {}), reason_code)


def conservation_failures(stats: Dict[str, Any]) -> List[str]:
    """统计守恒校验；返回不成立项（正常为空数组）。"""
    failures: List[str] = []

    def expect(expression: str, left: Any, right: Any) -> None:
        if int(left) != int(right):
            failures.append("%s（%s != %s）" % (expression, left, right))

    expect("input_lines = input + blank_lines",
           stats.get("input_lines", 0),
           int(stats.get("input", 0)) + int(stats.get("blank_lines", 0)))
    expect("input = valid + rejected",
           stats.get("input", 0),
           int(stats.get("valid", 0)) + int(stats.get("rejected", 0)))
    expect("objects = registered + origin + sample + returned",
           stats.get("objects", 0),
           sum(int(stats.get(name, 0)) for name in _DECISION_KEYS))
    expect("sum(by_source) = registered", sum(
        int(value) for value in (stats.get("by_source") or {}).values()),
        stats.get("registered", 0))
    expect("sum(by_modality) = registered", sum(
        int(value) for value in (stats.get("by_modality") or {}).values()),
        stats.get("registered", 0))
    return failures


def derive_status(stats: Dict[str, Any], business: Dict[str, Any]) -> str:
    """运行状态：``disabled / empty / failed / partial / success``。"""
    if not business.get("enabled", True):
        return "disabled"
    if int(stats.get("input", 0)) == 0:
        return "empty"
    if conservation_failures(stats):
        return "failed"
    if (int(stats.get("origin_quarantined", 0))
            or int(stats.get("sample_quarantined", 0))
            or int(stats.get("returned", 0))):
        return "partial"
    return "success"
