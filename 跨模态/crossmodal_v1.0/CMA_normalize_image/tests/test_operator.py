"""算子 4 业务算法与 run 流程测试（使用真实可解码媒体，不 mock 图像处理）。"""
from __future__ import annotations

import io
import json
from pathlib import Path

import pytest
from PIL import Image

import inference
from utils import image_processing as ip


def base_config(**overrides) -> dict:
    config = {
        "output_format": "PNG",
        "max_side": 1024,
        "allow_upscale": False,
        "alpha_background": [255, 255, 255],
        "max_pixels": 40000000,
        "jpeg_quality": 95,
    }
    config.update(overrides)
    return config


# --------------------------------------------------------------------------- #
# 纯函数
# --------------------------------------------------------------------------- #

def test_plan_scale_preserves_aspect_ratio():
    new_width, new_height, ratio, resized = ip.plan_scale(1600, 800, 1024, False)
    assert (new_width, new_height) == (1024, 512)
    assert resized is True
    assert ratio == pytest.approx(0.64)


def test_plan_scale_does_not_upscale_by_default():
    assert ip.plan_scale(64, 32, 1024, False) == (64, 32, 1.0, False)


def test_plan_scale_upscales_only_when_allowed():
    new_width, new_height, ratio, resized = ip.plan_scale(64, 32, 1024, True)
    assert (new_width, new_height) == (1024, 512)
    assert resized is True and ratio > 1.0


def test_plan_scale_never_below_one_pixel():
    new_width, new_height, _ratio, _resized = ip.plan_scale(10000, 1, 16, False)
    assert new_width == 16 and new_height == 1


def test_plan_scale_rejects_invalid_size():
    with pytest.raises(ip.ImageProcessingError) as excinfo:
        ip.plan_scale(0, 10, 1024, False)
    assert excinfo.value.reason == "invalid_size"


def test_parse_background_uses_first_three_channels():
    assert ip.parse_background([1, 2, 3]) == (1, 2, 3)
    assert ip.parse_background([1, 2, 3, 4]) == (1, 2, 3)
    with pytest.raises(ip.ImageProcessingError) as excinfo:
        ip.parse_background([1, 2])
    assert excinfo.value.reason == "invalid_background"


def test_normalize_output_format_and_suffix():
    assert ip.normalize_output_format("png") == "PNG"
    assert ip.suffix_for_format("JPEG") == ".jpg"
    with pytest.raises(ip.ImageProcessingError) as excinfo:
        ip.normalize_output_format("GIF")
    assert excinfo.value.reason == "format_unsupported"


def test_laplacian_variance_orders_sharp_above_blur(sample_dir):
    with Image.open(sample_dir / "media" / "sharp_checker_512.png") as handle:
        sharp_image = handle.convert("L").copy()
    with Image.open(sample_dir / "media" / "blur_checker_512.png") as handle:
        blur_image = handle.convert("L").copy()

    sharp_score, sharp_backend = ip.compute_laplacian_variance(sharp_image)
    blur_score, blur_backend = ip.compute_laplacian_variance(blur_image)

    assert sharp_backend == blur_backend  # 同一环境内后端一致
    assert sharp_score > 1000.0
    assert blur_score < 100.0
    assert sharp_score > 100.0 * blur_score


def test_laplacian_variance_reports_available_backend():
    assert ip.available_blur_backend() in ("opencv_laplacian", "numpy_laplacian")
    assert ip.pillow_available() is True


# --------------------------------------------------------------------------- #
# standardize_image
# --------------------------------------------------------------------------- #

def test_standardize_resizes_wide_image(tmp_path, sample_dir):
    result = ip.standardize_image(sample_dir / "media" / "wide_1600x800.png",
                                  tmp_path / "out.png", base_config())
    assert (result["width"], result["height"]) == (1024, 512)
    assert result["format"] == "PNG"
    transform = result["transform"]
    assert transform["resized"] is True and transform["upscaled"] is False
    assert (transform["original_width"], transform["original_height"]) == (1600, 800)
    assert transform["scale"] == pytest.approx(0.64)
    with Image.open(tmp_path / "out.png") as handle:
        assert handle.size == (1024, 512) and handle.mode == "RGB"


