# 自动回复主界面模块
import time
from PyQt6.QtCore import Qt, QTimer, QEvent, QThread, pyqtSignal
from PyQt6.QtWidgets import QDialog, QFrame, QWidget, QVBoxLayout, QHBoxLayout
from PyQt6.QtGui import QFont
from qfluentwidgets import (SubtitleLabel, CaptionLabel, PushButton, PrimaryPushButton,
                          ScrollArea, FluentIcon as FIF, isDarkTheme, MessageBox,
                          InfoBar, InfoBarPosition)
from utils.logger_loguru import get_logger
from database.db_manager import db_manager
from config import config
from .card import AutoReplyCard
from .manager import auto_reply_manager
from .threads import SetStatusThread


class _StopAutoReplyWorker(QThread):
    """在后台线程停止单个账号的自动回复，避免阻塞主线程事件循环"""

    finished = pyqtSignal(dict, bool)  # account_data, success

    def __init__(self, account_data: dict, parent=None):
        super().__init__(parent)
        self.account_data = account_data

    def run(self):
        success = auto_reply_manager.stop_auto_reply(self.account_data)
        self.finished.emit(self.account_data, success)


class AutoReplyUI(QFrame):
    """自动回复主界面"""

    def __init__(self, parent=None):
        super().__init__(parent=parent)
        self.logger = get_logger()  # 初始化logger（必须在其他操作之前）
        self.accounts_data: list = []  # 存储账号数据
        self._loaded_once = False
        self._is_cold_starting = False  # 冷启动标志，控制失败时不弹窗
        self._offline_stop_workers: list[_StopAutoReplyWorker] = []  # 离线后异步停止自动回复的 worker 引用
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

            # 停止所有自动回复线程（退出时阻塞等待，确保线程结束）
            auto_reply_manager.stop_all(blocking=True)

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
        # 先执行更新，再延迟重置标志 —— 避免 setStyleSheet 触发的 PaletteChange
        # 在标志仍为 True 时被忽略，从而打破乒乓循环
        try:
            self._update_label_styles()
        finally:
            # 延迟200ms重置标志，确保 setStyleSheet 产生的 PaletteChange 事件
            # 在 _palette_pending=True 期间被忽略
            QTimer.singleShot(200, self._reset_palette_pending)

    def _reset_palette_pending(self):
        """重置调色板更新标志"""
        self._palette_pending = False

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
        """启动时自动开始所有符合条件的账号的自动回复（含 cookie 预检）"""
        eligible_accounts = [
            acc_data for acc_data in self.accounts_data
            if acc_data.get("status") == 1 and not auto_reply_manager.is_running(acc_data)
        ]

        if not eligible_accounts:
            self.logger.info("启动时自动回复：没有符合条件的账号")
            return

        # 启动 cookie 预检线程，完成后回调 _start_all_after_precheck
        self._is_cold_starting = True
        self._precheck_thread = CookiePrecheckThread(eligible_accounts, self)
        self._precheck_thread.precheck_complete.connect(self._start_all_after_precheck)
        self._precheck_thread.start()

    def _start_all_after_precheck(self):
        """cookie 预检完成后，启动所有符合条件的账号"""
        try:
            # 预检中被判定为"自动重登达上限、等待人工处理"的账号跳过启动
            blocked = getattr(self._precheck_thread, 'blocked_accounts', []) or []
            blocked_keys = {
                f"{acc.get('channel_name')}_{acc.get('shop_id')}_{acc.get('username')}"
                for acc in blocked
            }

            eligible_accounts = [
                acc_data for acc_data in self.accounts_data
                if acc_data.get("status") == 1
                and not auto_reply_manager.is_running(acc_data)
                and f"{acc_data.get('channel_name')}_{acc_data.get('shop_id')}_{acc_data.get('username')}" not in blocked_keys
            ]

            started_count = 0
            for account_data in eligible_accounts:
                success = auto_reply_manager.start_auto_reply(account_data)
                if success:
                    started_count += 1
                    self._connect_auto_reply_signals(account_data)

            self._update_all_cards_auto_reply_status()
            self.updateStats()

            self.logger.info(f"启动时自动回复：已为 {started_count} 个账号启动自动回复")

            # 提示等待人工处理的账号
            if blocked:
                names = "、".join(
                    acc.get("username", "unknown") for acc in blocked
                )
                self.logger.error(f"以下账号自动重登失败达上限，等待人工处理: {names}")
                InfoBar.error(
                    title="登录过期，等待人工处理",
                    content=f"以下账号自动重登连续失败，已跳过自动回复，请人工处理后重新登录：{names}",
                    orient=Qt.Orientation.Horizontal,
                    isClosable=True,
                    position=InfoBarPosition.TOP,
                    duration=8000,
                    parent=self,
                )

        except Exception as e:
            self.logger.error(f"启动时自动回复失败: {str(e)}")
        finally:
            # 冷启动结束，恢复弹窗行为
            QTimer.singleShot(30000, self._reset_cold_start_flag)

    def onStartAllAutoReply(self):
        """开始所有符合条件的账号的自动回复"""
        try:
            eligible_accounts = [
                acc_data for acc_data in self.accounts_data
                if acc_data.get("status") == 1 and not auto_reply_manager.is_running(acc_data)
            ]

            if not eligible_accounts:
                InfoBar.warning(
                    title="提示",
                    content="没有符合条件的账号可以启动自动回复。\n(需要账号状态为'在线'且当前未在回复中)",
                    orient=Qt.Orientation.Horizontal,
                    isClosable=True,
                    position=InfoBarPosition.TOP,
                    duration=3000,
                    parent=self,
                )
                return

            if not self._ask_confirm(
                "确认开始",
                f"找到 {len(eligible_accounts)} 个可启动的账号。确定要全部开始自动回复吗？"
            ):
                return

            started_count = 0
            for account_data in eligible_accounts:
                success = auto_reply_manager.start_auto_reply(account_data)
                if success:
                    started_count += 1
                    self._connect_auto_reply_signals(account_data)

            self._update_all_cards_auto_reply_status()
            self.updateStats()

            InfoBar.success(
                title="操作完成",
                content=f"已成功为 {started_count} / {len(eligible_accounts)} 个账号启动自动回复。",
                orient=Qt.Orientation.Horizontal,
                isClosable=True,
                position=InfoBarPosition.TOP,
                duration=2000,
                parent=self,
            )

        except Exception as e:
            self.logger.error(f"开始所有自动回复失败: {str(e)}")
            InfoBar.error(
                title="错误",
                content=f"开始所有自动回复失败：{str(e)}",
                orient=Qt.Orientation.Horizontal,
                isClosable=True,
                position=InfoBarPosition.TOP,
                duration=3000,
                parent=self,
            )

    def stopAllAutoReply(self):
        """停止所有自动回复"""
        try:
            running_count = auto_reply_manager.get_running_count()

            if running_count == 0:
                InfoBar.info(
                    title="提示",
                    content="当前没有正在运行的自动回复",
                    orient=Qt.Orientation.Horizontal,
                    isClosable=True,
                    position=InfoBarPosition.TOP,
                    duration=2000,
                    parent=self,
                )
                return

            if not self._ask_confirm(
                "确认停止",
                f"确定要停止所有 {running_count} 个正在运行的自动回复吗？"
            ):
                return

            # 禁用按钮防止重复点击，并在后台等待线程结束
            self.stop_all_btn.setEnabled(False)
            self.stop_all_btn.setText("停止中...")
            auto_reply_manager.all_stopped.connect(
                self._on_stop_all_finished, Qt.ConnectionType.QueuedConnection  # type: ignore[call-arg]
            )
            auto_reply_manager.stop_all()

        except Exception as e:
            self.logger.error(f"停止所有自动回复失败: {str(e)}")
            InfoBar.error(
                title="错误",
                content=f"停止所有自动回复失败：{str(e)}",
                orient=Qt.Orientation.Horizontal,
                isClosable=True,
                position=InfoBarPosition.TOP,
                duration=3000,
                parent=self,
            )

    def _on_stop_all_finished(self):
        """所有自动回复停止完成后的回调（在后台等待结束后由主线程调用）"""
        try:
            # 断开一次性信号，避免多次触发
            try:
                auto_reply_manager.all_stopped.disconnect(self._on_stop_all_finished)
            except Exception:
                pass

            self._update_all_cards_auto_reply_status()
            self.updateStats()
            self.stop_all_btn.setEnabled(True)
            self.stop_all_btn.setText("停止所有")
            # 使用无系统提示音的方式提示，避免触发 winmm 音频线程
            self._show_slient_tip("已停止所有自动回复")
        except Exception as e:
            self.logger.error(f"停止所有完成回调失败: {str(e)}")

    def _show_slient_tip(self, text: str):
        """显示一个无系统提示音的轻量提示，避免触发多媒体服务线程"""
        InfoBar.success(
            title=text,
            content="",
            orient=Qt.Orientation.Horizontal,
            isClosable=True,
            position=InfoBarPosition.TOP,
            duration=2000,
            parent=self,
        )

    def _ask_confirm(self, title: str, content: str,
                     yes_text: str = "确认", no_text: str = "取消") -> bool:
        """使用 qfluentwidgets MessageBox 显示确认对话框，不触发 Windows 系统提示音"""
        mb = MessageBox(title, content, self)
        mb.yesButton.setText(yes_text)
        mb.cancelButton.setText(no_text)
        return mb.exec() == QDialog.DialogCode.Accepted

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

            self.status_thread.status_set_success.connect(self.onStatusSetSuccess, Qt.ConnectionType.QueuedConnection)  # type: ignore[call-arg]
            self.status_thread.status_set_failed.connect(self.onStatusSetFailed, Qt.ConnectionType.QueuedConnection)  # type: ignore[call-arg]

            self.status_thread.start()

        except Exception as e:
            self.logger.error(f"启动上线操作失败: {str(e)}")
            InfoBar.error(
                title="错误",
                content=f"启动上线操作失败：{str(e)}",
                orient=Qt.Orientation.Horizontal,
                isClosable=True,
                position=InfoBarPosition.TOP,
                duration=3000,
                parent=self,
            )

    def onAccountOffline(self, account_data: dict):
        """账号离线回调"""
        try:
            account_card = self.findAccountCard(account_data)
            if account_card:
                account_card.setButtonLoading("offline", True)

            self.status_thread = SetStatusThread(account_data, 3)

            self.status_thread.status_set_success.connect(self.onStatusSetSuccess, Qt.ConnectionType.QueuedConnection)  # type: ignore[call-arg]
            self.status_thread.status_set_failed.connect(self.onStatusSetFailed, Qt.ConnectionType.QueuedConnection)  # type: ignore[call-arg]

            self.status_thread.start()

        except Exception as e:
            self.logger.error(f"启动离线操作失败: {str(e)}")
            InfoBar.error(
                title="错误",
                content=f"启动离线操作失败：{str(e)}",
                orient=Qt.Orientation.Horizontal,
                isClosable=True,
                position=InfoBarPosition.TOP,
                duration=3000,
                parent=self,
            )

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

            # 离线成功后，自动停止该账号的自动回复（在后台线程执行，不阻塞 UI）
            if new_status != 1 and auto_reply_manager.is_running(account_data):
                self.logger.info(f"账号 '{account_data['username']}' 已离线，自动停止自动回复")
                worker = _StopAutoReplyWorker(account_data, self)
                worker.finished.connect(
                    self._on_auto_reply_stopped_after_offline, Qt.ConnectionType.QueuedConnection  # type: ignore[call-arg]
                )
                self._offline_stop_workers.append(worker)
                worker.start()

        except Exception as e:
            self.logger.error(f"处理状态设置成功回调失败: {str(e)}")

    def _on_auto_reply_stopped_after_offline(self, account_data: dict, success: bool):
        """离线后自动停止自动回复完成的回调"""
        try:
            # 清理已完成的 worker 引用
            for worker in list(self._offline_stop_workers):
                if not worker.isRunning():
                    self._offline_stop_workers.remove(worker)

            account_card = self.findAccountCard(account_data)
            if account_card:
                account_card.setAutoReplyStatus(False)

            self.updateStats()

            if success:
                self.logger.info(f"账号 '{account_data['username']}' 离线后自动回复已停止")
                self._show_slient_tip(f"账号 '{account_data['username']}' 已离线，自动回复已停止")
            else:
                self.logger.warning(f"账号 '{account_data['username']}' 离线后自动回复停止未生效（可能未在运行）")
                self._update_all_cards_auto_reply_status()

        except Exception as e:
            self.logger.error(f"处理离线后停止自动回复回调失败: {str(e)}")

    def onStatusSetFailed(self, account_data: dict, error_message: str):
        """状态设置失败回调"""
        try:
            account_card = self.findAccountCard(account_data)
            if account_card:
                account_card.setButtonLoading("online", False)
                account_card.setButtonLoading("offline", False)

            self.logger.error(f"设置账号 '{account_data['username']}' 状态失败：{error_message}")
            InfoBar.warning(
                title="失败",
                content=f"设置账号 '{account_data['username']}' 状态失败：{error_message}",
                orient=Qt.Orientation.Horizontal,
                isClosable=True,
                position=InfoBarPosition.TOP,
                duration=3000,
                parent=self,
            )

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
            InfoBar.error(
                title="错误",
                content=f"自动回复操作失败：{str(e)}",
                orient=Qt.Orientation.Horizontal,
                isClosable=True,
                position=InfoBarPosition.TOP,
                duration=3000,
                parent=self,
            )

    def _start_auto_reply(self, account_data: dict, account_card):
        """启动自动回复"""
        try:
            if account_data.get("status") != 1:
                InfoBar.warning(
                    title="提示",
                    content="账号必须先上线才能开始自动回复！",
                    orient=Qt.Orientation.Horizontal,
                    isClosable=True,
                    position=InfoBarPosition.TOP,
                    duration=2000,
                    parent=self,
                )
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
                InfoBar.warning(
                    title="失败",
                    content=f"启动账号 '{account_data['username']}' 自动回复失败！",
                    orient=Qt.Orientation.Horizontal,
                    isClosable=True,
                    position=InfoBarPosition.TOP,
                    duration=3000,
                    parent=self,
                )

        except Exception as e:
            self.logger.error(f"启动自动回复失败: {str(e)}")
            account_card.auto_reply_btn.setText("开始回复")
            account_card.auto_reply_btn.setEnabled(True)
            InfoBar.error(
                title="错误",
                content=f"启动自动回复失败：{str(e)}",
                orient=Qt.Orientation.Horizontal,
                isClosable=True,
                position=InfoBarPosition.TOP,
                duration=3000,
                parent=self,
            )

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
            InfoBar.error(
                title="错误",
                content=f"停止自动回复失败：{str(e)}",
                orient=Qt.Orientation.Horizontal,
                isClosable=True,
                position=InfoBarPosition.TOP,
                duration=3000,
                parent=self,
            )
            self.updateStats()

    def _connect_auto_reply_signals(self, account_data: dict):
        """连接自动回复相关信号"""
        try:
            account_key = f"{account_data['channel_name']}_{account_data['shop_id']}_{account_data['username']}"

            if account_key in auto_reply_manager.running_accounts:
                thread = auto_reply_manager.running_accounts[account_key]

                thread.connection_success.connect(
                    lambda: self._on_auto_reply_success(account_data),
                    Qt.ConnectionType.QueuedConnection  # type: ignore[call-arg]
                )
                thread.connection_failed.connect(
                    lambda error: self._on_auto_reply_failed(account_data, error),
                    Qt.ConnectionType.QueuedConnection  # type: ignore[call-arg]
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

            # 防抖：多个账号几乎同时连接成功时，合并 updateStats() 调用
            # 避免重连竞态下短时间内多次触发 UI 更新（可能触发行绘制中的 access violation）
            if not getattr(self, '_stats_debounce_pending', False):
                self._stats_debounce_pending = True
                QTimer.singleShot(200, self._debounced_update_stats)

        except Exception as e:
            self.logger.error(f"处理自动回复成功回调失败: {str(e)}")

    def _debounced_update_stats(self):
        """防抖后的统计更新"""
        self._stats_debounce_pending = False
        self.updateStats()

    def _on_auto_reply_failed(self, account_data: dict, error: str):
        """自动回复连接失败回调"""
        try:
            account_card = self.findAccountCard(account_data)
            if account_card:
                account_card.setAutoReplyStatus(False)
                account_card.auto_reply_btn.setText("开始回复")
                account_card.auto_reply_btn.setEnabled(True)

            self.logger.error(f"账号 '{account_data['username']}' 自动回复连接失败: {error}")

            # 冷启动期间不弹提示，仅日志记录
            if not self._is_cold_starting:
                InfoBar.warning(
                    title="连接失败",
                    content=f"账号 '{account_data['username']}' 自动回复连接失败：{error}",
                    orient=Qt.Orientation.Horizontal,
                    isClosable=True,
                    position=InfoBarPosition.TOP,
                    duration=3000,
                    parent=self,
                )

            self.updateStats()

        except Exception as e:
            self.logger.error(f"处理自动回复失败回调失败: {str(e)}")

    def _reset_cold_start_flag(self):
        """冷启动结束后恢复弹窗行为"""
        self._is_cold_starting = False
        self.logger.debug("冷启动期结束，恢复弹窗提示")

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


class CookiePrecheckThread(QThread):
    """冷启动时并行预检 cookie 有效性，过期则尝试重登"""

    precheck_complete = pyqtSignal()

    # 并行 worker 数：同时启动多个 Chromium 会消耗大量内存，限制为 2
    _MAX_WORKERS = 2

    def __init__(self, accounts: list, parent=None):
        super().__init__(parent)
        self.accounts = accounts
        self.logger = get_logger("CookiePrecheckThread")
        self.setObjectName("CookiePrecheckThread")
        # 自动重登达上限、等待人工处理的账号（预检后不自动启动）
        self.blocked_accounts: list = []

    def _precheck_single(self, account_data: dict):
        """预检单个账号的 cookie（供线程池调用）"""
        from Channel.pinduoduo.cookie_utils import (
            check_cookies_valid, perform_relogin, relogin_guard,
        )
        from Channel.pinduoduo.cookie_cache import cookie_cache
        from database.db_manager import db_manager
        import json

        shop_id = account_data.get("shop_id")
        user_id = account_data.get("user_id")
        username = account_data.get("username", "unknown")

        if not shop_id or not user_id:
            return

        # 从缓存或 DB 获取 cookie
        cookies = cookie_cache.get("pinduoduo", shop_id, user_id)
        if not cookies:
            account_info = db_manager.get_account("pinduoduo", shop_id, user_id)
            if account_info:
                cookies_data = account_info.get('cookies')
                if isinstance(cookies_data, str):
                    try:
                        cookies = json.loads(cookies_data)
                    except json.JSONDecodeError:
                        cookies = None
                elif isinstance(cookies_data, dict):
                    cookies = cookies_data

        if not cookies:
            self.logger.warning(f"冷启动预检: 账号 {username} 无 cookie，跳过预检")
            return

        # 验证 cookie 有效性
        is_valid = check_cookies_valid(
            "pinduoduo", shop_id, user_id, cookies, timeout=15.0
        )

        if not is_valid:
            self.logger.warning(f"冷启动预检: 账号 {username} cookie 已过期，尝试重登...")
            account_info = db_manager.get_account("pinduoduo", shop_id, user_id)
            if account_info:
                success = perform_relogin(
                    "pinduoduo", shop_id, user_id,
                    username,
                    account_info.get('password', ''),
                    False,
                )
                if success:
                    self.logger.info(f"冷启动预检: 账号 {username} 重登成功")
                else:
                    # 自动重登连续失败达上限 → 等待人工处理，不自动启动该账号
                    if relogin_guard.failure_count("pinduoduo", shop_id, user_id) >= relogin_guard._max_failures():
                        self.blocked_accounts.append(account_data)
                        self.logger.error(
                            f"冷启动预检: 账号 {username} 自动重登失败已达上限，"
                            f"跳过启动，等待人工处理"
                        )
                    else:
                        self.logger.error(f"冷启动预检: 账号 {username} 重登失败，仍将尝试启动")
        else:
            self.logger.debug(f"冷启动预检: 账号 {username} cookie 有效")

    def run(self):
        """在后台线程中并行预检所有账号的 cookie"""
        from concurrent.futures import ThreadPoolExecutor, as_completed

        try:
            if len(self.accounts) <= 1:
                # 单账号无需线程池
                for acc in self.accounts:
                    self._precheck_single(acc)
            else:
                with ThreadPoolExecutor(
                    max_workers=min(self._MAX_WORKERS, len(self.accounts)),
                    thread_name_prefix="CookiePrecheck"
                ) as executor:
                    futures = {executor.submit(self._precheck_single, acc): acc
                               for acc in self.accounts}
                    for future in as_completed(futures):
                        try:
                            future.result()
                        except Exception as e:
                            acc = futures[future]
                            self.logger.error(
                                f"冷启动预检: 账号 {acc.get('username', 'unknown')} 预检异常: {e}"
                            )
        except Exception as e:
            self.logger.error(f"冷启动 cookie 预检失败: {e}")
        finally:
            self.precheck_complete.emit()


__all__ = ['AutoReplyUI', 'CookiePrecheckThread']
