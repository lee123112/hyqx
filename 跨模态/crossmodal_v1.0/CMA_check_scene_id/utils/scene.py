"""算子 #02 核验场景编号 业务模块（表1 冻结口径的纯规则实现）。

表1 #02 口径（工具类型=系统功能，模型列 ``--``，默认关闭，P1）：
1. **默认关闭，配对集任务不执行**：``enabled=false`` 时本算子不核验也不补场景编号，
   记录原样继续交给 #3（改走配对集），不隔离、不改写上游字段。
2. **融合集任务打开后**：有场景编号则核验同一编号下至少 ``min_modalities``（默认 2，
   可选 2–4）种模态；无编号且配置为「本产线补一个编号」时补一个编号并**只绑定一次**。
3. **设备并发和故障切换只读采集平台结果**：只原样抄录平台字段，不调度设备、
   不新建视频流、不做并发编排。
4. **不做质量拦截**：缺场景编号或模态不足的样本留在产线中、改走配对集，
   **绝不因为缺场景编号把样本送进隔离区**；原始文件问题（坏 JSON / 非对象 / 缺字段）
   仍按通用契约进原始隔离区。

本模块不加载任何模型、不产生任何分数、不联网、不写死结论。
"""
from __future__ import annotations

import hashlib
import json
import re
from typing import Any, Dict, List, Optional, Sequence, Tuple

from utils import logger as log_utils

#: 远端链接（http/oss/s3/ftp 等）：不是本地相对路径，只记录不搬运，也不算错误
_REMOTE_SCHEME_RE = re.compile(r"^[a-zA-Z][a-zA-Z0-9+.\-]*://")
_SCHEME_ONLY_RE = re.compile(r"^(oss|s3|gs|hdfs|ftp)://")

# ---------------------------------------------------------------- 身份与常量
#: 算子正式名（血缘/台账使用）
OPERATOR_SLUG = "scene_id_verification"
#: 表1 正式编号
FORMAL_OPERATOR_NUMBER = 2
#: 阶段归属：表1 环节「输入核验」（#1–#3）在 demo 五阶段中记为 S1
DEFAULT_STAGE_ID = "S1"

#: 四种模态（与 #11 生成候选配对的模态顺序一致）
MODALITY_ORDER = ("text", "image", "video", "audio")

#: 表1 页面「同一场景至少几种模态」默认值与可选范围
DEFAULT_MIN_MODALITIES = 2
MIN_MODALITIES_RANGE = (2, 4)

#: 表1 页面「场景编号用谁的」（配置控件取值）
SCENE_SOURCE_PLATFORM = "platform"      # 沿用采集平台已有编号（默认显示）
SCENE_SOURCE_GENERATE = "generate"      # 本产线补一个编号
SCENE_ID_SOURCES = (SCENE_SOURCE_PLATFORM, SCENE_SOURCE_GENERATE)

#: 编号来源（输出/绑定侧取值）：补出来的编号写作 supplemented，
#: 与配置控件取值 generate 区分，便于下游识别该编号不是采集平台给的（#11 只看有无）。
SCENE_SOURCE_SUPPLEMENTED = "supplemented"

#: 编号来源（输出侧）额外的两个取值
SOURCE_NONE = "none"                    # 平台未提供且未开启补编号
SOURCE_DISABLED = "disabled"            # 本算子未启用

#: 记录级状态
STATUS_VERIFIED = "verified"                     # 场景组模态数达标
STATUS_INSUFFICIENT = "insufficient_modalities"  # 场景组模态数不足
STATUS_NO_SCENE_ID = "no_scene_id"               # 无场景编号（沿用平台编号模式）
STATUS_DISABLED = "disabled"                     # 工具未启用

# [v4.2] status 双字段（§2.10）：输出记录 int status（本算子处理型：1=核验通过/正常流程态，
# 0=未通过/不适用）+ 原字符串迁入 process_status
_STATUS_AS_INT = {
    STATUS_VERIFIED: 1,
    STATUS_INSUFFICIENT: 0,
    STATUS_NO_SCENE_ID: 0,
    STATUS_DISABLED: 0,
}


def status_int(value: Any) -> int:
    """记录级字符串状态 → [v4.2] int status；未登记的一律按正常流程态 1。"""
    return _STATUS_AS_INT.get(value, 1)

#: 处置路由
ROUTE_FUSION = "fusion"        # 融合集场景组 → 交 #3
ROUTE_PAIRING = "pairing"      # 改走配对集 → 交 #3
ROUTE_ORIGIN = "origin"        # 原始隔离区（原始文件问题，非质量拦截）
ROUTE_REJECT = "reject"        # 算子异常清单（流程状态，不隔离）

#: 原因码
REASON_INSUFFICIENT = "insufficient_modalities"
REASON_NO_SCENE_ID = "no_scene_id"
REASON_TOOL_DISABLED = "tool_disabled"
REASON_DUPLICATE = "duplicate_sample_id"
REASON_MISSING_SAMPLE_ID = "missing_field: sample_id"
REASON_JSON_PARSE_ERROR = "json_parse_error"
REASON_RECORD_NOT_OBJECT = "record_not_object"

