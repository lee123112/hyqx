# SME 算子部署交接包

版本：SME-2026.11.12-server。部署从本目录开始。包含五个算子及共享后端、私有模型、前端源码/生产构建和12个可上传输入样例。

**服务器约定：入口容器名 `op-pre-eva`，统一对外端口 `17024`。页面、业务 API、算子 API 均访问 `http://服务器地址:17024`。** 内部后端容器为 `op-pre-eva-backend`，其6289端口不发布到宿主机。详细步骤见[服务器部署联调](docs/服务器部署联调.md)。

工程接口、三种ZIP、前端业务调用、完整curl示例与测试用例见[接口说明与联调测试](docs/09_接口说明与联调测试.md)，另提供可离线打开的[HTML阅读版](docs/09_接口说明与联调测试.html)。

## 一、交付结构与边界

```text
SME/
├─ app/、common/、configs/、map.json    后端及算子注册
├─ SME_EVAL_TEXT_001/                  BERT模型与文本算子
├─ SME_EVAL_IMAGE_001/                 ResNet18/50、MobileNetV2与图像算子
├─ SME_EVAL_AUDIO_001/                 Whisper-base/small、Paraformer与音频算子
├─ SME_EVAL_VIDEO_001/                 CLIP及词表与视频算子
├─ SME_EVAL_BENCHMARK_001/             后训练区分度，导入评分，无模型
├─ frontend/src/、frontend/dist/       前端源码及同源API生产构建
├─ dataset/reference/                 在线文本测量所需参考语料
├─ dataset/business/                  工程侧业务数据挂载位置
├─ examples/                          12个ZIP及索引
├─ runtime/                           空目录，运行后产生数据库/上传/结果
├─ deploy/nginx.conf                  静态页面、SPA回退及API反向代理
├─ tools/                             Linux部署/启动、文件/依赖/模型校验、HTTP联调
├─ Dockerfile、docker-compose.yml
├─ .env.example
└─ 交付文件清单.json                    文件大小及SHA-256
```

模型共47个实体文件、约3.52GiB，各算子的models/manifest.json记录散列。不要只复制某个算子文件夹：平台适配器仍依赖随包的app/common/configs。正常运行无需任何原SME目录、公共weight目录或开发机G盘路径。

包内未包含开发机数据库、历史输出、缓存、node_modules、真实.env、旧output/model占位文件。前端生产资源已构建，不需要在部署机安装Node才能展示页面。模型文件已包含，Python依赖及Docker基础镜像未离线打包。

## 二、Docker Compose部署

服务器安装Docker及Compose v2，在本目录执行：

```sh
python3 tools/verify_package.py
sh tools/deploy.sh up
curl http://127.0.0.1:17024/health
sh tools/deploy.sh environment
sh tools/deploy.sh models
```

页面：`http://服务器地址:17024`；接口文档：`http://服务器地址:17024/docs`。health成功信封code=0，data.operatorCount应为5。前端由Nginx静态托管，不使用Vite开发服务或preview。启动脚本在.env不存在时创建模板，不覆盖已有配置，并等待两个服务的健康检查。

- .env中的SME_HTTP_PORT默认17024，另可配置API Key、上传上限、并发、CPU线程数和依赖包源。配置SME_API_KEY后，前端右上角“设置”填同一Key，工程接口写操作带X-API-Key。包里未配置或保存任何真实Key。
- runtime挂载到/app/data，存放SQLite、工作区、上传及结果；dataset挂载到/app/dataset并只读。替换或重建容器不会删除这两个宿主目录。
- 业务数据放dataset/business，可在训练协议表单中引用容器侧/app/dataset/business/...路径。不能填写浏览器本机文件路径。
- 前端代理包含/api、/health及四个算子接口；predict超时1800秒，后端单文件上限200MB，Nginx请求上限256MB以保留multipart开销。若修改SME_MAX_UPLOAD_MB，也应同步deploy/nginx.conf的client_max_body_size。
- 日志：`docker compose logs -f backend`。停止服务：`docker compose down`；不会删除相对挂载的runtime数据。

首次构建需要可用Python包源和Docker镜像源。默认安装CPU版torch/torchvision，不要求GPU；PIP_INDEX_URL与TORCH_INDEX_URL可切换内网包源。工程内网也可预构建镜像再导入；本包不是docker save产物。默认Whisper不需FunASR，Paraformer启用前另装requirements-asr.txt并匹配torch/torchaudio，不能仅切换路径就假定依赖齐全。

图像下拉中的deit_tiny是扩展选项，本次本地模型没有该权重，默认依赖也未包含timm；该选项需工程侧另配。随包图像模型为ResNet18/50、MobileNetV2，训练交付验收使用ResNet18。

## 三、裸机启动

Python3.11，在本目录创建独立环境并安装requirements.txt。Linux示例：

