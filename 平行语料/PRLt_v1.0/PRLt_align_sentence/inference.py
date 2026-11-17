"""算子10：句子级对齐 —— HTTP 推理服务（三接口）。

POST /uploadFile?uniqueId=  上传输入 JSONL 到任务目录
POST /predict               执行句子级对齐；成功返回 outputFile / rejectFile / stageZip / storageUri
POST /removeFile            删除任务目录

无血缘改造（2026-10-19）：不再生成/读取/返回血缘文件；`/predict` 成功响应 `data`
保留原有 4 个字段，并增加统计、样本与拒因；不再返回 `lineageFile`。
"""
import os
import shutil
from pathlib import Path
from typing import Optional

from fastapi import FastAPI, File, Query, UploadFile
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse

from processor import run
from utils.io import task_dir, validate_unique_id
from utils.logger import get_logger
from utils.schemas import PredictRequest
from utils.predict_payload import build_predict_fields, extract_sample_limit

app = FastAPI(title="10_句子级对齐", version="v1.0.0")

OPERATOR_ID = "10"


@app.exception_handler(RequestValidationError)
async def _validation_error(_request, _exc):
    """保持请求校验失败也遵循 MTY 的 {code,msg,data} 外层结构。"""
    return JSONResponse(
        status_code=200,
        content={"code": 1, "msg": "请求参数校验失败", "data": None},
    )

OUTPUT_ROOT = Path(os.getenv("OUTPUT_DIR", Path(__file__).resolve().parent / "output"))
INPUT_NAME = "input.jsonl"


def ok(data=None, msg: str = "success"):
    return JSONResponse({"code": 0, "msg": msg, "data": data})


def fail(msg: str, data=None):
    return JSONResponse({"code": 1, "msg": msg, "data": data})


def _task_root(unique_id: str) -> Path:
    return task_dir(OUTPUT_ROOT, unique_id)


def _safe_path(root: Path, relative: str) -> Path:
    """目录穿越防护：相对路径必须是任务目录内的普通文件。"""
    if not isinstance(relative, str) or not relative.strip():
        raise ValueError("path 不能为空")
    raw = Path(relative)
    candidate = (raw if raw.is_absolute() else root / raw).resolve()
    if candidate == root or root not in candidate.parents:
        raise ValueError("path 指向任务目录之外")
    if not candidate.is_file():
        raise ValueError("文件不存在")
    return candidate


def _latest_output(unique_id: str) -> Path:
    """链式输入回退：不再读取血缘文件，直接回退到任务目录约定的 input.jsonl。"""
    # [无血缘改造 2026-10-19] 原实现从 lineage.json 读取上一次输出路径，已停用。
    # lineage_file = _task_root(unique_id) / LINEAGE_NAME
    # ...
    return _task_root(unique_id) / INPUT_NAME


def _output_content(path: Optional[str]) -> Optional[str]:
    if not path:
        return None
    try:
        return Path(path).read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError):
        return None


def _predict_data(result: dict, is_last: bool, sample_limit=20, input_file=None):
    """组装规范返回：outputFile / rejectFile / stageZip / storageUri（无 lineageFile）。"""
    if result.get("status") == "failed":
        return None, result.get("message") or "处理失败"
    output_path = result.get("outputFile")
    if not output_path or not Path(output_path).is_file():
        return None, "输出文件不可读取"
    if is_last:
        content = _output_content(output_path)
        if content is None:
            return None, "阶段末输出文件内容不可读取"
        output_value = content
    else:
        output_value = str(output_path)
    reject_path = result.get("rejectFile")
    data = {
        "outputFile": output_value,
        "rejectFile": str(reject_path) if reject_path else None,
        "stageZip": result.get("stageZip"),
        "storageUri": result.get("storageUri"),
    }
    data.update(build_predict_fields(result, sample_limit=sample_limit,
                                     operator_id="09", input_file=input_file))
    return data, None


@app.post("/uploadFile")
async def upload_file(uniqueId: str = Query(...), file: UploadFile = File(...)):
    try:
        root = task_dir(OUTPUT_ROOT, uniqueId)
    except ValueError as exc:
        return fail(str(exc))
    payload = await file.read()
    suffix = Path(file.filename or INPUT_NAME).suffix or ".jsonl"
    target = root / (INPUT_NAME if suffix.lower() == ".jsonl" else f"input{suffix}")
    target.write_bytes(payload)
    return {"code": 0, "msg": "success"}


@app.post("/predict")
def predict(request: PredictRequest):
    if request.isLastOperator is None:
        return fail("isLastOperator 必须为布尔类型")
    operator_id = str(request.operatorId or "").strip()
    if operator_id and operator_id != OPERATOR_ID:
        return fail(f"operatorId 不匹配：本算子为 {OPERATOR_ID}")
    if not request.inputFile:
        return fail("阶段内后续算子必须传 inputFile")
    try:
        root = task_dir(OUTPUT_ROOT, request.uniqueId)
    except ValueError as exc:
        return fail(str(exc))

    if request.inputFile:
        try:
            input_path = _safe_path(root, request.inputFile)
        except ValueError as exc:
            return fail(f"inputFile 非法：{exc}")
    else:
        input_path = _latest_output(request.uniqueId)

    if not Path(input_path).is_file():
        return fail(f"未找到输入文件：{Path(input_path).name}")

    logger = get_logger(f"sentence_alignment.api.{request.uniqueId}", root / "run.log")
    overrides = dict(request.config or {})
    overrides.update(request.params or {})
    try:
        sample_limit = extract_sample_limit(overrides)
    except ValueError as exc:
        return fail(str(exc))
    result = run(str(input_path), request.uniqueId, OUTPUT_ROOT,
                 overrides=overrides or None, logger=logger)

    data, error = _predict_data(result, request.isLastOperator, sample_limit, input_path)
    if error:
        return fail(error)
    return ok(data)


@app.post("/removeFile")
def remove_file(uniqueId: str = Query(...)):
    try:
        root = task_dir(OUTPUT_ROOT, uniqueId)
    except ValueError as exc:
        return fail(str(exc))
    shutil.rmtree(root, ignore_errors=True)
    return {"code": 0, "msg": "success"}


if __name__ == "__main__":
    import uvicorn

    uvicorn.run(app, host="0.0.0.0", port=8010)
