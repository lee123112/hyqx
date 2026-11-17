# -*- coding: utf-8 -*-
"""11 优化建议报告（avp_generate_report）。

职责
----
把评测结果（007）、差异归因（008）、优化建议（009）汇编成一份**八段式**优化建议报告，
同时产出 Markdown 与**自包含** HTML（内联 CSS，浏览器打开即可打印成 PDF）。

八段固定为（规范 §11 第 12 条；原型标注里写的「七段」是笔误，按八段落地）：

1. 结论摘要
2. 整体指标
3. 分场景表现
4. 噪声场景下钻
5. 错误形态
6. 原因分析
7. 优化建议
8. 风险与限制

业务硬口径
----------
- 缺哪个输入就在报告里如实写「未接入」，**不许编数**；
- 所有数字来自输入或由输入直接换算（如完成率、占比），不接大模型、不做二次推算；
- HTML 自包含：不引外部 CDN / JS / CSS，不用 reportlab / LibreOffice；
- 报告类型本期只有「优化建议报告」，不做质控/质量缺陷等其他报告。

技术方案
--------
纯规则 + 模板渲染，无模型、无随机：

1. 输入识别：递归扫描输入目录（或单个文件），按文件名特征归类 007/008/009；
   也支持一个 JSON bundle 里同时带三份输入；
2. 口径归一：指标/占比/主因占比支持「比例值」与「百分数」两种登记习惯，
   由配置的 ``*_unit=auto`` 统一判定后换算成百分数展示；
3. 结构装配：先构造与渲染无关的中间模型（章节 → 区块 → 行/表/链），
   Markdown 与 HTML 两个渲染器读同一份模型，保证两种格式数字完全一致；
4. 可追溯：``011_report_meta.json`` 记录输入来源、关键数字与告警，供报告中心归档检索。
"""

from __future__ import annotations

import datetime as _dt
import difflib as _difflib
import html as _html
import re
import sys
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional, Tuple

_ROOT = Path(__file__).resolve().parent.parent
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))

from common import iofs  # noqa: E402
from common import schemas  # noqa: E402
from common.context import OperatorContext  # noqa: E402

OPERATOR_ID = "avp_generate_report"
OPERATOR_NAME = "优化建议报告"

SECTION_UNAVAILABLE_TEXT = "未接入"

CALIBER = {
    "sections": "固定八段：结论摘要/整体指标/分场景表现/噪声场景下钻/错误形态/原因分析/优化建议/风险与限制",
    "numbers": "所有数字来自 007/008/009 输入或由输入直接换算，未接大模型、未做二次推算",
    "html": "自包含 HTML（内联 CSS，不引外部 CDN/JS/CSS），浏览器打开即可打印成 PDF",
}


# --------------------------------------------------------------------------- #
# 参数
# --------------------------------------------------------------------------- #
def _parse_params(params: dict, cfg: dict) -> dict:
    formats = [str(x) for x in cfg["io"]["formats"]]
    fmt = str(params.get("format") or cfg["io"]["default_format"]).strip().lower()
    if fmt not in formats:
        raise schemas.ParamError("format 取值 %r 非法，允许：%s" % (fmt, " / ".join(formats)))

    report_no = params.get("report_no")
    if report_no is not None:
        report_no = schemas.as_str(report_no, "report_no", max_len=int(cfg["report"]["report_no_max_len"]))

    title = params.get("title")
    if title is not None:
        title = schemas.as_str(title, "title", max_len=int(cfg["report"]["title_max_len"]))

    include = params.get("include_sections")
    sections = [str(x) for x in cfg["report"]["sections"]]
    if include is not None:
        raw_items = schemas.as_list(include, "include_sections")
        chosen: List[str] = []
        for raw in raw_items:
            key = _section_key(raw, sections)
            if key is None:
                raise schemas.ParamError(
                    "include_sections 含未知章节 %r，允许：%s" % (raw, " / ".join(sections))
                )
            if key not in chosen:
                chosen.append(key)
        if not chosen:
            raise schemas.ParamError("include_sections 不能为空；不裁剪章节时请省略该参数")
        sections = [name for name in sections if name in chosen]

    return {"format": fmt, "report_no": report_no, "title": title, "sections": sections}


def _section_key(raw: Any, sections: List[str]) -> Optional[str]:
    text = str(raw or "").strip()
    if text in sections:
        return text
    stripped = re.sub(r"^[\s0-9０-９.、,，:：\-—]+", "", text)
    stripped = re.sub(r"^第[一二三四五六七八九十\d]+[章节段]\s*", "", stripped)
    stripped = stripped.strip()
    return stripped if stripped in sections else None


# --------------------------------------------------------------------------- #
# 输入识别
# --------------------------------------------------------------------------- #
def _load_artifacts(ctx: OperatorContext, cfg: dict) -> dict:
    """读取 007/008/009 输入；缺哪份就返回 None，不编造。"""
    inp = ctx.require_input()
    exts = tuple(str(x).lower() for x in cfg["io"]["input_exts"])
    if inp.is_dir():
        files = _scan_dir(inp, exts)
    else:
        files = _scan_dir(inp.parent, exts)
        if inp not in files:
            files.insert(0, inp)
    if not files:
        raise schemas.OpError("missing_file: 输入中没有可识别的 JSON/JSONL 文件")

    eval_rows: List[dict] = []
    attribution: Optional[dict] = None
    advice: Optional[dict] = None
    sources: List[dict] = []
    warnings: List[str] = []
    eval_files: List[str] = []
    invalid_eval = 0
    eval_seen = False

    for path in files:
        kind_payload = _read_payload(path)
        if kind_payload is None:
            warnings.append("%s：不是合法 JSON/JSONL，已跳过" % path.name)
            continue
        payload_kind, payload, bad_rows = kind_payload
        invalid_eval += bad_rows

        bundle = _as_bundle(payload, cfg)
        if bundle is not None:
            if bundle.get("eval") is not None:
                rows, bad = _eval_rows_from(bundle["eval"])
                eval_rows.extend(rows)
                invalid_eval += bad
                eval_seen = True
                eval_files.append(path.name)
            if bundle.get("attribution") is not None and attribution is None:
                attribution = _as_attribution(bundle["attribution"], path.name, warnings, cfg)
            if bundle.get("advice") is not None and advice is None:
                advice = _as_advice(bundle["advice"], path.name, warnings, cfg)
            sources.append({"file": path.name, "kind": "bundle"})
            continue

        kind = _classify(path, payload_kind, payload, cfg)
        if kind is None:
            warnings.append("%s：无法识别为 007/008/009 产物，已跳过" % path.name)
            continue
        if kind == "eval":
            rows, bad = _eval_rows_from(payload)
            eval_rows.extend(rows)
            invalid_eval += bad
            eval_seen = True
            eval_files.append(path.name)
            sources.append({"file": path.name, "kind": "eval_results", "records": len(rows)})
        elif kind == "attribution":
            if attribution is not None:
                warnings.append("%s：已有一份差异归因输入，本文件被忽略" % path.name)
                continue
            attribution = _as_attribution(payload, path.name, warnings, cfg)
            sources.append({"file": path.name, "kind": "attribution"})
        else:
            if advice is not None:
                warnings.append("%s：已有一份优化建议输入，本文件被忽略" % path.name)
                continue
            advice = _as_advice(payload, path.name, warnings, cfg)
            sources.append({"file": path.name, "kind": "advice"})

    if invalid_eval:
        warnings.append("%d 条评测结果缺少可用指标，已跳过" % invalid_eval)
    if eval_seen and not eval_rows and attribution is None and advice is None:
        raise schemas.OpError("invalid_jsonl: 007 评测结果中没有可用记录（缺 metrics.cer/wer/ser）")
    if not eval_rows and attribution is None and advice is None:
        raise schemas.OpError(
            "missing_file: 输入中未找到评测结果(007)/差异归因(008)/优化建议(009)任何产物"
        )

    return {
        "eval_rows": eval_rows,
        "attribution": attribution,
        "advice": advice,
        "sources": sources,
        "warnings": warnings,
        "eval_files": eval_files,
    }


def _scan_dir(root: Path, exts: Tuple[str, ...]) -> List[Path]:
    root_p = Path(root)
    files = [
        p
        for p in root_p.rglob("*")
        if p.is_file()
        and p.suffix.lower() in exts
        and not p.name.startswith(".")
        and "__MACOSX" not in p.parts
    ]
    return sorted(files, key=lambda p: p.relative_to(root_p).as_posix())


def _read_payload(path: Path) -> Optional[Tuple[str, Any, int]]:
    """返回 ``(kind, payload, bad_rows)``；文件坏到读不了时返回 None。"""
    suffix = path.suffix.lower()
    try:
        if suffix == ".jsonl":
            rows = iofs.read_jsonl(path)
            return "jsonl", rows, 0
        payload = iofs.read_json(path)
        return "json", payload, 0
    except ValueError:
        return None


def _classify(path: Path, payload_kind: str, payload: Any, cfg: dict) -> Optional[str]:
    name = path.name.casefold()
    patterns = (
        ("eval", cfg["discovery"]["eval_patterns"]),
        ("attribution", cfg["discovery"]["attribution_patterns"]),
        ("advice", cfg["discovery"]["advice_patterns"]),
    )
    for kind, keys in patterns:
        if any(str(k).casefold() in name for k in keys):
            return kind
    # 文件名不规范时按内容嗅探
    if isinstance(payload, list):
        first = payload[0] if payload else None
        if isinstance(first, dict) and ("eval_id" in first or "metrics" in first):
            return "eval"
        return None
    if isinstance(payload, dict):
        if "suggestions" in payload or _first_list(payload, cfg["discovery"]["suggestion_keys"]) is not None:
            return "advice"
        if "main_cause" in payload or "record_id" in payload or "factors" in payload:
            return "attribution"
        if "metrics" in payload or "eval_id" in payload:
            return "eval"
    return None


def _as_bundle(payload: Any, cfg: dict) -> Optional[dict]:
    if not isinstance(payload, dict):
        return None
    found: Dict[str, Any] = {}
    for name, key_list in cfg["discovery"]["bundle_keys"].items():
        for key in key_list:
            value = _pick(payload, [key])
            if value is not None:
                found[name] = value
                break
    if not found:
        return None
    return {
        "eval": found.get("eval"),
        "attribution": found.get("attribution"),
        "advice": found.get("advice"),
    }


def _eval_rows_from(payload: Any) -> Tuple[List[dict], int]:
    rows: List[Any]
    if isinstance(payload, list):
        rows = payload
    elif isinstance(payload, dict):
        container = _first_list(payload, ["eval_results", "results", "records", "items"])
        rows = container if container is not None else [payload]
    else:
        return [], 1
    valid: List[dict] = []
    bad = 0
    for row in rows:
        if not isinstance(row, dict) or not _has_metrics(row):
            bad += 1
            continue
        valid.append(row)
    return valid, bad


def _as_attribution(payload: Any, origin: str, warnings: List[str], cfg: dict) -> Optional[dict]:
    if isinstance(payload, list):
        candidates = [x for x in payload if isinstance(x, dict) and x]
        if len(candidates) > 1:
            warnings.append("%s：差异归因含 %d 条记录，采用第一条" % (origin, len(candidates)))
        return candidates[0] if candidates else None
    if isinstance(payload, dict):
        inner = _pick(payload, ["attribution"])
        if isinstance(inner, dict):
            return inner if inner else None
        return payload if payload else None
    return None


def _as_advice(payload: Any, origin: str, warnings: List[str], cfg: dict) -> Optional[dict]:
    if isinstance(payload, list):
        return {"suggestions": [x for x in payload if isinstance(x, dict)]}
    if not isinstance(payload, dict):
        return None
    items = _first_list(payload, [str(x) for x in cfg["discovery"]["suggestion_keys"]])
    if items is None:
        warnings.append("%s：优化建议缺少 suggestions 字段，按空建议处理" % origin)
        items = []
    return {"raw": payload, "suggestions": [x for x in items if isinstance(x, dict)]}


def _first_list(payload: dict, keys: Iterable[str]) -> Optional[List[Any]]:
    for key in keys:
        value = _pick(payload, [key])
        if isinstance(value, list):
            return value
        if isinstance(value, dict):
            return list(value.values())
    return None


