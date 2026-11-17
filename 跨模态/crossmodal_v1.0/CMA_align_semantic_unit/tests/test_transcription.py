"""算子 8 语音转写测试（表1 #8 冻结口径）。

覆盖表1 的五条完成判据：
1. 总开关或「是否做语音转写」关闭时不加载模型；
2. 固定 Whisper-base，语言按中文解码；
3. 输出带起止时间的文本，并按段落把音频裁开；
4. 转写失败留在产线、进候选描述、不能直接评分；
5. 纯图文默认任务的日志写明未加载 Whisper-base。
"""
from __future__ import annotations

import io
import json
import sys
import wave
from pathlib import Path

import pytest

BASE_DIR = Path(__file__).resolve().parents[1]
if str(BASE_DIR) not in sys.path:
    sys.path.insert(0, str(BASE_DIR))

import inference  # noqa: E402
from tests import fixtures  # noqa: E402
from utils import io as io_utils  # noqa: E402
from utils import transcription as tr  # noqa: E402
from utils.schemas import OperatorConfig  # noqa: E402

SAMPLE_AUDIO = BASE_DIR / "dataset" / "sample" / "06_audio" / "demo_07_v1.wav"


def _wav_bytes(seconds: float = 2.0, rate: int = 16000) -> bytes:
    import numpy as np

    timeline = np.arange(int(seconds * rate)) / rate
    # 2026-11-05：预检用分位 RMS 估 SNR，稳态单音的上下分位相同（SNR≈0 dB，
    # 判 low_snr）；夹具改为「0.25s 音节 + 0.15s 低噪间隙」的语音式起伏。
    envelope = np.zeros_like(timeline)
    for start in np.arange(0.0, seconds, 0.4):
        envelope[(timeline >= start) & (timeline < start + 0.25)] = 1.0
    rng = np.random.default_rng(20261105)
    signal = 0.4 * np.sin(2 * np.pi * 220.0 * timeline) * envelope
    signal = signal + 0.001 * rng.standard_normal(timeline.size)
    tone = (np.clip(signal, -1.0, 1.0) * 32767).astype("<i2")
    buffer = io.BytesIO()
    with wave.open(buffer, "wb") as handle:
        handle.setnchannels(1)
        handle.setsampwidth(2)
        handle.setframerate(rate)
        handle.writeframes(tone.tobytes())
    return buffer.getvalue()


def _read_log(result) -> str:
    return (Path(result["runDir"]) / "run.log").read_text(encoding="utf-8")


def _first_record(result) -> dict:
    for line in io_utils.iter_decoded_lines(Path(result["outputFile"]), "utf-8"):
        if line.strip():
            return json.loads(line)
    raise AssertionError("主清单为空")


def _run_audio_task(tmp_path: Path, unique_id: str, overrides=None,
                    audio_seconds: float = 2.0):
    out = tmp_path / "out"
    records = [fixtures.make_av_record("av1", 2.0, 2.0)]
    fixtures.write_task(
        out / unique_id, records,
        media={"06_audio/av1.wav": _wav_bytes(audio_seconds),
               "media/original/av1.mp4": b"\x00\x00\x00\x18ftypmp42"})
    return inference.run(None, unique_id, output_root=str(out), overrides=overrides)


def _stub_transcribe(segments, text, audio, sample_rate=tr.WHISPER_SAMPLE_RATE):
    """构造一个「转写成功」的替身，避免测试里真的加载权重。

    ``*args`` 兼容两种绑定方式：挂到类上（传 self）或挂到实例上（不传 self）。
    """
    def _fake(*_args, **_kwargs):
        return {"status": tr.STATUS_OK, "segments": segments, "text": text,
                "audio": audio, "sample_rate": sample_rate, "duration": 2.0}
    return _fake


# ---------------------------------------------------------------- 开关语义

def test_default_config_keeps_transcription_off():
    """表1 #8：默认关闭，两个固定项写死；总开关关闭优先报 master_switch_off。"""
    cfg = inference.load_config()
    settings = tr.TranscriptionSettings.from_config(cfg)
    assert settings.active is False
    assert settings.master_enabled is False
    assert settings.inactive_reason == tr.REASON_MASTER_SWITCH_OFF
    assert settings.model == "whisper-base"
    assert settings.language == "zh"
    assert settings.switch_detail()["transcription_effective"] is False


def test_master_switch_off_wins_over_transcription_switch():
    """表1 #8 步骤 1)：总开关关闭时，即使转写开关打开也不加载模型。"""
    cfg = inference.load_config(overrides={"enabled": False,
                                           "transcription_enabled": True})
    settings = tr.TranscriptionSettings.from_config(cfg)
    assert settings.active is False
    assert settings.inactive_reason == tr.REASON_MASTER_SWITCH_OFF


def test_request_can_toggle_transcription_but_not_fixed_items():
    # [v4.2] 两个开关都要打开：总开关 enabled + 是否转写 transcription_enabled
    cfg = inference.load_config(overrides={"enabled": True,
                                           "transcription_enabled": True})
    settings = tr.TranscriptionSettings.from_config(cfg)
    assert settings.active is True
    assert settings.model == "whisper-base" and settings.language == "zh"
    assert settings.initial_prompt == ""


def test_prompt_echo_is_detected_for_safe_fallback():
    prompt = "请使用简体中文转写。"
    assert tr.has_prompt_echo(prompt, prompt) is True
    assert tr.has_prompt_echo(prompt + "广州市房地产中介协会分析", prompt) is True
    assert tr.has_prompt_echo("广州市房地产中介协会分析", prompt) is False


