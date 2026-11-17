"""HTTP path contract and already fixed JIT loading, without real model loads."""
import sys
from types import SimpleNamespace

import pytest

import inference
from fixtures import (StubEncoder, build_input_tree, default_frames, default_media,
                      make_flat_record, read_jsonl)
from utils import backends, video_text


@pytest.mark.parametrize("enabled", [False, True])
def test_declared_temporal_route_override_is_accepted_and_audited(config_file, enabled):
    overrides = {"enabled": enabled, "temporal_route_exclusion": False}
    config = inference.load_config(config_file, overrides)
    assert config.enabled is enabled
    assert config.temporal_route_exclusion is False
    snapshot = inference.config_snapshot(config, overrides)
    assert snapshot["business"]["temporal_route_exclusion"] is False
    assert "temporal_route_exclusion" in snapshot["overridden_keys"]


@pytest.mark.parametrize("enabled", ["false", "true", 0, 1])
def test_route_enabled_requires_a_real_boolean(config_file, enabled):
    with pytest.raises(inference.ConfigError):
        inference.load_config(config_file, {"enabled": enabled})


def test_http_explicit_input_is_relative_to_current_task(tmp_path, monkeypatch):
    task = tmp_path / "task"
    entry = task / "input" / "candidate_records.jsonl"
    entry.parent.mkdir(parents=True)
    entry.write_text('{"sample_id":"s1"}\n', encoding="utf-8")
    cwd = tmp_path / "server"
    cwd.mkdir()
    monkeypatch.chdir(cwd)
    assert inference.resolve_input("input/candidate_records.jsonl", task, False) == entry.resolve()


def test_cli_relative_input_keeps_process_working_directory(tmp_path, monkeypatch):
    entry = tmp_path / "candidate_records.jsonl"
    entry.write_text('{}\n', encoding="utf-8")
    monkeypatch.chdir(tmp_path)
    assert inference.resolve_input(entry.name, tmp_path / "task", True) == entry.resolve()


@pytest.mark.parametrize("input_name", ["../outside.jsonl", "input/../../outside.jsonl"])
def test_http_relative_input_cannot_escape_current_task(tmp_path, input_name):
    outside = tmp_path / "outside.jsonl"
    outside.write_text('{}\n', encoding="utf-8")
    with pytest.raises(inference.InputError, match="越出当前任务"):
        inference.resolve_input(input_name, tmp_path / "task", False)


def test_http_bare_filename_does_not_implicitly_add_input(tmp_path, monkeypatch):
    task = tmp_path / "task"
    entry = task / "input" / "candidate_records.jsonl"
    entry.parent.mkdir(parents=True)
    entry.write_text('{}\n', encoding="utf-8")
    monkeypatch.chdir(tmp_path)
    with pytest.raises(inference.InputError, match="不存在"):
        inference.resolve_input(entry.name, task, False)


@pytest.mark.parametrize("torch_load_raises", [False, True])
def test_existing_jit_loader_handles_script_module_and_unicode_path(tmp_path, monkeypatch,
                                                                  torch_load_raises):
    weights = tmp_path / "中文权重" / "ViT-B-32.pt"
    weights.parent.mkdir()
    weights.write_bytes(b"local-test-archive")
    module = SimpleNamespace(state_dict=lambda: {"module.visual.proj": "weight"})
    calls = []

    def load(path, map_location):
        assert path == str(weights)
        assert map_location == "cpu"
        if torch_load_raises:
            raise RuntimeError("torch<2.4 archive dispatch")
        return module

    def jit_load(handle, map_location):
        assert hasattr(handle, "read")
        assert handle.read() == b"local-test-archive"
        assert map_location == "cpu"
        calls.append("jit_file_handle")
        return module

    model = SimpleNamespace(load_state_dict=lambda state, strict: calls.append((state, strict)))
    monkeypatch.setitem(sys.modules, "torch", SimpleNamespace(load=load,
                                                             jit=SimpleNamespace(load=jit_load)))
    backends._load_state_dict_into(model, weights)
    assert calls[-1] == ({"visual.proj": "weight"}, False)
    assert ("jit_file_handle" in calls) is torch_load_raises


