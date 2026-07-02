import sys
import traceback
from typing import Optional, TYPE_CHECKING
from PyQt6.QtCore import Qt, QTimer, QEvent, QMetaObject, Q_ARG, pyqtSlot, pyqtSignal
from PyQt6.QtWidgets import QApplication, QFrame, QHBoxLayout, QLabel, QWidget
from PyQt6.QtGui import QColor, QFont, QIcon, QPixmap
from qfluentwidgets import FluentWindow, qrouter, NavigationItemPosition
from qfluentwidgets import FluentIcon as FIF
from qfluentwidgets import SubtitleLabel, TeachingTip, TeachingTipTailPosition
from qfluentwidgets import Action, setTheme, Theme, isDarkTheme, SystemThemeListener, qconfig
from utils.logger_loguru import get_logger
import time


class SafeSystemThemeListener(SystemThemeListener):
    """线程安全的系统主题监听器

    qfluentwidgets 原版 SystemThemeListener 在后台线程中直接修改 qconfig.theme
    并 emit themeChanged 信号，导致主线程 navigation_widget.paintEvent 中
    isDarkTheme() / SVG 路径读取发生数据竞争，引发 access violation 崩溃。

    本子类重写 _onThemeChanged，将所有主题状态变更操作通过
    QMetaObject.invokeMethod (QueuedConnection) 转发到主线程执行，
    确保 qconfig.theme 的读写始终在主线程中发生。
    """

    # 新信号：通知主线程执行主题切换
    _theme_changed_in_main = pyqtSignal(str)

    def __init__(self, parent=None):
        super().__init__(parent)
        # 将信号连接到主线程槽（parent 通常在主线程，AutoConnection 会选择 QueuedConnection）
        self._theme_changed_in_main.connect(self._do_theme_change_in_main, Qt.ConnectionType.QueuedConnection)

    def _onThemeChanged(self, theme: str):
        """后台线程回调 — 不直接修改 qconfig，仅转发到主线程"""
        # 仅做轻量级的字符串处理，不触碰任何 Qt 全局状态
        t = theme.lower() if isinstance(theme, str) else "light"
        # 通过信号将变更投递到主线程事件队列
        self._theme_changed_in_main.emit(t)

    @pyqtSlot(str)
    def _do_theme_change_in_main(self, theme_lower: str):
        """在主线程中安全地执行主题切换"""
        try:
            theme = Theme.DARK if theme_lower == "dark" else Theme.LIGHT

            # 如果不是 AUTO 模式，或主题没变化，则跳过
            if qconfig.themeMode.value != Theme.AUTO or theme == qconfig.theme:
                return

            # 现在在主线程中安全地修改主题状态
            qconfig.theme = Theme.AUTO  # setter 内部会调用 darkdetect.theme() 获取实际值
            qconfig._cfg.themeChanged.emit(Theme.AUTO)
            self.systemThemeChanged.emit()
        except Exception:
            pass

if TYPE_CHECKING:
    from ui.auto_reply import AutoReplyUI
    from ui.keyword_ui import KeywordManagerWidget
    from ui.user_ui import UserManagerWidget
    from ui.log_ui import LogUI
    from ui.setting_ui import SettingUI
    from ui.Knowledge_ui import KnowledgeUI
    from ui.chat_ui import ChatUI
    from PyQt6.QtGui import QCloseEvent

class Widget(QFrame):

    def __init__(self, text: str, parent: Optional[QWidget] = None):
        super().__init__(parent=parent)
        # 创建标题标签
        self.label = SubtitleLabel(text, self)
        # 创建水平布局
        self.hBoxLayout = QHBoxLayout(self)
        # 设置标签文本居中对齐
        self.label.setAlignment(Qt.AlignmentFlag.AlignCenter)
        # 将标签添加到布局中,设置居中对齐和拉伸因子1
        self.hBoxLayout.addWidget(self.label, 1, Qt.AlignmentFlag.AlignCenter)
        # 必须给子界面设置全局唯一的对象名
        self.setObjectName(text.replace(' ', '-'))

