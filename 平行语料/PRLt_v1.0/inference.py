"""Unified demo API for the parallel-corpus operator bundle.

Each operator still owns its processor and API implementation. This root service
provides the template-level upload/predict/remove contract and dispatches a
selected operator to its local processor in an isolated Python subprocess.

无血缘改造（2026-10-19）：本服务不再生成、读取或归档血缘文件；/predict 成功时
保留 outputFile / rejectFile / stageZip / storageUri，并返回统计、样本和拒因。/predictChain 额外
返回 Java 可读的 auditFile / auditSummary（逐输入/业务单元判定，不是血缘文件）。
"""

from __future__ import annotations

import json
import os
import re
import shutil
import sqlite3
import subprocess
import tempfile
import sys
import zipfile
from datetime import datetime, timezone
from pathlib import Path, PureWindowsPath
from typing import Any, Literal, Optional

from fastapi import FastAPI, File, HTTPException, Query, Request, UploadFile
from fastapi.responses import FileResponse
from pydantic import BaseModel, Field, StrictBool

import ingest
from common.predict_payload import (build_predict_fields, extract_sample_limit,
                                    normalize_statistics)


ROOT = Path(__file__).resolve().parent


def _load_local_env() -> None:
    env_file = ROOT / ".env"
    if not env_file.is_file():
        return
    for line in env_file.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        os.environ.setdefault(key.strip(), value.strip().strip('"').strip("'"))


_load_local_env()
POOL_ROOT = Path(os.getenv("OPERATOR_POOL_ROOT", ROOT / "work" / "pool")).resolve()
REGISTRY_DOCUMENT = json.loads((ROOT / "map.json").read_text(encoding="utf-8"))
REGISTRY = REGISTRY_DOCUMENT["operators"]
LEGACY_OPERATOR_ALIASES = dict(REGISTRY_DOCUMENT["legacyOperatorAliases"])


def canonical_operator_id(operator_id: str) -> str:
    """Normalize legacy API02-API24 IDs to canonical 1022-aligned IDs.

    API01 is intentionally not aliased: since V1.6 it is checklist #1 language
    validation. The historical source-copyright operator is no longer registered.
    """
    value = str(operator_id or "").strip()
    return LEGACY_OPERATOR_ALIASES.get(value, value)


def canonical_operator_ids(operator_ids: list[str],
                           scheme: Literal["current", "legacy"] = "current") -> list[str]:
    if scheme == "legacy":
        return [canonical_operator_id(operator_id) for operator_id in operator_ids]
    return [str(operator_id or "").strip() for operator_id in operator_ids]


def canonical_operator_params(
    params: dict[str, Any],
    scheme: Literal["current", "legacy"] = "current",
) -> dict[str, Any]:
    """Canonicalize operatorOverrides only for an explicit legacy request."""
    normalized = dict(params or {})
    if scheme != "legacy":
        return normalized
    overrides = normalized.get("operatorOverrides")
    if isinstance(overrides, dict):
        normalized["operatorOverrides"] = {
            canonical_operator_id(key): value for key, value in overrides.items()
        }
    return normalized


app = FastAPI(title="Parallel Corpus Operator Pool", version="1.0.0")

# 模型根目录解析：优先环境变量 MODEL_ROOT；其次包内 models/（交付机挂载点）；
# 最后回退到开发验证机的共享权重目录（G:\HYQX\算子\model，与其它模态共用）。
_SHARED_MODEL_ROOT = Path(r"G:\HYQX\算子\model")


def _model_root() -> Path:
    env = os.getenv("MODEL_ROOT")
    if env and Path(env).is_dir():
        return Path(env)
    local = ROOT / "models"
    if local.is_dir() and any(local.iterdir()):
        return local
    if _SHARED_MODEL_ROOT.is_dir():
        return _SHARED_MODEL_ROOT
    return local


MODEL_ROOT = _model_root()


def _first_model_dir(*names: str) -> Path:
    """Return the first existing model directory under MODEL_ROOT.

    Handles delivery layout (lowercase names, e.g. ``labse``) and the shared
    development layout (e.g. ``LaBSE``) without hard-coding either one.
    """
    for name in names:
        candidate = MODEL_ROOT / name
        if candidate.is_dir():
            return candidate
    return MODEL_ROOT / names[0]


def _aux_model_path(*relative_paths: str) -> Path:
    """Resolve an adapter/aux artifact, preferring the delivered ``models/`` tree."""
    for relative in relative_paths:
        candidate = ROOT / relative
        if candidate.exists():
            return candidate
    return ROOT / relative_paths[0]


# Local model defaults. Missing 20/22/23 weights deliberately have no default
# service, so those optional operators fail explicitly instead of calling an API.
# Keys use canonical 1022-aligned IDs since V1.6.
MODEL_LOCAL_DEFAULTS = {
    "09": {"model_name": str(_first_model_dir("Qwen3-Embedding-0.6B")), "model_service_url": None},
    "10": {"model_name": "mDeBERTa-v3-base", "model_path": str(_first_model_dir("mdeberta-v3-base")), "model_service_url": None},
    "14": {"model_name": "Qwen/Qwen3-Embedding-0.6B", "model_path": str(_first_model_dir("Qwen3-Embedding-0.6B")), "model_service_url": None},
    "15": {"model_name": "Qwen3.5-2B", "model_path": str(_aux_model_path("models/Qwen3.5-2B/adapter.py", "weights/adapter_v6.py")), "model_service_url": None},
    "11": {"model_name": "Qwen3-Embedding-0.6B-domain-prototype-v1", "model_path": str(_aux_model_path("models/Qwen3-Embedding-0.6B/domain_adapter.py", "weights/domain_adapter_v4.py")), "model_service_url": None},
}


class PredictRequest(BaseModel):
    uniqueId: str
    inputFile: Optional[str] = None
    isLastOperator: Optional[StrictBool] = None
    operatorId: str = "01"
    operatorIdScheme: Literal["current", "legacy"] = "current"
    params: dict[str, Any] = Field(default_factory=dict)
    config: dict[str, Any] = Field(default_factory=dict)


class ChainPredictRequest(BaseModel):
    """Streaming chain request; the legacy single-operator contract is unchanged."""
    uniqueId: str
    inputFile: Optional[str] = None
    operatorIds: Optional[list[str]] = None
    operatorIdScheme: Literal["current", "legacy"] = "current"
    params: dict[str, Any] = Field(default_factory=dict)


def _identity(unique_id: str) -> tuple[str, str]:
    """Return task/file identities encoded by the public uniqueId."""
    if not isinstance(unique_id, str) or not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._-]*_[A-Za-z0-9][A-Za-z0-9._-]*", unique_id):
        raise ValueError("uniqueId 格式非法，必须为 taskId_fileId")
    task_id, file_id = unique_id.split("_", 1)
    return task_id, file_id


def _workspace(unique_id: str) -> Path:
    _identity(unique_id)
    path = (POOL_ROOT / unique_id).resolve()
    path.relative_to(POOL_ROOT)
    return path


def _validate_input_format(path: Path, operator_id: str) -> None:
    """Reject JSON arrays and non-object lines; operator 08 accepts a JSON plan object."""
    text = path.read_text(encoding="utf-8-sig")
    if operator_id == "08":
        value = json.loads(text)
        if not isinstance(value, dict):
            raise ValueError("算子09计划必须是 JSON 对象")
        return
    seen = False
    for line_no, line in enumerate(text.splitlines(), 1):
        if not line.strip():
            continue
        seen = True
        value = json.loads(line)
        if not isinstance(value, dict):
            raise ValueError(f"输入必须为 JSONL 对象，每行一条记录（第 {line_no} 行）")
    if not seen:
        raise ValueError("输入 JSONL 不能为空")


