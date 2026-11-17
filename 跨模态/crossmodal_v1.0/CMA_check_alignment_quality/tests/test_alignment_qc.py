"""核心业务测试：模态解析、五类硬检查（表1 #20 研发边界）、分诊与台账、报告/偏科清单。"""
from __future__ import annotations

import json
from pathlib import Path

from utils import alignment_qc as qc
from utils.media_carry import MediaCarrier
from tests.conftest import business


def _carrier(media_root: Path, tmp_path: Path) -> MediaCarrier:
    return MediaCarrier(media_root, tmp_path / "carried")


def _evaluate(record: dict, media_root: Path, tmp_path: Path, **overrides) -> qc.RecordOutcome:
    biz = business(**overrides)
    return qc.evaluate_record(
        record,
        sample_id=record.get("sample_id"),
        media_root=media_root,
        carrier=_carrier(media_root, tmp_path),
        business=biz,
        fallback_selected=True,
    )


# --------------------------------------------------------------------------- #
# 模态解析
# --------------------------------------------------------------------------- #

def test_default_business_matches_table1_settings() -> None:
    biz = qc.default_business()
    assert biz["enabled"] is True
    assert biz["quality_subset_fallback"] == 0.10
    assert biz["topic_skew_threshold"] == 0.70
    assert biz["metrics"] == {"ppl": "upstream_first", "brisque": "upstream_first",
                              "snr": "upstream_first", "ffmpeg": "upstream_first"}


def test_resolve_modalities_reads_string_and_object_forms(media_root: Path) -> None:
    text_obj = qc.resolve_modalities({"text": {"content": "红色方块", "lang": "zh"},
                                      "image": "media/ok_a.png"})
    assert text_obj["text"]["content"] == "红色方块"
    assert text_obj["text"]["source"] == "text.content"
    assert text_obj["image"]["path"] == "media/ok_a.png"
    assert text_obj["video"] is None and text_obj["audio"] is None

    text_str = qc.resolve_modalities({"text": "小狗在草地上"})
    assert text_str["text"]["content"] == "小狗在草地上"
    assert text_str["text"]["source"] == "text"


def test_resolve_modalities_supports_description_and_upstream_paths() -> None:
    alt = qc.resolve_modalities({"caption": "一只橘猫"})
    assert alt["text"]["content"] == "一只橘猫"

    deep = qc.resolve_modalities({
        "metadata": {"description": "蓝色杯子"},
        "processing": {"04": {"paths": {"image": "04_images/a.png"}}},
        "audio": {"path": "06_audio/a.wav", "duration": 3.0},
    })
    assert deep["text"]["content"] == "蓝色杯子"
    assert deep["image"]["path"] == "04_images/a.png"
    assert deep["audio"]["path"] == "06_audio/a.wav"


def test_resolve_modalities_reads_video_frames_and_audio_segments() -> None:
    resolved = qc.resolve_modalities({
        "video": {"path": "05_frames/v.mp4",
                  "frames": [{"path": "05_frames/f1.png"}, {"path": "05_frames/f2.png"}]},
        "audio": {"path": "06_audio/a.wav",
                  "segments": [{"path": "06_audio/a_0.wav", "start": 0.0, "end": 1.0}]},
    })
    assert resolved["video"]["path"] == "05_frames/v.mp4"
    assert len(resolved["video"]["frames"]) == 2
    assert resolved["audio"]["segments"][0]["path"] == "06_audio/a_0.wav"


def test_resolve_modalities_empty_record() -> None:
    resolved = qc.resolve_modalities({"sample_id": "s1"})
    assert all(resolved[key] is None for key in ("text", "image", "video", "audio"))


# --------------------------------------------------------------------------- #
# 通过路径
# --------------------------------------------------------------------------- #

def test_accepts_healthy_text_image_record(media_root: Path, tmp_path: Path) -> None:
    record = {
        "sample_id": "s1",
        "text": {"content": "金毛犬在草地上接飞盘"},
        "image": {"path": "media/ok_a.png", "brisque": 33.0},
        "qc": {"text_fluency": {"ppl": 41.0},
               "reverse_verify": {"passed": True, "score": 0.62,
                                  "check_status": "checked"}},
    }
    outcome = _evaluate(record, media_root, tmp_path)
    assert outcome.decision == qc.DECISION_ACCEPTED
    assert outcome.flagged is False
    assert outcome.label == qc.LABEL_PASSED
    assert outcome.destination == qc.DESTINATION_MAIN
    assert outcome.record is not None
    node = outcome.record["processing"]["20"]
    assert node["status"] == 1
    assert node["process_status"] == "success"
    assert node["operator_number"] == 20
    assert outcome.record["operator_20"]["source_reference"] == "operator_20"
    # 上游结论必须被复用，而不是重新伪造
    assert outcome.checks[qc.CHECK_METRIC_PPL]["status"] == "ok"
    assert outcome.checks[qc.CHECK_METRIC_PPL]["evidence"]["source"] == "upstream"


