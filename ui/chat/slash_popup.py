"""
斜杠快捷知识库检索浮窗
======================
- 输入框输入 "/" 后触发检索
- 后台线程查询 LanceDB 向量库（直接读 payload，不走向量搜索）
- QListWidget 浮窗显示候选项
- 支持鼠标点击 / 键盘上下选择 + Enter 确认
- 选中后用知识库 content 替换斜杠及检索文本
"""

from typing import List, Dict, Any
from PyQt6.QtCore import Qt, QThread, pyqtSignal, QTimer
from PyQt6.QtWidgets import QListWidget, QListWidgetItem
from qfluentwidgets import isDarkTheme

from utils.logger_loguru import get_logger

logger = get_logger("SlashPopup")


class _KnowledgeSearchWorker(QThread):
    """后台线程执行知识库搜索，避免阻塞 UI

    直接从 LanceDB 读取所有行的 payload（JSON），解析出 title/content，
    然后用 jieba 分词 + 文本包含匹配做过滤。
    不走向量搜索，不需要嵌入模型，速度快。
    """

    results_ready = pyqtSignal(list)  # List[Dict[str, str]]

    def __init__(self, query: str, limit: int = 8, parent=None):
        super().__init__(parent)
        self._query = query
        self._limit = limit

    def run(self):
        try:
            results = self._search_lancedb()
            self.results_ready.emit(results)
        except Exception as e:
            logger.error(f"斜杠检索后台搜索失败: {e}", exc_info=True)
            self.results_ready.emit([])

    def _search_lancedb(self) -> List[Dict[str, str]]:
        """直接从 LanceDB 读取数据并过滤"""
        import json
        import os
        from pathlib import Path

        # 定位 vector_db 路径（与 KnowledgeManager 一致）
        project_root = Path(__file__).resolve().parent.parent.parent.parent
        vector_path = project_root / "data" / "vector_db"
        table_name = "customer_knowledge"

        # 尝试用 lance 库读取
        try:
            import lance
            dataset = lance.dataset(str(vector_path / f"{table_name}.lance"))
            if dataset.count_rows() == 0:
                logger.warning("LanceDB customer_knowledge 表为空，无知识库数据")
                return []
            table = dataset.to_table()
        except ImportError:
            # 回退到 lancedb
            try:
                import lancedb
                db = lancedb.connect(str(vector_path))
                tbl = db.open_table(table_name)
                df = tbl.to_pandas()
                if len(df) == 0:
                    logger.warning("LanceDB customer_knowledge 表为空")
                    return []
            except Exception as e:
                logger.error(f"无法连接 LanceDB: {e}")
                return []

            # 从 DataFrame 提取数据
            return self._filter_from_dataframe(df)

        # 从 Arrow Table 提取数据
        results: List[Dict[str, str]] = []
        col_names = table.column_names

        for i in range(table.num_rows):
            row_data: Dict[str, Any] = {}
            for col_name in col_names:
                if col_name == "vector":
                    continue
                try:
                    row_data[col_name] = table.column(col_name)[i].as_py()
                except Exception:
                    row_data[col_name] = ""

            # 解析 payload
            payload_str = row_data.get("payload", "")
            title = ""
            content = ""

            if payload_str:
                try:
                    payload = json.loads(payload_str)
                    content = payload.get("content", "")
                    meta = payload.get("meta_data", {})
                    title = meta.get("title", "") or payload.get("name", "")
                except (json.JSONDecodeError, TypeError):
                    content = str(payload_str)

            if not content:
                continue

            results.append({"title": title, "content": content})

        # 过滤
        return self._filter_results(results)

    def _filter_from_dataframe(self, df) -> List[Dict[str, str]]:
        """从 pandas DataFrame 提取并过滤数据"""
        import json

        results: List[Dict[str, str]] = []
        for _, row in df.iterrows():
            row_dict = row.to_dict()
            payload_str = row_dict.get("payload", "")
            title = ""
            content = ""

            if payload_str:
                try:
                    payload = json.loads(payload_str)
                    content = payload.get("content", "")
                    meta = payload.get("meta_data", {})
                    title = meta.get("title", "") or payload.get("name", "")
                except (json.JSONDecodeError, TypeError):
                    content = str(payload_str)

            if not content:
                continue

            results.append({"title": title, "content": content})

        return self._filter_results(results)

    def _filter_results(self, results: List[Dict[str, str]]) -> List[Dict[str, str]]:
        """根据查询关键词过滤结果"""
        if not results:
            return []

        query = self._query.strip() if self._query else ""

        if not query:
            # 无关键词，返回最近的条目
            return results[: self._limit]

        # 使用 jieba 分词
        try:
            import jieba
            words = [w.strip() for w in jieba.cut_for_search(query) if w.strip() and len(w.strip()) >= 1]
        except ImportError:
            words = [query]

        if not words:
            words = [query]

        # 对每个结果检查是否包含所有分词
        filtered = []
        for item in results:
            title = item.get("title", "")
            content = item.get("content", "")
            combined = title + " " + content
            if all(w in combined for w in words):
                filtered.append(item)

        # 如果过滤后结果太少，放宽条件：任一匹配
        if len(filtered) < 3:
            for item in results:
                title = item.get("title", "")
                content = item.get("content", "")
                combined = title + " " + content
                if any(w in combined for w in words) and item not in filtered:
                    filtered.append(item)

        return filtered[: self._limit]


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

    def search(self, query: str):
        """触发搜索（带防抖 200ms）"""
        self._pending_query = query
        self._debounce_timer.start(200)

    def _do_search(self):
        """实际执行后台搜索"""
        # 取消旧 worker
        if self._worker is not None:
            try:
                self._worker.results_ready.disconnect(self._on_results)
            except (TypeError, RuntimeError):
                pass
            self._worker.quit()
            self._worker.wait(300)
            self._worker = None

        self._worker = _KnowledgeSearchWorker(self._pending_query)
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
