"""汇总评测结果，生成 report.md / report.csv / review.csv。

用法：
    python scripts/eval_model/report.py
    python scripts/eval_model/report.py --baseline workbuddy/deepseek-v4.1-flash
"""

from __future__ import annotations

import argparse
import csv
import json
import math
import re
import sys
from collections import Counter, defaultdict
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from scripts.eval_model.common import OUT_DIR, read_jsonl  # noqa: E402

TAG_LABEL = {
    "other": "未分类",
    "howto": "操作方法",
    "history_img": "历史影像",
    "license": "机器码授权",
    "trouble": "故障排查",
    "capability": "功能咨询",
    "what": "概念澄清",
    "price": "价格咨询",
}

_SECRET_RE = re.compile(r"(密码[:：]\s*)(\S+)|(用户名[:：]\s*)(\S+)|(服务地址[:：]\s*)(\S+)")


def redact(text: str, keep: int = 2) -> str:
    """报告展示用：账号/密码/地址打码，避免泄露到聊天或文档。"""
    def sub(m: re.Match) -> str:
        for i in (1, 3, 5):
            if m.group(i):
                val = m.group(i + 1) or ""
                return m.group(i) + (val[:keep] + "***" if len(val) > keep else "***")
        return m.group(0)
    return _SECRET_RE.sub(sub, text or "")


def mcnemar_exact(b: int, c: int) -> float:
    """配对二项精确检验（b/c 为两模型胜负样本数）。"""
    n = b + c
    if n == 0:
        return 1.0
    k = min(b, c)
    p = sum(math.comb(n, i) for i in range(0, k + 1)) / (2 ** n) * 2
    return min(1.0, p)