def test_accepts_audio_video_pair_with_upstream_snr(media_root: Path, tmp_path: Path) -> None:
    record = {
        "sample_id": "s2",
        "video": {"path": "media/clip_a.mp4"},
        "audio": {"path": "media/tone.wav", "snr_db": 22.5},
    }
    outcome = _evaluate(record, media_root, tmp_path)
    assert outcome.decision == qc.DECISION_ACCEPTED
    assert outcome.checks[qc.CHECK_VIDEO_DECODABLE]["status"] == "ok"
    assert outcome.checks[qc.CHECK_AUDIO_NON_SILENT]["status"] == "ok"
    # 纯音视频记录不适用图文反向验证 → 不适用，不计失败
    assert outcome.checks[qc.CHECK_REVERSE_VERIFY]["status"] == qc.STATUS_NOT_APPLICABLE


# --------------------------------------------------------------------------- #
# 原始隔离区
# --------------------------------------------------------------------------- #

def test_missing_media_goes_to_original_quarantine(media_root: Path, tmp_path: Path) -> None:
    record = {"sample_id": "s3", "text": {"content": "引用了不存在的图"},
              "image": "media/nope.png"}
    outcome = _evaluate(record, media_root, tmp_path)
    assert outcome.decision == qc.DECISION_ORIGIN_QUARANTINE
    assert outcome.reason_code == "media_missing"
    assert outcome.label == qc.LABEL_ORIGIN_UNPARSABLE
    assert outcome.destination == qc.DESTINATION_ORIGIN
    assert outcome.ledger is not None and outcome.ledger["sample_id"] == "s3"


def test_broken_image_goes_to_original_quarantine(media_root: Path, tmp_path: Path) -> None:
    record = {"sample_id": "s4", "text": {"content": "损坏图"},
              "image": "media/broken.png"}
    outcome = _evaluate(record, media_root, tmp_path)
    assert outcome.decision == qc.DECISION_ORIGIN_QUARANTINE
    assert outcome.reason_code == "media_unreadable"
    assert outcome.label == qc.LABEL_ORIGIN_BROKEN


def test_undecodable_video_goes_to_original_quarantine(media_root: Path, tmp_path: Path) -> None:
    record = {"sample_id": "s5", "video": "media/broken.mp4",
              "audio": "media/tone.wav"}
    outcome = _evaluate(record, media_root, tmp_path)
    assert outcome.decision == qc.DECISION_ORIGIN_QUARANTINE
    assert outcome.reason_code == "media_undecodable"
    assert outcome.checks[qc.CHECK_VIDEO_DECODABLE]["status"] == qc.STATUS_FAILED


def test_all_silent_audio_goes_to_original_quarantine(media_root: Path, tmp_path: Path) -> None:
    record = {"sample_id": "s6", "video": "media/clip_a.mp4",
              "audio": "media/silent.wav"}
    outcome = _evaluate(record, media_root, tmp_path)
    assert outcome.decision == qc.DECISION_ORIGIN_QUARANTINE
    assert outcome.reason_code == "audio_silent"
    assert outcome.checks[qc.CHECK_AUDIO_NON_SILENT]["status"] == qc.STATUS_FAILED


def test_unsupported_media_path_is_rejected(media_root: Path, tmp_path: Path) -> None:
    record = {"sample_id": "s7", "text": {"content": "绝对路径"},
              "image": "/etc/passwd.png"}
    outcome = _evaluate(record, media_root, tmp_path)
    assert outcome.decision == qc.DECISION_ORIGIN_QUARANTINE
    assert outcome.reason_code == "unsupported_media_path"

    escape = {"sample_id": "s8", "text": {"content": "穿越"},
              "image": "../outside.png"}
    assert _evaluate(escape, media_root, tmp_path).reason_code == \
        "unsupported_media_path"


def test_missing_sample_id_is_original_quarantine(media_root: Path, tmp_path: Path) -> None:
    record = {"text": {"content": "没有标识"}, "image": "media/ok_a.png"}
    outcome = _evaluate(record, media_root, tmp_path)
    assert outcome.decision == qc.DECISION_ORIGIN_QUARANTINE
    assert outcome.reason_code == "missing_field:sample_id"
    assert outcome.label == qc.LABEL_ORIGIN_MISSING_FIELD


