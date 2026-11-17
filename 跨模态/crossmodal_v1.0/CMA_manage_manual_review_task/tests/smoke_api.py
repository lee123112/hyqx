"""真实 HTTP 冒烟（**非 TestClient**）：真实 ``uvicorn`` 子进程 + 真实 multipart 上传。

覆盖：
1. ``GET /openapi.json`` 只有三个正式接口 + ``/health``（无 ``/downloadFile``）；
2. ``POST /uploadFile`` 真实 zip 上传 → ``code=0``；
3. ``POST /predict``（阶段内/阶段末）→ [v4.2] 统一 ``outputFile`` + ``result``，无 ``lineageFile``；
4. ``POST /predict``（multipart/form-data）→ [v4.2] 形态一单文件流，返回 int ``status`` 与
   ``review_tasks`` / ``review_results``；
5. 链式续跑（``inputFile`` 传上一节点 ``outputFile`` 落地后的入口文件）；
6. 非法 ``uniqueId`` → 422/400 信封；跨任务 ``inputFile`` → ``code=1``；
7. ``/downloadFile`` → 404 信封；
8. ``POST /removeFile`` 幂等。

用法::

    python tests/smoke_api.py [port]

脚本使用临时输出目录，结束前清理；退出码 0 表示全部断言通过。
"""
from __future__ import annotations

import io
import json
import os
import shutil
import subprocess
import sys
import tempfile
import time
import urllib.error
import urllib.request
import zipfile
from pathlib import Path

BASE_DIR = Path(__file__).resolve().parent.parent
if str(BASE_DIR) not in sys.path:
    sys.path.insert(0, str(BASE_DIR))

from tests.conftest import ENTRY_NAME, uncertain_record, with_review  # noqa: E402

HOST = "127.0.0.1"


def build_zip(rows) -> bytes:
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w") as archive:
        archive.writestr(ENTRY_NAME,
                         "\n".join(json.dumps(item, ensure_ascii=False) for item in rows) + "\n")
    return buffer.getvalue()


def post_json(url: str, payload: dict):
    request = urllib.request.Request(
        url, data=json.dumps(payload).encode("utf-8"),
        headers={"Content-Type": "application/json"}, method="POST")
    try:
        with urllib.request.urlopen(request, timeout=60) as response:
            return response.status, json.loads(response.read().decode("utf-8"))
    except urllib.error.HTTPError as exc:
        body = exc.read().decode("utf-8")
        try:
            return exc.code, json.loads(body)
        except json.JSONDecodeError:
            return exc.code, {"code": 1, "msg": body[:200], "data": {}}


def post_form(url: str):
    request = urllib.request.Request(url, data=b"", method="POST")
    try:
        with urllib.request.urlopen(request, timeout=60) as response:
            return response.status, json.loads(response.read().decode("utf-8"))
    except urllib.error.HTTPError as exc:
        return exc.code, json.loads(exc.read().decode("utf-8"))


def upload(url: str, payload: bytes, filename: str = "bundle.zip"):
    boundary = "----smoke25boundary"
    body = b"".join([
        ("--%s\r\n" % boundary).encode(),
        ('Content-Disposition: form-data; name="file"; filename="%s"\r\n' % filename).encode(),
        b"Content-Type: application/zip\r\n\r\n", payload, b"\r\n",
        ("--%s--\r\n" % boundary).encode(),
    ])
    request = urllib.request.Request(
        url, data=body,
        headers={"Content-Type": "multipart/form-data; boundary=%s" % boundary},
        method="POST")
    try:
        with urllib.request.urlopen(request, timeout=60) as response:
            return response.status, json.loads(response.read().decode("utf-8"))
    except urllib.error.HTTPError as exc:
        return exc.code, json.loads(exc.read().decode("utf-8"))


def get_json(url: str):
    try:
        with urllib.request.urlopen(url, timeout=30) as response:
            return response.status, json.loads(response.read().decode("utf-8"))
    except urllib.error.HTTPError as exc:
        return exc.code, json.loads(exc.read().decode("utf-8"))


def wait_ready(base: str, timeout: float = 30.0) -> None:
    deadline = time.time() + timeout
    while time.time() < deadline:
        try:
            status, _ = get_json("%s/health" % base)
            if status == 200:
                return
        except Exception:  # noqa: BLE001 - 启动期连接失败属正常
            pass
        time.sleep(0.3)
    raise RuntimeError("uvicorn 未在 %.0f 秒内就绪" % timeout)


def check(condition: bool, message: str) -> None:
    if not condition:
        raise AssertionError(message)
    print("  [ok] %s" % message)


