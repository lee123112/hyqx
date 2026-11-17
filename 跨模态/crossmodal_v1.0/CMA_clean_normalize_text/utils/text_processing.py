"""表1 #4：只去乱码、空文本、统一标点；不分词、不去重。"""
import re
import unicodedata

class TokenizerUnavailableError(RuntimeError):
    """兼容旧异常类型；当前实现从不加载分词后端。"""

def tokenize(*args, **kwargs):
    raise ValueError('表1禁止#4分词，需沿用上游或交#18处理')

PUNCT = str.maketrans({
    '，': ',', '。': '.', '！': '!', '？': '?', '：': ':', '；': ';', '、': ',',
    '“': '"', '”': '"', '‘': "'", '’': "'", '（': '(', '）': ')',
    '【': '[', '】': ']', '《': '<', '》': '>', '…': '...', '—': '-',
})

def normalize_text(text, config):
    if not isinstance(text, str):
        raise TypeError('text 必须为字符串')
    if set(config) - {'enabled', 'lang'}:
        raise ValueError('表1 #4只允许总开关，禁止分词和扩展清洗参数')
    counts = {'chars_in': len(text), 'control_chars_removed': 0,
              'replacement_chars': 0, 'punctuation_normalized': 0,
              'degraded': False}
    content = text
    if config.get('enabled', False):
        counts['replacement_chars'] = content.count('\ufffd')
        content = content.replace('\ufffd', '')
        kept=[]
        for ch in content:
            if unicodedata.category(ch) in ('Cc', 'Cs') and ch not in '\t\n\r':
                counts['control_chars_removed'] += 1
            else:
                kept.append(ch)
        content=''.join(kept)
        counts['punctuation_normalized'] = sum(ord(ch) in PUNCT for ch in content)
        content=content.translate(PUNCT)
        if not content.strip(): content=''
    counts['chars_out']=len(content)
    return {'content':content, 'original':text, 'lang':config.get('lang','zh'),
            'degraded':False,'degrade_reason':None,'metrics':counts}

def clean_text(text, config):
    result=normalize_text(text, config)
    return result['content'],result['metrics']
