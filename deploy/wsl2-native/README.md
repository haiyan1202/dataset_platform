# WSL2 原生部署（systemd）

本目录用于在 Ubuntu WSL2 中直接运行 Dataset Platform，不依赖 Docker。当前代码目录和默认应用根目录为：

```text
/home/qt/projects/dataset-platform
```

完整配置、首次安装、更新、重启、备份恢复和故障排查请阅读仓库根目录的 [README.md](../../README.md)。

## 快速操作

```bash
cd /home/qt/projects/dataset-platform

# 构建前端（代码有前端改动时必须执行）
cd frontend && npm ci && npm run build && cd ..

# 首次安装或依赖/服务配置更新
sudo ./deploy/wsl2-native/scripts/install-native.sh

# 仅重启当前服务
sudo systemctl restart dataset-platform-api dataset-platform-worker nginx

# 健康检查
curl http://127.0.0.1:8080/health/ready
```

运行时配置文件为：

```text
/etc/dataset-platform/dataset-platform.env
```

可通过下列命令查看服务日志：

```bash
sudo journalctl -fu dataset-platform-api
sudo journalctl -fu dataset-platform-worker
```

> 安装脚本支持代码目录与 `APP_ROOT` 相同的原地部署；在这种情况下不会再复制代码到 `/opt`。
