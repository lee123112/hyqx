# -*- coding: utf-8 -*-
"""06 样本版本（avp_pack_version）· S1 阶段末算子。

职责
----
把质检通过的批次固化成**可交付的样本版本**（zip + sha256 manifest），并提供
发布 / 回滚 / 版本对比；版本注册表持久化在 ``ctx.work_dir/006_registry.json``，
供 007 评测结果接收与 010 数据资产统计复用。

业务硬口径（任务书 §5 + 原型《样本版本 · 研发标注》）
-----------------------------------------------------
- 版本包含：名称、版本号、样本数、来源任务、质检结论、状态、发布时间；
- 只有「已发布」版本对外可见；草稿与已回滚不出现；
- 发布只能「草稿 → 已发布」，且 ``qc_conclusion`` 必须为「通过」（否则
  ``qc_not_passed``）；已发布不能退回草稿，已回滚不能再回到已发布；
- **回滚只改状态**：不复制、不删除任何样本/版本包文件；
- 版本对比只读，默认取最近两个「已发布」版本；
- 剔除样本（``qc.status=rejected``）不计入版本，包内样本数 = 通过数。

四个动作（``params.action``）
-----------------------------
``pack``     批次 → ``006_version.zip``（manifest.json + 最终样本表 + 音频）+ ``006_version.json``
``publish``  草稿 → 已发布（带版本包 sha256 完整性校验）
``rollback`` 已发布 → 已回滚（只改注册表状态，不动文件）
``compare``  两个版本 → ``006_compare.json``（样本数差、新增/删除、sha256 差异）

技术方案
--------
纯文件与哈希流水线，无模型、无伪造指标：

1. 只把 ``qc.status == "pass"`` 的样本纳入版本；每个音频算 SHA-256 与字节数，
   连同样本 ID 写进版本包内 ``manifest.json``；
2. 音频按 ``audio/<sample_id>.<ext>`` 放进 zip，回读 zip 逐个重算哈希做
   **打包完整性自检**，校验通过率写入版本记录（不通过则整批失败，不静默降级）；
3. 版本号同任务内同版本名自增（v1.0 → v1.1），版本注册表原子写；
4. 发布前校验版本包仍在且 SHA-256 未变；回滚只改两个 JSON 的状态字段。
"""

from __future__ import annotations

import datetime as _dt
import hashlib
import json
import re
import shutil
import sys
import zipfile
from pathlib import Path
from typing import Dict, List, Optional, Tuple

_ROOT = Path(__file__).resolve().parent.parent
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))

from common import audio as A  # noqa: E402
from common import config as CFG  # noqa: E402
from common import iofs  # noqa: E402
from common import schemas  # noqa: E402
from common.context import OperatorContext  # noqa: E402

OPERATOR_ID = "avp_pack_version"
OPERATOR_NAME = "样本版本"

ACTIONS = ("pack", "publish", "rollback", "compare")

STATUS_DRAFT = "草稿"
STATUS_PUBLISHED = "已发布"
STATUS_ROLLED_BACK = "已回滚"
STATUSES = (STATUS_DRAFT, STATUS_PUBLISHED, STATUS_ROLLED_BACK)

QC_VERDICTS = ("通过", "待复核", "不通过")
QC_PASS = "通过"

SAMPLE_STATUS_PASS = "pass"
SAMPLE_STATUS_REJECTED = "rejected"
SAMPLE_STATUSES = (SAMPLE_STATUS_PASS, SAMPLE_STATUS_REJECTED)

VERSION_FILENAME = "006_version.json"
COMPARE_FILENAME = "006_compare.json"


# --------------------------------------------------------------------------- #
# 通用小工具
# --------------------------------------------------------------------------- #
def _now_iso() -> str:
    return _dt.datetime.now().astimezone().isoformat(timespec="seconds")


def _cfg(cfg: dict, dotted: str):
    return CFG.get(cfg, dotted)


def _is_blank(value) -> bool:
    if value is None:
        return True
    if isinstance(value, str):
        return not value.strip()
    if isinstance(value, (dict, list, tuple)):
        return len(value) == 0
    return False


def _row_sample_id(row: dict, index: int) -> str:
    raw = row.get("sample_id")
    if _is_blank(raw):
        raw = row.get("source_uttid")
    if _is_blank(raw):
        return "row_%06d" % int(index)
    return str(raw).strip()


def _safe_stem(text: str, max_len: int = 80) -> str:
    """把 sample_id 变成安全的 zip 内文件名（保留字母数字与 -_. ，其余替换）。"""
    chars = [ch if (ch.isalnum() or ch in "-_.") else "_" for ch in str(text)]
    stem = "".join(chars).strip("._") or "sample"
    return stem[:max_len]


