"""
聊天记录页面 - 主界面
双栏布局：左侧会话列表 + 右侧聊天区域，顶部店铺筛选
"""

from PyQt6.QtCore import Qt, pyqtSignal, QEvent, QTimer, QThread
from PyQt6.QtWidgets import (
    QFrame, QVBoxLayout, QHBoxLayout, QLabel, QSplitter, QComboBox,
    QSizePolicy, QWidget,
)
from PyQt6.QtGui import QFont
from qfluentwidgets import BodyLabel, isDarkTheme

from ui.chat.conversation_list import ConversationListPanel
from ui.chat.chat_area import ChatAreaPanel
from utils.logger_loguru import get_logger

logger = get_logger("ChatUI")


class _ShopLoader(QThread):
    """后台线程加载店铺列表"""
    result = pyqtSignal(list)  # shops

    def run(self):
        try:
            from database.db_manager import get_db_manager
            db = get_db_manager()
            shops = db.get_all_shops()
        except Exception:
            shops = []
        self.result.emit(shops)


class _ConversationLoader(QThread):
    """后台线程加载会话列表"""
    result = pyqtSignal(list)  # conversations

    def __init__(self, shop_id: str | None = None, limit: int = 100, parent=None):
        super().__init__(parent)
        self._shop_id = shop_id
        self._limit = limit

    def run(self):
        try:
            from services.message_persistence import message_persistence_service
            convs = message_persistence_service.get_conversations(shop_id=self._shop_id, limit=self._limit)
        except Exception:
            convs = []
        self.result.emit(convs)


