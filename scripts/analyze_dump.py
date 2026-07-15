"""分析 minidump 文件，提取崩溃线程的调用堆栈和关键信息。"""
import sys

dump_path = r'C:\Users\Administrator\Documents\git\Customer-Agent\temp\dumps\python.exe.9220.dmp'

from minidump.minidumpfile import MinidumpFile
from minidump.minidumpreader import MinidumpFileReader

print(f"Loading dump: {dump_path}")
mf = MinidumpFile.parse(dump_path)
reader = MinidumpFileReader(mf)

# 系统信息
if mf.sysinfo:
    si = mf.sysinfo
    print(f"OS Build: {si.BuildNumber}")
    print(f"Arch: {si.ProcessorArchitecture}")
    print(f"CPUs: {si.NumberOfProcessors}")

# 异常信息
crash_addr = 0
crash_tid = 0
if mf.exception and mf.exception.exception_records:
    exc_rec = mf.exception.exception_records[0]
    crash_tid = mf.exception.ThreadId
    crash_addr = exc_rec.ExceptionAddress
    print(f"\n=== Exception ===")
    print(f"Thread ID: {crash_tid:#x}")
    print(f"Exception Code: {exc_rec.ExceptionCode:#010x}")
    print(f"Exception Address: {crash_addr:#018x}")
    if exc_rec.NumberParameters >= 2:
        op = exc_rec.ExceptionInformation[0]
        target = exc_rec.ExceptionInformation[1]
        op_name = {0: "READ", 1: "WRITE", 8: "EXECUTE"}.get(op, f"UNKNOWN({op})")
        print(f"Access: {op_name} at {target:#018x}")

# 模块列表 - 找到崩溃地址所在的模块
if mf.modules:
    print(f"\n=== Module containing crash address ===")
    for mod in mf.modules.modules:
        start = mod.baseaddress
        end = mod.baseaddress + mod.size
        if start <= crash_addr < end:
            print(f"  CRASH MODULE: {mod.name}")
            print(f"  Base: {start:#018x}, Size: {mod.size}, End: {end:#018x}")
            print(f"  Offset in module: {crash_addr - start:#x}")
            break
    else:
        print(f"  Crash address {crash_addr:#018x} not in any loaded module (heap/JIT code)")

    # 打印关键模块
    print(f"\n=== Key modules ===")
    for mod in mf.modules.modules:
        name = mod.name.split("\\")[-1] if mod.name else "unknown"
        if any(kw in name.lower() for kw in ['python3', 'pyqt', 'qt6core', 'qt6gui', 'lance', 'tantivy', 'numpy', 'pyarrow', 'pil', 'sqlite', 'ntdll', 'kernelbase']):
            print(f"  {name}: base={mod.baseaddress:#018x} size={mod.size}")

# 线程列表
if mf.threads:
    print(f"\n=== Threads ({len(mf.threads.threads)}) ===")
    for i, t in enumerate(mf.threads.threads):
        marker = " <--- CRASH THREAD" if t.ThreadId == crash_tid else ""
        if marker or i < 3 or i > len(mf.threads.threads) - 3:
            print(f"  Thread {i}: TID={t.ThreadId:#x}{marker}")

# 尝试获取崩溃线程的堆栈
print(f"\n=== Crash thread stack ===")
try:
    for i, t in enumerate(mf.threads.threads):
        if t.ThreadId == crash_tid:
            print(f"  TID={t.ThreadId:#x}")
            print(f"  Stack: {t.Stack.StartMemory:#018x} - {t.Stack.StartMemory + t.Stack.Size:#018x}")

            # 尝试用 reader 获取上下文和堆栈回溯
            ctx = reader.get_thread_context(t)
            if ctx:
                print(f"  RSP: {ctx.RSP:#018x}")
                print(f"  RIP: {ctx.RIP:#018x}")

                # 尝试回溯堆栈
                try:
                    stack = reader.get_return_values(t)
                    print(f"  Return addresses on stack:")
                    for addr in stack[:20]:
                        # 找到地址对应的模块
                        for mod in mf.modules.modules:
                            if mod.baseaddress <= addr < mod.baseaddress + mod.size:
                                mod_name = mod.name.split("\\")[-1]
                                offset = addr - mod.baseaddress
                                print(f"    {addr:#018x} {mod_name}+{offset:#x}")
                                break
                        else:
                            print(f"    {addr:#018x} (unknown)")
                except Exception as e:
                    print(f"  Stack walk failed: {e}")
            break
except Exception as e:
    print(f"  Error reading crash thread: {e}")

print("\nDone.")
