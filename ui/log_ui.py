#!/usr/bin/env python
# -*- coding: utf-8 -*-

"""
日志管理界面
"""

import os
import sys
import logging
from datetime import datetime
from typing import List, Dict, Any, Optional
from PyQt6.QtCore import Qt, QTimer, pyqtSignal, QThread
from PyQt6.QtWidgets import (QFrame, QVBoxLayout, QHBoxLayout, QWidget, 
                            QTextEdit, QFileDialog, QMessageBox, QSplitter)
from PyQt6.QtGui import QFont, QTextCursor, QColor, QTextCharFormat
from qfluentwidgets import (CardWidget, SubtitleLabel, CaptionLabel, BodyLabel, 
                           PrimaryPushButton, PushButton, StrongBodyLabel, 
                           ComboBox, LineEdit, ScrollArea, FluentIcon as FIF,
                           InfoBar, InfoBarPosition, ToolButton, CheckBox)
from utils.logger import get_logger, logger


class LogHandler(logging.Handler):
    """自定义日志处理器，用于捕获日志并发送信号"""
    
    def __init__(self, signal_emitter):
        super().__init__()
        self.signal_emitter = signal_emitter
    
    def emit(self, record):
        try:
            msg = self.format(record)
            self.signal_emitter.log_received.emit(record.levelname, msg, record)
        except Exception as e:
            pass


class LogSignalEmitter(QWidget):
    """日志信号发射器"""
    log_received = pyqtSignal(str, str, logging.LogRecord)  # level, message, record


class UILogManager:
    """UI日志管理器 - 只管理来自logger.py的日志处理器"""
    _instance = None
    _handlers = []
    
    def __new__(cls):
        if cls._instance is None:
            cls._instance = super().__new__(cls)
        return cls._instance
    
    def add_handler(self, handler):
        """添加UI处理器到logger.py中的特定logger"""
        self._handlers.append(handler)
        
        # 只添加到logger.py中的全局logger对象
        logger.addHandler(handler)
    
    def remove_handler(self, handler):
        """从logger.py的logger中移除UI处理器"""
        if handler in self._handlers:
            self._handlers.remove(handler)
        
        # 从logger.py的logger移除
        if handler in logger.handlers:
            logger.removeHandler(handler)


class LogDisplayWidget(QTextEdit):
    """日志显示组件"""
    
    def __init__(self, parent=None):
        super().__init__(parent)
        self.setReadOnly(True)
        self.setFont(QFont("Consolas", 10))
        
        # 设置样式
        self.setStyleSheet("""
            QTextEdit {
                border: 1px solid rgba(255, 255, 255, 0.1);
                border-radius: 6px;
                padding: 8px;
            }
        """)
        
        # 日志级别颜色配置 - 适配深浅色模式
        self.level_colors = {
            'DEBUG': QColor(150, 150, 150),     # 灰色
            'INFO': QColor(0, 180, 0),          # 绿色
            'WARNING': QColor(255, 180, 0),     # 橙色
            'ERROR': QColor(255, 80, 80),       # 红色
            'CRITICAL': QColor(255, 50, 50)     # 亮红色
        }
        
        # 最大显示行数
        self.max_lines = 1000
        self.current_lines = 0
    
    def append_log(self, level: str, message: str, record: logging.LogRecord):
        """添加日志条目"""
        
        # 检查行数限制
        if self.current_lines >= self.max_lines:
            self.clear_old_lines()
        
        # 创建格式化文本
        cursor = self.textCursor()
        cursor.movePosition(QTextCursor.MoveOperation.End)
        
        # 设置颜色格式
        format = QTextCharFormat()
        format.setForeground(self.level_colors.get(level, QColor(51, 51, 51)))
        
        cursor.setCharFormat(format)
        cursor.insertText(message + "\n")
        
        # 自动滚动到底部
        self.setTextCursor(cursor)
        self.ensureCursorVisible()
        
        self.current_lines += 1
    
    def clear_old_lines(self):
        """清除旧的日志行"""
        # 保留最后500行
        keep_lines = 500
        text = self.toPlainText()
        lines = text.split('\n')
        
        if len(lines) > keep_lines:
            new_text = '\n'.join(lines[-keep_lines:])
            self.setPlainText(new_text)
            self.current_lines = keep_lines
    
    def clear_all(self):
        """清空所有日志"""
        self.clear()
        self.current_lines = 0