def _pick(mapping: dict, keys: Iterable[str]) -> Any:
    low = {str(k).casefold(): v for k, v in mapping.items()}
    for key in keys:
        value = low.get(str(key).casefold())
        if value not in (None, ""):
            return value
    return None


def _has_metrics(row: dict) -> bool:
    metrics = row.get("metrics")
    if not isinstance(metrics, dict):
        return False
    return any(_to_number(metrics.get(key)) is not None for key in ("cer", "wer", "ser"))


# --------------------------------------------------------------------------- #
# 口径换算与格式化
# --------------------------------------------------------------------------- #
def _to_number(value: Any) -> Optional[float]:
    if isinstance(value, bool) or value is None:
        return None
    if isinstance(value, (int, float)):
        return float(value)
    text = str(value).strip().replace(",", "")
    if not text:
        return None
    if text.endswith("%"):
        text = text[:-1].strip()
    try:
        return float(text)
    except ValueError:
        return None


def _to_int(value: Any) -> Optional[int]:
    number = _to_number(value)
    if number is None:
        return None
    return int(round(number))


def _text(value: Any) -> str:
    if value is None:
        return ""
    return str(value).strip()


def _clip(text: str, cfg: dict) -> str:
    limit = int(cfg["limits"]["max_text_len"])
    text = str(text).replace("\r", " ").replace("\n", " ").strip()
    if len(text) <= limit:
        return text
    return text[: max(1, limit - 1)] + "…"


def _decimals(cfg: dict) -> int:
    return int(cfg["report"]["percent_decimals"])


def _fmt_pct(value: Optional[float], cfg: dict) -> str:
    if value is None:
        return "—"
    return ("%." + str(_decimals(cfg)) + "f%%") % float(value)


def _fmt_count(value: Optional[int], cfg: dict) -> str:
    if value is None:
        return "—"
    return "{:,}".format(int(value))


def _fmt_delta(value: Optional[float], cfg: dict) -> str:
    if value is None:
        return "—"
    digits = _decimals(cfg)
    eps = 0.5 * (10 ** -digits)  # 显示精度的一半：避免把「0.0」显示成「+0.0」
    if abs(value) < eps:
        return ("%." + str(digits) + "f") % 0.0
    sign = "+" if value > 0 else ""
    return sign + (("%." + str(digits) + "f") % float(value))


def _fmt_abs(value: Optional[float], cfg: dict) -> str:
    """不带正负号的百分比点值，给「高/低 X 个百分点」这类句式用。"""
    if value is None:
        return "—"
    return ("%." + str(_decimals(cfg)) + "f") % abs(float(value))


def _delta_class(value: Optional[float], cfg: dict) -> str:
    if value is None:
        return ""
    eps = 0.5 * (10 ** -_decimals(cfg))
    if value > eps:
        return "neg"
    if value < -eps:
        return "pos"
    return ""


def _resolve_unit(values: Iterable[Optional[float]], mode: str) -> str:
    """把 auto 口径解析成 ``ratio``（0.35→35%）或 ``percent``（35→35%）。"""
    if mode in ("ratio", "percent"):
        return mode
    numbers = [abs(float(v)) for v in values if v is not None]
    if numbers and all(v <= 1.0 for v in numbers):
        return "ratio"
    return "percent"


def _to_percent(value: Optional[float], mode: str) -> Optional[float]:
    if value is None:
        return None
    return value * 100.0 if mode == "ratio" else value


# --------------------------------------------------------------------------- #
# 时间
# --------------------------------------------------------------------------- #
def _tz(cfg: dict) -> _dt.tzinfo:
    try:
        from zoneinfo import ZoneInfo

        return ZoneInfo(str(cfg["report"]["timezone"]))
    except Exception:  # noqa: BLE001 - 无 tzdata 时按固定偏移兜底
        return _dt.timezone(_dt.timedelta(hours=float(cfg["report"]["utc_offset_hours"])))


def _parse_dt(value: Any, cfg: dict) -> Optional[_dt.datetime]:
    if value in (None, ""):
        return None
    if isinstance(value, _dt.datetime):
        moment = value
    elif isinstance(value, _dt.date):
        moment = _dt.datetime(value.year, value.month, value.day)
    else:
        text = str(value).strip()
        if not text:
            return None
        if text.endswith(("Z", "z")):
            text = text[:-1] + "+00:00"
        text = text.replace(" ", "T", 1) if "T" not in text and " " in text else text
        try:
            moment = _dt.datetime.fromisoformat(text)
        except ValueError:
            moment = None
            for fmt in ("%Y-%m-%dT%H:%M:%S%z", "%Y-%m-%d %H:%M:%S", "%Y-%m-%dT%H:%M", "%Y-%m-%d %H:%M", "%Y-%m-%d"):
                try:
                    moment = _dt.datetime.strptime(str(value).strip(), fmt)
                    break
                except ValueError:
                    continue
            if moment is None:
                return None
    if moment.tzinfo is None:
        moment = moment.replace(tzinfo=_tz(cfg))
    return moment.astimezone(_tz(cfg))


def _moment(value: Any, cfg: dict) -> float:
    moment = _parse_dt(value, cfg)
    return moment.timestamp() if moment is not None else float("-inf")


def _fmt_datetime(moment: _dt.datetime, cfg: dict) -> str:
    return moment.strftime(cfg["report"]["datetime_format"])


# --------------------------------------------------------------------------- #
# 评测结果口径
# --------------------------------------------------------------------------- #
def _metric_block(eval_row: Optional[dict], attribution: Optional[dict], cfg: dict) -> dict:
    """返回整体指标、对比对象指标、样本概况（全部按同一口径换算成百分数）。"""
    raw: Dict[str, Optional[float]] = {"cer": None, "wer": None, "ser": None}
    if eval_row:
        metrics = eval_row.get("metrics") if isinstance(eval_row.get("metrics"), dict) else {}
        for key in raw:
            raw[key] = _to_number(metrics.get(key))
    mode = _resolve_unit(raw.values(), str(cfg["report"]["metric_unit"]))
    current = {key: _to_percent(value, mode) for key, value in raw.items()}

    base_name, base_raw = _baseline_metrics(eval_row, attribution)
    baseline = (
        {key: _to_percent(_to_number(base_raw.get(key)), mode) for key in raw}
        if base_raw
        else {key: None for key in raw}
    )

    samples = None
    abnormal = None
    if eval_row:
        metrics = eval_row.get("metrics") if isinstance(eval_row.get("metrics"), dict) else {}
        # 007 的产物用的字段名是 sample_count（不重算，取输入原值）；
        # total_samples 是 007 的输入字段名，兼容旧数据。
        samples = _to_int(
            metrics.get("samples", eval_row.get("samples"))
            or metrics.get("sample_count")
            or eval_row.get("sample_count")
            or eval_row.get("total_samples")
        )
        abnormal = _to_int(metrics.get("abnormal", eval_row.get("abnormal")))
        if abnormal is None:
            # 007 不单独给 abnormal：报告里的「异常样本」= 未识别条数，即 error_mix.unk
            mix = eval_row.get("error_mix") if isinstance(eval_row.get("error_mix"), dict) else {}
            abnormal = _to_int(mix.get("unk"))
    return {
        "raw": raw,
        "mode": mode,
        "current": current,
        "baseline": baseline,
        "baseline_name": base_name,
        "samples": samples,
        "abnormal": abnormal,
    }


def _baseline_metrics(eval_row: Optional[dict], attribution: Optional[dict]) -> Tuple[str, dict]:
    name = ""
    metrics: Optional[dict] = None
    if eval_row:
        candidate = eval_row.get("baseline_metrics")
        if isinstance(candidate, dict):
            metrics = _only_metrics(candidate)
        base = eval_row.get("baseline")
        if isinstance(base, dict):
            name = name or _text(base.get("name") or base.get("baseline_id") or base.get("id"))
            if metrics is None:
                metrics = _only_metrics(base.get("metrics") if isinstance(base.get("metrics"), dict) else base)
        elif isinstance(base, str):
            name = name or base
    if attribution:
        base = attribution.get("baseline")
        if isinstance(base, dict):
            name = name or _text(base.get("name") or base.get("baseline_id") or base.get("id"))
            if metrics is None:
                metrics = _only_metrics(base.get("metrics") if isinstance(base.get("metrics"), dict) else base)
        elif isinstance(base, str):
            name = name or base
        if metrics is None and isinstance(attribution.get("baseline_metrics"), dict):
            metrics = _only_metrics(attribution["baseline_metrics"])
    return name, metrics or {}


def _only_metrics(container: Any) -> dict:
    if not isinstance(container, dict):
        return {}
    out = {key: container[key] for key in ("cer", "wer", "ser") if key in container}
    return out


def _dimensions(eval_row: Optional[dict]) -> List[dict]:
    if not eval_row:
        return []
    dims = eval_row.get("dimensions")
    if not isinstance(dims, list):
        return []
    return [d for d in dims if isinstance(d, dict)]


def _ratio_block(dims: List[dict], cfg: dict) -> dict:
    raw = [_to_number(d.get("ratio")) for d in dims]
    mode = _resolve_unit(raw, str(cfg["report"]["ratio_unit"]))
    return {"mode": mode, "values": [_to_percent(value, mode) for value in raw]}


def _error_mix(eval_row: Optional[dict]) -> dict:
    if not eval_row:
        return {}
    mix = eval_row.get("error_mix")
    return mix if isinstance(mix, dict) else {}


def _attribution_baseline_name(attribution: Optional[dict]) -> str:
    if not attribution:
        return ""
    base = attribution.get("baseline")
    if isinstance(base, str):
        return _text(base)
    if isinstance(base, dict):
        return _text(base.get("name") or base.get("baseline_id") or base.get("id"))
    return ""


def _pick_eval(rows: List[dict], cfg: dict) -> Optional[dict]:
    if not rows:
        return None
    if str(cfg["selection"]["eval_pick"]).lower() == "first":
        return rows[0]
    scored = [
        (_moment(row.get("completed_at") or row.get("finished_at") or row.get("generated_at"), cfg), idx)
        for idx, row in enumerate(rows)
    ]
    return rows[max(scored, key=lambda item: (item[0], item[1]))[1]]


def _pick_baseline_row(rows: List[dict], attribution: Optional[dict]) -> Optional[dict]:
    """在输入的多条评测结果里找出 008 声明的对比基线那一条（用于错误形态的对比占比）。"""
    if not attribution or not rows:
        return None
    base = attribution.get("baseline")
    if not isinstance(base, dict):
        return None
    wanted = {
        _text(base.get(key)) for key in ("eval_id", "version_id", "external_task_id") if _text(base.get(key))
    }
    if not wanted:
        return None
    for row in rows:
        for key in ("eval_id", "version_id", "external_task_id"):
            if _text(row.get(key)) in wanted:
                return row
    return None


# --------------------------------------------------------------------------- #
# 008 归因口径
# --------------------------------------------------------------------------- #
def _share_percent(attribution: Optional[dict], cfg: dict) -> Optional[float]:
    if not attribution:
        return None
    value = _to_number(attribution.get("share"))
    if value is None:
        return None
    mode = _resolve_unit([value], str(cfg["report"]["share_unit"]))
    return _to_percent(value, mode)


def _factor_raw(item: dict) -> dict:
    """把 008 factors[] 的一条归一成渲染无关的结构（字段只增不改名，别名兼容）。"""
    dim = _text(item.get("dimension") or item.get("dim"))
    value = _text(item.get("value"))
    name = _text(item.get("name") or item.get("factor") or item.get("title"))
    if not name:
        name = " · ".join(part for part in (dim, value) if part) or "—"
    affected = _to_int(item.get("affected") if item.get("affected") is not None else item.get("affected_count"))
    if affected is None:
        affected = _to_int(item.get("count"))
    delta = _first_number(item, ["delta_pp", "diff_pp", "gap_pp", "delta"])
    basis = _text(item.get("basis") or item.get("evidence") or item.get("reason") or item.get("note"))
    samples = _to_int(item.get("samples"))
    return {
        "name": name,
        "dimension": dim,
        "value": value,
        "affected": affected,
        "delta": delta,
        "basis": basis,
        "samples": samples,
        # 对比对象的同一指标值（008 factors 里叫 baseline_metric），给分场景表现/下钻的「对比对象」列用
        "target_metric": _first_number(item, ["target_metric", "cer", "metric"]),
        "baseline_metric": _first_number(item, ["baseline_metric", "baseline_cer", "base_metric"]),
    }


