# -*- coding: utf-8 -*-
"""07 评测结果接收（avp_ingest_eval_result）。

职责
----
接收外部「数据集评测」主动推送的评测结果（JSONL），按开发规范 §8 契约做字段级
校验与版本 ID 核对：

- 校验通过 → 归一化后落库为 ``007_eval_results.jsonl``；
- 任何一项不通过 → 整条写入 ``007_parse_failed.jsonl``，**绝不落库**；
- 汇总统计 → ``007_eval_report.json``。

业务硬口径
----------
1. 必填六项：``external_task_id``、``version_id``、``metrics{cer,wer,ser}``、
   ``dimensions[]``、``error_mix{sub,del,ins,unk}``、``sample_results[]``；
2. 版本 ID 不在版本库（006 注册表 / ``known_versions`` / ``rolled_back_versions``）
   → **整条解析失败、不落库**（``version_not_found``）；
3. 版本 ID 存在但状态为「已回滚」→ 照常落库，但 ``attributable=false``，
   下游归因与建议生成不使用它；
4. 字段缺失 / 类型错 / 数值越界 / JSON 坏 → 解析失败；失败不吞，逐条记原因；
5. 同一 ``external_task_id`` 重推（对方重试）按 ``last_wins`` 覆盖，计入 ``deduplicated``。

技术方案
--------
纯规则校验，无模型、无随机、无网络。三级校验：

1. **结构级**：六项必填、容器类型、ID 长度；
2. **版本级**：把 ``known_versions`` / 注册表文件（006 产物，兼容单对象、数组、
   ``{versions:[...]}``、``{version_id: {...}}`` 四种形态）合并成版本库，
   先判存在性（不存在即整条失败），再判回滚状态（存在但回滚 → attributable=false）；
3. **明细级**：``metrics`` 百分比范围、``dimensions`` 每项字段与占比、``error_mix``
   四类计数、``sample_results`` 每项 ``sample_id`` / 错误形态 / 加扰参数快照。

``sample_results`` 缺 ``error_type`` 时，用**字符级最小编辑脚本**（Levenshtein 动态
规划 + 回溯）从 ``ref_text`` / ``hyp_text`` 推导主导错误形态（替换/删除/插入/未识别）；
推导只做归一化，不参与任何指标计算，也不改外部给的 ``metrics`` 数字。

``total_samples`` 推导口径（写进 ``sample_count``，供下游算影响条数）：
显式 ``total_samples`` > 各维度取值样本数按维度分组求和的最大值 > ``sample_results`` 条数。
据此再判定 ``sample_results_mode``：条数等于总数 → ``full``（全样本级结果）；
少于总数 → ``subset``（只回传了错误样本清单，下游不据此算置信区间）。
"""

from __future__ import annotations

import datetime as _dt
import json
import sys
from collections import Counter, OrderedDict
from pathlib import Path
from typing import Any, Dict, List, Optional, Sequence, Tuple

_ROOT = Path(__file__).resolve().parent.parent
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))

from common import iofs  # noqa: E402
from common import schemas  # noqa: E402
from common.context import OperatorContext  # noqa: E402

OPERATOR_ID = "avp_ingest_eval_result"
OPERATOR_NAME = "评测结果接收"

CONTRACT_FIELDS: Tuple[str, ...] = (
    "external_task_id",
    "version_id",
    "metrics",
    "dimensions",
    "error_mix",
    "sample_results",
)
METRIC_KEYS: Tuple[str, ...] = ("cer", "wer", "ser")
ERROR_MIX_KEYS: Tuple[str, ...] = ("sub", "del", "ins", "unk")
SAMPLE_ID_KEYS: Tuple[str, ...] = ("sample_id", "uttid", "id")
REF_KEYS: Tuple[str, ...] = ("ref_text", "ref", "reference")
HYP_KEYS: Tuple[str, ...] = ("hyp_text", "hyp", "hypothesis")
ERRORS_KEYS: Tuple[str, ...] = ("errors", "error_counts")


# --------------------------------------------------------------------------- #
# 参数
# --------------------------------------------------------------------------- #
def _parse_params(params: dict, cfg: dict) -> dict:
    """校验并归一化业务参数；非法即抛 ``invalid_params``。"""
    params = params or {}

    known_raw = params.get("known_versions")
    known_versions: List[str] = []
    if known_raw:
        for item in schemas.as_list(known_raw, "known_versions"):
            text = schemas.as_str(item, "known_versions[]", max_len=int(cfg["validation"]["max_id_len"]))
            known_versions.append(text)

    rolled_raw = params.get("rolled_back_versions")
    rolled_versions: List[str] = []
    if rolled_raw:
        for item in schemas.as_list(rolled_raw, "rolled_back_versions"):
            text = schemas.as_str(item, "rolled_back_versions[]", max_len=int(cfg["validation"]["max_id_len"]))
            rolled_versions.append(text)

    registry_path = params.get("registry_path")
    if registry_path is not None and str(registry_path).strip() == "":
        registry_path = None
    if registry_path is None and cfg["versions"].get("default_registry_path"):
        registry_path = cfg["versions"]["default_registry_path"]
    if registry_path is not None:
        registry_path = str(registry_path).strip()

    required_extra: List[str] = []
    extra_raw = params.get("required_fields")
    if extra_raw:
        for item in schemas.as_list(extra_raw, "required_fields"):
            text = schemas.as_str(item, "required_fields[]", max_len=int(cfg["validation"]["max_id_len"]))
            if text not in required_extra:
                required_extra.append(text)
        unknown = [f for f in required_extra if f not in CONTRACT_FIELDS]
        if not unknown:
            # 传的只是契约六项 → 等于没追加
            required_extra = []
        else:
            required_extra = unknown

    if not known_versions and not registry_path:
        raise schemas.ParamError(
            "缺少版本库：known_versions 与 registry_path 至少提供一个，"
            "否则无法判定版本 ID 是否存在（配置键 versions.default_registry_path 可为默认值）"
        )

    return {
        "known_versions": known_versions,
        "rolled_back_versions": rolled_versions,
        "registry_path": registry_path,
        "required_extra": required_extra,
    }


