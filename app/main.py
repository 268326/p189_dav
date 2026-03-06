#!/usr/bin/env python3
# encoding: utf-8

"""
天翼网盘 302 直链服务

支持通过请求路径获取天翼网盘文件的下载直链，返回 302 重定向
"""

import os
import re
import json
import time
import secrets
import logging
import threading
import html
from collections import deque
from pathlib import Path
from urllib.parse import unquote, parse_qsl, urlsplit

from flask import Flask, request, jsonify, session, redirect, url_for, render_template
from dotenv import dotenv_values
import requests
import httpx

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
ACCOUNTS_FILE = os.path.join('db', 'accounts.json')

# 账号 key 仅允许字母、数字、下划线（用于 URL 路径）
ACCOUNT_KEY_PATTERN = re.compile(r'^[a-zA-Z0-9_]+$')

# 确保 db 目录存在
os.makedirs('db', exist_ok=True)

def _load_env_file(path: str) -> dict[str, str]:
    if not os.path.exists(path):
        return {}
    values = dotenv_values(path)
    return {key: value for key, value in values.items() if value is not None}


ENV_FILE_VALUES = _load_env_file(ENV_FILE_PATH)


# 从 db/user.env 获取配置（不读取 compose 环境变量）
def get_env(key, default=""):
    value = ENV_FILE_VALUES.get(key)
    if value is None or value == "":
        return default
    return value


def get_int_env(key, default=0):
    try:
        value = get_env(key, str(default))
        return int(value) if value != "" else default
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

# 账号定时健康检查间隔（分钟），0 表示禁用
ACCOUNT_CHECK_INTERVAL = get_int_env("ACCOUNT_CHECK_INTERVAL", 30)

# 全局 HTTP 代理（天翼网盘、Telegram 等出站请求均走代理）
PROXY_URL = (get_env("PROXY_URL") or get_env("HTTP_PROXY") or get_env("HTTPS_PROXY") or "").strip()
if PROXY_URL:
    os.environ["HTTP_PROXY"] = PROXY_URL
    os.environ["HTTPS_PROXY"] = PROXY_URL
    logger.info(f"已启用全局代理: {PROXY_URL}")


def get_proxies():
    """供 requests 使用的代理字典，未配置时返回 None"""
    if not PROXY_URL:
        return None
    return {"http": PROXY_URL, "https": PROXY_URL}


# 全局客户端实例：account_key -> P189Client
clients: dict[str, P189Client | None] = {}
clients_lock = threading.RLock()

# 账号配置：default_key, accounts list
def _load_accounts_config() -> dict:
    """加载 db/accounts.json，不存在则返回默认单账号结构"""
    if not os.path.exists(ACCOUNTS_FILE):
        return {"default_key": "default", "accounts": [{"key": "default", "label": "默认"}]}
    try:
        with open(ACCOUNTS_FILE, 'r', encoding='utf-8') as f:
            data = json.load(f)
        if "accounts" not in data or not data["accounts"]:
            data["accounts"] = [{"key": "default", "label": "默认"}]
        if "default_key" not in data or not data["default_key"]:
            data["default_key"] = data["accounts"][0]["key"]
        return data
    except Exception as e:
        logger.warning(f"读取账号配置失败: {e}，使用默认")
        return {"default_key": "default", "accounts": [{"key": "default", "label": "默认"}]}


def _save_accounts_config(data: dict) -> None:
    Path(ACCOUNTS_FILE).parent.mkdir(parents=True, exist_ok=True)
    with open(ACCOUNTS_FILE, 'w', encoding='utf-8') as f:
        json.dump(data, f, ensure_ascii=False, indent=2)


def get_accounts_list() -> list[dict]:
    """返回 [{"key","label"}, ...]"""
    cfg = _load_accounts_config()
    return list(cfg.get("accounts", []))


def get_default_account_key() -> str:
    cfg = _load_accounts_config()
    return cfg.get("default_key") or "default"


def get_account_keys_set() -> set[str]:
    return {a["key"] for a in get_accounts_list()}


def get_cookies_path_for_account(account_key: str) -> str:
    """默认账号用 db/cookies.txt，其余用 db/accounts/<key>/cookies.txt"""
    if account_key == "default":
        return os.path.join("db", "cookies.txt")
    return os.path.join("db", "accounts", account_key, "cookies.txt")


# 文件 ID 缓存：account_key -> (路径 -> (文件ID, 时间戳))
path_cache: dict[str, dict[str, tuple[int, float]]] = {}

# 下载链接缓存：account_key -> (文件ID -> (下载链接, 时间戳))
url_cache: dict[str, dict[int, tuple[str, float]]] = {}

def _path_cache(account_key: str) -> dict:
    if account_key not in path_cache:
        path_cache[account_key] = {}
    return path_cache[account_key]

def _url_cache(account_key: str) -> dict:
    if account_key not in url_cache:
        url_cache[account_key] = {}
    return url_cache[account_key]

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
            proxies=get_proxies(),
        )
    except Exception as e:
        logger.warning(f"发送 Telegram 消息失败: {e}")


def _tg_send_photo(chat_id: str, photo_url: str, caption: str = "") -> None:
    """通过 URL 发送图片到 Telegram"""
    if not TG_BOT_TOKEN:
        return
    try:
        payload: dict = {"chat_id": chat_id, "photo": photo_url}
        if caption:
            payload["caption"] = caption
        requests.post(
            f"https://api.telegram.org/bot{TG_BOT_TOKEN}/sendPhoto",
            data=payload,
            timeout=15,
            proxies=get_proxies(),
        )
    except Exception as e:
        logger.warning(f"发送 Telegram 图片失败: {e}")


def _notify_failure(file_path: str, error: str) -> None:
    if not TG_BOT_TOKEN or not TG_NOTIFY_CHAT_SET:
        return
    message = f"302 获取直链失败\n路径: {file_path}\n错误: {error}"
    for chat_id in TG_NOTIFY_CHAT_SET:
        _tg_send_message(chat_id, message)


# ---- 账号健康检查 ----
# 每个 key 上次检查结果：True=正常, False=异常, None=未检查
_account_health: dict[str, bool | None] = {}
_account_health_ts: dict[str, float] = {}       # 上次检查时间戳
_account_health_err: dict[str, str] = {}         # 上次异常信息
_health_check_lock = threading.Lock()


