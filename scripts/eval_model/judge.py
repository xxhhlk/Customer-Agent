"""对推理结果打分：硬规则 + 双裁判 LLM + 分歧裁决。

用法：
    python scripts/eval_model/judge.py --models workbuddy/glm-5.3-flash
    python scripts/eval_model/judge.py --limit 5          # 先小批验证

产物：temp/eval/judged/<model>/<sample_id>.json、temp/eval/judged_all.jsonl
"""

from __future__ import annotations

import argparse
import concurrent.futures as cf
import json
import re
import sys
import threading
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from scripts.eval_model.common import (  # noqa: E402
    OUT_DIR, chat, est_tokens, extract_facts, kb_fact_texts, load_config, load_kb, read_jsonl,
)

# 裁判刻意避开参评模型族（qoder/* 当前网关 502 排队，实测 workbuddy/* 稳定）
JUDGES = ["workbuddy/kimi-k3.1-pro", "workbuddy/mimo-v2.6-pro"]
TIEBREAKER = "workbuddy/step-5-preview"
MAX_CONTEXT_MSGS = 6

JUDGE_PROMPT = """你是电商客服质检员。给出「买家问题」「人工客服实际回复」「候选回复」，判断候选回复能否正确解决同一个问题。

评分标准（只按信息是否等价，不按措辞好坏）：
3 = 与人工回复信息等价：关键结论一致，必要的事实（地址/端口/版本/操作步骤/价格政策）准确无误，买家照做能解决
2 = 方向正确但缺少关键信息：结论对，但漏掉买家必须知道的地址/步骤/限制，买家照做可能仍卡住
1 = 部分相关：答到一点，但主要诉求没解决，或表述含糊无法执行
0 = 错误：答非所问、结论相反、编造事实（不存在的地址/端口/版本/政策）、违反平台规范

判定要点：
- 人工回复是该问题的参考答案，但只看「能否解决买家诉求」，不要要求候选逐字接近
- 候选出现人工回复和参考资料里都没有的网址、域名、端口、账号、版本号 → 视为编造，fatal=true
- 候选出现「永久/终身/长期有效」「微信/QQ/支付宝」等平台违规词 → 视为违规，fatal=true
- 候选是敷衍话术（如「我帮您问下技术人员」）而人工回复给了实质答案 → 最多 1 分
- 买家只发图片/机器码时，按人工回复的实际处理方式判断候选是否做了同类处理

只输出 JSON，不要解释：
{"score": 0-3, "fatal": true/false, "reason": "20字内理由", "missing": ["漏掉的关键信息"], "wrong": ["错误或编造的点"]}"""


def hard_rules(cand: str, gold: str, cfg: dict, allowed: set[str]) -> dict:
    """零成本硬规则：编造事实 / 禁用词 / 兜底话术 / 空回复 / 过长 / markdown。"""
    flags: list[str] = []
    banned_hits: list[str] = []
    fabricated: list[str] = []

    for w in cfg.get("banned_words") or []:
        w = str(w or "").strip()
        if w and len(w) >= 2 and w in cand and w not in ("禁用词", "这条不要发给客户"):
            banned_hits.append(w)
    if banned_hits:
        flags.append("banned_word")

    facts = extract_facts(cand)
    for kind in ("url", "domain", "version"):
        for f in facts[kind]:
            if f not in allowed:
                fabricated.append(f"{kind}:{f}")
    if fabricated:
        flags.append("hallucination")

    fallbacks = {str(x).strip() for x in (cfg.get("rate_limit") or {}).get("fallback_reply") or []}
    if cand.strip() in fallbacks:
        flags.append("fallback")
    if not cand.strip():
        flags.append("empty")
    if len(cand) > max(300, len(gold) * 3):
        flags.append("too_long")
    if re.search(r"\*\*|^#{1,3}\s|^\s*[-*]\s", cand, re.M):
        flags.append("markdown")

    return {
        "flags": flags,
        "banned_hits": banned_hits,
        "fabricated": fabricated,
    }


def build_judge_messages(rec: dict, samples: dict[str, dict]) -> list[dict]:
    s = samples.get(rec["sample_id"]) or {}
    staff = (s.get("prev_staff") or [])[-MAX_CONTEXT_MSGS:]
    ctx = "\n".join(f"客服: {x['content'][:120]}" for x in staff)
    payload = (
        f"【买家问题】\n{rec.get('question') or ''}\n\n"
        + (f"【此前对话（节选）】\n{ctx}\n\n" if ctx else "")
        + f"【人工客服实际回复】\n{rec.get('gold') or ''}\n\n"
        + f"【候选回复】\n{rec.get('content') or ''}"
    )
    return [
        {"role": "system", "content": JUDGE_PROMPT},
        {"role": "user", "content": payload},
    ]


def parse_json(text: str) -> dict:
    m = re.search(r"\{.*\}", text or "", re.S)
    if not m:
        return {}
    try:
        return json.loads(m.group(0))
    except Exception:
        return {}


