"""
三层崩溃捕获系统

1. Windows VEH (Vectored Exception Handler): 捕获 C++ 级 access violation 等
2. 心跳文件: 定时写入，启动时检测上次是否异常退出
3. Windows WER LocalDumps: 系统级 minidump 自动生成
"""
import sys
import os
import time
import json
import atexit
import threading
import traceback
from pathlib import Path
from datetime import datetime

_CRASH_LOG_DIR = Path("./temp")
_HEARTBEAT_FILE = _CRASH_LOG_DIR / "heartbeat.json"
_CRASH_LOG_FILE = _CRASH_LOG_DIR / "crash_veh.log"

# ============================================================================
# Layer 1: VEH — Windows 向量化异常处理器
# ============================================================================

# Windows SEH 异常代码
EXCEPTION_ACCESS_VIOLATION = 0xC0000005
EXCEPTION_STACK_OVERFLOW = 0xC00000FD
EXCEPTION_ILLEGAL_INSTRUCTION = 0xC000001D
EXCEPTION_IN_PAGE_ERROR = 0xC0000006
EXCEPTION_BREAKPOINT = 0x80000003
EXCEPTION_DATATYPE_MISALIGNMENT = 0x80000002
EXCEPTION_FLT_DIVIDE_BY_ZERO = 0xC000008E
EXCEPTION_FLT_INVALID_OPERATION = 0xC0000090

_EXCEPTION_NAMES = {
    0xC0000005: "ACCESS_VIOLATION",
    0xC00000FD: "STACK_OVERFLOW",
    0xC000001D: "ILLEGAL_INSTRUCTION",
    0xC0000006: "IN_PAGE_ERROR",
    0x80000003: "BREAKPOINT",
    0x80000002: "DATATYPE_MISALIGNMENT",
    0xC000008E: "FLT_DIVIDE_BY_ZERO",
    0xC0000090: "FLT_INVALID_OPERATION",
}

_veh_registered = False
_veh_handle = None


def _install_veh():
    """安装 Windows VEH，在 C++ 崩溃时捕获异常信息"""
    global _veh_registered, _veh_handle

    if sys.platform != "win32":
        return

    import ctypes
    from ctypes import wintypes

    kernel32 = ctypes.windll.kernel32

    # EXCEPTION_POINTERS 结构
    class EXCEPTION_RECORD(ctypes.Structure):
        pass

    EXCEPTION_RECORD._fields_ = [
        ("ExceptionCode", wintypes.DWORD),
        ("ExceptionFlags", wintypes.DWORD),
        ("ExceptionRecord", ctypes.POINTER(EXCEPTION_RECORD)),
        ("ExceptionAddress", ctypes.c_void_p),
        ("NumberParameters", wintypes.DWORD),
        ("ExceptionInformation", ctypes.c_ulonglong * 15),
    ]

    class CONTEXT(ctypes.Structure):
        _fields_ = [("dummy", ctypes.c_byte * 1232)]

    class EXCEPTION_POINTERS(ctypes.Structure):
        _fields_ = [
            ("ExceptionRecord", ctypes.POINTER(EXCEPTION_RECORD)),
            ("ContextRecord", ctypes.POINTER(CONTEXT)),
        ]

    # VEH 回调类型
    PVECTORED_EXCEPTION_HANDLER = ctypes.WINFUNCTYPE(
        ctypes.c_long,
        ctypes.POINTER(EXCEPTION_POINTERS),
    )

    # 常量
    EXCEPTION_CONTINUE_SEARCH = 0
    EXCEPTION_CONTINUE_EXECUTION = -1

    @PVECTORED_EXCEPTION_HANDLER
    def _veh_handler(exception_info):
        """VEH 回调：在崩溃时写入 crash log"""
        try:
            record = exception_info[0].ExceptionRecord[0]
            code = record.ExceptionCode
            addr = record.ExceptionAddress or 0

            name = _EXCEPTION_NAMES.get(code, f"0x{code:08X}")

            # 只记录致命异常
            fatal_codes = {
                EXCEPTION_ACCESS_VIOLATION,
                EXCEPTION_STACK_OVERFLOW,
                EXCEPTION_ILLEGAL_INSTRUCTION,
                EXCEPTION_IN_PAGE_ERROR,
                EXCEPTION_FLT_DIVIDE_BY_ZERO,
                EXCEPTION_FLT_INVALID_OPERATION,
            }
            if code not in fatal_codes:
                return EXCEPTION_CONTINUE_SEARCH

            lines = []
            lines.append(
                f"\n{'='*60}\n"
                f"=== VEH CRASH DETECTED at {datetime.now().strftime('%Y-%m-%d %H:%M:%S')} ===\n"
                f"Exception: {name} (code={code:#010x})\n"
                f"Address: {addr:#018x}\n"
                f"Access Type: "
            )

            if code == EXCEPTION_ACCESS_VIOLATION and record.NumberParameters >= 2:
                op = record.ExceptionInformation[0]
                target = record.ExceptionInformation[1]
                op_name = {0: "READ", 1: "WRITE", 8: "EXECUTE"}.get(op, f"UNKNOWN({op})")
                lines.append(f"{op_name} at {target:#018x}\n")
            else:
                lines.append("N/A\n")

            # Python 堆栈
            lines.append("--- Python Stack ---\n")
            for thread_id, stack in sys._current_frames().items():
                lines.append(f"\nThread 0x{thread_id:08x}:\n")
                for filename, lineno, name, line in traceback.extract_stack(stack):
                    lines.append(f"  File \"{filename}\", line {lineno}, in {name}\n")
                    if line:
                        lines.append(f"    {line.strip()}\n")

            lines.append("=" * 60 + "\n")

            _CRASH_LOG_DIR.mkdir(parents=True, exist_ok=True)
            with open(_CRASH_LOG_FILE, "a", encoding="utf-8") as f:
                f.writelines(lines)
                f.flush()
                os.fsync(f.fileno())

        except Exception:
            pass  # VEH 中绝对不能二次崩溃

        return EXCEPTION_CONTINUE_SEARCH

    # 注册 VEH (1 = 最先被调用)
    AddVectoredExceptionHandler = kernel32.AddVectoredExceptionHandler
    AddVectoredExceptionHandler.argtypes = [ctypes.c_ulong, PVECTORED_EXCEPTION_HANDLER]
    AddVectoredExceptionHandler.restype = ctypes.c_void_p

    _veh_handle = AddVectoredExceptionHandler(1, _veh_handler)
    _veh_registered = True


