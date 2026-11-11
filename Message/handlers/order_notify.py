"""新订单自动发送的固定文案（获取机器码教程）。

背景：订单消息（``ContextType.ORDER_INFO``）进队列后走 AI，AI 自己写
「已下单成功…教程…」。但 AI 的输出不稳定——同一句教程每次措辞不同，
有时还漏发。2026-10-31 前的 31 单里有 9 单从未发过机器码教程。

本模块提供确定性的文案与触发判定：

- 文案取知识库《下单之后发送-获取机器码开通授权》原文，
  与人工客服实际发送的内容一致（2026-11-11 14:40 staff 消息同源）
- 只对**新单**触发：订单号前 6 位是下单日期（YYMMDD），需为今天或昨天
- 售后单不发（``afterSalesStatus`` 非空 / 订单卡带「售后」标题）
- 同一订单号只发一次（进程内去重 + 落库去重）
"""

import json
import re
import time
from typing import Any, Dict, Optional, Tuple

from bridge.context import Context
from utils.logger_loguru import get_logger

logger = get_logger("OrderNotify")

# 知识库《下单之后发送-获取机器码开通授权》原文。
# 与 2026-11-11 14:40 人工客服实际发出的内容一致，保持店铺口径统一。
ORDER_TUTORIAL_TEXT = (
    "高清图源需要授权使用：\n"
    "注意，电脑和手机的都写了，认真看消息\n"
    "如果要看历史影像：用旧版才行\n"
    "提前说下\n"
    "\n"
    "【手机用】一共3步务必做完！！\n"
    "在奥维点击右下更多→VIP管理\n"
    "在下面找按钮“离线授权”点进去，找不到往下翻\n"
    "最后点排在上面的复制，粘贴发我就行\n"
    "\n"
    "【电脑用】\n"
    "在奥维上面一排菜单最右边 帮助→VIP管理\n"
    "点右侧复制机器码（如果是设备编号，截图按钮给我）\n"
    "先粘贴发我，然后再发“电脑用”"
)

# 出站回复来源标记（气泡上显示为「订单通知」）
REPLY_SOURCE = "order_notify"

# 订单号：6 位日期(YYMMDD) - 15 位数字
_ORDER_SN_RE = re.compile(r"^(\d{6})-(\d{15})$")

# 合并后的订单消息在文本里留下的标记（见 DebounceProcessorAdapter._context_line）
_ORDER_MARK = "[ORDER_INFO]"
_ORDER_JSON_RE = re.compile(r"\[ORDER_INFO\]\s*(\{.*?\})", re.S)

# 同一订单号的去重窗口（秒）。10-14 那单 2 分钟内推了 3 次同一订单号。
_DEDUP_TTL = 24 * 3600

# (shop_id, order_sn) -> 首次发送时间戳
_sent_orders: Dict[Tuple[str, str], float] = {}


def _parse_order_content(content: Optional[str]) -> Optional[Dict[str, Any]]:
    """解析订单消息的 JSON 内容。

    兼容两种形态：
    1. 订单消息单独到达时，content 就是整段 JSON
    2. 与买家文字合并后，content 里是 ``[ORDER_INFO] {json}`` 形式
       （防抖合并会把非文本类型压成一行带标记的文本）
    """
    if not content:
        return None
    text = str(content).strip()
    if not text:
        return None

    if text.startswith("{"):
        try:
            data = json.loads(text)
        except (json.JSONDecodeError, ValueError):
            return None
        return data if isinstance(data, dict) else None

    m = _ORDER_JSON_RE.search(text)
    if not m:
        return None
    try:
        data = json.loads(m.group(1))
    except (json.JSONDecodeError, ValueError):
        return None
    return data if isinstance(data, dict) else None


