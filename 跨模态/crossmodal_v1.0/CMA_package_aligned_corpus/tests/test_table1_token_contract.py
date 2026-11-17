from utils.delivery import _extract_token

def test_text_segmentation_is_not_a_generated_crossmodal_token():
    assert _extract_token({'text':{'content':'橘猫','tokens':['橘猫']}})==''

def test_canonical_token_preserved_verbatim():
    assert _extract_token({'token':'<image> 橘猫'})=='<image> 橘猫'
def test_malformed_processing_does_not_become_a_token():
    from utils.delivery import _extract_token
    assert _extract_token({'processing':'not an object'})==''
