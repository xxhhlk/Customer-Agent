from agno.vectordb.lancedb import LanceDb, SearchType
from agno.db.sqlite import SqliteDb
from agno.knowledge.embedder.openai import OpenAIEmbedder
from agno.knowledge.document import Document
from agno.models.openai import OpenAILike
from agno.knowledge.knowledge import Knowledge
from typing import Optional
import logging
import os
import uuid as uuid_lib
import hashlib

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
        import threading
        from pathlib import Path

        # 写操作锁，保护 insert/delete/update 的并发安全
        self._write_lock = threading.Lock()

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
        # search_type: vector (纯向量搜索) — 避免 tantivy C 扩展在低内存环境下崩溃
        # hybrid 模式会加载 tantivy 全文索引，4GB 内存服务器上夜间内存不足时
        # tantivy 堆操作导致 ntdll 堆管理器 access violation 崩溃
        vector_db = LanceDbWithProgress(
                table_name="customer_knowledge",
                uri=str(vector_path),
                embedder=VolcengineEmbedder(**embedder_config),
                search_type=SearchType.vector
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
        
        # 从配置中读取 max_results，默认为 3
        max_results = 3
        try:
            config = Config()
            max_results = config.get("knowledge_base.max_results", 3)
            if not isinstance(max_results, int) or max_results < 1:
                max_results = 3
            print(f"[DEBUG] 知识库搜索结果数量: {max_results}")
        except Exception as e:
            print(f"[DEBUG] 读取 max_results 配置失败: {e}, 使用默认值 3")
        
        self.knowledge = KnowledgeWithProgress(
            description="客户代理知识库，包含产品介绍、使用方法和常见问题解答。",
            contents_db=contents_db,
            vector_db=vector_db,
            max_results=max_results,
            readers=readers  # 只添加可用的读取器
        )
        print(f"[DEBUG] [OK] KnowledgeWithProgress 创建成功")
        logger.info(f"使用增强版 Knowledge，max_results={max_results}")


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

            file_ext = os.path.splitext(file_path)[1].lower()

            # CSV 文件特殊处理，正确解析带引号的多行字段
            if file_ext == '.csv':
                return await self._import_csv_file(file_path)

            # 其他文件使用 agno 框架的导入方法
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

    async def _import_csv_file(self, file_path: str) -> int:
        """
        导入 CSV 文件，正确处理带引号的多行字段

        采用攒批写入策略：每 BATCH_SIZE 条数据攒一批，
        一次性 embed + 一次性 table.add，避免 130 次独立 native 写入导致崩溃。

        Args:
            file_path: CSV 文件路径

        Returns:
            导入的内容数量
        """
        import csv

        logger.info(f"开始导入 CSV 文件: {file_path}")

        # 检测文件编码
        from utils.encoding_helper import EncodingConverter
        temp_path, encoding = EncodingConverter.ensure_utf8(file_path)
        actual_path = temp_path if temp_path else file_path
        logger.info(f"CSV 文件编码: {encoding}")

        imported_count = 0

        try:
            with open(actual_path, 'r', encoding='utf-8', newline='') as f:
                # 使用 csv 模块正确解析带引号的多行字段
                reader = csv.DictReader(f)

                # 检查必需的列
                if reader.fieldnames is None:
                    raise ValueError("CSV 文件为空或格式不正确")

                # 支持多种列名格式
                title_col = None
                content_col = None

                for field in reader.fieldnames:
                    field_lower = field.lower().strip()
                    if field_lower in ['标题', 'title', 'name']:
                        title_col = field
                    elif field_lower in ['内容', 'content', 'text', '正文']:
                        content_col = field

                if not title_col or not content_col:
                    raise ValueError(f"CSV 文件缺少必需的列。需要'标题'和'内容'列，当前列: {reader.fieldnames}")

                logger.info(f"CSV 列映射: 标题='{title_col}', 内容='{content_col}'")

                # 攒批读取所有行
                BATCH_SIZE = 20  # 每批 20 条，平衡内存与写入次数
                batch: list[tuple[str, str, int]] = []  # (title, content, row_num)

                for row_num, row in enumerate(reader, start=2):  # 从第2行开始（第1行是表头）
                    try:
                        title = row.get(title_col, '').strip()
                        content = row.get(content_col, '').strip()

                        if not title or not content:
                            logger.warning(f"第 {row_num} 行: 标题或内容为空，跳过")
                            continue

                        batch.append((title, content, row_num))

                        # 攒满一批就写入
                        if len(batch) >= BATCH_SIZE:
                            count = await self._add_batch_content(batch)
                            imported_count += count
                            logger.info(f"已导入 {imported_count} 条 (当前批次完成)")
                            batch.clear()

                    except Exception as row_err:
                        logger.warning(f"第 {row_num} 行预处理失败: {row_err}")
                        continue

                # 写入剩余不足一批的数据
                if batch:
                    count = await self._add_batch_content(batch)
                    imported_count += count
                    logger.info(f"最后一批导入完成，总计 {imported_count} 条")

            logger.info(f"CSV 文件导入完成: {file_path}, 成功导入 {imported_count} 条")

        finally:
            # 清理临时文件
            if actual_path != file_path and os.path.exists(actual_path):
                try:
                    os.remove(actual_path)
                except:
                    pass

        return imported_count

    @staticmethod
    def _build_embed_text(title: Optional[str], content: str) -> str:
        """
        构造参与向量计算的文本：标题 + 正文。

        标题参与向量能提升「用标题关键词检索」的召回率；
        存储的 content 仍是原始内容，不受影响。
        """
        title = (title or "").strip()
        if title:
            return f"{title}\n{content}"
        return content

    async def _add_batch_content(
        self,
        batch: list[tuple[str, str, int]],
        source: str = 'csv_import',
        doc_type: str = 'csv',
    ) -> int:
        """
        批量添加内容到知识库

        将一批 (title, content) 一次性 embed + 一次性 table.add，
        替代原来逐条调用 add_content_async 的方式。
        向量计算时标题参与（_build_embed_text），存储的 content 保持原始。

        Args:
            batch: [(title, content, row_num), ...] 列表
            source: metadata 来源标记（csv_import/manual_input/manual_edit）
            doc_type: contents_db 的 type 字段

        Returns:
            成功写入的条数
        """
        if not batch:
            return 0

        batch_size = len(batch)
        logger.info(f"开始批量写入 {batch_size} 条数据")

        try:
            vector_db = self.knowledge.vector_db
            if vector_db is None:
                logger.error("向量数据库未初始化，无法批量写入")
                return 0

            # 1. 构造 Document 列表
            documents: list[Document] = []
            for title, content, _row_num in batch:
                doc = Document(
                    content=content,
                    id=str(uuid_lib.uuid4()),
                    name=title,
                    meta_data={
                        'title': title,
                        'source': source,
                        'filename': f"{title}.txt"
                    }
                )
                doc.content_id = doc.id
                documents.append(doc)

            # 2. 批量嵌入 — 调用 embedder 的批量接口
            #    火山引擎 API 内部仍是逐个请求，但收集到一起后一次 table.add
            embedder = vector_db.embedder
            if embedder and hasattr(embedder, 'async_get_embeddings_batch_and_usage'):
                # 标题参与向量计算：embed 输入 = "标题\n正文"
                doc_contents = [self._build_embed_text(doc.name, doc.content) for doc in documents]
                embeddings, usages = await embedder.async_get_embeddings_batch_and_usage(doc_contents)
                for j, doc in enumerate(documents):
                    if j < len(embeddings) and embeddings[j]:
                        doc.embedding = embeddings[j]
                        doc.usage = usages[j] if j < len(usages) else None
                    else:
                        logger.warning(f"批量嵌入第 {j} 条失败，跳过")
                        doc.embedding = []
            else:
                # 降级：逐个嵌入（标题同样参与，与批量路径口径一致）
                for doc in documents:
                    if not doc.embedding:
                        embed_text = self._build_embed_text(doc.name, doc.content)
                        doc.embedding, doc.usage = await embedder.async_get_embedding_and_usage(embed_text)

            # 3. 过滤掉嵌入失败的文档
            valid_docs = [doc for doc in documents if doc.embedding]
            if not valid_docs:
                logger.error("本批所有文档嵌入均失败，跳过写入")
                return 0

            # 4. 加写锁，一次性写入 LanceDB
            with self._write_lock:
                # 构造 LanceDB 数据行
                import json
                data = []
                # 所有文档共用同一个 content_hash（agno 的 upsert 逻辑不依赖此字段做去重，
                # 因为我们传了 skip_if_exists=False）
                content_hash = self._build_content_hash_simple(batch[0][1])

                for doc in valid_docs:
                    cleaned_content = doc.content.replace("\x00", "\ufffd")
                    payload = {
                        "name": doc.name,
                        "meta_data": doc.meta_data,
                        "content": cleaned_content,
                        "usage": doc.usage,
                        "content_id": doc.content_id,
                        "content_hash": content_hash,
                    }
                    vector = vector_db._prepare_vector(doc.embedding)
                    if not isinstance(vector, list):
                        vector = list(vector)
                    data.append({
                        "vector": vector,
                        "id": doc.id,
                        "payload": json.dumps(payload, ensure_ascii=False),
                    })

                # 一次 table.add 写入整批数据
                table = vector_db.table
                if table is None:
                    import lancedb
                    db = lancedb.connect(vector_db.uri)
                    table = db.open_table(vector_db.table_name)

                result = table.add(data)
                logger.info(f"批量写入完成: {len(data)} 条, LanceDB 版本: {result.version}")

            # 5. 写入 contents_db（agno 的 SqliteDb）
            #    逐条 upsert，SQLite 层面没有 native crash 风险
            if self.knowledge.contents_db:
                from agno.db.schemas.knowledge import KnowledgeRow
                from agno.knowledge.content import ContentStatus
                import time
                for doc in valid_docs:
                    try:
                        content_row = KnowledgeRow(
                            id=doc.content_id,
                            name=doc.name or "",
                            description="",
                            metadata=doc.meta_data,
                            type=doc_type,
                            size=len(doc.content.encode("utf-8")) if doc.content else 0,
                            linked_to=self.knowledge.name or "",
                            access_count=0,
                            status=ContentStatus.COMPLETED.value,
                            status_message="",
                            created_at=int(time.time()),
                            updated_at=int(time.time()),
                        )
                        self.knowledge.contents_db.upsert_knowledge_content(knowledge_row=content_row)
                    except Exception as db_err:
                        logger.warning(f"contents_db 写入失败 ({doc.name}): {db_err}")

            logger.info(f"批量写入成功: {len(valid_docs)}/{batch_size} 条")
            return len(valid_docs)

        except Exception as e:
            logger.error(f"批量写入失败: {e}", exc_info=True)
            return 0

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
        删除指定文档（通过 LanceDB id 列精确删除）

        Args:
            doc_id: 文档ID（LanceDB id 列的值）

        Returns:
            是否删除成功
        """
        try:
            if not doc_id:
                logger.warning("文档ID为空，无法删除")
                return False

            logger.info(f"正在删除文档: {doc_id}")

            with self._write_lock:
                # 1. 通过 LanceDB id 列精确删除（不使用 remove_content_by_id，
                #    因为它基于 payload.content_id 匹配，可能误删同 content_id 的多条记录）
                self._delete_vector_by_lancedb_id(doc_id)

                # 2. 从 contents_db 删除（agno 的 SqliteDb）
                try:
                    if self.knowledge.contents_db:
                        self.knowledge.contents_db.delete_knowledge_content(doc_id)
                except Exception as e:
                    logger.warning(f"从 contents_db 删除失败（可能不存在）: {e}")

            logger.info(f"成功删除文档: {doc_id}")
            return True

        except Exception as e:
            logger.error(f"删除文档失败 {doc_id}: {str(e)}")
            return False

    def _delete_vector_by_lancedb_id(self, doc_id: str) -> bool:
        """
        通过 LanceDB id 列精确删除一条记录（不依赖 payload.content_id 匹配）

        Args:
            doc_id: LanceDB id 列的值

        Returns:
            是否删除成功
        """
        try:
            if not self.knowledge.vector_db:
                logger.warning("向量数据库未初始化")
                return False

            table = self.knowledge.vector_db.table
            if table is None:
                # 重新打开表
                import lancedb
                db = lancedb.connect(self.knowledge.vector_db.uri)
                table = db.open_table(self.knowledge.vector_db.table_name)

            # 通过 id 列精确删除（LanceDB 原生语法）
            id_col = getattr(self.knowledge.vector_db, '_id', 'id')
            table.delete(f"{id_col} = '{doc_id}'")
            logger.info(f"通过 LanceDB id 列删除文档: {doc_id}")
            return True

        except Exception as e:
            logger.warning(f"通过 LanceDB id 删除失败: {e}")
            return False

    @staticmethod
    def _build_content_hash_simple(content: str) -> str:
        """计算内容的 SHA256 hash，与 agno 框架的 _build_content_hash 格式一致"""
        import hashlib
        content_bytes = content.encode("utf-8")
        return hashlib.sha256(content_bytes).hexdigest()

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

            # 与 CSV 导入共用批量写入路径，保证标题参与向量计算的口径一致
            # （agno 原生 add_content_async 的 upsert 会重新 embed 纯 content，标题不参与）
            count = await self._add_batch_content(
                [(title, content, 0)],
                source='manual_input',
                doc_type='manual',
            )
            success = count > 0
            if success:
                logger.info(f"成功添加文本内容: {title}")
            else:
                logger.warning(f"添加文本内容失败: {title}")
            return success

        except Exception as e:
            logger.error(f"添加文本内容失败 {title}: {str(e)}")
            return False

    
    async def update_document_content(self, doc_id: str, title: str, content: str) -> Optional[str]:
        """
        异步更新文档内容（精确删除旧记录 + 插入新记录）

        Args:
            doc_id: 旧文档的 LanceDB id（UUID 或旧版 md5 hash）
            title: 新标题
            content: 新内容

        Returns:
            新文档的 UUID，失败返回 None
        """
        import uuid as uuid_lib

        try:
            if not doc_id or not title or not content:
                logger.warning("文档ID、标题或内容为空，无法更新")
                return None

            logger.info(f"正在更新文档: {doc_id}")

            # 生成新 UUID
            new_doc_id = str(uuid_lib.uuid4())

            # 计算 content_hash（agno 框架需要此字段）
            content_hash = self._build_content_hash_simple(content)

            with self._write_lock:
                # 1. 精确删除旧文档 — 通过 LanceDB id 列删除，不依赖 content_id 匹配
                self._delete_vector_by_lancedb_id(doc_id)

                # 同时从 contents_db 删除（agno 的 SqliteDb）
                try:
                    if self.knowledge.contents_db:
                        self.knowledge.contents_db.delete_knowledge_content(doc_id)
                except Exception as e:
                    logger.warning(f"从 contents_db 删除失败（可能不存在）: {e}")

                # 2. 构造 Document 并直接插入向量数据库
                #    绕过 agno 的 add_content_async，因为我们需要用自定义 UUID 作为 LanceDB id
                doc = Document(
                    content=content,
                    id=new_doc_id,
                    name=title,
                    meta_data={
                        'title': title,
                        'source': 'manual_edit',
                        'filename': f"{title}.txt"
                    }
                )
                # 设置 content_id（用于 payload 中的字段，agno 搜索时用）
                doc.content_id = new_doc_id

                # 预置 embedding（标题参与向量计算，与 CSV 导入口径一致）。
                # LanceDbWithProgress.insert 检测到 doc.embedding 已有值会跳过重新 embed。
                if self.knowledge.vector_db is not None:
                    embedder = self.knowledge.vector_db.embedder
                    if embedder:
                        embed_text = self._build_embed_text(title, content)
                        doc.embedding, doc.usage = await embedder.async_get_embedding_and_usage(embed_text)

                # 调用 vector_db.insert 插入（会触发我们重写的 insert 方法）
                if self.knowledge.vector_db is not None:
                    self.knowledge.vector_db.insert(
                        content_hash=content_hash,
                        documents=[doc],
                        filters=None
                    )
                else:
                    logger.warning("vector_db 为 None，跳过 insert")

            logger.info(f"成功更新文档: {title}, 新 ID: {new_doc_id}")
            return new_doc_id

        except Exception as e:
            logger.error(f"更新文档失败 {doc_id}: {str(e)}")
            return None

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

    def get_document_vector_info(self, doc_id: str) -> dict:
        """
        获取文档的向量信息

        Args:
            doc_id: 文档ID

        Returns:
            包含向量信息的字典，包括：
            - has_vector: 是否有向量
            - vector_dimension: 向量维度
            - vector_sample: 向量前10个值（用于验证）
        """
        try:
            import lancedb
            import numpy as np

            if not self.knowledge.vector_db:
                logger.warning("向量数据库未初始化")
                return {"has_vector": False, "vector_dimension": 0, "vector_sample": None}

            # 连接到 LanceDB
            db = lancedb.connect(self.knowledge.vector_db.uri)
            table = db.open_table("customer_knowledge")

            # 查询文档
            results = table.search().select(["id", "vector"]).limit(1000).to_pandas()

            # 找到对应的文档
            doc_row = results[results["id"] == doc_id]

            if doc_row.empty:
                logger.warning(f"未找到文档: {doc_id}")
                return {"has_vector": False, "vector_dimension": 0, "vector_sample": None}

            # 获取向量
            vector_data = doc_row.iloc[0]["vector"]

            if vector_data is None:
                logger.warning(f"文档向量为空: {doc_id}")
                return {"has_vector": False, "vector_dimension": 0, "vector_sample": None}

            # 转换为 numpy 数组
            if hasattr(vector_data, 'tolist'):
                vector_list = vector_data.tolist()
            else:
                vector_list = list(vector_data)

            dimension = len(vector_list)
            sample = vector_list[:10]  # 前10个值

            logger.info(f"文档 {doc_id} 向量信息: 维度={dimension}, 前10个值={sample}")

            return {
                "has_vector": True,
                "vector_dimension": dimension,
                "vector_sample": sample
            }

        except Exception as e:
            logger.error(f"获取文档向量信息失败 {doc_id}: {str(e)}")
            return {"has_vector": False, "vector_dimension": 0, "vector_sample": None}

    def clear_all_knowledge(self) -> int:
        """
        清空所有知识库数据（向量数据库 + 内容数据库）

        Returns:
            删除的文档数量
        """
        try:
            # 获取删除前的数量
            count_before = self.get_content_count()

            if count_before == 0:
                logger.info("知识库已为空，无需清空")
                return 0

            # 清空向量数据库
            self._clear_vector_db()

            # 清空内容数据库
            self._clear_contents_db()

            logger.info(f"清空完成，删除了 {count_before} 条记录")
            return count_before

        except Exception as e:
            logger.error(f"清空知识库失败: {str(e)}")
            raise

    def _clear_vector_db(self) -> None:
        """清空向量数据库"""
        import lancedb

        if not self.knowledge.vector_db:
            logger.warning("向量数据库未初始化")
            return

        db_path = self.knowledge.vector_db.uri
        table_name = self.knowledge.vector_db.table_name

        db = lancedb.connect(db_path)

        existing_tables = db.table_names()
        if table_name not in existing_tables:
            logger.info(f"表 {table_name} 不存在，无需清空")
            return

        # 删除并重建表
        db.drop_table(table_name)
        logger.info(f"已删除向量表: {table_name}")

        # 重置表连接，下次访问时自动重建
        self.knowledge.vector_db.table = None

    def _clear_contents_db(self) -> None:
        """清空内容数据库"""
        import sqlite3

        if not self.knowledge.contents_db:
            logger.warning("内容数据库未初始化")
            return

        db_file = getattr(self.knowledge.contents_db, 'db_file', None)
        if not db_file or not os.path.exists(db_file):
            logger.warning(f"内容数据库文件不存在: {db_file}")
            return

        conn = sqlite3.connect(db_file)
        try:
            cursor = conn.cursor()
            cursor.execute("SELECT name FROM sqlite_master WHERE type='table'")
            tables = cursor.fetchall()

            for table_info in tables:
                table_name = table_info[0]
                if table_name.startswith('sqlite_'):
                    continue
                cursor.execute(f"DELETE FROM [{table_name}]")
                logger.info(f"已清空表: {table_name}")

            conn.commit()
        finally:
            conn.close()

    def get_all_documents_for_export(self) -> list:
        """
        获取所有文档用于导出（包括禁用的和所有字段）

        在子进程中直接读 lancedb（不走 IPC，避免死循环）。
        返回可序列化的 SimpleDocument 列表，通过 IPC 传回主进程。

        Returns:
            文档列表，每个文档包含 id, content, metadata 等完整信息
        """
        try:
            import lancedb
            import json

            if not self.knowledge or not self.knowledge.vector_db:
                return []

            db_path = self.knowledge.vector_db.uri
            table_name = self.knowledge.vector_db.table_name
            db = lancedb.connect(db_path)
            table = db.open_table(table_name)
            df = table.to_pandas()

            from ui.knowledge.models import SimpleDocument
            docs = []
            for idx, row in df.iterrows():
                doc = SimpleDocument.from_lancedb_row(row.to_dict(), int(idx) if isinstance(idx, int) else 0)
                docs.append(doc)
            return docs
        except Exception as e:
            logger.error(f"获取所有文档失败: {str(e)}")
            return []
