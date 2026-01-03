# SillyTavern Telegram Integration v2.0

通过 Telegram Bot 与 SillyTavern 进行双向交互，**完整支持预设、世界书、高端角色卡**。

## 功能特性

| 功能 | 描述 |
|------|------|
| 🎭 角色切换 | 在 Telegram 中选择 SillyTavern 角色 |
| 📋 预设支持 | 读取并应用 SillyTavern 预设（Main Prompt, Jailbreak 等） |
| 📚 世界书支持 | 自动匹配 WorldInfo/Lorebook 条目，支持常驻条目 |
| 💬 完整对话 | 保持对话历史，支持长对话 |
| 🔄 多开场白 | 支持 alternate_greetings 切换 |
| ⏰ 时间宏 | 支持 `{{time}}`, `{{date}}`, `{{weekday}}` 等 |
| 🎲 随机宏 | 支持 `{{random:a,b,c}}`, `{{roll:2d6}}` 骰子 |
| 🔒 用户授权 | 仅允许指定用户使用 Bot |
| 🐳 Docker 部署 | 一键部署，开箱即用 |

## 目录结构

```
sillytavern-telegram/
├── .env.example              # 环境变量示例
├── docker-compose.yml        # Docker Compose 配置
├── config/                   # SillyTavern 配置目录
├── data/                     # SillyTavern 数据目录
│   ├── default-user/
│   │   ├── characters/       # 角色卡 (*.json)
│   │   ├── OpenAI Settings/  # 预设 (*.json)
│   │   └── worlds/           # 世界书 (*.json)
├── plugins/                  # SillyTavern 插件目录
│   └── telegram-integration/
│       ├── index.js          # 插件入口
│       └── package.json
├── telegram-bot/
│   ├── bot.py                # Bot 主程序
│   ├── Dockerfile
│   └── requirements.txt
└── nginx/                    # Nginx 配置（可选）
```

## 快速开始

> 本项目默认“Docker 在服务器上跑”，本地只负责改代码/传文件；部署与排障以服务器环境为准。

### 1. 获取 Telegram Bot Token

