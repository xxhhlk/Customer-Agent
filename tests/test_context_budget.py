"""端到端验证：历史注入按 token 预算截断 + 落盘剥离历史快照 + 摘要注入。

用本地 mock LLM（不产生真实 API 调用），复用 test_history_media_patch.py 的
服务端模板。运行时走真实的 agno Agent + SqliteDb，验证补丁在真实 arun 路径上
生效，而不是只测纯函数。

运行：uv run python tests/test_context_budget.py -v
"""

import asyncio
import json
import os
import sqlite3
import sys
import tempfile
import threading
import unittest
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

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
                    "message": {"role": "assistant", "content": "好的，已收到。"},
                    "finish_reason": "stop",
                }
            ],
            "usage": {"prompt_tokens": 1, "completion_tokens": 1, "total_tokens": 2},
        }
        data = json.dumps(resp, ensure_ascii=False).encode()
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def log_message(self, *args):
        pass


def _load_runs(raw):
    """解析 agno_sessions.runs 列。

    agno 这里可能是双重编码（JSON 字符串里再套一层 JSON），解到 list 为止。
    """
    data = raw
    for _ in range(3):
        if isinstance(data, str):
            data = json.loads(data)
        else:
            break
    return data or []


def _payload_text_tokens(payload):
    """估算一次请求 payload 里文本部分的 token（忽略图片块，测试不涉及）。"""
    from Agent.CustomerAgent.context_budget import estimate_text_tokens

    total = 0
    for m in payload.get("messages", []):
        content = m.get("content")
        if isinstance(content, str):
            total += estimate_text_tokens(content)
        elif isinstance(content, list):
            for block in content:
                if isinstance(block, dict) and block.get("type") == "text":
                    total += estimate_text_tokens(block.get("text") or "")
    return total


class ContextBudgetTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.server = HTTPServer(("127.0.0.1", 0), _MockLLMHandler)
        cls.port = cls.server.server_address[1]
        threading.Thread(target=cls.server.serve_forever, daemon=True).start()

        # 固定窗口值，避免测试结果随用户 config.json 漂移（只改内存，不落盘）
        from config import config

        cls._orig_cw = config.get("llm.context_window", 128000)
        config.update({"llm": {"context_window": 500000}}, save=False)

        # 应用生产同一份补丁（DB 走 to_thread + run 落盘裁剪 + token 预算截断）
        from Agent.CustomerAgent.agent import (
            _patch_agno_async_db,
            _patch_agno_history_media,
        )

        _patch_agno_async_db()
        _patch_agno_history_media()

    @classmethod
    def tearDownClass(cls):
        cls.server.shutdown()
        cls.server.server_close()
        try:
            from config import config

            config.update({"llm": {"context_window": cls._orig_cw}}, save=False)
        except Exception:
            pass

    def _make_agent(self, db_path):
        from agno.agent import Agent
        from agno.db.sqlite import SqliteDb
        from agno.models.openai import OpenAILike

        return Agent(
            model=OpenAILike(
                id="mock",
                api_key="dummy",
                base_url=f"http://127.0.0.1:{self.port}/v1",
            ),
            db=SqliteDb(db_file=db_path),
            # 有 description 才会生成 system message（否则 agno 整条省略）
            description="你是电商客服，回答买家问题。",
            add_history_to_context=True,
            num_history_runs=200,
            store_history_messages=False,
            add_session_summary_to_context=True,
        )

    def test_history_truncated_by_token_budget(self):
        """E1/E2：长会话历史被 token 预算截断，且落盘不再携带 from_history 快照。"""
        from Agent.CustomerAgent.context_budget import history_token_budget

        CAPTURED.clear()
        db_path = tempfile.mktemp(suffix="_ctx_budget.db")
        budget = history_token_budget()
        try:
            agent = self._make_agent(db_path)
            long_text = "字" * 5000  # 每轮约 5000 token

            async def _run():
                # 36 轮 × 5000 = 180000 token，超过封顶后的 150000 预算，必然截断
                for i in range(36):
                    await agent.arun(f"第{i}轮{long_text}", session_id="s1", user_id="u1")
                await agent.arun("最后确认一句", session_id="s1", user_id="u1")

            asyncio.run(_run())

            self.assertGreaterEqual(len(CAPTURED), 37, f"mock 捕获轮数不足: {len(CAPTURED)}")
            messages = CAPTURED[-1]["messages"]
            self.assertEqual(messages[0]["role"], "system", "首条应为本轮 system 消息")

            history_msgs = messages[1:-1]
            # 36 轮 × 2 条 = 72 条历史，截断后应少于这个数
            self.assertLess(len(history_msgs), 72, "历史条数未减少，截断未生效")
            self.assertNotEqual(history_msgs[0]["role"] if history_msgs else "", "tool",
                                "截断后历史不应以 tool 消息开头")

            history_tokens = _payload_text_tokens({"messages": history_msgs})
            # 允许一条消息的越界余量（从尾部累加时最后一条不拆分）
            self.assertLessEqual(
                history_tokens, budget + 7000,
                f"历史 token {history_tokens} 超出预算 {budget}",
            )

            # E2：落盘剥离历史快照
            conn = sqlite3.connect(db_path)
            try:
                rows = conn.execute("SELECT runs FROM agno_sessions").fetchall()
            finally:
                conn.close()
            history_snapshots = 0
            system_msgs = 0
            total_runs = 0
            for (runs_json,) in rows:
                for run in _load_runs(runs_json):
                    total_runs += 1
                    for m in run.get("messages") or []:
                        if m.get("from_history"):
                            history_snapshots += 1
                        if m.get("role") == "system":
                            system_msgs += 1
            self.assertGreater(total_runs, 0, "未读到任何 run")
            self.assertEqual(history_snapshots, 0, f"落盘仍存有 {history_snapshots} 条历史快照")
            self.assertEqual(system_msgs, 0, f"落盘仍存有 {system_msgs} 条 system 消息（应为 0）")
        finally:
            try:
                if os.path.exists(db_path):
                    os.remove(db_path)
            except Exception:
                pass

    def test_summary_injected_into_system_message(self):
        """E3：session.summary 会被注入 system message（滚动摘要的落地通道）。"""
        from agno.session.summary import SessionSummary

        CAPTURED.clear()
        db_path = tempfile.mktemp(suffix="_ctx_summary.db")
        marker = "买家订单 240101-9988 申请换货，已承诺 48 小时内寄出"
        try:
            agent = self._make_agent(db_path)

            async def _run():
                await agent.arun("你好", session_id="s2", user_id="u1")
                session = await agent._aread_or_create_session(session_id="s2", user_id="u1")
                session.summary = SessionSummary(summary=marker)
                await agent.asave_session(session)
                agent._cached_session = session
                await agent.arun("我的订单怎么样了", session_id="s2", user_id="u1")

            asyncio.run(_run())

            self.assertGreaterEqual(len(CAPTURED), 2, f"mock 捕获轮数不足: {len(CAPTURED)}")
            system_content = CAPTURED[-1]["messages"][0].get("content") or ""
            if isinstance(system_content, list):
                system_content = "".join(
                    b.get("text", "") for b in system_content if isinstance(b, dict)
                )
            self.assertIn("summary_of_previous_interactions", system_content,
                          "system message 未包含摘要包裹标签")
            self.assertIn("240101-9988", system_content, "system message 未包含摘要正文")
        finally:
            try:
                if os.path.exists(db_path):
                    os.remove(db_path)
            except Exception:
                pass


if __name__ == "__main__":
    unittest.main(verbosity=2)