#: 统一台账标签（背景规则 §2.3）
LABEL_BAD_JSON = "原始文件-无法解析"
LABEL_MISSING_FIELD = "原始文件-缺字段"
LABEL_DUPLICATE = "流程-重复样本ID"
LABEL_VERIFIED = "流程-场景编号已核验"
LABEL_TO_PAIRING = "流程-改走配对集"
LABEL_DISABLED = "流程-场景核验未启用"

#: 统一台账去向
DESTINATION_RAW_ISOLATION = "raw_isolation"
DESTINATION_OPERATOR_REJECTS = "operator_rejects"
DESTINATION_FUSION_GROUP = "fusion_scene_group"
DESTINATION_PAIRING_ROUTE = "pairing_route"

#: 台账动作文案
ACTION_QUARANTINE_RAW = "记录并隔离到原始隔离区"
ACTION_RECORD_REJECT = "记录到算子异常清单（不隔离）"
ACTION_BIND_FUSION = "写入场景编号并归入融合集场景组"
ACTION_ROUTE_PAIRING = "保留在产线中并改走配对集"
ACTION_SKIP_DISABLED = "工具未启用，不核验不改写"

#: 设备控制只读口径（表1 冻结，只读项）
DEVICE_CONTROL_READ_ONLY = "read_only"

#: 场景编号相关命名
GROUP_ID_PREFIX = "grp-scene-"
GROUP_ID_MAX_LEN = 128
GENERATED_SCENE_PREFIX = "scene-auto-"
GENERATED_SCENE_DIGEST_LEN = 12

#: 采集平台只读字段（本算子只抄录，不调度设备）
CAPTURE_PLATFORM_KEYS = ("capture_platform", "capturePlatform")
DEVICE_CONCURRENCY_KEYS = ("device_concurrency", "deviceConcurrency")
DEVICE_FAILOVER_KEYS = ("device_failover", "deviceFailover")

#: 模态别名：上游可能用 img/vid/voice 或扩展名表达模态（#01 文件登记项口径）
MODALITY_ALIASES = {
    "text": "text", "txt": "text", "caption": "text", "captions": "text",
    "description": "text", "content": "text", "transcript": "text", "asr": "text",
    "image": "image", "img": "image", "picture": "image", "photo": "image",
    "jpg": "image", "jpeg": "image", "png": "image", "bmp": "image",
    "gif": "image", "webp": "image",
    "video": "video", "vid": "video", "mp4": "video", "mov": "video",
    "avi": "video", "mkv": "video", "frame": "video", "keyframe": "video",
    "audio": "audio", "aud": "audio", "voice": "audio", "speech": "audio",
    "wav": "audio", "mp3": "audio", "flac": "audio", "m4a": "audio",
}

#: 文件登记项里的文件指针键（#01 `01_workitems.jsonl` 的 files[].path/link）
FILE_ENTRY_PATH_KEYS = ("path", "link", "file_link", "fileLink", "uri", "file", "url")

#: 单文件记录的模态兜底取值键
FALLBACK_MODALITY_KEYS = ("modality", "modalities", "type")

#: 场景编号解析顺序（与 #11 生成候选配对一致：#11 读 scene_id/group_id 成组）
SCENE_ID_KEYS = ("scene_id", "sceneId", "group_id", "groupId", "scene")

#: 产物文件名
MAIN_NAME = "02_scene_verified.jsonl"
GROUPS_NAME = "02_scene_groups.jsonl"
PAIRING_NAME = "02_routed_to_pairing.jsonl"
ORIGIN_NAME = "02_origin_quarantine.jsonl"
REJECT_NAME = "02_rejects.jsonl"
REPORT_NAME = "02_scene_report.json"

#: 主产物与配套产物（顺序即 manifest 中登记顺序）
OUTPUT_NAMES = (MAIN_NAME, GROUPS_NAME, PAIRING_NAME, ORIGIN_NAME, REJECT_NAME)

#: 业务产物 → manifest 角色
ARTIFACT_ROLES = {
    MAIN_NAME: "main",
    GROUPS_NAME: "sidecar",
    PAIRING_NAME: "sidecar",
    ORIGIN_NAME: "review",
    REJECT_NAME: "review",
    REPORT_NAME: "review",
}


class DependencyError(RuntimeError):
    """外部依赖（模型/平台）不可用且不允许降级。本算子无外部依赖，仅作契约占位。"""


# --------------------------------------------------------------------- 基础
def _extract_key(value: Any) -> Optional[str]:
    """把非空值转成稳定的字符串键；空串/空白/空容器视为不存在。"""
    if not is_present(value):
        return None
    text = str(value).strip()
    return text or None


