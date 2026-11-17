"""真实 uvicorn 子进程 + HTTP 的接口 smoke。

用法：``python tests/smoke_api.py``
不依赖 TestClient，真实启动 ``app:app``（默认端口 8113，可被 PORT 覆盖）。
"""
from __future__ import annotations

import json
import os
import shutil
import socket
import subprocess
import sys
import tempfile
import time
import urllib.error
import urllib.request
import uuid
from pathlib import Path

BASE_DIR = Path(__file__).resolve().parents[1]
if str(BASE_DIR) not in sys.path:
    sys.path.insert(0, str(BASE_DIR))

from tests import fixtures  # noqa: E402
from utils import audio_text  # noqa: E402

HOST = "127.0.0.1"
RESULTS = []


def _free_port() -> int:
    with socket.socket() as probe:
        probe.bind((HOST, 0))
        return int(probe.getsockname()[1])


def check(name: str, condition: bool, detail: str = "") -> bool:
    RESULTS.append((name, bool(condition), detail))
    status = "PASS" if condition else "FAIL"
    print("[%s] %s%s" % (status, name, (" | " + detail) if detail else ""))
    return bool(condition)


def _request(url, *, method="GET", payload=None, headers=None, raw=False):
    data = None
    send_headers = dict(headers or {})
    if payload is not None:
        if raw:
            data = payload
        else:
            data = json.dumps(payload).encode("utf-8")
            send_headers.setdefault("Content-Type", "application/json")
    request = urllib.request.Request(url, data=data, method=method, headers=send_headers)
    try:
        with urllib.request.urlopen(request, timeout=120) as response:
            body = response.read()
            status = response.status
    except urllib.error.HTTPError as exc:
        body = exc.read()
        status = exc.code
    try:
        parsed = json.loads(body.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError):
        parsed = None
    return status, parsed, body


def _multipart(name: str, payload: bytes, boundary: str) -> bytes:
    head = ("--%s\r\nContent-Disposition: form-data; name=\"%s\"; filename=\"%s\"\r\n"
            "Content-Type: application/zip\r\n\r\n" % (boundary, name, "bundle.zip"))
    tail = "\r\n--%s--\r\n" % boundary
    return head.encode("utf-8") + payload + tail.encode("utf-8")


def bundle_zip(records=None, media=None) -> bytes:
    records = records if records is not None else [
        fixtures.make_pair("pair-1", transcript="今天天气很好", description="今天天气很好"),
        fixtures.make_pair("pair-2", transcript="我们一起去公园散步",
                           description="我们一起去公园散步", audio_path="media/seg_02.wav"),
    ]
    members = {"demo.jsonl": bytes(
        "\n".join(json.dumps(record, ensure_ascii=False) for record in records) + "\n",
        encoding="utf-8")}
    members.update(media if media is not None else fixtures.default_media())
    return fixtures.zip_bytes(members)


def upload(base_url: str, unique_id: str, payload: bytes):
    boundary = "----smoke%s" % uuid.uuid4().hex
    body = _multipart("file", payload, boundary)
    return _request("%s/uploadFile?uniqueId=%s" % (base_url, unique_id), method="POST",
                    payload=body, raw=True,
                    headers={"Content-Type": "multipart/form-data; boundary=%s" % boundary})


