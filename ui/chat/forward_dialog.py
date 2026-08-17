"""
转发目标选择弹窗 - 选择要转发到的目标会话
"""

from PyQt6.QtCore import Qt, pyqtSignal, QThread, QEvent, QTimer
from PyQt6.QtWidgets import (
    QDialog, QVBoxLayout, QHBoxLayout, QLabel, QListWidget, QListWidgetItem,
    QSizePolicy, QFrame,
)
from PyQt6.QtGui import QFont
from PyQt6.QtCore import QMargins
from qfluentwidgets import LineEdit, PrimaryPushButton, PushButton, BodyLabel, isDarkTheme


class _TargetLoader(QThread):
    """后台线程加载会话列表供转发选择"""
    result = pyqtSignal(list)  # [(buyer_uid, nickname, shop_name, msg_count), ...]

    def __init__(self, shop_id: str, exclude_uid: str, parent=None):
        super().__init__(parent)
        self._shop_id = shop_id
        self._exclude_uid = exclude_uid

    def run(self):
        try:
            from services.message_persistence import message_persistence_service
            # 空字符串会让 SQL WHERE c.shop_id = "" 匹配不到任何记录，转为 None
            sid = self._shop_id if self._shop_id else None
            convs = message_persistence_service.get_conversations(
                shop_id=sid, limit=5000
            )
            result = []
            for c in convs:
                uid = c.get("buyer_uid", "")
                if uid == self._exclude_uid:
                    continue
                nickname = (
                    c.get("buyer_nickname")
                    or c.get("nickname")
                    or ""
                )
                # 过滤角色名（历史脏数据可能把 "客服"/"AI客服" 当买家昵称）
                if nickname in ("客服", "AI客服", "mall_cs", "user"):
                    nickname = ""
                nickname = nickname or uid
                shop_name = c.get("shop_name", "")
                msg_count = c.get("msg_count", 0)
                result.append((uid, nickname, shop_name, msg_count))
            # 按最近消息时间排序（conversations 已按 timestamp DESC）
            self.result.emit(result)
        except Exception:
            self.result.emit([])


