# [v4.2] 血缘/阶段归档/运输输出已停用：本模块不再被 app.py / inference.py 调用，代码保留存档，不再维护。
"""MTY 数据工程传输方案合规层（跨模态算子包通用模块）。

对应甲方《【MTY规范】数据工程数据传输方案设计260926-ylh》正文与流程图。
本模块补齐**算子侧**应承担的四件事（工程侧职责不在此实现）：

1. ``uniqueId`` 强制为「任务id_文件id」（正文第 2 条）；
2. 血缘记录补 ``isLastOperator``：当前算子是否为当前阶段的最后一个算子
   （正文第 3 条、流程图「工程侧添加字段」）；
3. 阶段末算子产出**阶段产物**（正文第 7 条、流程图「阶段产物」分支）：
   - ``stage_lineage.jsonl``：该阶段全部血缘**拼接文本**，按规范「拼接成一行内容」
     的口径落盘（一条记录一行，JSONL）；
   - ``stage_artifact.zip``：阶段血缘文本 + 该阶段文本类输出内容，即阶段产物包；
4. **OSS 归档 + 关系入库**（流程图 F2/F3/F4/F5）：把拼接血缘、输出文件、阶段产物
   上传到对象存储，并把「任务/源文件/血缘文件/阶段/区域(槽)/去向/输出文件」的关系写入
   ``mty_stage_relation`` 表。两种运行形态同一套字段：
   - 生产：配置 ``MTY_OSS_*``（oss2）或 ``AWS_*``（boto3/S3 兼容）+ ``MTY_REGISTRY_DSN``；
   - 交付/离线：未配置时退化为**本地 OSS 镜像目录 + 本地 SQLite（stdio 无第三方依赖）**，
     对象键与库表字段完全一致，工程侧可用同一脚本把镜像目录与库同步上云。
   归档与入库全程**不抛异常**：失败只记 ``degraded/error``，绝不改变业务响应。
5. **任务级最终输出**（《输出格式.md》）：阶段末算子把本阶段完整血缘增量合并进
   ``<OUTPUT_DIR>/<taskId>/output/lineage/jsonl/``，交付阶段（#23）同时把交付包放进
   ``data/zip/``；合并逻辑在交付包根目录 ``task_output.py``，此处只负责挂载调用，
   失败只记日志，绝不改变业务响应。

所有新增文件写在 ``OUTPUT_DIR/<uniqueId>/<operator>/runNNN/`` 内，并登记进
``manifest.json`` 的 ``artifacts``，从而随阶段末 ``crossmodal-files-v1``
内容对象一并交给工程侧。

挂载方式（由打包脚本自动完成，算子业务代码无需感知）：
- ``utils/io.py`` 的 ``task_dir`` / ``validate_unique_id`` 内调用 :func:`validate_unique_id`；
- ``app.py`` 的 ``/predict`` 在拿到运行结果后调用 :func:`prepare_stage_result`。
"""
from __future__ import annotations

import hashlib
import json
import logging
import os
import re
import shutil
import sqlite3
import sys
import tempfile
import zipfile
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Dict, Iterable, Iterator, List, Optional, Sequence, Tuple

__all__ = [
    "IS_STAGE_LAST_OPERATOR",
    "OPERATOR_NUMBER",
    "STAGE_ID",
    "STAGE_NAME",
    "STAGE_LINEAGE_NAME",
    "STAGE_ARTIFACT_NAME",
    "INPUT_MANIFEST_NAME",
    "INPUT_MANIFEST_FORMAT",
    "LINEAGE_DIRNAME",
    "LINEAGE_INDEX_NAME",
    "TASK_LINEAGE_NAME",
    "SAMPLE_LINEAGE_NAME",
    "CHAIN_STAGE_PLAN",
    "DELIVERY_OPERATOR_NUMBERS",
    "TRANSPORT_REPORT_NAME",
    "RELATION_TABLE",
    "validate_unique_id",
    "annotate_node",
    "annotate_result",
    "prepare_stage_result",
    "write_joined_lineage",
    "lineage_record_for_file",
    "write_per_file_lineages",
    "build_stage_artifact",
    "stage_plan",
    "current_stage",
    "load_input_manifest",
    "write_input_manifest",
    "manifest_ids",
    "manifest_entry",
    "build_input_manifest",
    "prior_lineage_path",
    "accumulate_lineage",
    "write_lineage_views",
    "upload_input_dir",
    "TransportSettings",
    "archive_object",
    "record_relation",
    "list_relations",
    "run_transport",
    "selftest",
]

#: 本算子是否为当前阶段的最后一个算子（打包时按 map.json 阶段内位置写入）
IS_STAGE_LAST_OPERATOR = False

#: 正式编号（表1 v4.1）
OPERATOR_NUMBER = 12

#: 所属大阶段
STAGE_ID = "M12"

#: 阶段血缘拼接文本（一条记录一行）
STAGE_LINEAGE_NAME = "stage_lineage.jsonl"

#: 阶段产物 zip
STAGE_ARTIFACT_NAME = "stage_artifact.zip"

#: manifest.artifacts 中阶段产物的 role
STAGE_ARTIFACT_ROLE = "stage_artifact"

#: 阶段产物字节上限；超限则只留拼接血缘文本，跳过 zip（规范允许去掉阶段产物下载）
MAX_ARTIFACT_BYTES = 64 * 1024 * 1024

_TZ = timezone(timedelta(hours=8))

_TEXT_SUFFIXES = {".jsonl", ".json", ".txt", ".md", ".log", ".csv", ".tsv", ".yaml", ".yml"}

#: 打包阶段产物时跳过的目录（媒体与原始素材不进 zip）
_SKIP_DIRS = {"media", "input", "__pycache__", ".git"}

#: 结果字典中可能承载血缘记录的键
_RECORD_KEYS = ("stageLineage", "upstreamLineage", "priorStageLineage", "priorLineage",
                "lineage", "node", "localNode", "intermediateLineage")

#: 结果字典中可能承载阶段上下文的键
_CONTEXT_KEYS = ("stage_context", "stageContext")

#: uniqueId 每一段的允许字符：中英文、数字、点、连字符，首字符必须是中英文或数字
_UID_PART = re.compile(r"^[0-9A-Za-z\u4e00-\u9fff][0-9A-Za-z\u4e00-\u9fff._\-]*$")

_UID_MAX_LENGTH = 128

#: 归档与入库报告（写在 run 目录，随阶段末 crossmodal-files-v1 一并交付）
TRANSPORT_REPORT_NAME = "transport_report.json"

#: manifest.artifacts 中归档报告的 role
TRANSPORT_ARTIFACT_ROLE = "transport_report"

#: 关系入库表名（工程侧可直接复用，字段见 SELF_DDL）
RELATION_TABLE = "mty_stage_relation"

#: 本地归档/入库根目录名（默认落在算子目录下，与 OUTPUT_DIR 任务目录完全隔离）
TRANSPORT_DIRNAME = "_mty_transport"

#: 媒体后缀：默认不随血缘上传对象存储（规范要求大数据量走对象引用/分片）
_MEDIA_SUFFIXES = {
    ".jpg", ".jpeg", ".png", ".bmp", ".webp", ".gif", ".tif", ".tiff",
    ".wav", ".mp3", ".flac", ".m4a", ".ogg", ".opus",
    ".mp4", ".avi", ".mov", ".mkv", ".webm",
}

#: 责任团队（血缘 file 头 ``ownerTeam``；甲方 Ownership 支柱的最小落地）
OWNER_TEAM = "跨模态语料-算子组"


# --------------------------------------------------------------------------- #
# 逐文件血缘跨阶段累计所需的常量（对照 `血缘示例-HTTP版` 与 `lineage_example.jsonl`）
# --------------------------------------------------------------------------- #

#: 输入清单：**输入接入阶段**建立，后续阶段只读、只透传；是输入文件唯一身份源
INPUT_MANIFEST_NAME = "input_manifest.json"
INPUT_MANIFEST_FORMAT = "crossmodal-input-manifest-v1"

#: 逐文件血缘目录：一个输入文件一份 ``lineage/lineage_<uniqueId>.jsonl``
LINEAGE_DIRNAME = "lineage"

#: 兼容视图（索引 + 任务级/样本级血缘，对应 lineage_example.jsonl 两种 record_type）
LINEAGE_INDEX_NAME = "lineage_index.json"
TASK_LINEAGE_NAME = "task_lineage.jsonl"
SAMPLE_LINEAGE_NAME = "sample_lineage.jsonl"

#: 链路阶段计划：正式编号 → ``(stageId, stageName)``。
#: S 号按**真实链路顺序**编排，保证阶段血缘只前进、不回退（不出现 S5→S3 倒序）。
CHAIN_STAGE_PLAN: Dict[int, Tuple[str, str]] = {
    1: ("S1", "输入核验"), 2: ("S1", "输入核验"), 3: ("S1", "输入核验"),
    4: ("S2", "数据清洗"), 5: ("S2", "数据清洗"), 6: ("S2", "数据清洗"),
    7: ("S2", "数据清洗"), 8: ("S2", "数据清洗"),
    9: ("S3", "数据加工"), 10: ("S3", "数据加工"), 11: ("S3", "数据加工"),
    12: ("S3", "数据加工"), 13: ("S3", "数据加工"), 14: ("S3", "数据加工"),
    15: ("S3", "数据加工"), 16: ("S3", "数据加工"), 17: ("S3", "数据加工"),
    18: ("S3", "数据加工"), 19: ("S3", "数据加工"),
    20: ("S4", "数据质量评估"), 21: ("S4", "数据质量评估"),
    22: ("S5", "样本构建"), 23: ("S6", "数据集交付"),
    24: ("S7", "人工抽检复核"), 25: ("S7", "人工抽检复核"),
    26: ("S8", "交付样例"),
}

#: 本算子所属阶段名（以链路计划为准，编号缺省时退回算子自带常量）
STAGE_NAME = CHAIN_STAGE_PLAN.get(OPERATOR_NUMBER, (STAGE_ID, STAGE_ID))[1]

#: 链路阶段顺序（由 CHAIN_STAGE_PLAN 推导）：用于保证阶段血缘只前进、不回退
STAGE_SEQUENCE: List[str] = list(dict.fromkeys(
    CHAIN_STAGE_PLAN[number][0] for number in sorted(CHAIN_STAGE_PLAN)))
STAGE_RANK: Dict[str, int] = {stage: index for index, stage in enumerate(STAGE_SEQUENCE)}

#: 「交付」阶段算子：只有这些算子能判定 ``delivered=true`` 并写 deliver_supplement
DELIVERY_OPERATOR_NUMBERS = frozenset({23})


# --------------------------------------------------------------------------- #
# 1. uniqueId 强制「任务id_文件id」
# --------------------------------------------------------------------------- #

def validate_unique_id(unique_id: Any) -> str:
    """校验并返回 ``uniqueId``；必须是「任务id_文件id」两段（按最后一个下划线切分）。

    规范正文第 2 条：``uniqueid = 任务id_文件id``。两段都非空，只允许中英文、数字、
    点、下划线与连字符；首尾空白按既有 io 口径先剥离再校验。
    """
    if not isinstance(unique_id, str) or not unique_id.strip():
        raise ValueError("uniqueId 不能为空")
    value = unique_id.strip()
    if len(value) > _UID_MAX_LENGTH:
        raise ValueError("uniqueId 长度不能超过 %d 个字符" % _UID_MAX_LENGTH)
    task_id, sep, file_id = value.rpartition("_")
    if not sep or not task_id or not file_id:
        raise ValueError(
            "uniqueId 必须符合「任务id_文件id」格式（形如 task001_file01），当前为 %r" % value)
    for part, label in ((task_id, "任务id"), (file_id, "文件id")):
        if not _UID_PART.match(part):
            raise ValueError(
                "uniqueId 的%s只允许中英文、数字、点、下划线和连字符，当前为 %r" % (label, part))
    return value


# --------------------------------------------------------------------------- #
# 2. 血缘补 isLastOperator
# --------------------------------------------------------------------------- #

def _is_lineage_node(value: Any) -> bool:
    """血缘节点判据：有 operator，且有 uniqueId/status 之一。"""
    return (isinstance(value, dict) and isinstance(value.get("operator"), str)
            and ("uniqueId" in value or "status" in value))


def annotate_node(node: Dict[str, Any], request_is_last: Optional[bool] = None) -> Dict[str, Any]:
    """给单个血缘节点补 ``isLastOperator``（静态：本算子是否是当前阶段的最后一个算子）。

    ``request_is_last`` 为本次请求传入值时，额外记录 ``requestIsLastOperator``，
    便于工程侧核对「声明值」与「本次调用形态」。
    """
    if not isinstance(node, dict):
        return node
    node["isLastOperator"] = bool(IS_STAGE_LAST_OPERATOR)
    node["stageId"] = node.get("stageId") or STAGE_ID
    if request_is_last is not None:
        node["requestIsLastOperator"] = bool(request_is_last)
    return node


def _iter_nodes(result: Dict[str, Any]) -> Iterator[Dict[str, Any]]:
    seen: set = set()

    def walk(value: Any) -> Iterator[Dict[str, Any]]:
        if isinstance(value, list):
            for item in value:
                yield from walk(item)
        elif _is_lineage_node(value):
            marker = id(value)
            if marker not in seen:
                seen.add(marker)
                yield value
        elif isinstance(value, dict):
            for key in ("stage_lineage", "prior_lineage", "stageLineage", "priorLineage"):
                if key in value:
                    yield from walk(value[key])

    for key in _RECORD_KEYS:
        if key in result:
            yield from walk(result[key])
    for key in _CONTEXT_KEYS:
        if key in result:
            yield from walk(result[key])


def annotate_result(result: Any, request_is_last: Optional[bool] = None) -> Any:
    """给运行结果里所有血缘节点补 ``isLastOperator``；返回原对象（原地修改）。"""
    if not isinstance(result, dict):
        return result
    for node in _iter_nodes(result):
        annotate_node(node, request_is_last)
    return result


def _record_operator(record: Any) -> Optional[str]:
    """取记录里的算子名（用于判定「当前算子」）。"""
    if isinstance(record, dict):
        value = record.get("operator")
        if isinstance(value, str) and value.strip():
            return value.strip()
    return None


def _public_records(result: Dict[str, Any], records: Sequence[Dict[str, Any]]) -> List[Dict[str, Any]]:
    """Copy records for external lineage and mark only the current operator as stage-final.

    ``isLastOperator`` 只对本算子为真（规范第 3 条：当前算子是否本阶段最后一个），
    上游阶段的节点一律为假。当前算子优先取 ``result['lineage'/'node'/'localNode']``；
    取不到时按「prior_lineage → stage_lineage → 当前节点」的记录顺序取最后一条。
    """
    current = result.get("lineage") or result.get("node") or result.get("localNode")
    if isinstance(current, list):
        current = next((item for item in reversed(current) if isinstance(item, dict)), None)
    current_ops: set = set()
    operator = _record_operator(current)
    if operator:
        current_ops.add(operator)
    if not current_ops:
        for record in reversed(list(records)):
            operator = _record_operator(record)
            if operator:
                current_ops.add(operator)
                break
    output: List[Dict[str, Any]] = []
    for record in records:
        item = dict(record)
        item["isLastOperator"] = bool(current_ops
                                      and item.get("operator") in current_ops
                                      and IS_STAGE_LAST_OPERATOR)
        output.append(item)
    return output


# --------------------------------------------------------------------------- #
# 3. 阶段血缘拼接文本 + 阶段产物 zip
# --------------------------------------------------------------------------- #

def _record_key(node: Dict[str, Any]) -> tuple:
    return (node.get("operator"), node.get("uniqueId"), node.get("runId"),
            node.get("startedAt"), node.get("finishedAt"))


def collect_records(result: Dict[str, Any]) -> List[Dict[str, Any]]:
    """按顺序收集本次运行可见的全部血缘记录（上游阶段 + 当前阶段），去掉重复。

    规范要求「把一个文件所有阶段血缘拼接在一起」，因此顺序为
    ``prior_lineage`` → ``stage_lineage`` → 当前节点。
    """
    records: List[Dict[str, Any]] = []
    seen: set = set()

    def add(nodes: Iterable[Any]) -> None:
        for node in nodes:
            if not _is_lineage_node(node):
                continue
            key = _record_key(node)
            if key in seen:
                continue
            seen.add(key)
            records.append(node)

    contexts: List[Dict[str, Any]] = []
    for key in _CONTEXT_KEYS:
        value = result.get(key)
        if isinstance(value, dict):
            contexts.append(value)
    for context in contexts:
        add(context.get("prior_lineage") or [])
    for context in contexts:
        add(context.get("stage_lineage") or [])
    for key in ("upstreamLineage", "priorStageLineage", "priorLineage", "stageLineage"):
        value = result.get(key)
        if isinstance(value, list):
            add(value)
    for key in ("lineage", "node", "localNode"):
        value = result.get(key)
        if isinstance(value, dict):
            add([value])
    return records


