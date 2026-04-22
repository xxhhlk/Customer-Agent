"""
知识库UI组件

提供知识库相关的UI组件，包括卡片、对话框和弹窗。
"""

from typing import Optional, Any, Union
from PyQt6.QtCore import Qt, QEvent, QObject, QTimer, QPropertyAnimation, QEasingCurve
from PyQt6.QtWidgets import (
    QWidget, QVBoxLayout, QHBoxLayout, QLabel,
    QSizePolicy, QFrame, QTextEdit, QDialog, QLineEdit, QGraphicsOpacityEffect
)
from PyQt6.QtGui import QCursor
from qfluentwidgets import (
    ElevatedCardWidget, Flyout, FlyoutViewBase, FluentIcon,
    PrimaryPushButton, PushButton, InfoBar, InfoBarPosition, MessageBox
)

from .models import SimpleDocument, DocumentTitleExtractor, MarkdownConverter
from utils.logger_loguru import get_logger

logger = get_logger(__name__)


class KnowledgeCard(ElevatedCardWidget):
    """
    知识库卡片组件

    显示文档的标题、预览内容和操作按钮。
    """

    # 类常量
    CARD_MIN_WIDTH = 280
    CARD_MAX_HEIGHT = 180
    PREVIEW_LENGTH = 150
    ID_DISPLAY_LENGTH = 16
    TOOLTIP_SHORT_LENGTH = 30

    def __init__(self, parent: QWidget, doc: SimpleDocument):
        """
        初始化知识卡片

        Args:
            parent: 父组件
            doc: 文档数据
        """
        super().__init__(parent)
        self.doc = doc
        self.current_dialog: Optional[Union[QDialog, Flyout]] = None
        self._delete_worker = None  # 删除工作线程
        self._setup_ui()

        # 设置样式
        self._setup_style()

        # 安装事件过滤器用于点击弹窗
        self.installEventFilter(self)

    def _setup_ui(self) -> None:
        """初始化UI布局"""
        vbox = QVBoxLayout(self)
        vbox.setContentsMargins(10, 10, 10, 10)
        vbox.setSpacing(6)

        # 获取文档标题
        doc_title = DocumentTitleExtractor.extract(self.doc)

        # 标题
        title = QLabel(doc_title)
        title.setStyleSheet("font-weight: bold; font-size: 14px;")
        title.setMaximumHeight(20)
        vbox.addWidget(title)

        # 文档ID
        if self.doc.id:
            cid = QLabel(f"ID: {self.doc.id[:self.ID_DISPLAY_LENGTH]}...")
            cid.setStyleSheet("color: #999; font-size: 10px;")
            cid.setMaximumHeight(15)
            vbox.addWidget(cid)

        # 内容预览
        if self.doc.content:
            content_preview = self.doc.content.strip()
            if len(content_preview) > self.PREVIEW_LENGTH:
                content_preview = content_preview[:self.PREVIEW_LENGTH] + "..."
            self._content_label = QLabel(content_preview)
            self._content_label.setStyleSheet("color: #666; font-size: 12px;")
            self._content_label.setWordWrap(True)
            self._content_label.setMaximumHeight(36)
            vbox.addWidget(self._content_label)

        # 底部信息栏
        info_layout = QHBoxLayout()

        # 文档长度信息
        if self.doc.content:
            length_label = QLabel(f"{len(self.doc.content)}字")
            length_label.setStyleSheet("color: #999; font-size: 10px;")
            info_layout.addWidget(length_label)

        # 元数据信息
        if self.doc.metadata:
            for key in ['row_number', 'sheet_name', 'section']:
                if key in self.doc.metadata:
                    meta_label = QLabel(f"{self.doc.metadata[key]}")
                    meta_label.setStyleSheet("color: #999; font-size: 10px;")
                    info_layout.addWidget(meta_label)
                    break

        info_layout.addStretch(1)
        info_layout.setContentsMargins(0, 0, 0, 0)
        vbox.addLayout(info_layout)

        # 按钮栏
        btn_bar = QHBoxLayout()
        view_btn = PrimaryPushButton("详情")
        delete_btn = PushButton("删除")
        view_btn.setFixedHeight(30)
        delete_btn.setFixedHeight(30)
        view_btn.setMinimumWidth(60)
        delete_btn.setMinimumWidth(60)
        view_btn.setIcon(FluentIcon.VIEW)
        delete_btn.setIcon(FluentIcon.DELETE)

        # 设置删除按钮样式为红色
        delete_btn.setStyleSheet(delete_btn.styleSheet() + "QPushButton { color: #ff4757; }")

        btn_bar.addWidget(view_btn)
        btn_bar.addWidget(delete_btn)
        btn_bar.setContentsMargins(0, 4, 0, 0)
        vbox.addLayout(btn_bar)

        # 连接信号
        view_btn.clicked.connect(self.show_detail)
        delete_btn.clicked.connect(self.delete_document)

    def _setup_style(self) -> None:
        """设置组件样式"""
        self.setMinimumWidth(self.CARD_MIN_WIDTH)
        self.setMaximumWidth(16777215)
        self.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Fixed)
        self.setMaximumHeight(self.CARD_MAX_HEIGHT)
        self.setContentsMargins(8, 8, 8, 8)
        self.setCursor(QCursor(Qt.CursorShape.PointingHandCursor))

    def eventFilter(self, obj: QObject, event: QEvent) -> bool:
        """事件过滤器，点击卡片显示详情"""
        if obj is self and event.type() == QEvent.Type.MouseButtonPress:
            self.show_detail()
            return True
        return super().eventFilter(obj, event)

    def show_detail(self) -> None:
        """显示详情弹窗"""
        # 防止重复点击
        if self.current_dialog and self.current_dialog.isVisible():
            return

        # 获取标题和Markdown内容
        doc_title = DocumentTitleExtractor.extract(self.doc)
        detail_markdown = MarkdownConverter.doc_to_markdown(doc_title, self.doc)

        # 创建Flyout弹窗
        flyout_view = KnowledgeDetailFlyout(
            title=doc_title,
            content_markdown=detail_markdown
        )

        # 使用Flyout控件显示
        self.current_dialog = Flyout.make(
            flyout_view,
            self,
            self.parentWidget(),
            isDeleteOnClose=False
        )

    def delete_document(self) -> None:
        """
        删除文档 - 优化后的确认对话框

        1. 使用顶层窗口确保对话框在屏幕中央
        2. 用户确认后，立即从UI移除卡片（乐观更新）
        3. 后台异步执行删除操作
        4. 失败时恢复卡片并提示错误
        """
        doc_title = DocumentTitleExtractor.extract(self.doc)

        # 获取顶层窗口，确保对话框在屏幕中央
        top_level_widget = self._get_top_level_widget()

        # 确认对话框
        title = "确认删除"
        content = f"确定要删除文档「{doc_title}」吗？\n\n⚠️ 此操作不可恢复！删除后数据将无法找回。"

        box = MessageBox(title, content, top_level_widget)

        # 设置按钮文本
        box.yesButton.setText("确认删除")
        box.cancelButton.setText("取消")

        # 设置对话框为应用模态，确保在屏幕中央显示
        box.setWindowModality(Qt.WindowModality.ApplicationModal)

        if box.exec():
            try:
                # 获取文档ID
                doc_id = self.doc.id
                if not doc_id:
                    self._show_message(
                        'error',
                        "删除失败",
                        "无法获取文档ID"
                    )
                    return

                # 查找知识库管理器
                parent_widget = self._find_knowledge_ui_parent()
                if not parent_widget:
                    self._show_message(
                        'error',
                        "删除失败",
                        "无法找到知识库管理器"
                    )
                    return

                # 乐观删除：先从UI移除卡片
                self._fade_out_and_remove()

                # 后台执行删除操作
                self._execute_delete_background(parent_widget, doc_id, doc_title)

            except Exception as e:
                self._show_message(
                    'error',
                    "删除失败",
                    f"删除文档时出错: {str(e)}"
                )

    def _get_top_level_widget(self) -> QWidget:
        """
        获取顶层窗口（主窗口）

        确保对话框在屏幕中央显示，而不是跟随卡片位置。

        Returns:
            顶层窗口组件
        """
        widget: QWidget = self
        parent = widget.parent()
        while parent is not None:
            widget = parent
            parent = widget.parent()
        return widget

    def _find_knowledge_ui_parent(self) -> Optional[QWidget]:
        """
        查找包含 knowledge_manager 的父组件

        Returns:
            知识库UI父组件，如果未找到则返回None
        """
        parent_widget = self.parent()
        while parent_widget and not hasattr(parent_widget, 'knowledge_manager'):
            parent_widget = parent_widget.parent()
        return parent_widget  # type: ignore[return-value]

    def _fade_out_and_remove(self) -> None:
        """
        淡出动画并从布局中移除卡片

        使用乐观删除策略，立即从UI移除，提供快速反馈。
        """
        try:
            # 创建透明度效果
            opacity_effect = QGraphicsOpacityEffect(self)
            self.setGraphicsEffect(opacity_effect)

            # 创建淡出动画
            self._fade_animation = QPropertyAnimation(opacity_effect, b"opacity")
            self._fade_animation.setDuration(300)  # 300ms动画
            self._fade_animation.setStartValue(1.0)
            self._fade_animation.setEndValue(0.0)
            self._fade_animation.setEasingCurve(QEasingCurve.Type.InOutQuad)

            # 动画完成后移除卡片
            self._fade_animation.finished.connect(self._remove_from_layout)

            # 启动动画
            self._fade_animation.start()

            # 禁用删除按钮，防止重复点击
            self.setEnabled(False)

        except Exception as e:
            # 如果动画失败，直接移除
            logger.warning(f"淡出动画失败，直接移除: {e}")
            self._remove_from_layout()

    def _remove_from_layout(self) -> None:
        """从布局中移除卡片"""
        try:
            parent_widget = self.parent()
            if parent_widget and hasattr(parent_widget, 'gridLayout'):
                # 从网格布局中移除
                parent_widget.gridLayout.removeWidget(self)  # type: ignore[union-attr]
                self.setParent(None)
                self.deleteLater()
        except Exception as e:
            logger.warning(f"从布局移除失败: {e}")

    def _execute_delete_background(self, parent_ui: QWidget, doc_id: str, doc_title: str) -> None:
        """
        在后台执行删除操作

        Args:
            parent_ui: 知识库UI父组件
            doc_id: 文档ID
            doc_title: 文档标题
        """
        from ui.Knowledge_ui import DeleteWorker

        # 创建删除工作线程
        self._delete_worker = DeleteWorker(
            getattr(parent_ui, 'knowledge_manager'),  # type: ignore[arg-type]
            doc_id,
            doc_title
        )

        # 连接信号
        self._delete_worker.success.connect(
            lambda did, dtitle: self._on_delete_success(parent_ui, did, dtitle)
        )
        self._delete_worker.failed.connect(
            lambda did, dtitle, error: self._on_delete_failed(parent_ui, did, dtitle, error)
        )

        # 启动后台删除
        self._delete_worker.start()

    def _on_delete_success(self, parent_ui: QWidget, doc_id: str, doc_title: str) -> None:
        """
        删除成功回调 - 自动刷新页面

        Args:
            parent_ui: 知识库UI父组件
            doc_id: 文档ID
            doc_title: 文档标题
        """
        try:
            # 从主数据列表中移除（不仅是缓存）
            if hasattr(parent_ui, 'docs') and getattr(parent_ui, 'docs'):
                parent_ui.docs = [doc for doc in getattr(parent_ui, 'docs') if doc.id != doc_id]  # type: ignore[misc]

            # 从缓存中移除（如果存在）
            if hasattr(parent_ui, '_cached_docs'):
                parent_ui._cached_docs = [  # type: ignore[misc]
                    doc for doc in getattr(parent_ui, '_cached_docs') if doc.id != doc_id
                ]

            # 重新计算分页 - 如果当前页空了，跳转到前一页
            if hasattr(parent_ui, '_current_page') and hasattr(parent_ui, '_page_size'):
                parent_ui_docs = getattr(parent_ui, 'docs', [])
                total_docs = len(parent_ui_docs)
                page_size = getattr(parent_ui, '_page_size')
                current_page = getattr(parent_ui, '_current_page')

                # 计算当前页是否还有数据
                start_idx = (current_page - 1) * page_size
                if start_idx >= total_docs and current_page > 1:
                    # 当前页空了，跳转到前一页
                    setattr(parent_ui, '_current_page', current_page - 1)

            # 重新渲染当前页
            if hasattr(parent_ui, '_populate_current_page'):
                getattr(parent_ui, '_populate_current_page')()

            # 显示成功消息 - 使用 parent_ui 作为父组件
            self._show_message(
                'success',
                "删除成功",
                f"已删除文档「{doc_title}」",
                parent=parent_ui
            )

            logger.info(f"✅ 成功删除文档: {doc_title} (ID: {doc_id})")

        except Exception as e:
            logger.error(f"删除成功回调处理失败: {e}")

    def _on_delete_failed(self, parent_ui: QWidget, doc_id: str, doc_title: str, error: str) -> None:
        """
        删除失败回调 - 刷新页面恢复卡片

        Args:
            parent_ui: 知识库UI父组件
            doc_id: 文档ID
            doc_title: 文档标题
            error: 错误消息
        """
        try:
            # 重新渲染当前页（卡片会被恢复显示）
            if hasattr(parent_ui, '_populate_current_page'):
                getattr(parent_ui, '_populate_current_page')()

            # 显示错误消息 - 使用 parent_ui 作为父组件
            self._show_message(
                'error',
                "删除失败",
                f"删除文档「{doc_title}」失败: {error}\n\n数据已恢复显示",
                parent=parent_ui
            )

            logger.error(f"❌ 删除文档失败: {doc_title}, 错误: {error}")

        except Exception as e:
            logger.error(f"删除失败回调处理失败: {e}")

    def _show_message(self, level: str, title: str, content: str, duration: int = 3000, parent: Optional[QWidget] = None) -> None:
        """
        显示消息提示 - 使用 InfoBar 顶部提示条

        Args:
            level: 消息级别 (success, error, warning, info)
            title: 标题
            content: 内容
            duration: 持续时间（毫秒）
            parent: 父组件（如果为None，则自动获取顶层窗口）
        """
        # 如果没有指定父组件，尝试获取顶层窗口
        if parent is None:
            parent = self._get_top_level_widget()

        # 确保父组件有效
        if parent is None:
            logger.warning(f"无法显示消息提示（未找到父组件）: {title} - {content}")
            return

        try:
            info_method = getattr(InfoBar, level)
            info_method(
                title=title,
                content=content,
                orient=InfoBarPosition.TOP,
                duration=duration,
                parent=parent
            )
        except Exception as e:
            logger.error(f"显示消息提示失败: {e}")