# --------------------------------------------------------------------------- #
# 样本隔离库
# --------------------------------------------------------------------------- #

def test_single_modality_goes_to_sample_quarantine(media_root: Path, tmp_path: Path) -> None:
    record = {"sample_id": "s9", "text": {"content": "只有文本一个模态"}}
    outcome = _evaluate(record, media_root, tmp_path)
    assert outcome.decision == qc.DECISION_SAMPLE_QUARANTINE
    assert outcome.reason_code == "insufficient_modalities"
    assert outcome.label == qc.LABEL_SAMPLE_SEMANTIC
    assert outcome.destination == qc.DESTINATION_SAMPLE


def test_empty_text_goes_to_sample_quarantine(media_root: Path, tmp_path: Path) -> None:
    record = {"sample_id": "s10", "text": {"content": "   \n  "},
              "image": "media/ok_a.png"}
    outcome = _evaluate(record, media_root, tmp_path)
    assert outcome.decision == qc.DECISION_SAMPLE_QUARANTINE
    assert outcome.reason_code == "text_empty"
    assert outcome.checks[qc.CHECK_TEXT_NONEMPTY]["status"] == qc.STATUS_FAILED


def test_no_media_and_no_text_is_sample_quarantine(media_root: Path, tmp_path: Path) -> None:
    record = {"sample_id": "s11"}
    outcome = _evaluate(record, media_root, tmp_path)
    assert outcome.decision == qc.DECISION_SAMPLE_QUARANTINE
    assert outcome.reason_code == "insufficient_modalities"


# --------------------------------------------------------------------------- #
# 待人工复核（flagged，仍留在主输出）
# --------------------------------------------------------------------------- #

def test_upstream_reverse_mismatch_flags_without_isolating(media_root: Path,
                                                           tmp_path: Path) -> None:
    record = {"sample_id": "s12", "text": {"content": "描述与图不符"},
              "image": {"path": "media/ok_a.png"},
              "qc": {"reverse_verify": {"passed": False, "score": 0.11,
                                        "check_status": "checked"}}}
    outcome = _evaluate(record, media_root, tmp_path)
    assert outcome.decision == qc.DECISION_FLAGGED
    assert outcome.flagged is True
    assert outcome.record is not None            # 不拦样本，仍进主输出
    assert outcome.reason_code == "reverse_inconsistent"
    assert outcome.destination == qc.DESTINATION_REVIEW
    assert outcome.record["operator_20"]["review_required"] is True


def test_absent_upstream_reverse_defers_to_pool(media_root: Path, tmp_path: Path) -> None:
    record = {"sample_id": "s13", "text": {"content": "需要本地反向检索"},
              "image": {"path": "media/ok_a.png"}}
    outcome = _evaluate(record, media_root, tmp_path)
    assert outcome.decision == qc.DECISION_ACCEPTED
    assert outcome.reverse_candidate is not None
    assert outcome.reverse_candidate["sample_id"] == "s13"
    assert outcome.checks[qc.CHECK_REVERSE_VERIFY]["status"] == "pending_pool"


def test_reverse_disabled_is_not_applicable(media_root: Path, tmp_path: Path) -> None:
    record = {"sample_id": "s14", "text": {"content": "关闭反向验证"},
              "image": {"path": "media/ok_a.png"}}
    outcome = _evaluate(record, media_root, tmp_path,
                        reverse_verify={"enabled": False})
    assert outcome.reverse_candidate is None
    assert outcome.checks[qc.CHECK_REVERSE_VERIFY]["status"] == qc.STATUS_NOT_APPLICABLE


# --------------------------------------------------------------------------- #
# 检查明细
# --------------------------------------------------------------------------- #

def test_checks_cover_all_table1_boundaries(media_root: Path, tmp_path: Path) -> None:
    record = {"sample_id": "s15", "text": {"content": "完整模态"},
              "image": {"path": "media/ok_a.png"},
              "video": {"path": "media/clip_a.mp4"},
              "audio": {"path": "media/tone.wav", "snr_db": 20.0}}
    outcome = _evaluate(record, media_root, tmp_path)
    for name in (qc.CHECK_TEXT_NONEMPTY, qc.CHECK_FILE_OPENABLE, qc.CHECK_MODALITIES,
                 qc.CHECK_VIDEO_DECODABLE, qc.CHECK_AUDIO_NON_SILENT,
                 qc.CHECK_METRIC_PPL, qc.CHECK_METRIC_BRISQUE, qc.CHECK_METRIC_SNR,
                 qc.CHECK_REVERSE_VERIFY):
        assert name in outcome.checks, name
        assert outcome.checks[name]["status"] in (
            qc.STATUS_OK, qc.STATUS_FAILED, qc.STATUS_NOT_APPLICABLE,
            qc.STATUS_INSUFFICIENT, "pending_pool")


