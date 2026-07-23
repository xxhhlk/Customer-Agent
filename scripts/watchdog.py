#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""独立看门狗：监控主程序存活，崩溃时推送 Bark 通知。

仅依赖标准库，无第三方包。常驻内存约 15-25MB，轮询极轻（每 30s 一次），
主程序崩溃后仍可独立运行并上报。

用法：
  python scripts/watchdog.py            # 常驻循环，每 30s 检测一次（Ctrl+C 退出）
  python scripts/watchdog.py once       # 单次检测（调试用：需 temp/agent.pid 指向已死进程才会推送）
  python scripts/watchdog.py test       # 直接发一条测试通知，验证 Bark key/链路是否正常（不依赖 pid）
  set AGENT_BARK_KEY=xxxxxxxxx                # 用环境变量设置 Bark key（优先于脚本内默认值）

原理：主程序 app.py 启动时写 temp/agent.pid，正常退出时 atexit 删除；
若发生 access violation 崩溃则 pid 文件残留。看门狗据此区分
「正常关闭」（pid 文件消失）与「崩溃」（pid 文件存在但进程已死）。
"""
import os
import sys
import time
import json
import urllib.request
import urllib.error
from pathlib import Path

# ===== 配置区 =====
BARK_KEY = os.environ.get("AGENT_BARK_KEY") or "YOUR_BARK_KEY_HERE"
PID_FILE = Path(__file__).resolve().parent.parent / "temp" / "agent.pid"
# 崩溃已通知标记：一旦对某次崩溃推送过（无论成败），写入此文件，
# 之后即便 pid 文件因异常未被删除，也绝不重复推送，避免睡觉时轰炸。
CRASH_NOTIFIED = Path(__file__).resolve().parent.parent / "temp" / "agent.crash_notified"
CHECK_INTERVAL = 30  # 秒


def push_bark(title: str, body: str) -> bool:
    """推送 Bark 通知（day.app 或自建 bark-server 通用）。"""
    payload = json.dumps(
        {"title": title, "body": body, "device_key": BARK_KEY}
    ).encode("utf-8")
    req = urllib.request.Request(
        "https://api.day.app/push",
        data=payload,
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    try:
        with urllib.request.urlopen(req, timeout=10) as resp:
            ok = resp.status == 200
            if not ok:
                try:
                    body = resp.read().decode("utf-8", "replace")
                except Exception:
                    body = ""
                print(f"[watchdog] Bark 返回非 200: {resp.status} | {body}")
            return ok
    except urllib.error.HTTPError as e:
        # day.app 对错误 key 返回 400 + JSON body（含具体原因）
        try:
            body = e.read().decode("utf-8", "replace")
        except Exception:
            body = ""
        print(f"[watchdog] Bark 返回 HTTP {e.code}: {body}")
        return False
    except Exception as e:
        print(f"[watchdog] Bark 推送失败: {e}")
        return False


def process_alive(pid: int) -> bool:
    """检测进程是否存活。os.kill(pid,0) 在 Windows 上不发信号、仅探测。
    OpenProcess 失败且 winerror==87(ERROR_INVALID_PARAMETER) 表示 pid 无效/不存在；
    其余（含 5=ACCESS_DENIED，进程存在但无权限）一律视为存活。"""
    try:
        os.kill(pid, 0)
        return True
    except ProcessLookupError:
        return False
    except OSError as e:
        if getattr(e, "winerror", None) == 87:
            return False
        return True


def check_once() -> None:
    if not PID_FILE.exists():
        # 主程序未运行 / 已正常退出 → 静默，不推送
        return
    try:
        pid = int(PID_FILE.read_text(encoding="utf-8").strip())
    except Exception:
        return
    if process_alive(pid):
        return
    # 进程消失 → 判定崩溃
    # 双保险：若已对此次崩溃通知过（标记文件存在），直接跳过，绝不重复推送
    if CRASH_NOTIFIED.exists():
        return
    ts = time.strftime("%Y-%m-%d %H:%M:%S")
    print(f"[{ts}] 检测到主程序崩溃：PID={pid} 已消失，推送 Bark 通知...")
    push_bark("客服进程已崩溃", f"PID {pid} 于 {ts} 消失，疑似 access violation 崩溃")
    # 无论推送成败，都标记已处理 + 删除 pid：
    #  - 成功：标记防重复，删除 pid 让后续轮询静默
    #  - 失败（如临时网络中断）：同样标记+删除，避免每 30s 反复重试轰炸；
    #    用户醒来看到程序已退出即为兜底，不靠重复推送弥补
    try:
        CRASH_NOTIFIED.write_text(str(int(time.time())), encoding="utf-8")
    except Exception:
        pass
    try:
        PID_FILE.unlink()
    except Exception:
        pass


def main() -> None:
    if len(sys.argv) > 1 and sys.argv[1] == "once":
        check_once()
        return
    if len(sys.argv) > 1 and sys.argv[1] == "test":
        masked = (BARK_KEY[:4] + "...") if BARK_KEY != "YOUR_BARK_KEY_HERE" else "未设置"
        print(f"[watchdog] 测试推送：key={masked} endpoint=https://api.day.app/push")
        ok = push_bark(
            "看门狗测试",
            "这是一条来自 watchdog 的测试通知。若手机收到，说明推送链路正常。",
        )
        print("结果:", "推送成功 ✓" if ok else "推送失败 ✗（见上方错误信息，检查 key 或网络）")
        return
    print(f"[watchdog] 启动，监控 {PID_FILE}，间隔 {CHECK_INTERVAL}s（Ctrl+C 退出）")
    try:
        while True:
            check_once()
            time.sleep(CHECK_INTERVAL)
    except KeyboardInterrupt:
        print("\n[watchdog] 已停止")


if __name__ == "__main__":
    main()
