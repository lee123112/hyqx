"""Enabled public ZIP route applicability; synthetic media, explicit test encoder."""
import io
import json
from pathlib import Path
import wave
import zipfile

import pytest
from fastapi.testclient import TestClient

import app as api
import inference
from fixtures import StubEncoder, default_frames, default_media, make_flat_record, zip_bytes
from utils import backends, video_text as vt


def _media(tmp_path):
    import cv2
    import numpy as np

    result = default_media()
    video = tmp_path / "real.mp4"
    writer = cv2.VideoWriter(str(video), cv2.VideoWriter_fourcc(*"mp4v"), 4.0, (32, 24))
    assert writer.isOpened()
    for _ in range(2):
        writer.write(np.full((24, 32, 3), (40, 40, 210), dtype=np.uint8))
    writer.release()
    result["video/clip.mp4"] = video.read_bytes()
    audio = io.BytesIO()
    with wave.open(audio, "wb") as handle:
        handle.setnchannels(1); handle.setsampwidth(2); handle.setframerate(16000)
        handle.writeframes(b"\x00\x10\x00\xf0" * 400)
    result["media/audio.wav"] = audio.getvalue()
    return result


def _non_video(kind, *, native=True):
    path = "frames/frame_0001.png" if kind == "image" else "media/audio.wav"
    text = {"sample_id": "text-" + kind, "text": {"content": "Non-video fixture text."},
            "video": None}
    media = {"sample_id": "media-" + kind, kind: {"path": path}, "video": None}
    if not native:
        return {**text, **media, "sample_id": "flat-" + kind,
                "modalities": [kind, "text"], "text": text["text"]}
    return {"pair_id": "pair-" + kind, "sample_id": "pair-" + kind,
            "modality_pair": ["text", kind],
            "left": {"modalities": ["text"], "record": text},
            "right": {"modalities": [kind], "record": media},
            "processing": {"12": {"passed": kind == "image"}}}


def _post(tmp_path, monkeypatch, config_file, rows, encoder=None):
    monkeypatch.setenv("OUTPUT_DIR", str(tmp_path / "out"))
    monkeypatch.setenv("OPERATOR_CONFIG", str(config_file))
    monkeypatch.setattr(api, "ENCODER_PROVIDER", None if encoder is None else lambda *args: encoder)
    payload = {"rows.jsonl": "".join(json.dumps(row) + "\n" for row in rows).encode(),
               "manifest.json": json.dumps({"format": "crossmodal-manifest-v1",
                                             "entrypoint": "rows.jsonl"}).encode()}
    payload.update(_media(tmp_path))
    with TestClient(api.app) as client:
        response = client.post("/predict?uniqueId=non_video_zip",
                               data={"enabled": "true", "allow_degraded": "false"},
                               files={"file": ("input.zip", zip_bytes(payload), "application/zip")})
    assert response.status_code == 200
    return response.json()


def _rows(body, name=None):
    reference = body["outputFile"]
    with zipfile.ZipFile(reference["zip"]) as archive:
        return [json.loads(line) for line in archive.read(name or reference["entrypoint"]).decode().splitlines()]


def test_enabled_non_video_public_zip_never_requires_clip(tmp_path, monkeypatch, config_file):
    calls = []
    def unavailable(*args):
        calls.append(True)
        return None, "test_fixture_missing_weights", None
    monkeypatch.setattr(backends, "build_clip_backend", unavailable)
    monkeypatch.setattr(backends, "get_registered_encoder", lambda *args: None)
    records = [_non_video(kind, native=native)
               for kind in ("image", "audio") for native in (True, False)]
    body = _post(tmp_path, monkeypatch, config_file, records)
    assert body["status"] == 1, body
    assert calls == []
    rows = _rows(body)
    assert len(rows) == 4 and _rows(body, vt.PENDING_NAME) == []
    for original, row in zip(records, rows):
        node = row["processing"]["14"]
        assert node["process_status"] == "not_applicable"
        assert node["route_enabled"] is False and node["passed"] is None
        assert node["video_text_score"] is None and node["segment_mean"] is None
        for key in original:
            if key != "processing":
                assert row[key] == original[key]
    stats = body["result"]["statistics"]
    assert stats["not_applicable_records"] == 4
    assert stats["temporal_route_not_applicable"] == 0
    assert stats["scored_records"] == stats["pending_records"] == 0


def test_mixed_public_zip_scores_only_video_and_keeps_real_pending_and_isolation(
        tmp_path, monkeypatch, config_file):
    encoder = StubEncoder(scores={"passes": 0.6, "fails": 0.05})
    records = [_non_video("image"), _non_video("audio"),
               make_flat_record("video-pass", "passes", default_frames(2)),
               make_flat_record("video-fail", "fails", default_frames(2)),
               make_flat_record("video-no-frames", "passes", []),
               {"sample_id": "declared-video", "modalities": ["video", "text"],
                "text": "Unresolved video.", "image": {"path": "frames/frame_0001.png"}},
               make_flat_record("temporal", "passes", default_frames(2),
                                timeline={"text": [{"start": 0, "end": 1, "content": "x"}]})]
    body = _post(tmp_path, monkeypatch, config_file, records, encoder)
    assert body["status"] == 1, body
    rows = {row["sample_id"]: row for row in _rows(body)}
    assert len(rows) == 6 and "video-fail" not in rows
    for identifier in ("pair-image", "pair-audio"):
        assert rows[identifier]["processing"]["14"]["route_enabled"] is False
    for identifier in ("declared-video", "video-no-frames"):
        node = rows[identifier]["processing"]["14"]
        assert node["route_enabled"] is True and node["process_status"] == "pending"
        assert node["passed"] is None and node["video_text_score"] is None
    stats = body["result"]["statistics"]
    assert stats["not_applicable_records"] == 3
    assert stats["temporal_route_not_applicable"] == 1
    assert stats["pending_records"] == 2 and stats["scored_records"] == 2
    assert stats["sample_isolated"] == 1 and stats["conservation_ok"] is True
    assert encoder.text_calls == ["passes", "fails"]


def test_native_audio_view_uses_active_pair_modalities_over_origin_modality_hint(
        tmp_path, monkeypatch, config_file):
    record = _non_video("audio")
    for side in ("left", "right"):
        record[side]["record"]["modality"] = ["video", "audio", "text"]
    monkeypatch.setattr(backends, "get_registered_encoder", lambda *args: None)
    monkeypatch.setattr(backends, "build_clip_backend",
                        lambda *args: (None, "test_fixture_missing_weights", None))
    body = _post(tmp_path, monkeypatch, config_file, [record])
    assert body["status"] == 1, body
    node = _rows(body)[0]["processing"]["14"]
    assert node["process_status"] == "not_applicable" and node["route_enabled"] is False


@pytest.mark.parametrize("declaration", [
    {"video": {}}, {"video_path": "video/clip.mp4"},
    {"modality_pair": ["text", "video"]}, {"modalities": ["video", "text"]}])
def test_declared_video_with_unresolved_reference_is_never_waived(config_file, declaration):
    record = {**_non_video("image", native=False), **declaration}
    config = inference.load_config(config_file, {"enabled": True})
    outcome = vt.evaluate_record(vt.resolve_pair(record), 1, media_roots=[], config=config,
                                 encoder=None, event_time="2026-11-11T16:00:00+08:00")
    assert outcome["action"] == vt.ACTION_PENDING
    assert outcome["node"]["route_enabled"] is True
    assert outcome["node"]["passed"] is None
