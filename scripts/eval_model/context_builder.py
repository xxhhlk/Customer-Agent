"""把一条样本还原成一次完整的 LLM 调用输入。

对齐生产（agent.py / _async_reply_locked）的三处注入：
1. 客服素材库清单（前置）
2. 买家本轮消息（防抖合并后的聚合文本）
3. [人工客服已回复] 历史块（最近 20 条）

额外注入 A′ 口径所需的「参考话术」，并对疑似答案泄漏的话术做过滤。
"""

from __future__ import annotations

import json
from typing import Any

from scripts.eval_model.common import (
    HIST_BUDGET_TOKENS, MAX_HISTORY_MSGS, MAX_STAFF_INJECT,
    _URL_RE, est_tokens, extract_facts, kb_fact_texts, retrieve_reference, ro_conn,
)

_ROLE = {"inbound": "user", "outbound": "assistant"}


def _media_url(msg: dict) -> str:
    """从消息里取可访问的图片地址。"""
    content = str(msg.get("content") or "").strip()
    if content.startswith("http"):
        return content
    meta = msg.get("media_meta")
    if meta:
        try:
            d = json.loads(meta) if isinstance(meta, str) else meta
            for key in ("url", "cover_url", "thumb_url", "image_url"):
                if d.get(key):
                    return str(d[key])
        except Exception:
            pass
    m = _URL_RE.search(content)
    return m.group(0) if m else ""


def load_history(sample: dict, budget: int = HIST_BUDGET_TOKENS) -> list[dict]:
    """取该样本之前同会话的消息，倒序累加到 token 预算，再翻转成对话顺序。"""
    con = ro_conn()
    rows = con.execute(
        "select direction, content, context_type from chat_message_records "
        "where shop_id=? and buyer_uid=? and id<? and content is not null and content!='' "
        "order by id desc limit ?",
        (sample["shop_id"], sample["buyer_uid"], sample["id"], MAX_HISTORY_MSGS * 2),
    ).fetchall()
    con.close()

    picked: list[tuple[str, str]] = []
    used = 0
    for direction, content, ctype in rows:
        role = _ROLE.get(str(direction), "user")
        text = str(content)
        if ctype in ("image", "video") and text.startswith("http"):
            text = "[图片]" if ctype == "image" else "[视频]"
        cost = est_tokens(text)
        if used + cost > budget and picked:
            break
        picked.append((role, text))
        used += cost
        if len(picked) >= MAX_HISTORY_MSGS:
            break
    picked.reverse()

    merged: list[dict] = []
    for role, text in picked:
        if merged and merged[-1]["role"] == role:
            merged[-1]["content"] += "\n" + text
        else:
            merged.append({"role": role, "content": text})
    while merged and merged[0]["role"] != "user":
        merged.pop(0)
    return merged


def build_input_text(sample: dict, material_block: str = "",
                     references: list[dict] | None = None) -> str:
    """拼本轮 user 文本：素材清单 + 买家问题 + 人工历史 + 参考话术。"""
    parts: list[str] = []
    if material_block:
        parts.append(material_block)
    parts.append(sample["question"])

    staff = sample.get("prev_staff") or []
    if staff:
        lines = []
        for s in staff[-MAX_STAFF_INJECT:]:
            ts = str(s.get("ts") or "")[11:16]
            lines.append(f"客服({ts}): {s['content']}")
        parts.append("[人工客服已回复]\n" + "\n".join(lines))

    if references:
        ref_lines = [f"{i + 1}. {r['name']}：{r['content']}" for i, r in enumerate(references)]
        parts.append("[参考话术]（知识库检索结果，优先沿用其中的准确表述）\n" + "\n".join(ref_lines))
    return "\n\n".join(p for p in parts if p)


def build_messages(sample: dict, system: str, kb: list[dict],
                   material_block: str = "") -> dict[str, Any]:
    """返回 {messages, meta}；meta 记录上下文来源与泄漏过滤情况。"""
    question_for_retrieval = sample["question"].replace("[买家发来图片]", "").strip()
    refs, dropped = retrieve_reference(question_for_retrieval, kb, k=3)
    history = load_history(sample)
    user_text = build_input_text(sample, material_block, refs)

    messages: list[dict] = [{"role": "system", "content": system}]
    messages.extend(history)

    images: list[str] = []
    for m in sample.get("buyer_msgs") or []:
        if str(m.get("context_type") or "") == "image":
            u = _media_url(m)
            if u:
                images.append(u)
    images = images[:2]
    if images:
        content: list[dict] = [{"type": "text", "text": user_text}]
        for url in images:
            content.append({"type": "image_url", "image_url": {"url": url}})
        messages.append({"role": "user", "content": content})
    else:
        messages.append({"role": "user", "content": user_text})

    facts = extract_facts(sample["gold"])
    allowed = facts["url"] | facts["domain"] | facts["version"]
    allowed |= extract_facts(kb_fact_texts(kb))["url"]
    allowed |= extract_facts(kb_fact_texts(kb))["domain"]
    allowed |= extract_facts(kb_fact_texts(kb))["version"]

    return {
        "messages": messages,
        "meta": {
            "history_msgs": len(history),
            "history_tokens": sum(est_tokens(m["content"] if isinstance(m["content"], str) else user_text) for m in history),
            "refs": [{"name": r["name"], "score": r["score"]} for r in refs],
            "refs_dropped_as_leak": dropped,
            "images": images,
            "input_tokens_est": est_tokens(user_text) + est_tokens(system),
            "allowed_facts": sorted(allowed)[:200],
        },
    }


def load_material_snapshot(shop_id: str, user_id: str, cache_path) -> str:
    """素材清单快照：首次拉取后固定，保证所有模型看到同一份。失败返回空串。"""
    if cache_path.exists():
        try:
            return json.loads(cache_path.read_text(encoding="utf-8")).get("block", "")
        except Exception:
            return ""
    block = ""
    try:
        from Agent.CustomerAgent.material_context import load_material_catalog

        block = load_material_catalog(shop_id, user_id) or ""
    except Exception as exc:
        print(f"[warn] 素材清单拉取失败（跳过注入）: {type(exc).__name__}: {exc}")
    cache_path.parent.mkdir(parents=True, exist_ok=True)
    cache_path.write_text(json.dumps({"shop_id": shop_id, "block": block}, ensure_ascii=False),
                          encoding="utf-8")
    return block
