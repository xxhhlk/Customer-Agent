"""检查 LanceDB 向量数据"""
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
        
        # 检查向量
        if 'vector' in row:
            vec = row['vector']
            if vec is not None:
                print(f'向量维度: {len(vec) if hasattr(vec, "__len__") else "N/A"}')
            else:
                print('向量: None')
        
        if 'payload' in row:
            try:
                payload = json.loads(row['payload'])
                print(f'Payload content: {payload.get("content", "N/A")[:50]}...')
            except:
                pass
        print()
else:
    print('数据库不存在')
