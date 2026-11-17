"""★业务规则：表1 #22「生成跨模态Token」（系统功能型，模型列 ``--``）。

表1 冻结口径：
1. 输入 = 候选池中**已有质量结论、且不在隔离区**的样本；没有质量结论则不生成，
   不拦样本、留在产线中；
2. 按路线分文件写样本文件（主文件名固定 ``samples.jsonl``）；
3. 文本中的模态位置写成 ``<image>`` / ``<audio>`` / ``<video>``；
4. 隔离区样本不进入样本文件；
5. 未开启的音文 / 视文 / 局部标注不产出对应文件；
6. 输出 Token 文件和样本文件，**原文件仅保留对象存储链接**（不搬运、不复制媒体）；
7. 边界：本工具**不压缩、不改版本状态**；
8. 不能训练的样本单独列出（表1 工具详细说明）。

本算子不加载任何模型、不产出任何模型分数；「Token」指**模态标记封装**而非子词 ID，
故 ``subword_token_ids`` 恒为 ``null``，绝不伪造分词结果。
"""
from __future__ import annotations

import copy
import hashlib
import json
import posixpath
from typing import Any, Dict, List, Optional, Sequence, Tuple

from . import names
from . import upstream
from .names import (FLAGGED_NAME, ISOLATED_NAME, LOG_NAME, METADATA_INDEX_NAME,
                    METADATA_NAME, OPERATOR, OPERATOR_LABEL, OPERATOR_NUMBER,
                    OPERATOR_VERSION, PENDING_NAME, REJECTS_NAME, ROUTE_AUDIO_TEXT,
                    ROUTE_IMAGE_TEXT, ROUTE_LOCAL_ANNOTATION, ROUTE_ORDER,
                    ROUTE_VIDEO_TEXT, SAMPLES_FILE_NAME, STAGE_ID, TOKENS_NAME,
                    UNTRAINABLE_NAME)

__all__ = [
    "ACTION_GENERATE", "ACTION_ISOLATED", "ACTION_ORIGIN", "ACTION_PENDING",
    "ACTION_UNTRAINABLE", "DESTINATION_ISOLATION", "DESTINATION_ORIGIN",
    "DESTINATION_PIPELINE_WAIT", "DESTINATION_SAMPLES", "DESTINATION_UNTRAINABLE",
    "FLAGGED_NAME", "ISOLATED_NAME", "LABEL_GENERATED", "LABEL_ISOLATED", "LABEL_ORIGIN",
    "LABEL_PENDING_LICENSE", "LABEL_PENDING_NO_ROUTE", "LABEL_PENDING_QUALITY",
    "LABEL_PENDING_ROUTE_DISABLED", "LABEL_PENDING_TEXT", "LABEL_UNTRAINABLE",
    "LOG_NAME", "MAX_RAW_CHARS", "MAX_RECORD_CHARS", "MEDIA_MODALITIES",
    "METADATA_INDEX_NAME", "METADATA_NAME", "OPERATOR", "OPERATOR_LABEL",
    "OPERATOR_NUMBER",
    "OPERATOR_VERSION", "ORIGIN_LABELS", "PENDING_NAME", "PROCESSOR_ACTIONS",
    "REASON_ENCODING", "REASON_JSON_PARSE", "REASON_LICENSE_MISSING",
    "REASON_LICENSE_NOT_TRAINABLE", "REASON_MARKER_VALIDATION_FAILED",
    "REASON_MISSING_IDENTIFIER", "REASON_MISSING_TEXT", "REASON_NOT_OBJECT",
    "REASON_NO_APPLICABLE_ROUTE", "REASON_NO_QUALITY_CONCLUSION",
    "REASON_ROUTE_DISABLED", "REASON_TOKEN_GENERATED", "REASON_UNSAFE_MEDIA_PATH",
    "REASON_UPSTREAM_ISOLATED", "REJECTS_NAME", "ROUTE_AUDIO_TEXT", "ROUTE_FILES",
    "ROUTE_IMAGE_TEXT", "ROUTE_LOCAL_ANNOTATION", "ROUTE_ORDER", "ROUTE_VIDEO_TEXT",
    "SAMPLES_FILE_NAME", "STAGE_ID", "TOKENS_NAME", "UNTRAINABLE_NAME",
    "applicable_routes", "build_markers", "build_metadata_row", "build_sample_record",
    "build_token_row", "build_token_text", "evaluate_record", "ledger_fields",
    "make_ledger_entry", "make_reject_entry", "route_files", "validate_markers",
]