def judge_one(rec: dict, samples: dict[str, dict], cfg: dict, allowed: set[str]) -> dict:
    rules = hard_rules(rec.get("content") or "", rec.get("gold") or "", cfg, allowed)
    msgs = build_judge_messages(rec, samples)

    verdicts: list[dict] = []
    for model in JUDGES:
        res = chat(msgs, model, cfg, timeout=90)
        v = parse_json(res.get("content") or "")
        verdicts.append({
            "model": model,
            "ok": bool(v),
            "score": int(v.get("score")) if str(v.get("score", "")).isdigit() else None,
            "fatal": bool(v.get("fatal")) if v else None,
            "reason": str(v.get("reason") or res.get("error") or "")[:80],
            "missing": v.get("missing") or [],
            "wrong": v.get("wrong") or [],
        })

    scores = [v["score"] for v in verdicts if isinstance(v["score"], int)]
    consensus = None
    tie = None
    if len(scores) == 2 and abs(scores[0] - scores[1]) >= 2:
        res = chat(msgs, TIEBREAKER, cfg, timeout=90)
        v = parse_json(res.get("content") or "")
        tie = {
            "model": TIEBREAKER,
            "score": int(v.get("score")) if str(v.get("score", "")).isdigit() else None,
            "reason": str(v.get("reason") or "")[:80],
        }
        if isinstance(tie["score"], int):
            scores.append(tie["score"])
    if scores:
        # 取中位数，避免单一裁判跑偏
        scores_sorted = sorted(scores)
        consensus = scores_sorted[len(scores_sorted) // 2]

    fatal = bool(set(rules["flags"]) & {"hallucination", "banned_word"}) or any(v["fatal"] for v in verdicts)
    return {
        "sample_id": rec["sample_id"],
        "model": rec["model"],
        "tags": rec.get("tags") or [],
        "latency": rec.get("latency"),
        "gold": rec.get("gold"),
        "content": rec.get("content"),
        "question": rec.get("question"),
        "buyer_hash": rec.get("buyer_hash"),
        "rules": rules,
        "judges": verdicts,
        "tiebreak": tie,
        "score": consensus,
        "fatal": fatal,
        "disagree": bool(len(scores) >= 2 and max(scores) - min(scores) >= 1),
        "error": "" if rec.get("ok") else rec.get("error", ""),
    }


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--models", default="", help="逗号分隔；空=raw 下全部")
    ap.add_argument("--limit", type=int, default=0)
    ap.add_argument("--workers", type=int, default=4)
    ap.add_argument("--raw-dir", default="", help="默认 temp/eval/raw；B 口径传 temp/eval/raw_agent")
    ap.add_argument("--judged-dir", default="", help="默认与 raw 目录同级的 judged")
    ap.add_argument("--out", default="", help="默认 temp/eval/judged_all.jsonl")
    args = ap.parse_args()

    raw_root = Path(args.raw_dir) if args.raw_dir else OUT_DIR / "raw"
    judged_root = Path(args.judged_dir) if args.judged_dir else raw_root.parent / (
        "judged_agent" if raw_root.name == "raw_agent" else "judged")
    out_path = Path(args.out) if args.out else OUT_DIR / (
        "judged_agent_all.jsonl" if raw_root.name == "raw_agent" else "judged_all.jsonl")

    cfg = load_config()
    kb = load_kb()
    allowed = set()
    for kind in ("url", "domain", "version"):
        allowed |= extract_facts(kb_fact_texts(kb))[kind]

    samples = {s["sample_id"]: s for s in read_jsonl(OUT_DIR / "dataset.jsonl")}
    models = [m.strip() for m in args.models.split(",") if m.strip()] or \
             sorted(p.name for p in raw_root.iterdir() if p.is_dir())

    jobs: list[tuple[str, dict]] = []
    for m in models:
        recs = read_jsonl(raw_root / m / "all.jsonl") if (raw_root / m / "all.jsonl").exists() else None
        if recs is None:
            recs = []
            for p in sorted((raw_root / m).glob("*.json")):
                try:
                    recs.append(json.loads(p.read_text(encoding="utf-8")))
                except Exception:
                    continue
        for rec in recs:
            rec.setdefault("model", m)
            if not rec.get("ok"):
                continue
            jobs.append((m, rec))
    if args.limit:
        jobs = jobs[: args.limit]

    print(f"[info] 待判 {len(jobs)} 条 | 裁判 {JUDGES} | 白名单事实 {len(allowed)} 条")
    done = 0
    t0 = time.perf_counter()
    out_root = judged_root

    def work(item: tuple[str, dict]) -> dict:
        model, rec = item
        out = out_root / model.replace("/", "__") / f"{rec['sample_id']}.json"
        if out.exists():
            try:
                return json.loads(out.read_text(encoding="utf-8"))
            except Exception:
                pass
        res = judge_one(rec, samples, cfg, allowed)
        out.parent.mkdir(parents=True, exist_ok=True)
        out.write_text(json.dumps(res, ensure_ascii=False, indent=1), encoding="utf-8")
        return res

    results: list[dict] = []
    with cf.ThreadPoolExecutor(max_workers=args.workers) as pool:
        for fut in cf.as_completed([pool.submit(work, j) for j in jobs]):
            results.append(fut.result())
            done += 1
            if done % 20 == 0 or done == len(jobs):
                el = time.perf_counter() - t0
                print(f"[{done}/{len(jobs)}] {el:.0f}s 剩余≈{el / done * (len(jobs) - done):.0f}s")

    all_path = out_path
    with all_path.open("w", encoding="utf-8") as fh:
        for r in sorted(results, key=lambda x: (x["model"], x["sample_id"])):
            fh.write(json.dumps(r, ensure_ascii=False) + "\n")
    print(f"[done] 写出 {len(results)} 条 -> {all_path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