# --------------------------------------------------------------------------- #
# 参数
# --------------------------------------------------------------------------- #
def _parse_params(params: dict, cfg: dict) -> dict:
    raw_action = params.get("action")
    action = str(raw_action).strip().lower() if raw_action is not None else ""
    if not action:
        raise schemas.ParamError("action 不能为空，允许：pack / publish / rollback / compare")
    if action not in ACTIONS:
        raise schemas.ParamError(
            "action 取值 %r 非法，允许：pack / publish / rollback / compare" % raw_action
        )

    version_name = None
    version_no = None
    source_task = None
    qc_conclusion = None
    published_at = None
    version_id = None
    compare_with = None

    if action == "pack":
        version_name = schemas.as_str(
            params.get("version_name"), "version_name", max_len=int(_cfg(cfg, "version.name_max_len"))
        )
        if len(version_name) < int(_cfg(cfg, "version.name_min_len")):
            raise schemas.ParamError(
                "version_name 至少 %d 个字：%r" % (int(_cfg(cfg, "version.name_min_len")), version_name)
            )
        raw_no = params.get("version_no")
        if not _is_blank(raw_no):
            version_no = str(raw_no).strip()
            pattern = re.compile(str(_cfg(cfg, "version.version_no_pattern")))
            if not pattern.match(version_no):
                raise schemas.ParamError(
                    "version_no 格式非法：%r（应形如 v1.2）" % version_no
                )
        if not _is_blank(params.get("source_task")):
            source_task = schemas.as_str(params.get("source_task"), "source_task", max_len=100)
        raw_qc = params.get("qc_conclusion")
        if not _is_blank(raw_qc):
            qc_conclusion = schemas.enum(str(raw_qc).strip(), QC_VERDICTS, "qc_conclusion")

    if action in ("publish", "rollback", "compare"):
        raw_id = params.get("version_id")
        if action in ("publish", "compare") and _is_blank(raw_id):
            version_id = None
        elif action == "rollback":
            version_id = schemas.as_str(raw_id, "version_id", max_len=120)
        else:
            version_id = schemas.as_str(raw_id, "version_id", max_len=120) if not _is_blank(raw_id) else None

        if not _is_blank(params.get("compare_with")):
            compare_with = schemas.as_str(params.get("compare_with"), "compare_with", max_len=120)

        if action == "publish":
            raw_qc = params.get("qc_conclusion")
            if not _is_blank(raw_qc):
                qc_conclusion = schemas.enum(str(raw_qc).strip(), QC_VERDICTS, "qc_conclusion")
            if not _is_blank(params.get("published_at")):
                published_at = schemas.as_str(params.get("published_at"), "published_at", max_len=64)

    return {
        "action": action,
        "version_name": version_name,
        "version_no": version_no,
        "source_task": source_task,
        "qc_conclusion": qc_conclusion,
        "published_at": published_at,
        "version_id": version_id,
        "compare_with": compare_with,
    }


# --------------------------------------------------------------------------- #
# 输入批次
# --------------------------------------------------------------------------- #
def _find_batch_file(ctx: OperatorContext, inp: Path) -> Path:
    for name in [str(x) for x in _cfg(ctx.config, "io.batch_filename_candidates")]:
        direct = inp / name
        if direct.is_file():
            return direct
        matches = sorted(inp.rglob(name))
        if matches:
            return matches[0]
    all_jsonl = sorted(p for p in inp.rglob("*.jsonl") if p.is_file())
    batch_like = [p for p in all_jsonl if "batch" in p.name.lower()]
    pool = batch_like or all_jsonl
    if len(pool) == 1:
        return pool[0]
    if pool:
        ctx.log.warning("输入目录有 %d 个候选 JSONL，按路径排序取第一个：%s" % (len(pool), pool[0].name))
        return pool[0]
    raise schemas.OpError("missing_file: 输入目录中没有最终样本表 JSONL：%s" % inp.name)


def _load_batch(ctx: OperatorContext) -> Tuple[Path, List[dict]]:
    inp = ctx.require_input()
    path = inp if inp.is_file() else _find_batch_file(ctx, inp)
    if path.suffix.lower() != ".jsonl":
        raise schemas.OpError("missing_file: 输入必须是最终样本表 JSONL，收到：%s" % path.name)
    try:
        rows = iofs.read_jsonl(path)
    except ValueError as exc:
        raise schemas.OpError("invalid_jsonl: %s" % exc) from exc
    if not rows:
        raise schemas.OpError("missing_file: 输入样本表没有记录：%s" % path.name)
    return path, rows


def _resolve_media(ctx: OperatorContext, raw) -> Optional[Path]:
    if _is_blank(raw):
        return None
    p = Path(str(raw))
    candidates: List[Path] = []
    if p.is_absolute():
        candidates.append(p)
    else:
        candidates.append(Path(ctx.work_dir) / p)
        candidates.append(ctx.input_dir() / p)
        candidates.append(Path(ctx.work_dir) / "input" / p)
    for candidate in candidates:
        try:
            if candidate.is_file():
                return candidate
        except OSError:  # pragma: no cover
            continue
    return None