def test_check_entry_shape(media_root: Path, tmp_path: Path) -> None:
    record = {"sample_id": "s16", "text": {"content": "形状检查"},
              "image": {"path": "media/ok_a.png"}}
    outcome = _evaluate(record, media_root, tmp_path)
    entry = outcome.checks[qc.CHECK_FILE_OPENABLE]
    assert set(entry) >= {"status", "reason", "evidence"}
    assert isinstance(entry["evidence"], dict)


def test_metrics_outside_fallback_subset_are_insufficient(media_root: Path,
                                                          tmp_path: Path) -> None:
    biz = business()
    outcome = qc.evaluate_record(
        {"sample_id": "s17", "text": {"content": "无上游指标"},
         "image": {"path": "media/ok_a.png"}},
        sample_id="s17", media_root=media_root,
        carrier=_carrier(media_root, tmp_path), business=biz, fallback_selected=False)
    entry = outcome.checks[qc.CHECK_METRIC_BRISQUE]
    assert entry["status"] in (qc.STATUS_INSUFFICIENT, qc.STATUS_OK)
    if entry["status"] == qc.STATUS_INSUFFICIENT:
        assert entry["reason"] in ("outside_fallback_subset", "brisque_backend_missing")
    assert outcome.decision == qc.DECISION_ACCEPTED   # 指标缺失不导致隔离


# --------------------------------------------------------------------------- #
# 报告与偏科清单
# --------------------------------------------------------------------------- #

def test_supplement_list_flags_skew_without_batch_isolation() -> None:
    payload = qc.build_supplement_list(
        {"image+text": 18, "audio+video": 1, "audio+text+video": 1},
        threshold=0.70, unique_id="t_1", run_id="run001",
        operator=qc.OPERATOR_NAME, operator_version=qc.OPERATOR_VERSION,
        stage_id=qc.STAGE_ID, generated_at=qc.now_iso())
    assert payload["skewed"] is True
    assert payload["dominant_route"] == "image+text"
    assert payload["batch_isolation"] is False
    assert "image+text" not in [item["route"] for item in payload["supplement_targets"]]
    assert payload["route_distribution"]["image+text"]["share"] == 0.9


def test_supplement_list_balanced_batch_not_skewed() -> None:
    payload = qc.build_supplement_list(
        {"image+text": 5, "audio+video": 5}, threshold=0.70,
        unique_id="t_1", run_id="run001", operator=qc.OPERATOR_NAME,
        operator_version=qc.OPERATOR_VERSION, stage_id=qc.STAGE_ID,
        generated_at=qc.now_iso())
    assert payload["skewed"] is False
    assert payload["supplement_targets"] == []


def test_quality_report_is_pending_writeback() -> None:
    report = qc.build_quality_report(
        statistics={"input": 10, "output": 8, "rejected": 1},
        checks={qc.CHECK_MODALITIES: {"ok": 8, "failed": 1, "not_applicable": 0,
                                      "insufficient_evidence": 0}},
        metrics={}, reverse={}, topic_skew={"skewed": False},
        unique_id="t_1", run_id="run001", operator=qc.OPERATOR_NAME,
        operator_version=qc.OPERATOR_VERSION, stage_id=qc.STAGE_ID,
        generated_at=qc.now_iso(), config_snapshot={"business": {}},
        reverse_available=True)
    assert report["status"] == qc.REPORT_STATUS == "pending_writeback"
    assert report["report_id"] == "qreport-t_1-run001"
    assert report["writeback"]["writeback_completed"] is False
    assert report["writeback"]["version_state"] == "pending_release"
    assert report["statistics"]["output"] == 8
    assert report["operator_number"] == 20


def test_outcome_record_is_json_serializable(media_root: Path, tmp_path: Path) -> None:
    record = {"sample_id": "s18", "text": {"content": "可序列化"},
              "image": {"path": "media/ok_a.png"}}
    outcome = _evaluate(record, media_root, tmp_path)
    text = json.dumps(outcome.record, ensure_ascii=False)
    assert "可序列化" in text and "operator_20" in text
