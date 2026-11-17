"""M20 quality and isolation material must survive real package-mode calls."""
import hashlib
import json
import os
from pathlib import Path
import subprocess
import sys
import zipfile

import pytest

ROOT = Path(__file__).resolve().parents[1]
OPERATORS = {21: "CMA_dispose_mismatched_sample", 22: "CMA_generate_crossmodal_tokens",
             23: "CMA_package_aligned_corpus"}
REPORT = b'{"format":"crossmodal-quality-report-v1","status":"pending_writeback","checks":{}}\r\n'
ORIGIN = b'{"sample_id":"from20-origin"}\r\n'
SAMPLE = b'{"sample_id":"from20-sample"}'


def package(tmp_path, members, manifest):
    path = tmp_path / "input.zip"
    with zipfile.ZipFile(path, "w") as handle:
        for name, content in members.items():
            handle.writestr(name, content)
        handle.writestr("manifest.json", json.dumps(manifest))
    return path


def call(tmp_path, number, source):
    script = """
import app, json, sys
from fastapi.testclient import TestClient
with TestClient(app.app) as client, open(sys.argv[1],'rb') as stream:
    body = client.post('/predict', params={'detection':'M'+sys.argv[2],
        'uniqueId':'test_file'}, files={'file':('input.zip',stream,'application/zip')},
        data={'manual_platform_available':'true'} if sys.argv[2]=='21' else {}).json()
print('RESPONSE '+json.dumps(body,ensure_ascii=True))
"""
    env = dict(os.environ, OUTPUT_DIR=str(tmp_path / str(number)),
               PYTHONDONTWRITEBYTECODE="1", PYTHONIOENCODING="utf-8")
    p = subprocess.run([sys.executable, "-B", "-c", script, str(source), str(number)],
                       cwd=ROOT / OPERATORS[number], env=env, capture_output=True,
                       text=True, encoding="utf-8", check=True)
    return json.loads(next(line[9:] for line in p.stdout.splitlines() if line.startswith("RESPONSE ")))


def test_official_packages_forward_current_and_prior_quarantines(tmp_path):
    members = {"20_qc.jsonl": '{"sample_id":"from21","action":"lock","destination":"sample_isolation"}\n',
               "quality.json": REPORT, "origin.jsonl": ORIGIN, "sample.jsonl": SAMPLE}
    roles = {"quality.json": "quality_report", "origin.jsonl": "origin_quarantine",
             "sample.jsonl": "sample_quarantine"}
    manifest = {"format": "crossmodal-manifest-v1", "entrypoint": "20_qc.jsonl",
                "artifacts": [{"path": p, "role": r} for p, r in roles.items()]}
    current = package(tmp_path, members, manifest)
    for number in (21, 22):
        body = call(tmp_path, number, current)
        assert body['status'] == 1, body
        current = Path(body['outputFile']['zip'])
        with zipfile.ZipFile(current) as z:
            handoff = json.loads(z.read("manifest.json"))['material_handoff']
            items = {(d['stage'],d['role']): z.read(d['path']) for d in handoff['items']}
            for d in handoff['items']:
                assert hashlib.sha256(z.read(d['path'])).hexdigest() == d['sha256']
            assert items["M20", "quality_report"] == REPORT
            assert items["M20", "origin_quarantine"] == ORIGIN
            assert items["M20", "sample_quarantine"] == SAMPLE
            assert b'from21' in items["M21", "sample_quarantine"]
    result = call(tmp_path, 23, current)
    assert result['status'] == 1, result
    assert result['publish_status'] == 'pending'
    with zipfile.ZipFile(Path(result['outputFile']['runDir']) / result['package']) as z:
        assert z.read("materials/quality_report.json") == REPORT
        rows = [json.loads(l) for l in z.read("materials/isolation_list.jsonl").splitlines() if l]
        assert {r["sample_id"] for r in rows} >= {"from20-origin","from20-sample","from21"}
    assert 'sampling_conclusion' in result['missing_materials']
    assert 'version_notes' in result['missing_materials']


@pytest.mark.parametrize("operator", [22, 23])
@pytest.mark.parametrize("problem", ["path","digest","duplicate"])
def test_corrupt_material_binding_fails_without_fallback(tmp_path, operator, problem):
    entry = {"stage":"M20", "role":"quality_report", "path":"../bad.json" if problem=="path"
             else "quality.json", "sha256": "0"*64 if problem=="digest" else hashlib.sha256(REPORT).hexdigest()}
    items = [entry, dict(entry)] if problem == "duplicate" else [entry]
    archive = package(tmp_path, {"rows.jsonl":"", "quality.json":REPORT},
        {"format":"crossmodal-manifest-v1","entrypoint":"rows.jsonl",
         "material_handoff":{"format":"crossmodal-material-handoff-v1","items":items}})
    body = call(tmp_path, operator, archive)
    assert body['status'] == 0, body
    assert not isinstance(body.get('outputFile'), dict)


def test_nested_entry_cannot_shadow_verified_isolation(tmp_path):
    rows = [{"sample_id": sid, "token": "<image> example", "routes": ["image_text"],
             "modalities": ["image", "text"]} for sid in ("old", "new", "keep")]
    members = {"data/rows.jsonl": "\n".join(json.dumps(v) for v in rows),
               "data/quality_report.json": '{"writeback":{"completed":true}}',
               "data/sampling_conclusion.json": '{"written_back":true}',
               "data/isolation_list.jsonl": "",
               "data/version_notes.json": '{"enabled_routes":["image_text"],"annotation_granularity":"sample"}'}
    items = []
    for stage, role, payload in (("M20", "quality_report", REPORT),
                                 ("M20", "sample_quarantine", b'{"sample_id":"old"}'),
                                 ("M21", "sample_quarantine", b'{"sample_id":"new"}')):
        name = "bound/" + stage + "_" + role + (".json" if role == "quality_report" else ".jsonl")
        members[name] = payload
        items.append({"stage":stage, "role":role, "path":name, "sha256":hashlib.sha256(payload).hexdigest()})
    current = package(tmp_path, members, {"format":"crossmodal-manifest-v1","entrypoint":"data/rows.jsonl",
        "material_handoff":{"format":"crossmodal-material-handoff-v1","items":items}})
    result = call(tmp_path, 23, current)
    assert result["status"] == 1, result
    assert result["publish_status"] == "pending"
    with zipfile.ZipFile(Path(result["outputFile"]["runDir"]) / result["package"]) as z:
        assert z.read("materials/quality_report.json") == REPORT
        assert [json.loads(row)["sample_id"] for row in z.read("train/samples.jsonl").splitlines()] == ["keep"]
