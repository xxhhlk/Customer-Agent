"""拼多多订单（买家订单列表 / 订单备注）API 封装。

接口来源与线上实测记录见 `docs/order-api-research-2026-10-12.md`。

实测要点：
- 买家订单列表 ``POST /latitude/order/userAllOrder``
  body ``{uid, pageNo, pageSize, showHistory}``；cookies 认证，无需 anti-content。
  商家备注（``note``）直接带在订单对象里，展示备注无需额外请求。
- 备注读 ``POST /pizza/order/note/query``  body ``{orderSn, source: 1}``
- 备注写 ``POST /pizza/order/note/update`` body ``{orderSn, source: 1, remark}``
  无备注时用 ``POST /pizza/order/note/add``（同一 body）。
- ★ ``source`` 必须为 1（update 严格校验，其它取值返回 2000000 入参有误）。
- ★ 同一订单两次写备注间隔需 > 1 秒（服务端限流），**调用方负责节流**
  （见 :data:`NOTE_WRITE_MIN_INTERVAL`）。
- 写入为全量覆盖；query 读回存在秒级延迟，写入成功以后端返回为准。

字段形态注意（实测）：
- ``orderTime / payTime / shippingTime`` 为 **Unix 秒**（int），非字符串；
- ``orderGoodsList`` 单商品订单为 **dict**、多商品为 list，归一化统一成 list。
"""

from datetime import datetime
from typing import Any, Dict, List, Optional

from ..base_request import BaseRequest

_BASE_URL = "https://mms.pinduoduo.com"

# /pizza/order/note/* 的 source 取值：实测必须为 1
NOTE_SOURCE = 1

# 备注写入最小间隔（秒）：服务端要求同一订单两次写 > 1s，留 200ms 余量
NOTE_WRITE_MIN_INTERVAL = 1.2

# 与商家聊天工作台一致的 referer，降低被风控概率
_REQUEST_HEADERS = {
    "accept": "application/json, text/plain, */*",
    "content-type": "application/json;charset=UTF-8",
    "origin": "https://mms.pinduoduo.com",
    "referer": "https://mms.pinduoduo.com/chat-merchant/index.html",
}


def _fmt_time(value: Any) -> str:
    """时间字段 → ``"YYYY-MM-DD HH:MM:SS"``。

    兼容 Unix 秒 / 毫秒（int, float）与字符串（ISO 或已格式化文本）；
    无法解析时原样返回，不抛异常。
    """
    if value is None or value == "":
        return ""
    try:
        if isinstance(value, str):
            stripped = value.strip()
            if not stripped:
                return ""
            # 纯数字字符串按时间戳处理，其余（ISO 等）截断后返回
            if stripped.isdigit():
                value = int(stripped)
            else:
                return stripped[:19].replace("T", " ")
        ts = float(value)
        if ts <= 0:
            return ""
        if ts > 1e12:  # 毫秒
            ts /= 1000.0
        return datetime.fromtimestamp(ts).strftime("%Y-%m-%d %H:%M:%S")
    except (TypeError, ValueError, OSError, OverflowError):
        return str(value)


