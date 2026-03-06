"""
Web 路由包

将 Flask 路由按功能拆分为多个 Blueprint。
"""

from flask import Flask


def register_all_blueprints(app: Flask) -> None:
    """向 Flask 应用注册所有 Blueprint"""
    from routes.auth import bp as auth_bp
    from routes.api import bp as api_bp
    from routes.cloud import bp as cloud_bp
    from routes.accounts import bp as accounts_bp
    from routes.redirect import bp as redirect_bp

    app.register_blueprint(auth_bp)
    app.register_blueprint(api_bp)
    app.register_blueprint(cloud_bp)
    app.register_blueprint(accounts_bp)
    # redirect 蓝图最后注册，因为它有通配路由 /<path:file_path>
    app.register_blueprint(redirect_bp)
