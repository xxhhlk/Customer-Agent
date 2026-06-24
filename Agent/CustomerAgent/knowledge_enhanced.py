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
import uuid
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
            import json

            logger.info(f"开始处理文档，当前表版本: {self.table.version}")

            data = []
            for document in documents:
                # 计算文档 ID — 优先使用 document.id（由 update_document_content 预先设置的 UUID）
                # 否则生成新 UUID（与内容解耦，避免 md5 碰撞导致误删）
                cleaned_content = document.content.replace("\x00", "\ufffd")
                doc_id = document.id if document.id else str(uuid.uuid4())

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

    def _build_search_results(self, results: List[Dict[str, Any]]) -> List[Document]:
        """
        重写 _build_search_results — 修复 Agno 不设置 Document.id 的问题

        Agno 原生 _build_search_results 构造 Document 时不含 id 属性，
        导致上层通过搜索 API 加载文档时 doc.id 为空，
        删除/更新操作传空字符串到 LanceDB delete("id = ''") 匹配不到记录。

        修复：将 LanceDB id 列的值赋给 Document.id。
        """
        import json as _json

        search_results: List[Document] = []
        try:
            for item in results:
                payload = _json.loads(item["payload"])
                doc = Document(
                    name=payload["name"],
                    meta_data=payload["meta_data"],
                    content=payload["content"],
                    embedder=self.embedder,
                    embedding=item["vector"],
                    usage=payload["usage"],
                    content_id=payload.get("content_id"),
                )
                # 关键修复：设置 id 为 LanceDB id 列的实际值
                doc.id = item.get("id", "")
                search_results.append(doc)
        except Exception:
            logger.exception("Error building search results")

        return search_results

    def update_metadata(self, content_id: str, metadata: Dict[str, Any]) -> None:
        """
        重写 update_metadata 方法 - 修复向量丢失 + 全表扫描崩溃问题

        原方法两个 bug：
        1. Agno 原版查询不含 vector 字段 → 更新时向量丢失
        2. 之前的修复用全表 to_pandas() 加载所有向量到内存 →
           导入 130 条时 O(N²) 全表扫描导致 LanceDB native access violation 崩溃

        新方案：只用 LanceDB 原生 table.update() 按 where 条件就地更新 payload 列，
        不加载任何向量数据到内存，完全避免 native 崩溃。
        """
        import json

        try:
            if self.table is None:
                logger.error("Table not initialized")
                return

            logger.info(f"[update_metadata] 开始更新元数据，content_id: {content_id}")

            # 只查询 id + payload（不含 vector），用 LIKE 过滤 content_id
            # payload 是 JSON 字符串列，用 LIKE 匹配 content_id
            where_clause = f"payload LIKE '%\"content_id\":\"{content_id}\"%'"
            results = self.table.search().where(where_clause).select(["id", "payload"]).limit(10).to_list()

            if not results:
                logger.debug(f"No documents found with content_id: {content_id}")
                return

            logger.info(f"[update_metadata] 找到 {len(results)} 条匹配记录")

            # 逐条用 LanceDB 原生 update 就地更新 payload（不涉及向量）
            updated_count = 0
            for row in results:
                row_id = row["id"]
                payload_str = row["payload"]
                if not isinstance(payload_str, str):
                    logger.warning(f"Payload is not a string for row {row_id}")
                    continue

                current_payload = json.loads(payload_str)

                # 合并 metadata
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

                new_payload_str = json.dumps(current_payload, ensure_ascii=False)

                # LanceDB 原生 update：只改 payload 列，不碰 vector
                self.table.update(
                    where=f"id = '{row_id}'",
                    values={"payload": new_payload_str}
                )
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
