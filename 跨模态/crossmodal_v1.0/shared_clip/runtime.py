"""冻结ViT-B/32的本地任务服务；按需加载，不联网、不返回替代分数。"""
from __future__ import annotations
import importlib.util
import json
import os
from pathlib import Path
import subprocess
import sys
import time
import uuid
from multiprocessing.connection import Client, Listener

ENV = 'MULTIMODAL_CLIP_SESSION'


def _load_real(config):
    path=Path(__file__).resolve().parents[1]/'CMA_score_image_text_consistency/utils/backends.py'
    spec=importlib.util.spec_from_file_location('_shared_real_clip12',path)
    module=importlib.util.module_from_spec(spec);spec.loader.exec_module(module)
    device=module.resolve_device(config.get('device','auto'))
    return module.ClipViTB32Backend(model_name='ViT-B-32',weights=config['weights'],
                                   device=device,batch_size=int(config.get('batch_size',8)),library='clip')


class ClipStore:
    def __init__(self, loader=None):
        self._loader=loader or _load_real
        self._encoder=None
        self._weights=None
        self._loads=0
        self._operators=set()
        self._instance=uuid.uuid4().hex

    def ensure(self, config, operator):
        architecture=str(config.get('model_name') or 'ViT-B-32').lower()
        if architecture not in {'vit-b-32','vit-b/32','clip-vit/b-32','clip-vit-base-patch32','openai/clip-vit-base-patch32'}:
            raise ValueError('表1冻结CLIP ViT-B/32；共享服务拒绝另一个模型')
        weights=config.get('weights')
        if not weights: raise ValueError('未配置真实CLIP权重')
        resolved=str(Path(weights).resolve())
        if self._weights and resolved!=self._weights:
            raise ValueError('同一任务不得加载第二份CLIP权重')
        if self._encoder is None:
            encoder=self._loader({**config,'weights':resolved})
            self._encoder=encoder
            self._weights=resolved
            self._loads+=1
        self._operators.add(str(operator))
        return self._encoder

    def identity(self):
        if self._encoder is None:return None
        return {**self._encoder.model_identity(), 'instance_id':self._instance,
                'shared_task_instance':True,'weight_loads':self._loads}

    def statistics(self):
        return {'weight_loads':self._loads,'instance_id':self._instance,
                'operators':sorted(self._operators),'model':self.identity()}

    def dispatch(self, request):
        operation=request['operation']
        if operation=='stats':return self.statistics()
        encoder=self.ensure(request['config'],request['operator'])
        if operation=='ensure':return self.identity()
        if operation=='texts':return encoder.encode_texts(request['values'])
        if operation=='images':return encoder.encode_images([Path(p) if p else None for p in request['values']])
        if operation=='crops':
            return self._crops(encoder,request['path'],request['boxes'])
        raise ValueError('未知共享CLIP操作')

    def _crops(self, encoder, path, boxes):
        # 使用同一模型和预处理，仅替换输入为候选框裁图。
        from PIL import Image
        import torch
        vectors=[None]*len(boxes);errors=[None]*len(boxes)
        try:
            with Image.open(path) as opened:image=opened.convert('RGB')
        except FileNotFoundError:return vectors,['media_missing']*len(boxes)
        except OSError:return vectors,['media_unreadable']*len(boxes)
        for index,box in enumerate(boxes):
            try:
                bbox=box.get('bbox') or {}
                x,y,w,h=[float(bbox.get(k,0)) for k in ('x','y','w','h')]
                if str(bbox.get('coord','xywh-pixel')).endswith('-normalized'):
                    x,w=x*image.width,w*image.width;y,h=y*image.height,h*image.height
                left,top=max(0,round(x)),max(0,round(y))
                right,bottom=min(image.width,round(x+w)),min(image.height,round(y+h))
                if right<=left or bottom<=top:raise ValueError('empty crop')
                crop=image.crop((left,top,right,bottom))
            except (TypeError,ValueError,KeyError):
                errors[index]='crop_invalid';continue
            with torch.no_grad():
                tensor=encoder._image_tensor(crop).unsqueeze(0).to(encoder.device)
                features=encoder._image_features(tensor)
                features=features/features.norm(dim=-1,keepdim=True)
            vectors[index]=features.detach().cpu().tolist()[0]
        return vectors,errors


