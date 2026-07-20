# Dataset Platform

面向计算机视觉数据集的本地优先（local-first）管理平台。平台把**数据集、导入批次（Part）、样本、标签、质量检查、导出任务和可撤销操作历史**统一到一个 Web 工作台中，适合在 Windows + WSL2 的单机或小团队环境运行。

> 当前推荐的运行位置：`/home/qt/projects/dataset-platform`（WSL Ubuntu）。
>
> Windows 资源管理器可通过 `\\wsl$\Ubuntu-22.04\home\qt\projects\dataset-platform` 访问该目录。

## 功能概览

### 数据集工作台

- 工作区内创建、浏览和切换多个数据集；
- 三栏工作台布局：**导入批次 / 样本浏览 / 样本预览**；
- 实时显示样本数、已标注样本、训练集数量、缺失标注数量和标注类型；
- 后台任务队列展示扫描、导入、质量检查和导出进度。

### 导入与 Part 管理

- 支持 ZIP、7z、TAR、TAR.GZ、TGZ、TAR.BZ2、TAR.XZ 等归档上传；
- 浏览器直传 MinIO，对归档执行安全扫描，再由用户确认正式导入；
- 自动识别并解析 YOLO、COCO、LabelMe、Pascal VOC 标注；
- 以导入批次（Part）组织数据，可编辑批次名称和备注、重新扫描或软删除；
- 导入、扫描和索引均由 Celery Worker 异步执行，不阻塞 Web 页面。

### 样本浏览与标注检查

- 服务端分页浏览样本；
- 按文件名、子集、标注格式、类别、是否有标注和导入批次筛选；
- 支持多选样本，批量移动到 `train`、`val` 或 `test`；
- 支持软删除样本，可从操作历史撤销；
- 预览图像、原始标注和标准化标注叠加层（检测框、多边形、关键点）。

### 标签、质量、导出与历史

- 在 Web 中新增、编辑或删除标签映射；标签颜色会用于预览和统计；
- 异步质量检查与问题列表；
- 导出 Manifest、YOLO、COCO JSON、LabelMe、Pascal VOC；导出可限制 Part、子集、类别和是否包含未标注图片；
- 操作历史支持对样本删除、子集调整、Part 编辑和 Part 删除进行撤销/重做；
- 审计日志记录数据集、导入、导出和标签等关键操作。

## 架构

```text
Windows 浏览器
  │ http://127.0.0.1:8080
  ▼
Nginx ───────────────────────────────┐
  │ 静态 React 工作台                 │ /api、/health 反向代理
  ▼                                   ▼
frontend/dist                    FastAPI API :8000
                                     │
                         ┌───────────┼───────────┐
                         ▼           ▼           ▼
                    PostgreSQL     Redis       MinIO
                         │           │           │
                         └──── Celery Worker ───┘

代码：/home/qt/projects/dataset-platform
运行数据：/var/lib/dataset-platform
运行配置：/etc/dataset-platform/dataset-platform.env
```

- **Nginx**：对外提供 `8080` 端口、React 静态文件和 API 代理；
- **FastAPI**：认证、数据集、样本、标签、任务、导出和历史 API；
- **Celery Worker**：归档扫描、确认导入、质量检查、导出等耗时任务；
- **PostgreSQL**：元数据与操作记录；**Redis**：任务消息与结果；**MinIO**：原始归档、图片、标注和导出文件。

## 目录说明

```text
/home/qt/projects/dataset-platform
├─ backend/                 # FastAPI、SQLAlchemy、Celery
├─ frontend/                # React + Vite；dist/ 为 Nginx 实际服务的静态文件
├─ src/dataset_core/        # 与 Web/数据库解耦的归档解析与领域模型
├─ tests/                   # 解析、安全性和归档格式测试
├─ deploy/wsl2-native/      # WSL 原生部署脚本、systemd 和 Nginx 模板
└─ deploy/wsl2/             # Docker Compose 部署方案
```

## 运行要求

推荐环境：

- Windows 11 + WSL2；
- Ubuntu 22.04，且 WSL 已启用 `systemd`；
- PostgreSQL、Redis、Nginx 与 MinIO 运行在 WSL 内；
- Python 3.10+；
- 仅在构建前端时需要 Node.js 20+ 和 npm。

