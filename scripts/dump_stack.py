"""深度分析 minidump — 读取崩溃线程的完整栈，找所有模块的返回地址"""
import struct
import sys

dump_path = r'C:\Users\Administrator\Documents\git\Customer-Agent\temp\dumps\python.exe.1412.dmp'

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
num_params = exc.ExceptionRecord.NumberParameters
if num_params >= 2:
    print(f"Access: Info[0]={exc.ExceptionRecord.ExceptionInformation[0]}, target={exc.ExceptionRecord.ExceptionInformation[1]:#018x}")

# 直接从文件读取 ThreadContext
tc = exc.ThreadContext
with open(dump_path, 'rb') as f:
    f.seek(tc.Rva)
    ctx_data = f.read(tc.DataSize)

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
crash_mod = None
for mod in mf.modules.modules:
    start = mod.baseaddress
    end = mod.baseaddress + mod.size
    if start <= crash_rip < end:
        mod_name = mod.name.replace('\\', '/').split('/')[-1]
        print(f"CRASH MODULE: {mod_name}")
        print(f"  Full path: {mod.name}")
        print(f"  Base: {start:#018x}")
        print(f"  Offset: {crash_rip - start:#x}")
        crash_mod = mod
        break
else:
    print(f"Crash address {crash_rip:#018x} is NOT in any loaded module (heap/JIT code)")

# 扫描崩溃线程的栈内存 — 扫描更大的范围
print(f"\n=== Stack scan (return addresses) ===")

for t in mf.threads.threads:
    if t.ThreadId == crash_tid:
        stack_start = t.Stack.StartOfMemoryRange
        stack_size = t.Stack.DataSize
        stack_end = stack_start + stack_size
        print(f"Stack: {stack_start:#018x} - {stack_end:#018x}")
        print(f"RSP:   {rsp:#018x}")
        break

# 扫描整个栈（不只是 0x4000）
scan_start = rsp
scan_size = min(stack_end - rsp, 0x20000)  # 扫描 128KB

try:
    stack_data = reader.read(scan_start, scan_size)
    print(f"Read {len(stack_data)} bytes from stack")

    found = 0
    seen = set()
    for offset in range(0, len(stack_data) - 8, 8):
        val = struct.unpack_from('<Q', stack_data, offset)[0]
        for mod in mf.modules.modules:
            if mod.baseaddress <= val < mod.baseaddress + mod.size:
                if val not in seen:
                    mod_name = mod.name.replace('\\', '/').split('/')[-1]
                    mod_offset = val - mod.baseaddress
                    print(f"  RSP+{offset:#06x}: {val:#018x} {mod_name}+{mod_offset:#x}")
                    seen.add(val)
                    found += 1
                break
        if found >= 80:
            break

    if found == 0:
        print("  No return addresses found in stack scan")

except Exception as e:
    print(f"  Stack read failed: {e}")

# 检查崩溃地址附近是否有可执行内存（可能是 JIT 或 shellcode）
print(f"\n=== Memory region around crash RIP ===")
crash_page = crash_rip & ~0xFFF
for info in mf.memory_info.infos:
    if info.BaseAddress <= crash_rip < info.BaseAddress + info.RegionSize:
        print(f"Region: {info.BaseAddress:#018x} - {info.BaseAddress + info.RegionSize:#018x}")
        print(f"  State: {info.State:#x}")
        print(f"  Protect: {info.Protect:#x}")
        print(f"  Type: {info.Type:#x}")
        # 0x10 = PAGE_EXECUTE
        # 0x20 = PAGE_EXECUTE_READ
        # 0x40 = PAGE_EXECUTE_READWRITE
        # 0x80 = PAGE_EXECUTE_WRITECOPY
        protect_names = {0x10: "EXECUTE", 0x20: "EXECUTE_READ", 0x40: "EXECUTE_READWRITE",
                        0x02: "READONLY", 0x04: "READ_WRITE", 0x08: "WRITECOPY"}
        print(f"  Protect name: {protect_names.get(info.Protect, 'UNKNOWN')}")
        break

# 列出所有包含 EXECUTE 的内存区域（可能是 JIT 代码所在）
print(f"\n=== All EXECUTE memory regions ===")
execute_regions = []
for info in mf.memory_info.infos:
    if info.Protect in (0x10, 0x20, 0x40, 0x80) and info.State == 0x1000:  # MEM_COMMIT
        if info.Baseaddress <= crash_rip < info.BaseAddress + info.RegionSize:
            execute_regions.append((info, "*** CRASH HERE ***"))
        else:
            execute_regions.append((info, ""))

for info, marker in execute_regions[:20]:
    print(f"  {info.BaseAddress:#018x} - {info.BaseAddress + info.RegionSize:#018x} protect={info.Protect:#x} {marker}")

# 检查卸载的模块
if mf.unloaded_modules:
    print(f"\n=== Recently unloaded modules ===")
    for mod in mf.unloaded_modules.modules[:10]:
        print(f"  {mod.name} (base={mod.baseaddress:#018x} size={mod.size})")
