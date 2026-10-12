"""
素材发送自回显登记表

PDD 服务端在 ``library_file/send`` 投递成功后，会向本账号的 WebSocket
推送一条同内容的 ``from=mall_cs`` 消息（自回显）。发送侧在本地已落库
一条 ``manual`` 记录，若回显再落一条 ``staff`` 记录，同一条素材会在
聊天界面出现两个气泡。

用法：
- 发送方在调用发送接口前 ``register(shop_id, buyer_uid, content)``，
  发送失败时 ``revoke(...)`` 撤销登记；
- WS 消息处理层在落库前 ``consume(shop_id, buyer_uid, content)``，
  命中即丢弃该条回显。

``content`` 为入库口径的消息内容（视频 = 转码地址 ``xxx.f30.mp4``），
与 WS 回显消息的 ``content`` 字段完全一致。
"""

import threading
import time
from typing import Dict, List, Tuple

from utils.logger_loguru import get_logger

logger = get_logger(__name__)

# 回显窗口（秒）：与 services.history_sync.SELF_ECHO_WINDOW_SECONDS 取齐，
# 超过窗口的登记条目视为失效
ECHO_TTL_SECONDS = 120.0
# 登记表容量兜底上限，防止异常情况下无限增长
MAX_ENTRIES = 500


class SelfEchoRegistry:
    """按 (shop_id, buyer_uid, content) 登记 / 消耗自回显指纹（线程安全）

    发送方运行于 QThread，消费方运行于 asyncio 消息循环线程，读写均加锁。
    同一指纹可登记多条（连续发送同一素材），回显逐条消耗。
    """

    def __init__(self):
        self._entries: Dict[Tuple[str, str, str], List[float]] = {}
        self._lock = threading.Lock()

    @staticmethod
    def _key(shop_id, buyer_uid, content) -> Tuple[str, str, str]:
        return (str(shop_id), str(buyer_uid), str(content))

    def register(self, shop_id, buyer_uid, content) -> bool:
        """登记一条「即将发送」的素材消息；参数为空时不登记并返回 False"""
        if not content or not buyer_uid:
            return False
        key = self._key(shop_id, buyer_uid, content)
        now = time.monotonic()
        with self._lock:
            self._prune(now)
            self._entries.setdefault(key, []).append(now + ECHO_TTL_SECONDS)
        return True

    def revoke(self, shop_id, buyer_uid, content) -> None:
        """发送失败时撤销最近一次登记（避免残留登记误吞后续真实消息）"""
        key = self._key(shop_id, buyer_uid, content)
        now = time.monotonic()
        with self._lock:
            self._prune(now)
            slots = self._entries.get(key)
            if slots:
                slots.pop()
                if not slots:
                    self._entries.pop(key, None)

    def consume(self, shop_id, buyer_uid, content) -> bool:
        """命中一条未过期的登记则消耗之并返回 True"""
        key = self._key(shop_id, buyer_uid, content)
        now = time.monotonic()
        with self._lock:
            self._prune(now)
            slots = self._entries.get(key)
            if not slots:
                return False
            slots.pop()
            if not slots:
                self._entries.pop(key, None)
        return True

    def _prune(self, now: float) -> None:
        """丢弃全部过期条目（须在持锁状态下调用）"""
        for key in list(self._entries.keys()):
            alive = [t for t in self._entries[key] if t > now]
            if alive:
                self._entries[key] = alive
            else:
                del self._entries[key]
        if len(self._entries) > MAX_ENTRIES:
            # 异常增长兜底：整体清空。登记表只影响回显去重，
            # 清空只会退化为「允许回显落库」，不会丢真实消息
            logger.warning("自回显登记表超限，已清空兜底")
            self._entries.clear()


# 全局单例
self_echo_registry = SelfEchoRegistry()

__all__ = ["SelfEchoRegistry", "self_echo_registry", "ECHO_TTL_SECONDS"]
