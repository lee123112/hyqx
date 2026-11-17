# -*- coding: utf-8 -*-
"""01 语料入库匹配校验（avp_ingest_corpus）。

职责
----
把用户上传的**音频**与**参考文本**配对成带 uttid 的语料表，并按三项内置规则做入库检测。

业务硬口径（任务书 §1 + 原型《素材库 · 导入与清单说明》）
------------------------------------------------------------
- uttid = 音频文件名去掉扩展名，**大小写不敏感**，精确匹配，不做模糊匹配；
- ZIP 解压后保留目录结构，取**文件名**作为 uttid；不同目录下同名文件视为**重复**；
- 缺文本 / 文本为空或仅空白 → 该音频**不入库**，计入「缺文本」并列出文件名；
- 文本多余（清单有、音频无）→ **只提示**，不阻断；
- 重复 uttid → **阻断创建**（抛 ``duplicate_uttid``）；
- 入库检测三项（时长 < 阈值 / 空音频 / 文本完全重复）→ **只提示不阻断**，结果写进报告。

两种动作（``params.action``）
-----------------------------
``ingest``  默认。扫音频 + 配文本 + 检测 → 写 ``001_corpus.jsonl`` / ``001_rejects.jsonl`` / 报告；
``recheck`` 手动重新检测。**只读已入库的 001_corpus.jsonl 重跑三项检测**，就地更新报告里的
            ``checks`` 与相关计数，不重扫音频、不改入库结果、不动被拒清单——对应任务书 §1
            「结果只在详情展示，可手动重新检测」。

技术方案
--------
纯规则 + 一段轻量能量检测，无模型。解码用 ``soundfile``（libsndfile），
逐条取 ``file_info`` 拿采样率/声道/格式/时长，再对需要跑空音频检测的样本做一次
单声道解码算 RMS。全程 O(n)，无随机，可复现。
"""

from __future__ import annotations

import datetime as _dt
import sys
from pathlib import Path
from typing import Dict, List, Optional, Tuple

_ROOT = Path(__file__).resolve().parent.parent
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))

from common import audio as A  # noqa: E402
from common import iofs  # noqa: E402
from common import schemas  # noqa: E402
from common.context import OperatorContext  # noqa: E402

OPERATOR_ID = "avp_ingest_corpus"
OPERATOR_NAME = "语料入库匹配校验"

DATASET_SOURCES = ("自主采集", "公开采购", "合作方提供", "业务回流")
MANIFEST_MODES = ("none", "file")
DELIMITERS = ("auto", "tab", "comma", "space")
INGEST_ACTIONS = ("ingest", "recheck")


# --------------------------------------------------------------------------- #
# 参数
# --------------------------------------------------------------------------- #
def _parse_params(params: dict, cfg: dict) -> dict:
    name = schemas.as_str(params.get("dataset_name"), "dataset_name", max_len=50)
    lo = int(cfg["dataset"]["name_min_len"])
    if len(name) < lo:
        raise schemas.ParamError("dataset_name 长度不能少于 %d 个字符" % lo)

    source = schemas.enum(params.get("source"), DATASET_SOURCES, "source")
    mode = str(params.get("manifest_mode") or "none").strip().lower()
    if mode not in MANIFEST_MODES:
        raise schemas.ParamError("manifest_mode 取值 %r 非法，允许：%s" % (mode, " / ".join(MANIFEST_MODES)))

    manifest_path = params.get("manifest_path")
    if mode == "file" and not manifest_path:
        raise schemas.ParamError("manifest_mode=file 时必须提供 manifest_path")

    delimiter = str(params.get("delimiter") or "auto").strip().lower()
    if delimiter not in DELIMITERS:
        raise schemas.ParamError("delimiter 取值 %r 非法，允许：%s" % (delimiter, " / ".join(DELIMITERS)))

    id_column = params.get("id_column")
    if id_column is not None:
        id_column = schemas.as_int(id_column, "id_column", lo=0)

    text_column = params.get("text_column")
    if text_column is not None:
        if isinstance(text_column, (list, tuple)):
            text_column = [schemas.as_int(x, "text_column", lo=0) for x in text_column]
        else:
            text_column = schemas.as_int(text_column, "text_column", lo=0)

    exts = params.get("audio_exts")
    if exts:
        exts = [str(e).strip().lower() for e in schemas.as_list(exts, "audio_exts")]
        for e in exts:
            if not e.startswith("."):
                raise schemas.ParamError("audio_exts 每项必须以 . 开头，收到 %r" % e)
    else:
        exts = list(cfg["io"]["audio_exts"])

    dataset_id = params.get("dataset_id") or _auto_dataset_id(cfg)

    return {
        "dataset_name": name,
        "source": source,
        "dataset_id": str(dataset_id),
        "manifest_mode": mode,
        "manifest_path": manifest_path,
        "id_column": id_column,
        "text_column": text_column,
        "delimiter": delimiter,
        "audio_exts": tuple(exts),
        "allow_gbk": schemas.as_bool(params.get("check_allow_gbk"), "check_allow_gbk", default=False),
        "min_duration": schemas.as_float(
            params.get("check_min_duration", cfg["checks"]["min_duration_sec"]),
            "check_min_duration",
            lo=0.0,
        ),
    }


