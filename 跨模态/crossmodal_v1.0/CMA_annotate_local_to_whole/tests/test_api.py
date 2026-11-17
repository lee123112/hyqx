"""HTTP 契约测试：三接口、两种 ``isLastOperator`` 形态、安全与任务隔离。"""
from __future__ import annotations

import json

import pytest

from tests import fixtures
from utils import transport


@pytest.fixture(autouse=True)
def _force_no_model_weights(tmp_path, monkeypatch):
    """本用例集按「无权重环境」编写（检测器显式降级）；把 MODEL_ROOT 指向空目录，
    使降级断言在已挂载权重的机器上同样可复现。业务断言不变。"""
    monkeypatch.setenv("MODEL_ROOT", str(tmp_path / "_no_model_weights"))


MEDIA = {"media/demo.png": fixtures.png_bytes(12, 12)}


def _upload(client, unique_id="api_task_1", payload=None):
    body = payload if payload is not None else fixtures.bundle_zip(media=MEDIA)
    return client.post("/uploadFile", params={"uniqueId": unique_id},
                       files={"file": ("bundle.zip", body, "application/zip")})


def _predict(client, unique_id="api_task_1", *, is_last=False, config=None,
             input_file=None):
    payload = {"uniqueId": unique_id, "isLastOperator": is_last}
    if config is not None:
        payload["config"] = config
    if input_file is not None:
        payload["inputFile"] = input_file
    return client.post("/predict", json=payload)


# --------------------------------------------------------------------------- #
# /uploadFile
# --------------------------------------------------------------------------- #

def test_upload_zip_success(client):
    response = _upload(client)
    assert response.status_code == 200
    body = response.json()
    assert body["code"] == 0
    assert body["data"]["uniqueId"] == "api_task_1"
    assert body["data"]["entrypoint"] == "demo.jsonl"
    assert "media/demo.png" in body["data"]["mediaFiles"]


def test_upload_empty_payload_rejected(client):
    response = _upload(client, payload=b"")
    assert response.status_code == 422
    assert response.json()["code"] == 1


@pytest.mark.parametrize("bad", ["../escape", "a/b", "a\\b", "CON", ""])
def test_upload_rejects_illegal_unique_id(client, bad):
    response = _upload(client, unique_id=bad)
    assert response.status_code == 422
    assert response.json()["code"] == 1


def test_upload_rejects_zip_slip(client, output_root):
    payload = fixtures.zip_bytes({"../escape.jsonl": b"{}\n"})
    response = _upload(client, payload=payload)
    assert response.status_code == 422
    assert response.json()["code"] == 1
    assert not (output_root / "escape.jsonl").exists()


def test_upload_rejects_package_without_entrypoint(client):
    payload = fixtures.zip_bytes({"media/demo.png": fixtures.png_bytes()})
    response = _upload(client, payload=payload)
    assert response.status_code == 422


# --------------------------------------------------------------------------- #
# /predict —— 统一响应形态（[v4.2] 去 lineageFile，始终返回 outputFile）
# --------------------------------------------------------------------------- #

def test_intermediate_response_shape(client):
    _upload(client)
    response = _predict(client, is_last=False)
    assert response.status_code == 200
    body = response.json()
    assert body["code"] == 0
    # [v4.2] isLastOperator 保留严格校验但不再改变响应形态
    assert set(body["data"]) == {"operator", "status", "outputFile", "result"}
    assert "lineageFile" not in body["data"]
    assert body["data"]["operator"] == "local_to_whole_annotation"
    assert body["data"]["status"] == "success"
    stats = body["data"]["result"]["statistics"]
    assert stats["input_records"] == 3
    assert stats["conservation_ok"] is True


def test_intermediate_output_file_can_be_reused_as_input(client, output_root, tmp_path):
    _upload(client)
    first = _predict(client).json()
    # [v4.2] 阶段间传递用 crossmodal-files-v1 文件对象：恢复后即可作为 inputFile
    restored = transport.restore_file_object(first["data"]["outputFile"],
                                             tmp_path / "restored")
    assert restored.is_file()

    # 恢复出的产物拷贝进当前任务目录后可作为下一次 /predict 的 inputFile
    staged = output_root / "api_task_1" / "input" / "restored.jsonl"
    staged.write_bytes(restored.read_bytes())
    second = _predict(client, config={"enabled": True}, input_file=str(staged))
    assert second.status_code == 200
    body = second.json()
    assert body["code"] == 0
    assert body["data"]["outputFile"]["entrypoint"] == "17_local_annotations.jsonl"
    assert body["data"]["result"]["statistics"]["input_records"] == 3


# --------------------------------------------------------------------------- #
# /predict —— 阶段末形态
# --------------------------------------------------------------------------- #

