from utils.tokens import build_sample_record

def test_samples_carry_the_generated_multimodal_token():
    payload={'sample_id':'a','text':{'content':'橘猫'},'image':{'path':'a.jpg'}}
    outcome={'token_text':'<image> 橘猫','routes':['image_text'],'route':'image_text',
             'markers':['<image>'],'modalities':['image','text'],'action':'generate','status':'generated'}
    outcome.update(decision='generate',processor_action='keep',destination='samples_file',
                   reason='token_generated',detail='generated',label='样本-样本构建：已生成Token')
    record=build_sample_record(payload,outcome)
    assert record['token']=='<image> 橘猫'
    assert record['processing']['22']['token_text']==record['token']
