"""
快捷语录联想浮窗（原斜杠检索）
==============================
- 输入框输入任意文本即触发联想，无需先输入 "/"
- 仍支持 "/" 显式触发（该模式下自动选中首项，Enter 直接插入）
- 后台线程查询 LanceDB 向量库（直接读 payload，不走向量搜索）
- QListWidget 浮窗显示候选项
- 支持鼠标点击 / 键盘上下选择 + Tab/Enter 确认
- 选中后用知识库 content 替换当前输入片段
- 键盘行为：未显式导航时 Enter 仍为发送，避免误插入
"""

import math
import threading
import time
from typing import Callable, List, Dict, Any, Optional
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

    results_ready = pyqtSignal(str, list)  # (query, List[Dict[str, str]])
    _MATCH_RATIO = 0.5  # 最长连续命中片段占 query 长度的最低比例（可调）

    def __init__(self, query: str, limit: int = 8,
                 doc_provider: Optional[Callable[[], List[Any]]] = None,
                 parent=None):
        super().__init__(parent)
        self._query = query
        self._limit = limit
        self._doc_provider = doc_provider

    def run(self):
        try:
            results = self._search_lancedb()
            self.results_ready.emit(self._query, results)
        except Exception as e:
            logger.error(f"快捷语录后台搜索失败: {e}", exc_info=True)
            self.results_ready.emit(self._query, [])

    @staticmethod
    def _to_doc_pair(doc: Any):
        """兼容 dataclass / dict 两种文档结构"""
        if isinstance(doc, dict):
            return doc.get("name") or "", doc.get("content") or ""
        return getattr(doc, "name", "") or "", getattr(doc, "content", "") or ""

    def _search_lancedb(self) -> List[Dict[str, str]]:
        """通过 IPC 从子进程读取知识库数据并过滤

        lancedb 在独立子进程中运行，主进程不直接 import lancedb。
        docs 由外部 provider 提供时可复用缓存，避免每次输入都走 IPC。
        """
        try:
            docs = self._doc_provider() if self._doc_provider else self._fetch_docs()

            if not docs:
                logger.warning("知识库为空或 IPC 加载失败")
                return []

            # 转换为搜索结果格式
            results: List[Dict[str, str]] = []
            for doc in docs:
                title, content = self._to_doc_pair(doc)
                if not content:
                    continue
                results.append({"title": title, "content": content})

            # 过滤
            return self._filter_results(results)

        except Exception as e:
            logger.error(f"IPC 搜索知识库失败: {e}")
            return []

    @staticmethod
    def _fetch_docs() -> List[Any]:
        """直接通过 IPC 拉取全量文档"""
        from Agent.CustomerAgent.lancedb_proxy import get_ipc_client
        client = get_ipc_client()
        if not client.is_started:
            client.start()
        return client.call("get_all_documents_for_export") or []

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

        # 第一阶段：分词匹配（有 jieba 时）
        words = self._tokenize(query)
        scored = self._score_by_terms(results, words) if words else []

        # 第二阶段：无 jieba 或整句分词没命中 → n-gram 回退
        # 例："运费怎么算" 整句匹配不到，但 4-gram/3-gram 能命中 "运费"
        if not scored:
            for n in range(min(4, len(query)), 1, -1):
                grams = [query[i:i + n] for i in range(len(query) - n + 1)]
                scored = self._score_by_terms(results, grams)
                if scored:
                    break

        if not scored and len(query) == 1:
            scored = self._score_by_terms(results, [query])

        if not scored:
            return []

        # 命中质量闸门：最长连续命中片段占 query 长度的比例 >= _MATCH_RATIO
        # 输入越长要求越严 —— 多打无关字会稀释命中片段，浮窗随之关闭
        need = max(2, math.ceil(len(query) * self._MATCH_RATIO)) if len(query) > 1 else 1
        lowered = query.lower()
        passed = []
        for score, idx, item in scored:
            combined = (item.get("title", "") + " " + item.get("content", "")).lower()
            if self._passes_match_ratio(lowered, combined, need):
                passed.append((score, idx, item))

        if not passed:
            return []

        passed.sort(key=lambda t: (-t[0], t[1]))
        return [t[2] for t in passed[: self._limit]]

    @classmethod
    def _passes_match_ratio(cls, query: str, combined: str, need: int) -> bool:
        """query 与条目文本的最长公共连续片段是否达到 need

        单字查询按整字匹配（与原有行为一致）；need 由 _MATCH_RATIO 推导。
        """
        n = len(query)
        if n == 1:
            return query in combined
        for length in range(need, n + 1):
            for i in range(n - length + 1):
                if query[i:i + length] in combined:
                    return True
        return False

    @staticmethod
    def _tokenize(query: str) -> List[str]:
        """分词（jieba 可用时），失败返回空列表由调用方回退"""
        try:
            import jieba  # type: ignore[import-untyped]
            words = [w.strip() for w in jieba.cut_for_search(query) if w.strip()]
            return words if words else []
        except Exception:
            return []

    @staticmethod
    def _score_by_terms(results: List[Dict[str, str]],
                        terms: List[str]) -> List[tuple]:
        """按词条命中打分：命中数 + 标题命中加权"""
        terms = [t.lower() for t in terms if t]
        if not terms:
            return []

        scored = []
        for idx, item in enumerate(results):
            title = item.get("title", "").lower()
            content = item.get("content", "").lower()
            combined = title + " " + content
            score = sum(1 for t in terms if t in combined)
            score += sum(1 for t in terms if t in title)
            if score > 0:
                scored.append((score, idx, item))
        return scored


