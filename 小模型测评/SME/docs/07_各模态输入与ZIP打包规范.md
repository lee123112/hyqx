# 各模态输入与 ZIP 打包规范

核对日期：2026-11-06。依据当前 SME 后端解析器、算子入口、训练协议和前端调用代码。本规范约定新打包文件使用的标准子集，不扩大后端已实现的能力；原有其他兼容字段仍以代码为准。

2026-11-11模型封装修订：训练ZIP默认省略模型路径字段，使用所属算子内部models资产；移动完整工程后不需要修改数据包中的模型路径。

## 1. 先确定入口类型

| 用途 | 支持模态 | ZIP 根清单 | 内容 |
|---|---|---|---|
| 引擎直接测量 | text、image、audio、video | samples.json | 一份待测批次及其媒体；不预填 measurements |
| 已有指标导入 | text、image、audio、video、benchmark | samples.json | 每条样本的 baseline/candidate 完整指标；可附媒体 |
| 训练对比协议 | text、image、audio | protocol.json | 待测训练数据 D_ours、对照训练数据 D_ref、固定测试集 E；文本另含下游任务训练集 T |

普通 ZIP 的统一布局：

```text
dataset.zip
├─ samples.json
└─ assets/             # 有媒体时提供
   ├─ item_001.jpg
   └─ ...
```

训练协议 ZIP 的统一布局：

```text
training.zip
├─ protocol.json
├─ ours/
├─ ref/
└─ eval/
```

文本的下游任务训练数据另放 task/train.jsonl。目录名称由 protocol.json 引用决定，规范固定用上述名称以便前端打包。不要把 samples.json 与 protocol.json 两类入口混在一个包里，期待后端自动识别运行方式。

## 2. 通用打包规则

1. JSON/JSONL 使用 UTF-8，JSON 采用合法标准语法，无注释、尾逗号、NaN、Infinity。本文示例可解析；路径、标签和内容需替换为真实业务数据。
2. 新包统一把主清单放 ZIP 根目录，不额外包一层同名目录。现有部分入口支持顶层目录，但按根清单打包可避免普通数据目录导入与算子路径解析差异。
3. 包内路径统一使用 `/`。普通 samples.json 的 `file` 写相对 ZIP 根的路径；训练协议配置路径相对 protocol.json，音频训练清单里的 `audio` 相对其所在 JSONL 文件。
4. 包内只放本次输入。不要放 node_modules、.git、缓存、执行结果或嵌套 ZIP。不得包含 `..` 路径、绝对 ZIP 条目或符号链接。
5. 每份清单中 id 使用非空唯一字符串；input 使用非空字符串；highValue 使用 JSON 布尔值，不使用字符串 `"true"`/`"false"`。
6. 默认上传文件上限200MB、ZIP条目上限5000、普通批次样本上限5000，均可由服务端环境变量调整。200MB为上传文件大小限制，不应解释为已实现的解压总量限制。不要把模型权重默认塞进数据 ZIP。
7. 模态、运行模式、算子ID和判定配置由请求指定；samples.json 内自加 type/mode/schemaVersion 等字段不会自动选择算子或切换执行模式。
8. 标准优先采用 JSON 主清单；不要把 CSV 的字段别名当作所有模态的完整公共协议。TXT 只能表达文本行，不能携带完整媒体标注或成对指标。

## 3. 普通 samples.json 的公共格式

```json
{
  "samples": [
    {
      "id": "S001",
      "input": "待评测文本或assets中的媒体路径",
      "title": "展示标题",
      "scene": "业务场景",
      "sourceStep": "数据清洗",
      "parameters": {},
      "highValue": false
    }
  ]
}
```

本规范要求 id/input；其余展示字段可选。媒体样本另带 `file`，值与 input 中的媒体路径一致。不能只依赖 audio、input 或 fileName 的各种兼容别名在所有上传入口表现一致。

### 3.1 文本直接测量

```text
text.zip
└─ samples.json
```

```json
{
  "samples": [
    {"id":"TXT001","input":"企业发布了新的季度经营报告。","scene":"财经"},
    {"id":"TXT002","input":"球队在决赛中获得冠军。","scene":"体育"}
  ]
}
```

至少需要两个按 NFKC/去空白后仍不同的内容组，才能建立当前在线入口的独立验证划分。此入口是 MLM 代理测量；若要用相同下游任务验证 D_ours 与 D_ref 的训练效果，使用第5节训练协议。

### 3.2 图像直接测量

```text
image.zip
├─ samples.json
└─ assets/
   ├─ IMG001.jpg
   └─ IMG002.png
```

```json
{
  "samples": [
    {"id":"IMG001","input":"assets/IMG001.jpg","file":"assets/IMG001.jpg","scene":"道路"},
    {"id":"IMG002","input":"assets/IMG002.png","file":"assets/IMG002.png","scene":"室内"}
  ]
}
```