def test_fixed_model_and_language_reject_other_values():
    """边界：不换成更大的 Whisper；语言固定中文解码。"""
    base = {"operator": {"name": "multimodal_preprocessing", "version": "v1.0.0"}}
    with pytest.raises(Exception):
        OperatorConfig.model_validate(
            {**base, "transcription": {"enabled": True, "model": "whisper-large-v3"}})
    with pytest.raises(Exception):
        OperatorConfig.model_validate(
            {**base, "transcription": {"enabled": True, "language": "en"}})


def test_settings_reject_non_fixed_model_at_runtime():
    class FakeSection:
        enabled = True
        model = "whisper-large-v3"
        language = "zh"

    class FakeConfig:
        enabled = True
        transcription = FakeSection()

    with pytest.raises(tr.TranscriptionConfigError):
        tr.TranscriptionSettings.from_config(FakeConfig())


# ---------------------------------------------------------------- 纯函数

def test_group_paragraphs_splits_on_silence_gap():
    segments = [{"start": 0.0, "end": 1.0, "text": "a"},
                {"start": 1.1, "end": 2.0, "text": "b"},
                {"start": 5.0, "end": 6.0, "text": "c"}]
    paragraphs = tr.group_paragraphs(segments, silence_seconds=0.6,
                                     max_segment_seconds=30.0)
    assert [item["text"] for item in paragraphs] == ["a b", "c"]
    assert paragraphs[0]["start"] == 0.0 and paragraphs[0]["end"] == 2.0
    assert [item["index"] for item in paragraphs] == [0, 1]


def test_group_paragraphs_splits_on_max_segment_seconds():
    segments = [{"start": 0.0, "end": 2.0, "text": "a"},
                {"start": 2.1, "end": 4.0, "text": "b"}]
    paragraphs = tr.group_paragraphs(segments, silence_seconds=5.0,
                                     max_segment_seconds=3.0)
    assert len(paragraphs) == 2


def test_finalize_segments_fills_missing_timestamps():
    filled = tr.finalize_segments([{"start": None, "end": None, "text": "a"},
                                   {"start": 1.0, "end": 2.0, "text": "b"}])
    assert filled[0]["start"] == 0.0 and filled[0]["end"] == 0.0
    assert filled[1]["start"] == 1.0 and filled[1]["end"] == 2.0


def test_read_audio_decodes_real_wav():
    """样本音频是 12 秒 16kHz 单声道 PCM；解码必须给出正确的长度与采样率。"""
    waveform, rate = tr.read_audio(SAMPLE_AUDIO)
    assert rate == 16000
    assert waveform.ndim == 1 and waveform.size == rate * 12


def test_read_audio_reports_decode_failure_without_faking():
    with pytest.raises(tr.AudioDecodeError):
        tr.read_audio(BASE_DIR / "tests" / "fixtures.py")


def test_audio_source_reference_prefers_audio_then_video():
    assert tr.audio_source_reference({"audio": {"path": "06_audio/a.wav"}}) == \
        "06_audio/a.wav"
    assert tr.audio_source_reference({"audio": None,
                                      "video": {"path": "media/original/a.mp4"}}) == \
        "media/original/a.mp4"
    assert tr.audio_source_reference({"audio": None, "video": None}) is None


def test_time_index_is_empty_when_transcript_failed():
    payload = tr.build_payload(tr.STATUS_FAILED, reason=tr.REASON_MODEL_MISSING)
    assert tr.time_index_of(payload) == []


def test_payload_carries_fixed_model_version():
    payload = tr.build_payload(tr.STATUS_OK, text="你好")
    assert payload["model"] == tr.FIXED_MODEL == "whisper-base"
    assert payload["model_version"] == payload["model"]
    assert payload["language"] == "zh"


def test_record_process_status_maps_every_payload_state():
    """记录级流程态由 ASR 结果决定；未知状态按 failed。"""
    cases = {
        tr.STATUS_OK: "transcribed",
        tr.STATUS_SKIPPED: "skipped",
        tr.STATUS_NOT_APPLICABLE: "not_applicable",
        tr.STATUS_NO_SPEECH: "no_speech",
        tr.STATUS_FAILED: "failed",
        "weird": "failed",
    }
    for state, expected in cases.items():
        assert tr.record_process_status({"status": state}) == expected


def test_milliseconds_round_half_up_and_clamp_negative():
    assert tr.milliseconds(0) == 0
    assert tr.milliseconds(-1.5) == 0
    assert tr.milliseconds(1.2345) == 1235      # 1234.5 → 1235（四舍五入）
    assert tr.milliseconds(0.0004) == 0
    assert tr.milliseconds(2) == 2000
    assert tr.milliseconds(None) is None
    assert tr.milliseconds("1.0") is None


def test_time_index_is_integer_milliseconds_monotonic_and_non_overlapping():
    """[start, end)：秒×1000 四舍五入；重叠时后段起点前移，保证单调不重叠。"""
    payload = tr.build_payload(tr.STATUS_OK, text="一二三", paragraphs=[
        {"index": 1, "start": 0.0, "end": 1.2345, "text": "一"},
        {"index": 2, "start": 1.2, "end": 2.0004, "text": "二"},
        {"index": 3, "start": 5.0, "end": 5.0, "text": "三"},
    ])
    index = tr.time_index_of(payload)
    assert [item["start_ms"] for item in index] == [0, 1235, 5000]
    assert [item["end_ms"] for item in index] == [1235, 2000, 5001]
    # 秒字段保留一版给现有消费方（#17/#11/#19）
    assert [item["start"] for item in index] == [0.0, 1.2, 5.0]
    for item in index:
        assert isinstance(item["start_ms"], int) and isinstance(item["end_ms"], int)
        assert item["end_ms"] > item["start_ms"]
    for previous, current in zip(index, index[1:]):
        assert current["start_ms"] >= previous["end_ms"]