def test_standardize_keeps_small_image_size(tmp_path, sample_dir):
    result = ip.standardize_image(sample_dir / "media" / "small_64x32.png",
                                  tmp_path / "small.png", base_config())
    assert (result["width"], result["height"]) == (64, 32)
    assert result["transform"]["resized"] is False


def test_standardize_applies_exif_orientation(tmp_path, sample_dir):
    result = ip.standardize_image(sample_dir / "media" / "exif_rot_90.jpg",
                                  tmp_path / "rotated.jpg",
                                  base_config(output_format="JPEG"))
    transform = result["transform"]
    assert transform["exif_orientation"] == 6
    assert transform["orientation_applied"] is True
    assert (transform["original_width"], transform["original_height"]) == (320, 160)
    assert (result["width"], result["height"]) == (160, 320)
    with Image.open(tmp_path / "rotated.jpg") as handle:
        assert handle.size == (160, 320)
        assert handle.getexif().get(274) in (None, 1)  # 输出不再携带原始方向


def test_standardize_composites_alpha_onto_configured_background(tmp_path, sample_dir):
    result = ip.standardize_image(sample_dir / "media" / "alpha_64x64.png",
                                  tmp_path / "alpha.png",
                                  base_config(alpha_background=[0, 0, 0]))
    assert result["format"] == "PNG"
    with Image.open(tmp_path / "alpha.png") as handle:
        assert handle.mode == "RGB"
        red, green, blue = handle.getpixel((0, 0))
    # 半透明红 (255,0,0,128) 合成到黑底 ≈ (128,0,0)
    assert red == pytest.approx(128, abs=2)
    assert green == 0 and blue == 0


def test_standardize_rejects_suffix_mismatch(tmp_path, sample_dir):
    with pytest.raises(ip.ImageProcessingError) as excinfo:
        ip.standardize_image(sample_dir / "media" / "small_64x32.png",
                             tmp_path / "mismatch.jpg", base_config(output_format="PNG"))
    assert excinfo.value.reason == "suffix_mismatch"


def test_standardize_rejects_corrupt_file(tmp_path, sample_dir):
    with pytest.raises(ip.ImageProcessingError) as excinfo:
        ip.standardize_image(sample_dir / "media" / "corrupt.png",
                             tmp_path / "corrupt.png", base_config())
    assert excinfo.value.reason == "decode_failed"


def test_standardize_rejects_pixel_limit(tmp_path, sample_dir):
    with pytest.raises(ip.ImageProcessingError) as excinfo:
        ip.standardize_image(sample_dir / "media" / "wide_1600x800.png",
                             tmp_path / "wide.png", base_config(max_pixels=1000))
    assert excinfo.value.reason == "pixel_limit_exceeded"


def test_standardize_leaves_no_partial_file_on_failure(tmp_path, sample_dir):
    target = tmp_path / "nested" / "corrupt.png"
    with pytest.raises(ip.ImageProcessingError):
        ip.standardize_image(sample_dir / "media" / "corrupt.png", target, base_config())
    assert not target.exists()
    assert not list(tmp_path.rglob("*.tmp"))


# --------------------------------------------------------------------------- #
# run 流程
# --------------------------------------------------------------------------- #

def read_jsonl(path: Path):
    with io.open(path, encoding="utf-8") as stream:
        return [json.loads(line) for line in stream if line.strip()]


