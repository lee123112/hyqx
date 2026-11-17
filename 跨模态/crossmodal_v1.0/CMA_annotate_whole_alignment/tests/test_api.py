"""三接口测试：上传、两种 isLastOperator 返回形态、任务隔离、路径安全、清理。"""
from __future__ import annotations

import io
import json
import zipfile
from pathlib import Path

import pytest

pytest.importorskip("fastapi.testclient")
from fastapi.testclient import TestClient  # noqa: E402

import app as app_module  # noqa: E402
import inference  # noqa: E402
from utils import alignment as al  # noqa: E402

SAMPLE = Path(inference.BASE) / "dataset" / "sample"


@pytest.fixture(autouse=True)
def _force_no_model_weights(tmp_path, monkeypatch):
    """本用例集按「无权重环境」编写（显式降级）；把 MODEL_ROOT 指向空目录，
    使降级断言在已挂载权重的机器上同样可复现。业务断言不变。"""
    monkeypatch.setenv("MODEL_ROOT", str(tmp_path / "_no_model_weights"))


@pytest.fixture()
def output_root(tmp_path, monkeypatch) -> Path:
    root = (tmp_path / "output").resolve()
    root.mkdir(parents=True, exist_ok=True)
    monkeypatch.setattr(app_module, "OUTPUT_ROOT", root)
    return root


@pytest.fixture()
def client(output_root) -> TestClient:
    return TestClient(app_module.app, raise_server_exceptions=False)


def _bundle(root: Path = SAMPLE) -> bytes:
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w", zipfile.ZIP_DEFLATED) as archive:
        for path in sorted(root.rglob("*")):
            if path.is_file():
                archive.write(str(path), path.relative_to(root).as_posix())
    return buffer.getvalue()


def _upload(client, unique_id, content=None):
    return client.post("/uploadFile", params={"uniqueId": unique_id},
                       files={"file": ("bundle.zip", content or _bundle(),
                                       "application/zip")}).json()


def _predict(client, payload):
    return client.post("/predict", json=payload).json()


# ------------------------------------------------------------------ 路由与信封

def test_only_three_formal_endpoints(client):
    paths = {route.path for route in app_module.app.routes}
    assert {"/uploadFile", "/predict", "/removeFile"} <= paths
    assert "/downloadFile" not in paths
    assert client.post("/downloadFile").status_code == 404


def test_health_envelope(client):
    body = client.get("/health").json()
    assert body["code"] == 0 and body["msg"] == "success"
    assert body["data"]["operator"] == "16_whole_alignment"
    assert body["data"]["stageId"] == "S3"


def test_every_response_is_enveloped(client):
    for payload in ({"uniqueId": "t1_f1"}, {"uniqueId": "../evil", "isLastOperator": False}):
        body = _predict(client, payload)
        assert set(body) == {"code", "msg", "data"}
        assert body["code"] == 1 and isinstance(body["msg"], str)


# ------------------------------------------------------------------ 上传

def test_upload_extracts_bundle_and_detects_entry(client, output_root):
    body = _upload(client, "t1_f1")
    assert body["code"] == 0
    assert body["data"]["entry"] == "demo.jsonl"
    assert body["data"]["humanEventsFile"] == "16_human_events.jsonl"
    entry = Path(body["data"]["inputFile"])
    assert entry.is_file() and entry.parent.parent == output_root / "t1_f1"
    assert (entry.parent / "media" / "cat.png").is_file()


def test_upload_rejects_bad_unique_id(client, output_root):
    body = _upload(client, "../evil")
    assert body["code"] == 1
    assert not (output_root.parent / "evil").exists()


def test_upload_rejects_corrupt_zip_and_rolls_back(client, output_root):
    body = _upload(client, "t2_f1", content=b"PK\x03\x04 not a real zip body")
    assert body["code"] == 1
    assert not (output_root / "t2_f1").exists()


def test_upload_rejects_zip_traversal_member(client, output_root):
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w") as archive:
        archive.writestr("../escaped.jsonl", "{}\n")
        archive.writestr("demo.jsonl", "{}\n")
    body = _upload(client, "t3_f1", content=buffer.getvalue())
    assert body["code"] == 1
    assert not (output_root / "escaped.jsonl").exists()
    assert not (output_root / "t3_f1").exists()


