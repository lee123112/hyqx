"""繁→简归一化专项测试。

覆盖：
1. 词典解析（注释/非法行/多候选）与有效性校验；
2. 解析优先级（包内自带 → ``$MODEL_ROOT/opencc`` → 内置兜底），全程离线；
3. ``normalize_text`` / ``edit_distance_ratio`` / ``text_error_metrics`` 的开关口径；
4. 算子端到端：真实繁体转写 + 简体描述从「被隔离」变为「通过」，且审计字段可追溯；
5. 英文/数字/已简体文本行为不变。
"""
from __future__ import annotations

import json
from pathlib import Path

import pytest

import inference
from tests import fixtures
from utils import scoring, t2s
from utils.schemas import ConfigError

#: 第八轮真实数据中的一条典型记录（ASR 繁体转写 vs 简体对齐描述）。
TRAD = "但讓一百分之四佔局市場主立位置"
SIMPLIFIED = "但让一百分之四占局市场主立位置"
DESC = "但仍以百分之四占据市场主力位置"
#: 改造前口径：繁体/简体书写差异直接计入字符级编辑距离 → 0.4（> 0.30 被隔离）。
OLD_RATIO = 0.4
#: 改造后：先转简体再比较 → 4/15 ≈ 0.2667（<= 0.30 通过）。
NEW_RATIO = 4.0 / 15.0


@pytest.fixture()
def fresh_t2s_cache():
    """每个用例前后清空 t2s 全局缓存，避免词典优先级/环境变量互相污染。"""
    t2s.reset_cache()
    yield
    t2s.reset_cache()


# --------------------------------------------------------------------------- #
# 词典解析与来源
# --------------------------------------------------------------------------- #

def test_parse_table_skips_comments_and_malformed_lines():
    text = "\n".join([
        "# OpenCC TSCharacters",
        "",
        "區\t区",
        "間\t间 間 閒",          # 多候选：取首个
        "malformed-line-without-tab",
        "too\tmany\tcolumns",
        "ＡＢ\tab",             # 非单字符 key：忽略
        "顎\t颚",
    ])
    mapping = t2s.parse_table(text)
    assert mapping == {"區": "区", "間": "间", "顎": "颚"}


def test_load_table_rejects_suspiciously_small_file(tmp_path):
    path = tmp_path / "TSCharacters.txt"
    path.write_text("# broken\n區\t区\n", encoding="utf-8")
    with pytest.raises(ValueError):
        t2s.load_table(path)


def test_package_dictionary_is_parsed_and_default_precedence(fresh_t2s_cache):
    snapshot = t2s.mapping_snapshot()
    assert snapshot["source"] == t2s.SOURCE_PACKAGE
    assert snapshot["entries"] >= 1000
    assert Path(snapshot["path"]).is_file()
    assert t2s.to_simplified("區間躍塊鐘") == "区间跃块钟"


def test_package_dictionary_wins_over_model_root(fresh_t2s_cache, tmp_path, monkeypatch):
    model_dir = tmp_path / "model" / "opencc"
    model_dir.mkdir(parents=True)
    # 部署目录放一份「故意错误」的词典：包内自带必须优先于它。
    lines = ["區\t区"] * 120 + ["間\t间"]
    (model_dir / "TSCharacters.txt").write_text("\n".join(lines), encoding="utf-8")
    monkeypatch.setenv("MODEL_ROOT", str(tmp_path / "model"))
    t2s.reset_cache()
    snapshot = t2s.mapping_snapshot()
    assert snapshot["source"] == t2s.SOURCE_PACKAGE
    assert snapshot["entries"] >= 1000


def test_model_root_fallback_when_package_table_missing(fresh_t2s_cache, tmp_path, monkeypatch):
    model_dir = tmp_path / "model" / "opencc"
    model_dir.mkdir(parents=True)
    # 120 条以上才通过有效性校验；用高频字构造一份最小合法词典。
    lines = ["區\t区", "間\t间", "躍\t跃", "塊\t块", "鐘\t钟"]
    lines += ["%s\t%s" % (chr(0x4E00 + index), chr(0x4E00 + index)) for index in range(120)]
    (model_dir / "TSCharacters.txt").write_text("\n".join(lines), encoding="utf-8")
    monkeypatch.setattr(t2s, "table_candidates",
                        lambda: [(t2s.SOURCE_MODEL_ROOT, model_dir / "TSCharacters.txt")])
    t2s.reset_cache()
    snapshot = t2s.mapping_snapshot()
    assert snapshot["source"] == t2s.SOURCE_MODEL_ROOT
    assert snapshot["path"].endswith("TSCharacters.txt")
    assert t2s.to_simplified("區間") == "区间"