def _read_qc_conclusion(ctx: OperatorContext, batch_path: Path, cfg: dict) -> Optional[str]:
    """pack 未显式给 qc_conclusion 时，回读 005_qc_final.json（就近优先）。"""
    name = str(_cfg(cfg, "io.qc_final_filename"))
    candidates: List[Path] = [
        batch_path.parent / name,
        batch_path.parent.parent / name,
        Path(ctx.work_dir) / "tmp" / "005" / name,
        ctx.input_dir() / name,
    ]
    candidates.extend(sorted(Path(ctx.work_dir).rglob(name)))
    for path in candidates:
        try:
            if not path.is_file():
                continue
            payload = iofs.read_json(path)
        except Exception:  # noqa: BLE001 - 结论文件损坏时按未提供处理
            continue
        if not isinstance(payload, dict):
            continue
        for key in ("qc_conclusion", "conclusion"):
            value = payload.get(key)
            if value in QC_VERDICTS:
                return str(value)
    return None


# --------------------------------------------------------------------------- #
# 注册表
# --------------------------------------------------------------------------- #
def _registry_path(ctx: OperatorContext) -> Path:
    return Path(ctx.work_dir) / str(_cfg(ctx.config, "io.registry_filename"))


def _load_registry(ctx: OperatorContext) -> dict:
    path = _registry_path(ctx)
    if not path.is_file():
        return {"task_id": ctx.unique_id, "updated_at": _now_iso(), "versions": []}
    payload = iofs.read_json(path)
    if not isinstance(payload, dict) or not isinstance(payload.get("versions"), list):
        raise schemas.OpError("invalid_jsonl: 版本注册表 %s 内容非法" % path.name)
    return payload


def _save_registry(ctx: OperatorContext, registry: dict) -> Path:
    registry["task_id"] = ctx.unique_id
    registry["updated_at"] = _now_iso()
    return iofs.write_json(_registry_path(ctx), registry)


def _find_version(registry: dict, version_id: str) -> Optional[dict]:
    for entry in registry.get("versions", []):
        if isinstance(entry, dict) and entry.get("version_id") == version_id:
            return entry
    return None


def _latest_version(registry: dict, status: Optional[str] = None) -> Optional[dict]:
    versions = [v for v in registry.get("versions", []) if isinstance(v, dict)]
    if status is not None:
        versions = [v for v in versions if v.get("status") == status]
    if not versions:
        return None
    versions.sort(key=lambda v: (str(v.get("created_at") or ""), str(v.get("version_id") or "")))
    return versions[-1]


def _store_path(ctx: OperatorContext, path: Path) -> str:
    try:
        return path.resolve().relative_to(Path(ctx.work_dir).resolve()).as_posix()
    except ValueError:
        return str(path.resolve())


def _abs_stored_path(ctx: OperatorContext, stored: str) -> Path:
    p = Path(str(stored))
    return p if p.is_absolute() else Path(ctx.work_dir) / p


# --------------------------------------------------------------------------- #
# 版本号 / 版本 ID
# --------------------------------------------------------------------------- #
def _parse_version_tuple(version_no: str) -> Optional[Tuple[int, int]]:
    match = re.match(r"^v(\d+)\.(\d+)$", str(version_no or "").strip())
    return (int(match.group(1)), int(match.group(2))) if match else None


def _next_version_no(registry: dict, version_name: str, cfg: dict) -> str:
    existing: List[Tuple[int, int]] = []
    for entry in registry.get("versions", []):
        if not isinstance(entry, dict) or entry.get("version_name") != version_name:
            continue
        parsed = _parse_version_tuple(str(entry.get("version_no") or ""))
        if parsed is not None:
            existing.append(parsed)
    if not existing:
        return str(_cfg(cfg, "version.first_version_no"))
    major, minor = max(existing)
    return "v%d.%d" % (major, minor + int(_cfg(cfg, "version.minor_step")))


def _collect_used_ids(registry: dict, ctx: Optional[OperatorContext],
                      cfg: dict) -> set:
    """版本号去重用的「已占用集合」。

    版本注册表是按 uniqueId 分开落盘的，光看自己这一份不够：两个任务在同一秒内
    打包，会各自生成 ``VER-<时间戳>-001``，发布后同一个版本号指向两份不同内容，
    前端拿 id 查版本直接串味。所以把同一 data 根下其它任务的注册表也扫进来。
    """
    used = {str(v.get("version_id")) for v in registry.get("versions", [])
            if isinstance(v, dict) and v.get("version_id")}
    if ctx is None:
        return used
    try:
        own = _registry_path(ctx)
        root = own.parent.parent          # <data>/{uniqueId}/006_registry.json
        for path in root.glob("*/" + own.name):
            if path == own:
                continue
            try:
                other = iofs.read_json(path)
            except Exception:             # noqa: BLE001 - 坏文件不该拖垮打包
                continue
            for entry in (other.get("versions") or []):
                if isinstance(entry, dict) and entry.get("version_id"):
                    used.add(str(entry["version_id"]))
    except Exception:                     # noqa: BLE001 - 兜底：至少保证本任务内不重号
        pass
    return used


