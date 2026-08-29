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
        """通过 IPC 从子进程读取知识库数据并过滤

        lancedb 在独立子进程中运行，主进程不直接 import lancedb。
        """
        import json
        from pathlib import Path

        try:
            # 通过 IPC 调用子进程获取所有文档
            from Agent.CustomerAgent.lancedb_proxy import get_ipc_client
            client = get_ipc_client()
            if not client.is_started:
                client.start()
            docs = client.call("get_all_documents_for_export")

            if not docs:
                logger.warning("知识库为空或 IPC 加载失败")
                return []

            # 转换为搜索结果格式
            results: List[Dict[str, str]] = []
            for doc in docs:
                title = doc.name or ""
                content = doc.content or ""
                if not content:
                    continue
                results.append({"title": title, "content": content})

            # 过滤
            return self._filter_results(results)

        except Exception as e:
            logger.error(f"IPC 搜索知识库失败: {e}")
            return []

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
            import jieba  # type: ignore[import-untyped]
            words = [w.strip() for w in jieba.cut_for_search(query) if w.strip() and len(w.strip()) >= 1]
        except ImportError:
            words = [query]

        if not words:
            words = [query]

        # 统一转小写，实现不区分大小写匹配
        words_lower = [w.lower() for w in words]

        # 对每个结果检查是否包含所有分词
        filtered = []
        for item in results:
            title = item.get("title", "")
            content = item.get("content", "")
            combined = (title + " " + content).lower()
            if all(w in combined for w in words_lower):
                filtered.append(item)

        # 如果过滤后结果太少，放宽条件：任一匹配
        if len(filtered) < 3:
            for item in results:
                title = item.get("title", "")
                content = item.get("content", "")
                combined = (title + " " + content).lower()
                if any(w in combined for w in words_lower) and item not in filtered:
                    filtered.append(item)

        return filtered[: self._limit]


class SlashKnowledgePopup(QListWidget):
    """斜杠检索浮窗

    用 QListWidget 实现的浮窗，显示知识库候选项。
    不自动定位 —— 由 InputArea 控制 geometry。
    """

    item_selected = pyqtSignal(str)  # 选中后 emit(content)
    position_requested = pyqtSignal()  # 需要重新定位浮窗位置

    def __init__(self, parent=None):
        super().__init__(parent)
        self._apply_style()
        # 改用 Tool 窗口类型，不抢焦点，输入焦点留在输入框
        self.setWindowFlags(
            Qt.WindowType.Tool
            | Qt.WindowType.FramelessWindowHint
        )
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
        """触发搜索（带防抖 200ms）

        query 为空时不弹浮窗，避免输入"/"就弹出无关内容。
        """
        self._pending_query = query
        if not query.strip():
            # 无关键词时停止防抖定时器 + 隐藏浮窗
            # 必须停止定时器，否则之前已启动的定时器会在 200ms 后
            # 用空的 _pending_query 触发搜索 → 返回全部条目 → 孤儿浮窗
            self._debounce_timer.stop()
            self.hide()
            return
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
        # 如果查询已被清空或浮窗已不可见，丢弃过期的异步结果
        if not self._pending_query.strip():
            self.hide()
            return
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
        # 先 show 再定位，保证 height 已确定
        self.show()
        self.raise_()
        # 通知 InputArea 重新定位
        self.position_requested.emit()

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

    def cancel(self):
        """取消搜索：停止防抖定时器、清空 pending query、隐藏浮窗

        用于 InputArea 退出斜杠模式时清理状态，防止异步搜索
        在模式退出后仍弹出孤儿浮窗。
        """
        self._debounce_timer.stop()
        self._pending_query = ""
        self.hide()

    def refresh_theme(self):
        """主题变化时刷新样式"""
        self._apply_style()

    def cleanup(self):
        """清理后台线程"""
        if self._worker is not None:
            self._worker.quit()
            self._worker.wait(500)
            self._worker = None