# ============================================================================
# Layer 2: 心跳文件
# ============================================================================

_heartbeat_running = False
_heartbeat_thread = None


def _heartbeat_loop(interval: float = 5.0):
    """每 N 秒写心跳文件"""
    while _heartbeat_running:
        try:
            data = {
                "timestamp": time.time(),
                "datetime": datetime.now().isoformat(),
                "pid": os.getpid(),
            }
            _CRASH_LOG_DIR.mkdir(parents=True, exist_ok=True)
            with open(_HEARTBEAT_FILE, "w", encoding="utf-8") as f:
                json.dump(data, f)
                f.flush()
                os.fsync(f.fileno())
        except Exception:
            pass
        time.sleep(interval)


def _cleanup_heartbeat():
    """正常退出时标记"""
    try:
        data = {
            "timestamp": time.time(),
            "datetime": datetime.now().isoformat(),
            "pid": os.getpid(),
            "clean_exit": True,
        }
        _CRASH_LOG_DIR.mkdir(parents=True, exist_ok=True)
        with open(_HEARTBEAT_FILE, "w", encoding="utf-8") as f:
            json.dump(data, f)
            f.flush()
            os.fsync(f.fileno())
    except Exception:
        pass


def start_heartbeat(interval: float = 5.0):
    """启动心跳写入线程"""
    global _heartbeat_running, _heartbeat_thread
    if _heartbeat_running:
        return
    _heartbeat_running = True
    _heartbeat_thread = threading.Thread(target=_heartbeat_loop, args=(interval,), daemon=True)
    _heartbeat_thread.start()
    atexit.register(_cleanup_heartbeat)


# ============================================================================
# Layer 3: Windows WER LocalDumps
# ============================================================================

def _enable_wer_localdumps():
    """
    启用 Windows Error Reporting LocalDumps
    崩溃时自动生成 .dmp 文件到 ./temp/dumps/
    """
    if sys.platform != "win32":
        return

    try:
        import winreg

        dump_dir = str((_CRASH_LOG_DIR / "dumps").resolve())
        os.makedirs(dump_dir, exist_ok=True)

        app_name = sys.executable.split("\\")[-1]

        # 在 HKLM 和 HKCU 都注册，确保 WER 能找到配置
        for hive in [winreg.HKEY_LOCAL_MACHINE, winreg.HKEY_CURRENT_USER]:
            try:
                key_path = rf"SOFTWARE\Microsoft\Windows\Windows Error Reporting\LocalDumps\{app_name}"
                key = winreg.CreateKey(hive, key_path)
                winreg.SetValueEx(key, "DumpFolder", 0, winreg.REG_EXPAND_SZ, dump_dir)
                winreg.SetValueEx(key, "DumpType", 0, winreg.REG_DWORD, 2)  # 2 = 完整 dump
                winreg.SetValueEx(key, "DumpCount", 0, winreg.REG_DWORD, 5)
                winreg.CloseKey(key)
            except Exception:
                pass  # HKLM 可能需要管理员权限

        # 同时注册通用 LocalDumps（不限定应用程序名）
        try:
            key_path = r"SOFTWARE\Microsoft\Windows\Windows Error Reporting\LocalDumps"
            key = winreg.CreateKey(winreg.HKEY_LOCAL_MACHINE, key_path)
            winreg.SetValueEx(key, "DumpFolder", 0, winreg.REG_EXPAND_SZ, dump_dir)
            winreg.SetValueEx(key, "DumpType", 0, winreg.REG_DWORD, 2)
            winreg.SetValueEx(key, "DumpCount", 0, winreg.REG_DWORD, 5)
            winreg.CloseKey(key)
        except Exception:
            pass

    except Exception:
        pass  # 权限不足等情况，静默失败


