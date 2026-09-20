<p align="center">
  <h1 align="center">TrainingEdge</h1>
  <p align="center">
    自托管运动数据分析引擎 — 让训练决策有据可依
    <br />
    <a href="#快速开始">快速开始</a> · <a href="#功能特性">功能特性</a> · <a href="#迁移与备份">迁移与备份</a> · <a href="#api-参考">API 参考</a>
  </p>
</p>

> 🤖 本项目由 [Claude Code](https://claude.ai/claude-code) 辅助开发，包括核心引擎、Web 仪表盘、部署脚本和本文档。

---

## 这是什么？

TrainingEdge 是一个**完全自托管**的运动训练分析平台。它从 Garmin 手表同步数据，计算专业训练指标，并通过 AI 生成训练计划和骑行复盘。

**所有数据留在你自己的机器上。** 没有云服务，没有订阅，没有第三方拿走你的训练数据。

### 核心能力

- 🔄 **Garmin 自动同步** — 活动、睡眠、HRV、静息心率、Body Battery
- 📊 **专业指标计算** — NP / TSS / IF / CTL / ATL / TSB / PDC / eFTP / W'
- 🤖 **AI 训练计划** — 基于你的体能状态和约束条件自动生成周计划
- 📋 **计划执行追踪** — 自动匹配实际训练与计划（支持换天做）
- 🏥 **每日准备度评估** — 综合 HRV、睡眠、TSB 判断今天能不能练
- 📈 **中文 Web 仪表盘** — 深色主题，结论优先，移动端适配

### 设计理念

**"结论 → 证据 → 动作"** — 每个页面先告诉你该做什么，再展示为什么，最后给操作入口。

---

## 功能特性

### 仪表盘

| 页面 | 内容 |
|------|------|
| 主面板 | 今日训练焦点、准备度评估、ACWR 负荷监控、赛事倒计时、本周执行进度 |
| 活动详情 | AI 骑行复盘、功率/心率时间序列、区间分布、圈速分析 |
| 训练计划 | AI 周计划、约束满足清单、计划 vs 实际对比 |
| 身体数据 | 健康趋势（HRV/睡眠/心率）、体成分记录（InBody） |

### 计算指标

| 指标 | 说明 |
|------|------|
| NP / TSS / IF | 标准化功率、训练压力、强度因子 |
| CTL / ATL / TSB | 体能 / 疲劳 / 状态平衡 |
| PDC / eFTP / W' | 功率曲线、估算 FTP、无氧做功能力 |
| xPower / TRIMP | 指数加权功率、心率训练冲量 |
| HR Drift / VDOT | 心率漂移、跑步能力指数 |

---

## 快速开始

### 环境要求

- Python 3.10+ 或 Docker
- Garmin 手表 + Garmin Connect 账号

### Docker 部署

```bash
git clone https://github.com/sisjune/training-edge.git
cd training-edge
cp .env.example .env   # 编辑填入你的参数
docker compose up -d
```

访问 `http://localhost:8420`

### 本地运行（macOS）

> **唯一运行源**：`<local-checkout>/training-edge`。
> OneDrive 只接收三份计划白名单备份，不运行代码，也不接收 SQLite、Token、FIT、日志、venv 或 `.env`。

**推荐目录分离：**

| 类型 | 位置 | 说明 |
|------|------|------|
| 代码 | `<local-checkout>/training-edge` | 本地唯一运行源 |
| 运行时 | `~/Library/Application Support/TrainingEdge/` | DB / venv / tokens / logs |
| 计划备份 | `OneDrive-个人/AICoachPortable/vault/` | 仅三份白名单文件 |

`.env` 中配置（已自动加载，无需手动 export）：

```bash
TRAININGEDGE_DB_PATH=~/Library/Application Support/TrainingEdge/training_edge.db
TRAININGEDGE_FIT_DIR=~/Library/Application Support/TrainingEdge/fit_files
TRAININGEDGE_LOG_FILE=~/Library/Application Support/TrainingEdge/logs/training_edge.log
GARMINTOKENS=~/Library/Application Support/TrainingEdge/tokens
TRAININGEDGE_SYNC_INTERVAL_HOURS=0   # Hermes 负责 garmin.db 同步时设为 0
```

**首次安装：**

```bash
# 1. 创建运行时 venv（在 OneDrive 外）
python3 -m venv ~/Library/Application\ Support/TrainingEdge/venv
~/Library/Application\ Support/TrainingEdge/venv/bin/pip install -e .

# 2. 初始化 + 从 Hermes 导入数据
python scripts/cli.py init
python scripts/cli.py sync-hermes --db ../garmin.db --all

# 3. 预检并安装 launchd 守护（安装器拒绝 OneDrive 代码路径）
bash scripts/install_service.sh --preflight
bash scripts/install_service.sh

# 4. 验证
bash scripts/verify_local_runtime.sh
python scripts/smoke_test.py

# 5. 计划数据白名单备份到 OneDrive
bash scripts/backup_plan_to_onedrive.sh --dry-run
bash scripts/backup_plan_to_onedrive.sh
```

**日常运维：**

```bash
bash scripts/start_server.sh --status          # 检查状态
bash scripts/install_service.sh --restart    # 重启守护服务
python scripts/smoke_test.py --offline       # 改代码后离线验证
python scripts/smoke_test.py                 # 完整回归
```

开发调试（带热重载）：

```bash
bash scripts/start_server.sh    # 前台启动，仅 api/engine/web 目录触发 reload
```

### 本地开发（通用）

```bash
python3 -m venv .venv && source .venv/bin/activate
pip install -e .
python scripts/cli.py init
python scripts/cli.py sync --days 7
python scripts/cli.py serve --reload --port 8420
```

### 复用 Hermes 的 `garmin.db`（全量导入）

如果你已经在上层项目通过 Hermes 生成了 `garmin.db`（例如本仓库根目录 `../garmin.db`），可以一键把历史数据导入 TrainingEdge（不需要再调 Garmin API）：

```bash
python scripts/cli.py sync-hermes --db ../garmin.db --all
```

> 说明：该导入会写入 TrainingEdge 的 `activities` / `wellness` / `fitness_history`，并把 Hermes 的 raw JSON 保存到 `hermes_*` 表；**不会下载 FIT**，因此 NP/TSS/IF 等基于功率的高级指标会为空。如需这些指标，再运行 `python scripts/cli.py sync --days N` 下载 FIT 并计算。

### 启动后如何访问？

- **Web 仪表盘**：`http://localhost:8420/`
- **健康检查**：`http://localhost:8420/api/health`（无需鉴权）
- **调用 API**：先运行 `python scripts/cli.py init` 获取 **API Key**，然后携带 `X-API-Key` 访问（示例）：

```bash
curl -H 'X-API-Key: <API_KEY>' http://localhost:8420/api/summary
```

> 如果设置了 `TRAININGEDGE_PASSWORD`，浏览器访问会跳转到 `/login`；API 仍然需要 `X-API-Key`。

### 配置

复制 `.env.example` 为 `.env`，核心参数：

| 变量 | 说明 |
|------|------|
| `TRAININGEDGE_FTP` | 你的 FTP (W) |
| `TRAININGEDGE_MAX_HR` | 最大心率 (bpm) |
| `TRAININGEDGE_RESTING_HR` | 静息心率 (bpm) |
| `TRAININGEDGE_PASSWORD` | Web 访问密码（可选） |
| `GARMIN_EMAIL` | Garmin 登录邮箱（用于自动获取或刷新 Token） |
| `GARMIN_PASSWORD` | Garmin 登录密码（用于自动获取或刷新 Token） |
| `GARMINTOKENS` | Garmin OAuth token 目录 |
| `GARMIN_IS_CN` | 是否使用 Garmin 中国区 (`garmin.cn`)，设置为 `true` 启用 |
| `OPENROUTER_API_KEY` | AI 功能所需（可在 Web 设置页配置） |

完整变量列表见 [.env.example](.env.example)。

---

## 架构

```
Hermes (cron) ──► garmin.db ──► sync-hermes ──► TrainingEdge SQLite
Garmin Watch ──► Garmin Connect ──► garminconnect API (可选 FIT 下载)
                                       │
                                       ▼
                              FIT 解析 (fitparse)
                                       │
                                       ▼
                             指标计算 (engine/metrics.py)
                                       │
                                       ▼
                    SQLite (~/Library/.../TrainingEdge/  或 Docker /data/)
                                       │
                          ┌────────────┼────────────┐
                          ▼            ▼            ▼
                     REST API    AI 计划生成    Web 仪表盘
                     (FastAPI)   (OpenRouter)   (Jinja2)
                          │
                          ▼
              launchd 守护 (macOS) / docker compose (NAS)
```

### 与 AICoachPortable 的协作

| 组件 | 职责 |
|------|------|
| **Hermes** | 每小时同步 `garmin.db`、飞书推送、体感笔记 |
| **TrainingEdge** | 训练指标计算、Web 仪表盘、计划执行追踪、ACWR/准备度 |
| **Cursor Skills** | 晨间/晚间/周度教练工作流（读 vault + garmin.db） |

数据流：`garmin.db` → `sync-hermes` → TrainingEdge DB → 仪表盘/API → 教练 Skills 交叉分析。

### 技术栈

Python 3.13 · FastAPI · SQLite (WAL) · Jinja2 · Chart.js · fitparse · garminconnect · Docker

---

## API 参考

### 活动

| 端点 | 方法 | 说明 |
|------|------|------|
| `/api/activities` | GET | 活动列表 |
| `/api/activity/{id}` | GET | 活动详情（含计算指标） |
| `/api/activities/{id}/ai-review` | GET | AI 活动复盘 |

### 体能与健康

| 端点 | 方法 | 说明 |
|------|------|------|
| `/api/fitness` | GET | CTL/ATL/TSB 历史 |
| `/api/pdc` | GET | 功率持续时间曲线 |
| `/api/wellness` | GET | HRV/睡眠/静息心率 |
| `/api/decision-summary` | GET | 今日综合决策摘要 |
| `/api/readiness` | GET | 今日训练就绪度 |
| `/api/acwr` | GET | ACWR 急慢性负荷比 |
| `/api/race-info` | GET | 目标赛事倒计时 |

### 训练计划

| 端点 | 方法 | 说明 |
|------|------|------|
| `/api/plan/generate` | POST | AI 生成周训练计划 |
| `/api/plan/workouts` | GET | 当前计划训练列表 |
| `/api/constraint-status` | GET | 约束满足情况 |

### 同步与设置

| 端点 | 方法 | 说明 |
|------|------|------|
| `/api/sync` | POST | 触发 Garmin 数据同步 |
| `/api/settings` | GET/POST | 读取/更新设置 |
| `/api/health` | GET | 健康检查 |

---

## 项目结构

```
training-edge/
├── engine/              # 核心计算引擎
│   ├── metrics.py       # NP/TSS/IF/CTL/ATL/TSB/PDC 计算
│   ├── database.py      # SQLite 数据层
│   ├── sync.py          # Garmin 数据同步
│   ├── readiness.py     # 每日准备度评估
│   ├── plan_generator.py # AI 训练计划生成
│   └── fit_parser.py    # FIT 文件解析
├── api/app.py           # FastAPI 应用
├── web/templates/       # Jinja2 中文页面模板
├── scripts/
│   ├── cli.py              # CLI 工具
│   ├── smoke_test.py       # 冒烟回归测试（改代码后必跑）
│   ├── start_server.sh     # 开发启动脚本
│   └── install_service.sh  # macOS launchd 守护安装
├── Dockerfile
├── docker-compose.yml
└── .env.example
```

---

## 迁移与备份

> 完整 AICoachPortable 项目（含 vault、garmin.db、Hermes）迁移见上级目录 [README.md](../README.md#项目迁移指南)。
>
> **Agent 自动化**：说 `/migrate` 加载 Skill `project-migration`，或手动运行：
> ```bash
> bash ../scripts/migrate_backup.sh
> bash ../scripts/migrate_restore.sh <备份目录>
> bash ../scripts/migrate_verify.sh
> ```

### 迁移前先理解：什么该搬、什么不该搬

| 类别 | 路径 | 是否随 OneDrive 同步 | 迁移方式 |
|------|------|---------------------|----------|
| **代码** | 本地 `AICoachPortableOnMac/training-edge/` | ❌ 不作为 OneDrive 同步源 | Git 更新 |
| **配置** | `training-edge/.env` | ⚠️ 含密钥，勿提交 Git | 手动复制或加密备份 |
| **运行时 DB** | `~/Library/Application Support/TrainingEdge/training_edge.db` | ❌ 不要 | 必须手动打包复制 |
| **Garmin Token** | 同上目录 `tokens/` | ❌ 不要 | 手动复制（避免重复登录） |
| **FIT 文件** | 同上目录 `fit_files/` | ❌ 不要 | 可选复制（体积大，可重建） |
| **Python venv** | 同上目录 `venv/` | ❌ 不要 | **不要复制**，新机器重建 |
| **launchd 服务** | `~/Library/LaunchAgents/com.trainingedge.server.plist` | ❌ | 新机器运行 `install_service.sh` 重建 |
| **计划白名单** | `training_plan.yaml`、`current_goal.md`、`2026_Marathon_Plan.md` | ✅ 仅这三份 | `backup_plan_to_onedrive.sh` |
| **旧残留 DB** | `state/*.db.bak*`、`data/*.db.bak*` | — | 仅作备份参考，勿再启用 |

**第一性原理**：代码从本地运行，**状态也只在本地维护**；OneDrive 只接收计划白名单备份。SQLite + Token + venv 必须在每台机器本地独立维护。

### 场景 A：换新 Mac

从 Git 或受控本地备份恢复代码；OneDrive 中的旧项目副本只能作为数据备份，不能启动服务。

**旧机器（迁移前）：**

```bash
cd training-edge

# 1. 停止服务
bash scripts/install_service.sh --uninstall   # 或 bash scripts/start_server.sh --stop

# 2. 打包运行时数据（不含 venv）
RUNTIME="$HOME/Library/Application Support/TrainingEdge"
tar czf ~/Desktop/trainingedge-runtime-backup.tar.gz \
  -C "$RUNTIME" \
  --exclude='venv' \
  training_edge.db fit_files tokens logs archive 2>/dev/null || true

# 3. 单独备份 .env（含密码和 API Key）
cp .env ~/Desktop/trainingedge.env.backup
```

将 `trainingedge-runtime-backup.tar.gz` 和 `trainingedge.env.backup` 通过 AirDrop / U 盘 / 加密云盘传到新机器。

**新机器（恢复后）：**

```bash
# 1. 先把代码恢复到本地目录
cd <local-checkout>/training-edge

# 2. 恢复运行时
RUNTIME="$HOME/Library/Application Support/TrainingEdge"
mkdir -p "$RUNTIME"
tar xzf ~/Desktop/trainingedge-runtime-backup.tar.gz -C "$RUNTIME"

# 3. 恢复 .env
cp ~/Desktop/trainingedge.env.backup .env

# 4. 重建 venv（不要复制旧 venv）
python3 -m venv "$RUNTIME/venv"
"$RUNTIME/venv/bin/pip" install -q --upgrade pip
"$RUNTIME/venv/bin/pip" install -q -e .

# 5. 从 Hermes 增量同步（确保 garmin.db 最新）
python scripts/cli.py sync-hermes --db ../garmin.db --days 30

# 6. 安装守护 + 验证
bash scripts/install_service.sh
python scripts/smoke_test.py
```

### 场景 B：从旧 OneDrive 代码副本迁到本地目录

安装器会拒绝从 `CloudStorage/OneDrive` 运行。迁移后应以本地目录为唯一运行源。

```bash
# 1. 旧机器：按场景 A 打包 runtime + .env

# 2. 复制整个项目（不含 .venv）
rsync -a --exclude='.venv' --exclude='__pycache__' \
  ~/Library/CloudStorage/OneDrive-个人/AICoachPortable/ \
  ~/Projects/AICoachPortable/

# 3. 在新路径按场景 A 步骤 2-6 恢复 runtime 并安装服务

# 4. 在 Cursor 中重新打开 ~/Projects/AICoachPortable 作为工作区
```

> vault/、garmin.db、Hermes 配置等上层项目迁移见 [根目录 README](../README.md#项目迁移指南)。

### 场景 C：迁到 NAS / Docker

Docker 模式下运行时数据在容器卷 `/data/`，与 macOS 本地路径不同。

```bash
# 1. 从 macOS 导出 DB 和 tokens
RUNTIME="$HOME/Library/Application Support/TrainingEdge"
mkdir -p ./docker-migrate
cp "$RUNTIME/training_edge.db" ./docker-migrate/
cp -R "$RUNTIME/tokens" ./docker-migrate/
cp -R "$RUNTIME/fit_files" ./docker-migrate/   # 可选

# 2. 编辑 .env（Docker 路径）
TRAININGEDGE_DB_PATH=/data/training_edge.db
TRAININGEDGE_FIT_DIR=/data/fit_files
GARMINTOKENS=/data/tokens

# 3. 挂载数据卷并启动
docker compose up -d

# 4. 容器内验证
docker compose exec training-edge python scripts/smoke_test.py --offline
```

macOS 的 `install_service.sh` / launchd **不适用于 Docker**，改用 `docker compose` 管理生命周期。

### 场景 D：双机并行（不推荐）

两台 Mac 同时跑 Hermes 同步 + TrainingEdge 写同一 `garmin.db` 或同一 runtime DB，**几乎必然**出现 SQLite 锁冲突或 OneDrive 冲突副本。

若必须双机：
- 只在一台机器运行 Hermes cron 和 TrainingEdge 服务
- 另一台只读（Cursor 教练对话），通过 OneDrive 同步 `garmin.db` 只读副本
- **不要**两台同时写 runtime DB

### 迁移后验证清单

```bash
# 全部应为 PASS
python scripts/smoke_test.py

# 手动确认
bash scripts/start_server.sh --status          # 服务 running
curl -s http://127.0.0.1:8420/api/health     # {"status":"ok",...}
curl -s http://127.0.0.1:8420/api/readiness  # JSON，非登录页

# 数据完整性
python -c "
from engine.database import get_db
with get_db() as conn:
    for t in ('activities','wellness','planned_workouts','fitness_history'):
        n = conn.execute(f'SELECT COUNT(*) FROM {t}').fetchone()[0]
        print(f'{t}: {n}')
"
```

### 定期备份建议

```bash
# 每周或重大变更前执行（可加入 cron）
RUNTIME="$HOME/Library/Application Support/TrainingEdge"
BACKUP_DIR="$HOME/Backups/trainingedge"
mkdir -p "$BACKUP_DIR"
DATE=$(date +%Y%m%d)
sqlite3 "$RUNTIME/training_edge.db" ".backup '$BACKUP_DIR/training_edge_$DATE.db'"
tar czf "$BACKUP_DIR/runtime_$DATE.tar.gz" -C "$RUNTIME" --exclude='venv' .
echo "备份完成: $BACKUP_DIR"
```

---

## 常见问题排查

### 页面打不开 / 503 Service Unavailable

**症状**：浏览器访问 `http://localhost:8420` 显示 503 或无法连接。

**原因**：服务进程未运行。常见触发场景：
- macOS 合盖休眠后进程被系统回收
- OneDrive 同步触发文件变更，uvicorn `--reload` 反复重启后崩溃
- 端口被其他进程占用

**解决**（按优先级）：

```bash
# 推荐：launchd 守护（休眠唤醒后自动拉起）
bash scripts/install_service.sh --restart
bash scripts/start_server.sh --status

# 开发模式手动重启
bash scripts/start_server.sh --stop
bash scripts/start_server.sh
```

查看日志：`~/Library/Application Support/TrainingEdge/logs/server.log`

### 页面返回 500 Internal Server Error

**症状**：页面显示白屏或 "Internal Server Error"。

**原因**：通常是代码修改后模板引用了未传递的变量。

**解决**：

```bash
# 运行离线冒烟测试定位问题
python scripts/smoke_test.py --offline

# 如果离线测试通过但在线失败，重启服务
bash scripts/start_server.sh --stop
bash scripts/start_server.sh
```

### 从 OneDrive 代码副本启动被拒绝

**症状**：安装器报告“拒绝从云盘副本安装”；旧服务也可能曾出现频繁 reload 或 `database is locked`。

**根因**：SQLite + OneDrive 同步 = 锁冲突；文件元数据变更触发 uvicorn reload。

**解决**（已内置）：
1. 从本地 `<local-checkout>/training-edge` 运行安装器
2. 保持运行时数据位于 `~/Library/Application Support/TrainingEdge/`
3. 用 `backup_plan_to_onedrive.sh` 将三份计划白名单备份到 OneDrive

### 冒烟测试怎么用

每次修改代码后运行，确保不引入回归问题：

```bash
# 离线测试（不需要服务运行）：验证语法 + 引擎 + 模板渲染
python scripts/smoke_test.py --offline

# 完整测试（需要服务运行）：离线 + 所有页面 HTTP 请求
python scripts/smoke_test.py
```

### SQLite 数据库被锁

**症状**：页面报错 `database is locked`。

**原因**：OneDrive 同步可能导致 SQLite WAL 文件冲突。

**解决**：

```bash
# 确保没有多个进程同时写入
lsof state/training_edge.db

# 如果有残留锁文件
ls state/*.db-shm state/*.db-wal
# 停止服务后删除锁文件，再重启
```

---

## License

[MIT](LICENSE)
