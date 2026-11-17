"""算子 #11 三接口契约测试：上传/预测/删除、阶段中间与阶段末响应、路径与 ZIP 安全。"""
from __future__ import annotations

import io
import json
import sys
import zipfile
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

BASE = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(BASE))

import app as app_module  # noqa: E402
from app import OUTPUT_ROOT, app  # noqa: E402
from utils.transport import restore_file_object  # noqa: E402

client = TestClient(app)
SAMPLE_DIR = BASE / "dataset" / "sample"


def make_zip(entry: str = "samples.jsonl", files=None) -> bytes:
    payload = {
        "format": "crossmodal-manifest-v1",
        "entrypoint": entry,
        "artifacts": [{"path": entry, "role": "main"}],
    }
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as archive:
        archive.writestr("manifest.json", json.dumps(payload, ensure_ascii=False))
        targets = files if files is not None else sorted(
            p for p in SAMPLE_DIR.iterdir() if p.name != "make_sample.py")
        for path in targets:
            archive.write(path, path.name)
    return buf.getvalue()


def upload(uid: str, data: bytes, name: str = "samples.zip"):
    return client.post("/uploadFile", params={"uniqueId": uid},
                       files={"file": (name, data)})


def test_upload_and_intermediate_response_contract():
    uid = "api_intermediate"
    response = upload(uid, make_zip())
    assert response.status_code == 200
    assert response.json()["code"] == 0

    body = client.post("/predict", json={"uniqueId": uid,
                                         "isLastOperator": False}).json()
    assert body["code"] == 0
    # [v4.2] 形态二统一响应：去 lineageFile，始终返回 operator/status/outputFile/result
    assert set(body["data"]) == {"operator", "status", "outputFile", "result"}
    assert body["data"]["operator"] == "generate_candidate_pairs"
    assert body["data"]["status"] == "success"
    assert body["data"]["outputFile"]["format"] == "crossmodal-files-v1"
    assert body["data"]["result"]["statistics"]["pairs_total"] == 8


def test_stage_first_request_needs_no_input_file_and_stage_tail_object():
    uid = "api_stage_tail"
    assert upload(uid, make_zip()).json()["code"] == 0
    intermediate = client.post("/predict", json={"uniqueId": uid,
                                                 "isLastOperator": False}).json()
    # [v4.2] 业务摘要内的产物路径为任务相对路径，可直接回传 inputFile
    temp_file = intermediate["data"]["result"]["files"]["outputFile"]

    body = client.post("/predict", json={"uniqueId": uid, "isLastOperator": True,
                                         "inputFile": temp_file}).json()
    assert body["code"] == 0
    assert set(body["data"]) == {"operator", "status", "outputFile", "result"}

    files = body["data"]["outputFile"]
    assert files["format"] == "crossmodal-files-v1"
    assert files["entrypoint"] == "11_candidate_pairs.jsonl"

    restore_dir = OUTPUT_ROOT / uid / "restored"
    entry = restore_file_object(files, restore_dir)
    assert entry.is_file()

    runs = OUTPUT_ROOT / uid / "generate_candidate_pairs"
    first_run = runs / "run001"
    second_run = runs / "run002"
    assert len((first_run / "11_candidate_pairs.jsonl").read_text(
        encoding="utf-8").splitlines()) == 8
    # 第二次把候选对清单当作 inputFile 传回：本算子只消费准入对象，
    # 因此结果为空，且必须与本次运行的产物一致——证明没有回退去读旧默认入口。
    assert entry.read_text(encoding="utf-8") == \
        (second_run / "11_candidate_pairs.jsonl").read_text(encoding="utf-8")
    assert entry.read_text(encoding="utf-8") == ""

    # 文件内容对象必须能逐字节恢复本次运行的全部入口/清单/媒体
    for item in files["files"]:
        restored = restore_dir / item["path"]
        assert restored.is_file()
        assert restored.read_bytes() == (second_run / item["path"]).read_bytes()
    # 第一次运行真实携带了 6 个被引用的媒体文件（第二次输入没有再引用媒体）
    manifest = json.loads((first_run / "manifest.json").read_text(encoding="utf-8"))
    media = [item for item in manifest["artifacts"] if item["role"] == "media"]
    assert len(media) == 6
    for item in media:
        assert (first_run / item["path"]).is_file()


def test_predict_argument_validation():
    uid = "api_validation"
    assert upload(uid, make_zip()).json()["code"] == 0

    assert client.post("/predict", json={"uniqueId": uid}).json()["code"] == 1
    assert client.post("/predict", json={"uniqueId": uid,
                                         "isLastOperator": "false"}).json()["code"] == 1
    assert client.post("/predict", json={"uniqueId": uid, "isLastOperator": 0}).json()["code"] == 1
    assert client.post("/predict", json={"uniqueId": uid, "isLastOperator": False,
                                         "extra": 1}).json()["code"] == 1
    assert client.post("/predict", json={"uniqueId": uid, "isLastOperator": False,
                                         "config": "x"}).json()["code"] == 1
    assert client.post("/predict", json="not-an-object").json()["code"] == 1


@pytest.mark.parametrize("bad", ["", "..", "a/b", "a\\b", "../escape", "/abs/path", "CON"])
def test_illegal_unique_id_rejected(bad):
    response = upload(bad, make_zip())
    body = response.json()
    assert body["code"] == 1
    assert set(body) == {"code", "msg", "data"}


