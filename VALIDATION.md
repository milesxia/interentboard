# IntelBoard 0.2.0 交付验证记录

本文件记录交付环境中实际执行过的检查，避免把“静态写了配置”说成“已经实跑”。

## 已实际通过

- Python 语法编译：通过
- 单元测试：8/8 通过
- FastAPI 进程实际启动：通过
- `/healthz`：HTTP 200
- 未登录访问首页：正确 303 跳转登录
- 登录：通过
- 总览页面：HTTP 200，关键控件存在
- 系统页面：HTTP 200
- 手动专题刷新 API：HTTP 200，成功创建异步任务
- Fake 搜索 → 抓取 → 证据入库 → 全文归档 → Mock AI → 快照：端到端测试通过
- 五专题 YAML 加载：通过
- GPU / CPU / Dev 三份 Compose：YAML 静态解析通过
- GPU Compose：NVIDIA device reservation 字段存在
- 40GB 模型自动选择逻辑：测试确认选择 `qwen3:30b-a3b-instruct-2507-q4_K_M`
- 每日调度计算：Asia/Shanghai 下一次正确落在 03:00

## 当前交付环境无法实际执行

### Docker build / docker compose up

当前执行容器没有 Docker / Podman / Buildah，因此无法在这里真实构建和启动镜像。项目已提供 Codespaces Docker-in-Docker 环境和 GitHub Actions，上传 GitHub 后按 `CODESPACES-DOCKERHUB.md` 可继续完成真实 Docker build/run 验收。

### 程序自身的公网联网抓取

当前执行容器的 DNS 被限制，`httpx` 实时抓取官方页面会得到 `Temporary failure in name resolution`。因此交付环境无法声称“程序公网抓取已成功”。搜索/抓取代码、失败记录和超时保护已完成；GitHub Codespaces / QNAP 只要具备正常互联网连接即可实际验证。

### GTX 1650 CUDA / Ollama 30B 实机推理

当前环境没有 QNAP、GTX 1650 和 40GB RAM，无法伪装成 NAS 实机测试。GPU Compose 使用 QNAP 官方当前 Ollama 教程同类的 `deploy.resources.reservations.devices` 配置；QNAP 端仍应按 `QNAP-DEPLOY.md` 检查 Container Station GPU Mode、NVIDIA Driver / NvKernelDriver 与 `nvidia-smi`。
