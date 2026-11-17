# -*- coding: utf-8 -*-
"""10 数据资产统计（avp_asset_statistics）。

职责
----
把「数据集登记表 + 样本入库记录」聚合成一页资产运营口径的统计：

- ``total_samples``：**各数据集样本数之和，不去重**。同一音频同时属于两个数据集，
  就算两次；本算子不做任何 uttid / 音频路径去重。
- ``dataset_count``：数据集对象个数。
- ``new_in_range``：时间范围内按入库时间 ``created_at`` 聚合的新增样本数。
- ``per_dataset``：``[{dataset_id, name, source, sample_count, new_in_range, updated_at}]``，
  数据集列表是**存量**，不随时间范围变化（按关键词过滤）。
- ``trend``：按天**连续**序列，覆盖「含今天在内的近 N 天」，没有数据的日期**补 0**。

业务硬口径（规范 §11 第 11 条 + 原型《数据资产 · 研发标注》③）
--------------------------------------------------------------
1. 资产总量 = 各数据集样本数之和，含所有数据集，不去重；
2. 近 N 天新增 = 近 N 天入库样本数之和，按样本入库时间聚合；无数据日补 0；
3. 只做统计，不承载质量问题处理与回流样本运营（那是别的模块的事）；
4. 指标卡与列表合计一致：关键词过滤后，指标卡与列表同口径缩减。

输入
----
- ``params.registry_path``：数据集登记表（JSON）或 006 单版本产物，**优先**使用；
  不传则用 ``inputFile``：
    * ``*.jsonl`` → 逐行入库记录（``dataset_id`` / ``created_at`` / 可选 ``dataset_name``）；
    * ``*.json``  → 登记表（``datasets[]`` + 可选 ``records[]``）或记录数组；
    * 目录        → 递归扫描 ``io.input_exts`` 下的全部文件并合并。
- 记录聚合即可得到数据集（登记表外出现的数据集按记录补全，不丢数）。

技术方案
--------
没有模型，也没有随机：两遍 O(n) 扫描 —— 第一遍按 ``dataset_id`` 归集样本数与时间窗口内
按天计数，第二遍按天补齐连续日期序列（缺日补 0）。时间解析支持带时区偏移的 ISO8601，
统一换算到配置时区后再取「天」，避免跨时区把同一天切到两天。所有阈值/口径参数来自
``configs/config.yaml``，同输入同配置输出确定。
"""

from __future__ import annotations

import datetime as _dt
import sys
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

_ROOT = Path(__file__).resolve().parent.parent
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))

from common import iofs  # noqa: E402
from common import schemas  # noqa: E402
from common.context import OperatorContext  # noqa: E402

OPERATOR_ID = "avp_asset_statistics"
OPERATOR_NAME = "数据资产统计"

# 输出里固化的口径说明（与规范 §11 第 11 条一致，不参与计算）
CALIBER = {
    "total_samples": "各数据集样本数之和，不去重（同一音频属两个数据集算两次）",
    "new_in_range": "按入库时间 created_at 聚合；缺失有效时间的记录计入总量、不计入新增",
    "trend": "含今天在内的连续 N 天，无数据日补 0",
    "per_dataset": "数据集存量列表，不随时间范围变化；按关键词过滤时指标卡与列表同口径",
}


# --------------------------------------------------------------------------- #
# 参数
# --------------------------------------------------------------------------- #
def _parse_params(params: dict, cfg: dict) -> dict:
    allowed = [int(x) for x in cfg["asset"]["time_ranges"]]
    default = int(cfg["asset"]["default_time_range"])
    raw = params.get("time_range", default)
    time_range = schemas.as_int(raw, "time_range")
    if time_range not in allowed:
        raise schemas.ParamError(
            "time_range 取值 %r 非法，允许：%s" % (time_range, " / ".join(str(x) for x in allowed))
        )

    keyword = params.get("keyword")
    if keyword is not None:
        keyword = str(keyword).strip()
        limit = int(cfg["asset"]["keyword_max_len"])
        if len(keyword) > limit:
            raise schemas.ParamError("keyword 长度不能超过 %d，收到 %d" % (limit, len(keyword)))
        keyword = keyword or None

    registry_path = params.get("registry_path")
    if registry_path is not None:
        registry_path = str(registry_path).strip() or None

    return {
        "time_range": time_range,
        "keyword": keyword,
        "registry_path": registry_path,
    }


