"""
知识库管理UI模块

提供知识库数据展示、添加、导入和删除功能。
"""

from __future__ import annotations
import asyncio
import os
from typing import TYPE_CHECKING, Optional, List
from PyQt6.QtWidgets import (
    QWidget, QVBoxLayout, QHBoxLayout, QLabel,
    QScrollArea, QFrame, QGridLayout, QFileDialog, QMessageBox, QDialog,
    QSizePolicy
)
from PyQt6.QtCore import Qt, QTimer, QThread, pyqtSignal
from qfluentwidgets import (
    FluentIcon, PrimaryPushButton, PushButton,
    InfoBar, InfoBarPosition
)

if TYPE_CHECKING:
    from Agent.CustomerAgent.agent_knowledge import KnowledgeManager
from utils.logger_loguru import get_logger
from utils.file_validator import FileValidator, ExcelValidator

from .knowledge.models import SimpleDocument, ImportError as KnowledgeImportError
from .knowledge.widgets import KnowledgeCard, AddKnowledgeDialog

logger = get_logger(__name__)


class ImportWorker(QThread):
    """导入工作线程，在后台执行异步导入操作"""

    success = pyqtSignal(int)
    failed = pyqtSignal(str)

    def __init__(self, knowledge_manager: KnowledgeManager, file_path: str):
        super().__init__()
        self.knowledge_manager = knowledge_manager
        self.file_path = file_path

    def run(self):
        """在子线程中运行异步导入"""
        try:
            # 在子线程中创建新的事件循环
            loop = asyncio.new_event_loop()
            asyncio.set_event_loop(loop)

            # 运行异步导入
            count = loop.run_until_complete(self._import_async())
            self.success.emit(count)
        except Exception as e:
            self.failed.emit(str(e))
        finally:
            # 清理事件循环
            try:
                loop.close()
            except:
                pass

    async def _import_async(self) -> int:
        """异步导入知识库文件"""
        # 导入前文件预检查
        logger.info("正在进行文件预检查...")
        from utils.file_validator import FileValidator, ExcelValidator

        file_ext = os.path.splitext(self.file_path)[1].lower()

        # 根据文件类型验证
        if file_ext in ['.xlsx', '.xls']:
            validator = ExcelValidator()
            result = validator.validate_readable(self.file_path)
            if not result.is_valid and result.error_type == "MISSING_DEPENDENCY":
                result = validator.validate_basic(self.file_path)
            if not result.is_valid:
                raise KnowledgeImportError(result.error_message or "文件验证失败", result.suggestions)
        else:
            validator = FileValidator()
            result = validator.validate_basic(self.file_path)
            if not result.is_valid:
                raise KnowledgeImportError(result.error_message or "文件验证失败", result.suggestions)

        logger.info("文件预检查通过")

        # 对于文本类文件（CSV、TXT、MD等），可能需要编码转换
        actual_file_path = self.file_path
        if file_ext in ['.csv', '.txt', '.text', '.md', '.markdown']:
            actual_file_path = self._ensure_utf8_encoding(self.file_path)

        # 获取导入前的文档数量
        count_before = self.knowledge_manager.get_content_count()

        # 使用标准导入方法
        imported_count = await self.knowledge_manager.add_content_from_file(actual_file_path)

        # 获取导入后的文档数量
        count_after = self.knowledge_manager.get_content_count()
        actual_imported = count_after - count_before

        logger.info(f"导入成功,实际新增文档数量: {actual_imported}")

        # 清理临时文件
        if actual_file_path != self.file_path and os.path.exists(actual_file_path):
            try:
                os.remove(actual_file_path)
            except:
                pass

        if actual_imported == 0 and imported_count == 0:
            raise KnowledgeImportError.from_empty_file()

        return max(actual_imported, imported_count)

    def _ensure_utf8_encoding(self, file_path: str) -> str:
        """
        确保文件使用UTF-8编码，如果不是则转换

        Args:
            file_path: 原始文件路径

        Returns:
            UTF-8编码的文件路径（可能是原文件或临时文件）
        """
        from utils.encoding_helper import EncodingConverter

        temp_path, encoding = EncodingConverter.ensure_utf8(file_path)
        logger.info(f"检测到文件编码: {encoding}")

        return temp_path


