"""
消息持久化服务
将实时消息存入 SQLite 数据库，通过 pyqtSignal 线程安全地通知 UI 更新。
"""

from typing import Optional, Dict, Any, List
from datetime import datetime
import uuid

from PyQt6.QtCore import QObject, pyqtSignal
from sqlalchemy.orm import Session
from sqlalchemy import desc, func

from database.models import ChatMessageRecord
from database.db_manager import get_db_manager
from utils.logger_loguru import get_logger

logger = get_logger("MessagePersistence")


class MessagePersistenceSignals(QObject):
    """消息持久化信号 - 线程安全通知 UI"""
    new_message = pyqtSignal(dict)  # 新消息到达，参数: msg_dict


class MessagePersistenceService:
    """消息持久化服务（单例）"""

    _instance: Optional["MessagePersistenceService"] = None
    _MAX_SEEN_IDS = 10000

    def __new__(cls) -> "MessagePersistenceService":
        if cls._instance is None:
            cls._instance = super().__new__(cls)
            cls._instance._initialized = False
        return cls._instance

    def __init__(self):
        if self._initialized:
            return
        self._signals = MessagePersistenceSignals()
        self._seen_msg_ids: set = set()
        self._initialized = True
        logger.info("MessagePersistenceService 初始化完成")

    @property
    def signals(self) -> MessagePersistenceSignals:
        return self._signals

    # ==================== 写入方法 ====================

    def save_inbound_message(self, context) -> Optional[Dict[str, Any]]:
        """保存入站消息（买家消息 + MALL_CS 平台客服消息）

        从 context.kwargs 提取字段，统一处理买家和客服消息。
        - from_role="user" → direction='inbound', reply_source=None
        - from_role="mall_cs" → direction='outbound', reply_source='staff'
        """
        try:
            kwargs = context.kwargs if hasattr(context, 'kwargs') else None
            if kwargs is None:
                logger.debug("save_inbound_message: kwargs is None")
                return None

            msg_id = str(kwargs.msg_id) if kwargs.msg_id else ""
            if not msg_id:
                logger.debug("save_inbound_message: msg_id is empty")
                return None

            # 去重
            if msg_id in self._seen_msg_ids:
                return None
            self._seen_msg_ids.add(msg_id)
            if len(self._seen_msg_ids) > self._MAX_SEEN_IDS:
                # 保留最近的一半
                to_keep = list(self._seen_msg_ids)[self._MAX_SEEN_IDS // 2:]
                self._seen_msg_ids = set(to_keep)

            from_uid = str(kwargs.from_uid) if kwargs.from_uid else ""
            to_uid = str(kwargs.to_uid) if kwargs.to_uid else ""
            from_user = str(kwargs.from_user) if kwargs.from_user else from_uid

            # 从 PinduoduoKwargs 的 raw_data 中提取 from_role 和 to_role
            from_role = "user"
            to_role = "mall_cs"
            raw_data = kwargs.raw_data if hasattr(kwargs, 'raw_data') and kwargs.raw_data else {}
            if isinstance(raw_data, dict):
                from_info = raw_data.get("from", {})
                to_info = raw_data.get("to", {})
                if isinstance(from_info, dict):
                    from_role = str(from_info.get("role", "user"))
                if isinstance(to_info, dict):
                    to_role = str(to_info.get("role", "mall_cs"))

            # 判断方向
            direction = "inbound" if from_role == "user" else "outbound"

            # 计算 buyer_uid（会话分组键）
            if direction == "inbound":
                buyer_uid = from_uid  # 买家发来的消息，from_uid 就是买家
            else:
                buyer_uid = to_uid    # 客服发出的消息，to_uid 是买家

            if not buyer_uid:
                # 兜底：如果 buyer_uid 为空，用 from_uid
                buyer_uid = from_uid

            nickname = str(kwargs.nickname) if kwargs.nickname else from_user
            content = str(context.content) if context.content else ""
            msg_type = str(kwargs.msg_type) if hasattr(kwargs, 'msg_type') and kwargs.msg_type else None
            context_type_str = str(context.type.value) if hasattr(context.type, 'value') else str(context.type)
            shop_id = str(kwargs.shop_id) if kwargs.shop_id else ""
            user_id = str(kwargs.user_id) if kwargs.user_id else ""
            shop_name = str(kwargs.shop_name) if kwargs.shop_name else ""

            # 解析时间戳
            ts_str = str(kwargs.timestamp) if kwargs.timestamp else None
            try:
                if ts_str and ts_str.isdigit():
                    from datetime import timezone, timedelta
                    tz = timezone(timedelta(hours=8))
                    timestamp = datetime.fromtimestamp(int(ts_str) / 1000, tz=tz)
                else:
                    timestamp = datetime.now()
            except (ValueError, OSError):
                timestamp = datetime.now()

            reply_source = "staff" if direction == "outbound" else None

            db_manager = get_db_manager()
            session: Session = db_manager.Session()
            try:
                record = ChatMessageRecord(
                    msg_id=msg_id,
                    shop_id=shop_id,
                    user_id=user_id,
                    shop_name=shop_name,
                    buyer_uid=buyer_uid,
                    from_uid=from_uid,
                    from_role=from_role,
                    to_uid=to_uid,
                    to_role=to_role,
                    nickname=nickname,
                    content=content,
                    msg_type=msg_type,
                    context_type=context_type_str,
                    direction=direction,
                    reply_source=reply_source,
                    timestamp=timestamp,
                    created_at=datetime.now(),
                )
                session.add(record)
                session.commit()

                msg_dict = self._record_to_dict(record)
                logger.debug(f"持久化入站消息: buyer={buyer_uid}, dir={direction}, source={reply_source}")
                return msg_dict
            except Exception as e:
                session.rollback()
                logger.warning(f"写入入站消息失败 (msg_id={msg_id}): {e}")
                return None
            finally:
                session.close()

        except Exception as e:
            logger.warning(f"save_inbound_message 异常: {e}")
            return None

    def save_outbound_message(
        self,
        shop_id: str,
        user_id: str,
        buyer_uid: str,
        reply_content: str,
        reply_source: str,
    ) -> Optional[Dict[str, Any]]:
        """保存出站消息（AI/关键词/兜底/手动 回复）

        Args:
            shop_id: 店铺 ID
            user_id: 登录账号 user_id
            buyer_uid: 买家 UID（也是发送目标 to_uid）
            reply_content: 回复内容
            reply_source: 回复来源 'ai'/'keyword'/'staff'/'fallback'/'manual'
        """
        try:
            # 生成唯一 msg_id
            msg_id = f"{reply_source}_{uuid.uuid4().hex[:16]}"

            if msg_id in self._seen_msg_ids:
                return None
            self._seen_msg_ids.add(msg_id)

            timestamp = datetime.now()
            nickname = "客服" if reply_source in ("manual", "keyword", "fallback") else "AI客服"
            from_role = "mall_cs"
            to_role = "user"

            db_manager = get_db_manager()
            session: Session = db_manager.Session()
            try:
                record = ChatMessageRecord(
                    msg_id=msg_id,
                    shop_id=str(shop_id),
                    user_id=str(user_id),
                    shop_name=None,
                    buyer_uid=str(buyer_uid),
                    from_uid=str(user_id),
                    from_role=from_role,
                    to_uid=str(buyer_uid),
                    to_role=to_role,
                    nickname=nickname,
                    content=reply_content,
                    msg_type=None,
                    context_type="text",
                    direction="outbound",
                    reply_source=reply_source,
                    timestamp=timestamp,
                    created_at=datetime.now(),
                )
                session.add(record)
                session.commit()

                msg_dict = self._record_to_dict(record)
                logger.debug(f"持久化出站消息: buyer={buyer_uid}, source={reply_source}")
                return msg_dict
            except Exception as e:
                session.rollback()
                logger.warning(f"写入出站消息失败 (msg_id={msg_id}): {e}")
                return None
            finally:
                session.close()

        except Exception as e:
            logger.warning(f"save_outbound_message 异常: {e}")
            return None

    def save_manual_reply(
        self, shop_id: str, user_id: str, buyer_uid: str, text: str
    ) -> Optional[Dict[str, Any]]:
        """保存手动回复（ChatUI 输入框发送）"""
        return self.save_outbound_message(
            shop_id=shop_id,
            user_id=user_id,
            buyer_uid=buyer_uid,
            reply_content=text,
            reply_source="manual",
        )

    # ==================== 查询方法 ====================

    def get_conversations(self, shop_id: Optional[str] = None, limit: int = 50) -> List[Dict[str, Any]]:
        """获取会话列表，按 (shop_id, buyer_uid) 分组

        使用窗口函数一次性完成分组+计数，避免 N+1 查询阻塞主线程。

        Args:
            shop_id: 店铺 ID 过滤，None 表示全部
            limit: 最大返回数
        """
        db_manager = get_db_manager()
        session: Session = db_manager.Session()
        try:
            from sqlalchemy import text

            sql = text("""
                SELECT * FROM (
                    SELECT
                        shop_id, buyer_uid, shop_name, nickname, content,
                        direction, timestamp,
                        ROW_NUMBER() OVER (
                            PARTITION BY shop_id, buyer_uid ORDER BY timestamp DESC
                        ) AS rn,
                        COUNT(*) OVER (
                            PARTITION BY shop_id, buyer_uid
                        ) AS msg_count
                    FROM chat_message_records
                    WHERE (:shop_id IS NULL OR shop_id = :shop_id)
                ) sub
                WHERE rn = 1
                ORDER BY timestamp DESC
                LIMIT :limit
            """)

            rows = session.execute(sql, {"shop_id": shop_id, "limit": limit}).fetchall()

            result = []
            for row in rows:
                ts = row.timestamp
                result.append({
                    "shop_id": row.shop_id,
                    "shop_name": row.shop_name or row.shop_id,
                    "buyer_uid": row.buyer_uid,
                    "nickname": row.nickname or row.buyer_uid,
                    "last_content": row.content or "",
                    "last_time": ts.isoformat() if ts else "",
                    "last_direction": row.direction,
                    "msg_count": row.msg_count,
                })
            return result
        except Exception as e:
            logger.error(f"get_conversations 失败: {e}")
            return []
        finally:
            session.close()

    def get_messages_by_uid(self, shop_id: str, buyer_uid: str, limit: int = 100) -> List[Dict[str, Any]]:
        """获取某店铺下某买家的全部消息"""
        db_manager = get_db_manager()
        session: Session = db_manager.Session()
        try:
            rows = (
                session.query(ChatMessageRecord)
                .filter(
                    ChatMessageRecord.shop_id == shop_id,
                    ChatMessageRecord.buyer_uid == buyer_uid,
                )
                .order_by(ChatMessageRecord.timestamp.asc())
                .limit(limit)
                .all()
            )
            return [self._record_to_dict(r) for r in rows]
        except Exception as e:
            logger.error(f"get_messages_by_uid 失败: {e}")
            return []
        finally:
            session.close()

    def search_messages(self, keyword: str, limit: int = 50) -> List[Dict[str, Any]]:
        """搜索消息内容"""
        db_manager = get_db_manager()
        session: Session = db_manager.Session()
        try:
            rows = (
                session.query(ChatMessageRecord)
                .filter(ChatMessageRecord.content.ilike(f"%{keyword}%"))
                .order_by(desc(ChatMessageRecord.timestamp))
                .limit(limit)
                .all()
            )
            return [self._record_to_dict(r) for r in rows]
        except Exception as e:
            logger.error(f"search_messages 失败: {e}")
            return []
        finally:
            session.close()

    # ==================== 工具方法 ====================

    def notify_new_message(self, msg_dict: Dict[str, Any]):
        """通过信号通知 UI 有新消息（线程安全）"""
        try:
            self._signals.new_message.emit(msg_dict)
        except Exception as e:
            logger.warning(f"通知新消息失败: {e}")

    @staticmethod
    def _record_to_dict(record: ChatMessageRecord) -> Dict[str, Any]:
        """将 ORM 对象转为字典"""
        return {
            "id": record.id,
            "msg_id": record.msg_id,
            "shop_id": record.shop_id,
            "user_id": record.user_id,
            "shop_name": record.shop_name,
            "buyer_uid": record.buyer_uid,
            "from_uid": record.from_uid,
            "from_role": record.from_role,
            "to_uid": record.to_uid,
            "to_role": record.to_role,
            "nickname": record.nickname,
            "content": record.content,
            "msg_type": record.msg_type,
            "context_type": record.context_type,
            "direction": record.direction,
            "reply_source": record.reply_source,
            "timestamp": record.timestamp.isoformat() if record.timestamp else "",
            "created_at": record.created_at.isoformat() if record.created_at else "",
        }


# 全局单例
message_persistence_service = MessagePersistenceService()
