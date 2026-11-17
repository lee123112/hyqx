# -*- coding: utf-8 -*-
"""02 语音标准化（avp_normalize_audio）。

职责
----
把 01 语料入库产出的 ``001_corpus.jsonl`` 统一成规则要求的**采样率 / 声道 / 格式 / 时长范围**，
产出 ``002_std.jsonl``，供加扰加工引用。

业务硬口径（任务书 §2 + 规范 §11.3，不得自行改动）
---------------------------------------------------
- 目标格式固定 **WAV（PCM 16bit）**；目标声道本期固定 **单声道**；
- 目标采样率由 ``params.sample_rate`` 指定，允许 **16k / 8k**；
- 时长**超出 [min_duration, max_duration] 直接剔除，不裁剪**，剔除记录写 ``002_dropped.jsonl``；
- ``002_std.jsonl`` = 001 字段 + ``{std_audio_path, sample_rate, channels, format}``；
- 无干净参考，若输出信噪比只能**盲估**，必须标 ``snr_source="estimated"``。

技术方案
--------
1. 逐条解码（``soundfile``/libsndfile）为 float32 单声道并去直流；
2. 用**解码得到的真实时长**（而不是清单里的旧 duration）判定是否在范围内，
   边界比较带 ``duration_tolerance_sec`` 容差（半帧级浮点噪声），超范围直接剔除；
3. 需要变采样率时用 librosa/soxr 高质量重采样（``soxr_hq``，缺失时回退 kaiser_best）；
4. 落盘统一走 ``common.audio.write_pcm16``（峰值 0.99 等比限幅，PCM16 量化）；
5. 写盘后**重新读文件头复核**一遍格式（采样率/声道/容器/PCM16），得到真实「格式合规率」，
   不靠"写了就应该对"的自证；
6. 标准化后逐条用 ``common.audio.estimate_snr_db`` 做**盲估计**，只作为补充质量信号
   写进报告并标注 ``estimated``，不做剔除依据（业务口径里剔除只看时长）。

确定性与失败口径
----------------
- 全程无随机，同输入同参数 → 同输出（音频逐字节一致，测试覆盖）；
- 单条解码失败 / 音频缺失 / 缺 uttid → 计入 ``rejects`` 并继续（``partial``），整批没有可读输入才抛错；
- 全部样本因超范围被剔除属于**正常业务结果**（成功，输出 0 条），日志明确告警，不伪造成功。
"""

from __future__ import annotations

import re
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

OPERATOR_ID = "avp_normalize_audio"
OPERATOR_NAME = "语音标准化"

# 输入行必备字段（001 契约保证；缺失即该行失败，不猜）
REQUIRED_FIELDS = ("uttid", "audio_path")

# 进度日志刷新间隔（非业务阈值）
PROGRESS_EVERY = 100

DROPPED_BELOW = "below_min_duration"
DROPPED_ABOVE = "above_max_duration"

# 输出音频文件名允许的字符（保留中文/字母/数字/下划线/点/横线，其余替换掉，防路径穿越）
_SAFE_STEM = re.compile(r"[^\w.\-]+", re.UNICODE)
_MAX_STEM_LEN = 80


# --------------------------------------------------------------------------- #
# 参数
# --------------------------------------------------------------------------- #
def _parse_params(params: dict, cfg: dict) -> dict:
    rule = cfg["normalize"]
    schemas.require(params, ("sample_rate", "channels", "min_duration", "max_duration"))

    allowed_sr = [int(x) for x in rule["allowed_sample_rates"]]
    sample_rate = schemas.enum(
        schemas.as_int(params.get("sample_rate"), "sample_rate"), allowed_sr, "sample_rate"
    )

    allowed_ch = [int(x) for x in rule["allowed_channels"]]
    channels = schemas.enum(
        schemas.as_int(params.get("channels"), "channels"), allowed_ch, "channels"
    )

    allowed_fmt = list(rule["allowed_formats"])
    raw_fmt = params.get("format") or rule["default_format"]
    fmt = schemas.enum(str(raw_fmt).strip(), allowed_fmt, "format")

    min_duration = schemas.as_float(params.get("min_duration"), "min_duration", lo=0.0)
    max_duration = schemas.as_float(params.get("max_duration"), "max_duration", lo=0.0)
    if min_duration >= max_duration:
        raise schemas.ParamError(
            "min_duration（%s）必须小于 max_duration（%s）" % (min_duration, max_duration)
        )

    return {
        "sample_rate": int(sample_rate),
        "channels": int(channels),
        "format": fmt,
        "min_duration": float(min_duration),
        "max_duration": float(max_duration),
        "remove_source": schemas.as_bool(params.get("remove_source"), "remove_source", default=False),
        "duration_tolerance": float(rule["duration_tolerance_sec"]),
        "snr_enabled": bool(rule["snr"]["enabled"]),
        "snr_bias_db": float(rule["snr"]["bias_db"]),
    }


