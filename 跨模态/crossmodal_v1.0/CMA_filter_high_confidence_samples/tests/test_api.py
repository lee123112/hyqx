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
from conftest import (DEFAULT_CONFIG_YAML, make_record, rec_12, rec_13, rec_14,
                      rec_15, rec_16, rec_license, write_jsonl)

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


def sample_zip_bytes() -> bytes:
    records = [
        make_record("api-0001", rec_12("auto_aligned"), rec_16("auto_aligned"),
                    rec_license("可训练"), rec_15("not_applicable", None)),
        make_record("api-0002", rec_13(True, "passed"), rec_16("manual_aligned"),
                    rec_license("可训练"), rec_15("not_applicable", None)),
        make_record("api-0003", rec_12("isolated"), rec_16("auto_aligned"),
                    rec_license("可训练"), rec_15("not_applicable", None)),
    ]
    payload = "\n".join(json.dumps(item, ensure_ascii=False) for item in records) + "\n"
    media = b"\x89PNG\r\n\x1a\n\x00binary"
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w", zipfile.ZIP_DEFLATED) as bundle:
        bundle.writestr("demo.jsonl", payload)
        bundle.writestr("media/shot.png", media)
        bundle.writestr("manifest.json", json.dumps({
            "format": "crossmodal-manifest-v1", "entrypoint": "demo.jsonl",
            "artifacts": [{"path": "demo.jsonl", "role": "main"},
                          {"path": "media/shot.png", "role": "media"}]},
            ensure_ascii=False))
    return buffer.getvalue()


def upload(client, unique_id: str):
    return client.post("/uploadFile", params={"uniqueId": unique_id},
                       files={"file": ("bundle.zip", sample_zip_bytes(),
                                       "application/zip")})


# --------------------------------------------------------------------------- #
# 上传
# --------------------------------------------------------------------------- #

def test_upload_returns_envelope_and_entrypoint(client):
    client_obj, _root = client
    response = upload(client_obj, "task_a")
    assert response.status_code == 200
    body = response.json()
    assert body["code"] == 0
    assert body["data"]["entrypoint"] == "demo.jsonl"
    assert "media/shot.png" in body["data"]["files"]


def test_upload_rejects_invalid_unique_id(client):
    client_obj, _root = client
    for bad in ("../escape", "a/b", "CON", ""):
        response = client_obj.post("/uploadFile", params={"uniqueId": bad},
                                   files={"file": ("b.zip", sample_zip_bytes(),
                                                   "application/zip")})
        assert response.status_code == 422
        assert response.json()["code"] == 1


def test_upload_rejects_non_zip(client):
    client_obj, _root = client
    response = client_obj.post("/uploadFile", params={"uniqueId": "task_a"},
                               files={"file": ("b.zip", b"not a zip", "application/zip")})
    assert response.status_code == 422
    assert response.json()["code"] == 1


def test_upload_rejects_empty_body(client):
    client_obj, _root = client
    response = client_obj.post("/uploadFile", params={"uniqueId": "task_a"},
                               files={"file": ("b.zip", b"", "application/zip")})
    assert response.status_code == 422
    assert response.json()["code"] == 1


def test_upload_rejects_zip_slip(client):
    client_obj, root = client
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w") as bundle:
        bundle.writestr("demo.jsonl", "{}\n")
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

def test_intermediate_response_returns_output_file_and_result(client):
    """[v4.2] 中间态与阶段末形态一致：outputFile + result，不再回传 lineageFile。"""
    client_obj, _root = client
    upload(client_obj, "task_a")
    response = client_obj.post("/predict", json={"uniqueId": "task_a",
                                                 "isLastOperator": False})
    assert response.status_code == 200
    body = response.json()
    assert body["code"] == 0
    assert set(body["data"]) == {"operator", "status", "outputFile", "result"}
    result = body["data"]["result"]
    assert result["uniqueId"] == "task_a"
    assert result["statistics"]["admitted_records"] == 2
    assert result["statistics"]["sample_isolated"] == 1
    assert result["outputFile"].endswith(".jsonl")


def test_last_operator_returns_file_object_and_result(client):
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
    assert file_object["entrypoint"] == "19_candidate_pool.jsonl"
    paths = [item["path"] for item in file_object["files"]]
    assert "19_candidate_pool.jsonl" in paths
    assert "19_sample_quarantine.jsonl" in paths
    assert "19_pending.jsonl" in paths
    assert "manifest.json" in paths
    assert body["data"]["result"]["statistics"]["admitted_records"] == 2


def _bundle_bytes(entry_name: str, content: str) -> bytes:
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w", zipfile.ZIP_DEFLATED) as bundle:
        bundle.writestr(entry_name, content)
        bundle.writestr("manifest.json", json.dumps({
            "format": "crossmodal-manifest-v1", "entrypoint": entry_name,
            "artifacts": [{"path": entry_name, "role": "main"}]}, ensure_ascii=False))
    return buffer.getvalue()


