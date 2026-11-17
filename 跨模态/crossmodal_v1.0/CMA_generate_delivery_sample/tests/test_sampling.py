"""业务规则层测试：路线归一、描述/标注/评分证据抽取、隔离与交付标记、守恒。"""
from __future__ import annotations

import pytest

from conftest import annotation_block, isolation_marker, sample_record, score_block
from utils import sampling


# --------------------------------------------------------------------------- #
# 路线归一与模态
# --------------------------------------------------------------------------- #

@pytest.mark.parametrize("raw,expected", [
    ("image_text", "image_text"),
    ("image+text", "image_text"),
    ("image-text", "image_text"),
    ("IMAGE_TEXT", "image_text"),
    ("audio_text", "audio_text"),
    ("audio+text", "audio_text"),
    ("video_text", "video_text"),
    ("video+text", "video_text"),
    ("image_text ", "image_text"),
    ("local_annotation", None),
    ("", None),
    (None, None),
    (12, None),
])
def test_normalize_route(raw, expected):
    assert sampling.normalize_route(raw) == expected


def test_extract_routes_orders_and_dedups():
    payload = {"routes": ["video_text", "image_text", "image_text"]}
    assert sampling.extract_routes(payload) == ["image_text", "video_text"]


def test_extract_routes_from_processing_and_operator_namespace():
    payload = {"processing": {"22": {"route": "audio_text"}}}
    assert sampling.extract_routes(payload) == ["audio_text"]
    payload = {"operator_22": {"routes": ["video_text"]}}
    assert sampling.extract_routes(payload) == ["video_text"]


def test_extract_routes_derives_from_modalities():
    payload = {"modalities": ["image", "text"]}
    assert sampling.extract_routes(payload) == ["image_text"]
    payload = {"modalities": ["audio", "video", "text"]}
    assert sampling.extract_routes(payload) == ["audio_text", "video_text"]


def test_extract_routes_unknown_is_empty():
    assert sampling.extract_routes({"modalities": ["text"]}) == []
    assert sampling.extract_routes({}) == []


def test_extract_modalities_declared_then_derived():
    assert sampling.extract_modalities({"modalities": ["image", "text"]}) == \
        ["image", "text"]
    assert sampling.extract_modalities({"image": {"path": "a.png"},
                                        "text": {"content": "x"}}) == ["image", "text"]
    assert sampling.extract_modalities({}) == []


def test_route_label_and_modal_label():
    assert sampling.route_modal_label("image_text") == "图文"
    assert sampling.route_modal_label("audio_text") == "音文"
    assert sampling.route_modal_label("video_text") == "视文"


# --------------------------------------------------------------------------- #
# 描述摘要
# --------------------------------------------------------------------------- #

def test_extract_description_priority_and_field():
    payload = {"description": "顶层描述", "text": {"content": "文本内容"}}
    spec = sampling.extract_description(payload)
    assert spec["text"] == "顶层描述"
    assert spec["field"] == "description"


def test_extract_description_falls_back_to_text_content():
    spec = sampling.extract_description({"text": {"content": "文本内容",
                                                  "description_source": "caption"}})
    assert spec["text"] == "文本内容"
    assert spec["field"] == "text.content"
    assert spec["source"] == "caption"


def test_extract_description_missing_returns_none():
    spec = sampling.extract_description({})
    assert spec["text"] is None
    assert spec["field"] is None


def test_summarize_description_truncates_and_counts():
    text = "甲" * 200
    summary = sampling.summarize_description(text, max_chars=50)
    assert summary["text"] == "甲" * 50 + "…"
    assert summary["truncated"] is True
    assert summary["chars"] == 200          # 上游真实字符数
    assert summary["stored_chars"] == 51    # 摘要实际写入单元格的字符数
    assert summary["sha256"] == sampling.summarize_description(text, 999)["sha256"]


def test_summarize_description_short_text_is_kept_verbatim():
    summary = sampling.summarize_description("短描述", max_chars=50)
    assert summary["text"] == "短描述"
    assert summary["truncated"] is False
    assert summary["chars"] == 3
    assert summary["stored_chars"] == 3


def test_summarize_description_none_is_empty():
    summary = sampling.summarize_description(None, max_chars=50)
    assert summary["text"] is None
    assert summary["chars"] == 0
    assert summary["stored_chars"] == 0
    assert summary["sha256"] is None


# --------------------------------------------------------------------------- #
# 标注结论
# --------------------------------------------------------------------------- #

def test_extract_annotation_from_processing_16():
    payload = {"processing": {"16": annotation_block(conclusion="对齐",
                                                     human_verified=False)}}
    spec = sampling.extract_annotation(payload)
    assert spec["conclusion"] == "对齐"
    assert spec["human_verified"] is False
    assert spec["source"] == "processing.16"


