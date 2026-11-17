# -*- coding: utf-8 -*-
"""03 噪声混音加扰（avp_mix_noise）。

职责
----
把 02 标准化语料按「加扰维度 + 取值 + SNR 区间」混入噪声/环境音素材，产出带**参数快照**的
对抗样本批次 ``003_batch.jsonl``，供 05 样本质检使用。

业务硬口径（任务书 §3 + 规范 §11.4，不得自行改动）
---------------------------------------------------
- 加扰只做**维度组合 + SNR 区间**：``dimension ∈ {noise, env}``，**口音不是加扰维度**；
- ``values`` 是维度取值（如 ``白噪/人群嘈杂/交通噪声`` 或 ``街道/车内``），素材默认从
  ``weight/assets/<dimension>/<取值>.wav`` 取；**找不到该取值就报 ``invalid_params``**，不静默换素材；
- ``snr_db`` 是 ``[下界, 上界]`` 区间，逐条在区间内取目标 SNR；
- 混音必须用 ``common.audio.merge_at_snr``（按目标 SNR 反算噪声增益），并**回报实测 SNR 与目标误差**；
- 输出 ``003_batch.jsonl`` 行结构固定：
  ``{sample_id, source_uttid, dataset_id, audio_path, text, duration,
  perturb:{dimension, value, snr_db, speed, asset}}``；
- 加工范围 `all`（全部）或 `count`（指定条数，不得超过语料样本数）。

技术方案
--------
1. 语音按**原生采样率**解码（02 已标准化，本算子不改格式），噪声素材重采样到同一采样率；
2. 目标 SNR 在区间内确定性随机（`np.random.default_rng(seed)`），按
   ``scale = sqrt(P_sig / (10^(snr/10) * P_noise))`` 反算噪声增益；
3. 混音后峰值超过 0.99 时**信号与噪声同乘一个系数**，SNR 不变；
4. **实测**：``merge_at_snr`` 返回的 ``applied_snr_db`` 是在实际参与混音的信号/噪声分量上按
   ``10*log10(P_sig/P_noise)`` 算出来的（不是把目标值抄回来），逐条记录 `applied - target` 误差；
   误差超过 ``max_snr_error_db`` 计为违规并让批次 ``partial``；
5. 素材按取值缓存、只加载一次；混音结果统一写 WAV PCM16。

注意：不要把「混音文件功率 / 噪声功率」当 SNR —— 那测的是 (S+N)/N，会系统性偏高（8 dB 目标会读成
8.6 dB）。SNR 定义要求以**干净参考**为信号分量，本项目用 merge 实际使用的干净信号参考
（可能已按峰值同比缩放）与该噪声分量计算，误差应为 0.000 dB 量级。

确定性与失败口径
----------------
- 同输入 + 同 params（含 seed）→ 同输出（音频逐字节一致，测试覆盖）；
- 单条静音/解码失败/混音异常 → 计入 ``rejects`` 并继续（``partial``）；整批 0 产出才抛错；
- 素材缺失/静音/无法解码属于**参数或环境问题**，整批直接报错，不产出半批假数据。
"""

from __future__ import annotations

import re
import sys
from pathlib import Path
from typing import Dict, List, Optional, Tuple

import numpy as np

_ROOT = Path(__file__).resolve().parent.parent
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))

from common import audio as A  # noqa: E402
from common import iofs  # noqa: E402
from common import schemas  # noqa: E402
from common.context import OperatorContext  # noqa: E402

OPERATOR_ID = "avp_mix_noise"
OPERATOR_NAME = "噪声混音加扰"

# 进度日志刷新间隔（非业务阈值）
PROGRESS_EVERY = 100

_RE_SAMPLE = re.compile(r"[^\w.\-]+", re.UNICODE)
_MAX_STEM_LEN = 60