class MainWindow(FluentWindow):
    def __init__(self):
        super().__init__()
        
        # 自动跟随系统主题（深色/浅色）
        setTheme(Theme.AUTO)
        
        # 监听系统主题切换事件（使用线程安全版本）
        self.theme_listener = SafeSystemThemeListener(self)
        self.theme_listener.systemThemeChanged.connect(self._on_theme_changed)
        self.theme_listener.setObjectName("SystemThemeListener")
        self.theme_listener.start()
        
        # 主线程卡顿检测定时器 — 每3秒检查事件循环是否畅通
        self._freeze_check_timer = QTimer(self)
        self._freeze_check_timer.setInterval(3000)
        self._freeze_check_last = time.perf_counter()
        self._freeze_check_timer.timeout.connect(self._check_freeze)
        self._freeze_check_timer.start()
        
        t = time.perf_counter()
        self.setWindowTitle('拼多多AI客服助手')
        self.setWindowIcon(QIcon("icon/icon.ico"))
        self.logger = get_logger("MainWindow")
        self.logger.info(f"  基础属性初始化: {time.perf_counter()-t:.2f}s")

        # 延迟加载的视图
        self.monitor_view: Optional["AutoReplyUI"] = None
        self.keyword_manager_view: Optional["KeywordManagerWidget"] = None
        self.user_manager_view: Optional["UserManagerWidget"] = None
        self.log_view: Optional["LogUI"] = None
        self.knowledge_view: Optional["KnowledgeUI"] = None
        self.settingInterface: Optional["SettingUI"] = None
        self.chat_view: Optional["ChatUI"] = None

        t = time.perf_counter()
        # 立即初始化导航和窗口
        self.initWindow()
        self.logger.info(f"  initWindow: {time.perf_counter()-t:.2f}s")

        # 延迟加载各个视图，让窗口先显示
        QTimer.singleShot(200, self.lazy_load_views)
    
    def _check_freeze(self):
        """检测主线程是否卡顿 — 如果3秒定时器触发时发现距离上次超过5秒，说明中间有阻塞"""
        now = time.perf_counter()
        gap = now - self._freeze_check_last
        self._freeze_check_last = now
        if gap > 5:
            # 输出主线程当前堆栈，帮助定位阻塞来源
            main_thread_id = self.thread()
            stack_frames = []
            for thread_id, frame in sys._current_frames().items():
                # 找到主线程的堆栈
                frame_info = traceback.extract_stack(frame)
                # 通过堆栈深度和内容判断是否是主线程（Qt主线程通常有app.exec）
                stack_str = ''.join(traceback.format_list(frame_info[-5:]))
                stack_frames.append(stack_str)

            self.logger.warning(
                f"⚠️ 主线程卡顿检测: 事件循环间隔 {gap:.1f}s（正常应≈3s），可能存在阻塞操作\n"
                f"主线程堆栈（最近5帧）:\n{stack_frames[0] if stack_frames else '无法获取'}"
            )

        # 顺便清理 staff_reply_event_manager 中的孤儿事件
        # 长时间运行下，AutoReplyThread 重启会留下绑定死 loop 的 asyncio.Event，
        # 这些孤儿事件引用着已 close 的 loop 对象，长期不清会泄漏，且如果下次
        # notify 撞上死 loop 触发 RuntimeError 也已被 notify_staff_reply 自身捕获。
        try:
            from Message.handlers.staff_reply_event import staff_reply_event_manager
            staff_reply_event_manager.cleanup_expired()
        except Exception:
            pass  # 清理失败不影响主流程

    def lazy_load_views(self):
        """延迟加载各个视图，提高启动速度"""
        t0 = time.perf_counter()
        # 局部按需导入，减少启动时的重依赖加载
        t = time.perf_counter()
        from ui.auto_reply import AutoReplyUI
        self.logger.info(f"  import AutoReplyUI: {time.perf_counter()-t:.2f}s")
        t = time.perf_counter()
        from ui.keyword_ui import KeywordManagerWidget
        self.logger.info(f"  import KeywordManagerWidget: {time.perf_counter()-t:.2f}s")
        t = time.perf_counter()
        from ui.user_ui import UserManagerWidget
        self.logger.info(f"  import UserManagerWidget: {time.perf_counter()-t:.2f}s")
        t = time.perf_counter()
        from ui.log_ui import LogUI
        self.logger.info(f"  import LogUI: {time.perf_counter()-t:.2f}s")
        t = time.perf_counter()
        from ui.setting_ui import SettingUI
        self.logger.info(f"  import SettingUI: {time.perf_counter()-t:.2f}s")
        t = time.perf_counter()
        from ui.Knowledge_ui import KnowledgeUI
        self.logger.info(f"  import KnowledgeUI: {time.perf_counter()-t:.2f}s")

        t = time.perf_counter()
        self.monitor_view = AutoReplyUI(self)
        dt = time.perf_counter()-t
        self.logger.info(f"  AutoReplyUI: {dt:.2f}s" + (" ⚠️>0.5s" if dt > 0.5 else ""))
        t = time.perf_counter()
        self.keyword_manager_view = KeywordManagerWidget(self)
        dt = time.perf_counter()-t
        self.logger.info(f"  KeywordManagerWidget: {dt:.2f}s" + (" ⚠️>0.5s" if dt > 0.5 else ""))
        t = time.perf_counter()
        self.user_manager_view = UserManagerWidget(self)
        dt = time.perf_counter()-t
        self.logger.info(f"  UserManagerWidget: {dt:.2f}s" + (" ⚠️>0.5s" if dt > 0.5 else ""))
        t = time.perf_counter()
        self.log_view = LogUI(self)
        dt = time.perf_counter()-t
        self.logger.info(f"  LogUI: {dt:.2f}s" + (" ⚠️>0.5s" if dt > 0.5 else ""))
        t = time.perf_counter()
        self.settingInterface = SettingUI(self)
        dt = time.perf_counter()-t
        self.logger.info(f"  SettingUI: {dt:.2f}s" + (" ⚠️>0.5s" if dt > 0.5 else ""))
        t = time.perf_counter()
        self.knowledge_view = KnowledgeUI(self)
        dt = time.perf_counter()-t
        self.logger.info(f"  KnowledgeUI: {dt:.2f}s" + (" ⚠️>0.5s" if dt > 0.5 else ""))
        t = time.perf_counter()
        from ui.chat_ui import ChatUI
        self.logger.info(f"  import ChatUI: {time.perf_counter()-t:.2f}s")
        t = time.perf_counter()
        self.chat_view = ChatUI(self)
        dt = time.perf_counter()-t
        self.logger.info(f"  ChatUI: {dt:.2f}s" + (" ⚠️>0.5s" if dt > 0.5 else ""))

        # 初始化导航
        t = time.perf_counter()
        self.initNavigation()
        self.logger.info(f"  initNavigation: {time.perf_counter()-t:.2f}s")
        self.logger.info(f"延迟视图初始化耗时: {time.perf_counter() - t0:.2f}s")

    # 初始化导航栏
    def initNavigation(self) -> None:
        # 确保所有视图都已初始化
        assert self.monitor_view is not None
        assert self.keyword_manager_view is not None
        assert self.user_manager_view is not None
        assert self.knowledge_view is not None
        assert self.chat_view is not None
        assert self.log_view is not None
        assert self.settingInterface is not None

        self.navigationInterface.setExpandWidth(200)
        self.navigationInterface.setMinimumWidth(200)
        self.addSubInterface(self.monitor_view, FIF.CHAT, '自动回复')
        self.addSubInterface(self.keyword_manager_view, FIF.EDIT, '关键词管理')
        self.addSubInterface(self.user_manager_view, FIF.PEOPLE, '账号管理')
        self.addSubInterface(self.knowledge_view, FIF.LIBRARY, '知识库管理')
        self.addSubInterface(self.chat_view, FIF.CHAT, '聊天记录')
        self.addSubInterface(self.log_view, FIF.HISTORY, '日志管理')
        # 添加二维码按钮
        self.navigationInterface.addItem(
            routeKey='contact_us',
            icon=FIF.QRCODE,
            text='联系我们',
            onClick=self.showQRCode,
            selectable=False,
            position=NavigationItemPosition.BOTTOM
        )

        self.addSubInterface(self.settingInterface, FIF.SETTING, '设置', NavigationItemPosition.BOTTOM)
        

    # 初始化窗口
    def initWindow(self):
        # 先设置最小尺寸
        self.setMinimumWidth(1280)
        self.setMinimumHeight(720)
        
        # 设置默认尺寸（避免几何冲突）
        self.resize(1400, 800)
        
        # 延迟最大化显示，避免在 paint engine 初始化前触发 QPainter 错误
        # 同时延迟标题栏颜色设置，避免 setStyleSheet → PaletteChange 乒乓
        QTimer.singleShot(0, self._deferred_show)
    
    def _deferred_show(self):
        """延迟显示窗口，确保 paint engine 已初始化"""
        # 设置标题栏文字颜色
        self._update_title_bar_color()
        # 最大化显示
        self.showMaximized()
    
    def _update_title_bar_color(self):
        """更新标题栏文字颜色，适配深色/浅色模式"""
        # 防递归：如果已经在更新中，跳过
        if getattr(self, '_updating_title_bar', False):
            return
        self._updating_title_bar = True
        try:
            # 获取标题栏标签（PyQt-Fluent-Widgets 内部属性）
            title_label = getattr(self.titleBar, "titleLabel", None)
            if title_label is None:
                return
            
            # 设置标题文字颜色
            if isDarkTheme():
                title_label.setStyleSheet("color: white; font-size: 14px; font-weight: bold;")

                # 直接设置按钮图标颜色，不覆盖 titleBar 的 QSS（避免破坏框架管理的样式）
                for btn_name in ['minBtn', 'maxBtn', 'closeBtn']:
                    btn = getattr(self.titleBar, btn_name, None)
                    if btn is not None:
                        btn.setNormalColor(QColor(255, 255, 255))
                        btn.setHoverColor(QColor(255, 255, 255))
                        btn.setPressedColor(QColor(255, 255, 255))
            else:
                title_label.setStyleSheet("color: black; font-size: 14px; font-weight: bold;")
                for btn_name in ['minBtn', 'maxBtn', 'closeBtn']:
                    btn = getattr(self.titleBar, btn_name, None)
                    if btn is not None:
                        btn.setNormalColor(QColor(0, 0, 0))
                        btn.setHoverColor(QColor(0, 0, 0))
                        btn.setPressedColor(QColor(0, 0, 0))
        except Exception as e:
            self.logger.warning(f"设置标题栏颜色失败: {e}")
        finally:
            self._updating_title_bar = False
    
    def _on_theme_changed(self):
        """主题切换时更新标题栏颜色（防抖：500ms内只响应一次）"""
        if getattr(self, '_theme_change_pending', False):
            return
        self._theme_change_pending = True
        QTimer.singleShot(500, self._do_theme_change)
    
    def _do_theme_change(self):
        """实际执行主题切换更新"""
        self._theme_change_pending = False

        # 冻结导航栏重绘，防止 paintEvent 在主题状态过渡期间读取不一致的 isDarkTheme()
        # 导致 QSvgRenderer 拿到无效 SVG 数据 → access violation
        nav = self.navigationInterface
        nav_frozen = False
        try:
            nav_frozen = True
            nav.setUpdatesEnabled(False)
        except Exception:
            nav_frozen = False

        try:
            self._update_title_bar_color()

            # 强制更新标题栏按钮
            self.titleBar.update()
            for btn_name in ['minBtn', 'maxBtn', 'closeBtn']:
                btn = getattr(self.titleBar, btn_name, None)
                if btn is not None:
                    btn.update()
        except Exception as e:
            self.logger.warning(f"更新标题栏按钮失败: {e}")
        finally:
            if nav_frozen:
                # 延迟恢复导航栏重绘，确保所有主题相关的样式变更已处理完毕
                QTimer.singleShot(50, lambda: self._restore_nav_updates(nav))

    def _restore_nav_updates(self, nav):
        """恢复导航栏重绘"""
        try:
            nav.setUpdatesEnabled(True)
            nav.update()
        except Exception:
            pass


    def showQRCode(self):
        """显示二维码TeachingTip"""
        try:
            tip = TeachingTip.create(
                target=self.navigationInterface,
                image="icon/Customer-Agent-qr.png",
                icon=FIF.PEOPLE,
                title="联系我们",
                content="扫码关注获取更多信息和支持",
                isClosable=True,
                duration=-1,
                tailPosition=TeachingTipTailPosition.LEFT,
                parent=self
            )
            
            # 显示TeachingTip
            tip.show()
            
        except Exception as e:
            self.logger.error(f"显示二维码失败: {e}")

    def closeEvent(self, a0: Optional["QCloseEvent"]) -> None:
        """ 重写窗口关闭事件，确保后台线程安全退出 """
        
        # 停止主题监听器（Windows上darkdetect.listener是阻塞的，quit无效，需terminate）
        try:
            if hasattr(self, 'theme_listener') and self.theme_listener:
                self.theme_listener.requestInterruption()
                # 等待最多500ms让线程退出
                if not self.theme_listener.wait(500):
                    self.logger.warning("主题监听器未在500ms内退出，执行强制终止")
                    self.theme_listener.terminate()
                    self.theme_listener.wait(500)
        except Exception:
            pass
        
        # 清理自动回复界面资源（内部会调用auto_reply_manager.stop_all()）
        try:
            if self.monitor_view:
                self.monitor_view.cleanup()
        except Exception:
            pass
        
        # 清理知识库界面资源（停止所有Worker线程）
        try:
            if self.knowledge_view:
                self.knowledge_view.cleanup()
        except Exception:
            pass
        
        # 清理聊天记录界面资源（断开消息信号）
        try:
            if self.chat_view:
                self.chat_view.cleanup()
        except Exception:
            pass
        
        # 清理账号管理界面资源（停止LoginThread等线程）
        try:
            if self.user_manager_view:
                self.user_manager_view.cleanup()
        except Exception:
            pass

        if a0 is not None:
            super().closeEvent(a0)
    
    def changeEvent(self, event):
        """监听主题切换事件，更新标题栏颜色"""
        super().changeEvent(event)
        
        # 当调色板改变时（主题切换会触发此事件），更新标题栏颜色
        # 防抖：避免 setStyleSheet → PaletteChange → singleShot(0, setStyleSheet) 乒乓循环
        if event.type() == QEvent.Type.PaletteChange:
            if not getattr(self, '_palette_pending', False):
                self._palette_pending = True
                QTimer.singleShot(100, self._do_palette_update) 

    def _do_palette_update(self):
        """实际执行调色板更新"""
        # 先执行更新，再延迟重置标志 —— 避免 setStyleSheet 触发的 PaletteChange
        # 在标志仍为 True 时被忽略，从而打破乒乓循环
        try:
            self._update_title_bar_color()
        finally:
            # 延迟200ms重置标志，确保 setStyleSheet 产生的 PaletteChange 事件
            # 在 _palette_pending=True 期间被忽略
            QTimer.singleShot(200, self._reset_palette_pending) 

    def _reset_palette_pending(self):
        """重置调色板更新标志"""
        self._palette_pending = False
