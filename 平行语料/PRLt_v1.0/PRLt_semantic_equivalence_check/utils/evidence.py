"""规则证据与问题标签：只做真实可复算的观测，绝不拼成模型分数。

证据类型：语言方向（langdetect 优先，不可用时字符规则降级且 confidence=null）、
长度比例、数字一致性。标签必须携带证据（technical_design §3）。
"""
import re
from collections import Counter
from decimal import Decimal, InvalidOperation
from typing import Any, Dict, List, Optional, Tuple

NUMBER_RE = re.compile(r"\d+(?:[.,]\d+)*")
CJK_RE = re.compile(r"[\u3400-\u4dbf\u4e00-\u9fff\uf900-\ufaff]")
LATIN_WORD_RE = re.compile(r"[A-Za-z]+")

# 数字归一化（2026-11-03）：同一份文档里「940 000」「940,000」「９４０，０００」都是 940000，
# 不归一就会把同一个数当成"源有目标无"，把正确译文判成数字不一致。
_FULLWIDTH_DIGITS = str.maketrans("０１２３４５６７８９", "0123456789")
# 千分位分隔符：半角/全角逗号、空格、不断行空格、撇号（用 chr() 构造，避免源码里出现不可见字符）
_GROUP_SEP_CHARS = "".join(chr(c) for c in (0x2C, 0x20, 0xA0, 0x2009, 0x202F, 0x2019, 0xFF0C))   # , space NBSP thin-space narrow-NBSP ’ ，
_GROUPED_NUMBER_RE = re.compile(r"\d{1,3}(?:[" + _GROUP_SEP_CHARS + r"]\d{3})+(?:[.,]\d+)?")
_GROUP_SEP_RE = re.compile(r"[" + _GROUP_SEP_CHARS + r"]")
# 先试千分位写法（分隔符后必须正好 3 位），再退回普通数字；小数「3.5」「3,5」原样保留
_NUMBER_TOKEN_RE = re.compile("(?:" + _GROUPED_NUMBER_RE.pattern + r")|(?:\d+(?:[.,]\d+)?)")

# 中文数量级（2026-11-03）：译文会把「2.639亿」写成「263.9 million」、「320亿」写成「32 billion」，
# 按字面比较必然判成"源有目标无"。这里只折叠中文万/亿这一档大数量级；
# 千、百不折——「3千米」「5百万」里的千/百是单位前缀或复合量词的一部分，折了反而出错。
_ZH_SCALE_RE = re.compile(r"\s*(万亿|千亿|百亿|十亿|百万|千万|万|亿|億)")
_ZH_SCALE_VALUE = {
    "万亿": 10 ** 12, "千亿": 10 ** 11, "百亿": 10 ** 10, "十亿": 10 ** 9,
    "百万": 10 ** 6, "千万": 10 ** 7, "万": 10 ** 4, "亿": 10 ** 8, "億": 10 ** 8,
}
_EN_SCALE_RE = re.compile(r"\s*(thousand|million|billion|trillion)\b", re.IGNORECASE)
_EN_SCALE_VALUE = {"thousand": 10 ** 3, "million": 10 ** 6,
                   "billion": 10 ** 9, "trillion": 10 ** 12}
# 段落编号：「115. 各项举措」「9. 缔约国承认」这类句首序号在译文里通常不带，
# 拿它当"缺失数字"硬拦是假阳性。只认 1–3 位，避免把 4 位年份（1993. / 2018.）当序号排掉。
_ORDINAL_RE = re.compile(r"(?:^|(?<=[\s(（]))(\d{1,3})\s*[.．、)）]")

_ZH_ALIASES = {"zh", "zh-cn", "zh-tw", "zh-hans", "zh-hant", "zh-chs", "zh-cht", "cmn"}
_EN_ALIASES = {"en", "en-us", "en-gb", "eng"}


def extract_numbers(text: str) -> List[str]:
    """抽数字并归一化：全角转半角、千分位分隔符去掉。只做字符串归一，不做数值换算。"""
    return [display for display, _ in canonical_numbers(text)]


def digit_count(value: str) -> int:
    """数字串的有效位数（小数点和小数部分也计入，只用于"够不够大"的判断）。"""
    return sum(1 for ch in str(value) if ch.isdigit())


def _decimal_value(token: str) -> Optional[Decimal]:
    """把数字串读成 Decimal；「1,5」这类欧洲式小数逗号按 1.5 处理，读不出来就返回 None。"""
    try:
        return Decimal(token.replace(",", "."))
    except (InvalidOperation, ValueError):
        return None


def _format_decimal(value: Decimal) -> str:
    """整数结果写成整数字符串（263900000.0 → 263900000），避免小数尾零造成不相等。"""
    if value == value.to_integral_value():
        return str(int(value))
    return format(value.normalize(), "f")


def _scale_after(text: str, position: int) -> Optional[Decimal]:
    """数字紧跟的中文/英文数量级；没有匹配返回 None。"""
    zh = _ZH_SCALE_RE.match(text, position)
    if zh:
        return Decimal(_ZH_SCALE_VALUE[zh.group(1)])
    en = _EN_SCALE_RE.match(text, position)
    if en:
        return Decimal(_EN_SCALE_VALUE[en.group(1).lower()])
    return None


def canonical_numbers(text: str) -> List[Tuple[str, str]]:
    """返回 (原文写法, 比较用键)。

    比较用键做了两件事：千分位、全角、空格写法归一（940 000 / 940,000 → 940000）；
    带数量级的数字折算成同量级的整数（2.639亿 与 263.9 million 都是 263900000）。
    这样「同一个数换了写法」不会被当成数字不一致，真正改掉的数字仍然对不上。
    """
    normalized = (text or "").translate(_FULLWIDTH_DIGITS)
    values: List[Tuple[str, str]] = []
    for match in _NUMBER_TOKEN_RE.finditer(normalized):
        token = match.group()
        if _GROUPED_NUMBER_RE.fullmatch(token):
            token = _GROUP_SEP_RE.sub("", token)
        number = _decimal_value(token)
        if number is None:
            values.append((token, token))
            continue
        scale = _scale_after(normalized, match.end())
        values.append((token, _format_decimal(number * scale) if scale else token))
    return values


# 中文日期数字："9月份"、"2014年"、"14日"、"5号"。只用于把日期数字从替换判定里排除。
_CALENDAR_SUFFIX_RE = re.compile(r"(?<![\d.])(\d{1,4}(?:[.,]\d+)?)\s*[月日号年]")

