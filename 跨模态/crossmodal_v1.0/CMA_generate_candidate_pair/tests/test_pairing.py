"""算子 #11 业务核心测试：三级证据配对、边界阈值、配置校验、无相似度模型边界。"""
from __future__ import annotations

import copy
import sys
from pathlib import Path

import pytest
import yaml

BASE = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(BASE))

from utils import pairing  # noqa: E402
from utils.pairing import ConfigError  # noqa: E402


def default_cfg() -> dict:
    with open(BASE / "configs" / "config.yaml", "r", encoding="utf-8") as handle:
        return yaml.safe_load(handle)


def rec(sample_id, **kwargs):
    data = {"sample_id": sample_id, "text": None, "image": None, "video": None,
            "audio": None, "metadata": {}}
    data.update(kwargs)
    return data


def text_rec(sample_id, tokens, content=None, **kwargs):
    return rec(sample_id,
               text={"content": content or " ".join(tokens), "tokens": list(tokens)},
               **kwargs)


def test_cross_record_pair_has_native_sample_identity():
    result = pairing.plan_pairs([
        text_rec('t', ['dog'], source_id='same'),
        rec('v', video={'path': 'video.mp4'}, source_id='same')],
        default_cfg(), 'identity', '2026-11-11')
    assert len(result.pairs) == 1
    assert result.pairs[0]['sample_id'] == result.pairs[0]['pair_id']


# ---------------------------------------------------------------- 配置校验

def test_default_config_is_valid_and_matches_table1():
    cfg = pairing.validate_config(default_cfg())
    assert cfg["pairing"]["enabled"] is True
    assert cfg["pairing"]["priority"] == "source"
    assert cfg["pairing"]["keyword_min_overlap"] == 2


def test_config_cannot_be_disabled():
    cfg = default_cfg()
    cfg["pairing"]["enabled"] = False
    with pytest.raises(ConfigError, match="enabled"):
        pairing.validate_config(cfg)


@pytest.mark.parametrize("value", [1, 0, 6, 2.5, "2"])
def test_keyword_min_overlap_out_of_range(value):
    cfg = default_cfg()
    cfg["pairing"]["keyword_min_overlap"] = value
    with pytest.raises(ConfigError):
        pairing.validate_config(cfg)


@pytest.mark.parametrize("value", [2, 3, 5])
def test_keyword_min_overlap_in_range(value):
    cfg = default_cfg()
    cfg["pairing"]["keyword_min_overlap"] = value
    assert pairing.validate_config(cfg)["pairing"]["keyword_min_overlap"] == value


def test_bad_priority_rejected():
    cfg = default_cfg()
    cfg["pairing"]["priority"] = "clip_similarity"
    with pytest.raises(ConfigError, match="priority"):
        pairing.validate_config(cfg)


def test_negative_time_overlap_rejected():
    cfg = default_cfg()
    cfg["pairing"]["time_min_overlap_seconds"] = -0.5
    with pytest.raises(ConfigError):
        pairing.validate_config(cfg)


def test_group_limit_must_be_positive_int():
    cfg = default_cfg()
    cfg["pairing"]["max_pairs_per_group"] = 0
    with pytest.raises(ConfigError):
        pairing.validate_config(cfg)


def test_overrides_allow_business_keys_only():
    cfg = default_cfg()
    merged = pairing.apply_overrides(cfg, {"keyword_min_overlap": 3, "priority": "keywords"})
    assert merged["pairing"]["keyword_min_overlap"] == 3
    assert merged["pairing"]["priority"] == "keywords"

    for bad in ({"operator": "hacked"}, {"stage_id": "S9"}, {"io": {"output_root": "/tmp"}},
                {"unknown": 1}):
        with pytest.raises(ConfigError):
            pairing.apply_overrides(default_cfg(), bad)


def test_apply_overrides_does_not_mutate_source():
    cfg = default_cfg()
    before = copy.deepcopy(cfg)
    pairing.apply_overrides(cfg, {"keyword_min_overlap": 5})
    assert cfg == before


