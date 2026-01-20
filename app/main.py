#!/usr/bin/env python3
# encoding: utf-8

"""
天翼网盘 302 直链服务

支持通过请求路径获取天翼网盘文件的下载直链，返回 302 重定向
"""

import os
import time
import secrets
import logging
import threading
import re
import html
from collections import deque
from pathlib import Path
from urllib.parse import unquote

from flask import Flask, request, jsonify, session, redirect, url_for, render_template
from dotenv import load_dotenv
import requests

# 加载环境变量
load_dotenv(dotenv_path="db/user.env", override=True)

from p189client import P189Client, check_response

LOG_FORMAT = '%(asctime)s - %(name)s - %(levelname)s - %(message)s'
logger = logging.getLogger(__name__)
logging.basicConfig(level=logging.INFO, format=LOG_FORMAT)

# Flask 应用
app = Flask(__name__, template_folder='templates', static_folder='static')
app.secret_key = secrets.token_hex(16)

# 配置文件路径
TEMPLATE_ENV_PATH = 'templete.env'
ENV_FILE_PATH = os.path.join('db', 'user.env')

# 确保 db 目录存在
os.makedirs('db', exist_ok=True)

# 从环境变量获取配置
def get_env(key, default=""):
    return os.getenv(key, default)

def get_int_env(key, default=0):
    try:
        value = os.getenv(key, str(default))
        return int(value) if value else default
    except (ValueError, TypeError):
        logger.warning(f"环境变量 {key} 值不是有效的整数，使用默认值 {default}")
        return default

# Web 管理界面认证
ENV_WEB_PASSPORT = get_env("ENV_WEB_PASSPORT", "admin")
ENV_WEB_PASSWORD = get_env("ENV_WEB_PASSWORD", "123456")

# 天翼网盘配置
ENV_189_USERNAME = get_env("ENV_189_USERNAME", "")
ENV_189_PASSWORD = get_env("ENV_189_PASSWORD", "")
ENV_189_COOKIES = get_env("ENV_189_COOKIES", "")
ENV_189_COOKIES_FILE = get_env("ENV_189_COOKIES_FILE", "db/cookies.txt")

# 缓存配置
MAX_CACHE_302LINK = get_int_env("MAX_CACHE_302LINK", 100)
CACHE_EXPIRATION = get_int_env("CACHE_EXPIRATION", 720) * 60  # 默认 720 分钟
PATH_CACHE_EXPIRATION = get_int_env("PATH_CACHE_EXPIRATION", 12) * 3600  # 默认 12 小时

# Telegram Bot 配置
TG_BOT_TOKEN = get_env("TG_BOT_TOKEN", "")
TG_BOT_NOTIFY_CHAT_IDS = get_env("TG_BOT_NOTIFY_CHAT_IDS", "")
TG_BOT_USER_WHITELIST = get_env("TG_BOT_USER_WHITELIST", "")
LOG_BUFFER_MAX = get_int_env("LOG_BUFFER_MAX", 1000)

# 全局客户端实例
client: P189Client | None = None
client_lock = threading.Lock()

# 文件 ID 缓存 (路径 -> (文件ID, 时间戳))
path_cache: dict[str, tuple[int, float]] = {}

# 下载链接缓存 (文件ID -> (下载链接, 时间戳))
url_cache: dict[int, tuple[str, float]] = {}

# 预缓存锁
precache_lock = threading.Lock()

# 日志缓冲区（用于 /189log）
log_buffer: deque[str] = deque(maxlen=LOG_BUFFER_MAX)


class LogBufferHandler(logging.Handler):
    def emit(self, record: logging.LogRecord) -> None:
        try:
            message = self.format(record)
        except Exception:
            message = record.getMessage()
        log_buffer.append(message)


_buffer_handler = LogBufferHandler()
_buffer_handler.setFormatter(logging.Formatter(LOG_FORMAT))
logging.getLogger().addHandler(_buffer_handler)


def _parse_whitelist(value: str) -> set[str]:
    entries = [v.strip() for v in value.split(",") if v.strip()]
    return set(entries)


