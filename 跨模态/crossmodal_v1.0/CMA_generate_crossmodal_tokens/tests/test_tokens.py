"""表1 #22 业务规则测试（``utils/tokens.py``）。

覆盖：模态标记与 Token 文本、路线判定与分文件、无质量结论不生成、
隔离区样本不进入 samples.jsonl、不可训练样本单独列出、未开启路线不产出文件、
路径安全与台账字段。
"""
from __future__ import annotations

import pytest
from conftest import (DEFAULT_CONFIG_YAML, deep_merge, make_pair, make_record,
                      rec_17, rec_18, rec_19, rec_20, rec_21, rec_audio,
                      rec_image, rec_license, rec_text, rec_video, rec_version)

from utils import tokens, upstream
from utils.schemas import OperatorConfig

import yaml


def load_config(text: str = DEFAULT_CONFIG_YAML) -> OperatorConfig:
    return OperatorConfig(**yaml.safe_load(text))


ALL_ROUTES_CONFIG = DEFAULT_CONFIG_YAML.replace(
    "  audio_text: false\n  video_text: false\n  local_annotation: false\n",
    "  audio_text: true\n  video_text: true\n  local_annotation: true\n")


def eval_record(payload, config=None, event_time="2026-10-08T10:00:00+08:00",
                link_status=None):
    config = config or load_config()
    spec = upstream.resolve_record(payload)
    return tokens.evaluate_record(spec, config=config, event_time=event_time,
                                  link_status=link_status or {})


# --------------------------------------------------------------------------- #
# 模态标记与 Token 文本
# --------------------------------------------------------------------------- #

def test_markers_follow_canonical_order_image_audio_video():
    markers = tokens.build_markers(("video", "text", "image", "audio"), load_config())
    assert markers == ["<image>", "<audio>", "<video>"]


def test_token_text_starts_with_image_marker_for_default_image_text():
    markers = tokens.build_markers(("image", "text"), load_config())
    token_text = tokens.build_token_text(markers, "一只猫趴在草地上")
    assert token_text == "<image> 一只猫趴在草地上"
    assert token_text.startswith("<image>")


def test_token_text_for_audio_and_video():
    config = load_config(ALL_ROUTES_CONFIG)
    assert tokens.build_token_text(tokens.build_markers(("audio", "text"), config),
                                   "语音内容") == "<audio> 语音内容"
    assert tokens.build_token_text(tokens.build_markers(("video", "text"), config),
                                   "视频内容") == "<video> 视频内容"


def test_marker_config_values_are_fixed_by_table1():
    with pytest.raises(Exception):
        load_config(DEFAULT_CONFIG_YAML.replace('image: "<image>"',
                                                'image: "<img>"'))


def test_validate_markers_detects_missing_and_unexpected():
    validation = tokens.validate_markers(["<image>"], ("image", "audio", "text"))
    assert validation["valid"] is False
    assert validation["missing"] == ["<audio>"]
    assert validation["unexpected"] == []
    ok = tokens.validate_markers(["<image>"], ("image", "text"))
    assert ok["valid"] is True


def test_validate_markers_rejects_bad_order_or_duplicates():
    assert tokens.validate_markers(["<text>"], ("text",))["valid"] is False
    assert tokens.validate_markers(["<image>", "<image>"], ("image", "text"))["valid"] is False


# --------------------------------------------------------------------------- #
# 路线判定
# --------------------------------------------------------------------------- #

def test_default_config_only_enables_image_text_route():
    config = load_config()
    assert config.enabled_routes == ("image_text",)


def test_applicable_routes_by_modalities():
    assert tokens.applicable_routes(("image", "text"), local_annotation=False) == ("image_text",)
    assert tokens.applicable_routes(("audio", "text"), local_annotation=False) == ("audio_text",)
    assert tokens.applicable_routes(("video", "text"), local_annotation=False) == ("video_text",)
    assert tokens.applicable_routes(("text",), local_annotation=False) == ()
    assert tokens.applicable_routes(("image", "audio"), local_annotation=False) == ()


def test_multi_modality_sample_maps_to_multiple_routes():
    routes = tokens.applicable_routes(("image", "audio", "video", "text"),
                                      local_annotation=False)
    assert routes == ("image_text", "audio_text", "video_text")