# ---------------------------------------------------------------- 字段解析

def test_is_present_and_modalities():
    assert pairing.is_present("a") is True
    assert pairing.is_present({"path": "a.png"}) is True
    assert pairing.is_present("") is False
    assert pairing.is_present("   ") is False
    assert pairing.is_present(None) is False
    assert pairing.is_present({}) is False
    assert pairing.is_present([]) is False

    record = rec("s1", text={"content": "x"}, image="a.png", audio={"path": "a.wav"})
    assert pairing.modalities_of(record) == ["text", "image", "audio"]


def test_source_and_scene_fallback_to_metadata():
    assert pairing.source_id_of(rec("s1", source_id="src-1")) == "src-1"
    assert pairing.source_id_of(rec("s1", metadata={"source_id": "src-2"})) == "src-2"
    assert pairing.source_id_of(rec("s1", metadata={"source": "src-3"})) == "src-3"
    assert pairing.source_id_of(rec("s1")) is None
    assert pairing.scene_id_of(rec("s1", scene_id="sc-1")) == "sc-1"
    assert pairing.scene_id_of(rec("s1", metadata={"scene_id": "sc-2"})) == "sc-2"
    assert pairing.scene_id_of(rec("s1", group_id="g-1")) == "g-1"
    assert pairing.scene_id_of(rec("s1")) is None


def test_time_range_from_text_index_and_audio_duration():
    assert pairing.time_range_of(rec("s1", text={"time_index": [0.5, 2.5]})) == (0.5, 2.5)
    assert pairing.time_range_of(rec("s1", audio={"duration": 2.0})) == (0.0, 2.0)
    assert pairing.time_range_of(
        rec("s1", audio={"segments": [{"start": 1.0, "end": 3.0}]})) == (1.0, 3.0)
    assert pairing.time_range_of(rec("s1", video={"duration": 4.0})) == (0.0, 4.0)
    assert pairing.time_range_of(rec("s1", video={"frames": [
        {"timestamp": 0.5}, {"timestamp": 3.5}]})) == (0.5, 3.5)
    assert pairing.time_range_of(rec("s1", text={"content": "无时间戳"})) is None
    # 时间范围无效（终点不大于起点）视为无时间证据
    assert pairing.time_range_of(rec("s1", text={"time_index": [2.0, 2.0]})) is None
    assert pairing.time_range_of(rec("s1", audio={"duration": 0})) is None


def test_overlap_seconds():
    assert pairing.overlap_seconds((0.0, 2.0), (1.0, 3.0)) == 1.0
    assert pairing.overlap_seconds((0.0, 2.0), (3.0, 4.0)) == 0.0
    assert pairing.overlap_seconds((0.0, 2.0), (2.0, 4.0)) == 0.0


def test_keyword_set_prefers_tokens_and_tags():
    record = rec("s1",
                 text={"content": "一只金毛犬", "tokens": ["金毛犬", "飞盘"]},
                 metadata={"image_tags": ["草地"]})
    kws = pairing.keyword_set(record)
    assert {"金毛犬", "飞盘", "草地"} <= kws


def test_keyword_set_fallback_uses_cjk_bigrams_when_no_tokens():
    left = rec("s1", text={"content": "金毛犬在草地上接飞盘"})
    right = rec("s2", metadata={"image_tags": ["金毛犬", "草地", "飞盘"]})
    shared = pairing.keyword_set(left) & pairing.keyword_set(right)
    assert len(shared) >= 2


def test_keyword_set_is_case_insensitive_for_latin():
    left = rec("s1", text={"content": "Golden Retriever"})
    right = rec("s2", text={"content": "golden retriever"})
    assert pairing.keyword_set(left) & pairing.keyword_set(right)


# ---------------------------------------------------------------- 三级证据