# --------------------------------------------------------------------------- #
# 小工具
# --------------------------------------------------------------------------- #
def _now_iso() -> str:
    return _dt.datetime.now().astimezone().isoformat(timespec="seconds")


def _basename(path: Any) -> str:
    try:
        return Path(str(path)).name
    except (TypeError, ValueError):  # pragma: no cover - 兜底
        return str(path)


def _preview(value: Any, limit: int) -> str:
    """JSON 行预览：压成一行并截断，避免把整条大记录抄进失败文件。"""
    if isinstance(value, (dict, list)):
        try:
            text = json.dumps(value, ensure_ascii=False)
        except (TypeError, ValueError):  # pragma: no cover - 外部数据异常
            text = str(value)
    else:
        text = "" if value is None else str(value)
    text = " ".join(text.split())
    return text if len(text) <= limit else text[: max(0, limit - 1)] + "…"


def _problem(code: str, field: str, message: str) -> dict:
    return {"code": code, "field": field, "message": message}


def _is_number(value: Any) -> bool:
    return isinstance(value, (int, float)) and not isinstance(value, bool)


def _pick_text(raw: dict, keys: Sequence[str], allow_empty: bool = False) -> Optional[str]:
    """按候选键取字符串；``allow_empty=True`` 时空字符串也算「有值」（识别结果为空＝未识别）。"""
    for key in keys:
        value = raw.get(key)
        if isinstance(value, str):
            text = value.strip()
            if text or allow_empty:
                return text
    return None


# --------------------------------------------------------------------------- #
# 版本库
# --------------------------------------------------------------------------- #
def _load_registry(path: Path, cfg: dict) -> Dict[str, Optional[str]]:
    """读 006 版本注册表，抽出 ``{version_id: status}``；读不到直接失败。"""
    if not path.is_file():
        raise schemas.OpError("missing_file: 版本注册表不存在：%s" % path.name)
    try:
        payload = iofs.read_json(path)
    except (ValueError, json.JSONDecodeError) as exc:  # pragma: no cover - 极少数损坏
        raise schemas.OpError("invalid_jsonl: 版本注册表不是合法 JSON：%s" % exc) from exc

    versions: Dict[str, Optional[str]] = OrderedDict()

    def _add(item: Any) -> None:
        if not isinstance(item, dict):
            return
        version_id = item.get("version_id") or item.get("versionId") or item.get("id")
        if not isinstance(version_id, str) or not version_id.strip():
            return
        status = item.get("status")
        versions[version_id.strip()] = str(status).strip() if isinstance(status, str) and status.strip() else None

    if isinstance(payload, dict):
        nested = None
        for key in ("versions", "items", "records", "data"):
            if isinstance(payload.get(key), list):
                nested = payload[key]
                break
        if nested is not None:
            for item in nested:
                _add(item)
        elif any(k in payload for k in ("version_id", "versionId", "id")):
            _add(payload)
        else:
            # {version_id: {status: ...}} 形态
            for key, value in payload.items():
                if isinstance(value, dict):
                    item = dict(value)
                    item.setdefault("version_id", key)
                    _add(item)
    elif isinstance(payload, list):
        for item in payload:
            _add(item)

    if not versions:
        raise schemas.OpError(
            "invalid_jsonl: 版本注册表 %s 中没有可识别的 version_id" % path.name
        )
    return versions


def _build_version_bank(p: dict, ctx: OperatorContext, cfg: dict) -> Dict[str, Any]:
    """合并三种版本来源；返回 ``{versions, rolled, meta}``。"""
    registry_meta: dict = {"source": "param_list", "path": None, "count": 0}
    versions: Dict[str, Optional[str]] = OrderedDict()

    if p["registry_path"]:
        raw = Path(str(p["registry_path"]))
        candidates = [raw] if raw.is_absolute() else [Path(ctx.work_dir) / raw, Path.cwd() / raw]
        target = next((c for c in candidates if c.is_file()), None)
        if target is None:
            raise schemas.OpError(
                "missing_file: 版本注册表不存在：%s" % p["registry_path"]
            )
        versions.update(_load_registry(target, cfg))
        registry_meta = {"source": "registry_file", "path": _basename(target), "count": len(versions)}

    added_from_list = 0
    for version_id in p["known_versions"]:
        if version_id not in versions:
            versions[version_id] = None
            added_from_list += 1
    if added_from_list:
        registry_meta["source"] = "registry_file+known_versions" if registry_meta["path"] else "param_list"
        registry_meta["known_versions"] = len(p["known_versions"])

    rolled = {v for v, status in versions.items() if status == cfg["versions"]["rolled_back_status"]}
    rolled.update(p["rolled_back_versions"])

    # 显式回滚名单里的版本即使没在 known_versions 里也视为「存在但已回滚」
    for version_id in p["rolled_back_versions"]:
        versions.setdefault(version_id, cfg["versions"]["rolled_back_status"])

    registry_meta.update(
        {
            "count": len(versions),
            "rolled_back": len(rolled),
            "known_versions": len(p["known_versions"]),
        }
    )
    return {"versions": versions, "rolled": rolled, "meta": registry_meta}


