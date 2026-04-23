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

        # 创建Flyout弹窗，传递文档ID和原始内容
        flyout_view = KnowledgeDetailFlyout(
            title=doc_title,
            content_markdown=detail_markdown,
            doc_id=self.doc.id or "",
            doc_content=self.doc.content or ""
        )

        # 使用Flyout控件显示
        flyout = Flyout.make(
            flyout_view,
            self,
            self.parentWidget(),
            isDeleteOnClose=False
        )

        # 设置引用
        flyout_view.set_flyout(flyout)
        flyout_view.set_card(self)
        self.current_dialog = flyout

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

    显示文档的完整详情内容，支持编辑功能。
    """

    # 类常量
    FLYOUT_WIDTH = 800
    FLYOUT_HEIGHT = 600
    CONTENT_MIN_HEIGHT = 400
    BUTTON_WIDTH = 100
    BUTTON_HEIGHT = 36

    def __init__(self, title: str, content_markdown: str, doc_id: str = "", doc_content: str = ""):
        """
        初始化详情弹窗

        Args:
            title: 文档标题
            content_markdown: Markdown格式的内容（用于显示）
            doc_id: 文档ID（用于编辑保存）
            doc_content: 原始内容（用于编辑）
        """
        super().__init__()
        self._title = title
        self._content_markdown = content_markdown
        self._doc_id = doc_id
        self._doc_content = doc_content
        self._flyout = None
        self._card = None  # 卡片引用，用于保存后刷新
        self._is_editing = False
        self._setup_ui()

    def set_flyout(self, flyout) -> None:
        """设置 Flyout 实例引用"""
        self._flyout = flyout

    def set_card(self, card) -> None:
        """设置卡片引用，用于保存后刷新"""
        self._card = card

    def _setup_ui(self) -> None:
        """初始化UI"""
        self.setFixedSize(self.FLYOUT_WIDTH, self.FLYOUT_HEIGHT)

        # 主布局
        main_layout = QVBoxLayout(self)
        main_layout.setContentsMargins(20, 20, 20, 20)
        main_layout.setSpacing(12)

        # 标题区域（支持编辑模式切换）
        self._title_container = QWidget()
        title_layout = QHBoxLayout(self._title_container)
        title_layout.setContentsMargins(0, 0, 0, 0)

        self._title_label = QLabel(self._title)
        self._title_label.setStyleSheet("font-weight: 600; font-size: 16px; color: #333;")

        self._title_edit = QLineEdit(self._title)
        self._title_edit.setStyleSheet("font-size: 16px; padding: 4px;")
        self._title_edit.setVisible(False)

        title_layout.addWidget(self._title_label)
        title_layout.addWidget(self._title_edit)
        title_layout.addStretch(1)

        main_layout.addWidget(self._title_container)

        # 分隔线
        self._line = QFrame()
        self._line.setFrameShape(QFrame.Shape.HLine)
        self._line.setFrameShadow(QFrame.Shadow.Sunken)
        self._line.setStyleSheet("color: #e0e0e0;")
        main_layout.addWidget(self._line)

        # 内容区域（支持编辑模式切换）
        # 查看模式：HTML 渲染
        self._text_edit = QTextEdit()
        self._text_edit.setHtml(MarkdownConverter.to_html(self._content_markdown))
        self._text_edit.setReadOnly(True)
        self._text_edit.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Expanding)
        self._text_edit.setMinimumHeight(self.CONTENT_MIN_HEIGHT)
        self._text_edit.setStyleSheet("""
            QTextEdit {
                border: none;
                background-color: transparent;
                color: #333;
                font-size: 13px;
                font-family: 'Consolas', 'Monaco', 'Courier New', monospace;
            }
        """)

        # 编辑模式：纯文本编辑
        self._content_edit = QTextEdit()
        self._content_edit.setPlainText(self._doc_content)
        self._content_edit.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Expanding)
        self._content_edit.setMinimumHeight(self.CONTENT_MIN_HEIGHT)
        self._content_edit.setStyleSheet("""
            QTextEdit {
                border: 1px solid #0078d4;
                background-color: #fff;
                color: #333;
                font-size: 13px;
            }
        """)
        self._content_edit.setVisible(False)

        main_layout.addWidget(self._text_edit, 1)
        main_layout.addWidget(self._content_edit, 1)

        # 底部按钮栏
        btn_bar = QHBoxLayout()
        btn_bar.setSpacing(12)
        btn_bar.setContentsMargins(0, 10, 0, 0)

        # 编辑/保存按钮
        self._edit_btn = PushButton("编辑")
        self._edit_btn.setFixedWidth(self.BUTTON_WIDTH)
        self._edit_btn.setFixedHeight(self.BUTTON_HEIGHT)
        self._edit_btn.setIcon(FluentIcon.EDIT)
        self._edit_btn.clicked.connect(self._toggle_edit_mode)

        self._save_btn = PrimaryPushButton("保存")
        self._save_btn.setFixedWidth(self.BUTTON_WIDTH)
        self._save_btn.setFixedHeight(self.BUTTON_HEIGHT)
        self._save_btn.setIcon(FluentIcon.SAVE)
        self._save_btn.clicked.connect(self._save_edit)
        self._save_btn.setVisible(False)

        # 复制/取消按钮
        self._copy_btn = PushButton("复制")
        self._copy_btn.setFixedWidth(self.BUTTON_WIDTH)
        self._copy_btn.setFixedHeight(self.BUTTON_HEIGHT)
        self._copy_btn.setIcon(FluentIcon.COPY)
        self._copy_btn.clicked.connect(self._copy_content)

        self._cancel_btn = PushButton("取消")
        self._cancel_btn.setFixedWidth(self.BUTTON_WIDTH)
        self._cancel_btn.setFixedHeight(self.BUTTON_HEIGHT)
        self._cancel_btn.clicked.connect(self._cancel_edit)
        self._cancel_btn.setVisible(False)

        close_btn = PushButton("关闭")
        close_btn.setFixedWidth(self.BUTTON_WIDTH)
        close_btn.setFixedHeight(self.BUTTON_HEIGHT)
        close_btn.setIcon(FluentIcon.CLOSE)
        close_btn.clicked.connect(self._close_flyout)

        btn_bar.addStretch(1)
        btn_bar.addWidget(self._edit_btn)
        btn_bar.addWidget(self._save_btn)
        btn_bar.addWidget(self._copy_btn)
        btn_bar.addWidget(self._cancel_btn)
        btn_bar.addWidget(close_btn)

        main_layout.addLayout(btn_bar)

    def _toggle_edit_mode(self) -> None:
        """切换编辑模式"""
        self._is_editing = True

        # 切换标题显示
        self._title_label.setVisible(False)
        self._title_edit.setVisible(True)
        self._title_edit.setText(self._title)

        # 切换内容显示
        self._text_edit.setVisible(False)
        self._content_edit.setVisible(True)
        self._content_edit.setPlainText(self._doc_content)

        # 切换按钮显示
        self._edit_btn.setVisible(False)
        self._save_btn.setVisible(True)
        self._copy_btn.setVisible(False)
        self._cancel_btn.setVisible(True)

    def _cancel_edit(self) -> None:
        """取消编辑"""
        self._is_editing = False

        # 恢复标题显示
        self._title_label.setVisible(True)
        self._title_edit.setVisible(False)

        # 恢复内容显示
        self._text_edit.setVisible(True)
        self._content_edit.setVisible(False)

        # 恢复按钮显示
        self._edit_btn.setVisible(True)
        self._save_btn.setVisible(False)
        self._copy_btn.setVisible(True)
        self._cancel_btn.setVisible(False)

    def _save_edit(self) -> None:
        """保存编辑"""
        new_title = self._title_edit.text().strip()
        new_content = self._content_edit.toPlainText().strip()

        if not new_title:
            self._show_message('warning', "提示", "标题不能为空")
            return

        if not new_content:
            self._show_message('warning', "提示", "内容不能为空")
            return

        # 执行保存（异步）
        self._execute_save(new_title, new_content)

    def _execute_save(self, title: str, content: str) -> None:
        """执行保存操作"""
        import asyncio
        from threading import Thread

        def run_save():
            try:
                # 获取知识库管理器
                knowledge_manager = self._get_knowledge_manager()
                if not knowledge_manager:
                    self._show_message('error', "错误", "无法获取知识库管理器")
                    return

                # 创建新的事件循环
                loop = asyncio.new_event_loop()
                asyncio.set_event_loop(loop)

                # 执行更新
                success = loop.run_until_complete(
                    knowledge_manager.update_document_content(self._doc_id, title, content)
                )
                loop.close()

                if success:
                    # 更新本地数据
                    self._title = title
                    self._doc_content = content
                    self._content_markdown = f"# {title}\n\n{content}"

                    # 刷新显示
                    self._title_label.setText(title)
                    self._text_edit.setHtml(MarkdownConverter.to_html(self._content_markdown))

                    # 退出编辑模式
                    self._cancel_edit()

                    # 刷新卡片显示
                    if self._card and hasattr(self._card, 'doc'):
                        self._card.doc.title = title
                        self._card.doc.content = content

                    self._show_message('success', "成功", "文档已更新")
                else:
                    self._show_message('error', "失败", "保存文档失败")

            except Exception as e:
                logger.error(f"保存文档失败: {e}")
                self._show_message('error', "错误", f"保存失败: {str(e)}")

        # 在后台线程执行
        thread = Thread(target=run_save, daemon=True)
        thread.start()

    def _get_knowledge_manager(self):
        """获取知识库管理器"""
        if self._card:
            parent = self._card.parent()
            while parent:
                if hasattr(parent, 'knowledge_manager'):
                    return parent.knowledge_manager
                parent = parent.parent()
        return None

    def _copy_content(self) -> None:
        """复制内容到剪贴板"""
        if self._is_editing:
            self._content_edit.selectAll()
            self._content_edit.copy()
        else:
            self._text_edit.selectAll()
            self._text_edit.copy()
        self._show_message('success', "成功", "内容已复制到剪贴板")

    def _close_flyout(self) -> None:
        """关闭 Flyout 弹窗"""
        if self._flyout:
            Flyout.close(self._flyout)

    def _show_message(self, level: str, title: str, content: str, duration: int = 2000) -> None:
        """显示消息提示"""
        try:
            info_method = getattr(InfoBar, level)
            info_method(
                title=title,
                content=content,
                orient=InfoBarPosition.TOP,
                duration=duration,
                parent=self
            )
        except Exception as e:
            logger.error(f"显示消息失败: {e}")
            Flyout.close(self._flyout)
