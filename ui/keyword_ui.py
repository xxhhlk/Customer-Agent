# 关键词管理界面

from typing import Optional, List
from PyQt6.QtCore import Qt, pyqtSignal
from PyQt6.QtWidgets import (QFrame, QHBoxLayout, QVBoxLayout, QWidget, QLabel,
                            QTableWidget, QTableWidgetItem, QHeaderView, QAbstractItemView,
                            QInputDialog, QMessageBox, QDialog, QFormLayout, QLineEdit,
                            QSpinBox, QCheckBox, QComboBox, QTextEdit)
from PyQt6.QtGui import QFont, QIcon, QPalette, QColor
from qfluentwidgets import (SubtitleLabel, CaptionLabel, BodyLabel,
                           PrimaryPushButton, PushButton,
                           ScrollArea, FluentIcon as FIF,
                           TableWidget, LineEdit, SpinBox, CheckBox, ComboBox,
                           isDarkTheme)
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
            self.result_text.setText("❌ 请输入测试消息")
            return
        
        # 使用关键词处理器进行匹配
        matched = self.keyword_handler.match_keyword(test_message)
        
        if not matched:
            result = f"📝 测试消息：{test_message}\n\n"
            result += "❌ 未匹配到任何关键词"
            self.result_text.setText(result)
            return
        
        # 构建匹配结果
        result = f"📝 测试消息：{test_message}\n\n"
        result += f"✅ 匹配成功！\n\n"
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
            result += f"\n💬 回复内容：\n{reply_content}\n"
        
        # 转人工
        if matched.get('transfer_to_human', False):
            result += f"\n👤 会转人工客服"
        else:
            result += f"\n👤 不会转人工客服"
        
        # pass_to_ai
        if matched.get('pass_to_ai', False):
            result += f"\n🤖 会传递给AI处理"
        
        self.result_text.setText(result)