def is_present(value: Any) -> bool:
    """判断字段是否有效存在；``null``、空串、空白、空容器视为不存在。"""
    if value is None:
        return False
    if isinstance(value, str):
        return bool(value.strip())
    if isinstance(value, dict):
        return any(is_present(item) for item in value.values())
    if isinstance(value, (list, tuple, set)):
        return len(value) > 0
    return True


def modalities_of(record: Dict[str, Any]) -> List[str]:
    """按 text→image→video→audio 返回本记录的模态列表。

    兼容两种上游形态（并集取值，两者都取不全时才走兜底）：
    - **样本级**（表1 #01 → #03 链路口径）：一条记录就是一个样本，
      模态写在顶层 ``text``/``image``/``video``/``audio``；
    - **文件登记项级**（demo 10.6 的 #01 `01_workitems.jsonl` 口径）：
      一条记录是一个文件条目，模态写在 ``files[].modality``（或由 ``files[].path``
      扩展名推断），同一 ``sample_id`` 会有多行。
    """
    found = {name for name in MODALITY_ORDER if is_present(record.get(name))}
    for modality, _path in file_references(record):
        if modality is not None:
            found.add(modality)
    if not found:
        for key in FALLBACK_MODALITY_KEYS:
            modality = _normalise_modality(record.get(key))
            if modality is not None:
                found.add(modality)
                break
    if not found:
        module = record.get("operator_01")
        if isinstance(module, dict):
            for key in FALLBACK_MODALITY_KEYS:
                modality = _normalise_modality(module.get(key))
                if modality is not None:
                    found.add(modality)
                    break
            else:
                modality = _normalise_modality(module.get("file_link"))
                if modality is not None:
                    found.add(modality)
    return [name for name in MODALITY_ORDER if name in found]


def _normalise_modality(value: Any) -> Optional[str]:
    """把 ``img``/``voice``/``a.png``/``["image"]`` 之类的写法归一成四种模态之一。"""
    if not is_present(value):
        return None
    if isinstance(value, (list, tuple, set)):
        for item in value:
            found = _normalise_modality(item)
            if found is not None:
                return found
        return None
    text = str(value).strip().lower()
    if text in MODALITY_ALIASES:
        return MODALITY_ALIASES[text]
    if "." in text:
        extension = text.rsplit(".", 1)[-1]
        if extension in MODALITY_ALIASES:
            return MODALITY_ALIASES[extension]
    return None


def file_entries(record: Dict[str, Any]) -> List[Dict[str, Any]]:
    """取 ``files`` 文件登记项列表（非列表或元素非对象时视为没有）。"""
    files = record.get("files")
    if isinstance(files, list):
        return [item for item in files if isinstance(item, dict)]
    return []


def file_references(record: Dict[str, Any]) -> List[Tuple[Optional[str], str]]:
    """返回 ``[(模态, 文件相对路径), ...]``；模态推断不出来时为 ``None``。"""
    references: List[Tuple[Optional[str], str]] = []
    for item in file_entries(record):
        path = None
        for key in FILE_ENTRY_PATH_KEYS:
            value = item.get(key)
            if isinstance(value, str) and value.strip():
                path = value.strip()
                break
        if path is None:
            continue
        modality = _normalise_modality(item.get("modality"))
        if modality is None:
            modality = _normalise_modality(path)
        references.append((modality, path))
    return references


def file_discriminator(record: Dict[str, Any]) -> Optional[str]:
    """同一 ``sample_id`` 的「文件级」区分键，用于区分重复登记与多文件对象。

    - 样本级记录（一条一个样本，无文件条目）返回 ``None`` → 同 ``sample_id`` 重复即异常；
    - 文件登记项级记录返回文件引用串 → 同一对象的多个**不同**文件不算重复，
      同一文件被重复登记才算重复。
    """
    paths = sorted(path for _modality, path in file_references(record))
    if paths:
        return "files:" + "|".join(paths)
    for key in ("file_link", "fileLink"):
        value = _extract_key(record.get(key))
        if value is not None:
            return "file:" + value
    module = record.get("operator_01")
    if isinstance(module, dict):
        value = _extract_key(module.get("file_link"))
        if value is not None:
            return "file:" + value
    return None


def _first_key(record: Dict[str, Any], keys: Sequence[str]) -> Optional[str]:
    for key in keys:
        found = _extract_key(record.get(key))
        if found is not None:
            return found
    metadata = record.get("metadata")
    if isinstance(metadata, dict):
        for key in keys:
            found = _extract_key(metadata.get(key))
            if found is not None:
                return found
    return None


def _first_raw(record: Dict[str, Any], keys: Sequence[str]) -> Any:
    """与 ``_first_key`` 相同，但保留原始值类型（用于只读抄录设备字段）。"""
    for key in keys:
        if is_present(record.get(key)):
            return record[key]
    metadata = record.get("metadata")
    if isinstance(metadata, dict):
        for key in keys:
            if is_present(metadata.get(key)):
                return metadata[key]
    return None


