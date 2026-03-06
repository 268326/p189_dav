#!/usr/bin/env python3
# encoding: utf-8

"""
账号管理模块

管理天翼网盘多账号配置：账号 CRUD、客户端实例管理、Cookies 持久化。
"""

import os
import re
import json
import logging
import threading
from pathlib import Path

from p189client import P189Client

from config import (
    ACCOUNTS_FILE,
    ENV_189_USERNAME, ENV_189_PASSWORD, ENV_189_COOKIES,
)

logger = logging.getLogger(__name__)

# 账号 key 仅允许字母、数字、下划线（用于 URL 路径）
ACCOUNT_KEY_PATTERN = re.compile(r'^[a-zA-Z0-9_]+$')

# ==================== 客户端实例管理 ====================

# 全局客户端实例：account_key -> P189Client | None
clients: dict[str, P189Client | None] = {}
clients_lock = threading.RLock()

# ==================== 账号配置文件操作 ====================


def load_accounts_config() -> dict:
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


def save_accounts_config(data: dict) -> None:
    Path(ACCOUNTS_FILE).parent.mkdir(parents=True, exist_ok=True)
    with open(ACCOUNTS_FILE, 'w', encoding='utf-8') as f:
        json.dump(data, f, ensure_ascii=False, indent=2)


def get_accounts_list() -> list[dict]:
    """返回 [{"key", "label", ...}, ...]"""
    cfg = load_accounts_config()
    return list(cfg.get("accounts", []))


def get_default_account_key() -> str:
    cfg = load_accounts_config()
    return cfg.get("default_key") or "default"


def get_account_keys_set() -> set[str]:
    return {a["key"] for a in get_accounts_list()}


def get_cookies_path_for_account(account_key: str) -> str:
    """默认账号用 db/cookies.txt，其余用 db/accounts/<key>/cookies.txt"""
    if account_key == "default":
        return os.path.join("db", "cookies.txt")
    return os.path.join("db", "accounts", account_key, "cookies.txt")


def get_account_auto_login(account_key: str) -> dict:
    """获取账号的自动登录配置"""
    for a in get_accounts_list():
        if a["key"] == account_key:
            return {
                "method": a.get("auto_login", "none"),
                "username": a.get("username", ""),
                "password": a.get("password", ""),
            }
    return {"method": "none", "username": "", "password": ""}


# ==================== 客户端操作 ====================


def get_client(account_key: str | None = None) -> P189Client:
    """获取指定账号的客户端实例，account_key 为空时使用默认账号"""
    key = account_key or get_default_account_key()
    with clients_lock:
        c = clients.get(key)
    if c is None:
        raise Exception(f"账号 [{key}] 未登录，请先在管理界面登录天翼网盘")
    return c


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


def init_clients() -> None:
    """初始化所有账号的客户端（从 cookies 文件或环境变量）"""
    accounts = get_accounts_list()
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


def parse_account_and_path(raw_path: str) -> tuple[str, str]:
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
        rest = "/" + "/".join(parts[1:])
        return first, rest
    return default_key, "/" + raw_path
