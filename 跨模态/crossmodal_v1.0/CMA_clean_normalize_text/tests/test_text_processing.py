"""表1当前清洗合同；旧分词/HTML/广告测试保存在原始源码快照。"""
import pytest
from utils.text_processing import normalize_text,tokenize

def test_disabled_preserves_content():
    text='<b>ＡＢＣ</b> 广告\ufffd，'
    assert normalize_text(text,{})['content']==text
def test_enabled_removes_unreadable_and_unifies_punctuation():
    r=normalize_text('Hello，世界！\x00\ufffd',{'enabled':True})
    assert r['content']=='Hello,世界!'
    assert r['metrics']['control_chars_removed']==1
    assert r['metrics']['replacement_chars']==1
    assert 'tokens' not in r and 'token_offsets' not in r
def test_enabled_keeps_html_case_and_ad_text():
    assert normalize_text('<b>ABC</b>广告',{'enabled':True})['content']=='<b>ABC</b>广告'
def test_emoji_and_zwj_preserved():
    text='👨‍👩‍👧 😀'
    assert normalize_text(text,{'enabled':True})['content']==text
def test_empty_and_invalid_text():
    assert normalize_text(' \t\n',{'enabled':True})['content']==''
    with pytest.raises(TypeError):normalize_text(123,{'enabled':True})
@pytest.mark.parametrize('config',[{'tokenizer':'jieba'},{'strip_html':True},{'ad_patterns':[]},{'lowercase_latin':True}])
def test_extended_cleaning_parameters_rejected(config):
    with pytest.raises(ValueError):normalize_text('内容',config)
def test_tokenizer_entry_rejects_use():
    with pytest.raises(ValueError):tokenize('文本')
