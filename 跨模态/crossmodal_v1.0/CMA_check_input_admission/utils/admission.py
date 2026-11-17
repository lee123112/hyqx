"""算子 10 业务模块：核对输入对象准入（纯规则，无模型、无权重、不联网）。

表1 #10 职责（“研发测关键技术” + “质量不过关拦截处理”）：
1. 至少两种模态（默认 2，可选 2–4）；
2. 描述非空、来自候选描述而不是文件名/标签（#9 的输出）；
3. 授权为“可训练”；
4. 来源 ID、批次号、文件都能打开。
任一项失败按表1 质量三场景处置：
- 文件损坏/无法解析/空文件 → 原始隔离区（label 形如 ``原始文件-损坏``）；
- 授权不可训练或无法确认 → 样本隔离库（``样本-敏感``）；
- 模态不足 → 样本隔离库（``样本-语义不通``）；
- 尚无有效描述 → 不拦、退回 #9（``流程-待补描述``）；退回次数超限才锁定进样本隔离库。

边界（表1 明确禁止越界）：
- **不重做单模态画质评分**：只判断“文件能否打开”，不做清晰度/分辨率/信噪比评分；
- 不做配对、不打 CLIP 分、不加载任何模型；
- 不修改 ``alignment`` / ``qc`` 及上游 processing 命名空间，只追加 ``admission`` 与 ``processing["10"]``。

降级/失败语义：
- ``file_check_mode=header``（默认）：纯 Python 头签名 + 空文件 + 扩展名白名单，不依赖第三方库；
- ``file_check_mode=decode``：叠加真实解码（Pillow/OpenCV/标准库 wave）；
  解码器缺失且 ``allow_degraded=false`` → 结构化失败（``DependencyError``），不伪造“已解码”；
  允许降级时该文件标记 ``status=header_only``，运行状态标 ``degraded``。
"""
from __future__ import annotations

import copy
import importlib.util
import json
from pathlib import Path
from typing import Any, Dict, List, Optional

from utils import io as io_utils
from utils.io import UnsupportedMediaPath
from utils.schemas import MODALITY_EXTENSIONS, TEXT_EXTENSIONS

#: 血缘/映射里的算子身份（demo ID，不是甲方最终模态编码）
OPERATOR = "input_admission"
FORMAL_OPERATOR_NUMBER = 10

# ---------------------------------------------------------------- 台账口径
#: 表1 质量标签（原始文件问题 → 原始隔离区；业务问题 → 样本隔离库）
LABEL_ORIGIN_BROKEN = "原始文件-损坏"
LABEL_ORIGIN_EMPTY = "原始文件-空"
LABEL_ORIGIN_UNPARSABLE = "原始文件-无法解析"
LABEL_SAMPLE_SENSITIVE = "样本-敏感"
LABEL_SAMPLE_SEMANTIC = "样本-语义不通"
#: 流程状态（不隔离）：缺描述/缺登记属于待补，不得误写成低质量隔离
LABEL_FLOW_PENDING_DESCRIPTION = "流程-待补描述"
LABEL_FLOW_PENDING_REGISTRATION = "流程-待补登记"

DEST_ORIGIN = "原始隔离区"
DEST_SAMPLE = "样本隔离库"
DEST_CANDIDATE_DESCRIPTION = "09_准备候选描述"
DEST_BATCH_INTAKE = "01_接入多模态批次"

ACTION_ADMIT = "admit"
ACTION_RETURN = "return"
ACTION_ISOLATE = "isolate"
ACTION_LOCK = "lock"

#: evaluate_record 的四类结果
DECISION_ADMIT = "admit"
DECISION_RETURN = "return"
DECISION_ORIGIN = "origin_quarantine"
DECISION_SAMPLE = "sample_quarantine"

#: 描述字段不允许的来源写法（表1 #9：文件名和标签不能当描述）
FORBIDDEN_DESCRIPTION_SOURCES = {
    "文件名", "文件名称", "filename", "file_name", "file-name", "file", "path", "路径",
    "标签", "分类标签", "tag", "tags", "label",
}

