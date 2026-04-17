# 关键词管理界面 - 支持分组管理，多关键词对应同一回复

from PyQt6.QtCore import Qt, pyqtSignal
from PyQt6.QtWidgets import (QFrame, QHBoxLayout, QVBoxLayout, QWidget, QLabel,
                            QTableWidget, QTableWidgetItem, QHeaderView, QAbstractItemView,
                            QInputDialog, QMessageBox, QTextEdit, QSizePolicy,
                            QDialog, QScrollArea)
from PyQt6.QtGui import QFont, QIcon, QColor
from qfluentwidgets import (SubtitleLabel, CaptionLabel, BodyLabel,
                           PrimaryPushButton, PushButton,
                           ScrollArea, FluentIcon as FIF,
                           TableWidget, ComboBox, LineEdit, StrongBodyLabel)
from database.db_manager import db_manager
from Message.keyword_matcher import matcher_factory


class KeywordEditDialog(QDialog):
    """关键词编辑对话框（支持设置匹配类型）"""

    def __init__(self, keyword_data: dict = None, parent=None):
        """
        Args:
            keyword_data: 编辑模式传入 {'text': 'xxx', 'match_type': 'partial'}, 添加模式传None
        """
        super().__init__(parent)
        self.is_edit = keyword_data is not None
        self.keyword_data = keyword_data or {}

        self.setWindowTitle("编辑关键词" if self.is_edit else "添加关键词")
        self.setModal(True)
        self.resize(420, 200)
        self.setupUI()

    def setupUI(self):
        layout = QVBoxLayout(self)
        layout.setSpacing(12)
        layout.setContentsMargins(24, 20, 24, 20)

        # 关键词文本
        kw_label = BodyLabel("关键词")
        self.kw_edit = LineEdit()
        self.kw_edit.setPlaceholderText("请输入关键词")
        self.kw_edit.setText(self.keyword_data.get('text', ''))
        layout.addWidget(kw_label)
        layout.addWidget(self.kw_edit)

        # 匹配类型
        type_label = BodyLabel("匹配类型")
        self.type_combo = ComboBox()
        self.type_combo.addItems([
            "🔍 部分匹配（默认）",
            "🎯 完全匹配（忽略符号）",
            "📝 正则表达式",
            "⭐ 通配符匹配（*任意字符, ?单个字符）"
        ])
        type_map = {'partial': 0, 'exact': 1, 'regex': 2, 'wildcard': 3}
        current_type = self.keyword_data.get('match_type', 'partial')
        self.type_combo.setCurrentIndex(type_map.get(current_type, 0))
        layout.addWidget(type_label)
        layout.addWidget(self.type_combo)

        # 按钮
        btn_layout = QHBoxLayout()
        btn_layout.addStretch()
        cancel_btn = PushButton("取消")
        cancel_btn.setFixedSize(100, 34)
        cancel_btn.clicked.connect(self.reject)
        ok_btn = PrimaryPushButton("确定")
        ok_btn.setFixedSize(100, 34)
        ok_btn.clicked.connect(self._on_confirm)
        btn_layout.addWidget(cancel_btn)
        btn_layout.addWidget(ok_btn)
        layout.addLayout(btn_layout)

    def _on_confirm(self):
        text = self.kw_edit.text().strip()
        if not text:
            QMessageBox.warning(self, '提示', '关键词不能为空！')
            return

        type_map = {0: 'partial', 1: 'exact', 2: 'regex', 3: 'wildcard'}
        match_type = type_map.get(self.type_combo.currentIndex(), 'partial')

        self._result = {
            'text': text,
            'match_type': match_type
        }
        self.accept()

    def get_result(self) -> dict:
        return getattr(self, '_result', None)