def resolve_run_dir(result: Dict[str, Any]) -> Optional[Path]:
    """定位本次运行的 ``runNNN`` 目录。"""
    for key in ("runDir", "run_dir"):
        value = result.get(key)
        if isinstance(value, str) and value:
            path = Path(value)
            if path.is_dir():
                return path
    for key in ("lineageFile", "manifestPath", "outputFile"):
        value = result.get(key)
        if isinstance(value, str) and value:
            path = Path(value)
            parent = path.parent if path.suffix else path
            if parent.is_dir():
                return parent
    return None


def _dumps(record: Any) -> str:
    return json.dumps(record, ensure_ascii=False, separators=(",", ":"))


def write_joined_lineage(run_dir: Path, records: Sequence[Dict[str, Any]]) -> Path:
    """写阶段血缘拼接文本：``stage_lineage.jsonl``，**一条记录一行**（规范拼接口径）。"""
    target = Path(run_dir) / STAGE_LINEAGE_NAME
    lines = [_dumps(record) for record in records]
    target.write_text("".join(line + "\n" for line in lines), encoding="utf-8")
    return target


def _record_unique_id(record: Dict[str, Any]) -> Optional[str]:
    """Return the file-level unique id carried by a lineage record."""
    for key in ("uniqueId", "unique_id", "fileId", "file_id"):
        value = record.get(key)
        if isinstance(value, str) and value.strip():
            return value.strip()
    return None


def _safe_file_part(value: Any) -> Optional[str]:
    """Normalize a business file/sample identifier for ``任务id_文件id``."""
    if value is None:
        return None
    text = str(value).strip()
    if not text:
        return None
    text = Path(text.replace("\\", "/")).name
    stem = Path(text).stem if Path(text).suffix else text
    stem = re.sub(r"[^0-9A-Za-z\u4e00-\u9fff._-]+", "_", stem).strip("._-")
    return stem or None


def _output_rows(result: Dict[str, Any]) -> List[Dict[str, Any]]:
    """Read JSONL output rows when available; used to derive file-level IDs."""
    value = result.get("outputFile") or result.get("output_file")
    if isinstance(value, dict):
        value = value.get("path") or value.get("file")
    if not isinstance(value, str) or not value:
        return []
    path = Path(value)
    if not path.is_file() or path.suffix.lower() != ".jsonl":
        return []
    rows: List[Dict[str, Any]] = []
    try:
        for line in path.read_text(encoding="utf-8").splitlines():
            if not line.strip():
                continue
            item = json.loads(line)
            if isinstance(item, dict):
                rows.append(item)
    except (OSError, UnicodeDecodeError, json.JSONDecodeError):
        return []
    return rows


#: 侧车文件名里的「拒绝/隔离」关键词（用于判定单文件 verdict）
_REJECT_TOKENS = ("reject", "quarantine", "isolat", "exclud", "invalid", "bad")
#: 侧车文件名里的「待处理/跳过」关键词
_PENDING_TOKENS = ("pending", "skip", "wait", "deferred", "hold")

#: 单文件记录里可用于派生「文件 id」的深层键（与 :func:`_row_file_part` 对齐）
_ID_KEYS = ("file_id", "fileId", "sample_id", "sampleId", "text_sample_id",
            "image_sample_id", "source_id", "sourceId", "pair_id", "pairId",
            "identifier", "id")

#: 单文件记录里可用于派生数据集版本的深层键
_VERSION_KEYS = ("datasetVersion", "dataset_version", "upstream_version", "version")


def _deep_value(row: Any, keys: Sequence[str], depth: int = 3) -> Optional[str]:
    """在嵌套记录里按 key 名查找首个非空字符串值（侧车行常把信息放在 ``record`` 里）。"""
    if depth < 0 or not isinstance(row, dict):
        return None
    for key in keys:
        value = row.get(key)
        if isinstance(value, str) and value.strip():
            return value.strip()
    for key in ("record", "source", "item", "data", "sample", "pair", "left", "right"):
        nested = row.get(key)
        if isinstance(nested, dict):
            found = _deep_value(nested, keys, depth - 1)
            if found:
                return found
    return None


def _row_category(path: Path, row: Dict[str, Any]) -> str:
    """判定一条记录行的结局类别：``PASS`` / ``REJECT`` / ``PENDING``。

    先按侧车文件名（reject/quarantine/isolated/pending/skip…），再按行内
    ``action``/``decision``/``destination``/``status``；主输出默认 ``PASS``。
    台账型主输出（例如 ``21_ledger.jsonl``）行内只有 ``action=isolate``，
    必须按行内语义判定，否则会把被隔离的文件误判成交付。
    """
    name = Path(path).stem.lower()
    if any(token in name for token in _REJECT_TOKENS):
        return "REJECT"
    if any(token in name for token in _PENDING_TOKENS):
        return "PENDING"
    acted = " ".join(str(row.get(key) or "") for key in
                     ("action", "decision", "destination", "verdict", "status")).lower()
    if any(token in acted for token in _REJECT_TOKENS):
        return "REJECT"
    if any(token in acted for token in _PENDING_TOKENS):
        return "PENDING"
    status = str(row.get("status") or row.get("verdict") or "").strip().lower()
    if status in {"failed", "rejected", "reject", "quarantined", "isolated", "excluded"}:
        return "REJECT"
    if status in {"pending", "deferred", "waiting", "skipped", "skip"}:
        return "PENDING"
    if row.get("passed") is False:
        return "REJECT"
    return "PASS"


def _record_rows(result: Dict[str, Any],
                 run_dir: Optional[Path]) -> List[Tuple[Path, Dict[str, Any]]]:
    """收集本次运行产出的全部业务记录行（主输出 + run 目录内所有业务 jsonl）。

    这是「每个输入文件一份血缘」的数据来源：主输出为空时（例如整批被隔离或
    待处理），侧车台账（隔离/拒绝/待处理）同样逐文件登记，避免退化成批次级。
    """
    items: List[Tuple[Path, Dict[str, Any]]] = []
    primary: Optional[Path] = None
    raw_primary = result.get("outputFile") or result.get("output_file")
    if isinstance(raw_primary, str) and raw_primary.strip():
        primary = Path(raw_primary.strip())

    candidates: List[Path] = []
    if primary is not None:
        candidates.append(primary)
    if run_dir is not None:
        root = Path(run_dir)
        for path in sorted(root.rglob("*.jsonl")):
            rel = path.relative_to(root)
            if any(part in _SKIP_DIRS for part in rel.parts[:-1]):
                continue
            if path.parent.name == LINEAGE_DIRNAME:
                continue
            if path.name in {STAGE_LINEAGE_NAME, TRANSPORT_REPORT_NAME,
                             TASK_LINEAGE_NAME, SAMPLE_LINEAGE_NAME}:
                continue                      # 血缘/兼容视图不是业务记录行
            if primary is not None and path.resolve() == primary.resolve():
                continue
            candidates.append(path)

    for path in candidates:
        for row in _load_jsonl(path):
            # 空 JSON 对象不是业务记录行（例如仅作占位的输出主文件），跳过，
            # 避免合成出 task001_0001 这类序号 id 抢占逐文件血缘全集。
            if isinstance(row, dict) and row:
                items.append((path, row))
    return items


def _zip_entry_rows(path: Path, entrypoint: Optional[str] = None) -> List[Dict[str, Any]]:
    """读取上传 ZIP 内入口 JSONL 的行（入口优先，其次根级唯一 JSONL）。"""
    try:
        with zipfile.ZipFile(path) as archive:
            names = [name for name in archive.namelist() if name.lower().endswith(".jsonl")]
            if not names:
                return []
            target: Optional[str] = None
            if entrypoint:
                wanted = entrypoint.replace("\\", "/").lstrip("/")
                for name in names:
                    if name == wanted or name.endswith("/" + wanted):
                        target = name
                        break
            if target is None:
                roots = [name for name in names if "/" not in name]
                target = roots[0] if len(roots) == 1 else (roots or names)[0]
            rows: List[Dict[str, Any]] = []
            for line in archive.read(target).decode("utf-8", "replace").splitlines():
                if not line.strip():
                    continue
                try:
                    item = json.loads(line)
                except json.JSONDecodeError:
                    continue
                if isinstance(item, dict):
                    rows.append(item)
            return rows
    except (OSError, zipfile.BadZipFile, KeyError):
        return []


def _input_rows(result: Dict[str, Any],
                records: Sequence[Dict[str, Any]]) -> List[Tuple[Path, Dict[str, Any]]]:
    """兜底来源：读本次运行的输入 JSONL（或 ZIP 内入口）逐行记录。"""
    sources: List[Dict[str, Any]] = [item for item in records if isinstance(item, dict)]
    sources.append(result)
    candidates: List[str] = []
    entrypoint: Optional[str] = None
    for source in sources:
        for key in ("inputFile", "input_file", "sourceFile", "source_file"):
            value = source.get(key)
            if isinstance(value, str) and value.strip():
                candidates.append(value.strip())
        if entrypoint is None:
            for key in ("inputEntrypoint", "input_entrypoint", "entrypoint"):
                value = source.get(key)
                if isinstance(value, str) and value.strip():
                    entrypoint = value.strip()
                    break
    ordered: List[str] = []
    seen: set = set()
    for raw in candidates:
        if raw not in seen:
            seen.add(raw)
            ordered.append(raw)
    for raw in ordered:
        path = Path(raw)
        if path.is_file() and path.suffix.lower() == ".jsonl":
            rows = _load_jsonl(path)
            if rows:
                return [(path, row) for row in rows]
    for raw in ordered:
        path = Path(raw)
        if path.is_file() and path.suffix.lower() == ".zip":
            rows = _zip_entry_rows(path, entrypoint)
            if rows:
                return [(path, row) for row in rows]
    return []


def _row_file_part(row: Dict[str, Any]) -> Optional[str]:
    """Extract the stable source-file/sample identifier from a business row."""
    for key in ("file_id", "fileId", "sample_id", "sampleId", "text_sample_id",
                "image_sample_id", "source_id", "sourceId"):
        candidate = _safe_file_part(row.get(key))
        if candidate:
            return candidate
    files = row.get("files")
    if isinstance(files, list):
        for item in files:
            if isinstance(item, dict):
                candidate = _safe_file_part(item.get("path") or item.get("file"))
                if candidate:
                    return candidate
    for container in ("image", "audio", "video"):
        item = row.get(container)
        if isinstance(item, dict):
            candidate = _safe_file_part(item.get("path") or item.get("file"))
            if candidate:
                return candidate
    for key in ("left", "right", "text", "image", "audio", "video"):
        nested = row.get(key)
        if isinstance(nested, dict):
            candidate = _row_file_part(nested)
            if candidate:
                return candidate
    for key in ("pair_id", "pairId", "identifier"):
        candidate = _safe_file_part(row.get(key))
        if candidate:
            return candidate.split("--", 1)[0].removeprefix("pair-")
    return None


def _task_part(value: Any) -> str:
    text = str(value or "task").strip()
    task, sep, _file = text.rpartition("_")
    return task if sep and task else text


def _first_text_value(*values: Any) -> Optional[str]:
    for value in values:
        if isinstance(value, str) and value.strip():
            return value.strip()
    return None


def _resolve_batch_uid(result: Dict[str, Any],
                       records: Sequence[Dict[str, Any]]) -> Optional[str]:
    """本次运行的批次 ``uniqueId``；结果字典缺失时从血缘节点兜底。

    ``/predict`` 的返回体未必带 ``uniqueId``（部分算子只把它写在业务字段里）。
    此时用第一条血缘节点的 ``uniqueId``（形如 ``r8_08``）兜底，保证
    :func:`_result_unique_ids` 与 :func:`lineage_record_for_file` 得到**同一个**
    任务前缀——否则逐文件血缘会丢掉全部 ``stage_segment``。
    """
    direct = _first_text_value(result.get("uniqueId"), result.get("unique_id"))
    if direct:
        return direct
    for record in records:
        if isinstance(record, dict):
            candidate = _record_unique_id(record)
            if candidate:
                return candidate
    for key in ("uniqueIds", "unique_ids"):
        value = result.get(key)
        if isinstance(value, (list, tuple)) and value:
            first = value[0]
            if isinstance(first, str) and first.strip():
                return first.strip()
    return None


def _resolve_task(result: Dict[str, Any],
                  records: Sequence[Dict[str, Any]]) -> str:
    """任务 id：全链唯一，保证同一输入文件在各阶段的 ``uniqueId`` 不变。

    规范第 2 条要求 ``uniqueId = 任务id_文件id`` 且在**所有阶段保持不变**。本包
    各算子节点的 ``uniqueId`` 形如 ``r8_03`` / ``r8_24_reject``，故取「全部节点
    首段一致」的那个首段作为任务 id（``r8``）；首段不一致时退化为批次
    ``uniqueId`` 的前缀。
    """
    values: List[str] = []
    for record in records:
        if isinstance(record, dict):
            candidate = _record_unique_id(record)
            if candidate:
                values.append(candidate)
    direct = _first_text_value(result.get("uniqueId"), result.get("unique_id"))
    if direct:
        values.append(direct)
    heads = {value.split("_", 1)[0] for value in values if value}
    if len(heads) == 1:
        head = heads.pop()
        if head:
            return head
    return _task_part(_resolve_batch_uid(result, records))


def _result_unique_ids(result: Dict[str, Any], records: Sequence[Dict[str, Any]],
                       run_dir: Optional[Path] = None) -> List[str]:
    """为本次运行的**每个输入文件**派生一个 uniqueId，保证逐文件血缘。

    派生顺序：① run 目录内全部业务记录行（主输出 + 隔离/拒绝/待处理侧车台账）
    → ② 本次输入 JSONL（或上传 ZIP 内入口）
    → ③ 批次 uniqueId 兜底（仅在完全取不到逐文件线索时）。
    找不到文件 id 时按行序号 ``%04d`` 兜底，从而绝不退化成「一批一份」。
    """
    batch_uid = _resolve_batch_uid(result, records)
    # 批次 id 只有直接写在 result 上（如算子运行 id r8_08）才视为“非文件 id”；
    # 兜底从第一条血缘节点取来的值本身可能就是首个文件的 uid，
    # 此时不得在兜底 ③ 里把它排除（否则会丢掉第一个文件的血缘）。
    batch_from_result = bool(_first_text_value(result.get("uniqueId"), result.get("unique_id")))
    task = _resolve_task(result, records)
    ids: List[str] = []
    meta: Dict[str, Dict[str, Any]] = {}

    def add(part: Optional[str], row: Dict[str, Any], category: str, source: str) -> None:
        cleaned = _safe_file_part(part)
        if not cleaned:
            return
        uid = (cleaned if ("_" in cleaned and cleaned.startswith(task + "_"))
               else "%s_%s" % (task, cleaned))
        existing = meta.get(uid)
        if existing is not None:
            # 同一文件可能同时出现在主输出与侧车台账里：取更严格的那个结局
            if _CATEGORY_RANK.get(category, 0) <= _CATEGORY_RANK.get(
                    str(existing.get("category") or ""), 0):
                return
            existing.update({"row": row, "category": category, "source": source})
            return
        ids.append(uid)
        meta[uid] = {"row": row, "category": category, "source": source,
                     "index": len(ids)}

    sequence = 0
    for path, row in _record_rows(result, run_dir):
        part = _row_file_part(row)
        if not part:
            sequence += 1
            part = "%04d" % sequence
        add(part, row, _row_category(path, row), path.name)

    if not ids:
        for path, row in _input_rows(result, records):
            part = _row_file_part(row)
            if not part:
                sequence += 1
                part = "%04d" % sequence
            add(part, row, "PASS", path.name)

    if not ids:
        values: List[Any] = []
        for record in records:
            if isinstance(record, dict):
                values.append(_record_unique_id(record))
        for key in ("uniqueIds", "unique_ids", "uniqueId", "unique_id"):
            value = result.get(key)
            values.extend(value if isinstance(value, (list, tuple, set)) else [value])
        for value in values:
            if (isinstance(value, str) and value.strip()
                    and not (batch_from_result and value.strip() == batch_uid)
                    and value.strip() not in ids):
                ids.append(value.strip())
                # 兜底 ③ 的文件同样“本次运行可见”：登记 meta，保证
                # accumulate_lineage 里 present=True，能写出 stage_segment/outcome。
                meta.setdefault(value.strip(), {"row": {}, "category": "PASS",
                                                "source": "records",
                                                "index": len(ids)})

    result["_lineage_file_meta"] = meta
    return ids


#: 宿主机绝对路径前缀（Windows 盘符 / UNC），对外血缘一律不暴露
_HOST_PATH = re.compile(r"^(?:[A-Za-z]:[\\/]|\\\\)")

#: 单文件记录里可能的「越限证据」数值键
_METRIC_VALUE_KEYS = ("value", "score", "similarity", "cosine_similarity",
                      "mapped_cosine_similarity", "edit_distance_ratio", "ratio",
                      "metric_value")
