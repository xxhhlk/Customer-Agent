#自动回复界面

import asyncio
from PyQt6.QtCore import Qt, pyqtSignal, QThread, pyqtSignal as Signal, QTimer
from PyQt6.QtWidgets import (QFrame, QHBoxLayout, QVBoxLayout, QWidget, QSizePolicy, QLabel,
                            QInputDialog, QMessageBox, QComboBox, QDialog, QFormLayout)
from PyQt6.QtGui import QFont, QIcon, QPixmap, QPainter, QPainterPath
from qfluentwidgets import (CardWidget, SubtitleLabel, CaptionLabel, BodyLabel, 
                           PrimaryPushButton, PushButton, StrongBodyLabel, 
                           InfoBadge, ScrollArea, FluentIcon as FIF)
from database.db_manager import db_manager
from utils.logger import get_logger
from Channel.pinduoduo.utils.API.Set_up_online import AccountMonitor
import threading
from typing import Dict, Optional
import requests
import os


class LogoLoaderThread(QThread):
    """异步加载Logo的线程"""
    logo_loaded = pyqtSignal(QPixmap)

    def __init__(self, url):
        super().__init__()
        self.url = url

    def run(self):
        try:
            response = requests.get(self.url, timeout=10)
            response.raise_for_status()
            
            pixmap = QPixmap()
            pixmap.loadFromData(response.content)

            if pixmap.isNull():
                raise ValueError("Loaded data is not a valid image.")

            # 创建圆形pixmap
            size = 60
            circular_pixmap = QPixmap(size, size)
            circular_pixmap.fill(Qt.GlobalColor.transparent)

            painter = QPainter(circular_pixmap)
            painter.setRenderHint(QPainter.RenderHint.Antialiasing)
            
            path = QPainterPath()
            path.addEllipse(0, 0, size, size)
            
            painter.setClipPath(path)
            
            # 缩放并绘制原始图片
            scaled_pixmap = pixmap.scaled(size, size, Qt.AspectRatioMode.KeepAspectRatioByExpanding, Qt.TransformationMode.SmoothTransformation)
            painter.drawPixmap(0, 0, scaled_pixmap)
            painter.end()

            self.logo_loaded.emit(circular_pixmap)
        except Exception as e:
            get_logger().error(f"Failed to load logo from {self.url}: {e}")
            self.logo_loaded.emit(QPixmap()) # 失败时发射空pixmap