TG_NOTIFY_CHAT_SET = _parse_whitelist(TG_BOT_NOTIFY_CHAT_IDS)
TG_USER_WHITELIST_SET = _parse_whitelist(TG_BOT_USER_WHITELIST)


def _is_user_allowed(user_id: str) -> bool:
    return bool(TG_USER_WHITELIST_SET) and user_id in TG_USER_WHITELIST_SET


def _tg_send_message(chat_id: str, text: str, parse_mode: str | None = None) -> None:
    if not TG_BOT_TOKEN:
        return
    try:
        payload = {
            "chat_id": chat_id,
            "text": text,
            "disable_web_page_preview": True,
        }
        if parse_mode:
            payload["parse_mode"] = parse_mode
        requests.post(
            f"https://api.telegram.org/bot{TG_BOT_TOKEN}/sendMessage",
            data=payload,
            timeout=10,
        )
    except Exception as e:
        logger.warning(f"发送 Telegram 消息失败: {e}")


def _notify_failure(file_path: str, error: str) -> None:
    if not TG_BOT_TOKEN or not TG_NOTIFY_CHAT_SET:
        return
    message = f"302 获取直链失败\n路径: {file_path}\n错误: {error}"
    for chat_id in TG_NOTIFY_CHAT_SET:
        _tg_send_message(chat_id, message)


def _split_message(text: str, limit: int = 3800) -> list[str]:
    if len(text) <= limit:
        return [text]
    parts = []
    current = []
    length = 0
    for line in text.splitlines():
        line_len = len(line) + 1
        if length + line_len > limit and current:
            parts.append("\n".join(current))
            current = [line]
            length = line_len
        else:
            current.append(line)
            length += line_len
    if current:
        parts.append("\n".join(current))
    return parts


def _sanitize_log_line(line: str) -> str:
    line = line.replace("\x1b", "")
    line = re.sub(r"\x1b\[[0-9;]*m", "", line)
    line = re.sub(r"\[[0-9;]*m", "", line)
    return line


def _send_log_to_chat(chat_id: str, lines: list[str]) -> None:
    cleaned = [_sanitize_log_line(line) for line in lines]
    payload = "\n".join(cleaned).strip()
    if not payload:
        _tg_send_message(chat_id, "暂无日志")
        return
    for part in _split_message(payload):
        safe_part = html.escape(part)
        _tg_send_message(chat_id, f"<pre>{safe_part}</pre>", parse_mode="HTML")


def _bot_polling_loop() -> None:
    if not TG_BOT_TOKEN:
        return
    if not TG_USER_WHITELIST_SET:
        logger.warning("Telegram 机器人未配置用户白名单，/189log 将拒绝所有用户")
    offset = 0
    while True:
        try:
            resp = requests.get(
                f"https://api.telegram.org/bot{TG_BOT_TOKEN}/getUpdates",
                params={"timeout": 30, "offset": offset},
                timeout=35,
            )
            data = resp.json()
            if not data.get("ok"):
                time.sleep(2)
                continue
            for update in data.get("result", []):
                offset = update.get("update_id", offset) + 1
                message = update.get("message") or update.get("edited_message") or {}
                text = message.get("text", "")
                if not text.startswith("/189log"):
                    continue
                user_id = str(message.get("from", {}).get("id", ""))
                chat_id = str(message.get("chat", {}).get("id", ""))
                if not _is_user_allowed(user_id):
                    _tg_send_message(chat_id, "未授权的用户")
                    continue
                lines = list(log_buffer)[-100:]
                _send_log_to_chat(chat_id, lines)
        except Exception as e:
            logger.warning(f"Telegram 轮询异常: {e}")
            time.sleep(2)


def get_client() -> P189Client:
    """获取客户端实例"""
    global client
    if client is None:
        raise Exception("未登录，请先登录天翼网盘账号")
    return client


