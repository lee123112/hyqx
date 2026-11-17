# PRLt_v1.0 整理后目录

```text
PRLt_v1.0/
├─ PRLt_*/                  # 23个注册算子：processor.py、局部API、utils、configs及参数文档
├─ common/                  # 统一回包与增强运行依赖
├─ models/                  # 原3组本地模型、适配器、原型与溯源文件，内容未改
├─ weights/                 # 原兼容适配器与说明，内容未改
├─ examples/
│  └─ smoke_one_pair.csv     # 唯一保留的最小工程联调输入（1条）
├─ work/pool/               # 空运行目录，服务运行时自行生成任务
├─ inference.py             # 根服务和算子调度，保留最新异常回包修复
├─ ingest.py                # 正式输入归一
├─ main.py                  # 正式启动入口
├─ map.json                 # 原23算子注册表
├─ quality_scoring_config.json
├─ requirements.txt         # 原依赖要求
├─ Dockerfile / docker-compose.yml / .dockerignore
├─ .env / .env.example      # 沿用原配置，不新增入站令牌
├─ verify_models.py         # 原模型检查入口，非算法调用依赖
├─ README.md / MODEL_SERVICES.md / DIRECTORY_STRUCTURE.md
├─ PREDICT_JAVA_CONTRACT.md  # 工程接入契约
├─ LOCAL_SMOKE_RESULT.txt   # 本次单条真实HTTP链结果摘要
└─ PACKAGE_MANIFEST.json / PACKAGE_SHA256.txt
```

算子中的 `configs/` 全部保留，包括同义词库及其溯源文件；`utils/`源码不裁剪。算子局部README与dataset/DATA_SCHEMA.md保留供参数/格式参考，历史sample数据已移出。

包内已移出的内容：本机虚拟环境、tests、tools、旧scripts、handoff_evidence、__pycache__、.pytest_cache、_eval、output、历史work任务、批量样例和旧交接资料。所有移出项均有包外位置和清单，可追溯恢复。

本机运行环境独立位于同级“运行环境”目录；归档、输入测试、原始响应及日志独立位于同级“归档”和“联调数据/local_test”。这些都不属于工程运行包。
为保持模型目录逐文件一致，models/内原有2个适配器编译缓存也原样保留；其余算子/公共目录的缓存已移出。
