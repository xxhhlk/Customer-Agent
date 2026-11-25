"""B 口径：真 Agent + 真知识库检索的端到端评测。

与 A′（run_eval.py）的区别：走 `CustomerAgent.async_reply()`，模型可自主调用
`search_knowledge_base`、`send_material`、`view_material`、`get_time` 等工具，
历史、订单、素材清单三类注入全部按生产逻辑执行。

安全边界：
- agno session 写到 temp/eval/agent_eval.db（改内存 db_path，不落盘 config、不碰生产库）
- send_material / send_goods_link / transfer_conversation 全部打桩，不会真的往拼多多发消息
- 每条样本用独立 buyer_uid（eval<SID>），session 互不污染

用法：
    python -m scripts.eval_model.run_eval_agent --samples temp/eval/needs_kb.json --limit 5 --dry-run
    python -m scripts.eval_model.run_eval_agent --models workbuddy/glm-5.3-flash,workbuddy/deepseek-v4.1-flash

产物：temp/eval/raw_agent/<model>/<sample_id>.json（字段与 A′ 对齐，judge.py 可直接复用）
"""

from __future__ import annotations

import argparse
import asyncio
import json
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from scripts.eval_model.common import (  # noqa: E402
    OUT_DIR, read_jsonl, ro_conn, set_config_in_memory,
)

EVAL_DB = OUT_DIR / "agent_eval.db"
RAW_DIR = OUT_DIR / "raw_agent"
DEFAULT_MODELS = ["workbuddy/glm-5.3-flash", "workbuddy/deepseek-v4.1-flash"]


def patch_outbound_tools() -> None:
    """打桩所有会真正对外发消息的工具，防止评测把消息发给真实买家。

    同时掐掉两处依赖拼多多登录态的注入（订单、素材清单）：无 cookie 环境下
    它们会触发自动重登流程，既慢又可能扰动生产登录态。
    """
    import Agent.CustomerAgent.agent as agent_mod
    import Agent.CustomerAgent.material_context as material_mod
    import Agent.CustomerAgent.order_context as order_mod

    def stub_send_material(recipient_uid: str, material_name: str, shop_id: str, user_id: str) -> str:
        return f"[评测模式] 素材「{material_name}」已模拟发送（未真正推送）"

    def stub_send_goods_link(recipient_uid: str, goods_id: int, shop_id: str, user_id: str) -> str:
        return f"[评测模式] 商品卡片 {goods_id} 已模拟发送（未真正推送）"

    def stub_transfer(shop_id: str, user_id: str, recipient_uid: str) -> str:
        return "[评测模式] 转人工已模拟（未真正执行）"

    agent_mod.send_material = stub_send_material
    agent_mod.send_goods_link = stub_send_goods_link
    agent_mod.transfer_conversation = stub_transfer

    # 订单 / 素材清单：无登录态，直接返回空，避免触发 PDD 请求与自动重登
    order_mod.load_order_block = lambda *a, **k: ""
    material_mod.load_material_catalog = lambda *a, **k: ""


def build_context(sample: dict, buyer_uid: str):
    from bridge.context import ChannelType, Context, ContextType

    images = [m for m in (sample.get("buyer_msgs") or [])
              if str(m.get("context_type") or "") == "image"]
    text = sample.get("question") or ""
    if images:
        url = str(images[-1].get("content") or "").strip()
        if url.startswith("http"):
            ctx = Context.create_pinduoduo_context(
                content=url,
                msg_id=f"eval-{sample['sample_id']}",
                from_uid=buyer_uid,
                shop_id=sample["shop_id"],
                user_id=str(sample.get("user_id") or "0"),
                shop_name="",
                channel_type=ChannelType.PINDUODUO,
                user_msg_type=ContextType.IMAGE,
            )
            ctx.merged_text = text.replace("[买家发来图片]", "").strip()
            return ctx

    return Context.create_pinduoduo_context(
        content=text,
        msg_id=f"eval-{sample['sample_id']}",
        from_uid=buyer_uid,
        shop_id=sample["shop_id"],
        user_id=str(sample.get("user_id") or "0"),
        shop_name="",
        channel_type=ChannelType.PINDUODUO,
        user_msg_type=ContextType.TEXT,
    )


def feed_staff_cache(sample: dict, buyer_uid: str) -> None:
    """把该样本之前的人工回复灌进缓存，复现生产里的 [人工客服已回复] 注入。"""
    from Message.handlers.staff_message_cache import staff_message_cache

    staff_message_cache.clear(buyer_uid)
    for item in sample.get("prev_staff") or []:
        staff_message_cache.add_message(buyer_uid, str(item.get("content") or ""))