def _check_accounts_health() -> dict[str, dict]:
    """逐个检查已登录账号的可用性，状态变化时发 TG 通知。
    返回 {account_key: {"ok": bool, "error": str|None, "ts": float}}"""
    results: dict[str, dict] = {}
    accounts = get_accounts_list()
    now = time.time()

    for a in accounts:
        key = a["key"]
        label = a.get("label") or key
        with clients_lock:
            c = clients.get(key)
        if c is None:
            results[key] = {"ok": False, "error": "未登录", "ts": now}
            continue

        ok = False
        err_msg: str | None = None
        try:
            resp = c.user_info_brief()
            res_code = resp.get("res_code")
            error_code = resp.get("errorCode")
            if res_code in (0, "0", None) or error_code in (0, "0", None):
                ok = True
            else:
                err_msg = resp.get("res_message") or resp.get("errorMsg") or f"res_code={res_code}"
        except Exception as e:
            err_msg = str(e)

        results[key] = {"ok": ok, "error": err_msg, "ts": now}

        with _health_check_lock:
            prev = _account_health.get(key)
            _account_health[key] = ok
            _account_health_ts[key] = now
            if err_msg:
                _account_health_err[key] = err_msg
            elif key in _account_health_err:
                del _account_health_err[key]

        # 状态从正常变为异常时发送 TG 通知
        if prev is not False and not ok and err_msg and err_msg != "未登录":
            _notify_account_expired(key, label, err_msg)
            logger.warning(f"账号健康检查: [{key}] 异常 — {err_msg}")
        elif ok:
            if prev is False:
                logger.info(f"账号健康检查: [{key}] 已恢复正常")
            else:
                logger.info(f"账号健康检查: [{key}] 正常")

    return results


def _notify_account_expired(account_key: str, label: str, error: str) -> None:
    if not TG_BOT_TOKEN or not TG_NOTIFY_CHAT_SET:
        return
    message = (
        f"⚠️ 天翼网盘账号异常\n"
        f"账号: {label} [{account_key}]\n"
        f"错误: {error}\n"
        f"请及时重新登录"
    )
    for chat_id in TG_NOTIFY_CHAT_SET:
        _tg_send_message(chat_id, message)


def _account_check_loop() -> None:
    """后台定时检查账号健康状态"""
    if ACCOUNT_CHECK_INTERVAL <= 0:
        return
    interval = ACCOUNT_CHECK_INTERVAL * 60
    time.sleep(30)
    while True:
        try:
            results = _check_accounts_health()
            _auto_relogin_if_needed(results)
        except Exception as e:
            logger.error(f"账号健康检查异常: {e}")
        time.sleep(interval)


# ---- 自动重新登录 ----
# 正在进行扫码登录的账号，防止重复触发
_qr_relogin_active: dict[str, bool] = {}


def _get_account_auto_login(account_key: str) -> dict:
    """获取账号的自动登录配置"""
    for a in get_accounts_list():
        if a["key"] == account_key:
            return {
                "method": a.get("auto_login", "none"),
                "username": a.get("username", ""),
                "password": a.get("password", ""),
            }
    return {"method": "none", "username": "", "password": ""}


def _auto_relogin_if_needed(results: dict[str, dict]) -> None:
    """根据健康检查结果，对异常账号触发自动重新登录"""
    for key, r in results.items():
        if r["ok"]:
            continue
        if r.get("error") == "未登录":
            continue
        cfg = _get_account_auto_login(key)
        method = cfg["method"]
        if method == "none":
            continue

        label = ""
        for a in get_accounts_list():
            if a["key"] == key:
                label = a.get("label") or key
                break

        if method == "password":
            _auto_relogin_password(key, label, cfg["username"], cfg["password"])
        elif method == "qrcode":
            if _qr_relogin_active.get(key):
                logger.info(f"账号 [{key}] 扫码重登录正在进行中，跳过")
                continue
            threading.Thread(
                target=_auto_relogin_qrcode,
                args=(key, label),
                daemon=True,
            ).start()


def _auto_relogin_password(key: str, label: str, username: str, password: str) -> None:
    """使用账号密码自动重新登录"""
    if not username or not password:
        logger.warning(f"账号 [{key}] 配置了密码自动登录但未设置用户名/密码")
        return
    logger.info(f"账号 [{key}] 正在使用密码自动重新登录...")
    try:
        with clients_lock:
            clients[key] = P189Client(username, password)
            save_cookies(key)
            _path_cache(key).clear()
            _url_cache(key).clear()
        logger.info(f"账号 [{key}] 密码自动重新登录成功")
        for chat_id in TG_NOTIFY_CHAT_SET:
            _tg_send_message(chat_id, f"✅ 账号 [{label}] 已通过密码自动重新登录成功")
    except Exception as e:
        logger.error(f"账号 [{key}] 密码自动重新登录失败: {e}")
        for chat_id in TG_NOTIFY_CHAT_SET:
            _tg_send_message(chat_id, f"❌ 账号 [{label}] 密码自动重新登录失败: {e}")