def init_client():
    """初始化客户端"""
    global client
    
    with client_lock:
        try:
            if ENV_189_COOKIES:
                client = P189Client(cookies=ENV_189_COOKIES)
                logger.info("已使用环境变量 ENV_189_COOKIES 登录")
            elif ENV_189_COOKIES_FILE and Path(ENV_189_COOKIES_FILE).exists():
                client = P189Client(cookies=Path(ENV_189_COOKIES_FILE))
                logger.info(f"已使用 cookies 文件登录: {ENV_189_COOKIES_FILE}")
            elif ENV_189_USERNAME and ENV_189_PASSWORD:
                client = P189Client(ENV_189_USERNAME, ENV_189_PASSWORD)
                logger.info("已使用环境变量账号密码登录")
                # 保存 cookies
                save_cookies()
            else:
                logger.info("未配置登录信息，请通过 Web 界面登录")
        except Exception as e:
            logger.error(f"自动登录失败: {e}")
            client = None


def save_cookies():
    """保存 cookies 到文件"""
    global client
    if client and ENV_189_COOKIES_FILE:
        try:
            cookies_str = client.cookies_str
            Path(ENV_189_COOKIES_FILE).parent.mkdir(parents=True, exist_ok=True)
            with open(ENV_189_COOKIES_FILE, 'w', encoding='utf-8') as f:
                f.write(cookies_str)
            logger.info(f"Cookies 已保存到: {ENV_189_COOKIES_FILE}")
        except Exception as e:
            logger.error(f"保存 cookies 失败: {e}")


def resolve_path_to_file_id(file_path: str) -> int:
    """
    将文件路径解析为文件 ID
    
    :param file_path: 文件路径，如 /test/test.mkv
    :return: 文件 ID
    """
    global path_cache
    
    # 标准化路径
    if not file_path.startswith("/"):
        file_path = "/" + file_path
    
    current_time = time.time()
    
    # 检查缓存（带过期时间）
    if file_path in path_cache:
        file_id, cache_time = path_cache[file_path]
        if current_time - cache_time < PATH_CACHE_EXPIRATION:
            remaining = (PATH_CACHE_EXPIRATION - (current_time - cache_time)) / 3600
            logger.info(f"使用缓存的文件ID: {file_id}，剩余有效期: {remaining:.1f}小时")
            return file_id
        else:
            del path_cache[file_path]
    
    c = get_client()
    
    # 从根目录开始逐级查找
    parts = [p for p in file_path.split("/") if p]
    current_folder_id = -11  # 根目录 ID
    current_path = ""
    parent_folder_id = -11
    
    for i, part in enumerate(parts):
        current_path += "/" + part
        is_last = (i == len(parts) - 1)
        
        # 检查当前级别的缓存
        if current_path in path_cache:
            cached_id, cache_time = path_cache[current_path]
            if current_time - cache_time < PATH_CACHE_EXPIRATION:
                parent_folder_id = current_folder_id
                current_folder_id = cached_id
                continue
            else:
                del path_cache[current_path]
        
        # 搜索当前目录下的文件/文件夹
        found = False
        page_num = 1
        
        while not found:
            resp = c.fs_list_portal(
                {"fileId": current_folder_id, "pageNum": page_num, "pageSize": 100}
            )
            
            if resp.get("res_code") not in (0, "0", None) and resp.get("errorCode") not in (0, "0", None):
                raise Exception(f"获取目录列表失败: {resp}")
            
            # 注意：API 返回的字段是 data/fileName/fileId，不是 fileList/name/id
            file_list = resp.get("data", []) or resp.get("fileList", []) or []
            
            # 缓存当前目录下的所有文件（预缓存）
            for item in file_list:
                item_name = item.get("fileName") or item.get("name", "")
                item_id = item.get("fileId") or item.get("id")
                item_path = current_path.rsplit("/", 1)[0] + "/" + item_name if current_path.count("/") > 1 else "/" + item_name
                
                # 更新或添加缓存
                if item_path not in path_cache:
                    path_cache[item_path] = (item_id, current_time)
                
                if item_name == part:
                    parent_folder_id = current_folder_id
                    current_folder_id = item_id
                    path_cache[current_path] = (item_id, current_time)
                    found = True
            
            if found:
                break
            
            # 检查是否还有下一页
            record_count = resp.get("recordCount", 0)
            if page_num * 100 >= record_count:
                break
            page_num += 1
        
        if not found:
            raise Exception(f"文件或目录不存在: {current_path}")
    
    # 如果是文件，异步预缓存同目录其他文件的下载链接
    if parts:
        threading.Thread(
            target=precache_directory_urls, 
            args=(parent_folder_id, current_folder_id), 
            daemon=True
        ).start()
    
    return current_folder_id


