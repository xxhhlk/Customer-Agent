"""
知识库导出与清空工作线程

提供后台导出和清空操作，避免阻塞UI。
导出格式兼容现有导入系统（CSV/JSON），支持无缝重新导入。
"""

from __future__ import annotations

import asyncio
import csv
import json
import os
import shutil
from datetime import datetime
from typing import TYPE_CHECKING, List, Optional

from PyQt6.QtCore import QThread, pyqtSignal

from utils.logger_loguru import get_logger

if TYPE_CHECKING:
    from Agent.CustomerAgent.agent_knowledge import KnowledgeManager

logger = get_logger(__name__)


class ExportWorker(QThread):
    """
    知识库导出工作线程

    支持两种导出格式：
    - CSV: 兼容现有CSV导入功能，包含 标题,内容,标签,来源 列
    - JSON: 完整备份，包含文档ID、元数据等所有信息

    导出的CSV文件可直接通过"导入知识库"功能重新导入。
    """

    success = pyqtSignal(str, int)  # file_path, exported_count
    failed = pyqtSignal(str)        # error_message
    progress = pyqtSignal(str)     # progress_message

    def __init__(
        self,
        knowledge_manager: "KnowledgeManager",
        file_path: str,
        export_format: str = "csv"
    ):
        """
        初始化导出工作线程

        Args:
            knowledge_manager: 知识库管理器
            file_path: 导出文件路径
            export_format: 导出格式 ("csv" 或 "json")
        """
        super().__init__()
        self.knowledge_manager = knowledge_manager
        self.file_path = file_path
        self.export_format = export_format.lower()
        self.setObjectName("ExportWorker")

    def run(self):
        """在子线程中执行导出"""
        try:
            self.progress.emit("正在加载知识库数据...")

            # 加载所有文档
            docs = self._load_all_documents()

            if not docs:
                self.failed.emit("知识库为空，没有可导出的数据")
                return

            self.progress.emit(f"已加载 {len(docs)} 条数据，正在导出...")

            # 根据格式导出
            if self.export_format == "csv":
                count = self._export_csv(docs)
            elif self.export_format == "json":
                count = self._export_json(docs)
            else:
                self.failed.emit(f"不支持的导出格式: {self.export_format}")
                return

            self.success.emit(self.file_path, count)

        except Exception as e:
            logger.error(f"导出失败: {e}", exc_info=True)
            self.failed.emit(str(e))

    def _load_all_documents(self) -> list:
        """加载所有知识库文档"""
        from ui.knowledge.data_loader import KnowledgeDataLoader
        loader = KnowledgeDataLoader(self.knowledge_manager)
        docs = loader.load_documents()
        logger.info(f"加载了 {len(docs)} 条文档用于导出")
        return docs

    def _export_csv(self, docs: list) -> int:
        """
        导出为CSV格式

        格式兼容现有CSV导入功能：
        - 标题列 (title): 从 metadata.title 或 metadata.name 或文档name 提取
        - 内容列 (content): 文档内容
        - 标签列 (tags): 从 metadata.tags 提取（如有）
        - 来源列 (source): 从 metadata.source 提取（如有，仅备份用，导入时忽略）

        Args:
            docs: SimpleDocument 列表

        Returns:
            导出的文档数量
        """
        from ui.knowledge.models import DocumentTitleExtractor

        count = 0
        with open(self.file_path, 'w', encoding='utf-8-sig', newline='') as f:
            writer = csv.writer(f, quoting=csv.QUOTE_ALL)

            # 写入表头 — 使用中文列名，与导入系统兼容
            writer.writerow(["标题", "内容", "标签", "来源"])

            for doc in docs:
                title = DocumentTitleExtractor.extract(doc)
                content = doc.content or ""

                # 跳过空内容
                if not content.strip():
                    logger.warning(f"跳过空内容文档: {title}")
                    continue

                # 提取标签
                tags = doc.metadata.get("tags", "") if doc.metadata else ""

                # 提取来源
                source = doc.metadata.get("source", "") if doc.metadata else ""

                writer.writerow([title, content, tags, source])
                count += 1

        logger.info(f"CSV导出完成: {self.file_path}, 共 {count} 条")
        return count

    def _export_json(self, docs: list) -> int:
        """
        导出为JSON格式（完整备份）

        JSON结构:
        {
            "export_info": {
                "version": "1.0",
                "exported_at": "2026-06-25T02:40:00",
                "total_count": 100,
                "format": "knowledge_base_full_backup"
            },
            "documents": [
                {
                    "id": "doc-uuid",
                    "title": "标题",
                    "content": "内容",
                    "metadata": {...},
                    "name": "name属性",
                    "description": "描述"
                },
                ...
            ]
        }

        注意: JSON格式包含完整元数据，但重新导入时需要通过CSV格式。
        建议导出JSON后转换为CSV再导入，或使用CSV格式导出以直接重新导入。

        Args:
            docs: SimpleDocument 列表

        Returns:
            导出的文档数量
        """
        from ui.knowledge.models import DocumentTitleExtractor

        documents = []
        for doc in docs:
            title = DocumentTitleExtractor.extract(doc)
            doc_data = {
                "id": doc.id,
                "title": title,
                "content": doc.content or "",
                "metadata": doc.metadata or {},
                "name": doc.name or "",
                "description": doc.description or ""
            }
            documents.append(doc_data)

        export_data = {
            "export_info": {
                "version": "1.0",
                "exported_at": datetime.now().isoformat(),
                "total_count": len(documents),
                "format": "knowledge_base_full_backup",
                "note": "此文件为完整备份。重新导入请使用CSV格式导出，或基于此JSON生成CSV。"
            },
            "documents": documents
        }

        with open(self.file_path, 'w', encoding='utf-8') as f:
            json.dump(export_data, f, ensure_ascii=False, indent=2)

        count = len(documents)
        logger.info(f"JSON导出完成: {self.file_path}, 共 {count} 条")
        return count


