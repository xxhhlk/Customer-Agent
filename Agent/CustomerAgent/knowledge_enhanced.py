"""
知识库增强模块 - 基于继承的扩展

功能：
1. 进度追踪：实时反馈导入进度
2. 可中断操作：支持取消长时间运行的导入
3. 完全兼容：保持与 agno 的数据格式和接口兼容

作者：Claude AI
日期：2025-12-25
"""

import asyncio
from enum import Enum
from typing import Callable, Optional, List, Any, Dict, Protocol, TYPE_CHECKING, cast
from loguru import logger

# 导入 agno 基类
from agno.vectordb.lancedb import LanceDb
from agno.knowledge.knowledge import Knowledge
from agno.knowledge.document import Document

if TYPE_CHECKING:
    from lancedb.table import Table
    from lancedb.db import LanceTable


# ==============================================================================
# 1. 基础类型定义
# ==============================================================================

class ImportStage(Enum):
    """导入阶段枚举"""
    READING = "reading"        # 读取文件
    CHUNKING = "chunking"      # 文档分块
    EMBEDDING = "embedding"    # 生成向量
    SAVING = "saving"          # 保存到数据库


class ProgressCallback(Protocol):
    """进度回调协议"""
    def __call__(
        self,
        stage: ImportStage,
        current: int,
        total: int,
        message: str,
        metadata: Optional[Dict[str, Any]] = None
    ) -> None: ...


class CancelToken:
    """取消令牌 - 用于中断长时间运行的操作"""

    def __init__(self):
        self._cancelled = False

    def cancel(self):
        """请求取消操作"""
        logger.info("收到取消请求")
        self._cancelled = True

    def is_cancelled(self) -> bool:
        """检查是否已取消"""
        return self._cancelled

    def reset(self):
        """重置令牌（可复用）"""
        self._cancelled = False


# ==============================================================================
# 2. 增强的 LanceDB
# ==============================================================================

