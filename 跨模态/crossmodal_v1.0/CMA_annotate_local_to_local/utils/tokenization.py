"""中文分词、名词短语合并与「文件名不能当词」过滤（表1 #18 第 2/4 步与研发边界）。

优先级（与研发测边界一致）：
1. 优先复用**预训练文本产线**的分词结果（上游 ``tokens``）；
2. 上游没有时用**结巴分词**，并把实际使用的分词后端写进日志与血缘；
3. 两者都不可用时用**确定性规则兜底**（仅中文串与拉丁词，不做模型推理），
   并在血缘标记 ``tokenizer_source=rule_fallback``。

在分词之后再做三件事（不引入新模型，全部是可解释的规则）：
- **停用词/量词/数词过滤**：功能词、代词、数词、量词不参与词-框匹配；
- **标点与文件名过滤**：纯符号片段、图像文件名及其主干/片段永不作为词；
- **名词短语合并**：把「修饰语 + 名词」合并成短语（中文 ≤4 字、英文 ≤2 词），
  作为**该词的上下文变体**参与打分；词本身仍是匹配单元（一 词 一 框不变）。

禁止：把文件名当作词；新建多语言模型。
"""
from __future__ import annotations

import os
import re
from typing import Any, Dict, Iterable, List, Optional, Sequence, Set, Tuple

from .schemas import TOKENIZER_JIEBA, TOKENIZER_PRETRAINED

__all__ = [
    "SOURCE_JIEBA",
    "SOURCE_RULE",
    "SOURCE_UPSTREAM",
    "STOPWORDS",
    "TOKENIZER_JIEBA",
    "TOKENIZER_PRETRAINED",
    "forbidden_tokens",
    "is_stopword",
    "merge_phrases",
    "normalize_token",
    "phrase_spans",
    "tokenize_description",
]

SOURCE_UPSTREAM = "pretrained"
SOURCE_JIEBA = "jieba"
SOURCE_RULE = "rule_fallback"

_CJK_RE = re.compile(r"[一-鿿]")
_CJK_RUN_RE = re.compile(r"[一-鿿]+")
_CJK_ONLY_RE = re.compile(r"^[一-鿿]+$")
_ASCII_WORD_RE = re.compile(r"[A-Za-z][A-Za-z0-9]*")
_LATIN_ONLY_RE = re.compile(r"^[A-Za-z][A-Za-z'\-]*$")
_DIGIT_RE = re.compile(r"[0-9]")
_PART_SPLIT_RE = re.compile(r"[_\-.\s]+")

# 只剔除功能词与纯虚词，避免影响「词-区域」语义对应；
# 参考实现不得把该表当作模型能力。
STOPWORDS: Set[str] = {
    # 中文功能词
    "的", "了", "在", "是", "和", "与", "及", "或", "等", "被", "把", "让", "给",
    "对", "为", "以", "之", "其", "而", "但", "却", "则", "因", "由", "于", "从",
    "向", "到", "就", "都", "也", "很", "最", "更", "还", "又", "才", "只", "不",
    "没", "有", "我", "你", "他", "她", "它", "们", "这", "那", "个", "上", "下",
    "里", "中", "后", "前", "然后", "而且", "并且", "一个", "一只", "这个", "那个",
    "什么", "怎么", "以及", "还有", "图片", "照片", "图像", "图中", "画面", "如下",
    "所示", "可以", "能够", "进行", "使用", "通过",
    # 中文描述模板里的高频虚词（不指向任何局部框）
    "其中", "此时", "此后", "之后", "之前", "当时", "正在", "已经", "出现", "显示",
    "展示", "描述", "所示的是", "看起来", "似乎", "一些", "旁边", "附近", "上面",
    "下面", "里面", "外面", "中间", "左右",
    # 英文功能词
    "a", "an", "the", "of", "and", "or", "is", "are", "was", "were", "am", "be",
    "been", "being", "to", "in", "on", "at", "by", "for", "from", "into", "onto",
    "over", "under", "after", "before", "during", "near", "behind", "between",
    "up", "down", "out", "off", "again", "also", "just", "very", "with", "without",
    "there", "here", "then", "when", "while", "as", "this", "that", "these",
    "those", "it", "its", "he", "she", "they", "them", "his", "her", "their",
    "we", "you", "i", "my", "your", "our", "some", "many", "much", "more", "most",
    "all", "both", "each", "every", "no", "not", "can", "could", "will", "would",
    "shall", "should", "may", "might", "must", "do", "does", "did", "has", "have",
    "had", "s", "t", "who", "whom", "which", "what", "where", "why", "how",
    # 英文描述模板词（不指向任何局部框）
    "image", "images", "picture", "pictures", "photo", "photos", "photograph",
    "scene", "video", "frame", "frames", "caption", "description", "shows",
    "showing", "shown", "depicts", "depicting", "features", "featuring",
    "appears", "visible", "background", "foreground", "left", "right",
}

