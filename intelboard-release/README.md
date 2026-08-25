# IntelBoard 0.2.0

面向长期项目跟踪的 **本地 AI 情报看板**。当前版本按你的硬件基线设计：

- QNAP TS-673A / AMD Ryzen V1500B
- NVIDIA GTX 1650 4GB
- 40GB RAM
- QNAP Container Station / Docker Compose
- 数据目录统一使用 `/share/Container/intelboard`

内置《综合交接文件 V2.8》作为初始知识基线，之后每天联网获取新证据并持续积累。

## 已包含

- 完整 Web 前端：总览、五大专题、系统状态
- FastAPI 后端
- 本地 SQLite/WAL 知识数据库（单机 NAS 低维护、无需额外数据库容器）
- V2.8 历史知识基线导入
- 每天 **03:00** 自动完整检索
- 全部 / 单专题 **立即刷新**
- 联网搜索 + 官方入口定向抓取
- HTML / PDF 文本提取
- 原始 HTML/PDF + 提取全文本地归档
- URL / 内容 Hash 去重
- A / B+ / B / C 来源分级
- 搜索失败与“未发现”记录
- P / E / T 阶段规则
- 双时间戳：完整增量检索 / 专项复核
- 到期节点自动追加复核检索词
- AI 变化总结、趋势判断、预测、下一观察节点
- 人工确认 / 排除证据
- 历史快照
- 一键下载知识库备份
- Ollama 本地模型，不调用云 AI
- QNAP GPU Compose + CPU 兜底 Compose
- GitHub Codespaces
- GitHub Actions 自动构建推送 Docker Hub（linux/amd64）

## 40GB RAM 模型策略

默认 `OLLAMA_MODEL=auto`：

- **≥36GB RAM**：`qwen3:30b-a3b-instruct-2507-q4_K_M`（约19GB，主分析模型）
- ≥20GB：`qwen3.5:9b`
- ≥12GB：`qwen3.5:4b`
- 更低：`qwen3.5:2b`

你的 40GB TS-673A 会自动选 30B-A3B Instruct Q4。系统不会把几十篇网页逐篇交给大模型，而是先由程序抓取、去重、筛选，最后每个专题做一次综合分析；所有专题严格顺序运行，并发为 1。

为了给 QTS、文件缓存和抓取任务留余量，默认把模型上下文限制为 8192 tokens。

## QNAP 持久化

```text
/share/Container/intelboard/data      # 数据库、证据全文、备份
/share/Container/intelboard/ollama    # 本地模型
```

访问：

```text
http://NAS_IP:8733
```

详细部署见 `QNAP-DEPLOY.md`。

## Codespaces / Docker Hub

详细流程见 `CODESPACES-DOCKERHUB.md`。

## 本地开发验证

```bash
python -m venv .venv
. .venv/bin/activate
pip install -r requirements-dev.txt
MOCK_AI=true AUTO_PULL_MODEL=false pytest -q
MOCK_AI=true AUTO_PULL_MODEL=false python scripts/smoke_test.py
```

当前交付环境若没有 Docker daemon，只能完成应用实跑、单元测试、联网抓取及 Compose 静态解析；Docker 镜像的真实 build/run 应在 Codespaces 的 Docker 环境继续执行，项目已提供对应配置与命令。