# --------------------------------------------------------------------------- #
# 参数
# --------------------------------------------------------------------------- #
def _parse_params(params: dict, cfg: dict) -> dict:
    mix = cfg["mix"]
    schemas.require(params, ("dimension", "values", "snr_db"))

    dimension = schemas.enum(
        str(params.get("dimension") or "").strip(), list(mix["allowed_dimensions"]), "dimension"
    )

    raw_values = schemas.as_list(params.get("values"), "values")
    values = [str(v).strip() for v in raw_values]
    if any(not v for v in values):
        raise schemas.ParamError("values 不能包含空取值")
    if len(set(values)) != len(values):
        raise schemas.ParamError("values 不能包含重复取值：%r" % raw_values)

    snr_range = schemas.as_range(params.get("snr_db"), "snr_db", 2)
    lo, hi = float(snr_range[0]), float(snr_range[1])
    snr_min = float(mix["snr_min_db"])
    snr_max = float(mix["snr_max_db"])
    if lo < snr_min or hi > snr_max:
        raise schemas.ParamError(
            "snr_db 区间 [%s, %s] 超出允许范围 [%s, %s] dB" % (lo, hi, snr_min, snr_max)
        )

    scope_mode = str(params.get("scope_mode") or "all").strip().lower()
    if scope_mode not in ("all", "count"):
        raise schemas.ParamError("scope_mode 取值 %r 非法，允许：all / count" % scope_mode)
    scope_count = None
    if scope_mode == "count":
        if params.get("scope_count") in (None, ""):
            raise schemas.ParamError("scope_mode=count 时必须提供 scope_count")
        scope_count = schemas.as_int(params.get("scope_count"), "scope_count", lo=1)

    assets_dir = params.get("assets_dir")
    if assets_dir is not None:
        assets_dir = schemas.as_str(assets_dir, "assets_dir", max_len=300)

    return {
        "dimension": dimension,
        "values": values,
        "snr_low": lo,
        "snr_high": hi,
        "snr_decimals": int(mix["snr_decimals"]),
        "scope_mode": scope_mode,
        "scope_count": scope_count,
        "assets_dir": assets_dir,
        "seed": schemas.as_int(
            params.get("seed", mix["default_seed"]), "seed", lo=0, hi=2**32 - 1
        ),
        "per_value": schemas.as_bool(
            params.get("per_value"), "per_value", default=bool(mix["per_value_default"])
        ),
    }


# --------------------------------------------------------------------------- #
# 输入 / 素材
# --------------------------------------------------------------------------- #
def _resolve_input(ctx: OperatorContext, filename: str) -> Path:
    """定位 02 的 ``002_std.jsonl``；定位不到就 ``missing_file``，不静默换源。"""
    raw = ctx.input_file
    if raw is None:
        for candidate in (Path(ctx.work_dir) / "input" / "extracted", Path(ctx.work_dir) / "input"):
            if candidate.is_dir():
                raw = candidate
                break
    if raw is None:
        raise schemas.OpError("missing_file: 未提供 inputFile，且任务输入目录为空")
    path = Path(raw)
    if path.is_dir():
        exact = path / filename
        if exact.is_file():
            return exact
        found = sorted(path.rglob("*.jsonl"))
        named = [q for q in found if q.name == filename]
        if named:
            return named[0]
        if len(found) == 1:
            return found[0]
        raise schemas.OpError(
            "missing_file: 输入目录中未找到 %s（共发现 %d 个 jsonl）" % (filename, len(found))
        )
    if not path.is_file():
        raise schemas.OpError("missing_file: 输入文件不存在 %s" % path.name)
    return path


def _read_rows(path: Path) -> List[dict]:
    try:
        rows = iofs.read_jsonl(path)
    except ValueError as exc:
        raise schemas.OpError("invalid_jsonl: %s" % exc) from exc
    if not rows:
        raise schemas.OpError("invalid_jsonl: 输入文件没有记录 %s" % path.name)
    return rows


def _resolve_audio(raw, corpus_path: Path, work_dir: Path) -> Optional[Path]:
    if raw in (None, ""):
        return None
    p = Path(str(raw))
    if p.is_absolute():
        return p if p.is_file() else None
    for base in (corpus_path.parent, work_dir / "input", work_dir):
        candidate = (base / p).resolve()
        if candidate.is_file():
            return candidate
    return None


def _assets_dir(ctx: OperatorContext, cfg: dict, p: dict) -> Path:
    if p["assets_dir"]:
        base = Path(p["assets_dir"])
        if not base.is_absolute():
            base = Path(ctx.work_dir) / base
        if not base.is_dir():
            raise schemas.ParamError("assets_dir 不存在：%s" % p["assets_dir"])
        return base
    base = _ROOT / str(cfg["mix"]["assets_root"]) / p["dimension"]
    if not base.is_dir():
        raise schemas.OpError("missing_file: 噪声素材目录不存在：%s" % base.name)
    return base