def test_run_end_to_end(run_dirs, sample_dir):
    root,unique_id,entry,locate=run_dirs
    result=inference.run(str(entry),unique_id,False,str(root),overrides={"enabled":True,"output_format":"PNG"})
    stats=result['statistics'];run_dir=locate(result)
    assert result['status']=='partial' and stats['conservation_ok']
    assert stats['output']+stats['rejected']==stats['valid_records']==13
    assert stats['output']==8 and stats['images_standardized']==6
    records=read_jsonl(run_dir/'04_image_index.jsonl'); by_id={r['sample_id']:r for r in records}
    assert (by_id['demo_04_wide_001']['image']['width'],by_id['demo_04_wide_001']['image']['height'])==(1024,512)
    assert (by_id['demo_04_small_001']['image']['width'],by_id['demo_04_small_001']['image']['height'])==(64,32)
    assert 'demo_04_blur_001' in by_id
    for r in records:
        image=r.get('image')
        if not image:continue
        assert image.get('blur_score') is None  # 上游空占位保留，但不生成清晰度分。
        assert not {'blur_metric','blur_status'} & image.keys()
        with Image.open(run_dir/image['path']) as handle:handle.verify()
        assert (run_dir/image['original_path']).is_file()
    assert inference.verify_stage_media(run_dir/'04_image_index.jsonl',run_dir)==[]
    manifest=json.loads((run_dir/'manifest.json').read_text('utf-8'))
    for artifact in manifest['artifacts']:assert (run_dir/artifact['path']).is_file()
    assert by_id['demo_04_wide_001']['processing']['03']['status']=='success'
    # [v4.2] 血缘输出已停用：run() 返回不再携带 stage_context 键
    assert 'stage_context' not in result
    assert 'lineage' not in result
    assert (run_dir/by_id['demo_04_wide_001']['image']['original_path']).read_bytes()==(sample_dir/'media/wide_1600x800.png').read_bytes()


def test_run_rejects_carry_expected_reasons(run_dirs):
    root, unique_id, entry, locate = run_dirs
    result = inference.run(str(entry), unique_id, False, str(root), overrides={"enabled":True,"output_format":"PNG"})
    run_dir = locate(result)
    rejects = read_jsonl(run_dir / "04_rejects.jsonl")
    assert len(rejects) == 6  # 5 条记录级拒绝 + 1 条坏 JSON 行
    reasons = {}
    for item in rejects:
        reasons.setdefault(item["reason"], []).append(item.get("sample_id"))
    assert reasons["invalid_json"] == [None]
    assert reasons["media_missing:image"] == ["demo_04_missing_001", "demo_04_zero_001"]
    assert reasons["image_decode_failed"] == ["demo_04_corrupt_001"]
    assert reasons["duplicate_sample_id"] == ["demo_04_text_001"]
    assert reasons["invalid_type:image"] == ["demo_04_badtype_001"]

    # 因媒体问题被拒绝的记录：image 置 null 且保留可追溯来源
    corrupt = next(item for item in rejects if item["reason"] == "image_decode_failed")
    assert corrupt["record"]["image"] is None
    assert corrupt["evidence"]["source_reference"]["image"] == "media/corrupt.png"
    assert corrupt["record"]["text"]["content"]  # 其余完整记录仍保留


def test_run_blur_action_reject_drops_flagged_without_dangling_files(run_dirs):
    root,uid,entry,_=run_dirs
    r=inference.run(str(entry),uid,False,str(root),overrides={'enabled':True,'blur_action':'reject'})
    assert r['status']=='failed'
    assert r['output_file'] is None


def test_run_output_format_jpeg(run_dirs):
    root, unique_id, entry, locate = run_dirs
    result = inference.run(str(entry), unique_id, False, str(root),
                           overrides={"enabled":True,"output_format": "JPEG"})
    run_dir = locate(result)
    assert result["statistics"]["output_format"] == "JPEG"
    records = read_jsonl(run_dir / "04_image_index.jsonl")
    for item in records:
        image = item.get("image")
        if not image:
            continue
        assert image["format"] == "JPEG"
        assert image["path"].endswith(".jpg")
        assert image["transform"]["jpeg_quality"] == 95
        with Image.open(run_dir / image["path"]) as handle:
            assert handle.format == "JPEG"