class AddKnowledgeDialog(QDialog):
    """
    添加知识对话框

    允许用户输入知识的标题和内容。
    """

    # 类常量
    DIALOG_WIDTH = 600
    DIALOG_HEIGHT = 500
    TITLE_HEIGHT = 35
    CONTENT_MIN_HEIGHT = 350
    BUTTON_WIDTH = 100

    def __init__(self, parent: Optional[QWidget] = None):
        """
        初始化对话框

        Args:
            parent: 父组件
        """
        super().__init__(parent)
        self.setWindowTitle("添加知识")
        self.setFixedSize(self.DIALOG_WIDTH, self.DIALOG_HEIGHT)
        self.setWindowModality(Qt.WindowModality.ApplicationModal)
        self._init_ui()

    def _init_ui(self) -> None:
        """初始化UI"""
        layout = QVBoxLayout(self)
        layout.setSpacing(15)
        layout.setContentsMargins(25, 25, 25, 25)

        # 标题
        title_label = QLabel("知识标题")
        title_label.setStyleSheet("font-size: 14px; font-weight: bold; color: #333;")
        layout.addWidget(title_label)

        self.title_edit = QLineEdit()
        self.title_edit.setPlaceholderText("请输入知识标题...")
        self.title_edit.setFixedHeight(self.TITLE_HEIGHT)
        layout.addWidget(self.title_edit)

        # 内容
        content_label = QLabel("知识内容")
        content_label.setStyleSheet("font-size: 14px; font-weight: bold; color: #333;")
        layout.addWidget(content_label)

        self.content_edit = QTextEdit()
        self.content_edit.setPlaceholderText("请输入知识内容，支持Markdown格式...")
        self.content_edit.setMinimumHeight(self.CONTENT_MIN_HEIGHT)
        layout.addWidget(self.content_edit)

        # 提示信息
        hint_label = QLabel("提示：内容将自动进行分块和向量化处理")
        hint_label.setStyleSheet("color: #999; font-size: 12px;")
        layout.addWidget(hint_label)

        # 按钮栏
        button_layout = QHBoxLayout()
        button_layout.setContentsMargins(0, 10, 0, 0)

        cancel_btn = PushButton("取消")
        cancel_btn.setFixedWidth(self.BUTTON_WIDTH)
        cancel_btn.clicked.connect(self.reject)

        save_btn = PrimaryPushButton("保存")
        save_btn.setFixedWidth(self.BUTTON_WIDTH)
        save_btn.clicked.connect(self._validate_and_accept)

        button_layout.addStretch()
        button_layout.addWidget(cancel_btn)
        button_layout.addWidget(save_btn)

        layout.addLayout(button_layout)

    def _validate_and_accept(self) -> None:
        """验证输入并接受"""
        title = self.title_edit.text().strip()
        content = self.content_edit.toPlainText().strip()

        if not title:
            self._show_message('warning', "提示", "请输入知识标题")
            self.title_edit.setFocus()
            return

        if not content:
            self._show_message('warning', "提示", "请输入知识内容")
            self.content_edit.setFocus()
            return

        self.accept()

    def _show_message(self, level: str, title: str, content: str, duration: int = 2000) -> None:
        """显示消息提示"""
        info_method = getattr(InfoBar, level)
        info_method(
            title=title,
            content=content,
            orient=InfoBarPosition.TOP,
            duration=duration,
            parent=self
        )

    def get_data(self) -> tuple[str, str]:
        """
        获取输入的标题和内容

        Returns:
            (标题, 内容) 元组
        """
        return self.title_edit.text().strip(), self.content_edit.toPlainText().strip()


