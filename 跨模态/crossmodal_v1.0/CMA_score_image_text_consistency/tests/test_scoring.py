"""算子 #12 业务核心测试：阈值分档、配置定义域、字段抽取、台账与统计守恒。

本文件用测试替身 ``StubEncoder`` 精确控制余弦值，验证表1 #12 冻结口径：
<0.25 隔离 / 0.25–0.32 人工 / >=0.32 自动对齐；检测模型不改变分档。
"""
from __future__ import annotations

import json
import math
from pathlib import Path

import pytest

from conftest import StubEncoder, unit, vector_with_cosine, write_png
from utils import scoring


# --------------------------------------------------------------------- 余弦

def test_cosine_similarity_is_real_arithmetic():
    assert scoring.cosine_similarity([1.0, 0.0], [1.0, 0.0]) == pytest.approx(1.0)
    assert scoring.cosine_similarity([1.0, 0.0], [0.0, 1.0]) == pytest.approx(0.0)
    assert scoring.cosine_similarity([1.0, 0.0], [-1.0, 0.0]) == pytest.approx(-1.0)
    assert scoring.cosine_similarity([3.0, 4.0], [3.0, 4.0]) == pytest.approx(1.0)
    assert scoring.cosine_similarity([0.0, 0.0], [1.0, 0.0]) == 0.0


def test_cosine_similarity_rejects_shape_mismatch():
    with pytest.raises(ValueError):
        scoring.cosine_similarity([1.0, 0.0], [1.0, 0.0, 0.0])


# --------------------------------------------------------------------- 分档

@pytest.mark.parametrize("score,expected", [
    (0.0, scoring.TIER_ISOLATED),
    (0.2499, scoring.TIER_ISOLATED),
    (0.25, scoring.TIER_PENDING_MANUAL),
    (0.3199, scoring.TIER_PENDING_MANUAL),
    (0.32, scoring.TIER_AUTO_ALIGNED),
    (0.95, scoring.TIER_AUTO_ALIGNED),
    (-0.4, scoring.TIER_ISOLATED),
])
def test_bucket_tier_boundaries(score, expected):
    assert scoring.bucket_tier(score, 0.25, 0.32) == expected


def test_bucket_tier_respects_custom_thresholds():
    assert scoring.bucket_tier(0.30, 0.28, 0.36) == scoring.TIER_PENDING_MANUAL
    assert scoring.bucket_tier(0.27, 0.28, 0.36) == scoring.TIER_ISOLATED
    assert scoring.bucket_tier(0.40, 0.28, 0.36) == scoring.TIER_AUTO_ALIGNED


def test_tier_ledger_entries_match_table1():
    assert scoring.TIER_LABELS[scoring.TIER_ISOLATED] == "样本-语义不通"
    assert scoring.TIER_DESTINATIONS[scoring.TIER_ISOLATED] == "样本隔离库"
    assert scoring.TIER_LABELS[scoring.TIER_PENDING_MANUAL] == "样本-评分：待人工"
    assert scoring.TIER_DESTINATIONS[scoring.TIER_PENDING_MANUAL] == "人工队列"
    assert scoring.TIER_LABELS[scoring.TIER_AUTO_ALIGNED] == "样本-评分：通过"
    assert scoring.TIER_DESTINATIONS[scoring.TIER_AUTO_ALIGNED] == "样本池"


# --------------------------------------------------------------------- 配置

def test_default_config_matches_table1():
    cfg = scoring.validate_config({})
    section = cfg["image_text_scoring"]
    assert section["enabled"] is True
    # [2026-11-05 全量标定] 默认隔离阈值 0.20（见 14_16算子时序路线与阈值联动_修改说明_20261105.md）
    assert section["isolation_threshold"] == pytest.approx(0.20)
    assert section["auto_align_threshold"] == pytest.approx(0.32)
    assert section["use_detector_for_borderline"] is False
    assert cfg["model"]["model_name"] == "ViT-B-32"


@pytest.mark.parametrize("value", [0.19, 0.41, "0.25", True, None])
def test_isolation_threshold_out_of_range(value):
    with pytest.raises(scoring.ConfigError):
        scoring.validate_config({"image_text_scoring": {"isolation_threshold": value}})


@pytest.mark.parametrize("value", [0.24, 0.51, "0.32", False])
def test_auto_align_threshold_out_of_range(value):
    with pytest.raises(scoring.ConfigError):
        scoring.validate_config({"image_text_scoring": {"auto_align_threshold": value}})