def test_run_pixel_limit_override_rejects_large_image(run_dirs):
    root,uid,entry,_=run_dirs
    r=inference.run(str(entry),uid,False,str(root),overrides={'enabled':True,'max_pixels':100})
    assert r['status']=='failed'
    assert r['output_file'] is None  # 部署资源参数不属于业务请求合同。


def test_run_empty_input_fails_without_products(run_dirs):
    root, unique_id, _entry, _locate = run_dirs
    empty = root / unique_id / "input" / "input_empty.jsonl"
    result = inference.run(str(empty), unique_id, False, str(root))
    assert result["status"] == "failed"
    assert result["code"] == "empty_input"
    assert result["output_file"] is None
    assert result["statistics"]["output"] == 0
    run_dir = Path(result["run_dir"])
    assert not (run_dir / "04_image_index.jsonl").exists()
    assert not (run_dir / "manifest.json").exists()


def test_run_bad_encoding_fails(run_dirs):
    root, unique_id, _entry, _locate = run_dirs
    bad = root / unique_id / "input" / "input_bad_encoding.jsonl"
    result = inference.run(str(bad), unique_id, False, str(root))
    assert result["status"] == "failed"
    assert result["code"] == "invalid_input_encoding"
    assert result["output_file"] is None


def test_run_missing_input_file_fails(run_dirs, tmp_path):
    root, unique_id, _entry, _locate = run_dirs
    result = inference.run(str(tmp_path / "nope.jsonl"), unique_id, False, str(root))
    assert result["status"] == "failed"
    assert result["code"] == "input_file_not_found"


def test_run_invalid_unique_id_fails(run_dirs, tmp_path):
    root, _unique_id, entry, _locate = run_dirs
    result = inference.run(str(entry), "../escape", False, str(root))
    assert result["status"] == "failed"
    assert result["code"] == "invalid_request"
    assert not (root / "escape").exists()


# --------------------------------------------------------------------------- #
# 配置校验
# --------------------------------------------------------------------------- #

@pytest.mark.parametrize("overrides", [
    {"output_format": "GIF"},                 # 未知枚举
    {"blur_action": "drop"},                  # 未知枚举
    {"max_side": 4},                          # 低于下限
    {"max_side": 99999},                      # 高于上限
    {"jpeg_quality": 0},                      # 越界
    {"alpha_background": [1, 2]},             # 通道数错误
    {"alpha_background": [1, 2, 300]},        # 通道越界
    {"blur_threshold": float("nan")},         # 非有限值
    {"allow_upscale": "yes"},                 # 非布尔
    {"sharpness": "high"},                    # 未知字段
])
def test_run_rejects_invalid_business_config(run_dirs, overrides):
    root, unique_id, entry, _locate = run_dirs
    result = inference.run(str(entry), unique_id, False, str(root), overrides=overrides)
    assert result["status"] == "failed"
    assert result["code"] == "invalid_config"


def test_deployment_keys_cannot_be_overridden(run_dirs):
    root, unique_id, entry, _locate = run_dirs
    result = inference.run(str(entry), unique_id, False, str(root),
                           overrides={"stage_id": "S9", "io": {"output_root": "hacked"},
                                      "max_side": 512})
    assert result["status"] in ("success", "partial")
    config = result["config"]
    assert config["deployment"]["stage_id"] == "S2"
    assert config["business"]["max_side"] == 512
    assert config["overridden_keys"] == ["max_side"]
    assert config["ignored_deployment_keys"] == ["io", "stage_id"]


def test_load_config_defaults_are_valid():
    cfg,_=inference.load_config()
    assert cfg.enabled is False and cfg.max_side==1024
    assert cfg.output_format=='JPEG' and cfg.allow_upscale is False


def test_parse_overrides_parses_json_literals():
    parsed = inference.parse_overrides(["max_side=512", "allow_upscale=true",
                                        "alpha_background=[0,0,0]", "output_format=PNG"])
    assert parsed == {"max_side": 512, "allow_upscale": True,
                      "alpha_background": [0, 0, 0], "output_format": "PNG"}
    with pytest.raises(ValueError):
        inference.parse_overrides(["max_side"])
