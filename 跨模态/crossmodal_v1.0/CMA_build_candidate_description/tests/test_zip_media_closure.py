"""Exercise nested media carrying at the real multipart/output ZIP seam."""
import io
import json
import zipfile

import pytest
from fastapi.testclient import TestClient

import app as api


def _package(record, media):
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as archive:
        archive.writestr("input.jsonl", json.dumps(record) + "\n")
        for relative, content in media.items():
            archive.writestr(relative, content)
    return buf.getvalue()


def test_http_zip_preserves_nested_media_and_optional_provenance(tmp_path, monkeypatch):
    monkeypatch.setattr(api, "OUTPUT_ROOT", tmp_path)
    media = {"video/clip.mp4": b"original-video", "frames/top.jpg": b"top-frame",
             "frames/nested.jpg": b"nested-frame", "audio/part.wav": b"asr-segment"}
    record = {"sample_id": "nested", "caption": "A person walks slowly beside a river.",
              "video": {"path": "video/clip.mp4", "original_path": "history/missing.mp4",
                        "frames": [{"path": "frames/top.jpg"}],
                        "segments": [{"frames": [{"path": "frames/top.jpg"},
                                                  {"path": "frames/nested.jpg"}]}]},
              "transcription": {"paragraphs": [{"segment_path": "audio/part.wav"}]},
              "metadata": {"files": [{"path": "history/missing.srt"}]}}
    body = TestClient(api.app).post("/predict?detection=M09&uniqueId=media_closure",
        files={"file": ("input.zip", _package(record, media), "application/zip")}).json()
    assert body["status"] == 1, body
    with zipfile.ZipFile(body["outputFile"]["zip"]) as archive:
        assert archive.testzip() is None
        manifest = json.loads(archive.read("manifest.json"))
        result = json.loads(archive.read(manifest["entrypoint"]).splitlines()[0])
        assert result["video"] == record["video"]
        assert result["transcription"] == record["transcription"]
        assert result["metadata"] == record["metadata"]
        for relative, expected in media.items():
            assert archive.read(relative) == expected
        assert "history/missing.srt" not in archive.namelist()


def test_collect_media_recurses_pair_records_but_ignores_provenance(tmp_path):
    path = tmp_path / "pairs.jsonl"
    path.write_text(json.dumps({"left": {"record": {"video": {"segments": [
        {"frames": [{"path": "frame.jpg"}]}]}, "metadata": {"image": "old.jpg"}}},
        "right": {"record": {"transcription": {"paragraphs": [
            {"segment_path": "part.wav"}]}}}, "media": {
                "original_path": "history.wav", "files": ["history.jpg"]}}) + "\n", encoding="utf-8")
    assert api._collect_media([path]) == {"frame.jpg", "part.wav"}


@pytest.mark.parametrize("diagnostics", [
    {"processing": {"07": {"model": {"audio": {"path": "models/audio.pt"}}}}},
    {"model": {"video": {"path": "models/video.pt"}}},
    {"history": [{"image": {"path": "history/old.jpg"}}]},
])
def test_http_zip_ignores_media_names_inside_processing_model_and_history(
        tmp_path, monkeypatch, diagnostics):
    monkeypatch.setattr(api, "OUTPUT_ROOT", tmp_path)
    record = {"sample_id": "caption_only", "caption": "A person walks slowly beside a river.",
              **diagnostics,
              "metadata": {"files": [{"path": "history/missing.srt"}]}}
    body = TestClient(api.app).post("/predict?detection=M09&uniqueId=diagnostic_paths",
        files={"file": ("input.zip", _package(record, {}), "application/zip")}).json()
    assert body["status"] == 1, body
    with zipfile.ZipFile(body["outputFile"]["zip"]) as archive:
        assert archive.testzip() is None
        manifest = json.loads(archive.read("manifest.json"))
        result = json.loads(archive.read(manifest["entrypoint"]).splitlines()[0])
        for key in (*diagnostics, "metadata"):
            if key == "processing":
                assert result[key]["07"] == record[key]["07"]
            else:
                assert result[key] == record[key]
        assert not {"models/audio.pt", "models/video.pt", "history/old.jpg",
                    "history/missing.srt"}.intersection(archive.namelist())


