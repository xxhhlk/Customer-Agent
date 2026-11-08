"""人工客服图片 → 一句话描述（后台线程）

WS 收到人工发送的图片时，先以 ``[图片]`` 占位写进 ``staff_message_cache``，
本模块在后台调视觉模型生成描述，完成后回填，AI 下一轮上下文即
``[图片] 电脑端输入机器码的位置截图``。

约束：
- 不阻塞 WS 消息处理（daemon 线程，单进程最多 2 个在飞）
- 同一 URL 的描述只在内存里算一次（避免多人转发同一张图反复烧 token）
- 任何失败静默降级为 ``[图片]``，不影响消息处理
"""

import asyncio
import threading
from typing import Dict

from utils.logger_loguru import get_logger

logger = get_logger("StaffMediaCaption")

# 同时在飞的描述任务上限（视觉模型调用较重，人工发图频率低但别打满）
MAX_INFLIGHT = 2
# URL → 描述 的进程内缓存条数上限
DESC_CACHE_LIMIT = 200

_INSTRUCTION = (
    "这是人工客服发给买家的图片。用一句不超过25字的中文描述画面主体和关键文字，"
    "只描述、不评价、不加引号。"
)

_lock = threading.Lock()
_inflight = 0
_desc_cache: Dict[str, str] = {}


def schedule(buyer_uid: str, token: str, url: str) -> None:
    """登记一次后台描述任务；命中内存缓存时立即回填。"""
    clean_url = str(url or "").strip()
    if not (buyer_uid and token) or not clean_url.startswith("http"):
        return

    with _lock:
        cached = _desc_cache.get(clean_url)
    if cached:
        _apply(buyer_uid, token, cached)
        return

    global _inflight
    with _lock:
        if _inflight >= MAX_INFLIGHT:
            logger.debug("人工图片描述任务已满，本次跳过（保留占位）")
            return
        _inflight += 1

    threading.Thread(
        target=_worker,
        args=(buyer_uid, token, clean_url),
        name="staff-media-caption",
        daemon=True,
    ).start()


def _worker(buyer_uid: str, token: str, url: str) -> None:
    global _inflight
    try:
        from Agent.CustomerAgent.vision import describe_image

        desc = asyncio.run(describe_image(url, _INSTRUCTION))
        if desc:
            with _lock:
                if len(_desc_cache) >= DESC_CACHE_LIMIT:
                    _desc_cache.clear()
                _desc_cache[url] = desc
            _apply(buyer_uid, token, desc)
            logger.info(f"人工图片描述完成: {desc}")
    except Exception as e:  # noqa: BLE001
        logger.warning(f"人工图片描述失败: {type(e).__name__} {e}")
    finally:
        with _lock:
            _inflight -= 1


def _apply(buyer_uid: str, token: str, desc: str) -> None:
    try:
        from Message.handlers.staff_message_cache import staff_message_cache

        staff_message_cache.update_description(buyer_uid, token, desc)
    except Exception as e:  # noqa: BLE001
        logger.warning(f"回填人工图片描述失败: {type(e).__name__} {e}")


def clear_desc_cache() -> None:
    """清空 URL → 描述缓存（测试/素材大改时用）"""
    with _lock:
        _desc_cache.clear()


__all__ = ["schedule", "clear_desc_cache", "MAX_INFLIGHT", "DESC_CACHE_LIMIT"]
