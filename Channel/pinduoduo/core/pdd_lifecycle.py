# 生命周期管理模块
import asyncio
import time
import threading
import websockets
from websockets import exceptions as ws_exceptions
from typing import Optional, Any, Dict, Set, TYPE_CHECKING
from utils.logger_loguru import get_logger
from Channel.pinduoduo.utils.API.get_token import GetToken
from config import config

if TYPE_CHECKING:
    from core.connection_status import ConnectionStatusManager
    from Channel.pinduoduo.core.pdd_config import ReconnectConfig, HeartbeatConfig
    from utils.resource_manager import WebSocketResourceManager


class LifecycleMixin:
    """生命周期管理 Mixin"""

    # Attributes provided by PDDChannel host class
    channel_name: str
    logger: Any
    status_manager: "ConnectionStatusManager"
    reconnect_config: "ReconnectConfig"
    heartbeat_config: "HeartbeatConfig"
    resource_manager: "WebSocketResourceManager"
    _stop_event: Optional[asyncio.Event]
    _threading_stop_event: threading.Event
    _reconnect_tasks: Dict[str, asyncio.Task]
    _heartbeat_tasks: Dict[str, asyncio.Task]
    _health_tasks: Dict[str, asyncio.Task]
    processing_tasks: Set[asyncio.Task[Any]]
    message_semaphore: asyncio.Semaphore
    loop: Optional[asyncio.AbstractEventLoop]
    ws: Optional[Any]
    API_VERSION: str
    base_url: str

    # Methods provided by ConnectionMixin / MessageHandlerMixin
    async def _connect_with_retry(self, shop_id: str, user_id: str, username: str, on_success, on_failure) -> None: ...
    async def _connect_single_attempt(self, shop_id: str, user_id: str, username: str, on_success, on_failure) -> None: ...
    async def _safe_close_websocket(self, ws: Any) -> None: ...
    async def _setup_message_consumer(self, queue_name: str) -> None: ...
    def _is_ws_closed(self, ws: Any) -> bool: ...
    async def _process_websocket_message(self, message: str, shop_id: str, user_id: str, username: str, queue_name: str) -> None: ...

    async def start_account(self, shop_id: str, user_id: str, on_success, on_failure):
        """启动指定店铺下账号"""
        self.logger.info(f"start_account 开始: {shop_id}-{user_id}")
        account_info = await asyncio.to_thread(db_manager.get_account, self.channel_name, shop_id, user_id)
        if not account_info:
            error_msg = f"账号 {user_id} 在数据库中不存在"
            self.logger.error(error_msg)
            on_failure(error_msg)
            return

        username = account_info.get("username", user_id)
        connection_key = f"{shop_id}_{user_id}"

        self.logger.info(f"start_account: account_info 获取成功, username={username}, connection_key={connection_key}")
        self.status_manager.update_status(shop_id, user_id, username, ConnectionState.CONNECTING)

        if connection_key in self._reconnect_tasks:
            self._reconnect_tasks[connection_key].cancel()
            del self._reconnect_tasks[connection_key]

        if self.reconnect_config.enable_auto_reconnect:
            self.logger.info(f"start_account: 调用 _connect_with_retry, _threading_stop_event={self._threading_stop_event.is_set()}, _stop_event={self._stop_event}")
            task = self._connect_with_retry(shop_id, user_id, username, on_success, on_failure)
        else:
            task = self._connect_single_attempt(shop_id, user_id, username, on_success, on_failure)

        # 保存任务引用
        connect_task = asyncio.create_task(task)
        self._reconnect_tasks[connection_key] = connect_task

        # 等待任务完成
        try:
            await connect_task
            self.logger.info(f"start_account: _connect_with_retry 正常返回: {shop_id}-{username}")
        except asyncio.CancelledError:
            self.logger.debug(f"连接任务被取消: {shop_id}-{username}")
        except Exception as e:
            self.logger.error(f"连接任务异常: {shop_id}-{username}, 错误: {e}")
        finally:
            # 清理任务引用
            if connection_key in self._reconnect_tasks:
                del self._reconnect_tasks[connection_key]
            self.logger.info(f"start_account 结束: {shop_id}-{username}")

    async def stop_account(self, shop_id: str, user_id: str):
        """停止指定店铺下账号"""
        try:
            account_info = await asyncio.to_thread(db_manager.get_account, self.channel_name, shop_id, user_id)
            if not account_info:
                self.logger.warning(f"账号 {user_id} 不存在，无法停止")
                return

            username = account_info.get("username", user_id)
            connection_key = f"{shop_id}_{user_id}"

            self.logger.info(f"正在停止店铺 {shop_id} 账号 {username}")

            if self._stop_event:
                self._stop_event.set()

            if connection_key in self._reconnect_tasks:
                task = self._reconnect_tasks[connection_key]
                if not task.done():
                    task.cancel()
                    try:
                        await asyncio.wait_for(task, timeout=5.0)
                    except asyncio.CancelledError:
                        self.logger.debug(f"重连任务已被取消: {connection_key}")
                    except asyncio.TimeoutError:
                        self.logger.warning(f"重连任务取消超时: {connection_key}")
                    except Exception as task_error:
                        self.logger.error(f"等待重连任务完成时出错: {task_error}")
                del self._reconnect_tasks[connection_key]
                self.logger.debug(f"已清理重连任务: {connection_key}")

            if connection_key in self._heartbeat_tasks:
                task = self._heartbeat_tasks[connection_key]
                if not task.done():
                    task.cancel()
                    try:
                        await asyncio.wait_for(task, timeout=3.0)
                    except asyncio.CancelledError:
                        self.logger.debug(f"心跳任务已被取消: {connection_key}")
                    except asyncio.TimeoutError:
                        self.logger.warning(f"心跳任务取消超时: {connection_key}")
                    except Exception as task_error:
                        self.logger.error(f"等待心跳任务完成时出错: {task_error}")
                del self._heartbeat_tasks[connection_key]
                self.logger.debug(f"已清理心跳任务: {connection_key}")

            if connection_key in self._health_tasks:
                task = self._health_tasks[connection_key]
                if not task.done():
                    task.cancel()
                    try:
                        await asyncio.wait_for(task, timeout=3.0)
                    except asyncio.CancelledError:
                        self.logger.debug(f"Cookie健康检查任务已被取消: {connection_key}")
                    except asyncio.TimeoutError:
                        self.logger.warning(f"Cookie健康检查任务取消超时: {connection_key}")
                    except Exception as task_error:
                        self.logger.error(f"等待Cookie健康检查任务完成时出错: {task_error}")
                del self._health_tasks[connection_key]
                self.logger.debug(f"已清理Cookie健康检查任务: {connection_key}")

            self.status_manager.update_status(shop_id, user_id, username, ConnectionState.DISCONNECTED)

            if self.ws:
                await self._safe_close_websocket(self.ws)
                self.logger.info(f"已关闭店铺 {shop_id} 账号 {username} 的WebSocket连接")
            else:
                self.logger.warning(f"店铺 {shop_id} 账号 {username} 的WebSocket连接已经关闭或不存在")

            await self.cleanup_processing_tasks()

            queue_name = f"pdd_{shop_id}"
            await self._cleanup_resources(queue_name)

            self.logger.info(f"成功停止店铺 {shop_id} 账号 {username}")

        except Exception as e:
            self.logger.error(f"停止店铺 {shop_id} 账号 {user_id} 时发生错误: {str(e)}")

    async def init(self, shop_id: str, user_id: str, username: str, on_success, on_failure):
        """初始化WebSocket连接和消息处理系统"""
        try:
            # 使用实例级停止事件，避免全局停止信号影响新连接
            self._stop_event = asyncio.Event()
            self.logger.info(f"init 开始: {shop_id}-{username}")

            token = GetToken(shop_id, user_id)
            access_token = token.get_token()
            self.logger.info(f"init: get_token 返回: {shop_id}-{username}, token={'None' if not access_token else 'OK(len=' + str(len(access_token)) + ')'}")

            if not access_token:
                raise RuntimeError(
                    f"获取access_token失败，cookie可能已过期: {shop_id}-{username}"
                )

            queue_name = f"pdd_{shop_id}"
            self.logger.info(f"init: 设置消息消费者: {shop_id}-{username}")
            await self._setup_message_consumer(queue_name)
            self.logger.info(f"init: 消息消费者设置完成: {shop_id}-{username}")

            params = {
                "access_token": access_token,
                "role": "mall_cs",
                "client": "web",
                "version": self.API_VERSION
            }
            query = "&".join([f"{k}={v}" for k, v in params.items()])
            full_url = f"{self.base_url}?{query}"

            self.logger.debug(f"正在连接到拼多多WebSocket: {shop_id}-{username}")

            self.logger.info(f"init: 开始 WebSocket 连接: {shop_id}-{username}")
            async with websockets.connect(
                full_url,
                ping_interval=60,
                ping_timeout=30,
                max_size=10**7,
                compression=None,
                close_timeout=10
            ) as websocket:
                self.ws = websocket
                self.resource_manager.register_websocket(
                    websocket,
                    f"PDD WebSocket ({shop_id}-{username})"
                )
                self.logger.debug(f"WebSocket连接已建立: {shop_id}-{username}")

                if self.ws and not self._is_ws_closed(self.ws):
                    self.logger.debug(f"WebSocket连接正常: {shop_id}-{username}")
                else:
                    self.logger.error(f"WebSocket连接异常: {shop_id}-{username}")

                self.status_manager.update_status(shop_id, user_id, username, ConnectionState.CONNECTED)
                self.logger.info(f"WebSocket 连接成功，状态已更新: {shop_id}-{username}")
                self.logger.debug(f"暂时跳过在线状态设置: {shop_id}-{username}")

                on_success()

                heartbeat_task = None
                if self.heartbeat_config.enable_heartbeat:
                    connection_key = f"{shop_id}_{user_id}"
                    heartbeat_task = asyncio.create_task(
                        self._heartbeat_loop(websocket, shop_id, user_id, username)
                    )
                    self._heartbeat_tasks[connection_key] = heartbeat_task
                    self.logger.debug(f"心跳检查已启动: {shop_id}-{username}")

                health_task = None
                if self.heartbeat_config.enable_cookie_health_check:
                    connection_key = f"{shop_id}_{user_id}"
                    health_task = asyncio.create_task(
                        self._cookie_health_loop(shop_id, user_id, username, on_failure)
                    )
                    self._health_tasks[connection_key] = health_task
                    self.logger.debug(f"Cookie 健康检查已启动: {shop_id}-{username}")

                message_task = asyncio.create_task(
                    self._message_loop(websocket, shop_id, user_id, username, queue_name)
                )

                stop_task = asyncio.create_task(self._stop_event.wait())

                try:
                    tasks = [message_task, stop_task]
                    if heartbeat_task:
                        tasks.append(heartbeat_task)
                    if health_task:
                        tasks.append(health_task)

                    done, pending = await asyncio.wait(
                        tasks,
                        return_when=asyncio.FIRST_COMPLETED
                    )

                    should_cleanup = False
                    should_reconnect = False  # 是否需要触发重连
                    if stop_task in done:
                        self.logger.debug(f"收到停止信号: {shop_id}-{username}")
                        should_cleanup = True
                    else:
                        # 消息循环或心跳异常结束（不是正常停止）
                        self.logger.warning(f"消息循环异常结束: {shop_id}-{username}")
                        should_cleanup = True
                        should_reconnect = True  # 需要触发重连

                    for task in pending:
                        task.cancel()
                        try:
                            await asyncio.wait_for(task, timeout=3.0)
                        except (asyncio.CancelledError, asyncio.TimeoutError, asyncio.InvalidStateError):
                            pass
                        except Exception as e:
                            self.logger.debug(f"等待任务取消时出错: {e}")

                    if should_cleanup:
                        await self._cleanup_resources(f"pdd_{shop_id}")

                    # 如果需要重连，抛出异常让 _connect_with_retry 捕获
                    if should_reconnect:
                        self.logger.info(f"消息循环异常结束，触发重连: {shop_id}-{username}")
                        raise RuntimeError(f"消息循环异常结束，需要重连: {shop_id}-{username}")

                except asyncio.CancelledError:
                    self.logger.debug(f"WebSocket任务被取消: {shop_id}-{username}")
                    message_task.cancel()
                    if heartbeat_task:
                        heartbeat_task.cancel()
                    if health_task:
                        health_task.cancel()
                    try:
                        await asyncio.wait_for(message_task, timeout=3.0)
                    except (asyncio.CancelledError, asyncio.TimeoutError, asyncio.InvalidStateError):
                        pass
                    if heartbeat_task:
                        try:
                            await asyncio.wait_for(heartbeat_task, timeout=3.0)
                        except (asyncio.CancelledError, asyncio.TimeoutError, asyncio.InvalidStateError):
                            pass
                    if health_task:
                        try:
                            await asyncio.wait_for(health_task, timeout=3.0)
                        except (asyncio.CancelledError, asyncio.TimeoutError, asyncio.InvalidStateError):
                            pass
                    await self._cleanup_resources(f"pdd_{shop_id}")
                    # 被取消不需要重连（用户主动停止）
                    raise

        except ws_exceptions.ConnectionClosed as e:
            self.status_manager.update_status(shop_id, user_id, username, ConnectionState.ERROR, str(e))
            self.logger.warning(f"WebSocket连接已关闭: {shop_id}-{username}, 错误: {str(e)}")
            # 不在此处调用 on_failure，让 _connect_with_retry 决定是否回调
            await self._cleanup_resources(f"pdd_{shop_id}")
            # 抛出异常，让 _connect_with_retry 能够捕获并触发重连
            raise
        except Exception as e:
            self.status_manager.update_status(shop_id, user_id, username, ConnectionState.ERROR, str(e))
            self.logger.error(f"WebSocket连接错误: {shop_id}-{username}, 错误: {str(e)}")
            await self._cleanup_resources(f"pdd_{shop_id}")
            # 抛出异常，让 _connect_with_retry 能够捕获并触发重连
            raise

    def request_stop(self):
        """请求停止WebSocket连接（线程安全）"""
        # 设置线程安全的停止事件
        self._threading_stop_event.set()
        # 同时设置asyncio.Event（在事件循环线程中安全地设置）
        if self._stop_event:
            if self.loop and not self.loop.is_closed():
                self.loop.call_soon_threadsafe(self._stop_event.set)
            else:
                try:
                    self._stop_event.set()
                except RuntimeError:
                    pass
            self.logger.info("已设置停止事件")

    async def stop_all_connections(self):
        """停止所有连接并清理所有任务"""
        try:
            self.logger.info("正在停止所有连接...")

            # 设置停止事件（两个都设置，确保所有检查点都能响应）
            self._threading_stop_event.set()
            if self._stop_event:
                self._stop_event.set()

            for connection_key, task in list(self._reconnect_tasks.items()):
                if not task.done():
                    task.cancel()
                    try:
                        await asyncio.wait_for(task, timeout=5.0)
                    except (asyncio.CancelledError, asyncio.TimeoutError):
                        self.logger.debug(f"任务已取消或超时: {connection_key}")
                    except Exception as e:
                        self.logger.error(f"停止任务时出错: {connection_key}, {e}")
                del self._reconnect_tasks[connection_key]

            for connection_key, task in list(self._heartbeat_tasks.items()):
                if not task.done():
                    task.cancel()
                    try:
                        await asyncio.wait_for(task, timeout=3.0)
                    except (asyncio.CancelledError, asyncio.TimeoutError):
                        self.logger.debug(f"心跳任务已取消或超时: {connection_key}")
                    except Exception as e:
                        self.logger.error(f"停止心跳任务时出错: {connection_key}, {e}")
                del self._heartbeat_tasks[connection_key]

            for connection_key, task in list(self._health_tasks.items()):
                if not task.done():
                    task.cancel()
                    try:
                        await asyncio.wait_for(task, timeout=3.0)
                    except (asyncio.CancelledError, asyncio.TimeoutError):
                        self.logger.debug(f"Cookie健康检查任务已取消或超时: {connection_key}")
                    except Exception as e:
                        self.logger.error(f"停止Cookie健康检查任务时出错: {connection_key}, {e}")
                del self._health_tasks[connection_key]

            if self.ws:
                await self._safe_close_websocket(self.ws)
                self.ws = None

            self.logger.info("所有连接已停止")

        except Exception as e:
            self.logger.error(f"停止所有连接时发生错误: {e}")

    async def _heartbeat_loop(self, websocket, shop_id: str, user_id: str, username: str):
        """心跳检查循环"""
        connection_key = f"{shop_id}_{user_id}"
        consecutive_failures = 0

        try:
            while not (self._stop_event and self._stop_event.is_set()) and not self._threading_stop_event.is_set():
                try:
                    start_time = time.time()
                    await websocket.ping()
                    response_time = time.time() - start_time

                    consecutive_failures = 0
                    self.logger.debug(f"心跳成功: {shop_id}-{username}, 响应时间: {response_time:.3f}s")

                    status = self.status_manager.get_status(shop_id, user_id)
                    if status and status.state == ConnectionState.CONNECTED:
                        pass

                    await asyncio.sleep(self.heartbeat_config.heartbeat_interval)

                except asyncio.TimeoutError:
                    consecutive_failures += 1
                    self.logger.warning(f"心跳超时: {shop_id}-{username}, 连续失败: {consecutive_failures}")
                    await asyncio.sleep(self.heartbeat_config.heartbeat_timeout)

                except Exception as e:
                    consecutive_failures += 1
                    self.logger.warning(f"心跳失败: {shop_id}-{username}, 错误: {str(e)}, 连续失败: {consecutive_failures}")

                    if consecutive_failures >= self.heartbeat_config.max_heartbeat_failures:
                        self.logger.error(f"心跳检查失败次数过多，标记连接为错误状态: {shop_id}-{username}")
                        self.status_manager.update_status(
                            shop_id, user_id, username,
                            ConnectionState.ERROR,
                            f"心跳检查失败: 连续{consecutive_failures}次失败"
                        )
                        break

                    await asyncio.sleep(self.heartbeat_config.heartbeat_timeout)

        except asyncio.CancelledError:
            self.logger.debug(f"心跳循环被取消: {shop_id}-{username}")
        except Exception as e:
            self.logger.error(f"心跳循环异常: {shop_id}-{username}, 错误: {str(e)}")
        finally:
            if connection_key in self._heartbeat_tasks:
                del self._heartbeat_tasks[connection_key]
            self.logger.debug(f"心跳循环已结束: {shop_id}-{username}")

    async def _cookie_health_loop(self, shop_id: str, user_id: str, username: str, on_failure=None):
        """Cookie 健康检查循环，定期验证 cookie 有效性并主动刷新

        自动重登连续失败达上限后停止自动重登（relogin_guard 拦截），
        置 ERROR 状态并回调 on_failure 通知 UI 等待人工处理（仅首次触发一次）。
        """
        from Channel.pinduoduo.cookie_utils import check_cookies_valid, perform_relogin, relogin_guard
        from Channel.pinduoduo.cookie_cache import cookie_cache

        connection_key = f"{shop_id}_{user_id}"
        notified_manual = False  # 防止达上限后每轮重复通知

        try:
            while not (self._stop_event and self._stop_event.is_set()) and not self._threading_stop_event.is_set():
                await asyncio.sleep(self.heartbeat_config.cookie_health_check_interval)

                if (self._stop_event and self._stop_event.is_set()) or self._threading_stop_event.is_set():
                    break

                # 从共享缓存或 DB 加载当前 cookie
                cookies = cookie_cache.get("pinduoduo", shop_id, user_id)
                if not cookies:
                    account_info = await asyncio.to_thread(db_manager.get_account, "pinduoduo", shop_id, user_id)
                    if account_info:
                        cookies_data = account_info.get('cookies')
                        if isinstance(cookies_data, str):
                            import json
                            try:
                                cookies = json.loads(cookies_data)
                            except json.JSONDecodeError:
                                cookies = None
                        elif isinstance(cookies_data, dict):
                            cookies = cookies_data

                if not cookies:
                    self.logger.debug(f"无 cookie 可检查: {shop_id}-{username}")
                    continue

                # 轻量级 HTTP 验证
                is_valid = await asyncio.to_thread(
                    check_cookies_valid,
                    "pinduoduo", shop_id, user_id, cookies,
                    self.heartbeat_config.cookie_health_check_timeout,
                )

                if not is_valid:
                    self.logger.warning(
                        f"Cookie 健康检查失败: {shop_id}-{username}，触发主动重登"
                    )
                    account_info = await asyncio.to_thread(db_manager.get_account, "pinduoduo", shop_id, user_id)
                    if account_info:
                        self.logger.info(f"开始主动重登: {shop_id}-{username}")
                        success = await asyncio.to_thread(
                            perform_relogin,
                            "pinduoduo", shop_id, user_id,
                            account_info.get('username', username),
                            account_info.get('password', ''),
                            False,
                        )
                        if success:
                            self.logger.info(f"主动重登成功: {shop_id}-{username}")
                            notified_manual = False  # 重登成功，后续再失败可重新通知
                        else:
                            self.logger.error(f"主动重登失败: {shop_id}-{username}")
                            # 自动重登连续失败达上限 → 停止自动重登，等待人工处理
                            max_failures = relogin_guard._max_failures()
                            failures = relogin_guard.failure_count("pinduoduo", shop_id, user_id)
                            if failures >= max_failures and not notified_manual:
                                notified_manual = True
                                error_msg = (
                                    f"登录过期，自动重登连续失败 {failures} 次，"
                                    f"等待人工处理: {username}"
                                )
                                self.logger.error(error_msg)
                                self.status_manager.update_status(
                                    shop_id, user_id, username,
                                    ConnectionState.ERROR, error_msg
                                )
                                if on_failure:
                                    try:
                                        on_failure(error_msg)
                                    except Exception as e:
                                        self.logger.warning(f"通知连接失败回调异常: {e}")
                else:
                    self.logger.info(f"Cookie 健康检查通过: {shop_id}-{username}")

        except asyncio.CancelledError:
            self.logger.debug(f"Cookie 健康检查循环被取消: {shop_id}-{username}")
        except Exception as e:
            self.logger.error(f"Cookie 健康检查循环异常: {shop_id}-{username}, {e}")
        finally:
            if connection_key in self._health_tasks:
                del self._health_tasks[connection_key]
            self.logger.debug(f"Cookie 健康检查循环已结束: {shop_id}-{username}")

    async def _message_loop(self, websocket, shop_id: str, user_id: str, username: str, queue_name: str):
        """消息接收循环"""
        try:
            self.logger.info(f"消息循环开始: {shop_id}-{username}")

            async for message in websocket:

                task = asyncio.create_task(
                    self._process_websocket_message_concurrent(
                        message, shop_id, user_id, username, queue_name
                    )
                )

                self.processing_tasks.add(task)
                task.add_done_callback(self.processing_tasks.discard)

        except ws_exceptions.ConnectionClosedError as cce:
            self.logger.error(f"WebSocket连接异常关闭: {shop_id}-{username}, 错误: {cce}")
        except ws_exceptions.ConnectionClosed as cc:
            self.logger.warning(f"WebSocket连接正常关闭: {shop_id}-{username}, 代码: {cc.code}")
        except Exception as e:
            self.logger.error(f"消息循环错误: {shop_id}-{username}, 错误: {str(e)}")

    async def _process_websocket_message_concurrent(self, message: str, shop_id: str, user_id: str, username: str, queue_name: str):
        """并发处理WebSocket消息"""
        async with self.message_semaphore:
            try:
                await self._process_websocket_message(message, shop_id, user_id, username, queue_name)
            except Exception as e:
                self.logger.error(f"并发处理消息失败: {e}")

    async def cleanup_processing_tasks(self):
        """清理所有处理任务"""
        if not self.processing_tasks:
            return

        self.logger.info(f"清理 {len(self.processing_tasks)} 个处理任务")
        for task in self.processing_tasks:
            if not task.done():
                task.cancel()
                try:
                    await task
                except asyncio.CancelledError:
                    pass
                except Exception as e:
                    self.logger.error(f"清理任务失败: {e}")

        self.processing_tasks.clear()

    async def _cleanup_reconnect_tasks(self):
        """清理所有重连任务"""
        try:
            for connection_key, task in list(self._reconnect_tasks.items()):
                if not task.done():
                    task.cancel()
                    try:
                        await asyncio.wait_for(task, timeout=5.0)
                    except (asyncio.CancelledError, asyncio.TimeoutError):
                        pass
                    except asyncio.InvalidStateError:
                        self.logger.debug(f"重连任务在不同的的事件循环中: {connection_key}")
                    except Exception as e:
                        self.logger.error(f"清理重连任务失败: {connection_key}, {e}")
            self._reconnect_tasks.clear()
        except Exception as e:
            self.logger.error(f"清理重连任务列表失败: {e}")

    async def _cleanup_heartbeat_tasks(self):
        """清理所有心跳任务"""
        try:
            for connection_key, task in list(self._heartbeat_tasks.items()):
                if not task.done():
                    task.cancel()
                    try:
                        await asyncio.wait_for(task, timeout=3.0)
                    except (asyncio.CancelledError, asyncio.TimeoutError):
                        pass
                    except asyncio.InvalidStateError:
                        self.logger.debug(f"心跳任务在不同的的事件循环中: {connection_key}")
                    except Exception as e:
                        self.logger.error(f"清理心跳任务失败: {connection_key}, {e}")
            self._heartbeat_tasks.clear()
        except Exception as e:
            self.logger.error(f"清理心跳任务列表失败: {e}")

    async def _cleanup_health_tasks(self):
        """清理所有 Cookie 健康检查任务"""
        try:
            for connection_key, task in list(self._health_tasks.items()):
                if not task.done():
                    task.cancel()
                    try:
                        await asyncio.wait_for(task, timeout=3.0)
                    except (asyncio.CancelledError, asyncio.TimeoutError):
                        pass
                    except asyncio.InvalidStateError:
                        self.logger.debug(f"Cookie健康检查任务在不同的的事件循环中: {connection_key}")
                    except Exception as e:
                        self.logger.error(f"清理Cookie健康检查任务失败: {connection_key}, {e}")
            self._health_tasks.clear()
        except Exception as e:
            self.logger.error(f"清理Cookie健康检查任务列表失败: {e}")

    async def _cleanup_resources(self, queue_name: str):
        """清理资源"""
        from Message.core.enhanced_consumer import enhanced_message_consumer_manager

        try:
            await self.cleanup_processing_tasks()
            await self._cleanup_reconnect_tasks()
            await self._cleanup_heartbeat_tasks()
            await self._cleanup_health_tasks()
            await self.resource_manager.cleanup_all()

            try:
                await enhanced_message_consumer_manager.stop_consumer(queue_name)
                self.logger.debug(f"已停止消息消费者: {queue_name}")
            except asyncio.InvalidStateError:
                self.logger.debug(f"消息消费者已在其他事件循环中停止: {queue_name}")
            except Exception as e:
                self.logger.warning(f"停止消息消费者失败: {queue_name}, {e}")

            self.ws = None

        except Exception as e:
            self.logger.error(f"清理资源失败: {e}")


# 延迟导入避免循环依赖
from database import db_manager
from core.connection_status import ConnectionState

__all__ = ['LifecycleMixin']