def test_http_zip_does_not_report_success_when_media_cannot_be_packed(tmp_path, monkeypatch):
    monkeypatch.setattr(api, "OUTPUT_ROOT", tmp_path)
    def fail_pack(*args):
        raise OSError("media copy failed")
    monkeypatch.setattr(api, "_pack", fail_pack)
    record = {"sample_id": "one", "caption": "A person walks slowly beside a river."}
    body = TestClient(api.app).post("/predict?detection=M09&uniqueId=pack_failure",
        files={"file": ("input.zip", _package(record, {}), "application/zip")}).json()
    assert body["status"] == 0, body
    assert "outputFile" not in body


def test_http_zip_missing_active_frame_fails_without_fabricating_media(tmp_path, monkeypatch):
    monkeypatch.setattr(api, "OUTPUT_ROOT", tmp_path)
    record = {"sample_id": "one", "caption": "A person walks slowly beside a river.",
              "video": {"path": "video.mp4", "segments": [{
                  "frames": [{"path": "missing.jpg"}]}]}}
    body = TestClient(api.app).post("/predict?detection=M09&uniqueId=missing_frame",
        files={"file": ("input.zip", _package(record, {"video.mp4": b"video"}),
                         "application/zip")}).json()
    assert body["status"] == 0, body
    assert "missing.jpg" in body["msg"]
    assert "outputFile" not in body
    assert not (tmp_path / "missing_frame" / api.inference.OPERATOR / "run001").exists()


def test_http_rewrite_only_media_survives_typed_media_map(tmp_path, monkeypatch):
    monkeypatch.setattr(api, 'OUTPUT_ROOT', tmp_path)
    row = {'sample_id': 'bare', 'image': {'path': 'photo.jpg'}}
    body = TestClient(api.app).post('/predict?detection=M09&uniqueId=rewrite_media',
        files={'file': ('input.zip', _package(row, {'photo.jpg': b'photo'}),
                         'application/zip')}).json()
    assert body['status'] == 1, body
    with zipfile.ZipFile(body['outputFile']['zip']) as archive:
        assert archive.read('photo.jpg') == b'photo'
        tasks = [json.loads(line) for name in archive.namelist() if 'rewrite' in name
                 and name.endswith('.jsonl') for line in archive.read(name).splitlines()]
        assert tasks[0]['media']['image'] == 'photo.jpg'


def test_missing_rewrite_image_does_not_block_valid_main_record(tmp_path, monkeypatch):
    monkeypatch.setattr(api, 'OUTPUT_ROOT', tmp_path)
    rows = [{'sample_id': 'valid', 'caption': 'A person walks slowly beside a river.'},
            {'sample_id': 'bare', 'image': {'path': 'missing-photo.jpg'}}]
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, 'w') as archive:
        archive.writestr('input.jsonl', '\n'.join(json.dumps(row) for row in rows) + '\n')
    body = TestClient(api.app).post('/predict?detection=M09&uniqueId=optional_rewrite',
        files={'file': ('input.zip', buffer.getvalue(), 'application/zip')}).json()
    assert body['status'] == 1, body
    with zipfile.ZipFile(body['outputFile']['zip']) as archive:
        manifest = json.loads(archive.read('manifest.json'))
        main = [json.loads(line) for line in archive.read(manifest['entrypoint']).splitlines()]
        assert [row['sample_id'] for row in main] == ['valid']
        tasks = [json.loads(line) for name in archive.namelist() if 'rewrite' in name
                 and name.endswith('.jsonl') for line in archive.read(name).splitlines()]
        assert len(tasks) == 1
        assert tasks[0]['sample_id'] == 'bare'
        assert tasks[0]['media']['image'] == 'missing-photo.jpg'
        assert manifest['missing_optional_media'] == [{'path': 'missing-photo.jpg',
            'role': 'review', 'reason': 'media_missing', 'required': False}]
        assert 'missing-photo.jpg' not in archive.namelist()
