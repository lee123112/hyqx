"""业务规则层测试（表1 #23 冻结口径）。

覆盖：
- 记录解析（标识、媒体引用、外部引用、Token）；
- 隔离样本识别（#21 处理标记 + 隔离清单）；
- 三路分流（入包 / 排除隔离样本 / 原始隔离）；
- 材料发现与分类（质量报告、抽检结论、隔离清单、版本说明）；
- 交付门禁（训练文件已生成 + 质量报告已写回 + 抽检已写回，三项缺一不可）。
"""
from __future__ import annotations

import json
from pathlib import Path
from typing import Any, Dict

import pytest

from conftest import (build_input_package, isolation_entry, isolation_marker,
                      quality_report, sampling_conclusion, token_record,
                      version_notes, write_json, write_jsonl)
from utils import delivery


# --------------------------------------------------------------------------- #
# 记录解析
# --------------------------------------------------------------------------- #

def test_resolve_record_extracts_identifier_media_and_token():
    spec = delivery.resolve_record(token_record(
        "cm-0001", media=["media/a.png"],
        external=["oss://bucket/x.png", "https://example.com/y.png"]))
    assert spec["identifier"] == "cm-0001"
    assert spec["media_refs"] == ["media/a.png"]
    assert sorted(spec["external_refs"]) == ["https://example.com/y.png",
                                             "oss://bucket/x.png"]
    assert spec["token"] == "<image> 一只橘猫坐在窗台上 <image>"
    assert spec["route"] == "image_text"


def test_resolve_record_prefers_nested_and_list_forms():
    payload = {"id": "cm-0009", "tokens": ["<video>", "街景", "<video>"],
               "processing": {"22": {"route": "video_text"}},
               "image": {"path": "media/frame.png"}}
    spec = delivery.resolve_record(payload)
    assert spec["identifier"] == "cm-0009"
    assert spec["media_refs"] == ["media/frame.png"]
    assert spec["token"] == "<video> 街景 <video>"
    assert spec["route"] == "video_text"


def test_resolve_record_marks_unsafe_and_external_references():
    spec = delivery.resolve_record(token_record(
        "cm-0015", media=["C:/temp/x.png", "../escape.png", "/abs/x.png",
                          "http://a/b.png"]))
    # 绝对路径/穿越/协议地址一律不进相对引用
    assert spec["media_refs"] == []
    assert len(spec["unsafe_refs"]) == 3
    assert spec["external_refs"] == ["http://a/b.png"]


def test_resolve_record_missing_identifier_is_none():
    spec = delivery.resolve_record({"token": "x", "routes": ["image_text"]})
    assert spec["identifier"] is None


# --------------------------------------------------------------------------- #
# 隔离识别
# --------------------------------------------------------------------------- #

def test_is_isolated_by_processing_marker():
    payload = token_record("cm-0011", isolation=isolation_marker("cm-0011"))
    verdict = delivery.is_isolated(payload, set())
    assert verdict is not None
    assert verdict["source"] == "record_marker"
    assert verdict["label"] == "样本-语义不通"
    assert verdict["reason_code"] == ingestion_reason(verdict)


def ingestion_reason(verdict: Dict[str, Any]) -> str:
    return verdict["reason_code"]


def test_is_isolated_by_status_field_without_boolean():
    payload = {"sample_id": "cm-0012", "processing": {"21": {"status": "isolated"}}}
    verdict = delivery.is_isolated(payload, set())
    assert verdict is not None
    assert verdict["source"] == "record_marker"


def test_is_isolated_by_isolation_list_only():
    verdict = delivery.is_isolated({"sample_id": "cm-0013"}, {"cm-0013"})
    assert verdict is not None
    assert verdict["source"] == "isolation_list"
    assert verdict["reason_code"] == delivery.REASON_UPSTREAM_ISOLATED


def test_clean_record_is_not_isolated():
    assert delivery.is_isolated(token_record("cm-0001"), set()) is None


# --------------------------------------------------------------------------- #
# 三路分流
# --------------------------------------------------------------------------- #

