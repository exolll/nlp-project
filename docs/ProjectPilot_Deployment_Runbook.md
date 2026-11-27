# 部署手册（2026-11-27，基于本机踩坑整理）

> 目标：把 `slave1` 部署成**公网可访问的演示环境**，并保留"证据可核查"的口径。
> 本文只写经过验证的路径；未验证的部分明确标注。

---

## 0. 先明确一件事：这次部署是"演示环境"，不是"生产环境"

代码里有两道生产闸门，会直接挡住演示：

| 闸门 | 位置 | 后果 |
|---|---|---|
| `environment == 'production'` → 提取直接 503 | `services/requirements.py` `provider_config()` | 点「提取需求」必报 `EXTRACTION_NOT_PUBLIC` |
| `production` 必须 `storage_backend == 's3'` | `core/settings.py` `deployment_safety()` | 用本地存储直接启动失败 |

比赛演示只有一次机会，**建议用 `ENVIRONMENT=development` + 反代层鉴权**，并在材料里如实写：

> 本次公网演示为开发配置（仅开放单实例、单 worker、限定访问），生产化所需的私有对象存储、配额与多租户隔离不在本次范围内。

这样既跑得起来，也不会被指"宣称了未做到的事"。

---

## 1. 机器与前置

| 项 | 建议 |
|---|---|
| 服务器 | 阿里云 ECS / 腾讯云轻量，**2 核 4 G**，Ubuntu 22.04 或 24.04 |
| 磁盘 | 40 G 起（模型无关，但 Python 依赖 + README 快照会占几 G） |
| 域名 | 可选。没有域名就用 `http://<公网IP>`，但 HTTPS 缺失会影响演示观感 |
| 出网 | 需要访问 `api.deepseek.com`、`api.siliconflow.cn`、`api.github.com`、PyPI 镜像 |

国内机器装包建议直接用镜像：

```bash
pip config set global.index-url https://mirrors.aliyun.com/pypi/simple/
```

---

## 2. 依赖清单（必须装齐，缺一个就卡在某个阶段）

```bash
sudo apt update
sudo apt install -y python3.12 python3.12-venv python3-pip nginx git build-essential libssl-dev
```

Python 侧（**不要漏 chromadb**，它不在 `pyproject.toml` 里，只在运行搜索时才 import）：

```bash
cd /opt/projectpilot
python3.12 -m venv .venv
. .venv/bin/activate
pip install -U pip
pip install -e "./backend[test]"
pip install "chromadb>=1.5,<2"        # 约 300 MB，含 numpy / onnxruntime / tokenizers
```

> **版本漂移警告**：`chromadb` 会把 `pydantic` 拉到 2.14.x。本机已用它跑通 101 项后端测试；
> 如果换成 2.9.x 的组合，`fastapi` 需要钉在 `0.120.0` + `starlette 0.48.0`（`Request.form(max_part_size=...)` 依赖这个版本）。
> 最省事的做法：跑完后执行 `pip freeze > requirements.lock.txt` 并随仓库提交，部署时照锁文件装。

---

## 3. 数据库

Linux 上直接装服务，不要学本机折腾便携版：

```bash
sudo apt install -y mysql-server
sudo mysql -e "CREATE DATABASE IF NOT EXISTS projectpilot_search CHARACTER SET utf8mb4 COLLATE utf8mb4_0900_ai_ci;"
sudo mysql -e "CREATE USER IF NOT EXISTS 'pp_user'@'127.0.0.1' IDENTIFIED BY '<强密码>';"
sudo mysql -e "GRANT ALL PRIVILEGES ON projectpilot_search.* TO 'pp_user'@'127.0.0.1'; FLUSH PRIVILEGES;"
```

> 应用**拒绝空密码**（`SearchDatabaseSettings.require_secure_connection`），必须给非空密码。
> 若数据库不在本机，或 `ENVIRONMENT=production`，还必须配 `MYSQL_SSL_CA`（指向可信 CA 文件）。

建表（只做一次）：

