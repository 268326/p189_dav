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
      - ENV_WEB_PASSPORT=admin
      - ENV_WEB_PASSWORD=123456
      - ENV_189_COOKIES_FILE=/app/db/cookies.txt
      # 缓存配置（设为 0 关闭缓存）
      - CACHE_EXPIRATION=0
      - PATH_CACHE_EXPIRATION=0
      - MAX_CACHE_302LINK=0
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
http://your-ip:8515/电影/test.mkv
  ↓ 302 重定向
https://xxx.cloud.189.cn/download/...

# 或者使用 /d/ 前缀
http://your-ip:8515/d/电影/test.mkv
```

## 📝 配置说明

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

### 登录优先级

1. `ENV_189_COOKIES` 环境变量
2. `ENV_189_COOKIES_FILE` 文件
3. `ENV_189_USERNAME` + `ENV_189_PASSWORD`
4. Web 界面手动登录

### 数据持久化

`docker-compose.yml` 默认将 `./db` 目录挂载到容器的 `/app/db`，用于保存：
- Cookies 文件
- 用户配置文件

## 📚 API 接口

| 接口 | 方法 | 说明 |
|------|------|------|
| `/` | GET | 首页/管理界面 |
| `/login` | GET | 登录页面 |
| `/api/login` | POST | Web 管理登录 |
| `/api/logout` | GET/POST | Web 管理登出 |
| `/api/status` | GET | 检查状态 |
| `/api/env` | GET | 获取配置 |
| `/api/env` | POST | 保存配置 |
| `/api/189/login` | POST | 天翼网盘登录 |
| `/api/189/logout` | POST | 天翼网盘登出 |
| `/api/189/qrcode` | GET | 获取扫码登录二维码 |
| `/api/189/qrcode/status` | GET | 检查扫码状态 |
| `/api/clear-cache` | POST | 清除缓存 |
| `/d/{path}` | GET | 302 重定向到直链（推荐） |
| `/{path}` | GET | 302 重定向到直链 |

## 🤖 Telegram 通知

- 当 302 获取直链失败时，机器人会通知 `TG_BOT_NOTIFY_CHAT_IDS` 中的 chat
- 支持命令 `/189log` 获取容器内最近 100 行日志（仅 `TG_BOT_USER_WHITELIST` 用户）

## 📺 与 Emby/Jellyfin 配合使用

### Emby 302 播放配置

1. 部署本服务
2. 在 Emby 中添加 strm 文件，内容为本服务的 URL：
   ```
   http://your-ip:8515/电影/你的电影.mkv
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
