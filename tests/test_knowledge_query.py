"""知识库检索 query 净化回归测试（纯本地，不产生真实 API 调用）。

场景（2026-11-09 13:56 实测事故）：
  买家问「19.9可以导多少」，AI 答了收藏上限数字却没发知识库里的会员购买链接。

根因：agno 的 add_knowledge_to_context 拿整块拼装后的 input（945 字，含素材
清单 + 人工历史 + 订单块）当向量检索 query，买家的短问题被彻底稀释，三道不同
的会员问题召回结果完全相同，且都召不回含链接的「奥维VIP/svip/会员价格介绍」。

本测试覆盖：
  1. extract_buyer_turn —— 从污染 input 里精准抽出买家原话
  2. build_query —— 多轮买家原话拼 query（滤寒暄、控长度）
  3. _knowledge_retriever —— 钩子确实用净化 query 检索，且不影响模型自主调工具

运行：uv run python tests/test_knowledge_query.py -v
"""

import sys
import unittest
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from Agent.CustomerAgent.knowledge_query import (  # noqa: E402
    build_query,
    build_query_from_input,
    extract_buyer_turn,
    get_current_query,
    reset_current_query,
    set_current_query,
)


# 13:56 那轮真实拼装后的 input（截取自 agno session 落盘内容）
REAL_POLLUTED_INPUT = (
    "[客服素材库]（买家需要看图/看视频说明时，调用 send_material 发送，"
    "material_name 必须与名称完全一致；破折号后是素材内容，看不出来时可调用 "
    "view_material 查看实际画面）\n"
    "图片：电脑获取机器码 —— 软件教程图指示点击帮助菜单的VIP管理并复制机器码发给客服；"
    "安卓获取机器码 —— 奥维地图安卓设备四步获取机器码并复制发给网店客服操作图\n"
    "视频：奥维安卓-解决企业不能输入_202406_无水印_Mux；奥维重新加地图_Mux；"
    "奥维登录202406；奥维 - 拼多多 机器码_Mux；历史影像_202406\n"
    '[{"type": "text", "text": "内容：19.9可以导多少"}]\n'
    "[人工客服已回复]\n"
    "客服(13:36): 亲亲，这里直接对接人工客服的我们提供企业账号登录，20级高清图源，"
    "可看全世界，高速稳定，简单实用，导航，测距，直接下单，不会用全额退款，一分钟立即安装     \n"
    "您好，请问您具体想问什么问题呢？\n"
    "亲，这里直接对接人工客服的，需要什么直接问"
)


class TestExtractBuyerTurn(unittest.TestCase):
    """从污染 input 里抽买家原话。"""

    def test_extract_from_real_polluted_input(self):
        got = extract_buyer_turn(REAL_POLLUTED_INPUT)
        self.assertEqual(got, "19.9可以导多少")

    def test_extract_plain_text(self):
        self.assertEqual(extract_buyer_turn("这款可以导进去多少个坐标？"), "这款可以导进去多少个坐标？")

    def test_extract_json_text_message(self):
        got = extract_buyer_turn('[{"type": "text", "text": "内容：功能？"}]')
        self.assertEqual(got, "功能？")

    def test_extract_strips_content_prefix(self):
        self.assertEqual(extract_buyer_turn("内容：19.9可以导多少"), "19.9可以导多少")

    def test_empty_input(self):
        self.assertEqual(extract_buyer_turn(""), "")
        self.assertEqual(extract_buyer_turn("   "), "")

    def test_no_material_or_staff_leak(self):
        """抽取结果里不能残留素材清单/人工话术的任何片段。"""
        got = extract_buyer_turn(REAL_POLLUTED_INPUT)
        for leak in ("客服素材库", "机器码", "send_material", "人工客服已回复", "亲亲"):
            self.assertNotIn(leak, got)


class TestBuildQuery(unittest.TestCase):
    """多轮买家原话拼检索 query。"""

    def test_current_only(self):
        self.assertEqual(build_query(["19.9可以导多少"]), "19.9可以导多少")

    def test_merges_recent_turns_newest_first(self):
        q = build_query(["请问人工客服在吗", "这款可以导进去多少个坐标？", "19.9可以导多少"])
        # 最近的轮次排在前面（向量里权重更高）
        self.assertEqual(q, "19.9可以导多少 这款可以导进去多少个坐标？")

    def test_filters_noise_turns(self):
        self.assertEqual(build_query(["好的"]), "")
        self.assertEqual(build_query(["请问人工客服在吗", "19.9可以导多少"]), "19.9可以导多少")

    def test_limits_turn_count(self):
        turns = [f"问题{i}" for i in range(10)]
        q = build_query(turns)
        # 只保留最近 3 轮
        self.assertNotIn("问题0", q)
        self.assertNotIn("问题5", q)
        self.assertIn("问题9", q)
        self.assertIn("问题7", q)

    def test_limits_total_length(self):
        q = build_query(["很长的提问" * 100])
        self.assertLessEqual(len(q), 400)

    def test_from_input_uses_only_buyer_turn(self):
        q = build_query_from_input(REAL_POLLUTED_INPUT, [])
        self.assertEqual(q, "19.9可以导多少")

    def test_from_input_with_history(self):
        q = build_query_from_input(
            REAL_POLLUTED_INPUT, ["这款可以导进去多少个坐标？"]
        )
        self.assertEqual(q, "19.9可以导多少 这款可以导进去多少个坐标？")