#: 单文件记录里可能的阈值键
_METRIC_THRESHOLD_KEYS = ("threshold", "cosine_threshold", "similarity_threshold",
                          "value_threshold", "threshold_value",
                          "edit_distance_ratio_max", "limit", "max_ratio")
#: 「打标」语义关键词（outcome.marks 汇总用）
_MARK_TOKENS = ("打标", "标记", "标注", "marked", "mark_")
#: 失败结局的状态取值
_FAIL_STATUS = {"failed", "error", "fail", "aborted"}
#: 结局严格程度（同一文件多处登记时取更严格者）
_CATEGORY_RANK = {"PASS": 0, "PENDING": 1, "REJECT": 2}
#: 侧车台账文件名关键词（挑主输出入口时排除）
_SIDECAR_TOKENS = _REJECT_TOKENS + _PENDING_TOKENS


def _parse_ts(value: Any) -> Optional[datetime]:
    """容错解析 ISO8601 时间戳（含 ``Z`` 与无时区两种写法）。"""
    if not isinstance(value, str) or not value.strip():
        return None
    text = value.strip().replace("Z", "+00:00")
    try:
        parsed = datetime.fromisoformat(text)
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=_TZ)
    return parsed


def _cost_ms(record: Dict[str, Any]) -> int:
    """算子耗时：显式字段优先，其次 ``finishedAt - startedAt``，取不到记 0。"""
    for key in ("costMs", "cost_ms", "elapsed_ms", "duration_ms"):
        value = record.get(key)
        if isinstance(value, (int, float)) and not isinstance(value, bool):
            return int(value)
    statistics = record.get("statistics")
    if isinstance(statistics, dict):
        for key in ("elapsed_ms", "duration_ms", "cost_ms"):
            value = statistics.get(key)
            if isinstance(value, (int, float)) and not isinstance(value, bool):
                return int(value)
        seconds = statistics.get("duration_seconds") or statistics.get("durationSeconds")
        if isinstance(seconds, (int, float)) and not isinstance(seconds, bool):
            return int(float(seconds) * 1000)
    started = _parse_ts(record.get("startedAt"))
    finished = _parse_ts(record.get("finishedAt"))
    if started is not None and finished is not None and finished >= started:
        return int((finished - started).total_seconds() * 1000)
    return 0


def _to_workspace_path(text: str, run_dir: Optional[Path], unique_id: str) -> str:
    """宿主机绝对路径 → ``/workspace/<uniqueId>/<run 内相对路径>``（不暴露部署路径）。"""
    path = Path(str(text))
    if run_dir is not None:
        try:
            relative = path.resolve().relative_to(Path(run_dir).resolve())
            return "/workspace/%s/%s" % (unique_id, relative.as_posix())
        except (ValueError, OSError):
            pass
    return "/workspace/%s/%s" % (unique_id, path.name)


def _sanitize_value(value: Any, run_dir: Optional[Path], unique_id: str) -> Any:
    """递归把记录里的宿主机绝对路径换成 ``/workspace/...`` 逻辑地址。"""
    if isinstance(value, dict):
        return {key: _sanitize_value(item, run_dir, unique_id)
                for key, item in value.items()}
    if isinstance(value, list):
        return [_sanitize_value(item, run_dir, unique_id) for item in value]
    if isinstance(value, str) and _HOST_PATH.match(value.strip()):
        return _to_workspace_path(value.strip(), run_dir, unique_id)
    return value


def _deep_number(row: Any, keys: Sequence[str], depth: int = 2) -> Optional[float]:
    """在嵌套记录里按 key 名查找首个数值（用于越限 metrics）。"""
    if depth < 0 or not isinstance(row, dict):
        return None
    for key in keys:
        value = row.get(key)
        if isinstance(value, bool):
            continue
        if isinstance(value, (int, float)):
            return float(value)
    for key in ("record", "metrics", "audio_text", "image_text", "video_text",
                "temporal", "detail", "sample", "pair", "left", "right"):
        nested = row.get(key)
        if isinstance(nested, dict):
            found = _deep_number(nested, keys, depth - 1)
            if found is not None:
                return found
    return None


def _row_metrics(row: Dict[str, Any]) -> Optional[Dict[str, Any]]:
    """拦截/打标行的越限证据 ``{value, threshold}``（取不到就不写，不臆造）。"""
    value = _deep_number(row, _METRIC_VALUE_KEYS)
    threshold = _deep_number(row, _METRIC_THRESHOLD_KEYS)
    if value is None or threshold is None:
        return None
    return {"value": value, "threshold": threshold}


def _row_marks(row: Dict[str, Any], stage: str, operator: str) -> List[Dict[str, Any]]:
    """outcome.marks：打标汇总（优先取记录内显式 marks）。"""
    declared = row.get("marks") or row.get("marks_summary")
    if isinstance(declared, list):
        marks = [item for item in declared if isinstance(item, dict)]
        if marks:
            return marks
    text = "%s %s" % (row.get("action") or "", row.get("label") or "")
    if any(token in text for token in _MARK_TOKENS):
        return [{"stage": stage, "tool": operator,
                 "marked": str(row.get("reason") or row.get("label") or "marked")}]
    return []


def _row_partial_filter(row: Dict[str, Any], stage: str, operator: str,
                        statistics: Dict[str, Any]) -> Optional[Dict[str, Any]]:
    """outcome.partialFilter：过滤生效但未拦死（``rowsIn > rowsOut > 0``）。"""
    declared = row.get("partial_filter") or row.get("partialFilter")
    if isinstance(declared, dict) and declared:
        return declared
    rows_in = statistics.get("input_records")
    rows_out = None
    for key in ("output_records", "valid_records", "kept_records", "sampled_records"):
        value = statistics.get(key)
        if isinstance(value, int) and not isinstance(value, bool):
            rows_out = value
            break
    if not isinstance(rows_in, int) or not isinstance(rows_out, int):
        return None
    if 0 < rows_out < rows_in:
        return {"stage": stage, "tool": operator, "rowsIn": rows_in, "rowsOut": rows_out}
    return None


def _resolve_run_member(run_dir: Optional[Path], reference: Any) -> Optional[Path]:
    """把业务记录里的相对引用解析成 run 目录内的真实文件（用于 bytes/checksum）。"""
    if run_dir is None or not isinstance(reference, str) or not reference.strip():
        return None
    text = reference.strip().replace("\\", "/")
    candidates: List[Path] = []
    if not Path(text).is_absolute():
        candidates.append(Path(run_dir) / text)
    candidates.append(Path(text))
    for candidate in candidates:
        try:
            if candidate.is_file():
                return candidate
        except OSError:
            continue
    name = Path(text).name
    if name:
        try:
            for candidate in Path(run_dir).rglob(name):
                if candidate.is_file():
                    return candidate
        except OSError:
            return None
    return None


def _file_fingerprint(path: Optional[Path]) -> Dict[str, Any]:
    """文件字节数与校验和。

    规范口径（需求第 4 条）：首行 ``bytes``/``checksum`` 优先继承输入接入阶段的值；
    需要按文件实测时**统一用 SHA-256**（64 位十六进制摘要）。
    """
    if path is None:
        return {}
    try:
        digest = hashlib.sha256()
        with path.open("rb") as handle:
            for chunk in iter(lambda: handle.read(1024 * 1024), b""):
                digest.update(chunk)
        return {"bytes": path.stat().st_size, "checksum": digest.hexdigest()}
    except OSError:
        return {}


def _object_uri(unique_id: str, name: Optional[str], run_dir: Optional[Path],
                result: Dict[str, Any]) -> Optional[str]:
    """按归档层的对象键生成逻辑地址（离线测试为 ``local-mirror://``）。"""
    if run_dir is None or not name:
        return None
    try:
        settings = TransportSettings.from_env()
    except Exception:
        return None
    try:
        key = object_key(unique_id, STAGE_ID, Path(run_dir).name, name, settings)
    except Exception:
        return None
    try:
        if settings.archive_backend in {"oss2", "boto3", "s3"} and settings.remote_ready():
            return "oss://%s/%s" % (settings.bucket, key)
    except Exception:
        pass
    return "local-mirror://%s" % key


def _upload_block(result: Dict[str, Any], unique_id: str,
                  records: Sequence[Dict[str, Any]]) -> Dict[str, Any]:
    """file 头的 ``upload`` 块：输入对象接入算子系统的真实形态。"""
    container = _first_text_value(result.get("inputFile"), result.get("input_file"))
    entrypoint = _first_text_value(result.get("inputEntrypoint"))
    for record in reversed(list(records)):
        if isinstance(record, dict):
            container = container or _first_text_value(
                record.get("inputFile"), record.get("input_file"))
            entrypoint = entrypoint or _first_text_value(
                record.get("inputEntrypoint"), record.get("input_entrypoint"))
    if not container:
        return {}
    batch_uid = _first_text_value(result.get("uniqueId"),
                                  result.get("unique_id")) or unique_id
    block: Dict[str, Any] = {
        "request": "POST /uploadFile?uniqueId=%s" % batch_uid,
        "reqType": "multipart/form-data",
    }
    if str(result.get("status") or "success").lower() in _FAIL_STATUS:
        block["resp"] = {"code": 1, "msg": str(result.get("status"))}
    else:
        block["resp"] = {"code": 0, "msg": "success"}
    block["container"] = Path(str(container).replace("\\", "/")).name
    if entrypoint:
        block["entrypoint"] = entrypoint
    return block


def _media_reference(row: Any, depth: int = 3) -> Optional[str]:
    """从业务行里取输入文件的相对引用；媒体容器优先，行内直取字段兜底。"""
    if depth < 0 or not isinstance(row, dict):
        return None
    nested: List[str] = []
    for key in ("media", "files", "assets", "image", "audio", "video",
                "record", "data", "sample", "item", "payload"):
        value = row.get(key)
        candidates = value if isinstance(value, list) else [value]
        for item in candidates:
            found = _media_reference(item, depth - 1)
            if found:
                nested.append(found)
    direct: List[str] = []
    for key in ("file", "file_url", "file_link", "path", "url"):
        value = row.get(key)
        if isinstance(value, str) and value.strip():
            direct.append(value.strip())
    for candidate in nested + direct:
        if Path(candidate.replace("\\", "/")).suffix.lower() in _MEDIA_SUFFIXES:
            return candidate
    return (nested + direct)[0] if (nested or direct) else None


def _lineage_file_header(unique_id: str, records: Sequence[Dict[str, Any]],
                         result: Dict[str, Any],
                         run_dir: Optional[Path] = None) -> Dict[str, Any]:
    """第 1 行 ``file``：身份 + 来源引用 + 责任团队 + 校验 + 上传形态。"""
    info = (result.get("_lineage_file_meta") or {}).get(unique_id)
    row = info.get("row") if isinstance(info, dict) else None
    row = row if isinstance(row, dict) else {}
    last = next((item for item in records if _record_unique_id(item) == unique_id), {})
    if run_dir is None:
        run_dir = resolve_run_dir(result)
    reference = _media_reference(row) or _row_file_part(row)
    file_name = last.get("file") or result.get("file")
    if reference:
        file_name = Path(str(reference).replace("\\", "/")).name
    header: Dict[str, Any] = {
        "type": "file",
        "writtenBy": "engineering",
        "formatVersion": "1.0",
        "uniqueId": unique_id,
        "datasetVersion": (_deep_value(row, _VERSION_KEYS)
                           or _first_text_value(last.get("datasetVersion"),
                                                result.get("datasetVersion"))
                           or "unknown"),
        "sourceRef": _source_ref(row, last, result),
        "ownerTeam": OWNER_TEAM,
        "file": file_name or unique_id,
    }
    header.update(_file_fingerprint(_resolve_run_member(run_dir, reference)))
    upload = _upload_block(result, unique_id, records)
    if upload:
        header["upload"] = upload
    rerun_of = _first_text_value(last.get("rerunOf"), result.get("rerunOf"))
    if rerun_of:
        header["rerunOf"] = rerun_of
    header["startedAt"] = (_first_text_value(last.get("startedAt"),
                                             result.get("startedAt")) or _now())
    return header


def _source_ref(row: Dict[str, Any], last: Dict[str, Any],
                result: Dict[str, Any]) -> str:
    """数据来源引用：指向工程侧数据集台账（来源/授权/采集方式以台账为准）。"""
    declared = _first_text_value(last.get("sourceRef"), result.get("sourceRef"))
    if declared:
        return declared
    dataset = (_deep_value(row, ("dataset_id", "datasetId", "batch_id", "batchId",
                                 "source_id", "sourceId"))
               or "unknown")
    return "ledger://raw-dataset/%s" % dataset


def _node_operator_meta(record: Dict[str, Any]
                        ) -> Tuple[Any, Optional[str], Optional[str], Optional[str]]:
    """节点里的算子编号 / 名称 / 版本 / 阶段名：显式字段优先，其次配置块。

    返回 ``(number, label, version, stageLabel)``。上游阶段节点的 ``config`` 形态
    不一（有的 ``config.operator`` 是字符串），因此再兜底 ``config.deployment``；
    仍取不到编号时，用本包节点 ``uniqueId``（形如 ``r8_03``）的尾段数字。
    """
    config = record.get("config")
    if not isinstance(config, dict):
        config = {}
    operator_config = config.get("operator")
    if not isinstance(operator_config, dict):
        operator_config = {}
    deployment = config.get("deployment")
    if not isinstance(deployment, dict):
        deployment = {}
    deploy_operator = deployment.get("operator")
    if not isinstance(deploy_operator, dict):
        deploy_operator = {}
    number = record.get("formalOperatorNumber")
    if number is None:
        number = record.get("formal_operator_number")
    if number is None:
        number = record.get("operator_number")
    if number is None:
        number = operator_config.get("number")
    if number is None:
        number = operator_config.get("formalOperatorNumber")
    if number is None:
        number = deploy_operator.get("number")
    if number is None:
        number = deploy_operator.get("formalOperatorNumber")
    if number is None:
        # 分支调用的 uniqueId 形如 ``r8_24_reject``，取其中的纯数字段作为正式编号
        for segment in str(record.get("uniqueId") or "").split("_"):
            if segment.isdigit():
                number = int(segment)
                break
    label = _first_text_value(record.get("operator_label"),
                              operator_config.get("label"),
                              operator_config.get("name"),
                              deploy_operator.get("label"),
                              deploy_operator.get("name"))
    version = _first_text_value(record.get("operator_version"),
                                operator_config.get("version"),
                                deploy_operator.get("version"))
    stage_label = _first_text_value(record.get("stageName"), record.get("stage_name"),
                                    deployment.get("stage_label"))
    return number, label, version, stage_label


def _operator_contract(node: Dict[str, Any], unique_id: str, stage: str,
                       sequence: int, previous_output: str,
                       run_dir: Optional[Path], row: Dict[str, Any],
                       rejected: bool, is_last: bool,
                       stage_name: str) -> Tuple[Dict[str, Any], str]:
    """把血缘节点整理成 HTTP 版 ``operators[]`` 的一条，原始记录进 ``operatorRecord``。"""
    record = json.loads(json.dumps(node, ensure_ascii=False, default=str))
    operator = str(record.get("operator") or "unknown")
    stage_no = re.sub(r"\D+", "", stage) or "0"
    output = "/workspace/%s/s%s/%s.out" % (unique_id, stage_no, operator)
    terminal = bool(rejected and is_last)
    process: Dict[str, Any] = {
        "action": "REJECT" if terminal else "PROCESS",
        "detail": ("命中拦截条件，本文件终止" if terminal else "处理通过，输出中间结果"),
        "rowsIn": 1,
        "rowsOut": 0 if terminal else 1,
    }
    if terminal:
        process["terminal"] = True
        metrics = _row_metrics(row)
        if metrics:
            process["metrics"] = metrics
    number, label, version, _stage_label = _node_operator_meta(record)
    if number is None:
        # 规范要求 operators[] 一律带正式算子编号，取不到时用本算子编号兜底
        number = OPERATOR_NUMBER
    contract: Dict[str, Any] = {
        "seq": sequence,
        "operator": operator,
        "operatorNumber": number,
        "operatorVersion": version,
        "stageId": stage,
        "stageName": stage_name,
        "runId": _first_text_value(record.get("runId"),
                                   Path(run_dir).name if run_dir else None),
        "isLastOperator": bool(record.get("isLastOperator")),
        "requestIsLastOperator": record.get("requestIsLastOperator"),
        "tool": {
            "id": _first_text_value(record.get("source_reference"), operator) or operator,
            "name": label or operator,
            "type": "algorithm",
            "kind": "algorithm",
            "endpoint": "POST /predict",
            "version": version,
        },
        "request": {"uniqueId": unique_id, "input": previous_output},
        "process": process,
        "output": output,
        "ts": (_first_text_value(record.get("startedAt"), record.get("finishedAt"))
               or _now()),
        "costMs": _cost_ms(record),
        "operatorRecord": {
            key: _sanitize_value(value, run_dir, unique_id)
            for key, value in record.items()
            if key not in ("request", "process", "output", "tool")
        },
    }
    # 逐文件血缘统一按**链路阶段**（S 号）标注，避免算子内部 M** 运行标记
    # 与阶段顺序口径不一致（链路阶段见 CHAIN_STAGE_PLAN）。
    contract["operatorRecord"]["stageId"] = stage
    if stage_name:
        contract["operatorRecord"]["stageName"] = stage_name
    return contract, output