# --------------------------------------------------------------------------- #
# 输入解析
# --------------------------------------------------------------------------- #
def _load_sources(ctx: OperatorContext, params: dict, cfg: dict) -> Tuple[List[dict], List[dict], List[dict], List[str]]:
    """返回 ``(datasets, records, sources, warnings)``。"""
    if params["registry_path"]:
        target = _resolve_path(ctx, params["registry_path"])
        ctx.log.read(1, "个指定登记表路径")
    else:
        target = ctx.require_input()

    paths = [target] if target.is_file() else _scan_dir(target, cfg)
    if not paths:
        exts = "/".join(cfg["io"]["input_exts"])
        raise schemas.OpError("missing_file: 输入目录中没有可用的 %s 文件" % exts)

    datasets: List[dict] = []
    records: List[dict] = []
    sources: List[dict] = []
    warnings: List[str] = []
    for path in paths:
        ds, rc, ws, kind = _load_file(path, cfg)
        datasets.extend(ds)
        records.extend(rc)
        warnings.extend("%s：%s" % (path.name, w) for w in ws)
        sources.append({"file": path.name, "kind": kind, "datasets": len(ds), "records": len(rc)})

    # 合并重复登记（同一 dataset_id 多次出现时取第一个非空字段）
    merged: Dict[str, dict] = {}
    order: List[str] = []
    for item in datasets:
        did = item["dataset_id"]
        if did not in merged:
            merged[did] = dict(item)
            order.append(did)
            continue
        node = merged[did]
        for key in ("name", "source", "declared_updated", "declared_created"):
            if not node.get(key) and item.get(key):
                node[key] = item[key]
        if node.get("declared_count") is None and item.get("declared_count") is not None:
            node["declared_count"] = item["declared_count"]

    datasets = [merged[did] for did in order]
    if len(datasets) < int(cfg["checks"]["min_datasets"]) and not records:
        raise schemas.OpError(
            "missing_file: 输入中没有识别到任何数据集或入库记录（文件 %d 个）" % len(paths)
        )
    return datasets, records, sources, warnings


def _scan_dir(root: Path, cfg: dict) -> List[Path]:
    exts = tuple(str(x).lower() for x in cfg["io"]["input_exts"])
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


def _resolve_path(ctx: OperatorContext, raw: str) -> Path:
    candidate = Path(str(raw))
    if not candidate.is_absolute():
        candidate = (Path(ctx.work_dir) / candidate).resolve()
    if candidate.is_file() or candidate.is_dir():
        return candidate
    # 兼容：只给文件名时去输入目录里找
    alt = ctx.input_dir() / Path(str(raw)).name
    if alt.is_file():
        return alt
    raise schemas.OpError("missing_file: registry_path 不存在：%s" % raw)


def _load_file(path: Path, cfg: dict) -> Tuple[List[dict], List[dict], List[str], str]:
    suffix = path.suffix.lower()
    if suffix == ".jsonl":
        rows = _read_jsonl(path)
        records, warnings = _norm_records(rows, cfg, default_dataset_id=None, origin=path.name)
        return [], records, warnings, "records"
    if suffix == ".json":
        payload = _read_json(path)
        return _parse_json_payload(payload, cfg, path.name)
    exts = "/".join(cfg["io"]["input_exts"])
    raise schemas.OpError("missing_file: 不支持的输入文件 %s（仅支持 %s）" % (path.name, exts))


