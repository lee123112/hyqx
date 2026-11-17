"""三接口契约测试：统一信封、中间态/阶段末形态、参数校验、路径安全、无 /downloadFile。"""
from __future__ import annotations

import json
from pathlib import Path

import pytest

from tests import fixtures
from utils import temporal
from utils import transport


def _envelope(response, expected_code=0, expected_status=200):
    assert response.status_code == expected_status, response.text
    body = response.json()
    assert set(body) == {"code", "msg", "data"}, body
    assert body["code"] == expected_code
    assert isinstance(body["msg"], str) and body["msg"]
    assert isinstance(body["data"], dict)
    return body


def test_upload_single_jsonl_and_predict_middle(client, output_root):
    payload = json.dumps(fixtures.make_record("single-1"), ensure_ascii=False).encode("utf-8")
    response = client.post("/uploadFile", params={"uniqueId": "api_single"},
                           files={"file": ("demo.jsonl", payload, "application/jsonl")})
    stored = _envelope(response)["data"]
    assert stored["entrypoint"] == "demo.jsonl"
    assert stored["files"] == ["demo.jsonl"]

    predict = client.post("/predict", json={
        "uniqueId": "api_single", "isLastOperator": False, "config": {"enabled": True}})
    body = _envelope(predict)
    assert body["msg"]
    # [v4.2] 所有调用统一返回 outputFile + result（无 lineageFile）
    assert set(body["data"]) == {"operator", "status", "outputFile", "result"}
    result = body["data"]["result"]
    assert result["uniqueId"] == "api_single"
    output = output_root / "api_single" / result["outputFile"]
    assert output.is_file()
    assert result["statistics"]["passed_records"] == 1
    assert result["statistics"]["conservation_ok"] is True
    assert result["model"] is None
    assert str(output.resolve()).startswith(str(output_root.resolve()))


def test_upload_zip_and_predict_last(client):
    response = client.post("/uploadFile", params={"uniqueId": "api_zip"},
                           files={"file": ("bundle.zip", fixtures.bundle_zip(),
                                           "application/zip")})
    stored = _envelope(response)["data"]
    assert stored["entrypoint"] == "demo.jsonl"
    assert "media/tl_ok.json" in stored["files"]

    predict = client.post("/predict", json={
        "uniqueId": "api_zip", "isLastOperator": True, "config": {"enabled": True}})
    body = _envelope(predict)
    # [v4.2] 阶段末与阶段内形态一致：outputFile + result
    assert set(body["data"]) == {"operator", "status", "outputFile", "result"}
    file_object = body["data"]["outputFile"]
    assert file_object["format"] == transport.FILE_OBJECT_FORMAT
    assert file_object["entrypoint"] == temporal.MAIN_NAME_ENABLED
    paths = {item["path"] for item in file_object["files"]}
    assert temporal.MAIN_NAME_ENABLED in paths
    assert temporal.SAMPLE_QUARANTINE_NAME in paths
    assert transport.MANIFEST_NAME in paths

    result = body["data"]["result"]
    stats = result["statistics"]
    assert stats["input_records"] == 2
    assert stats["passed_records"] == 1
    assert stats["failed_records"] == 1
    assert stats["conservation_ok"] is True


def test_upload_bundle_with_manifest_media_and_sidecar(client):
    """带 manifest + 媒体子目录 + sidecar 的素材包：入口解析、随包搬运、判定正确。"""
    record = fixtures.make_record("bundle-1", timeline_file="media/tl_ok.json",
                                 media=["media/subtitles.srt"])
    members = {
        "manifest.json": json.dumps(
            {"format": "crossmodal-manifest-v1", "entrypoint": "demo.jsonl",
             "artifacts": [{"path": "demo.jsonl", "role": "main"},
                           {"path": "media/tl_ok.json", "role": "media"}]},
            ensure_ascii=False).encode("utf-8"),
        "demo.jsonl": (json.dumps(record, ensure_ascii=False) + "\n").encode("utf-8"),
        "media/tl_ok.json": fixtures.default_media()["media/tl_ok.json"],
        "media/subtitles.srt": fixtures.default_media()["media/subtitles.srt"],
    }
    response = client.post("/uploadFile", params={"uniqueId": "api_bundle"},
                           files={"file": ("bundle.zip", fixtures.zip_bytes(members),
                                           "application/zip")})
    stored = _envelope(response)["data"]
    assert stored["entrypoint"] == "demo.jsonl"
    assert stored["files"] == ["demo.jsonl", "manifest.json", "media/subtitles.srt",
                               "media/tl_ok.json"]

    result = _envelope(client.post("/predict", json={
        "uniqueId": "api_bundle", "isLastOperator": False,
        "config": {"enabled": True}}))["data"]["result"]
    stats = result["statistics"]
    assert stats["input_records"] == 1
    assert stats["passed_records"] == 1
    assert stats["media_carried"] == 2
    assert stats["conservation_ok"] is True