MEDIA_MODALITIES = names.MEDIA_MODALITIES
ROUTE_FILES = names.ROUTE_FILES

# --------------------------------------------------------------------------- #
# 动作与去向
# --------------------------------------------------------------------------- #

ACTION_GENERATE = "generate"
ACTION_PENDING = "pending"
ACTION_UNTRAINABLE = "untrainable"
ACTION_ISOLATED = "isolated_skip"
ACTION_ORIGIN = "origin"

#: 判定动作 → 记录状态
STATUS_BY_ACTION = {ACTION_GENERATE: "generated", ACTION_PENDING: "pending",
                    ACTION_UNTRAINABLE: "untrainable", ACTION_ISOLATED: "isolated",
                    ACTION_ORIGIN: "origin"}

#: [v4.2] status 双字段（规则 §2.10）：判定动作 → int status（1=成功/不过滤，
#: 0=失败/过滤）；原字符串流程态由 STATUS_BY_ACTION 迁入 process_status。
INT_STATUS_BY_ACTION = {ACTION_GENERATE: 1, ACTION_PENDING: 1,
                        ACTION_UNTRAINABLE: 0, ACTION_ISOLATED: 0,
                        ACTION_ORIGIN: 0}

#: 判定动作 → 工序动作（表1「标签 + 动作 + 去向」中的动作列）
PROCESSOR_ACTIONS = {ACTION_GENERATE: "keep",
                     ACTION_PENDING: "waiting_manual",
                     ACTION_UNTRAINABLE: "list_separately",
                     ACTION_ISOLATED: "skip",
                     ACTION_ORIGIN: "origin_quarantine"}

DESTINATION_BY_ACTION = {ACTION_GENERATE: "samples_file",
                         ACTION_PENDING: "pipeline_wait",
                         ACTION_UNTRAINABLE: "untrainable_list",
                         ACTION_ISOLATED: "sample_isolation",
                         ACTION_ORIGIN: "origin_isolation"}

DESTINATION_SAMPLES = DESTINATION_BY_ACTION[ACTION_GENERATE]
DESTINATION_PIPELINE_WAIT = DESTINATION_BY_ACTION[ACTION_PENDING]
DESTINATION_UNTRAINABLE = DESTINATION_BY_ACTION[ACTION_UNTRAINABLE]
DESTINATION_ISOLATION = DESTINATION_BY_ACTION[ACTION_ISOLATED]
DESTINATION_ORIGIN = DESTINATION_BY_ACTION[ACTION_ORIGIN]

# --------------------------------------------------------------------------- #
# 原因码
# --------------------------------------------------------------------------- #

REASON_TOKEN_GENERATED = "token_generated"
REASON_MISSING_IDENTIFIER = "missing_field:identifier"
REASON_UNSAFE_MEDIA_PATH = upstream.REASON_UNSAFE_MEDIA_PATH
REASON_NO_QUALITY_CONCLUSION = "no_quality_conclusion"
REASON_UPSTREAM_ISOLATED = "upstream_isolated"
REASON_LICENSE_NOT_TRAINABLE = "license_not_trainable"
REASON_LICENSE_MISSING = "license_missing"
REASON_MISSING_TEXT = "missing_text"
REASON_NO_APPLICABLE_ROUTE = "no_applicable_route"
REASON_ROUTE_DISABLED = "route_disabled"
REASON_MARKER_VALIDATION_FAILED = "marker_validation_failed"
REASON_JSON_PARSE = "json_parse_error"
REASON_NOT_OBJECT = "record_not_object"
REASON_ENCODING = "encoding_error"

# --------------------------------------------------------------------------- #
# 标签（表1「标签 + 动作 + 去向」）
# --------------------------------------------------------------------------- #

LABEL_GENERATED = "样本-样本构建：已生成Token"
LABEL_PENDING_QUALITY = "流程-待质量结论"
LABEL_PENDING_TEXT = "流程-待补文本"
LABEL_PENDING_ROUTE_DISABLED = "流程-路线未开启"
LABEL_PENDING_NO_ROUTE = "流程-无适用路线"
LABEL_PENDING_LICENSE = "流程-待补授权"
LABEL_UNTRAINABLE = "样本-不可训练"
LABEL_ISOLATED = "样本-已隔离"

