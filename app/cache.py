#!/usr/bin/env python3
# encoding: utf-8

"""
缓存模块

管理路径→文件ID缓存、文件ID→下载链接缓存，以及下载链接获取。
"""

import time
import logging

from config import CACHE_EXPIRATION, PATH_CACHE_EXPIRATION
from accounts import get_client

logger = logging.getLogger(__name__)

# ==================== 缓存存储 ====================

# 文件 ID 缓存：account_key -> {路径: (文件ID, 时间戳)}
path_cache: dict[str, dict[str, tuple]] = {}

# 下载链接缓存：account_key -> {文件ID: (下载链接, 时间戳)}
url_cache: dict[str, dict] = {}

# 每个账号记住可用的下载方法，避免反复尝试必然失败的方法
# account_key -> "video" | "portal" | "info" | None
_working_download_method: dict[str, str | None] = {}


def get_path_cache(account_key: str) -> dict:
    if account_key not in path_cache:
        path_cache[account_key] = {}
    return path_cache[account_key]


def get_url_cache(account_key: str) -> dict:
    if account_key not in url_cache:
        url_cache[account_key] = {}
    return url_cache[account_key]


# ==================== 缓存工具 ====================


def format_ts(ts: float) -> str:
    return time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(ts))


def cache_meta(cache_time: float, ttl_seconds: int) -> dict:
    if ttl_seconds <= 0:
        return {
            "created_at": format_ts(cache_time),
            "expires_at": "-",
            "ttl_seconds": ttl_seconds,
            "remaining_seconds": -1,
        }
    expires_at = cache_time + ttl_seconds
    remaining = max(0, int(expires_at - time.time()))
    return {
        "created_at": format_ts(cache_time),
        "expires_at": format_ts(expires_at),
        "ttl_seconds": ttl_seconds,
        "remaining_seconds": remaining,
    }


# ==================== 核心业务 ====================


def resolve_path_to_file_id(file_path: str, account_key: str):
    """
    将文件路径解析为文件 ID

    :param file_path: 文件路径，如 /test/test.mkv
    :param account_key: 账号标识
    :return: 文件 ID
    """
    pc = get_path_cache(account_key)

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

    for i, part in enumerate(parts):
        current_path += "/" + part

        # 检查当前级别的缓存
        if current_path in pc:
            cached_id, cache_time = pc[current_path]
            if current_time - cache_time < PATH_CACHE_EXPIRATION:
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

            file_list = resp.get("data", []) or resp.get("fileList", []) or []

            # 缓存当前目录下的所有文件路径
            for item in file_list:
                item_name = item.get("fileName") or item.get("name", "")
                item_id = item.get("fileId") or item.get("id")
                item_path = current_path.rsplit("/", 1)[0] + "/" + item_name if current_path.count("/") > 1 else "/" + item_name

                if item_path not in pc:
                    pc[item_path] = (item_id, current_time)

                if item_name == part:
                    current_folder_id = item_id
                    pc[current_path] = (item_id, current_time)
                    found = True

            if found:
                break

            record_count = resp.get("recordCount", 0)
            if page_num * 100 >= record_count:
                break
            page_num += 1

        if not found:
            raise Exception(f"文件或目录不存在: {current_path}")

    return current_folder_id


def _fetch_download_url(c, file_id, account_key: str = "") -> str:
    """
    通过多种方式获取文件下载链接。
    记住每个账号可用的方法，后续直接使用，避免反复调用必然失败的 API。
    """
    methods = {
        "video": lambda: _try_video(c, file_id),
        "portal": lambda: _try_portal(c, file_id),
        "info": lambda: _try_info(c, file_id),
    }
    method_order = ["video", "portal", "info"]

    # 如果已知可用方法，优先使用
    known = _working_download_method.get(account_key)
    if known and known in methods:
        try:
            url = methods[known]()
            if url:
                return url
        except Exception:
            pass
        # 已知方法失败了，清除缓存，重新探测
        _working_download_method.pop(account_key, None)

    # 按顺序探测
    for name in method_order:
        if name == known:
            continue  # 已经试过了
        try:
            url = methods[name]()
            if url:
                _working_download_method[account_key] = name
                logger.debug(f"账号 [{account_key}] 下载方法锁定为: {name}")
                return url
        except Exception:
            continue

    raise Exception(f"无法获取文件 {file_id} 的下载链接")


def _try_video(c, file_id) -> str | None:
    """使用 video 接口获取下载链接"""
    resp = c.download_url_video({"fileId": file_id})
    if resp.get("res_code") == 0:
        normal = resp.get("normal", {})
        if normal.get("url"):
            return normal["url"]
    return None


def _try_portal(c, file_id) -> str | None:
    """使用 portal 接口获取下载链接"""
    resp = c.download_url_video_portal({"fileId": file_id})
    if resp.get("res_code") == 0:
        normal = resp.get("normal", {})
        if normal.get("url"):
            return normal["url"]
    return None


def _try_info(c, file_id) -> str | None:
    """使用 info 接口获取下载链接"""
    resp = c.download_url_info({"fileId": file_id})
    if resp.get("res_code") == 0 and "fileDownloadUrl" in resp:
        from html import unescape
        return unescape(resp["fileDownloadUrl"])
    return None


def get_download_url(file_id, account_key: str) -> str:
    """
    获取文件下载直链

    :param file_id: 文件 ID
    :param account_key: 账号标识
    :return: 下载链接
    """
    uc = get_url_cache(account_key)
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

    download_url = _fetch_download_url(c, file_id, account_key)
    uc[file_id] = (download_url, current_time)
    return download_url
