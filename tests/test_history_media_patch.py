"""H4 回归测试：历史图片重放上限（本地 mock LLM，不产生真实 API 调用）。

场景：分 4 轮各发 1 张图（共 4 条带图历史消息），第 5 轮发纯文本追问。
期望：补丁生效时，第 5 轮请求中携带的历史图片 = 2 张（仅保留最近 2 条）；
      补丁失效时为 4 张。
"""

import asyncio
import base64
import json
import sys
import threading
import unittest
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

# 1x1 透明 PNG（base64），避免外网图片依赖
PNG_B64 = (
    "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mNkYAAAAAYAAjCB0C8AAAAASUVORK5CYII="
)

CAPTURED = []


class _MockLLMHandler(BaseHTTPRequestHandler):
    def do_POST(self):
        n = int(self.headers.get("Content-Length", 0))
        try:
            body = json.loads(self.rfile.read(n))
        except Exception:
            body = {}
        CAPTURED.append(body)
        resp = {
            "id": "mock-1",
            "object": "chat.completion",
            "created": 0,
            "model": "mock",
            "choices": [
                {
                    "index": 0,
                    "message": {"role": "assistant", "content": "ok"},
                    "finish_reason": "stop",
                }
            ],
            "usage": {"prompt_tokens": 1, "completion_tokens": 1, "total_tokens": 2},
        }
        data = json.dumps(resp).encode()
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def log_message(self, *args):
        pass


def _count_image_blocks(payload):
    cnt = 0
    for m in payload.get("messages", []):
        content = m.get("content")
        if isinstance(content, list):
            cnt += sum(
                1 for b in content
                if isinstance(b, dict) and b.get("type") == "image_url"
            )
    return cnt


class HistoryMediaReplayTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.server = HTTPServer(("127.0.0.1", 0), _MockLLMHandler)
        cls.port = cls.server.server_address[1]
        threading.Thread(target=cls.server.serve_forever, daemon=True).start()

        # 应用历史图片补丁（与生产同一函数）
        from Agent.CustomerAgent.agent import _patch_agno_history_media
        _patch_agno_history_media()

    @classmethod
    def tearDownClass(cls):
        cls.server.shutdown()
        cls.server.server_close()

    def test_only_recent_two_history_images_are_replayed(self):
        from agno.agent import Agent
        from agno.db.sqlite import SqliteDb
        from agno.media import Image
        from agno.models.openai import OpenAILike
        import tempfile

        CAPTURED.clear()
        db_path = tempfile.mktemp(suffix="_agno_test.db")
        try:
            agent = Agent(
                model=OpenAILike(
                    id="mock",
                    api_key="dummy",
                    base_url=f"http://127.0.0.1:{self.port}/v1",
                ),
                db=SqliteDb(db_file=db_path),
                add_history_to_context=True,
                num_history_runs=8,
            )

            async def _run():
                url = "data:image/png;base64," + PNG_B64
                for i in range(4):
                    await agent.arun(
                        f"图{i + 1}", images=[Image(url=url)],
                        session_id="s1", user_id="u1",
                    )
                await agent.arun("一共几张图？", session_id="s1", user_id="u1")

            asyncio.run(_run())

            self.assertGreaterEqual(len(CAPTURED), 5, f"mock 未捕获到 5 轮请求: {len(CAPTURED)}")
            last = CAPTURED[-1]
            cnt = _count_image_blocks(last)
            # 补丁生效：4 条带图历史只保留最近 2 条
            self.assertEqual(cnt, 2, f"第5轮携带历史图片数={cnt}，期望 2（保留最近2条）")
        finally:
            try:
                import os
                if os.path.exists(db_path):
                    os.remove(db_path)
            except Exception:
                pass


if __name__ == "__main__":
    unittest.main()
