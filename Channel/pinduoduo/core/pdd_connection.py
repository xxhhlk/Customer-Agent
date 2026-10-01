# 连接管理模块
import asyncio
import time
import threading
import websockets
from websockets import exceptions as ws_exceptions
from typing import Optional, Any, TYPE_CHECKING
from utils.logger_loguru import get_logger

if TYPE_CHECKING:
    from core.connection_status import ConnectionStatusManager
    from Channel.pinduoduo.core.pdd_config import ReconnectConfig


class ConnectionLostError(Exception):
    """WebSocket 曾成功建立后掉线（不等同于连接建立失败，不计入重试预算）"""


class ConnectionMixin:
    """连接管理 Mixin"""

    # Attributes provided by PDDChannel host class
    base_url: str = "wss://m-ws.pinduoduo.com/"
    logger: Any
    status_manager: "ConnectionStatusManager"
    reconnect_config: "ReconnectConfig"
    _stop_event: Optional[asyncio.Event]
    _threading_stop_event: threading.Event
    _ws_connected_at: Optional[float] = None  # 最近一次 WebSocket 连接建立时刻（monotonic）

    # 连接存活时长达到该阈值视为"稳定建立"：其后的掉线重置重试预算，不计入连续失败
    _CONNECT_ALIVE_THRESHOLD: float = 60.0

    # NOTE: init / _setup_message_consumer / _process_websocket_message
    # 实现在 LifecycleMixin / MessageHandlerMixin 中，不在此声明 stub，
    # 否则 MRO 会优先匹配 ConnectionMixin 的 stub 而非真正的实现。

    async def _connect_with_retry(self, shop_id: str, user_id: str, username: str, on_success, on_failure):
        """带重连机制的WebSocket连接

        重试预算只针对"连续建立连接失败"：连接成功建立并稳定存活
        (>= _CONNECT_ALIVE_THRESHOLD) 后掉线时重置预算与退避；
        秒断(存活 < 阈值)或建连失败仍计入连续失败。
        """
        logger = get_logger("PDDChannel")
        logger.info(f"_connect_with_retry 开始: {shop_id}-{username}, max_attempts={self.reconnect_config.max_attempts}")

        attempt = 0  # 连续连接失败次数
        while attempt < self.reconnect_config.max_attempts:
            # 检查是否收到停止信号（同时检查asyncio.Event和threading.Event）
            if (self._stop_event and self._stop_event.is_set()) or self._threading_stop_event.is_set():
                logger.info(f"收到停止信号，取消重连: {shop_id}-{username} (stop_event={self._stop_event.is_set() if self._stop_event else 'None'}, threading_stop={self._threading_stop_event.is_set()})")
                self.status_manager.update_status(shop_id, user_id, username, ConnectionState.DISCONNECTED)
                return

            try:
                if attempt > 0:
                    self.status_manager.update_status(shop_id, user_id, username, ConnectionState.RECONNECTING)
                    logger.info(f"尝试重连 ({attempt + 1}/{self.reconnect_config.max_attempts}): {shop_id}-{username}")

                logger.info(f"_connect_with_retry: 调用 _connect_single_attempt (attempt {attempt}): {shop_id}-{username}")
                await self._connect_single_attempt(shop_id, user_id, username, on_success, on_failure)
                logger.info(f"_connect_with_retry: _connect_single_attempt 正常返回: {shop_id}-{username}")
                return  # 连接成功，退出重试循环

            except ConnectionLostError as e:
                # 连接曾成功建立后掉线
                alive = self._ws_alive_seconds()
                if alive >= self._CONNECT_ALIVE_THRESHOLD:
                    # 稳定存活过：重置重试预算，退避从初始延迟重新开始
                    if attempt > 0:
                        logger.info(f"连接稳定后掉线(存活 {alive:.0f}s)，重置重试计数: {shop_id}-{username}")
                    attempt = 0
                    delay = self.reconnect_config.initial_delay
                    self.status_manager.update_status(shop_id, user_id, username, ConnectionState.RECONNECTING)
                    logger.warning(f"连接掉线(存活 {alive:.0f}s)，{delay:.1f}秒后重连: {shop_id}-{username}, 错误: {str(e)}")
                else:
                    # 秒断（连上即断）：视为连续失败，正常退避
                    attempt += 1
                    if attempt >= self.reconnect_config.max_attempts:
                        self.status_manager.update_status(shop_id, user_id, username, ConnectionState.ERROR, str(e))
                        logger.error(f"连接失败，已达到最大重试次数 ({self.reconnect_config.max_attempts}): {shop_id}-{username}, 错误: {str(e)}")
                        on_failure(f"连接失败，已达到最大重试次数: {e}")
                        return
                    delay = min(
                        self.reconnect_config.initial_delay * (self.reconnect_config.backoff_factor ** (attempt - 1)),
                        self.reconnect_config.max_delay
                    )
                    logger.warning(f"连接秒断(存活 {alive:.0f}s < {self._CONNECT_ALIVE_THRESHOLD:.0f}s)，{delay:.1f}秒后重试 ({attempt}/{self.reconnect_config.max_attempts}): {shop_id}-{username}, 错误: {str(e)}")

            except Exception as e:
                # 检查是否是因为停止事件导致的异常
                if (self._stop_event and self._stop_event.is_set()) or self._threading_stop_event.is_set():
                    logger.info(f"连接被停止信号中断: {shop_id}-{username}")
                    self.status_manager.update_status(shop_id, user_id, username, ConnectionState.DISCONNECTED)
                    return

                attempt += 1
                if attempt >= self.reconnect_config.max_attempts:
                    self.status_manager.update_status(shop_id, user_id, username, ConnectionState.ERROR, str(e))
                    logger.error(f"连接失败，已达到最大重试次数 ({self.reconnect_config.max_attempts}): {shop_id}-{username}, 错误: {str(e)}")
                    on_failure(f"连接失败，已达到最大重试次数: {e}")
                    return

                # 计算重连延迟（指数退避）
                delay = min(
                    self.reconnect_config.initial_delay * (self.reconnect_config.backoff_factor ** (attempt - 1)),
                    self.reconnect_config.max_delay
                )

                logger.warning(f"连接失败，{delay:.1f}秒后重试 ({attempt}/{self.reconnect_config.max_attempts}): {shop_id}-{username}, 错误: {str(e)}")

            # 可中断的延迟等待
            if not await self._delay_before_next_attempt(delay, shop_id, user_id, username):
                return

    async def _delay_before_next_attempt(self, delay: float, shop_id: str, user_id: str, username: str) -> bool:
        """可中断的重连延迟；返回 False 表示收到停止信号，应终止重连"""
        try:
            for _ in range(int(delay * 10)):  # 每0.1秒检查一次
                if (self._stop_event and self._stop_event.is_set()) or self._threading_stop_event.is_set():
                    self.logger.info(f"重连延迟被停止信号中断: {shop_id}-{username}")
                    self.status_manager.update_status(shop_id, user_id, username, ConnectionState.DISCONNECTED)
                    return False
                await asyncio.sleep(0.1)  # 短暂睡眠，可以快速响应
        except (asyncio.CancelledError, RuntimeError):
            # 处理事件循环关闭的情况
            self.logger.info(f"重连延迟被中断或事件循环关闭: {shop_id}-{username}")
            self.status_manager.update_status(shop_id, user_id, username, ConnectionState.DISCONNECTED)
            return False
        return True

    def _ws_alive_seconds(self) -> float:
        """最近一条连接自建立以来的存活时长（秒）；无记录时返回 0"""
        if not self._ws_connected_at:
            return 0.0
        return time.monotonic() - self._ws_connected_at

    async def _connect_single_attempt(self, shop_id: str, user_id: str, username: str, on_success, on_failure):
        """单次WebSocket连接尝试"""
        await self.init(shop_id, user_id, username, on_success, on_failure)  # type: ignore[attr-defined]

    def _is_ws_closed(self, ws: Any) -> bool:
        """检查WebSocket是否已关闭"""
        try:
            closed = getattr(ws, "closed", None)
            if isinstance(closed, bool):
                return closed
            return False
        except Exception:
            return False

    async def _safe_close_websocket(self, ws: Any):
        """安全关闭WebSocket"""
        try:
            close_fn = getattr(ws, "close", None)
            if close_fn:
                result = close_fn()
                if asyncio.iscoroutine(result):
                    await result
        except Exception as e:
            self.logger.debug(f"关闭WebSocket失败: {e}")


# 延迟导入避免循环依赖
from core.connection_status import ConnectionState
__all__ = ['ConnectionMixin', 'ConnectionLostError']
