"""客服素材目录：向 AI 暴露「客服专用」素材库清单。

- 图片空间：仅「客服专用」文件夹（按名解析 dir_id，见 material_space）
- 视频空间：客服专用视频库（``dir_id=-1``）

两块用途：
1. :func:`load_material_catalog` 把素材名注入 prompt，模型据此知道能发什么；
2. :func:`get_catalog` 给 ``send_material`` 工具按名匹配素材用。

缓存按 ``(shop_id, account_user_id)`` 隔离（素材库随账号不同），
成功 5 分钟、失败 60 秒；任何失败均返回空清单，不影响正常回复。
"""

import time
from typing import Any, Dict, List, Tuple

from utils.logger_loguru import get_logger

logger = get_logger("MaterialContext")

# 成功结果缓存（秒）：素材库变动不频繁
CATALOG_TTL = 300.0
# 失败结果缓存（秒）：接口抖动时别每条消息都重试
CATALOG_FAIL_TTL = 60.0
# 注入清单的条数上限（防止素材多了以后 prompt 爆炸）
MAX_IMAGES = 40
MAX_VIDEOS = 20

# (shop_id, account_user_id) -> (monotonic_ts, images, videos, ok)
_CATALOG_CACHE: Dict[Tuple[str, str], Tuple[float, List[Dict[str, Any]], List[Dict[str, Any]], bool]] = {}


def clear_cache() -> None:
    """清空素材目录缓存"""
    _CATALOG_CACHE.clear()


def get_catalog(
    shop_id: str,
    account_user_id: str,
    force: bool = False,
) -> Tuple[List[Dict[str, Any]], List[Dict[str, Any]]]:
    """返回 ``(图片素材, 视频素材)``，均为已归一化且审核通过的素材项。

    同步 HTTP 访问（含 cookies 读取），调用方需用 ``asyncio.to_thread`` 包装。
    """
    shop = str(shop_id or "").strip()
    user = str(account_user_id or "").strip()
    if not shop or not user:
        return [], []

    key = (shop, user)
    now = time.monotonic()
    cached = _CATALOG_CACHE.get(key)
    if not force and cached:
        ttl = CATALOG_TTL if cached[3] else CATALOG_FAIL_TTL
        if (now - cached[0]) < ttl:
            return cached[1], cached[2]

    images: List[Dict[str, Any]] = []
    videos: List[Dict[str, Any]] = []
    ok = False
    try:
        from Channel.pinduoduo.utils.API.material_space import MaterialSpace

        ms = MaterialSpace(shop, user)
        ok = True

        dir_id = ms.get_cs_dir_id()
        if dir_id is None:
            logger.warning(f"图片空间未找到客服专用文件夹 (shop={shop}, user={user})")
        else:
            data = ms.list_files(
                space=MaterialSpace.SPACE_IMAGE,
                page=1, page_size=MAX_IMAGES, dir_id=dir_id,
            )
            if data.get("success"):
                images = [it for it in data["items"] if MaterialSpace.is_sendable(it)]
            else:
                ok = False
                logger.warning(f"拉取客服图片素材失败: {data.get('error_msg')}")

        data_v = ms.list_files(
            space=MaterialSpace.SPACE_VIDEO,
            page=1, page_size=MAX_VIDEOS,
        )
        if data_v.get("success"):
            videos = [it for it in data_v["items"] if MaterialSpace.is_sendable(it)]
        else:
            ok = False
            logger.warning(f"拉取客服视频素材失败: {data_v.get('error_msg')}")
    except Exception as e:  # noqa: BLE001
        ok = False
        logger.warning(f"拉取客服素材目录异常: {e}")

    _CATALOG_CACHE[key] = (now, images, videos, ok)
    return images, videos


def load_material_catalog(shop_id: str, account_user_id: str) -> str:
    """素材目录 → 供模型阅读的 prompt 块；无素材或失败返回空串。

    同步 HTTP 访问，调用方需用 ``asyncio.to_thread`` 包装。
    """
    images, videos = get_catalog(shop_id, account_user_id)
    if not images and not videos:
        return ""

    lines = [
        "[客服素材库]（买家需要看图/看视频说明时，可调用 send_material 发送；"
        "material_name 必须与下列名称完全一致）"
    ]
    if images:
        lines.append("图片：" + _join_names(images))
    if videos:
        lines.append("视频：" + _join_names(videos))
    return "\n".join(lines) + "\n"


def _join_names(items: List[Dict[str, Any]], limit: int = 20) -> str:
    """素材名拼接；超出上限时截断并标注总数。"""
    names: List[str] = []
    for it in items[:limit]:
        name = str(it.get("name") or "").strip()
        if name:
            names.append(name)
    text = "、".join(names)
    if len(items) > limit:
        text += f" …（共 {len(items)} 个）"
    return text


def display_name(item: Dict[str, Any]) -> str:
    """素材展示名（去扩展名，用于匹配与回话）"""
    name = str(item.get("name") or "").strip()
    ext = str(item.get("extension") or "").strip()
    if ext and name.lower().endswith("." + ext.lower()):
        name = name[: -(len(ext) + 1)]
    return name or str(item.get("id") or "")


__all__ = [
    "get_catalog",
    "load_material_catalog",
    "display_name",
    "clear_cache",
    "CATALOG_TTL",
    "MAX_IMAGES",
    "MAX_VIDEOS",
]
