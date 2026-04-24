#!/usr/bin/env python
# -*- coding: utf-8 -*-

"""
日志管理界面
"""

import os
import sys
from datetime import datetime
from typing import List, Dict, Any, Optional
from collections import deque
from PyQt6.QtCore import Qt, QTimer, pyqtSignal, QThread, QAbstractTableModel, QModelIndex, QObject, QEvent
from PyQt6.QtWidgets import (QFrame, QVBoxLayout, QHBoxLayout, QWidget,
                            QTextEdit, QFileDialog, QMessageBox, QSplitter,
                            QTableView, QHeaderView, QApplication,
                            QStyledItemDelegate, QStyleOptionViewItem)
from PyQt6.QtGui import QFont, QTextCursor, QColor, QTextCharFormat, QBrush, QPainter
from qfluentwidgets import (CardWidget, SubtitleLabel, CaptionLabel, BodyLabel,
                           PrimaryPushButton, PushButton, StrongBodyLabel,
                           ComboBox, LineEdit, ScrollArea, FluentIcon as FIF,
                           InfoBar, InfoBarPosition, ToolButton, CheckBox, isDarkTheme)
from utils.logger_loguru import get_logger, logger, UILogHandler  # pyright: ignore[reportAttributeAccessIssue]


class LogHandler:
    """兼容性LogHandler类 - 实际使用UILogHandler"""

    def __init__(self, signal_emitter):
        # 使用新的UILogHandler
        self.ui_handler = UILogHandler()
        # 连接信号
        self.ui_handler.log_received.connect(signal_emitter.log_received)
        self.signal_emitter = signal_emitter
        self._installed = False
        self.level = "DEBUG"  # 默认级别

    def emit(self, record):
        """为了兼容性保留，实际不使用"""
        pass

    def install(self):
        """安装日志处理器"""
        if not self._installed:
            self.ui_handler.install()
            self._installed = True

    def uninstall(self):
        """卸载日志处理器"""
        if self._installed:
            self.ui_handler.uninstall()
            self._installed = False

    def setLevel(self, level):
        """设置日志级别（兼容性方法）"""
        self.level = level

    def setFormatter(self, formatter):
        """设置格式器（兼容性方法）"""
        # loguru不需要格式器，保留为兼容性
        pass


class LogSignalEmitter(QWidget):
    """日志信号发射器"""
    # 适配loguru的record类型
    log_received = pyqtSignal(str, str, object)  # level, message, record

    def __init__(self, parent: Optional[QWidget] = None):
        super().__init__(parent)


class UILogManager:
    """UI日志管理器 - 适配loguru的日志处理器"""
    _instance: Optional["UILogManager"] = None
    _handlers: List[Any] = []

    def __init__(self):
        """初始化 - 单例模式下仅执行一次"""
        pass

    @classmethod
    def get_instance(cls) -> "UILogManager":
        if cls._instance is None:
            cls._instance = cls.__new__(cls)  # pyright: ignore[reportCallIssue,reportGeneralTypeIssues]
        return cls._instance

    def add_handler(self, handler):
        """添加UI处理器"""
        self._handlers.append(handler)

        # 使用新的安装方法
        if hasattr(handler, 'install'):
            handler.install()

    def remove_handler(self, handler):
        """移除UI处理器"""
        if handler in self._handlers:
            self._handlers.remove(handler)

        # 使用新的卸载方法
        if hasattr(handler, 'uninstall'):
            handler.uninstall()


