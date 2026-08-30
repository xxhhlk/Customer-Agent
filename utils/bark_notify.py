"""
Bark 通知工具

从 config.json 的 bark 段读取配置（密钥在配置里填，支持 day.app 或自建 bark-server）：
    "bark": {
        "key": "xxxxxxxxxxxx",              # 设备 key（留空则不发通知）
        "base_url": "https://api.day.app"   # 可选，默认 day.app
    }

发送在 daemon 线程异步执行，绝不阻塞调用方。
"""

import json
import os
import threading
import urllib.error
import urllib.request

from utils.logger_loguru import get_logger

logger = get_logger("BarkNotify")

# 发送超时（秒），daemon 线程内阻塞，不影响主流程
_TIMEOUT = 10


def _get_bark_config() -> dict:
    """读取 bark 配置，任何异常都回退为不启用"""
    try:
        from config import config as _config
        base_url = str(_config.get("bark.base_url", "https://api.day.app") or "").strip()
        key = str(_config.get("bark.key", "") or "").strip()
        if not key:
            key = os.environ.get("AGENT_BARK_KEY", "").strip()  # 兼容 watchdog 的环境变量配置
        return {"base_url": base_url.rstrip("/"), "key": key}
    except Exception:
        return {"base_url": "https://api.day.app", "key": ""}


def _do_push(title: str, body: str) -> bool:
    """同步推送（内部用，勿直接调用，会阻塞）"""
    cfg = _get_bark_config()
    key = cfg["key"]
    if not key:
        logger.debug("Bark key 未配置，跳过通知")
        return False

    payload = json.dumps({"title": title, "body": body, "device_key": key}).encode("utf-8")
    url = f"{cfg['base_url']}/push"
    req = urllib.request.Request(
        url,
        data=payload,
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    try:
        with urllib.request.urlopen(req, timeout=_TIMEOUT) as resp:
            if resp.status != 200:
                logger.warning(f"Bark 返回非 200: {resp.status}")
                return False
            return True
    except urllib.error.HTTPError as e:
        try:
            resp_body = e.read().decode("utf-8", "replace")
        except Exception:
            resp_body = ""
        logger.warning(f"Bark 返回 HTTP {e.code}: {resp_body}")
        return False
    except Exception as e:
        logger.warning(f"Bark 推送失败: {e}")
        return False


def push_bark(title: str, body: str) -> None:
    """异步推送 Bark 通知（daemon 线程，不阻塞调用方）。key 未配置时静默跳过。"""
    if not _get_bark_config()["key"]:
        return

    def _worker():
        try:
            _do_push(title, body)
        except Exception as e:  # 兜底，绝不抛到调用方
            logger.error(f"Bark 推送异常: {e}")

    threading.Thread(target=_worker, daemon=True, name="bark-notify").start()
