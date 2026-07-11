<div align="center">

<img alt="Grok2API" src="grok-official.png" width="120" />

<h1>Grok2API</h1>

<h3>将 Grok Web 能力封装为 OpenAI / Anthropic 兼容 API 网关</h3>

<p>
  <a href="https://www.python.org/"><img alt="Python" src="https://img.shields.io/badge/python-3.13%2B-3776AB?logo=python&logoColor=white"></a>
  <a href="https://fastapi.tiangolo.com/"><img alt="FastAPI" src="https://img.shields.io/badge/FastAPI-0.137%2B-009688?logo=fastapi&logoColor=white"></a>
  <a href="https://github.com/hmtdpetn/grok2api"><img alt="Docker" src="https://img.shields.io/badge/Docker-ready-2496ED?logo=docker&logoColor=white"></a>
  <a href="LICENSE"><img alt="License" src="https://img.shields.io/badge/license-MIT-16a34a"></a>
</p>

</div>

> [!NOTE]
> 本项目仅供学习与研究交流。请遵守 Grok 的使用条款及当地法律法规。基于 [chenyme/grok2api](https://github.com/chenyme/grok2api) 与 [jiujiu532/grok2api](https://github.com/jiujiu532/grok2api) 二次开发。

---

## 核心特性

| 能力 | 说明 |
| :--- | :--- |
| **OpenAI 兼容** | `/v1/chat/completions`、`/v1/responses`、`/v1/images/generations`、`/v1/videos` |
| **Anthropic 兼容** | `/v1/messages`（Claude SDK 直接对接） |
| **多账号池** | basic / super / heavy 三级池，自动负载均衡与配额同步 |
| **免费账号** | `console.x.ai` SSO Token，`*-console` 模型零成本使用 |
| **媒体生成** | 文生图、图像编辑、文生视频、图生视频，本地缓存与代理链接 |
| **管理后台** | WebUI 配置管理、账号管理、批量操作、缓存管理 |
| **🆕 防封版** | WARP + Privoxy + FlareSolverr 一键部署，突破 GFW 与 Cloudflare 封锁 |
| **🆕 智能重试** | 图片生成失败自动换号，CDN 下载降级，详细中文错误提示 |
| **🆕 批量管理** | 跨页全选、批量禁用/恢复、一键删除失效账号 |

### 本仓库附加改动

相较于 `jiujiu532/grok2api`，本仓库额外提供：

- **图片失败可读化**：上游 HTTP 状态会转换成中文原因；本地媒体缓存下载失败时，非 Base64 响应会降级返回原始 CDN URL。
- **并发图片容错**：同一请求中的单张失败不会立即中断其他图片；全部失败时返回首个可读错误。
- **批量账号操作**：按筛选条件跨页全选，显示已选数量，并可批量禁用、恢复或删除。
- **防封版自动配置**：防封版从当前目录构建镜像，并在首次启动时写入 Privoxy 与 FlareSolverr 配置；数据和日志使用 Docker 命名卷持久化。

---

## 架构说明

### 标准版 vs 防封版

| | 标准版 | 防封版 |
| :--- | :--- | :--- |
| **适用场景** | IP 干净，能直连 grok.com | IP 被封锁（中国大陆等），需代理 |
| **部署方式** | Docker 单容器 / Docker Compose | Docker Compose（4 个服务） |
| **代码来源** | 当前文件直接拉取 `jiujiu532` 的预构建镜像 | 从当前仓库本地构建镜像 |
| **出口网络** | 直连 | WARP WireGuard 隧道 → Cloudflare 全球网络 |
| **CF 反爬** | 可能被 403 拦截 | FlareSolverr 自动解 JS 挑战 |
| **成功率** | ~30%（被墙环境） | ~95%+ |

### 防封版网络链路

```
grok2api 容器
  → FlareSolverr (获取 cf_clearance Cookie)
  → Privoxy (HTTP → SOCKS5 转换)
  → WARP (WireGuard 加密隧道)
  → Cloudflare 全球网络出口
  → grok.com / console.x.ai / assets.grok.com
```

> **重要**：防封版仅支持 Docker 部署，因为 WARP 容器需要 `NET_ADMIN` 内核能力和 Linux 容器引擎。Windows/macOS 请使用 Docker Desktop 的 Linux 容器模式，并确保虚拟化/WSL2 可用；部分嵌套虚拟机环境可能不支持这些能力。

> **版本提醒**：当前 `docker-compose.yml` 直接使用 `ghcr.io/jiujiu532/grok2api:latest`，不会构建本仓库工作目录；上面的本仓库附加改动由 `docker-compose.warp.yml` 的本地构建提供。若标准版也要使用这些改动，应先将标准版编排改为本地构建。

---

## 快速开始

### 前提条件

- **标准版**: Docker 或 Python 3.13+ + [uv](https://docs.astral.sh/uv/)
- **防封版**: Docker + Docker Compose；Linux 可直接运行，Windows/macOS 请安装 Docker Desktop

### 标准版部署

**Docker Compose（推荐）:**

```bash
git clone https://github.com/hmtdpetn/grok2api
cd grok2api
cp .env.example .env
docker compose up -d
```

访问 `http://localhost:8000/admin/login`，默认密码 `grok2api`。

**本地 Python 运行:**

```bash
git clone https://github.com/hmtdpetn/grok2api
cd grok2api
cp .env.example .env
uv sync
uv run granian --interface asgi --host 0.0.0.0 --port 8000 --workers 1 app.main:app
```

### 防封版部署

```bash
git clone https://github.com/hmtdpetn/grok2api
cd grok2api
# 一条命令启动全套防封服务
docker compose -f docker-compose.warp.yml up -d
```

启动后访问 `http://localhost:8000/admin/login`。

四个服务会自动协同工作：
| 容器 | 作用 |
| :--- | :--- |
| `warp-proxy` | Cloudflare WARP WireGuard 隧道 |
| `privoxy` | HTTP → SOCKS5 代理转换 |
| `flaresolverr` | 自动解 Cloudflare JS 挑战 |
| `grok2api` | 主应用（API 网关 + 管理后台） |

**Windows 防封版启动器：** 双击 `启动-Grok2API-防封版.cmd`。它会先检查 `http://localhost:8000/health`；服务已就绪时直接打开管理后台，否则使用 `docker compose -f docker-compose.warp.yml up -d` 创建或启动完整防封栈，并等待服务就绪。

### 首次配置

1. 打开 `http://localhost:8000/admin/login`，默认密码 `grok2api`
2. 设置 `app.app_key`（管理密码）、`app.api_key`（API 密钥）
3. 设置 `app.app_url`（公网地址，图片/视频链接需要）
4. 在账号管理页面导入你的 Grok Token

---

## 账号配置

### 获取 Token

**付费账号 (grok.com):**
1. 浏览器登录 [grok.com](https://grok.com)
2. F12 → Application → Cookies → 复制对应 Cookie

**免费账号 (console.x.ai):**
1. 浏览器访问 [console.x.ai](https://console.x.ai)
2. F12 → Network → 任意请求 → Cookies → 复制 `sso` 值

### 账号池

| 池 | 适用模型 | Token 类型 |
| :--- | :--- | :--- |
| **basic** | `grok-*-console`, `grok-*-fast`, `grok-imagine-image-lite` | 免费/基础付费 |
| **super** | `grok-*-auto`, `grok-*-expert`, `grok-imagine-image` | 高级付费 |
| **heavy** | `grok-*-heavy`, `grok-*-multi-agent` | 顶级付费 |

> Token 导入后系统会自动探测配额并分池。失效账号（status=expired）可在管理后台一键批量删除。

---

## 模型列表

### 聊天模型 (grok.com)

| 模型 | 模式 | 池 |
| :--- | :--- | :--- |
| `grok-4.20-fast` / `grok-4.3-fast` | fast | basic |
| `grok-4.20-auto` | auto | super |
| `grok-4.20-expert` | expert | super |
| `grok-4.20-heavy` | heavy | heavy |
| `grok-4.20-multi-agent-0309` | heavy | heavy |
| `grok-4.3-beta` | grok-420 | super |

### 免费 Console 模型 (console.x.ai)

| 模型 | 推理强度 |
| :--- | :--- |
| `grok-4.3-console` | 用户指定（默认 medium） |
| `grok-4.3-low` | low |
| `grok-4.3-medium` | medium |
| `grok-4.3-high` | high |
| `grok-4.20-multi-agent-console` | 用户指定 |

> Console 配额：每 15 分钟窗口 30 次请求，系统自动轮换账号。

### 图像/视频模型

| 模型 | 能力 | 池 |
| :--- | :--- | :--- |
| `grok-imagine-image-lite` | 文生图 | basic |
| `grok-imagine-image` | 文生图 | super |
| `grok-imagine-image-pro` | 文生图 (高质量) | super |
| `grok-imagine-image-edit` | 图像编辑 | super |
| `grok-imagine-video` | 文生视频 | super |

---

## API 端点

### OpenAI 兼容

| 端点 | 说明 |
| :--- | :--- |
| `GET /v1/models` | 模型列表 |
| `POST /v1/chat/completions` | 聊天补全（支持流式） |
| `POST /v1/responses` | Responses API |
| `POST /v1/images/generations` | 图片生成 |
| `POST /v1/images/edits` | 图片编辑 |
| `POST /v1/videos` | 视频生成（异步） |
| `GET /v1/videos/{id}` | 查询视频任务 |
| `GET /v1/videos/{id}/content` | 下载视频 |

### Anthropic 兼容

| 端点 | 说明 |
| :--- | :--- |
| `POST /v1/messages` | Messages API（支持 Claude SDK） |

### 管理后台

| 端点 | 说明 |
| :--- | :--- |
| `GET /admin/login` | 登录页 |
| `GET /admin/account` | 账号管理 |
| `GET /admin/config` | 配置管理 |
| `GET /admin/cache` | 缓存管理 |
| `GET /health` | 健康检查 |

### 使用示例

```bash
# 付费模型聊天
curl http://localhost:8000/v1/chat/completions \
  -H "Authorization: Bearer $API_KEY" \
  -H "Content-Type: application/json" \
  -d '{"model":"grok-4.3-fast","messages":[{"role":"user","content":"你好"}]}'

# 免费 Console 聊天
curl http://localhost:8000/v1/chat/completions \
  -H "Authorization: Bearer $API_KEY" \
  -H "Content-Type: application/json" \
  -d '{"model":"grok-4.3-console","messages":[{"role":"user","content":"你好"}]}'

# 图片生成
curl http://localhost:8000/v1/images/generations \
  -H "Authorization: Bearer $API_KEY" \
  -H "Content-Type: application/json" \
  -d '{"model":"grok-imagine-image-lite","prompt":"A red apple","n":1,"size":"1024x1024"}'

# Anthropic SDK 兼容
curl http://localhost:8000/v1/messages \
  -H "x-api-key: $API_KEY" \
  -H "Content-Type: application/json" \
  -d '{"model":"grok-4.3-fast","max_tokens":1024,"messages":[{"role":"user","content":"Hello"}]}'
```

---

## 配置说明

### 图片格式

通过管理后台 `http://localhost:8000/admin/config` 修改 `features.image_format`：

| 值 | 说明 |
| :--- | :--- |
| `grok_url` | 直接返回 Grok CDN URL |
| `local_url` | 下载后存本地，返回本地代理 URL |
| `grok_md` | Markdown 内嵌 Grok CDN URL |
| `local_md` | Markdown 内嵌本地代理 URL |
| `base64` | Markdown 内嵌 Base64 |

> 在中国大陆建议使用 `local_url` 或 `local_md`。CDN 不可达时会自动降级。

### 代理模式

修改 `proxy.egress.mode`：

| 模式 | 配置 | 说明 |
| :--- | :--- | :--- |
| `direct` | 无需配置 | 直连 grok.com |
| `single_proxy` | 设置 `proxy_url` | 单代理（防封版自动设为 privoxy） |
| `proxy_pool` | 设置 `proxy_pool` | 代理池轮换 |

### CF Clearance 模式

修改 `proxy.clearance.mode`：

| 模式 | 说明 |
| :--- | :--- |
| `none` | 不使用（标准版） |
| `manual` | 手动填入 `cf_cookies` + `user_agent` |
| `flaresolverr` | FlareSolverr 自动获取（防封版） |

---

## 错误提示说明

本版本改进了错误提示，不再显示笼统的 "Internal server error"：

| 返回错误 | 含义 | 处理建议 |
| :--- | :--- | :--- |
| `HTTP 403 — Cloudflare 反爬拦截` | IP/指纹被 CF 识别 | 启用防封版或更换代理 |
| `HTTP 429 — 图片生成额度已用尽` | 该账号配额耗尽 | 系统自动换号，无需处理 |
| `HTTP 401 — Token 已过期或无效` | 账号失效 | 在管理后台删除该账号 |
| `图片生成未返回结果 — 内容审核拦截` | 提示词被内容审核 | 更换提示词 |
| `CDN下载失败(已降级为远程URL)` | assets.grok.com 不可达 | 自动降级，不影响使用 |

---

## 环境变量

| 变量 | 默认值 | 说明 |
| :--- | :--- | :--- |
| `TZ` | `Asia/Shanghai` | 时区 |
| `LOG_LEVEL` | `INFO` | 日志级别 |
| `SERVER_HOST` | `0.0.0.0` | 监听地址 |
| `SERVER_PORT` | `8000` | 监听端口 |
| `ACCOUNT_STORAGE` | `local` | 账号存储后端 (`local`/`redis`/`mysql`/`postgresql`) |
| `ACCOUNT_LOCAL_PATH` | `data/accounts.db` | SQLite 路径 |
| `DATA_DIR` | `./data` | 数据目录 |
| `LOG_DIR` | `./logs` | 日志目录 |

---

## Docker Compose 文件

| 文件 | 用途 |
| :--- | :--- |
| `docker-compose.yml` | 标准版 — 单容器直连部署 |
| `docker-compose.warp.yml` | 防封版 — WARP + Privoxy + FlareSolverr 全套（5 容器） |

---

## 常见问题

### 图片生成失败率很高怎么办？
中国大陆用户建议部署防封版 (`docker compose -f docker-compose.warp.yml up -d`)，可解决 90%+ 的网络问题。

### 管理后台加载不出来？
检查端口映射和防火墙：`docker compose ps`。

### 图片/视频链接返回 403？
在管理后台设置 `app.app_url` 为你的公网访问地址。

### 如何批量管理账号？
账号管理页面使用筛选器 → 点击表头全选（跨页全选所有匹配账号）→ 使用工具栏按钮批量禁用/恢复/删除。

### 如何更新代码？
```bash
git pull
docker compose -f docker-compose.warp.yml build grok2api
docker compose -f docker-compose.warp.yml up -d grok2api
```

### 防封版在虚拟化环境中无法启动？
确认 Docker 正在运行 Linux 容器，并检查该环境是否允许 `NET_ADMIN` 能力。若嵌套虚拟化或安全策略阻止 WARP 容器，请使用标准版并配置自己的代理。

---

## 目录结构

```
grok2api/
├── app/                     # 应用代码 (26,500+ 行 Python)
│   ├── main.py              # 入口 (FastAPI + Granian ASGI)
│   ├── products/            # API 产品层
│   │   ├── openai/          # Chat, Images, Videos, Responses
│   │   ├── anthropic/       # Messages API
│   │   └── web/             # Admin 后台 + WebUI
│   ├── dataplane/           # 数据面 (逆向 Grok Web 协议)
│   │   ├── reverse/         # HTTP/WebSocket/gRPC 协议实现
│   │   ├── account/         # 账号热路径选号
│   │   └── proxy/           # 代理适配 (curl_cffi + SOCKS5)
│   ├── control/             # 控制面 (账号池/模型注册/代理调度)
│   ├── platform/            # 平台层 (配置/日志/认证/存储)
│   └── statics/             # 前端 (HTML/CSS/JS + i18n 六语言)
├── scripts/                 # 部署脚本
│   ├── entrypoint.sh        # 容器入口 (自动写代理配置)
│   ├── init_storage.sh      # 存储初始化
│   └── init_proxy_config.py # 防封版代理配置写入
├── 启动-Grok2API-防封版.cmd  # Windows 防封版启动器
├── docker-compose.yml       # 标准版
├── docker-compose.warp.yml  # 防封版 (本地构建)
├── Dockerfile               # 镜像构建 (Alpine + Rust + curl_cffi)
├── Dockerfile.privoxy       # Privoxy 镜像
├── config.defaults.toml     # 默认配置模板
├── pyproject.toml           # Python 项目定义
├── uv.lock                  # 依赖锁定 (47 个包)
└── tests/                   # 测试
```

---

## 致谢

- [chenyme/grok2api](https://github.com/chenyme/grok2api) — 原始项目
- [jiujiu532/grok2api](https://github.com/jiujiu532/grok2api) — 多账号池与防封版基础

---

## License

MIT License. 详见 [LICENSE](LICENSE)。