# 逗号+空格的双重写法（"$164, 700"、"$1, 958.6 million"）：英文里不常见，但真实语料里有。
# 不能直接当成千分位——"251, 252, 254" 也是逗号+空格，那是列举不是数字。
# 处理办法：第一遍按严格口径比；只有仍然"缺"的数，才在把逗号+空格合并后的第二遍再比一次。
# 第二遍只能撤销拦截、不能新增，合并过头也只会漏拦，不会误拦。
_COMMA_SPACE_RE = re.compile(r"(?<=\d)[,，]\s+(?=\d{3})")
_TERMINAL_CHARS = tuple(".。!！?？\"”』」）)")


def _join_comma_space(text: str) -> str:
    return _COMMA_SPACE_RE.sub("", (text or "").translate(_FULLWIDTH_DIGITS))


def looks_complete(text: str) -> bool:
    """判定单元是否以句末标点收尾。语料里跨子句切分的译文常在半截处断开
    （"reduced to 4," / "on 21"），这种片段上的"数字缺失"是切分造成的，不是漏译。"""
    stripped = (text or "").strip()
    return bool(stripped) and stripped.endswith(_TERMINAL_CHARS)


def calendar_displays(text: str) -> set:
    """中文里紧跟在「月/日/号/年」前的数字（"9月份"、"2014年"）。

    日期在译文里常写成月份单词（9月 → "September"），这种"源文有数字、译文没有"
    不是漏译；做替换判定时把这类数字从源文侧排除，否则会把正常的日期写法差异
    当成"源文多一个数字"。
    """
    normalized = (text or "").translate(_FULLWIDTH_DIGITS)
    return {match.group(1) for match in _CALENDAR_SUFFIX_RE.finditer(normalized)}


def paragraph_ordinals(text: str) -> set:
    """句首/空白后的段落编号（115. / 172、），译文不带编号属于排版差异，不算漏译。"""
    normalized = (text or "").translate(_FULLWIDTH_DIGITS)
    return {match.group(1) for match in _ORDINAL_RE.finditer(normalized)}


def _empty_guard_result() -> Dict[str, Any]:
    return {"missing": [], "count": [], "swap": [], "rules": []}


def guard_number_mismatch(source: str, target: str, min_digits: int) -> Dict[str, Any]:
    """数字守卫的完整口径：缺失、计数、序列三条规则，命中任一即拦截。

    - missing（原有）：源文有、译文没有，且位数 >= min_digits（含 19xx/20xx 年份）；
    - count（P0-B）：位数 >= min_digits 的数字按出现次数比，源文出现 n 次、译文少于 n 次。
      "存不存在"这一维看不出重复：源文两处都写 2015、译文只留一处，是真实的漏译/压缩；
    - swap（P0-A）：源文与译文的数字序列个数相同、恰好一个位置不同，该位置就是被改写/替换。
      "截至 2 月" → "as of 9 February" 这类 1–2 位数字替换位数不够，missing 规则拦不住。

    三条规则都只看"数字序列的形状"，不做数值推论：个数不同（译文合并/拆分数字、
    日期换序写成 16 April 2015）一律不判，宁漏勿错。
    返回 {"missing", "count", "swap", "rules"}；swap 是 [{"source", "target"}] 对。
    """
    if not looks_complete(target):
        return _empty_guard_result()
    result = _guard_pass(source, target, int(min_digits))
    if result["rules"]:
        # 第二遍：把「164, 700」这类逗号+空格的写法合并后再比一次，只用于撤销第一遍的拦截
        result = _guard_pass(_join_comma_space(source), _join_comma_space(target), int(min_digits))
    return result


def guard_missing_numbers(source: str, target: str, min_digits: int) -> List[str]:
    """缺失口径（missing 规则）：源文有、译文缺，且位数 >= min_digits 的数字。

    保留这个入口是为了让单元测试和外部调用能单独验证"缺失"这一条，
    口径与 guard_number_mismatch 的 missing 字段完全一致。
    """
    return guard_number_mismatch(source, target, min_digits)["missing"]


def _guard_pass(source: str, target: str, min_digits: int) -> Dict[str, Any]:
    source_pairs = canonical_numbers(source)
    target_pairs = canonical_numbers(target)
    ordinals = paragraph_ordinals(source)
    missing = _guard_diff(source_pairs, target_pairs, ordinals, min_digits)
    count = _count_shortfall(source_pairs, target_pairs, ordinals, min_digits)
    swap = _sequence_swap(source_pairs, target_pairs, ordinals, calendar_displays(source))
    rules = [name for name, hit in (("missing", missing), ("count", count), ("swap", swap)) if hit]
    return {"missing": missing, "count": count, "swap": swap, "rules": rules}


def _guard_diff(source_pairs: List[Tuple[str, str]], target_pairs: List[Tuple[str, str]],
                ordinals: set, threshold: int) -> List[str]:
    target_keys = {key for _, key in target_pairs}
    missing: List[str] = []
    seen = set()
    for display, key in source_pairs:
        if display in ordinals or key in target_keys or key in seen:
            continue
        if digit_count(key) < threshold:
            continue
        seen.add(key)
        missing.append(display)
    return missing


def _count_shortfall(source_pairs: List[Tuple[str, str]], target_pairs: List[Tuple[str, str]],
                     ordinals: set, threshold: int) -> List[str]:
    """按出现次数比：源文出现次数多于译文的数字（只比位数 >= threshold 的）。

    与 _guard_diff 的唯一区别是"重复出现"这一维。仍排除段落编号与小数字，
    返回原文写法并按源文出现顺序去重。
    """
    target_counts = Counter(key for _, key in target_pairs)
    source_counts: Counter = Counter()
    displays: Dict[str, str] = {}
    for display, key in source_pairs:
        if display in ordinals or digit_count(key) < threshold:
            continue
        source_counts[key] += 1
        displays.setdefault(key, display)
    short = source_counts - target_counts
    return [displays[key] for key in dict.fromkeys(k for _, k in source_pairs if k in short)]


