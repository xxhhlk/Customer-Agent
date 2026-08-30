"""
聊天区域面板 - 消息气泡区 + 输入区
"""

from datetime import datetime
from PyQt6.QtCore import Qt, pyqtSignal, QTimer, QEvent, QThread
from PyQt6.QtWidgets import (
    QWidget, QVBoxLayout, QHBoxLayout, QLabel, QSizePolicy, QFrame, QScrollArea,
)
from PyQt6.QtGui import QFont
from qfluentwidgets import StrongBodyLabel, CaptionLabel, isDarkTheme

from ui.chat.message_bubble import MessageBubble
from ui.chat.input_area import InputArea
from ui.chat.forward_dialog import ForwardDialog
from utils.logger_loguru import get_logger
from utils.time_format import format_day_label, format_time_label, needs_time_separator, parse_dt

logger = get_logger("ChatArea")


class TimeSeparator(QLabel):
    """居中时间/日期分隔标签（微信风格）：跨天插日期，同天间隔超阈值插时间"""

    def __init__(self, text: str, parent=None):
        super().__init__(text, parent)
        self.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.setSizePolicy(QSizePolicy.Policy.Preferred, QSizePolicy.Policy.Fixed)
        dark = isDarkTheme()
        bg = "rgba(255,255,255,0.14)" if dark else "rgba(0,0,0,0.06)"
        fg = "#b0b0b0" if dark else "#8f8f8f"
        self.setStyleSheet(f"""
            color: {fg};
            background-color: {bg};
            border-radius: 7px;
            padding: 2px 10px;
            font-size: 11px;
        """)


class _MessageLoader(QThread):
    """后台线程加载消息记录"""
    result = pyqtSignal(str, str, list)  # shop_id, buyer_uid, messages

    def __init__(self, shop_id: str, buyer_uid: str, parent=None):
        super().__init__(parent)
        self._shop_id = shop_id
        self._buyer_uid = buyer_uid

    def run(self):
        try:
            from services.message_persistence import message_persistence_service
            messages = message_persistence_service.get_messages_by_uid(
                shop_id=self._shop_id, buyer_uid=self._buyer_uid, limit=500
            )
        except Exception:
            messages = []
        self.result.emit(self._shop_id, self._buyer_uid, messages)


