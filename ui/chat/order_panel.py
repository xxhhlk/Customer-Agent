"""订单面板（OrderPopup）
============================
浮层，挂在聊天输入框上方，展示**当前会话买家**的订单，并支持编辑商家备注。

- 数据：``/latitude/order/userAllOrder``（订单 + 备注一体返回，无需额外请求）
- 备注编辑为**覆盖式**：卡片内联编辑框；保存经 note/update（无备注时 note/add）
- 写备注节流：服务端要求同一订单两次写间隔 > 1 秒，本地按订单节流
- 列表拉取 / 备注保存全部在 QThread 后台完成（含 cookie 读取，禁止在 UI 线程建）
- 缩略图复用 ``ui.chat.media.image_loader.ImageLoaderManager``（QPixmap 仅在主线程创建）
- 禁用 QMessageBox（项目铁律）

接口与实测记录见 `docs/order-api-research-2026-10-12.md`。
"""

import time
from typing import Any, Dict, List, Optional, Tuple

from PyQt6.QtCore import Qt, QThread, QTimer, pyqtSignal
from PyQt6.QtWidgets import (
    QFrame, QVBoxLayout, QHBoxLayout, QScrollArea, QWidget,
    QLabel, QTextEdit,
)
from PyQt6.QtGui import QPixmap
from qfluentwidgets import PushButton, TransparentToolButton, FluentIcon, isDarkTheme

from utils.logger_loguru import get_logger

logger = get_logger("OrderPanel")

# 列表缓存 TTL（秒）：避免每次打开面板都打接口
_CACHE_TTL = 120
# (shop_id, user_id, buyer_uid, page) -> (timestamp, data)
_ORDER_CACHE: Dict[Tuple[str, str, str, int], Tuple[float, Dict[str, Any]]] = {}

_PAGE_SIZE = 20
_THUMB_SIZE = 56


def _format_amount(fen: Any) -> str:
    """金额（分）→ ``¥19.90``；无效值返回 ``—``"""
    try:
        return f"¥{int(fen) / 100:.2f}"
    except (TypeError, ValueError):
        return "—"


def _elide(text: str, limit: int) -> str:
    text = str(text or "").strip()
    return text if len(text) <= limit else text[: limit - 1] + "…"


def _status_color(text: str, dark: bool) -> str:
    """按状态文案给颜色：退款/异常红、完成绿、待办橙、其余蓝"""
    t = str(text or "")
    if any(k in t for k in ("退款", "售后", "失败", "取消", "关闭", "异常")):
        return "#ff7875" if dark else "#d93025"
    if any(k in t for k in ("完成", "成功", "签收")):
        return "#4cd07d" if dark else "#1a9e5c"
    if any(k in t for k in ("待付款", "待发货", "待成团", "待处理", "待揽收", "待收货")):
        return "#ffb454" if dark else "#d97706"
    return "#6cb2f7" if dark else "#2b7fd4"


class _OrderLoader(QThread):
    """后台拉取买家订单列表（含 cookie 读取，禁止在 UI 线程执行）"""

    # shop_id, user_id, buyer_uid, page, data|None, error
    result = pyqtSignal(str, str, str, int, object, str)

    def __init__(self, shop_id: str, user_id: str, buyer_uid: str, page: int, parent=None):
        super().__init__(parent)
        self._shop_id = shop_id
        self._user_id = user_id
        self._buyer_uid = buyer_uid
        self._page = page

    @property
    def key(self) -> Tuple[str, str, str, int]:
        """本次请求标识（含账号与买家），用于判断"同一请求是否已在飞行中" """
        return (self._shop_id, self._user_id, self._buyer_uid, self._page)

    def run(self):
        emit = self.result.emit
        try:
            from Channel.pinduoduo.utils.API.order import OrderApi
            api = OrderApi(str(self._shop_id), str(self._user_id))
            data = api.list_orders(self._buyer_uid, page=self._page, page_size=_PAGE_SIZE)
            if not data.get("success"):
                emit(*self.key, None, data.get("error_msg") or "未知错误")
                return
            _ORDER_CACHE[self.key] = (time.time(), data)
            emit(*self.key, data, "")
        except Exception as e:  # noqa: BLE001
            logger.error(f"拉取订单列表异常: {e}", exc_info=True)
            emit(*self.key, None, f"加载异常：{e}")


