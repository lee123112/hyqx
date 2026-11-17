"""算子 #09 准备候选描述 业务逻辑（规则型，不加载任何图像描述模型）。

严格按表1：
- 来源优先级：图注→Alt→标题→字幕→语音转写→人工补写（只读固定）；
- 取第一条长度落在 [min_length, max_length] 的文本，超长截断并记录；
- 文件名和分类标签不得作为描述；
- 没有有效描述时只生成人工补写任务，留在产线等待回写，不进入准入也不进隔离。
"""
from __future__ import annotations

from pathlib import Path

from utils.language import detect_language, word_count
from utils.schemas import SOURCE_LABELS, SOURCE_PRIORITY

# 每个来源的字段别名，按顺序取第一个非空。表1未包含通用 text 字段，因此不在此列。
SOURCE_FIELDS = {
    "caption": [("caption",), ("image", "caption"), ("metadata", "caption")],
    "alt": [("alt",), ("alt_text",), ("image", "alt"),
            ("image", "alt_text"), ("metadata", "alt")],
    "title": [("title",), ("metadata", "title")],
    "subtitle": [("subtitle",), ("video", "subtitle"), ("metadata", "subtitle")],
    "transcript": [("transcript",), ("audio", "transcript"), ("audio", "segments"),
                   ("metadata", "transcript"), ("metadata", "speech_transcript")],
    "manual": [("manual_description",), ("manual_text",),
               ("description_manual",), ("metadata", "manual_description")],
}
MODALITY_KEYS = ("image", "video", "audio")

# #08 语音转写负载的读取路径（transcription.text / transcription.paragraphs）。
# 表1「语音转写」来源的实现别名；默认关闭，需任务显式开启
# description.include_transcription_payload（round9 复核：短音频 ASR 乱码时，
# 开启后这些记录会从「待人工补写」进入描述/评分流程，由 #13 按 CER 正确隔离）。
TRANSCRIPTION_PAYLOAD_PATHS = [("transcription", "text"),
                               ("transcription", "paragraphs")]


def _get(record: dict, path: tuple) -> object:
    cur: object = record
    for key in path:
        if not isinstance(cur, dict) or key not in cur:
            return None
        cur = cur[key]
    return cur


def stringify(value: object) -> str | None:
    """把字幕/转写等结构化文本规范为一段字符串；无法解析返回 None。"""
    if value is None:
        return None
    if isinstance(value, str):
        return value
    if isinstance(value, dict):
        for key in ("text", "content", "value"):
            inner = value.get(key)
            if isinstance(inner, str):
                return inner
        return None
    if isinstance(value, (list, tuple)):
        parts = []
        for item in value:
            if isinstance(item, str):
                parts.append(item)
            elif isinstance(item, dict):
                inner = item.get("text") or item.get("content")
                if isinstance(inner, str):
                    parts.append(inner)
        return "".join(parts) if parts else None
    return None


def candidate_texts(record: dict, *, include_transcription_payload: bool = False) -> list:
    """按固定优先级返回 [(source, text|None), ...]。

    ``include_transcription_payload`` 为 True 时，「语音转写」来源额外读取
    #08 的 ``transcription.text`` / ``transcription.paragraphs`` 负载。
    """
    result = []
    for source in SOURCE_PRIORITY:
        paths = list(SOURCE_FIELDS[source])
        if source == "transcript" and include_transcription_payload:
            paths += TRANSCRIPTION_PAYLOAD_PATHS
        text = None
        for path in paths:
            text = stringify(_get(record, path))
            if text and text.strip():
                break
            text = None
        result.append((source, text))
    return result


def media_paths(record: dict) -> dict:
    """返回 {modality: 相对路径}，供补写任务与打包使用。"""
    found = {}
    for key in MODALITY_KEYS:
        value = record.get(key)
        if isinstance(value, str) and value.strip():
            found[key] = value
        elif isinstance(value, dict):
            path = value.get("path")
            if isinstance(path, str) and path.strip():
                found[key] = path
    return found


