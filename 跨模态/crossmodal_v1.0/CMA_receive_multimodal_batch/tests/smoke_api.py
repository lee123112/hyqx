"""真实 HTTP 冒烟：启动本算子 ``uvicorn`` 子进程并用 httpx 打三个正式接口。

与 ``test_api.py``（TestClient 进程内）不同，这里验证**真实服务进程**：
上传（zip/多文件）、阶段首不传 inputFile、中间/阶段末响应形态、
两个 uniqueId 隔离、路径安全、非法 uniqueId、严格布尔、`/downloadFile` 404、
`removeFile` 幂等，以及阶段末对象能否还原成可搬运交付物。

用法::

    python tests/smoke_api.py
"""
from __future__ import annotations

import io
import json
import os
import shutil
import socket
import subprocess
import sys
import tempfile
import time
import zipfile
from pathlib import Path
from typing import Any, List, Tuple

BASE = Path(__file__).resolve().parents[1]
if str(BASE) not in sys.path:
    sys.path.insert(0, str(BASE))

ENTRY_NAME = "01_batch_manifest.jsonl"
CHECKS: List[Tuple[str, bool, str]] = []


def check(name: str, condition: Any, detail: str = "") -> None:
    CHECKS.append((name, bool(condition), detail))


def free_port() -> int:
    sock = socket.socket()
    sock.bind(("127.0.0.1", 0))
    port = int(sock.getsockname()[1])
    sock.close()
    return port


def record(sample_id: str, *, ext: str = "jpg", remote: bool = True,
           license: Any = "可训练", **extra: Any) -> dict:
    link = ("oss://bucket/obj/%s.%s" % (sample_id, ext) if remote
            else "media/%s.%s" % (sample_id, ext))
    payload = {"sample_id": sample_id,
               "files": [{"oss_uri": link} if remote else {"path": link}],
               "source": "预训练图像产线交付", "source_id": "SRC-IMG-2026",
               "batch_id": "BATCH-20261001-01", "upstream_version": "pipeline-1.4.2",
               "license": license}
    payload.update(extra)
    return payload


def jsonl(rows) -> bytes:
    return ("\n".join(json.dumps(row, ensure_ascii=False) for row in rows)
            + "\n").encode("utf-8")


def zip_payload(rows, extra: dict = None) -> bytes:
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w") as archive:
        archive.writestr(ENTRY_NAME, jsonl(rows))
        for name, payload in (extra or {}).items():
            archive.writestr(name, payload)
    return buffer.getvalue()