def fill_user_id(samples: list[dict]) -> None:
    """样本里没存登录账号 user_id 时，从 accounts 表取该店铺的账号。"""
    con = ro_conn()
    cache: dict[str, str] = {}
    for s in samples:
        if s.get("user_id"):
            continue
        shop = str(s.get("shop_id") or "")
        if shop not in cache:
            row = con.execute("select user_id from accounts where shop_id=? limit 1", (shop,)).fetchone()
            cache[shop] = str(row[0]) if row else "0"
        s["user_id"] = cache[shop]
    con.close()


async def run(args: argparse.Namespace) -> int:
    samples = read_jsonl(Path(args.samples))
    if args.limit:
        samples = samples[: args.limit]
    fill_user_id(samples)
    models = [m.strip() for m in args.models.split(",") if m.strip()]

    patch_outbound_tools()
    RAW_DIR.mkdir(parents=True, exist_ok=True)

    pending = []
    for m in models:
        for s in samples:
            out = RAW_DIR / m.replace("/", "__") / f"{s['sample_id']}.json"
            if out.exists() and not args.force:
                try:
                    if json.loads(out.read_text(encoding="utf-8")).get("ok"):
                        continue
                except Exception:
                    pass
            pending.append((m, s))

    print(f"[info] 样本 {len(samples)} × 模型 {len(models)}，待跑 {len(pending)}")
    if args.dry_run:
        for m, s in pending[:5]:
            print(f"  {m} {s['sample_id']} Q={s['question'][:50]!r}")
        return 0
    if not pending:
        return 0

    # 隔离 session 库：必须在 initialize_async 之前改，Agent 构造时才读 db_path
    EVAL_DB.parent.mkdir(parents=True, exist_ok=True)
    set_config_in_memory("db_path", str(EVAL_DB))

    # 补上 DI 容器注册（生产在 app.py 里做）：否则历史聊天记录注入会报
    # 「服务 DatabaseManager 未注册」，上下文比生产短一截。
    from config import config as app_config
    from core.di_container import configure_standard_services
    configure_standard_services(app_config)

    from Agent.CustomerAgent.agent import CustomerAgent

    agent = CustomerAgent()
    t0 = time.perf_counter()
    if not await agent.initialize_async():
        print("[err] Agent 初始化失败")
        return 1
    print(f"[info] Agent 初始化完成 {time.perf_counter() - t0:.1f}s（含 lancedb 子进程）")

    done = 0
    for model, sample in pending:
        set_config_in_memory("llm.model_name", model)
        buyer_uid = f"eval{sample['sample_id']}"
        feed_staff_cache(sample, buyer_uid)
        ctx = build_context(sample, buyer_uid)
        query = sample.get("question") or ""
        out = RAW_DIR / model.replace("/", "__") / f"{sample['sample_id']}.json"
        out.parent.mkdir(parents=True, exist_ok=True)

        started = time.perf_counter()
        err = ""
        content = ""
        try:
            from bridge.reply import ReplyType

            reply = await asyncio.wait_for(agent.async_reply(query, ctx), timeout=args.timeout)
            content = str(getattr(reply, "content", "") or "")
            if getattr(reply, "type", None) not in (None, ReplyType.TEXT):
                err = f"non-text reply: {getattr(reply, 'type', '')}"
        except asyncio.TimeoutError:
            err = f"timeout>{args.timeout}s"
        except Exception as exc:
            err = f"{type(exc).__name__}: {exc}"

        rec = {
            "sample_id": sample["sample_id"],
            "model": model,
            "gold": sample["gold"],
            "question": sample["question"],
            "tags": sample["tags"],
            "shop_id": sample["shop_id"],
            "buyer_hash": sample["buyer_hash"],
            "ok": bool(content) and not err,
            "content": content,
            "latency": round(time.perf_counter() - started, 2),
            "usage": {},
            "error": err,
            "mode": "agent",
            "meta": {"buyer_uid": buyer_uid},
        }
        out.write_text(json.dumps(rec, ensure_ascii=False, indent=1), encoding="utf-8")
        done += 1
        flag = "" if rec["ok"] else f"  ! {err[:60]}"
        print(f"[{done}/{len(pending)}] {model.split('/')[-1]} {sample['sample_id']} "
              f"{rec['latency']:.1f}s len={len(content)}{flag}")

    print(f"[done] {done} 轮，总耗时 {time.perf_counter() - t0:.0f}s")
    return 0


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--samples", default=str(OUT_DIR / "needs_kb.json"))
    ap.add_argument("--models", default=",".join(DEFAULT_MODELS))
    ap.add_argument("--limit", type=int, default=0)
    ap.add_argument("--timeout", type=float, default=180.0)
    ap.add_argument("--dry-run", action="store_true")
    ap.add_argument("--force", action="store_true", help="忽略已有结果重跑")
    args = ap.parse_args()
    return asyncio.run(run(args))


if __name__ == "__main__":
    raise SystemExit(main())