def test_local_annotation_route_requires_evidence():
    assert tokens.applicable_routes(("image", "text"),
                                    local_annotation=True) == ("image_text",
                                                               "local_annotation")
    assert tokens.applicable_routes(("image",),
                                    local_annotation=True) == ("local_annotation",)
    assert tokens.applicable_routes(("text",), local_annotation=True) == ()


def test_route_file_names_are_declared_and_disabled_routes_have_none():
    config = load_config(ALL_ROUTES_CONFIG)
    assert set(tokens.route_files(config)) == {"image_text", "audio_text",
                                               "video_text", "local_annotation"}
    assert set(tokens.route_files(load_config())) == {"image_text"}


# --------------------------------------------------------------------------- #
# 分流：生成 / 待处理 / 不可训练 / 隔离跳过 / 原始隔离
# --------------------------------------------------------------------------- #

def test_generate_default_image_text():
    outcome = eval_record(make_record("tc-0001", rec_19(), rec_20(), rec_license(),
                                      rec_text("一只猫"), rec_image(), rec_version()))
    assert outcome["action"] == "generate"
    assert outcome["primary_route"] == "image_text"
    assert outcome["token_text"] == "<image> 一只猫"
    assert outcome["label"] == tokens.LABEL_GENERATED


def test_no_quality_conclusion_does_not_generate():
    outcome = eval_record(make_record("tc-0002", rec_19(), rec_license(),
                                      rec_text(), rec_image()))
    assert outcome["action"] == "pending"
    assert outcome["reason"] == tokens.REASON_NO_QUALITY_CONCLUSION
    assert outcome["label"] == tokens.LABEL_PENDING_QUALITY
    assert outcome["token_text"] is None


def test_isolated_sample_is_skipped_and_not_generated():
    outcome = eval_record(make_record("tc-0003", rec_19(), rec_20(), rec_21(),
                                      rec_license(), rec_text(), rec_image()))
    assert outcome["action"] == "isolated_skip"
    assert outcome["reason"] == tokens.REASON_UPSTREAM_ISOLATED
    assert outcome["token_text"] is None


def test_quality_isolated_sample_is_skipped():
    outcome = eval_record(make_record("tc-0004", rec_20("isolated", "sample_quarantine"),
                                      rec_license(), rec_text(), rec_image()))
    assert outcome["action"] == "isolated_skip"


def test_untrainable_sample_listed_separately():
    outcome = eval_record(make_record("tc-0005", rec_20(), rec_license("不可训练"),
                                      rec_text(), rec_image()))
    assert outcome["action"] == "untrainable"
    assert outcome["reason"] == tokens.REASON_LICENSE_NOT_TRAINABLE
    assert outcome["label"] == tokens.LABEL_UNTRAINABLE
    # 规则 §2.1/§2.3：授权不可训练属业务问题，标注建议去向为样本隔离库
    assert outcome["label_detail"] == "样本-敏感"
    assert outcome["recommended_destination"] == "sample_quarantine"
    assert outcome["token_text"] is None


def test_upstream_lock_is_treated_as_isolated():
    outcome = eval_record(make_record("tc-lock", rec_20(), rec_license(),
                                      rec_text(), rec_image(), {"locked": True}))
    assert outcome["action"] == "isolated_skip"
    assert outcome["reason"] == tokens.REASON_UPSTREAM_ISOLATED


def test_missing_license_is_pending_not_isolated():
    outcome = eval_record(make_record("tc-0006", rec_20(), rec_text(), rec_image()))
    assert outcome["action"] == "pending"
    assert outcome["reason"] == tokens.REASON_LICENSE_MISSING


def test_missing_text_is_pending():
    outcome = eval_record(make_record("tc-0007", rec_20(), rec_license(), rec_image()))
    assert outcome["action"] == "pending"
    assert outcome["reason"] == tokens.REASON_MISSING_TEXT


def test_no_applicable_route_is_pending():
    outcome = eval_record(make_record("tc-0008", rec_20(), rec_license(), rec_text()))
    assert outcome["action"] == "pending"
    assert outcome["reason"] == tokens.REASON_NO_APPLICABLE_ROUTE