class TestCurrentQueryContextVar(unittest.TestCase):
    """contextvar 存取与复位。"""

    def test_set_get_reset(self):
        self.assertEqual(get_current_query(), "")
        token = set_current_query("19.9可以导多少")
        try:
            self.assertEqual(get_current_query(), "19.9可以导多少")
        finally:
            reset_current_query(token)
        self.assertEqual(get_current_query(), "")

    def test_nested_reset_restores_outer(self):
        outer = set_current_query("outer")
        inner = set_current_query("inner")
        self.assertEqual(get_current_query(), "inner")
        reset_current_query(inner)
        self.assertEqual(get_current_query(), "outer")
        reset_current_query(outer)


class TestKnowledgeRetrieverHook(unittest.TestCase):
    """_knowledge_retriever 钩子行为。"""

    def _make_agent(self, docs):
        class _Doc:
            def __init__(self, name, content):
                self.name = name
                self.content = content

            def to_dict(self):
                return {"name": self.name, "content": self.content}

        class _Knowledge:
            def __init__(self):
                self.calls = []

            def search(self, query, max_results=None, filters=None):
                self.calls.append(query)
                return docs

        class _Agent:
            def __init__(self):
                self.knowledge = _Knowledge()

        return _Agent()

    def test_uses_clean_query_when_set(self):
        from Agent.CustomerAgent.agent import _knowledge_retriever

        agent = self._make_agent([])
        token = set_current_query("19.9可以导多少")
        try:
            _knowledge_retriever(agent=agent, query=REAL_POLLUTED_INPUT, num_documents=7)
        finally:
            reset_current_query(token)

        self.assertEqual(len(agent.knowledge.calls), 1)
        used = agent.knowledge.calls[0]
        self.assertEqual(used, "19.9可以导多少")
        self.assertNotIn("客服素材库", used)
        self.assertLess(len(used), 50)

    def test_model_tool_query_is_not_overridden(self):
        """模型自主调 search_knowledge_base 时，query 是模型自己提炼的关键词，
        必须原样放行——否则会覆盖模型的判断力。
        """
        from Agent.CustomerAgent.agent import _knowledge_retriever

        agent = self._make_agent([])
        token = set_current_query("19.9可以导多少")
        try:
            _knowledge_retriever(agent=agent, query="VIP SVIP 会员功能", num_documents=7)
        finally:
            reset_current_query(token)

        self.assertEqual(agent.knowledge.calls, ["VIP SVIP 会员功能"])

    def test_falls_back_to_original_query_when_not_set(self):
        """净化 query 提取失败时原样放行，退化成修复前行为，不中断回复。"""
        from Agent.CustomerAgent.agent import _knowledge_retriever

        agent = self._make_agent([])
        _knowledge_retriever(agent=agent, query="原始 query", num_documents=7)
        self.assertEqual(agent.knowledge.calls, ["原始 query"])

    def test_returns_doc_dicts(self):
        from Agent.CustomerAgent.agent import _knowledge_retriever

        agent = self._make_agent([type("D", (), {"name": "奥维VIP/svip/会员价格介绍",
                                                 "content": "kdocs.cn/l/csB1Zg1C0Vl4",
                                                 "to_dict": lambda self: {"name": "奥维VIP/svip/会员价格介绍",
                                                                          "content": "kdocs.cn/l/csB1Zg1C0Vl4"}})()])
        token = set_current_query("19.9可以导多少")
        try:
            result = _knowledge_retriever(agent=agent, query=REAL_POLLUTED_INPUT, num_documents=7)
        finally:
            reset_current_query(token)

        self.assertEqual(len(result), 1)
        self.assertEqual(result[0]["name"], "奥维VIP/svip/会员价格介绍")

    def test_search_failure_does_not_raise(self):
        """检索失败返回空列表，不能中断回复。"""
        from Agent.CustomerAgent.agent import _knowledge_retriever

        class _Boom:
            def search(self, *a, **kw):
                raise RuntimeError("lancedb 挂了")

        class _Agent:
            knowledge = _Boom()

        result = _knowledge_retriever(agent=_Agent(), query="x", num_documents=7)
        self.assertEqual(result, [])

    def test_no_knowledge_returns_empty(self):
        from Agent.CustomerAgent.agent import _knowledge_retriever

        class _Agent:
            knowledge = None

        self.assertEqual(_knowledge_retriever(agent=_Agent(), query="x", num_documents=7), [])


if __name__ == "__main__":
    unittest.main(verbosity=2)