def _sequence_swap(source_pairs: List[Tuple[str, str]],
                   target_pairs: List[Tuple[str, str]],
                   ordinals: set = frozenset(),
                   calendar: set = frozenset()) -> List[Dict[str, str]]:
    """序列规则：数字被改写/替换时命中，两个口径按顺序尝试。

    1. 两边数字个数相同、恰好一个位置不同 => 该位置的数字被改写/替换。
       个数不同一律不判：译文合并/拆分数字、日期换序（"2015 年 4 月 16 日" →
       "16 April 2015"）都会改变个数，属于正常转写。
    2. [2026-11-05] 个数不同时，若恰好「源文多出一个 1–2 位数字、译文多出一个
       1–2 位数字」，同样按替换处理：这些是改数字但没有改变个数结构的用例
       （现年 26 岁 → "33"、10 月 14 日 → "21"），缺失规则因位数门槛看不见，
       序列规则因个数不同也看不见。
       实测自然语料 1546 条句对里误报 3 条（0.19%），远低于缺失规则放宽位数门槛的代价
       （1–2 位小数字缺失在对照句对里占 35%）。
    """
    if not source_pairs or not target_pairs:
        return []
    if len(source_pairs) == len(target_pairs):
        diffs = [(source_display, target_display)
                 for (source_display, source_key), (target_display, target_key)
                 in zip(source_pairs, target_pairs)
                 if source_key != target_key]
        if len(diffs) == 1:
            return [{"source": diffs[0][0], "target": diffs[0][1]}]
    return _substitution_pair(source_pairs, target_pairs, ordinals, calendar)


def _substitution_pair(source_pairs: List[Tuple[str, str]],
                       target_pairs: List[Tuple[str, str]],
                       ordinals: set, calendar: set) -> List[Dict[str, str]]:
    """「一路多一个、另一路也多一个」的 1–2 位数字替换（口径 2，见 _sequence_swap）。"""
    source_counts = Counter(key for _, key in source_pairs)
    target_counts = Counter(key for _, key in target_pairs)
    source_only: List[Tuple[str, str]] = []
    for display, key in source_pairs:
        if display in ordinals or display in calendar or digit_count(key) > 2:
            continue
        if source_counts[key] > target_counts.get(key, 0) and key not in [k for _, k in source_only]:
            source_only.append((display, key))
    target_only: List[Tuple[str, str]] = []
    for display, key in target_pairs:
        if digit_count(key) > 2:
            continue
        if target_counts[key] > source_counts.get(key, 0) and key not in [k for _, k in target_only]:
            target_only.append((display, key))
    if len(source_only) != 1 or len(target_only) != 1:
        return []
    return [{"source": source_only[0][0], "target": target_only[0][0]}]


# --------------------------------------------------------------------------- #
# 不完整译文守卫（2026-11-05）
# --------------------------------------------------------------------------- #
# 嵌入分对「译文只译了半句」同样不敏感：Fullflow-Effect-Bench-v1 的 clause_drop
# 15 条里 14 条分数高于阈值（父句级 0.65–0.86），因为前半句译得对，相似度主要
# 由前半句撑着。数字守卫里 looks_complete() 只用来**撤销**数字拦截，不做完整性判定。
#
# 口径：原文以句末标点收尾、译文却没有收尾 → 译文中途截断，判 incomplete_translation。
# 依据：
#   - 自然语料 1554 条入口句对中，原文完整而译文没有收尾的只有 23 条（1.7%），
#     且这 23 条本身多是错配片段（译文只有一个词或半句），拦下来是对的；
#   - 本数据集 clause_drop 标签 15 条里命中 13 条；其余 2 条译文自带分号/片段，属切分差异。
# 译文的合法收尾不止 .!?：联合国文件大量以分号、冒号、右括号、脚注编号（…2014.42）结尾，
# 这些都不判，否则会在真实语料上大面积误拦。
_ZH_SENTENCE_END = tuple("。！？")
_TARGET_TERMINAL = tuple('.。!！?？;；:："”\'’』」）)*…—–')
_FOOTNOTE_TAIL_RE = re.compile(r"(?<=[.。])\d{1,3}$")
# [2026-11-05] 源文以脚注标记收尾（"帕帕·路易斯·法尔先生(塞内加尔)*"）时，
# 末尾的 * / † / ‡ 说明这句本来就是名录条目，不是"没写完"，按完整句处理。
# 自然语料 1554 句对 + 543 个父句单元里这类收尾 0 条，不引入误报。
_SOURCE_FOOTNOTE_TAIL_RE = re.compile(r"[*†‡]+\s*$")


# [2026-11-05] 悬空虚词：正确收尾的英文句子几乎不会以这些词结尾。
# 只收「出现在句尾就不成立」的高频虚词；in / on / at / over / about / by / from 这类
# 可以合法做副词或名词短语收尾（"falling over"、"in 2016"），实测加进来自然语料误报
# 从 1/543 涨到 12/543，所以不收。
_DANGLING_TAIL_TOKENS = frozenset({
    "and", "or", "nor", "of", "to", "the", "a", "an", "with", "for", "but",
    "that", "which", "as", "than", "into", "upon", "via",
})
_TAIL_JUNK = "\"'”’）)」』].,;:!?… *"


# 原文停在「这里就是本条记录的边界」的字符上：句末标点或右闭合符号。
# 逗号/顿号/冒号不收——它们表示句子还要往下接，下一条记录才是后半句。
_SOURCE_CLOSED_TAIL = frozenset("。！？；…!?.;)]}】」』”’\"'")


def source_closed(source: str) -> bool:
    """原文是否停在句末标点/右闭合符号上（而不是断在汉字、字母或逗号后）。"""
    text = (source or "").strip()
    return bool(text) and text[-1] in _SOURCE_CLOSED_TAIL


def dangling_final_token(target: str) -> Optional[str]:
    """译文最后一个实词若是悬空虚词，返回该词；否则返回 None。"""
    text = (target or "").rstrip(_TAIL_JUNK).rstrip()
    if not text:
        return None
    token = text.split()[-1].strip("\"'“”‘’()（）[]【】").lower()
    return token if token in _DANGLING_TAIL_TOKENS else None


def target_complete(text: str) -> bool:
    """译文是否以合法收尾符号结束（含分号、冒号、引号、右括号与脚注编号）。"""
    stripped = (text or "").strip()
    if not stripped:
        return False
    stripped = _FOOTNOTE_TAIL_RE.sub("", stripped).rstrip(" \t]}")
    return stripped.endswith(_TARGET_TERMINAL)


