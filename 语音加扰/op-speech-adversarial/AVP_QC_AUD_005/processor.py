# -*- coding: utf-8 -*-
"""05 样本质检（avp_inspect_batch）。

职责
----
判断一个批次能不能发布成样本版本：先做**全量自动检查**，再按比例抽样生成
**人工试听复核队列**，复核判定回填后产出最终样本表与质检结论。

业务硬口径（任务书 §4 + 原型《生产任务 · 研发标注》）
-------------------------------------------------------
- 自动检查**固定只做两项**：完整性（可解码、非空、时长 > 0、字段齐全）、
  信噪比（低于阈值判存疑）；两项全过才允许出最终结论。
- 抽样条数 = ``max(min_samples, ceil(N * sample_ratio))``，默认 5% / 最少 20 条；
  实际队列不得超过批次样本数（N 小于最少条数时只能全量入队）。
- 复核未判定完**不能**提交（``human_review_incomplete``）；剔除样本不计入版本。
- 加扰「可听性」只能人工判定，算子**绝不**自动评价音频好不好听。
- 有干净参考时 SNR 必须用 :func:`common.audio.snr_db` 实测并标 ``measured``；
  无参考才用 :func:`common.audio.estimate_snr_db` 盲估并标 ``estimated``。

两个动作（``params.mode``）
---------------------------
``start``
    全量自动检查 + 抽样 → ``005_qc_report.json`` + ``005_review_queue.jsonl``；
    结论为「待复核」时用 ``ctx.log.waiting()``，不写成功。
``finalize``
    回填 ``params.review_decisions``（``[{"sample_id", "decision": pass|reject}]``）
    → ``005_batch_final.jsonl``（003/004 字段 + ``qc``）+ ``005_qc_final.json``。

技术方案
--------
纯 DSP + 规则，无模型、无随机分数：

1. 逐条解码（soundfile / libsndfile）拿采样率与实际时长，同时作为「可解码 / 非空 /
   时长 > 0」的完整性证据；
2. 加扰样本若带干净参考路径，则 ``noise = mixed - clean``（重采样到同一采样率、
   截到等长），按 ``10*log10(P_sig/P_noise)`` 实测；无参考时用帧能量最小值统计盲估；
3. 抽样用 ``numpy.random.default_rng(seed)``，且**优先把信噪比存疑样本放入队列**，
   剩余名额再按种子随机补齐，保证同输入同种子结果一致；
4. 复核判定只做规则回填：缺判定即报错，绝不替人做「可听性」判断。
"""

from __future__ import annotations

import datetime as _dt
import math
import sys
from pathlib import Path
from typing import Dict, List, Optional, Tuple

import numpy as np

_ROOT = Path(__file__).resolve().parent.parent
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))

from common import audio as A  # noqa: E402
from common import config as CFG  # noqa: E402
from common import iofs  # noqa: E402
from common import schemas  # noqa: E402
from common.context import OperatorContext  # noqa: E402

OPERATOR_ID = "avp_inspect_batch"
OPERATOR_NAME = "样本质检"

MODES = ("start", "finalize")
DECISIONS = ("pass", "reject")

CONCLUSION_PENDING = "待复核"
CONCLUSION_PASS = "通过"
CONCLUSION_FAIL = "不通过"

# 完整性问题的机器码 → 报告里的可读文案
PROBLEM_LABELS = {
    "missing_fields": "字段不全",
    "audio_missing": "音频文件不存在",
    "decode_failed": "音频无法解码",
    "empty_audio": "空音频",
    "zero_duration": "实际时长不大于 0",
}

STATE_FILENAME = "005_autocheck.json"
REPORT_FILENAME = "005_qc_report.json"
QUEUE_FILENAME = "005_review_queue.jsonl"
BATCH_FINAL_FILENAME = "005_batch_final.jsonl"
QC_FINAL_FILENAME = "005_qc_final.json"


# --------------------------------------------------------------------------- #
# 通用小工具
# --------------------------------------------------------------------------- #
def _now_iso() -> str:
    return _dt.datetime.now().astimezone().isoformat(timespec="seconds")


def _cfg(cfg: dict, dotted: str):
    """全部阈值/系数从 config.yaml 取，缺键直接 fail fast。"""
    return CFG.get(cfg, dotted)


def _is_blank(value) -> bool:
    if value is None:
        return True
    if isinstance(value, str):
        return not value.strip()
    if isinstance(value, (dict, list, tuple)):
        return len(value) == 0
    return False


