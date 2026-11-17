from PIL import Image
import json
import pytest
import inference as op
from utils.schemas import OperatorConfig
from utils import image_processing as ip

def test_defaults():
    cfg,_=op.load_config()
    assert cfg.enabled is False
    assert cfg.output_format=='JPEG' and cfg.max_side==1024

def test_no_blur_scoring_or_upscale(tmp_path,monkeypatch):
    def forbidden(*_): raise AssertionError('表1禁止清晰度评分')
    monkeypatch.setattr(ip,'compute_laplacian_variance',forbidden)
    p=tmp_path/'a.png';Image.new('RGB',(80,50)).save(p)
    r=ip.standardize_image(p,tmp_path/'out.jpg',{'enabled':True,'output_format':'JPEG','max_side':1024})
    assert (r['width'],r['height'])==(80,50)
    assert not any('blur' in k for k in r)

@pytest.mark.parametrize('cfg',[{'max_side':511},{'max_side':2049},{'allow_upscale':True},{'blur_action':'reject'},{'blur_threshold':80}])
def test_rejects_out_of_contract(cfg):
    with pytest.raises(ValueError):OperatorConfig(**cfg)

def test_resize_large_image(tmp_path):
    p=tmp_path/'a.png';Image.new('RGB',(2100,1050)).save(p)
    r=ip.standardize_image(p,tmp_path/'out.png',{'enabled':True,'output_format':'PNG','max_side':1024})
    assert (r['width'],r['height'])==(1024,512)

def test_enabled_cli_contract_carries_standard_image(tmp_path):
    Image.new('RGB',(80,50)).save(tmp_path/'a.png')
    src=tmp_path/'in.jsonl'
    src.write_text(json.dumps({'sample_id':'a','image':'a.png','text':'橘猫'})+'\n',encoding='utf-8')
    r=op.run(str(src),'enabled_f1',output_root=str(tmp_path/'out'),overrides={'enabled':True})
    row=json.loads(open(r['output_file'],encoding='utf-8').readline())
    assert row['image']['format']=='JPEG'
    assert not any('blur' in key for key in row['image'])