class ChatUI(QFrame):
    """聊天记录页面"""

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setObjectName("ChatUI")
        self._shops: list[dict] = []
        self._shop_loader = None   # _ShopLoader 引用
        self._conv_loader = None   # _ConversationLoader 引用
        self._shops_loaded = False  # 店铺列表是否已加载过
        self._init_ui()
        self._apply_theme()
        # 延迟加载数据，放到事件队列末尾确保窗口先渲染
        QTimer.singleShot(500, self._initial_load)

    def _initial_load(self):
        """首次加载：同时加载店铺列表和会话列表"""
        self._load_shops()
        self._load_conversations(None)

    def _load_shops(self):
        """后台加载店铺列表（仅在首次或需要刷新时调用）"""
        if self._shop_loader is not None:
            try:
                self._shop_loader.result.disconnect(self._on_shops_loaded)
            except (TypeError, RuntimeError):
                pass
            self._shop_loader.quit()
            self._shop_loader.wait(500)
            self._shop_loader = None

        self._shop_loader = _ShopLoader(self)
        self._shop_loader.result.connect(self._on_shops_loaded)
        self._shop_loader.start()

    def _on_shops_loaded(self, shops: list[dict]):
        """店铺列表加载完成"""
        self._shops = shops
        self._shops_loaded = True

        self.shop_combo.blockSignals(True)
        self.shop_combo.clear()
        self.shop_combo.addItem("全部店铺", None)
        for shop in shops:
            display = f"{shop['shop_name']} ({shop['shop_id']})"
            self.shop_combo.addItem(display, shop["shop_id"])
        self.shop_combo.blockSignals(False)

        if len(shops) <= 1:
            self.shop_filter_container.hide()
        else:
            self.shop_filter_container.show()

    def _load_conversations(self, shop_id: str | None):
        """后台加载会话列表"""
        if self._conv_loader is not None:
            try:
                self._conv_loader.result.disconnect(self._on_conversations_loaded)
            except (TypeError, RuntimeError):
                pass
            self._conv_loader.quit()
            self._conv_loader.wait(500)
            self._conv_loader = None

        self._conv_loader = _ConversationLoader(shop_id=shop_id, limit=100, parent=self)
        self._conv_loader.result.connect(self._on_conversations_loaded)
        self._conv_loader.start()

    def _on_conversations_loaded(self, convs: list[dict]):
        """会话列表加载完成"""
        # 同步 _current_shop_filter（修复新消息过滤不一致）
        current_filter = self.shop_combo.currentData() if self._shops_loaded else None
        self.conversation_list._current_shop_filter = current_filter
        self.conversation_list._all_data = convs
        self.conversation_list._rebuild_cards(convs)

    def _on_shop_changed(self, index: int):
        """店铺筛选切换 — 只重新加载会话，不重建店铺列表"""
        shop_id = self.shop_combo.currentData()
        # 同步 filter 状态
        self.conversation_list._current_shop_filter = shop_id
        self.conversation_list._rebuild_cards([])  # 先清空
        self._load_conversations(shop_id)

    def _apply_theme(self):
        dark = isDarkTheme()
        # ChatUI 容器透明，继承 FluentWindow 背景
        self.setStyleSheet(f"""
            #ChatUI {{
                background-color: transparent;
                border: none;
            }}
        """)
        # 店铺筛选栏背景
        self.shop_filter_container.setStyleSheet(f"background-color: transparent;")

    def _init_ui(self):
        main_layout = QVBoxLayout(self)
        main_layout.setContentsMargins(8, 8, 8, 8)
        main_layout.setSpacing(6)

        # ---- 店铺筛选栏 ----
        self.shop_filter_container = QWidget()
        filter_layout = QHBoxLayout(self.shop_filter_container)
        filter_layout.setContentsMargins(0, 0, 0, 0)
        filter_layout.setSpacing(8)

        shop_label = BodyLabel("店铺:")
        shop_label.setFixedWidth(40)
        filter_layout.addWidget(shop_label)

        self.shop_combo = QComboBox()
        self.shop_combo.setMinimumWidth(200)
        self.shop_combo.setMaximumWidth(300)
        self.shop_combo.currentIndexChanged.connect(self._on_shop_changed)
        filter_layout.addWidget(self.shop_combo)
        filter_layout.addStretch()

        main_layout.addWidget(self.shop_filter_container)

        # ---- 双栏分割器 ----
        splitter = QSplitter(Qt.Orientation.Horizontal)
        splitter.setHandleWidth(2)

        # 左侧：会话列表
        self.conversation_list = ConversationListPanel()
        self.conversation_list.conversation_selected.connect(self._on_conversation_selected)
        splitter.addWidget(self.conversation_list)

        # 右侧：聊天区域
        self.chat_area = ChatAreaPanel()
        self.chat_area.send_manual_reply.connect(self._send_manual_reply)
        self.chat_area.forward_message.connect(self._on_forward_message)
        splitter.addWidget(self.chat_area)

        # 初始比例 1:3
        splitter.setSizes([250, 750])
        splitter.setStretchFactor(0, 1)
        splitter.setStretchFactor(1, 3)

        main_layout.addWidget(splitter, 1)

        # ---- 连接实时消息信号 ----
        try:
            from services.message_persistence import message_persistence_service
            message_persistence_service.signals.new_message.connect(self._on_new_message)
        except Exception as e:
            logger.warning(f"连接消息信号失败: {e}")

    def _on_conversation_selected(self, shop_id: str, buyer_uid: str):
        """选中会话"""
        self.chat_area.load_messages(shop_id, buyer_uid)

    def _on_new_message(self, msg_data: dict):
        """收到新消息"""
        # 检查店铺筛选
        current_filter = self.shop_combo.currentData()
        if current_filter and msg_data.get("shop_id") != current_filter:
            return

        # 增量更新会话列表（不全部重建）
        self.conversation_list.on_new_message(msg_data)

        # 追加到当前聊天
        self.chat_area.append_message(msg_data)

    def _send_manual_reply(self, shop_id: str, user_id: str, text: str, buyer_uid: str):
        """发送手动回复"""
        try:
            from Channel.pinduoduo.utils.API.send_message import SendMessage
            sender = SendMessage(str(shop_id), str(user_id))
            result = sender.send_text(str(buyer_uid), text)

            if isinstance(result, dict) and result.get("success"):
                # 通知客服回复事件管理器：手动回复等同于人工客服回复，
                # 需要取消正在等待的AI处理流程
                try:
                    from Message.handlers.staff_reply_event import staff_reply_event_manager
                    staff_reply_event_manager.notify_staff_reply(buyer_uid)
                except Exception:
                    pass

                # 在后台线程持久化，避免阻塞主线程
                from PyQt6.QtCore import QThread

                class _PersistWorker(QThread):
                    done = pyqtSignal(dict)

                    def __init__(self, sid, uid, buid, txt):
                        super().__init__()
                        self._sid = sid
                        self._uid = uid
                        self._buid = buid
                        self._txt = txt

                    def run(self):
                        try:
                            from services.message_persistence import message_persistence_service
                            msg_dict = message_persistence_service.save_manual_reply(
                                shop_id=self._sid, user_id=self._uid,
                                buyer_uid=self._buid, text=self._txt
                            )
                            if msg_dict:
                                self.done.emit(msg_dict)
                        except Exception:
                            pass

                worker = _PersistWorker(shop_id, user_id, buyer_uid, text)

                def _on_persist_done(msg_dict: dict):
                    from services.message_persistence import message_persistence_service
                    message_persistence_service.notify_new_message(msg_dict)

                worker.done.connect(_on_persist_done)
                worker.start()
                # 保持引用防止 GC
                self._persist_worker = worker
            else:
                logger.warning(f"发送手动回复失败: {result}")
        except Exception as e:
            logger.error(f"发送手动回复异常: {e}")

    def _on_forward_message(self, msg_data: dict, target_buyer_uid: str):
        """转发消息到目标会话"""
        shop_id = msg_data.get("shop_id", "")
        user_id = msg_data.get("user_id", "")
        content = msg_data.get("content", "")
        context_type = msg_data.get("context_type", "text") or "text"

        if not shop_id or not user_id or not content:
            logger.warning("转发消息缺少必要参数")
            return

        class _ForwardWorker(QThread):
            done = pyqtSignal(bool, str)  # success, error_msg

            def __init__(self, sid, uid, target_uid, cnt, ctx_type):
                super().__init__()
                self._sid = sid
                self._uid = uid
                self._target_uid = target_uid
                self._cnt = cnt
                self._ctx_type = ctx_type

            def run(self):
                try:
                    from Channel.pinduoduo.utils.API.send_message import SendMessage
                    sender = SendMessage(str(self._sid), str(self._uid))

                    if self._ctx_type == "image":
                        result = sender.send_image(str(self._target_uid), self._cnt)
                    elif self._ctx_type == "video":
                        result = sender.send_video(str(self._target_uid), self._cnt)
                    elif self._ctx_type == "goods_card":
                        # 商品卡片尝试提取 goods_id
                        try:
                            import json
                            meta = json.loads(self._cnt) if self._cnt.startswith("{") else {}
                            goods_id = meta.get("goods_id", self._cnt)
                            result = sender.send_mallGoodsCard(str(self._target_uid), str(goods_id))
                        except Exception:
                            result = sender.send_text(str(self._target_uid), self._cnt)
                    else:
                        result = sender.send_text(str(self._target_uid), self._cnt)

                    if isinstance(result, dict) and result.get("success"):
                        # 持久化
                        try:
                            from services.message_persistence import message_persistence_service
                            msg_dict = message_persistence_service.save_outbound_message(
                                shop_id=self._sid,
                                user_id=self._uid,
                                buyer_uid=self._target_uid,
                                reply_content=self._cnt,
                                reply_source="manual",
                                context_type=self._ctx_type,
                            )
                            if msg_dict:
                                message_persistence_service.notify_new_message(msg_dict)
                        except Exception:
                            pass
                        self.done.emit(True, "")
                    else:
                        self.done.emit(False, str(result))
                except Exception as e:
                    self.done.emit(False, str(e))

        worker = _ForwardWorker(shop_id, user_id, target_buyer_uid, content, context_type)
        worker.done.connect(
            lambda success, err: logger.info(f"转发消息: success={success}, err={err}")
            if not success else None
        )
        worker.start()
        # 保持引用防止 GC
        self._forward_worker = worker

    def cleanup(self):
        """清理资源"""
        try:
            from services.message_persistence import message_persistence_service
            message_persistence_service.signals.new_message.disconnect(self._on_new_message)
        except Exception:
            pass

    def changeEvent(self, event):
        if event.type() == QEvent.Type.PaletteChange:
            # 防抖：避免 setStyleSheet → PaletteChange → singleShot 乒乓循环
            if not getattr(self, '_palette_pending', False):
                self._palette_pending = True
                from PyQt6.QtCore import QTimer
                QTimer.singleShot(100, self._do_palette_update)
        super().changeEvent(event)

    def _do_palette_update(self):
        """实际执行调色板更新"""
        # 先执行更新，再延迟重置标志 —— 避免 setStyleSheet 触发的 PaletteChange
        # 在标志仍为 True 时被忽略，从而打破乒乓循环
        try:
            self._apply_theme()
        finally:
            QTimer.singleShot(200, self._reset_palette_pending)

    def _reset_palette_pending(self):
        """重置调色板更新标志"""
        self._palette_pending = False
