"""检查 LanceDB 数据"""
import lancedb
from pathlib import Path
import json

db_path = Path('data/vector_db/customer_knowledge.lance')
if db_path.exists():
    db = lancedb.connect(str(db_path.parent))
    table = db.open_table('customer_knowledge')
    df = table.to_pandas()
    
    print('=== LanceDB 数据 ===')
    print(f'总行数: {len(df)}')
    print(f'列: {list(df.columns)}')
    print()
    
    for idx, row in df.iterrows():
        print(f'--- 行 {idx} ---')
        print(f'ID: {row.get("id", "N/A")}')
        if 'payload' in row:
            try:
                payload = json.loads(row['payload'])
                content = payload.get('content', 'N/A')
                print(f'Payload content (前200字): {content[:200] if len(content) > 200 else content}')
                print(f'Payload meta_data: {payload.get("meta_data", {})}')
            except Exception as e:
                print(f'Payload 解析失败: {e}')
                print(f'Payload (raw 前100字): {str(row["payload"])[:100]}')
        print()
else:
    print('数据库不存在')
