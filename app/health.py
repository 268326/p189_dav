#!/usr/bin/env python3
# encoding: utf-8

"""
健康检查与自动重新登录模块

定期检查账号可用性，异常时通过 Telegram 通知并触发自动重新登录。
"""

import time
import json as _json
import logging
import threading
from urllib.parse import quote, parse_qsl, urlsplit

import requests
import httpx

from p189client import P189Client, check_response

from config import ACCOUNT_CHECK_INTERVAL, PROXY_URL, get_proxies, TG_BOT_TOKEN
from accounts import (
    clients, clients_lock,
    get_accounts_list, get_account_auto_login,
    save_cookies,
)
from cache import get_path_cache, get_url_cache
from telegram import (
    TG_NOTIFY_CHAT_SET,
    send_message, send_photo,
    notify_account_expired,
)

logger = logging.getLogger(__name__)

# ==================== 健康检查状态 ====================

# 每个 key 上次检查结果：True=正常, False=异常, None=未检查
account_health: dict[str, bool | None] = {}
account_health_ts: dict[str, float] = {}      # 上次检查时间戳
account_health_err: dict[str, str] = {}        # 上次异常信息
health_check_lock = threading.Lock()


def check_accounts_health() -> dict[str, dict]:
    """逐个检查已登录账号的可用性，状态变化时发 TG 通知。
    返回 {account_key: {"ok": bool, "error": str|None, "ts": float}}"""
    results: dict[str, dict] = {}
    accounts = get_accounts_list()
    now = time.time()

    for a in accounts:
        key = a["key"]
        label = a.get("label") or key
        with clients_lock:
            c = clients.get(key)
        if c is None:
            results[key] = {"ok": False, "error": "未登录", "ts": now}
            continue

        ok = False
        err_msg: str | None = None
        try:
            resp = c.fs_list_portal(
                {"fileId": -11, "pageNum": 1, "pageSize": 1}
            )
            error_code = resp.get("errorCode")
            res_code = resp.get("res_code")
            error_msg = resp.get("errorMsg") or resp.get("res_message") or ""
            if error_code == "InvalidSessionKey" or "check ip error" in error_msg:
                err_msg = f"Session 失效（IP 变更）: {error_msg}"
            elif error_code not in (0, "0", None) and res_code not in (0, "0", None):
                err_msg = error_msg or f"errorCode={error_code}"
            else:
                ok = True
        except Exception as e:
            err_msg = str(e)

        results[key] = {"ok": ok, "error": err_msg, "ts": now}

        with health_check_lock:
            prev = account_health.get(key)
            account_health[key] = ok
            account_health_ts[key] = now
            if err_msg:
                account_health_err[key] = err_msg
            elif key in account_health_err:
                del account_health_err[key]

        # 状态从正常变为异常时发送 TG 通知
        if prev is not False and not ok and err_msg and err_msg != "未登录":
            notify_account_expired(key, label, err_msg)
            logger.warning(f"账号健康检查: [{key}] 异常 — {err_msg}")
        elif ok:
            if prev is False:
                logger.info(f"账号健康检查: [{key}] 已恢复正常")
            else:
                logger.info(f"账号健康检查: [{key}] 正常")

    return results


def account_check_loop() -> None:
    """后台定时检查账号健康状态"""
    if ACCOUNT_CHECK_INTERVAL <= 0:
        return
    interval = ACCOUNT_CHECK_INTERVAL * 60
    time.sleep(30)
    while True:
        try:
            results = check_accounts_health()
            _auto_relogin_if_needed(results)
        except Exception as e:
            logger.error(f"账号健康检查异常: {e}")
        time.sleep(interval)


# ==================== 自动重新登录 ====================

# 正在进行扫码登录的账号，防止重复触发
_qr_relogin_active: dict[str, bool] = {}