def guard_incomplete_target(source: str, target: str,
                            min_source_chars: int = 10,
                            check_dangling_tail: bool = True) -> Dict[str, Any]:
    """不完整译文守卫：原文是完整句、译文却在中途断开 → 漏译，应拦截。

    ``min_source_chars``：原文去空白后短于该长度不判（标题、标签类短单位本来就常不带句号）。
    ``check_dangling_tail``：译文以悬空虚词收尾时也判不完整（见 _DANGLING_TAIL_TOKENS）。
    返回 ``{"rules", "source_complete", "target_complete", "source_chars", "dangling_tail"}``，
    rules 为 ``["incomplete_translation"]`` 即命中。
    """
    source = source or ""
    target = target or ""
    source_chars = len("".join(source.split()))
    source_body = _SOURCE_FOOTNOTE_TAIL_RE.sub("", source.strip())
    # 悬空虚词只对「原文自己也停在边界上」的单元计算，见下面的 source_closed 说明
    dangling = (dangling_final_token(target)
                if (check_dangling_tail and source_closed(source)) else None)
    result: Dict[str, Any] = {
        "rules": [],
        # 脚注标记收尾（"…(塞内加尔)*"）本身就算完整，见 _SOURCE_FOOTNOTE_TAIL_RE
        "source_complete": source.strip().endswith(_ZH_SENTENCE_END)
        or (source_body != source.strip() and bool(source_body)),
        "target_complete": target_complete(target),
        "source_chars": source_chars,
        "dangling_tail": dangling,
    }
    if source_chars < int(min_source_chars):
        return result
    # [2026-11-05] 悬空虚词收尾：译文以 and / of / the 这类虚词断在句尾，说明译文没写完
    # （defect-14-026:p9「From Western European and」的原文以「[待补]」收尾，不是 。！？，
    # 原来的「原文完整」前提不成立，规则看不见）。自然语料 543 + 399 个判定单元误报 1 / 0 条。
    # 前提：原文自己也停在标点/右括号上（source_closed）。原文断在汉字中间时说明这条记录
    # 只是句子被切开的一段，译文跟着断在半句是正常的（「…职位由新任部长接」/「…resigned and」）。
    if dangling:
        result["rules"] = ["incomplete_translation"]
        return result
    if not result["source_complete"] or result["target_complete"]:
        return result
    result["rules"] = ["incomplete_translation"]
    return result


# --------------------------------------------------------------------------- #
# 否定守卫（2026-11-04）
# --------------------------------------------------------------------------- #
# 嵌入分对否定翻转几乎不敏感：defect-14-025 的三段否定翻转得分 0.7194 / 0.7388 / 0.7854，
# 全部高于阈值 0.65，其中一段还拿到了 S 级。数字有 number_guard 兜底，否定此前没有任何守卫。
#
# 只判一个方向：**译文出现否定、原文没有否定**。
# 反方向（原文有否定、译文没有）在自然语料上误报 17.5%——中文否定经常被译成肯定式
# （「没有…之前」→「before…」、「不得不」→「had to」），判了必爆，所以不判。
# 实测（2026-11-04）：876 个父句判定单元误报 6 条（0.68%）；构造负例召回 12/18。
#
# 误报主要来自固定搭配，先屏蔽再数：no doubt / not only / nothing but / whether or not /
# 「No. 5」编号。屏蔽只会让守卫更保守（少拦，不多拦）。

_ZH_NEGATIVE_RE = re.compile("[不没未无非勿别]")
# 含否定字但本身不是否定的词：先屏蔽再数
# [2026-11-05] 补「不同/未成年人/不少/不久/不丹」：这几类词让「原文没有否定」被误判成
# 「原文也有否定」，守卫直接放弃判定。Fullflow-Effect-Bench-v1 里有 2 条否定翻转
# （defect-14-029:p1「不同方法」、defect-14-030:p3「未成年人」）就卡在这里。
# [2026-11-05] 移除「并非」：它是真否定（并非如此 = it is not the case），屏蔽它会让
# 「并非如此 → It is not true that …」这种正确译文被第 1 条口径误拦；
# 自然语料 1554 句对实测误报 15 → 13。
_ZH_NEG_FALSE_FRIENDS = (
    "非常", "非政府", "非营利", "非正式", "非法", "非洲", "不仅", "不但",
    "不只", "不得不", "不错", "不论", "无论", "不管", "不断", "未来",
    "不同", "未成年人", "不少", "不久", "不丹", "不愧",
)
_ZH_NEG_FALSE_FRIENDS_RE = re.compile("|".join(_ZH_NEG_FALSE_FRIENDS))

_EN_NEGATIVE_RE = re.compile(
    r"\b(?:not|no|never|none|nothing|nobody|no-one|neither|nor|without|cannot|"
    r"can[’']t|won[’']t|isn[’']t|aren[’']t|wasn[’']t|weren[’']t|"
    r"don[’']t|doesn[’']t|didn[’']t|wouldn[’']t|shouldn[’']t|couldn[’']t|"
    r"mustn[’']t|needn[’']t|hasn[’']t|haven[’']t|hadn[’']t)\b",
    re.IGNORECASE)
_EN_NEG_FALSE_FRIENDS_RE = re.compile(
    r"\b(?:not\s+only|not\s+just|not\s+merely|not\s+least|not\s+to\s+mention|"
    r"not\s+the\s+only|no\s+doubt|no\s+wonder|nothing\s+but|whether\s+or\s+not|"
    r"if\s+not)\b",
    re.IGNORECASE)
# 「No. 5」「no. 12」是编号写法，不是否定
_EN_ABBREV_NO_RE = re.compile(r"\bno\.\s*(?=\d)", re.IGNORECASE)
# [2026-11-05] 否定框架句：「It is not true that …」把整句语义翻过来，但两边否定子句数
# 可能相同（defect-14-030:p7：原文「没有…威胁」1 个子句，译文「not true … no threat」
# 也只剩 1 个子句，子句口径看不见）。只认这一组成句的框架，逐词否定仍走下面的口径。
_EN_NEGATION_FRAME_RE = re.compile(
    r"\b(?:it|that|this)\s+(?:is|was)\s+not\s+(?:true|the\s+case|correct|so|accurate)\b"
    r"|\b(?:it|that|this)\s+(?:is|was)\s+(?:false|untrue)\b", re.IGNORECASE)
_ZH_NEGATION_FRAME_RE = re.compile(r"不是真的|并非如此|并非事实|不属实|不实|不正确|不对|绝非|并非")


def chinese_negations(text: str) -> List[str]:
    """中文否定词；先屏蔽「非常/不仅/无论」这类含否定字但不表否定的词。"""
    return _ZH_NEGATIVE_RE.findall(_ZH_NEG_FALSE_FRIENDS_RE.sub(" ", text or ""))


def english_negations(text: str) -> List[str]:
    """英文否定词；整词匹配，先屏蔽固定搭配与「No. 5」编号写法。"""
    masked = _EN_ABBREV_NO_RE.sub(" ", text or "")
    masked = _EN_NEG_FALSE_FRIENDS_RE.sub(" ", masked)
    return [match.group(0) for match in _EN_NEGATIVE_RE.finditer(masked)]


# 子句切分（2026-11-05）：整句比否定数量不可靠，改比「带否定的子句数」
_ZH_CLAUSE_SPLIT_RE = re.compile(r"[，,；;：:。！？!?]")
_EN_CLAUSE_SPLIT_RE = re.compile(
    r"[,;:.]|\s+(?:and|but|while|although|though|because|which|that)\s+", re.IGNORECASE)


