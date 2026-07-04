"""
异步图片加载器 - QThread + requests + 磁盘缓存

策略:
- 缩略图/原图独立内存缓存 (LRU, 上限各100条)
- 缩略图/原图独立磁盘缓存 (thumb_ / full_ 前缀)
- 未命中 → requests.get 下载 → Pillow 缩放(可选) → 存缓存 → 回调
"""

import hashlib
import io
import os
from typing import Callable, Optional
from collections import OrderedDict

from PyQt6.QtCore import QThread, pyqtSignal
from PyQt6.QtGui import QPixmap, QImage

import requests
from PIL import Image

from utils.runtime_path import ensure_temp_dir


# 磁盘缓存目录
_CACHE_DIR = ensure_temp_dir("media_cache")
# 缩略图内存缓存 (LRU)
_THUMB_MEMORY_CACHE: OrderedDict = OrderedDict()
# 原图内存缓存 (LRU)
_FULL_MEMORY_CACHE: OrderedDict = OrderedDict()
_MEMORY_CACHE_MAX = 100


def _url_to_cache_path(url: str, prefix: str = "") -> str:
    """URL → 磁盘缓存文件路径"""
    url_hash = hashlib.md5(url.encode("utf-8")).hexdigest()
    return str(_CACHE_DIR / f"{prefix}{url_hash}.png")


class ImageLoadWorker(QThread):
    """后台下载+缩放图片，通过信号回传图片字节数据

    thumb=True: 缩略图缓存 (thumb_ 前缀)
    thumb=False: 原图缓存 (full_ 前缀)

    注意：QPixmap 是 GUI 资源，不能在非 GUI 线程创建。
    本 worker 只在后台线程做网络下载和 Pillow 处理，将 PNG bytes
    通过信号传到主线程，由主线程创建 QPixmap，避免 access violation。
    """

    finished_ok = pyqtSignal(str, bytes)  # url, png_bytes (成功)
    finished_err = pyqtSignal(str)  # url (失败)

    def __init__(self, url: str, target_size: tuple, thumb: bool = True, parent=None):
        super().__init__(parent)
        self._url = url
        self._target_size = target_size  # (max_w, max_h)
        self._thumb = thumb

    @property
    def _cache_prefix(self) -> str:
        return "thumb_" if self._thumb else "full_"

    def run(self):
        try:
            png_bytes = self._load_image_bytes()
            if png_bytes is not None:
                self.finished_ok.emit(self._url, png_bytes)
            else:
                self.finished_err.emit(self._url)
        except Exception:
            self.finished_err.emit(self._url)

    def _load_image_bytes(self) -> Optional[bytes]:
        """加载图片: 内存缓存 → 磁盘缓存 → 网络下载，返回 PNG bytes

        注意：内存缓存中存储的是 bytes 而非 QPixmap，因为 QPixmap 不能跨线程使用。
        QPixmap 的创建延迟到主线程的回调中完成。
        """
        mem_cache = _THUMB_MEMORY_CACHE if self._thumb else _FULL_MEMORY_CACHE

        # 1. 内存缓存 (bytes)
        if self._url in mem_cache:
            mem_cache.move_to_end(self._url)
            return mem_cache[self._url]

        # 2. 磁盘缓存 → 读 bytes
        cache_path = _url_to_cache_path(self._url, self._cache_prefix)
        if os.path.exists(cache_path):
            with open(cache_path, "rb") as f:
                data = f.read()
            if data:
                self._store_memory(self._url, data)
                return data

        # 3. 网络下载 → Pillow 缩放(仅缩略图) → PNG bytes → 存磁盘缓存
        resp = requests.get(self._url, timeout=15)
        resp.raise_for_status()
        img_data = resp.content

        pil_img = Image.open(io.BytesIO(img_data))
        if self._thumb:
            pil_img = self._scale_pil(pil_img, self._target_size)

        buf = io.BytesIO()
        pil_img.save(buf, format="PNG")
        png_bytes = buf.getvalue()

        # 存磁盘缓存
        with open(cache_path, "wb") as f:
            f.write(png_bytes)

        self._store_memory(self._url, png_bytes)
        return png_bytes

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

    def _store_memory(self, url: str, data: bytes):
        """存入内存缓存 (LRU)"""
        mem_cache = _THUMB_MEMORY_CACHE if self._thumb else _FULL_MEMORY_CACHE
        mem_cache[url] = data
        mem_cache.move_to_end(url)
        while len(mem_cache) > _MEMORY_CACHE_MAX:
            mem_cache.popitem(last=False)