def _auto_relogin_if_needed(results: dict[str, dict]) -> None:
    """根据健康检查结果，对异常账号触发自动重新登录"""
    for key, r in results.items():
        if r["ok"]:
            continue
        if r.get("error") == "未登录":
            continue
        cfg = get_account_auto_login(key)
        method = cfg["method"]
        if method == "none":
            continue

        label = ""
        for a in get_accounts_list():
            if a["key"] == key:
                label = a.get("label") or key
                break

        if method == "password":
            _auto_relogin_password(key, label, cfg["username"], cfg["password"])
        elif method == "qrcode":
            if _qr_relogin_active.get(key):
                logger.info(f"账号 [{key}] 扫码重登录正在进行中，跳过")
                continue
            threading.Thread(
                target=_auto_relogin_qrcode,
                args=(key, label),
                daemon=True,
            ).start()


def _auto_relogin_password(key: str, label: str, username: str, password: str) -> None:
    """使用账号密码自动重新登录"""
    if not username or not password:
        logger.warning(f"账号 [{key}] 配置了密码自动登录但未设置用户名/密码")
        return
    logger.info(f"账号 [{key}] 正在使用密码自动重新登录...")
    try:
        with clients_lock:
            clients[key] = P189Client(username, password)
            save_cookies(key)
            get_path_cache(key).clear()
            get_url_cache(key).clear()
        logger.info(f"账号 [{key}] 密码自动重新登录成功")
        for chat_id in TG_NOTIFY_CHAT_SET:
            send_message(chat_id, f"✅ 账号 [{label}] 已通过密码自动重新登录成功")
    except Exception as e:
        logger.error(f"账号 [{key}] 密码自动重新登录失败: {e}")
        for chat_id in TG_NOTIFY_CHAT_SET:
            send_message(chat_id, f"❌ 账号 [{label}] 密码自动重新登录失败: {e}")


def fresh_login_url_params() -> dict:
    """用无 cookies 的干净请求获取 lt / reqId / url 登录参数。"""
    with httpx.Client(follow_redirects=True, proxy=PROXY_URL or None, timeout=15) as client:
        resp = client.get(
            "https://cloud.189.cn/api/portal/loginUrl.action",
            params={
                "redirectURL": "https://cloud.189.cn/web/redirect.html",
                "defaultSaveName": 3,
                "defaultSaveNameCheck": "uncheck",
            },
        )
        url = str(resp.url)
        data = dict(parse_qsl(urlsplit(url).query))
        data["url"] = url
        if "lt" in data and "reqId" in data:
            return data

        for hist in resp.history:
            loc = hist.headers.get("location", "")
            if loc:
                loc_data = dict(parse_qsl(urlsplit(loc).query))
                if "lt" in loc_data and "reqId" in loc_data:
                    loc_data["url"] = loc
                    return loc_data

        raise Exception(f"无法获取登录参数 (lt/reqId)，最终 URL: {url}")