def test_disabled_route_does_not_generate_file():
    """音文样本在默认（音文路线关闭）配置下不生成 Token。"""
    outcome = eval_record(make_record("tc-0009", rec_20(), rec_license(),
                                      rec_text(), rec_audio()))
    assert outcome["action"] == "pending"
    assert outcome["reason"] == tokens.REASON_ROUTE_DISABLED


def test_audio_route_enabled_generates_audio_token():
    config = load_config(ALL_ROUTES_CONFIG)
    outcome = eval_record(make_record("tc-0010", rec_20(), rec_license(),
                                      rec_text("语音内容"), rec_audio()), config)
    assert outcome["action"] == "generate"
    assert outcome["primary_route"] == "audio_text"
    assert outcome["token_text"] == "<audio> 语音内容"


def test_local_annotation_route_produces_extra_file_membership():
    config = load_config(ALL_ROUTES_CONFIG)
    outcome = eval_record(make_record("tc-0011", rec_20(), rec_license(),
                                      rec_text("局部描述"), rec_image(),
                                      rec_17("confirmed")), config)
    assert outcome["action"] == "generate"
    assert outcome["routes"] == ["image_text", "local_annotation"]
    assert outcome["primary_route"] == "image_text"


def test_missing_identifier_goes_to_origin_quarantine():
    outcome = eval_record(make_record(None, rec_20(), rec_license(),
                                      rec_text(), rec_image()))
    assert outcome["action"] == "origin"
    assert outcome["reason"] == tokens.REASON_MISSING_IDENTIFIER


def test_unsafe_media_path_goes_to_origin_quarantine():
    outcome = eval_record(make_record("tc-0012", rec_20(), rec_license(),
                                      rec_text(), {"image": {"path": "/abs/x.png"}}))
    assert outcome["action"] == "origin"
    assert outcome["reason"] == tokens.REASON_UNSAFE_MEDIA_PATH


def test_origin_takes_precedence_over_isolation():
    outcome = eval_record(make_record(None, rec_20(), rec_21(), rec_license(),
                                      rec_text(), rec_image()))
    assert outcome["action"] == "origin"


def test_pending_takes_precedence_over_license():
    """没有质量结论时不生成 Token，也不再进入不可训练清单（无质量结论优先）。"""
    outcome = eval_record(make_record("tc-0013", rec_license("不可训练"),
                                      rec_text(), rec_image()))
    assert outcome["action"] == "pending"


# --------------------------------------------------------------------------- #
# 产物构造
# --------------------------------------------------------------------------- #

def test_sample_record_appends_only_own_namespace():
    payload = make_record("tc-0020", rec_19(), rec_20(), rec_license(),
                          rec_text("一只猫"), rec_image(), rec_version(),
                          {"unknown_extension": {"keep": True}})
    outcome = eval_record(payload)
    sample = tokens.build_sample_record(payload, outcome, route_files=None)
    assert sample["unknown_extension"] == {"keep": True}
    assert sample["processing"]["20"]["operator"] == "alignment_qc"
    node = sample["processing"]["22"]
    assert node["operator"] == tokens.OPERATOR
    assert node["operator_number"] == 22
    # [v4.2] status 双字段：int status + 字符串 process_status
    assert node["status"] == 1
    assert node["process_status"] == "generated"
    assert node["reason_code"] == tokens.REASON_TOKEN_GENERATED
    assert node["token_text"] == "<image> 一只猫"
    assert node["version_state_changed"] is False
    assert node["compressed"] is False
    assert node["media_link_policy"] == "original_files_link_only"
    assert sample["operator_22"]["token_char_count"] == len("<image> 一只猫")


def test_sample_record_keeps_original_media_reference_untouched():
    payload = make_record("tc-0021", rec_19(), rec_20(), rec_license(),
                          rec_text("一只猫"), rec_image("media/cat.png"))
    outcome = eval_record(payload)
    sample = tokens.build_sample_record(payload, outcome, route_files=None)
    assert sample["image"] == {"path": "media/cat.png", "modality": "image"}
    node = sample["processing"]["22"]
    assert node["media_links"][0]["path"] == "media/cat.png"
    assert node["media_links"][0]["storage"] == "object_storage_reference"
    assert "content" not in node["media_links"][0]