def _auto_relogin_qrcode(key: str, label: str) -> None:
    """通过 TG Bot 发送二维码让用户扫码重新登录"""
    import json as _json
    from urllib.parse import quote

    if not TG_BOT_TOKEN or not TG_NOTIFY_CHAT_SET:
        logger.warning(f"账号 [{key}] 配置了扫码自动登录但未配置 TG Bot")
        return

    _qr_relogin_active[key] = True
    try:
        logger.info(f"账号 [{key}] 正在生成扫码登录二维码...")

        app_id = "cloud"
        resp = P189Client.login_qrcode_uuid(app_id)
        if isinstance(resp, str):
            resp = _json.loads(resp)
        check_response(resp)
        encryuuid = resp["encryuuid"]
        uuid = resp["uuid"]

        conf = _fresh_login_url_params()

        app_conf = P189Client.login_app_conf(
            app_id,
            headers={"lt": conf["lt"], "reqId": conf["reqId"]},
        )
        if isinstance(app_conf, str):
            app_conf = _json.loads(app_conf)
        data = app_conf.get("data", {})

        qr_session = {
            "app_id": app_id,
            "encryuuid": encryuuid,
            "uuid": uuid,
            "lt": conf["lt"],
            "reqId": conf["reqId"],
            "url": conf["url"],
            "paramId": data.get("paramId", ""),
            "returnUrl": data.get("returnUrl", ""),
        }

        qr_image_url = f"https://open.e.189.cn/api/logbox/oauth2/image.do?uuid={quote(uuid, safe='')}"

        for chat_id in TG_NOTIFY_CHAT_SET:
            _tg_send_photo(
                chat_id,
                qr_image_url,
                caption=f"🔑 账号 [{label}] 已过期，请扫码重新登录\n（二维码 5 分钟内有效）",
            )

        logger.info(f"账号 [{key}] 二维码已发送到 TG，等待扫码...")

        # 轮询扫码状态，最多等 5 分钟
        deadline = time.time() + 300
        while time.time() < deadline:
            time.sleep(3)
            try:
                state = P189Client.login_qrcode_state(
                    {
                        "appId": app_id,
                        "encryuuid": encryuuid,
                        "uuid": uuid,
                        "returnUrl": quote(qr_session["returnUrl"], safe="") if qr_session["returnUrl"] else "",
                        "paramId": qr_session["paramId"],
                    },
                    headers={"lt": qr_session["lt"], "reqid": qr_session["reqId"], "referer": qr_session["url"]},
                )
                if isinstance(state, str):
                    state = _json.loads(state)

                status_code = state.get("status")
                if status_code is None:
                    status_code = state.get("result")

                if status_code == -106:
                    continue  # 等待扫码
                elif status_code == -11002:
                    logger.info(f"账号 [{key}] 已扫码，等待确认...")
                    continue
                elif status_code == 0:
                    redirect_url = state.get("redirectUrl", "")
                    cookies_from_state = state.get("cookies", {})
                    all_cookies = dict(cookies_from_state) if cookies_from_state else {}

                    if redirect_url:
                        for attempt in range(2):
                            try:
                                redirect_resp = requests.get(
                                    redirect_url,
                                    allow_redirects=False,
                                    timeout=25,
                                    headers={
                                        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36",
                                        "Referer": qr_session["url"],
                                    },
                                    proxies=get_proxies(),
                                )
                                for cookie in redirect_resp.cookies:
                                    all_cookies[cookie.name] = cookie.value
                                break
                            except Exception as e:
                                logger.warning(f"账号 [{key}] 请求 redirectUrl 失败（尝试 {attempt + 1}/2）: {e}")

                    if not all_cookies:
                        logger.error(f"账号 [{key}] 扫码成功但获取 cookies 失败")
                        for chat_id in TG_NOTIFY_CHAT_SET:
                            _tg_send_message(chat_id, f"❌ 账号 [{label}] 扫码成功但获取 cookies 失败，请手动登录")
                        return

                    with clients_lock:
                        cookies_str = "; ".join(f"{k}={v}" for k, v in all_cookies.items())
                        clients[key] = P189Client(cookies=cookies_str)
                        save_cookies(key)
                        _path_cache(key).clear()
                        _url_cache(key).clear()

                    logger.info(f"账号 [{key}] 扫码自动重新登录成功")
                    for chat_id in TG_NOTIFY_CHAT_SET:
                        _tg_send_message(chat_id, f"✅ 账号 [{label}] 扫码重新登录成功")
                    return
                elif status_code == -20099:
                    logger.warning(f"账号 [{key}] 二维码已过期")
                    for chat_id in TG_NOTIFY_CHAT_SET:
                        _tg_send_message(chat_id, f"⏰ 账号 [{label}] 二维码已过期，将在下次检查时重新生成")
                    return
                else:
                    logger.warning(f"账号 [{key}] 扫码状态异常: {state}")
                    return
            except Exception as e:
                logger.warning(f"账号 [{key}] 检查扫码状态失败: {e}")

        logger.warning(f"账号 [{key}] 扫码登录超时（5分钟），将在下次检查时重试")
        for chat_id in TG_NOTIFY_CHAT_SET:
            _tg_send_message(chat_id, f"⏰ 账号 [{label}] 扫码登录超时，将在下次检查时重试")

    except Exception as e:
        logger.error(f"账号 [{key}] 扫码自动重新登录异常: {e}")
        for chat_id in TG_NOTIFY_CHAT_SET:
            _tg_send_message(chat_id, f"❌ 账号 [{label}] 扫码自动重新登录异常: {e}")
    finally:
        _qr_relogin_active.pop(key, None)


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
                proxies=get_proxies(),
            )
            data = resp.json()
            if not data.get("ok"):
                time.sleep(2)
                continue
            for update in data.get("result", []):
                offset = update.get("update_id", offset) + 1
                message = update.get("message") or update.get("edited_message") or {}
                text = message.get("text", "")
                user_id = str(message.get("from", {}).get("id", ""))
                chat_id = str(message.get("chat", {}).get("id", ""))
                if text.startswith("/189log"):
                    if not _is_user_allowed(user_id):
                        _tg_send_message(chat_id, "未授权的用户")
                        continue
                    lines = list(log_buffer)[-100:]
                    _send_log_to_chat(chat_id, lines)
                elif text.startswith("/189health"):
                    if not _is_user_allowed(user_id):
                        _tg_send_message(chat_id, "未授权的用户")
                        continue
                    try:
                        results = _check_accounts_health()
                        lines = []
                        for k, r in results.items():
                            icon = "✅" if r["ok"] else "❌"
                            lines.append(f'{icon} {k}: {"正常" if r["ok"] else r.get("error", "异常")}')
                        _tg_send_message(chat_id, "账号健康检查结果:\n" + "\n".join(lines))
                    except Exception as e:
                        _tg_send_message(chat_id, f"检查失败: {e}")
        except Exception as e:
            logger.warning(f"Telegram 轮询异常: {e}")
            time.sleep(2)


def get_client(account_key: str | None = None) -> P189Client:
    """获取指定账号的客户端实例，account_key 为空时使用默认账号"""
    key = account_key or get_default_account_key()
    with clients_lock:
        c = clients.get(key)
    if c is None:
        raise Exception(f"账号 [{key}] 未登录，请先在管理界面登录天翼网盘")
    return c


