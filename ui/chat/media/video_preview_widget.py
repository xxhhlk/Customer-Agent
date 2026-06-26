"""
气泡内视频预览 widget

- 显示视频封面帧缩略图 + 播放按钮覆盖层
- 异步加载封面（复用 ImageLoaderManager）
- 点击发射 clicked 信号（携带视频 URL）
- 加载失败降级为可点击链接
- 主题适配
"""

import json
from typing import Optional

from PyQt6.QtCore import Qt, pyqtSignal, QRect
from PyQt6.QtGui import QPixmap, QPainter, QColor, QPainterPath, QFont
from PyQt6.QtWidgets import QFrame, QVBoxLayout, QLabel, QSizePolicy
from qfluentwidgets import isDarkTheme


class VideoPreviewWidget(QFrame):
    """气泡内视频缩略图 widget — 封面 + 播放按钮"""

    clicked = pyqtSignal(str)  # 点击时发射 video url

    MAX_THUMB_WIDTH = 380
    MAX_THUMB_HEIGHT = 280
    LOADING_W = 160
    LOADING_H = 100

    def __init__(self, url: str, media_meta: Optional[str] = None, parent=None):
        super().__init__(parent)
        self._url = url
        self._cover_url: Optional[str] = None
        self._duration: Optional[float] = None
        self._loaded = False

        # 解析 media_meta
        self._parse_media_meta(media_meta)

        self._init_ui()
        self._start_load()
        self.apply_theme()

    def _parse_media_meta(self, media_meta: Optional[str]):
        """解析 media_meta JSON 字符串"""
        if not media_meta:
            return
        try:
            meta = json.loads(media_meta) if isinstance(media_meta, str) else media_meta
            self._cover_url = meta.get("cover_url")
            self._duration = meta.get("duration")
        except (json.JSONDecodeError, TypeError):
            pass

    def _init_ui(self):
        self.setFrameShape(QFrame.Shape.NoFrame)
        # 拦截子 widget 的鼠标事件：不让 QLabel 处理链接点击
        self.setAttribute(Qt.WidgetAttribute.WA_Hover, True)

        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(0)

        # 封面展示 label
        self._image_label = QLabel(self)
        self._image_label.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self._image_label.setFixedSize(self.LOADING_W, self.LOADING_H)
        self._image_label.setText("加载中...")
        # 阻止 QLabel 处理链接点击（防止弹出浏览器）
        self._image_label.setTextInteractionFlags(Qt.TextInteractionFlag.NoTextInteraction)
        layout.addWidget(self._image_label)

        # 降级链接 (默认隐藏) — 不使用 HTML <a> 标签，纯文本+样式模拟链接
        self._fallback_label = QLabel(self)
        self._fallback_label.setWordWrap(True)
        self._fallback_label.setTextInteractionFlags(Qt.TextInteractionFlag.NoTextInteraction)
        self._fallback_label.setMaximumWidth(self.MAX_THUMB_WIDTH)
        self._fallback_label.hide()
        layout.addWidget(self._fallback_label)

        self.setSizePolicy(QSizePolicy.Policy.Fixed, QSizePolicy.Policy.Fixed)
        self.setFixedSize(self.LOADING_W, self.LOADING_H)
        self.setCursor(Qt.CursorShape.PointingHandCursor)

    def _start_load(self):
        """异步加载封面帧"""
        if not self._cover_url:
            # 没有封面 URL，直接降级
            self._on_load_failed("")
            return

        from ui.chat.media.image_loader import ImageLoaderManager

        ImageLoaderManager().get_pixmap(
            self._cover_url,
            (self.MAX_THUMB_WIDTH, self.MAX_THUMB_HEIGHT),
            self._on_pixmap_ready,
            self._on_load_failed,
        )

    def _on_pixmap_ready(self, url: str, pixmap: QPixmap):
        if url != self._cover_url:
            return  # 过期回调

        self._loaded = True

        # 缩放到气泡内最大尺寸
        scaled = pixmap.scaled(
            self.MAX_THUMB_WIDTH,
            self.MAX_THUMB_HEIGHT,
            Qt.AspectRatioMode.KeepAspectRatio,
            Qt.TransformationMode.SmoothTransformation,
        )

        # 在封面上绘制播放按钮和时长
        self._image_label.setText("")
        self._image_label.setFixedSize(scaled.size())
        self.setFixedSize(scaled.size())
        self._draw_overlay(scaled)

    def _draw_overlay(self, pixmap: QPixmap):
        """在封面上绘制半透明播放按钮 + 时长标签"""
        result = pixmap.copy()  # 不修改原图
        painter = QPainter(result)
        painter.setRenderHint(QPainter.RenderHint.SmoothPixmapTransform)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing)

        w, h = result.width(), result.height()

        # 1. 半透明遮罩（让播放按钮更突出）
        overlay = QColor(0, 0, 0, 60)
        painter.fillRect(0, 0, w, h, overlay)

        # 2. 中心圆形播放按钮
        btn_radius = min(w, h) // 5
        btn_radius = max(btn_radius, 24)  # 最小 24px
        btn_radius = min(btn_radius, 40)  # 最大 40px
        cx, cy = w // 2, h // 2

        # 半透明白色背景圆
        painter.setBrush(QColor(255, 255, 255, 200))
        painter.setPen(Qt.PenStyle.NoPen)
        painter.drawEllipse(
            QRect(cx - btn_radius, cy - btn_radius, btn_radius * 2, btn_radius * 2)
        )

        # 三角形播放图标（指向右）
        tri_size = btn_radius
        triangle = QPainterPath()
        triangle.moveTo(cx - tri_size // 3, cy - tri_size // 2)
        triangle.lineTo(cx - tri_size // 3, cy + tri_size // 2)
        triangle.lineTo(cx + tri_size * 2 // 3, cy)
        triangle.closeSubpath()
        painter.setBrush(QColor(0, 0, 0, 200))
        painter.drawPath(triangle)

        # 3. 时长标签（右下角）
        if self._duration is not None:
            try:
                dur = float(self._duration)
                mins = int(dur // 60)
                secs = int(dur % 60)
                dur_text = f"{mins}:{secs:02d}"
            except (ValueError, TypeError):
                dur_text = ""

            if dur_text:
                font = QFont("Microsoft YaHei", 8)
                painter.setFont(font)
                fm = painter.fontMetrics()
                text_w = fm.horizontalAdvance(dur_text) + 10
                text_h = fm.height() + 4
                margin = 6

                # 半透明黑色背景
                painter.setBrush(QColor(0, 0, 0, 170))
                painter.setPen(Qt.PenStyle.NoPen)
                painter.drawRoundedRect(
                    w - text_w - margin,
                    h - text_h - margin,
                    text_w,
                    text_h,
                    3,
                    3,
                )
                # 白色文字
                painter.setPen(QColor(255, 255, 255))
                painter.drawText(
                    w - text_w - margin + 5,
                    h - margin - 3,
                    dur_text,
                )

        painter.end()
        self._image_label.setPixmap(result)

    def _on_load_failed(self, url: str):
        if url and url != self._cover_url:
            return

        # 隐藏封面 label，显示降级文本（纯文本+蓝色下划线样式模拟链接，不弹出浏览器）
        self._image_label.hide()
        self._fallback_label.setText("▶ 查看视频")
        self._fallback_label.setToolTip(self._url)
        self._apply_fallback_style()
        self._fallback_label.show()
        self.setFixedSize(120, 40)

    def _apply_fallback_style(self):
        """设置降级链接样式（蓝色下划线）"""
        dark = isDarkTheme()
        link_color = "#5b9bd5" if dark else "#007bff"
        self._fallback_label.setStyleSheet(
            f"color: {link_color}; text-decoration: underline; font-size: 10px;"
        )

    def mousePressEvent(self, event):
        if event.button() == Qt.MouseButton.LeftButton:
            self.clicked.emit(self._url)
        super().mousePressEvent(event)

    def apply_theme(self):
        """主题适配"""
        dark = isDarkTheme()
        loading_color = "#e0e0e0" if dark else "#888888"
        self._image_label.setStyleSheet(f"color: {loading_color};")
        font = QFont("Microsoft YaHei", 9)
        self._image_label.setFont(font)
        # 如果降级链接触发过，也更新其样式
        if self._fallback_label.isVisible():
            self._apply_fallback_style()
