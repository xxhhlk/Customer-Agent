"""
全屏图片查看器 - QGraphicsView + QGraphicsPixmapItem

功能:
- 滚轮缩放 (Ctrl+滚轮)
- 鼠标拖拽
- 工具栏: 放大/缩小/适应窗口/实际大小/另存为/关闭
- 异步加载原图
- ESC 关闭
"""

from PyQt6.QtCore import Qt, QRectF
from PyQt6.QtGui import QPixmap, QPainter, QKeyEvent
from PyQt6.QtWidgets import (
    QDialog,
    QVBoxLayout,
    QHBoxLayout,
    QGraphicsView,
    QGraphicsScene,
    QGraphicsPixmapItem,
    QPushButton,
    QFileDialog,
    QLabel,
)
from qfluentwidgets import isDarkTheme


class FullImageViewer(QDialog):
    """全屏图片查看器 - 支持缩放/拖拽"""

    def __init__(self, url: str, parent=None):
        super().__init__(parent)
        self._url = url
        self._pixmap: QPixmap = QPixmap()
        self._pixmap_item: QGraphicsPixmapItem | None = None
        self._zoom = 1.0

        self._init_ui()
        self._load_full_image()

    def _init_ui(self):
        self.setWindowTitle("图片查看")
        self.resize(900, 700)

        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(0)

        # 工具栏
        toolbar = QHBoxLayout()
        toolbar.setContentsMargins(8, 6, 8, 6)
        toolbar.setSpacing(6)

        self._status_label = QLabel("加载中...")
        toolbar.addWidget(self._status_label)
        toolbar.addStretch()

        btn_zoom_in = QPushButton("放大")
        btn_zoom_in.clicked.connect(self._zoom_in)
        toolbar.addWidget(btn_zoom_in)

        btn_zoom_out = QPushButton("缩小")
        btn_zoom_out.clicked.connect(self._zoom_out)
        toolbar.addWidget(btn_zoom_out)

        btn_fit = QPushButton("适应窗口")
        btn_fit.clicked.connect(self._fit_to_window)
        toolbar.addWidget(btn_fit)

        btn_actual = QPushButton("实际大小")
        btn_actual.clicked.connect(self._actual_size)
        toolbar.addWidget(btn_actual)

        btn_save = QPushButton("另存为...")
        btn_save.clicked.connect(self._save_as)
        toolbar.addWidget(btn_save)

        btn_close = QPushButton("关闭")
        btn_close.clicked.connect(self.close)
        toolbar.addWidget(btn_close)

        layout.addLayout(toolbar)

        # 图片视图
        self._view = QGraphicsView()
        self._scene = QGraphicsScene(self)
        self._view.setScene(self._scene)
        self._view.setRenderHint(QPainter.RenderHint.SmoothPixmapTransform)
        self._view.setDragMode(QGraphicsView.DragMode.ScrollHandDrag)
        self._view.setTransformationAnchor(QGraphicsView.ViewportAnchor.AnchorUnderMouse)
        layout.addWidget(self._view)

        self._apply_theme()

    def _load_full_image(self):
        """异步加载原图"""
        from ui.chat.media.image_loader import ImageLoaderManager

        ImageLoaderManager().get_full_image(
            self._url,
            self._on_image_loaded,
            self._on_load_failed,
        )

    def _on_image_loaded(self, url: str, pixmap: QPixmap):
        if url != self._url:
            return

        self._pixmap = pixmap
        self._pixmap_item = QGraphicsPixmapItem(pixmap)
        self._scene.addItem(self._pixmap_item)
        self._scene.setSceneRect(QRectF(pixmap.rect()))

        self._status_label.setText(
            f"{pixmap.width()}x{pixmap.height()} | {self._url[-40:]}"
        )
        self._fit_to_window()

    def _on_load_failed(self, url: str):
        self._status_label.setText("图片加载失败")
        # 尝试直接用浏览器打开
        from PyQt6.QtGui import QDesktopServices
        from PyQt6.QtCore import QUrl

        QDesktopServices.openUrl(QUrl(self._url))

    def _zoom_in(self):
        if not self._pixmap_item:
            return
        self._zoom *= 1.2
        self._view.scale(1.2, 1.2)

    def _zoom_out(self):
        if not self._pixmap_item:
            return
        self._zoom /= 1.2
        self._view.scale(1 / 1.2, 1 / 1.2)

    def _fit_to_window(self):
        if not self._pixmap or self._pixmap.isNull():
            return
        self._view.fitInView(QRectF(self._pixmap.rect()), Qt.AspectRatioMode.KeepAspectRatio)
        self._zoom = 1.0

    def _actual_size(self):
        if not self._pixmap_item:
            return
        self._view.resetTransform()
        self._zoom = 1.0

    def _save_as(self):
        if self._pixmap.isNull():
            return

        default_name = "image.png"
        try:
            import hashlib

            url_hash = hashlib.md5(self._url.encode()).hexdigest()[:8]
            default_name = f"pdd_{url_hash}.png"
        except Exception:
            pass

        path, _ = QFileDialog.getSaveFileName(
            self, "保存图片", default_name, "PNG 图片 (*.png);;JPEG 图片 (*.jpg)"
        )
        if path:
            if path.lower().endswith(".jpg"):
                self._pixmap.save(path, "JPG", quality=95)
            else:
                self._pixmap.save(path, "PNG")

    def keyPressEvent(self, event: QKeyEvent):
        if event.key() == Qt.Key.Key_Escape:
            self.close()
        super().keyPressEvent(event)

    def wheelEvent(self, event):
        if event.modifiers() & Qt.KeyboardModifier.ControlModifier:
            delta = event.angleDelta().y()
            if delta > 0:
                self._zoom_in()
            else:
                self._zoom_out()
        super().wheelEvent(event)

    def _apply_theme(self):
        dark = isDarkTheme()
        bg = "#1e1e1e" if dark else "#f5f5f5"
        fg = "#e0e0e0" if dark else "#333333"
        self.setStyleSheet(f"background-color: {bg}; color: {fg};")
