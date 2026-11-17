"""算子 13 业务模块：音文语义一致性评分（表1 #13 口径）。

职责边界：
- 只消费 #11 的候选配对（音文对）或等价单记录，输出音文分数、编辑距离、通过结论、模型版本；
- 通过 → 交 #19（主产物）；不通过 → 交 #21（样本隔离台账）；缺转写/缺描述/无音文对属流程状态，不隔离；
- 原始文件问题（无法解析/损坏/编码异常/整段无声）进原始隔离区，不静默丢弃；
- 关闭路线时不加载模型、不写音文评分文件、不隔离。

判定（v2）：两侧文本先做繁→简字符级归一化（默认开启，可配关闭），规范化后的
字符级 CER/编辑距离比例 ≤ 任务阈值才通过。
Whisper-base 转写由上游 #10 提供；Wav2Vec2/BERT 裸余弦只允许作为显式实验诊断，
不参与入池、隔离或通过结论。
"""
from __future__ import annotations

import re
from typing import Any, Dict, List, Optional, Tuple

from utils import audio as audio_mod
from utils import io as io_utils
from utils import pinyin as pinyin_mod
from utils import scoring

OPERATOR = "audio_text_scoring"
OPERATOR_VERSION = "v2.0.0"
FORMAL_OPERATOR_NUMBER = 13
STAGE_ID = "S3"

MAIN_NAME_ENABLED = "13_audio_text_scores.jsonl"
MAIN_NAME_DISABLED = "13_passthrough.jsonl"
FLOW_NAME = "13_flow_records.jsonl"
REJECTS_NAME = "13_rejects.jsonl"
SAMPLE_QUARANTINE_NAME = "13_sample_quarantine.jsonl"
PENDING_NAME = "13_pending.jsonl"

LEDGER_NAMES = (REJECTS_NAME, SAMPLE_QUARANTINE_NAME, PENDING_NAME)

#: 质量标签（背景规则 §2.3）
LABEL_ORIGIN_UNPARSABLE = "原始文件-无法解析"
LABEL_ORIGIN_BROKEN = "原始文件-损坏"
LABEL_ORIGIN_EMPTY = "原始文件-空"
LABEL_ORIGIN_ENCODING = "原始文件-编码异常"
LABEL_ORIGIN_MISSING_FIELD = "原始文件-缺字段"
LABEL_SAMPLE_SEMANTIC = "样本-语义不通"
LABEL_SCORE_PASS = "样本-评分：通过"
LABEL_FLOW_PENDING_SCORE = "流程-待评分"
LABEL_FLOW_PENDING_TRANSCRIPT = "流程-待转写"
LABEL_FLOW_PENDING_DESCRIPTION = "流程-待补描述"
LABEL_FLOW_NO_AUDIO_TEXT = "流程-无音文对"
LABEL_FLOW_ROUTE_DISABLED = "流程-音文路线未启用"
LABEL_FLOW_LANGUAGE_NOT_APPLICABLE = "流程-语言不适用"
LABEL_FLOW_TRANSCRIPT_UNRELIABLE = "流程-转写不可靠"
LABEL_FLOW_PINYIN_TABLE_UNAVAILABLE = "流程-同音容错表缺失"

#: 去向
DEST_ORIGIN = "原始隔离区"
DEST_SAMPLE = "样本隔离库"
DEST_PIPELINE = "产线待处理"
DEST_NEXT = "#19"

ACTION_SCORE = "score"
ACTION_SKIP = "skip"
ACTION_ISOLATE = "记录并隔离"
ACTION_PENDING = "不评分并待处理"
ACTION_PASS = "通过并交下游"
ACTION_RECORD = "记录并隔离"

#: 台账 reason 枚举
REASON_JSON_PARSE_ERROR = "json_parse_error"
REASON_RECORD_NOT_OBJECT = "record_not_object"
REASON_ENCODING_ERROR = "encoding_error"
REASON_MISSING_FIELD = "missing_field: sample_id"
REASON_NO_AUDIO = "no_audio_text_pair"
REASON_MISSING_TRANSCRIPT = "missing_transcript"
REASON_MISSING_DESCRIPTION = "missing_description"
REASON_AUDIO_MISSING = "audio_missing"
REASON_AUDIO_UNREADABLE = "audio_unreadable"
REASON_AUDIO_SILENT = "audio_silent"
REASON_UNSUPPORTED_MEDIA_PATH = "unsupported_media_path"
REASON_EDIT_EXCEEDED = "edit_distance_exceeded"
REASON_COSINE_BELOW = "cosine_below_threshold"
REASON_PASSED = "passed"
#: 字符 CER 超阈值，但拼音级编辑距离在阈值内：ASR 同音误写，口语内容一致
REASON_PASSED_HOMOPHONE_TOLERANT = "passed_homophone_tolerant"
REASON_MODEL_UNAVAILABLE = "model_unavailable"
REASON_SCORE_ERROR = "score_error"
#: 非中文音文对：CER 是中文口径，两侧没有中文字符时无法按本算子的阈值语义判定
REASON_LANGUAGE_NOT_APPLICABLE = "language_not_applicable"
#: 转写不可靠：Whisper 解码循环 / 语速超出物理上限时，CER 反映的是 ASR 故障，不是语义差异
REASON_TRANSCRIPT_UNRELIABLE = "transcript_unreliable"
#: 同音容错已开启但拼音表不可用：严格字符口径会把同音误写判成语义差异，属依赖缺失
REASON_PINYIN_TABLE_UNAVAILABLE = "pinyin_table_unavailable"


