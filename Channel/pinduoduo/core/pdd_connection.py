# 连接管理模块
import asyncio
import threading
import websockets
from websockets import exceptions as ws_exceptions
from typing import Optional, Any, TYPE_CHECKING
from utils.logger_loguru import get_logger

if TYPE_CHECKING:
    from core.connection_status import ConnectionStatusManager
    from Channel.pinduoduo.core.pdd_config import ReconnectConfig


class ConnectionMixin:
    """连接管理 Mixin"""

    # Attributes provided by PDDChannel host class
    base_url: str = "wss://m-ws.pinduoduo.com/"
    logger: Any
    status_manager: "ConnectionStatusManager"
    reconnect_config: "ReconnectConfig"
    _stop_event: Optional[asyncio.Event]
    _threading_stop_event: threading.Event

    # NOTE: init / _setup_message_consumer / _process_websocket_message
    # 实现在 LifecycleMixin / MessageHandlerMixin 中，不在此声明 stub，
    # 否则 MRO 会优先匹配 ConnectionMixin 的 stub 而非真正的实现。

    async def _connect_with_retry(self, shop_id: str, user_id: str, username: str, on_success, on_failure):
        """带重连机制的WebSocket连接"""
        logger = get_logger("PDDChannel")
        logger.info(f"_connect_with_retry 开始: {shop_id}-{username}, max_attempts={self.reconnect_config.max_attempts}")

        for attempt in range(self.reconnect_config.max_attempts):
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

            except Exception as e:
                # 检查是否是因为停止事件导致的异常
                if (self._stop_event and self._stop_event.is_set()) or self._threading_stop_event.is_set():
                    logger.info(f"连接被停止信号中断: {shop_id}-{username}")
                    self.status_manager.update_status(shop_id, user_id, username, ConnectionState.DISCONNECTED)
                    return

                if attempt == self.reconnect_config.max_attempts - 1:
                    self.status_manager.update_status(shop_id, user_id, username, ConnectionState.ERROR, str(e))
                    logger.error(f"连接失败，已达到最大重试次数 ({self.reconnect_config.max_attempts}): {shop_id}-{username}, 错误: {str(e)}")
                    on_failure(f"连接失败，已达到最大重试次数: {e}")
                    return

                # 计算重连延迟（指数退避）
                delay = min(
                    self.reconnect_config.initial_delay * (self.reconnect_config.backoff_factor ** attempt),
                    self.reconnect_config.max_delay
                )

                logger.warning(f"连接失败，{delay:.1f}秒后重试 ({attempt + 1}/{self.reconnect_config.max_attempts}): {shop_id}-{username}, 错误: {str(e)}")

                # 可中断的延迟等待
                try:
                    # 检查停止事件，避免事件循环关闭时的异步调用问题
                    for _ in range(int(delay * 10)):  # 每0.1秒检查一次
                        if (self._stop_event and self._stop_event.is_set()) or self._threading_stop_event.is_set():
                            logger.info(f"重连延迟被停止信号中断: {shop_id}-{username}")
                            self.status_manager.update_status(shop_id, user_id, username, ConnectionState.DISCONNECTED)
                            return
                        await asyncio.sleep(0.1)  # 短暂睡眠，可以快速响应
                except (asyncio.CancelledError, RuntimeError):
                    # 处理事件循环关闭的情况
                    logger.info(f"重连延迟被中断或事件循环关闭: {shop_id}-{username}")
                    self.status_manager.update_status(shop_id, user_id, username, ConnectionState.DISCONNECTED)
                    return

    async def _connect_single_attempt(self, shop_id: str, user_id: str, username: str, on_success, on_failure):
        """单次WebSocket连接尝试"""
        await self.init(shop_id, user_id, username, on_success, on_failure)

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
__all__ = ['ConnectionMixin']
