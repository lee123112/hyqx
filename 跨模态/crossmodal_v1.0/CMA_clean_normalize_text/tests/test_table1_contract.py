import json
import pytest
import inference as op
from utils.schemas import OperatorConfig
from utils import text_processing as tp

def test_default_disabled(tmp_path):
    cfg,_=op.load_config()
    assert cfg.enabled is False
    src=tmp_path/'in.jsonl';rec={'sample_id':'a','text':'  ，\ufffd  '}
    src.write_text(json.dumps(rec,ensure_ascii=False)+'\n',encoding='utf-8')
    r=op.run(str(src),'off_f1',output_root=str(tmp_path/'out'))
    out=json.loads(open(r['outputFile'],encoding='utf-8').readline())
    assert out['text']==rec['text']
    assert '关闭' in (tmp_path/'out/off_f1/text_normalization'/r['runId']/'run.log').read_text(encoding='utf-8')

def test_cleaning_does_not_tokenize_and_unifies_punctuation(monkeypatch):
    def forbidden(*_): raise AssertionError('表1禁止#4分词')
    monkeypatch.setattr(tp,'tokenize',forbidden)
    r=tp.normalize_text('橘猫，乙炔！\ufffd',{'enabled':True})
    assert r['content']=='橘猫,乙炔!'
    assert 'tokens' not in r

def test_preserves_existing_tokens():
    r=tp.normalize_text('橘猫，',{'enabled':True})
    rec={'text':{'content':'橘猫，','tokens':['橘猫'],'token_offsets':[[0,2]],'tokenizer':'upstream'}}
    out=op._build_text_object(rec,r)
    assert out['tokens']==['橘猫'] and out['tokenizer']=='upstream'

@pytest.mark.parametrize('key', ['tokenizer','ad_patterns','lowercase_latin'])
def test_forbidden_business_parameters(key):
    with pytest.raises(ValueError): OperatorConfig(**{key: 'jieba' if key=='tokenizer' else [] if key=='ad_patterns' else True})

def test_enabled_empty_text_original_quarantine(tmp_path):
    src=tmp_path/'in.jsonl';src.write_text(json.dumps({'sample_id':'a','text':'\ufffd\x00'})+'\n',encoding='utf-8')
    r=op.run(str(src),'on_f1',output_root=str(tmp_path/'out'),overrides={'enabled':True})
    rejects=[json.loads(l) for l in open(r['rejectsFile'],encoding='utf-8')]
    assert rejects[0]['destination']=='原始隔离区'

def test_missing_media_description_is_kept(tmp_path):
    src=tmp_path/'in.jsonl'
    (tmp_path/'audio.wav').write_bytes(b'upstream media')
    src.write_text(json.dumps({'sample_id':'a','text':'  ','audio':'audio.wav'})+'\n',encoding='utf-8')
    r=op.run(str(src),'missing_desc',output_root=str(tmp_path/'out'),overrides={'enabled':True})
    out=[json.loads(l) for l in open(r['outputFile'],encoding='utf-8')]
    assert len(out)==1 and out[0]['sample_id']=='a'
    assert out[0]['processing']['03']['reason']=='missing_description'