class AutoReplyManager:
    """自动回复管理器 - 管理所有账号的自动回复连接
    
    增强功能：
    - 重连状态跟踪：实时记录每个账号的重连状态
    - 自动恢复标记：支持崩溃后自动恢复连接
    - 智能重连调度：指数退避算法，避免频繁重连
    """
    
    # 重连配置
    MAX_RECONNECT_ATTEMPTS = 3  # 最大重连次数
    RECONNECT_DELAY_BASE = 5    # 基础重连延迟（秒）
    RECONNECT_DELAY_MAX = 60    # 最大重连延迟（秒）
    
    # 重连状态枚举
    class ReconnectStatus:
        IDLE = "idle"                    # 空闲状态
        CONNECTING = "connecting"        # 首次连接中
        CONNECTED = "connected"          # 已连接
        RECONNECTING = "reconnecting"    # 正在重连
        RECONNECT_SCHEDULED = "reconnect_scheduled"  # 已调度重连（等待中）
        FAILED = "failed"                # 重连失败
        STOPPED = "stopped"              # 已停止
    
    def __init__(self):
        self.running_accounts: Dict[str, 'AutoReplyThread'] = {}  # 正在运行的账号线程
        
        # 重连状态跟踪
        self.reconnect_attempts: Dict[str, int] = {}  # 重连尝试次数记录
        self.should_auto_reconnect: Dict[str, bool] = {}  # 是否应自动重连标记
        self.reconnect_timers: Dict[str, QTimer] = {}  # 重连定时器
        self.reconnect_status: Dict[str, str] = {}  # 当前重连状态
        self.reconnect_history: Dict[str, list] = {}  # 重连历史记录
        self.last_disconnect_time: Dict[str, float] = {}  # 上次断开时间
        self.last_reconnect_time: Dict[str, float] = {}  # 上次重连时间
        
        # 自动恢复标记
        self.auto_recovery_enabled: Dict[str, bool] = {}  # 是否启用自动恢复
        self.scheduled_reconnects: Dict[str, dict] = {}  # 计划的重连任务
        
        self.logger = get_logger()
        
        # UI回调信号（将在AutoReplyPage中连接）
        self.on_reconnecting_callback = None  # 正在重连回调
        self.on_reconnect_failed_callback = None  # 重连失败回调
        self.on_reconnect_success_callback = None  # 重连成功回调
        self.on_status_changed_callback = None  # 状态变更回调（新增）
    
    def start_auto_reply(self, account_data: dict, is_reconnect: bool = False) -> bool:
        """启动账号自动回复
        
        Args:
            account_data: 账号数据
            is_reconnect: 是否是重连（用于区分首次连接和重连）
        """
        try:
            account_key = f"{account_data['channel_name']}_{account_data['shop_id']}_{account_data['username']}"
            
            # 检查是否已经在运行
            if account_key in self.running_accounts:
                self.logger.warning(f"账号 {account_data['username']} 自动回复已在运行")
                return False
            
            # 重置重连标记（首次连接时）
            if not is_reconnect:
                self.should_auto_reconnect[account_key] = True
                self.reconnect_attempts[account_key] = 0
            
            # 创建并启动自动回复线程
            thread = AutoReplyThread(account_data)
            self.running_accounts[account_key] = thread
            
            # 连接信号
            thread.connection_success.connect(lambda: self._on_connection_success(account_key, account_data, is_reconnect))
            thread.connection_failed.connect(lambda error: self._on_connection_failed(account_key, account_data, error))
            thread.connection_closed_unexpectedly.connect(lambda error: self._on_connection_closed_unexpectedly(account_key, account_data, error))
            thread.finished.connect(lambda: self._on_thread_finished(account_key))
            
            # 启动线程
            thread.start()
            return True
            
        except Exception as e:
            self.logger.error(f"启动自动回复失败: {str(e)}")
            return False
    
    def stop_auto_reply(self, account_data: dict) -> bool:
        """停止账号自动回复"""
        try:
            account_key = f"{account_data['channel_name']}_{account_data['shop_id']}_{account_data['username']}"
            
            if account_key not in self.running_accounts:
                self.logger.warning(f"账号 {account_data['username']} 自动回复未在运行")
                return False
            
            # 清除自动重连标记（用户主动停止）
            self.should_auto_reconnect[account_key] = False
            
            # 取消任何待执行的重连
            self._cancel_reconnect_timer(account_key)
            
            # 停止线程
            thread = self.running_accounts[account_key]
            thread.stop()
            
            # 等待线程结束后再从列表中移除
            if thread.isRunning():
                thread.wait(5000)  # 最多等待5秒
            
            # 从运行列表中移除
            if account_key in self.running_accounts:
                del self.running_accounts[account_key]
            
            # 清理重连相关状态
            self._cleanup_reconnect_state(account_key)
            
            return True
            
        except Exception as e:
            self.logger.error(f"停止自动回复失败: {str(e)}")
            return False
    
    def is_running(self, account_data: dict) -> bool:
        """检查账号是否正在自动回复"""
        account_key = f"{account_data['channel_name']}_{account_data['shop_id']}_{account_data['username']}"
        return account_key in self.running_accounts and self.running_accounts[account_key].is_running()
    
    def _on_connection_success(self, account_key: str, account_data: dict, is_reconnect: bool):
        """连接成功回调"""
        try:
            self.logger.debug(f"账号 {account_key} 自动回复连接成功")
            
            # 重置重连计数
            self.reconnect_attempts[account_key] = 0
            
            # 如果是重连成功，通知UI
            if is_reconnect and self.on_reconnect_success_callback:
                self.on_reconnect_success_callback(account_data)
                
        except Exception as e:
            self.logger.error(f"处理连接成功回调失败: {e}")
    
    def _on_connection_failed(self, account_key: str, account_data: dict, error: str):
        """连接失败回调（首次连接失败）"""
        try:
            self.logger.error(f"账号 {account_key} 自动回复连接失败: {error}")
            # 清理失败的线程
            if account_key in self.running_accounts:
                del self.running_accounts[account_key]
            
            # 首次连接失败不重连
            self.should_auto_reconnect[account_key] = False
            self._cleanup_reconnect_state(account_key)
            
        except Exception as e:
            self.logger.error(f"处理连接失败回调失败: {e}")
    
    def _on_connection_closed_unexpectedly(self, account_key: str, account_data: dict, error: str):
        """连接异常关闭回调（触发重连）"""
        try:
            self.logger.warning(f"账号 {account_key} WebSocket异常关闭: {error}")
            
            # 从运行列表中移除（线程已经结束）
            if account_key in self.running_accounts:
                del self.running_accounts[account_key]
            
            # 检查是否应该自动重连
            if not self.should_auto_reconnect.get(account_key, False):
                self.logger.info(f"账号 {account_key} 自动重连已禁用，不重连")
                self._cleanup_reconnect_state(account_key)
                return
            
            # 检查重连次数
            current_attempts = self.reconnect_attempts.get(account_key, 0)
            if current_attempts >= self.MAX_RECONNECT_ATTEMPTS:
                self.logger.error(f"账号 {account_key} 重连次数已达上限({self.MAX_RECONNECT_ATTEMPTS})，放弃重连")
                self.should_auto_reconnect[account_key] = False
                if self.on_reconnect_failed_callback:
                    self.on_reconnect_failed_callback(account_data, f"重连失败，已达到最大重试次数({self.MAX_RECONNECT_ATTEMPTS})")
                self._cleanup_reconnect_state(account_key)
                return
            
            # 增加重连计数
            self.reconnect_attempts[account_key] = current_attempts + 1
            attempt = self.reconnect_attempts[account_key]
            
            # 计算延迟（指数退避）
            import random
            delay = min(self.RECONNECT_DELAY_BASE * (2 ** (attempt - 1)) + random.uniform(0, 2), self.RECONNECT_DELAY_MAX)
            
            self.logger.info(f"账号 {account_key} 将在 {delay:.1f} 秒后进行第 {attempt} 次重连")
            
            # 通知UI正在重连
            if self.on_reconnecting_callback:
                self.on_reconnecting_callback(account_data, attempt, self.MAX_RECONNECT_ATTEMPTS, delay)
            
            # 创建定时器进行延迟重连
            timer = QTimer()
            timer.setSingleShot(True)
            timer.timeout.connect(lambda: self._do_reconnect(account_key, account_data))
            timer.start(int(delay * 1000))
            
            self.reconnect_timers[account_key] = timer
            
        except Exception as e:
            self.logger.error(f"处理连接异常关闭回调失败: {e}")
    
    def _do_reconnect(self, account_key: str, account_data: dict):
        """执行重连"""
        try:
            # 清理定时器引用
            if account_key in self.reconnect_timers:
                del self.reconnect_timers[account_key]
            
            # 检查是否仍需要重连
            if not self.should_auto_reconnect.get(account_key, False):
                self.logger.info(f"账号 {account_key} 重连已取消")
                return
            
            self.logger.info(f"账号 {account_key} 开始重连...")
            
            # 尝试重新登录（刷新cookies）
            relogin_success = self._try_relogin(account_data)
            if not relogin_success:
                self.logger.warning(f"账号 {account_key} 重新登录失败，继续尝试重连WebSocket")
                # 继续尝试重连WebSocket，即使重新登录失败
            
            # 启动自动回复（标记为重连）
            success = self.start_auto_reply(account_data, is_reconnect=True)
            if not success:
                self.logger.error(f"账号 {account_key} 重连启动失败")
                # 触发下一次重连尝试
                self._on_connection_closed_unexpectedly(account_key, account_data, "重连启动失败")
                
        except Exception as e:
            self.logger.error(f"执行重连失败: {e}")
            # 触发下一次重连尝试
            self._on_connection_closed_unexpectedly(account_key, account_data, f"重连异常: {e}")
    
    def _try_relogin(self, account_data: dict) -> bool:
        """尝试重新登录以刷新cookies"""
        try:
            from Channel.pinduoduo.utils.API.base_request import BaseRequest
            
            shop_id = account_data.get('shop_id')
            user_id = account_data.get('user_id')
            
            if not shop_id or not user_id:
                self.logger.error(f"账号数据缺少shop_id或user_id，无法重新登录")
                return False
            
            # 创建BaseRequest实例并强制重新登录
            base_request = BaseRequest(shop_id, user_id)
            success = base_request.force_relogin()
            
            if success:
                self.logger.info(f"账号 {account_data.get('username')} 重新登录成功")
            else:
                self.logger.error(f"账号 {account_data.get('username')} 重新登录失败")
            
            return success
            
        except Exception as e:
            self.logger.error(f"尝试重新登录时发生错误: {e}")
            return False
    
    def _cancel_reconnect_timer(self, account_key: str):
        """取消重连定时器"""
        if account_key in self.reconnect_timers:
            timer = self.reconnect_timers[account_key]
            timer.stop()
            del self.reconnect_timers[account_key]
    
    def _cleanup_reconnect_state(self, account_key: str):
        """清理重连相关状态"""
        self._cancel_reconnect_timer(account_key)
        if account_key in self.reconnect_attempts:
            del self.reconnect_attempts[account_key]
        if account_key in self.should_auto_reconnect:
            del self.should_auto_reconnect[account_key]
    
    def _on_thread_finished(self, account_key: str):
        """线程结束回调"""
        self.logger.debug(f"账号 {account_key} 自动回复线程已结束")
    
    def get_running_count(self) -> int:
        """获取正在运行的账号数量"""
        return len(self.running_accounts)
    
    def stop_all(self):
        """停止所有自动回复"""
        try:
            # 停止所有正在运行的线程
            for account_key, thread in self.running_accounts.items():
                if thread.is_running():
                    thread.stop()
            
            # 等待所有线程结束
            for thread in self.running_accounts.values():
                thread.wait(5000) # 等待5秒
            
            self.running_accounts.clear()
            self.logger.info("所有自动回复任务已停止")
            
        except Exception as e:
            self.logger.error(f"停止所有自动回复失败: {e}")


class AutoReplyThread(QThread):
    """自动回复线程 - 每个账号独立的WebSocket连接线程"""
    
    connection_success = pyqtSignal()  # 连接成功信号
    connection_failed = pyqtSignal(str)  # 连接失败信号
    connection_closed_unexpectedly = pyqtSignal(str)  # 连接异常关闭信号（触发重连）
    
    def __init__(self, account_data: dict):
        super().__init__()
        self.account_data = account_data
        self.channel = None
        self.logger = get_logger("AutoReplyThread")
        self._is_stopped_by_user = False  # 标记是否被用户主动停止
        
    def run(self):
        """启动后端 PDDChannel 引擎"""
        from Channel.pinduoduo.pdd_chnnel import PDDChannel
        
        # 重置停止标记
        self._is_stopped_by_user = False
        
        try:
            # 为当前线程创建并设置新的事件循环
            self.loop = asyncio.new_event_loop()
            asyncio.set_event_loop(self.loop)
            
            # 创建 PDDChannel 实例
            self.channel = PDDChannel()
            
            # 定义成功和失败的回调函数
            def on_success():
                self.connection_success.emit()

            def on_failure(error_msg, is_connection_closed=False):
                """连接失败回调
                
                Args:
                    error_msg: 错误信息
                    is_connection_closed: 是否是连接异常关闭（需要重连）
                """
                if is_connection_closed and not self._is_stopped_by_user:
                    # 异常关闭且不是用户主动停止，触发重连
                    self.connection_closed_unexpectedly.emit(error_msg)
                else:
                    # 正常失败或用户主动停止
                    self.connection_failed.emit(error_msg)

            # 启动引擎，并传递回调
            task = self.loop.create_task(
                self.channel.start_account(
                    shop_id=self.account_data['shop_id'],
                    user_id=self.account_data['user_id'],
                    on_success=on_success,
                    on_failure=on_failure
                )
            )
            
            # 运行事件循环直到任务完成
            self.loop.run_until_complete(task)

        except Exception as e:
            self.logger.error(f"自动回复线程启动失败: {e}")
            # 检查是否是连接异常关闭
            if not self._is_stopped_by_user:
                self.connection_closed_unexpectedly.emit(str(e))
            else:
                self.connection_failed.emit(str(e))
        finally:
            if hasattr(self, 'loop') and self.loop:
                if self.loop.is_running():
                    self.loop.stop()
                self.loop.close()

    def stop(self):
        """停止后端引擎"""
        try:
            # 标记为用户主动停止
            self._is_stopped_by_user = True
            
            if self.channel:
                self.channel.request_stop()

        except Exception as e:
            self.logger.error(f"停止自动回复线程失败: {e}")
        
    def is_running(self) -> bool:
        """检查线程是否在运行"""
        # 实际的运行状态由 PDDChannel 内部管理，这里仅表示线程是否已启动
        return self.isRunning()


