"""算子 6 三接口契约测试：信封、严格 bool、中间/阶段末 data 形状、媒体可还原性。

覆盖 DEVELOPMENT_CONTRACT §2/§3/§4 与 §6 的最小公开检查点：
- 只注册 /uploadFile、/predict、/removeFile；/downloadFile 必须 404；
- ``isLastOperator=false`` 的 data **只有** ``lineageFile``（含临时 outputFile）；
- ``isLastOperator=true`` 的 data 只有 ``outputFile``（crossmodal-files-v1）与 ``lineageFile``（完整数组）；
- 阶段末负载必须等于实际输出内容：还原后入口可解析、全部引用媒体可读。
"""
from __future__ import annotations

import json
import wave
from pathlib import Path

import cv2
import numpy as np
import pytest

from tests.conftest import (
    SAMPLE_DIR,
    make_zip,
    predict,
    read_sample_files,
    restored_path,
    sample_zip_bytes,
    upload_zip,
)

MEDIA_FILES = [
    "media/aud_sine_22050_stereo_1s.wav",
    "media/aud_sine_8000_mono_05s.wav",
    "media/img_main.jpg",
    "media/vid_main.mp4",
]


def _data(client, unique_id, is_last, **extra) -> dict:
    """[v4.2] 统一响应：is_last 不再改变 data 形状（operator/status/outputFile/result）。"""
    response = predict(client, unique_id, is_last, **extra)
    payload = response.json()
    assert response.status_code == 200, response.text
    assert payload["code"] == 0, payload
    assert set(payload["data"]) == {"operator", "status", "outputFile", "result"}, \
        payload["data"].keys()
    return payload["data"]


def _intermediate(client, unique_id, **extra) -> dict:
    return _data(client, unique_id, False, **extra)


def _final(client, unique_id, **extra):
    return _data(client, unique_id, True, **extra)


# ---------------------------------------------------------------- uploadFile

def test_upload_requires_unique_id_query(client):
    response = client.post("/uploadFile", files={"file": ("a.zip", b"PK\x03\x04", "application/zip")})
    assert response.status_code == 422
    assert response.json()["code"] == 1


@pytest.mark.parametrize("value", ["", "a/b", "a\\b", "..", "con", "x "])
def test_upload_rejects_invalid_unique_id(client, value):
    response = upload_zip(client, value, sample_zip_bytes())
    assert response.status_code == 422
    assert response.json()["code"] == 1


def test_upload_rejects_empty_payload(client, unique_id):
    response = upload_zip(client, unique_id, b"")
    assert response.status_code == 400
    assert response.json()["code"] == 1


def test_upload_echoes_resolved_entrypoint(client, unique_id):
    response = upload_zip(client, unique_id, sample_zip_bytes())
    assert response.status_code == 200
    payload = response.json()
    assert payload["code"] == 0
    assert payload["data"]["entrypoint"] == "demo.jsonl"


def test_upload_rejects_zip_path_traversal(client, unique_id):
    payload = make_zip({
        "demo.jsonl": json.dumps({"sample_id": "s1"}) + "\n",
        "../escape.txt": "boom",
    })
    response = upload_zip(client, unique_id, payload)
    assert response.status_code == 400
    assert response.json()["code"] == 1


def test_upload_accepts_nested_entrypoint(client, unique_id):
    payload = make_zip({"sub/demo.jsonl": json.dumps({"sample_id": "s1"}) + "\n"})
    response = upload_zip(client, unique_id, payload)
    assert response.status_code == 200, response.text
    predict_payload = predict(client, unique_id, True).json()
    assert predict_payload["code"] == 0
    assert predict_payload["data"]["outputFile"]["entrypoint"] == "06_audio_index.jsonl"


def test_upload_rejects_zip_without_entrypoint(client, unique_id):
    files = read_sample_files(MEDIA_FILES)
    response = upload_zip(client, unique_id, make_zip(files))
    assert response.status_code == 400
    assert response.json()["code"] == 1


def test_upload_rejects_duplicate_members(client, unique_id):
    payload = make_zip({"demo.jsonl": "{}\n", "DEMO.jsonl": "{}\n"})
    response = upload_zip(client, unique_id, payload)
    assert response.status_code == 400


