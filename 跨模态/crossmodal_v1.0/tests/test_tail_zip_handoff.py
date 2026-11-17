"""Real HTTP tail-chain tests with explicit synthetic human-review fixtures.

These fixtures are contract tests, not evidence that original business input
has passed any manual gate.
"""
import importlib.util
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

ROOT = Path(__file__).resolve().parents[1]
PACKAGES = {23: "CMA_package_aligned_corpus", 24: "CMA_writeback_sampling_quality_report",
            25: "CMA_manage_manual_review_task", 26: "CMA_generate_delivery_sample"}


@pytest.fixture(scope="module")
def servers(tmp_path_factory):
    base = tmp_path_factory.mktemp("tail-http")
    running, endpoints = [], {}
    try:
        for stage, package in PACKAGES.items():
            with socket.socket() as sock:
                sock.bind(("127.0.0.1", 0))
                port = sock.getsockname()[1]
            log = (base / ("m%d-server.log" % stage)).open("w", encoding="utf-8")
            env = dict(os.environ, OUTPUT_DIR=str(base / ("m%d" % stage)),
                       USE_TF="0", USE_FLAX="0", PYTHONIOENCODING="utf-8")
            process = subprocess.Popen([sys.executable, "-m", "uvicorn", "app:app",
                                        "--host", "127.0.0.1", "--port", str(port)],
                                       cwd=ROOT / package, env=env, stdout=log,
                                       stderr=subprocess.STDOUT,
                                       creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
            running.append((process, log))
            endpoint = "http://127.0.0.1:%d" % port
            for _ in range(150):
                if process.poll() is not None:
                    pytest.fail("M%d exited: %s" % (stage, log.name))
                try:
                    if requests.get(endpoint + "/health", timeout=0.3).status_code == 200:
                        break
                except requests.RequestException:
                    pass
                time.sleep(0.1)
            else:
                pytest.fail("M%d did not become healthy" % stage)
            endpoints[stage] = endpoint
        yield endpoints
    finally:
        for process, log in running:
            process.terminate()
            process.wait(timeout=15)
            log.close()


def _source(tmp_path, *, written_back=True, verdict="pass", manual_verdict=None):
    spec = importlib.util.spec_from_file_location(
        "tail_source_fixtures", ROOT / PACKAGES[23] / "tests" / "conftest.py")
    fixtures = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(fixtures)
    extra = {} if verdict is None else {
        "human_review": {"verdict": verdict, "reviewer": "synthetic-fixture-reviewer",
                         "reviewed_at": "2026-11-11T10:00:00+08:00"}}
    if manual_verdict is not None:
        extra.update(alignment={"status": "uncertain"}, manual_review={
            "verdict": manual_verdict, "reviewer": "synthetic-manual-fixture-reviewer"})
    rows = [fixtures.token_record("tail-%02d" % index, media=["media/cm-0001.png"],
                                  extra=extra) for index in range(10)]
    return fixtures.build_input_zip(tmp_path, written_back=written_back, records=rows).read_bytes()


def _predict(servers, stage, payload, uid, form=None):
    response = requests.post(servers[stage] + "/predict?uniqueId=" + uid,
                             files={"file": ("handoff.zip", payload, "application/zip")},
                             data=form,
                             timeout=60)
    assert response.status_code == 200, response.text
    return response.json()


def _zip(body):
    assert body["status"] == 1, body
    return Path(body["outputFile"]["zip"]).read_bytes()


def _chain(servers, tmp_path, *, written_back=True, verdict="pass"):
    bodies = {}
    payload = _source(tmp_path, written_back=written_back, verdict=verdict)
    for stage in (23, 24, 25):
        bodies[stage] = _predict(servers, stage, payload, "%s_m%d" % (tmp_path.name, stage))
        (tmp_path / ("m%d_response.json" % stage)).write_text(
            json.dumps(bodies[stage], ensure_ascii=False, indent=2), encoding="utf-8")
        payload = _zip(bodies[stage])
    bodies[26] = _predict(servers, 26, payload, tmp_path.name + "_m26")
    (tmp_path / "m26_response.json").write_text(
        json.dumps(bodies[26], ensure_ascii=False, indent=2), encoding="utf-8")
    return bodies


def test_deliverable_zip_crosses_all_four_http_operators(servers, tmp_path):
    bodies = _chain(servers, tmp_path)
    assert bodies[23]["publish_status"] == "deliverable"
    assert bodies[24]["quality_report_status"] == "completed"
    assert bodies[26]["status"] == 1, bodies[26]
    assert bodies[26]["sample_count"] == 10
    with zipfile.ZipFile(io.BytesIO(_zip(bodies[25]))) as archive:
        manifest = json.loads(archive.read("manifest.json"))
        handoff = manifest["delivery_handoff"]
        assert handoff["format"] == "crossmodal-delivery-handoff-v1"
        assert [step["operator_number"] for step in handoff["reviews"]] == [24, 25]
        assert archive.read(handoff["archive"]["path"])


@pytest.mark.parametrize("written_back,verdict", [(False, "pass"), (True, None)])
def test_original_pending_or_new_pending_stays_blocked(servers, tmp_path, written_back, verdict):
    bodies = _chain(servers, tmp_path, written_back=written_back, verdict=verdict)
    assert bodies[26]["status"] == 0, bodies[26]


def test_new_human_rejection_cannot_be_hidden_by_original_deliverable(servers, tmp_path):
    bodies = _chain(servers, tmp_path, verdict="fail")
    assert bodies[23]["publish_status"] == "deliverable"
    assert bodies[26]["status"] == 1, bodies[26]
    assert bodies[26]["sample_count"] == 9


def test_new_m25_pending_cannot_be_hidden_by_older_completed_reports(servers, tmp_path):
    payload = _source(tmp_path, manual_verdict="uncertain")
    original = _predict(servers, 23, payload, tmp_path.name + "_m23")
    reviewed = _predict(servers, 24, _zip(original), tmp_path.name + "_m24")
    assert reviewed["quality_report_status"] == "completed"
    manual = _predict(servers, 25, _zip(reviewed), tmp_path.name + "_m25",
                       form={"enabled": "true", "allow_degraded": "true"})
    assert manual["status"] == 1, manual
    result = _predict(servers, 26, _zip(manual), tmp_path.name + "_m26")
    assert result["status"] == 0, result


def test_native_m23_delivery_archive_remains_supported(servers, tmp_path):
    original = _predict(servers, 23, _source(tmp_path), tmp_path.name + "_m23")
    with zipfile.ZipFile(io.BytesIO(_zip(original))) as archive:
        handoff = json.loads(archive.read("manifest.json"))["delivery_handoff"]
        native = archive.read(handoff["archive"]["path"])
    result = _predict(servers, 26, native, tmp_path.name + "_m26")
    assert result["status"] == 1 and result["sample_count"] == 10, result


def test_m24_missing_quality_report_returns_status_zero_instead_of_http_500(servers, tmp_path):
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w") as archive:
        archive.writestr("rows.jsonl", '{"sample_id":"no-quality-report"}\n')
        archive.writestr("manifest.json", json.dumps({
            "format": "crossmodal-manifest-v1", "entrypoint": "rows.jsonl"}))
    result = _predict(servers, 24, buffer.getvalue(), tmp_path.name + "_m24")
    assert result["status"] == 0, result


@pytest.mark.parametrize("tamper", ["missing", "ambiguous", "unsafe", "sha256"])
def test_handoff_requires_unique_safe_digest_bound_archive(servers, tmp_path, tamper):
    body = _predict(servers, 23, _source(tmp_path), tmp_path.name + "_source23")
    with zipfile.ZipFile(io.BytesIO(_zip(body))) as archive:
        files = {name: archive.read(name) for name in archive.namelist()}
    manifest = json.loads(files["manifest.json"])
    if tamper == "missing":
        manifest.pop("delivery_handoff", None)
    else:
        handoff = manifest.get("delivery_handoff", {})
        if tamper == "ambiguous":
            handoff["archive"] = [{"path": "one.zip"}, {"path": "two.zip"}]
        elif tamper == "unsafe":
            handoff["archive"] = {"path": "../outside.zip", "sha256": "0" * 64}
        else:
            handoff.setdefault("archive", {})["sha256"] = "0" * 64
        manifest["delivery_handoff"] = handoff
    files["manifest.json"] = json.dumps(manifest).encode()
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w") as archive:
        for name, payload in files.items():
            archive.writestr(name, payload)
    result = _predict(servers, 26, buffer.getvalue(), tmp_path.name + "_tamper26")
    assert result["status"] == 0, result
