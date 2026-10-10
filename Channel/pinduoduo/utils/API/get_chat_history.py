import time
from ..base_request import BaseRequest
from typing import Dict, Any, List, Optional


class GetChatHistory(BaseRequest):
    """拼多多会话列表与历史聊天记录拉取

    接口说明：
    - ``latest_conversations``：拉取最近会话列表，返回每个买家的最近一条消息与用户信息。
    - ``chat/list``：拉取指定会话的历史消息，按时间倒序分页，``has_more`` 表示是否还有更早的消息。
    """

    LATEST_CONVERSATIONS_URL = "https://mms.pinduoduo.com/plateau/chat/latest_conversations"
    CHAT_LIST_URL = "https://mms.pinduoduo.com/plateau/chat/list"

    # 翻页间隔（秒），避免请求过于密集触发风控
    PAGE_INTERVAL = 0.5

    def __init__(self, shop_id: str, user_id: str, channel_name: str = "pinduoduo"):
        super().__init__(shop_id, user_id, channel_name)

    def _anti_content(self) -> str:
        """风控签名，取不到时返回空串，交由服务端裁决"""
        value = self.cookies.get("anti_content") or self.cookies.get("anti-content")
        return str(value) if value else ""

    def _chat_headers(self) -> Dict[str, str]:
        """聊天接口请求头，与发送消息口径保持一致"""
        return {
            "accept": "application/json, text/plain, */*",
            "anti-content": self._anti_content(),
            "content-type": "application/json;charset=UTF-8",
            "origin": "https://mms.pinduoduo.com",
            "referer": "https://mms.pinduoduo.com/chat-merchant/index.html",
        }

    def fetch_conversations(self, page: int = 1, size: int = 100) -> Dict[str, Any]:
        """拉取一页最近会话列表

        Args:
            page: 页码，从 1 开始
            size: 每页条数

        Returns:
            ``{"success": bool, "conversations": [...], "has_more": bool}``，
            失败时 ``success=False`` 并附 ``error_msg``。
        """
        data = {
            "data": {
                "cmd": "latest_conversations",
                "request_id": self.generate_request_id(),
                "version": 2,
                "need_unreply_time": True,
                "page": page,
                "size": size,
                "anti_content": self._anti_content(),
            },
            "client": 1,
        }

        result = self.post(
            self.LATEST_CONVERSATIONS_URL, json_data=data, headers=self._chat_headers()
        )
        if not result or result.get("success") is not True:
            error_msg = (result or {}).get("error_msg", "未知错误")
            self.logger.error(f"获取会话列表失败: {error_msg}")
            return {"success": False, "error_msg": error_msg, "conversations": [], "has_more": False}

        inner = result.get("result") or {}
        raw_list = inner.get("conversations") or []
        conversations: List[Dict[str, Any]] = []
        for raw in raw_list:
            if not isinstance(raw, dict):
                continue
            buyer_uid = self._resolve_buyer_uid(raw)
            if not buyer_uid:
                continue
            user_info = raw.get("user_info") or {}
            conversations.append({
                "buyer_uid": buyer_uid,
                "nickname": user_info.get("nickname") or raw.get("nickname") or "",
                "last_msg_id": raw.get("msg_id"),
                "last_ts": self._to_int(raw.get("ts")),
            })

        return {
            "success": True,
            "conversations": conversations,
            "has_more": bool(inner.get("has_more")),
        }

    def fetch_messages(
        self, buyer_uid: str, start_msg_id: Optional[str] = None, size: int = 50
    ) -> Optional[Dict[str, Any]]:
        """拉取单页历史消息

        Args:
            buyer_uid: 买家 UID
            start_msg_id: 游标，传入上一页最旧一条的 msg_id 以往前翻，首页为 None
            size: 每页条数

        Returns:
            ``{"messages": [...], "has_more": bool}``，请求失败返回 None。
        """
        data = {
            "data": {
                "cmd": "list",
                "request_id": self.generate_request_id(),
                "list": {
                    "with": {"role": "user", "id": str(buyer_uid)},
                    "start_msg_id": start_msg_id,
                    "start_index": 0,
                    "size": size,
                },
                "notUpdateUnreplyTs": True,
                "anti_content": self._anti_content(),
            }
        }

        result = self.post(self.CHAT_LIST_URL, json_data=data, headers=self._chat_headers())
        if not result or result.get("success") is not True:
            error_msg = (result or {}).get("error_msg", "未知错误")
            self.logger.error(f"获取聊天记录失败: buyer={buyer_uid}, {error_msg}")
            return None

        inner = result.get("result") or {}
        return {
            "messages": inner.get("messages") or [],
            "has_more": bool(inner.get("has_more")),
        }

    def fetch_history(
        self, buyer_uid: str, max_pages: int = 3, page_size: int = 50
    ) -> List[Dict[str, Any]]:
        """循环翻页拉取单个会话的历史消息

        接口按时间倒序返回单页，以本页最旧一条的 msg_id 作为下一页游标往前翻，
        直至 ``has_more=false`` 或达到页数上限；按 msg_id 去重后按时间正序返回。

        Args:
            buyer_uid: 买家 UID
            max_pages: 最多翻页数
            page_size: 每页条数

        Returns:
            按时间正序（旧 → 新）排列的原始消息列表。
        """
        collected: List[Dict[str, Any]] = []
        seen: set = set()
        start_msg_id: Optional[str] = None

        for page_no in range(max_pages):
            if page_no > 0:
                time.sleep(self.PAGE_INTERVAL)

            page = self.fetch_messages(buyer_uid, start_msg_id=start_msg_id, size=page_size)
            if page is None:
                # 请求失败即停止翻页，已拉取的部分照常返回
                break

            batch = page.get("messages") or []
            if not batch:
                break

            oldest_msg_id = ""
            for raw in batch:
                if not isinstance(raw, dict):
                    continue
                msg_id = str(raw.get("msg_id") or "")
                if msg_id:
                    oldest_msg_id = msg_id
                if not msg_id or msg_id in seen:
                    continue
                seen.add(msg_id)
                collected.append(raw)

            if not page.get("has_more") or not oldest_msg_id:
                break
            start_msg_id = oldest_msg_id

        collected.sort(key=lambda item: self._to_int(item.get("ts")) or 0)
        return collected

    @staticmethod
    def _resolve_buyer_uid(raw: Dict[str, Any]) -> str:
        """取会话对端买家 UID（from / to 中角色为 user 的一方）"""
        for key in ("from", "to"):
            party = raw.get(key)
            if isinstance(party, dict) and party.get("role") == "user":
                uid = party.get("uid")
                if uid:
                    return str(uid)
        user_info = raw.get("user_info") or {}
        uid = user_info.get("uid")
        return str(uid) if uid else ""

    @staticmethod
    def _to_int(value: Any) -> Optional[int]:
        """安全转为 int，失败返回 None"""
        try:
            return int(value)
        except (TypeError, ValueError):
            return None
