"""三接口与媒体可搬运性测试。

覆盖 DEVELOPMENT_CONTRACT §2/§3：
- ``/health`` 存活探针；
- ``/predict`` 中间算子只返回本节点血缘（保留临时 outputFile 绝对路径）；
- ``/predict`` 阶段末返回 ``crossmodal-files-v1`` 文件内容对象 + 完整阶段血缘；
- ``/downloadFile`` 保留路径但不实现（明确 404）；
- ``/removeFile`` 幂等；
- 媒体可搬运性：把阶段末内容对象严格恢复到全新空目录后，主清单所有媒体引用可读。
"""
from __future__ import annotations

import io
import json
import zipfile
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

import app as app_module
from utils import transport

SAMPLE_ZIP_FILES = ("demo.jsonl", "manifest.json", "lineage_history.json")


def _read_jsonl(path: Path):
    records = []
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if line:
            records.append(json.loads(line))
    return records


def _build_sample_zip(sample_dir: Path) -> bytes:
    """把 ``dataset/sample`` 打成上传用的素材包（zip 根即 input/ 内容）。"""
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w", zipfile.ZIP_DEFLATED) as archive:
        for name in SAMPLE_ZIP_FILES:
            archive.write(sample_dir / name, name)
        for media in sorted((sample_dir / "media").iterdir()):
            if media.is_file():
                archive.write(media, "media/" + media.name)
    return buffer.getvalue()


@pytest.fixture()
def client(tmp_path, monkeypatch):
    """独立 output 根的 TestClient，避免污染仓库内 output/。"""
    root = tmp_path / "output"
    monkeypatch.setenv("OUTPUT_DIR", str(root))
    with TestClient(app_module.app) as test_client:
        yield test_client, root


@pytest.fixture()
def sample_zip(sample_dir):
    return _build_sample_zip(sample_dir)


# --------------------------------------------------------------------------- #
# /health、/downloadFile
# --------------------------------------------------------------------------- #

def test_health_probe(client):
    test_client, _root = client
    response = test_client.get("/health")
    assert response.status_code == 200
    assert response.json()["status"] == "ok"


def test_download_file_is_explicitly_not_implemented(client):
    test_client, _root = client
    response = test_client.get("/downloadFile", params={"uniqueId": "x_f1"})
    assert response.status_code == 404
    body = response.json()
    assert body["success"] is False
    assert body["data"]["error"] == "not_implemented"


# --------------------------------------------------------------------------- #
# /predict —— 中间算子
# --------------------------------------------------------------------------- #

def test_predict_intermediate_returns_unified_response(client, sample_zip):
    test_client, root = client
    response = test_client.post(
        "/predict",
        data={"request": json.dumps({"uniqueId": "demoS2_api1",
                                     "isLastOperator": False})},
        files={"file": ("input.zip", sample_zip, "application/zip")},
    )
    assert response.status_code == 200
    body = response.json()
    assert body["success"] is True
    data = body["data"]
    # [v4.2] 形态二响应统一：始终含 outputFile + result，不再返回血缘
    assert set(data) == {"operator", "status", "outputFile", "result"}
    assert data["operator"] == "normalize_image"
    assert data["status"] == "success"
    assert data["outputFile"]["format"] == transport.FILE_OBJECT_FORMAT
    assert data["outputFile"]["entrypoint"] == "04_image_index.jsonl"
    assert data["result"]["process_status"] in ("success", "partial", "degraded")


def test_predict_intermediate_json_body_uses_staged_input(client, sample_dir, tmp_path):
    test_client, root = client
    # 预先铺好 input/（模拟主 agent 已投放素材，无上传）
    input_dir = root / "demoS2_api2" / "input"
    input_dir.mkdir(parents=True)
    for name in SAMPLE_ZIP_FILES:
        (input_dir).joinpath(name).write_bytes((sample_dir / name).read_bytes())
    import shutil

    shutil.copytree(sample_dir / "media", input_dir / "media")

    response = test_client.post("/predict", json={"uniqueId": "demoS2_api2",
                                                  "isLastOperator": False})
    assert response.status_code == 200
    body = response.json()
    assert body["success"] is True
    # [v4.2] 形态二统一响应：始终返回 outputFile
    assert body["data"]["outputFile"]["entrypoint"] == "04_image_index.jsonl"


# --------------------------------------------------------------------------- #
# /predict —— 阶段末（文件内容对象 + 完整阶段血缘）
# --------------------------------------------------------------------------- #