class ChatAreaPanel(QWidget):
    """聊天区域 - 头部 + 消息列表 + 输入框"""

    send_manual_reply = pyqtSignal(str, str, str, str)  # shop_id, user_id, text, buyer_uid
    forward_message = pyqtSignal(dict, str)  # msg_data, target_buyer_uid

    # 判断"在底部"的像素容差
    _BOTTOM_THRESHOLD = 60

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setObjectName("ChatAreaPanel")
        self._current_shop_id: str = ""
        self._current_user_id: str = ""
        self._current_buyer_uid: str = ""
        self._loader: _MessageLoader | None = None
        self._load_token: int = 0  # 每次加载递增，用于区分过期回调
        self._user_scrolled_up: bool = False  # 用户是否手动向上滚动（浏览历史）
        self._scroll_fallback_timer: QTimer | None = None  # 兜底定时器（1.5s后强制滚动）
        self._last_msg_time: datetime | None = None  # 上一条消息时间，用于插入时间/日期分隔标签

        self._init_ui()
        self._apply_theme()

    def _apply_theme(self):
        dark = isDarkTheme()
        # 整体背景透明，由上层决定
        self.setStyleSheet(f"""
            #ChatAreaPanel {{
                background-color: transparent;
                border: none;
            }}
        """)
        # Header 背景
        header_bg = "#2b2b2b" if dark else "#f5f5f5"
        header_border = "#3a3a3a" if dark else "#e0e0e0"
        self.header.setStyleSheet(f"""
            background-color: {header_bg};
            border-bottom: 1px solid {header_border};
        """)
        # 消息容器背景
        msg_bg = "#1e1e1e" if dark else "#fafafa"
        self._msg_container.setStyleSheet(f"background-color: {msg_bg};")

    def _init_ui(self):
        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(0)

        # 头部信息栏
        self.header = QWidget()
        self.header.setFixedHeight(48)
        header_layout = QHBoxLayout(self.header)
        header_layout.setContentsMargins(16, 4, 16, 4)

        self.header_title = StrongBodyLabel("请选择一个会话")
        self.header_title.setFont(QFont("Microsoft YaHei", 13))
        header_layout.addWidget(self.header_title)

        self.header_detail = CaptionLabel("")
        self.header_detail.setStyleSheet("color: #999;")
        header_layout.addWidget(self.header_detail)
        header_layout.addStretch()

        layout.addWidget(self.header)

        # 消息滚动区
        self.scroll_area = QScrollArea()
        self.scroll_area.setWidgetResizable(True)
        self.scroll_area.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        self.scroll_area.setFrameShape(QFrame.Shape.NoFrame)

        self._msg_container = QWidget()
        self._msg_layout = QVBoxLayout(self._msg_container)
        self._msg_layout.setContentsMargins(4, 4, 4, 4)
        self._msg_layout.setSpacing(4)
        self._msg_layout.addStretch()
        self.scroll_area.setWidget(self._msg_container)

        # 监听滚动条变化，检测用户是否手动向上滚动
        scroll_bar = self.scroll_area.verticalScrollBar()
        if scroll_bar is not None:
            scroll_bar.valueChanged.connect(self._on_scroll_value_changed)

        layout.addWidget(self.scroll_area, 1)

        # 输入区域
        self.input_area = InputArea()
        self.input_area.send_message.connect(self._on_input_message)
        self.input_area.set_enabled(False)
        layout.addWidget(self.input_area)

    def load_messages(self, shop_id: str, buyer_uid: str):
        """加载指定买家在指定店铺的消息（异步后台加载）"""
        logger.info(f"[ChatArea] load_messages: shop_id={shop_id}, buyer_uid={buyer_uid}")
        # 缓存 shop_id/buyer_uid 用于手动发送
        self._current_shop_id = shop_id
        self._current_buyer_uid = buyer_uid
        # 递增 token，让旧的 loader 回调过期
        self._load_token += 1
        token = self._load_token

        # 切换会话时重置滚动状态
        self._user_scrolled_up = False
        self._cancel_scroll_retry()
        self._last_msg_time = None  # 重置时间分隔状态

        # 清空旧消息
        self._clear_messages()
        self.header_title.setText("加载中...")
        self.header_detail.setText("")
        self.input_area.set_enabled(False)

        # 后台线程加载
        if self._loader is not None:
            try:
                self._loader.result.disconnect(self._on_messages_loaded)
            except (TypeError, RuntimeError):
                pass
            self._loader.quit()
            self._loader.wait(500)
        self._loader = _MessageLoader(shop_id, buyer_uid, self)
        self._loader.result.connect(lambda sid, buid, msgs: self._on_messages_loaded(sid, buid, msgs, token))
        self._loader.start()
        logger.info("[ChatArea] load_messages: loader 已启动")

    def _on_messages_loaded(self, shop_id: str, buyer_uid: str, messages: list, token: int = 0):
        """后台线程加载完成后，在主线程渲染气泡"""
        logger.info(f"[ChatArea] _on_messages_loaded: {len(messages)} 条消息")

        # 防止过期结果（用户已切换到其他会话）
        if token != self._load_token:
            return
        if shop_id != self._current_shop_id or buyer_uid != self._current_buyer_uid:
            return

        if messages:
            # 从首条消息提取 user_id
            self._current_user_id = messages[0].get("user_id", "")
            # 标题应显示买家昵称：首条消息可能是客服发出的（outbound），
            # 其 nickname 为空或历史脏数据 "客服"/"AI客服"，需过滤后兜底 buyer_uid
            nickname = messages[0].get("nickname") or ""
            if nickname in ("客服", "AI客服", "mall_cs", "user"):
                nickname = ""
            self.header_title.setText(nickname or buyer_uid)
            self.header_detail.setText(f"({buyer_uid})")

            # 渲染气泡
            logger.info("[ChatArea] _on_messages_loaded: 开始渲染气泡")
            for msg in messages:
                self._add_time_separator_if_needed(msg)
                bubble = MessageBubble(msg)
                self._connect_bubble_signal(bubble)
                self._msg_layout.insertWidget(self._msg_layout.count() - 1, bubble)
            logger.info("[ChatArea] _on_messages_loaded: 气泡渲染完成")

            self.input_area.set_enabled(True)
        else:
            self.header_title.setText("暂无消息")
            self.header_detail.setText("")
            self.input_area.set_enabled(False)

        # 滚动到底部 — 使用带重试的滚动策略
        logger.info("[ChatArea] _on_messages_loaded: 准备滚动到底部")
        self._schedule_scroll_to_bottom(token)
        logger.info("[ChatArea] _on_messages_loaded: 完成")

    def append_message(self, msg_data: dict):
        """追加新消息（实时）"""
        direction = msg_data.get("direction", "")
        buyer_uid = msg_data.get("buyer_uid", "")
        shop_id = msg_data.get("shop_id", "")

        # 只追加当前会话的消息
        if shop_id != self._current_shop_id or buyer_uid != self._current_buyer_uid:
            return

        self._add_time_separator_if_needed(msg_data)
        bubble = MessageBubble(msg_data)
        self._connect_bubble_signal(bubble)
        self._msg_layout.insertWidget(self._msg_layout.count() - 1, bubble)

        # 智能滚动：用户在浏览历史时不强制拉回，否则滚到底部
        if not self._user_scrolled_up:
            self._schedule_scroll_to_bottom(self._load_token)

    def _add_time_separator_if_needed(self, msg_data: dict):
        """微信风格时间分隔：首条/跨天插日期标签，同一天间隔超阈值插时间标签"""
        dt = parse_dt(msg_data.get("timestamp", ""))
        if dt is None:
            return
        prev_dt = self._last_msg_time
        self._last_msg_time = dt
        if not needs_time_separator(prev_dt, dt):
            return
        if prev_dt is None or prev_dt.date() != dt.date():
            text = format_day_label(dt)
        else:
            text = format_time_label(dt)
        sep = TimeSeparator(text)
        self._msg_layout.insertWidget(
            self._msg_layout.count() - 1, sep, alignment=Qt.AlignmentFlag.AlignHCenter
        )

    def _clear_messages(self):
        """清空消息气泡"""
        while self._msg_layout.count() > 1:  # 保留底部 stretch
            item = self._msg_layout.takeAt(0)
            if item is None:
                continue
            w = item.widget()
            if w is not None:
                w.setParent(None)
                w.deleteLater()
            elif item.layout() is not None:
                # 嵌套布局项，递归清理
                self._clear_sub_layout(item.layout())

    @staticmethod
    def _clear_sub_layout(layout):
        """递归清理嵌套布局中的 widget"""
        while layout.count():
            child = layout.takeAt(0)
            w = child.widget()
            if w is not None:
                w.setParent(None)
                w.deleteLater()
            elif child.layout() is not None:
                ChatAreaPanel._clear_sub_layout(child.layout())

    def _on_input_message(self, text: str):
        """输入框发送消息"""
        if not self._current_shop_id or not self._current_user_id or not self._current_buyer_uid:
            return
        self.send_manual_reply.emit(
            self._current_shop_id,
            self._current_user_id,
            text,
            self._current_buyer_uid,
        )

    def _connect_bubble_signal(self, bubble: MessageBubble):
        """连接 bubble 的转发信号"""
        bubble.forward_requested.connect(self._on_forward_requested)

    def _on_forward_requested(self, msg_data: dict):
        """右键点击「转发消息」→ 弹出目标选择弹窗"""
        shop_id = self._current_shop_id or msg_data.get("shop_id", "")
        exclude_uid = self._current_buyer_uid or msg_data.get("buyer_uid", "")

        dlg = ForwardDialog(shop_id, exclude_uid, self)
        dlg.selected.connect(
            lambda target_uid, _nickname: self.forward_message.emit(msg_data, target_uid)
        )
        dlg.exec()

    _SCROLL_FALLBACK_MS = 2000  # 兜底：2秒后强制滚动

    def _on_scroll_value_changed(self, value: int):
        """滚动条值变化 → 检测用户是否手动向上滚动"""
        vbar = self.scroll_area.verticalScrollBar()
        if vbar is None:
            return
        at_bottom = vbar.maximum() - value <= self._BOTTOM_THRESHOLD
        self._user_scrolled_up = not at_bottom

    def _is_at_bottom(self) -> bool:
        vbar = self.scroll_area.verticalScrollBar()
        if vbar is None:
            return True
        return vbar.maximum() - vbar.value() <= self._BOTTOM_THRESHOLD

    # ---------- 自动滚动 --------

    def _schedule_scroll_to_bottom(self, token: int):
        """布局完成后自动滚到底，通过 rangeChanged + 兜底定时器保证"""
        self._cancel_scroll_retry()

        vbar = self.scroll_area.verticalScrollBar()
        if vbar is None:
            return

        # 方案：rangeChanged 在滚动区域几何变化后触发，此时 maximum 是准确的
        def _on_range(min_val, max_val):
            vbar.setValue(max_val)
            # 再补一发 QTimer(0)：某些 widget 在 rangeChanged 后才完成最终大小计算
            QTimer.singleShot(0, lambda: vbar.setValue(vbar.maximum()) if vbar else None)
            self._cancel_scroll_retry()  # 成功后取消兜底

        # 先做一次即时滚动（可能不准）
        vbar.setValue(vbar.maximum())

        # 监听范围变化（布局完成后会触发），成功滚动后会自动断开
        vbar.rangeChanged.connect(_on_range)

        # 兜底定时器：如果 rangeChanged 长时间不触发，2秒后强滚
        self._scroll_fallback_timer = QTimer(self)
        self._scroll_fallback_timer.setSingleShot(True)
        self._scroll_fallback_timer.timeout.connect(
            lambda: (vbar.setValue(vbar.maximum()), self._cancel_scroll_retry()) if vbar else None
        )
        self._scroll_fallback_timer.start(self._SCROLL_FALLBACK_MS)

    def _cancel_scroll_retry(self):
        """断开 rangeChanged 监听并停止兜底定时器"""
        vbar = self.scroll_area.verticalScrollBar()
        if vbar is not None:
            try:
                vbar.rangeChanged.disconnect()
            except TypeError:
                pass
        if self._scroll_fallback_timer is not None:
            self._scroll_fallback_timer.stop()
            self._scroll_fallback_timer.deleteLater()
            self._scroll_fallback_timer = None

    def changeEvent(self, event):
        if event.type() == QEvent.Type.PaletteChange:
            # 防抖：避免 setStyleSheet → PaletteChange → singleShot 乒乓循环
            if not getattr(self, '_palette_pending', False):
                self._palette_pending = True
                QTimer.singleShot(100, self._do_palette_update)
        super().changeEvent(event)

    def _do_palette_update(self):
        """实际执行调色板更新"""
        # 先执行更新，再延迟重置标志 —— 避免 setStyleSheet 触发的 PaletteChange
        # 在标志仍为 True 时被忽略，从而打破乒乓循环
        try:
            self._apply_theme()
        finally:
            QTimer.singleShot(200, self._reset_palette_pending)

    def _reset_palette_pending(self):
        """重置调色板更新标志"""
        self._palette_pending = False