def _parse_json_payload(payload: Any, cfg: dict, origin: str) -> Tuple[List[dict], List[dict], List[str], str]:
    reg = cfg["registry"]
    warnings: List[str] = []

    if isinstance(payload, list):
        records, ws = _norm_records(payload, cfg, default_dataset_id=None, origin=origin)
        return [], records, ws, "records"

    if not isinstance(payload, dict):
        raise schemas.OpError("invalid_jsonl: %s 不是 JSON 对象或数组" % origin)

    dataset_list = _first_mapping_list(payload, reg["dataset_list_keys"])
    if dataset_list is not None:
        datasets: List[dict] = []
        inline_records: List[dict] = []
        for idx, entry in enumerate(dataset_list):
            node, recs, ws = _norm_dataset_entry(entry, cfg, origin, idx)
            warnings.extend(ws)
            if node is None:
                continue
            datasets.append(node)
            inline_records.extend(recs)
        top_records = _first_mapping_list(payload, reg["record_list_keys"])
        records, ws = _norm_records(top_records or [], cfg, default_dataset_id=None, origin=origin)
        warnings.extend(ws)
        return datasets, inline_records + records, warnings, "registry"

    record_list = _first_mapping_list(payload, reg["record_list_keys"])
    if record_list is not None:
        records, ws = _norm_records(record_list, cfg, default_dataset_id=None, origin=origin)
        return [], records, ws, "records"

    # 006 单版本产物兼容：{version_id, version_name, sample_count, published_at}
    version_id = _pick(payload, reg["version_id_keys"])
    if version_id is not None:
        name = _pick(payload, reg["version_name_keys"]) or str(version_id)
        count, ok = _as_count(_pick(payload, reg["sample_count_keys"]))
        if not ok:
            warnings.append("版本产物 sample_count 不是整数，已按「未知」处理")
        node = {
            "dataset_id": str(version_id).strip(),
            "name": str(name).strip(),
            "source": "",
            "declared_count": count,
            "declared_updated": _as_text(_pick(payload, reg["updated_at_keys"]))
            or _as_text(_pick(payload, ["published_at", "publishedAt"])),
            "declared_created": _as_text(_pick(payload, reg["created_at_keys"])),
        }
        return [node], [], warnings, "version"

    raise schemas.OpError(
        "invalid_params: %s 无法识别为登记表/入库记录/版本产物（缺 datasets、records 或 version_id）" % origin
    )


def _norm_dataset_entry(
    entry: Any, cfg: dict, origin: str, idx: int
) -> Tuple[Optional[dict], List[dict], List[str]]:
    reg = cfg["registry"]
    warnings: List[str] = []
    if not isinstance(entry, dict):
        return None, [], ["第 %d 个数据集条目不是对象，已跳过" % (idx + 1)]

    raw_id = _pick(entry, reg["dataset_id_keys"])
    did = _as_text(raw_id)
    if not did:
        return None, [], ["第 %d 个数据集缺少 dataset_id，已跳过" % (idx + 1)]

    count, ok = _as_count(_pick(entry, reg["sample_count_keys"]))
    if not ok:
        warnings.append("数据集 %s 的样本数不是整数，已按「未知」处理" % did)
    node = {
        "dataset_id": did,
        "name": _as_text(_pick(entry, reg["dataset_name_keys"])) or did,
        "source": _as_text(_pick(entry, reg["source_keys"])) or "",
        "declared_count": count,
        "declared_updated": _as_text(_pick(entry, reg["updated_at_keys"])),
        "declared_created": _as_text(_pick(entry, reg["created_at_keys"])),
    }
    inline = _first_mapping_list(entry, reg["inline_record_keys"])
    recs, ws = _norm_records(inline or [], cfg, default_dataset_id=did, origin=origin)
    warnings.extend(ws)
    return node, recs, warnings


def _norm_records(
    rows: List[Any], cfg: dict, default_dataset_id: Optional[str], origin: str
) -> Tuple[List[dict], List[str]]:
    reg = cfg["registry"]
    unknown = str(cfg["asset"]["unknown_dataset_id"])
    out: List[dict] = []
    warnings: List[str] = []
    for idx, row in enumerate(rows):
        if not isinstance(row, dict):
            warnings.append("第 %d 条入库记录不是对象，已跳过" % (idx + 1))
            continue
        did = _as_text(_pick(row, reg["dataset_id_keys"])) or default_dataset_id or unknown
        out.append(
            {
                "dataset_id": did,
                "dataset_name": _as_text(_pick(row, reg["dataset_name_keys"])),
                "created_at": _pick(row, reg["record_time_keys"]),
            }
        )
    if warnings:
        warnings = ["%s（%s）" % (w, origin) for w in warnings]
    return out, warnings