class LogItem:
    """日志项数据结构"""
    def __init__(self, level: str, message: str, record):
        self.level = level
        self.message = message
        self.record = record
        self.formatted_text = ""
        self.timestamp = ""
        self.module = ""
        self.file_info = ""
        self._format_log_record()

    def _format_log_record(self):
        """格式化日志记录"""
        try:
            if isinstance(self.record, dict):
                # loguru record
                record_data = self.record
                time_obj = record_data.get('time', datetime.now())
                if hasattr(time_obj, 'strftime'):
                    self.timestamp = time_obj.strftime('%Y-%m-%d %H:%M:%S.%f')[:-3]
                file_info = record_data.get('file', {})
                filename = getattr(file_info, 'name', 'unknown') if hasattr(file_info, 'name') else str(file_info.get('name', 'unknown'))
                function = record_data.get('function', '')
                line = record_data.get('line', '')
                self.file_info = f"{filename}:{function}:{line}" if all([filename, function, line]) else filename
                self.module = record_data.get('extra', {}).get('module', record_data.get('name', ''))
            else:
                # 标准logging record
                time_obj = getattr(self.record, 'created', datetime.now())
                self.timestamp = time_obj.strftime('%Y-%m-%d %H:%M:%S.%f')[:-3]
                filename = os.path.basename(getattr(self.record, 'filename', 'unknown'))
                function = getattr(self.record, 'funcName', '')
                line = getattr(self.record, 'lineno', '')
                self.file_info = f"{filename}:{function}:{line}" if all([filename, function, line]) else filename
                self.module = getattr(self.record, 'module', getattr(self.record, 'name', 'unknown'))

            self.formatted_text = f"{self.timestamp} | {self.level:8} | {self.file_info} - {self.message}"
        except Exception:
            time_str = datetime.now().strftime('%Y-%m-%d %H:%M:%S')
            self.formatted_text = f"{time_str} | {self.level:8} | - {self.message}"


class LogModel(QAbstractTableModel):
    """日志数据模型"""

    # 定义列角色
    TimestampRole = Qt.ItemDataRole.UserRole + 1
    LevelRole = Qt.ItemDataRole.UserRole + 2
    MessageRole = Qt.ItemDataRole.UserRole + 3
    ModuleRole = Qt.ItemDataRole.UserRole + 4
    FileInfoRole = Qt.ItemDataRole.UserRole + 5

    def __init__(self, parent: Optional[QObject] = None):
        super().__init__(parent)
        self._logs = deque(maxlen=10000)  # 使用循环缓冲区，最多保存10000条日志
        self._filtered_logs: List[LogItem] = []
        self._headers = ["时间", "级别", "模块", "文件", "消息"]

    def rowCount(self, parent=QModelIndex()):
        return len(self._filtered_logs)

    def columnCount(self, parent=QModelIndex()):
        return len(self._headers)

    def data(self, index: QModelIndex, role=Qt.ItemDataRole.DisplayRole):
        if not index.isValid() or index.row() >= len(self._filtered_logs):
            return None

        log_item = self._filtered_logs[index.row()]
        col = index.column()

        if role == Qt.ItemDataRole.DisplayRole:
            if col == 0:
                return log_item.timestamp
            elif col == 1:
                return log_item.level
            elif col == 2:
                return log_item.module
            elif col == 3:
                return log_item.file_info
            elif col == 4:
                return log_item.message
        elif role == Qt.ItemDataRole.ToolTipRole:
            return log_item.formatted_text
        elif role == self.TimestampRole:
            return log_item.timestamp
        elif role == self.LevelRole:
            return log_item.level
        elif role == self.MessageRole:
            return log_item.message
        elif role == self.ModuleRole:
            return log_item.module
        elif role == self.FileInfoRole:
            return log_item.file_info
        elif role == Qt.ItemDataRole.ForegroundRole:
            # 根据日志级别设置颜色
            level_colors = {
                'DEBUG': QColor(100, 100, 100),
                'INFO': QColor(0, 128, 0),
                'WARNING': QColor(255, 140, 0),
                'ERROR': QColor(220, 20, 60),
                'CRITICAL': QColor(139, 0, 0)
            }
            return level_colors.get(log_item.level, QColor(51, 51, 51))

        return None

    def headerData(self, section, orientation, role=Qt.ItemDataRole.DisplayRole):
        if orientation == Qt.Orientation.Horizontal and role == Qt.ItemDataRole.DisplayRole:
            return self._headers[section]
        return None

    def add_log(self, level: str, message: str, record):
        """添加日志"""
        log_item = LogItem(level, message, record)
        self._logs.append(log_item)

        # 如果没有过滤条件，直接添加到显示列表
        if not hasattr(self, '_filter') or self._filter is None:
            self._filtered_logs.append(log_item)
        else:
            # 检查是否通过过滤
            if self._filter(log_item):
                self._filtered_logs.append(log_item)

        # 限制显示的日志数量
        if len(self._filtered_logs) > 1000:
            self._filtered_logs = self._filtered_logs[-1000:]

        # 发出数据变更信号
        self.layoutChanged.emit()

    def set_filter(self, filter_func=None):
        """设置过滤器"""
        self._filter = filter_func
        self._filtered_logs = []

        if filter_func is None:
            # 无过滤，显示所有日志
            self._filtered_logs = list(self._logs)
        else:
            # 应用过滤
            for log_item in self._logs:
                if filter_func(log_item):
                    self._filtered_logs.append(log_item)

        self.layoutChanged.emit()

    def clear(self):
        """清空所有日志"""
        self._logs.clear()
        self._filtered_logs.clear()
        self.layoutChanged.emit()