def _sample_size(total: int, ratio: float, minimum: int) -> int:
    """抽样条数公式：``max(min_samples, ceil(N * sample_ratio))``。"""
    return max(int(minimum), int(math.ceil(float(total) * float(ratio))))


def _row_sample_id(row: dict, index: int) -> str:
    raw = row.get("sample_id")
    if _is_blank(raw):
        raw = row.get("source_uttid")
    if _is_blank(raw):
        return "row_%06d" % int(index)
    return str(raw).strip()


# --------------------------------------------------------------------------- #
# 参数
# --------------------------------------------------------------------------- #
def _parse_decisions(raw) -> Dict[str, str]:
    items = schemas.as_list(raw, "review_decisions")
    out: Dict[str, str] = {}
    for idx, item in enumerate(items):
        if not isinstance(item, dict):
            raise schemas.ParamError("review_decisions[%d] 必须是对象" % idx)
        sid = schemas.as_str(item.get("sample_id"), "review_decisions[%d].sample_id" % idx, max_len=200)
        decision = str(item.get("decision") or "").strip().lower()
        if decision not in DECISIONS:
            raise schemas.ParamError(
                "review_decisions[%d].decision 取值 %r 非法，允许：pass / reject" % (idx, decision)
            )
        if sid in out:
            raise schemas.ParamError("review_decisions 中 sample_id 重复：%s" % sid)
        out[sid] = decision
    return out


def _parse_params(params: dict, cfg: dict) -> dict:
    raw_mode = params.get("mode")
    mode = str(raw_mode).strip().lower() if raw_mode is not None else ""
    if not mode:
        raise schemas.ParamError("mode 不能为空，允许：start / finalize")
    if mode not in MODES:
        raise schemas.ParamError("mode 取值 %r 非法，允许：start / finalize" % raw_mode)

    ratio = schemas.as_float(
        params.get("sample_ratio", _cfg(cfg, "qc.sample_ratio")), "sample_ratio", lo=0.0, hi=1.0
    )
    min_samples = schemas.as_int(
        params.get("min_samples", _cfg(cfg, "qc.min_samples")), "min_samples", lo=1
    )
    snr_threshold = schemas.as_float(
        params.get("snr_threshold_db", _cfg(cfg, "qc.snr_threshold_db")), "snr_threshold_db"
    )
    below_ratio_max = schemas.as_float(
        params.get("snr_below_ratio_max", _cfg(cfg, "qc.snr_below_ratio_max")),
        "snr_below_ratio_max",
        lo=0.0,
        hi=1.0,
    )
    seed = schemas.as_int(params.get("seed", _cfg(cfg, "qc.seed")), "seed")
    batch_reject = schemas.as_bool(params.get("batch_reject"), "batch_reject", default=False)

    decisions: Dict[str, str] = {}
    if mode == "finalize":
        if batch_reject:
            # 整批退回不需要逐条判定（对应状态机的「待复核 → 已退回」）
            decisions = {}
        else:
            raw_decisions = params.get("review_decisions")
            if raw_decisions is None:
                raise schemas.ParamError("finalize 时必须提供 review_decisions（复核判定）")
            decisions = _parse_decisions(raw_decisions)

    return {
        "mode": mode,
        "sample_ratio": float(ratio),
        "min_samples": int(min_samples),
        "snr_threshold_db": float(snr_threshold),
        "snr_below_ratio_max": float(below_ratio_max),
        "seed": int(seed),
        "batch_reject": bool(batch_reject),
        "review_decisions": decisions,
    }


# --------------------------------------------------------------------------- #
# 输入与路径
# --------------------------------------------------------------------------- #
def _find_batch_file(ctx: OperatorContext, inp: Path) -> Path:
    """输入是目录时定位批次 JSONL（按 config 的候选名优先，其次唯一 batch 文件）。"""
    candidates = [str(x) for x in _cfg(ctx.config, "io.batch_filename_candidates")]
    for name in candidates:
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
    raise schemas.OpError("missing_file: 输入目录中没有批次 JSONL 文件：%s" % inp.name)