def _factors(attribution: Optional[dict], cfg: dict) -> List[dict]:
    if not attribution:
        return []
    raw = attribution.get("factors")
    if not isinstance(raw, list):
        return []
    factors: List[dict] = []
    for item in raw:
        if not isinstance(item, dict):
            continue
        factor = _factor_raw(item)
        if not factor["basis"]:
            bits = []
            if factor["delta"] is not None:
                bits.append("差值 %s pp" % _fmt_delta(factor["delta"], cfg))
            if factor["samples"] is not None:
                bits.append("样本 %s 条" % _fmt_count(factor["samples"], cfg))
            factor["basis"] = "，".join(bits)
        factors.append(factor)
    return factors


def _first_number(mapping: dict, keys: Iterable[str]) -> Optional[float]:
    for key in keys:
        if key in mapping:
            value = _to_number(mapping.get(key))
            if value is not None:
                return value
    return None


def _gap_index(attribution: Optional[dict]) -> Dict[Tuple[str, str], float]:
    """按 (维度, 取值) 归集 008 因素里的差值，供分场景表现/噪声下钻复用。"""
    index: Dict[Tuple[str, str], float] = {}
    if not attribution:
        return index
    raw = attribution.get("factors")
    if not isinstance(raw, list):
        return index
    for item in raw:
        if not isinstance(item, dict):
            continue
        factor = _factor_raw(item)
        if factor["delta"] is None:
            continue
        key = (factor["dimension"].casefold(), factor["value"].casefold())
        if key != ("", ""):
            index[key] = factor["delta"]
    return index


def _factor_index(attribution: Optional[dict]) -> Dict[Tuple[str, str], dict]:
    """按 (维度, 取值) 归集 008 factors 的完整条目，供分场景/下钻取「对比对象的值 + 影响条数」。"""
    index: Dict[Tuple[str, str], dict] = {}
    if not attribution:
        return index
    raw = attribution.get("factors")
    if not isinstance(raw, list):
        return index
    for item in raw:
        if not isinstance(item, dict):
            continue
        factor = _factor_raw(item)
        key = (factor["dimension"].casefold(), factor["value"].casefold())
        if key != ("", ""):
            index[key] = factor
    return index


def _net_error_text(delta: Optional[float], affected: Optional[int], cfg: dict) -> Tuple[str, str]:
    """合成界面里的「多错 N 条 / 少错 N 条 / 基本持平」并给出配色类名。"""
    if delta is None:
        return "—", ""
    eps = 0.5 * (10 ** -_decimals(cfg))
    count = abs(int(affected)) if affected is not None else None
    if abs(delta) <= eps:
        return ("多错 %s 条" % _fmt_count(count, cfg) if count else "基本持平"), ""
    if delta > 0:
        return ("多错 %s 条" % _fmt_count(count, cfg) if count else "有差距"), "neg"
    return ("少错 %s 条" % _fmt_count(count, cfg) if count else "优于对比"), "pos"


def _chain(attribution: Optional[dict]) -> List[dict]:
    if not attribution:
        return []
    raw = attribution.get("chain")
    if not isinstance(raw, list):
        return []
    # 008 的 chain 元素是 {"level": "...", "ref": {...}} 结构，这里把两种写法都认下来
    level_labels = {
        "record": "受测记录",
        "baseline": "对比基线",
        "dimension": "维度下钻",
        "error_type": "错误形态",
        "sample": "样本明细",
        "metric": "指标",
        "scope": "统计范围",
    }
    steps = []
    for item in raw:
        if isinstance(item, dict) and isinstance(item.get("ref"), dict):
            ref = item["ref"]
            level = _text(item.get("level") or item.get("type"))
            label = level_labels.get(level, level)
            if level == "dimension":
                text = "%s/%s 差值 %+.2fpp" % (
                    _text(ref.get("dimension")) or "—",
                    _text(ref.get("value")) or "—",
                    _to_number(ref.get("diff_pp")) or 0.0,
                )
                if ref.get("affected") is not None:
                    text += "，影响 %s 条" % _text(ref.get("affected"))
            elif level == "error_type":
                counts = ref.get("error_counts") if isinstance(ref.get("error_counts"), dict) else {}
                text = "%s 条（替换 %s / 删除 %s / 插入 %s / 未识别 %s）" % (
                    _text(ref.get("error_samples")) or "—",
                    _text(counts.get("sub", 0)),
                    _text(counts.get("del", 0)),
                    _text(counts.get("ins", 0)),
                    _text(counts.get("unk", 0)),
                )
            elif level == "sample":
                text = "%s 条" % (_text(ref.get("count")) or "—")
            else:
                parts = []
                for key in ("version_id", "model", "external_task_id", "eval_id"):
                    if _text(ref.get(key)):
                        parts.append(_text(ref.get(key)))
                        break
                value = _to_number(ref.get("value"))
                if value is not None:
                    parts.append("%s %s" % (_text(ref.get("metric")) or "指标",
                                            _text(ref.get("value"))))
                text = " · ".join(parts)
            steps.append({"label": label, "text": text or "—"})
        elif isinstance(item, dict):
            label = _text(item.get("label") or item.get("name") or item.get("step") or item.get("type"))
            text = _text(item.get("text") or item.get("value") or item.get("desc") or item.get("detail"))
            steps.append({"label": label, "text": text or "—"})
        else:
            steps.append({"label": "", "text": _text(item) or "—"})
    return steps


# --------------------------------------------------------------------------- #
# 模型装配（与渲染无关）
# --------------------------------------------------------------------------- #
def _rows(*pairs: Tuple[str, str]) -> dict:
    return {"type": "rows", "rows": [{"label": label, "text": text} for label, text in pairs]}


def _cell(text: Any, cls: str = "", num: bool = False, bar: Optional[float] = None) -> dict:
    return {"text": _text(text), "cls": cls, "num": bool(num), "bar": bar}


def _table(columns: List[Tuple[str, bool]], rows: List[List[dict]], note: str = "") -> dict:
    return {
        "type": "table",
        "columns": [{"text": text, "num": bool(num)} for text, num in columns],
        "rows": rows,
        "note": note,
    }


def _unavailable(text: str) -> dict:
    return {"type": "unavailable", "text": text}


def _note(text: str) -> dict:
    return {"type": "note", "text": text}


def _subhead(text: str) -> dict:
    return {"type": "subhead", "text": text}


def _list(items: List[str]) -> dict:
    return {"type": "list", "items": [item for item in items if item]}


def _chips(items: List[str]) -> dict:
    return {"type": "chips", "items": [item for item in items if item]}


def _sample_plan_text(plan: Any, cfg: dict) -> str:
    if plan in (None, "", {}):
        return ""
    if isinstance(plan, dict):
        bits = []
        count = _to_int(plan.get("count") or plan.get("samples") or plan.get("sample_count"))
        if count is not None:
            bits.append("补样 %s 条" % _fmt_count(count, cfg))
        snr = plan.get("snr") or plan.get("snr_db") or plan.get("snr_range")
        if isinstance(snr, (list, tuple)) and len(snr) >= 2:
            low, high = _to_number(snr[0]), _to_number(snr[1])
            if low is not None and high is not None:
                bits.append("SNR %s–%s dB" % (_fmt_delta(low, cfg).lstrip("+"), _fmt_delta(high, cfg).lstrip("+")))
        elif _to_number(snr) is not None:
            bits.append("SNR %s dB" % _fmt_delta(_to_number(snr), cfg).lstrip("+"))
        for key in ("dimension", "value", "snr_bucket"):
            text = _text(plan.get(key))
            if text:
                bits.append(text)
        return "，".join(bits)
    return _text(plan)


def _suggestion_title(item: dict, index: int) -> str:
    return _text(item.get("title")) or "建议 %d" % index


def _build_meta_rows(
    eval_row: Optional[dict], metric_block: dict, attribution: Optional[dict], cfg: dict
) -> List[Tuple[str, str]]:
    rows: List[Tuple[str, str]] = []
    samples = metric_block["samples"]
    abnormal = metric_block["abnormal"]
    rows.append(("样本量", "%s 条" % _fmt_count(samples, cfg) if samples is not None else "未接入"))
    if abnormal is not None and samples:
        rows.append(
            (
                "异常样本",
                "%s 条（%s）" % (_fmt_count(abnormal, cfg), _fmt_pct(100.0 * abnormal / samples, cfg)),
            )
        )
    elif abnormal is not None:
        rows.append(("异常样本", "%s 条" % _fmt_count(abnormal, cfg)))
    else:
        rows.append(("异常样本", "未接入"))
    baseline_name = metric_block["baseline_name"]
    if not baseline_name:
        rows.append(("对比口径", "未接入"))
    else:
        base = attribution.get("baseline") if isinstance(attribution, dict) else None
        base_type = _text(base.get("type")) if isinstance(base, dict) else ""
        if base_type == "version":
            rows.append(("对比口径", "同一样本版本（对比 %s），可比" % baseline_name))
        elif base_type == "model":
            rows.append(("对比口径", "跨模型对比（对比 %s），不可比版本结论" % baseline_name))
        else:
            rows.append(("对比口径", "对比 %s，差值来自输入口径" % baseline_name))
    return rows


def _subtitle(eval_row: Optional[dict], attribution: Optional[dict], cfg: dict) -> str:
    task = ""
    version = ""
    model = ""
    if eval_row:
        task = _text(eval_row.get("task_name") or eval_row.get("external_task_id") or eval_row.get("eval_id"))
        version = _text(eval_row.get("version_name") or eval_row.get("version_id"))
        model = _text(eval_row.get("model") or eval_row.get("model_name"))
    if not task and attribution:
        task = _text(attribution.get("task_name") or attribution.get("record_id"))
    parts = [part for part in (task, version) if part]
    if model:
        parts.append(model)
    return " · ".join(parts) if parts else "未接入评测结果"


def _flag(eval_row: Optional[dict], attribution: Optional[dict], cfg: dict) -> Tuple[str, str]:
    if eval_row is None:
        return "数据待补全", "muted"
    share = _share_percent(attribution, cfg)
    if attribution and _text(attribution.get("main_cause")):
        threshold = float(cfg["report"]["flag_warn_share"]) * 100.0
        if share is None or share >= threshold:
            return "对标差距 · 需关注", "warn"
        return "整体差距有限", "info"
    return "归因结论待补全", "muted"


