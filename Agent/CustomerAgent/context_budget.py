"""上下文 token 预算与滚动摘要的纯函数集合。

设计要点（与 agno 2.3.4 内置能力的差异）：

1. agno 的 ``utils.tokens.count_tokens`` 在未装 tiktoken 时退化为 ``len//4``，
   中文被低估 3~4 倍；本模块 CJK 统一按 1 字 = 1 token 估算（偏保守，宁可早
   截断也不超模型窗口）。
2. 视觉 token 与 base64 长度无关：一张 768 宽帧 base64 约 90KB 但只占约 1.2k
   token，按字符数算会得出几十万 token 的错误结论，故按张数固定估。
3. 全部函数无 IO、无全局状态，便于探针直接断言。

**禁止**import lancedb / agent_knowledge / knowledge_enhanced：主进程一旦连带
加载 lancedb C 扩展就会击穿子进程隔离，触发 ntdll 堆崩溃（0xfc0）。本模块只
用标准库，``get_config`` 走惰性 import。
"""

from __future__ import annotations

import re
from typing import Any, List, Optional, Tuple

# ---------------------------------------------------------------------------
# 预算参数
# ---------------------------------------------------------------------------

# 历史注入预算占 context_window 的比例。剩余预算留给 system prompt、知识库
# 检索结果（add_knowledge_to_context=True）、本轮输入与模型输出。
HISTORY_BUDGET_RATIO = 0.40

# 历史估算达到预算的该比例时，后台生成滚动摘要（略早于截断点，提前准备）。
SUMMARY_TRIGGER_RATIO = 0.85

# 生成摘要时，末尾这段比例的历史保留原文，更早的部分进摘要。
KEEP_VERBATIM_RATIO = 0.50

# 默认上下文窗口（tokens）。国内主流模型 doubao-seed / deepseek-v4 均为 128k。
DEFAULT_CONTEXT_WINDOW = 128000
MIN_CONTEXT_WINDOW = 8000
MAX_CONTEXT_WINDOW = 1000000

# 编造截断逻辑时的保底条数：即使单条消息就超预算，也至少留这么多条，
# 否则模型会收到一个空历史（等价于失忆），比超预算更糟。
MIN_KEEP_MESSAGES = 2

# 单张图片固定估算的视觉 token。URL 与 base64 相同：agno 会把历史 images
# 无条件转成 image_url 内容块发给模型，计费口径与字节数无关。
IMG_TOKENS = 1200
# 原生 video_url 内容块（仅本轮输入可能出现，历史里不会重放）
VIDEO_URL_TOKENS = 8000

SUMMARY_MAX_CHARS = 800       # 摘要正文上限（超出直接硬截，防模型啰嗦）
SUMMARY_TIMEOUT = 45.0        # 摘要单次 LLM 超时（秒）
SUMMARY_INPUT_MSG_LIMIT = 600 # 拼接摘要输入时单条消息的截断长度


# ---------------------------------------------------------------------------
# token 估算
# ---------------------------------------------------------------------------

# 汉字（基本区 + 扩展 A + 兼容区）+ 日文假名 + 谚文
_CJK_RE = re.compile(
    r"[\u3400-\u4dbf\u4e00-\u9fff\uf900-\ufaff"
    r"\u3040-\u30ff\u31f0-\u31ff"
    r"\uac00-\ud7af\u1100-\u11ff]"
)

# emoji 主码点与常见符号区。零宽连接符 / 变体选择符不在此列：它们是
# 依附字符，按普通字符（1/4 token）计更接近真实 tokenizer 行为。
_EMOJI_RE = re.compile("[\U0001F300-\U0001FAFF\u2600-\u27BF]")


