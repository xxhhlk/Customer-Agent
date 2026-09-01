# 设置界面

import json
import os
import time
from typing import Optional
from PyQt6.QtCore import Qt, pyqtSignal, QEvent, QTimer, QThread
from PyQt6.QtWidgets import (QFrame, QHBoxLayout, QVBoxLayout, QWidget, QLabel,
                            QFormLayout, QGroupBox)
from PyQt6.QtWidgets import QDialog
from PyQt6.QtGui import QFont
from qfluentwidgets import (CardWidget, SubtitleLabel, CaptionLabel, BodyLabel,
                           PrimaryPushButton, PushButton, StrongBodyLabel,
                           LineEdit, ComboBox, ScrollArea, FluentIcon as FIF,
                           InfoBar, InfoBarPosition, MessageBox, TextEdit, PasswordLineEdit,
                           TimePicker, SpinBox, isDarkTheme)
from PyQt6.QtCore import QTime
from utils.logger_loguru import get_logger
from config import config




class LLMConfigCard(CardWidget):
    """LLM配置卡片"""

    def __init__(self, parent: Optional[QWidget] = None):
        super().__init__(parent)
        self.setupUI()

    def setupUI(self) -> None:
        """设置UI"""
        layout = QVBoxLayout(self)
        layout.setContentsMargins(20, 16, 20, 16)
        layout.setSpacing(16)

        # 卡片标题
        title_label = StrongBodyLabel("LLM模型配置")
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
        self.api_base_edit.setPlaceholderText("https://ark.cn-beijing.volces.com/api/v3")
        self.api_base_edit.setText("https://ark.cn-beijing.volces.com/api/v3")
        form_layout.addRow("API Base URL:", self.api_base_edit)

        # API Key
        self.api_key_edit = PasswordLineEdit()
        self.api_key_edit.setPlaceholderText("输入您的 API Key")
        form_layout.addRow("API Key:", self.api_key_edit)

        # Model Name
        self.model_name_edit = LineEdit()
        self.model_name_edit.setPlaceholderText("输入模型名称，如：doubao-seed-1-6-flash-250828")
        form_layout.addRow("模型名称:", self.model_name_edit)

        # 深度思考模式
        self.thinking_combo = ComboBox()
        self.thinking_combo.addItems(["禁用", "自动", "启用"])
        self.thinking_combo.setCurrentIndex(0)  # 默认禁用
        self.thinking_combo.setToolTip(
            "深度思考模式配置：\n"
            "• 禁用：不使用深度思考，响应更快\n"
            "• 自动：模型自动判断是否需要深度思考\n"
            "• 启用：强制启用深度思考，适合复杂问题"
        )
        form_layout.addRow("深度思考:", self.thinking_combo)

        # 思考强度（reasoning_effort）
        self.effort_combo = ComboBox()
        self.effort_combo.addItems(["自动", "关闭思考", "低", "中", "高", "最高"])
        self.effort_combo.setCurrentIndex(0)  # 默认自动
        self.effort_combo.setToolTip(
            "思考强度（reasoning_effort）配置：\n"
            "• 自动：不传该参数，由模型使用默认思考强度\n"
            "• 关闭思考：直接回答，速度最快\n"
            "• 低 / 中 / 高：调节思维链长度，平衡效果与速度\n"
            "• 最高：最强推理，适合高难度问题，耗时最长\n"
            "注意：仅支持该参数的模型生效（doubao-seed 2.x/1.8、deepseek-v4、glm-5-2 等），\n"
            "不支持的模型会自动忽略此设置，不影响正常回复。"
        )
        form_layout.addRow("思考强度:", self.effort_combo)

        # 图片/视频传 AI 识别开关
        from qfluentwidgets import SwitchButton
        self.send_image_switch = SwitchButton("开启")
        self.send_image_switch.setChecked(True)
        self.send_image_switch.setToolTip(
            "开启后，买家图片/视频消息传给视觉大模型识别。\n"
            "图片 URL 直传优先，失败自动下载转 base64 重试一次。\n"
            "视频抽帧率在 config.json 的 llm.video_fps 配置（默认 1）。\n"
            "非视觉模型（如 DeepSeek）建议关闭。"
        )
        form_layout.addRow("图片/视频传AI识别:", self.send_image_switch)

        layout.addLayout(form_layout)

        # 说明文本
        description_label = CaptionLabel(
            "配置LLM模型的连接参数。\n"
            "支持OpenAI兼容的API接口，包括豆包、通义千问等模型。"
        )
        description_label.setStyleSheet("padding: 8px 0;")
        layout.addWidget(description_label)

    def getConfig(self) -> dict:
        """获取配置"""
        # 映射下拉框索引到 thinking type
        thinking_map = {0: "disabled", 1: "auto", 2: "enabled"}
        thinking_type = thinking_map.get(self.thinking_combo.currentIndex(), "disabled")

        # 映射下拉框索引到 reasoning_effort（自动 -> 空字符串，不传该参数）
        effort_map = {0: "", 1: "minimal", 2: "low", 3: "medium", 4: "high", 5: "max"}
        reasoning_effort = effort_map.get(self.effort_combo.currentIndex(), "")

        return {
            "api_base": self.api_base_edit.text().strip() or "https://ark.cn-beijing.volces.com/api/v3",
            "api_key": self.api_key_edit.text().strip(),
            "model_name": self.model_name_edit.text().strip(),
            "thinking": {
                "type": thinking_type
            },
            "reasoning_effort": reasoning_effort,
            "send_image_to_ai": self.send_image_switch.isChecked()
        }

    def setConfig(self, config: dict):
        """设置配置"""
        self.api_base_edit.setText(config.get("api_base", "https://ark.cn-beijing.volces.com/api/v3"))
        self.api_key_edit.setText(config.get("api_key", ""))
        self.model_name_edit.setText(config.get("model_name", ""))

        # 设置深度思考模式
        thinking_config = config.get("thinking", {})
        thinking_type = thinking_config.get("type", "disabled")
        thinking_map = {"disabled": 0, "auto": 1, "enabled": 2}
        self.thinking_combo.setCurrentIndex(thinking_map.get(thinking_type, 0))

        # 设置思考强度
        reasoning_effort = config.get("reasoning_effort", "")
        effort_map = {"": 0, "minimal": 1, "low": 2, "medium": 3, "high": 4, "max": 5}
        self.effort_combo.setCurrentIndex(effort_map.get(reasoning_effort, 0))

        # 设置图片传 AI 识别开关
        self.send_image_switch.setChecked(config.get("send_image_to_ai", True))


