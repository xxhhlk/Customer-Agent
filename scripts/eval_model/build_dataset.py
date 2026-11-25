"""构建评测样本集：人工客服回复作为金标准。

用法：
    python scripts/eval_model/build_dataset.py --limit 200 --seed 42

产物：temp/eval/dataset.jsonl + temp/eval/dataset_stats.json
"""

from __future__ import annotations

import argparse
import collections
import json
import random
import re
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from scripts.eval_model.common import (  # noqa: E402
    OUT_DIR, buyer_hash, clean_buyer_text, load_config, ro_conn, write_jsonl,
)

MIN_GOLD_LEN = 8
MAX_BUYER_TURNS = 3
DUP_GOLD_KEEP = 1          # 同一回复文本出现 >=3 次视作固定话术，只留 1 条
DUP_GOLD_THRESHOLD = 3

TAG_RULES: list[tuple[str, re.Pattern]] = [
    ("license", re.compile(r"机器码|授权|开通|激活|绑定设备")),
    ("history_img", re.compile(r"历史影像|旧版|老版|安装包|下载地址|更新包")),
    ("trouble", re.compile(r"不行|失败|报错|登不上|登不了|打不开|连不上|没反应|错误|超时|未完成审核|不显示|黑屏")),
    ("price", re.compile(r"多少钱|价格|贵|便宜|优惠|几块|收费")),
    ("howto", re.compile(r"怎么|怎样|如何|教程|步骤|在哪|哪里")),
    ("capability", re.compile(r"能不能|可以吗|支持|有没有|是否|可不可以")),
    ("what", re.compile(r"是什么|什么|哪个|区别|什么意思|什么版本")),
]


def tag_sample(question: str, context_types: list[str]) -> list[str]:
    tags: list[str] = []
    if "image" in context_types:
        tags.append("with_image")
    if "video" in context_types:
        tags.append("with_video")
    if not question.strip():
        tags.append("media_only")
    for name, pat in TAG_RULES:
        if pat.search(question):
            tags.append(name)
            break
    if not tags or all(t in ("with_image", "with_video", "media_only") for t in tags):
        tags.append("other")
    return tags


def primary_tag(tags: list[str]) -> str:
    for t in tags:
        if t not in ("with_image", "with_video", "media_only"):
            return t
    return tags[0] if tags else "other"