def test_upload_requires_file(client):
    body = client.post("/uploadFile", params={"uniqueId": "t1_f1"}).json()
    assert body["code"] == 1


# ------------------------------------------------------------------ predict：统一响应形态（[v4.2] 去 lineageFile）

def test_predict_middle_form_returns_same_envelope(client):
    _upload(client, "t1_f1")
    body = _predict(client, {"uniqueId": "t1_f1", "isLastOperator": False})
    assert body["code"] == 0
    # [v4.2] isLastOperator 保留严格校验但不再改变响应形态
    assert set(body["data"]) == {"operator", "status", "outputFile", "result"}
    assert body["data"]["operator"] == "16_whole_alignment"
    assert body["data"]["status"] == "degraded"
    files = body["data"]["outputFile"]
    assert files["format"] == "crossmodal-files-v1"
    assert files["entrypoint"] == "16_whole_alignment.jsonl"
    assert body["data"]["result"]["statistics"]["conservation_ok"] is True


def test_predict_accepts_previous_run_output_as_input_file(client, output_root):
    _upload(client, "t1_f1")
    first = _predict(client, {"uniqueId": "t1_f1", "isLastOperator": False})
    assert first["code"] == 0
    own = (output_root / "t1_f1" / "16_whole_alignment" / "run001"
           / "16_whole_alignment.jsonl")
    assert own.is_file()
    again = _predict(client, {"uniqueId": "t1_f1", "isLastOperator": True,
                              "inputFile": str(own)})
    assert again["code"] == 0
    assert again["data"]["result"].get("reused") is True


# ------------------------------------------------------------------ predict：阶段末形态

def test_predict_last_operator_returns_file_object_envelope(client):
    _upload(client, "t1_f1")
    body = _predict(client, {"uniqueId": "t1_f1", "isLastOperator": True})
    assert body["code"] == 0
    assert set(body["data"]) == {"operator", "status", "outputFile", "result"}
    assert "lineageFile" not in body["data"]      # [v4.2] 血缘输出已停用

    files = body["data"]["outputFile"]
    assert files["format"] == "crossmodal-files-v1"
    assert files["entrypoint"] == "16_whole_alignment.jsonl"
    encodings = {item["path"]: item["encoding"] for item in files["files"]}
    assert encodings["16_whole_alignment.jsonl"] == "utf-8"
    assert encodings["media/cat.png"] == "base64"
    assert all(".." not in item["path"] for item in files["files"])
    paths = {item["path"] for item in files["files"]}
    assert "lineage.json" not in paths and "stage_lineage.json" not in paths


# ------------------------------------------------------------------ predict：形态一（单文件流，[v4.2] 新增）

def _candidate(pair_id="pair_0001", score=0.36):
    return {"pair_id": pair_id,
            "left": {"sample_id": "s1", "record": {"image": {"path": "media/cat.png"}}},
            "right": {"sample_id": "s2", "record": {"text": "a cat"}},
            "image_text_score": {"clip_score": score, "tier": "auto_align"}}


def test_predict_form1_single_candidate_json(client):
    body = client.post(
        "/predict?detection=annotate_whole_alignment",
        files={"file": ("pair.json", json.dumps(_candidate()).encode("utf-8"),
                        "application/json")}).json()
    assert body["status"] == 1
    assert body["pair_id"] == "pair_0001"
    assert body["alignment_label"] == "aligned"
    assert body["decision_source"] == "automatic"
    assert body["score"] == 0.36
    assert body["process_status"] == "auto_aligned"


def test_predict_form1_jsonl_and_threshold_override(client):
    content = (json.dumps(_candidate()) + "\n").encode("utf-8")
    body = client.post(
        "/predict?detection=M16",
        files={"file": ("pairs.jsonl", content, "application/jsonl")},
        data={"auto_align_threshold": "0.5"}).json()
    assert body["status"] == 1
    assert body["process_status"] == "pending_manual_review"
    assert body["alignment_label"] == "uncertain"
    assert body["review_id"]


def test_predict_form1_detection_mismatch_rejected(client):
    body = client.post(
        "/predict?detection=M17",
        files={"file": ("pair.json", json.dumps(_candidate()).encode("utf-8"),
                        "application/json")}).json()
    assert body["status"] == 0


