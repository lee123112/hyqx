"""单位白名单与受控换算。

设计约束（对应 DEVELOPMENT_PLAN / TECHNICAL_DESIGN §1）：

- 只换算白名单内的单位：同一 dimension 内统一换算到基准单位后比较；
- 不在白名单的单位（furlong、stone、barrel 等）**不做任何猜测性换算**，只按普通数字处理；
- 温度使用显式公式（基准摄氏度），不使用线性因子；
- 不同 dimension 之间永不换算。

单位代码在全局唯一（m/kg/s/...），因此配置里的 `unit_aliases` 只需给出单位代码。
"""
import re
from typing import Dict, Optional, Tuple

# dimension -> {单位代码: 到基准单位的因子}
_LINEAR: Dict[str, Dict[str, float]] = {
    "length": {
        "m": 1.0, "km": 1000.0, "cm": 0.01, "mm": 0.001, "um": 1e-06,
        "mi": 1609.344, "ft": 0.3048, "yd": 0.9144, "nmi": 1852.0,
    },
    "mass": {
        "kg": 1.0, "g": 0.001, "mg": 1e-06, "t": 1000.0,
        "lb": 0.45359237, "oz": 0.028349523125,
    },
    "area": {
        "m2": 1.0, "km2": 1e06, "cm2": 1e-04, "ha": 10000.0, "mu": 2000.0 / 3.0,
    },
    "volume": {
        "l": 1.0, "ml": 0.001, "m3": 1000.0, "gal": 3.785411784,
    },
    # 2026-11-04：补齐 year/month/quarter。月与季度都按「一年 = 365 天」整除，
    # 保证 1 year == 12 months == 4 quarters 精确相等，避免换算误差把正确译文判成不一致。
    "duration": {
        "s": 1.0, "ms": 0.001, "min": 60.0, "h": 3600.0, "d": 86400.0, "wk": 604800.0,
        "mo": 2628000.0,        # 365d / 12
        "qtr": 7884000.0,       # 365d / 4
        "yr": 31536000.0,       # 365d
    },
    # 温度用显式公式，因子不参与换算，仅用于占位与单位代码校验。
    "temperature": {"c": 1.0, "f": 1.0, "k": 1.0},
}

_CODE_TO_DIMENSION: Dict[str, str] = {}
for _dimension, _table in _LINEAR.items():
    for _code in _table:
        _CODE_TO_DIMENSION[_code] = _dimension

CANONICAL_KEYS = frozenset(_CODE_TO_DIMENSION)
DIMENSIONS = frozenset(_LINEAR)

# 别名 -> 单位代码；统一小写登记，匹配时也用小写查询。
_ALIASES: Dict[str, str] = {}


def _register(code: str, aliases) -> None:
    _ALIASES[code.lower()] = code
    for alias in aliases:
        _ALIASES[alias.lower()] = code


# 长度
_register("m", ["meter", "meters", "metre", "metres", "米"])
_register("km", ["kilometer", "kilometers", "kilometre", "kilometres", "千米", "公里"])
_register("cm", ["centimeter", "centimeters", "centimetre", "centimetres", "厘米"])
_register("mm", ["millimeter", "millimeters", "millimetre", "millimetres", "毫米"])
_register("um", ["micrometer", "micrometers", "micron", "microns", "微米"])
_register("mi", ["mile", "miles", "英里"])
_register("ft", ["foot", "feet", "英尺"])
_register("yd", ["yard", "yards", "码"])
_register("nmi", ["nauticalmile", "nauticalmiles", "海里"])
# 质量
_register("kg", ["kilogram", "kilograms", "kilogramme", "千克", "公斤"])
_register("g", ["gram", "grams", "gramme", "克"])
_register("mg", ["milligram", "milligrams", "毫克"])
_register("t", ["tonne", "tonnes", "ton", "tons", "metricton", "吨"])
_register("lb", ["pound", "pounds", "磅"])
_register("oz", ["ounce", "ounces", "盎司"])
# 面积
_register("m2", ["sqm", "squaremeter", "squaremeters", "平方米", "平米"])
_register("km2", ["sqkm", "squarekilometer", "squarekilometers", "平方公里"])
_register("cm2", ["sqcm", "squarecentimeter", "squarecentimeters", "平方厘米"])
_register("ha", ["hectare", "hectares", "公顷"])
_register("mu", ["亩"])
# 体积
_register("l", ["liter", "liters", "litre", "litres", "升"])
_register("ml", ["milliliter", "milliliters", "millilitre", "millilitres", "毫升"])
_register("m3", ["cubicmeter", "cubicmeters", "立方米"])
_register("gal", ["gallon", "gallons", "加仑"])
# 时长
_register("s", ["sec", "secs", "second", "seconds", "秒"])
_register("ms", ["millisecond", "milliseconds", "毫秒"])
_register("min", ["minute", "minutes", "分钟"])
_register("h", ["hr", "hrs", "hour", "hours", "小时"])
_register("d", ["day", "days", "天"])
_register("wk", ["week", "weeks", "周", "星期"])
_register("mo", ["month", "months", "mo", "个月", "月"])
_register("qtr", ["quarter", "quarters", "qtr", "qtrs", "个季度", "季度", "季"])
_register("yr", ["year", "years", "yr", "yrs", "年"])
# 温度
_register("c", ["°c", "℃", "celsius", "degreecelsius", "摄氏度", "度c"])
_register("f", ["°f", "℉", "fahrenheit", "华氏度", "度f"])
_register("k", ["°k", "kelvin", "开尔文", "开氏度"])


