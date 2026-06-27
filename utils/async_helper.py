"""
异步处理工具
统一处理异步调用的复杂逻辑
"""
import asyncio
import concurrent.futures
from typing import Any, Coroutine, TypeVar, Optional
import logging

logger = logging.getLogger(__name__)

T = TypeVar('T')


def run_async(coro: Coroutine[T, Any, Any]) -> T:
    """
    统一处理异步调用的工具函数

    自动处理事件循环的复杂情况：
    - 如果已有运行中的事件循环，使用线程池运行
    - 如果没有运行中的事件循环，直接运行

    Args:
        coro: 协程对象

    Returns:
        协程的返回值

    Examples:
        >>> async def fetch_data():
        ...     return "data"
        >>> result = run_async(fetch_data())
        >>> print(result)  # "data"
    """
    try:
        # 尝试获取当前运行的事件循环
        loop = asyncio.get_running_loop()

        # 如果有运行中的事件循环，使用线程池运行
        logger.debug("检测到运行中的事件循环，使用线程池执行")
        return run_async_in_thread(coro)

    except RuntimeError:
        # 没有运行中的事件循环，直接运行
        logger.debug("未检测到运行中的事件循环，直接执行")
        return asyncio.run(coro)


async def run_async_multiple(*coros: Coroutine) -> list:
    """
    并发运行多个协程

    Args:
        *coros: 多个协程对象

    Returns:
        协程返回值列表

    Examples:
        >>> async def task1(): return "task1"
        >>> async def task2(): return "task2"
        >>> results = await run_async_multiple(task1(), task2())
        >>> print(results)  # ["task1", "task2"]
    """
    return await asyncio.gather(*coros)


def run_async_in_thread(coro: Coroutine, timeout: Optional[float] = None) -> Any:
    """
    在独立线程中运行异步协程，支持超时取消和资源清理。

    与旧实现不同，此版本在超时时会正确取消 asyncio Task 并等待清理完成
    （包括 Playwright 等需要 finally 清理的资源），避免孤立浏览器进程
    导致 access violation。

    Args:
        coro: 协程对象
        timeout: 超时时间（秒），None表示不限制

    Returns:
        协程的返回值

    Raises:
        TimeoutError: 如果超时
    """
    def _run_with_cleanup():
        """在新线程中创建事件循环，运行协程，确保超时时正确取消和清理"""
        loop = asyncio.new_event_loop()
        asyncio.set_event_loop(loop)
        task = loop.create_task(coro)
        try:
            if timeout is not None:
                result = loop.run_until_complete(
                    asyncio.wait_for(task, timeout=timeout)
                )
            else:
                result = loop.run_until_complete(task)
            return result
        except asyncio.TimeoutError:
            # 超时时取消任务，等待 finally 清理完成
            logger.warning(f"协程执行超时 ({timeout}秒)，正在取消并清理资源")
            if not task.done():
                task.cancel()
                try:
                    loop.run_until_complete(task)
                except (asyncio.CancelledError, Exception):
                    pass
            raise TimeoutError(f"协程执行超时 ({timeout}秒)")
        except Exception:
            # 其他异常也确保任务被取消
            if not task.done():
                task.cancel()
                try:
                    loop.run_until_complete(task)
                except (asyncio.CancelledError, Exception):
                    pass
            raise
        finally:
            try:
                # 运行剩余的 async generator 清理
                loop.run_until_complete(loop.shutdown_asyncgens())
            except Exception:
                pass
            try:
                loop.close()
            except Exception:
                pass

    with concurrent.futures.ThreadPoolExecutor(max_workers=1) as executor:
        future = executor.submit(_run_with_cleanup)
        # 不设 future.result timeout，因为内部已用 asyncio.wait_for 处理
        return future.result()