def _section_summary(eval_row, metric_block, attribution, advice, cfg) -> dict:
    blocks: List[dict] = []
    labels = cfg["metric_labels"]
    current = metric_block["current"]
    if eval_row:
        present = [(key, current[key]) for key in ("cer", "wer", "ser") if current.get(key) is not None]
        if present:
            model_name = _text(eval_row.get("model") or eval_row.get("model_name"))
            seg = "、".join("%s %s" % (labels[key], _fmt_pct(value, cfg)) for key, value in present)
            text = "%s 整体 %s" % (model_name, seg) if model_name else "整体 %s" % seg
            if metric_block["samples"] is not None:
                text += "，评测样本 %s 条" % _fmt_count(metric_block["samples"], cfg)
            if metric_block["baseline_name"] and any(v is not None for v in metric_block["baseline"].values()):
                gaps = [
                    current[key] - metric_block["baseline"][key]
                    for key in current
                    if current.get(key) is not None and metric_block["baseline"].get(key) is not None
                ]
                if gaps:
                    text += "；对比%s的差值 %s pp" % (
                        metric_block["baseline_name"],
                        " / ".join(_fmt_delta(gap, cfg) for gap in gaps),
                    )
            blocks.append(_rows(("整体结论", text + "。")))
    else:
        blocks.append(_unavailable("未接入：缺少 007 评测结果输入，整体指标无法生成。"))

    main_cause = _text(attribution.get("main_cause")) if attribution else ""
    share = _share_percent(attribution, cfg)
    affected = _to_int(attribution.get("affected")) if attribution else None
    net_error = _to_int(attribution.get("total_net_error")) if attribution else None
    confidence = _text(attribution.get("confidence")) if attribution else ""
    if attribution:
        bits = []
        if main_cause:
            bits.append("主因为 %s" % main_cause)
        if share is not None:
            bits.append("占整体退化 %s" % _fmt_pct(share, cfg))
        if affected is not None:
            bits.append("影响 %s 条" % _fmt_count(affected, cfg))
        if net_error is not None:
            bits.append("整体净多错 %s 条" % _fmt_count(net_error, cfg))
        if bits:
            blocks.append(_rows(("主要差距", "，".join(bits) + "。")))
        better = [f for f in _factors(attribution, cfg) if f["delta"] is not None and f["delta"] < 0]
        if better:
            best = min(better, key=lambda f: f["delta"])
            text = "%s 优于对比对象 %s 个百分点" % (best["name"], _fmt_abs(best["delta"], cfg))
            if best["affected"]:
                text += "（少错 %s 条）" % _fmt_count(abs(int(best["affected"])), cfg)
            blocks.append(_rows(("相对优势", text + "。")))
        else:
            blocks.append(_rows(("相对优势", "本批各维度均未优于对比对象，没有可列出的相对优势。")))
        if main_cause:
            blocks.append(
                _rows(
                    (
                        "原因判断",
                        "初步判断为「%s」相关%s；结论为可能原因，未经补充实验验证。"
                        % (main_cause, "（置信度：%s）" % confidence if confidence else ""),
                    )
                )
            )
    else:
        blocks.append(_unavailable("未接入：缺少 008 差异归因输入，主要差距与原因判断无法生成。"))

    suggestions = (advice or {}).get("suggestions") or []
    if suggestions:
        first = suggestions[0]
        plan = _sample_plan_text(first.get("sample_plan"), cfg)
        text = _suggestion_title(first, 1)
        if plan:
            text += "：" + plan
        blocks.append(_rows(("优化建议", text + "。")))
    elif advice is not None:
        blocks.append(_unavailable("未接入：009 优化建议输入为空（suggestions 为空）。"))
    else:
        blocks.append(_unavailable("未接入：缺少 009 优化建议输入。"))

    if eval_row and eval_row.get("attributable") is False:
        blocks.append(_rows(("使用边界", "该评测对应样本版本已回滚，结果不参与归因与建议生成，本报告仅供参考。")))
    return {"type": "section", "title": "结论摘要", "blocks": blocks}


def _section_metrics(eval_row, metric_block, cfg) -> dict:
    if not eval_row:
        return {"type": "section", "title": "整体指标", "blocks": [_unavailable("未接入：缺少 007 评测结果输入。")]}
    labels = cfg["metric_labels"]
    rows = []
    kpi_items = []
    for key in ("cer", "wer", "ser"):
        current = metric_block["current"].get(key)
        baseline = metric_block["baseline"].get(key)
        if current is None:
            continue
        delta = current - baseline if baseline is not None else None
        rows.append(
            [
                _cell(labels[key]),
                _cell(_fmt_pct(current, cfg), num=True),
                _cell(_fmt_pct(baseline, cfg) if baseline is not None else "—", num=True),
                _cell(_fmt_delta(delta, cfg), cls=_delta_class(delta, cfg), num=True),
            ]
        )
        if baseline is None:
            diff_text, diff_cls, ref_text = "对比对象未提供", "", "对比对象 —"
        else:
            eps = 0.5 * (10 ** -_decimals(cfg))
            if abs(delta) <= eps:
                diff_text = "与对比对象持平"
            else:
                diff_text = "%s %s 个百分点" % ("高" if delta > 0 else "低", _fmt_abs(delta, cfg))
            diff_cls = _delta_class(delta, cfg)
            ref_text = "对比对象 %s" % _fmt_pct(baseline, cfg)
        kpi_items.append(
            {
                "name": labels[key],
                "value": _fmt_pct(current, cfg),
                "diff": diff_text,
                "diff_class": diff_cls,
                "ref": ref_text,
            }
        )
    blocks = []
    if kpi_items:
        blocks.append({"type": "kpi", "items": kpi_items})
    blocks.append(
        _table(
            [("指标", False), ("本模型", True), ("对比对象", True), ("差值（pp）", True)],
            rows,
            note="指标单位：百分比，取自 007 评测结果；差值 = 本模型 − 对比对象。"
            if metric_block["baseline_name"]
            else "未提供对比对象指标，对比列与差值列以「—」展示（不编数）。",
        )
    )
    samples = metric_block["samples"]
    abnormal = metric_block["abnormal"]
    overview = [
        [_cell("样本量", num=False), _cell("%s 条" % _fmt_count(samples, cfg) if samples is not None else "—")],
        [
            _cell("异常样本", num=False),
            _cell(
                "%s 条（%s）"
                % (_fmt_count(abnormal, cfg), _fmt_pct(100.0 * abnormal / samples, cfg) if samples else "—")
                if abnormal is not None
                else "—",
            ),
        ],
        [
            _cell("完成率", num=False),
            _cell(
                _fmt_pct(100.0 * (samples - abnormal) / samples, cfg) if samples and abnormal is not None else "—",
            ),
        ],
    ]
    blocks.append(_subhead("样本概况"))
    blocks.append(_table([("项目", False), ("数值", False)], overview,
                         note="完成率 = （样本量 − 异常样本）÷ 样本量，由输入直接换算；"
                              "异常样本取 007 未识别（error_mix.unk）条数。"))
    return {"type": "section", "title": "整体指标", "blocks": blocks}


def _section_dimensions(eval_row, metric_block, attribution, cfg) -> dict:
    dims = _dimensions(eval_row)
    if not dims:
        return {
            "type": "section",
            "title": "分场景表现",
            "blocks": [_unavailable("未接入：007 评测结果未提供 dimensions[] 维度得分。")],
        }
    ratio = _ratio_block(dims, cfg)
    gap_index = _gap_index(attribution)
    factor_index = _factor_index(attribution)
    mode = metric_block["mode"]
    labels = cfg["metric_labels"]
    max_rows = int(cfg["limits"]["max_table_rows"])
    rows = []
    for idx, item in enumerate(dims[:max_rows]):
        dim = _text(item.get("dimension")) or "—"
        value = _text(item.get("value")) or "—"
        delta = _gap_index_delta(item, gap_index)
        factor = factor_index.get((dim.casefold(), value.casefold())) or {}
        base_cer = factor.get("baseline_metric")
        net_text, net_cls = _net_error_text(delta, factor.get("affected"), cfg)
        ratio_value = ratio["values"][idx] if idx < len(ratio["values"]) else None
        rows.append(
            [
                _cell(dim),
                _cell(value),
                _cell(_fmt_count(_to_int(item.get("samples")), cfg), num=True),
                _cell(_fmt_pct(ratio_value, cfg), num=True),
                _cell(_fmt_pct(_to_percent(_to_number(item.get("cer")), mode), cfg), num=True),
                _cell(_fmt_pct(_to_percent(_to_number(item.get("wer")), mode), cfg), num=True),
                _cell(_fmt_pct(_to_percent(_to_number(item.get("ser")), mode), cfg), num=True),
                _cell(_fmt_pct(_to_percent(base_cer, mode), cfg) if base_cer is not None else "—", num=True),
                _cell(_fmt_delta(delta, cfg), cls=_delta_class(delta, cfg), num=True),
                _cell(net_text, cls=net_cls, num=True),
            ]
        )
    total_delta = None
    if metric_block["baseline_name"]:
        gaps = [
            metric_block["current"][key] - metric_block["baseline"][key]
            for key in metric_block["current"]
            if metric_block["current"].get(key) is not None and metric_block["baseline"].get(key) is not None
        ]
        if gaps:
            total_delta = gaps[0]
    net_total = _to_int(attribution.get("total_net_error")) if attribution else None
    total_net_text, total_net_cls = _net_error_text(total_delta, net_total, cfg)
    rows.append(
        [
            _cell("合计"),
            _cell("—"),
            _cell(_fmt_count(metric_block["samples"], cfg) if metric_block["samples"] is not None else "—", num=True),
            _cell(_fmt_pct(100.0, cfg) if metric_block["samples"] else "—", num=True),
            _cell(_fmt_pct(metric_block["current"]["cer"], cfg), num=True),
            _cell(_fmt_pct(metric_block["current"]["wer"], cfg), num=True),
            _cell(_fmt_pct(metric_block["current"]["ser"], cfg), num=True),
            _cell(
                _fmt_pct(metric_block["baseline"]["cer"], cfg)
                if metric_block["baseline"].get("cer") is not None
                else "—",
                num=True,
            ),
            _cell(_fmt_delta(total_delta, cfg), cls=_delta_class(total_delta, cfg), num=True),
            _cell(total_net_text, cls=total_net_cls, num=True),
        ]
    )
    note = "维度得分按 007 dimensions[] 原样展示（样本数、占比来自输入），本报告不重新计算指标。"
    if metric_block["baseline_name"]:
        note += "对比对象列与「多错/少错」取自 008 归因因素（baseline_metric / affected）。"
    else:
        note += "未提供对比对象的分维度指标，对比对象列、差值与「多错/少错」列以「—」展示。"
    blocks = [
        _table(
            [
                ("维度", False),
                ("取值", False),
                ("样本数", True),
                ("占比", True),
                (labels["cer"], True),
                (labels["wer"], True),
                (labels["ser"], True),
                ("对比对象 " + labels["cer"], True),
                ("差值（pp）", True),
                ("多错 / 少错", True),
            ],
            rows,
            note=note,
        )
    ]
    if len(dims) > max_rows:
        blocks.append(_note("维度行数超过展示上限 %d，仅展示前 %d 行（统计与归因不受影响）。" % (max_rows, max_rows)))
    return {"type": "section", "title": "分场景表现", "blocks": blocks}


def _gap_index_delta(item: dict, gap_index: Dict[Tuple[str, str], float]) -> Optional[float]:
    direct = _baseline_delta_from_dimension(item)
    if direct is not None:
        return direct
    dim = _text(item.get("dimension")).casefold()
    value = _text(item.get("value")).casefold()
    return gap_index.get((dim, value))


def _baseline_delta_from_dimension(item: dict) -> Optional[float]:
    for key in ("delta_pp", "diff_pp", "gap_pp"):
        value = _to_number(item.get(key))
        if value is not None:
            return value
    cer = _to_number(item.get("cer"))
    for key in ("baseline_cer", "base_cer", "ref_cer"):
        base = _to_number(item.get(key))
        if base is not None:
            return None if cer is None else cer - base
    base = item.get("baseline")
    if isinstance(base, dict):
        base_cer = _to_number(base.get("cer"))
        if base_cer is not None and cer is not None:
            return cer - base_cer
    return None