检查 systemd：

```bash
ps -p 1 -o comm=
# 输出应为 systemd
```

## 首次配置（WSL 原生部署）

以下操作在 WSL Ubuntu 中执行。先进入代码目录：

```bash
cd /home/qt/projects/dataset-platform
```

### 1. 创建部署配置

配置文件包含数据库、MinIO、初始管理员密码和 JWT 密钥，**不要提交到 Git**：

```bash
cp deploy/wsl2-native/env.example deploy/wsl2-native/.env
chmod 600 deploy/wsl2-native/.env
nano deploy/wsl2-native/.env
```

至少替换以下值：

- `POSTGRES_PASSWORD`
- `MINIO_ROOT_PASSWORD`
- `MINIO_SECRET_KEY`
- `APP_BOOTSTRAP_PASSWORD`
- `TOKEN_SECRET`

生成安全的 `TOKEN_SECRET`：

```bash
python3 -c "import secrets; print(secrets.token_urlsafe(48))"
```

确认以下配置与当前目录一致：

```dotenv
APP_ROOT=/home/qt/projects/dataset-platform
APP_USER=dataset-platform
NGINX_PORT=8080
```

> 安装完成后，运行时实际读取的是 `/etc/dataset-platform/dataset-platform.env`。修改仓库中的 `.env` 后，需要重新执行安装脚本，或在修改运行时配置后重启服务。

### 2. 构建前端

在具有 Node.js 的环境构建静态资源：

```bash
cd /home/qt/projects/dataset-platform/frontend
npm ci
npm run build
```

构建成功后必须存在：

```text
/home/qt/projects/dataset-platform/frontend/dist/index.html
```

### 3. 安装并启动服务

```bash
cd /home/qt/projects/dataset-platform
chmod +x deploy/wsl2-native/scripts/*.sh
./deploy/wsl2-native/scripts/preflight-native.sh
sudo ./deploy/wsl2-native/scripts/install-native.sh
```

安装脚本会准备系统依赖、Python 虚拟环境、PostgreSQL、Redis、MinIO、systemd 服务与 Nginx，并执行 Alembic 迁移和初始工作区/管理员引导。

访问地址：

| 服务 | 地址 |
| --- | --- |
| 数据集工作台 | `http://127.0.0.1:8080` |
| API 文档 | `http://127.0.0.1:8080/api/docs` |
| 健康检查 | `http://127.0.0.1:8080/health/ready` |
| MinIO Console | `http://127.0.0.1:9001` |

初始登录账号由配置中的 `APP_BOOTSTRAP_EMAIL` 和 `APP_BOOTSTRAP_PASSWORD` 决定。

## 日常启动、停止与更新

### 查看状态与健康检查

```bash
cd /home/qt/projects/dataset-platform
./deploy/wsl2-native/scripts/status-native.sh
curl http://127.0.0.1:8080/health/ready
```

当 Worker 刚重启时，健康检查可能短暂返回 `degraded`；等待几秒后应返回：

```json
{"status":"ok","checks":{"postgres":"ok","redis":"ok","object_storage":"ok","disk":"ok","worker":"ok"}}
```

### 重启服务

代码或配置更新后：

```bash
sudo systemctl restart dataset-platform-api dataset-platform-worker nginx
```

分别查看状态：

```bash
sudo systemctl status dataset-platform-api
sudo systemctl status dataset-platform-worker
sudo systemctl status dataset-platform-minio
sudo systemctl status nginx
```

### 更新代码并发布

1. 更新 `/home/qt/projects/dataset-platform` 内的代码；
2. 若前端有改动，重新构建：

   ```bash
   cd /home/qt/projects/dataset-platform/frontend
   npm ci
   npm run build
   ```

3. 若 Python 依赖、迁移、systemd 或 Nginx 模板有变化，执行完整安装更新：

   ```bash
   cd /home/qt/projects/dataset-platform
   sudo ./deploy/wsl2-native/scripts/install-native.sh
   ```

4. 若仅修改前端 `dist`、后端应用代码或 Worker 任务代码，直接重启即可：

   ```bash
   sudo systemctl restart dataset-platform-api dataset-platform-worker nginx
   ```

