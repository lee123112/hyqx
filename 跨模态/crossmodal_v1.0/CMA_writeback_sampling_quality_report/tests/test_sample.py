"""仓库样例回归：样例必须可复现，且端到端真实统计与文档一致。"""
from __future__ import annotations

import importlib.util
import json
from pathlib import Path

import pytest

import inference
from utils import spot_check as sc

BASE_DIR = Path(__file__).resolve().parent.parent
SAMPLE_DIR = BASE_DIR / "dataset" / "sample"


def _load_generator():
    spec = importlib.util.spec_from_file_location("op24_make_sample",
                                                 SAMPLE_DIR / "make_sample.py")
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def run_sample(tmp_path, unique_id, name="demo.jsonl", overrides=None):
    return inference.run(input_file=str(SAMPLE_DIR / name), unique_id=unique_id,
                         output_root=tmp_path / "out", overrides=overrides,
                         allow_external_input=True)


def test_shipped_sample_is_reproducible():
    module = _load_generator()
    records = module.build_records()
    module.attach_reviews(records, module.SAMPLE_RATE)
    expected = "\n".join(module.json_lines(records) + module.abnormal_lines(records)) + "\n"
    assert (SAMPLE_DIR / "demo.jsonl").read_text(encoding="utf-8") == expected

    pending = module.json_lines(module.build_records())
    assert (SAMPLE_DIR / "demo_pending.jsonl").read_text(encoding="utf-8") == \
        "\n".join(pending) + "\n"

    upstream = json.loads((SAMPLE_DIR / "20_quality_report.json").read_text(
        encoding="utf-8"))
    assert upstream == json.loads(json.dumps(module.build_report(len(records)),
                                            ensure_ascii=False))

    from utils import transport

    manifest = transport.build_manifest("demo.jsonl", [
        {"path": "demo.jsonl", "role": "candidate_pool"},
        {"path": "demo_pending.jsonl", "role": "candidate_pool_without_review"},
        {"path": "20_quality_report.json", "role": "upstream_quality_report"},
    ])
    assert (SAMPLE_DIR / "manifest.json").read_text(encoding="utf-8") == \
        json.dumps(manifest, ensure_ascii=False, indent=2)


def test_sample_files_are_utf8_without_bom():
    for name in ("demo.jsonl", "demo_pending.jsonl", "20_quality_report.json"):
        raw = (SAMPLE_DIR / name).read_bytes()
        assert not raw.startswith(b"\xef\xbb\xbf")
        raw.decode("utf-8")


def test_default_run_on_sample_matches_documented_numbers(tmp_path):
    result = run_sample(tmp_path, "sample_default")
    stats = result["statistics"]
    assert (stats["input_lines"], stats["blank_lines"], stats["input"]) == (45, 1, 44)
    assert (stats["rejected"], stats["valid"]) == (2, 42)
    assert (stats["origin_quarantined"], stats["duplicates"]) == (2, 1)
    assert stats["sampled"] == 4
    assert stats["sample_target"] == 4
    assert stats["achieved_sample_rate"] == pytest.approx(0.10)
    assert len(stats["strata"]) == 4
    assert stats["sampled_passed"] == 3
    assert stats["sampled_rejected"] == 1
    assert stats["sampled_in_output"] == 3
    assert stats["sample_quarantined"] == 1
    assert stats["auto_alignment_cancelled"] == 1
    assert stats["output"] == 39
    assert stats["not_sampled"] == 36
    assert stats["disputes"] == 1
    assert stats["pending_manual"] == 0
    assert stats["human_verified_records"] == 4
    assert stats["review_required_records"] == 0
    assert stats["conservation_ok"] is True
    assert stats["conservation_failures"] == []
    assert result["status"] == "partial"
    assert result["reportStatus"] == sc.REPORT_STATUS_WRITTEN


def test_max_rate_run_on_sample_keeps_writeback_pending(tmp_path):
    result = run_sample(tmp_path, "sample_max", overrides={"sample_rate": 0.20})
    stats = result["statistics"]
    assert stats["sampled"] == 8
    assert stats["achieved_sample_rate"] == pytest.approx(0.20)
    assert (stats["sampled_passed"], stats["sampled_rejected"]) == (4, 2)
    assert (stats["sampled_uncertain"], stats["sampled_pending"]) == (1, 1)
    assert stats["sample_quarantined"] == 2
    assert stats["auto_alignment_cancelled"] == 1
    assert stats["output"] == 38
    assert stats["disputes"] == 3
    assert stats["pending_manual"] == 1
    assert stats["review_required_records"] == 2
    assert stats["conservation_ok"] is True
    assert result["status"] == "waiting_manual"
    assert result["reportStatus"] == sc.REPORT_STATUS_WAITING


def test_pending_sample_never_seals_the_report(tmp_path):
    result = run_sample(tmp_path, "sample_pending", name="demo_pending.jsonl")
    stats = result["statistics"]
    assert (stats["input"], stats["sampled"], stats["sampled_pending"]) == (40, 4, 4)
    assert stats["writeback"]["completed"] is False
    assert stats["conservation_ok"] is True
    assert result["status"] == "waiting_manual"
    report = json.loads((Path(result["runDir"]) / sc.REPORT_NAME).read_text(
        encoding="utf-8"))
    assert report["status"] == sc.REPORT_STATUS_WAITING
    assert report["writeback"]["version_state"] == sc.VERSION_STATE_PENDING
    assert report["writeback"]["gate"]["reason"] == "sampled_without_final_conclusion"


def test_sample_report_is_written_back_without_touching_upstream_fields(tmp_path):
    result = run_sample(tmp_path, "sample_report")
    run_dir = Path(result["runDir"])
    report = json.loads((run_dir / sc.REPORT_NAME).read_text(encoding="utf-8"))
    upstream = json.loads((SAMPLE_DIR / "20_quality_report.json").read_text(
        encoding="utf-8"))
    # 上游结论只增不改
    assert report["checks"] == upstream["checks"]
    assert report["metrics"] == upstream["metrics"]
    assert report["statistics"] == upstream["statistics"]
    assert report["format"] == upstream["format"]
    assert report["status"] == sc.REPORT_STATUS_WRITTEN
    assert report["original_status"] == upstream["status"]
    assert report["source_report"]["available"] is True
    # 判错样本被取消自动对齐，并如实登记在报告回写段
    quarantine = [json.loads(line) for line in
                  (run_dir / sc.SAMPLE_QUARANTINE_NAME).read_text(
                      encoding="utf-8").splitlines() if line.strip()]
    assert [item["sample_id"] for item in quarantine] == ["sp-img-07"]
    assert report["writeback"]["cancelled_auto_alignment"] == ["sp-img-07"]
    assert quarantine[0]["record"]["alignment"]["status"] == sc.ALIGNMENT_NOT
    assert quarantine[0]["record"]["alignment"]["auto_aligned"] is False
    assert report["operator_24"]["artifacts"]["main"] == sc.MAIN_NAME
    # 抽检样本明细里的复核人来自输入证据，不是算子自己生成的
    sampled = [json.loads(line) for line in (run_dir / sc.SAMPLED_NAME).read_text(
        encoding="utf-8").splitlines() if line.strip()]
    assert len(sampled) == 4
    assert {item["review"]["reviewer"] for item in sampled} == {
        "reviewer-07", "reviewer-02", "reviewer-04", "reviewer-05"}
