# 天翼网盘 302 直链服务

一个基于 Docker 的天翼网盘 302 直链服务，用于 Emby/Jellyfin 等媒体服务器的 302 播放。

## ✨ 功能特性

- ✅ **302 直链重定向**: 请求 `/path/to/file.mkv` 自动获取天翼网盘对应路径文件的下载直链并 302 重定向
- ✅ **多种登录方式**: 支持扫码登录、账号密码登录、Cookies 登录
- ✅ **智能路径缓存**: 自动缓存文件路径到 ID 的映射，支持过期时间配置
- ✅ **下载链接缓存**: 缓存下载直链，减少 API 调用
- ✅ **预缓存机制**: 自动预缓存同目录其他文件的直链，提高连续播放速度
- ✅ **Docker 部署**: 一键 Docker 部署，方便管理
- ✅ **持久化登录**: 支持 Cookies 文件持久化，重启容器后无需重新登录
- ✅ **精美 Web 界面**: 提供配置管理、登录管理的 Web 界面
- ✅ **多架构支持**: 支持 amd64 和 arm64 架构
- ✅ **多账号支持**: 支持多个天翼网盘账号，通过路径前缀区分（如 `/账号key/路径/文件.mkv`）
- ✅ **账号健康检查**: 定时检测账号状态，过期自动通知
- ✅ **自动重新登录**: 支持密码自动重登录、扫码二维码发送到 Telegram 重登录

## 🚀 快速开始

### 方式1: 使用预构建镜像（推荐）

```yaml
# docker-compose.yml
services:
  tianyi-302:
    # 使用 GitHub Container Registry 镜像（支持 amd64/arm64）
    image: ghcr.io/268326/p189_dav:latest
    # 或者本地构建：取消下面build注释，注释上面的 image
    # build: .
    container_name: tianyi-302
    restart: always
    ports:
      - "8515:8515"
    volumes:
      # 持久化数据目录（cookies、配置等）
      - ./db:/app/db
    environment:
      # ========== 时区设置 ==========
      - TZ=Asia/Shanghai
      # 应用配置统一在 ./db/user.env
    healthcheck:
      test: ["CMD", "curl", "-f", "http://localhost:8515/api/status"]
      interval: 30s
      timeout: 10s
      retries: 3
```

```bash
# 启动服务
docker compose up -d
```

### 方式2: 从源码构建

```bash
# 克隆仓库
git clone https://github.com/268326/p189_dav.git
cd p189_dav

# 创建数据目录
mkdir -p db

# 启动服务
docker compose up -d
```

### 2. 访问管理界面

打开浏览器访问 `http://your-ip:8515`

默认登录账号：
- 用户名: `admin`
- 密码: `123456`

### 3. 登录天翼网盘

登录管理界面后，在【天翼网盘登录】标签页选择登录方式：

#### 方式1: 扫码登录（推荐）
点击"获取二维码"，使用天翼网盘 APP 扫描

#### 方式2: 账号密码登录
输入手机号/邮箱和密码（需要先关闭设备锁）

> 关闭设备锁: 网页端登录后访问 https://e.dlife.cn/user/index.do

#### 方式3: Cookies 登录
直接粘贴从浏览器获取的 Cookies 字符串

### 4. 使用 302 直链

登录成功后，访问任意路径即可获取 302 重定向：

```
# 使用默认账号（不写前缀）
http://your-ip:8515/电影/test.mkv
http://your-ip:8515/d/电影/test.mkv

# 多账号时，路径第一段为账号 key
http://your-ip:8515/default/电影/test.mkv
http://your-ip:8515/work/文档/yyy.pdf
  ↓ 302 重定向
https://xxx.cloud.189.cn/download/...
```

## 📝 配置说明

### 配置来源

- 配置统一读取 `db/user.env`（Web 界面保存后会写入该文件）
- `docker-compose.yml` 的环境变量不再作为应用配置来源

### 环境变量

