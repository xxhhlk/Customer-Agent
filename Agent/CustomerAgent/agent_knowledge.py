from agno.vectordb.lancedb import LanceDb, SearchType
from agno.db.sqlite import SqliteDb
from agno.knowledge.embedder.openai import OpenAIEmbedder
from agno.models.openai import OpenAILike
from agno.knowledge.knowledge import Knowledge
from typing import Optional
import logging

# 导入自定义的火山引擎嵌入模型
from Agent.CustomerAgent.volcengine_embedder import VolcengineEmbedder

# 导入知识库增强类
from Agent.CustomerAgent.knowledge_enhanced import (
    LanceDbWithProgress,
    KnowledgeWithProgress
)

try:
    from agno.knowledge.reader.pdf_reader import PDFReader
except ImportError:
    PDFReader = None
    print("Warning: pypdf not installed, PDF reader disabled")

try:
    from agno.knowledge.reader.text_reader import TextReader
except ImportError:
    TextReader = None
    print("Warning: Text reader not available")

try:
    from agno.knowledge.reader.json_reader import JSONReader
except ImportError:
    JSONReader = None

try:
    from agno.knowledge.reader.docx_reader import DocxReader
except ImportError:
    DocxReader = None

try:
    from agno.knowledge.reader.csv_reader import CSVReader
    from agno.knowledge.chunking.row import RowChunking
except ImportError:
    CSVReader = None
    RowChunking = None

# 导入自定义读取器
try:
    from Agent.CustomerAgent.readers.excel_reader import ExcelReader
except Exception:
    ExcelReader = None
    print("Warning: Excel reader not available")

try:
    from Agent.CustomerAgent.readers.doc_reader import DocReader
except Exception:
    DocReader = None
    print("Warning: Doc reader not available")

from agno.db.schemas.culture import CulturalKnowledge
from agno.culture.manager import CultureManager
from config import Config
import logging

logger = logging.getLogger(__name__)