#: 来源 ID / 批次号的候选字段名
SOURCE_ID_KEYS = ("source_id", "sourceId", "来源ID", "source")
BATCH_ID_KEYS = ("batch_id", "batchId", "批次号", "batch")

#: 授权结论的候选字段名
LICENSE_KEYS = ("license", "authorization", "授权结论", "authorization_status", "license_status")

#: 媒体引用可选用的内联对象字段
_PATH_KEYS = ("path", "file", "file_path", "uri")

#: 头签名读取字节数
HEADER_BYTES = 16


class DependencyError(RuntimeError):
    """必需解码器缺失且不允许降级。"""


# ---------------------------------------------------------------- 头签名
def _sig_png(data: bytes) -> bool:
    return data.startswith(b"\x89PNG\r\n\x1a\n")


def _sig_jpeg(data: bytes) -> bool:
    return data.startswith(b"\xff\xd8\xff")


def _sig_webp(data: bytes) -> bool:
    return data[:4] == b"RIFF" and data[8:12] == b"WEBP"


def _sig_bmp(data: bytes) -> bool:
    return data.startswith(b"BM")


def _sig_gif(data: bytes) -> bool:
    return data.startswith(b"GIF8")


def _sig_ftyp(data: bytes) -> bool:
    return data[4:8] == b"ftyp"


def _sig_avi(data: bytes) -> bool:
    return data[:4] == b"RIFF" and data[8:12] in (b"AVI ", b"AVIX")


def _sig_mkv(data: bytes) -> bool:
    return data.startswith(b"\x1a\x45\xdf\xa3")


def _sig_wav(data: bytes) -> bool:
    return data[:4] == b"RIFF" and data[8:12] == b"WAVE"


def _sig_mp3(data: bytes) -> bool:
    if data.startswith(b"ID3"):
        return True
    return len(data) > 1 and data[0] == 0xFF and (data[1] & 0xE0) == 0xE0


def _sig_flac(data: bytes) -> bool:
    return data.startswith(b"fLaC")


def _sig_aiff(data: bytes) -> bool:
    return data[:4] == b"FORM" and data[8:12] in (b"AIFF", b"AIFC")


#: 扩展名 → 头签名校验（未列出的扩展名按“无签名”处理，例如纯文本）
SIGNATURES = {
    "png": _sig_png,
    "jpg": _sig_jpeg, "jpeg": _sig_jpeg,
    "webp": _sig_webp, "bmp": _sig_bmp, "gif": _sig_gif,
    "mp4": _sig_ftyp, "mov": _sig_ftyp, "m4a": _sig_ftyp,
    "avi": _sig_avi, "mkv": _sig_mkv,
    "wav": _sig_wav, "mp3": _sig_mp3, "flac": _sig_flac,
    "aiff": _sig_aiff, "aif": _sig_aiff,
}


def available_backends() -> Dict[str, Any]:
    """探测可选解码后端；不联网、不加载权重。"""
    pillow = importlib.util.find_spec("PIL") is not None
    opencv = importlib.util.find_spec("cv2") is not None
    missing: List[str] = []
    if not pillow:
        missing.append("pillow")
    if not opencv:
        missing.append("opencv")
    return {
        "pillow": pillow,
        "opencv": opencv,
        "wave": True,
        "missing": missing,
        "image_decoder": "pillow" if pillow else None,
        "video_decoder": "opencv" if opencv else None,
        "audio_decoder": "wave(wav only)",
        "text_decoder": "utf-8",
    }


# ---------------------------------------------------------------- 字段提取
def media_reference(record: dict, name: str) -> Optional[str]:
    """取出模态字段引用的相对媒体路径；无该模态返回 None。"""
    value = record.get(name)
    if isinstance(value, dict):
        for key in _PATH_KEYS:
            candidate = value.get(key)
            if isinstance(candidate, str) and candidate.strip():
                return candidate.strip()
        return None
    if isinstance(value, str) and value.strip():
        return value.strip()
    return None