def _pick(mapping: dict, keys: List[str]) -> Any:
    low = {str(k).casefold(): v for k, v in mapping.items()}
    for key in keys:
        value = low.get(str(key).casefold())
        if value not in (None, ""):
            return value
    return None


def _first_mapping_list(payload: dict, keys: List[str]) -> Optional[List[Any]]:
    for key in keys:
        value = _pick(payload, [key])
        if isinstance(value, list):
            return value
        if isinstance(value, dict):
            return list(value.values())
    return None


def _as_text(value: Any) -> str:
    if value is None:
        return ""
    return str(value).strip()


def _as_count(value: Any) -> Tuple[Optional[int], bool]:
    if value in (None, ""):
        return None, True
    try:
        number = int(value)
    except (TypeError, ValueError):
        return None, False
    return (number, True) if number >= 0 else (None, False)


def _read_jsonl(path: Path) -> List[Any]:
    try:
        return list(iofs.read_jsonl(path))
    except ValueError as exc:
        raise schemas.OpError(str(exc)) from exc


def _read_json(path: Path) -> Any:
    try:
        return iofs.read_json(path)
    except ValueError as exc:  # json.JSONDecodeError 是 ValueError 子类
        raise schemas.OpError("invalid_jsonl: %s 不是合法 JSON：%s" % (path.name, exc)) from exc


# --------------------------------------------------------------------------- #
# 时间
# --------------------------------------------------------------------------- #
def _tz(cfg: dict) -> _dt.tzinfo:
    name = str(cfg["asset"]["timezone"])
    try:
        from zoneinfo import ZoneInfo

        return ZoneInfo(name)
    except Exception:  # noqa: BLE001 - 无 tzdata 时按固定偏移兜底
        return _dt.timezone(_dt.timedelta(hours=float(cfg["asset"]["utc_offset_hours"])))


def _parse_dt(value: Any, tz: _dt.tzinfo, cfg: dict) -> Optional[_dt.datetime]:
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
        moment: Optional[_dt.datetime]
        try:
            moment = _dt.datetime.fromisoformat(text)
        except ValueError:
            moment = None
            for fmt in cfg["time"]["formats"]:
                try:
                    moment = _dt.datetime.strptime(text, fmt)
                    break
                except ValueError:
                    continue
            if moment is None:
                return None
    if moment.tzinfo is None:
        moment = moment.replace(tzinfo=tz)
    return moment.astimezone(tz)


def _today(cfg: dict) -> _dt.date:
    return _dt.datetime.now(_tz(cfg)).date()


def _fmt_dt(value: Optional[_dt.datetime], cfg: dict) -> Optional[str]:
    if value is None:
        return None
    return value.strftime(cfg["time"]["output_formats"]["datetime"])