# --------------------------------------------------------------------------- #
# 输入读取
# --------------------------------------------------------------------------- #
def _resolve_input(ctx: OperatorContext, cfg: dict) -> Path:
    """定位输入文件；传入目录时按关键词挑一个评测结果文件。"""
    path = ctx.require_input()
    if path.is_dir():
        files: List[Path] = []
        for ext in cfg["io"]["input_exts"]:
            files.extend(sorted(path.rglob("*%s" % ext)))
        if not files:
            raise schemas.OpError(
                "missing_file: 输入目录中没有评测结果文件（支持 %s）" % "/".join(cfg["io"]["input_exts"])
            )
        keyword = str(cfg["io"]["preferred_input_keyword"]).lower()
        preferred = [f for f in files if keyword in f.name.lower()]
        return (preferred or files)[0]
    return path


def _iter_records(path: Path) -> List[Tuple[int, Any, Optional[str]]]:
    """按行读 JSONL，坏行不吞（返回错误文本），由调用方记解析失败。"""
    suffix = path.suffix.lower()
    rows: List[Tuple[int, Any, Optional[str]]] = []
    if suffix == ".jsonl":
        with path.open("r", encoding="utf-8-sig") as fh:
            for lineno, line in enumerate(fh, 1):
                stripped = line.strip()
                if not stripped:
                    rows.append((lineno, None, "blank"))
                    continue
                try:
                    rows.append((lineno, json.loads(stripped), None))
                except json.JSONDecodeError as exc:
                    rows.append((lineno, None, "invalid_json: %s" % exc))
        return rows

    if suffix == ".json":
        try:
            payload = json.loads(path.read_text(encoding="utf-8-sig"))
        except json.JSONDecodeError as exc:
            raise schemas.OpError("invalid_jsonl: %s 不是合法 JSON：%s" % (path.name, exc)) from exc
        if isinstance(payload, list):
            items = payload
        elif isinstance(payload, dict):
            items = None
            for key in ("items", "records", "results"):
                value = payload.get(key)
                if isinstance(value, list):
                    items = value
                    break
            if items is None:
                items = [payload]
        else:
            items = [payload]
        for idx, item in enumerate(items, 1):
            rows.append((idx, item, None))
        return rows

    raise schemas.OpError(
        "invalid_jsonl: 输入文件必须是 %s，收到 %s" % ("/".join(cfg["io"]["input_exts"]), suffix)
    )


# --------------------------------------------------------------------------- #
# 明细校验
# --------------------------------------------------------------------------- #
def _norm_error_type(value: Any, cfg: dict) -> Optional[str]:
    """把外部错误类型（码或中文）归一化成契约码；无法识别返回 None。"""
    if value is None:
        return None
    text = str(value).strip()
    if not text:
        return None
    allowed = {str(x).casefold() for x in cfg["validation"]["error_types"]}
    lowered = text.casefold()
    if lowered in allowed:
        return lowered
    aliases = {
        str(k).strip().casefold(): str(v).strip().casefold()
        for k, v in (cfg["validation"].get("error_type_aliases") or {}).items()
    }
    mapped = aliases.get(lowered)
    if mapped in allowed:
        return mapped
    return None


def _dominant_from_counts(counts: Dict[str, int], cfg: dict) -> Optional[str]:
    priority = [str(x).casefold() for x in cfg["validation"]["error_type_priority"]]
    best = None
    best_count = 0
    for key in priority:
        count = counts.get(key)
        if isinstance(count, (int, float)) and not isinstance(count, bool) and count > best_count:
            best, best_count = key, count
    return best


def _classify_edit(ref_text: str, hyp_text: str, cfg: dict) -> str:
    """字符级最小编辑脚本（Levenshtein + 回溯）→ 主导错误形态。

    ``hyp_text`` 为空 → ``unk``（未识别）；完全一致 → ``none``；
    ``ref`` 为空而 ``hyp`` 有内容 → ``ins``。推导结果只用于归一化，不改指标。
    """
    ref = list(str(ref_text or ""))
    hyp = list(str(hyp_text or ""))
    if ref == hyp:
        return "none"
    if not hyp:
        return "unk"
    if not ref:
        return "ins"

    n, m = len(ref), len(hyp)
    dp = [[0] * (m + 1) for _ in range(n + 1)]
    for i in range(n + 1):
        dp[i][0] = i
    for j in range(m + 1):
        dp[0][j] = j
    for i in range(1, n + 1):
        ri = ref[i - 1]
        row, prev = dp[i], dp[i - 1]
        for j in range(1, m + 1):
            cost = 0 if ri == hyp[j - 1] else 1
            row[j] = min(prev[j - 1] + cost, prev[j] + 1, row[j - 1] + 1)

    counts = {"sub": 0, "del": 0, "ins": 0}
    i, j = n, m
    while i > 0 or j > 0:
        if i > 0 and j > 0:
            cost = 0 if ref[i - 1] == hyp[j - 1] else 1
            if dp[i][j] == dp[i - 1][j - 1] + cost:
                if cost:
                    counts["sub"] += 1
                i -= 1
                j -= 1
                continue
        if i > 0 and dp[i][j] == dp[i - 1][j] + 1:
            counts["del"] += 1
            i -= 1
            continue
        if j > 0 and dp[i][j] == dp[i][j - 1] + 1:
            counts["ins"] += 1
            j -= 1
            continue
        break  # pragma: no cover - 理论上到不了

    order = [str(x).casefold() for x in cfg["validation"]["edit_type_tie_order"]]
    best = max(order, key=lambda key: (counts.get(key, 0), -order.index(key)))
    return best if counts.get(best, 0) > 0 else "none"


