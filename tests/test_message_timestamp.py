"""消息时间戳解析回归测试：毫秒/秒兼容 + 异常值回退。

背景：移植后将 timestamp 源字段改为服务端 ts（实测为秒级 10 位 epoch），
而持久化层原实现一律按毫秒 /1000 解析，导致消息时间显示为 1970。
"""

import sys
import unittest
from datetime import datetime, timezone, timedelta
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from services.message_persistence import MessagePersistenceService  # noqa: E402

TZ = timezone(timedelta(hours=8))


class ParseMessageTimestampTest(unittest.TestCase):
    def test_seconds_epoch_is_parsed(self):
        # 服务端 ts 字段：1791562961 = 2026-10-10 00:22:41 (UTC+8)
        dt = MessagePersistenceService._parse_message_timestamp("1791562961")
        self.assertEqual(dt.astimezone(TZ).strftime("%Y-%m-%d %H:%M:%S"), "2026-10-10 00:22:41")

    def test_milliseconds_epoch_is_parsed(self):
        dt = MessagePersistenceService._parse_message_timestamp("1791562961000")
        self.assertEqual(dt.astimezone(TZ).strftime("%Y-%m-%d %H:%M:%S"), "2026-10-10 00:22:41")

    def test_int_value_supported(self):
        dt = MessagePersistenceService._parse_message_timestamp(1791562961)
        self.assertEqual(dt.astimezone(TZ).strftime("%Y-%m-%d %H:%M:%S"), "2026-10-10 00:22:41")

    def test_absurd_value_falls_back_to_now(self):
        before = datetime.now(TZ)
        dt = MessagePersistenceService._parse_message_timestamp("1")
        after = datetime.now(TZ)
        self.assertGreaterEqual(dt, before)
        self.assertLessEqual(dt, after)

    def test_future_value_falls_back_to_now(self):
        far_future = int(datetime.now(TZ).timestamp()) + 400 * 86400
        before = datetime.now(TZ)
        dt = MessagePersistenceService._parse_message_timestamp(str(far_future))
        after = datetime.now(TZ)
        self.assertGreaterEqual(dt, before)
        self.assertLessEqual(dt, after)

    def test_empty_value_falls_back_to_now(self):
        dt = MessagePersistenceService._parse_message_timestamp(None)
        self.assertIsNotNone(dt)


if __name__ == "__main__":
    unittest.main()