# --------------------------------------------------------------------------- #
# 字段解析
# --------------------------------------------------------------------------- #

def _text_value(value: Any) -> Optional[str]:
    if isinstance(value, str) and value.strip():
        return value.strip()
    return None


#: 中文字符（含扩展 A、兼容区）。#13 的 CER 是中文口径，两侧没有中文时该阈值不可解释。
_CJK_PATTERN = re.compile(r"[㐀-䶿一-鿿豈-﫿]")


def cjk_char_count(text: Any) -> int:
    """统计文本中的中文字符数（用于判断音文对是否落在中文 CER 口径内）。"""
    if not isinstance(text, str) or not text:
        return 0
    return len(_CJK_PATTERN.findall(text))


def language_applicable(description: Any) -> bool:
    """描述侧是否含中文。

    上游 ``text.lang`` 常被统一填成 ``zh``（英文素材也一样），不可作为语言依据；
    这里只看实际字符：描述侧一个中文字符都没有时，说明该对的比较目标不是中文，
    中文 CER 阈值（0.10–0.50）对它没有语义，不能据此判「语义不通」。
    """
    return cjk_char_count(description) > 0


#: 解码循环判定：同一单元（1–12 字）连续出现 ≥6 次。正常中文口播不会出现这种重复；
#: Whisper 在音乐/噪声/低信噪比片段上会产生 "你不想要我 你不想要我 …" 这类循环。
_REPEAT_UNIT_MAX_CHARS = 12
_REPEAT_MIN_TIMES = 6
_REPEAT_PATTERN = re.compile(r"(.{1,%d}?)\1{%d,}" % (_REPEAT_UNIT_MAX_CHARS, _REPEAT_MIN_TIMES - 1))


def detect_repetition_loop(text: Any) -> Optional[Dict[str, Any]]:
    """检测 Whisper 解码重复循环；返回 ``{unit, times, matched_chars}`` 或 None。"""
    if not isinstance(text, str) or not text:
        return None
    compact = re.sub(r"\s+", "", text)
    match = _REPEAT_PATTERN.search(compact)
    if not match:
        return None
    unit = match.group(1)
    return {"unit": unit, "times": len(match.group(0)) // len(unit),
            "matched_chars": len(match.group(0))}


def transcript_reliability(transcript: Any, audio_seconds: Any,
                           max_chars_per_second: float = 30.0) -> Dict[str, Any]:
    """判断转写是否可用于 CER 比较。

    两类不可靠：
    1. 语速超出物理上限（``max_chars_per_second``）：5 秒音频不可能说出数百字；
    2. 同一单元连续重复 ≥6 次（Whisper 解码循环）。

    这两种情况下 CER 度量的是 ASR 故障的程度，而不是音文语义差异，因此不能据此
    判「语义不通」。返回 ``{reliable, compact_chars, audio_seconds, chars_per_second,
    repetition, reasons}``。
    """
    compact = re.sub(r"\s+", "", transcript) if isinstance(transcript, str) else ""
    seconds = float(audio_seconds) if isinstance(audio_seconds, (int, float)) else 0.0
    chars_per_second = round(len(compact) / seconds, 3) if seconds > 0 else None
    repetition = detect_repetition_loop(transcript)
    reasons: List[str] = []
    if chars_per_second is not None and chars_per_second > float(max_chars_per_second):
        reasons.append("speech_rate_exceeded")
    if repetition is not None:
        reasons.append("repetition_loop")
    return {
        "reliable": not reasons,
        "compact_chars": len(compact),
        "audio_seconds": round(seconds, 3),
        "chars_per_second": chars_per_second,
        "max_chars_per_second": float(max_chars_per_second),
        "repetition": repetition,
        "reasons": reasons,
    }


def _as_float(value: Any, default: float) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return default
    number = float(value)
    if number != number:
        return default
    return number


def _audio_payload(record: Any) -> Optional[Dict[str, Any]]:
    if not isinstance(record, dict):
        return None
    payload = record.get("audio")
    if isinstance(payload, dict):
        return payload
    path = _text_value(payload)
    return {"path": path} if path else None


def _official_transcript(record: Any) -> Optional[Dict[str, Any]]:
    """Accept the verified AISHELL whole-audio source, never a generic caption.

    Only an inactive ASR route can be replaced. Failed/review/unknown gates
    continue blocking scoring even when official text is present.
    """
    if not isinstance(record, dict):
        return None
    metadata = record.get("metadata")
    audio = _audio_payload(record)
    if not isinstance(metadata, dict) or not audio or not _text_value(audio.get("path")):
        return None
    value = _text_value(metadata.get("official_text"))
    producer = _text_value(metadata.get("transcription_producer")) or ""
    sample_id = _text_value(record.get("sample_id")) or ""
    if not (value and metadata.get("dataset_id") == "kaggle_aishell3_01"
            and "AISHELL-3" in producer and "AISHELL-3_" in sample_id):
        return None
    gate = record.get("transcription_gate") or {}
    if not isinstance(gate, dict):
        return None
    reason = gate.get("reason")
    if gate.get("direct_scoring_allowed") is False and reason not in (
            "transcription_disabled", "master_switch_off"):
        return None
    return {"value": value, "source": "metadata.official_text",
            "model": "dataset_official_transcript", "source_type": "dataset_official",
            "producer": producer, "language": metadata.get("language") or "zh",
            "scope": "whole_audio", "upstream_gate_reason": reason}