class KeywordTableWidget(TableWidget):
    """关键词表格组件"""

    # 定义信号
    edit_clicked = pyqtSignal(int)  # 编辑按钮点击信号，传递关键词ID
    delete_clicked = pyqtSignal(int)  # 删除按钮点击信号，传递关键词ID

    def __init__(self, parent: Optional[QWidget] = None):
        super().__init__(parent)
        self.setupTable()
        
    def setupTable(self):
        """设置表格"""
        # 设置列数和表头
        self.setColumnCount(7)
        self.setHorizontalHeaderLabels(['关键词', '分组', '匹配类型', '回复内容', '转人工', '优先级', '操作'])
        
        # 设置表格属性
        self.setAlternatingRowColors(True)  # 交替行颜色
        self.setSelectionBehavior(QAbstractItemView.SelectionBehavior.SelectRows)  # 选择整行
        self.setSelectionMode(QAbstractItemView.SelectionMode.SingleSelection)  # 单选
        vertical_header = self.verticalHeader()
        if vertical_header is not None:
            vertical_header.setVisible(False)  # 隐藏行号
        
        # 设置列宽
        header = self.horizontalHeader()
        if header is not None:
            header.setSectionResizeMode(0, QHeaderView.ResizeMode.Stretch)  # 关键词列自动拉伸
            header.setSectionResizeMode(1, QHeaderView.ResizeMode.ResizeToContents)  # 分组列
            header.setSectionResizeMode(2, QHeaderView.ResizeMode.ResizeToContents)  # 匹配类型列
            header.setSectionResizeMode(3, QHeaderView.ResizeMode.Stretch)  # 回复内容列自动拉伸
            header.setSectionResizeMode(4, QHeaderView.ResizeMode.ResizeToContents)  # 转人工列
            header.setSectionResizeMode(5, QHeaderView.ResizeMode.ResizeToContents)  # 优先级列
            header.setSectionResizeMode(6, QHeaderView.ResizeMode.Fixed)   # 操作列固定宽度
        
        self.setColumnWidth(6, 250)  # 操作列
        
        # 设置行高
        vertical_header = self.verticalHeader()
        if vertical_header is not None:
            vertical_header.setDefaultSectionSize(50)
        
    def addKeyword(self, keyword_data: dict):
        """添加关键词到表格
        
        Args:
            keyword_data: 包含关键词信息的字典 {
                'id': int,
                'keyword': str,
                'group_name': str,
                'match_type': str,
                'reply_content': str,
                'transfer_to_human': bool,
                'priority': int
            }
        """
        row = self.rowCount()
        self.insertRow(row)
        
        keyword_id = keyword_data.get('id', 0)
        
        # 关键词
        keyword_item = QTableWidgetItem(keyword_data.get('keyword', ''))
        keyword_item.setFlags(Qt.ItemFlag.ItemIsSelectable | Qt.ItemFlag.ItemIsEnabled)
        keyword_item.setData(Qt.ItemDataRole.UserRole, keyword_id)  # 存储ID
        self.setItem(row, 0, keyword_item)
        
        # 分组
        group_item = QTableWidgetItem(keyword_data.get('group_name', 'default'))
        group_item.setFlags(Qt.ItemFlag.ItemIsSelectable | Qt.ItemFlag.ItemIsEnabled)
        self.setItem(row, 1, group_item)
        
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
        self.setItem(row, 2, match_type_item)
        
        # 回复内容
        reply_item = QTableWidgetItem(keyword_data.get('reply_content', '') or '')
        reply_item.setFlags(Qt.ItemFlag.ItemIsSelectable | Qt.ItemFlag.ItemIsEnabled)
        self.setItem(row, 3, reply_item)
        
        # 转人工
        transfer_text = '是' if keyword_data.get('transfer_to_human', False) else '否'
        transfer_item = QTableWidgetItem(transfer_text)
        transfer_item.setFlags(Qt.ItemFlag.ItemIsSelectable | Qt.ItemFlag.ItemIsEnabled)
        self.setItem(row, 4, transfer_item)
        
        # 优先级
        priority_item = QTableWidgetItem(str(keyword_data.get('priority', 0)))
        priority_item.setFlags(Qt.ItemFlag.ItemIsSelectable | Qt.ItemFlag.ItemIsEnabled)
        self.setItem(row, 5, priority_item)
        
        # 操作按钮
        action_widget = QWidget()
        action_layout = QHBoxLayout(action_widget)
        action_layout.setContentsMargins(5, 5, 5, 5)
        action_layout.setSpacing(5)
        action_layout.setAlignment(Qt.AlignmentFlag.AlignCenter)
        
        # 编辑按钮
        edit_btn = PushButton("编辑")
        edit_btn.setIcon(FIF.EDIT)
        edit_btn.setFixedSize(100, 30)
        edit_btn.clicked.connect(lambda: self.edit_clicked.emit(keyword_id))
        
        # 删除按钮
        delete_btn = PushButton("删除")
        delete_btn.setIcon(FIF.DELETE)
        delete_btn.setFixedSize(100, 30)
        delete_btn.clicked.connect(lambda: self.delete_clicked.emit(keyword_id))
        
        action_layout.addWidget(edit_btn)
        action_layout.addWidget(delete_btn)
        self.setCellWidget(row, 6, action_widget)
        
    def clearTable(self):
        """清空表格"""
        self.setRowCount(0)


