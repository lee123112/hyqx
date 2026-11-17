"""交付样例（dataset/sample）端到端测试：样例即真实产物，三态各自可复现。

样例由 ``dataset/sample/make_sample.py`` 确定性生成，不联网、不使用外部大数据。
"""
from __future__ import annotations

import json
import zipfile
from pathlib import Path

import pytest

import inference
from utils import delivery

SAMPLES = Path(__file__).resolve().parents[1] / "dataset" / "sample"


def _run(entry: Path, output_root: Path, unique_id: str):
    return inference.run(input_file=str(entry), unique_id=unique_id,
                         output_root=str(output_root))


def _require_sample() -> None:
    if not (SAMPLES / "demo.jsonl").is_file():
        pytest.skip("样例未生成，请先运行 dataset/sample/make_sample.py")


def test_sample_demo_is_pending_release(config_file, output_root, tmp_path):
    _require_sample()
    result = _run(SAMPLES / "demo.jsonl", output_root, "sample_pending")
    stats = result["statistics"]
    assert result["status"] == "degraded"
    assert result["versionState"] == delivery.VERSION_STATE_PENDING
    assert result["packageName"] == delivery.PACKAGE_PENDING_NAME
    assert result["materials"]["missing"] == [] and result["materials"]["invalid"] == []
    assert stats["input_records"] == 18
    assert stats["valid_records"] == 16
    assert stats["packaged_records"] == 10
    assert stats["excluded_isolated"] == 3
    assert stats["origin_quarantined"] == 3
    assert stats["bad_lines"] == 2
    assert stats["blank_lines"] == 1
    assert stats["conservation_ok"] is True
    assert stats["deliverable_requirements"] == {
        "training_files_present": True,
        "quality_report_written_back": False,
        "sampling_written_back": False}
    assert stats["media_carried"] == 4 and stats["media_missing"] == 1
    assert stats["external_references"] == 1


def test_sample_deliverable_scenario_is_deliverable(config_file, output_root):
    _require_sample()
    result = _run(SAMPLES / "deliverable" / "demo.jsonl", output_root,
                  "sample_deliverable")
    assert result["status"] == "success"
    assert result["versionState"] == delivery.VERSION_STATE_DELIVERABLE
    assert result["packageName"] == delivery.PACKAGE_DELIVERABLE_NAME
    assert result["statistics"]["conservation_ok"] is True
    with zipfile.ZipFile(result["packagePath"]) as bundle:
        names = bundle.namelist()
        manifest = json.loads(bundle.read("manifest.json").decode("utf-8"))
        version = json.loads(bundle.read("version.json").decode("utf-8"))
        train_lines = bundle.read("train/samples.jsonl").decode("utf-8").strip().splitlines()
        isolation = bundle.read("materials/isolation_list.jsonl").decode("utf-8")
    assert "media/cm-0001.png" in names and "media/cm-0003.wav" in names
    assert manifest["deliverable"] is True
    assert manifest["version_state"] == delivery.VERSION_STATE_DELIVERABLE
    assert manifest["missing_requirements"] == []
    assert version["enabled_routes"] == ["image_text", "audio_text", "video_text"]
    assert version["annotation_granularity"] == "whole"
    assert version["status"] == "可交付"
    assert len(train_lines) == 10
    # 隔离样本既不在训练文件，也不在包内媒体清单里
    assert all("cm-0011" not in line and "cm-0012" not in line
               and "cm-0013" not in line for line in train_lines)
    assert "cm-0011" in isolation


def test_sample_incomplete_scenario_stays_pending(config_file, output_root):
    _require_sample()
    result = _run(SAMPLES / "incomplete" / "demo.jsonl", output_root,
                  "sample_incomplete")
    assert result["status"] == "degraded"
    assert result["versionState"] == delivery.VERSION_STATE_PENDING
    assert delivery.MATERIAL_VERSION_NOTES in result["materials"]["missing"]
    assert result["statistics"]["deliverable_requirements"][
        "quality_report_written_back"] is True
    assert result["statistics"]["deliverable_requirements"][
        "training_files_present"] is True


def test_sample_run_directory_is_self_contained(config_file, output_root):
    _require_sample()
    result = _run(SAMPLES / "deliverable" / "demo.jsonl", output_root,
                  "sample_selfcontained")
    run_dir = Path(result["runDir"])
    # [v4.2] 血缘输出已停用：run 目录不再生成 lineage.json / stage_context.json
    for name in (delivery.MAIN_NAME, delivery.EXCLUDED_NAME, delivery.MATERIALS_NAME,
                 delivery.REJECTS_NAME, delivery.LOG_NAME, result["packageName"],
                 "manifest.json"):
        assert (run_dir / name).is_file(), name
    assert not (run_dir / "lineage.json").exists()
    assert not (run_dir / "stage_context.json").exists()
    assert (run_dir / "media" / "cm-0001.png").is_file()
    with zipfile.ZipFile(run_dir / result["packageName"]) as bundle:
        names = bundle.namelist()
    assert len(names) == len(set(names))
    # 包内成员路径全部为相对路径
    assert not any(name.startswith(("/", "C:")) or ".." in name for name in names)


def test_sample_isolated_records_never_enter_package(config_file, output_root):
    _require_sample()
    result = _run(SAMPLES / "deliverable" / "demo.jsonl", output_root,
                  "sample_isolation_guard")
    with zipfile.ZipFile(result["packagePath"]) as bundle:
        blob = "\n".join(bundle.read(name).decode("utf-8", errors="replace")
                         for name in bundle.namelist()
                         if name.startswith(("train/", "materials/quality",
                                             "materials/sampling")))
    for isolated in ("cm-0011", "cm-0012", "cm-0013"):
        assert isolated not in blob, isolated
    excluded = Path(result["excludedFile"]).read_text(encoding="utf-8").strip().splitlines()
    assert len(excluded) == 3
    rejects = Path(result["rejectsFile"]).read_text(encoding="utf-8").strip().splitlines()
    assert len(rejects) == 5