class AddKnowledgeWorker(QThread):
    """添加知识工作线程，在后台执行异步添加操作"""

    success = pyqtSignal(str)  # 传递标题
    failed = pyqtSignal(str, str)  # 传递标题和错误信息

    def __init__(self, knowledge_manager: KnowledgeManager, title: str, content: str):
        super().__init__()
        self.knowledge_manager = knowledge_manager
        self.title = title
        self.content = content

    def run(self):
        """在子线程中运行异步添加"""
        try:
            # 在子线程中创建新的事件循环
            loop = asyncio.new_event_loop()
            asyncio.set_event_loop(loop)

            # 运行异步添加
            loop.run_until_complete(self._add_async())
            self.success.emit(self.title)
        except Exception as e:
            self.failed.emit(self.title, str(e))
        finally:
            # 清理事件循环
            try:
                loop.close()
            except:
                pass

    async def _add_async(self) -> None:
        """异步添加知识内容"""
        # 直接存储原始内容，metadata 中已有 title
        await self.knowledge_manager.knowledge.add_content_async(
            name=self.title,  # 使用标题作为 name，确保每个文档有唯一的 ID
            text_content=self.content,
            metadata={
                'title': self.title,
                'source': 'manual_input',
                'filename': f"{self.title}.txt"
            }
        )
        logger.info(f"成功添加文本内容: {self.title}")


class DeleteWorker(QThread):
    """删除文档工作线程，在后台执行删除操作"""

    success = pyqtSignal(str, str)  # 传递 (doc_id, doc_title)
    failed = pyqtSignal(str, str, str)  # 传递 (doc_id, doc_title, error_message)

    def __init__(self, knowledge_manager: KnowledgeManager, doc_id: str, doc_title: str):
        super().__init__()
        self.knowledge_manager = knowledge_manager
        self.doc_id = doc_id
        self.doc_title = doc_title

    def run(self):
        """在子线程中运行删除操作"""
        try:
            # 执行删除（同步操作，已经在子线程中）
            success = self.knowledge_manager.delete_document(self.doc_id)

            if success:
                self.success.emit(self.doc_id, self.doc_title)
            else:
                self.failed.emit(self.doc_id, self.doc_title, "删除操作失败")

        except Exception as e:
            self.failed.emit(self.doc_id, self.doc_title, str(e))


class LoadDataWorker(QThread):
    """数据加载工作线程，在后台执行异步加载操作"""

    finished = pyqtSignal(list)  # 传递加载的文档列表
    failed = pyqtSignal(str)     # 错误消息

    def __init__(self, knowledge_manager: KnowledgeManager):
        super().__init__()
        self.knowledge_manager = knowledge_manager

    def run(self):
        """在子线程中运行异步加载"""
        try:
            # 在子线程中创建新的事件循环
            loop = asyncio.new_event_loop()
            asyncio.set_event_loop(loop)

            # 运行异步加载
            docs = loop.run_until_complete(self._load_async())
            self.finished.emit(docs)
        except Exception as e:
            self.failed.emit(str(e))
        finally:
            # 清理事件循环
            try:
                loop.close()
            except:
                pass

    async def _load_async(self) -> list:
        """异步加载文档数据"""
        from ui.knowledge.data_loader import KnowledgeDataLoader

        try:
            # 使用 KnowledgeDataLoader 加载数据
            loader = KnowledgeDataLoader(self.knowledge_manager)
            docs = loader.load_documents()

            logger.info(f"成功加载 {len(docs)} 个文档")
            return docs

        except Exception as e:
            logger.error(f"加载文档失败: {str(e)}")
            raise