def _resolve_chain_input_mode(input_file: Path, params: dict[str, Any]) -> str:
    """选择 /predictChain 入口模式（pairs / txt / documents）。

    优先级：``params.inputMode``（auto|pairs|txt|documents，默认 auto）。
    auto 时按扩展名与 ``planDirectory`` 判断：
    - .txt→txt；
    - .jsonl 且提供 planDirectory→documents（保留原文档对齐路径），否则→pairs；
    - .tsv/.csv/.tmx/.json→pairs（同文件双语准入格式）；
    - 其他格式拒绝并提示由上游转为同文件双语或 txt。
    """
    raw_mode = params.get("inputMode")
    mode = str(raw_mode).strip().lower() if raw_mode is not None else ""
    if not mode:
        mode = "auto"
    if mode not in CHAIN_INPUT_MODES:
        raise ValueError("params.inputMode 仅支持 auto / pairs / txt / documents（默认 auto）")
    suffix = input_file.suffix.lower()
    has_plan = bool(params.get("planDirectory"))
    if mode == "auto":
        if suffix in _CHAIN_TXT_SUFFIXES:
            return "txt"
        if suffix in _CHAIN_DOCUMENTS_SUFFIXES:
            return "documents" if has_plan else "pairs"
        if suffix in _CHAIN_PAIRS_SUFFIXES:
            return "pairs"
        raise ValueError(ingest.unsupported_format_message(suffix))
    if mode == "txt":
        if suffix not in _CHAIN_TXT_SUFFIXES:
            raise ValueError(f"params.inputMode=txt 要求 .txt 双语文本文件，当前输入：{input_file.name}")
        return "txt"
    if mode == "pairs":
        if suffix not in _CHAIN_PAIRS_SUFFIXES:
            raise ValueError(
                "params.inputMode=pairs 要求同文件双语格式（.jsonl / .tsv / .csv / .tmx），"
                f"当前输入：{input_file.name}")
        return "pairs"
    # documents
    if suffix not in _CHAIN_DOCUMENTS_SUFFIXES:
        raise ValueError(
            f"params.inputMode=documents 要求文档清单 JSONL（.jsonl），当前输入：{input_file.name}")
    if not has_plan:
        raise ValueError("documents 模式必须提供 params.planDirectory（文档级对齐计划目录）")
    return "documents"


def _validate_pairs_chain_operators(operator_ids: list[str]) -> None:
    """pairs/txt 链不执行已删除的旧 00、文档级对齐 08 和系统功能 18/19。"""
    for operator_id in operator_ids:
        reason = _PAIRS_CHAIN_REJECTED_OPERATORS.get(str(operator_id))
        if reason:
            raise ValueError(
                f"匹配句对/txt 输入不执行 #{operator_id}：{reason}；"
                f"请从 operatorIds 移除 {operator_id}（默认链已按数据流配置）。")


# [无血缘改造 2026-10-19] 原 _read_lineage_file() 血缘文件读取已停用并删除；
# 原实现见改造前备份 PRLt_code_backup_*.zip。


# [无血缘改造 2026-10-19] 原 _legacy_stage() / _annotate_lineage()（血缘行规范化与
# oss_supplement 补行）已停用并删除；阶段归档改由 /predict 直接返回 stageZip/storageUri。


def _invalidate_stage(task: Path, operator_id: str, unique_id: str) -> None:
    filename = f"stage_{operator_id}_{unique_id}.zip"
    store = Path(os.getenv("OBJECT_STORE_DIR", POOL_ROOT / "object_store"))
    for root, parent in ((task, task / "artifacts"), (store, store / task.name)):
        parent.resolve().relative_to(root.resolve())
        (parent / filename).unlink(missing_ok=True)


def _archive_stage(task: Path, operator_id: str, unique_id: str,
                   output_file: Path | None) -> Path | None:
    if output_file is None or not output_file.exists():
        return None
    stage_dir = output_file.parent
    artifact_dir = task / "artifacts"
    artifact_dir.mkdir(parents=True, exist_ok=True)
    archive = artifact_dir / f"stage_{operator_id}_{unique_id}.zip"
    with tempfile.NamedTemporaryFile(dir=artifact_dir, delete=False) as tmp:
        temporary = Path(tmp.name)
    try:
        with zipfile.ZipFile(temporary, "w", compression=zipfile.ZIP_DEFLATED) as bundle:
            for child in stage_dir.rglob("*"):
                if child.is_file():
                    bundle.write(child, child.relative_to(stage_dir.parent))
        temporary.replace(archive)
    finally:
        temporary.unlink(missing_ok=True)
    return archive