class EmbedderConfigCard(CardWidget):
    """嵌入器配置卡片"""

    def __init__(self, parent: Optional[QWidget] = None):
        super().__init__(parent)
        self.setupUI()

    def setupUI(self) -> None:
        """设置UI"""
        layout = QVBoxLayout(self)
        layout.setContentsMargins(20, 16, 20, 16)
        layout.setSpacing(16)

        # 卡片标题
        title_label = StrongBodyLabel("嵌入模型配置")
        title_label.setFont(QFont("Microsoft YaHei", 12, QFont.Weight.Bold))
        layout.addWidget(title_label)

        # 表单布局
        form_layout = QFormLayout()
        form_layout.setSpacing(12)
        form_layout.setLabelAlignment(Qt.AlignmentFlag.AlignLeft)
        form_layout.setFieldGrowthPolicy(QFormLayout.FieldGrowthPolicy.ExpandingFieldsGrow)
        form_layout.setFormAlignment(Qt.AlignmentFlag.AlignLeft)

        # Embedder API Base URL
        self.api_base_edit = LineEdit()
        self.api_base_edit.setPlaceholderText("https://ark.cn-beijing.volces.com/api/v3")
        form_layout.addRow("API Base URL:", self.api_base_edit)

        # Embedder API Key
        self.api_key_edit = PasswordLineEdit()
        self.api_key_edit.setPlaceholderText("输入嵌入模型的 API Key（可选）")
        form_layout.addRow("API Key:", self.api_key_edit)

        # Embedder Model Name
        self.model_name_edit = LineEdit()
        self.model_name_edit.setPlaceholderText("输入嵌入模型名称，如：doubao-embedding-large-text-250515")
        form_layout.addRow("模型名称:", self.model_name_edit)

        layout.addLayout(form_layout)

        # 说明文本
        description_label = CaptionLabel(
            "配置向量嵌入模型参数。\n"
            "用于知识库的语义搜索和相似度匹配。"
        )
        description_label.setStyleSheet("padding: 8px 0;")
        layout.addWidget(description_label)

    def getConfig(self) -> dict:
        """获取配置"""
        return {
            "api_base": self.api_base_edit.text().strip() or "https://ark.cn-beijing.volces.com/api/v3",
            "api_key": self.api_key_edit.text().strip(),
            "model_name": self.model_name_edit.text().strip()
        }

    def setConfig(self, config: dict):
        """设置配置"""
        self.api_base_edit.setText(config.get("api_base", "https://ark.cn-beijing.volces.com/api/v3"))
        self.api_key_edit.setText(config.get("api_key", ""))
        self.model_name_edit.setText(config.get("model_name", ""))


class KnowledgeConfigCard(CardWidget):
    """知识库配置卡片"""

    def __init__(self, parent: Optional[QWidget] = None):
        super().__init__(parent)
        self.setupUI()

    def setupUI(self) -> None:
        """设置UI"""
        layout = QVBoxLayout(self)
        layout.setContentsMargins(20, 16, 20, 16)
        layout.setSpacing(16)

        # 卡片标题
        title_label = StrongBodyLabel("知识库配置")
        title_label.setFont(QFont("Microsoft YaHei", 12, QFont.Weight.Bold))
        layout.addWidget(title_label)

        # 表单布局
        form_layout = QFormLayout()
        form_layout.setSpacing(12)
        form_layout.setLabelAlignment(Qt.AlignmentFlag.AlignLeft)
        form_layout.setFieldGrowthPolicy(QFormLayout.FieldGrowthPolicy.ExpandingFieldsGrow)
        form_layout.setFormAlignment(Qt.AlignmentFlag.AlignLeft)

        # Contents DB Path
        self.contents_db_edit = LineEdit()
        self.contents_db_edit.setPlaceholderText("内容数据库路径")
        form_layout.addRow("内容数据库路径:", self.contents_db_edit)

        # Vector DB Path
        self.vector_db_edit = LineEdit()
        self.vector_db_edit.setPlaceholderText("向量数据库路径")
        form_layout.addRow("向量数据库路径:", self.vector_db_edit)

        # Max Results - 搜索返回的最大结果数
        from qfluentwidgets import SpinBox
        self.max_results_spin = SpinBox()
        self.max_results_spin.setRange(1, 20)
        self.max_results_spin.setValue(3)
        self.max_results_spin.setSuffix(" 条")
        self.max_results_spin.setToolTip(
            "知识库搜索时返回的最大结果数量。\n"
            "• 值越大，返回的相关文档越多，但响应可能变慢\n"
            "• 值越小，响应越快，但可能遗漏相关信息\n"
            "• 建议值：3-5"
        )
        form_layout.addRow("搜索结果数量:", self.max_results_spin)

        layout.addLayout(form_layout)

        # 说明文本
        description_label = CaptionLabel(
            "配置知识库的存储路径和搜索参数。\n"
            "内容数据库存储结构化数据，向量数据库存储嵌入向量。"
        )
        description_label.setStyleSheet("padding: 8px 0;")
        layout.addWidget(description_label)

    def getConfig(self) -> dict:
        """获取配置"""
        return {
            "contents_db_path": self.contents_db_edit.text().strip(),
            "vector_db_path": self.vector_db_edit.text().strip(),
            "max_results": self.max_results_spin.value()
        }

    def setConfig(self, config: dict):
        """设置配置"""
        self.contents_db_edit.setText(config.get("contents_db_path", ""))
        self.vector_db_edit.setText(config.get("vector_db_path", ""))
        self.max_results_spin.setValue(config.get("max_results", 3))


class PromptConfigCard(CardWidget):
    """提示词配置卡片"""

    def __init__(self, parent: Optional[QWidget] = None):
        super().__init__(parent)
        self.setupUI()

    def setupUI(self) -> None:
        """设置UI"""
        layout = QVBoxLayout(self)
        layout.setContentsMargins(20, 16, 20, 16)
        layout.setSpacing(16)

        # 卡片标题
        title_label = StrongBodyLabel("AI提示词配置")
        title_label.setFont(QFont("Microsoft YaHei", 12, QFont.Weight.Bold))
        layout.addWidget(title_label)

        # 表单布局
        form_layout = QFormLayout()
        form_layout.setSpacing(12)
        form_layout.setLabelAlignment(Qt.AlignmentFlag.AlignLeft)
        form_layout.setFieldGrowthPolicy(QFormLayout.FieldGrowthPolicy.ExpandingFieldsGrow)
        form_layout.setFormAlignment(Qt.AlignmentFlag.AlignLeft)

        # 角色描述
        self.description_edit = TextEdit()
        self.description_edit.setPlaceholderText(
            "输入AI助手的角色描述，如：你是一个专业的电商客服助手..."
        )
        self.description_edit.setMaximumHeight(100)
        form_layout.addRow("角色描述:", self.description_edit)

        # 额外提示词
        self.additional_context_edit = TextEdit()
        self.additional_context_edit.setPlaceholderText(
            "输入额外的提示词或上下文信息..."
        )
        self.additional_context_edit.setMaximumHeight(100)
        form_layout.addRow("额外提示词:", self.additional_context_edit)

        self.instructions_edit = TextEdit()
        self.instructions_edit.setPlaceholderText("输入行为指令，每行一条")
        self.instructions_edit.setMaximumHeight(120)
        form_layout.addRow("行为指令:", self.instructions_edit)

        layout.addLayout(form_layout)

        # 说明文本
        description_label = CaptionLabel(
            "配置AI助手的行为和回复风格。\n"
            "清晰的提示词可以帮助AI提供更准确和有用的回复。"
        )
        description_label.setStyleSheet("padding: 8px 0;")
        layout.addWidget(description_label)

    def getConfig(self) -> dict:
        """获取配置"""
        return {
            "description": self.description_edit.toPlainText().strip(),
            "additional_context": self.additional_context_edit.toPlainText().strip(),
            "instructions": [
                line.strip() for line in self.instructions_edit.toPlainText().splitlines() if line.strip()
            ]
        }

    def setConfig(self, config: dict):
        """设置配置"""
        self.description_edit.setPlainText(config.get("description", ""))
        self.additional_context_edit.setPlainText(config.get("additional_context", ""))
        instructions = config.get("instructions", [])
        if isinstance(instructions, list):
            self.instructions_edit.setPlainText("\n".join(instructions))
        elif isinstance(instructions, str):
            self.instructions_edit.setPlainText(instructions)