class GroupEditDialog(QDialog):
    """分组编辑对话框（添加/编辑共用）"""

    def __init__(self, group_data: dict = None, parent=None):
        """
        Args:
            group_data: 编辑模式传入已有分组数据，添加模式传None
        """
        super().__init__(parent)
        self.is_edit = group_data is not None
        self.group_data = group_data or {}

        self.setWindowTitle("编辑分组" if self.is_edit else "添加分组")
        self.setModal(True)
        self.resize(480, 420)
        self.setupUI()

    def setupUI(self):
        layout = QVBoxLayout(self)
        layout.setSpacing(12)
        layout.setContentsMargins(24, 20, 24, 20)

        # 分组名称
        name_label = BodyLabel("分组名称")
        self.name_edit = LineEdit()
        self.name_edit.setPlaceholderText("请输入分组名称")
        self.name_edit.setText(self.group_data.get('group_name', ''))
        layout.addWidget(name_label)
        layout.addWidget(self.name_edit)

        # 匹配类型：自动回复 / 转人工
        type_label = BodyLabel("匹配后操作")
        self.type_combo = ComboBox()
        self.type_combo.addItems(["💬 自动回复", "🔄 转人工"])
        if self.group_data.get('is_transfer'):
            self.type_combo.setCurrentIndex(1)
        self.type_combo.currentIndexChanged.connect(self._on_type_changed)
        layout.addWidget(type_label)
        layout.addWidget(self.type_combo)

        # 传递给AI（仅自动回复时显示）
        self.ai_container = QWidget()
        ai_layout = QVBoxLayout(self.ai_container)
        ai_layout.setContentsMargins(0, 0, 0, 0)
        ai_label = BodyLabel("传递给AI")
        ai_hint = CaptionLabel("开启后，如果消息中还有其他内容（如\"谢谢，能用多久？\"中的\"能用多久？\"），将把剩余内容交给AI处理")
        ai_hint.setStyleSheet("color: #888;")
        ai_hint.setWordWrap(True)
        self.ai_combo = ComboBox()
        self.ai_combo.addItems(["否 - 仅发送预设回复", "是 - 剩余内容传给AI"])
        if self.group_data.get('pass_to_ai'):
            self.ai_combo.setCurrentIndex(1)
        ai_layout.addWidget(ai_label)
        ai_layout.addWidget(ai_hint)
        ai_layout.addWidget(self.ai_combo)
        layout.addWidget(self.ai_container)

        # 回复内容
        self.reply_container = QWidget()
        reply_layout = QVBoxLayout(self.reply_container)
        reply_layout.setContentsMargins(0, 0, 0, 0)
        reply_label = BodyLabel("回复内容")
        reply_layout.addWidget(reply_label)
        self.reply_edit = QTextEdit()
        self.reply_edit.setPlaceholderText("请输入自动回复内容")
        old_reply = self.group_data.get('reply', '') or ''
        self.reply_edit.setPlainText(old_reply)
        self.reply_edit.setMinimumHeight(100)
        reply_layout.addWidget(self.reply_edit)
        layout.addWidget(self.reply_container)

        # 初始状态
        self._on_type_changed(self.type_combo.currentIndex())

        # 按钮
        btn_layout = QHBoxLayout()
        btn_layout.addStretch()
        cancel_btn = PushButton("取消")
        cancel_btn.setFixedSize(100, 34)
        cancel_btn.clicked.connect(self.reject)
        ok_btn = PrimaryPushButton("确定")
        ok_btn.setFixedSize(100, 34)
        ok_btn.clicked.connect(self._on_confirm)
        btn_layout.addWidget(cancel_btn)
        btn_layout.addWidget(ok_btn)
        layout.addLayout(btn_layout)

    def _on_type_changed(self, index):
        """切换类型时显隐控件"""
        is_auto_reply = (index == 0)
        self.ai_container.setVisible(is_auto_reply)
        self.reply_container.setVisible(True)
        if not is_auto_reply:
            self.reply_edit.setPlaceholderText("可选：转人工前先发送的回复内容，留空则直接转人工")

    def _on_confirm(self):
        name = self.name_edit.text().strip()
        if not name:
            QMessageBox.warning(self, '提示', '分组名称不能为空！')
            return

        is_auto_reply = (self.type_combo.currentIndex() == 0)
        if is_auto_reply:
            reply = self.reply_edit.toPlainText().strip()
            if not reply:
                QMessageBox.warning(self, '提示', '自动回复内容不能为空！')
                return
        else:
            reply = self.reply_edit.toPlainText().strip() or None

        self._result = {
            'group_name': name,
            'is_transfer': 0 if is_auto_reply else 1,
            'pass_to_ai': 1 if is_auto_reply and self.ai_combo.currentIndex() == 1 else 0,
            'reply': reply,
        }
        self.accept()

    def get_result(self) -> dict:
        return getattr(self, '_result', None)