# --------------------------------------------------------------------------- #
# 聚合（纯函数，便于测试直接断言真值）
# --------------------------------------------------------------------------- #
def aggregate(
    datasets: List[dict],
    records: List[dict],
    time_range: int,
    keyword: Optional[str],
    today: _dt.date,
    cfg: dict,
) -> Tuple[dict, List[str], dict]:
    """把登记表 + 入库记录聚合成 010 资产结果；返回 ``(asset, warnings, incomplete)``。"""
    tz = _tz(cfg)
    unknown_id = str(cfg["asset"]["unknown_dataset_id"])
    warnings: List[str] = []

    nodes: Dict[str, dict] = {}
    for item in datasets:
        did = str(item.get("dataset_id") or "").strip()
        if not did:
            continue
        nodes[did] = {
            "dataset_id": did,
            "name": item.get("name") or did,
            "source": item.get("source") or "",
            "declared_count": item.get("declared_count"),
            "declared_updated": item.get("declared_updated"),
            "declared_created": item.get("declared_created"),
        }

    start_day = today - _dt.timedelta(days=int(time_range) - 1)
    record_counts: Dict[str, int] = {}
    in_range: Dict[Tuple[str, _dt.date], int] = {}
    first_day: Dict[str, _dt.date] = {}
    last_day: Dict[str, _dt.date] = {}
    unregistered: List[str] = []
    bad_time = 0
    no_dataset_id = 0

    for row in records:
        did = str(row.get("dataset_id") or "").strip() or unknown_id
        if did == unknown_id:
            no_dataset_id += 1
        if did not in nodes:
            nodes[did] = {
                "dataset_id": did,
                "name": row.get("dataset_name") or did,
                "source": "",
                "declared_count": None,
                "declared_updated": None,
                "declared_created": None,
            }
            unregistered.append(did)
        record_counts[did] = record_counts.get(did, 0) + 1

        moment = _parse_dt(row.get("created_at"), tz, cfg)
        if moment is None:
            bad_time += 1
            continue
        day = moment.date()
        if did not in first_day or day < first_day[did]:
            first_day[did] = day
        if did not in last_day or day > last_day[did]:
            last_day[did] = day
        if start_day <= day <= today:
            key = (did, day)
            in_range[key] = in_range.get(key, 0) + 1

    selected: List[dict] = []
    missing_records: List[str] = []
    derived_count: List[str] = []
    for did, node in nodes.items():
        declared = node["declared_count"]
        counted = record_counts.get(did, 0)
        if declared is None:
            sample_count = counted
            if counted:
                derived_count.append(did)
        else:
            sample_count = int(declared)
            if counted == 0 and int(declared) > 0:
                missing_records.append(did)
        name = node["name"] or did
        if keyword and keyword.casefold() not in str(name).casefold():
            continue
        updated = (
            _parse_dt(node["declared_updated"], tz, cfg)
            or last_day.get(did)
            or _parse_dt(node["declared_created"], tz, cfg)
            or first_day.get(did)
        )
        created_day = _parse_dt(node["declared_created"], tz, cfg)
        created_date = created_day.date() if created_day else first_day.get(did)
        new_count = sum(cnt for (dd, _day), cnt in in_range.items() if dd == did)
        selected.append(
            {
                "dataset_id": did,
                "name": name,
                "source": node["source"] or "",
                "sample_count": int(sample_count),
                "new_in_range": int(new_count),
                "updated_at": _fmt_dt(updated, cfg),
                "_created_day": created_date,
            }
        )

    selected.sort(key=lambda n: (-n["sample_count"], str(n["name"]), n["dataset_id"]))

    day_new_samples: Dict[_dt.date, int] = {}
    day_new_datasets: Dict[_dt.date, int] = {}
    selected_ids = {n["dataset_id"] for n in selected}
    for (did, day), cnt in in_range.items():
        if did in selected_ids:
            day_new_samples[day] = day_new_samples.get(day, 0) + cnt
    for node in selected:
        day = node.pop("_created_day", None)
        if day is not None and start_day <= day <= today:
            day_new_datasets[day] = day_new_datasets.get(day, 0) + 1

    trend = [
        {
            "date": (start_day + _dt.timedelta(days=offset)).strftime(cfg["time"]["output_formats"]["date"]),
            "new_samples": int(day_new_samples.get(start_day + _dt.timedelta(days=offset), 0)),
            "new_datasets": int(day_new_datasets.get(start_day + _dt.timedelta(days=offset), 0)),
        }
        for offset in range(int(time_range))
    ]

    if bad_time:
        warnings.append("%d 条入库记录缺少可解析的入库时间，计入总量但不计入新增/趋势" % bad_time)
    if missing_records:
        warnings.append(
            "%d 个数据集只有登记样本数、没有入库记录，新增按 0 计（%s）"
            % (len(missing_records), ", ".join(sorted(missing_records)[:5]))
        )
    if unregistered:
        warnings.append(
            "%d 个数据集由入库记录补全（登记表未列出）：%s"
            % (len(unregistered), ", ".join(sorted(set(unregistered))[:5]))
        )
    if derived_count:
        warnings.append(
            "%d 个数据集未登记样本数，按入库记录条数统计：%s"
            % (len(derived_count), ", ".join(sorted(derived_count)[:5]))
        )
    if no_dataset_id:
        warnings.append("%d 条入库记录缺少 dataset_id，归集到 %s" % (no_dataset_id, unknown_id))
    if keyword is not None:
        warnings.append("关键词「%s」匹配 %d 个数据集" % (keyword, len(selected)))

    total_samples = sum(n["sample_count"] for n in selected)
    new_in_range = sum(n["new_in_range"] for n in selected)
    asset = {
        "total_samples": int(total_samples),
        "dataset_count": len(selected),
        "new_in_range": int(new_in_range),
        "time_range_days": int(time_range),
        "range": {
            "start": start_day.strftime(cfg["time"]["output_formats"]["date"]),
            "end": today.strftime(cfg["time"]["output_formats"]["date"]),
        },
        "per_dataset": [
            {
                "dataset_id": n["dataset_id"],
                "name": n["name"],
                "source": n["source"],
                "sample_count": n["sample_count"],
                "new_in_range": n["new_in_range"],
                "updated_at": n["updated_at"],
            }
            for n in selected
        ],
        "trend": trend,
        "caliber": dict(CALIBER),
        "filter": {"keyword": keyword},
    }
    incomplete = {
        "records_without_time": int(bad_time),
        "datasets_without_records": missing_records,
    }
    return asset, warnings, incomplete


