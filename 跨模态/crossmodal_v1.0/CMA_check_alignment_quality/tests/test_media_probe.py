"""媒体探测测试：全量文件可打开、视频可解码、音频非全静音、文本非空。

全部使用脚本合成的真实媒体，**不使用任何固定返回值或 mock 探测结果**。
"""
from __future__ import annotations

from pathlib import Path

from utils import media_probe


def test_available_backends_reports_real_sources() -> None:
    backends = media_probe.available_backends()
    # 本机实测可用的能力必须被如实报告
    assert backends["image_decoder"] in ("pillow", None)
    assert backends["video_decoder"] in ("opencv", "ffmpeg", None)
    assert backends["audio_decoder"] in ("soundfile", "wave", "ffmpeg", None)


def test_probe_image_ok(media_root: Path) -> None:
    result = media_probe.probe_image(media_root / "media" / "ok_a.png")
    assert result["status"] == "ok"
    assert (result["width"], result["height"]) == (32, 24)
    assert result["format"].upper() == "PNG"


def test_probe_image_missing(media_root: Path) -> None:
    result = media_probe.probe_image(media_root / "media" / "nope.png")
    assert result["status"] == "missing"


def test_probe_image_unreadable(media_root: Path) -> None:
    result = media_probe.probe_image(media_root / "media" / "broken.png")
    assert result["status"] == "unreadable"
    assert result["error"]


def test_probe_video_decodes_real_frames(media_root: Path) -> None:
    result = media_probe.probe_video(media_root / "media" / "clip_a.mp4", max_frames=4)
    assert result["status"] == "ok"
    assert result["frames_probed"] >= 1
    assert result["frames_probed"] <= 4


def test_probe_video_undecodable(media_root: Path) -> None:
    result = media_probe.probe_video(media_root / "media" / "broken.mp4")
    assert result["status"] == "undecodable"


def test_probe_video_missing(media_root: Path) -> None:
    assert media_probe.probe_video(media_root / "media" / "nope.mp4")["status"] == "missing"


def test_probe_audio_tone_not_silent(media_root: Path) -> None:
    result = media_probe.probe_audio(media_root / "media" / "tone.wav")
    assert result["status"] == "ok"
    assert result["silent"] is False
    assert result["rms"] > 0
    assert result["sample_rate"] == 16000
    assert 0.2 < result["duration"] < 0.3


def test_probe_audio_detects_all_silent(media_root: Path) -> None:
    result = media_probe.probe_audio(media_root / "media" / "silent.wav")
    assert result["status"] == "ok"
    assert result["silent"] is True
    assert result["rms"] == 0.0


def test_probe_audio_undecodable(media_root: Path) -> None:
    assert media_probe.probe_audio(media_root / "media" / "broken.wav")["status"] == "undecodable"


def test_probe_audio_missing(media_root: Path) -> None:
    assert media_probe.probe_audio(media_root / "media" / "nope.wav")["status"] == "missing"


def test_silence_threshold_is_configurable(media_root: Path) -> None:
    strict = media_probe.probe_audio(media_root / "media" / "tone.wav", silence_rms_threshold=0.2)
    assert strict["silent"] is True


def test_text_state_counts_real_characters() -> None:
    state = media_probe.text_state("  红色方块  ")
    assert state["nonempty"] is True
    assert state["chars"] == 4

    blank = media_probe.text_state("   \n\t ")
    assert blank["nonempty"] is False
    assert blank["chars"] == 0


def test_text_state_accepts_structured_value() -> None:
    assert media_probe.text_state({"content": "金毛犬在草地上"})["nonempty"] is True
    assert media_probe.text_state({"content": "   "})["nonempty"] is False
    assert media_probe.text_state(None)["nonempty"] is False


def test_snr_db_is_really_measured(media_root: Path) -> None:
    """响亮段+安静段的真实波形 → SNR 必须为正；恒定纯音如实为 ≈0 dB。"""
    mixed = media_probe.probe_audio(media_root / "media" / "mixed.wav")
    assert mixed["snr_db"] is not None
    assert mixed["snr_db"] > 0

    tone = media_probe.probe_audio(media_root / "media" / "tone.wav")
    assert tone["snr_db"] is not None
    assert abs(tone["snr_db"]) < 1.0

    silent = media_probe.probe_audio(media_root / "media" / "silent.wav")
    assert silent["snr_db"] is None          # 全静音无法给出 SNR，不编造