def test_prompt_and_beam_are_carried_in_success_payload():
    settings = tr.TranscriptionSettings(
        master_enabled=True, switch_enabled=True, model="whisper-base",
        language="zh", model_root="weight", model_dir="whisper-base",
        device="cpu", max_segment_seconds=30.0, paragraph_silence_seconds=0.6,
        beam_size=3, initial_prompt="请使用简体中文转写。")
    payload = tr.build_payload(tr.STATUS_OK, text="你好", paragraphs=[],
                               settings=settings, device="cpu")
    assert payload["beam_size"] == 3
    assert payload["initial_prompt_enabled"] is True


def test_transcriber_falls_back_when_prompt_is_echoed(monkeypatch):
    """提示词回声不能进入音频文本，必须回退到同一模型的无提示结果。"""
    import types
    import torch
    import numpy as np

    settings = tr.TranscriptionSettings(
        master_enabled=True, switch_enabled=True, model="whisper-base",
        language="zh", model_root="weight", model_dir="whisper-base",
        device="cpu", max_segment_seconds=30.0, paragraph_silence_seconds=0.6,
        beam_size=3, initial_prompt="请使用简体中文转写。", precheck=False)
    transcriber = tr.WhisperTranscriber(settings)
    transcriber.loaded = True
    transcriber.device = "cpu"

    class FakeProcessor:
        def __call__(self, *_args, **_kwargs):
            return types.SimpleNamespace(input_features=torch.zeros((1, 80, 20)))

        def get_prompt_ids(self, *_args, **_kwargs):
            return torch.tensor([1, 2])

    transcriber._processor = FakeProcessor()
    calls = []

    def fake_generate(_features, prompt_ids=None):
        calls.append(prompt_ids)
        text = "请使用简体中文转写。" if prompt_ids is not None else "真实音频内容"
        return [{"start": 0.0, "end": 1.0, "text": text}]

    transcriber._generate_segments = fake_generate  # type: ignore[method-assign]
    monkeypatch.setattr(tr, "read_audio", lambda _path: (
        np.zeros(tr.WHISPER_SAMPLE_RATE, dtype="float32"), tr.WHISPER_SAMPLE_RATE))

    result = transcriber.transcribe(SAMPLE_AUDIO)
    assert result["status"] == tr.STATUS_OK
    assert result["text"] == "真实音频内容"
    assert result["prompt_used"] is False
    assert result["prompt_fallback"] is True
    assert len(calls) == 2


# ---------------------------------------------------------------- 端到端

def test_run_transcription_cuts_paragraphs_and_writes_time_index(tmp_path):
    """表1 #8 步骤 2)+3)：带起止时间的文本 + 按段落裁开的音频。"""
    import numpy as np

    audio = np.zeros(int(4.0 * tr.WHISPER_SAMPLE_RATE), dtype="float32")
    segments = [{"start": 0.0, "end": 1.0, "text": "第一段"},
                {"start": 3.0, "end": 4.0, "text": "第二段"}]
    settings = tr.TranscriptionSettings(
        master_enabled=True, switch_enabled=True, model="whisper-base",
        language="zh", model_root="weight", model_dir="whisper-base",
        device="cpu", max_segment_seconds=30.0, paragraph_silence_seconds=0.6,
        beam_size=1)
    transcriber = tr.WhisperTranscriber(settings)
    transcriber.transcribe = _stub_transcribe(segments, "第一段 第二段", audio)  # type: ignore

    payload = tr.run_transcription(
        transcriber, SAMPLE_AUDIO, sample_id="av1", settings=settings,
        segment_dir=tmp_path / "media" / "segments",
        relative_prefix="media/segments")

    assert payload["status"] == tr.STATUS_OK
    assert payload["model"] == "whisper-base" and payload["language"] == "zh"
    assert payload["paragraph_count"] == 2
    index = tr.time_index_of(payload)
    assert [item["start"] for item in index] == [0.0, 3.0]
    assert [item["end"] for item in index] == [1.0, 4.0]
    for item in index:
        assert (tmp_path / item["segment_path"]).is_file()


def test_pure_image_text_task_logs_whisper_not_loaded(tmp_path):
    """表1 #8 完成判据：纯图文默认任务的日志写明未加载 Whisper-base。"""
    out = tmp_path / "out"
    fixtures.write_task(out / "cm08_pure",
                        [fixtures.make_record("ti1")],
                        media={"04_images/ti1.png": fixtures.PNG_BYTES})
    result = inference.run(None, "cm08_pure", output_root=str(out))

    log = _read_log(result)
    assert "whisper_not_loaded" in log
    assert "未加载 Whisper-base" in log
    # [v4.2] 默认总开关关闭，优先报 master_switch_off
    assert tr.REASON_MASTER_SWITCH_OFF in log
    assert result["statistics"]["transcription_model_loaded"] is False
    assert result["statistics"]["transcription"]["effective"] is False


def test_audio_task_with_switch_off_is_not_directly_scorable(tmp_path):
    """开关关闭时音频样本不产生时间索引，并显式标记「不能直接评分」。"""
    result = _run_audio_task(tmp_path, "cm08_off")
    assert "未加载 Whisper-base" in _read_log(result)
    stats = result["statistics"]
    assert stats["transcription_model_loaded"] is False
    assert stats["transcription_loaded"] is False
    assert stats["transcription_skipped"] == 1 and stats["audio_units"] == 1

    record = _first_record(result)
    assert record["transcription"]["status"] == tr.STATUS_SKIPPED
    assert record["time_index"] == []
    assert record["transcription_gate"]["direct_scoring_allowed"] is False