def _load_batch(ctx: OperatorContext) -> Tuple[Path, List[dict]]:
    inp = ctx.require_input()
    path = inp if inp.is_file() else _find_batch_file(ctx, inp)
    if path.suffix.lower() != ".jsonl":
        raise schemas.OpError("missing_file: 输入批次必须是 JSONL 文件，收到：%s" % path.name)
    try:
        rows = iofs.read_jsonl(path)
    except ValueError as exc:
        raise schemas.OpError("invalid_jsonl: %s" % exc) from exc
    if not rows:
        raise schemas.OpError("missing_file: 输入批次没有样本记录：%s" % path.name)

    # sample_id 是复核判定的唯一键，重复属于输入契约损坏，直接拒绝（不能悄悄合并）
    seen: Dict[str, int] = {}
    duplicates: List[str] = []
    for idx, row in enumerate(rows):
        sid = _row_sample_id(row, idx)
        if sid in seen:
            duplicates.append(sid)
        else:
            seen[sid] = idx
    if duplicates:
        preview = "、".join(sorted(set(duplicates))[:5])
        raise schemas.OpError(
            "invalid_jsonl: sample_id 重复 %d 个（%s），复核判定无法唯一定位" % (len(set(duplicates)), preview)
        )
    return path, rows


def _resolve_media(ctx: OperatorContext, raw) -> Optional[Path]:
    """把行内路径解析成真实文件：绝对路径 / 相对工作目录 / 相对输入目录。"""
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
        except OSError:  # pragma: no cover - 极端非法路径
            continue
    return None


def _clean_reference(row: dict, keys: List[str]) -> Optional[str]:
    """取干净参考路径：优先 ``perturb`` 内部，其次行顶层（兼容上游字段命名）。"""
    perturb = row.get("perturb") if isinstance(row.get("perturb"), dict) else {}
    for key in keys:
        for container in (perturb, row):
            value = container.get(key)
            if not _is_blank(value):
                return str(value)
    return None


# --------------------------------------------------------------------------- #
# 自动检查
# --------------------------------------------------------------------------- #
def _check_sample(
    ctx: OperatorContext,
    row: dict,
    index: int,
    cfg: dict,
    snr_threshold: Optional[float] = None,
) -> dict:
    """单条样本：完整性 + 信噪比（不做任何「可听性」判断）。

    ``snr_threshold`` 必须由调用方从**本次请求的参数**传入（``params.snr_threshold_db``）；
    只有拿不到时才回落到 ``configs/config.yaml`` 的默认值。否则会出现
    「聚合判定用参数、逐样本标记用默认值」的口径分裂——加扰批次的 SNR 目标区间本来
    就低，用原始语料的 15 dB 去判会把整批故意加噪的样本全标成存疑。
    """
    required = [str(x) for x in _cfg(cfg, "qc.required_fields")]
    clean_keys = [str(x) for x in _cfg(cfg, "qc.clean_ref_keys")]
    no_snr_dims = [str(x) for x in (cfg.get("qc", {}).get("no_snr_dimensions") or [])]
    if snr_threshold is None:
        snr_threshold = float(_cfg(cfg, "qc.snr_threshold_db"))
    snr_threshold = float(snr_threshold)
    estimate_bias = float(_cfg(cfg, "qc.estimate_bias_db"))
    snr_cap = float(_cfg(cfg, "qc.snr_cap_db"))

    sid = _row_sample_id(row, index)
    problems: List[str] = []

    # 维度先定：语速类样本没有加噪声，SNR 无业务含义，不做判定也不进分母
    perturb = row.get("perturb") if isinstance(row.get("perturb"), dict) else {}
    dimension = str(perturb.get("dimension") or "").strip()
    snr_applicable = dimension not in no_snr_dims

    missing = [field for field in required if _is_blank(row.get(field))]
    if "perturb" in required and not _is_blank(row.get("perturb")) and not isinstance(
        row.get("perturb"), dict
    ):
        missing.append("perturb")
    if missing:
        problems.append("missing_fields")

    audio = _resolve_media(ctx, row.get("audio_path"))
    y = None
    sr: Optional[int] = None
    if audio is None:
        problems.append("audio_missing")
    else:
        try:
            y, sr = A.load_mono(audio)
        except A.AudioDecodeError as exc:
            problems.append("empty_audio" if str(exc).startswith("empty_audio") else "decode_failed")
        except Exception:  # noqa: BLE001 - 第三方解码异常统一计为坏音频
            problems.append("decode_failed")

    duration: Optional[float] = None
    if y is not None and sr:
        duration = A.duration_sec(y, sr)
        if duration <= 0.0:
            problems.append("zero_duration")
        if A.is_silent(y, float(_cfg(cfg, "audio.silence_rms"))):
            # 「非空」按全产线统一口径：RMS 低于静音阈值即视为空音频
            problems.append("empty_audio")

    snr_db: Optional[float] = None
    snr_source: Optional[str] = None
    clean_raw: Optional[str] = None
    clean_used = False
    if not snr_applicable:
        # 语速这类「没有加噪」的维度：不判 SNR，避免把盲估值当业务指标
        snr_source = "not_applicable"
    elif y is not None and sr:
        clean_raw = _clean_reference(row, clean_keys)
        if clean_raw:
            clean_path = _resolve_media(ctx, clean_raw)
            if clean_path is not None:
                try:
                    clean_y, _clean_sr = A.load_mono(clean_path, sr=sr)
                except A.AudioDecodeError:
                    clean_y = None
                if clean_y is not None and clean_y.size > 0:
                    n = int(min(y.size, clean_y.size))
                    if n > 0:
                        noise = (y[:n] - clean_y[:n]).astype(np.float32)
                        value = A.snr_db(clean_y[:n], noise)
                        if not math.isfinite(value):
                            value = snr_cap if value > 0 else -snr_cap
                        snr_db = round(float(value), 3)
                        snr_source = "measured"
                        clean_used = True
        if snr_source is None:
            snr_db = A.estimate_snr_db(y, sr, bias_db=estimate_bias)
            snr_source = "estimated"

    target = perturb.get("snr_db")
    try:
        target_db = float(target) if target is not None and not _is_blank(target) else None
    except (TypeError, ValueError):
        target_db = None

    snr_below = snr_db is not None and float(snr_db) < snr_threshold
    return {
        "sample_id": sid,
        "index": int(index),
        "integrity_ok": not problems,
        "problems": problems,
        "duration": round(float(duration), 4) if duration is not None else None,
        "audio_found": audio is not None,
        "audio_resolved": str(audio) if audio is not None else None,
        "snr_db": snr_db,
        "snr_source": snr_source,
        "snr_applicable": bool(snr_applicable),
        "dimension": dimension,
        "snr_below": bool(snr_below),
        "snr_target_db": target_db,
        "clean_reference": clean_raw,
        "clean_used": bool(clean_used),
    }


