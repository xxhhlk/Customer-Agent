"""新订单固定文案触发判定测试。

运行：uv run python tests/test_order_notify.py
"""

import json
import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from bridge.context import Context, ContextType  # noqa: E402
from Message.handlers import order_notify  # noqa: E402


def _ctx(payload: dict) -> Context:
    return Context(
        type=ContextType.ORDER_INFO,
        content=json.dumps(payload, ensure_ascii=False),
        kwargs={},
    )


class TestOrderNotify(unittest.TestCase):
    def test_today_order_triggers(self):
        """今天下的单应触发教程。"""
        import datetime

        today = datetime.date.today()
        sn = today.strftime("%y%m%d") + "-283891515713620"
        ok, order_sn = order_notify.is_new_order(_ctx({
            "order_id": sn,
            "goods_id": 674248573770,
            "goods_name": "奥维地图高清图层历史影像等高线导航3d卫星定位路亚钓鱼手机电脑",
            "afterSalesStatus": None,
            "afterSalesType": None,
            "spec": "企业版+历史影像",
        }))
        self.assertTrue(ok)
        self.assertEqual(order_sn, sn)

    def test_yesterday_order_triggers(self):
        """昨天下的单也触发（跨天边界）。"""
        import datetime

        yesterday = datetime.date.today() - datetime.timedelta(days=1)
        sn = yesterday.strftime("%y%m%d") + "-123456789012345"
        ok, _ = order_notify.is_new_order(_ctx({
            "order_id": sn, "goods_id": 1, "goods_name": "x",
            "afterSalesStatus": None, "afterSalesType": None,
        }))
        self.assertTrue(ok)

    def test_old_order_skipped(self):
        """几天前的单不触发（那是售后咨询）。"""
        ok, _ = order_notify.is_new_order(_ctx({
            "order_id": "261011-523375767090740", "goods_id": 674248573770,
            "goods_name": "x", "afterSalesStatus": None, "afterSalesType": None,
        }))
        self.assertFalse(ok)

    def test_after_sale_order_skipped(self):
        """已申请售后的单不触发。"""
        import datetime

        today = datetime.date.today().strftime("%y%m%d")
        ok, _ = order_notify.is_new_order(_ctx({
            "order_id": f"{today}-123456789012345", "goods_id": 1,
            "goods_name": "x", "afterSalesStatus": 5, "afterSalesType": 1,
        }))
        self.assertFalse(ok)

    def test_after_sale_card_skipped(self):
        """带「售后」标题的订单卡不触发。"""
        import datetime

        today = datetime.date.today().strftime("%y%m%d")
        ok, _ = order_notify.is_new_order(_ctx({
            "order_id": f"{today}-123456789012345", "goods_id": 1,
            "goods_name": "x", "title": "请尽快处理售后问题，避免平台介入",
        }))
        self.assertFalse(ok)

    def test_non_order_context_skipped(self):
        """非订单消息不触发。"""
        ctx = Context(type=ContextType.TEXT, content="我要下单", kwargs={})
        ok, _ = order_notify.is_new_order(ctx)
        self.assertFalse(ok)

    def test_merged_with_buyer_text_not_triggered(self):
        """订单与买家文字合并时不触发固定文案（否则会吞掉买家的问题）。"""
        import datetime

        today = datetime.date.today().strftime("%y%m%d")
        payload = json.dumps({
            "order_id": f"{today}-283891515713620",
            "goods_name": "x", "spec": "y",
        }, ensure_ascii=False)
        ctx = Context(
            type=ContextType.TEXT,
            content=f"[ORDER_INFO] {payload}\n我要看历史卫星图",
            kwargs={},
        )
        ok, _ = order_notify.is_new_order(ctx)
        self.assertFalse(ok)

    def test_merged_order_only_still_triggered(self):
        """合并后内容只有订单那一行时仍触发（防抖可能重排了顺序）。"""
        import datetime

        today = datetime.date.today().strftime("%y%m%d")
        payload = json.dumps({
            "order_id": f"{today}-283891515713620",
            "goods_name": "x", "spec": "y",
        }, ensure_ascii=False)
        ctx = Context(
            type=ContextType.TEXT,
            content=f"[ORDER_INFO] {payload}",
            kwargs={},
        )
        ok, order_sn = order_notify.is_new_order(ctx)
        self.assertTrue(ok)
        self.assertEqual(order_sn, f"{today}-283891515713620")

    def test_malformed_content_skipped(self):
        """非 JSON 内容不触发。"""
        ctx = Context(type=ContextType.ORDER_INFO, content="not json", kwargs={})
        ok, _ = order_notify.is_new_order(ctx)
        self.assertFalse(ok)

    def test_tutorial_text_mentions_both_platforms(self):
        """固定文案必须同时覆盖手机与电脑的取码路径。"""
        text = order_notify.tutorial_text()
        self.assertIn("离线授权", text)
        self.assertIn("VIP管理", text)
        self.assertIn("机器码", text)

    def test_dedup_mark(self):
        """同一订单号只发一次。"""
        order_notify.mark_sent("shop1", "sn-dedup-test")
        self.assertTrue(order_notify.already_sent("shop1", "sn-dedup-test"))
        self.assertFalse(order_notify.already_sent("shop1", "sn-other"))
        self.assertFalse(order_notify.already_sent("shop2", "sn-dedup-test"))


if __name__ == "__main__":
    unittest.main(verbosity=2)