def resolve_audio_reference(record: Any) -> Optional[Dict[str, Any]]:
    """整段转写配整段音频；明确段落输入才使用段落WAV，避免丢掉后续段落。"""
    payload = _audio_payload(record)
    if payload is None:
        return None
    whole_transcript = None
    if isinstance(record, dict):
        whole_transcript = _text_value(record.get('audio_transcript'))
        transcription = record.get('transcription') or {}
        if isinstance(transcription, dict):
            whole_transcript = whole_transcript or _text_value(transcription.get('text'))
    whole_transcript = whole_transcript or _text_value(payload.get('transcript'))
    if not whole_transcript and resolve_transcript(record).get("scope") == "whole_audio":
        whole_transcript = True
    path = _text_value(payload.get('path'))
    if whole_transcript and path:
        return {'path': path, 'start': 0.0, 'end': _as_float(payload.get('duration'), 0.0),
                'source': 'audio.path'}
    segments = payload.get("segments")
    if isinstance(segments, list):
        for segment in segments:
            if not isinstance(segment, dict):
                continue
            path = _text_value(segment.get("path"))
            if not path:
                continue
            start = _as_float(segment.get("start"), 0.0)
            return {
                "path": path,
                "start": start,
                "end": _as_float(segment.get("end"), start),
                "source": "audio.segments[0]",
            }
    path = _text_value(payload.get("path"))
    if not path:
        return None
    return {
        "path": path,
        "start": 0.0,
        "end": _as_float(payload.get("duration"), 0.0),
        "source": "audio.path",
    }


def resolve_transcript(record: Any) -> Dict[str, Any]:
    """Prefer upstream ASR; fall back to the verified whole-audio official source."""
    official = _official_transcript(record)
    candidates: List[Tuple[str, Any]] = []
    if isinstance(record, dict):
        gate = record.get("transcription_gate") or {}
        if gate.get("direct_scoring_allowed") is False:
            return official or {"value": None, "source": "transcription_gate"}
        transcription = record.get("transcription") or {}
        candidates.append(("transcription.text", transcription.get("text")))
        candidates.append(("audio_transcript", record.get("audio_transcript")))
    payload = _audio_payload(record)
    if isinstance(payload, dict):
        candidates.append(("audio.transcript", payload.get("transcript")))
        first_segment = None
        segments = payload.get("segments")
        if isinstance(segments, list):
            for segment in segments:
                if isinstance(segment, dict) and _text_value(segment.get("transcript")):
                    first_segment = segment.get("transcript")
                    break
        candidates.append(("audio.segments[0].transcript", first_segment))
    if isinstance(record, dict):
        text = record.get("text")
        if isinstance(text, dict):
            candidates.append(("text.transcript", text.get("transcript")))
        processing = record.get("processing")
        if isinstance(processing, dict):
            node = processing.get("08")
            if isinstance(node, dict):
                candidates.append(("processing.08.transcript", node.get("transcript")))
        metadata = record.get("metadata")
        if isinstance(metadata, dict):
            candidates.append(("metadata.transcript", metadata.get("transcript")))
    for source, value in candidates:
        cleaned = _text_value(value)
        if cleaned:
            result = {"value": cleaned, "source": source}
            # #10 的转写节点通常把模型身份放在 transcription 对象中；
            # 这里只引用上游证据，不在本算子重新加载 Whisper。
            if isinstance(record, dict):
                metadata_sources = []
                if source.startswith("transcription."):
                    metadata_sources.append(record.get("transcription"))
                if source.startswith("processing.08."):
                    processing = record.get("processing")
                    metadata_sources.append(processing.get("08") if isinstance(processing, dict) else None)
                payload = _audio_payload(record)
                if source.startswith("audio."):
                    metadata_sources.append(payload)
                for metadata in metadata_sources:
                    if isinstance(metadata, dict):
                        for key in ("model", "model_version", "language", "start_ms", "end_ms"):
                            if metadata.get(key) is not None:
                                result[key] = metadata.get(key)
            return result
    return official or {"value": None, "source": None}


def resolve_description(record: Any) -> Dict[str, Any]:
    """解析对齐描述（本产线 #9 产出），返回 ``{value, source}``。"""
    candidates: List[Tuple[str, Any]] = []
    if isinstance(record, dict):
        # M09's selected description is authoritative; M11 keeps it in side.record.
        candidates.append(("description", record.get("description")))
        text = record.get("text")
        if isinstance(text, dict):
            candidates.append(("text.content", text.get("content")))
        elif isinstance(text, str):
            candidates.append(("text", text))
        metadata = record.get("metadata")
        if isinstance(metadata, dict):
            candidates.append(("metadata.description", metadata.get("description")))
        processing = record.get("processing")
        if isinstance(processing, dict):
            node = processing.get("09")
            if isinstance(node, dict):
                candidates.append(("processing.09.description", node.get("description")))
        if isinstance(text, dict):
            candidates.append(("text.original", text.get("original")))
    for source, value in candidates:
        cleaned = _text_value(value)
        if cleaned:
            result = {"value": cleaned, "source": source}
            if source == "description":
                origin = _text_value(record.get("description_source"))
                if origin:
                    result["origin"] = origin
            return result
    return {"value": None, "source": None}


def _batch_id(record: Any, *extra_records: Any) -> Optional[str]:
    for candidate in (record,) + extra_records:
        if not isinstance(candidate, dict):
            continue
        for key in ("batch_id", "batchId"):
            value = _text_value(candidate.get(key))
            if value:
                return value
        metadata = candidate.get("metadata")
        if isinstance(metadata, dict):
            for key in ("batch_id", "batchId", "batch"):
                value = _text_value(metadata.get(key))
                if value:
                    return value
    return None


