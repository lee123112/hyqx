import pytest
import inference as op
from utils.schemas import OperatorConfig
from utils import video_processing as vp

def test_defaults():
    cfg,_=op.load_config()
    assert cfg.enabled is False and cfg.max_frames==8 and cfg.sample_fps==1

@pytest.mark.parametrize('kw',[{'max_frames':9},{'sample_fps':0.5},{'sample_fps':3},{'crop_black_borders':True}])
def test_contract_parameter_range(kw):
    with pytest.raises(ValueError):OperatorConfig(**kw)

def test_frame_limit_truncates_instead_of_rejecting_long_video():
    assert vp.select_frame_indices(300,10,1,8)==[0,10,20,30,40,50,60,70]
def test_disabled_run_accepts_string_video_without_decoding(tmp_path):
    import json
    import inference
    entry=tmp_path/'input.jsonl'
    (tmp_path/'broken.mp4').write_bytes(b'not a video')
    entry.write_text(json.dumps({'sample_id':'disabled','video':'broken.mp4'})+'\n',encoding='utf-8')
    result=inference.run(str(entry),'off_string',output_root=str(tmp_path/'out'))
    assert result['status']=='success'
    assert result['statistics']['frames_saved']==0