def _oss_supplement(unique_id: str, stage: str, run_dir: Optional[Path],
                    result: Dict[str, Any],
                    entrypoint: Optional[str]) -> Dict[str, Any]:
    """环节打包后补的 OSS 地址（离线测试为 ``local-mirror://`` 逻辑地址）。"""
    return {
        "type": "oss_supplement",
        "writtenBy": "engineering",
        "stage": stage,
        "recordedAfter": "stage zip packaged",
        "oss": {
            "stageOutput": _object_uri(unique_id, entrypoint, run_dir, result) or "",
            "zip": _object_uri(unique_id, STAGE_ARTIFACT_NAME, run_dir, result) or "",
            "lineage": _object_uri(
                unique_id, "lineage/lineage_%s.jsonl" % unique_id, run_dir, result) or "",
        },
    }


def _deliver_supplement(unique_id: str, run_dir: Optional[Path],
                        result: Dict[str, Any], index: Any,
                        delivered: bool,
                        entrypoint: Optional[str]) -> Dict[str, Any]:
    """交付打包后补的 ``finalRow`` + 交付包地址。"""
    package = _object_uri(unique_id, STAGE_ARTIFACT_NAME, run_dir, result)
    delivery: Dict[str, Any] = {
        "lineage": _object_uri(unique_id, "lineage/lineage_%s.jsonl" % unique_id,
                               run_dir, result)
        or ("lineage/lineage_%s.jsonl" % unique_id),
        "package": package or STAGE_ARTIFACT_NAME,
    }
    if delivered and isinstance(index, int) and index > 0:
        delivery["finalRow"] = "%s#row%d" % (entrypoint or "delivery", index)
    return {"type": "deliver_supplement", "writtenBy": "engineering",
            "recordedAfter": "delivery packaged", "delivery": delivery}


def lineage_record_for_file(unique_id: str, records: Sequence[Dict[str, Any]],
                            result: Dict[str, Any],
                            run_dir: Optional[Path] = None) -> List[Dict[str, Any]]:
    """按 HTTP 版规范为一个输入文件生成**完整五类行**血缘。

    行序：``file`` → （每环节 ``stage_segment`` / 末环节后再加 ``oss_supplement``）
    → ``outcome`` →（交付时）``deliver_supplement``。
    """
    batch_uid = _resolve_batch_uid(result, records)
    task = _resolve_task(result, records)
    meta = result.get("_lineage_file_meta") or {}
    known_ids = {key for key in meta if isinstance(key, str)}

    def belongs(record: Dict[str, Any]) -> bool:
        rid = _record_unique_id(record)
        if rid is None:
            return True
        if rid == unique_id:
            return True
        if rid in known_ids:
            return False
        if rid == batch_uid:
            return True
        return rid.startswith(task + "_")

    selected = [record for record in records
                if isinstance(record, dict) and belongs(record)]
    if run_dir is None:
        run_dir = resolve_run_dir(result)
    header = _lineage_file_header(unique_id, selected, result, run_dir)
    lines: List[Dict[str, Any]] = [header]

    info = meta.get(unique_id) if isinstance(meta.get(unique_id), dict) else {}
    category = str(info.get("category") or "").upper()
    row = info.get("row") if isinstance(info.get("row"), dict) else {}
    index = info.get("index")
    status = result.get("status")
    status_text = str(status or "success").lower()

    output_entrypoint = _first_text_value(result.get("outputFile"),
                                          result.get("output_file"))
    if not output_entrypoint and run_dir is not None:
        # 响应体缺 outputFile 时，用 run 目录里真实存在的主输出文件兜底
        for candidate in sorted(Path(run_dir).glob("*.jsonl")):
            name = candidate.stem.lower()
            if name.startswith("lineage") or any(
                    token in name for token in _SIDECAR_TOKENS):
                continue
            output_entrypoint = candidate.name
            break
    if isinstance(output_entrypoint, str) and output_entrypoint:
        output_entrypoint = Path(output_entrypoint.replace("\\", "/")).name

    groups: Dict[str, List[Dict[str, Any]]] = {}
    order: List[str] = []
    names: Dict[str, str] = {}
    sequence = 0
    last_stage = STAGE_ID
    previous_output = "/workspace/%s/upload/%s" % (unique_id, header.get("file"))
    for node in selected:
        stage = str(node.get("stageId") or node.get("stage_id") or STAGE_ID)
        _, _, _, stage_label = _node_operator_meta(node)
        if stage not in groups:
            groups[stage] = []
            order.append(stage)
            names[stage] = stage_label or stage
        sequence += 1
        last_stage = stage
        contract, previous_output = _operator_contract(
            node, unique_id, stage, sequence, previous_output, run_dir, row,
            category == "REJECT", node is selected[-1], names[stage])
        groups[stage].append(contract)

    verdict = "FAIL"
    if category in {"REJECT", "PENDING"}:
        verdict = category
    elif status_text not in _FAIL_STATUS:
        verdict = "PASS"
    delivered = verdict == "PASS"

    statistics = {}
    for node in reversed(selected):
        candidate = node.get("statistics")
        if isinstance(candidate, dict):
            statistics = candidate
            break
    last_operator = str(groups[last_stage][-1].get("operator") or "") if order else ""

    for stage in order:
        lines.append({
            "type": "stage_segment", "writtenBy": "operator", "stage": stage,
            "name": names.get(stage, stage), "operators": groups[stage],
        })
    if IS_STAGE_LAST_OPERATOR:
        lines.append(_oss_supplement(unique_id, last_stage, run_dir, result,
                                     output_entrypoint))

    outcome: Dict[str, Any] = {"type": "outcome", "writtenBy": "engineering",
                               "uniqueId": unique_id, "verdict": verdict}
    if verdict != "PASS":
        reason = _first_text_value(row.get("reason"), row.get("reason_code"),
                                   row.get("label"), row.get("decision"))
        if reason:
            outcome["reason"] = reason
        destination = _first_text_value(row.get("destination"), row.get("downstream"))
        if destination:
            outcome["downstream"] = destination
        if not reason and verdict == "FAIL":
            outcome["reason"] = str(status or "failed")
    marks = _row_marks(row, last_stage, last_operator)
    if marks:
        outcome["marked"] = marks[0].get("marked")
        outcome["marks"] = marks
    partial = _row_partial_filter(row, last_stage, last_operator, statistics)
    if partial:
        outcome["partialFilter"] = partial
    lines.append(outcome)

    # 规范：deliver_supplement 只在**交付打包后**补，被拦/待处理文件没有交付位置
    if IS_STAGE_LAST_OPERATOR and delivered:
        lines.append(_deliver_supplement(unique_id, run_dir, result, index,
                                         delivered, output_entrypoint))
    return lines


def _load_jsonl(path: Path) -> List[Dict[str, Any]]:
    if not path.is_file():
        return []
    rows: List[Dict[str, Any]] = []
    try:
        for line in path.read_text(encoding="utf-8").splitlines():
            value = json.loads(line)
            if isinstance(value, dict):
                rows.append(value)
    except (OSError, json.JSONDecodeError):
        return []
    return rows


def write_per_file_lineages(run_dir: Path, records: Sequence[Dict[str, Any]],
                            result: Dict[str, Any]) -> List[Path]:
    """Write one HTTP-contract JSONL sidecar for every input uniqueId."""
    run_dir = Path(run_dir)
    ids = _result_unique_ids(result, records, run_dir)
    if not ids:
        fallback = result.get("uniqueId") or result.get("unique_id") or run_dir.parent.parent.name
        if isinstance(fallback, str) and fallback:
            ids = [fallback]
    target_dir = run_dir / "lineage"
    target_dir.mkdir(parents=True, exist_ok=True)
    paths: List[Path] = []
    for unique_id in ids:
        try:
            safe_id = validate_unique_id(unique_id)
        except ValueError:
            safe_id = re.sub(r"[^0-9A-Za-z\u4e00-\u9fff._-]+", "_", unique_id)
        path = target_dir / ("lineage_%s.jsonl" % safe_id)
        current = lineage_record_for_file(unique_id, records, result, run_dir)
        previous = _load_jsonl(path)
        if previous:
            # Preserve the file header and prior stages, append only new stage/outcome lines.
            seen = {_dumps(item) for item in previous}
            current = previous + [item for item in current if _dumps(item) not in seen]
        path.write_text("".join(_dumps(item) + "\n" for item in current), encoding="utf-8")
        paths.append(path)
    result["lineageFiles"] = [str(path) for path in paths]
    result["perFileLineage"] = len(paths)
    return paths


def _text_members(run_dir: Path) -> List[Path]:
    """收集 run 目录下的文本类成员（跳过媒体、原始素材与既有 zip）。"""
    members: List[Path] = []
    for path in sorted(run_dir.rglob("*")):
        if not path.is_file():
            continue
        if any(part in _SKIP_DIRS for part in path.relative_to(run_dir).parts[:-1]):
            continue
        if path.suffix.lower() not in _TEXT_SUFFIXES:
            continue
        if path.name in {STAGE_ARTIFACT_NAME, TRANSPORT_REPORT_NAME}:
            continue
        members.append(path)
    return members


def build_stage_artifact(run_dir: Path, records: Sequence[Dict[str, Any]],
                         entrypoint: Optional[str] = None,
                         lineage_files: Optional[Sequence[Path]] = None) -> Optional[Path]:
    """把「拼接后的阶段血缘文本 + 该阶段文本类输出内容」打包成阶段产物 zip。

    规范正文第 7 条 / 流程图「阶段产物」：阶段末把该阶段所有血缘拼成一个文本，
    与该阶段输出内容一起打包 zip。返回 zip 路径；超过 ``MAX_ARTIFACT_BYTES``
    或无法打包时返回 ``None``（规范允许大数据量时去掉阶段产物下载）。
    """
    run_dir = Path(run_dir)
    joined = run_dir / STAGE_LINEAGE_NAME
    if not joined.is_file():
        joined = write_joined_lineage(run_dir, records)

    members = _text_members(run_dir)
    for path in lineage_files or ():
        path = Path(path)
        if path.is_file() and path not in members:
            members.append(path)
    manifest_member = run_dir / INPUT_MANIFEST_NAME
    if manifest_member.is_file() and manifest_member not in members:
        members.append(manifest_member)
    for path in sorted((run_dir / LINEAGE_DIRNAME).glob("lineage_*.jsonl")):
        if path.is_file() and path not in members:
            members.append(path)
    if joined not in members:
        members.insert(0, joined)
    else:
        members.remove(joined)
        members.insert(0, joined)

    total = sum(path.stat().st_size for path in members)
    if total > MAX_ARTIFACT_BYTES:
        return None

    # ---- 阶段产物登记：口径必须自洽（输入文件数 == 血缘文件数 == uniqueId 数）
    lineage_paths = [Path(path) for path in members
                     if Path(path).is_file()
                     and Path(path).name.startswith("lineage_")
                     and Path(path).suffix.lower() == ".jsonl"]
    lineage_rel: List[str] = []
    unique_ids: List[str] = []
    for path in lineage_paths:
        relative = path.relative_to(run_dir).as_posix()
        if relative not in lineage_rel:
            lineage_rel.append(relative)
        uid = path.stem[len("lineage_"):]
        if uid not in unique_ids:
            unique_ids.append(uid)
    expected = manifest_ids(load_input_manifest(run_dir))
    missing = [uid for uid in expected if uid not in unique_ids]
    stage_id, stage_name = current_stage()

    target = run_dir / STAGE_ARTIFACT_NAME
    temp = target.with_name(target.name + ".partial")
    with zipfile.ZipFile(temp, "w", zipfile.ZIP_DEFLATED) as archive:
        for path in members:
            archive.write(path, path.relative_to(run_dir).as_posix())
        archive.writestr("stage_artifact_manifest.json", json.dumps({
            "format": "crossmodal-stage-artifact-v1",
            "stageId": stage_id,
            "stageName": stage_name,
            "operatorNumber": OPERATOR_NUMBER,
            "entrypoint": entrypoint,
            "stageLineage": STAGE_LINEAGE_NAME,
            "isLastOperator": bool(IS_STAGE_LAST_OPERATOR),
            "inputFileCount": len(expected),
            "expectedUniqueIds": expected,
            "lineageFiles": lineage_rel,
            "lineageFileCount": len(lineage_rel),
            "uniqueIds": sorted(unique_ids),
            "uniqueIdCount": len(unique_ids),
            "missingLineageFiles": missing,
            "recordCount": len(unique_ids),
            "recordCountDefinition": ("输入文件/样本数：== inputFileCount == len(lineageFiles) "
                                      "== len(uniqueIds) == len(expectedUniqueIds)"),
            "operatorLineageRecords": len(records),
            "operatorLineageRecordDefinition": "本算子 stage_lineage 记录条数（非输入文件数）",
            "inputManifest": INPUT_MANIFEST_NAME,
            "members": [path.relative_to(run_dir).as_posix() for path in members],
            "generatedAt": datetime.now(_TZ).isoformat(timespec="seconds"),
        }, ensure_ascii=False, indent=2))
    temp.replace(target)
    return target


def _append_artifact(payload: Dict[str, Any], name: str, role: str) -> None:
    """往 manifest 结构（含 ``artifacts`` 列表）里登记一个成员，幂等。"""
    items = payload.get("artifacts")
    if not isinstance(items, list):
        return
    for item in items:
        if isinstance(item, dict) and item.get("path") == name:
            return
        if isinstance(item, str) and item == name:
            return
    items.append({"path": name, "role": role})


def _register(result: Dict[str, Any], name: str, role: str,
              manifest: Optional[Dict[str, Any]] = None) -> None:
    """把新增产物登记进 ``result["artifacts"]``、内存 manifest 与磁盘 manifest.json。"""
    artifacts = result.get("artifacts")
    if isinstance(artifacts, list):
        _append_artifact({"artifacts": artifacts}, name, role)
    if isinstance(manifest, dict):
        _append_artifact(manifest, name, role)

    for key in ("manifestPath", "manifest"):
        value = result.get(key)
        target: Optional[Path] = None
        if isinstance(value, str) and value:
            target = Path(value)
        elif isinstance(value, Path):
            target = value
        if target is None or not target.is_file():
            continue
        try:
            payload = json.loads(target.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError):
            continue
        if not isinstance(payload, dict):
            continue
        _append_artifact(payload, name, role)
        try:
            target.write_text(json.dumps(payload, ensure_ascii=False, indent=2),
                              encoding="utf-8")
        except OSError:
            pass


# --------------------------------------------------------------------------- #
# 3.5 输入清单 + 逐文件血缘跨阶段累计 + 兼容视图
# --------------------------------------------------------------------------- #
#
# 规范要求「一个输入文件 = 一份最终血缘文件」，因此每个阶段都做四件事：
#   ① 从上一阶段传入的 ``input/`` 读入 ``input_manifest.json`` 与 ``lineage/``；
#   ② 以 **input_manifest 的 uniqueId 全集**为输入全集，不从当前输出反推 UID；
#   ③ 对每个 UID **更新同一个** ``lineage/lineage_<uniqueId>.jsonl``：
#      追加本阶段 ``stage_segment``（同阶段追加到同一 segment 的 ``operators[]``）；
#   ④ 重排全局 ``seq``，保证行序恒为
#      ``file → seg → oss → … → outcome → deliver_supplement``。
#
# 阶段快照（``stage_lineage.jsonl``、``stage_context.json``）保留，但**不再**
# 当作逐文件最终结果；最终结果只认 ``lineage/lineage_<uniqueId>.jsonl``。

def stage_plan(number: Any = None, fallback_id: Optional[str] = None,
               fallback_name: Optional[str] = None) -> Tuple[str, str]:
    """返回 ``(stageId, stageName)``；编号不在链路计划里时退回算子自带常量。"""
    try:
        key: Any = int(number)
    except (TypeError, ValueError):
        key = None
    if key in CHAIN_STAGE_PLAN:
        return CHAIN_STAGE_PLAN[key]
    identity = fallback_id or STAGE_ID
    return identity, fallback_name or identity


def current_stage() -> Tuple[str, str]:
    """本算子所属的 ``(stageId, stageName)``。"""
    return stage_plan(OPERATOR_NUMBER, STAGE_ID, STAGE_NAME)


def stage_rank(stage_id: Any) -> int:
    """阶段在链路中的序号；未知阶段返回 ``-1``。"""
    return STAGE_RANK.get(str(stage_id), -1)


def _safe_uid(unique_id: Any) -> str:
    """逐文件血缘的落盘 id：合法 ``任务id_文件id`` 原样使用，否则做字符净化。"""
    try:
        return validate_unique_id(unique_id)
    except ValueError:
        return re.sub(r"[^0-9A-Za-z\u4e00-\u9fff._-]+", "_", str(unique_id)) or "unknown"