def resolve_pair(record: Any) -> Dict[str, Any]:
    """把记录归一为音文评分所需字段。

    兼容两种上游形态：
    - #11 ``11_candidate_pairs.jsonl`` 的配对记录（``left`` / ``right`` 各带 ``record``）；
    - 单记录形态（同一记录内同时含音频与对齐描述）。
    """
    if not isinstance(record, dict):
        return {"mode": "flat", "identifier": None}
    left = record.get("left")
    right = record.get("right")
    if isinstance(left, dict) and isinstance(right, dict):
        left_record = left.get("record") if isinstance(left.get("record"), dict) else left
        right_record = right.get("record") if isinstance(right.get("record"), dict) else right
        left_audio = resolve_audio_reference(left_record)
        right_audio = resolve_audio_reference(right_record)
        if right_audio is not None and left_audio is None:
            audio_record, text_record = right_record, left_record
        else:
            audio_record, text_record = left_record, right_record
        transcript = resolve_transcript(audio_record)
        if transcript["value"] is None and transcript["source"] != "transcription_gate":
            transcript = resolve_transcript(text_record)
        description = resolve_description(text_record)
        if description["value"] is None:
            description = resolve_description(audio_record)
        return {
            "mode": "pair",
            "identifier": _text_value(record.get("pair_id"))
                          or _text_value(left.get("sample_id"))
                          or _text_value(right.get("sample_id"))
                          or _text_value(left_record.get("sample_id"))
                          or _text_value(right_record.get("sample_id")),
            "pair_id": _text_value(record.get("pair_id")),
            "sample_id": _text_value(left.get("sample_id")) or _text_value(right.get("sample_id")),
            "batch_id": _batch_id(record, text_record, audio_record),
            "audio": resolve_audio_reference(audio_record),
            "transcript": transcript,
            "description": description,
        }
    return {
        "mode": "flat",
        "identifier": _text_value(record.get("sample_id")),
        "pair_id": None,
        "sample_id": _text_value(record.get("sample_id")),
        "batch_id": _batch_id(record),
        "audio": resolve_audio_reference(record),
        "transcript": resolve_transcript(record),
        "description": resolve_description(record),
    }


def _modality_tokens(value):
    entries = value if isinstance(value, list) else [value]
    return {token for entry in entries if isinstance(entry, str)
            for token in re.split(r"[\s,+/_|\-]+", entry.lower()) if token}


def _only_non_audio_media(record: dict) -> bool:
    """Require evidence of a different route and no declared or supplied audio."""
    pair_modalities = _modality_tokens(record.get("modality_pair"))
    sources = [(record, True)]
    for name in ("left", "right"):
        side = record.get(name)
        if isinstance(side, dict):
            sources.append((side, True))
            if isinstance(side.get("record"), dict):
                view_modalities = _modality_tokens(side.get("modalities")) or _modality_tokens(side.get("modality"))
                # M11 keeps the original collection's modality tags inside each
                # derived record. Its wrapper and pair describe the active view.
                sources.append((side["record"], not (pair_modalities or view_modalities)))
    flags = set()
    for source, use_modality_tags in sources:
        if use_modality_tags:
            for key in ("modality_pair", "modality", "modalities"):
                flags.update(_modality_tokens(source.get(key)))
        media = source.get("media")
        items = media if isinstance(media, list) else [media]
        for item in items:
            if isinstance(item, str) and item.strip():
                return False  # Untyped media could be the missing audio.
            if isinstance(item, dict) and any(item.get(k) for k in ("path", "file", "uri")):
                kinds = _modality_tokens(item.get("role") or item.get("modality") or item.get("type"))
                if not kinds:
                    return False
                flags.update(kinds)
        processing = source.get("processing")
        local = processing.get("17") if isinstance(processing, dict) else None
        local_paths = local.get("paths") if isinstance(local, dict) else None
        for container in (source, media, source.get("paths"), local_paths):
            if not isinstance(container, dict):
                continue
            for kind in ("audio", "image", "video"):
                if any(container.get(key) for key in
                       (kind, kind + "s", kind + "_path", kind + "_reference")):
                    flags.add(kind)
        if source.get("audio_segment"):
            flags.add("audio")
    return bool(flags & {"image", "video", "图像", "图片", "视频"}) and not (
        flags & {"audio", "speech", "sound", "av", "音频", "音视频"})


# --------------------------------------------------------------------------- #
# 台账条目与节点
# --------------------------------------------------------------------------- #

