"""
斜杠快捷知识库检索浮窗
======================
- 输入框输入 "/" 后触发检索
- 后台线程查询知识库（jieba分词 + LIKE）
- QListWidget 浮窗显示候选项
- 支持鼠标点击 / 键盘上下选择 + Enter 确认
- 选中后用知识库 content 替换斜杠及检索文本
"""

from typing import List, Dict, Any
from PyQt6.QtCore import Qt, QThread, pyqtSignal, QTimer
from PyQt6.QtWidgets import QListWidget, QListWidgetItem
from qfluentwidgets import isDarkTheme


class _KnowledgeSearchWorker(QThread):
    """后台线程执行知识库搜索，避免阻塞 UI"""
    results_ready = pyqtSignal(list)  # List[Dict]

    def __init__(self, shop_id: int, query: str, limit: int = 8, parent=None):
        super().__init__(parent)
        self._shop_id = shop_id
        self._query = query
        self._limit = limit

    def run(self):
        try:
            from database.knowledge_service import KnowledgeService
            svc = KnowledgeService()
            results = svc.search_customer_service_quick(
                shop_id=self._shop_id,
                query=self._query,
                limit=self._limit,
            )
            self.results_ready.emit(results)
        except Exception:
            self.results_ready.emit([])


class SlashKnowledgePopup(QListWidget):
    """斜杠检索浮窗

    用 QListWidget 实现的浮窗，显示知识库候选项。
    不自动定位 —— 由 InputArea 控制 geometry。
    """

    item_selected = pyqtSignal(str)  # 选中后 emit(content)
    popup_hidden = pyqtSignal()  # 浮窗关闭（失去焦点等）

    def __init__(self, parent=None):
        super().__init__(parent)
        self._apply_style()
        self.setWindowFlags(Qt.WindowType.Popup)
        self.setFocusPolicy(Qt.FocusPolicy.NoFocus)
        self.setMouseTracking(True)
        self.setFixedWidth(400)
        self.setVisible(False)
        self.itemClicked.connect(self._on_item_clicked)
        self._worker: _KnowledgeSearchWorker | None = None
        self._debounce_timer = QTimer(self)
        self._debounce_timer.setSingleShot(True)
        self._debounce_timer.timeout.connect(self._do_search)
        self._shop_id: int | None = None
        self._pending_query: str = ""

    def hideEvent(self, event):
        """浮窗关闭时通知 InputArea"""
        super().hideEvent(event)
        self.popup_hidden.emit()

    def _apply_style(self):
        dark = isDarkTheme()
        bg = "#2b2b2b" if dark else "#ffffff"
        border = "#3a3a3a" if dark else "#e0e0e0"
        hover_bg = "#3a3a3a" if dark else "#f0f7ff"
        text_color = "#e0e0e0" if dark else "#333333"
        selected_bg = "#4a90d9" if dark else "#4a90d9"
        self.setStyleSheet(f"""
            QListWidget {{
                background-color: {bg};
                border: 1px solid {border};
                border-radius: 6px;
                padding: 4px;
                color: {text_color};
                outline: none;
            }}
            QListWidget::item {{
                padding: 6px 10px;
                border-radius: 4px;
            }}
            QListWidget::item:hover {{
                background-color: {hover_bg};
            }}
            QListWidget::item:selected {{
                background-color: {selected_bg};
                color: white;
            }}
        """)

    def set_shop_id(self, shop_id: int | None):
        """设置当前店铺 ID（数据库 Shop.id）"""
        self._shop_id = shop_id

    def search(self, query: str):
        """触发搜索（带防抖 200ms）"""
        if self._shop_id is None:
            return
        self._pending_query = query
        # 空查询也搜索（显示最近条目）
        self._debounce_timer.start(200)

    def _do_search(self):
        """实际执行后台搜索"""
        if self._shop_id is None:
            return
        # 取消旧 worker
        if self._worker is not None:
            try:
                self._worker.results_ready.disconnect(self._on_results)
            except (TypeError, RuntimeError):
                pass
            self._worker.quit()
            self._worker.wait(300)
            self._worker = None

        self._worker = _KnowledgeSearchWorker(self._shop_id, self._pending_query)
        self._worker.results_ready.connect(self._on_results)
        self._worker.start()

    def _on_results(self, results: list):
        """搜索完成，更新浮窗"""
        self.clear()
        if not results:
            self.hide()
            return

        for item_data in results:
            title = item_data.get("title", "")
            content = item_data.get("content", "")
            # 显示标题 + 内容预览（前60字）
            preview = content[:60].replace("\n", " ")
            if len(content) > 60:
                preview += "..."
            display_text = f"{title}\n{preview}" if title else preview

            list_item = QListWidgetItem(display_text)
            list_item.setData(Qt.ItemDataRole.UserRole, content)
            list_item.setToolTip(title)
            self.addItem(list_item)

        # 自适应高度
        self.adjust_height()
        self.show()
        self.raise_()

        # 默认选中第一项
        if self.count() > 0:
            self.setCurrentRow(0)

    def adjust_height(self):
        """根据条目数量自适应高度"""
        count = self.count()
        if count == 0:
            self.hide()
            return
        # 每项约 50px（两行 + padding），最高 6 项
        visible = min(count, 6)
        h = visible * 52 + 16  # padding
        self.setFixedHeight(h)

    def _on_item_clicked(self, item: QListWidgetItem):
        """鼠标点击选中"""
        content = item.data(Qt.ItemDataRole.UserRole)
        self.item_selected.emit(content)
        self.hide()

    def select_next(self):
        """键盘向下选择"""
        if self.count() == 0 or not self.isVisible():
            return
        row = self.currentRow()
        if row < self.count() - 1:
            self.setCurrentRow(row + 1)

    def select_prev(self):
        """键盘向上选择"""
        if self.count() == 0 or not self.isVisible():
            return
        row = self.currentRow()
        if row > 0:
            self.setCurrentRow(row - 1)

    def confirm_selection(self) -> bool:
        """确认当前选中项，返回是否成功"""
        if not self.isVisible() or self.count() == 0:
            return False
        item = self.currentItem()
        if item is None:
            return False
        content = item.data(Qt.ItemDataRole.UserRole)
        self.item_selected.emit(content)
        self.hide()
        return True

    def refresh_theme(self):
        """主题变化时刷新样式"""
        self._apply_style()

    def cleanup(self):
        """清理后台线程"""
        if self._worker is not None:
            self._worker.quit()
            self._worker.wait(500)
            self._worker = None
