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

# Qt 渲染后端配置 —— 必须在导入任何 PyQt6 模块之前设置，否则平台插件已加载、失效。
# 根因：7-13/7-14/7-15/7-18/7-31 的 ntdll 堆崩溃，崩溃线程为 Qt/D3D 内部线程，
# WER 报告显示 d3d10warp.dll（WARP 软件光栅化器）曾被加载并卸载。
# 关键："software" 在 Windows 上仍会走 d3d10warp.dll（WARP），只有 "no" 才能
# 完全禁用 OpenGL、不加载 WARP，避免 d3d10warp 卸载后函数指针悬空崩溃。
# 聊天 tab 重绘量大，是触发该崩溃的高频场景。
os.environ.setdefault("QT_OPENGL", "software")      # 软件渲染（8-1 实测 software 不崩，no 反而崩）
os.environ.setdefault("QT_QUICK_BACKEND", "software") # QML 用软件后端
os.environ.setdefault("LIBGL_ALWAYS_SOFTWARE", "1")   # Mesa 软件渲染

# —— 预加载 D3D/D2D 关键 DLL 并永久持有引用 ——
# 根因：Qt6 ModernWindows 风格 → d2d1 → d3d11 → d3d10warp（WARP）。
# d3d10warp.dll 被某个组件用完即 FreeLibrary，引用计数归零后被卸载，
# 但 Qt/D3D 内部线程仍持有其函数指针 → 跳到已释放堆地址 → 0xc0000005。
# 5 个 dump 的崩溃地址末三位均为 0xfc0，证实是同一悬空函数指针。
# 预加载并永不释放，让引用计数始终 ≥1，阻止卸载。
_dll_handles = []
for _dll_name in ("d3d10warp.dll", "d2d1.dll", "d3d11.dll", "d3d9.dll", "DWrite.dll"):
    try:
        _h = ctypes.WinDLL(_dll_name)
        _dll_handles.append(_h)
    except OSError:
        pass

from PyQt6.QtCore import Qt, QTimer, QSharedMemory
from PyQt6.QtWidgets import QApplication

# qfluentwidgets MessageBox（替代 QMessageBox，不触发系统音频线程→崩溃）
from qfluentwidgets import MessageBox

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
# 不再使用 dump_traceback_later（每 30s dump 一次在 4GB 机器上会加剧内存压力，
# 且 crash_trace.log 可达 87MB+。faulthandler.enable() 已在 segfault 时 dump，足够。）

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
    # —— 看门狗联动：写 pid 文件，供 scripts/watchdog.py 监控存活 ——
    # 正常退出时 atexit 删除 pid（看门狗静默）；崩溃时进程被 OS 杀死、pid 残留
    # → 看门狗据此区分「正常关闭」与「崩溃」。同时清除上次崩溃通知记录，
    #   让本会话若再次崩溃仍可推送（避免标记残留导致漏通知）。
    _root = Path(__file__).resolve().parent
    _pid_file = _root / "temp" / "agent.pid"
    _last_notified = _root / "temp" / "agent.last_notified_pid"
    _legacy_crash_notified = _root / "temp" / "agent.crash_notified"
    try:
        _pid_file.parent.mkdir(parents=True, exist_ok=True)
        # 先删除可能残留的旧 pid，避免看门狗在 app 启动瞬间仍读到旧 pid 而误报
        if _pid_file.exists():
            _pid_file.unlink()
        _pid_file.write_text(str(os.getpid()), encoding="utf-8")
        if _last_notified.exists():
            _last_notified.unlink()
        if _legacy_crash_notified.exists():
            _legacy_crash_notified.unlink()
    except Exception:
        pass

    def _cleanup_pid():
        try:
            if _pid_file.exists():
                _pid_file.unlink()
        except Exception:
            pass

    atexit.register(_cleanup_pid)

    # 设置 Playwright 浏览器路径
    browsers_path = setup_playwright_browsers_path()

    # 创建应用 — 通过 -style 参数在 QApplication 构造时就指定 windowsvista 风格，
    # 阻止 Qt6 默认加载 qmodernwindowsstyle.dll（该 DLL 的 D2D 渲染线程会破坏堆）。
    # setStyle() 在构造后调用太晚，DLL 已被加载且内部线程已启动。
    # 7次 dump 证实：崩溃线程 RIP 始终是堆地址（末三位 0xfc0），
    # 且 TID=0x22a4 线程栈里有 qmodernwindowsstyle.dll → 崩溃与之直接关联。
    _qt_args = ["-style", "windowsvista"] + sys.argv
    app = QApplication(_qt_args)
    app.setApplicationName("Agent-Customer")

    # 强制使用 windowsvista 风格，避免 Qt6 默认的 windows11（ModernWindows）风格。
    # ModernWindows 风格会加载 qmodernwindowsstyle.dll → d2d1.dll → d3d11.dll →
    # d3d10warp.dll（WARP 软件光栅化器）。dump 显示 d3d10warp.dll 被加载后卸载，
    # 崩溃线程 RIP 指向已释放的堆地址，符合 WARP 函数指针悬空特征。
    # windowsvista 风格使用传统 GDI 绘制，不依赖 D2D/WARP，可彻底规避该崩溃链。
    try:
        from PyQt6.QtWidgets import QStyleFactory
        if "windowsvista" in QStyleFactory.keys():
            app.setStyle("windowsvista")
        else:
            app.setStyle("Fusion")
    except Exception:
        pass

    # 禁用 Qt 动画效果，减少聊天 tab 等大量 widget 切换/重绘时的渲染负担，
    # 降低在 4GB 内存机器上触发 D3D/WARP/ntdll 堆崩溃的概率。
    try:
        for effect in (
            Qt.UIEffect.UI_AnimateMenu,
            Qt.UIEffect.UI_FadeMenu,
            Qt.UIEffect.UI_AnimateCombo,
            Qt.UIEffect.UI_AnimateTooltip,
            Qt.UIEffect.UI_FadeTooltip,
            Qt.UIEffect.UI_AnimateToolBox,
        ):
            QApplication.setEffectEnabled(effect, False)
    except Exception:
        pass

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
            MessageBox("程序已在运行", "拼多多AI客服助手已经在运行中，请勿重复启动。", None).exec()
            sys.exit(1)
        else:
            # create 和 attach 都失败 — 尝试清理后重新 create
            shared_mem.detach()
            if not shared_mem.create(1):
                MessageBox("启动失败", "无法创建实例检查器，请检查权限或重启电脑后再试。", None).exec()
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
    # 高频心跳：每 2 秒写一行到 app.log，精确记录崩溃前最后活动时间
    _heartbeat_logger = _get_logger("Heartbeat")
    _hb_timer = QTimer()
    _hb_timer.setInterval(2000)
    _hb_counter = [0]
    def _do_heartbeat():
        _hb_counter[0] += 1
        _heartbeat_logger.info(f"alive #{_hb_counter[0]}")
    _hb_timer.timeout.connect(_do_heartbeat)
    _hb_timer.start()

    exit_code = app.exec()
    
    # 退出时释放共享内存
    if shared_mem.isAttached():
        shared_mem.detach()
    
    sys.exit(exit_code)

if __name__ == '__main__':
    main()