```bash
cd /opt/projectpilot/backend
PROJECTPILOT_ALLOW_MIGRATIONS=1 ../.venv/bin/python -m alembic upgrade head
```

---

## 4. 配置 `.env`

`.env` 不入库，必须在服务器上重建。演示用模板：

```ini
ENVIRONMENT=development
STORAGE_BACKEND=local
LOCAL_STORAGE_DIRECTORY=/opt/projectpilot/data/objects
CHROMA_PATH=/opt/projectpilot/data/chroma
CORS_ORIGINS=["https://<你的域名或 http://IP>"]

MAX_FILE_BYTES=10000000
MAX_REQUEST_BYTES=11534336
EXTRACTION_TIMEOUT_SECONDS=90
EXTRACTION_MAX_INPUT_TOKENS=60000
EXTRACTION_MAX_OUTPUT_TOKENS=8000
EXTRACTION_MAX_CALLS=20
MAX_TASK_EXTRACTIONS=10
MAX_REQUIREMENT_REVISIONS=50

DEEPSEEK_API_KEY=sk-...
DEEPSEEK_MODEL=deepseek-flash
SILICONFLOW_API_KEY=sk-...
EMBEDDING_ENABLED=true
EMBEDDING_MODEL=BAAI/bge-m3
EMBEDDING_DIMENSION=1024

GITHUB_TOKEN=ghp_...

MYSQL_HOST=127.0.0.1
MYSQL_PORT=3306
MYSQL_DATABASE=projectpilot_search
MYSQL_USER=pp_user
MYSQL_PASSWORD=<强密码>
```

要点：

- `EXTRACTION_MAX_CALLS` 是**全站累计**，默认 5 会让第 6 个访客就 429；演示前调到 20 以上。
- `EMBEDDING_ENABLED=true` 时缺 `SILICONFLOW_API_KEY` 会**启动即失败**（这是刻意的启动闸门）。
- `data/` 必须持久化（`objects` + `chroma`），否则重启丢任务与会话。

---

## 5. 启动（systemd，单 worker）

`/etc/systemd/system/projectpilot.service`：

```ini
[Unit]
Description=ProjectPilot API
After=network.target mysql.service

[Service]
User=www-data
WorkingDirectory=/opt/projectpilot/backend
EnvironmentFile=/opt/projectpilot/.env
ExecStart=/opt/projectpilot/.venv/bin/uvicorn app.main:app \
  --host 127.0.0.1 --port 8000 --workers 1 --proxy-headers
Restart=always
RestartSec=3

[Install]
WantedBy=multi-user.target
```

```bash
sudo systemctl daemon-reload && sudo systemctl enable --now projectpilot
sudo journalctl -u projectpilot -f      # 看到 "Application startup complete" 即成功
```

**必须 `--workers 1`**：后台任务是进程内的 `BackgroundTasks`，本地存储用跨进程文件锁，Chroma 也是单实例假设。多 worker 会丢任务、争用锁。

---

## 6. 前端 + 反代（同域，最省事）

```bash
cd /opt/projectpilot/frontend
npm ci && npm run build            # 产物 dist/
sudo mkdir -p /var/www/projectpilot
sudo cp -r dist/* /var/www/projectpilot/
```

`/etc/nginx/sites-available/projectpilot`：

```nginx
server {
    listen 80;
    server_name <你的域名或IP>;

    client_max_body_size 12m;          # 对应 MAX_REQUEST_BYTES

    root /var/www/projectpilot;
    index index.html;
    location / { try_files $uri /index.html; }   # SPA fallback

    location /api/ {
        proxy_pass http://127.0.0.1:8000;
        proxy_set_header Host $host;
        proxy_set_header X-Real-IP $remote_addr;
        proxy_set_header X-Forwarded-For $proxy_add_x_forwarded_for;
        proxy_set_header X-Forwarded-Proto $scheme;
        proxy_read_timeout 180s;
    }
}
```