class KeywordTestDialog(QDialog):
    """关键词测试对话框"""
    
    def __init__(self, parent=None):
        super().__init__(parent)
        self.setWindowTitle("关键词匹配测试")
        self.setModal(True)
        self.resize(700, 600)
        self.setupUI()
    
    def setupUI(self):
        layout = QVBoxLayout(self)
        layout.setSpacing(15)
        layout.setContentsMargins(24, 20, 24, 20)
        
        # 说明文字
        hint_label = CaptionLabel("输入测试消息，查看会匹配哪些关键词")
        hint_label.setStyleSheet("color: #888;")
        layout.addWidget(hint_label)
        
        # 输入区域
        input_label = BodyLabel("测试消息")
        self.message_edit = QTextEdit()
        self.message_edit.setPlaceholderText("请输入要测试的消息内容...")
        self.message_edit.setFixedHeight(60)
        layout.addWidget(input_label)
        layout.addWidget(self.message_edit)
        
        # 测试按钮
        btn_layout = QHBoxLayout()
        btn_layout.addStretch()
        test_btn = PrimaryPushButton("开始测试")
        test_btn.setFixedSize(120, 34)
        test_btn.clicked.connect(self._on_test)
        btn_layout.addWidget(test_btn)
        layout.addLayout(btn_layout)
        
        # 结果标题
        result_title = StrongBodyLabel("匹配结果")
        layout.addWidget(result_title)
        
        # 结果显示区域（滚动）
        self.result_scroll = ScrollArea()
        self.result_scroll.setWidgetResizable(True)
        self.result_scroll.setStyleSheet("QScrollArea { border: 1px solid rgba(255, 255, 255, 0.1); border-radius: 4px; background: transparent; }")
        
        self.result_container = QWidget()
        self.result_container.setStyleSheet("background: transparent;")
        self.result_layout = QVBoxLayout(self.result_container)
        self.result_layout.setContentsMargins(15, 15, 15, 15)
        self.result_layout.setSpacing(10)
        self.result_layout.addStretch()
        
        self.result_scroll.setWidget(self.result_container)
        layout.addWidget(self.result_scroll, 1)  # 设置伸展因子为1，让结果区域占据剩余空间
        
        # 关闭按钮
        close_btn = PushButton("关闭")
        close_btn.setFixedSize(100, 34)
        close_btn.clicked.connect(self.accept)
        close_layout = QHBoxLayout()
        close_layout.addStretch()
        close_layout.addWidget(close_btn)
        layout.addLayout(close_layout)
    
    def _on_test(self):
        """执行测试"""
        message = self.message_edit.toPlainText().strip()
        if not message:
            QMessageBox.warning(self, '提示', '请输入测试消息！')
            return
        
        # 清空之前的结果
        while self.result_layout.count() > 1:
            item = self.result_layout.takeAt(0)
            if item.widget():
                item.widget().deleteLater()
        
        # 获取所有分组数据
        groups = db_manager.get_all_keyword_groups()
        if not groups:
            no_data_label = CaptionLabel("暂无关键词数据")
            no_data_label.setStyleSheet("color: #999; padding: 20px;")
            self.result_layout.insertWidget(0, no_data_label)
            return
        
        # 匹配结果
        matched_groups = []
        
        for group in groups:
            keywords = group.get('keywords', [])
            matched_keywords = []
            
            for kw in keywords:
                # 兼容新旧格式
                if isinstance(kw, dict):
                    kw_text = kw.get('text', '')
                    kw_type = kw.get('match_type', 'partial')
                else:
                    kw_text = str(kw)
                    kw_type = 'partial'
                
                # 使用匹配器测试
                matcher = matcher_factory.get_matcher(kw_type)
                if matcher.match(kw_text, message):
                    matched_keywords.append({
                        'text': kw_text,
                        'match_type': kw_type
                    })
            
            if matched_keywords:
                matched_groups.append({
                    'group': group,
                    'keywords': matched_keywords
                })
        
        # 显示结果
        if not matched_groups:
            no_match_label = CaptionLabel("❌ 未匹配到任何关键词")
            no_match_label.setStyleSheet("color: #e74c3c; padding: 20px; font-size: 14px;")
            self.result_layout.insertWidget(0, no_match_label)
            return
        
        # 显示匹配结果
        for match_info in matched_groups:
            group = match_info['group']
            keywords = match_info['keywords']
            
            # 创建结果卡片
            card = QFrame()
            card.setStyleSheet("""
                QFrame {
                    border: 1px solid rgba(255, 255, 255, 0.1);
                    border-radius: 6px;
                    padding: 10px;
                }
            """)
            card_layout = QVBoxLayout(card)
            card_layout.setSpacing(8)
            
            # 分组名称
            group_name_label = StrongBodyLabel(f"📦 {group['group_name']}")
            card_layout.addWidget(group_name_label)
            
            # 匹配的关键词
            kw_text = "、".join([f"「{kw['text']}」" for kw in keywords])
            kw_label = CaptionLabel(f"匹配关键词: {kw_text}")
            kw_label.setWordWrap(True)
            card_layout.addWidget(kw_label)
            
            # 操作类型
            is_transfer = group.get('is_transfer', 0)
            if is_transfer:
                type_text = "🔄 转人工"
                type_color = "#e74c3c"
            elif group.get('pass_to_ai'):
                type_text = "🤖 传AI"
                type_color = "#3498db"
            else:
                type_text = "💬 自动回复"
                type_color = "#27ae60"
            
            type_label = CaptionLabel(f"操作类型: {type_text}")
            type_label.setStyleSheet(f"color: {type_color};")
            card_layout.addWidget(type_label)
            
            # 回复内容
            reply = group.get('reply', '')
            if reply:
                reply_preview = reply[:100] + ("..." if len(reply) > 100 else "")
                reply_label = CaptionLabel(f"回复内容: {reply_preview}")
                reply_label.setWordWrap(True)
                reply_label.setStyleSheet("color: #666;")
                card_layout.addWidget(reply_label)
            
            self.result_layout.insertWidget(self.result_layout.count() - 1, card)


