# Codespaces → Docker Hub

目标镜像：

```text
milesxia/internetboard:latest
```

上传 / 覆盖 v0.4 源码后，在 Codespaces 只执行：

```bash
bash scripts/codespaces-push.sh
```

脚本会先跑测试和静态检查，再让你隐藏输入 Docker Hub Token，然后构建并推送 `linux/amd64`。

GitHub Actions 也已更新到 Node 24 版本的 Actions，避免旧 Node 20 弃用警告。若仓库 Secret 仍不可读，直接使用上面的 Codespaces 脚本即可，不影响 NAS 部署。