def _run_autocheck(
    ctx: OperatorContext,
    rows: List[dict],
    cfg: dict,
    snr_threshold: Optional[float] = None,
) -> Dict[str, dict]:
    samples: Dict[str, dict] = {}
    total = len(rows)
    every = int(_cfg(cfg, "io.progress_every"))
    for idx, row in enumerate(rows, start=1):
        check = _check_sample(ctx, row, idx - 1, cfg, snr_threshold)
        samples[check["sample_id"]] = check
        if every > 0 and (idx % every == 0 or idx == total):
            ctx.log.progress(idx, total)
    return samples


# --------------------------------------------------------------------------- #
# 抽样
# --------------------------------------------------------------------------- #
def _select_review_queue(samples: Dict[str, dict], size: int, seed: int) -> List[str]:
    """选人工试听队列：优先存疑样本，剩余名额按种子随机补齐；结果排序稳定。"""
    pool = [s for s in samples.values() if s["integrity_ok"]]
    actual = int(min(int(size), len(pool)))
    if actual <= 0:
        return []

    suspicious = sorted(
        (s for s in pool if s["snr_below"]),
        key=lambda s: (float(s["snr_db"] if s["snr_db"] is not None else 0.0), s["sample_id"]),
    )
    chosen: List[str] = [s["sample_id"] for s in suspicious[:actual]]
    chosen_set = set(chosen)
    remaining = sorted(s["sample_id"] for s in pool if s["sample_id"] not in chosen_set)
    need = actual - len(chosen)
    if need > 0 and remaining:
        rng = np.random.default_rng(int(seed))
        picks = rng.choice(len(remaining), size=need, replace=False)
        chosen.extend(remaining[int(i)] for i in sorted(picks))
    return sorted(chosen)


