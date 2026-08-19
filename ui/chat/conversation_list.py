"""
会话列表面板 + ConversationCard
"""

from PyQt6.QtCore import Qt, pyqtSignal, QEvent, QThread, QTimer
from PyQt6.QtWidgets import (
    QWidget, QVBoxLayout, QHBoxLayout, QLabel, QScrollArea, QSizePolicy, QFrame,
)
from PyQt6.QtGui import QFont
from qfluentwidgets import (
    CaptionLabel, SearchLineEdit,
    isDarkTheme,
)

from utils.time_format import format_list_time, parse_dt

# 分批创建卡片的配置：避免一次创建 80+ 个 CardWidget 导致内存峰值/ntdll 堆崩溃
_CARD_BATCH_SIZE = 15      # 每批创建的卡片数
_CARD_BATCH_INTERVAL = 30  # 批次间隔 ms（给事件循环喘息时间）


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


class ElideLabel(QLabel):
    """宽度不足时自动省略号（…）的标签，用于会话列表缩窄场景

    QLabel 默认 minimumSizeHint 是完整文本宽度，布局空间不足时会挤压其他
    控件（时间标签被挤出）。本类配合 Ignored 水平策略可收缩到任意宽度，
    并在 resizeEvent 中按当前宽度重新省略，保证长文本以省略号截断而非溢出。
    """

    def __init__(self, text: str = "", parent=None):
        super().__init__(text, parent)
        self._full_text = text

    def setText(self, text: str):
        self._full_text = text if text is not None else ""
        self._update_elided()

    def resizeEvent(self, event):
        super().resizeEvent(event)
        self._update_elided()

    def _update_elided(self):
        if not self._full_text:
            if self.text():
                super().setText("")
            return
        w = self.width()
        if w <= 0:
            return
        elided = self.fontMetrics().elidedText(
            self._full_text, Qt.TextElideMode.ElideRight, w
        )
        if elided != self.text():
            super().setText(elided)