def _section_noise(eval_row, metric_block, attribution, cfg) -> dict:
    dims = _dimensions(eval_row)
    names = [str(x).casefold() for x in cfg["report"]["noise_dimensions"]]
    noise = [d for d in dims if _text(d.get("dimension")).casefold() in names]
    if not noise:
        return {
            "type": "section",
            "title": "噪声场景下钻",
            "blocks": [_unavailable("未接入：007 评测结果中没有噪声维度的维度得分。")],
        }
    gap_index = _gap_index(attribution)
    factor_index = _factor_index(attribution)
    mode = metric_block["mode"]
    ratio = _ratio_block(dims, cfg)
    ratio_map = {id(d): ratio["values"][i] if i < len(ratio["values"]) else None for i, d in enumerate(dims)}
    ordered = sorted(
        noise,
        key=lambda d: (_to_percent(_to_number(d.get("cer")), mode) is None, -(_to_percent(_to_number(d.get("cer")), mode) or 0.0)),
    )
    deltas = [_gap_index_delta(d, gap_index) for d in ordered]
    max_delta = max([d for d in deltas if d is not None], default=None)
    large = float(cfg["report"]["judgement_large_pp"])
    medium = float(cfg["report"]["judgement_medium_pp"])
    rows = []
    for idx, item in enumerate(ordered):
        delta = deltas[idx]
        dim_name = _text(item.get("dimension")).casefold()
        value_name = _text(item.get("value")).casefold()
        factor = factor_index.get((dim_name, value_name)) or {}
        base_cer = factor.get("baseline_metric")
        net_text, net_cls = _net_error_text(delta, factor.get("affected"), cfg)
        if delta is None:
            judgement = "—"
        elif max_delta is not None and delta >= max_delta > 0:
            judgement = "差距最大"
        elif delta >= large:
            judgement = "差距明显"
        elif delta >= medium:
            judgement = "差距较小"
        elif delta <= 0:
            judgement = "优于对比"
        else:
            judgement = "基本持平"
        rows.append(
            [
                _cell(_text(item.get("value")) or "—"),
                _cell(_fmt_count(_to_int(item.get("samples")), cfg), num=True),
                _cell(_fmt_pct(ratio_map.get(id(item)), cfg), num=True),
                _cell(_fmt_pct(_to_percent(_to_number(item.get("cer")), mode), cfg), num=True),
                _cell(_fmt_pct(_to_percent(_to_number(item.get("wer")), mode), cfg), num=True),
                _cell(_fmt_pct(_to_percent(_to_number(item.get("ser")), mode), cfg), num=True),
                _cell(_fmt_pct(_to_percent(base_cer, mode), cfg) if base_cer is not None else "—", num=True),
                _cell(_fmt_delta(delta, cfg), cls=_delta_class(delta, cfg), num=True),
                _cell(net_text, cls=net_cls, num=True),
                _cell(judgement),
            ]
        )
    blocks = [
        _table(
            [
                ("取值", False),
                ("样本数", True),
                ("占比", True),
                (cfg["metric_labels"]["cer"], True),
                (cfg["metric_labels"]["wer"], True),
                (cfg["metric_labels"]["ser"], True),
                ("对比对象 " + cfg["metric_labels"]["cer"], True),
                ("差值（pp）", True),
                ("多错 / 少错", True),
                ("判断", False),
            ],
            rows,
            note="判断阈值：差值 ≥ %s pp 为差距明显，≥ %s pp 为差距较小；无对比对象指标时判断列显示「—」。"
            "对比对象列与「多错/少错」取自 008 归因因素（baseline_metric / affected）。"
            % (_fmt_delta(large, cfg).lstrip("+"), _fmt_delta(medium, cfg).lstrip("+")),
        )
    ]
    main_cause = _text(attribution.get("main_cause")) if attribution else ""
    if main_cause:
        hit = [item for item in noise if _text(item.get("value")) and _text(item.get("value")) in main_cause]
        if hit:
            blocks.append(_note("归因主因「%s」落在噪声维度下钻范围内。" % main_cause))
        else:
            blocks.append(_note("归因主因「%s」不在噪声维度取值内，噪声下钻仅作参考。" % main_cause))
    return {"type": "section", "title": "噪声场景下钻", "blocks": blocks}


def _section_error_mix(eval_row, cfg, baseline_row: Optional[dict] = None) -> dict:
    mix = _error_mix(eval_row)
    labels = cfg["report"]["error_types"]
    if not mix:
        return {
            "type": "section",
            "title": "错误形态",
            "blocks": [_unavailable("未接入：007 评测结果未提供 error_mix（替换/删除/插入/未识别）分布。")],
        }
    counts = {key: _to_int(mix.get(key)) for key in labels}
    total = sum(value for value in counts.values() if value is not None)
    base_mix = _error_mix(baseline_row)
    base_counts = {key: _to_int(base_mix.get(key)) for key in labels}
    base_total = sum(value for value in base_counts.values() if value is not None)
    dims = _dimensions(eval_row)
    rows = []
    chips: List[str] = []
    for key, label in labels.items():
        count = counts[key]
        if count is None:
            continue
        share = 100.0 * count / total if total else None
        base_count = base_counts.get(key)
        base_share = 100.0 * base_count / base_total if (base_total and base_count is not None) else None
        chips.append(
            "%s %s%s"
            % (
                label,
                _fmt_pct(share, cfg),
                " · 对比 %s" % _fmt_pct(base_share, cfg) if base_share is not None else "",
            )
        )
        rows.append(
            [
                _cell(label),
                _cell(_fmt_count(count, cfg), num=True),
                _cell(_fmt_pct(share, cfg), num=True, bar=share),
                _cell(_typical_errors(eval_row, key, cfg)),
                _cell(_sample_concentration(eval_row, key, cfg) or _concentration(dims, key, cfg)),
                _cell(_fmt_pct(base_share, cfg) if base_share is not None else "—", num=True),
            ]
        )
    blocks: List[dict] = []
    if chips:
        blocks.append(_chips(chips))
    blocks.append(
        _table(
            [
                ("形态", False),
                ("条数", True),
                ("占比", True),
                ("典型错误", False),
                ("集中在", False),
                ("对比对象占比", True),
            ],
            rows,
            note="错误形态取自 007 error_mix；占比 = 各形态条数 ÷ 合计 %s 条。" % _fmt_count(total, cfg),
        )
    )
    if total == 0:
        blocks.append(_note("error_mix 各形态条数均为 0，错误形态无可用分布。"))
    if any(_sample_results(eval_row)):
        blocks.append(
            _note(
                "「典型错误」由 007 sample_results 的 ref/hyp 逐字对齐取首例（如 四 → 是），"
                "「集中在」按该形态样本的加扰维度/取值统计；对比对象占比取基线评测的 error_mix。"
            )
        )
    return {"type": "section", "title": "错误形态", "blocks": blocks}


def _strip_ws(text: Any) -> str:
    return "".join(_text(text).split())


def _diff_pairs(ref: Any, hyp: Any, limit: int = 2) -> List[str]:
    """两个文本逐字对齐，优先取「替换」，再补丢字/多字，格式成「四 → 是」。"""
    left, right = _strip_ws(ref), _strip_ws(hyp)
    if not left and not right:
        return []
    substitutions: List[Tuple[str, str]] = []
    deletions: List[Tuple[str, str]] = []
    insertions: List[Tuple[str, str]] = []
    matcher = _difflib.SequenceMatcher(a=list(left), b=list(right), autojunk=False)
    for tag, i1, i2, j1, j2 in matcher.get_opcodes():
        if tag == "equal":
            continue
        a = "".join(left[i1:i2])[:1]
        b = "".join(right[j1:j2])[:1]
        if a and b:
            substitutions.append((a, b))
        elif a:
            deletions.append((a, ""))
        elif b:
            insertions.append(("", b))
    pairs: List[str] = []
    for a, b in substitutions + deletions + insertions:
        pairs.append("%s → %s" % (a or "∅", b or "∅"))
        if len(pairs) >= limit:
            break
    return pairs


def _sample_results(eval_row: Optional[dict]) -> List[dict]:
    rows = eval_row.get("sample_results") if isinstance(eval_row, dict) else None
    if not isinstance(rows, list):
        return []
    return [row for row in rows if isinstance(row, dict)]


def _typical_errors(eval_row: Optional[dict], key: str, cfg: dict) -> str:
    examples: List[str] = []
    for row in _sample_results(eval_row):
        if _text(row.get("error_type")).casefold() != key:
            continue
        pairs = _diff_pairs(row.get("ref_text"), row.get("hyp_text"))
        if not pairs:
            continue
        example = "、".join(pairs)
        if example in examples:
            continue
        examples.append(example)
        if len(examples) >= 2:
            break
    return "；".join(examples) if examples else "—"


def _sample_concentration(eval_row: Optional[dict], key: str, cfg: dict) -> str:
    """按样本的 perturb 维度/取值统计该错误形态集中在哪里。"""
    counts: Dict[str, int] = {}
    for row in _sample_results(eval_row):
        if _text(row.get("error_type")).casefold() != key:
            continue
        perturb = row.get("perturb") if isinstance(row.get("perturb"), dict) else {}
        label = " · ".join(
            part for part in (_text(perturb.get("dimension")), _text(perturb.get("value"))) if part
        )
        if not label:
            continue
        counts[label] = counts.get(label, 0) + 1
    if not counts:
        return ""
    label, count = max(counts.items(), key=lambda kv: kv[1])
    return "%s（%s 条）" % (label, _fmt_count(count, cfg))


def _concentration(dims: List[dict], key: str, cfg: dict) -> str:
    best: Optional[Tuple[int, dict]] = None
    for item in dims:
        mix = item.get("error_mix")
        if not isinstance(mix, dict):
            continue
        count = _to_int(mix.get(key))
        if count is None or count <= 0:
            continue
        if best is None or count > best[0]:
            best = (count, item)
    if best is None:
        return "—"
    count, item = best
    dim = _text(item.get("dimension"))
    value = _text(item.get("value"))
    label = " · ".join(part for part in (dim, value) if part) or "—"
    return "%s（%s 条）" % (label, _fmt_count(count, cfg))


def _section_attribution(attribution, cfg, eval_row: Optional[dict] = None) -> dict:
    if not attribution:
        return {
            "type": "section",
            "title": "原因分析",
            "blocks": [_unavailable("未接入：缺少 008 差异归因输入，无法给出原因分析。")],
        }
    labels = [
        ("主因", _text(attribution.get("main_cause")) or "—"),
        ("占整体退化", _fmt_pct(_share_percent(attribution, cfg), cfg)),
        ("影响条数", _fmt_count(_to_int(attribution.get("affected")), cfg)),
        ("置信度", _text(attribution.get("confidence")) or "—"),
        ("对比对象", _attribution_baseline_name(attribution)),
    ]
    rows = [(label, value) for label, value in labels if value]
    blocks: List[dict] = []
    if rows:
        blocks.append(_rows(*rows))
    factors = _factors(attribution, cfg)
    total_net = _to_int(attribution.get("total_net_error"))
    if factors:
        table_rows = []
        for factor in factors[: int(cfg["limits"]["max_table_rows"])]:
            affected = factor["affected"]
            table_rows.append(
                [
                    _cell(_clip(factor["name"], cfg)),
                    _cell(_fmt_count(affected, cfg), num=True),
                    _cell(_clip(factor["basis"], cfg) or "—"),
                ]
            )
        if total_net is not None:
            table_rows.append(
                [
                    _cell("合计"),
                    _cell(_fmt_count(total_net, cfg), num=True),
                    _cell("净多错 %s 条（多错 − 少错），与整体指标差值同口径" % _fmt_count(total_net, cfg)),
                ]
            )
        blocks.append(_subhead("归因因素"))
        blocks.append(_table([("因素", False), ("影响（条）", True), ("依据", False)], table_rows))
    overview = _cause_overview(attribution, eval_row, cfg)
    if overview:
        blocks.append(_subhead("原因链条"))
        blocks.append({"type": "chain", "steps": overview})
    chain = _chain(attribution)
    if chain:
        blocks.append(_subhead("血缘（可追溯）"))
        blocks.append({"type": "chain", "steps": chain})
    blocks.append(
        _note("置信度按主因样本量分档（<100 低 / 100–200 中 / >200 高）；归因结论为可能原因，需补充实验验证。")
    )
    return {"type": "section", "title": "原因分析", "blocks": blocks}


