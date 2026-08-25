# GitHub Codespaces → Docker Hub → QNAP

## A. 上传 GitHub

1. GitHub 新建仓库，例如 `intelboard`。
2. 解压本 ZIP，把全部文件上传到仓库根目录。
3. `Code` → `Codespaces` → `Create codespace on main`。
4. `.devcontainer/devcontainer.json` 会准备 Python + Docker-in-Docker。

## B. Codespaces 必跑验收

```bash
pytest -q
python scripts/smoke_test.py

docker compose -f docker-compose.dev.yml up --build -d
curl http://127.0.0.1:8080/healthz
```

打开 Codespaces 转发的 8080，开发密码：

```text
dev
```

开发 Compose 使用 `MOCK_AI=true`，目的是先验证前端、后端、数据库、调度、联网抓取和按钮流程，不在 Codespaces 下载19GB模型。

完成后：

```bash
docker compose -f docker-compose.dev.yml down
```

## C. GitHub Actions 自动推 Docker Hub

Docker Hub 创建 Access Token，在 GitHub 仓库：

`Settings` → `Secrets and variables` → `Actions` → `New repository secret`

添加：

```text
DOCKERHUB_USERNAME = 你的 Docker Hub 用户名
DOCKERHUB_TOKEN    = Docker Hub Access Token
```

仓库已经包含：

```text
.github/workflows/dockerhub.yml
```

push 到 `main` 后自动构建 `linux/amd64`：

```text
你的用户名/intelboard:latest
你的用户名/intelboard:sha-xxxxxxx
```

TS-673A 是 x86_64，所以不浪费 CI 时间构建 ARM。

## D. Codespaces 手动推送

```bash
export DOCKERHUB_USERNAME=你的用户名
export DOCKERHUB_TOKEN=你的Token
./scripts/codespaces-push.sh
```

## E. QNAP 部署

NAS 的 `.env`：

```dotenv
INTELBOARD_IMAGE=你的用户名/intelboard:latest
```

然后：

```bash
docker compose pull
docker compose up -d
```

之后日常更新只需要重复这两条命令。
