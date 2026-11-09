"""
知识库检索 query 构造与执行

背景（2026-11-09 排查，13:56 会话）：
买家问「19.9可以导多少」，AI 答了收藏上限数字却没发知识库里的会员购买链接。
根因不在 prompt，而在检索 query 被污染：

agno 的 add_knowledge_to_context 会把**整块拼装后的 input**（945 字）当作
向量检索 query。这块 input 包含：
  [客服素材库] 9 张图 + 5 个视频的完整描述   ≈ 700 字
  [{"type":"text","text":"内容：19.9可以导多少"}]
  [人工客服已回复] 客服(13:36): 亲亲，这里直接对接人工客服的…
素材清单里"机器码/图源/授权/教程"等词在向量空间里权重远高于买家那句 9 字提问，
实测三道完全不同的会员问题（多少坐标 / 19.9 多少 / 只是图源？）召回的 7 条
references **完全相同**，且都落在"机器码开通授权/欢迎语/图源教程"上，
含购买链接的「奥维VIP/svip/会员价格介绍」一条都没进来。

同一份知识库换成短 query 立刻命中：
  "19.9可以导多少" → 命中「奥维VIP/svip/会员价格介绍」（含 kdocs 链接）

本模块把检索 query 与模型输入彻底分离：
  模型输入 = 拼装后的完整 input（不变，模型照旧能看到素材清单/人工历史）
  检索 query = 买家原话 + 最近几轮买家原话（不含素材清单/人工话术）

query 构造策略经实测矩阵确定（7 条召回上限）：
  只用当前问题           → 命中位次 1 / 2 / 未命中
  当前 + 上一轮          → 命中位次 1 / 1 / 未命中
  当前 + 前两轮          → 命中位次 1 / 1 / 2     ← 采纳
带上一两轮能把"这款/19.9"这类指代补全，是命中位次稳定在 1-2 的关键。
"""

from __future__ import annotations

import contextvars
import re
from typing import Any, Dict, List, Optional

from utils.logger_loguru import get_logger

logger = get_logger("KnowledgeQuery")

# ---------------------------------------------------------------------------
# 当前轮的净化检索 query
#
# CustomerAgent 是全局单例、多账号线程共用，因此不能用实例属性存"当前轮
# query"（会串号）。用 contextvar：每个 asyncio 任务自带独立上下文，
# 并发账号之间互不干扰。
# ---------------------------------------------------------------------------
_current_query: contextvars.ContextVar[Optional[str]] = contextvars.ContextVar(
    "knowledge_clean_query", default=None
)

# 参与检索的最近买家轮次（含当前轮）。实测 3 轮命中位次稳定在 1-2。
_QUERY_TURN_LIMIT = 3

# 单条买家原话的最大长度（截断超长粘贴内容，避免污染向量语义）
_MAX_TURN_CHARS = 120

# 检索 query 总长度上限
_MAX_QUERY_CHARS = 400

# 买家原话里常见的「指代词」，本身不含检索语义，但与上下文拼接后能补全指代
_NOISE_PREFIXES = (
    "请问人工客服在吗",
    "在吗",
    "好的",
    "谢谢",
    "嗯",
    "OK",
    "ok",
)

# 从拼装后的 input 中抽取买家真实提问。
# 拼装格式由 agent.py 决定，形如：
#   [客服素材库]…
#   [{"type": "text", "text": "内容：19.9可以导多少"}]
#   [人工客服已回复]…
# 这里只取中间那段 JSON text 里的"内容："部分。
_JSON_TEXT_RE = re.compile(r'\{"type"\s*:\s*"text"\s*,\s*"text"\s*:\s*"(.*?)"\}', re.S)
_CONTENT_PREFIX_RE = re.compile(r"^内容[:：]\s*")