def test_auto_align_threshold_must_exceed_isolation_threshold():
    with pytest.raises(scoring.ConfigError):
        scoring.validate_config({"image_text_scoring": {
            "isolation_threshold": 0.32, "auto_align_threshold": 0.32}})
    with pytest.raises(scoring.ConfigError):
        scoring.validate_config({"image_text_scoring": {
            "isolation_threshold": 0.30, "auto_align_threshold": 0.28}})


def test_detector_switch_must_be_bool():
    with pytest.raises(scoring.ConfigError):
        scoring.validate_config({"image_text_scoring": {"use_detector_for_borderline": "true"}})


def test_enabled_must_be_bool_but_can_be_false():
    with pytest.raises(scoring.ConfigError):
        scoring.validate_config({"image_text_scoring": {"enabled": "false"}})
    assert scoring.validate_config(
        {"image_text_scoring": {"enabled": False}})["image_text_scoring"]["enabled"] is False


def test_apply_overrides_allows_business_keys_only():
    cfg = scoring.validate_config({})
    merged = scoring.apply_overrides(cfg, {"isolation_threshold": 0.30,
                                           "auto_align_threshold": 0.40})
    assert merged["image_text_scoring"]["isolation_threshold"] == pytest.approx(0.30)
    assert merged["image_text_scoring"]["auto_align_threshold"] == pytest.approx(0.40)
    # 原始配置不被修改
    assert cfg["image_text_scoring"]["isolation_threshold"] == pytest.approx(0.20)


@pytest.mark.parametrize("overrides", [
    {"weights": "/tmp/evil.bin"},
    {"model": {"weights": "/tmp/evil.bin"}},
    {"output_root": "/"},
    {"operator": "hacked"},
    {"unknown_key": 1},
])
def test_apply_overrides_rejects_non_business_keys(overrides):
    with pytest.raises(scoring.ConfigError):
        scoring.apply_overrides(scoring.validate_config({}), overrides)


def test_config_must_be_object():
    with pytest.raises(scoring.ConfigError):
        scoring.validate_config([])  # type: ignore[arg-type]


# --------------------------------------------------------------------- 抽取

def test_extract_description_prefers_text_content():
    record = {"text": {"content": "一只金毛犬在接飞盘", "description_source": "caption"},
              "metadata": {"description": "别的描述", "description_source": "alt"}}
    result = scoring.extract_description(record)
    assert result["value"] == "一只金毛犬在接飞盘"
    assert result["source"] == "caption"


def test_extract_description_falls_back_to_metadata_and_processing():
    record = {"metadata": {"description": "来自 metadata 的描述"},
              "processing": {"09": {"description_source": "manual"}}}
    result = scoring.extract_description(record)
    assert result["value"] == "来自 metadata 的描述"
    assert result["source"] == "manual"


def test_extract_description_accepts_plain_string_text():
    assert scoring.extract_description({"text": "纯字符串文本"})["value"] == "纯字符串文本"


def test_extract_description_returns_empty_when_missing():
    assert scoring.extract_description({"metadata": {}})["value"] == ""


def test_image_reference_accepts_str_and_dict():
    assert scoring.image_reference({"image": "media/a.png"}) == "media/a.png"
    assert scoring.image_reference({"image": {"path": "media/b.png"}}) == "media/b.png"
    assert scoring.image_reference({"image": {"file": "media/c.png"}}) == "media/c.png"
    assert scoring.image_reference({"image": None}) is None
    assert scoring.image_reference({"image": {}}) is None


@pytest.mark.parametrize("value", [
    "/abs/path/a.png", "C:/win/a.png", "..\\escape.png", "media/../../escape.png",
    "http://example.com/a.png", "https://example.com/a.png", "\\\\server\\share\\a.png",
])
def test_safe_media_path_rejects_unsafe_values(value):
    with pytest.raises(ValueError):
        scoring.safe_media_path(value)


@pytest.mark.parametrize("value,expected", [
    ("media/a.png", "media/a.png"),
    ("./media/a.png", "media/a.png"),
    ("media\\a.png", "media/a.png"),
])
def test_safe_media_path_normalizes(value, expected):
    assert scoring.safe_media_path(value) == expected


# --------------------------------------------------------------- 配对取向

