# 语音加扰算子 · 服务器部署包

> 2026-11-13 接口更新：现提供六个接口。工程侧下载采用 `GET /downloadFile?path=完整返回路径`，成功响应为文件流；先重建新版入口，再重新上传并使用本轮 outputs 路径串联。详细步骤见 [下载与远程链路修复说明](docs/downloadFile与远程链路修复-2026-11-13.md)。

整理日期：2026-11-12。来源：相邻 `AVP` 开发源包。此目录用于上传服务器并构建运行镜像。

2026-11-13 启动修复：Compose 日志级别改为 `INFO`，入口兼容大小写日志级别；已部署实例的免重建镜像恢复命令见 [日志级别修复说明](docs/启动日志级别修复-2026-11-13.md)。

## 部署参数

| 项目 | 值 |
|---|---|
| 容器名 | `op-speech-adversarial` |
| 对外端口 | `17023` |
| 容器内端口 | `8000`，映射 `17023:8000` |
| 镜像 | `audio-adversarial-pipeline:v1.0.0` |
| 算子数 | 11，共用 `/predict`，通过 `operator` 字段路由 |
| 首次部署运行目录 | 在服务器新建 `./data`，挂载到容器 `/data` |

## 包内内容

```text
op-speech-adversarial/
├── Dockerfile、docker-compose.yml、.dockerignore、requirements.txt
├── inference.py、map.json
├── common/                    公共运行代码
├── AVP_*/
│   ├── processor.py           算子运行代码
│   ├── configs/config.yaml    算子自带运行配置
│   ├── dataset/DATA_SCHEMA.md 字段契约文档，目录内没有样本数据
│   ├── README.md、OPERATOR_MANUAL.md
│   └── avp_report_pdf.py      仅 11 号算子包含，PDF 生成模块
├── weight/assets/             03 号运行必需的 5 个噪声 WAV 和来源说明
├── tools/http_smoke.py        标准库 HTTP 验收客户端
├── 服务器部署说明.md、交接说明.md
├── 接口文档-前端联调.md、语音加扰算子-接口与对接说明.md
├── docs/部署包验收记录-2026-11-12.md
└── 部署包清单.json             文件 SHA-256 校验清单
```

本包不携带 `data/`、公共 ASR 模型 `weight/models/`、备用 `weight/assets_synthetic/`、音频样本、测试代码、历史输出、缓存、原型页面和离线 ASR 评测工具。`requirements.txt` 仅保留运行依赖。

模型依赖核对结果：01–11 使用 DSP/规则处理与报告生成，运行时不加载 ASR 模型。各算子 `configs/config.yaml` 是运行配置；原开发目录的 `output/model/config.json`、`metrics.json` 是交付指标/配置快照，不是必须加载的推理权重。
`weight/assets/` 是音频加扰素材，03 号算子按其现有配置直接读取，必须保留。07 号只接收外部评测结果，正式 ASR 评测由外部评测系统承担。

## Linux 服务器首次启动

将整个目录上传或解压到服务器后，在包根执行；以下示例目录可按实际位置调整：

```bash
cd /opt/op-speech-adversarial
mkdir -p data
sudo chown 1000:1000 data
sudo chmod 750 data
docker compose config
docker compose up -d --build
docker compose ps
curl --fail --show-error http://127.0.0.1:17023/health
```

预期探活返回 `status=1`、`operators=11`。构建机器需能够获取基础镜像、系统包及 Python 依赖；运行时不下载模型。离线镜像交付、目录权限、日志与回退见 [服务器部署说明](服务器部署说明.md)。

## 前后端接入

业务后端配置算子基址 `http://<算子服务器IP>:17023`；11 个算子均调用该基址的 `/predict`。已联调 Java 平台使用 `SPEECH_ADV_OPERATOR_BASE` 配置基址，`SPEECH_ADV_OP_RUNTIME_DIR` 配置其可读取的算子数据目录。
跨服务器部署时，业务后端需挂载同一份数据，才能读取 `/data/...` 对应的产物。前端继续通过业务后端访问和下载文件。

- [后端接口与对接说明](语音加扰算子-接口与对接说明.md)
- [前端联调接口说明](接口文档-前端联调.md)
- [交接说明](交接说明.md)
- [部署包验收记录](docs/部署包验收记录-2026-11-12.md)

## 上传前校验

本目录的 `部署包清单.json` 记录全部交付文件的大小和 SHA-256（不包含清单自身）。压缩包与校验文件放在本目录外，不会混入 Docker 构建上下文。
本机没有 Docker；本次验证以本地运行代码、全链路调用与 HTTP 接口为准，目标服务器仍需实际执行镜像构建及容器健康检查。
