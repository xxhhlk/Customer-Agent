"""外发文本消息失败重发队列

为什么需要这个队列
------------------
``send_text`` 走 SOCKS 代理发 HTTP，代理失联时单次请求最坏耗时约 130s
（30s 读超时 × 4 次尝试 + 退避），最终返回 ``None``。此前调用方只记一条
warning 就把这条回复丢弃，买家侧表现为「完全没回复」（2026-11-19 02:52
「人工在吗」：夜间防抖 300s 后才处理，恰好撞上代理失联窗口，关键词回复
发送失败后既无重试也无兜底，整条消息静默丢失）。

设计要点
--------
- **只接管网络层失败**（``send_text`` 返回 ``None``）。业务拒绝（HTTP 200
  但 ``success=false``、``error_code=10002`` 风控）返回 dict，不入队，
  避免重发加剧风控。
- **独立守护线程**轮询，不依赖 asyncio 事件循环 —— 账号 WS 掉线时事件循环
  会被重建，协程任务随之取消，这也是原故障里处理链断掉的原因。
- **重发前探测服务端**：HTTP 超时并不等于没送达，先用 ``chat/list`` 查最近
  消息，同内容已存在就只补落库、不再重发，避免买家收到重复话术。
- 单条最多 ``MAX_ATTEMPTS`` 次、``TTL_SECONDS`` 内有效，超限丢弃并记 ERROR。
"""
from __future__ import annotations

import threading
import time
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional

from utils.logger_loguru import get_logger

logger = get_logger("ReplyRetry")

MAX_ATTEMPTS = 5          # 单条最多重发次数
TTL_SECONDS = 600         # 超过 10 分钟不再重发（话术已过时）
POLL_INTERVAL = 5.0       # 轮询间隔
MAX_PER_TICK = 3          # 每轮最多处理条数，避免一次打太多请求
BACKOFF_BASE = 20.0       # 退避基数：20s、40s、80s…
MAX_PENDING = 50          # 队列上限，防止异常情况内存膨胀


@dataclass
class _PendingReply:
    """一条待重发的外发文本。"""

    shop_id: str
    user_id: str
    recipient_uid: str
    text: str
    reply_source: str
    channel_type: Any = None
    attempts: int = 0
    created_at: float = field(default_factory=time.time)
    next_at: float = field(default_factory=time.time)

    @property
    def key(self) -> tuple:
        return (self.shop_id, self.user_id, self.recipient_uid, self.text)


