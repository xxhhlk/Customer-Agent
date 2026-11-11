"""机器码 / 设备编号判定测试。

运行：uv run python tests/test_machine_code.py
"""

import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from Agent.CustomerAgent.machine_code import (  # noqa: E402
    detect_machine_code_violation,
    is_device_id,
    is_device_id_only,
    is_machine_code,
    is_order_sn,
)


class TestMachineCodeFormat(unittest.TestCase):
    def test_valid_machine_code(self):
        """真实机器码：8位HEX - 8位HEX，可带空格 / 三段。"""
        self.assertTrue(is_machine_code("50378B2F - D563EA05"))
        self.assertTrue(is_machine_code("4296292C-5258A423"))
        self.assertTrue(is_machine_code("808ACA82 - DE714A55 - 6E9FE0FD"))
        self.assertTrue(is_machine_code("62576BA7 - 4AE09378"))
        self.assertTrue(is_machine_code("4296292C-5258A423".lower()))

    def test_device_id_is_not_machine_code(self):
        """纯数字是设备编号，不是机器码。"""
        self.assertFalse(is_machine_code("113317104"))
        self.assertFalse(is_machine_code("14313890"))
        self.assertTrue(is_device_id("113317104"))
        self.assertTrue(is_device_id("14313890"))

    def test_order_sn_not_confused(self):
        """订单号不能被当成设备编号。"""
        self.assertTrue(is_order_sn("261111-283891515713620"))
        self.assertFalse(is_device_id("261111-283891515713620"))

    def test_device_id_only(self):
        """整条消息只有一个编号时才判定为「纯编号消息」。"""
        self.assertTrue(is_device_id_only("113317104"))
        self.assertTrue(is_device_id_only("  113317104  "))
        self.assertFalse(is_device_id_only("113317104 这个是机器编号"))
        self.assertFalse(is_device_id_only("我的订单号 261111-283891515713620"))
        self.assertFalse(is_device_id_only(""))


class TestViolationDetection(unittest.TestCase):
    def test_accept_device_id_as_machine_code(self):
        """11-11 生产事故原文：AI 把机器编号当机器码收下了。"""
        reply = "收到 机器码 113317104 已经记下 我这边安排授权 稍等一下 / 授权好后把登录教程发您"
        self.assertEqual(detect_machine_code_violation(reply), ["113317104"])

    def test_accept_device_id_short(self):
        reply = "收到 机器码 14313890 已经记下 我这边安排授权"
        self.assertEqual(detect_machine_code_violation(reply), ["14313890"])

    def test_legit_machine_code_not_flagged(self):
        """合法机器码不应被误判。"""
        for reply in (
            "收到 机器码 50378B2F - D563EA05 已经记下 我这边安排授权",
            "收到 机器码 4296292C-5258A423 已经记下",
            "收到 机器码 808ACA82 - DE714A55 - 6E9FE0FD 已经记下",
        ):
            self.assertEqual(detect_machine_code_violation(reply), [], reply)

    def test_all_digit_machine_code_not_flagged(self):
        """全数字但构成合法机器码形态的不该被误判。"""
        self.assertEqual(
            detect_machine_code_violation("收到 机器码 12345678 - 87654321 已经记下"), []
        )

    def test_order_sn_in_reply_not_flagged(self):
        reply = "您的订单 261111-283891515713620 已收到，机器码请按教程发我"
        self.assertEqual(detect_machine_code_violation(reply), [])

    def test_no_number_not_flagged(self):
        reply = "手机更多→VIP管理→离线授权 点上面的复制，粘贴发我"
        self.assertEqual(detect_machine_code_violation(reply), [])

    def test_asking_for_code_not_flagged(self):
        """索要机器码的回复不应被误判。"""
        reply = "麻烦把机器码复制粘贴发我一下，格式类似 50378B2F - D563EA05"
        self.assertEqual(detect_machine_code_violation(reply), [])


if __name__ == "__main__":
    unittest.main(verbosity=2)
