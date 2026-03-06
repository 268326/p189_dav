#!/usr/bin/env python3
# encoding: utf-8

"""天翼网盘登录/登出/扫码/Cookies 相关路由"""

import json as _json
import logging
import traceback
from urllib.parse import quote

import requests

from flask import Blueprint, request, jsonify, session

from p189client import P189Client, check_response

from config import get_proxies
from accounts import (
    clients, clients_lock,
    get_default_account_key, get_account_keys_set,
    save_cookies,
)
from cache import get_path_cache, get_url_cache
from health import fresh_login_url_params

logger = logging.getLogger(__name__)

bp = Blueprint('cloud', __name__)


@bp.route('/api/189/cookies')
def get_189_cookies():
    """获取天翼网盘 Cookies"""
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

        if hasattr(c, "cookies_str") and isinstance(c.cookies_str, str):
            cookies_str = c.cookies_str
            for pair in cookies_str.split(";"):
                if "=" in pair:
                    key, value = pair.strip().split("=", 1)
                    if key:
                        cookies_dict[key] = value
        else:
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


@bp.route('/api/189/login', methods=['POST'])
def api_189_login():
    """天翼网盘登录"""
    if not session.get('logged_in'):
        return jsonify({'error': '未登录'}), 401
    data = request.get_json(silent=True) or {}
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
            get_path_cache(account_key).clear()
            get_url_cache(account_key).clear()

            return jsonify({'success': True, 'message': '登录成功', 'account_key': account_key})
        except Exception as e:
            clients[account_key] = None
            return jsonify({'error': f'登录失败: {str(e)}'}), 401


@bp.route('/api/189/logout', methods=['POST'])
def api_189_logout():
    """天翼网盘登出"""
    if not session.get('logged_in'):
        return jsonify({'error': '未登录'}), 401
    data = request.get_json(silent=True) or {}
    account_key = data.get('account_key')

    with clients_lock:
        if account_key:
            if account_key in clients:
                clients[account_key] = None
                get_path_cache(account_key).clear()
                get_url_cache(account_key).clear()
            return jsonify({'success': True, 'message': f'已登出账号 [{account_key}]'})
        else:
            for key in list(clients.keys()):
                clients[key] = None
            from cache import path_cache, url_cache
            for key in list(path_cache.keys()):
                path_cache[key].clear()
            for key in list(url_cache.keys()):
                url_cache[key].clear()
            return jsonify({'success': True, 'message': '已登出所有账号'})


@bp.route('/api/189/qrcode')
def api_189_qrcode():
    """获取天翼网盘扫码登录二维码"""
    if not session.get('logged_in'):
        return jsonify({'error': '未登录'}), 401
    account_key = request.args.get('account_key') or 'default'
    if account_key not in get_account_keys_set():
        return jsonify({'error': f'账号 [{account_key}] 不存在'}), 400

    session_key = 'qr_session' if account_key == 'default' else f'qr_session_{account_key}'
    try:
        app_id = "cloud"
        resp = P189Client.login_qrcode_uuid(app_id)
        if isinstance(resp, str):
            resp = _json.loads(resp)
        check_response(resp)
        encryuuid = resp["encryuuid"]
        uuid = resp["uuid"]

        logger.info(f"获取到二维码 UUID: {uuid[:50]}...")

        conf = fresh_login_url_params()

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
        logger.error(f"获取二维码失败: {e}\n{traceback.format_exc()}")
        return jsonify({'error': f'获取二维码失败: {str(e)}'}), 500


@bp.route('/api/189/qrcode/status')
def api_189_qrcode_status():
    """检查扫码状态"""
    if not session.get('logged_in'):
        return jsonify({'error': '未登录'}), 401
    account_key = request.args.get('account_key') or 'default'
    session_key = 'qr_session' if account_key == 'default' else f'qr_session_{account_key}'
    qr_session = session.get(session_key)
    if not qr_session:
        return jsonify({'error': '请先获取二维码'})

    try:
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
                    get_path_cache(account_key).clear()
                    get_url_cache(account_key).clear()
                    session.pop(session_key, None)
                    return jsonify({'success': True, 'message': '登录成功', 'account_key': account_key})
                except Exception as e:
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
        logger.error(f"检查扫码状态失败: {e}\n{traceback.format_exc()}")
        return jsonify({'error': str(e)})