推荐 JPG/PNG，现有解析和图像协议也支持 JPEG/WEBP/BMP。文件必须可解码。直接测量入口不消费人工分割 mask 作为真实标注 IoU；需要按有标签分类数据比较训练质量时，使用第6节。

### 3.3 音频直接测量

```text
audio.zip
├─ samples.json
└─ assets/
   ├─ AUD001.wav
   ├─ AUD002.wav
   ├─ AUD003.wav
   └─ AUD004.wav
```

```json
{
  "samples": [
    {"id":"AUD001","input":"assets/AUD001.wav","file":"assets/AUD001.wav","transcript":"请打开客厅的灯。","speaker":"spk01","domain":"home","language":"zh"},
    {"id":"AUD002","input":"assets/AUD002.wav","file":"assets/AUD002.wav","transcript":"请关闭卧室空调。","speaker":"spk01","domain":"home","language":"zh"},
    {"id":"AUD003","input":"assets/AUD003.wav","file":"assets/AUD003.wav","transcript":"请查询列车到站时间。","speaker":"spk02","domain":"travel","language":"zh"},
    {"id":"AUD004","input":"assets/AUD004.wav","file":"assets/AUD004.wav","transcript":"导航到最近的车站。","speaker":"spk02","domain":"travel","language":"zh"}
  ]
}
```

- 媒体与参考转写 transcript 用于 CER；speaker 用于说话人代理诊断；domain 用于领域代理诊断。
- 若要求当前入口三项指标齐全，speaker 和 domain 各自都需至少2类、每类至少2条，并且样本带齐标签。一条音频不能完成这些整批留一验证指标。
- 推荐可解码的 WAV/PCM；MP3/FLAC 是否能解码还取决于服务端音频依赖。ASR 模型由服务端准备。
- 本节三个指标与第7节训练协议的 ASR CER 对比不是同一个评测流程。

### 3.4 视频直接测量

```text
video.zip
├─ samples.json
└─ assets/
   ├─ VID001.mp4
   └─ VID002.mp4
```

```json
{
  "samples": [
    {
      "id":"VID001",
      "input":"assets/VID001.mp4",
      "file":"assets/VID001.mp4",
      "caption":"白天道路上车辆行驶。",
      "source":"https://example.org/dataset/VID001",
      "expectedDuration":10.0,
      "expectedFrames":250,
      "highValue":false
    }
  ]
}
```

caption、source、预期时长/帧数、高价值标记是可选但有意义的证据字段。source 应填写真实来源，示例地址仅示范格式；时长单位秒，帧数填正数且来自真实元数据，不要统一写死。推荐 MP4，MOV/WEBM/AVI/MKV 需服务端解码器支持。该入口直接评价输入视频，不要求 D_ref 或模型训练集。

真实 MOS 标签不由 samples.json 中的 mos 字段或 params.protocol 自动消费。SRCC/PLCC 使用现有离线 `tools/run_video_vqa.py --videos <目录> --labels <文件>`；标签可用 CSV `video,score`，按文件名匹配，至少匹配3条才有相关性结果。若前端需要 ZIP 内 MOS 自动联调，这是额外的入口适配工作，不能仅添加文件名就视作已支持。

### 3.5 benchmark 逐题分数导入

```text
benchmark.zip
└─ samples.json
```

```json
{
  "samples": [
    {
      "id":"Q001",
      "input":"水在标准大气压下的沸点是多少摄氏度？",
      "output":"100摄氏度",
      "scene":"常识",
      "measurements":{
        "baseline":{"score":70},
        "candidate":{"score":85}
      }
    }
  ]
}
```

score 为同一道题两个模型的0–100有限数值，不能用字符串或布尔值。只有问题与答案而无两侧得分，当前 benchmark 不会自动调用模型补分。output/scene 推荐提供；其余模型版本和上游评分协议需另有可追溯记录。

## 4. 各模态已有指标导入

仍使用普通 ZIP 和 samples.json，每条添加 measurements，两侧完整填写：

| type | baseline 与 candidate 各自必须包含 | 数值单位 |
|---|---|---|
| text | accuracy、f1、lossCv | accuracy/f1 为0–100；lossCv当前协议为0–1 |
| image | iou、clarity、semantic、accuracy | iou为0–1；其他为0–100 |
| audio | cer、speakerAccuracy、domainF1 | CER为非负有限百分数，可大于100；其他为0–100 |
| video | clarity、alignment、trust、safety、integrity | 五项均0–100 |
| benchmark | score | 0–100 |