class KeywordDialog(QDialog):
    """关键词编辑对话框"""
    
    def __init__(self, parent=None, keyword_data: Optional[dict] = None):
        super().__init__(parent)
        self.keyword_data = keyword_data or {}
        self.setupUI()
        
    def setupUI(self):
        """设置对话框UI"""
        self.setWindowTitle('编辑关键词' if self.keyword_data else '添加关键词')
        self.setMinimumWidth(400)
        
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
                QComboBox::drop-down {
                    border: none;
                }
                QComboBox::down-arrow {
                    image: none;
                    border-left: 4px solid transparent;
                    border-right: 4px solid transparent;
                    border-top: 6px solid #ffffff;
                    margin-right: 8px;
                }
                QComboBox QAbstractItemView {
                    background-color: #333333;
                    color: #ffffff;
                    selection-background-color: #0078d4;
                }
                QCheckBox {
                    color: #ffffff;
                }
                QCheckBox::indicator {
                    width: 16px;
                    height: 16px;
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
        self.group_combo.setEditable(True)
        self.group_combo.addItem('default')
        # 加载现有分组
        try:
            groups = db_manager.get_all_keyword_groups()
            for group in groups:
                if group and group not in ['default']:
                    self.group_combo.addItem(group)
        except:
            pass
        self.group_combo.setCurrentText(self.keyword_data.get('group_name', 'default'))
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
        
        # 回复内容
        self.reply_edit = QTextEdit()
        self.reply_edit.setPlaceholderText('输入回复内容（可选）')
        self.reply_edit.setMaximumHeight(80)
        self.reply_edit.setText(self.keyword_data.get('reply_content', '') or '')
        layout.addRow('回复内容:', self.reply_edit)
        
        # 转人工
        self.transfer_check = QCheckBox('匹配后转人工客服')
        self.transfer_check.setChecked(self.keyword_data.get('transfer_to_human', False))
        layout.addRow('', self.transfer_check)
        
        # 优先级
        self.priority_spin = QSpinBox()
        self.priority_spin.setRange(0, 100)
        self.priority_spin.setValue(self.keyword_data.get('priority', 0))
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
            'keyword': self.keyword_edit.text().strip(),
            'group_name': self.group_combo.currentText().strip() or 'default',
            'match_type': self.match_type_combo.currentData(),
            'reply_content': self.reply_edit.toPlainText().strip() or None,
            'transfer_to_human': self.transfer_check.isChecked(),
            'priority': self.priority_spin.value()
        }


