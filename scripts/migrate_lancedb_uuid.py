"""
LanceDB 数据迁移脚本 — 将旧版 md5 内容哈希 ID 升级为 UUID ID

使用方法：
    python scripts/migrate_lancedb_uuid.py

功能：
1. 备份现有 LanceDB 表数据
2. 为每条记录分配新 UUID 作为 id 列值
3. 同步更新 contents.db 中的 content_id 映射
4. 重写 LanceDB 表

迁移条件：仅当 id 列值为 md5 hash（32 位十六进制）时执行迁移。
已迁移的数据（UUID 格式）会自动跳过。
"""
import os
import sys
import json
import uuid
import shutil
from pathlib import Path
from datetime import datetime

# 添加项目根目录到 path
project_root = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(project_root))


def is_uuid(s: str) -> bool:
    """检查字符串是否为 UUID 格式"""
    try:
        uuid.UUID(s)
        return True
    except (ValueError, TypeError):
        return False


def is_md5_hash(s: str) -> bool:
    """检查字符串是否为 md5 hash 格式（32 位十六进制）"""
    if not isinstance(s, str) or len(s) != 32:
        return False
    try:
        int(s, 16)
        return True
    except ValueError:
        return False


def _find_data_paths():
    """自动发现 vector_db_path 和 contents_db_path。

    核心原则：vector_db 和 contents.db 必须在同一个父目录下配对使用，
    不能各自独立选择，否则会导致路径不一致。

    策略：
    1. 收集所有候选父目录（data/、temp/、config 配置路径的父目录）
    2. 对每个候选目录，检查 vector_db/customer_knowledge 表是否有数据
    3. 选择第一个 vector_db 有数据的目录；都没有数据时优先 data/
    """
    import lancedb as _lancedb
    import sqlite3 as _sqlite3

    config_path = project_root / "config.json"

    # 候选父目录列表（优先级从高到低）
    candidate_dirs = [
        project_root / "data",
        project_root / "temp",
    ]

    # 从 config.json 读取路径，提取其父目录加入候选
    if config_path.exists():
        try:
            with open(config_path, "r", encoding="utf-8") as f:
                config = json.load(f)
            kb_config = config.get("knowledge_base", {})
            cfg_vector = kb_config.get("vector_db_path", "")
            cfg_contents = kb_config.get("contents_db_path", "")

            for cfg_path in [cfg_vector, cfg_contents]:
                if not cfg_path:
                    continue
                p = Path(cfg_path)
                if not p.is_absolute():
                    p = project_root / p
                # vector_db_path 的父目录就是数据目录
                # contents_db_path 的父目录就是数据目录
                parent_dir = p.parent if p.name == "contents.db" else p.parent
                # vector_db_path 本身可能是目录（如 .../vector_db），取其父目录
                if p.name == "vector_db" or p.name.endswith("vector_db"):
                    parent_dir = p.parent
                if parent_dir not in candidate_dirs:
                    candidate_dirs.insert(0, parent_dir)
        except (json.JSONDecodeError, KeyError):
            pass

    # 去重
    seen = set()
    unique_dirs = []
    for d in candidate_dirs:
        key = str(d.resolve())
        if key not in seen:
            seen.add(key)
            unique_dirs.append(d)

    # 逐个检查候选目录，找到 vector_db 有数据的那个
    selected_dir = unique_dirs[0]  # 默认 data/
    for cand_dir in unique_dirs:
        vector_db = cand_dir / "vector_db"
        if not vector_db.exists():
            continue
        try:
            db = _lancedb.connect(str(vector_db))
            resp = db.list_tables()
            table_names = resp.tables if hasattr(resp, "tables") else resp
            if "customer_knowledge" in table_names:
                table = db.open_table("customer_knowledge")
                count = table.count_rows()
                if count > 0:
                    selected_dir = cand_dir
                    print(f"[INFO] 在 {vector_db} 中找到 {count} 条向量数据")
                    break
        except Exception:
            continue

    vector_db_path = selected_dir / "vector_db"
    contents_db_path = selected_dir / "contents.db"

    # 检查 contents.db 是否有数据（仅用于日志）
    if contents_db_path.exists() and contents_db_path.is_file():
        try:
            conn = _sqlite3.connect(str(contents_db_path))
            cursor = conn.cursor()
            cursor.execute("SELECT COUNT(*) FROM agno_knowledge")
            count = cursor.fetchone()[0]
            conn.close()
            if count > 0:
                print(f"[INFO] 在 {contents_db_path} 中找到 {count} 条元数据")
        except Exception:
            pass

    return vector_db_path, contents_db_path