例：文本指标字段为 `"measurements":{"baseline":{"accuracy":77,"f1":78,"lossCv":0.18},"candidate":{"accuracy":85,"f1":86,"lossCv":0.15}}`。不得将80%写成0.8；IoU的0.85则保持比例单位。

音频可选在每侧提供 cerEdits、cerReferenceChars；两者为整数，前者≥0、后者>0，且 CER 与两者计算值一致。仅在全部样本两侧均提供完整计数时采用语料级编辑数/参考字数聚合。

已有指标是 imported 来源，不表示模型在本服务真实执行。引擎测量包应去掉 measurements，避免部分入口优先消费完整导入分数。Demo 下载模板含固定评分和 demo:// 地址，应与真实测量包区分。

## 5. 文本训练对比 ZIP

```text
text-training.zip
├─ protocol.json
├─ ours/
│  └─ corpus.jsonl
├─ ref/
│  └─ corpus.jsonl
├─ task/
│  └─ train.jsonl
└─ eval/
   └─ test.jsonl
```

protocol.json：

```json
{
  "protocol":{
    "oursCorpus":"ours/corpus.jsonl",
    "refCorpus":"ref/corpus.jsonl",
    "trainFile":"task/train.jsonl",
    "evalFile":"eval/test.jsonl",
    "task":"tnews",
    "seeds":[42,43,44],
    "purpose":"quality",
    "taskEpochs":3
  }
}
```

模型默认来自 `SME_EVAL_TEXT_001/models/bert-base-chinese`，不需要在ZIP或前端表单中填写modelPath。modelPath/tokenizerPath仅用于显式高级覆写，路径必须在服务器实际存在；不能使用浏览器本机路径。模型与分词器资产随算子交付，不放进输入数据ZIP。

ours/ref 语料 JSONL 每行推荐 `{"id":"O001","text":"一段真实的训练语料。"}`。这两份是继续预训练语料，不要求下游标签。当前训练语料读取器读取 JSON/JSONL；不要把普通文本每行直接放进去并只改为 .txt。

task/train.jsonl 与 eval/test.jsonl 使用相同任务字段：

| task | 每行标准结构 |
|---|---|
| tnews | `{"id":"T001","text":"新闻文本","label":0}` |
| afqmc | `{"id":"T001","text1":"第一句","text2":"第二句","label":1}` |
| cluener | `{"id":"T001","text":"张三来到北京","entities":[{"start":0,"end":2,"label":"name"}]}` |

TNEWS使用统一的非负整数类别标签，AFQMC标准标签0/1。CLUENER实体范围为Python字符位置的左闭右开区间，end不包含在实体内。不要把原生CLUENER嵌套label字典直接当作上述entities清单。

两侧都在同一份 task/train 上做下游训练，最终在固定 eval/test 上比较。测试标签必须真实有效，label=-1会被跳过；E不得与T或任一预训练语料存在当前规范化内容重复。每份任务清单内部ID唯一，跨集合泄漏依据规范化内容判断，不依赖ID相同与否。更换ID不能绕过内容审计。

## 6. 图像训练对比 ZIP

```text
image-training.zip
├─ protocol.json
├─ ours/
│  ├─ cat/
│  │  └─ O001.jpg
│  └─ dog/
│     └─ O002.jpg
├─ ref/
│  ├─ cat/
│  │  └─ R001.jpg
│  └─ dog/
│     └─ R002.jpg
└─ eval/
   ├─ cat/
   │  └─ E001.jpg
   └─ dog/
      └─ E002.jpg
```

```json
{
  "protocol":{
    "oursDir":"ours",
    "refDir":"ref",
    "evalDir":"eval",
    "proxyModel":"resnet18",
    "task":"classification",
    "trainingMode":"full",
    "epochs":3,
    "batchSize":32,
    "seeds":[42,43,44],
    "purpose":"quality"
  }
}
```

- 三个目录均至少2类，类别目录名完全相同；标签来自目录名，不靠文件名或 samples.json 的label字段。
- 图片直接放在类别目录内；当前读取器不递归扫描类别目录的更深层子目录。
- ours/ref每类样本数相同；测试数据不得与任一训练侧精确或近重复。
- 这是当前有标签分类协议，不是标注mask的分割训练协议。不会因填了iou字段自动变成分割任务。
- 默认从 `SME_EVAL_IMAGE_001/models/<proxyModel>/` 读取随算子交付的预训练权重；可选指定modelWeight覆写。benchmark字段是协议数据域标识，设置cifar10等名称不会自动下载数据。

## 7. 音频训练对比 ZIP

```text
audio-training.zip
├─ protocol.json
├─ ours/
│  ├─ manifest.jsonl
│  └─ audio/
│     └─ O001.wav
├─ ref/
│  ├─ manifest.jsonl
│  └─ audio/
│     └─ R001.wav
└─ eval/
   ├─ manifest.jsonl
   └─ audio/
      └─ E001.wav
```

