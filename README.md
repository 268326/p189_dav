# 天翼网盘 302 直链服务

基于 Docker 的天翼网盘 302 直链服务，用于 Emby / Jellyfin 等媒体服务器的 302 播放。

## ✨ 功能特性

- ✅ **302 直链重定向** — 请求文件路径自动获取天翼网盘下载直链并 302 重定向
- ✅ **多种登录方式** — 扫码登录、账号密码登录、Cookies 登录
- ✅ **多账号管理** — 支持多个天翼网盘账号，通过路径前缀区分（`/账号key/路径`）
- ✅ **智能缓存** — 路径 → 文件ID 缓存 + 文件ID → 下载链接缓存，大幅减少 API 调用
- ✅ **账号健康检查** — 定时检测账号状态，异常时自动 Telegram 通知
- ✅ **自动重新登录** — 密码自动重登录 / 扫码二维码发送到 Telegram 重登录
- ✅ **Telegram Bot** — 账号异常通知、远程日志查看、远程健康检查
- ✅ **Web 管理界面** — 配置管理、登录管理、缓存管理一体化
- ✅ **Docker 一键部署** — 支持 amd64 / arm64，持久化 Cookies，重启免登录
- ✅ **模块化架构** — 代码按功能拆分为独立模块，便于维护和扩展

## 📁 项目结构

```
p189_dav/
├── docker-compose.yml        # Docker Compose 编排
├── Dockerfile                # Docker 镜像构建
├── requirements.txt          # Python 依赖
├── app/
│   ├── main.py               # 入口：Flask 应用创建、蓝图注册、后台线程启动
│   ├── config.py             # 配置中心：从 db/user.env 加载所有配置项
│   ├── accounts.py           # 多账号管理：P189Client 实例生命周期、Cookies 持久化
│   ├── cache.py              # 缓存核心：路径→ID 缓存、ID→URL 缓存、下载链接获取
│   ├── telegram.py           # Telegram Bot：消息推送、日志缓冲、Bot 长轮询
│   ├── health.py             # 健康检查：定时账号检测、密码/扫码自动重登录
│   ├── templete.env          # 配置模板（Web 界面读取结构用）
│   ├── routes/
│   │   ├── __init__.py       # 蓝图注册入口
│   │   ├── auth.py           # Web 认证路由（登录/登出/首页）
│   │   ├── api.py            # 状态/配置/缓存管理 API
│   │   ├── cloud.py          # 天翼网盘登录/扫码/Cookies 路由
│   │   ├── accounts.py       # 账号 CRUD / 自动登录配置 API
│   │   └── redirect.py       # 302 直链重定向路由（/d/路径 和 /路径）
│   ├── templates/            # Jinja2 HTML 模板
│   └── static/               # 静态资源（PWA manifest 等）
└── db/                       # 持久化数据目录（Docker 挂载）
    ├── user.env              # 用户配置文件
    ├── accounts.json         # 多账号配置
    └── accounts/<key>/       # 各账号 Cookies 文件
        └── cookies.txt
```

## 🚀 快速开始

### 方式1: 使用预构建镜像（推荐）

```yaml
# docker-compose.yml
services:
  tianyi-302:
    image: ghcr.io/268326/p189_dav:latest
    container_name: tianyi-302
    restart: always
    ports:
      - "8515:8515"
    volumes:
      - ./db:/app/db
    environment:
      - TZ=Asia/Shanghai
    healthcheck:
      test: ["CMD", "curl", "-f", "http://localhost:8515/api/status"]
      interval: 30s
      timeout: 10s
      retries: 3
```

```bash
docker compose up -d
```

### 方式2: 从源码构建

```bash
git clone https://github.com/268326/p189_dav.git
cd p189_dav
mkdir -p db
docker compose up -d --build
```

### 2. 访问管理界面

打开浏览器访问 `http://your-ip:8515`

默认账号：
- 用户名：`admin`
- 密码：`123456`

> 首次使用请在 Web 管理界面的**配置管理**中修改默认密码。

### 3. 登录天翼网盘

登录管理界面后，在【天翼网盘登录】标签页选择登录方式：

