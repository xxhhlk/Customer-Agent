"""
数据库迁移脚本：为 keywords 表添加 match_type 字段
执行方式：python scripts/migrate_add_match_type.py
"""

import sys
import os

# 添加项目根目录到 Python 路径
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from sqlalchemy import text
from database.db_manager import db_manager
from utils.logger_loguru import get_logger


def migrate_add_match_type():
    """为 keywords 表添加 match_type 字段"""
    logger = get_logger("migration")
    
    try:
        session = db_manager.get_session()
        
        # 检查字段是否已存在
        check_sql = text("PRAGMA table_info(keywords)")
        result = session.execute(check_sql)
        columns = [row[1] for row in result.fetchall()]
        
        if 'match_type' in columns:
            logger.info("match_type 字段已存在，无需迁移")
            session.close()
            return True
        
        logger.info("开始添加 match_type 字段...")
        
        # SQLite 不支持 ALTER TABLE ADD COLUMN with DEFAULT
        # 需要分步操作
        # 1. 添加字段（允许为空）
        add_column_sql = text("ALTER TABLE keywords ADD COLUMN match_type VARCHAR(20)")
        session.execute(add_column_sql)
        session.commit()
        
        logger.info("match_type 字段添加成功")
        
        # 2. 为所有现有记录设置默认值
        update_sql = text("UPDATE keywords SET match_type = 'partial' WHERE match_type IS NULL")
        result = session.execute(update_sql)
        session.commit()
        
        logger.info(f"已为 {result.rowcount} 条关键词记录设置默认匹配类型为 'partial'")
        
        session.close()
        return True
        
    except Exception as e:
        logger.error(f"迁移失败: {e}")
        return False


if __name__ == "__main__":
    logger = get_logger("migration")
    logger.info("=" * 50)
    logger.info("开始数据库迁移：添加 match_type 字段")
    logger.info("=" * 50)
    
    success = migrate_add_match_type()
    
    if success:
        logger.info("✅ 迁移成功完成！")
    else:
        logger.error("❌ 迁移失败！")
        sys.exit(1)
