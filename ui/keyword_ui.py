# 关键词管理界面 - 分组视图

from typing import Optional, List
from PyQt6.QtCore import Qt, pyqtSignal, QEvent
from PyQt6.QtWidgets import (QFrame, QHBoxLayout, QVBoxLayout, QWidget, QLabel,
                             QTableWidgetItem, QHeaderView, QAbstractItemView,
                             QMessageBox, QDialog, QFormLayout, QLineEdit,
                             QSpinBox, QCheckBox, QComboBox, QTextEdit, QListWidget,
                             QListWidgetItem, QSplitter, QMenu)
from PyQt6.QtGui import QFont, QIcon, QPalette, QColor, QAction
from qfluentwidgets import (SubtitleLabel, CaptionLabel, BodyLabel,
                            PrimaryPushButton, PushButton, ToolButton,
                            ScrollArea, FluentIcon as FIF,
                            TableWidget, LineEdit, SpinBox, CheckBox, ComboBox,
                            isDarkTheme, TextEdit)
from database.db_manager import db_manager
from Message.handlers.keyword_handler import KeywordDetectionHandler


class KeywordTestDialog(QDialog):
    """关键词测试对话框"""

    def __init__(self, parent=None):
        super().__init__(parent)
        self.keyword_handler = KeywordDetectionHandler()
        self.setupUI()

    def setupUI(self):
        """设置对话框UI"""
        self.setWindowTitle('关键词测试')
        self.setMinimumSize(500, 400)

        # 设置对话框背景色，适配深色模式
        if isDarkTheme():
            self.setStyleSheet("""
                QDialog {
                    background-color: #202020;
                }
                QLabel {
                    color: #ffffff;
                }
                QLineEdit, QTextEdit {
                    background-color: #333333;
                    color: #ffffff;
                    border: 1px solid #484848;
                    border-radius: 4px;
                    padding: 8px;
                }
                QLineEdit:focus, QTextEdit:focus {
                    border: 1px solid #0078d4;
                }
            """)
        else:
            self.setStyleSheet("""
                QDialog {
                    background-color: #ffffff;
                }
            """)

        layout = QVBoxLayout(self)
        layout.setSpacing(15)
        layout.setContentsMargins(20, 20, 20, 20)

        # 说明文字
        desc_label = BodyLabel("输入测试消息，查看会匹配到哪个关键词：")
        layout.addWidget(desc_label)

        # 测试输入框
        self.test_input = QLineEdit()
        self.test_input.setPlaceholderText("请输入测试消息...")
        self.test_input.returnPressed.connect(self.onTest)
        layout.addWidget(self.test_input)

        # 测试按钮
        btn_layout = QHBoxLayout()
        btn_layout.addStretch()
        self.test_btn = PrimaryPushButton("测试")
        self.test_btn.setIcon(FIF.SEARCH)
        self.test_btn.setFixedSize(100, 35)
        self.test_btn.clicked.connect(self.onTest)
        btn_layout.addWidget(self.test_btn)
        btn_layout.addStretch()
        layout.addLayout(btn_layout)

        # 结果区域
        result_label = BodyLabel("测试结果：")
        layout.addWidget(result_label)

        self.result_text = QTextEdit()
        self.result_text.setReadOnly(True)
        self.result_text.setMinimumHeight(200)
        layout.addWidget(self.result_text)

        # 关闭按钮
        close_btn = PushButton("关闭")
        close_btn.clicked.connect(self.accept)
        btn_layout2 = QHBoxLayout()
        btn_layout2.addStretch()
        btn_layout2.addWidget(close_btn)
        btn_layout2.addStretch()
        layout.addLayout(btn_layout2)

    def onTest(self):
        """执行测试"""
        test_message = self.test_input.text().strip()

        if not test_message:
            self.result_text.setText("请输入测试消息")
            return

        # 使用关键词处理器进行匹配
        matched = self.keyword_handler.match_keyword(test_message)

        if not matched:
            result = f"测试消息：{test_message}\n\n"
            result += "未匹配到任何关键词"
            self.result_text.setText(result)
            return

        # 构建匹配结果
        result = f"测试消息：{test_message}\n\n"
        result += f"匹配成功！\n\n"
        result += f"关键词：{matched.get('keyword', 'N/A')}\n"
        result += f"分组：{matched.get('group_name', 'N/A')}\n"

        # 匹配类型
        match_type_map = {
            'exact': '完全匹配',
            'partial': '部分匹配',
            'regex': '正则匹配',
            'wildcard': '通配符匹配'
        }
        match_type_text = match_type_map.get(matched.get('match_type', 'partial'), '部分匹配')
        result += f"匹配类型：{match_type_text}\n"

        # 优先级
        result += f"优先级：{matched.get('priority', 0)}\n"

        # 回复内容
        reply_content = matched.get('reply_content')
        if reply_content:
            result += f"\n回复内容：\n{reply_content}\n"

        # 转人工
        if matched.get('transfer_to_human', False):
            result += f"\n会转人工客服"
        else:
            result += f"\n不会转人工客服"

        # pass_to_ai
        if matched.get('pass_to_ai', False):
            result += f"\n会传递给AI处理"

        self.result_text.setText(result)


