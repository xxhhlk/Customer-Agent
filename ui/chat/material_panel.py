"""素材面板（MaterialPopup）
============================
浮层，挂在聊天输入框上方。从拼多多「图片空间 / 视频空间」挑选素材，点击后
由上层（ChatArea → ChatUI）直接发给当前会话买家。

- 双 Tab：图片空间（`dir_id` 省略=全店）/ 视频空间（`dir_id=-1`，客服专用视频）
- 支持文件名搜索、分页、审核状态标记
- 列表拉取全部在 QThread 后台完成（含 MaterialSpace 的 cookie 读取，禁止在 UI 线程建）
- 缩略图复用 `ui.chat.media.image_loader.ImageLoaderManager`（QPixmap 仅在主线程创建）
- 禁用 QMessageBox（项目铁律）

接口与字段说明见 `docs/material-space-send-research-2026-10-11.md`。
"""

import time
from typing import Any, Dict, List, Optional, Tuple

from PyQt6.QtCore import Qt, QThread, QTimer, pyqtSignal
from PyQt6.QtWidgets import (
    QFrame, QVBoxLayout, QHBoxLayout, QGridLayout, QScrollArea, QWidget,
    QLabel, QLineEdit, QTabBar, QSizePolicy, QToolButton,
)
from PyQt6.QtGui import QPixmap
from qfluentwidgets import PushButton, TransparentToolButton, FluentIcon, isDarkTheme

from utils.logger_loguru import get_logger

logger = get_logger("MaterialPanel")

# 列表缓存 TTL（秒）：避免每次打开面板都打接口
_CACHE_TTL = 180

# (space, page, keyword) -> (timestamp, data)
_LIST_CACHE: Dict[Tuple[str, int, str], Tuple[float, Dict[str, Any]]] = {}

_GRID_COLS = 4
_PAGE_SIZE = 24
_THUMB_SIZE = 96


def _clear_cache() -> None:
    """清空素材列表缓存（刷新按钮 / 账号切换时调用）"""
    _LIST_CACHE.clear()


class _MaterialLoader(QThread):
    """后台拉取素材列表（含 cookie 读取，禁止在 UI 线程执行）"""

    result = pyqtSignal(str, int, str, object, str)  # space, page, keyword, data|None, error

    def __init__(self, shop_id: str, user_id: str, space: str, page: int,
                 page_size: int, keyword: str, parent=None):
        super().__init__(parent)
        self._shop_id = shop_id
        self._user_id = user_id
        self._space = space
        self._page = page
        self._page_size = page_size
        self._keyword = keyword

    @property
    def key(self) -> Tuple[str, int, str]:
        """本次请求的标识，用于判断"同一请求是否已在飞行中" """
        return (self._space, self._page, self._keyword)

    def run(self):
        try:
            from Channel.pinduoduo.utils.API.material_space import MaterialSpace
            ms = MaterialSpace(str(self._shop_id), str(self._user_id))
            data = ms.list_files(
                space=self._space,
                page=self._page,
                page_size=self._page_size,
                keyword=self._keyword,
            )
            if not data.get("success"):
                self.result.emit(self._space, self._page, self._keyword, None,
                                 data.get("error_msg") or "获取素材失败")
                return
            _LIST_CACHE[(self._space, self._page, self._keyword)] = (time.time(), data)
            self.result.emit(self._space, self._page, self._keyword, data, "")
        except Exception as e:  # noqa: BLE001
            logger.error(f"拉取素材列表异常: {e}", exc_info=True)
            self.result.emit(self._space, self._page, self._keyword, None, str(e))