class KnowledgeManager:
    def __init__(self):
        import os
        import sys
        from pathlib import Path
        
        print(f"[DEBUG] 开始初始化 KnowledgeManager")
        print(f"[DEBUG] 脚本位置: {__file__}")
        print(f"[DEBUG] 当前目录: {os.getcwd()}")
        
        # 默认使用 data 目录，避免 temp 权限问题！
        project_root = Path(__file__).resolve().parent.parent.parent
        data_dir = project_root / "data"
        data_dir.mkdir(parents=True, exist_ok=True)
        print(f"[DEBUG] 使用 data 目录: {data_dir}")
        
        contents_path = data_dir / "contents.db"
        vector_path = data_dir / "vector_db"
        vector_path.mkdir(parents=True, exist_ok=True)
        
        print(f"[DEBUG] 内容数据库: {contents_path}")
        print(f"[DEBUG] 向量数据库目录: {vector_path}")
        
        # 创建内容数据库 - 直接传 db_file，让 agno 处理！
        print(f"[DEBUG] 准备创建 SqliteDb")
        contents_db = SqliteDb(db_file=str(contents_path))
        print(f"[DEBUG] [OK] SqliteDb 创建成功")
        
        # 创建向量数据库
        print(f"[DEBUG] 准备创建向量数据库")
        
        # 尝试读取配置
        embedder_config = {
            "id": "doubao-embedding-vision-251215",
            "dimensions": 2048,  # 火山引擎多模态嵌入维度
            "api_key": "",
            "base_url": "https://ark.cn-beijing.volces.com/api/v3/embeddings/multimodal"
        }
        try:
            config = Config()
            # 拼接 base_url: api_base + /embeddings/multimodal
            api_base = config.get("embedder.api_base", "https://ark.cn-beijing.volces.com/api/v3")
            embedder_config = {
                "id": config.get("embedder.model_name", "doubao-embedding-vision-251215"),
                "dimensions": 2048,
                "api_key": config.get("embedder.api_key", ""),
                "base_url": f"{api_base.rstrip('/')}/embeddings/multimodal"
            }
        except Exception as e:
            print(f"[DEBUG] 配置读取失败: {e}")
        
        # 使用火山引擎多模态嵌入模型
        vector_db = LanceDbWithProgress(
                table_name="customer_knowledge",
                uri=str(vector_path),
                embedder=VolcengineEmbedder(**embedder_config),
                search_type=SearchType.hybrid
            )
        print(f"[DEBUG] [OK] 向量数据库创建成功")
            

        # 准备可用的读取器
        readers = []
        if CSVReader and RowChunking:
            readers.append(CSVReader(chunking_strategy=RowChunking(),encoding="utf-8" ))
        if PDFReader:
            readers.append(PDFReader())
        if TextReader:
            readers.append(TextReader())
        if JSONReader:
            readers.append(JSONReader())
        if DocxReader:
            readers.append(DocxReader())
        if ExcelReader:
            readers.append(ExcelReader())
        if DocReader:
            readers.append(DocReader())

        logger.info(f"启用的读取器: {[type(r).__name__ for r in readers]}")

        # 创建知识库实例 - 使用增强版本
        print(f"[DEBUG] 准备创建 KnowledgeWithProgress")
        self.knowledge = KnowledgeWithProgress(
            description="客户代理知识库，包含产品介绍、使用方法和常见问题解答。",
            contents_db=contents_db,
            vector_db=vector_db,
            max_results=3,
            readers=readers  # 只添加可用的读取器
        )
        print(f"[DEBUG] [OK] KnowledgeWithProgress 创建成功")
        logger.info("使用增强版 Knowledge")


    async def add_content_from_file(self, file_path: str) -> int:
        """
        从文件添加内容到知识库

        Args:
            file_path: 文件路径

        Returns:
            导入的内容数量
        """
        try:
            logger.info(f"开始导入文件: {file_path}")

            # 使用正确的API添加内容
            result = await self.knowledge.add_content_async(
                path=file_path,
                skip_if_exists=False
            )

            logger.info(f"文件导入完成: {file_path}, 结果: {result}")

            # 强制刷新数据库连接，确保内容被正确保存
            try:
                # 重新连接数据库确保数据被写入
                if hasattr(self.knowledge, 'contents_db') and self.knowledge.contents_db:
                    # 创建新的连接来验证数据
                    db_file = getattr(self.knowledge.contents_db, 'db_file', None)
                    if db_file:
                        test_db = SqliteDb(db_file=db_file)
                        logger.info(f"内容数据库连接测试成功: {db_file}")
            except Exception as db_err:
                logger.warning(f"内容数据库连接测试失败: {db_err}")

            return result if result is not None else 0

        except Exception as e:
            logger.error(f"导入文件失败 {file_path}: {str(e)}")
            raise

    def search_knowledge(self, query: str, limit: Optional[int] = None) -> list:
        """
        搜索知识库内容

        Args:
            query: 搜索查询
            limit: 结果数量限制

        Returns:
            搜索结果列表
        """
        try:
            logger.info(f"搜索知识库: {query}")

            # 使用正确的API搜索（不使用limit参数）
            results = self.knowledge.search(query)

            # 如果指定了limit，手动截取结果
            if limit and len(results) > limit:
                results = results[:limit]

            logger.info(f"搜索完成，返回 {len(results)} 个结果")
            return results

        except Exception as e:
            logger.error(f"搜索知识库失败: {str(e)}")
            raise

    def get_content_count(self) -> int:
        """
        获取知识库中的内容数量

        Returns:
            文档总数
        """
        try:
            # get_content() 返回 (List[Content], total_count)
            contents, count = self.knowledge.get_content()

            # 直接使用框架返回的计数值（避免重复计算）
            return count if count is not None else 0

        except Exception as e:
            logger.error(f"获取内容数量失败: {str(e)}")
            return 0

    def get_all_contents(self) -> list:
        """获取所有知识库内容"""
        try:
            contents = self.knowledge.get_content()
            if isinstance(contents, tuple) and len(contents) >= 1:
                return contents[0]  # 第一部分是内容列表
            elif isinstance(contents, list):
                return contents
            else:
                return []
        except Exception as e:
            logger.error(f"获取所有内容失败: {str(e)}")
            return []

    def delete_document(self, doc_id: str) -> bool:
        """
        删除指定文档（同时删除向量和元数据）

        Args:
            doc_id: 文档ID（content hash）

        Returns:
            是否删除成功
        """
        try:
            if not doc_id:
                logger.warning("文档ID为空，无法删除")
                return False

            logger.info(f"正在删除文档: {doc_id}")

            # 1. 使用框架方法删除内容数据库记录
            self.knowledge.remove_content_by_id(doc_id)

            # 2. 额外确保从向量数据库删除（防止残留）
            try:
                import lancedb
                if self.knowledge.vector_db is not None:
                    db = lancedb.connect(self.knowledge.vector_db.uri)
                    table = db.open_table("customer_knowledge")
                    
                    # LanceDB 使用 id 列来删除
                    table.delete(f"id == '{doc_id}'")
                    logger.info(f"从向量数据库删除文档: {doc_id}")
                else:
                    logger.warning("向量数据库未初始化，跳过删除")
                
            except Exception as e:
                logger.warning(f"从向量数据库删除失败（可能已不存在）: {e}")

            logger.info(f"成功删除文档: {doc_id}")
            return True

        except Exception as e:
            logger.error(f"删除文档失败 {doc_id}: {str(e)}")
            return False

    async def add_text_content(self, title: str, content: str) -> bool:
        """
        异步添加文本内容到知识库

        Args:
            title: 文本标题
            content: 文本内容

        Returns:
            是否添加成功
        """
        try:
            if not title or not content:
                logger.warning("标题或内容为空，无法添加")
                return False

            logger.info(f"正在添加文本内容: {title}")

            # 直接存储原始内容，metadata 中已有 title
            await self.knowledge.add_content_async(
                text_content=content,
                metadata={
                    'title': title,
                    'source': 'manual_input',
                    'filename': f"{title}.txt"
                },
                skip_if_exists=False
            )

            logger.info(f"成功添加文本内容: {title}")
            return True

        except Exception as e:
            logger.error(f"添加文本内容失败 {title}: {str(e)}")
            return False

    
    async def update_document_content(self, doc_id: str, title: str, content: str) -> bool:
        """
        异步更新文档内容（删除旧的，添加新的）

        Args:
            doc_id: 文档ID
            title: 新标题
            content: 新内容

        Returns:
            是否更新成功
        """
        try:
            if not doc_id or not title or not content:
                logger.warning("文档ID、标题或内容为空，无法更新")
                return False

            logger.info(f"正在更新文档: {doc_id}")

            # 1. 删除旧文档
            self.knowledge.remove_content_by_id(doc_id)

            # 2. 添加新内容（直接存储原始内容，不添加格式前缀）
            await self.knowledge.add_content_async(
                text_content=content,
                metadata={
                    'title': title,
                    'source': 'manual_edit',
                    'filename': f"{title}.txt"
                },
                skip_if_exists=False
            )

            logger.info(f"成功更新文档: {title}")
            return True

        except Exception as e:
            logger.error(f"更新文档失败 {doc_id}: {str(e)}")
            return False

    def modify_document(self, doc_id: str, file_path: str) -> bool:
        """修改指定文档的内容（通过文件）"""
        try:
            # 1. 先删除旧文档
            if not self.delete_document(doc_id):
                logger.error(f"修改文档失败，无法删除旧文档: {doc_id}")
                return False

            # 2. 使用统一的异步工具导入新文档
            from utils.async_helper import run_async
            result = run_async(self.add_content_from_file(file_path))

            logger.info(f"成功修改文档: {doc_id}, 导入新文档数量: {result}")
            return True

        except Exception as e:
            logger.error(f"修改文档失败: {str(e)}")
            return False