# 数词与量词：不指向局部区域，参与打分只会稀释一对一匹配。
NUMERALS: Set[str] = {
    "一", "二", "三", "四", "五", "六", "七", "八", "九", "十", "百", "千", "万",
    "两", "几", "半", "第", "些",
    "one", "two", "three", "four", "five", "six", "seven", "eight", "nine", "ten",
    "eleven", "twelve", "twenty", "thirty", "forty", "fifty", "hundred",
    "thousand", "million", "several", "few",
}

CLASSIFIERS: Set[str] = {
    "个", "只", "条", "张", "幅", "位", "名", "群", "辆", "架", "台", "件", "套",
    "双", "部", "片", "棵", "根", "颗", "座",
}

#: 名词短语的「修饰语」白名单：颜色/大小/新旧等单字修饰语
_MODIFIER_CHARS: Set[str] = set("大小长短高低宽窄圆方粗细新旧红橙黄绿青蓝紫黑白灰棕粉金银木铁石布纸")

#: 英文修饰语白名单（`jieba.posseg` 对拉丁词一律标 `eng`，无法靠词性区分）
_LATIN_MODIFIERS: Set[str] = {
    "black", "white", "red", "blue", "green", "yellow", "brown", "gray", "grey",
    "pink", "purple", "orange", "gold", "golden", "silver", "beige",
    "wooden", "metal", "metallic", "plastic", "glass", "stone", "brick",
    "concrete", "leather", "cotton", "wool", "silk",
    "small", "large", "big", "tiny", "huge", "tall", "short", "young", "old",
    "new", "bright", "dark", "light", "long", "wide", "narrow", "round",
    "square", "striped", "spotted", "plain", "open", "closed", "empty", "full",
    "beautiful", "lovely", "colorful", "little", "wooden", "painted",
}

#: 不阻断名词短语合并的结构助词（「红色的汽车」→ 红色汽车）
_PHRASE_GAP_PARTICLES = {"的", "地", "得"}

#: 名词短语合并的硬边界（中文 ≤6 字，英文 ≤2 词且 ≤40 字符）
PHRASE_MAX_CJK_CHARS = 6
PHRASE_MAX_CJK_CHARS_NO_POS = 4
PHRASE_MAX_LATIN_CHARS = 40

_PUNCT_CHARS = set(
    "，。！？、；：“”‘’（）《》〈〉【】「」『』…—～·!?.,;:\"'`()[]{}<>-_/\\|~@#$%^&*+= \t\r\n"
    "　​"
)

# 分词后的可合并词性：名词类与常见修饰类。动词（v/vd）、副词、助词等不参与合并。
_POS_NOUN_PREFIXES = ("n",)      # n / nr / ns / nt / nz / ng
_POS_MODIFIER = {"a", "an", "b", "n", "nr", "ns", "nt", "nz", "ng", "vn", "eng"}

#: 不指向任何局部区域的词性（数词、量词、代词、介词、连词、助词、副词、标点…），
#: 仅在使用 ``jieba.posseg`` 时生效；没有词性模型时退回 ``STOPWORDS`` 等规则。
_POS_DROP = {"m", "q", "r", "p", "c", "u", "uj", "ul", "ug", "ud", "uz", "y", "e",
             "o", "d", "x", "w"}

_JIEBA_MODULE: Any = None
_JIEBA_LOADED = False
_POSSEG_MODULE: Any = None
_POSSEG_LOADED = False


def _load_jieba() -> Optional[Any]:
    """延迟加载结巴分词；不可用返回 ``None``（不抛错，由调用方兜底）。"""
    global _JIEBA_MODULE, _JIEBA_LOADED
    if not _JIEBA_LOADED:
        try:
            import jieba  # type: ignore
        except Exception:            # pragma: no cover - 取决于运行环境
            _JIEBA_MODULE = None
        else:
            _JIEBA_MODULE = jieba
        _JIEBA_LOADED = True
    return _JIEBA_MODULE