class MaterialCard(QFrame):
    """单个素材卡片：缩略图 + 名称 + 角标"""

    clicked = pyqtSignal(dict)

    def __init__(self, item: Dict[str, Any], sendable: bool, parent=None):
        super().__init__(parent)
        self.item = item
        self.sendable = sendable
        self._pixmap_url = ""
        self.setFixedSize(118, 138)
        self.setCursor(Qt.CursorShape.PointingHandCursor if sendable else Qt.CursorShape.ForbiddenCursor)
        self._build_ui()
        self.apply_theme()

    def _build_ui(self):
        layout = QVBoxLayout(self)
        layout.setContentsMargins(6, 6, 6, 6)
        layout.setSpacing(3)

        # 缩略图
        self._thumb = QLabel()
        self._thumb.setObjectName("MaterialThumb")
        self._thumb.setFixedSize(_THUMB_SIZE, _THUMB_SIZE)
        self._thumb.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self._thumb.setText("载入中")
        layout.addWidget(self._thumb, 0, Qt.AlignmentFlag.AlignHCenter)

        # 名称（过长省略）
        name = self.item.get("name") or "(未命名)"
        self._name_label = QLabel(self._elide(name, 13))
        self._name_label.setObjectName("MaterialName")
        self._name_label.setAlignment(Qt.AlignmentFlag.AlignHCenter)
        layout.addWidget(self._name_label)

        # 角标行：时长/大小 + 不可发送标记
        meta = self._format_meta()
        self._meta_label = QLabel(meta)
        self._meta_label.setObjectName("MaterialMeta")
        self._meta_label.setAlignment(Qt.AlignmentFlag.AlignHCenter)
        layout.addWidget(self._meta_label)

        self.setToolTip(self._build_tooltip())

    def _build_tooltip(self) -> str:
        it = self.item
        lines = [
            f"名称：{it.get('name') or '(未命名)'}",
            f"类型：{'视频' if it.get('file_type') == 'video' else '图片'}",
            f"ID：{it.get('id')}",
        ]
        if it.get("size"):
            lines.append(f"大小：{it['size'] / 1048576:.2f} MB")
        if it.get("duration"):
            lines.append(f"时长：{self._format_duration(it['duration'])}")
        if it.get("width") and it.get("height"):
            lines.append(f"尺寸：{it['width']}x{it['height']}")
        if it.get("dir_names"):
            lines.append(f"文件夹：{', '.join(it['dir_names'])}")
        if not self.sendable:
            lines.append("⚠ 该素材未通过审核，不可发送")
        return "\n".join(lines)

    def _format_meta(self) -> str:
        if not self.sendable:
            return "不可发送"
        it = self.item
        if it.get("file_type") == "video":
            parts = []
            if it.get("duration"):
                parts.append(self._format_duration(it["duration"]))
            if it.get("size"):
                parts.append(f"{it['size'] / 1048576:.1f}MB")
            return " · ".join(parts) if parts else "视频"
        if it.get("size"):
            return f"{it['size'] / 1024:.0f} KB"
        return "图片"

    @staticmethod
    def _format_duration(seconds: Any) -> str:
        try:
            total = int(seconds)
        except (TypeError, ValueError):
            return "00:00"
        return f"{total // 60:02d}:{total % 60:02d}"

    @staticmethod
    def _elide(text: str, limit: int) -> str:
        text = text.strip()
        return text if len(text) <= limit else text[: limit - 1] + "…"

    def thumbnail_url(self) -> str:
        """缩略图 URL：图片用素材 url，视频用封面"""
        it = self.item
        if it.get("file_type") == "video":
            return it.get("cover_url") or ""
        return it.get("url") or it.get("transcode_url") or ""

    def set_thumb(self, pixmap: QPixmap) -> None:
        try:
            if pixmap is None or pixmap.isNull():
                self._thumb.setText("无预览")
                return
            scaled = pixmap.scaled(
                _THUMB_SIZE, _THUMB_SIZE,
                Qt.AspectRatioMode.KeepAspectRatio,
                Qt.TransformationMode.SmoothTransformation,
            )
            self._thumb.setPixmap(scaled)
            self._thumb.setText("")
        except RuntimeError:
            # 卡片已被 deleteLater
            pass

    def set_thumb_failed(self) -> None:
        try:
            self._thumb.setText("加载失败")
        except RuntimeError:
            pass

    def _build_theme_colors(self):
        dark = isDarkTheme()
        return {
            "bg": "#2b2b2b" if dark else "#ffffff",
            "border": "#3a3a3a" if dark else "#e0e0e0",
            "hover": "#333333" if dark else "#f0f7ff",
            "text": "#e0e0e0" if dark else "#333333",
            "sub": "#999999" if dark else "#888888",
            "thumb_bg": "#1e1e1e" if dark else "#f5f5f5",
        }

    def apply_theme(self):
        c = self._build_theme_colors()
        self.setStyleSheet(f"""
            MaterialCard {{
                background-color: {c['bg']};
                border: 1px solid {c['border']};
                border-radius: 8px;
            }}
            MaterialCard:hover {{
                background-color: {c['hover']};
                border-color: #4a90d9;
            }}
            #MaterialThumb {{
                background-color: {c['thumb_bg']};
                color: {c['sub']};
                border-radius: 6px;
                font-size: 11px;
            }}
            #MaterialName {{
                color: {c['text']};
                font-size: 11px;
                background: transparent;
            }}
            #MaterialMeta {{
                color: {c['sub']};
                font-size: 10px;
                background: transparent;
            }}
        """)

    def mousePressEvent(self, event):  # noqa: N802
        if event.button() == Qt.MouseButton.LeftButton:
            if self.sendable:
                self.clicked.emit(self.item)
            else:
                logger.info(f"素材未通过审核，忽略点击: id={self.item.get('id')}")
        super().mousePressEvent(event)


