import sys
from typing import Optional, TYPE_CHECKING
from PyQt6.QtCore import Qt, QTimer, QEvent
from PyQt6.QtWidgets import QApplication, QFrame, QHBoxLayout, QLabel, QWidget
from PyQt6.QtGui import QFont, QIcon, QPixmap
from qfluentwidgets import FluentWindow, qrouter, NavigationItemPosition
from qfluentwidgets import FluentIcon as FIF
from qfluentwidgets import SubtitleLabel, TeachingTip, TeachingTipTailPosition
from qfluentwidgets import Action, setTheme, Theme, isDarkTheme, SystemThemeListener, qconfig
from utils.logger_loguru import get_logger
import time

if TYPE_CHECKING:
    from ui.auto_reply_ui import AutoReplyUI
    from ui.keyword_ui import KeywordManagerWidget
    from ui.user_ui import UserManagerWidget
    from ui.log_ui import LogUI
    from ui.setting_ui import SettingUI
    from ui.Knowledge_ui import KnowledgeUI
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
        
        # 监听系统主题切换事件
        self.theme_listener = SystemThemeListener(self)
        self.theme_listener.systemThemeChanged.connect(self._on_theme_changed)
        self.theme_listener.start()
        
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

        t = time.perf_counter()
        # 立即初始化导航和窗口
        self.initWindow()
        self.logger.info(f"  initWindow: {time.perf_counter()-t:.2f}s")

        # 延迟加载各个视图，让窗口先显示
        QTimer.singleShot(200, self.lazy_load_views)

    def lazy_load_views(self):
        """延迟加载各个视图，提高启动速度"""
        t0 = time.perf_counter()
        # 局部按需导入，减少启动时的重依赖加载
        t = time.perf_counter()
        from ui.auto_reply_ui import AutoReplyUI
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
        self.logger.info(f"  AutoReplyUI: {time.perf_counter()-t:.2f}s")
        t = time.perf_counter()
        self.keyword_manager_view = KeywordManagerWidget(self)
        self.logger.info(f"  KeywordManagerWidget: {time.perf_counter()-t:.2f}s")
        t = time.perf_counter()
        self.user_manager_view = UserManagerWidget(self)
        self.logger.info(f"  UserManagerWidget: {time.perf_counter()-t:.2f}s")
        t = time.perf_counter()
        self.log_view = LogUI(self)
        self.logger.info(f"  LogUI: {time.perf_counter()-t:.2f}s")
        t = time.perf_counter()
        self.settingInterface = SettingUI(self)
        self.logger.info(f"  SettingUI: {time.perf_counter()-t:.2f}s")
        t = time.perf_counter()
        self.knowledge_view = KnowledgeUI(self)
        self.logger.info(f"  KnowledgeUI: {time.perf_counter()-t:.2f}s")

        # 初始化导航
        self.initNavigation()
        self.logger.info(f"延迟视图初始化耗时: {time.perf_counter() - t0:.2f}s")

    # 初始化导航栏
    def initNavigation(self) -> None:
        # 确保所有视图都已初始化
        assert self.monitor_view is not None
        assert self.keyword_manager_view is not None
        assert self.user_manager_view is not None
        assert self.knowledge_view is not None
        assert self.log_view is not None
        assert self.settingInterface is not None

        self.navigationInterface.setExpandWidth(200)
        self.navigationInterface.setMinimumWidth(200)
        self.addSubInterface(self.monitor_view, FIF.CHAT, '自动回复')
        self.addSubInterface(self.keyword_manager_view, FIF.EDIT, '关键词管理')
        self.addSubInterface(self.user_manager_view, FIF.PEOPLE, '账号管理')
        self.addSubInterface(self.knowledge_view, FIF.LIBRARY, '知识库管理')
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
        
        # 设置标题栏文字颜色，确保深色模式下清晰可见
        self._update_title_bar_color()
        
        # 最后最大化显示
        self.showMaximized()
    
    def _update_title_bar_color(self):
        """更新标题栏文字颜色，适配深色/浅色模式"""
        try:
            # 获取标题栏标签（PyQt-Fluent-Widgets 内部属性）
            title_label = getattr(self.titleBar, "titleLabel", None)
            if title_label is None:
                return
            
            # 设置标题文字颜色
            if isDarkTheme():
                title_label.setStyleSheet("color: white; font-size: 14px; font-weight: bold;")
                
                # 设置标题栏按钮样式，确保在深色模式下图标清晰可见
                self.titleBar.setStyleSheet("""
                    QWidget {
                        background-color: transparent;
                    }
                    QPushButton {
                        color: white;
                        background-color: transparent;
                        border: none;
                        border-radius: 0px;
                        padding: 0px;
                        margin: 0px;
                        min-width: 46px;
                        min-height: 32px;
                    }
                    QPushButton:hover {
                        background-color: rgba(255, 255, 255, 0.1);
                    }
                    QPushButton:pressed {
                        background-color: rgba(255, 255, 255, 0.05);
                    }
                """)
            else:
                title_label.setStyleSheet("color: black; font-size: 14px; font-weight: bold;")
                # 浅色模式使用默认样式
                self.titleBar.setStyleSheet("")
        except Exception as e:
            self.logger.warning(f"设置标题栏颜色失败: {e}")
    
    def _on_theme_changed(self):
        """主题切换时更新标题栏颜色"""
        # 强制重新应用主题
        try:
            current_theme = qconfig.theme
            setTheme(Theme.LIGHT)
            setTheme(current_theme)
        except Exception:
            pass
        
        self._update_title_bar_color()
        
        # 强制更新标题栏按钮
        try:
            # 获取标题栏的所有子控件并更新
            self.titleBar.update()
            
            # 尝试直接访问按钮并更新
            for btn_name in ['minBtn', 'maxBtn', 'closeBtn']:
                btn = getattr(self.titleBar, btn_name, None)
                if btn is not None:
                    btn.update()
        except Exception as e:
            self.logger.warning(f"更新标题栏按钮失败: {e}")


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
        
        # 停止主题监听器
        try:
            if hasattr(self, 'theme_listener'):
                self.theme_listener.quit()
                # 等待最多500ms让线程退出
                self.theme_listener.wait(500)
        except Exception:
            pass
        
        # 停止所有自动回复线程
        try:
            from ui.auto_reply_ui import auto_reply_manager
            auto_reply_manager.stop_all()
        except Exception:
            pass
        
        # 清理自动回复界面资源（包括账号卡片的线程和定时器）
        try:
            if self.monitor_view:
                self.monitor_view.cleanup()
        except Exception:
            pass

        if a0 is not None:
            super().closeEvent(a0)
    
    def changeEvent(self, event):
        """监听主题切换事件，更新标题栏颜色"""
        super().changeEvent(event)
        
        # 当调色板改变时（主题切换会触发此事件），更新标题栏颜色
        if event.type() == QEvent.Type.PaletteChange:
            self._update_title_bar_color() 
