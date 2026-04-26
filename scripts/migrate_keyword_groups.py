"""
关键词分组数据迁移脚本

将旧版关键词数据（Keyword 表包含 reply_content、transfer_to_human 等冗余字段）
迁移到新版结构（KeywordGroup 统一管理回复和转人工设置）

运行方式:
    python scripts/migrate_keyword_groups.py
"""

import sys
import os
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from sqlalchemy import create_engine, text, inspect
from sqlalchemy.orm import sessionmaker
from database.models import Base, KeywordGroup
from utils.logger_loguru import get_logger

logger = get_logger("migrate")


def check_need_migration(db_path: str = './temp/channel_shop.db') -> bool:
    """检查是否需要进行迁移
    
    检测 keywords 表是否包含旧字段（group_name, reply_content, transfer_to_human）
    """
    engine = create_engine(f'sqlite:///{db_path}')
    inspector = inspect(engine)

    if not inspector.has_table('keywords'):
        return False

    columns = [c['name'] for c in inspector.get_columns('keywords')]
    old_columns = {'group_name', 'reply_content', 'transfer_to_human'}

    return bool(old_columns & set(columns))


def migrate(db_path: str = './temp/channel_shop.db'):
    """执行数据迁移"""
    logger.info(f"开始迁移数据库: {db_path}")

    engine = create_engine(f'sqlite:///{db_path}')
    Session = sessionmaker(bind=engine)

    # 1. 读取旧数据
    with engine.connect() as conn:
        result = conn.execute(text("""
            SELECT id, keyword, group_name, match_type, reply_content,
                   transfer_to_human, priority
            FROM keywords
        """))
        old_keywords = [
            {
                'id': row[0],
                'keyword': row[1],
                'group_name': row[2] or 'default',
                'match_type': row[3] or 'partial',
                'reply_content': row[4],
                'transfer_to_human': bool(row[5]),
                'priority': row[6] or 0
            }
            for row in result.fetchall()
        ]

    logger.info(f"读取到 {len(old_keywords)} 个旧关键词")

    if not old_keywords:
        logger.info("没有旧数据需要迁移")
        return

    # 2. 按 group_name 分组聚合
    groups_map = {}
    for kw in old_keywords:
        gn = kw['group_name']
        if gn not in groups_map:
            groups_map[gn] = {
                'reply_content': kw['reply_content'],
                'transfer_to_human': kw['transfer_to_human'],
                'priority': kw['priority']
            }
        else:
            # 如果同一分组有不同设置，取优先级最高的
            if kw['priority'] > groups_map[gn]['priority']:
                groups_map[gn]['reply_content'] = kw['reply_content']
                groups_map[gn]['transfer_to_human'] = kw['transfer_to_human']
                groups_map[gn]['priority'] = kw['priority']

    # 3. 创建 keyword_groups 表（如果不存在）
    Base.metadata.create_all(engine, tables=[Base.metadata.tables['keyword_groups']])

    # 3.5 确保 keyword_groups 表有 priority 列（旧表可能缺少）
    inspector = inspect(engine)
    if inspector.has_table('keyword_groups'):
        columns = [c['name'] for c in inspector.get_columns('keyword_groups')]
        if 'priority' not in columns:
            logger.info("keyword_groups 表缺少 priority 列，正在添加...")
            with engine.connect() as conn:
                conn.execute(text("ALTER TABLE keyword_groups ADD COLUMN priority INTEGER DEFAULT 0"))
                conn.commit()
            logger.info("priority 列添加成功")

    # 4. 创建分组并记录 ID 映射
    session = Session()
    group_name_to_id = {}

    try:
        for group_name, group_data in groups_map.items():
            existing = session.query(KeywordGroup).filter(
                KeywordGroup.group_name == group_name
            ).first()

            if existing:
                group_name_to_id[group_name] = existing.id
                logger.info(f"分组已存在: {group_name} (id={existing.id})")
            else:
                group = KeywordGroup(
                    group_name=group_name,
                    reply=group_data['reply_content'],
                    is_transfer=1 if group_data['transfer_to_human'] else 0,
                    pass_to_ai=0,
                    priority=group_data['priority']
                )
                session.add(group)
                session.flush()
                group_name_to_id[group_name] = group.id
                logger.info(f"创建分组: {group_name} (id={group.id})")

        session.commit()
    except Exception as e:
        session.rollback()
        logger.error(f"创建分组失败: {e}")
        raise
    finally:
        session.close()

    # 5. 重建 keywords 表（去掉冗余列）
    with engine.connect() as conn:
        conn.execute(text("""
            CREATE TABLE keywords_new (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                keyword VARCHAR(100) NOT NULL UNIQUE,
                group_id INTEGER NOT NULL,
                match_type VARCHAR(20) NOT NULL DEFAULT 'partial',
                created_at DATETIME DEFAULT CURRENT_TIMESTAMP,
                updated_at DATETIME DEFAULT CURRENT_TIMESTAMP,
                FOREIGN KEY (group_id) REFERENCES keyword_groups (id)
            )
        """))

        # 6. 迁移数据到新表
        for kw in old_keywords:
            group_id = group_name_to_id.get(kw['group_name'], 0)
            if group_id == 0:
                logger.warning(f"关键词 '{kw['keyword']}' 找不到分组映射，跳过")
                continue

            conn.execute(text("""
                INSERT INTO keywords_new (id, keyword, group_id, match_type)
                VALUES (:id, :keyword, :group_id, :match_type)
            """), {
                'id': kw['id'],
                'keyword': kw['keyword'],
                'group_id': group_id,
                'match_type': kw['match_type']
            })

        conn.execute(text("DROP TABLE keywords"))
        conn.execute(text("ALTER TABLE keywords_new RENAME TO keywords"))
        conn.commit()

    logger.info("数据迁移完成！")

    # 7. 验证
    with engine.connect() as conn:
        group_count = conn.execute(text("SELECT COUNT(*) FROM keyword_groups")).scalar()
        keyword_count = conn.execute(text("SELECT COUNT(*) FROM keywords")).scalar()
        logger.info(f"迁移后: {group_count} 个分组, {keyword_count} 个关键词")


def auto_migrate(db_path: str = './temp/channel_shop.db'):
    """自动检测并执行迁移"""
    if check_need_migration(db_path):
        logger.info("检测到旧版数据库结构，开始自动迁移...")
        migrate(db_path)
        return True
    else:
        logger.info("数据库结构已是最新，无需迁移")
        return False


if __name__ == "__main__":
    # 手动运行迁移
    success = auto_migrate()
    sys.exit(0 if success else 0)
