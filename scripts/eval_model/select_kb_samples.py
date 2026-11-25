"""挑出「必须查知识库才能答对」的样本，供 B 口径（真 Agent）专项复测。

判据：金标准与知识库某条话术的 2-gram Jaccard 相似度 >= 阈值，
说明人工回复本质是在抄/改写话术库，模型不检索就没法给出等价答案。

用法：
    python -m scripts.eval_model.select_kb_samples --threshold 0.4
产物：temp/eval/needs_kb.json（字段与 dataset.jsonl 一致）
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from scripts.eval_model.common import OUT_DIR, _ngrams, load_kb, read_jsonl, write_jsonl  # noqa: E402


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--threshold", type=float, default=0.4)
    ap.add_argument("--dataset", default=str(OUT_DIR / "dataset.jsonl"))
    ap.add_argument("--out", default=str(OUT_DIR / "needs_kb.json"))
    args = ap.parse_args()

    kb = load_kb()
    kb_grams = [(k["name"], _ngrams(k["content"])) for k in kb]
    picked: list[dict] = []
    for s in read_jsonl(Path(args.dataset)):
        g = _ngrams(s["gold"])
        best, name = 0.0, ""
        for kb_name, d in kb_grams:
            if not d or not g:
                continue
            sim = len(d & g) / len(d | g)
            if sim > best:
                best, name = sim, kb_name
        if best >= args.threshold:
            s["kb_similarity"] = round(best, 3)
            s["kb_match"] = name
            picked.append(s)

    n = write_jsonl(Path(args.out), picked)
    print(f"[ok] {n} 条（阈值 {args.threshold}）-> {args.out}")
    for s in picked[:10]:
        print(f"  {s['sample_id']} sim={s['kb_similarity']} match={s['kb_match'][:24]} "
              f"Q={s['question'][:36]!r}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