def estimate_text_tokens(text: str) -> int:
    """估算文本 token 数。

    CJK 1 字 = 1 token，emoji = 3 token，其余按 4 字符 = 1 token（向上取整）。
    对中文场景这是偏保守的下界，用来做预算判断足够。
    """
    if not text:
        return 0
    if not isinstance(text, str):
        text = str(text)
    cjk = len(_CJK_RE.findall(text))
    emoji = len(_EMOJI_RE.findall(text))
    rest = len(text) - cjk - emoji
    if rest < 0:
        # 极少见的区间重叠，保守兜底
        rest = 0
    return cjk + emoji * 3 + (rest + 3) // 4


def estimate_content_tokens(content: Any) -> int:
    """估算 Message.content 的 token 数（字符串或 OpenAI 多模态块列表）。"""
    if content is None:
        return 0
    if isinstance(content, str):
        return estimate_text_tokens(content)
    if isinstance(content, (list, tuple)):
        total = 0
        for block in content:
            if not isinstance(block, dict):
                total += estimate_text_tokens(str(block))
                continue
            btype = block.get("type")
            if btype in ("text", "input_text"):
                total += estimate_text_tokens(str(block.get("text") or ""))
            elif btype == "image_url":
                total += IMG_TOKENS
            elif btype in ("video_url", "input_video"):
                total += VIDEO_URL_TOKENS
            else:
                # 未知块按文本处理，避免漏算
                total += estimate_text_tokens(str(block))
        return total
    return estimate_text_tokens(str(content))


def estimate_message_tokens(msg: Any) -> int:
    """估算单条 Message 的 token 数。"""
    if msg is None:
        return 0
    total = estimate_content_tokens(getattr(msg, "content", None))
    total += IMG_TOKENS * len(getattr(msg, "images", None) or [])
    total += IMG_TOKENS * len(getattr(msg, "videos", None) or [])
    total += IMG_TOKENS * len(getattr(msg, "audio", None) or [])
    tool_calls = getattr(msg, "tool_calls", None)
    if tool_calls:
        total += estimate_text_tokens(str(tool_calls))
    return total


def estimate_messages_tokens(messages: Any) -> int:
    """估算一组 Message 的 token 总数。"""
    return sum(estimate_message_tokens(m) for m in messages or [])


# ---------------------------------------------------------------------------
# 预算读取
# ---------------------------------------------------------------------------

def context_window() -> int:
    """读取配置里的模型上下文窗口，夹取到 [8000, 1000000]。

    每轮实时读取，设置页保存后下一条回复即生效（与 llm.reply_timeout 同款）。
    """
    from config import get_config

    try:
        value = int(get_config("llm.context_window", DEFAULT_CONTEXT_WINDOW))
    except (TypeError, ValueError):
        return DEFAULT_CONTEXT_WINDOW
    return max(MIN_CONTEXT_WINDOW, min(MAX_CONTEXT_WINDOW, value))


def history_token_budget() -> int:
    """历史注入允许占用的 token 预算。"""
    return int(context_window() * HISTORY_BUDGET_RATIO)


# ---------------------------------------------------------------------------
# 截断与摘要选片
# ---------------------------------------------------------------------------

def _drop_leading_tool_messages(messages: List[Any]) -> List[Any]:
    """去掉队首的 tool 消息。

    OpenAI 协议要求 tool 消息必须紧跟带 tool_calls 的 assistant 消息，否则整条
    请求报错。agno 自己的 limit 分支也做同样处理（session/agent.py:202-203），
    但 last_n_runs 分支没有，截断后必须由我们补上。
    """
    start = 0
    while start < len(messages) and getattr(messages[start], "role", "") == "tool":
        start += 1
    return messages[start:] if start else messages