# --------------------------------------------------------------------------- #
# 报告 / 状态
# --------------------------------------------------------------------------- #
def _build_checks(
    samples: Dict[str, dict],
    total_rows: int,
    queue_ids: List[str],
    cfg: dict,
    params: dict,
) -> dict:
    max_listed = int(_cfg(cfg, "io.max_listed"))
    snr_threshold = float(params["snr_threshold_db"])
    below_ratio_max = float(params["snr_below_ratio_max"])

    failed = [s for s in samples.values() if not s["integrity_ok"]]
    applicable = [s for s in samples.values() if s.get("snr_applicable", True)]
    not_applicable = [s for s in samples.values() if not s.get("snr_applicable", True)]
    checked = [s for s in samples.values() if s["snr_db"] is not None]
    below = [s for s in checked if s["snr_below"]]
    measured = [s for s in checked if s["snr_source"] == "measured"]
    # 分母用「可做 SNR 判定的样本数」：语速类样本不计入，否则会被稀释成假通过
    below_ratio = float(len(below)) / float(len(applicable)) if applicable else 0.0

    problem_counts: Dict[str, int] = {}
    for item in failed:
        for problem in item["problems"]:
            problem_counts[problem] = problem_counts.get(problem, 0) + 1

    queue_set = set(queue_ids)
    integrity_ok = len(failed) == 0
    snr_ok = below_ratio <= below_ratio_max
    fail_reasons: List[str] = []
    if not integrity_ok:
        fail_reasons.append("完整性检查未通过：%d 条样本存在字段/音频问题" % len(failed))
    if not snr_ok:
        fail_reasons.append(
            "信噪比低于 %.1f dB 的样本占比 %.1f%%，超过上限 %.1f%%"
            % (snr_threshold, below_ratio * 100.0, below_ratio_max * 100.0)
        )

    below_samples = []
    for item in sorted(below, key=lambda s: (float(s["snr_db"]), s["sample_id"]))[:max_listed]:
        below_samples.append(
            {
                "sample_id": item["sample_id"],
                "snr_db": item["snr_db"],
                "snr_source": item["snr_source"],
                "snr_target_db": item["snr_target_db"],
                "in_review_queue": item["sample_id"] in queue_set,
            }
        )

    failed_samples = [
        {"sample_id": item["sample_id"], "problems": list(item["problems"])}
        for item in failed[:max_listed]
    ]

    return {
        "items": ["完整性", "信噪比"],
        "integrity": {
            "checked": len(samples),
            "failed": len(failed),
            "passed": len(samples) - len(failed),
            "problem_counts": problem_counts,
            "failed_samples": failed_samples,
            "truncated": max(0, len(failed) - max_listed),
        },
        "snr": {
            "checked": len(checked),
            "applicable": len(applicable),
            "not_applicable": len(not_applicable),
            "threshold_db": snr_threshold,
            "below": len(below),
            "below_ratio": round(below_ratio, 4),
            "below_ratio_max": below_ratio_max,
            "source_counts": {"measured": len(measured), "estimated": len(checked) - len(measured)},
            "measure_fallback": sum(
                1
                for s in checked
                if s["snr_source"] == "estimated" and s["clean_reference"] and not s["clean_used"]
            ),
            "min_db": round(min(float(s["snr_db"]) for s in checked), 3) if checked else None,
            "mean_db": round(float(np.mean([float(s["snr_db"]) for s in checked])), 3) if checked else None,
            "max_db": round(max(float(s["snr_db"]) for s in checked), 3) if checked else None,
            "below_samples": below_samples,
            "truncated": max(0, len(below) - max_listed),
        },
        "auto_passed": bool(integrity_ok and snr_ok),
        "fail_reasons": fail_reasons,
    }


def _statistics(
    samples: Dict[str, dict],
    checks: dict,
    sample_size: int,
    review_queue_size: int,
    conclusion: str,
) -> dict:
    return {
        "input_samples": len(samples),
        "total_duration_sec": round(sum(float(s["duration"] or 0.0) for s in samples.values()), 3),
        "integrity_checked": checks["integrity"]["checked"],
        "integrity_failed": checks["integrity"]["failed"],
        "snr_checked": checks["snr"]["checked"],
        "snr_applicable": checks["snr"].get("applicable", checks["snr"]["checked"]),
        "snr_not_applicable": checks["snr"].get("not_applicable", 0),
        "snr_measured": checks["snr"]["source_counts"]["measured"],
        "snr_estimated": checks["snr"]["source_counts"]["estimated"],
        "snr_below_threshold": checks["snr"]["below"],
        "snr_below_ratio": checks["snr"]["below_ratio"],
        "sample_size": int(sample_size),
        "sample_size_actual": int(review_queue_size),
        "review_required": conclusion == CONCLUSION_PENDING,
        "conclusion": conclusion,
    }


def _state_path(ctx: OperatorContext) -> Path:
    return Path(ctx.out_dir) / STATE_FILENAME


def _read_state(ctx: OperatorContext) -> Optional[dict]:
    path = _state_path(ctx)
    if not path.is_file():
        return None
    try:
        payload = iofs.read_json(path)
    except Exception:  # noqa: BLE001 - 状态损坏时回退重算，不阻断
        ctx.log.warning("start 阶段检查结果不可读，改为重新检查")
        return None
    return payload if isinstance(payload, dict) else None