def _pair(left, right, modality_pair=None):
    return {
        "pair_id": "pair-0001",
        "group_id": "grp-src-A",
        "modality_pair": modality_pair or ["text", "image"],
        "evidence": {"type": "A", "criterion": "same_source_id", "evidence_source": "rule",
                     "value": {"source_id": "src-A"}},
        "evidence_priority": 1,
        "left": {"sample_id": left.get("sample_id", "sample-0001"),
                 "modalities": ["text"] if "text" in left else [], "record": left},
        "right": {"sample_id": right.get("sample_id", "sample-0002"),
                  "modalities": ["image"] if "image" in right else [], "record": right},
    }


def test_select_orientation_cross_record():
    pair = _pair({"sample_id": "s1", "text": {"content": "一只金毛犬"}},
                 {"sample_id": "s2", "image": {"path": "media/a.png"}})
    chosen = scoring.select_orientation(pair)
    assert chosen["ok"] is True
    assert chosen["orientation"] == "left_text_right_image"
    assert chosen["text_sample_id"] == "s1"
    assert chosen["image_sample_id"] == "s2"
    assert chosen["image_path"] == "media/a.png"


def test_select_orientation_reverse_direction():
    pair = _pair({"sample_id": "s1", "image": {"path": "media/a.png"}},
                 {"sample_id": "s2", "text": {"content": "一只金毛犬"}})
    chosen = scoring.select_orientation(pair)
    assert chosen["orientation"] == "right_text_left_image"
    assert chosen["text_sample_id"] == "s2"


def test_select_orientation_internal_fallback():
    pair = _pair({"sample_id": "s1", "text": {"content": "一只金毛犬"},
                  "image": {"path": "media/a.png"}},
                 {"sample_id": "s2", "audio": {"path": "media/b.wav"}})
    chosen = scoring.select_orientation(pair)
    assert chosen["orientation"] == "left_internal_text_image"
    assert chosen["text_sample_id"] == "s1"
    assert chosen["image_sample_id"] == "s1"


def test_select_orientation_reports_missing_modality():
    no_image = _pair({"sample_id": "s1", "text": {"content": "只有文本"}},
                     {"sample_id": "s2", "audio": {"path": "media/b.wav"}},
                     modality_pair=['text', 'audio'])
    assert scoring.select_orientation(no_image)["reason"] == "no_image"

    no_text = _pair({"sample_id": "s1", "image": {"path": "media/a.png"}},
                    {"sample_id": "s2", "audio": {"path": "media/b.wav"}})
    assert scoring.select_orientation(no_text)["reason"] == "no_description"


# ------------------------------------------------------------- plan_scoring

def _lines(rows, start=1):
    return [(index, row, None) for index, row in enumerate(rows, start)]


def _prepare_media(root: Path, names=("a.png", "b.png", "c.png", "d.png")) -> None:
    for name in names:
        write_png(root / "media" / name)


def _stub(text_a, text_b):
    return StubEncoder(
        text_vectors={text_a: [1.0, 0.0], text_b: [1.0, 0.0]},
        image_vectors={"a.png": [1.0, 0.0], "b.png": vector_with_cosine(0.25),
                       "c.png": unit([-1.0, 0.0]), "d.png": [0.0, 1.0]},
    )


TEXT_A = "一只金毛犬在草地上接飞盘"
TEXT_B = "红色方块放在桌面上"


def _run(tmp_path, rows, cfg=None, encoder=None, lines=None):
    media_root = tmp_path
    _prepare_media(media_root)
    validated = scoring.validate_config(cfg or {})
    return scoring.plan_scoring(
        lines if lines is not None else _lines(rows), validated,
        unique_id="unit_case", media_root=media_root,
        now="2026-10-06T10:00:00+08:00", encoder=encoder)


def test_plan_scoring_buckets_and_ledger(tmp_path):
    rows = [
        _pair({"sample_id": "s1", "text": {"content": TEXT_A}},
              {"sample_id": "s2", "image": {"path": "media/a.png"}}),
        _pair({"sample_id": "s3", "text": {"content": TEXT_B}},
              {"sample_id": "s4", "image": {"path": "media/b.png"}}),
        _pair({"sample_id": "s5", "text": {"content": TEXT_A}},
              {"sample_id": "s6", "image": {"path": "media/c.png"}}),
    ]
    rows[0]["pair_id"], rows[1]["pair_id"], rows[2]["pair_id"] = "p1", "p2", "p3"
    result = _run(tmp_path, rows, encoder=_stub(TEXT_A, TEXT_B))

    assert len(result.main) == 2
    assert len(result.isolation) == 1
    tiers = {item["pair_id"]: item["image_text_score"]["tier"] for item in result.main}
    assert tiers["p1"] == scoring.TIER_AUTO_ALIGNED
    assert tiers["p2"] == scoring.TIER_PENDING_MANUAL
    isolated = result.isolation[0]
    assert isolated["pair_id"] == "p3"
    assert isolated["label"] == "样本-语义不通"
    assert isolated["destination"] == "样本隔离库"
    assert isolated["evidence"]["clip_score"] == pytest.approx(-1.0)
    assert result.stats["conservation_ok"] is True


