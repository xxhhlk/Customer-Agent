# 设置界面

import json
import os
from PyQt6.QtCore import Qt, pyqtSignal
from PyQt6.QtWidgets import (QFrame, QHBoxLayout, QVBoxLayout, QWidget, QLabel, 
                            QFormLayout, QGroupBox, QMessageBox)
from PyQt6.QtGui import QFont
from qfluentwidgets import (CardWidget, SubtitleLabel, CaptionLabel, BodyLabel, 
                           PrimaryPushButton, PushButton, StrongBodyLabel, 
                           LineEdit, ComboBox, ScrollArea, FluentIcon as FIF,
                           InfoBar, InfoBarPosition, TextEdit, PasswordLineEdit,
                           TimePicker, SpinBox)
from PyQt6.QtCore import QTime
from utils.logger import get_logger
from config import config


class CozeConfigCard(CardWidget):
    """Coze配置卡片"""
    
    def __init__(self, parent=None):
        super().__init__(parent)
        self.setupUI()
    
    def setupUI(self):
        """设置UI"""
        layout = QVBoxLayout(self)
        layout.setContentsMargins(20, 16, 20, 16)
        layout.setSpacing(16)
        
        # 卡片标题
        title_label = StrongBodyLabel("Coze AI 配置")
        title_label.setFont(QFont("Microsoft YaHei", 12, QFont.Weight.Bold))
        layout.addWidget(title_label)
        
        # 表单布局
        form_layout = QFormLayout()
        form_layout.setSpacing(12)
        form_layout.setLabelAlignment(Qt.AlignmentFlag.AlignLeft)
        form_layout.setFieldGrowthPolicy(QFormLayout.FieldGrowthPolicy.ExpandingFieldsGrow)
        form_layout.setFormAlignment(Qt.AlignmentFlag.AlignLeft)
        
        # API Base URL
        self.api_base_edit = LineEdit()
        self.api_base_edit.setPlaceholderText("https://api.coze.cn")
        self.api_base_edit.setText("https://api.coze.cn")
        form_layout.addRow("API Base URL:", self.api_base_edit)
        
        # API Token
        self.api_token_edit = PasswordLineEdit()
        self.api_token_edit.setPlaceholderText("输入您的 Coze API Token")
        form_layout.addRow("API Token:", self.api_token_edit)
        
        # Bot ID
        self.bot_id_edit = LineEdit()
        self.bot_id_edit.setPlaceholderText("输入您的 Bot ID")
        form_layout.addRow("Bot ID:", self.bot_id_edit)
                
        layout.addLayout(form_layout)
        
        # 说明文本
        description_label = CaptionLabel(
            "请在 Coze 平台获取您的 API Token 和 Bot ID。\n"
            "API Token 用于身份验证，Bot ID 用于指定使用的特定机器人。"
        )
        description_label.setStyleSheet("color: #666; padding: 8px 0;")
        layout.addWidget(description_label)
    
    def getConfig(self) -> dict:
        """获取配置"""
        return {
            "coze_api_base": self.api_base_edit.text().strip() or "https://api.coze.cn",
            "coze_token": self.api_token_edit.text().strip(),
            "coze_bot_id": self.bot_id_edit.text().strip()
        }
    
    def setConfig(self, config: dict):
        """设置配置"""
        self.api_base_edit.setText(config.get("coze_api_base", "https://api.coze.cn"))
        self.api_token_edit.setText(config.get("coze_token", ""))
        self.bot_id_edit.setText(config.get("coze_bot_id", ""))