def test_transcription_failure_stays_in_pipeline_but_cannot_be_scored(tmp_path,
                                                                     monkeypatch):
    """表1 #8：转写失败留在产线、进候选描述、不能直接评分，且不伪造文本。"""
    monkeypatch.setattr(tr.WhisperTranscriber, "load",
                        lambda self: (False, tr.REASON_MODEL_MISSING))
    result = _run_audio_task(tmp_path, "cm08_fail",
                             overrides={"enabled": True,
                                        "transcription_enabled": True})

    stats = result["statistics"]
    assert stats["output"] == 1 and stats["transcription_failed"] == 1
    assert stats["transcription_fail_reasons"][tr.REASON_MODEL_MISSING] == 1
    assert result["status"] == "success"

    record = json.loads(Path(result["outputFile"]).read_text(encoding="utf-8").strip())
    assert record["transcription"]["status"] == tr.STATUS_FAILED
    assert record["transcription"]["text"] is None
    assert record["audio_transcript"] is None
    assert record["transcription"]["reason"] == tr.REASON_MODEL_MISSING
    assert record["candidate_description_required"] is True
    assert record["transcription_gate"]["direct_scoring_allowed"] is False


def test_transcription_success_registers_segments_as_artifacts(tmp_path, monkeypatch):
    """成功转写：段落裁片随包搬运并登记为产物，时间索引写入记录。"""
    import numpy as np

    audio = np.zeros(int(2.0 * tr.WHISPER_SAMPLE_RATE), dtype="float32")
    segments = [{"start": 0.0, "end": 1.0, "text": "你好"},
                {"start": 1.0, "end": 2.0, "text": "世界"}]
    def _fake_load(self):
        self.loaded = True
        self.device = "cpu"
        return True, None

    monkeypatch.setattr(tr.WhisperTranscriber, "transcribe",
                        _stub_transcribe(segments, "你好 世界", audio))
    monkeypatch.setattr(tr.WhisperTranscriber, "load", _fake_load)

    result = _run_audio_task(tmp_path, "cm08_ok",
                             overrides={"enabled": True,
                                        "transcription_enabled": True})
    stats = result["statistics"]
    assert stats["transcribed"] == 1 and stats["transcription_failed"] == 0
    assert stats["segments_written"] == 1

    run_dir = Path(result["runDir"])
    record = json.loads(Path(result["outputFile"]).read_text(encoding="utf-8").strip())
    assert record["transcription"]["status"] == tr.STATUS_OK
    assert record["transcription_gate"]["direct_scoring_allowed"] is True
    assert len(record["time_index"]) == 1
    for item in record["time_index"]:
        assert (run_dir / item["segment_path"]).is_file()
    listed = {item["path"] for item in result["artifacts"]}
    assert record["time_index"][0]["segment_path"] in listed


# ------------------------------------------------- 音轨探测 / ffmpeg 兜底（#8 P0+P1）

def _fake_run(returncode: int = 0, stdout: bytes = b"", stderr: bytes = b""):
    from types import SimpleNamespace

    def _run(*_args, **_kwargs):
        return SimpleNamespace(returncode=returncode, stdout=stdout, stderr=stderr)
    return _run


def _enabled_settings() -> tr.TranscriptionSettings:
    return tr.TranscriptionSettings(
        master_enabled=True, switch_enabled=True, model="whisper-base",
        language="zh", model_root="weight", model_dir="whisper-base",
        device="cpu", max_segment_seconds=30.0, paragraph_silence_seconds=0.6,
        beam_size=1)


def test_probe_audio_stream_returns_none_without_ffprobe(monkeypatch):
    """没装 ffprobe 时「探测不了」，不能当成「没有音轨」。"""
    monkeypatch.setattr(tr, "ffprobe_binary", lambda: None)
    assert tr.probe_audio_stream(SAMPLE_AUDIO) is None


def test_probe_audio_stream_reports_missing_audio_track(monkeypatch):
    monkeypatch.setattr(tr, "ffprobe_binary", lambda: "ffprobe")
    monkeypatch.setattr(tr.subprocess, "run", _fake_run(stdout=b""))
    assert tr.probe_audio_stream(SAMPLE_AUDIO) is False


def test_probe_audio_stream_true_when_stream_listed(monkeypatch):
    monkeypatch.setattr(tr, "ffprobe_binary", lambda: "ffprobe")
    monkeypatch.setattr(tr.subprocess, "run",
                        _fake_run(stdout=b"1\n"))
    assert tr.probe_audio_stream(SAMPLE_AUDIO) is True


def test_probe_audio_stream_returns_none_on_probe_error(monkeypatch):
    monkeypatch.setattr(tr, "ffprobe_binary", lambda: "ffprobe")
    monkeypatch.setattr(tr.subprocess, "run", _fake_run(returncode=1))
    assert tr.probe_audio_stream(SAMPLE_AUDIO) is None


def test_decode_with_ffmpeg_reads_float32_pcm(monkeypatch):
    import numpy as np

    samples = np.array([0.0, 0.5, -0.5, 0.25], dtype="<f4")
    monkeypatch.setattr(tr, "ffmpeg_binary", lambda: "ffmpeg")
    monkeypatch.setattr(tr.subprocess, "run", _fake_run(stdout=samples.tobytes()))

    waveform, rate = tr.decode_with_ffmpeg(SAMPLE_AUDIO)
    assert rate == tr.WHISPER_SAMPLE_RATE
    assert waveform.dtype == np.float32
    # 不用 numpy.testing：它首次导入会调 subprocess，而本用例打了 subprocess.run 的桩
    assert np.allclose(waveform, samples)


