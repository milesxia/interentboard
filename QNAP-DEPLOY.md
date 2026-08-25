# QNAP TS-673A 部署 / 升级 InternetBoard 0.4

## 不要删除旧数据

```text
/share/Container/internetboard/data
/share/Container/internetboard/ollama
```

v0.4 会自动迁移现有 `intelboard.db`，旧证据、Claim、人工知识和已下载模型继续保留。

## 默认模型

```text
qwen3:4b-instruct-2507-q4_K_M   # 大量文本提炼
qwen3.8:27b-q4_K_M              # 最终分析
qwen3-embedding:0.6b             # 语义检索，达到知识量门槛后按需下载
```

## Container Station

用仓库中的 `docker-compose.yml` 更新应用。默认 Web：

```text
http://NAS_IP:8733
```

Lucky 现有 HTTPS 反代可继续使用。

## GPU / 内存策略

- 4B 提炼模型优先尽量使用 GTX 1650。
- 27B 默认请求 `num_gpu=4`；如果显存不足，应用自动逐级降低 GPU offload 后重试。
- Ollama 同时只驻留 1 个模型，AI 并发 1。
- Ollama 默认内存上限 30GB；InternetBoard 默认 3GB。

## 第一次升级后的运行

第一次会比日常增量任务更久，因为系统会逐步：

1. 迁移旧数据库结构；
2. 补做旧 Evidence 的 Claim 化；
3. 建立页面版本 / 来源组关系；
4. 当单专题有效 Claim 达到 200 条后，按需建立语义向量索引。

这些过程都有断点，不要求一次做完。