def test_upload_single_jsonl_without_media_is_accepted(client, unique_id):
    body = json.dumps({"sample_id": "only_text", "text": {"content": "hi"}}) + "\n"
    response = client.post("/uploadFile", params={"uniqueId": unique_id},
                           files={"file": ("demo.jsonl", body.encode("utf-8"), "application/json")})
    assert response.status_code == 200, response.text
    assert response.json()["code"] == 0

    payload = predict(client, unique_id, True).json()
    assert payload["code"] == 0
    statistics = payload["data"]["result"]["statistics"]
    assert statistics["input"] == 1
    assert statistics["not_applicable"] == 1


def test_upload_single_jsonl_with_media_reference_is_rejected(client, unique_id):
    body = json.dumps({"sample_id": "with_media",
                       "audio": {"path": "media/a.wav"}}) + "\n"
    response = client.post("/uploadFile", params={"uniqueId": unique_id},
                           files={"file": ("demo.jsonl", body.encode("utf-8"), "application/json")})
    assert response.status_code == 400
    assert "媒体" in response.json()["msg"]


def test_upload_rejects_non_utf8_single_file(client, unique_id):
    response = client.post("/uploadFile", params={"uniqueId": unique_id},
                           files={"file": ("demo.jsonl", "样例".encode("gbk"), "application/json")})
    assert response.status_code == 400


# ---------------------------------------------------------------- predict

def test_predict_requires_uploaded_task(client, unique_id):
    response = predict(client, unique_id, False)
    assert response.status_code == 400
    assert response.json()["code"] == 1


def test_predict_unified_response_contract(client, uploaded_fixture):
    """[v4.2] 统一响应：operator/status/outputFile/result；result 为 status 双字段业务摘要。"""
    data = _intermediate(client, uploaded_fixture)

    assert data["operator"] == "normalize_audio"
    assert data["status"] == "success"
    assert data["outputFile"]["entrypoint"] == "06_audio_index.jsonl"
    summary = data["result"]
    assert summary["status"] == 1
    assert summary["process_status"] == "partial"
    # 阶段首必须从 input/ 的上传入口读取（任务相对路径）
    assert summary["input"].startswith("input/")
    assert set(summary["statistics"]).issuperset(
        {"input", "output", "rejected", "not_applicable", "conservation_ok"})


def test_predict_final_returns_unified_file_object(client, uploaded_fixture):
    data = _final(client, uploaded_fixture)
    file_object = data["outputFile"]

    # [v4.2] isLastOperator=true 与非阶段末完全同形，无血缘文件字段
    assert set(data) == {"operator", "status", "outputFile", "result"}
    assert file_object["format"] == "crossmodal-files-v1"
    assert file_object["entrypoint"] == "06_audio_index.jsonl"
    paths = {item["path"] for item in file_object["files"]}
    assert {"06_audio_index.jsonl", "06_rejects.jsonl", "manifest.json"} <= paths
    assert any(item["path"].startswith("06_audio/") for item in file_object["files"])
    assert any(item["path"].startswith("media/") for item in file_object["files"])


def test_final_payload_restores_to_readable_media(client, uploaded_fixture):
    """阶段末负载必须等于实际输出内容：还原后可解析且全部引用媒体可读。"""
    data = _final(client, uploaded_fixture)
    entry = restored_path(data["outputFile"])
    root = entry.parent

    assert entry.name == "06_audio_index.jsonl"
    assert (root / "manifest.json").is_file()

    records = [json.loads(line) for line in entry.read_text(encoding="utf-8").splitlines() if line]
    assert len(records) == 5
    for record in records:
        audio = record.get("audio")
        if audio is not None:
            with wave.open(str(root / audio["path"]), "rb") as reader:
                assert reader.getframerate() == 16000
                assert reader.getnchannels() == 1
            with wave.open(str(root / audio["original_path"]), "rb") as reader:
                assert reader.getnframes() > 0
        for field in ("image", "video"):
            value = record.get(field)
            if isinstance(value, dict) and value.get("path"):
                assert (root / value["path"]).is_file()