def test_predict_stage_end_returns_file_object_and_stage_lineage(client, sample_dir,
                                                                sample_zip, tmp_path):
    test_client, root = client
    response = test_client.post(
        "/predict",
        data={"request": json.dumps({"uniqueId": "demoS2_api3",
                                     "isLastOperator": True,
                                     "config": {"enabled":True,"max_side": 512}})},
        files={"file": ("input.zip", sample_zip, "application/zip")},
    )
    assert response.status_code == 200
    body = response.json()
    assert body["success"] is True
    data = body["data"]

    # --- 输出文件内容对象 ---
    output = data["outputFile"]
    assert output["format"] == transport.FILE_OBJECT_FORMAT
    assert output["entrypoint"] == "04_image_index.jsonl"
    listed = {item["path"] for item in output["files"]}
    assert "manifest.json" in listed
    assert "04_rejects.jsonl" in listed
    for item in output["files"]:
        if transport.is_text_path(item["path"]):
            assert item["encoding"] == "utf-8"
        else:
            assert item["encoding"] == "base64"

    # [v4.2] 血缘输出已停用：响应不再包含 lineageFile/stage_context
    assert "lineageFile" not in data

    # --- 媒体可搬运性：严格恢复到全新空目录后引用可读 ---
    restore_dir = tmp_path / "restored"
    entry = transport.restore_file_object(output, restore_dir)
    assert entry.name == "04_image_index.jsonl"

    records = _read_jsonl(entry)
    assert records, "主清单不应为空"
    for record in records:
        for ref in (record.get("image"), record.get("video"), record.get("audio")):
            if isinstance(ref, dict):
                media = ref.get("path") or ref.get("original_path")
                if media:
                    assert (restore_dir / media).is_file(), "媒体引用不可读：%s" % media
            elif isinstance(ref, str):
                assert (restore_dir / ref).is_file()

    # 清单内所有 artifact 均已随内容对象搬运
    manifest = json.loads((restore_dir / "manifest.json").read_text(encoding="utf-8"))
    for artifact in manifest["artifacts"]:
        assert (restore_dir / artifact["path"]).is_file()

    # 另存原图字节与 fixture 完全一致（无损搬运）
    wide = next(item for item in records if item["sample_id"] == "demo_04_wide_001")
    assert (restore_dir / wide["image"]["original_path"]).read_bytes() == \
        (sample_dir / "media" / "wide_1600x800.png").read_bytes()


def test_predict_stage_end_media_gate_passes_for_all_references(client, sample_zip):
    test_client, _root = client
    response = test_client.post(
        "/predict",
        data={"request": json.dumps({"uniqueId": "demoS2_api4", "isLastOperator": True,"config":{"enabled":True}})},
        files={"file": ("input.zip", sample_zip, "application/zip")},
    )
    assert response.status_code == 200
    assert response.json()["success"] is True


# --------------------------------------------------------------------------- #
# /predict —— 校验与错误信封
# --------------------------------------------------------------------------- #

@pytest.mark.parametrize("payload,status", [
    ({"isLastOperator": False}, 422),                       # 缺 uniqueId
    ({"uniqueId": "x_f1", "isLastOperator": "yes"}, 422),      # 非严格布尔
    ({"uniqueId": "x_f1", "isLastOperator": False, "extra": 1}, 422),  # 未知字段
    ({"uniqueId": "../escape", "isLastOperator": False}, 400),      # 非法 uniqueId
])
def test_predict_rejects_invalid_requests(client, payload, status):
    test_client, _root = client
    response = test_client.post("/predict", json=payload)
    assert response.status_code == status
    body = response.json()
    assert body["success"] is False
    assert "message" in body


def test_predict_config_must_be_object(client):
    test_client, _root = client
    response = test_client.post("/predict", json={"uniqueId": "x_f1", "isLastOperator": False,
                                                  "config": "nope"})
    assert response.status_code == 422
    assert response.json()["data"]["error"] == "validation_failed"


def test_predict_invalid_business_config_returns_failed_envelope(client, sample_zip):
    test_client, _root = client
    response = test_client.post(
        "/predict",
        data={"request": json.dumps({"uniqueId": "demoS2_api5",
                                     "isLastOperator": False,
                                     "config": {"output_format": "GIF"}})},
        files={"file": ("input.zip", sample_zip, "application/zip")},
    )
    assert response.status_code == 200
    body = response.json()
    assert body["success"] is False
    assert body["data"]["error"] == "invalid_config"


def test_predict_multipart_missing_request_field(client, sample_zip):
    test_client, _root = client
    # [v4.3] 无 request 字段的 multipart 即形态一：zip 素材包按 A 类跑列表推理，
    # 返回 outputFile 路径引用（不再是旧的“zip 按参数错误 status 0”）。
    response = test_client.post(
        "/predict?detection=M05&uniqueId=sf_zip_case",
        files={"file": ("input.zip", sample_zip, "application/zip")},
    )
    assert response.status_code == 200, response.text[:300]
    body = response.json()
    assert body["status"] == 1, body
    assert body["operator"] == "normalize_image"
    reference = body["outputFile"]
    assert reference["mode"] == "path-reference"
    assert reference["entrypoint"] == "04_image_index.jsonl"
    run_dir = Path(reference["runDir"])
    assert run_dir.is_dir()
    assert (run_dir / reference["entrypoint"]).is_file()
    package = Path(reference["zip"])
    assert package.is_file()
    with zipfile.ZipFile(package) as archive:
        assert reference["entrypoint"] in archive.namelist()


