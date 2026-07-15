"""
分析 minidump 崩溃线程 — 直接从文件读取上下文和栈。
"""
import struct

dump_path = r'C:\Users\Administrator\Documents\git\Customer-Agent\temp\dumps\python.exe.9220.dmp'

from minidump.minidumpfile import MinidumpFile
from minidump.minidumpreader import MinidumpFileReader

mf = MinidumpFile.parse(dump_path)
reader = MinidumpFileReader(mf)

# 获取异常信息
exc = mf.exception.exception_records[0]
crash_tid = exc.ThreadId
crash_rip = exc.ExceptionRecord.ExceptionAddress
print(f"Crash TID: {crash_tid:#x}")
print(f"Crash RIP: {crash_rip:#018x}")
print(f"Exception Code: {exc.ExceptionRecord.ExceptionCode_raw:#010x}")
print(f"Access: WRITE to {exc.ExceptionRecord.ExceptionInformation[1]:#018x}")

# 直接从文件读取 ThreadContext
tc = exc.ThreadContext
print(f"\nThreadContext: Rva={tc.Rva}, DataSize={tc.DataSize}")

with open(dump_path, 'rb') as f:
    f.seek(tc.Rva)
    ctx_data = f.read(tc.DataSize)
print(f"Read {len(ctx_data)} bytes of context from file offset {tc.Rva}")

# x64 CONTEXT 寄存器偏移
def read_reg(offset):
    return struct.unpack_from('<Q', ctx_data, offset)[0]

rip = read_reg(0xF8)
rsp = read_reg(0x98)
rax = read_reg(0x78)
rcx = read_reg(0x80)
rdx = read_reg(0x88)
rbx = read_reg(0x90)
rsi = read_reg(0xA8)
rdi = read_reg(0xB0)
r8 = read_reg(0xB8)
r9 = read_reg(0xC0)
r12 = read_reg(0xD8)
r13 = read_reg(0xE0)
r14 = read_reg(0xE8)
r15 = read_reg(0xF0)

print(f"\n=== Registers ===")
print(f"RIP: {rip:#018x}")
print(f"RSP: {rsp:#018x}")
print(f"RAX: {rax:#018x}")
print(f"RCX: {rcx:#018x}")
print(f"RDX: {rdx:#018x}")
print(f"RBX: {rbx:#018x}")
print(f"RSI: {rsi:#018x}")
print(f"RDI: {rdi:#018x}")
print(f"R8:  {r8:#018x}")
print(f"R9:  {r9:#018x}")
print(f"R12: {r12:#018x}")
print(f"R13: {r13:#018x}")
print(f"R14: {r14:#018x}")
print(f"R15: {r15:#018x}")

# 崩溃地址分析
print(f"\n=== Crash address analysis ===")
for mod in mf.modules.modules:
    start = mod.baseaddress
    end = mod.baseaddress + mod.size
    if start <= crash_rip < end:
        mod_name = mod.name.replace('\\', '/').split('/')[-1]
        print(f"CRASH MODULE: {mod_name}")
        print(f"  Base: {start:#018x}")
        print(f"  Offset: {crash_rip - start:#x}")
        break
else:
    print(f"Crash address {crash_rip:#018x} is NOT in any loaded module (heap/JIT code)")
    print("Likely a C++ virtual method call on freed/corrupted object (use-after-free)")

# 扫描崩溃线程的栈内存中的返回地址
print(f"\n=== Stack scan (return addresses) ===")

# 找到崩溃线程的栈范围
for t in mf.threads.threads:
    if t.ThreadId == crash_tid:
        stack_start = t.Stack.StartOfMemoryRange
        stack_size = t.Stack.DataSize
        stack_end = stack_start + stack_size
        print(f"Stack: {stack_start:#018x} - {stack_end:#018x}")
        print(f"RSP:   {rsp:#018x}")
        break

# 读取 RSP 到栈顶的内存
scan_start = rsp
scan_size = min(stack_end - rsp, 0x4000)  # 最多扫描 16KB

try:
    stack_data = reader.read(scan_start, scan_size)
    print(f"Read {len(stack_data)} bytes from stack (RSP to RSP+{scan_size:#x})")

    # 扫描 8 字节对齐的地址，找返回地址
    found = 0
    seen = set()
    for offset in range(0, len(stack_data) - 8, 8):
        val = struct.unpack_from('<Q', stack_data, offset)[0]
        # 检查是否是有效的代码地址（在某个模块范围内）
        for mod in mf.modules.modules:
            if mod.baseaddress <= val < mod.baseaddress + mod.size:
                if val not in seen:
                    mod_name = mod.name.replace('\\', '/').split('/')[-1]
                    mod_offset = val - mod.baseaddress
                    print(f"  RSP+{offset:#06x}: {val:#018x} {mod_name}+{mod_offset:#x}")
                    seen.add(val)
                    found += 1
                break
        if found >= 40:
            break

    if found == 0:
        print("  No return addresses found in stack scan")

except Exception as e:
    print(f"  Stack read failed: {e}")

# 检查卸载的模块
if mf.unloaded_modules:
    print(f"\n=== Recently unloaded modules ===")
    for mod in mf.unloaded_modules.modules[:10]:
        print(f"  {mod.name} (base={mod.baseaddress:#018x} size={mod.size})")
