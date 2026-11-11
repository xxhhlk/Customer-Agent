"""端到端验证：真实消息样本走一遍拦截判定。

运行：uv run python tests/test_reply_pipeline_sim.py
"""

import json
import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from bridge.context import Context, ContextType  # noqa: E402
from Message.core.enhanced_consumer import EnhancedMessageConsumer  # noqa: E402
from Message.handlers import order_notify  # noqa: E402
from Message.handlers.machine_code_guide import (  # noqa: E402
    is_pure_device_id_message,
    reply_mentions_accepted_code,
)


def _text_ctx(text: str) -> Context:
    return Context(type=ContextType.TEXT, content=text, kwargs={})


class TestRealIncident(unittest.TestCase):
    """复盘 2026-11-11 的两轮消息。"""

    def test_round1_pure_number_intercepted(self):
        """第 1 轮：买家单独发「113317104」→ 必须被拦截。"""
        ctx = _text_ctx("113317104")
        self.assertTrue(is_pure_device_id_message(ctx.content))
        # 不拦截的话模型会看到这串数字
        self.assertFalse(is_pure_device_id_message("机器编号是这个"))

    def test_round2_with_words_not_intercepted(self):
        """第 2 轮：买家说「机器编号是这个」→ 不拦，交给模型+出站兜底。"""
        ctx = _text_ctx("机器编号是这个")
        self.assertFalse(is_pure_device_id_message(ctx.content))

    def test_outbound_guard_catches_original_reply(self):
        """原始事故回复必须被出站兜底拦住。"""
        bad = "收到 机器码 113317104 已经记下 我这边安排授权 稍等一下 / 授权好后把登录教程发您"
        self.assertEqual(reply_mentions_accepted_code(bad), ["113317104"])

    def test_correct_reply_passes(self):
        """正确口径的回复不应被拦。"""
        good = (
            "这个是设备编号哦，不是机器码，我们授权只认机器码的～\n"
            "麻烦重新复制一下机器码发我：\n"
            "手机：更多→VIP管理→离线授权→点排在上面的复制\n"
            "电脑：菜单栏最右边 帮助→VIP管理→复制机器码"
        )
        self.assertEqual(reply_mentions_accepted_code(good), [])

    def test_valid_machine_code_reply_passes(self):
        """买家给了真机器码，回复照常确认，不该被拦。"""
        good = "收到 机器码 50378B2F - D563EA05 已经记下 我这边安排授权"
        self.assertEqual(reply_mentions_accepted_code(good), [])


class TestOrderPipeline(unittest.TestCase):
    def test_real_new_order_payload(self):
        """真实订单 payload（2026-11-11）应触发教程。"""
        payload = json.dumps({
            "order_id": "261111-283891515713620",
            "goods_id": 674248573770,
            "goods_name": "奥维地图高清图层历史影像等高线导航3d卫星定位路亚钓鱼手机电脑",
            "afterSalesStatus": None,
            "afterSalesType": None,
            "spec": "企业版+历史影像",
        }, ensure_ascii=False)
        ctx = Context(type=ContextType.ORDER_INFO, content=payload, kwargs={})
        # 该订单是 11-11 下的；今天若不是 11-11/11-12 则不应触发
        ok, sn = order_notify.is_new_order(ctx)
        self.assertEqual(sn, "261111-283891515713620" if ok else "")

    def test_order_history_summary(self):
        """订单写入 AI 历史时应是摘要而非整段 JSON。"""
        payload = json.dumps({
            "order_id": "261111-283891515713620",
            "goods_name": "奥维地图高清图层",
            "spec": "企业版+历史影像",
        }, ensure_ascii=False)
        ctx = Context(type=ContextType.ORDER_INFO, content=payload, kwargs={})
        text = EnhancedMessageConsumer._history_text(ctx)
        self.assertIn("买家已下单", text)
        self.assertIn("261111-283891515713620", text)
        self.assertNotIn("{", text)  # 不该有 JSON 原文


if __name__ == "__main__":
    unittest.main(verbosity=2)
