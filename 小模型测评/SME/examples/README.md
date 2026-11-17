# 联调输入ZIP

共12个样例，文件、算子ID、数据量和SHA-256以index.json为准。

| 目录 | 用例ID |
|---|---|
| 导入已有指标 | import-text、import-image、import-audio、import-video、import-benchmark |
| 引擎测量 | engine-text、engine-image、engine-audio、engine-video |
| 训练对比协议 | training-text、training-image、training-audio |

导入已有指标：根目录samples.json，每条有完整measurements.baseline/candidate；不会重新运行上游模型。引擎测量：根目录samples.json加真实媒体/标注，不预填measurements。训练协议：根目录protocol.json和D_ours/D_ref/E，文本另有共同下游训练集T；默认使用算子内模型，无开发机路径。

在SME根目录执行：

```sh
python tools/run_example.py --list
python tools/run_example.py --url http://服务器IP:17024 --case import-image --download
python tools/run_example.py --url http://服务器IP:17024 --all --download --cleanup
```

Compose容器内使用 `sh tools/deploy.sh smoke --all --download --cleanup`，包装脚本自动使用内部6289端口并继承API Key。输出默认保存在runtime/manual_tests。

--cleanup会先保存并校验output.zip，再移除对应服务端算子工作区，任务记录保留。需要在页面继续查看原输入、复评时，先只用--download保留工作区。

三个训练样例都是极小预算、单种子探索：固定E分别为文本4条、图像20张、音频2条；rowsIn/rowsOut以该E计数。音频样例的两侧训练数据为流程对照，不能解释为真实训练收益。引擎文本输入6条，只有独立验证子集计入汇总，因此rowsOut可以小于rowsIn。

这些用例用于验证接口、模型加载、训练调用和产物，不代替正式数据质量验收。五类普通导入指标及代理口径详见docs/07_各模态输入与ZIP打包规范.md。