LABEL_ORIGIN = {
    REASON_MISSING_IDENTIFIER: "原始文件-缺字段",
    REASON_UNSAFE_MEDIA_PATH: "原始文件-路径非法",
    REASON_JSON_PARSE: "原始文件-无法解析",
    REASON_NOT_OBJECT: "原始文件-无法解析",
    REASON_ENCODING: "原始文件-编码异常",
}
ORIGIN_LABELS = LABEL_ORIGIN

#: 原始隔离条目的工序动作
ORIGIN_ACTIONS = {REASON_MISSING_IDENTIFIER: "origin_quarantine",
                  REASON_UNSAFE_MEDIA_PATH: "origin_quarantine",
                  REASON_JSON_PARSE: "reject",
                  REASON_NOT_OBJECT: "reject",
                  REASON_ENCODING: "reject"}

MAX_RAW_CHARS = 512
MAX_RECORD_CHARS = 32768

#: 授权结论视为「可训练」/「不可训练」的取值（大小写/空白不敏感）
TRAINABLE_LICENSE_VALUES = {"可训练", "可训练使用", "允许训练", "可商用可训练",
                            "trainable", "train", "yes", "true", "allowed"}
UNTRAINABLE_LICENSE_VALUES = {"不可训练", "禁止训练", "不允许训练", "不可用于训练",
                              "仅可评估", "untrainable", "not_trainable", "no",
                              "false", "denied", "restricted"}
UNTRAINABLE_LICENSE_VALUE_LOOKUP = {item.lower()
                                    for item in UNTRAINABLE_LICENSE_VALUES}


# --------------------------------------------------------------------------- #
# 模态标记与 Token 文本
# --------------------------------------------------------------------------- #

def _markers_config(config: Any) -> Dict[str, str]:
    markers = getattr(config, "markers", names.MARKERS)
    if hasattr(markers, "as_dict"):
        return markers.as_dict()
    return dict(markers)


def build_markers(modalities: Sequence[str], config: Any) -> List[str]:
    """按 ``<image>`` → ``<audio>`` → ``<video>`` 的固定顺序生成标记列表。"""
    markers_config = _markers_config(config)
    present = set(modalities or ())
    return [markers_config[modality] for modality in MEDIA_MODALITIES
            if modality in present]


def build_token_text(markers: Sequence[str], text: str) -> str:
    """Token 文本 = 模态标记（按固定顺序）+ 文本内容。"""
    parts = [str(item) for item in markers if str(item).strip()]
    body = (text or "").strip()
    if body:
        parts.append(body)
    return " ".join(parts)


def validate_markers(markers: Sequence[str],
                     modalities: Sequence[str]) -> Dict[str, Any]:
    """校验模态标记与记录声明模态一致（F-ALIGN-059 模态标记与词元引用校验）。"""
    declared = [str(item) for item in markers]
    present = set(modalities or ())
    required = [names.MARKERS[modality] for modality in MEDIA_MODALITIES
                if modality in present]
    missing = [item for item in required if item not in declared]
    unexpected = [item for item in declared if item not in required]
    duplicated = len(set(declared)) != len(declared)
    return {"valid": (declared == required) and not duplicated,
            "declared": declared, "required": required,
            "missing": missing, "unexpected": unexpected, "duplicated": duplicated}


def _marker_modality(marker: str) -> Optional[str]:
    for modality, value in names.MARKERS.items():
        if value == marker:
            return modality
    return None


# --------------------------------------------------------------------------- #
# 路线
# --------------------------------------------------------------------------- #

def applicable_routes(modalities: Sequence[str],
                      local_annotation: bool) -> Tuple[str, ...]:
    """按模态与局部标注证据推导适用路线（顺序固定）。"""
    present = set(modalities or ())
    has_text = "text" in present
    routes: List[str] = []
    if has_text and "image" in present:
        routes.append(ROUTE_IMAGE_TEXT)
    if has_text and "audio" in present:
        routes.append(ROUTE_AUDIO_TEXT)
    if has_text and "video" in present:
        routes.append(ROUTE_VIDEO_TEXT)
    has_media = any(modality in present for modality in MEDIA_MODALITIES)
    # 局部标注路线依赖 #17/#18 的标注证据 + 至少一种媒体模态（文本由上游单独校验）
    if local_annotation and has_media:
        routes.append(ROUTE_LOCAL_ANNOTATION)
    return tuple(routes)