# --------------------------------------------------------------------------- #
# 输入解析
# --------------------------------------------------------------------------- #
def _resolve_input(ctx: OperatorContext, filename: str) -> Path:
    """定位 001_corpus.jsonl。

    支持三种形态（与 ``inference.py`` 的上传/CLI 行为对齐）：

    1. ``inputFile`` 直接是 jsonl 文件；
    2. ``inputFile`` 是目录（zip 解压目录 / 任务输入目录）：优先同名文件，其次目录里唯一的 jsonl；
    3. 未传 ``inputFile``：按 ``work_dir/input/extracted`` → ``work_dir/input`` 兜底。

    找不到就抛 ``missing_file``，不静默换源。
    """
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
    """把行里的 audio_path 解析成真实文件；相对路径依次相对语料表目录、任务输入目录、工作目录。"""
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


def _audio_filename(uttid: str, used: set) -> str:
    stem = _SAFE_STEM.sub("_", str(uttid)).strip("._-") or "utt"
    stem = stem[:_MAX_STEM_LEN]
    name = stem
    seq = 2
    while name in used:
        name = "%s_%d" % (stem, seq)
        seq += 1
    used.add(name)
    return name + ".wav"


def _format_ok(info: dict, sample_rate: int, channels: int) -> bool:
    """落盘后复核：WAV 容器 + PCM_16 + 目标采样率/声道。"""
    return (
        int(info["sample_rate"]) == int(sample_rate)
        and int(info["channels"]) == int(channels)
        and str(info["format"]) == "wav"
        and str(info["subtype"]) == "pcm_16"
    )