def _install_minidump_handler():
    """
    安装 C 级别的 minidump 生成器。
    在进程崩溃时通过 MiniDumpWriteDump 生成 .dmp 文件，
    比 WER LocalDumps 更可靠（WER 可能因为各种策略不生成 dump）。
    """
    if sys.platform != "win32":
        return

    try:
        import ctypes
        from ctypes import wintypes

        # dbghelp.dll 的 MiniDumpWriteDump 函数
        dbghelp = ctypes.windll.dbghelp

        # MiniDumpWriteDump 函数签名
        dbghelp.MiniDumpWriteDump.argtypes = [
            wintypes.HANDLE,   # hProcess
            wintypes.DWORD,    # ProcessId
            wintypes.HANDLE,   # hFile
            wintypes.DWORD,    # DumpType
            ctypes.c_void_p,   # ExceptionParam
            ctypes.c_void_p,   # UserStreamParam
            ctypes.c_void_p,   # CallbackParam
        ]
        dbghelp.MiniDumpWriteDump.restype = wintypes.BOOL

        # MINIDUMP_TYPE 标志
        MiniDumpNormal = 0x00000000
        MiniDumpWithFullMemory = 0x00000002
        MiniDumpWithHandleData = 0x00000004
        MiniDumpWithThreadInfo = 0x00001000
        MiniDumpWithUnloadedModules = 0x00000020

        dump_type = (MiniDumpNormal | MiniDumpWithHandleData |
                     MiniDumpWithUnloadedModules | MiniDumpWithThreadInfo)

        _dump_dir = _CRASH_LOG_DIR / "dumps"
        _dump_dir.mkdir(parents=True, exist_ok=True)

        # VEH 回调：在崩溃时生成 minidump
        kernel32 = ctypes.windll.kernel32

        class EXCEPTION_RECORD(ctypes.Structure):
            pass

        EXCEPTION_RECORD._fields_ = [
            ("ExceptionCode", wintypes.DWORD),
            ("ExceptionFlags", wintypes.DWORD),
            ("ExceptionRecord", ctypes.POINTER(EXCEPTION_RECORD)),
            ("ExceptionAddress", ctypes.c_void_p),
            ("NumberParameters", wintypes.DWORD),
            ("ExceptionInformation", ctypes.c_ulonglong * 15),
        ]

        class CONTEXT(ctypes.Structure):
            _fields_ = [("dummy", ctypes.c_byte * 1232)]

        class EXCEPTION_POINTERS(ctypes.Structure):
            _fields_ = [
                ("ExceptionRecord", ctypes.POINTER(EXCEPTION_RECORD)),
                ("ContextRecord", ctypes.POINTER(CONTEXT)),
            ]

        PVECTORED_EXCEPTION_HANDLER = ctypes.WINFUNCTYPE(
            ctypes.c_long,
            ctypes.POINTER(EXCEPTION_POINTERS),
        )

        EXCEPTION_CONTINUE_SEARCH = 0
        fatal_codes = {
            0xC0000005,  # ACCESS_VIOLATION
            0xC00000FD,  # STACK_OVERFLOW
            0xC000001D,  # ILLEGAL_INSTRUCTION
            0xC0000006,  # IN_PAGE_ERROR
            0xC000008E,  # FLT_DIVIDE_BY_ZERO
            0xC0000090,  # FLT_INVALID_OPERATION
            0xC0000409,  # STACK_BUFFER_OVERRUN
            0xC0000374,  # HEAP_CORRUPTION
        }

        @PVECTORED_EXCEPTION_HANDLER
        def _veh_with_dump(exception_info):
            try:
                record = exception_info[0].ExceptionRecord[0]
                code = record.ExceptionCode

                if code not in fatal_codes:
                    return EXCEPTION_CONTINUE_SEARCH

                name = _EXCEPTION_NAMES.get(code, f"0x{code:08X}")

                # 生成 minidump
                pid = os.getpid()
                from datetime import datetime
                dump_path = _dump_dir / f"crash_{datetime.now().strftime('%Y%m%d_%H%M%S')}_{pid}.dmp"

                # 创建文件句柄
                GENERIC_WRITE = 0x40000000
                CREATE_ALWAYS = 2
                file_handle = kernel32.CreateFileW(
                    str(dump_path),
                    GENERIC_WRITE,
                    0,
                    None,
                    CREATE_ALWAYS,
                    0,
                    None,
                )

                if file_handle:
                    success = dbghelp.MiniDumpWriteDump(
                        kernel32.GetCurrentProcess(),
                        pid,
                        file_handle,
                        dump_type,
                        exception_info,  # 传入异常信息
                        None,
                        None,
                    )
                    kernel32.CloseHandle(file_handle)

                    # 写入崩溃信息到日志
                    lines = [
                        f"\n{'='*60}\n",
                        f"=== CRASH DUMP at {datetime.now().strftime('%Y-%m-%d %H:%M:%S')} ===\n",
                        f"Exception: {name} (code={code:#010x})\n",
                        f"Address: {record.ExceptionAddress or 0:#018x}\n",
                        f"Dump saved to: {dump_path}\n",
                    ]
                    if code == 0xC0000005 and record.NumberParameters >= 2:
                        op = record.ExceptionInformation[0]
                        target = record.ExceptionInformation[1]
                        op_name = {0: "READ", 1: "WRITE", 8: "EXECUTE"}.get(op, f"UNKNOWN({op})")
                        lines.append(f"Access Type: {op_name} at {target:#018x}\n")

                    with open(_CRASH_LOG_FILE, "a", encoding="utf-8") as f:
                        f.writelines(lines)
                        f.flush()

            except Exception:
                pass  # VEH 中绝对不能二次崩溃

            return EXCEPTION_CONTINUE_SEARCH

        # 注册 VEH (1 = 最先被调用)
        kernel32.AddVectoredExceptionHandler.argtypes = [wintypes.ULONG, PVECTORED_EXCEPTION_HANDLER]
        kernel32.AddVectoredExceptionHandler.restype = ctypes.c_void_p
        kernel32.AddVectoredExceptionHandler(1, _veh_with_dump)

    except Exception:
        pass  # 安装失败不影响程序运行