def _validate_metrics(raw: dict, cfg: dict, problems: List[dict]) -> Optional[dict]:
    before = len(problems)
    block = raw.get("metrics")
    if not isinstance(block, dict):
        problems.append(_problem("type_error", "metrics", "metrics 必须是对象"))
        return None
    lo = float(cfg["validation"]["metric_min"])
    hi = float(cfg["validation"]["metric_max"])
    out: Dict[str, float] = {}
    for key in METRIC_KEYS:
        if key not in block:
            problems.append(_problem("missing_field", "metrics.%s" % key, "metrics 缺少 %s" % key))
            continue
        value = block[key]
        if not _is_number(value):
            problems.append(_problem("type_error", "metrics.%s" % key, "metrics.%s 必须是数字" % key))
            continue
        number = float(value)
        if number < lo or number > hi:
            problems.append(
                _problem(
                    "range_error",
                    "metrics.%s" % key,
                    "metrics.%s 必须在 [%s, %s] 百分比区间内，收到 %s" % (key, lo, hi, number),
                )
            )
            continue
        out[key] = number
    return out if len(problems) == before else (out or None)


def _validate_error_mix(raw: dict, cfg: dict, problems: List[dict]) -> Optional[dict]:
    before = len(problems)
    block = raw.get("error_mix")
    if not isinstance(block, dict):
        problems.append(_problem("type_error", "error_mix", "error_mix 必须是对象"))
        return None
    out: Dict[str, int] = {}
    for key in ERROR_MIX_KEYS:
        if key not in block:
            problems.append(_problem("missing_field", "error_mix.%s" % key, "error_mix 缺少 %s" % key))
            continue
        value = block[key]
        if isinstance(value, bool) or not isinstance(value, (int, float)):
            problems.append(_problem("type_error", "error_mix.%s" % key, "error_mix.%s 必须是整数条数" % key))
            continue
        if float(value) != int(value):
            problems.append(_problem("type_error", "error_mix.%s" % key, "error_mix.%s 必须是整数条数" % key))
            continue
        count = int(value)
        if count < 0:
            problems.append(_problem("range_error", "error_mix.%s" % key, "error_mix.%s 不能为负" % key))
            continue
        out[key] = count
    return out if len(problems) == before else (out or None)