def _auto_dataset_id(cfg: dict) -> str:
    prefix = str(cfg["dataset"].get("id_prefix", "DS"))
    stamp = _dt.datetime.now().strftime("%Y%m%d%H%M%S")
    return "%s-%s" % (prefix, stamp)


# --------------------------------------------------------------------------- #
# 主流程
# --------------------------------------------------------------------------- #
def _run_recheck(ctx: OperatorContext, cfg: dict) -> dict:
    """手动重新检测：只读已入库语料重跑三项检查，报告就地更新，不动入库结果。"""
    params = ctx.params or {}
    max_listed = int(cfg["io"]["max_listed"])
    min_duration = schemas.as_float(
        params.get("check_min_duration", cfg["checks"]["min_duration_sec"]),
        "check_min_duration",
        lo=0.0,
    )
    normalize = bool(cfg["checks"].get("normalize_text", True))
    run_empty = bool(cfg["checks"].get("empty_audio", True))
    run_dup = bool(cfg["checks"].get("duplicate_text", True))
    silence = float(cfg["audio"]["silence_rms"])

    corpus_path = ctx.out("001_corpus.jsonl")
    report_path = ctx.out("001_ingest_report.json")
    if not corpus_path.is_file():
        raise schemas.OpError(
            "missing_file: 没有 %s，先跑 action=ingest 入库再重检" % corpus_path.name
        )

    ctx.log.start()
    ctx.log.route("手动重新检测 → 复跑三项入库检查")
    rows = iofs.read_jsonl(corpus_path)
    ctx.log.read(len(rows), "条已入库语料")

    short_audio: List[dict] = []
    empty_audio: List[dict] = []
    unreadable: List[dict] = []
    text_groups: Dict[str, List[str]] = {}

    for row in rows:
        uttid = str(row.get("uttid") or "")
        text = str(row.get("text") or "")
        raw_path = str(row.get("audio_path") or "")
        path = Path(raw_path)
        if not raw_path or not path.is_file():
            unreadable.append({"uttid": uttid, "file": path.name, "reason": "audio_not_found"})
            continue
        try:
            info = A.file_info(path)
        except A.AudioDecodeError as exc:
            unreadable.append({"uttid": uttid, "file": path.name, "reason": str(exc)})
            continue

        if float(info["duration"]) < min_duration:
            short_audio.append({"uttid": uttid, "duration": info["duration"],
                                "threshold": min_duration, "problem": "短音频"})
        if run_empty:
            try:
                y, _sr = A.load_mono(path)
                if A.is_silent(y, silence):
                    empty_audio.append({"uttid": uttid, "rms": round(A.rms(y), 8),
                                        "threshold": silence, "problem": "空音频"})
            except A.AudioDecodeError:
                unreadable.append({"uttid": uttid, "file": path.name,
                                   "reason": "decode_failed_on_checks"})
        if run_dup:
            key = iofs.normalize_text(text) if normalize else text
            text_groups.setdefault(key, []).append(uttid)

    duplicate_text = [
        {"text": key[:80], "uttids": uttids, "count": len(uttids)}
        for key, uttids in text_groups.items()
        if len(uttids) > 1
    ]

    now = _dt.datetime.now().isoformat(timespec="seconds")
    report = iofs.read_json(Path(report_path)) if Path(report_path).is_file() else {}
    checks = dict(report.get("checks") or {})
    checks.update({
        "rules": ["时长<%.2fs" % min_duration, "空音频", "文本完全重复"],
        "note": "入库检测只提示不阻断，通过与否都不影响数据集被生产任务引用",
        "short_audio": short_audio[:max_listed],
        "empty_audio": empty_audio[:max_listed],
        "duplicate_text": duplicate_text[:max_listed],
        "unreadable": unreadable[:max_listed],
        "rechecked_at": now,
        "recheck_note": "手动重新检测：只复跑三项检查，未重扫音频、未改写入库结果",
    })
    report["checks"] = checks
    stats = dict(report.get("statistics") or {})
    stats.update({
        "input_records": len(rows),
        "short_audio": len(short_audio),
        "empty_audio": len(empty_audio),
        "duplicate_text_groups": len(duplicate_text),
        "unreadable": len(unreadable),
    })
    report["statistics"] = stats
    report["rechecked"] = {"at": now, "rows": len(rows), "corpus": corpus_path.name}
    iofs.write_json(Path(report_path), report)
    ctx.log.write_output(Path(report_path).name)

    summary = {
        "short_audio": len(short_audio),
        "empty_audio": len(empty_audio),
        "duplicate_text_groups": len(duplicate_text),
        "unreadable": len(unreadable),
    }
    ctx.log.result("重新检测完成", output=len(rows), success=len(rows))
    return {
        "outputs": {"report": Path(report_path)},
        "statistics": dict(summary, action="recheck", input_records=len(rows)),
        "result": {
            "action": "recheck",
            "corpus": str(corpus_path),
            "checks": summary,
            "rechecked_at": now,
            "note": "只更新检测结果，入库清单与拒收清单未改动",
        },
        "status": "success",
    }