class GroupDialog(QDialog):
    """分组编辑对话框"""

    def __init__(self, parent=None, group_data: Optional[dict] = None):
        super().__init__(parent)
        self.group_data = group_data or {}
        self.setupUI()

    def setupUI(self):
        """设置对话框UI"""
        self.setWindowTitle('编辑分组' if self.group_data else '新增分组')
        self.setMinimumWidth(450)

        # 设置对话框背景色，适配深色模式
        if isDarkTheme():
            self.setStyleSheet("""
                QDialog {
                    background-color: #202020;
                }
                QLabel {
                    color: #ffffff;
                }
                QLineEdit, QTextEdit, QComboBox, QSpinBox {
                    background-color: #333333;
                    color: #ffffff;
                    border: 1px solid #484848;
                    border-radius: 4px;
                    padding: 4px;
                }
                QLineEdit:focus, QTextEdit:focus, QComboBox:focus, QSpinBox:focus {
                    border: 1px solid #0078d4;
                }
                QCheckBox {
                    color: #ffffff;
                }
            """)
        else:
            self.setStyleSheet("""
                QDialog {
                    background-color: #ffffff;
                }
            """)

        layout = QFormLayout(self)
        layout.setSpacing(12)
        layout.setContentsMargins(20, 20, 20, 20)

        # 分组名称
        self.name_edit = QLineEdit()
        self.name_edit.setText(self.group_data.get('group_name', ''))
        layout.addRow('分组名称:', self.name_edit)

        # 回复内容
        self.reply_edit = TextEdit()
        self.reply_edit.setPlaceholderText('输入回复内容（可选）')
        self.reply_edit.setMaximumHeight(100)
        self.reply_edit.setText(self.group_data.get('reply', '') or '')
        layout.addRow('回复内容:', self.reply_edit)

        # 是否转人工
        self.transfer_check = QCheckBox('匹配后转人工客服')
        self.transfer_check.setChecked(self.group_data.get('is_transfer', False))
        layout.addRow('', self.transfer_check)

        # 是否传递给AI
        self.pass_to_ai_check = QCheckBox('发送回复后传递给AI处理')
        self.pass_to_ai_check.setChecked(self.group_data.get('pass_to_ai', False))
        layout.addRow('', self.pass_to_ai_check)

        # 优先级
        self.priority_spin = QSpinBox()
        self.priority_spin.setRange(0, 100)
        self.priority_spin.setValue(self.group_data.get('priority', 0))
        layout.addRow('优先级:', self.priority_spin)

        # 按钮
        button_layout = QHBoxLayout()
        self.ok_btn = PrimaryPushButton('确定')
        self.cancel_btn = PushButton('取消')
        self.ok_btn.clicked.connect(self.accept)
        self.cancel_btn.clicked.connect(self.reject)
        button_layout.addStretch()
        button_layout.addWidget(self.ok_btn)
        button_layout.addWidget(self.cancel_btn)
        layout.addRow(button_layout)

    def get_data(self) -> dict:
        """获取对话框数据"""
        return {
            'group_name': self.name_edit.text().strip(),
            'reply': self.reply_edit.toPlainText().strip() or None,
            'is_transfer': self.transfer_check.isChecked(),
            'pass_to_ai': self.pass_to_ai_check.isChecked(),
            'priority': self.priority_spin.value()
        }