def _review_queue_rows(rows: List[dict], samples: Dict[str, dict], queue_ids: List[str]) -> List[dict]:
    by_id: Dict[str, dict] = {}
    for idx, row in enumerate(rows):
        by_id[_row_sample_id(row, idx)] = row
    out: List[dict] = []
    for sid in queue_ids:
        row = by_id.get(sid, {})
        check = samples[sid]
        out.append(
            {
                "sample_id": sid,
                "source_uttid": row.get("source_uttid"),
                "dataset_id": row.get("dataset_id"),
                "audio_path": row.get("audio_path"),
                "audio_path_resolved": check["audio_resolved"],
                "duration": check["duration"],
                "text": row.get("text"),
                "perturb": row.get("perturb"),
                "snr_db": check["snr_db"],
                "snr_source": check["snr_source"],
                "snr_below_threshold": check["snr_below"],
                "review_required": "人工试听判定加扰可听性（通过 / 剔除）",
            }
        )
    return out


# --------------------------------------------------------------------------- #
# mode=start
# --------------------------------------------------------------------------- #
def _start(ctx: OperatorContext, cfg: dict, p: dict) -> dict:
    batch_path, rows = _load_batch(ctx)
    ctx.log.route("全量自动检查 → 按比例抽样 → 人工试听复核队列")
    ctx.log.read(len(rows), "条批次记录")

    samples = _run_autocheck(ctx, rows, cfg, p["snr_threshold_db"])
    formula_size = _sample_size(len(rows), p["sample_ratio"], p["min_samples"])
    queue_ids = _select_review_queue(samples, formula_size, p["seed"])
    checks = _build_checks(samples, len(rows), queue_ids, cfg, p)

    if checks["auto_passed"]:
        conclusion = CONCLUSION_PENDING
        review_queue = _review_queue_rows(rows, samples, queue_ids)
    else:
        # 自动检查不通过：两项全过才进入抽样，本批次直接退回，不产生复核队列
        conclusion = CONCLUSION_FAIL
        queue_ids = []
        review_queue = []
        checks = _build_checks(samples, len(rows), [], cfg, p)

    statistics = _statistics(samples, checks, formula_size, len(review_queue), conclusion)
    report = {
        "operator": OPERATOR_ID,
        "operator_name": OPERATOR_NAME,
        "unique_id": ctx.unique_id,
        "mode": "start",
        "created_at": _now_iso(),
        "batch": {
            "file": batch_path.name,
            "input_samples": len(rows),
            "fingerprint": A.sha256_file(batch_path),
        },
        "parameters": {
            "sample_ratio": p["sample_ratio"],
            "min_samples": p["min_samples"],
            "snr_threshold_db": p["snr_threshold_db"],
            "snr_below_ratio_max": p["snr_below_ratio_max"],
            "seed": p["seed"],
        },
        "statistics": statistics,
        "checks": checks,
        "sample_size": int(formula_size),
        "sample_size_actual": len(review_queue),
        "review_required": conclusion == CONCLUSION_PENDING,
        "conclusion": conclusion,
        "review_queue": {
            "file": QUEUE_FILENAME,
            "size": len(review_queue),
            "note": "自动检查只做完整性与信噪比两项；加扰可听性由人工试听判定，算子不自动评价",
        },
    }
    state = {
        "operator": OPERATOR_ID,
        "unique_id": ctx.unique_id,
        "mode": "start",
        "created_at": report["created_at"],
        "batch": report["batch"],
        "parameters": report["parameters"],
        "statistics": statistics,
        "checks": checks,
        "conclusion": conclusion,
        "sample_size": int(formula_size),
        "sample_size_actual": len(review_queue),
        "review_queue_ids": list(queue_ids),
        "samples": samples,
    }

    report_path = iofs.write_json(ctx.out(REPORT_FILENAME), report)
    queue_path = iofs.write_jsonl(ctx.out(QUEUE_FILENAME), review_queue)
    iofs.write_json(_state_path(ctx), state)

    ctx.log.result(
        "自动检查",
        output=len(rows),
        success=len(rows) - checks["integrity"]["failed"],
        failed=checks["integrity"]["failed"],
        extra={"信噪比存疑": checks["snr"]["below"]},
    )
    fallback = checks["snr"]["measure_fallback"]
    if fallback:
        ctx.log.warning("%d 条样本声明了干净参考但不可用，已回退盲估 SNR（标 estimated）" % fallback)
    ctx.log.write_output(report_path.name)
    ctx.log.write_output(queue_path.name)
    if conclusion == CONCLUSION_PENDING:
        ctx.log.waiting("人工试听复核 %d 条（抽样比例 %.1f%%，最少 %d 条）" % (
            len(review_queue), p["sample_ratio"] * 100.0, p["min_samples"]
        ))
    else:
        ctx.log.partial(
            "自动检查未通过，本批次退回重做",
            "完整性失败 %d 条，信噪比低于阈值 %d 条（占 %.1f%%）"
            % (
                checks["integrity"]["failed"],
                checks["snr"]["below"],
                checks["snr"]["below_ratio"] * 100.0,
            ),
        )

    return {
        "outputs": {"report": report_path, "review_queue": queue_path},
        "statistics": statistics,
        "result": {
            "mode": "start",
            "conclusion": conclusion,
            "sample_size": int(formula_size),
            "sample_size_actual": len(review_queue),
            "review_required": conclusion == CONCLUSION_PENDING,
            "auto_passed": checks["auto_passed"],
            "integrity_failed": checks["integrity"]["failed"],
            "snr_below": checks["snr"]["below"],
            "snr_below_ratio": checks["snr"]["below_ratio"],
        },
        # 结论「待复核」= 业务还没定论，必须回 partial。
        # 只把「自动检查过了就当完成」是错的：抽检还没人听，链路不能算走完，
        # 否则平台会把一个等人工确认的批次当成已完成批次往下游推。
        "status": "partial" if (not checks["auto_passed"] or conclusion == CONCLUSION_PENDING) else "success",
    }