def init_clients():
    """初始化所有账号的客户端（从 cookies 文件或环境变量）"""
    global clients
    accounts = get_accounts_list()
    default_key = get_default_account_key()
    # 确保每个账号在 clients 里有槽位
    with clients_lock:
        for a in accounts:
            key = a["key"]
            if key not in clients:
                clients[key] = None
        for key in list(clients.keys()):
            if key not in get_account_keys_set():
                del clients[key]
    # 逐个尝试登录
    for a in accounts:
        key = a["key"]
        cookies_path = get_cookies_path_for_account(key)
        with clients_lock:
            try:
                if key == "default" and ENV_189_COOKIES:
                    clients[key] = P189Client(cookies=ENV_189_COOKIES)
                    logger.info("默认账号已使用环境变量 ENV_189_COOKIES 登录")
                elif key == "default" and ENV_189_USERNAME and ENV_189_PASSWORD:
                    clients[key] = P189Client(ENV_189_USERNAME, ENV_189_PASSWORD)
                    logger.info("默认账号已使用环境变量账号密码登录")
                    save_cookies("default")
                elif Path(cookies_path).exists():
                    clients[key] = P189Client(cookies=Path(cookies_path))
                    logger.info(f"账号 [{key}] 已使用 cookies 文件登录: {cookies_path}")
                else:
                    logger.info(f"账号 [{key}] 未配置登录信息，请通过 Web 界面登录")
            except Exception as e:
                logger.error(f"账号 [{key}] 自动登录失败: {e}")
                clients[key] = None


def save_cookies(account_key: str) -> None:
    """将指定账号的客户端 cookies 写入文件"""
    with clients_lock:
        c = clients.get(account_key)
    if not c:
        return
    cookies_file = get_cookies_path_for_account(account_key)
    try:
        cookies_str = c.cookies_str
        Path(cookies_file).parent.mkdir(parents=True, exist_ok=True)
        with open(cookies_file, 'w', encoding='utf-8') as f:
            f.write(cookies_str)
        logger.info(f"账号 [{account_key}] Cookies 已保存: {cookies_file}")
    except Exception as e:
        logger.error(f"保存账号 [{account_key}] cookies 失败: {e}")


def resolve_path_to_file_id(file_path: str, account_key: str) -> int:
    """
    将文件路径解析为文件 ID

    :param file_path: 文件路径，如 /test/test.mkv
    :param account_key: 账号标识
    :return: 文件 ID
    """
    pc = _path_cache(account_key)

    # 标准化路径
    if not file_path.startswith("/"):
        file_path = "/" + file_path

    current_time = time.time()

    # 检查缓存（带过期时间）
    if file_path in pc:
        file_id, cache_time = pc[file_path]
        if current_time - cache_time < PATH_CACHE_EXPIRATION:
            remaining = (PATH_CACHE_EXPIRATION - (current_time - cache_time)) / 3600
            logger.info(f"使用缓存的文件ID: {file_id}，剩余有效期: {remaining:.1f}小时")
            return file_id
        else:
            del pc[file_path]

    c = get_client(account_key)

    # 从根目录开始逐级查找
    parts = [p for p in file_path.split("/") if p]
    current_folder_id = -11  # 根目录 ID
    current_path = ""
    parent_folder_id = -11

    for i, part in enumerate(parts):
        current_path += "/" + part
        is_last = (i == len(parts) - 1)

        # 检查当前级别的缓存
        if current_path in pc:
            cached_id, cache_time = pc[current_path]
            if current_time - cache_time < PATH_CACHE_EXPIRATION:
                parent_folder_id = current_folder_id
                current_folder_id = cached_id
                continue
            else:
                del pc[current_path]

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
                if item_path not in pc:
                    pc[item_path] = (item_id, current_time)

                if item_name == part:
                    parent_folder_id = current_folder_id
                    current_folder_id = item_id
                    pc[current_path] = (item_id, current_time)
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
            args=(account_key, parent_folder_id, current_folder_id),
            daemon=True
        ).start()

    return current_folder_id


def precache_directory_urls(account_key: str, parent_folder_id: int, current_file_id: int):
    """预缓存同目录下其他文件的下载链接"""
    if not precache_lock.acquire(blocking=False):
        return

    try:
        c = get_client(account_key)
        uc = _url_cache(account_key)
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
                if item_id in uc:
                    _, cache_time = uc[item_id]
                    if current_time - cache_time < CACHE_EXPIRATION:
                        continue

                try:
                    download_url = c.download_url({"fileId": item_id}, True)
                    uc[item_id] = (download_url, current_time)
                    cached_count += 1
                    logger.debug(f"预缓存下载链接: {item.get('fileName') or item.get('name')}")
                except Exception as e:
                    logger.debug(f"预缓存失败: {e}")

            record_count = resp.get("recordCount", 0)
            if page_num * 100 >= record_count:
                break
            page_num += 1

        if cached_count > 0:
            logger.info(f"账号 [{account_key}] 预缓存了 {cached_count} 个文件的下载链接")
    except Exception as e:
        logger.error(f"预缓存失败: {e}")
    finally:
        precache_lock.release()


def get_download_url(file_id: int, account_key: str) -> str:
    """
    获取文件下载直链

    :param file_id: 文件 ID
    :param account_key: 账号标识
    :return: 下载链接
    """
    uc = _url_cache(account_key)

    current_time = time.time()

    # 检查缓存
    if file_id in uc:
        cached_url, cache_time = uc[file_id]
        if current_time - cache_time < CACHE_EXPIRATION:
            remaining = (CACHE_EXPIRATION - (current_time - cache_time)) / 60
            logger.info(f"使用缓存的下载链接，剩余有效期: {remaining:.1f}分钟")
            return cached_url
        else:
            del uc[file_id]

    c = get_client(account_key)

    # 优先使用视频下载链接（适合大文件和视频）
    try:
        resp = c.download_url_video({"fileId": file_id})
        if resp.get("res_code") == 0:
            normal = resp.get("normal", {})
            if normal.get("url"):
                download_url = normal["url"]
                uc[file_id] = (download_url, current_time)
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
                uc[file_id] = (download_url, current_time)
                return download_url
    except Exception:
        pass

    # 最后尝试普通下载链接（适合小文件）
    try:
        resp = c.download_url_info({"fileId": file_id})
        if resp.get("res_code") == 0 and "fileDownloadUrl" in resp:
            from html import unescape
            download_url = unescape(resp["fileDownloadUrl"])
            uc[file_id] = (download_url, current_time)
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
    """检查状态（含多账号列表）"""
    accounts = get_accounts_list()
    default_key = get_default_account_key()
    with clients_lock:
        account_list = [
            {
                "key": a["key"],
                "label": a.get("label", a["key"]),
                "is_default": a["key"] == default_key,
                "logged_in": clients.get(a["key"]) is not None,
            }
            for a in accounts
        ]
        any_logged_in = any(clients.get(a["key"]) for a in accounts)
    path_total = sum(len(_path_cache(k)) for k in list(path_cache))
    url_total = sum(len(_url_cache(k)) for k in list(url_cache))
    return jsonify({
        'logged_in': any_logged_in,
        'web_logged_in': session.get('logged_in', False),
        'accounts': account_list,
        'default_key': default_key,
        'path_cache_size': path_total,
        'url_cache_size': url_total
    })