class KeywordDialog(QDialog):
    """关键词编辑对话框（简化版）"""

    def __init__(self, parent=None, keyword_data: Optional[dict] = None, group_id: int = 0):
        super().__init__(parent)
        self.keyword_data = keyword_data or {}
        self.group_id = group_id
        self.setupUI()

    def setupUI(self):
        """设置对话框UI"""
        self.setWindowTitle('编辑关键词' if self.keyword_data else '添加关键词')
        self.setMinimumWidth(350)

        # 设置对话框背景色，适配深色模式
        if isDarkTheme():
            self.setStyleSheet("""
                QDialog {
                    background-color: #202020;
                }
                QLabel {
                    color: #ffffff;
                }
                QLineEdit, QComboBox {
                    background-color: #333333;
                    color: #ffffff;
                    border: 1px solid #484848;
                    border-radius: 4px;
                    padding: 4px;
                }
                QLineEdit:focus, QComboBox:focus {
                    border: 1px solid #0078d4;
                }
            """)
        else:
            self.setStyleSheet("""
                QDialog {
                    background-color: #ffffff;
                }
            """)

        layout = QFormLayout(self)
        layout.setSpacing(10)
        layout.setContentsMargins(20, 20, 20, 20)

        # 关键词输入
        self.keyword_edit = QLineEdit()
        self.keyword_edit.setText(self.keyword_data.get('keyword', ''))
        layout.addRow('关键词:', self.keyword_edit)

        # 分组选择
        self.group_combo = QComboBox()
        self.group_combo.addItem('请选择分组', 0)
        try:
            groups = db_manager.get_all_keyword_groups()
            for g in groups:
                self.group_combo.addItem(g['group_name'], g['id'])
        except:
            pass
        # 设置当前分组
        current_group_id = self.keyword_data.get('group_id', self.group_id)
        index = self.group_combo.findData(current_group_id)
        if index >= 0:
            self.group_combo.setCurrentIndex(index)
        layout.addRow('分组:', self.group_combo)

        # 匹配类型选择
        self.match_type_combo = QComboBox()
        self.match_type_combo.addItem('完全匹配', 'exact')
        self.match_type_combo.addItem('部分匹配', 'partial')
        self.match_type_combo.addItem('正则匹配', 'regex')
        self.match_type_combo.addItem('通配符匹配', 'wildcard')

        # 设置当前匹配类型
        current_match_type = self.keyword_data.get('match_type', 'partial')
        index = self.match_type_combo.findData(current_match_type)
        if index >= 0:
            self.match_type_combo.setCurrentIndex(index)

        layout.addRow('匹配类型:', self.match_type_combo)

        # 按钮
        button_layout = QHBoxLayout()
        self.ok_btn = PrimaryPushButton('确定')
        self.cancel_btn = PushButton('取消')
        self.ok_btn.clicked.connect(self.accept)
        self.cancel_btn.clicked.connect(self.reject)
        button_layout.addStretch()
        button_layout.addWidget(self.ok_btn)
        button_layout.addWidget(self.cancel_btn)
        layout.addRow(button_layout)

    def get_data(self) -> dict:
        """获取对话框数据"""
        return {
            'keyword': self.keyword_edit.text().strip(),
            'group_id': self.group_combo.currentData(),
            'match_type': self.match_type_combo.currentData()
        }