def _record_transfer(unique_id: str, operator_id: str, is_last: bool,
                     result: dict[str, Any], stage_zip: Path | None) -> None:
    task_id, file_id = _identity(unique_id)
    POOL_ROOT.mkdir(parents=True, exist_ok=True)
    db = POOL_ROOT / "transfer_catalog.sqlite3"
    with sqlite3.connect(db) as conn:
        conn.execute("""CREATE TABLE IF NOT EXISTS transfer_records (
            id INTEGER PRIMARY KEY AUTOINCREMENT, unique_id TEXT NOT NULL,
            task_id TEXT NOT NULL, file_id TEXT NOT NULL, operator_id TEXT NOT NULL,
            status TEXT NOT NULL, input_file TEXT, output_file TEXT,
            lineage_file TEXT, stage_zip TEXT, is_last_operator INTEGER NOT NULL,
            created_at TEXT NOT NULL)""")
        conn.execute("""INSERT INTO transfer_records
            (unique_id, task_id, file_id, operator_id, status, input_file,
             output_file, lineage_file, stage_zip, is_last_operator, created_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (unique_id, task_id, file_id, operator_id, result.get("status", "unknown"),
             result.get("inputFile"), result.get("outputFile"),
             None,  # [无血缘改造 2026-10-19] 不再登记血缘文件
             str(stage_zip) if stage_zip else None, int(is_last),
             datetime.now(timezone.utc).isoformat(timespec="seconds")))


def _publish_local_object(task: Path, stage_zip: Path | None) -> str | None:
    """Internal-network object-store mode; replace OBJECT_STORE_DIR for deployment storage."""
    if stage_zip is None or not stage_zip.is_file():
        return None
    root = Path(os.getenv("OBJECT_STORE_DIR", POOL_ROOT / "object_store"))
    dest = root / task.name / stage_zip.name
    dest.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.NamedTemporaryFile(dir=dest.parent, delete=False) as tmp:
        temporary = Path(tmp.name)
    try:
        shutil.copy2(stage_zip, temporary)
        temporary.replace(dest)
    finally:
        temporary.unlink(missing_ok=True)
    return dest.resolve().as_uri()


def _resolve_input(task: Path, value: str | None) -> Path:
    if value:
        candidate = Path(value)
        if not candidate.is_absolute():
            candidate = task / value
    else:
        files = sorted((task / "input").glob("*"))
        if len(files) != 1:
            raise FileNotFoundError("阶段首算子需要唯一上传文件")
        candidate = files[0]
    candidate = candidate.resolve()
    candidate.relative_to(task)
    if not candidate.is_file():
        raise FileNotFoundError("inputFile 不存在")
    return candidate


def _run_processor(op: str, input_file: Path | None, uid: str, output_root: Path,
                   params: dict[str, Any]) -> dict[str, Any]:
    spec = REGISTRY.get(op)
    if spec is None:
        raise ValueError(f"未注册的 operatorId: {op}")
    module_dir = ROOT / spec["package"]
    processor = module_dir / "processor.py"
    if not processor.is_file():
        raise FileNotFoundError(f"算子处理器不存在: {processor}")
    params = dict(params)
    for key, value in MODEL_LOCAL_DEFAULTS.get(op, {}).items():
        params.setdefault(key, value)
    if op in {"10", "14"}:
        params.setdefault("model_backend", "model")
        params.setdefault("fallback_policy", "fail")
    if op in {"11", "15"}:
        # 模型权重随 models/ 交付；缺件时按 fallback_policy=fail 结构化失败，
        # 不静默降级（与 weights/README.md 的交付约定一致）。
        params.setdefault("backend", "model")
        params.setdefault("fallback_policy", "fail")
    code = r'''
import importlib.util, json, pathlib, sys
module_dir = pathlib.Path(sys.argv[1])
op = sys.argv[2]
input_file = sys.argv[3] if sys.argv[3] != "null" else None
uid = sys.argv[4]
out_root = sys.argv[5]
params = json.loads(sys.argv[6])
sys.path.insert(0, str(module_dir))
spec = importlib.util.spec_from_file_location("operator_processor", module_dir / "processor.py")
mod = importlib.util.module_from_spec(spec)
spec.loader.exec_module(mod)
if op == "08":
    plan_dir = params.pop("planDirectory", None)
    if plan_dir:
        for _name in ("alignment_plan.json", "10_align_plan.json"):
            _cand = pathlib.Path(plan_dir) / _name
            if _cand.is_file():
                params["plan_file"] = str(_cand)
                params.setdefault("extra_input_root", str(pathlib.Path(plan_dir).parent))
                break
    plan = params.pop("plan_file", input_file)
    extra_root = params.pop("extra_input_root", None)
    result = mod.run(uid, plan_file=plan, output_root=out_root,
                     overrides=params or None, extra_input_root=extra_root)
elif op == "19":
    result = mod.run(input_file, uid, out_root, overrides=params or None)
else:
    result = mod.run(input_file, uid, out_root, overrides=params or None)
print(json.dumps(result, ensure_ascii=False, default=str))
'''
    args = [sys.executable, "-c", code, str(module_dir), op,
            str(input_file) if input_file else "null", uid, str(output_root),
            json.dumps(dict(params), ensure_ascii=False)]
    env = dict(os.environ)
    env["PYTHONIOENCODING"] = "utf-8"
    proc = subprocess.run(args, cwd=str(module_dir), capture_output=True,
                          text=True, encoding="utf-8", errors="replace", env=env)
    if proc.returncode:
        raise RuntimeError(proc.stderr[-4000:] or proc.stdout[-4000:])
    lines = [line for line in proc.stdout.splitlines() if line.strip()]
    if not lines:
        raise RuntimeError("处理器没有返回 JSON 结果")
    result = json.loads(lines[-1])
    # [无血缘改造 2026-10-19] 不再把算子 lineage.json 重命名为 lineage.jsonl。
    return result


def _run_chain_processor(op: str, input_file: Path | None, uid: str,
                         output_root: Path, params: dict[str, Any]) -> dict[str, Any]:
    """Keep invocation errors attached to their chain stage, not just the endpoint."""
    try:
        result = _run_processor(op, input_file, uid, output_root, params)
        if not isinstance(result, dict):
            raise ValueError("处理器返回值必须为 JSON 对象")
        return result
    except Exception as exc:
        return {"status": "failed", "outputFile": None, "rejectFile": None,
                "statistics": {},
                "message": f"{type(exc).__name__}: {exc}".rstrip(": ")}


# 数据流默认链（2026-10-24 V1.6 编号迁移后）：API编号=1022清单编号。
# 历史来源准入旧01已删除；当前API01是清单#1语言与句对结构校验。
# - pairs/txt：01→07 清洗 → 09 句子级对齐 → 12→15 → 11 领域 → 16 实体 → 17 打包；
#   已匹配句对无需 08 文档级对齐。
# - documents：08→09→12→15→11→16→17，保留原文档对齐路径。
# 10 子句级对齐为可选默认关闭算子（map.json default_enabled=false），按需显式传入。
STREAM_DEFAULT_CHAIN = ["01", "02", "03", "04", "05", "06", "07", "09",
                       "12", "13", "14", "15", "11", "16", "17"]
STREAM_DOCUMENTS_DEFAULT_CHAIN = ["08", "09", "12", "13", "14", "15", "11", "16", "17"]
# executionMode / streamMode 是链级控制参数，不下传给算子 processor。
STREAM_CONTROL_KEYS = {"planDirectory", "operatorOverrides", "streamMode", "maxRecords",
                       "executionMode", "inputMode"}
# 入口模式：auto 按扩展名/planDirectory 选择；pairs/txt 走入口预处理，documents 走 09 文档计划。
CHAIN_INPUT_MODES = {"auto", "pairs", "txt", "documents"}
# pairs = 同文件双语准入格式（JSONL/TSV/CSV/TMX）；txt = 上游由分文件双语转换来的双语文本。
_CHAIN_PAIRS_SUFFIXES = {".jsonl", ".json", ".tsv", ".csv", ".tmx"}
_CHAIN_TXT_SUFFIXES = {".txt"}
# documents 模式的输入是文档清单 JSONL，另需 params.planDirectory。
_CHAIN_DOCUMENTS_SUFFIXES = {".jsonl"}
# 已移出/dataflow 不适用算子：pairs/txt 链显式传入时给出业务可读原因。
# 编号已按1022清单迁移：08=文档级对齐，18/19=系统功能。
_PAIRS_CHAIN_REJECTED_OPERATORS = {
    "00": "旧多格式解析算子已删除（业务输入只有同文件双语/双语 txt）",
    "08": "文档级对齐对已匹配句对无意义（匹配句对直接进入 #9 句子级对齐）",
    "18": "数据质量报告是清单#18系统功能，不进入业务链",
    "19": "人工抽检复核是清单#19系统功能，不进入业务链",
}
# stage_batched（默认）：每个算子对当前全部记录只调用一次 processor（模型一阶段一加载）；
# record_immediate（旧模式）：逐条记录交接，保留用于回滚/对照。
STREAM_EXECUTION_MODES = {"stage_batched", "record_immediate"}


def _read_jsonl_rows(path: Path) -> list[dict[str, Any]]:
    path = Path(path) if path else Path("")
    if not path.is_file():
        return []
    rows: list[dict[str, Any]] = []
    for line in path.read_text(encoding="utf-8-sig").splitlines():
        if not line.strip():
            continue
        value = json.loads(line)
        if isinstance(value, dict):
            rows.append(value)
    return rows


def _document_id_of(record: dict[str, Any]) -> str | None:
    """Best-effort document id extraction from top-level, metadata, or paragraph id."""
    if not isinstance(record, dict):
        return None
    value = record.get("document_id")
    if isinstance(value, str) and value.strip():
        return value.strip()
    metadata = record.get("metadata") if isinstance(record.get("metadata"), dict) else {}
    value = metadata.get("document_id")
    if isinstance(value, str) and value.strip():
        return value.strip()
    paragraph_metadata = metadata.get("paragraph_metadata")
    if isinstance(paragraph_metadata, dict):
        value = paragraph_metadata.get("document_id")
        if isinstance(value, str) and value.strip():
            return value.strip()
    for key in ("paragraph_id", "id"):
        value = record.get(key)
        if isinstance(value, str) and "::" in value:
            candidate = value.split("::", 1)[0].strip()
            if candidate:
                return candidate
    return None


def _carry_document_id(rows: list[dict[str, Any]],
                       input_records: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Keep documents-mode document_id on every downstream record.

    Processors such as #10 rebuild metadata and only retain the document id in
    ``metadata.paragraph_metadata``.  The chain must preserve a first-class
    document_id for evidence and final delivery rows; map output rows back to
    the input batch by id/paragraph_id when the processor did not copy it.
    """
    by_id: dict[str, str] = {}
    for item in input_records:
        if not isinstance(item, dict):
            continue
        document_id = _document_id_of(item)
        if not document_id:
            continue
        for key in ("id", "paragraph_id"):
            value = item.get(key)
            if isinstance(value, str) and value and value not in by_id:
                by_id[value] = document_id
    for row in rows:
        if not isinstance(row, dict):
            continue
        document_id = _document_id_of(row)
        if not document_id:
            for key in ("id", "paragraph_id"):
                value = row.get(key)
                if isinstance(value, str) and value in by_id:
                    document_id = by_id[value]
                    break
        if not document_id:
            continue
        row["document_id"] = document_id
        metadata = row.get("metadata") if isinstance(row.get("metadata"), dict) else {}
        metadata_document_id = metadata.get("document_id")
        if not (isinstance(metadata_document_id, str) and metadata_document_id.strip()):
            metadata = dict(metadata)
            metadata["document_id"] = document_id
            row["metadata"] = metadata
    return rows


def _write_jsonl_rows(path: Path, rows: list[dict[str, Any]]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8", newline="\n") as stream:
        for row in rows:
            stream.write(json.dumps(row, ensure_ascii=False, separators=(",", ":")) + "\n")


_AUDIT_STATUS_KEYS = (
    "quality_grade",
    "quality_score",
    "quality_status",
    "alignment_confidence",
    "semantic_score",
    "fluency_status",
    "fluency_score",
    "domain_status",
    "domain",
    "domain_confidence",
    "entity_consistency_status",
    "review_status",
    "delivery_status",
)


def _audit_record_id(record: dict[str, Any]) -> str | None:
    """Return the business id carried by a delivered/rejected/stage record."""
    if not isinstance(record, dict):
        return None
    for key in ("id", "pair_id", "source_ref"):
        value = record.get(key)
        if isinstance(value, str) and value.strip():
            return value.strip()
    metadata = record.get("metadata")
    if isinstance(metadata, dict):
        for key in ("original_id", "source_ref", "parent_pair_id"):
            value = metadata.get(key)
            if isinstance(value, str) and value.strip():
                return value.strip()
    return None


def _audit_metadata(record: dict[str, Any]) -> dict[str, Any]:
    metadata = record.get("metadata") if isinstance(record, dict) else None
    return metadata if isinstance(metadata, dict) else {}


_PARENT_KEYS = ("parent_input_id", "parent_pair_id", "paragraph_id", "original_id",
                "pair_id", "source_ref", "document_id")


def _audit_refs(record):
    metadata = _audit_metadata(record)
    for key in _PARENT_KEYS:
        for source in (record, metadata):
            value = source.get(key)
            if isinstance(value, str) and value:
                yield value


def _audit_status_values(record: dict[str, Any]) -> dict[str, Any]:
    """Keep only stable status/score fields for the Java-facing audit row."""
    values: dict[str, Any] = {}
    if not isinstance(record, dict):
        return values
    for key in _AUDIT_STATUS_KEYS:
        if key in record and record.get(key) is not None:
            values[key] = record.get(key)
    for key, value in (record.get("status_values") or {}).items():
        if value is not None:
            values.setdefault(key, value)
    return values


def _build_chain_audit(task: Path, unique_id: str, input_file: Path,
                       operator_ids: list[str],
                       stages: dict[str, dict[str, Any]],
                       selected_inputs: list[dict[str, Any]] | None = None
                       ) -> tuple[Path, list[dict[str, Any]], dict[str, Any]]:
    """Trace terminal evidence back to individual normalized input rows."""
    inputs = _read_jsonl_rows(input_file) if selected_inputs is None else selected_inputs
    input_ids = [(_audit_record_id(row) or _document_id_of(row) or f"input-{i:06d}")
                 for i, row in enumerate(inputs, start=1)]
    aliases: dict[str, list[int]] = {}
    for index, (row, identity) in enumerate(zip(inputs, input_ids)):
        for ref in {identity, *_audit_refs(row)}:
            aliases.setdefault(ref, []).append(index)

    def candidates(record, unit_id):
        refs = (unit_id, *_audit_refs(record))
        for ref in refs:
            if ref in aliases:
                return aliases[ref]
        # Legacy sentence outputs sometimes carry only a suffixed business id.
        for end in range(len(unit_id) - 1, 0, -1):
            if unit_id[end] in "-_:" and unit_id[:end] in aliases:
                return aliases[unit_id[:end]]
        return []

    order = [op for op in operator_ids if op in stages]
    final_op = "17" if "17" in order else (order[-1] if order else None)
    terminals = []
    unknowns = []
    # Every processor's line numbers refer to its own input batch. Carry the
    # normalized input ordinal alongside each stage output, without adding any
    # tracing fields to the business data actually sent to processors.
    active = list(zip(inputs, range(len(inputs))))
    for op in order:
        stage = stages[op]
        outputs = _read_jsonl_rows(stage.get("outputFile"))
        stage_output_ids = {_audit_record_id(row) for row in outputs}
        fields = build_predict_fields(stage, sample_limit=None, operator_id=op)
        local_by_id = {}
        for pos, (row, _) in enumerate(active):
            key = _audit_record_id(row) or _document_id_of(row)
            if key:
                local_by_id.setdefault(key, []).append(pos)
        consumed = set()

        def local_parent(record, uid, *, output=False):
            positions = local_by_id.get(uid, ())
            matches = [pos for pos in positions if pos not in consumed and all(
                record.get(key) is None or record[key] == active[pos][0].get(key)
                for key in ("source", "target"))]
            line = record.get("line")
            if isinstance(line, int) and line - 1 in matches:
                pos = line - 1
            elif matches:
                pos = matches[0]
            elif output and positions:
                # Some processors rewrite text while keeping input order/ID.
                pos = next((pos for pos in positions if pos not in consumed), None)
            else:
                pos = None
            if pos is not None:
                consumed.add(pos)
                return active[pos][1]
            parents = candidates(record, uid)
            return parents[0] if len(parents) == 1 else None

        for record in fields["rejectedSample"]:
            decision = record["decision"]
            uid = _audit_record_id(record)
            if not uid or decision == "retained":
                continue
            if decision == "unknown":
                raw = fields["statistics"]["raw_statistics"]
                if uid in stage_output_ids and raw.get("physical_reject_records") == 0:
                    continue
                unknowns.append((op, uid, record, decision, None))
            else:
                terminals.append((op, uid, record, decision, local_parent(record, uid)))

        next_active = []
        for record in outputs:
            uid = _audit_record_id(record)
            parent = local_parent(record, uid, output=True) if uid else None
            next_active.append((record, parent))
            if uid and parent is not None:
                parents = aliases.setdefault(uid, [])
                if parent not in parents:
                    parents.append(parent)
            if uid and op == final_op:
                terminals.append((op, uid, record, "delivered", parent))
        active = next_active

    rows = []
    used_by_unit: dict[str, set[int]] = {}
    represented = set()
    occurrences = {}

    def append_row(op, uid, record, decision, parent):
        original = inputs[parent] if parent is not None else {}
        parent_id = input_ids[parent] if parent is not None else None
        ordinal = parent + 1 if parent is not None else None
        occurrence = occurrences.get((ordinal, uid), 0) + 1
        occurrences[(ordinal, uid)] = occurrence
        row = {"input_id": parent_id or uid, "unit_id": uid, "parent_input_id": parent_id,
               "input_line": ordinal,
               "instance_id": json.dumps([ordinal, uid, occurrence], ensure_ascii=False),
               "source": record.get("source") or original.get("source"),
               "target": record.get("target") or original.get("target"),
               "source_ref": (record.get("source_ref") or _audit_metadata(record).get("source_ref")
                              or original.get("source_ref")),
               "document_id": _document_id_of(record) or _document_id_of(original),
               "decision": decision, "final_stage": op,
               "reasons": record.get("reasons", []) if decision in {"rejected", "unknown"} else [],
               "status_values": _audit_status_values(record),
               "delivery_status": (record.get("delivery_status") if decision == "delivered" else None) or decision}
        if record.get("audit_id") is not None:
            row["audit_id"] = record["audit_id"]
        rows.append(row)
        if parent is not None:
            represented.add(parent)
            used_by_unit.setdefault(uid, set()).add(parent)

    for op, uid, record, decision, parent in terminals + unknowns:
        parents = candidates(record, uid)
        used = used_by_unit.get(uid, set())
        available = [index for index in parents if index not in used]
        if decision == "unknown" and used and not available:
            continue
        if parent is None:
            matched = [index for index in available if all(
                not record.get(key) or record[key] == inputs[index].get(key)
                for key in ("source", "target"))]
            parent = next(iter(matched or available or parents), None)
        append_row(op, uid, record, decision, parent)
    for index, (uid, record) in enumerate(zip(input_ids, inputs)):
        if index not in represented:
            append_row(None, uid, record, "not_processed", index)
    rows.sort(key=lambda row: (row["input_line"] or len(inputs) + 1, row["unit_id"]))
    by_stage: dict[str, int] = {}
    by_reason: dict[str, int] = {}
    delivered = rejected = unknown = not_processed = 0
    for row in rows:
        decision = row["decision"]
        if decision == "delivered":
            delivered += 1
        elif decision == "rejected":
            rejected += 1
            stage = str(row.get("final_stage") or "unknown")
            by_stage[stage] = by_stage.get(stage, 0) + 1
            for reason in row.get("reasons") or []:
                by_reason[reason] = by_reason.get(reason, 0) + 1
        elif decision == "unknown":
            unknown += 1
        else:
            not_processed += 1

    audit_path = task / "audit" / "18_audit.jsonl"
    _write_jsonl_rows(audit_path, rows)
    summary = {
        "input_records": len(inputs),
        "audit_records": len(rows),
        "delivered": delivered,
        "rejected": rejected,
        "not_processed": not_processed,
        "unknown": unknown,
        "byStage": by_stage,
        "byReason": by_reason,
    }
    return audit_path, rows, summary


# [无血缘改造 2026-10-19] 原 _file_header() 文件身份头、_engineering_tail() 结论/交付补行
# 以及 _lineage_rows() 均已停用并删除；不再生成任何血缘行。


class _StreamStage:
    """Incremental stage sink. Files are visible while downstream records run."""

    def __init__(self, task: Path, unique_id: str, operator_id: str, input_path: Path | None = None):
        self.task = task
        self.unique_id = unique_id
        self.operator_id = operator_id
        self.input_path = input_path
        self.stage_dir = task / "operators" / operator_id / unique_id
        if self.stage_dir.exists():
            shutil.rmtree(self.stage_dir)
        self.stage_dir.mkdir(parents=True, exist_ok=True)
        spec = REGISTRY[operator_id]
        self.output_file = self.stage_dir / spec["outputFile"]
        self.reject_file = self.stage_dir / spec["rejectFile"]
        # [无血缘改造 2026-10-19] 不再创建 lineage.jsonl
        # self.lineage_file = self.stage_dir / "lineage.jsonl"
        self.events_file = self.stage_dir / "stream_events.jsonl"
        self.output_stream = self.output_file.open("w", encoding="utf-8", newline="\n")
        self.reject_stream = self.reject_file.open("w", encoding="utf-8", newline="\n")
        self.events_stream = self.events_file.open("w", encoding="utf-8", newline="\n")
        self.input_count = 0
        self.output_count = 0
        self.reject_count = 0
        self.physical_reject_count = 0
        self.raw_statistics: dict[str, Any] = {}
        self.processor_calls = 0
        self.failure_message: str | None = None

    def collect_statistics(self, result: dict[str, Any], audit_records: int) -> None:
        self.processor_calls += 1
        if result.get("status") == "failed" and self.failure_message is None:
            self.failure_message = str(result.get("message") or "处理器返回 failed").strip() or "处理器返回 failed"
        normalized = normalize_statistics(result, operator_id=self.operator_id,
                                          audit_records=audit_records)
        self.physical_reject_count += normalized["rejected_records"]
        raw = normalized.get("raw_statistics") or {}
        for key, value in raw.items():
            if (isinstance(value, (int, float)) and not isinstance(value, bool)
                    and key.startswith(("min_", "max_"))):
                combine = min if key.startswith("min_") else max
                previous = self.raw_statistics.get(key)
                if isinstance(previous, (int, float)) and not isinstance(previous, bool):
                    self.raw_statistics[key] = combine(previous, value)
                else:
                    self.raw_statistics[key] = value
            elif key in {"window", "candidate_window", "batch_size"}:
                self.raw_statistics.setdefault(key, value)
            elif isinstance(value, int) and not isinstance(value, bool):
                self.raw_statistics[key] = self.raw_statistics.get(key, 0) + value
            else:
                self.raw_statistics.setdefault(key, value)

    def event(self, event: str, sequence: int, record_id: str | None = None) -> None:
        value = {"event": event, "operatorId": self.operator_id,
                 "sequence": sequence, "recordId": record_id,
                 "timestamp": datetime.now(timezone.utc).isoformat(timespec="milliseconds")}
        self.events_stream.write(json.dumps(value, ensure_ascii=False) + "\n")
        self.events_stream.flush()

    def append(self, result: dict[str, Any], rows: list[dict[str, Any]],
               rejects: list[dict[str, Any]], sequence: int) -> None:
        self.collect_statistics(result, len(rejects))
        self.input_count += 1
        for row in rows:
            self.output_stream.write(json.dumps(row, ensure_ascii=False, separators=(",", ":")) + "\n")
            self.output_count += 1
        for item in rejects:
            self.reject_stream.write(json.dumps(item, ensure_ascii=False, separators=(",", ":")) + "\n")
            self.reject_count += 1
        self.output_stream.flush()
        self.reject_stream.flush()
        duplicate_file = result.get("duplicateFile")
        if duplicate_file and Path(duplicate_file).is_file():
            shutil.copy2(duplicate_file, self.stage_dir / Path(duplicate_file).name)
        # [无血缘改造 2026-10-19] 不再从算子结果读取/追加血缘行。

    def append_batch(self, result: dict[str, Any], rows: list[dict[str, Any]],
                     rejects: list[dict[str, Any]],
                     input_records: list[dict[str, Any]] | None = None,
                     sequence_start: int = 0) -> int:
        """Append one whole-stage batch (阶段批处理).

        ``input_records`` is the batch the operator just consumed: it drives
        ``input_count`` and one per-record ``input`` stream event. Output rows and
        rejects accumulate as usual; every output row also gets a per-record
        ``output`` event, so ``stream_events.jsonl`` keeps record-level semantics
        even though the processor ran once for the whole batch. Returns the next
        unused event sequence.
        """
        batch_input = [item for item in (input_records or []) if isinstance(item, dict)]
        self.collect_statistics(result, len(rejects))
        self.input_count += len(batch_input)
        sequence = sequence_start
        for item in batch_input:
            self.event("input", sequence, item.get("id"))
            sequence += 1
        for row in rows:
            self.output_stream.write(json.dumps(row, ensure_ascii=False, separators=(",", ":")) + "\n")
            self.output_count += 1
            self.event("output", sequence, row.get("id"))
            sequence += 1
        for item in rejects:
            self.reject_stream.write(json.dumps(item, ensure_ascii=False, separators=(",", ":")) + "\n")
            self.reject_count += 1
        self.output_stream.flush()
        self.reject_stream.flush()
        duplicate_file = result.get("duplicateFile")
        if duplicate_file and Path(duplicate_file).is_file():
            shutil.copy2(duplicate_file, self.stage_dir / Path(duplicate_file).name)
        return sequence

    def close(self) -> None:
        self.output_stream.close()
        self.reject_stream.close()
        self.events_stream.close()

    def result(self, status: str, message: str = "") -> dict[str, Any]:
        if self.failure_message is not None:
            status, message = "failed", self.failure_message
        elif status == "failed" and not message:
            message = ("本阶段未产生输出记录，请查看统计和拒因"
                       if self.processor_calls else "上游无可处理记录，本阶段未执行")
        return {"status": status, "outputFile": str(self.output_file) if self.output_count else None,
                "rejectFile": str(self.reject_file),
                "statistics": {"input_records": self.input_count,
                                "output_records": self.output_count,
                                "rejected_records": self.physical_reject_count,
                                "audit_records": self.reject_count,
                                "raw_statistics": self.raw_statistics},
                "message": message}


def _stage_reject_rows(result: dict[str, Any]) -> list[dict[str, Any]]:
    """Include dedup removals, which processors keep in a separate artifact."""
    rejected = _read_jsonl_rows(result.get("rejectFile"))
    duplicate_file = result.get("duplicateFile")
    if duplicate_file and duplicate_file != result.get("rejectFile"):
        for row in _read_jsonl_rows(duplicate_file):
            row.setdefault("reasons", ["duplicate_pair"])
            row.setdefault("decision", "rejected")
            rejected.append(row)
    return rejected


def _stream_operator_item(op: str, record: dict[str, Any], sequence: int,
                          task: Path, unique_id: str, params: dict[str, Any]) -> tuple[dict[str, Any], list[dict[str, Any]], list[dict[str, Any]]]:
    run_root = task / "stream_runs"
    item_uid = f"stream_{sequence:06d}_{op}"
    item_dir = run_root / item_uid
    item_dir.mkdir(parents=True, exist_ok=True)
    input_file = item_dir / "input.jsonl"
    _write_jsonl_rows(input_file, [record])
    overrides = dict(params.get("operatorOverrides", {}).get(op, {}))
    for key, value in params.items():
        if key not in STREAM_CONTROL_KEYS:
            overrides.setdefault(key, value)
    result = _run_chain_processor(op, input_file, item_uid, run_root / "outputs", overrides)
    output_file = Path(result["outputFile"]) if result.get("outputFile") else None
    rows = _read_jsonl_rows(output_file)
    rejects = _stage_reject_rows(result)
    return result, rows, rejects


def _stream_stage_finalize(task: Path, unique_id: str, stage: _StreamStage,
                           status: str, message: str = "") -> dict[str, Any]:
    stage.close()
    result = stage.result(status, message)
    if stage.output_count:
        stage_zip = _archive_stage(task, stage.operator_id, unique_id, stage.output_file)
    else:
        _invalidate_stage(task, stage.operator_id, unique_id)
        stage_zip = None
    result["stageZip"] = str(stage_zip) if stage_zip else None
    result["storageUri"] = _publish_local_object(task, stage_zip)
    _record_transfer(unique_id, stage.operator_id, stage.operator_id == "17", result, stage_zip)
    return result


def _run_stream_linear(task: Path, unique_id: str, records: list[dict[str, Any]],
                       operator_ids: list[str], params: dict[str, Any],
                       sequence_start: int = 0, previous_operator: str | None = None) -> tuple[list[dict[str, Any]], dict[str, dict[str, Any]], int]:
    """Pass one record through all listed operators before taking the next record."""
    sinks = {}
    for index, op in enumerate(operator_ids):
        previous = operator_ids[index - 1] if index else previous_operator
        if previous:
            previous_name = REGISTRY[previous]["outputFile"]
            input_path = task / "operators" / previous / unique_id / previous_name
        else:
            input_files = sorted((task / "input").glob("*"))
            input_path = input_files[0] if input_files else task / "input"
        sinks[op] = _StreamStage(task, unique_id, op, input_path)
    final_records: list[dict[str, Any]] = []
    sequence = sequence_start
    for record in records:
        carry = [record]
        for op in operator_ids:
            next_records: list[dict[str, Any]] = []
            for item in carry:
                sequence += 1
                sinks[op].event("input", sequence, item.get("id"))
                result, rows, rejects = _stream_operator_item(op, item, sequence, task, unique_id, params)
                rows = _carry_document_id(rows, [item])
                sinks[op].append(result, rows, rejects, sequence)
                sinks[op].event("output", sequence, item.get("id"))
                next_records.extend(rows)
            carry = next_records
            if not carry:
                break
        final_records.extend(carry)
    results = {}
    for op, sink in sinks.items():
        status = "success" if sink.output_count and not sink.reject_count else (
            "partial" if sink.output_count else "failed")
        results[op] = _stream_stage_finalize(task, unique_id, sink, status)
    return final_records, results, sequence


def _run_stream_stage_batched(task: Path, unique_id: str, records: list[dict[str, Any]],
                              operator_ids: list[str], params: dict[str, Any],
                              sequence_start: int = 0,
                              previous_operator: str | None = None
                              ) -> tuple[list[dict[str, Any]], dict[str, dict[str, Any]], int]:
    """阶段批处理：每个算子一次处理当前全部记录，模型权重每阶段只加载一次。

    与 `_run_stream_linear` 的逐条交接不同，这里按算子顺序把当前整批记录写入
    `task/stream_runs/batch_<seq>_<op>/input.jsonl`，调用一次 `_run_processor`，
    并直接消费该算子声明的 JSONL 输出。阶段产物
    （outputFile/rejectFile/stageZip/storageUri）与逐条模式完全一致；`stream_events`
    仍按记录写 input/output 事件。
    """
    run_root = task / "stream_runs"
    sinks: dict[str, _StreamStage] = {}
    for index, op in enumerate(operator_ids):
        previous = operator_ids[index - 1] if index else previous_operator
        if previous:
            previous_name = REGISTRY[previous]["outputFile"]
            input_path = task / "operators" / previous / unique_id / previous_name
        else:
            input_files = sorted((task / "input").glob("*"))
            input_path = input_files[0] if input_files else task / "input"
        sinks[op] = _StreamStage(task, unique_id, op, input_path)
    sequence = sequence_start
    current = list(records)
    for op in operator_ids:
        if not current:
            break
        batch_uid = f"batch_{sequence:06d}_{op}"
        batch_dir = run_root / batch_uid
        batch_dir.mkdir(parents=True, exist_ok=True)
        batch_input = batch_dir / "input.jsonl"
        _write_jsonl_rows(batch_input, current)
        overrides = dict(params.get("operatorOverrides", {}).get(op, {}))
        for key, value in params.items():
            if key not in STREAM_CONTROL_KEYS:
                overrides.setdefault(key, value)
        result = _run_chain_processor(op, batch_input, batch_uid, run_root / "outputs", overrides)
        output_file = Path(result["outputFile"]) if result.get("outputFile") else None
        rows = _read_jsonl_rows(output_file)
        rows = _carry_document_id(rows, current)
        rejects = _stage_reject_rows(result)
        sequence = sinks[op].append_batch(result, rows, rejects,
                                          input_records=current,
                                          sequence_start=sequence)
        current = rows
    results: dict[str, dict[str, Any]] = {}
    for op, sink in sinks.items():
        status = "success" if sink.output_count and not sink.reject_count else (
            "partial" if sink.output_count else "failed")
        results[op] = _stream_stage_finalize(task, unique_id, sink, status)
    return current, results, sequence


def _stream_plan_files(records: list[dict[str, Any]], plan_directory: Path) -> dict[str, Path]:
    root = plan_directory.resolve()
    plans = {}
    for index, row in enumerate(records, start=1):
        doc_id = row.get("document_id") or _audit_metadata(row).get("document_id")
        if not isinstance(doc_id, str) or not doc_id.strip():
            raise ValueError(f"清单第{index}条记录缺少 document_id")
        relative = PureWindowsPath(doc_id)
        if relative.anchor or ".." in relative.parts:
            raise ValueError(f"document_id 路径越界: {doc_id}")
        plan = (root / f"{doc_id}.json").resolve()
        if not plan.is_relative_to(root):
            raise ValueError(f"document_id 路径越界: {doc_id}")
        if not plan.is_file():
            raise FileNotFoundError(f"算子08计划缺失: {doc_id}.json")
        plans[doc_id] = plan
    return plans


def _stream_chain(task: Path, unique_id: str, input_file: Path,
                  operator_ids: list[str], params: dict[str, Any]) -> dict[str, Any]:
    """Run the adapted dataflow chain.

    入口层已按 ``params.inputMode`` 适配输入并选定默认链：
    - pairs/txt：01→07 清理 → 09 句子级对齐 → 12→15 → 11 领域 → 16 实体 → 17 打包；
    - documents：08→09→12→15→11→16→17（params.planDirectory 文档对齐计划路径）。
    ``params.executionMode`` 控制阶段间交接语义：
    - ``stage_batched``（默认）：每个算子对当前全部记录调用一次 processor（模型一阶段
      一加载），返回 ``handoff=stage_batched``；
    - ``record_immediate``：旧模式，逐条记录交接（模型逐条重载，仅用于回滚/对照），
      返回 ``handoff=record_immediate``。
    两种模式的 stages/stageZip/storageUri/outputFile 契约与 stream_events 记录级事件一致。
    成功后额外生成 ``task/audit/18_audit.jsonl``，顶层返回 ``auditFile`` /
    ``auditFileUri`` / ``auditSummary``；它汇总所有阶段 reject 与最终交付，不是血缘文件。
    """
    # 旧 00 已删除；18/19 是工程侧系统功能；20-23 是可选增强分支。
    unsupported = [op for op in operator_ids if op not in REGISTRY or op in {"18", "19"}]
    if unsupported:
        raise ValueError("链式演示仅支持已注册的业务算子01-17与可选增强20-23，不调用: " + ",".join(unsupported))
    execution_mode = params.get("executionMode", "stage_batched")
    if execution_mode not in STREAM_EXECUTION_MODES:
        raise ValueError("params.executionMode 仅支持 stage_batched（默认）或 record_immediate")
    runner = _run_stream_stage_batched if execution_mode == "stage_batched" else _run_stream_linear
    stages: dict[str, dict[str, Any]] = {}
    current = _read_jsonl_rows(input_file)
    max_records = params.get("maxRecords")
    if isinstance(max_records, int) and max_records > 0:
        current = current[:max_records]
    selected_inputs = current

    # 01-07 are record processors. 默认整批交给下一阶段；record_immediate 逐条交接。
    prefix = [op for op in operator_ids if op not in {"08", "09", "10", "11", "12", "13", "14", "15", "16", "17"}]
    sequence = 0
    if prefix:
        current, prefix_stages, sequence = runner(
            task, unique_id, current, prefix, params, sequence)
        stages.update(prefix_stages)

    # Operator 08 has a document-plan contract. Documents are the only explicit barrier;
    # each emitted paragraph pair then enters operator 09 onward (stage-batched by default).
    if "08" in operator_ids:
        plan_dir = Path(params.get("planDirectory", ""))
        if not plan_dir.is_absolute():
            plan_dir = (ROOT / plan_dir).resolve()
        if not plan_dir.is_dir():
            raise FileNotFoundError("链式调用算子08必须提供 params.planDirectory")
        plans = _stream_plan_files(selected_inputs, plan_dir)
        stage08 = _StreamStage(task, unique_id, "08")
        paragraph_rows: list[dict[str, Any]] = []
        for document_id, plan in plans.items():
            sequence += 1
            stage08.event("document_start", sequence, document_id)
            result, rows, rejects = _stream_operator_item(
                "08", {"plan_file": str(plan)}, sequence, task, unique_id,
                {**params, "plan_file": str(plan), "extra_input_root": str(plan_dir.parent)})
            # _stream_operator_item is JSONL-oriented; operator 08 needs its plan directly.
            if result.get("status") == "failed":
                result = _run_chain_processor("08", plan, f"stream_{sequence:06d}_08", task / "stream_runs" / "outputs",
                                        {"plan_file": str(plan), "extra_input_root": str(plan_dir.parent),
                                         **dict(params.get("operatorOverrides", {}).get("08", {}))})
                rows = _read_jsonl_rows(result.get("outputFile"))
                rejects = _read_jsonl_rows(result.get("rejectFile"))
            processed_rows: list[dict[str, Any]] = []
            for row in rows:
                row = dict(row)
                declared = row.get("document_id")
                if declared and declared != document_id:
                    row.setdefault("declared_document_id", declared)
                row["id"] = f"{document_id}::{row.get('id', sequence)}"
                row["document_id"] = document_id
                row["parent_input_id"] = document_id
                metadata = dict(row.get("metadata") or {})
                metadata["document_id"] = document_id
                row["metadata"] = metadata
                processed_rows.append(row)
            for reject in rejects:
                declared = reject.get("document_id")
                if declared and declared != document_id:
                    reject.setdefault("declared_document_id", declared)
                reject["document_id"] = document_id
                reject["parent_input_id"] = document_id
                reject_id = reject.get("id")
                if isinstance(reject_id, str) and not reject_id.startswith(document_id + "::"):
                    reject["id"] = f"{document_id}::{reject_id}"
            paragraph_rows.extend(processed_rows)
            stage08.input_path = plan
            # Sink the exact rows handed to #9, not the un-prefixed #8 output.
            stage08.append(result, processed_rows, rejects, sequence)
            stage08.event("document_output", sequence, document_id)
        status08 = "success" if paragraph_rows and not stage08.reject_count else ("partial" if paragraph_rows else "failed")
        stages["08"] = _stream_stage_finalize(task, unique_id, stage08, status08)
        current = paragraph_rows

    downstream = [op for op in operator_ids if op in {"09", "10", "12", "13", "14", "15", "11", "16"}]
    if downstream:
        prior = "08" if "08" in operator_ids else (prefix[-1] if prefix else None)
        current, downstream_stages, sequence = runner(
            task, unique_id, current, downstream, params, sequence, prior)
        stages.update(downstream_stages)

    if "17" in operator_ids:
        stage_root = task / "operators" / "17"
        stage_root.mkdir(parents=True, exist_ok=True)
        input16 = task / "operators" / "16" / unique_id / REGISTRY["16"]["outputFile"]
        if "16" not in stages or not input16.is_file():
            input16 = task / "stream_runs" / "final_16.jsonl"
            _write_jsonl_rows(input16, current)
        result17 = _run_chain_processor("17", input16, unique_id, stage_root,
                                  dict(params.get("operatorOverrides", {}).get("17", {})))
        output17 = Path(result17["outputFile"]) if result17.get("outputFile") else None
        zip17 = _archive_stage(task, "17", unique_id, output17)
        result17["stageZip"] = str(zip17) if zip17 else None
        result17["storageUri"] = _publish_local_object(task, zip17)
        _record_transfer(unique_id, "17", True, result17, zip17)
        stages["17"] = result17

    # [无血缘改造 2026-10-19] 不再生成任务级聚合 lineage.jsonl，也不再写 file/outcome 补行。
    final = stages.get("17") or stages.get(operator_ids[-1], {})
    data = {"uniqueId": unique_id, "streaming": True, "stages": stages,
            "stageZip": final.get("stageZip"),
            "storageUri": final.get("storageUri"), "handoff": execution_mode}
    if final.get("outputFile") and Path(final["outputFile"]).is_file():
        data["outputFile"] = Path(final["outputFile"]).read_text(encoding="utf-8")

    # Java-facing decision audit: one row per input/business unit, joining all
    # upstream stage rejects with the final delivered output.  This is not a
    # lineage file; lineage_refs remain empty/unavailable by design.
    audit_path, _audit_rows, audit_summary = _build_chain_audit(
        task, unique_id, input_file, operator_ids, stages, selected_inputs)
    data["auditFile"] = audit_path.read_text(encoding="utf-8")
    data["auditFileUri"] = audit_path.as_uri()
    data["auditSummary"] = audit_summary
    return data


@app.get("/health")
async def health() -> dict[str, Any]:
    return {"code": 0, "msg": "success", "data": {
        "status": "alive", "service": "parallel-corpus-operator-pool",
        "version": "v1.0.0", "operatorCount": len(REGISTRY)}}


@app.get("/operators")
async def operators() -> dict[str, Any]:
    return {"code": 0, "msg": "success", "data": REGISTRY}


@app.post("/uploadFile")
async def upload_file(uniqueId: str = Query(...), file: UploadFile = File(...)):
    try:
        task = _workspace(uniqueId)
        task_id, file_id = _identity(uniqueId)
    except ValueError as exc:
        return {"code": 1, "msg": str(exc), "data": None}
    dest_dir = task / "input"
    dest_dir.mkdir(parents=True, exist_ok=True)
    filename = Path(file.filename or "input.jsonl").name
    dest = dest_dir / filename
    dest.write_bytes(await file.read())
    # [无血缘改造 2026-10-19] 不再在任务目录写入 lineage.jsonl。
    return {"code": 0, "msg": "success", "data": {
        "taskId": task_id, "fileId": file_id, "uniqueId": uniqueId,
        "inputFile": str(dest)}}


@app.get("/downloadFile", response_class=FileResponse, responses={
    200: {"description": "Stage ZIP archive", "content": {
        "application/zip": {"schema": {"type": "string", "format": "binary"}}}},
    400: {"description": "Invalid task, operator, or query parameter"},
    403: {"description": "Archive outside the task workspace"},
    404: {"description": "Stage archive not found"},
})
async def download_file(request: Request, uniqueId: str = Query(...),
                        operatorId: str = Query(...),
                        operatorIdScheme: Literal["current", "legacy"] = Query("current")):
    """Stream the same task/operator ZIP published by predict or predictChain."""
    allowed = {"uniqueId", "operatorId", "operatorIdScheme"}
    if any(key not in allowed for key in request.query_params):
        raise HTTPException(status_code=400, detail="仅支持 uniqueId、operatorId 和 operatorIdScheme")
    operator_id = canonical_operator_ids([operatorId], operatorIdScheme)[0]
    if operator_id not in REGISTRY:
        raise HTTPException(status_code=400, detail="operatorId 未注册")
    try:
        task = _workspace(uniqueId)
    except (ValueError, OSError, RuntimeError):
        raise HTTPException(status_code=400, detail="uniqueId 格式非法或任务路径越界")
    # _archive_stage owns this layout; do not reconstruct processor-internal
    # paths, whose historical numeric prefixes differ from current API IDs.
    filename = f"stage_{operator_id}_{uniqueId}.zip"
    try:
        archive = (task / "artifacts" / filename).resolve()
        archive.relative_to(task)
    except (ValueError, OSError, RuntimeError):
        raise HTTPException(status_code=403, detail="阶段产物路径越界")
    if not archive.is_file():
        raise HTTPException(status_code=404, detail="阶段产物不存在")
    return FileResponse(archive, media_type="application/zip", filename=filename)


@app.post("/predict")
async def predict(req: PredictRequest):
    try:
        if req.isLastOperator is None:
            return {"code": 1, "msg": "isLastOperator 必传且必须为布尔值", "data": None}
        requested_operator_id = str(req.operatorId or "").strip()
        if not requested_operator_id:
            return {"code": 1, "msg": "operatorId 必传", "data": None}
        operator_id = (canonical_operator_id(requested_operator_id)
                       if req.operatorIdScheme == "legacy" else requested_operator_id)
        if operator_id not in REGISTRY:
            return {"code": 1, "msg": f"未注册的 operatorId: {requested_operator_id}", "data": None}
        task = _workspace(req.uniqueId)
        _invalidate_stage(task, operator_id, req.uniqueId)
        input_file = _resolve_input(task, req.inputFile)
        _validate_input_format(input_file, operator_id)
        overrides = dict(req.config or {})
        overrides.update(req.params or {})
        sample_limit = extract_sample_limit(overrides)
        result = _run_processor(operator_id, input_file, req.uniqueId,
                                task / "operators" / operator_id, overrides)
        if result.get("status") == "failed":
            _record_transfer(req.uniqueId, operator_id, req.isLastOperator, result, None)
            return {"code": 1, "msg": result.get("message") or "处理失败", "data": None}
        output_file = Path(result["outputFile"]) if result.get("outputFile") else None
        if req.isLastOperator and (output_file is None or not output_file.is_file()):
            _record_transfer(req.uniqueId, operator_id, req.isLastOperator, result, None)
            return {"code": 1, "msg": "阶段末输出文件不存在", "data": None}
        stage_zip = _archive_stage(task, operator_id, req.uniqueId, output_file)
        storage_uri = _publish_local_object(task, stage_zip)
        _record_transfer(req.uniqueId, operator_id, req.isLastOperator, result, stage_zip)
        # 保留原传输字段；Java 侧使用下面新增的业务样本和判定证据建立血缘。
        data: dict[str, Any] = {
            "outputFile": (output_file.read_text(encoding="utf-8")
                           if req.isLastOperator and output_file else
                           (str(output_file) if output_file else None)),
            "rejectFile": result.get("rejectFile"),
            "stageZip": str(stage_zip) if stage_zip else None,
            "storageUri": storage_uri,
        }
        data.update(build_predict_fields(result, sample_limit=sample_limit,
                                         operator_id=operator_id, input_file=input_file))
        return {"code": 0, "msg": "success", "data": data}
    except (ValueError, FileNotFoundError, RuntimeError, OSError, json.JSONDecodeError) as exc:
        return {"code": 1, "msg": str(exc), "data": None}


@app.post("/predictChain")
async def predict_chain(req: ChainPredictRequest):
    """入口适配后的数据流链（默认 stage_batched 交接；record_immediate 用于回滚）。

    ``params.inputMode``（auto|pairs|txt|documents，默认 auto）：
    - pairs：同文件双语 JSONL/TSV/CSV/TMX 归一到任务 input/ 下的句对 JSONL，再执行
      默认链 01→07→09→12→15→11→16→17（不执行 00/08/18/19）；
    - txt：上游由分文件双语 PDF/TXT/Moses 转换的双语 txt，行内分隔或奇偶行配对，
      转换后执行同一默认链；
    - documents：保留原 planDirectory 文档对齐路径，默认链 08→09→12→15→11→16→17。
    ``req.operatorIds`` 显式传入时优先于默认链。默认 ``operatorIdScheme=current``
    不做旧编号转换；兼容期请求须显式传 ``operatorIdScheme="legacy"`` 才会把
    旧 API02-API24 编号归一化为当前 API01-API23 编号。
    """
    try:
        task = _workspace(req.uniqueId)
        operator_ids = canonical_operator_ids(list(req.operatorIds or []), req.operatorIdScheme)
        for op in operator_ids:
            if op in REGISTRY:
                _invalidate_stage(task, op, req.uniqueId)
        input_file = _resolve_input(task, req.inputFile)
        params = canonical_operator_params(req.params, req.operatorIdScheme)
        input_mode = _resolve_chain_input_mode(input_file, params)
        if not operator_ids:
            default_chain = STREAM_DOCUMENTS_DEFAULT_CHAIN if input_mode == "documents" else STREAM_DEFAULT_CHAIN
            operator_ids = canonical_operator_ids(list(default_chain), req.operatorIdScheme)
            for op in operator_ids:
                _invalidate_stage(task, op, req.uniqueId)
        sample_limit = extract_sample_limit(params)
        preparation: Optional[dict[str, Any]] = None
        if input_mode == "documents":
            try:
                _validate_input_format(input_file, "01")
            except (ValueError, json.JSONDecodeError) as exc:
                raise ValueError(
                    "documents 模式需要文档清单 JSONL（每行一条文档/句对记录，含 document_id）"
                    f"与 params.planDirectory 对齐计划目录；输入校验失败：{exc}") from exc
        else:
            try:
                preparation = ingest.prepare_input(input_file, input_mode, task / "input")
            except ingest.IngestError as exc:
                raise ValueError(f"{input_mode} 入口预处理失败：{exc}") from exc
            normalized_input = Path(preparation["normalizedFile"])
            _validate_input_format(normalized_input, "01")
            _validate_pairs_chain_operators(operator_ids)
            input_file = normalized_input
        data = _stream_chain(task, req.uniqueId, input_file, operator_ids, params)
        for op, stage in data["stages"].items():
            stage.update(build_predict_fields(stage, sample_limit=sample_limit,
                                              operator_id=op, input_file=input_file))
        final = data["stages"].get("17") or data["stages"].get(operator_ids[-1], {})
        for key in ("statistics", "outputSample", "rejectedSample", "rejectReasons"):
            data[key] = final.get(key, [] if key.endswith("Sample") else {})
        data["inputMode"] = input_mode
        data["operatorIds"] = list(operator_ids)
        if preparation is not None:
            data["inputPrep"] = {
                "originalFile": preparation["originalFile"],
                "normalizedFile": preparation["normalizedFile"],
                "records": preparation["records"],
                "statistics": preparation["statistics"],
            }
        failed_stages = [
            {"operatorId": op, "reason": stage.get("message") or "本阶段执行失败"}
            for op, stage in data["stages"].items()
            if stage.get("status") == "failed"
        ]
        if failed_stages:
            data["failedStages"] = failed_stages
            first = failed_stages[0]
            return {"code": 1,
                    "msg": f"算子链执行失败，阶段{first['operatorId']}：{first['reason']}",
                    "data": data}
        return {"code": 0, "msg": "success", "data": data}
    except (ValueError, FileNotFoundError, RuntimeError, OSError, json.JSONDecodeError) as exc:
        return {"code": 1, "msg": str(exc), "data": None}


@app.post("/removeFile")
async def remove_file(uniqueId: str = Query(...)):
    try:
        task = _workspace(uniqueId)
    except ValueError as exc:
        return {"code": 1, "msg": str(exc), "data": None}
    if task.exists():
        for path in sorted(task.rglob("*"), reverse=True):
            if path.is_file():
                path.unlink()
            elif path.is_dir():
                path.rmdir()
        task.rmdir()
    return {"code": 0, "msg": "success"}