def _request(endpoint, message):
    with Client(tuple(endpoint['address']),authkey=bytes.fromhex(endpoint['key'])) as conn:
        conn.send(message)
        if not conn.poll(120):raise RuntimeError('共享CLIP响应超时；不退回重复加载')
        response=conn.recv()
    if not response['ok']:raise RuntimeError(response['error'])
    return response['result']


class RemoteClip:
    name='CLIP-ViT/B-32'
    degraded=False
    is_test_double=False
    untrained_architecture_only=False
    embed_dim=512

    @classmethod
    def from_env(cls, config, operator):
        path=os.getenv(ENV)
        if not path:return None
        endpoint=json.loads(Path(path).read_text(encoding='utf-8'))
        return cls(endpoint,config,operator)

    def __init__(self, endpoint, config, operator):
        self.endpoint=endpoint;self.config=dict(config);self.operator=str(operator)
        self._identity=self._call('ensure')
        self.device=self._identity['device'];self.library=self._identity['library']
        self.version=self._identity['version'];self.batch_size=int(config.get('batch_size',8))
        self.weights=config['weights']

    def _call(self, operation, **fields):
        return _request(self.endpoint,{'operation':operation,'config':self.config,'operator':self.operator,**fields})
    def model_identity(self):return dict(self._identity)
    def encode_texts(self,texts):return self._call('texts',values=list(texts))
    def encode_images(self,paths):
        return self._call('images',values=[str(Path(p).resolve()) if p is not None else None for p in paths])
    def encode_crops(self,path,boxes):return self._call('crops',path=str(Path(path).resolve()),boxes=list(boxes))


class ReverseRemoteClip(RemoteClip):
    """#20的旧接口仅返回逐图向量，错误以None表示。"""
    def encode_images(self,paths):return super().encode_images(paths)[0]


def _serve(endpoint_file,key,stats_file):
    store=ClipStore()
    with Listener(('127.0.0.1',0),authkey=bytes.fromhex(key)) as listener:
        Path(endpoint_file).write_text(json.dumps({'address':listener.address,'key':key}),encoding='utf-8')
        running=True
        while running:
            with listener.accept() as conn:
                try:
                    message=conn.recv()
                    if message.get('operation')=='shutdown':
                        result=store.statistics();running=False
                    else:result=store.dispatch(message)
                    conn.send({'ok':True,'result':result})
                except Exception as exc:
                    conn.send({'ok':False,'error':str(exc)})
                Path(stats_file).write_text(json.dumps(store.statistics(),ensure_ascii=False,indent=2),encoding='utf-8')


class SharedClipSession:
    """用一个上下文包住全部阶段；子进程继承本地服务地址。"""
    def __init__(self, output_dir):self.output=Path(output_dir)
    def __enter__(self):
        self.output.mkdir(parents=True,exist_ok=True)
        self.endpoint=self.output/('_clip_endpoint_'+uuid.uuid4().hex+'.json')
        self.stats_file=self.output/'共享CLIP实例校验.json'
        self.old_env=os.getenv(ENV)
        key=os.urandom(32).hex()
        self.log=open(self.output/'shared_clip.log','w',encoding='utf-8')
        self.proc=subprocess.Popen([sys.executable,str(Path(__file__)),str(self.endpoint),key,str(self.stats_file)],stdout=self.log,stderr=self.log)
        start=time.monotonic()
        while not self.endpoint.exists():
            if self.proc.poll() is not None:raise RuntimeError('共享CLIP服务启动失败')
            if time.monotonic()-start>15:raise RuntimeError('共享CLIP启动超时')
            time.sleep(0.05)
        os.environ[ENV]=str(self.endpoint)
        return self
    def statistics(self):
        return _request(json.loads(self.endpoint.read_text(encoding='utf-8')),{'operation':'stats'})
    def __exit__(self,*args):
        try:
            if self.proc.poll() is None:
                _request(json.loads(self.endpoint.read_text(encoding='utf-8')),{'operation':'shutdown'})
                self.proc.wait(timeout=10)
        finally:
            if self.proc.poll() is None:self.proc.terminate()
            self.log.close();self.endpoint.unlink(missing_ok=True)
            if self.old_env is None:os.environ.pop(ENV,None)
            else:os.environ[ENV]=self.old_env


if __name__=='__main__':_serve(*sys.argv[1:])
