"""HTTP 三接口契约测试（``app.py``）。

覆盖：统一信封、阶段首/中间/末三种请求与返回形态、阶段末文件对象可恢复、
任务隔离、非法 uniqueId、跨任务 inputFile、严格布尔 isLastOperator、
``/downloadFile`` 不存在、``/removeFile`` 幂等与请求级 config 白名单。
"""
from __future__ import annotations

import io
import json
import sys
import zipfile
from pathlib import Path

import pytest
from conftest import (DEFAULT_CONFIG_YAML, make_record, rec_19, rec_20, rec_21,
                      rec_audio, rec_image, rec_license, rec_text, write_jsonl)

BASE = Path(__file__).resolve().parents[1]
if str(BASE) not in sys.path:
    sys.path.insert(0, str(BASE))

fastapi_testclient = pytest.importorskip("fastapi.testclient")
TestClient = fastapi_testclient.TestClient


@pytest.fixture()
def client(tmp_path, monkeypatch):
    root = tmp_path / "api_output"
    root.mkdir(parents=True, exist_ok=True)
    config = tmp_path / "api_config.yaml"
    config.write_text(DEFAULT_CONFIG_YAML, encoding="utf-8")
    monkeypatch.setenv("OUTPUT_DIR", str(root))
    monkeypatch.setenv("OPERATOR_CONFIG", str(config))
    import app as app_module

    return TestClient(app_module.app), root


def sample_records():
    return [
        make_record("api-0001", rec_19(), rec_20(), rec_license(),
                    rec_text("一只猫"), rec_image("media/cat.png")),
        make_record("api-0002", rec_20(), rec_license(), rec_text("狗在跑"),
                    rec_image("media/dog.png")),
        make_record("api-0003", rec_20(), rec_21(), rec_license(), rec_text("隔离"),
                    rec_image("media/iso.png")),
    ]


def sample_zip_bytes() -> bytes:
    payload = "\n".join(json.dumps(item, ensure_ascii=False)
                        for item in sample_records()) + "\n"
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w", zipfile.ZIP_DEFLATED) as bundle:
        bundle.writestr("19_candidate_pool.jsonl", payload)
        bundle.writestr("media/cat.png", b"\x89PNG\r\n\x1a\n\x00binary")
        bundle.writestr("manifest.json", json.dumps({
            "format": "crossmodal-manifest-v1",
            "entrypoint": "19_candidate_pool.jsonl",
            "artifacts": [{"path": "19_candidate_pool.jsonl", "role": "records"},
                          {"path": "media/cat.png", "role": "media"}]},
            ensure_ascii=False))
    return buffer.getvalue()


def test_form1_zip_returns_chainable_reference(client):
    http, _root = client
    # Valid empty input exercises packaging without invented model/quality evidence.
    body = http.post('/predict?detection=M22&uniqueId=chain_zip',
        files={'file': ('input.zip', _bundle_bytes('samples.jsonl', ''), 'application/zip')}).json()
    assert body['status'] == 1
    assert body['outputFile']['mode'] == 'path-reference'
    assert Path(body['outputFile']['zip']).is_file()


def upload(client, unique_id: str):
    return client.post("/uploadFile", params={"uniqueId": unique_id},
                       files={"file": ("bundle.zip", sample_zip_bytes(),
                                       "application/zip")})


def _bundle_bytes(entry_name: str, content: str) -> bytes:
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w", zipfile.ZIP_DEFLATED) as bundle:
        bundle.writestr(entry_name, content)
        bundle.writestr("manifest.json", json.dumps({
            "format": "crossmodal-manifest-v1", "entrypoint": entry_name,
            "artifacts": [{"path": entry_name, "role": "records"}]},
            ensure_ascii=False))
    return buffer.getvalue()


# --------------------------------------------------------------------------- #
# 上传
# --------------------------------------------------------------------------- #

def test_upload_returns_envelope_and_entrypoint(client):
    client_obj, _root = client
    response = upload(client_obj, "task_a")
    assert response.status_code == 200
    body = response.json()
    assert body["code"] == 0
    assert body["data"]["entrypoint"] == "19_candidate_pool.jsonl"
    assert "media/cat.png" in body["data"]["files"]


def test_upload_rejects_invalid_unique_id(client):
    client_obj, _root = client
    for bad in ("../escape", "a/b", "CON", ""):
        response = client_obj.post("/uploadFile", params={"uniqueId": bad},
                                   files={"file": ("b.zip", sample_zip_bytes(),
                                                   "application/zip")})
        assert response.status_code == 422
        assert response.json()["code"] == 1


def test_upload_rejects_non_zip_and_empty_body(client):
    client_obj, _root = client
    assert client_obj.post("/uploadFile", params={"uniqueId": "task_a"},
                           files={"file": ("b.zip", b"not a zip",
                                           "application/zip")}).status_code == 422
    assert client_obj.post("/uploadFile", params={"uniqueId": "task_a"},
                           files={"file": ("b.zip", b"",
                                           "application/zip")}).status_code == 422


