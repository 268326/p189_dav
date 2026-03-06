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
