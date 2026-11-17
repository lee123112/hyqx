"""真实服务 API smoke：启动 uvicorn 子进程，用 HTTP 打三个接口。

覆盖提示词要求的三件事：
1. 两个 ``uniqueId`` 的任务隔离；
2. 非法路径拒绝（``inputFile`` 越界、``uniqueId`` 非法、zip slip）；
3. 阶段中间态与阶段末两种响应形态。

这不是 pytest 用例（文件名不以 ``test_`` 开头，不会被 pytest 收集），
用于交付前的手工/CI smoke：

    python tests/smoke_api.py
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
import urllib.parse
import urllib.request
import uuid
import zipfile
from pathlib import Path

BASE_DIR = Path(__file__).resolve().parents[1]
HOST = "127.0.0.1"
PORT = 8111
BASE_URL = "http://%s:%d" % (HOST, PORT)

CHECKS = []


def check(name: str, condition: bool, detail: str = "") -> None:
    CHECKS.append((name, bool(condition), detail))
    print("%s %s%s" % ("PASS" if condition else "FAIL", name,
                       "" if condition else " -> %s" % detail))


def _request(method: str, path: str, payload=None, raw: bytes = None,
             content_type: str = "application/json"):
    url = BASE_URL + path
    body = raw if raw is not None else (json.dumps(payload).encode("utf-8")
                                        if payload is not None else None)
    request = urllib.request.Request(url, data=body, method=method)
    if body is not None:
        request.add_header("Content-Type", content_type)
    try:
        with urllib.request.urlopen(request, timeout=120) as response:
            return response.status, json.loads(response.read().decode("utf-8"))
    except urllib.error.HTTPError as exc:
        return exc.code, json.loads(exc.read().decode("utf-8"))


def _upload(unique_id: str, bundle: Path):
    boundary = "----inputadmission%s" % uuid.uuid4().hex
    parts = []
    parts.append(("--%s\r\n" % boundary).encode("ascii"))
    parts.append(b'Content-Disposition: form-data; name="file"; filename="bundle.zip"\r\n')
    parts.append(b"Content-Type: application/zip\r\n\r\n")
    parts.append(bundle.read_bytes())
    parts.append(("\r\n--%s--\r\n" % boundary).encode("ascii"))
    return _request("POST", "/uploadFile?uniqueId=" + urllib.parse.quote(unique_id),
                    raw=b"".join(parts),
                    content_type="multipart/form-data; boundary=%s" % boundary)


def _free_port() -> int:
    sock = socket.socket()
    sock.bind((HOST, 0))
    port = sock.getsockname()[1]
    sock.close()
    return port


def _build_bundle(work: Path) -> Path:
    bundle = work / "bundle.zip"
    sample = BASE_DIR / "dataset" / "sample"
    with zipfile.ZipFile(bundle, "w", zipfile.ZIP_DEFLATED) as archive:
        for path in sorted(sample.rglob("*")):
            if path.is_file():
                archive.write(path, path.relative_to(sample).as_posix())
    return bundle


def main() -> int:
    global PORT, BASE_URL
    PORT = _free_port()
    BASE_URL = "http://%s:%d" % (HOST, PORT)
    work = Path(tempfile.mkdtemp(prefix="input_admission_smoke_"))
    output_root = work / "output"
    env = dict(os.environ)
    env["OUTPUT_DIR"] = str(output_root)
    env["PYTHONIOENCODING"] = "utf-8"
    log_path = work / "server.log"
    server = None
    try:
        with open(log_path, "wb") as log:
            server = subprocess.Popen(
                [sys.executable, "-m", "uvicorn", "app:app", "--host", HOST,
                 "--port", str(PORT), "--log-level", "warning"],
                cwd=str(BASE_DIR), env=env, stdout=log, stderr=subprocess.STDOUT)
        for _ in range(60):
            try:
                status, _body = _request("GET", "/openapi.json")
                if status == 200:
                    break
            except Exception:  # noqa: BLE001 - 服务未就绪时继续等待
                pass
            if server.poll() is not None:
                raise RuntimeError("uvicorn 提前退出，见 %s" % log_path)
            time.sleep(0.5)
        else:
            raise RuntimeError("uvicorn 启动超时")

        bundle = _build_bundle(work)

        # 1) 上传 + 阶段中间态
        status, body = _upload("smoke_a", bundle)
        check("uploadFile 返回 200/code=0", status == 200 and body["code"] == 0, repr(body))
        check("uploadFile 返回入口名", body.get("data", {}).get("entrypoint") == "demo.jsonl",
              repr(body.get("data")))

        status, body = _request("POST", "/predict",
                                {"uniqueId": "smoke_a", "isLastOperator": False})
        check("中间态 200/code=0", status == 200 and body["code"] == 0, repr(body)[:200])
        node = body.get("data", {}).get("lineageFile")
        check("中间态 data 只有 lineageFile",
              set(body.get("data", {})) == {"lineageFile"} and isinstance(node, dict),
              repr(body.get("data"))[:200])
        check("中间态 lineageFile 是本节点对象且统计真实",
              isinstance(node, dict) and node["statistics"]["admitted"] == 4
              and node["statistics"]["input"] == 16, repr(node)[:200])
        check("中间态含临时 outputFile",
              str(node.get("outputFile", "")).endswith("10_admitted.jsonl"),
              repr(node.get("outputFile")))

        # 2) 阶段末形态
        status, body = _request("POST", "/predict",
                                {"uniqueId": "smoke_a", "isLastOperator": True})
        data = body.get("data", {})
        check("阶段末 data == {outputFile, lineageFile}",
              status == 200 and body["code"] == 0
              and set(data) == {"outputFile", "lineageFile"}, repr(data)[:200])
        check("阶段末 outputFile 是 crossmodal-files-v1 文件内容对象",
              data.get("outputFile", {}).get("format") == "crossmodal-files-v1"
              and data["outputFile"]["entrypoint"] == "10_admitted.jsonl",
              repr(data.get("outputFile", {}))[:200])
        check("阶段末血缘是数组且不含临时 outputFile",
              isinstance(data.get("lineageFile"), list)
              and len(data["lineageFile"]) >= 1
              and all("outputFile" not in item for item in data["lineageFile"]),
              repr(data.get("lineageFile"))[:200])
        check("阶段末对象带齐四类清单与媒体",
              {"10_admitted.jsonl", "10_return.jsonl", "10_origin_quarantine.jsonl",
               "10_sample_quarantine.jsonl"} <= {item["path"]
                                                 for item in data["outputFile"]["files"]},
              repr([item["path"] for item in data["outputFile"]["files"]])[:300])

        # 3) 第二个 uniqueId：任务隔离
        status, body = _upload("smoke_b", bundle)
        check("第二任务上传成功", status == 200 and body["code"] == 0, repr(body))
        status, body = _request("POST", "/predict",
                                {"uniqueId": "smoke_b", "isLastOperator": False})
        node_b = body.get("data", {}).get("lineageFile", {})
        check("第二任务血缘 uniqueId 正确且与第一任务目录不同",
              node_b.get("uniqueId") == "smoke_b"
              and Path(node_b["outputFile"]).parent != Path(node["outputFile"]).parent,
              "%s vs %s" % (node_b.get("outputFile"), node.get("outputFile")))
        check("两任务输出互不污染",
              "s-ok-1" in Path(node["outputFile"]).read_text(encoding="utf-8")
              and output_root.joinpath("smoke_b").is_dir()
              and output_root.joinpath("smoke_a").is_dir(), "任务目录缺失")

        # 4) 非法路径与非法请求
        outside = work / "outside.jsonl"
        outside.write_text("{}", encoding="utf-8")
        status, body = _request("POST", "/predict",
                                {"uniqueId": "smoke_a", "isLastOperator": False,
                                 "inputFile": str(outside)})
        check("inputFile 出任务目录 → 400/code=1",
              status == 400 and body["code"] == 1, "%s %s" % (status, body))
        status, body = _request("POST", "/predict",
                                {"uniqueId": "smoke_a", "isLastOperator": False,
                                 "inputFile": "../smoke_b/input/demo.jsonl"})
        check("inputFile 目录穿越 → 400/code=1",
              status == 400 and body["code"] == 1, "%s %s" % (status, body))
        status, body = _upload("../escape", bundle)
        check("非法 uniqueId → 422/code=1",
              status == 422 and body["code"] == 1, "%s %s" % (status, body))
        status, body = _request("POST", "/predict",
                                {"uniqueId": "smoke_a", "isLastOperator": "false"})
        check("isLastOperator 非严格 bool → 422/code=1",
              status == 422 and body["code"] == 1, "%s %s" % (status, body))
        slip = work / "slip.zip"
        with zipfile.ZipFile(slip, "w") as archive:
            archive.writestr("demo.jsonl", '{"sample_id": "x"}\n')
            archive.writestr("../evil.jsonl", '{"sample_id": "evil"}\n')
        status, body = _upload("smoke_slip", slip)
        check("zip slip → code=1 且未落盘",
              body["code"] == 1 and not (work / "evil.jsonl").exists(), repr(body))
        status, body = _request("GET", "/downloadFile")
        check("/downloadFile 不存在 → 404/code=1",
              status == 404 and body["code"] == 1, "%s %s" % (status, body))

        # 5) 清理幂等
        status, body = _request("POST", "/removeFile?uniqueId=smoke_b")
        removed_once = body["code"] == 0 and not output_root.joinpath("smoke_b").exists()
        status, body = _request("POST", "/removeFile?uniqueId=smoke_b")
        check("removeFile 删除并幂等",
              removed_once and body["code"] == 0, "%s %s" % (status, body))
        check("另一任务目录未被误删", output_root.joinpath("smoke_a").is_dir(), "smoke_a 丢失")
    finally:
        if server is not None and server.poll() is None:
            server.terminate()
            try:
                server.wait(timeout=15)
            except subprocess.TimeoutExpired:  # pragma: no cover
                server.kill()
        shutil.rmtree(work, ignore_errors=True)

    failed = [name for name, ok, _ in CHECKS if not ok]
    print("\nAPI smoke：%d/%d 项通过" % (len(CHECKS) - len(failed), len(CHECKS)))
    if failed:
        print("失败项：%s" % ", ".join(failed))
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
