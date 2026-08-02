"""
LanceDB 独立进程服务端

在子进程中持有真正的 KnowledgeManager（含 LanceDbWithProgress + agno Knowledge），
通过 multiprocessing.Pipe 接收主进程的 IPC 请求。

根因：lancedb C 扩展（lance/arrow/tantivy）的后台线程会破坏 ntdll 堆，
导致主进程点聊天 tab 创建大量 widget 时堆崩溃（地址末三位 0xfc0）。
将 lancedb 隔离到子进程，堆损坏只影响子进程，主进程堆保持干净。

协议：每条请求 = (request_id, method_name, args, kwargs)
      每条响应 = (request_id, status, result_or_error)
      status: "ok" | "error" | "async_result"
"""
import sys
import os
import traceback
import threading

# 确保项目根目录在 path 中
_project_root = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
if _project_root not in sys.path:
    sys.path.insert(0, _project_root)


def _server_main(conn):
    """子进程主函数：加载 KnowledgeManager，循环处理 IPC 请求"""
    # 确保工作目录是项目根目录（Config 读 config.json 依赖 cwd）
    os.chdir(_project_root)

    # 重定向 stdout/stderr 到日志文件，避免子进程输出干扰主进程
    log_path = os.path.join(_project_root, "temp", "lancedb_server.log")
    os.makedirs(os.path.dirname(log_path), exist_ok=True)
    with open(log_path, "a", encoding="utf-8") as log_f:
        sys.stdout = log_f
        sys.stderr = log_f
        print(f"[lancedb_server] 子进程启动 PID={os.getpid()}", flush=True)

        try:
            from Agent.CustomerAgent.agent_knowledge import KnowledgeManager
            print("[lancedb_server] 开始创建 KnowledgeManager...", flush=True)
            km = KnowledgeManager()
            print("[lancedb_server] KnowledgeManager 创建成功", flush=True)
        except Exception:
            print("[lancedb_server] KnowledgeManager 创建失败:", flush=True)
            traceback.print_exc()
            conn.send(("init", "error", traceback.format_exc()))
            conn.close()
            return

        # 通知主进程初始化完成
        conn.send(("init", "ok", None))

        # 请求处理循环
        while True:
            try:
                req = conn.recv()
            except EOFError:
                print("[lancedb_server] 主进程关闭连接，退出", flush=True)
                break

            if req is None:
                # 关闭信号
                print("[lancedb_server] 收到关闭信号，退出", flush=True)
                break

            req_id, method_name, args, kwargs = req

            # 在 knowledge_manager 和 knowledge.vector_db 上查找方法
            target = None
            if hasattr(km, method_name):
                target = getattr(km, method_name)
            elif hasattr(km.knowledge, method_name):
                target = getattr(km.knowledge, method_name)
            elif hasattr(km.knowledge.vector_db, method_name):
                target = getattr(km.knowledge.vector_db, method_name)

            if target is None:
                conn.send((req_id, "error", f"方法不存在: {method_name}"))
                continue

            try:
                result = target(*args, **kwargs)
                conn.send((req_id, "ok", result))
            except Exception as e:
                conn.send((req_id, "error", f"{type(e).__name__}: {e}\n{traceback.format_exc()}"))

    conn.close()


def start_lancedb_server():
    """启动 lancedb 子进程，返回 (conn, process)"""
    from multiprocessing import Process, Pipe

    parent_conn, child_conn = Pipe(duplex=True)
    proc = Process(target=_server_main, args=(child_conn,), daemon=True)
    proc.start()

    # 等待子进程初始化
    try:
        msg = parent_conn.recv()
        status = msg[1]
        if status != "ok":
            error = msg[2] if len(msg) > 2 else "未知错误"
            proc.join(timeout=3)
            raise RuntimeError(f"lancedb 子进程初始化失败:\n{error}")
    except EOFError:
        proc.join(timeout=3)
        raise RuntimeError("lancedb 子进程意外退出")

    return parent_conn, proc
