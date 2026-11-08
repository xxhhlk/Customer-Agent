"""AI 查看客服素材内容（按需兜底）。

素材清单里已经带了「名称 —— 一句描述」（``material_context`` 后台预热生成），
多数情况模型不需要调用本工具。只有在描述不足以判断素材是否符合买家需求时，
才调用它看实际画面。

返回的是**文字描述**，不是图片本身——agno 的工具结果只能是字符串，
且本工具不会向买家发送任何东西（发送请用 send_material）。
"""

import threading
from typing import Any, Dict

from agno.tools import tool

from Agent.CustomerAgent.material_context import (
    display_name,
    get_catalog,
    get_description,
    match_material,
    save_description,
)
from utils.logger_loguru import get_logger

logger = get_logger("ViewMaterialTool")

# 进程内最多查看次数（防模型反复看图烧 token）
MAX_VIEWS_PER_PROCESS = 20

_VIEW_INSTRUCTION = (
    "这是客服准备发给买家的素材图。用一句不超过25字的中文描述画面主体和关键文字，"
    "只描述、不评价、不加引号。"
)

_view_lock = threading.Lock()
_views = 0


def _take_view_slot() -> bool:
    """占用一次查看额度；超限返回 False"""
    global _views
    with _view_lock:
        if _views >= MAX_VIEWS_PER_PROCESS:
            return False
        _views += 1
        return True


def _preview_url(item: Dict[str, Any]) -> str:
    """图片取原图，视频取封面（不下载视频抽帧）"""
    if str(item.get("file_type") or "") == "video":
        return str(item.get("cover_url") or "")
    return str(item.get("url") or item.get("transcode_url") or "")


async def _describe(item: Dict[str, Any]) -> str:
    from Agent.CustomerAgent.vision import describe_image

    url = _preview_url(item)
    if not url.startswith("http"):
        return ""
    return await describe_image(url, _VIEW_INSTRUCTION)


@tool(
    name="view_material",
    description=(
        "查看店铺『客服专用』素材库里某个图片/视频的实际内容，返回一句话文字描述。"
        "仅当你根据素材名和清单里的描述仍无法判断该素材是否适合买家时使用；"
        "能判断时直接调用 send_material 发送，不要先查看。本工具不会给买家发消息。"
    ),
)
async def view_material(material_name: str, shop_id: str, user_id: str) -> str:
    """
    查看客服素材的实际内容（返回文字描述）。

    Args:
        material_name: 素材名称（必须与素材清单中的名称一致）
        shop_id: 店铺 ID
        user_id: 登录账号 ID

    Returns:
        str: 素材内容描述，或失败/未找到的说明
    """
    name = str(material_name or "").strip()
    shop = str(shop_id or "").strip()
    user = str(user_id or "").strip()
    if not name or not shop or not user:
        return f"查看失败：缺少必要参数 (material_name={name}, shop_id={shop}, user_id={user})"

    try:
        images, videos = get_catalog(shop, user)
        items = images + videos
        if not items:
            return "素材库暂时不可用或为空"

        item, candidates = match_material(items, name)
        if item is None:
            if candidates:
                return (
                    f"「{name}」匹配到多个素材：{'、'.join(candidates)}。"
                    "请用完整素材名重新调用"
                )
            available = "、".join(display_name(it) for it in items[:12])
            return f"素材库中没有「{name}」。可用素材：{available}"

        shown = display_name(item)
        cached = get_description(item)
        if cached:
            return f"「{shown}」的内容：{cached}"

        if not _take_view_slot():
            return "本次会话查看素材的次数已用完，请根据素材名判断，或直接用文字回复买家"

        desc = await _describe(item)
        if not desc:
            return f"「{shown}」查看失败（视觉模型未返回结果），请按素材名判断或改用文字回复"

        save_description(item, desc)
        logger.info(f"AI 查看素材: {shown} -> {desc}")
        return f"「{shown}」的内容：{desc}"
    except Exception as e:  # noqa: BLE001
        logger.error(f"查看素材工具异常: {type(e).__name__} {e}", exc_info=True)
        return f"查看素材异常：{type(e).__name__} {e}"


__all__ = ["view_material", "MAX_VIEWS_PER_PROCESS"]