def _lineage_filename(unique_id: Any) -> str:
    return "lineage_%s.jsonl" % _safe_uid(unique_id)


# ---------------------------------------------------------------- 输入清单

def find_input_manifest(source: Any) -> Optional[Path]:
    """定位输入清单文件；目录下取不到时递归查找（交付包会解压到子目录）。"""
    if source is None:
        return None
    target = Path(source)
    if target.is_file():
        return target
    if not target.is_dir():
        return None
    direct = target / INPUT_MANIFEST_NAME
    if direct.is_file():
        return direct
    hits = sorted(path for path in target.rglob(INPUT_MANIFEST_NAME) if path.is_file())
    return hits[0] if hits else None


def load_input_manifest(source: Any) -> Optional[Dict[str, Any]]:
    """读取输入清单；``source`` 可以是目录（取目录内 ``input_manifest.json``）或文件。"""
    target = find_input_manifest(source)
    if target is None:
        return None
    try:
        payload = json.loads(target.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError):
        return None
    if not isinstance(payload, dict):
        return None
    items = payload.get("files")
    if not isinstance(items, list):
        items = payload.get("entries")
    if not isinstance(items, list):
        return None
    payload["files"] = [item for item in items if isinstance(item, dict)]
    return payload


def write_input_manifest(directory: Path, payload: Dict[str, Any]) -> Path:
    """把输入清单落盘到 ``directory/input_manifest.json``。"""
    target = Path(directory)
    target.mkdir(parents=True, exist_ok=True)
    path = target / INPUT_MANIFEST_NAME
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    return path


def manifest_ids(payload: Optional[Dict[str, Any]]) -> List[str]:
    """输入清单里的 ``uniqueId`` 全集（保持清单顺序、去重）。"""
    ids: List[str] = []
    if not isinstance(payload, dict):
        return ids
    for item in payload.get("files") or []:
        if not isinstance(item, dict):
            continue
        value = _first_text_value(item.get("uniqueId"), item.get("unique_id"))
        if value and value not in ids:
            ids.append(value)
    return ids


def manifest_entry(payload: Optional[Dict[str, Any]], unique_id: str) -> Dict[str, Any]:
    """取某个 ``uniqueId`` 的清单条目（取不到返回空字典）。"""
    if not isinstance(payload, dict):
        return {}
    for item in payload.get("files") or []:
        if isinstance(item, dict) and _first_text_value(
                item.get("uniqueId"), item.get("unique_id")) == unique_id:
            return item
    return {}


def _input_source_dirs(run_dir: Optional[Path]) -> List[Path]:
    """能解析输入素材的目录：本 run 目录 → 上传 ``input/`` → 任务目录。"""
    roots: List[Path] = []
    if run_dir is not None:
        run_dir = Path(run_dir)
        roots.append(run_dir)
        task_root = run_dir.parent.parent
        roots.extend([task_root / "input", task_root])
    return roots


def build_input_manifest(result: Dict[str, Any], records: Sequence[Dict[str, Any]],
                         run_dir: Optional[Path] = None,
                         task_id: Optional[str] = None) -> Dict[str, Any]:
    """**输入接入阶段**建立输入清单：每个输入文件的身份 / 来源 / 字节数 / 校验和。

    ``uniqueId = 任务id_文件id``，此后所有阶段一律沿用清单里的 ``uniqueId``，
    绝不用 ``r8_23``/``r8_26`` 这类算子运行 id 充当输入文件 UID。
    """
    run_dir = Path(run_dir) if run_dir is not None else resolve_run_dir(result)
    task = task_id or _resolve_task(result, records)
    ids = _result_unique_ids(result, records, run_dir)
    meta = result.get("_lineage_file_meta") or {}
    roots = _input_source_dirs(run_dir)
    files: List[Dict[str, Any]] = []
    for offset, unique_id in enumerate(ids):
        info = meta.get(unique_id) if isinstance(meta.get(unique_id), dict) else {}
        row = info.get("row") if isinstance(info.get("row"), dict) else {}
        reference = _media_reference(row) or _row_file_part(row)
        member: Optional[Path] = None
        for root in roots:
            member = _resolve_run_member(root, reference)
            if member is not None:
                break
        fingerprint = _file_fingerprint(member)
        if not fingerprint.get("checksum"):
            size = _deep_number(row, ("bytes", "size", "file_size", "fileSize"))
            digest = _deep_value(row, ("checksum", "sha256", "md5", "file_hash"))
            fingerprint = {
                "bytes": (fingerprint.get("bytes")
                          if fingerprint.get("bytes") is not None
                          else (int(size) if size is not None else 0)),
                "checksum": digest or "unknown",
            }
        safe_id = _safe_uid(unique_id)
        files.append({
            "uniqueId": safe_id,
            "taskId": task,
            "fileId": safe_id.split("_", 1)[1] if "_" in safe_id else safe_id,
            "file": (Path(str(reference).replace("\\", "/")).name
                     if reference else safe_id),
            "sourceRef": _source_ref(row, {}, result),
            "datasetVersion": (_deep_value(row, _VERSION_KEYS)
                               or _first_text_value(result.get("datasetVersion"))
                               or "unknown"),
            "ownerTeam": OWNER_TEAM,
            "bytes": int(fingerprint.get("bytes") or 0),
            "checksum": fingerprint.get("checksum") or "unknown",
            "rowIndex": offset,
        })
    stage_id, stage_name = current_stage()
    return {
        "format": INPUT_MANIFEST_FORMAT,
        "taskId": task,
        "stageId": stage_id,
        "stageName": stage_name,
        "operatorNumber": OPERATOR_NUMBER,
        "generatedAt": _now(),
        "fileCount": len(files),
        "files": files,
    }


# ---------------------------------------------------------------- 上一阶段传入的血缘

def prior_lineage_path(source: Any, unique_id: Any) -> Optional[Path]:
    """定位上一阶段传入的 ``<source>/lineage/lineage_<uniqueId>.jsonl``。

    目录下直接找不到时递归查找 ``lineage/``（交付包解压后的子目录形态）。
    """
    if source is None:
        return None
    root = Path(source)
    name = _lineage_filename(unique_id)
    for directory in (root / LINEAGE_DIRNAME, root):
        candidate = directory / name
        if candidate.is_file():
            return candidate
    if not root.is_dir():
        return None
    hits: List[Path] = []
    try:
        for directory in root.rglob(LINEAGE_DIRNAME):
            if not directory.is_dir():
                continue
            candidate = directory / name
            if candidate.is_file():
                hits.append(candidate)
    except OSError:
        return None
    return sorted(hits)[0] if hits else None


def _read_lineage_lines(path: Optional[Path]) -> List[Dict[str, Any]]:
    if path is None:
        return []
    return [item for item in _load_jsonl(Path(path)) if isinstance(item, dict)]


def _current_operator_node(result: Dict[str, Any],
                           records: Sequence[Dict[str, Any]]) -> Optional[Dict[str, Any]]:
    """本次运行的「当前算子节点」（用于生成本阶段 ``operators[]`` 条目）。

    优先取正式编号等于本算子编号的节点，避免误取上游/历史节点。
    """
    candidates: List[Dict[str, Any]] = []
    value = result.get("lineage") or result.get("node") or result.get("localNode")
    if isinstance(value, list):
        value = next((item for item in reversed(value) if isinstance(item, dict)), None)
    if isinstance(value, dict) and _is_lineage_node(value):
        candidates.append(value)
    candidates.extend(record for record in reversed(list(records))
                      if isinstance(record, dict) and _is_lineage_node(record))
    for item in candidates:
        number, _label, _version, _stage = _node_operator_meta(item)
        if number == OPERATOR_NUMBER:
            return item
    return candidates[0] if candidates else None


def _output_entrypoint(result: Dict[str, Any], run_dir: Optional[Path]) -> Optional[str]:
    """本次阶段输出入口文件名（响应体缺失时用 run 目录主输出兜底）。"""
    value = _first_text_value(result.get("outputFile"), result.get("output_file"))
    if not value and run_dir is not None:
        for candidate in sorted(Path(run_dir).glob("*.jsonl")):
            if candidate.name in {STAGE_LINEAGE_NAME, TASK_LINEAGE_NAME, SAMPLE_LINEAGE_NAME}:
                continue
            name = candidate.stem.lower()
            if name.startswith("lineage") or any(token in name for token in _SIDECAR_TOKENS):
                continue
            value = candidate.name
            break
    return Path(value.replace("\\", "/")).name if value else None


# ---------------------------------------------------------------- 跨阶段累计

def _segment_slot(segments: List[Dict[str, Any]], stage_id: str,
                  stage_name: str) -> Dict[str, Any]:
    """给本阶段算子挑一个 ``stage_segment``，保证阶段集合始终按链路顺序单调排列。

    * 与最后一段同阶段 → 复用该段（同阶段多算子只产出一个 ``stage_segment``）；
    * 阶段前进 → 追加新段；
    * 阶段回退（人工复核后的再处置等**回环**调用）→ 合并进已有的同阶段段，
      绝不追加出 ``S7 -> S4`` 这种倒序段。
    """
    rank = stage_rank(stage_id)
    if segments:
        last = segments[-1]
        if str(last.get("stage") or "") == stage_id:
            return last
        if rank < 0 or rank > stage_rank(last.get("stage")):
            target = {"type": "stage_segment", "writtenBy": "operator",
                      "stage": stage_id, "name": stage_name, "operators": []}
            segments.append(target)
            return target
        for segment in segments:
            if str(segment.get("stage") or "") == stage_id:
                return segment
        # 新阶段但链路序号在上一个阶段之前（如 S7 之后的 S6）→ 按序号插入，
        # 保证阶段集合始终单调前进，绝不出现 ``S7 -> S6`` 这种倒序段。
        target = {"type": "stage_segment", "writtenBy": "operator",
                  "stage": stage_id, "name": stage_name, "operators": []}
        index = len(segments)
        for position, segment in enumerate(segments):
            if stage_rank(segment.get("stage")) > rank:
                index = position
                break
        segments.insert(index, target)
        return target
    target = {"type": "stage_segment", "writtenBy": "operator",
              "stage": stage_id, "name": stage_name, "operators": []}
    segments.append(target)
    return target


def accumulate_lineage(unique_id: str, previous: Sequence[Dict[str, Any]],
                       entry: Dict[str, Any], records: Sequence[Dict[str, Any]],
                       result: Dict[str, Any], run_dir: Optional[Path]) -> List[Dict[str, Any]]:
    """把「上一阶段传入的同一份血缘」+「本阶段节点」合并成一份最终血缘。

    行序恒定：``file`` → （``stage_segment`` → ``oss_supplement``）× N
    → ``outcome`` → （交付成功时）``deliver_supplement``。
    """
    stage_id, stage_name = current_stage()
    header: Optional[Dict[str, Any]] = None
    segments: List[Dict[str, Any]] = []
    osses: List[Dict[str, Any]] = []
    prior_outcome: Optional[Dict[str, Any]] = None
    prior_deliver: Optional[Dict[str, Any]] = None
    for item in previous:
        kind = item.get("type")
        if kind == "file" and header is None:
            header = dict(item)
        elif kind == "stage_segment":
            segments.append(dict(item))
        elif kind == "oss_supplement":
            osses.append(dict(item))
        elif kind == "outcome":
            prior_outcome = dict(item)
        elif kind == "deliver_supplement":
            prior_deliver = dict(item)

    meta = result.get("_lineage_file_meta") or {}
    info = meta.get(unique_id) if isinstance(meta.get(unique_id), dict) else None
    present = info is not None
    row = info.get("row") if (info and isinstance(info.get("row"), dict)) else {}
    category = str(info.get("category") or "").upper() if info else ""
    index = info.get("index") if info else None
    status_text = str(result.get("status") or "success").lower()
    output_entrypoint = _output_entrypoint(result, run_dir)

    # ---- file 头：继承输入接入阶段的身份与校验，绝不被后续阶段抹掉
    if header is None:
        header = _lineage_file_header(unique_id, records, result, run_dir)
    for key in ("taskId", "fileId", "file", "sourceRef", "datasetVersion",
                "ownerTeam", "rowIndex"):
        if entry.get(key) not in (None, ""):
            header.setdefault(key, entry[key])
    for key in ("bytes", "checksum"):
        if entry.get(key) not in (None, "", "unknown"):
            header.setdefault(key, entry[key])
    header["type"] = "file"
    header["writtenBy"] = "engineering"
    header["uniqueId"] = unique_id

    # ---- 本阶段 operators[]：同阶段追加，同 operator+runId 幂等去重
    if present:
        node = _current_operator_node(result, records)
        if node is not None:
            previous_output = "/workspace/%s/upload/%s" % (unique_id, header.get("file"))
            for segment in segments:
                for op in segment.get("operators") or []:
                    value = op.get("output")
                    if isinstance(value, str) and value:
                        previous_output = value
            contract, _output = _operator_contract(
                node, unique_id, stage_id, 0, previous_output, run_dir, row,
                category == "REJECT", True, stage_name)
            target = _segment_slot(segments, stage_id, stage_name)
            operators = target.setdefault("operators", [])
            # 去重键：uniqueId + stageId + operator + runId + 调用时刻
            # （同一阶段重复调用不重复写事件；不同调用/分支调用必须各自留痕）
            identity = (
                contract.get("operator"), contract.get("runId"), contract.get("ts"),
                (contract.get("operatorRecord") or {}).get("uniqueId"),
                unique_id, stage_id,
            )
            existing = {
                (op.get("operator"), op.get("runId"), op.get("ts"),
                 (op.get("operatorRecord") or {}).get("uniqueId"), unique_id, stage_id)
                for op in operators}
            if identity not in existing:
                operators.append(contract)
            if not target.get("name"):
                target["name"] = stage_name

    # ---- oss_supplement：阶段打包后补；本算子是本阶段末算子时补本阶段
    if IS_STAGE_LAST_OPERATOR and any(seg.get("stage") == stage_id for seg in segments):
        osses = [item for item in osses if item.get("stage") != stage_id]
        osses.append(_oss_supplement(unique_id, stage_id, run_dir, result,
                                     output_entrypoint))

    # ---- outcome：显式台账结论优先；无记录时保留/降级，绝不错标 PASS
    prior_verdict = str(prior_outcome.get("verdict") or "") if prior_outcome else ""
    if present:
        if category in {"REJECT", "PENDING"}:
            verdict = category
        elif status_text in _FAIL_STATUS:
            verdict = "FAIL"
        else:
            verdict = "PASS"
    elif prior_verdict and prior_verdict != "PASS":
        verdict = prior_verdict
    else:
        verdict = "PENDING"
    # 规范口径：``delivered`` 与 ``verdict`` 必须一致（PASS ⇒ true，其余 ⇒ false）
    delivered = verdict == "PASS"

    outcome: Dict[str, Any] = {"type": "outcome", "writtenBy": "engineering",
                               "uniqueId": unique_id, "verdict": verdict,
                               "delivered": bool(delivered)}
    if verdict != "PASS":
        reason = _first_text_value(row.get("reason"), row.get("reason_code"),
                                   row.get("label"), row.get("decision"))
        if reason is None and prior_outcome is not None:
            reason = _first_text_value(prior_outcome.get("reason"))
        if reason is None:
            reason = (str(result.get("status") or "未通过")
                      if present else "本阶段未产生该文件的输出记录，未继续流转")
        outcome["reason"] = reason
        downstream = _first_text_value(row.get("destination"), row.get("downstream"))
        if downstream is None and prior_outcome is not None:
            downstream = _first_text_value(prior_outcome.get("downstream"))
        outcome["downstream"] = downstream or "不进入后续交付集"
    statistics: Dict[str, Any] = {}
    for node in reversed(list(records)):
        if isinstance(node, dict) and isinstance(node.get("statistics"), dict):
            statistics = node["statistics"]
            break
    last_operator = ""
    for segment in reversed(segments):
        operators = segment.get("operators") or []
        if operators:
            last_operator = str(operators[-1].get("operator") or "")
            break
    marks = _row_marks(row, stage_id, last_operator)
    if marks:
        outcome["marked"] = marks[0].get("marked")
        outcome["marks"] = marks
    partial = _row_partial_filter(row, stage_id, last_operator, statistics)
    if partial:
        outcome["partialFilter"] = partial

    # ---- deliver_supplement：仅交付阶段且交付成功
    deliver = prior_deliver
    if OPERATOR_NUMBER in DELIVERY_OPERATOR_NUMBERS and verdict == "PASS":
        deliver = _deliver_supplement(unique_id, run_dir, result, index, delivered,
                                      output_entrypoint)

    # ---- 装配 + 全局 seq 重排
    sequence = 0
    lines: List[Dict[str, Any]] = [header]
    for segment in segments:
        for op in segment.get("operators") or []:
            sequence += 1
            op["seq"] = sequence
        lines.append(segment)
        supplement = next((item for item in osses
                           if item.get("stage") == segment.get("stage")), None)
        if supplement is not None:
            lines.append(supplement)
    lines.append(outcome)
    if deliver is not None:
        lines.append(deliver)
    return lines


