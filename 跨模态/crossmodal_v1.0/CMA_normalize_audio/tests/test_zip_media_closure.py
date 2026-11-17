"""The public ZIP response must carry every current nested media reference."""
import json
import zipfile

import pytest

from tests.conftest import make_zip


@pytest.mark.parametrize("enabled", [True, False])
def test_http_zip_carries_and_remaps_nested_frames_and_asr_segments(client, unique_id, enabled):
    media = {"video/clip.mp4": b"original-video", "frames/top.jpg": b"top-frame",
             "frames/nested.jpg": b"nested-frame", "audio/part.wav": b"asr-segment"}
    record = {"sample_id": "nested", "video": {
        "path": "video/clip.mp4", "original_path": "history/missing.mp4",
        "frames": [{"path": "frames/top.jpg"}],
        "segments": [{"frames": [{"path": "frames/top.jpg"},
                                  {"path": "frames/nested.jpg"}]}]},
        "transcription": {"paragraphs": [{"segment_path": "audio/part.wav", "text": "hello"}]},
        "metadata": {"files": [{"path": "history/missing.srt"}]}}
    payload = make_zip(dict(media, **{"input.jsonl": json.dumps(record) + "\n"}))
    body = client.post("/predict?detection=M07&uniqueId=" + unique_id,
                       data={"enabled": str(enabled).lower()},
                       files={"file": ("input.zip", payload, "application/zip")}).json()
    assert body["status"] == 1, body
    with zipfile.ZipFile(body["outputFile"]["zip"]) as archive:
        assert archive.testzip() is None
        manifest = json.loads(archive.read("manifest.json"))
        result = json.loads(archive.read(manifest["entrypoint"]).splitlines()[0])
        video = result["video"]
        refs = [(video["path"], media["video/clip.mp4"]),
                (video["frames"][0]["path"], media["frames/top.jpg"]),
                (video["segments"][0]["frames"][0]["path"], media["frames/top.jpg"]),
                (video["segments"][0]["frames"][1]["path"], media["frames/nested.jpg"]),
                (result["transcription"]["paragraphs"][0]["segment_path"], media["audio/part.wav"])]
        for relative, expected in refs:
            assert archive.read(relative) == expected
        assert video["frames"][0]["path"] == video["segments"][0]["frames"][0]["path"]
        assert result["metadata"] == record["metadata"]
        assert "history/missing.srt" not in archive.namelist()