def test_plan_scoring_scores_are_real_cosine(tmp_path):
    rows = [_pair({"text": {"content": TEXT_A}}, {"image": {"path": "media/b.png"}})]
    rows[0]["pair_id"] = "p1"
    result = _run(tmp_path, rows, encoder=_stub(TEXT_A, TEXT_B))
    item = result.main[0]
    assert item["image_text_score"]["clip_score"] == pytest.approx(0.25, abs=1e-6)
    assert item["image_text_score"]["score_semantics"] == "cosine"
    assert item["image_text_score"]["model"]["degraded"] is False
    assert item["image_text_score"]["model"]["test_double"] is True


def test_plan_scoring_not_applicable_is_not_isolated(tmp_path):
    rows = [
        _pair({"sample_id": "s1", "text": {"content": TEXT_A}},
              {"sample_id": "s2", "audio": {"path": "media/x.wav"}},
              modality_pair=["text", "audio"]),
        _pair({"sample_id": "s3", "image": {"path": "media/a.png"}},
              {"sample_id": "s4", "audio": {"path": "media/x.wav"}},
              modality_pair=["image", "audio"]),
    ]
    rows[0]["pair_id"], rows[1]["pair_id"] = "p1", "p2"
    result = _run(tmp_path, rows, encoder=_stub(TEXT_A, TEXT_B))
    assert result.main == []
    assert result.isolation == []
    assert len(result.not_applicable) == 2
    reasons = {item["reason"] for item in result.not_applicable}
    assert reasons == {"no_image", "no_description"}
    assert all(item["destination"] != "样本隔离库" for item in result.not_applicable)
    assert result.stats["conservation_ok"] is True


def test_plan_scoring_media_problems_go_to_origin_quarantine(tmp_path):
    rows = [
        _pair({"text": {"content": TEXT_A}}, {"image": {"path": "media/missing.png"}}),
        _pair({"text": {"content": TEXT_A}}, {"image": {"path": "../escape.png"}}),
        _pair({"text": {"content": TEXT_A}}, {"image": {"path": "C:/abs.png"}}),
    ]
    for index, row in enumerate(rows, 1):
        row["pair_id"] = "p%d" % index
    result = _run(tmp_path, rows, encoder=_stub(TEXT_A, TEXT_B))
    assert result.main == []
    assert len(result.rejects) == 3
    assert all(item["destination"] == "原始隔离区" for item in result.rejects)
    labels = {item["reason"]: item["label"] for item in result.rejects}
    assert labels["media_missing"] == "原始文件-损坏"
    assert labels["media_path_invalid"] == "原始文件-无法解析"
    assert result.stats["conservation_ok"] is True


def test_plan_scoring_decoder_error_is_origin_quarantine(tmp_path):
    rows = [_pair({"text": {"content": TEXT_A}}, {"image": {"path": "media/d.png"}})]
    rows[0]["pair_id"] = "p1"
    encoder = StubEncoder(text_vectors={TEXT_A: [1.0, 0.0]},
                          image_errors={"d.png": "media_unreadable"})
    result = _run(tmp_path, rows, encoder=encoder)
    assert len(result.rejects) == 1
    assert result.rejects[0]["reason"] == "media_unreadable"
    assert result.rejects[0]["label"] == "原始文件-损坏"


def test_plan_scoring_bad_lines_and_structural_rejects(tmp_path):
    rows = [
        "{not json}",
        {"pair_id": "", "left": {}, "right": {}},
        _pair({"text": {"content": TEXT_A}}, {"image": {"path": "media/a.png"}}),
    ]
    rows[2]["pair_id"] = "p1"
    lines = [(1, None, "json_parse_error"), (2, rows[1], None), (3, rows[2], None)]
    result = _run(tmp_path, rows[:0], encoder=_stub(TEXT_A, TEXT_B), lines=lines)
    assert result.stats["bad_lines"] == 1
    assert result.stats["structural_rejects"] == 1
    assert len(result.main) == 1
    assert result.stats["conservation_ok"] is True