class HumanReplyWaitCard(CardWidget):
    """人工客服优先回复配置卡片"""

    def __init__(self, parent: Optional[QWidget] = None):
        super().__init__(parent)
        self.setupUI()

    def setupUI(self) -> None:
        """设置UI"""
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
        from qfluentwidgets import SwitchButton
        self.enable_switch = SwitchButton("启用")
        self.enable_switch.setChecked(True)
        form_layout.addRow("启用功能:", self.enable_switch)

        # 等待时间
        from qfluentwidgets import SpinBox
        self.wait_seconds_spin = SpinBox()
        self.wait_seconds_spin.setRange(5, 120)
        self.wait_seconds_spin.setValue(30)
        self.wait_seconds_spin.setSuffix(" 秒")
        form_layout.addRow("等待时间:", self.wait_seconds_spin)

        layout.addLayout(form_layout)

        # 说明文本
        description_label = CaptionLabel(
            "启用后，收到客户消息时会优先等待人工客服回复。\n"
            "如果人工客服在指定时间内回复，则取消AI自动回复。"
        )
        description_label.setStyleSheet("padding: 8px 0;")
        layout.addWidget(description_label)

    def getConfig(self) -> dict:
        """获取配置"""
        return {
            "staff_reply_wait": {
                "enable": self.enable_switch.isChecked(),
                "wait_seconds": self.wait_seconds_spin.value()
            }
        }

    def setConfig(self, config: dict):
        """设置配置"""
        staff_reply_wait = config.get("staff_reply_wait", {})
        self.enable_switch.setChecked(staff_reply_wait.get("enable", True))
        self.wait_seconds_spin.setValue(staff_reply_wait.get("wait_seconds", 30))


class BusinessHoursCard(CardWidget):
    """业务时间配置卡片"""

    def __init__(self, parent: Optional[QWidget] = None):
        super().__init__(parent)
        self.setupUI()

    def setupUI(self) -> None:
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
        description_label.setStyleSheet("padding: 8px 0;")
        layout.addWidget(description_label)
    
    def getConfig(self) -> dict:
        """获取配置"""
        return {
            "businessHours": {
                "start": self.start_time_picker.getTime().toString("HH:mm"),
                "end": self.end_time_picker.getTime().toString("HH:mm")
            },
            "business_hours": {
                "start": self.start_time_picker.getTime().toString("HH:mm"),
                "end": self.end_time_picker.getTime().toString("HH:mm")
            }
        }
    
    def setConfig(self, config: dict):
        """设置配置"""
        # 支持新旧配置格式
        business_hours = config.get("businessHours", config.get("business_hours", {}))

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


class AutoStartCard(CardWidget):
    """自动启动配置卡片"""

    def __init__(self, parent: Optional[QWidget] = None):
        super().__init__(parent)
        self.setupUI()

    def setupUI(self) -> None:
        """设置UI"""
        layout = QVBoxLayout(self)
        layout.setContentsMargins(20, 16, 20, 16)
        layout.setSpacing(16)

        # 卡片标题
        title_label = StrongBodyLabel("启动设置")
        title_label.setFont(QFont("Microsoft YaHei", 12, QFont.Weight.Bold))
        layout.addWidget(title_label)

        # 表单布局
        form_layout = QFormLayout()
        form_layout.setSpacing(12)
        form_layout.setLabelAlignment(Qt.AlignmentFlag.AlignLeft)
        form_layout.setFieldGrowthPolicy(QFormLayout.FieldGrowthPolicy.ExpandingFieldsGrow)
        form_layout.setFormAlignment(Qt.AlignmentFlag.AlignLeft)

        # 启用开关
        from qfluentwidgets import SwitchButton
        self.auto_start_switch = SwitchButton("启用")
        self.auto_start_switch.setChecked(False)
        form_layout.addRow("启动时自动开始回复:", self.auto_start_switch)

        layout.addLayout(form_layout)

        # 说明文本
        description_label = CaptionLabel(
            "启用后，应用启动时会自动为所有在线状态的账号开始自动回复。\n"
            "无需手动逐个点击开始按钮。"
        )
        description_label.setStyleSheet("padding: 8px 0;")
        layout.addWidget(description_label)

    def getConfig(self) -> dict:
        """获取配置"""
        return {
            "auto_start_on_launch": self.auto_start_switch.isChecked()
        }

    def setConfig(self, config: dict):
        """设置配置"""
        self.auto_start_switch.setChecked(config.get("auto_start_on_launch", False))


class AutoReloginCard(CardWidget):
    """自动重登配置卡片 - cookie 过期自动重登失败上限"""

    def __init__(self, parent: Optional[QWidget] = None):
        super().__init__(parent)
        self.setupUI()

    def setupUI(self) -> None:
        """设置UI"""
        layout = QVBoxLayout(self)
        layout.setContentsMargins(20, 16, 20, 16)
        layout.setSpacing(16)

        # 卡片标题
        title_label = StrongBodyLabel("自动重登设置")
        title_label.setFont(QFont("Microsoft YaHei", 12, QFont.Weight.Bold))
        layout.addWidget(title_label)

        # 表单布局
        form_layout = QFormLayout()
        form_layout.setSpacing(12)
        form_layout.setLabelAlignment(Qt.AlignmentFlag.AlignLeft)
        form_layout.setFieldGrowthPolicy(QFormLayout.FieldGrowthPolicy.ExpandingFieldsGrow)
        form_layout.setFormAlignment(Qt.AlignmentFlag.AlignLeft)

        # 连续失败上限
        self.max_failures_spin = SpinBox()
        self.max_failures_spin.setRange(1, 20)
        self.max_failures_spin.setValue(3)
        self.max_failures_spin.setSuffix(" 次")
        form_layout.addRow("失败上限:", self.max_failures_spin)

        layout.addLayout(form_layout)

        # 说明文本
        description_label = CaptionLabel(
            "账号登录态过期时自动重登（弹出浏览器等待处理）。\n"
            "连续失败达到上限后，停止自动重登并提示等待人工处理，\n"
            "重新启动账号自动回复后可再次获得自动重登机会。"
        )
        description_label.setStyleSheet("padding: 8px 0;")
        layout.addWidget(description_label)

    def getConfig(self) -> dict:
        """获取配置"""
        return {
            "relogin": {
                "max_auto_failures": self.max_failures_spin.value()
            }
        }

    def setConfig(self, config: dict):
        """设置配置"""
        relogin_config = config.get("relogin", {}) if isinstance(config, dict) else {}
        max_failures = relogin_config.get("max_auto_failures", 3)
        try:
            self.max_failures_spin.setValue(int(max_failures))
        except (TypeError, ValueError):
            self.max_failures_spin.setValue(3)


