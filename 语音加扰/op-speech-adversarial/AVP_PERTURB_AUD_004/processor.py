# -*- coding: utf-8 -*-
"""04 语速变换加扰（avp_speed_perturb）。

职责
----
把 02 标准化语料按语速倍率做**变速不变调**（time-stretch），产出带参数快照的对抗样本批次
``004_batch.jsonl``，供 05 样本质检使用。

业务硬口径（任务书 §4 + 规范 §11.5，不得自行改动）
---------------------------------------------------
- ``values`` 只能来自 ``["0.8x", "1.0x", "1.2x"]``，其它值报 ``invalid_params``；
- 变速用 ``common.audio.time_stretch(y, rate)``（变速不变调）；
- **1.0× 必须原样透传，输出 PCM 与输入逐字节一致**；
- 输出 ``004_batch.jsonl`` 行结构固定：
  ``{sample_id, source_uttid, dataset_id, audio_path, text, duration,
  perturb:{dimension:"speed", value:"1.2x", snr_db:null, speed:1.2, asset:null}}``；
- 加工范围 `all` / `count`（不得超过语料样本数）；
- 必须回报**输出时长误差**（与 ``源时长 / 倍率`` 的偏差）。

技术方案
--------
1. 语音按**原生采样率**解码（02 已标准化，本算子不改采样率/声道/格式）；
2. 非 1.0× 用 ``librosa.effects.time_stretch``（STFT 相位声码器）做变速不变调：
   帧长/跳长由库内部决定，本算子只传 rate，不自己实现相位声码器；
3. **1.0× 走文件级原样透传**：不重新解码、不重新量化，直接复制 02 写出的 WAV PCM16 字节，
   因此输出文件与输入**逐字节一致**（只改文件名，不改内容）。解码→写盘会因去直流与 PCM16
   量化产生 ±1 LSB 漂移，无法满足"逐字节一致"，所以这里必须走复制分支；
   ``common.audio.time_stretch`` 在 rate=1.0 本身也是原样返回，本算子在该分支不重复加工。
4. 时长误差 = ``|实际输出时长 - 源时长/倍率| / (源时长/倍率) × 100%``：
   - 1.0× 理论为 0；
   - 相位声码器输出帧数取整会引入一个跳到几个采样点的误差，实测 < 0.01%，阈值 1%；
   超过 ``max_duration_error_pct`` 记为违规，批次 ``partial`` 并告警。
5. 落盘后从**文件头重新读取**输出时长，不用数组长度冒充落盘结果。

确定性与失败口径
----------------
- 同输入 + 同 params（含 seed）→ 同输出（音频逐字节一致，测试覆盖）；
- 单条解码失败 / 写盘失败 → 计入 ``rejects`` 并继续（``partial``）；整批 0 产出才抛错；
- 1.0× 输入不是 16bit PCM WAV（理论上 02 不会产出）→ 退化为解码+写出，标记
  ``passthrough_identical=false`` 并让批次 ``partial``，不伪装成"逐字节一致"。
"""

from __future__ import annotations

import re
import shutil
import sys
from pathlib import Path
from typing import Dict, List, Optional

import numpy as np

_ROOT = Path(__file__).resolve().parent.parent
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))

from common import audio as A  # noqa: E402
from common import iofs  # noqa: E402
from common import schemas  # noqa: E402
from common.context import OperatorContext  # noqa: E402

OPERATOR_ID = "avp_speed_perturb"
OPERATOR_NAME = "语速变换加扰"

# 进度日志刷新间隔（非业务阈值）
PROGRESS_EVERY = 100

_RE_SAMPLE = re.compile(r"[^\w.\-]+", re.UNICODE)
_MAX_STEM_LEN = 60