@app.route('/api/health')
def api_health():
    """获取账号健康检查状态"""
    with _health_check_lock:
        data = {}
        for key in _account_health:
            data[key] = {
                "ok": _account_health.get(key),
                "error": _account_health_err.get(key),
                "ts": _account_health_ts.get(key, 0),
            }
    return jsonify({
        "interval_minutes": ACCOUNT_CHECK_INTERVAL,
        "accounts": data,
    })


@app.route('/api/health/check', methods=['POST'])
def api_health_check_now():
    """手动触发一次账号健康检查"""
    if not session.get('logged_in'):
        return jsonify({'error': '未登录'}), 401
    results = _check_accounts_health()
    return jsonify({"success": True, "results": results})


@app.route('/api/189/cookies')
def get_189_cookies():
    """获取天翼网盘 Cookies（可选 account_key 查询参数）"""
    if not session.get('logged_in'):
        return jsonify({'error': '未登录'}), 401

    account_key = request.args.get('account_key') or get_default_account_key()
    with clients_lock:
        c = clients.get(account_key)
    if c is None:
        return jsonify({'error': f'账号 [{account_key}] 未登录'}), 400

    try:
        cookies_dict = {}
        cookies_str = ""

        # 优先使用客户端自身的 cookies 字符串（更稳定）
        if hasattr(c, "cookies_str") and isinstance(c.cookies_str, str):
            cookies_str = c.cookies_str
            for pair in cookies_str.split(";"):
                if "=" in pair:
                    key, value = pair.strip().split("=", 1)
                    if key:
                        cookies_dict[key] = value
        else:
            # 兼容 requests cookiejar 或 dict/字符串等情况
            raw_cookies = getattr(c, "session", None)
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
            'account_key': account_key,
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
    """天翼网盘登录（body 中可传 account_key，默认为 default）"""
    global clients

    data = request.json or {}
    account_key = (data.get('account_key') or "").strip() or "default"
    username = data.get('username')
    password = data.get('password')
    cookies = data.get('cookies')

    if account_key not in get_account_keys_set():
        return jsonify({'error': f'账号 [{account_key}] 不存在，请先添加账号'}), 400

    with clients_lock:
        try:
            if cookies:
                clients[account_key] = P189Client(cookies=cookies)
            elif username and password:
                clients[account_key] = P189Client(username, password)
            else:
                return jsonify({'error': '请提供用户名密码或 cookies'}), 400

            save_cookies(account_key)
            _path_cache(account_key).clear()
            _url_cache(account_key).clear()

            return jsonify({'success': True, 'message': '登录成功', 'account_key': account_key})
        except Exception as e:
            clients[account_key] = None
            return jsonify({'error': f'登录失败: {str(e)}'}), 401


@app.route('/api/189/logout', methods=['POST'])
def api_189_logout():
    """天翼网盘登出（body 中可传 account_key，不传则登出所有）"""
    global clients

    data = request.json or {}
    account_key = data.get('account_key')

    with clients_lock:
        if account_key:
            if account_key in clients:
                clients[account_key] = None
                _path_cache(account_key).clear()
                _url_cache(account_key).clear()
            return jsonify({'success': True, 'message': f'已登出账号 [{account_key}]'})
        else:
            for key in list(clients.keys()):
                clients[key] = None
            for key in list(path_cache.keys()):
                path_cache[key].clear()
            for key in list(url_cache.keys()):
                url_cache[key].clear()
            return jsonify({'success': True, 'message': '已登出所有账号'})


def _fresh_login_url_params() -> dict:
    """用无 cookies 的干净请求获取 lt / reqId / url 登录参数。
    解决多账号场景下共享 httpx 客户端携带旧 cookies 导致
    login_url 被直接重定向到已登录页面、拿不到 lt/reqId 的问题。
    """
    proxy_map = {}
    if PROXY_URL:
        proxy_map = {"http://": PROXY_URL, "https://": PROXY_URL}

    with httpx.Client(follow_redirects=True, proxies=proxy_map, timeout=15) as client:
        resp = client.get(
            "https://cloud.189.cn/api/portal/loginUrl.action",
            params={
                "redirectURL": "https://cloud.189.cn/web/redirect.html",
                "defaultSaveName": 3,
                "defaultSaveNameCheck": "uncheck",
            },
        )
        # 优先从最终 URL 提取
        url = str(resp.url)
        data = dict(parse_qsl(urlsplit(url).query))
        data["url"] = url
        if "lt" in data and "reqId" in data:
            return data

        # 最终 URL 无 lt/reqId，检查中间重定向的 Location
        for hist in resp.history:
            loc = hist.headers.get("location", "")
            if loc:
                loc_data = dict(parse_qsl(urlsplit(loc).query))
                if "lt" in loc_data and "reqId" in loc_data:
                    loc_data["url"] = loc
                    return loc_data

        raise Exception(f"无法获取登录参数 (lt/reqId)，最终 URL: {url}")


