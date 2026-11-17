"""文本抽取与归一化：数字、百分比、货币、日期、时间、单位与可配置实体。

工程口径（对应 DEVELOPMENT_PLAN「核心处理逻辑」与 TECHNICAL_DESIGN §1/§4）：

1. **优先级消费**：实体 -> 日期 -> 时间 -> 货币 -> 百分比 -> 单位 -> 数字。
   先匹配的长跨度类型会「消费」字符区间，避免日期、货币、单位里的数字被重复抽取；
2. **归一化**：Unicode 十进制数字（含全角）统一成 ASCII；千分位与十进制尾零归一；
   大小写按 casefold；日期/时间统一成 ISO 形态；单位按白名单换算到基准单位；
3. **无法解析**：形如 `1.2.3`、`2024-13-45`、`25:99` 的畸形数字/日期会被「保留」下来，
   既不当作数字，也不静默丢弃，而是产出 unparsed 证据（只含哈希与长度，不含原文）；
4. **白名单外单位**：furlong 之类不做任何猜测性换算，只按普通数字处理。
"""
import calendar
import hashlib
import re
import unicodedata
from typing import Dict, List, Optional, Tuple

from .units import build_unit_aliases, build_unit_regex, resolve_alias, to_base

CATEGORY_ORDER = ("entity", "date", "time", "currency", "percentage", "unit", "number")

# --------------------------------------------------------------------------- #
# 归一化
# --------------------------------------------------------------------------- #

_FULLWIDTH_MAP = {
    "\u3000": " ",   # 表意空格
    "％": "%",
    "．": ".",
    "＋": "+",
    "－": "-",
    "＄": "$",
    "￡": "£",
    "￥": "¥",
}


def normalize_text(text: str) -> str:
    """1:1 字符归一（不改变长度，因此匹配跨度对原文仍然有效）。

    仅归一 Unicode 十进制数字（如 `１２３４`、`١٢٣٤`）与上表中的全角符号；
    刻意**不**把全角逗号 `，` 转成 `,`，避免把中文顿挫误判成千分位。
    """
    out: List[str] = []
    for ch in text:
        if ch.isdigit() and ch not in "0123456789":
            try:
                out.append(str(unicodedata.digit(ch)))
                continue
            except (TypeError, ValueError):
                pass
        out.append(_FULLWIDTH_MAP.get(ch, ch))
    return "".join(out)


def format_number(value: float) -> str:
    """数值 -> 稳定字符串：去掉尾零与浮点噪声，保证 3.50 与 3.5 等价。"""
    if value != value or value in (float("inf"), float("-inf")):
        raise ValueError("无法格式化非有限数值")
    rounded = round(value, 10)
    if rounded == 0:
        return "0"
    if abs(rounded) < 1e15 and rounded == int(rounded):
        return str(int(rounded))
    text = f"{rounded:.10g}"
    return "0" if text == "-0" else text


def _parse_number(raw: str) -> Optional[float]:
    """去掉千分位（含空格/不换行空格）与英文序数后缀后转数值。"""
    cleaned = raw.replace(",", "").replace("_", "").replace(" ", "").replace(" ", "")
    cleaned = re.sub(r"(?:st|nd|rd|th)$", "", cleaned, flags=re.IGNORECASE)
    try:
        return float(cleaned)
    except (TypeError, ValueError):
        return None