def _generate_version_id(registry: dict, cfg: dict,
                         ctx: Optional[OperatorContext] = None) -> str:
    prefix = str(_cfg(cfg, "version.id_prefix"))
    stamp = _dt.datetime.now().strftime("%Y%m%d%H%M%S")
    used = _collect_used_ids(registry, ctx, cfg)
    seq = 1
    while True:
        candidate = "%s-%s-%03d" % (prefix, stamp, seq)
        if candidate not in used:
            return candidate
        seq += 1


# --------------------------------------------------------------------------- #
# 版本包
# --------------------------------------------------------------------------- #
def _build_manifest_samples(ctx: OperatorContext, pass_rows: List[dict], cfg: dict) -> Tuple[List[dict], List[Tuple[str, Path]]]:
    audio_dir = str(_cfg(cfg, "version.audio_dir_in_zip")).strip("/")
    samples: List[dict] = []
    audio_items: List[Tuple[str, Path]] = []
    used_names: set = set()
    seen_ids: set = set()

    every = int(_cfg(cfg, "io.progress_every"))
    total = len(pass_rows)
    for idx, row in enumerate(pass_rows, start=1):
        sid = _row_sample_id(row, idx - 1)
        if sid in seen_ids:
            raise schemas.OpError("invalid_jsonl: 样本 sample_id 重复：%s" % sid)
        seen_ids.add(sid)

        raw_audio = row.get("audio_path")
        source = _resolve_media(ctx, raw_audio)
        if source is None:
            raise schemas.OpError(
                "missing_file: 样本 %s 的音频不存在：%s" % (sid, raw_audio)
            )
        digest = A.sha256_file(source)
        size = source.stat().st_size

        stem = _safe_stem(sid)
        ext = source.suffix.lower() or ".wav"
        name = "%s%s" % (stem, ext)
        counter = 2
        while name in used_names:
            name = "%s_%02d%s" % (stem, counter, ext)
            counter += 1
        used_names.add(name)
        entry_name = "%s/%s" % (audio_dir, name)

        samples.append(
            {
                "sample_id": sid,
                "audio_path": entry_name,
                "sha256": digest,
                "bytes": int(size),
            }
        )
        audio_items.append((entry_name, source))
        if every > 0 and (idx % every == 0 or idx == total):
            ctx.log.progress(idx, total)
    return samples, audio_items


def _write_version_zip(
    zip_path: Path,
    manifest: dict,
    batch_rows: List[dict],
    audio_items: List[Tuple[str, Path]],
    cfg: dict,
) -> Path:
    manifest_name = str(_cfg(cfg, "version.manifest_name"))
    batch_name = str(_cfg(cfg, "version.batch_name_in_zip"))
    body = "\n".join(json.dumps(row, ensure_ascii=False) for row in batch_rows) + "\n"
    zip_path.parent.mkdir(parents=True, exist_ok=True)
    with zipfile.ZipFile(str(zip_path), "w", compression=zipfile.ZIP_DEFLATED) as zf:
        zf.writestr(manifest_name, json.dumps(manifest, ensure_ascii=False, indent=2))
        zf.writestr(batch_name, body)
        for entry_name, source in audio_items:
            zf.write(str(source), entry_name)
    return zip_path