def test_stage_file_object_can_be_restored_and_reuploaded_as_next_input(client, tmp_path):
    """阶段末对象 → 恢复到新目录 → 作为下一阶段输入重新上传（工程侧流程）。"""
    client_obj, _root = client
    upload(client_obj, "task_a")
    client_obj.post("/predict", json={"uniqueId": "task_a", "isLastOperator": False})
    body = client_obj.post("/predict", json={"uniqueId": "task_a",
                                             "isLastOperator": True}).json()
    from utils import transport

    dest = tmp_path / "restored"
    entry = transport.restore_file_object(body["data"]["outputFile"], dest)
    assert entry.name == "19_candidate_pool.jsonl"
    lines = [line for line in entry.read_text(encoding="utf-8").splitlines()
             if line.strip()]
    assert len(lines) == 2

    response = client_obj.post(
        "/uploadFile", params={"uniqueId": "task_next"},
        files={"file": ("bundle.zip", _bundle_bytes(
            "19_candidate_pool.jsonl", entry.read_text(encoding="utf-8")),
            "application/zip")})
    assert response.status_code == 200
    again = client_obj.post("/predict", json={"uniqueId": "task_next",
                                              "isLastOperator": False}).json()
    assert again["code"] == 0
    assert again["data"]["result"]["statistics"]["input_records"] == 2


def test_other_task_output_path_cannot_be_reused_as_input(client, root=None):
    """阶段内 inputFile 只能在当前任务目录内解析；跨任务路径必须拒绝。"""
    client_obj, root = client
    upload(client_obj, "task_a")
    upload(client_obj, "task_b")
    first = client_obj.post("/predict", json={"uniqueId": "task_a",
                                              "isLastOperator": False}).json()
    # [v4.2] result.outputFile 是任务内相对路径，可直接作为下一阶段 inputFile 试探
    relative = first["data"]["result"]["outputFile"]
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
    outside = tmp_path / "outside.jsonl"
    write_jsonl(outside, [make_record("x", rec_12("auto_aligned"))])
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
        "overrides": {"clip": {"weights": "/tmp/x.pt"}}})
    assert response.status_code == 400
    assert response.json()["code"] == 1


def test_predict_rejects_disabling_tool(client):
    client_obj, _root = client
    upload(client_obj, "task_a")
    response = client_obj.post("/predict", json={
        "uniqueId": "task_a", "isLastOperator": False, "overrides": {"enabled": False}})
    assert response.status_code == 400
    assert response.json()["code"] == 1


def test_predict_rejects_narrowing_required_alignment(client):
    """表1 #19 固定口径：请求把整体对齐收窄为 auto → 422，四项不得去掉任何一项。"""
    client_obj, _root = client
    upload(client_obj, "task_a")
    response = client_obj.post("/predict", json={
        "uniqueId": "task_a", "isLastOperator": False,
        "overrides": {"required_alignment": "auto"}})
    assert response.status_code == 422
    body = response.json()
    assert body["code"] == 1
    assert body["data"]["errorType"] == "FrozenOverride"
    assert "固定口径" in body["msg"]
    assert "required_alignment" in body["msg"]
    assert "auto|human" in body["msg"]


def test_predict_rejects_disabling_temporal_gate(client):
    """表1 #19 固定口径：require_temporal_if_present=false → 422（时序必过项不可关）。"""
    client_obj, _root = client
    upload(client_obj, "task_a")
    response = client_obj.post("/predict", json={
        "uniqueId": "task_a", "isLastOperator": False,
        "overrides": {"require_temporal_if_present": False}})
    assert response.status_code == 422
    body = response.json()
    assert body["code"] == 1
    assert body["data"]["errorType"] == "FrozenOverride"
    assert "固定口径" in body["msg"]
    assert "require_temporal_if_present" in body["msg"]


def test_predict_rejects_frozen_override_before_upload(client):
    """固定口径校验先于任务状态：未上传输入也返回 422，不被 400 抢先掩盖。"""
    client_obj, _root = client
    response = client_obj.post("/predict", json={
        "uniqueId": "nothing_frozen", "isLastOperator": False,
        "overrides": {"require_temporal_if_present": False}})
    assert response.status_code == 422
    assert response.json()["data"]["errorType"] == "FrozenOverride"


def test_predict_rejects_unknown_override_before_upload(client):
    """非白名单键仍是 400（ConfigError），与固定口径的 422 区分开。"""
    client_obj, _root = client
    response = client_obj.post("/predict", json={
        "uniqueId": "nothing_cfg", "isLastOperator": False,
        "overrides": {"clip": {"model": "x"}}})
    assert response.status_code == 400
    assert response.json()["code"] == 1


def _single_file(client, uid, record, **form):
    payload = json.dumps([record], ensure_ascii=False).encode("utf-8")
    data = {key: str(value) for key, value in form.items()}
    return client.post("/predict?detection=filter_high_confidence_samples&uniqueId=%s" % uid,
                       data=data,
                       files={"file": ("sample.json", payload, "application/json")})


