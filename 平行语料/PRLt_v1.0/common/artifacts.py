"""阶段产物打包与对象存储占位（无血缘改造，增强算子 21–24 共用）。

阶段产物 zip 只包含本算子主输出、异常清单和运行日志，不再包含血缘文件。
与各算子 utils/artifacts.py 保持一致。
"""
import os
import shutil
import zipfile
from pathlib import Path

STAGE_ARTIFACT_DIRNAME = "artifacts"


def _iter_existing(paths):
    seen = set()
    for path in paths:
        if not path:
            continue
        p = Path(path)
        if not p.is_file():
            continue
        key = str(p.resolve())
        if key in seen:
            continue
        seen.add(key)
        yield p


def build_stage_artifact(task_dir, operator_id, unique_id, output_file, reject_file, log_file=None):
    """把主输出、异常清单和运行日志打包为 artifacts/stage_<op>_<uid>.zip。"""
    task_dir = Path(task_dir)
    files = list(_iter_existing([output_file, reject_file, log_file]))
    if not files:
        return None
    artifact_dir = task_dir / STAGE_ARTIFACT_DIRNAME
    artifact_dir.mkdir(parents=True, exist_ok=True)
    archive = artifact_dir / f"stage_{operator_id}_{unique_id}.zip"
    if archive.exists():
        archive.unlink()
    with zipfile.ZipFile(archive, "w", compression=zipfile.ZIP_DEFLATED) as bundle:
        for path in files:
            try:
                arcname = path.resolve().relative_to(task_dir.resolve())
            except ValueError:
                arcname = path.name
            bundle.write(path, arcname.as_posix())
    return archive


def publish_stage_artifact(unique_id, stage_zip):
    """对象存储归档：配置 OBJECT_STORE_DIR 时复制并返回 file URI，否则返回 local-mirror 逻辑地址。"""
    if not stage_zip:
        return None
    archive = Path(stage_zip)
    if not archive.is_file():
        return None
    root = os.getenv("OBJECT_STORE_DIR")
    if root:
        dest = Path(root) / unique_id / archive.name
        dest.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(archive, dest)
        return dest.resolve().as_uri()
    return f"local-mirror://{unique_id}/{archive.name}"


def finalize_stage_artifacts(task_dir, operator_id, unique_id, output_file, reject_file, log_file=None):
    """生成 stageZip 与 storageUri，返回 (stage_zip, storage_uri)，均为 str 或 None。"""
    archive = build_stage_artifact(task_dir, operator_id, unique_id, output_file, reject_file, log_file)
    return (str(archive) if archive else None, publish_stage_artifact(unique_id, archive))
