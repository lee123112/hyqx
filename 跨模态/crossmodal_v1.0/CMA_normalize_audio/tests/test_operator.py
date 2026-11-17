"""算子 6 业务层测试：纯函数、真实媒体解码、run 统计守恒与血缘。

原则（DEVELOPMENT_CONTRACT §6）：
- 断言真实产物（写出的 WAV 复读参数、RMS、时长、媒体文件存在），不伪造分数；
- 缺后端时验证的是**显式降级**（dependency_missing），不是编造成功。
"""
from __future__ import annotations

import json
import struct
import wave
from pathlib import Path

import numpy as np
import pytest

from tests.conftest import CONFIG_PATH, SAMPLE_DIR, core, write_config
from utils import io as io_utils
from utils.audio_processing import (
    AudioMetadataError,
    ClipRangeError,
    DependencyMissingError,
    MediaUnreadableError,
    ResourceLimitError,
    UnsupportedAudioFormatError,
    pcm_bytes_to_mono,
    quantize_to_pcm,
    read_wav_summary,
    resample_pcm,
    rms,
)
from utils.schemas import ConfigError, OperatorConfig

MEDIA = SAMPLE_DIR / "media"
DEMO = SAMPLE_DIR / "demo.jsonl"
DEMO_BAD = SAMPLE_DIR / "demo_bad.jsonl"

FFMPEG_UNAVAILABLE_REASONS = {"dependency_missing", "media_unreadable", "unsupported_format"}


# ---------------------------------------------------------------- 纯函数

def test_resample_preserves_duration_and_signal():
    source_rate, target_rate = 8000, 16000
    index = np.arange(source_rate, dtype=np.float64)
    samples = (0.4 * np.sin(2 * np.pi * 440.0 * index / source_rate)).astype(np.float32)

    output = resample_pcm(samples, source_rate, target_rate)

    assert output.dtype == np.float32
    assert len(output) == 2 * source_rate
    assert abs(len(output) / target_rate - len(samples) / source_rate) < 1e-3
    assert rms(output) > 0.0


def test_resample_same_rate_returns_equal_length_copy():
    samples = np.linspace(-0.5, 0.5, 1000, dtype=np.float32)
    output = resample_pcm(samples, 16000, 16000)
    assert len(output) == len(samples)
    assert np.allclose(output, samples, atol=1e-6)


def test_pcm_bytes_to_mono_averages_channels():
    # 左 +1.0、右 -1.0 -> 平均为 0；再一帧 左 -1.0、右 -1.0 -> -1.0
    raw = struct.pack("<hhhh", 32767, -32768, -32768, -32768)
    mono = pcm_bytes_to_mono(raw, 2, 2)
    assert mono.shape == (2,)
    assert abs(float(mono[0])) < 1e-4
    assert float(mono[1]) <= -0.99


def test_quantize_int16_roundtrip():
    samples = np.array([0.0, 0.5, -0.5], dtype=np.float32)
    raw = quantize_to_pcm(samples, 16)
    values = np.frombuffer(raw, dtype="<i2").astype(np.float64)
    assert values[0] == 0
    assert abs(values[1] / 32768.0 - 0.5) < 1e-3
    assert abs(values[2] / 32768.0 + 0.5) < 1e-3


def test_rms_is_zero_for_silence():
    assert rms(np.zeros(64, dtype=np.float32)) == 0.0


# ---------------------------------------------------------------- 真实文件标准化

def _cfg(tmp_path: Path, **overrides) -> Path:
    return write_config(tmp_path / "cfg", **overrides)


def test_normalize_audio_resamples_stereo_22050_to_mono_16000(tmp_path):
    cfg = core.load_config(str(_cfg(tmp_path)))
    destination = tmp_path / "out" / "a.wav"

    result = core.normalize_audio(MEDIA / "aud_sine_22050_stereo_1s.wav", destination, cfg)

    summary = read_wav_summary(destination)
    assert summary["sample_rate"] == 16000
    assert summary["channels"] == 1
    assert summary["sampwidth"] == 2
    assert abs(summary["duration"] - 1.0) < 0.01
    assert result["source_sample_rate"] == 22050
    assert result["source_channels"] == 2
    assert result["decoder_backend"] == "ffmpeg"
    assert result["rms"] > 0.0
    assert result["clipped"] is False


