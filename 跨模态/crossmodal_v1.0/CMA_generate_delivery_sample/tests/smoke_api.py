"""真实服务冒烟：启动 uvicorn 子进程 + httpx 走三接口全链路（非 TestClient）。

用法::

    python tests/smoke_api.py [port]

退出码 0 表示全部通过。不联网、不加载模型。
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
import zipfile
from pathlib import Path
from typing import Any, List, Tuple

BASE = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(BASE))
sys.path.insert(0, str(Path(__file__).resolve().parent))

import httpx  # noqa: E402

from conftest import (build_package, build_package_zip, default_records,  # noqa: E402
                      sample_record)

from utils import sampling, transport  # noqa: E402

CHECKS: List[Tuple[str, bool, str]] = []
ALL_ROUTES = ("image_text", "audio_text", "video_text")


def check(name: str, condition: Any, detail: str = "") -> None:
    CHECKS.append((name, bool(condition), detail))


def free_port(preferred: int) -> int:
    with socket.socket() as probe:
        try:
            probe.bind(("127.0.0.1", preferred))
            return preferred
        except OSError:
            probe.bind(("127.0.0.1", 0))
            return int(probe.getsockname()[1])


def wait_ready(base_url: str, timeout: float = 40.0) -> bool:
    deadline = time.time() + timeout
    while time.time() < deadline:
        try:
            response = httpx.get(base_url + "/health", timeout=2.0)
            if response.status_code == 200:
                return True
        except Exception:                                  # noqa: BLE001
            time.sleep(0.4)
    return False


def main(argv: List[str]) -> int:
    port = free_port(int(argv[1]) if len(argv) > 1 else 8126)
    base_url = "http://127.0.0.1:%d" % port
    workdir = Path(tempfile.mkdtemp(prefix="op26_smoke_"))
    output_root = workdir / "output"
    output_root.mkdir(parents=True, exist_ok=True)

    env = dict(os.environ)
    env.update({"OUTPUT_DIR": str(output_root), "PYTHONIOENCODING": "utf-8",
                "OPERATOR_LOG_LEVEL": "WARNING"})
    process = subprocess.Popen(
        [sys.executable, "-m", "uvicorn", "app:app", "--host", "127.0.0.1",
         "--port", str(port), "--log-level", "warning"],
        cwd=str(BASE), env=env, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
        text=True, encoding="utf-8")

    try:
        if not wait_ready(base_url):
            print("服务未就绪，输出如下：")
            print(process.stdout.read() if process.stdout else "")
            return 1

        client = httpx.Client(base_url=base_url, timeout=60.0)

        health = client.get("/health").json()
        check("health 信封与编号", health["code"] == 0
              and health["data"]["formal_operator_number"] == 26, str(health))
        check("health 声明三接口且无 /downloadFile",
              health["data"]["endpoints"] == ["/uploadFile", "/predict", "/removeFile"]
              and health["data"]["downloadFile"] == "not_registered")

        ready_zip = build_package_zip(workdir, "ready.zip", records=default_records(),
                                      routes=ALL_ROUTES, isolation_ids=("cm-9912",))
        pending_root = workdir / "pending_src"
        build_package(
            pending_root,
            records=[sample_record("cm-%03d" % index, deliverable=False)
                     for index in range(1, 4)],
            routes=("image_text",), version_state="pending_release", status="待发布")
        pending_zip = workdir / "pending.zip"
        with zipfile.ZipFile(pending_zip, "w", zipfile.ZIP_DEFLATED) as bundle:
            for path in sorted(Path(pending_root).rglob("*")):
                if path.is_file():
                    bundle.write(path, path.relative_to(Path(pending_root)).as_posix())

        with open(ready_zip, "rb") as handle:
            upload_one = client.post("/uploadFile", params={"uniqueId": "smoke_one"},
                                     files={"file": (ready_zip.name, handle,
                                                     "application/zip")})
        check("上传可交付包成功", upload_one.status_code == 200
              and upload_one.json()["code"] == 0, upload_one.text[:200])
        check("上传包入口识别为 #23 训练文件",
              upload_one.json()["data"]["entrypoint"] == "train/samples.jsonl")
        with open(pending_zip, "rb") as handle:
            upload_two = client.post("/uploadFile", params={"uniqueId": "smoke_two"},
                                     files={"file": (pending_zip.name, handle,
                                                     "application/zip")})
        check("上传待发布包成功", upload_two.status_code == 200
              and upload_two.json()["code"] == 0)

        mid = client.post("/predict", json={"uniqueId": "smoke_one",
                                            "isLastOperator": False}).json()
        check("[v4.2] 中间态 data 恰为 operator/status/outputFile/result",
              mid["code"] == 0
              and set(mid["data"]) == {"operator", "status", "outputFile", "result"},
              str(mid["data"].keys()))
        summary = mid["data"]["result"]
        check("中间态统计真实",
              summary["statistics"]["sampled_records"] == 16
              and summary["statistics"]["route_sampled"] == {"image_text": 10,
                                                             "audio_text": 4,
                                                             "video_text": 2}
              and summary["statistics"]["conservation_ok"] is True,
              str(summary["statistics"]))
        check("中间态含 outputFile 文件对象",
              mid["data"]["outputFile"]["format"] == transport.FILE_OBJECT_FORMAT)

        pending_mid = client.post("/predict", json={"uniqueId": "smoke_two",
                                                    "isLastOperator": False}).json()
        check("两条任务互相隔离",
              pending_mid["data"]["result"]["uniqueId"] == "smoke_two")
        check("待发布包不允许抽样（degraded）",
              pending_mid["data"]["result"]["status"] == "degraded"
              and pending_mid["data"]["result"]["statistics"]["sampled_records"] == 0
              and pending_mid["data"]["result"]["statistics"]["degrade_reason"]
              == "source_not_deliverable")

        final = client.post("/predict", json={"uniqueId": "smoke_one",
                                              "isLastOperator": True}).json()
        check("[v4.2] 阶段末 data 恰为 operator/status/outputFile/result",
              set(final["data"]) == {"operator", "status", "outputFile", "result"})
        file_object = final["data"]["outputFile"]
        check("阶段末文件对象为 crossmodal-files-v1",
              file_object["format"] == transport.FILE_OBJECT_FORMAT
              and file_object["entrypoint"] == sampling.MAIN_NAME)
        paths = [item["path"] for item in file_object["files"]]
        check("阶段末包含 Excel 样例清单与样例报告",
              sampling.EXCEL_NAME in paths and sampling.REPORT_NAME in paths
              and "manifest.json" in paths, str(paths))
        check("[v4.2] 响应不含 lineageFile 且未落 lineage.json",
              "lineageFile" not in final["data"]
              and not (output_root / "smoke_one" / "delivery_sample" / "run001"
                       / "lineage.json").exists())

        restored = workdir / "restored"
        entry_path = transport.restore_file_object(file_object, restored)
        rows = [json.loads(line) for line in
                entry_path.read_text(encoding="utf-8").strip().splitlines()]
        check("文件对象可逐字节恢复且行数一致", len(rows) == 16, str(len(rows)))
        openpyxl = __import__("openpyxl")
        book = openpyxl.load_workbook(restored / sampling.EXCEL_NAME)
        sheet = book["交付样例"]
        check("恢复后的 Excel 可被真实库打开且行数一致",
              book.sheetnames == ["交付样例", "样例报告"] and sheet.max_row == 17,
              str(book.sheetnames))
        excel_ids = {sheet.cell(row=index, column=2).value for index in range(2, 18)}
        check("Excel 与 JSONL 样本 ID 完全一致",
              excel_ids == {row["sample_id"] for row in rows})

        # [v4.2] 形态一：multipart/form-data 单文件流（detection=M26）
        with open(ready_zip, "rb") as handle:
            form1 = client.post("/predict",
                                params={"detection": "M26",
                                        "uniqueId": "smoke_form1"},
                                files={"file": (ready_zip.name, handle,
                                                "application/zip")},
                                data={"per_route_max": "2",
                                      "sampling_seed": "7"}).json()
        check("形态一 status=1 且每路线上限/固定种子生效",
              form1["status"] == 1
              and form1["routes"]["image_text"]["max_count"] == 2
              and form1["routes"]["image_text"]["sample_count"] == 2,
              str(form1.get("routes")))

        # 阶段内下一算子把上一响应 outputFile 还原到任务 input 后再传 inputFile
        chain_task = output_root / "smoke_one"
        chain_entry = transport.restore_file_object(mid["data"]["outputFile"],
                                                   chain_task / "input")
        relative = chain_entry.relative_to(chain_task).as_posix()
        chained = client.post("/predict", json={"uniqueId": "smoke_one",
                                                "isLastOperator": True,
                                                "inputFile": relative}).json()
        check("阶段内传入上一节点 outputFile 可复用", chained["code"] == 0,
              chained.get("msg", ""))
        chained_stats = chained["data"]["result"]["statistics"]
        chained_object = chained["data"]["outputFile"]
        chained_dir = workdir / "restored_chained"
        chained_entry = transport.restore_file_object(chained_object, chained_dir)
        chained_rows = [json.loads(line) for line in
                        chained_entry.read_text(encoding="utf-8").strip().splitlines()]
        check("链式输入时文件对象与自身统计自洽",
              len(chained_rows) == chained_stats["sampled_records"]
              and len(chained_rows) > 0,
              "%s vs %s" % (len(chained_rows),
                            chained_stats["sampled_records"]))

        bad_id = client.post("/predict", json={"uniqueId": "../escape",
                                               "isLastOperator": False})
        check("非法 uniqueId 被拒绝", bad_id.status_code == 422
              and bad_id.json()["code"] == 1)
        escape = client.post("/predict", json={
            "uniqueId": "smoke_one", "isLastOperator": False,
            "inputFile": "../smoke_two/input/train/samples.jsonl"})
        check("跨任务 inputFile 被拒绝", escape.status_code == 400
              and escape.json()["code"] == 1)
        strict_bool = client.post("/predict", json={"uniqueId": "smoke_one",
                                                    "isLastOperator": "false"})
        check("isLastOperator 非严格布尔被拒绝", strict_bool.status_code == 422)
        download = client.get("/downloadFile", params={"uniqueId": "smoke_one"})
        check("/downloadFile 未注册", download.status_code == 404)

        slip_zip = workdir / "slip.zip"
        with zipfile.ZipFile(slip_zip, "w") as bundle:
            bundle.writestr("../escape.jsonl", "{}")
            bundle.writestr("train/samples.jsonl", "{}\n")
        with open(slip_zip, "rb") as handle:
            slip = client.post("/uploadFile", params={"uniqueId": "smoke_slip"},
                               files={"file": ("slip.zip", handle, "application/zip")})
        check("zip 目录穿越被拒绝",
              slip.status_code == 422 and slip.json()["code"] == 1)

        removed = client.post("/removeFile", params={"uniqueId": "smoke_two"}).json()
        again = client.post("/removeFile", params={"uniqueId": "smoke_two"}).json()
        check("removeFile 幂等且只清本任务",
              removed["data"]["removed"] is True and again["data"]["removed"] is False
              and (output_root / "smoke_one").is_dir())

        client.close()
    finally:
        process.terminate()
        try:
            process.wait(timeout=10)
        except subprocess.TimeoutExpired:                  # pragma: no cover
            process.kill()
        shutil.rmtree(workdir, ignore_errors=True)

    passed = sum(1 for _, ok, _ in CHECKS if ok)
    for name, ok, detail in CHECKS:
        print("%s %s%s" % ("PASS" if ok else "FAIL", name,
                           (" | " + detail) if (detail and not ok) else ""))
    print("共 %d 项检查，通过 %d 项，失败 %d 项" % (len(CHECKS), passed,
                                                  len(CHECKS) - passed))
    return 0 if passed == len(CHECKS) else 1


if __name__ == "__main__":
    sys.exit(main(sys.argv))