def test_upload_rejects_zip_slip(client):
    client_obj, root = client
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w") as bundle:
        bundle.writestr("a.jsonl", "{}\n")
        bundle.writestr("../escape.jsonl", "{}\n")
    response = client_obj.post("/uploadFile", params={"uniqueId": "task_slip"},
                               files={"file": ("b.zip", buffer.getvalue(),
                                               "application/zip")})
    assert response.status_code == 422
    assert response.json()["code"] == 1
    assert not (root / "escape.jsonl").exists()


# --------------------------------------------------------------------------- #
# 阶段中间 / 阶段末响应形态
# --------------------------------------------------------------------------- #

def test_intermediate_response_returns_unified_envelope(client):
    # [v4.2] 响应统一：不再区分阶段末/非阶段末，lineageFile 已去除
    client_obj, _root = client
    upload(client_obj, "task_a")
    response = client_obj.post("/predict", json={"uniqueId": "task_a",
                                                 "isLastOperator": False})
    assert response.status_code == 200
    body = response.json()
    assert body["code"] == 0
    assert set(body["data"]) == {"operator", "status", "outputFile", "result"}
    assert body["data"]["operator"] == "generate_crossmodal_tokens"
    assert "lineageFile" not in body["data"]
    node = body["data"]["result"]
    assert node["uniqueId"] == "task_a"
    assert node["outputFile"]
    assert node["statistics"]["generated_records"] == 2
    assert node["statistics"]["isolated_skipped_records"] == 1


def test_last_operator_returns_file_object_without_lineage(client):
    client_obj, _root = client
    upload(client_obj, "task_a")
    client_obj.post("/predict", json={"uniqueId": "task_a", "isLastOperator": False})
    response = client_obj.post("/predict", json={"uniqueId": "task_a",
                                                 "isLastOperator": True})
    assert response.status_code == 200
    body = response.json()
    assert set(body["data"]) == {"operator", "status", "outputFile", "result"}
    file_object = body["data"]["outputFile"]
    assert file_object["format"] == "crossmodal-files-v1"
    assert file_object["entrypoint"] == "samples.jsonl"
    paths = [item["path"] for item in file_object["files"]]
    assert "samples.jsonl" in paths
    assert "samples_image_text.jsonl" in paths
    assert "22_tokens.jsonl" in paths
    assert "22_isolated_skipped.jsonl" in paths
    assert "manifest.json" in paths


def test_stage_file_object_can_be_restored_and_reuploaded(client, tmp_path):
    client_obj, _root = client
    upload(client_obj, "task_a")
    client_obj.post("/predict", json={"uniqueId": "task_a", "isLastOperator": False})
    body = client_obj.post("/predict", json={"uniqueId": "task_a",
                                             "isLastOperator": True}).json()
    from utils import transport

    dest = tmp_path / "restored"
    entry = transport.restore_file_object(body["data"]["outputFile"], dest)
    assert entry.name == "samples.jsonl"
    lines = [line for line in entry.read_text(encoding="utf-8").splitlines()
             if line.strip()]
    assert len(lines) == 2

    response = client_obj.post(
        "/uploadFile", params={"uniqueId": "task_next"},
        files={"file": ("bundle.zip", _bundle_bytes(
            "samples.jsonl", entry.read_text(encoding="utf-8")),
            "application/zip")})
    assert response.status_code == 200
    again = client_obj.post("/predict", json={"uniqueId": "task_next",
                                              "isLastOperator": False}).json()
    assert again["code"] == 0
    assert again["data"]["result"]["statistics"]["input_records"] == 2


def test_other_task_output_path_cannot_be_reused_as_input(client):
    client_obj, root = client
    upload(client_obj, "task_a")
    upload(client_obj, "task_b")
    first = client_obj.post("/predict", json={"uniqueId": "task_a",
                                              "isLastOperator": False}).json()
    relative = Path(first["data"]["result"]["outputFile"]).relative_to(root)
    response = client_obj.post("/predict", json={
        "uniqueId": "task_b", "isLastOperator": False,
        "inputFile": str(relative).replace("\\", "/")})
    assert response.status_code == 400
    assert response.json()["code"] == 1


# --------------------------------------------------------------------------- #
# 安全与参数校验
# --------------------------------------------------------------------------- #

def test_predict_requires_strict_boolean_is_last_operator(client):
    client_obj, _root = client
    upload(client_obj, "task_a")
    for bad in ("false", "true", 1, 0, None):
        response = client_obj.post("/predict", json={"uniqueId": "task_a",
                                                     "isLastOperator": bad})
        assert response.status_code == 422, bad
        assert response.json()["code"] == 1


def test_predict_rejects_cross_task_input_file(client, tmp_path):
    client_obj, _root = client
    upload(client_obj, "task_a")
    upload(client_obj, "task_b")
    outside = write_jsonl(tmp_path / "outside.jsonl",
                          [make_record("x", rec_20(), rec_license(),
                                       rec_text("y"), rec_image())])
    response = client_obj.post("/predict", json={
        "uniqueId": "task_a", "isLastOperator": False, "inputFile": str(outside)})
    assert response.status_code == 400
    assert response.json()["code"] == 1


