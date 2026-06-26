"""
视频播放器对话框

- 先下载视频到本地 temp/media_cache/ 再播放
- 使用 StandardMediaPlayBar（固定底栏）+ QVideoWidget（画面渲染）
- 支持另存为
- ESC 关闭
- 下载进度提示
"""

import hashlib
import os
from typing import Optional

import requests
from PyQt6.QtCore import Qt, QThread, pyqtSignal, QUrl, QTimer
from PyQt6.QtGui import QKeyEvent
from PyQt6.QtMultimedia import QMediaPlayer, QAudioOutput
from PyQt6.QtMultimediaWidgets import QVideoWidget
from PyQt6.QtWidgets import (
    QDialog,
    QVBoxLayout,
    QHBoxLayout,
    QPushButton,
    QLabel,
    QFileDialog,
    QProgressBar,
)
from qfluentwidgets import isDarkTheme
from qfluentwidgets.multimedia import StandardMediaPlayBar

from utils.runtime_path import ensure_temp_dir


# 视频缓存目录（复用图片缓存的目录）
_VIDEO_CACHE_DIR = ensure_temp_dir("media_cache")


def _video_url_to_cache_path(url: str, ext: str = ".mp4") -> str:
    """视频 URL → 本地缓存文件路径"""
    url_hash = hashlib.md5(url.encode("utf-8")).hexdigest()
    return str(_VIDEO_CACHE_DIR / f"video_{url_hash}{ext}")


class VideoDownloadWorker(QThread):
    """后台下载视频文件"""

    progress = pyqtSignal(int, int)  # downloaded_bytes, total_bytes
    finished_ok = pyqtSignal(str)    # local_path
    finished_err = pyqtSignal(str)   # error_message

    def __init__(self, url: str, cache_path: str, parent=None):
        super().__init__(parent)
        self._url = url
        self._cache_path = cache_path

    def run(self):
        try:
            # 如果缓存已有文件，直接返回
            if os.path.exists(self._cache_path) and os.path.getsize(self._cache_path) > 0:
                self.finished_ok.emit(self._cache_path)
                return

            resp = requests.get(self._url, timeout=30, stream=True)
            resp.raise_for_status()

            total = int(resp.headers.get("content-length", 0))
            downloaded = 0
            chunk_size = 64 * 1024  # 64KB

            tmp_path = self._cache_path + ".tmp"
            with open(tmp_path, "wb") as f:
                for chunk in resp.iter_content(chunk_size=chunk_size):
                    if chunk:
                        f.write(chunk)
                        downloaded += len(chunk)
                        if total > 0:
                            self.progress.emit(downloaded, total)

            # 下载完成，重命名
            os.rename(tmp_path, self._cache_path)
            self.finished_ok.emit(self._cache_path)

        except Exception as e:
            self.finished_err.emit(str(e))