def _validate_sample_results(
    raw: dict, cfg: dict, problems: List[dict]
) -> List[dict]:
    block = raw.get("sample_results")
    if not isinstance(block, list):
        problems.append(_problem("type_error", "sample_results", "sample_results 必须是数组"))
        return []
    if not block:
        problems.append(_problem("empty_field", "sample_results", "sample_results 不能为空数组"))
        return []

    max_len = int(cfg["validation"]["max_id_len"])
    samples: List[dict] = []
    seen_ids: Dict[str, int] = {}
    for idx, item in enumerate(block):
        where = "sample_results[%d]" % idx
        if not isinstance(item, dict):
            problems.append(_problem("type_error", where, "样本级结果必须是对象"))
            continue

        sample_id = _pick_text(item, SAMPLE_ID_KEYS)
        if sample_id is None:
            problems.append(_problem("missing_field", "%s.sample_id" % where, "样本级结果缺少 sample_id/uttid"))
            continue
        if len(sample_id) > max_len:
            problems.append(_problem("range_error", "%s.sample_id" % where, "sample_id 超过 %d 字符" % max_len))
            continue
        if sample_id in seen_ids:
            problems.append(
                _problem("duplicate_sample", "%s.sample_id" % where, "同一条回传里 sample_id 重复：%s" % sample_id)
            )
            continue
        seen_ids[sample_id] = idx

        ref_text = _pick_text(item, REF_KEYS)
        hyp_text = _pick_text(item, HYP_KEYS, allow_empty=True)

        error_type = _norm_error_type(item.get("error_type"), cfg)
        if error_type is None:
            counts_raw = None
            for key in ERRORS_KEYS:
                if isinstance(item.get(key), dict):
                    counts_raw = item[key]
                    break
            if counts_raw is not None:
                counts = {}
                for key in cfg["validation"]["error_type_priority"]:
                    value = counts_raw.get(key)
                    if _is_number(value) and value > 0:
                        counts[str(key).casefold()] = int(value)
                error_type = _dominant_from_counts(counts, cfg)
            elif ref_text is not None and hyp_text is not None:
                error_type = _classify_edit(ref_text, hyp_text, cfg)
        if error_type is None:
            problems.append(
                _problem(
                    "missing_field",
                    "%s.error_type" % where,
                    "样本缺少 error_type，且无法从 ref_text/hyp_text 推导",
                )
            )
            continue

        perturb_raw = item.get("perturb")
        if perturb_raw is not None and not isinstance(perturb_raw, dict):
            problems.append(_problem("type_error", "%s.perturb" % where, "perturb 必须是对象"))
            continue
        perturb: Dict[str, Any] = {
            "dimension": None,
            "value": None,
            "snr_db": None,
            "speed": None,
            "asset": None,
        }
        if isinstance(perturb_raw, dict):
            for key in ("dimension", "value", "asset"):
                value = perturb_raw.get(key)
                if value is None:
                    continue
                if not isinstance(value, str) or not value.strip():
                    problems.append(_problem("type_error", "%s.perturb.%s" % (where, key), "必须是字符串"))
                    continue
                perturb[key] = value.strip()
            for key in ("snr_db", "speed"):
                value = perturb_raw.get(key)
                if value is None:
                    continue
                if not _is_number(value):
                    problems.append(_problem("type_error", "%s.perturb.%s" % (where, key), "必须是数字"))
                    continue
                number = float(value)
                if key == "speed" and number <= 0:
                    problems.append(_problem("range_error", "%s.perturb.speed" % where, "speed 必须大于 0"))
                    continue
                perturb[key] = number
        else:
            # 兼容扁平写法：样本行上直接给 dimension/value/snr_db/speed/asset
            for key in ("dimension", "value", "asset"):
                value = item.get(key)
                if isinstance(value, str) and value.strip():
                    perturb[key] = value.strip()
            for key in ("snr_db", "speed"):
                value = item.get(key)
                if _is_number(value):
                    perturb[key] = float(value)

        if (perturb["dimension"] is None) != (perturb["value"] is None):
            problems.append(
                _problem(
                    "missing_field",
                    "%s.perturb" % where,
                    "perturb.dimension 与 perturb.value 必须同时给或同时为空",
                )
            )
            continue

        samples.append(
            {
                "sample_id": sample_id,
                "ref_text": ref_text,
                "hyp_text": hyp_text,
                "error_type": error_type,
                "perturb": perturb,
            }
        )
    return samples


def _derive_total_samples(
    raw: dict,
    dimensions: List[dict],
    samples: List[dict],
    cfg: dict,
    problems: List[dict],
) -> Optional[int]:
    raw_total = raw.get("total_samples")
    if raw_total is not None:
        if isinstance(raw_total, bool) or not isinstance(raw_total, (int, float)) or float(raw_total) != int(raw_total):
            problems.append(_problem("type_error", "total_samples", "total_samples 必须是整数条数"))
            return None
        total = int(raw_total)
        if total < int(cfg["validation"]["min_samples_per_value"]):
            problems.append(_problem("range_error", "total_samples", "total_samples 必须大于 0"))
            return None
        return total

    total = sum(int(dim["samples"]) for dim in dimensions)
    if dimensions and total > 0:
        return total
    if samples:
        return len(samples)
    problems.append(_problem("missing_field", "total_samples", "无法从 dimensions / sample_results 推导样本总数"))
    return None


def _validate_dimensions(
    raw: dict,
    cfg: dict,
    problems: List[dict],
) -> List[dict]:
    block = raw.get("dimensions")
    if not isinstance(block, list):
        problems.append(_problem("type_error", "dimensions", "dimensions 必须是数组"))
        return []
    max_len = int(cfg["validation"]["max_id_len"])
    lo = float(cfg["validation"]["metric_min"])
    hi = float(cfg["validation"]["metric_max"])
    min_samples = int(cfg["validation"]["min_samples_per_value"])
    dims: List[dict] = []
    for idx, item in enumerate(block):
        where = "dimensions[%d]" % idx
        if not isinstance(item, dict):
            problems.append(_problem("type_error", where, "维度得分必须是对象"))
            continue
        dimension = item.get("dimension")
        value = item.get("value")
        if not isinstance(dimension, str) or not dimension.strip():
            problems.append(_problem("missing_field", "%s.dimension" % where, "dimension 必须是非空字符串"))
            continue
        if not isinstance(value, str) or not value.strip():
            problems.append(_problem("missing_field", "%s.value" % where, "value 必须是非空字符串"))
            continue
        if len(dimension) > max_len or len(value) > max_len:
            problems.append(_problem("range_error", where, "dimension/value 超过 %d 字符" % max_len))
            continue

        entry: Dict[str, Any] = {"dimension": dimension.strip(), "value": value.strip()}
        ok = True
        for key in METRIC_KEYS:
            if key not in item:
                problems.append(_problem("missing_field", "%s.%s" % (where, key), "维度得分缺少 %s" % key))
                ok = False
                continue
            metric = item[key]
            if not _is_number(metric):
                problems.append(_problem("type_error", "%s.%s" % (where, key), "维度 %s 必须是数字" % key))
                ok = False
                continue
            number = float(metric)
            if number < lo or number > hi:
                problems.append(
                    _problem(
                        "range_error",
                        "%s.%s" % (where, key),
                        "维度 %s 必须在 [%s, %s] 百分比区间内" % (key, lo, hi),
                    )
                )
                ok = False
                continue
            entry[key] = number

        if "samples" not in item:
            problems.append(_problem("missing_field", "%s.samples" % where, "维度得分缺少 samples"))
            ok = False
            continue
        samples = item.get("samples")
        if isinstance(samples, bool) or not isinstance(samples, (int, float)) or float(samples) != int(samples):
            problems.append(_problem("type_error", "%s.samples" % where, "samples 必须是整数"))
            ok = False
            continue
        entry["samples"] = int(samples)
        if entry["samples"] < min_samples:
            problems.append(_problem("range_error", "%s.samples" % where, "samples 必须不小于 %d" % min_samples))
            ok = False

        ratio = item.get("ratio")
        if ratio is not None:
            if not _is_number(ratio):
                problems.append(_problem("type_error", "%s.ratio" % where, "ratio 必须是数字"))
                ok = False
            else:
                number = float(ratio)
                if number < float(cfg["validation"]["ratio_min"]) or number > float(cfg["validation"]["ratio_max"]):
                    problems.append(
                        _problem("range_error", "%s.ratio" % where, "ratio 必须在 [0, 1] 区间内")
                    )
                    ok = False
                else:
                    entry["ratio"] = number
        if ok:
            dims.append(entry)
    return dims