class ForwardDialog(QDialog):
    """转发目标选择弹窗"""

    selected = pyqtSignal(str, str)  # buyer_uid, nickname

    def __init__(self, shop_id: str, exclude_uid: str, parent=None):
        super().__init__(parent)
        self._shop_id = shop_id
        self._exclude_uid = exclude_uid
        self._loader: _TargetLoader | None = None
        self._all_items: list[tuple[str, str, str, int]] = []  # [(uid, nick, shop, count), ...]
        self._data_loaded = False

        self._init_ui()
        self._apply_theme()
        self._load_targets()

    def _init_ui(self):
        self.setWindowTitle("转发到...")
        self.setMinimumSize(360, 420)
        self.resize(400, 480)

        layout = QVBoxLayout(self)
        layout.setContentsMargins(16, 16, 16, 16)
        layout.setSpacing(10)

        # 标题
        title = BodyLabel("选择转发目标会话")
        title.setFont(QFont("Microsoft YaHei", 13))
        layout.addWidget(title)

        # 搜索框
        self.search_input = LineEdit()
        self.search_input.setPlaceholderText("搜索买家昵称或UID...")
        self.search_input.textChanged.connect(self._on_search)
        layout.addWidget(self.search_input)

        # 会话列表
        self.list_widget = QListWidget()
        self.list_widget.setFrameShape(QFrame.Shape.NoFrame)
        self.list_widget.setSizePolicy(
            QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Expanding
        )
        self.list_widget.itemDoubleClicked.connect(self._on_item_double_clicked)
        layout.addWidget(self.list_widget, 1)

        # 底部按钮
        btn_layout = QHBoxLayout()
        btn_layout.addStretch()

        self.cancel_btn = PushButton("取消")
        self.cancel_btn.clicked.connect(self.reject)
        btn_layout.addWidget(self.cancel_btn)

        self.confirm_btn = PrimaryPushButton("转发")
        self.confirm_btn.setEnabled(False)
        self.confirm_btn.clicked.connect(self._on_confirm)
        btn_layout.addWidget(self.confirm_btn)

        layout.addLayout(btn_layout)

        self.list_widget.currentItemChanged.connect(self._on_selection_changed)

    def _load_targets(self):
        """后台加载会话列表"""
        self.list_widget.clear()
        placeholder = QListWidgetItem("加载中...")
        placeholder.setFlags(Qt.ItemFlag.NoItemFlags)
        self.list_widget.addItem(placeholder)

        if self._loader is not None:
            try:
                self._loader.result.disconnect(self._on_targets_loaded)
            except (TypeError, RuntimeError):
                pass
            self._loader.quit()
            self._loader.wait(500)

        self._loader = _TargetLoader(self._shop_id, self._exclude_uid, self)
        self._loader.result.connect(self._on_targets_loaded)
        self._loader.start()

    def _on_targets_loaded(self, items: list):
        """会话列表加载完成"""
        self._all_items = items
        self._data_loaded = True
        self._rebuild_list(items)

    def _rebuild_list(self, items: list):
        """重建列表显示"""
        self.list_widget.blockSignals(True)
        self.list_widget.clear()

        if not items:
            empty = QListWidgetItem("没有可转发的会话")
            empty.setFlags(Qt.ItemFlag.NoItemFlags)
            self.list_widget.addItem(empty)
            self.confirm_btn.setEnabled(False)
        else:
            for uid, nickname, shop_name, msg_count in items:
                display = f"{nickname} ({uid})"
                if shop_name:
                    display += f" — {shop_name}"
                item = QListWidgetItem(display)
                item.setData(Qt.ItemDataRole.UserRole, uid)
                item.setData(Qt.ItemDataRole.UserRole + 1, nickname)
                item.setSizeHint(item.sizeHint().grownBy(QMargins(0, 4, 0, 4)))
                self.list_widget.addItem(item)

        self.list_widget.blockSignals(False)

    def _on_search(self, text: str):
        """搜索过滤（仅在数据加载完成后生效）"""
        if not self._data_loaded:
            return
        keyword = text.strip().lower()
        if not keyword:
            self._rebuild_list(self._all_items)
            return

        filtered = []
        for uid, nick, shop, count in self._all_items:
            if keyword in nick.lower() or keyword in uid.lower() or keyword in shop.lower():
                filtered.append((uid, nick, shop, count))
        self._rebuild_list(filtered)

    def _on_selection_changed(self, current, previous):
        self.confirm_btn.setEnabled(
            current is not None
            and current.data(Qt.ItemDataRole.UserRole) is not None
        )

    def _on_item_double_clicked(self, item):
        uid = item.data(Qt.ItemDataRole.UserRole)
        if uid:
            self._emit_selected(item)

    def _on_confirm(self):
        item = self.list_widget.currentItem()
        if item:
            self._emit_selected(item)

    def _emit_selected(self, item):
        uid = item.data(Qt.ItemDataRole.UserRole)
        nickname = item.data(Qt.ItemDataRole.UserRole + 1)
        if uid:
            self.selected.emit(uid, nickname)
            self.accept()

    def _apply_theme(self):
        dark = isDarkTheme()
        bg = "#2b2b2b" if dark else "#f5f5f5"
        item_bg = "#333333" if dark else "#ffffff"
        text_color = "#e0e0e0" if dark else "#333333"
        placeholder_color = "#666" if dark else "#aaa"

        self.setStyleSheet(f"""
            QDialog {{
                background-color: {bg};
            }}
        """)
        self.list_widget.setStyleSheet(f"""
            QListWidget {{
                background-color: {item_bg};
                border: 1px solid {"#3a3a3a" if dark else "#e0e0e0"};
                border-radius: 6px;
                color: {text_color};
                font-size: 13px;
            }}
            QListWidget::item {{
                padding: 8px 12px;
                border-bottom: 1px solid {"#3a3a3a" if dark else "#f0f0f0"};
            }}
            QListWidget::item:selected {{
                background-color: {"#2d7db8" if dark else "#cce5ff"};
                color: {"#ffffff" if dark else "#333333"};
            }}
            QListWidget::item:hover {{
                background-color: {"#3a3a3a" if dark else "#f0f8ff"};
            }}
        """)
        self.search_input.setStyleSheet(f"""
            QLineEdit {{
                background-color: {item_bg};
                border: 1px solid {"#3a3a3a" if dark else "#e0e0e0"};
                border-radius: 6px;
                color: {text_color};
                padding: 6px 12px;
                font-size: 13px;
            }}
        """)

    def changeEvent(self, event):
        if event.type() == QEvent.Type.PaletteChange:
            if not getattr(self, '_palette_pending', False):
                self._palette_pending = True
                QTimer.singleShot(100, self._do_palette_update)
        super().changeEvent(event)

    def _do_palette_update(self):
        try:
            self._apply_theme()
        finally:
            QTimer.singleShot(200, self._reset_palette_pending)

    def _reset_palette_pending(self):
        self._palette_pending = False