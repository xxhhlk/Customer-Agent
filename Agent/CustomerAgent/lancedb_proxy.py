"""
LanceDB 独立进程客户端代理

在主进程中替代真正的 LanceDbWithProgress 和 KnowledgeManager。
所有调用通过 multiprocessing.Pipe 转发到子进程。

这样 lancedb 的 C 扩展（lance/arrow/tantivy）只在子进程中运行，
其后台线程破坏的 ntdll 堆只影响子进程，主进程堆保持干净。
"""
import asyncio
import threading
from typing import Any, Dict, List, Optional, Callable
from loguru import logger

from agno.knowledge.document import Document
from agno.knowledge.knowledge import Knowledge
from agno.vectordb.search import SearchType


class _IPCException(Exception):
    pass


class LanceDbProxy:
    """
    LanceDbWithProgress 的 IPC 代理。

    实现 agno VectorDb 接口 + LanceDbWithProgress 重写的方法。
    所有调用通过 Pipe 转发到子进程中真正的 LanceDbWithProgress 实例。

    agno 框架通过 knowledge.vector_db 访问此代理，
    调用 search/insert/upsert/exists/content_hash_exists 等方法时，
    请求被转发到子进程执行，结果（Document 列表等）序列化传回。
    """

    # 声明属性类型（和 LanceDbWithProgress 保持一致）
    progress_callback: Optional[Callable]
    cancel_token: Optional[Any]
    on_bad_vectors: Optional[str]
    fill_value: Optional[float]
    table: Optional[Any]
    search_type: SearchType
    embedder: Any
    uri: str
    table_name: str

    def __init__(self, ipc_client: 'LanceDbIPCClient'):
        self._ipc = ipc_client
        # 属性占位，agno 框架会访问这些属性
        self.progress_callback = None
        self.cancel_token = None
        self.on_bad_vectors = None
        self.fill_value = None
        self.table = None
        self.search_type = SearchType.vector
        self.embedder = None  # embedder 留空，子进程有自己的 embedder
        self.uri = ""
        self.table_name = "customer_knowledge"

    def _call(self, method: str, *args, **kwargs):
        """同步 IPC 调用"""
        return self._ipc.call(method, *args, **kwargs)

    async def _call_async(self, method: str, *args, **kwargs):
        """异步 IPC 调用（在线程池中执行同步调用）"""
        return await asyncio.to_thread(self._call, method, *args, **kwargs)

    # === agno VectorDb 基类接口 ===

    def exists(self) -> bool:
        return self._call("exists")

    def create(self) -> None:
        self._call("create")

    def content_hash_exists(self, content_hash: str) -> bool:
        return self._call("content_hash_exists", content_hash)

    def upsert_available(self) -> bool:
        return self._call("upsert_available")

    def search(self, query: str, limit: int = 5, filters: Optional[Any] = None) -> List[Document]:
        return self._call("search", query, limit, filters)

    async def async_search(self, query: str, limit: int = 5, filters: Optional[Any] = None) -> List[Document]:
        return await self._call_async("async_search", query, limit, filters)

    def insert(self, content_hash: str, documents: List[Document], filters: Optional[Dict[str, Any]] = None) -> None:
        self._call("insert", content_hash, documents, filters)

    async def async_insert(self, content_hash: str, documents: List[Document], filters: Optional[Dict[str, Any]] = None) -> None:
        await self._call_async("async_insert", content_hash, documents, filters)

    def upsert(self, content_hash: str, documents: List[Document], filters: Optional[Dict[str, Any]] = None) -> None:
        self._call("upsert", content_hash, documents, filters)

    async def async_upsert(self, content_hash: str, documents: List[Document], filters: Optional[Dict[str, Any]] = None) -> None:
        await self._call_async("async_upsert", content_hash, documents, filters)

    def update_metadata(self, content_id: str, metadata: Dict[str, Any]) -> None:
        # no-op（和 LanceDbWithProgress 一样跳过）
        return

    def delete(self) -> bool:
        return self._call("delete")

    def drop(self) -> None:
        self._call("drop")

    def optimize(self) -> None:
        self._call("optimize")

    # === LanceDbWithProgress 额外接口 ===

    def _prepare_vector(self, embedding: Any) -> Any:
        # 这个方法在子进程的 insert 内部调用，主进程不需要
        # 但 agno 可能直接调用，转发到子进程
        return self._call("_prepare_vector", embedding)


class KnowledgeProxy(Knowledge):
    """
    agno Knowledge 的 IPC 代理。

    持有 LanceDbProxy 作为 vector_db，search/insert 等方法通过代理转发。
    agno Agent 创建时传入此对象作为 knowledge 参数。
    """

    def __init__(self, ipc_client: 'LanceDbIPCClient', vector_db_proxy: LanceDbProxy):
        # 不调用 Knowledge.__init__（它会尝试创建 vector_db 等）
        # 只设置必要的属性
        self.vector_db = vector_db_proxy
        self._ipc = ipc_client
        # Knowledge 基类需要的属性
        self.contents_db = None  # contents_db 在子进程中，主进程不直接访问
        self.readers = []
        self.max_results = 3


