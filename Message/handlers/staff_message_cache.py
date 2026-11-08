"""
人工客服消息缓存
按买家UID缓存客服消息（文本 / 图片 / 视频），供AI回复时作为上下文注入

- 文本：原样缓存
- 图片 / 视频：先以占位形式入缓存（``[图片]``），视觉模型描述好之后回填成
  ``[图片] 电脑端机器码截图``（见 ``staff_media_caption``），AI 下一轮就能"看到"内容
- 本地（本客户端）发送素材时直接带上素材名与已缓存的描述

线程安全：WS 线程写入 / 后台补描述 / AI 线程读取，全部走同一把锁。
"""

import threading
import time
import uuid
from typing import Any, Dict, List, Optional, Tuple

from utils.logger_loguru import get_logger

logger = get_logger(__name__)

_KIND_CN = {"image": "图片", "video": "视频"}


class StaffMessageCache:
    """缓存人工客服消息，按买家UID隔离

    当人工客服回复买家后，消息会被缓存。下次同一买家触发AI回复时，
    缓存的客服消息会带时间戳拼接到AI的input中作为上下文。
    """

    def __init__(self, max_messages_per_buyer: int = 20, ttl_seconds: int = 3600):
        """
        Args:
            max_messages_per_buyer: 每个买家最多缓存的消息条数
            ttl_seconds: 缓存过期时间（秒），默认1小时
        """
        # buyer_uid -> [{ts, kind, text, url, name, desc, token}]
        self._cache: Dict[str, List[Dict[str, Any]]] = {}
        self._max_messages = max_messages_per_buyer
        self._ttl = ttl_seconds
        self._lock = threading.RLock()

    # ------------------------------------------------------------ 写入

    def add_message(self, buyer_uid: str, content: str) -> None:
        """添加客服文本消息到缓存"""
        text = str(content or "")
        if not buyer_uid or not text.strip():
            return
        self._append(buyer_uid, {"kind": "text", "text": text})

    def add_media(
        self,
        buyer_uid: str,
        kind: str,
        url: str = "",
        name: str = "",
        desc: str = "",
    ) -> str:
        """添加客服发送的图片/视频，返回 token（供后续回填描述）。

        Args:
            kind: ``image`` / ``video``
            url: 图片地址（用于后台生成描述，可为空）
            name: 素材名（本地发送素材时已知）
            desc: 已有的内容描述（素材库预热过的直接带上）
        """
        kind_value = kind if kind in _KIND_CN else "image"
        clean_url = str(url or "").strip()
        clean_name = str(name or "").strip()
        clean_desc = str(desc or "").strip()
        token = uuid.uuid4().hex[:12]
        # 即使 url/name/desc 全空也入缓存：只少告诉 AI「客服发了一张图」，
        # 好过完全没痕迹（转发历史图片拿不到名称，就属于这种情况）
        if not buyer_uid:
            return token
        self._append(buyer_uid, {
            "kind": kind_value,
            "url": clean_url,
            "name": clean_name,
            "desc": clean_desc,
            "token": token,
        })
        return token

    def update_description(self, buyer_uid: str, token: str, desc: str) -> bool:
        """回填某条媒体消息的内容描述；返回是否命中"""
        text = str(desc or "").strip()
        if not buyer_uid or not token or not text:
            return False
        with self._lock:
            for entry in self._cache.get(buyer_uid, []):
                if entry.get("token") == token:
                    entry["desc"] = text
                    logger.debug(f"回填客服图片描述: buyer_uid={buyer_uid}, {text[:30]}")
                    return True
        return False

    def _append(self, buyer_uid: str, entry: Dict[str, Any]) -> None:
        now = time.time()
        with self._lock:
            items = [e for e in self._cache.get(buyer_uid, []) if now - e["ts"] < self._ttl]
            entry["ts"] = now
            items.append(entry)
            if len(items) > self._max_messages:
                items = items[-self._max_messages:]
            self._cache[buyer_uid] = items
        logger.debug(f"缓存客服消息: buyer_uid={buyer_uid}, 内容={_render_entry(entry)[:30]}...")

    # ------------------------------------------------------------ 读取

    def get_messages(self, buyer_uid: str) -> List[Tuple[str, str]]:
        """获取买家的客服消息（自动清理过期消息）

        Returns:
            [(time_str, content)] 时间格式为 HH:MM
        """
        with self._lock:
            if buyer_uid not in self._cache:
                return []

            now = time.time()
            items = [e for e in self._cache[buyer_uid] if now - e["ts"] < self._ttl]
            if not items:
                self._cache.pop(buyer_uid, None)
                return []
            self._cache[buyer_uid] = items
            snapshot = [(e["ts"], _render_entry(e)) for e in items]

        return [
            (time.strftime("%H:%M", time.localtime(ts)), text)
            for ts, text in snapshot
        ]

    def clear(self, buyer_uid: Optional[str] = None) -> None:
        """清除缓存

        Args:
            buyer_uid: 指定买家UID则清除该买家的缓存，None则清除全部
        """
        with self._lock:
            if buyer_uid is None:
                self._cache.clear()
            else:
                self._cache.pop(buyer_uid, None)


def _render_entry(entry: Dict[str, Any]) -> str:
    """缓存条目 → 注入 AI 的一行文本"""
    kind = str(entry.get("kind") or "text")
    if kind == "text":
        return str(entry.get("text") or "")

    label = f"[{_KIND_CN.get(kind, '媒体')}]"
    name = str(entry.get("name") or "").strip()
    desc = str(entry.get("desc") or "").strip()
    if name and desc:
        return f"{label} {name} —— {desc}"
    if name or desc:
        return f"{label} {name or desc}"
    return label


# 全局单例
staff_message_cache = StaffMessageCache()