def test_normalize_audio_upsamples_8000_to_16000(tmp_path):
    cfg = core.load_config(str(_cfg(tmp_path)))
    destination = tmp_path / "b.wav"
    result = core.normalize_audio(MEDIA / "aud_sine_8000_mono_05s.wav", destination, cfg)
    summary = read_wav_summary(destination)
    assert summary["sample_rate"] == 16000
    assert abs(summary["duration"] - 0.5) < 0.01
    assert result["source_sample_rate"] == 8000


def test_normalize_audio_quantizes_8bit_source_to_16bit(tmp_path):
    cfg = core.load_config(str(_cfg(tmp_path)))
    destination = tmp_path / "c.wav"
    result = core.normalize_audio(MEDIA / "aud_8bit_mono_05s.wav", destination, cfg)
    assert result["pcm_bits"] == 16
    assert read_wav_summary(destination)["sampwidth"] == 2
    assert result["rms"] > 0.0


@pytest.mark.parametrize("bits,width", [(8, 1), (16, 2), (24, 3), (32, 4)])
def test_normalize_audio_supports_all_pcm_bits(tmp_path, bits, width):
    if bits!=16:
        with pytest.raises(ConfigError):core.load_config(str(_cfg(tmp_path,pcm_bits=bits)))
    else:
        cfg=core.load_config(str(_cfg(tmp_path)))
        destination=tmp_path/'pcm16.wav'
        core.normalize_audio(MEDIA/'aud_sine_16000_mono_2s.wav',destination,cfg)
        assert read_wav_summary(destination)['sampwidth']==2


def test_normalize_audio_outputs_two_channels(tmp_path):
    with pytest.raises(ConfigError):core.load_config(str(_cfg(tmp_path,channels=2)))


def test_normalize_audio_clips_within_source_duration(tmp_path):
    cfg=core.load_config(str(_cfg(tmp_path)))
    destination=tmp_path/'whole.wav'
    result=core.normalize_audio(MEDIA/'aud_sine_16000_mono_2s.wav',destination,cfg)
    assert result['clipped'] is False
    assert result['duration']==pytest.approx(2.0,abs=0.01)


def test_normalize_audio_rejects_clip_beyond_duration(tmp_path):
    with pytest.raises(ValueError):
        OperatorConfig(operator={'name':'audio_standardization','version':'v1.1.0'},stage_id='S2',clip_start=0,clip_end=5)


def test_normalize_audio_rejects_over_max_duration(tmp_path):
    cfg = core.load_config(str(_cfg(tmp_path, max_duration_seconds=0.1)))
    with pytest.raises(ResourceLimitError):
        core.normalize_audio(MEDIA / "aud_sine_16000_mono_2s.wav", tmp_path / "x.wav", cfg)


def test_normalize_audio_rejects_empty_wav(tmp_path):
    cfg = core.load_config(str(_cfg(tmp_path)))
    with pytest.raises(AudioMetadataError):
        core.normalize_audio(MEDIA / "aud_empty.wav", tmp_path / "x.wav", cfg)


def test_normalize_audio_rejects_missing_source(tmp_path):
    cfg = core.load_config(str(_cfg(tmp_path)))
    with pytest.raises(MediaUnreadableError):
        core.normalize_audio(MEDIA / "not_exists.wav", tmp_path / "x.wav", cfg)


def test_normalize_audio_broken_wav_degrades_explicitly(tmp_path):
    """损坏 WAV 的期望行为：有 ffmpeg 则回退解码，无 ffmpeg 则显式依赖缺失。

    本地开发环境没有 ffmpeg（见 HANDOFF），因此这里断言的是显式降级原因，
    而不是编造成功——两种情况都必须给出可追溯的 reason。
    """
    cfg = core.load_config(str(_cfg(tmp_path)))
    with pytest.raises((DependencyMissingError, MediaUnreadableError,
                        UnsupportedAudioFormatError)) as excinfo:
        core.normalize_audio(MEDIA / "aud_broken.wav", tmp_path / "x.wav", cfg)
    assert excinfo.value.reason in FFMPEG_UNAVAILABLE_REASONS


# ---------------------------------------------------------------- 配置校验

def test_load_config_defaults_from_shipped_yaml():
    cfg = core.load_config(str(CONFIG_PATH))
    assert isinstance(cfg, OperatorConfig)
    assert cfg.operator.name == "audio_standardization"
    assert cfg.sample_rate == 16000
    assert cfg.channels == 1
    assert cfg.pcm_bits == 16