class _NoteSaver(QThread):
    """后台保存订单备注（覆盖式）"""

    # order_sn, ok, error, remark（实际写入的内容）
    result = pyqtSignal(str, bool, str, str)

    def __init__(self, shop_id: str, user_id: str, order_sn: str,
                 remark: str, has_note: bool, parent=None):
        super().__init__(parent)
        self._shop_id = shop_id
        self._user_id = user_id
        self._order_sn = order_sn
        self._remark = remark
        self._has_note = has_note

    def run(self):
        try:
            from Channel.pinduoduo.utils.API.order import OrderApi
            api = OrderApi(str(self._shop_id), str(self._user_id))
            res = api.save_note(self._order_sn, self._remark, has_note=self._has_note)
            self.result.emit(self._order_sn, bool(res.get("success")),
                             res.get("error_msg") or "", self._remark)
        except Exception as e:  # noqa: BLE001
            logger.error(f"保存订单备注异常: {e}", exc_info=True)
            self.result.emit(self._order_sn, False, f"保存异常：{e}", self._remark)


class OrderCard(QFrame):
    """单个订单卡片：状态 / 商品 / 金额 / 时间 / 物流 / 备注（内联编辑）"""

    # order_sn, new_text, has_note
    save_note_requested = pyqtSignal(str, str, bool)

    def __init__(self, order: Dict[str, Any], parent=None):
        super().__init__(parent)
        self.order = order
        self._saving = False
        self._pixmap_url = ""
        self.setObjectName("OrderCard")
        self._build_ui()
        self.apply_theme()

    # ------------------------------------------------------------------ #
    # 构建
    # ------------------------------------------------------------------ #

    def _build_ui(self):
        layout = QVBoxLayout(self)
        layout.setContentsMargins(10, 8, 10, 8)
        layout.setSpacing(6)

        # ---- 行1：订单号 + 状态 ----
        row1 = QHBoxLayout()
        row1.setSpacing(8)
        self._sn_label = QLabel(f"订单号 {self.order.get('order_sn') or '-'}")
        self._sn_label.setObjectName("OrderSn")
        row1.addWidget(self._sn_label)
        row1.addStretch()
        self._status_label = QLabel(str(self.order.get("status_str") or ""))
        self._status_label.setObjectName("OrderStatus")
        row1.addWidget(self._status_label)
        layout.addLayout(row1)

        # ---- 行2：商品 ----
        goods = self.order.get("goods") or []
        goods_row = QHBoxLayout()
        goods_row.setSpacing(8)

        self._thumb = QLabel()
        self._thumb.setObjectName("OrderThumb")
        self._thumb.setFixedSize(_THUMB_SIZE, _THUMB_SIZE)
        self._thumb.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self._thumb.setText("载入中" if self.thumb_url() else "无图")
        goods_row.addWidget(self._thumb, 0, Qt.AlignmentFlag.AlignTop)

        info = QVBoxLayout()
        info.setSpacing(2)
        if goods:
            g = goods[0]
            name = QLabel(_elide(g.get("name") or "(未命名商品)", 44))
            name.setObjectName("OrderGoodsName")
            info.addWidget(name)

            sub_parts = []
            if g.get("spec"):
                sub_parts.append(str(g["spec"]))
            sub_parts.append(f"{_format_amount(g.get('price'))} × {g.get('number') or 1}")
            sub = QLabel("  ".join(sub_parts))
            sub.setObjectName("OrderGoodsSub")
            info.addWidget(sub)

            if len(goods) > 1:
                more = QLabel(f"等共 {len(goods)} 件商品")
                more.setObjectName("OrderGoodsSub")
                info.addWidget(more)
        else:
            empty = QLabel("(无商品信息)")
            empty.setObjectName("OrderGoodsSub")
            info.addWidget(empty)
        info.addStretch()
        goods_row.addLayout(info, 1)
        layout.addLayout(goods_row)

        # ---- 行3：合计 + 下单时间 ----
        row3 = QHBoxLayout()
        row3.setSpacing(8)
        self._amount_label = QLabel(f"合计 {_format_amount(self.order.get('amount'))}")
        self._amount_label.setObjectName("OrderAmount")
        row3.addWidget(self._amount_label)
        row3.addStretch()
        self._time_label = QLabel(f"下单 {self.order.get('order_time') or '-'}")
        self._time_label.setObjectName("OrderMeta")
        row3.addWidget(self._time_label)
        layout.addLayout(row3)

        # ---- 行4：物流（有才显示）----
        tn = str(self.order.get("tracking_number") or "")
        self._tracking_label = QLabel(f"物流单号 {tn}")
        self._tracking_label.setObjectName("OrderMeta")
        self._tracking_label.setVisible(bool(tn))
        layout.addWidget(self._tracking_label)

        # ---- 分隔线 ----
        sep = QFrame()
        sep.setObjectName("OrderSep")
        sep.setFixedHeight(1)
        layout.addWidget(sep)

        # ---- 备注显示行 ----
        self._note_display = QWidget()
        nd = QHBoxLayout(self._note_display)
        nd.setContentsMargins(0, 0, 0, 0)
        nd.setSpacing(6)
        self._note_title = QLabel("备注")
        self._note_title.setObjectName("OrderNoteTitle")
        nd.addWidget(self._note_title, 0, Qt.AlignmentFlag.AlignTop)
        self._note_label = QLabel("")
        self._note_label.setObjectName("OrderNoteText")
        nd.addWidget(self._note_label, 1)
        self._edit_btn = TransparentToolButton(FluentIcon.EDIT)
        self._edit_btn.setToolTip("编辑备注")
        self._edit_btn.setFixedSize(28, 28)
        self._edit_btn.clicked.connect(self._enter_edit)
        nd.addWidget(self._edit_btn, 0, Qt.AlignmentFlag.AlignTop)
        layout.addWidget(self._note_display)
        self._update_note_display()

        # ---- 备注编辑区（默认隐藏）----
        self._editor_host = QWidget()
        ev = QVBoxLayout(self._editor_host)
        ev.setContentsMargins(0, 0, 0, 0)
        ev.setSpacing(4)

        self._editor = QTextEdit()
        self._editor.setObjectName("OrderNoteEditor")
        self._editor.setPlaceholderText("输入备注内容")
        self._editor.setFixedHeight(56)
        ev.addWidget(self._editor)

        btn_row = QHBoxLayout()
        btn_row.setSpacing(6)
        self._error_label = QLabel("")
        self._error_label.setObjectName("OrderNoteError")
        self._error_label.setVisible(False)
        btn_row.addWidget(self._error_label, 1)
        btn_row.addStretch()
        self._cancel_btn = PushButton("取消")
        self._cancel_btn.setFixedSize(64, 28)
        self._cancel_btn.clicked.connect(self._cancel_edit)
        btn_row.addWidget(self._cancel_btn)
        self._save_btn = PushButton("保存")
        self._save_btn.setFixedSize(64, 28)
        self._save_btn.setToolTip("保存（覆盖原备注）")
        self._save_btn.clicked.connect(self._on_save_clicked)
        btn_row.addWidget(self._save_btn)
        ev.addLayout(btn_row)

        self._editor_host.setVisible(False)
        layout.addWidget(self._editor_host)

    # ------------------------------------------------------------------ #
    # 缩略图
    # ------------------------------------------------------------------ #

    def thumb_url(self) -> str:
        goods = self.order.get("goods") or []
        return str(goods[0].get("thumb") or "") if goods else ""

    def set_thumb(self, pixmap: QPixmap) -> None:
        try:
            if pixmap is None or pixmap.isNull():
                self._thumb.setText("无图")
                return
            scaled = pixmap.scaled(
                _THUMB_SIZE, _THUMB_SIZE,
                Qt.AspectRatioMode.KeepAspectRatio,
                Qt.TransformationMode.SmoothTransformation,
            )
            self._thumb.setPixmap(scaled)
            self._thumb.setText("")
        except RuntimeError:
            pass

    def set_thumb_failed(self) -> None:
        try:
            self._thumb.setText("加载失败")
        except RuntimeError:
            pass

    # ------------------------------------------------------------------ #
    # 备注编辑
    # ------------------------------------------------------------------ #

    def _update_note_display(self):
        note = str(self.order.get("note") or "")
        if note.strip():
            shown = note.replace("\n", " ")
            self._note_label.setText(_elide(shown, 56))
            self._note_label.setToolTip(note)
        else:
            self._note_label.setText("暂无备注")
            self._note_label.setToolTip("")
        # 空备注用弱化样式
        try:
            self._note_label.setProperty("empty", not bool(note.strip()))
            self._note_label.style().unpolish(self._note_label)
            self._note_label.style().polish(self._note_label)
        except RuntimeError:
            pass

    def _enter_edit(self):
        if self._saving:
            return
        try:
            self._editor.setPlainText(str(self.order.get("note") or ""))
            self._error_label.setVisible(False)
            self._note_display.setVisible(False)
            self._editor_host.setVisible(True)
            self._editor.setFocus()
        except RuntimeError:
            pass

    def _cancel_edit(self):
        if self._saving:
            return
        try:
            self._editor_host.setVisible(False)
            self._note_display.setVisible(True)
        except RuntimeError:
            pass

    def _on_save_clicked(self):
        if self._saving:
            return
        try:
            text = self._editor.toPlainText().strip()
        except RuntimeError:
            return
        self._set_saving_state(True)
        self.save_note_requested.emit(
            str(self.order.get("order_sn") or ""), text,
            bool(str(self.order.get("note") or "").strip()),
        )

    def _set_saving_state(self, saving: bool):
        self._saving = saving
        try:
            self._save_btn.setText("保存中…" if saving else "保存")
            self._save_btn.setEnabled(not saving)
            self._cancel_btn.setEnabled(not saving)
            self._editor.setReadOnly(saving)
            if saving:
                self._error_label.setVisible(False)
        except RuntimeError:
            pass

    def set_note_result(self, ok: bool, err: str, remark: str):
        """保存结果回调（主线程）：成功退出编辑态并刷新显示；失败留在编辑态"""
        try:
            self._set_saving_state(False)
            if ok:
                self.order["note"] = remark
                self._update_note_display()
                self._editor_host.setVisible(False)
                self._note_display.setVisible(True)
                self._error_label.setVisible(False)
            else:
                self._error_label.setText(f"保存失败：{err}")
                self._error_label.setVisible(True)
        except RuntimeError:
            pass

    # ------------------------------------------------------------------ #
    # 主题
    # ------------------------------------------------------------------ #

    def _colors(self, dark: bool) -> Dict[str, str]:
        return {
            "bg": "#2b2b2b" if dark else "#ffffff",
            "border": "#3a3a3a" if dark else "#e6e6e6",
            "text": "#e0e0e0" if dark else "#333333",
            "sub": "#999999" if dark else "#8a8a8a",
            "sep": "#3a3a3a" if dark else "#f0f0f0",
            "thumb_bg": "#1e1e1e" if dark else "#f5f5f5",
            "editor_bg": "#1e1e1e" if dark else "#ffffff",
            "amount": "#ff6b5e" if dark else "#e02e24",
            "error": "#ff7875" if dark else "#d93025",
        }

    def apply_theme(self):
        dark = isDarkTheme()
        c = self._colors(dark)
        status_color = _status_color(str(self.order.get("status_str") or ""), dark)
        self.setStyleSheet(f"""
            OrderCard {{
                background-color: {c['bg']};
                border: 1px solid {c['border']};
                border-radius: 8px;
            }}
            #OrderSn {{
                color: {c['text']};
                font-size: 12px;
                font-weight: 600;
                background: transparent;
            }}
            #OrderStatus {{
                color: {status_color};
                font-size: 12px;
                font-weight: 600;
                background: transparent;
            }}
            #OrderThumb {{
                background-color: {c['thumb_bg']};
                color: {c['sub']};
                border-radius: 6px;
                font-size: 11px;
            }}
            #OrderGoodsName {{
                color: {c['text']};
                font-size: 12px;
                background: transparent;
            }}
            #OrderGoodsSub {{
                color: {c['sub']};
                font-size: 11px;
                background: transparent;
            }}
            #OrderAmount {{
                color: {c['amount']};
                font-size: 13px;
                font-weight: 600;
                background: transparent;
            }}
            #OrderMeta {{
                color: {c['sub']};
                font-size: 11px;
                background: transparent;
            }}
            #OrderSep {{
                background-color: {c['sep']};
                border: none;
            }}
            #OrderNoteTitle {{
                color: {c['sub']};
                font-size: 11px;
                background: transparent;
            }}
            #OrderNoteText {{
                color: {c['text']};
                font-size: 12px;
                background: transparent;
            }}
            #OrderNoteText[empty="true"] {{
                color: {c['sub']};
            }}
            #OrderNoteError {{
                color: {c['error']};
                font-size: 11px;
                background: transparent;
            }}
            #OrderNoteEditor {{
                background-color: {c['editor_bg']};
                color: {c['text']};
                border: 1px solid {c['border']};
                border-radius: 6px;
                padding: 4px 6px;
                font-size: 12px;
            }}
            #OrderNoteEditor:focus {{
                border-color: #4a90d9;
            }}
        """)