def test_two_tasks_isolated(client):
    for unique_id in ("api_iso_1", "api_iso_2"):
        client.post("/uploadFile", params={"uniqueId": unique_id},
                    files={"file": ("bundle.zip", fixtures.bundle_zip(), "application/zip")})
    first = _envelope(client.post("/predict", json={
        "uniqueId": "api_iso_1", "isLastOperator": False}))["data"]["result"]
    second = _envelope(client.post("/predict", json={
        "uniqueId": "api_iso_2", "isLastOperator": False}))["data"]["result"]
    assert first["uniqueId"] == "api_iso_1"
    assert second["uniqueId"] == "api_iso_2"
    assert first["runId"] and second["runId"]
    assert first["statistics"]["skipped_records"] == 2  # 默认关闭
    assert second["statistics"]["skipped_records"] == 2


def test_predict_without_input_rejected(client):
    body = _envelope(client.post("/predict", json={
        "uniqueId": "api_no_input", "isLastOperator": False}), 1, 400)
    assert "InputError" in body["msg"]


@pytest.mark.parametrize("unique_id,expected", [
    ("", 422), ("..", 422), ("a/b", 422), ("a\\b", 422), ("con", 422), ("C:\\x", 422),
])
def test_upload_invalid_unique_id(client, unique_id, expected):
    response = client.post("/uploadFile", params={"uniqueId": unique_id},
                           files={"file": ("demo.jsonl", b"{}\n", "application/jsonl")})
    _envelope(response, 1, expected)


def test_predict_invalid_unique_id(client):
    _envelope(client.post("/predict", json={"uniqueId": "../x", "isLastOperator": False}),
              1, 422)


@pytest.mark.parametrize("payload", [
    {"uniqueId": "api_bad", "isLastOperator": "yes"},
    {"uniqueId": "api_bad", "isLastOperator": True, "unknown": 1},
    {"uniqueId": "api_bad"},
    {"isLastOperator": True},
])
def test_predict_request_validation(client, payload):
    _envelope(client.post("/predict", json=payload), 1, 422)


def test_predict_config_validation(client):
    _envelope(client.post("/predict", json={
        "uniqueId": "api_cfg", "isLastOperator": False,
        "config": {"interpolation": "spline"}}), 1, 400)
    _envelope(client.post("/predict", json={
        "uniqueId": "api_cfg", "isLastOperator": False,
        "config": {"max_time_delta_sec": 9.0}}), 1, 400)
    _envelope(client.post("/predict", json={
        "uniqueId": "api_cfg", "isLastOperator": False, "config": {"nope": 1}}), 1, 400)


def test_predict_input_file_must_stay_in_task(client):
    body = _envelope(client.post("/predict", json={
        "uniqueId": "api_escape", "isLastOperator": False,
        "inputFile": "C:\\Windows\\win.ini"}), 1, 400)
    assert "InputError" in body["msg"] or "越出" in body["msg"]


def test_upload_empty_content_rejected(client):
    _envelope(client.post("/uploadFile", params={"uniqueId": "api_empty"},
                          files={"file": ("demo.jsonl", b"", "application/jsonl")}), 1, 422)


def test_upload_zip_slip_rejected(client, output_root):
    payload = fixtures.zip_bytes({"../evil.jsonl": b"{}\n"})
    body = _envelope(client.post("/uploadFile", params={"uniqueId": "api_slip"},
                                 files={"file": ("bundle.zip", payload, "application/zip")}),
                     1, 400)
    assert "非法" in body["msg"] or "逃逸" in body["msg"]
    assert not (output_root / "evil.jsonl").exists()
    assert not (output_root / "api_slip" / "evil.jsonl").exists()


def test_upload_zip_ambiguous_entrypoint_rejected(client):
    payload = fixtures.zip_bytes({"a.jsonl": b"{}\n", "b.jsonl": b"{}\n"})
    _envelope(client.post("/uploadFile", params={"uniqueId": "api_amb"},
                          files={"file": ("bundle.zip", payload, "application/zip")}), 1, 400)


def test_upload_replaces_previous_input(client, output_root):
    client.post("/uploadFile", params={"uniqueId": "api_replace"},
                files={"file": ("demo.jsonl", b'{"sample_id": "old"}\n', "application/jsonl")})
    client.post("/uploadFile", params={"uniqueId": "api_replace"},
                files={"file": ("new.jsonl", b'{"sample_id": "new"}\n', "application/jsonl")})
    input_dir = output_root / "api_replace" / "input"
    assert sorted(path.name for path in input_dir.iterdir()) == ["new.jsonl"]
    result = _envelope(client.post("/predict", json={
        "uniqueId": "api_replace", "isLastOperator": False}))["data"]["result"]
    main = fixtures.read_jsonl(output_root / "api_replace" / result["outputFile"])
    assert [record["sample_id"] for record in main] == ["new"]


