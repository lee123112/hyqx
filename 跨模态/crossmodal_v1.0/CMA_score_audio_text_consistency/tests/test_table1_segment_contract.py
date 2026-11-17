from utils import audio_text

def test_whole_transcript_uses_whole_audio_not_first_paragraph():
    record={'audio_transcript':'第一段。第二段。','audio':{'path':'whole.wav','duration':10,
             'segments':[{'path':'p1.wav','start':0,'end':3,'transcript':'第一段'},
                         {'path':'p2.wav','start':4,'end':10,'transcript':'第二段'}]}}
    ref=audio_text.resolve_audio_reference(record)
    assert ref['path']=='whole.wav'
    assert ref['source']=='audio.path'

def test_explicit_single_paragraph_still_uses_segment():
    record={'audio':{'path':'whole.wav','segments':[{'path':'p.wav','start':4,'end':10,'transcript':'第二段'}]}}
    assert audio_text.resolve_audio_reference(record)['path']=='p.wav'
