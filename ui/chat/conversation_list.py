"""
会话列表面板 + ConversationCard
"""

from PyQt6.QtCore import Qt, pyqtSignal, QEvent, QThread, QTimer
from PyQt6.QtWidgets import (
    QWidget, QVBoxLayout, QHBoxLayout, QLabel, QScrollArea, QSizePolicy, QFrame,
)
from PyQt6.QtGui import QFont
from qfluentwidgets import (
    CardWidget, StrongBodyLabel, CaptionLabel, BodyLabel, SearchLineEdit,
    ScrollArea, isDarkTheme,
)


class _ConversationLoader(QThread):
    """后台线程加载会话列表"""
    result = pyqtSignal(list)  # conversations

    def __init__(self, shop_id: str | None = None, limit: int = 100, parent=None):
        super().__init__(parent)
        self._shop_id = shop_id
        self._limit = limit

    def run(self):
        try:
            from services.message_persistence import message_persistence_service
            convs = message_persistence_service.get_conversations(shop_id=self._shop_id, limit=self._limit)
        except Exception:
            convs = []
        self.result.emit(convs)


class ConversationCard(CardWidget):
    """会话卡片 - 显示在左侧列表"""

    # 用 conversation_clicked 避免与父类 CardWidget.clicked (无参数) 冲突
    conversation_clicked = pyqtSignal(str, str)  # shop_id, buyer_uid

    def __init__(self, conv_data: dict, parent=None):
        super().__init__(parent)
        self.shop_id = conv_data["shop_id"]
        self.buyer_uid = conv_data["buyer_uid"]
        self.conv_data = conv_data
        self._selected = False
        self._init_ui()

    def _init_ui(self):
        self.setMinimumHeight(64)
        self.setCursor(Qt.CursorShape.PointingHandCursor)
        self.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Fixed)

        layout = QVBoxLayout(self)
        layout.setContentsMargins(10, 6, 10, 6)
        layout.setSpacing(2)

        # 第一行：昵称 + 时间
        top_layout = QHBoxLayout()
        top_layout.setContentsMargins(0, 0, 0, 0)

        # 头像（首字）
        nickname = self.conv_data.get("nickname", "?")
        avatar = QLabel(nickname[0] if nickname else "?")
        avatar.setFixedSize(32, 32)
        avatar.setAlignment(Qt.AlignmentFlag.AlignCenter)
        avatar.setStyleSheet("""
            background-color: #4a90d9;
            color: white;
            border-radius: 16px;
            font-weight: bold;
            font-size: 14px;
        """)
        top_layout.addWidget(avatar)

        info_layout = QVBoxLayout()
        info_layout.setContentsMargins(0, 0, 0, 0)
        info_layout.setSpacing(0)

        # 昵称行
        name_row = QHBoxLayout()
        name_row.setContentsMargins(0, 0, 0, 0)

        name_label = StrongBodyLabel(nickname)
        name_label.setMaximumWidth(110)
        self._name_label = name_label
        name_row.addWidget(name_label)

        # 店铺标签
        shop_name = self.conv_data.get("shop_name", "")
        shop_label = CaptionLabel(f"·{shop_name}" if shop_name else "")
        shop_label.setStyleSheet("color: #888;")
        self._shop_label = shop_label
        name_row.addWidget(shop_label)

        name_row.addStretch()

        # 时间
        last_time = self.conv_data.get("last_time", "")
        time_short = ""
        if last_time:
            try:
                from datetime import datetime
                dt = datetime.fromisoformat(last_time)
                now = datetime.now()
                diff = now - dt
                if diff.days == 0:
                    time_short = dt.strftime("%H:%M")
                elif diff.days == 1:
                    time_short = "昨天"
                elif diff.days < 7:
                    time_short = dt.strftime("周%a").replace("Mon", "一").replace("Tue", "二").replace("Wed", "三").replace("Thu", "四").replace("Fri", "五").replace("Sat", "六").replace("Sun", "日")
                else:
                    time_short = dt.strftime("%m-%d")
            except (ValueError, TypeError):
                time_short = ""
        time_label = CaptionLabel(time_short)
        time_label.setStyleSheet("color: #999;")
        self._time_label = time_label
        name_row.addWidget(time_label)

        info_layout.addLayout(name_row)

        # 消息预览
        preview = self.conv_data.get("last_content", "")
        if len(preview) > 30:
            preview = preview[:28] + "..."
        preview_label = CaptionLabel(preview)
        preview_label.setStyleSheet("color: #999;")
        self._preview_label = preview_label
        info_layout.addWidget(preview_label)

        top_layout.addLayout(info_layout, 1)
        layout.addLayout(top_layout)

        self._apply_theme()

    def _apply_theme(self):
        dark = isDarkTheme()
        if self._selected:
            bg = "#3a5068" if dark else "#d0e8ff"
        else:
            bg = "#2a2a2a" if dark else "#ffffff"
        self.setStyleSheet(f"background-color: {bg}; border-radius: 6px;")

    def mousePressEvent(self, event):
        super().mousePressEvent(event)

    def mouseReleaseEvent(self, event):
        """重写父类 mouseReleaseEvent，emit 带参数的 conversation_clicked 信号"""
        super().mouseReleaseEvent(event)
        # 父类 CardWidget.mouseReleaseEvent 会调用 self.clicked.emit()（无参数）
        # 我们不使用父类的 clicked 信号，而是 emit 自定义的 conversation_clicked
        self.conversation_clicked.emit(self.shop_id, self.buyer_uid)

    def set_selected(self, selected: bool):
        self._selected = selected
        self._apply_theme()

    def update_preview(self, conv_data: dict):
        """更新预览信息（新消息到达时）"""
        self.conv_data = conv_data
        content = conv_data.get("last_content", "")
        if len(content) > 30:
            content = content[:28] + "..."
        self._preview_label.setText(content)
        self._time_label.setText(conv_data.get("last_time", "")[-8:-3] or "")

    def changeEvent(self, event):
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
            from PyQt6.QtCore import QTimer
            QTimer.singleShot(200, self._reset_palette_pending)

    def _reset_palette_pending(self):
        """重置调色板更新标志"""
        self._palette_pending = False


