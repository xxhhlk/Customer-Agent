"""上线历史消息同步

账号连接建立后，从拼多多商家后台补拉最近会话的历史聊天记录，
按 msg_id 去重落库，补齐本地聊天记录在重启、换机后缺失的部分。
"""

import asyncio
import json
import time
from typing import Any, Dict, List, Optional, Set

from bridge.context import ChannelType, Context
from Channel.pinduoduo.pdd_message import PDDChatMessage
from Channel.pinduoduo.utils.API.get_chat_history import GetChatHistory
from database.db_manager import get_db_manager
from database.models import ChatMessageRecord, Shop, Channel
from services.message_persistence import message_persistence_service
from utils.logger_loguru import get_logger

logger = get_logger("HistorySync")

# 单次上线最多补拉的会话数（会话列表按最近活跃排序）
MAX_CONVERSATIONS = 50
# 单会话最多翻页数
MAX_PAGES_PER_CONVERSATION = 2
# 单页条数
PAGE_SIZE = 50
# 同一账号两次同步的最小间隔（秒），避免重连时重复拉取
SYNC_INTERVAL_SECONDS = 600
# 连接建立后的等待时间（秒），先让实时消息流稳定
START_DELAY_SECONDS = 5


class HistorySyncService:
    """历史消息同步服务"""

    def __init__(self) -> None:
        self._last_sync: Dict[str, float] = {}
        self._running: Set[str] = set()

    async def sync_on_start(self, shop_id: str, user_id: str, username: str = "") -> int:
        """上线后补拉历史消息

        Returns:
            本次新增的消息条数；命中节流或同步失败时返回 0。
        """
        key = f"{shop_id}_{user_id}"
        if key in self._running:
            return 0
        if time.monotonic() - self._last_sync.get(key, 0.0) < SYNC_INTERVAL_SECONDS:
            return 0

        self._running.add(key)
        try:
            await asyncio.sleep(START_DELAY_SECONDS)
            added = await asyncio.to_thread(self._sync, shop_id, user_id, username)
            self._last_sync[key] = time.monotonic()
            return added
        except Exception as e:
            logger.warning(f"历史消息同步失败: {shop_id}-{username}, {e}")
            return 0
        finally:
            self._running.discard(key)

    def _sync(self, shop_id: str, user_id: str, username: str) -> int:
        """拉取会话列表并逐会话补拉历史（同步执行，由调用方置于工作线程）"""
        client = GetChatHistory(shop_id, user_id)
        conv_result = client.fetch_conversations(page=1, size=MAX_CONVERSATIONS)
        if not conv_result.get("success"):
            logger.warning(f"历史消息同步跳过，会话列表不可用: {shop_id}-{username}")
            return 0

        conversations = conv_result.get("conversations") or []
        if not conversations:
            return 0

        logger.info(f"历史消息同步开始: {username}, 会话数={len(conversations)}")
        shop_name = self._get_shop_name(shop_id)

        total = 0
        for conversation in conversations:
            buyer_uid = conversation.get("buyer_uid")
            if not buyer_uid:
                continue

            existing = self._load_existing_msg_ids(shop_id, buyer_uid)
            last_msg_id = str(conversation.get("last_msg_id") or "")
            # 本地已含该会话最新一条消息，说明无新增，无需再占用接口
            if existing and last_msg_id and last_msg_id in existing:
                continue

            messages = client.fetch_history(
                buyer_uid, max_pages=MAX_PAGES_PER_CONVERSATION, page_size=PAGE_SIZE
            )
            if not messages:
                continue
            total += self._persist(
                shop_id,
                user_id,
                buyer_uid,
                conversation.get("nickname") or "",
                shop_name,
                messages,
                existing,
            )

        logger.info(f"历史消息同步完成: {username}, 新增 {total} 条")
        return total

    def _persist(
        self,
        shop_id: str,
        user_id: str,
        buyer_uid: str,
        nickname: str,
        shop_name: str,
        messages: List[Dict[str, Any]],
        existing: Set[str],
    ) -> int:
        """将历史消息落库，返回新增条数"""
        added = 0

        for raw in messages:
            msg_id = str(raw.get("msg_id") or "")
            if not msg_id or msg_id in existing:
                continue
            context = self._build_context(raw, shop_id, user_id, shop_name, nickname)
            if context is None:
                continue
            existing.add(msg_id)
            # 不触发新消息通知：历史回填不应产生未读提示
            if message_persistence_service.save_inbound_message(context):
                added += 1

        return added

    @staticmethod
    def _build_context(
        raw: Dict[str, Any],
        shop_id: str,
        user_id: str,
        shop_name: str,
        nickname: str,
    ) -> Optional[Context]:
        """把原始历史消息还原为 Context

        ``chat/list`` 返回的单条消息字段平铺在顶层且不带 response 字段，
        补上 ``response="push"`` 后即可复用推送消息的解析与落库口径。
        """
        try:
            wrapped = {"response": "push", "message": raw}
            pdd_message = PDDChatMessage(wrapped)
        except Exception as e:
            logger.debug(f"解析历史消息失败: {e}")
            return None

        content = pdd_message.content
        if isinstance(content, dict):
            content = json.dumps(content, ensure_ascii=False)
        elif content is None:
            content = ""
        else:
            content = str(content)

        # 昵称兜底用会话级买家昵称：客服侧消息自身不带昵称，
        # 传空会让持久化层逐条回查数据库。
        resolved_nickname = pdd_message.nickname or nickname or ""

        return Context.create_pinduoduo_context(
            content=content,
            msg_id=str(pdd_message.msg_id) if pdd_message.msg_id is not None else "",
            from_user=str(pdd_message.from_user) if pdd_message.from_user is not None else "",
            from_uid=str(pdd_message.from_uid) if pdd_message.from_uid is not None else "",
            to_user=str(pdd_message.to_user) if pdd_message.to_user is not None else "",
            to_uid=str(pdd_message.to_uid) if pdd_message.to_uid is not None else "",
            nickname=str(resolved_nickname),
            timestamp=str(pdd_message.timestamp) if pdd_message.timestamp is not None else None,
            user_msg_type=pdd_message.user_msg_type,
            shop_id=str(shop_id),
            user_id=str(user_id),
            username="",
            shop_name=str(shop_name),
            raw_data=pdd_message.raw_data,
            channel_type=ChannelType.PINDUODUO,
            origin=getattr(getattr(pdd_message, "origin", None), "value", None),
            action=getattr(getattr(pdd_message, "action", None), "value", None),
            pdd_type=getattr(pdd_message, "pdd_type", None),
            pdd_sub_type=getattr(pdd_message, "pdd_sub_type", None),
            template_name=getattr(pdd_message, "template_name", None),
        )

    @staticmethod
    def _load_existing_msg_ids(shop_id: str, buyer_uid: str) -> Set[str]:
        """取该会话已入库的 msg_id 集合，用于历史回填去重"""
        db_manager = get_db_manager()
        session = db_manager.Session()
        try:
            rows = (
                session.query(ChatMessageRecord.msg_id)
                .filter(
                    ChatMessageRecord.shop_id == str(shop_id),
                    ChatMessageRecord.buyer_uid == str(buyer_uid),
                )
                .all()
            )
            return {str(row[0]) for row in rows if row[0]}
        except Exception as e:
            logger.debug(f"读取已入库消息ID失败: {e}")
            return set()
        finally:
            session.close()

    @staticmethod
    def _get_shop_name(shop_id: str) -> str:
        """查询店铺名称"""
        db_manager = get_db_manager()
        session = db_manager.Session()
        try:
            row = (
                session.query(Shop.shop_name)
                .join(Channel, Channel.id == Shop.channel_id)
                .filter(
                    Channel.channel_name == "pinduoduo",
                    Shop.shop_id == str(shop_id),
                )
                .first()
            )
            return row.shop_name if row else ""
        except Exception:
            return ""
        finally:
            session.close()


# 全局单例
history_sync_service = HistorySyncService()