def _verify_version_zip(ctx: OperatorContext, zip_path: Path, cfg: dict) -> dict:
    """回读 zip 校验每个样本的 sha256 与字节数（打包完整性指标的唯一来源）。"""
    manifest_name = str(_cfg(cfg, "version.manifest_name"))
    batch_name = str(_cfg(cfg, "version.batch_name_in_zip"))
    max_listed = int(_cfg(cfg, "io.max_listed"))
    checked = passed = 0
    failures: List[dict] = []
    batch_in_zip = False
    manifest_count = None
    try:
        with zipfile.ZipFile(str(zip_path)) as zf:
            names = set(zf.namelist())
            batch_in_zip = batch_name in names
            raw = zf.read(manifest_name)
            manifest = json.loads(raw.decode("utf-8"))
            samples = manifest.get("samples") if isinstance(manifest, dict) else None
            if not isinstance(samples, list):
                raise ValueError("manifest 缺少 samples 数组")
            manifest_count = int(manifest.get("sample_count") or 0)
            for sample in samples:
                checked += 1
                entry_name = str(sample.get("audio_path") or "")
                try:
                    data = zf.read(entry_name)
                except KeyError:
                    failures.append({"sample_id": sample.get("sample_id"), "problem": "audio_entry_missing"})
                    continue
                digest = hashlib.sha256(data).hexdigest()
                if digest == sample.get("sha256") and len(data) == int(sample.get("bytes") or -1):
                    passed += 1
                else:
                    failures.append({"sample_id": sample.get("sample_id"), "problem": "sha256_mismatch"})
    except (zipfile.BadZipFile, KeyError, ValueError, json.JSONDecodeError) as exc:
        ctx.log.error("版本包自检", "无法读取 %s：%s" % (zip_path.name, exc))
        return {
            "checked": checked,
            "passed": passed,
            "rate": 0.0,
            "batch_in_zip": False,
            "manifest_count_matches": False,
            "failures": failures[:max_listed],
            "error": str(exc),
        }

    rate = (float(passed) / float(checked)) if checked else 0.0
    return {
        "checked": int(checked),
        "passed": int(passed),
        "rate": round(rate, 4),
        "batch_in_zip": bool(batch_in_zip),
        "manifest_count_matches": manifest_count == checked,
        "failures": failures[:max_listed],
    }


# --------------------------------------------------------------------------- #
# 版本记录
# --------------------------------------------------------------------------- #
def _version_document(entry: dict, cfg: dict) -> dict:
    """``006_version.json``：§8 契约字段 + manifest 明细（字段只增不改名）。"""
    manifest = entry.get("manifest") or {}
    return {
        "version_id": entry["version_id"],
        "version_name": entry["version_name"],
        "version_no": entry["version_no"],
        "sample_count": int(entry["sample_count"]),
        "source_task": entry.get("source_task"),
        "qc_conclusion": entry.get("qc_conclusion"),
        "status": entry.get("status"),
        "published_at": entry.get("published_at"),
        "created_at": entry.get("created_at"),
        "rolled_back_at": entry.get("rolled_back_at"),
        "visible": entry.get("status") == STATUS_PUBLISHED,
        "manifest": {
            "file": manifest.get("file", str(_cfg(cfg, "version.manifest_name"))),
            "sample_count": int(entry["sample_count"]),
            "zip": dict(entry.get("zip") or {}),
            "verify": dict(manifest.get("verify") or {}),
            "samples": list(manifest.get("samples") or []),
        },
    }


def _write_version_document(ctx: OperatorContext, entry: dict, cfg: dict) -> Path:
    return iofs.write_json(ctx.out(VERSION_FILENAME), _version_document(entry, cfg))


def _entry_summary(entry: dict) -> dict:
    return {
        "version_id": entry.get("version_id"),
        "version_name": entry.get("version_name"),
        "version_no": entry.get("version_no"),
        "sample_count": int(entry.get("sample_count") or 0),
        "status": entry.get("status"),
        "created_at": entry.get("created_at"),
        "published_at": entry.get("published_at"),
    }


def _manifest_sample_map(entry: dict) -> Dict[str, dict]:
    samples = (entry.get("manifest") or {}).get("samples")
    if not isinstance(samples, list):
        raise schemas.OpError(
            "invalid_jsonl: 版本 %s 的 manifest 样本清单缺失" % entry.get("version_no")
        )
    out: Dict[str, dict] = {}
    for sample in samples:
        if isinstance(sample, dict) and not _is_blank(sample.get("sample_id")):
            out[str(sample["sample_id"])] = sample
    return out