class ConversationListPanel(QWidget):
    """会话列表面板"""

    conversation_selected = pyqtSignal(str, str)  # shop_id, buyer_uid

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setObjectName("ConversationListPanel")
        self._cards: list[ConversationCard] = []
        self._selected_card: ConversationCard | None = None
        self._current_shop_filter: str | None = None
        self._all_data: list[dict] = []
        self._loader: _ConversationLoader | None = None
        self._init_ui()
        self._apply_theme()

    def _apply_theme(self):
        dark = isDarkTheme()
        # 面板背景
        list_bg = "#1e1e1e" if dark else "#fafafa"
        self.setStyleSheet(f"""
            #ConversationListPanel {{
                background-color: {list_bg};
                border: none;
            }}
        """)
        # 卡片容器背景
        card_container_bg = "#252525" if dark else "#f0f0f0"
        self._card_container.setStyleSheet(f"background-color: {card_container_bg};")

    def _init_ui(self):
        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(0)

        # 搜索框
        self.search_edit = SearchLineEdit()
        self.search_edit.setPlaceholderText("搜索会话...")
        self.search_edit.textChanged.connect(self._on_search)
        self.search_edit.setFixedHeight(36)
        layout.addWidget(self.search_edit)

        # 滚动区域
        scroll = ScrollArea()
        scroll.setWidgetResizable(True)
        scroll.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)

        self._card_container = QWidget()
        self._card_layout = QVBoxLayout(self._card_container)
        self._card_layout.setContentsMargins(4, 4, 4, 4)
        self._card_layout.setSpacing(4)
        self._card_layout.addStretch()

        scroll.setWidget(self._card_container)
        layout.addWidget(scroll)

    def refresh(self, shop_id: str | None = None):
        """刷新会话列表（后台异步加载）"""
        self._current_shop_filter = shop_id
        # 取消旧的加载任务
        if self._loader is not None:
            self._loader.result.disconnect(self._on_conversations_loaded)
            self._loader.quit()
            self._loader.wait(500)
        self._loader = _ConversationLoader(shop_id=shop_id, limit=100, parent=self)
        self._loader.result.connect(self._on_conversations_loaded)
        self._loader.start()

    def _on_conversations_loaded(self, convs: list[dict]):
        """后台线程加载完成后在主线程更新 UI"""
        self._all_data = convs
        self._rebuild_cards(convs)

    def on_new_message(self, msg_data: dict):
        """新消息到达时增量更新 — 不重建全部卡片"""
        # 检查店铺筛选
        if self._current_shop_filter and msg_data.get("shop_id") != self._current_shop_filter:
            return

        buyer_uid = msg_data.get("buyer_uid", "")
        shop_id = msg_data.get("shop_id", "")
        content = msg_data.get("content", "") or ""
        nickname = msg_data.get("nickname", "")
        timestamp = msg_data.get("timestamp", "")

        # 查找现有卡片
        existing_card = None
        for card in self._cards:
            if card.shop_id == shop_id and card.buyer_uid == buyer_uid:
                existing_card = card
                break

        if existing_card:
            # 更新预览内容
            preview = content[:28] + "..." if len(content) > 30 else content or ""
            existing_card._preview_label.setText(preview)
            # 只在昵称有效且当前昵称不正确时更新
            # 跳过 "mall_cs"/"user" 等角色名，它们不是真正的昵称
            if nickname and nickname not in ("mall_cs", "user", "客服", "AI客服"):
                current_name = existing_card._name_label.text()
                if not current_name or current_name in ("mall_cs", "user", "?"):
                    existing_card._name_label.setText(nickname)
            if timestamp:
                try:
                    from datetime import datetime
                    dt = datetime.fromisoformat(timestamp)
                    existing_card._time_label.setText(dt.strftime("%H:%M"))
                except (ValueError, TypeError):
                    pass
            # 将该卡片移到列表顶部
            idx = self._card_layout.indexOf(existing_card)
            if idx > 0:
                self._card_layout.removeWidget(existing_card)
                self._card_layout.insertWidget(0, existing_card)
        else:
            # 新会话 — 异步刷新（不阻塞主线程）
            self.refresh(self._current_shop_filter)

    def _rebuild_cards(self, convs: list[dict]):
        """重建卡片列表"""
        # 清除旧卡片
        for card in self._cards:
            card.setParent(None)
            card.deleteLater()
        self._cards.clear()
        self._selected_card = None

        # 移除 stretch
        while self._card_layout.count():
            item = self._card_layout.takeAt(0)
            if item.widget():
                item.widget().setParent(None)

        # 创建新卡片
        for conv in convs:
            card = ConversationCard(conv)
            card.conversation_clicked.connect(self._on_card_clicked)
            self._card_layout.addWidget(card)
            self._cards.append(card)

        self._card_layout.addStretch()

    def _on_card_clicked(self, shop_id: str, buyer_uid: str):
        """点击卡片"""
        # 取消旧选中
        if self._selected_card:
            self._selected_card.set_selected(False)
        # 设置新选中
        for card in self._cards:
            if card.shop_id == shop_id and card.buyer_uid == buyer_uid:
                card.set_selected(True)
                self._selected_card = card
                break
        self.conversation_selected.emit(shop_id, buyer_uid)

    def _on_search(self, keyword: str):
        """搜索过滤"""
        if not keyword.strip():
            self._rebuild_cards(self._all_data)
            return
        kw = keyword.strip().lower()
        filtered = [
            d for d in self._all_data
            if kw in (d.get("nickname", "") or "").lower()
            or kw in (d.get("last_content", "") or "").lower()
            or kw in (d.get("shop_name", "") or "").lower()
        ]
        self._rebuild_cards(filtered)

    def changeEvent(self, event):
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
            from PyQt6.QtCore import QTimer
            QTimer.singleShot(200, self._reset_palette_pending)

    def _reset_palette_pending(self):
        """重置调色板更新标志"""
        self._palette_pending = False
