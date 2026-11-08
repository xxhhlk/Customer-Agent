"""AI 发送客服素材工具。

从店铺「客服专用」素材库按名称挑图片/视频发给买家。素材清单由
``material_context.load_material_catalog`` 注入 prompt，模型据此选择。

发送通道与人工素材面板保持一致：
- 图片：``send_image(uid, url)``
- 视频：``library_file/send``（传素材原片），**发送前登记自回显**，
  否则服务端 WS 回显会被当成新的人工消息重复入库

安全边界：
- 人工客服冷却期内（60s）拒绝发送，避免与人工重复打扰
- 同一素材对同一买家 60s 内不重发；同一买家 60s 内最多 2 条
- 未通过审核的素材不进目录，天然排除
"""

import asyncio
import threading
import time
from typing import Any, Dict, List, Optional, Tuple

from agno.tools import tool

from Agent.CustomerAgent.material_context import display_name, get_catalog, match_material
from utils.logger_loguru import get_logger

logger = get_logger("SendMaterialTool")

# 同一买家时间窗内最多发送条数（防模型连续刷素材）
RATE_WINDOW = 60.0
RATE_MAX = 2
# 同一素材对同一买家的去重窗口
DUP_WINDOW = 60.0

_lock = threading.Lock()
# (shop_id, buyer_uid, item_id) -> monotonic ts
_recent: Dict[Tuple[str, str, str], float] = {}
# (shop_id, buyer_uid) -> [monotonic ts]
_sent_ts: Dict[Tuple[str, str], List[float]] = {}
# (shop_id, buyer_uid) -> [wall clock ts]：供上层判断"本次 AI 处理窗口内是否已发素材"
_sent_log: Dict[Tuple[str, str], List[float]] = {}
_SENT_LOG_TTL = 300.0


def _staff_cooldown_active(buyer_uid: str) -> bool:
    """人工客服冷却期内（刚回复过）不允许 AI 再发素材"""
    try:
        from Message.handlers.staff_reply_event import staff_reply_event_manager

        return staff_reply_event_manager.get_cooldown_remaining(str(buyer_uid)) > 0
    except Exception:  # noqa: BLE001
        return False


def _reserve(shop_id: str, buyer_uid: str, item_id: str) -> Optional[str]:
    """发送前预占名额；返回错误文案表示拒绝，返回 None 表示可发。"""
    now = time.monotonic()
    item_key = (str(shop_id), str(buyer_uid), str(item_id))
    buyer_key = (str(shop_id), str(buyer_uid))
    with _lock:
        last = _recent.get(item_key)
        if last and (now - last) < DUP_WINDOW:
            return "这个素材刚刚已经发给买家了，不要重复发送"
        times = [t for t in _sent_ts.get(buyer_key, []) if (now - t) < RATE_WINDOW]
        if len(times) >= RATE_MAX:
            return (
                f"{int(RATE_WINDOW)} 秒内已经发过 {len(times)} 条素材，"
                "先用文字回复，不要继续发素材"
            )
        times.append(now)
        _sent_ts[buyer_key] = times
        _recent[item_key] = now
    return None


def _release(shop_id: str, buyer_uid: str, item_id: str) -> None:
    """发送失败时撤销预占，允许立即重试"""
    item_key = (str(shop_id), str(buyer_uid), str(item_id))
    buyer_key = (str(shop_id), str(buyer_uid))
    with _lock:
        _recent.pop(item_key, None)
        times = _sent_ts.get(buyer_key)
        if times:
            times.pop()
            if not times:
                _sent_ts.pop(buyer_key, None)


def _mark_sent(shop_id: str, buyer_uid: str) -> None:
    """记录一次成功发送（wall clock，与 ai_handler 的处理起始时间对齐）"""
    key = (str(shop_id), str(buyer_uid))
    now = time.time()
    with _lock:
        times = [t for t in _sent_log.get(key, []) if (now - t) < _SENT_LOG_TTL]
        times.append(now)
        _sent_log[key] = times[-10:]


def sent_since(shop_id: str, buyer_uid: str, since: float) -> bool:
    """since（time.time()）之后是否给该买家发过素材。

    供 ai_handler 判断"本轮只调了工具、没产出文字"，避免再补一条兜底话术。
    """
    key = (str(shop_id), str(buyer_uid))
    with _lock:
        times = list(_sent_log.get(key) or [])
    return any(t > since for t in times)


def _judge(result: Any) -> Tuple[bool, str]:
    """判定发送结果：success + result.result != fail + 无 error_code"""
    if not isinstance(result, dict) or not result.get("success"):
        return False, str(result)
    inner = result.get("result") or {}
    if inner.get("result") == "fail":
        code = inner.get("error_code")
        detail = inner.get("error") or inner.get("reason") or "未知错误"
        return False, f"{detail}（error_code={code}）" if code else detail
    if inner.get("error_code") and inner.get("error_code") != 0:
        return False, f"error_code={inner.get('error_code')} {inner.get('error', '')}"
    return True, ""


