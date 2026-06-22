"""
聊天记录页面 - 主界面
双栏布局：左侧会话列表 + 右侧聊天区域，顶部店铺筛选
"""

from PyQt6.QtCore import Qt, pyqtSignal, QEvent
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


class ChatUI(QFrame):
    """聊天记录页面"""

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setObjectName("ChatUI")
        self._shops: list[dict] = []
        self._init_ui()
        # 延迟加载店铺列表
        from PyQt6.QtCore import QTimer
        QTimer.singleShot(300, self._load_shops)

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

    def _load_shops(self):
        """加载店铺列表到 ComboBox"""
        try:
            from database.db_manager import get_db_manager
            db = get_db_manager()
            self._shops = db.get_all_shops()
        except Exception as e:
            logger.warning(f"加载店铺列表失败: {e}")
            self._shops = []

        self.shop_combo.blockSignals(True)
        self.shop_combo.clear()
        self.shop_combo.addItem("全部店铺", None)
        for shop in self._shops:
            display = f"{shop['shop_name']} ({shop['shop_id']})"
            self.shop_combo.addItem(display, shop["shop_id"])
        self.shop_combo.blockSignals(False)

        # 单店自动隐藏
        if len(self._shops) <= 1:
            self.shop_filter_container.hide()
        else:
            self.shop_filter_container.show()

        # 加载会话列表
        self.conversation_list.refresh(shop_id=None)

    def _on_shop_changed(self, index: int):
        """店铺筛选切换"""
        shop_id = self.shop_combo.currentData()
        self.conversation_list.refresh(shop_id=shop_id)

    def _on_conversation_selected(self, shop_id: str, buyer_uid: str):
        """选中会话"""
        self.chat_area.load_messages(shop_id, buyer_uid)

    def _on_new_message(self, msg_data: dict):
        """收到新消息"""
        # 检查店铺筛选
        current_filter = self.shop_combo.currentData()
        if current_filter and msg_data.get("shop_id") != current_filter:
            return

        # 更新会话列表
        shop_id = msg_data.get("shop_id", "")
        if current_filter is None or current_filter == shop_id:
            self.conversation_list.refresh(shop_id=current_filter)

        # 追加到当前聊天
        self.chat_area.append_message(msg_data)

    def _send_manual_reply(self, shop_id: str, user_id: str, text: str, buyer_uid: str):
        """发送手动回复"""
        try:
            from Channel.pinduoduo.utils.API.send_message import SendMessage
            sender = SendMessage(str(shop_id), str(user_id))
            result = sender.send_text(str(buyer_uid), text)

            if isinstance(result, dict) and result.get("success"):
                # 持久化
                try:
                    from services.message_persistence import message_persistence_service
                    msg_dict = message_persistence_service.save_manual_reply(
                        shop_id=shop_id, user_id=user_id, buyer_uid=buyer_uid, text=text
                    )
                    if msg_dict:
                        message_persistence_service.notify_new_message(msg_dict)
                except Exception as e:
                    logger.warning(f"持久化手动回复失败: {e}")
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
            # 子组件自行处理主题，此处不需要额外操作
            pass
        super().changeEvent(event)
