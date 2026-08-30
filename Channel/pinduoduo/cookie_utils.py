"""
Cookie 工具模块

提供:
- check_cookies_valid: 轻量级 cookie 有效性验证（HTTP 请求，非浏览器）
- ReloginGuard: 按账户的并发重登互斥锁 + 冷却期
- perform_relogin: 独立的 cookie 刷新/重登函数，可从任意上下文调用
"""

import json
import threading
import time
from typing import Dict, Optional

import requests

from database import db_manager
from utils.logger_loguru import get_logger
from utils.async_helper import run_async_in_thread
from utils.bark_notify import push_bark

logger = get_logger("CookieUtils")

# 会话过期错误码（与 BaseRequest.SESSION_EXPIRED_ERROR_CODE 一致）
SESSION_EXPIRED_ERROR_CODE = 43001

# 验证请求的默认 headers
_VALIDATION_HEADERS = {
    'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/137.0.0.0 Safari/537.36',
    'Content-Type': 'application/json',
    'accept-language': 'zh-CN,zh;q=0.9,en;q=0.8',
}


def check_cookies_valid(
    channel_name: str,
    shop_id: str,
    user_id: str,
    cookies: dict,
    timeout: float = 15.0,
) -> bool:
    """
    通过轻量级 HTTP 请求验证 cookie 是否有效。

    向拼多多 getToken 端点发送 POST 请求，检查响应中是否包含
    error_code=43001（会话已过期）。

    Args:
        channel_name: 渠道名称
        shop_id: 店铺 ID
        user_id: 用户 ID
        cookies: cookie 字典
        timeout: 请求超时（秒）

    Returns:
        True: cookie 有效（或网络错误无法确定时保守返回 True）
        False: cookie 已过期
    """
    url = "https://mms.pinduoduo.com/chats/getToken"
    payload = {'version': '3'}

    try:
        from utils.proxy_config import get_proxies
        response = requests.post(
            url,
            json=payload,
            headers=_VALIDATION_HEADERS,
            cookies=cookies,
            timeout=timeout,
            proxies=get_proxies(),
        )

        if response.status_code == 200:
            try:
                data = response.json()
                if data.get('error_code') == SESSION_EXPIRED_ERROR_CODE and \
                   '会话已过期' in str(data.get('error_msg', '')):
                    logger.warning(f"Cookie 验证失败: 会话已过期 ({channel_name}:{shop_id}:{user_id})")
                    return False
            except json.JSONDecodeError:
                logger.debug(f"Cookie 验证: 响应非 JSON，视为有效")
        else:
            logger.debug(f"Cookie 验证: HTTP {response.status_code}，视为有效")

        return True

    except requests.ConnectionError:
        logger.debug(f"Cookie 验证: 网络连接失败，保守视为有效")
        return True
    except requests.Timeout:
        logger.debug(f"Cookie 验证: 请求超时，保守视为有效")
        return True
    except Exception as e:
        logger.debug(f"Cookie 验证: 异常 {e}，保守视为有效")
        return True