> 代码当前由服务从 `/home/qt/projects/dataset-platform` 加载；旧版本保留在 `/opt/dataset-platform.pre-home-migration-20260717`，可在确认稳定后手动清理。

### 查看日志

```bash
sudo journalctl -fu dataset-platform-api
sudo journalctl -fu dataset-platform-worker
sudo journalctl -fu dataset-platform-minio
sudo journalctl -fu nginx
```

## 配置项说明

运行配置位于：

```text
/etc/dataset-platform/dataset-platform.env
```

常用配置：

| 配置项 | 用途 |
| --- | --- |
| `DATABASE_URL` | PostgreSQL 连接字符串 |
| `REDIS_URL` | Redis/Celery broker 地址 |
| `MINIO_ENDPOINT` | MinIO 内网地址 |
| `MINIO_PUBLIC_ENDPOINT` | 浏览器访问预签名文件时使用的地址 |
| `MINIO_BUCKET` | 数据、标注和导出对象所在桶 |
| `APP_BOOTSTRAP_EMAIL` | 初始管理员邮箱 |
| `APP_BOOTSTRAP_PASSWORD` | 初始管理员密码 |
| `TOKEN_SECRET` | JWT 签名密钥，至少 32 个字符 |
| `MAX_UPLOAD_BYTES` | 单个归档上传上限，默认 10 GiB |
| `WORKER_TMP_DIR` | Worker 解压与扫描临时目录 |
| `HEALTH_MIN_FREE_BYTES` | 磁盘健康检查所需的最小可用空间 |
| `NGINX_PORT` | Web 对外端口，默认 `8080` |

修改运行时配置后执行：

```bash
sudo systemctl restart dataset-platform-minio dataset-platform-api dataset-platform-worker nginx
```

## 数据备份与恢复

运行数据不在代码目录，而位于 `/var/lib/dataset-platform`。使用原生脚本进行备份：

```bash
cd /home/qt/projects/dataset-platform
sudo ./deploy/wsl2-native/scripts/backup-native.sh /home/qt/backups
```

恢复会覆盖 PostgreSQL 与 MinIO 数据，属于破坏性操作；恢复前请核验备份目录和校验文件：

```bash
sudo ./deploy/wsl2-native/scripts/restore-native.sh /home/qt/backups/YYYYMMDD_HHMMSS
```

## 开发与测试

Windows 本地开发：

```powershell
cd C:\Users\Admin\Desktop\dataset_platform
.\.venv\Scripts\python.exe -m pytest -q -p no:cacheprovider
cd frontend
cmd /c npm run lint
cmd /c npm run build
```

WSL 中运行后端测试：

```bash
cd /home/qt/projects/dataset-platform
.venv/bin/python -m pytest -q
```

## 常见问题

### `/health/ready` 显示 `worker: unavailable`

Worker 重启后需要连接 Redis 并完成 Celery 初始化。等待 5–10 秒后再次检查；若仍失败：

```bash
sudo systemctl status dataset-platform-worker
sudo journalctl -u dataset-platform-worker -n 100 --no-pager
```

### 前端页面没有显示最新改动

确认已重新构建并重启 Nginx：

```bash
cd /home/qt/projects/dataset-platform/frontend
npm run build
sudo systemctl restart nginx
```

### 上传失败或导入卡住

检查 API、Worker、MinIO 与磁盘空间：

```bash
curl http://127.0.0.1:8080/health/ready
sudo journalctl -u dataset-platform-api -n 100 --no-pager
sudo journalctl -u dataset-platform-worker -n 100 --no-pager
```

## 安全与边界

- 不要提交任何 `.env`、数据库备份或 MinIO 数据；
- PostgreSQL、Redis、MinIO 服务默认绑定本机回环地址，Web 由 Nginx 的 `8080` 端口提供；
- 归档扫描会拒绝路径穿越和不安全归档项；
- 原始图片、标注和导出文件保存在 MinIO；运行数据目录位于 `/var/lib/dataset-platform`；
- `src/dataset_core` 只依赖 Python 标准库，保持与 Web、数据库和对象存储层解耦。

## Docker Compose 方案

仓库仍保留 `deploy/wsl2/` 下的 Docker Compose 部署方案，适合需要接近云端拓扑的环境。当前本机推荐使用上文的 **WSL 原生 systemd 部署**。