def _load_assets(
    ctx: OperatorContext, base: Path, values: List[str], ext: str
) -> Dict[str, dict]:
    """逐个取值加载素材：找不到 → invalid_params；静音/解码失败 → 整批报错。"""
    missing = [v for v in values if not (base / (v + ext)).is_file()]
    if missing:
        raise schemas.ParamError(
            "values 中以下取值在素材目录 %s 下没有对应素材（%s）：%s"
            % (base.name, ext, "、".join(missing))
        )
    assets: Dict[str, dict] = {}
    for value in values:
        path = base / (value + ext)
        try:
            y, sr = A.load_raw(path)
        except A.AudioDecodeError as exc:
            raise schemas.OpError("decode_failed: 噪声素材无法解码 %s：%s" % (path.name, exc)) from exc
        if A.is_silent(y, float(ctx.config["audio"]["silence_rms"])):
            raise schemas.OpError("silent_audio: 噪声素材全静音 %s" % path.name)
        assets[value] = {"path": path, "y": y, "sr": int(sr)}
    return assets


# --------------------------------------------------------------------------- #
# 主流程
# --------------------------------------------------------------------------- #
def run(ctx: OperatorContext) -> dict:
    cfg = ctx.config
    p = _parse_params(ctx.params or {}, cfg)
    max_listed = int(cfg["io"]["max_listed"])
    out_subdir = str(cfg["io"]["output_subdir"])

    ctx.log.route("读取标准语料 → 校验维度/素材/SNR 区间 → 按 SNR 混音 → 实测复核 → 落盘")

    input_path = _resolve_input(ctx, str(cfg["io"]["input_filename"]))
    rows = _read_rows(input_path)
    ctx.log.validated(input_path.name)
    ctx.log.read(len(rows), "条标准语料记录")

    if p["scope_mode"] == "count" and int(p["scope_count"]) > len(rows):
        raise schemas.ParamError(
            "scope_count（%d）不得超过输入语料样本数（%d）" % (p["scope_count"], len(rows))
        )
    selected = rows[: int(p["scope_count"])] if p["scope_mode"] == "count" else list(rows)

    base = _assets_dir(ctx, cfg, p)
    assets = _load_assets(ctx, base, p["values"], str(cfg["mix"]["asset_ext"]))
    ctx.log.result(
        "素材加载",
        output=len(assets),
        extra={"素材目录": base.name, "取值": "、".join(p["values"])},
    )

    audio_dir = iofs.ensure_dir(ctx.out(out_subdir))
    rng = np.random.default_rng(p["seed"])

    out_rows: List[dict] = []
    samples: List[dict] = []
    rejects: List[dict] = []
    by_value: Dict[str, dict] = {
        v: {"value": v, "count": 0, "snr_target_sum": 0.0, "snr_error_max_abs_db": 0.0}
        for v in p["values"]
    }
    snr_errors: List[float] = []
    snr_targets: List[float] = []
    total = len(selected)
    seq = 0

    for idx, row in enumerate(selected, start=1):
        if idx % PROGRESS_EVERY == 0 or idx == total:
            ctx.log.progress(idx, total)

        uttid = str(row.get("uttid") or "").strip()
        if not uttid:
            rejects.append({"line": idx, "reason": "missing_field", "detail": "缺少 uttid"})
            continue

        raw_audio = row.get("std_audio_path")
        if raw_audio in (None, ""):
            raw_audio = row.get("audio_path")
        src = _resolve_audio(raw_audio, input_path, Path(ctx.work_dir))
        if src is None:
            rejects.append(
                {
                    "line": idx,
                    "uttid": uttid,
                    "reason": "audio_not_found",
                    "detail": "音频文件不存在：%s" % Path(str(raw_audio)).name,
                }
            )
            continue

        try:
            sig, sr = A.load_raw(src)
        except A.AudioDecodeError as exc:
            rejects.append({"line": idx, "uttid": uttid, "reason": "decode_failed", "detail": short(exc)})
            continue
        if A.is_silent(sig, float(cfg["audio"]["silence_rms"])):
            rejects.append({"line": idx, "uttid": uttid, "reason": "silent_audio", "detail": "干净语音为静音，无法按 SNR 混音"})
            continue

        if p["per_value"]:
            value_list = list(p["values"])
        else:
            value_list = [p["values"][int(rng.integers(0, len(p["values"])))]]

        for value in value_list:
            seq += 1
            if p["snr_low"] == p["snr_high"]:
                target = float(p["snr_low"])
            else:
                target = round(float(rng.uniform(p["snr_low"], p["snr_high"])), p["snr_decimals"])
                target = min(max(target, p["snr_low"]), p["snr_high"])
            seed_i = int(rng.integers(0, 2**31 - 1))

            asset = assets[value]
            noise = A.resample(asset["y"], asset["sr"], sr) if asset["sr"] != sr else asset["y"]
            try:
                mixed, _scaled_noise, applied = A.merge_at_snr(sig, noise, target, seed=seed_i)
            except ValueError as exc:
                rejects.append(
                    {
                        "line": idx,
                        "uttid": uttid,
                        "value": value,
                        "reason": "mix_failed",
                        "detail": str(exc),
                    }
                )
                continue

            sample_id = _sample_id(uttid, p["dimension"], seq)
            dst = audio_dir / ("%s.wav" % sample_id)
            try:
                A.write_pcm16(dst, mixed, sr)
                duration = A.file_duration(dst)
            except Exception as exc:  # noqa: BLE001 - 单条写盘失败不拖垮整批
                rejects.append(
                    {"line": idx, "uttid": uttid, "value": value, "reason": "write_failed", "detail": short(exc)}
                )
                continue

            error = round(float(applied) - float(target), 6)
            snr_errors.append(abs(error))
            snr_targets.append(float(target))

            out_rows.append(
                {
                    "sample_id": sample_id,
                    "source_uttid": uttid,
                    "dataset_id": row.get("dataset_id", ""),
                    "audio_path": str(dst),
                    # 干净参考路径（契约外附加字段）：05 质检据此用
                    # 「noise = mixed - clean」**实测** SNR，而不是盲估。
                    # 少了它，加扰样本的信噪比只能靠估，与业务口径不符。
                    "clean_audio_path": str(src),
                    "text": row.get("text", ""),
                    "duration": round(float(duration), 3),
                    "perturb": {
                        "dimension": p["dimension"],
                        "value": value,
                        "snr_db": float(target),
                        "speed": None,
                        "asset": str(asset["path"]),
                    },
                }
            )
            samples.append(
                {
                    "sample_id": sample_id,
                    "source_uttid": uttid,
                    "value": value,
                    "snr_target_db": float(target),
                    "snr_applied_db": round(float(applied), 3),
                    "snr_error_db": error,
                    "sample_rate": int(sr),
                    "duration": round(float(duration), 3),
                    "audio_path": str(dst),
                }
            )
            stats_v = by_value[value]
            stats_v["count"] += 1
            stats_v["snr_target_sum"] += float(target)
            stats_v["snr_error_max_abs_db"] = max(stats_v["snr_error_max_abs_db"], abs(error))

    if not out_rows:
        reasons = {str(r.get("reason")) for r in rejects}
        prefix = "silent_audio" if reasons == {"silent_audio"} else "decode_failed"
        raise schemas.OpError(
            "%s: 批内没有成功产出混音样本（输入 %d 条，失败 %d 条），详见 rejects"
            % (prefix, total, len(rejects))
        )

    max_error_allowed = float(cfg["mix"]["max_snr_error_db"])
    violations = sum(1 for err in snr_errors if err > max_error_allowed)
    mean_abs_error = round(float(np.mean(snr_errors)), 6) if snr_errors else 0.0
    max_abs_error = round(float(np.max(snr_errors)), 6) if snr_errors else 0.0

    value_summaries = []
    for value in p["values"]:
        stats_v = by_value[value]
        count = int(stats_v["count"])
        value_summaries.append(
            {
                "value": value,
                "asset": str(assets[value]["path"]),
                "count": count,
                "snr_target_mean_db": round(stats_v["snr_target_sum"] / count, 3) if count else None,
                "snr_error_max_abs_db": round(stats_v["snr_error_max_abs_db"], 6),
            }
        )

    statistics = {
        "input_records": len(rows),
        "selected_input": total,
        "output": len(out_rows),
        "success": len(out_rows),
        "rejected": len(rejects),
        "values": len(p["values"]),
        "per_value": bool(p["per_value"]),
        "snr_error_mean_abs_db": mean_abs_error,
        "snr_error_max_abs_db": max_abs_error,
        "snr_error_threshold_db": max_error_allowed,
        "snr_error_violations": violations,
        "snr_target_min_db": round(min(snr_targets), 3) if snr_targets else None,
        "snr_target_max_db": round(max(snr_targets), 3) if snr_targets else None,
        "total_duration_sec": round(sum(float(r["duration"]) for r in out_rows), 3),
        "dimension": p["dimension"],
    }

    status = "partial" if (rejects or violations) else "success"

    report = {
        "operator": {"id": OPERATOR_ID, "name": OPERATOR_NAME},
        "params": {
            "dimension": p["dimension"],
            "values": list(p["values"]),
            "snr_db": [p["snr_low"], p["snr_high"]],
            "assets_dir": str(base),
            "scope_mode": p["scope_mode"],
            "scope_count": p["scope_count"],
            "seed": p["seed"],
            "per_value": p["per_value"],
        },
        "statistics": statistics,
        "mix": {
            "dimension": p["dimension"],
            "values": list(p["values"]),
            "snr_range": [p["snr_low"], p["snr_high"]],
            "snr_decimals": p["snr_decimals"],
            "assets": value_summaries,
            "snr_validation": {
                "definition": "10*log10(P_signal/P_noise)，signal 为实际参与混音的干净参考（峰值限幅后），noise 为缩放后的噪声分量；由 merge_at_snr 在真实分量上计算，非目标值回填",
                "mean_abs_error_db": mean_abs_error,
                "max_abs_error_db": max_abs_error,
                "threshold_db": max_error_allowed,
                "violations": violations,
            },
        },
        "samples": samples[:max_listed],
        "rejects": rejects[:max_listed],
        "truncated": {
            "samples": max(0, len(samples) - max_listed),
            "rejects": max(0, len(rejects) - max_listed),
        },
        "conclusion": status,
    }

    batch_path = iofs.write_jsonl(ctx.out("003_batch.jsonl"), out_rows)
    report_path = iofs.write_json(ctx.out("003_perturb_report.json"), report)

    ctx.log.write_output(batch_path.name)
    ctx.log.write_output(report_path.name)

    if violations:
        ctx.log.warning(
            "%d 条样本实测 SNR 误差超过 %.1f dB 阈值（最大 %.3f dB），批次标记 partial"
            % (violations, max_error_allowed, max_abs_error)
        )
    if rejects:
        ctx.log.error("混音", "%d 条样本未产出（静音/解码/写盘异常）" % len(rejects))

    if status == "partial":
        ctx.log.partial(
            "批次存在未完成/未达标项",
            "成功 %d 条，失败 %d 条，SNR 误差超阈值 %d 条"
            % (len(out_rows), len(rejects), violations),
        )
    else:
        ctx.log.result(
            "混音",
            output=len(out_rows),
            success=len(out_rows),
            failed=len(rejects),
            extra={"实测SNR误差均值": "%.6f dB" % mean_abs_error},
        )

    return {
        "outputs": {"batch": batch_path, "report": report_path},
        "statistics": statistics,
        "result": {
            "dimension": p["dimension"],
            "values": list(p["values"]),
            "snr_db": [p["snr_low"], p["snr_high"]],
            "generated": len(out_rows),
            "rejected": len(rejects),
            "snr_error_mean_abs_db": mean_abs_error,
            "snr_error_max_abs_db": max_abs_error,
            "conclusion": status,
        },
        "status": status,
    }


def _sample_id(uttid: str, dimension: str, seq: int) -> str:
    stem = _RE_SAMPLE.sub("_", str(uttid)).strip("._-") or "utt"
    stem = stem[:_MAX_STEM_LEN]
    return "%s__%s__%04d" % (stem, dimension, int(seq))


def short(value, limit: int = 120) -> str:
    text = str(value).replace("\n", " ").replace("\r", " ")
    return text if len(text) <= limit else text[: limit - 1] + "…"


__all__ = ["run", "OPERATOR_ID", "OPERATOR_NAME"]
