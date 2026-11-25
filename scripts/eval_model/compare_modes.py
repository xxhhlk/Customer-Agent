"""对照 A′（静态注入）与 B（真 Agent + 检索）在「需查库」样本上的差异。

用法：
    python -m scripts.eval_model.compare_modes
产物：temp/eval/compare_modes.md / .csv
"""

from __future__ import annotations

import argparse
import csv
import math
import sys
from collections import defaultdict
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from scripts.eval_model.common import OUT_DIR, read_jsonl  # noqa: E402


def rate(xs: list[bool]) -> float:
    return sum(xs) / len(xs) if xs else 0.0


def mcnemar(b: int, c: int) -> float:
    n = b + c
    if n == 0:
        return 1.0
    k = min(b, c)
    return min(1.0, sum(math.comb(n, i) for i in range(k + 1)) / (2 ** n) * 2)


def index(rows: list[dict]) -> dict[tuple[str, str], dict]:
    return {(r["model"], r["sample_id"]): r for r in rows if isinstance(r.get("score"), int)}


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--static", default=str(OUT_DIR / "judged_all.jsonl"))
    ap.add_argument("--agent", default=str(OUT_DIR / "judged_agent_all.jsonl"))
    args = ap.parse_args()

    static = index(read_jsonl(Path(args.static)))
    agent = index(read_jsonl(Path(args.agent)))
    if not agent:
        print("[err] 没有 B 口径结果，先跑 run_eval_agent + judge --raw-dir temp/eval/raw_agent")
        return 1

    models = sorted({m for m, _ in agent})
    samples = sorted({sid for _, sid in agent})
    print(f"[info] B 口径样本 {len(samples)} 条，模型 {len(models)}")

    table: dict[str, dict] = {}
    for m in models:
        pairs = [(static.get((m, s)), agent.get((m, s))) for s in samples]
        pairs = [(a, b) for a, b in pairs if a and b]
        a_scores = [a["score"] for a, _ in pairs]
        b_scores = [b["score"] for _, b in pairs]
        win = sum(1 for a, b in pairs if b["score"] > a["score"])
        loss = sum(1 for a, b in pairs if b["score"] < a["score"])
        table[m] = {
            "n": len(pairs),
            "a_strict": rate([s == 3 for s in a_scores]),
            "b_strict": rate([s == 3 for s in b_scores]),
            "a_usable": rate([s >= 2 for s in a_scores]),
            "b_usable": rate([s >= 2 for s in b_scores]),
            "win": win,
            "loss": loss,
            "tie": len(pairs) - win - loss,
            "p": mcnemar(win, loss) if (win + loss) else 1.0,
            "a_lat": 0.0,
            "b_lat": 0.0,
        }

    # 延迟从 raw 目录取（judged 里保留了 latency）
    for m in models:
        lat_a = [static.get((m, s), {}).get("latency") or 0 for s in samples]
        lat_b = [agent.get((m, s), {}).get("latency") or 0 for s in samples]
        table[m]["a_lat"] = sum(lat_a) / max(1, len(lat_a))
        table[m]["b_lat"] = sum(lat_b) / max(1, len(lat_b))

    out_csv = OUT_DIR / "compare_modes.csv"
    with out_csv.open("w", encoding="utf-8-sig", newline="") as fh:
        w = csv.writer(fh)
        w.writerow(["model", "n", "A_strict", "B_strict", "delta", "A_usable", "B_usable",
                    "B_win", "A_win", "tie", "mcnemar_p", "A_latency_s", "B_latency_s"])
        for m, d in table.items():
            w.writerow([m, d["n"], f"{d['a_strict']:.3f}", f"{d['b_strict']:.3f}",
                        f"{(d['b_strict'] - d['a_strict']) * 100:+.1f}pt",
                        f"{d['a_usable']:.3f}", f"{d['b_usable']:.3f}",
                        d["win"], d["loss"], d["tie"], f"{d['p']:.3f}",
                        f"{d['a_lat']:.1f}", f"{d['b_lat']:.1f}"])

    lines = ["# A′（静态注入）vs B（真 Agent + 检索）对照\n",
             f"- 样本：{len(samples)} 条「人工回复与知识库话术相似度 ≥0.4」的需查库样本",
             "- A′：单次生成 + IDF top-3 参考话术；B：`CustomerAgent.async_reply()`，真检索 + 工具\n",
             "| 模型 | n | A′ 严格 | B 严格 | 差值 | B 胜 | A 胜 | McNemar p | A′ 耗时 | B 耗时 |",
             "|---|---|---|---|---|---|---|---|---|---|"]
    for m, d in table.items():
        lines.append(
            f"| `{m.split('/')[-1]}` | {d['n']} | {d['a_strict'] * 100:.1f}% | {d['b_strict'] * 100:.1f}% | "
            f"{(d['b_strict'] - d['a_strict']) * 100:+.1f}pt | {d['win']} | {d['loss']} | {d['p']:.3f} | "
            f"{d['a_lat']:.1f}s | {d['b_lat']:.1f}s |")
    lines.append("\n> 差值正数表示真 Agent 比静态注入更准；p<0.05 才算显著。")

    out_md = OUT_DIR / "compare_modes.md"
    out_md.write_text("\n".join(lines), encoding="utf-8")
    print(f"[ok] {out_md}")
    print(f"[ok] {out_csv}")
    print("\n".join(lines[5:]))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