def test_tier_a_source_group_splits_one_to_many_sharing_group_id():
    cfg = pairing.validate_config(default_cfg())
    records = [rec("s1", source_id="src-A", text={"content": "a"}),
               rec("s2", source_id="src-A", image="b.png"),
               rec("s3", source_id="src-A", image="d.png")]
    result = pairing.plan_pairs(records, cfg, unique_id="u_f1", now="2026-10-06T00:00:00+08:00")
    pairs = result.pairs
    assert len(pairs) == 2
    assert {p["group_id"] for p in pairs} == {"grp-src-src-A"}
    assert all(p["evidence"]["type"] == "A" for p in pairs)
    assert all(p["evidence"]["criterion"] == "same_source_id" for p in pairs)
    assert all(p["evidence"]["value"]["source_id"] == "src-A" for p in pairs)
    assert result.stats["paired_records"] == 3
    assert result.stats["unpaired_records"] == 0
    assert result.stats["groups_total"] == 1


def test_tier_a_scene_match_when_source_absent():
    cfg = pairing.validate_config(default_cfg())
    records = [rec("s1", scene_id="outdoor", text={"content": "a"}),
               rec("s2", scene_id="outdoor", image="b.png")]
    result = pairing.plan_pairs(records, cfg, unique_id="u_f1", now="t")
    assert len(result.pairs) == 1
    evidence = result.pairs[0]["evidence"]
    assert evidence["type"] == "A"
    assert evidence["criterion"] == "same_scene_id"
    assert evidence["value"]["scene_id"] == "outdoor"


def test_tier_b_uses_time_overlap_when_source_differs():
    """来源不同但同批次：B 级按时间重叠配对（共享上下文防护命中 batch_id）。"""
    cfg = pairing.validate_config(default_cfg())
    records = [rec("s1", source_id="a", batch_id="batch-1", text={"time_index": [0.0, 2.0]},
                   audio={"duration": 2.0}),
               rec("s2", source_id="b", batch_id="batch-1", text={"time_index": [1.0, 3.0]},
                   image="b.png")]
    result = pairing.plan_pairs(records, cfg, unique_id="u_f1", now="t")
    assert len(result.pairs) == 1
    evidence = result.pairs[0]["evidence"]
    assert evidence["type"] == "B"
    assert evidence["criterion"] == "time_overlap"
    assert evidence["value"]["overlap_seconds"] == 1.0
    assert evidence["value"]["shared_context"] == "batch_id"


def test_tier_b_cross_source_overlap_blocked_by_guard():
    """round9 复核风险：跨来源素材时间轴都从 0 秒开始必然重叠，但不得误配。"""
    cfg = pairing.validate_config(default_cfg())
    records = [rec("s1", source_id="src-movie", batch_id="batch-A",
                   text={"time_index": [0.0, 5.0]}, video={"duration": 5.0}),
               rec("s2", source_id="src-news", batch_id="batch-B",
                   text={"time_index": [0.0, 5.0]}, audio={"duration": 5.0})]
    result = pairing.plan_pairs(records, cfg, unique_id="u_f1", now="t")
    assert result.pairs == []
    assert result.stats["unpaired_records"] == 2
    assert {item["reason"] for item in result.unpaired} == {"no_candidate"}


def test_tier_b_guard_can_be_disabled_explicitly():
    cfg = pairing.validate_config(default_cfg())
    cfg["pairing"]["time_overlap_require_shared_context"] = False
    records = [rec("s1", source_id="src-movie", batch_id="batch-A",
                   text={"time_index": [0.0, 5.0]}, video={"duration": 5.0}),
               rec("s2", source_id="src-news", batch_id="batch-B",
                   text={"time_index": [0.0, 5.0]}, audio={"duration": 5.0})]
    result = pairing.plan_pairs(records, cfg, unique_id="u_f1", now="t")
    assert len(result.pairs) == 1
    assert result.pairs[0]["evidence"]["type"] == "B"