def test_extract_annotation_from_alignment_whole():
    payload = {"alignment": {"whole": {"decision": "aligned",
                                       "confirmation_method": "human",
                                       "human_verified": True}}}
    spec = sampling.extract_annotation(payload)
    assert spec["conclusion"] == "aligned"
    assert spec["human_verified"] is True
    assert spec["source"] == "alignment.whole"


def test_extract_annotation_from_operator_namespace():
    payload = {"operator_16": {"decision": "not_aligned", "status": "isolated",
                               "human_verified": False}}
    spec = sampling.extract_annotation(payload)
    assert spec["conclusion"] == "not_aligned"
    assert spec["source"] == "operator_16"


def test_extract_annotation_missing_is_none():
    spec = sampling.extract_annotation({"sample_id": "x"})
    assert spec["conclusion"] is None
    assert spec["source"] is None


def test_format_annotation_never_fabricates():
    assert sampling.format_annotation({"conclusion": None, "human_verified": False}) == \
        "未提供"
    human = sampling.format_annotation({"conclusion": "对齐", "human_verified": True})
    assert human.startswith("人工确认：") and "对齐" in human
    auto = sampling.format_annotation({"conclusion": "对齐", "human_verified": False})
    assert auto.startswith("对齐") and "人工确认：" not in auto
    assert "自动" in auto


# --------------------------------------------------------------------------- #
# 评分结论
# --------------------------------------------------------------------------- #

def test_extract_score_prefers_route_operator():
    payload = {"processing": {"12": score_block("image_text", value=0.41)}}
    spec = sampling.extract_score(payload, "image_text")
    assert spec["value"] == 0.41
    assert spec["tier"] == "auto_aligned"
    assert spec["source"] == "processing.12"


def test_extract_score_audio_route_uses_13():
    payload = {"processing": {"13": score_block("audio_text", value=0.26,
                                                operator_number=13)}}
    spec = sampling.extract_score(payload, "audio_text")
    assert spec["value"] == 0.26
    assert spec["source"] == "processing.13"


def test_extract_score_ignores_other_route_block():
    payload = {"processing": {"13": score_block("audio_text", operator_number=13)}}
    spec = sampling.extract_score(payload, "video_text")
    assert spec["value"] is None
    assert spec["source"] is None


def test_extract_score_falls_back_to_route_specific_key():
    payload = {"image_text_score": {"clip_score": 0.33, "tier": "auto_aligned"}}
    spec = sampling.extract_score(payload, "image_text")
    assert spec["value"] == 0.33
    assert spec["source"] == "image_text_score"


def test_extract_score_accepts_numeral_strings_but_rejects_bad_values():
    payload = {"processing": {"12": {"score": "0.31", "tier": "auto_aligned"}}}
    assert sampling.extract_score(payload, "image_text")["value"] == 0.31
    payload = {"processing": {"12": {"score": "高分", "tier": "auto_aligned"}}}
    spec = sampling.extract_score(payload, "image_text")
    assert spec["value"] is None
    assert spec["tier"] == "auto_aligned"


def test_format_score_never_fabricates_numbers():
    assert sampling.format_score({"value": None, "tier": None, "label": None,
                                  "model": None}) == "未提供"
    text = sampling.format_score({"value": 0.28, "tier": "pending_manual",
                                  "label": "样本-评分：待人工", "model": "CLIP-ViT/B-32"})
    assert "0.280" in text and "待人工" in text and "CLIP-ViT/B-32" in text


def test_format_score_without_value_keeps_real_label_only():
    text = sampling.format_score({"value": None, "tier": "pending_manual",
                                  "label": None, "model": None})
    assert "0.000" not in text
    assert "pending_manual" in text


# --------------------------------------------------------------------------- #
# 隔离与交付标记
# --------------------------------------------------------------------------- #

def test_find_isolation_from_marker_and_list():
    payload = {"sample_id": "cm-9911", "processing": {"21": isolation_marker("cm-9911")}}
    verdict = sampling.find_isolation(payload, set())
    assert verdict and verdict["reason_code"] == "upstream_isolated"
    assert sampling.find_isolation({"sample_id": "cm-9911"}, {"cm-9911"})["source"] == \
        "isolation_list"
    assert sampling.find_isolation({"sample_id": "ok"}, set()) is None


def test_find_isolation_from_status_string_and_boolean():
    assert sampling.find_isolation({"sample_status": "quarantined"}, set())
    assert sampling.find_isolation({"delivery_status": "isolated"}, set())
    assert sampling.find_isolation({"isolation": True}, set())
    assert sampling.find_isolation({"isolation": False}, set()) is None