```sh
python3.11 -m venv .venv
. .venv/bin/activate
python -m pip install 'torch>=2.2' 'torchvision>=0.17' --index-url https://download.pytorch.org/whl/cpu
python -m pip install -r requirements.txt
export PYTHONDONTWRITEBYTECODE=1
export SME_DATA_DIR="$PWD/runtime"
export SME_WORKSPACE_DIR="$PWD/runtime/workspace"
export SME_DB_PATH="$PWD/runtime/evaluation.db"
export SME_DATASET_DIR="$PWD/dataset"
export HF_HUB_OFFLINE=1
export TRANSFORMERS_OFFLINE=1
sh tools/start_backend.sh
```

裸机建议在安装requirements前先用CPU wheel源安装torch/torchvision，具体命令见部署手册。启动脚本默认监听17024，可用SME_PORT覆盖；仅启动后端时根路径是服务信息，不是Vue页面。Windows可运行 `powershell -ExecutionPolicy Bypass -File tools/start_backend.ps1`，可用-Python指定解释器，默认端口同为17024。

裸机前端使用系统Nginx托管frontend/dist；deploy/nginx.conf是Compose版本，裸机使用时把静态root改为实际frontend/dist位置、listen改为17024、backend:6289改为127.0.0.1:6289，并以SME_PORT=6289启动后端。必须保留SPA路由回退和API代理。

如需修改前端：安装Node20或以上，在frontend执行npm ci、npm run build。已锁定依赖在package-lock.json；构建时VITE_API_BASE_URL使用/，不要写死开发机后端地址。

## 四、工程侧算子调用

注册信息以map.json为准：SME_eval_text、SME_eval_image、SME_eval_audio、SME_eval_video、SME_eval_benchmark；对应SME_EVAL_*_001算子包。可沿用平台类适配器，也可调用根HTTP服务。

1. POST /uploadFile?uniqueId=...，multipart字段file上传ZIP。
2. POST /predict，传uniqueId、operatorId、上传响应的data.inputFile、params和isLastOperator。
3. 通过响应outputPath调用GET /downloadFile?uniqueId=...&path=...；isLastOperator=true时也可返回Base64输出ZIP。
4. 下载保留后，POST /removeFile传uniqueId清理工作区，不会删除算子内模型。

普通原始数据：params.mode=measured、measurementSource=engine；已有指标：measurementSource=import。训练ZIP：params.mode=measured、protocol=true；包内protocol.json省略模型字段会自动使用所属算子内模型。完整字段见docs/02_接口契约.md与docs/07_各模态输入与ZIP打包规范.md。

## 五、最小联调样例

先检查模型：`python tools/verify_models.py`，期望status=passed、modelFiles=47。此命令仅用标准库，不联网。

服务就绪后运行：

```sh
python tools/run_example.py --case import-benchmark
python tools/run_example.py --case engine-image
python tools/run_example.py --case training-text
python tools/run_example.py --all --download --cleanup
```

默认请求127.0.0.1:17024，支持--url指定目标服务。后端有Key时设置当前进程SME_API_KEY。脚本仅用标准库，会上传、执行、登记任务，在runtime/manual_tests保存响应。--download下载并校验产物，--cleanup在下载验证后清理服务端工作区；失败时保留工作区便于排查。训练会实际执行模型计算。

Compose内执行：`sh tools/deploy.sh smoke --case training-image --download`，或`sh tools/deploy.sh smoke --all --download --cleanup`。脚本已指定容器内6289地址。样例ID和路径见examples/index.json和examples/README.md；共12例。前三模态训练为极小预算探索，E数量4/20/2，不代表正式质量验收。benchmark只有导入评分。

前端普通样例按对应评测方法选ZIP。训练页面当前使用服务器路径表单，训练ZIP应走上述算子API，不应上传给普通samples解析器。

## 六、结果与数据说明

输出ZIP保留输入资源并使用包内相对引用，模型只显示operator-model标识；下载后清理工作区仍可读取包内资源。训练rowsIn/rowsOut以固定E计数，不虚构逐样本结果。

内置实测基线是随源工程交付的校准快照，不是新部署现场运行结果。必要的文本参考语料来自原工程，来源与使用范围见dataset/reference/README.md。样例用于联调，不作为正式业务验收数据；报告中的代理指标、探索用途、accepted=null或未达标应原样解释。

## 七、交付校验范围

交付文件清单.json可用于逐文件校验，外层SHA256SUMS.txt用于ZIP传输校验。本包的实际冷启动/模型/接口验收见交付验收.json；来源工程此前回归摘要见docs/来源工程回归摘要.json。

当前构建机没有Docker，Dockerfile/Compose/Nginx提供为部署配置，未声称已在Linux容器内构建或启动。交接人员应在目标服务器执行本节启动检查；交付内的依赖环境记录仅代表已实际验证的本机组合，不代替目标机安装验证。

2026-11-11历史验收：从非工程工作目录导入本包、全新临时库启动，5算子注册成功，12个样例通过。当天完整记录保留在docs/历史交付验收_20261111.json。本次重新整理后的实际验证范围与结果以根目录交付验收.json为准，不把历史结果记为当天运行。
