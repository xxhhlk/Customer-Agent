import sys
import ctypes
from PyQt6.QtCore import Qt
from PyQt6.QtWidgets import QApplication, QMessageBox
from PyQt6.QtCore import QSharedMemory

# 设置高DPI支持必须在导入 QApplication 之前
if hasattr(Qt.ApplicationAttribute, 'AA_EnableHighDpiScaling'):
    QApplication.setAttribute(Qt.ApplicationAttribute.AA_EnableHighDpiScaling)
if hasattr(Qt.ApplicationAttribute, 'AA_UseHighDpiPixmaps'):
    QApplication.setAttribute(Qt.ApplicationAttribute.AA_UseHighDpiPixmaps)

from ui.main_ui import MainWindow
from utils.logger import get_logger

def main():
    """ 应用程序主函数 """
    logger = get_logger("App")
    logger.info("应用程序启动...")

    # 防多开检查 - 使用 QSharedMemory
    shared_memory = QSharedMemory("CustomerAgent_SingleInstance_Lock")
    
    # 尝试创建共享内存段，如果已存在则说明已有实例在运行
    if not shared_memory.create(1):
        # 创建失败，可能已有实例运行
        logger.warning("检测到程序已在运行，禁止多开")
        
        # 创建临时 QApplication 显示提示框
        temp_app = QApplication(sys.argv)
        QMessageBox.warning(
            None,
            "程序已在运行",
            "客服助手已经启动，请勿重复运行。\n\n"
            "如果程序未显示，请检查任务栏或系统托盘。"
        )
        sys.exit(0)
    
    # 共享内存创建成功，继续正常启动
    logger.info("单实例检查通过，继续启动...")

    # 创建应用实例
    app = QApplication(sys.argv)
    
    # 设置应用程序跟随系统深色模式
    from qfluentwidgets import setTheme, Theme
    from PyQt6.QtGui import QPalette
    from PyQt6.QtCore import QEvent, QObject, QTimer
    
    # 主题更新防抖标志和当前主题状态
    theme_update_pending = False
    current_theme = None  # 记录当前主题状态
    
    def update_theme():
        """根据系统主题更新应用主题"""
        nonlocal theme_update_pending, current_theme
        theme_update_pending = False
        
        palette = app.palette()
        # 通过背景色亮度判断是否为深色模式
        bg_color = palette.color(QPalette.ColorRole.Window)
        is_dark = bg_color.lightness() < 128
        
        # 检测到的主题
        detected_theme = Theme.DARK if is_dark else Theme.LIGHT
        
        # 如果主题没有变化，不执行更新
        if current_theme == detected_theme:
            return
        
        # 更新主题
        current_theme = detected_theme
        if is_dark:
            setTheme(Theme.DARK)
            logger.info("切换到深色模式")
        else:
            setTheme(Theme.LIGHT)
            logger.info("切换到浅色模式")
    
    # 初始化主题
    update_theme()
    
    # 监听系统主题变化
    class ThemeChangeListener(QObject):
        def __init__(self):
            super().__init__()
            
        def eventFilter(self, obj, event):
            nonlocal theme_update_pending
            
            if event.type() == QEvent.Type.PaletteChange:
                # 如果已有定时器在运行，不创建新的
                if not theme_update_pending:
                    theme_update_pending = True
                    # 使用单次定时器，500ms 后执行
                    QTimer.singleShot(500, update_theme)
                return False
            return False
    
    theme_listener = ThemeChangeListener()
    app.installEventFilter(theme_listener)
    
    # 在Windows上设置AppUserModelID，以确保任务栏图标正确显示
    try:
        if sys.platform == "win32":
            ctypes.windll.shell32.SetCurrentProcessExplicitAppUserModelID("my.company.my.product.version")
    except Exception as e:
        logger.warning(f"设置AppUserModelID失败: {e}")

    # 初始化并显示主窗口
    window = MainWindow()
    window.show()

    # 运行事件循环
    sys.exit(app.exec())

if __name__ == '__main__':
    main()
