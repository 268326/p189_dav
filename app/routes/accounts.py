#!/usr/bin/env python3
# encoding: utf-8

"""账号管理 API 路由"""

import logging

from flask import Blueprint, request, jsonify, session

from accounts import (
    ACCOUNT_KEY_PATTERN,
    clients, clients_lock,
    load_accounts_config, save_accounts_config,
    get_accounts_list, get_default_account_key, get_account_keys_set,
    get_account_auto_login,
)
from cache import path_cache, url_cache

logger = logging.getLogger(__name__)

bp = Blueprint('accounts_bp', __name__)


@bp.route('/api/accounts')
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


@bp.route('/api/accounts', methods=['POST'])
def api_accounts_add():
    """添加账号，body: { "key": "xxx", "label": "显示名" }"""
    if not session.get('logged_in'):
        return jsonify({'error': '未登录'}), 401
    data = request.get_json(silent=True) or {}
    key = (data.get("key") or "").strip()
    label = (data.get("label") or key or "").strip()
    if not key:
        return jsonify({"error": "账号 key 不能为空"}), 400
    if not ACCOUNT_KEY_PATTERN.match(key):
        return jsonify({"error": "账号 key 仅允许字母、数字、下划线"}), 400
    if key in get_account_keys_set():
        return jsonify({"error": f"账号 [{key}] 已存在"}), 400
    cfg = load_accounts_config()
    cfg["accounts"].append({"key": key, "label": label or key})
    save_accounts_config(cfg)
    with clients_lock:
        clients[key] = None
    return jsonify({"success": True, "account_key": key})


@bp.route('/api/accounts/set-default', methods=['POST'])
def api_accounts_set_default():
    """设置默认账号，body: { "account_key": "xxx" }"""
    if not session.get('logged_in'):
        return jsonify({'error': '未登录'}), 401
    data = request.get_json(silent=True) or {}
    key = (data.get("account_key") or "").strip()
    if key not in get_account_keys_set():
        return jsonify({"error": f"账号 [{key}] 不存在"}), 400
    cfg = load_accounts_config()
    cfg["default_key"] = key
    save_accounts_config(cfg)
    return jsonify({"success": True, "default_key": key})


@bp.route('/api/accounts/<account_key>', methods=['DELETE'])
def api_accounts_remove(account_key):
    """删除账号（不能删除最后一个）"""
    if not session.get('logged_in'):
        return jsonify({'error': '未登录'}), 401
    if account_key not in get_account_keys_set():
        return jsonify({"error": "账号不存在"}), 404
    cfg = load_accounts_config()
    if len(cfg["accounts"]) <= 1:
        return jsonify({"error": "至少保留一个账号"}), 400
    cfg["accounts"] = [a for a in cfg["accounts"] if a["key"] != account_key]
    if cfg.get("default_key") == account_key:
        cfg["default_key"] = cfg["accounts"][0]["key"]
    save_accounts_config(cfg)
    with clients_lock:
        if account_key in clients:
            del clients[account_key]
    if account_key in path_cache:
        del path_cache[account_key]
    if account_key in url_cache:
        del url_cache[account_key]
    return jsonify({"success": True})


@bp.route('/api/accounts/auto-login', methods=['POST'])
def api_accounts_auto_login():
    """设置账号自动登录方式，body: { account_key, method, username?, password? }"""
    if not session.get('logged_in'):
        return jsonify({'error': '未登录'}), 401
    data = request.get_json(silent=True) or {}
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

    cfg = load_accounts_config()
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
    save_accounts_config(cfg)
    return jsonify({"success": True, "account_key": key, "method": method})


@bp.route('/api/accounts/auto-login/<account_key>')
def api_accounts_auto_login_get(account_key):
    """获取账号自动登录配置"""
    if not session.get('logged_in'):
        return jsonify({'error': '未登录'}), 401
    cfg = get_account_auto_login(account_key)
    has_password = bool(cfg["username"] and cfg["password"])
    return jsonify({
        "account_key": account_key,
        "method": cfg["method"],
        "username": cfg["username"] if has_password else "",
        "has_password": has_password,
    })


import time as _time

# 账号详情缓存: { account_key: { "data": {...}, "ts": timestamp } }
_detail_cache: dict = {}
_DETAIL_CACHE_TTL = 300  # 5 分钟


def _format_size(byte_val) -> str:
    """将字节数格式化为人类可读的字符串"""
    try:
        b = float(byte_val)
    except (TypeError, ValueError):
        return str(byte_val)
    for unit in ('B', 'KB', 'MB', 'GB', 'TB'):
        if abs(b) < 1024:
            return f"{b:.2f} {unit}"
        b /= 1024
    return f"{b:.2f} PB"