def forbidden_texts(record: dict) -> dict:
    """返回 {'filename': set, 'tag': set}，两类文本都不得作为描述。"""
    filenames = set()
    for path in media_paths(record).values():
        p = Path(path.replace("\\", "/"))
        filenames.add(p.stem.strip().lower())
        filenames.add(p.name.strip().lower())
    tags = set()
    metadata = record.get("metadata")
    raw_tags = []
    if isinstance(metadata, dict):
        for key in ("image_tags", "tags"):
            value = metadata.get(key)
            if isinstance(value, (list, tuple)):
                raw_tags.extend(str(t) for t in value)
            elif isinstance(value, str):
                raw_tags.append(value)
    top_tags = record.get("tags")
    if isinstance(top_tags, (list, tuple)):
        raw_tags.extend(str(t) for t in top_tags)
    for tag in raw_tags:
        if tag.strip():
            tags.add(tag.strip().lower())
    return {"filename": filenames, "tag": tags}


def alternate_texts(record: dict) -> list:
    """同源候选描述（Flickr8k/MSVD 的 5 条候选图注），用于过短时择优。

    只读取记录自带的候选图注，不引入文件名、标签或任何新文本来源；
    顺序去重，保持上游给的出现次序。
    """
    values = []
    for path in (("metadata", "alt_candidates"), ("alt_candidates",),
                 ("image", "alt_candidates")):
        raw = _get(record, path)
        if isinstance(raw, (list, tuple)):
            values.extend(item for item in raw if isinstance(item, str))
        elif isinstance(raw, str) and raw.strip():
            values.append(raw)
    unique = []
    seen = set()
    for item in values:
        key = item.strip()
        if key and key.lower() not in seen:
            seen.add(key.lower())
            unique.append(key)
    return unique


def _valid_candidate(text: str, *, min_len: int, max_len: int, forbidden: dict,
                     forbid_filename: bool, forbid_tags: bool):
    """返回 (value, length, truncated, reason)；reason 为 None 表示可用。"""
    normalized = text.strip()
    length = len(normalized)
    if length < min_len:
        return None, length, False, "too_short"
    key = normalized.lower()
    basename = Path(key.replace("\\", "/")).name
    if forbid_filename and (key in forbidden["filename"]
                            or basename in forbidden["filename"]):
        return None, length, False, "equals_filename"
    if forbid_tags and key in forbidden["tag"]:
        return None, length, False, "equals_tag"
    truncated = length > max_len
    return (normalized[:max_len] if truncated else normalized), length, truncated, None


def _declared_language(record: dict) -> str | None:
    """记录自带的语言声明（上游 metadata.language）；没有则返回 None。"""
    for path in (("metadata", "language"), ("language",), ("metadata", "lang")):
        value = _get(record, path)
        if isinstance(value, str) and value.strip():
            return value.strip().lower()
    return None