class BusinessHoursCard(CardWidget):
    """业务时间配置卡片"""
    
    def __init__(self, parent=None):
        super().__init__(parent)
        self.setupUI()
    
    def setupUI(self):
        """设置UI"""
        layout = QVBoxLayout(self)
        layout.setContentsMargins(20, 16, 20, 16)
        layout.setSpacing(16)
        
        # 卡片标题
        title_label = StrongBodyLabel("业务时间设置")
        title_label.setFont(QFont("Microsoft YaHei", 12, QFont.Weight.Bold))
        layout.addWidget(title_label)
        
        # 表单布局
        form_layout = QFormLayout()
        form_layout.setSpacing(12)
        form_layout.setLabelAlignment(Qt.AlignmentFlag.AlignLeft)
        form_layout.setFieldGrowthPolicy(QFormLayout.FieldGrowthPolicy.ExpandingFieldsGrow)
        form_layout.setFormAlignment(Qt.AlignmentFlag.AlignLeft)
        
        # 开始时间
        self.start_time_picker = TimePicker()
        self.start_time_picker.setTime(QTime(8, 0))  # 默认8:00
        form_layout.addRow("开始时间:", self.start_time_picker)
        
        # 结束时间
        self.end_time_picker = TimePicker()
        self.end_time_picker.setTime(QTime(23, 0))  # 默认23:00
        form_layout.addRow("结束时间:", self.end_time_picker)
        
        layout.addLayout(form_layout)
        
        # 说明文本
        description_label = CaptionLabel(
            "设置AI客服的工作时间。在工作时间内，系统将自动响应客户消息。\n"
            "在非工作时间，系统将不会自动回复。"
        )
        description_label.setStyleSheet("color: #666; padding: 8px 0;")
        layout.addWidget(description_label)
    
    def getConfig(self) -> dict:
        """获取配置"""
        return {
            "businessHours": {
                "start": self.start_time_picker.getTime().toString("HH:mm"),
                "end": self.end_time_picker.getTime().toString("HH:mm")
            }
        }
    
    def setConfig(self, config: dict):
        """设置配置"""
        business_hours = config.get("businessHours", {})
        
        # 解析开始时间
        start_time_str = business_hours.get("start", "08:00")
        start_time = QTime.fromString(start_time_str, "HH:mm")
        if start_time.isValid():
            self.start_time_picker.setTime(start_time)
        
        # 解析结束时间
        end_time_str = business_hours.get("end", "23:00")
        end_time = QTime.fromString(end_time_str, "HH:mm")
        if end_time.isValid():
            self.end_time_picker.setTime(end_time)


class RateLimitCard(CardWidget):
    """限流配置卡片 - Coze API 请求频率限制"""

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setupUI()

    def setupUI(self):
        """设置UI"""
        layout = QVBoxLayout(self)
        layout.setContentsMargins(20, 16, 20, 16)
        layout.setSpacing(16)

        # 卡片标题
        title_label = StrongBodyLabel("AI 请求限流")
        title_label.setFont(QFont("Microsoft YaHei", 12, QFont.Weight.Bold))
        layout.addWidget(title_label)

        # 表单布局
        form_layout = QFormLayout()
        form_layout.setSpacing(12)
        form_layout.setLabelAlignment(Qt.AlignmentFlag.AlignLeft)
        form_layout.setFieldGrowthPolicy(QFormLayout.FieldGrowthPolicy.ExpandingFieldsGrow)
        form_layout.setFormAlignment(Qt.AlignmentFlag.AlignLeft)

        # 窗口时长（小时）
        self.window_hours_spin = SpinBox()
        self.window_hours_spin.setRange(1, 168)  # 1小时 ~ 7天
        self.window_hours_spin.setValue(4)
        self.window_hours_spin.setSuffix(" 小时")
        form_layout.addRow("窗口时长:", self.window_hours_spin)

        # 最大请求数
        self.max_requests_spin = SpinBox()
        self.max_requests_spin.setRange(1, 1000)
        self.max_requests_spin.setValue(10)
        self.max_requests_spin.setSuffix(" 次")
        form_layout.addRow("最大请求数:", self.max_requests_spin)

        # 兜底回复
        self.fallback_reply_edit = TextEdit()
        self.fallback_reply_edit.setPlaceholderText("输入限流后的兜底回复内容")
        self.fallback_reply_edit.setFixedHeight(80)
        self.fallback_reply_edit.setPlainText("这个我不了解呢，帮你问下我们的技术人员")
        form_layout.addRow("兜底回复:", self.fallback_reply_edit)

        layout.addLayout(form_layout)

        # 说明文本
        description_label = CaptionLabel(
            "每个买家在指定时间窗口内最多发送指定次数的 AI 请求，\n"
            "超出限制后将自动回复兜底内容。窗口从买家第一次请求开始计时，\n"
            "到期后自动重置。按买家ID全局计数，跨店铺共享。"
        )
        description_label.setStyleSheet("color: #666; padding: 8px 0;")
        description_label.setWordWrap(True)
        layout.addWidget(description_label)

    def getConfig(self) -> dict:
        """获取配置"""
        return {
            "rate_limit": {
                "window_hours": self.window_hours_spin.value(),
                "max_requests": self.max_requests_spin.value(),
                "fallback_reply": self.fallback_reply_edit.toPlainText().strip() or "这个我不了解呢，帮你问下我们的技术人员",
            }
        }

    def setConfig(self, config: dict):
        """设置配置"""
        rate_limit = config.get("rate_limit", {})
        self.window_hours_spin.setValue(rate_limit.get("window_hours", 4))
        self.max_requests_spin.setValue(rate_limit.get("max_requests", 10))
        self.fallback_reply_edit.setPlainText(
            rate_limit.get("fallback_reply", "这个我不了解呢，帮你问下我们的技术人员")
        )