class ConversationCard(QFrame):
    """会话卡片 - 显示在左侧列表

    不使用 qfluentwidgets CardWidget，因为后者继承 BackgroundAnimationWidget，
    每个 card 创建 QPropertyAnimation + eventFilter + 复杂 QPainterPath paintEvent。
    30 个 card 同时首次显示时触发大量绘制，在 ntdll 堆已损坏的环境下崩溃。
    改用纯 QFrame + setStyleSheet 实现选中/hover 效果。
    """

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

        # 昵称：可收缩 + 省略号（缩窄时优先让位，不能挤掉时间标签）
        name_label = ElideLabel(nickname)
        name_label.setFont(QFont("Microsoft YaHei", 13, QFont.Weight.DemiBold))
        name_label.setMaximumWidth(110)
        name_label.setSizePolicy(QSizePolicy.Policy.Ignored, QSizePolicy.Policy.Preferred)
        self._name_label = name_label
        name_row.addWidget(name_label, 1)

        # 店铺标签：可收缩 + 省略号
        shop_name = self.conv_data.get("shop_name", "")
        shop_label = ElideLabel(f"·{shop_name}" if shop_name else "")
        shop_label.setFont(QFont("Microsoft YaHei", 9))
        shop_label.setStyleSheet("color: #888;")
        shop_label.setMaximumWidth(120)
        shop_label.setSizePolicy(QSizePolicy.Policy.Ignored, QSizePolicy.Policy.Preferred)
        self._shop_label = shop_label
        name_row.addWidget(shop_label)

        name_row.addStretch()

        # 时间（微信风格：今天 HH:MM / 昨天 / 星期X / M月d日 / yyyy/M/d）
        # Fixed 策略：始终完整显示，不参与压缩，缩窄时昵称/店铺先让位
        last_time = self.conv_data.get("last_time", "")
        dt = parse_dt(last_time)
        time_short = format_list_time(dt) if dt is not None else ""
        time_label = CaptionLabel(time_short)
        time_label.setStyleSheet("color: #999;")
        time_label.setSizePolicy(QSizePolicy.Policy.Fixed, QSizePolicy.Policy.Fixed)
        self._time_label = time_label
        name_row.addWidget(time_label)

        info_layout.addLayout(name_row)

        # 消息预览：可收缩 + 省略号
        preview = self.conv_data.get("last_content", "")
        if len(preview) > 30:
            preview = preview[:28] + "..."
        preview_label = ElideLabel(preview)
        preview_label.setFont(QFont("Microsoft YaHei", 9))
        preview_label.setStyleSheet("color: #999;")
        preview_label.setSizePolicy(QSizePolicy.Policy.Ignored, QSizePolicy.Policy.Preferred)
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
        super().mouseReleaseEvent(event)
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
        dt = parse_dt(conv_data.get("last_time", ""))
        self._time_label.setText(format_list_time(dt) if dt is not None else "")

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

    # 首次加载最大卡片数，避免一次创建过多 CardWidget 引发 ntdll 堆崩溃
    MAX_INITIAL_CARDS = 30

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setObjectName("ConversationListPanel")
        self._cards: list[ConversationCard] = []
        self._selected_card: ConversationCard | None = None
        self._current_shop_filter: str | None = None
        self._all_data: list[dict] = []
        self._loader: _ConversationLoader | None = None
        # 分批重建状态
        self._pending_convs: list[dict] = []
        self._rebuild_index: int = 0
        self._rebuild_timer: QTimer | None = None
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

        # 滚动区域 — 用原生 QScrollArea，不用 qfluentwidgets ScrollArea
        # (后者有 60fps SmoothScroll QTimer，在堆损坏环境下会加剧崩溃)
        self._scroll_area = QScrollArea()
        self._scroll_area.setWidgetResizable(True)
        self._scroll_area.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)

        self._card_container = QWidget()
        self._card_layout = QVBoxLayout(self._card_container)
        self._card_layout.setContentsMargins(4, 4, 4, 4)
        self._card_layout.setSpacing(4)
        self._card_layout.addStretch()

        self._scroll_area.setWidget(self._card_container)
        layout.addWidget(self._scroll_area)

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
                dt = parse_dt(timestamp)
                if dt is not None:
                    existing_card._time_label.setText(format_list_time(dt))
            # 将该卡片移到列表顶部
            idx = self._card_layout.indexOf(existing_card)
            if idx > 0:
                self._card_layout.removeWidget(existing_card)
                self._card_layout.insertWidget(0, existing_card)
        else:
            # 新会话 — 异步刷新（不阻塞主线程）
            self.refresh(self._current_shop_filter)

    def _rebuild_cards(self, convs: list[dict]):
        """重建卡片列表 — 分批创建，避免一次创建 80+ CardWidget 触发堆崩溃

        先展示最多 MAX_INITIAL_CARDS 个，剩余在滚动到底时按需加载。
        """
        # 取消进行中的分批重建
        self._cancel_batch_rebuild()

        # 清除旧卡片
        for card in self._cards:
            card.setParent(None)
            card.deleteLater()
        self._cards.clear()
        self._selected_card = None

        # 移除所有布局项（stretch + 可能的残留 widget）
        while self._card_layout.count():
            item = self._card_layout.takeAt(0)
            if item is not None:
                w = item.widget()
                if w is not None:
                    w.setParent(None)

        if not convs:
            self._card_layout.addStretch()
            return

        # 准备分批数据
        self._pending_convs = convs
        # 首次加载限制为 MAX_INITIAL_CARDS
        initial = convs[:self.MAX_INITIAL_CARDS]
        self._rebuild_index = len(initial)
        remaining = convs[self._rebuild_index:]

        # 如果初始数量少，直接一次性创建
        if len(initial) <= _CARD_BATCH_SIZE:
            self._create_card_batch(initial)
            self._card_layout.addStretch()
        else:
            # 分批创建：先创建首批 _CARD_BATCH_SIZE 个，其余通过定时器异步加载
            self._card_layout.addStretch()
            self._create_card_batch(initial[:_CARD_BATCH_SIZE])
            self._rebuild_index = _CARD_BATCH_SIZE
            # 安排异步加载剩余的 MAX_INITIAL_CARDS 卡片
            if self._rebuild_index < len(initial):
                self._schedule_batch_continue(initial)

        # 如果有超出 MAX_INITIAL_CARDS 的卡片，监听滚动条以便按需加载
        if remaining:
            self._install_scroll_listener()

    def _schedule_batch_continue(self, convs: list[dict]):
        """安排下一批卡片创建（异步，让事件循环喘气）"""
        self._rebuild_timer = QTimer(self)
        self._rebuild_timer.setSingleShot(True)
        self._rebuild_timer.timeout.connect(lambda: self._continue_batch(convs))
        self._rebuild_timer.start(_CARD_BATCH_INTERVAL)

    def _continue_batch(self, convs: list[dict]):
        """继续创建下一批卡片"""
        if self._rebuild_index >= len(convs):
            return
        remaining = convs[self._rebuild_index:]
        batch = remaining[:_CARD_BATCH_SIZE]
        self._rebuild_index += len(batch)
        self._create_card_batch(batch)
        # 如果还有，继续安排
        if self._rebuild_index < len(convs):
            self._schedule_batch_continue(convs)

    def _cancel_batch_rebuild(self):
        """取消进行中的分批重建"""
        self._pending_convs = []
        self._rebuild_index = 0
        if self._rebuild_timer is not None:
            self._rebuild_timer.stop()
            self._rebuild_timer = None

    def _create_card_batch(self, conv_batch: list[dict]):
        """创建一批卡片（在 stretch 之前插入）"""
        # stretch 是第 count-1 项，卡片插入在它前面
        stretch_idx = self._card_layout.count() - 1
        for conv in conv_batch:
            card = ConversationCard(conv)
            card.conversation_clicked.connect(self._on_card_clicked)
            self._card_layout.insertWidget(stretch_idx, card)
            self._cards.append(card)
            stretch_idx += 1

    def _install_scroll_listener(self):
        """监听滚动条，在滚动到底时加载更多"""
        if not hasattr(self, '_scroll_area') or self._scroll_area is None:
            return
        vbar = self._scroll_area.verticalScrollBar()
        if vbar is None:
            return
        try:
            vbar.valueChanged.disconnect(self._on_scroll_changed)
        except (TypeError, RuntimeError):
            pass
        vbar.valueChanged.connect(self._on_scroll_changed)

    def _on_scroll_changed(self, value: int):
        """滚动条变化 — 检查是否接近底部"""
        if not hasattr(self, '_scroll_area') or self._scroll_area is None:
            return
        vbar = self._scroll_area.verticalScrollBar()
        if vbar is None:
            return
        if vbar.maximum() - value <= 60:
            self._load_more_if_needed()

    def _load_more_if_needed(self):
        """按需加载下一批卡片"""
        if not self._pending_convs or self._rebuild_index >= len(self._pending_convs):
            return
        # 防御：避免重复触发
        if self._rebuild_timer is not None and self._rebuild_timer.isActive():
            return

        remaining = self._pending_convs[self._rebuild_index:]
        if not remaining:
            return
        batch = remaining[:_CARD_BATCH_SIZE]
        self._rebuild_index += len(batch)

        self._create_card_batch(batch)

        # 如果还有更多，继续监听（timer 只用于防重复，不用于调度）
        if self._rebuild_index < len(self._pending_convs):
            self._rebuild_timer = QTimer(self)
            self._rebuild_timer.setSingleShot(True)
            self._rebuild_timer.timeout.connect(self._clear_load_timer)
            self._rebuild_timer.start(200)  # 200ms 防重复

    def _clear_load_timer(self):
        """清除防重复定时器"""
        self._rebuild_timer = None

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