# ============================================================================
# 汇总启动函数
# ============================================================================

_last_crash_info = None


def check_previous_crash() -> dict | None:
    """检查上次运行是否异常退出"""
    global _last_crash_info

    if not _HEARTBEAT_FILE.exists():
        return None

    try:
        with open(_HEARTBEAT_FILE, "r", encoding="utf-8") as f:
            data = json.load(f)

        if data.get("clean_exit"):
            return None  # 正常退出

        last_ts = data.get("timestamp", 0)
        now = time.time()
        elapsed = now - last_ts

        # 心跳间隔 5s，如果超过 15s 未更新 → 异常退出
        if elapsed > 15:
            info = {
                "last_heartbeat": data.get("datetime", "unknown"),
                "seconds_ago": round(elapsed),
                "pid": data.get("pid", "unknown"),
            }
            _last_crash_info = info
            return info
    except (json.JSONDecodeError, KeyError):
        pass

    return None


def get_last_crash_info() -> dict | None:
    """获取最近一次 crash 信息"""
    return _last_crash_info


def setup_crash_detection(enable_wer: bool = True, heartbeat_interval: float = 5.0):
    """
    初始化三层崩溃捕获

    Args:
        enable_wer: 是否启用 Windows Error Reporting minidump
        heartbeat_interval: 心跳写入间隔（秒）
    """
    # Layer 1: VEH (含 minidump 生成)
    # 临时禁用 VEH 排查聊天 tab 崩溃：
    # VEH 回调用 ctypes.WINFUNCTYPE 创建，代码块在堆上。
    # Qt 内部产生可恢复的 ACCESS_VIOLATION 时 VEH 被调用，
    # _veh_with_dump 里的 MiniDumpWriteDump 大量操作堆，可能破坏 ntdll 堆。
    # 聊天 tab 创建卡片时易触发 Qt 内部异常 -> VEH -> 堆损坏 -> 崩在 0xfc0。
    # 先禁用验证假设。如果禁用后不崩，说明 VEH 是元凶。
    # _install_veh()
    try:
        import logging as _l
        _l.getLogger(__name__).info("VEH 已临时禁用（排查聊天 tab 崩溃）")
    except Exception:
        pass

    # Layer 1b: 增强 VEH（带 minidump 生成，比 WER 更可靠）
    # _install_minidump_handler()  # 同上临时禁用

    # Layer 2: 心跳
    start_heartbeat(heartbeat_interval)

    # Layer 3: WER (作为备份)
    if enable_wer:
        _enable_wer_localdumps()

    # 检查上次是否 crash
    prev = check_previous_crash()
    return prev