1. 在 Telegram 中找到 [@BotFather](https://t.me/BotFather)
2. 发送 `/newbot` 创建新机器人
3. 保存获取到的 Bot Token

### 2. 获取你的 Telegram 用户 ID

1. 在 Telegram 中找到 [@userinfobot](https://t.me/userinfobot)
2. 发送任意消息，保存返回的用户 ID

### 3. 配置环境变量

```bash
cp .env.example .env
```

**必填配置：**
```bash
# Telegram Bot
TELEGRAM_BOT_TOKEN=你的Bot_Token
ALLOWED_USER_ID=你的用户ID

# LLM API（用于 AI 回复）
LLM_API_KEY=你的OpenAI_API_Key
LLM_MODEL=gpt-4o-mini
```

### 4. 准备角色和预设

你可以：
- **方式 A**：先运行 SillyTavern Web UI，创建角色和预设
- **方式 B**：将现有的角色卡（.json）复制到 `data/default-user/characters/`

### 5. 启动服务

```bash
# 首次启动（构建镜像）
docker compose up -d --build

# 后续启动
docker compose up -d

# 查看日志
docker compose logs -f

# 停止服务
docker compose down
```

**常用命令：**
```bash
# 重新构建（代码更新后）
docker compose up -d --build

# 查看运行状态
docker compose ps

# 查看 Bot 日志
docker compose logs telegram-bot

# 查看插件加载日志
docker compose logs sillytavern | grep TG

# 重启单个服务
docker compose restart sillytavern
```

**代码更新与重启策略（重要）**
- 修改 `telegram-bot/bot.py`（Bot 代码）：需要重建并更新容器：`docker compose up -d --build --force-recreate telegram-bot`（注意 `--force-recreate` 是一个完整参数，不要写成 `--force- recreate`）
- 修改 `plugins/telegram-integration/index.js`（插件代码）：只需重启 SillyTavern：`docker compose restart sillytavern`
- 修改 `.env`（环境变量）：用 `docker compose up -d` 重新创建容器（`restart` 不会更新 env）
- 数据安全说明：`./data`、`./config`、`./plugins` 都是挂载目录，重建容器不会丢角色卡/预设/世界书/配置
- 注意：对话历史目前是内存会话，重启/重建 `sillytavern` 会清空历史（角色卡和配置不受影响）

### 6. 开始使用

在 Telegram 中找到你的 Bot，发送 `/start`：
1. 选择角色
2. 选择预设
3. （可选）选择世界书
4. 开始对话！

## 服务器部署（推荐流程）

### 0. 前置条件

- 服务器已安装 Docker + Docker Compose（`docker compose version` 可用）
- 服务器可访问 Telegram（Polling/Webhook）以及你的 LLM API（如 OpenAI/OpenRouter）
- 如需访问 SillyTavern Web UI：开启端口映射或使用隧道/反代

### 1. 初始化目录与配置

```bash
cp .env.example .env
```

建议先把数据目录建好（避免“列表为空”其实是目录不存在）：

```bash
mkdir -p "data/default-user/characters" "data/default-user/OpenAI Settings" "data/default-user/worlds"
```

### 2. 启动

```bash
docker compose up -d --build
docker compose ps
```

### 2.1 Polling vs Webhook

- 默认 **Polling**（推荐）：`WEBHOOK_URL` 留空即可；不需要暴露 `telegram-bot` 端口。
- 使用 **Webhook**：设置 `WEBHOOK_URL`（外网可访问的 HTTPS 地址），并在 `docker-compose.yml` 里取消 `telegram-bot` 的端口映射/反代配置（默认监听 `8443`，路径 `/webhook`）。

### 3. 验证清单（最常用）

- 插件是否加载：`docker compose logs sillytavern --tail=200`（过滤可用 `grep "\[TG\]"` 或 `findstr [TG]`）
- 数据路径是否正确：日志应出现 `Data path resolved: /home/node/app/data/default-user`
- 预设/世界书文件是否在容器内可见：
  - `docker compose exec sillytavern sh -lc 'ls -la "/home/node/app/data/default-user/OpenAI Settings" && ls -la /home/node/app/data/default-user/worlds'`
- 插件接口是否能返回数据（容器内自测，不依赖外网）：
  - `docker compose exec sillytavern sh -lc 'node -e "fetch(\"http://127.0.0.1:8000/api/plugins/telegram-integration/presets\").then(r=>r.text()).then(console.log).catch(console.error)"'`
  - `docker compose exec sillytavern sh -lc 'node -e "fetch(\"http://127.0.0.1:8000/api/plugins/telegram-integration/worldinfo\").then(r=>r.text()).then(console.log).catch(console.error)"'`

### 4. 更新/回滚

- 更新代码后：上传文件或 `git pull` → `docker compose up -d --build`
- 仅更新插件 JS：上传 `plugins/telegram-integration/index.js` 后 `docker compose restart sillytavern`
- 仅更新 Bot：上传 `telegram-bot/bot.py` 后 `docker compose restart telegram-bot`
- 仅更新 `.env`：需要重建容器以重新加载环境变量（不需要重建镜像）
  - 推荐：`docker compose up -d --force-recreate`
  - 只影响单个服务：`docker compose up -d --force-recreate sillytavern` 或 `docker compose up -d --force-recreate telegram-bot`
- 回滚：`git checkout <commit> -- <file>` → `docker compose up -d --build`

## 已知限制/注意事项（逻辑层）

- “预设/世界书/角色为空”最常见原因是 `data/default-user/` 对应目录没有文件或没挂载进容器。
- Telegram 菜单里预设/世界书使用索引回调（`telegram-bot/bot.py`），如果你在“打开列表后”立刻改名/删文件，可能提示列表过期，重新打开菜单即可（旧按钮可能失效）。
- 角色列表使用“遍历目录生成的递增 id”（`plugins/telegram-integration/index.js`），如果你频繁增删文件导致排序变化，历史会话里保存的 `characterId` 可能指向不同角色。
- SillyTavern 支持多用户目录（`/home/node/app/data/<user>`）；本项目默认使用 `default-user`，请把数据放到 `data/default-user/`。
- `sillytavern` 容器启动会执行一次 `npm install --prefix /home/node/app/plugins/telegram-integration`；如果服务器无法访问 npm（无外网/受限网络），需要提前把依赖装好或改为自定义镜像。
- 容器文件系统是临时的：不要在容器里“手改代码/拷文件”当作持久化；应修改宿主机目录（本项目用 `./data`/`./config`/`./plugins` 挂载保证持久化）。
- `docker compose down` 不会删除挂载目录；但 `docker compose down -v` 会删除命名卷（如果你后续引入了 volumes，谨慎使用）。

## 常见坑（排障速查）

- 预设/世界书/角色全空：优先检查宿主机目录是否有文件（`data/default-user/...`），以及容器内是否可见（见“验证清单”）。
- `Can't parse entities`：通常是 Markdown 解析失败（角色名/预设名/开场白/LLM 回复含不完整的 `* _ [ ]` 等）；Bot 已做自动降级为纯文本，但若你强依赖富文本建议改成 HTML 模式。
- “list expired”：点了旧菜单按钮或 Bot 重启后内存状态丢失；重新打开对应菜单即可（Bot 会尝试自动刷新一次）。
- 权限/属主问题：容器内 `node` 用户可能无权读写某些目录；优先确保宿主机 `data/`、`plugins/` 目录权限合理（避免 `Permission denied`）。

## 命令列表

| 命令 | 描述 |
|------|------|
| `/start` | 主菜单 |
| `/help` | 帮助信息 |
| `/status` | 当前状态（角色、预设、世界书） |
| `/chars` | 角色列表 |
| `/presets` | 预设列表 |
| `/worlds` | 世界书列表 |
| `/clear` | 清除对话历史 |
| `/model` | （管理员）查看/设置默认模型：`/model <模型名>`（别名：`/llm`） |
| `/mymodel` | 查看/设置“我的模型”（仅对自己生效）：`/mymodel <模型名>` / `/mymodel clear`（别名：`/umodel`） |
| `/delmodel` | 删除“我的模型”覆盖（恢复默认） |
| `/register` | 申请/使用邀请码开通权限 |
| `/invite` | （管理员）生成一次性邀请码 |
| `/users` | （管理员）查看已授权用户 |
| `/pending` | （管理员）查看待审批申请 |
| `/approve` | （管理员）通过申请 |
| `/revoke` | （管理员）移除授权 |
| `/registration` | （管理员）开/关注册 |

### 模型切换说明

- “默认模型”：管理员用 `/model <模型名>` 写入插件配置（持久化到 `plugins/telegram-integration/config.json`），影响所有用户未覆盖的情况。
- “我的模型”：用户用 `/mymodel <模型名>` 设置个人覆盖（持久化到 `data/telegram-bot/auth.json`），只影响自己；用 `/delmodel` 或 `/mymodel clear` 删除覆盖恢复默认。

## 配置说明

### 环境变量

| 变量名 | 必填 | 默认值 | 描述 |
|--------|------|--------|------|
| `TELEGRAM_BOT_TOKEN` | ✅ | - | Telegram Bot Token |
| `ALLOWED_USER_ID` | ✅ | 0 | 允许使用的用户 ID（0=允许所有） |
| `LLM_API_KEY` | ✅ | - | LLM API 密钥 |
| `LLM_API_URL` | ❌ | https://api.openai.com/v1 | LLM API 端点 |
| `LLM_MODEL` | ❌ | gpt-4o-mini | 模型名称 |
| `LLM_MAX_TOKENS` | ❌ | 2048 | 最大生成 token |
| `LLM_TEMPERATURE` | ❌ | 0.9 | 温度参数（0.0-2.0） |
| `PRESET_NAME` | ❌ | Default | 默认预设名称 |
| `CONTEXT_SIZE` | ❌ | 8192 | 上下文长度限制（tokens） |
| `DEFAULT_WORLD_INFO` | ❌ | - | 默认世界书名称 |
| `WEBHOOK_URL` | ❌ | - | Webhook URL（生产环境） |
| `LOG_LEVEL` | ❌ | INFO | 日志级别 |

| `TG_AUTH_DB_PATH` | 可选 | /app/data/auth.json | 机器人授权数据库路径（持久化） |
| `TG_REGISTRATION_ENABLED` | 可选 | 1 | 默认是否开放注册（可用 /registration 切换） |
| `TG_CONCURRENT_UPDATES` | 可选 | 8 | Bot 并发处理消息数（多用户建议调大） |
| `TG_CONNECTION_POOL_SIZE` | 可选 | 64 | Telegram Bot API 连接池大小 |
| `TG_POOL_TIMEOUT` | 可选 | 30 | 连接池等待超时（秒） |
| `TELEGRAM_STREAM_RESPONSES` | 可选 | 1 | 启用“输入中”与流式编辑（SSE） |
| `TELEGRAM_STREAM_EDIT_INTERVAL_MS` | 可选 | 750 | 流式编辑刷新间隔（毫秒） |
| `TELEGRAM_TYPING_INTERVAL_MS` | 可选 | 3500 | 发送 typing 动作间隔（毫秒） |
| `TELEGRAM_STREAM_PLACEHOLDER` | 可选 | 输入中... | 首条占位文本 |

**配置示例：**
```bash
# 必填
TELEGRAM_BOT_TOKEN=123456:ABC-DEF...
ALLOWED_USER_ID=987654321
LLM_API_KEY=sk-xxxxxxxx

# 可选 - LLM 设置
LLM_API_URL=https://api.openai.com/v1
LLM_MODEL=gpt-4o
LLM_MAX_TOKENS=4096
LLM_TEMPERATURE=0.8

# 可选 - 预设和世界书
PRESET_NAME=MyPreset
CONTEXT_SIZE=16384
DEFAULT_WORLD_INFO=MyLorebook

# 可选 - 多用户授权（机器人层）
TG_AUTH_DB_PATH=/app/data/auth.json
TG_REGISTRATION_ENABLED=1

# 可选 - 多用户性能
TG_CONCURRENT_UPDATES=8
TG_CONNECTION_POOL_SIZE=64
TG_POOL_TIMEOUT=30

# 可选 - Telegram 流式/typing
TELEGRAM_STREAM_RESPONSES=1
TELEGRAM_STREAM_EDIT_INTERVAL_MS=750
TELEGRAM_TYPING_INTERVAL_MS=3500
TELEGRAM_STREAM_PLACEHOLDER=输入中...
```

### 预设支持

插件会读取 `data/default-user/OpenAI Settings/` 目录下的预设文件，支持：

- **Main Prompt** - 主提示词
- **Jailbreak** - 越狱提示词（Post-History Instructions）
- **采样参数** - temperature, top_p, frequency_penalty 等

### 世界书支持

插件会读取 `data/default-user/worlds/` 目录下的世界书，支持：

- **关键词匹配** - 自动在对话中触发
- **常驻条目** - `constant: true` 的条目始终激活
- **位置控制** - before/after 消息
- **排序** - 按 `order` 字段排序插入

### 宏替换

支持以下宏（在角色卡、预设、世界书中均可使用）：

| 宏 | 说明 | 示例输出 |
|----|------|----------|
| `{{char}}` | 角色名 | Alice |
| `{{user}}` | 用户名 | Bob |
| `{{time}}` | 当前时间 | 3:45 PM |
| `{{date}}` | 当前日期 | January 1, 2026 |
| `{{weekday}}` | 星期 | Wednesday |
| `{{isotime}}` | ISO 时间 | 15:45:30 |
| `{{isodate}}` | ISO 日期 | 2026-01-01 |
| `{{idle_duration}}` | 距上次消息 | 5 minutes ago |
| `{{random:a,b,c}}` | 随机选择 | b |
| `{{roll:2d6}}` | 掷骰子 | 7 |
| `{{roll:1d20+5}}` | 骰子+加值 | 15 |

### 多开场白

角色卡支持 `alternate_greetings` 字段（多个开场白）：
- 选择角色后，如有多个开场白会显示切换按钮
- 支持 ⬅️ 上一个 / ➡️ 下一个 / 🎲 随机

## 支持的 LLM API

| 服务商 | API URL | 说明 |
|--------|---------|------|
| OpenAI | https://api.openai.com/v1 | 官方 API |
| OpenRouter | https://openrouter.ai/api/v1 | 多模型聚合 |
| Azure OpenAI | https://xxx.openai.azure.com | Azure 托管 |
| 本地 LLM | http://localhost:5001/v1 | KoboldCpp/Ollama 等 |

## 优化方向（Roadmap）

- Bot 富文本：从 `Markdown` 逐步切换为 `HTML`（并统一转义），减少 `Can't parse entities` 的不确定性。
- 菜单分页与唯一标识：为角色/预设/世界书提供分页与稳定 id（避免名称重复、避免角色 id 随目录顺序变化）。
- 依赖安装方式：避免容器启动时 `npm install`（改为构建自定义 SillyTavern 镜像或提供离线依赖包），提升启动速度与可控性。
- 多用户目录：显式配置使用哪个用户目录（`default-user`/`node` 等），或通过 `.env` 指定。
- 会话持久化：将 `telegramSessions` 从内存迁移到持久化存储（SQLite/Redis），支持容器重启后保留会话状态。

## API 接口

所有 API 通过 SillyTavern 插件系统暴露，路径前缀为 `/api/plugins/telegram-integration`。

| 端点 | 方法 | 描述 |
|------|------|------|
| `/health` | GET | 健康检查 |
| `/characters` | GET | 角色列表 |
| `/presets` | GET | 预设列表 |
| `/worldinfo` | GET | 世界书列表 |
| `/character/switch` | POST | 切换角色 |
| `/greeting/switch` | POST | 切换开场白 |
| `/session` | GET | 会话信息 |
| `/session/preset` | POST | 设置预设 |
| `/session/worldinfo` | POST | 设置世界书 |
| `/history` | GET | 历史记录 |
| `/history/summary` | GET | 历史汇总（按角色） |
| `/history/clear` | POST | 清除当前角色历史 |
| `/history/clear/all` | POST | 清除全部历史（所有角色） |
| `/send` | POST | 发送消息 |
| `/greeting` | GET | 获取开场白 |

## 故障排除

### Bot 无响应

1. 检查 Bot Token 是否正确
2. 检查 `ALLOWED_USER_ID` 是否正确
3. 查看日志：`docker compose logs telegram-bot`

### 无法连接 SillyTavern

1. 确保 SillyTavern 容器正在运行：`docker compose ps`
2. 检查插件是否加载：`docker compose logs sillytavern | grep TG`

### 角色列表为空

1. 确保 `data/default-user/characters/` 目录存在
2. 添加角色卡文件（.json 格式）

### 预设列表为空

1. 确保 `data/default-user/OpenAI Settings/` 目录存在
2. 添加预设文件或使用默认预设

### 世界书列表为空

1. 确保 `data/default-user/worlds/` 目录存在
2. 添加世界书文件（`.json`）后重启：`docker compose restart sillytavern`

### Telegram 提示 Can't parse entities

1. 一般是 Markdown 格式不完整导致（来自角色名/开场白/LLM 回复）
2. 可先验证 Bot 端是否已更新到最新 `telegram-bot/bot.py`（已做降级处理）

### Telegram 提示 list expired

1. 重新打开 `/presets` 或 `/worlds` 菜单即可
2. 如频繁遇到，避免在 Bot 运行时改名/批量增删对应文件

### AI 回复错误

1. 检查 `LLM_API_KEY` 是否正确
2. 确认 API 账户有足够额度
3. 查看日志获取详细错误信息

## 安全说明

- SillyTavern 默认不暴露到外网
- 建议设置 `ALLOWED_USER_ID` 限制访问
- 不要将 `.env` 文件提交到版本控制
- API Key 仅保存在环境变量中

## SillyTavern 配置

项目已包含 `config/config.yaml` 配置文件，支持 Docker 内部通信和 Cloudflare 隧道访问：

```yaml
listen: true
whitelistMode: true
whitelist:
  - "0.0.0.0/0"
  - "::/0"
enableForwardedWhitelist: true
basicAuthMode: false
disableCsrfProtection: true
hostWhitelist:
  enabled: false
  scan: false
enableServerPlugins: true
```

**配置说明：**

| 配置项 | 值 | 说明 |
|--------|-----|------|
| `whitelistMode` | `true` | 必须为 true，否则会因安全检查无限重启 |
| `whitelist` | `["0.0.0.0/0", "::/0"]` | 允许所有 IPv4/IPv6（端口未暴露，实际安全） |
| `enableForwardedWhitelist` | `true` | 支持 Cloudflare 隧道等转发 IP |
| `disableCsrfProtection` | `true` | API 调用需要禁用 CSRF |
| `hostWhitelist.enabled` | `false` | 禁用主机名检查 |

**可选：启用密码保护**

如需通过隧道访问 Web UI 并启用密码：

1. 修改 `config/config.yaml`：
```yaml
basicAuthMode: true
basicAuthUser:
  username: "admin"
  password: "your_password"
```

2. 在 `.env` 中添加（Bot 需要同样的密码）：
```bash
ST_AUTH_USER=admin
ST_AUTH_PASS=your_password
```

3. 重新构建：`docker compose up -d --build`

## 许可证

本项目采用 Apache License 2.0，详见 `LICENSE`。