class LanceDbWithProgress(LanceDb):
    """
    增强的 LanceDB - 添加进度追踪和取消支持

    继承自 agno.vectordb.lancedb.LanceDb
    保持完全的数据格式兼容性
    """

    # 声明动态属性的类型
    progress_callback: Optional[ProgressCallback]
    cancel_token: Optional[CancelToken]
    on_bad_vectors: Optional[str]
    fill_value: Optional[float]
    table: Optional["LanceTable"]

    def __init__(
        self,
        *args,
        progress_callback: Optional[ProgressCallback] = None,
        cancel_token: Optional[CancelToken] = None,
        **kwargs
    ):
        """
        初始化

        参数：
            progress_callback: 进度回调函数
            cancel_token: 取消令牌
        """
        super().__init__(*args, **kwargs)
        self.progress_callback = progress_callback
        self.cancel_token = cancel_token

    def insert(
        self,
        content_hash: str,
        documents: List[Document],
        filters: Optional[Dict[str, Any]] = None
    ) -> None:
        """
        重写同步 insert 方法，添加进度报告和取消支持
        """
        logger.debug(f"[LanceDbWithProgress] insert() 被调用，文档数: {len(documents)}, 进度回调: {self.progress_callback is not None}")

        if len(documents) <= 0:
            logger.info("没有文档需要插入")
            return

        # 报告开始嵌入
        if self.progress_callback:
            logger.info(f"开始向量化 {len(documents)} 个文档")
            self.progress_callback(
                ImportStage.EMBEDDING,
                0,
                len(documents),
                f"开始向量化 {len(documents)} 个文档"
            )

        # 处理每个文档
        processed_count = 0
        for idx, document in enumerate(documents):
            # 检查取消
            if self.cancel_token and self.cancel_token.is_cancelled:
                logger.info(f"插入已取消，已完成 {processed_count}/{len(documents)} 个文档")
                break

            # 检查文档是否已存在
            doc_exists_method = getattr(self, 'doc_exists', None)
            if doc_exists_method and doc_exists_method(document):
                logger.debug(f"文档已存在，跳过: {document.name}")
                continue

            # 添加 filters 到元数据
            if filters:
                meta_data = document.meta_data.copy() if document.meta_data else {}
                meta_data.update(filters)
                document.meta_data = meta_data

            # 注意：不在这里嵌入，让父类的 insert() 方法来处理
            # 这样避免重复嵌入
            processed_count += 1

            # 报告进度
            if self.progress_callback:
                self.progress_callback(
                    ImportStage.EMBEDDING,
                    idx + 1,
                    len(documents),
                    f"准备处理: {document.name}",
                    metadata={"doc_name": document.name}
                )

        # 直接实现插入逻辑，不调用父类的 insert()
        try:
            logger.info(f"直接实现插入逻辑，保存 {len(documents)} 个文档")
            logger.info(f"embedder 类型: {type(self.embedder).__name__}")
            logger.info(f"embedder.dimensions: {self.embedder.dimensions}")
            
            # 检查表状态
            if self.table is None:
                logger.error("self.table is None!")
                return
            logger.info(f"self.table 类型: {type(self.table)}")
            logger.info(f"self.table 对象: {self.table}")
            # 直接调用属性，不使用 getattr
            try:
                tbl_name = self.table.name
                tbl_schema = self.table.schema
                logger.info(f"self.table.name: {tbl_name}")
                logger.info(f"self.table.schema: {tbl_schema}")
            except Exception as e:
                logger.error(f"获取表属性失败: {e}")
            logger.info(f"self._vector_col: {getattr(self, '_vector_col', 'not set')}")
            logger.info(f"self._id: {getattr(self, '_id', 'not set')}")
            
            # 检查文档嵌入前的状态
            for i, doc in enumerate(documents):
                logger.debug(f"文档 {i} 嵌入前: embedding={doc.embedding is not None}, content={doc.content[:50] if doc.content else 'None'}")
            
            # 构建数据列表
            from hashlib import md5
            import json
            
            logger.info(f"开始处理文档，当前表版本: {self.table.version}")
            
            data = []
            for document in documents:
                # 计算文档 ID
                cleaned_content = document.content.replace("\x00", "\ufffd")
                doc_id = str(md5(cleaned_content.encode()).hexdigest())
                
                # 检查文档是否已存在，如果存在则删除
                doc_exists_method = getattr(self, 'doc_exists', None)
                if doc_exists_method and doc_exists_method(document):
                    logger.info(f"文档已存在，删除旧记录: {document.name} (ID: {doc_id})")
                    try:
                        if self.table:
                            self.table.delete(f"id == '{doc_id}'")
                            logger.info(f"成功删除旧记录: {doc_id}, 新版本: {self.table.version}")
                    except Exception as e:
                        logger.warning(f"删除旧记录失败: {e}")
                
                # 添加 filters 到元数据
                if filters:
                    meta_data = document.meta_data.copy() if document.meta_data else {}
                    meta_data.update(filters)
                    document.meta_data = meta_data
                
                # 嵌入文档（如果还没有嵌入）
                if not document.embedding:
                    logger.info(f"文档 {document.name} 没有嵌入，调用 embed()")
                    document.embed(embedder=self.embedder)
                else:
                    logger.info(f"文档 {document.name} 已有嵌入，维度: {len(document.embedding)}")
                
                # 准备数据
                payload = {
                    "name": document.name,
                    "meta_data": document.meta_data,
                    "content": cleaned_content,
                    "usage": document.usage,
                    "content_id": document.content_id,
                    "content_hash": content_hash,
                }
                
                # 准备向量
                vector = self._prepare_vector(document.embedding)
                logger.info(f"文档 {document.name} 向量准备完成: 维度={len(vector)}, 类型={type(vector)}, 前5个值={vector[:5]}")
                
                # 确保向量是列表类型
                if not isinstance(vector, list):
                    vector = list(vector)
                    logger.info(f"向量转换为列表，维度={len(vector)}")
                
                # 注意：字段顺序必须与 LanceDB schema 一致：vector, id, payload
                data.append({
                    "vector": vector,
                    "id": doc_id,
                    "payload": json.dumps(payload, ensure_ascii=False),
                })
            
            # 添加到 LanceDB
            if data:
                logger.info(f"准备添加 {len(data)} 条记录到 LanceDB")
                # 详细打印每条记录
                for i, record in enumerate(data):
                    logger.info(f"记录 {i}: id={record['id']}, vector类型={type(record['vector'])}, vector维度={len(record['vector']) if record['vector'] else 0}")
                    if record['vector']:
                        logger.info(f"  vector前5个值: {record['vector'][:5]}")

                # 添加数据到表
                if self.table is None:
                    logger.error("self.table is None, 无法添加数据")
                    return

                if self.on_bad_vectors is not None and self.fill_value is not None:
                    # 使用类型断言确保类型正确
                    result = self.table.add(
                        data,
                        on_bad_vectors=cast(Any, self.on_bad_vectors),
                        fill_value=self.fill_value
                    )
                else:
                    result = self.table.add(data)
                logger.info(f"成功添加 {len(data)} 条记录, 结果: {result}")
                
                # 立即验证数据是否正确写入
                logger.info("验证写入的数据...")
                # 使用 Arrow API 直接查询
                arrow_table = self.table.to_arrow()
                vector_col = arrow_table.column('vector')
                vector_list = vector_col.to_pylist()
                for i, vec in enumerate(vector_list):
                    dim = len(vec) if vec is not None else 0
                    logger.info(f"  Arrow 记录 {i}: 向量维度={dim}")
                
                # 强制刷新表连接
                self.table = cast(Optional["LanceTable"], self.connection.open_table(name=self.table_name))
                if self.table is None:
                    logger.error("刷新表连接失败")
                    return
                df = self.table.to_pandas()
                for i, row in df.iterrows():
                    row_id = row['id']
                    vector = row['vector']
                    dim = len(vector) if vector is not None else 0
                    logger.info(f"  Pandas 记录 {i}: ID={row_id}, 向量维度={dim}")
            else:
                logger.info("没有新数据需要添加")
            
            # 检查文档嵌入后的状态
            for i, doc in enumerate(documents):
                logger.info(f"文档 {i} 嵌入后: embedding={doc.embedding is not None}, 维度={len(doc.embedding) if doc.embedding else 0}")

            # 报告完成
            if self.progress_callback:
                logger.info(f"完成！成功保存 {processed_count} 个文档")
                self.progress_callback(
                    ImportStage.SAVING,
                    len(documents),
                    len(documents),
                    f"完成！成功保存 {processed_count} 个文档"
                )

        except Exception as e:
            logger.error(f"保存文档失败: {e}")
            raise

    async def async_insert(
        self,
        content_hash: str,
        documents: List[Document],
        filters: Optional[Dict[str, Any]] = None
    ) -> None:
        """
        重写异步 insert 方法，添加进度报告和取消支持
        """
        logger.debug(f"[LanceDbWithProgress] async_insert() 被调用，文档数: {len(documents)}, 进度回调: {self.progress_callback is not None}")

        if len(documents) <= 0:
            logger.info("没有文档需要插入")
            return

        # 报告开始嵌入
        if self.progress_callback:
            logger.info(f"开始向量化 {len(documents)} 个文档")
            self.progress_callback(
                ImportStage.EMBEDDING,
                0,
                len(documents),
                f"开始向量化 {len(documents)} 个文档"
            )

        # 处理每个文档
        processed_count = 0
        for idx, document in enumerate(documents):
            # 检查取消
            if self.cancel_token and self.cancel_token.is_cancelled:
                logger.info(f"插入已取消，已完成 {processed_count}/{len(documents)} 个文档")
                break

            # 检查文档是否已存在
            doc_exists_method = getattr(self, 'doc_exists', None)
            if doc_exists_method and doc_exists_method(document):
                logger.debug(f"文档已存在，跳过: {document.name}")
                continue

            # 添加 filters 到元数据
            if filters:
                meta_data = document.meta_data.copy() if document.meta_data else {}
                meta_data.update(filters)
                document.meta_data = meta_data

            # 注意：不在这里嵌入，让父类的 async_insert() 方法来处理
            # 这样避免重复嵌入
            processed_count += 1

            # 报告进度
            if self.progress_callback:
                self.progress_callback(
                    ImportStage.EMBEDDING,
                    idx + 1,
                    len(documents),
                    f"准备处理: {document.name}",
                    metadata={"doc_name": document.name}
                )

        # 直接调用我们的 insert 方法
        try:
            logger.info(f"直接调用 insert() 保存 {len(documents)} 个文档")
            logger.info(f"embedder 类型: {type(self.embedder).__name__}")
            logger.info(f"embedder.enable_batch: {getattr(self.embedder, 'enable_batch', 'N/A')}")
            logger.info(f"embedder.dimensions: {self.embedder.dimensions}")
            
            # 检查文档嵌入前的状态
            for i, doc in enumerate(documents):
                logger.debug(f"文档 {i} 嵌入前: embedding={doc.embedding is not None}, content={doc.content[:50] if doc.content else 'None'}")
            
            # 嵌入文档（如果还没有嵌入）
            if self.embedder.enable_batch:
                # 检查是否有批量嵌入方法
                async_batch_embed = getattr(self.embedder, "async_get_embeddings_batch_and_usage", None)
                if async_batch_embed:
                    try:
                        doc_contents = [doc.content for doc in documents]
                        embeddings, usages = await async_batch_embed(doc_contents)
                        for j, doc in enumerate(documents):
                            if j < len(embeddings):
                                doc.embedding = embeddings[j]
                                doc.usage = usages[j] if j < len(usages) else None
                    except Exception as e:
                        logger.error(f"异步批量嵌入失败: {e}")
                        raise
                else:
                    # 逐个嵌入
                    for doc in documents:
                        if not doc.embedding:
                            await doc.async_embed(embedder=self.embedder)
            else:
                # 逐个嵌入
                for doc in documents:
                    if not doc.embedding:
                        await doc.async_embed(embedder=self.embedder)
            
            # 调用我们的 insert 方法
            self.insert(content_hash, documents, filters)
            
            # 检查文档嵌入后的状态
            for i, doc in enumerate(documents):
                logger.info(f"文档 {i} 嵌入后: embedding={doc.embedding is not None}, 维度={len(doc.embedding) if doc.embedding else 0}")

            # 报告完成
            if self.progress_callback:
                logger.info(f"完成！成功保存 {processed_count} 个文档")
                self.progress_callback(
                    ImportStage.SAVING,
                    len(documents),
                    len(documents),
                    f"完成！成功保存 {processed_count} 个文档"
                )

        except Exception as e:
            logger.error(f"保存文档失败: {e}")
            raise

    def upsert(
        self,
        content_hash: str,
        documents: List[Document],
        filters: Optional[Dict[str, Any]] = None
    ) -> None:
        """
        重写 upsert 方法 - 直接调用我们的 insert 方法
        """
        logger.debug(f"[LanceDbWithProgress] upsert() 被调用，文档数: {len(documents)}")
        # 直接调用我们的 insert 方法
        self.insert(content_hash, documents, filters)

    async def async_upsert(
        self,
        content_hash: str,
        documents: List[Document],
        filters: Optional[Dict[str, Any]] = None
    ) -> None:
        """
        重写 async_upsert 方法 - 直接调用我们的 async_insert 方法
        """
        logger.debug(f"[LanceDbWithProgress] async_upsert() 被调用，文档数: {len(documents)}")
        # 直接调用我们的 async_insert 方法
        await self.async_insert(content_hash, documents, filters)

    def update_metadata(self, content_id: str, metadata: Dict[str, Any]) -> None:
        """
        重写 update_metadata 方法 - 修复向量丢失问题

        原方法的 bug：查询时只选择 ["id", "payload"]，不包含 "vector" 字段，
        导致更新时 vector_data 永远是 None，向量数据丢失。

        修复：在查询时包含 "vector" 字段。
        """
        import json

        try:
            if self.table is None:
                logger.error("Table not initialized")
                return

            logger.info(f"[update_metadata] 开始更新元数据，content_id: {content_id}")

            # Get all documents and filter in Python (LanceDB doesn't support JSON operators)
            # 关键修复：包含 "vector" 字段！
            total_count = self.table.count_rows()
            results = self.table.search().select(["id", "payload", "vector"]).limit(total_count).to_pandas()

            if results.empty:
                logger.debug("No documents found")
                return

            # Find matching documents with the given content_id
            matching_rows = []
            for _, row in results.iterrows():
                payload_str = row["payload"]
                if isinstance(payload_str, str):
                    payload = json.loads(payload_str)
                    if payload.get("content_id") == content_id:
                        matching_rows.append(row)

            if not matching_rows:
                logger.debug(f"No documents found with content_id: {content_id}")
                return

            logger.info(f"[update_metadata] 找到 {len(matching_rows)} 条匹配记录")

            # Update each matching document
            updated_count = 0
            for row in matching_rows:
                row_id = row["id"]
                payload_str = row["payload"]
                if not isinstance(payload_str, str):
                    logger.warning(f"Payload is not a string for row {row_id}")
                    continue
                current_payload = json.loads(payload_str)

                # Merge existing metadata with new metadata
                if "meta_data" in current_payload:
                    current_payload["meta_data"].update(metadata)
                else:
                    current_payload["meta_data"] = metadata

                if "filters" in current_payload:
                    if isinstance(current_payload["filters"], dict):
                        current_payload["filters"].update(metadata)
                    else:
                        current_payload["filters"] = metadata
                else:
                    current_payload["filters"] = metadata

                # Update the document
                update_data = {"id": row_id, "payload": json.dumps(current_payload, ensure_ascii=False)}

                # 关键修复：正确获取 vector 数据
                vector_data = row["vector"] if "vector" in row else None
                text_data = row["text"] if "text" in row else None

                # Create complete update record
                if vector_data is not None:
                    # 确保向量是列表格式
                    if hasattr(vector_data, 'tolist'):
                        vector_data = vector_data.tolist()
                    update_data["vector"] = vector_data
                    logger.info(f"[update_metadata] 保留向量数据，维度: {len(vector_data)}")
                else:
                    logger.warning(f"[update_metadata] 警告：记录 {row_id} 没有向量数据！")

                if text_data is not None:
                    update_data["text"] = text_data

                # Delete old record and insert updated one
                self.table.delete(f"id = '{row_id}'")
                self.table.add([update_data])
                updated_count += 1
                logger.info(f"[update_metadata] 更新记录 {row_id}，新表版本: {self.table.version}")

            logger.info(f"[update_metadata] 成功更新 {updated_count} 条记录的元数据")

        except Exception as e:
            logger.error(f"Error updating metadata for content_id '{content_id}': {e}")
            raise