class MaterialPopup(QFrame):
    """素材选择浮窗"""

    material_selected = pyqtSignal(dict)

    def __init__(self, parent=None):
        super().__init__(parent, Qt.WindowType.Popup | Qt.WindowType.FramelessWindowHint)
        self.setObjectName("MaterialPopup")
        self.setFixedSize(560, 460)
        self.setFocusPolicy(Qt.FocusPolicy.NoFocus)

        self._shop_id = ""
        self._user_id = ""
        self._space = "image"
        self._page = 1
        self._keyword = ""
        self._total = 0
        self._items: List[Dict[str, Any]] = []
        self._cards: List[MaterialCard] = []
        self._worker: Optional[_MaterialLoader] = None
        self._render_token = 0  # 每次渲染递增，丢弃过期缩略图回调
        self._pending: List[Tuple[str, MaterialCard]] = []

        self._build_ui()
        self.refresh_theme()

    # ------------------------------------------------------------------ #
    # 构建
    # ------------------------------------------------------------------ #

    def _build_ui(self):
        layout = QVBoxLayout(self)
        layout.setContentsMargins(8, 8, 8, 8)
        layout.setSpacing(6)

        # ---- 顶部：空间切换 + 搜索 + 刷新 ----
        top = QHBoxLayout()
        top.setSpacing(6)

        self._tab_bar = QTabBar()
        self._tab_bar.setObjectName("MaterialTabBar")
        self._tab_bar.addTab("图片空间")
        self._tab_bar.addTab("视频空间")
        self._tab_bar.setExpanding(False)
        self._tab_bar.setDrawBase(False)
        self._tab_bar.currentChanged.connect(self._on_space_changed)
        top.addWidget(self._tab_bar)
        top.addStretch()

        self._search = QLineEdit()
        self._search.setPlaceholderText("搜索文件名…")
        self._search.setFixedWidth(160)
        self._search.returnPressed.connect(self._on_search)
        top.addWidget(self._search)

        self._refresh_btn = TransparentToolButton(FluentIcon.SYNC)
        self._refresh_btn.setToolTip("刷新（忽略缓存）")
        self._refresh_btn.setFixedSize(32, 32)
        self._refresh_btn.clicked.connect(self._on_refresh)
        top.addWidget(self._refresh_btn)

        layout.addLayout(top)

        # ---- 中部：网格 ----
        self._scroll = QScrollArea()
        self._scroll.setWidgetResizable(True)
        self._scroll.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        self._scroll.setFrameShape(QFrame.Shape.NoFrame)

        self._grid_host = QWidget()
        self._grid = QGridLayout(self._grid_host)
        self._grid.setContentsMargins(2, 2, 2, 2)
        self._grid.setSpacing(6)
        for col in range(_GRID_COLS):
            self._grid.setColumnStretch(col, 1)
        self._scroll.setWidget(self._grid_host)
        layout.addWidget(self._scroll, 1)

        # ---- 底部：状态 + 分页 ----
        bottom = QHBoxLayout()
        bottom.setSpacing(6)

        self._status = QLabel("")
        self._status.setObjectName("MaterialStatus")
        bottom.addWidget(self._status)
        bottom.addStretch()

        self._prev_btn = PushButton("上一页")
        self._prev_btn.setFixedWidth(72)
        self._prev_btn.clicked.connect(lambda: self._goto_page(self._page - 1))
        bottom.addWidget(self._prev_btn)

        self._page_label = QLabel("")
        self._page_label.setObjectName("MaterialStatus")
        self._page_label.setMinimumWidth(90)
        self._page_label.setAlignment(Qt.AlignmentFlag.AlignCenter)
        bottom.addWidget(self._page_label)

        self._next_btn = PushButton("下一页")
        self._next_btn.setFixedWidth(72)
        self._next_btn.clicked.connect(lambda: self._goto_page(self._page + 1))
        bottom.addWidget(self._next_btn)

        layout.addLayout(bottom)

    # ------------------------------------------------------------------ #
    # 对外接口
    # ------------------------------------------------------------------ #

    def set_account(self, shop_id: str, user_id: str) -> None:
        """设置当前店铺/账号；切换账号时清空缓存"""
        new_shop, new_user = str(shop_id or ""), str(user_id or "")
        if (new_shop, new_user) != (self._shop_id, self._user_id):
            _clear_cache()
            self._shop_id, self._user_id = new_shop, new_user
            self._reset_state()

    def open_for(self, shop_id: str, user_id: str) -> None:
        """打开面板：设置账号并加载当前页"""
        self.set_account(shop_id, user_id)
        if not self._shop_id or not self._user_id:
            self._set_status("未选择会话或账号信息缺失")
            return
        self._reload(use_cache=True)

    def reload(self) -> None:
        """重新加载（外部调用）"""
        self._reload(use_cache=True)

    # ------------------------------------------------------------------ #
    # 状态
    # ------------------------------------------------------------------ #

    def _reset_state(self):
        self._page = 1
        self._keyword = ""
        self._search.blockSignals(True)
        self._search.clear()
        self._search.blockSignals(False)
        self._items = []
        self._total = 0
        self._clear_grid()

    def _set_status(self, text: str):
        try:
            self._status.setText(text)
        except RuntimeError:
            pass

    def _update_pager(self):
        pages = max(1, (self._total + _PAGE_SIZE - 1) // _PAGE_SIZE)
        self._page_label.setText(f"{self._page} / {pages}")
        self._prev_btn.setEnabled(self._page > 1)
        self._next_btn.setEnabled(self._page < pages)

    # ------------------------------------------------------------------ #
    # 数据加载
    # ------------------------------------------------------------------ #

    def _on_space_changed(self, index: int):
        self._space = "image" if index == 0 else "video"
        self._page = 1
        self._keyword = ""
        self._search.blockSignals(True)
        self._search.clear()
        self._search.blockSignals(False)
        self._reload(use_cache=True)

    def _on_search(self):
        self._keyword = self._search.text().strip()
        self._page = 1
        self._reload(use_cache=True)

    def _on_refresh(self):
        _clear_cache()
        self._reload(use_cache=False)

    def _goto_page(self, page: int):
        if page < 1:
            return
        self._page = page
        self._reload(use_cache=True)

    def _reload(self, use_cache: bool = True):
        if not self._shop_id or not self._user_id:
            self._set_status("缺少账号信息，无法加载素材")
            return

        key = (self._space, self._page, self._keyword)
        if use_cache:
            cached = _LIST_CACHE.get(key)
            if cached and (time.time() - cached[0]) < _CACHE_TTL:
                self._render(cached[1])
                return

        # 同一请求已在飞行中 → 复用，等它回来（结果由 _on_loaded 按 key 匹配）
        worker = self._current_worker()
        if worker is not None:
            try:
                if worker.isRunning() and worker.key == key:
                    self._set_status("加载中…")
                    return
            except RuntimeError:
                # C++ 对象已被销毁（不应发生，兜底）
                self._worker = None

        self._set_status("加载中…")
        self._clear_grid()
        self._page_label.setText("")

        worker = _MaterialLoader(
            self._shop_id, self._user_id, self._space, self._page, _PAGE_SIZE, self._keyword, self
        )
        worker.result.connect(self._on_loaded)
        # 线程结束后清引用，避免后续访问已被 deleteLater 的 C++ 对象
        # （PyQt 中此类 RuntimeError 若从信号槽抛出会被 qFatal → 进程 abort）
        worker.finished.connect(lambda w=worker: self._on_worker_finished(w))
        self._worker = worker
        worker.start()

    def _current_worker(self) -> Optional["_MaterialLoader"]:
        """安全取当前 worker；已被销毁时返回 None 并清引用"""
        w = self._worker
        if w is None:
            return None
        try:
            w.isRunning()
        except RuntimeError:
            self._worker = None
            return None
        return w

    def _on_worker_finished(self, worker: "_MaterialLoader"):
        if self._worker is worker:
            self._worker = None
        try:
            worker.deleteLater()
        except RuntimeError:
            pass

    def _on_loaded(self, space: str, page: int, keyword: str, data: object, error: str):
        # 丢弃过期结果（用户已切换空间/页码/搜索词）
        if (space, page, keyword) != (self._space, self._page, self._keyword):
            return
        if not data:
            self._set_status(f"加载失败：{error[:60]}")
            return
        self._render(data)  # type: ignore[arg-type]

    # ------------------------------------------------------------------ #
    # 渲染
    # ------------------------------------------------------------------ #

    def _clear_grid(self):
        self._render_token += 1
        self._cards = []
        self._pending = []
        while self._grid.count():
            child = self._grid.takeAt(0)
            w = child.widget()
            if w is not None:
                w.setParent(None)
                w.deleteLater()

    def _render(self, data: Dict[str, Any]):
        from Channel.pinduoduo.utils.API.material_space import MaterialSpace

        self._clear_grid()
        self._render_token += 1
        token = self._render_token

        items = data.get("items") or []
        self._items = items
        self._total = int(data.get("total") or len(items))

        self._update_pager()
        space_label = "图片空间" if self._space == "image" else "视频空间"

        if not items:
            self._set_status(f"{space_label}：无素材" + (f"（关键词「{self._keyword}」）" if self._keyword else ""))
            return

        self._set_status(f"{space_label}：共 {self._total} 条，本页 {len(items)} 条")

        for idx, item in enumerate(items):
            card = MaterialCard(item, MaterialSpace.is_sendable(item), self._grid_host)
            card.clicked.connect(self._on_card_clicked)
            self._grid.addWidget(card, idx // _GRID_COLS, idx % _GRID_COLS)
            self._cards.append(card)

            url = card.thumbnail_url()
            if url:
                self._pending.append((url, card))

        self._ensure_bottom_stretch()
        # 缩略图延后一拍再拉，先让网格完成布局（避免一次性启动大批线程卡顿）
        QTimer.singleShot(0, lambda t=token: self._load_thumbs(t))

    def _ensure_bottom_stretch(self):
        """最后一行不足时保持左对齐"""
        rows = (len(self._cards) + _GRID_COLS - 1) // _GRID_COLS
        if rows > 0:
            self._grid.setRowStretch(rows, 1)

    def _load_thumbs(self, token: int):
        if token != self._render_token:
            return
        if not self._pending:
            return
        try:
            from ui.chat.media.image_loader import ImageLoaderManager
        except Exception as e:  # noqa: BLE001
            logger.warning(f"缩略图加载器不可用: {e}")
            return

        manager = ImageLoaderManager()
        pending, self._pending = self._pending, []
        for url, card in pending:
            manager.get_pixmap(
                url,
                (_THUMB_SIZE, _THUMB_SIZE),
                lambda u, pm, c=card, t=token: self._on_thumb_ok(t, c, pm),
                lambda u, c=card, t=token: self._on_thumb_err(t, c),
            )

    def _on_thumb_ok(self, token: int, card: MaterialCard, pixmap: QPixmap):
        if token != self._render_token:
            return
        try:
            card.set_thumb(pixmap)
        except RuntimeError:
            pass

    def _on_thumb_err(self, token: int, card: MaterialCard):
        if token != self._render_token:
            return
        try:
            card.set_thumb_failed()
        except RuntimeError:
            pass

    def _on_card_clicked(self, item: Dict[str, Any]):
        logger.info(
            "素材已选择: id=%s, type=%s, name=%r",
            item.get("id"), item.get("file_type"), item.get("name"),
        )
        self.material_selected.emit(item)
        self.hide()

    # ------------------------------------------------------------------ #
    # 主题
    # ------------------------------------------------------------------ #

    def refresh_theme(self):
        dark = isDarkTheme()
        bg = "#2b2b2b" if dark else "#ffffff"
        border = "#3a3a3a" if dark else "#e0e0e0"
        text = "#e0e0e0" if dark else "#333333"
        sub = "#999999" if dark else "#888888"

        self.setStyleSheet(f"""
            MaterialPopup {{
                background-color: {bg};
                border: 1px solid {border};
                border-radius: 8px;
            }}
            #MaterialStatus {{
                color: {sub};
                background: transparent;
                font-size: 11px;
            }}
            QTabBar::tab {{
                color: {text};
                padding: 4px 10px;
                background: transparent;
            }}
            QTabBar::tab:selected {{
                color: #4a90d9;
                border-bottom: 2px solid #4a90d9;
            }}
            QLineEdit {{
                background-color: {bg};
                color: {text};
                border: 1px solid {border};
                border-radius: 6px;
                padding: 4px 6px;
            }}
            QScrollArea {{
                border: none;
                background-color: {bg};
            }}
        """)
        try:
            self._grid_host.setStyleSheet(f"background-color: {bg};")
        except RuntimeError:
            pass
        for card in list(self._cards):
            try:
                card.apply_theme()
            except RuntimeError:
                pass

    def hideEvent(self, event):  # noqa: N802
        """收起时把未完成的缩略图队列丢掉，避免无意义下载"""
        self._pending = []
        super().hideEvent(event)