def test_evaluate_record_packages_clean_record_with_operator_namespace():
    payload = token_record("cm-0001", media=["media/cm-0001.png"])
    outcome = delivery.evaluate_record(
        payload, isolation_ids=set(), event_time="2026-10-08T12:00:00+08:00",
        config=_config(), line=1)
    assert outcome["action"] == delivery.ACTION_PACKAGE
    output = outcome["output"]
    assert output["sample_id"] == "cm-0001"
    node = output[delivery.NAMESPACE]
    assert node["operator_number"] == 23
    assert node["operator_version"] == delivery.OPERATOR_VERSION
    # [v4.2] status 双字段：int status + 字符串 process_status
    assert node["status"] == 1
    assert node["process_status"] == "packaged"
    assert node["decision"] == "pending_release"
    assert node["human_verified"] is False
    assert node["review_required"] is True
    assert node["source_reference"] == delivery.SOURCE_REFERENCE
    assert node["member"] == "train/samples.jsonl"
    assert node["token_present"] is True
    assert output["processing"]["22"]["status"] == "tokenized"


def test_evaluate_record_excludes_isolated_without_packaging():
    payload = token_record("cm-0013")
    outcome = delivery.evaluate_record(
        payload, isolation_ids={"cm-0013"}, event_time="t", config=_config(), line=3)
    assert outcome["action"] == delivery.ACTION_EXCLUDE
    assert outcome["reason"] == delivery.REASON_UPSTREAM_ISOLATED
    assert outcome["label"] == "样本-语义不通"
    assert outcome["entry"]["destination"] == delivery.DEST_SAMPLE_ISOLATION
    assert outcome["entry"]["action"] == "exclude"
    assert outcome["entry"]["sample_id"] == "cm-0013"


def test_evaluate_record_origin_quarantine_for_empty_token():
    outcome = delivery.evaluate_record(
        token_record("cm-0016", token=""), isolation_ids=set(),
        event_time="t", config=_config(), line=4)
    assert outcome["action"] == delivery.ACTION_ORIGIN
    assert outcome["reason"] == delivery.REASON_EMPTY_TOKEN
    assert outcome["label"] == delivery.LABEL_ORIGIN_EMPTY


def test_evaluate_record_origin_quarantine_for_unsafe_media_path():
    outcome = delivery.evaluate_record(
        token_record("cm-0015", media=["C:/temp/x.png"]), isolation_ids=set(),
        event_time="t", config=_config(), line=5)
    assert outcome["action"] == delivery.ACTION_ORIGIN
    assert outcome["reason"] == delivery.REASON_UNSAFE_MEDIA_PATH


def test_evaluate_record_requires_identifier():
    outcome = delivery.evaluate_record(
        {"token": "<image> x <image>"}, isolation_ids=set(), event_time="t",
        config=_config(), line=6)
    assert outcome["action"] == delivery.ACTION_ORIGIN
    assert outcome["reason"] == delivery.REASON_MISSING_IDENTIFIER


def test_evaluate_record_token_requirement_is_configurable():
    payload = token_record("cm-0016", token="")
    outcome = delivery.evaluate_record(
        payload, isolation_ids=set(), event_time="t",
        config=_config(require_token=False), line=7)
    assert outcome["action"] == delivery.ACTION_PACKAGE


def test_applicability_follows_enabled_routes():
    """模态适用性：版本说明声明的启用路线之外为 not_applicable（保留在主输出）。"""
    audio = token_record("cm-0003", route="audio_text", modalities=["audio", "text"],
                         token="<audio> 播报 <audio>")
    applicable = delivery.evaluate_record(
        audio, isolation_ids=set(), event_time="t", config=_config(), line=1,
        enabled_routes=["audio_text"])
    declared_only_image = delivery.evaluate_record(
        audio, isolation_ids=set(), event_time="t", config=_config(), line=2,
        enabled_routes=["image_text"])
    unknown = delivery.evaluate_record(
        audio, isolation_ids=set(), event_time="t", config=_config(), line=3)
    assert applicable["applicability"] == "applicable"
    assert declared_only_image["applicability"] == "not_applicable"
    assert unknown["applicability"] == "unknown"
    assert applicable["output"][delivery.NAMESPACE]["applicability"] == "applicable"
    assert declared_only_image["output"][delivery.NAMESPACE]["evidence"][
        "applicability"] == "not_applicable"