class LogFilterWidget(CardWidget):
    """日志过滤控制组件"""
    
    filter_changed = pyqtSignal(dict)  # 过滤条件改变信号
    
    def __init__(self, parent=None):
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
    
    def __init__(self, parent=None):
        super().__init__(parent)
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
        
        layout.addLayout(buttons_layout)
    
    def connectSignals(self):
        """连接信号"""
        self.clear_btn.clicked.connect(self.clear_logs.emit)
        self.export_btn.clicked.connect(self.export_logs.emit)


class LogUI(QFrame):
    """日志管理界面"""
    
    def __init__(self, parent=None):
        super().__init__(parent)
        self.logger = get_logger()
        self.log_records: List[logging.LogRecord] = []
        self.filtered_records: List[logging.LogRecord] = []
        self.current_filter = {}
        
        # 设置对象名（用于导航）
        self.setObjectName('log-ui')
        
        self.setupUI()
        self.setupLogHandler()
        self.connectSignals()
        
    def setupUI(self):
        """设置UI"""
        layout = QVBoxLayout(self)
        layout.setContentsMargins(20, 20, 20, 20)
        layout.setSpacing(16)
        
        # 标题
        title_label = SubtitleLabel("日志管理")
        title_label.setFont(QFont("Microsoft YaHei", 18, QFont.Weight.Bold))
        layout.addWidget(title_label)
        
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
        self.log_handler.setLevel(logging.DEBUG)  # 确保捕获所有级别的日志
        
        # 设置格式
        formatter = logging.Formatter(
            '%(asctime)s - %(levelname)s - %(filename)s:%(lineno)d - %(message)s'
        )
        self.log_handler.setFormatter(formatter)
        
        # 先连接信号，再添加处理器 - 使用QueuedConnection确保线程安全
        self.signal_emitter.log_received.connect(
            self.handle_log_received, 
            Qt.ConnectionType.QueuedConnection
        )
        
        # 使用UILogManager添加处理器到logger.py的logger
        self.ui_log_manager = UILogManager()
        self.ui_log_manager.add_handler(self.log_handler)
    
    def connectSignals(self):
        """连接信号"""
        # 日志信号已在setupLogHandler中连接
        self.filter_widget.filter_changed.connect(self.apply_filter)
        self.control_widget.clear_logs.connect(self.clear_logs)
        self.control_widget.export_logs.connect(self.export_logs)
    
    def handle_log_received(self, level: str, message: str, record: logging.LogRecord):
        """处理接收到的日志"""
        
        # 添加到记录列表
        self.log_records.append(record)
        
        # 检查是否通过过滤
        if self.passes_filter(record):
            self.log_display.append_log(level, message, record)
        
        # 限制记录数量
        if len(self.log_records) > 5000:
            self.log_records = self.log_records[-3000:]
    
    def passes_filter(self, record: logging.LogRecord) -> bool:
        """检查日志记录是否通过过滤器"""
        # 级别过滤
        if self.current_filter.get('level', '全部') != '全部':
            if record.levelname != self.current_filter['level']:
                return False
        
        # 搜索过滤
        search_text = self.current_filter.get('search', '').strip().lower()
        if search_text:
            message = self.log_handler.format(record).lower()
            if search_text not in message:
                return False
        
        return True
    
    def apply_filter(self, filter_dict: dict):
        """应用过滤条件"""
        self.current_filter = filter_dict
        
        # 重新显示所有符合条件的日志
        self.log_display.clear_all()
        
        for record in self.log_records:
            if self.passes_filter(record):
                message = self.log_handler.format(record)
                self.log_display.append_log(record.levelname, message, record)
    
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
            self.log_records.clear()
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
        file_path, _ = QFileDialog.getSaveFileName(
            self,
            "导出日志",
            f"logs_{datetime.now().strftime('%Y%m%d_%H%M%S')}.txt",
            "文本文件 (*.txt);;所有文件 (*.*)"
        )
        
        if file_path:
            try:
                with open(file_path, 'w', encoding='utf-8') as f:
                    for record in self.log_records:
                        if self.passes_filter(record):
                            message = self.log_handler.format(record)
                            f.write(message + '\n')
                
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
    
    def closeEvent(self, event):
        """关闭事件"""
        # 从logger.py的logger中移除日志处理器
        self.ui_log_manager.remove_handler(self.log_handler)
        super().closeEvent(event) 