def test_final_payload_media_decodable(client, uploaded_fixture):
    """还原后的媒体必须真能解码（用 imdecode 规避 OpenCV 对非 ASCII 路径的限制）。"""
    data = _final(client, uploaded_fixture)
    entry = restored_path(data["outputFile"])
    root = entry.parent

    raw = np.frombuffer((root / "media" / "as_001" / "img_main.jpg").read_bytes(),
                        dtype=np.uint8)
    image = cv2.imdecode(raw, cv2.IMREAD_COLOR)
    assert image is not None
    assert tuple(image.shape[:2]) == (80, 100)


def test_predict_stage_chaining_via_output_file_object(client, uploaded_fixture, tmp_path):
    """[v4.2] 阶段链式：outputFile 内容对象恢复后重新上传，作为阶段内下一轮输入。"""
    from utils.transport import restore_file_object

    first = _intermediate(client, uploaded_fixture)
    restored = tmp_path / "chain"
    entry = restore_file_object(first["outputFile"], restored)
    assert entry.name == "06_audio_index.jsonl"
    files = {path.relative_to(restored).as_posix(): path.read_bytes()
             for path in restored.rglob("*") if path.is_file()}
    again = upload_zip(client, uploaded_fixture, make_zip(files))
    assert again.json()["code"] == 0, again.text

    second = _intermediate(client, uploaded_fixture)
    assert second["result"]["statistics"]["input"] == 5


def test_predict_rejects_cross_task_input_file(client, uploaded_fixture):
    outside = (SAMPLE_DIR / "demo.jsonl").resolve()
    response = predict(client, uploaded_fixture, False, inputFile=str(outside))
    assert response.status_code == 422
    assert response.json()["code"] == 1


@pytest.mark.parametrize("payload", [
    {"isLastOperator": "false"},
    {"isLastOperator": 1},
    {"isLastOperator": None},
])
def test_predict_is_last_operator_must_be_strict_bool(client, uploaded_fixture, payload):
    body = {"uniqueId": uploaded_fixture, **payload}
    response = client.post("/predict", json=body)
    assert response.status_code == 422
    assert response.json()["code"] == 1


def test_predict_rejects_unknown_body_field(client, uploaded_fixture):
    body = {"uniqueId": uploaded_fixture, "isLastOperator": False, "unexpected": 1}
    response = client.post("/predict", json=body)
    assert response.status_code == 422


def test_predict_business_override_is_applied(client, uploaded_fixture):
    data=_final(client,uploaded_fixture,config={'enabled':True,'sample_rate':16000,'pcm_bits':16})
    statistics=data['result']['statistics']
    assert statistics['requested_sample_rate']==16000
    entry=restored_path(data['outputFile'])
    record=json.loads(entry.read_text('utf-8').splitlines()[0])
    assert record['audio']['sample_rate']==16000 and record['audio']['channels']==1
    response=client.post('/predict',json={'uniqueId':uploaded_fixture,'isLastOperator':False,'config':{'sample_rate':8000}})
    assert response.status_code==400


@pytest.mark.parametrize("config", [
    {"sample_rate": 4000},
    {"channels": 3},
    {"pcm_bits": 12},
    {"unknown_field": 1},
    {"operator": {"name": "hijack", "version": "1"}},
    {"stage_id": "S9"},
])
def test_predict_rejects_invalid_config(client, uploaded_fixture, config):
    response = predict(client, uploaded_fixture, False, config=config)
    assert response.status_code == 400
    assert response.json()["code"] == 1


def test_predict_empty_input_reports_empty_input(client, unique_id):
    response = client.post("/uploadFile", params={"uniqueId": unique_id},
                           files={"file": ("demo.jsonl", b"\n\n", "application/json")})
    assert response.json()["code"] == 0
    payload = predict(client, unique_id, True).json()
    assert payload["code"] == 0
    assert payload["msg"].startswith("empty_input")
    assert payload["data"]["result"]["statistics"]["input"] == 0


def test_predict_response_never_exposes_repo_paths(client, uploaded_fixture):
    data = _final(client, uploaded_fixture)
    text = json.dumps(data, ensure_ascii=False)
    assert r"G:\HYQX" not in text
    assert "G:/HYQX" not in text
    assert "/site-packages/" not in text