def extract_buyer_turn(text: str) -> str:
    """从单条消息里提取买家原话。

    处理两种形态：
      1. 原始文本 —— 直接用
      2. JSON 文本消息 —— 取 text 字段并剥掉"内容："前缀
    """
    if not text:
        return ""

    raw = text.strip()

    # JSON 形态：{"type": "text", "text": "内容：xxx"}
    match = _JSON_TEXT_RE.search(raw)
    if match:
        raw = match.group(1)
        # JSON 字符串转义还原
        raw = raw.replace('\\"', '"').replace("\\n", "\n").replace("\\\\", "\\")
        raw = _CONTENT_PREFIX_RE.sub("", raw.strip())
        return raw.strip()

    # 已经是纯文本，但可能带"内容："前缀
    return _CONTENT_PREFIX_RE.sub("", raw).strip()


def _is_noise_turn(text: str) -> bool:
    """判断是否为无检索价值的寒暄（问在不在/好的等）。"""
    if not text:
        return True
    compact = text.strip()
    return compact in _NOISE_PREFIXES


def build_query(turns: List[str]) -> str:
    """把若干轮买家原话拼成检索 query。

    参数 turns 按时间正序排列（最早 → 最新）。返回从后往前拼接，
    让最近一轮的语义在向量里权重更高。
    """
    picked: List[str] = []
    for turn in turns[-_QUERY_TURN_LIMIT:]:
        text = extract_buyer_turn(turn)
        if not text or _is_noise_turn(text):
            continue
        if len(text) > _MAX_TURN_CHARS:
            text = text[:_MAX_TURN_CHARS]
        picked.append(text)

    if not picked:
        return ""

    query = " ".join(reversed(picked))
    if len(query) > _MAX_QUERY_CHARS:
        query = query[-_MAX_QUERY_CHARS:]
    return query


def build_query_from_input(final_input: str, extra_turns: Optional[List[str]] = None) -> str:
    """从拼装后的 input + 额外的买家轮次构造检索 query。

    final_input 里含素材清单与人工话术，抽取时只取其中的买家提问段；
    extra_turns 用来补上「当前轮之前」的历史买家原话。
    """
    candidates: List[str] = []
    if extra_turns:
        candidates.extend(extra_turns)
    if final_input:
        candidates.append(final_input)
    return build_query(candidates)


async def search_with_clean_query(
    knowledge_manager: Any,
    query: str,
    limit: Optional[int] = None,
) -> List[Dict[str, Any]]:
    """用净化后的 query 检索知识库，返回 Document.to_dict() 列表。

    检索走 knowledge_manager（主进程是 IPC 代理，实际检索在 lancedb 子进程），
    这里只负责把 query 换干净。失败返回空列表——上层据此跳过 references 注入，
    不能因为检索失败而中断回复。
    """
    if not query:
        return []
    if knowledge_manager is None:
        return []

    try:
        import asyncio

        search = getattr(knowledge_manager, "search_knowledge", None)
        if search is None:
            return []

        result = await asyncio.to_thread(search, query, limit)
        docs = result or []
        return [_doc_to_dict(doc) for doc in docs]
    except Exception as e:
        logger.warning(f"[知识库检索] 净化 query 检索失败（跳过注入）: {type(e).__name__}: {e}")
        return []


def set_current_query(query: str):
    """置位当前轮的净化 query，返回 token（用于 reset）。"""
    return _current_query.set(query)


def reset_current_query(token) -> None:
    """复位当前轮的净化 query。"""
    try:
        _current_query.reset(token)
    except Exception:
        pass


def get_current_query() -> str:
    """读取当前轮的净化 query（未置位时返回空串）。"""
    return _current_query.get() or ""



def _doc_to_dict(doc: Any) -> Dict[str, Any]:
    """把 Document / dict 统一成 agno references 用的 dict 形态。"""
    if isinstance(doc, dict):
        return doc
    to_dict = getattr(doc, "to_dict", None)
    if callable(to_dict):
        try:
            result = to_dict()
            if isinstance(result, dict):
                return result
        except Exception:
            pass
    return {
        "name": getattr(doc, "name", "") or "",
        "content": getattr(doc, "content", "") or "",
        "meta_data": getattr(doc, "meta_data", {}) or {},
    }


__all__ = [
    "build_query",
    "build_query_from_input",
    "extract_buyer_turn",
    "get_current_query",
    "reset_current_query",
    "search_with_clean_query",
    "set_current_query",
]
