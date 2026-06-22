"""
聊天区域面板 - 消息气泡区 + 输入区
"""

from PyQt6.QtCore import Qt, pyqtSignal, QTimer, QEvent, QThread
from PyQt6.QtWidgets import (
    QWidget, QVBoxLayout, QHBoxLayout, QLabel, QSizePolicy, QFrame, QScrollArea,
)
from PyQt6.QtGui import QFont
from qfluentwidgets import ScrollArea, StrongBodyLabel, CaptionLabel, isDarkTheme

from ui.chat.message_bubble import MessageBubble
from ui.chat.input_area import InputArea


class _MessageLoader(QThread):
    """后台线程加载消息记录"""
    result = pyqtSignal(str, str, list)  # shop_id, buyer_uid, messages

    def __init__(self, shop_id: str, buyer_uid: str, parent=None):
        super().__init__(parent)
        self._shop_id = shop_id
        self._buyer_uid = buyer_uid

    def run(self):
        try:
            from services.message_persistence import message_persistence_service
            messages = message_persistence_service.get_messages_by_uid(
                shop_id=self._shop_id, buyer_uid=self._buyer_uid, limit=200
            )
        except Exception:
            messages = []
        self.result.emit(self._shop_id, self._buyer_uid, messages)


class ChatAreaPanel(QWidget):
    """聊天区域 - 头部 + 消息列表 + 输入框"""

    send_manual_reply = pyqtSignal(str, str, str, str)  # shop_id, user_id, text, buyer_uid

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setObjectName("ChatAreaPanel")
        self._current_shop_id: str = ""
        self._current_user_id: str = ""
        self._current_buyer_uid: str = ""
        self._loader: _MessageLoader | None = None

        self._init_ui()
        self._apply_theme()

    def _apply_theme(self):
        dark = isDarkTheme()
        # 整体背景透明，由上层决定
        self.setStyleSheet(f"""
            #ChatAreaPanel {{
                background-color: transparent;
                border: none;
            }}
        """)
        # Header 背景
        header_bg = "#2b2b2b" if dark else "#f5f5f5"
        header_border = "#3a3a3a" if dark else "#e0e0e0"
        self.header.setStyleSheet(f"""
            background-color: {header_bg};
            border-bottom: 1px solid {header_border};
        """)
        # 消息容器背景
        msg_bg = "#1e1e1e" if dark else "#fafafa"
        self._msg_container.setStyleSheet(f"background-color: {msg_bg};")

    def _init_ui(self):
        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(0)

        # 头部信息栏
        self.header = QWidget()
        self.header.setFixedHeight(48)
        header_layout = QHBoxLayout(self.header)
        header_layout.setContentsMargins(16, 4, 16, 4)

        self.header_title = StrongBodyLabel("请选择一个会话")
        self.header_title.setFont(QFont("Microsoft YaHei", 13))
        header_layout.addWidget(self.header_title)

        self.header_detail = CaptionLabel("")
        self.header_detail.setStyleSheet("color: #999;")
        header_layout.addWidget(self.header_detail)
        header_layout.addStretch()

        layout.addWidget(self.header)

        # 消息滚动区
        self.scroll_area = QScrollArea()
        self.scroll_area.setWidgetResizable(True)
        self.scroll_area.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        self.scroll_area.setFrameShape(QFrame.Shape.NoFrame)

        self._msg_container = QWidget()
        self._msg_layout = QVBoxLayout(self._msg_container)
        self._msg_layout.setContentsMargins(4, 4, 4, 4)
        self._msg_layout.setSpacing(4)
        self._msg_layout.addStretch()
        self.scroll_area.setWidget(self._msg_container)

        layout.addWidget(self.scroll_area, 1)

        # 输入区域
        self.input_area = InputArea()
        self.input_area.send_message.connect(self._on_input_message)
        self.input_area.set_enabled(False)
        layout.addWidget(self.input_area)

    def load_messages(self, shop_id: str, buyer_uid: str):
        """加载指定买家在指定店铺的消息（异步后台加载）"""
        # 缓存 shop_id/buyer_uid 用于手动发送
        self._current_shop_id = shop_id
        self._current_buyer_uid = buyer_uid

        # 清空旧消息
        self._clear_messages()
        self.header_title.setText("加载中...")
        self.header_detail.setText("")
        self.input_area.set_enabled(False)

        # 后台线程加载
        if self._loader is not None:
            self._loader.result.disconnect(self._on_messages_loaded)
            self._loader.quit()
            self._loader.wait(500)
        self._loader = _MessageLoader(shop_id, buyer_uid, self)
        self._loader.result.connect(self._on_messages_loaded)
        self._loader.start()

    def _on_messages_loaded(self, shop_id: str, buyer_uid: str, messages: list):
        """后台线程加载完成后，在主线程渲染气泡"""
        # 防止过期结果（用户已切换到其他会话）
        if shop_id != self._current_shop_id or buyer_uid != self._current_buyer_uid:
            return

        if messages:
            # 从首条消息提取 user_id
            self._current_user_id = messages[0].get("user_id", "")
            nickname = messages[0].get("nickname", buyer_uid)
            self.header_title.setText(nickname)
            self.header_detail.setText(f"({buyer_uid})")

            # 渲染气泡
            for msg in messages:
                bubble = MessageBubble(msg)
                self._msg_layout.insertWidget(self._msg_layout.count() - 1, bubble)

            self.input_area.set_enabled(True)
        else:
            self.header_title.setText("暂无消息")
            self.header_detail.setText("")
            self.input_area.set_enabled(False)

        # 滚动到底部
        QTimer.singleShot(80, self._scroll_to_bottom)

    def append_message(self, msg_data: dict):
        """追加新消息（实时）"""
        direction = msg_data.get("direction", "")
        buyer_uid = msg_data.get("buyer_uid", "")
        shop_id = msg_data.get("shop_id", "")

        # 只追加当前会话的消息
        if shop_id != self._current_shop_id or buyer_uid != self._current_buyer_uid:
            return

        bubble = MessageBubble(msg_data)
        self._msg_layout.insertWidget(self._msg_layout.count() - 1, bubble)
        QTimer.singleShot(50, self._scroll_to_bottom)

    def _clear_messages(self):
        """清空消息气泡"""
        while self._msg_layout.count() > 1:  # 保留底部 stretch
            item = self._msg_layout.takeAt(0)
            w = item.widget()
            if w is not None:
                w.setParent(None)
                w.deleteLater()
            elif item.layout() is not None:
                # 嵌套布局项，递归清理
                self._clear_sub_layout(item.layout())

    @staticmethod
    def _clear_sub_layout(layout):
        """递归清理嵌套布局中的 widget"""
        while layout.count():
            child = layout.takeAt(0)
            w = child.widget()
            if w is not None:
                w.setParent(None)
                w.deleteLater()
            elif child.layout() is not None:
                ChatAreaPanel._clear_sub_layout(child.layout())

    def _on_input_message(self, text: str):
        """输入框发送消息"""
        if not self._current_shop_id or not self._current_user_id or not self._current_buyer_uid:
            return
        self.send_manual_reply.emit(
            self._current_shop_id,
            self._current_user_id,
            text,
            self._current_buyer_uid,
        )

    def _scroll_to_bottom(self):
        """滚动到底部"""
        vbar = self.scroll_area.verticalScrollBar()
        vbar.setValue(vbar.maximum())

    def changeEvent(self, event):
        if event.type() == QEvent.Type.PaletteChange:
            # 防抖：避免 setStyleSheet → PaletteChange → singleShot 乒乓循环
            if not getattr(self, '_palette_pending', False):
                self._palette_pending = True
                QTimer.singleShot(100, self._do_palette_update)
        super().changeEvent(event)

    def _do_palette_update(self):
        """实际执行调色板更新"""
        self._palette_pending = False
        self._apply_theme()