@pytest.mark.parametrize("overrides,message", [
    ({"sample_rate": 4000}, "sample_rate"),
    ({"sample_rate": "16000"}, "sample_rate"),
    ({"channels": 3}, "channels"),
    ({"pcm_bits": 12}, "pcm_bits"),
    ({"max_duration_seconds": 0}, "max_duration_seconds"),
    ({"clip_start": 1.0}, "clip_start"),
    ({"clip_start": 2.0, "clip_end": 1.0}, "clip_start < clip_end"),
    ({"unknown_key": 1}, "unknown_key"),
])
def test_load_config_rejects_invalid_business_values(overrides, message):
    with pytest.raises(ConfigError) as excinfo:
        core.load_config(str(CONFIG_PATH), overrides)
    assert message.split(" ")[0] in str(excinfo.value)


@pytest.mark.parametrize("blocked", [{"operator": {"name": "x", "version": "1"}},
                                     {"stage_id": "S9"},
                                     {"operator": "x"}])
def test_request_config_cannot_rewrite_identity(blocked):
    with pytest.raises(ConfigError):
        core.load_config(str(CONFIG_PATH), blocked)


def test_request_config_can_close_deployment_clip(tmp_path):
    with pytest.raises(ConfigError):core.load_config(overrides={'clip_start':None,'clip_end':None})


# ---------------------------------------------------------------- run 集成

def _read_jsonl(path: Path):
    with open(path, "r", encoding="utf-8") as stream:
        return [json.loads(line) for line in stream if line.strip()]


def test_media_paths_ignore_provenance_original_path():
    """上游 ``original_path`` 不应被当作当前包内的待搬运文件。"""
    value = {
        "path": "media/sample-0001/image.png",
        "original_path": "04_images/sample-0001.png",
        "frames": [
            {"path": "05_frames/sample-0001/frame_000000.jpg"},
            {"path": "05_frames/sample-0001/frame_000001.jpg", "timestamp": 1.0},
        ],
    }
    assert core._media_paths_of(value) == [
        "media/sample-0001/image.png",
        "05_frames/sample-0001/frame_000000.jpg",
        "05_frames/sample-0001/frame_000001.jpg",
    ]


def _run(tmp_path, unique_id, entry=DEMO, **overrides):
    return core.run(str(entry), unique_id, config_path=str(CONFIG_PATH),
                    output_root=str(tmp_path / "root"), overrides=overrides or None)


def test_run_conservation_and_reject_reasons(tmp_path):
    result = _run(tmp_path, "run_conservation")
    stats = result["statistics"]

    assert result["status"] == "partial"
    assert stats["input"] == 8
    assert stats["output"] == 5
    assert stats["rejected"] == 3
    assert stats["input"] == stats["output"] + stats["rejected"]
    assert stats["conservation_ok"] is True
    assert stats["audio_standardized"] == 4
    assert stats["not_applicable"] == 1
    assert stats["success"] == 4
    assert stats["duplicates"] == 0
    assert stats["decoder_backend"] == "ffmpeg"
    assert stats["audio_resampled"] == 3
    assert stats["reject_reasons"] == {
        "media_unreadable": 2, "invalid_audio_metadata": 1}
    assert stats["artifact_files"] == 2 + stats["media_copied"] + stats["media_generated"]


def test_run_writes_conserved_output_and_referenced_media(tmp_path):
    result = _run(tmp_path, "run_products")
    run_dir = Path(result["runDir"])
    main = _read_jsonl(run_dir / "06_audio_index.jsonl")
    rejects = _read_jsonl(run_dir / "06_rejects.jsonl")

    assert len(main) + len(rejects) == 8
    for record in main:
        for relative in core._media_paths_of(record.get("image")) + \
                core._media_paths_of(record.get("video")):
            assert (run_dir / relative).is_file(), relative
        audio = record.get("audio")
        if audio is None:
            continue
        assert (run_dir / audio["path"]).is_file()
        assert audio["path"].startswith("06_audio/")
        assert (run_dir / audio["original_path"]).is_file()
        summary = read_wav_summary(run_dir / audio["path"])
        assert summary["sample_rate"] == audio["sample_rate"] == 16000
        assert summary["channels"] == audio["channels"] == 1
        assert audio["rms"] > 0.0


def test_run_not_applicable_keeps_full_record_without_fake_audio(tmp_path):
    result = _run(tmp_path, "run_passthrough")
    main = _read_jsonl(Path(result["runDir"]) / "06_audio_index.jsonl")
    record = next(item for item in main if item["sample_id"] == "as_004")

    assert record["audio"] is None
    # [v4.2] 输出记录 status 双字段：int status（1 正常）+ process_status 流程态
    assert record["processing"]["06"]["status"] == 1
    assert record["processing"]["06"]["process_status"] == "not_applicable"
    assert record["processing"]["06"]["reason"] == "no_audio_modality"
    # 上游节点结果与文本 token 偏移必须原样保留
    assert record["processing"]["05"]["status"] == "success"
    assert record["text"]["token_offsets"] == [[0, 2], [2, 4], [4, 6]]
    assert record["image"]["path"].startswith("media/as_004/")