# 全局自动回复管理器实例
auto_reply_manager = AutoReplyManager()


class SetStatusThread(QThread):
    """设置账号状态的线程"""
    
    status_set_success = pyqtSignal(dict, int)  # 设置成功信号
    status_set_failed = pyqtSignal(dict, str)   # 设置失败信号
    
    def __init__(self, account_data: dict, target_status: int):
        super().__init__()
        self.account_data = account_data
        self.target_status = target_status
        self.logger = get_logger()
    def run(self):
        """在后台线程中执行状态更新"""
        try:
            # 1. 从数据库获取最新的账号信息（包括最新的cookies）
            fresh_account = db_manager.get_account(
                self.account_data["channel_name"],
                self.account_data["shop_id"],
                self.account_data["user_id"]
            )
            
            if fresh_account:
                cookies = fresh_account.get("cookies")
            else:
                cookies = self.account_data.get("cookies")
            
            if not cookies:
                raise ValueError("账号缺少cookies，无法设置状态")

            account_monitor = AccountMonitor(cookies)
            
            api_success = account_monitor.set_csstatus(self.target_status)

            if not api_success:
                # API调用失败
                self.status_set_failed.emit(self.account_data, "平台状态设置失败")
                return

            # 2. 更新数据库状态
            db_success = db_manager.update_account_status(
                channel_name=self.account_data["channel_name"],
                shop_id=self.account_data["shop_id"],
                user_id=self.account_data["user_id"],
                status=self.target_status
            )
            
            if db_success:
                # 发射成功信号
                self.status_set_success.emit(self.account_data, self.target_status)
            else:
                # 发射失败信号
                self.status_set_failed.emit(self.account_data, "数据库状态更新失败")
                
        except KeyError:
            # 如果缺少 'user_id' 等关键信息
            self.status_set_failed.emit(self.account_data, "账号数据不完整，无法设置状态")
        except Exception as e:
            # 其他异常
            self.status_set_failed.emit(self.account_data, str(e))


class AutoReplyCard(CardWidget):
    """自动回复卡片组件"""
    
    # 定义信号
    online_clicked = pyqtSignal(dict)  # 上线按钮点击信号
    offline_clicked = pyqtSignal(dict)  # 离线按钮点击信号
    auto_reply_clicked = pyqtSignal(dict)  # 开始自动回复按钮点击信号
    open_backend_clicked = pyqtSignal(dict)  # 打开后台按钮点击信号
    
    def __init__(self, account_data: dict, parent=None):
        super().__init__(parent)
        self.account_data = account_data
        self.shop_id = account_data.get("shop_id", "")
        self.shop_name = account_data.get("shop_name", "")
        self.shop_logo = account_data.get("shop_logo")
        self.account_name = account_data.get("username", "")
        self.platform = account_data.get("channel_name", "")
        self.status = self.getStatusText(account_data.get("status", 0))
        self.auto_reply_status = False  # 自动回复状态
        self.setupUI()
        self.connectSignals()
        self.loadLogo()
        
    def getStatusText(self, status_code: int) -> str:
        """将状态码转换为文本"""
        status_map = {
            0: "休息",
            1: "在线", 
            3: "离线",
            None: "未验证"
        }
        return status_map.get(status_code, "未知")
        
    def setupUI(self):
        """设置卡片UI"""
        # 设置卡片样式
        self.setFixedHeight(120)
        # 主布局
        layout = QHBoxLayout(self)
        layout.setContentsMargins(20, 15, 20, 15)
        layout.setSpacing(20)
        
        # Logo
        logo_widget = self.createLogoWidget()
        
        # 左侧信息区域
        info_widget = self.createInfoWidget()
        
        # 右侧操作区域
        self.action_widget = self.createActionWidget()
        
        # 添加到主布局
        layout.addWidget(logo_widget)
        layout.addWidget(info_widget, 1)
        layout.addWidget(self.action_widget, 0)
    
    def createLogoWidget(self):
        """创建Logo显示区域"""
        self.logo_label = QLabel()
        self.logo_label.setFixedSize(65, 65)
        self.logo_label.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.logo_label.setStyleSheet("border-radius: 30px; border: 1px solid #e0e0e0; background-color: #f5f5f5;")
        self.logo_label.setText("加载中...")
        return self.logo_label
        
    def createInfoWidget(self):
        """创建信息显示区域"""
        info_widget = QWidget()
        info_layout = QVBoxLayout(info_widget)
        info_layout.setSpacing(8)
        info_layout.setContentsMargins(0, 0, 0, 0)

        # 第一行：店铺名称 + 平台标签
        first_row = QWidget()
        first_row_layout = QHBoxLayout(first_row)
        first_row_layout.setContentsMargins(0, 0, 0, 0)
        first_row_layout.setSpacing(10)
        
        # 店铺名称
        shop_name_label = StrongBodyLabel(self.shop_name)
        shop_name_label.setFont(QFont("Microsoft YaHei", 14, QFont.Weight.Bold))
        shop_name_label.setStyleSheet("color: #2c3e50;")
        
        # 平台标签
        platform_badge = InfoBadge.info(self.platform, self)
        platform_badge.setFont(QFont("Microsoft YaHei", 9))

        first_row_layout.addWidget(shop_name_label)
        first_row_layout.addWidget(platform_badge)
        first_row_layout.addStretch()
        
        # 第二行：店铺ID
        second_row = self.createInfoRow("店铺ID:", self.shop_id)
        
        # 第三行：账号名称
        third_row = self.createInfoRow("账号:", self.account_name)
        
        # 添加到布局
        info_layout.addWidget(first_row)
        info_layout.addWidget(second_row)
        info_layout.addWidget(third_row)
        info_layout.addStretch()
        
        return info_widget
    
    def createInfoRow(self, label_text: str, value_text: str):
        """创建信息行"""
        row_widget = QWidget()
        row_layout = QHBoxLayout(row_widget)
        row_layout.setContentsMargins(0, 0, 0, 0)
        row_layout.setSpacing(8)
        
        # 标签
        label = CaptionLabel(label_text)
        label.setStyleSheet("color: #7f8c8d; font-weight: 500;")
        label.setFixedWidth(60)
        
        # 值
        value = BodyLabel(value_text)
        value.setStyleSheet("color: #34495e;")
        
        row_layout.addWidget(label)
        row_layout.addWidget(value)
        row_layout.addStretch()
        
        return row_widget
    
    def createActionWidget(self):
        """创建操作区域"""
        action_widget = QWidget()
        action_layout = QVBoxLayout(action_widget)
        action_layout.setContentsMargins(0, 0, 0, 0)
        action_layout.setSpacing(10)
        action_layout.setAlignment(Qt.AlignmentFlag.AlignTop)
        
        # 状态标签
        status_badge = self.createStatusBadge()
        
        # 操作按钮容器
        buttons_widget = QWidget()
        buttons_layout = QHBoxLayout(buttons_widget)
        buttons_layout.setContentsMargins(0, 0, 0, 0)
        buttons_layout.setSpacing(8)
        
        # 上线按钮
        self.online_btn = PushButton("上线")
        self.online_btn.setIcon(FIF.PLAY)
        self.online_btn.setFixedSize(100, 32)
        
        # 离线按钮
        self.offline_btn = PushButton("离线")
        self.offline_btn.setIcon(FIF.PAUSE)
        self.offline_btn.setFixedSize(100, 32)
       
        # 自动回复按钮
        self.auto_reply_btn = PrimaryPushButton("开始回复")
        self.auto_reply_btn.setIcon(FIF.ROBOT)
        self.auto_reply_btn.setFixedSize(110, 32)

        # 打开后台按钮
        self.open_backend_btn = PushButton("打开后台")
        self.open_backend_btn.setIcon(FIF.LINK)
        self.open_backend_btn.setFixedSize(100, 32)

        buttons_layout.addWidget(self.online_btn)
        buttons_layout.addWidget(self.offline_btn)
        buttons_layout.addWidget(self.auto_reply_btn)
        buttons_layout.addWidget(self.open_backend_btn)
        
        # 添加到操作布局
        action_layout.addWidget(status_badge, 0, Qt.AlignmentFlag.AlignRight)
        action_layout.addWidget(buttons_widget)
        action_layout.addStretch()
        
        return action_widget
    
    def createStatusBadge(self):
        """创建状态标签"""
        if self.status == "在线":
            status_badge = InfoBadge.success("● 在线", self)
        elif self.status == "离线":
            status_badge = InfoBadge.error("● 离线", self)
        elif self.status == "未验证":
            status_badge = InfoBadge.warning("● 未验证", self)
        elif self.status == "休息":
            status_badge = InfoBadge.info("● 休息", self)
        else:
            status_badge = InfoBadge.info(f"● {self.status}", self)
        
        status_badge.setFont(QFont("Microsoft YaHei", 9, QFont.Weight.Bold))
        return status_badge
    
    def connectSignals(self):
        """连接信号"""
        self.online_btn.clicked.connect(lambda: self.online_clicked.emit(self.account_data))
        self.offline_btn.clicked.connect(lambda: self.offline_clicked.emit(self.account_data))
        self.auto_reply_btn.clicked.connect(lambda: self.auto_reply_clicked.emit(self.account_data))
        self.open_backend_btn.clicked.connect(lambda: self.open_backend_clicked.emit(self.account_data))
    
    def setButtonsEnabled(self, enabled: bool):
        """设置按钮是否可用"""
        self.online_btn.setEnabled(enabled)
        self.offline_btn.setEnabled(enabled)
        
    def setButtonLoading(self, button_type: str, loading: bool):
        """设置按钮加载状态"""
        if button_type == "online":
            if loading:
                self.online_btn.setText("设置中...")
                self.online_btn.setEnabled(False)
            else:
                self.online_btn.setText("上线")
                self.online_btn.setEnabled(True)
        elif button_type == "offline":
            if loading:
                self.offline_btn.setText("设置中...")
                self.offline_btn.setEnabled(False)
            else:
                self.offline_btn.setText("离线")
                self.offline_btn.setEnabled(True)
    
    def setAutoReplyStatus(self, is_running: bool):
        """设置自动回复状态"""
        self.auto_reply_status = is_running
        if is_running:
            self.auto_reply_btn.setText("停止回复")
            self.auto_reply_btn.setIcon(FIF.CANCEL)
        else:
            self.auto_reply_btn.setText("开始回复")
            self.auto_reply_btn.setIcon(FIF.ROBOT)
    
    def updateStatus(self, new_status: int):
        """更新账号状态"""
        self.account_data["status"] = new_status
        self.status = self.getStatusText(new_status)
        
        # 重新创建状态标签
        old_badge = self.action_widget.layout().itemAt(0).widget()
        if old_badge:
            old_badge.deleteLater()
            
        new_badge = self.createStatusBadge()
        self.action_widget.layout().insertWidget(0, new_badge, 0, Qt.AlignmentFlag.AlignRight)

    def loadLogo(self):
        """异步加载Logo"""
        if self.shop_logo:
            # 创建并启动Logo加载线程
            self.logo_loader_thread = LogoLoaderThread(self.shop_logo)
            self.logo_loader_thread.logo_loaded.connect(self.setLogo)
            self.logo_loader_thread.start()
        else:
            self.logo_label.setText("无Logo")

    def setLogo(self, pixmap: QPixmap):
        """设置Logo"""
        if not pixmap.isNull():
            self.logo_label.setPixmap(pixmap)
        else:
            self.logo_label.setText("加载失败")