def present_modalities(record: dict) -> List[str]:
    """统计记录实际具备的模态（顺序固定 text/image/video/audio）。"""
    found: List[str] = []
    text = record.get("text")
    content = text.get("content") if isinstance(text, dict) else text
    if isinstance(content, str) and content.strip():
        found.append("text")
    for name in ("image", "video", "audio"):
        if media_reference(record, name):
            found.append(name)
    return found


def extract_description(record: dict) -> Dict[str, Any]:
    """按 #9 的输出约定取出候选描述与来源。"""
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

    return {
        "value": value if isinstance(value, str) else "",
        "source": source if isinstance(source, str) else None,
    }


def _first_string(record: dict, keys) -> Optional[str]:
    metadata = record.get("metadata") if isinstance(record.get("metadata"), dict) else {}
    for key in keys:
        for container in (metadata, record):
            if not isinstance(container, dict):
                continue
            value = container.get(key)
            if isinstance(value, str) and value.strip():
                return value.strip()
    return None


def _license_value(record: dict) -> Any:
    metadata = record.get("metadata") if isinstance(record.get("metadata"), dict) else {}
    for key in LICENSE_KEYS:
        for container in (metadata, record):
            if not isinstance(container, dict):
                continue
            if key in container and container[key] is not None:
                value = container[key]
                if isinstance(value, str) and not value.strip():
                    continue
                return value
    return None


def _is_trainable_license(value: Any, trainable: List[str]) -> bool:
    if isinstance(value, bool):
        return value
    if not isinstance(value, str):
        return False
    candidate = value.strip()
    if candidate in trainable:
        return True
    return candidate.lower() in {item.lower() for item in trainable}


def retry_count(record: dict) -> int:
    """读取样本已退回次数（#21 全产线台账口径；缺失按 0）。"""
    candidates: List[Any] = []
    metadata = record.get("metadata") if isinstance(record.get("metadata"), dict) else {}
    candidates.append(metadata.get("retry_count"))
    candidates.append(record.get("retry_count"))
    processing = record.get("processing") if isinstance(record.get("processing"), dict) else {}
    for key in ("10", "09"):
        node = processing.get(key)
        if isinstance(node, dict):
            candidates.append(node.get("retry_count"))
    for value in candidates:
        if isinstance(value, int) and not isinstance(value, bool) and value >= 0:
            return value
    return 0


def validate_description(description: Dict[str, Any], record: dict,
                         business: Dict[str, Any]) -> Optional[str]:
    """返回失败原因码；通过返回 None。

    检查顺序（先结构性问题，再长度）：
    缺失 → 来源非法（文件名/标签）→ 等于媒体文件名 → 等于标签 → 过短 → 过长。
    """
    value = (description.get("value") or "").strip()
    if not value:
        return "description_missing"

    source = description.get("source")
    if isinstance(source, str) and source.strip().lower() in FORBIDDEN_DESCRIPTION_SOURCES:
        return "description_source_invalid"

    collapsed = " ".join(value.split())
    names = set()
    for name in ("image", "video", "audio"):
        reference = media_reference(record, name)
        if reference:
            path = Path(reference)
            names.add(path.name)
            names.add(path.stem)
    if value in names:
        return "description_is_filename"

    metadata = record.get("metadata") if isinstance(record.get("metadata"), dict) else {}
    tags = metadata.get("image_tags") or []
    if isinstance(tags, (list, tuple)):
        normalized = {str(tag).strip() for tag in tags if str(tag).strip()}
        if value in normalized:
            return "description_is_tag"

    if len(collapsed) < int(business["min_description_chars"]):
        return "description_too_short"
    if len(collapsed) > int(business["max_description_chars"]):
        return "description_too_long"
    return None


