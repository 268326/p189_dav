#!/usr/bin/env python3
# encoding: utf-8

"""
应用配置模块

从 db/user.env 加载配置项，提供统一的配置访问接口。
"""

import os
import logging

from dotenv import dotenv_values

logger = logging.getLogger(__name__)

# ==================== 路径常量 ====================

TEMPLATE_ENV_PATH = 'templete.env'
ENV_FILE_PATH = os.path.join('db', 'user.env')
ACCOUNTS_FILE = os.path.join('db', 'accounts.json')

# 确保 db 目录存在
os.makedirs('db', exist_ok=True)

# ==================== Env 加载 ====================

def _load_env_file(path: str) -> dict[str, str]:
    if not os.path.exists(path):
        return {}
    values = dotenv_values(path)
    return {key: value for key, value in values.items() if value is not None}


ENV_FILE_VALUES = _load_env_file(ENV_FILE_PATH)


def get_env(key: str, default: str = "") -> str:
    """从 db/user.env 获取配置（不读取 compose 环境变量）"""
    value = ENV_FILE_VALUES.get(key)
    if value is None or value == "":
        return default
    return value


def get_int_env(key: str, default: int = 0) -> int:
    try:
        value = get_env(key, str(default))
        return int(value) if value != "" else default
    except (ValueError, TypeError):
        logger.warning(f"环境变量 {key} 值不是有效的整数，使用默认值 {default}")
        return default


# ==================== Web 管理界面认证 ====================

ENV_WEB_PASSPORT = get_env("ENV_WEB_PASSPORT", "admin")
ENV_WEB_PASSWORD = get_env("ENV_WEB_PASSWORD", "123456")

# ==================== 天翼网盘配置 ====================

ENV_189_USERNAME = get_env("ENV_189_USERNAME", "")
ENV_189_PASSWORD = get_env("ENV_189_PASSWORD", "")
ENV_189_COOKIES = get_env("ENV_189_COOKIES", "")
ENV_189_COOKIES_FILE = get_env("ENV_189_COOKIES_FILE", "db/cookies.txt")

# ==================== 缓存配置 ====================

CACHE_EXPIRATION = get_int_env("CACHE_EXPIRATION", 720) * 60    # 默认 720 分钟 → 秒
PATH_CACHE_EXPIRATION = get_int_env("PATH_CACHE_EXPIRATION", 12) * 3600  # 默认 12 小时 → 秒

# ==================== Telegram Bot 配置 ====================

TG_BOT_TOKEN = get_env("TG_BOT_TOKEN", "")
TG_BOT_NOTIFY_CHAT_IDS = get_env("TG_BOT_NOTIFY_CHAT_IDS", "")
TG_BOT_USER_WHITELIST = get_env("TG_BOT_USER_WHITELIST", "")
LOG_BUFFER_MAX = get_int_env("LOG_BUFFER_MAX", 1000)

# ==================== 账号健康检查 ====================

ACCOUNT_CHECK_INTERVAL = get_int_env("ACCOUNT_CHECK_INTERVAL", 30)  # 分钟，0 禁用

# ==================== 网络代理 ====================

PROXY_URL = (get_env("PROXY_URL") or get_env("HTTP_PROXY") or get_env("HTTPS_PROXY") or "").strip()

if PROXY_URL:
    os.environ["HTTP_PROXY"] = PROXY_URL
    os.environ["HTTPS_PROXY"] = PROXY_URL
    logger.info(f"已启用全局代理: {PROXY_URL}")


def get_proxies() -> dict[str, str] | None:
    """供 requests 使用的代理字典，未配置时返回 None"""
    if not PROXY_URL:
        return None
    return {"http": PROXY_URL, "https": PROXY_URL}