@app.route('/api/189/qrcode')
def api_189_qrcode():
    """获取天翼网盘扫码登录二维码（可选 account_key 查询参数）"""
    account_key = request.args.get('account_key') or 'default'
    if account_key not in get_account_keys_set():
        return jsonify({'error': f'账号 [{account_key}] 不存在'}), 400

    session_key = 'qr_session' if account_key == 'default' else f'qr_session_{account_key}'
    try:
        import json as _json
        from urllib.parse import quote

        app_id = "cloud"
        resp = P189Client.login_qrcode_uuid(app_id)
        if isinstance(resp, str):
            resp = _json.loads(resp)
        check_response(resp)
        encryuuid = resp["encryuuid"]
        uuid = resp["uuid"]

        logger.info(f"获取到二维码 UUID: {uuid[:50]}...")

        conf = _fresh_login_url_params()

        app_conf = P189Client.login_app_conf(
            app_id,
            headers={"lt": conf["lt"], "reqId": conf["reqId"]}
        )
        if isinstance(app_conf, str):
            app_conf = _json.loads(app_conf)
        data = app_conf.get("data", {})

        session[session_key] = {
            'app_id': app_id,
            'encryuuid': encryuuid,
            'uuid': uuid,
            'lt': conf["lt"],
            'reqId': conf["reqId"],
            'url': conf["url"],
            'paramId': data.get("paramId", ""),
            'returnUrl': data.get("returnUrl", ""),
            'account_key': account_key,
        }

        qr_image_url = f"https://open.e.189.cn/api/logbox/oauth2/image.do?uuid={quote(uuid, safe='')}"

        return jsonify({
            'success': True,
            'account_key': account_key,
            'qrCodeUrl': uuid,
            'qrImageUrl': qr_image_url,
            'uuid': uuid
        })
    except Exception as e:
        import traceback
        logger.error(f"获取二维码失败: {e}\n{traceback.format_exc()}")
        return jsonify({'error': f'获取二维码失败: {str(e)}'}), 500


@app.route('/api/189/qrcode/status')
def api_189_qrcode_status():
    """检查扫码状态（可选 account_key 查询参数）"""
    global clients

    account_key = request.args.get('account_key') or 'default'
    session_key = 'qr_session' if account_key == 'default' else f'qr_session_{account_key}'
    qr_session = session.get(session_key)
    if not qr_session:
        return jsonify({'error': '请先获取二维码'})

    try:
        import json as _json
        from urllib.parse import quote

        app_id = qr_session['app_id']
        encryuuid = qr_session['encryuuid']
        uuid = qr_session['uuid']
        lt = qr_session['lt']
        reqId = qr_session['reqId']
        url = qr_session['url']
        paramId = qr_session['paramId']
        returnUrl = qr_session['returnUrl']

        resp = P189Client.login_qrcode_state(
            {
                "appId": app_id,
                "encryuuid": encryuuid,
                "uuid": uuid,
                "returnUrl": quote(returnUrl, safe="") if returnUrl else "",
                "paramId": paramId,
            },
            headers={"lt": lt, "reqid": reqId, "referer": url}
        )

        if isinstance(resp, str):
            resp = _json.loads(resp)

        status_code = resp.get("status")
        if status_code is None:
            status_code = resp.get("result")

        logger.debug(f"扫码状态响应: status_code={status_code}")

        if status_code == -106:
            return jsonify({'status': '等待扫码...'})
        elif status_code == -11002:
            return jsonify({'status': '已扫码，请在手机上确认'})
        elif status_code == 0:
            redirect_url = resp.get("redirectUrl", "")
            cookies_from_state = resp.get("cookies", {})
            all_cookies = dict(cookies_from_state) if cookies_from_state else {}

            # 先请求 redirectUrl 获取 cookies（不持锁，避免阻塞其他请求）
            # 服务器与天翼网盘连接差时容易超时，适当延长超时并重试一次
            if redirect_url:
                last_err = None
                for attempt in range(2):
                    try:
                        redirect_resp = requests.get(
                            redirect_url,
                            allow_redirects=False,
                            timeout=25,
                            headers={"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36", "Referer": url},
                            proxies=get_proxies(),
                        )
                        for cookie in redirect_resp.cookies:
                            all_cookies[cookie.name] = cookie.value
                        last_err = None
                        break
                    except requests.exceptions.Timeout as e:
                        last_err = "连接天翼网盘超时"
                        logger.warning(f"请求 redirectUrl 超时（尝试 {attempt + 1}/2）: {e}")
                    except Exception as e:
                        last_err = str(e)
                        logger.warning(f"请求 redirectUrl 失败（尝试 {attempt + 1}/2）: {e}")
                if last_err and not all_cookies:
                    return jsonify({
                        'error': '获取登录信息失败（可能是服务器与天翼网盘连接较慢或超时），请重试或使用账号密码/Cookie 登录'
                    })

            if not all_cookies:
                return jsonify({'error': '无法获取登录 cookies，请重试或使用账号密码/Cookie 登录'})

            with clients_lock:
                try:
                    cookies_str = "; ".join(f"{k}={v}" for k, v in all_cookies.items())
                    clients[account_key] = P189Client(cookies=cookies_str)
                    logger.info(f"账号 [{account_key}] 扫码登录成功")
                    save_cookies(account_key)
                    _path_cache(account_key).clear()
                    _url_cache(account_key).clear()
                    session.pop(session_key, None)
                    return jsonify({'success': True, 'message': '登录成功', 'account_key': account_key})
                except Exception as e:
                    import traceback
                    logger.error(f"初始化客户端失败: {e}\n{traceback.format_exc()}")
                    if account_key in clients:
                        clients[account_key] = None
                    return jsonify({'error': f'登录失败: {str(e)}'})
        elif status_code == -20099:
            session.pop(session_key, None)
            return jsonify({'error': '二维码已过期，请重新获取'})
        else:
            return jsonify({'error': f'扫码异常: {resp}'})
    except Exception as e:
        import traceback
        logger.error(f"检查扫码状态失败: {e}\n{traceback.format_exc()}")
        return jsonify({'error': str(e)})


@app.route('/api/clear-cache', methods=['POST'])
def api_clear_cache():
    """清除缓存（body 可传 account_key，不传则清除所有账号缓存）"""
    data = request.json or {}
    account_key = data.get('account_key')
    if account_key:
        if account_key in path_cache:
            path_cache[account_key].clear()
        if account_key in url_cache:
            url_cache[account_key].clear()
        return jsonify({'success': True, 'message': f'已清除账号 [{account_key}] 的缓存'})
    for key in list(path_cache.keys()):
        path_cache[key].clear()
    for key in list(url_cache.keys()):
        url_cache[key].clear()
    return jsonify({'success': True, 'message': '缓存已清除'})