def test_predict_form1_rejects_media_upload(client):
    body = client.post(
        "/predict?detection=M16",
        files={"file": ("cat.png", b"\x89PNG\r\n\x1a\n", "image/png")}).json()
    assert body["status"] == 0
    assert "JSON" in body["msg"]


def test_predict_form1_registry_operator_id_alias(client):
    """[D2] map.json 登记表英文 operatorId 别名可寻址。"""
    body = client.post(
        "/predict?detection=whole_alignment_annotation",
        files={"file": ("pair.json", json.dumps(_candidate()).encode("utf-8"),
                        "application/json")}).json()
    assert body["status"] == 1
    assert body["pair_id"] == "pair_0001"


def test_stage_object_restores_to_identical_entry(client, tmp_path):
    from utils import transport

    _upload(client, "t1_f1")
    files = _predict(client, {"uniqueId": "t1_f1", "isLastOperator": True})["data"]["outputFile"]
    entry = transport.restore_file_object(files, tmp_path / "restored")
    lines = [json.loads(line) for line in
             entry.read_text(encoding="utf-8").splitlines() if line]
    assert len(lines) == 11
    assert lines[0]["alignment"]["whole"]["decision"] in al.DECISIONS


# ------------------------------------------------------------------ 隔离与安全

def test_two_unique_ids_are_isolated(client, output_root):
    first = _upload(client, "t1_f1")["data"]["inputFile"]
    second = _upload(client, "t2_f1")["data"]["inputFile"]
    assert Path(first).parent.parent != Path(second).parent.parent
    assert Path(first).parent.parent.parent == output_root


@pytest.mark.parametrize("bad_input", [
    "../t2/input/demo.jsonl", "..", "input/../../escape.jsonl", "   ", 123,
])
def test_predict_rejects_unsafe_input_file(client, bad_input):
    _upload(client, "t1_f1")
    body = _predict(client, {"uniqueId": "t1_f1", "isLastOperator": False,
                             "inputFile": bad_input})
    assert body["code"] == 1


def test_predict_rejects_absolute_path_outside_task(client, tmp_path):
    _upload(client, "t1_f1")
    outside = tmp_path / "outside.jsonl"
    outside.write_text("{}\n", encoding="utf-8")
    body = _predict(client, {"uniqueId": "t1_f1", "isLastOperator": False,
                             "inputFile": str(outside)})
    assert body["code"] == 1


def test_predict_reports_missing_input_file(client):
    _upload(client, "t1_f1")
    body = _predict(client, {"uniqueId": "t1_f1", "isLastOperator": False,
                             "inputFile": "input/none.jsonl"})
    assert body["code"] == 1 and "不存在" in body["msg"]


def test_predict_before_upload_is_rejected(client):
    body = _predict(client, {"uniqueId": "t_fresh", "isLastOperator": False})
    assert body["code"] == 1 and "uploadFile" in body["msg"]


@pytest.mark.parametrize("payload", [
    {},
    {"uniqueId": "t1_f1"},
    {"uniqueId": "t1_f1", "isLastOperator": "true"},
    {"uniqueId": "t1_f1", "isLastOperator": False, "config": {"weights": "/x"}},
    {"uniqueId": "t1_f1", "isLastOperator": False, "unknown": 1},
])
def test_predict_validates_payload(client, payload):
    assert _predict(client, payload)["code"] == 1


def test_predict_rejects_unsafe_unique_id(client):
    assert _predict(client, {"uniqueId": "../evil", "isLastOperator": False})["code"] == 1


# ------------------------------------------------------------------ 清理

def test_remove_file_only_clears_its_own_task(client, output_root):
    _upload(client, "t1_f1")
    _upload(client, "t2_f1")
    body = client.post("/removeFile", params={"uniqueId": "t1_f1"}).json()
    assert body["code"] == 0 and body["data"]["deleted"] is True
    assert not (output_root / "t1_f1").exists()
    assert (output_root / "t2_f1" / "input" / "demo.jsonl").is_file()
    again = client.post("/removeFile", params={"uniqueId": "t1_f1"}).json()
    assert again["code"] == 0 and again["data"]["deleted"] is False


def test_remove_file_rejects_bad_unique_id(client):
    body = client.post("/removeFile", params={"uniqueId": "../evil"}).json()
    assert body["code"] == 1