def _finalize_dimensions(
    dims: List[dict],
    total: int,
    cfg: dict,
    warnings: List[dict],
) -> None:
    """补齐 ratio，并做占比/样本数一致性告警（只告警不改数）。

    契约口径：``dimensions[]`` 是「维度取值」的**互斥分组**（一条样本只带一个
    ``perturb`` 快照），因此全部取值的占比合计应为 1、样本数合计应等于样本总数。
    """
    digits = int(cfg["precision"]["ratio_digits"])
    tol = float(cfg["validation"]["ratio_tolerance"])
    ratio_sum = 0.0
    samples_sum = 0
    for dim in dims:
        expected = float(dim["samples"]) / float(total) if total else 0.0
        if "ratio" not in dim:
            dim["ratio"] = round(expected, digits)
        else:
            if abs(dim["ratio"] - expected) > tol:
                warnings.append(
                    {
                        "code": "ratio_mismatch",
                        "detail": "%s=%s 的 ratio=%.4f 与 samples/total=%.4f 偏差超过 %.2f"
                        % (dim["dimension"], dim["value"], dim["ratio"], expected, tol),
                    }
                )
        ratio_sum += float(dim["ratio"])
        samples_sum += int(dim["samples"])

    if dims and abs(ratio_sum - 1.0) > tol:
        warnings.append(
            {
                "code": "dimension_ratio_sum_mismatch",
                "detail": "全部维度取值的占比合计 %.4f，偏离 1.0 超过 %.2f（维度取值应为互斥分组）"
                % (ratio_sum, tol),
            }
        )
    if dims and total and samples_sum != total:
        warnings.append(
            {
                "code": "dimension_samples_mismatch",
                "detail": "全部维度取值的样本数合计 %d，与样本总数 %d 不一致" % (samples_sum, total),
            }
        )


def _cross_check_samples(
    samples: List[dict],
    error_mix: dict,
    warnings: List[dict],
) -> None:
    """样本级错误形态计数与 error_mix 汇总做一致性告警（只告警不改数）。"""
    counts = Counter(row["error_type"] for row in samples if row["error_type"] != "none")
    for key in ERROR_MIX_KEYS:
        if counts.get(key, 0) != error_mix.get(key, 0):
            warnings.append(
                {
                    "code": "error_mix_mismatch",
                    "detail": "error_mix.%s=%d 与 sample_results 中该形态样本数 %d 不一致"
                    % (key, error_mix.get(key, 0), counts.get(key, 0)),
                }
            )