def test_delivery_marker_reads_operator_23_and_processing():
    assert sampling.delivery_marker({"operator_23": {"decision": "deliverable"}}) == \
        "deliverable"
    assert sampling.delivery_marker({"processing": {"23": {"decision": "pending_release"}}}) \
        == "pending_release"
    assert sampling.delivery_marker({}) is None


# --------------------------------------------------------------------------- #
# resolve_record
# --------------------------------------------------------------------------- #

def test_resolve_record_collects_all_evidence():
    payload = sample_record("cm-0001", route="image_text",
                            description="一只橘猫坐在窗台上" * 20,
                            annotation=annotation_block(conclusion="对齐"),
                            score=score_block("image_text", value=0.41))
    spec = sampling.resolve_record(payload, enabled_routes=["image_text"],
                                   isolation_ids=set(), description_chars=40)
    assert spec["identifier"] == "cm-0001"
    assert spec["route"] == "image_text"
    assert spec["routes"] == ["image_text"]
    assert spec["modalities"] == ["image", "text"]
    assert spec["description"]["truncated"] is True
    assert spec["annotation"]["conclusion"] == "对齐"
    assert spec["score"]["value"] == 0.41
    assert spec["delivery"] == "deliverable"
    assert spec["isolation"] is None
    assert spec["unsafe_refs"] == []


def test_resolve_record_flags_unsafe_media_reference():
    payload = sample_record("cm-9913", media=["C:/temp/escape.png"])
    spec = sampling.resolve_record(payload, enabled_routes=["image_text"],
                                   isolation_ids=set(), description_chars=40)
    assert spec["unsafe_refs"] == ["C:/temp/escape.png"]


def test_resolve_record_flags_unsafe_relative_traversal():
    payload = sample_record("cm-9914", media=["../escape.png"])
    spec = sampling.resolve_record(payload, enabled_routes=["image_text"],
                                   isolation_ids=set(), description_chars=40)
    assert spec["unsafe_refs"] == ["../escape.png"]


def test_resolve_record_primary_route_is_first_enabled():
    payload = sample_record("cm-0002", route="video_text",
                            modalities=["image", "video", "text"])
    payload["routes"] = ["image_text", "video_text"]
    spec = sampling.resolve_record(payload, enabled_routes=["video_text"],
                                   isolation_ids=set(), description_chars=40)
    assert spec["route"] == "video_text"
    assert spec["multi_route"] is True


def test_resolve_record_no_route_when_not_enabled():
    payload = sample_record("cm-0003", route="audio_text")
    spec = sampling.resolve_record(payload, enabled_routes=["image_text"],
                                   isolation_ids=set(), description_chars=40)
    assert spec["route"] is None
    assert spec["routes"] == ["image_text"] or spec["route"] is None


# --------------------------------------------------------------------------- #
# 抽样
# --------------------------------------------------------------------------- #

def _pool(prefix: str, size: int) -> list:
    return [{"identifier": "%s%03d" % (prefix, index)} for index in range(size)]


def test_sample_pool_shorter_than_limit_keeps_all():
    pool = _pool("cm-", 4)
    picked = sampling.select_samples(pool, max_samples=10, mode="random", seed=26)
    assert len(picked) == 4
    assert [item["identifier"] for item in picked] == \
        [item["identifier"] for item in pool]


def test_sample_pool_longer_than_limit_is_truncated():
    pool = _pool("cm-", 12)
    picked = sampling.select_samples(pool, max_samples=10, mode="random", seed=26)
    assert len(picked) == 10
    assert [item["identifier"] for item in picked] == \
        sorted(item["identifier"] for item in picked)


def test_sample_is_reproducible_for_same_seed_and_input_order():
    pool = _pool("cm-", 12)
    first = [item["identifier"] for item in
             sampling.select_samples(pool, max_samples=5, mode="random", seed=7)]
    shuffled = list(reversed(pool))
    second = [item["identifier"] for item in
              sampling.select_samples(shuffled, max_samples=5, mode="random", seed=7)]
    assert first == second


def test_sample_different_seed_can_differ():
    pool = _pool("cm-", 12)
    first = [item["identifier"] for item in
             sampling.select_samples(pool, max_samples=5, mode="random", seed=7)]
    pool_b = _pool("cm-", 12)
    second = [item["identifier"] for item in
              sampling.select_samples(pool_b, max_samples=5, mode="random", seed=8)]
    assert set(first) != set(second)


def test_sample_first_mode_takes_prefix():
    pool = _pool("cm-", 12)
    picked = sampling.select_samples(pool, max_samples=3, mode="first", seed=1)
    assert [item["identifier"] for item in picked] == ["cm-000", "cm-001", "cm-002"]