def is_remote_reference(value: Any) -> bool:
    """判断媒体引用是否为远端链接（http/oss/s3/... ）——只登记不搬运，也不算错误。"""
    if not isinstance(value, str):
        return False
    text = value.strip()
    return bool(_REMOTE_SCHEME_RE.match(text) or _SCHEME_ONLY_RE.match(text))


def resolve_platform_scene_id(record: Dict[str, Any]) -> Optional[str]:
    """取采集平台已有场景编号：``scene_id``/``sceneId``/``group_id``/``groupId``/``scene``。"""
    return _first_key(record, SCENE_ID_KEYS)


def resolve_bound_scene_id(record: Dict[str, Any]) -> Tuple[Optional[str], Optional[str]]:
    """读取本算子历史运行写入的绑定（``processing["02"]``），实现「只绑定一次」。

    返回 ``(scene_id, scene_source)``；未绑定过时返回 ``(None, None)``。
    """
    processing = record.get("processing")
    if not isinstance(processing, dict):
        return None, None
    node = processing.get("02")
    if not isinstance(node, dict):
        return None, None
    scene_id = _extract_key(node.get("scene_id"))
    if scene_id is None:
        return None, None
    source = node.get("scene_source")
    if source not in (SCENE_SOURCE_PLATFORM, SCENE_SOURCE_GENERATE,
                      SCENE_SOURCE_SUPPLEMENTED):
        return None, None
    return scene_id, source


def generate_scene_id(unique_id: str, sample_id: str) -> str:
    """本产线补的编号：由任务标识与样本标识确定性派生，不使用随机数。

    同一任务重复运行得到同一编号（只绑定一次、可复现）；不同样本/任务互不相同。
    """
    digest = hashlib.sha1(("%s|%s" % (unique_id, sample_id)).encode("utf-8")).hexdigest()
    return GENERATED_SCENE_PREFIX + digest[:GENERATED_SCENE_DIGEST_LEN]


def _sanitize_token(value: Any, fallback: str = "unknown") -> str:
    import re

    text = re.sub(r"[^0-9A-Za-z_.\-\u4e00-\u9fff]", "_", str(value).strip())
    return text[:GROUP_ID_MAX_LEN] or fallback


def group_id_for(scene_id: str) -> str:
    """场景组编号：``grp-scene-<scene_id>``（与 #11 生成候选配对同名口径）。"""
    return GROUP_ID_PREFIX + _sanitize_token(scene_id)


def capture_readonly(record: Dict[str, Any]) -> Dict[str, Any]:
    """只读抄录采集平台的并发与故障切换信息（本算子不做设备调度）。"""
    captured: Dict[str, Any] = {}
    platform = _first_raw(record, CAPTURE_PLATFORM_KEYS)
    concurrency = _first_raw(record, DEVICE_CONCURRENCY_KEYS)
    failover = _first_raw(record, DEVICE_FAILOVER_KEYS)
    if platform is not None:
        captured["capture_platform"] = platform
    if concurrency is not None:
        captured["device_concurrency"] = concurrency
    if failover is not None:
        captured["device_failover"] = failover
    return captured


# --------------------------------------------------------------------- 统计
def initial_statistics() -> Dict[str, Any]:
    """全部统计字段初始化；数值全部来自真实处理结果，不使用固定值。"""
    return {
        # 输入规模
        "input_lines": 0, "blank_lines": 0, "input_records": 0,
        "valid_records": 0, "origin_rejected": 0, "duplicate_records": 0,
        # 主产物与分流
        "main_entries": 0,
        "fusion_records": 0,
        "pairing_records": 0,
        "pairing_no_scene_id": 0,
        "pairing_insufficient_modalities": 0,
        "pairing_disabled": 0,
        # 场景组
        "scene_groups_total": 0,
        "verified_groups": 0,
        "insufficient_groups": 0,
        "scene_groups_verified_members": 0,
        "records_in_groups": 0,
        # 编号来源
        "platform_scene_ids": 0,
        "supplemented_scene_ids": 0,
        # 设备只读抄录
        "capture_readonly_records": 0,
        # 媒体
        "media_carried": 0, "media_missing": 0, "media_errors": 0, "media_bytes": 0,
        "media_remote": 0,
        # 各产物行数
        "origin_entries": 0, "reject_entries": 0, "pairing_entries": 0, "group_entries": 0,
        # 状态
        "enabled": False, "disabled_records": 0,
        "empty_input": False, "conservation_ok": False,
        "degraded": False, "degradation_reason": None, "active_backend": "rule",
        "model": None,
    }


def bump(stats: Dict[str, Any], key: str, amount: int = 1) -> None:
    stats[key] = int(stats.get(key, 0)) + int(amount)