def precache_directory_urls(parent_folder_id: int, current_file_id: int):
    """预缓存同目录下其他文件的下载链接"""
    if not precache_lock.acquire(blocking=False):
        return
    
    try:
        c = get_client()
        current_time = time.time()
        cached_count = 0
        
        page_num = 1
        while cached_count < MAX_CACHE_302LINK:
            resp = c.fs_list_portal(
                {"fileId": parent_folder_id, "pageNum": page_num, "pageSize": 100}
            )
            
            if resp.get("res_code") not in (0, "0", None):
                break
            
            # 注意：API 返回的字段是 data/fileName/fileId
            file_list = resp.get("data", []) or resp.get("fileList", []) or []
            
            for item in file_list:
                if cached_count >= MAX_CACHE_302LINK:
                    break
                
                item_id = item.get("fileId") or item.get("id")
                is_folder = item.get("isFolder", False)
                
                # 只缓存文件，不缓存文件夹，跳过当前文件
                if is_folder or item_id == current_file_id:
                    continue
                
                # 检查是否已缓存
                if item_id in url_cache:
                    _, cache_time = url_cache[item_id]
                    if current_time - cache_time < CACHE_EXPIRATION:
                        continue
                
                try:
                    download_url = c.download_url({"fileId": item_id}, True)
                    url_cache[item_id] = (download_url, current_time)
                    cached_count += 1
                    logger.debug(f"预缓存下载链接: {item.get('fileName') or item.get('name')}")
                except Exception as e:
                    logger.debug(f"预缓存失败: {e}")
            
            record_count = resp.get("recordCount", 0)
            if page_num * 100 >= record_count:
                break
            page_num += 1
        
        if cached_count > 0:
            logger.info(f"预缓存了 {cached_count} 个文件的下载链接")
    except Exception as e:
        logger.error(f"预缓存失败: {e}")
    finally:
        precache_lock.release()


def get_download_url(file_id: int) -> str:
    """
    获取文件下载直链
    
    :param file_id: 文件 ID
    :return: 下载链接
    """
    global url_cache
    
    current_time = time.time()
    
    # 检查缓存
    if file_id in url_cache:
        cached_url, cache_time = url_cache[file_id]
        if current_time - cache_time < CACHE_EXPIRATION:
            remaining = (CACHE_EXPIRATION - (current_time - cache_time)) / 60
            logger.info(f"使用缓存的下载链接，剩余有效期: {remaining:.1f}分钟")
            return cached_url
        else:
            del url_cache[file_id]
    
    c = get_client()
    
    # 优先使用视频下载链接（适合大文件和视频）
    try:
        resp = c.download_url_video({"fileId": file_id})
        if resp.get("res_code") == 0:
            normal = resp.get("normal", {})
            if normal.get("url"):
                download_url = normal["url"]
                url_cache[file_id] = (download_url, current_time)
                return download_url
    except Exception:
        pass
    
    # 备选：portal 方式
    try:
        resp = c.download_url_video_portal({"fileId": file_id})
        if resp.get("res_code") == 0:
            normal = resp.get("normal", {})
            if normal.get("url"):
                download_url = normal["url"]
                url_cache[file_id] = (download_url, current_time)
                return download_url
    except Exception:
        pass
    
    # 最后尝试普通下载链接（适合小文件）
    try:
        resp = c.download_url_info({"fileId": file_id})
        if resp.get("res_code") == 0 and "fileDownloadUrl" in resp:
            from html import unescape
            download_url = unescape(resp["fileDownloadUrl"])
            url_cache[file_id] = (download_url, current_time)
            return download_url
    except Exception:
        pass
    
    raise Exception(f"无法获取文件 {file_id} 的下载链接")