def rate(xs: list[bool]) -> float:
    return sum(xs) / len(xs) if xs else 0.0


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--baseline", default="workbuddy/deepseek-v4.1-flash")
    args = ap.parse_args()

    rows = read_jsonl(OUT_DIR / "judged_all.jsonl")
    if not rows:
        print("[err] 没有 judged_all.jsonl，先跑 judge.py")
        return 1
    raw_rows = read_jsonl(OUT_DIR / "dataset.jsonl")
    ds = {s["sample_id"]: s for s in raw_rows}

    by_model: dict[str, list[dict]] = defaultdict(list)
    for r in rows:
        by_model[r["model"]].append(r)
    models = sorted(by_model, key=lambda m: -len(by_model[m]))

    # 样本总数（用于缺失模型的对齐）
    total_samples = len({r["sample_id"] for r in rows})

    summary: dict[str, dict] = {}
    for m, items in by_model.items():
        scored = [r for r in items if isinstance(r.get("score"), int)]
        fatal_rules = [bool(set((r.get("rules") or {}).get("flags") or [])
                            & {"hallucination", "banned_word"}) for r in items]
        fatal_judge = [any(j.get("fatal") for j in (r.get("judges") or [])) for r in items]
        summary[m] = {
            "n": len(items),
            "scored": len(scored),
            "strict": rate([r["score"] == 3 for r in scored]),
            "usable": rate([r["score"] >= 2 for r in scored]),
            "zero": rate([r["score"] == 0 for r in scored]),
            "fatal_rules": rate(fatal_rules),
            "fatal_judge": rate(fatal_judge),
            "empty": rate([not (r.get("content") or "").strip() for r in items]),
            "latency": round(sum((r.get("latency") or 0) for r in items) / max(1, len(items)), 2),
            "judge_fail": rate([r["score"] is None for r in items]),
            "disagree": rate([bool(r.get("disagree")) for r in items]),
        }

    # 分场景
    tag_stats: dict[str, dict[str, dict]] = {}
    for m, items in by_model.items():
        buckets: dict[str, list[dict]] = defaultdict(list)
        for r in items:
            tags = [t for t in (r.get("tags") or []) if t in TAG_LABEL]
            buckets[tags[0] if tags else "other"].append(r)
        tag_stats[m] = {}
        for tag, rs in buckets.items():
            sc = [r for r in rs if isinstance(r.get("score"), int)]
            tag_stats[m][tag] = {
                "n": len(rs),
                "strict": rate([r["score"] == 3 for r in sc]),
                "usable": rate([r["score"] >= 2 for r in sc]),
                "fatal": rate([bool(set((r.get("rules") or {}).get("flags") or [])
                                    & {"hallucination", "banned_word"}) for r in rs]),
            }

    # 带图 vs 纯文本
    modal: dict[str, dict] = {}
    for m, items in by_model.items():
        img = [r for r in items if "with_image" in (r.get("tags") or [])]
        txt = [r for r in items if "with_image" not in (r.get("tags") or [])]
        modal[m] = {
            "img_n": len(img),
            "img_strict": rate([r["score"] == 3 for r in img if isinstance(r.get("score"), int)]),
            "txt_n": len(txt),
            "txt_strict": rate([r["score"] == 3 for r in txt if isinstance(r.get("score"), int)]),
        }

    # 可解性：金标准里的硬事实（地址/端口/账号/版本）是否出现在模型可见的
    # （参考话术 ∪ 会话历史）里。不可解样本所有模型必错，单独统计避免拉低结论。
    from scripts.eval_model.common import extract_facts, load_kb, kb_fact_texts, ro_conn  # noqa: PLC0415

    kb_text = kb_fact_texts(load_kb())
    con = ro_conn()
    solvability: dict[str, bool] = {}
    for sid, s in ds.items():
        rows = con.execute(
            "select content from chat_message_records where shop_id=? and buyer_uid=? and id<? "
            "and content is not null order by id desc limit 30",
            (s["shop_id"], s["buyer_uid"], s["id"]),
        ).fetchall()
        visible = kb_text + "\n" + "\n".join(r[0] for r in rows)
        facts = extract_facts(s["gold"])
        need = set(facts["url"]) | set(facts["version"])
        need |= set(re.findall(r"端口号?[:：]?\s*(\d{4,5})", s["gold"]))
        need |= set(re.findall(r"(?:密码|用户名)[:：]\s*(\S+)", s["gold"]))
        solvability[sid] = all(f in visible for f in need) if need else True
    con.close()

    solv_stats: dict[str, dict] = {}
    for m, items in by_model.items():
        solv = [r for r in items if solvability.get(r["sample_id"], True)]
        hard = [r for r in items if not solvability.get(r["sample_id"], True)]
        solv_stats[m] = {
            "solv_n": len(solv),
            "solv_strict": rate([r["score"] == 3 for r in solv if isinstance(r.get("score"), int)]),
            "unsolv_n": len(hard),
            "unsolv_strict": rate([r["score"] == 3 for r in hard if isinstance(r.get("score"), int)]),
        }

    # 与基准的配对比较
    base = args.baseline if args.baseline in by_model else models[0]
    base_map = {r["sample_id"]: r for r in by_model[base]}
    pairing: dict[str, dict] = {}
    for m, items in by_model.items():
        if m == base:
            continue
        win = loss = tie2 = 0
        for r in items:
            b = base_map.get(r["sample_id"])
            if not b or not isinstance(r.get("score"), int) or not isinstance(b.get("score"), int):
                continue
            if r["score"] > b["score"]:
                win += 1
            elif r["score"] < b["score"]:
                loss += 1
            else:
                tie2 += 1
        pairing[m] = {"win": win, "loss": loss, "tie": tie2,
                      "p": round(mcnemar_exact(win, loss), 3) if (win + loss) else 1.0}

    # CSV
    csv_path = OUT_DIR / "report.csv"
    with csv_path.open("w", encoding="utf-8-sig", newline="") as fh:
        w = csv.writer(fh)
        w.writerow(["model", "n", "scored", "strict_acc", "usable_acc", "zero_rate",
                    "fatal_rules", "fatal_judge", "empty_rate", "judge_fail", "avg_latency_s"])
        for m, s in summary.items():
            w.writerow([m, s["n"], s["scored"], f"{s['strict']:.3f}", f"{s['usable']:.3f}",
                        f"{s['zero']:.3f}", f"{s['fatal_rules']:.3f}", f"{s['fatal_judge']:.3f}",
                        f"{s['empty']:.3f}", f"{s['judge_fail']:.3f}", s["latency"]])

    # 人工抽检队列：致命 + 分歧 + 0/1 分 + 少量随机高分
    review: list[dict] = []
    for m, items in by_model.items():
        pri = sorted(items, key=lambda r: (
            0 if r.get("fatal") else 1,
            0 if isinstance(r.get("score"), int) and r["score"] <= 1 else 1,
            0 if r.get("disagree") else 1,
            r["sample_id"],
        ))
        review.extend(pri[:8])
    review_path = OUT_DIR / "review.csv"
    with review_path.open("w", encoding="utf-8-sig", newline="") as fh:
        w = csv.writer(fh)
        w.writerow(["model", "sample_id", "score", "fatal", "flags", "question",
                    "gold", "candidate", "judge_reasons"])
        for r in review:
            w.writerow([
                r["model"], r["sample_id"], r.get("score"), r.get("fatal"),
                ";".join((r.get("rules") or {}).get("flags") or []),
                redact(r.get("question") or ""), redact(r.get("gold") or ""),
                redact(r.get("content") or ""),
                " | ".join(f"{j['model'].split('/')[-1]}:{j['score']}:{j['reason']}"
                           for j in r.get("judges") or []),
            ])

    # Markdown
    lines: list[str] = []
    lines.append("# 客服模型回复正确率评测\n")
    lines.append(f"- 样本：{total_samples} 条（人工客服回复作金标准，分层抽样 seed=42）")
    lines.append(f"- 口径：A′ 固定 system prompt + 会话历史 + 知识库参考话术，单次生成，无工具调用")
    lines.append(f"- 裁判：{' + '.join(['workbuddy/kimi-k3.1-pro', 'workbuddy/mimo-v2.6-pro'])}，取中位数；分差 ≥2 由第三人裁决")
    lines.append(f"- 基准：`{base}`（同一样本配对比较）\n")

    lines.append("## 总览\n")
    lines.append("| 模型 | 样本 | 严格正确率(3分) | 可用率(≥2分) | 0分率 | 硬规则致命 | 裁判判致命 | 裁判分歧 | 平均耗时 | 空回复 |")
    lines.append("|---|---|---|---|---|---|---|---|---|---|")
    for m, s in summary.items():
        star = " ★" if m == base else ""
        lines.append(
            f"| `{m}`{star} | {s['scored']} | {s['strict'] * 100:.1f}% | {s['usable'] * 100:.1f}% | "
            f"{s['zero'] * 100:.1f}% | {s['fatal_rules'] * 100:.1f}% | {s['fatal_judge'] * 100:.1f}% | "
            f"{s['disagree'] * 100:.0f}% | {s['latency']:.1f}s | {s['empty'] * 100:.1f}% |"
        )

    lines.append("\n## 分场景严格正确率\n")
    tags = sorted({t for m in tag_stats for t in tag_stats[m]}, key=lambda t: -sum(
        tag_stats[m].get(t, {}).get("n", 0) for m in tag_stats))
    lines.append("| 场景 | " + " | ".join(m.split("/")[-1] for m in models) + " |")
    lines.append("|---|" + "---|" * len(models))
    for t in tags:
        cells = []
        for m in models:
            d = tag_stats[m].get(t)
            cells.append(f"{d['strict'] * 100:.0f}% (n={d['n']})" if d else "-")
        lines.append(f"| {TAG_LABEL.get(t, t)} | " + " | ".join(cells) + " |")

    lines.append("\n## 带图样本 vs 纯文本\n")
    lines.append("| 模型 | 带图严格正确率 | 纯文本严格正确率 |")
    lines.append("|---|---|---|")
    for m in models:
        d = modal[m]
        lines.append(f"| `{m.split('/')[-1]}` | {d['img_strict'] * 100:.0f}% (n={d['img_n']}) | "
                     f"{d['txt_strict'] * 100:.0f}% (n={d['txt_n']}) |")

    lines.append(f"\n## 可解性拆分\n")
    any_m = models[0]
    lines.append(f"金标准含地址/端口/账号/版本等硬事实、且这些事实在「参考话术 ∪ 会话历史」中查不到的样本，"
                 f"任何模型都无法答对。本批共 {solv_stats[any_m]['unsolv_n']} 条（"
                 f"{solv_stats[any_m]['unsolv_n'] / max(1, total_samples) * 100:.0f}%）。\n")
    lines.append("| 模型 | 可解样本 严格正确率 | 不可解样本 严格正确率 |")
    lines.append("|---|---|---|")
    for m in models:
        d = solv_stats[m]
        lines.append(f"| `{m.split('/')[-1]}` | {d['solv_strict'] * 100:.1f}% (n={d['solv_n']}) | "
                     f"{d['unsolv_strict'] * 100:.1f}% (n={d['unsolv_n']}) |")

    lines.append(f"\n## 与基准 `{base}` 的配对比较\n")
    lines.append("| 模型 | 胜 | 负 | 平 | McNemar p |")
    lines.append("|---|---|---|---|---|")
    for m, d in pairing.items():
        lines.append(f"| `{m.split('/')[-1]}` | {d['win']} | {d['loss']} | {d['tie']} | {d['p']} |")

    # 典型失败
    lines.append("\n## 典型失败样本\n")
    shown = 0
    for m, items in by_model.items():
        for r in items:
            if shown >= 6:
                break
            if isinstance(r.get("score"), int) and r["score"] <= 1:
                reasons = "；".join(j["reason"] for j in r.get("judges") or [] if j.get("reason"))
                hard = set((r.get("rules") or {}).get("flags") or []) & {"hallucination", "banned_word"}
                tag = f", 硬规则:{'/'.join(sorted(hard))}" if hard else ""
                lines.append(f"- **{m.split('/')[-1]} / {r['sample_id']}** (score={r['score']}{tag})")
                lines.append(f"  - 问：{redact((r.get('question') or '')[:70])}")
                lines.append(f"  - 人工：{redact((r.get('gold') or '')[:80])}")
                lines.append(f"  - 候选：{redact((r.get('content') or '')[:80])}")
                lines.append(f"  - 判定：{reasons[:120]}")
                shown += 1
    if shown == 0:
        lines.append("- 无")

    lines.append("\n## 口径与限制\n")
    lines.append("1. 金标准是人工客服实际回复，本身不保证最优；报告另出「命中金标准」而非「绝对正确」。")
    lines.append("2. 素材库清单未注入（需登录态拉取），带图样本只传图不传视频。")
    lines.append("3. 订单信息与实时知识库检索未复现，模型只能看到静态参考话术。")
    lines.append("4. 参考话术已过滤与金标准相似度 ≥0.6 的条目，避免答案泄漏；检索无显著命中时不注入。")
    lines.append("5. 裁判为第三方模型，与参评模型不同族；人工抽检见 `review.csv`。")

    md_path = OUT_DIR / "report.md"
    md_path.write_text("\n".join(lines), encoding="utf-8")
    print(f"[ok] {md_path}")
    print(f"[ok] {csv_path}")
    print(f"[ok] {review_path}")
    print("\n".join(lines[:24]))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