class SlashKnowledgePopup(QListWidget):
    """快捷语录联想浮窗

    用 QListWidget 实现的浮窗，显示知识库候选项。
    不自动定位 —— 由 InputArea 控制 geometry。

    两种激活方式：
    - 显式（输入 "/"）：auto_select=True，默认选中首项，Enter 直接插入
    - 隐式（普通输入）：auto_select=False，不自动选中，Enter 仍为发送，
      需按 ↑/↓ 或点击/Tab 才插入，避免误伤正常发送
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
        self._auto_select: bool = False
        self._nav_active: bool = False  # 是否已进入键盘导航/可确认状态
        self._suppressed_query: str = ""  # 被 Esc/点击外部关闭时的 query，避免立刻重弹
        self._cache_lock = threading.Lock()
        self._doc_cache: tuple = ()  # (ts, docs)

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

    # ========== 知识库文档缓存 ==========

    _DOC_CACHE_TTL = 60.0  # 秒

    def _get_docs(self) -> List[Any]:
        """带 TTL 的文档缓存：避免每次按键都走 IPC 拉全库"""
        now = time.monotonic()
        with self._cache_lock:
            if self._doc_cache and now - self._doc_cache[0] < self._DOC_CACHE_TTL:
                return self._doc_cache[1]
            docs = _KnowledgeSearchWorker._fetch_docs()
            self._doc_cache = (now, docs)
            return docs

    def invalidate_cache(self):
        """知识库变更后调用，清空缓存"""
        with self._cache_lock:
            self._doc_cache = ()

    def search(self, query: str, auto_select: bool = False):
        """触发搜索（带防抖 200ms）

        query 为空时不弹浮窗，避免输入"/"就弹出无关内容。
        auto_select=True 时结果回来后自动选中首项（显式斜杠模式）。
        """
        self._pending_query = query
        if query != self._suppressed_query:
            self._suppressed_query = ""  # query 变化，解除抑制
        if not query.strip() or query == self._suppressed_query:
            # 无关键词 / 已被用户关闭：停止防抖定时器 + 隐藏浮窗
            # 必须停止定时器，否则之前已启动的定时器会在 200ms 后
            # 用空的 _pending_query 触发搜索 → 返回全部条目 → 孤儿浮窗
            self._debounce_timer.stop()
            self.hide()
            return
        self._auto_select = auto_select
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

        self._worker = _KnowledgeSearchWorker(
            self._pending_query, doc_provider=self._get_docs
        )
        self._worker.results_ready.connect(self._on_results)
        self._worker.start()

    def _on_results(self, query: str, results: list):
        """搜索完成，更新浮窗"""
        # 丢弃过期结果：用户已继续输入，结果对应的 query 不再是当前 query
        if query != self._pending_query:
            return
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

        # 显式模式自动选中首项；隐式模式不选中，Enter 仍走发送
        self._nav_active = bool(self._auto_select)
        if self.count() > 0:
            self.setCurrentRow(0 if self._nav_active else -1)
        else:
            self._nav_active = False

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

    def is_nav_active(self) -> bool:
        """是否已进入可确认状态（显式模式或按过 ↑/↓）"""
        return self._nav_active and self.isVisible() and self.count() > 0

    def select_next(self):
        """键盘向下选择（首次按下即从 -1 进入导航态）"""
        if self.count() == 0 or not self.isVisible():
            return
        self._nav_active = True
        row = self.currentRow()
        self.setCurrentRow(0 if row < 0 else min(row + 1, self.count() - 1))

    def select_prev(self):
        """键盘向上选择"""
        if self.count() == 0 or not self.isVisible():
            return
        self._nav_active = True
        row = self.currentRow()
        self.setCurrentRow(0 if row < 0 else max(row - 1, 0))

    def confirm_selection(self) -> bool:
        """确认当前选中项，返回是否成功

        仅当用户已进入导航态（显式斜杠模式或按过 ↑/↓）才插入，
        否则返回 False 让 Enter 继续走发送逻辑。
        """
        if not self.isVisible() or self.count() == 0 or not self._nav_active:
            return False
        item = self.currentItem()
        if item is None:
            return False
        content = item.data(Qt.ItemDataRole.UserRole)
        self.item_selected.emit(content)
        self._nav_active = False
        self.hide()
        return True

    def dismiss(self):
        """用户主动关闭（Esc / 点击外部）：记住当前 query，避免立刻重弹"""
        self.suppress(self._pending_query)

    def suppress(self, query: str):
        """隐藏并抑制指定 query（该 query 未变化前不再自动弹出）"""
        self._debounce_timer.stop()
        self._suppressed_query = query
        self._nav_active = False
        self.hide()

    def cancel(self):
        """取消搜索：停止防抖定时器、清空 pending query、隐藏浮窗

        用于 InputArea 退出联想模式时清理状态，防止异步搜索
        在模式退出后仍弹出孤儿浮窗。
        """
        self._debounce_timer.stop()
        self._pending_query = ""
        self._suppressed_query = ""
        self._nav_active = False
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
