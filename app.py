"""
应用程序入口点

全局单例初始化顺序（重要）：
1. config           → 必须在最前面，其他模块都依赖配置
2. DI 容器           → 通过 configure_standard_services() 统一注册所有服务
3. db_manager       → 通过 DI 容器获取
4. logger           → 日志系统，依赖 config
5. queue_manager    → 通过 DI 容器获取
6. message_consumer_manager → 通过 DI 容器获取
7. status_manager   → 通过 DI 容器获取（ConnectionStatusManager 单例）
8. cache_manager    → 通过 DI 容器获取

关键原则：
- config 必须最先初始化
- DI 容器通过 configure_standard_services() 统一管理所有服务的生命周期
- UI 模块在 main() 中通过延迟加载初始化
- 业务模块间通过延迟导入（lazy import）避免循环依赖
- PDDChannel 每个 AutoReplyThread 独立实例，共享 ConnectionStatusManager
"""
import sys
import ctypes
import asyncio
import os
import faulthandler
import traceback
import threading
from pathlib import Path
from PyQt6.QtCore import Qt, QTimer, QSharedMemory
from PyQt6.QtWidgets import QApplication, QMessageBox

# 启用 faulthandler：捕获 C++ 级 segfault 的 Python 堆栈
faulthandler.enable()
# 额外：将 faulthandler 输出写入文件（方便事后排查崩溃）
_fault_log_path = Path("./temp") / "crash_trace.log"
_fault_log_path.parent.mkdir(parents=True, exist_ok=True)

# 清理 crash_trace.log：超过阈值时只保留最近 1 小时
from utils.log_manager import truncate_to_recent_hours
CRASH_LOG_MAX_SIZE = os.environ.get("CRASH_LOG_MAX_SIZE", "100 MB")
truncate_to_recent_hours(
    _fault_log_path,
    hours=1,
    max_size=CRASH_LOG_MAX_SIZE,
)

_fault_file = open(_fault_log_path, "a", encoding="utf-8")

# 写入启动时间戳，方便定位每次运行
import time as _time
_fault_file.write(f"\n{'='*60}\n=== App started at {_time.strftime('%Y-%m-%d %H:%M:%S')} ===\n{'='*60}\n")
_fault_file.flush()

faulthandler.enable(_fault_file)
faulthandler.dump_traceback_later(timeout=30, repeat=True, file=_fault_file)

# 注册信号处理：崩溃时立即 dump 堆栈到文件
# faulthandler.enable() 已注册 SIGSEGV，但某些 C 扩展崩溃（如堆损坏）可能绕过它。
# faulthandler.register() 在 Unix 上可用，Windows 上不支持。
for _sig_name in ('SIGABRT', 'SIGFPE', 'SIGILL', 'SIGSEGV'):
    _sig = getattr(__import__('signal'), _sig_name, None)
    if _sig is not None:
        try:
            faulthandler.register(_sig, file=_fault_file, all_threads=True, chain=False)
        except (ValueError, OSError, AttributeError):
            pass  # Windows 不支持 register

# atexit 钩子：记录退出方式（正常 atexit vs 异常终止）
import atexit
def _on_exit():
    try:
        _fault_file.write(f"\n=== Process exiting at {_time.strftime('%Y-%m-%d %H:%M:%S')} (atexit) ===\n")
        _fault_file.flush()
    except Exception:
        pass
atexit.register(_on_exit)

# 周期性写入时间戳到 fault file，方便定位崩溃发生的时间点
def _periodic_fault_timestamp():
    """每 30 秒在 crash_trace.log 中写入时间戳，与 faulthandler dump 交替出现"""
    while True:
        try:
            _fault_file.write(f"\n--- Timestamp: {_time.strftime('%Y-%m-%d %H:%M:%S')} ---\n")
            _fault_file.flush()
        except Exception:
            pass  # 文件已关闭等情况，静默退出
        _time.sleep(30)

_ts_thread = threading.Thread(target=_periodic_fault_timestamp, daemon=True)
_ts_thread.start()

# ============================================================================
# 三层崩溃捕获系统（VEH + 心跳 + WER minidump）
# 必须在 faulthandler 之后、其他模块导入之前初始化
# ============================================================================
from utils.crash_detector import setup_crash_detection, check_previous_crash

_prev_crash = setup_crash_detection(enable_wer=True, heartbeat_interval=5.0)
if _prev_crash:
    _fault_file.write(
        f"!!! WARNING: Previous session crashed (not clean exit) !!!\n"
        f"  Last heartbeat: {_prev_crash['last_heartbeat']}\n"
        f"  Time since crash: {_prev_crash['seconds_ago']}s ago\n"
        f"  PID: {_prev_crash['pid']}\n"
    )
    _fault_file.flush()

# ============================================================================
# 全局单例预初始化（确保正确的初始化顺序）
# ============================================================================
# 1. 配置必须最先加载
from config import config as _app_config

# 2. 数据库管理器（通过 DI 代理，懒加载）
from database import db_manager as _app_db_manager

# 3. 日志系统（依赖配置）
from utils.logger_loguru import get_logger as _get_logger

