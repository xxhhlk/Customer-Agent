"""把买家近期订单组装成 AI 上下文块。

异步回复前把当前买家的近期订单（状态 / 商品 / 金额 / 物流）拼成文本块注入
prompt，让 AI 能直接回答「我的订单到哪了」「什么时候发货」这类问题。

- 数据源：``OrderApi.list_orders``（``/latitude/order/userAllOrder``，cookies 认证）
- **商家备注不注入**：备注属店铺内部信息（可能含机器码、内部标记），
  一旦进 prompt 就有被模型说给买家的风险
- 结果按 ``(shop_id, buyer_uid)`` 缓存 :data:`ORDER_CTX_TTL` 秒，
  同一买家短时间多轮对话不重复打接口
- 任何失败都返回空串（静默降级），不影响正常回复
"""

import time
from typing import Any, Dict, List, Tuple

from utils.logger_loguru import get_logger

logger = get_logger("OrderContext")

# 结果缓存 TTL（秒）
ORDER_CTX_TTL = 60.0
# 注入给模型的订单条数上限
ORDER_CTX_LIMIT = 5
# 商品名截断长度（商品名普遍很长，整段拼进 prompt 意义不大）
_GOODS_NAME_LIMIT = 28
# 单个订单最多列出的商品件数
_GOODS_MAX_ITEMS = 3

# (shop_id, buyer_uid) -> (monotonic_ts, block)
_CACHE: Dict[Tuple[str, str], Tuple[float, str]] = {}


def clear_cache() -> None:
    """清空订单上下文缓存"""
    _CACHE.clear()


def load_order_block(
    shop_id: str,
    account_user_id: str,
    buyer_uid: str,
    limit: int = ORDER_CTX_LIMIT,
) -> str:
    """读取买家近期订单并格式化为 prompt 块；无订单或失败返回空串。

    同步 HTTP + 数据库访问（OrderApi 构造会读账号 cookies），调用方须用
    ``asyncio.to_thread`` 包装，勿在事件循环内直接调用。
    """
    shop = str(shop_id or "").strip()
    buyer = str(buyer_uid or "").strip()
    if not shop or not buyer:
        return ""

    key = (shop, buyer)
    now = time.monotonic()
    cached = _CACHE.get(key)
    if cached and (now - cached[0]) < ORDER_CTX_TTL:
        return cached[1]

    block = ""
    try:
        from Channel.pinduoduo.utils.API.order import OrderApi

        api = OrderApi(shop, str(account_user_id or ""))
        data = api.list_orders(buyer, page=1, page_size=max(1, int(limit)))
        if data.get("success"):
            block = _format_block(data.get("orders") or [], int(data.get("total") or 0))
        else:
            logger.warning(f"加载买家订单信息失败: {data.get('error_msg')}")
    except Exception as e:  # noqa: BLE001
        logger.warning(f"加载买家订单信息异常: {e}")

    # 失败同样缓存空结果：cookies 失效等场景短时间重试无意义，60s 后再试
    _CACHE[key] = (now, block)
    return block


def _format_block(orders: List[Dict[str, Any]], total: int) -> str:
    """订单列表 → 供模型阅读的文本块；无订单返回空串。"""
    if not orders:
        return ""

    lines: List[str] = []
    for idx, o in enumerate(orders, 1):
        parts = [
            f"{idx}) 订单号 {o.get('order_sn') or '-'}",
            f"状态 {o.get('status_str') or '未知'}",
        ]
        goods_desc = _format_goods(o.get("goods") or [])
        if goods_desc:
            parts.append(f"商品 {goods_desc}")
        amount = _format_amount(o.get("amount"))
        if amount:
            parts.append(f"金额 {amount}")
        if o.get("order_time"):
            parts.append(f"下单 {_short_time(o['order_time'])}")
        if o.get("shipping_time"):
            parts.append(f"发货 {_short_time(o['shipping_time'])}")
        elif o.get("promise_shipping_time"):
            parts.append(f"承诺发货 {_short_time(o['promise_shipping_time'])}")
        if o.get("tracking_number"):
            parts.append(f"物流单号 {o['tracking_number']}")
        lines.append("，".join(parts))

    head = "[买家订单信息]（店铺系统数据，回答订单/物流问题时以此为准，不要编造未提供的订单信息）"
    if total > len(orders):
        head += f"（共 {total} 个订单，以下为最近 {len(orders)} 个）"
    return head + "\n" + "\n".join(lines) + "\n"


def _format_goods(goods: List[Dict[str, Any]]) -> str:
    """商品列表 → ``名称（规格）×数量``；超出上限标注总数。"""
    items: List[str] = []
    for g in goods[:_GOODS_MAX_ITEMS]:
        name = str(g.get("name") or "").strip()
        if not name:
            continue
        if len(name) > _GOODS_NAME_LIMIT:
            name = name[: _GOODS_NAME_LIMIT - 1] + "…"
        spec = str(g.get("spec") or "").strip()
        try:
            number = int(g.get("number") or 1)
        except (TypeError, ValueError):
            number = 1
        items.append(f"{name}（{spec}）×{number}" if spec else f"{name}×{number}")
    if not items:
        return ""
    suffix = f" 等 {len(goods)} 件商品" if len(goods) > _GOODS_MAX_ITEMS else ""
    return "、".join(items) + suffix


def _format_amount(fen: Any) -> str:
    """金额（分）→ ``¥19.90``；无效值返回空串。"""
    try:
        return f"¥{int(fen) / 100:.2f}"
    except (TypeError, ValueError):
        return ""


def _short_time(text: Any) -> str:
    """``2026-10-11 22:56:46`` → ``2026-10-11 22:56``（订单类问题秒级无意义）"""
    s = str(text or "")
    return s[:16] if len(s) >= 16 else s


__all__ = ["load_order_block", "clear_cache", "ORDER_CTX_TTL", "ORDER_CTX_LIMIT"]