def write_per_file_lineages(run_dir: Path, records: Sequence[Dict[str, Any]],
                            result: Dict[str, Any],
                            prior_dir: Optional[Path] = None,
                            manifest: Optional[Dict[str, Any]] = None) -> List[Path]:
    """写/更新每个输入文件的**最终**血缘文件（跨阶段累计，一个 UID 一份）。"""
    run_dir = Path(run_dir)
    if not isinstance(manifest, dict):
        manifest = load_input_manifest(prior_dir)
    if manifest is None:
        manifest = build_input_manifest(result, records, run_dir)
    try:
        write_input_manifest(run_dir, manifest)
    except OSError:
        pass
    result["inputManifestPath"] = str(run_dir / INPUT_MANIFEST_NAME)
    result["inputFileCount"] = len(manifest_ids(manifest))

    ids = manifest_ids(manifest)
    # 本阶段真正可见的文件集合（同时填充 ``_lineage_file_meta``，供结局判定使用）
    stage_ids = _result_unique_ids(result, records, run_dir)
    if not ids:
        ids = stage_ids
    if not ids:
        fallback = result.get("uniqueId") or result.get("unique_id") or run_dir.parent.parent.name
        if isinstance(fallback, str) and fallback:
            ids = [fallback]

    target_dir = run_dir / LINEAGE_DIRNAME
    target_dir.mkdir(parents=True, exist_ok=True)
    paths: List[Path] = []
    for unique_id in ids:
        safe_id = _safe_uid(unique_id)
        path = target_dir / _lineage_filename(safe_id)
        previous = _read_lineage_lines(prior_lineage_path(prior_dir, safe_id))
        if not previous:
            previous = _read_lineage_lines(path)
        entry = manifest_entry(manifest, safe_id)
        lines = accumulate_lineage(safe_id, previous, entry, records, result, run_dir)
        path.write_text("".join(_dumps(item) + "\n" for item in lines), encoding="utf-8")
        paths.append(path)
    result["lineageFiles"] = [str(path) for path in paths]
    result["perFileLineage"] = len(paths)
    result["lineageDir"] = str(target_dir)
    return paths


# ---------------------------------------------------------------- 兼容视图与索引

def _read_lineage_view(run_dir: Path) -> List[Tuple[str, List[Dict[str, Any]]]]:
    """读取 run 目录内全部逐文件血缘（按文件名排序，返回 ``(uid, 行)``）。"""
    items: List[Tuple[str, List[Dict[str, Any]]]] = []
    directory = Path(run_dir) / LINEAGE_DIRNAME
    if not directory.is_dir():
        return items
    for path in sorted(directory.glob("lineage_*.jsonl")):
        uid = path.name[len("lineage_"):-len(".jsonl")]
        items.append((uid, _read_lineage_lines(path)))
    return items


def _view_of(uid: str, lines: Sequence[Dict[str, Any]], manifest: Optional[Dict[str, Any]],
             step_of: Dict[str, int]) -> Dict[str, Any]:
    """把 HTTP 版逐文件血缘映射成 ``lineage_example.jsonl`` 的 sample_lineage 视图。"""
    entry = manifest_entry(manifest, uid)
    header = next((item for item in lines if item.get("type") == "file"), {})
    outcome = next((item for item in reversed(list(lines))
                    if item.get("type") == "outcome"), {})
    steps: List[Dict[str, Any]] = []
    for item in lines:
        if item.get("type") != "stage_segment":
            continue
        stage = str(item.get("stage") or "")
        for op in item.get("operators") or []:
            process = op.get("process") if isinstance(op.get("process"), dict) else {}
            tool = op.get("tool") if isinstance(op.get("tool"), dict) else {}
            step: Dict[str, Any] = {
                "step": step_of.get(stage, 0),
                "stage": item.get("name") or stage,
                "fn": tool.get("id") or "",
                "op": op.get("operator") or "",
                "action": process.get("action") or "",
                "detail": process.get("detail") or "",
                "operator_number": op.get("operatorNumber"),
                "stage_id": stage,
            }
            if op.get("output"):
                step["out_artifact"] = Path(str(op["output"])).name
            steps.append(step)
    source = {
        "file": header.get("file") or entry.get("file") or uid,
        "checksum": header.get("checksum") or entry.get("checksum") or "unknown",
        "bytes": header.get("bytes") if header.get("bytes") is not None
        else entry.get("bytes", 0),
        "row_index": header.get("rowIndex", entry.get("rowIndex")),
        "dataset_code": entry.get("sourceRef") or header.get("sourceRef") or "unknown",
    }
    result_block: Dict[str, Any] = {
        "verdict": outcome.get("verdict") or "PENDING",
        "delivered": bool(outcome.get("delivered")),
    }
    for key in ("reason", "downstream", "marked", "marks", "partialFilter"):
        if outcome.get(key) is not None:
            result_block[key] = outcome[key]
    result_block["dataset_version"] = (header.get("datasetVersion")
                                      or entry.get("datasetVersion") or "unknown")
    return {
        "record_type": "sample_lineage",
        "sample_id": entry.get("fileId") or uid,
        "unique_id": uid,
        "source": source,
        "lineage": steps,
        "result": result_block,
    }


def write_lineage_views(run_dir: Path, manifest: Optional[Dict[str, Any]] = None,
                        task_id: Optional[str] = None,
                        records: Sequence[Dict[str, Any]] = ()) -> List[Path]:
    """写兼容视图：``lineage_index.json`` + ``task_lineage.jsonl`` + ``sample_lineage.jsonl``。

    视图只是 ``lineage_example.jsonl`` 的兼容投影，**不替代**逐文件 HTTP 血缘文件。
    """
    run_dir = Path(run_dir)
    if not isinstance(manifest, dict):
        manifest = load_input_manifest(run_dir)
    expected = manifest_ids(manifest)
    items = _read_lineage_view(run_dir)
    actual = [uid for uid, _lines in items]
    result: Dict[str, Any] = {}

    stage_order: List[str] = []
    for _uid, lines in items:
        for item in lines:
            if item.get("type") == "stage_segment":
                stage = str(item.get("stage") or "")
                if stage and stage not in stage_order:
                    stage_order.append(stage)
    step_of = {stage: index for index, stage in enumerate(stage_order)}
    stage_names: Dict[str, str] = {}
    for _uid, lines in items:
        for item in lines:
            if item.get("type") == "stage_segment" and item.get("stage"):
                stage_names.setdefault(str(item["stage"]), str(item.get("name") or ""))

    coverage: List[Dict[str, Any]] = []
    verdicts: Dict[str, int] = {}
    written: List[Path] = []
    sample_rows: List[Dict[str, Any]] = []
    for uid, lines in items:
        stages = [str(item.get("stage")) for item in lines
                  if item.get("type") == "stage_segment" and item.get("stage")]
        outcome = next((item for item in reversed(lines)
                        if item.get("type") == "outcome"), {})
        verdict = str(outcome.get("verdict") or "PENDING")
        verdicts[verdict] = verdicts.get(verdict, 0) + 1
        coverage.append({
            "uniqueId": uid,
            "stageIds": stages,
            "stageCount": len(stages),
            "verdict": verdict,
            "delivered": bool(outcome.get("delivered")),
            "lineageFile": "%s/%s" % (LINEAGE_DIRNAME, _lineage_filename(uid)),
        })
        sample_rows.append(_view_of(uid, lines, manifest, step_of))

    missing = [uid for uid in expected if uid not in actual]
    extra = [uid for uid in actual if uid not in expected]
    index = {
        "format": "crossmodal-lineage-index-v1",
        "taskId": task_id or (manifest or {}).get("taskId") or "",
        "stageId": current_stage()[0],
        "operatorNumber": OPERATOR_NUMBER,
        "generatedAt": _now(),
        "expectedUniqueIds": expected,
        "actualUniqueIds": actual,
        "expectedCount": len(expected),
        "actualCount": len(actual),
        "lineageFileCount": len(items),
        "missingUniqueIds": missing,
        "extraUniqueIds": extra,
        "consistent": bool(not missing and not extra and bool(expected)),
        "stageOrder": stage_order,
        "stageNames": stage_names,
        "verdictCounts": verdicts,
        "coverage": coverage,
    }
    index_path = run_dir / LINEAGE_INDEX_NAME
    index_path.write_text(json.dumps(index, ensure_ascii=False, indent=2), encoding="utf-8")
    written.append(index_path)

    sample_path = run_dir / SAMPLE_LINEAGE_NAME
    sample_path.write_text("".join(_dumps(row) + "\n" for row in sample_rows),
                           encoding="utf-8")
    written.append(sample_path)

    summary: List[Dict[str, Any]] = []
    for stage in stage_order:
        rows = [row for row in coverage if stage in row["stageIds"]]
        rejected = [row for row in rows if row["verdict"] != "PASS"]
        operators: List[str] = []
        for _uid, lines in items:
            for item in lines:
                if item.get("type") == "stage_segment" and item.get("stage") == stage:
                    for op in item.get("operators") or []:
                        name = str(op.get("operator") or "")
                        if name and name not in operators:
                            operators.append(name)
        summary.append({
            "step": step_of.get(stage, 0),
            "stage": stage_names.get(stage) or stage,
            "stage_id": stage,
            "operators": len(operators),
            "in": len(rows),
            "out": len(rows) - len(rejected),
            "rejected": len(rejected),
        })
    task_row = {
        "record_type": "task_lineage",
        "task_id": task_id or (manifest or {}).get("taskId") or "",
        "task_no": (manifest or {}).get("taskNo") or (manifest or {}).get("taskId") or "",
        "task_name": "跨模态多模态语料链路",
        "line": {"id": (manifest or {}).get("taskId") or "",
                 "type": "MULTIMODAL", "name": "跨模态预训练语料生产"},
        "template": {"name": current_stage()[1], "spec_version": "MTY-260926"},
        "stage_summary": summary,
        "dataset_flow": [{
            "node": "task_input",
            "unique_ids": expected,
            "input_file_count": len(expected),
            "snapshot_asset": INPUT_MANIFEST_NAME,
            "action": "输入快照锁定，后续阶段只读",
        }],
        "input_file_count": len(expected),
        "lineage_file_count": len(items),
        "verdict_counts": verdicts,
    }
    task_path = run_dir / TASK_LINEAGE_NAME
    task_path.write_text(_dumps(task_row) + "\n", encoding="utf-8")
    written.append(task_path)
    result["lineageIndex"] = str(index_path)
    result["taskLineage"] = str(task_path)
    result["sampleLineage"] = str(sample_path)
    return written


# --------------------------------------------------------------------------- #
# 4. 统一挂载入口
# --------------------------------------------------------------------------- #

def upload_input_dir(run_dir: Any) -> Optional[Path]:
    """本次运行对应的**上传目录**（上一阶段传入的血缘与清单在此）。

    多数算子上传到 ``<task>/input/``；少数算子直接解压到 ``<task>/``（无 ``input``
    子目录），因此先找 ``input`` 再用任务根目录兜底。
    """
    if run_dir is None:
        return None
    task_root = Path(run_dir).parent.parent
    candidate = task_root / "input"
    if candidate.is_dir():
        return candidate
    if task_root.is_dir() and _upload_root_evidence(task_root):
        return task_root
    return None

def _upload_root_evidence(task_root: Path) -> bool:
    """裸任务根兜底前，确认它真是一个上传根（防把外层目录当任务根扫进来）。

    证据：顶层直接有 ``input_manifest.json`` / ``input/`` / ``lineage/`` /
    ``.zip`` / ``.jsonl``，或顶层某个子目录的一层之内有
    ``input_manifest.json`` / ``lineage/`` / ``input/``（解压到任务根的形态）。
    """
    try:
        entries = list(task_root.iterdir())
    except OSError:
        return False
    for item in entries:
        name = item.name.lower()
        if item.is_file():
            if name == INPUT_MANIFEST_NAME or item.suffix.lower() in {".zip", ".jsonl"}:
                return True
            continue
        if name in {"input", "lineage"}:
            return True
        try:
            for child in item.iterdir():
                child_name = child.name.lower()
                if child.is_file() and child_name == INPUT_MANIFEST_NAME:
                    return True
                if child.is_dir() and child_name in {"input", "lineage"}:
                    return True
        except OSError:
            continue
    return False


# --------------------------------------------------------------------------- #
# 4.1 任务级最终输出（阶段末算子增量合并，见交付包根目录 task_output.py）
# --------------------------------------------------------------------------- #

def _task_output_context(run_dir: Path, input_manifest: Optional[Dict[str, Any]],
                         result: Dict[str, Any],
                         lineage_files: Sequence[Path] = ()) -> Optional[Tuple[str, Path]]:
    """解析任务级输出的 ``(taskId, 输出根)``；解析不出时返回 ``None``（跳过写入）。

    taskId 依次取输入清单、结果字典、逐文件血缘的 ``file`` 头（HTTP 链路的
    适配器不传 ``input_manifest.json``，血缘头是最可靠的兜底）；输出根依次取
    环境变量 ``TASK_OUTPUT_ROOT``、``OUTPUT_DIR``，都没有时用 run 目录向上
    找到的任务目录的上级。
    """
    task_id = ""
    if isinstance(input_manifest, dict):
        raw = input_manifest.get("taskId")
        if isinstance(raw, str) and raw.strip():
            task_id = raw.strip()
    if not task_id:
        raw = result.get("taskId")
        if isinstance(raw, str) and raw.strip():
            task_id = raw.strip()
    if not task_id:
        for path in lineage_files:
            try:
                with open(str(path), "r", encoding="utf-8") as handle:
                    first = handle.readline()
            except OSError:
                continue
            if not first.strip():
                continue
            try:
                row = json.loads(first)
            except ValueError:
                continue
            raw = row.get("taskId") if isinstance(row, dict) else None
            if isinstance(raw, str) and raw.strip():
                task_id = raw.strip()
                break
    if not task_id:
        return None
    root = ""
    for key in ("TASK_OUTPUT_ROOT", "OUTPUT_DIR"):
        raw = os.environ.get(key)
        if raw and raw.strip():
            root = raw.strip()
            break
    if not root:
        for parent in Path(run_dir).resolve().parents:
            name = parent.name
            if name == task_id or name.startswith(task_id + "_"):
                root = str(parent.parent)
                break
    if not root:
        return None
    return task_id, Path(root)


def _publish_task_output(run_dir: Path, lineage_files: Sequence[Path],
                         result: Dict[str, Any],
                         input_manifest: Optional[Dict[str, Any]]) -> None:
    """阶段末算子把本阶段完整血缘增量写入任务级最终输出（《输出格式.md》§3）。

    实现在交付包根目录 ``task_output.py``：扫描同一任务、同一阶段各算子的 run
    目录，把本阶段全部节点合成一个 ``stage_segment`` 后合并进
    ``<OUTPUT_DIR>/<taskId>/output/lineage/jsonl/``；交付阶段（#23）同时把交付包
    复制进 ``data/zip/``。全程不抛异常——失败只记 warning，绝不影响 ``/predict``。
    """
    try:
        context = _task_output_context(run_dir, input_manifest, result, lineage_files)
        if context is None:
            logging.getLogger(__name__).warning(
                "任务级输出跳过：解析不出 taskId/输出根 run_dir=%s", run_dir)
            return
        task_id, root = context
        stage_id, _stage_name = current_stage()
        package_root = Path(__file__).resolve().parents[2]
        if str(package_root) not in sys.path:
            sys.path.append(str(package_root))
        import task_output
        summary = task_output.publish_stage(
            run_dir=Path(run_dir),
            lineage_files=[Path(path) for path in lineage_files],
            result=result,
            task_id=task_id,
            stage_id=stage_id,
            operator_number=OPERATOR_NUMBER,
            output_root=root,
        )
        logging.getLogger(__name__).info(
            "任务级输出完成 task=%s stage=%s written=%s skipped=%s errors=%s",
            task_id, stage_id, summary.get("written"), summary.get("skipped"),
            summary.get("errors"))
    except Exception:  # noqa: BLE001 - 任务级输出绝不阻断业务响应
        logging.getLogger(__name__).warning("任务级输出写入失败", exc_info=True)


