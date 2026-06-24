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


class _DataLoader(QThread):
    """后台线程加载聊天数据"""
    result = pyqtSignal(list, list)  # shops, conversations

    def __init__(self, shop_id: str | None, parent=None):
        super().__init__(parent)
        self._shop_id = shop_id

    def run(self):
        try:
            from database.db_manager import get_db_manager
            db = get_db_manager()
            shops = db.get_all_shops()
        except Exception:
            shops = []
        try:
            from services.message_persistence import message_persistence_service
            convs = message_persistence_service.get_conversations(shop_id=self._shop_id, limit=100)
        except Exception:
            convs = []
        self.result.emit(shops, convs)


class ChatUI(QFrame):
    """聊天记录页面"""

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setObjectName("ChatUI")
        self._shops: list[dict] = []
        self._loader = None  # DataLoader 实例引用，用于取消旧任务
        self._init_ui()
        self._apply_theme()
        # 延迟加载数据，放到事件队列末尾确保窗口先渲染
        QTimer.singleShot(500, lambda: self._load_data(shop_id=None))

    def _load_data(self, shop_id: str | None):
        """在后台线程加载数据库数据，避免阻塞 UI"""
        # 取消旧 loader（防止多个 loader 同时运行导致竞态条件）
        if self._loader is not None:
            try:
                self._loader.result.disconnect(self._on_data_loaded)
            except (TypeError, RuntimeError):
                pass
            self._loader.quit()
            self._loader.wait(500)
            self._loader = None

        self._loader = _DataLoader(shop_id, self)
        self._loader.result.connect(self._on_data_loaded)
        self._loader.start()

    def _on_data_loaded(self, shops: list[dict], convs: list[dict]):
        """后台线程加载完成后，在主线程更新 UI"""
        self._shops = shops

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

        self.conversation_list._all_data = convs
        self.conversation_list._rebuild_cards(convs)

    def _on_shop_changed(self, index: int):
        """店铺筛选切换 — 后台加载"""
        shop_id = self.shop_combo.currentData()
        self.conversation_list._rebuild_cards([])  # 先清空
        from PyQt6.QtCore import QTimer
        QTimer.singleShot(50, lambda: self._load_data(shop_id=shop_id))

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