def route_files(config: Any) -> Dict[str, str]:
    """已开启路线 → 分文件文件名（未开启路线不产出对应文件）。"""
    return {route: ROUTE_FILES[route] for route in config.enabled_routes}


# --------------------------------------------------------------------------- #
# 单一记录判定
# --------------------------------------------------------------------------- #

def _outcome(action: str, reason: str, label: str, detail: str,
             spec: Optional[Dict[str, Any]] = None, **extra: Any) -> Dict[str, Any]:
    result: Dict[str, Any] = {
        "action": action,
        "status": INT_STATUS_BY_ACTION[action],            # [v4.2] int status
        "process_status": STATUS_BY_ACTION[action],        # [v4.2] 字符串流程态
        "decision": action,
        "processor_action": PROCESSOR_ACTIONS[action],
        "destination": DESTINATION_BY_ACTION[action],
        "reason": reason,
        "label": label,
        "label_detail": None,
        "detail": detail,
        "routes": [],
        "applicable_routes": [],
        "primary_route": None,
        "markers": [],
        "token_text": None,
        "marker_validation": {},
        "media_links": [],
        "modalities": list((spec or {}).get("modalities") or []),
        "identifier": (spec or {}).get("identifier"),
        "sample_id": (spec or {}).get("sample_id"),
        "pair_id": (spec or {}).get("pair_id"),
    }
    result.update(extra)
    return result


def _link_entries(spec: Dict[str, Any],
                  link_status: Optional[Dict[str, bool]]) -> List[Dict[str, Any]]:
    """把媒体引用转成「对象存储链接」条目（**只存链接，不搬运文件**）。"""
    status = link_status or {}
    entries: List[Dict[str, Any]] = []
    for item in spec.get("media_links") or []:
        path = item["path"]
        found = status.get(path)
        local_presence = "unknown" if found is None else ("present" if found else "absent")
        entries.append({
            "modality": item["modality"],
            "path": path,
            "object_key": posixpath.join("crossmodal",
                                         str(spec.get("identifier") or "unknown"), path),
            "storage": "object_storage_reference",
            "link_status": "reference_only",
            "local_presence": local_presence,
        })
    return entries