def _negated_clauses(text: str, splitter: re.Pattern, matcher) -> int:
    """切子句后统计「含否定的子句」个数（空子句不计）。"""
    return sum(1 for clause in splitter.split(text or "")
               if clause.strip() and matcher(clause))


def negated_clause_counts(source: str, target: str) -> Tuple[int, int]:
    """原文/译文各自含否定的子句数。"""
    return (_negated_clauses(source, _ZH_CLAUSE_SPLIT_RE, chinese_negations),
            _negated_clauses(target, _EN_CLAUSE_SPLIT_RE, english_negations))


def guard_negation_mismatch(source: str, target: str,
                            min_chars: int = 0) -> Dict[str, Any]:
    """否定守卫：译文有否定、原文没有 → 语义翻转，应拦截。

    只判这一个方向（理由见本节注释）。``min_chars`` > 0 时，译文（去空白后）短于该
    字符数不判——「Yes. / No.」这类短答句两边都合法，按否定词判会误伤。

    两个口径：
      1. 原文整句没有否定、译文有 → ``negation_added``（原有口径）；
      2. 两边都有否定时不再直接放弃（[2026-11-05]）：改比「含否定的子句数」，
         译文比原文多出否定子句、且译文否定子句 >= 2 → ``negation_clause_added``。
         依据 Fullflow-Effect-Bench-v1：defect-14-025:p5「没有受伤」→「is not aged 64,
         was not injured」、defect-14-027:p5「不会导致」↔「It is not true that … did not
         result」、defect-14-030:p7「没有…威胁」↔「It is not true that … no tsunami threat」
         都是「原文 1 个否定子句、译文 2 个」，旧口径直接返回导致漏检。
         自然语料 150 条带英文否定的句对里该口径误报 5 条（3.3%，占全部句对 0.3%）。
      3. 否定框架句（[2026-11-05] 补）：译文出现「It is not true that …」「It is false that …」
         而原文没有「不是真的/并非/不实/绝非」这类对应说法 → ``negation_frame_added``。
         这类插入把整句语义翻过来，但否定会挤进同一个子句，第 2 条口径看不见
         （defect-14-030:p7）。自然语料 1554 句对 / 543 个父句单元误报 0 条。

    返回 ``{"source_negations", "target_negations", "source_negated_clauses",
    "target_negated_clauses", "rules"}``。原文有否定、译文没有的情况只记录观测，不拦截。
    """
    result: Dict[str, Any] = {"source_negations": [], "target_negations": [],
                              "source_negated_clauses": 0, "target_negated_clauses": 0,
                              "rules": []}
    target_negations = english_negations(target)
    if not target_negations:
        return result
    result["target_negations"] = target_negations
    result["source_negations"] = chinese_negations(source)
    if min_chars and len("".join((target or "").split())) < int(min_chars):
        return result
    source_clauses, target_clauses = negated_clause_counts(source, target)
    result["source_negated_clauses"] = source_clauses
    result["target_negated_clauses"] = target_clauses
    # 否定框架句：译文多了「It is not true that …」而原文没有对应说法 → 语义翻转。
    # 先于子句口径判定，因为框架句会把译文的否定摊薄到同一个子句里，子句数看不出来。
    if _EN_NEGATION_FRAME_RE.search(target or "") and not _ZH_NEGATION_FRAME_RE.search(source or ""):
        result["rules"] = ["negation_frame_added"]
        return result
    if not result["source_negations"]:
        result["rules"] = ["negation_added"]
        return result
    # 两边都有否定：只有「译文多出一个否定子句」才判，按整句数量比会大面积误报
    if target_clauses > source_clauses and target_clauses >= 2:
        result["rules"] = ["negation_clause_added"]
    return result


# --------------------------------------------------------------------------- #
# 单位 / 币种守卫（2026-11-04）
# --------------------------------------------------------------------------- #
# 嵌入分对「3 年 → 3 months」「5000 美元 → 5000 euros」这类同数字换单位的错误
# 几乎不敏感（实测 0.7674 / 0.868，均高于阈值）。数字守卫只比数字，也拦不住。
# 这里补一条纯规则守卫：只比较「数字相同」的成对数量，单位/币种不同即拦截；
# 数字本身不同仍交给 number_guard，避免重复报同一处错误。
#
# 注意：本表是算子14 自己的轻量副本，不 import 算子16（两个算子需可独立部署）。
_UNIT_FACTORS = {
    "duration": {
        "s": 1.0, "ms": 0.001, "min": 60.0, "h": 3600.0, "d": 86400.0,
        "wk": 604800.0, "mo": 2628000.0, "qtr": 7884000.0, "yr": 31536000.0,
    },
    "length": {
        "m": 1.0, "km": 1000.0, "cm": 0.01, "mm": 0.001, "mi": 1609.344,
        "ft": 0.3048, "yd": 0.9144, "nmi": 1852.0,
    },
    "mass": {
        "kg": 1.0, "g": 0.001, "mg": 1e-06, "t": 1000.0,
        "lb": 0.45359237, "oz": 0.028349523125,
    },
    "area": {"m2": 1.0, "km2": 1e06, "cm2": 1e-04, "ha": 10000.0, "mu": 2000.0 / 3.0},
    "volume": {"l": 1.0, "ml": 0.001, "m3": 1000.0, "gal": 3.785411784},
    "temperature": {"c": 1.0, "f": 1.0, "k": 1.0},
}

_UNIT_ALIASES: Dict[str, str] = {}


def _register_unit(code: str, aliases) -> None:
    _UNIT_ALIASES[code.lower()] = code
    for alias in aliases:
        _UNIT_ALIASES[alias.lower()] = code