def extract_candidate(record: dict, cfg: dict) -> dict:
    """返回候选描述决策；不产生任何模型分数。"""
    desc_cfg = cfg["description"]
    min_len = desc_cfg["min_length"]
    max_len = desc_cfg["max_length"]
    min_words = int(desc_cfg.get("min_words", 0) or 0)
    prefer_alternate = bool(desc_cfg.get("prefer_longer_alternate", True))
    detect_lang = bool(desc_cfg.get("detect_language", True))
    forbid_filename = desc_cfg.get("forbid_filename", True)
    forbid_tags = desc_cfg.get("forbid_tags", True)
    include_payload = bool(desc_cfg.get("include_transcription_payload", False))
    forbidden = forbidden_texts(record)
    skipped = []
    checks = dict(min_len=min_len, max_len=max_len, forbidden=forbidden,
                  forbid_filename=forbid_filename, forbid_tags=forbid_tags)

    for source, text in candidate_texts(
            record, include_transcription_payload=include_payload):
        if text is None:
            skipped.append({"source": source, "label": SOURCE_LABELS[source],
                            "reason": "empty"})
            continue
        value, length, truncated, reason = _valid_candidate(text, **checks)
        if reason is not None:
            skipped.append({"source": source, "label": SOURCE_LABELS[source],
                            "reason": reason, "length": length})
            continue

        chosen, words = value, word_count(value)
        alternate_used = False
        alternate_index = None
        # 描述过短（词数不足）时，先在同源候选图注里换成最长的一条有效候选；
        # 换不到就保留原文并标注 description_quality=short（不阻塞，保证下游有描述）。
        if prefer_alternate and min_words and words < min_words:
            best_value, best_words, best_index = None, words, None
            for index, item in enumerate(alternate_texts(record)):
                candidate, _alt_len, _alt_trunc, alt_reason = _valid_candidate(
                    item, **checks)
                if alt_reason is not None or candidate is None:
                    continue
                candidate_words = word_count(candidate)
                if candidate_words > best_words:
                    best_value, best_words = candidate, candidate_words
                    best_index = index
            if best_value is not None:
                chosen, words, alternate_used = best_value, best_words, True
                alternate_index = best_index
                length = len(chosen)
                truncated = length > max_len or truncated

        declared = _declared_language(record)
        if detect_lang:
            language = detect_language(chosen)
            lang_source = "detected"
            if language == "und":
                if declared in ("zh", "en", "ja", "ko"):
                    language, lang_source = declared, "declared"
        else:
            language = declared if declared in ("zh", "en", "ja", "ko") else "und"
            lang_source = "declared" if language != "und" else "none"
        return {
            "status": "described",
            "description": chosen,
            "description_source": source,
            "description_source_label": SOURCE_LABELS[source],
            "description_length": len(chosen),
            "original_length": length,
            "truncated": truncated,
            "word_count": words,
            "quality": "short" if min_words and words < min_words else "ok",
            "language": language,
            "lang_source": lang_source,
            "alternate_used": alternate_used,
            "alternate_index": alternate_index,
            "skipped": skipped,
            "reason": None,
        }

    return {
        "status": "pending_manual",
        "description": None,
        "description_source": None,
        "description_source_label": None,
        "description_length": None,
        "original_length": None,
        "truncated": False,
        "word_count": None,
        "quality": None,
        "language": None,
        "lang_source": None,
        "alternate_used": False,
        "alternate_index": None,
        "skipped": skipped,
        "reason": "no_valid_description",
    }


def build_rewrite_task(record: dict, decision: dict, cfg: dict, now: str,
                       unique_id: str) -> dict:
    """人工补写任务：留产线等待回写，不进入准入也不进隔离。"""
    media = media_paths(record)
    metadata = record.get("metadata") if isinstance(record.get("metadata"), dict) else {}
    desc_cfg = cfg["description"]
    return {
        "task_id": "rewrite-%s" % record.get("sample_id"),
        "sample_id": record.get("sample_id"),
        "operator": "candidate_description",
        "operator_version": cfg.get("operator_version", "v1.0.0"),
        "uniqueId": unique_id,
        "reason": decision.get("reason", "no_valid_description"),
        "label": "流程状态-待人工补写",
        "action": "waiting_manual",
        "destination": "pipeline_wait",
        # [v4.2] status 双字段（§2.10）：int status=1（等待回写，不是失败）+ 流程态
        "status": 1,
        "process_status": "waiting_description",
        "required_action": "manual_fill_description",
        "is_bare_image": bool(media.get("image")),
        "modalities": sorted(media.keys()),
        "media": media,
        "skipped_sources": decision.get("skipped", []),
        "source_priority": list(SOURCE_PRIORITY),
        "min_length": desc_cfg["min_length"],
        "max_length": desc_cfg["max_length"],
        "batch_id": metadata.get("batch_id") or metadata.get("source"),
        "event_time": now,
        "retry_count": int(metadata.get("retry_count") or 0),
        "model_used": None,
    }