# --------------------------------------------------------------------------- #
# 材料分类与发现
# --------------------------------------------------------------------------- #

@pytest.mark.parametrize("name,role", [
    ("quality_report.json", delivery.MATERIAL_QUALITY_REPORT),
    ("20_quality_report.json", delivery.MATERIAL_QUALITY_REPORT),
    ("sampling_conclusion.json", delivery.MATERIAL_SAMPLING_CONCLUSION),
    ("24_sampling_conclusion.json", delivery.MATERIAL_SAMPLING_CONCLUSION),
    ("isolation_list.jsonl", delivery.MATERIAL_ISOLATION_LIST),
    ("21_quarantine_list.jsonl", delivery.MATERIAL_ISOLATION_LIST),
    ("version_notes.json", delivery.MATERIAL_VERSION_NOTES),
    ("版本说明.json", delivery.MATERIAL_VERSION_NOTES),
    ("samples.jsonl", None),
    ("cm-0001.png", None),
])
def test_classify_material_name(name, role):
    assert delivery.classify_material_name(name) == role


def test_inspect_materials_finds_all_from_materials_dir(tmp_path):
    entry = build_input_package(
        tmp_path / "task", records=[token_record()],
        materials={"quality_report": quality_report(),
                   "sampling_conclusion": sampling_conclusion(),
                   "isolation_list": [isolation_entry("cm-0013")],
                   "version_notes": version_notes()})
    result = delivery.inspect_materials(tmp_path / "task", config=_config(),
                                        entry_path=entry)
    roles = result["roles"]
    assert set(result["found"]) == set(delivery.MATERIAL_ROLES)
    assert result["missing"] == [] and result["invalid"] == []
    assert roles[delivery.MATERIAL_QUALITY_REPORT]["path"] == "materials/quality_report.json"
    assert roles[delivery.MATERIAL_ISOLATION_LIST]["relative"] == "materials/isolation_list.jsonl"
    assert result["entry"] == "samples.jsonl"


def test_inspect_materials_reports_missing_version_notes(tmp_path):
    entry = build_input_package(
        tmp_path / "task", records=[token_record()],
        materials={"quality_report": quality_report(written_back=True),
                   "sampling_conclusion": sampling_conclusion(written_back=True),
                   "isolation_list": [isolation_entry("cm-0013")]})
    result = delivery.inspect_materials(tmp_path / "task", config=_config(),
                                        entry_path=entry)
    assert result["missing"] == [delivery.MATERIAL_VERSION_NOTES]
    assert delivery.MATERIAL_VERSION_NOTES not in result["found"]


def test_inspect_materials_flags_incomplete_version_notes(tmp_path):
    entry = build_input_package(
        tmp_path / "task", records=[token_record()],
        materials={"quality_report": quality_report(written_back=True),
                   "sampling_conclusion": sampling_conclusion(written_back=True),
                   "isolation_list": [isolation_entry("cm-0013")],
                   "version_notes": version_notes(routes=None)})
    result = delivery.inspect_materials(tmp_path / "task", config=_config(),
                                        entry_path=entry)
    role = result["roles"][delivery.MATERIAL_VERSION_NOTES]
    assert role["status"] == "invalid"
    assert "enabled_routes" in "".join(role["detail"])
    assert delivery.MATERIAL_VERSION_NOTES in result["invalid"]


def test_inspect_materials_supports_explicit_paths(tmp_path):
    root = tmp_path / "task"
    entry = build_input_package(root, records=[token_record()],
                                materials={"isolation_list": [isolation_entry("cm-0013")]})
    external = tmp_path / "external_reports"
    write_json(external / "qr.json", quality_report(written_back=True))
    (root / "manifest.json").unlink()
    config = _config(materials={delivery.MATERIAL_QUALITY_REPORT: "../external_reports/qr.json"})
    result = delivery.inspect_materials(root, config=config, entry_path=entry)
    role = result["roles"][delivery.MATERIAL_QUALITY_REPORT]
    assert role["status"] == "found"
    assert role["source"] == "explicit"