class RateLimitCard(CardWidget):
    """限流配置卡片 - AI 请求频率限制与兜底回复"""

    def __init__(self, parent: Optional[QWidget] = None):
        super().__init__(parent)
        self.setupUI()

    def setupUI(self) -> None:
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
        self.fallback_reply_edit.setPlaceholderText("输入限流后的兜底回复内容，每行一个回复，发送时随机抽取")
        self.fallback_reply_edit.setFixedHeight(120)
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
        # 将多行文本按行分割，过滤空行
        text = self.fallback_reply_edit.toPlainText().strip()
        fallback_replies = [line.strip() for line in text.split('\n') if line.strip()]
        if not fallback_replies:
            fallback_replies = ["这个我不了解呢，帮你问下我们的技术人员"]
        return {
            "rate_limit": {
                "window_hours": self.window_hours_spin.value(),
                "max_requests": self.max_requests_spin.value(),
                "fallback_reply": fallback_replies,
            }
        }

    def setConfig(self, config: dict):
        """设置配置"""
        rate_limit = config.get("rate_limit", {})
        self.window_hours_spin.setValue(rate_limit.get("window_hours", 4))
        self.max_requests_spin.setValue(rate_limit.get("max_requests", 10))
        fallback = rate_limit.get("fallback_reply", ["这个我不了解呢，帮你问下我们的技术人员"])
        # 兼容旧格式（单个字符串）和新格式（数组）
        if isinstance(fallback, str):
            text = fallback
        elif isinstance(fallback, list):
            text = '\n'.join(fallback)
        else:
            text = "这个我不了解呢，帮你问下我们的技术人员"
        self.fallback_reply_edit.setPlainText(text)


class ProxyTestThread(QThread):
    """后台测试 SOCKS5 代理连通性（requests 走代理访问拼多多域名，不阻塞 UI）"""

    # (耗时ms, 状态码或说明) 成功
    finished_ok = pyqtSignal(float, int)
    # (错误信息) 失败
    finished_err = pyqtSignal(str)

    # 测试目标：业务核心域名，响应码 < 500 即视为代理通路可用
    _TEST_URL = "https://mms.pinduoduo.com/"
    _TIMEOUT = 8

    def __init__(self, proxy_url: str, parent: Optional[QWidget] = None):
        super().__init__(parent)
        self.proxy_url = proxy_url
        self.setObjectName("ProxyTestThread")

    def run(self):
        """后台线程：同步 requests 走代理，通过信号回传结果"""
        try:
            import requests
            start = time.perf_counter()
            resp = requests.get(
                self._TEST_URL,
                proxies={"http": self.proxy_url, "https": self.proxy_url},
                timeout=self._TIMEOUT,
            )
            elapsed_ms = int((time.perf_counter() - start) * 1000)
            if resp.status_code < 500:
                self.finished_ok.emit(elapsed_ms, resp.status_code)
            else:
                self.finished_err.emit(f"目标站点返回 HTTP {resp.status_code}")
        except Exception as e:
            self.finished_err.emit(str(e))


class ProxyConfigCard(CardWidget):
    """SOCKS5 网络代理配置卡片"""

    def __init__(self, parent: Optional[QWidget] = None):
        super().__init__(parent)
        self._test_thread: Optional[ProxyTestThread] = None
        self.setupUI()

    @staticmethod
    def build_proxy_url(server: str, remote_dns: bool) -> str:
        """按表单值构造完整代理 URL（socks5h=远端解析 / socks5=本地解析）"""
        server = (server or "").strip()
        if "://" in server:
            server = server.split("://", 1)[1]
        scheme = "socks5h" if remote_dns else "socks5"
        return f"{scheme}://{server}"

    def setupUI(self) -> None:
        """设置UI"""
        from qfluentwidgets import SwitchButton

        layout = QVBoxLayout(self)
        layout.setContentsMargins(20, 16, 20, 16)
        layout.setSpacing(16)

        # 卡片标题行（标题 + 测试按钮）
        title_row = QHBoxLayout()
        title_label = StrongBodyLabel("网络代理（SOCKS5）")
        title_label.setFont(QFont("Microsoft YaHei", 12, QFont.Weight.Bold))
        title_row.addWidget(title_label)
        title_row.addStretch()
        self.test_btn = PushButton(FIF.SYNC, "测试连接")
        self.test_btn.setToolTip("使用当前填写的代理地址发起一次测试请求，验证代理是否可用")
        self.test_btn.clicked.connect(self._on_test_proxy)
        title_row.addWidget(self.test_btn)
        layout.addLayout(title_row)

        # 表单布局
        form_layout = QFormLayout()
        form_layout.setSpacing(12)
        form_layout.setLabelAlignment(Qt.AlignmentFlag.AlignLeft)
        form_layout.setFieldGrowthPolicy(QFormLayout.FieldGrowthPolicy.ExpandingFieldsGrow)
        form_layout.setFormAlignment(Qt.AlignmentFlag.AlignLeft)

        # 启用开关
        self.enable_switch = SwitchButton("启用")
        self.enable_switch.setChecked(False)
        form_layout.addRow("启用代理:", self.enable_switch)

        # 代理地址
        self.server_edit = LineEdit()
        self.server_edit.setPlaceholderText("127.0.0.1:1080（可省略 socks5:// 前缀）")
        self.server_edit.setText("127.0.0.1:1080")
        form_layout.addRow("代理地址:", self.server_edit)

        # 代理端解析域名开关
        self.remote_dns_switch = SwitchButton("代理端解析")
        self.remote_dns_switch.setChecked(True)
        form_layout.addRow("代理端解析域名:", self.remote_dns_switch)

        # 健康检查间隔
        self.check_interval_spin = SpinBox()
        self.check_interval_spin.setRange(0, 3600)
        self.check_interval_spin.setValue(60)
        self.check_interval_spin.setSuffix(" 秒")
        form_layout.addRow("健康检查间隔:", self.check_interval_spin)

        # 聊天媒体不走代理
        self.exclude_media_switch = SwitchButton("媒体直连")
        self.exclude_media_switch.setChecked(False)
        form_layout.addRow("聊天媒体不走代理:", self.exclude_media_switch)

        layout.addLayout(form_layout)

        # 说明文本
        description_label = CaptionLabel(
            "启用后所有网络请求（AI 接口、拼多多消息、图片下载等）经 SOCKS5 代理。\n"
            "代理地址支持域名（如 proxy.example.com:1080），每次连接实时解析，适配地址变动。\n"
            "代理端解析域名：由代理服务端解析域名，本地不发 DNS 查询（socks5h）；关闭则本地解析。\n"
            "健康检查：按设定间隔经代理探测拼多多首页，连续失败自动回退直连，恢复后自动切回代理"
            "（0 秒表示关闭自动检查）。\n"
            "聊天媒体不走代理：聊天窗口中的图片/视频下载直连，不经过代理（可能提高加载速度）。\n"
            "注意：AI 接口与拼多多 WebSocket 需重启或重连后生效；浏览器登录窗口不受开关影响，"
            "Chromium 的 SOCKS5 始终由代理服务端解析域名。\n"
            "测试连接：使用当前表单填写的地址（无需先保存），经代理访问拼多多首页验证可用性。"
        )
        description_label.setStyleSheet("color: #666; padding: 8px 0;")
        description_label.setWordWrap(True)
        layout.addWidget(description_label)

    def _on_test_proxy(self):
        """点击测试连接：校验表单值 → 后台线程实测代理通路"""
        if self._test_thread is not None and self._test_thread.isRunning():
            return

        server = self.server_edit.text().strip()
        if not server:
            InfoBar.warning(
                title="代理地址为空",
                content="请先填写代理地址，例如 127.0.0.1:1080",
                isClosable=True,
                position=InfoBarPosition.TOP,
                duration=3000,
                parent=self,
            )
            return

        proxy_url = self.build_proxy_url(server, self.remote_dns_switch.isChecked())

        # 禁用按钮，显示测试中
        self.test_btn.setEnabled(False)
        self.test_btn.setText("测试中...")

        self._test_thread = ProxyTestThread(proxy_url, parent=self)
        self._test_thread.finished_ok.connect(self._on_test_ok)
        self._test_thread.finished_err.connect(self._on_test_err)
        self._test_thread.finished.connect(self._on_test_done)
        self._test_thread.start()

    def _on_test_ok(self, elapsed_ms: float, status_code: int):
        """测试成功（代理通路可用）"""
        InfoBar.success(
            title="代理连接成功",
            content=f"经代理访问拼多多首页成功（HTTP {status_code}），耗时 {elapsed_ms} ms",
            isClosable=True,
            position=InfoBarPosition.TOP,
            duration=5000,
            parent=self,
        )

    def _on_test_err(self, error_msg: str):
        """测试失败"""
        InfoBar.error(
            title="代理连接失败",
            content=f"无法通过该代理访问目标站点：{error_msg}",
            isClosable=True,
            position=InfoBarPosition.TOP,
            duration=8000,
            parent=self,
        )

    def _on_test_done(self):
        """测试结束，恢复按钮"""
        try:
            self.test_btn.setEnabled(True)
            self.test_btn.setText("测试连接")
        except RuntimeError:
            pass  # 窗口已销毁

    def getConfig(self) -> dict:
        """获取配置"""
        return {
            "proxy": {
                "enabled": self.enable_switch.isChecked(),
                "server": self.server_edit.text().strip(),
                "remote_dns": self.remote_dns_switch.isChecked(),
                "check_interval": self.check_interval_spin.value(),
                "exclude_media": self.exclude_media_switch.isChecked()
            }
        }

    def setConfig(self, config: dict):
        """设置配置"""
        proxy = config.get("proxy", {})
        self.enable_switch.setChecked(proxy.get("enabled", False))
        self.server_edit.setText(proxy.get("server", "127.0.0.1:1080"))
        self.remote_dns_switch.setChecked(proxy.get("remote_dns", True))
        self.check_interval_spin.setValue(proxy.get("check_interval", 60))
        self.exclude_media_switch.setChecked(proxy.get("exclude_media", False))


