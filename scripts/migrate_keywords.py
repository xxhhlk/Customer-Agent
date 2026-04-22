"""
关键词表迁移脚本 - 添加分组、回复、转人工、优先级字段
"""
import sqlite3
import os
from datetime import datetime

def migrate_keywords():
    """迁移关键词表结构"""
    db_path = 'temp/channel_shop.db'
    
    # 检查数据库文件是否存在
    if not os.path.exists(db_path):
        print(f"数据库文件不存在: {db_path}")
        return False
    
    conn = sqlite3.connect(db_path)
    cursor = conn.cursor()
    
    try:
        # 检查新列是否已存在
        cursor.execute("PRAGMA table_info(keywords)")
        columns = [col[1] for col in cursor.fetchall()]
        
        if 'group_name' in columns:
            print("关键词表已是新版本，无需迁移。")
            return True
        
        print("开始迁移关键词表...")
        
        # 备份旧表
        cursor.execute("ALTER TABLE keywords RENAME TO keywords_old")
        
        # 创建新表
        cursor.execute('''
            CREATE TABLE keywords (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                keyword VARCHAR(100) NOT NULL,
                group_name VARCHAR(100) NOT NULL DEFAULT 'default',
                reply_content TEXT,
                transfer_to_human INTEGER DEFAULT 0,
                priority INTEGER DEFAULT 0,
                created_at DATETIME DEFAULT CURRENT_TIMESTAMP,
                updated_at DATETIME DEFAULT CURRENT_TIMESTAMP
            )
        ''')
        
        # 迁移旧数据
        cursor.execute('''
            INSERT INTO keywords (id, keyword, group_name, reply_content, transfer_to_human, priority, created_at, updated_at)
            SELECT id, keyword, 'default', NULL, 0, 0, datetime('now'), datetime('now')
            FROM keywords_old
        ''')
        
        # 删除旧表
        cursor.execute("DROP TABLE keywords_old")
        
        conn.commit()
        print("关键词表迁移成功！")
        return True
        
    except Exception as e:
        conn.rollback()
        print(f"迁移失败: {e}")
        return False
    finally:
        conn.close()

if __name__ == "__main__":
    migrate_keywords()