def test_no_video_does_not_become_score_or_passed(config_file):
    config = inference.load_config(config_file, {"enabled": True})
    spec = video_text.resolve_pair({"sample_id": "image-only", "text": "图片描述"})
    result = video_text.evaluate_record(spec, 1, media_roots=[], config=config,
                                        encoder=None, event_time="2026-11-09T12:00:00+08:00")
    assert result["reason"] == "no_video_text_pair"
    assert result["node"]["process_status"] == "pending"
    assert result["node"]["passed"] is None
    assert result["node"]["video_text_score"] is None


def test_run_keeps_temporal_record_in_main_stream_and_counts_written_rows(tmp_path, config_file):
    temporal = make_flat_record("temporal", "时间轴描述", default_frames(1),
                               timeline={"text": [{"start_sec": 0, "end_sec": 1,
                                                   "content": "时间轴描述"}]},
                               temporal_bindings=[{"text": "时间轴描述", "start": 0, "end": 1}],
                               transcription={"text": "保留上游转写", "model": "whisper-base"})
    records = [temporal,
               make_flat_record("passed", "正常视频", default_frames(1)),
               make_flat_record("isolated", "错配视频", default_frames(1)),
               {"sample_id": "no-video", "text": "图片描述"},
               make_flat_record("no-frames", "尚未抽帧", []),
               {"sample_id": "no-description", "video": {
                   "path": "video/clip.mp4", "frames": default_frames(1)}}]
    source = build_input_tree(tmp_path / "input", records, media=default_media())
    encoder = StubEncoder(scores={"错配视频": 0.05}, default=0.9)
    result = inference.run(str(source / "demo.jsonl"), "temporal_stream",
                           config_path=config_file, output_root=tmp_path / "output",
                           overrides={"enabled": True}, encoder=encoder)
    rows = read_jsonl(result["outputFile"])
    assert {row["sample_id"] for row in rows} == {
        "temporal", "passed", "no-video", "no-frames", "no-description"}
    retained = next(row for row in rows if row["sample_id"] == "temporal")
    assert retained["timeline"] == temporal["timeline"]
    assert retained["temporal_bindings"] == temporal["temporal_bindings"]
    assert retained["transcription"] == temporal["transcription"]
    node = retained["processing"]["14"]
    assert node["process_status"] == "not_applicable"
    assert node["passed"] is None
    assert node["video_text_score"] is None
    assert retained["video_text_score_value"] is None
    ledger = read_jsonl(result["notApplicableFile"])
    assert len(ledger) == 1
    assert ledger[0]["sample_id"] == "temporal"
    assert ledger[0]["video_text"]["process_status"] == "not_applicable"
    assert result["statistics"]["output_records"] == len(rows) == 5
    assert result["statistics"]["not_applicable_records"] == 1
    assert result["statistics"]["passed_records"] == 1
    assert result["statistics"]["scored_records"] == 2
    assert result["statistics"]["sample_isolated"] == 1
    assert result["statistics"]["pending_records"] == 3
    assert result["statistics"]["conservation_ok"] is True
    assert "时间轴描述" not in encoder.text_calls


def test_run_with_only_temporal_records_keeps_all_without_scores(tmp_path, config_file):
    records = [{"sample_id": "t%d" % number,
                "timeline": {"text": [{"start_sec": 0, "end_sec": 1, "content": "时序"}]}}
               for number in range(2)]
    source = build_input_tree(tmp_path / "input", records)
    encoder = StubEncoder()
    result = inference.run(str(source / "demo.jsonl"), "all_temporal_stream",
                           config_path=config_file, output_root=tmp_path / "output",
                           overrides={"enabled": True}, encoder=encoder)
    rows = read_jsonl(result["outputFile"])
    assert len(rows) == 2
    assert len(read_jsonl(result["notApplicableFile"])) == 2
    assert result["statistics"]["output_records"] == 2
    assert result["statistics"]["not_applicable_records"] == 2
    assert result["statistics"]["scored_records"] == 0
    assert result["statistics"]["passed_records"] == 0
    assert result["statistics"]["conservation_ok"] is True
    assert all(row["processing"]["14"]["passed"] is None for row in rows)
    assert all(row["video_text_score_value"] is None for row in rows)
    assert encoder.text_calls == [] and encoder.image_calls == []