def _digest(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()[:16]


class Token:
    """一个已归一化的可比较实体。数值类只带归一值，实体类只带哈希与长度。"""

    __slots__ = ("category", "normalized", "span", "numeric_value", "subtype", "digest", "length")

    def __init__(self, category, normalized, span, numeric_value=None,
                 subtype=None, digest=None, length=None):
        self.category = category
        self.normalized = normalized
        self.span = span
        self.numeric_value = numeric_value
        self.subtype = subtype
        self.digest = digest
        self.length = length

    def __repr__(self) -> str:  # pragma: no cover - 仅调试用
        return f"Token({self.category!r}, {self.normalized!r}, {self.span})"


class Unparsed:
    """畸形但含指示符的片段；只暴露哈希与长度，不暴露原文。"""

    __slots__ = ("category", "span", "reason", "digest", "length")

    def __init__(self, category, span, reason, digest, length):
        self.category = category
        self.span = span
        self.reason = reason
        self.digest = digest
        self.length = length


# --------------------------------------------------------------------------- #
# 正则
# --------------------------------------------------------------------------- #

# 数字本体（2026-11 修订）：
# - 逗号千分位必须紧跟 3 位数字（`251, 252, 254` 是并列列举，不合并）；
# - 空格千分位只在货币/单位锚定处启用（`3 747 800美元`、`1 650万吨`），
#   裸数字保持旧口径，避免把 `100 200 300` 这类列表并成一个数；
# - 允许英文序数后缀（`17th`、`348th`），数值按整数比较；
# - 句末 `.25` 这类脚注号在抽取时单独跳过（见 _is_trailing_footnote）。
_NUM_PLAIN_BODY = r"(?:\d{1,3}(?:[,_]\d{3})+|\d+)(?:\.\d+)?"
_NUM_SPACE_BODY = r"(?:\d{1,3}(?:[,_]\d{3}|\s\d{3})+|\d+)(?:\.\d+)?"
# 只在货币符号后放宽：`$1, 958.6 million` 的逗号+空格确实是千分位。
_NUM_LOOSE_BODY = r"(?:\d{1,3}(?:[,_]\s?\d{3}|\s\d{3})+|\d+)(?:\.\d+)?"
_AMOUNT_BODY = _NUM_PLAIN_BODY
_NUMBER = re.compile(
    r"(?<![0-9A-Za-z_])[+-]?" + _AMOUNT_BODY + r"(?:st|nd|rd|th)?(?![0-9A-Za-z_])",
    re.IGNORECASE,
)
# 量级词：中英各一套，乘到数字上再比较（`2.639亿` == `263.9 million`）。
_MAGNITUDE = {"千": 1e3, "万": 1e4, "亿": 1e8,
              "thousand": 1e3, "million": 1e6, "billion": 1e9}
_MAGNITUDE_RE = re.compile(r"\s*(亿|万|thousand|million|billion)(?![A-Za-z])", re.IGNORECASE)
# 段落编号：`12. 俄罗斯联邦政府`、`8. The facilitator…`——序号不是数量。
_ORDINAL_MARKS = (".", "、")
_PERCENT = re.compile(r"(?<![0-9A-Za-z_])[+-]?\d+(?:\.\d+)?\s*%(?![0-9A-Za-z_])")
_PERCENT_CN = re.compile(r"百分之\s*(\d+(?:\.\d+)?)")
_PERCENT_EN = re.compile(
    r"(?<![0-9A-Za-z_])([+-]?\d+(?:\.\d+)?)\s*(?:percent|per\s+cent)(?![A-Za-z])",
    re.IGNORECASE,
)

_MONTHS = {
    "january": 1, "february": 2, "march": 3, "april": 4, "may": 5, "june": 6,
    "july": 7, "august": 8, "september": 9, "october": 10, "november": 11, "december": 12,
    "jan": 1, "feb": 2, "mar": 3, "apr": 4, "jun": 6, "jul": 7, "aug": 8,
    "sep": 9, "sept": 9, "oct": 10, "nov": 11, "dec": 12,
}

_DATE_YMD = re.compile(r"(?<!\d)(\d{4})[-/.](\d{1,2})[-/.](\d{1,2})(?!\d)")
# 中文日期允许全角/空格排版：`2018 年 9月 29 日`（UN 文件里常见）。
_DATE_CN = re.compile(r"(?<!\d)(\d{4})\s*年\s*(\d{1,2})\s*月\s*(\d{1,2})\s*日")
_DATE_EN_MD = re.compile(
    r"(?<![A-Za-z])([A-Za-z]{3,9})\.?\s+(\d{1,2})(?:st|nd|rd|th)?,?\s+(\d{4})(?!\d)", re.IGNORECASE
)
_DATE_EN_DM = re.compile(
    r"(?<![A-Za-z])(\d{1,2})(?:st|nd|rd|th)?\s+([A-Za-z]{3,9})\.?,?\s+(\d{4})(?!\d)", re.IGNORECASE
)
# 只有年月、没有日的写法：`2013年6月` / `June 2013`，统一归一成 `YYYY-MM`。
# 负向前瞻保证 `2013年6月30日`、`16 July 2015` 仍走完整日期。
_DATE_CN_MY = re.compile(r"(?<!\d)(\d{4})年\s*(\d{1,2})月(?!\s*\d{1,2}\s*日)")
_DATE_EN_MY = re.compile(r"(?<![A-Za-z])([A-Za-z]{3,9})\.?\s+(\d{4})(?!\d)", re.IGNORECASE)
# 日期区间：`2015年7月13日至16日`、`13 to 16 July 2015`，拆成两个日期再比。
_DATE_RANGE_CN = re.compile(
    r"(?<!\d)(\d{4})年\s*(\d{1,2})月\s*(\d{1,2})日(?:至|到|和|、|及)\s*"
    r"(?:(\d{1,2})月\s*)?(\d{1,2})日")
_DATE_RANGE_EN = re.compile(
    r"(?<![A-Za-z0-9])(\d{1,2})\s*(?:to|through|and|[-–—])\s*(\d{1,2})\s+"
    r"([A-Za-z]{3,9})\.?,?\s+(\d{4})(?!\d)", re.IGNORECASE
)
# 跨月区间：`27 April to 8 May 2015`（两端的月名各自出现）。
_DATE_RANGE_EN_XM = re.compile(
    r"(?<![A-Za-z0-9])(\d{1,2})\s+([A-Za-z]{3,9})\.?\s*(?:to|through|and|[-–—])\s*"
    r"(\d{1,2})\s+([A-Za-z]{3,9})\.?,?\s+(\d{4})(?!\d)", re.IGNORECASE
)
# 没有年份的月日：`4月7日` / `7 April`，归一成 `--MM-DD`。
# 与完整日期不互通（`2015-04-07` != `--04-07`），避免跨句把「某年某日」和「某月某日」判成同一个。
_DATE_CN_MD = re.compile(r"(?<!\d)(\d{1,2})月(\d{1,2})日(?!\d)")
_DATE_EN_DM_NY = re.compile(
    r"(?<![A-Za-z0-9])(\d{1,2})(?:st|nd|rd|th)?\s+([A-Za-z]{3,9})\.?(?![A-Za-z])", re.IGNORECASE
)
_DATE_EN_MD_NY = re.compile(
    r"(?<![A-Za-z])([A-Za-z]{3,9})\.?\s+(\d{1,2})(?:st|nd|rd|th)?(?![A-Za-z0-9])", re.IGNORECASE
)

_TIME_AMPM = re.compile(
    r"(?<![0-9:])(\d{1,2})(?::(\d{2}))?\s*([AaPp])\.?\s?[Mm]\.?(?![A-Za-z])"
)
_TIME_HMS = re.compile(r"(?<![0-9:])(\d{1,2}):(\d{2})(?::(\d{2}))?(?![0-9:])")
_TIME_CN = re.compile(r"(上午|下午|中午|凌晨|晚上|早上|清晨)?\s*(\d{1,2})\s*点(?:\s*(\d{1,2})\s*分)?")

_CURRENCY_SYMBOLS = ("$", "€", "£", "¥")
_CURRENCY_ALIASES = {
    "$": "USD", "dollar": "USD", "dollars": "USD", "美元": "USD", "usd": "USD",
    "€": "EUR", "euro": "EUR", "euros": "EUR", "欧元": "EUR", "eur": "EUR",
    "£": "GBP", "英镑": "GBP", "gbp": "GBP",
    "¥": "CNY", "元": "CNY", "人民币": "CNY", "yuan": "CNY", "renminbi": "CNY",
    "cny": "CNY", "rmb": "CNY",
    "日元": "JPY", "yen": "JPY", "jpy": "JPY",
}
_CURRENCY_CODES = ("USD", "EUR", "GBP", "JPY", "CNY", "RMB")

_AMPM_PREFIX = {
    "上午": "am", "早上": "am", "清晨": "am", "凌晨": "am",
    "下午": "pm", "晚上": "pm", "中午": "pm",
}

# 畸形片段（先于数字抽取执行，避免把 1.2.3 拆成 1.2 与 3）
_LOOSE_SHAPES = (
    (re.compile(r"(?<![0-9A-Za-z_])\d+(?:[.,]\d+){2,}(?![0-9A-Za-z_])"), "number", "unparsed_number"),
    (re.compile(r"(?<!\d)\d{4}年\d{1,2}月\d{1,2}日"), "date", "unparsed_date"),
    (re.compile(r"(?<!\d)\d{1,4}[-/.]\d{1,2}[-/.]\d{1,4}(?!\d)"), "date", "unparsed_date"),
    (re.compile(r"(?<![0-9:])\d{1,2}:\d{2}(?::\d{2})?(?![0-9:])"), "time", "unparsed_time"),
    (re.compile(r"\d+(?:\.\d+)?\s*%"), "percentage", "unparsed_percentage"),
    (re.compile(r"[$€£¥]\s*\d|\d+(?:\.\d+)?\s*(?:元|美元|欧元|英镑|日元|人民币)"),
     "currency", "unparsed_currency"),
)


def _valid_ymd(year: int, month: int, day: int) -> bool:
    if not 1 <= month <= 12:
        return False
    if year < 1000 or year > 9999:
        return False
    return 1 <= day <= calendar.monthrange(year, month)[1]


def _iso_date(year: int, month: int, day: int) -> Optional[str]:
    if not _valid_ymd(year, month, day):
        return None
    return f"{year:04d}-{month:02d}-{day:02d}"


def _iso_time(hour: int, minute: int, second: Optional[int] = None) -> Optional[str]:
    if not 0 <= hour <= 23 or not 0 <= minute <= 59:
        return None
    if second is None:
        return f"{hour:02d}:{minute:02d}"
    if not 0 <= second <= 59:
        return None
    return f"{hour:02d}:{minute:02d}:{second:02d}"


def build_percentage_regex() -> Tuple:
    return (
        ("symbol", _PERCENT),
        ("percent_word", _PERCENT_EN),
        ("percent_cn", _PERCENT_CN),
    )


class ExtractionContext:
    """把配置编译成可复用的抽取器，避免逐条记录重复编译正则。"""

    def __init__(self, cfg):
        self.cfg = cfg
        self.categories = set(cfg.normalized_categories())
        self.entity_patterns = [
            (name, re.compile(pattern, re.IGNORECASE))
            for name, patterns in (cfg.entity_patterns or {}).items()
            for pattern in patterns
        ]
        self.unit_aliases = build_unit_aliases(cfg.unit_aliases or {})
        self.unit_regex = build_unit_regex(self.unit_aliases) if "unit" in self.categories else None
        self.date_patterns = self._build_date_patterns()
        self.date_range_patterns = self._build_date_range_patterns()
        self.time_patterns = (
            ("ampm", _TIME_AMPM), ("hms", _TIME_HMS), ("cn", _TIME_CN)
        )
        self.currency_aliases = self._build_currency_aliases(cfg.currency_aliases or {})
        self.currency_patterns = self._build_currency_patterns()

    # -- 构建期 ----------------------------------------------------------- #

    def _build_date_patterns(self):
        # 顺序即优先级：完整日期在「年月」之前，否则 `16 July 2015` 会被拆成 `2015-07` + 裸数字 16。
        patterns = [("ymd", _DATE_YMD), ("cn_date", _DATE_CN), ("en_md", _DATE_EN_MD),
                    ("en_dm", _DATE_EN_DM), ("cn_month", _DATE_CN_MY), ("en_month", _DATE_EN_MY),
                    ("cn_md", _DATE_CN_MD), ("en_dm_ny", _DATE_EN_DM_NY),
                    ("en_md_ny", _DATE_EN_MD_NY)]
        return tuple(patterns)

    def _build_date_range_patterns(self):
        return (("cn_range", _DATE_RANGE_CN), ("en_range_xm", _DATE_RANGE_EN_XM),
                ("en_range", _DATE_RANGE_EN))

    def _build_currency_aliases(self, extra: Dict[str, str]) -> Dict[str, str]:
        aliases = dict(_CURRENCY_ALIASES)
        for token, code in extra.items():
            if not isinstance(token, str) or not token.strip():
                raise ValueError("currency_aliases 的键不能为空")
            if not isinstance(code, str) or not code.strip():
                raise ValueError("currency_aliases 的值不能为空")
            aliases[token.strip().lower()] = code.strip().upper()
        return aliases

    def _build_currency_patterns(self):
        symbols = sorted({s for s in self.currency_aliases if s in _CURRENCY_SYMBOLS}, key=len, reverse=True)
        words = sorted(
            {w for w in self.currency_aliases if w not in _CURRENCY_SYMBOLS and w not in _CURRENCY_CODES},
            key=lambda item: (-len(item), item),
        )
        codes = sorted(_CURRENCY_CODES, key=lambda item: (-len(item), item))
        amount = "(" + _NUM_SPACE_BODY + ")"
        amount_loose = "(" + _NUM_LOOSE_BODY + ")"
        mag = r"(?:\s*([万亿]|thousand|million|billion)(?![A-Za-z]))?"

        patterns = []
        if symbols:
            cls = "".join(re.escape(s) for s in symbols)
            patterns.append(("symbol_prefix", re.compile(
                r"([" + cls + r"])\s*" + amount_loose + mag, re.IGNORECASE)))
        if words:
            alternation = "|".join(re.escape(word) for word in words)
            patterns.append(("word_suffix", re.compile(
                r"(?<![0-9A-Za-z_])" + amount + mag + r"\s*(" + alternation + r")(?![A-Za-z])",
                re.IGNORECASE)))
        if codes:
            alternation = "|".join(re.escape(code) for code in codes)
            patterns.append(("code_prefix", re.compile(
                r"(?<![A-Za-z])(" + alternation + r")\s*" + amount + mag + r"(?![0-9A-Za-z_])",
                re.IGNORECASE)))
        return tuple(patterns)


# --------------------------------------------------------------------------- #
# 抽取
# --------------------------------------------------------------------------- #

def _resolve_date(kind: str, match) -> Optional[str]:
    try:
        if kind == "ymd":
            return _iso_date(int(match.group(1)), int(match.group(2)), int(match.group(3)))
        if kind == "cn_date":
            return _iso_date(int(match.group(1)), int(match.group(2)), int(match.group(3)))
        if kind == "en_md":
            month = _MONTHS.get((match.group(1) or "").lower().rstrip("."))
            if month is None:
                return None
            return _iso_date(int(match.group(3)), month, int(match.group(2)))
        if kind == "en_dm":
            month = _MONTHS.get((match.group(2) or "").lower().rstrip("."))
            if month is None:
                return None
            return _iso_date(int(match.group(3)), month, int(match.group(1)))
        if kind == "cn_month":
            return _iso_month(int(match.group(1)), int(match.group(2)))
        if kind == "en_month":
            month = _MONTHS.get((match.group(1) or "").lower().rstrip("."))
            if month is None:
                return None
            return _iso_month(int(match.group(2)), month)
        if kind == "cn_md":
            return _iso_month_day(int(match.group(1)), int(match.group(2)))
        if kind == "en_dm_ny":
            month = _MONTHS.get((match.group(2) or "").lower().rstrip("."))
            if month is None:
                return None
            return _iso_month_day(month, int(match.group(1)))
        if kind == "en_md_ny":
            month = _MONTHS.get((match.group(1) or "").lower().rstrip("."))
            if month is None:
                return None
            return _iso_month_day(month, int(match.group(2)))
    except (TypeError, ValueError):
        return None
    return None


def _iso_month_day(month: int, day: int) -> Optional[str]:
    """没有年份的月日；用 `--MM-DD` 占位，和完整日期互不相等。"""
    if not 1 <= month <= 12:
        return None
    if not 1 <= day <= calendar.monthrange(2000, month)[1]:
        return None
    return f"--{month:02d}-{day:02d}"


def _iso_month(year: int, month: int) -> Optional[str]:
    if not 1 <= month <= 12 or year < 1000 or year > 9999:
        return None
    return f"{year:04d}-{month:02d}"


def _resolve_date_range(kind: str, match) -> List[str]:
    """日期区间拆成两个 ISO 日期；任一端非法则整段不产出。"""
    try:
        if kind == "cn_range":
            year, month, day1 = (int(match.group(i)) for i in range(1, 4))
            month2 = int(match.group(4)) if match.group(4) else month
            day2 = int(match.group(5))
            first = _iso_date(year, month, day1)
            second = _iso_date(year, month2, day2)
        elif kind == "en_range_xm":
            day1, first_month, day2, second_month = (
                match.group(1), match.group(2), match.group(3), match.group(4))
            month = _MONTHS.get((first_month or "").lower().rstrip("."))
            month2 = _MONTHS.get((second_month or "").lower().rstrip("."))
            if month is None or month2 is None:
                return []
            year = int(match.group(5))
            first = _iso_date(year, month, int(day1))
            second = _iso_date(year, month2, int(day2))
        else:
            day1, day2, month_name, year = (
                match.group(1), match.group(2), match.group(3), int(match.group(4)))
            month = _MONTHS.get((month_name or "").lower().rstrip("."))
            if month is None:
                return []
            first = _iso_date(year, month, int(day1))
            second = _iso_date(year, month, int(day2))
    except (TypeError, ValueError):
        return []
    return [value for value in (first, second) if value]


def _resolve_time(kind: str, match) -> Optional[str]:
    try:
        if kind == "ampm":
            hour = int(match.group(1))
            minute = int(match.group(2)) if match.group(2) else 0
            marker = (match.group(3) or "").lower()
            if not 1 <= hour <= 12:
                return None
            if marker == "p" and hour < 12:
                hour += 12
            elif marker == "a" and hour == 12:
                hour = 0
            return _iso_time(hour, minute)
        if kind == "hms":
            second = int(match.group(3)) if match.group(3) else None
            return _iso_time(int(match.group(1)), int(match.group(2)), second)
        if kind == "cn":
            hour = int(match.group(2))
            minute = int(match.group(3)) if match.group(3) else 0
            prefix = match.group(1)
            if prefix in _AMPM_PREFIX and _AMPM_PREFIX[prefix] == "pm" and hour < 12:
                hour += 12
            return _iso_time(hour, minute)
    except (TypeError, ValueError):
        return None
    return None


# 日历表达式与时长同形：「2024 年」「3 月」「第 3 季度」是日期部件，不是时长。
# 只对裸中文单位做排除；「3 个月」「3 个季度」仍按duration 抽取。
_CALENDAR_YEAR_RANGE = (1000, 2999)


_RANGE_JOINERS = ("-", "–", "—", "~", "～", "至", "到", "–")


def _is_range_tail(text: str, start: int) -> bool:
    """`2015-2017年` / `15-19 years` 的后半截：紧跟在区间连接符后面，不是独立数量。"""
    before = (text or "")[:start].rstrip()
    return bool(before) and before[-1] in _RANGE_JOINERS


def _is_calendar_expression(alias: str, amount: Optional[float]) -> bool:
    if amount is None:
        return False
    label = (alias or "").strip().lower()
    if label == "年":
        return amount.is_integer() and _CALENDAR_YEAR_RANGE[0] <= amount <= _CALENDAR_YEAR_RANGE[1]
    if label == "月":
        return 1 <= amount <= 12
    if label in {"季", "季度"}:
        return 1 <= amount <= 4
    return False


def _apply_magnitude(amount: Optional[float], marker: Optional[str]) -> Optional[float]:
    """把量级词乘进金额/数量：`2.639亿` 与 `263.9 million` 都归一成 2.639e8。"""
    if amount is None or not marker:
        return amount
    factor = _MAGNITUDE.get(marker.lower())
    return amount * factor if factor else amount


def _resolve_currency(kind: str, match, aliases: Dict[str, str]) -> Optional[Tuple[str, float]]:
    if kind == "symbol_prefix":
        # 组：1=符号 2=金额 3=量级
        code = aliases.get((match.group(1) or "").lower())
        amount = _apply_magnitude(_parse_number(match.group(2)), match.group(3))
    elif kind == "word_suffix":
        # 组：1=金额 2=量级 3=货币词
        amount = _apply_magnitude(_parse_number(match.group(1)), match.group(2))
        code = aliases.get((match.group(3) or "").lower())
    else:
        # 组：1=代码 2=金额 3=量级
        code = aliases.get((match.group(1) or "").lower())
        amount = _apply_magnitude(_parse_number(match.group(2)), match.group(3))
    if not code or amount is None:
        return None
    return code.strip().upper(), amount


def _resolve_percentage(kind: str, match) -> Optional[float]:
    if kind == "percent_cn" or kind == "percent_word":
        raw = match.group(1)
    else:
        raw = match.group(0).rstrip("%").strip()
    value = _parse_number(raw)
    if value is None:
        return None
    return value / 100.0


_BLOCK_PREFIX_CHARS = ("\n", "\r", "。", "；", ";", "：", ":")


def _is_trailing_footnote(text: str, start: int, end: int) -> bool:
    """句末脚注号（`…coastal communities.25`）不是数量。

    只认「词 + `.` + 数字」且数字位于整段文本末尾的情形；
    `(r.46, Rules of Criminal Procedure…)` 这种正文中间的上标/条号照常抽取。
    """
    if text[end:].strip():
        return False
    return start >= 2 and text[start - 1] == "." and text[start - 2].isalnum()


def _is_block_ordinal(text: str, start: int, end: int) -> bool:
    """判断这个数字是不是段落/条款编号（`12. 俄罗斯联邦政府`、`8. The facilitator…`）。

    编号不是数量：源译两侧都不该因为「一边写了序号、一边没写」而判不一致。
    只认紧跟在行首或句读（。；：）之后、后面跟 `.`/`、` 的那一个数字，避免误伤小数（`2.639`）。
    """
    if end >= len(text) or text[end] not in _ORDINAL_MARKS:
        return False
    after = text[end + 1:end + 2]
    if after.isdigit():
        return False
    prefix = text[:start]
    stripped = prefix.strip()
    return stripped == "" or prefix.rstrip().endswith(_BLOCK_PREFIX_CHARS)


def extract_tokens(text: str, ctx: ExtractionContext) -> Tuple[List[Token], List[Unparsed]]:
    """抽取并归一化一行文本；返回 (tokens, unparsed)。文本必须是 str。"""
    if not isinstance(text, str):
        raise TypeError("待抽取文本必须是字符串")

    normalized = normalize_text(text)
    consumed: List[Tuple[int, int]] = []
    tokens: List[Token] = []

    def is_free(start: int, end: int) -> bool:
        return all(start >= stop or end <= begin for begin, stop in consumed)

    # 1) 实体（显式配置，优先级最高）
    if "entity" in ctx.categories:
        for name, pattern in ctx.entity_patterns:
            for match in pattern.finditer(normalized):
                span = match.span()
                if not is_free(*span):
                    continue
                raw = normalized[span[0]:span[1]]
                value = raw if ctx.cfg.entity_case_sensitive else raw.casefold()
                value = unicodedata.normalize("NFC", value)
                tokens.append(Token("entity", value, span, None, name, _digest(value), len(raw)))
                consumed.append(span)

    # 2) 日期（区间先于单日：`2015年7月13日至16日` 整体算两个日期，不留给裸数字）
    if "date" in ctx.categories:
        for kind, pattern in ctx.date_range_patterns:
            for match in pattern.finditer(normalized):
                span = match.span()
                if not is_free(*span):
                    continue
                values = _resolve_date_range(kind, match)
                if not values:
                    continue
                for value in values:
                    tokens.append(Token("date", value, span, None, kind))
                consumed.append(span)
        for kind, pattern in ctx.date_patterns:
            for match in pattern.finditer(normalized):
                span = match.span()
                if not is_free(*span):
                    continue
                value = _resolve_date(kind, match)
                if value is None:
                    continue
                tokens.append(Token("date", value, span, None, kind))
                consumed.append(span)

    # 3) 时间（AM/PM 必须先于 HH:MM，否则 3:00 PM 会被截成 3:00）
    if "time" in ctx.categories:
        for kind, pattern in ctx.time_patterns:
            for match in pattern.finditer(normalized):
                span = match.span()
                if not is_free(*span):
                    continue
                value = _resolve_time(kind, match)
                if value is None:
                    continue
                tokens.append(Token("time", value, span, None, kind))
                consumed.append(span)

    # 4) 货币
    if "currency" in ctx.categories:
        for kind, pattern in ctx.currency_patterns:
            for match in pattern.finditer(normalized):
                span = match.span()
                if not is_free(*span):
                    continue
                resolved = _resolve_currency(kind, match, ctx.currency_aliases)
                if resolved is None:
                    continue
                code, amount = resolved
                tokens.append(Token("currency", f"{code}:{format_number(amount)}", span, amount, code))
                consumed.append(span)

    # 5) 百分比（统一归一到比率）
    if "percentage" in ctx.categories:
        for kind, pattern in build_percentage_regex():
            for match in pattern.finditer(normalized):
                span = match.span()
                if not is_free(*span):
                    continue
                ratio = _resolve_percentage(kind, match)
                if ratio is None:
                    continue
                tokens.append(Token("percentage", format_number(ratio), span, ratio, kind))
                consumed.append(span)

    # 6) 单位（白名单内换算到基准单位；白名单外不猜测）
    if "unit" in ctx.categories and ctx.unit_regex is not None:
        for match in ctx.unit_regex.finditer(normalized):
            span = match.span()
            if not is_free(*span):
                continue
            if _is_range_tail(normalized, span[0]):
                # 区间后半截（`2015-2017年`、`15-19 years`）不是独立数量：
                # 不抽单位、不消费跨度，让两侧的数字各自按裸数字对齐。
                continue
            resolved = resolve_alias(match.group(3), ctx.unit_aliases)
            if resolved is None:
                continue
            dimension, code = resolved
            # 组：1=数量 2=量级 3=单位（units.build_unit_regex 的组序）
            amount = _apply_magnitude(_parse_number(match.group(1)), match.group(2))
            if amount is None:
                continue
            if _is_calendar_expression(match.group(3), amount):
                # 日历部件不算时长。月份/季度可能被译成 March / Q3 等名称，
                # 数字消失是正常转写，因此消费跨度、不留下裸数字；
                # 四位年份（2024 年）两侧通常都保留数字，保留裸数字让 2024 正常匹配。
                if (match.group(3) or "").strip().lower() != "年":
                    consumed.append(span)
                continue
            convert = ctx.cfg.unit_conversion and dimension not in ctx.cfg.disabled_dimensions
            base = to_base(dimension, code, amount) if convert else None
            if base is None:
                tokens.append(Token("unit", f"{dimension}:{code}:{format_number(amount)}",
                                    span, amount, dimension))
            else:
                tokens.append(Token("unit", f"{dimension}:{format_number(base)}", span, base, dimension))
            consumed.append(span)

    # 7) 畸形片段：先保留跨度，再抽数字
    unparsed: List[Unparsed] = []
    for pattern, category, reason in _LOOSE_SHAPES:
        for match in pattern.finditer(normalized):
            span = match.span()
            if not is_free(*span):
                continue
            raw = normalized[span[0]:span[1]]
            unparsed.append(Unparsed(category, span, reason, _digest(raw), len(raw)))
            consumed.append(span)
    unparsed.sort(key=lambda item: item.span[0])

    # 8) 裸数字
    if "number" in ctx.categories:
        for match in _NUMBER.finditer(normalized):
            span = match.span()
            if not is_free(*span):
                continue
            if _is_block_ordinal(normalized, span[0], span[1]):
                consumed.append(span)
                continue
            if _is_trailing_footnote(normalized, span[0], span[1]):
                consumed.append(span)
                continue
            value = _parse_number(match.group(0))
            if value is None:
                continue
            end = span[1]
            magnitude = _MAGNITUDE_RE.match(normalized, end)
            if magnitude:
                value *= _MAGNITUDE.get(magnitude.group(1).lower(), 1.0)
                end = magnitude.end()
            tokens.append(Token("number", format_number(value), (span[0], end), value))
            consumed.append((span[0], end))

    tokens.sort(key=lambda item: item.span[0])
    return tokens, unparsed