# ---------------------------------------------------------------- 文件可打开
def check_file(media_root: Path, relative: str, modality: str,
               business: Dict[str, Any], backends: Dict[str, Any]) -> Dict[str, Any]:
    """核对单个媒体引用“能否打开”，不做任何画质评分。

    返回报告 dict；``ok=False`` 时带 ``reason`` 与表1 ``label``。
    """
    report: Dict[str, Any] = {
        "modality": modality,
        "path": relative,
        "ext": "",
        "bytes": 0,
        "mode": business["file_check_mode"],
        # [v4.2] status 双字段：int status（1=可打开/已检查，0=失败）+ 原字符串迁入 process_status
        "status": 1,
        "process_status": "checked",
        "ok": True,
        "reason": None,
        "label": None,
        "checker": "signature",
    }

    try:
        target = io_utils.resolve_media(media_root, relative)
    except UnsupportedMediaPath as exc:
        return _fail(report, "media_path_invalid", LABEL_ORIGIN_UNPARSABLE, str(exc))

    extension = target.suffix.lower().lstrip(".")
    report["ext"] = extension
    allowed = {str(item).lower() for item in business["allowed_extensions"]}
    modality_allowed = {str(item).lower() for item in MODALITY_EXTENSIONS.get(modality, ())}
    if extension not in allowed or extension not in modality_allowed:
        return _fail(report, "format_not_allowed", LABEL_ORIGIN_UNPARSABLE,
                     "%s 字段不允许的格式：%s" % (modality, extension or "<无扩展名>"))

    if not target.exists() or not target.is_file():
        return _fail(report, "file_missing", LABEL_ORIGIN_BROKEN, "文件不存在或不是普通文件")

    size = target.stat().st_size
    report["bytes"] = size
    if size == 0:
        return _fail(report, "file_empty", LABEL_ORIGIN_EMPTY, "文件为空")

    try:
        with open(target, "rb") as stream:
            head = stream.read(HEADER_BYTES)
    except OSError as exc:
        return _fail(report, "file_unreadable", LABEL_ORIGIN_BROKEN,
                     "文件无法读取：%s" % type(exc).__name__)

    signature = SIGNATURES.get(extension)
    if signature is not None and not signature(head):
        return _fail(report, "file_signature_mismatch", LABEL_ORIGIN_BROKEN,
                     "文件头与扩展名不符（疑似损坏或格式不符）")

    if extension in TEXT_EXTENSIONS:
        try:
            with open(target, "rb") as stream:
                stream.read().decode("utf-8")
        except UnicodeDecodeError:
            return _fail(report, "text_decode_failed", LABEL_ORIGIN_UNPARSABLE,
                         "文本不是严格 UTF-8")

    if business["file_check_mode"] == "decode":
        outcome = _decode_file(target, extension, modality, backends)
        if outcome == "failed":
            return _fail(report, "decode_failed", LABEL_ORIGIN_BROKEN,
                         "解码失败（文件损坏或截断）")
        if outcome == "unavailable":
            if not business["allow_degraded"]:
                raise DependencyError(
                    "解码器缺失且禁止降级（allow_degraded=false）：%s 需要 %s 解码器"
                    % (extension, modality))
            report["status"] = 1
            report["process_status"] = "header_only"
            report["checker"] = "signature"
            report["degraded"] = True
        else:
            report["status"] = 1
            report["process_status"] = "decoded"
            report["checker"] = _decoder_name(modality, extension, backends)
            report["degraded"] = False
    return report


def _decoder_name(modality: str, extension: str, backends: Dict[str, Any]) -> str:
    if modality == "image":
        return backends.get("image_decoder") or "unknown"
    if modality == "video":
        return backends.get("video_decoder") or "unknown"
    if modality == "audio":
        return "wave" if extension == "wav" else "unknown"
    return "utf-8"


