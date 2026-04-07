import sys
import ctypes
from PyQt6.QtCore import Qt
from PyQt6.QtWidgets import QApplication, QMessageBox
from PyQt6.QtCore import QSharedMemory

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

    # 启用高分屏支持
    if hasattr(Qt.ApplicationAttribute, 'AA_EnableHighDpiScaling'):
        QApplication.setAttribute(Qt.ApplicationAttribute.AA_EnableHighDpiScaling)
    if hasattr(Qt.ApplicationAttribute, 'AA_UseHighDpiPixmaps'):
        QApplication.setAttribute(Qt.ApplicationAttribute.AA_UseHighDpiPixmaps)

    app = QApplication(sys.argv)
    
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