# ==================== Web 路由 ====================

@app.route('/')
def index():
    """首页/配置管理"""
    if not session.get('logged_in'):
        return redirect(url_for('login_page'))
    return render_template('index.html')


@app.route('/login')
def login_page():
    """登录页面"""
    if session.get('logged_in'):
        return redirect(url_for('index'))
    return render_template('login.html')


@app.route('/api/login', methods=['POST'])
def api_login():
    """登录 API"""
    data = request.json
    username = data.get('username')
    password = data.get('password')
    
    if username == ENV_WEB_PASSPORT and password == ENV_WEB_PASSWORD:
        session['logged_in'] = True
        return jsonify({'success': True})
    else:
        return jsonify({'success': False, 'error': '用户名或密码错误'})


@app.route('/api/logout', methods=['GET', 'POST'])
def api_logout():
    """登出 API"""
    session.pop('logged_in', None)
    if request.method == 'POST':
        return jsonify({'success': True})
    return redirect(url_for('login_page'))


@app.route('/api/status')
def api_status():
    """检查状态"""
    return jsonify({
        'logged_in': client is not None,
        'web_logged_in': session.get('logged_in', False),
        'path_cache_size': len(path_cache),
        'url_cache_size': len(url_cache)
    })


@app.route('/api/189/cookies')
def get_189_cookies():
    """获取天翼网盘 Cookies"""
    if not session.get('logged_in'):
        return jsonify({'error': '未登录'}), 401
    
    if client is None:
        return jsonify({'error': '天翼网盘未登录'}), 400
    
    try:
        cookies_dict = {}
        cookies_str = ""

        # 优先使用客户端自身的 cookies 字符串（更稳定）
        if hasattr(client, "cookies_str") and isinstance(client.cookies_str, str):
            cookies_str = client.cookies_str
            for pair in cookies_str.split(";"):
                if "=" in pair:
                    key, value = pair.strip().split("=", 1)
                    if key:
                        cookies_dict[key] = value
        else:
            # 兼容 requests cookiejar 或 dict/字符串等情况
            raw_cookies = getattr(client, "session", None)
            raw_cookies = getattr(raw_cookies, "cookies", raw_cookies)
            if isinstance(raw_cookies, dict):
                cookies_dict = dict(raw_cookies)
            else:
                try:
                    for cookie in raw_cookies:
                        if hasattr(cookie, "name") and hasattr(cookie, "value"):
                            cookies_dict[cookie.name] = cookie.value
                except TypeError:
                    pass

            if cookies_dict:
                cookies_str = '; '.join([f'{k}={v}' for k, v in cookies_dict.items()])
        
        return jsonify({
            'success': True,
            'cookies': cookies_str,
            'cookies_dict': cookies_dict
        })
    except Exception as e:
        logger.error(f"获取 Cookies 失败: {e}")
        return jsonify({'error': str(e)}), 500