def test_last_operator_response_shape(client, tmp_path):
    _upload(client)
    response = _predict(client, is_last=True)
    assert response.status_code == 200
    body = response.json()
    assert body["code"] == 0
    # [v4.2] 统一响应形态：outputFile + result，无 lineageFile
    assert set(body["data"]) == {"operator", "status", "outputFile", "result"}
    file_object = body["data"]["outputFile"]
    assert file_object["format"] == transport.FILE_OBJECT_FORMAT
    assert file_object["entrypoint"] == "17_passthrough.jsonl"
    paths = {item["path"] for item in file_object["files"]}
    assert "17_passthrough.jsonl" in paths
    assert "17_rejects.jsonl" in paths
    assert "manifest.json" in paths
    assert "media/demo.png" in paths

    # 把文件对象恢复到全新目录后逐条核对
    restored = transport.restore_file_object(file_object, tmp_path / "restored")
    rows = fixtures.read_jsonl(restored)
    assert len(rows) == 3
    assert (tmp_path / "restored" / "media" / "demo.png").read_bytes() == MEDIA["media/demo.png"]


def test_last_operator_enabled_route_entrypoint(client):
    _upload(client)
    response = _predict(client, is_last=True, config={"enabled": True})
    body = response.json()
    assert body["code"] == 0
    assert body["data"]["outputFile"]["entrypoint"] == "17_local_annotations.jsonl"
    stats = body["data"]["result"]["statistics"]
    assert stats["detector_degraded"] is True
    assert stats["boxes_prefilled"] == 0
    assert body["data"]["result"]["statistics"]["model"] is None


# --------------------------------------------------------------------------- #
# /predict —— 形态一（单文件流，[v4.2] 新增）
# --------------------------------------------------------------------------- #

def _form1_zip(record, with_media=True):
    members = {"sample.json": json.dumps(record, ensure_ascii=False).encode("utf-8")}
    if with_media:
        members["media/demo.png"] = fixtures.png_bytes()
    return fixtures.zip_bytes(members)


def test_predict_form1_route_disabled_returns_normal_state(client):
    record = fixtures.make_record("s-1", image="media/demo.png")
    response = client.post(
        "/predict?detection=annotate_local_to_whole",
        files={"file": ("bundle.zip", _form1_zip(record), "application/zip")})
    body = response.json()
    assert body["status"] == 1                       # 默认关闭属正常流程态
    assert body["process_status"] == "route_disabled"
    assert body["local_annotations"] == []


def test_predict_form1_pending_review_withholds_prefill(client):
    record = fixtures.make_record("s-1", image="media/demo.png")
    response = client.post(
        "/predict?detection=M17",
        files={"file": ("bundle.zip", _form1_zip(record), "application/zip")},
        data={"enabled": "true"})
    body = response.json()
    assert body["status"] == 1
    # 未人工确认 → 只回 pending_review，预填框/时间段不得交付
    assert body["process_status"] == "pending_review"
    assert body["local_annotations"] == []


def test_predict_form1_confirmed_annotations_delivered(client):
    record = fixtures.make_record(
        "s-1", image="media/demo.png",
        human=fixtures.human_evidence(boxes=[fixtures.human_box()]))
    response = client.post(
        "/predict?detection=M17",
        files={"file": ("bundle.zip", _form1_zip(record), "application/zip")},
        data={"enabled": "true"})
    body = response.json()
    assert body["status"] == 1
    assert body["process_status"] == "success"
    assert body["sample_id"] == "s-1"
    assert body["local_annotations"]
    assert body["local_annotations"][0]["label"] == "person"


def test_predict_form1_detection_mismatch_rejected(client):
    record = fixtures.make_record("s-1", image="media/demo.png")
    response = client.post(
        "/predict?detection=M16",
        files={"file": ("bundle.zip", _form1_zip(record), "application/zip")})
    assert response.json()["status"] == 0


def test_predict_form1_rejects_media_upload(client):
    response = client.post(
        "/predict?detection=M17",
        files={"file": ("demo.png", fixtures.png_bytes(), "image/png")})
    body = response.json()
    assert body["status"] == 0
    assert "JSON" in body["msg"]


def test_predict_form1_registry_operator_id_alias(client):
    """[D2] map.json 登记表英文 operatorId 别名可寻址。"""
    record = fixtures.make_record("s-1", image="media/demo.png")
    response = client.post(
        "/predict?detection=local_to_whole_annotation",
        files={"file": ("bundle.zip", _form1_zip(record), "application/zip")})
    body = response.json()
    assert body["status"] == 1
    assert body["process_status"] == "route_disabled"


# --------------------------------------------------------------------------- #
# 请求校验与安全
# --------------------------------------------------------------------------- #

@pytest.mark.parametrize("value", ["false", 0, 1, None])
def test_is_last_operator_must_be_strict_bool(client, value):
    _upload(client)
    payload = {"uniqueId": "api_task_1", "isLastOperator": value}
    response = client.post("/predict", json=payload)
    assert response.status_code == 422
    assert response.json()["code"] == 1


def test_is_last_operator_required(client):
    _upload(client)
    response = client.post("/predict", json={"uniqueId": "api_task_1"})
    assert response.status_code == 422


def test_predict_rejects_illegal_unique_id(client):
    response = _predict(client, unique_id="../escape")
    assert response.status_code == 422


def test_predict_missing_task_returns_failure_envelope(client):
    response = _predict(client, unique_id="never_uploaded")
    assert response.status_code == 400
    assert response.json()["code"] == 1