# --------------------------------------------------------------------------- #
# 参数
# --------------------------------------------------------------------------- #
def _parse_params(params: dict, cfg: dict) -> dict:
    rule = cfg["speed"]
    schemas.require(params, ("values",))

    allowed = [str(v).strip().lower() for v in rule["allowed_values"]]
    raw_values = schemas.as_list(params.get("values"), "values")
    values: List[str] = []
    for raw in raw_values:
        value = str(raw).strip().lower()
        if value not in allowed:
            raise schemas.ParamError(
                "values 取值 %r 非法，允许：%s" % (raw, " / ".join(allowed))
            )
        values.append(value)
    if len(set(values)) != len(values):
        raise schemas.ParamError("values 不能包含重复取值：%r" % raw_values)

    scope_mode = str(params.get("scope_mode") or "all").strip().lower()
    if scope_mode not in ("all", "count"):
        raise schemas.ParamError("scope_mode 取值 %r 非法，允许：all / count" % scope_mode)
    scope_count = None
    if scope_mode == "count":
        if params.get("scope_count") in (None, ""):
            raise schemas.ParamError("scope_mode=count 时必须提供 scope_count")
        scope_count = schemas.as_int(params.get("scope_count"), "scope_count", lo=1)

    return {
        "values": values,
        "scope_mode": scope_mode,
        "scope_count": scope_count,
        "seed": schemas.as_int(
            params.get("seed", rule["default_seed"]), "seed", lo=0, hi=2**32 - 1
        ),
        "per_value": schemas.as_bool(
            params.get("per_value"), "per_value", default=bool(rule["per_value_default"])
        ),
        "passthrough_value": str(rule["passthrough_value"]).strip().lower(),
        "max_duration_error_pct": float(rule["max_duration_error_pct"]),
    }


def _rate_of(value: str) -> float:
    """``"1.2x"`` → ``1.2``。取值已在参数层校验过枚举，这里只做解析。"""
    return float(str(value).strip().lower().rstrip("x"))


# --------------------------------------------------------------------------- #
# 输入
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


def _sample_id(uttid: str, value: str, seq: int) -> str:
    stem = _RE_SAMPLE.sub("_", str(uttid)).strip("._-") or "utt"
    stem = stem[:_MAX_STEM_LEN]
    return "%s__speed__%s__%04d" % (stem, str(value).replace(".", "p"), int(seq))


def short(value, limit: int = 120) -> str:
    text = str(value).replace("\n", " ").replace("\r", " ")
    return text if len(text) <= limit else text[: limit - 1] + "…"


# --------------------------------------------------------------------------- #
# 单条变换
# --------------------------------------------------------------------------- #
def _passthrough_copy(src: Path, dst: Path) -> bool:
    """1.0× 原样透传：复制文件字节，返回是否与源文件逐字节一致。"""
    shutil.copyfile(src, dst)
    return A.sha256_file(src) == A.sha256_file(dst)


def _stretch_to_wav(src: Path, dst: Path, rate: float) -> None:
    """非 1.0×：解码 → time_stretch（变速不变调）→ PCM16 写出。"""
    y, sr = A.load_raw(src)
    out = A.time_stretch(y, float(rate))
    if out.size == 0:
        raise ValueError("time_stretch 输出为空")
    A.write_pcm16(dst, out, sr)