class KnowledgeDetailFlyout(FlyoutViewBase):
    """
    知识详情弹窗视图

    显示文档的完整详情内容。
    """

    # 类常量
    FLYOUT_WIDTH = 800
    FLYOUT_HEIGHT = 600
    CONTENT_MIN_HEIGHT = 400
    BUTTON_WIDTH_COPY = 120
    BUTTON_WIDTH_CLOSE = 100
    BUTTON_HEIGHT = 36

    def __init__(self, title: str, content_markdown: str):
        """
        初始化详情弹窗

        Args:
            title: 文档标题
            content_markdown: Markdown格式的内容
        """
        super().__init__()
        self._title = title
        self._content_markdown = content_markdown
        self._setup_ui()

    def _setup_ui(self) -> None:
        """初始化UI"""
        # 设置弹窗大小
        self.setFixedSize(self.FLYOUT_WIDTH, self.FLYOUT_HEIGHT)

        # 主布局
        main_layout = QVBoxLayout(self)
        main_layout.setContentsMargins(20, 20, 20, 20)
        main_layout.setSpacing(12)

        # 标题
        title_label = QLabel(self._title)
        title_label.setStyleSheet("font-weight: 600; font-size: 16px; color: #333;")
        main_layout.addWidget(title_label)

        # 分隔线
        line = QFrame()
        line.setFrameShape(QFrame.Shape.HLine)
        line.setFrameShadow(QFrame.Shadow.Sunken)
        line.setStyleSheet("color: #e0e0e0;")
        main_layout.addWidget(line)

        # 可滚动内容
        text_edit = QTextEdit()
        text_edit.setHtml(MarkdownConverter.to_html(self._content_markdown))
        text_edit.setReadOnly(True)
        text_edit.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Expanding)
        text_edit.setMinimumHeight(self.CONTENT_MIN_HEIGHT)
        text_edit.setVerticalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAsNeeded)
        text_edit.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAsNeeded)
        text_edit.setStyleSheet("""
            QTextEdit {
                border: none;
                background-color: transparent;
                color: #333;
                font-size: 13px;
                font-family: 'Consolas', 'Monaco', 'Courier New', monospace;
            }
        """)

        main_layout.addWidget(text_edit, 1)

        # 底部按钮栏
        btn_bar = QHBoxLayout()
        btn_bar.setSpacing(12)
        btn_bar.setContentsMargins(0, 10, 0, 0)

        copy_btn = PrimaryPushButton("复制内容")
        copy_btn.setFixedWidth(self.BUTTON_WIDTH_COPY)
        copy_btn.setFixedHeight(self.BUTTON_HEIGHT)
        copy_btn.setIcon(FluentIcon.COPY)

        close_btn = PushButton("关闭")
        close_btn.setFixedWidth(self.BUTTON_WIDTH_CLOSE)
        close_btn.setFixedHeight(self.BUTTON_HEIGHT)
        close_btn.setIcon(FluentIcon.CLOSE)

        # 连接信号
        copy_btn.clicked.connect(self._copy_content)
        parent_widget = self.parent()
        close_btn.clicked.connect(lambda: Flyout.close(parent_widget) if parent_widget else None)  # type: ignore[arg-type]

        btn_bar.addStretch(1)
        btn_bar.addWidget(copy_btn)
        btn_bar.addWidget(close_btn)

        main_layout.addLayout(btn_bar)

        # 保存引用
        self._text_edit = text_edit

    def _copy_content(self) -> None:
        """复制内容到剪贴板"""
        self._text_edit.selectAll()
        self._text_edit.copy()