```bash
sudo ln -s /etc/nginx/sites-available/projectpilot /etc/nginx/sites-enabled/
sudo nginx -t && sudo systemctl reload nginx
```

> **必须加一层鉴权**：应用本身是匿名可用的（只有 Bearer 会话，任何人可新建）。演示前至少在 nginx 上加
> `auth_basic` + `auth_basic_user_file`，或用云厂商的安全组限制来源 IP。否则 DeepSeek 额度会被刷穿，
> 这在本机已经踩过一次（`reserve_global` 的 5 次上限救了我们，但那是拒绝服务，不是保护）。

---

## 7. 验收（照 `docs/ProjectPilot_Acceptance_Record.md` 走一遍）

1. 打开公网地址 → 新建会话 → 上传**短文档**（1–2 页，几千字）；
2. 提取 → 对照原文核查「必须/优先/选做」→ 编辑 → 保存 → **确认**；
3. 勾选搜索授权 → 开始搜索 → 看候选、README 证据、检索片段；
4. 单独授权深度核对 → 中文推荐；
5. 记录：URL、时间、`X-Request-ID`、运行 ID、截图。

**长文档不要在演示时用**：本机实测 1.3 万字会撞 `PROVIDER_OUTPUT_LIMIT`（输出上限 8000 token），
那是"整篇一次抽取"的结构限制，不是配置问题。

---

## 8. 已知坑（本机全部踩过，按出现顺序）

| # | 坑 | 现象 | 处理 |
|---|---|---|---|
| 1 | 缺 `chromadb` | 搜索立即 `PIPELINE_INTERRUPTED`，数据库零写入 | 装 `chromadb`，见 §2 |
| 2 | Chroma 目录不存在 | `unable to open database file (code 14)` | 已修（`ChromaStore.persistent` 自动 mkdir），提交 `d5b34d3` |
| 3 | `pip install -e` 卡在 build isolation | `Installing build dependencies ...` 长时间不动 | 用 `--no-build-isolation`，或先 `pip install -U setuptools wheel` |
| 4 | 空密码 MySQL | 启动即 `必须配置非空 MySQL 密码` | 建专用账号 + 强密码 |
| 5 | 生产闸门 | 提取 503 / 启动失败 | 演示用 `development`，见 §0 |
| 6 | 全局额度 5 次 | 第 6 个访客 429 | `EXTRACTION_MAX_CALLS` 调到 20+ |
| 7 | nginx 默认 1 MB body | 上传大 PDF 直接 413 | `client_max_body_size 12m` |
| 8 | 多 worker | 后台任务丢失、锁争用 | `--workers 1` |

---

## 9. Qoder Sites 的边界（别指望它跑后端）

Qoder Sites 是静态站点托管（Functions 为 Edge/TypeScript 运行时），**不能跑 FastAPI/uvicorn**，
也**不提供 MySQL**。可行组合只有一种：

- **方案 A（推荐）**：全部放 ECS（本文 §5–§6），Sites 不用；
- **方案 B**：前端 `dist/` 发布到 Sites，后端单独一个域名。此时前端构建必须设绝对地址：
  `VITE_API_BASE_URL=https://api.<域名>/api/v1`，并把该域名加进 `CORS_ORIGINS`。
  Sites 对 `/api` 路径有保留规则，不能靠同源相对路径。

---

## 10. 部署后 30 分钟内的检查清单

- [ ] `curl http://127.0.0.1:8000/api/v1/config` 返回配置
- [ ] 公网能打开首页，`/api/v1/config` 不 404
- [ ] 上传 10 MB PDF 不被 413
- [ ] 提取一次成功（看 `prompt_version=requirements-v4`、`usage` 有值）
- [ ] 确认页不再因概述字段被拦
- [ ] 搜索能出候选（`repositories > 0`）
- [ ] 检索片段有 `余弦距离` 与 `RRF` 数值
- [ ] `journalctl -u projectpilot` 无异常堆栈
- [ ] `data/` 有持久化策略（快照/备份），重启后任务可恢复
