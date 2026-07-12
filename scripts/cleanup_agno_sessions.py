"""
清理 agno_sessions 表中膨胀的 runs 字段。

根因：agno 的 upsert_run 只 append 不删除，导致 runs JSON 无限增长。
当前 2 个 session 的 runs 字段分别为 37MB 和 24MB（共 61MB）。
每次 arun 都要 json.loads + pydantic 反序列化全部历史，导致阻塞和崩溃。

此脚本将每个 session 的 runs 截断为最近 50 条，并 VACUUM 压缩数据库。
"""
import sqlite3
import json
import sys
import os
from pathlib import Path

DB_PATH = Path(__file__).parent.parent / "database" / "channel_shop.db"
MAX_RUNS = 15


def main():
    if not DB_PATH.exists():
        print(f"ERROR: 数据库不存在: {DB_PATH}")
        sys.exit(1)

    # 备份
    backup_path = DB_PATH.with_suffix(".db.bak")
    print(f"备份: {DB_PATH} -> {backup_path}")
    import shutil
    shutil.copy2(DB_PATH, backup_path)

    conn = sqlite3.connect(str(DB_PATH))
    c = conn.cursor()

    # 检查 agno_sessions 表
    c.execute("SELECT COUNT(*) FROM agno_sessions")
    total = c.fetchone()[0]
    print(f"agno_sessions 总行数: {total}")

    # 检查每行 runs 大小
    c.execute("SELECT session_id, LENGTH(runs) FROM agno_sessions ORDER BY LENGTH(runs) DESC")
    rows = c.fetchall()
    for sid, runs_len in rows:
        print(f"  session={sid[:50]} runs={runs_len:,} bytes")

    # 截断每个 session 的 runs
    truncated = 0
    for sid, _ in rows:
        c.execute("SELECT runs FROM agno_sessions WHERE session_id = ?", (sid,))
        row = c.fetchone()
        if not row or not row[0]:
            continue

        runs_raw = row[0]
        # runs 在 agno 中是双重 JSON 编码：存的是 json.dumps(list) 的字符串
        # deserialize_session_json_fields 会 json.loads 一次得到字符串，
        # 然后 AgentSession.from_dict 再 json.loads 一次得到列表
        try:
            runs_str = json.loads(runs_raw)  # 第一次解析：得到字符串
            if isinstance(runs_str, str):
                runs_list = json.loads(runs_str)  # 第二次解析：得到列表
            elif isinstance(runs_str, list):
                runs_list = runs_str
            else:
                print(f"  跳过 {sid}: runs 类型异常 {type(runs_str)}")
                continue
        except (json.JSONDecodeError, TypeError) as e:
            print(f"  跳过 {sid}: JSON 解析失败 {e}")
            continue

        if not isinstance(runs_list, list):
            print(f"  跳过 {sid}: runs 不是列表 {type(runs_list)}")
            continue

        original_count = len(runs_list)
        if original_count <= MAX_RUNS:
            print(f"  跳过 {sid}: runs 数量 {original_count} <= {MAX_RUNS}")
            continue

        # 截断为最近 MAX_RUNS 条
        truncated_list = runs_list[-MAX_RUNS:]
        # 重新编码：与 agno 存储格式一致（双重 JSON）
        new_runs_str = json.dumps(truncated_list)
        new_runs_raw = json.dumps(new_runs_str)

        c.execute(
            "UPDATE agno_sessions SET runs = ? WHERE session_id = ?",
            (new_runs_raw, sid),
        )
        truncated += 1
        print(
            f"  截断 {sid}: {original_count} -> {MAX_RUNS} 条, "
            f"{len(runs_raw):,} -> {len(new_runs_raw):,} bytes"
        )

    conn.commit()
    print(f"\n截断了 {truncated} 个 session")

    # VACUUM 压缩数据库
    print("VACUUM 压缩数据库...")
    conn.execute("VACUUM")
    conn.close()

    # 检查结果
    new_size = DB_PATH.stat().st_size
    old_size = backup_path.stat().st_size
    print(f"\n数据库大小: {old_size:,} -> {new_size:,} bytes")
    print(f"节省: {(old_size - new_size):,} bytes ({(old_size - new_size) / old_size * 100:.1f}%)")
    print(f"\n备份位于: {backup_path}")
    print("如确认无问题，可手动删除备份。")


if __name__ == "__main__":
    main()