# ----------------------------------------------------------------- 记录扫描
def busy_line_entry(operator: str, stage_id: str, line: int, reason: str, message: str,
                    event_time: str, batch_id: Optional[str] = None) -> Dict[str, Any]:
    """原始文件问题台账条目（统一九字段 + 行号与原因码）。"""
    return {
        "line": int(line),
        "sample_id": None,
        "reason": reason,
        "message": message,
        "label": LABEL_BAD_JSON if reason in (REASON_JSON_PARSE_ERROR,
                                             REASON_RECORD_NOT_OBJECT)
        else LABEL_MISSING_FIELD,
        "stage": stage_id,
        "operator": operator,
        "action": ACTION_QUARANTINE_RAW,
        "destination": DESTINATION_RAW_ISOLATION,
        "event_time": event_time,
        "batch_id": batch_id,
        "retry_count": 0,
    }


def reject_entry(operator: str, stage_id: str, line: int, sample_id: Optional[str],
                 reason: str, message: str, event_time: str,
                 batch_id: Optional[str] = None) -> Dict[str, Any]:
    """算子异常清单条目（流程状态，**不隔离**）。"""
    return {
        "line": int(line),
        "sample_id": sample_id,
        "reason": reason,
        "message": message,
        "label": LABEL_DUPLICATE,
        "stage": stage_id,
        "operator": operator,
        "action": ACTION_RECORD_REJECT,
        "destination": DESTINATION_OPERATOR_REJECTS,
        "event_time": event_time,
        "batch_id": batch_id,
        "retry_count": 0,
    }


def scan_record(record: Any, line: int, stats: Dict[str, Any], seen_ids: set,
                section: Dict[str, Any], unique_id: str,
                event_time: str = "") -> Dict[str, Any]:
    """校验单条记录并解析其场景编号绑定，返回候选（分组前）。

    ``section`` 为业务参数（``enabled`` / ``scene_id_source`` / ``min_modalities``
    + 身份与阶段）。
    """
    operator = section["operator"]
    stage_id = section["stage_id"]
    candidate: Dict[str, Any] = {
        "line": int(line), "sample_id": None, "record": None, "modalities": [],
        "scene_id": None, "scene_source": SOURCE_NONE, "reject": False,
        "route": None, "status": None, "reason": None, "label": None,
        "action": None, "destination": None, "group_id": None,
        "scene_modalities": [], "scene_member_count": 0, "entry": None,
        "batch_id": None,
    }

    # 非对象（含 JSON 解析失败）→ 原始隔离区，保留物理行号，绝不静默丢弃
    if not isinstance(record, dict):
        reason = REASON_JSON_PARSE_ERROR if record is _PARSE_FAILED else REASON_RECORD_NOT_OBJECT
        message = "JSON 解析失败" if reason == REASON_JSON_PARSE_ERROR else "记录必须是 JSON 对象"
        bump(stats, "origin_rejected")
        candidate.update({"reject": True, "route": ROUTE_ORIGIN, "reason": reason,
                          "label": LABEL_BAD_JSON, "action": ACTION_QUARANTINE_RAW,
                          "destination": DESTINATION_RAW_ISOLATION})
        candidate["entry"] = busy_line_entry(operator, stage_id, line, reason, message,
                                             event_time)
        return candidate

    sample_id = _extract_key(record.get("sample_id"))
    candidate["record"] = record
    candidate["batch_id"] = _extract_key(record.get("batch_id"))
    if sample_id is None:
        bump(stats, "origin_rejected")
        candidate.update({"reject": True, "route": ROUTE_ORIGIN,
                          "reason": REASON_MISSING_SAMPLE_ID,
                          "label": LABEL_MISSING_FIELD,
                          "action": ACTION_QUARANTINE_RAW,
                          "destination": DESTINATION_RAW_ISOLATION})
        candidate["entry"] = busy_line_entry(operator, stage_id, line,
                                             REASON_MISSING_SAMPLE_ID,
                                             "缺少 sample_id", event_time,
                                             candidate["batch_id"])
        return candidate

    candidate["sample_id"] = sample_id
    discriminator = file_discriminator(record)
    identity = (sample_id, discriminator)
    if identity in seen_ids:
        bump(stats, "duplicate_records")
        candidate.update({"reject": True, "route": ROUTE_REJECT, "reason": REASON_DUPLICATE,
                          "label": LABEL_DUPLICATE, "action": ACTION_RECORD_REJECT,
                          "destination": DESTINATION_OPERATOR_REJECTS})
        message = ("同一批内 sample_id 重复"
                   if discriminator is None
                   else "同一批内 sample_id + 文件登记项重复")
        candidate["entry"] = reject_entry(operator, stage_id, line, sample_id,
                                          REASON_DUPLICATE, message,
                                          event_time, candidate["batch_id"])
        return candidate
    seen_ids.add(identity)

    candidate["modalities"] = modalities_of(record)
    if capture_readonly(record):
        bump(stats, "capture_readonly_records")

    # 场景编号：先复用本算子历史绑定（只绑定一次），再取平台编号，最后按需补编号
    bound_id, bound_source = resolve_bound_scene_id(record)
    if bound_id is not None:
        candidate["scene_id"] = bound_id
        candidate["scene_source"] = bound_source
        bump(stats, "platform_scene_ids" if bound_source == SCENE_SOURCE_PLATFORM
             else "supplemented_scene_ids")
        return candidate

    platform_id = resolve_platform_scene_id(record)
    if platform_id is not None:
        candidate["scene_id"] = platform_id
        candidate["scene_source"] = SCENE_SOURCE_PLATFORM
        bump(stats, "platform_scene_ids")
        return candidate

    if section["scene_id_source"] == SCENE_SOURCE_GENERATE:
        candidate["scene_id"] = generate_scene_id(unique_id, sample_id)
        candidate["scene_source"] = SCENE_SOURCE_SUPPLEMENTED
        bump(stats, "supplemented_scene_ids")
        return candidate

    candidate["scene_id"] = None
    candidate["scene_source"] = SOURCE_NONE
    return candidate