@app.route('/api/env')
def get_env_config():
    """获取配置"""
    if not session.get('logged_in'):
        return jsonify({'error': '未登录'}), 401
    
    template_structure = {}
    template_order = []
    current_section = None
    current_comment = ''
    
    if not os.path.exists(TEMPLATE_ENV_PATH):
        return jsonify({'error': '配置模板文件不存在'}), 404
    
    with open(TEMPLATE_ENV_PATH, 'r', encoding='utf-8') as f:
        lines = f.readlines()
        for line in lines:
            line = line.strip()
            if not line:
                continue
            
            if line.startswith('# ') and not line.startswith('## '):
                current_section = line[2:]
                if current_section not in template_structure:
                    template_structure[current_section] = []
                    template_order.append(current_section)
                current_comment = ''
            elif line.startswith('#'):
                if line.startswith('## '):
                    current_comment = line[3:]
                else:
                    current_comment = line[2:]
            elif '=' in line and not line.startswith('#'):
                key, _ = line.split('=', 1)
                config_item = {
                    'key': key.strip(),
                    'value': '',
                    'comment': current_comment
                }
                if current_section:
                    template_structure[current_section].append(config_item)
                current_comment = ''
    
    # 读取实际配置值
    if os.path.exists(ENV_FILE_PATH):
        with open(ENV_FILE_PATH, 'r', encoding='utf-8') as f:
            env_values = {}
            for line in f.readlines():
                line = line.strip()
                if '=' in line and not line.startswith('#'):
                    key, value = line.split('=', 1)
                    env_values[key.strip()] = value.strip()
            
            for section in template_structure:
                for item in template_structure[section]:
                    if item['key'] in env_values:
                        item['value'] = env_values[item['key']]
    
    return jsonify({'sections': template_structure, 'order': template_order})


@app.route('/api/env', methods=['POST'])
def save_env_config():
    """保存配置"""
    if not session.get('logged_in'):
        return jsonify({'error': '未登录'}), 401
    
    data = request.json
    with open(ENV_FILE_PATH, 'w', encoding='utf-8') as f:
        for section, items in data.items():
            f.write(f'# {section}\n')
            for item in items:
                f.write(f'## {item["comment"]}\n')
                f.write(f'{item["key"]}={item["value"]}\n')
            f.write('\n')
    
    logger.info("配置已保存，程序将退出以触发容器重启...")
    time.sleep(1)
    os._exit(0)
    
    return jsonify({'success': True})


@app.route('/api/189/login', methods=['POST'])
def api_189_login():
    """天翼网盘登录"""
    global client
    
    data = request.json
    username = data.get('username')
    password = data.get('password')
    cookies = data.get('cookies')
    
    with client_lock:
        try:
            if cookies:
                client = P189Client(cookies=cookies)
            elif username and password:
                client = P189Client(username, password)
            else:
                return jsonify({'error': '请提供用户名密码或 cookies'}), 400
            
            # 保存 cookies
            save_cookies()
            
            # 清除缓存
            path_cache.clear()
            url_cache.clear()
            
            return jsonify({'success': True, 'message': '登录成功'})
        except Exception as e:
            client = None
            return jsonify({'error': f'登录失败: {str(e)}'}), 401


@app.route('/api/189/logout', methods=['POST'])
def api_189_logout():
    """天翼网盘登出"""
    global client
    
    with client_lock:
        client = None
        path_cache.clear()
        url_cache.clear()
    
    return jsonify({'success': True, 'message': '已登出'})