| 方式 | 说明 |
|------|------|
| **扫码登录**（推荐） | 点击"获取二维码"，使用天翼网盘 APP 扫描 |
| **账号密码登录** | 输入手机号/邮箱和密码（需先关闭设备锁：[关闭入口](https://e.dlife.cn/user/index.do)） |
| **Cookies 登录** | 粘贴从浏览器获取的 Cookies 字符串 |

### 4. 使用 302 直链

登录成功后，访问文件路径即可获取 302 重定向：

```bash
# 默认账号
http://your-ip:8515/电影/test.mkv
http://your-ip:8515/d/电影/test.mkv

# 多账号 — 路径第一段为账号 key
http://your-ip:8515/work/文档/report.pdf
http://your-ip:8515/d/work/文档/report.pdf

# ↓ 自动 302 重定向到天翼网盘下载直链
https://download.cloud.189.cn/file/downloadFile.action?...
```

## 📝 配置说明

所有配置统一存储在 `db/user.env`，通过 Web 管理界面的**配置管理**页面修改。  
`docker-compose.yml` 的 `environment` 仅用于时区等系统级设置，不作为应用配置来源。

### 环境变量

| 变量名 | 说明 | 默认值 |
|--------|------|--------|
| **Web 管理** | | |
| `ENV_WEB_PASSPORT` | Web 管理用户名 | `admin` |
| `ENV_WEB_PASSWORD` | Web 管理密码 | `123456` |
| **服务** | | |
| `PORT` | 服务端口 | `8515` |
| `HOST` | 监听地址 | `0.0.0.0` |
| `DEBUG` | 调试模式（`true`/`false`） | `false` |
| **天翼网盘** | | |
| `ENV_189_USERNAME` | 天翼账号（手机号/邮箱） | - |
| `ENV_189_PASSWORD` | 天翼密码 | - |
| `ENV_189_COOKIES` | Cookies 字符串（优先级最高） | - |
| `ENV_189_COOKIES_FILE` | Cookies 文件路径 | `db/cookies.txt` |
| **缓存** | | |
| `CACHE_EXPIRATION` | 下载链接缓存（分钟），0 关闭 | `720` |
| `PATH_CACHE_EXPIRATION` | 路径缓存（小时），0 关闭 | `12` |
| **Telegram** | | |
| `TG_BOT_TOKEN` | Bot Token，留空关闭 | - |
| `TG_BOT_NOTIFY_CHAT_IDS` | 通知 Chat ID（逗号分隔） | - |
| `TG_BOT_USER_WHITELIST` | `/189log` 用户白名单（逗号分隔） | - |
| `LOG_BUFFER_MAX` | 日志缓冲最大行数 | `1000` |
| **健康检查** | | |
| `ACCOUNT_CHECK_INTERVAL` | 检查间隔（分钟），0 禁用 | `30` |
| **网络** | | |
| `PROXY_URL` | HTTP/HTTPS/SOCKS5 代理 | - |

### 网络代理

设置 `PROXY_URL` 后，所有出站请求（天翼网盘 API、Telegram、扫码回调）均走该代理。

```
http://127.0.0.1:7890
http://user:pass@proxy:8080
socks5://127.0.0.1:1080
```

### 登录优先级

1. `ENV_189_COOKIES`（环境变量 Cookies）
2. Cookies 文件（`db/cookies.txt` 或 `db/accounts/<key>/cookies.txt`）
3. `ENV_189_USERNAME` + `ENV_189_PASSWORD`（账号密码）
4. Web 界面手动登录

### 数据持久化

`./db` 目录挂载到容器的 `/app/db`，包含：

| 文件/目录 | 说明 |
|-----------|------|
| `user.env` | 用户配置 |
| `accounts.json` | 多账号配置（账号列表、默认账号） |
| `cookies.txt` | 默认账号的 Cookies |
| `accounts/<key>/cookies.txt` | 各账号的 Cookies |

## 📚 API 接口

| 接口 | 方法 | 说明 |
|------|------|------|
| `/` | GET | 首页/管理界面 |
| `/login` | GET | 登录页面 |
| `/api/login` | POST | Web 管理登录 |
| `/api/logout` | GET/POST | Web 管理登出 |
| `/api/status` | GET | 检查状态（含多账号列表与登录状态） |
| `/api/env` | GET | 获取配置 |
| `/api/env` | POST | 保存配置 |
| `/api/accounts` | GET | 获取账号列表 |
| `/api/accounts` | POST | 添加账号（body: `key`, `label`） |
| `/api/accounts/set-default` | POST | 设置默认账号（body: `account_key`） |
| `/api/accounts/<key>` | DELETE | 删除账号 |
| `/api/189/login` | POST | 天翼网盘登录（body 可传 `account_key`） |
| `/api/189/logout` | POST | 天翼网盘登出（body 可传 `account_key`，不传则登出全部） |
| `/api/189/qrcode` | GET | 获取扫码登录二维码（可选 query `account_key`） |
| `/api/189/qrcode/status` | GET | 检查扫码状态（可选 query `account_key`） |
| `/api/189/cookies` | GET | 获取当前 Cookies（可选 query `account_key`） |
| `/api/clear-cache` | POST | 清除缓存（body 可传 `account_key`） |
| `/api/cache` | GET | 获取缓存详情（可选 query `account_key`） |
| `/api/cache/path` | POST | 删除单条路径缓存（body: `path`, 可选 `account_key`） |
| `/api/cache/url` | POST | 删除单条链接缓存（body: `file_id`, 可选 `account_key`） |
| `/api/health` | GET | 获取账号健康检查状态 |
| `/api/health/check` | POST | 手动触发一次健康检查 |
| `/api/accounts/auto-login` | POST | 设置账号自动登录方式（body: `account_key`, `method`, `username?`, `password?`） |
| `/api/accounts/auto-login/<key>` | GET | 获取账号自动登录配置 |
| `/d/{path}` | GET | 302 重定向到直链；路径可为 `账号key/网盘路径` |
| `/{path}` | GET | 302 重定向到直链；路径可为 `账号key/网盘路径` |

## 🤖 Telegram 通知

配置 `TG_BOT_TOKEN` 和 `TG_BOT_NOTIFY_CHAT_IDS` 后可开启 TG 通知：

- 302 直链获取失败 / 账号过期时自动通知
- 扫码重登录模式下，二维码图片直接发送到 TG
- Bot 命令（仅白名单用户）：
  - `/189log` — 最近 100 行日志
  - `/189health` — 手动触发健康检查

## 🔄 账号健康检查与自动重登录

后台定时（默认 30 分钟）检查所有已登录账号会话有效性。过期时：

| 自动登录方式 | 行为 |
|-------------|------|
| **密码** | 自动重登录，结果通知 TG |
| **扫码** | 二维码发到 TG，等待扫码（5 分钟超时） |
| **关闭** | 仅发 TG 通知 |

在 Web 管理界面的多账号列表中可独立配置每个账号的自动登录方式。

## 📺 与 Emby/Jellyfin 配合使用

在 Emby 中添加 `.strm` 文件，内容指向本服务的 302 地址：

```
http://your-ip:8515/电影/你的电影.mkv
http://your-ip:8515/账号key/电影/你的电影.mkv
```

Emby 播放时会自动跟随 302 重定向。

<details>
<summary>nginx 反向代理示例</summary>

```nginx
server {
    listen 80;
    server_name tianyi.example.com;
    location / {
        proxy_pass http://127.0.0.1:8515;
        proxy_set_header Host $host;
        proxy_set_header X-Real-IP $remote_addr;
        proxy_set_header X-Forwarded-For $proxy_add_x_forwarded_for;
    }
}
```
</details>

## ❓ 常见问题

### Q: 扫码确认后网页一直没反应 / 提示获取登录信息失败？

A: 很可能是**服务器与天翼网盘（open.e.189.cn / cloud.189.cn）之间网络较慢或不稳定**。服务端在扫码成功后需要请求天翼的接口换取 cookies，若超时就会失败。建议：
- 在服务器上测试连通性：`curl -o /dev/null -w "%{time_total}s\n" "https://open.e.189.cn"`，若经常超过 5–10 秒说明连接较差。
- 可改用**账号密码登录**或**Cookie 登录**（在浏览器登录天翼云盘后复制 Cookie 到本服务），减少对扫码链路依赖。
- 将本服务部署到与国内网络连接更好的机器上再试。

### Q: 登录后提示获取目录列表失败？

A: 可能是 Cookies 已过期，请重新登录。

### Q: 找不到文件？

A: 检查请求路径是否与天翼网盘中的路径完全一致（包括大小写）。

### Q: 直链过期？

A: 天翼网盘直链有时效性，默认缓存 12 小时。如需调整，修改 `CACHE_EXPIRATION` 环境变量。

### Q: 如何清除缓存？

A: 
- 方式1: Web 界面点击"清除缓存"按钮
- 方式2: 调用 `POST /api/clear-cache` 接口
- 方式3: 重启容器

### Q: 如何修改 Web 管理密码？

A: 在 Web 管理界面的 **配置管理** 中修改 `ENV_WEB_PASSWORD`，保存后服务自动重启生效。

## 🛠 本地开发

```bash
# 安装依赖
pip install -r requirements.txt

# 准备配置（首次）
mkdir -p db
cp app/templete.env db/user.env
# 按需编辑 db/user.env

# 运行
cd app && python main.py
```

## 📄 许可证

MIT License

## 🔗 相关项目

- [p189client](https://github.com/ChenyangGao/p189client) - Python 天翼网盘客户端