def prepare_stage_result(result: Any, request_is_last: Optional[bool] = None,
                         run_dir: Optional[Path] = None,
                         manifest: Optional[Dict[str, Any]] = None) -> Any:
    """``/predict`` 拿到运行结果后的统一后处理（由各算子 app.py 调用）。

    1. 给全部血缘节点补 ``isLastOperator``；
    2. 读入上一阶段传入的 ``input_manifest.json`` + ``lineage/``，**累计**写出
       每个输入文件的最终血缘 ``lineage/lineage_<uniqueId>.jsonl``；
    3. 写阶段血缘拼接文本 ``stage_lineage.jsonl``（阶段快照，不是最终结果）；
    4. 写兼容视图 ``lineage_index.json`` / ``task_lineage.jsonl`` /
       ``sample_lineage.jsonl``；
    5. 阶段末算子（``IS_STAGE_LAST_OPERATOR``）产出 ``stage_artifact.zip``；
    6. 阶段末算子把本阶段完整血缘增量写入任务级最终输出
       ``<OUTPUT_DIR>/<taskId>/output/``（见 :func:`_publish_task_output`）。

    ``run_dir`` / ``manifest`` 用于结果字典里没有运行目录线索的算子
    （例如在 app.py 内自行落盘的算子）。失败运行只做字段补齐，不产出文件。
    """
    if not isinstance(result, dict):
        return result
    annotate_result(result, request_is_last)
    if result.get("status") == "failed":
        return result
    run_dir = Path(run_dir) if run_dir is not None else resolve_run_dir(result)
    if run_dir is None:
        return result
    try:
        run_dir.mkdir(parents=True, exist_ok=True)
    except OSError:
        return result

    records = collect_records(result)
    if not records:
        return result
    public_records = _public_records(result, records)

    prior_dir = upload_input_dir(run_dir)
    input_manifest = load_input_manifest(prior_dir)
    try:
        lineage_files = write_per_file_lineages(run_dir, public_records, result,
                                                prior_dir=prior_dir,
                                                manifest=input_manifest)
        write_joined_lineage(run_dir, public_records)
        _register(result, STAGE_LINEAGE_NAME, "sidecar", manifest)
        _register(result, INPUT_MANIFEST_NAME, "input_manifest", manifest)
        for lineage_file in lineage_files:
            _register(result, lineage_file.relative_to(run_dir).as_posix(), "lineage", manifest)
        for view in write_lineage_views(run_dir, input_manifest):
            role = ("lineage_index" if view.suffix == ".json" else "compat_view")
            _register(result, view.name, role, manifest)
        result.pop("_lineage_file_meta", None)
    except OSError:
        return result

    entrypoint = result.get("entrypoint")
    if not isinstance(entrypoint, str) or not entrypoint:
        output = result.get("outputFile")
        entrypoint = Path(output).name if isinstance(output, str) and output else None

    if IS_STAGE_LAST_OPERATOR:
        artifact = build_stage_artifact(run_dir, public_records, entrypoint, lineage_files)
        if artifact is not None:
            _register(result, artifact.name, STAGE_ARTIFACT_ROLE, manifest)
            result["stageArtifact"] = str(artifact)

    result["isLastOperator"] = bool(IS_STAGE_LAST_OPERATOR)

    report = run_transport(run_dir, public_records, result, request_is_last, entrypoint=entrypoint)
    if report.get("reportPath"):
        _register(result, TRANSPORT_REPORT_NAME, TRANSPORT_ARTIFACT_ROLE, manifest)
    if IS_STAGE_LAST_OPERATOR:
        _publish_task_output(run_dir, lineage_files, result, input_manifest)
    return result


# --------------------------------------------------------------------------- #
# 5. OSS 归档 + 关系入库（规范流程图 F2/F3/F4/F5）
# --------------------------------------------------------------------------- #
#
# 规范要求：拼接后的血缘文本上传 OSS、阶段产物 zip 上传 OSS，并记录
# 「任务/源文件/血缘文件/阶段/区域(槽)/去向/输出文件」的关系入库。
# 生产环境配置对象存储与库连接即可直连上云；未配置时退化为本地镜像目录 +
# 本地 SQLite，对象键与库表字段完全一致，交付环境零第三方依赖即可验收。

_RELATION_COLUMNS: Tuple[Tuple[str, str], ...] = (
    ("relation_id", "TEXT PRIMARY KEY"),
    ("task_id", "TEXT"),
    ("file_id", "TEXT"),
    ("unique_id", "TEXT"),
    ("stage_id", "TEXT"),
    ("slot_id", "TEXT"),
    ("operator", "TEXT"),
    ("operator_number", "TEXT"),
    ("operator_version", "TEXT"),
    ("direction", "TEXT"),
    ("branch", "TEXT"),
    ("status", "TEXT"),
    ("source_file", "TEXT"),
    ("lineage_file", "TEXT"),
    ("lineage_uri", "TEXT"),
    ("output_file", "TEXT"),
    ("output_uri", "TEXT"),
    ("artifact_file", "TEXT"),
    ("artifact_uri", "TEXT"),
    ("is_last_operator", "INTEGER"),
    ("run_id", "TEXT"),
    ("degraded", "INTEGER"),
    ("created_at", "TEXT"),
    ("updated_at", "TEXT"),
)

#: 关系表 DDL（工程侧可直接执行；``python -m utils.mty_compliance --ddl`` 打印）
RELATION_DDL = "CREATE TABLE IF NOT EXISTS %s (\n  %s\n);" % (
    RELATION_TABLE, ",\n  ".join("%s %s" % item for item in _RELATION_COLUMNS))


def _env_int(env: Dict[str, str], key: str, default: int) -> int:
    try:
        return int(str(env.get(key, default)).strip())
    except (TypeError, ValueError):
        return default


def _brief(value: Any) -> str:
    return " ".join(str(value).split())[:200]


def _has_module(name: str) -> bool:
    try:
        import importlib.util
        return importlib.util.find_spec(name) is not None
    except (ImportError, ValueError):
        return False


def _now() -> str:
    return datetime.now(_TZ).isoformat(timespec="seconds")


def _first_text(*values: Any) -> str:
    for value in values:
        if isinstance(value, str) and value.strip():
            return value.strip()
    return ""


class TransportSettings:
    """归档与入库配置：全部来自环境变量，默认「本地镜像 + 本地 SQLite」。

    对象存储（任一）：``MTY_OSS_ENDPOINT`` / ``MTY_OSS_BUCKET`` /
    ``MTY_OSS_ACCESS_KEY_ID`` / ``MTY_OSS_ACCESS_KEY_SECRET``（S3 兼容可直接用
    ``AWS_ACCESS_KEY_ID`` / ``AWS_SECRET_ACCESS_KEY``）。
    关系库：``MTY_REGISTRY_DSN``（SQLAlchemy DSN，表结构由工程侧预建）或默认 SQLite。
    开关：``MTY_TRANSPORT_ENABLED``、``MTY_ARCHIVE_BACKEND``（auto|oss2|boto3|local|none）、
    ``MTY_REGISTRY_BACKEND``（auto|sqlalchemy|sqlite|none）、``MTY_OSS_PREFIX``、
    ``MTY_OSS_INCLUDE_MEDIA``、``MTY_OSS_MAX_OBJECT_BYTES``、``MTY_SLOT_ID``、
    ``MTY_TRANSPORT_DIR`` / ``MTY_OSS_MIRROR_DIR`` / ``MTY_REGISTRY_DB``。
    """

    def __init__(self, env: Optional[Dict[str, str]] = None) -> None:
        env = dict(os.environ if env is None else env)

        def value(*keys: str) -> str:
            for key in keys:
                raw = env.get(key)
                if isinstance(raw, str) and raw.strip():
                    return raw.strip()
            return ""

        flag = value("MTY_TRANSPORT_ENABLED") or "1"
        self.enabled = flag.lower() not in {"0", "false", "no", "off"}
        self.archive_backend = (value("MTY_ARCHIVE_BACKEND") or "auto").lower()
        self.registry_backend = (value("MTY_REGISTRY_BACKEND") or "auto").lower()
        self.endpoint = value("MTY_OSS_ENDPOINT", "OSS_ENDPOINT")
        self.bucket = value("MTY_OSS_BUCKET", "OSS_BUCKET")
        self.access_key = value("MTY_OSS_ACCESS_KEY_ID", "OSS_ACCESS_KEY_ID", "AWS_ACCESS_KEY_ID")
        self.secret_key = value("MTY_OSS_ACCESS_KEY_SECRET", "OSS_ACCESS_KEY_SECRET",
                                "AWS_SECRET_ACCESS_KEY")
        self.region = value("MTY_OSS_REGION", "AWS_DEFAULT_REGION")
        self.prefix = (value("MTY_OSS_PREFIX") or "crossmodal").strip("/") or "crossmodal"
        self.slot = value("MTY_SLOT_ID")
        self.registry_dsn = value("MTY_REGISTRY_DSN")
        self.max_object_bytes = _env_int(env, "MTY_OSS_MAX_OBJECT_BYTES", 256 * 1024 * 1024)
        self.include_media = value("MTY_OSS_INCLUDE_MEDIA").lower() in {"1", "true", "yes", "on"}

        root = value("MTY_TRANSPORT_DIR")
        if root:
            base = Path(root).expanduser()
        else:
            # 默认落在算子目录下、OUTPUT_DIR 之外：
            # 1）``/removeFile`` 会清理任务目录，归档镜像与关系库必须独立留存；
            # 2）既有契约要求 OUTPUT_DIR 内所有内容都在 uniqueId 目录下。
            # 生产环境建议用 ``MTY_TRANSPORT_DIR`` 指到持久化卷。
            base = Path(__file__).resolve().parents[1] / TRANSPORT_DIRNAME
        self.mirror_root = Path(value("MTY_OSS_MIRROR_DIR") or (base / "oss_mirror"))
        self.registry_db = Path(value("MTY_REGISTRY_DB")
                                or (base / "registry" / "mty_relation.sqlite3"))

    @classmethod
    def from_env(cls) -> "TransportSettings":
        return cls()

    @property
    def remote_ready(self) -> bool:
        return bool(self.endpoint and self.bucket and self.access_key and self.secret_key)

    def summary(self) -> Dict[str, Any]:
        """对外可打印的配置摘要（**不含任何密钥与宿主机绝对路径**）。

        归档报告会随阶段末产物交给工程侧，因此这里只暴露逻辑口径
        （桶名、前缀、槽位、开关），不暴露镜像目录/库文件路径。
        """
        return {
            "enabled": self.enabled,
            "archiveBackend": self.archive_backend,
            "registryBackend": self.registry_backend,
            "remoteReady": self.remote_ready,
            "bucket": self.bucket,
            "endpoint": self.endpoint,
            "prefix": self.prefix,
            "slotId": self.slot,
            "includeMedia": self.include_media,
            "maxObjectBytes": self.max_object_bytes,
            "mirrorConfigured": True,
            "registryDsnConfigured": bool(self.registry_dsn),
        }


def split_unique_id(unique_id: Any) -> Tuple[str, str]:
    """把 uniqueId 拆成「任务id / 文件id」；不合规时按最后一个下划线尽力拆分。"""
    try:
        value = validate_unique_id(unique_id)
    except ValueError:
        value = str(unique_id or "").strip()
    task_id, _, file_id = value.rpartition("_")
    return task_id or "task", file_id or "file"


def _safe_segment(value: Any) -> str:
    text = re.sub(r"[^0-9A-Za-z._\-\u4e00-\u9fff]", "_", str(value or "").strip())
    return text.strip("._") or "unknown"


def object_key(unique_id: Any, stage_id: str, run_id: str, name: str,
               settings: Optional[TransportSettings] = None) -> str:
    """对象键：``<prefix>/<阶段>/<任务id>/<文件id>/<run>/<文件名>``。"""
    settings = settings or TransportSettings.from_env()
    task_id, file_id = split_unique_id(unique_id)
    parts = [settings.prefix, _safe_segment(stage_id or STAGE_ID), _safe_segment(task_id),
             _safe_segment(file_id), _safe_segment(run_id or "run"), _safe_segment(name)]
    return "/".join(part for part in parts if part)


def _put_oss2(path: Path, key: str, settings: TransportSettings) -> str:
    import oss2  # type: ignore

    auth = oss2.Auth(settings.access_key, settings.secret_key)
    bucket = oss2.Bucket(auth, settings.endpoint, settings.bucket)
    bucket.put_object_from_file(key, str(path))
    return "oss://%s/%s" % (settings.bucket, key)


def _put_s3(path: Path, key: str, settings: TransportSettings) -> str:
    import boto3  # type: ignore

    client = boto3.client("s3", endpoint_url=settings.endpoint,
                          aws_access_key_id=settings.access_key,
                          aws_secret_access_key=settings.secret_key,
                          region_name=settings.region or None)
    client.upload_file(str(path), settings.bucket, key)
    return "s3://%s/%s" % (settings.bucket, key)


def _mirror_object(path: Path, key: str, settings: TransportSettings) -> str:
    target = Path(settings.mirror_root) / Path(key)
    target.parent.mkdir(parents=True, exist_ok=True)
    temp = target.with_name(target.name + ".partial")
    shutil.copyfile(path, temp)
    temp.replace(target)
    # 只回逻辑地址，不回宿主机绝对路径（响应体不得暴露部署路径）
    return "local-mirror://%s" % key


def archive_object(local_path: Any, key: str,
                   settings: Optional[TransportSettings] = None) -> Dict[str, Any]:
    """把单个文件归档到对象存储；未配置对象存储时镜像到本地目录。

    返回 ``{"status", "backend", "uri", "key", "bytes", "degraded", "error"}``；
    ``status`` 取 ``uploaded``（远端）/ ``mirrored``（本地镜像）/ ``skipped`` / ``failed``。
    **不抛异常**，失败只记录 ``error``。
    """
    settings = settings or TransportSettings.from_env()
    path = Path(local_path)
    entry: Dict[str, Any] = {"name": path.name, "key": key, "localPath": str(path),
                             "status": "skipped", "backend": settings.archive_backend,
                             "uri": "", "bytes": 0, "degraded": True, "error": ""}
    if settings.archive_backend == "none":
        entry["error"] = "已关闭归档（MTY_ARCHIVE_BACKEND=none）"
        return entry
    if not path.is_file():
        entry["error"] = "本地文件不存在"
        return entry
    size = path.stat().st_size
    entry["bytes"] = size
    if size > settings.max_object_bytes:
        entry["backend"] = "size_limit"
        entry["error"] = ("对象 %d 字节超过 MTY_OSS_MAX_OBJECT_BYTES=%d，按规范改为对象引用/分片"
                          % (size, settings.max_object_bytes))
        return entry

    backend = (settings.archive_backend or "auto").lower()
    candidates = {"oss2": ("oss2",), "boto3": ("s3",), "s3": ("s3",),
                  "auto": ("oss2", "s3"), "local": ()}.get(backend, ("oss2", "s3"))
    errors: List[str] = []
    if candidates and not settings.remote_ready:
        errors.append("未配置 MTY_OSS_ENDPOINT/BUCKET/ACCESS_KEY，按本地镜像归档")
    for candidate in candidates:
        if not settings.remote_ready:
            break
        if candidate == "oss2" and not _has_module("oss2"):
            errors.append("未安装 oss2")
            continue
        if candidate == "s3" and not _has_module("boto3"):
            errors.append("未安装 boto3")
            continue
        try:
            uri = _put_oss2(path, key, settings) if candidate == "oss2" else _put_s3(path, key, settings)
            entry.update(status="uploaded", backend=candidate, uri=uri, degraded=False)
            return entry
        except Exception as exc:  # 归档失败不阻断业务
            errors.append("%s 归档失败：%s" % (candidate, _brief(exc)))
    if backend in {"oss2", "boto3", "s3"}:
        entry.update(status="failed", backend=backend, error="；".join(errors))
        return entry
    try:
        uri = _mirror_object(path, key, settings)
        entry.update(status="mirrored", backend="local_mirror", uri=uri, degraded=True,
                     error="；".join(errors))
    except Exception as exc:
        entry.update(status="failed", backend="local_mirror", error=_brief(exc))
    return entry


def _relation_id(row: Dict[str, Any]) -> str:
    return "|".join([_safe_segment(row.get("unique_id")), _safe_segment(row.get("stage_id")),
                     _safe_segment(row.get("operator") or row.get("operator_number")),
                     _safe_segment(row.get("run_id") or "-")])


def _relation_payload(row: Dict[str, Any]) -> Dict[str, Any]:
    payload = {name: row.get(name, "") for name, _ in _RELATION_COLUMNS}
    for name in ("task_id", "file_id", "unique_id", "stage_id", "slot_id", "operator",
                 "operator_number", "operator_version", "direction", "branch", "status",
                 "source_file", "lineage_file", "lineage_uri", "output_file", "output_uri",
                 "artifact_file", "artifact_uri", "run_id"):
        payload[name] = "" if payload[name] is None else str(payload[name])
    payload["relation_id"] = payload.get("relation_id") or _relation_id(payload)
    payload["is_last_operator"] = 1 if payload.get("is_last_operator") else 0
    payload["degraded"] = 1 if payload.get("degraded") else 0
    payload["created_at"] = payload.get("created_at") or _now()
    payload["updated_at"] = _now()
    return payload