def test_tier_b_scene_match_takes_tier_a_precedence():
    """场景相同会先命中 A 级（配上即停）；B 级防护的实用上下文是 batch_id。"""
    cfg = pairing.validate_config(default_cfg())
    records = [rec("s1", scene_id="scene-9", text={"time_index": [0.0, 2.0]},
                   video={"duration": 2.0}),
               rec("s2", scene_id="scene-9", text={"time_index": [1.0, 3.0]},
                   audio={"duration": 3.0})]
    result = pairing.plan_pairs(records, cfg, unique_id="u_f1", now="t")
    assert len(result.pairs) == 1
    evidence = result.pairs[0]["evidence"]
    assert evidence["type"] == "A"
    assert evidence["criterion"] == "same_scene_id"


def test_default_config_enables_shared_context_guard():
    cfg = pairing.validate_config(default_cfg())
    assert cfg["pairing"]["time_overlap_require_shared_context"] is True


def test_shared_context_guard_must_be_bool():
    cfg = default_cfg()
    cfg["pairing"]["time_overlap_require_shared_context"] = "yes"
    with pytest.raises(ConfigError):
        pairing.validate_config(cfg)


def test_tier_b_requires_both_sides_timed():
    cfg = pairing.validate_config(default_cfg())
    records = [rec("s1", source_id="a", text={"content": "无时间"}), rec("s2", source_id="b", image="b.png")]
    result = pairing.plan_pairs(records, cfg, unique_id="u_f1", now="t")
    assert result.pairs == []
    assert result.stats["unpaired_records"] == 2


def test_tier_b_respects_min_overlap_seconds():
    cfg = pairing.validate_config(default_cfg())
    cfg["pairing"]["time_min_overlap_seconds"] = 1.5
    records = [rec("s1", source_id="a", batch_id="batch-1",
                   text={"content": "甲", "time_index": [0.0, 2.0]}),
               rec("s2", source_id="b", batch_id="batch-1", image="b.png",
                   audio={"segments": [{"start": 1.0, "end": 3.0}]})]
    result = pairing.plan_pairs(records, cfg, unique_id="u_f1", now="t")
    assert result.pairs == []


def test_tier_c_keyword_threshold_boundary():
    cfg = pairing.validate_config(default_cfg())
    records = [text_rec("s1", ["汽车", "街道"], image="a.png"),
               text_rec("s2", ["汽车", "街道", "红绿灯"], image="b.png"),
               text_rec("s3", ["汽车", "维护"], image="c.png")]
    result = pairing.plan_pairs(records, cfg, unique_id="u_f1", now="t")
    assert len(result.pairs) == 1
    pair = result.pairs[0]
    assert pair["evidence"]["type"] == "C"
    assert pair["evidence"]["criterion"] == "shared_keywords"
    assert sorted(pair["evidence"]["value"]["matched"]) == ["汽车", "街道"]
    assert pair["evidence"]["value"]["matched_count"] == 2
    assert pair["evidence"]["value"]["min_overlap"] == 2
    assert pair["evidence"]["value"]["tokenizer"] == "upstream_tokens+cjk_bigram_rule"
    assert pair["evidence"]["evidence_source"] == "rule"
    assert [u["sample_id"] for u in result.unpaired] == ["s3"]


def test_tier_c_threshold_configurable():
    cfg = pairing.validate_config(default_cfg())
    cfg["pairing"]["keyword_min_overlap"] = 3
    records = [text_rec("s1", ["汽车", "街道"], image="a.png"),
               text_rec("s2", ["汽车", "街道"], image="b.png")]
    result = pairing.plan_pairs(records, cfg, unique_id="u_f1", now="t")
    assert result.pairs == []
    assert result.stats["unpaired_records"] == 2