def _dispatch(shop_id: str, user_id: str, buyer_uid: str, item: Dict[str, Any]) -> Tuple[bool, str]:
    """发送素材并落库（AI 来源），成功后通知 UI 追加气泡。"""
    from Channel.pinduoduo.utils.API.material_space import MaterialSpace
    from Channel.pinduoduo.utils.API.send_message import SendMessage
    from Message.handlers.self_echo_registry import self_echo_registry

    payload = MaterialSpace.build_send_payload(item)
    ctx_type = payload["context_type"]
    url = payload["url"]
    content = payload.get("content") or url
    if not url:
        return False, "素材没有可用的下载地址"

    sender = SendMessage(str(shop_id), str(user_id))
    registered = False
    if ctx_type == "video":
        registered = self_echo_registry.register(str(shop_id), str(buyer_uid), content)
        result = sender.send_library_file(str(buyer_uid), url)
    else:
        result = sender.send_image(str(buyer_uid), url)

    ok, err = _judge(result)
    if not ok:
        if registered:
            self_echo_registry.revoke(str(shop_id), str(buyer_uid), content)
        return False, err

    try:
        from services.message_persistence import message_persistence_service

        msg_dict = message_persistence_service.save_outbound_message(
            shop_id=str(shop_id),
            user_id=str(user_id),
            buyer_uid=str(buyer_uid),
            reply_content=content,
            reply_source="ai",
            context_type=ctx_type,
            media_meta=payload.get("media_meta"),
        )
        if msg_dict:
            message_persistence_service.notify_new_message(msg_dict)
    except Exception as e:  # noqa: BLE001
        logger.warning(f"素材发送成功但持久化失败: {e}")

    return True, ""


def _send_material_sync(
    shop_id: str, user_id: str, buyer_uid: str, material_name: str
) -> str:
    """工具主体（同步）：匹配素材 → 限流 → 发送 → 返回给模型的结果文案。"""
    images, videos = get_catalog(shop_id, user_id)
    items = images + videos
    if not items:
        return "素材库暂时不可用或为空，请直接用文字回复买家"

    item, candidates = match_material(items, material_name)
    if item is None:
        if candidates:
            return (
                f"「{material_name}」匹配到多个素材：{'、'.join(candidates)}。"
                "请用完整素材名重新调用"
            )
        available = "、".join(display_name(it) for it in items[:12])
        return f"素材库中没有「{material_name}」。可用素材：{available}"

    name = display_name(item)
    item_id = str(item.get("id") or "")
    rejected = _reserve(shop_id, buyer_uid, item_id)
    if rejected:
        return rejected

    ok, err = _dispatch(shop_id, user_id, buyer_uid, item)
    if not ok:
        _release(shop_id, buyer_uid, item_id)
        logger.warning(f"AI 发送素材失败: name={name}, err={err}")
        return f"「{name}」发送失败：{err}。可以改用文字说明，或让买家稍后再试"

    kind = "视频" if item.get("file_type") == "video" else "图片"
    logger.info(f"AI 发送素材成功: {kind} {name} -> {buyer_uid}")
    _mark_sent(shop_id, buyer_uid)
    return f"已发送{kind}「{name}」给买家"


@tool(
    name="send_material",
    description=(
        "从店铺『客服专用』素材库挑一个图片或视频发给买家。"
        "买家要求看教程图、操作截图、功能演示视频，或需要用图片/视频说明操作步骤时使用。"
        "material_name 必须与素材清单里的名称完全一致，不要自己编造素材名。"
    ),
)
async def send_material(
    recipient_uid: str, material_name: str, shop_id: str, user_id: str
) -> str:
    """
    从客服素材库发送图片/视频给买家。

    Args:
        recipient_uid: 接收消息的买家 UID
        material_name: 素材名称（必须与素材清单中的名称一致）
        shop_id: 店铺 ID
        user_id: 登录账号 ID

    Returns:
        str: 发送结果说明（成功/失败原因/候选素材）
    """
    buyer = str(recipient_uid or "").strip()
    name = str(material_name or "").strip()
    shop = str(shop_id or "").strip()
    user = str(user_id or "").strip()
    if not buyer or not name or not shop or not user:
        return f"发送失败：缺少必要参数 (recipient_uid={buyer}, material_name={name})"

    if _staff_cooldown_active(buyer):
        return "人工客服刚刚回复过这个买家，本轮不要发送素材"

    try:
        return await asyncio.to_thread(_send_material_sync, shop, user, buyer, name)
    except Exception as e:  # noqa: BLE001
        logger.error(f"发送素材工具异常: {e}", exc_info=True)
        return f"发送素材异常：{e}"


__all__ = ["send_material", "sent_since", "RATE_WINDOW", "RATE_MAX", "DUP_WINDOW"]
