"""
消息气泡组件 - 聊天界面中的单条消息展示
"""

from PyQt6.QtCore import Qt, pyqtSignal
from PyQt6.QtWidgets import (
    QFrame, QHBoxLayout, QVBoxLayout, QLabel, QSizePolicy, QTextEdit,
)
from PyQt6.QtGui import QFont, QTextOption
from qfluentwidgets import isDarkTheme, CaptionLabel, InfoBadge


class MessageBubble(QFrame):
    """消息气泡 - 左对齐（买家）或右对齐（客服）"""

    # 颜色常量
    INBOUND_BG_LIGHT = "#e8e8e8"
    INBOUND_BG_DARK = "#3a3a3a"
    INBOUND_TEXT_LIGHT = "#333333"
    INBOUND_TEXT_DARK = "#e0e0e0"

    OUTBOUND_BG_LIGHT = "#cce5ff"
    OUTBOUND_BG_DARK = "#1a5a8a"
    OUTBOUND_TEXT_LIGHT = "#333333"
    OUTBOUND_TEXT_DARK = "#ffffff"

    REPLY_SOURCE_LABELS = {
        "ai": "AI",
        "keyword": "关键词",
        "staff": "人工",
        "fallback": "兜底",
        "manual": "手动",
    }
    REPLY_SOURCE_COLORS = {
        "ai": "#28a745",
        "keyword": "#6f42c1",
        "staff": "#007bff",
        "fallback": "#dc3545",
        "manual": "#fd7e14",
    }

    def __init__(self, msg_data: dict, parent=None):
        super().__init__(parent)
        self.msg_data = msg_data
        self.direction = msg_data.get("direction", "inbound")
        self._init_ui()

    def _init_ui(self):
        self.setObjectName("MessageBubble")
        self.setFrameShape(QFrame.Shape.NoFrame)

        # 主布局（控制左/右对齐）
        outer_layout = QHBoxLayout(self)
        outer_layout.setContentsMargins(8, 2, 8, 2)

        # 气泡容器
        bubble = QFrame()
        bubble.setObjectName("BubbleContainer")
        bubble.setSizePolicy(QSizePolicy.Policy.Maximum, QSizePolicy.Policy.Preferred)
        self._bubble = bubble

        # 气泡内部布局
        bubble_layout = QVBoxLayout(bubble)
        bubble_layout.setContentsMargins(10, 6, 10, 6)
        bubble_layout.setSpacing(3)

        # 回复来源标签（仅出站）
        reply_source = self.msg_data.get("reply_source")
        if self.direction == "outbound" and reply_source:
            source_label = self.REPLY_SOURCE_LABELS.get(reply_source, reply_source)
            badge = InfoBadge.custom(
                source_label,
                self.REPLY_SOURCE_COLORS.get(reply_source, "#888888"),
                "#ffffff",
            )
            badge.setMaximumWidth(50)
            badge.setMaximumHeight(18)
            badge_layout = QHBoxLayout()
            badge_layout.setContentsMargins(0, 0, 0, 0)
            badge_layout.addStretch()
            badge_layout.addWidget(badge)
            bubble_layout.addLayout(badge_layout)

        # 消息内容
        content = self.msg_data.get("content") or ""
        content_label = QLabel(content)
        content_label.setWordWrap(True)
        content_label.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse)
        content_label.setMaximumWidth(420)
        content_font = QFont("Microsoft YaHei", 10)
        content_label.setFont(content_font)
        content_label.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Preferred)
        self._content_label = content_label
        bubble_layout.addWidget(content_label)

        # 时间戳
        ts_str = self.msg_data.get("timestamp", "")
        if ts_str:
            try:
                from datetime import datetime
                dt = datetime.fromisoformat(ts_str)
                time_text = dt.strftime("%H:%M")
            except (ValueError, TypeError):
                time_text = ts_str[-8:-3] if len(ts_str) > 8 else ts_str
        else:
            time_text = ""
        time_label = CaptionLabel(time_text)
        time_layout = QHBoxLayout()
        time_layout.setContentsMargins(0, 0, 0, 0)
        time_layout.addStretch()
        time_layout.addWidget(time_label)
        bubble_layout.addLayout(time_layout)

        # 对齐方向
        if self.direction == "inbound":
            # 买家消息 — 靠左
            spacer_right = QFrame()
            spacer_right.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Preferred)
            outer_layout.addWidget(bubble)
            outer_layout.addWidget(spacer_right)
        else:
            # 客服消息 — 靠右
            spacer_left = QFrame()
            spacer_left.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Preferred)
            outer_layout.addWidget(spacer_left)
            outer_layout.addWidget(bubble)

        self._apply_theme()

    def _apply_theme(self):
        """应用主题颜色"""
        dark = isDarkTheme()
        if self.direction == "inbound":
            bg = self.INBOUND_BG_DARK if dark else self.INBOUND_BG_LIGHT
            fg = self.INBOUND_TEXT_DARK if dark else self.INBOUND_TEXT_LIGHT
        else:
            bg = self.OUTBOUND_BG_DARK if dark else self.OUTBOUND_BG_LIGHT
            fg = self.OUTBOUND_TEXT_DARK if dark else self.OUTBOUND_TEXT_LIGHT

        self._bubble.setStyleSheet(f"""
            #BubbleContainer {{
                background-color: {bg};
                border-radius: 10px;
            }}
        """)
        self._content_label.setStyleSheet(f"color: {fg};")

    def changeEvent(self, event):
        """监听主题切换"""
        from PyQt6.QtCore import QEvent
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