def _order_date_is_recent(order_sn: str) -> bool:
    """订单号前 6 位是否为今天或昨天。

    订单号前 6 位是下单日期（YYMMDD）。只处理今天/昨天的单——
    几天前的单号对应的是售后咨询，发下单教程是错的
    （提示词技能3第8条已有同样口径）。
    """
    m = _ORDER_SN_RE.match(order_sn or "")
    if not m:
        return False
    date_part = m.group(1)
    try:
        yy = int(date_part[0:2])
        mm = int(date_part[2:4])
        dd = int(date_part[4:6])
        year = 2000 + yy
    except (ValueError, IndexError):
        return False
    try:
        import datetime

        today = datetime.date.today()
        order_date = datetime.date(year, mm, dd)
    except ValueError:
        return False
    delta = (today - order_date).days
    return 0 <= delta <= 1


def is_new_order(context: Context) -> Tuple[bool, str]:
    """判断该订单消息是否应触发下单教程。

    订单消息与买家文字合并后 context.type 会变成 TEXT，content 里是
    ``[ORDER_INFO] {json}`` 加买家原话。此时**不能**只发固定文案——
    买家的问题会被吞掉，所以只有「整条消息就是订单本身」才触发。

    Returns:
        ``(是否触发, 订单号)``。不触发时订单号为空串。
    """
    content = str(context.content or "").strip()
    if not content:
        return False, ""

    # 只处理买家侧消息。商家侧订单卡（type=8，content 带 title）走 CONTEXT_ONLY
    # 不会进队列，这里再挡一道，避免把客服自己的订单卡片当成新单。
    kwargs = getattr(context, "kwargs", None)
    from_user = str(getattr(kwargs, "from_user", "") or "") if kwargs else ""
    if from_user and from_user != "user":
        return False, ""

    if context.type.name == "ORDER_INFO":
        data = _parse_order_content(content)
    else:
        # 合并形态：整条内容必须只有订单那一行，别无他物
        m = _ORDER_JSON_RE.search(content)
        if not m:
            return False, ""
        if content.strip() != m.group(0).strip():
            return False, ""  # 合并了买家文字，交给 AI 处理
        data = _parse_order_content(content)
    if not data:
        return False, ""

    order_sn = str(data.get("order_id") or "").strip()
    if not order_sn:
        return False, ""

    # 售后单不发：afterSalesStatus 非空表示已申请售后
    if data.get("afterSalesStatus") not in (None, "", 0):
        logger.info(f"订单 {order_sn} 已申请售后，跳过下单教程")
        return False, ""

    # 订单卡带「售后」标题的也不是新单
    title = str(data.get("title") or "")
    if "售后" in title:
        logger.info(f"订单 {order_sn} 是售后卡片，跳过下单教程")
        return False, ""

    if not _order_date_is_recent(order_sn):
        logger.info(f"订单 {order_sn} 不是今天/昨天的单，跳过下单教程")
        return False, ""

    return True, order_sn


def mark_sent(shop_id: str, order_sn: str) -> None:
    """标记该订单已发过教程。"""
    _sent_orders[(str(shop_id), str(order_sn))] = time.time()
    # 顺手清理过期项，防止长期运行内存膨胀
    cutoff = time.time() - _DEDUP_TTL
    stale = [k for k, v in _sent_orders.items() if v < cutoff]
    for k in stale:
        _sent_orders.pop(k, None)


def already_sent(shop_id: str, order_sn: str) -> bool:
    """该订单是否已发过教程。"""
    ts = _sent_orders.get((str(shop_id), str(order_sn)))
    if ts is None:
        return False
    if time.time() - ts > _DEDUP_TTL:
        _sent_orders.pop((str(shop_id), str(order_sn)), None)
        return False
    return True


def tutorial_text() -> str:
    """下单教程固定文案。"""
    return ORDER_TUTORIAL_TEXT


__all__ = [
    "ORDER_TUTORIAL_TEXT",
    "is_new_order",
    "mark_sent",
    "already_sent",
    "tutorial_text",
]