class AutoReplyUI(QFrame):
    """自动回复主界面"""
    
    def __init__(self, parent=None):
        super().__init__(parent=parent)
        self.accounts_data = []  # 存储账号数据
        self.setupUI()
        self.loadAccountsFromDB()
        
        # 设置定时器定期更新统计信息
        self.stats_timer = QTimer()
        self.stats_timer.timeout.connect(self.updateStats)
        self.stats_timer.start(5000)  # 每5秒更新一次
        self.logger = get_logger()
        
        # 设置自动回复管理器的重连回调
        self._setup_reconnect_callbacks()
        
        # 启动后自动开始回复（延迟1秒，等待界面完全加载）
        QTimer.singleShot(1000, self._auto_start_all_auto_reply)
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
        self.refresh_btn.clicked.connect(self.reloadAccounts)
        self.online_all_btn.clicked.connect(self.onOnlineAll)
        self.offline_all_btn.clicked.connect(self.onOfflineAll)
        self.start_all_btn.clicked.connect(self.onStartAllAutoReply)
        self.stop_all_btn.clicked.connect(self.stopAllAutoReply)
        
        # 添加到主布局
        main_layout.addWidget(header_widget)
        main_layout.addWidget(content_widget, 1)
        
        # 设置对象名
        self.setObjectName("自动回复")

        # 持有线程引用，防止被垃圾回收
        self._status_threads: list[SetStatusThread] = []
    
    def createHeaderWidget(self):
        """创建头部区域"""
        header_widget = QWidget()
        header_layout = QHBoxLayout(header_widget)
        header_layout.setContentsMargins(0, 0, 0, 0)
        header_layout.setSpacing(20)
        
        # 标题
        title_label = SubtitleLabel("自动回复管理")
        # 统计信息
        self.stats_label = CaptionLabel("共 0 个账号")
        # 运行状态统计
        self.running_stats_label = CaptionLabel("运行中: 0 个")
        self.running_stats_label.setStyleSheet("color: #28a745; font-weight: bold;")
        
        # 左侧标题区域
        title_area = QWidget()
        title_layout = QVBoxLayout(title_area)
        title_layout.setContentsMargins(0, 0, 0, 0)
        title_layout.setSpacing(5)
        title_layout.addWidget(title_label)
        title_layout.addWidget(self.stats_label)
        title_layout.addWidget(self.running_stats_label)
        
        # 刷新按钮
        self.refresh_btn = PushButton("刷新")
        self.refresh_btn.setIcon(FIF.UPDATE)
        self.refresh_btn.setFixedHeight(34)

        # 上线所有按钮
        self.online_all_btn = PushButton("上线所有")
        self.online_all_btn.setIcon(FIF.PLAY)
        self.online_all_btn.setFixedHeight(34)
        
        # 下线所有按钮
        self.offline_all_btn = PushButton("下线所有")
        self.offline_all_btn.setIcon(FIF.PAUSE)
        self.offline_all_btn.setFixedHeight(34)
        
        # 开始所有按钮
        self.start_all_btn = PrimaryPushButton("开始所有")
        self.start_all_btn.setIcon(FIF.PLAY_SOLID)
        self.start_all_btn.setFixedHeight(34)
        
        # 停止所有按钮
        self.stop_all_btn = PushButton("停止所有")
        self.stop_all_btn.setIcon(FIF.CANCEL)
        self.stop_all_btn.setFixedHeight(34)
        
        # 按钮容器（两行排列）
        buttons_widget = QWidget()
        buttons_layout = QVBoxLayout(buttons_widget)
        buttons_layout.setContentsMargins(0, 0, 0, 0)
        buttons_layout.setSpacing(6)

        # 第一行：上线/下线
        row1 = QHBoxLayout()
        row1.setContentsMargins(0, 0, 0, 0)
        row1.setSpacing(10)
        row1.addWidget(self.online_all_btn)
        row1.addWidget(self.offline_all_btn)

        # 第二行：开始/停止/刷新
        row2 = QHBoxLayout()
        row2.setContentsMargins(0, 0, 0, 0)
        row2.setSpacing(10)
        row2.addWidget(self.start_all_btn)
        row2.addWidget(self.stop_all_btn)
        row2.addWidget(self.refresh_btn)

        buttons_layout.addLayout(row1)
        buttons_layout.addLayout(row2)
        
        # 添加到头部布局
        header_layout.addWidget(title_area)
        header_layout.addStretch()
        header_layout.addWidget(buttons_widget)
        
        return header_widget
    
    def createContentWidget(self):
        """创建内容区域"""
        content_widget = QWidget()
        content_layout = QVBoxLayout(content_widget)
        content_layout.setContentsMargins(0, 0, 0, 0)
        content_layout.setSpacing(0)
        content_layout.setAlignment(Qt.AlignmentFlag.AlignTop)

        # 滚动区域
        self.scroll_area = ScrollArea()
        self.scroll_area.setWidgetResizable(True)
        self.scroll_area.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        self.scroll_area.setVerticalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAsNeeded)
        # 去除边框
        self.scroll_area.setStyleSheet("""
            ScrollArea {
                border: none;
                background-color: transparent;
            }
        """)
        
        # 账号列表容器
        self.accounts_container = QWidget()
        self.accounts_layout = QVBoxLayout(self.accounts_container)
        self.accounts_layout.setSpacing(15)  # 设置账号卡片之间的间距
        self.accounts_layout.setContentsMargins(20, 20, 20, 20)  # 设置容器内边距
        self.accounts_layout.setAlignment(Qt.AlignmentFlag.AlignTop)  # 顶部对齐
        
        # 设置容器样式，去除边框
        self.accounts_container.setStyleSheet("""
            QWidget {
                background-color: transparent;
                border: none;
            }
        """)
        
        # 设置滚动区域内容
        self.scroll_area.setWidget(self.accounts_container)
        
        # 添加到内容布局
        content_layout.addWidget(self.scroll_area)
        
        return content_widget
    
    def loadAccountsFromDB(self):
        """从数据库加载账号数据"""
        try:
            self.accounts_data.clear()  # 使用 clear() 替代重新赋值
            
            channels = db_manager.get_all_channels()
            
            for channel in channels:
                channel_name = channel["channel_name"]
                
                # 获取该渠道下的所有店铺
                shops = db_manager.get_shops_by_channel(channel_name)
                
                for shop in shops:
                    shop_id = shop["shop_id"]
                    
                    # 获取该店铺下的所有账号
                    accounts = db_manager.get_accounts_by_shop(channel_name, shop_id)
                    
                    for account in accounts:
                        account_data = {
                            "channel_name": channel_name,
                            "shop_id": shop_id,
                            "shop_name": shop["shop_name"],
                            "shop_logo": shop.get("shop_logo"),
                            "username": account["username"],
                            "password": account["password"],
                            "status": account["status"],
                            "user_id": account["user_id"],
                            "cookies": account["cookies"]
                        }
                        self.accounts_data.append(account_data)
                        
            self.refreshAccountList()
            
        except Exception as e:
            self.logger.error(f"加载账号数据失败: {e}")
    
    def refreshAccountList(self):
        """刷新账号列表"""
        # 清空现有卡片
        self.clearAccountList()
        
        # 添加账号卡片
        for account_data in self.accounts_data:
            account_card = AutoReplyCard(account_data)
            
            # 连接卡片信号
            account_card.online_clicked.connect(self.onAccountOnline)
            account_card.offline_clicked.connect(self.onAccountOffline)
            account_card.auto_reply_clicked.connect(self.onAutoReplyToggle)
            account_card.open_backend_clicked.connect(self.onOpenBackend)
            
            # 检查并设置自动回复状态
            if auto_reply_manager.is_running(account_data):
                account_card.setAutoReplyStatus(True)
            
            self.accounts_layout.addWidget(account_card)
        
        # 添加弹性空间
        self.accounts_layout.addStretch()
        
        # 更新统计信息
        self.updateStats()
    
    def clearAccountList(self):
        """清空账号列表"""
        while self.accounts_layout.count():
            child = self.accounts_layout.takeAt(0)
            if child.widget():
                child.widget().deleteLater()
    
    def updateStats(self):
        """更新统计信息"""
        count = len(self.accounts_data)
        running_count = auto_reply_manager.get_running_count()
        self.stats_label.setText(f"共 {count} 个账号")
        self.running_stats_label.setText(f"运行中: {running_count} 个")
    
    def reloadAccounts(self):
        """重新加载账号"""
        self.loadAccountsFromDB()
    
    def onOpenBackend(self, account_data: dict):
        """打开商家后台 - 使用已保存的用户数据目录"""
        try:
            username = account_data.get("username", "")
            user_data_dir = os.path.abspath(f"./user_data/{username}")
            
            if not os.path.exists(user_data_dir):
                QMessageBox.warning(self, "提示", f"未找到账号 '{username}' 的登录数据\n请先通过程序登录一次")
                return
            
            # 在后台线程中启动浏览器，避免阻塞UI
            threading.Thread(
                target=self._open_backend_browser,
                args=(user_data_dir,),
                daemon=True
            ).start()
            
        except Exception as e:
            self.logger.error(f"打开后台失败: {e}")
            QMessageBox.critical(self, "错误", f"打开后台失败: {e}")
    
    def _open_backend_browser(self, user_data_dir: str):
        """在后台线程中用 Playwright 打开浏览器"""
        import subprocess
        import sys
        
        # 用独立进程打开浏览器，避免阻塞主程序
        # 注意：user_data_dir 通过参数传入，不在 f-string 中拼接路径，避免 \U 转义问题
        script = '''
import asyncio
import sys
from playwright.async_api import async_playwright

async def main():
    user_data_dir = sys.argv[1]
    async with async_playwright() as p:
        context = await p.chromium.launch_persistent_context(
            user_data_dir,
            headless=False,
            args=[
                "--disable-gpu",
                "--no-sandbox",
                "--disable-dev-shm-usage",
                "--disable-blink-features=AutomationControlled",
                "--disable-notifications",
            ]
        )
        page = context.pages[0] if context.pages else await context.new_page()
        await page.goto("https://mms.pinduoduo.com/home/")
        try:
            while True:
                await asyncio.sleep(1)
        except asyncio.CancelledError:
            pass
        finally:
            await context.close()

asyncio.run(main())
'''
        subprocess.Popen(
            [sys.executable, "-c", script, user_data_dir],
            creationflags=subprocess.CREATE_NEW_PROCESS_GROUP
        )
    
    def onOnlineAll(self):
        """上线所有符合条件的账号"""
        try:
            # 筛选出可以上线的账号（当前非在线状态）
            eligible_accounts = [
                acc_data for acc_data in self.accounts_data
                if acc_data.get("status") != 1
            ]

            if not eligible_accounts:
                QMessageBox.information(self, "提示", "没有需要上线的账号")
                return

            reply = QMessageBox.question(
                self,
                "确认上线",
                f"找到 {len(eligible_accounts)} 个非在线账号。确定要全部上线吗？",
                QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
                QMessageBox.StandardButton.No
            )

            if reply == QMessageBox.StandardButton.No:
                return

            # 设置按钮为加载状态
            self.online_all_btn.setText("上线中...")
            self.online_all_btn.setEnabled(False)

            # 统计结果
            self._batch_status_results = {"total": len(eligible_accounts), "success": 0, "failed": 0}
            self._batch_status_pending = len(eligible_accounts)

            for account_data in eligible_accounts:
                thread = SetStatusThread(account_data, 1)  # 1表示在线
                self._status_threads.append(thread)
                thread.finished.connect(lambda: self._status_threads.remove(thread) if thread in self._status_threads else None)
                thread.status_set_success.connect(self._on_batch_status_success)
                thread.status_set_failed.connect(self._on_batch_status_failed)
                thread.start()

        except Exception as e:
            self.logger.error(f"上线所有失败: {str(e)}")
            self.online_all_btn.setText("上线所有")
            self.online_all_btn.setEnabled(True)
            QMessageBox.critical(self, "错误", f"上线所有失败：{str(e)}")

    def onOfflineAll(self):
        """下线所有符合条件的账号"""
        try:
            # 筛选出可以下线的账号（当前非离线状态）
            eligible_accounts = [
                acc_data for acc_data in self.accounts_data
                if acc_data.get("status") != 3
            ]

            if not eligible_accounts:
                QMessageBox.information(self, "提示", "没有需要下线的账号")
                return

            reply = QMessageBox.question(
                self,
                "确认下线",
                f"找到 {len(eligible_accounts)} 个非离线账号。确定要全部下线吗？\n\n注意：下线后自动回复也会停止。建议先停止自动回复再下线。",
                QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
                QMessageBox.StandardButton.No
            )

            if reply == QMessageBox.StandardButton.No:
                return

            # 设置按钮为加载状态
            self.offline_all_btn.setText("下线中...")
            self.offline_all_btn.setEnabled(False)

            # 统计结果
            self._batch_status_results = {"total": len(eligible_accounts), "success": 0, "failed": 0}
            self._batch_status_pending = len(eligible_accounts)

            for account_data in eligible_accounts:
                thread = SetStatusThread(account_data, 3)  # 3表示离线
                self._status_threads.append(thread)
                thread.finished.connect(lambda: self._status_threads.remove(thread) if thread in self._status_threads else None)
                thread.status_set_success.connect(self._on_batch_status_success)
                thread.status_set_failed.connect(self._on_batch_status_failed)
                thread.start()

        except Exception as e:
            self.logger.error(f"下线所有失败: {str(e)}")
            self.offline_all_btn.setText("下线所有")
            self.offline_all_btn.setEnabled(True)
            QMessageBox.critical(self, "错误", f"下线所有失败：{str(e)}")

    def _on_batch_status_success(self, account_data: dict, new_status: int):
        """批量状态设置 - 单个成功回调"""
        try:
            self._batch_status_results["success"] += 1
            self._batch_status_pending -= 1

            # 更新单个卡片状态
            self.updateCardStatus(account_data, new_status)

            # 检查是否全部完成
            self._check_batch_status_complete(new_status)
        except Exception as e:
            self.logger.error(f"批量状态设置成功回调失败: {e}")

    def _on_batch_status_failed(self, account_data: dict, error_message: str):
        """批量状态设置 - 单个失败回调"""
        try:
            self._batch_status_results["failed"] += 1
            self._batch_status_pending -= 1
            self.logger.error(f"账号 '{account_data.get('username')}' 状态设置失败：{error_message}")

            # 检查是否全部完成
            target_status = 1 if self.online_all_btn.text() == "上线中..." else 3
            self._check_batch_status_complete(target_status)
        except Exception as e:
            self.logger.error(f"批量状态设置失败回调失败: {e}")

    def _check_batch_status_complete(self, target_status: int):
        """检查批量状态设置是否全部完成"""
        if self._batch_status_pending <= 0:
            results = self._batch_status_results
            status_text = "上线" if target_status == 1 else "下线"

            # 恢复按钮状态
            if target_status == 1:
                self.online_all_btn.setText("上线所有")
                self.online_all_btn.setEnabled(True)
            else:
                self.offline_all_btn.setText("下线所有")
                self.offline_all_btn.setEnabled(True)

            # 从数据库重新加载账号数据以同步最新状态
            self.loadAccountsFromDB()

            # 显示结果
            msg = f"{status_text}完成：成功 {results['success']} 个"
            if results["failed"] > 0:
                msg += f"，失败 {results['failed']} 个"
            self.logger.info(msg)

            if results["failed"] == 0:
                QMessageBox.information(self, "操作完成", msg)
            else:
                QMessageBox.warning(self, "操作完成", msg + "\n\n失败的账号可能因cookies过期导致，请检查后重试。")

    def _auto_start_all_auto_reply(self):
        """启动后自动开始所有账号的自动回复（无需用户确认）"""
        try:
            # 1. 筛选出可以启动的账号（未在运行中的）
            non_running_accounts = [
                acc_data for acc_data in self.accounts_data
                if not auto_reply_manager.is_running(acc_data)
            ]

            # 2. 如果没有可启动的账号，直接返回
            if not non_running_accounts:
                self.logger.info("没有需要自动启动的账号")
                return

            self.logger.info(f"自动启动：找到 {len(non_running_accounts)} 个账号")

            # 3. 先把非在线的账号上线
            need_online = [
                acc_data for acc_data in non_running_accounts
                if acc_data.get("status") != 1
            ]

            if need_online:
                # 有需要上线的账号，先批量上线
                self.logger.info(f"自动启动：需要先上线 {len(need_online)} 个账号")
                self._auto_start_accounts_to_start = non_running_accounts
                self._auto_start_online_pending = len(need_online)
                self._auto_start_online_results = {"total": len(need_online), "success": 0, "failed": 0}

                for account_data in need_online:
                    thread = SetStatusThread(account_data, 1)  # 1表示在线
                    self._status_threads.append(thread)
                    thread.finished.connect(lambda t=thread: self._status_threads.remove(t) if t in self._status_threads else None)
                    thread.status_set_success.connect(self._on_auto_start_online_success)
                    thread.status_set_failed.connect(self._on_auto_start_online_failed)
                    thread.start()
            else:
                # 所有账号都已在线，直接启动自动回复
                self.logger.info("自动启动：所有账号已在线，直接启动自动回复")
                self._do_auto_start_all_auto_reply(non_running_accounts)

        except Exception as e:
            self.logger.error(f"自动启动所有自动回复失败: {str(e)}")

    def _on_auto_start_online_success(self, account_data: dict, new_status: int):
        """自动启动 - 单个账号上线成功回调"""
        try:
            self._auto_start_online_results["success"] += 1
            self._auto_start_online_pending -= 1
            self.updateCardStatus(account_data, new_status)
            self._check_auto_start_online_complete()
        except Exception as e:
            self.logger.error(f"自动启动-上线成功回调失败: {e}")

    def _on_auto_start_online_failed(self, account_data: dict, error_message: str):
        """自动启动 - 单个账号上线失败回调"""
        try:
            self._auto_start_online_results["failed"] += 1
            self._auto_start_online_pending -= 1
            self.logger.error(f"自动启动：账号 '{account_data.get('username')}' 上线失败：{error_message}")
            self._check_auto_start_online_complete()
        except Exception as e:
            self.logger.error(f"自动启动-上线失败回调失败: {e}")

    def _check_auto_start_online_complete(self):
        """检查自动启动的上线是否全部完成，完成后启动自动回复"""
        if self._auto_start_online_pending <= 0:
            results = self._auto_start_online_results
            self.logger.info(f"自动启动：上线完成，成功 {results['success']} 个，失败 {results['failed']} 个")

            # 从数据库重新加载以获取最新状态
            self.loadAccountsFromDB()

            # 筛选出已成功上线的账号来启动自动回复
            accounts_to_start = [
                acc_data for acc_data in self._auto_start_accounts_to_start
                if acc_data.get("status") == 1 and not auto_reply_manager.is_running(acc_data)
            ]

            self._do_auto_start_all_auto_reply(accounts_to_start)

    def _do_auto_start_all_auto_reply(self, accounts: list):
        """自动启动 - 实际执行启动自动回复"""
        if not accounts:
            self.logger.info("自动启动：没有可启动的账号")
            return

        started_count = 0
        for account_data in accounts:
            success = auto_reply_manager.start_auto_reply(account_data)
            if success:
                started_count += 1
                self._connect_auto_reply_signals(account_data)

        # 更新UI
        self._update_all_cards_auto_reply_status()
        self.updateStats()

        self.logger.info(f"自动启动完成：成功启动 {started_count} / {len(accounts)} 个账号的自动回复")

    def onStartAllAutoReply(self):
        """开始所有账号的自动回复（自动上线非在线账号）"""
        try:
            # 1. 筛选出可以启动的账号（未在运行中的）
            non_running_accounts = [
                acc_data for acc_data in self.accounts_data
                if not auto_reply_manager.is_running(acc_data)
            ]

            # 2. 如果没有可启动的账号，提示用户
            if not non_running_accounts:
                QMessageBox.information(self, "提示", "没有需要启动的账号。\n\n所有账号已在自动回复中。")
                return

            # 3. 确认对话框
            reply = QMessageBox.question(
                self,
                "确认开始",
                f"找到 {len(non_running_accounts)} 个未运行的账号，将自动上线非在线账号后开始自动回复。\n确定要继续吗？",
                QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
                QMessageBox.StandardButton.No
            )

            if reply == QMessageBox.StandardButton.No:
                return

            # 4. 设置按钮为加载状态
            self.start_all_btn.setText("启动中...")
            self.start_all_btn.setEnabled(False)

            # 5. 先把非在线的账号上线
            need_online = [
                acc_data for acc_data in non_running_accounts
                if acc_data.get("status") != 1
            ]

            if need_online:
                # 有需要上线的账号，先批量上线
                self._start_all_accounts_to_start = non_running_accounts
                self._start_all_online_pending = len(need_online)
                self._start_all_online_results = {"total": len(need_online), "success": 0, "failed": 0}

                for account_data in need_online:
                    thread = SetStatusThread(account_data, 1)  # 1表示在线
                    self._status_threads.append(thread)
                    thread.finished.connect(lambda: self._status_threads.remove(thread) if thread in self._status_threads else None)
                    thread.status_set_success.connect(self._on_start_all_online_success)
                    thread.status_set_failed.connect(self._on_start_all_online_failed)
                    thread.start()
            else:
                # 所有账号都已在线，直接启动自动回复
                self._do_start_all_auto_reply(non_running_accounts)

        except Exception as e:
            self.logger.error(f"开始所有自动回复失败: {str(e)}")
            self.start_all_btn.setText("开始所有")
            self.start_all_btn.setEnabled(True)
            QMessageBox.critical(self, "错误", f"开始所有自动回复失败：{str(e)}")

    def _on_start_all_online_success(self, account_data: dict, new_status: int):
        """开始所有 - 单个账号上线成功回调"""
        try:
            self._start_all_online_results["success"] += 1
            self._start_all_online_pending -= 1
            self.updateCardStatus(account_data, new_status)
            self._check_start_all_online_complete()
        except Exception as e:
            self.logger.error(f"开始所有-上线成功回调失败: {e}")

    def _on_start_all_online_failed(self, account_data: dict, error_message: str):
        """开始所有 - 单个账号上线失败回调"""
        try:
            self._start_all_online_results["failed"] += 1
            self._start_all_online_pending -= 1
            self.logger.error(f"账号 '{account_data.get('username')}' 上线失败：{error_message}")
            self._check_start_all_online_complete()
        except Exception as e:
            self.logger.error(f"开始所有-上线失败回调失败: {e}")

    def _check_start_all_online_complete(self):
        """检查上线是否全部完成，完成后启动自动回复"""
        if self._start_all_online_pending <= 0:
            results = self._start_all_online_results
            self.logger.info(f"上线完成：成功 {results['success']} 个，失败 {results['failed']} 个")

            if results["failed"] > 0:
                QMessageBox.warning(self, "部分上线失败",
                    f"有 {results['failed']} 个账号上线失败（可能cookies过期）。\n\n将继续为已成功的账号启动自动回复。")

            # 从数据库重新加载以获取最新状态
            self.loadAccountsFromDB()

            # 筛选出已成功上线的账号来启动自动回复
            accounts_to_start = [
                acc_data for acc_data in self._start_all_accounts_to_start
                if acc_data.get("status") == 1 and not auto_reply_manager.is_running(acc_data)
            ]

            self._do_start_all_auto_reply(accounts_to_start)

    def _do_start_all_auto_reply(self, accounts: list):
        """实际执行启动自动回复"""
        if not accounts:
            self.start_all_btn.setText("开始所有")
            self.start_all_btn.setEnabled(True)
            QMessageBox.information(self, "提示", "没有可启动的账号。")
            return

        started_count = 0
        for account_data in accounts:
            success = auto_reply_manager.start_auto_reply(account_data)
            if success:
                started_count += 1
                self._connect_auto_reply_signals(account_data)

        # 恢复按钮并更新UI
        self.start_all_btn.setText("开始所有")
        self.start_all_btn.setEnabled(True)
        self._update_all_cards_auto_reply_status()
        self.updateStats()

        QMessageBox.information(self, "操作完成",
            f"已成功为 {started_count} / {len(accounts)} 个账号启动自动回复。")

    def stopAllAutoReply(self):
        """停止所有自动回复"""
        try:
            running_count = auto_reply_manager.get_running_count()
            
            if running_count == 0:
                QMessageBox.information(self, "提示", "当前没有正在运行的自动回复")
                return
            
            # 确认对话框
            reply = QMessageBox.question(
                self, 
                "确认停止", 
                f"确定要停止所有 {running_count} 个正在运行的自动回复吗？",
                QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
                QMessageBox.StandardButton.No
            )
            
            if reply == QMessageBox.StandardButton.Yes:
                # 停止所有自动回复
                auto_reply_manager.stop_all()
                
                # 更新所有卡片状态
                self._update_all_cards_auto_reply_status()
                
                # 更新统计信息
                self.updateStats()
                
                QMessageBox.information(self, "成功", "已停止所有自动回复")
                
        except Exception as e:
            self.logger.error(f"停止所有自动回复失败: {str(e)}")
            QMessageBox.critical(self, "错误", f"停止所有自动回复失败：{str(e)}")
    
    def _update_all_cards_auto_reply_status(self):
        """更新所有卡片的自动回复状态"""
        try:
            for i in range(self.accounts_layout.count() - 1):  # -1 因为最后一个是stretch
                widget = self.accounts_layout.itemAt(i).widget()
                if isinstance(widget, AutoReplyCard):
                    # 检查实际运行状态
                    is_running = auto_reply_manager.is_running(widget.account_data)
                    widget.setAutoReplyStatus(is_running)
                    
        except Exception as e:
            self.logger.error(f"更新卡片状态失败: {str(e)}")
    
    def onAccountOnline(self, account_data: dict):
        """账号上线回调"""
        try:
            # 找到对应的卡片
            account_card = self.findAccountCard(account_data)
            if account_card:
                account_card.setButtonLoading("online", True)
            
            # 创建设置状态线程
            thread = SetStatusThread(account_data, 1)  # 1表示在线
            self._status_threads.append(thread)
            thread.finished.connect(lambda: self._status_threads.remove(thread) if thread in self._status_threads else None)
            
            # 连接信号
            thread.status_set_success.connect(self.onStatusSetSuccess)
            thread.status_set_failed.connect(self.onStatusSetFailed)
            
            # 启动线程
            thread.start()
            
        except Exception as e:
            self.logger.error(f"启动上线操作失败: {str(e)}")
            QMessageBox.critical(self, "错误", f"启动上线操作失败：{str(e)}")
    
    def onAccountOffline(self, account_data: dict):
        """账号离线回调"""
        try:
            # 找到对应的卡片
            account_card = self.findAccountCard(account_data)
            if account_card:
                account_card.setButtonLoading("offline", True)
            
            # 创建设置状态线程
            thread = SetStatusThread(account_data, 3)  # 3表示离线
            self._status_threads.append(thread)
            thread.finished.connect(lambda: self._status_threads.remove(thread) if thread in self._status_threads else None)
            
            # 连接信号
            thread.status_set_success.connect(self.onStatusSetSuccess)
            thread.status_set_failed.connect(self.onStatusSetFailed)
            
            # 启动线程
            thread.start()
            
        except Exception as e:
            self.logger.error(f"启动离线操作失败: {str(e)}")
            QMessageBox.critical(self, "错误", f"启动离线操作失败：{str(e)}")
    
    def findAccountCard(self, account_data: dict):
        """查找对应的账号卡片"""
        for i in range(self.accounts_layout.count() - 1):  # -1 因为最后一个是stretch
            widget = self.accounts_layout.itemAt(i).widget()
            if isinstance(widget, AutoReplyCard) and widget.account_data == account_data:
                return widget
        return None
    
    def onStatusSetSuccess(self, account_data: dict, new_status: int):
        """状态设置成功回调"""
        try:
            # 找到对应的卡片
            account_card = self.findAccountCard(account_data)
            if account_card:
                # 恢复按钮状态
                account_card.setButtonLoading("online", False)
                account_card.setButtonLoading("offline", False)
            
            # 更新卡片状态
            self.updateCardStatus(account_data, new_status)
            
            # 显示成功消息
            status_text = "在线" if new_status == 1 else "离线"
            self.logger.info(f"账号 '{account_data['username']}' 已成功设置为{status_text}状态")
            
        except Exception as e:
            self.logger.error(f"处理状态设置成功回调失败: {str(e)}")
    
    def onStatusSetFailed(self, account_data: dict, error_message: str):
        """状态设置失败回调"""
        try:
            # 找到对应的卡片
            account_card = self.findAccountCard(account_data)
            if account_card:
                # 恢复按钮状态
                account_card.setButtonLoading("online", False)
                account_card.setButtonLoading("offline", False)
            
            # 显示失败消息
            self.logger.error(f"设置账号 '{account_data['username']}' 状态失败：{error_message}")
            QMessageBox.warning(self, "失败", f"设置账号 '{account_data['username']}' 状态失败：{error_message}")
            
        except Exception as e:
            self.logger.error(f"处理状态设置失败回调失败: {str(e)}")
    
    def onAutoReplyToggle(self, account_data: dict):
        """自动回复开关回调"""
        try:
            # 找到对应的卡片
            account_card = self.findAccountCard(account_data)
            if not account_card:
                self.logger.error("找不到对应的账号卡片")
                return
            
            # 检查当前自动回复状态
            current_status = auto_reply_manager.is_running(account_data)
            
            if current_status:
                # 当前正在运行，需要停止
                self._stop_auto_reply(account_data, account_card)
            else:
                # 当前未运行，需要启动
                self._start_auto_reply(account_data, account_card)
                
        except Exception as e:
            self.logger.error(f"自动回复开关操作失败: {str(e)}")
            QMessageBox.critical(self, "错误", f"自动回复操作失败：{str(e)}")
    
    def _start_auto_reply(self, account_data: dict, account_card):
        """启动自动回复"""
        try:
            # 检查账号状态，只有在线状态才能启动自动回复
            if account_data.get("status") != 1:
                QMessageBox.warning(self, "提示", "账号必须先上线才能开始自动回复！")
                return
            
            # 设置按钮为加载状态
            account_card.auto_reply_btn.setText("启动中...")
            account_card.auto_reply_btn.setEnabled(False)
            
            # 启动自动回复
            success = auto_reply_manager.start_auto_reply(account_data)
            
            if success:
                # 更新UI状态
                account_card.setAutoReplyStatus(True)
                self.logger.info(f"账号 '{account_data['username']}' 自动回复启动成功")
                
                # 连接自动回复管理器的信号（如果需要的话）
                self._connect_auto_reply_signals(account_data)
                
            else:
                # 启动失败，恢复按钮状态
                account_card.auto_reply_btn.setText("开始回复")
                account_card.auto_reply_btn.setEnabled(True)
                QMessageBox.warning(self, "失败", f"启动账号 '{account_data['username']}' 自动回复失败！")
                
        except Exception as e:
            self.logger.error(f"启动自动回复失败: {str(e)}")
            account_card.auto_reply_btn.setText("开始回复")
            account_card.auto_reply_btn.setEnabled(True)
            QMessageBox.critical(self, "错误", f"启动自动回复失败：{str(e)}")
    
    def _stop_auto_reply(self, account_data: dict, account_card):
        """停止自动回复"""
        try:
            # 设置按钮为加载状态
            account_card.auto_reply_btn.setText("停止中...")
            account_card.auto_reply_btn.setEnabled(False)
            
            # 停止自动回复
            success = auto_reply_manager.stop_auto_reply(account_data)
            
            # 无论成功与否，都更新UI状态（因为已经从管理器中移除）
            account_card.setAutoReplyStatus(False)
            
            if success:
                self.logger.info(f"账号 '{account_data['username']}' 自动回复停止成功")
            else:
                self.logger.warning(f"账号 '{account_data['username']}' 自动回复停止可能未完全成功，但已从管理器中移除")
            
            # 更新统计信息
            self.updateStats()
                
        except Exception as e:
            self.logger.error(f"停止自动回复失败: {str(e)}")
            # 即使出错也要恢复按钮状态
            account_card.setAutoReplyStatus(False)
            QMessageBox.critical(self, "错误", f"停止自动回复失败：{str(e)}")
            # 更新统计信息
            self.updateStats()
    
    def _connect_auto_reply_signals(self, account_data: dict):
        """连接自动回复相关信号"""
        try:
            account_key = f"{account_data['channel_name']}_{account_data['shop_id']}_{account_data['username']}"
            
            if account_key in auto_reply_manager.running_accounts:
                thread = auto_reply_manager.running_accounts[account_key]
                
                # 连接成功和失败信号
                thread.connection_success.connect(
                    lambda: self._on_auto_reply_success(account_data)
                )
                thread.connection_failed.connect(
                    lambda error: self._on_auto_reply_failed(account_data, error)
                )
                
        except Exception as e:
            self.logger.error(f"连接自动回复信号失败: {str(e)}")
    
    def _on_auto_reply_success(self, account_data: dict):
        """自动回复连接成功回调"""
        try:
            account_card = self.findAccountCard(account_data)
            if account_card:
                account_card.auto_reply_btn.setText("停止回复")
                account_card.auto_reply_btn.setEnabled(True)
                
            self.logger.info(f"账号 '{account_data['username']}' 自动回复连接成功")
            
            # 更新统计信息
            self.updateStats()
            
        except Exception as e:
            self.logger.error(f"处理自动回复成功回调失败: {str(e)}")
    
    def _on_auto_reply_failed(self, account_data: dict, error: str):
        """自动回复连接失败回调"""
        try:
            account_card = self.findAccountCard(account_data)
            if account_card:
                account_card.setAutoReplyStatus(False)
                account_card.auto_reply_btn.setText("开始回复")
                account_card.auto_reply_btn.setEnabled(True)
            
            self.logger.error(f"账号 '{account_data['username']}' 自动回复连接失败: {error}")
            QMessageBox.warning(self, "连接失败", f"账号 '{account_data['username']}' 自动回复连接失败：{error}")
            
            # 更新统计信息
            self.updateStats()
            
        except Exception as e:
            self.logger.error(f"处理自动回复失败回调失败: {str(e)}")
    
    def updateCardStatus(self, account_data: dict, new_status: int):
        """更新卡片状态"""
        for i in range(self.accounts_layout.count() - 1):  # -1 因为最后一个是stretch
            widget = self.accounts_layout.itemAt(i).widget()
            if isinstance(widget, AutoReplyCard) and widget.account_data == account_data:
                widget.updateStatus(new_status)
                break
    
    def _setup_reconnect_callbacks(self):
        """设置自动重连回调"""
        try:
            auto_reply_manager.on_reconnecting_callback = self._on_reconnecting
            auto_reply_manager.on_reconnect_failed_callback = self._on_reconnect_failed
            auto_reply_manager.on_reconnect_success_callback = self._on_reconnect_success
            self.logger.debug("已设置自动重连回调")
        except Exception as e:
            self.logger.error(f"设置重连回调失败: {e}")
    
    def _on_reconnecting(self, account_data: dict, attempt: int, max_attempts: int, delay: float):
        """正在重连回调"""
        try:
            account_card = self.findAccountCard(account_data)
            if account_card:
                # 显示重连状态
                account_card.auto_reply_btn.setText(f"重连中({attempt}/{max_attempts})...")
                account_card.auto_reply_btn.setEnabled(False)
            
            self.logger.info(f"账号 '{account_data['username']}' WebSocket连接断开，{delay:.1f}秒后进行第{attempt}次重连")
            
        except Exception as e:
            self.logger.error(f"处理重连中回调失败: {e}")
    
    def _on_reconnect_failed(self, account_data: dict, error: str):
        """重连失败回调"""
        try:
            account_card = self.findAccountCard(account_data)
            if account_card:
                # 恢复按钮状态
                account_card.setAutoReplyStatus(False)
                account_card.auto_reply_btn.setText("开始回复")
                account_card.auto_reply_btn.setEnabled(True)
            
            self.logger.error(f"账号 '{account_data['username']}' 自动回复重连失败: {error}")
            QMessageBox.warning(self, "重连失败", f"账号 '{account_data['username']}' 自动回复重连失败：{error}\n请检查网络连接或手动重新启动自动回复。")
            
            # 更新统计信息
            self.updateStats()
            
        except Exception as e:
            self.logger.error(f"处理重连失败回调失败: {e}")
    
    def _on_reconnect_success(self, account_data: dict):
        """重连成功回调"""
        try:
            account_card = self.findAccountCard(account_data)
            if account_card:
                # 更新按钮状态
                account_card.auto_reply_btn.setText("停止回复")
                account_card.auto_reply_btn.setEnabled(True)
            
            self.logger.info(f"账号 '{account_data['username']}' 自动回复重连成功")
            
            # 更新统计信息
            self.updateStats()
            
        except Exception as e:
            self.logger.error(f"处理重连成功回调失败: {e}") 