def test_input_file_path_escape_rejected():
    uid = "api_escape"
    assert upload(uid, make_zip()).json()["code"] == 0
    for bad in ("../../secret.jsonl", "C:/Windows/system.ini", str(BASE / "configs" / "config.yaml")):
        body = client.post("/predict", json={"uniqueId": uid, "isLastOperator": False,
                                             "inputFile": bad}).json()
        assert body["code"] == 1, bad


def test_config_override_and_identity_rejected():
    uid = "api_override"
    assert upload(uid, make_zip()).json()["code"] == 0
    body = client.post("/predict", json={"uniqueId": uid, "isLastOperator": False,
                                         "config": {"keyword_min_overlap": 5}}).json()
    assert body["code"] == 0
    assert body["data"]["result"]["statistics"]["pairs_by_evidence"]["C"] == 0
    assert body["data"]["result"]["config"]["pairing"]["keyword_min_overlap"] == 5

    limited = client.post("/predict", json={"uniqueId": uid, "isLastOperator": False,
                                            "config": {"operator": "hacked"}}).json()
    assert limited["code"] == 1


def test_zip_slip_and_duplicate_members_rejected():
    slip = io.BytesIO()
    with zipfile.ZipFile(slip, "w") as archive:
        archive.writestr("../evil.jsonl", "{}")
    assert upload("api_slip", slip.getvalue()).json()["code"] == 1
    assert not (OUTPUT_ROOT / "api_slip" / "input").exists()

    dup = io.BytesIO()
    with zipfile.ZipFile(dup, "w") as archive:
        archive.writestr("samples.jsonl", "{}")
        archive.writestr("SAMPLES.JSONL", "{}")
    assert upload("api_dup", dup.getvalue()).json()["code"] == 1


def test_upload_file_limit(monkeypatch):
    monkeypatch.setattr(app_module, "MAX_FILES", 1)
    response = upload("api_limit", make_zip())
    assert response.json()["code"] == 1
    assert "文件数" in response.json()["msg"]


def test_upload_requires_file_and_entry():
    assert client.post("/uploadFile", params={"uniqueId": "api_no_file"}).json()["code"] == 1

    ambiguous = io.BytesIO()
    with zipfile.ZipFile(ambiguous, "w") as archive:
        archive.writestr("a.jsonl", "{}")
        archive.writestr("b.jsonl", "{}")
    assert upload("api_ambiguous", ambiguous.getvalue()).json()["code"] == 1


def test_remove_file_isolation():
    keep, drop = "api_keep", "api_drop"
    assert upload(keep, make_zip()).json()["code"] == 0
    assert upload(drop, make_zip()).json()["code"] == 0

    body = client.post("/removeFile", params={"uniqueId": drop}).json()
    assert body["code"] == 0 and body["data"]["deleted"] is True
    assert not (OUTPUT_ROOT / drop).exists()
    assert (OUTPUT_ROOT / keep / "input").is_dir()

    again = client.post("/removeFile", params={"uniqueId": drop}).json()
    assert again["code"] == 1 and again["data"]["deleted"] is False

    assert client.post("/removeFile", params={"uniqueId": "../escape"}).json()["code"] == 1


def test_no_download_file_interface():
    response = client.get("/downloadFile")
    assert response.status_code == 404
    assert response.json()["code"] == 1


def test_envelope_shape_for_all_interfaces():
    for response in (upload("api_envelope", make_zip()),
                     client.post("/predict", json={"uniqueId": "api_envelope",
                                                   "isLastOperator": False}),
                     client.post("/removeFile", params={"uniqueId": "api_envelope"})):
        assert response.status_code == 200
        body = response.json()
        assert set(body) == {"code", "msg", "data"}
        assert body["code"] in (0, 1)


# ---------------------------------------------------------------- [v4.2] 形态一（单文件流）


def test_single_file_protocol_jsonl_success():
    data = (SAMPLE_DIR / "samples.jsonl").read_bytes()
    response = client.post("/predict", params={"detection": "M11"},
                           files={"file": ("samples.jsonl", data, "application/jsonl")})
    body = response.json()
    assert body["status"] == 1
    assert len(body["pairs"]) == 8
    assert isinstance(body["unpaired"], list)
    assert body["uniqueId"]


def test_single_file_protocol_detection_mismatch_rejected():
    data = (SAMPLE_DIR / "samples.jsonl").read_bytes()
    response = client.post("/predict", params={"detection": "M12"},
                           files={"file": ("samples.jsonl", data)})
    body = response.json()
    assert body["status"] == 0
    assert "不匹配" in body["msg"]


def test_single_file_protocol_media_rejected():
    response = client.post("/predict", params={"detection": "generate_candidate_pair"},
                           files={"file": ("cat.jpg", b"\xff\xd8\xff\xe0", "image/jpeg")})
    body = response.json()
    assert body["status"] == 0
    assert "JSON" in body["msg"]


def test_health_reports_identity():
    """[D1] /health 存活探针：返回算子标识/状态。"""
    body = client.get("/health").json()
    assert body["code"] == 0
    assert body["data"]["operator"] == "generate_candidate_pairs"
    assert body["data"]["stageId"] == "S3"