class ReloginGuard:
    """按账户的并发重登互斥锁 + 冷却期，防止多个请求同时触发重登

    同时维护每个账户的连续失败计数：自动重登连续失败达到上限后
    （默认 3 次，可由 config.json 的 relogin.max_auto_failures 配置），
    停止自动重登并等待人工处理（try_acquire 直接拒绝）。
    """

    def __init__(self, cooldown_seconds: float = 60.0):
        self._lock = threading.Lock()
        self._account_locks: Dict[str, threading.Lock] = {}
        self._last_relogin: Dict[str, float] = {}
        self._failures: Dict[str, int] = {}
        self._notified: set = set()  # 已达上限且已发 Bark 通知的 key（同会话内去重）
        self._cooldown = cooldown_seconds

    @staticmethod
    def _make_key(channel_name: str, shop_id: str, user_id: str) -> str:
        return f"{channel_name}:{shop_id}:{user_id}"

    def _max_failures(self) -> int:
        """连续失败上限（动态读配置，支持热更新）"""
        try:
            from config import config as _config
            return int(_config.get("relogin.max_auto_failures", 3))
        except Exception:
            return 3

    def failure_count(self, channel_name: str, shop_id: str, user_id: str) -> int:
        """当前连续失败次数"""
        key = self._make_key(channel_name, shop_id, user_id)
        with self._lock:
            return self._failures.get(key, 0)

    def reset(self, channel_name: str, shop_id: str, user_id: str):
        """清零连续失败计数（人工处理/重启账号后调用，恢复自动重登机会）"""
        key = self._make_key(channel_name, shop_id, user_id)
        with self._lock:
            self._failures.pop(key, None)
            self._notified.discard(key)

    def reset_all(self):
        """清零所有账户的失败计数"""
        with self._lock:
            self._failures.clear()
            self._notified.clear()

    def try_acquire(self, channel_name: str, shop_id: str, user_id: str) -> bool:
        """
        尝试获取重登权。

        若其他线程正在重登，或冷却期内刚完成重登，返回 False。
        若连续失败已达上限（等待人工处理），返回 False，不再自动重登。
        否则获取锁并返回 True。
        """
        key = self._make_key(channel_name, shop_id, user_id)

        with self._lock:
            # 连续失败达上限 → 转入等待人工处理，不再自动重登
            if self._failures.get(key, 0) >= self._max_failures():
                logger.warning(
                    f"自动重登连续失败 {self._failures[key]} 次，已达上限，"
                    f"停止自动重登等待人工处理: {key}"
                )
                return False

            last_time = self._last_relogin.get(key, 0)
            if time.time() - last_time < self._cooldown:
                return False

            if key not in self._account_locks:
                self._account_locks[key] = threading.Lock()
            acct_lock = self._account_locks[key]

        return acct_lock.acquire(blocking=False)

    def release(self, channel_name: str, shop_id: str, user_id: str, success: bool = False):
        """释放锁；成功时记录时间戳用于冷却期并清零失败计数，失败时累加失败计数"""
        key = self._make_key(channel_name, shop_id, user_id)
        should_notify = False

        with self._lock:
            acct_lock = self._account_locks.get(key)
            if success:
                self._failures.pop(key, None)
                self._notified.discard(key)
            else:
                new_count = self._failures.get(key, 0) + 1
                self._failures[key] = new_count
                if new_count >= self._max_failures():
                    logger.error(
                        f"自动重登连续失败 {new_count} 次，"
                        f"已达上限，转入等待人工处理: {key}"
                    )
                    # 仅在第一次达上限时发 Bark 通知，避免健康检查循环重复轰炸
                    if key not in self._notified:
                        self._notified.add(key)
                        should_notify = True

        if acct_lock:
            try:
                acct_lock.release()
            except RuntimeError:
                pass

        if success:
            with self._lock:
                self._last_relogin[key] = time.time()

        # 锁外异步发通知，绝不阻塞重登/发送链路
        if should_notify:
            shop_name = str(shop_id)
            try:
                info = db_manager.get_shop(channel_name, shop_id)
                if info and info.get("shop_name"):
                    shop_name = str(info["shop_name"])
            except Exception:
                pass
            push_bark(
                "客服系统：账号重登失败，需人工处理",
                f"店铺「{shop_name}」({shop_id}) 自动重登连续失败 "
                f"{self._failures.get(key, 0)} 次已达上限，已停止自动重登。"
                f"请尽快扫码/手动重新登录，期间该店铺消息将无法回复。",
            )


# 模块级单例
relogin_guard = ReloginGuard(cooldown_seconds=60.0)