def _decode_file(target: Path, extension: str, modality: str,
                 backends: Dict[str, Any]) -> str:
    """真实解码校验：返回 ``ok`` / ``failed`` / ``unavailable``。"""
    if extension in TEXT_EXTENSIONS:
        return "ok"
    if modality == "image":
        if not backends.get("pillow"):
            return "unavailable"
        try:
            from PIL import Image

            with Image.open(str(target)) as image:
                image.verify()
        except Exception:  # noqa: BLE001 - 任何解码异常都视为损坏
            return "failed"
        return "ok"
    if modality == "video":
        if not backends.get("opencv"):
            return "unavailable"
        try:
            import cv2

            capture = cv2.VideoCapture(str(target))
            try:
                opened = bool(capture.isOpened())
                read_ok = bool(capture.read()[0]) if opened else False
            finally:
                capture.release()
        except Exception:  # noqa: BLE001
            return "failed"
        return "ok" if (opened and read_ok) else "failed"
    if modality == "audio":
        if extension != "wav":
            # mp3/flac/aiff/m4a 没有内置解码器：不声称已解码
            return "unavailable"
        try:
            import wave

            with wave.open(str(target), "rb") as handle:
                handle.readframes(1)
        except Exception:  # noqa: BLE001
            return "failed"
        return "ok"
    return "unavailable"


def _fail(report: Dict[str, Any], reason: str, label: str, message: str) -> Dict[str, Any]:
    # [v4.2] status 双字段：int status（0=文件不可打开）+ 原字符串迁入 process_status
    report.update({"ok": False, "reason": reason, "label": label, "status": 0,
                   "process_status": "failed", "message": message})
    return report


# ---------------------------------------------------------------- 台账条目
def make_ledger_entry(*, operator: str, stage_id: str, sample_id: Optional[str], line: int,
                      label: str, action: str, destination: str, reason: str, message: str,
                      event_time: str, batch_id: Optional[str] = None, retry_count_: int = 0,
                      locked: bool = False, record: Optional[dict] = None,
                      evidence: Optional[dict] = None) -> Dict[str, Any]:
    """统一台账字段：label/stage/operator/action/destination/event_time/batch_id/retry_count。"""
    entry: Dict[str, Any] = {
        "sample_id": sample_id,
        "line": line,
        "label": label,
        "operator": operator,
        "formalOperatorNumber": FORMAL_OPERATOR_NUMBER,
        "stage": stage_id,
        "action": action,
        "destination": destination,
        "reason": reason,
        "message": message,
        "event_time": event_time,
        "batch_id": batch_id,
        "retry_count": retry_count_,
        "locked": locked,
    }
    if evidence:
        entry["evidence"] = evidence
    if record is not None:
        entry["record"] = record
    return entry


def _bump(counter: Dict[str, Any], key: str, amount: int = 1) -> None:
    counter[key] = counter.get(key, 0) + amount