def evaluate_record(spec: Dict[str, Any], *, config: Any, event_time: str,
                    link_status: Optional[Dict[str, bool]] = None) -> Dict[str, Any]:
    """表1 #22 单条判定：生成 / 待处理 / 单独列出 / 跳过隔离 / 原始隔离。"""
    # 1) 结构：缺标识
    if not spec.get("identifier"):
        return _outcome(ACTION_ORIGIN, REASON_MISSING_IDENTIFIER,
                        LABEL_ORIGIN[REASON_MISSING_IDENTIFIER],
                        "既无 sample_id 也无 pair_id，进入原始隔离区", spec)
    # 2) 路径安全：非法媒体引用
    if spec.get("unsafe_media"):
        bad = spec["unsafe_media"][0]
        return _outcome(ACTION_ORIGIN, REASON_UNSAFE_MEDIA_PATH,
                        LABEL_ORIGIN[REASON_UNSAFE_MEDIA_PATH],
                        "媒体引用不是可搬运的相对路径：%s" % bad.get("path"), spec)

    isolation = spec.get("isolation") or {}
    quality = spec.get("quality") or {}

    # 3) 隔离区样本：不进入样本文件
    if isolation.get("isolated"):
        return _outcome(ACTION_ISOLATED, REASON_UPSTREAM_ISOLATED, LABEL_ISOLATED,
                        isolation.get("detail") or "上游已隔离，不生成 Token", spec,
                        isolation=isolation)
    if quality.get("outcome") == "isolated":
        return _outcome(ACTION_ISOLATED, REASON_UPSTREAM_ISOLATED, LABEL_ISOLATED,
                        quality.get("detail") or "质量结论为隔离，不生成 Token", spec,
                        quality=quality)

    # 4) 没有质量结论：不拦、留产线、不生成
    if quality.get("outcome") != "accepted":
        return _outcome(ACTION_PENDING, REASON_NO_QUALITY_CONCLUSION,
                        LABEL_PENDING_QUALITY,
                        quality.get("detail") or "记录中不存在可判定的质量结论", spec,
                        quality=quality)

    # 5) 授权：缺失 → 待补；不可训练 → 单独列出
    license_value = spec.get("license")
    if not license_value:
        return _outcome(ACTION_PENDING, REASON_LICENSE_MISSING, LABEL_PENDING_LICENSE,
                        "缺少授权结论，无法确认可训练", spec)
    normalized_license = str(license_value).strip().lower()
    if normalized_license in {item.lower() for item in UNTRAINABLE_LICENSE_VALUE_LOOKUP}:
        # 规则 §2.1/§2.3：授权不可训练属业务问题，去向为样本隔离库；
        # 表1 #22 要求「不能训练的样本单独列出」→ 写入 22_untrainable.jsonl 并标注建议去向。
        return _outcome(ACTION_UNTRAINABLE, REASON_LICENSE_NOT_TRAINABLE,
                        LABEL_UNTRAINABLE,
                        "授权结论为「%s」，不能训练，单独列出" % license_value, spec,
                        license=license_value, label_detail="样本-敏感",
                        recommended_destination="sample_quarantine")
    if normalized_license not in {item.lower() for item in TRAINABLE_LICENSE_VALUES}:
        return _outcome(ACTION_PENDING, REASON_LICENSE_MISSING, LABEL_PENDING_LICENSE,
                        "授权结论「%s」无法判定为可训练" % license_value, spec,
                        license=license_value)

    # 6) 文本
    text_spec = spec.get("text") or {}
    if not text_spec.get("content"):
        return _outcome(ACTION_PENDING, REASON_MISSING_TEXT, LABEL_PENDING_TEXT,
                        "缺少对齐文本，无法生成 Token", spec)

    modalities = tuple(spec.get("modalities") or ())
    local = spec.get("local_annotation") or {}
    applicable = applicable_routes(modalities, bool(local.get("present")))
    # 7) 无适用路线
    if not applicable:
        return _outcome(ACTION_PENDING, REASON_NO_APPLICABLE_ROUTE,
                        LABEL_PENDING_NO_ROUTE,
                        "记录模态 %s 不构成任何已知对齐路线" % (list(modalities),), spec,
                        applicable_routes=[])
    # 8) 路线未开启则不产出对应文件
    enabled = tuple(route for route in applicable if config.route_enabled(route))
    if not enabled:
        return _outcome(ACTION_PENDING, REASON_ROUTE_DISABLED,
                        LABEL_PENDING_ROUTE_DISABLED,
                        "适用的路线 %s 均未开启，不产出对应文件" % (list(applicable),),
                        spec, applicable_routes=list(applicable))

    # 9) 模态标记与词元引用校验
    markers = build_markers(modalities, config)
    validation = validate_markers(markers, modalities)
    links = _link_entries(spec, link_status)
    linked_modalities = {item["modality"] for item in links}
    reference_missing = [item for item in markers
                         if _marker_modality(item) not in linked_modalities]
    validation["reference_ok"] = not reference_missing
    validation["reference_missing"] = reference_missing
    if not validation["valid"]:
        return _outcome(ACTION_PENDING, REASON_MARKER_VALIDATION_FAILED,
                        LABEL_PENDING_NO_ROUTE,
                        "模态标记与声明模态不一致：%s" % validation, spec,
                        marker_validation=validation)
    if reference_missing:
        # 声明了模态但没有可记录的链接：显式标出，仍按链接策略生成，绝不伪造文件
        validation["reference_note"] = "声明模态缺少引用链接，仅登记标记"

    token_text = build_token_text(markers, text_spec["content"])
    primary = next((route for route in ROUTE_ORDER if route in enabled), enabled[0])
    return _outcome(ACTION_GENERATE, REASON_TOKEN_GENERATED, LABEL_GENERATED,
                    "生成 Token（路线 %s），原文件仅保留对象存储链接"
                    % ",".join(enabled), spec,
                    routes=list(enabled), applicable_routes=list(applicable),
                    primary_route=primary, markers=markers, token_text=token_text,
                    marker_validation=validation, media_links=links, quality=quality,
                    license=license_value, version=spec.get("version"),
                    evaluated_at=event_time,
                    text_reference={key: text_spec.get(key)
                                    for key in ("field", "length", "sha256")})


