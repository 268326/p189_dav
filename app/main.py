#!/usr/bin/env python3
# encoding: utf-8

"""
天翼网盘 302 直链服务

支持通过请求路径获取天翼网盘文件的下载直链，返回 302 重定向。
模块化入口：注册蓝图、启动后台线程、运行 Flask 应用。
"""

import os
import secrets
import logging
import threading

from flask import Flask

# ==================== 日志 ====================

LOG_FORMAT = '%(asctime)s - %(name)s - %(levelname)s - %(message)s'
logging.basicConfig(level=logging.INFO, format=LOG_FORMAT)
logger = logging.getLogger(__name__)

# ==================== 确保 db 目录存在 ====================

os.makedirs('db', exist_ok=True)

# ==================== Flask 应用 ====================


def create_app() -> Flask:
    """创建并配置 Flask 应用"""
    app = Flask(__name__, template_folder='templates', static_folder='static')
    app.secret_key = secrets.token_hex(16)

    # 注册所有蓝图
    from routes import register_all_blueprints
    register_all_blueprints(app)

    return app


app = create_app()


# ==================== 启动 ====================

if __name__ == "__main__":
    from config import get_env, get_int_env, TG_BOT_TOKEN, ACCOUNT_CHECK_INTERVAL
    from accounts import init_clients
    from telegram import install_log_handler, bot_polling_loop
    from health import check_accounts_health, account_check_loop

    # 安装 Telegram 日志 Handler（可选）
    install_log_handler()

    # 初始化所有账号客户端
    init_clients()

    # Telegram Bot 轮询线程
    if TG_BOT_TOKEN:
        threading.Thread(target=bot_polling_loop, args=(check_accounts_health,), daemon=True).start()
        logger.info("Telegram Bot 轮询线程已启动")

    # 账号健康检查线程
    if ACCOUNT_CHECK_INTERVAL > 0:
        threading.Thread(target=account_check_loop, daemon=True).start()
        logger.info(f"账号健康检查已启用，间隔 {ACCOUNT_CHECK_INTERVAL} 分钟")

    port = get_int_env("PORT", 8515)
    host = get_env("HOST", "0.0.0.0")
    debug = get_env("DEBUG", "false").lower() == "true"

    logger.info(f"服务启动: http://{host}:{port}")

    app.run(host=host, port=port, debug=debug, threaded=True)