class GroupListWidget(QListWidget):
    """分组列表组件"""

    group_selected = pyqtSignal(int, str)  # 分组ID, 分组名称

    def __init__(self, parent: Optional[QWidget] = None):
        super().__init__(parent)
        self.setupUI()
        self._update_style()

    def setupUI(self):
        """设置列表"""
        self.setMinimumWidth(180)
        self.setMaximumWidth(280)
        self.setContextMenuPolicy(Qt.ContextMenuPolicy.CustomContextMenu)
        self.customContextMenuRequested.connect(self.showContextMenu)
        self.itemClicked.connect(self.onItemClicked)
        self.itemDoubleClicked.connect(self.onItemDoubleClicked)

    def changeEvent(self, event):
        """监听主题切换事件"""
        super().changeEvent(event)
        if event.type() == QEvent.Type.PaletteChange:
            self._update_style()

    def _update_style(self):
        """更新样式以适配当前主题"""
        try:
            if isDarkTheme():
                self.setStyleSheet("""
                    QListWidget {
                        background-color: #2b2b2b;
                        border: 1px solid #3d3d3d;
                        border-radius: 4px;
                        color: #ffffff;
                    }
                    QListWidget::item {
                        padding: 8px;
                        border-bottom: 1px solid #3d3d3d;
                    }
                    QListWidget::item:hover {
                        background-color: #3d3d3d;
                    }
                    QListWidget::item:selected {
                        background-color: #0078d4;
                        color: #ffffff;
                    }
                """)
            else:
                self.setStyleSheet("""
                    QListWidget {
                        background-color: #ffffff;
                        border: 1px solid #e0e0e0;
                        border-radius: 4px;
                    }
                    QListWidget::item {
                        padding: 8px;
                        border-bottom: 1px solid #f0f0f0;
                    }
                    QListWidget::item:hover {
                        background-color: #f5f5f5;
                    }
                    QListWidget::item:selected {
                        background-color: #0078d4;
                        color: #ffffff;
                    }
                """)
        except Exception:
            pass

    def addGroup(self, group_data: dict):
        """添加分组到列表"""
        item = QListWidgetItem()
        item.setText(group_data['group_name'])
        item.setData(Qt.ItemDataRole.UserRole, group_data['id'])
        item.setToolTip(f"优先级: {group_data.get('priority', 0)}\n关键词数: {group_data.get('keyword_count', 0)}")
        self.addItem(item)

    def clearList(self):
        """清空列表"""
        self.clear()

    def onItemClicked(self, item: QListWidgetItem):
        """点击分组"""
        group_id = item.data(Qt.ItemDataRole.UserRole)
        group_name = item.text()
        self.group_selected.emit(group_id, group_name)

    def onItemDoubleClicked(self, item: QListWidgetItem):
        """双击编辑分组"""
        group_id = item.data(Qt.ItemDataRole.UserRole)
        # 通过父组件处理编辑
        if self.parent() and hasattr(self.parent(), 'onEditGroup'):
            self.parent().onEditGroup(group_id)

    def showContextMenu(self, position):
        """显示右键菜单"""
        item = self.itemAt(position)
        if not item:
            return

        menu = QMenu(self)
        edit_action = QAction("编辑分组", self)
        delete_action = QAction("删除分组", self)

        group_id = item.data(Qt.ItemDataRole.UserRole)
        edit_action.triggered.connect(lambda: self.onEditGroup(group_id))
        delete_action.triggered.connect(lambda: self.onDeleteGroup(group_id))

        menu.addAction(edit_action)
        menu.addAction(delete_action)
        menu.exec(self.mapToGlobal(position))

    def onEditGroup(self, group_id: int):
        """编辑分组"""
        if self.parent() and hasattr(self.parent(), 'onEditGroup'):
            self.parent().onEditGroup(group_id)

    def onDeleteGroup(self, group_id: int):
        """删除分组"""
        if self.parent() and hasattr(self.parent(), 'onDeleteGroup'):
            self.parent().onDeleteGroup(group_id)


class KeywordTableWidget(TableWidget):
    """关键词表格组件（简化版）"""

    edit_clicked = pyqtSignal(int)
    delete_clicked = pyqtSignal(int)

    def __init__(self, parent: Optional[QWidget] = None):
        super().__init__(parent)
        self.setupTable()

    def setupTable(self):
        """设置表格"""
        # 设置列数和表头
        self.setColumnCount(3)
        self.setHorizontalHeaderLabels(['关键词', '匹配类型', '操作'])

        # 设置表格属性
        self.setAlternatingRowColors(True)
        self.setSelectionBehavior(QAbstractItemView.SelectionBehavior.SelectRows)
        self.setSelectionMode(QAbstractItemView.SelectionMode.SingleSelection)
        vertical_header = self.verticalHeader()
        if vertical_header is not None:
            vertical_header.setVisible(False)

        # 设置列宽
        header = self.horizontalHeader()
        if header is not None:
            header.setSectionResizeMode(0, QHeaderView.ResizeMode.Stretch)
            header.setSectionResizeMode(1, QHeaderView.ResizeMode.ResizeToContents)
            header.setSectionResizeMode(2, QHeaderView.ResizeMode.Fixed)

        self.setColumnWidth(2, 220)

        # 设置行高
        vertical_header = self.verticalHeader()
        if vertical_header is not None:
            vertical_header.setDefaultSectionSize(45)

    def addKeyword(self, keyword_data: dict):
        """添加关键词到表格"""
        row = self.rowCount()
        self.insertRow(row)

        keyword_id = keyword_data.get('id', 0)

        # 关键词
        keyword_item = QTableWidgetItem(keyword_data.get('keyword', ''))
        keyword_item.setFlags(Qt.ItemFlag.ItemIsSelectable | Qt.ItemFlag.ItemIsEnabled)
        keyword_item.setData(Qt.ItemDataRole.UserRole, keyword_id)
        self.setItem(row, 0, keyword_item)

        # 匹配类型
        match_type_map = {
            'exact': '完全匹配',
            'partial': '部分匹配',
            'regex': '正则匹配',
            'wildcard': '通配符匹配'
        }
        match_type_text = match_type_map.get(keyword_data.get('match_type', 'partial'), '部分匹配')
        match_type_item = QTableWidgetItem(match_type_text)
        match_type_item.setFlags(Qt.ItemFlag.ItemIsSelectable | Qt.ItemFlag.ItemIsEnabled)
        self.setItem(row, 1, match_type_item)

        # 操作按钮
        action_widget = QWidget()
        action_layout = QHBoxLayout(action_widget)
        action_layout.setContentsMargins(5, 5, 5, 5)
        action_layout.setSpacing(5)
        action_layout.setAlignment(Qt.AlignmentFlag.AlignCenter)

        # 编辑按钮
        edit_btn = PushButton("编辑")
        edit_btn.setIcon(FIF.EDIT)
        edit_btn.setFixedSize(80, 28)
        edit_btn.clicked.connect(lambda: self.edit_clicked.emit(keyword_id))

        # 删除按钮
        delete_btn = PushButton("删除")
        delete_btn.setIcon(FIF.DELETE)
        delete_btn.setFixedSize(80, 28)
        delete_btn.clicked.connect(lambda: self.delete_clicked.emit(keyword_id))

        action_layout.addWidget(edit_btn)
        action_layout.addWidget(delete_btn)
        self.setCellWidget(row, 2, action_widget)

    def clearTable(self):
        """清空表格"""
        self.setRowCount(0)