# --------------------------------------------------------------------------- #
# action=pack
# --------------------------------------------------------------------------- #
def _do_pack(ctx: OperatorContext, cfg: dict, p: dict) -> dict:
    batch_path, rows = _load_batch(ctx)
    ctx.log.route("质检通过样本固化 → sha256 manifest → 版本包")
    ctx.log.read(len(rows), "条最终样本")

    pass_rows: List[dict] = []
    rejected = 0
    for idx, row in enumerate(rows):
        qc = row.get("qc")
        if not isinstance(qc, dict) or qc.get("status") not in SAMPLE_STATUSES:
            sid = _row_sample_id(row, idx)
            raise schemas.OpError(
                "invalid_jsonl: 样本 %s 缺少合法 qc.status（应为 pass / rejected），请先执行 005 finalize" % sid
            )
        if qc["status"] == SAMPLE_STATUS_PASS:
            pass_rows.append(row)
        else:
            rejected += 1

    if not pass_rows:
        raise schemas.OpError(
            "invalid_jsonl: 批次中没有质检通过（qc.status=pass）的样本，无法打包版本"
        )

    qc_conclusion = p["qc_conclusion"] or _read_qc_conclusion(ctx, batch_path, cfg)
    if qc_conclusion is None:
        qc_conclusion = str(_cfg(cfg, "version.default_qc_conclusion"))
    if qc_conclusion not in QC_VERDICTS:
        raise schemas.OpError("invalid_jsonl: 回读到非法质检结论 %r" % qc_conclusion)

    registry = _load_registry(ctx)
    version_name = str(p["version_name"])
    version_no = p["version_no"] or _next_version_no(registry, version_name, cfg)
    version_id = _generate_version_id(registry, cfg, ctx)
    created_at = _now_iso()

    samples, audio_items = _build_manifest_samples(ctx, pass_rows, cfg)
    manifest = {
        "version_id": version_id,
        "version_name": version_name,
        "version_no": version_no,
        "sample_count": len(samples),
        "source_task": p["source_task"],
        "qc_conclusion": qc_conclusion,
        "status": STATUS_DRAFT,
        "created_at": created_at,
        "samples": samples,
    }

    # 每个版本在 work_dir 留一份权威包（同名版本重复打包不会互相覆盖）；
    # 同时按接口约定在本次输出目录放一份 006_version.zip 交付副本。
    package_dir = Path(ctx.work_dir) / str(_cfg(cfg, "io.package_subdir"))
    package_dir.mkdir(parents=True, exist_ok=True)
    zip_path = package_dir / ("%s.zip" % version_id)
    _write_version_zip(zip_path, manifest, pass_rows, audio_items, cfg)
    verify = _verify_version_zip(ctx, zip_path, cfg)
    if verify["rate"] < 1.0 or not verify["batch_in_zip"] or not verify["manifest_count_matches"]:
        raise schemas.OpError(
            "internal_error: 版本包打包完整性校验未通过（样本 %d/%d，manifest 条数一致=%s）"
            % (verify["passed"], verify["checked"], verify["manifest_count_matches"])
        )
    deliverable_path = ctx.out(str(_cfg(cfg, "version.zip_name")))
    shutil.copyfile(str(zip_path), str(deliverable_path))

    entry = {
        "version_id": version_id,
        "version_name": version_name,
        "version_no": version_no,
        "sample_count": len(samples),
        "source_task": p["source_task"],
        "qc_conclusion": qc_conclusion,
        "status": STATUS_DRAFT,
        "created_at": created_at,
        "published_at": None,
        "rolled_back_at": None,
        "zip": {
            "path": _store_path(ctx, zip_path),
            "bytes": int(zip_path.stat().st_size),
            "sha256": A.sha256_file(zip_path),
        },
        "manifest": {
            "file": str(_cfg(cfg, "version.manifest_name")),
            "samples": samples,
            "verify": {
                "checked": verify["checked"],
                "passed": verify["passed"],
                "rate": verify["rate"],
                "batch_in_zip": verify["batch_in_zip"],
                "manifest_count_matches": verify["manifest_count_matches"],
            },
        },
    }
    registry.setdefault("versions", []).append(entry)
    _save_registry(ctx, registry)
    version_path = _write_version_document(ctx, entry, cfg)

    statistics = {
        "input_samples": len(rows),
        "packed_samples": len(samples),
        "excluded_rejected": rejected,
        "version_no": version_no,
        "manifest_samples": len(samples),
        "manifest_verify_checked": verify["checked"],
        "manifest_verify_passed": verify["passed"],
        "manifest_verify_rate": verify["rate"],
        "zip_bytes": entry["zip"]["bytes"],
    }

    ctx.log.result(
        "版本打包",
        output=len(samples),
        success=len(samples),
        skipped=rejected,
        extra={"版本": version_no, "manifest 校验": "%d/%d" % (verify["passed"], verify["checked"])},
    )
    if rejected:
        ctx.log.warning("%d 条剔除样本不计入版本（qc.status=rejected）" % rejected)
    ctx.log.write_output(deliverable_path.name)
    ctx.log.write_output(version_path.name)

    return {
        "outputs": {"version_zip": deliverable_path, "version": version_path},
        "statistics": statistics,
        "result": {
            "action": "pack",
            "version_id": version_id,
            "version_name": version_name,
            "version_no": version_no,
            "sample_count": len(samples),
            "qc_conclusion": qc_conclusion,
            "status": STATUS_DRAFT,
            "manifest_verify_rate": verify["rate"],
        },
        "status": "success",
    }


