"""量化 A′ 口径的信息缺口：金标准里的硬事实，有多少落在「实际注入的 top-3 话术 + 历史」里。

对比两种可见范围：
- 全库       = 知识库 143 条全文（假想的"模型能自己检索到任意条目"）
- A′ 实注入 = IDF top-3 参考话术 + 同会话近 30 条历史（实际发给模型的东西）
"""

from __future__ import annotations

import collections
import re
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from scripts.eval_model.common import (  # noqa: E402
    OUT_DIR, extract_facts, kb_fact_texts, load_kb, read_jsonl, retrieve_reference, ro_conn,
)

_PORT_RE = re.compile(r"端口号?[:：]?\s*(\d{4,5})")
_CRED_RE = re.compile(r"(?:密码|用户名)[:：]\s*(\S+)")


def gold_facts(gold: str) -> set[str]:
    facts = extract_facts(gold)
    need = set(facts["url"]) | set(facts["version"])
    need |= set(_PORT_RE.findall(gold))
    need |= set(_CRED_RE.findall(gold))
    return need


def main() -> int:
    kb = load_kb()
    kb_all = kb_fact_texts(kb)
    samples = read_jsonl(OUT_DIR / "dataset.jsonl")
    con = ro_conn()

    stat = collections.Counter()
    per_sample: list[tuple[str, int, int, int]] = []  # sid, 事实数, 全库命中, 实注入命中

    for s in samples:
        need = gold_facts(s["gold"])
        if not need:
            stat["no_fact_samples"] += 1
            continue
        stat["fact_samples"] += 1

        q = s["question"].replace("[买家发来图片]", "").strip()
        refs, _ = retrieve_reference(q, kb, k=3)
        refs_text = "\n".join(f"{r['name']} {r['content']}" for r in refs)

        rows = con.execute(
            "select content from chat_message_records where shop_id=? and buyer_uid=? and id<? "
            "and content is not null order by id desc limit 30",
            (s["shop_id"], s["buyer_uid"], s["id"]),
        ).fetchall()
        hist_text = "\n".join(r[0] for r in rows)

        hit_all = sum(1 for f in need if f in kb_all)
        hit_inject = sum(1 for f in need if f in refs_text or f in hist_text)
        stat["facts_total"] += len(need)
        stat["facts_in_kb"] += hit_all
        stat["facts_in_injected"] += hit_inject
        if hit_inject < len(need):
            stat["samples_with_gap"] += 1
        per_sample.append((s["sample_id"], len(need), hit_all, hit_inject))

    con.close()

    print(f"样本 {len(samples)}：含硬事实 {stat['fact_samples']}，无硬事实 {stat['no_fact_samples']}")
    print(f"硬事实总数 {stat['facts_total']}")
    print(f"  可用全库覆盖   : {stat['facts_in_kb']} ({stat['facts_in_kb'] / max(1, stat['facts_total']) * 100:.1f}%)")
    print(f"  实际注入覆盖   : {stat['facts_in_injected']} ({stat['facts_in_injected'] / max(1, stat['facts_total']) * 100:.1f}%)")
    print(f"存在缺口的样本 : {stat['samples_with_gap']} / {stat['fact_samples']}")
    gaps = [x for x in per_sample if x[3] < x[1]]
    print("\n缺口最大的样本：")
    for sid, n, all_hit, inj_hit in sorted(gaps, key=lambda x: x[3] - x[1])[:8]:
        print(f"  {sid}: 事实 {n}，全库命中 {all_hit}，实注入命中 {inj_hit}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