class KeywordManagerWidget(QFrame):
    """关键词管理主界面 - 分组视图"""

    def __init__(self, parent: Optional[QWidget] = None):
        super().__init__(parent=parent)
        self.groups_data: List[dict] = []
        self.keywords_data: List[dict] = []
        self.current_group_id: int = 0
        self.setupUI()
        self.loadGroupsFromDB()

    def changeEvent(self, event):
        """监听主题切换事件"""
        super().changeEvent(event)
        if event.type() == QEvent.Type.PaletteChange:
            self._update_label_styles()

    def _update_label_styles(self):
        """更新标签样式以适配当前主题"""
        try:
            if isDarkTheme():
                self.group_stats_label.setStyleSheet("color: #cccccc;")
                self.keyword_stats_label.setStyleSheet("color: #cccccc;")
            else:
                self.group_stats_label.setStyleSheet("")
                self.keyword_stats_label.setStyleSheet("")
        except Exception:
            pass

    def setupUI(self):
        """设置主界面UI"""
        # 主布局
        main_layout = QVBoxLayout(self)
        main_layout.setContentsMargins(20, 20, 20, 20)
        main_layout.setSpacing(15)

        # 标题栏
        header_widget = self.createHeaderWidget()
        main_layout.addWidget(header_widget)

        # 分栏布局
        splitter = QSplitter(Qt.Orientation.Horizontal)

        # 左侧分组列表区域
        left_widget = self.createLeftWidget()
        splitter.addWidget(left_widget)

        # 右侧关键词列表区域
        right_widget = self.createRightWidget()
        splitter.addWidget(right_widget)

        # 设置分栏比例
        splitter.setSizes([220, 580])
        splitter.setStretchFactor(0, 0)
        splitter.setStretchFactor(1, 1)

        main_layout.addWidget(splitter, 1)
        self.setObjectName("关键词管理")

    def createHeaderWidget(self):
        """创建头部区域"""
        header_widget = QWidget()
        header_layout = QHBoxLayout(header_widget)
        header_layout.setContentsMargins(0, 0, 0, 0)
        header_layout.setSpacing(20)

        # 标题
        title_label = SubtitleLabel("关键词管理")
        if isDarkTheme():
            title_label.setStyleSheet("color: #ffffff;")

        # 测试按钮
        self.test_btn = PushButton("测试关键词")
        self.test_btn.setIcon(FIF.SEARCH)
        self.test_btn.setFixedSize(120, 35)
        self.test_btn.clicked.connect(self.onTestKeywords)

        header_layout.addWidget(title_label)
        header_layout.addStretch()
        header_layout.addWidget(self.test_btn)

        return header_widget

    def createLeftWidget(self):
        """创建左侧分组列表区域"""
        left_widget = QWidget()
        left_layout = QVBoxLayout(left_widget)
        left_layout.setContentsMargins(0, 0, 0, 0)
        left_layout.setSpacing(10)

        # 分组列表头部
        left_header = QWidget()
        left_header_layout = QHBoxLayout(left_header)
        left_header_layout.setContentsMargins(0, 0, 0, 0)

        group_title = BodyLabel("分组列表")
        if isDarkTheme():
            group_title.setStyleSheet("color: #ffffff;")

        # 新增分组按钮
        self.add_group_btn = ToolButton(FIF.ADD)
        self.add_group_btn.setFixedSize(32, 32)
        self.add_group_btn.setToolTip("新增分组")
        self.add_group_btn.clicked.connect(self.onAddGroup)

        left_header_layout.addWidget(group_title)
        left_header_layout.addStretch()
        left_header_layout.addWidget(self.add_group_btn)

        left_layout.addWidget(left_header)

        # 分组列表
        self.group_list = GroupListWidget(self)
        self.group_list.group_selected.connect(self.onGroupSelected)
        left_layout.addWidget(self.group_list, 1)

        # 分组统计
        self.group_stats_label = CaptionLabel("共 0 个分组")
        if isDarkTheme():
            self.group_stats_label.setStyleSheet("color: #cccccc;")
        left_layout.addWidget(self.group_stats_label)

        return left_widget

    def createRightWidget(self):
        """创建右侧关键词列表区域"""
        right_widget = QWidget()
        right_layout = QVBoxLayout(right_widget)
        right_layout.setContentsMargins(0, 0, 0, 0)
        right_layout.setSpacing(10)

        # 右侧头部
        right_header = QWidget()
        right_header_layout = QHBoxLayout(right_header)
        right_header_layout.setContentsMargins(0, 0, 0, 0)

        # 当前分组名称
        self.current_group_label = BodyLabel("请选择一个分组")
        if isDarkTheme():
            self.current_group_label.setStyleSheet("color: #ffffff;")

        # 编辑分组按钮
        self.edit_group_btn = PushButton("编辑分组")
        self.edit_group_btn.setIcon(FIF.EDIT)
        self.edit_group_btn.setFixedSize(100, 32)
        self.edit_group_btn.clicked.connect(self.onEditCurrentGroup)
        self.edit_group_btn.setEnabled(False)

        # 添加关键词按钮
        self.add_keyword_btn = PrimaryPushButton("添加关键词")
        self.add_keyword_btn.setIcon(FIF.ADD)
        self.add_keyword_btn.setFixedSize(120, 32)
        self.add_keyword_btn.clicked.connect(self.onAddKeyword)
        self.add_keyword_btn.setEnabled(False)

        right_header_layout.addWidget(self.current_group_label)
        right_header_layout.addStretch()
        right_header_layout.addWidget(self.edit_group_btn)
        right_header_layout.addWidget(self.add_keyword_btn)

        right_layout.addWidget(right_header)

        # 分组信息展示
        self.group_info_label = CaptionLabel("")
        if isDarkTheme():
            self.group_info_label.setStyleSheet("color: #999999;")
        right_layout.addWidget(self.group_info_label)

        # 关键词表格
        self.table_widget = KeywordTableWidget()
        self.table_widget.edit_clicked.connect(self.onEditKeyword)
        self.table_widget.delete_clicked.connect(self.onDeleteKeyword)
        right_layout.addWidget(self.table_widget, 1)

        # 关键词统计
        self.keyword_stats_label = CaptionLabel("共 0 个关键词")
        if isDarkTheme():
            self.keyword_stats_label.setStyleSheet("color: #cccccc;")
        right_layout.addWidget(self.keyword_stats_label)

        return right_widget

    # ========== 数据加载 ==========

    def loadGroupsFromDB(self):
        """从数据库加载分组数据"""
        try:
            self.groups_data = db_manager.get_all_keyword_groups()

            # 如果数据库为空，初始化示例数据
            if not self.groups_data:
                self.initializeSampleData()
                self.groups_data = db_manager.get_all_keyword_groups()

            self.refreshGroupList()
        except Exception as e:
            print(f"加载分组失败: {e}")

    def initializeSampleData(self):
        """初始化示例分组和关键词"""
        sample_groups = [
            {"group_name": "转人工", "is_transfer": True, "priority": 10},
            {"group_name": "好评", "reply": "感谢您的好评！祝您生活愉快~", "priority": 5},
            {"group_name": "订单问题", "is_transfer": True, "priority": 8},
            {"group_name": "售后", "is_transfer": True, "priority": 9},
            {"group_name": "投诉", "is_transfer": True, "priority": 10},
            {"group_name": "发票", "is_transfer": True, "priority": 6},
        ]

        sample_keywords = {
            "转人工": ["转人工", "人工客服", "真人", "客服", "人工", "工单", "转售后客服", "转售后"],
            "好评": ["好评"],
            "订单问题": ["取消订单", "改地址", "取消", "备注"],
            "售后": ["返现", "过敏", "退款", "没有效果", "烂"],
            "投诉": ["骗人", "投诉", "纠纷"],
            "发票": ["开发票", "开票"],
        }

        # 创建分组
        group_name_to_id = {}
        for g in sample_groups:
            gid = db_manager.add_keyword_group(
                group_name=g["group_name"],
                reply=g.get("reply"),
                is_transfer=g.get("is_transfer", False),
                priority=g.get("priority", 0)
            )
            if gid:
                group_name_to_id[g["group_name"]] = gid

        # 创建关键词
        for group_name, keywords in sample_keywords.items():
            gid = group_name_to_id.get(group_name)
            if gid:
                for kw in keywords:
                    db_manager.add_keyword(keyword=kw, group_id=gid, match_type="partial")

    def refreshGroupList(self):
        """刷新分组列表"""
        self.group_list.clearList()
        for group_data in self.groups_data:
            self.group_list.addGroup(group_data)
        self.updateGroupStats()

    def updateGroupStats(self):
        """更新分组统计"""
        total = len(self.groups_data)
        self.group_stats_label.setText(f"共 {total} 个分组")

    def onGroupSelected(self, group_id: int, group_name: str):
        """选择分组"""
        self.current_group_id = group_id
        self.current_group_label.setText(group_name)
        self.edit_group_btn.setEnabled(True)
        self.add_keyword_btn.setEnabled(True)

        # 更新分组信息
        group_data = None
        for g in self.groups_data:
            if g['id'] == group_id:
                group_data = g
                break

        if group_data:
            info_parts = []
            if group_data.get('is_transfer'):
                info_parts.append("转人工")
            if group_data.get('pass_to_ai'):
                info_parts.append("pass_to_ai")
            if group_data.get('reply'):
                info_parts.append(f"回复: {group_data['reply'][:20]}...")
            if group_data.get('priority'):
                info_parts.append(f"优先级: {group_data['priority']}")
            self.group_info_label.setText(" | ".join(info_parts) if info_parts else "")
        else:
            self.group_info_label.setText("")

        # 加载该分组的关键词
        self.loadKeywordsByGroup(group_id)

    def loadKeywordsByGroup(self, group_id: int):
        """加载指定分组的关键词"""
        try:
            self.keywords_data = db_manager.get_keywords_by_group(group_id)
            self.refreshKeywordList()
        except Exception as e:
            print(f"加载关键词失败: {e}")
            self.keywords_data = []
            self.refreshKeywordList()

    def refreshKeywordList(self):
        """刷新关键词列表"""
        self.table_widget.clearTable()
        for keyword_data in self.keywords_data:
            self.table_widget.addKeyword(keyword_data)
        self.updateKeywordStats()

    def updateKeywordStats(self):
        """更新关键词统计"""
        total = len(self.keywords_data)
        self.keyword_stats_label.setText(f"共 {total} 个关键词")

    # ========== 分组操作 ==========

    def onAddGroup(self):
        """新增分组"""
        dialog = GroupDialog(self)
        if dialog.exec():
            data = dialog.get_data()
            if not data['group_name']:
                QMessageBox.warning(self, '失败', '分组名称不能为空！')
                return

            group_id = db_manager.add_keyword_group(**data)
            if group_id:
                self.loadGroupsFromDB()
                # 选中新分组
                for i in range(self.group_list.count()):
                    item = self.group_list.item(i)
                    if item.data(Qt.ItemDataRole.UserRole) == group_id:
                        self.group_list.setCurrentItem(item)
                        self.onGroupSelected(group_id, data['group_name'])
                        break
                QMessageBox.information(self, '成功', f'分组 "{data["group_name"]}" 添加成功！')
            else:
                QMessageBox.warning(self, '失败', '分组添加失败，可能已存在！')

    def onEditGroup(self, group_id: int):
        """编辑分组"""
        group_data = None
        for g in self.groups_data:
            if g['id'] == group_id:
                group_data = g
                break

        if not group_data:
            QMessageBox.warning(self, '错误', '找不到分组数据！')
            return

        dialog = GroupDialog(self, group_data)
        if dialog.exec():
            data = dialog.get_data()
            if not data['group_name']:
                QMessageBox.warning(self, '失败', '分组名称不能为空！')
                return

            if db_manager.update_keyword_group(group_id, **data):
                self.loadGroupsFromDB()
                # 如果当前正在显示这个分组，刷新显示
                if self.current_group_id == group_id:
                    self.onGroupSelected(group_id, data['group_name'])
                QMessageBox.information(self, '成功', '分组修改成功！')
            else:
                QMessageBox.warning(self, '失败', '分组修改失败！')

    def onEditCurrentGroup(self):
        """编辑当前选中的分组"""
        if self.current_group_id:
            self.onEditGroup(self.current_group_id)

    def onDeleteGroup(self, group_id: int):
        """删除分组"""
        group_data = None
        for g in self.groups_data:
            if g['id'] == group_id:
                group_data = g
                break

        if not group_data:
            return

        # 确认删除
        reply = QMessageBox.question(
            self, '确认删除',
            f'确定要删除分组 "{group_data["group_name"]}" 吗？\n该分组下的所有关键词也会被删除！',
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
            QMessageBox.StandardButton.No
        )

        if reply == QMessageBox.StandardButton.Yes:
            if db_manager.delete_keyword_group(group_id):
                self.loadGroupsFromDB()
                # 清空右侧
                if self.current_group_id == group_id:
                    self.current_group_id = 0
                    self.current_group_label.setText("请选择一个分组")
                    self.edit_group_btn.setEnabled(False)
                    self.add_keyword_btn.setEnabled(False)
                    self.group_info_label.setText("")
                    self.keywords_data = []
                    self.refreshKeywordList()
                QMessageBox.information(self, '成功', f'分组 "{group_data["group_name"]}" 删除成功！')
            else:
                QMessageBox.warning(self, '失败', '分组删除失败！')

    # ========== 关键词操作 ==========

    def onAddKeyword(self):
        """添加关键词"""
        if not self.current_group_id:
            QMessageBox.warning(self, '提示', '请先选择一个分组！')
            return

        dialog = KeywordDialog(self, group_id=self.current_group_id)
        if dialog.exec():
            data = dialog.get_data()
            if not data['keyword']:
                QMessageBox.warning(self, '失败', '关键词不能为空！')
                return
            if not data['group_id']:
                QMessageBox.warning(self, '失败', '请选择一个分组！')
                return

            if db_manager.add_keyword(**data):
                self.loadKeywordsByGroup(self.current_group_id)
                QMessageBox.information(self, '成功', f'关键词 "{data["keyword"]}" 添加成功！')
            else:
                QMessageBox.warning(self, '失败', f'关键词 "{data["keyword"]}" 添加失败，可能已存在！')

    def onEditKeyword(self, keyword_id: int):
        """编辑关键词"""
        keyword_data = None
        for kw in self.keywords_data:
            if kw.get('id') == keyword_id:
                keyword_data = kw
                break

        if not keyword_data:
            QMessageBox.warning(self, '错误', '找不到关键词数据！')
            return

        dialog = KeywordDialog(self, keyword_data, self.current_group_id)
        if dialog.exec():
            data = dialog.get_data()
            if not data['keyword']:
                QMessageBox.warning(self, '失败', '关键词不能为空！')
                return

            if db_manager.update_keyword(
                keyword_id=keyword_id,
                new_keyword=data['keyword'],
                group_id=data['group_id'],
                match_type=data['match_type']
            ):
                # 刷新当前分组的关键词列表
                self.loadKeywordsByGroup(self.current_group_id)
                QMessageBox.information(self, '成功', '关键词修改成功！')
            else:
                QMessageBox.warning(self, '失败', '关键词修改失败！')

    def onDeleteKeyword(self, keyword_id: int):
        """删除关键词"""
        keyword_data = None
        for kw in self.keywords_data:
            if kw.get('id') == keyword_id:
                keyword_data = kw
                break

        if not keyword_data:
            return

        keyword = keyword_data.get('keyword', '')

        # 确认删除
        reply = QMessageBox.question(
            self, '确认删除',
            f'确定要删除关键词 "{keyword}" 吗？',
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
            QMessageBox.StandardButton.No
        )

        if reply == QMessageBox.StandardButton.Yes:
            if db_manager.delete_keyword(keyword_id):
                self.keywords_data = [k for k in self.keywords_data if k.get('id') != keyword_id]
                self.refreshKeywordList()
                QMessageBox.information(self, '成功', f'关键词 "{keyword}" 删除成功！')
            else:
                QMessageBox.warning(self, '失败', f'删除关键词 "{keyword}" 失败！')

    def onTestKeywords(self):
        """测试关键词"""
        dialog = KeywordTestDialog(self)
        dialog.exec()