def test_run_media_only_kept_for_successful_records(tmp_path):
    """失败记录不得在 run 目录留下无人引用的媒体。"""
    result = _run(tmp_path, "run_no_orphan")
    run_dir = Path(result["runDir"])
    rejects = _read_jsonl(run_dir / "06_rejects.jsonl")
    rejected_ids = {item.get("sample_id") for item in rejects if item.get("sample_id")}

    assert {"as_005", "as_006", "as_007"} <= rejected_ids
    for sample_id in rejected_ids:
        assert not (run_dir / "media" / sample_id).exists()


def test_run_reject_entries_carry_reason_and_traceability(tmp_path):
    result = _run(tmp_path, "run_reject_shape")
    rejects = _read_jsonl(Path(result["runDir"]) / "06_rejects.jsonl")
    by_id = {item.get("sample_id"): item for item in rejects}

    assert by_id["as_005"]["reason"] == "media_unreadable"
    assert by_id["as_005"]["source_reference"] == ["media/missing_audio.wav"]
    assert by_id["as_007"]["reason"] == "invalid_audio_metadata"
    assert by_id["as_006"]["reason"] in FFMPEG_UNAVAILABLE_REASONS
    assert by_id["as_006"]["media_errors"][0]["reason"] == by_id["as_006"]["reason"]
    for item in rejects:
        assert item["record"]["sample_id"] == item["sample_id"]


def test_run_manifest_lists_entrypoint_and_all_files(tmp_path):
    result = _run(tmp_path, "run_manifest")
    run_dir = Path(result["runDir"])
    manifest = json.loads((run_dir / "manifest.json").read_text(encoding="utf-8"))

    assert manifest["format"] == "crossmodal-manifest-v1"
    assert manifest["entrypoint"] == "06_audio_index.jsonl"
    listed = {item["path"] for item in manifest["artifacts"]}
    assert {"06_audio_index.jsonl", "06_rejects.jsonl"} <= listed
    for path in listed:
        assert (run_dir / path).is_file(), path


def test_run_no_lineage_and_stage_context_written(tmp_path):
    """[v4.2] 血缘/阶段归档停用：不再写 lineage.json / stage_context.json，结果无血缘键。"""
    result = _run(tmp_path, "run_lineage")
    run_dir = Path(result["runDir"])

    assert not (run_dir / "lineage.json").exists()
    assert not (run_dir / "stage_context.json").exists()
    for key in ("lineage", "lineageFile", "stageLineage", "priorLineage"):
        assert key not in result


def test_run_bad_lines_and_duplicates_are_reported(tmp_path):
    result = _run(tmp_path, "run_bad", entry=DEMO_BAD)
    rejects = _read_jsonl(Path(result["runDir"]) / "06_rejects.jsonl")
    reasons = [item["reason"] for item in rejects]

    assert result["statistics"]["input"] == 5
    assert result["statistics"]["output"] == 1
    assert result["statistics"]["duplicates"] == 1
    assert reasons.count("invalid_json") == 1
    assert reasons.count("invalid_record") == 2
    assert reasons.count("duplicate_sample_id") == 1


def test_run_empty_input_is_explicit(tmp_path):
    empty = tmp_path / "empty.jsonl"
    empty.write_text("", encoding="utf-8")
    result = core.run(str(empty), "run_empty", config_path=str(CONFIG_PATH),
                      output_root=str(tmp_path / "root"))

    assert result["status"] == "success"
    assert result["message"].startswith("empty_input")
    assert result["statistics"]["input"] == 0
    assert result["statistics"]["empty_input"] is True


def test_run_rejects_non_utf8_input(tmp_path):
    bad = tmp_path / "gbk.jsonl"
    bad.write_bytes("样例".encode("gbk") + b"\n")
    with pytest.raises(core.InputError):
        core.run(str(bad), "run_gbk", config_path=str(CONFIG_PATH),
                 output_root=str(tmp_path / "root"))


