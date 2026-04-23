"""
知识库数据加载器
负责从 LanceDB 和 Agno API 加载文档数据
"""
from __future__ import annotations
from typing import TYPE_CHECKING, List, Optional
import logging

from .models import SimpleDocument

if TYPE_CHECKING:
    from Agent.CustomerAgent.agent_knowledge import KnowledgeManager

logger = logging.getLogger(__name__)


class KnowledgeDataLoader:
    """知识库数据加载器 - 单一职责：数据加载"""

    def __init__(self, knowledge_manager: KnowledgeManager):
        """
        初始化数据加载器

        Args:
            knowledge_manager: 知识库管理器实例
        """
        self.knowledge_manager = knowledge_manager

    def load_documents(self, limit: Optional[int] = None) -> List[SimpleDocument]:
        """
        加载文档列表

        Args:
            limit: 最大文档数量限制

        Returns:
            文档列表
        """
        try:
            docs = []

            # 方案1: 直接从 LanceDB 获取
            try:
                docs = self._load_from_lancedb()
                logger.info(f"从 LanceDB 加载了 {len(docs)} 个文档")

            except Exception as lancedb_err:
                logger.warning(f"LanceDB 直接获取失败: {lancedb_err}")
                # 方案2: 回退到搜索 API
                docs = self._load_from_search_api(limit)
                logger.info(f"从搜索 API 加载了 {len(docs)} 个文档")

            if limit and len(docs) > limit:
                docs = docs[:limit]

            return docs

        except Exception as e:
            logger.error(f"加载文档失败: {e}")
            return []

    def _load_from_lancedb(self) -> List[SimpleDocument]:
        """
        直接从 LanceDB 加载数据

        Returns:
            文档列表
        """
        import lancedb

        if self.knowledge_manager.knowledge.vector_db is None:
            logger.warning("向量数据库未初始化")
            return []
        
        db_path = self.knowledge_manager.knowledge.vector_db.uri
        db = lancedb.connect(db_path)
        table = db.open_table("customer_knowledge")

        df = table.to_pandas()

        docs = []
        for idx, row in df.iterrows():
            doc = SimpleDocument.from_lancedb_row(row.to_dict(), int(idx) if isinstance(idx, int) else 0)
            docs.append(doc)

        return docs

    def _load_from_search_api(self, limit: Optional[int] = None) -> List[SimpleDocument]:
        """
        通过搜索 API 加载数据

        Args:
            limit: 结果数量限制

        Returns:
            文档列表
        """
        search_limit = limit or 1000
        results = self.knowledge_manager.search_knowledge("", limit=search_limit)
        return [SimpleDocument.from_agno_doc(doc) for doc in results]
