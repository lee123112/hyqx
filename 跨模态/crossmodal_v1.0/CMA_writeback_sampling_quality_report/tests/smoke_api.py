"""真实 uvicorn HTTP 冒烟（**不是 TestClient**）。

启动真实服务子进程 → httpx 走网络请求 → 覆盖：三接口、阶段首/中间/末三种形态、
两个 uniqueId 隔离、非法 uniqueId、跨任务 inputFile、非严格布尔 isLastOperator、
zip 目录穿越、`/downloadFile` 404、删除幂等。

用法::

    python tests/smoke_api.py [port]

服务端 stdout/stderr 落到临时日志文件，任何请求异常都会打印日志尾部便于定位。
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

import httpx

BASE_DIR = Path(__file__).resolve().parent.parent
if str(BASE_DIR) not in sys.path:
    sys.path.insert(0, str(BASE_DIR))

# 控制台可能是 GBK，统一按 UTF-8 输出并容错，避免冒烟脚本自身因编码中断
for _stream_name in ("stdout", "stderr"):
    _stream = getattr(sys, _stream_name)
    if hasattr(_stream, "reconfigure"):
        try:
            _stream.reconfigure(encoding="utf-8", errors="replace")
        except (ValueError, OSError):  # pragma: no cover - 非文本流时忽略
            pass

from tests.conftest import make_record  # noqa: E402

DEFAULT_PORT = int(os.environ.get("SMOKE_PORT", "8124"))
REQUEST_TIMEOUT = float(os.environ.get("SMOKE_TIMEOUT", "60"))
CHECKS: list = []
SERVER_LOG: list = []


def check(name: str, condition: bool, detail: str = "") -> None:
    CHECKS.append((name, bool(condition), detail))
    line = "[%s] %s" % ("PASS" if condition else "FAIL", name)
    if detail and not condition:
        line += " —— %s" % detail
    print(line, flush=True)


def dump_server_log(tail: int = 40) -> None:
    if not SERVER_LOG:
        return
    print("---- server log tail ----", flush=True)
    for line in SERVER_LOG[-tail:]:
        print("  | %s" % line, flush=True)


def request(method: str, url: str, **kwargs):
    """发起请求；异常时返回 None 并打印服务端日志便于定位。"""
    kwargs.setdefault("timeout", REQUEST_TIMEOUT)
    try:
        return httpx.request(method, url, **kwargs)
    except Exception as exc:  # noqa: BLE001
        print("!! 请求异常 %s %s -> %s: %s"
              % (method, url, type(exc).__name__, exc), flush=True)
        dump_server_log()
        return None


def body_of(response, default=None):
    if response is None:
        return default if default is not None else {}
    try:
        return response.json()
    except Exception:  # noqa: BLE001
        return default if default is not None else {}


def free_port(preferred: int) -> int:
    for candidate in (preferred, 0):
        with socket.socket() as probe:
            try:
                probe.bind(("127.0.0.1", candidate))
            except OSError:
                continue
            return probe.getsockname()[1]
    raise RuntimeError("无法分配端口")


def build_bundle(rows, report: bool = True) -> bytes:
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w") as archive:
        archive.writestr("24_candidate_pool.jsonl",
                         "\n".join(json.dumps(item, ensure_ascii=False)
                                   for item in rows) + "\n")
        if report:
            archive.writestr("20_quality_report.json", json.dumps({
                "format": "crossmodal-quality-report-v1",
                "operator": "alignment_qc",
                "status": "waiting_writeback",
                "writeback": {"required": True, "writeback_by": "operator_24",
                              "writeback_completed": False,
                              "version_state": "pending_release"},
                "checks": {"text_nonempty": {"ok": len(rows)}},
            }, ensure_ascii=False))
    return buffer.getvalue()


def main() -> int:
    port = free_port(DEFAULT_PORT)
    work = Path(tempfile.mkdtemp(prefix="op24_smoke_"))
    output_dir = work / "out"
    server_log_path = work / "server.log"
    env = dict(os.environ)
    env["OUTPUT_DIR"] = str(output_dir)
    env["PYTHONPATH"] = str(BASE_DIR)
    env["PYTHONIOENCODING"] = "utf-8"

    log_stream = open(server_log_path, "w", encoding="utf-8", errors="replace")
    process = subprocess.Popen(
        [sys.executable, "-m", "uvicorn", "app:app", "--host", "127.0.0.1",
         "--port", str(port), "--log-level", "warning"],
        cwd=str(BASE_DIR), env=env, stdout=log_stream, stderr=subprocess.STDOUT)
    base = "http://127.0.0.1:%d" % port
    try:
        ready = False
        for _ in range(60):
            if process.poll() is not None:
                break
            try:
                if httpx.get(base + "/health", timeout=1.0).status_code == 200:
                    ready = True
                    break
            except Exception:  # noqa: BLE001 - 启动期连不上是正常的
                time.sleep(0.5)
        check("服务启动并 /health 就绪", ready)
        if not ready:
            SERVER_LOG.extend(server_log_path.read_text(
                encoding="utf-8", errors="replace").splitlines())
            dump_server_log()
            return 1

        health = body_of(request("GET", base + "/health"))
        check("health 身份正确",
              health.get("operator") == "spot_check_writeback"
              and health.get("operatorNumber") == 24)

        pass_rows = [make_record("smoke-pass-%02d" % index, verdict="pass")
                     for index in range(10)]
        fail_rows = [make_record("smoke-fail-%02d" % index, verdict="not_aligned")
                     for index in range(6)]
        pending_rows = [make_record("smoke-pend-%02d" % index) for index in range(10)]

        for unique_id, rows in (("smoke_a", pass_rows), ("smoke_b", fail_rows),
                                ("smoke_c", pending_rows)):
            response = body_of(request(
                "POST", "%s/uploadFile?uniqueId=%s" % (base, unique_id),
                files={"file": ("bundle.zip", build_bundle(rows), "application/zip")}))
            check("上传 %s" % unique_id, response.get("code") == 0, str(response)[:200])

        middle = body_of(request("POST", base + "/predict", json={
            "uniqueId": "smoke_a", "isLastOperator": False}))
        check("中间形态 code=0", middle.get("code") == 0, str(middle)[:300])
        check("中间 data 仅含 lineageFile", set(middle.get("data", {})) == {"lineageFile"})
        node = middle.get("data", {}).get("lineageFile", {})
        check("中间血缘含临时 outputFile", bool(node.get("outputFile")))
        check("中间血缘 operator/stage/编号正确",
              node.get("operator") == "spot_check_writeback"
              and node.get("stageId") == "S5"
              and node.get("formalOperatorNumber") == 24)
        check("中间统计真实（output=10）",
              node.get("statistics", {}).get("output") == 10,
              str(node.get("statistics")))

        last = body_of(request("POST", base + "/predict", json={
            "uniqueId": "smoke_a", "isLastOperator": True}))
        check("阶段末 code=0", last.get("code") == 0, str(last)[:300])
        check("阶段末 data 含 outputFile+lineageFile",
              set(last.get("data", {})) == {"outputFile", "lineageFile"})
        file_object = last.get("data", {}).get("outputFile", {})
        check("阶段末 outputFile 是文件内容对象",
              file_object.get("format") == "crossmodal-files-v1")
        check("阶段末入口为 24_spot_check.jsonl",
              file_object.get("entrypoint") == "24_spot_check.jsonl")
        names = [item["path"] for item in file_object.get("files", [])]
        check("阶段末文件对象含质量报告", "24_quality_report.json" in names, str(names))
        check("阶段末文件对象含抽检样本清单", "24_sampled.jsonl" in names, str(names))
        lineage = last.get("data", {}).get("lineageFile")
        check("阶段末 lineageFile 是数组", isinstance(lineage, list))
        check("阶段末血缘已剔除临时 outputFile",
              all("outputFile" not in item for item in (lineage or [])))

        chained = body_of(request("POST", base + "/predict", json={
            "uniqueId": "smoke_a", "isLastOperator": False,
            "inputFile": node.get("outputFile")}))
        check("阶段内串联（传上一临时 outputFile）", chained.get("code") == 0,
              str(chained)[:200])

        other = body_of(request("POST", base + "/predict", json={
            "uniqueId": "smoke_b", "isLastOperator": True}))
        other_node = (other.get("data", {}).get("lineageFile") or [{}])[0]
        check("两个 uniqueId 互不污染",
              other_node.get("uniqueId") == "smoke_b"
              and other_node.get("statistics", {}).get("sample_quarantined") == 1,
              str(other_node.get("statistics")))
        check("判错样本取消自动对齐",
              other_node.get("statistics", {}).get("auto_alignment_cancelled") == 1)

        pending = body_of(request("POST", base + "/predict", json={
            "uniqueId": "smoke_c", "isLastOperator": True}))
        pending_node = (pending.get("data", {}).get("lineageFile") or [{}])[0]
        pending_stats = pending_node.get("statistics", {})
        check("无人工结论时保持待回写",
              pending_stats.get("writeback", {}).get("completed") is False
              and pending_node.get("status") == "waiting_manual",
              str(pending_stats.get("writeback")))
        check("无人工结论时守恒校验通过",
              pending_stats.get("conservation_ok") is True,
              str(pending_stats.get("conservation_failures")))
        report_item = [item for item in pending.get("data", {})
                       .get("outputFile", {}).get("files", [])
                       if item["path"] == "24_quality_report.json"]
        report = json.loads(report_item[0]["content"]) if report_item else {}
        check("报告状态保持 waiting_writeback",
              report.get("status") == "waiting_writeback"
              and report.get("writeback", {}).get("version_state") == "pending_release",
              str(report.get("status")))

        bad_uid = body_of(request(
            "POST", "%s/uploadFile?uniqueId=%s" % (base, "..%2Fescape"),
            files={"file": ("a.jsonl", b"{}\n", "application/json")}))
        check("非法 uniqueId 被拒", bad_uid.get("code") == 1, str(bad_uid))

        not_found = request("GET", base + "/downloadFile?uniqueId=x")
        check("/downloadFile 未实现（404）",
              not_found is not None and not_found.status_code == 404)
        check("/downloadFile 仍是统一信封",
              body_of(not_found).get("code") == 1)

        bad_bool = request("POST", base + "/predict", json={
            "uniqueId": "smoke_a", "isLastOperator": "false"})
        check("非严格布尔 isLastOperator 被拒",
              bad_bool is not None and bad_bool.status_code == 422)

        foreign = output_dir / "smoke_b" / "input" / "24_candidate_pool.jsonl"
        cross = body_of(request("POST", base + "/predict", json={
            "uniqueId": "smoke_c", "isLastOperator": False,
            "inputFile": str(foreign)}))
        check("跨任务 inputFile 被拒", cross.get("code") == 1, str(cross)[:200])

        slip_buffer = io.BytesIO()
        with zipfile.ZipFile(slip_buffer, "w") as archive:
            archive.writestr("../escape.jsonl", "{}\n")
        slip = body_of(request(
            "POST", "%s/uploadFile?uniqueId=smoke_slip" % base,
            files={"file": ("b.zip", slip_buffer.getvalue(), "application/zip")}))
        check("zip 目录穿越被拒", slip.get("code") == 1, str(slip)[:200])
        check("zip 穿越未落盘", not (output_dir / "escape.jsonl").exists())

        first_remove = body_of(request("POST", base + "/removeFile?uniqueId=smoke_a"))
        second_remove = body_of(request("POST", base + "/removeFile?uniqueId=smoke_a"))
        check("removeFile 幂等", first_remove.get("code") == 0
              and second_remove.get("code") == 0, str(second_remove))
        check("removeFile 未误删其他任务", (output_dir / "smoke_b").is_dir())

        routes = set(body_of(request("GET", base + "/openapi.json"), {}).get("paths", {}))
        check("仅暴露三接口 + /health",
              routes == {"/uploadFile", "/predict", "/removeFile", "/health"}, str(routes))
    finally:
        process.terminate()
        try:
            process.wait(timeout=10)
        except subprocess.TimeoutExpired:  # pragma: no cover
            process.kill()
        log_stream.close()
        SERVER_LOG.clear()
        SERVER_LOG.extend(server_log_path.read_text(
            encoding="utf-8", errors="replace").splitlines())
        shutil.rmtree(work, ignore_errors=True)

    passed = sum(1 for _name, ok, _detail in CHECKS if ok)
    failed = len(CHECKS) - passed
    print("\n共 %d 项检查，通过 %d 项，失败 %d 项" % (len(CHECKS), passed, failed),
          flush=True)
    if failed:
        dump_server_log()
    return 0 if failed == 0 else 1


if __name__ == "__main__":
    sys.exit(main())
