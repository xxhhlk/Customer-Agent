"""客服素材目录：向 AI 暴露「客服专用」素材库清单。

- 图片空间：仅「客服专用」文件夹（按名解析 dir_id，见 material_space）
- 视频空间：客服专用视频库（``dir_id=-1``）

三块用途：
1. :func:`load_material_catalog` 把「素材名 —— 内容描述」注入 prompt，模型据此挑素材；
2. :func:`get_catalog` / :func:`match_material` 给 ``send_material`` / ``view_material`` 用；
3. :func:`warm_captions` 后台补描述（视觉模型生成，落盘缓存，不阻塞回复）。

缓存按 ``(shop_id, account_user_id)`` 隔离（素材库随账号不同），
成功 5 分钟、失败 60 秒；任何失败均返回空清单，不影响正常回复。
"""

import json
import re
import threading
import time
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

from utils.logger_loguru import get_logger

logger = get_logger("MaterialContext")

# 成功结果缓存（秒）：素材库变动不频繁
CATALOG_TTL = 300.0
# 失败结果缓存（秒）：接口抖动时别每条消息都重试
CATALOG_FAIL_TTL = 60.0
# 注入清单的条数上限（防止素材多了以后 prompt 爆炸）
MAX_IMAGES = 40
MAX_VIDEOS = 20
# 单次后台预热最多处理多少条（首次接入素材库时为全部 14 条）
CAPTION_WARM_LIMIT = 16

# 素材描述缓存文件（按 素材ID + create_time 命中，素材被替换后自动失效）
CAPTION_FILE = Path(__file__).resolve().parents[2] / "temp" / "material_captions.json"

_CAPTION_INSTRUCTION = (
    "这是客服准备发给买家的素材图。用一句不超过25字的中文描述画面主体和关键文字，"
    "只描述、不评价、不加引号。"
)

_EXT_RE = re.compile(r"\.(png|jpe?g|gif|webp|bmp|mp4|mov|avi|mkv)$", re.IGNORECASE)

# (shop_id, account_user_id) -> (monotonic_ts, images, videos, ok)
_CATALOG_CACHE: Dict[Tuple[str, str], Tuple[float, List[Dict[str, Any]], List[Dict[str, Any]], bool]] = {}

# 描述缓存（内存 + 落盘）
_caption_lock = threading.Lock()
_captions: Optional[Dict[str, Dict[str, Any]]] = None
_warm_running = False


# ---------------------------------------------------------------- 目录

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


# ---------------------------------------------------------------- 名称匹配

def display_name(item: Dict[str, Any]) -> str:
    """素材展示名（去扩展名，用于匹配与回话）"""
    name = str(item.get("name") or "").strip()
    ext = str(item.get("extension") or "").strip()
    if ext and name.lower().endswith("." + ext.lower()):
        name = name[: -(len(ext) + 1)]
    return name or str(item.get("id") or "")


def normalize_name(name: Any) -> str:
    """匹配用归一化：去扩展名、去空格、小写"""
    text = _EXT_RE.sub("", str(name or "").strip())
    return text.replace(" ", "").lower()


def match_material(
    items: List[Dict[str, Any]], name: str
) -> Tuple[Optional[Dict[str, Any]], List[str]]:
    """按名称匹配素材。

    Returns:
        ``(命中素材 | None, 候选名列表)``
        - 精确命中或唯一包含匹配 → 命中
        - 多个包含匹配 → 不命中，返回候选让模型改名重试
    """
    query = normalize_name(name)
    if not query or not items:
        return None, []

    exact = [it for it in items if normalize_name(display_name(it)) == query]
    pool = exact or [
        it for it in items
        if query in normalize_name(display_name(it))
        or normalize_name(display_name(it)) in query
    ]
    if len(pool) == 1:
        return pool[0], []
    if len(pool) > 1:
        return None, [display_name(it) for it in pool[:8]]
    return None, []


# ---------------------------------------------------------------- 素材描述

def caption_key(item: Dict[str, Any]) -> str:
    """描述缓存键：素材 id + 创建时间（素材被替换后自动失效）"""
    return f"{item.get('id')}:{item.get('create_time')}"


def _load_caption_file() -> Dict[str, Dict[str, Any]]:
    try:
        if CAPTION_FILE.exists():
            with open(CAPTION_FILE, "r", encoding="utf-8") as f:
                data = json.load(f)
            if isinstance(data, dict):
                return {k: v for k, v in data.items() if isinstance(v, dict)}
    except Exception as e:  # noqa: BLE001
        logger.warning(f"读取素材描述缓存失败: {type(e).__name__} {e}")
    return {}


def _caption_store() -> Dict[str, Dict[str, Any]]:
    global _captions
    if _captions is None:
        _captions = _load_caption_file()
    return _captions