def test_builtin_fallback_when_no_dictionary_available(fresh_t2s_cache, monkeypatch):
    monkeypatch.setattr(t2s, "table_candidates", lambda: [])
    t2s.reset_cache()
    snapshot = t2s.mapping_snapshot()
    assert snapshot["source"] == t2s.SOURCE_BUILTIN
    assert snapshot["entries"] >= 500
    assert snapshot["path"] is None
    # 真实数据中的繁体字在兜底映射里必须全部可转（兜底不是空壳）。
    assert t2s.to_simplified(TRAD) == SIMPLIFIED
    assert t2s.to_simplified("中心六區中間活躍板塊") == "中心六区中间活跃板块"
    assert t2s._builtin_mapping()["區"] == "区"


# --------------------------------------------------------------------------- #
# 归一化 / 编辑距离口径
# --------------------------------------------------------------------------- #

@pytest.mark.parametrize("raw,expected", [
    (TRAD, SIMPLIFIED),
    ("中心六區中間促成二手住宅的十大活躍板塊中", "中心六区中间促成二手住宅的十大活跃板块中"),
    ("Hello， 世界!", "hello世界"),               # 英文/符号行为不变
    ("ＡＢＣ１２３", "abc123"),                    # 全角数字/字母行为不变
    ("今天天气很好", "今天天气很好"),                  # 已是简体：保持原样
    ("著名的旅游景点，关于这个问题", "著名的旅游景点关于这个问题"),  # 简体常用字不改写
    ("一只猫在台风天散步", "一只猫在台风天散步"),        # 简繁同形字保持原样
    ("", ""),
])
def test_normalize_text_with_traditional_to_simplified(raw, expected):
    # 本组用例只断言 t2s 口径；显式关闭 ITN（ITN 由 test_itn.py 单独覆盖），
    # 避免「百分之四→4%」等数字归一干扰繁简断言。
    assert scoring.normalize_text(raw, inverse_text_normalization=False) == expected
    assert scoring.normalize_text(raw, traditional_to_simplified=True,
                                  inverse_text_normalization=False) == expected


def test_normalize_text_switch_off_keeps_original_script():
    assert scoring.normalize_text(TRAD, traditional_to_simplified=False,
                                  inverse_text_normalization=False) == TRAD
    assert scoring.normalize_text("Hello， 世界!", traditional_to_simplified=False,
                                  inverse_text_normalization=False) == "hello世界"


def test_edit_distance_ratio_benefits_from_normalization():
    assert scoring.edit_distance_ratio(TRAD, DESC) == pytest.approx(NEW_RATIO)
    assert scoring.edit_distance_ratio(TRAD, DESC, traditional_to_simplified=True) == \
        pytest.approx(NEW_RATIO)
    assert scoring.edit_distance_ratio(TRAD, DESC, traditional_to_simplified=False) == \
        pytest.approx(OLD_RATIO)
    # 阈值语义不变：0.2667 <= 0.30 才通过，0.4 > 0.30 判失败。
    assert scoring.decide_edit(NEW_RATIO, edit_ratio_max=0.30)[0] is True
    assert scoring.decide_edit(OLD_RATIO, edit_ratio_max=0.30)[0] is False


def test_text_error_metrics_keeps_contract_and_audits_before_after():
    metrics = scoring.text_error_metrics(TRAD, DESC)
    # 原有契约字段全部保留且语义不变（cer/edit_distance_ratio 同值）。
    for key in ("transcript_normalized", "description_normalized", "edit_distance",
                "cer", "wer", "edit_distance_ratio", "metric"):
        assert key in metrics
    assert metrics["metric"] == "CER"
    assert metrics["cer"] == pytest.approx(metrics["edit_distance_ratio"])
    # 归一化前后均可追溯：最终口径已转简体，before 保留原繁体口径。
    assert metrics["traditional_to_simplified"] is True
    assert metrics["transcript_normalized"] == SIMPLIFIED
    assert metrics["description_normalized"] == DESC
    assert metrics["transcript_normalized_before_t2s"] == TRAD
    assert metrics["description_normalized_before_t2s"] == DESC
    assert metrics["traditional_chars_converted"] == 3      # 讓/佔/場
    assert metrics["t2s_dictionary_source"] == t2s.SOURCE_PACKAGE
    assert metrics["t2s_mapping_entries"] >= 1000
    assert metrics["edit_distance_ratio"] == pytest.approx(NEW_RATIO)


def test_text_error_metrics_switch_off_restores_old_contract():
    metrics = scoring.text_error_metrics(TRAD, DESC, traditional_to_simplified=False)
    assert metrics["traditional_to_simplified"] is False
    assert metrics["transcript_normalized"] == TRAD
    assert metrics["transcript_normalized_before_t2s"] == TRAD
    assert metrics["traditional_chars_converted"] == 0
    assert metrics["t2s_dictionary_source"] is None
    assert metrics["t2s_mapping_entries"] is None
    assert metrics["edit_distance_ratio"] == pytest.approx(OLD_RATIO)