def test_priority_keywords_skips_source_tier():
    cfg = pairing.validate_config(default_cfg())
    cfg["pairing"]["priority"] = "keywords"
    records = [text_rec("s1", ["甲", "乙"], source_id="same"),
               rec("s2", source_id="same", image="b.png", metadata={"tags": ["丙", "丁"]})]
    result = pairing.plan_pairs(records, cfg, unique_id="u_f1", now="t")
    assert result.pairs == []
    assert result.stats["unpaired_records"] == 2


def test_priority_time_overlap_skips_source_tier():
    cfg = pairing.validate_config(default_cfg())
    cfg["pairing"]["priority"] = "time_overlap"
    records = [rec("s1", source_id="same", text={"content": "a"}), rec("s2", source_id="same", image="b.png")]
    result = pairing.plan_pairs(records, cfg, unique_id="u_f1", now="t")
    assert result.pairs == []


def test_same_modality_pair_is_skipped():
    cfg = pairing.validate_config(default_cfg())
    records = [text_rec("s1", ["甲"], source_id="same"),
               text_rec("s2", ["乙"], source_id="same")]
    result = pairing.plan_pairs(records, cfg, unique_id="u_f1", now="t")
    assert result.pairs == []
    assert result.stats["same_modality_pairs_skipped"] == 1
    assert result.stats["unpaired_records"] == 2


def test_group_pair_limit_records_excess():
    cfg = pairing.validate_config(default_cfg())
    cfg["pairing"]["max_pairs_per_group"] = 1
    records = [rec("s1", source_id="src-A", text={"content": "a"}),
               rec("s2", source_id="src-A", image="b.png"),
               rec("s3", source_id="src-A", image="c.png")]
    result = pairing.plan_pairs(records, cfg, unique_id="u_f1", now="t")
    assert result.stats["pairs_total"] == 1
    assert result.stats["group_pair_limit_hits"] == 1
    reasons = [item["reason"] for item in result.rejects]
    assert "group_pair_limit_exceeded" in reasons
    assert result.stats["unpaired_records"] == 1


def test_pair_payload_shape_and_uniqueness():
    cfg = pairing.validate_config(default_cfg())
    records = [rec("s1", source_id="src-A", text={"content": "a"}),
               rec("s2", source_id="src-A", image="b.png")]
    result = pairing.plan_pairs(records, cfg, unique_id="u_f1", now="t")
    pair = result.pairs[0]
    assert set(pair) >= {"pair_id", "group_id", "modality_pair", "evidence",
                         "evidence_priority", "left", "right", "producer"}
    assert pair["left"]["sample_id"] == "s1"
    assert pair["right"]["sample_id"] == "s2"
    assert pair["left"]["record"]["sample_id"] == "s1"
    assert pair["right"]["record"]["sample_id"] == "s2"
    assert sorted(pair["modality_pair"]) == ["image", "text"]
    assert pair["producer"]["operator"] == "generate_candidate_pairs"
    assert pair["producer"]["uniqueId"] == "u_f1"
    assert pair["evidence_priority"] == 1


def test_paired_records_preserve_original_fields():
    cfg = pairing.validate_config(default_cfg())
    records = [rec("s1", source_id="src-A", text={"content": "a"}, image="a.png",
                   processing={"10": {"status": "success"}}, metadata={"keep": 1}),
               rec("s2", source_id="src-A", text={"content": "b"}, image="b.png")]
    result = pairing.plan_pairs(records, cfg, unique_id="u_f1", now="t")
    left_record = result.pairs[0]["left"]["record"]
    assert left_record["processing"] == {"10": {"status": "success"}}
    assert left_record["metadata"] == {"keep": 1}


def test_evidence_types_are_only_a_b_c_and_no_similarity_score():
    cfg = pairing.validate_config(default_cfg())
    records = [rec("s1", source_id="src-A", text={"content": "a"}, image="a.png"),
               rec("s2", source_id="src-A", text={"content": "b"}, image="b.png")]
    result = pairing.plan_pairs(records, cfg, unique_id="u_f1", now="t")
    pair = result.pairs[0]
    assert pair["evidence"]["type"] in {"A", "B", "C"}
    flat = repr(pair)
    for banned in ("clip", "cosine", "similarity", "wav2vec", "score"):
        assert banned not in flat.lower()


