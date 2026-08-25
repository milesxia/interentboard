# InternetBoard 0.4.0

面向 QNAP NAS 长期运行的本地 AI 情报 / 知识系统。v0.4 重点把“自动搜新闻后总结”升级为可恢复、可追溯、会积累、会二次补搜的长期研究流水线。

## v0.4 核心能力

- **完整性优先**：长文按段落切块并重叠，应用端控制 token；Ollama 请求 `truncate=false`，不允许静默丢掉后半段。
- **分层模型**：Qwen3 4B 负责大量证据提取 / 融合 / 搜索缺口判断；Qwen3.8 27B 只做最终判断和预测。
- **持久任务队列**：刷新、人工知识处理都进入 SQLite 队列；NAS / 容器重启后恢复排队，已完成 chunk 不重算。
- **运行步骤账本**：搜索、抓取、提取、补搜、综合分析都记录进度和状态。
- **二次补搜**：首轮证据提取后，4B 判断仍缺哪些关键证据，最多自动追加 2 条精确查询。
- **页面变化检测**：同一 URL 再次出现时比较新旧版本；小改动只分析新增/变化部分，同时保留完整新版本。
- **转载 / 镜像识别**：内容哈希 + SimHash 识别重复和近重复文章，同源转载不会被误算成多份独立证据。
- **长期 Claim 知识库**：事实、计划、预测、传闻拆成独立 Claim，记录来源、时间、确定性、实体、可信度。
- **知识生命周期**：自动记录 supports / conflicts / supersedes / duplicate；旧知识不删除，只标记被替代。
- **人工知识自动入库**：粘贴新闻或手打情报后自动保存原文、AI 提炼并写入长期知识库。
- **人工修改最高优先**：Claim 可编辑 / 删除 / 查看版本；`human_override` 不会被 AI 自动覆盖。
- **混合历史检索**：关键词规则 + SQLite FTS + 可选语义向量；知识量达到门槛后按需启用 `qwen3-embedding:0.6b`。
- **增量分析**：没有新证据、没有到期节点时跳过 27B 重复推理。
- **搜索健康管理**：记录每个引擎耗时 / 成功率；连续故障自动短时熔断，避免一个失败引擎拖慢整轮任务。
- **持久性能指标**：记录模型用途、prompt / generation tokens、t/s、GPU 层、耗时和成功率。
- **实时状态**：前端使用 SSE 获取任务状态，断开时自动回退轮询。
- **自动备份**：每天 03:00 正式任务前创建 SQLite 一致性备份，默认保留最近 7 份。
- **NAS 保护**：Ollama 30GB、主程序 3GB 的默认内存上限；AI 并发 1，保证 QTS 有资源余量。

## 默认模型

```text
证据提取 / 融合 / 搜索缺口：qwen3:4b-instruct-2507-q4_K_M
最终分析：qwen3.8:27b-q4_K_M
语义向量（达到阈值后按需）：qwen3-embedding:0.6b
```

## 持久化目录

```text
/share/Container/internetboard/data
/share/Container/internetboard/ollama
```

数据库继续使用：

```text
/share/Container/internetboard/data/intelboard.db
```

升级不会删除原有 v0.2 / v0.3 数据；启动时自动执行增量数据库迁移。

## 核心流水线

```text
联网搜索 / 人工输入
        ↓
保存原始全文 + 抓取元数据
        ↓
URL规范化 / 转载识别 / 页面版本变化
        ↓
按段落切块 + 完整性账本
        ↓
Qwen3 4B：逐块事实提取
        ↓
Claim 长期知识库
        ↓
首轮知识缺口判断 → 有价值时二次补搜
        ↓
关键词 + FTS + 语义向量混合检索历史知识
        ↓
Qwen3.8 27B：最终阶段 / 风险 / 趋势 / 预测
        ↓
知识关系更新 + 时间线 + 下一观察节点
```

## 测试

```bash
PYTHONPATH=. MOCK_AI=true AUTO_PULL_MODEL=false pytest -q
PYTHONPATH=. MOCK_AI=true AUTO_PULL_MODEL=false python scripts/smoke_test.py
```

## 参考方向

架构设计吸收了 Local Deep Research 的持久任务 / 指标 / 研究状态思路、GPT Researcher 的多阶段与反思补搜、RAGFlow 的混合知识检索、changedetection.io 的变化检测、ArchiveBox 的原始证据归档和 Perplexica 的多源搜索思路；InternetBoard 的代码和数据结构按本项目长期专题跟踪需求重新实现。