def test_decode_with_ffmpeg_reports_missing_audio_track(monkeypatch):
    """ffmpeg 正常退出但输出为空 ⇒ 无声视频，不当作坏文件。"""
    monkeypatch.setattr(tr, "ffmpeg_binary", lambda: "ffmpeg")
    monkeypatch.setattr(tr.subprocess, "run", _fake_run(stdout=b""))
    with pytest.raises(tr.AudioDecodeError) as info:
        tr.decode_with_ffmpeg(SAMPLE_AUDIO)
    assert info.value.reason == tr.REASON_NO_AUDIO_TRACK


def test_decode_with_ffmpeg_reports_nonzero_exit(monkeypatch):
    monkeypatch.setattr(tr, "ffmpeg_binary", lambda: "ffmpeg")
    monkeypatch.setattr(tr.subprocess, "run",
                        _fake_run(returncode=1, stderr=b"Invalid data"))
    with pytest.raises(tr.AudioDecodeError) as info:
        tr.decode_with_ffmpeg(SAMPLE_AUDIO)
    assert info.value.reason == tr.REASON_DECODE_FAILED
    assert "Invalid data" in str(info.value)


def test_decode_with_ffmpeg_requires_available_binary(monkeypatch):
    monkeypatch.setattr(tr, "ffmpeg_binary", lambda: None)
    with pytest.raises(tr.AudioDecodeError) as info:
        tr.decode_with_ffmpeg(SAMPLE_AUDIO)
    assert info.value.reason == tr.REASON_DECODER_UNAVAILABLE


def test_read_audio_falls_back_to_ffmpeg_for_containers(monkeypatch, tmp_path):
    """soundfile/torchaudio 都失败时，read_audio 必须落到 ffmpeg 兜底。"""
    import numpy as np

    calls = []

    def _fake(path):
        calls.append(path)
        return np.zeros(16, dtype="float32"), tr.WHISPER_SAMPLE_RATE

    monkeypatch.setattr(tr, "decode_with_ffmpeg", _fake)
    waveform, rate = tr.read_audio(tmp_path / "container.mp4")
    assert calls and rate == tr.WHISPER_SAMPLE_RATE
    assert waveform.size == 16


def test_transcriber_short_circuits_when_media_has_no_audio(monkeypatch):
    """探测到无音轨时直接返回，不进入解码、不白跑 Whisper。

    [v1.0.1] 无音轨归 ``not_applicable``（不需要 ASR），不再冒充 ``failed``。
    """
    transcriber = tr.WhisperTranscriber(_enabled_settings())
    monkeypatch.setattr(tr, "probe_audio_stream", lambda _path: False)

    def _must_not_read(_path):
        raise AssertionError("无声视频不应进入解码")

    monkeypatch.setattr(tr, "read_audio", _must_not_read)
    result = transcriber.transcribe(SAMPLE_AUDIO)
    assert result["status"] == tr.STATUS_NOT_APPLICABLE
    assert result["reason"] == tr.REASON_NO_AUDIO_TRACK


def test_transcriber_preserves_decode_failure_reason(monkeypatch):
    transcriber = tr.WhisperTranscriber(_enabled_settings())
    monkeypatch.setattr(tr, "probe_audio_stream", lambda _path: None)

    def _fail(_path):
        raise tr.AudioDecodeError("解码为空", tr.REASON_NO_AUDIO_TRACK)

    monkeypatch.setattr(tr, "read_audio", _fail)
    result = transcriber.transcribe(SAMPLE_AUDIO)
    assert result["status"] == tr.STATUS_FAILED
    assert result["reason"] == tr.REASON_NO_AUDIO_TRACK


# ------------------------------------------------- 视觉时间索引（#8 P2b）

def test_visual_time_index_uses_keyframes_when_available():
    """有 ≥2 个关键帧 → 按帧切成首尾相接的子时间段（source=visual_frame）。"""
    record = {"video": {"segments": [
        {"start": 0.0, "end": 3.0, "keyframes": [0.0, 1.0]},
        {"start": 3.0, "end": 8.5, "keyframes": [3.0, 5.0]}]}}
    index = tr.visual_time_index(record)
    assert [item["start"] for item in index] == [0.0, 1.0, 3.0, 5.0]
    assert [item["end"] for item in index] == [1.0, 3.0, 5.0, 8.5]
    assert [item["index"] for item in index] == [0, 1, 2, 3]
    for item in index:
        assert item["source"] == "visual_frame"
        assert item["granularity"] == "frame"
        assert item["text"] is None and item["segment_path"] is None
    # 每段首尾都连上：前一条的 end 等于后一条的 start
    assert all(index[i]["end"] == index[i + 1]["start"] for i in range(len(index) - 1))


def test_visual_time_index_carries_frame_paths():
    """帧带路径时，条目带上 frame_path/frame_index/timestamp，供 #17/#18 定位裁图源。"""
    record = {"video": {"segments": [{
        "start": 0.0, "end": 3.0,
        "frames": [
            {"path": "media/v1/frame_000001.jpg", "timestamp": 0.0, "frame_index": 0},
            {"path": "media/v1/frame_000002.jpg", "timestamp": 1.0, "frame_index": 1},
            {"path": "media/v1/frame_000003.jpg", "timestamp": 2.0, "frame_index": 2},
        ]}]}}
    index = tr.visual_time_index(record)
    assert [item["start"] for item in index] == [0.0, 1.0, 2.0]
    assert [item["end"] for item in index] == [1.0, 2.0, 3.0]
    assert [item["frame_path"] for item in index] == ["media/v1/frame_000001.jpg",
                                                      "media/v1/frame_000002.jpg",
                                                      "media/v1/frame_000003.jpg"]
    assert [item["frame_index"] for item in index] == [0, 1, 2]
    assert [item["timestamp"] for item in index] == [0.0, 1.0, 2.0]
    assert all(item["segment_start"] == 0.0 and item["segment_end"] == 3.0
               for item in index)