def test_every_evidence_declares_rule_source_and_valid_priority():
    cfg = pairing.validate_config(default_cfg())
    records = [rec("s1", source_id="src-A", text={"content": "a"}, image="a.png"),
               rec("s2", source_id="src-A", image="b.png"),
               text_rec("s3", ["甲", "乙"], image="c.png"),
               text_rec("s4", ["甲", "乙"], image="d.png")]
    result = pairing.plan_pairs(records, cfg, unique_id="u_f1", now="t")
    assert result.pairs
    for pair in result.pairs:
        evidence = pair["evidence"]
        assert set(evidence) == {"type", "criterion", "evidence_source", "value"}
        assert evidence["evidence_source"] == "rule"
        assert pair["evidence_priority"] == pairing.EVIDENCE_PRIORITY[evidence["type"]]


def test_business_module_has_no_similarity_model_dependency():
    source = (BASE / "utils" / "pairing.py").read_text(encoding="utf-8").lower()
    for banned in ("import clip", "from clip", "transformers", "torch", "wav2vec",
                   "open_clip", "sentence_transformers"):
        assert banned not in source


def test_deterministic_output_for_same_input():
    cfg = pairing.validate_config(default_cfg())
    records = [rec("s1", source_id="src-A", text={"content": "a"}),
               rec("s2", source_id="src-A", image="b.png"),
               text_rec("s3", ["甲", "乙"], image="c.png"),
               text_rec("s4", ["甲", "乙"], image="d.png")]
    first = pairing.plan_pairs(records, cfg, unique_id="u_f1", now="t")
    second = pairing.plan_pairs(copy.deepcopy(records), cfg, unique_id="u_f1", now="t")
    assert [p["pair_id"] for p in first.pairs] == [p["pair_id"] for p in second.pairs]
    assert [p["group_id"] for p in first.pairs] == [p["group_id"] for p in second.pairs]


def test_unpaired_payload_keeps_record_and_reason():
    cfg = pairing.validate_config(default_cfg())
    records = [text_rec("s1", ["独立", "样本"], image="a.png")]
    result = pairing.plan_pairs(records, cfg, unique_id="u_f1", now="t")
    item = result.unpaired[0]
    assert item["sample_id"] == "s1"
    assert item["reason"] == "no_candidate"
    assert item["tried_evidence"] == ["A", "B", "C"]
    assert item["record"]["text"]["tokens"] == ["独立", "样本"]


@pytest.mark.parametrize("media", ["image", "audio", "video"])
def test_in_record_pair_preserves_source_and_native_payload_without_mutation(media):
    record = rec("原始/a b", source_id="real-source", batch_id="batch", license="train",
                 text={"content": "a valid caption"},
                 transcription={"paragraphs": [{"segment_path": "segments/a.wav"}]},
                 metadata={"keep": {"nested": True}}, admission={"passed": True})
    record[media] = {"path": "media/asset.bin", "frames": [{"path": "frames/1.jpg"}],
                     "segments": [{"frames": [{"path": "frames/1.jpg"}]}]}
    before = copy.deepcopy(record)
    result = pairing.plan_pairs([record], default_cfg(), unique_id="u", now="t")
    assert len(result.pairs) == 1
    pair = result.pairs[0]
    assert pair["modality_pair"] == ["text", media]
    assert pair["origin_sample_id"] == record["sample_id"]
    assert pair["sample_id"] == pair["pair_id"]
    assert pair["evidence"] == {
        "type": "A", "criterion": "same_record_source_id", "evidence_source": "rule",
        "value": {"source_id": "real-source", "origin_sample_id": record["sample_id"],
                  "media_modality": media},
    }
    assert pair["left"]["sample_id"] != pair["right"]["sample_id"]
    for side, modality in (("left", "text"), ("right", media)):
        view = pair[side]["record"]
        assert view["sample_id"] == pair[side]["sample_id"]
        assert view["origin_sample_id"] == record["sample_id"]
        assert pair[side]["origin_sample_id"] == record["sample_id"]
        assert pair[side]["modalities"] == [modality]
        assert view[modality] == record[modality]
        assert all(view[other] is None for other in pairing.MODALITY_ORDER if other != modality)
        for key in ("source_id", "batch_id", "license", "transcription", "metadata", "admission"):
            assert view[key] == record[key]
        assert not any(char in view["sample_id"] for char in '/\\ :')
    assert result.unpaired == []
    assert result.stats["valid_records"] == result.stats["paired_records"] == 1
    assert result.stats["derived_views"] == 2
    assert record == before
    pair["left"]["record"]["metadata"]["keep"]["nested"] = False
    assert record == before