# --------------------------------------------------------------------------- #
# 主流程
# --------------------------------------------------------------------------- #
def run(ctx: OperatorContext) -> dict:
    cfg = ctx.config
    params = _parse_params(ctx.params or {}, cfg)
    max_warnings = int(cfg["io"]["max_warnings"])

    ctx.log.route("登记表/入库记录解析 → 按数据集聚合 → 时间窗口新增与连续日趋势")

    datasets, records, sources, load_warnings = _load_sources(ctx, params, cfg)
    ctx.log.read(len(datasets), "个数据集")
    ctx.log.read(len(records), "条入库记录")

    today = _today(cfg)
    asset, warnings, incomplete = aggregate(
        datasets=datasets,
        records=records,
        time_range=params["time_range"],
        keyword=params["keyword"],
        today=today,
        cfg=cfg,
    )
    warnings = load_warnings + warnings
    asset["sources"] = sources
    asset["generated_at"] = _dt.datetime.now(_tz(cfg)).strftime(cfg["time"]["output_formats"]["datetime"])
    asset["warnings"] = warnings[:max_warnings]

    status = "partial" if (incomplete["records_without_time"] or incomplete["datasets_without_records"]) else "success"

    total_samples = asset["total_samples"]
    new_in_range = asset["new_in_range"]
    trend_days = len(asset["trend"])
    zero_days = sum(1 for item in asset["trend"] if item["new_samples"] == 0)

    output_path = iofs.write_json(ctx.out(str(cfg["io"]["asset_filename"])), asset)
    ctx.log.result(
        "资产统计",
        output=total_samples,
        extra={
            "数据集": asset["dataset_count"],
            "近%d天新增" % params["time_range"]: new_in_range,
            "趋势天数": trend_days,
        },
    )
    if warnings:
        ctx.log.warning(
            "统计口径提示：%d 条（%s）" % (len(warnings), warnings[0])
        )
    if status == "partial":
        ctx.log.partial(
            "部分数据无法完整统计",
            "缺入库时间 %d 条，缺入库记录的数据集 %d 个"
            % (incomplete["records_without_time"], len(incomplete["datasets_without_records"])),
        )
    ctx.log.write_output(output_path.name)

    statistics = {
        "datasets": asset["dataset_count"],
        "records": len(records),
        "total_samples": total_samples,
        "new_in_range": new_in_range,
        "time_range_days": params["time_range"],
        "trend_days": trend_days,
        "trend_zero_days": zero_days,
        "records_without_time": incomplete["records_without_time"],
        "datasets_without_records": len(incomplete["datasets_without_records"]),
        "warnings": len(warnings),
    }
    return {
        "outputs": {"asset": output_path},
        "statistics": statistics,
        "result": {
            "total_samples": total_samples,
            "dataset_count": asset["dataset_count"],
            "new_in_range": new_in_range,
            "time_range_days": params["time_range"],
            "range": asset["range"],
            "trend_days": trend_days,
            "keyword": params["keyword"],
            "conclusion": status,
        },
        "status": status,
    }


__all__ = ["run", "aggregate", "OPERATOR_ID", "OPERATOR_NAME"]