class GroupCard(QFrame):
    """关键词分组卡片"""

    # 定义信号
    edit_group_clicked = pyqtSignal(int)   # 编辑分组（group_id）
    delete_group_clicked = pyqtSignal(int) # 删除分组（group_id）
    add_keyword_clicked = pyqtSignal(int)  # 添加关键词（group_id）
    edit_keyword_clicked = pyqtSignal(int, object, int)  # 编辑关键词（group_id, keyword_data, index）
    delete_keyword_clicked = pyqtSignal(int, object, int)  # 删除关键词（group_id, keyword_data, index）

    def __init__(self, group_data: dict, parent=None):
        super().__init__(parent)
        self.group_data = group_data
        self.setupUI()

    def setupUI(self):
        """设置卡片UI"""
        self.setObjectName("groupCard")
        self.setFrameShape(QFrame.Shape.StyledPanel)

        main_layout = QVBoxLayout(self)
        main_layout.setContentsMargins(15, 12, 15, 12)
        main_layout.setSpacing(8)

        # === 头部：分组名 + 操作按钮 ===
        header_layout = QHBoxLayout()
        header_layout.setSpacing(10)

        # 分组名称
        name_label = BodyLabel(self.group_data['group_name'])
        name_label.setFont(QFont("Microsoft YaHei", 11, QFont.Weight.Bold))
        header_layout.addWidget(name_label)

        # 标签：显示回复类型
        if self.group_data.get('is_transfer'):
            tag = QLabel("🔄 转人工")
            tag.setStyleSheet("color: #e74c3c; font-size: 12px; padding: 2px 8px; "
                             "background: #fde8e8; border-radius: 4px;")
            header_layout.addWidget(tag)
        if self.group_data.get('pass_to_ai'):
            tag = QLabel("🤖 传AI")
            tag.setStyleSheet("color: #3498db; font-size: 12px; padding: 2px 8px; "
                             "background: #e8f0fe; border-radius: 4px;")
            header_layout.addWidget(tag)
        elif self.group_data.get('reply'):
            tag = QLabel("💬 自动回复")
            tag.setStyleSheet("color: #27ae60; font-size: 12px; padding: 2px 8px; "
                             "background: #e8f8f0; border-radius: 4px;")
            header_layout.addWidget(tag)

        header_layout.addStretch()

        # 编辑分组按钮
        edit_group_btn = PushButton("编辑分组")
        edit_group_btn.setIcon(FIF.EDIT)
        edit_group_btn.setFixedSize(90, 28)
        edit_group_btn.clicked.connect(lambda: self.edit_group_clicked.emit(self.group_data['id']))
        header_layout.addWidget(edit_group_btn)

        # 删除分组按钮
        delete_group_btn = PushButton("删除分组")
        delete_group_btn.setIcon(FIF.DELETE)
        delete_group_btn.setFixedSize(90, 28)
        delete_group_btn.clicked.connect(lambda: self.delete_group_clicked.emit(self.group_data['id']))
        header_layout.addWidget(delete_group_btn)

        main_layout.addLayout(header_layout)

        # === 回复内容预览 ===
        reply = self.group_data.get('reply', '')
        is_transfer = self.group_data.get('is_transfer', 0)
        if is_transfer:
            reply_preview = CaptionLabel("匹配关键词后自动转接人工客服")
            reply_preview.setStyleSheet("color: #e74c3c; padding: 4px 0;")
            reply_preview.setWordWrap(True)
            main_layout.addWidget(reply_preview)
        elif reply:
            preview_text = reply[:80] + ("..." if len(reply) > 80 else "")
            reply_preview = CaptionLabel(f"📝 回复: {preview_text}")
            reply_preview.setStyleSheet("color: #666; padding: 4px 0;")
            reply_preview.setWordWrap(True)
            main_layout.addWidget(reply_preview)

        # === 关键词列表 ===
        keywords = self.group_data.get('keywords', [])
        kw_count_label = CaptionLabel(f"关键词 ({len(keywords)}个)")
        kw_count_label.setStyleSheet("color: #888; margin-top: 4px;")
        main_layout.addWidget(kw_count_label)

        if keywords:
            kw_container = QWidget()
            kw_layout = QVBoxLayout(kw_container)
            kw_layout.setContentsMargins(0, 0, 0, 0)
            kw_layout.setSpacing(4)

            # 匹配类型显示映射
            type_labels = {
                'partial': '🔍',
                'exact': '🎯',
                'regex': '📝',
                'wildcard': '⭐'
            }

            for idx, kw in enumerate(keywords):
                kw_row = QWidget()
                kw_row_layout = QHBoxLayout(kw_row)
                kw_row_layout.setContentsMargins(8, 2, 8, 2)
                kw_row_layout.setSpacing(8)

                # 兼容新旧格式
                if isinstance(kw, dict):
                    kw_text = kw.get('text', '')
                    kw_type = kw.get('match_type', 'partial')
                else:
                    kw_text = str(kw)
                    kw_type = 'partial'

                # 显示匹配类型图标 + 关键词
                type_icon = type_labels.get(kw_type, '🔍')
                kw_label = QLabel(f"{type_icon} {kw_text}")
                kw_label.setStyleSheet("font-size: 13px;")
                kw_row_layout.addWidget(kw_label)

                kw_row_layout.addStretch()

                # 编辑关键词按钮
                edit_btn = PushButton("编辑")
                edit_btn.setFixedSize(60, 24)
                edit_btn.clicked.connect(lambda checked, g=self.group_data['id'], k=kw, i=idx:
                                         self._emit_edit_keyword(g, k, i))
                kw_row_layout.addWidget(edit_btn)

                # 删除关键词按钮
                del_btn = PushButton("删除")
                del_btn.setFixedSize(60, 24)
                del_btn.clicked.connect(lambda checked, g=self.group_data['id'], k=kw, i=idx:
                                        self._emit_delete_keyword(g, k, i))
                kw_row_layout.addWidget(del_btn)

                kw_layout.addWidget(kw_row)

            main_layout.addWidget(kw_container)

        # === 底部：添加关键词按钮 ===
        add_kw_btn = PushButton("+ 添加关键词")
        add_kw_btn.setFixedHeight(30)
        add_kw_btn.clicked.connect(lambda: self.add_keyword_clicked.emit(self.group_data['id']))
        main_layout.addWidget(add_kw_btn)

    def _find_keyword_id(self, keyword_data) -> int:
        """根据关键词数据从数据库查找关键词ID"""
        # 兼容新旧格式
        if isinstance(keyword_data, dict):
            kw_text = keyword_data.get('text', '')
        else:
            kw_text = str(keyword_data)
            
        all_keywords = db_manager.get_all_keywords()
        for kw in all_keywords:
            if kw.get('keyword') == kw_text:
                return kw['id']
        return -1

    def _emit_edit_keyword(self, group_id: int, keyword_data, index: int):
        """发送编辑关键词信号"""
        self.edit_keyword_clicked.emit(group_id, keyword_data, index)

    def _emit_delete_keyword(self, group_id: int, keyword_data, index: int):
        """发送删除关键词信号"""
        kw_id = self._find_keyword_id(keyword_data)
        self.delete_keyword_clicked.emit(group_id, keyword_data, kw_id)