def test_plan_scoring_duplicate_pair_id_is_structural(tmp_path):
    pair = _pair({"text": {"content": TEXT_A}}, {"image": {"path": "media/a.png"}})
    pair["pair_id"] = "p1"
    duplicate = json.loads(json.dumps(pair))
    result = _run(tmp_path, [pair, duplicate], encoder=_stub(TEXT_A, TEXT_B))
    assert len(result.main) == 1
    assert result.stats["structural_rejects"] == 1
    assert result.rejects[0]["reason"] == "duplicate_pair_id"


def test_plan_scoring_degraded_produces_pending_not_scores(tmp_path):
    rows = [
        _pair({"text": {"content": TEXT_A}}, {"image": {"path": "media/a.png"}}),
        _pair({"text": {"content": TEXT_B}}, {"image": {"path": "media/b.png"}}),
    ]
    rows[0]["pair_id"], rows[1]["pair_id"] = "p1", "p2"
    result = _run(tmp_path, rows, encoder=None)
    assert result.main == []
    assert len(result.pending_model) == 2
    assert result.stats["degraded"] is True
    assert result.stats["degradation_reason"]
    for item in result.pending_model:
        assert item["image_text_score"]["clip_score"] is None
        # [v4.2] status 双字段：int status + process_status 字符串
        assert item["image_text_score"]["status"] == 1
        assert item["image_text_score"]["process_status"] == "pending_model"
        assert item["image_text_score"]["model"] is None
        assert item["destination"] != "样本隔离库"
    assert result.stats["conservation_ok"] is True


def test_plan_scoring_runtime_backend_failure_falls_back_to_pending(tmp_path):
    rows = [_pair({"text": {"content": TEXT_A}}, {"image": {"path": "media/a.png"}})]
    rows[0]["pair_id"] = "p1"
    encoder = StubEncoder(raise_on_call=True)
    result = _run(tmp_path, rows, encoder=encoder)
    assert result.main == []
    assert len(result.pending_model) == 1
    assert result.stats["runtime_degraded"] is True
    assert "stub_encoder_failure" in result.stats["degradation_reason"]


@pytest.mark.parametrize('image_record', [
    {'image': {'relative_path': 'media/a.png'}},
    {'images': [{'relative_path': 'media/a.png'}]},
    {'images': ['media/a.png']},
    {'image_path': 'media/a.png'},
    {'image_reference': {'relative_path': 'media/a.png'}},
    {'media': {'images': [{'relative_path': 'media/a.png'}]}},
    {'media': {'image': {'relative_path': 'media/a.png'}}},
])
@pytest.mark.parametrize('model_available', [False, True])
def test_native_image_aliases_score_or_stay_model_pending(tmp_path, image_record, model_available):
    pair = _pair({'text': {'content': TEXT_A}}, image_record)
    pair['pair_id'] = 'native-alias'
    result = _run(tmp_path, [pair], encoder=_stub(TEXT_A, TEXT_B) if model_available else None)
    assert result.not_applicable == []
    assert result.rejects == []
    records = result.main if model_available else result.pending_model
    assert len(records) == 1
    assert records[0]['right']['record'] == image_record
    assert records[0]['processing']['12'].get('route_enabled') is not False
    if model_available:
        assert records[0]['image_text_score']['clip_score'] == pytest.approx(1.0)
    else:
        assert records[0]['image_text_score']['process_status'] == 'pending_model'
        assert records[0]['image_text_score']['clip_score'] is None


@pytest.mark.parametrize('image_record', [
    {'image': {'unsupported_reference': 'media/a.png'}},
    {'images': [{'unsupported_reference': 'media/a.png'}]},
    {'modalities': ['image']},
])
def test_unresolved_declared_image_cannot_waive_enabled_route(tmp_path, image_record):
    pair = _pair({'text': {'content': TEXT_A}}, image_record)
    pair['pair_id'] = 'unresolved-image'
    result = _run(tmp_path, [pair], encoder=None)
    assert result.main == []
    assert result.rejects == []
    assert result.pipeline[0]['processing']['12']['reason'] == 'image_reference_unresolved'
    assert result.pipeline[0]['processing']['12']['route_enabled'] is True
    assert result.pipeline[0]['image_text_score']['clip_score'] is None