def build_unit_aliases(extra: Optional[Dict[str, str]] = None) -> Dict[str, str]:
    """合并默认别名与任务自定义别名（别名 -> 单位代码），返回小写键的映射。"""
    aliases = dict(_ALIASES)
    for alias, code in (extra or {}).items():
        if not isinstance(alias, str) or not alias.strip():
            raise ValueError("unit_aliases 的别名不能为空")
        if not isinstance(code, str) or code.strip().lower() not in CANONICAL_KEYS:
            raise ValueError(f"unit_aliases 的值必须是白名单单位代码：{alias} -> {code}")
        aliases[alias.strip().lower()] = code.strip().lower()
    return aliases


def resolve_alias(alias: str, alias_map: Optional[Dict[str, str]] = None) -> Optional[Tuple[str, str]]:
    """别名 -> (dimension, 单位代码)。不可识别返回 None，绝不猜测。"""
    table = alias_map if alias_map is not None else _ALIASES
    code = table.get((alias or "").strip().lower())
    if not code:
        return None
    dimension = _CODE_TO_DIMENSION.get(code)
    if not dimension:
        return None
    return dimension, code


def build_unit_regex(alias_map: Dict[str, str]) -> "re.Pattern":
    """构造「数量 + 单位别名」的匹配模式；别名按长度倒序，保证最长优先。

    2026-11 修订：数量支持空格千分位（`1 650`）与量级词（`16.5 million tons`、`1 650万吨`），
    量级由调用方乘进数值后再按单位换算；组序固定为 1=数量、2=量级、3=单位。

    2026-11-04 修订：负号只在前面不是数字时才当正负号（`-5 °C` 保留负值），
    避免把区间 `2015-2017年`、`15-19 years` 的后半截 `-2017年` / `-19 years`
    误当成负数时长（实测在 flow_15 上造成 4 条 unit/number 假拦截）。
    """
    aliases = sorted(alias_map, key=lambda item: (-len(item), item))
    alternation = "|".join(re.escape(alias) for alias in aliases)
    pattern = (
        r"((?<!\d)[+-]?(?:\d{1,3}(?:[,_]\d{3}|\s\d{3})+|\d+)(?:\.\d+)?)"
        r"(?:\s*(亿|万|thousand|million|billion)(?![A-Za-z]))?"
        r"\s*(" + alternation + r")"
        r"(?![A-Za-z0-9°])"
    )
    return re.compile(pattern, re.IGNORECASE)


def to_base(dimension: str, code: str, value: float) -> Optional[float]:
    """按白名单把数值换算到该 dimension 的基准单位。"""
    if dimension == "temperature":
        if code == "c":
            return value
        if code == "f":
            return (value - 32.0) * 5.0 / 9.0
        if code == "k":
            return value - 273.15
        return None
    factor = _LINEAR.get(dimension, {}).get(code)
    if factor is None:
        return None
    return value * factor