class LogTableDelegate(QStyledItemDelegate):
    """自定义表格项渲染器"""

    def __init__(self, parent: Optional[QObject] = None):
        super().__init__(parent)
        self.highlight_text = ""

    def set_highlight(self, text: str):
        """设置高亮文本"""
        self.highlight_text = text.lower()

    def paint(self, painter: QPainter, option: QStyleOptionViewItem, index: QModelIndex):
        """绘制表格项"""
        super().paint(painter, option, index)

        # 绘制高亮
        if self.highlight_text:
            text = index.data(Qt.ItemDataRole.DisplayRole)
            if text and self.highlight_text in text.lower():
                # 创建高亮画刷
                highlight_color = QColor(255, 255, 0, 50)  # 半透明黄色
                painter.fillRect(option.rect, highlight_color)

    def sizeHint(self, option: QStyleOptionViewItem, index: QModelIndex):
        """计算项大小"""
        size = super().sizeHint(option, index)
        # 确保有足够的高度显示内容
        size.setHeight(max(size.height(), 24))
        return size


class LogTableView(QTableView):
    """优化的日志表格视图"""

    def __init__(self, parent: Optional[QWidget] = None):
        super().__init__(parent)

        # 设置模型
        self._log_model = LogModel()
        self.setModel(self._log_model)

        # 设置代理
        self.setItemDelegate(LogTableDelegate())

        # 设置表格属性
        self.setAlternatingRowColors(True)
        self.setSortingEnabled(True)
        self.setSelectionBehavior(QTableView.SelectionBehavior.SelectRows)
        self.setSelectionMode(QTableView.SelectionMode.SingleSelection)

        # 设置表头
        header = self.horizontalHeader()
        if header is not None:
            header.setSectionResizeMode(QHeaderView.ResizeMode.Interactive)
            header.setStretchLastSection(True)

        # 设置列宽
        self.setColumnWidth(0, 160)  # 时间
        self.setColumnWidth(1, 80)   # 级别
        self.setColumnWidth(2, 120)  # 模块
        self.setColumnWidth(3, 200)  # 文件
        # 消息列自动拉伸

        # 设置样式（支持深色模式）
        self._update_table_style()

    def _update_table_style(self):
        """更新表格样式以适配当前主题"""
        if isDarkTheme():
            self.setStyleSheet("""
                QTableView {
                    background-color: #2d2d2d;
                    alternate-background-color: #353535;
                    gridline-color: rgba(255, 255, 255, 0.1);
                    selection-background-color: #007bff;
                    selection-color: white;
                    color: #ffffff;
                }
                QTableView::item {
                    padding: 4px;
                    border: none;
                    color: #ffffff;
                }
                QTableView::item:selected {
                    background-color: #007bff;
                    color: white;
                }
                QHeaderView::section {
                    background-color: #1e1e1e;
                    color: #ffffff;
                    padding: 8px;
                    border: none;
                    border-right: 1px solid rgba(255, 255, 255, 0.1);
                    border-bottom: 1px solid rgba(255, 255, 255, 0.1);
                    font-weight: bold;
                }
                QHeaderView::section:vertical {
                    background-color: #1e1e1e;
                    color: #ffffff;
                    border-right: 1px solid rgba(255, 255, 255, 0.1);
                }
            """)
        else:
            self.setStyleSheet("""
                QTableView {
                    background-color: #ffffff;
                    alternate-background-color: rgba(0, 0, 0, 0.03);
                    gridline-color: rgba(0, 0, 0, 0.1);
                    selection-background-color: #007bff;
                    selection-color: white;
                    color: #000000;
                }
                QTableView::item {
                    padding: 4px;
                    border: none;
                    color: #000000;
                }
                QTableView::item:selected {
                    background-color: #007bff;
                    color: white;
                }
                QHeaderView::section {
                    background-color: #f0f0f0;
                    color: #000000;
                    padding: 8px;
                    border: none;
                    border-right: 1px solid rgba(0, 0, 0, 0.1);
                    border-bottom: 1px solid rgba(0, 0, 0, 0.1);
                    font-weight: bold;
                }
                QHeaderView::section:vertical {
                    background-color: #f0f0f0;
                    color: #000000;
                    border-right: 1px solid rgba(0, 0, 0, 0.1);
                }
            """)

    def set_highlight(self, text: str):
        """设置搜索高亮"""
        delegate = self.itemDelegate()
        if isinstance(delegate, LogTableDelegate):
            delegate.set_highlight(text)
            viewport = self.viewport()
            if viewport is not None:
                viewport.update()