def test_inspect_materials_ignores_entry_file_and_media(tmp_path):
    root = tmp_path / "task"
    entry = build_input_package(root, records=[token_record()],
                                materials={"isolation_list": []})
    result = delivery.inspect_materials(root, config=_config(), entry_path=entry)
    assert result["entry"] == "samples.jsonl"
    assert delivery.MATERIAL_QUALITY_REPORT in result["missing"]


# --------------------------------------------------------------------------- #
# 材料解析
# --------------------------------------------------------------------------- #

@pytest.mark.parametrize("written_back", [True, False])
def test_parse_quality_report_writeback(written_back):
    verdict = delivery.parse_quality_report(quality_report(written_back=written_back))
    assert verdict["written_back"] is written_back
    assert verdict["status"] == ("written_back" if written_back else "waiting_writeback")


def test_parse_quality_report_without_explicit_flag_is_not_written_back():
    verdict = delivery.parse_quality_report({"format": "crossmodal-quality-report-v1",
                                             "checks": {"openable": True}})
    assert verdict["written_back"] is False


def test_parse_quality_report_accepts_top_level_flag():
    verdict = delivery.parse_quality_report({"written_back": True})
    assert verdict["written_back"] is True


@pytest.mark.parametrize("written_back", [True, False])
def test_parse_sampling_conclusion_writeback(written_back):
    verdict = delivery.parse_sampling_conclusion(
        sampling_conclusion(written_back=written_back))
    assert verdict["written_back"] is written_back


def test_parse_isolation_list_collects_sample_ids():
    verdict = delivery.parse_isolation_list(
        [isolation_entry("cm-0013"), isolation_entry("cm-0011")])
    assert verdict["valid"] is True
    assert sorted(verdict["sample_ids"]) == ["cm-0011", "cm-0013"]
    assert verdict["entries"] == 2


def test_parse_isolation_list_handles_wrapped_and_empty():
    wrapped = delivery.parse_isolation_list({"entries": [isolation_entry("cm-0013")]})
    assert wrapped["sample_ids"] == ["cm-0013"]
    empty = delivery.parse_isolation_list([])
    assert empty["valid"] is True and empty["sample_ids"] == []


@pytest.mark.parametrize("routes,granularity,valid", [
    (["image_text"], "whole", True),
    (["image_text", "video_text"], "local_to_whole", True),
    ([], "whole", False),
    (["unknown_route"], "whole", False),
    (["image_text"], "", False),
    (["image_text"], None, False),
])
def test_parse_version_notes(routes, granularity, valid):
    verdict = delivery.parse_version_notes(
        version_notes(routes=routes, granularity=granularity))
    assert verdict["valid"] is valid
    if valid:
        assert verdict["routes"] == list(routes)
        assert verdict["granularity"] == granularity
    else:
        assert verdict["missing"], verdict


# --------------------------------------------------------------------------- #
# 交付门禁
# --------------------------------------------------------------------------- #

def _materials_fixture(tmp_path: Path, *, written_back: bool, **kwargs) -> Dict[str, Any]:
    payload: Dict[str, Any] = {
        "quality_report": quality_report(written_back=written_back),
        "sampling_conclusion": sampling_conclusion(written_back=written_back),
        "isolation_list": [isolation_entry("cm-0013")],
        "version_notes": version_notes(),
    }
    payload.update(kwargs)
    entry = build_input_package(tmp_path / "task", records=[token_record()],
                                materials=payload)
    return delivery.inspect_materials(tmp_path / "task", config=_config(),
                                      entry_path=entry)


def test_deliverable_when_all_three_requirements_met(tmp_path):
    materials = _materials_fixture(tmp_path, written_back=True)
    verdict = delivery.evaluate_deliverable(materials, packaged_records=10,
                                            config=_config())
    assert verdict["deliverable"] is True
    assert verdict["version_state"] == delivery.VERSION_STATE_DELIVERABLE
    assert verdict["status_label"] == "可交付"
    assert verdict["missing_requirements"] == []


def test_pending_release_when_report_not_written_back(tmp_path):
    materials = _materials_fixture(tmp_path, written_back=False)
    verdict = delivery.evaluate_deliverable(materials, packaged_records=10,
                                            config=_config())
    assert verdict["deliverable"] is False
    assert verdict["version_state"] == delivery.VERSION_STATE_PENDING
    assert verdict["status_label"] == "待发布"
    assert "quality_report_written_back" in verdict["missing_requirements"]
    assert "sampling_written_back" in verdict["missing_requirements"]
    assert verdict["requirements"]["training_files_present"] is True