def _cause_overview(attribution: dict, eval_row: Optional[dict], cfg: dict) -> List[dict]:
    """把「现象 → 形态 → 特征 → 可能原因」拼成原型报告里的原因链条。"""
    detail = attribution.get("main_cause_detail")
    detail = detail if isinstance(detail, dict) else {}
    main_cause = _text(attribution.get("main_cause"))
    steps: List[dict] = []
    if main_cause:
        bits = [main_cause]
        affected = _to_int(attribution.get("affected"))
        if affected is not None:
            bits.append("多错 %s 条" % _fmt_count(affected, cfg))
        delta = _to_number(detail.get("diff_pp"))
        if delta is not None:
            bits.append("差值 %s pp" % _fmt_abs(delta, cfg))
        steps.append({"label": "现象", "text": "，".join(bits)})
    counts = detail.get("error_counts") if isinstance(detail.get("error_counts"), dict) else {}
    if not counts:
        counts = _error_mix(eval_row)
    labels = cfg["report"]["error_types"]
    pairs = [(_text(labels.get(key, key)), _to_int(counts.get(key))) for key in labels]
    pairs = [(label, count) for label, count in pairs if count]
    if pairs:
        total = sum(count for _, count in pairs)
        label, count = max(pairs, key=lambda item: item[1])
        steps.append(
            {
                "label": "形态",
                "text": "%s %s（%s 条）" % (label, _fmt_pct(100.0 * count / total, cfg), _fmt_count(count, cfg)),
            }
        )
    features: List[str] = []
    samples = _to_int(detail.get("samples"))
    if samples is not None:
        features.append("子类样本 %s 条" % _fmt_count(samples, cfg))
    bucket = _text(detail.get("top_snr_bucket"))
    if bucket:
        features.append("集中在 SNR %s dB 分桶" % bucket)
    ci = _to_number(detail.get("ci_half_pp"))
    if ci is not None:
        features.append("95%% 置信区间半宽 ±%s pp" % _fmt_abs(ci, cfg))
    if features:
        steps.append({"label": "特征", "text": "，".join(features)})
    confidence = _text(attribution.get("confidence"))
    cause_bits = []
    if main_cause:
        cause_bits.append("缺「%s」覆盖样本，需补样复测验证" % main_cause)
    else:
        cause_bits.append("数据覆盖不足，需补样复测验证")
    if confidence:
        cause_bits.append("008 判定置信度 %s" % confidence)
    cause_bits.append("未做补充实验，不能排除其他原因")
    steps.append({"label": "可能原因", "text": "；".join(cause_bits) + "。"})
    return steps


def _section_advice(advice, cfg) -> dict:
    if advice is None:
        return {
            "type": "section",
            "title": "优化建议",
            "blocks": [_unavailable("未接入：缺少 009 优化建议输入。")],
        }
    suggestions = advice.get("suggestions") or []
    if not suggestions:
        return {
            "type": "section",
            "title": "优化建议",
            "blocks": [_unavailable("未接入：优化建议输入为空（suggestions 为空）。")],
        }
    max_items = int(cfg["report"]["max_suggestions"])
    limit_rows = int(cfg["limits"]["max_table_rows"])
    items = []
    for idx, item in enumerate(suggestions[:max_items], start=1):
        rows = []
        priority = _text(item.get("priority"))
        if priority:
            rows.append({"label": "优先级", "text": priority})
        dim = _text(item.get("dimension"))
        value = _text(item.get("value"))
        if dim or value:
            rows.append({"label": "归因维度", "text": " · ".join(part for part in (dim, value) if part)})
        judgement = _clip(_text(item.get("explanation")) or _text(item.get("fact")), cfg)
        if judgement:
            rows.append({"label": "判断", "text": judgement})
        plan = _sample_plan_text(item.get("sample_plan"), cfg)
        verify = _clip(_text(item.get("verification")), cfg)
        execution = "；".join(
            part.rstrip("。；") for part in (verify, plan) if part
        )
        if execution:
            rows.append({"label": "执行单", "text": execution + "。"})
        for label, key in (("事实", "fact"), ("证据强度", "evidence"), ("解释", "explanation"), ("验证方式", "verification")):
            text = _clip(_text(item.get(key)), cfg)
            if text:
                rows.append({"label": label, "text": text})
        plan = _sample_plan_text(item.get("sample_plan"), cfg)
        if plan:
            rows.append({"label": "补样计划", "text": plan})
        items.append({"no": idx, "title": _suggestion_title(item, idx), "priority": priority, "rows": rows[:limit_rows]})
    blocks: List[dict] = [{"type": "suggestions", "items": items}]
    if len(suggestions) > max_items:
        blocks.append(
            _note("建议条数 %d 超过业务上限 %d，仅展示前 %d 条。" % (len(suggestions), max_items, max_items))
        )
    return {"type": "section", "title": "优化建议", "blocks": blocks}


def _section_risks(eval_row, metric_block, attribution, advice, meta_info, cfg) -> dict:
    items: List[str] = []
    has_eval = eval_row is not None
    has_attr = attribution is not None
    has_advice = bool((advice or {}).get("suggestions"))
    items.append(
        "输入接入：评测结果%s、差异归因%s、优化建议%s。"
        % ("已接入" if has_eval else "未接入", "已接入" if has_attr else "未接入", "已接入" if has_advice else "未接入")
    )
    dims = _dimensions(eval_row)
    samples = [_to_int(d.get("samples")) for d in dims]
    samples = [value for value in samples if value is not None]
    if samples:
        minimum, maximum = min(samples), max(samples)
        threshold = int(cfg["report"]["small_sample_threshold"])
        small = [
            "%s·%s" % (_text(d.get("dimension")), _text(d.get("value")))
            for d in dims
            if _to_int(d.get("samples")) is not None and _to_int(d.get("samples")) < threshold
        ]
        text = "样本规模：维度子类样本量 %s–%s 条" % (_fmt_count(minimum, cfg), _fmt_count(maximum, cfg))
        if minimum == maximum:
            text = "样本规模：维度子类样本量均为 %s 条" % _fmt_count(minimum, cfg)
        if small:
            text += "；其中 %d 个子类少于 %d 条（%s），结论存在波动。" % (
                len(small),
                threshold,
                "、".join(small[:5]),
            )
        else:
            text += "，均不低于 %d 条。" % threshold
            if minimum == maximum:
                text = "样本规模：维度子类样本量均为 %s 条，不低于 %d 条。" % (
                    _fmt_count(minimum, cfg),
                    threshold,
                )
        items.append(text)
    if metric_block["abnormal"] is not None and metric_block["samples"]:
        items.append(
            "过程异常：异常样本 %s 条（%s），未参与指标计算的样本可能影响代表性。"
            % (_fmt_count(metric_block["abnormal"], cfg), _fmt_pct(100.0 * metric_block["abnormal"] / metric_block["samples"], cfg))
        )
    if metric_block["baseline_name"]:
        items.append("对比口径：对比对象为%s，差值来自输入口径，本报告不重复执行评测。" % metric_block["baseline_name"])
    else:
        items.append("对比口径：未提供对比对象指标，报告不做差距判定，仅呈现单模型表现。")
    if has_attr:
        items.append("归因边界：归因结论为可能原因，未经补充实验验证；影响条数为规则估算值。")
    else:
        items.append("归因边界：未接入差异归因，报告不对差距成因下结论。")
    items.append("口径边界：百分比指标来自 007 输入；占比与主因占比按输入登记习惯自动识别（比例值 ×100）。")
    if meta_info.get("picked_note"):
        items.append(meta_info["picked_note"])
    if meta_info.get("section_note"):
        items.append(meta_info["section_note"])
    if suggestions_out_of_range(advice, cfg):
        items.append(
            "建议条数：输入含 %d 条建议，超出「每任务 1–3 条」的业务口径，按上限展示。"
            % suggestions_out_of_range(advice, cfg)
        )
    if eval_row and eval_row.get("attributable") is False:
        items.append("版本状态：评测样本版本已回滚，结果不参与归因与建议生成，本报告仅供参考。")
    items.append("导出边界：报告只导出结论，不导出样本数据；数据不导出是本期固定口径。")
    return {
        "type": "section",
        "title": "风险与限制",
        "blocks": [_list([_clip(item, cfg) for item in items])],
    }


def suggestions_out_of_range(advice: Optional[dict], cfg: dict) -> int:
    if not advice:
        return 0
    count = len(advice.get("suggestions") or [])
    limit = int(cfg["report"]["max_suggestions"])
    return int(count) if count > limit else 0


def _build_model(
    eval_row: Optional[dict],
    attribution: Optional[dict],
    advice: Optional[dict],
    report_meta: dict,
    sections: List[str],
    cfg: dict,
    baseline_row: Optional[dict] = None,
) -> dict:
    metric_block = _metric_block(eval_row, attribution, cfg)
    builders = {
        "结论摘要": lambda: _section_summary(eval_row, metric_block, attribution, advice, cfg),
        "整体指标": lambda: _section_metrics(eval_row, metric_block, cfg),
        "分场景表现": lambda: _section_dimensions(eval_row, metric_block, attribution, cfg),
        "噪声场景下钻": lambda: _section_noise(eval_row, metric_block, attribution, cfg),
        "错误形态": lambda: _section_error_mix(eval_row, cfg, baseline_row),
        "原因分析": lambda: _section_attribution(attribution, cfg, eval_row),
        "优化建议": lambda: _section_advice(advice, cfg),
        "风险与限制": lambda: _section_risks(eval_row, metric_block, attribution, advice, report_meta, cfg),
    }
    built = []
    number = 0
    for title in sections:
        number += 1
        section = builders[title]()
        section["no"] = number
        built.append(section)

    flag_text, flag_class = _flag(eval_row, attribution, cfg)
    meta_rows = [("报告编号", report_meta["report_no"]), ("生成时间", report_meta["generated_at"])]
    meta_rows.extend(_build_meta_rows(eval_row, metric_block, attribution, cfg))
    return {
        "report_no": report_meta["report_no"],
        "title": report_meta["title"],
        "generated_at": report_meta["generated_at"],
        "subtitle": _subtitle(eval_row, attribution, cfg),
        "flag": flag_text,
        "flag_class": flag_class,
        "meta_rows": meta_rows,
        "sections": built,
        "metric_block": metric_block,
    }


# --------------------------------------------------------------------------- #
# 渲染：Markdown
# --------------------------------------------------------------------------- #
def render_markdown(model: dict, cfg: dict, pdf_name: Optional[str] = None) -> str:
    lines: List[str] = ["# %s" % model["title"], ""]
    for label, text in model["meta_rows"]:
        lines.append("- **%s**：%s" % (label, text))
    lines.append("")
    for section in model["sections"]:
        lines.append("## %d. %s" % (section["no"], section["title"]))
        lines.append("")
        for block in section["blocks"]:
            lines.extend(_md_block(block, cfg))
            lines.append("")
    lines.append("---")
    lines.append("")
    lines.append(
        "本报告由规则引擎自动生成：所有数字来自 007/008/009 输入或由输入直接换算，"
        "未接大模型、未做二次推算；HTML 为自包含样式，浏览器打开即可打印成 PDF。"
    )
    if pdf_name:
        lines.append("")
        lines.append("PDF：`%s`（与本文同源，已由 011 生成，可直接下载）。" % pdf_name)
    return "\n".join(lines).rstrip() + "\n"


def _md_cell(text: str) -> str:
    return str(text).replace("|", "\\|").replace("\r", " ").replace("\n", " ").strip()


def _md_block(block: dict, cfg: dict) -> List[str]:
    kind = block.get("type")
    if kind == "rows":
        out: List[str] = []
        for row in block["rows"]:
            out.append("**%s**：%s" % (row["label"], row["text"]))
            out.append("")
        return out
    if kind == "kpi":
        out = []
        for item in block["items"]:
            out.append(
                "- **%s**：%s（%s；%s）" % (item["name"], item["value"], item["diff"], item["ref"])
            )
        out.append("")
        return out
    if kind == "table":
        header = "| " + " | ".join(_md_cell(col["text"]) for col in block["columns"]) + " |"
        divider = "| " + " | ".join("---" for _ in block["columns"]) + " |"
        body = [
            "| " + " | ".join(_md_cell(cell.get("text", "")) for cell in row) + " |"
            for row in block["rows"]
        ]
        out = [header, divider] + body
        if block.get("note"):
            out.append("")
            out.append("> 注：%s" % block["note"])
        return out
    if kind == "chips":
        return ["- " + item for item in block["items"]]
    if kind == "list":
        return ["%d. %s" % (idx, item) for idx, item in enumerate(block["items"], start=1)]
    if kind == "note":
        return ["> %s" % block["text"]]
    if kind == "subhead":
        return ["**%s**" % block["text"], ""]
    if kind == "chain":
        parts = []
        for step in block["steps"]:
            label = "**%s** " % step["label"] if step.get("label") else ""
            parts.append("%s%s" % (label, step["text"]))
        return [" → ".join(parts)]
    if kind == "unavailable":
        return ["> **%s**" % block["text"]]
    if kind == "suggestions":
        out: List[str] = []
        for item in block["items"]:
            out.append("### 建议 %d · %s" % (item["no"], item["title"]))
            out.append("")
            for row in item["rows"]:
                out.append("**%s**：%s" % (row["label"], row["text"]))
                out.append("")
        return out
    return []