class StaffReplyWaitCard(CardWidget):
    """人工客服优先回复配置卡片"""

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setupUI()

    def setupUI(self):
        """设置UI"""
        from qfluentwidgets import CheckBox
        
        layout = QVBoxLayout(self)
        layout.setContentsMargins(20, 16, 20, 16)
        layout.setSpacing(16)

        # 卡片标题
        title_label = StrongBodyLabel("人工客服优先回复")
        title_label.setFont(QFont("Microsoft YaHei", 12, QFont.Weight.Bold))
        layout.addWidget(title_label)

        # 表单布局
        form_layout = QFormLayout()
        form_layout.setSpacing(12)
        form_layout.setLabelAlignment(Qt.AlignmentFlag.AlignLeft)
        form_layout.setFieldGrowthPolicy(QFormLayout.FieldGrowthPolicy.ExpandingFieldsGrow)
        form_layout.setFormAlignment(Qt.AlignmentFlag.AlignLeft)

        # 启用开关
        self.enable_checkbox = CheckBox("启用人工客服优先回复")
        self.enable_checkbox.setChecked(True)
        form_layout.addRow("功能开关:", self.enable_checkbox)

        # 等待时间（秒）
        self.wait_seconds_spin = SpinBox()
        self.wait_seconds_spin.setRange(5, 300)  # 5秒 ~ 5分钟
        self.wait_seconds_spin.setValue(30)
        self.wait_seconds_spin.setSuffix(" 秒")
        form_layout.addRow("等待时间:", self.wait_seconds_spin)

        layout.addLayout(form_layout)

        # 说明文本
        description_label = CaptionLabel(
            "在白天工作时间段，收到客户消息后先等待人工客服回复。\n"
            "如果人工客服在指定时间内回复，则不触发AI自动回复。\n"
            "如果超时未回复，则继续交给AI处理（AI超时时间会扣除等待时间）。\n"
            "夜间时段不启用此功能。"
        )
        description_label.setStyleSheet("color: #666; padding: 8px 0;")
        description_label.setWordWrap(True)
        layout.addWidget(description_label)

    def getConfig(self) -> dict:
        """获取配置"""
        return {
            "staff_reply_wait": {
                "enable": self.enable_checkbox.isChecked(),
                "wait_seconds": self.wait_seconds_spin.value(),
            }
        }

    def setConfig(self, config: dict):
        """设置配置"""
        staff_wait = config.get("staff_reply_wait", {})
        self.enable_checkbox.setChecked(staff_wait.get("enable", True))
        self.wait_seconds_spin.setValue(staff_wait.get("wait_seconds", 30))