| 变量名 | 说明 | 默认值 |
|--------|------|--------|
| `PORT` | 服务端口 | `8515` |
| `HOST` | 监听地址 | `0.0.0.0` |
| `ENV_WEB_PASSPORT` | Web 管理用户名 | `admin` |
| `ENV_WEB_PASSWORD` | Web 管理密码 | `123456` |
| `ENV_189_USERNAME` | 天翼账号（手机号/邮箱） | - |
| `ENV_189_PASSWORD` | 天翼密码 | - |
| `ENV_189_COOKIES` | Cookies 字符串 | - |
| `ENV_189_COOKIES_FILE` | Cookies 文件路径 | `/app/db/cookies.txt` |
| `CACHE_EXPIRATION` | 下载链接缓存时间（分钟） | `720` |
| `PATH_CACHE_EXPIRATION` | 路径缓存时间（小时） | `12` |
| `MAX_CACHE_302LINK` | 预缓存最大数量 | `100` |
| `TG_BOT_TOKEN` | Telegram Bot Token（留空关闭） | - |
| `TG_BOT_NOTIFY_CHAT_IDS` | 接收通知的 Chat ID（逗号分隔） | - |
| `TG_BOT_USER_WHITELIST` | 允许使用 /189log 的用户ID白名单（逗号分隔） | - |
| `LOG_BUFFER_MAX` | 日志缓冲最大行数 | `1000` |
| `ACCOUNT_CHECK_INTERVAL` | 账号健康检查间隔（分钟），0 禁用 | `30` |

### 网络代理（可选）

在 **配置管理** 中设置 `PROXY_URL`（或 `HTTP_PROXY`/`HTTPS_PROXY`）后，本服务所有出站 HTTP/HTTPS 请求（天翼网盘 API、扫码登录回调、Telegram、直链重定向等）均会经过该代理。用于服务器直连天翼网盘较慢时，通过代理改善连通性。

- 示例：`http://127.0.0.1:7890`、`http://user:pass@proxy:8080`、`socks5://127.0.0.1:1080`
- 留空则不使用代理
- 修改后需保存配置并等待服务重启后生效

### 登录优先级

1. `ENV_189_COOKIES` 环境变量
2. `ENV_189_COOKIES_FILE` 文件
3. `ENV_189_USERNAME` + `ENV_189_PASSWORD`
4. Web 界面手动登录

### 数据持久化

`docker-compose.yml` 默认将 `./db` 目录挂载到容器的 `/app/db`，用于保存：
- Cookies 文件（默认账号：`db/cookies.txt`；其他账号：`db/accounts/<账号key>/cookies.txt`）
- 用户配置文件 `user.env`
- 多账号配置 `accounts.json`（账号列表及默认账号）

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

- 当 302 获取直链失败时，机器人会通知 `TG_BOT_NOTIFY_CHAT_IDS` 中的 chat
- 账号过期/异常时自动发送 TG 通知
- 扫码自动重登录模式下，二维码图片会直接发送到 TG
- 支持命令：
  - `/189log` — 获取容器内最近 100 行日志（仅 `TG_BOT_USER_WHITELIST` 用户）
  - `/189health` — 手动触发账号健康检查并返回结果

## 🔄 账号健康检查与自动重登录

后台定时（默认 30 分钟）检查所有已登录账号的会话是否有效。当检测到账号过期时：

1. 发送 TG 通知告知账号异常
2. 根据每个账号配置的自动登录方式尝试重新登录：
   - **密码模式**: 自动使用保存的用户名密码重新登录，成功/失败均发 TG 通知
   - **扫码模式**: 自动生成二维码图片发送到 TG，等待用户扫码完成登录（5 分钟超时，下次检查时重试）
   - **关闭**: 仅通知，不自动重登录

在 Web 管理界面的多账号列表中，每个账号可独立配置自动登录方式。

## 📺 与 Emby/Jellyfin 配合使用

### Emby 302 播放配置

1. 部署本服务
2. 在 Emby 中添加 strm 文件，内容为本服务的 URL：
   ```
   # 默认账号
   http://your-ip:8515/电影/你的电影.mkv
   # 多账号时指定账号
   http://your-ip:8515/账号key/电影/你的电影.mkv
   ```
3. Emby 会自动跟随 302 重定向播放

### nginx 反向代理（可选）

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

A: 修改 `docker-compose.yml` 中的 `ENV_WEB_PASSWORD` 环境变量，然后重启容器。

## 🛠 本地开发

```bash
# 安装依赖
pip install -r requirements.txt

# 进入 app 目录
cd app

# 运行服务
python main.py
```

## 📄 许可证

MIT License

## 🔗 相关项目

- [p189client](https://github.com/ChenyangGao/p189client) - Python 天翼网盘客户端