# ==============================================================================
# 3. 增强的 Knowledge
# ==============================================================================

class KnowledgeWithProgress(Knowledge):
    """
    增强的 Knowledge - 添加进度反馈和取消支持

    继承自 agno.knowledge.knowledge.Knowledge
    保持完全的接口兼容性
    """

    def __init__(
        self,
        *args,
        progress_callback: Optional[ProgressCallback] = None,
        cancel_token: Optional[CancelToken] = None,
        **kwargs
    ):
        """
        初始化

        参数：
            progress_callback: 进度回调函数
            cancel_token: 取消令牌
        """
        # 先初始化父类
        super().__init__(*args, **kwargs)

        # 处理 vector_db 的进度追踪
        if self.vector_db:
            # 如果已经是 LanceDbWithProgress 实例，直接设置回调
            if isinstance(self.vector_db, LanceDbWithProgress):
                self.vector_db.progress_callback = progress_callback
                self.vector_db.cancel_token = cancel_token
                logger.info("vector_db 已是 LanceDbWithProgress 实例，已设置回调")
            # 如果是普通 LanceDb 实例，动态替换方法
            elif isinstance(self.vector_db, LanceDb):
                # 添加进度回调属性（使用 setattr 避免 pyright 类型检查错误）
                setattr(self.vector_db, 'progress_callback', progress_callback)
                setattr(self.vector_db, 'cancel_token', cancel_token)
                # 动态替换方法 - 必须替换所有可能被调用的方法
                setattr(self.vector_db, 'insert', lambda content_hash, documents, filters=None: \
                    LanceDbWithProgress.insert(cast(LanceDbWithProgress, self.vector_db), content_hash, documents, filters))
                setattr(self.vector_db, 'async_insert', lambda content_hash, documents, filters=None: \
                    LanceDbWithProgress.async_insert(cast(LanceDbWithProgress, self.vector_db), content_hash, documents, filters))
                setattr(self.vector_db, 'upsert', lambda content_hash, documents, filters=None: \
                    LanceDbWithProgress.upsert(cast(LanceDbWithProgress, self.vector_db), content_hash, documents, filters))
                setattr(self.vector_db, 'async_upsert', lambda content_hash, documents, filters=None: \
                    LanceDbWithProgress.async_upsert(cast(LanceDbWithProgress, self.vector_db), content_hash, documents, filters))
                logger.info("已为普通 LanceDb 实例添加进度追踪支持（包括 upsert 方法）")

    async def add_content_async(
        self,
        *args,
        progress_callback: Optional[ProgressCallback] = None,
        cancel_token: Optional[CancelToken] = None,
        **kwargs
    ) -> None:
        """
        重写 add_content_async，添加进度追踪

        这个方法会在每次导入时被调用，确保回调和令牌被正确设置到 vector_db
        """
        # 更新 vector_db 的回调和令牌
        if isinstance(self.vector_db, LanceDbWithProgress):
            self.vector_db.progress_callback = progress_callback
            self.vector_db.cancel_token = cancel_token

        # 调用父类方法
        await super().add_content_async(*args, **kwargs)
