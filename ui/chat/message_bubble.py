"""
消息气泡组件 - 聊天界面中的单条消息展示
"""

from PyQt6.QtCore import Qt, pyqtSignal, QTimer
from PyQt6.QtWidgets import (
    QFrame, QHBoxLayout, QVBoxLayout, QLabel, QSizePolicy, QTextEdit,
    QApplication, QMenu,
)
from PyQt6.QtGui import QFont, QTextOption, QAction
from qfluentwidgets import isDarkTheme, CaptionLabel


class MessageBubble(QFrame):
    """消息气泡 - 左对齐（买家）或右对齐（客服）"""

    forward_requested = pyqtSignal(dict)  # 转发消息请求，携带 msg_data

    # 颜色常量
    INBOUND_BG_LIGHT = "#e8e8e8"
    INBOUND_BG_DARK = "#3a3a3a"
    INBOUND_TEXT_LIGHT = "#333333"
    INBOUND_TEXT_DARK = "#e0e0e0"

    OUTBOUND_BG_LIGHT = "#cce5ff"
    OUTBOUND_BG_DARK = "#2d7db8"  # 提亮深色模式我方气泡，增强与 #1e1e1e 背景的对比
    OUTBOUND_TEXT_LIGHT = "#333333"
    OUTBOUND_TEXT_DARK = "#ffffff"

    REPLY_SOURCE_LABELS = {
        "ai": "AI",
        "keyword": "关键词",
        "staff": "人工",
        "fallback": "兜底",
        "manual": "手动",
    }
    # 深色模式下使用更亮的标签颜色，避免与深色气泡背景融为一体
    REPLY_SOURCE_COLORS = {
        "ai": "#28a745",
        "keyword": "#6f42c1",
        "staff": "#007bff",
        "fallback": "#dc3545",
        "manual": "#fd7e14",
    }
    REPLY_SOURCE_COLORS_DARK = {
        "ai": "#3dd16a",       # 亮绿
        "keyword": "#a061d4",   # 亮紫
        "staff": "#3d9bff",     # 亮蓝
        "fallback": "#f56565",  # 亮红
        "manual": "#ffa040",    # 亮橙
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
        self._source_badge = None
        self._reply_source_key = None
        reply_source = self.msg_data.get("reply_source")
        if self.direction == "outbound" and reply_source:
            source_label = self.REPLY_SOURCE_LABELS.get(reply_source, reply_source) or ""
            # 使用自定义 QLabel 而非 InfoBadge，避免 FluentStyleSheet 主题切换时覆盖颜色
            badge = QLabel(source_label)
            badge.setAlignment(Qt.AlignmentFlag.AlignCenter)
            badge.setFixedHeight(18)
            badge.setMaximumWidth(60)
            badge_font = QFont("Microsoft YaHei", 8)
            badge.setFont(badge_font)
            badge_layout = QHBoxLayout()
            badge_layout.setContentsMargins(0, 0, 0, 0)
            badge_layout.addStretch()
            badge_layout.addWidget(badge)
            bubble_layout.addLayout(badge_layout)
            self._source_badge = badge
            self._reply_source_key = reply_source

        # 消息内容 - 根据 context_type 分发渲染
        content = self.msg_data.get("content") or ""
        context_type = self.msg_data.get("context_type", "text") or "text"

        # 对历史 mall_cs 消息，通过 content URL 后缀推断实际媒体类型
        # （旧消息 raw_data 未持久化，无法从 DB 修正，在 UI 层兼容）
        if context_type not in ("image", "video"):
            detected = self._detect_media_type_from_content(content)
            if detected:
                context_type = detected
                self.msg_data["context_type"] = context_type  # 同步给 _apply_theme

        if context_type == "image":
            from ui.chat.media.image_preview_widget import ImagePreviewWidget

            self._content_widget = ImagePreviewWidget(content, self._bubble)
            self._content_widget.clicked.connect(self._on_image_clicked)
            self._content_label = self._content_widget  # 兼容别名
        elif context_type == "video":
            from ui.chat.media.video_preview_widget import VideoPreviewWidget

            media_meta = self.msg_data.get("media_meta")
            self._content_widget = VideoPreviewWidget(content, media_meta, self._bubble)
            self._content_widget.clicked.connect(self._on_video_clicked)
            self._content_label = self._content_widget  # 兼容别名
        else:
            content_label = QLabel(content)
            content_label.setObjectName("ContentLabel")
            content_label.setWordWrap(True)
            content_label.setTextFormat(Qt.TextFormat.PlainText)  # 防止HTML内联样式覆盖颜色
            content_label.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse)
            content_label.setMaximumWidth(420)
            content_font = QFont("Microsoft YaHei", 10)
            content_label.setFont(content_font)
            content_label.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Preferred)
            self._content_label = content_label
            self._content_widget = content_label  # 统一引用

        bubble_layout.addWidget(self._content_widget)

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

        # 右键菜单
        self.setContextMenuPolicy(Qt.ContextMenuPolicy.CustomContextMenu)
        self.customContextMenuRequested.connect(self._show_context_menu)

    def _show_context_menu(self, pos):
        """显示右键菜单 — 复制消息 / 转发消息"""
        menu = QMenu(self)

        content = self.msg_data.get("content", "")
        context_type = self.msg_data.get("context_type", "text") or "text"

        if content:
            copy_action = QAction("复制消息", self)
            copy_action.triggered.connect(self._copy_content)
            menu.addAction(copy_action)

        # 图片/视频消息：额外提供「复制链接」
        if context_type in ("image", "video") and content.startswith(("http://", "https://")):
            copy_url_action = QAction("复制链接", self)
            copy_url_action.triggered.connect(self._copy_content)
            menu.addAction(copy_url_action)

        menu.addSeparator()

        forward_action = QAction("转发消息", self)
        forward_action.triggered.connect(self._on_forward)
        menu.addAction(forward_action)

        menu.exec(self.mapToGlobal(pos))

    def _copy_content(self):
        """复制消息内容到剪贴板"""
        content = self.msg_data.get("content", "")
        if content:
            clipboard = QApplication.clipboard()
            if clipboard is not None:
                clipboard.setText(content)

    def _on_forward(self):
        """触发转发请求"""
        self.forward_requested.emit(self.msg_data)

    # -- 历史消息媒体类型推断 --
    # PDD 图片消息 content 是 URL，以 .jpg/.jpeg/.png/.gif/.webp 结尾
    # PDD 视频消息 content 是 URL，以 .mp4/.mov/.avi/.mkv 结尾
    _IMAGE_EXTS = ('.jpg', '.jpeg', '.png', '.gif', '.webp', '.bmp')
    _VIDEO_EXTS = ('.mp4', '.mov', '.avi', '.mkv', '.webm', '.3gp', '.m4v')

    def _detect_media_type_from_content(self, content: str) -> str | None:
        """通过 content URL 后缀推断媒体类型，仅对 http(s) URL 有效"""
        if not content or not content.startswith(('http://', 'https://')):
            return None
        url_lower = content.split('?')[0].split('#')[0].lower()
        if url_lower.endswith(self._VIDEO_EXTS):
            return "video"
        if url_lower.endswith(self._IMAGE_EXTS):
            return "image"
        return None

    def _on_image_clicked(self, url: str):
        from ui.chat.media.full_image_viewer import FullImageViewer

        # 用 window() 作为 parent，不存引用到 self 上，
        # 避免 MessageBubble 被 deleteLater() 后引用断裂
        viewer = FullImageViewer(url, self.window())
        # 存到 window 上防止 GC（而不是 self）
        win = self.window()
        if hasattr(win, '_open_viewers'):
            win._open_viewers.append(viewer)
        else:
            win._open_viewers = [viewer]
        viewer.setAttribute(Qt.WidgetAttribute.WA_DeleteOnClose, True)
        viewer.show()
        # 对话框关闭时从列表移除
        viewer.destroyed.connect(lambda: win._open_viewers.remove(viewer) if hasattr(win, '_open_viewers') and viewer in win._open_viewers else None)

    def _on_video_clicked(self, url: str):
        """点击视频 → 打开视频播放器"""
        from ui.chat.media.video_player_dialog import VideoPlayerDialog

        player = VideoPlayerDialog(url, self.window())
        win = self.window()
        if hasattr(win, '_open_viewers'):
            win._open_viewers.append(player)
        else:
            win._open_viewers = [player]
        player.setAttribute(Qt.WidgetAttribute.WA_DeleteOnClose, True)
        player.show()
        player.destroyed.connect(lambda: win._open_viewers.remove(player) if hasattr(win, '_open_viewers') and player in win._open_viewers else None)

    def _apply_theme(self):
        """应用主题颜色"""
        dark = isDarkTheme()
        if self.direction == "inbound":
            bg = self.INBOUND_BG_DARK if dark else self.INBOUND_BG_LIGHT
            fg = self.INBOUND_TEXT_DARK if dark else self.INBOUND_TEXT_LIGHT
        else:
            bg = self.OUTBOUND_BG_DARK if dark else self.OUTBOUND_BG_LIGHT
            fg = self.OUTBOUND_TEXT_DARK if dark else self.OUTBOUND_TEXT_LIGHT

        # 一次性设置气泡背景 + 文字颜色，避免两次 setStyleSheet 的竞态
        context_type = self.msg_data.get("context_type", "text") or "text"
        if context_type in ("image", "video"):
            # 图片/视频消息：只设置气泡背景，内容由各自的 widget 自己渲染
            self._bubble.setStyleSheet(f"""
                #BubbleContainer {{
                    background-color: {bg};
                    border-radius: 10px;
                }}
            """)
            if hasattr(self._content_widget, "apply_theme"):
                self._content_widget.apply_theme()  # type: ignore[union-attr]
        else:
            # 文本类消息（text / mall_cs / emotion / goods_card 等）：
            # 气泡背景设在 bubble 上，文字颜色直接设在 content_label 自身上，
            # 不再依赖父级 #ContentLabel CSS 选择器级联，避免 setStyleSheet("")
            # 清空时触发样式重算竞态导致文字随机变深色
            self._bubble.setStyleSheet(f"""
                #BubbleContainer {{
                    background-color: {bg};
                    border-radius: 10px;
                }}
            """)
            self._content_label.setStyleSheet(
                f"color: {fg}; background-color: transparent;"
            )

        # 更新来源标签的颜色以匹配当前主题
        if self._source_badge is not None and self._reply_source_key:
            color_map = self.REPLY_SOURCE_COLORS_DARK if dark else self.REPLY_SOURCE_COLORS
            badge_bg = color_map.get(self._reply_source_key, "#888888")
            self._source_badge.setStyleSheet(f"""
                background-color: {badge_bg};
                color: white;
                border-radius: 9px;
                padding: 1px 6px;
            """)

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