def is_pure_url(text: str) -> bool:
    return bool(re.fullmatch(r"https?://\S+", (text or "").strip()))


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--limit", type=int, default=200)
    ap.add_argument("--seed", type=int, default=42)
    ap.add_argument("--out", default=str(OUT_DIR / "dataset.jsonl"))
    args = ap.parse_args()

    cfg = load_config()
    fallbacks = {str(x).strip() for x in (cfg.get("rate_limit") or {}).get("fallback_reply") or []}

    con = ro_conn()
    cols = ("id", "shop_id", "buyer_uid", "from_role", "content", "direction",
            "reply_source", "context_type", "media_meta", "timestamp")
    rows = con.execute(
        f"select {','.join(cols)} from chat_message_records "
        "where content is not null and content != '' order by shop_id, buyer_uid, timestamp"
    ).fetchall()
    con.close()

    sessions: dict[tuple, list[dict]] = collections.defaultdict(list)
    for r in rows:
        d = dict(zip(cols, r))
        sessions[(d["shop_id"], d["buyer_uid"])].append(d)

    stats = collections.Counter()
    cands: list[dict] = []
    gold_counter: collections.Counter = collections.Counter()

    # 第一遍：配对 + 基础过滤
    for (shop_id, buyer_uid), seq in sessions.items():
        staff_so_far: list[dict] = []
        for i, msg in enumerate(seq):
            if msg["reply_source"] != "staff":
                continue
            prev_staff = [{"content": s["content"], "ts": str(s["timestamp"])}
                          for s in staff_so_far[-20:]]
            staff_so_far.append(msg)
            gold = (msg["content"] or "").strip()
            stats["staff_total"] += 1

            buyer: list[dict] = []
            j = i - 1
            while j >= 0 and seq[j]["direction"] == "inbound" and len(buyer) < MAX_BUYER_TURNS:
                buyer.append(seq[j])
                j -= 1
            buyer.reverse()

            if not buyer:
                stats["drop_no_buyer_msg"] += 1
                continue
            if len(gold) < MIN_GOLD_LEN:
                stats["drop_short"] += 1
                continue
            if is_pure_url(gold):
                stats["drop_pure_url"] += 1
                continue
            if gold in fallbacks:
                stats["drop_fallback"] += 1
                continue

            q_parts: list[str] = []
            for b in buyer:
                ctype = str(b["context_type"] or "")
                txt = clean_buyer_text(b["content"])
                if ctype == "image" or (not txt and is_pure_url(b["content"])):
                    q_parts.append("[买家发来图片]")
                elif ctype == "video":
                    q_parts.append("[买家发来视频]")
                elif txt:
                    q_parts.append(txt)
            q = "\n".join(dict.fromkeys(q_parts))
            ctx_types = [str(b["context_type"] or "") for b in buyer]
            if not q and not any(t in ("image", "video") for t in ctx_types):
                stats["drop_empty_question"] += 1
                continue

            cands.append({
                "id": msg["id"],
                "shop_id": shop_id,
                "buyer_uid": str(buyer_uid),
                "buyer_hash": buyer_hash(buyer_uid),
                "ts": str(msg["timestamp"]),
                "gold": gold,
                "question": q,
                "buyer_msgs": [{
                    "id": b["id"],
                    "content": b["content"],
                    "context_type": b["context_type"],
                    "media_meta": b["media_meta"],
                } for b in buyer],
                "prev_staff": prev_staff,
                "tags": tag_sample(q, ctx_types),
            })
            gold_counter[re.sub(r"\s+", "", gold)] += 1

    # 第二遍：去重固定话术（同文本出现 >=3 次只留最早 1 条）
    seen: collections.Counter = collections.Counter()
    deduped: list[dict] = []
    for c in sorted(cands, key=lambda x: x["ts"]):
        key = re.sub(r"\s+", "", c["gold"])
        if gold_counter[key] >= DUP_GOLD_THRESHOLD:
            seen[key] += 1
            if seen[key] > DUP_GOLD_KEEP:
                stats["drop_duplicate_template"] += 1
                continue
        deduped.append(c)

    stats["candidates"] = len(deduped)

    # 分层抽样：按主标签，每层保底 8 条，余量按层内规模比例分配
    layers: dict[str, list[dict]] = collections.defaultdict(list)
    for c in deduped:
        layers[primary_tag(c["tags"])].append(c)

    rng = random.Random(args.seed)
    total = sum(len(v) for v in layers.values())
    limit = min(args.limit, total)

    # 分层：每层先保底 8 条，剩余池随机补足到 limit（小类不被大类的比例吃掉）
    pools: dict[str, list[dict]] = {
        name: rng.sample(items, len(items)) for name, items in layers.items()
    }
    picked: list[dict] = []
    overflow: list[dict] = []
    for name, pool in pools.items():
        picked.extend(pool[:8])
        overflow.extend(pool[8:])
    rng.shuffle(overflow)
    if len(picked) < limit:
        picked.extend(overflow[: limit - len(picked)])
    picked.sort(key=lambda x: x["ts"])
    picked = picked[:limit]
    for idx, p in enumerate(picked):
        p["sample_id"] = f"S{idx + 1:03d}"

    n = write_jsonl(Path(args.out), picked)
    tag_dist = collections.Counter(primary_tag(p["tags"]) for p in picked)
    summary = {
        "stats": dict(stats),
        "written": n,
        "tag_distribution": dict(tag_dist),
        "with_image": sum(1 for p in picked if "with_image" in p["tags"]),
        "with_video": sum(1 for p in picked if "with_video" in p["tags"]),
        "sessions": len(sessions),
        "seed": args.seed,
        "limit": args.limit,
    }
    out_stats = Path(args.out).with_name("dataset_stats.json")
    out_stats.write_text(json.dumps(summary, ensure_ascii=False, indent=2), encoding="utf-8")

    print(json.dumps(summary, ensure_ascii=False, indent=2))
    print(f"[ok] 写出 {n} 条 -> {args.out}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
