#!/usr/bin/env python3
# encoding: utf-8

"""
Telegram Bot 模块

提供消息发送、通知推送、日志缓冲区、Bot 长轮询等功能。
"""

import re
import html
import time
import logging
from collections import deque

import requests

from config import (
    TG_BOT_TOKEN, TG_BOT_NOTIFY_CHAT_IDS, TG_BOT_USER_WHITELIST,
    LOG_BUFFER_MAX, get_proxies,
)

logger = logging.getLogger(__name__)

# ==================== 日志缓冲区 ====================

LOG_FORMAT = '%(asctime)s - %(name)s - %(levelname)s - %(message)s'

log_buffer: deque[str] = deque(maxlen=LOG_BUFFER_MAX)


class LogBufferHandler(logging.Handler):
    def emit(self, record: logging.LogRecord) -> None:
        try:
            message = self.format(record)
        except Exception:
            message = record.getMessage()
        log_buffer.append(message)


def install_log_handler() -> None:
    """安装日志缓冲 Handler 到 root logger"""
    handler = LogBufferHandler()
    handler.setFormatter(logging.Formatter(LOG_FORMAT))
    logging.getLogger().addHandler(handler)


# ==================== 白名单 / Chat ID 解析 ====================

def _parse_whitelist(value: str) -> set[str]:
    entries = [v.strip() for v in value.split(",") if v.strip()]
    return set(entries)


TG_NOTIFY_CHAT_SET = _parse_whitelist(TG_BOT_NOTIFY_CHAT_IDS)
TG_USER_WHITELIST_SET = _parse_whitelist(TG_BOT_USER_WHITELIST)


def is_user_allowed(user_id: str) -> bool:
    return bool(TG_USER_WHITELIST_SET) and user_id in TG_USER_WHITELIST_SET


# ==================== 消息发送 ====================


def send_message(chat_id: str, text: str, parse_mode: str | None = None) -> None:
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


def send_photo(chat_id: str, photo_url: str, caption: str = "") -> None:
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


# ==================== 通知 ====================


def notify_all(message: str) -> None:
    """向所有通知 Chat 发送消息"""
    for chat_id in TG_NOTIFY_CHAT_SET:
        send_message(chat_id, message)


def notify_failure(file_path: str, error: str) -> None:
    if not TG_BOT_TOKEN or not TG_NOTIFY_CHAT_SET:
        return
    message = f"302 获取直链失败\n路径: {file_path}\n错误: {error}"
    notify_all(message)


def notify_account_expired(account_key: str, label: str, error: str) -> None:
    if not TG_BOT_TOKEN or not TG_NOTIFY_CHAT_SET:
        return
    message = (
        f"⚠️ 天翼网盘账号异常\n"
        f"账号: {label} [{account_key}]\n"
        f"错误: {error}\n"
        f"请及时重新登录"
    )
    notify_all(message)


# ==================== 日志发送 ====================


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


def send_log_to_chat(chat_id: str, lines: list[str]) -> None:
    cleaned = [_sanitize_log_line(line) for line in lines]
    payload = "\n".join(cleaned).strip()
    if not payload:
        send_message(chat_id, "暂无日志")
        return
    for part in _split_message(payload):
        safe_part = html.escape(part)
        send_message(chat_id, f"<pre>{safe_part}</pre>", parse_mode="HTML")


# ==================== Bot 长轮询 ====================


def bot_polling_loop(health_check_fn=None) -> None:
    """
    Telegram Bot 长轮询主循环

    :param health_check_fn: 健康检查回调函数，用于 /189health 命令
    """
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
                    if not is_user_allowed(user_id):
                        send_message(chat_id, "未授权的用户")
                        continue
                    lines = list(log_buffer)[-100:]
                    send_log_to_chat(chat_id, lines)
                elif text.startswith("/189health"):
                    if not is_user_allowed(user_id):
                        send_message(chat_id, "未授权的用户")
                        continue
                    if health_check_fn:
                        try:
                            results = health_check_fn()
                            lines = []
                            for k, r in results.items():
                                icon = "✅" if r["ok"] else "❌"
                                lines.append(f'{icon} {k}: {"正常" if r["ok"] else r.get("error", "异常")}')
                            send_message(chat_id, "账号健康检查结果:\n" + "\n".join(lines))
                        except Exception as e:
                            send_message(chat_id, f"检查失败: {e}")
                    else:
                        send_message(chat_id, "健康检查未配置")
        except Exception as e:
            logger.warning(f"Telegram 轮询异常: {e}")
            time.sleep(2)