class BarkTestThread(QThread):
    """后台发送一条 Bark 测试通知（不阻塞 UI）"""

    # (耗时ms) 成功
    finished_ok = pyqtSignal(int)
    # (错误信息) 失败
    finished_err = pyqtSignal(str)

    _TIMEOUT = 10

    def __init__(self, key: str, base_url: str, parent: Optional[QWidget] = None):
        super().__init__(parent)
        self.key = key
        self.base_url = base_url
        self.setObjectName("BarkTestThread")

    def run(self):
        """后台线程：走 bark_notify 的推送逻辑（复用同一套请求），通过信号回传结果"""
        try:
            from utils.bark_notify import _do_push_with

            start = time.perf_counter()
            ok = _do_push_with(
                self.key, self.base_url,
                "客服系统：测试通知",
                "Bark 通知配置验证成功，重登失败告警将推送到此设备。",
            )
            elapsed_ms = int((time.perf_counter() - start) * 1000)
            if ok:
                self.finished_ok.emit(elapsed_ms)
            else:
                self.finished_err.emit("推送失败（详见日志）")
        except Exception as e:
            self.finished_err.emit(str(e))


class BarkConfigCard(CardWidget):
    """Bark 通知配置卡片 - 账号重登失败等告警推送"""

    def __init__(self, parent: Optional[QWidget] = None):
        super().__init__(parent)
        self._test_thread: Optional[BarkTestThread] = None
        self.setupUI()

    def setupUI(self) -> None:
        """设置UI"""
        layout = QVBoxLayout(self)
        layout.setContentsMargins(20, 16, 20, 16)
        layout.setSpacing(16)

        # 卡片标题行（标题 + 测试按钮）
        title_row = QHBoxLayout()
        title_label = StrongBodyLabel("Bark 通知")
        title_label.setFont(QFont("Microsoft YaHei", 12, QFont.Weight.Bold))
        title_row.addWidget(title_label)
        title_row.addStretch()
        self.test_btn = PushButton(FIF.SEND, "发送测试通知")
        self.test_btn.setToolTip("用当前填写的密钥立即发送一条测试通知，验证能否推送到手机")
        self.test_btn.clicked.connect(self._on_test)
        title_row.addWidget(self.test_btn)
        layout.addLayout(title_row)

        # 表单布局
        form_layout = QFormLayout()
        form_layout.setSpacing(12)
        form_layout.setLabelAlignment(Qt.AlignmentFlag.AlignLeft)
        form_layout.setFieldGrowthPolicy(QFormLayout.FieldGrowthPolicy.ExpandingFieldsGrow)
        form_layout.setFormAlignment(Qt.AlignmentFlag.AlignLeft)

        # 设备密钥
        self.key_edit = PasswordLineEdit()
        self.key_edit.setPlaceholderText("Bark 设备 key（App 内复制）")
        form_layout.addRow("设备密钥:", self.key_edit)

        # 服务地址
        self.base_url_edit = LineEdit()
        self.base_url_edit.setPlaceholderText("https://api.day.app")
        self.base_url_edit.setText("https://api.day.app")
        form_layout.addRow("服务地址:", self.base_url_edit)

        layout.addLayout(form_layout)

        # 说明文本
        description_label = CaptionLabel(
            "自动重登连续失败达上限时推送告警，提醒人工处理（否则该店铺消息将无法回复）。\n"
            "密钥：手机安装 Bark App 后，在 App 内获取设备 key 填入。\n"
            "服务地址：默认 day.app 官方服务；自建 bark-server 可改为自己的地址。\n"
            "发送测试通知：使用当前填写的密钥（无需先保存），验证推送链路是否正常。"
        )
        description_label.setStyleSheet("color: #666; padding: 8px 0;")
        description_label.setWordWrap(True)
        layout.addWidget(description_label)

    def _on_test(self):
        """点击测试：校验表单值 → 后台线程发测试通知"""
        if self._test_thread is not None and self._test_thread.isRunning():
            return

        key = self.key_edit.text().strip()
        base_url = self.base_url_edit.text().strip() or "https://api.day.app"
        if not key:
            InfoBar.warning(
                title="密钥为空",
                content="请先填写 Bark 设备密钥",
                isClosable=True,
                position=InfoBarPosition.TOP,
                duration=3000,
                parent=self,
            )
            return

        self.test_btn.setEnabled(False)
        self.test_btn.setText("发送中...")

        self._test_thread = BarkTestThread(key, base_url, parent=self)
        self._test_thread.finished_ok.connect(self._on_test_ok)
        self._test_thread.finished_err.connect(self._on_test_err)
        self._test_thread.finished.connect(self._on_test_done)
        self._test_thread.start()

    def _on_test_ok(self, elapsed_ms: int):
        InfoBar.success(
            title="测试通知已发送",
            content=f"Bark 推送成功，耗时 {elapsed_ms} ms，请查看手机通知",
            isClosable=True,
            position=InfoBarPosition.TOP,
            duration=5000,
            parent=self,
        )

    def _on_test_err(self, error_msg: str):
        InfoBar.error(
            title="测试通知发送失败",
            content=f"{error_msg}，请检查密钥和服务地址",
            isClosable=True,
            position=InfoBarPosition.TOP,
            duration=8000,
            parent=self,
        )

    def _on_test_done(self):
        try:
            self.test_btn.setEnabled(True)
            self.test_btn.setText("发送测试通知")
        except RuntimeError:
            pass  # 窗口已销毁

    def getConfig(self) -> dict:
        """获取配置"""
        return {
            "bark": {
                "key": self.key_edit.text().strip(),
                "base_url": self.base_url_edit.text().strip() or "https://api.day.app"
            }
        }

    def setConfig(self, config: dict):
        """设置配置"""
        bark_config = config.get("bark", {}) if isinstance(config, dict) else {}
        self.key_edit.setText(bark_config.get("key", ""))
        self.base_url_edit.setText(bark_config.get("base_url", "https://api.day.app"))


