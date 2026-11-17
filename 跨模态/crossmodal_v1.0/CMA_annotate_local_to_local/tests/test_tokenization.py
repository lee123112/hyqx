"""分词与「文件名不能当词」规则测试（表1 #18 第 2 步与研发边界）。"""
from __future__ import annotations

import sys
from pathlib import Path

import pytest

BASE = Path(__file__).resolve().parents[1]
if str(BASE) not in sys.path:
    sys.path.insert(0, str(BASE))

from utils import tokenization as tk  # noqa: E402


# --------------------------------------------------------------------------- #
# 上游预训练分词复用（优先项）
# --------------------------------------------------------------------------- #

def test_pretrained_mode_reuses_upstream_tokens():
    result = tk.tokenize_description(
        "金毛犬在草地上接飞盘",
        image_path="media/shot_001.png",
        upstream_tokens=["金毛犬", "草地", "飞盘"],
        mode=tk.TOKENIZER_PRETRAINED)
    assert result["tokens"] == ["金毛犬", "草地", "飞盘"]
    assert result["source"] == tk.SOURCE_UPSTREAM
    assert result["upstream_used"] is True


def test_pretrained_mode_filters_upstream_tokens_against_filename():
    result = tk.tokenize_description(
        "shot_001.png 金毛犬",
        image_path="media/shot_001.png",
        upstream_tokens=["shot_001.png", "shot_001", "金毛犬"],
        mode=tk.TOKENIZER_PRETRAINED)
    assert result["tokens"] == ["金毛犬"]


def test_pretrained_mode_falls_back_to_jieba_when_upstream_missing():
    result = tk.tokenize_description(
        "金毛犬在草地上接飞盘",
        image_path="media/shot_001.png",
        upstream_tokens=None,
        mode=tk.TOKENIZER_PRETRAINED)
    assert result["tokens"], "应有可用词"
    assert result["source"] in (tk.SOURCE_JIEBA, tk.SOURCE_RULE)
    assert result["upstream_used"] is False


# --------------------------------------------------------------------------- #
# 结巴分词
# --------------------------------------------------------------------------- #

def test_jieba_mode_segments_chinese():
    result = tk.tokenize_description("一只金毛犬在草地上接飞盘",
                                     mode=tk.TOKENIZER_JIEBA)
    assert "草地" in result["tokens"] or "草地" in "".join(result["tokens"])
    assert result["source"] in (tk.SOURCE_JIEBA, tk.SOURCE_RULE)


def test_jieba_unavailable_falls_back_to_rule(monkeypatch):
    monkeypatch.setattr(tk, "_load_jieba", lambda: None)
    result = tk.tokenize_description("金毛犬在草地上接飞盘", mode=tk.TOKENIZER_JIEBA)
    assert result["source"] == tk.SOURCE_RULE
    assert result["tokens"], "规则兜底也必须产出候选片段"
    assert all(len(token) >= 2 for token in result["tokens"])


# --------------------------------------------------------------------------- #
# 文件名不能当词
# --------------------------------------------------------------------------- #

@pytest.mark.parametrize("text", ["shot_001.png", "shot_001", "shot_001.PNG"])
def test_filename_and_stem_are_never_tokens(text):
    result = tk.tokenize_description(text, image_path="media/shot_001.png",
                                     mode=tk.TOKENIZER_JIEBA)
    assert result["tokens"] == []
    assert result["dropped"] >= 1


def test_description_used_only_when_not_filename():
    result = tk.tokenize_description("金毛犬特写", image_path="media/dog.png",
                                     mode=tk.TOKENIZER_JIEBA)
    assert "金毛犬特写" in result["tokens"] or "金毛犬" in result["tokens"]


def test_forbidden_tokens_contains_filename_stem_and_parts():
    forbidden = tk.forbidden_tokens("media/golden_retriever_001.jpg")
    assert "golden_retriever_001.jpg" in forbidden
    assert "golden_retriever_001" in forbidden
    assert "golden" in forbidden
    assert "retriever" in forbidden
    assert "jpg" in forbidden


# --------------------------------------------------------------------------- #
# 清洗与边界
# --------------------------------------------------------------------------- #

def test_punctuation_only_text_yields_no_tokens():
    result = tk.tokenize_description("，。！？——  ", mode=tk.TOKENIZER_JIEBA)
    assert result["tokens"] == []


def test_stopwords_are_dropped():
    assert tk.is_stopword("的")
    assert tk.is_stopword("然后")
    result = tk.tokenize_description("的 金毛犬 然后 草地", mode=tk.TOKENIZER_JIEBA)
    assert "的" not in result["tokens"]
    assert "然后" not in result["tokens"]


def test_tokens_are_deduplicated_preserving_order():
    result = tk.tokenize_description("草地 金毛犬 草地",
                                     mode=tk.TOKENIZER_JIEBA)
    assert result["tokens"].count("草地") == 1
    assert result["tokens"].index("草地") < result["tokens"].index("金毛犬")


def test_max_tokens_truncates_and_flags():
    result = tk.tokenize_description("金毛犬 草地 飞盘 公园 阳光",
                                     mode=tk.TOKENIZER_JIEBA, max_tokens=2)
    assert len(result["tokens"]) == 2
    assert result["truncated"] is True


