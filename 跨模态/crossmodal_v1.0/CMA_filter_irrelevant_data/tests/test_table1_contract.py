"""表1 v4.1 初筛合同；不把文件名或标签当成描述。"""
from pathlib import Path
import pytest
import inference as op

def test_filename_and_tags_are_not_evidence(tmp_path):
    (tmp_path / '橘猫.jpg').write_bytes(b'placeholder')
    rec = {'sample_id': 'a', 'text': '乙炔', 'image': '橘猫.jpg',
           'metadata': {'image_tags': ['橘猫']}}
    out, reason, evidence = op.prescreen_record(rec, op.load_config(), tmp_path)
    assert reason is None
    assert out['sample_id'] == 'a'
    assert evidence.get('matched_keywords', []) == []

def test_explicit_caption_with_no_common_word_is_isolated(tmp_path):
    (tmp_path / 'a.jpg').write_bytes(b'placeholder')
    rec = {'sample_id': 'a', 'text': '乙炔', 'image': 'a.jpg',
           'metadata': {'image_caption': '橘猫'}}
    assert op.prescreen_record(rec, op.load_config(), tmp_path)[1] == 'no_common_words'

# 2026-10-22 试验值：默认阈值 2→7，原 (100,103)（差 3 秒）的拒绝用例需改成差 >7 秒才能保持"超阈值拒绝"语义
@pytest.mark.parametrize('video,audio,reject', [(100,110,True),(3,5,False),(10,11,False)])
def test_duration_uses_seconds(tmp_path, monkeypatch, video, audio, reject):
    (tmp_path/'v.mp4').write_bytes(b'x'); (tmp_path/'a.wav').write_bytes(b'x')
    monkeypatch.setattr(op, 'video_duration', lambda _: video)
    monkeypatch.setattr(op, 'audio_duration', lambda _: audio)
    rec={'sample_id':'a','video':'v.mp4','audio':'a.wav'}
    out,reason,e=op.prescreen_record(rec,op.load_config(),tmp_path)
    assert bool(reason) == reject
    assert e['duration_diff_seconds'] == abs(video-audio)

def test_missing_duration_preserved(tmp_path,monkeypatch):
    (tmp_path/'v.mp4').write_bytes(b'x');(tmp_path/'a.wav').write_bytes(b'x')
    monkeypatch.setattr(op,'video_duration',lambda _: None)
    assert op.prescreen_record({'sample_id':'a','video':'v.mp4','audio':'a.wav'},op.load_config(),tmp_path)[1] is None

def test_disabled_does_not_probe_missing_media(tmp_path):
    cfg=op.load_config();cfg['enabled']=False
    out,reason,e=op.prescreen_record({'sample_id':'a','image':'missing.jpg'},cfg,tmp_path)
    assert reason is None
    # [v4.2] status 双字段：int status + process_status
    assert out['prescreen']['status'] == 1
    assert out['prescreen']['process_status'] == 'disabled'

def test_api_business_override_is_applied(tmp_path):
    import json
    from fastapi.testclient import TestClient
    import app
    src=tmp_path/'output/off_f1/input/input.jsonl'
    src.parent.mkdir(parents=True)
    src.write_text(json.dumps({'sample_id':'a','text':'乙炔','image':'missing.jpg'})+'\n',encoding='utf-8')
    monkey=pytest.MonkeyPatch();monkey.setattr(app,'OUTPUT_ROOT',tmp_path/'output')
    try:
        r=TestClient(app.app).post('/predict',json={'uniqueId':'off_f1','isLastOperator':True,'inputFile':'input/input.jsonl','config':{'enabled':False}})
        assert r.json()['code']==0, r.json()
        row=json.loads((tmp_path/'output/off_f1/02_screened.jsonl').read_text(encoding='utf-8').splitlines()[0])
        # [v4.2] status 双字段：int status + process_status
        assert row['prescreen']['status'] == 1
        assert row['prescreen']['process_status'] == 'disabled'
    finally:monkey.undo()

# --------------------------------------------------------------------------- #
# #3 音视频时长差阈值实验：[v4.2] 2026-10-22 业务方要求默认 7 秒、代码允许 1–10
# （规格建议仍为默认 2 / 可选 1–5，待业务确认后定稿或回退）
# --------------------------------------------------------------------------- #

def test_duration_threshold_experimental_value_7():
    cfg = op.load_config(overrides={'prescreen': {'video_audio_max_duration_diff_seconds': 7.0}})
    assert cfg['prescreen']['video_audio_max_duration_diff_seconds'] == 7.0

def test_duration_threshold_upper_bound_10():
    cfg = op.load_config(overrides={'prescreen': {'video_audio_max_duration_diff_seconds': 10.0}})
    assert cfg['prescreen']['video_audio_max_duration_diff_seconds'] == 10.0

def test_duration_threshold_over_upper_bound_raises():
    with pytest.raises(ValueError):
        op.load_config(overrides={'prescreen': {'video_audio_max_duration_diff_seconds': 10.5}})