def truncate_messages_by_tokens(
    messages: List[Any], budget: int, min_keep: int = MIN_KEEP_MESSAGES
) -> Tuple[List[Any], dict]:
    """从末尾向前累加 token 预算，超出则丢弃更早的消息。

    Returns:
        (kept, stats)。stats 含 total / kept / dropped / dropped_tokens，
        仅用于日志与探针。
    """
    if not messages:
        return [], {"total": 0, "kept": 0, "dropped": 0, "dropped_tokens": 0}

    body = list(messages)
    # 历史里的 system 消息一律丢弃：agno 调用 get_messages 时传了
    # skip_roles=["system"]（system_message_role="system"），正常情况下不会出现；
    # 这里做兜底，避免旧日期 / 旧摘要随历史重复注入。
    if body and getattr(body[0], "role", "") == "system":
        body = body[1:]
    if not body:
        return [], {"total": 0, "kept": 0, "dropped": len(messages), "dropped_tokens": 0}

    kept_rev: List[Any] = []
    used = 0
    for msg in reversed(body):
        cost = estimate_message_tokens(msg)
        if len(kept_rev) >= min_keep and used + cost > budget:
            break
        kept_rev.append(msg)
        used += cost

    kept = list(reversed(kept_rev))
    dropped = body[: len(body) - len(kept)]
    kept = _drop_leading_tool_messages(kept)

    return kept, {
        "total": used + estimate_messages_tokens(dropped),
        "kept": len(kept),
        "dropped": len(dropped),
        "dropped_tokens": estimate_messages_tokens(dropped),
    }


def select_summarizable(
    messages: List[Any], keep_budget: int, upto_msg_id: Optional[str] = None
) -> Tuple[List[Any], Optional[str]]:
    """从历史里切出「该进摘要」的旧消息。

    末尾按 keep_budget 保留原文，更早的部分返回给摘要任务。

    Args:
        upto_msg_id: 上一次摘要覆盖到的消息 id。传入时只返回其后的增量，
            避免同一段对话被反复摘要。

    Returns:
        (old_messages, new_upto_msg_id)
    """
    if not messages:
        return [], upto_msg_id

    keep_count = 0
    used = 0
    for msg in reversed(messages):
        cost = estimate_message_tokens(msg)
        if used + cost > keep_budget:
            break
        used += cost
        keep_count += 1

    cut = len(messages) - keep_count
    old = list(messages[:cut])

    if upto_msg_id:
        for idx, msg in enumerate(old):
            if getattr(msg, "id", None) == upto_msg_id:
                old = old[idx + 1:]
                break

    new_marker = getattr(old[-1], "id", None) if old else upto_msg_id
    return old, new_marker


# ---------------------------------------------------------------------------
# 摘要提示词
# ---------------------------------------------------------------------------

SUMMARY_SYSTEM_PROMPT = (
    "你是电商客服会话的上下文压缩器。把「已有摘要」和「新增对话」合并成一段新的中文摘要。\n"
    "只保留对后续客服回复有用的信息：买家的称呼与身份、订单号 / 商品 / 金额 / 规格、"
    "已经承诺过的处理方案、已经排查过的步骤与结论、仍未解决的问题、买家的情绪与禁忌事项。\n"
    "要求：第三人称陈述句；不超过 400 字；不要寒暄、不要编造、不要输出标题或分析过程；"
    "遇到「[internal:」开头的内部标记时，视为「人工客服已直接回复」，不要原样重复该标记。"
)


def build_summary_input(prev_summary: str, old_messages: List[Any]) -> str:
    """把「已有摘要 + 待摘要的旧消息」拼成摘要模型的 user message。"""
    lines = [
        "【已有摘要】",
        (prev_summary or "").strip() or "（无）",
        "【新增对话】",
    ]
    for msg in old_messages:
        role = getattr(msg, "role", "")
        if role in ("tool", "system"):
            continue
        content = getattr(msg, "content", "")
        if not isinstance(content, str):
            content = str(content)
        content = content.strip()
        if not content or content.startswith("[internal:"):
            content = "（人工客服已直接回复）"
        else:
            content = content[:SUMMARY_INPUT_MSG_LIMIT]
        who = "买家" if role == "user" else "客服"
        lines.append(f"{who}: {content}")
    return "\n".join(lines)
