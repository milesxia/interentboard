# QNAP TS-673A 部署 IntelBoard

## 你的硬件基线

本项目按以下配置约束编写：

- QNAP TS-673A（x86_64 / Ryzen V1500B）
- GTX 1650 4GB
- **40GB RAM**
- Container Station 3.x
- Docker Compose

GPU 使用 QNAP 官方 Ollama Compose 同类写法：GTX 1650 需在 QTS/QuTS hero 控制台设为 **Container Station Mode**，并安装 NVIDIA GPU Driver 与 NvKernelDriver。

## 1. 创建目录

SSH 登录 NAS：

```bash
mkdir -p /share/Container/intelboard/{data,ollama}
```

建议给 IntelBoard 预留至少 **50GB 可用空间**，用于约19GB主模型、抓取全文和数据库备份。

## 2. 准备 `.env`

复制 `.env.example` 为 `.env`，至少修改：

```dotenv
INTELBOARD_IMAGE=你的DockerHub用户名/intelboard:latest
ADMIN_PASSWORD=你的看板密码
SESSION_SECRET=一串足够长的随机字符串
TZ=Asia/Shanghai
OLLAMA_MODEL=auto
```

40GB RAM 下 `auto` 会选择：

```text
qwen3:30b-a3b-instruct-2507-q4_K_M
```

如果你之后觉得 30B 在 V1500B 上太慢，可以只改成：

```dotenv
OLLAMA_MODEL=qwen3.5:9b
```

不需要重装看板或数据库。

## 3. GPU 版启动

使用 `docker-compose.yml`：

```bash
docker compose pull
docker compose up -d
```

首次启动会下载约19GB模型，模型持久化到：

```text
/share/Container/intelboard/ollama
```

看板：

```text
http://NAS_IP:8733
```

## 4. 验证 GPU

Container Station 打开 `intelboard-ollama` 的终端，执行：

```bash
nvidia-smi
```

应该能看到 GTX 1650。GTX 1650 只有4GB显存，30B模型不会完整进入显存；Ollama 会使用系统内存并尽可能进行 GPU offload，这是本项目预期工作方式。

## 5. GPU 启动异常时

QNAP 的 NVIDIA 驱动版本与 Ollama 镜像存在兼容性风险。如果 GPU Compose 无法正常启动，先不要动 `/share/Container/intelboard` 数据，直接：

```bash
docker compose -f docker-compose.cpu.yml up -d
```

CPU 模式功能完整，只是最终 AI 分析速度较慢。修复 QNAP GPU 环境后再切回 GPU Compose即可。

## 6. 自动和手动检索

固定计划：

```text
每天 03:00（Asia/Shanghai）
```

首页支持：

- `立即刷新全部`
- 每个专题单独 `立即刷新`

为了保护 TS-673A，任何时刻只运行一个检索/AI任务；正在运行时重复刷新会提示“已有任务运行”。

## 7. 数据在哪里

```text
/share/Container/intelboard/data/intelboard.db     # 主知识数据库
/share/Container/intelboard/data/archive/          # 抓取的HTML/PDF及全文
/share/Container/intelboard/data/backups/          # 看板生成的备份
/share/Container/intelboard/ollama/                 # 模型
```

看板首页和“系统”页面都可以下载数据库 + V2.8基线 + 专题配置的 ZIP 备份。

## 8. 更新

GitHub Actions 推送新镜像后：

```bash
docker compose pull
docker compose up -d
```

持久化目录不会被镜像更新覆盖。