class KeywordManagerWidget(QFrame):
    """关键词管理主界面"""

    def __init__(self, parent: Optional[QWidget] = None):
        super().__init__(parent=parent)
        self.keywords_data: List[dict] = []  # 存储关键词数据
        self.setupUI()
        self.loadKeywordsFromDB()
        
    def setupUI(self):
        """设置主界面UI"""
        # 主布局
        main_layout = QVBoxLayout(self)
        main_layout.setContentsMargins(30, 30, 30, 30)
        main_layout.setSpacing(25)
        
        # 创建头部区域
        header_widget = self.createHeaderWidget()
        
        # 创建内容区域（表格）
        self.table_widget = KeywordTableWidget()
        
        # 连接表格信号
        self.table_widget.edit_clicked.connect(self.onEditKeyword)
        self.table_widget.delete_clicked.connect(self.onDeleteKeyword)
        
        # 连接按钮信号
        self.add_btn.clicked.connect(self.onAddKeyword)
        self.test_btn.clicked.connect(self.onTestKeywords)
        self.import_btn.clicked.connect(self.onImportKeywords)
        
        # 添加到主布局
        main_layout.addWidget(header_widget)
        main_layout.addWidget(self.table_widget, 1)
        
        # 设置对象名
        self.setObjectName("关键词管理")
    
    def createHeaderWidget(self):
        """创建头部区域"""
        header_widget = QWidget()
        header_layout = QHBoxLayout(header_widget)
        header_layout.setContentsMargins(0, 0, 0, 0)
        header_layout.setSpacing(20)
        
        # 标题
        title_label = SubtitleLabel("关键词管理")
        
        # 统计信息
        self.stats_label = CaptionLabel("共 0 个关键词")
        
        # 左侧标题区域
        title_area = QWidget()
        title_layout = QVBoxLayout(title_area)
        title_layout.setContentsMargins(0, 0, 0, 0)
        title_layout.setSpacing(5)
        title_layout.addWidget(title_label)
        title_layout.addWidget(self.stats_label)
        
        # 添加关键词按钮
        self.add_btn = PrimaryPushButton("添加关键词")
        self.add_btn.setIcon(FIF.ADD)
        self.add_btn.setFixedSize(120, 40)
        
        # 测试按钮
        self.test_btn = PushButton("测试关键词")
        self.test_btn.setIcon(FIF.SEARCH)
        self.test_btn.setFixedSize(120, 40)
        
        # 批量导入按钮
        self.import_btn = PushButton("批量导入")
        self.import_btn.setIcon(FIF.FOLDER_ADD)
        self.import_btn.setFixedSize(120, 40)
        
        # 按钮容器
        buttons_widget = QWidget()
        buttons_layout = QHBoxLayout(buttons_widget)
        buttons_layout.setContentsMargins(0, 0, 0, 0)
        buttons_layout.setSpacing(10)
        buttons_layout.addWidget(self.test_btn)
        buttons_layout.addWidget(self.import_btn)
        buttons_layout.addWidget(self.add_btn)
        
        # 添加到头部布局
        header_layout.addWidget(title_area)
        header_layout.addStretch()
        header_layout.addWidget(buttons_widget)
        
        return header_widget
    
    def loadKeywordsFromDB(self):
        """从数据库加载关键词数据"""
        try:
            # 从数据库获取所有关键词
            keywords = db_manager.get_all_keywords()
            self.keywords_data = [
                {
                    "id": kw["id"],
                    "keyword": kw["keyword"],
                    "group_name": kw.get("group_name", "default"),
                    "match_type": kw.get("match_type", "partial"),
                    "reply_content": kw.get("reply_content"),
                    "transfer_to_human": kw.get("transfer_to_human", False),
                    "priority": kw.get("priority", 0)
                }
                for kw in keywords
            ]
            
            # 如果数据库为空，初始化示例关键词
            if not self.keywords_data:
                self.initializeSampleKeywords()
            
            self.refreshKeywordList()
        except Exception as e:
            print(f"加载关键词失败: {e}")
            # 如果数据库加载失败，使用示例数据
            self.initializeSampleKeywords()
    
    def initializeSampleKeywords(self):
        """初始化示例关键词到数据库"""
        sample_keywords = [
            {"keyword": "转人工", "group_name": "转人工", "match_type": "partial", "transfer_to_human": True, "priority": 10},
            {"keyword": "人工客服", "group_name": "转人工", "match_type": "partial", "transfer_to_human": True, "priority": 10},
            {"keyword": "真人", "group_name": "转人工", "match_type": "partial", "transfer_to_human": True, "priority": 10},
            {"keyword": "客服", "group_name": "转人工", "match_type": "partial", "transfer_to_human": True, "priority": 10},
            {"keyword": "人工", "group_name": "转人工", "match_type": "partial", "transfer_to_human": True, "priority": 10},
            {"keyword": "工单", "group_name": "转人工", "match_type": "partial", "transfer_to_human": True, "priority": 10},
            {"keyword": "好评", "group_name": "默认", "match_type": "partial", "reply_content": "感谢您的好评！祝您生活愉快~", "priority": 5},
            {"keyword": "取消订单", "group_name": "订单问题", "match_type": "partial", "transfer_to_human": True, "priority": 8},
            {"keyword": "改地址", "group_name": "订单问题", "match_type": "partial", "transfer_to_human": True, "priority": 8},
            {"keyword": "转售后客服", "group_name": "转人工", "match_type": "partial", "transfer_to_human": True, "priority": 10},
            {"keyword": "转售后", "group_name": "转人工", "match_type": "partial", "transfer_to_human": True, "priority": 10},
            {"keyword": "返现", "group_name": "售后", "match_type": "partial", "transfer_to_human": True, "priority": 7},
            {"keyword": "过敏", "group_name": "售后", "match_type": "partial", "transfer_to_human": True, "priority": 9},
            {"keyword": "退款", "group_name": "售后", "match_type": "partial", "transfer_to_human": True, "priority": 8},
            {"keyword": "没有效果", "group_name": "售后", "match_type": "partial", "transfer_to_human": True, "priority": 7},
            {"keyword": "骗人", "group_name": "投诉", "match_type": "partial", "transfer_to_human": True, "priority": 9},
            {"keyword": "投诉", "group_name": "投诉", "match_type": "partial", "transfer_to_human": True, "priority": 10},
            {"keyword": "纠纷", "group_name": "投诉", "match_type": "partial", "transfer_to_human": True, "priority": 9},
            {"keyword": "开发票", "group_name": "发票", "match_type": "partial", "transfer_to_human": True, "priority": 6},
            {"keyword": "开票", "group_name": "发票", "match_type": "partial", "transfer_to_human": True, "priority": 6},
            {"keyword": "烂", "group_name": "售后", "match_type": "partial", "transfer_to_human": True, "priority": 7},
            {"keyword": "取消", "group_name": "订单问题", "match_type": "partial", "transfer_to_human": True, "priority": 7},
            {"keyword": "备注", "group_name": "订单问题", "match_type": "partial", "transfer_to_human": True, "priority": 6}
        ]
        
        # 将示例关键词添加到数据库
        for kw_data in sample_keywords:
            if db_manager.add_keyword(
                keyword=kw_data["keyword"],
                group_name=kw_data.get("group_name", "default"),
                match_type=kw_data.get("match_type", "partial"),
                reply_content=kw_data.get("reply_content"),
                transfer_to_human=kw_data.get("transfer_to_human", False),
                priority=kw_data.get("priority", 0)
            ):
                self.keywords_data.append(kw_data)
        
        self.refreshKeywordList()
    
    def refreshKeywordList(self):
        """刷新关键词列表"""
        # 清空表格
        self.table_widget.clearTable()
        
        # 添加关键词到表格
        for keyword_data in self.keywords_data:
            self.table_widget.addKeyword(keyword_data)
        
        # 更新统计信息
        self.updateStats()
    
    def updateStats(self):
        """更新统计信息"""
        total_count = len(self.keywords_data)
        groups = set(kw.get('group_name', 'default') for kw in self.keywords_data)
        self.stats_label.setText(f"共 {total_count} 个关键词，{len(groups)} 个分组")
    
    def onEditKeyword(self, keyword_id: int):
        """编辑关键词回调"""
        # 查找关键词数据
        keyword_data = None
        for kw in self.keywords_data:
            if kw.get('id') == keyword_id:
                keyword_data = kw
                break
        
        if not keyword_data:
            QMessageBox.warning(self, '错误', '找不到关键词数据！')
            return
        
        # 打开编辑对话框
        dialog = KeywordDialog(self, keyword_data)
        if dialog.exec():
            new_data = dialog.get_data()
            if not new_data['keyword']:
                QMessageBox.warning(self, '失败', '关键词不能为空！')
                return
            
            # 更新数据库
            if db_manager.update_keyword(
                old_keyword=keyword_data['keyword'],
                new_keyword=new_data['keyword'],
                group_name=new_data['group_name'],
                match_type=new_data['match_type'],
                reply_content=new_data['reply_content'],
                transfer_to_human=new_data['transfer_to_human'],
                priority=new_data['priority']
            ):
                # 更新本地数据
                for i, kw in enumerate(self.keywords_data):
                    if kw.get('id') == keyword_id:
                        self.keywords_data[i] = {
                            'id': keyword_id,
                            **new_data
                        }
                        break
                
                self.refreshKeywordList()
                QMessageBox.information(self, '成功', '关键词修改成功！')
            else:
                QMessageBox.warning(self, '失败', '关键词修改失败！')
    
    def onDeleteKeyword(self, keyword_id: int):
        """删除关键词回调"""
        # 查找关键词数据
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
            try:
                # 从数据库删除关键词
                if db_manager.delete_keyword(keyword):
                    print(f"成功删除关键词: {keyword}")
                    # 从本地数据中移除
                    self.keywords_data = [k for k in self.keywords_data if k.get('id') != keyword_id]
                    self.refreshKeywordList()
                    QMessageBox.information(self, '成功', f'关键词 "{keyword}" 删除成功!')
                else:
                    print(f"删除关键词失败: {keyword}")
                    QMessageBox.warning(self, '失败', f'删除关键词 "{keyword}" 失败!')
            except Exception as e:
                print(f"删除关键词出错: {e}")
                QMessageBox.critical(self, '错误', f'删除关键词时出错: {str(e)}')
    
    def addKeyword(self, keyword_data: dict) -> bool:
        """添加新关键词"""
        try:
            keyword = keyword_data.get('keyword', '').strip()
            # 检查关键词是否为空
            if not keyword:
                print("关键词不能为空")
                return False
                
            # 添加到数据库
            if db_manager.add_keyword(
                keyword=keyword,
                group_name=keyword_data.get('group_name', 'default'),
                match_type=keyword_data.get('match_type', 'partial'),
                reply_content=keyword_data.get('reply_content'),
                transfer_to_human=keyword_data.get('transfer_to_human', False),
                priority=keyword_data.get('priority', 0)
            ):
                print(f"成功添加关键词: {keyword}")
                # 重新加载数据
                self.loadKeywordsFromDB()
                return True
            else:
                print(f"添加关键词失败: {keyword} (可能已存在)")
                return False
        except Exception as e:
            print(f"添加关键词出错: {e}")
            return False
    
    def removeKeyword(self, keyword: str) -> bool:
        """移除关键词"""
        try:
            # 从数据库删除
            if db_manager.delete_keyword(keyword):
                # 从本地数据中移除
                self.keywords_data = [k for k in self.keywords_data if k.get('keyword') != keyword]
                self.refreshKeywordList()
                return True
            else:
                return False
        except Exception as e:
            print(f"移除关键词出错: {e}")
            return False
            
    def reloadKeywords(self):
        """重新加载关键词数据"""
        self.loadKeywordsFromDB()
        
    def onAddKeyword(self):
        """添加关键词按钮点击事件"""
        dialog = KeywordDialog(self)
        if dialog.exec():
            keyword_data = dialog.get_data()
            if not keyword_data['keyword']:
                QMessageBox.warning(self, '失败', '关键词不能为空！')
                return
            
            if self.addKeyword(keyword_data):
                QMessageBox.information(self, '成功', f'关键词 "{keyword_data["keyword"]}" 添加成功!')
            else:
                QMessageBox.warning(self, '失败', f'关键词 "{keyword_data["keyword"]}" 添加失败，可能已存在!')
    
    def onTestKeywords(self):
        """测试关键词按钮点击事件"""
        dialog = KeywordTestDialog(self)
        dialog.exec()
    
    def onImportKeywords(self):
        """批量导入关键词按钮点击事件"""
        text, ok = QInputDialog.getMultiLineText(
            self, '批量导入关键词', 
            '请输入关键词，每行一个:\n(空行将被忽略)'
        )
        if ok and text.strip():
            keywords = [line.strip() for line in text.split('\n') if line.strip()]
            success_count = 0
            duplicate_count = 0
            
            for keyword in keywords:
                if self.addKeyword({'keyword': keyword}):
                    success_count += 1
                else:
                    duplicate_count += 1
            
            message = f'导入完成!\n成功: {success_count} 个\n重复/失败: {duplicate_count} 个'
            QMessageBox.information(self, '导入结果', message)