class ClearAllWorker(QThread):
    """
    知识库清空工作线程

    支持两种模式：
    - 完全清空: 删除所有向量数据库记录和内容数据库记录
    - 带备份清空: 先导出备份，再清空

    清空操作直接操作 LanceDB 和 contents_db，确保彻底清除。
    """

    success = pyqtSignal(str, int)  # backup_path_or_empty, deleted_count
    failed = pyqtSignal(str)        # error_message
    progress = pyqtSignal(str)     # progress_message

    def __init__(
        self,
        knowledge_manager: "KnowledgeManager",
        create_backup: bool = False,
        backup_dir: str = ""
    ):
        """
        初始化清空工作线程

        Args:
            knowledge_manager: 知识库管理器
            create_backup: 是否在清空前创建备份
            backup_dir: 备份目录路径
        """
        super().__init__()
        self.knowledge_manager = knowledge_manager
        self.create_backup = create_backup
        self.backup_dir = backup_dir
        self.setObjectName("ClearAllWorker")

    def run(self):
        """在子线程中执行清空"""
        try:
            backup_path = ""

            # 步骤1: 创建备份（如果需要）
            if self.create_backup:
                self.progress.emit("正在创建备份...")
                backup_path = self._create_backup()
                if not backup_path:
                    self.failed.emit("备份创建失败，已中止清空操作")
                    return
                self.progress.emit(f"备份完成: {backup_path}")

            # 步骤2: 统计当前数量
            self.progress.emit("正在统计数据...")
            count_before = self.knowledge_manager.get_content_count()

            if count_before == 0:
                self.success.emit("", 0)
                return

            # 步骤3: 清空向量数据库
            self.progress.emit("正在清空向量数据库...")
            self._clear_vector_db()

            # 步骤4: 清空内容数据库
            self.progress.emit("正在清空内容数据库...")
            self._clear_contents_db()

            # 步骤5: 验证清空结果
            self.progress.emit("正在验证...")
            count_after = self.knowledge_manager.get_content_count()

            if count_after > 0:
                logger.warning(f"清空后仍有 {count_after} 条记录残留")
                self.failed.emit(f"清空不完整，仍有 {count_after} 条记录残留")
                return

            logger.info(f"清空完成，删除了 {count_before} 条记录")
            self.success.emit(backup_path, count_before)

        except Exception as e:
            logger.error(f"清空失败: {e}", exc_info=True)
            self.failed.emit(str(e))

    def _create_backup(self) -> str:
        """
        创建数据库文件备份

        复制 LanceDB 目录和 contents.db 文件到备份目录。

        Returns:
            备份目录路径，失败返回空字符串
        """
        try:
            timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
            backup_path = os.path.join(self.backup_dir, f"kb_backup_{timestamp}")

            os.makedirs(backup_path, exist_ok=True)

            km = self.knowledge_manager

            # 备份向量数据库
            if km.knowledge.vector_db:
                vector_db_path = km.knowledge.vector_db.uri
                if os.path.exists(vector_db_path):
                    vector_backup = os.path.join(backup_path, "vector_db")
                    shutil.copytree(vector_db_path, vector_backup)
                    logger.info(f"向量数据库备份到: {vector_backup}")

            # 备份内容数据库
            if km.knowledge.contents_db:
                db_file = getattr(km.knowledge.contents_db, 'db_file', None)
                if db_file and os.path.exists(db_file):
                    db_backup = os.path.join(backup_path, "contents.db")
                    shutil.copy2(db_file, db_backup)
                    logger.info(f"内容数据库备份到: {db_backup}")

            return backup_path

        except Exception as e:
            logger.error(f"创建备份失败: {e}", exc_info=True)
            return ""

    def _clear_vector_db(self) -> None:
        """通过 IPC 清空向量数据库（lancedb 在独立子进程中运行）"""
        km = self.knowledge_manager

        try:
            # 通过 IPC 调用子进程的 clear_all_knowledge
            deleted_count = km.clear_all_knowledge()
            logger.info(f"通过 IPC 清空知识库，删除 {deleted_count} 条记录")
        except Exception as e:
            logger.error(f"IPC 清空知识库失败: {e}")

    def _clear_contents_db(self) -> None:
        """清空内容数据库中的所有记录"""
        km = self.knowledge_manager

        if not km.knowledge.contents_db:
            logger.warning("内容数据库未初始化，跳过")
            return

        # 使用 agno 的 SqliteDb 接口清空
        # 由于 agno 没有直接的 clear_all 方法，我们直接操作 SQLite
        import sqlite3

        db_file = getattr(km.knowledge.contents_db, 'db_file', None)
        if not db_file or not os.path.exists(db_file):
            logger.warning(f"内容数据库文件不存在: {db_file}")
            return

        conn = sqlite3.connect(db_file)
        try:
            cursor = conn.cursor()

            # 查找所有知识库相关的表
            cursor.execute("SELECT name FROM sqlite_master WHERE type='table'")
            tables = cursor.fetchall()

            for table_info in tables:
                table_name = table_info[0]
                # 跳过 sqlite 内部表
                if table_name.startswith('sqlite_'):
                    continue
                # 清空表数据
                cursor.execute(f"DELETE FROM [{table_name}]")
                logger.info(f"已清空表: {table_name}")

            conn.commit()
            logger.info("内容数据库已清空")

        finally:
            conn.close()
