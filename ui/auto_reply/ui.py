# 自动回复主界面模块
import time
from PyQt6.QtCore import Qt, QTimer, QEvent
from PyQt6.QtWidgets import QFrame, QWidget, QMessageBox, QVBoxLayout, QHBoxLayout
from PyQt6.QtGui import QFont
from qfluentwidgets import (SubtitleLabel, CaptionLabel, PushButton, PrimaryPushButton,
                          ScrollArea, FluentIcon as FIF, isDarkTheme)
from utils.logger_loguru import get_logger
from database.db_manager import db_manager
from config import config
from .card import AutoReplyCard
from .manager import auto_reply_manager
from .threads import SetStatusThread


class AutoReplyUI(QFrame):
    """自动回复主界面"""

    def __init__(self, parent=None):
        super().__init__(parent=parent)
        self.logger = get_logger()  # 初始化logger（必须在其他操作之前）
        self.accounts_data: list = []  # 存储账号数据
        self._loaded_once = False
        self.setupUI()
        QTimer.singleShot(300, self._maybeLoadOnShow)

        # 设置定时器定期更新统计信息
        self.stats_timer = QTimer()
        self.stats_timer.timeout.connect(self.updateStats)
        self.stats_timer.start(5000)  # 每5秒更新一次

        # 设置定时器定期同步自动回复状态（减少检查频率，避免过度同步）
        self.sync_timer = QTimer()
        self.sync_timer.timeout.connect(self._sync_auto_reply_status)
        self.sync_timer.start(10000)  # 每10秒同步一次状态

    def closeEvent(self, event):
        """窗口关闭时清理定时器和线程"""
        self.cleanup()
        event.accept()

    def cleanup(self):
        """程序退出时清理所有资源"""
        try:
            if hasattr(self, 'stats_timer') and self.stats_timer:
                self.stats_timer.stop()
            if hasattr(self, 'sync_timer') and self.sync_timer:
                self.sync_timer.stop()
            if hasattr(self, 'status_thread') and self.status_thread and self.status_thread.isRunning():
                self.status_thread.requestInterruption()
                self.status_thread.wait(3000)

            # 停止所有自动回复线程
            auto_reply_manager.stop_all()

            # 清理所有账号卡片的线程
            for i in range(self.accounts_layout.count()):
                item = self.accounts_layout.itemAt(i)
                if item is None:
                    continue
                widget = item.widget()
                if isinstance(widget, AutoReplyCard):
                    widget.cleanup()
        except Exception as e:
            self.logger.error(f"清理自动回复界面失败: {e}")

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
        self._palette_pending = False
        self._update_label_styles()

    def _update_label_styles(self):
        """更新标签样式以适配当前主题"""
        try:
            if isDarkTheme():
                self.running_stats_label.setStyleSheet("font-weight: bold; color: #ffffff;")
                self.stats_label.setStyleSheet("color: #cccccc;")
            else:
                self.running_stats_label.setStyleSheet("font-weight: bold;")
                self.stats_label.setStyleSheet("")
        except Exception as e:
            self.logger.warning(f"更新标签样式失败: {e}")

    def showEvent(self, event):
        super().showEvent(event)
        self._maybeLoadOnShow()

    def _maybeLoadOnShow(self):
        if not self._loaded_once and self.isVisible():
            self._loaded_once = True
            self._loadAccountsAsync()

            # 检查是否启用启动时自动开始回复
            if config.get("auto_start_on_launch", False):
                # 延迟执行，确保 UI 完全加载
                QTimer.singleShot(500, self._autoStartAllReply)

    def _loadAccountsAsync(self):
        """在后台线程加载账号数据，避免阻塞主线程"""
        import time as _t
        _t0 = _t.perf_counter()

        class _AccountLoader(QThread):
            from PyQt6.QtCore import QThread, pyqtSignal
            result = pyqtSignal(list)

            def run(self):
                try:
                    from database.db_manager import get_db_manager
                    db = get_db_manager()
                    accounts = db.get_all_accounts_flat()
                except Exception:
                    accounts = []
                self.result.emit(accounts)

        self._account_loader = _AccountLoader(self)
        self._account_loader.result.connect(self._on_accounts_loaded)
        self._account_loader.start()
        self.logger.info(f"  _loadAccountsAsync 启动耗时: {_t.perf_counter()-_t0:.3f}s")

    def _on_accounts_loaded(self, accounts: list):
        """后台线程加载完成后，在主线程更新 UI"""
        self.logger.info(f"  账号数据加载完成，共 {len(accounts)} 条，开始渲染卡片...")
        t = time.perf_counter()
        self.accounts_data = accounts
        self.refreshAccountList()
        self.logger.info(f"  卡片渲染耗时: {time.perf_counter()-t:.2f}s")

    def setupUI(self):
        """设置主界面UI"""
        main_layout = QVBoxLayout(self)
        main_layout.setContentsMargins(30, 30, 30, 30)
        main_layout.setSpacing(25)

        header_widget = self.createHeaderWidget()
        content_widget = self.createContentWidget()

        self.refresh_btn.clicked.connect(self.reloadAccounts)
        self.start_all_btn.clicked.connect(self.onStartAllAutoReply)
        self.stop_all_btn.clicked.connect(self.stopAllAutoReply)

        main_layout.addWidget(header_widget)
        main_layout.addWidget(content_widget, 1)
        self.setObjectName("自动回复")

    def createHeaderWidget(self):
        """创建头部区域"""
        header_widget = QWidget()
        header_layout = QHBoxLayout(header_widget)
        header_layout.setContentsMargins(0, 0, 0, 0)
        header_layout.setSpacing(20)

        title_label = SubtitleLabel("自动回复管理")
        self.stats_label = CaptionLabel("共 0 个账号")
        self.running_stats_label = CaptionLabel("运行中: 0 个")

        # 根据主题设置标签样式
        if isDarkTheme():
            self.running_stats_label.setStyleSheet("font-weight: bold; color: #ffffff;")
            self.stats_label.setStyleSheet("color: #cccccc;")
            title_label.setStyleSheet("color: #ffffff;")
        else:
            self.running_stats_label.setStyleSheet("font-weight: bold;")

        title_area = QWidget()
        title_layout = QVBoxLayout(title_area)
        title_layout.setContentsMargins(0, 0, 0, 0)
        title_layout.setSpacing(5)
        title_layout.addWidget(title_label)
        title_layout.addWidget(self.stats_label)
        title_layout.addWidget(self.running_stats_label)

        self.refresh_btn = PushButton("刷新")
        self.refresh_btn.setIcon(FIF.UPDATE)
        self.refresh_btn.setFixedSize(80, 40)

        self.start_all_btn = PrimaryPushButton("开始所有")
        self.start_all_btn.setIcon(FIF.PLAY_SOLID)
        self.start_all_btn.setFixedSize(120, 40)

        self.stop_all_btn = PushButton("停止所有")
        self.stop_all_btn.setIcon(FIF.CANCEL)
        self.stop_all_btn.setFixedSize(120, 40)

        buttons_widget = QWidget()
        buttons_layout = QHBoxLayout(buttons_widget)
        buttons_layout.setContentsMargins(0, 0, 0, 0)
        buttons_layout.setSpacing(10)
        buttons_layout.addWidget(self.refresh_btn)
        buttons_layout.addWidget(self.start_all_btn)
        buttons_layout.addWidget(self.stop_all_btn)

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

        self.scroll_area = ScrollArea()
        self.scroll_area.setWidgetResizable(True)
        self.scroll_area.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        self.scroll_area.setVerticalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAsNeeded)
        self.scroll_area.setStyleSheet("""
            ScrollArea {
                border: none;
                background-color: transparent;
            }
        """)

        self.accounts_container = QWidget()
        self.accounts_layout = QVBoxLayout(self.accounts_container)
        self.accounts_layout.setSpacing(15)
        self.accounts_layout.setContentsMargins(20, 20, 20, 20)
        self.accounts_layout.setAlignment(Qt.AlignmentFlag.AlignTop)

        self.accounts_container.setStyleSheet("""
            QWidget {
                background-color: transparent;
                border: none;
            }
        """)

        self.scroll_area.setWidget(self.accounts_container)
        content_layout.addWidget(self.scroll_area)

        return content_widget

    def loadAccountsFromDB(self):
        """从数据库加载账号数据"""
        try:
            self.accounts_data.clear()

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
        self.clearAccountList()

        for account_data in self.accounts_data:
            account_card = AutoReplyCard(account_data)

            account_card.online_clicked.connect(self.onAccountOnline)
            account_card.offline_clicked.connect(self.onAccountOffline)
            account_card.auto_reply_clicked.connect(self.onAutoReplyToggle)

            is_running = auto_reply_manager.is_running(account_data)
            account_key = f"{account_data['channel_name']}_{account_data['shop_id']}_{account_data['username']}"
            self.logger.debug(f"账号 {account_data['username']} 状态检查: key={account_key}, running={is_running}")
            account_card.setAutoReplyStatus(is_running)

            self.accounts_layout.addWidget(account_card)

        self.accounts_layout.addStretch()
        self.updateStats()
        QTimer.singleShot(2000, self._sync_auto_reply_status)

    def clearAccountList(self):
        """清空账号列表"""
        while self.accounts_layout.count():
            child = self.accounts_layout.takeAt(0)
            if child is None:
                continue
            widget = child.widget()
            if widget is not None:
                widget.deleteLater()

    def updateStats(self):
        """更新统计信息"""
        count = len(self.accounts_data)
        running_count = auto_reply_manager.get_running_count()
        self.stats_label.setText(f"共 {count} 个账号")
        self.running_stats_label.setText(f"运行中: {running_count} 个")

    def _sync_auto_reply_status(self):
        """同步自动回复状态（优化版本，减少误判）"""
        try:
            updated_count = 0

            for i in range(self.accounts_layout.count() - 1):  # -1 因为最后一个是stretch
                item = self.accounts_layout.itemAt(i)
                if item is None:
                    continue
                widget = item.widget()
                if isinstance(widget, AutoReplyCard):
                    is_running = auto_reply_manager.is_running(widget.account_data)
                    current_status = widget.auto_reply_status

                    if is_running != current_status:
                        # 额外检查：如果是运行中变为停止，需要确认线程真的停止了
                        if current_status and not is_running:
                            account_key = f"{widget.account_data['channel_name']}_{widget.account_data['shop_id']}_{widget.account_data['username']}"
                            if account_key in auto_reply_manager.running_accounts:
                                thread = auto_reply_manager.running_accounts[account_key]
                                if hasattr(thread, 'isRunning') and thread.isRunning():
                                    continue

                        self.logger.info(f"同步状态: {widget.account_data['username']} 从 {current_status} 更新为 {is_running}")
                        widget.setAutoReplyStatus(is_running)
                        updated_count += 1

            if updated_count > 0:
                self.logger.info(f"状态同步完成，更新了 {updated_count} 个账号的状态")
                self.updateStats()

        except Exception as e:
            self.logger.error(f"同步自动回复状态失败: {str(e)}")

    def reloadAccounts(self):
        """重新加载账号（异步）"""
        self._loadAccountsAsync()

    def _autoStartAllReply(self):
        """启动时自动开始所有符合条件的账号的自动回复"""
        try:
            eligible_accounts = [
                acc_data for acc_data in self.accounts_data
                if acc_data.get("status") == 1 and not auto_reply_manager.is_running(acc_data)
            ]

            if not eligible_accounts:
                self.logger.info("启动时自动回复：没有符合条件的账号")
                return

            started_count = 0
            for account_data in eligible_accounts:
                success = auto_reply_manager.start_auto_reply(account_data)
                if success:
                    started_count += 1
                    self._connect_auto_reply_signals(account_data)

            self._update_all_cards_auto_reply_status()
            self.updateStats()

            self.logger.info(f"启动时自动回复：已为 {started_count} 个账号启动自动回复")

        except Exception as e:
            self.logger.error(f"启动时自动回复失败: {str(e)}")

    def onStartAllAutoReply(self):
        """开始所有符合条件的账号的自动回复"""
        try:
            eligible_accounts = [
                acc_data for acc_data in self.accounts_data
                if acc_data.get("status") == 1 and not auto_reply_manager.is_running(acc_data)
            ]

            if not eligible_accounts:
                QMessageBox.information(self, "提示", "没有符合条件的账号可以启动自动回复。\n\n(需要账号状态为'在线'且当前未在回复中)")
                return

            reply = QMessageBox.question(
                self,
                "确认开始",
                f"找到 {len(eligible_accounts)} 个可启动的账号。确定要全部开始自动回复吗？",
                QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
                QMessageBox.StandardButton.No
            )

            if reply == QMessageBox.StandardButton.No:
                return

            started_count = 0
            for account_data in eligible_accounts:
                success = auto_reply_manager.start_auto_reply(account_data)
                if success:
                    started_count += 1
                    self._connect_auto_reply_signals(account_data)

            self._update_all_cards_auto_reply_status()
            self.updateStats()

            QMessageBox.information(self, "操作完成", f"已成功为 {started_count} / {len(eligible_accounts)} 个账号启动自动回复。")

        except Exception as e:
            self.logger.error(f"开始所有自动回复失败: {str(e)}")
            QMessageBox.critical(self, "错误", f"开始所有自动回复失败：{str(e)}")

    def stopAllAutoReply(self):
        """停止所有自动回复"""
        try:
            running_count = auto_reply_manager.get_running_count()

            if running_count == 0:
                QMessageBox.information(self, "提示", "当前没有正在运行的自动回复")
                return

            reply = QMessageBox.question(
                self,
                "确认停止",
                f"确定要停止所有 {running_count} 个正在运行的自动回复吗？",
                QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
                QMessageBox.StandardButton.No
            )

            if reply == QMessageBox.StandardButton.Yes:
                auto_reply_manager.stop_all()
                self._update_all_cards_auto_reply_status()
                self.updateStats()
                QMessageBox.information(self, "成功", "已停止所有自动回复")

        except Exception as e:
            self.logger.error(f"停止所有自动回复失败: {str(e)}")
            QMessageBox.critical(self, "错误", f"停止所有自动回复失败：{str(e)}")

    def _update_all_cards_auto_reply_status(self):
        """更新所有卡片的自动回复状态"""
        try:
            for i in range(self.accounts_layout.count() - 1):  # -1 因为最后一个是stretch
                item = self.accounts_layout.itemAt(i)
                if item is None:
                    continue
                widget = item.widget()
                if isinstance(widget, AutoReplyCard):
                    is_running = auto_reply_manager.is_running(widget.account_data)
                    widget.setAutoReplyStatus(is_running)

        except Exception as e:
            self.logger.error(f"更新卡片状态失败: {str(e)}")

    def onAccountOnline(self, account_data: dict):
        """账号上线回调"""
        try:
            account_card = self.findAccountCard(account_data)
            if account_card:
                account_card.setButtonLoading("online", True)

            self.status_thread = SetStatusThread(account_data, 1)

            self.status_thread.status_set_success.connect(self.onStatusSetSuccess)
            self.status_thread.status_set_failed.connect(self.onStatusSetFailed)

            self.status_thread.start()

        except Exception as e:
            self.logger.error(f"启动上线操作失败: {str(e)}")
            QMessageBox.critical(self, "错误", f"启动上线操作失败：{str(e)}")

    def onAccountOffline(self, account_data: dict):
        """账号离线回调"""
        try:
            account_card = self.findAccountCard(account_data)
            if account_card:
                account_card.setButtonLoading("offline", True)

            self.status_thread = SetStatusThread(account_data, 3)

            self.status_thread.status_set_success.connect(self.onStatusSetSuccess)
            self.status_thread.status_set_failed.connect(self.onStatusSetFailed)

            self.status_thread.start()

        except Exception as e:
            self.logger.error(f"启动离线操作失败: {str(e)}")
            QMessageBox.critical(self, "错误", f"启动离线操作失败：{str(e)}")

    def findAccountCard(self, account_data: dict):
        """查找对应的账号卡片"""
        for i in range(self.accounts_layout.count() - 1):  # -1 因为最后一个是stretch
            item = self.accounts_layout.itemAt(i)
            if item is None:
                continue
            widget = item.widget()
            if isinstance(widget, AutoReplyCard) and widget.account_data == account_data:
                return widget
        return None

    def onStatusSetSuccess(self, account_data: dict, new_status: int):
        """状态设置成功回调"""
        try:
            account_card = self.findAccountCard(account_data)
            if account_card:
                account_card.setButtonLoading("online", False)
                account_card.setButtonLoading("offline", False)

            self.updateCardStatus(account_data, new_status)

            status_text = "在线" if new_status == 1 else "离线"
            self.logger.info(f"账号 '{account_data['username']}' 已成功设置为{status_text}状态")

        except Exception as e:
            self.logger.error(f"处理状态设置成功回调失败: {str(e)}")

    def onStatusSetFailed(self, account_data: dict, error_message: str):
        """状态设置失败回调"""
        try:
            account_card = self.findAccountCard(account_data)
            if account_card:
                account_card.setButtonLoading("online", False)
                account_card.setButtonLoading("offline", False)

            self.logger.error(f"设置账号 '{account_data['username']}' 状态失败：{error_message}")
            QMessageBox.warning(self, "失败", f"设置账号 '{account_data['username']}' 状态失败：{error_message}")

        except Exception as e:
            self.logger.error(f"处理状态设置失败回调失败: {str(e)}")

    def onAutoReplyToggle(self, account_data: dict):
        """自动回复开关回调"""
        try:
            account_card = self.findAccountCard(account_data)
            if not account_card:
                self.logger.error("找不到对应的账号卡片")
                return

            current_status = auto_reply_manager.is_running(account_data)

            if current_status:
                self._stop_auto_reply(account_data, account_card)
            else:
                self._start_auto_reply(account_data, account_card)

        except Exception as e:
            self.logger.error(f"自动回复开关操作失败: {str(e)}")
            QMessageBox.critical(self, "错误", f"自动回复操作失败：{str(e)}")

    def _start_auto_reply(self, account_data: dict, account_card):
        """启动自动回复"""
        try:
            if account_data.get("status") != 1:
                QMessageBox.warning(self, "提示", "账号必须先上线才能开始自动回复！")
                return

            account_card.auto_reply_btn.setText("启动中...")
            account_card.auto_reply_btn.setEnabled(False)

            success = auto_reply_manager.start_auto_reply(account_data)

            if success:
                account_card.setAutoReplyStatus(True)
                self.logger.info(f"账号 '{account_data['username']}' 自动回复启动成功")
                self._connect_auto_reply_signals(account_data)
            else:
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
            account_card.auto_reply_btn.setText("停止中...")
            account_card.auto_reply_btn.setEnabled(False)

            success = auto_reply_manager.stop_auto_reply(account_data)

            # 无论成功与否，都更新UI状态
            account_card.setAutoReplyStatus(False)

            if success:
                self.logger.info(f"账号 '{account_data['username']}' 自动回复停止成功")
            else:
                self.logger.warning(f"账号 '{account_data['username']}' 自动回复停止可能未完全成功，但已从管理器中移除")

            self.updateStats()

        except Exception as e:
            self.logger.error(f"停止自动回复失败: {str(e)}")
            account_card.setAutoReplyStatus(False)
            QMessageBox.critical(self, "错误", f"停止自动回复失败：{str(e)}")
            self.updateStats()

    def _connect_auto_reply_signals(self, account_data: dict):
        """连接自动回复相关信号"""
        try:
            account_key = f"{account_data['channel_name']}_{account_data['shop_id']}_{account_data['username']}"

            if account_key in auto_reply_manager.running_accounts:
                thread = auto_reply_manager.running_accounts[account_key]

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
            self.updateStats()

        except Exception as e:
            self.logger.error(f"处理自动回复失败回调失败: {str(e)}")

    def updateCardStatus(self, account_data: dict, new_status: int):
        """更新卡片状态"""
        for i in range(self.accounts_layout.count() - 1):  # -1 因为最后一个是stretch
            item = self.accounts_layout.itemAt(i)
            if item is None:
                continue
            widget = item.widget()
            if isinstance(widget, AutoReplyCard) and widget.account_data == account_data:
                widget.updateStatus(new_status)
                break


__all__ = ['AutoReplyUI']