# --------------------------------------------------------------------------- #
# 主流程
# --------------------------------------------------------------------------- #
def run(ctx: OperatorContext) -> dict:
    cfg = ctx.config
    p = _parse_params(ctx.params or {}, cfg)
    max_listed = int(cfg["io"]["max_listed"])
    out_subdir = str(cfg["io"]["output_subdir"])

    ctx.log.route("语料表读取 → 时长判定（超范围剔除）→ 重采样/单声道/PCM16 → 格式复核")

    input_path = _resolve_input(ctx, str(cfg["io"]["input_filename"]))
    rows = _read_rows(input_path)
    ctx.log.validated(input_path.name)
    ctx.log.read(len(rows), "条语料记录")

    audio_dir = iofs.ensure_dir(ctx.out(out_subdir))
    used_names: set = set()

    out_rows: List[dict] = []
    dropped: List[dict] = []
    rejects: List[dict] = []
    samples: List[dict] = []
    snr_values: List[float] = []
    duration_errors_sec: List[float] = []
    duration_errors_pct: List[float] = []
    format_checked = 0
    format_compliant = 0
    sources_to_remove: set = set()

    total = len(rows)
    for idx, row in enumerate(rows, start=1):
        if idx % PROGRESS_EVERY == 0 or idx == total:
            ctx.log.progress(idx, total)

        uttid = str(row.get("uttid") or "").strip()
        missing = [key for key in REQUIRED_FIELDS if row.get(key) in (None, "")]
        if missing:
            rejects.append(
                {
                    "line": idx,
                    "uttid": uttid,
                    "reason": "missing_field",
                    "detail": "缺少字段：%s" % ", ".join(missing),
                }
            )
            continue

        src = _resolve_audio(row.get("audio_path"), input_path, Path(ctx.work_dir))
        if src is None:
            rejects.append(
                {
                    "line": idx,
                    "uttid": uttid,
                    "reason": "audio_not_found",
                    "detail": "音频文件不存在：%s" % Path(str(row.get("audio_path"))).name,
                }
            )
            continue

        try:
            raw, native_sr = A.load_raw(src)
            in_info = A.file_info(src)
        except A.AudioDecodeError as exc:
            rejects.append({"line": idx, "uttid": uttid, "reason": "decode_failed", "detail": str(exc)})
            continue

        in_duration = float(raw.size) / float(native_sr) if native_sr > 0 else 0.0
        tol = p["duration_tolerance"]
        if in_duration < p["min_duration"] - tol:
            dropped.append(
                {
                    "dataset_id": row.get("dataset_id", ""),
                    "uttid": uttid,
                    "audio_path": str(src),
                    "text": row.get("text", ""),
                    "duration": round(in_duration, 3),
                    "reason": DROPPED_BELOW,
                    "min_duration": p["min_duration"],
                    "max_duration": p["max_duration"],
                }
            )
            continue
        if in_duration > p["max_duration"] + tol:
            dropped.append(
                {
                    "dataset_id": row.get("dataset_id", ""),
                    "uttid": uttid,
                    "audio_path": str(src),
                    "text": row.get("text", ""),
                    "duration": round(in_duration, 3),
                    "reason": DROPPED_ABOVE,
                    "min_duration": p["min_duration"],
                    "max_duration": p["max_duration"],
                }
            )
            continue

        try:
            y = A.resample(raw, native_sr, p["sample_rate"])
        except Exception as exc:  # noqa: BLE001 - 单条失败不拖垮整批
            rejects.append(
                {"line": idx, "uttid": uttid, "reason": "resample_failed", "detail": short(exc)}
            )
            continue

        out_duration = float(y.size) / float(p["sample_rate"])
        dst = audio_dir / _audio_filename(uttid, used_names)
        try:
            A.write_pcm16(dst, y, p["sample_rate"])
            out_info = A.file_info(dst)
        except Exception as exc:  # noqa: BLE001
            rejects.append(
                {"line": idx, "uttid": uttid, "reason": "write_failed", "detail": short(exc)}
            )
            continue

        compliant = _format_ok(out_info, p["sample_rate"], p["channels"])
        format_checked += 1
        if compliant:
            format_compliant += 1
        else:
            ctx.log.warning("%s 写盘后格式复核未通过，已计入格式违规" % uttid)

        snr_value: Optional[float] = None
        if p["snr_enabled"]:
            snr_value = A.estimate_snr_db(y, p["sample_rate"], bias_db=p["snr_bias_db"])
            snr_values.append(float(snr_value))

        error_sec = abs(out_duration - in_duration)
        error_pct = (error_sec / in_duration * 100.0) if in_duration > 0 else 0.0
        duration_errors_sec.append(error_sec)
        duration_errors_pct.append(error_pct)

        out_row = dict(row)
        # 契约要求保留 001 的 duration；标准化后的真实时长另放 std_duration，避免改字段语义
        out_row.setdefault("duration", round(in_duration, 3))
        out_row.update(
            {
                "std_audio_path": str(dst),
                "sample_rate": p["sample_rate"],
                "channels": p["channels"],
                "format": p["format"],
                "std_duration": round(out_duration, 3),
            }
        )
        out_rows.append(out_row)

        samples.append(
            {
                "uttid": uttid,
                "source_audio_path": str(src),
                "std_audio_path": str(dst),
                "source_sample_rate": int(native_sr),
                "source_channels": int(in_info["channels"]),
                "source_format": str(in_info["format"]),
                "source_duration": round(in_duration, 3),
                "std_duration": round(out_duration, 3),
                "duration_error_sec": round(error_sec, 6),
                "duration_error_pct": round(error_pct, 4),
                "snr_db": None if snr_value is None else round(float(snr_value), 3),
                "snr_source": "estimated" if snr_value is not None else None,
                "format_compliant": bool(compliant),
            }
        )
        sources_to_remove.add(src)

    # 删原音频（默认关闭；只允许删任务工作目录内的文件，防止越界删用户数据）
    source_removed = 0
    if p["remove_source"]:
        work_root = Path(ctx.work_dir).resolve()
        for src in sorted(sources_to_remove):
            try:
                resolved = Path(src).resolve()
                if work_root in resolved.parents:
                    resolved.unlink()
                    source_removed += 1
                else:
                    ctx.log.warning("原音频不在任务工作目录内，跳过删除：%s" % Path(src).name)
            except OSError as exc:
                ctx.log.warning("删除原音频失败：%s（%s）" % (Path(src).name, short(exc)))

    format_compliance_rate = (
        round(float(format_compliant) / float(format_checked), 4) if format_checked else 0.0
    )
    duration_error_mean_sec = (
        round(sum(duration_errors_sec) / float(len(duration_errors_sec)), 6)
        if duration_errors_sec
        else 0.0
    )
    duration_error_max_sec = round(max(duration_errors_sec), 6) if duration_errors_sec else 0.0
    duration_error_max_pct = round(max(duration_errors_pct), 4) if duration_errors_pct else 0.0

    statistics = {
        "input_records": total,
        "output": len(out_rows),
        "dropped": len(dropped),
        "dropped_below_min": sum(1 for d in dropped if d["reason"] == DROPPED_BELOW),
        "dropped_above_max": sum(1 for d in dropped if d["reason"] == DROPPED_ABOVE),
        "rejected": len(rejects),
        "format_checked": format_checked,
        "format_compliant": format_compliant,
        "format_compliance_rate": format_compliance_rate,
        "duration_error_mean_sec": duration_error_mean_sec,
        "duration_error_max_sec": duration_error_max_sec,
        "duration_error_max_pct": duration_error_max_pct,
        "total_duration_sec": round(sum(float(r["std_duration"]) for r in out_rows), 3),
        "source_removed": source_removed,
        "target_sample_rate": p["sample_rate"],
        "target_channels": p["channels"],
        "target_format": p["format"],
    }
    if snr_values:
        statistics.update(
            {
                "snr_source": "estimated",
                "snr_estimated_mean_db": round(sum(snr_values) / float(len(snr_values)), 3),
                "snr_estimated_min_db": round(min(snr_values), 3),
                "snr_estimated_max_db": round(max(snr_values), 3),
            }
        )

    status = "partial" if (rejects or (format_checked and format_compliant < format_checked)) else "success"

    report = {
        "operator": {"id": OPERATOR_ID, "name": OPERATOR_NAME},
        "params": {
            "sample_rate": p["sample_rate"],
            "channels": p["channels"],
            "format": p["format"],
            "min_duration": p["min_duration"],
            "max_duration": p["max_duration"],
            "remove_source": p["remove_source"],
        },
        "statistics": statistics,
        "normalize": {
            "target": {
                "sample_rate": p["sample_rate"],
                "channels": p["channels"],
                "format": p["format"],
                "container": "wav",
                "subtype": "pcm_16",
            },
            "duration_range": [p["min_duration"], p["max_duration"]],
            "duration_tolerance_sec": p["duration_tolerance"],
            "format_compliance": {
                "checked": format_checked,
                "compliant": format_compliant,
                "rate": format_compliance_rate,
            },
            "duration_error": {
                "unit": "sec / percent",
                "mean_sec": duration_error_mean_sec,
                "max_sec": duration_error_max_sec,
                "max_pct": duration_error_max_pct,
            },
        },
        "samples": samples[:max_listed],
        "dropped": dropped[:max_listed],
        "rejects": rejects[:max_listed],
        "truncated": {
            "samples": max(0, len(samples) - max_listed),
            "dropped": max(0, len(dropped) - max_listed),
            "rejects": max(0, len(rejects) - max_listed),
        },
        "conclusion": status,
    }
    if snr_values:
        report["normalize"]["snr_estimated"] = {
            "snr_source": "estimated",
            "count": len(snr_values),
            "mean_db": statistics["snr_estimated_mean_db"],
            "min_db": statistics["snr_estimated_min_db"],
            "max_db": statistics["snr_estimated_max_db"],
            "note": "无干净参考，使用 common.audio.estimate_snr_db 盲估；实测系统性偏高约 1.0–2.5 dB，未做偏置修正，仅作补充质量信号",
        }

    corpus_path = iofs.write_jsonl(ctx.out("002_std.jsonl"), out_rows)
    dropped_path = iofs.write_jsonl(ctx.out("002_dropped.jsonl"), dropped)
    report_path = iofs.write_json(ctx.out("002_normalize_report.json"), report)

    ctx.log.write_output(corpus_path.name)
    ctx.log.write_output(dropped_path.name)
    ctx.log.write_output(report_path.name)

    if dropped:
        ctx.log.warning(
            "%d 条样本超出时长范围 [%s, %s]s 已剔除（下限 %d 条、上限 %d 条），未裁剪"
            % (
                len(dropped),
                p["min_duration"],
                p["max_duration"],
                statistics["dropped_below_min"],
                statistics["dropped_above_max"],
            )
        )
    if rejects:
        ctx.log.error("标准化", "%d 条样本解码/字段异常，已计入 rejects" % len(rejects))

    if status == "partial":
        ctx.log.partial(
            "部分样本未完成标准化",
            "成功 %d 条，剔除 %d 条，失败 %d 条" % (len(out_rows), len(dropped), len(rejects)),
        )
    else:
        ctx.log.result(
            "标准化",
            output=len(out_rows),
            success=len(out_rows),
            skipped=len(dropped),
            failed=len(rejects),
        )
        if not out_rows and dropped:
            ctx.log.warning("全部样本均超出时长范围，标准化结果为空（业务口径：剔除不裁剪）")

    return {
        "outputs": {"corpus": corpus_path, "dropped": dropped_path, "report": report_path},
        "statistics": statistics,
        "result": {
            "sample_rate": p["sample_rate"],
            "channels": p["channels"],
            "format": p["format"],
            "min_duration": p["min_duration"],
            "max_duration": p["max_duration"],
            "kept": len(out_rows),
            "dropped": len(dropped),
            "rejected": len(rejects),
            "format_compliance_rate": format_compliance_rate,
            "duration_error_max_pct": duration_error_max_pct,
            "conclusion": status,
        },
        "status": status,
    }


def short(value, limit: int = 120) -> str:
    """单行短文本（日志/报告用，不塞堆栈）。"""
    text = str(value).replace("\n", " ").replace("\r", " ")
    return text if len(text) <= limit else text[: limit - 1] + "…"


__all__ = ["run", "OPERATOR_ID", "OPERATOR_NAME"]