class LogDisplayWidget(QWidget):
    """日志显示组件容器"""

    def __init__(self, parent: Optional[QWidget] = None):
        super().__init__(parent)
        self._setup_ui()
        # 保存所有日志记录
        self.all_logs: List[tuple] = []

    def _setup_ui(self):
        """设置UI"""
        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)

        # 创建表格视图
        self.log_table = LogTableView()
        layout.addWidget(self.log_table)

    def append_log(self, level: str, message: str, record):
        """添加日志"""
        # 保存到所有日志列表
        log_item = (level, message, record)
        self.all_logs.append(log_item)

        # 直接添加到模型（模型会根据当前过滤器进行处理）
        self.log_table._log_model.add_log(level, message, record)

    def clear_all(self):
        """清空所有日志"""
        self.all_logs.clear()
        self.log_table._log_model.clear()

    def set_filter(self, filter_dict):
        """设置过滤条件"""
        level_filter = filter_dict.get('level', '全部')

        # 创建过滤器函数
        def filter_func(log_item):
            # 级别过滤
            if level_filter != '全部' and level_filter != log_item.level:
                return False

            # 搜索过滤
            search_text = filter_dict.get('search', '').strip()
            if search_text and search_text.lower() not in log_item.formatted_text.lower():
                return False

            return True

        # 清空模型
        model = self.log_table._log_model
        model.clear()

        # 重新添加所有日志，让过滤器决定是否显示
        for level, message, record in self.all_logs:
            model.add_log(level, message, record)

        # 应用过滤器到模型
        model.set_filter(filter_func)

        # 设置搜索高亮
        search_text = filter_dict.get('search', '').strip()
        self.log_table.set_highlight(search_text if search_text else "")
    
    

class LogFilterWidget(CardWidget):
    """日志过滤控制组件"""

    filter_changed = pyqtSignal(dict)  # 过滤条件改变信号

    def __init__(self, parent: Optional[QWidget] = None):
        super().__init__(parent)
        self.setupUI()
        self.connectSignals()

    def setupUI(self):
        """设置UI"""
        layout = QVBoxLayout(self)
        layout.setContentsMargins(16, 12, 16, 12)
        layout.setSpacing(12)

        # 标题
        title_label = StrongBodyLabel("日志过滤")
        layout.addWidget(title_label)

        # 日志级别过滤
        level_layout = QHBoxLayout()
        level_label = CaptionLabel("日志级别:")
        level_label.setFixedWidth(60)

        self.level_combo = ComboBox()
        self.level_combo.addItems(["全部", "DEBUG", "INFO", "WARNING", "ERROR", "CRITICAL"])
        self.level_combo.setCurrentText("全部")
        self.level_combo.setFixedWidth(120)

        level_layout.addWidget(level_label)
        level_layout.addWidget(self.level_combo)
        level_layout.addStretch()

        # 搜索框
        search_layout = QHBoxLayout()
        search_label = CaptionLabel("搜索:")
        search_label.setFixedWidth(60)

        self.search_edit = LineEdit()
        self.search_edit.setPlaceholderText("输入关键词搜索...")

        search_layout.addWidget(search_label)
        search_layout.addWidget(self.search_edit)

        # 自动滚动开关
        self.auto_scroll_check = CheckBox("自动滚动")
        self.auto_scroll_check.setChecked(True)

        # 添加到布局
        layout.addLayout(level_layout)
        layout.addLayout(search_layout)
        layout.addWidget(self.auto_scroll_check)

    def connectSignals(self):
        """连接信号"""
        self.level_combo.currentTextChanged.connect(self.emit_filter_changed)
        self.search_edit.textChanged.connect(self.emit_filter_changed)
        self.auto_scroll_check.stateChanged.connect(self.emit_filter_changed)

    def emit_filter_changed(self):
        """发射过滤条件改变信号"""
        filter_dict = {
            'level': self.level_combo.currentText(),
            'search': self.search_edit.text(),
            'auto_scroll': self.auto_scroll_check.isChecked()
        }
        self.filter_changed.emit(filter_dict)

    

