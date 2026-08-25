# InternetBoard 0.4 验证项

- Python 全项目 compileall。
- pytest：基础规则、分块完整性、旧数据库迁移、持久队列恢复、来源去重 / 变化、刷新全链路、Web 模板渲染。
- Compose YAML 静态解析。
- NVIDIA GPU reservation 保留。
- Ollama 请求 `truncate=false`。
- 4B / 27B 分层模型和 GPU fallback。
- 分块账本 + 任务队列双层断点恢复。
- 页面版本 / 转载来源组 / Claim 关系数据结构。
- FTS + 可选 Qwen3 Embedding 混合知识检索。
- 人工知识自动提炼、人工修改优先、版本历史。
- 每日运行前 SQLite 一致性备份与保留策略。
- GitHub Actions 使用 Node 24 版本 Action。
