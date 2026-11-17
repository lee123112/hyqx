"""真实 HTTP 冒烟：拉起 uvicorn，跑通 /uploadFile → /predict（中间态与阶段末）→ /removeFile。

与 ``test_api.py`` 的区别：这里走真实网络与真实 ASGI 服务器进程，验证响应体大小、
multipart 上传、uvicorn 启动与端口可用性。跨平台（Windows/Linux）可重复运行。

用法：``python tests/smoke_api.py``（成功退出码 0，失败非 0）
"""
from __future__ import annotations

import json
import os
import shutil
import socket
import subprocess
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path

BASE_DIR = Path(__file__).resolve().parents[1]
if str(BASE_DIR) not in sys.path:
    sys.path.insert(0, str(BASE_DIR))

from tests import fixtures  # noqa: E402
from utils import transport  # noqa: E402

OUTPUT_ROOT = BASE_DIR / "output_smoke"


def free_port() -> int:
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return int(sock.getsockname()[1])


def request(url: str, *, payload: dict | None = None, method: str | None = None,
            timeout: float = 60.0):
    data = None
    headers = {}
    if payload is not None:
        data = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        headers["Content-Type"] = "application/json"
    if method is None:
        method = "POST" if payload is not None else "GET"
    req = urllib.request.Request(url, data=data, method=method, headers=headers)
    with urllib.request.urlopen(req, timeout=timeout) as response:
        return response.status, json.loads(response.read().decode("utf-8"))


def upload(url: str, unique_id: str, filename: str, payload: bytes):
    boundary = "----smoke15boundary"
    body = b"".join([
        ("--%s\r\n" % boundary).encode("ascii"),
        ('Content-Disposition: form-data; name="file"; filename="%s"\r\n' % filename)
        .encode("utf-8"),
        b"Content-Type: application/octet-stream\r\n\r\n",
        payload,
        ("\r\n--%s--\r\n" % boundary).encode("ascii"),
    ])
    req = urllib.request.Request(
        "%s/uploadFile?uniqueId=%s" % (url, urllib.parse.quote(unique_id)),
        data=body, method="POST",
        headers={"Content-Type": "multipart/form-data; boundary=%s" % boundary})
    with urllib.request.urlopen(req, timeout=60.0) as response:
        return response.status, json.loads(response.read().decode("utf-8"))


def wait_ready(url: str, process: subprocess.Popen, log_path: Path,
               timeout: float = 60.0) -> None:
    deadline = time.time() + timeout
    while time.time() < deadline:
        if process.poll() is not None:
            output = log_path.read_text(encoding="utf-8", errors="replace") if log_path.is_file() else ""
            raise RuntimeError("uvicorn 提前退出：\n%s" % output[-2000:])
        try:
            with urllib.request.urlopen(url + "/openapi.json", timeout=2.0) as response:
                if response.status == 200:
                    return
        except (urllib.error.URLError, OSError):
            time.sleep(0.3)
    raise RuntimeError("uvicorn 在 %.0f 秒内未就绪" % timeout)


def check(condition: bool, message: str, checks: list) -> None:
    checks.append({"check": message, "ok": bool(condition)})
    if not condition:
        raise AssertionError(message)