# ---------------------------------------------------------------- removeFile / downloadFile

def test_download_file_is_not_registered(client):
    assert client.post("/downloadFile").status_code == 404
    assert client.get("/downloadFile").status_code == 404


def test_remove_file_is_idempotent_and_isolated(client, unique_id):
    upload_zip(client, unique_id, sample_zip_bytes())
    other = unique_id + "_other"
    upload_zip(client, other, sample_zip_bytes())

    removed = client.post("/removeFile", params={"uniqueId": unique_id})
    assert removed.status_code == 200 and removed.json()["code"] == 0
    again = client.post("/removeFile", params={"uniqueId": unique_id})
    assert again.status_code == 200 and again.json()["code"] == 0

    assert predict(client, unique_id, False).status_code == 400
    assert predict(client, other, False).status_code == 200


def test_remove_file_requires_valid_unique_id(client):
    assert client.post("/removeFile", params={"uniqueId": "a/b"}).status_code == 422


# ---------------------------------------------------------------- 信封一致性

def test_all_failures_use_unified_envelope(client, unique_id):
    samples = [
        upload_zip(client, "", b"x"),
        upload_zip(client, unique_id, b""),
        client.post("/predict", json={"uniqueId": unique_id, "isLastOperator": "no"}),
        client.post("/removeFile", params={"uniqueId": "bad/id"}),
    ]
    for response in samples:
        body = response.json()
        assert set(body) == {"code", "msg", "data"}
        assert body["code"] == 1
        assert isinstance(body["msg"], str) and body["msg"]
        assert body["data"] == {}


# --------------------------------------------------------------------------- #
# [v4.2] 形态一单文件流协议（multipart/form-data + detection 别名）
# --------------------------------------------------------------------------- #

def test_single_file_form_returns_wav_stream(client, unique_id):
    audio = read_sample_files(
        ["media/aud_sine_22050_stereo_1s.wav"])["media/aud_sine_22050_stereo_1s.wav"]
    response = client.post("/predict",
                           params={"detection": "normalize_audio", "uniqueId": unique_id},
                           files={"file": ("aud_sine.wav", audio, "audio/wav")})
    assert response.status_code == 200, response.text
    assert response.headers["content-type"].startswith("audio/wav")
    import io

    with wave.open(io.BytesIO(response.content), "rb") as reader:
        assert reader.getframerate() == 16000
        assert reader.getnchannels() == 1


def test_single_file_form_detection_alias_and_mismatch(client, unique_id):
    audio = read_sample_files(
        ["media/aud_sine_8000_mono_05s.wav"])["media/aud_sine_8000_mono_05s.wav"]
    ok = client.post("/predict", params={"detection": "M07", "uniqueId": unique_id},
                     files={"file": ("a.wav", audio, "audio/wav")})
    assert ok.status_code == 200, ok.text

    mismatch = client.post("/predict", params={"detection": "M06"},
                           files={"file": ("a.wav", audio, "audio/wav")})
    assert mismatch.status_code == 422
    assert mismatch.json()["code"] == 1

    conflict = client.post("/predict",
                           params={"detection": "M07", "operatorId": "normalize_audio"},
                           files={"file": ("a.wav", audio, "audio/wav")})
    assert conflict.status_code == 422
    assert conflict.json()["code"] == 1

    # [D2] map.json 登记表英文 operatorId 别名可寻址
    registry = client.post("/predict", params={"detection": "audio_standardization",
                                               "uniqueId": unique_id},
                           files={"file": ("a.wav", audio, "audio/wav")})
    assert registry.status_code == 200, registry.text


def test_single_file_form_missing_file(client):
    # multipart 表单但缺少 file 字段
    missing = client.post("/predict", params={"detection": "normalize_audio"},
                          files={"other": ("note.txt", b"hi", "text/plain")})
    assert missing.json()["status"] == 0


def test_health_reports_identity(client):
    """[D1] /health 存活探针：返回算子标识/状态。"""
    body = client.get("/health").json()
    assert body["code"] == 0
    assert body["data"]["status"] == "ok"
    assert body["data"]["operator"] == "audio_standardization"