def main() -> int:
    port = int(os.environ.get("PORT", "0") or 0) or _free_port()
    base_url = "http://%s:%d" % (HOST, port)
    output_root = Path(tempfile.mkdtemp(prefix="smoke13_out_"))
    log_path = output_root / "uvicorn.log"
    env = dict(os.environ)
    env["OUTPUT_DIR"] = str(output_root)
    env["PYTHONIOENCODING"] = "utf-8"
    env["PORT"] = str(port)

    print("启动真实服务：%s（OUTPUT_DIR=%s）" % (base_url, output_root))
    with open(log_path, "wb") as log:
        server = subprocess.Popen(
            [sys.executable, "-m", "uvicorn", "app:app", "--host", HOST, "--port", str(port),
             "--log-level", "warning"],
            cwd=str(BASE_DIR), env=env, stdout=log, stderr=subprocess.STDOUT)
    try:
        ready = False
        for _ in range(120):
            if server.poll() is not None:
                break
            try:
                status, _, _ = _request("%s/openapi.json" % base_url)
                if status == 200:
                    ready = True
                    break
            except Exception:  # noqa: BLE001 - 启动期连接失败属预期
                pass
            time.sleep(0.5)
        if not check("服务就绪", ready, "" if ready else "uvicorn 未能启动，见 %s" % log_path):
            return 1

        # 1) 上传与入口
        status, body, _ = upload(base_url, "smoke_a", bundle_zip())
        check("上传返回 200/code=0", status == 200 and body and body["code"] == 0,
              "status=%s" % status)
        check("上传识别入口 demo.jsonl",
              bool(body) and body["data"].get("entrypoint") == "demo.jsonl")

        # 2) 中间态：data 只含 lineageFile
        status, body, _ = _request("%s/predict" % base_url, method="POST",
                                   payload={"uniqueId": "smoke_a", "isLastOperator": False})
        check("中间态返回 200/code=0", status == 200 and body["code"] == 0)
        check("中间态 data 仅含 lineageFile", set(body["data"].keys()) == {"lineageFile"},
              str(sorted(body["data"].keys())))
        node = body["data"]["lineageFile"]
        check("中间态含临时 outputFile", bool(node.get("outputFile"))
              and node["outputFile"].endswith(audio_text.MAIN_NAME_DISABLED))
        check("中间态统计真实（input=2, skipped=2）",
              node["statistics"]["input_records"] == 2 and node["statistics"]["skipped_records"] == 2,
              str(node["statistics"]))
        check("中间态未写出音文评分文件",
              node["statistics"]["audio_text_file_written"] is False)
        check("中间态统计守恒", node["statistics"]["conservation_ok"] is True)

        # 3) 阶段末：outputFile 文件对象 + 阶段血缘数组
        status, body, _ = _request("%s/predict" % base_url, method="POST",
                                   payload={"uniqueId": "smoke_a", "isLastOperator": True})
        check("阶段末返回 200/code=0", status == 200 and body["code"] == 0)
        check("阶段末 data 恰为 outputFile+lineageFile",
              set(body["data"].keys()) == {"outputFile", "lineageFile"},
              str(sorted(body["data"].keys())))
        file_object = body["data"]["outputFile"]
        check("阶段末对象格式 crossmodal-files-v1",
              file_object["format"] == "crossmodal-files-v1")
        check("阶段末入口为 13_passthrough.jsonl",
              file_object["entrypoint"] == audio_text.MAIN_NAME_DISABLED)
        paths = [item["path"] for item in file_object["files"]]
        for name in (audio_text.MAIN_NAME_DISABLED, audio_text.REJECTS_NAME,
                     audio_text.PENDING_NAME, audio_text.SAMPLE_QUARANTINE_NAME, "manifest.json"):
            check("阶段末包含 %s" % name, name in paths)
        check("阶段末血缘为数组且无临时 outputFile",
              isinstance(body["data"]["lineageFile"], list)
              and all("outputFile" not in item for item in body["data"]["lineageFile"]))

        # 4) 启用路线（显式降级，不伪造模型分数）
        status, _, _ = upload(base_url, "smoke_b", bundle_zip())
        config = {"enabled": True, "allow_degraded": True,
                  "model_root": str(output_root / "absent_models")}
        status, body, _ = _request("%s/predict" % base_url, method="POST",
                                   payload={"uniqueId": "smoke_b", "isLastOperator": True,
                                            "config": config})
        check("降级路线返回 200/code=0", status == 200 and body["code"] == 0, "status=%s" % status)
        stats = body["data"]["lineageFile"][-1]["statistics"]
        check("降级路线状态为 degraded", body["data"]["lineageFile"][-1]["status"] == "degraded")
        check("降级路线未产生任何模型分数",
              stats["scored_records"] == 0 and stats["models_available"] is False,
              str(stats))

        # 5) 严格路线（不允许降级）必须显式失败
        upload(base_url, "smoke_c", bundle_zip())
        strict = {"enabled": True, "allow_degraded": False,
                  "model_root": str(output_root / "absent_models")}
        status, body, _ = _request("%s/predict" % base_url, method="POST",
                                   payload={"uniqueId": "smoke_c", "isLastOperator": False,
                                            "config": strict})
        check("严格路线依赖缺失返回 400/code=1", status == 400 and body["code"] == 1,
              "status=%s msg=%s" % (status, body.get("msg") if body else None))

        # 6) 任务隔离
        upload(base_url, "smoke_iso", bundle_zip([fixtures.make_pair(
            "pair-iso", transcript="今天天气很好", description="今天天气很好")]))
        status, body, _ = _request("%s/predict" % base_url, method="POST",
                                   payload={"uniqueId": "smoke_iso", "isLastOperator": True})
        entry = [item for item in body["data"]["outputFile"]["files"]
                 if item["path"] == audio_text.MAIN_NAME_DISABLED][0]
        rows = [json.loads(line) for line in entry["content"].splitlines() if line.strip()]
        check("两个 uniqueId 互不污染", rows[0]["pair_id"] == "pair-iso", str(rows[0].get("pair_id")))

        # 7) 请求校验与路径安全
        status, body, _ = _request("%s/predict" % base_url, method="POST",
                                   payload={"uniqueId": "smoke_a", "isLastOperator": "false"})
        check("isLastOperator 非严格布尔 → 422", status == 422 and body["code"] == 1,
              "status=%s" % status)
        status, body, _ = _request("%s/predict" % base_url, method="POST",
                                   payload={"uniqueId": "../escape", "isLastOperator": False})
        check("非法 uniqueId → 422", status == 422 and body["code"] == 1, "status=%s" % status)
        outside = output_root / "outside.jsonl"
        outside.write_text("{}\n", encoding="utf-8")
        status, body, _ = _request("%s/predict" % base_url, method="POST",
                                   payload={"uniqueId": "smoke_a", "isLastOperator": False,
                                            "inputFile": str(outside)})
        check("任务外 inputFile → 400", status == 400 and body["code"] == 1, "status=%s" % status)
        status, _, _ = _request("%s/downloadFile?uniqueId=smoke_a" % base_url)
        check("/downloadFile 未注册 → 404", status == 404, "status=%s" % status)
        slip = fixtures.zip_bytes({"demo.jsonl": b"{}\n", "../evil.jsonl": b"{}\n"})
        status, body, _ = upload(base_url, "smoke_slip", slip)
        check("zip slip → code=1", body is not None and body["code"] == 1)
        check("zip slip 未落盘", not (output_root / "evil.jsonl").exists())

        # 8) removeFile 幂等且不影响其他任务
        status, body, _ = _request("%s/removeFile?uniqueId=smoke_b" % base_url, method="POST")
        first_ok = status == 200 and body["code"] == 0 and not (output_root / "smoke_b").exists()
        status, body, _ = _request("%s/removeFile?uniqueId=smoke_b" % base_url, method="POST")
        second_ok = status == 200 and body["code"] == 0
        check("removeFile 删除并幂等", first_ok and second_ok)
        check("removeFile 不影响其他任务", (output_root / "smoke_a").exists())
    finally:
        server.terminate()
        try:
            server.wait(timeout=20)
        except subprocess.TimeoutExpired:
            server.kill()
        shutil.rmtree(output_root, ignore_errors=True)

    passed = sum(1 for _name, ok, _detail in RESULTS if ok)
    total = len(RESULTS)
    print("\nAPI smoke 结果：%d/%d 通过" % (passed, total))
    for name, ok, detail in RESULTS:
        if not ok:
            print("  - 失败：%s %s" % (name, detail))
    return 0 if passed == total else 1


if __name__ == "__main__":
    raise SystemExit(main())