def _validate_record(
    raw: Any,
    cfg: dict,
    bank: Dict[str, Any],
    required_extra: Sequence[str],
) -> Tuple[Optional[dict], List[dict], List[dict]]:
    """校验一条回传记录；返回 ``(归一化记录 or None, problems, warnings)``。"""
    problems: List[dict] = []
    warnings: List[dict] = []
    max_len = int(cfg["validation"]["max_id_len"])

    if not isinstance(raw, dict):
        return None, [_problem("not_object", "-", "该行不是 JSON 对象")], warnings

    required = list(CONTRACT_FIELDS) + [f for f in required_extra if f not in CONTRACT_FIELDS]
    missing = [f for f in required if f not in raw or raw[f] in (None, "")]
    if missing:
        return None, [
            _problem("missing_field", field, "缺少必填字段 %s" % field) for field in missing
        ], warnings

    for key in ("external_task_id", "version_id"):
        value = raw.get(key)
        if not isinstance(value, str):
            problems.append(_problem("type_error", key, "%s 必须是字符串" % key))
        elif len(value.strip()) > max_len:
            problems.append(_problem("range_error", key, "%s 超过 %d 字符" % (key, max_len)))
    if problems:
        return None, problems, warnings

    external_task_id = str(raw["external_task_id"]).strip()
    version_id = str(raw["version_id"]).strip()

    # 版本存在性：不存在 → 整条解析失败，绝不落库
    if version_id not in bank["versions"]:
        return None, [
            _problem(
                "version_not_found",
                "version_id",
                "版本 ID %s 不在版本库中（版本库来源：%s）" % (version_id, bank["meta"]["source"]),
            )
        ], warnings

    metrics = _validate_metrics(raw, cfg, problems)
    error_mix = _validate_error_mix(raw, cfg, problems)
    dimensions = _validate_dimensions(raw, cfg, problems)
    samples = _validate_sample_results(raw, cfg, problems)
    if problems:
        return None, problems, warnings

    total = _derive_total_samples(raw, dimensions, samples, cfg, problems)
    if problems or total is None:
        return None, problems or [_problem("range_error", "total_samples", "样本总数无效")], warnings

    _finalize_dimensions(dimensions, total, cfg, warnings)
    _cross_check_samples(samples, error_mix or {}, warnings)

    model = raw.get("model")
    if model is None or (isinstance(model, str) and not model.strip()):
        model = None
        warnings.append({"code": "missing_model", "detail": "回传未带 model，下游按模型对比时将无法匹配"})
    elif not isinstance(model, str):
        problems.append(_problem("type_error", "model", "model 必须是字符串"))
    elif len(model.strip()) > max_len:
        problems.append(_problem("range_error", "model", "model 超过 %d 字符" % max_len))
    if problems:
        return None, problems, warnings

    attributable = version_id not in bank["rolled"]
    record = {
        "eval_id": "%s-%s" % (cfg["ids"]["eval_id_prefix"], external_task_id),
        "external_task_id": external_task_id,
        "version_id": version_id,
        "model": model.strip() if isinstance(model, str) else None,
        "metrics": {key: metrics[key] for key in METRIC_KEYS},
        "dimensions": dimensions,
        "error_mix": {key: error_mix[key] for key in ERROR_MIX_KEYS},
        "sample_results": samples,
        "attributable": attributable,
        # 只增字段：便于下游/排查，不改动契约字段
        "received_at": _now_iso(),
        "sample_count": total,
        "sample_results_mode": "full" if len(samples) == total else "subset",
        "version_status": cfg["versions"]["rolled_back_status"] if not attributable else cfg["versions"]["default_status"],
        "warnings": list(warnings),
    }
    return record, problems, warnings


