#!/usr/bin/env python3
# encoding: utf-8

"""状态、配置管理、缓存管理 API 路由"""

import os
import time
import logging

from flask import Blueprint, request, jsonify, session
from pathlib import Path

from config import (
    TEMPLATE_ENV_PATH, ENV_FILE_PATH, ACCOUNT_CHECK_INTERVAL,
    CACHE_EXPIRATION, PATH_CACHE_EXPIRATION,
)
from accounts import (
    clients, clients_lock,
    get_accounts_list, get_default_account_key, get_account_keys_set,
)
from cache import (
    path_cache, url_cache,
    get_path_cache, get_url_cache, cache_meta,
)
from health import (
    health_check_lock, account_health, account_health_ts, account_health_err,
    check_accounts_health,
)

logger = logging.getLogger(__name__)

bp = Blueprint('api', __name__)


# ==================== 状态 ====================

@bp.route('/api/status')
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
    path_total = sum(len(get_path_cache(k)) for k in list(path_cache))
    url_total = sum(len(get_url_cache(k)) for k in list(url_cache))
    return jsonify({
        'logged_in': any_logged_in,
        'web_logged_in': session.get('logged_in', False),
        'accounts': account_list,
        'default_key': default_key,
        'path_cache_size': path_total,
        'url_cache_size': url_total
    })


@bp.route('/api/health')
def api_health():
    """获取账号健康检查状态"""
    with health_check_lock:
        data = {}
        for key in account_health:
            data[key] = {
                "ok": account_health.get(key),
                "error": account_health_err.get(key),
                "ts": account_health_ts.get(key, 0),
            }
    return jsonify({
        "interval_minutes": ACCOUNT_CHECK_INTERVAL,
        "accounts": data,
    })


@bp.route('/api/health/check', methods=['POST'])
def api_health_check_now():
    """手动触发一次账号健康检查"""
    if not session.get('logged_in'):
        return jsonify({'error': '未登录'}), 401
    results = check_accounts_health()
    return jsonify({"success": True, "results": results})


# ==================== 配置管理 ====================

@bp.route('/api/env')
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


@bp.route('/api/env', methods=['POST'])
def save_env_config():
    """保存配置"""
    if not session.get('logged_in'):
        return jsonify({'error': '未登录'}), 401

    data = request.get_json(silent=True)
    if not data:
        return jsonify({'error': '无效的请求数据'}), 400
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


# ==================== 缓存管理 ====================

@bp.route('/api/clear-cache', methods=['POST'])
def api_clear_cache():
    """清除缓存（body 可传 account_key，不传则清除所有账号缓存）"""
    data = request.get_json(silent=True) or {}
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


@bp.route('/api/cache')
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
    file_id_to_path: dict[tuple[str, str], tuple[str, float]] = {}

    for ak in account_keys:
        pc = get_path_cache(ak)
        uc = get_url_cache(ak)
        for path, (file_id, cache_time) in pc.items():
            key = (ak, str(file_id))
            if key not in file_id_to_path or cache_time > file_id_to_path[key][1]:
                file_id_to_path[key] = (path, cache_time)
        for path, (file_id, cache_time) in pc.items():
            meta = cache_meta(cache_time, PATH_CACHE_EXPIRATION)
            path_items.append({
                "account_key": ak,
                "path": path,
                "file_id": str(file_id),
                **meta
            })
        for file_id, (url, cache_time) in uc.items():
            path, _ = file_id_to_path.get((ak, str(file_id)), ("", 0))
            file_name = Path(path).name if path else ""
            meta = cache_meta(cache_time, CACHE_EXPIRATION)
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


@bp.route('/api/cache/path', methods=['POST'])
def api_cache_delete_path():
    """删除单个路径缓存"""
    if not session.get('logged_in'):
        return jsonify({'error': '未登录'}), 401
    data = request.get_json(silent=True) or {}
    path = data.get('path')
    account_key = data.get('account_key') or get_default_account_key()
    if not path:
        return jsonify({'error': '缺少 path'}), 400
    pc = get_path_cache(account_key)
    if path in pc:
        del pc[path]
    return jsonify({'success': True})


@bp.route('/api/cache/url', methods=['POST'])
def api_cache_delete_url():
    """删除单个链接缓存"""
    if not session.get('logged_in'):
        return jsonify({'error': '未登录'}), 401
    data = request.get_json(silent=True) or {}
    file_id_raw = data.get('file_id')
    account_key = data.get('account_key') or get_default_account_key()
    if file_id_raw is None or file_id_raw == '':
        return jsonify({'error': 'file_id 无效'}), 400
    uc = get_url_cache(account_key)
    # url_cache 的 key 可能是 int 或 str，两种都尝试删除
    for key_variant in (file_id_raw, str(file_id_raw)):
        if key_variant in uc:
            del uc[key_variant]
    try:
        int_key = int(file_id_raw)
        if int_key in uc:
            del uc[int_key]
    except (TypeError, ValueError):
        pass
    return jsonify({'success': True})