@app.route('/api/189/qrcode')
def api_189_qrcode():
    """获取天翼网盘扫码登录二维码
    
    严格按照 P189Client.login_with_qrcode 的流程实现：
    1. login_qrcode_uuid() - 获取 encryuuid 和 uuid
    2. login_url() - 获取 lt, reqId, url (作为 referer)
    3. login_app_conf() - 获取 data (paramId, returnUrl)
    """
    try:
        import json
        from urllib.parse import quote
        
        app_id = "cloud"  # P189Client 默认使用 "cloud"
        
        # 1. 获取二维码 UUID
        resp = P189Client.login_qrcode_uuid(app_id)
        # 处理响应可能是字符串的情况（API 返回 text/html 但内容是 JSON）
        if isinstance(resp, str):
            resp = json.loads(resp)
        check_response(resp)
        encryuuid = resp["encryuuid"]
        uuid = resp["uuid"]
        
        logger.info(f"获取到二维码 UUID: {uuid[:50]}...")
        
        # 2. 获取登录 URL 配置 (lt, reqId, url)
        conf = P189Client.login_url()
        if isinstance(conf, str):
            conf = json.loads(conf)
        
        logger.info(f"获取到登录配置: lt={conf.get('lt', '')[:20]}...")
        
        # 3. 获取 app 配置 (需要 lt 和 reqId 作为请求头)
        app_conf = P189Client.login_app_conf(
            app_id,
            headers={
                "lt": conf["lt"],
                "reqId": conf["reqId"]
            }
        )
        if isinstance(app_conf, str):
            app_conf = json.loads(app_conf)
        data = app_conf.get("data", {})
        
        logger.info(f"获取到 app 配置: paramId={data.get('paramId', '')[:20]}...")
        
        # 保存到 session，用于后续轮询
        session['qr_session'] = {
            'app_id': app_id,
            'encryuuid': encryuuid,
            'uuid': uuid,
            'lt': conf["lt"],
            'reqId': conf["reqId"],
            'url': conf["url"],  # 用作 referer
            'paramId': data.get("paramId", ""),
            'returnUrl': data.get("returnUrl", ""),
        }
        
        # uuid 就是二维码内容（是一个 URL）
        # 前端可以直接用这个 URL 生成二维码
        # 也可以使用官方二维码图片 URL
        qr_image_url = f"https://open.e.189.cn/api/logbox/oauth2/image.do?uuid={quote(uuid, safe='')}"
        
        return jsonify({
            'success': True,
            'qrCodeUrl': uuid,  # 二维码内容（是一个 URL）
            'qrImageUrl': qr_image_url,  # 官方二维码图片 URL
            'uuid': uuid
        })
    except Exception as e:
        import traceback
        logger.error(f"获取二维码失败: {e}\n{traceback.format_exc()}")
        return jsonify({'error': f'获取二维码失败: {str(e)}'}), 500


@app.route('/api/189/qrcode/status')
def api_189_qrcode_status():
    """检查扫码状态
    
    严格按照 P189Client.login_with_qrcode 的流程实现
    """
    global client
    
    qr_session = session.get('qr_session')
    if not qr_session:
        return jsonify({'error': '请先获取二维码'})
    
    try:
        import json
        from urllib.parse import quote
        import requests
        
        app_id = qr_session['app_id']
        encryuuid = qr_session['encryuuid']
        uuid = qr_session['uuid']
        lt = qr_session['lt']
        reqId = qr_session['reqId']
        url = qr_session['url']
        paramId = qr_session['paramId']
        returnUrl = qr_session['returnUrl']
        
        # 检查扫码状态 - 严格按照 P189Client.login_with_qrcode 的参数
        resp = P189Client.login_qrcode_state(
            {
                "appId": app_id,
                "encryuuid": encryuuid,
                "uuid": uuid,
                "returnUrl": quote(returnUrl, safe="") if returnUrl else "",
                "paramId": paramId,
            },
            headers={
                "lt": lt,
                "reqid": reqId,
                "referer": url
            }
        )
        
        # 处理响应可能是字符串的情况
        if isinstance(resp, str):
            resp = json.loads(resp)
        
        # 获取状态码 (status 或 result)
        status_code = resp.get("status")
        if status_code is None:
            status_code = resp.get("result")
        
        logger.debug(f"扫码状态响应: status_code={status_code}")
        
        if status_code == -106:
            return jsonify({'status': '等待扫码...'})
        elif status_code == -11002:
            return jsonify({'status': '已扫码，请在手机上确认'})
        elif status_code == 0:
            # 登录成功 - 请求 redirectUrl 获取 cookies
            redirect_url = resp.get("redirectUrl", "")
            cookies_from_state = resp.get("cookies", {})
            
            logger.info(f"扫码成功，redirectUrl: {redirect_url[:50] if redirect_url else 'None'}...")
            
            with client_lock:
                try:
                    # 请求 redirectUrl 获取更多 cookies (不跟随重定向)
                    all_cookies = dict(cookies_from_state) if cookies_from_state else {}
                    
                    if redirect_url:
                        try:
                            redirect_resp = requests.get(redirect_url, allow_redirects=False, timeout=10)
                            for cookie in redirect_resp.cookies:
                                all_cookies[cookie.name] = cookie.value
                            logger.info(f"从 redirectUrl 获取到 cookies: {list(all_cookies.keys())}")
                        except Exception as e:
                            logger.warning(f"请求 redirectUrl 失败: {e}")
                    
                    if all_cookies:
                        cookies_str = "; ".join(f"{k}={v}" for k, v in all_cookies.items())
                        client = P189Client(cookies=cookies_str)
                        logger.info(f"使用 cookies 登录成功")
                    else:
                        raise Exception("无法获取登录 cookies")
                    
                    save_cookies()
                    path_cache.clear()
                    url_cache.clear()
                    session.pop('qr_session', None)
                    return jsonify({'success': True, 'message': '登录成功'})
                except Exception as e:
                    import traceback
                    logger.error(f"初始化客户端失败: {e}\n{traceback.format_exc()}")
                    return jsonify({'error': f'初始化客户端失败: {str(e)}'})
        elif status_code == -20099:
            session.pop('qr_session', None)
            return jsonify({'error': '二维码已过期，请重新获取'})
        else:
            return jsonify({'error': f'扫码异常: {resp}'})
    except Exception as e:
        import traceback
        logger.error(f"检查扫码状态失败: {e}\n{traceback.format_exc()}")
        return jsonify({'error': str(e)})