def test_visual_time_index_falls_back_to_segment_without_frames():
    """没有可用关键帧 → 保持分段级（粗粒度）兜底，不编造子段。"""
    record = {"video": {"segments": [{"start": 1.0, "end": 4.0}]}}
    index = tr.visual_time_index(record)
    assert len(index) == 1
    assert index[0]["source"] == "visual_segment"
    assert index[0]["granularity"] == "segment"
    assert (index[0]["start"], index[0]["end"]) == (1.0, 4.0)


def test_visual_time_index_downsamples_but_stays_contiguous():
    """超过条目上限时连续帧合组降采样：数量受限、首尾相接、不跳帧。"""
    start = 0.0
    frames = [{"path": "media/v1/frame_%06d.jpg" % i, "timestamp": float(i),
               "frame_index": i} for i in range(120)]
    record = {"video": {"segments": [
        {"start": start, "end": 120.0, "frames": frames}]}}
    index = tr.visual_time_index(record)
    assert len(index) <= tr.MAX_VISUAL_INDEX_ENTRIES
    assert index[0]["start"] == 0.0 and index[-1]["end"] == 120.0
    assert all(index[i]["end"] == index[i + 1]["start"] for i in range(len(index) - 1))
    assert all(item["granularity"] == "frame_group" for item in index)
    # 每组的 frame_path 是该组第一个关键帧
    assert index[0]["frame_path"] == "media/v1/frame_000000.jpg"


def test_visual_time_index_skips_unusable_segments():
    record = {"video": {"segments": [
        {"start": 2.0, "end": 2.0},
        {"start": "0", "end": 1.0},
        {"start": True, "end": 2.0},
        "bad-entry",
        {"start": 1.0, "end": 2.0}]}}
    index = tr.visual_time_index(record)
    assert len(index) == 1 and index[0]["start"] == 1.0
    assert tr.visual_time_index({"video": None}) == []
    assert tr.visual_time_index({}) == []


def test_silent_video_gets_visual_time_index(tmp_path, monkeypatch):
    """无声视频归 not_applicable，改挂视觉时间索引，并登记待补清单（不回采）。"""
    monkeypatch.setattr(tr, "probe_audio_stream", lambda _path: False)
    monkeypatch.setattr(tr.WhisperTranscriber, "load", lambda self: (True, None))

    out = tmp_path / "out"
    record = fixtures.make_av_record("silent1", 8.0, 8.0)
    record["audio"] = None
    record["video"]["segments"] = [
        {"start": 0.0, "end": 3.0, "keyframes": [0.0, 1.0]},
        {"start": 3.0, "end": 8.0, "keyframes": [3.0, 5.0, 7.0]}]
    fixtures.write_task(out / "cm08_silent", [record],
                        media={"media/original/silent1.mp4":
                               b"\x00\x00\x00\x18ftypmp42"})
    result = inference.run(None, "cm08_silent", output_root=str(out),
                           overrides={"enabled": True,
                                      "transcription_enabled": True})

    stats = result["statistics"]
    # [v1.0.1] 无音轨 = not_applicable（不再是 failed），并进待补清单
    assert stats["not_applicable"] == 1
    assert stats["transcription_failed"] == 0
    assert tr.REASON_NO_AUDIO_TRACK not in stats["transcription_fail_reasons"]
    assert stats["supplement_requests"] == 1
    assert stats["visual_time_index_built"] == 1
    assert stats["visual_time_index_entries"] == 5
    assert stats["visual_time_index_frame_entries"] == 5

    saved = _first_record(result)
    assert saved["time_index"] == []
    assert [item["start"] for item in saved["visual_time_index"]] == [0.0, 1.0, 3.0, 5.0, 7.0]
    assert all(item["source"] == "visual_frame"
               for item in saved["visual_time_index"])
    gate = saved["transcription_gate"]
    assert gate["reason"] == tr.REASON_NO_AUDIO_TRACK
    assert gate["transcript_available"] is False
    assert gate["direct_scoring_allowed"] is False
    assert gate["visual_time_index_available"] is True
    assert gate["visual_time_index_source"] == "m08_visual_index"
    assert gate["visual_time_index_granularity"] == "frame"
    assert gate["visual_time_index_frame_entries"] == 5
    assert saved["candidate_description_required"] is True
    assert saved["missing_modality"] == ["audio"]
    assert saved["processing"]["07"]["process_status"] == "not_applicable"
    # 待补清单是旁路台账，只登记不回采
    supplement = Path(result["runDir"]) / inference.SUPPLEMENT_NAME
    rows = [json.loads(line) for line in
            io_utils.iter_decoded_lines(supplement, "utf-8") if line.strip()]
    assert len(rows) == 1
    assert rows[0]["sample_id"] == "silent1"
    assert rows[0]["missing_modality"] == ["audio"]
    assert rows[0]["reason"] == tr.REASON_NO_AUDIO_TRACK
    assert "media/original/silent1.mp4" in rows[0]["source_reference"]


