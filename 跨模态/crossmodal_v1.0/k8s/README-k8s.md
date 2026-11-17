# Kubernetes 联调部署

本目录提供 `Deployment + Service` 示例，服务直接运行池化入口。
部署前将 `opcrossmodalpool.yaml` 中的镜像地址和各资源的 `namespace` 改成实际值，
按环境调整输出卷与资源配额。Kubernetes 不自动读取包根 `.env`，运行配置写在清单 `env` 中。

```bash
docker build -t <registry>/<repo>/opcrossmodalpool:1.0.0 .
# 按平台流程上传镜像，然后由部署人员执行：
kubectl apply -f k8s/opcrossmodalpool.yaml
kubectl rollout status deployment/opcrossmodalpool -n <namespace>
kubectl get pods,svc,endpoints -n <namespace> -l app=opcrossmodalpool
```

示例 Service 为 `ClusterIP`，集群内端口 `17059` 转容器 `8100`。
健康探针使用 `/health`；`/ready` 和 `/operators` 用于查看子算子状态。
本地联调可由部署人员建立端口转发：

```bash
kubectl port-forward -n <namespace> service/opcrossmodalpool 17059:17059
curl -fsS http://127.0.0.1:17059/health
curl -fsS http://127.0.0.1:17059/operators
```

浏览器可打开 `http://127.0.0.1:17059/docs` 查看接口。
清单默认使用 `emptyDir` 方便临时联调，Pod 替换后数据不保留；需要跨重建保留产物时改为 PVC。
更新代码后应重新构建并部署镜像，确认 Pod 使用新镜像和当前 Dockerfile 的默认启动命令。