_register_unit("m", ["meter", "meters", "metre", "metres", "米"])
_register_unit("km", ["kilometer", "kilometers", "kilometre", "kilometres", "千米", "公里"])
_register_unit("cm", ["centimeter", "centimeters", "厘米"])
_register_unit("mm", ["millimeter", "millimeters", "毫米"])
_register_unit("mi", ["mile", "miles", "英里"])
_register_unit("ft", ["foot", "feet", "英尺"])
_register_unit("yd", ["yard", "yards", "码"])
_register_unit("nmi", ["nauticalmile", "nauticalmiles", "海里"])
_register_unit("kg", ["kilogram", "kilograms", "千克", "公斤"])
_register_unit("g", ["gram", "grams", "克"])
_register_unit("mg", ["milligram", "milligrams", "毫克"])
_register_unit("t", ["tonne", "tonnes", "ton", "tons", "吨"])
_register_unit("lb", ["pound", "pounds", "磅"])
_register_unit("oz", ["ounce", "ounces", "盎司"])
_register_unit("m2", ["sqm", "squaremeter", "squaremeters", "平方米", "平米"])
_register_unit("km2", ["sqkm", "squarekilometer", "squarekilometers", "平方公里"])
_register_unit("ha", ["hectare", "hectares", "公顷"])
_register_unit("mu", ["亩"])
_register_unit("l", ["liter", "liters", "litre", "litres", "升"])
_register_unit("ml", ["milliliter", "milliliters", "毫升"])
_register_unit("m3", ["cubicmeter", "cubicmeters", "立方米"])
_register_unit("gal", ["gallon", "gallons", "加仑"])
_register_unit("s", ["sec", "secs", "second", "seconds", "秒"])
_register_unit("ms", ["millisecond", "milliseconds", "毫秒"])
_register_unit("min", ["minute", "minutes", "分钟"])
_register_unit("h", ["hr", "hrs", "hour", "hours", "小时"])
_register_unit("d", ["day", "days", "天"])
_register_unit("wk", ["week", "weeks", "周", "星期"])
_register_unit("mo", ["month", "months", "个月", "月"])
_register_unit("qtr", ["quarter", "quarters", "qtr", "qtrs", "个季度", "季度", "季"])
_register_unit("yr", ["year", "years", "yr", "yrs", "年"])
_register_unit("c", ["°c", "℃", "celsius", "摄氏度"])
_register_unit("f", ["°f", "℉", "fahrenheit", "华氏度"])
_register_unit("k", ["kelvin", "开尔文"])

_UNIT_CODE_TO_DIMENSION = {
    code: dimension for dimension, table in _UNIT_FACTORS.items() for code in table
}

_CURRENCY_ALIASES = {
    "$": "USD", "us$": "USD", "usd": "USD", "dollar": "USD", "dollars": "USD", "美元": "USD",
    "hk$": "HKD", "hkd": "HKD",
    "a$": "AUD", "aud": "AUD",
    "c$": "CAD", "cad": "CAD",
    "nz$": "NZD", "nzd": "NZD",
    "€": "EUR", "eur": "EUR", "euro": "EUR", "euros": "EUR", "欧元": "EUR",
    "£": "GBP", "gbp": "GBP", "英镑": "GBP",
    "¥": "CNY", "￥": "CNY", "cny": "CNY", "rmb": "CNY", "yuan": "CNY",
    "renminbi": "CNY", "人民币": "CNY",
    "jpy": "JPY", "yen": "JPY", "日元": "JPY",
}

_AMOUNT_PATTERN = r"(?:\d{1,3}(?:[,_]\d{3})+|\d+)(?:\.\d+)?"
_UNIT_PATTERN = re.compile(
    r"(?<![A-Za-z0-9.])(" + _AMOUNT_PATTERN + r")\s*("
    + "|".join(sorted((re.escape(alias) for alias in _UNIT_ALIASES),
                      key=lambda item: (-len(item), item)))
    + r")(?![A-Za-z])",
    re.IGNORECASE,
)
_CURRENCY_PREFIX_RE = re.compile(
    r"(?<![A-Za-z])(" + "|".join(sorted((re.escape(alias) for alias in _CURRENCY_ALIASES),
                                        key=lambda item: (-len(item), item)))
    + r")\s*(" + _AMOUNT_PATTERN + r")",
    re.IGNORECASE,
)
_CURRENCY_SUFFIX_RE = re.compile(
    r"(?<![A-Za-z0-9.])(" + _AMOUNT_PATTERN + r")\s*("
    + "|".join(sorted((re.escape(alias) for alias in _CURRENCY_ALIASES),
                      key=lambda item: (-len(item), item)))
    + r")(?![A-Za-z])",
    re.IGNORECASE,
)


def _amount_key(raw: str) -> str:
    token = raw.replace(" ", "")
    if _GROUPED_NUMBER_RE.fullmatch(token):
        token = _GROUP_SEP_RE.sub("", token)
    value = _decimal_value(token)
    return _format_decimal(value) if value is not None else token


def _is_calendar_unit(alias: str, amount: float) -> bool:
    """「2024 年」「3 月」是日期部件，不是时长。"""
    label = (alias or "").strip().lower()
    if label == "年":
        return amount.is_integer() and 1000 <= amount <= 2999
    if label == "月":
        return 1 <= amount <= 12
    if label in {"季", "季度"}:
        return 1 <= amount <= 4
    return False


def _collect_quantities(text: str) -> List[Dict[str, Any]]:
    """抽取「数字 + 单位/币种」，并附带归一值；同一跨度只取优先级最高的一类。"""
    normalized = (text or "").translate(_FULLWIDTH_DIGITS)
    consumed: List[Tuple[int, int]] = []
    items: List[Dict[str, Any]] = []

    def free(start: int, end: int) -> bool:
        return all(start >= stop or end <= begin for begin, stop in consumed)

    for pattern, prefix in ((_CURRENCY_PREFIX_RE, True), (_CURRENCY_SUFFIX_RE, False)):
        for match in pattern.finditer(normalized):
            span = match.span()
            if not free(*span):
                continue
            if prefix:
                code, raw_amount = match.group(1), match.group(2)
            else:
                raw_amount, code = match.group(1), match.group(2)
            iso = _CURRENCY_ALIASES.get(code.strip().lower())
            if not iso:
                continue
            items.append({
                "category": "currency", "code": iso, "amount": _amount_key(raw_amount),
                "display": match.group(0).strip(),
                "amount_span": match.span(2 if prefix else 1),
            })
            consumed.append(span)

    for match in _UNIT_PATTERN.finditer(normalized):
        span = match.span()
        if not free(*span):
            continue
        raw_amount, alias = match.group(1), match.group(2)
        code = _UNIT_ALIASES.get(alias.strip().lower())
        if not code:
            continue
        amount_value = _decimal_value(_amount_key(raw_amount))
        if amount_value is None:
            continue
        amount = float(amount_value)
        if _is_calendar_unit(alias, amount):
            continue
        dimension = _UNIT_CODE_TO_DIMENSION.get(code)
        if not dimension:
            continue
        factor = _UNIT_FACTORS[dimension][code]
        base: Optional[float]
        if dimension == "temperature":
            base = {"c": amount, "f": (amount - 32.0) * 5.0 / 9.0,
                    "k": amount - 273.15}.get(code)
        else:
            base = amount * factor
        items.append({
            "category": "unit", "amount": _amount_key(raw_amount),
            "dimension": dimension, "code": code, "base": base,
            "display": match.group(0).strip(),
            "amount_span": match.span(1),
        })
        consumed.append(span)
    return items