# ---------------------------------------------------------------- 主判定
def evaluate_record(record: dict, lineno: int, media_root: Path, business: Dict[str, Any],
                    stats: Dict[str, Any], seen_ids: set,
                    event_time: str, backends: Dict[str, Any]) -> Dict[str, Any]:
    """对一条有效记录执行准入核对，返回 ``{"action", "record", "entry", "reason"}``。

    判定优先级（前项不通过即返回，保证统计唯一归口）：
    结构(sample_id) → 文件可打开 → 来源/批次登记 → 描述 → 授权 → 模态数 → 通过。
    """
    operator = business.get("operator", OPERATOR)
    stage_id = business.get("stage_id", "S3")
    sample_id = record.get("sample_id")
    if not isinstance(sample_id, str) or not sample_id.strip():
        _bump(stats["origin_reasons"], "missing_sample_id")
        return _decision(DECISION_ORIGIN, None, make_ledger_entry(
            operator=operator, stage_id=stage_id, sample_id=None, line=lineno,
            label=LABEL_ORIGIN_UNPARSABLE, action=ACTION_ISOLATE, destination=DEST_ORIGIN,
            reason="missing_sample_id", message="记录缺少可追溯的 sample_id",
            event_time=event_time), "missing_sample_id")
    sample_id = sample_id.strip()

    duplicate = sample_id in seen_ids
    seen_ids.add(sample_id)
    if duplicate:
        stats["duplicates"] += 1

    modalities = present_modalities(record)
    _bump(stats["modality_counts"], str(len(modalities)))
    _bump(stats["modality_pairs"], "+".join(sorted(modalities)) or "none")

    source_id = _first_string(record, SOURCE_ID_KEYS)
    batch_id = _first_string(record, BATCH_ID_KEYS)
    license_value = _license_value(record)
    attempts = retry_count(record)
    metadata = record.get("metadata") if isinstance(record.get("metadata"), dict) else {}

    def origin(reason: str, label: str, message: str, evidence: Optional[dict] = None):
        _bump(stats["origin_reasons"], reason)
        return _decision(DECISION_ORIGIN, None, make_ledger_entry(
            operator=operator, stage_id=stage_id, sample_id=sample_id, line=lineno,
            label=label, action=ACTION_ISOLATE, destination=DEST_ORIGIN, reason=reason,
            message=message, event_time=event_time, batch_id=batch_id,
            retry_count_=attempts, record=record, evidence=evidence), reason)

    def sample_quarantine(reason: str, label: str, message: str, locked: bool = False,
                          evidence: Optional[dict] = None):
        _bump(stats["sample_reasons"], reason)
        if locked:
            stats["locked"] += 1
        return _decision(DECISION_SAMPLE, None, make_ledger_entry(
            operator=operator, stage_id=stage_id, sample_id=sample_id, line=lineno,
            label=label, action=ACTION_LOCK if locked else ACTION_ISOLATE,
            destination=DEST_SAMPLE, reason=reason, message=message,
            event_time=event_time, batch_id=batch_id, retry_count_=attempts,
            locked=locked, record=record, evidence=evidence), reason)

    def returned(reason: str, destination: str, label: str, message: str):
        _bump(stats["return_reasons"], reason)
        if destination == DEST_CANDIDATE_DESCRIPTION:
            stats["returned_description"] += 1
        else:
            stats["returned_registration"] += 1
        return _decision(DECISION_RETURN, None, make_ledger_entry(
            operator=operator, stage_id=stage_id, sample_id=sample_id, line=lineno,
            label=label, action=ACTION_RETURN, destination=destination, reason=reason,
            message=message, event_time=event_time, batch_id=batch_id,
            retry_count_=attempts, record=record), reason)

    # 1) 文件可打开（原始文件问题优先）
    file_reports: List[Dict[str, Any]] = []
    for name in ("image", "video", "audio"):
        reference = media_reference(record, name)
        if not reference:
            continue
        report = check_file(media_root, reference, name, business, backends)
        file_reports.append(report)
        if not report["ok"]:
            if report["reason"] == "file_missing":
                stats["files_missing"] += 1
            else:
                stats["files_broken"] += 1
            return origin(report["reason"], report["label"],
                          "%s：%s" % (name, report.get("message", "文件不可用")),
                          evidence={"file": report})
        stats["files_checked"] += 1
        if report.get("degraded"):
            stats["degraded_mode"] = True

    # 2) 来源 ID / 批次号登记
    if business["require_source_and_batch"]:
        if not source_id:
            return returned("missing_source_id", DEST_BATCH_INTAKE,
                            LABEL_FLOW_PENDING_REGISTRATION,
                            "缺少来源 ID，无法进入配对；退回批次登记补充")
        if not batch_id:
            return returned("missing_batch_id", DEST_BATCH_INTAKE,
                            LABEL_FLOW_PENDING_REGISTRATION,
                            "缺少批次号，无法追溯；退回批次登记补充")

    # 3) 候选描述
    description = extract_description(record)
    problem = validate_description(description, record, business)
    if problem:
        if problem == "description_missing":
            stats["description_missing"] += 1
        else:
            stats["description_invalid"] += 1
        if attempts >= int(business["max_return_count"]):
            return sample_quarantine(
                "return_limit_exceeded", LABEL_SAMPLE_SEMANTIC,
                "同一条已退回 %d 次（上限 %d），锁定不再自动回流；描述问题：%s"
                % (attempts, int(business["max_return_count"]), problem),
                locked=True, evidence={"description_reason": problem})
        return returned(problem, DEST_CANDIDATE_DESCRIPTION, LABEL_FLOW_PENDING_DESCRIPTION,
                        "描述不可用（%s），退回候选描述补写，不进隔离区" % problem)

    # 4) 授权结论
    trainable_values = list(business.get("trainable_license_values") or [])
    if license_value is None:
        stats["license_missing"] += 1
        return sample_quarantine("license_missing", LABEL_SAMPLE_SENSITIVE,
                                 "无法确认授权结论为“可训练”，按样本隔离处置")
    if not _is_trainable_license(license_value, trainable_values):
        stats["license_not_trainable"] += 1
        return sample_quarantine("license_not_trainable", LABEL_SAMPLE_SENSITIVE,
                                 "授权结论不是“可训练”（实际：%s），按样本隔离处置"
                                 % license_value)
    stats["license_trainable"] += 1

    # 5) 模态数
    if len(modalities) < int(business["min_modalities"]):
        return sample_quarantine(
            "modality_insufficient", LABEL_SAMPLE_SEMANTIC,
            "只有 %d 种模态，低于要求的 %d 种，无法配对"
            % (len(modalities), int(business["min_modalities"])),
            evidence={"modalities": modalities})

    # 6) 通过：只追加本算子命名空间
    admitted = _admit(record, description, license_value, source_id, batch_id,
                      modalities, file_reports, duplicate, event_time,
                      business.get("operator_version"))
    return _decision(DECISION_ADMIT, admitted, None, None)