def test_predict_form1_rejects_plain_non_image_file(client):
    test_client, _root = client
    response = test_client.post(
        "/predict?uniqueId=sf_bad_suffix",
        files={"file": ("notes.txt", b"hello", "text/plain")},
    )
    assert response.status_code == 200
    assert response.json()["status"] == 0


def test_predict_unsupported_content_type(client):
    test_client, _root = client
    response = test_client.post("/predict", content=b"raw", headers={"content-type": "text/plain"})
    assert response.status_code == 415
    assert response.json()["success"] is False


def test_predict_upload_without_entrypoint_fails(client):
    test_client, _root = client
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w") as archive:
        archive.writestr("readme.txt", "no jsonl entry here")
    response = test_client.post(
        "/predict",
        data={"request": json.dumps({"uniqueId": "demoS2_api6", "isLastOperator": False})},
        files={"file": ("input.zip", buffer.getvalue(), "application/zip")},
    )
    assert response.status_code == 400
    assert response.json()["data"]["error"] == "invalid_archive"


def test_predict_input_file_traversal_is_rejected(client):
    test_client, _root = client
    response = test_client.post("/predict", json={"uniqueId": "demoS2_api7",
                                                  "isLastOperator": False,
                                                  "inputFile": "../../secret.jsonl"})
    assert response.status_code == 400
    assert response.json()["data"]["error"] == "invalid_input_file"


# --------------------------------------------------------------------------- #
# /removeFile
# --------------------------------------------------------------------------- #

def test_remove_file_is_idempotent(client):
    test_client, root = client
    task = root / "demoS2_remove"
    task.mkdir(parents=True)
    (task / "marker.txt").write_text("x", encoding="utf-8")

    first = test_client.post("/removeFile", json={"uniqueId": "demoS2_remove"})
    assert first.status_code == 200
    assert first.json()["success"] is True
    assert first.json()["data"]["deleted"] is True
    assert not task.exists()

    second = test_client.post("/removeFile", json={"uniqueId": "demoS2_remove"})
    assert second.status_code == 200
    assert second.json()["success"] is False
    assert second.json()["data"]["deleted"] is False
    # 幂等删除不应把已删除任务重新建出来
    assert not task.exists()


def test_remove_file_rejects_invalid_unique_id(client):
    test_client, _root = client
    response = test_client.post("/removeFile", json={"uniqueId": "../escape"})
    assert response.status_code == 400
    assert response.json()["data"]["error"] == "invalid_unique_id"


# --------------------------------------------------------------------------- #
# [v4.2] 形态一（单文件流）：成功返回标准化图像文件流
# --------------------------------------------------------------------------- #

def _png_bytes(width: int = 2000, height: int = 1000) -> bytes:
    from PIL import Image

    buffer = io.BytesIO()
    Image.new("RGB", (width, height), (120, 30, 200)).save(buffer, format="PNG")
    return buffer.getvalue()


def test_single_file_flow_returns_file_stream(client):
    """形态一：上传单个图像，成功返回文件流（默认 jpg，等比缩到 max_side 内）。"""
    test_client, _root = client
    response = test_client.post(
        "/predict?detection=M05&uniqueId=sf_img_1",
        files={"file": ("photo.png", _png_bytes(), "image/png")},
    )
    assert response.status_code == 200, response.text[:200]
    assert response.headers["content-type"].startswith("image/")
    payload = response.content
    assert payload[:2] == b"\xff\xd8"  # JPEG 文件头
    from PIL import Image

    image = Image.open(io.BytesIO(payload))
    assert max(image.size) <= 1024


def test_single_file_flow_filetype_png(client):
    test_client, _root = client
    response = test_client.post(
        "/predict?operatorId=normalize_image&uniqueId=sf_img_2",
        files={"file": ("photo.png", _png_bytes(64, 64), "image/png")},
        data={"fileType": "png"},
    )
    assert response.status_code == 200, response.text[:200]
    assert response.content[:8] == b"\x89PNG\r\n\x1a\n"


def test_single_file_flow_detection_mismatch(client):
    test_client, _root = client
    response = test_client.post(
        "/predict?detection=M99&uniqueId=sf_img_3",
        files={"file": ("photo.png", _png_bytes(64, 64), "image/png")},
    )
    assert response.json()["status"] == 0
    conflict = test_client.post(
        "/predict?detection=M05&operatorId=M04&uniqueId=sf_img_3",
        files={"file": ("photo.png", _png_bytes(64, 64), "image/png")},
    )
    assert conflict.json()["status"] == 0


def test_single_file_flow_corrupt_image_returns_status_0(client):
    test_client, _root = client
    response = test_client.post(
        "/predict?uniqueId=sf_img_4",
        files={"file": ("bad.png", b"not-a-real-image", "image/png")},
    )
    assert response.status_code == 200
    assert response.json()["status"] == 0