def _auto_relogin_qrcode(key: str, label: str) -> None:
    """通过 TG Bot 发送二维码让用户扫码重新登录"""
    if not TG_BOT_TOKEN or not TG_NOTIFY_CHAT_SET:
        logger.warning(f"账号 [{key}] 配置了扫码自动登录但未配置 TG Bot")
        return

    _qr_relogin_active[key] = True
    try:
        logger.info(f"账号 [{key}] 正在生成扫码登录二维码...")

        app_id = "cloud"
        resp = P189Client.login_qrcode_uuid(app_id)
        if isinstance(resp, str):
            resp = _json.loads(resp)
        check_response(resp)
        encryuuid = resp["encryuuid"]
        uuid = resp["uuid"]

        conf = fresh_login_url_params()

        app_conf = P189Client.login_app_conf(
            app_id,
            headers={"lt": conf["lt"], "reqId": conf["reqId"]},
        )
        if isinstance(app_conf, str):
            app_conf = _json.loads(app_conf)
        data = app_conf.get("data", {})

        qr_session = {
            "app_id": app_id,
            "encryuuid": encryuuid,
            "uuid": uuid,
            "lt": conf["lt"],
            "reqId": conf["reqId"],
            "url": conf["url"],
            "paramId": data.get("paramId", ""),
            "returnUrl": data.get("returnUrl", ""),
        }

        qr_image_url = f"https://open.e.189.cn/api/logbox/oauth2/image.do?uuid={quote(uuid, safe='')}"

        for chat_id in TG_NOTIFY_CHAT_SET:
            send_photo(
                chat_id,
                qr_image_url,
                caption=f"🔑 账号 [{label}] 已过期，请扫码重新登录\n（二维码 5 分钟内有效）",
            )

        logger.info(f"账号 [{key}] 二维码已发送到 TG，等待扫码...")

        # 轮询扫码状态，最多等 5 分钟
        deadline = time.time() + 300
        while time.time() < deadline:
            time.sleep(3)
            try:
                state = P189Client.login_qrcode_state(
                    {
                        "appId": app_id,
                        "encryuuid": encryuuid,
                        "uuid": uuid,
                        "returnUrl": quote(qr_session["returnUrl"], safe="") if qr_session["returnUrl"] else "",
                        "paramId": qr_session["paramId"],
                    },
                    headers={"lt": qr_session["lt"], "reqid": qr_session["reqId"], "referer": qr_session["url"]},
                )
                if isinstance(state, str):
                    state = _json.loads(state)

                status_code = state.get("status")
                if status_code is None:
                    status_code = state.get("result")

                if status_code == -106:
                    continue
                elif status_code == -11002:
                    logger.info(f"账号 [{key}] 已扫码，等待确认...")
                    continue
                elif status_code == 0:
                    redirect_url = state.get("redirectUrl", "")
                    cookies_from_state = state.get("cookies", {})
                    all_cookies = dict(cookies_from_state) if cookies_from_state else {}

                    if redirect_url:
                        for attempt in range(2):
                            try:
                                redirect_resp = requests.get(
                                    redirect_url,
                                    allow_redirects=False,
                                    timeout=25,
                                    headers={
                                        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36",
                                        "Referer": qr_session["url"],
                                    },
                                    proxies=get_proxies(),
                                )
                                for cookie in redirect_resp.cookies:
                                    all_cookies[cookie.name] = cookie.value
                                break
                            except Exception as e:
                                logger.warning(f"账号 [{key}] 请求 redirectUrl 失败（尝试 {attempt + 1}/2）: {e}")

                    if not all_cookies:
                        logger.error(f"账号 [{key}] 扫码成功但获取 cookies 失败")
                        for chat_id in TG_NOTIFY_CHAT_SET:
                            send_message(chat_id, f"❌ 账号 [{label}] 扫码成功但获取 cookies 失败，请手动登录")
                        return

                    with clients_lock:
                        cookies_str = "; ".join(f"{k}={v}" for k, v in all_cookies.items())
                        clients[key] = P189Client(cookies=cookies_str)
                        save_cookies(key)
                        get_path_cache(key).clear()
                        get_url_cache(key).clear()

                    logger.info(f"账号 [{key}] 扫码自动重新登录成功")
                    for chat_id in TG_NOTIFY_CHAT_SET:
                        send_message(chat_id, f"✅ 账号 [{label}] 扫码重新登录成功")
                    return
                elif status_code == -20099:
                    logger.warning(f"账号 [{key}] 二维码已过期")
                    for chat_id in TG_NOTIFY_CHAT_SET:
                        send_message(chat_id, f"⏰ 账号 [{label}] 二维码已过期，将在下次检查时重新生成")
                    return
                else:
                    logger.warning(f"账号 [{key}] 扫码状态异常: {state}")
                    return
            except Exception as e:
                logger.warning(f"账号 [{key}] 检查扫码状态失败: {e}")

        logger.warning(f"账号 [{key}] 扫码登录超时（5分钟），将在下次检查时重试")
        for chat_id in TG_NOTIFY_CHAT_SET:
            send_message(chat_id, f"⏰ 账号 [{label}] 扫码登录超时，将在下次检查时重试")

    except Exception as e:
        logger.error(f"账号 [{key}] 扫码自动重新登录异常: {e}")
        for chat_id in TG_NOTIFY_CHAT_SET:
            send_message(chat_id, f"❌ 账号 [{label}] 扫码自动重新登录异常: {e}")
    finally:
        _qr_relogin_active.pop(key, None)