def _unit_value_close(left: Optional[float], right: Optional[float], rel_tol: float) -> bool:
    if left is None or right is None:
        return False
    if left == right:
        return True
    return abs(left - right) <= rel_tol * max(abs(left), abs(right), 1.0)


def guard_unit_currency_mismatch(source: str, target: str,
                                 rel_tol: float = 0.02) -> Dict[str, Any]:
    """同数字、不同单位/币种的局部错误守卫。

    只在两侧出现「数值相同」的数量时比较单位，因此不会和 number_guard 重复报错：
    数字不同（3 年 → 5 years）交给 number_guard，本守卫不判。

    返回 ``{"unit_mismatches", "currency_mismatches", "rules"}``；rules 是
    ``unit_mismatch`` / ``currency_mismatch`` 的子集，命中任一即应拦截。
    """
    result: Dict[str, Any] = {"unit_mismatches": [], "currency_mismatches": [], "rules": []}
    if not looks_complete(target):
        return result

    source_items = _collect_quantities(source)
    target_items = _collect_quantities(target)
    if not source_items or not target_items:
        return result

    used = set()
    for source_item in source_items:
        paired_index = None
        for index, target_item in enumerate(target_items):
            if index in used:
                continue
            if target_item["amount"] != source_item["amount"]:
                continue
            paired_index = index
            break
        if paired_index is None:
            continue
        used.add(paired_index)
        target_item = target_items[paired_index]

        if source_item["category"] != target_item["category"]:
            result["unit_mismatches"].append({
                "source": source_item["display"], "target": target_item["display"],
                "reason": "quantity_category_mismatch",
            })
            continue
        if source_item["category"] == "currency":
            if source_item["code"] != target_item["code"]:
                result["currency_mismatches"].append({
                    "source": source_item["display"], "target": target_item["display"],
                    "source_currency": source_item["code"],
                    "target_currency": target_item["code"],
                })
            continue
        # unit
        if (source_item["dimension"] != target_item["dimension"]
                or not _unit_value_close(source_item["base"], target_item["base"], rel_tol)):
            result["unit_mismatches"].append({
                "source": source_item["display"], "target": target_item["display"],
                "source_dimension": source_item["dimension"],
                "target_dimension": target_item["dimension"],
                "source_unit": source_item["code"], "target_unit": target_item["code"],
            })

    rules: List[str] = []
    if result["unit_mismatches"]:
        rules.append("unit_mismatch")
    if result["currency_mismatches"]:
        rules.append("currency_mismatch")
    result["rules"] = rules
    return result


def normalize_equivalent_units(source: str, target: str,
                               rel_tol: float = 0.02) -> Tuple[str, List[Dict[str, str]]]:
    """把译文里与源文「单位不同、数值等价」的数量换算回源文数值。

    「3 年 ↔ 36 months」「5 kg ↔ 5000 g」是正确的换算，不是数字错误；但数字守卫
    只比数字形状，会把 3 与 36 判成 `swap` 假拦截（2026-11-04 实测：6 条单位样本里
    2 条等价换算被拦）。这里只改写**等价换算对**的数字部分，单位词和词序都不动，
    改写后再交给数字守卫，等价换算不误拦、真实改数仍照拦
    （「3 年 ↔ 36 years」base 不等价，不在改写范围，仍判 `swap`）。

    返回 ``(normalized_target, pairs)``；``pairs`` 用于审计：
    ``[{"source": "3 年", "target": "36 months", "source_amount": "3",
    "target_amount": "36"}]``。
    """
    if not isinstance(target, str) or not target:
        return target, []
    source_items = [item for item in _collect_quantities(source) if item["category"] == "unit"]
    target_items = [item for item in _collect_quantities(target) if item["category"] == "unit"]
    if not source_items or not target_items:
        return target, []

    pairs: List[Dict[str, str]] = []
    used = set()
    replacements: List[Tuple[Tuple[int, int], str]] = []
    for source_item in source_items:
        for index, target_item in enumerate(target_items):
            if index in used or target_item["dimension"] != source_item["dimension"]:
                continue
            if not _unit_value_close(source_item["base"], target_item["base"], rel_tol):
                continue
            used.add(index)
            pairs.append({
                "source": source_item["display"], "target": target_item["display"],
                "source_amount": source_item["amount"], "target_amount": target_item["amount"],
            })
            # 数字写法相同就没有改写必要；只改数字，单位词不动
            if target_item["amount"] != source_item["amount"]:
                replacements.append((target_item["amount_span"], source_item["amount"]))
            break
    if not replacements:
        return target, pairs

    # 全角数字是 1:1 字符映射，_collect_quantities 给的 span 在原串上同样有效
    normalized = target
    for (start, end), amount in sorted(replacements, key=lambda item: item[0][0], reverse=True):
        normalized = normalized[:start] + amount + normalized[end:]
    return normalized, pairs


def measure_units(text: str) -> int:
    """计量单元 = 汉字数 + 拉丁词数 + 数字串数（不删标点，标点不计入）。"""
    if not isinstance(text, str):
        return 0
    return (len(CJK_RE.findall(text)) + len(LATIN_WORD_RE.findall(text))
            + len(NUMBER_RE.findall(text)))


def length_ratio(source: str, target: str) -> float:
    """目标/源计量单元比；源为空时按 0 处理，不除零。"""
    source_units = measure_units(source)
    target_units = measure_units(target)
    if source_units == 0:
        return float(target_units) if target_units else 0.0
    return target_units / source_units


def number_evidence(source: str, target: str) -> Dict[str, Any]:
    """数字一致性：源有目标无 → 可能漏译；目标有源无 → 可能增译。

    比较按 canonical_numbers 的键做（写法差异、万/亿与 million/billion 的换写不算不一致），
    missing/extra 里仍写原文写法，人工复核时能直接对照。
    """
    source_numbers = extract_numbers(source)
    target_numbers = extract_numbers(target)
    source_keys = {key for _, key in canonical_numbers(source)}
    target_keys = {key for _, key in canonical_numbers(target)}

    def _diff(pairs, other_keys):
        values: List[str] = []
        seen = set()
        for display, key in pairs:
            if key in other_keys or key in seen:
                continue
            seen.add(key)
            values.append(display)
        return values

    missing = _diff(canonical_numbers(source), target_keys)
    extra = _diff(canonical_numbers(target), source_keys)
    return {
        "type": "number_mismatch",
        "source_numbers": source_numbers,
        "target_numbers": target_numbers,
        "missing_numbers": missing,
        "extra_numbers": extra,
        "consistent": not missing and not extra,
    }