def run(ctx: OperatorContext) -> dict:
    cfg = ctx.config
    raw_action = (ctx.params or {}).get("action")
    action = str(raw_action or "ingest").strip().lower()
    if action not in INGEST_ACTIONS:
        raise schemas.ParamError(
            "action 取值 %r 非法，允许：%s" % (raw_action, " / ".join(INGEST_ACTIONS))
        )
    if action == "recheck":
        return _run_recheck(ctx, cfg)

    p = _parse_params(ctx.params or {}, cfg)
    root = ctx.input_dir()
    max_listed = int(cfg["io"]["max_listed"])

    ctx.log.start()
    ctx.log.route("清单解析 → uttid 匹配 → 入库检测")

    # 1) 扫描音频 ----------------------------------------------------------- #
    audio_files = iofs.list_audio_files(root, exts=p["audio_exts"])
    if not audio_files:
        raise schemas.OpError("missing_file: 未在输入中找到音频（支持 %s）" % "/".join(p["audio_exts"]))
    ctx.log.read(len(audio_files), "条音频")

    # 2) uttid 归一化 + 重复检测（重复即阻断） ------------------------------- #
    by_uttid: Dict[str, List[Path]] = {}
    for path in audio_files:
        by_uttid.setdefault(iofs.norm_uttid(path.name), []).append(path)
    duplicates = {u: [pp.name for pp in ps] for u, ps in by_uttid.items() if len(ps) > 1}
    if duplicates:
        preview = "; ".join("%s(%s)" % (u, ",".join(v)) for u, v in list(duplicates.items())[:5])
        ctx.log.warning("%d 个 uttid 重复，阻断创建" % len(duplicates))
        raise schemas.OpError(
            "duplicate_uttid: %d 个 uttid 重复，请先修清单或重命名文件：%s" % (len(duplicates), preview)
        )

    # 3) 参考文本 ----------------------------------------------------------- #
    manifest_meta: dict = {}
    if p["manifest_mode"] == "file":
        manifest_file = _resolve_manifest(ctx, p["manifest_path"])
        ctx.log.load_input(manifest_file.name)
        try:
            text_map, manifest_meta = iofs.parse_manifest(
                manifest_file,
                id_column=p["id_column"],
                text_column=p["text_column"],
                delimiter=p["delimiter"],
                allow_gbk=p["allow_gbk"],
            )
        except iofs.ManifestError as exc:
            raise schemas.OpError(str(exc)) from exc
        manifest_meta["mode"] = "file"
        ctx.log.read(len(text_map), "条参考文本")
    else:
        text_map, manifest_meta = iofs.parse_same_name_txt(root)
        manifest_meta["mode"] = "same_name_txt"
        ctx.log.read(len(text_map), "条同名文本")

    # 4) 匹配 --------------------------------------------------------------- #
    normalize = bool(cfg["checks"].get("normalize_text", True))
    matched: List[dict] = []
    rejects: List[dict] = []
    unreadable: List[dict] = []
    short_audio: List[dict] = []
    empty_audio: List[dict] = []
    text_groups: Dict[str, List[str]] = {}

    created_at = _dt.datetime.now().astimezone().isoformat(timespec="seconds")
    total = len(by_uttid)
    for idx, (uttid, paths) in enumerate(sorted(by_uttid.items()), start=1):
        if idx % 200 == 0 or idx == total:
            ctx.log.progress(idx, total)

        path = paths[0]
        raw_text = text_map.get(uttid)
        text = (raw_text or "").strip()
        if not text:
            rejects.append(
                {
                    "uttid": uttid,
                    "file": path.name,
                    "reason": "missing_text",
                    "detail": "清单无对应 id 或文本为空" if raw_text is None else "文本仅含空白",
                }
            )
            continue

        try:
            info = A.file_info(path)
        except A.AudioDecodeError as exc:
            unreadable.append({"uttid": uttid, "file": path.name, "reason": str(exc)})
            continue

        record = {
            "dataset_id": p["dataset_id"],
            "dataset_name": p["dataset_name"],
            "source": p["source"],
            "uttid": uttid,
            "audio_path": str(path),
            "text": text,
            "duration": info["duration"],
            "sample_rate": info["sample_rate"],
            "channels": info["channels"],
            "format": str(info["format"]).lower(),
            "created_at": created_at,
        }
        matched.append(record)

        # 检测一：短音频（只提示）
        if info["duration"] < p["min_duration"]:
            short_audio.append(
                {"uttid": uttid, "duration": info["duration"], "threshold": p["min_duration"], "problem": "短音频"}
            )
        # 检测二：空音频（只提示）——需要解码算 RMS
        if cfg["checks"].get("empty_audio", True):
            try:
                y, _sr = A.load_mono(path)
                if A.is_silent(y, float(cfg["audio"]["silence_rms"])):
                    empty_audio.append(
                        {
                            "uttid": uttid,
                            "rms": round(A.rms(y), 8),
                            "threshold": float(cfg["audio"]["silence_rms"]),
                            "problem": "空音频",
                        }
                    )
            except A.AudioDecodeError:
                unreadable.append({"uttid": uttid, "file": path.name, "reason": "decode_failed_on_checks"})
        # 检测三：文本完全重复
        if cfg["checks"].get("duplicate_text", True):
            key = iofs.normalize_text(text) if normalize else text
            text_groups.setdefault(key, []).append(uttid)

    duplicate_text = [
        {"text": key[:80], "uttids": uttids, "count": len(uttids)}
        for key, uttids in text_groups.items()
        if len(uttids) > 1
    ]

    # 5) 文本多余（只提示） -------------------------------------------------- #
    extra_text = sorted(set(text_map.keys()) - set(by_uttid.keys()))

    if not matched:
        raise schemas.OpError(
            "missing_text: 所有音频都没有匹配到参考文本（音频 %d 条，文本 %d 条）"
            % (len(by_uttid), len(text_map))
        )

    # 6) 落盘 --------------------------------------------------------------- #
    corpus_path = iofs.write_jsonl(ctx.out("001_corpus.jsonl"), matched)
    rejects_path = iofs.write_jsonl(ctx.out("001_rejects.jsonl"), rejects)

    statistics = {
        "audio_files": len(audio_files),
        "input_records": total,
        "manifest_ids": len(text_map),
        "matched": len(matched),
        "missing_text": len(rejects),
        "extra_text": len(extra_text),
        "unreadable": len(unreadable),
        "short_audio": len(short_audio),
        "empty_audio": len(empty_audio),
        "duplicate_text_groups": len(duplicate_text),
        "total_duration_sec": round(sum(r["duration"] for r in matched), 3),
        "avg_duration_sec": round(
            sum(r["duration"] for r in matched) / float(len(matched)), 3
        ),
    }

    report = {
        "dataset_id": p["dataset_id"],
        "dataset_name": p["dataset_name"],
        "source": p["source"],
        "created_at": created_at,
        "manifest": manifest_meta,
        "statistics": statistics,
        "checks": {
            "rules": ["时长<%.2fs" % p["min_duration"], "空音频", "文本完全重复"],
            "note": "入库检测只提示不阻断，通过与否都不影响数据集被生产任务引用",
            "short_audio": short_audio[:max_listed],
            "empty_audio": empty_audio[:max_listed],
            "duplicate_text": duplicate_text[:max_listed],
            "extra_text": extra_text[:max_listed],
            "unreadable": unreadable[:max_listed],
            "truncated": {
                "short_audio": max(0, len(short_audio) - max_listed),
                "empty_audio": max(0, len(empty_audio) - max_listed),
                "duplicate_text": max(0, len(duplicate_text) - max_listed),
                "extra_text": max(0, len(extra_text) - max_listed),
                "unreadable": max(0, len(unreadable) - max_listed),
            },
        },
        "rejects": rejects[:max_listed],
        "conclusion": "partial" if (rejects or unreadable) else "success",
    }
    report_path = iofs.write_json(ctx.out("001_ingest_report.json"), report)

    ctx.log.write_output(corpus_path.name)
    ctx.log.write_output(report_path.name)
    status = "partial" if (rejects or unreadable) else "success"
    if status == "partial":
        ctx.log.partial(
            "部分音频未入库",
            "入库 %d 条，缺文本 %d 条，无法解码 %d 条" % (len(matched), len(rejects), len(unreadable)),
        )
    else:
        ctx.log.result("语料入库", output=len(matched), success=len(matched))
    if extra_text:
        ctx.log.warning("%d 条清单文本没有对应音频，仅提示不阻断" % len(extra_text))
    if short_audio or empty_audio or duplicate_text:
        ctx.log.warning(
            "入库检测有问题项（不阻断入库）",
            "短音频 %d、空音频 %d、文本重复组 %d"
            % (len(short_audio), len(empty_audio), len(duplicate_text)),
        )
    ctx.log.done()

    return {
        "outputs": {"corpus": corpus_path, "rejects": rejects_path, "report": report_path},
        "statistics": statistics,
        "result": {
            "dataset_id": p["dataset_id"],
            "dataset_name": p["dataset_name"],
            "source": p["source"],
            "sample_count": len(matched),
            "conclusion": status,
            "checks": {
                "short_audio": len(short_audio),
                "empty_audio": len(empty_audio),
                "duplicate_text_groups": len(duplicate_text),
            },
        },
        "status": status,
    }


def _resolve_manifest(ctx: OperatorContext, raw: str) -> Path:
    candidate = Path(str(raw))
    if not candidate.is_absolute():
        candidate = (Path(ctx.work_dir) / candidate).resolve()
    if not candidate.is_file():
        # 兼容：只给了文件名，去输入目录里找
        alt = ctx.input_dir() / Path(str(raw)).name
        if alt.is_file():
            return alt
        raise schemas.OpError("missing_file: 清单不存在 %s" % raw)
    return candidate


__all__ = ["run", "OPERATOR_ID", "OPERATOR_NAME"]
