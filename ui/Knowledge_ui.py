"""
知识库管理UI模块

提供知识库数据展示、添加、导入和删除功能。
"""

from __future__ import annotations
import asyncio
import os
from datetime import datetime
from typing import TYPE_CHECKING, Optional, List
from PyQt6.QtWidgets import (
    QWidget, QVBoxLayout, QHBoxLayout, QLabel,
    QScrollArea, QFrame, QGridLayout, QFileDialog, QDialog,
    QSizePolicy
)
from PyQt6.QtCore import Qt, QTimer, QThread, pyqtSignal, QEvent
from qfluentwidgets import (
    FluentIcon, PrimaryPushButton, PushButton,
    InfoBar, InfoBarPosition, MessageBox, SearchLineEdit, isDarkTheme,
    MessageBoxBase, SubtitleLabel, BodyLabel, RadioButton
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
        self.setObjectName("ImportWorker")

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
        self.setObjectName("AddKnowledgeWorker")

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
        self.setObjectName("DeleteWorker")

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
        self.setObjectName("LoadDataWorker")

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


class ExportFormatDialog(QDialog):
    """导出格式选择对话框"""

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setWindowTitle("选择导出格式")
        self.setFixedWidth(420)

        from qfluentwidgets import (
            SubtitleLabel, BodyLabel, RadioButton,
            PrimaryPushButton, PushButton
        )

        layout = QVBoxLayout(self)
        layout.setSpacing(16)
        layout.setContentsMargins(24, 24, 24, 24)

        # 标题
        title_label = SubtitleLabel("选择导出格式")
        layout.addWidget(title_label)

        # 说明
        desc = BodyLabel("请选择知识库导出格式：")
        layout.addWidget(desc)

        # CSV 选项
        self.csv_radio = RadioButton("CSV格式（推荐，可直接重新导入）")
        self.csv_radio.setChecked(True)
        layout.addWidget(self.csv_radio)

        csv_desc = BodyLabel(
            "  · 格式与导入系统完全兼容，导出后可直接通过\"导入知识库\"重新加载\n"
            "  · 包含列：标题、内容、标签、来源\n"
            "  · 适合日常备份和数据迁移"
        )
        csv_desc.setStyleSheet("color: #888; font-size: 12px;")
        layout.addWidget(csv_desc)

        # JSON 选项
        self.json_radio = RadioButton("JSON格式（完整备份）")
        layout.addWidget(self.json_radio)

        json_desc = BodyLabel(
            "  · 包含完整元数据和文档ID\n"
            "  · 适合归档备份，不可直接导入\n"
            "  · 需转换为CSV才能重新导入"
        )
        json_desc.setStyleSheet("color: #888; font-size: 12px;")
        layout.addWidget(json_desc)

        layout.addStretch(1)

        # 按钮
        btn_layout = QHBoxLayout()
        cancel_btn = PushButton("取消")
        cancel_btn.clicked.connect(self.reject)
        ok_btn = PrimaryPushButton("确定")
        ok_btn.clicked.connect(self._on_confirm)
        btn_layout.addStretch(1)
        btn_layout.addWidget(cancel_btn)
        btn_layout.addWidget(ok_btn)
        layout.addLayout(btn_layout)

    def _on_confirm(self):
        self.accept()

    def get_format(self) -> str:
        """获取选择的格式"""
        if self.csv_radio.isChecked():
            return "csv"
        elif self.json_radio.isChecked():
            return "json"
        return "csv"


class ClearConfirmDialog(QDialog):
    """清空知识库确认对话框"""

    def __init__(self, parent=None, doc_count: int = 0):
        super().__init__(parent)
        self.setWindowTitle("确认清空知识库")
        self.setFixedWidth(480)

        from qfluentwidgets import (
            SubtitleLabel, BodyLabel, RadioButton,
            PrimaryPushButton, PushButton
        )

        self._create_backup = True  # 默认创建备份

        layout = QVBoxLayout(self)
        layout.setSpacing(16)
        layout.setContentsMargins(24, 24, 24, 24)

        # 警告标题
        warning_label = SubtitleLabel("⚠️ 危险操作：清空知识库")
        warning_label.setStyleSheet("color: #d32f2f;")
        layout.addWidget(warning_label)

        # 内容说明
        info_text = BodyLabel(
            f"您即将清空知识库中的所有数据（共 {doc_count} 条记录）。\n\n"
            "此操作不可撤销！清空后所有知识条目将被永久删除，"
            "包括向量数据和内容数据库。\n\n"
            "建议在清空前创建备份，以便在需要时恢复数据。"
        )
        info_text.setWordWrap(True)
        layout.addWidget(info_text)

        # 备份选项
        backup_title = BodyLabel("备份选项：")
        backup_title.setStyleSheet("font-weight: bold;")
        layout.addWidget(backup_title)

        self.backup_yes = RadioButton("创建备份（推荐）")
        self.backup_yes.setChecked(True)
        self.backup_yes.toggled.connect(lambda checked: self._on_backup_changed(checked, True))
        layout.addWidget(self.backup_yes)

        backup_yes_desc = BodyLabel(
            "  · 备份向量数据库和内容数据库到指定目录\n"
            "  · 备份后可手动恢复数据"
        )
        backup_yes_desc.setStyleSheet("color: #888; font-size: 12px;")
        layout.addWidget(backup_yes_desc)

        self.backup_no = RadioButton("不创建备份（直接清空）")
        self.backup_no.toggled.connect(lambda checked: self._on_backup_changed(checked, False))
        layout.addWidget(self.backup_no)

        backup_no_desc = BodyLabel(
            "  · 跳过备份，直接清空所有数据\n"
            "  · ⚠️ 清空后无法恢复！"
        )
        backup_no_desc.setStyleSheet("color: #d32f2f; font-size: 12px;")
        layout.addWidget(backup_no_desc)

        layout.addStretch(1)

        # 按钮
        btn_layout = QHBoxLayout()
        cancel_btn = PushButton("取消")
        cancel_btn.clicked.connect(self.reject)
        clear_btn = PrimaryPushButton("确认清空")
        clear_btn.setStyleSheet(
            "QPushButton { background-color: #d32f2f; }"
            "QPushButton:hover { background-color: #b71c1c; }"
        )
        clear_btn.clicked.connect(self._on_confirm)
        btn_layout.addStretch(1)
        btn_layout.addWidget(cancel_btn)
        btn_layout.addWidget(clear_btn)
        layout.addLayout(btn_layout)

    def _on_backup_changed(self, checked: bool, create_backup: bool):
        """备份选项改变"""
        if checked:
            self._create_backup = create_backup

    def _ask_confirm(self, title: str, content: str, yes_text: str = "确认", no_text: str = "取消") -> bool:
        """使用 qfluentwidgets MessageBox 显示确认对话框"""
        mb = MessageBox(title, content, self)
        mb.yesButton.setText(yes_text)
        mb.cancelButton.setText(no_text)
        return mb.exec() == QDialog.DialogCode.Accepted

    def _on_confirm(self):
        """二次确认"""
        if not self._ask_confirm("最终确认", "您确定要清空知识库吗？\n\n此操作不可撤销！"):
            return
        self.accept()

    def get_create_backup(self) -> bool:
        """是否创建备份"""
        return self._create_backup


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
        self._updating_styles = False  # 防止递归更新样式

        # 数据缓存
        self._cached_docs: List[SimpleDocument] = []
        self._cache_valid = False

        # 分页相关
        self._current_page = 1  # 当前页码（从1开始）
        self._page_size = 12  # 每页显示数量
        self._total_pages = 1  # 总页数

        # 搜索相关
        self._search_query = ""  # 当前搜索关键词
        self._filtered_docs: List[SimpleDocument] = []  # 搜索过滤后的文档

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
        
        # 应用主题样式（设置背景色等）
        self._update_label_styles()

        # 延迟加载数据
        QTimer.singleShot(self.INITIAL_LOAD_DELAY, self.populate_cards)

    def changeEvent(self, event):
        """监听主题切换事件"""
        super().changeEvent(event)
        # 防抖：避免 setStyleSheet → PaletteChange → singleShot 乒乓循环
        if event.type() == QEvent.Type.PaletteChange:
            if not getattr(self, '_palette_pending', False):
                self._palette_pending = True
                QTimer.singleShot(100, self._do_palette_update)

    def _do_palette_update(self):
        """实际执行调色板更新"""
        # 先执行更新，再延迟重置标志 —— 避免 setStyleSheet 触发的 PaletteChange
        # 在标志仍为 True 时被忽略，从而打破乒乓循环
        try:
            self._update_label_styles()
        finally:
            QTimer.singleShot(200, self._reset_palette_pending)

    def _reset_palette_pending(self):
        """重置调色板更新标志"""
        self._palette_pending = False

    def _update_label_styles(self):
        """更新所有标签样式以适配当前主题"""
        # 防止递归调用
        if self._updating_styles:
            return
        
        self._updating_styles = True
        try:
            is_dark = isDarkTheme()
            
            # 使用调色板设置背景色（比样式表更可靠）
            from PyQt6.QtGui import QPalette, QColor
            palette = self.palette()
            if is_dark:
                palette.setColor(QPalette.ColorRole.Window, QColor("#1e1e1e"))
                palette.setColor(QPalette.ColorRole.Base, QColor("#1e1e1e"))
            else:
                palette.setColor(QPalette.ColorRole.Window, QColor("#f5f5f5"))
                palette.setColor(QPalette.ColorRole.Base, QColor("#f5f5f5"))
            self.setPalette(palette)
            self.setAutoFillBackground(True)
            
            # 同时设置滚动区域背景色
            if hasattr(self, 'scroll_area'):
                scroll_palette = self.scroll_area.palette()
                if is_dark:
                    scroll_palette.setColor(QPalette.ColorRole.Window, QColor("#1e1e1e"))
                    scroll_palette.setColor(QPalette.ColorRole.Base, QColor("#1e1e1e"))
                else:
                    scroll_palette.setColor(QPalette.ColorRole.Window, QColor("#f5f5f5"))
                    scroll_palette.setColor(QPalette.ColorRole.Base, QColor("#f5f5f5"))
                self.scroll_area.setPalette(scroll_palette)
                self.scroll_area.setAutoFillBackground(True)
            
            # 设置内容控件背景色
            if hasattr(self, 'contentWidget'):
                content_palette = self.contentWidget.palette()
                if is_dark:
                    content_palette.setColor(QPalette.ColorRole.Window, QColor("#1e1e1e"))
                    content_palette.setColor(QPalette.ColorRole.Base, QColor("#1e1e1e"))
                else:
                    content_palette.setColor(QPalette.ColorRole.Window, QColor("#f5f5f5"))
                    content_palette.setColor(QPalette.ColorRole.Base, QColor("#f5f5f5"))
                self.contentWidget.setPalette(content_palette)
                self.contentWidget.setAutoFillBackground(True)
            
            # 更新状态标签文字颜色
            if is_dark:
                self.status_label.setStyleSheet("font-size: 12px; color: #cccccc;")
                self.page_label.setStyleSheet("font-size: 13px; font-weight: bold; color: #ffffff;")
                self.total_label.setStyleSheet("font-size: 12px; color: #cccccc;")
                self.page_size_label.setStyleSheet("font-size: 12px; color: #cccccc;")
                self.loading_icon.setStyleSheet("color: #ffffff; font-size: 24px;")
                self.loading_text.setStyleSheet("color: #ffffff; font-size: 14px; font-weight: bold;")
                self.loading_dots.setStyleSheet("color: #ffffff; font-size: 14px; font-weight: bold;")
                self.tip_label.setStyleSheet("color: #ffffff; border: 1px solid rgba(255, 193, 7, 0.5); border-radius: 4px; padding: 8px 12px; font-size: 13px;")
            else:
                self.status_label.setStyleSheet("font-size: 12px;")
                self.page_label.setStyleSheet("font-size: 13px; font-weight: bold;")
                self.total_label.setStyleSheet("font-size: 12px;")
                self.page_size_label.setStyleSheet("font-size: 12px;")
                self.loading_icon.setStyleSheet("font-size: 24px;")
                self.loading_text.setStyleSheet("font-size: 14px; font-weight: bold;")
                self.loading_dots.setStyleSheet("font-size: 14px; font-weight: bold;")
                self.tip_label.setStyleSheet("border: 1px solid rgba(255, 193, 7, 0.5); border-radius: 4px; padding: 8px 12px; font-size: 13px;")
        finally:
            self._updating_styles = False

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

        export_btn = PushButton("导出")
        export_btn.clicked.connect(self.export_knowledge)
        export_btn.setFixedWidth(80)
        export_btn.setFixedHeight(self.BUTTON_HEIGHT)
        export_btn.setIcon(FluentIcon.SHARE)
        self.toolbar.addWidget(export_btn)

        clear_btn = PushButton("清空")
        clear_btn.clicked.connect(self.clear_knowledge)
        clear_btn.setFixedWidth(80)
        clear_btn.setFixedHeight(self.BUTTON_HEIGHT)
        clear_btn.setIcon(FluentIcon.DELETE)
        self.toolbar.addWidget(clear_btn)

        refresh_btn = PushButton("刷新")
        refresh_btn.clicked.connect(self.refresh_data)
        refresh_btn.setFixedWidth(self.BUTTON_WIDTH)
        refresh_btn.setFixedHeight(self.BUTTON_HEIGHT)
        self.toolbar.addWidget(refresh_btn)

        # 搜索框
        self.search_input = SearchLineEdit()
        self.search_input.setPlaceholderText("搜索知识库...")
        self.search_input.setFixedWidth(250)
        self.search_input.setFixedHeight(self.BUTTON_HEIGHT)
        self.search_input.searchSignal.connect(self._on_search)
        self.search_input.clearSignal.connect(self._on_clear_search)
        # 安装事件过滤器以支持 Enter 键搜索
        self.search_input.installEventFilter(self)
        self.toolbar.addWidget(self.search_input)

        self.toolbar.addStretch(1)

        self.status_label = QLabel(f"共 {len(self.docs)} 条记录")
        if isDarkTheme():
            self.status_label.setStyleSheet("font-size: 12px; color: #cccccc;")
        else:
            self.status_label.setStyleSheet("font-size: 12px;")
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
        if isDarkTheme():
            self.loading_icon.setStyleSheet("""
                QLabel {
                    color: #ffffff;
                    font-size: 24px;
                    font-weight: normal;
                }
            """)
        else:
            self.loading_icon.setStyleSheet("""
                QLabel {
                    
                    font-size: 24px;
                    font-weight: normal;
                }
            """)
        loading_text_layout.addWidget(self.loading_icon, alignment=Qt.AlignmentFlag.AlignCenter)

        # 加载文字
        self.loading_text = QLabel("正在导入")
        if isDarkTheme():
            self.loading_text.setStyleSheet("""
                QLabel {
                    color: #ffffff;
                    font-size: 14px;
                    font-weight: bold;
                }
            """)
        else:
            self.loading_text.setStyleSheet("""
                QLabel {
                    
                    font-size: 14px;
                    font-weight: bold;
                }
            """)
        loading_text_layout.addWidget(self.loading_text, alignment=Qt.AlignmentFlag.AlignCenter)

        # 动态省略号
        self.loading_dots = QLabel("...")
        if isDarkTheme():
            self.loading_dots.setStyleSheet("""
                QLabel {
                    color: #ffffff;
                    font-size: 14px;
                    font-weight: bold;
                }
            """)
        else:
            self.loading_dots.setStyleSheet("""
                QLabel {
                    
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
        self.tip_label = QLabel("💡 提示：导入或添加知识后需重启应用才可生效哦")
        if isDarkTheme():
            self.tip_label.setStyleSheet("""
                QLabel {
                    color: #ffffff;
                    border: 1px solid rgba(255, 193, 7, 0.5);
                    border-radius: 4px;
                    padding: 8px 12px;
                    font-size: 13px;
                }
            """)
        else:
            self.tip_label.setStyleSheet("""
                QLabel {
                    border: 1px solid rgba(255, 193, 7, 0.5);
                    border-radius: 4px;
                    padding: 8px 12px;
                    font-size: 13px;
                }
            """)
        self.tip_label.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.mainLayout.addWidget(self.tip_label)

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
        if isDarkTheme():
            self.page_label.setStyleSheet("font-size: 13px; font-weight: bold; color: #ffffff;")
        else:
            self.page_label.setStyleSheet("font-size: 13px; font-weight: bold;")
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
        self.page_size_label = QLabel("每页:")
        if isDarkTheme():
            self.page_size_label.setStyleSheet("font-size: 12px; color: #cccccc;")
        else:
            self.page_size_label.setStyleSheet("font-size: 12px;")
        pagination_layout.addWidget(self.page_size_label)

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
        if isDarkTheme():
            total_label.setStyleSheet("font-size: 12px; color: #cccccc;")
        else:
            total_label.setStyleSheet("font-size: 12px;")
        pagination_layout.addWidget(total_label)

        self.mainLayout.addWidget(pagination_container)

        # 保存引用
        self.pagination_container = pagination_container
        self.total_label = total_label

    def _ensure_knowledge_manager(self) -> None:
        """按需创建知识库管理器（IPC 代理，lancedb 在独立子进程运行）"""
        if self.knowledge_manager is None:
            try:
                from Agent.CustomerAgent.lancedb_proxy import get_knowledge_manager_proxy
                self.knowledge_manager = get_knowledge_manager_proxy()
                logger.info("✅ 知识库管理器代理初始化成功（lancedb 子进程模式）")
            except Exception as e:
                logger.error(f"❌ 知识库管理器初始化失败: {e}")
                self.knowledge_manager = None

    def _start_km_in_background(self) -> None:
        """后台线程启动 lancedb 子进程，避免阻塞主线程

        子进程启动需要 5-10 秒（加载 lancedb/agno/pandas 等），
        在主线程同步等待会导致 UI 卡顿。改为后台线程启动，
        完成后通过信号回到主线程加载数据。
        """
        from PyQt6.QtCore import QThread, pyqtSignal

        class _KMStarter(QThread):
            done = pyqtSignal(bool)
            def __init__(self, parent):
                super().__init__(parent)
                self._parent = parent
            def run(self):
                try:
                    from Agent.CustomerAgent.lancedb_proxy import get_knowledge_manager_proxy
                    km = get_knowledge_manager_proxy()
                    self._parent.knowledge_manager = km
                    self.done.emit(True)
                except Exception as e:
                    logger.error(f"❌ 后台启动 lancedb 子进程失败: {e}")
                    self.done.emit(False)

        self._km_starter = _KMStarter(self)
        self._km_starter.done.connect(self._on_km_started)
        self._km_starter.start()

    def _on_km_started(self, success: bool) -> None:
        """lancedb 子进程后台启动完成回调"""
        if success and self.knowledge_manager is not None:
            logger.info("✅ lancedb 子进程后台启动完成，开始加载数据")
            # 启动数据加载
            self._load_worker = LoadDataWorker(self.knowledge_manager)
            self._load_worker.finished.connect(self._on_data_loaded)
            self._load_worker.failed.connect(self._on_load_failed)
            self._load_worker.start()
        else:
            logger.error("❌ lancedb 子进程启动失败")
            self._hide_loading_indicator()
            no_data_label = QLabel("知识库初始化失败，请重试")
            no_data_label.setAlignment(Qt.AlignmentFlag.AlignCenter)
            no_data_label.setStyleSheet("font-size: 14px; padding: 40px; color: red;")
            self.gridLayout.addWidget(no_data_label, 0, 0)
            self._layout_initialized = True

    def eventFilter(self, obj, event):
        """事件过滤器：支持搜索框按 Enter 键搜索"""
        if obj is self.search_input and event.type() == QEvent.Type.KeyPress:
            if event.key() in (Qt.Key.Key_Return, Qt.Key.Key_Enter):
                text = self.search_input.text().strip()
                if text:
                    self._on_search(text)
                else:
                    self._on_clear_search()
                return True
        return super().eventFilter(obj, event)

    def cleanup(self):
        """程序退出时清理所有Worker线程"""
        # 清理自身持有的 Worker 线程
        worker_attrs = ['_load_worker', '_add_worker', '_import_worker', '_export_worker', '_clear_worker']
        for attr in worker_attrs:
            worker = getattr(self, attr, None)
            if worker and worker.isRunning():
                worker.requestInterruption()
                worker.wait(3000)
        
        # 清理 KnowledgeCard 的线程（_delete_worker 和 Flyout 中的 _save_worker）
        try:
            from ui.knowledge.widgets import KnowledgeCard
            for i in range(self.gridLayout.count()):
                item = self.gridLayout.itemAt(i)
                if item is None:
                    continue
                widget = item.widget()
                if isinstance(widget, KnowledgeCard) and hasattr(widget, 'cleanup'):
                    widget.cleanup()
        except Exception as e:
            logger.error(f"清理知识库卡片线程失败: {e}")

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
        """清空网格布局中的所有控件

        清理时必须：
        1. 停止淡出动画并清除 QGraphicsOpacityEffect（残留 effect 会导致
           卡片绘制错位/重叠，刷新后消失正是因为重建了无 effect 的新卡片）
        2. deleteLater 销毁被移除的 widget（避免幽灵控件泄漏）
        3. activate() 立即重算布局几何（避免 stale 布局导致卡片错位）
        """
        while self.gridLayout.count():
            item = self.gridLayout.takeAt(0)
            if item is None:
                continue
            w = item.widget()
            if w is None:
                continue
            if isinstance(w, KnowledgeCard):
                w._abort_fade_and_destroy()
            else:
                w.setParent(None)
                w.deleteLater()
        # 强制立即重算布局几何
        self.gridLayout.activate()

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
            # 先展示加载骨架（不阻塞主线程）
            self._show_loading_indicator()

            if self.knowledge_manager is None:
                # 后台启动 lancedb 子进程（避免阻塞主线程 5-10 秒）
                self._start_km_in_background()
                return

            # knowledge_manager 已就绪，启动后台数据加载
            # 防并发：若已有加载任务在运行，跳过本次（避免重复渲染/竞态）
            if getattr(self, '_load_worker', None) and self._load_worker.isRunning():
                logger.debug("数据加载任务已在运行，跳过重复加载")
                return
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
        no_data_label.setStyleSheet("font-size: 14px; padding: 40px;")
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

            # 通过 IPC 调用子进程加载文档（lancedb 在独立子进程中运行）
            try:
                docs = self.knowledge_manager.get_all_documents_for_export()
                if docs:
                    self.docs = docs
                    logger.info(f"通过 IPC 获取到 {len(self.docs)} 条记录")
                else:
                    # 回退到搜索 API
                    results = self.knowledge_manager.search_knowledge("", limit=1000)
                    self.docs = [SimpleDocument.from_agno_doc(doc) for doc in results]
                    logger.info(f"通过搜索API获取到 {len(self.docs)} 条记录")
            except Exception as lancedb_err:
                logger.warning(f"IPC 获取数据失败: {lancedb_err}")
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
                if self._ask_confirm("确认添加", f"确定要添加知识「{title}」吗？\n\n内容长度：{len(content)} 字符", yes_text="添加"):
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
            self._show_info("错误", "知识库管理器未初始化，无法导入。", "error")
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
            self._show_info("成功", f"知识库导入完成！\n成功导入 {count} 条记录", "success")

    def _on_import_failed(self, msg: str) -> None:
        """
        导入失败回调

        Args:
            msg: 错误消息
        """
        self._hide_loading_indicator()
        self._show_info("错误", f"导入失败：{msg}", "error")

    def export_knowledge(self) -> None:
        """导出知识库数据"""
        self._ensure_knowledge_manager()
        if self.knowledge_manager is None:
            self._show_info("错误", "知识库管理器未初始化，无法导出。", "error")
            return

        # 检查是否有数据
        count = self.knowledge_manager.get_content_count()
        if count == 0:
            self._show_info("提示", "知识库为空，没有可导出的数据。", "info")
            return

        # 选择导出格式
        format_dialog = ExportFormatDialog(self)
        if format_dialog.exec() != QDialog.DialogCode.Accepted:
            return

        export_format = format_dialog.get_format()

        # 设置默认文件名
        default_name = f"knowledge_export_{datetime.now().strftime('%Y%m%d_%H%M%S')}.{export_format}"

        if export_format == "csv":
            file_filter = "CSV文件 (*.csv);;所有文件 (*.*)"
        else:
            file_filter = "JSON文件 (*.json);;所有文件 (*.*)"

        file_path, _ = QFileDialog.getSaveFileName(
            self,
            "导出知识库",
            default_name,
            file_filter
        )

        if not file_path:
            return

        # 确保扩展名正确
        if export_format == "csv" and not file_path.lower().endswith('.csv'):
            file_path += '.csv'
        elif export_format == "json" and not file_path.lower().endswith('.json'):
            file_path += '.json'

        # 显示加载指示器
        self._show_loading_indicator("正在导出知识库")

        # 启动导出工作线程
        from .knowledge.export_clear_workers import ExportWorker
        self._export_worker = ExportWorker(self.knowledge_manager, file_path, export_format)
        self._export_worker.success.connect(self._on_export_success)
        self._export_worker.failed.connect(self._on_export_failed)
        self._export_worker.progress.connect(self._on_export_progress)
        self._export_worker.start()

    def _on_export_success(self, file_path: str, count: int) -> None:
        """导出成功回调"""
        self._hide_loading_indicator()
        self._show_info("导出成功",
            f"知识库导出完成！\n\n"
            f"导出文件: {file_path}\n"
            f"导出数量: {count} 条记录\n\n"
            f"提示: CSV格式的导出文件可直接通过\"导入知识库\"功能重新导入。",
            "success")

    def _on_export_failed(self, msg: str) -> None:
        """导出失败回调"""
        self._hide_loading_indicator()
        self._show_info("错误", f"导出失败：{msg}", "error")

    def _on_export_progress(self, msg: str) -> None:
        """导出进度回调"""
        self._show_loading_indicator(msg)

    def clear_knowledge(self) -> None:
        """清空知识库"""
        self._ensure_knowledge_manager()
        if self.knowledge_manager is None:
            self._show_info("错误", "知识库管理器未初始化，无法清空。", "error")
            return

        # 检查是否有数据
        count = self.knowledge_manager.get_content_count()
        if count == 0:
            self._show_info("提示", "知识库已经是空的。", "info")
            return

        # 显示确认对话框
        confirm_dialog = ClearConfirmDialog(self, count)
        if confirm_dialog.exec() != QDialog.DialogCode.Accepted:
            return

        create_backup = confirm_dialog.get_create_backup()

        # 如果需要备份，选择备份目录
        backup_dir = ""
        if create_backup:
            backup_dir = QFileDialog.getExistingDirectory(
                self,
                "选择备份保存目录",
                os.path.expanduser("~")
            )
            if not backup_dir:
                self._show_info("提示", "未选择备份目录，操作已取消。", "warning")
                return

        # 显示加载指示器
        self._show_loading_indicator("正在清空知识库")

        # 启动清空工作线程
        from .knowledge.export_clear_workers import ClearAllWorker
        self._clear_worker = ClearAllWorker(
            self.knowledge_manager,
            create_backup=create_backup,
            backup_dir=backup_dir
        )
        self._clear_worker.success.connect(self._on_clear_success)
        self._clear_worker.failed.connect(self._on_clear_failed)
        self._clear_worker.progress.connect(self._on_clear_progress)
        self._clear_worker.start()

    def _on_clear_success(self, backup_path: str, deleted_count: int) -> None:
        """清空成功回调"""
        self._hide_loading_indicator()

        message = f"知识库已清空！\n\n删除了 {deleted_count} 条记录。"
        if backup_path:
            message += f"\n\n备份已保存到:\n{backup_path}"

        self._show_info("清空成功", message, "success")

        # 强制刷新数据
        self.refresh_data(force_reload=True)

    def _on_clear_failed(self, msg: str) -> None:
        """清空失败回调"""
        self._hide_loading_indicator()
        self._show_info("错误", f"清空失败：{msg}", "error")

    def _on_clear_progress(self, msg: str) -> None:
        """清空进度回调"""
        self._show_loading_indicator(msg)

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
                self._show_info("文件锁定",
                    "知识库文件被其他程序占用，请尝试以下方法：\n\n"
                    "1. 关闭其他可能使用知识库的程序\n"
                    "2. 重启本应用程序\n"
                    "3. 检查是否有杀毒软件在扫描该文件\n\n"
                    "如果问题持续存在，请联系技术支持。", "warning")
            else:
                self._show_info("错误", f"刷新失败：{error_msg}", "error")

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
            # 防并发：若已有加载任务在运行，跳过本次（避免重复渲染/竞态）
            if getattr(self, '_load_worker', None) and self._load_worker.isRunning():
                logger.debug("后台刷新任务已在运行，跳过重复刷新")
                return
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
            no_data_label.setStyleSheet("font-size: 14px; padding: 40px;")
            self.gridLayout.addWidget(no_data_label, 0, 0)

    def _show_info(self, title: str, content: str, level: str = "info") -> None:
        """显示 InfoBar 提示（非阻塞），替代 QMessageBox"""
        duration = 3000 if level in ("warning", "error") else 2000
        if level == "success":
            InfoBar.success(title, content, isClosable=True,
                            position=InfoBarPosition.TOP, duration=duration, parent=self)
        elif level == "warning":
            InfoBar.warning(title, content, isClosable=True,
                            position=InfoBarPosition.TOP, duration=duration, parent=self)
        elif level == "error":
            InfoBar.error(title, content, isClosable=True,
                          position=InfoBarPosition.TOP, duration=duration, parent=self)
        else:
            InfoBar.info(title, content, isClosable=True,
                         position=InfoBarPosition.TOP, duration=duration, parent=self)

    def _ask_confirm(self, title: str, content: str, yes_text: str = "确认", no_text: str = "取消") -> bool:
        """使用 qfluentwidgets MessageBox 显示确认对话框"""
        mb = MessageBox(title, content, self)
        mb.yesButton.setText(yes_text)
        mb.cancelButton.setText(no_text)
        return mb.exec() == QDialog.DialogCode.Accepted

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
                no_data_label.setStyleSheet("font-size: 14px; padding: 40px;")
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

    # ========== 搜索功能方法 ==========

    def _on_search(self, query: str) -> None:
        """
        搜索知识库

        Args:
            query: 搜索关键词
        """
        if not query or not query.strip():
            self._on_clear_search()
            return

        self._search_query = query.strip().lower()

        # 在所有文档中搜索
        self._filtered_docs = [
            doc for doc in self.docs
            if self._match_document(doc, self._search_query)
        ]

        # 重置到第一页
        self._current_page = 1

        # 更新状态标签
        self.status_label.setText(
            f"搜索结果: {len(self._filtered_docs)} / {len(self.docs)} 条记录"
        )

        # 渲染搜索结果
        self._populate_search_results()

        logger.info(f"🔍 搜索 '{query}' 找到 {len(self._filtered_docs)} 条结果")

    def _on_clear_search(self) -> None:
        """清除搜索"""
        self._search_query = ""
        self._filtered_docs = []
        self.search_input.clear()

        # 恢复显示所有文档
        self._current_page = 1
        self.status_label.setText(f"共 {len(self.docs)} 条记录")
        self._populate_current_page()

    def _match_document(self, doc: SimpleDocument, query: str) -> bool:
        """
        匹配文档是否包含搜索关键词

        Args:
            doc: 文档对象
            query: 搜索关键词（已转为小写）

        Returns:
            是否匹配
        """
        # 搜索标题（从 metadata 中获取）
        title = doc.metadata.get('title') or doc.metadata.get('name') or doc.metadata.get('filename') or doc.name
        if title and query in str(title).lower():
            return True

        # 搜索内容
        if doc.content and query in doc.content.lower():
            return True

        # 搜索ID
        if doc.id and query in doc.id.lower():
            return True

        return False


    def _populate_search_results(self) -> None:
        """渲染搜索结果"""
        try:
            # 清空现有卡片
            self.clear_grid_layout()

            # 如果没有搜索结果
            if not self._filtered_docs:
                no_result_label = QLabel(
                    f"未找到包含「{self._search_query}」的知识\n"
                    "请尝试其他关键词"
                )
                no_result_label.setAlignment(Qt.AlignmentFlag.AlignCenter)
                no_result_label.setStyleSheet("font-size: 14px; padding: 40px;")
                self.gridLayout.addWidget(no_result_label, 0, 0)
                self._layout_initialized = True
                return

            # 固定列数布局
            columns = self.DEFAULT_COLUMNS

            # 添加卡片到网格
            for idx, doc in enumerate(self._filtered_docs):
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
            logger.error(f"❌ 渲染搜索结果失败: {e}")