def test_tokens_row_shape():
    payload = make_record("tc-0022", rec_19(), rec_20(), rec_license(),
                          rec_text("一只猫"), rec_image("media/cat.png"))
    outcome = eval_record(payload)
    row = tokens.build_token_row(payload, outcome, event_time="2026-10-08T10:00:00+08:00")
    assert row["sample_id"] == "tc-0022"
    assert row["route"] == "image_text"
    assert row["markers"] == ["<image>"]
    assert row["token_text"] == "<image> 一只猫"
    assert row["token_char_count"] == len(row["token_text"])
    assert row["subword_token_ids"] is None
    assert row["tokenizer"] is None
    assert row["marker_validation"]["valid"] is True
    assert row["text_reference"]["field"] == "text.content"


def test_metadata_index_row_shape():
    payload = make_record("tc-0023", rec_20(), rec_license(), rec_text("一只猫"),
                          rec_image("media/cat.png"), rec_version("v1.2"))
    outcome = eval_record(payload)
    row = tokens.build_metadata_row(payload, outcome, event_time="2026-10-08T10:00:00+08:00")
    assert row["sample_id"] == "tc-0023"
    assert row["version"] == "v1.2"
    assert row["version_state_changed"] is False
    assert row["modalities"] == ["image", "text"]
    assert row["object_links"][0]["path"] == "media/cat.png"


def test_ledger_entry_carries_reason_and_record():
    payload = make_record("tc-0024", rec_19(), rec_license(), rec_text(), rec_image())
    outcome = eval_record(payload)
    entry = tokens.make_ledger_entry(line=3, spec=upstream.resolve_record(payload),
                                     outcome=outcome, detail=outcome["detail"],
                                     detected_at="2026-10-08T10:00:00+08:00")
    assert entry["line"] == 3
    assert entry["reason"] == tokens.REASON_NO_QUALITY_CONCLUSION
    assert entry["destination"] == "pipeline_wait"
    assert entry["record"]["sample_id"] == "tc-0024"
    assert entry["operator_number"] == 22


def test_ledger_entry_has_rule_2_3_tabular_fields():
    """规则 §2.3 统一台账字段：label/stage·operator/action/destination/event_time/batch_id/sample_id/retry_count。"""
    payload = make_record("tc-0025", rec_19(), rec_license(), rec_text(), rec_image(),
                          rec_version(),
                          {"metadata": {"retry_count": 2}})
    outcome = eval_record(payload)
    entry = tokens.make_ledger_entry(line=9, spec=upstream.resolve_record(payload),
                                     outcome=outcome, detail=outcome["detail"],
                                     detected_at="2026-10-08T10:00:00+08:00")
    for key in ("label", "stage", "operator", "action", "destination", "event_time",
                "batch_id", "sample_id", "retry_count"):
        assert key in entry, key
    assert entry["stage"] == tokens.STAGE_ID
    assert entry["batch_id"] == "batch-A"
    assert entry["retry_count"] == 2
    assert entry["event_time"] == entry["detected_at"]


def test_sample_record_carries_batch_and_event_time():
    payload = make_record("tc-0026", rec_20(), rec_license(), rec_text("文本"),
                          rec_image(), rec_version(), {"metadata": {"retry_count": 1}})
    outcome = eval_record(payload)
    sample = tokens.build_sample_record(payload, outcome, route_files=None)
    node = sample["processing"]["22"]
    assert node["batch_id"] == "batch-A"
    assert node["retry_count"] == 1
    assert node["event_time"] == node["generated_at"]


def test_reject_entry_has_no_record_and_truncates_raw():
    entry = tokens.make_reject_entry(line=4, reason=tokens.REASON_JSON_PARSE,
                                     detail="JSON 解析失败",
                                     detected_at="2026-10-08T10:00:00+08:00",
                                     raw_text="x" * 900)
    assert entry["line"] == 4
    assert len(entry["raw"]) <= tokens.MAX_RAW_CHARS
    assert "record" not in entry


def test_pair_record_generates_from_nested_side_records():
    payload = deep_merge(
        make_pair("pair-0050",
                  left=deep_merge(rec_image("media/l.png"), rec_text("左侧文本")),
                  right=deep_merge(rec_text("右侧文本"))),
        rec_20(), rec_license())
    outcome = eval_record(payload)
    assert outcome["action"] == "generate"
    assert outcome["primary_route"] == "image_text"
    assert outcome["token_text"] == "<image> 左侧文本"