def _fetch_account_detail(account_key, c):
    """从天翼云 API 获取单个账号的详细信息"""
    result = {"account_key": account_key}

    # 获取用户登录信息
    try:
        logined_info = c.user_logined_infos_portal()
        info_data = logined_info.get("data", logined_info)
        result["account"] = info_data.get("userAccount", "")
        result["nickname"] = info_data.get("nickname", "") or ""
    except Exception as e:
        logger.warning(f"获取账号 [{account_key}] 登录信息失败: {e}")
        result["account"] = ""
        result["nickname"] = ""

    # 获取用户基本信息
    try:
        user_info = c.user_info_portal()
        if not result.get("account"):
            result["account"] = (
                user_info.get("userAccount")
                or user_info.get("loginName")
                or user_info.get("account", "")
            )
        result["has_family"] = user_info.get("hasFamily", 0) == 1
        result["max_filesize"] = user_info.get("maxFilesize", 0)
    except Exception as e:
        logger.warning(f"获取账号 [{account_key}] 用户信息失败: {e}")

    # 获取昵称
    if not result.get("nickname") or result["nickname"] == result.get("account", ""):
        try:
            ext_info = c.user_info_ext_portal()
            nick = ext_info.get("nickName", "")
            if nick:
                result["nickname"] = nick
        except Exception:
            pass

    # 获取容量信息
    try:
        size_info = c.user_size_info_portal()
        result["account"] = result.get("account") or size_info.get("account", "")
        cloud_info = size_info.get("cloudCapacityInfo", {})
        family_info = size_info.get("familyCapacityInfo", {})
        cloud_capacity = cloud_info.get("totalSize", 0) or size_info.get("cloudCapacity", 0)
        cloud_use = cloud_info.get("usedSize", 0) or size_info.get("cloudUse", 0)
        family_capacity = family_info.get("totalSize", 0) or size_info.get("familyCapacity", 0)
        family_use = family_info.get("usedSize", 0) or size_info.get("familyUse", 0)
        result["personal"] = {
            "capacity": cloud_capacity, "used": cloud_use,
            "capacity_str": _format_size(cloud_capacity), "used_str": _format_size(cloud_use),
            "percent": round(cloud_use / cloud_capacity * 100, 1) if cloud_capacity else 0,
        }
        result["family"] = {
            "capacity": family_capacity, "used": family_use,
            "capacity_str": _format_size(family_capacity), "used_str": _format_size(family_use),
            "percent": round(family_use / family_capacity * 100, 1) if family_capacity else 0,
        }
    except Exception as e:
        logger.warning(f"获取账号 [{account_key}] 容量信息失败: {e}")
        result["personal"] = None
        result["family"] = None

    # 获取每日流量信息
    try:
        privileges = c.user_privileges_portal()
        trans_day_flow = int(privileges.get("transDayFlow", 0))
        used_day_flow = int(privileges.get("usedDayFlow", 0))
        remain_day_flow = max(trans_day_flow - used_day_flow, 0)
        result["day_flow"] = {
            "total": trans_day_flow, "used": used_day_flow, "remain": remain_day_flow,
            "total_str": _format_size(trans_day_flow), "used_str": _format_size(used_day_flow),
            "remain_str": _format_size(remain_day_flow),
            "percent_used": round(used_day_flow / trans_day_flow * 100, 1) if trans_day_flow else 0,
        }
    except Exception as e:
        logger.warning(f"获取账号 [{account_key}] 每日流量信息失败: {e}")
        result["day_flow"] = None

    # 获取家庭云信息
    try:
        family_list = c.fs_family_list()
        families = family_list.get("familyInfoResp", [])
        if families:
            result["family_count"] = len(families)
            result["families"] = [
                {"name": fam.get("remarkName") or fam.get("familyName") or "",
                 "member_count": fam.get("memberCount") or fam.get("userCount") or 0}
                for fam in families
            ]
        else:
            result["family_count"] = 0
            result["families"] = []
    except Exception as e:
        logger.debug(f"获取账号 [{account_key}] 家庭云信息失败: {e}")
        result["family_count"] = 0
        result["families"] = []

    return result


@bp.route('/api/accounts/details')
def api_accounts_details():
    """批量获取所有已登录账号的详细信息（带缓存）"""
    if not session.get('logged_in'):
        return jsonify({'error': '未登录'}), 401

    force_refresh = request.args.get('refresh') == '1'
    refresh_key = request.args.get('account_key', '').strip()  # 可选：只刷新指定账号

    now = _time.time()
    result = {}

    with clients_lock:
        logged_in_keys = {k: c for k, c in clients.items() if c is not None}

    for key, c in logged_in_keys.items():
        # 检查缓存
        should_refresh = force_refresh and (not refresh_key or refresh_key == key)
        cached = _detail_cache.get(key)
        if not should_refresh and cached and (now - cached["ts"]) < _DETAIL_CACHE_TTL:
            result[key] = cached["data"]
            result[key]["cached"] = True
            continue

        # 获取新数据
        try:
            detail = _fetch_account_detail(key, c)
            detail["cached"] = False
            _detail_cache[key] = {"data": detail, "ts": now}
            result[key] = detail
        except Exception as e:
            logger.error(f"获取账号 [{key}] 详情失败: {e}")
            if cached:
                result[key] = cached["data"]
                result[key]["cached"] = True
            else:
                result[key] = {"account_key": key, "error": str(e)}

    return jsonify({"details": result})


@bp.route('/api/accounts/detail/<account_key>')
def api_account_detail(account_key):
    """获取单个账号详细信息"""
    if not session.get('logged_in'):
        return jsonify({'error': '未登录'}), 401

    with clients_lock:
        c = clients.get(account_key)
    if c is None:
        return jsonify({'error': f'账号 [{account_key}] 未登录'}), 400

    force_refresh = request.args.get('refresh') == '1'
    now = _time.time()

    if not force_refresh:
        cached = _detail_cache.get(account_key)
        if cached and (now - cached["ts"]) < _DETAIL_CACHE_TTL:
            data = cached["data"]
            data["cached"] = True
            return jsonify(data)

    result = _fetch_account_detail(account_key, c)
    result["cached"] = False
    _detail_cache[account_key] = {"data": result, "ts": now}
    return jsonify(result)
