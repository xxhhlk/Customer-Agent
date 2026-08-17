"""
消息持久化服务
将实时消息存入 SQLite 数据库，通过 pyqtSignal 线程安全地通知 UI 更新。

**线程安全设计**：
notify_new_message 会被 AutoReplyThread 的 asyncio 协程调用（非主 Qt 线程），
直接 emit pyqtSignal 在特定时序下会导致 C 层 access violation。

解决方案：notify_new_message 只将消息写入线程安全的 deque 缓冲区，
由主线程的 QTimer 定期轮询并批量 emit 信号。
"""

from typing import Optional, Dict, Any, List, Set
from datetime import datetime
import json
import uuid
import threading
from collections import deque

from bridge.context import ContextType

from PyQt6.QtCore import QObject, pyqtSignal, QTimer
from sqlalchemy.orm import Session
from sqlalchemy import desc, func

from database.models import ChatMessageRecord, Shop, Channel
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
        # 跨线程消息通知缓冲区
        self._notify_buffer: deque = deque(maxlen=200)
        self._notify_lock = threading.Lock()
        self._notify_timer: Optional[QTimer] = None
        self._start_notify_timer()
        self._initialized = True
        logger.info("MessagePersistenceService 初始化完成")

    def _start_notify_timer(self):
        """启动主线程定时器，定期从缓冲区取消息并 emit 信号"""
        try:
            self._notify_timer = QTimer(self._signals)
            self._notify_timer.setInterval(50)  # 50ms 轮询
            self._notify_timer.timeout.connect(self._flush_notify_buffer)
            self._notify_timer.start()
        except Exception as e:
            logger.warning(f"启动消息通知定时器失败，将退化为直接 emit: {e}")
            self._notify_timer = None

    def _flush_notify_buffer(self):
        """从缓冲区批量取消息并在主线程 emit"""
        if not self._notify_buffer:
            return
        batch = []
        with self._notify_lock:
            while self._notify_buffer and len(batch) < 20:
                batch.append(self._notify_buffer.popleft())
        for msg_dict in batch:
            try:
                self._signals.new_message.emit(msg_dict)
            except Exception:
                pass  # emit 失败时静默丢弃

    @property
    def signals(self) -> MessagePersistenceSignals:
        return self._signals

    # ==================== 写入方法 ====================

    def save_inbound_message(self, context) -> Optional[Dict[str, Any]]:
        """保存入站消息（买家消息 + MALL_CS 平台客服消息）

        从 context.kwargs 提取字段，统一处理买家和客服消息。
        - from_role="user" → direction='inbound', reply_source=None
        - from_role="mall_cs" → direction='outbound', reply_source='staff'

        不应持久化的系统消息类型（如认证、系统状态、商城系统消息等）会被直接跳过。
        """
        try:
            # 过滤不应在聊天界面展示的系统消息类型
            _SKIP_PERSIST_TYPES: Set[str] = {
                ContextType.AUTH.value,             # 认证消息
                ContextType.SYSTEM_STATUS.value,    # 系统状态（心跳、不支持的消息类型等）
                ContextType.MALL_SYSTEM_MSG.value,  # 商城系统消息（如 {"user_id": "xxx"}）
                ContextType.WITHDRAW.value,         # 撤回消息
                ContextType.TRANSFER.value,         # 转接消息
                ContextType.SYSTEM_HINT.value,      # 系统提示（资金安全提示等）
            }
            context_type_value = context.type.value if hasattr(context.type, 'value') else str(context.type)
            if context_type_value in _SKIP_PERSIST_TYPES:
                logger.debug(f"跳过持久化系统消息: type={context_type_value}")
                return None

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

            # 直接使用 PDDChatMessage 已解析好的 from_user（即 from.role）判断方向
            # 不再从 raw_data 重新解析，因为 raw_data 顶层没有 from/to（它们在 message 子对象下）
            from_role = str(kwargs.from_user) if kwargs.from_user else "user"
            to_role = str(kwargs.to_user) if kwargs.to_user else "mall_cs"

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

            shop_id = str(kwargs.shop_id) if kwargs.shop_id else ""
            user_id = str(kwargs.user_id) if kwargs.user_id else ""
            shop_name = str(kwargs.shop_name) if kwargs.shop_name else ""

            nickname = str(kwargs.nickname) if kwargs.nickname else ""
            # 对于客服消息 (outbound)，nickname 通常为空，从数据库查找该会话的买家昵称
            # 注意：找不到时存空串而非 "客服"，否则当第一条消息是客服发出时，
            # 会把 "客服" 误存为该会话的买家昵称（聊天标题/会话列表/转发列表会错误显示）
            if not nickname:
                if direction == "outbound":
                    # 客服消息：查找同一会话买家消息的昵称
                    buyer_nick = self._get_buyer_nickname(shop_id, buyer_uid)
                    nickname = buyer_nick or ""
                else:
                    # 买家消息：也没有昵称时用 from_uid 兜底
                    nickname = from_uid
            content = str(context.content) if context.content else ""
            msg_type = str(kwargs.msg_type) if hasattr(kwargs, 'msg_type') and kwargs.msg_type else None
            context_type_str = str(context.type.value) if hasattr(context.type, 'value') else str(context.type)

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

            # 当 context_type 为 mall_cs 时，检查 raw_data 中的实际消息类型
            # 人工客服在其他客户端发送的图片/视频消息也会被标记为 mall_cs，
            # 需要还原为 image/video 以便 UI 正确渲染内联预览
            if context_type_str == "mall_cs":
                detected_type = self._detect_media_type_from_raw(context)
                if detected_type:
                    context_type_str = detected_type
                    logger.info(f"[MEDIA_META] mall_cs 消息检测到媒体类型: {detected_type}")

            # 从 raw_data 提取媒体元数据（如视频封面 URL）
            media_meta = self._extract_media_meta(context, context_type_str)

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
                    media_meta=media_meta,
                )
                session.add(record)
                session.commit()

                msg_dict = self._record_to_dict(record)
                if media_meta:
                    logger.info(f"[MEDIA_META] 已存储: context_type={context_type_str}, media_meta={media_meta[:120]}")
                if direction == "outbound" and reply_source == "staff":
                    logger.info(f"持久化客服消息(网页端回复): buyer={buyer_uid}, shop={shop_id}, content={content[:50]}")
                else:
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

    def _detect_media_type_from_raw(self, context) -> Optional[str]:
        """从 raw_data 中检测实际消息类型（图片/视频）

        当 context_type 被标记为 mall_cs 时，人工客服可能发送了图片或视频，
        需要从 raw_data.message.type 中检测实际类型。
        """
        try:
            raw_data = context.kwargs.raw_data if hasattr(context, 'kwargs') and context.kwargs else None
            if not raw_data or not isinstance(raw_data, dict):
                return None

            msg_data = raw_data.get("message", raw_data)
            if not isinstance(msg_data, dict):
                return None

            msg_type = msg_data.get("type")
            # PDDMsgType: IMAGE=1, VIDEO=14
            if msg_type == 1:
                return "image"
            elif msg_type == 14:
                return "video"
            return None
        except Exception as e:
            logger.debug(f"_detect_media_type_from_raw 异常: {e}")
            return None

    def _extract_media_meta(self, context, context_type_str: str) -> Optional[str]:
        """从 context.kwargs.raw_data 提取媒体元数据"""
        try:
            if context_type_str not in ("video", "image"):
                return None

            raw_data = context.kwargs.raw_data if hasattr(context, 'kwargs') and context.kwargs else None
            if not raw_data or not isinstance(raw_data, dict):
                logger.info(f"[MEDIA_META] raw_data缺失或非dict, type={type(raw_data).__name__}, context_type={context_type_str}")
                return None

            # raw_data 是完整的 WebSocket 消息 JSON，message 数据在 "message" 键下
            msg_data = raw_data.get("message", raw_data)
            info = msg_data.get("info")
            if not info or not isinstance(info, dict):
                logger.info(f"[MEDIA_META] info缺失, raw_keys={list(raw_data.keys())[:8]}, msg_keys={list(msg_data.keys())[:8] if isinstance(msg_data, dict) else 'not_dict'}, context_type={context_type_str}")
                return None

            meta: Dict[str, Any] = {}

            if context_type_str == "video":
                # 保存完整 raw_info，转发时需要原样发送给 PDD
                # PDD 对 info 字段有严格校验，缺 download_url/file_id/size/status 会返回 param error
                meta["raw_info"] = info
                preview = info.get("preview")
                if preview and isinstance(preview, dict):
                    cover_url = preview.get("url")
                    if cover_url:
                        meta["cover_url"] = cover_url
                    preview_size = preview.get("size")
                    if preview_size:
                        meta["cover_size"] = preview_size
                duration = info.get("duration")
                if duration is not None:
                    meta["duration"] = duration
                logger.info(f"[MEDIA_META] video: cover_url={'YES' if meta.get('cover_url') else 'NO'}, duration={meta.get('duration')}, has_raw_info=True")
            elif context_type_str == "image":
                w = info.get("width")
                h = info.get("height")
                if w and h:
                    meta["width"] = w
                    meta["height"] = h
                img_size = info.get("image_size")
                if img_size is not None:
                    meta["file_size"] = img_size
                logger.info(f"[MEDIA_META] image: {w}x{h}")

            if meta:
                return json.dumps(meta, ensure_ascii=False)
            return None
        except Exception as e:
            logger.debug(f"_extract_media_meta异常: {e}")

    def _get_buyer_nickname(self, shop_id: str, buyer_uid: str) -> Optional[str]:
        """从数据库查找该会话买家消息的昵称（最近一条买家消息）"""
        try:
            db_manager = get_db_manager()
            session: Session = db_manager.Session()
            try:
                from sqlalchemy import text
                sql = text("""
                    SELECT nickname FROM chat_message_records
                    WHERE shop_id = :shop_id AND buyer_uid = :buyer_uid
                      AND direction = 'inbound' AND nickname IS NOT NULL AND nickname != ''
                      AND nickname != 'user' AND nickname != 'mall_cs'
                    ORDER BY timestamp DESC LIMIT 1
                """)
                row = session.execute(sql, {"shop_id": shop_id, "buyer_uid": buyer_uid}).fetchone()
                return row.nickname if row else None
            finally:
                session.close()
        except Exception as e:
            logger.warning(f"_get_buyer_nickname 异常: {e}")
            return None

    def save_outbound_message(
        self,
        shop_id: str,
        user_id: str,
        buyer_uid: str,
        reply_content: str,
        reply_source: str,
        context_type: str = "text",
        media_meta: Optional[str] = None,
    ) -> Optional[Dict[str, Any]]:
        """保存出站消息（AI/关键词/兜底/手动 回复）

        Args:
            shop_id: 店铺 ID
            user_id: 登录账号 user_id
            buyer_uid: 买家 UID（也是发送目标 to_uid）
            reply_content: 回复内容
            reply_source: 回复来源 'ai'/'keyword'/'staff'/'fallback'/'manual'
            context_type: 消息类型 'text'/'image'/'video'/'goods_card' 等
            media_meta: 媒体元数据 JSON 字符串（视频/图片消息的封面/时长等）
        """
        try:
            # 生成唯一 msg_id
            msg_id = f"{reply_source}_{uuid.uuid4().hex[:16]}"

            if msg_id in self._seen_msg_ids:
                return None
            self._seen_msg_ids.add(msg_id)

            timestamp = datetime.now()
            # 出站消息不存发送者昵称：该字段会被聊天标题/会话列表当作"买家昵称"消费，
            # 误存 "客服"/"AI客服" 会导致界面把角色名当买家昵称显示；
            # 发送者身份由 direction + reply_source（气泡来源标签）表达，无需 nickname。
            nickname = ""
            from_role = "mall_cs"
            to_role = "user"

            # 查询店铺名称（避免依赖调用方传入，统一在持久化层处理）
            shop_name = None
            try:
                db_manager = get_db_manager()
                session: Session = db_manager.Session()
                try:
                    shop_row = session.query(Shop.shop_name).join(
                        Channel, Channel.id == Shop.channel_id
                    ).filter(
                        Channel.channel_name == "pinduoduo",
                        Shop.shop_id == str(shop_id),
                    ).first()
                    if shop_row:
                        shop_name = shop_row.shop_name
                finally:
                    session.close()
            except Exception:
                pass

            db_manager = get_db_manager()
            session: Session = db_manager.Session()
            try:
                record = ChatMessageRecord(
                    msg_id=msg_id,
                    shop_id=str(shop_id),
                    user_id=str(user_id),
                    shop_name=shop_name,
                    buyer_uid=str(buyer_uid),
                    from_uid=str(user_id),
                    from_role=from_role,
                    to_uid=str(buyer_uid),
                    to_role=to_role,
                    nickname=nickname,
                    content=reply_content,
                    msg_type=None,
                    context_type=context_type,
                    direction="outbound",
                    reply_source=reply_source,
                    timestamp=timestamp,
                    created_at=datetime.now(),
                    media_meta=media_meta,
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
                WITH buyer_nicks AS (
                    SELECT shop_id, buyer_uid, nickname
                    FROM (
                        SELECT shop_id, buyer_uid, nickname,
                               ROW_NUMBER() OVER (
                                   PARTITION BY shop_id, buyer_uid ORDER BY timestamp DESC
                               ) AS rn
                        FROM chat_message_records
                        WHERE direction = 'inbound'
                          AND nickname IS NOT NULL AND nickname != ''
                          AND nickname NOT IN ('user', 'mall_cs', '客服', 'AI客服')
                    ) sub
                    WHERE rn = 1
                ),
                shop_names AS (
                    SELECT s.shop_id, s.shop_name
                    FROM shops s
                    JOIN channels ch ON ch.id = s.channel_id
                    WHERE ch.channel_name = 'pinduoduo'
                )
                SELECT * FROM (
                    SELECT
                        c.shop_id, c.buyer_uid, c.shop_name, c.nickname, c.content,
                        c.direction, c.timestamp,
                        ROW_NUMBER() OVER (
                            PARTITION BY c.shop_id, c.buyer_uid ORDER BY c.timestamp DESC
                        ) AS rn,
                        COUNT(*) OVER (
                            PARTITION BY c.shop_id, c.buyer_uid
                        ) AS msg_count,
                        bn.nickname AS buyer_nickname,
                        sn.shop_name AS shop_name_reliable
                    FROM chat_message_records c
                    LEFT JOIN buyer_nicks bn ON bn.shop_id = c.shop_id AND bn.buyer_uid = c.buyer_uid
                    LEFT JOIN shop_names sn ON sn.shop_id = c.shop_id
                    WHERE (:shop_id IS NULL OR c.shop_id = :shop_id)
                ) sub2
                WHERE rn = 1
                ORDER BY timestamp DESC
                LIMIT :limit
            """)

            rows = session.execute(sql, {"shop_id": shop_id, "limit": limit}).fetchall()

            result = []
            for row in rows:
                ts = row.timestamp
                # raw SQL 返回的 timestamp 可能是 str 而非 datetime，需要兼容
                if ts and isinstance(ts, str):
                    last_time = ts  # 已经是 ISO 格式字符串
                elif ts and hasattr(ts, "isoformat"):
                    last_time = ts.isoformat()
                else:
                    last_time = ""
                # 兜底昵称过滤角色名（历史脏数据可能把 "客服"/"AI客服" 存进记录）
                nick = row.nickname or ""
                if nick in ("客服", "AI客服", "mall_cs", "user"):
                    nick = ""
                result.append({
                    "shop_id": row.shop_id,
                    "shop_name": row.shop_name_reliable or row.shop_name or row.shop_id,
                    "buyer_uid": row.buyer_uid,
                    "nickname": row.buyer_nickname or nick or row.buyer_uid,
                    "last_content": row.content or "",
                    "last_time": last_time,
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
        """通过信号通知 UI 有新消息（线程安全）

        从任意线程调用都是安全的：消息先写入线程安全缓冲区，
        由主线程 QTimer 定期批量 emit 信号。
        """
        try:
            if self._notify_timer is not None:
                # 使用缓冲区模式（推荐）
                with self._notify_lock:
                    self._notify_buffer.append(msg_dict)
            else:
                # 定时器不可用时退化为直接 emit
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
            "media_meta": record.media_meta,
            "timestamp": record.timestamp.isoformat() if record.timestamp else "",
            "created_at": record.created_at.isoformat() if record.created_at else "",
        }


# 全局单例
message_persistence_service = MessagePersistenceService()
