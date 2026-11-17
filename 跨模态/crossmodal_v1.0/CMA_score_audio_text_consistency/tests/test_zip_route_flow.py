"""Form-one M13 forwards live records without mixing other routes into CER results."""
import json
import zipfile
from copy import deepcopy
from pathlib import Path
import pytest

from tests import fixtures
from tests.test_api import bundle_zip


def predict(client, rows, media):
    return client.post("/predict?detection=M13&uniqueId=routing_file",
                       data={"enabled": "true"},
                       files={"file": ("input.zip", bundle_zip(rows, media), "application/zip")}).json()


def read(z, name):
    return [json.loads(line) for line in z.read(name).splitlines() if line]


def test_enabled_mixed_zip_forwards_in_input_order_without_releasing_isolation(client):
    media = dict(fixtures.default_media(), **{"images/a.jpg": b"image", "video/v.avi": b"video"})
    image = {"sample_id": "image-flat", "image": {"path": "images/a.jpg"}, "text": "caption",
             "processing": {"12": {"passed": True}}}
    video = {"pair_id": "video-pair", "modality_pair": ["text", "video"],
             "left": {"record": {"sample_id": "caption", "text": "caption"}},
             "right": {"record": {"sample_id": "movie", "video": {"path": "video/v.avi"}}}}
    main = fixtures.make_pair("pass", transcript="今晚下雨", description="今晚下雨")
    pending = fixtures.make_flat_record("wait", transcript="", description="今晚下雨")
    isolated = fixtures.make_pair("isolated", transcript="大河向东流", description="月亮明晃晃")
    bad = {"text": "no sample or pair identifier"}
    body = predict(client, [image, main, video, pending, isolated, bad], media)
    assert body["status"] == 1, body
    with zipfile.ZipFile(body["outputFile"]["zip"]) as z:
        manifest = json.loads(z.read("manifest.json"))
        assert manifest["entrypoint"] == "13_flow_records.jsonl"
        assert manifest["business_entrypoint"] == "13_audio_text_scores.jsonl"
        flow = read(z, manifest["entrypoint"])
        assert [r.get("pair_id") or r.get("sample_id") for r in flow] == [
            "image-flat", "pass", "video-pair", "wait"]
        assert [r["pair_id"] for r in read(z, manifest["business_entrypoint"])] == ["pass"]
        assert flow[1]["processing"]["13"]["cer"] == 0
        assert flow[0]["processing"]["12"] == image["processing"]["12"]
        assert flow[2]["right"] == video["right"]
        for row in (flow[0], flow[2]):
            node = row["processing"]["13"]
            assert node["process_status"] == "not_applicable" and node["route_enabled"] is False
            assert node["passed"] is None and node["cer"] is None
        wait = flow[-1]["processing"]["13"]
        assert wait["process_status"] == "pending" and wait["route_enabled"] is True
        assert wait["reason"] == "missing_transcript" and wait["passed"] is None and wait["cer"] is None
        assert len(read(z, "13_pending.jsonl")) == 1
        assert read(z, "13_sample_quarantine.jsonl")[0]["pair_id"] == "isolated"
        assert len(read(z, "13_rejects.jsonl")) == 1
        for path in ("images/a.jpg", "video/v.avi", "media/seg_01.wav"):
            assert z.read(path) == media[path]
    assert body["result"]["statistics"]["conservation_ok"]


@pytest.mark.parametrize("evidence", [
    {"modalities": ["image", "audio", "text"]},
    {"media": [{"role": "audio", "path": "part.wav"}]},
    {"processing": {"17": {"paths": {"audio": "part.wav"}}}},
])
def test_missing_declared_audio_is_pending_and_not_waived_by_image(client, evidence):
    row = {"sample_id": "incomplete", **evidence,
           "image": "pic.jpg", "text": {"content": "真实音频仍缺失"}}
    body = predict(client, [row], {"pic.jpg": b"image", "part.wav": b"undecoded"})
    assert body["status"] == 1, body
    assert body["decision"] == "pending"
    with zipfile.ZipFile(body["outputFile"]["zip"]) as z:
        entry = json.loads(z.read("manifest.json"))["entrypoint"]
        node = read(z, entry)[0]["processing"]["13"]
        assert node["route_enabled"] and node["process_status"] == "pending"
        assert node["reason"] == "audio_reference_unresolved"
        assert node["passed"] is None and node["cer"] is None


def test_m11_native_views_use_current_pair_modalities_and_retain_audio_evidence(client):
    # Unmodified plan_pairs output for one record containing text, image and audio.
    fixture = Path(__file__).parent / "data" / "m11_origin_modality_pairs.jsonl"
    image_pair, audio_pair = [json.loads(line) for line in fixture.read_text(encoding="utf-8").splitlines()]
    assert image_pair["modality_pair"] == ["text", "image"]
    assert image_pair["right"]["record"]["modality"] == ["text", "image", "audio"]
    missing_audio = deepcopy(audio_pair)
    missing_audio["right"]["record"]["audio"] = None
    wrapper_audio = deepcopy(image_pair)
    wrapper_audio["pair_id"] = "wrapper-declares-audio"
    wrapper_audio["right"]["modalities"] = ["image", "audio"]
    real_audio = deepcopy(image_pair)
    real_audio["pair_id"] = "payload-includes-audio"
    real_audio["right"]["record"]["audio"] = {"path": "a.wav"}
    records = [image_pair, missing_audio, wrapper_audio, real_audio]
    body = predict(client, records, {"i.png": b"fixture-image", "a.wav": b"fixture-audio"})
    assert body["status"] == 1, body
    with zipfile.ZipFile(body["outputFile"]["zip"]) as z:
        flow = read(z, "13_flow_records.jsonl")
    assert [row["pair_id"] for row in flow] == [row["pair_id"] for row in records]
    nodes = [row["processing"]["13"] for row in flow]
    assert [(n["process_status"], n["route_enabled"], n["reason"]) for n in nodes] == [
        ("not_applicable", False, "no_audio_text_pair"),
        ("pending", True, "audio_reference_unresolved"),
        ("pending", True, "audio_reference_unresolved"),
        ("pending", True, "missing_transcript"),
    ]
    assert all(n["passed"] is None and n["cer"] is None for n in nodes)