class SettingUI(QFrame):
    """设置界面"""
    
    def __init__(self, parent=None):
        super().__init__(parent=parent)
        self.logger = get_logger("SettingUI")
        self.setupUI()
        self.loadConfig()
        
        # 设置对象名
        self.setObjectName("设置")
    
    def setupUI(self):
        """设置主界面UI"""
        # 主布局
        main_layout = QVBoxLayout(self)
        main_layout.setContentsMargins(30, 30, 30, 30)
        main_layout.setSpacing(25)
        
        # 创建头部区域
        header_widget = self.createHeaderWidget()
        
        # 创建内容区域
        content_widget = self.createContentWidget()
        
        # 连接按钮信号
        self.save_btn.clicked.connect(self.onSaveConfig)
        self.reset_btn.clicked.connect(self.onResetConfig)
        
        # 添加到主布局
        main_layout.addWidget(header_widget)
        main_layout.addWidget(content_widget, 1)
    
    def createHeaderWidget(self):
        """创建头部区域"""
        header_widget = QWidget()
        header_layout = QHBoxLayout(header_widget)
        header_layout.setContentsMargins(0, 0, 0, 0)
        header_layout.setSpacing(20)
        
        # 标题
        title_label = SubtitleLabel("系统设置")
        title_label.setFont(QFont("Microsoft YaHei", 18, QFont.Weight.Bold))
        
        # 描述
        description_label = CaptionLabel("配置AI客服的基本参数和工作时间")
        description_label.setStyleSheet("color: #666;")
        
        # 左侧标题区域
        title_area = QWidget()
        title_layout = QVBoxLayout(title_area)
        title_layout.setContentsMargins(0, 0, 0, 0)
        title_layout.setSpacing(5)
        title_layout.addWidget(title_label)
        title_layout.addWidget(description_label)
        
        # 按钮区域
        buttons_widget = QWidget()
        buttons_layout = QHBoxLayout(buttons_widget)
        buttons_layout.setContentsMargins(0, 0, 0, 0)
        buttons_layout.setSpacing(10)
        
        # 重置按钮
        self.reset_btn = PushButton("重置")
        self.reset_btn.setIcon(FIF.UPDATE)
        self.reset_btn.setFixedSize(80, 40)
        
        # 保存按钮
        self.save_btn = PrimaryPushButton("保存")
        self.save_btn.setIcon(FIF.SAVE)
        self.save_btn.setFixedSize(100, 40)
        
        buttons_layout.addWidget(self.reset_btn)
        buttons_layout.addWidget(self.save_btn)
        
        # 添加到头部布局
        header_layout.addWidget(title_area)
        header_layout.addStretch()
        header_layout.addWidget(buttons_widget)
        
        return header_widget
    
    def createContentWidget(self):
        """创建内容区域"""
        # 滚动区域
        scroll_area = ScrollArea()
        scroll_area.setWidgetResizable(True)
        scroll_area.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        scroll_area.setVerticalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAsNeeded)
        
        # 去除边框
        scroll_area.setStyleSheet("""
            ScrollArea {
                border: none;
                background-color: transparent;
            }
        """)
        
        # 内容容器
        content_container = QWidget()
        content_layout = QVBoxLayout(content_container)
        content_layout.setSpacing(20)
        content_layout.setContentsMargins(20, 20, 20, 20)
        content_layout.setAlignment(Qt.AlignmentFlag.AlignTop)
        
        # 创建配置卡片
        self.coze_config_card = CozeConfigCard()
        self.rate_limit_card = RateLimitCard()
        self.staff_reply_wait_card = StaffReplyWaitCard()
        self.business_hours_card = BusinessHoursCard()
        
        # 添加到布局
        content_layout.addWidget(self.coze_config_card)
        content_layout.addWidget(self.rate_limit_card)
        content_layout.addWidget(self.staff_reply_wait_card)
        content_layout.addWidget(self.business_hours_card)
        content_layout.addStretch()
        
        # 设置容器样式
        content_container.setStyleSheet("""
            QWidget {
                background-color: transparent;
                border: none;
            }
        """)
        
        scroll_area.setWidget(content_container)
        
        return scroll_area
    
    def loadConfig(self):
        """从config模块加载配置"""
        try:            
            loaded_config = {
                "coze_api_base": config.get("coze_api_base", "https://api.coze.cn"),
                "coze_token": config.get("coze_token", ""),
                "coze_bot_id": config.get("coze_bot_id", ""),
                "businessHours": config.get("businessHours", {"start": "08:00", "end": "23:00"}),
                "rate_limit": config.get("rate_limit", {
                    "window_hours": 4,
                    "max_requests": 10,
                    "fallback_reply": "这个我不了解呢，帮你问下我们的技术人员"
                })
            }
            
            # 验证并设置配置
            self._validateAndSetConfig(loaded_config)
            self.logger.info("配置加载成功")
            
        except Exception as e:
            self.logger.error(f"加载配置失败: {e}")
            QMessageBox.warning(self, "加载失败", f"加载配置失败：{str(e)}")
            self._loadDefaultConfig()
    
    def _loadDefaultConfig(self):
        """加载默认配置"""
        default_config = {
            "coze_api_base": "https://api.coze.cn",
            "coze_token": "",
            "coze_bot_id": "",
            "businessHours": {
                "start": "08:00",
                "end": "23:00"
            },
            "rate_limit": {
                "window_hours": 4,
                "max_requests": 10,
                "fallback_reply": "这个我不了解呢，帮你问下我们的技术人员"
            }
        }
        
        self.coze_config_card.setConfig(default_config)
        self.rate_limit_card.setConfig(default_config)
        self.staff_reply_wait_card.setConfig(default_config)
        self.business_hours_card.setConfig(default_config)
        self.logger.info("已加载默认配置")
    
    def _validateAndSetConfig(self, config_data):
        """验证并设置配置"""
        # 确保必要的字段存在
        validated_config = {
            "coze_api_base": config_data.get("coze_api_base", "https://api.coze.cn"),
            "coze_token": config_data.get("coze_token", ""),
            "coze_bot_id": config_data.get("coze_bot_id", ""),
            "businessHours": config_data.get("businessHours", {"start": "08:00", "end": "23:00"}),
            "rate_limit": config_data.get("rate_limit", {
                "window_hours": 4,
                "max_requests": 10,
                "fallback_reply": "这个我不了解呢，帮你问下我们的技术人员"
            })
        }
        
        # 验证businessHours格式
        business_hours = validated_config["businessHours"]
        if not isinstance(business_hours, dict):
            business_hours = {"start": "08:00", "end": "23:00"}
            validated_config["businessHours"] = business_hours
        
        if "start" not in business_hours:
            business_hours["start"] = "08:00"
        if "end" not in business_hours:
            business_hours["end"] = "23:00"
        
        # 验证rate_limit格式
        rate_limit = validated_config["rate_limit"]
        if not isinstance(rate_limit, dict):
            rate_limit = {"window_hours": 4, "max_requests": 10, "fallback_reply": "这个我不了解呢，帮你问下我们的技术人员"}
            validated_config["rate_limit"] = rate_limit
        
        rate_limit.setdefault("window_hours", 4)
        rate_limit.setdefault("max_requests", 10)
        rate_limit.setdefault("fallback_reply", "这个我不了解呢，帮你问下我们的技术人员")
        
        # 设置到界面
        self.coze_config_card.setConfig(validated_config)
        self.rate_limit_card.setConfig(validated_config)
        self.staff_reply_wait_card.setConfig(validated_config)
        self.business_hours_card.setConfig(validated_config)
    
    def onSaveConfig(self):
        """保存配置到config模块"""
        try:
            # 获取配置
            coze_config = self.coze_config_card.getConfig()
            business_config = self.business_hours_card.getConfig()
            rate_limit_config = self.rate_limit_card.getConfig()
            staff_wait_config = self.staff_reply_wait_card.getConfig()
            
            # 合并配置
            new_config = {**coze_config, **business_config, **rate_limit_config, **staff_wait_config}
            
            # 验证必填项
            if not new_config.get("coze_token"):
                QMessageBox.warning(self, "配置错误", "请输入 Coze API Token！")
                return
            
            if not new_config.get("coze_bot_id"):
                QMessageBox.warning(self, "配置错误", "请输入 Bot ID！")
                return
            
            # 验证时间设置
            start_time = self.business_hours_card.start_time_picker.getTime()
            end_time = self.business_hours_card.end_time_picker.getTime()
            
            if start_time >= end_time:
                QMessageBox.warning(self, "时间设置错误", "开始时间必须早于结束时间！")
                return
            
            # 验证限流配置
            rate_limit = rate_limit_config.get("rate_limit", {})
            if not rate_limit.get("fallback_reply"):
                QMessageBox.warning(self, "配置错误", "请输入限流兜底回复内容！")
                return
            
            # 使用config模块保存配置
            config.update(new_config, save=True)
            
            # 热更新限流器配置
            try:
                from Message.rate_limiter import coze_rate_limiter
                coze_rate_limiter.configure(
                    window_size=rate_limit['window_hours'] * 3600,
                    max_requests=rate_limit['max_requests']
                )
                self.logger.info("限流器配置已热更新")
            except Exception as e:
                self.logger.error(f"限流器热更新失败: {e}")
            
            self.logger.info("配置保存成功")
            
            # 显示成功消息
            InfoBar.success(
                title="保存成功",
                content="配置已保存！",
                orient=Qt.Orientation.Horizontal,
                isClosable=True,
                position=InfoBarPosition.TOP,
                duration=2000,
                parent=self
            )
            
        except Exception as e:
            self.logger.error(f"保存配置失败: {e}")
            QMessageBox.critical(self, "保存失败", f"保存配置时发生错误：{str(e)}")
    
    def onResetConfig(self):
        """重置配置"""
        reply = QMessageBox.question(
            self,
            "确认重置",
            "确定要重置所有配置吗？\n这将重新加载配置文件中的原始设置。",
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
            QMessageBox.StandardButton.No
        )
        
        if reply == QMessageBox.StandardButton.Yes:
            try:
                # 使用config模块重新加载配置文件
                config.reload()
                self.loadConfig()
                self.logger.info("配置已重置")
                
                InfoBar.success(
                    title="重置成功",
                    content="配置已重置为配置文件中的设置！",
                    orient=Qt.Orientation.Horizontal,
                    isClosable=True,
                    position=InfoBarPosition.TOP,
                    duration=2000,
                    parent=self
                )
            except Exception as e:
                self.logger.error(f"重置配置失败: {e}")
                QMessageBox.critical(self, "重置失败", f"重置配置失败：{str(e)}")
    
 