def test_empty_text_yields_no_tokens():
    assert tk.tokenize_description("", mode=tk.TOKENIZER_JIEBA)["tokens"] == []
    assert tk.tokenize_description("   ", mode=tk.TOKENIZER_JIEBA)["tokens"] == []


def test_normalize_token_strips_whitespace_and_punctuation():
    assert tk.normalize_token("  金毛犬，") == "金毛犬"
    assert tk.normalize_token("\u3000  ") == ""


def test_latin_words_shorter_than_two_chars_are_dropped():
    result = tk.tokenize_description("a dog runs", mode=tk.TOKENIZER_JIEBA)
    assert "a" not in result["tokens"]
    assert "dog" in result["tokens"]


# --------------------------------------------------------------------------- #
# 数词 / 量词 / 词性过滤（v1.0.1 效果优化）
# --------------------------------------------------------------------------- #

def test_numerals_and_classifiers_are_dropped():
    """「一只金毛犬」里的数词与量词不参与词-框匹配。"""
    result = tk.tokenize_description("一只金毛犬在草地上接飞盘",
                                     mode=tk.TOKENIZER_JIEBA)
    assert "金毛犬" in result["tokens"]
    for token in ("一只", "一", "只"):
        assert token not in result["tokens"]


def test_stopword_and_function_word_counts_are_reported():
    result = tk.tokenize_description("一只金毛犬在草地上接飞盘",
                                     mode=tk.TOKENIZER_JIEBA)
    detail = result["dropped_detail"]
    assert result["dropped"] == sum(detail.values())
    assert detail.get("function_word", 0) >= 1      # 「在」等介词/助词
    assert detail.get("stopword", 0) >= 1           # 「一只」等模板词


def test_punctuation_is_dropped_with_symbol_reason():
    result = tk.tokenize_description("金毛犬，草地！", mode=tk.TOKENIZER_JIEBA)
    assert "金毛犬" in result["tokens"]
    assert all("，" not in token and "！" not in token for token in result["tokens"])


def test_filename_tokens_report_filename_reason():
    result = tk.tokenize_description("shot_001.png 金毛犬",
                                     image_path="media/shot_001.png",
                                     mode=tk.TOKENIZER_JIEBA)
    assert result["tokens"] == ["金毛犬"]
    assert result["dropped_detail"].get("filename", 0) >= 1


def test_upstream_tokens_are_also_pos_filtered():
    result = tk.tokenize_description(
        "一只金毛犬在草地",
        upstream_tokens=["一只", "金毛犬", "在", "草地"],
        mode=tk.TOKENIZER_PRETRAINED)
    assert result["tokens"] == ["金毛犬", "草地"]
    assert result["dropped"] >= 2


# --------------------------------------------------------------------------- #
# 名词短语合并（作为词的上下文变体，不改变「一词一框」）
# --------------------------------------------------------------------------- #

def test_chinese_modifier_noun_is_merged():
    result = tk.tokenize_description("红色小汽车", mode=tk.TOKENIZER_JIEBA)
    assert result["tokens"] == ["红色", "小汽车"]
    assert [phrase["text"] for phrase in result["phrases"]] == ["红色小汽车"]
    # 每个词本身仍是一个匹配单元，短语只是附加变体
    assert result["variants"][0] == ["红色", "红色小汽车"]
    assert result["variants"][1] == ["小汽车", "红色小汽车"]
    assert result["phrase_count"] == 1
    assert result["variant_count"] == 3


def test_chinese_gap_particle_does_not_block_merge():
    result = tk.tokenize_description("红色的汽车", mode=tk.TOKENIZER_JIEBA)
    assert "的" not in result["tokens"]
    assert [phrase["text"] for phrase in result["phrases"]] == ["红色汽车"]


def test_non_modifier_across_de_is_not_merged():
    """「跑的车」不是名词短语（修饰语不是形容词类），不合并。"""
    assert tk.merge_phrases("跑的车", ["跑", "车"], ["v", "n"]) == []


def test_punctuation_between_tokens_blocks_merge():
    assert tk.merge_phrases("红色，小汽车", ["红色", "小汽车"], ["a", "n"]) == []


def test_verb_head_is_not_merged():
    assert tk.merge_phrases("草地 接球", ["草地", "接球"], ["n", "v"]) == []


def test_latin_bigram_is_merged_with_space():
    assert [item["text"] for item in
            tk.merge_phrases("a black woman", ["black", "woman"], ["eng", "eng"])] \
        == ["black woman"]


def test_latin_noun_gerund_is_not_merged():
    assert tk.merge_phrases("a woman holding a bag",
                            ["woman", "holding"], ["eng", "eng"]) == []


def test_merge_can_be_disabled():
    result = tk.tokenize_description("红色小汽车", mode=tk.TOKENIZER_JIEBA,
                                     merge_phrases_enabled=False)
    assert result["phrases"] == []
    assert result["variants"] == [["红色"], ["小汽车"]]


def test_latin_phrases_use_upstream_tokens():
    result = tk.tokenize_description(
        "a black woman", upstream_tokens=["black", "woman"],
        mode=tk.TOKENIZER_PRETRAINED)
    assert result["upstream_used"] is True
    assert [phrase["text"] for phrase in result["phrases"]] == ["black woman"]