class KnowledgeUI(QWidget):
    """
    知识库管理界面

    提供知识库的可视化管理功能，包括：
    - 知识文档卡片展示
    - 添加/删除知识
    - 导入文件到知识库
    - 刷新数据
    """

    # 类常量
    INITIAL_LOAD_DELAY = 500  # 初始加载延迟（ms）
    RESIZE_DEBOUNCE_DELAY = 150  # 调整大小防抖延迟（ms）
    DEFAULT_COLUMNS = 2  # 默认列数
    CARD_SPACING = 16  # 卡片间距

    BUTTON_WIDTH = 120
    BUTTON_HEIGHT = 36

    def __init__(self, parent: Optional[QWidget] = None):
        """
        初始化知识库UI

        Args:
            parent: 父组件
        """
        super().__init__(parent)
        self.setWindowTitle('知识库数据展示')
        self.setObjectName('Knowledge-UI')
        self.resize(900, 700)

        # 成员变量
        self.knowledge_manager: Optional[KnowledgeManager] = None
        self.docs: List[SimpleDocument] = []
        self._layout_initialized = False

        # 数据缓存
        self._cached_docs: List[SimpleDocument] = []
        self._cache_valid = False

        # 分页相关
        self._current_page = 1  # 当前页码（从1开始）
        self._page_size = 12  # 每页显示数量
        self._total_pages = 1  # 总页数

        # 设置大小策略
        self.setSizePolicy(
            QSizePolicy.Policy.Expanding,
            QSizePolicy.Policy.Expanding
        )

        # 防抖定时器
        self._resize_timer = QTimer()
        self._resize_timer.setSingleShot(True)
        self._resize_timer.timeout.connect(self._handle_resize_timeout)

        # 初始化UI
        self._init_ui()

        # 延迟加载数据
        QTimer.singleShot(self.INITIAL_LOAD_DELAY, self.populate_cards)

    def _init_ui(self) -> None:
        """初始化UI组件"""
        # 主布局
        self.mainLayout = QVBoxLayout(self)
        self.setLayout(self.mainLayout)

        # 顶部工具栏
        self.toolbar = QHBoxLayout()
        self.toolbar.setContentsMargins(16, 16, 16, 8)

        add_btn = PrimaryPushButton("添加知识")
        add_btn.clicked.connect(self.add_knowledge)
        add_btn.setFixedWidth(self.BUTTON_WIDTH)
        add_btn.setFixedHeight(self.BUTTON_HEIGHT)
        add_btn.setIcon(FluentIcon.ADD)
        self.toolbar.addWidget(add_btn)

        import_btn = PrimaryPushButton("导入知识库")
        import_btn.clicked.connect(self.import_knowledge)
        import_btn.setFixedWidth(self.BUTTON_WIDTH)
        import_btn.setFixedHeight(self.BUTTON_HEIGHT)
        self.toolbar.addWidget(import_btn)

        refresh_btn = PushButton("刷新")
        refresh_btn.clicked.connect(self.refresh_data)
        refresh_btn.setFixedWidth(self.BUTTON_WIDTH)
        refresh_btn.setFixedHeight(self.BUTTON_HEIGHT)
        self.toolbar.addWidget(refresh_btn)

        self.toolbar.addStretch(1)

        self.status_label = QLabel(f"共 {len(self.docs)} 条记录")
        self.status_label.setStyleSheet("color: #666; font-size: 12px;")
        self.toolbar.addWidget(self.status_label)

        self.mainLayout.addLayout(self.toolbar)

        # 添加加载指示器容器（初始隐藏）
        from PyQt6.QtCore import QTimer
        self.loading_container = QWidget()
        self.loading_container.setFixedHeight(40)
        self.loading_container.setVisible(False)

        loading_layout = QVBoxLayout(self.loading_container)
        loading_layout.setContentsMargins(16, 8, 16, 8)
        loading_layout.setSpacing(8)

        # 加载文字提示容器（文字 + 图标）
        loading_text_widget = QWidget()
        loading_text_layout = QHBoxLayout(loading_text_widget)
        loading_text_layout.setContentsMargins(0, 0, 0, 0)
        loading_text_layout.setSpacing(12)

        # 旋转图标（使用圆形点阵）
        self.loading_icon = QLabel("⠋")
        self.loading_icon.setStyleSheet("""
            QLabel {
                color: #0078d4;
                font-size: 24px;
                font-weight: normal;
            }
        """)
        loading_text_layout.addWidget(self.loading_icon, alignment=Qt.AlignmentFlag.AlignCenter)

        # 加载文字
        self.loading_text = QLabel("正在导入")
        self.loading_text.setStyleSheet("""
            QLabel {
                color: #0078d4;
                font-size: 14px;
                font-weight: bold;
            }
        """)
        loading_text_layout.addWidget(self.loading_text, alignment=Qt.AlignmentFlag.AlignCenter)

        # 动态省略号
        self.loading_dots = QLabel("...")
        self.loading_dots.setStyleSheet("""
            QLabel {
                color: #0078d4;
                font-size: 14px;
                font-weight: bold;
            }
        """)
        loading_text_layout.addWidget(self.loading_dots, alignment=Qt.AlignmentFlag.AlignCenter)

        loading_text_layout.addStretch(1)
        loading_layout.addWidget(loading_text_widget)

        self.mainLayout.addWidget(self.loading_container)

        # 动画定时器（用于省略号和图标动画）
        self._loading_animation_timer = QTimer()
        self._loading_animation_timer.timeout.connect(self._update_loading_animation)
        self._loading_dots_state = 0
        self._loading_icon_state = 0

        # 提示语
        tip_label = QLabel("💡 提示：导入或添加知识后需重启应用才可生效哦")
        tip_label.setStyleSheet("""
            QLabel {
                background-color: #fff3cd;
                border: 1px solid #ffeaa7;
                border-radius: 4px;
                padding: 8px 12px;
                color: #856404;
                font-size: 13px;
            }
        """)
        tip_label.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.mainLayout.addWidget(tip_label)

        # 主内容滚动区域
        self.scroll_area = QScrollArea(self)
        self.scroll_area.setWidgetResizable(True)
        self.scroll_area.setFrameShape(QFrame.Shape.NoFrame)

        # 内容容器和网格布局
        self.contentWidget = QWidget()
        self.gridLayout = QGridLayout(self.contentWidget)
        self.gridLayout.setAlignment(Qt.AlignmentFlag.AlignTop)
        self.gridLayout.setContentsMargins(16, 16, 16, 16)
        self.gridLayout.setSpacing(self.CARD_SPACING)

        self.scroll_area.setWidget(self.contentWidget)
        self.mainLayout.addWidget(self.scroll_area)

        # 分页控件
        self._init_pagination_ui()

    def _init_pagination_ui(self) -> None:
        """初始化分页控件"""
        from qfluentwidgets import ComboBox, PushButton

        # 分页容器
        pagination_container = QWidget()
        pagination_layout = QHBoxLayout(pagination_container)
        pagination_layout.setContentsMargins(16, 8, 16, 16)
        pagination_layout.setSpacing(12)

        # 上一页按钮
        self.prev_page_btn = PushButton("上一页")
        self.prev_page_btn.setFixedWidth(80)
        self.prev_page_btn.setFixedHeight(32)
        self.prev_page_btn.setEnabled(False)
        self.prev_page_btn.clicked.connect(self._go_to_previous_page)
        pagination_layout.addWidget(self.prev_page_btn)

        # 页码显示
        self.page_label = QLabel("第 1 / 1 页")
        self.page_label.setStyleSheet("color: #666; font-size: 13px; font-weight: bold;")
        self.page_label.setAlignment(Qt.AlignmentFlag.AlignCenter)
        pagination_layout.addWidget(self.page_label)

        # 下一页按钮
        self.next_page_btn = PushButton("下一页")
        self.next_page_btn.setFixedWidth(80)
        self.next_page_btn.setFixedHeight(32)
        self.next_page_btn.setEnabled(False)
        self.next_page_btn.clicked.connect(self._go_to_next_page)
        pagination_layout.addWidget(self.next_page_btn)

        # 每页数量选择
        page_size_label = QLabel("每页:")
        page_size_label.setStyleSheet("color: #666; font-size: 12px;")
        pagination_layout.addWidget(page_size_label)

        self.page_size_combo = ComboBox()
        self.page_size_combo.addItems(["12", "24", "48", "96"])
        self.page_size_combo.setCurrentIndex(0)
        self.page_size_combo.setFixedWidth(70)
        self.page_size_combo.setFixedHeight(32)
        self.page_size_combo.currentIndexChanged.connect(self._on_page_size_changed)
        pagination_layout.addWidget(self.page_size_combo)

        # 显示总记录数
        pagination_layout.addStretch(1)
        total_label = QLabel(f"共 {len(self.docs)} 条记录")
        total_label.setStyleSheet("color: #999; font-size: 12px;")
        pagination_layout.addWidget(total_label)

        self.mainLayout.addWidget(pagination_container)

        # 保存引用
        self.pagination_container = pagination_container
        self.total_label = total_label

    def _ensure_knowledge_manager(self) -> None:
        """按需创建知识库管理器"""
        if self.knowledge_manager is None:
            try:
                # 延迟导入，避免启动时加载 Agno/LanceDB/CulturalManager 等重型模块
                from Agent.CustomerAgent.agent_knowledge import KnowledgeManager
                self.knowledge_manager = KnowledgeManager()
                logger.info("✅ 知识库管理器初始化成功")
            except Exception as e:
                logger.error(f"❌ 知识库管理器初始化失败: {e}")
                self.knowledge_manager = None

    def showEvent(self, event) -> None:
        """窗口显示事件，确保布局正确"""
        super().showEvent(event)
        if event.spontaneous() or not self.isVisible():
            QTimer.singleShot(150, self.populate_cards)

    def _handle_resize_timeout(self) -> None:
        """处理resize防抖超时，重新布局卡片"""
        if self.isVisible() and self._layout_initialized:
            self.populate_cards()

    def resizeEvent(self, event) -> None:
        """窗口大小变化时重新计算布局 - 使用防抖机制"""
        super().resizeEvent(event)

        if self.isVisible() and self._layout_initialized:
            new_size = event.size()
            old_size = event.oldSize()

            if (not old_size.isValid() or
                abs(new_size.width() - old_size.width()) > 30):
                self._resize_timer.stop()
                self._resize_timer.start(self.RESIZE_DEBOUNCE_DELAY)

    def clear_grid_layout(self) -> None:
        """清空网格布局中的所有控件"""
        while self.gridLayout.count():
            item = self.gridLayout.takeAt(0)
            if item is None:
                continue
            w = item.widget()
            if w is not None:
                w.setParent(None)

    def populate_cards(self) -> None:
        """
        填充知识库卡片到网格布局

        根据窗口大小自适应调整列数。
        使用后台线程加载，避免阻塞UI。
        """
        # 如果窗口还没有正确显示，延迟处理
        if not self.isVisible() or self.width() <= 0:
            if not self._layout_initialized:
                QTimer.singleShot(100, self.populate_cards)
            return

        # 清空现有卡片
        self.clear_grid_layout()

        # 获取知识库数据
        try:
            self._ensure_knowledge_manager()
            if self.knowledge_manager is None:
                no_data_label = QLabel("知识库未初始化，打开该页时将自动加载")
                no_data_label.setAlignment(Qt.AlignmentFlag.AlignCenter)
                no_data_label.setStyleSheet("color: #888; font-size: 14px; padding: 40px;")
                self.gridLayout.addWidget(no_data_label, 0, 0)
                self._layout_initialized = True
                return

            # 显示加载指示器
            self._show_loading_indicator()

            # 启动后台加载
            self._load_worker = LoadDataWorker(self.knowledge_manager)
            self._load_worker.finished.connect(self._on_data_loaded)
            self._load_worker.failed.connect(self._on_load_failed)
            self._load_worker.start()

        except Exception as e:
            logger.error(f"❌ 启动数据加载失败: {e}")
            self._hide_loading_indicator()
            return

    def _on_data_loaded(self, docs: list) -> None:
        """数据加载完成回调"""
        try:
            self.docs = docs
            self._hide_loading_indicator()

            # 更新缓存
            self._cached_docs = docs
            self._cache_valid = True

            # 重置到第一页
            self._current_page = 1

            # 渲染第一页
            self._populate_current_page()

            logger.info(f"✅ 成功加载 {len(self.docs)} 条知识库记录")

        except Exception as e:
            logger.error(f"❌ 渲染数据失败: {e}")
            self._hide_loading_indicator()

    def _on_load_failed(self, error: str) -> None:
        """数据加载失败回调"""
        logger.error(f"❌ 数据加载失败: {error}")
        self._hide_loading_indicator()

        no_data_label = QLabel(f"加载失败: {error}\n请刷新页面重试")
        no_data_label.setAlignment(Qt.AlignmentFlag.AlignCenter)
        no_data_label.setStyleSheet("color: #d63031; font-size: 14px; padding: 40px;")
        self.gridLayout.addWidget(no_data_label, 0, 0)
        self._layout_initialized = True

    def _load_knowledge_data(self) -> None:
        """
        加载知识库数据

        从LanceDB或Agno API获取文档数据并转换为SimpleDocument列表。
        """
        if self.knowledge_manager is None:
            return

        try:
            self.docs = []

            # 尝试直接从LanceDB获取数据
            try:
                import lancedb
                if self.knowledge_manager.knowledge.vector_db is None:
                    logger.warning("向量数据库未初始化")
                    return
                db_path = self.knowledge_manager.knowledge.vector_db.uri
                db = lancedb.connect(db_path)
                table = db.open_table("customer_knowledge")

                # 获取所有数据
                df = table.to_pandas()

                # 转换为SimpleDocument列表
                for idx, row in df.iterrows():
                    doc = SimpleDocument.from_lancedb_row(row.to_dict(), int(idx) if isinstance(idx, int) else 0)
                    self.docs.append(doc)

            except Exception as lancedb_err:
                logger.warning(f"从LanceDB直接获取数据失败: {lancedb_err}")

                # 回退到使用Agno的API
                try:
                    results = self.knowledge_manager.search_knowledge("", limit=1000)
                    self.docs = [SimpleDocument.from_agno_doc(doc) for doc in results]
                    logger.info(f"通过搜索API获取到 {len(self.docs)} 条记录")
                except Exception as search_err:
                    logger.error(f"搜索API也失败: {search_err}")
                    self.docs = []

            logger.info(f"✅ 成功加载 {len(self.docs)} 条知识库记录")

        except Exception as e:
            logger.error(f"❌ 获取知识库内容失败: {e}")
            import traceback
            traceback.print_exc()
            self.docs = []

    def add_knowledge(self) -> None:
        """添加知识内容"""
        self._ensure_knowledge_manager()
        if self.knowledge_manager is None:
            self._show_message('error', "错误", "知识库管理器未初始化")
            return

        # 创建并显示添加知识对话框
        dialog = AddKnowledgeDialog(self)

        if dialog.exec() == QDialog.DialogCode.Accepted:
            title, content = dialog.get_data()

            try:
                # 确认对话框
                confirm_box = QMessageBox(
                    QMessageBox.Icon.Question,
                    "确认添加",
                    f"确定要添加知识「{title}」吗？\n\n内容长度：{len(content)} 字符",
                    QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
                    self
                )
                yes_btn = confirm_box.button(QMessageBox.StandardButton.Yes)
                no_btn = confirm_box.button(QMessageBox.StandardButton.No)
                if yes_btn is not None:
                    yes_btn.setText("添加")
                if no_btn is not None:
                    no_btn.setText("取消")

                if confirm_box.exec() == QMessageBox.StandardButton.Yes:
                    # 使用工作线程执行添加
                    self._add_worker = AddKnowledgeWorker(self.knowledge_manager, title, content)
                    self._add_worker.success.connect(self._on_add_success)
                    self._add_worker.failed.connect(self._on_add_failed)
                    self._add_worker.start()

            except Exception as e:
                logger.error(f"添加知识失败: {e}")
                self._show_message('error', "添加失败", f"添加知识时出错: {str(e)}")

    def _on_add_success(self, title: str) -> None:
        """添加成功回调"""
        self._show_message('success', "添加成功", f"知识「{title}」已成功添加")
        # 强制刷新缓存
        self.refresh_data(force_reload=True)

    def _on_add_failed(self, title: str, error: str) -> None:
        """添加失败回调"""
        self._show_message('error', "添加失败", f"添加知识「{title}」失败: {error}")

    def _show_loading_indicator(self, message: str = "正在加载"):
        """
        显示加载指示器（带动画）

        Args:
            message: 加载提示文字（不包含省略号）
        """
        self.loading_container.setVisible(True)

        # 提取文字部分（去除可能的省略号）
        base_message = message.replace("...", "").strip()
        self.loading_text.setText(base_message)
        self.status_label.setText(base_message + "...")

        # 启动动画定时器（每200ms更新一次，动画更流畅）
        self._loading_dots_state = 0
        self._update_loading_animation()  # 立即显示初始状态
        self._loading_animation_timer.start(200)

    def _hide_loading_indicator(self):
        """隐藏加载指示器"""
        self.loading_container.setVisible(False)
        # 停止动画定时器
        self._loading_animation_timer.stop()

    def _update_loading_animation(self):
        """更新加载动画（省略号 + 图标动画）"""
        # 更新省略号动画
        dots_states = ["", ".", "..", "..."]
        self._loading_dots_state = (self._loading_dots_state + 1) % len(dots_states)
        self.loading_dots.setText(dots_states[self._loading_dots_state])

        # 更新图标动画（使用圆形点阵，更流畅的加载效果）
        icon_states = ["⠋", "⠙", "⠹", "⠸", "⠼", "⠴", "⠦", "⠇", "⠏"]
        current_icon = self.loading_icon.text()
        try:
            current_index = icon_states.index(current_icon)
            next_index = (current_index + 1) % len(icon_states)
            self.loading_icon.setText(icon_states[next_index])
        except ValueError:
            self.loading_icon.setText("⠋")

    def import_knowledge(self) -> None:
        """导入知识库文件"""
        self._ensure_knowledge_manager()
        if self.knowledge_manager is None:
            QMessageBox.critical(self, "错误", "知识库管理器未初始化，无法导入。")
            return

        file_path, _ = QFileDialog.getOpenFileName(
            self,
            "选择知识库文件",
            "",
            "CSV文件 (*.csv);;文本文件 (*.txt *.text *.md *.markdown);;PDF 文件 (*.pdf);;JSON 文件 (*.json);;Excel 文件 (*.xlsx *.xls);;Word 文件 (*.doc *.docx);;所有文件 (*.*)"
        )

        if file_path:
            # 显示加载指示器（带动画）
            self._show_loading_indicator("正在导入知识库")

            # 使用工作线程执行导入
            self._import_worker = ImportWorker(self.knowledge_manager, file_path)

            # 连接信号
            self._import_worker.success.connect(self._on_import_success)
            self._import_worker.failed.connect(self._on_import_failed)

            # 启动导入
            self._import_worker.start()

    def _on_import_success(self, count: int) -> None:
        """
        导入成功回调

        Args:
            count: 导入的文档数量
        """
        self._hide_loading_indicator()
        try:
            # 强制刷新缓存
            self.refresh_data(force_reload=True)
        finally:
            QMessageBox.information(self, "成功", f"知识库导入完成！\n成功导入 {count} 条记录")

    def _on_import_failed(self, msg: str) -> None:
        """
        导入失败回调

        Args:
            msg: 错误消息
        """
        self._hide_loading_indicator()
        QMessageBox.critical(self, "错误", f"导入失败：{msg}")

    def refresh_data(self, force_reload: bool = False) -> None:
        """
        刷新数据，确保布局一致性

        Args:
            force_reload: 是否强制重新加载（忽略缓存）
        """
        try:
            # 如果有有效缓存且不是强制刷新，先显示缓存数据
            if self._cached_docs and self._cache_valid and not force_reload:
                self.docs = self._cached_docs
                # 不等待，直接显示缓存
                QTimer.singleShot(0, self._populate_from_cache)
            else:
                # 没有缓存或强制刷新，清空当前显示
                self.clear_grid_layout()

            # 重置布局初始化标志，强制重新计算布局
            self._layout_initialized = False

            # 后台更新数据
            QTimer.singleShot(50, lambda: self._background_refresh(force_reload))

        except Exception as e:
            error_msg = str(e)
            if "Cannot delete" in error_msg or "Access is denied" in error_msg:
                QMessageBox.warning(
                    self, "文件锁定",
                    "知识库文件被其他程序占用，请尝试以下方法：\n\n"
                    "1. 关闭其他可能使用知识库的程序\n"
                    "2. 重启本应用程序\n"
                    "3. 检查是否有杀毒软件在扫描该文件\n\n"
                    "如果问题持续存在，请联系技术支持。"
                )
            else:
                QMessageBox.critical(self, "错误", f"刷新失败：{error_msg}")

    def _populate_from_cache(self) -> None:
        """从缓存数据快速渲染"""
        try:
            if not self.docs:
                return

            # 渲染当前页（使用分页）
            self._populate_current_page()

            # 更新状态标签
            self.status_label.setText(f"共 {len(self.docs)} 条记录（正在更新...）")

        except Exception as e:
            logger.error(f"❌ 渲染缓存数据失败: {e}")

    def _background_refresh(self, force_reload: bool = False) -> None:
        """后台刷新数据"""
        try:
            self._ensure_knowledge_manager()
            if self.knowledge_manager is None:
                return

            # 显示加载指示器（仅在状态栏显示小图标，不显示进度条）
            if not (self._cached_docs and self._cache_valid and not force_reload):
                self._show_loading_indicator()

            # 启动后台加载
            self._load_worker = LoadDataWorker(self.knowledge_manager)
            self._load_worker.finished.connect(self._on_refresh_completed)
            self._load_worker.failed.connect(self._on_refresh_failed)
            self._load_worker.start()

        except Exception as e:
            logger.error(f"❌ 启动后台刷新失败: {e}")
            self._hide_loading_indicator()

    def _on_refresh_completed(self, docs: list) -> None:
        """后台刷新完成回调"""
        try:
            # 更新缓存
            self._cached_docs = docs
            self._cache_valid = True
            self.docs = docs

            # 隐藏加载指示器
            self._hide_loading_indicator()

            # 保持当前页码（如果超出范围则重置）
            if self._current_page > self._total_pages:
                self._current_page = 1

            # 重新渲染当前页
            self._populate_current_page()

            logger.info(f"✅ 后台刷新完成，共 {len(docs)} 条记录")

        except Exception as e:
            logger.error(f"❌ 后台刷新处理失败: {e}")
            self._hide_loading_indicator()

    def _on_refresh_failed(self, error: str) -> None:
        """后台刷新失败回调"""
        logger.error(f"❌ 后台刷新失败: {error}")
        self._hide_loading_indicator()

        # 如果有缓存，保留缓存显示
        if self._cached_docs:
            self._show_message('warning', "更新失败", f"后台更新失败，显示缓存数据\n{error}")
        else:
            # 无缓存，显示错误
            self.clear_grid_layout()
            no_data_label = QLabel(f"刷新失败: {error}\n请重试")
            no_data_label.setAlignment(Qt.AlignmentFlag.AlignCenter)
            no_data_label.setStyleSheet("color: #d63031; font-size: 14px; padding: 40px;")
            self.gridLayout.addWidget(no_data_label, 0, 0)

    def _show_message(
        self,
        level: str,
        title: str,
        content: str,
        duration: int = 3000
    ) -> None:
        """
        统一的消息显示方法

        Args:
            level: 消息级别 ('success', 'error', 'warning', 'info')
            title: 标题
            content: 内容
            duration: 显示时长（毫秒）
        """
        # 使用 getattr 获取 InfoBar 的方法
        info_method = getattr(InfoBar, level)
        info_method(
            title=title,
            content=content,
            orient=InfoBarPosition.TOP,
            duration=duration,
            parent=self
        )

    # ========== 分页功能方法 ==========

    def _update_pagination(self) -> None:
        """更新分页控件状态"""
        # 计算总页数
        total_docs = len(self.docs)
        if total_docs == 0:
            self._total_pages = 1
        else:
            self._total_pages = (total_docs + self._page_size - 1) // self._page_size

        # 确保当前页码有效
        if self._current_page > self._total_pages:
            self._current_page = self._total_pages
        if self._current_page < 1:
            self._current_page = 1

        # 更新页码显示
        self.page_label.setText(f"第 {self._current_page} / {self._total_pages} 页")

        # 更新按钮状态
        self.prev_page_btn.setEnabled(self._current_page > 1)
        self.next_page_btn.setEnabled(self._current_page < self._total_pages)

        # 更新总记录数
        self.total_label.setText(f"共 {total_docs} 条记录")
        self.status_label.setText(f"共 {total_docs} 条记录")

    def _go_to_previous_page(self) -> None:
        """跳转到上一页"""
        if self._current_page > 1:
            self._current_page -= 1
            self._populate_current_page()

    def _go_to_next_page(self) -> None:
        """跳转到下一页"""
        if self._current_page < self._total_pages:
            self._current_page += 1
            self._populate_current_page()

    def _on_page_size_changed(self, index: int) -> None:
        """
        每页数量改变回调

        Args:
            index: 下拉框索引
        """
        page_sizes = [12, 24, 48, 96]
        new_page_size = page_sizes[index]

        if new_page_size != self._page_size:
            self._page_size = new_page_size
            # 重新计算当前页（保持在相同的数据范围内）
            self._current_page = 1
            self._populate_current_page()

    def _get_current_page_docs(self) -> List[SimpleDocument]:
        """
        获取当前页的文档列表

        Returns:
            当前页的文档列表
        """
        start_idx = (self._current_page - 1) * self._page_size
        end_idx = min(start_idx + self._page_size, len(self.docs))

        if start_idx >= len(self.docs):
            return []

        return self.docs[start_idx:end_idx]

    def _populate_current_page(self) -> None:
        """渲染当前页的卡片"""
        try:
            # 清空现有卡片
            self.clear_grid_layout()

            # 更新分页状态
            self._update_pagination()

            # 获取当前页的文档
            current_docs = self._get_current_page_docs()

            # 检查是否有数据
            if not current_docs:
                if len(self.docs) == 0:
                    # 完全没有数据
                    no_data_label = QLabel("暂无知识库数据\n请点击\"导入知识库\"按钮添加数据")
                else:
                    # 当前页没有数据（异常情况）
                    no_data_label = QLabel("当前页没有数据")
                no_data_label.setAlignment(Qt.AlignmentFlag.AlignCenter)
                no_data_label.setStyleSheet("color: #888; font-size: 14px; padding: 40px;")
                self.gridLayout.addWidget(no_data_label, 0, 0)
                self._layout_initialized = True
                return

            # 固定列数布局
            columns = self.DEFAULT_COLUMNS

            # 添加卡片到网格
            for idx, doc in enumerate(current_docs):
                card = KnowledgeCard(self, doc)
                row = idx // columns
                col = idx % columns
                self.gridLayout.addWidget(card, row, col)

            # 设置列等宽拉伸
            for col in range(columns):
                self.gridLayout.setColumnStretch(col, 1)

            # 标记布局已初始化
            self._layout_initialized = True


        except Exception as e:
            logger.error(f"❌ 渲染当前页失败: {e}")