def _write_sqlite(payload: Dict[str, Any], settings: TransportSettings) -> Path:
    path = Path(settings.registry_db)
    path.parent.mkdir(parents=True, exist_ok=True)
    columns = [name for name, _ in _RELATION_COLUMNS]
    statement = ("INSERT INTO %s (%s) VALUES (%s) ON CONFLICT(relation_id) DO UPDATE SET %s"
                 % (RELATION_TABLE, ", ".join(columns), ", ".join("?" * len(columns)),
                    ", ".join("%s = excluded.%s" % (name, name)
                              for name in columns if name != "relation_id")))
    # 注意：sqlite3 连接的 with 只管事务提交，不负责 close；不显式关闭会锁住
    # WAL 附属文件，Windows 上会卡死 TemporaryDirectory 清理（WinError 32）。
    connection = sqlite3.connect(str(path), timeout=15)
    try:
        with connection:
            connection.execute("PRAGMA journal_mode=WAL")
            connection.execute(RELATION_DDL)
            connection.execute(statement, [payload[name] for name in columns])
    finally:
        connection.close()
    return path


def _write_dsn(payload: Dict[str, Any], settings: TransportSettings) -> None:
    from sqlalchemy import create_engine, text  # type: ignore

    columns = [name for name, _ in _RELATION_COLUMNS]
    engine = create_engine(settings.registry_dsn, pool_pre_ping=True)
    insert = "INSERT INTO %s (%s) VALUES (%s)" % (
        RELATION_TABLE, ", ".join(columns), ", ".join(":%s" % name for name in columns))
    update = "UPDATE %s SET %s WHERE relation_id = :relation_id" % (
        RELATION_TABLE, ", ".join("%s = :%s" % (name, name)
                                  for name in columns if name != "relation_id"))
    with engine.begin() as connection:
        try:
            connection.execute(text(RELATION_DDL))
        except Exception:
            pass  # 表结构由工程侧预建（MySQL 等方言需自行指定长度）
        try:
            connection.execute(text(insert), payload)
        except Exception:
            connection.execute(text(update), payload)


def record_relation(row: Dict[str, Any],
                    settings: Optional[TransportSettings] = None) -> Dict[str, Any]:
    """关系入库：写入「任务/文件/血缘文件/阶段/区域(槽)/去向/输出文件」关系行。

    优先使用 ``MTY_REGISTRY_DSN``（SQLAlchemy）；否则写本地 SQLite。
    **不抛异常**，失败返回 ``status=failed`` 与 ``error``。
    """
    settings = settings or TransportSettings.from_env()
    payload = _relation_payload(row)
    result: Dict[str, Any] = {"status": "skipped", "backend": settings.registry_backend,
                              "relationId": payload["relation_id"], "table": RELATION_TABLE,
                              "error": ""}
    if settings.registry_backend == "none":
        result["error"] = "已关闭入库（MTY_REGISTRY_BACKEND=none）"
        return result
    errors: List[str] = []
    if settings.registry_dsn and settings.registry_backend in {"auto", "sqlalchemy", "dsn"}:
        try:
            _write_dsn(payload, settings)
            result.update(status="inserted", backend="dsn")
            return result
        except Exception as exc:
            errors.append("DSN 入库失败：%s" % _brief(exc))
            if settings.registry_backend in {"sqlalchemy", "dsn"}:
                result.update(status="failed", backend="dsn", error="；".join(errors))
                return result
    if settings.registry_backend in {"auto", "sqlalchemy", "sqlite"}:
        try:
            path = _write_sqlite(payload, settings)
            result.update(status="inserted", backend="sqlite", database=str(path),
                          error="；".join(errors))
            return result
        except Exception as exc:
            errors.append("SQLite 入库失败：%s" % _brief(exc))
    result.update(status="failed", error="；".join(errors))
    return result


def list_relations(unique_id: Optional[str] = None, limit: int = 50,
                   settings: Optional[TransportSettings] = None) -> List[Dict[str, Any]]:
    """读取本地 SQLite 关系行（验收/排障用；DSN 模式请直接在库里查）。"""
    settings = settings or TransportSettings.from_env()
    path = Path(settings.registry_db)
    if not path.is_file():
        return []
    columns = [name for name, _ in _RELATION_COLUMNS]
    statement = "SELECT %s FROM %s" % (", ".join(columns), RELATION_TABLE)
    params: List[Any] = []
    if unique_id:
        statement += " WHERE unique_id = ?"
        params.append(unique_id)
    statement += " ORDER BY updated_at DESC LIMIT ?"
    params.append(int(limit))
    connection = sqlite3.connect(str(path), timeout=15)
    try:
        connection.row_factory = sqlite3.Row
        rows = connection.execute(statement, params).fetchall()
    finally:
        connection.close()
    return [dict(row) for row in rows]


def _resolve_output(run_dir: Path, entrypoint: Optional[str]) -> Optional[Path]:
    """定位本算子输出主文件（用于上传「输出文件」到对象存储）。"""
    if isinstance(entrypoint, str) and entrypoint:
        candidate = run_dir / entrypoint
        if candidate.is_file():
            return candidate
    skipped = {STAGE_LINEAGE_NAME, STAGE_ARTIFACT_NAME, TRANSPORT_REPORT_NAME}
    for path in sorted(run_dir.glob("*")):
        if path.is_file() and path.suffix.lower() in _TEXT_SUFFIXES and path.name not in skipped:
            return path
    return None


def _archive_status(entries: Sequence[Dict[str, Any]]) -> str:
    if not entries:
        return "skipped"
    statuses = {(entry.get("status") or "").lower() for entry in entries}
    if statuses == {"uploaded"}:
        return "uploaded"
    if "uploaded" in statuses or "mirrored" in statuses:
        return "degraded"
    return "failed"


def _media_members(run_dir: Path) -> List[Path]:
    return [path for path in sorted(run_dir.rglob("*"))
            if path.is_file() and path.suffix.lower() in _MEDIA_SUFFIXES]


def run_transport(run_dir: Any, records: Sequence[Dict[str, Any]], result: Dict[str, Any],
                  request_is_last: Optional[bool] = None,
                  entrypoint: Optional[str] = None,
                  settings: Optional[TransportSettings] = None) -> Dict[str, Any]:
    """阶段收尾的归档 + 入库：血缘文本 / 输出文件 / 阶段产物 → 对象存储，并写关系表。

    产出 ``transport_report.json`` 落在 run 目录，随阶段末 ``crossmodal-files-v1``
    一并交付给工程侧。**任何异常都被吞掉并记入报告**，不影响业务响应。
    """
    report: Dict[str, Any] = {
        "format": "crossmodal-transport-report-v1",
        "status": "skipped", "stageId": STAGE_ID, "operatorNumber": OPERATOR_NUMBER,
        "isLastOperator": bool(IS_STAGE_LAST_OPERATOR),
        "requestIsLastOperator": None if request_is_last is None else bool(request_is_last),
        "generatedAt": _now(), "archive": [], "error": "",
    }
    try:
        settings = settings or TransportSettings.from_env()
        run_dir = Path(run_dir)
        report["settings"] = settings.summary()
        if not settings.enabled:
            report["status"] = "disabled"
            report["error"] = "MTY_TRANSPORT_ENABLED=0"
            return report

        nodes = [node for node in records if isinstance(node, dict)]
        last = nodes[-1] if nodes else {}
        unique_id = _first_text(last.get("uniqueId"), result.get("uniqueId"),
                                result.get("unique_id"), run_dir.parent.name)
        task_id, file_id = split_unique_id(unique_id)
        run_id = _first_text(last.get("runId"), result.get("runId"), run_dir.name)
        source_file = _first_text(last.get("inputFile"), result.get("inputFile"),
                                  result.get("input_file"))

        targets: List[Path] = []
        lineage = run_dir / STAGE_LINEAGE_NAME
        if lineage.is_file():
            targets.append(lineage)
        artifact = run_dir / STAGE_ARTIFACT_NAME
        if artifact.is_file():
            targets.append(artifact)
        for extra in sorted((run_dir / "lineage").glob("lineage_*.jsonl")):
            targets.append(extra)
        output = _resolve_output(run_dir, entrypoint)
        if output is not None:
            targets.append(output)
        if settings.include_media:
            targets.extend(path for path in _media_members(run_dir) if path not in targets)

        entries = [archive_object(path, object_key(unique_id, STAGE_ID, run_id,
                                                   path.relative_to(run_dir).as_posix(), settings),
                                 settings)
                   for path in targets]
        report["archive"] = entries
        report["archiveStatus"] = _archive_status(entries)

        def pick(name: str) -> Dict[str, Any]:
            for entry in entries:
                if entry.get("name") == name:
                    return entry
            return {}

        lineage_entry = pick(STAGE_LINEAGE_NAME)
        artifact_entry = pick(STAGE_ARTIFACT_NAME)
        output_entry = pick(output.name) if output is not None else {}
        status_text = str(result.get("status") or "success").lower()
        row: Dict[str, Any] = {
            "task_id": task_id, "file_id": file_id, "unique_id": unique_id,
            "stage_id": STAGE_ID,
            "slot_id": settings.slot or ("slot-%s" % OPERATOR_NUMBER),
            "operator": _first_text(last.get("operator"), result.get("operator"),
                                    result.get("operatorId")),
            "operator_number": OPERATOR_NUMBER,
            "operator_version": _first_text(last.get("operator_version"),
                                            last.get("operatorVersion"),
                                            result.get("version")),
            "direction": "stage_end->next_stage" if IS_STAGE_LAST_OPERATOR else "next_operator",
            "branch": "normal" if status_text in {"success", "ok", "degraded", "partial"}
                      else "exception",
            "status": status_text or "success",
            "source_file": source_file,
            "lineage_file": str(lineage_entry.get("localPath", "")),
            "lineage_uri": str(lineage_entry.get("uri", "")),
            "output_file": str(output_entry.get("localPath", "")),
            "output_uri": str(output_entry.get("uri", "")),
            "artifact_file": str(artifact_entry.get("localPath", "")),
            "artifact_uri": str(artifact_entry.get("uri", "")),
            "is_last_operator": bool(IS_STAGE_LAST_OPERATOR),
            "run_id": run_id,
            "degraded": any(entry.get("degraded") for entry in entries),
        }
        registry = record_relation(row, settings)
        report.update({"uniqueId": unique_id, "taskId": task_id, "fileId": file_id,
                       "runId": run_id, "relation": row,
                       "relationId": registry.get("relationId", ""), "registry": registry})
        report["status"] = "ok" if registry.get("status") == "inserted" else "degraded"
        report["errors"] = sorted({entry["error"] for entry in entries if entry.get("error")}
                                  | {registry.get("error", "")} - {""})
        target = run_dir / TRANSPORT_REPORT_NAME
        target.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
        report["reportPath"] = str(target)
    except Exception as exc:  # 归档/入库绝不阻断业务响应
        report["status"] = "failed"
        report["error"] = _brief(exc)
    return report


# --------------------------------------------------------------------------- #
# 6. 离线自检
# --------------------------------------------------------------------------- #

def selftest() -> Dict[str, Any]:
    """离线自检：uniqueId 强校验 → 血缘补字段 → 拼接文本 → 阶段产物 → 归档 → 入库。

    运行：``python -m utils.mty_compliance --selftest``（返回 0 即全部通过）。
    自检强制走「本地镜像 + 本地 SQLite」，写入临时目录，不接触生产配置。
    """
    checks: List[Dict[str, Any]] = []

    def check(name: str, ok: bool, detail: str = "") -> bool:
        checks.append({"name": name, "ok": bool(ok), "detail": _brief(detail)})
        return bool(ok)

    try:
        validate_unique_id("task001_file01")
        check("uniqueId 合规值通过", True, "task001_file01")
    except ValueError as exc:
        check("uniqueId 合规值通过", False, str(exc))
    bad_values = ("task001", "", "a b_c", "t1", "_file01", "task001_", "task/001_file01")
    rejected = []
    for bad in bad_values:
        try:
            validate_unique_id(bad)
        except ValueError:
            rejected.append(bad)
    check("uniqueId 不合规值被拒", len(rejected) == len(bad_values),
          "已拒绝 %d/%d：%s" % (len(rejected), len(bad_values), rejected))

    saved = {key: os.environ.get(key) for key in
             ("MTY_TRANSPORT_ENABLED", "MTY_ARCHIVE_BACKEND", "MTY_REGISTRY_BACKEND",
              "MTY_TRANSPORT_DIR", "MTY_OSS_ENDPOINT", "MTY_OSS_BUCKET", "MTY_REGISTRY_DSN")}
    with tempfile.TemporaryDirectory(prefix="mty_selftest_") as temp:
        try:
            os.environ.update({"MTY_TRANSPORT_ENABLED": "1", "MTY_ARCHIVE_BACKEND": "local",
                               "MTY_REGISTRY_BACKEND": "sqlite", "MTY_TRANSPORT_DIR": temp})
            for key in ("MTY_OSS_ENDPOINT", "MTY_OSS_BUCKET", "MTY_REGISTRY_DSN"):
                os.environ.pop(key, None)

            run_dir = Path(temp) / "task001_file01" / "operator" / "run001"
            run_dir.mkdir(parents=True, exist_ok=True)
            (run_dir / "out.jsonl").write_text('{"sample_id":"s1"}\n', encoding="utf-8")
            nodes = [
                {"operator": "upstream_op", "uniqueId": "task001_file01", "runId": "run000",
                 "status": "success", "stageId": "M1", "inputFile": "input/entry.jsonl"},
                {"operator": "current_op", "uniqueId": "task001_file01", "runId": "run001",
                 "status": "success", "stageId": STAGE_ID, "inputFile": "input/entry.jsonl"},
            ]
            result = {"status": "success", "runDir": str(run_dir), "entrypoint": "out.jsonl",
                      "lineage": nodes[1], "stageLineage": nodes, "artifacts": []}
            prepare_stage_result(result, True, run_dir=run_dir, manifest=None)

            check("血缘补 isLastOperator",
                  nodes[-1].get("isLastOperator") is bool(IS_STAGE_LAST_OPERATOR)
                  and all("isLastOperator" in node for node in nodes),
                  "current=%s" % nodes[-1].get("isLastOperator"))
            joined = run_dir / STAGE_LINEAGE_NAME
            lines = joined.read_text(encoding="utf-8").splitlines() if joined.is_file() else []
            check("阶段血缘拼接文本（一条记录一行）", len(lines) == 2
                  and all(json.loads(line).get("operator") for line in lines),
                  "%s 行数=%d" % (STAGE_LINEAGE_NAME, len(lines)))
            artifact = run_dir / STAGE_ARTIFACT_NAME
            check("阶段产物 zip（阶段末算子）",
                  artifact.is_file() == bool(IS_STAGE_LAST_OPERATOR),
                  "存在=%s" % artifact.is_file())
            report_path = run_dir / TRANSPORT_REPORT_NAME
            report = json.loads(report_path.read_text(encoding="utf-8")) if report_path.is_file() else {}
            check("归档报告落盘", report.get("format") == "crossmodal-transport-report-v1",
                  str(report_path.name))
            entries = report.get("archive") or []
            check("血缘文本与输出文件已归档（本地镜像）",
                  {"stage_lineage.jsonl", "out.jsonl"} <= {item.get("name") for item in entries}
                  and all(item.get("status") in {"uploaded", "mirrored"} for item in entries),
                  ", ".join("%s:%s" % (item.get("name"), item.get("status")) for item in entries))
            rows = list_relations("task001_file01")
            check("关系入库（任务/文件/阶段/区域/去向/输出）",
                  bool(rows) and rows[0].get("task_id") == "task001"
                  and rows[0].get("file_id") == "file01" and rows[0].get("stage_id") == STAGE_ID
                  and bool(rows[0].get("direction")) and bool(rows[0].get("slot_id")),
                  "relationId=%s" % (rows[0].get("relation_id") if rows else ""))
            check("入库行含对象地址（逻辑地址，不暴露宿主机路径）",
                  bool(rows) and str(rows[0].get("lineage_uri", "")).startswith("local-mirror://")
                  and str(Path(temp).as_posix()) not in str(rows[0].get("lineage_uri", "")),
                  str(rows[0].get("lineage_uri", "")) if rows else "")
        finally:
            for key, value in saved.items():
                if value is None:
                    os.environ.pop(key, None)
                else:
                    os.environ[key] = value

    passed = sum(1 for item in checks if item["ok"])
    return {"ok": passed == len(checks), "passed": passed, "total": len(checks),
            "stageId": STAGE_ID, "operatorNumber": OPERATOR_NUMBER,
            "isLastOperator": bool(IS_STAGE_LAST_OPERATOR), "checks": checks}


def _main(argv: Optional[Sequence[str]] = None) -> int:
    import sys

    args = list(sys.argv[1:] if argv is None else argv)
    if "--selftest" in args:
        payload = selftest()
        print(json.dumps(payload, ensure_ascii=False, indent=2))
        return 0 if payload.get("ok") else 1
    if "--ddl" in args:
        print(RELATION_DDL)
        return 0
    if "--relations" in args:
        print(json.dumps(list_relations(limit=20), ensure_ascii=False, indent=2))
        return 0
    print(__doc__)
    return 0


if __name__ == "__main__":
    raise SystemExit(_main())

