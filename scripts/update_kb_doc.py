"""修改知识库语录正文并重算向量（单条）。

背景（2026-11-27）：历史影像账号轮换、语录正文调整后，必须同时更新
LanceDB 里的 **正文** 和 **向量**——只改正文不重算向量，检索仍按旧向量走，
等于没改；而直接用 lancedb 命令行改会漏掉 ``content_hash``（= sha256(content)）
导致导出/去重不一致。

知识库 UI 只有导入/导出，没有单条编辑，所以账号轮换这类改动走这个脚本。

用法：
    uv run python scripts/update_kb_doc.py --name "历史影像账号" --list
    uv run python scripts/update_kb_doc.py --name "历史影像账号" \\
        --replace "用户名 / 账号：wdrdzy 密码：ccQhBhb0" \\
        --replace-with "用户名 / 账号：newuser 密码：newpass"
    uv run python scripts/update_kb_doc.py --name "历史影像账号" --content-file new.txt

不带 --replace / --content-file 时只打印当前正文，不写库。
写库前会自动备份 data/vector_db 到 temp/backup/。
"""

from __future__ import annotations

import argparse
import asyncio
import hashlib
import json
import shutil
import sys
import time
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

import lancedb  # noqa: E402


def _get_table():
    from config import Config

    cfg = Config()
    db_path = cfg.get("knowledge_base.vector_db_path", "")
    if not db_path:
        db_path = str(REPO_ROOT / "data" / "vector_db")
    db = lancedb.connect(db_path)
    return db.open_table("customer_knowledge"), cfg


def _backup(vector_db_path: Path, tag: str) -> Path:
    backup_root = REPO_ROOT / "temp" / "backup"
    backup_root.mkdir(parents=True, exist_ok=True)
    stamp = time.strftime("%Y%m%d_%H%M%S")
    target = backup_root / f"vector_db_before_{tag}_{stamp}"
    shutil.copytree(vector_db_path, target)
    return target


async def _embed(text: str, cfg):
    from Agent.CustomerAgent.volcengine_embedder import VolcengineEmbedder

    embedder = VolcengineEmbedder(
        id=cfg.get("embedder.model_name", "doubao-embedding-vision-251215"),
        dimensions=2048,
        api_key=cfg.get("embedder.api_key", ""),
        base_url=f"{cfg.get('embedder.api_base', '').rstrip('/')}/embeddings/multimodal",
    )
    return await embedder.async_get_embedding_and_usage(text)


def main() -> int:
    parser = argparse.ArgumentParser(description="修改知识库语录正文并重算向量")
    parser.add_argument("--name", required=True, help="语录标题（精确匹配）")
    parser.add_argument("--replace", help="要被替换的旧文本")
    parser.add_argument("--replace-with", help="替换后的新文本")
    parser.add_argument("--content-file", help="用文件内容整体替换正文")
    parser.add_argument("--list", action="store_true", help="只打印当前正文")
    args = parser.parse_args()

    table, cfg = _get_table()
    df = table.to_pandas()
    hits = [i for i, p in enumerate(df["payload"]) if json.loads(p).get("name") == args.name]
    if not hits:
        print(f"未找到标题为「{args.name}」的语录")
        return 1
    if len(hits) > 1:
        print(f"标题「{args.name}」有 {len(hits)} 条，请先改名使其唯一")
        return 1

    idx = hits[0]
    row_id = df.iloc[idx]["id"]
    payload = json.loads(df.iloc[idx]["payload"])
    old_content = payload.get("content", "")

    if args.list or (not args.replace and not args.content_file):
        print(f"id={row_id}\n{old_content}")
        return 0

    if args.content_file:
        new_content = Path(args.content_file).read_text(encoding="utf-8").strip()
    else:
        if not args.replace_with:
            print("--replace 必须配合 --replace-with 使用")
            return 1
        if args.replace not in old_content:
            print(f"正文中找不到待替换文本：{args.replace!r}")
            return 1
        new_content = old_content.replace(args.replace, args.replace_with, 1)

    if new_content == old_content:
        print("正文没有变化，未写库")
        return 0

    vector_db_path = Path(cfg.get("knowledge_base.vector_db_path", "") or
                          (REPO_ROOT / "data" / "vector_db"))
    backup = _backup(vector_db_path, args.name[:12])
    print(f"已备份：{backup}")

    vector, usage = asyncio.run(_embed(new_content, cfg))
    payload["content"] = new_content
    payload["content_hash"] = hashlib.sha256(new_content.encode("utf-8")).hexdigest()
    if isinstance(usage, dict):
        payload["usage"] = usage

    result = table.update(
        where=f"id = '{row_id}'",
        values={"vector": list(vector), "payload": json.dumps(payload, ensure_ascii=False)},
    )
    print(f"已更新：{result}")

    check = table.to_pandas()
    new_payload = json.loads(check[check["id"] == row_id].iloc[0]["payload"])
    print(f"新正文首行：{new_payload['content'].splitlines()[0]}")
    print(f"content_hash 一致："
          f"{new_payload['content_hash'] == hashlib.sha256(new_payload['content'].encode('utf-8')).hexdigest()}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