# --------------------------------------------------------------------------- #
# mode=finalize
# --------------------------------------------------------------------------- #
def _finalize(ctx: OperatorContext, cfg: dict, p: dict) -> dict:
    batch_path, rows = _load_batch(ctx)
    ctx.log.route("复核判定回填 → 最终样本表")
    ctx.log.read(len(rows), "条批次记录")

    fingerprint = A.sha256_file(batch_path)
    state = _read_state(ctx)
    reuse = bool(state and state.get("batch", {}).get("fingerprint") == fingerprint)
    if reuse:
        samples = state["samples"]
        queue_ids = list(state.get("review_queue_ids") or [])
        formula_size = int(state.get("sample_size") or _sample_size(
            len(rows), p["sample_ratio"], p["min_samples"]
        ))
        checks = state["checks"]
        ctx.log.warning("复用 start 阶段自动检查结果（同批次指纹），未重复解码音频")
    else:
        if state is not None:
            ctx.log.warning("start 阶段结果与当前批次不一致，重新执行自动检查")
        samples = _run_autocheck(ctx, rows, cfg, p["snr_threshold_db"])
        formula_size = _sample_size(len(rows), p["sample_ratio"], p["min_samples"])
        queue_ids = _select_review_queue(samples, formula_size, p["seed"])
        checks = _build_checks(samples, len(rows), queue_ids, cfg, p)

    if len(p["review_decisions"]):
        ctx.log.read(len(p["review_decisions"]), "条复核判定")

    queue_set = set(queue_ids)
    if not p["batch_reject"]:
        missing = [sid for sid in queue_ids if sid not in p["review_decisions"]]
        if missing:
            ctx.log.error(
                "复核提交",
                "复核未判定完，剩余 %d 条（共 %d 条），不允许提交" % (len(missing), len(queue_ids)),
            )
            raise schemas.OpError(
                "human_review_incomplete: 复核未判定完，剩余 %d 条未判定（共 %d 条），不允许提交"
                % (len(missing), len(queue_ids))
            )
        unknown = sorted(set(p["review_decisions"]) - queue_set)
        if unknown:
            preview = "、".join(unknown[:5])
            raise schemas.OpError(
                "invalid_params: review_decisions 含 %d 个不在复核队列中的 sample_id（%s）"
                % (len(unknown), preview)
            )

    if p["batch_reject"]:
        conclusion = CONCLUSION_FAIL
    elif not checks["auto_passed"]:
        conclusion = CONCLUSION_FAIL
    else:
        conclusion = CONCLUSION_PASS

    reviewed_pass = sum(1 for d in p["review_decisions"].values() if d == "pass")
    reviewed_reject = sum(1 for d in p["review_decisions"].values() if d == "reject")

    final_rows: List[dict] = []
    passed = rejected = auto_rejected = human_rejected = 0
    for idx, row in enumerate(rows):
        sid = _row_sample_id(row, idx)
        check = samples[sid]
        if p["batch_reject"]:
            status = "rejected"
            reason = "整批退回"
            human_rejected += 0
        elif not check["integrity_ok"]:
            status = "rejected"
            labels = "、".join(PROBLEM_LABELS.get(x, x) for x in check["problems"])
            reason = "自动检查未通过：完整性（%s）" % labels
            auto_rejected += 1
        elif p["review_decisions"].get(sid) == "reject":
            status = "rejected"
            reason = "人工复核剔除"
            human_rejected += 1
        else:
            status = "pass"
            reason = "人工复核通过" if sid in queue_set else "自动检查通过"

        if status == "pass":
            passed += 1
        else:
            rejected += 1
        out_row = dict(row)
        out_row["qc"] = {
            "status": status,
            "snr_db": check["snr_db"],
            "snr_source": check["snr_source"],
            "reason": reason,
        }
        final_rows.append(out_row)

    statistics = {
        "input_samples": len(rows),
        "total_duration_sec": round(
            sum(float(samples[_row_sample_id(row, idx)]["duration"] or 0.0) for idx, row in enumerate(rows)), 3
        ),
        "review_queue": len(queue_ids),
        "reviewed": len(p["review_decisions"]),
        "review_passed": reviewed_pass,
        "review_rejected": reviewed_reject,
        "auto_rejected": auto_rejected,
        "passed": passed,
        "rejected": rejected,
        "version_sample_count": passed,
        "conclusion": conclusion,
    }

    final_report = {
        "operator": OPERATOR_ID,
        "operator_name": OPERATOR_NAME,
        "unique_id": ctx.unique_id,
        "mode": "finalize",
        "created_at": _now_iso(),
        "batch": {
            "file": batch_path.name,
            "input_samples": len(rows),
            "fingerprint": fingerprint,
        },
        "conclusion": conclusion,
        "qc_conclusion": conclusion,
        "review": {
            "queue_size": len(queue_ids),
            "reviewed": len(p["review_decisions"]),
            "passed": reviewed_pass,
            "rejected": reviewed_reject,
            "complete": True,
            "batch_reject": p["batch_reject"],
            "status": "已退回" if p["batch_reject"] else "已完成",
            "note": "加扰可听性为人工试听判定，算子仅回填判定结果",
        },
        "auto_checks": {
            "auto_passed": checks["auto_passed"],
            "integrity_failed": checks["integrity"]["failed"],
            "snr_below": checks["snr"]["below"],
            "snr_below_ratio": checks["snr"]["below_ratio"],
            "fail_reasons": checks["fail_reasons"],
        },
        "snr": {
            "threshold_db": p["snr_threshold_db"],
            "measured": checks["snr"]["source_counts"]["measured"],
            "estimated": checks["snr"]["source_counts"]["estimated"],
            "below": checks["snr"]["below"],
        },
        "statistics": statistics,
        "sample_count": {"input": len(rows), "version": passed, "rejected": rejected},
        "batch_final": {"file": BATCH_FINAL_FILENAME, "row_count": len(final_rows)},
    }

    batch_final_path = iofs.write_jsonl(ctx.out(BATCH_FINAL_FILENAME), final_rows)
    qc_final_path = iofs.write_json(ctx.out(QC_FINAL_FILENAME), final_report)

    ctx.log.result(
        "复核回填",
        output=len(final_rows),
        success=passed,
        skipped=human_rejected,
        failed=auto_rejected,
        extra={"质检结论": conclusion},
    )
    ctx.log.write_output(batch_final_path.name)
    ctx.log.write_output(qc_final_path.name)
    if conclusion == CONCLUSION_FAIL:
        ctx.log.partial(
            "质检结论不通过，剔除样本不计入版本",
            "通过 %d 条，剔除 %d 条" % (passed, rejected),
        )

    return {
        "outputs": {"batch_final": batch_final_path, "qc_final": qc_final_path},
        "statistics": statistics,
        "result": {
            "mode": "finalize",
            "conclusion": conclusion,
            "qc_conclusion": conclusion,
            "sample_count": {"input": len(rows), "version": passed, "rejected": rejected},
            "review": {
                "queue_size": len(queue_ids),
                "reviewed": len(p["review_decisions"]),
                "complete": True,
                "batch_reject": p["batch_reject"],
            },
        },
        "status": "success" if conclusion == CONCLUSION_PASS else "partial",
    }


# --------------------------------------------------------------------------- #
# 入口
# --------------------------------------------------------------------------- #
def run(ctx: OperatorContext) -> dict:
    ctx.log.start()
    cfg = ctx.config
    p = _parse_params(ctx.params or {}, cfg)
    if p["mode"] == "start":
        result = _start(ctx, cfg, p)
    else:
        result = _finalize(ctx, cfg, p)
    ctx.log.done()
    return result


__all__ = ["run", "OPERATOR_ID", "OPERATOR_NAME"]
