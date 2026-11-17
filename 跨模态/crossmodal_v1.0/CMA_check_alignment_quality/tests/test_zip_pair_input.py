"""Native M19 pairs must reach M20 QC through direct and real HTTP ZIP input."""
from __future__ import annotations

import copy
import io
import json
import os
from pathlib import Path
import socket
import subprocess
import sys
import time
import zipfile

import pytest
import requests

import inference
from tests.conftest import png_bytes, write_jsonl, write_png
from utils import alignment_qc as qc
from utils.media_carry import MediaCarrier


def native_pair():
    return {
        "sample_id": "stable-pair-19", "pair_id": "pair-11",
        "decision": "accepted", "score": 0.81,
        "processing": {"19": {"decision": "accepted", "score": 0.81}},
        "left": {"source_file": "text.jsonl", "line_no": 4, "record": {
            "sample_id": "text-side", "text": {"content": "A red square on a plain background."},
            "image": None, "qc": {"text_fluency": {"ppl": 38.0}}}},
        "right": {"source_file": "image.jsonl", "line_no": 8, "record": {
            "sample_id": "image-side", "text": {"content": "  "},
            "image": {"path": "media/pair.png", "brisque": 30.5},
            "qc": {"reverse_verify": {"passed": True, "score": 0.6,
                                       "check_status": "checked"}}}},
    }


def _assert_native_output(row, original):
    assert row["sample_id"] == original["sample_id"]
    assert row["left"] == original["left"]
    assert row["right"] == original["right"]
    assert row["decision"] == original["decision"]
    assert row["score"] == original["score"]
    assert row["processing"]["19"] == original["processing"]["19"]
    node = row["processing"]["20"]
    assert node["reason_code"] != "insufficient_modalities"
    assert set(node["modalities"]) == {"image", "text"}
    assert node["metrics"]["ppl"]["value"] == 38.0
    assert node["metrics"]["brisque"]["value"] == 30.5
    assert node["metrics"]["ppl"]["source"] == "upstream"


def test_native_pair_direct_run_retains_pair_and_namespaces(tmp_path, monkeypatch):
    monkeypatch.setenv("MODEL_ROOT", str(tmp_path / "no-models"))
    original = native_pair()
    source = write_jsonl(tmp_path / "input" / "pairs.jsonl", [original])
    write_png(source.parent / "media" / "pair.png")
    result = inference.run(str(source), "native_run", output_root=tmp_path / "out",
                           allow_external_input=True)
    assert result["statistics"]["sample_quarantined"] == 0
    rows = [json.loads(line) for line in
            (Path(result["runDir"]) / qc.MAIN_NAME).read_text(encoding="utf-8").splitlines()]
    assert len(rows) == 1
    _assert_native_output(rows[0], original)
    assert json.loads(source.read_text(encoding="utf-8")) == original


def test_native_pair_root_evidence_wins_without_mutating_input(tmp_path):
    original = native_pair()
    original["text"] = {"content": "Explicit root text must win."}
    original["qc"] = {"text_fluency": {"ppl": 42.0},
                       "reverse_verify": {"passed": False, "score": 0.1}}
    before = copy.deepcopy(original)
    write_png(tmp_path / "media" / "pair.png")
    outcome = qc.evaluate_record(original, sample_id=original["sample_id"],
                                 media_root=tmp_path,
                                 carrier=MediaCarrier(tmp_path, tmp_path / "out"),
                                 business=qc.default_business())
    assert outcome.reason_code == "reverse_inconsistent"
    assert outcome.metrics["ppl"]["value"] == 42.0
    assert outcome.record["text"] == before["text"]
    assert outcome.record["left"] == before["left"]
    assert outcome.record["right"] == before["right"]
    assert original == before


def test_native_pair_explicit_root_media_alias_takes_precedence(tmp_path):
    original = native_pair()
    original["caption"] = "Explicit root caption."
    original["image_path"] = "media/root-missing.png"
    write_png(tmp_path / "media" / "pair.png")
    outcome = qc.evaluate_record(original, sample_id=original["sample_id"],
                                 media_root=tmp_path,
                                 carrier=MediaCarrier(tmp_path, tmp_path / "out"),
                                 business=qc.default_business())
    assert outcome.reason_code == "media_missing"
    assert outcome.evidence["media"]["image"]["path"] == "media/root-missing.png"