class BannedWordsCard(CardWidget):
    """禁用词配置卡片 - AI 回复发送前的硬拦截词表"""

    def __init__(self, parent: Optional[QWidget] = None):
        super().__init__(parent)
        self.setupUI()

    def setupUI(self) -> None:
        """设置UI"""
        layout = QVBoxLayout(self)
        layout.setContentsMargins(20, 16, 20, 16)
        layout.setSpacing(16)

        # 卡片标题
        title_label = StrongBodyLabel("禁用词拦截")
        title_label.setFont(QFont("Microsoft YaHei", 12, QFont.Weight.Bold))
        layout.addWidget(title_label)

        # 表单布局
        form_layout = QFormLayout()
        form_layout.setSpacing(12)
        form_layout.setLabelAlignment(Qt.AlignmentFlag.AlignLeft)
        form_layout.setFieldGrowthPolicy(QFormLayout.FieldGrowthPolicy.ExpandingFieldsGrow)
        form_layout.setFormAlignment(Qt.AlignmentFlag.AlignLeft)

        # 禁用词列表（每行一个）
        self.banned_words_edit = TextEdit()
        self.banned_words_edit.setPlaceholderText(
            "每行输入一个禁用词，AI 回复中若包含这些词将被拦截并要求重新生成"
        )
        self.banned_words_edit.setFixedHeight(120)
        form_layout.addRow("禁用词:", self.banned_words_edit)

        layout.addLayout(form_layout)

        # 说明文本
        description_label = CaptionLabel(
            "AI 生成回复后、发送前会检查是否包含以上禁用词。\n"
            "命中则把具体词反馈给 AI 重新生成合规回复，最多重试 2 次；\n"
            "仍命中则改发兜底话术。子串匹配、大小写不敏感。"
        )
        description_label.setStyleSheet("color: #666; padding: 8px 0;")
        description_label.setWordWrap(True)
        layout.addWidget(description_label)

    def getConfig(self) -> dict:
        """获取配置：每行一个词，过滤空行"""
        text = self.banned_words_edit.toPlainText().strip()
        words = [line.strip() for line in text.split('\n') if line.strip()]
        return {"banned_words": words}

    def setConfig(self, config: dict):
        """设置配置：兼容 list 与单字符串两种格式"""
        banned = config.get("banned_words", [])
        if isinstance(banned, str):
            text = banned
        elif isinstance(banned, list):
            text = '\n'.join(banned)
        else:
            text = ""
        self.banned_words_edit.setPlainText(text)