class OrderApi(BaseRequest):
    """订单查询与备注读写接口。

    用法::

        api = OrderApi(shop_id, user_id)
        data = api.list_orders(buyer_uid, page=1, page_size=20)
        for o in data["orders"]:
            ...  # 已归一化
        api.save_note(order_sn, "新备注", has_note=bool(o["note"]))

    说明：本类实例化会从数据库读取账号 cookies（同步 DB 操作），
    请在后台线程中创建/使用，不要在 UI 主线程直接调用。
    """

    # ------------------------------------------------------------------ #
    # 订单列表
    # ------------------------------------------------------------------ #

    def list_orders(
        self,
        buyer_uid: str,
        page: int = 1,
        page_size: int = 20,
        show_history: bool = True,
    ) -> Dict[str, Any]:
        """拉取指定买家的订单列表（含备注字段）。

        Args:
            buyer_uid: 买家 uid
            page: 页码，从 1 开始
            page_size: 每页数量
            show_history: 是否包含历史订单（含已完结）

        Returns:
            ``{"success": bool, "orders": [归一化订单], "total": int,
               "error_msg": str}``
        """
        uid = str(buyer_uid or "").strip()
        if not uid:
            return {"success": False, "orders": [], "total": 0, "error_msg": "买家 uid 为空"}

        result = self.post(
            f"{_BASE_URL}/latitude/order/userAllOrder",
            json_data={
                "uid": uid,
                "pageNo": max(1, int(page)),
                "pageSize": max(1, int(page_size)),
                "showHistory": bool(show_history),
            },
            headers=_REQUEST_HEADERS,
        )

        if not result or not result.get("success"):
            err = self._extract_error(result)
            self.logger.error(f"获取买家订单列表失败 (uid={uid}, page={page}): {err}")
            return {"success": False, "orders": [], "total": 0, "error_msg": err}

        inner = result.get("result") or {}
        orders: List[Dict[str, Any]] = []
        for raw in inner.get("orders") or []:
            norm = self._normalize_order(raw)
            if norm:
                orders.append(norm)

        return {
            "success": True,
            "orders": orders,
            "total": int(inner.get("total") or len(orders)),
            "error_msg": "",
        }

    # ------------------------------------------------------------------ #
    # 备注读写
    # ------------------------------------------------------------------ #

    def get_note(self, order_sn: str) -> Dict[str, Any]:
        """读取订单商家备注。

        Returns:
            ``{"success": bool, "note": str, "error_msg": str}``
            （无备注时 ``note`` 为空字符串）
        """
        sn = str(order_sn or "").strip()
        if not sn:
            return {"success": False, "note": "", "error_msg": "订单号为空"}

        result = self.post(
            f"{_BASE_URL}/pizza/order/note/query",
            json_data={"orderSn": sn, "source": NOTE_SOURCE},
            headers=_REQUEST_HEADERS,
        )
        if not result or not result.get("success"):
            err = self._extract_error(result)
            self.logger.error(f"读取订单备注失败 (orderSn={sn}): {err}")
            return {"success": False, "note": "", "error_msg": err}

        return {"success": True, "note": str(result.get("result") or ""), "error_msg": ""}

    def save_note(
        self,
        order_sn: str,
        remark: str,
        has_note: Optional[bool] = None,
    ) -> Dict[str, Any]:
        """**覆盖式**写入订单商家备注。

        Args:
            order_sn: 订单号
            remark: 新备注全文（覆盖旧备注；空字符串表示清空尝试）
            has_note: 当前是否已有备注；``None`` 时先 query 判断。
                调用方已知（如列表带出的 ``note``）时传入可省一次请求。

        Returns:
            ``{"success": bool, "error_msg": str}``

        注意：同订单两次写间隔需 > 1 秒（服务端限流），调用方负责节流。
        """
        sn = str(order_sn or "").strip()
        if not sn:
            return {"success": False, "error_msg": "订单号为空"}
        text = str(remark if remark is not None else "")

        if has_note is None:
            q = self.get_note(sn)
            if not q.get("success"):
                return {"success": False, "error_msg": f"读取当前备注失败：{q.get('error_msg')}"}
            has_note = bool((q.get("note") or "").strip())

        path = "update" if has_note else "add"
        result = self._write_note(sn, text, path)
        if result and result.get("success"):
            return {"success": True, "error_msg": ""}

        err = self._extract_error(result)
        # add 失败（如服务端判定已有备注）时兜底重试 update 一次
        if path == "add":
            self.logger.warning(f"note/add 失败，回退 note/update 重试 (orderSn={sn}): {err}")
            result2 = self._write_note(sn, text, "update")
            if result2 and result2.get("success"):
                return {"success": True, "error_msg": ""}
            err = self._extract_error(result2) or err

        self.logger.error(f"写入订单备注失败 (orderSn={sn}, path={path}): {err}")
        return {"success": False, "error_msg": err}

    def _write_note(self, order_sn: str, remark: str, path: str):
        """执行一次备注写入请求（path 为 add / update）"""
        return self.post(
            f"{_BASE_URL}/pizza/order/note/{path}",
            json_data={"orderSn": order_sn, "source": NOTE_SOURCE, "remark": remark},
            headers=_REQUEST_HEADERS,
        )

    # ------------------------------------------------------------------ #
    # 归一化 / 工具
    # ------------------------------------------------------------------ #

    @staticmethod
    def _normalize_order(raw: Any) -> Optional[Dict[str, Any]]:
        """把接口原始订单整理成 UI 友好的结构。

        保留：order_sn / status_str / amount（分）/ order_time / pay_time /
        shipping_time / receive_time / promise_shipping_time /
        tracking_number / note / goods[]
        """
        if not isinstance(raw, dict):
            return None

        goods_raw = raw.get("orderGoodsList")
        goods: List[Dict[str, Any]] = []
        if isinstance(goods_raw, dict):
            goods = [OrderApi._normalize_goods(goods_raw)]
        elif isinstance(goods_raw, list):
            goods = [
                OrderApi._normalize_goods(g)
                for g in goods_raw
                if isinstance(g, dict)
            ]

        return {
            "order_sn": str(raw.get("orderSn") or ""),
            "status_str": str(raw.get("orderStatusStr") or ""),
            "amount": raw.get("orderAmount"),  # 单位：分
            "order_time": _fmt_time(raw.get("orderTime")),
            "pay_time": _fmt_time(raw.get("payTime")),
            "shipping_time": _fmt_time(raw.get("shippingTime")),
            "receive_time": _fmt_time(raw.get("receiveTime")),
            "promise_shipping_time": _fmt_time(raw.get("promiseShippingTime")),
            "tracking_number": str(raw.get("trackingNumber") or ""),
            "note": str(raw.get("note") or ""),
            "goods": goods,
        }

    @staticmethod
    def _normalize_goods(g: Dict[str, Any]) -> Dict[str, Any]:
        """单件商品：name / price（分）/ number / thumb / spec"""
        try:
            number = int(g.get("goodsNumber") or 1)
        except (TypeError, ValueError):
            number = 1
        return {
            "name": str(g.get("goodsName") or ""),
            "price": g.get("goodsPrice"),  # 单位：分
            "number": number,
            "thumb": str(g.get("thumbUrl") or ""),
            "spec": str(g.get("spec") or ""),
        }

    @staticmethod
    def _extract_error(res: Any) -> str:
        """从响应里提取可读错误信息（兼容多套字段命名）"""
        if not isinstance(res, dict):
            return "请求失败（无响应）"
        if res.get("success"):
            return ""
        msg = res.get("error_msg") or res.get("errorMsg") or res.get("message")
        if not msg and isinstance(res.get("result"), str):
            msg = res["result"]
        code = res.get("error_code") or res.get("errorCode")
        if code and msg:
            return f"[{code}] {msg}"
        return str(msg or code or "未知错误")


__all__ = ["OrderApi", "NOTE_SOURCE", "NOTE_WRITE_MIN_INTERVAL"]