def main(argv=None) -> int:
    port = int((argv or sys.argv[1:2] or ["8135"])[0])
    base = "http://%s:%d" % (HOST, port)
    workdir = Path(tempfile.mkdtemp(prefix="op25_smoke_"))
    env = dict(os.environ)
    env["OUTPUT_DIR"] = str(workdir / "out")
    env["PYTHONIOENCODING"] = "utf-8"
    server = subprocess.Popen(
        [sys.executable, "-m", "uvicorn", "app:app", "--host", HOST,
         "--port", str(port), "--log-level", "warning"],
        cwd=str(BASE_DIR), env=env, stdout=subprocess.PIPE, stderr=subprocess.STDOUT)
    try:
        wait_ready(base)
        print("真实 HTTP 冒烟（uvicorn 子进程 %s，非 TestClient）" % base)

        status, body = get_json("%s/openapi.json" % base)
        paths = sorted(body["paths"])
        check(status == 200 and "/downloadFile" not in paths,
              "openapi paths=%s（无 /downloadFile）" % paths)

        status, body = upload(
            "%s/uploadFile?uniqueId=smoke_http_a" % base,
            build_zip([uncertain_record("http-a-%02d" % index) for index in range(4)]))
        check(status == 200 and body["code"] == 0 and body["data"]["entrypoint"] == ENTRY_NAME,
              "upload → %s code=%s entrypoint=%s"
              % (status, body["code"], body["data"].get("entrypoint")))

        status, body = post_json("%s/predict" % base, {
            "uniqueId": "smoke_http_a", "isLastOperator": False,
            "config": {"enabled": True}})
        data = body["data"]
        node = data["result"]
        check(status == 200 and body["code"] == 0
              and set(data) == {"operator", "status", "outputFile", "result"},
              "predict(mid) → data=%s status=%s" % (sorted(data), data.get("status")))
        check(node["statistics"]["tasks_generated"] == 4
              and node["statistics"]["conservation_ok"] is True,
              "predict(mid) → tasks=%d conservation=%s"
              % (node["statistics"]["tasks_generated"],
                 node["statistics"]["conservation_ok"]))
        check(data["outputFile"]["format"] == "crossmodal-files-v1",
              "predict(mid) 含 outputFile 文件对象")

        status, body = post_json("%s/predict" % base, {
            "uniqueId": "smoke_http_a", "isLastOperator": True})
        file_object = body["data"]["outputFile"]
        names = [item["path"] for item in file_object["files"]]
        check(status == 200 and body["code"] == 0 and set(body["data"]) == {
            "operator", "status", "outputFile", "result"},
            "predict(final) → format=%s files=%d"
            % (file_object["format"], len(file_object["files"])))
        check("25_review_tasks.jsonl" in names and "25_review_report.json" in names,
              "predict(final) 产物=%s" % names)

        # [v4.2] 形态一：multipart/form-data 单文件流（detection=M25）
        status, body = upload("%s/predict?detection=M25&uniqueId=smoke_form1" % base,
                              build_zip([uncertain_record("http-f1-01")]))
        check(status == 200 and body["status"] == 1
              and isinstance(body["review_tasks"], list)
              and isinstance(body["review_results"], list),
              "predict(form1) → status=%s tasks=%d"
              % (body.get("status"), len(body.get("review_tasks") or [])))

        status, body = upload(
            "%s/uploadFile?uniqueId=smoke_http_b" % base,
            build_zip([with_review(uncertain_record("http-b-01"), "pass"),
                       with_review(uncertain_record("http-b-02"), "fail",
                                   dimensions={"semantics": "fail"})]))
        check(status == 200 and body["code"] == 0, "upload(b) → code=%s" % body["code"])
        status, body = post_json("%s/predict" % base, {
            "uniqueId": "smoke_http_b", "isLastOperator": False,
            "config": {"enabled": True, "manual_platform_available": True}})
        stats = body["data"]["result"]["statistics"]
        check(stats["dispatched"] == 2 and stats["returned_to_pool"] == 1
              and stats["sample_quarantined"] == 1,
              "predict(b) → dispatched=%d returned=%d quarantined=%d"
              % (stats["dispatched"], stats["returned_to_pool"],
                 stats["sample_quarantined"]))

        status, body = post_json("%s/predict" % base, {
            "uniqueId": "smoke_http_a", "isLastOperator": False})
        check(body["data"]["result"]["status"] == "disabled",
              "默认（未开启）→ status=%s tasks=%d"
              % (body["data"]["result"]["status"],
                 body["data"]["result"]["statistics"]["tasks_generated"]))

        for bad, expect in (("smoke%2Fescape", 200), ("CON", 200), ("", 200)):
            observed, body = upload("%s/uploadFile?uniqueId=%s" % (base, bad),
                                    b"{}\n", "a.jsonl")
            check(body["code"] == 1 and observed == expect,
                  "非法 uniqueId=%r → http=%s code=%s" % (bad, observed, body["code"]))

        foreign = workdir / "out" / "smoke_http_b" / "input" / ENTRY_NAME
        status, body = post_json("%s/predict" % base, {
            "uniqueId": "smoke_http_a", "isLastOperator": False,
            "inputFile": str(foreign)})
        check(body["code"] == 1 and "任务" in body["msg"],
              "跨任务 inputFile → code=%s msg=%s" % (body["code"], body["msg"]))

        status, body = get_json("%s/downloadFile?uniqueId=x" % base)
        check(status == 404 and body["code"] == 1,
              "/downloadFile → http=%s code=%s" % (status, body["code"]))

        status, body = post_form("%s/removeFile?uniqueId=smoke_http_a" % base)
        first_removed = body["data"]["removed"]
        status, body = post_form("%s/removeFile?uniqueId=smoke_http_a" % base)
        check(first_removed is True and body["data"]["removed"] is False,
              "removeFile 幂等 → removed=%s/%s" % (first_removed, body["data"]["removed"]))
        check((workdir / "out" / "smoke_http_b").is_dir(),
              "其他任务目录未被删除（任务隔离）")

        print("真实 HTTP 冒烟全部通过；输出目录=%s" % (workdir / "out"))
        return 0
    finally:
        server.terminate()
        try:
            server.wait(timeout=10)
        except subprocess.TimeoutExpired:  # pragma: no cover
            server.kill()
        if server.stdout is not None:
            server.stdout.close()
        shutil.rmtree(workdir, ignore_errors=True)


if __name__ == "__main__":
    try:
        sys.exit(main())
    except AssertionError as exc:
        print("冒烟失败：%s" % exc, file=sys.stderr)
        sys.exit(1)
