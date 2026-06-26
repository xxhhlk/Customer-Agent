"""
异步图片加载器 - QThread + requests + 磁盘缓存

策略:
- 内存缓存 (LRU, 上限100条 QPixmap)
- 磁盘缓存 (temp/media_cache/, 文件名用 URL 的 MD5)
- 未命中 → requests.get 下载 → Pillow 缩放 → 存缓存 → 回调
"""

import hashlib
import io
import os
from typing import Callable, Optional
from collections import OrderedDict

from PyQt6.QtCore import QThread, pyqtSignal, Qt
from PyQt6.QtGui import QPixmap, QImage

import requests
from PIL import Image
from PIL.Image import DecompressionBombError

from utils.runtime_path import ensure_temp_dir


# 磁盘缓存目录
_CACHE_DIR = ensure_temp_dir("media_cache")
_MEMORY_CACHE: OrderedDict = OrderedDict()
_MEMORY_CACHE_MAX = 100


def _url_to_cache_path(url: str) -> str:
    """URL → 磁盘缓存文件路径"""
    url_hash = hashlib.md5(url.encode("utf-8")).hexdigest()
    return str(_CACHE_DIR / f"{url_hash}.png")


class ImageLoadWorker(QThread):
    """后台下载+缩放图片，通过信号回传 QPixmap"""

    finished_ok = pyqtSignal(str, QPixmap)  # url, pixmap (成功)
    finished_err = pyqtSignal(str)  # url (失败)

    def __init__(self, url: str, target_size: tuple, parent=None):
        super().__init__(parent)
        self._url = url
        self._target_size = target_size  # (max_w, max_h)

    def run(self):
        try:
            pixmap = self._load_pixmap()
            if pixmap is not None and not pixmap.isNull():
                self.finished_ok.emit(self._url, pixmap)
            else:
                self.finished_err.emit(self._url)
        except Exception:
            self.finished_err.emit(self._url)

    def _load_pixmap(self) -> Optional[QPixmap]:
        """加载图片: 内存缓存 → 磁盘缓存 → 网络下载"""
        # 1. 内存缓存
        if self._url in _MEMORY_CACHE:
            _MEMORY_CACHE.move_to_end(self._url)
            return _MEMORY_CACHE[self._url]

        # 2. 磁盘缓存
        cache_path = _url_to_cache_path(self._url)
        if os.path.exists(cache_path):
            pixmap = QPixmap(cache_path)
            if not pixmap.isNull():
                self._store_memory(self._url, pixmap)
                return pixmap

        # 3. 网络下载
        resp = requests.get(self._url, timeout=15)
        resp.raise_for_status()
        img_data = resp.content

        # 用 Pillow 打开 → 缩放 → 存磁盘缓存
        pil_img = Image.open(io.BytesIO(img_data))
        pil_img = self._scale_pil(pil_img, self._target_size)

        # 存磁盘缓存 (PNG 格式)
        buf = io.BytesIO()
        pil_img.save(buf, format="PNG")
        buf.seek(0)
        with open(cache_path, "wb") as f:
            f.write(buf.getvalue())

        # 转 QPixmap
        qimg = QImage()
        qimg.loadFromData(buf.getvalue(), "PNG")
        pixmap = QPixmap.fromImage(qimg)

        if not pixmap.isNull():
            self._store_memory(self._url, pixmap)

        return pixmap

    @staticmethod
    def _scale_pil(img: Image.Image, target_size: tuple) -> Image.Image:
        """用 Pillow 按比例缩放到 target_size 以内"""
        max_w, max_h = target_size
        w, h = img.size
        if w <= max_w and h <= max_h:
            return img

        ratio = min(max_w / w, max_h / h)
        new_w = int(w * ratio)
        new_h = int(h * ratio)
        return img.resize((new_w, new_h), Image.LANCZOS)

    @staticmethod
    def _store_memory(url: str, pixmap: QPixmap):
        """存入内存缓存 (LRU)"""
        _MEMORY_CACHE[url] = pixmap
        _MEMORY_CACHE.move_to_end(url)
        while len(_MEMORY_CACHE) > _MEMORY_CACHE_MAX:
            _MEMORY_CACHE.popitem(last=False)


class ImageLoaderManager:
    """单例管理器: 内存缓存 + 管理活跃 worker"""

    _instance: Optional["ImageLoaderManager"] = None

    def __new__(cls):
        if cls._instance is None:
            cls._instance = super().__new__(cls)
            cls._instance._workers = []  # type: ignore
        return cls._instance

    def get_pixmap(
        self,
        url: str,
        target_size: tuple,
        callback: Callable[[str, QPixmap], None],
        error_callback: Optional[Callable[[str], None]] = None,
    ) -> None:
        """
        异步获取图片缩略图。

        先查内存缓存，命中则直接回调；否则启动 ImageLoadWorker。
        """
        # 内存缓存命中
        if url in _MEMORY_CACHE:
            _MEMORY_CACHE.move_to_end(url)
            callback(url, _MEMORY_CACHE[url])
            return

        # 启动 worker
        worker = ImageLoadWorker(url, target_size)

        def _on_finished_ok(u: str, p: QPixmap):
            callback(u, p)
            self._cleanup_worker(worker)

        def _on_finished_err(u: str):
            if error_callback:
                error_callback(u)
            self._cleanup_worker(worker)

        worker.finished_ok.connect(_on_finished_ok)
        worker.finished_err.connect(_on_finished_err)

        # 保存引用防止 GC
        self._workers.append(worker)  # type: ignore
        worker.start()

    def _cleanup_worker(self, worker: ImageLoadWorker):
        """清理已完成的 worker"""
        try:
            if worker in self._workers:  # type: ignore
                self._workers.remove(worker)  # type: ignore
            worker.deleteLater()
        except Exception:
            pass

    def clear_cache(self) -> None:
        """清空内存缓存 (磁盘缓存保留)"""
        _MEMORY_CACHE.clear()

    def get_full_image(
        self,
        url: str,
        callback: Callable[[str, QPixmap], None],
        error_callback: Optional[Callable[[str], None]] = None,
    ) -> None:
        """
        异步获取原图 (不缩放)。
        先查磁盘缓存，命中则直接加载；否则下载原图。
        """
        # 内存缓存命中
        if url in _MEMORY_CACHE:
            _MEMORY_CACHE.move_to_end(url)
            callback(url, _MEMORY_CACHE[url])
            return

        # 启动 worker (用大 target_size 确保不缩放)
        worker = ImageLoadWorker(url, (9999, 9999))

        def _on_finished_ok(u: str, p: QPixmap):
            callback(u, p)
            self._cleanup_worker(worker)

        def _on_finished_err(u: str):
            if error_callback:
                error_callback(u)
            self._cleanup_worker(worker)

        worker.finished_ok.connect(_on_finished_ok)
        worker.finished_err.connect(_on_finished_err)
        self._workers.append(worker)  # type: ignore
        worker.start()