def test_sample_fingerprint_is_stable_and_unique():
    pool = _pool("cm-", 12)
    picked = sampling.select_samples(pool, max_samples=5, mode="random", seed=7)
    assert sampling.sampling_fingerprint(picked) == sampling.sampling_fingerprint(picked)
    other = sampling.select_samples(pool, max_samples=4, mode="random", seed=7)
    assert sampling.sampling_fingerprint(picked) != sampling.sampling_fingerprint(other)


# --------------------------------------------------------------------------- #
# 台账与守恒
# --------------------------------------------------------------------------- #

def test_make_ledger_entry_has_required_fields_and_bounded_record():
    payload = sample_record("cm-0001")
    entry = sampling.make_ledger_entry(line=3, payload=payload, action="skip",
                                       label="流程-超出抽样上限",
                                       destination="sample_pool",
                                       reason_code="route_quota_exhausted",
                                       event_time="2026-10-08T10:00:00+08:00")
    for key in ("label", "stage", "operator", "action", "destination", "reason_code",
                "event_time", "batch_id", "sample_id", "retry_count", "line"):
        assert key in entry, key
    assert entry["sample_id"] == "cm-0001"
    assert entry["batch_id"] == "batch-A"
    assert entry["stage"] == sampling.STAGE_ID
    assert "record" in entry

    big = sample_record("cm-big", extra={"payload": "x" * 5000})
    bounded = sampling.make_ledger_entry(line=4, payload=big, action="skip",
                                         label="流程-超出抽样上限",
                                         destination="sample_pool",
                                         reason_code="route_quota_exhausted",
                                         event_time="2026-10-08T10:00:00+08:00",
                                         max_embedded_bytes=512)
    assert "record" not in bounded
    assert bounded["record_omitted"] is True
    assert bounded["record_bytes"] > 512


def test_make_reject_entry_keeps_reason_and_preview():
    entry = sampling.make_reject_entry(line=7, reason="json_parse_error",
                                       detail="JSON 解析失败",
                                       detected_at="2026-10-08T10:00:00+08:00",
                                       raw_text="x" * 500)
    assert entry["line"] == 7
    assert entry["reason_code"] == "json_parse_error"
    assert entry["destination"] == sampling.DEST_ORIGIN
    assert len(entry["raw_text_preview"]) == 200
    assert entry["raw_text_truncated"] is True


def _balanced_stats() -> dict:
    return {"input_records": 12, "valid_records": 10, "bad_lines": 1,
            "origin_quarantined": 1, "rejects_entries": 2,
            "sampled_records": 4, "route_disabled_records": 2,
            "route_exhausted_records": 1, "excluded_records": 3,
            "bad_line_by_reason": {"json_parse_error": 1},
            "origin_by_reason": {"duplicate_identifier": 1},
            "route_disabled_by_reason": {"route_not_enabled": 2},
            "excluded_by_reason": {"upstream_isolated": 3},
            "samples_jsonl_rows": 4, "route_sampled": {"image_text": 4},
            "blank_lines": 0}


def test_conservation_accepts_balanced_statistics():
    ok, failures = sampling.check_conservation(_balanced_stats())
    assert ok is True and failures == []


def test_conservation_detects_row_count_violation():
    broken = _balanced_stats()
    broken["samples_jsonl_rows"] = 5
    ok, failures = sampling.check_conservation(broken)
    assert ok is False
    assert any("samples_jsonl_rows" in item for item in failures)


def test_conservation_detects_route_sum_violation():
    broken = _balanced_stats()
    broken["route_sampled"] = {"image_text": 3}
    ok, failures = sampling.check_conservation(broken)
    assert ok is False
    assert any("route_sampled" in item for item in failures)


def test_conservation_detects_reject_bucket_violation():
    broken = _balanced_stats()
    broken["origin_by_reason"] = {}
    ok, failures = sampling.check_conservation(broken)
    assert ok is False
    assert any("origin_quarantined" in item for item in failures)


def test_conservation_detects_input_mismatch():
    broken = _balanced_stats()
    broken["bad_lines"] = 3
    ok, failures = sampling.check_conservation(broken)
    assert ok is False
    assert any("input_records" in item for item in failures)


def test_route_order_and_constants_are_table1_readonly():
    assert sampling.ROUTE_ORDER == ("image_text", "audio_text", "video_text")
    assert sampling.OPERATOR == "delivery_sample"
    assert sampling.OPERATOR_NUMBER == 26
    # [v4.2] 阶段号按 §4.26 修正为 S8（交付样例）
    assert sampling.STAGE_ID == "S8"
    assert sampling.EXCEL_NAME == "26_sample_list.xlsx"
    assert sampling.MAIN_NAME == "26_samples.jsonl"
    assert sampling.TABLE1_MAX_SAMPLES_PER_ROUTE == 10