# --------------------------------------------------------------------------- #
# 主流程
# --------------------------------------------------------------------------- #
def run(ctx: OperatorContext) -> dict:
    cfg = ctx.config
    p = _parse_params(ctx.params or {}, cfg)

    ctx.log.route("回传 JSONL 解析 → 六项字段校验 → 版本 ID 核对 → 落库")
    input_path = _resolve_input(ctx, cfg)

    rows = _iter_records(input_path)
    if not rows:
        raise schemas.OpError("invalid_jsonl: 评测结果文件为空：%s" % input_path.name)
    if not any(read_error != "blank" for _, _, read_error in rows):
        raise schemas.OpError("invalid_jsonl: 评测结果文件中没有可解析的记录：%s" % input_path.name)
    ctx.log.read(len(rows), "行")

    bank = _build_version_bank(p, ctx, cfg)
    ctx.log.validated(input_path.name)

    results: "OrderedDict[str, dict]" = OrderedDict()
    parse_failed: List[dict] = []
    warnings: List[dict] = []
    blank_lines = 0
    deduplicated = 0
    duplicate_events: List[dict] = []
    progress_step = int(cfg["logs"]["progress_step"])
    preview_len = int(cfg["io"]["raw_preview_len"])

    total = len(rows)
    for idx, (lineno, raw, read_error) in enumerate(rows, start=1):
        if progress_step and (idx % progress_step == 0 or idx == total):
            ctx.log.progress(idx, total)

        if read_error == "blank":
            blank_lines += 1
            continue
        if read_error:
            parse_failed.append(
                {
                    "source_line": lineno,
                    "reason_code": "invalid_json",
                    "reason": read_error,
                    "raw_preview": "",
                }
            )
            continue

        record, problems, record_warnings = _validate_record(raw, cfg, bank, p["required_extra"])
        if record is None:
            primary = problems[0] if problems else _problem("internal_error", "-", "未知校验失败")
            parse_failed.append(
                {
                    "source_line": lineno,
                    "external_task_id": raw.get("external_task_id") if isinstance(raw, dict) else None,
                    "version_id": raw.get("version_id") if isinstance(raw, dict) else None,
                    "reason_code": primary["code"],
                    "reason": primary["message"],
                    "problems": problems,
                    "raw_preview": _preview(raw, preview_len),
                }
            )
            continue

        warnings.extend(
            dict(item, external_task_id=record["external_task_id"]) for item in record_warnings
        )

        key = record["external_task_id"]
        policy = str(cfg["validation"]["duplicate_policy"]).lower()
        if key in results:
            deduplicated += 1
            duplicate_events.append(
                {"external_task_id": key, "policy": policy, "reason": "同一 external_task_id 重推"}
            )
            if policy == "first_wins":
                continue
            results.pop(key)
        results[key] = record

    failed_count = len(parse_failed)
    valid_records = list(results.values())
    attributable = [r for r in valid_records if r["attributable"]]
    rolled_back = [r for r in valid_records if not r["attributable"]]

    # --- 落盘 --------------------------------------------------------------- #
    results_path = iofs.write_jsonl(ctx.out(cfg["io"]["output_results"]), valid_records)
    failed_path = iofs.write_jsonl(ctx.out(cfg["io"]["output_parse_failed"]), parse_failed)

    by_reason = Counter(item["reason_code"] for item in parse_failed)
    by_warning = Counter(item["code"] for item in warnings)
    statistics = {
        "input_lines": total,
        "blank_lines": blank_lines,
        "valid_records": len(valid_records),
        "parse_failed": failed_count,
        "deduplicated": deduplicated,
        "attributable": len(attributable),
        "not_attributable": len(rolled_back),
        "total_samples": sum(int(r["sample_count"]) for r in valid_records),
        "dimension_entries": sum(len(r["dimensions"]) for r in valid_records),
        "sample_result_entries": sum(len(r["sample_results"]) for r in valid_records),
        "warnings": len(warnings),
        "known_versions": bank["meta"]["count"],
        "rolled_back_versions": len(bank["rolled"]),
    }
    if failed_count and valid_records:
        status = "partial"
    elif failed_count and not valid_records:
        status = "failed"
    else:
        status = "success"

    max_listed = int(cfg["io"]["max_listed"])
    report = {
        "operator": OPERATOR_NAME,
        "operator_id": OPERATOR_ID,
        "operator_directory": "AVP_EVAL_DATA_007",
        "generated_at": _now_iso(),
        "input_file": input_path.name,
        "version_bank": bank["meta"],
        "parameters": {
            "known_versions_provided": p["known_versions"],
            "rolled_back_versions_provided": p["rolled_back_versions"],
            "registry_path": p["registry_path"],
            "extra_required_fields": p["required_extra"],
            "duplicate_policy": str(cfg["validation"]["duplicate_policy"]).lower(),
        },
        "checks": {
            "required_fields": list(CONTRACT_FIELDS) + list(p["required_extra"]),
            "rules": [
                "必填六项齐全且类型正确",
                "metrics/dimensions 指标单位为百分比，取值 0–100",
                "dimensions 每项含 dimension/value/cer/wer/ser/samples",
                "error_mix 含 sub/del/ins/unk 且为非负整数",
                "sample_results 每项含 sample_id 与错误形态（缺失时由 ref/hyp 推导）",
                "版本 ID 必须存在于版本库；不存在 → 整条解析失败不落库",
                "版本已回滚 → 落库但 attributable=false",
            ],
            "duplicate_policy": str(cfg["validation"]["duplicate_policy"]).lower(),
        },
        "statistics": statistics,
        "parse_failed_by_reason": dict(sorted(by_reason.items())),
        "warnings_by_code": dict(sorted(by_warning.items())),
        "parse_failed_detail": parse_failed[:max_listed],
        "parse_failed_truncated": max(0, failed_count - max_listed),
        "warnings_detail": warnings[:max_listed],
        "warnings_truncated": max(0, len(warnings) - max_listed),
        "duplicate_events": duplicate_events[:max_listed],
        "conclusion": status,
        "results_preview": [
            {
                "eval_id": r["eval_id"],
                "external_task_id": r["external_task_id"],
                "version_id": r["version_id"],
                "model": r["model"],
                "attributable": r["attributable"],
                "sample_count": r["sample_count"],
                "cer": r["metrics"]["cer"],
            }
            for r in valid_records[:max_listed]
        ],
    }
    report_path = iofs.write_json(ctx.out(cfg["io"]["output_report"]), report)

    # --- 日志 --------------------------------------------------------------- #
    ctx.log.write_output(results_path.name)
    ctx.log.write_output(failed_path.name)
    ctx.log.write_output(report_path.name)
    if valid_records:
        ctx.log.result(
            "评测结果接收",
            output=len(valid_records),
            success=len(valid_records),
            failed=failed_count,
            skipped=deduplicated,
            extra={"已回滚": len(rolled_back)},
        )
    if rolled_back:
        ctx.log.warning("%d 条评测结果对应已回滚版本，落库但标记 attributable=false" % len(rolled_back))
    if deduplicated:
        ctx.log.warning("%d 条 external_task_id 重推，按 %s 策略去重" % (deduplicated, str(cfg["validation"]["duplicate_policy"]).lower()))
    if warnings:
        ctx.log.warning("%d 条一致性问题仅告警不改数（如 error_mix 与样本级计数不一致）" % len(warnings))
    if failed_count and valid_records:
        ctx.log.partial(
            "部分评测结果解析失败，未落库",
            "失败 %d 条，原因：%s" % (failed_count, "、".join("%s %d" % kv for kv in sorted(by_reason.items()))),
        )
    elif failed_count:
        ctx.log.error(
            "评测结果接收",
            "全部 %d 条解析失败、未落库（原因：%s）"
            % (total, "、".join("%s %d" % kv for kv in sorted(by_reason.items()))),
        )

    return {
        "outputs": {
            "results": results_path,
            "parse_failed": failed_path,
            "report": report_path,
        },
        "statistics": statistics,
        "result": {
            "eval_ids": [r["eval_id"] for r in valid_records[:max_listed]],
            "eval_count": len(valid_records),
            "parse_failed": failed_count,
            "attributable": len(attributable),
            "not_attributable": len(rolled_back),
            "conclusion": status,
        },
        "status": status,
    }


__all__ = ["run", "OPERATOR_ID", "OPERATOR_NAME"]