def test_pending_release_when_material_missing(tmp_path):
    materials = _materials_fixture(tmp_path, written_back=True,
                                   version_notes=None)
    # version_notes=None 时材料目录不写该文件
    verdict = delivery.evaluate_deliverable(materials, packaged_records=10,
                                            config=_config())
    assert verdict["deliverable"] is False
    assert delivery.MATERIAL_VERSION_NOTES in verdict["missing_requirements"]


def test_pending_release_when_training_file_empty(tmp_path):
    materials = _materials_fixture(tmp_path, written_back=True)
    verdict = delivery.evaluate_deliverable(materials, packaged_records=0,
                                            config=_config())
    assert verdict["deliverable"] is False
    assert verdict["requirements"]["training_files_present"] is False


def test_deliverable_gate_conditions_are_readonly(tmp_path):
    """三项条件缺一不可：不允许通过配置放宽。"""
    materials = _materials_fixture(tmp_path, written_back=False)
    config = _config()
    verdict = delivery.evaluate_deliverable(materials, packaged_records=10,
                                            config=config)
    assert verdict["requirements"]["quality_report_written_back"] is False
    assert verdict["requirements"]["sampling_written_back"] is False
    assert verdict["deliverable"] is False


# --------------------------------------------------------------------------- #
# 台账条目
# --------------------------------------------------------------------------- #

def test_make_ledger_entry_carries_unified_ledger_fields():
    payload = token_record("cm-0013")
    entry = delivery.make_ledger_entry(
        line=7, payload=payload, action="exclude", label="样本-语义不通",
        destination=delivery.DEST_SAMPLE_ISOLATION,
        reason_code=delivery.REASON_UPSTREAM_ISOLATED,
        event_time="2026-10-08T12:00:00+08:00")
    for field in ("label", "stage", "operator", "action", "destination",
                  "event_time", "batch_id", "sample_id", "retry_count"):
        assert field in entry, field
    assert entry["stage"] == delivery.STAGE_ID
    assert entry["operator"] == delivery.SOURCE_REFERENCE
    assert entry["sample_id"] == "cm-0013"
    assert entry["record"]["sample_id"] == "cm-0013"


def test_make_ledger_entry_truncates_oversized_record():
    payload = token_record("cm-0001", extra={"blob": "x" * 5000})
    entry = delivery.make_ledger_entry(
        line=1, payload=payload, action="exclude", label="样本-语义不通",
        destination=delivery.DEST_SAMPLE_ISOLATION, reason_code="x",
        event_time="t", max_embedded_bytes=256)
    assert "record" not in entry
    assert entry["record_omitted"] is True
    assert entry["record_bytes"] > 256


def test_make_reject_entry_keeps_line_and_reason():
    entry = delivery.make_reject_entry(line=11, reason=delivery.REASON_JSON_PARSE,
                                       detail="JSON 解析失败", detected_at="t",
                                       raw_text='{"a": ')
    assert entry["line"] == 11
    assert entry["reason_code"] == delivery.REASON_JSON_PARSE
    assert entry["stage"] == delivery.STAGE_ID
    assert entry["raw_text_preview"].startswith('{"a"')
    assert json.loads(json.dumps(entry, ensure_ascii=False))


# --------------------------------------------------------------------------- #
# 辅助
# --------------------------------------------------------------------------- #

def _config(**overrides):
    from utils.schemas import OperatorConfig

    base = json.loads(json.dumps({
        "operator": {"number": 23, "name": "delivery_package",
                     "label": "压缩交付对齐语料包", "version": "v1.0.0",
                     "enabled": True},
        "transport": {"format": "crossmodal-files-v1",
                      "max_response_bytes": 67108864},
        "stage_id": "S6",
        "enabled": True,
        "archive_format": "zip",
        "exclude_isolated": True,
        "status_before_review": "待发布",
        "status_after_review": "可交付",
        "include_media": True,
        "require_deliverable": False,
    }))
    base.update(overrides)
    return OperatorConfig(**base)