class KeywordManagerWidget(QFrame):
    """关键词管理主界面"""

    def __init__(self, parent=None):
        super().__init__(parent)
        self.groups_data = []  # 存储分组数据
        self.setupUI()
        self.loadFromDB()

    def setupUI(self):
        """设置主界面UI"""
        main_layout = QVBoxLayout(self)
        main_layout.setContentsMargins(30, 30, 30, 30)
        main_layout.setSpacing(20)

        # 创建头部区域
        header_widget = self.createHeaderWidget()

        # 创建滚动区域（放卡片列表）
        self.scroll_area = ScrollArea()
        self.scroll_area.setWidgetResizable(True)
        self.scroll_area.setStyleSheet("""
            QScrollArea { border: none; background: transparent; }
            QScrollArea > QWidget > QWidget { background: transparent; }
        """)

        # 卡片容器
        self.cards_container = QWidget()
        self.cards_container.setStyleSheet("background: transparent;")
        self.cards_layout = QVBoxLayout(self.cards_container)
        self.cards_layout.setContentsMargins(0, 0, 0, 0)
        self.cards_layout.setSpacing(15)
        self.cards_layout.addStretch()

        self.scroll_area.setWidget(self.cards_container)

        main_layout.addWidget(header_widget)
        main_layout.addWidget(self.scroll_area, 1)

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
        self.stats_label = CaptionLabel("共 0 个分组，0 个关键词")

        # 左侧标题区域
        title_area = QWidget()
        title_layout = QVBoxLayout(title_area)
        title_layout.setContentsMargins(0, 0, 0, 0)
        title_layout.setSpacing(5)
        title_layout.addWidget(title_label)
        title_layout.addWidget(self.stats_label)

        # 测试匹配按钮
        self.test_btn = PushButton("🧪 测试匹配")
        self.test_btn.setFixedSize(120, 40)
        self.test_btn.clicked.connect(self.onTestKeywords)

        # 添加分组按钮
        self.add_group_btn = PrimaryPushButton("添加分组")
        self.add_group_btn.setIcon(FIF.ADD)
        self.add_group_btn.setFixedSize(120, 40)
        self.add_group_btn.clicked.connect(self.onAddGroup)

        header_layout.addWidget(title_area)
        header_layout.addStretch()
        header_layout.addWidget(self.test_btn)
        header_layout.addWidget(self.add_group_btn)

        return header_widget

    def loadFromDB(self):
        """从数据库加载分组数据"""
        try:
            self.groups_data = db_manager.get_all_keyword_groups()

            # 如果数据库为空，初始化示例数据
            if not self.groups_data:
                self.initializeSampleGroups()

            self.refreshCards()
        except Exception as e:
            print(f"加载关键词分组失败: {e}")
            self.initializeSampleGroups()

    def initializeSampleGroups(self):
        """初始化示例关键词分组 - 兼容新旧格式"""
        # 分组1：转人工
        db_manager.add_keyword_group(
            group_name="转人工",
            reply="稍等，我帮您转接人工客服，请稍候~",
            is_transfer=1
        )
        transfer_keywords = ["转人工", "人工客服", "真人", "客服", "人工", "工单",
                            "投诉", "不满意", "解决不了", "要求赔偿", "举报"]
        groups = db_manager.get_all_keyword_groups()
        if groups:
            transfer_group = next((g for g in groups if g['group_name'] == "转人工"), None)
            if transfer_group:
                for kw in transfer_keywords:
                    db_manager.add_keyword_to_group(kw, transfer_group['id'], match_type='partial')

        # 分组2：售后问题
        db_manager.add_keyword_group(
            group_name="售后问题",
            reply="关于售后问题，我会尽快帮您处理，请提供一下您的订单编号。",
            is_transfer=1
        )
        after_sale_keywords = ["退款", "取消订单", "没有效果", "骗人", "纠纷",
                              "过敏", "烂", "投诉", "返现"]
        groups = db_manager.get_all_keyword_groups()
        if groups:
            after_group = next((g for g in groups if g['group_name'] == "售后问题"), None)
            if after_group:
                for kw in after_sale_keywords:
                    db_manager.add_keyword_to_group(kw, after_group['id'], match_type='partial')

        # 分组3：订单操作
        db_manager.add_keyword_group(
            group_name="订单操作",
            reply="好的，我来帮您处理订单，请告诉我您的订单编号。",
            is_transfer=1
        )
        order_keywords = ["改地址", "转售后客服", "转售后", "取消", "备注"]
        groups = db_manager.get_all_keyword_groups()
        if groups:
            order_group = next((g for g in groups if g['group_name'] == "订单操作"), None)
            if order_group:
                for kw in order_keywords:
                    db_manager.add_keyword_to_group(kw, order_group['id'], match_type='partial')

        # 分组4：开发票
        db_manager.add_keyword_group(
            group_name="开发票",
            reply="好的，需要开具发票的话，请您提供一下开票信息（抬头、税号），我这边帮您处理。",
            is_transfer=0
        )
        invoice_keywords = ["开发票", "开票", "发票"]
        groups = db_manager.get_all_keyword_groups()
        if groups:
            invoice_group = next((g for g in groups if g['group_name'] == "开发票"), None)
            if invoice_group:
                for kw in invoice_keywords:
                    db_manager.add_keyword_to_group(kw, invoice_group['id'], match_type='partial')

        # 分组5：好评返现
        db_manager.add_keyword_group(
            group_name="好评返现",
            reply="您好，关于好评返现活动，请您先确认收货并给予五星好评，截图发给客服后即可领取奖励哦~",
            is_transfer=0
        )
        good_review_keywords = ["好评"]
        groups = db_manager.get_all_keyword_groups()
        if groups:
            good_review_group = next((g for g in groups if g['group_name'] == "好评返现"), None)
            if good_review_group:
                for kw in good_review_keywords:
                    db_manager.add_keyword_to_group(kw, good_review_group['id'], match_type='partial')

        # 重新加载
        self.groups_data = db_manager.get_all_keyword_groups()
        self.refreshCards()

    def refreshCards(self):
        """刷新卡片列表"""
        # 清空现有卡片（保留最后的stretch）
        while self.cards_layout.count() > 1:
            item = self.cards_layout.takeAt(0)
            if item.widget():
                item.widget().deleteLater()

        # 添加分组卡片
        for group_data in self.groups_data:
            card = GroupCard(group_data)
            card.edit_group_clicked.connect(self.onEditGroup)
            card.delete_group_clicked.connect(self.onDeleteGroup)
            card.add_keyword_clicked.connect(self.onAddKeyword)
            card.edit_keyword_clicked.connect(self.onEditKeyword)
            card.delete_keyword_clicked.connect(self.onDeleteKeyword)
            self.cards_layout.insertWidget(self.cards_layout.count() - 1, card)

        # 更新统计信息
        total_groups = len(self.groups_data)
        total_keywords = 0
        for g in self.groups_data:
            keywords = g.get('keywords', [])
            total_keywords += len(keywords)
        self.stats_label.setText(f"共 {total_groups} 个分组，{total_keywords} 个关键词")

    # ===== 分组操作 =====
    def onAddGroup(self):
        """添加分组"""
        dlg = GroupEditDialog(group_data=None, parent=self)
        if dlg.exec() != QDialog.DialogCode.Accepted:
            return
        result = dlg.get_result()

        if db_manager.add_keyword_group(
            result['group_name'], reply=result['reply'],
            is_transfer=result['is_transfer'], pass_to_ai=result['pass_to_ai']
        ):
            self.groups_data = db_manager.get_all_keyword_groups()
            self.refreshCards()
            QMessageBox.information(self, '成功', f'分组 "{result["group_name"]}" 创建成功！')
        else:
            QMessageBox.warning(self, '失败', f'创建分组失败！')

    def onEditGroup(self, group_id: int):
        """编辑分组"""
        group = db_manager.get_keyword_group(group_id)
        if not group:
            return

        dlg = GroupEditDialog(group_data=group, parent=self)
        if dlg.exec() != QDialog.DialogCode.Accepted:
            return
        result = dlg.get_result()

        if db_manager.update_keyword_group(
            group_id, group_name=result['group_name'],
            reply=result['reply'], is_transfer=result['is_transfer'],
            pass_to_ai=result['pass_to_ai']
        ):
            self.groups_data = db_manager.get_all_keyword_groups()
            self.refreshCards()
            QMessageBox.information(self, '成功', f'分组 "{result["group_name"]}" 更新成功！')
        else:
            QMessageBox.warning(self, '失败', f'更新分组失败！')

    def onDeleteGroup(self, group_id: int):
        """删除分组"""
        group = db_manager.get_keyword_group(group_id)
        if not group:
            return

        kw_count = len(group.get('keywords', []))
        reply = QMessageBox.question(
            self, '确认删除',
            f'确定要删除分组 "{group["group_name"]}" 吗？\n'
            f'该分组包含 {kw_count} 个关键词，将一并删除。',
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
            QMessageBox.StandardButton.No
        )

        if reply == QMessageBox.StandardButton.Yes:
            if db_manager.delete_keyword_group(group_id):
                self.groups_data = db_manager.get_all_keyword_groups()
                self.refreshCards()
                QMessageBox.information(self, '成功', f'分组 "{group["group_name"]}" 删除成功！')
            else:
                QMessageBox.warning(self, '失败', f'删除分组失败！')

    # ===== 关键词操作 =====
    def onAddKeyword(self, group_id: int):
        """向分组添加关键词（支持设置匹配类型）"""
        group = db_manager.get_keyword_group(group_id)
        if not group:
            return

        dlg = KeywordEditDialog(keyword_data=None, parent=self)
        if dlg.exec() != QDialog.DialogCode.Accepted:
            return
        result = dlg.get_result()

        if db_manager.add_keyword_to_group(result['text'], group_id, result['match_type']):
            self.groups_data = db_manager.get_all_keyword_groups()
            self.refreshCards()
        else:
            QMessageBox.warning(self, '失败', f'关键词 "{result["text"]}" 已存在于该分组！')

    def onEditKeyword(self, group_id: int, keyword_data, index: int):
        """编辑关键词（支持修改匹配类型）"""
        group = db_manager.get_keyword_group(group_id)
        if not group:
            return

        # 准备编辑数据
        if isinstance(keyword_data, dict):
            edit_data = keyword_data
        else:
            edit_data = {'text': str(keyword_data), 'match_type': 'partial'}

        # 找到关键词ID
        keyword_id = -1
        all_keywords = db_manager.get_all_keywords()
        for kw in all_keywords:
            if kw.get('keyword') == edit_data['text']:
                keyword_id = kw.get('id', -1)
                break

        dlg = KeywordEditDialog(keyword_data=edit_data, parent=self)
        if dlg.exec() != QDialog.DialogCode.Accepted:
            return
        result = dlg.get_result()

        if keyword_id > 0:
            if db_manager.update_keyword(
                keyword_id, 
                new_keyword=result['text'],
                match_type=result['match_type']
            ):
                self.groups_data = db_manager.get_all_keyword_groups()
                self.refreshCards()
            else:
                QMessageBox.warning(self, '失败', f'关键词 "{result["text"]}" 已存在于该分组！')
        else:
            QMessageBox.warning(self, '提示', '关键词数据异常，请刷新后重试')

    def onDeleteKeyword(self, group_id: int, keyword_data, keyword_id: int):
        """删除关键词"""
        group = db_manager.get_keyword_group(group_id)
        if not group:
            return

        # 获取关键词文本
        if isinstance(keyword_data, dict):
            kw_text = keyword_data.get('text', '')
        else:
            kw_text = str(keyword_data)

        reply = QMessageBox.question(
            self, '确认删除',
            f'确定要删除关键词 "{kw_text}" 吗？',
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
            QMessageBox.StandardButton.No
        )

        if reply == QMessageBox.StandardButton.Yes and keyword_id > 0:
            if db_manager.delete_keyword(keyword_id):
                self.groups_data = db_manager.get_all_keyword_groups()
                self.refreshCards()
            else:
                QMessageBox.warning(self, '失败', f'删除关键词失败！')

    def reloadKeywords(self):
        """重新加载关键词数据"""
        self.loadFromDB()
    
    def onTestKeywords(self):
        """打开关键词测试对话框"""
        dlg = KeywordTestDialog(parent=self)
        dlg.exec()