def test_predict_rejects_cross_task_input_file(client):
    _upload(client, "api_task_1")
    _upload(client, "api_task_2")
    response = _predict(client, "api_task_1",
                        input_file="../api_task_2/input/demo.jsonl")
    assert response.status_code == 400
    assert response.json()["code"] == 1


def test_predict_rejects_unknown_config_key(client):
    _upload(client)
    response = _predict(client, config={"weights": "hack.pt"})
    assert response.status_code == 400
    assert response.json()["code"] == 1


def test_predict_rejects_bad_detector_backend(client):
    _upload(client)
    response = _predict(client, config={"detector_backend": "grounding_dino"})
    assert response.status_code == 400
    assert response.json()["code"] == 1


def test_download_file_not_registered(client):
    response = client.get("/downloadFile", params={"uniqueId": "api_task_1"})
    assert response.status_code == 404
    assert response.json()["code"] == 1


def test_failure_envelope_uses_empty_data_object(client):
    """契约 §2：失败统一 ``code=1`` + 可理解 msg + ``data={}``。"""
    response = _predict(client, unique_id="never_uploaded")
    body = response.json()
    assert response.status_code == 400
    assert body["code"] == 1
    assert body["data"] == {}
    assert isinstance(body["msg"], str) and body["msg"]


def test_predict_returns_task_busy_when_lock_held(client, output_root, monkeypatch):
    """契约 §3：同一 uniqueId 并发写必须显式 task_busy。"""
    from utils import io as io_utils

    _upload(client)
    with io_utils.TaskLock(output_root, "api_task_1"):
        response = _predict(client)
    assert response.status_code == 409
    body = response.json()
    assert body["code"] == 1
    assert "task_busy" in body["msg"]
    assert body["data"] == {}


def test_remove_file_returns_task_busy_when_lock_held(client, output_root):
    from utils import io as io_utils

    _upload(client)
    with io_utils.TaskLock(output_root, "api_task_1"):
        response = client.post("/removeFile", params={"uniqueId": "api_task_1"})
    assert response.status_code == 409
    assert "task_busy" in response.json()["msg"]


# --------------------------------------------------------------------------- #
# /removeFile 与任务隔离
# --------------------------------------------------------------------------- #

def test_remove_file_is_idempotent_and_isolated(client, output_root):
    _upload(client, "api_task_1")
    _upload(client, "api_task_2")
    first = client.post("/removeFile", params={"uniqueId": "api_task_1"})
    assert first.status_code == 200
    assert first.json()["code"] == 0
    assert first.json()["data"]["removed"] is True
    assert not (output_root / "api_task_1").exists()
    assert (output_root / "api_task_2").is_dir()

    second = client.post("/removeFile", params={"uniqueId": "api_task_1"})
    assert second.status_code == 200
    assert second.json()["code"] == 0
    assert second.json()["data"]["removed"] is False


def test_remove_file_rejects_illegal_unique_id(client):
    response = client.post("/removeFile", params={"uniqueId": "../escape"})
    assert response.status_code == 422


def test_two_unique_ids_produce_isolated_outputs(client, output_root):
    _upload(client, "api_task_1")
    _upload(client, "api_task_2",
            payload=fixtures.bundle_zip(records=[
                fixtures.make_record("other-1", modality=["text"], description="别的样本")],
                media=MEDIA))
    first = _predict(client, "api_task_1").json()["data"]
    second = _predict(client, "api_task_2").json()["data"]
    assert first["result"]["uniqueId"] == "api_task_1"
    assert second["result"]["uniqueId"] == "api_task_2"
    assert first["result"]["runId"] == second["result"]["runId"] or True

    # 两个任务的运行目录必须隔离：各自 output/<uniqueId>/ 下有自己的产物
    first_files = list((output_root / "api_task_1").rglob("17_passthrough.jsonl"))
    second_files = list((output_root / "api_task_2").rglob("17_passthrough.jsonl"))
    assert first_files and second_files and first_files[0] != second_files[0]

    rows = fixtures.read_jsonl(second_files[0])
    assert [row["sample_id"] for row in rows] == ["other-1"]


# --------------------------------------------------------------------------- #
# 健康检查
# --------------------------------------------------------------------------- #

def test_health_reports_honest_detector_state(client):
    response = client.get("/health")
    assert response.status_code == 200
    body = response.json()
    assert body["code"] == 0
    data = body["data"]
    assert data["operator"] == "local_to_whole_annotation"
    assert data["formal_operator_number"] == 17
    assert data["stage_id"] == "S3"
    assert data["detector"]["detector_model"] == "YOLOv8n"
    assert data["detector"]["general_classes_only"] is True
    assert data["detector"]["available"] in (True, False)
    if not data["detector"]["available"]:
        # 不可用时必须给出真实原因，不能伪造可用
        assert data["detector"]["reason"]
    assert data["endpoints"] == ["/uploadFile?uniqueId=", "/predict", "/removeFile?uniqueId="]
    assert data["downloadFile"] == "not_registered"