# --------------------------------------------------------------------------- #
# 产物构造
# --------------------------------------------------------------------------- #

def _min_spec(payload: Dict[str, Any], outcome: Dict[str, Any]) -> Dict[str, Any]:
    metadata = payload.get("metadata") if isinstance(payload.get("metadata"), dict) else {}
    return {"identifier": outcome.get("identifier") or payload.get("sample_id"),
            "sample_id": payload.get("sample_id"),
            "pair_id": payload.get("pair_id"),
            "modalities": outcome.get("modalities") or payload.get("modalities") or [],
            "batch_id": metadata.get("batch_id"),
            "retry_count": metadata.get("retry_count")}


def _own_node(spec: Dict[str, Any], outcome: Dict[str, Any],
              route_files_map: Optional[Dict[str, str]]) -> Dict[str, Any]:
    quality = outcome.get("quality") or {}
    marker_validation = dict(outcome.get("marker_validation") or {})
    token_text = outcome.get("token_text")
    return {
        "operator": OPERATOR,
        "operator_label": OPERATOR_LABEL,
        "operator_number": OPERATOR_NUMBER,
        "operator_version": OPERATOR_VERSION,
        "stage_id": STAGE_ID,
        "run_stage": "sample_build",
        "source_reference": "operator_22",
        "identifier": spec.get("identifier"),
        "sample_id": spec.get("sample_id"),
        "pair_id": spec.get("pair_id"),
        "status": outcome["status"],
        "process_status": outcome.get("process_status"),  # [v4.2] 字符串流程态
        "decision": outcome["decision"],
        "action": outcome["processor_action"],
        "destination": outcome["destination"],
        "reason_code": outcome["reason"],
        "reason": outcome["detail"],
        "label": outcome["label"],
        "label_detail": outcome.get("label_detail"),
        "batch_id": spec.get("batch_id") or _batch_id(spec),
        "retry_count": spec.get("retry_count") if isinstance(spec.get("retry_count"), int)
        else _retry_count(spec),
        "event_time": outcome.get("evaluated_at"),
        "route": outcome.get("primary_route"),
        "routes": list(outcome.get("routes") or []),
        "applicable_routes": list(outcome.get("applicable_routes") or []),
        "route_files": dict(route_files_map or {}),
        "samples_file": SAMPLES_FILE_NAME,
        "modalities": list(spec.get("modalities") or []),
        "markers": list(outcome.get("markers") or []),
        "token_text": token_text,
        "token_char_count": len(token_text or ""),
        "marker_validation": marker_validation,
        "text_reference": outcome.get("text_reference"),
        "media_links": list(outcome.get("media_links") or []),
        "media_link_policy": "original_files_link_only",
        "media_copied": False,
        "object_storage_connected": False,
        "quality_conclusion": {"outcome": quality.get("outcome"),
                               "source": quality.get("source"),
                               "status": quality.get("status"),
                               "decision": quality.get("decision"),
                               "flagged": bool(quality.get("flagged"))},
        "license": outcome.get("license"),
        "version": outcome.get("version"),
        "version_state_changed": False,
        "compressed": False,
        "packaged": False,
        "human_verified": False,
        "review_required": True,
        "subword_tokenizer": None,
        "generated_at": outcome.get("evaluated_at"),
    }


def _own_summary(node: Dict[str, Any]) -> Dict[str, Any]:
    keys = ("operator", "operator_number", "operator_version", "stage_id",
            "source_reference", "identifier", "sample_id", "status", "process_status",
            "decision",
            "action", "destination", "reason_code", "label", "label_detail",
            "route", "routes", "markers", "token_char_count", "media_link_policy",
            "version", "version_state_changed", "compressed", "human_verified",
            "review_required", "generated_at")
    summary = {key: node.get(key) for key in keys}
    summary["modalities"] = node.get("modalities")
    return summary


def build_sample_record(payload: Dict[str, Any], outcome: Dict[str, Any],
                        route_files: Optional[Dict[str, str]] = None) -> Dict[str, Any]:
    """样本文件行 = 原记录（全部字段原样保留）+ ``processing["22"]`` + ``operator_22``。"""
    sample = copy.deepcopy(payload)
    if not isinstance(sample.get("processing"), dict):
        sample["processing"] = {}
    node = _own_node(_min_spec(payload, outcome), outcome, route_files)
    # 交付清单携带本算子真实生成的Token，#23无需借用上游分词或重做生成。
    if outcome.get('token_text'):
        sample['token'] = outcome['token_text']
    sample["processing"]["22"] = node
    sample["operator_22"] = _own_summary(node)
    return sample