@app.route('/api/clear-cache', methods=['POST'])
def api_clear_cache():
    """清除缓存"""
    path_cache.clear()
    url_cache.clear()
    return jsonify({'success': True, 'message': '缓存已清除'})


# ==================== 302 直链路由 ====================

@app.route('/d/<path:file_path>')
def handle_download(file_path):
    """302 重定向到下载链接（/d/ 前缀）"""
    full_path = ""
    try:
        # URL 解码
        query_part = request.query_string.decode('utf-8')
        if query_part:
            full_path = f"{file_path}?{query_part}"
        else:
            full_path = file_path
        
        decoded_path = unquote(full_path)
        full_path = f"/{decoded_path}"
        
        # 解析路径获取文件 ID
        file_id = resolve_path_to_file_id(full_path)
        
        # 获取下载链接
        download_url = get_download_url(file_id)
        
        logger.info(f"302 重定向: {full_path} -> {download_url[:80]}...")
        
        return redirect(download_url, code=302)
    except Exception as e:
        logger.error(f"下载处理异常: {str(e)}")
        _notify_failure(full_path, str(e))
        return jsonify({'error': str(e)}), 500


@app.route('/<path:file_path>')
def handle_root_download(file_path):
    """302 重定向到下载链接（根路径，排除特定路由）"""
    # 排除静态文件和 API 路由
    excluded_prefixes = ('api/', 'static/', 'login', 'favicon.ico')
    if file_path.startswith(excluded_prefixes) or file_path in ('', 'login'):
        return jsonify({'error': '路径不存在'}), 404
    
    full_path = ""
    try:
        # URL 解码
        query_part = request.query_string.decode('utf-8')
        if query_part:
            full_path = f"{file_path}?{query_part}"
        else:
            full_path = file_path
        
        decoded_path = unquote(full_path)
        full_path = f"/{decoded_path}"
        
        # 解析路径获取文件 ID
        file_id = resolve_path_to_file_id(full_path)
        
        # 获取下载链接
        download_url = get_download_url(file_id)
        
        logger.info(f"302 重定向: {full_path} -> {download_url[:80]}...")
        
        return redirect(download_url, code=302)
    except Exception as e:
        logger.error(f"下载处理异常: {str(e)}")
        _notify_failure(full_path, str(e))
        return jsonify({'error': str(e)}), 500


# ==================== 启动 ====================

if __name__ == "__main__":
    # 初始化客户端
    init_client()

    if TG_BOT_TOKEN:
        threading.Thread(target=_bot_polling_loop, daemon=True).start()
    
    port = get_int_env("PORT", 8515)
    host = get_env("HOST", "0.0.0.0")
    debug = get_env("DEBUG", "false").lower() == "true"
    
    logger.info(f"服务启动: http://{host}:{port}")
    
    app.run(host=host, port=port, debug=debug, threaded=True)