```json
{
  "protocol":{
    "oursManifest":"ours/manifest.jsonl",
    "refManifest":"ref/manifest.jsonl",
    "evalManifest":"eval/manifest.jsonl",
    "seeds":[42,43,44],
    "purpose":"quality",
    "epochs":1,
    "language":"chinese"
  }
}
```

每份清单每行使用以下结构；ref/eval相应换成自己的ID、文件、转写和说话人：

```json
{"id":"O001","audio":"audio/O001.wav","text":"请打开客厅的灯。","speaker":"train-spk01"}
```

audio相对该清单，所以这里写 audio/O001.wav，而不是 ours/audio/O001.wav。id、有效text、真实speaker和本地可解码audio均需具备；本入口不下载远程音频。每份清单1–5000条，单条时长0.05–30秒且不能静音；长音频须预先切分。读取后统一16kHz单声道。

E与训练两侧必须说话人和样本独立，speaker ID跨三份清单使用相同命名空间，不能对同一人换ID伪造独立。两侧训练条数须相等，总训练时长须满足协议匹配要求，默认差距不超过5%。此协议当前是Whisper ASR训练与CER对比，不训练CNN-LSTM，也不输出三项普通音频指标的训练收益。模型默认来自 `SME_EVAL_AUDIO_001/models/whisper-base`，modelDir仅作可选高级覆写。

## 8. 前端与算子入口对接

### 8.1 普通 ZIP：当前业务前端已使用的路径

1. `POST /api/v1/datasets/import`，multipart字段file、type、mode。引擎测量用mode=unmeasured；已有指标用measured；Demo用demo。
2. `POST /api/v1/eval/tasks`，指定type、mode=measured、source.kind=raw及导入返回的id。引擎测量任务加measurementSource=engine，已有指标任务用import；autoRun控制是否执行。

### 8.2 训练协议 ZIP：当前算子入口

1. `POST /uploadFile?uniqueId=...`，multipart字段file上传训练ZIP，保存响应中的inputFile。
2. `POST /predict`：

```json
{
  "uniqueId":"TEXT-TRAIN-001",
  "operatorId":"SME_eval_text",
  "inputFile":"/workspace/TEXT-TRAIN-001/input/text-training.zip",
  "params":{
    "mode":"measured",
    "protocol":true,
    "registerTask":true
  },
  "isLastOperator":true
}
```

inputFile必须使用实际上传响应值，不自行拼接。图像、音频分别换operatorId为SME_eval_image、SME_eval_audio。params.protocol=true才会从ZIP中寻找唯一protocol.json；不指定会进入普通样本解析。视频与benchmark不支持这个训练协议入口。

不要把params.protocol直接改为协议对象来绕过配置读取并继续期待ZIP自动解压：当前对象分支以已上传文件父目录为基准，不负责解压ZIP。需要ZIP资产时统一使用true和包内protocol.json。

业务任务接口也支持直接传protocol对象，但其相对路径以SME上级项目目录为准；该路径不是上传ZIP解析入口。当前新前端训练协议表单正是提交服务器路径的protocol对象，尚未通过该表单执行“上传protocol ZIP→解包→创建协议任务”的自动流程。前端如要直接上传本规范的训练ZIP，需接入上述uploadFile/predict流程或另行实现业务层适配。

## 9. 模式选择小结

- 只想上传原始数据让现有引擎测量：samples.json + assets；媒体同时提供input/file。
- 已有真实baseline/candidate指标：samples.json逐条带完整measurements。
- 想比较D_ours和D_ref训练后的效果：文本、图像、音频使用protocol.json，并提供各自规定的独立数据；不能统一为四份任意文件。
- 视频：待测视频和证据元数据；MOS关联另走离线标签入口。
- benchmark：同一批题目的两侧真实得分；不在本算子内自动训练或答题。

结构通过不代表真实媒体可解码、标签有效、无泄漏、模型可用或训练协议已经通过。本规范与验证只覆盖输入结构、路径及解析边界，不代替真实训练验收。

## 10. 本次核验

2026-11-06 使用临时目录和独立数据库完成37项检查：11个JSON示例可解析；五类普通ZIP可由算子解析器和业务导入接口读取；图像/音频/视频资源路径可转换为可访问的资产地址；视频元数据保留；benchmark成对分数可校验；三类训练ZIP可读出协议并构建参数对象；文本任务行、图像类别目录和音频清单相对路径可由对应读取器解析。检查未执行模型、视频解码或训练。

复现脚本及详细结果保存在项目上级 input_zip_review_20261106/verify_input_contracts.py 与 verification.json。本次未修改算子实现或前端实现。