class VideoPlayerDialog(QDialog):
    """视频播放器对话框 — 下载到本地后播放，固定底栏控制"""

    def __init__(self, url: str, parent=None):
        super().__init__(parent)
        self._url = url
        self._local_path: Optional[str] = None
        self._download_worker: Optional[VideoDownloadWorker] = None

        self._init_ui()
        self._start_download()

    def _init_ui(self):
        self.setWindowTitle("视频播放")
        self.resize(800, 600)

        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(0)

        # 状态/进度区域（下载时显示，播放时隐藏）
        self._status_layout = QVBoxLayout()
        self._status_layout.setContentsMargins(12, 8, 12, 8)
        self._status_layout.setSpacing(4)

        self._status_label = QLabel("正在下载视频...")
        self._status_label.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self._status_layout.addWidget(self._status_label)

        self._progress_bar = QProgressBar()
        self._progress_bar.setRange(0, 100)
        self._progress_bar.setValue(0)
        self._status_layout.addWidget(self._progress_bar)

        self._status_container = QVBoxLayout()
        self._status_container.addLayout(self._status_layout)
        layout.addLayout(self._status_container)

        # 视频画面区域（下载完成后才显示）
        self._video_widget = QVideoWidget(self)
        self._video_widget.hide()
        layout.addWidget(self._video_widget, 1)  # stretch=1，占满剩余空间

        # 固定底栏：播放控制条
        self._play_bar = StandardMediaPlayBar(self)
        self._play_bar.hide()
        layout.addWidget(self._play_bar, 0)  # stretch=0，固定高度

        # 底部工具栏（URL + 另存为 + 关闭）
        toolbar = QHBoxLayout()
        toolbar.setContentsMargins(8, 4, 8, 6)
        toolbar.setSpacing(6)

        self._url_label = QLabel(self._truncate_url(self._url))
        self._url_label.setMaximumWidth(400)
        toolbar.addWidget(self._url_label)
        toolbar.addStretch()

        btn_save = QPushButton("另存为...")
        btn_save.clicked.connect(self._save_as)
        toolbar.addWidget(btn_save)

        btn_close = QPushButton("关闭")
        btn_close.clicked.connect(self.close)
        toolbar.addWidget(btn_close)

        layout.addLayout(toolbar)

        self._apply_theme()

    def _start_download(self):
        """启动后台下载"""
        cache_path = _video_url_to_cache_path(self._url)
        self._download_worker = VideoDownloadWorker(self._url, cache_path)

        def _on_progress(downloaded: int, total: int):
            pct = int(downloaded * 100 / total) if total > 0 else 0
            self._progress_bar.setValue(pct)
            self._status_label.setText(
                f"正在下载视频... {pct}% ({self._fmt_size(downloaded)}/{self._fmt_size(total)})"
            )

        def _on_ok(local_path: str):
            self._local_path = local_path
            self._on_download_complete(local_path)

        def _on_err(msg: str):
            self._status_label.setText(f"视频下载失败: {msg}")
            self._progress_bar.hide()
            # 尝试直接用 URL 播放（网络流）
            self._status_label.setText("下载失败，尝试在线播放...")
            QTimer.singleShot(500, lambda: self._play_url(self._url))

        self._download_worker.progress.connect(_on_progress)
        self._download_worker.finished_ok.connect(_on_ok)
        self._download_worker.finished_err.connect(_on_err)
        self._download_worker.start()

    def _on_download_complete(self, local_path: str):
        """下载完成，切换到播放界面"""
        self._status_label.hide()
        self._progress_bar.hide()

        # 显示视频画面和播放控制条
        self._video_widget.show()
        self._play_bar.show()
        self._play_local(local_path)

    def _play_local(self, local_path: str):
        """播放本地视频文件"""
        url = QUrl.fromLocalFile(local_path)
        # 使用 play_bar 内置的 MediaPlayer，绑定视频输出到 QVideoWidget
        self._play_bar.player.setVideoOutput(self._video_widget)
        self._play_bar.player.setSource(url)
        self._play_bar.play()

    def _play_url(self, url: str):
        """直接用网络 URL 播放"""
        self._video_widget.show()
        self._play_bar.show()
        qurl = QUrl(url)
        self._play_bar.player.setVideoOutput(self._video_widget)
        self._play_bar.player.setSource(qurl)
        self._play_bar.play()

    def _save_as(self):
        """另存视频到用户指定路径"""
        if not self._local_path or not os.path.exists(self._local_path):
            # 下载中或下载失败
            if self._local_path is None:
                self._status_label.setText("视频尚未下载完成，请稍候...")
            return

        default_name = os.path.basename(self._local_path)
        path, _ = QFileDialog.getSaveFileName(
            self, "保存视频", default_name, "MP4 视频 (*.mp4);;所有文件 (*.*)"
        )
        if path:
            try:
                import shutil
                shutil.copy2(self._local_path, path)
                self._status_label.setText(f"已保存到: {path}")
            except Exception as e:
                self._status_label.setText(f"保存失败: {e}")

    def keyPressEvent(self, event: QKeyEvent):
        if event.key() == Qt.Key.Key_Escape:
            self.close()
        super().keyPressEvent(event)

    def closeEvent(self, event):
        """关闭时停止播放和清理 worker"""
        try:
            self._play_bar.stop()
        except Exception:
            pass
        if self._download_worker and self._download_worker.isRunning():
            self._download_worker.terminate()
            self._download_worker.wait(3000)
        super().closeEvent(event)

    @staticmethod
    def _fmt_size(b: int) -> str:
        if b < 1024:
            return f"{b}B"
        elif b < 1024 * 1024:
            return f"{b / 1024:.1f}KB"
        elif b < 1024 * 1024 * 1024:
            return f"{b / (1024 * 1024):.1f}MB"
        return f"{b / (1024 * 1024 * 1024):.2f}GB"

    @staticmethod
    def _truncate_url(url: str, max_len: int = 60) -> str:
        if len(url) <= max_len:
            return url
        return url[:max_len - 3] + "..."

    def _apply_theme(self):
        dark = isDarkTheme()
        bg = "#1e1e1e" if dark else "#f5f5f5"
        fg = "#e0e0e0" if dark else "#333333"
        self.setStyleSheet(f"background-color: {bg}; color: {fg};")
