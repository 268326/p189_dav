#!/usr/bin/env python3
# encoding: utf-8

"""Web 管理界面认证路由（登录/登出/首页）"""

from flask import Blueprint, request, jsonify, session, redirect, url_for, render_template

from config import ENV_WEB_PASSPORT, ENV_WEB_PASSWORD

bp = Blueprint('auth', __name__)


@bp.route('/')
def index():
    """首页/配置管理"""
    if not session.get('logged_in'):
        return redirect(url_for('auth.login_page'))
    return render_template('index.html')


@bp.route('/login')
def login_page():
    """登录页面"""
    if session.get('logged_in'):
        return redirect(url_for('auth.index'))
    return render_template('login.html')


@bp.route('/api/login', methods=['POST'])
def api_login():
    """登录 API"""
    data = request.get_json(silent=True) or {}
    username = data.get('username')
    password = data.get('password')

    if username == ENV_WEB_PASSPORT and password == ENV_WEB_PASSWORD:
        session['logged_in'] = True
        return jsonify({'success': True})
    else:
        return jsonify({'success': False, 'error': '用户名或密码错误'})


@bp.route('/api/logout', methods=['GET', 'POST'])
def api_logout():
    """登出 API"""
    session.pop('logged_in', None)
    if request.method == 'POST':
        return jsonify({'success': True})
    return redirect(url_for('auth.login_page'))