def build_token_row(payload: Dict[str, Any], outcome: Dict[str, Any], *,
                    event_time: str) -> Dict[str, Any]:
    """Token 文件行：模态标记 + 文本 + 词元引用校验结果（不产出子词 ID）。"""
    token_text = outcome.get("token_text") or ""
    return {
        "sample_id": payload.get("sample_id"),
        "identifier": outcome.get("identifier"),
        "pair_id": payload.get("pair_id"),
        "route": outcome.get("primary_route"),
        "routes": list(outcome.get("routes") or []),
        "modalities": list(outcome.get("modalities") or []),
        "markers": list(outcome.get("markers") or []),
        "token_text": token_text,
        "token_char_count": len(token_text),
        "token_text_sha256": hashlib.sha256(token_text.encode("utf-8")).hexdigest(),
        "text_reference": dict(outcome.get("text_reference") or {}),
        "media_links": list(outcome.get("media_links") or []),
        "marker_validation": dict(outcome.get("marker_validation") or {}),
        "subword_token_ids": None,
        "tokenizer": None,
        "license": outcome.get("license"),
        "version": outcome.get("version"),
        "version_state_changed": False,
        "compressed": False,
        "operator": OPERATOR,
        "operator_number": OPERATOR_NUMBER,
        "operator_version": OPERATOR_VERSION,
        "source_reference": "operator_22",
        "human_verified": False,
        "review_required": True,
        "generated_at": event_time,
    }


def _alignment_relation(payload: Dict[str, Any]) -> Optional[str]:
    for path in (("alignment", "whole", "decision"), ("alignment", "relation"),
                 ("alignment_relation",)):
        node: Any = payload
        for key in path:
            if not isinstance(node, dict):
                node = None
                break
            node = node.get(key)
        if isinstance(node, str) and node.strip():
            return node.strip()
    return None


def build_metadata_row(payload: Dict[str, Any], outcome: Dict[str, Any], *,
                       event_time: str) -> Dict[str, Any]:
    """元数据索引行（FGW：元数据与文件分离存储，只存访问链接与模态索引）。"""
    metadata = payload.get("metadata") if isinstance(payload.get("metadata"), dict) else {}
    links = outcome.get("media_links") or []
    token_text = outcome.get("token_text") or ""
    return {
        "sample_id": payload.get("sample_id"),
        "identifier": outcome.get("identifier"),
        "pair_id": payload.get("pair_id"),
        "modalities": list(outcome.get("modalities") or []),
        "route": outcome.get("primary_route"),
        "routes": list(outcome.get("routes") or []),
        "alignment_relation": _alignment_relation(payload),
        "quality_label": (outcome.get("quality") or {}).get("label"),
        "quality_outcome": (outcome.get("quality") or {}).get("outcome"),
        "license": outcome.get("license"),
        "version": outcome.get("version"),
        "version_state_changed": False,
        "batch_id": metadata.get("batch_id"),
        "source": metadata.get("source"),
        "collected_at": metadata.get("collected_at") or metadata.get("captured_at"),
        "modalities_index": sorted({item["modality"] for item in links}),
        "object_links": [{"modality": item["modality"], "path": item["path"],
                          "object_key": item["object_key"], "storage": item["storage"],
                          "link_status": item["link_status"],
                          "local_presence": item["local_presence"]} for item in links],
        "token_text_sha256": hashlib.sha256(token_text.encode("utf-8")).hexdigest(),
        "operator": OPERATOR,
        "operator_number": OPERATOR_NUMBER,
        "operator_version": OPERATOR_VERSION,
        "source_reference": "operator_22",
        "generated_at": event_time,
    }


# --------------------------------------------------------------------------- #
# 台账与原始隔离条目
# --------------------------------------------------------------------------- #

def _metadata_value(spec: Dict[str, Any], key: str) -> Any:
    view = spec.get("view") or {}
    metadata = view.get("metadata") if isinstance(view.get("metadata"), dict) else {}
    value = metadata.get(key)
    if value is None:
        value = view.get(key)
    return value


