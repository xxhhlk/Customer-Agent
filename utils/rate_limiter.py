"""
用户级别频率限制器 - 固定窗口计数器模式

用于限制单个用户在指定时间窗口内的API请求次数，防止单个用户过度消耗资源。
"""

import asyncio
import time
from typing import Optional
from loguru import logger


class RateLimiter:
    """用户级别频率限制器 - 固定窗口计数器
    
    特点：
    - 从第一条消息开始计时，窗口结束后重置计数
    - 线程安全（使用 asyncio.Lock）
    - 自动清理过期记录
    
    使用示例：
        limiter = RateLimiter(window_seconds=14400, max_requests=10)  # 4小时10次
        
        if await limiter.is_allowed():
            await limiter.record_request()
            # 执行API调用
        else:
            # 返回兜底回复
    """
    
    def __init__(self, window_seconds: int = 14400, max_requests: int = 10):
        """初始化频率限制器
        
        Args:
            window_seconds: 时间窗口时长（秒），默认14400秒（4小时）
            max_requests: 窗口内最大请求次数，默认10次
        """
        self.window_seconds = window_seconds
        self.max_requests = max_requests
        self._requests: list[float] = []  # 请求时间戳列表
        self._lock = asyncio.Lock()
        self._logger = logger.bind(component="RateLimiter")
    
    async def is_allowed(self) -> bool:
        """检查是否允许请求
        
        Returns:
            bool: True 表示允许请求，False 表示已达到限制
        """
        async with self._lock:
            self._cleanup_expired()
            return len(self._requests) < self.max_requests
    
    async def record_request(self) -> None:
        """记录一次请求"""
        async with self._lock:
            self._requests.append(time.time())
            self._logger.debug(
                f"记录请求，当前窗口内已请求 {len(self._requests)}/{self.max_requests} 次"
            )
    
    def _cleanup_expired(self) -> None:
        """清理过期记录
        
        注意：此方法在持有锁的情况下调用，不需要额外加锁
        """
        if not self._requests:
            return
        
        # 从第一条消息开始计时
        window_start = self._requests[0]
        cutoff = window_start + self.window_seconds
        now = time.time()
        
        # 窗口结束，重置计数
        if now >= cutoff:
            old_count = len(self._requests)
            self._requests.clear()
            self._logger.info(
                f"时间窗口结束，重置计数器（原请求次数: {old_count}）"
            )
    
    def get_status(self) -> dict:
        """获取当前状态（用于调试和监控）
        
        Returns:
            dict: 包含当前请求次数、剩余次数、窗口信息等
        """
        now = time.time()
        
        if not self._requests:
            return {
                "request_count": 0,
                "remaining": self.max_requests,
                "window_start": None,
                "window_end": None,
                "time_until_reset": None
            }
        
        window_start = self._requests[0]
        window_end = window_start + self.window_seconds
        time_until_reset = max(0, window_end - now)
        
        return {
            "request_count": len(self._requests),
            "remaining": max(0, self.max_requests - len(self._requests)),
            "window_start": window_start,
            "window_end": window_end,
            "time_until_reset": time_until_reset
        }


class UserRateLimiterManager:
    """用户频率限制器管理器
    
    为每个用户维护独立的 RateLimiter 实例
    """
    
    def __init__(self, window_seconds: int = 14400, max_requests: int = 10):
        """初始化管理器
        
        Args:
            window_seconds: 时间窗口时长（秒）
            max_requests: 窗口内最大请求次数
        """
        self.window_seconds = window_seconds
        self.max_requests = max_requests
        self._limiters: dict[str, RateLimiter] = {}
        self._lock = asyncio.Lock()
        self._logger = logger.bind(component="UserRateLimiterManager")
    
    async def get_limiter(self, user_id: str) -> RateLimiter:
        """获取或创建用户的频率限制器
        
        Args:
            user_id: 用户ID
            
        Returns:
            RateLimiter: 用户的频率限制器实例
        """
        async with self._lock:
            if user_id not in self._limiters:
                self._limiters[user_id] = RateLimiter(
                    window_seconds=self.window_seconds,
                    max_requests=self.max_requests
                )
                self._logger.debug(f"为用户 {user_id} 创建新的频率限制器")
            return self._limiters[user_id]
    
    async def is_allowed(self, user_id: str) -> bool:
        """检查用户是否允许请求
        
        Args:
            user_id: 用户ID
            
        Returns:
            bool: True 表示允许，False 表示已达到限制
        """
        limiter = await self.get_limiter(user_id)
        return await limiter.is_allowed()
    
    async def record_request(self, user_id: str) -> None:
        """记录用户的请求
        
        Args:
            user_id: 用户ID
        """
        limiter = await self.get_limiter(user_id)
        await limiter.record_request()
    
    async def get_user_status(self, user_id: str) -> dict:
        """获取用户的频率限制状态
        
        Args:
            user_id: 用户ID
            
        Returns:
            dict: 用户频率限制状态信息
        """
        limiter = await self.get_limiter(user_id)
        return limiter.get_status()
    
    async def cleanup_inactive_users(self, inactive_threshold: int = 86400) -> int:
        """清理长时间不活跃用户的限制器（可选功能）
        
        Args:
            inactive_threshold: 不活跃阈值（秒），默认24小时
            
        Returns:
            int: 清理的用户数量
        """
        async with self._lock:
            now = time.time()
            to_remove = []
            
            for user_id, limiter in self._limiters.items():
                status = limiter.get_status()
                # 如果窗口已结束且没有新请求，则清理
                if status["window_end"] and now - status["window_end"] > inactive_threshold:
                    to_remove.append(user_id)
            
            for user_id in to_remove:
                del self._limiters[user_id]
            
            if to_remove:
                self._logger.info(f"清理了 {len(to_remove)} 个不活跃用户的频率限制器")
            
            return len(to_remove)