class KnowledgeManagerProxy:
    """
    KnowledgeManager 的 IPC 代理。

    替代真正的 KnowledgeManager，所有方法通过 Pipe 转发到子进程。
    UI 和 agent.py 通过此代理调用 search_knowledge/add_content_from_file/delete_document 等。
    """

    def __init__(self, ipc_client: 'LanceDbIPCClient'):
        self._ipc = ipc_client
        # 创建 knowledge 代理（给 agno Agent 用）
        self._vector_db_proxy = LanceDbProxy(ipc_client)
        self.knowledge = KnowledgeProxy(ipc_client, self._vector_db_proxy)

    def _call(self, method: str, *args, **kwargs):
        return self._ipc.call(method, *args, **kwargs)

    async def _call_async(self, method: str, *args, **kwargs):
        return await asyncio.to_thread(self._call, method, *args, **kwargs)

    # === KnowledgeManager 接口 ===

    async def add_content_from_file(self, file_path: str) -> int:
        return await self._call_async("add_content_from_file", file_path)

    def search_knowledge(self, query: str, limit: Optional[int] = None) -> list:
        return self._call("search_knowledge", query, limit)

    def get_content_count(self) -> int:
        return self._call("get_content_count")

    def get_all_contents(self) -> list:
        return self._call("get_all_contents")

    def delete_document(self, doc_id: str) -> bool:
        return self._call("delete_document", doc_id)

    async def add_text_content(self, title: str, content: str) -> bool:
        return await self._call_async("add_text_content", title, content)

    async def update_document_content(self, doc_id: str, title: str, content: str) -> Optional[str]:
        return await self._call_async("update_document_content", doc_id, title, content)

    def modify_document(self, doc_id: str, file_path: str) -> bool:
        return self._call("modify_document", doc_id, file_path)

    def get_document_vector_info(self, doc_id: str) -> dict:
        return self._call("get_document_vector_info", doc_id)

    def clear_all_knowledge(self) -> int:
        return self._call("clear_all_knowledge")

    def get_all_documents_for_export(self) -> list:
        return self._call("get_all_documents_for_export")


class LanceDbIPCClient:
    """
    IPC 客户端：管理子进程连接，提供线程安全的 call() 方法。

    内部用 multiprocessing.Pipe + 锁保证多线程安全调用。
    """

    def __init__(self):
        self._conn = None
        self._proc = None
        self._lock = threading.Lock()
        self._req_id = 0
        self._started = False

    def start(self):
        """启动子进程"""
        from Agent.CustomerAgent.lancedb_server import start_lancedb_server
        logger.info("[LanceDbIPC] 启动 lancedb 子进程...")
        self._conn, self._proc = start_lancedb_server()
        self._started = True
        logger.info(f"[LanceDbIPC] 子进程启动成功 PID={self._proc.pid}")

    def stop(self):
        """停止子进程"""
        if not self._started:
            return
        try:
            self._conn.send(None)  # 关闭信号
        except Exception:
            pass
        self._proc.join(timeout=5)
        if self._proc.is_alive():
            self._proc.terminate()
            self._proc.join(timeout=3)
        self._started = False
        logger.info("[LanceDbIPC] 子进程已停止")

    def call(self, method: str, *args, **kwargs):
        """同步调用子进程方法（线程安全）"""
        if not self._started:
            raise _IPCException("IPC 未启动")

        with self._lock:
            self._req_id += 1
            req_id = self._req_id
            try:
                self._conn.send((req_id, method, args, kwargs))
                resp = self._conn.recv()
            except (EOFError, OSError) as e:
                raise _IPCException(f"子进程连接断开: {e}")

        if len(resp) < 3:
            raise _IPCException(f"子进程返回格式错误: {resp}")

        ret_id, status, data = resp[0], resp[1], resp[2]
        if ret_id != req_id:
            raise _IPCException(f"请求 ID 不匹配: 期望 {req_id}，收到 {ret_id}")

        if status == "ok":
            return data
        else:
            raise _IPCException(f"子进程调用 {method} 失败: {data}")

    @property
    def is_started(self) -> bool:
        return self._started


# 全局单例
_ipc_client: Optional[LanceDbIPCClient] = None
_ipc_lock = threading.Lock()


def get_ipc_client() -> LanceDbIPCClient:
    """获取全局 IPC 客户端单例"""
    global _ipc_client
    if _ipc_client is None:
        with _ipc_lock:
            if _ipc_client is None:
                _ipc_client = LanceDbIPCClient()
    return _ipc_client


def get_knowledge_manager_proxy() -> KnowledgeManagerProxy:
    """获取 KnowledgeManager 代理（延迟启动子进程）"""
    client = get_ipc_client()
    if not client.is_started:
        client.start()
    return KnowledgeManagerProxy(client)