def _retry_count(spec: Dict[str, Any]) -> int:
    value = _metadata_value(spec, "retry_count")
    return value if isinstance(value, int) and value >= 0 else 0


def _batch_id(spec: Dict[str, Any]) -> Optional[str]:
    for key in ("batch_id", "batchId", "batch"):
        value = _metadata_value(spec, key)
        if isinstance(value, str) and value.strip():
            return value.strip()
    return None


def ledger_fields(spec: Dict[str, Any], outcome: Dict[str, Any],
                  detected_at: str) -> Dict[str, Any]:
    """台账公共字段（规则 §2.3：label / stage·operator / action / destination /
    event_time / batch_id / sample_id / retry_count，另加本算子追溯字段）。"""
    return {
        "identifier": spec.get("identifier"),
        "sample_id": spec.get("sample_id"),
        "pair_id": spec.get("pair_id"),
        "operator": OPERATOR,
        "operator_number": OPERATOR_NUMBER,
        "operator_version": OPERATOR_VERSION,
        "stage_id": STAGE_ID,
        "stage": STAGE_ID,
        "source_reference": "operator_22",
        "status": outcome["status"],
        "process_status": outcome.get("process_status"),  # [v4.2] 字符串流程态
        "decision": outcome["decision"],
        "action": outcome["processor_action"],
        "destination": outcome["destination"],
        "recommended_destination": outcome.get("recommended_destination"),
        "reason": outcome["reason"],
        "label": outcome["label"],
        "label_detail": outcome.get("label_detail"),
        "detail": outcome["detail"],
        "routes": list(outcome.get("routes") or []),
        "applicable_routes": list(outcome.get("applicable_routes") or []),
        "batch_id": _batch_id(spec),
        "human_verified": False,
        "review_required": True,
        "retry_count": _retry_count(spec),
        "event_time": detected_at,
        "detected_at": detected_at,
    }


def _truncate_record(payload: Any) -> Any:
    try:
        text = json.dumps(payload, ensure_ascii=False)
    except (TypeError, ValueError):
        return {"_truncated": True, "reason": "record_not_serializable"}
    if len(text) <= MAX_RECORD_CHARS:
        return payload
    summary: Dict[str, Any] = {"_truncated": True, "original_chars": len(text)}
    if isinstance(payload, dict):
        for key in ("sample_id", "pair_id", "identifier"):
            if payload.get(key) is not None:
                summary[key] = payload.get(key)
    return summary


def make_ledger_entry(*, line: int, spec: Dict[str, Any], outcome: Dict[str, Any],
                      detail: str, detected_at: str) -> Dict[str, Any]:
    """旁路台账条目：携带原记录（超限截断）以便追溯，不静默丢弃。"""
    entry: Dict[str, Any] = {"line": int(line)}
    entry.update(ledger_fields(spec, outcome, detected_at))
    entry["detail"] = detail
    entry["record"] = _truncate_record(spec.get("view") or {})
    return entry


def make_reject_entry(*, line: int, reason: str, detail: str, detected_at: str,
                      raw_text: Optional[str] = None,
                      identifier: Optional[str] = None) -> Dict[str, Any]:
    """原始隔离条目：不携带 record，只保留行号、原因与截断的原始行。"""
    entry: Dict[str, Any] = {
        "line": int(line),
        "identifier": identifier,
        "sample_id": identifier,
        "pair_id": None,
        "operator": OPERATOR,
        "operator_number": OPERATOR_NUMBER,
        "operator_version": OPERATOR_VERSION,
        "stage_id": STAGE_ID,
        "source_reference": "operator_22",
        "status": INT_STATUS_BY_ACTION[ACTION_ORIGIN],            # [v4.2] int status
        "process_status": STATUS_BY_ACTION[ACTION_ORIGIN],        # [v4.2] 字符串流程态
        "decision": ACTION_ORIGIN,
        "action": ORIGIN_ACTIONS.get(reason, "origin_quarantine"),
        "destination": DESTINATION_ORIGIN,
        "reason": reason,
        "label": LABEL_ORIGIN.get(reason, "原始文件-无法解析"),
        "label_detail": None,
        "detail": detail,
        "human_verified": False,
        "review_required": True,
        "detected_at": detected_at,
    }
    if raw_text is not None:
        entry["raw"] = raw_text[:MAX_RAW_CHARS]
    return entry
