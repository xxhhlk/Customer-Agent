"""
关键词分组整理脚本
将 default 分组中的关键词分配到对应的分组中
"""
import sqlite3

db_path = "database/channel_shop.db"
conn = sqlite3.connect(db_path)
cursor = conn.cursor()

# 定义关键词到分组的映射规则
keyword_mapping = {
    "转人工": ["转人工", "人工客服", "人工", "客服", "真人", "工单"],
    "问候语": ["你好", "您好", "在吗", "在不在", "有人吗", "哈喽", "hello", "hi"],
    "收到确认": ["收到", "好的", "知道了", "明白了", "ok", "OK"],
    "感谢回复": ["好评", "谢谢", "感谢"],
    "催发货": ["发货", "催", "什么时候发货"],
    "查物流": ["物流", "快递", "到哪了", "什么时候到"],
    "奥维": ["奥维"],
    "不能用": ["不能用", "无法", "失败", "错误"],
    "闪退": ["闪退", "崩溃", "退出"],
    "可以用": ["可以用", "能用", "好了"],
    "历史影像": ["历史影像", "历史"],
}

# 获取分组ID
cursor.execute("SELECT id, group_name FROM keyword_groups")
groups = {name: gid for gid, name in cursor.fetchall()}

print("开始整理关键词...")
print("=" * 60)

# 统计
moved_count = 0
not_moved = []

# 获取 default 分组中的所有关键词
cursor.execute("SELECT id, keyword FROM keywords WHERE group_id = 12")
keywords = cursor.fetchall()

for keyword_id, keyword in keywords:
    moved = False
    
    # 检查关键词应该属于哪个分组
    for group_name, keywords_list in keyword_mapping.items():
        for kw_pattern in keywords_list:
            if kw_pattern in keyword:
                # 找到匹配的分组
                target_group_id = groups.get(group_name)
                if target_group_id:
                    # 更新关键词的分组
                    cursor.execute(
                        "UPDATE keywords SET group_id = ? WHERE id = ?",
                        (target_group_id, keyword_id)
                    )
                    print(f"[OK] '{keyword}' -> {group_name}")
                    moved_count += 1
                    moved = True
                    break
        if moved:
            break
    
    if not moved:
        not_moved.append(keyword)

# 提交更改
conn.commit()

print("\n" + "=" * 60)
print(f"整理完成！")
print(f"  已移动: {moved_count} 个关键词")
print(f"  未移动: {len(not_moved)} 个关键词（保留在 default 分组）")

if not_moved:
    print("\n未移动的关键词（前20个）:")
    for kw in not_moved[:20]:
        print(f"  - {kw}")

# 验证结果
print("\n" + "=" * 60)
print("验证分组关键词数量:")
print("=" * 60)
cursor.execute("""
    SELECT g.group_name, COUNT(k.id) as keyword_count
    FROM keyword_groups g
    LEFT JOIN keywords k ON g.id = k.group_id
    GROUP BY g.id
    ORDER BY g.id
""")
for row in cursor.fetchall():
    print(f"  {row[0]:20s}: {row[1]:3d} 个关键词")

conn.close()