class SettingUI(QFrame):
    """设置界面"""

    def __init__(self, parent: Optional[QWidget] = None):
        super().__init__(parent=parent)
        self.logger = get_logger("SettingUI")
        self.setupUI()
        self.loadConfig()

        # 设置对象名
        self.setObjectName("设置")
    
    def changeEvent(self, event):
        """监听主题切换事件，更新标签样式"""
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
        """更新标签样式以适配当前主题"""
        try:
            if isDarkTheme():
                self.title_label.setStyleSheet("color: #ffffff;")
                self.description_label.setStyleSheet("padding: 8px 0; color: #cccccc;")
                # 更新所有配置卡片中的表单标签颜色
                self._update_form_label_styles(True)
            else:
                self.title_label.setStyleSheet("")
                self.description_label.setStyleSheet("padding: 8px 0;")
                # 恢复所有配置卡片中的表单标签颜色
                self._update_form_label_styles(False)
        except Exception as e:
            self.logger.warning(f"更新标签样式失败: {e}")
    
    def _update_form_label_styles(self, is_dark: bool):
        """更新表单标签样式"""
        # 更新所有配置卡片中的表单标签颜色
        cards = [
            self.llm_config_card,
            self.embedder_config_card,
            self.knowledge_config_card,
            self.prompt_config_card,
            self.business_hours_card,
            self.human_reply_wait_card,
            self.auto_start_card,
            self.relogin_card,
            self.rate_limit_card,
            self.proxy_card,
            self.banned_words_card
        ]
        
        for card in cards:
            if hasattr(card, 'layout') and card.layout() is not None:
                # 遍历卡片中的所有子控件
                for i in range(card.layout().count()):
                    item = card.layout().itemAt(i)
                    if item and item.widget():
                        # 如果是表单布局
                        if isinstance(item.widget(), QFormLayout):
                            form_layout = item.widget()
                            # 更新表单中所有标签的颜色
                            for row in range(form_layout.rowCount()):
                                label_item = form_layout.itemAt(row, QFormLayout.ItemRole.LabelRole)
                                if label_item and label_item.widget():
                                    label_widget = label_item.widget()
                                    if is_dark:
                                        label_widget.setStyleSheet("color: #ffffff;")
                                    else:
                                        label_widget.setStyleSheet("")

    def setupUI(self) -> None:
        """设置主界面UI"""
        # 主布局
        main_layout = QVBoxLayout(self)
        main_layout.setContentsMargins(30, 30, 30, 30)
        main_layout.setSpacing(25)
        
        # 创建头部区域
        header_widget = self.createHeaderWidget()
        
        # 创建内容区域
        content_widget = self.createContentWidget()
        
        # 根据主题更新表单标签样式（卡片已创建，可以安全调用）
        if isDarkTheme():
            self._update_form_label_styles(True)
        else:
            self._update_form_label_styles(False)
        
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
        self.title_label = SubtitleLabel("系统设置")
        self.title_label.setFont(QFont("Microsoft YaHei", 18, QFont.Weight.Bold))
        
        # 描述
        self.description_label = CaptionLabel("配置AI客服的基本参数和工作时间")
        
        # 根据主题设置标签样式
        if isDarkTheme():
            self.title_label.setStyleSheet("color: #ffffff;")
            self.description_label.setStyleSheet("padding: 8px 0; color: #cccccc;")
        else:
            self.description_label.setStyleSheet("padding: 8px 0;")
        
        # 左侧标题区域
        title_area = QWidget()
        title_layout = QVBoxLayout(title_area)
        title_layout.setContentsMargins(0, 0, 0, 0)
        title_layout.setSpacing(5)
        title_layout.addWidget(self.title_label)
        title_layout.addWidget(self.description_label)
        
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
        self.llm_config_card = LLMConfigCard()
        self.embedder_config_card = EmbedderConfigCard()
        self.knowledge_config_card = KnowledgeConfigCard()
        self.prompt_config_card = PromptConfigCard()
        self.business_hours_card = BusinessHoursCard()
        self.human_reply_wait_card = HumanReplyWaitCard()
        self.auto_start_card = AutoStartCard()
        self.relogin_card = AutoReloginCard()
        self.rate_limit_card = RateLimitCard()
        self.proxy_card = ProxyConfigCard()
        self.bark_card = BarkConfigCard()
        self.banned_words_card = BannedWordsCard()

        # 添加到布局
        content_layout.addWidget(self.llm_config_card)
        content_layout.addWidget(self.embedder_config_card)
        content_layout.addWidget(self.knowledge_config_card)
        content_layout.addWidget(self.prompt_config_card)
        content_layout.addWidget(self.business_hours_card)
        content_layout.addWidget(self.human_reply_wait_card)
        content_layout.addWidget(self.auto_start_card)
        content_layout.addWidget(self.relogin_card)
        content_layout.addWidget(self.rate_limit_card)
        content_layout.addWidget(self.proxy_card)
        content_layout.addWidget(self.bark_card)
        content_layout.addWidget(self.banned_words_card)
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

    def loadConfig(self):
        """从config模块加载配置"""
        try:
            # 从配置模块获取各个配置项
            loaded_config = {
                "llm": {
                    "api_base": config.get("llm.api_base", "https://ark.cn-beijing.volces.com/api/v3"),
                    "api_key": config.get("llm.api_key", ""),
                    "model_name": config.get("llm.model_name", "doubao-seed-1-6-flash-250828"),
                    "thinking": config.get("llm.thinking", {"type": "disabled"}),
                    "reasoning_effort": config.get("llm.reasoning_effort", ""),
                    "send_image_to_ai": config.get("llm.send_image_to_ai", True)
                },
                "embedder": {
                    "api_base": config.get("embedder.api_base", "https://ark.cn-beijing.volces.com/api/v3"),
                    "api_key": config.get("embedder.api_key", ""),
                    "model_name": config.get("embedder.model_name", "doubao-embedding-large-text-250515")
                },
                "knowledge_base": {
                    "contents_db_path": config.get("knowledge_base.contents_db_path", ""),
                    "vector_db_path": config.get("knowledge_base.vector_db_path", ""),
                    "max_results": config.get("knowledge_base.max_results", 3)
                },
                "prompt": {
                    "description": config.get("prompt.description", ""),
                    "additional_context": config.get("prompt.additional_context", ""),
                    "instructions": config.get("prompt.instructions", [])
                },
                "business_hours": {
                    "start": config.get("business_hours.start", "08:00"),
                    "end": config.get("business_hours.end", "23:00")
                },
                "staff_reply_wait": {
                    "enable": config.get("staff_reply_wait.enable", True),
                    "wait_seconds": config.get("staff_reply_wait.wait_seconds", 30)
                },
                "auto_start_on_launch": config.get("auto_start_on_launch", False),
                "rate_limit": {
                    "window_hours": config.get("rate_limit.window_hours", 4),
                    "max_requests": config.get("rate_limit.max_requests", 10),
                    "fallback_reply": config.get("rate_limit.fallback_reply", [])
                },
                "banned_words": config.get("banned_words", []),
                "proxy": {
                    "enabled": config.get("proxy.enabled", False),
                    "server": config.get("proxy.server", "127.0.0.1:1080"),
                    "remote_dns": config.get("proxy.remote_dns", True),
                    "check_interval": config.get("proxy.check_interval", 60),
                    "exclude_media": config.get("proxy.exclude_media", False)
                },
                "bark": {
                    "key": config.get("bark.key", ""),
                    "base_url": config.get("bark.base_url", "https://api.day.app")
                }
            }

            # 验证并设置配置
            self._validateAndSetConfig(loaded_config)
            self.logger.info("配置加载成功")

        except Exception as e:
            self.logger.error(f"加载配置失败: {e}")
            self._show_info("加载失败", f"加载配置失败：{str(e)}", "warning")
            self._loadDefaultConfig()
    
    def _loadDefaultConfig(self):
        """加载默认配置"""
        default_config = {
            "llm": {
                "api_base": "https://ark.cn-beijing.volces.com/api/v3",
                "api_key": "",
                "model_name": "doubao-seed-1-6-flash-250828",
                "thinking": {"type": "disabled"},
                "reasoning_effort": "",
                "send_image_to_ai": True
            },
            "embedder": {
                "api_base": "https://ark.cn-beijing.volces.com/api/v3",
                "api_key": "",
                "model_name": "doubao-embedding-large-text-250515"
            },
            "knowledge_base": {
                "contents_db_path": "",
                "vector_db_path": ""
            },
            "prompt": {
                "description": "你是一个专业的电商客服助手，负责为拼多多店铺提供优质的客户服务。请遵循以下原则：\n\n1. 友好专业：始终保持礼貌、耐心和专业的态度\n2. 准确回答：根据客户问题提供准确、有用的信息\n3. 主动服务：主动了解客户需求，提供个性化建议\n4. 及时响应：快速响应客户咨询，提高服务效率\n5. 问题解决：积极帮助客户解决购物和售后问题",
                "additional_context": "请用简洁明了的语言回复，避免过长的回答。如果遇到无法解决的复杂问题，请礼貌地建议客户联系人工客服。",
                "instructions": [
                    "1. 请用中文回复客户问题",
                    "2. 如果客户问题超出了你的能力范围，请建议客户联系人工客服"
                ]
            },
            "business_hours": {
                "start": "08:00",
                "end": "23:00"
            },
            "rate_limit": {
                "window_hours": 4,
                "max_requests": 10,
                "fallback_reply": ["这个我不了解呢，帮你问下我们的技术人员"]
            },
            "relogin": {
                "max_auto_failures": 3
            },
            "bark": {
                "key": "",
                "base_url": "https://api.day.app"
            }
        }

        self._validateAndSetConfig(default_config)
        self.logger.info("已加载默认配置")

    def _validateAndSetConfig(self, config_data):
        """验证并设置配置"""
        # 确保必要的字段存在
        validated_config = {
            "llm": config_data.get("llm", {
                "api_base": "https://ark.cn-beijing.volces.com/api/v3",
                "api_key": "",
                "model_name": "doubao-seed-1-6-flash-250828",
                "thinking": {"type": "disabled"},
                "reasoning_effort": "",
                "send_image_to_ai": True
            }),
            "embedder": config_data.get("embedder", {
                "api_base": "https://ark.cn-beijing.volces.com/api/v3",
                "api_key": "",
                "model_name": "doubao-embedding-large-text-250515"
            }),
            "knowledge_base": config_data.get("knowledge_base", {
                "contents_db_path": "",
                "vector_db_path": "",
                "max_results": 3
            }),
            "prompt": config_data.get("prompt", {
                "description": "",
                "additional_context": "",
                "instructions": []
            }),
            "business_hours": config_data.get("business_hours", {"start": "08:00", "end": "23:00"}),
            "staff_reply_wait": config_data.get("staff_reply_wait", {"enable": True, "wait_seconds": 30}),
            "rate_limit": config_data.get("rate_limit", {"window_hours": 4, "max_requests": 10, "fallback_reply": ["这个我不了解呢，帮你问下我们的技术人员"]}),
            "banned_words": config_data.get("banned_words", []),
            "proxy": config_data.get("proxy", {
                "enabled": False,
                "server": "127.0.0.1:1080",
                "remote_dns": True,
                "check_interval": 60,
                "exclude_media": False
            }),
            "bark": config_data.get("bark", {
                "key": "",
                "base_url": "https://api.day.app"
            })
        }

        # 验证business_hours格式
        business_hours = validated_config["business_hours"]
        if not isinstance(business_hours, dict):
            business_hours = {"start": "08:00", "end": "23:00"}
            validated_config["business_hours"] = business_hours

        if "start" not in business_hours:
            business_hours["start"] = "08:00"
        if "end" not in business_hours:
            business_hours["end"] = "23:00"

        # 验证staff_reply_wait格式
        staff_reply_wait = validated_config["staff_reply_wait"]
        if not isinstance(staff_reply_wait, dict):
            staff_reply_wait = {"enable": True, "wait_seconds": 30}
            validated_config["staff_reply_wait"] = staff_reply_wait

        if "enable" not in staff_reply_wait:
            staff_reply_wait["enable"] = True
        if "wait_seconds" not in staff_reply_wait:
            staff_reply_wait["wait_seconds"] = 30

        # 验证rate_limit格式
        rate_limit = validated_config["rate_limit"]
        if not isinstance(rate_limit, dict):
            rate_limit = {"window_hours": 4, "max_requests": 10, "fallback_reply": ["这个我不了解呢，帮你问下我们的技术人员"]}
            validated_config["rate_limit"] = rate_limit

        rate_limit.setdefault("window_hours", 4)
        rate_limit.setdefault("max_requests", 10)
        rate_limit.setdefault("fallback_reply", ["这个我不了解呢，帮你问下我们的技术人员"])

        # 设置到界面
        self.llm_config_card.setConfig(validated_config["llm"])
        self.embedder_config_card.setConfig(validated_config["embedder"])
        self.knowledge_config_card.setConfig(validated_config["knowledge_base"])
        self.prompt_config_card.setConfig(validated_config["prompt"])

        # 处理业务时间配置
        business_hours_config = validated_config["business_hours"]
        self.business_hours_card.setConfig({"business_hours": business_hours_config})

        # 处理人工回复等待配置
        staff_reply_wait_config = validated_config["staff_reply_wait"]
        self.human_reply_wait_card.setConfig({"staff_reply_wait": staff_reply_wait_config})

        # 处理自动启动配置
        auto_start = config_data.get("auto_start_on_launch", False)
        self.auto_start_card.setConfig({"auto_start_on_launch": auto_start})

        # 处理自动重登配置
        relogin_config = validated_config.get("relogin", {"max_auto_failures": 3})
        if not isinstance(relogin_config, dict):
            relogin_config = {"max_auto_failures": 3}
            validated_config["relogin"] = relogin_config
        self.relogin_card.setConfig({"relogin": relogin_config})

        self.rate_limit_card.setConfig(validated_config)
        self.proxy_card.setConfig(validated_config)
        self.bark_card.setConfig(validated_config)
        self.banned_words_card.setConfig(validated_config)
    
    def onSaveConfig(self):
        """保存配置到config模块"""
        try:
            # 获取各配置卡片的配置
            llm_config = self.llm_config_card.getConfig()
            embedder_config = self.embedder_config_card.getConfig()
            knowledge_config = self.knowledge_config_card.getConfig()
            prompt_config = self.prompt_config_card.getConfig()
            business_config = self.business_hours_card.getConfig()
            staff_reply_wait_config = self.human_reply_wait_card.getConfig()
            auto_start_config = self.auto_start_card.getConfig()
            relogin_config = self.relogin_card.getConfig()
            rate_limit_config = self.rate_limit_card.getConfig()
            proxy_config = self.proxy_card.getConfig()
            bark_config = self.bark_card.getConfig()
            banned_words_config = self.banned_words_card.getConfig()

            # 合并配置为新的结构
            new_config = {
                "llm": llm_config,
                "embedder": embedder_config,
                "knowledge_base": knowledge_config,
                "prompt": prompt_config,
                "business_hours": business_config.get("businessHours", {"start": "08:00", "end": "23:00"}),
                "staff_reply_wait": staff_reply_wait_config.get("staff_reply_wait", {"enable": True, "wait_seconds": 30}),
                "auto_start_on_launch": auto_start_config.get("auto_start_on_launch", False),
                **relogin_config,
                **rate_limit_config,
                **proxy_config,
                **bark_config,
                "banned_words": banned_words_config.get("banned_words", []),
                # 保持与旧配置的兼容性
                "db_path": config.get("db_path", "")
            }

            # 验证 LLM 必填项
            if not llm_config.get("api_key"):
                self._show_info("配置错误", "请输入LLM API Key！", "warning")
                return
            if not llm_config.get("model_name"):
                self._show_info("配置错误", "请输入LLM模型名称！", "warning")
                return

            # 验证时间设置
            start_time = self.business_hours_card.start_time_picker.getTime()
            end_time = self.business_hours_card.end_time_picker.getTime()

            if start_time >= end_time:
                self._show_info("时间设置错误", "开始时间必须早于结束时间！", "warning")
                return

            # 使用config模块保存配置
            config.update(new_config, save=True)

            # 热更新全局限流器：保存后立即生效，无需重启/重连
            try:
                rc = config.get_rate_limit_config()
                from Message.handlers.rate_limiter import coze_rate_limiter
                coze_rate_limiter.configure(
                    window_size=rc['window_hours'] * 3600,
                    max_requests=rc['max_requests']
                )
                self.logger.info(f"限流器已热更新: {rc}")
            except Exception as e:
                self.logger.warning(f"限流器热更新失败: {e}，重启后生效")

            # 热更新网络代理环境变量：保存后立即生效
            # （requests 即时生效；httpx/openai 长驻客户端与 websockets/playwright 需重启或重连）
            try:
                from utils.proxy_config import apply_proxy_env, start_proxy_health_monitor
                apply_proxy_env()
                # 按新配置重启健康检查（未启用代理或 check_interval=0 关闭自动检查）
                _proxy_cfg = self.proxy_card.getConfig().get("proxy", {})
                _proxy_interval = _proxy_cfg.get("check_interval", 60) if _proxy_cfg.get("enabled", False) else 0
                start_proxy_health_monitor(_proxy_interval)
                self.logger.info(f"网络代理已热更新: {self.proxy_card.getConfig()}")
            except Exception as e:
                self.logger.warning(f"网络代理热更新失败: {e}，重启后生效")

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
            self._show_info("保存失败", f"保存配置时发生错误：{str(e)}", "error")
    
    def onResetConfig(self):
        """重置配置"""
        if not self._ask_confirm("确认重置", "确定要重置所有配置吗？\n这将重新加载配置文件中的原始设置。"):
            return
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
            self._show_info("重置失败", f"重置配置失败：{str(e)}", "error")
    
 
