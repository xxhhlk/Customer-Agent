"""
输入区域组件 - 消息输入框 + 发送按钮
"""

from PyQt6.QtCore import Qt, pyqtSignal, QEvent
from PyQt6.QtWidgets import QWidget, QHBoxLayout, QVBoxLayout, QTextEdit, QLabel
from PyQt6.QtGui import QFont, QKeyEvent
from qfluentwidgets import PrimaryPushButton, isDarkTheme


class InputArea(QWidget):
    """聊天输入区域"""

    send_message = pyqtSignal(str)  # 发送消息信号

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setObjectName("InputArea")
        self.setFixedHeight(120)
        self._init_ui()
        self._apply_theme()

    def _apply_theme(self):
        dark = isDarkTheme()
        bg = "#2b2b2b" if dark else "#f5f5f5"
        border = "#3a3a3a" if dark else "#e0e0e0"
        input_bg = "#1e1e1e" if dark else "#ffffff"
        input_color = "#e0e0e0" if dark else "#333333"
        self.setStyleSheet(f"""
            #InputArea {{
                background-color: {bg};
                border-top: 1px solid {border};
            }}
        """)
        self.text_edit.setStyleSheet(f"""
            QTextEdit {{
                background-color: {input_bg};
                color: {input_color};
                border: 1px solid {border};
                border-radius: 6px;
                padding: 6px 8px;
            }}
            QTextEdit:focus {{
                border-color: #4a90d9;
            }}
        """)

    def _init_ui(self):
        layout = QVBoxLayout(self)
        layout.setContentsMargins(8, 4, 8, 8)
        layout.setSpacing(4)

        # 输入框
        self.text_edit = QTextEdit()
        self.text_edit.setPlaceholderText("输入消息...")
        self.text_edit.setFont(QFont("Microsoft YaHei", 10))
        self.text_edit.setMaximumHeight(68)
        self.text_edit.setMinimumHeight(60)
        self.text_edit.installEventFilter(self)
        layout.addWidget(self.text_edit)

        # 按钮行
        btn_layout = QHBoxLayout()
        btn_layout.setContentsMargins(0, 0, 0, 0)
        btn_layout.setSpacing(8)

        self.hint_label = QLabel()
        self.hint_label.setFont(QFont("Microsoft YaHei", 9))
        self.hint_label.setStyleSheet("color: #999;")
        btn_layout.addWidget(self.hint_label)
        btn_layout.addStretch()

        self.send_btn = PrimaryPushButton("发送 (Enter)")
        self.send_btn.setFixedWidth(110)
        self.send_btn.clicked.connect(self._on_send_clicked)
        btn_layout.addWidget(self.send_btn)

        layout.addLayout(btn_layout)

    def eventFilter(self, obj, event):
        """拦截键盘事件：Enter 发送 / Shift+Enter 换行"""
        if obj is self.text_edit and event.type() == QKeyEvent.Type.KeyPress:
            key = event.key()
            modifiers = event.modifiers()
            if key == Qt.Key.Key_Return or key == Qt.Key.Key_Enter:
                if modifiers & Qt.KeyboardModifier.ShiftModifier:
                    # Shift+Enter → 换行
                    return False
                else:
                    # Enter → 发送
                    self._on_send_clicked()
                    return True
        return super().eventFilter(obj, event)

    def _on_send_clicked(self):
        """发送按钮 / Enter 触发"""
        text = self.text_edit.toPlainText().strip()
        if not text:
            return
        self.send_message.emit(text)
        self.text_edit.clear()

    def set_enabled(self, enabled: bool):
        """启用/禁用输入框"""
        self.text_edit.setEnabled(enabled)
        self.send_btn.setEnabled(enabled)
        if not enabled:
            self.hint_label.setText("请先选择一个会话")
        else:
            self.hint_label.setText("")

    def clear(self):
        self.text_edit.clear()

    def changeEvent(self, event):
        if event.type() == QEvent.Type.PaletteChange:
            from PyQt6.QtCore import QTimer
            QTimer.singleShot(0, self._apply_theme)
        super().changeEvent(event)