class _ParseFailed:
    """哨兵：区分「JSON 解析失败」与「解析成功但不是对象」。"""

    def __repr__(self) -> str:  # pragma: no cover - 仅调试
        return "<parse-failed>"


_PARSE_FAILED = _ParseFailed()


def parse_line(raw_line: str) -> Any:
    """解析一行 JSONL；解析失败返回哨兵 ``_PARSE_FAILED``。"""
    try:
        return json.loads(raw_line)
    except json.JSONDecodeError:
        return _PARSE_FAILED


# ----------------------------------------------------------------- 场景组判定
def bind_scene_groups(candidates: List[Dict[str, Any]], section: Dict[str, Any],
                      stats: Dict[str, Any]) -> List[Dict[str, Any]]:
    """按场景编号聚合，核验同编号模态数，决定融合集 / 改走配对集。

    返回场景组摘要列表（按首次出现顺序）。**任何情况都不隔离样本**。
    """
    enabled = bool(section["enabled"])
    min_modalities = int(section["min_modalities"])
    live = [item for item in candidates if not item["reject"]]

    if not enabled:
        # 表1：工具默认关闭，配对集任务不执行；原样继续交给 #3
        for item in live:
            item.update({
                "route": ROUTE_PAIRING, "status": STATUS_DISABLED,
                "reason": REASON_TOOL_DISABLED, "scene_source": SOURCE_DISABLED,
                "label": LABEL_DISABLED, "action": ACTION_SKIP_DISABLED,
                "destination": DESTINATION_PAIRING_ROUTE,
            })
        bump(stats, "disabled_records", len(live))
        bump(stats, "pairing_disabled", len(live))
        bump(stats, "pairing_records", len(live))
        return []

    groups: Dict[str, Dict[str, Any]] = {}
    for item in live:
        scene_id = item["scene_id"]
        if scene_id is None:
            item.update({
                "route": ROUTE_PAIRING, "status": STATUS_NO_SCENE_ID,
                "reason": REASON_NO_SCENE_ID, "label": LABEL_TO_PAIRING,
                "action": ACTION_ROUTE_PAIRING,
                "destination": DESTINATION_PAIRING_ROUTE,
            })
            bump(stats, "pairing_no_scene_id")
            bump(stats, "pairing_records")
            continue
        group = groups.get(scene_id)
        if group is None:
            group = groups[scene_id] = {
                "scene_id": scene_id,
                "group_id": group_id_for(scene_id),
                "scene_source": item["scene_source"],
                "modalities": set(),
                "members": [],
            }
        group["modalities"].update(item["modalities"])
        group["members"].append(item)

    summaries: List[Dict[str, Any]] = []
    for scene_id, group in groups.items():
        modalities = [name for name in MODALITY_ORDER if name in group["modalities"]]
        member_count = len(group["members"])
        verified = len(modalities) >= min_modalities
        group["modalities"] = modalities
        group["member_count"] = member_count
        group["status"] = STATUS_VERIFIED if verified else STATUS_INSUFFICIENT
        bump(stats, "verified_groups" if verified else "insufficient_groups")
        bump(stats, "records_in_groups", member_count)
        if verified:
            bump(stats, "scene_groups_verified_members", member_count)
        for item in group["members"]:
            item["group_id"] = group["group_id"]
            item["scene_modalities"] = list(modalities)
            item["scene_member_count"] = member_count
            if verified:
                item.update({
                    "route": ROUTE_FUSION, "status": STATUS_VERIFIED, "reason": None,
                    "label": LABEL_VERIFIED, "action": ACTION_BIND_FUSION,
                    "destination": DESTINATION_FUSION_GROUP,
                })
                bump(stats, "fusion_records")
            else:
                item.update({
                    "route": ROUTE_PAIRING, "status": STATUS_INSUFFICIENT,
                    "reason": REASON_INSUFFICIENT, "label": LABEL_TO_PAIRING,
                    "action": ACTION_ROUTE_PAIRING,
                    "destination": DESTINATION_PAIRING_ROUTE,
                })
                bump(stats, "pairing_insufficient_modalities")
                bump(stats, "pairing_records")
        summaries.append(group)
    stats["scene_groups_total"] = len(summaries)
    return summaries