class OrderPopup(QFrame):
    """订单查看/备注编辑浮窗（Popup，挂在输入框上方）"""

    def __init__(self, parent=None):
        super().__init__(parent, Qt.WindowType.Popup | Qt.WindowType.FramelessWindowHint)
        self.setObjectName("OrderPopup")
        self.setFixedSize(620, 560)
        self.setFocusPolicy(Qt.FocusPolicy.NoFocus)

        self._shop_id = ""
        self._user_id = ""
        self._shop_name = ""
        self._buyer_uid = ""
        self._buyer_name = ""
        self._page = 1
        self._total = 0
        self._orders: List[Dict[str, Any]] = []
        self._cards: List[OrderCard] = []
        self._cards_by_sn: Dict[str, OrderCard] = {}
        self._worker: Optional[_OrderLoader] = None
        self._note_savers: Dict[str, _NoteSaver] = {}
        self._note_write_at: Dict[Tuple[str, str, str], float] = {}  # 写备注节流（按订单）
        self._render_token = 0
        self._pending: List[Tuple[str, OrderCard]] = []

        self._build_ui()
        self.refresh_theme()

    # ------------------------------------------------------------------ #
    # 构建
    # ------------------------------------------------------------------ #

    def _build_ui(self):
        layout = QVBoxLayout(self)
        layout.setContentsMargins(8, 8, 8, 8)
        layout.setSpacing(6)

        # ---- 顶部：账号 + 买家 + 刷新 ----
        top = QHBoxLayout()
        top.setSpacing(6)

        self._account_label = QLabel("")
        self._account_label.setObjectName("OrderAccount")
        top.addWidget(self._account_label)

        self._buyer_label = QLabel("")
        self._buyer_label.setObjectName("OrderBuyer")
        top.addWidget(self._buyer_label)
        top.addStretch()

        self._refresh_btn = TransparentToolButton(FluentIcon.SYNC)
        self._refresh_btn.setToolTip("刷新（忽略缓存）")
        self._refresh_btn.setFixedSize(32, 32)
        self._refresh_btn.clicked.connect(self._on_refresh)
        top.addWidget(self._refresh_btn)

        layout.addLayout(top)

        # ---- 中部：订单卡片列表 ----
        self._scroll = QScrollArea()
        self._scroll.setWidgetResizable(True)
        self._scroll.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        self._scroll.setFrameShape(QFrame.Shape.NoFrame)

        self._list_host = QWidget()
        self._list_layout = QVBoxLayout(self._list_host)
        self._list_layout.setContentsMargins(2, 2, 2, 2)
        self._list_layout.setSpacing(8)
        self._scroll.setWidget(self._list_host)
        layout.addWidget(self._scroll, 1)

        # ---- 底部：状态 + 分页 ----
        bottom = QHBoxLayout()
        bottom.setSpacing(6)

        self._status = QLabel("")
        self._status.setObjectName("OrderStatusBar")
        bottom.addWidget(self._status)
        bottom.addStretch()

        self._prev_btn = PushButton("上一页")
        self._prev_btn.setFixedWidth(72)
        self._prev_btn.clicked.connect(lambda: self._goto_page(self._page - 1))
        bottom.addWidget(self._prev_btn)

        self._page_label = QLabel("")
        self._page_label.setObjectName("OrderStatusBar")
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

    def open_for(self, shop_id: str, user_id: str, shop_name: str = "",
                 buyer_uid: str = "", buyer_name: str = "") -> None:
        """打开面板：绑定当前账号与买家并加载订单"""
        new_key = (str(shop_id or ""), str(user_id or ""), str(buyer_uid or ""))
        self._shop_name = str(shop_name or "")
        self._buyer_name = str(buyer_name or "")
        if new_key != (self._shop_id, self._user_id, self._buyer_uid):
            self._shop_id, self._user_id, self._buyer_uid = new_key
            self._reset_state()
        self._update_header()

        if not self._shop_id or not self._user_id:
            self._set_status("未选择会话或账号信息缺失")
            return
        if not self._buyer_uid:
            self._set_status("未选择会话买家，无法查询订单")
            return
        self._reload(use_cache=True)

    def reload(self) -> None:
        """重新加载（外部调用）"""
        self._reload(use_cache=True)

    # ------------------------------------------------------------------ #
    # 状态
    # ------------------------------------------------------------------ #

    def _update_header(self):
        try:
            name = self._shop_name or (f"店铺 {self._shop_id}" if self._shop_id else "")
            self._account_label.setText(f"订单 · {name}" if name else "订单")
            self._account_label.setToolTip(
                f"当前订单归属：{name}\n店铺 {self._shop_id}\n账号 {self._user_id}"
            )
            buyer = self._buyer_name or self._buyer_uid
            self._buyer_label.setText(f"买家 {buyer}" if buyer else "")
        except RuntimeError:
            pass

    def _reset_state(self):
        self._page = 1
        self._total = 0
        self._orders = []
        self._clear_list()
        self._page_label.setText("")
        self._prev_btn.setEnabled(False)
        self._next_btn.setEnabled(False)

    def _set_status(self, text: str):
        try:
            self._status.setText(text)
        except RuntimeError:
            pass

    def _cache_key(self) -> Tuple[str, str, str, int]:
        """列表缓存 / 在途请求标识：**含账号与买家**，避免跨会话串数据"""
        return (self._shop_id, self._user_id, self._buyer_uid, self._page)

    def _update_pager(self):
        pages = max(1, (self._total + _PAGE_SIZE - 1) // _PAGE_SIZE)
        self._page_label.setText(f"{self._page} / {pages}")
        self._prev_btn.setEnabled(self._page > 1)
        self._next_btn.setEnabled(self._page < pages)

    # ------------------------------------------------------------------ #
    # 数据加载
    # ------------------------------------------------------------------ #

    def _on_refresh(self):
        self._reload(use_cache=False)

    def _goto_page(self, page: int):
        if page < 1:
            return
        pages = max(1, (self._total + _PAGE_SIZE - 1) // _PAGE_SIZE)
        if page > pages:
            return
        self._page = page
        self._reload(use_cache=True)

    def _reload(self, use_cache: bool = True):
        if not self._shop_id or not self._user_id or not self._buyer_uid:
            self._set_status("缺少账号或买家信息，无法加载订单")
            return

        key = self._cache_key()
        if use_cache:
            cached = _ORDER_CACHE.get(key)
            if cached and (time.time() - cached[0]) < _CACHE_TTL:
                self._render(cached[1])
                return

        # 同一请求已在飞行中 → 复用，等它回来
        worker = self._current_worker()
        if worker is not None:
            try:
                if worker.isRunning() and worker.key == key:
                    self._set_status("加载中…")
                    return
            except RuntimeError:
                self._worker = None

        self._set_status("加载中…")
        self._clear_list()
        self._page_label.setText("")

        worker = _OrderLoader(self._shop_id, self._user_id, self._buyer_uid, self._page, self)
        worker.result.connect(self._on_loaded)
        # 线程结束后清引用，避免后续访问已被 deleteLater 的 C++ 对象
        # （PyQt 中此类 RuntimeError 若从信号槽抛出会被 qFatal → 进程 abort）
        worker.finished.connect(lambda w=worker: self._on_worker_finished(w))
        self._worker = worker
        worker.start()

    def _current_worker(self) -> Optional["_OrderLoader"]:
        w = self._worker
        if w is None:
            return None
        try:
            w.isRunning()
        except RuntimeError:
            self._worker = None
            return None
        return w

    def _on_worker_finished(self, worker: "_OrderLoader"):
        if self._worker is worker:
            self._worker = None
        try:
            worker.deleteLater()
        except RuntimeError:
            pass

    def _on_loaded(self, shop_id: str, user_id: str, buyer_uid: str, page: int,
                   data: object, error: str):
        # 丢弃过期结果（账号 / 买家 / 页码任一变化）
        if (shop_id, user_id, buyer_uid, page) != self._cache_key():
            return
        if not data:
            self._set_status((error or "加载失败")[:100])
            return
        self._render(data)  # type: ignore[arg-type]

    # ------------------------------------------------------------------ #
    # 渲染
    # ------------------------------------------------------------------ #

    def _clear_list(self):
        self._render_token += 1
        self._cards = []
        self._cards_by_sn = {}
        self._pending = []
        while self._list_layout.count():
            item = self._list_layout.takeAt(0)
            if item is None:
                continue
            w = item.widget()
            if w is not None:
                w.setParent(None)
                w.deleteLater()

    def _render(self, data: Dict[str, Any]):
        self._clear_list()
        self._render_token += 1
        token = self._render_token

        orders = data.get("orders") or []
        self._orders = orders
        self._total = int(data.get("total") or len(orders))
        self._update_pager()

        if not orders:
            self._set_status("该买家暂无订单")
            return

        self._set_status(f"共 {self._total} 个订单，本页 {len(orders)} 个")

        for o in orders:
            card = OrderCard(o, self._list_host)
            card.save_note_requested.connect(self._on_save_note_requested)
            self._list_layout.addWidget(card)
            self._cards.append(card)
            sn = str(o.get("order_sn") or "")
            if sn:
                self._cards_by_sn[sn] = card
            url = card.thumb_url()
            if url:
                self._pending.append((url, card))

        self._list_layout.addStretch(1)
        # 缩略图延后一拍再拉，先让列表完成布局
        QTimer.singleShot(0, lambda t=token: self._load_thumbs(t))

    def _load_thumbs(self, token: int):
        if token != self._render_token or not self._pending:
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

    def _on_thumb_ok(self, token: int, card: OrderCard, pixmap: QPixmap):
        if token != self._render_token:
            return
        try:
            card.set_thumb(pixmap)
        except RuntimeError:
            pass

    def _on_thumb_err(self, token: int, card: OrderCard):
        if token != self._render_token:
            return
        try:
            card.set_thumb_failed()
        except RuntimeError:
            pass

    # ------------------------------------------------------------------ #
    # 备注保存
    # ------------------------------------------------------------------ #

    def _on_save_note_requested(self, order_sn: str, text: str, has_note: bool):
        """卡片请求保存备注 → 节流后启动后台保存"""
        if not order_sn:
            return
        shop, user = self._shop_id, self._user_id
        # 服务端要求同一订单两次写备注间隔 > 1 秒：本地节流
        key = (shop, user, order_sn)
        last = self._note_write_at.get(key, 0.0)
        wait = max(0.0, 1.2 - (time.monotonic() - last)) if last else 0.0
        if wait > 0:
            logger.info(f"备注写入节流: orderSn={order_sn} 延迟 {wait * 1000:.0f}ms")
            QTimer.singleShot(
                int(wait * 1000) + 30,
                lambda: self._start_note_save(shop, user, order_sn, text, has_note),
            )
            return
        self._start_note_save(shop, user, order_sn, text, has_note)

    def _start_note_save(self, shop: str, user: str, order_sn: str,
                         text: str, has_note: bool):
        # 账号已切换 / 会话已变更 → 放弃（卡片可能也已销毁）
        if (shop, user) != (self._shop_id, self._user_id):
            logger.info(f"账号已切换，放弃保存备注: orderSn={order_sn}")
            return
        if order_sn in self._note_savers:
            return  # 同一订单已有保存在飞行中

        self._note_write_at[(shop, user, order_sn)] = time.monotonic()
        saver = _NoteSaver(shop, user, order_sn, text, has_note, self)
        saver.result.connect(self._on_note_saved)
        saver.finished.connect(lambda w=saver: self._on_note_saver_finished(w))
        self._note_savers[order_sn] = saver
        saver.start()
        logger.info(f"[ORDER] 保存备注: orderSn={order_sn}, len={len(text)}")

    def _on_note_saver_finished(self, worker: "_NoteSaver"):
        for sn, w in list(self._note_savers.items()):
            if w is worker:
                del self._note_savers[sn]
                break
        try:
            worker.deleteLater()
        except RuntimeError:
            pass

    def _on_note_saved(self, order_sn: str, ok: bool, err: str, remark: str):
        card = self._cards_by_sn.get(order_sn)
        if card is not None:
            try:
                card.set_note_result(ok, err, remark)
            except RuntimeError:
                card = None
        else:
            logger.warning(f"备注结果到达时卡片已不存在: orderSn={order_sn}")

        if ok:
            self._patch_cache_note(order_sn, remark)
            self._set_status("备注已保存")
            logger.info(f"[ORDER] 备注保存成功: orderSn={order_sn}")
        else:
            self._set_status(f"备注保存失败：{err}"[:120])
            logger.warning(f"[ORDER] 备注保存失败: orderSn={order_sn}, err={err}")

    @staticmethod
    def _patch_cache_note(order_sn: str, note: str):
        """把保存后的备注同步进缓存，避免短时间重开面板读到旧值"""
        for _key, (_ts, data) in list(_ORDER_CACHE.items()):
            for o in data.get("orders") or []:
                if str(o.get("order_sn") or "") == order_sn:
                    o["note"] = note

    # ------------------------------------------------------------------ #
    # 主题 / 显示事件
    # ------------------------------------------------------------------ #

    def refresh_theme(self):
        dark = isDarkTheme()
        bg = "#2b2b2b" if dark else "#ffffff"
        border = "#3a3a3a" if dark else "#e0e0e0"
        text = "#e0e0e0" if dark else "#333333"
        sub = "#999999" if dark else "#888888"

        self.setStyleSheet(f"""
            OrderPopup {{
                background-color: {bg};
                border: 1px solid {border};
                border-radius: 8px;
            }}
            #OrderAccount {{
                color: {text};
                font-size: 12px;
                font-weight: 600;
                padding-left: 4px;
                background: transparent;
            }}
            #OrderBuyer {{
                color: {sub};
                font-size: 12px;
                background: transparent;
            }}
            #OrderStatusBar {{
                color: {sub};
                background: transparent;
                font-size: 11px;
            }}
            QScrollArea {{
                border: none;
                background-color: {bg};
            }}
        """)
        try:
            self._list_host.setStyleSheet(f"background-color: {bg};")
        except RuntimeError:
            pass
        for card in list(self._cards):
            try:
                card.apply_theme()
            except RuntimeError:
                pass

    def hideEvent(self, event):  # noqa: N802
        """收起时丢掉未完成的缩略图队列，避免无意义下载"""
        self._pending = []
        super().hideEvent(event)

    def showEvent(self, event):  # noqa: N802
        super().showEvent(event)
        # Windows 上 Qt::Popup 窗口不会自动成为 Win32 前台窗口，中文输入法会
        # 把组合输入送给主窗口的控件。这里等窗口显示完成后显式激活自己，
        # 让「前台窗口 / 焦点控件」保持一致（与素材面板同一处理）。
        QTimer.singleShot(0, self._activate_self)

    def _activate_self(self) -> None:
        try:
            if self.isVisible():
                self.activateWindow()
        except RuntimeError:
            pass