def main() -> int:
    shutil.rmtree(OUTPUT_ROOT, ignore_errors=True)
    OUTPUT_ROOT.mkdir(parents=True, exist_ok=True)
    port = free_port()
    url = "http://127.0.0.1:%d" % port
    env = dict(os.environ)
    env["OUTPUT_DIR"] = str(OUTPUT_ROOT)
    env["PYTHONPATH"] = str(BASE_DIR)
    env.setdefault("PYTHONIOENCODING", "utf-8")

    # 子进程日志直接落盘，不走管道：避免宿主 shell 的本地编码影响输出读取
    log_path = OUTPUT_ROOT / "uvicorn.log"
    log_handle = open(log_path, "wb")
    process = subprocess.Popen(
        [sys.executable, "-m", "uvicorn", "app:app", "--host", "127.0.0.1",
         "--port", str(port), "--log-level", "warning"],
        cwd=str(BASE_DIR), env=env, stdout=log_handle, stderr=subprocess.STDOUT)

    checks: list = []
    summary: dict = {"baseUrl": url, "port": port}
    exit_code = 1
    try:
        wait_ready(url, process, log_path)
        check(True, "uvicorn 启动并就绪", checks)

        status, body = upload(url, "smoke_api_a", "bundle.zip", fixtures.bundle_zip())
        check(status == 200 and body["code"] == 0, "上传标准 zip 素材包", checks)
        check(set(body["data"]) == {"entrypoint", "files"}, "上传响应结构", checks)
        check(body["data"]["entrypoint"] == "demo.jsonl", "入口解析为 demo.jsonl", checks)

        nested = {
            "content/manifest.json": json.dumps(
                {"format": "crossmodal-manifest-v1", "entrypoint": "demo.jsonl",
                 "artifacts": [{"path": "demo.jsonl", "role": "main"}]}).encode("utf-8"),
            "content/demo.jsonl": (json.dumps(fixtures.make_record("nested"), ensure_ascii=False)
                                   + "\n").encode("utf-8"),
        }
        status, body = upload(url, "smoke_api_b", "nested.zip", fixtures.zip_bytes(nested))
        check(status == 200 and body["code"] == 0, "上传含 manifest 的嵌套 zip", checks)

        # 中间态：data 为 operator/status/outputFile/result（不再有 lineageFile）
        status, middle = request(url + "/predict", payload={
            "uniqueId": "smoke_api_a", "isLastOperator": False,
            "config": {"enabled": True}})
        check(status == 200 and middle["code"] == 0, "中间态 /predict 成功", checks)
        check(set(middle["data"]) == {"operator", "status", "outputFile", "result"},
              "中间态 data 结构（outputFile + result）", checks)
        result = middle["data"]["result"]
        check(bool(result.get("outputFile")), "中间态 result 含任务内 outputFile", checks)
        check(result["statistics"]["passed_records"] == 1, "统计通过数真实（1 条）", checks)
        check(result["statistics"]["failed_records"] == 1, "统计隔离数真实（1 条）", checks)
        check(result["statistics"]["conservation_ok"] is True, "统计守恒", checks)
        check(result["model"] is None, "无模型：model 显式为 null", checks)

        # 阶段末：data 同样返回文件内容对象 + result
        status, last = request(url + "/predict", payload={
            "uniqueId": "smoke_api_a", "isLastOperator": True,
            "config": {"enabled": True}})
        check(status == 200 and last["code"] == 0, "阶段末 /predict 成功", checks)
        check(set(last["data"]) == {"operator", "status", "outputFile", "result"},
              "阶段末 data 结构", checks)
        file_object = last["data"]["outputFile"]
        check(file_object["format"] == transport.FILE_OBJECT_FORMAT, "文件对象格式", checks)
        paths = {item["path"] for item in file_object["files"]}
        check("manifest.json" in paths, "文件对象含 manifest.json", checks)
        check(any(path.endswith("_sample_quarantine.jsonl") for path in paths),
              "文件对象含样本隔离台账", checks)

        # 阶段内续跑：把阶段末入口还原到本任务目录内，作为 inputFile 送回 /predict
        task_root = OUTPUT_ROOT / "smoke_api_a"
        restore_dir = task_root / "restored_prev"
        entry = transport.restore_file_object(file_object, restore_dir)
        relative = entry.relative_to(task_root).as_posix()
        status, chained = request(url + "/predict", payload={
            "uniqueId": "smoke_api_a", "isLastOperator": True, "inputFile": relative})
        check(status == 200 and chained["code"] == 0,
              "把阶段末入口作为 inputFile 继续执行", checks)
        check(chained["data"]["result"]["statistics"]["input_records"] == 1,
              "续跑输入为上一节点主产物（1 条）", checks)

        # 非法 uniqueId 与非法路径
        try:
            request(url + "/predict", payload={"uniqueId": "../x", "isLastOperator": False})
            check(False, "非法 uniqueId 应被拒绝", checks)
        except urllib.error.HTTPError as exc:
            check(exc.code == 422, "非法 uniqueId 返回 422", checks)

        try:
            request(url + "/predict", payload={
                "uniqueId": "smoke_api_a", "isLastOperator": False,
                "inputFile": "C:\\Windows\\win.ini"})
            check(False, "任务外 inputFile 应被拒绝", checks)
        except urllib.error.HTTPError as exc:
            check(exc.code == 400, "任务外 inputFile 返回 400", checks)

        # 不实现 /downloadFile
        try:
            request(url + "/downloadFile", payload={"uniqueId": "smoke_api_a"})
            check(False, "/downloadFile 不应存在", checks)
        except urllib.error.HTTPError as exc:
            check(exc.code == 404, "/downloadFile 返回 404", checks)

        status, removed = request(url + "/removeFile?uniqueId=smoke_api_a", method="POST")
        check(status == 200 and removed["data"]["removed"] is True, "清理任务目录", checks)
        status, again = request(url + "/removeFile?uniqueId=smoke_api_a", method="POST")
        check(status == 200 and again["data"]["removed"] is False, "重复清理幂等", checks)

        summary["checks"] = checks
        summary["passed"] = sum(1 for item in checks if item["ok"])
        summary["total"] = len(checks)
        exit_code = 0
    except Exception as exc:  # noqa: BLE001
        summary["error"] = "%s: %s" % (type(exc).__name__, exc)
        summary["checks"] = checks
        summary["passed"] = sum(1 for item in checks if item["ok"])
        summary["total"] = len(checks)
    finally:
        process.terminate()
        try:
            process.wait(timeout=15)
        except subprocess.TimeoutExpired:  # pragma: no cover
            process.kill()
        log_handle.close()

    print(json.dumps(summary, ensure_ascii=False, indent=2))
    return exit_code


if __name__ == "__main__":
    raise SystemExit(main())