# --------------------------------------------------------------------------- #
# 渲染：自包含 HTML
# --------------------------------------------------------------------------- #
def render_html(model: dict, cfg: dict, pdf_name: Optional[str] = None) -> str:
    esc = _html.escape
    parts: List[str] = []
    parts.append("<!DOCTYPE html>")
    parts.append('<html lang="zh-CN">')
    parts.append("<head>")
    parts.append('<meta charset="utf-8">')
    parts.append('<meta name="viewport" content="width=device-width, initial-scale=1">')
    parts.append("<title>%s</title>" % esc(model["title"]))
    parts.append("<style>%s</style>" % _css())
    parts.append("</head>")
    parts.append("<body>")
    parts.append('<main class="doc">')
    parts.append('<header class="doc-head">')
    parts.append('<div class="head-top"><div class="head-main">')
    parts.append('<div class="crumb">语音对抗平台 / 报告中心</div>')
    parts.append("<h1>%s</h1>" % esc(model["title"]))
    parts.append('<div class="sub">%s</div>' % esc(model["subtitle"]))
    parts.append("</div>")
    parts.append(
        '<span class="flag flag-%s">%s</span></div>' % (esc(model["flag_class"]), esc(model["flag"]))
    )
    parts.append('<div class="meta-grid">')
    for label, text in model["meta_rows"]:
        extra = " wide" if str(label) == "对比口径" else ""
        parts.append(
            '<div class="meta-item%s"><span>%s</span><b>%s</b></div>'
            % (extra, esc(str(label)), esc(str(text)))
        )
    parts.append("</div>")
    parts.append("</header>")
    for section in model["sections"]:
        parts.append('<section class="card">')
        parts.append(
            '<h2><span class="no">%d</span>%s</h2>' % (section["no"], esc(section["title"]))
        )
        for block in section["blocks"]:
            parts.append(_html_block(block, esc))
        parts.append("</section>")
    parts.append('<footer class="doc-foot">')
    parts.append(
        "<div>本报告由规则引擎自动生成：所有数字来自 007/008/009 输入或由输入直接换算，未接大模型、未做二次推算。</div>"
    )
    if pdf_name:
        parts.append(
            '<div class="doc-actions">'
            '<a class="btn-dl" href="%s" download data-report-pdf="%s" data-report-no="%s">下载 PDF</a>'
            '<button type="button" class="btn-print" onclick="window.print()">打印</button>'
            "</div>" % (esc(pdf_name), esc(pdf_name), esc(model["report_no"]))
        )
        parts.append(
            "<div>报告编号 %s · PDF 与 HTML 同源（都由 011 直接生成），下载不依赖浏览器打印。</div>"
            % esc(model["report_no"])
        )
    else:
        parts.append(
            "<div>报告编号 %s · 本次未生成 PDF，可用浏览器「打印 → 另存为 PDF」，A4 纵向。</div>"
            % esc(model["report_no"])
        )
    parts.append("</footer>")
    parts.append("</main>")
    parts.append("</body>")
    parts.append("</html>")
    return "\n".join(parts) + "\n"


def _html_block(block: dict, esc) -> str:
    kind = block.get("type")
    if kind == "rows":
        body = "".join(
            '<div class="kv"><span class="lbl">%s</span><div>%s</div></div>'
            % (esc(str(row["label"])), esc(str(row["text"])))
            for row in block["rows"]
        )
        return '<div class="kv-list">%s</div>' % body
    if kind == "kpi":
        cards = []
        for item in block["items"]:
            cards.append(
                '<div class="kpi"><div class="kpi-name">%s</div><div class="kpi-val">%s</div>'
                '<div class="kpi-diff %s">%s</div><div class="kpi-ref">%s</div></div>'
                % (
                    esc(str(item["name"])),
                    esc(str(item["value"])),
                    esc(str(item.get("diff_class") or "")),
                    esc(str(item["diff"])),
                    esc(str(item["ref"])),
                )
            )
        return '<div class="kpi-row">%s</div>' % "".join(cards)
    if kind == "table":
        head = "".join(
            '<th class="%s">%s</th>' % ("num" if col.get("num") else "", esc(str(col["text"])))
            for col in block["columns"]
        )
        rows = []
        for row in block["rows"]:
            cells = []
            for cell in row:
                classes = []
                if cell.get("num"):
                    classes.append("num")
                if cell.get("cls"):
                    classes.append(cell["cls"])
                content = esc(str(cell.get("text", "")))
                if cell.get("bar") is not None:
                    width = max(0.0, min(100.0, float(cell["bar"])))
                    content = (
                        '<span class="barcell"><span class="bartrack"><i style="width:%.1f%%"></i></span>'
                        "<em>%s</em></span>" % (width, content)
                    )
                cells.append('<td class="%s">%s</td>' % (" ".join(classes), content))
            rows.append("<tr>%s</tr>" % "".join(cells))
        html = '<div class="table-wrap"><table class="t"><thead><tr>%s</tr></thead><tbody>%s</tbody></table></div>' % (
            head,
            "".join(rows),
        )
        if block.get("note"):
            html += '<div class="note">注：%s</div>' % esc(str(block["note"]))
        return html
    if kind == "chips":
        return '<div class="chips">%s</div>' % "".join(
            '<span class="chip">%s</span>' % esc(str(item)) for item in block["items"]
        )
    if kind == "list":
        return '<ol class="olist">%s</ol>' % "".join("<li>%s</li>" % esc(str(item)) for item in block["items"])
    if kind == "note":
        return '<div class="note">%s</div>' % esc(str(block["text"]))
    if kind == "subhead":
        return '<h3 class="subhead">%s</h3>' % esc(str(block["text"]))
    if kind == "chain":
        steps = []
        for step in block["steps"]:
            label = '<em>%s</em>' % esc(str(step["label"])) if step.get("label") else ""
            steps.append('<span class="chain-step">%s%s</span>' % (label, esc(str(step["text"]))))
        return '<div class="chain">%s</div>' % '<span class="chain-arrow">→</span>'.join(steps)
    if kind == "unavailable":
        return '<div class="unavailable">%s</div>' % esc(str(block["text"]))
    if kind == "suggestions":
        out = []
        for item in block["items"]:
            rows = "".join(
                '<div class="kv"><span class="lbl">%s</span><div>%s</div></div>'
                % (esc(str(row["label"])), esc(str(row["text"])))
                for row in item["rows"]
            )
            out.append(
                '<div class="sug"><div class="sug-no">%d</div><div class="sug-body">'
                '<div class="sug-title">%s</div>%s</div></div>'
                % (item["no"], esc(str(item["title"])), rows)
            )
        return "".join(out)
    return ""


def _css() -> str:
    return """
:root{--ink:#1f2937;--muted:#667085;--line:#e4e7ec;--soft:#f7f9fd;--accent:#4169f6;--danger:#d92d20;--ok:#12b76a;}
*{box-sizing:border-box;}
html,body{margin:0;padding:0;}
body{background:#eef2f8;color:var(--ink);font-family:"Microsoft YaHei","PingFang SC","Hiragino Sans GB",system-ui,-apple-system,sans-serif;font-size:13px;line-height:1.72;-webkit-print-color-adjust:exact;print-color-adjust:exact;}
.doc{max-width:960px;margin:24px auto 40px;background:#fff;padding:34px 40px 40px;box-shadow:0 6px 24px rgba(16,24,40,.08);border-radius:6px;}
.doc-head{border-bottom:3px solid var(--accent);padding-bottom:16px;margin-bottom:22px;}
.head-top{display:flex;justify-content:space-between;align-items:flex-start;gap:16px;flex-wrap:wrap;}
.crumb{color:var(--muted);font-size:12px;margin-bottom:6px;}
h1{font-size:25px;line-height:1.35;margin:0 0 6px;letter-spacing:.5px;}
.sub{color:var(--muted);font-size:13px;}
.flag{display:inline-block;padding:5px 12px;border-radius:999px;font-size:12px;font-weight:700;white-space:nowrap;}
.flag-warn{background:#fef3f2;color:#b42318;border:1px solid #fecdca;}
.flag-info{background:#ecfdf3;color:#027a48;border:1px solid #abefc6;}
.flag-muted{background:var(--soft);color:var(--muted);border:1px solid var(--line);}
.meta-grid{display:grid;grid-template-columns:repeat(3,minmax(0,1fr));gap:8px 18px;margin-top:16px;}
.meta-item{display:flex;gap:8px;font-size:12px;border-bottom:1px dashed var(--line);padding-bottom:4px;}
.meta-item span{color:var(--muted);white-space:nowrap;}
.meta-item b{font-weight:600;word-break:break-word;}
.meta-item.wide{grid-column:span 2;}
.card{border:1px solid var(--line);border-radius:6px;padding:16px 20px 18px;margin-bottom:16px;break-inside:avoid;page-break-inside:avoid;}
.card h2{font-size:16px;margin:0 0 12px;padding-bottom:8px;border-bottom:1px solid var(--line);display:flex;align-items:center;gap:10px;}
.card h2 .no{display:inline-flex;align-items:center;justify-content:center;width:22px;height:22px;border-radius:6px;background:var(--accent);color:#fff;font-size:12px;}
.subhead{font-size:13px;margin:14px 0 8px;color:#344054;}
.kv-list{display:flex;flex-direction:column;gap:6px;}
.kv{display:grid;grid-template-columns:112px 1fr;gap:10px;font-size:13px;}
.kv .lbl{color:var(--muted);}
.table-wrap{overflow-x:auto;margin-top:4px;}
table.t{width:100%;border-collapse:collapse;font-size:12.5px;}
table.t th,table.t td{border:1px solid var(--line);padding:7px 9px;text-align:left;vertical-align:top;}
table.t th{background:var(--soft);font-weight:600;color:#344054;white-space:nowrap;}
table.t td.num,table.t th.num{text-align:right;white-space:nowrap;}
table.t td.neg{color:var(--danger);font-weight:600;}
table.t td.pos{color:var(--ok);font-weight:600;}
.barcell{display:inline-flex;align-items:center;gap:8px;}
.bartrack{display:inline-block;width:64px;height:6px;background:#eef2f8;border-radius:3px;overflow:hidden;vertical-align:middle;}
.bartrack i{display:block;height:100%;background:var(--accent);border-radius:3px;}
.barcell em{font-style:normal;}
.note{color:var(--muted);font-size:12px;margin-top:8px;padding-left:10px;border-left:2px solid var(--line);}
.unavailable{background:#fffaeb;border:1px dashed #fedf89;color:#b54708;border-radius:6px;padding:10px 12px;font-size:12.5px;}
.chips{display:flex;flex-wrap:wrap;gap:8px;}
.chip{background:var(--soft);border:1px solid var(--line);border-radius:999px;padding:4px 10px;font-size:12px;}
.olist{margin:0;padding-left:22px;}
.olist li{margin-bottom:6px;}
.chain{display:flex;flex-wrap:wrap;align-items:center;gap:6px;margin-top:6px;}
.chain-step{background:var(--soft);border:1px solid var(--line);border-radius:6px;padding:4px 8px;font-size:12px;}
.chain-step em{font-style:normal;color:var(--muted);margin-right:4px;}
.chain-arrow{color:var(--muted);}
.sug{display:grid;grid-template-columns:30px 1fr;gap:12px;padding:12px 0;border-top:1px dashed var(--line);}
.sug:first-child{border-top:0;padding-top:2px;}
.sug-no{width:26px;height:26px;border-radius:50%;background:var(--accent);color:#fff;display:flex;align-items:center;justify-content:center;font-size:12px;font-weight:700;}
.sug-title{font-weight:700;font-size:13.5px;margin-bottom:6px;}
.sug-body .kv{margin-bottom:4px;}
.doc-foot{border-top:1px solid var(--line);margin-top:22px;padding-top:12px;color:var(--muted);font-size:11.5px;display:flex;flex-direction:column;gap:4px;}
.kpi-row{display:grid;grid-template-columns:repeat(3,1fr);gap:12px;margin:2px 0 10px;}
.kpi{border:1px solid var(--line);border-radius:6px;padding:12px 14px;background:#fcfdff;}
.kpi-name{color:var(--muted);font-size:11.5px;}
.kpi-val{font-size:26px;font-weight:800;line-height:1.25;letter-spacing:.5px;}
.kpi-diff{font-size:12px;font-weight:600;margin-top:2px;}
.kpi-ref{color:var(--muted);font-size:11.5px;}
.doc-actions{display:flex;gap:10px;margin-top:6px;}
.btn-dl,.btn-print{border-radius:6px;font-family:inherit;font-size:12.5px;padding:7px 16px;cursor:pointer;text-decoration:none;display:inline-block;}
.btn-dl{background:var(--accent);color:#fff;border:1px solid var(--accent);}
.btn-print{background:#fff;color:var(--ink);border:1px solid #cbd5e1;}
@media (max-width:760px){.doc{margin:0;padding:20px 14px;border-radius:0;}.meta-grid{grid-template-columns:1fr 1fr;}}
@media print{@page{size:A4;margin:16mm 14mm;}body{background:#fff;font-size:11.5px;}.doc{max-width:none;margin:0;padding:0;box-shadow:none;border-radius:0;}.card{break-inside:avoid;page-break-inside:avoid;}.table-wrap{overflow:visible;}table.t{font-size:11px;}table.t th,table.t td{padding:5px 7px;}h1{font-size:22px;}.doc-actions{display:none;}.kpi-row{grid-template-columns:repeat(3,1fr);}}
""".strip()