def _format_ts(ts: float) -> str:
    return time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(ts))


def _cache_meta(cache_time: float, ttl_seconds: int) -> dict:
    if ttl_seconds <= 0:
        return {
            "created_at": _format_ts(cache_time),
            "expires_at": "-",
            "ttl_seconds": ttl_seconds,
            "remaining_seconds": -1,
        }
    expires_at = cache_time + ttl_seconds
    remaining = max(0, int(expires_at - time.time()))
    return {
        "created_at": _format_ts(cache_time),
        "expires_at": _format_ts(expires_at),
        "ttl_seconds": ttl_seconds,
        "remaining_seconds": remaining,
    }


@app.route('/api/cache')
def api_cache_list():
    """获取缓存详情（可选 account_key 查询参数筛选）"""
    if not session.get('logged_in'):
        return jsonify({'error': '未登录'}), 401

    filter_key = request.args.get('account_key')
    account_keys = [filter_key] if filter_key and filter_key in get_account_keys_set() else list(path_cache.keys()) or get_account_keys_set()
    if not account_keys:
        account_keys = get_account_keys_set()

    path_items = []
    url_items = []
    file_id_to_path: dict[tuple[str, int], tuple[str, float]] = {}

    for ak in account_keys:
        pc = _path_cache(ak)
        uc = _url_cache(ak)
        for path, (file_id, cache_time) in pc.items():
            key = (ak, file_id)
            if key not in file_id_to_path or cache_time > file_id_to_path[key][1]:
                file_id_to_path[key] = (path, cache_time)
        for path, (file_id, cache_time) in pc.items():
            meta = _cache_meta(cache_time, PATH_CACHE_EXPIRATION)
            path_items.append({
                "account_key": ak,
                "path": path,
                "file_id": str(file_id),
                **meta
            })
        for file_id, (url, cache_time) in uc.items():
            path, _ = file_id_to_path.get((ak, file_id), ("", 0))
            file_name = Path(path).name if path else ""
            meta = _cache_meta(cache_time, CACHE_EXPIRATION)
            url_items.append({
                "account_key": ak,
                "file_id": str(file_id),
                "file_path": path,
                "file_name": file_name,
                "url": url,
                **meta
            })

    path_items.sort(key=lambda x: (x.get("account_key", ""), x.get("created_at", "")), reverse=True)
    url_items.sort(key=lambda x: (x.get("account_key", ""), x.get("created_at", "")), reverse=True)

    return jsonify({
        "path_cache": path_items,
        "url_cache": url_items
    })


@app.route('/api/cache/path', methods=['POST'])
def api_cache_delete_path():
    """删除单个路径缓存（body 需 path，可选 account_key）"""
    if not session.get('logged_in'):
        return jsonify({'error': '未登录'}), 401
    data = request.json or {}
    path = data.get('path')
    account_key = data.get('account_key') or get_default_account_key()
    if not path:
        return jsonify({'error': '缺少 path'}), 400
    pc = _path_cache(account_key)
    if path in pc:
        del pc[path]
    return jsonify({'success': True})


@app.route('/api/cache/url', methods=['POST'])
def api_cache_delete_url():
    """删除单个链接缓存（body 需 file_id，可选 account_key）"""
    if not session.get('logged_in'):
        return jsonify({'error': '未登录'}), 401
    data = request.json or {}
    file_id = data.get('file_id')
    account_key = data.get('account_key') or get_default_account_key()
    try:
        file_id = int(file_id)
    except (TypeError, ValueError):
        return jsonify({'error': 'file_id 无效'}), 400
    uc = _url_cache(account_key)
    if file_id in uc:
        del uc[file_id]
    return jsonify({'success': True})


# ==================== 账号管理 API ====================

@app.route('/api/accounts')
def api_accounts_list():
    """获取账号列表"""
    if not session.get('logged_in'):
        return jsonify({'error': '未登录'}), 401
    accounts = get_accounts_list()
    default_key = get_default_account_key()
    with clients_lock:
        out = [
            {
                "key": a["key"],
                "label": a.get("label", a["key"]),
                "is_default": a["key"] == default_key,
                "logged_in": clients.get(a["key"]) is not None,
                "auto_login": a.get("auto_login", "none"),
            }
            for a in accounts
        ]
    return jsonify({"accounts": out, "default_key": default_key})


@app.route('/api/accounts', methods=['POST'])
def api_accounts_add():
    """添加账号，body: { "key": "xxx", "label": "显示名" }"""
    if not session.get('logged_in'):
        return jsonify({'error': '未登录'}), 401
    data = request.json or {}
    key = (data.get("key") or "").strip()
    label = (data.get("label") or key or "").strip()
    if not key:
        return jsonify({"error": "账号 key 不能为空"}), 400
    if not ACCOUNT_KEY_PATTERN.match(key):
        return jsonify({"error": "账号 key 仅允许字母、数字、下划线"}), 400
    if key in get_account_keys_set():
        return jsonify({"error": f"账号 [{key}] 已存在"}), 400
    cfg = _load_accounts_config()
    cfg["accounts"].append({"key": key, "label": label or key})
    _save_accounts_config(cfg)
    with clients_lock:
        clients[key] = None
    return jsonify({"success": True, "account_key": key})


@app.route('/api/accounts/set-default', methods=['POST'])
def api_accounts_set_default():
    """设置默认账号，body: { "account_key": "xxx" }"""
    if not session.get('logged_in'):
        return jsonify({'error': '未登录'}), 401
    data = request.json or {}
    key = (data.get("account_key") or "").strip()
    if key not in get_account_keys_set():
        return jsonify({"error": f"账号 [{key}] 不存在"}), 400
    cfg = _load_accounts_config()
    cfg["default_key"] = key
    _save_accounts_config(cfg)
    return jsonify({"success": True, "default_key": key})