# ----------------------------------------------------------------- 产物构造
def build_record(candidate: Dict[str, Any], section: Dict[str, Any]) -> Dict[str, Any]:
    """把候选记录变成主产物记录：只追加 ``processing["02"]``/``scene_id``/``group_id``。

    - 深拷贝后只写本算子命名空间，不重置上游 ``processing``/``alignment``/``qc``/``metadata``；
    - 不写入处理时刻，保证同一输入重复运行逐字节一致（时刻见台账与血缘）。
    """
    record = json.loads(json.dumps(candidate["record"], ensure_ascii=False))
    processing = record.get("processing")
    if not isinstance(processing, dict):
        processing = {}
    node = {
        "operator": OPERATOR_SLUG,
        # [v4.2] status 双字段：int status + 原字符串迁入 process_status
        "status": status_int(candidate.get("status")),
        "process_status": candidate.get("status"),
        "route": candidate.get("route"),
        "scene_source": candidate.get("scene_source"),
        "scene_id": candidate.get("scene_id"),
        "group_id": candidate.get("group_id"),
        "modalities": list(candidate.get("modalities") or []),
        "scene_modalities": list(candidate.get("scene_modalities") or []),
        "scene_member_count": int(candidate.get("scene_member_count") or 0),
        "min_modalities": int(section["min_modalities"]),
        # 事实字段：本记录的场景编号只绑定一次（配置口径为 bind_once）
        "bound_once": True,
        "device_control": DEVICE_CONTROL_READ_ONLY,
        "enabled": bool(section["enabled"]),
        "reason": candidate.get("reason"),
        "capture_readonly": capture_readonly(record),
        "model": None,
    }
    processing["02"] = node
    record["processing"] = processing

    scene_id = candidate.get("scene_id")
    if scene_id is not None and bool(section["enabled"]):
        record["scene_id"] = scene_id
        record["group_id"] = candidate.get("group_id") or group_id_for(scene_id)
        record["scene_group"] = {
            "scene_id": scene_id,
            "group_id": record["group_id"],
            # [v4.2] status 双字段：int status + 原字符串迁入 process_status
            "status": status_int(candidate.get("status")),
            "process_status": candidate.get("status"),
            "scene_source": candidate.get("scene_source"),
            "modalities": list(candidate.get("scene_modalities") or []),
            "member_count": int(candidate.get("scene_member_count") or 0),
            "min_modalities": int(section["min_modalities"]),
        }
    else:
        record["scene_group"] = None
    return record


def build_group_entry(group: Dict[str, Any], section: Dict[str, Any]) -> Dict[str, Any]:
    """场景组摘要（一行一组，供前端与 #11 引用）。"""
    return {
        "scene_id": group["scene_id"],
        "group_id": group["group_id"],
        # [v4.2] status 双字段：int status + 原字符串迁入 process_status
        "status": status_int(group["status"]),
        "process_status": group["status"],
        "scene_source": group["scene_source"],
        "modalities": list(group["modalities"]),
        "modality_count": len(group["modalities"]),
        "member_count": group["member_count"],
        "member_sample_ids": [item["sample_id"] for item in group["members"]],
        "min_modalities": int(section["min_modalities"]),
        "route": (ROUTE_FUSION if group["status"] == STATUS_VERIFIED else ROUTE_PAIRING),
        "operator": OPERATOR_SLUG,
        "formal_operator_number": FORMAL_OPERATOR_NUMBER,
        "stage_id": section["stage_id"],
    }


def build_pairing_entry(candidate: Dict[str, Any], section: Dict[str, Any]) -> Dict[str, Any]:
    """改走配对集的记录（含原因与真实证据，不隔离）。"""
    return {
        "sample_id": candidate["sample_id"],
        "line": candidate["line"],
        "batch_id": candidate["batch_id"],
        "reason": candidate["reason"],
        # [v4.2] status 双字段：int status + 原字符串迁入 process_status
        "status": status_int(candidate["status"]),
        "process_status": candidate["status"],
        "scene_id": candidate["scene_id"],
        "scene_source": candidate["scene_source"],
        "modalities": list(candidate["modalities"]),
        "scene_modalities": list(candidate["scene_modalities"]),
        "min_modalities": int(section["min_modalities"]),
        "label": candidate["label"],
        "action": candidate["action"],
        "destination": candidate["destination"],
        "operator": OPERATOR_SLUG,
        "formal_operator_number": FORMAL_OPERATOR_NUMBER,
        "stage_id": section["stage_id"],
    }