def test_predict_rejects_traversal_input_file(client):
    client_obj, _root = client
    upload(client_obj, "task_a")
    response = client_obj.post("/predict", json={
        "uniqueId": "task_a", "isLastOperator": False,
        "inputFile": "../../../etc/passwd"})
    assert response.status_code == 400
    assert response.json()["code"] == 1


def test_predict_before_upload_fails(client):
    client_obj, _root = client
    response = client_obj.post("/predict", json={"uniqueId": "nothing_f1",
                                                 "isLastOperator": False})
    assert response.status_code == 400
    assert response.json()["code"] == 1


def test_predict_rejects_invalid_unique_id(client):
    client_obj, _root = client
    response = client_obj.post("/predict", json={"uniqueId": "../escape",
                                                 "isLastOperator": False})
    assert response.status_code == 422
    assert response.json()["code"] == 1


def test_predict_rejects_unknown_config_keys(client):
    client_obj, _root = client
    upload(client_obj, "task_a")
    response = client_obj.post("/predict", json={
        "uniqueId": "task_a", "isLastOperator": False,
        "overrides": {"nope": {"weights": "/tmp/x.pt"}}})
    assert response.status_code == 400
    assert response.json()["code"] == 1


def test_predict_rejects_disabling_tool(client):
    client_obj, _root = client
    upload(client_obj, "task_a")
    response = client_obj.post("/predict", json={
        "uniqueId": "task_a", "isLastOperator": False,
        "overrides": {"enabled": False}})
    assert response.status_code == 400
    assert response.json()["code"] == 1


def test_predict_rejects_disabling_image_text_route(client):
    client_obj, _root = client
    upload(client_obj, "task_a")
    response = client_obj.post("/predict", json={
        "uniqueId": "task_a", "isLastOperator": False,
        "overrides": {"routes": {"image_text": False}}})
    assert response.status_code == 400
    assert response.json()["code"] == 1


def test_predict_override_can_enable_audio_route(client):
    client_obj, _root = client
    upload(client_obj, "task_a")
    response = client_obj.post("/predict", json={
        "uniqueId": "task_a", "isLastOperator": False,
        "overrides": {"routes": {"audio_text": True}}})
    assert response.status_code == 200
    assert response.json()["data"]["result"]["statistics"]["routes_enabled"] \
        == ["image_text", "audio_text"]


def test_download_file_is_not_registered(client):
    client_obj, _root = client
    assert client_obj.get("/downloadFile").status_code == 404
    assert client_obj.post("/downloadFile",
                           params={"uniqueId": "a_f1"}).status_code == 404


# --------------------------------------------------------------------------- #
# 任务隔离与清理
# --------------------------------------------------------------------------- #

def test_two_unique_ids_are_isolated(client):
    client_obj, _root = client
    upload(client_obj, "task_a")
    upload(client_obj, "task_b")
    first = client_obj.post("/predict", json={"uniqueId": "task_a",
                                              "isLastOperator": False}).json()
    second = client_obj.post("/predict", json={"uniqueId": "task_b",
                                               "isLastOperator": False}).json()
    assert first["data"]["result"]["runDir"] != second["data"]["result"]["runDir"]
    assert first["data"]["result"]["uniqueId"] == "task_a"
    assert second["data"]["result"]["uniqueId"] == "task_b"


def test_remove_file_is_idempotent_and_scoped(client):
    client_obj, root = client
    upload(client_obj, "task_a")
    upload(client_obj, "task_b")
    first = client_obj.post("/removeFile", params={"uniqueId": "task_a"})
    assert first.status_code == 200
    assert first.json()["data"]["removed"] is True
    assert not (root / "task_a").exists()
    assert (root / "task_b").exists()
    second = client_obj.post("/removeFile", params={"uniqueId": "task_a"})
    assert second.json()["data"]["removed"] is False


def test_remove_file_rejects_invalid_unique_id(client):
    client_obj, _root = client
    response = client_obj.post("/removeFile", params={"uniqueId": "../escape"})
    assert response.status_code == 422
    assert response.json()["code"] == 1


def test_health_reports_no_download_endpoint_and_no_model(client):
    client_obj, _root = client
    body = client_obj.get("/health").json()
    assert body["code"] == 0
    assert body["data"]["downloadFile"] == "not_registered"
    assert body["data"]["formal_operator_number"] == 22
    assert body["data"]["model"] is None


def test_form2_detection_operator_id_conflict_rejected(client):
    """[D3] 形态二入口：detection 与 operatorId 同时出现且归一化后不一致 → 参数错误。"""
    client_obj, _root = client
    response = client_obj.post("/predict",
                               params={"detection": "m22", "operatorId": "m23"},
                               json={"uniqueId": "x", "isLastOperator": False})
    assert response.status_code == 422
    body = response.json()
    assert body["code"] == 1 and "必须一致" in body["msg"]