def perform_relogin(
    channel_name: str,
    shop_id: str,
    user_id: str,
    username: str,
    password: str,
    headless_fallback: bool = False,
) -> bool:
    """
    执行 cookie 刷新/重登，优先刷新，失败时回退到完整登录。

    此函数可从任意上下文调用（同步/异步），内部处理线程隔离。

    Args:
        channel_name: 渠道名称
        shop_id: 店铺 ID
        user_id: 用户 ID
        username: 用户名
        password: 密码
        headless_fallback: 完整登录时是否使用 headless 模式

    Returns:
        是否成功获取新 cookie
    """
    if not relogin_guard.try_acquire(channel_name, shop_id, user_id):
        logger.info(f"重登已在其他线程进行中或冷却期内: {username}")
        from Channel.pinduoduo.cookie_cache import cookie_cache
        return cookie_cache.get(channel_name, shop_id, user_id) is not None

    try:
        import importlib
        pdd_login_module = importlib.import_module('Channel.pinduoduo.pdd_login')

        # 阶段 1: 刷新 cookie（headless，利用浏览器持久化会话）
        logger.info(f"尝试刷新 cookie（无需重新登录）: {username}")
        try:
            refresh_result = run_async_in_thread(
                pdd_login_module.refresh_pdd_cookies(username, password),
                timeout=60.0,
            )
            if refresh_result and isinstance(refresh_result, dict):
                new_cookies = refresh_result.get('cookies')
                if new_cookies:
                    _apply_cookies_to_cache_and_db(
                        channel_name, shop_id, user_id, new_cookies
                    )
                    logger.info(f"Cookie 刷新成功: {username}")
                    relogin_guard.release(channel_name, shop_id, user_id, success=True)
                    return True
        except Exception as e:
            logger.warning(f"Cookie 刷新异常: {username}, {e}")

        # 阶段 2: 完整重新登录
        if not password:
            logger.error(f"缺少密码，无法完整重新登录: {username}")
            relogin_guard.release(channel_name, shop_id, user_id, success=False)
            return False

        logger.info(f"回退到完整重新登录 (headless={headless_fallback}): {username}")
        try:
            # 非 headless 模式给用户更多时间处理验证码/滑块
            login_timeout = 60.0 if headless_fallback else 120.0
            login_result = run_async_in_thread(
                pdd_login_module.login_pdd(username, password, headless_fallback),
                timeout=login_timeout,
            )
            if login_result and isinstance(login_result, dict):
                new_cookies = login_result.get('cookies')
                if new_cookies:
                    _apply_cookies_to_cache_and_db(
                        channel_name, shop_id, user_id, new_cookies
                    )
                    logger.info(f"完整重新登录成功: {username}")
                    relogin_guard.release(channel_name, shop_id, user_id, success=True)
                    return True
        except Exception as e:
            logger.error(f"完整重新登录异常: {username}, {e}")

        logger.error(f"重新登录失败: {username}")
        relogin_guard.release(channel_name, shop_id, user_id, success=False)
        return False

    except Exception as e:
        logger.error(f"重登流程异常: {username}, {e}")
        relogin_guard.release(channel_name, shop_id, user_id, success=False)
        return False


def _apply_cookies_to_cache_and_db(
    channel_name: str, shop_id: str, user_id: str, new_cookies
):
    """将新 cookie 同步写入缓存和数据库"""
    from Channel.pinduoduo.cookie_cache import cookie_cache

    if isinstance(new_cookies, str):
        try:
            cookies_dict = json.loads(new_cookies)
        except json.JSONDecodeError:
            logger.error("应用 cookie 失败: JSON 解析错误")
            return
    elif isinstance(new_cookies, dict):
        cookies_dict = new_cookies
    else:
        logger.error(f"应用 cookie 失败: 不支持的类型 {type(new_cookies)}")
        return

    cookie_cache.set(channel_name, shop_id, user_id, cookies_dict)
    db_manager.update_account_cookies(channel_name, shop_id, user_id, new_cookies)