# --------------------------------------------------------------------------- #
# 主流程
# --------------------------------------------------------------------------- #
def run(ctx: OperatorContext) -> dict:
    cfg = ctx.config
    p = _parse_params(ctx.params or {}, cfg)
    max_listed = int(cfg["io"]["max_listed"])
    out_subdir = str(cfg["io"]["output_subdir"])

    ctx.log.route("读取标准语料 → 语速变换（1.0× 原样透传）→ 时长误差复核 → 落盘")

    input_path = _resolve_input(ctx, str(cfg["io"]["input_filename"]))
    rows = _read_rows(input_path)
    ctx.log.validated(input_path.name)
    ctx.log.read(len(rows), "条标准语料记录")

    if p["scope_mode"] == "count" and int(p["scope_count"]) > len(rows):
        raise schemas.ParamError(
            "scope_count（%d）不得超过输入语料样本数（%d）" % (p["scope_count"], len(rows))
        )
    selected = rows[: int(p["scope_count"])] if p["scope_mode"] == "count" else list(rows)

    audio_dir = iofs.ensure_dir(ctx.out(out_subdir))
    rng = np.random.default_rng(p["seed"])

    out_rows: List[dict] = []
    samples: List[dict] = []
    rejects: List[dict] = []
    by_value: Dict[str, dict] = {
        v: {"value": v, "speed": _rate_of(v), "count": 0, "duration_error_max_pct": 0.0,
            "passthrough_checked": 0, "passthrough_identical": 0}
        for v in p["values"]
    }
    duration_errors: List[float] = []
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
            info_in = A.file_info(src)
        except A.AudioDecodeError as exc:
            rejects.append({"line": idx, "uttid": uttid, "reason": "decode_failed", "detail": short(exc)})
            continue
        src_sr = int(info_in["sample_rate"])
        src_duration = float(info_in["frames"]) / float(src_sr) if src_sr > 0 else 0.0
        if src_duration <= 0:
            rejects.append({"line": idx, "uttid": uttid, "reason": "empty_audio", "detail": "源音频 0 采样点"})
            continue

        if p["per_value"]:
            value_list = list(p["values"])
        else:
            value_list = [p["values"][int(rng.integers(0, len(p["values"])))]]

        for value in value_list:
            seq += 1
            rate = _rate_of(value)
            expected_duration = src_duration / rate
            sample_id = _sample_id(uttid, value, seq)
            dst = audio_dir / ("%s.wav" % sample_id)
            passthrough_identical: Optional[bool] = None

            try:
                if value == p["passthrough_value"] and str(info_in["format"]) == "wav" and str(
                    info_in["subtype"]
                ) == "pcm_16":
                    passthrough_identical = _passthrough_copy(src, dst)
                else:
                    if value == p["passthrough_value"]:
                        ctx.log.warning(
                            "%s 不是 16bit PCM WAV，1.0× 无法逐字节透传，退化为重新编码"
                            % Path(src).name
                        )
                    _stretch_to_wav(src, dst, rate)
                    if value == p["passthrough_value"]:
                        passthrough_identical = False
                duration = A.file_duration(dst)
            except A.AudioDecodeError as exc:
                rejects.append(
                    {"line": idx, "uttid": uttid, "value": value, "reason": "decode_failed", "detail": short(exc)}
                )
                continue
            except Exception as exc:  # noqa: BLE001 - 单条失败不拖垮整批
                rejects.append(
                    {"line": idx, "uttid": uttid, "value": value, "reason": "stretch_failed", "detail": short(exc)}
                )
                continue

            error_pct = abs(float(duration) - expected_duration) / expected_duration * 100.0
            duration_errors.append(error_pct)

            out_rows.append(
                {
                    "sample_id": sample_id,
                    "source_uttid": uttid,
                    "dataset_id": row.get("dataset_id", ""),
                    "audio_path": str(dst),
                    "text": row.get("text", ""),
                    "duration": round(float(duration), 3),
                    "perturb": {
                        "dimension": "speed",
                        "value": value,
                        "snr_db": None,
                        "speed": float(rate),
                        "asset": None,
                    },
                }
            )
            samples.append(
                {
                    "sample_id": sample_id,
                    "source_uttid": uttid,
                    "value": value,
                    "speed": float(rate),
                    "source_duration": round(src_duration, 6),
                    "expected_duration": round(expected_duration, 6),
                    "duration": round(float(duration), 3),
                    "duration_exact": round(float(duration), 6),
                    "duration_error_pct": round(error_pct, 6),
                    "sample_rate": src_sr,
                    "passthrough": value == p["passthrough_value"],
                    "passthrough_identical": passthrough_identical,
                    "audio_path": str(dst),
                }
            )
            stats_v = by_value[value]
            stats_v["count"] += 1
            stats_v["duration_error_max_pct"] = max(stats_v["duration_error_max_pct"], error_pct)
            if passthrough_identical is not None:
                stats_v["passthrough_checked"] += 1
                stats_v["passthrough_identical"] += 1 if passthrough_identical else 0

    if not out_rows:
        raise schemas.OpError(
            "decode_failed: 批内没有成功产出变速样本（输入 %d 条，失败 %d 条），详见 rejects"
            % (total, len(rejects))
        )

    max_error_allowed = p["max_duration_error_pct"]
    violations = sum(1 for err in duration_errors if err > max_error_allowed)
    mean_pct = round(float(np.mean(duration_errors)), 6) if duration_errors else 0.0
    max_pct = round(float(np.max(duration_errors)), 6) if duration_errors else 0.0

    passthrough_checked = sum(1 for s in samples if s["passthrough"])
    passthrough_identical = sum(1 for s in samples if s["passthrough"] and s["passthrough_identical"])
    passthrough_rate = (
        round(float(passthrough_identical) / float(passthrough_checked), 4)
        if passthrough_checked
        else None
    )

    value_summaries = []
    for value in p["values"]:
        stats_v = by_value[value]
        count = int(stats_v["count"])
        checked = int(stats_v["passthrough_checked"])
        value_summaries.append(
            {
                "value": value,
                "speed": float(stats_v["speed"]),
                "count": count,
                "duration_error_max_pct": round(stats_v["duration_error_max_pct"], 6),
                "passthrough_checked": checked,
                "passthrough_identical": int(stats_v["passthrough_identical"]),
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
        "duration_error_mean_pct": mean_pct,
        "duration_error_max_pct": max_pct,
        "duration_error_threshold_pct": max_error_allowed,
        "duration_error_violations": violations,
        "passthrough_checked": passthrough_checked,
        "passthrough_identical": passthrough_identical,
        "passthrough_identical_rate": passthrough_rate,
        "total_duration_sec": round(sum(float(r["duration"]) for r in out_rows), 3),
        "sample_rate_preserved": True,
    }

    status = (
        "partial"
        if (rejects or violations or (passthrough_checked and passthrough_identical < passthrough_checked))
        else "success"
    )

    report = {
        "operator": {"id": OPERATOR_ID, "name": OPERATOR_NAME},
        "params": {
            "values": list(p["values"]),
            "scope_mode": p["scope_mode"],
            "scope_count": p["scope_count"],
            "seed": p["seed"],
            "per_value": p["per_value"],
        },
        "statistics": statistics,
        "speed": {
            "allowed_values": [str(v) for v in cfg["speed"]["allowed_values"]],
            "passthrough_value": p["passthrough_value"],
            "duration_error": {
                "definition": "|输出时长 - 源时长/倍率| / (源时长/倍率) × 100%，时长从落盘文件头读取",
                "unit": "percent",
                "mean_pct": mean_pct,
                "max_pct": max_pct,
                "threshold_pct": max_error_allowed,
                "violations": violations,
            },
            "passthrough": {
                "checked": passthrough_checked,
                "identical": passthrough_identical,
                "rate": passthrough_rate,
                "definition": "1.0× 直接复制 02 写出的 WAV PCM16 字节，逐字节比对 sha256",
            },
            "by_value": value_summaries,
        },
        "samples": samples[:max_listed],
        "rejects": rejects[:max_listed],
        "truncated": {
            "samples": max(0, len(samples) - max_listed),
            "rejects": max(0, len(rejects) - max_listed),
        },
        "conclusion": status,
    }

    batch_path = iofs.write_jsonl(ctx.out("004_batch.jsonl"), out_rows)
    report_path = iofs.write_json(ctx.out("004_speed_report.json"), report)

    ctx.log.write_output(batch_path.name)
    ctx.log.write_output(report_path.name)

    if violations:
        ctx.log.warning(
            "%d 条样本时长误差超过 %.2f%% 阈值（最大 %.6f%%），批次标记 partial"
            % (violations, max_error_allowed, max_pct)
        )
    if passthrough_checked and passthrough_identical < passthrough_checked:
        ctx.log.warning(
            "1.0× 逐字节透传 %d/%d 条一致，存在非 PCM16 WAV 输入" % (passthrough_identical, passthrough_checked)
        )
    if rejects:
        ctx.log.error("变速", "%d 条样本未产出（解码/写盘异常）" % len(rejects))

    if status == "partial":
        ctx.log.partial(
            "批次存在未完成/未达标项",
            "成功 %d 条，失败 %d 条，时长误差超阈值 %d 条"
            % (len(out_rows), len(rejects), violations),
        )
    else:
        ctx.log.result(
            "变速",
            output=len(out_rows),
            success=len(out_rows),
            failed=len(rejects),
            extra={"时长误差均值": "%.6f%%" % mean_pct},
        )

    return {
        "outputs": {"batch": batch_path, "report": report_path},
        "statistics": statistics,
        "result": {
            "values": list(p["values"]),
            "generated": len(out_rows),
            "rejected": len(rejects),
            "duration_error_mean_pct": mean_pct,
            "duration_error_max_pct": max_pct,
            "passthrough_identical_rate": passthrough_rate,
            "conclusion": status,
        },
        "status": status,
    }


__all__ = ["run", "OPERATOR_ID", "OPERATOR_NAME"]