class LogControlWidget(CardWidget):
    """日志控制组件"""

    clear_logs = pyqtSignal()
    export_logs = pyqtSignal()

    def __init__(self, parent: Optional[QWidget] = None):
        super().__init__(parent)
        self.export_format = "txt"  # 默认导出格式
        self.setupUI()
        self.connectSignals()

    def setupUI(self):
        """设置UI"""
        layout = QVBoxLayout(self)
        layout.setContentsMargins(16, 12, 16, 12)
        layout.setSpacing(12)

        # 标题
        title_label = StrongBodyLabel("日志控制")
        layout.addWidget(title_label)

        # 导出格式选择
        format_layout = QHBoxLayout()
        format_label = CaptionLabel("导出格式:")
        format_label.setFixedWidth(60)

        self.format_combo = ComboBox()
        self.format_combo.addItems(["TXT", "JSON", "CSV"])
        self.format_combo.setFixedWidth(120)
        self.format_combo.setCurrentText("TXT")

        format_layout.addWidget(format_label)
        format_layout.addWidget(self.format_combo)
        format_layout.addStretch()

        # 按钮布局
        buttons_layout = QHBoxLayout()

        # 清空按钮
        self.clear_btn = PushButton("清空")
        self.clear_btn.setIcon(FIF.DELETE)
        self.clear_btn.setFixedWidth(120)

        # 导出按钮
        self.export_btn = PrimaryPushButton("导出")
        self.export_btn.setIcon(FIF.SAVE)
        self.export_btn.setFixedWidth(120)

        buttons_layout.addWidget(self.clear_btn)
        buttons_layout.addWidget(self.export_btn)

        layout.addLayout(format_layout)
        layout.addLayout(buttons_layout)

    def connectSignals(self):
        """连接信号"""
        self.clear_btn.clicked.connect(self.clear_logs.emit)
        self.export_btn.clicked.connect(self.export_logs.emit)
        self.format_combo.currentTextChanged.connect(self.set_export_format)

    def set_export_format(self, format):
        """设置导出格式"""
        self.export_format = format.lower()

    def get_export_format(self):
        """获取导出格式"""
        return self.export_format


