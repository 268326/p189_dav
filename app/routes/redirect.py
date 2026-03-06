#!/usr/bin/env python3
# encoding: utf-8

"""302 直链重定向路由"""

import logging
from urllib.parse import unquote

from flask import Blueprint, request, jsonify, redirect

from accounts import parse_account_and_path
from cache import resolve_path_to_file_id, get_download_url
from telegram import notify_failure

logger = logging.getLogger(__name__)

bp = Blueprint('redirect', __name__)


@bp.route('/d/<path:file_path>')
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
        account_key, path_for_api = parse_account_and_path(decoded_path)

        file_id = resolve_path_to_file_id(path_for_api, account_key)
        download_url = get_download_url(file_id, account_key)

        logger.info(f"302 重定向: [{account_key}] {path_for_api} -> {download_url[:80]}...")

        return redirect(download_url, code=302)
    except Exception as e:
        logger.error(f"下载处理异常: {str(e)}")
        notify_failure(full_path, str(e))
        return jsonify({'error': str(e)}), 500


@bp.route('/<path:file_path>')
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
        account_key, path_for_api = parse_account_and_path(decoded_path)

        file_id = resolve_path_to_file_id(path_for_api, account_key)
        download_url = get_download_url(file_id, account_key)

        logger.info(f"302 重定向: [{account_key}] {path_for_api} -> {download_url[:80]}...")

        return redirect(download_url, code=302)
    except Exception as e:
        logger.error(f"下载处理异常: {str(e)}")
        notify_failure(full_path, str(e))
        return jsonify({'error': str(e)}), 500
