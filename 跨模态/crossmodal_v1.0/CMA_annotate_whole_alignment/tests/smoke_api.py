"""三接口冒烟脚本（独立可执行，不属于 pytest 用例集）。

用法：
    python tests/smoke_api.py

会在临时目录下依次验证：/health → /uploadFile(两个 uniqueId) → /predict(中间形态)
→ /predict(阶段末形态) → /removeFile，并打印真实计数与守恒结果。
"""
from __future__ import annotations

import io
import json
import os
import shutil
import sys
import tempfile
import zipfile
from pathlib import Path

BASE = Path(__file__).resolve().parent.parent
if str(BASE) not in sys.path:
    sys.path.insert(0, str(BASE))

SAMPLE = BASE / "dataset" / "sample"


def bundle() -> bytes:
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w", zipfile.ZIP_DEFLATED) as archive:
        for path in sorted(SAMPLE.rglob("*")):
            if path.is_file():
                archive.write(str(path), path.relative_to(SAMPLE).as_posix())
    return buffer.getvalue()


def main() -> int:
    workspace = Path(tempfile.mkdtemp(prefix="op16_smoke_"))
    os.environ["OUTPUT_DIR"] = str(workspace / "output")
    try:
        from fastapi.testclient import TestClient

        import app as app_module

        app_module.OUTPUT_ROOT = (workspace / "output").resolve()
        app_module.OUTPUT_ROOT.mkdir(parents=True, exist_ok=True)
        client = TestClient(app_module.app)
        payload = bundle()
        report = {}

        health = client.get("/health").json()
        report["health"] = health["code"]

        for unique_id in ("smoke_api_a", "smoke_api_b"):
            uploaded = client.post(
                "/uploadFile", params={"uniqueId": unique_id},
                files={"file": ("bundle.zip", payload, "application/zip")}).json()
            report["upload_" + unique_id] = uploaded["code"]
            assert uploaded["code"] == 0, uploaded

        middle = client.post("/predict", json={"uniqueId": "smoke_api_a",
                                               "isLastOperator": False}).json()
        assert middle["code"] == 0, middle
        report["middle_keys"] = sorted(middle["data"].keys())
        # [v4.2] 统一响应形态：不再返回 lineageFile，始终返回 outputFile
        report["middle_has_lineage_file"] = "lineageFile" in middle["data"]

        # 阶段内后续算子：把上一运行的产物作为 inputFile 传回（本算子识别后直接复用）
        own_output = (workspace / "output" / "smoke_api_a" / "16_whole_alignment"
                      / "run001" / "16_whole_alignment.jsonl")
        reuse = client.post("/predict", json={
            "uniqueId": "smoke_api_a", "isLastOperator": False,
            "inputFile": str(own_output)}).json()
        report["reuse"] = reuse["code"]

        final = client.post("/predict", json={"uniqueId": "smoke_api_a",
                                              "isLastOperator": True}).json()
        assert final["code"] == 0, final
        files = final["data"]["outputFile"]
        report["final_keys"] = sorted(final["data"].keys())
        report["final_entrypoint"] = files["entrypoint"]
        report["final_file_count"] = len(files["files"])
        report["final_has_lineage_file"] = "lineageFile" in final["data"]

        stats = final["data"]["result"]["statistics"]
        report["conservation_ok"] = stats["conservation_ok"]
        report["main_records"] = stats.get("output")

        bad = client.post("/predict", json={"uniqueId": "smoke_api_a",
                                            "isLastOperator": False,
                                            "inputFile": "../smoke_api_b/input/bundle.zip"})
        report["traversal_rejected"] = bad.json()["code"]

        removed = client.post("/removeFile", params={"uniqueId": "smoke_api_a"}).json()
        report["remove"] = removed["code"], removed["data"]["deleted"]
        again = client.post("/removeFile", params={"uniqueId": "smoke_api_a"}).json()
        report["remove_again_deleted"] = again["data"]["deleted"]
        report["downloadFile_status"] = client.post("/downloadFile").status_code

        print(json.dumps(report, ensure_ascii=False, indent=2))
        return 0
    finally:
        shutil.rmtree(str(workspace), ignore_errors=True)


if __name__ == "__main__":
    raise SystemExit(main())