def test_in_record_multiroute_precedes_cross_record_rules_and_counts_originals():
    records = [rec("complete", source_id="same", text={"content": "caption"},
                   image="a.png", audio="a.wav", video="a.mp4"),
               rec("external", source_id="same", image="external.png")]
    cfg = default_cfg()
    cfg["pairing"]["priority"] = "keywords"
    cfg["pairing"]["max_pairs_per_group"] = 1
    first = pairing.plan_pairs(records, cfg, unique_id="u", now="t")
    second = pairing.plan_pairs(copy.deepcopy(records), cfg, unique_id="u", now="t")
    assert first.pairs == second.pairs
    assert len(first.pairs) == 1
    assert first.pairs[0]["modality_pair"] == ["text", "image"]
    assert all(pair["origin_sample_id"] == "complete" for pair in first.pairs)
    assert [item["sample_id"] for item in first.unpaired] == ["external"]
    assert first.stats["valid_records"] == 2
    assert first.stats["paired_records"] == 1
    assert first.stats["derived_views"] == 2
    assert first.stats["in_record_pairs"] == 1
    assert first.stats["pairs_total"] == 1
    assert first.stats["pairs_by_evidence"] == {"A": 1, "B": 0, "C": 0}
    assert first.stats["group_pair_limit_hits"] == 2
    assert len(first.rejects) == 2
    assert [item["modality_pair"] for item in first.rejects] == [["text", "video"],
                                                               ["text", "audio"]]
    for item in first.rejects:
        assert item["sample_id"] == "complete"
        assert item["peer_sample_id"].endswith(("-audio", "-video"))
        assert item["reason"] == "group_pair_limit_exceeded"
        assert item["group_level"] == "A"
        assert item["detectedAt"] == "t"


def test_in_record_limit_is_per_original_seed_even_when_source_is_shared():
    records = [rec(sample_id, source_id="shared", text={"content": "caption"},
                   image="a.png", video="a.mp4") for sample_id in ("one", "two")]
    cfg = default_cfg()
    cfg["pairing"]["max_pairs_per_group"] = 1
    result = pairing.plan_pairs(records, cfg, unique_id="u", now="t")
    assert [pair["origin_sample_id"] for pair in result.pairs] == ["one", "two"]
    assert len(result.rejects) == result.stats["group_pair_limit_hits"] == 2
    assert result.stats["paired_records"] == 2
    assert result.stats["derived_views"] == 4
    assert result.stats["in_record_pairs"] == 2
    assert result.unpaired == []


@pytest.mark.parametrize("text,source_id", [({"content": " "}, "source"),
                                           ({"time_index": [0, 2]}, "source"),
                                           ({"content": "caption"}, None)])
def test_in_record_pair_requires_valid_text_and_real_source(text, source_id):
    result = pairing.plan_pairs([rec("s", text=text, source_id=source_id, image="i.png")],
                                default_cfg(), unique_id="u", now="t")
    assert result.pairs == []
    assert result.stats["unpaired_records"] == 1
