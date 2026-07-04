"""
气泡内图片缩略图 widget

- 异步加载图片缩略图
- 点击发射 clicked 信号
- 加载失败降级为可点击链接
- 主题适配
- 尺寸自适应：加载前小占位，加载后按实际图片尺寸调整
"""

from PyQt6.QtCore import Qt, pyqtSignal
from PyQt6.QtWidgets import QFrame, QVBoxLayout, QLabel, QSizePolicy
from PyQt6.QtGui import QPixmap, QFont
from qfluentwidgets import isDarkTheme


class ImagePreviewWidget(QFrame):
    """气泡内图片缩略图 widget"""

    clicked = pyqtSignal(str)  # 点击时发射 image url

    MAX_THUMB_WIDTH = 380
    MAX_THUMB_HEIGHT = 280
    LOADING_W = 120
    LOADING_H = 80

    def __init__(self, url: str, parent=None):
        super().__init__(parent)
        self._url = url
        self._loaded = False
        self._init_ui()
        self._start_load()
        self.apply_theme()

    def _init_ui(self):
        self.setFrameShape(QFrame.Shape.NoFrame)
        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(0)

        # 图片展示 label — 加载前不固定尺寸
        self._image_label = QLabel(self)
        self._image_label.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self._image_label.setFixedSize(self.LOADING_W, self.LOADING_H)
        self._image_label.setText("加载中...")
        layout.addWidget(self._image_label)

        # 降级链接 (默认隐藏)
        self._fallback_label = QLabel(self)
        self._fallback_label.setWordWrap(True)
        self._fallback_label.setOpenExternalLinks(True)
        self._fallback_label.setMaximumWidth(self.MAX_THUMB_WIDTH)
        self._fallback_label.hide()
        layout.addWidget(self._fallback_label)

        self.setSizePolicy(QSizePolicy.Policy.Fixed, QSizePolicy.Policy.Fixed)
        self.setFixedSize(self.LOADING_W, self.LOADING_H)
        self.setCursor(Qt.CursorShape.PointingHandCursor)

    def _start_load(self):
        from ui.chat.media.image_loader import ImageLoaderManager

        ImageLoaderManager().get_pixmap(
            self._url,
            (self.MAX_THUMB_WIDTH, self.MAX_THUMB_HEIGHT),
            self._on_pixmap_ready,
            self._on_load_failed,
        )

    def _on_pixmap_ready(self, url: str, pixmap: QPixmap):
        # 安全检查：widget 可能已被 deleteLater()
        try:
            if not self._image_label or not self._image_label.isVisible():
                return
        except RuntimeError:
            # C++ 对象已删除
            return

        if url != self._url:
            return  # 过期回调

        self._loaded = True

        # 缩放到气泡内最大尺寸
        scaled = pixmap.scaled(
            self.MAX_THUMB_WIDTH,
            self.MAX_THUMB_HEIGHT,
            Qt.AspectRatioMode.KeepAspectRatio,
            Qt.TransformationMode.SmoothTransformation,
        )
        self._image_label.setText("")
        self._image_label.setPixmap(scaled)
        self._image_label.setFixedSize(scaled.size())

        # widget 自身尺寸跟随图片
        self.setFixedSize(scaled.size())

    def _on_load_failed(self, url: str):
        # 安全检查：widget 可能已被 deleteLater()
        try:
            if not self._fallback_label:
                return
        except RuntimeError:
            return

        if url != self._url:
            return

        # 隐藏图片 label，显示降级链接
        try:
            self._image_label.hide()
        except RuntimeError:
            return
        self._fallback_label.setText(
            f'<a href="{self._url}" style="color: #007bff;">查看图片</a>'
        )
        self._fallback_label.show()

        # 收缩 widget 尺寸
        self.setFixedSize(120, 40)

    def mousePressEvent(self, event):
        if event.button() == Qt.MouseButton.LeftButton and self._loaded:
            self.clicked.emit(self._url)
        super().mousePressEvent(event)

    def apply_theme(self):
        """主题适配"""
        dark = isDarkTheme()
        loading_color = "#e0e0e0" if dark else "#888888"
        self._image_label.setStyleSheet(f"color: {loading_color};")
        font = QFont("Microsoft YaHei", 9)
        self._image_label.setFont(font)