def migrate_lancedb_to_uuid():
    """执行 LanceDB 数据迁移"""
    import lancedb

    vector_db_path, contents_db_path = _find_data_paths()
    table_name = "customer_knowledge"

    print(f"[INFO] 向量数据库路径: {vector_db_path}")
    print(f"[INFO] 元数据库路径: {contents_db_path}")

    if not vector_db_path.exists():
        print("[SKIP] 向量数据库目录不存在，无需迁移")
        return True

    # 连接 LanceDB
    db = lancedb.connect(str(vector_db_path))

    # 检查表是否存在
    try:
        table = db.open_table(table_name)
    except Exception:
        print(f"[SKIP] 表 {table_name} 不存在，无需迁移")
        return True

    # 读取所有数据
    df = table.to_pandas()
    total_rows = len(df)
    print(f"[INFO] 当前表中有 {total_rows} 条记录")

    if total_rows == 0:
        print("[SKIP] 表中没有数据，无需迁移")
        return True

    # 检查是否已迁移（所有 id 列已经是 UUID 格式）
    sample_ids = df['id'].astype(str).tolist()
    all_uuid = all(is_uuid(id_val) for id_val in sample_ids)
    if all_uuid:
        print(f"[SKIP] 数据已是 UUID 格式（示例: {sample_ids[0]}），无需迁移")
        return True

    # 检查是否有需要迁移的 md5 hash ID
    md5_count = sum(1 for id_val in sample_ids if is_md5_hash(id_val))
    uuid_count = sum(1 for id_val in sample_ids if is_uuid(id_val))
    other_count = total_rows - md5_count - uuid_count
    print(f"[INFO] ID 类型统计: md5 hash={md5_count}, UUID={uuid_count}, 其他={other_count}")

    if md5_count == 0 and uuid_count == total_rows:
        print("[SKIP] 没有需要迁移的 md5 hash ID")
        return True

    # 1. 备份旧数据
    backup_dir = vector_db_path.parent / f"vector_db_backup_{datetime.now().strftime('%Y%m%d_%H%M%S')}"
    print(f"[BACKUP] 备份向量数据库到: {backup_dir}")
    shutil.copytree(vector_db_path, backup_dir)

    # 2. 读取所有记录，分配新 UUID
    print("[MIGRATE] 开始迁移数据...")
    new_records = []
    id_mapping = {}  # 旧 ID -> 新 UUID

    for idx, row in df.iterrows():
        old_id = str(row['id'])

        # 如果已经是 UUID，保持不变
        if is_uuid(old_id):
            new_id = old_id
        else:
            new_id = str(uuid.uuid4())

        id_mapping[old_id] = new_id

        # 复制行数据，替换 id
        record = row.to_dict()
        record['id'] = new_id

        # 更新 payload 中的 content_id（使其与新 id 一致）
        if 'payload' in record and isinstance(record['payload'], str):
            try:
                payload = json.loads(record['payload'])
                # 仅当 content_id 等于旧 id 时才更新
                if payload.get('content_id') == old_id:
                    payload['content_id'] = new_id
                    record['payload'] = json.dumps(payload, ensure_ascii=False)
            except json.JSONDecodeError:
                print(f"[WARN] 记录 {idx} payload 解析失败，保留原值")

        new_records.append(record)

    # 3. 获取表 schema
    schema = table.schema

    # 4. 删除旧表，创建新表
    print("[MIGRATE] 删除旧表，创建新表...")
    try:
        db.drop_table(table_name)
    except Exception as e:
        print(f"[WARN] 删除旧表失败: {e}")

    # 创建新表（使用相同 schema）
    new_table = db.create_table(name=table_name, schema=schema, mode="overwrite")

    # 5. 插入迁移后的数据
    print(f"[MIGRATE] 插入 {len(new_records)} 条迁移后的记录...")
    new_table.add(new_records)

    # 6. 同步更新 contents.db（如果存在）
    if contents_db_path.exists() and contents_db_path.is_file():
        print("[MIGRATE] 更新 contents.db 中的 content_id 映射...")
        import sqlite3
        conn = sqlite3.connect(str(contents_db_path))
        cursor = conn.cursor()

        # 查看表结构
        cursor.execute("SELECT name FROM sqlite_master WHERE type='table'")
        tables = [row[0] for row in cursor.fetchall()]
        print(f"[INFO] contents.db 中的表: {tables}")

        # 更新 knowledge_contents 表中的 id
        if 'knowledge_contents' in tables:
            updated = 0
            for old_id, new_id in id_mapping.items():
                if old_id == new_id:
                    continue  # 跳过未变更的
                cursor.execute(
                    "UPDATE knowledge_contents SET id = ? WHERE id = ?",
                    (new_id, old_id)
                )
                updated += cursor.rowcount
            conn.commit()
            print(f"[MIGRATE] 更新了 {updated} 条 contents.db 记录")
        else:
            print(f"[INFO] contents.db 中没有 knowledge_contents 表，跳过")

        conn.close()
    else:
        print("[INFO] contents.db 不存在，跳过")

    # 7. 验证迁移结果
    new_df = new_table.to_pandas()
    print(f"[VERIFY] 迁移后表中有 {len(new_df)} 条记录")

    # 验证所有 ID 都是 UUID 格式
    all_uuid = True
    for idx, row in new_df.iterrows():
        if not is_uuid(str(row['id'])):
            all_uuid = False
            print(f"[ERROR] 记录 {idx} 的 ID 不是 UUID 格式: {row['id']}")
            break

    if all_uuid:
        print("[SUCCESS] 所有 ID 已成功迁移为 UUID 格式")
        print(f"[INFO] 旧数据备份在: {backup_dir}")

        # 保存 ID 映射关系
        mapping_path = backup_dir / "id_mapping.json"
        with open(mapping_path, "w", encoding="utf-8") as f:
            json.dump(id_mapping, f, ensure_ascii=False, indent=2)
        print(f"[INFO] ID 映射关系已保存到: {mapping_path}")
    else:
        print("[ERROR] 迁移失败，部分 ID 不是 UUID 格式")
        print(f"[INFO] 可从备份恢复: {backup_dir}")
        return False

    return True


if __name__ == "__main__":
    print("=" * 60)
    print("LanceDB UUID 迁移脚本")
    print("=" * 60)
    print()

    try:
        success = migrate_lancedb_to_uuid()
        if success:
            print()
            print("[DONE] 迁移完成！")
        else:
            print()
            print("[FAILED] 迁移失败，请检查错误信息")
            sys.exit(1)
    except Exception as e:
        print(f"[ERROR] 迁移过程中发生异常: {e}")
        import traceback
        traceback.print_exc()
        sys.exit(1)