def main() -> int:
    import httpx

    port = free_port()
    output_root = Path(tempfile.mkdtemp(prefix="op01_smoke_"))
    env = dict(os.environ)
    env["OUTPUT_DIR"] = str(output_root)
    env["PYTHONUNBUFFERED"] = "1"
    env["PYTHONPATH"] = str(BASE) + os.pathsep + env.get("PYTHONPATH", "")
    process = subprocess.Popen(
        [sys.executable, "-m", "uvicorn", "app:app", "--host", "127.0.0.1",
         "--port", str(port), "--log-level", "warning"],
        cwd=str(BASE), env=env, stdout=subprocess.PIPE, stderr=subprocess.STDOUT)
    base = "http://127.0.0.1:%d" % port
    try:
        client = httpx.Client(base_url=base, timeout=30.0)
        deadline = time.time() + 40
        health: Any = None
        while time.time() < deadline:
            try:
                response = client.get("/health")
                if response.status_code == 200:
                    health = response.json()
                    break
            except Exception:  # noqa: BLE001 - 服务尚未就绪
                time.sleep(0.4)
            if process.poll() is not None:
                break
            time.sleep(0.4)
        check("服务启动 /health=200", health is not None,
              "" if health else "uvicorn 未能启动")
        if health is None:
            raise SystemExit(1)
        check("健康检查声明三接口",
              health["interfaces"] == ["/uploadFile", "/predict", "/removeFile"],
              str(health.get("interfaces")))
        check("未声明 /downloadFile", health["downloadFile"] is False)

        # 1) 上传：zip（含媒体）+ 多文件
        payload = zip_payload([record("smoke-1"), record("smoke-2"),
                               record("smoke-local", remote=False)],
                              extra={"media/smoke-local.jpg": b"\xff\xd8\xff\xe0opaque"})
        up1 = client.post("/uploadFile", params={"uniqueId": "smoke_a"},
                          files={"file": ("bundle.zip", payload,
                                          "application/octet-stream")}).json()
        check("上传 zip 成功且回入口", up1["code"] == 0 and
              up1["data"]["entrypoint"] == ENTRY_NAME, json.dumps(up1, ensure_ascii=False))
        check("上传回文件数=2（清单+媒体）",
              up1["data"]["fileCount"] == 2
              and up1["data"]["files"] == sorted([ENTRY_NAME, "media/smoke-local.jpg"]),
              str(up1["data"]))

        up2 = client.post("/uploadFile", params={"uniqueId": "smoke_b"},
                          files=[("files", (ENTRY_NAME, jsonl([record("smoke-b1")]),
                                            "application/json")),
                                 ("files", ("media/smoke-b1.jpg", b"\xff\xd8\xff\xe0",
                                            "image/jpeg"))]).json()
        check("多文件上传成功", up2["code"] == 0 and up2["data"]["fileCount"] == 2,
              json.dumps(up2, ensure_ascii=False))

        # 2) 阶段首：不传 inputFile，中间响应只含 lineageFile
        middle = client.post("/predict", json={"uniqueId": "smoke_a",
                                              "isLastOperator": False}).json()
        check("阶段首 predict code=0", middle["code"] == 0,
              json.dumps(middle, ensure_ascii=False)[:300])
        check("中间响应 data 恰为 lineageFile",
              set(middle["data"]) == {"lineageFile"}, str(sorted(middle["data"])))
        node = middle["data"]["lineageFile"]
        check("中间血缘含临时 outputFile", bool(node.get("outputFile")))
        check("阶段首 inputFile 为包内入口", node["inputFile"] == ENTRY_NAME,
              str(node.get("inputFile")))
        check("统计真实 registered=3", node["statistics"]["registered"] == 3,
              str(node["statistics"].get("registered")))
        check("统计守恒通过", node["statistics"]["conservation_ok"] is True)
        check("本地文件头校验生效 local_verified=1",
              node["statistics"]["local_verified"] == 1,
              str(node["statistics"].get("local_verified")))

        # 3) 阶段末：outputFile 文件对象 + 完整阶段血缘数组
        last = client.post("/predict", json={"uniqueId": "smoke_a",
                                            "isLastOperator": True}).json()
        check("阶段末 data 恰为 outputFile+lineageFile",
              set(last["data"]) == {"outputFile", "lineageFile"},
              str(sorted(last["data"])))
        file_object = last["data"]["outputFile"]
        check("阶段末对象格式 crossmodal-files-v1",
              file_object["format"] == "crossmodal-files-v1")
        named = [item["path"] for item in file_object["files"]]
        check("阶段末含四类清单与台账",
              all(name in named for name in ("01_workitems.jsonl",
                                             "01_origin_quarantine.jsonl",
                                             "01_sample_quarantine.jsonl",
                                             "01_return.jsonl",
                                             "01_batch_registry.json",
                                             "manifest.json")), str(named))
        stage = last["data"]["lineageFile"]
        check("阶段血缘为数组且剔除临时 outputFile",
              isinstance(stage, list) and all("outputFile" not in item for item in stage))

        # 4) 阶段末对象可还原并续跑下一节点
        from utils import transport

        restored_root = output_root / "smoke_restore"
        restored_entry = transport.restore_file_object(file_object, restored_root)
        check("阶段末对象可还原为入口 JSONL",
              restored_entry.name == "01_workitems.jsonl" and restored_entry.is_file())

        chain = client.post("/predict", json={"uniqueId": "smoke_a",
                                             "isLastOperator": False,
                                             "inputFile": node["outputFile"]}).json()
        check("阶段内以临时 outputFile 续跑成功", chain["code"] == 0,
              json.dumps(chain, ensure_ascii=False)[:200])

        # 5) 任务隔离
        last_b = client.post("/predict", json={"uniqueId": "smoke_b",
                                              "isLastOperator": True}).json()
        check("两个 uniqueId 统计互不污染",
              last["data"]["lineageFile"][0]["statistics"]["registered"] == 3 and
              last_b["data"]["lineageFile"][0]["statistics"]["registered"] == 1)
        check("血缘 uniqueId 各自独立",
              {item["uniqueId"] for item in last["data"]["lineageFile"]} == {"smoke_a"} and
              {item["uniqueId"] for item in last_b["data"]["lineageFile"]} == {"smoke_b"})

        cross = client.post("/predict", json={
            "uniqueId": "smoke_b", "isLastOperator": False,
            "inputFile": str(output_root / "smoke_a" / "input" / ENTRY_NAME)}).json()
        check("跨任务 inputFile 被拒绝", cross["code"] == 1, str(cross.get("msg")))

        # 6) 路径与参数安全
        bad_uid = client.post("/uploadFile", params={"uniqueId": "../escape"},
                              files={"file": (ENTRY_NAME, jsonl([record("x")]),
                                              "application/json")}).json()
        check("非法 uniqueId 被拒绝", bad_uid["code"] == 1, str(bad_uid.get("msg")))
        zip_slip = client.post("/uploadFile", params={"uniqueId": "smoke_slip"},
                               files={"file": ("slip.zip", _slip_zip(),
                                               "application/octet-stream")}).json()
        check("zip 路径穿越被拒绝", zip_slip["code"] == 1, str(zip_slip.get("msg")))
        check("zip 穿越未落盘",
              not (output_root / "escape.jsonl").exists() and
              not (output_root / "smoke_slip" / "escape.jsonl").exists())

        invalid_bool = client.post("/predict", json={"uniqueId": "smoke_a",
                                                    "isLastOperator": "false"})
        check("isLastOperator 非布尔 → 422+code=1",
              invalid_bool.status_code == 422 and invalid_bool.json()["code"] == 1)

        download = client.get("/downloadFile", params={"uniqueId": "smoke_a"})
        check("/downloadFile 未注册 → 404+code=1",
              download.status_code == 404 and download.json()["code"] == 1)

        frozen = client.post("/predict", json={"uniqueId": "smoke_b",
                                              "isLastOperator": False,
                                              "config": {"sources": ["collection_platform"]}}).json()
        check("冻结来源配置不可覆盖", frozen["code"] == 1, str(frozen.get("msg")))

        # 7) removeFile 幂等且不越界
        removed_1 = client.post("/removeFile", params={"uniqueId": "smoke_a"}).json()
        removed_2 = client.post("/removeFile", params={"uniqueId": "smoke_a"}).json()
        check("removeFile 幂等（false 二次）",
              removed_1["data"]["removed"] is True and
              removed_2["data"]["removed"] is False)
        check("removeFile 只删除目标任务",
              not (output_root / "smoke_a").exists() and
              (output_root / "smoke_b").is_dir())
        client.close()
    finally:
        process.terminate()
        try:
            process.wait(timeout=10)
        except subprocess.TimeoutExpired:  # pragma: no cover
            process.kill()
        shutil.rmtree(output_root, ignore_errors=True)

    passed = sum(1 for _name, ok, _detail in CHECKS if ok)
    for name, ok, detail in CHECKS:
        if not ok:
            print("FAIL  %s  %s" % (name, detail))
    print("SMOKE %d/%d passed" % (passed, len(CHECKS)))
    return 0 if passed == len(CHECKS) else 1


def _slip_zip() -> bytes:
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w") as archive:
        archive.writestr("../escape.jsonl", "{}\n")
    return buffer.getvalue()


if __name__ == "__main__":
    sys.exit(main())