def test_run_chaining_creates_new_run(tmp_path):
    """[v4.2] 阶段链式仍承接上游主清单；血缘串联已停用，每次运行独立 run 目录。"""
    root = tmp_path / "root"
    first = core.run(str(DEMO), "chain_f1", config_path=str(CONFIG_PATH), output_root=str(root))
    second = core.run(first["outputFile"], "chain_f1", config_path=str(CONFIG_PATH),
                      output_root=str(root))

    assert Path(second["runDir"]).name == "run002"
    assert "stageLineage" not in second and "priorLineage" not in second
    assert second["statistics"]["input"] == 5  # 承接上游主清单，不再读回默认入口
    assert second["statistics"]["output"] == 5


def test_run_rejects_path_traversal_media(tmp_path):
    entry = tmp_path / "trav.jsonl"
    payload = {"sample_id": "trav_1", "audio": "../../../etc/passwd"}
    entry.write_text(json.dumps(payload) + "\n", encoding="utf-8")
    result = core.run(str(entry), "run_trav", config_path=str(CONFIG_PATH),
                      output_root=str(tmp_path / "root"))

    rejects = _read_jsonl(Path(result["runDir"]) / "06_rejects.jsonl")
    assert result["statistics"]["rejected"] == 1
    assert rejects[0]["reason"] == "unsupported_media_path"


def test_run_writes_run_log_without_payload(tmp_path):
    result = _run(tmp_path, "run_log")
    log = (Path(result["runDir"]) / "run.log").read_text(encoding="utf-8")
    assert "start" in log and "statistics" in log and "finish" in log
    assert "示例语音文本" not in log


def test_io_resolve_media_blocks_escape(tmp_path):
    with pytest.raises(io_utils.UnsupportedMediaPath):
        io_utils.resolve_media(tmp_path, "../outside.wav")
    with pytest.raises(io_utils.UnsupportedMediaPath):
        io_utils.resolve_media(tmp_path, "/absolute/path.wav")


# ---------------------------------------------------------------- CLI

def test_parse_overrides_merges_json_and_key_value():
    assert core._parse_overrides(None, None) is None
    merged = core._parse_overrides(["sample_rate=8000", "clip_start=null"],
                                   '{"pcm_bits": 24}')
    assert merged == {"pcm_bits": 24, "sample_rate": 8000, "clip_start": None}
    assert core._parse_overrides(["pcm_bits=bad"], None) == {"pcm_bits": "bad"}
    with pytest.raises(ConfigError):
        core._parse_overrides(["broken"], None)
    with pytest.raises(ConfigError):
        core._parse_overrides(None, "[1, 2]")


def _stdout_json(capsys) -> dict:
    """stdout 是一份完整的 JSON 信封（缩进多行），日志全部走 stderr。"""
    return json.loads(capsys.readouterr().out)


def test_cli_main_reports_summary_and_exit_zero(tmp_path, capsys):
    code = core.main(["--input", str(DEMO), "--unique-id", "cli_smoke",
                      "--output-root", str(tmp_path / "root"),"--override","enabled=true"])
    summary = _stdout_json(capsys)

    assert code == 0
    assert summary["code"] == 0
    assert summary["status"] == "partial"
    assert summary["statistics"]["output"] == 5
    # [v4.2] 统一响应形状预览：operator/status/outputFile/result
    assert set(summary["dataShape"]) == {"operator", "status", "outputFile", "result"}
    assert Path(summary["outputFile"]).is_file()


def test_cli_main_is_last_previews_file_object_shape(tmp_path, capsys):
    code = core.main(["--input", str(DEMO), "--unique-id", "cli_last",
                      "--output-root", str(tmp_path / "root"), "--is-last",
                      "--override", "enabled=true", "--quiet"])
    summary = _stdout_json(capsys)

    assert code == 0
    assert summary["statistics"]["requested_sample_rate"] == 16000
    # [v4.2] 血缘输出停用：CLI 摘要不再含 lineageFile
    assert set(summary) == {"code", "msg", "status", "outputFile", "statistics"}


def test_cli_main_fails_loudly_on_missing_input(tmp_path, capsys):
    code = core.main(["--input", str(tmp_path / "nope.jsonl"), "--unique-id", "cli_bad",
                      "--output-root", str(tmp_path / "root"),"--override","enabled=true"])
    summary = _stdout_json(capsys)

    assert code == 1
    assert summary["code"] == 1
    assert summary["data"] == {}


def test_cli_main_rejects_bad_override_expression(tmp_path, capsys):
    code = core.main(["--input", str(DEMO), "--unique-id", "cli_bad_override",
                      "--output-root", str(tmp_path / "root"), "--override", "broken"])
    assert code == 1
    assert _stdout_json(capsys)["code"] == 1