def _save_caption_file(store: Dict[str, Dict[str, Any]]) -> None:
    try:
        CAPTION_FILE.parent.mkdir(parents=True, exist_ok=True)
        tmp = CAPTION_FILE.with_suffix(".tmp")
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(store, f, ensure_ascii=False, indent=1)
        tmp.replace(CAPTION_FILE)
    except Exception as e:  # noqa: BLE001
        logger.warning(f"写入素材描述缓存失败: {type(e).__name__} {e}")


def get_description(item: Dict[str, Any]) -> str:
    """取素材描述；没有则返回空串"""
    with _caption_lock:
        entry = _caption_store().get(caption_key(item)) or {}
    return str(entry.get("desc") or "").strip()


def save_description(item: Dict[str, Any], desc: str) -> None:
    """写入素材描述（内存 + 落盘）"""
    text = str(desc or "").strip()
    if not text:
        return
    with _caption_lock:
        store = _caption_store()
        store[caption_key(item)] = {
            "name": display_name(item),
            "desc": text,
            "at": int(time.time()),
        }
        snapshot = dict(store)
    _save_caption_file(snapshot)


def _preview_url(item: Dict[str, Any]) -> str:
    """用于看图的地址：图片取原图，视频取封面（视频本身不抽帧）"""
    if str(item.get("file_type") or "") == "video":
        return str(item.get("cover_url") or "")
    return str(item.get("url") or item.get("transcode_url") or "")


def clear_caption_cache() -> None:
    """清空内存与落盘描述缓存（素材大改后手动调用）"""
    global _captions
    with _caption_lock:
        _captions = {}
    _save_caption_file({})


def warm_captions(images: List[Dict[str, Any]], videos: List[Dict[str, Any]]) -> None:
    """后台补齐缺失的素材描述（不阻塞回复；同一时间只跑一个预热线程）"""
    global _warm_running
    pending = [it for it in (images + videos) if not get_description(it)]
    pending = [it for it in pending if _preview_url(it).startswith("http")]
    if not pending:
        return
    with _caption_lock:
        if _warm_running:
            return
        _warm_running = True

    todo = pending[:CAPTION_WARM_LIMIT]
    threading.Thread(
        target=_warm_worker, args=(todo,), name="material-caption", daemon=True
    ).start()


def _warm_worker(items: List[Dict[str, Any]]) -> None:
    global _warm_running
    try:
        import asyncio

        from Agent.CustomerAgent.vision import describe_images

        urls = [_preview_url(it) for it in items]
        descs = asyncio.run(
            describe_images(urls, concurrency=3, instruction=_CAPTION_INSTRUCTION)
        )
        filled = 0
        for item, desc in zip(items, descs):
            if desc:
                save_description(item, desc)
                filled += 1
        logger.info(f"素材描述预热完成: {filled}/{len(items)} 条")
    except Exception as e:  # noqa: BLE001
        logger.warning(f"素材描述预热失败: {type(e).__name__} {e}")
    finally:
        with _caption_lock:
            _warm_running = False


# ---------------------------------------------------------------- 注入块

def load_material_catalog(shop_id: str, account_user_id: str) -> str:
    """素材目录 → 供模型阅读的 prompt 块；无素材或失败返回空串。

    同步 HTTP 访问，调用方需用 ``asyncio.to_thread`` 包装。
    缺失的描述在后台补（首次调用后，下一条回复起带描述）。
    """
    images, videos = get_catalog(shop_id, account_user_id)
    if not images and not videos:
        return ""

    try:
        warm_captions(images, videos)
    except Exception as e:  # noqa: BLE001
        logger.warning(f"启动素材描述预热失败: {type(e).__name__} {e}")

    lines = [
        "[客服素材库]（买家需要看图/看视频说明时，调用 send_material 发送，"
        "material_name 必须与名称完全一致；破折号后是素材内容，"
        "看不出来时可调用 view_material 查看实际画面）"
    ]
    if images:
        lines.append("图片：" + _join_items(images))
    if videos:
        lines.append("视频：" + _join_items(videos))
    return "\n".join(lines) + "\n"


def _join_items(items: List[Dict[str, Any]], limit: int = 20) -> str:
    """素材名（带描述）拼接；超出上限时截断并标注总数。"""
    parts: List[str] = []
    for it in items[:limit]:
        name = display_name(it)
        if not name:
            continue
        desc = get_description(it)
        parts.append(f"{name} —— {desc}" if desc else name)
    text = "；".join(parts)
    if len(items) > limit:
        text += f"；…（共 {len(items)} 个）"
    return text


__all__ = [
    "get_catalog",
    "load_material_catalog",
    "display_name",
    "normalize_name",
    "match_material",
    "get_description",
    "save_description",
    "warm_captions",
    "clear_caption_cache",
    "clear_cache",
    "CATALOG_TTL",
    "MAX_IMAGES",
    "MAX_VIDEOS",
]