class ReplyRetryQueue:
    """失败外发文本的重发队列（进程内单例，见文件末尾 ``reply_retry_queue``）"""

    def __init__(self) -> None:
        self._pending: List[_PendingReply] = []
        self._lock = threading.Lock()
        self._thread_lock = threading.Lock()
        self._thread: Optional[threading.Thread] = None
        self._stop = threading.Event()

    # ==================== 对外接口 ====================

    def enqueue(
        self,
        shop_id: Any,
        user_id: Any,
        recipient_uid: Any,
        text: str,
        reply_source: str,
        channel_type: Any = None,
    ) -> bool:
        """把网络层发送失败的文本加入重发队列。

        Args:
            reply_source: 落库用的来源标记（keyword / ai / fallback / order_notify…），
                重发成功后按该值写入会话历史。

        Returns:
            是否成功入队（重复内容、参数缺失、队列已满时返回 False）。
        """
        if not shop_id or not user_id or not recipient_uid or not text:
            return False

        item = _PendingReply(
            shop_id=str(shop_id),
            user_id=str(user_id),
            recipient_uid=str(recipient_uid),
            text=str(text),
            reply_source=reply_source,
            channel_type=channel_type,
        )

        with self._lock:
            if any(p.key == item.key for p in self._pending):
                return False
            if len(self._pending) >= MAX_PENDING:
                logger.warning(
                    f"重发队列已满（{MAX_PENDING}），丢弃: buyer={item.recipient_uid}, "
                    f"source={reply_source}"
                )
                return False
            self._pending.append(item)

        self._ensure_thread()
        logger.warning(
            f"发送失败，已加入重发队列: buyer={item.recipient_uid}, "
            f"source={reply_source}, text={item.text[:40]!r}"
        )
        return True

    def pending_count(self) -> int:
        with self._lock:
            return len(self._pending)

    def stop(self) -> None:
        """停止后台线程（进程退出/测试用）"""
        self._stop.set()
        thread = self._thread
        if thread and thread.is_alive():
            thread.join(timeout=2.0)

    # ==================== 后台线程 ====================

    def _ensure_thread(self) -> None:
        with self._thread_lock:
            if self._thread and self._thread.is_alive():
                return
            self._stop.clear()
            self._thread = threading.Thread(
                target=self._loop, name="reply-retry", daemon=True
            )
            self._thread.start()
            logger.info("重发队列后台线程已启动")

    def _loop(self) -> None:
        while not self._stop.wait(POLL_INTERVAL):
            try:
                self._tick()
            except Exception as e:  # noqa: BLE001 - 后台线程不能因单条异常退出
                logger.error(f"重发队列轮询异常: {type(e).__name__}: {e}")

    def _tick(self) -> None:
        now = time.time()
        with self._lock:
            due = [p for p in self._pending if p.next_at <= now][:MAX_PER_TICK]

        for item in due:
            if now - item.created_at > TTL_SECONDS:
                self._drop(item, f"超过重发有效期 {TTL_SECONDS}s")
                continue
            self._process(item)

    def _process(self, item: _PendingReply) -> None:
        # 1. 先探测：HTTP 超时不代表没送达
        delivered = self._already_delivered(item)
        if delivered is None:
            self._reschedule(item, "服务端探测失败（网络仍不可用）")
            return
        if delivered:
            logger.info(
                f"探测到消息已在服务端，跳过重发只补落库: buyer={item.recipient_uid}, "
                f"source={item.reply_source}"
            )
            self._finish(item, persist=True)
            return

        # 2. 真正重发
        if self._try_send(item):
            logger.info(
                f"重发成功（第 {item.attempts + 1} 次尝试）: buyer={item.recipient_uid}, "
                f"source={item.reply_source}, text={item.text[:40]!r}"
            )
            self._finish(item, persist=True)
        else:
            self._reschedule(item, "重发仍失败")

    def _try_send(self, item: _PendingReply) -> bool:
        try:
            from bridge.sender import get_sender

            sender = get_sender(item.channel_type)
            if not sender:
                return False
            result = sender.send_text(
                item.shop_id, item.user_id, item.recipient_uid, item.text
            )
            return isinstance(result, dict) and bool(result.get("success"))
        except Exception as e:  # noqa: BLE001
            logger.warning(
                f"重发异常: buyer={item.recipient_uid}, {type(e).__name__}: {e}"
            )
            return False

    def _already_delivered(self, item: _PendingReply) -> Optional[bool]:
        """查服务端最近消息，判断该文本是否已投递。

        Returns:
            True 已存在 / False 不存在 / None 探测失败（网络不可用，无法判断）
        """
        try:
            from Channel.pinduoduo.utils.API.get_chat_history import GetChatHistory

            page = GetChatHistory(item.shop_id, item.user_id).fetch_messages(
                item.recipient_uid, size=20
            )
        except Exception as e:  # noqa: BLE001
            logger.warning(
                f"探测历史消息异常: buyer={item.recipient_uid}, {type(e).__name__}: {e}"
            )
            return None

        if page is None:
            return None

        target = item.text.strip()
        earliest = item.created_at - 60  # 容忍发送前的时钟误差
        for raw in page.get("messages") or []:
            if not isinstance(raw, dict):
                continue
            # 方向判定与 history_sync 一致：from 角色为 user 才是买家消息，
            # 这里只认外发（客服侧）消息，避免把买家的同文消息误判成已送达
            from_party = raw.get("from")
            if isinstance(from_party, dict) and from_party.get("role") == "user":
                continue
            if str(raw.get("content") or "").strip() != target:
                continue
            # 服务端下发 ts（秒级），历史字段 time 为毫秒
            ts = self._to_epoch(raw.get("ts") or raw.get("time"))
            if ts is not None and ts >= earliest:
                return True
        return False

    @staticmethod
    def _to_epoch(value: Any) -> Optional[float]:
        """服务端 ts 为秒级 epoch，兼容毫秒。"""
        try:
            ts = float(str(value))
        except (TypeError, ValueError):
            return None
        return ts / 1000.0 if ts >= 100_000_000_000 else ts

    # ==================== 状态迁移 ====================

    def _finish(self, item: _PendingReply, persist: bool = False) -> None:
        with self._lock:
            if item in self._pending:
                self._pending.remove(item)
        if persist:
            self._persist(item)

    def _drop(self, item: _PendingReply, reason: str) -> None:
        with self._lock:
            if item in self._pending:
                self._pending.remove(item)
        logger.error(
            f"放弃重发: buyer={item.recipient_uid}, source={item.reply_source}, "
            f"原因={reason}, 已尝试 {item.attempts} 次, text={item.text[:40]!r}"
        )

    def _reschedule(self, item: _PendingReply, reason: str) -> None:
        item.attempts += 1
        if item.attempts >= MAX_ATTEMPTS:
            self._drop(item, f"{reason}，已达最大尝试次数 {MAX_ATTEMPTS}")
            return
        delay = BACKOFF_BASE * (2 ** (item.attempts - 1))
        item.next_at = time.time() + delay
        logger.warning(
            f"{reason}，{delay:.0f}s 后重试 ({item.attempts}/{MAX_ATTEMPTS}): "
            f"buyer={item.recipient_uid}, source={item.reply_source}"
        )

    def _persist(self, item: _PendingReply) -> None:
        """重发成功/确认已送达后补写会话历史，并通知界面。"""
        try:
            from services.message_persistence import message_persistence_service

            msg_dict = message_persistence_service.save_outbound_message(
                shop_id=item.shop_id,
                user_id=item.user_id,
                buyer_uid=item.recipient_uid,
                reply_content=item.text,
                reply_source=item.reply_source,
            )
            if msg_dict:
                message_persistence_service.notify_new_message(msg_dict)
        except Exception as e:  # noqa: BLE001
            logger.warning(f"重发后持久化失败: {type(e).__name__}: {e}")


# 进程内单例：调用方直接 import 使用，线程按需启动
reply_retry_queue = ReplyRetryQueue()