def make_ledger_entry(*, line: int, reason: str, label: str, action: str, destination: str,
                      event_time: str, sample_id: Optional[str] = None,
                      pair_id: Optional[str] = None, batch_id: Optional[str] = None,
                      retry_count: int = 0, node: Optional[Dict[str, Any]] = None,
                      record: Optional[Dict[str, Any]] = None,
                      detail: Optional[str] = None,
                      model: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
    """统一台账条目（背景规则 §2.3 的标签/工序/动作/去向/时间/批次/重跑次数）。"""
    entry: Dict[str, Any] = {
        "line": int(line),
        "sample_id": sample_id,
        "pair_id": pair_id,
        "reason": reason,
        "label": label,
        "stage": str(FORMAL_OPERATOR_NUMBER),
        "operator": OPERATOR,
        "action": action,
        "destination": destination,
        "event_time": event_time,
        "batch_id": batch_id,
        "retry_count": int(retry_count),
    }
    if detail:
        entry["detail"] = detail
    if model:
        entry["model"] = model
    if node is not None:
        entry["audio_text"] = node
    if record is not None:
        entry["record"] = record
    return entry


def skipped_node() -> Dict[str, Any]:
    """关闭路线时的节点：不加载模型、不写音文评分文件、不隔离。"""
    return {
        # [v4.2] status 双字段：路线关闭是正常流程态（不过滤）→ status=1 + process_status
        "status": 1,
        "process_status": "skipped",
        "route_enabled": False,
        "reason": "route_disabled",
        "label": LABEL_FLOW_ROUTE_DISABLED,
        "action": ACTION_SKIP,
        "destination": DEST_PIPELINE,
        "passed": None,
        "degraded": False,
    }


def _base_node(config, event_time: str, spec: Dict[str, Any]) -> Dict[str, Any]:
    audio = spec.get("audio") or {}
    return {
        "route_enabled": True,
        "cosine_similarity": None,
        "raw_cosine_similarity": None,
        "cosine_source": None,
        "auxiliary_similarity": None,
        "auxiliary_model": None,
        "auxiliary_model_version": None,
        "auxiliary_model_path": None,
        "auxiliary_device": None,
        "auxiliary_log_likelihood": None,
        "auxiliary_token_count": None,
        "mapped_cosine_similarity": None,
        "mapping_version": None,
        "mapping_source": None,
        "mapping_status": "disabled",
        # Schema compatibility only. Raw cosine is diagnostic and does not
        # participate in the production decision.
        "cosine_threshold": None,
        # [v4.2] 裸余弦诊断字段（V4.2 §4.13 字段名 diag_cosine）：仅实验诊断，不参与判定
        "diag_cosine": None,
        "edit_distance": None,
        "edit_distance_ratio": None,
        "edit_distance_ratio_max": config.edit_distance_ratio_max,
        "audio_model": None,
        "text_model": None,
        "audio_model_version": None,
        "text_model_version": None,
        "audio_model_path": None,
        "text_model_path": None,
        "audio_segment": {
            "path": audio.get("path"),
            "start": audio.get("start"),
            "end": audio.get("end"),
        } if audio else None,
        "transcript_source": (spec.get("transcript") or {}).get("source"),
        "transcript_model": (spec.get("transcript") or {}).get("model", "whisper-base"),
        "transcript_model_version": (spec.get("transcript") or {}).get("model_version"),
        "transcript_language": (spec.get("transcript") or {}).get("language", "zh"),
        "transcript_source_type": (spec.get("transcript") or {}).get("source_type"),
        "transcript_producer": (spec.get("transcript") or {}).get("producer"),
        "transcript_scope": (spec.get("transcript") or {}).get("scope"),
        "transcript_upstream_gate_reason": (spec.get("transcript") or {}).get("upstream_gate_reason"),
        "description_source": (spec.get("description") or {}).get("source"),
        "description_origin": (spec.get("description") or {}).get("origin"),
        "transcript_chars": len((spec.get("transcript") or {}).get("value") or ""),
        "description_chars": len((spec.get("description") or {}).get("value") or ""),
        # 语言适用性审计：中文 CER 只对含中文的描述有语义（见 language_applicable）
        "language_gate": bool(getattr(config, "language_gate", True)),
        "language_applicable": None,
        "description_cjk_chars": None,
        # 转写可靠性审计：语速上限、解码循环与判定依据
        "transcript_reliability_gate": bool(
            getattr(config, "transcript_reliability_gate", True)),
        "transcript_reliable": None,
        "transcript_chars_per_second": None,
        "transcript_repetition": None,
        "transcript_unreliable_reasons": None,
        "scoring_method": "character_edit_distance_ratio",
        "scoring_version": "v2",
        "metric": "CER",
        "threshold_source": "task_config",
        "cer": None,
        "wer": None,
        "transcript_normalized": None,
        "description_normalized": None,
        # 繁→简归一化审计字段：开关、转换前口径、实际转换字符数、词典来源
        "traditional_to_simplified": bool(config.traditional_to_simplified),
        "transcript_normalized_before_t2s": None,
        "description_normalized_before_t2s": None,
        "traditional_chars_converted": None,
        "t2s_dictionary_source": None,
        "t2s_mapping_entries": None,
        # 中文 ITN 审计字段：开关与真正改写的数字词元数（scoring.text_error_metrics 填充）
        "itn_applied": bool(getattr(config, "inverse_text_normalization", True)),
        "itn_lowered_cer": None,
        "cer_without_itn": None,
        "number_tokens_normalized": None,
        # 同音容错审计：同音字替换代价为 0 的字符级编辑距离比例与词典来源
        # （见 utils/pinyin.py）。阈值 edit_distance_ratio_max 语义不变；
        # edit_distance_ratio 仍是严格字符口径，判定取两者的较小值。
        "homophone_tolerance": bool(getattr(config, "homophone_tolerance", True)),
        "edit_distance_ratio_homophone": None,
        "homophone_tolerated": False,
        "pinyin_dictionary_source": None,
        "pinyin_table_entries": None,
        "candidate_pool_eligible": False,
        "degraded": False,
        "scored_at": event_time,
    }


def audio_failure(detail: str) -> Tuple[str, str]:
    """把音频读取失败映射为 (reason, label)。"""
    text = str(detail or "")
    if "整段无声" in text or "音频为空" in text:
        return REASON_AUDIO_SILENT, LABEL_ORIGIN_EMPTY
    if "不存在" in text:
        return REASON_AUDIO_MISSING, LABEL_ORIGIN_UNPARSABLE
    if "无法解码" in text:
        return REASON_AUDIO_UNREADABLE, LABEL_ORIGIN_UNPARSABLE
    return REASON_AUDIO_UNREADABLE, LABEL_ORIGIN_BROKEN


def resolve_media_file(media_roots: List[Any], relative: str):
    """在给定根目录集合中定位被引用的音频；先做路径安全校验，再找文件。"""
    last_error: Optional[Exception] = None
    for root in media_roots:
        try:
            candidate = io_utils.resolve_media(root, relative)
        except io_utils.UnsupportedMediaPath as exc:
            last_error = exc
            continue
        if candidate.is_file():
            return candidate
    if last_error is not None:
        raise last_error
    return None


# --------------------------------------------------------------------------- #
# 单条记录判定
# --------------------------------------------------------------------------- #

def evaluate_record(spec: Dict[str, Any], line: int, *, media_roots: List[Any], config,
                    scorer, auxiliary_scorer=None, mapping=None, event_time: str) -> Dict[str, Any]:
    """判定一条记录：返回 ``{action, record, entry, reason, degraded}``。

    ``action``：``pass``（主产物）/ ``isolate``（样本隔离）/ ``pending``（流程待处理）/
    ``origin``（原始隔离）。
    """
    record = spec["record"]
    sample_id = spec.get("sample_id")
    pair_id = spec.get("pair_id")
    batch_id = spec.get("batch_id")

    def ledger(reason: str, label: str, action: str, destination: str,
               node=None, detail=None, model=None):
        return make_ledger_entry(line=line, reason=reason, label=label, action=action,
                                 destination=destination, event_time=event_time,
                                 sample_id=sample_id, pair_id=pair_id, batch_id=batch_id,
                                 node=node, record=record, detail=detail, model=model)

    # 1) 音频引用是否存在（无音文对属流程状态，不隔离）
    audio_ref = spec.get("audio")
    if audio_ref is None:
        node = _base_node(config, event_time, spec)
        if _only_non_audio_media(record):
            node.update({"status": 1, "process_status": "not_applicable",
                         "route_enabled": False, "passed": None, "reason": REASON_NO_AUDIO,
                         "label": LABEL_FLOW_NO_AUDIO_TEXT, "action": ACTION_SKIP,
                         "destination": DEST_PIPELINE})
            record.setdefault("processing", {})["13"] = node
            return {"action": "other_modality", "reason": REASON_NO_AUDIO,
                    "node": node, "record": record}
        # [v4.2] status 双字段：等待是正常流程态（不过滤）→ status=1 + process_status=pending
        node.update({"status": 1, "process_status": "pending", "passed": None,
                     "reason": "audio_reference_unresolved",
                     "label": LABEL_FLOW_NO_AUDIO_TEXT, "action": ACTION_PENDING,
                     "destination": DEST_PIPELINE})
        record.setdefault("processing", {})["13"] = node
        return {"action": "pending", "reason": REASON_NO_AUDIO, "node": node,
                "record": record,
                "entry": ledger(REASON_NO_AUDIO, LABEL_FLOW_NO_AUDIO_TEXT, ACTION_PENDING,
                                DEST_PIPELINE, node=node)}

    # 2) 转写缺失 / 描述缺失：留产线，不隔离，也不把缺失当低分
    transcript = (spec.get("transcript") or {}).get("value")
    description = (spec.get("description") or {}).get("value")
    for value, reason, label in (
            (transcript, REASON_MISSING_TRANSCRIPT, LABEL_FLOW_PENDING_TRANSCRIPT),
            (description, REASON_MISSING_DESCRIPTION, LABEL_FLOW_PENDING_DESCRIPTION)):
        if not value:
            node = _base_node(config, event_time, spec)
            # [v4.2] status 双字段：status=1 + process_status=pending
            node.update({"status": 1, "process_status": "pending", "passed": None,
                         "reason": reason,
                         "label": label, "action": ACTION_PENDING, "destination": DEST_PIPELINE})
            record.setdefault("processing", {})["13"] = node
            return {"action": "pending", "reason": reason, "node": node,
                    "record": record,
                    "entry": ledger(reason, label, ACTION_PENDING, DEST_PIPELINE, node=node)}

    # 规范化后为空的占位符（例如仅有标点）同样视为缺失，不当作 CER=0。
    for value, reason, label in (
            (transcript, REASON_MISSING_TRANSCRIPT, LABEL_FLOW_PENDING_TRANSCRIPT),
            (description, REASON_MISSING_DESCRIPTION, LABEL_FLOW_PENDING_DESCRIPTION)):
        if not scoring.normalize_text(value):
            node = _base_node(config, event_time, spec)
            # [v4.2] status 双字段：status=1 + process_status=pending
            node.update({"status": 1, "process_status": "pending", "passed": None,
                         "reason": reason,
                         "label": label, "action": ACTION_PENDING, "destination": DEST_PIPELINE})
            record.setdefault("processing", {})["13"] = node
            return {"action": "pending", "reason": reason, "node": node,
                    "record": record,
                    "entry": ledger(reason, label, ACTION_PENDING, DEST_PIPELINE, node=node)}

    # 3) 原始文件问题：路径非法 / 缺失 / 无法解码 / 整段无声
    try:
        audio_path = resolve_media_file(media_roots, audio_ref["path"])
    except io_utils.UnsupportedMediaPath as exc:
        entry = ledger(REASON_UNSUPPORTED_MEDIA_PATH, LABEL_ORIGIN_UNPARSABLE,
                       ACTION_RECORD, DEST_ORIGIN, detail=str(exc))
        return {"action": "origin", "reason": REASON_UNSUPPORTED_MEDIA_PATH, "entry": entry,
                "node": None}
    if audio_path is None:
        entry = ledger(REASON_AUDIO_MISSING, LABEL_ORIGIN_UNPARSABLE, ACTION_RECORD, DEST_ORIGIN)
        return {"action": "origin", "reason": REASON_AUDIO_MISSING, "entry": entry, "node": None}

    try:
        _waveform, _sample_rate, audio_meta = audio_mod.load_audio_mono(
            audio_path, target_rate=config.sample_rate, max_seconds=config.max_audio_seconds)
    except audio_mod.AudioReadError as exc:
        reason, label = audio_failure(str(exc))
        entry = ledger(reason, label, ACTION_RECORD, DEST_ORIGIN, detail=str(exc))
        return {"action": "origin", "reason": reason, "entry": entry, "node": None}

    # 3.5) 语言适用性：中文 CER 阈值只对中文描述有语义。
    #      描述侧没有中文字符（例如英文素材、时序窗口的英文字幕）时，Whisper 受
    #      #8「固定中文解码」约束会输出中文/混排转写，与英文描述算 CER 必然虚高；
    #      这不是「语义不通」，不能隔离。按流程态写 not_applicable，样本留产线，
    #      由下游/时序路线（#15）继续判定。配置 language_gate=false 可关闭该门。
    if getattr(config, "language_gate", True) and not language_applicable(description):
        node = _base_node(config, event_time, spec)
        node.update({
            # [v4.2] status 双字段：不适用是正常流程态（不过滤）→ status=1 + not_applicable
            "status": 1,
            "process_status": "not_applicable",
            "passed": None,
            "reason": REASON_LANGUAGE_NOT_APPLICABLE,
            "label": LABEL_FLOW_LANGUAGE_NOT_APPLICABLE,
            "action": ACTION_SKIP,
            "destination": DEST_PIPELINE,
            "language_applicable": False,
            "description_cjk_chars": cjk_char_count(description),
            "audio_duration_seconds": audio_meta["source_duration"],
            "audio_truncated": audio_meta["truncated"],
        })
        record.setdefault("processing", {})["13"] = node
        return {"action": "skip", "reason": REASON_LANGUAGE_NOT_APPLICABLE, "node": node,
                "record": record, "entry": None}

    # 3.6) 转写可靠性：Whisper 解码循环 / 语速超出物理上限时，CER 度量的是 ASR
    #      故障程度而不是语义差异，不能据此判「语义不通」，也不隔离（留产线，
    #      由 #15 时序等其他证据继续判定）。配置 transcript_reliability_gate=false 可关闭。
    if getattr(config, "transcript_reliability_gate", True):
        reliability = transcript_reliability(
            transcript, audio_meta.get("source_duration"),
            max_chars_per_second=getattr(config, "max_chars_per_second", 30.0))
        if not reliability["reliable"]:
            node = _base_node(config, event_time, spec)
            node.update({
                # [v4.2] status 双字段：转写不可靠是流程态（不过滤）→ status=1 + not_applicable
                "status": 1,
                "process_status": "not_applicable",
                "passed": None,
                "reason": REASON_TRANSCRIPT_UNRELIABLE,
                "label": LABEL_FLOW_TRANSCRIPT_UNRELIABLE,
                "action": ACTION_SKIP,
                "destination": DEST_PIPELINE,
                "language_applicable": language_applicable(description),
                "description_cjk_chars": cjk_char_count(description),
                "transcript_reliable": False,
                "transcript_chars_per_second": reliability["chars_per_second"],
                "transcript_repetition": reliability["repetition"],
                "transcript_unreliable_reasons": reliability["reasons"],
                "audio_duration_seconds": audio_meta["source_duration"],
                "audio_truncated": audio_meta["truncated"],
            })
            record.setdefault("processing", {})["13"] = node
            return {"action": "skip", "reason": REASON_TRANSCRIPT_UNRELIABLE, "node": node,
                    "record": record, "entry": None}

    # 4) 生产指标：字符级 CER/编辑距离比例。该结果不依赖任何模型。
    #    两侧文本先做繁→简归一化（配置默认开启）与中文 ITN 数字写法归一
    #    （配置默认开启），再算字符级编辑距离；阈值语义不变。
    metrics = scoring.text_error_metrics(
        transcript, description,
        traditional_to_simplified=config.traditional_to_simplified,
        inverse_text_normalization=getattr(config, "inverse_text_normalization", True))
    edit_ratio = metrics["edit_distance_ratio"]
    node = _base_node(config, event_time, spec)
    node.update(metrics)
    node["edit_distance_ratio_max"] = config.edit_distance_ratio_max
    node["audio_duration_seconds"] = audio_meta["source_duration"]
    node["audio_truncated"] = audio_meta["truncated"]
    reliability = transcript_reliability(
        transcript, audio_meta.get("source_duration"),
        max_chars_per_second=getattr(config, "max_chars_per_second", 30.0))
    node["transcript_reliable"] = reliability["reliable"]
    node["transcript_chars_per_second"] = reliability["chars_per_second"]
    node["transcript_repetition"] = reliability["repetition"]
    node["transcript_unreliable_reasons"] = reliability["reasons"]

    # 5) 可选实验诊断：只有调用方显式注入 scorer 时才计算裸余弦。
    #    它只写 diagnostic 字段，绝不改变生产结论或去向。
    if scorer is not None:
        try:
            diagnostic = scorer.score(_waveform, _sample_rate, description[:config.max_text_chars])
            raw_cosine = float(diagnostic["cosine"])
            node["raw_cosine_similarity"] = raw_cosine
            # [v4.2] V4.2 §4.13 诊断字段名 diag_cosine（与 raw_cosine_similarity 同值，仅诊断）
            node["diag_cosine"] = raw_cosine
            node["cosine_source"] = "diagnostic_only"
            node["diagnostic_model"] = diagnostic.get("audio_model") or diagnostic.get("text_model")
            node["diagnostic_model_version"] = diagnostic.get("audio_model_version") or diagnostic.get("text_model_version")
        except Exception as exc:  # noqa: BLE001 - diagnostics never block production scoring
            node["diagnostic_error"] = "%s: %s" % (type(exc).__name__, exc)

    # 4.5) 同音容错：Whisper 在中文上的典型错误是同音误写（张工程师→張宮城師、
    #      午休时→胡修實、锻炼身体→斷鏈身體），字符口径下 CER 可达 0.56，但口语
    #      内容一致。这里不改阈值、不改分母，只把「替换成同音字」的代价记为 0 后
    #      重算同一口径的编辑距离比例，判定取严格口径与该口径的较小值：
    #      同音容错只会让结果更宽松（单调），不会把原本通过的记录判失败。
    #      表不可用时**不静默降级**：严格口径已超阈值的记录按依赖缺失判
    #      `not_applicable`（留产线、不隔离），并显式写 `pinyin_table_unavailable`；
    #      严格口径本就在阈值内的记录照常通过，但同样写入该标记供审计。
    homophone_ratio = None
    homophone_error = None
    if getattr(config, "homophone_tolerance", True):
        try:
            table, source, entries = pinyin_mod.load_table()
        except pinyin_mod.PinyinTableError as exc:
            table, source, entries = None, None, None
            homophone_error = str(exc)
        if table is not None:
            homophone_ratio = pinyin_mod.homophone_edit_ratio(
                node.get("transcript_normalized"), node.get("description_normalized"),
                table=table)
            node["pinyin_dictionary_source"] = source
            node["pinyin_table_entries"] = entries
            if homophone_ratio is not None:
                node["edit_distance_ratio_homophone"] = round(homophone_ratio, 6)

    if homophone_error is not None:
        node["pinyin_table_unavailable"] = True
        node["pinyin_table_error"] = homophone_error
        if edit_ratio is not None and edit_ratio > config.edit_distance_ratio_max:
            node.update({
                # [v4.2] status 双字段：依赖缺失是流程态（不过滤）→ status=1 + not_applicable
                "status": 1,
                "process_status": "not_applicable",
                "passed": None,
                "reason": REASON_PINYIN_TABLE_UNAVAILABLE,
                "label": LABEL_FLOW_PINYIN_TABLE_UNAVAILABLE,
                "action": ACTION_SKIP,
                "destination": DEST_PIPELINE,
                "language_applicable": language_applicable(description),
                "description_cjk_chars": cjk_char_count(description),
                "homophone_tolerance": True,
                "audio_duration_seconds": audio_meta["source_duration"],
                "audio_truncated": audio_meta["truncated"],
            })
            record.setdefault("processing", {})["13"] = node
            return {"action": "skip", "reason": REASON_PINYIN_TABLE_UNAVAILABLE, "node": node,
                    "record": record, "entry": None}

    decision_ratio = edit_ratio
    if homophone_ratio is not None and (decision_ratio is None or homophone_ratio < decision_ratio):
        decision_ratio = homophone_ratio
    passed, reason = scoring.decide_edit(
        decision_ratio, edit_ratio_max=config.edit_distance_ratio_max)
    if passed and edit_ratio is not None and edit_ratio > config.edit_distance_ratio_max:
        # 严格字符口径超阈值、同音容错口径通过：记录容错实际生效，便于审计/统计。
        node["homophone_tolerated"] = True
        reason = REASON_PASSED_HOMOPHONE_TOLERANT

    if passed:
        # [v4.2] status 双字段：通过（不过滤）→ status=1 + process_status=passed
        node.update({"status": 1, "process_status": "passed", "passed": True,
                     "reason": reason,
                     "label": LABEL_SCORE_PASS, "action": ACTION_PASS,
                     "destination": DEST_NEXT, "candidate_pool_eligible": True})
        record.setdefault("processing", {})["13"] = node
        return {"action": "pass", "reason": reason, "node": node, "record": record,
                "entry": None}

    # [v4.2] status 双字段：超阈值隔离（过滤）→ status=0 + process_status=failed
    node.update({"status": 0, "process_status": "failed", "passed": False, "reason": reason,
                 "label": LABEL_SAMPLE_SEMANTIC, "action": ACTION_ISOLATE,
                 "destination": DEST_SAMPLE, "candidate_pool_eligible": False})
    record.setdefault("processing", {})["13"] = node
    return {"action": "isolate", "reason": reason, "node": node, "record": record,
            "entry": ledger(reason, LABEL_SAMPLE_SEMANTIC, ACTION_ISOLATE, DEST_SAMPLE,
                            node=node)}