# 4. 配置标准服务到 DI 容器（必须在其他业务模块导入前执行）
from core.di_container import configure_standard_services
configure_standard_services(_app_config)

# ============================================================================

from ui.main_ui import MainWindow
import time

# 设置 Playwright 浏览器路径（支持打包后的 exe）
def get_project_root():
    """获取项目根目录（支持 PyInstaller 打包后的 exe）"""
    import sys
    if getattr(sys, 'frozen', False):
        # PyInstaller 打包后的 exe
        return Path(sys._MEIPASS).parent
    return Path(__file__).resolve().parent

def setup_playwright_browsers_path():
    """设置 Playwright 浏览器安装路径"""
    project_root = get_project_root()
    browsers_path = project_root / ".browsers"
    os.environ["PLAYWRIGHT_BROWSERS_PATH"] = str(browsers_path)
    return browsers_path

def main():
    """ 应用程序主函数 """
    # 设置 Playwright 浏览器路径
    browsers_path = setup_playwright_browsers_path()

    # Qt 渲染后端配置 — 强制使用软件渲染
    # 根因：7-13/7-14/7-15/7-18 的 ntdll 堆崩溃，崩溃 RIP 在堆地址（非任何模块），
    # 崩溃线程不在 faulthandler 的 Python 线程列表中（是 Qt/D3D 内部线程），
    # WER 报告显示 d3d10warp.dll（WARP 软件光栅化器）曾被加载并卸载。
    # 推测：Qt 的 D3D 渲染线程在 d3d10warp.dll 卸载后仍引用其代码地址 → 跳到
    # 已释放堆地址执行 → access violation。
    # 修复：强制 Qt 使用软件渲染，避免 D3D/WARP 相关的内部线程崩溃。
    os.environ.setdefault("QT_OPENGL", "software")       # 禁用硬件 OpenGL
    os.environ.setdefault("QT_QUICK_BACKEND", "software") # QML 用软件后端
    os.environ.setdefault("LIBGL_ALWAYS_SOFTWARE", "1")   # Mesa 软件渲染

    # 创建应用
    app = QApplication(sys.argv)
    app.setApplicationName("Agent-Customer")

    # 全局异常钩子：捕获未处理的 Python 异常，写入日志并打印到 stderr
    def _global_excepthook(exc_type, exc_value, exc_tb):
        if issubclass(exc_type, KeyboardInterrupt):
            sys.__excepthook__(exc_type, exc_value, exc_tb)
            return
        logger = _get_logger("CrashHook")
        logger.error(f"未捕获的异常: {exc_type.__name__}: {exc_value}")
        logger.error("".join(traceback.format_tb(exc_tb)))
        # 也写入 faulthandler 文件
        print(f"\n=== Unhandled Exception {time.strftime('%Y-%m-%d %H:%M:%S')} ===", file=_fault_file)
        traceback.print_exception(exc_type, exc_value, exc_tb, file=_fault_file)
        _fault_file.flush()
        sys.__excepthook__(exc_type, exc_value, exc_tb)

    sys.excepthook = _global_excepthook
    
    # 多开校验 - 使用 QSharedMemory 确保单实例运行
    # 注意：进程崩溃后 QSharedMemory 不会自动释放，attach() 可能误判为"已有实例"。
    # 正确做法：先尝试 create（只有没有任何实例时才会成功），create 失败时再 attach 验证。
    shared_memory_key = "AgentCustomerApp_InstanceChecker"
    shared_mem = QSharedMemory(shared_memory_key)

    # 先尝试 create — 如果成功，说明没有其他实例（即使上次崩溃残留的共享内存也会被覆盖）
    if not shared_mem.create(1):
        # create 失败，可能是真的有其他实例在运行
        # 再用 attach 验证：如果 attach 也失败，说明是残留的共享内存
        if shared_mem.attach():
            shared_mem.detach()
            QMessageBox.critical(None, "程序已在运行", "拼多多AI客服助手已经在运行中，请勿重复启动。")
            sys.exit(1)
        else:
            # create 和 attach 都失败 — 尝试清理后重新 create
            shared_mem.detach()
            if not shared_mem.create(1):
                QMessageBox.critical(None, "启动失败", "无法创建实例检查器，请检查权限或重启电脑后再试。")
                sys.exit(1)

    # 创建主窗口
    logger = _get_logger("App")
    logger.info("应用程序启动...")

    t0 = time.perf_counter()
    t_import = time.perf_counter()
    from ui.main_ui import MainWindow  # noqa: F401
    logger.info(f"  MainWindow 模块导入耗时: {time.perf_counter() - t_import:.2f}s")
    t_window = time.perf_counter()
    window = MainWindow()
    logger.info(f"  MainWindow 实例化耗时: {time.perf_counter() - t_window:.2f}s")
    logger.info(f"窗口创建与显示总耗时: {time.perf_counter() - t0:.2f}s")

    # 将窗口设为应用级别的变量，防止被垃圾回收
    app.main_window = window
    app.shared_mem = shared_mem  # 保存共享内存引用

    # 运行事件循环
    exit_code = app.exec()
    
    # 退出时释放共享内存
    if shared_mem.isAttached():
        shared_mem.detach()
    
    sys.exit(exit_code)

if __name__ == '__main__':
    main()