def test_plan_scoring_route_closed_passes_pairs_through(tmp_path):
    rows = [_pair({"text": {"content": TEXT_A}}, {"image": {"path": "media/a.png"}})]
    rows[0]["pair_id"] = "p1"
    result = _run(tmp_path, rows, cfg={"image_text_scoring": {"enabled": False}},
                  encoder=_stub(TEXT_A, TEXT_B))
    assert result.main == []
    assert len(result.not_applicable) == 1
    assert result.not_applicable[0]["reason"] == "route_closed"
    assert result.not_applicable[0]["label"] == "流程-路线关闭"
    assert result.not_applicable[0]["destination"] != "样本隔离库"
    assert result.stats["route_enabled"] is False


def test_plan_scoring_detector_not_requested_by_default(tmp_path):
    rows = [_pair({"text": {"content": TEXT_B}}, {"image": {"path": "media/b.png"}})]
    rows[0]["pair_id"] = "p1"
    result = _run(tmp_path, rows, encoder=_stub(TEXT_A, TEXT_B))
    detector = result.main[0]["image_text_score"]["detector"]
    assert detector["status"] == "not_requested"


def test_plan_scoring_detector_unavailable_does_not_change_tier(tmp_path):
    rows = [_pair({"text": {"content": TEXT_B}}, {"image": {"path": "media/b.png"}})]
    rows[0]["pair_id"] = "p1"
    result = _run(tmp_path, rows,
                  cfg={"image_text_scoring": {"use_detector_for_borderline": True}},
                  encoder=_stub(TEXT_A, TEXT_B))
    item = result.main[0]["image_text_score"]
    assert item["tier"] == scoring.TIER_PENDING_MANUAL
    assert item["detector"]["status"] == "unavailable"
    assert item["detector"]["tier_unchanged"] is True


def test_plan_scoring_detector_evidence_is_recorded_without_changing_tier(tmp_path):
    class StubDetector(object):
        name = "stub_detector"
        degraded = False

        def detect(self, path):
            return {"labels": ["dog", "frisbee"], "scores": [0.9, 0.8]}

    rows = [_pair({"text": {"content": TEXT_B}}, {"image": {"path": "media/b.png"}})]
    rows[0]["pair_id"] = "p1"
    result = _run(tmp_path, rows,
                  cfg={"image_text_scoring": {"use_detector_for_borderline": True}},
                  encoder=_stub(TEXT_A, TEXT_B))
    # 没有传入 detector 时只记录不可用；再显式传入 stub detector 验证证据写入
    result_with = scoring.plan_scoring(
        _lines(rows), scoring.validate_config(
            {"image_text_scoring": {"use_detector_for_borderline": True}}),
        unique_id="unit_case", media_root=tmp_path,
        now="2026-10-06T10:00:00+08:00", encoder=_stub(TEXT_A, TEXT_B),
        detector=StubDetector())
    detector = result_with.main[0]["image_text_score"]["detector"]
    assert detector["status"] == "applied"
    assert detector["labels"] == ["dog", "frisbee"]
    assert detector["tier_unchanged"] is True
    assert result_with.main[0]["image_text_score"]["tier"] == scoring.TIER_PENDING_MANUAL
    assert result.main[0]["image_text_score"]["detector"]["status"] == "unavailable"


def test_plan_scoring_keeps_upstream_fields_and_namespace(tmp_path):
    pair = _pair({"sample_id": "s1", "text": {"content": TEXT_A},
                  "metadata": {"image_tags": ["金毛犬"]}},
                 {"sample_id": "s2", "image": {"path": "media/a.png"}})
    pair["pair_id"] = "p1"
    pair["producer"] = {"operator": "generate_candidate_pairs", "operator_version": "v1.0.0"}
    result = _run(tmp_path, [pair], encoder=_stub(TEXT_A, TEXT_B))
    item = result.main[0]
    assert item["producer"]["operator"] == "generate_candidate_pairs"
    assert item["left"]["record"]["metadata"] == {"image_tags": ["金毛犬"]}
    assert item["text_reference"]["sample_id"] == "s1"
    assert item["image_reference"]["path"] == "media/a.png"
    assert item["scoring"]["operator"] == "#12"
    assert item["scoring"]["retry_count"] == 0


def test_plan_scoring_empty_input(tmp_path):
    result = _run(tmp_path, [], encoder=_stub(TEXT_A, TEXT_B))
    assert result.main == []
    assert result.stats["pairs_total"] == 0
    assert result.stats["conservation_ok"] is True


def test_statistics_counts_are_finite_numbers():
    stats = scoring.empty_stats()
    for key, value in stats.items():
        if isinstance(value, float):
            assert math.isfinite(value), key