@app.route('/api/accounts/<account_key>', methods=['DELETE'])
def api_accounts_remove(account_key):
    """删除账号（不能删除最后一个）"""
    if not session.get('logged_in'):
        return jsonify({'error': '未登录'}), 401
    if account_key not in get_account_keys_set():
        return jsonify({"error": "账号不存在"}), 404
    cfg = _load_accounts_config()
    if len(cfg["accounts"]) <= 1:
        return jsonify({"error": "至少保留一个账号"}), 400
    cfg["accounts"] = [a for a in cfg["accounts"] if a["key"] != account_key]
    if cfg.get("default_key") == account_key:
        cfg["default_key"] = cfg["accounts"][0]["key"]
    _save_accounts_config(cfg)
    with clients_lock:
        if account_key in clients:
            del clients[account_key]
    if account_key in path_cache:
        del path_cache[account_key]
    if account_key in url_cache:
        del url_cache[account_key]
    return jsonify({"success": True})


@app.route('/api/accounts/auto-login', methods=['POST'])
def api_accounts_auto_login():
    """设置账号自动登录方式，body: { account_key, method, username?, password? }"""
    if not session.get('logged_in'):
        return jsonify({'error': '未登录'}), 401
    data = request.json or {}
    key = (data.get("account_key") or "").strip()
    method = (data.get("method") or "none").strip()
    if key not in get_account_keys_set():
        return jsonify({"error": f"账号 [{key}] 不存在"}), 400
    if method not in ("password", "qrcode", "none"):
        return jsonify({"error": "method 必须为 password / qrcode / none"}), 400
    if method == "password":
        username = (data.get("username") or "").strip()
        password = (data.get("password") or "").strip()
        if not username or not password:
            return jsonify({"error": "密码模式需要提供 username 和 password"}), 400

    cfg = _load_accounts_config()
    for a in cfg["accounts"]:
        if a["key"] == key:
            a["auto_login"] = method
            if method == "password":
                a["username"] = data.get("username", "").strip()
                a["password"] = data.get("password", "").strip()
            else:
                a.pop("username", None)
                a.pop("password", None)
            break
    _save_accounts_config(cfg)
    return jsonify({"success": True, "account_key": key, "method": method})


@app.route('/api/accounts/auto-login/<account_key>')
def api_accounts_auto_login_get(account_key):
    """获取账号自动登录配置"""
    if not session.get('logged_in'):
        return jsonify({'error': '未登录'}), 401
    cfg = _get_account_auto_login(account_key)
    has_password = bool(cfg["username"] and cfg["password"])
    return jsonify({
        "account_key": account_key,
        "method": cfg["method"],
        "username": cfg["username"] if has_password else "",
        "has_password": has_password,
    })


# ==================== 302 直链路由 ====================

def _parse_account_and_path(raw_path: str):
    """
    从路径中解析账号与网盘路径。
    若首段是已配置的账号 key，则返回 (account_key, /rest/path)；否则返回 (default_key, /full_path)。
    """
    parts = [p for p in raw_path.split("/") if p]
    keys = get_account_keys_set()
    default_key = get_default_account_key()
    if not parts:
        return default_key, "/"
    first = parts[0]
    if first in keys and len(parts) > 1:
        # 首段是账号 key，路径为剩余部分
        rest = "/" + "/".join(parts[1:])
        return first, rest
    # 无账号前缀，使用默认账号，完整路径
    return default_key, "/" + raw_path


@app.route('/d/<path:file_path>')
def handle_download(file_path):
    """302 重定向到下载链接（/d/ 前缀，支持 /d/账号key/路径）"""
    full_path = ""
    try:
        query_part = request.query_string.decode('utf-8')
        if query_part:
            full_path = f"{file_path}?{query_part}"
        else:
            full_path = file_path

        decoded_path = unquote(full_path)
        if "?" in decoded_path:
            decoded_path = decoded_path.split("?")[0]
        account_key, path_for_api = _parse_account_and_path(decoded_path)

        file_id = resolve_path_to_file_id(path_for_api, account_key)
        download_url = get_download_url(file_id, account_key)

        logger.info(f"302 重定向: [{account_key}] {path_for_api} -> {download_url[:80]}...")

        return redirect(download_url, code=302)
    except Exception as e:
        logger.error(f"下载处理异常: {str(e)}")
        _notify_failure(full_path, str(e))
        return jsonify({'error': str(e)}), 500


@app.route('/<path:file_path>')
def handle_root_download(file_path):
    """302 重定向到下载链接（根路径，支持 /账号key/路径，排除 api/static/login）"""
    excluded_prefixes = ('api/', 'static/', 'login', 'favicon.ico')
    if file_path.startswith(excluded_prefixes) or file_path in ('', 'login'):
        return jsonify({'error': '路径不存在'}), 404

    full_path = ""
    try:
        query_part = request.query_string.decode('utf-8')
        if query_part:
            full_path = f"{file_path}?{query_part}"
        else:
            full_path = file_path

        decoded_path = unquote(full_path)
        if "?" in decoded_path:
            decoded_path = decoded_path.split("?")[0]
        account_key, path_for_api = _parse_account_and_path(decoded_path)

        file_id = resolve_path_to_file_id(path_for_api, account_key)
        download_url = get_download_url(file_id, account_key)

        logger.info(f"302 重定向: [{account_key}] {path_for_api} -> {download_url[:80]}...")

        return redirect(download_url, code=302)
    except Exception as e:
        logger.error(f"下载处理异常: {str(e)}")
        _notify_failure(full_path, str(e))
        return jsonify({'error': str(e)}), 500


# ==================== 启动 ====================

if __name__ == "__main__":
    # 初始化所有账号客户端
    init_clients()

    if TG_BOT_TOKEN:
        threading.Thread(target=_bot_polling_loop, daemon=True).start()

    if ACCOUNT_CHECK_INTERVAL > 0:
        threading.Thread(target=_account_check_loop, daemon=True).start()
        logger.info(f"账号健康检查已启用，间隔 {ACCOUNT_CHECK_INTERVAL} 分钟")
    
    port = get_int_env("PORT", 8515)
    host = get_env("HOST", "0.0.0.0")
    debug = get_env("DEBUG", "false").lower() == "true"
    
    logger.info(f"服务启动: http://{host}:{port}")
    
    app.run(host=host, port=port, debug=debug, threaded=True)