# --------------------------------------------------------------------------- #
# action=publish
# --------------------------------------------------------------------------- #
def _do_publish(ctx: OperatorContext, cfg: dict, p: dict) -> dict:
    registry = _load_registry(ctx)
    ctx.log.route("发布版本 → 状态校验 → 对外可见")

    version_id = p["version_id"]
    entry = _find_version(registry, version_id) if version_id else _latest_version(registry, STATUS_DRAFT)
    if entry is None:
        raise schemas.OpError("version_not_found: 待发布版本不存在：%s" % (version_id or "（无草稿版本）"))

    version_no = str(entry.get("version_no"))
    if entry.get("status") != STATUS_DRAFT:
        raise schemas.OpError(
            "invalid_params: 版本 %s 当前状态为「%s」，只允许从「草稿」发布"
            % (version_no, entry.get("status"))
        )

    effective_qc = p["qc_conclusion"] or entry.get("qc_conclusion")
    if effective_qc != QC_PASS:
        raise schemas.OpError(
            "qc_not_passed: 版本 %s 质检结论为「%s」，未通过质检不允许发布"
            % (version_no, effective_qc)
        )

    zip_info = entry.get("zip") or {}
    zip_path = _abs_stored_path(ctx, str(zip_info.get("path") or ""))
    if not zip_path.is_file():
        raise schemas.OpError("missing_file: 版本 %s 的版本包不存在：%s" % (version_no, zip_path.name))
    if A.sha256_file(zip_path) != zip_info.get("sha256"):
        raise schemas.OpError(
            "invalid_params: 版本 %s 的版本包 sha256 与注册表不一致，拒绝发布" % version_no
        )

    entry["qc_conclusion"] = effective_qc
    entry["status"] = STATUS_PUBLISHED
    entry["published_at"] = p["published_at"] or _now_iso()
    _save_registry(ctx, registry)
    version_path = _write_version_document(ctx, entry, cfg)

    statistics = {
        "version_id": entry["version_id"],
        "version_no": version_no,
        "sample_count": int(entry["sample_count"]),
        "status": entry["status"],
    }
    ctx.log.result(
        "版本发布",
        output=int(entry["sample_count"]),
        success=int(entry["sample_count"]),
        extra={"版本": version_no, "状态": entry["status"]},
    )
    ctx.log.write_output(version_path.name)

    return {
        "outputs": {"version": version_path},
        "statistics": statistics,
        "result": {
            "action": "publish",
            "version_id": entry["version_id"],
            "version_no": version_no,
            "status": entry["status"],
            "published_at": entry["published_at"],
            "sample_count": int(entry["sample_count"]),
        },
        "status": "success",
    }


# --------------------------------------------------------------------------- #
# action=rollback
# --------------------------------------------------------------------------- #
def _do_rollback(ctx: OperatorContext, cfg: dict, p: dict) -> dict:
    registry = _load_registry(ctx)
    ctx.log.route("回滚版本 → 只改状态，不复制不删除文件")

    entry = _find_version(registry, p["version_id"])
    if entry is None:
        raise schemas.OpError("version_not_found: 版本不存在：%s" % p["version_id"])
    version_no = str(entry.get("version_no"))
    if entry.get("status") != STATUS_PUBLISHED:
        raise schemas.OpError(
            "invalid_params: 版本 %s 当前状态为「%s」，只允许回滚「已发布」版本"
            % (version_no, entry.get("status"))
        )

    entry["status"] = STATUS_ROLLED_BACK
    entry["rolled_back_at"] = _now_iso()
    _save_registry(ctx, registry)
    version_path = _write_version_document(ctx, entry, cfg)

    statistics = {
        "version_id": entry["version_id"],
        "version_no": version_no,
        "sample_count": int(entry["sample_count"]),
        "status": entry["status"],
    }
    ctx.log.result(
        "版本回滚",
        output=int(entry["sample_count"]),
        success=int(entry["sample_count"]),
        extra={"版本": version_no, "状态": entry["status"]},
    )
    ctx.log.write_output(version_path.name)

    return {
        "outputs": {"version": version_path},
        "statistics": statistics,
        "result": {
            "action": "rollback",
            "version_id": entry["version_id"],
            "version_no": version_no,
            "status": entry["status"],
            "rolled_back_at": entry["rolled_back_at"],
            "sample_count": int(entry["sample_count"]),
        },
        "status": "success",
    }


