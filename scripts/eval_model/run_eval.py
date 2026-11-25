"""对样本集批量跑推理。

用法：
    python scripts/eval_model/run_eval.py --models workbuddy/deepseek-v4.1-flash,workbuddy/glm-5.3-flash
    python scripts/eval_model/run_eval.py --dry-run --limit 3     # 只打印输入长度不发请求

产物：temp/eval/raw/<model>/<sample_id>.json（支持断点续跑）
"""

from __future__ import annotations

import argparse
import concurrent.futures as cf
import json
import sys
import threading
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from scripts.eval_model.common import (  # noqa: E402
    OUT_DIR, chat, est_tokens, load_config, load_kb, read_jsonl, system_prompt,
)
from scripts.eval_model.context_builder import (  # noqa: E402
    build_messages, load_material_snapshot,
)

DEFAULT_MODELS = [
    "workbuddy/deepseek-v4.1-flash",
    "workbuddy/glm-5.3-flash",
    "workbuddy/minimax-m3.1-flash",
    "workbuddy/hy4flash",
]

_lock = threading.Lock()


def safe_model(model: str) -> str:
    return model.replace("/", "__")


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--models", default=",".join(DEFAULT_MODELS))
    ap.add_argument("--dataset", default=str(OUT_DIR / "dataset.jsonl"))
    ap.add_argument("--limit", type=int, default=0, help="0=全部样本")
    ap.add_argument("--workers", type=int, default=3)
    ap.add_argument("--timeout", type=float, default=120.0)
    ap.add_argument("--with-material", action="store_true", help="注入客服素材库清单（需登录态）")
    ap.add_argument("--dry-run", action="store_true", help="只构造输入并打印长度，不发请求")
    args = ap.parse_args()

    cfg = load_config()
    system = system_prompt(cfg)
    kb = load_kb()
    samples = read_jsonl(Path(args.dataset))
    if args.limit:
        samples = samples[: args.limit]
    models = [m.strip() for m in args.models.split(",") if m.strip()]

    material = ""
    if args.with_material:
        first = samples[0]
        material = load_material_snapshot(first["shop_id"], str(first.get("user_id") or ""),
                                          OUT_DIR / "material_snapshot.json")

    print(f"[info] 样本 {len(samples)} | 模型 {len(models)} | system {est_tokens(system)} tok | "
          f"知识库 {len(kb)} 条 | 素材清单 {'有' if material else '无'}")

    prepared: list[tuple[dict, dict]] = []
    for s in samples:
        built = build_messages(s, system, kb, material)
        prepared.append((s, built))

    if args.dry_run:
        for s, b in prepared[:5]:
            m = b["meta"]
            print(f"  {s['sample_id']} hist={m['history_msgs']}msgs/{m['history_tokens']}tok "
                  f"refs={len(m['refs'])}(drop {m['refs_dropped_as_leak']}) imgs={len(m['images'])} "
                  f"total≈{m['input_tokens_est']}tok  Q={s['question'][:40]!r}")
        avg = sum(b["meta"]["input_tokens_est"] for _, b in prepared) / max(1, len(prepared))
        print(f"[dry-run] 平均输入 ≈{avg:.0f} tok，请求总数 {len(prepared) * len(models)}")
        return 0

    total = len(prepared) * len(models)
    done = 0
    t_start = time.perf_counter()

    def work(item: tuple[str, dict, dict]) -> tuple[str, str, dict]:
        model, sample, built = item
        out = OUT_DIR / "raw" / safe_model(model) / f"{sample['sample_id']}.json"
        if out.exists():
            try:
                cached = json.loads(out.read_text(encoding="utf-8"))
                if cached.get("ok"):
                    return model, sample["sample_id"], cached
            except Exception:
                pass
        res = chat(built["messages"], model, cfg, timeout=args.timeout)
        rec = {
            "sample_id": sample["sample_id"],
            "model": model,
            "gold": sample["gold"],
            "question": sample["question"],
            "tags": sample["tags"],
            "shop_id": sample["shop_id"],
            "buyer_hash": sample["buyer_hash"],
            "ok": res["ok"],
            "content": res["content"],
            "latency": res["latency"],
            "usage": res["usage"],
            "error": res["error"],
            "meta": {k: v for k, v in built["meta"].items() if k != "allowed_facts"},
        }
        out.parent.mkdir(parents=True, exist_ok=True)
        out.write_text(json.dumps(rec, ensure_ascii=False, indent=1), encoding="utf-8")
        return model, sample["sample_id"], rec

    tasks = [(m, s, b) for m in models for s, b in prepared]
    stats: dict[str, dict[str, int]] = {m: {"ok": 0, "fail": 0} for m in models}

    with cf.ThreadPoolExecutor(max_workers=args.workers) as pool:
        futures = {pool.submit(work, t): t for t in tasks}
        for fut in cf.as_completed(futures):
            model, sid, rec = fut.result()
            with _lock:
                done += 1
                key = "ok" if rec["ok"] else "fail"
                stats[model][key] += 1
                if done % 20 == 0 or done == total:
                    el = time.perf_counter() - t_start
                    eta = el / done * (total - done)
                    print(f"[{done}/{total}] {el:.0f}s 剩余≈{eta:.0f}s "
                          + " ".join(f"{m.split('/')[-1]}:{s['ok']}/{s['fail']}" for m, s in stats.items()))
                if not rec["ok"]:
                    print(f"  ! {model} {sid} {rec['error'][:120]}")

    print(f"[done] {done} 次请求，耗时 {time.perf_counter() - t_start:.0f}s")
    for m, s in stats.items():
        print(f"  {m}: ok={s['ok']} fail={s['fail']}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