def test_native_cross_source_pair_does_not_reuse_one_sides_reverse_result(tmp_path):
    original = native_pair()
    original['left']['record']['source_id'] = 'source-A'
    original['right']['record']['source_id'] = 'source-B'
    write_png(tmp_path / 'media/pair.png')
    outcome = qc.evaluate_record(original, sample_id=original['sample_id'], media_root=tmp_path,
        carrier=MediaCarrier(tmp_path, tmp_path / 'out'), business=qc.default_business())
    assert outcome.reverse is None
    assert outcome.reverse_candidate is not None
    assert outcome.checks[qc.CHECK_REVERSE_VERIFY]['reason'] == 'deferred_to_pool'
    assert 'reverse_verify' not in outcome.record.get('qc', {})
    assert original['right']['record']['qc']['reverse_verify']['passed'] is True


def test_flat_record_with_unrelated_right_extension_is_not_projected(tmp_path):
    original = {'sample_id': 'flat-text', 'text': {'content': 'Only the root text.'},
                'right': {'image': {'path': 'media/pair.png'}}}
    write_png(tmp_path / 'media/pair.png')
    outcome = qc.evaluate_record(original, sample_id=original['sample_id'], media_root=tmp_path,
        carrier=MediaCarrier(tmp_path, tmp_path / 'out'), business=qc.default_business())
    assert outcome.reason_code == 'insufficient_modalities'


@pytest.fixture
def live_http(tmp_path):
    """Exercise the deployed ASGI app over a real TCP HTTP connection."""
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        port = sock.getsockname()[1]
    env = dict(os.environ, OUTPUT_DIR=str(tmp_path / "http-output"),
               MODEL_ROOT=str(tmp_path / "no-models"), USE_TF="0", USE_FLAX="0")
    log = (tmp_path / "http-server.log").open("w", encoding="utf-8")
    process = subprocess.Popen(
        [sys.executable, "-m", "uvicorn", "app:app", "--host", "127.0.0.1",
         "--port", str(port)], cwd=Path(inference.__file__).parent,
        env=env, stdout=log, stderr=subprocess.STDOUT,
        creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
    base = "http://127.0.0.1:%d" % port
    try:
        for _ in range(100):
            if process.poll() is not None:
                pytest.fail("HTTP server exited; see %s" % log.name)
            try:
                if requests.get(base + "/health", timeout=0.5).status_code == 200:
                    break
            except requests.RequestException:
                pass
            time.sleep(0.1)
        else:
            pytest.fail("HTTP server did not become healthy")
        yield base
    finally:
        process.terminate()
        process.wait(timeout=15)
        log.close()


def test_native_pair_http_zip_keeps_one_sample_and_native_fields(live_http):
    original = native_pair()
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w") as archive:
        archive.writestr("pairs.jsonl", json.dumps(original) + "\n")
        archive.writestr("media/pair.png", png_bytes())
        archive.writestr("manifest.json", json.dumps({
            "format": "crossmodal-manifest-v1", "entrypoint": "pairs.jsonl",
            "artifacts": [{"path": "pairs.jsonl", "role": "main"}]}))
    response = requests.post(live_http + "/predict?uniqueId=native_http",
                             files={"file": ("native.zip", buffer.getvalue(), "application/zip")},
                             timeout=60)
    response.raise_for_status()
    body = response.json()
    assert body["status"] == 1, body
    assert body["hard_failures"] == []
    reference = body["outputFile"]
    with zipfile.ZipFile(reference["zip"]) as archive:
        rows = [json.loads(line) for line in
                archive.read(reference["entrypoint"]).decode("utf-8").splitlines()]
        assert len(rows) == 1
        _assert_native_output(rows[0], original)
        assert archive.read("media/pair.png") == png_bytes()