def _admit(record: dict, description: Dict[str, Any], license_value: Any,
           source_id: Optional[str], batch_id: Optional[str], modalities: List[str],
           file_reports: List[Dict[str, Any]], duplicate: bool, event_time: str,
           operator_version: Optional[str]) -> Dict[str, Any]:
    """在通过记录上追加 ``admission`` 与 ``processing["10"]``，不覆盖上游结论。"""
    output = copy.deepcopy(record)
    collapsed = " ".join((description.get("value") or "").split())
    checks: Dict[str, Any] = {}
    for report in file_reports:
        checks[report["modality"]] = {
            key: value for key, value in report.items()
            if key not in ("ok", "label", "reason", "message", "modality")
        }
    output["admission"] = {
        "operator": OPERATOR,
        "formalOperatorNumber": FORMAL_OPERATOR_NUMBER,
        "version": operator_version,
        "passed": True,
        "modalities": list(modalities),
        "modality_count": len(modalities),
        "description": {
            "source": description.get("source"),
            "chars": len(collapsed),
            "valid": True,
        },
        "license": license_value if isinstance(license_value, str) else str(license_value),
        "source_id": source_id,
        "batch_id": batch_id,
        "file_checks": checks,
        "duplicate": bool(duplicate),
        "admittedAt": event_time,
    }
    processing = output.get("processing")
    if not isinstance(processing, dict):
        processing = {}
    processing["10"] = {
        # [v4.2] status 双字段：int status + 原字符串迁入 process_status
        "status": 1,
        "process_status": "success",
        "action": ACTION_ADMIT,
        "modalities": list(modalities),
        "metrics": {
            "modality_count": len(modalities),
            "description_chars": len(collapsed),
            "files_checked": len(file_reports),
        },
        "finished_at": event_time,
    }
    output["processing"] = processing
    return output


def _decision(action: str, record: Optional[dict], entry: Optional[dict],
              reason: Optional[str]) -> Dict[str, Any]:
    return {"action": action, "record": record, "entry": entry, "reason": reason}


def busy_line_entry(*, operator: str, stage_id: str, line: int, reason: str, message: str,
                    event_time: str) -> Dict[str, Any]:
    """坏行台账条目：只保留行号与原因，不保存无限长的原始坏行。"""
    return {
        "sample_id": None,
        "line": line,
        "label": LABEL_ORIGIN_UNPARSABLE,
        "operator": operator,
        "formalOperatorNumber": FORMAL_OPERATOR_NUMBER,
        "stage": stage_id,
        "action": ACTION_ISOLATE,
        "destination": DEST_ORIGIN,
        "reason": reason,
        "message": message,
        "event_time": event_time,
        "batch_id": None,
        "retry_count": 0,
        "locked": False,
    }


def json_dumps(record: dict) -> str:
    return json.dumps(record, ensure_ascii=False)