def test_decode_failure_does_not_fake_visual_index(tmp_path, monkeypatch):
    """解码失败 ≠ 无声视频：不得借视觉分段冒充时间轴。"""
    monkeypatch.setattr(tr, "probe_audio_stream", lambda _path: None)
    monkeypatch.setattr(tr.WhisperTranscriber, "load", lambda self: (True, None))

    def _fail(_path):
        raise tr.AudioDecodeError("ffmpeg 解码失败")

    monkeypatch.setattr(tr, "read_audio", _fail)

    out = tmp_path / "out"
    record = fixtures.make_av_record("broken1", 8.0, 8.0)
    record["video"]["segments"] = [{"start": 0.0, "end": 8.0}]
    fixtures.write_task(out / "cm08_broken", [record],
                        media={"06_audio/broken1.wav": _wav_bytes(2.0),
                               "media/original/broken1.mp4":
                               b"\x00\x00\x00\x18ftypmp42"})
    result = inference.run(None, "cm08_broken", output_root=str(out),
                           overrides={"enabled": True,
                                      "transcription_enabled": True})

    saved = _first_record(result)
    assert result["statistics"]["transcription_fail_reasons"][
        tr.REASON_DECODE_FAILED] == 1
    assert "visual_time_index_built" not in result["statistics"]
    assert saved["visual_time_index"] == []
    assert saved["transcription_gate"]["visual_time_index_available"] is False


def test_empty_transcript_is_no_speech_not_failure(tmp_path, monkeypatch):
    """有音轨但空转写 ⇒ ``no_speech``：不伪造文本、不直接评分，也不算缺模态。"""
    monkeypatch.setattr(tr, "probe_audio_stream", lambda _path: None)

    def _empty_segments(_self, _path):
        return {"status": tr.STATUS_OK, "segments": [], "text": "",
                "audio": None, "sample_rate": tr.WHISPER_SAMPLE_RATE}

    monkeypatch.setattr(tr.WhisperTranscriber, "transcribe", _empty_segments)

    out = tmp_path / "out"
    record = fixtures.make_av_record("quiet1", 2.0, 2.0)
    fixtures.write_task(out / "cm08_quiet", [record],
                        media={"06_audio/quiet1.wav": _wav_bytes(2.0),
                               "media/original/quiet1.mp4":
                               b"\x00\x00\x00\x18ftypmp42"})
    result = inference.run(None, "cm08_quiet", output_root=str(out),
                           overrides={"enabled": True,
                                      "transcription_enabled": True})

    stats = result["statistics"]
    assert stats["no_speech"] == 1
    assert stats["transcription_failed"] == 0 and stats["not_applicable"] == 0
    assert stats["no_speech_reasons"][tr.REASON_EMPTY_TEXT] == 1
    assert stats["supplement_requests"] == 0

    saved = _first_record(result)
    assert saved["time_index"] == []
    assert saved["audio_transcript"] in (None, "")
    assert saved["transcription"]["status"] == tr.STATUS_NO_SPEECH
    assert saved["transcription"]["model_version"] == "whisper-base"
    assert saved["candidate_description_required"] is True
    assert saved["transcription_gate"]["direct_scoring_allowed"] is False
    assert saved["processing"]["07"]["process_status"] == "no_speech"
    assert "missing_modality" not in saved


# ------------------------------------------------- 送 ASR 前时长/信噪比预检（2026-11-05）

def _speech_like(seconds: float, rate: int = 16000):
    """语音式起伏波形：0.25s 有声 + 0.15s 低噪间隙，分位 RMS 差异明显。"""
    import numpy as np

    timeline = np.arange(int(seconds * rate)) / rate
    envelope = np.zeros_like(timeline)
    for start in np.arange(0.0, seconds, 0.4):
        envelope[(timeline >= start) & (timeline < start + 0.25)] = 1.0
    rng = np.random.default_rng(20261105)
    signal = 0.4 * np.sin(2 * np.pi * 220.0 * timeline) * envelope
    signal = signal + 0.001 * rng.standard_normal(timeline.size)
    return signal.astype("float32")


def _silent_wav_bytes(seconds: float = 2.0, rate: int = 16000) -> bytes:
    import numpy as np

    pcm = (np.zeros(int(seconds * rate))).astype("<i2")
    buffer = io.BytesIO()
    with wave.open(buffer, "wb") as handle:
        handle.setnchannels(1)
        handle.setsampwidth(2)
        handle.setframerate(rate)
        handle.writeframes(pcm.tobytes())
    return buffer.getvalue()


def _settings_with(**overrides) -> tr.TranscriptionSettings:
    import dataclasses

    return dataclasses.replace(_enabled_settings(), **overrides)


def test_precheck_silent_audio_returns_no_speech_without_loading(monkeypatch):
    """整段静音：预检命中 audio_silent，不加载模型、不进入解码。"""
    import numpy as np

    transcriber = tr.WhisperTranscriber(_enabled_settings())
    monkeypatch.setattr(tr, "probe_audio_stream", lambda _path: None)
    monkeypatch.setattr(tr, "read_audio",
                        lambda _path: (np.zeros(tr.WHISPER_SAMPLE_RATE,
                                                dtype="float32"),
                                       tr.WHISPER_SAMPLE_RATE))

    def _must_not_load(_self):
        raise AssertionError("预检命中不应加载 Whisper")

    monkeypatch.setattr(tr.WhisperTranscriber, "load", _must_not_load)
    result = transcriber.transcribe(SAMPLE_AUDIO)

    assert result["status"] == tr.STATUS_NO_SPEECH
    assert result["reason"] == tr.REASON_AUDIO_SILENT
    assert result["duration"] == 1.0
    assert result["precheck"]["snr_db"] is None
    assert result["precheck"]["min_snr_db"] == 6.0


