"""The incoming ZIP can contain bound and independently produced quarantine ledgers."""
import hashlib
import io
import json
from pathlib import Path
from types import SimpleNamespace
import zipfile

import pytest
from fastapi.testclient import TestClient

import app
from conftest import token_record
from utils import delivery


@pytest.mark.parametrize("entry", ["rows.jsonl", "nested/rows.jsonl"])
def test_bound_and_local_quarantine_both_exclude_records(tmp_path, monkeypatch, entry):
    monkeypatch.setenv("OUTPUT_DIR", str(tmp_path / "output"))
    bound = b'{"sample_id":"bound-only"}\n'
    binding = {"stage": "M21", "role": "sample_quarantine", "path": "evidence/samples.jsonl",
               "sha256": hashlib.sha256(bound).hexdigest()}
    members = {
        entry: "\n".join(json.dumps(token_record(s)) for s in
                         ("bound-only", "local-only", "keep")),
        binding["path"]: bound,
        "materials/isolation_list.jsonl": '{"sample_id":"local-only"}\n',
        "nested/isolation_list.jsonl": "",
        "manifest.json": json.dumps({"format": "crossmodal-manifest-v1", "entrypoint": entry,
                                     "material_handoff": {"format": "crossmodal-material-handoff-v1",
                                                          "items": [binding]}}),
    }
    stream = io.BytesIO()
    with zipfile.ZipFile(stream, "w") as z:
        for name, contents in members.items():
            z.writestr(name, contents)
    with TestClient(app.app) as client:
        body = client.post("/predict?detection=M23&uniqueId=union_file",
                           files={"file": ("input.zip", stream.getvalue(), "application/zip")}).json()
    assert body["status"] == 1, body
    assert body["publish_status"] == "pending"
    with zipfile.ZipFile(Path(body["outputFile"]["runDir"]) / body["package"]) as z:
        train = [json.loads(line)["sample_id"] for line in z.read("train/samples.jsonl").splitlines()]
        isolated = {json.loads(line)["sample_id"] for line in
                    z.read("materials/isolation_list.jsonl").splitlines()}
    assert train == ["keep"]
    assert isolated == {"bound-only", "local-only"}


@pytest.mark.parametrize("configured", ["bound.jsonl", "settings.jsonl"])
def test_explicit_isolation_is_included_once_alongside_local_and_bound(tmp_path, configured):
    for name, sid in (("bound.jsonl", "A"), ("isolation_list.jsonl", "B"), ("settings.jsonl", "C")):
        (tmp_path / name).write_text(json.dumps({"sample_id": sid}) + "\n", encoding="utf-8")
    descriptor = {"stage": "M20", "role": "origin_quarantine", "path": "bound.jsonl",
                  "sha256": hashlib.sha256((tmp_path / "bound.jsonl").read_bytes()).hexdigest()}
    (tmp_path / "manifest.json").write_text(json.dumps({"material_handoff": {
        "format": "crossmodal-material-handoff-v1", "items": [descriptor]}}), encoding="utf-8")
    entry = tmp_path / "rows.jsonl"
    entry.touch()
    config = SimpleNamespace(materials={"isolation_list": configured})
    role = delivery.inspect_materials(tmp_path, config=config, entry_path=entry)["roles"]["isolation_list"]
    expected = ["A", "B", "C"] if configured == "settings.jsonl" else ["A", "B"]
    assert role["sample_ids"] == expected
    assert role["entries"] == len(expected)