class ImageLoaderManager:
    """单例管理器: 独立缩略图/原图内存缓存 + 管理活跃 worker

    内存缓存存储 PNG bytes（而非 QPixmap），因为 QPixmap 是 GUI 资源，
    不能在后台线程创建。QPixmap 在主线程从 bytes 按需创建。
    """

    _instance: Optional["ImageLoaderManager"] = None

    def __new__(cls):
        if cls._instance is None:
            cls._instance = super().__new__(cls)
            cls._instance._workers = []  # type: ignore
        return cls._instance

    @staticmethod
    def _bytes_to_pixmap(data: bytes) -> QPixmap:
        """在主线程中从 PNG bytes 创建 QPixmap"""
        qimg = QImage()
        qimg.loadFromData(data, "PNG")
        return QPixmap.fromImage(qimg)

    def get_pixmap(
        self,
        url: str,
        target_size: tuple,
        callback: Callable[[str, QPixmap], None],
        error_callback: Optional[Callable[[str], None]] = None,
    ) -> None:
        """
        异步获取图片缩略图。

        先查缩略图内存缓存(bytes)，命中则在主线程转 QPixmap 直接回调；
        否则启动 ImageLoadWorker，worker 在后台线程返回 bytes，
        主线程回调中再转 QPixmap。
        """
        # 缩略图内存缓存命中 (bytes → QPixmap)
        if url in _THUMB_MEMORY_CACHE:
            _THUMB_MEMORY_CACHE.move_to_end(url)
            pixmap = self._bytes_to_pixmap(_THUMB_MEMORY_CACHE[url])
            callback(url, pixmap)
            return

        # 启动 worker (thumb=True) — worker 返回 bytes
        worker = ImageLoadWorker(url, target_size, thumb=True)

        def _on_finished_ok(u: str, data: bytes):
            # 在主线程中从 bytes 创建 QPixmap
            pixmap = self._bytes_to_pixmap(data)
            if not pixmap.isNull():
                callback(u, pixmap)
            else:
                if error_callback:
                    error_callback(u)
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
        """清空所有内存缓存 (磁盘缓存保留)"""
        _THUMB_MEMORY_CACHE.clear()
        _FULL_MEMORY_CACHE.clear()

    def get_full_image(
        self,
        url: str,
        callback: Callable[[str, QPixmap], None],
        error_callback: Optional[Callable[[str], None]] = None,
    ) -> None:
        """
        异步获取原图 (不缩放)。

        先查原图内存缓存(bytes)，命中则在主线程转 QPixmap 直接回调；
        否则启动 ImageLoadWorker，worker 在后台线程返回 bytes，
        主线程回调中再转 QPixmap。
        注意：原图和缩略图使用独立缓存，互不污染。
        """
        # 原图内存缓存命中 (bytes → QPixmap)
        if url in _FULL_MEMORY_CACHE:
            _FULL_MEMORY_CACHE.move_to_end(url)
            pixmap = self._bytes_to_pixmap(_FULL_MEMORY_CACHE[url])
            callback(url, pixmap)
            return

        # 启动 worker (thumb=False, 不缩放) — worker 返回 bytes
        worker = ImageLoadWorker(url, (9999, 9999), thumb=False)

        def _on_finished_ok(u: str, data: bytes):
            # 在主线程中从 bytes 创建 QPixmap
            pixmap = self._bytes_to_pixmap(data)
            if not pixmap.isNull():
                callback(u, pixmap)
            else:
                if error_callback:
                    error_callback(u)
            self._cleanup_worker(worker)

        def _on_finished_err(u: str):
            if error_callback:
                error_callback(u)
            self._cleanup_worker(worker)

        worker.finished_ok.connect(_on_finished_ok)
        worker.finished_err.connect(_on_finished_err)
        self._workers.append(worker)  # type: ignore
        worker.start()