def _load_posseg() -> Optional[Any]:
    """延迟加载 ``jieba.posseg``（仅用于名词短语合并的词性判断）。"""
    global _POSSEG_MODULE, _POSSEG_LOADED
    if not _POSSEG_LOADED:
        try:
            import jieba.posseg as posseg  # type: ignore
        except Exception:            # pragma: no cover - 取决于运行环境
            _POSSEG_MODULE = None
        else:
            _POSSEG_MODULE = posseg
        _POSSEG_LOADED = True
    return _POSSEG_MODULE


def jieba_available() -> bool:
    return _load_jieba() is not None


def normalize_token(value: Any) -> str:
    """去首尾空白与标点；内部标点保留。"""
    if value is None:
        return ""
    text = str(value).strip()
    while text and text[0] in _PUNCT_CHARS:
        text = text[1:]
    while text and text[-1] in _PUNCT_CHARS:
        text = text[:-1]
    return text.strip()


def is_stopword(token: str) -> bool:
    candidate = normalize_token(token)
    if not candidate:
        return True
    lowered = candidate.lower()
    if lowered in STOPWORDS or candidate in NUMERALS or candidate in CLASSIFIERS:
        return True
    return lowered in NUMERALS


def forbidden_tokens(image_path: Optional[Any]) -> Set[str]:
    """由图像文件名推导禁止作为词的字符串集合（文件名、主干、扩展名、主干片段）。"""
    if not image_path:
        return set()
    name = os.path.basename(str(image_path).replace("\\", "/"))
    if not name:
        return set()
    forbidden = {name.lower()}
    stem, extension = os.path.splitext(name)
    if stem:
        forbidden.add(stem.lower())
    if extension:
        forbidden.add(extension.lower())
        forbidden.add(extension.lstrip(".").lower())
    for part in _PART_SPLIT_RE.split(stem):
        lowered = part.lower()
        if len(lowered) < 2:
            continue
        if re.search(r"[a-z]", lowered) or _CJK_RE.search(part):
            forbidden.add(lowered)
    return forbidden


def _reject_reason(token: str, forbidden: Set[str],
                   pos: Optional[str] = None) -> Optional[str]:
    """返回剔除原因码；合法词返回 ``None``。"""
    if not token:
        return "empty"
    lowered = token.lower()
    if lowered in forbidden:
        return "filename"
    if pos and pos in _POS_DROP:
        return "function_word"
    if is_stopword(token):
        if lowered in STOPWORDS or token in NUMERALS or token in CLASSIFIERS or lowered in NUMERALS:
            return "stopword"
        return "empty"
    has_cjk = bool(_CJK_RE.search(token))
    has_latin = bool(re.search(r"[A-Za-z]", token))
    if not has_cjk and not has_latin:
        return "numeric" if _DIGIT_RE.search(token) else "symbol"
    if len(token) == 1 and not has_cjk:
        return "symbol"
    return None


def _clean(candidates: Iterable[Any], forbidden: Set[str],
           tags: Optional[Sequence[str]] = None
           ) -> Tuple[List[str], int, Dict[str, int]]:
    tokens: List[str] = []
    seen: Set[str] = set()
    dropped = 0
    detail: Dict[str, int] = {}
    for index, raw in enumerate(candidates):
        token = normalize_token(raw)
        pos = tags[index] if tags is not None and index < len(tags) else None
        reason = _reject_reason(token, forbidden, pos)
        if reason is None and token.lower() in seen:
            reason = "duplicate"
        if reason is not None:
            dropped += 1
            detail[reason] = detail.get(reason, 0) + 1
            continue
        seen.add(token.lower())
        tokens.append(token)
    return tokens, dropped, detail


def _rule_segment(text: str) -> List[str]:
    """确定性规则兜底：中文串取整段（≤4 字）或连续二元组，拉丁词取整词。"""
    pieces: List[Tuple[int, str]] = []
    for match in _CJK_RUN_RE.finditer(text):
        run = match.group(0)
        if len(run) < 2:
            continue
        if len(run) <= 4:
            pieces.append((match.start(), run))
        else:
            for offset in range(len(run) - 1):
                pieces.append((match.start() + offset, run[offset:offset + 2]))
    for match in _ASCII_WORD_RE.finditer(text):
        pieces.append((match.start(), match.group(0)))
    pieces.sort(key=lambda item: item[0])
    return [piece for _position, piece in pieces]