def normalize_lang(value: str) -> str:
    if not isinstance(value, str):
        return "unknown"
    lowered = value.strip().lower()
    if lowered in _ZH_ALIASES:
        return "zh"
    if lowered in _EN_ALIASES:
        return "en"
    return lowered or "unknown"


def _heuristic_lang(text: str) -> str:
    """langdetect 不可用时的字符规则：只数汉字与 ASCII 字母，标点/数字/空白不入分母。"""
    cjk = len(CJK_RE.findall(text))
    latin = len(LATIN_WORD_RE.findall(text))
    total = cjk + latin
    if total == 0:
        return "unknown"
    if cjk / total >= 0.5:
        return "zh"
    if latin / total >= 0.5:
        return "en"
    return "unknown"


def detect_language(text: str) -> Dict[str, Any]:
    """语言探测。langdetect 优先；不可用则降级为字符规则，confidence 恒为 None。"""
    stripped = (text or "").strip()
    if not stripped:
        return {"lang": "unknown", "confidence": None, "backend": "none",
                "reason": "empty_text"}
    try:
        from langdetect import detect_langs
        from langdetect.lang_detect_exception import LangDetectException
    except ImportError:
        return {"lang": _heuristic_lang(stripped), "confidence": None,
                "backend": "heuristic_degraded", "reason": "langdetect_unavailable"}
    try:
        candidates = detect_langs(stripped)
    except LangDetectException as exc:
        return {"lang": "unknown", "confidence": None, "backend": "langdetect",
                "reason": f"langdetect_error:{type(exc).__name__}"}
    if not candidates:
        return {"lang": "unknown", "confidence": None, "backend": "langdetect",
                "reason": "no_candidate"}
    best = candidates[0]
    return {"lang": normalize_lang(best.lang), "raw_lang": best.lang,
            "confidence": float(best.prob), "backend": "langdetect", "reason": "ok"}


def _language_direction(source: str, target: str, cfg,
                        declared_source: Optional[str] = None,
                        declared_target: Optional[str] = None) -> Dict[str, Any]:
    declared_source = declared_source or cfg.source_lang
    declared_target = declared_target or cfg.target_lang
    source_probe = detect_language(source)
    target_probe = detect_language(target)
    backends = {source_probe["backend"], target_probe["backend"]}
    backend = backends.pop() if len(backends) == 1 else "mixed"

    confidences = [probe["confidence"] for probe in (source_probe, target_probe)]
    if confidences[0] is not None and confidences[1] is not None:
        confidence: Optional[float] = min(confidences)
    else:
        confidence = None

    expected_source = normalize_lang(declared_source)
    expected_target = normalize_lang(declared_target)
    detected_source = source_probe["lang"]
    detected_target = target_probe["lang"]
    # 只有两端都由 langdetect 给出真实判定时才断言方向；字符规则降级时只记录观测，
    # direction_ok=None 表示"未知"，避免用启发式给出错误的语义偏差标签。
    reliable = (source_probe["backend"] == "langdetect"
                and target_probe["backend"] == "langdetect")
    if not reliable or detected_source == "unknown" or detected_target == "unknown":
        direction_ok: Optional[bool] = None
    else:
        direction_ok = (detected_source == expected_source
                        and detected_target == expected_target)

    return {
        "declared_source_lang": declared_source,
        "declared_target_lang": declared_target,
        "detected_source_lang": detected_source,
        "detected_target_lang": detected_target,
        "language_detection_backend": backend,
        "language_detection_confidence": confidence,
        "language_detection_reason": "{}/{}".format(source_probe["reason"],
                                                    target_probe["reason"]),
        "direction_ok": direction_ok,
    }


def build_rule_evidence(source: str, target: str, cfg,
                        source_lang: Optional[str] = None,
                        target_lang: Optional[str] = None) -> Dict[str, Any]:
    """真实规则证据：长度比例、数字一致性、语言方向。不含任何伪造分数。"""
    ratio = length_ratio(source, target)
    evidence: Dict[str, Any] = {
        "length_ratio": round(ratio, 6),
        "length_ratio_ok": cfg.length_ratio_min <= ratio <= cfg.length_ratio_max,
        "length_ratio_range": [cfg.length_ratio_min, cfg.length_ratio_max],
        "source_units": measure_units(source),
        "target_units": measure_units(target),
        "number_check": number_evidence(source, target) if cfg.number_check
        else {"type": "number_mismatch", "checked": False},
        "language_direction": _language_direction(source, target, cfg,
                                                  source_lang, target_lang),
    }
    return evidence


def build_labels(source: str, target: str, evidence: Dict[str, Any],
                 semantic_score: Optional[float], threshold: float, cfg) -> List[Dict[str, Any]]:
    """问题标签 + 证据。标签只能来自真实观测；低分标签带 model_margin 证据。"""
    labels: List[Dict[str, Any]] = []

    numbers = evidence.get("number_check", {})
    if numbers.get("checked", True) and not numbers.get("consistent", True):
        if numbers.get("missing_numbers"):
            labels.append({"label": "omission", "evidence": {
                "type": "number_mismatch",
                "missing_numbers": numbers["missing_numbers"],
            }})
        if numbers.get("extra_numbers"):
            labels.append({"label": "addition", "evidence": {
                "type": "number_mismatch",
                "extra_numbers": numbers["extra_numbers"],
            }})

    ratio = evidence.get("length_ratio")
    if ratio is not None:
        if ratio < cfg.length_ratio_min:
            labels.append({"label": "omission", "evidence": {
                "type": "length_ratio", "length_ratio": ratio,
                "min": cfg.length_ratio_min,
            }})
        elif ratio > cfg.length_ratio_max:
            labels.append({"label": "addition", "evidence": {
                "type": "length_ratio", "length_ratio": ratio,
                "max": cfg.length_ratio_max,
            }})

    direction = evidence.get("language_direction", {})
    if direction.get("direction_ok") is False:
        labels.append({"label": "semantic_drift", "evidence": {
            "type": "language_direction",
            "detected_source_lang": direction.get("detected_source_lang"),
            "detected_target_lang": direction.get("detected_target_lang"),
        }})

    if semantic_score is not None and semantic_score < threshold:
        labels.append({"label": "mistranslation", "evidence": {
            "type": "model_margin",
            "semantic_score": round(float(semantic_score), 6),
            "threshold": threshold,
            "margin": round(float(semantic_score) - threshold, 6),
        }})

    if not labels and semantic_score is None:
        labels.append({"label": "low_evidence", "evidence": {
            "type": "no_model_score", "reason": "rule_backend_degraded",
        }})
    return labels