def _single_file(client, uid, record, **form):
    payload = json.dumps(record, ensure_ascii=False).encode("utf-8")
    data = {key: str(value) for key, value in form.items()}
    return client.post("/predict?detection=score_spatiotemporal_consistency&uniqueId=%s" % uid,
                       data=data,
                       files={"file": ("sample.json", payload, "application/json")})


def test_predict_single_file_form_pass(client):
    """[v4.2] 形态一：单条候选对 JSON → 判定型 int status + 时序业务字段。"""
    body = _single_file(client, "form1_pass", fixtures.make_record("f1"),
                        enabled="true", interpolation_method="linear").json()
    assert body["status"] == 1, body
    assert body["decision"] == "pass"
    assert body["applicable"] is True
    assert body["pair_id"] == "f1"
    assert body["max_delta_sec"] is not None
    assert body["threshold_sec"] == 0.5
    # [v4.2] §4.15 规范枚举 linear 作为别名被接受，形态一回显 linear
    assert body["interpolation_method"] == "linear"


def test_predict_single_file_form_not_applicable(client):
    """[v4.2] 缺时间戳 → not_applicable + status=1，绝不写成「时序通过」。"""
    record = fixtures.make_record("f2", with_timeline=False, with_bindings=False)
    body = _single_file(client, "form1_na", record, enabled="true").json()
    assert body["status"] == 1
    assert body["applicable"] is False
    assert body["decision"] == "not_applicable"
    assert body["process_status"] == "not_applicable"


def test_predict_single_file_form_rejects_wrong_detection(client):
    payload = json.dumps(fixtures.make_record("f3"), ensure_ascii=False).encode("utf-8")
    body = client.post("/predict?detection=m99",
                       files={"file": ("sample.json", payload, "application/json")}).json()
    assert body["status"] == 0
    assert "detection" in body["msg"]


def test_predict_single_file_form_requires_file(client):
    body = client.post("/predict?detection=score_spatiotemporal_consistency",
                       files={"other": ("note.txt", b"x", "text/plain")}).json()
    assert body["status"] == 0
    assert "file" in body["msg"]


def test_no_download_file_endpoint(client):
    response = client.post("/downloadFile", params={"uniqueId": "api_zip"})
    assert response.status_code == 404
    body = response.json()
    assert body["code"] == 1
    assert "downloadFile" in body["msg"] or "Not Found" in body["msg"]


def test_remove_file_is_idempotent(client, output_root):
    client.post("/uploadFile", params={"uniqueId": "api_remove"},
                files={"file": ("bundle.zip", fixtures.bundle_zip(), "application/zip")})
    task = output_root / "api_remove"
    assert task.is_dir()
    first = _envelope(client.post("/removeFile", params={"uniqueId": "api_remove"}))
    assert first["data"]["removed"] is True
    assert not task.exists()
    second = _envelope(client.post("/removeFile", params={"uniqueId": "api_remove"}))
    assert second["data"]["removed"] is False


def test_remove_file_invalid_unique_id(client):
    _envelope(client.post("/removeFile", params={"uniqueId": "../x"}), 1, 422)


def test_predict_last_operator_requires_output(client):
    """[v4.2] 阶段末与阶段内都给出 outputFile 文件内容对象；形态不再随 isLastOperator 改变。"""
    client.post("/uploadFile", params={"uniqueId": "api_shape"},
                files={"file": ("bundle.zip", fixtures.bundle_zip(), "application/zip")})
    middle = _envelope(client.post("/predict", json={
        "uniqueId": "api_shape", "isLastOperator": False, "config": {"enabled": True}}))
    last = _envelope(client.post("/predict", json={
        "uniqueId": "api_shape", "isLastOperator": True, "config": {"enabled": True}}))
    for body in (middle, last):
        assert set(body["data"]) == {"operator", "status", "outputFile", "result"}
    object_ = last["data"]["outputFile"]
    assert object_["format"] == transport.FILE_OBJECT_FORMAT
    main = [item for item in object_["files"]
            if item["path"] == temporal.MAIN_NAME_ENABLED][0]
    for line in main["content"].splitlines():
        record = json.loads(line)
        assert record["temporal_alignment"]["passed"] is not None or \
            record["temporal_alignment"]["process_status"] == "not_applicable"


def test_health_reports_identity(client):
    """[D1] /health 存活探针：返回算子标识/状态。"""
    body = client.get("/health").json()
    assert body["code"] == 0
    assert body["data"]["operator"] == "temporal_consistency"
    assert body["data"]["formalOperatorNumber"] == 15
    assert body["data"]["stageId"] == "S3"