def test_precheck_flags_short_audio():
    gate = tr.precheck_audio(_speech_like(0.2), 16000, _enabled_settings())
    assert gate is not None
    assert gate["status"] == tr.STATUS_NO_SPEECH
    assert gate["reason"] == tr.REASON_AUDIO_TOO_SHORT
    assert gate["duration"] == 0.2
    assert gate["precheck"]["duration_sec"] == 0.2
    assert gate["precheck"]["min_duration_seconds"] == 0.4


def test_precheck_flags_steady_tone_as_low_snr():
    """稳态单音上下分位相同 ⇒ SNR≈0 dB，属噪声/音乐类片段。"""
    import numpy as np

    timeline = np.arange(int(2.0 * 16000)) / 16000
    tone = (0.3 * np.sin(2 * np.pi * 220.0 * timeline)).astype("float32")
    gate = tr.precheck_audio(tone, 16000, _enabled_settings())
    assert gate is not None
    assert gate["reason"] == tr.REASON_LOW_SNR
    assert gate["precheck"]["snr_db"] < 6.0
    assert gate["precheck"]["frame_cv"] < tr.PRECHECK_STATIONARY_CV


def test_precheck_stationary_noise_is_rejected():
    """白噪声同样低 SNR 且几乎不波动，属稳态噪声。"""
    import numpy as np

    noise = (0.1 * np.random.default_rng(11).standard_normal(2 * 16000)).astype("float32")
    gate = tr.precheck_audio(noise, 16000, _enabled_settings())
    assert gate is not None
    assert gate["reason"] == tr.REASON_LOW_SNR
    assert gate["precheck"]["frame_cv"] < tr.PRECHECK_STATIONARY_CV


def test_precheck_low_snr_but_modulated_audio_passes():
    """低 SNR 但有明显能量起伏（带背景声/伴奏的语音）不得误杀。

    真实回归（2026-11-05）：diler-science-kids-pop w04/w06 实测 SNR 5.07/5.54 dB、
    帧 RMS CV 0.21/0.26，Whisper 仍能转出有效文本；旧判据按全局分位比一刀切会误杀。
    """
    import numpy as np

    rate = 16000
    timeline = np.arange(int(4.0 * rate)) / rate
    envelope = np.zeros_like(timeline)
    for start in np.arange(0.0, 4.0, 0.4):
        envelope[(timeline >= start) & (timeline < start + 0.25)] = 1.0
    rng = np.random.default_rng(20261106)
    speech = 0.20 * np.sin(2 * np.pi * 220.0 * timeline) * envelope
    noisy = (speech + 0.15 * rng.standard_normal(timeline.size)).astype("float32")

    gate = tr.precheck_audio(noisy, rate, _enabled_settings())
    assert gate is None
    assert tr.estimate_snr_db(noisy, rate) < 6.0
    assert tr.frame_rms_cv(noisy, rate) >= tr.PRECHECK_STATIONARY_CV


def test_precheck_passes_speech_like_audio():
    assert tr.precheck_audio(_speech_like(2.0), 16000,
                             _enabled_settings()) is None


def test_precheck_can_be_disabled():
    import numpy as np

    silence = np.zeros(tr.WHISPER_SAMPLE_RATE, dtype="float32")
    assert tr.precheck_audio(silence, 16000,
                             _settings_with(precheck=False)) is None


def test_precheck_config_defaults_and_bounds():
    from pydantic import ValidationError

    from utils.schemas import TranscriptionConfig

    cfg = TranscriptionConfig()
    assert cfg.precheck is True
    assert cfg.min_duration_seconds == 0.4
    assert cfg.min_snr_db == 6.0

    loaded = inference.load_config()
    assert loaded.transcription.precheck is True
    assert loaded.transcription.min_duration_seconds == 0.4
    assert loaded.transcription.min_snr_db == 6.0

    base = {"operator": {"name": "multimodal_preprocessing", "version": "v1.0.0"}}
    with pytest.raises(ValidationError):
        OperatorConfig.model_validate({**base, "transcription": {"min_duration_seconds": 0}})
    with pytest.raises(ValidationError):
        OperatorConfig.model_validate({**base, "transcription": {"min_snr_db": 100}})


def test_precheck_hit_is_counted_without_model_load(tmp_path, monkeypatch):
    """全链路：静音样本走预检 → no_speech 落桶，模型统计保持未加载。"""
    out = tmp_path / "out"
    records = [fixtures.make_av_record("av1", 2.0, 2.0)]
    fixtures.write_task(
        out / "cm08_precheck", records,
        media={"06_audio/av1.wav": _silent_wav_bytes(2.0),
               "media/original/av1.mp4": b"\x00\x00\x00\x18ftypmp42"})

    def _must_not_load(_self):
        raise AssertionError("预检命中不应加载 Whisper")

    monkeypatch.setattr(tr.WhisperTranscriber, "load", _must_not_load)
    result = inference.run(None, "cm08_precheck", output_root=str(out),
                           overrides={"enabled": True,
                                      "transcription_enabled": True})

    stats = result["statistics"]
    assert stats["precheck_skipped"] == 1
    assert stats["precheck_reasons"] == {tr.REASON_AUDIO_SILENT: 1}
    assert stats["no_speech"] == 1
    assert stats["transcription_model_loaded"] is False
    assert stats["transcription_loaded"] is False
    assert stats["transcription"]["precheck"] is True

    saved = _first_record(result)
    assert saved["transcription"]["status"] == tr.STATUS_NO_SPEECH
    assert saved["transcription"]["reason"] == tr.REASON_AUDIO_SILENT
    assert saved["transcription"]["duration"] == 2.0
    assert saved["transcription"]["precheck"]["snr_db"] is None
    assert saved["time_index"] == []
    assert saved["transcription_gate"]["direct_scoring_allowed"] is False
    assert saved["processing"]["07"]["process_status"] == "no_speech"