class LogUI(QFrame):
    """日志管理界面"""

    def __init__(self, parent: Optional[QWidget] = None):
        super().__init__(parent)
        self.logger = get_logger()

        # 设置对象名（用于导航）
        self.setObjectName('log-ui')
        
        # 防止递归更新样式
        self._updating_styles = False

        self.setupUI()
        self.setupLogHandler()
        self.connectSignals()
        
        # 应用主题样式（设置背景色等）
        self._update_label_styles()
    
    def changeEvent(self, event):
        """监听主题切换事件，更新标签样式"""
        super().changeEvent(event)
        
        # 当调色板改变时（主题切换会触发此事件），更新标签颜色
        if event.type() == QEvent.Type.PaletteChange:
            self._update_label_styles()
    
    def _update_label_styles(self):
        """更新标签样式以适配当前主题"""
        # 防止递归调用
        if self._updating_styles:
            return
        
        self._updating_styles = True
        try:
            from PyQt6.QtGui import QPalette, QColor
            
            # 使用调色板设置背景色（比样式表更可靠）
            palette = self.palette()
            if isDarkTheme():
                palette.setColor(QPalette.ColorRole.Window, QColor("#1e1e1e"))
                palette.setColor(QPalette.ColorRole.Base, QColor("#1e1e1e"))
                # 更新标题标签
                self.title_label.setStyleSheet("color: #ffffff;")
            else:
                palette.setColor(QPalette.ColorRole.Window, QColor("#f5f5f5"))
                palette.setColor(QPalette.ColorRole.Base, QColor("#f5f5f5"))
                # 清除标题标签样式
                self.title_label.setStyleSheet("")
            self.setPalette(palette)
            self.setAutoFillBackground(True)
            
            # 同时设置主布局容器的背景色
            if hasattr(self, 'main_layout'):
                for i in range(self.main_layout.count()):
                    item = self.main_layout.itemAt(i)
                    if item and item.widget():
                        widget = item.widget()
                        widget_palette = widget.palette()
                        if isDarkTheme():
                            widget_palette.setColor(QPalette.ColorRole.Window, QColor("#1e1e1e"))
                            widget_palette.setColor(QPalette.ColorRole.Base, QColor("#1e1e1e"))
                        else:
                            widget_palette.setColor(QPalette.ColorRole.Window, QColor("#f5f5f5"))
                            widget_palette.setColor(QPalette.ColorRole.Base, QColor("#f5f5f5"))
                        widget.setPalette(widget_palette)
                        widget.setAutoFillBackground(True)
            
            # 设置日志表格背景色
            if hasattr(self, 'log_table'):
                table_palette = self.log_table.palette()
                if isDarkTheme():
                    table_palette.setColor(QPalette.ColorRole.Base, QColor("#2d2d2d"))
                    table_palette.setColor(QPalette.ColorRole.AlternateBase, QColor("#353535"))
                    table_palette.setColor(QPalette.ColorRole.Text, QColor("#ffffff"))
                else:
                    table_palette.setColor(QPalette.ColorRole.Base, QColor("#ffffff"))
                    table_palette.setColor(QPalette.ColorRole.AlternateBase, QColor("#f5f5f5"))
                    table_palette.setColor(QPalette.ColorRole.Text, QColor("#000000"))
                self.log_table.setPalette(table_palette)
                self.log_table.setAutoFillBackground(True)
                # 更新表格样式（支持深色模式）
                self.log_table._update_table_style()
                        
        except Exception as e:
            self.logger.warning(f"更新标签样式失败: {e}")
        finally:
            self._updating_styles = False
        
    def setupUI(self):
        """设置UI"""
        layout = QVBoxLayout(self)
        layout.setContentsMargins(20, 20, 20, 20)
        layout.setSpacing(16)
        
        # 标题
        self.title_label = SubtitleLabel("日志管理")
        self.title_label.setFont(QFont("Microsoft YaHei", 18, QFont.Weight.Bold))
        
        # 根据主题设置标签样式
        if isDarkTheme():
            self.title_label.setStyleSheet("color: #ffffff;")
        layout.addWidget(self.title_label)
        
        # 主要内容区域
        content_widget = QWidget()
        content_layout = QHBoxLayout(content_widget)
        content_layout.setContentsMargins(0, 0, 0, 0)
        content_layout.setSpacing(16)
        
        # 左侧控制面板
        left_panel = QWidget()
        left_panel.setFixedWidth(280)
        left_layout = QVBoxLayout(left_panel)
        left_layout.setContentsMargins(0, 0, 0, 0)
        left_layout.setSpacing(12)
        
        # 过滤控件
        self.filter_widget = LogFilterWidget()
        left_layout.addWidget(self.filter_widget)
        
        # 控制按钮
        self.control_widget = LogControlWidget()
        left_layout.addWidget(self.control_widget)
        
        left_layout.addStretch()
        
        # 右侧日志显示区域
        self.log_display = LogDisplayWidget()
        
        content_layout.addWidget(left_panel)
        content_layout.addWidget(self.log_display, 1)
        
        layout.addWidget(content_widget, 1)
    
    def setupLogHandler(self):
        """设置日志处理器 - 只监听logger.py中的日志"""
        # 创建信号发射器 - 必须在主线程中创建
        self.signal_emitter = LogSignalEmitter(self)
        
        # 创建自定义日志处理器
        self.log_handler = LogHandler(self.signal_emitter)
        self.log_handler.setLevel("DEBUG")  # 确保捕获所有级别的日志

        # 设置格式（loguru不需要格式器，保留为兼容性）
        self.log_handler.setFormatter(None)
        
        # 先连接信号，再添加处理器 - 使用QueuedConnection确保线程安全
        self.signal_emitter.log_received.connect(
            self.handle_log_received,
            Qt.ConnectionType.QueuedConnection  # pyright: ignore[reportCallIssue]
        )
        
        # 使用UILogManager添加处理器到loguru系统
        # 获取单例
        self.ui_log_manager = UILogManager.get_instance()
        self.ui_log_manager.add_handler(self.log_handler)
    
    def connectSignals(self):
        """连接信号"""
        # 日志信号已在setupLogHandler中连接
        self.filter_widget.filter_changed.connect(self.apply_filter)
        self.control_widget.clear_logs.connect(self.clear_logs)
        self.control_widget.export_logs.connect(self.export_logs)
    
    def handle_log_received(self, level: str, message: str, record):
        """处理接收到的日志"""
        # 直接添加日志到显示，让LogDisplayWidget处理过滤
        self.log_display.append_log(level, message, record)
    
        
    def apply_filter(self, filter_dict: dict):
        """应用过滤条件"""
        # 直接将过滤条件传递给LogDisplayWidget
        self.log_display.set_filter(filter_dict)
    
    def clear_logs(self):
        """清空日志"""
        reply = QMessageBox.question(
            self,
            "确认清空",
            "确定要清空所有日志吗？",
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
            QMessageBox.StandardButton.No
        )

        if reply == QMessageBox.StandardButton.Yes:
            self.log_display.clear_all()
            InfoBar.success(
                title="清空成功",
                content="所有日志已清空",
                orient=Qt.Orientation.Horizontal,
                isClosable=True,
                position=InfoBarPosition.TOP,
                duration=2000,
                parent=self
            )
    
    
    def export_logs(self):
        """导出日志"""
        format = self.control_widget.get_export_format()

        if format == "json":
            file_filter = "JSON文件 (*.json);;所有文件 (*.*)"
            default_name = f"logs_{datetime.now().strftime('%Y%m%d_%H%M%S')}.json"
        elif format == "csv":
            file_filter = "CSV文件 (*.csv);;所有文件 (*.*)"
            default_name = f"logs_{datetime.now().strftime('%Y%m%d_%H%M%S')}.csv"
        else:
            file_filter = "文本文件 (*.txt);;所有文件 (*.*)"
            default_name = f"logs_{datetime.now().strftime('%Y%m%d_%H%M%S')}.txt"

        file_path, _ = QFileDialog.getSaveFileName(
            self,
            "导出日志",
            default_name,
            file_filter
        )

        if file_path:
            try:
                if format == "json":
                    self._export_json(file_path)
                elif format == "csv":
                    self._export_csv(file_path)
                else:
                    self._export_txt(file_path)

                InfoBar.success(
                    title="导出成功",
                    content=f"日志已导出到: {file_path}",
                    orient=Qt.Orientation.Horizontal,
                    isClosable=True,
                    position=InfoBarPosition.TOP,
                    duration=3000,
                    parent=self
                )
            except Exception as e:
                QMessageBox.critical(self, "导出失败", f"导出日志失败：{str(e)}")

    def _export_txt(self, file_path: str):
        """导出为TXT格式"""
        with open(file_path, 'w', encoding='utf-8') as f:
            # 从LogDisplayWidget获取过滤后的日志
            model = self.log_display.log_table._log_model
            for log_item in model._filtered_logs:
                # 导出完整格式的日志
                formatted_log = f"{log_item.timestamp} | {log_item.level:8} | {log_item.file_info} - {log_item.message}"
                f.write(formatted_log + '\n')

    def _export_json(self, file_path: str):
        """导出为JSON格式"""
        import json
        logs = []

        # 从LogDisplayWidget获取过滤后的日志
        model = self.log_display.log_table._log_model
        for log_item in model._filtered_logs:
            log_data = {
                'timestamp': log_item.timestamp,
                'level': log_item.level,
                'module': log_item.module,
                'message': log_item.message,
                'file_info': log_item.file_info
            }
            logs.append(log_data)

        with open(file_path, 'w', encoding='utf-8') as f:
            json.dump(logs, f, ensure_ascii=False, indent=2)

    def _export_csv(self, file_path: str):
        """导出为CSV格式"""
        import csv
        with open(file_path, 'w', newline='', encoding='utf-8') as f:
            writer = csv.writer(f)
            writer.writerow(['时间', '级别', '模块', '文件', '消息'])

            # 从LogDisplayWidget获取过滤后的日志
            model = self.log_display.log_table._log_model
            for log_item in model._filtered_logs:
                writer.writerow([
                    log_item.timestamp,
                    log_item.level,
                    log_item.module,
                    log_item.file_info,
                    log_item.message
                ])
    
    def closeEvent(self, event):
        """关闭事件"""
        # 从logger.py的logger中移除日志处理器
        self.ui_log_manager.remove_handler(self.log_handler)
        super().closeEvent(event) 