# --------------------------------------------------------------------------- #
# 名词短语合并
# --------------------------------------------------------------------------- #

def _spans_in(text: str, tokens: Sequence[str]) -> List[Optional[Tuple[int, int]]]:
    """按出现顺序定位每个词在原文中的区间；定位不到记 ``None``（不参与合并）。"""
    lowered = text.lower()
    spans: List[Optional[Tuple[int, int]]] = []
    cursor = 0
    for token in tokens:
        index = lowered.find(token.lower(), cursor)
        if index < 0:
            spans.append(None)
            continue
        spans.append((index, index + len(token)))
        cursor = index + len(token)
    return spans


def _pos_tags(tokens: Sequence[str]) -> Optional[List[str]]:
    """逐词词性；``posseg`` 不可用时返回 ``None``（走确定性规则）。"""
    module = _load_posseg()
    if module is None:
        return None
    tags: List[str] = []
    try:
        for token in tokens:
            pairs = list(module.cut(token))
            tags.append(pairs[0].flag if pairs else "x")
    except Exception:            # pragma: no cover - 词性模型异常时退回规则
        return None
    return tags


def _is_modifier_token(token: str, tags: Optional[Sequence[str]], index: int) -> bool:
    """「X 的 Y」里 X 是否为可保留的修饰语（形容词/颜色/大小等）。"""
    if tags is not None and index < len(tags) and tags[index] in ("a", "an", "b"):
        return True
    if token in _MODIFIER_CHARS:
        return True
    return token.endswith("色")


def _pairable(first: str, second: str, tags: Optional[Sequence[str]],
              index: int) -> bool:
    """判断相邻两词能否合并为名词短语。"""
    first_cjk = bool(_CJK_ONLY_RE.match(first))
    second_cjk = bool(_CJK_ONLY_RE.match(second))
    first_latin = bool(_LATIN_ONLY_RE.match(first))
    second_latin = bool(_LATIN_ONLY_RE.match(second))

    if first_latin and second_latin:
        if len(first) + len(second) + 1 > PHRASE_MAX_LATIN_CHARS:
            return False
        # 「woman holding」这类「名词 + 动名词」不是名词短语；只有修饰语开头才合并
        if (second.lower().endswith("ing") and len(second) > 4
                and first.lower() not in _LATIN_MODIFIERS):
            return False
        return True

    if not (first_cjk and second_cjk):
        return False
    limit = PHRASE_MAX_CJK_CHARS if tags is not None else PHRASE_MAX_CJK_CHARS_NO_POS
    if len(first) + len(second) > limit:
        return False

    if tags is not None:
        head, modifier = tags[index + 1], tags[index]
        if not head.startswith(_POS_NOUN_PREFIXES):
            return False
        return modifier in _POS_MODIFIER
    # 无词性模型：名词通常是多字词；单字修饰语只放行颜色/大小等白名单
    if len(second) < 2:
        return False
    return len(first) >= 2 or first in _MODIFIER_CHARS


def phrase_spans(text: str, tokens: Sequence[str],
                 tags: Optional[Sequence[str]] = None
                 ) -> List[Tuple[int, int]]:
    """返回可合并的相邻词下标对 ``(i, i+1)``。

    合并条件：原文中两词之间只有空白（不跨标点）、词性满足「修饰语 + 名词」。
    """
    if len(tokens) < 2:
        return []
    spans = _spans_in(text, tokens)
    result: List[Tuple[int, int]] = []
    for index in range(len(tokens) - 1):
        left, right = spans[index], spans[index + 1]
        if left is None or right is None:
            continue
        gap = text[left[1]:right[0]].strip()
        if gap:
            # 「红色的汽车」：结构助词不阻断合并，但修饰语必须是形容词类
            if gap not in _PHRASE_GAP_PARTICLES:
                continue
            if not _is_modifier_token(tokens[index], tags, index):
                continue
        if not _pairable(tokens[index], tokens[index + 1], tags, index):
            continue
        result.append((index, index + 1))
    return result