# --------------------------------------------------------------------------- #
# action=compare
# --------------------------------------------------------------------------- #
def _resolve_compare_pair(registry: dict, target_id: Optional[str], baseline_id: Optional[str]) -> Tuple[dict, dict]:
    versions = [v for v in registry.get("versions", []) if isinstance(v, dict)]

    if not target_id and not baseline_id:
        published = [v for v in versions if v.get("status") == STATUS_PUBLISHED]
        published.sort(
            key=lambda v: (str(v.get("published_at") or v.get("created_at") or ""), str(v.get("version_id")))
        )
        if len(published) < 2:
            raise schemas.OpError(
                "version_not_found: 可比较的「已发布」版本不足 2 个（当前 %d 个）" % len(published)
            )
        return published[-2], published[-1]

    if target_id and baseline_id:
        baseline = _find_version(registry, baseline_id)
        target = _find_version(registry, target_id)
        if target is None:
            raise schemas.OpError("version_not_found: 目标版本不存在：%s" % target_id)
        if baseline is None:
            raise schemas.OpError("version_not_found: 对比版本不存在：%s" % baseline_id)
        return baseline, target

    if target_id:
        target = _find_version(registry, target_id)
        if target is None:
            raise schemas.OpError("version_not_found: 目标版本不存在：%s" % target_id)
        candidates = [
            v
            for v in versions
            if v.get("status") == STATUS_PUBLISHED
            and v.get("version_id") != target.get("version_id")
            and str(v.get("created_at") or "") <= str(target.get("created_at") or "")
        ]
        candidates.sort(key=lambda v: (str(v.get("created_at") or ""), str(v.get("version_id"))))
        if not candidates:
            raise schemas.OpError(
                "version_not_found: 版本 %s 之前没有可对比的已发布版本，请显式传 compare_with"
                % target.get("version_no")
            )
        return candidates[-1], target

    baseline = _find_version(registry, baseline_id)
    if baseline is None:
        raise schemas.OpError("version_not_found: 对比版本不存在：%s" % baseline_id)
    target = _latest_version(registry, STATUS_PUBLISHED)
    if target is None or target.get("version_id") == baseline.get("version_id"):
        raise schemas.OpError("version_not_found: 没有可对比的已发布目标版本")
    return baseline, target


def _do_compare(ctx: OperatorContext, cfg: dict, p: dict) -> dict:
    registry = _load_registry(ctx)
    ctx.log.route("版本对比 → 样本清单差异")
    ctx.log.read(len(registry.get("versions", [])), "个版本记录")

    baseline, target = _resolve_compare_pair(registry, p["version_id"], p["compare_with"])
    if baseline.get("version_id") == target.get("version_id"):
        raise schemas.OpError("invalid_params: 对比版本不能与目标版本相同")

    a_samples = _manifest_sample_map(baseline)
    b_samples = _manifest_sample_map(target)
    a_ids = set(a_samples)
    b_ids = set(b_samples)
    added = sorted(b_ids - a_ids)
    removed = sorted(a_ids - b_ids)
    common = sorted(a_ids & b_ids)
    changed = sorted(
        sid for sid in common if a_samples[sid].get("sha256") != b_samples[sid].get("sha256")
    )

    max_listed = int(_cfg(cfg, "io.max_listed"))
    compare = {
        "compare_at": _now_iso(),
        "version_a": _entry_summary(baseline),
        "version_b": _entry_summary(target),
        "sample_count_diff": int(target["sample_count"]) - int(baseline["sample_count"]),
        "added_count": len(added),
        "removed_count": len(removed),
        "sha256_diff_count": len(changed),
        "common_count": len(common),
        "identical_count": len(common) - len(changed),
        "added_samples": added[:max_listed],
        "removed_samples": removed[:max_listed],
        "changed_samples": changed[:max_listed],
        "truncated": {
            "added": max(0, len(added) - max_listed),
            "removed": max(0, len(removed) - max_listed),
            "changed": max(0, len(changed) - max_listed),
        },
        "note": "只读对比；默认取最近两个「已发布」版本，回滚只改状态不改样本",
    }
    compare_path = iofs.write_json(ctx.out(COMPARE_FILENAME), compare)

    statistics = {
        "baseline_version": baseline["version_id"],
        "target_version": target["version_id"],
        "baseline_samples": int(baseline["sample_count"]),
        "target_samples": int(target["sample_count"]),
        "sample_count_diff": compare["sample_count_diff"],
        "added": len(added),
        "removed": len(removed),
        "sha256_diff": len(changed),
    }
    ctx.log.result(
        "版本对比",
        output=len(common) + len(added) + len(removed),
        extra={"新增": len(added), "删除": len(removed), "内容变化": len(changed)},
    )
    ctx.log.write_output(compare_path.name)

    return {
        "outputs": {"compare": compare_path},
        "statistics": statistics,
        "result": {
            "action": "compare",
            "version_a": baseline["version_id"],
            "version_b": target["version_id"],
            "sample_count_diff": compare["sample_count_diff"],
            "added_count": compare["added_count"],
            "removed_count": compare["removed_count"],
            "sha256_diff_count": compare["sha256_diff_count"],
        },
        "status": "success",
    }


# --------------------------------------------------------------------------- #
# 入口
# --------------------------------------------------------------------------- #
def run(ctx: OperatorContext) -> dict:
    ctx.log.start()
    cfg = ctx.config
    p = _parse_params(ctx.params or {}, cfg)
    if p["action"] == "pack":
        result = _do_pack(ctx, cfg, p)
    elif p["action"] == "publish":
        result = _do_publish(ctx, cfg, p)
    elif p["action"] == "rollback":
        result = _do_rollback(ctx, cfg, p)
    else:
        result = _do_compare(ctx, cfg, p)
    ctx.log.done()
    return result


__all__ = ["run", "OPERATOR_ID", "OPERATOR_NAME"]
