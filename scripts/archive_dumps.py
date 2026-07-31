#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""
批量分析 minidump 文件，生成文字报告后删除原 dmp 以释放空间。
"""
import os
import sys
import json
from datetime import datetime
from pathlib import Path

from minidump.minidumpfile import MinidumpFile

PROJECT_ROOT = Path(r"C:\Users\Administrator\Documents\git\Customer-Agent")
DUMPS_DIR = PROJECT_ROOT / "temp" / "dumps"
REPORTS_DIR = PROJECT_ROOT / "temp" / "dump_reports"

INTERESTING_MODULES = {
    'ntdll.dll', 'kernelbase.dll', 'qt6core.dll', 'qt6gui.dll', 'qt6widgets.dll',
    'd3d11.dll', 'd3d12.dll', 'd3d9.dll', 'd3d10warp.dll', 'gdi32.dll', 'gdi32full.dll',
    'win32u.dll', 'user32.dll', 'winmm.dll', 'winmmbase.dll', 'mmdevapi.dll', 'tiptsf.dll',
    'msvcrt.dll', 'ucrtbase.dll', 'python311.dll', 'python3.dll', 'qwindows.dll', '_lancedb.pyd'
}


def format_size(size_bytes: int) -> str:
    for unit in ['B', 'KB', 'MB', 'GB']:
        if size_bytes < 1024:
            return f"{size_bytes:.2f} {unit}"
        size_bytes /= 1024
    return f"{size_bytes:.2f} TB"


def analyze_dump(dump_path: Path) -> str:
    lines = []
    lines.append(f"Dump File: {dump_path.name}")
    lines.append(f"Original Path: {dump_path}")
    lines.append(f"File Size: {format_size(dump_path.stat().st_size)}")
    lines.append(f"Modified Time: {datetime.fromtimestamp(dump_path.stat().st_mtime)}")
    lines.append("")

    try:
        mf = MinidumpFile.parse(str(dump_path))
    except Exception as e:
        lines.append(f"ERROR parsing dump: {e}")
        return "\n".join(lines)

    if mf.sysinfo:
        si = mf.sysinfo
        lines.append("=== System Info ===")
        lines.append(f"OS Build: {si.BuildNumber}")
        lines.append(f"Architecture: {si.ProcessorArchitecture}")
        lines.append(f"CPUs: {si.NumberOfProcessors}")
        lines.append("")

    if mf.exception and mf.exception.exception_records:
        exc = mf.exception.exception_records[0]
        crash_addr = exc.ExceptionRecord.ExceptionAddress
        crash_tid = exc.ThreadId
        lines.append("=== Exception ===")
        lines.append(f"Thread ID: {crash_tid:#x}")
        lines.append(f"Exception Code: {exc.ExceptionRecord.ExceptionCode_raw:#010x}")
        lines.append(f"Exception Address: {crash_addr:#018x}")
        if exc.ExceptionRecord.NumberParameters >= 2:
            op = exc.ExceptionRecord.ExceptionInformation[0]
            target = exc.ExceptionRecord.ExceptionInformation[1]
            op_name = {0: 'READ', 1: 'WRITE', 8: 'EXECUTE'}.get(op, f'UNKNOWN({op})')
            lines.append(f"Access: {op_name} at {target:#018x}")
        lines.append("")

    lines.append("=== Crash Module ===")
    crash_module = None
    for mod in mf.modules.modules:
        start = mod.baseaddress
        end = mod.baseaddress + mod.size
        if start <= crash_addr < end:
            crash_module = mod
            lines.append(f"  Module: {mod.name}")
            lines.append(f"  Base: {start:#018x}, Size: {mod.size}, End: {end:#018x}")
            lines.append(f"  Offset: {crash_addr - start:#x}")
            break
    if not crash_module:
        lines.append(f"  Crash address {crash_addr:#018x} not in any loaded module (heap/JIT code)")
    lines.append("")

    lines.append("=== Key Modules ===")
    for mod in mf.modules.modules:
        name = os.path.basename(mod.name).lower() if mod.name else 'unknown'
        if name in INTERESTING_MODULES:
            lines.append(f"  {name}: base={mod.baseaddress:#018x} size={mod.size}")
    lines.append("")

    lines.append(f"=== Threads ({len(mf.threads.threads)}) ===")
    for i, t in enumerate(mf.threads.threads):
        marker = ' <--- CRASH THREAD' if t.ThreadId == crash_tid else ''
        if marker or i < 3 or i > len(mf.threads.threads) - 4:
            lines.append(f"  Thread {i}: TID={t.ThreadId:#x}{marker}")
    lines.append("")

    lines.append("=== Recently Unloaded Modules ===")
    if mf.unloaded_modules:
        for mod in mf.unloaded_modules.modules[:20]:
            lines.append(f"  {mod.name} (base={mod.baseaddress:#018x} size={mod.size})")
    else:
        lines.append("  None")
    lines.append("")

    lines.append("=== Memory Info Around Crash ===")
    if mf.memory_info:
        for info in mf.memory_info.infos:
            if info.BaseAddress <= crash_addr < info.BaseAddress + info.RegionSize:
                lines.append(f"  Region: {info.BaseAddress:#018x} - {info.BaseAddress + info.RegionSize:#018x}")
                lines.append(f"  State: {info.State}, Protect: {info.Protect}, Type: {info.Type}")
                break
    lines.append("")
    lines.append("=" * 60)
    return "\n".join(lines)


def main():
    REPORTS_DIR.mkdir(parents=True, exist_ok=True)
    summary_lines = []
    summary_lines.append(f"Dump Archive Report generated at {datetime.now()}")
    summary_lines.append("")

    total_saved = 0
    analyzed = []

    dump_files = sorted(DUMPS_DIR.glob("*.dmp"))
    for dump_path in dump_files:
        report_path = REPORTS_DIR / f"{dump_path.stem}.txt"
        print(f"Analyzing {dump_path.name}...")
        report_text = analyze_dump(dump_path)
        report_path.write_text(report_text, encoding="utf-8")
        size = dump_path.stat().st_size
        total_saved += size
        analyzed.append({
            "file": dump_path.name,
            "size": size,
            "report": str(report_path.relative_to(PROJECT_ROOT)),
        })
        print(f"  -> Report saved: {report_path}")
        try:
            dump_path.unlink()
            print(f"  -> Deleted: {dump_path.name}")
        except Exception as e:
            print(f"  -> Failed to delete {dump_path.name}: {e}")

    # Record Crashpad dumps in user_data
    crashpad_dumps = list((PROJECT_ROOT / "user_data").rglob("*.dmp"))
    if crashpad_dumps:
        summary_lines.append("=== Crashpad Dumps (Playwright/Chrome) ===")
        for p in sorted(crashpad_dumps):
            size = p.stat().st_size
            summary_lines.append(f"  {p.relative_to(PROJECT_ROOT)}: {format_size(size)} (modified {datetime.fromtimestamp(p.stat().st_mtime)})")
            total_saved += size
        summary_lines.append("")

    summary_lines.append("=== Analyzed Minidumps ===")
    for item in analyzed:
        summary_lines.append(f"  {item['file']}: {format_size(item['size'])} -> {item['report']}")
    summary_lines.append("")
    summary_lines.append(f"Total disk space saved (deleted dmp): {format_size(total_saved)}")

    summary_path = REPORTS_DIR / "summary.txt"
    summary_path.write_text("\n".join(summary_lines), encoding="utf-8")
    print(f"\nSummary saved: {summary_path}")
    print(f"Total saved: {format_size(total_saved)}")


if __name__ == "__main__":
    main()