def merge_phrases(text: str, tokens: Sequence[str],
                  tags: Optional[Sequence[str]] = None) -> List[Dict[str, Any]]:
    """把相邻「修饰语 + 名词」合并成短语，返回短语及其覆盖的词下标。"""
    phrases: List[Dict[str, Any]] = []
    for start, end in phrase_spans(text, tokens, tags):
        first, second = tokens[start], tokens[end]
        joiner = " " if _LATIN_ONLY_RE.match(first) else ""
        phrases.append({"text": "%s%s%s" % (first, joiner, second),
                        "start": start,
                        "end": end,
                        "tokens": [first, second],
                        "kind": "noun_phrase"})
    return phrases


def _build_variants(tokens: Sequence[str],
                    phrases: Sequence[Dict[str, Any]]) -> List[List[str]]:
    """每个词的匹配文本变体：首个是词本身，其后是覆盖它的名词短语。"""
    variants: List[List[str]] = [[token] for token in tokens]
    for phrase in phrases:
        for index in (phrase["start"], phrase["end"]):
            bucket = variants[index]
            if phrase["text"] not in bucket:
                bucket.append(phrase["text"])
    return variants


def tokenize_description(text: Any, *,
                         image_path: Optional[Any] = None,
                         upstream_tokens: Optional[Sequence[Any]] = None,
                         mode: str = TOKENIZER_PRETRAINED,
                         max_tokens: int = 64,
                         max_chars: Optional[int] = None,
                         merge_phrases_enabled: bool = True) -> Dict[str, Any]:
    """产出用于词-区域匹配的中文词片段，并返回完整的分词溯源信息。"""
    raw = text if isinstance(text, str) else ("" if text is None else str(text))
    text_truncated = False
    if max_chars is not None and max_chars > 0 and len(raw) > max_chars:
        raw = raw[:max_chars]
        text_truncated = True

    forbidden = forbidden_tokens(image_path)
    source: Optional[str] = None
    backend: Optional[str] = None
    upstream_used = False
    tokens: Optional[List[str]] = None
    dropped = 0
    dropped_detail: Dict[str, int] = {}

    if mode == TOKENIZER_PRETRAINED and upstream_tokens:
        upstream_list = [str(item) for item in upstream_tokens]
        cleaned, dropped, dropped_detail = _clean(upstream_list, forbidden,
                                                  _pos_tags(upstream_list))
        if cleaned:
            tokens = cleaned
            source = SOURCE_UPSTREAM
            backend = "pretrained-text-pipeline"
            upstream_used = True

    if tokens is None:
        module = _load_jieba()
        if module is not None:
            posseg = _load_posseg()
            pieces: Optional[List[str]] = None
            tags: Optional[List[str]] = None
            if posseg is not None:
                try:
                    pairs = list(posseg.cut(raw))
                except Exception:          # pragma: no cover - 词性模型异常
                    pairs = []
                if pairs:
                    pieces = [str(pair.word) for pair in pairs]
                    tags = [str(pair.flag) for pair in pairs]
            if pieces is None:
                pieces = [str(piece) for piece in module.lcut(raw)]
                tags = None
            source = SOURCE_JIEBA
            backend = "jieba"
            tokens, dropped, dropped_detail = _clean(pieces, forbidden, tags)
        else:
            pieces = _rule_segment(raw)
            source = SOURCE_RULE
            backend = "rule"
            tokens, dropped, dropped_detail = _clean(pieces, forbidden)

    raw_count = len(tokens)
    truncated = False
    if max_tokens and len(tokens) > int(max_tokens):
        tokens = tokens[:int(max_tokens)]
        truncated = True

    phrases: List[Dict[str, Any]] = []
    if merge_phrases_enabled:
        tags = _pos_tags(tokens)
        phrases = merge_phrases(raw, tokens, tags)
    variants = _build_variants(tokens, phrases)

    return {"tokens": tokens,
            "source": source,
            "backend": backend,
            "dropped": dropped,
            "dropped_detail": dropped_detail,
            "truncated": truncated,
            "upstream_used": upstream_used,
            "raw_count": raw_count,
            "text_truncated": text_truncated,
            "text_chars": len(raw),
            "phrases": phrases,
            "phrase_count": len(phrases),
            "variants": variants,
            "variant_count": len({item for unit in variants for item in unit})}