def test_metrics_ignore_english_and_numbers():
    metrics = scoring.text_error_metrics("Hello, World 2026!", "hello world 2026")
    assert metrics["edit_distance_ratio"] == pytest.approx(0.0)
    assert metrics["traditional_chars_converted"] == 0
    assert metrics["wer"] == pytest.approx(0.0)


# --------------------------------------------------------------------------- #
# 配置开关
# --------------------------------------------------------------------------- #

def test_config_defaults_enable_traditional_to_simplified():
    config = inference.load_config()
    assert config.traditional_to_simplified is True
    assert config.business_dict()["traditional_to_simplified"] is True


def test_config_override_can_disable_traditional_to_simplified():
    config = inference.load_config(overrides={"traditional_to_simplified": False})
    assert config.traditional_to_simplified is False


def test_config_rejects_non_boolean_traditional_to_simplified():
    with pytest.raises(ConfigError):
        inference.load_config(overrides={"traditional_to_simplified": "yes"})


# --------------------------------------------------------------------------- #
# 算子端到端：繁体转写从隔离变通过
# --------------------------------------------------------------------------- #

def _run(tmp_path, record, **overrides):
    entry = fixtures.build_input_tree(tmp_path / "in", [record],
                                      media=fixtures.default_media())
    return inference.run(str(entry / "demo.jsonl"), "task_traditional",
                         output_root=tmp_path / "out", allow_external_input=True,
                         overrides={"enabled": True, **overrides})


def _first_row(result):
    return json.loads(Path(result["outputFile"]).read_text(encoding="utf-8").splitlines()[0])


def test_traditional_transcript_passes_after_normalization(tmp_path):
    result = _run(tmp_path, fixtures.make_pair("pair-trad", transcript=TRAD,
                                               description=DESC))
    row = _first_row(result)
    node = row["processing"]["13"]
    assert result["statistics"]["passed_records"] == 1
    assert result["statistics"]["traditional_to_simplified"] is True
    assert node["passed"] is True
    assert node["reason"] == "passed"
    assert node["edit_distance_ratio"] == pytest.approx(NEW_RATIO)
    assert node["edit_distance_ratio_max"] == pytest.approx(0.30)
    assert node["transcript_normalized"] == SIMPLIFIED
    assert node["transcript_normalized_before_t2s"] == TRAD
    assert node["description_normalized_before_t2s"] == DESC
    assert node["traditional_chars_converted"] == 3
    assert node["t2s_dictionary_source"] == t2s.SOURCE_PACKAGE
    assert node["t2s_mapping_entries"] >= 1000
    assert node["traditional_to_simplified"] is True


def test_switch_off_keeps_traditional_transcript_quarantined(tmp_path):
    # 本用例验证的是「纯字符 CER 口径」下的繁简差异：同时关闭同音容错，否则
    # 繁简同音字（一/以、佔/占、局/据、場/场、立/力）会被同音容错豁免，测不到 T2S 开关本身。
    result = _run(tmp_path, fixtures.make_pair("pair-trad-off", transcript=TRAD,
                                               description=DESC),
                  traditional_to_simplified=False, homophone_tolerance=False)
    rows = fixtures.read_jsonl(result["sampleQuarantineFile"])
    assert rows, "关闭归一化后该记录应被隔离"
    node = rows[0]["audio_text"]
    assert node["passed"] is False
    assert node["reason"] == "edit_distance_exceeded"
    assert node["edit_distance_ratio"] == pytest.approx(OLD_RATIO)
    assert node["traditional_to_simplified"] is False
    assert node["transcript_normalized"] == TRAD
    assert node["traditional_chars_converted"] == 0
    assert node["t2s_dictionary_source"] is None
    assert node["edit_distance_ratio_homophone"] is None
    assert result["statistics"]["traditional_to_simplified"] is False


def test_homophone_tolerance_forgives_traditional_same_sound_chars(tmp_path):
    """同音容错口径：繁简同音字不再计入距离，只剩真正不同音的「讓/仍」。

    说明 T2S 与同音容错是两道独立的归一化：T2S 关闭时同音容错仍能豁免同音书写差异，
    但非同音的「讓→仍」仍需 T2S 才能消除（开启 T2S 时该记录 CER=4/15）。
    """
    result = _run(tmp_path, fixtures.make_pair("pair-trad-hom", transcript=TRAD,
                                               description=DESC),
                  traditional_to_simplified=False)
    row = _first_row(result)
    node = row["processing"]["13"]
    assert result["statistics"]["passed_records"] == 1
    assert node["passed"] is True
    assert node["reason"] == "passed_homophone_tolerant"
    assert node["homophone_tolerated"] is True
    assert node["edit_distance_ratio"] == pytest.approx(OLD_RATIO)
    assert node["edit_distance_ratio_homophone"] == pytest.approx(1.0 / 15.0, abs=1e-6)
    assert node["pinyin_dictionary_source"]
    assert node["pinyin_table_entries"] >= 1000