# --------------------------------------------------------------------------- #
# 主流程
# --------------------------------------------------------------------------- #
def _render_pdf_bytes(model: dict, cfg: dict, report_no: str) -> bytes:
    """按文件路径加载 PDF 渲染器。

    服务是以 importlib 方式加载 processor 的，算子目录不在 ``sys.path`` 里，
    直接 ``import avp_report_pdf`` 会失败，所以这里显式按同目录文件加载。
    """
    import importlib.util

    module_path = Path(__file__).with_name("avp_report_pdf.py")
    if not module_path.is_file():
        raise ImportError("找不到 avp_report_pdf.py")
    spec = importlib.util.spec_from_file_location("avp_report_pdf", str(module_path))
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module.render(model, cfg, report_no=report_no)


def run(ctx: OperatorContext) -> dict:
    cfg = ctx.config
    params = _parse_params(ctx.params or {}, cfg)
    ctx.log.route("输入识别(007/008/009) → 八段结构装配 → Markdown/HTML 渲染")

    artifacts = _load_artifacts(ctx, cfg)
    eval_row = _pick_eval(artifacts["eval_rows"], cfg)
    picked_eval_id = _text(eval_row.get("eval_id")) if eval_row else ""
    attribution = artifacts["attribution"]
    advice = artifacts["advice"]
    warnings = list(artifacts["warnings"])

    if eval_row is not None:
        ctx.log.read(len(artifacts["eval_rows"]), "条评测结果")
    else:
        warnings.append("未识别到可用评测结果，整体指标将标注「未接入」")
    if attribution is not None:
        ctx.log.read(1, "份差异归因")
    if advice is not None:
        ctx.log.read(len(advice.get("suggestions") or []), "条优化建议")

    now = _dt.datetime.now(_tz(cfg))
    generated_at = _fmt_datetime(now, cfg)
    report_no = params["report_no"] or _auto_report_no(now, cfg)
    title = params["title"] or _default_title(eval_row, attribution, cfg)

    if len(artifacts["eval_rows"]) > 1:
        warnings.append(
            "输入含 %d 条评测结果，本报告采用最新一条%s"
            % (len(artifacts["eval_rows"]), "（eval_id=%s）" % picked_eval_id if picked_eval_id else "")
        )
    if eval_row is not None and attribution is not None:
        eval_id = _text(eval_row.get("eval_id"))
        attr_eval_id = _text(attribution.get("eval_id"))
        if eval_id and attr_eval_id and eval_id != attr_eval_id:
            warnings.append("差异归因的 eval_id（%s）与评测结果（%s）不一致，请人工核对" % (attr_eval_id, eval_id))
    section_note = ""
    if len(params["sections"]) < len(cfg["report"]["sections"]):
        section_note = "本次按参数裁剪章节，仅输出：%s（固定八段之外的章节不生成）。" % "、".join(params["sections"])

    report_meta_info = {
        "report_no": report_no,
        "title": title,
        "generated_at": generated_at,
        "picked_note": "",
        "section_note": section_note,
    }
    if len(artifacts["eval_rows"]) > 1:
        report_meta_info["picked_note"] = "评测结果选取：输入 %d 条，按完成时间取最新一条%s。" % (
            len(artifacts["eval_rows"]),
            "（eval_id=%s）" % picked_eval_id if picked_eval_id else "",
        )

    baseline_row = _pick_baseline_row(artifacts["eval_rows"], attribution)
    model = _build_model(
        eval_row, attribution, advice, report_meta_info, params["sections"], cfg, baseline_row=baseline_row
    )
    advice_usable = bool((advice or {}).get("suggestions"))
    missing = [
        label
        for label, present in (
            ("007 评测结果", eval_row is not None),
            ("008 差异归因", attribution is not None),
            ("009 优化建议", advice_usable),
        )
        if not present
    ]
    status = "partial" if missing else "success"

    outputs: Dict[str, Path] = {}
    fmt = params["format"]
    want_md = fmt in ("md", "both", "all")
    want_html = fmt in ("html", "both", "all")
    want_pdf = fmt in ("pdf", "all") and bool(cfg.get("pdf", {}).get("enabled", True))
    pdf_name: Optional[str] = None
    pdf_bytes: Optional[bytes] = None
    if want_pdf:
        try:
            pdf_bytes = _render_pdf_bytes(model, cfg, report_no)
            pdf_name = str(cfg["io"]["report_pdf_filename"])
        except ImportError as exc:  # reportlab 不在时不能让整条链挂
            ctx.log.warning("PDF 未生成：缺少 reportlab（%s）" % exc)
        except Exception as exc:  # noqa: BLE001 - 渲染异常降级为无 PDF
            ctx.log.warning("PDF 未生成：%s" % exc)
    if want_md:
        md_path = iofs.write_text(
            ctx.out(str(cfg["io"]["report_md_filename"])), render_markdown(model, cfg, pdf_name)
        )
        outputs["report_md"] = md_path
        ctx.log.write_output(md_path.name)
    if want_html:
        html_path = iofs.write_text(
            ctx.out(str(cfg["io"]["report_html_filename"])), render_html(model, cfg, pdf_name)
        )
        outputs["report_html"] = html_path
        ctx.log.write_output(html_path.name)
    if pdf_name and pdf_bytes is not None:
        # PDF 最后登记：outputFile.main 保持「报告 md」为主产物（报告中心按 main 打开正文）
        pdf_path = Path(ctx.out(pdf_name))
        pdf_path.write_bytes(pdf_bytes)
        outputs["report_pdf"] = pdf_path
        ctx.log.write_output(pdf_path.name)

    meta_payload = _build_report_meta(model, artifacts, eval_row, attribution, advice, warnings, params, cfg)
    meta_path = iofs.write_json(ctx.out(str(cfg["io"]["meta_filename"])), meta_payload)
    outputs["report_meta"] = meta_path
    ctx.log.write_output(meta_path.name)

    if missing:
        ctx.log.warning("缺少输入：%s，报告对应章节标注「未接入」" % "、".join(missing))
    ctx.log.result(
        "报告生成",
        output=len(model["sections"]),
        extra={
            "章节": len(model["sections"]),
            "建议": len((advice or {}).get("suggestions") or []),
            "缺失输入": len(missing),
        },
    )
    if status == "partial":
        ctx.log.partial("报告已生成但不完整", "未接入：%s" % "、".join(missing))
    for warning in warnings:
        ctx.log.warning(_clip(warning, cfg))

    statistics = {
        "sections": len(model["sections"]),
        "suggestions": len((advice or {}).get("suggestions") or []),
        "eval_results": len(artifacts["eval_rows"]),
        "has_attribution": attribution is not None,
        "has_advice": advice is not None,
        "pdf_generated": "report_pdf" in outputs,
        "missing_inputs": missing,
        "warnings": len(warnings),
        "inputs": len(artifacts["sources"]),
    }
    return {
        "outputs": outputs,
        "statistics": statistics,
        "result": {
            "report_no": report_no,
            "title": title,
            "generated_at": generated_at,
            "format": fmt,
            "sections": [{"no": section["no"], "title": section["title"]} for section in model["sections"]],
            "conclusion": status,
            "missing_inputs": missing,
        },
        "status": status,
    }


def _auto_report_no(now: _dt.datetime, cfg: dict) -> str:
    date = now.strftime(cfg["report"]["report_date_format"])
    seq_width = int(cfg["report"]["report_no_seq_width"])
    seq_start = int(cfg["report"]["report_no_seq_start"])
    return "%s-%s-%0*d" % (cfg["report"]["report_no_prefix"], date, seq_width, seq_start)


def _default_title(eval_row: Optional[dict], attribution: Optional[dict], cfg: dict) -> str:
    task = ""
    if eval_row:
        task = _text(eval_row.get("task_name") or eval_row.get("external_task_id"))
    if not task and attribution:
        task = _text(attribution.get("task_name"))
    if not task:
        task = "未命名任务"
    return "%s · %s" % (task, cfg["report"]["default_title_suffix"])


def _build_report_meta(model, artifacts, eval_row, attribution, advice, warnings, params, cfg) -> dict:
    metric_block = model["metric_block"]
    inputs = {
        "eval_results": {
            "files": artifacts["eval_files"],
            "records": len(artifacts["eval_rows"]),
            "eval_id": _text(eval_row.get("eval_id")) if eval_row else None,
            "version_id": _text(eval_row.get("version_id")) if eval_row else None,
            "model": _text(eval_row.get("model") or eval_row.get("model_name")) if eval_row else None,
        }
        if eval_row is not None
        else None,
        "attribution": {
            "record_id": _text(attribution.get("record_id")),
            "eval_id": _text(attribution.get("eval_id")),
            "main_cause": _text(attribution.get("main_cause")),
            "confidence": _text(attribution.get("confidence")),
        }
        if attribution is not None
        else None,
        "advice": {
            "suggestions": len(advice.get("suggestions") or []),
            "task_id": _text((advice.get("raw") or {}).get("task_id")) if isinstance(advice.get("raw"), dict) else "",
        }
        if advice is not None
        else None,
    }
    key_numbers = {
        "cer": metric_block["current"].get("cer"),
        "wer": metric_block["current"].get("wer"),
        "ser": metric_block["current"].get("ser"),
        "samples": metric_block["samples"],
        "abnormal": metric_block["abnormal"],
        "baseline_name": metric_block["baseline_name"] or None,
        "main_cause": _text(attribution.get("main_cause")) if attribution else None,
        "share_percent": _share_percent(attribution, cfg),
        "affected": _to_int(attribution.get("affected")) if attribution else None,
        "total_net_error": _to_int(attribution.get("total_net_error")) if attribution else None,
        "error_mix": {key: _to_int(value) for key, value in _error_mix(eval_row).items()} if eval_row else {},
        "dimensions": len(_dimensions(eval_row)),
        "suggestions": len((advice or {}).get("suggestions") or []),
    }
    return {
        "report_no": model["report_no"],
        "title": model["title"],
        "generated_at": model["generated_at"],
        "format": params["format"],
        "sections": [{"no": section["no"], "title": section["title"]} for section in model["sections"]],
        "inputs": inputs,
        "sources": artifacts["sources"],
        "key_numbers": key_numbers,
        "warnings": warnings,
        "caliber": dict(CALIBER),
    }


__all__ = ["run", "render_markdown", "render_html", "OPERATOR_ID", "OPERATOR_NAME"]