def build_report(stats: Dict[str, Any], section: Dict[str, Any], generated_at: str,
                 unique_id: str, run_id: str) -> Dict[str, Any]:
    """运行级汇总报告（场景组 / 分流 / 编号来源 / 守恒），全部来自真实统计。"""
    return {
        "report_format": "crossmodal-scene-verification-v1",
        "operator": OPERATOR_SLUG,
        "operator_version": section.get("operator_version"),
        "formal_operator_number": FORMAL_OPERATOR_NUMBER,
        "stage_id": section["stage_id"],
        "uniqueId": unique_id,
        "runId": run_id,
        "generated_at": generated_at,
        "enabled": bool(section["enabled"]),
        "scene_id_source": section["scene_id_source"],
        "min_modalities": int(section["min_modalities"]),
        "bind_once": True,
        "device_control": DEVICE_CONTROL_READ_ONLY,
        "scene_groups_total": stats["scene_groups_total"],
        "verified_groups": stats["verified_groups"],
        "insufficient_groups": stats["insufficient_groups"],
        "platform_scene_ids": stats["platform_scene_ids"],
        "supplemented_scene_ids": stats["supplemented_scene_ids"],
        "fusion_records": stats["fusion_records"],
        "pairing_records": stats["pairing_records"],
        "pairing_no_scene_id": stats["pairing_no_scene_id"],
        "pairing_insufficient_modalities": stats["pairing_insufficient_modalities"],
        "pairing_disabled": stats["pairing_disabled"],
        "origin_rejected": stats["origin_rejected"],
        "duplicate_records": stats["duplicate_records"],
        "media_carried": stats["media_carried"],
        "media_missing": stats["media_missing"],
        "media_errors": stats["media_errors"],
        "media_remote": stats.get("media_remote", 0),
        "conservation_ok": stats["conservation_ok"],
        "model": None,
        "degraded": False,
    }


# --------------------------------------------------------------------- 守恒
def check_conservation(stats: Dict[str, Any]) -> bool:
    """守恒校验：每条记录只能有一个去处，各清单与计数逐项对齐。"""
    pairing_disabled = int(stats.get("pairing_disabled", 0))
    checks = [
        stats["input_lines"] == stats["input_records"] + stats["blank_lines"],
        stats["input_records"] == stats["valid_records"] + stats["origin_rejected"],
        stats["valid_records"] == stats["main_entries"] + stats["duplicate_records"],
        stats["main_entries"] == stats["fusion_records"] + stats["pairing_records"],
        stats["pairing_records"] == (stats["pairing_no_scene_id"]
                                     + stats["pairing_insufficient_modalities"]
                                     + pairing_disabled),
        stats["scene_groups_total"] == (stats["verified_groups"]
                                        + stats["insufficient_groups"]),
        stats["fusion_records"] == stats["scene_groups_verified_members"],
        stats["origin_entries"] == stats["origin_rejected"],
        stats["reject_entries"] == stats["duplicate_records"],
        stats["pairing_entries"] == stats["pairing_records"],
        stats["group_entries"] == stats["scene_groups_total"],
    ]
    if not bool(stats["enabled"]):
        checks.append(stats["fusion_records"] == 0 and stats["scene_groups_total"] == 0)
    return all(checks)


def conservation_view(stats: Dict[str, Any]) -> Dict[str, int]:
    keys = ("input_lines", "blank_lines", "input_records", "valid_records",
            "origin_rejected", "duplicate_records", "main_entries", "fusion_records",
            "pairing_records", "pairing_no_scene_id", "pairing_insufficient_modalities",
            "pairing_disabled", "scene_groups_total", "verified_groups",
            "insufficient_groups", "scene_groups_verified_members", "records_in_groups",
            "origin_entries", "reject_entries", "pairing_entries", "group_entries")
    return {key: int(stats.get(key, 0)) for key in keys}


def resolve_status(stats: Dict[str, Any]) -> str:
    """运行状态：空输入成功；有原始问题/重复标识为 partial；本算子不存在降级态。"""
    if stats["input_records"] == 0:
        return "success"
    if stats["origin_rejected"] or stats["duplicate_records"]:
        return "partial"
    return "success"


def log_summary(logger, stats: Dict[str, Any]) -> None:
    """按真实数量输出统计日志（不输出原文与大段 JSON）。"""
    log_utils.log_action(logger, "counts",
                         "input_lines=%d blank_lines=%d input_records=%d valid_records=%d "
                         "origin_rejected=%d duplicate_records=%d"
                         % (stats["input_lines"], stats["blank_lines"],
                            stats["input_records"], stats["valid_records"],
                            stats["origin_rejected"], stats["duplicate_records"]))
    log_utils.log_action(logger, "scene",
                         "enabled=%s scene_groups=%d verified_groups=%d "
                         "insufficient_groups=%d platform_ids=%d supplemented_ids=%d"
                         % (str(bool(stats["enabled"])).lower(), stats["scene_groups_total"],
                            stats["verified_groups"], stats["insufficient_groups"],
                            stats["platform_scene_ids"], stats["supplemented_scene_ids"]))
    log_utils.log_action(logger, "route",
                         "fusion_records=%d pairing_records=%d (no_scene_id=%d "
                         "insufficient=%d disabled=%d)"
                         % (stats["fusion_records"], stats["pairing_records"],
                            stats["pairing_no_scene_id"],
                            stats["pairing_insufficient_modalities"],
                            stats["pairing_disabled"]))