def _admitted_record(sample_id="api-f1"):
    return make_record(sample_id, rec_12("auto_aligned"), rec_16("auto_aligned"),
                       rec_license("可训练"), rec_15("not_applicable", None))


def test_predict_single_file_form_admitted(client):
    """[v4.2] 形态一：判定型 status=1 入池 + pool_status/pool/route/reason_code。"""
    client_obj, _root = client
    body = _single_file(client_obj, "form1_ok", _admitted_record()).json()
    assert body["status"] == 1, body
    assert body["process_status"] == "success"
    assert body["pool_status"] == "accepted"
    assert body["pool"] == "high_confidence"
    assert body["sample_id"] == "api-f1"
    assert body["reason_code"] == "all_gates_passed"


def test_predict_single_file_form_rejected(client):
    """[v4.2] 形态一：不入池 status=0 + pool_status=rejected（进隔离台账）。"""
    client_obj, _root = client
    record = make_record("api-f2", rec_12("isolated"), rec_16("auto_aligned"),
                         rec_license("可训练"), rec_15("not_applicable", None))
    body = _single_file(client_obj, "form1_rej", record).json()
    assert body["status"] == 0
    assert body["pool_status"] == "rejected"
    assert body["pool"] is None
    assert body["sample_id"] == "api-f2"


def test_predict_single_file_form_pending(client):
    """[v4.2] 形态一：待人工/结论未定 → status=1 + 对应 process_status，绝不隔离。"""
    client_obj, _root = client
    record = make_record("api-f3", rec_12("auto_aligned"), rec_16("auto_aligned"),
                         rec_15("not_applicable", None))
    body = _single_file(client_obj, "form1_pend", record).json()
    assert body["status"] == 1, body
    assert body["pool_status"] == "pending"
    assert body["pool"] is None
    assert body["process_status"] == "pending_review"


def test_predict_single_file_form_rejects_narrowed_alignment(client):
    """形态一表单携带 required_alignment → 422（值不参与解析，绝不静默收窄）。"""
    client_obj, _root = client
    response = _single_file(client_obj, "form1_frozen_align", _admitted_record(),
                            required_alignment="auto")
    assert response.status_code == 422
    body = response.json()
    assert body["status"] == 0
    assert body["errorType"] == "FrozenOverride"
    assert "固定口径" in body["msg"]
    assert "required_alignment" in body["msg"]


def test_predict_single_file_form_rejects_disabled_temporal_gate(client):
    """形态一表单携带 require_temporal_if_present=false → 422。"""
    client_obj, _root = client
    response = _single_file(client_obj, "form1_frozen_temporal", _admitted_record(),
                            require_temporal_if_present="false")
    assert response.status_code == 422
    body = response.json()
    assert body["status"] == 0
    assert body["errorType"] == "FrozenOverride"
    assert "require_temporal_if_present" in body["msg"]


def test_predict_single_file_form_rejects_wrong_detection(client):
    client_obj, _root = client
    body = client_obj.post("/predict?detection=m99",
                       files={"file": ("sample.json", b"[]", "application/json")}).json()
    assert body["status"] == 0
    assert "detection" in body["msg"]


def test_predict_single_file_form_requires_file(client):
    client_obj, _root = client
    body = client_obj.post("/predict?detection=m19",
                       files={"other": ("note.txt", b"x", "text/plain")}).json()
    assert body["status"] == 0
    assert "file" in body["msg"]


def test_download_file_is_not_registered(client):
    client_obj, _root = client
    assert client_obj.get("/downloadFile").status_code == 404
    assert client_obj.post("/downloadFile", params={"uniqueId": "a_f1"}).status_code == 404


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
    assert first["data"]["result"]["uniqueId"] == "task_a"
    assert second["data"]["result"]["uniqueId"] == "task_b"
    assert first["data"]["result"]["uniqueId"] != second["data"]["result"]["uniqueId"]


def test_remove_file_is_idempotent_and_scoped(client):
    client_obj, root = client
    upload(client_obj, "task_a")
    upload(client_obj, "task_b")
    first = client_obj.post("/removeFile", params={"uniqueId": "task_a"})
    assert first.status_code == 200
    assert first.json()["code"] == 0
    assert first.json()["data"]["removed"] is True
    assert not (root / "task_a").exists()
    assert (root / "task_b").exists()

    second = client_obj.post("/removeFile", params={"uniqueId": "task_a"})
    assert second.status_code == 200
    assert second.json()["code"] == 0
    assert second.json()["data"]["removed"] is False


def test_remove_file_rejects_invalid_unique_id(client):
    client_obj, _root = client
    response = client_obj.post("/removeFile", params={"uniqueId": "../escape"})
    assert response.status_code == 422
    assert response.json()["code"] == 1


def test_health_reports_no_download_endpoint(client):
    client_obj, _root = client
    body = client_obj.get("/health").json()
    assert body["code"] == 0
    assert body["data"]["downloadFile"] == "not_registered"
    assert body["data"]["formal_operator_number"] == 19
    assert body["data"]["rule_based"] is True
